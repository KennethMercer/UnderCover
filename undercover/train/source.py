"""Find and download source audio for a person with yt-dlp (YouTube search, or any link yt-dlp supports)."""
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..config import CONFIG
from ..models import ModelError

TCFG = CONFIG["training"]

QUERIES = {
    "speaking": ["{name} interview", "{name} full speech", "{name} podcast", "{name} talks"],
    "singing": ["{name} isolated vocals", "{name} acapella", "{name} live vocals", "{name} live"],
}
# Singers: their singing is what the voice will be used for, so search mostly for sung material (isolated vocals
# and acapellas separate cleanly); one interview search is the backup if not enough singing turns up.
QUERIES["both"] = ["{name} isolated vocals", "{name} acapella", "{name} vocals only", "{name} live acoustic",
                   "{name} interview"]
# Titles that usually mean someone else's voice, a fake, or a mash-up. Shown unticked with the reason.
SUSPECT = [
    (re.compile(r"\bimpressions?\b|\bimpersonat|\bdoes an? .*voice|\bparody\b|\bsnl\b|\brap battles?\b", re.I), "may be an impersonator"),
    (re.compile(r"\bai\b|\bdeepfake|\bvoice ?clon|\brvc\b|\bai cover\b|\bsynthesi[sz]ed\b", re.I), "may be an AI voice"),
    (re.compile(r"\breacts?\b|\breaction\b|\breacting\b", re.I), "a reaction video (mostly someone else)"),
    (re.compile(r"\bcompilation\b|\bmashup\b|\bmeme\b|\bremix\b|\bnightcore\b|\bsped up\b|\bslowed\b", re.I),
     "edited or mixed audio"),
    (re.compile(r"\bkaraoke\b|\binstrumental\b|\bbacking track\b", re.I), "no vocals"),
    (re.compile(r"\bcover\b", re.I), "probably another singer"),
]
COMMENTARY = re.compile(r"destroys|obliterates|slams|owns|claps back|shuts down|what were they thinking|baffling|"
                        r"breaks down|explained|analysis|tier list|ranking|top \d+", re.I)
GOOD = {"speaking": re.compile(r"interview|podcast|speech|address|conversation|talk|keynote|q&a|lecture", re.I),
        "singing": re.compile(r"acapella|a cappella|isolated|vocals? only|live|unplugged|session", re.I)}


# Spoken-word "album tracks": YouTube Music lists speeches, audiobooks and narration as music (Barack Obama:
# "Obama DNC Speech", "Presidential Oath Of Office"; Morgan Freeman's narration albums). Not singing.
SPOKEN = re.compile(r"speech|address|remarks|rally|debate|oath|interview|keynote|sermon|commencement|audiobook|"
                    r"chapter|lecture|conversation|podcast|town hall|state of the union|press conference|q ?& ?a|"
                    r"narrat|poem|reading|spoken|monologue|stand-?up|comedy|testimony|eulogy|introduc", re.I)


def is_sung(src) -> bool:
    """A source of the person singing: their own album tracks, or a title saying so (acapella, isolated vocals,
    live...). Used to find their singing voice and to prefer singing. Accepts a Source or a title."""
    if not isinstance(src, str):
        if (src.extra or {}).get("sung"):
            return True
        src = src.title
    return bool(GOOD["singing"].search(src)) and not GOOD["speaking"].search(src)


def album_tracks(name: str, n: int = 20) -> list:
    """The person's own studio recordings from YouTube Music (their "Artist - Topic" channel): consistent,
    well-recorded singing, separated here with the same model as covers. Fan acapellas vary a lot (Elliott Smith's
    Clementine upload was too hissy to use) and there are only a handful per artist."""
    from .. import songs
    words = [w for w in re.findall(r"\w+", name.lower()) if len(w) > 2]
    out: dict = {}
    for q in (name, f"{name} songs"):
        try:
            found = songs._music_tracks(q, n=n)
        except YouTubeBlocked:
            raise
        except Exception:
            continue
        for r in found:
            artist = r.channel.lower().replace(" - topic", "")
            if not words or not all(w in artist for w in words) or r.id in out:
                continue   # only recordings by the person themselves
            if SPOKEN.search(r.title):
                continue   # a speech or audiobook listed as music
            out[r.id] = Source(id=r.id, url=r.url, title=r.title, channel=r.channel, duration=float(r.duration or 0),
                               note="album track", extra={"sung": True})
    return list(out.values())


@dataclass
class Source:
    id: str
    url: str
    title: str
    channel: str = ""
    duration: float = 0.0          # seconds
    selected: bool = True
    note: str = ""                 # why it was unticked, or the kind of video
    status: str = "found"          # found | downloading | separating | ready | failed
    error: str = ""
    user: bool = False             # added by the operator (link or file)
    extra: dict = field(default_factory=dict)

    def to_json(self):
        return asdict(self)


def _ydl(**opts):
    try:
        import yt_dlp
    except ImportError:
        raise ModelError("yt-dlp isn't installed in UnderCover's environment, so clips can't be found online. "
                         "You can still add your own recordings.")
    base = {"quiet": True, "no_warnings": True, "noprogress": True, "logger": _QuietLogger()}
    return yt_dlp.YoutubeDL({**base, **opts})


def _cancelled_in(e):
    """A Cancelled raised in our progress hook comes back wrapped by yt-dlp: find it."""
    cause = e
    while cause is not None:
        if cause.__class__.__name__ == "Cancelled":
            return cause
        cause = getattr(cause, "exc_info", [None, None])[1] if getattr(cause, "exc_info", None) else cause.__context__
    return None


class YouTubeBlocked(ModelError):
    """YouTube's bot check ("Sign in to confirm you're not a bot"): too many requests from this PC. It clears by
    itself after a while; retrying right away only prolongs it."""


BLOCKED_MSG = ("YouTube is temporarily blocking downloads from this PC (it asks to confirm you're not a bot, usually "
               "after a lot of downloads in a short time). It clears by itself, usually within a few hours.")


def check_blocked(e: BaseException):
    t = str(e)
    if "not a bot" in t or "Sign in to confirm" in t:
        raise YouTubeBlocked(BLOCKED_MSG)


TRANSIENT = ("403", "429", "HTTP Error 5", "timed out", "Connection", "Remote end closed", "IncompleteRead")


def download_retrying(ydl, url: str, attempts: int = 3):
    """ydl.download with retries: YouTube throws a 403 at the audio stream now and then (2026-10-10: 4 of 7 videos
    in a row, all fine on retry). Raises the last error; a cancel is raised immediately."""
    import time
    for i in range(attempts):
        try:
            return ydl.download([url])
        except Exception as e:
            c = _cancelled_in(e)
            if c is not None:
                raise c
            if i == attempts - 1 or not any(t in str(e) for t in TRANSIENT):
                raise
            time.sleep(2 + 3 * i)


class _QuietLogger:
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


def _rank(src: Source, name: str, mode: str) -> float:
    t = f"{src.title} {src.channel}".lower()
    score = 0.0
    name_tokens = [w for w in re.findall(r"\w+", name.lower()) if len(w) > 1]
    score += 2.0 * sum(w in t for w in name_tokens) / max(1, len(name_tokens))
    sung = mode != "speaking" and is_sung(src)
    if mode == "both":   # singers: sung sources first, interviews only as backup
        score += 2.0 if sung else (0.5 if GOOD["speaking"].search(t) else 0.0)
    elif GOOD["singing" if mode == "singing" else "speaking"].search(t):
        score += 1.0
    mins = src.duration / 60
    lo, hi = (1.5, 15) if mode == "singing" or sung else (8, 120)
    score += 1.0 if lo <= mins <= hi else (-1.0 if mins < 1.5 else 0.0)
    if any(k in t for k in ("official audio", "lyrics", "lyric video")):
        score -= 0.5  # studio mixes separate fine, but fan uploads are often re-encoded
    # Commentary/clickbait uploads ("X OBLITERATES Y", "What were they thinking?") are mostly someone else talking.
    shouty = [w for w in re.findall(r"\b[A-Z]{4,}\b", src.title) if w.lower() not in name_tokens]
    if shouty or COMMENTARY.search(t):
        score -= 1.5
    return score


def search(name: str, mode: str = "speaking", per_query: int | None = None) -> list[Source]:
    """Search YouTube with a few query variants, dedupe, flag suspicious titles, rank best first."""
    name = name.strip()
    if not name:
        raise ModelError("Type the name of the person whose voice you want.")
    per_query = per_query or TCFG["search_per_query"]
    found: dict[str, Source] = {}
    with _ydl(extract_flat="in_playlist", skip_download=True) as ydl:
        for q in QUERIES.get(mode, QUERIES["speaking"]):
            try:
                info = ydl.extract_info(f"ytsearch{per_query}:{q.format(name=name)}", download=False)
            except Exception:
                continue
            for e in info.get("entries") or []:
                vid = e.get("id")
                if not vid or vid in found or not e.get("duration"):
                    continue  # live streams and shorts without a duration are skipped
                src = Source(id=vid, url=f"https://www.youtube.com/watch?v={vid}", title=e.get("title") or vid,
                             channel=e.get("channel") or e.get("uploader") or "", duration=float(e["duration"]))
                for rx, why in SUSPECT:
                    if rx.search(src.title) and not (why == "probably another singer" and mode == "speaking"):
                        src.selected, src.note = False, why
                        break
                found[vid] = src
    albums = album_tracks(name) if mode != "speaking" else []
    if not found and not albums:
        raise ModelError(f"No videos found for '{name}'. Check the spelling, or paste links / add recordings.")
    ranked = albums + sorted([s for s in found.values() if s.id not in {a.id for a in albums}], key=lambda s: -_rank(s, name, mode))
    # Tick the best sources until there is comfortably enough raw audio for the target.
    want = TCFG["target_minutes"] * 60 * 4   # assume ~1/4 of the used audio survives as clean target voice
    have = 0.0
    for s in ranked:
        if not s.selected:
            continue
        if have >= want or s.duration < 60:
            s.selected = False
            continue
        have += min(s.duration, TCFG["max_minutes_per_source"] * 60)
        if not s.note:
            m = GOOD["singing"].search(s.title) or GOOD["speaking"].search(s.title)
            s.note = m.group(0).lower() if m else ""
    # sung sources are songs (2-4 min each), so singers need more of them listed to reach the target
    return ranked[:TCFG["search_results"] * (2 if mode == "both" else 1) + len(albums)]


def from_link(url: str) -> list[Source]:
    """A pasted video or playlist link -> sources (metadata only)."""
    if not re.match(r"https?://", url.strip()):
        raise ModelError("That doesn't look like a link. Paste a full address starting with https://")
    with _ydl(extract_flat="in_playlist", skip_download=True) as ydl:
        try:
            info = ydl.extract_info(url.strip(), download=False)
        except Exception as e:
            raise ModelError(f"Couldn't read that link ({_short(e)}).")
    entries = info.get("entries") or [info]
    out = []
    for e in entries[:50]:
        vid = e.get("id") or "link"
        out.append(Source(id=re.sub(r"[^\w-]", "_", vid)[:40], url=e.get("webpage_url") or e.get("url") or url,
                          title=e.get("title") or vid, channel=e.get("channel") or e.get("uploader") or "",
                          duration=float(e.get("duration") or 0), user=True, note="added by you"))
    return out


def _short(e: Exception) -> str:
    msg = re.sub(r"\x1b\[[0-9;]*m", "", str(e)).replace("ERROR: ", "")
    return msg.split("\n")[0][:160]


def download(src: Source, dest_dir: Path, progress=None) -> Path:
    """Download the audio as 44.1 kHz mono wav. Long videos are trimmed to max_minutes_per_source, skipping
    the first minute (intros, music) when the video is long enough. Returns the wav path."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = dest_dir / f"{src.id}.wav"
    if out.exists() and out.stat().st_size > 0:
        return out
    # The whole audio stream is fetched (yt-dlp's range download streams through ffmpeg at about real time,
    # ~10x slower) and trimmed locally.
    cap = TCFG["max_minutes_per_source"] * 60
    start = 60 if src.duration > cap + 120 else 0
    trim = ["-ss", str(start), "-t", str(cap)] if src.duration and src.duration > cap else []
    # Download into a private temp folder and move the finished wav into place last: an interrupted download (crash,
    # power cut) then never leaves a truncated "<id>.wav" that later runs would trust.
    import shutil
    work = dest_dir / f".dl_{src.id}"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    opts = {
        "format": "bestaudio[abr<=160]/bestaudio/best",
        "outtmpl": str(work / f"{src.id}.%(ext)s"),
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "wav"}],
        "postprocessor_args": {"extractaudio": trim + ["-ac", "1", "-ar", "44100"]},
        "overwrites": True,
    }
    if progress:
        def hook(d):
            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                if total:
                    progress(min(1.0, d.get("downloaded_bytes", 0) / total))
        opts["progress_hooks"] = [hook]
    try:
        with _ydl(**opts) as ydl:
            try:
                download_retrying(ydl, src.url)
            except Exception as e:
                if _cancelled_in(e) is not None:
                    raise
                check_blocked(e)
                raise ModelError(f"Couldn't download '{src.title}' ({_short(e)}). If YouTube downloads keep failing, "
                                 "update the YouTube downloader (Library, top right).")
        cands = [p for p in work.glob(f"{src.id}.*") if p.suffix == ".wav"]
        if not cands or cands[0].stat().st_size == 0:
            raise ModelError(f"Downloaded '{src.title}' but couldn't convert it to audio.")
        cands[0].replace(out)
        return out
    finally:
        shutil.rmtree(work, ignore_errors=True)
