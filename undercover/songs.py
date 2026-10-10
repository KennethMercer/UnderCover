"""Find a song on YouTube and fetch its audio for a cover (yt-dlp).

Music videos often differ from the record (intros, skits, sound effects, extended endings), so the album track
is preferred: YouTube Music's song search comes first (exact album audio, with artist and album metadata; ~4 s),
then plain YouTube as a fallback, where music videos and anything noticeably longer than the album track are
ranked down and labelled. Live versions, covers, karaoke, remixes and sped-up edits are pushed down unless asked
for. Audio is saved as FLAC (a lossless copy of what YouTube serves).
"""
import json
import math
import re
import shutil
import subprocess
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import path
from .models import ModelError
from .train.source import _QuietLogger, _short

BAD = [(re.compile(p, re.I), w) for p, w in [
    (r"\blive\b|\bconcert\b|\btour\b|\bperforms?\b|\bperformance\b", 2.0),
    (r"\bcover\b|\bkaraoke\b|\binstrumental\b|\bacapella\b|\ba cappella\b", 3.0),
    (r"\bremix\b|\bsped up\b|\bslowed\b|\bnightcore\b|\b8d\b|\breverb\b|\bbass boosted\b|\bmashup\b", 3.0),
    (r"\breaction\b|\breacts?\b|\btutorial\b|\blesson\b|\bpiano\b|\bguitar\b|\bdrum\b", 3.0),
    (r"\bai\b|\brvc\b", 3.0),
    (r"\blyrics?\b|\blyric video\b", 0.5),
    (r"\bmusic video\b|\bofficial video\b|\bvideo\b|\bvisualizer\b|\bmv\b", 2.5),   # intros, skits, extra audio
]]
GOOD = [(re.compile(p, re.I), w) for p, w in [
    (r"official audio|\(audio\)|\[audio\]", 2.0),
    (r"\bofficial\b", 0.5),
]]


@dataclass
class SongResult:
    id: str
    url: str
    title: str          # display title, "Artist - Song" when known
    raw_title: str
    channel: str
    duration: float
    score: float = 0.0
    note: str = ""
    album: str = ""
    views: int = 0
    kind: str = "video"     # "track" (YouTube Music album audio) | "video"

    def to_json(self):
        return asdict(self)


def _ydl(**opts):
    try:
        import yt_dlp
    except ImportError:
        raise ModelError("yt-dlp isn't installed in UnderCover's environment, so songs can't be fetched from YouTube.")
    return yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "noprogress": True, "logger": _QuietLogger(), **opts})


def _display_title(title: str, channel: str) -> str:
    t = re.sub(r"\s*[\(\[](official|audio|lyrics?|lyric video|music video|visualizer|hd|hq|4k|remaster(ed)?[^)\]]*)[^)\]]*[\)\]]", "",
               title, flags=re.I).strip(" -|")
    if channel.endswith(" - Topic") and " - " not in t:
        t = f"{channel[:-8]} - {t}"   # YouTube Music uploads: title is just the song, channel is "Artist - Topic"
    return t or title


def _score(r: SongResult, q_tokens: list[str]) -> float:
    t = f"{r.title} {r.raw_title} {r.channel}".lower()
    s = 2.0 * sum(tok in t for tok in q_tokens) / max(1, len(q_tokens))
    if r.kind == "track":
        s += 3.0   # the record itself
    # Query words that aren't in the song title are usually the artist: reward a matching artist/channel.
    # (Only those words: a channel called "Toxic Arts" says nothing about a search for "Toxic".)
    title_tokens = set(re.findall(r"\w+", r.raw_title.lower()))
    artist_words = set(q_tokens) - title_tokens
    who = set(re.findall(r"\w+", f"{r.title.split(' - ')[0] if ' - ' in r.title else ''} {r.channel}".lower().replace("vevo", " ")))
    if artist_words and artist_words & who:
        s += 1.5
    # Popularity separates the original from covers and re-recordings with the same title.
    if r.views:
        s += max(-1.5, min(3.0, 0.8 * (math.log10(r.views + 10) - 6)))
    for rx, w in GOOD:
        if rx.search(r.raw_title):
            s += w
    for rx, w in BAD:
        if rx.search(f"{r.raw_title} {r.album}") and not rx.search(" ".join(q_tokens)):   # unless asked for
            s -= w
    m = r.duration / 60
    s += 1.0 if 1.5 <= m <= 8 else (-3.0 if m > 15 or m < 1 else -0.5)
    return s


def _music_tracks(query: str, n: int = 6) -> list[SongResult]:
    """YouTube Music song search: album tracks with artist/album metadata (details fetched in parallel)."""
    from concurrent.futures import ThreadPoolExecutor
    url = "https://music.youtube.com/search?q=" + "+".join(query.split()) + "#songs"
    try:
        with _ydl(extract_flat="in_playlist", skip_download=True, playlistend=n + 1) as ydl:
            ids = [e["id"] for e in (ydl.extract_info(url, download=False).get("entries") or [])[:n] if e.get("id")]
    except Exception:
        return []

    def detail(vid):
        try:
            with _ydl(skip_download=True) as ydl:
                return ydl.extract_info(f"https://www.youtube.com/watch?v={vid}", download=False, process=False)
        except Exception:
            return None
    with ThreadPoolExecutor(max_workers=4) as pool:
        infos = list(pool.map(detail, ids))
    out = []
    for rank, d in enumerate(infos):
        if not d or not d.get("duration"):
            continue
        artists = d.get("artists") or ([d["artist"]] if d.get("artist") else [])
        artist = ", ".join(artists[:2]) if isinstance(artists, list) else str(artists)
        title = d.get("track") or d.get("title") or d["id"]
        r = SongResult(id=d["id"], url=f"https://www.youtube.com/watch?v={d['id']}", raw_title=d.get("title") or title,
                       title=f"{artist} - {title}" if artist else title, channel=d.get("channel") or artist,
                       duration=float(d["duration"]), album=d.get("album") or "", kind="track",
                       views=int(d.get("view_count") or 0),
                       note="album track" + (f" · {d['album']}" if d.get("album") else ""))
        r.score = 0.6 - 0.3 * rank   # YouTube Music's own order (popularity) is a good tiebreaker
        out.append(r)
    return out


def search(query: str, limit: int = 8, style: str | None = None, words: list[str] | None = None) -> list[SongResult]:
    """Best matches for a song; the first one is what a one-shot cover uses. With a style ("acoustic"), the
    search asks for that version and only titles mentioning one of `words` rank well (the original studio
    recording drops to the bottom)."""
    if style:
        words = [w.lower() for w in (words or [style])]
        res = search(f"{query} {style} cover", limit=limit * 2)
        for r in res:
            t = f"{r.raw_title} {r.title} {r.album}".lower()
            r.score = round(r.score + (5.0 if any(w in t for w in words) else -6.0), 2)
            if re.search(r"\binstrumental\b|\bkaraoke\b|\btutorial\b|\blesson\b|\btabs?\b|\bchords\b|\bbacking track\b", t):
                r.score -= 4.0   # we need someone singing
                r.note = (r.note + " · " if r.note else "") + "probably no singing"
        return sorted(res, key=lambda r: -r.score)[:limit]
    from concurrent.futures import ThreadPoolExecutor
    query = query.strip()
    if not query:
        raise ModelError("Type a song name (and the artist, if you know it).")
    q_tokens = [w for w in re.findall(r"\w+", query.lower()) if len(w) > 1]

    def videos():
        res = []
        with _ydl(extract_flat="in_playlist", skip_download=True) as ydl:
            try:
                info = ydl.extract_info(f"ytsearch8:{query}", download=False)
            except Exception:
                return res
            for e in info.get("entries") or []:
                if e.get("id") and e.get("duration"):
                    ch = e.get("channel") or e.get("uploader") or ""
                    res.append(SongResult(id=e["id"], url=f"https://www.youtube.com/watch?v={e['id']}",
                                          raw_title=e.get("title") or e["id"], title=_display_title(e.get("title") or e["id"], ch),
                                          channel=ch, duration=float(e["duration"]),
                                          views=int(e.get("view_count") or 0)))
        return res
    with ThreadPoolExecutor(max_workers=2) as pool:
        f_tracks, f_videos = pool.submit(_music_tracks, query), pool.submit(videos)
        tracks, vids = f_tracks.result(), f_videos.result()
    album_len = next((t.duration for t in tracks), None)
    found: dict[str, SongResult] = {}
    for r in tracks + vids:
        if r.id in found:
            continue
        r.score = round(r.score + _score(r, q_tokens), 2)
        if r.kind == "video":
            if re.search(r"official audio|\(audio\)|\[audio\]", r.raw_title, re.I):
                r.note = "official audio"
            elif re.search(r"\bvideo\b|\bmv\b|visualizer", r.raw_title, re.I):
                r.note = "music video: may have extra intro or sounds"
            elif re.search(r"\blive\b", r.raw_title, re.I):
                r.note = "live"
            if album_len and r.duration > album_len + 8:
                r.score -= 2.0
                r.note = f"{round(r.duration - album_len)} s longer than the album track" + (f" · {r.note}" if r.note else "")
        found[r.id] = r
    if not found:
        raise ModelError(f"Couldn't find '{query}' on YouTube. Try adding the artist's name, or paste a link.")
    return sorted(found.values(), key=lambda r: -r.score)[:limit]


def from_link(url: str) -> SongResult:
    if not re.match(r"https?://", url.strip()):
        raise ModelError("That doesn't look like a link. Paste a full address starting with https://")
    with _ydl(skip_download=True, noplaylist=True) as ydl:
        try:
            e = ydl.extract_info(url.strip(), download=False)
        except Exception as ex:
            raise ModelError(f"Couldn't read that link ({_short(ex)}).")
    if e.get("entries"):
        e = e["entries"][0]
    ch = e.get("channel") or e.get("uploader") or ""
    return SongResult(id=re.sub(r"[^\w-]", "_", e.get("id") or "link")[:40], url=e.get("webpage_url") or url,
                      raw_title=e.get("title") or "song", title=_display_title(e.get("title") or "song", ch),
                      channel=ch, duration=float(e.get("duration") or 0))


def _known() -> dict:
    f = path("cache_dir") / "youtube_songs.json"
    try:
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
    except ValueError:
        return {}


def known_song(video_id: str) -> str | None:
    """Song id (uploads/) of a video fetched before, if its file is still there."""
    sid = _known().get(video_id)
    return sid if sid and (path("uploads_dir") / f"{sid}.json").exists() else None


def download(r: SongResult, register, progress=None) -> dict:
    """Fetch the audio and hand it to `register(tmp_path, name, None, extra)` (server.register_song).
    Returns the song metadata. Videos fetched before are reused."""
    sid = known_song(r.id)
    if sid:
        return json.loads((path("uploads_dir") / f"{sid}.json").read_text(encoding="utf-8"))
    if r.duration and r.duration > 20 * 60:
        raise ModelError(f"'{r.title}' is {round(r.duration / 60)} minutes long. Pick a single song (under 20 minutes).")
    work = path("cache_dir") / f"yt_{uuid.uuid4().hex[:8]}"
    work.mkdir(parents=True)
    try:
        opts = {"format": "bestaudio/best", "outtmpl": str(work / "song.%(ext)s"), "noplaylist": True,
                "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "flac"}]}
        if progress:
            def hook(d):
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                if d.get("status") == "downloading" and total:
                    progress(min(1.0, d.get("downloaded_bytes", 0) / total))
            opts["progress_hooks"] = [hook]
        with _ydl(**opts) as ydl:
            from .train.source import _cancelled_in, download_retrying
            try:
                download_retrying(ydl, r.url)
            except Exception as e:
                if _cancelled_in(e) is not None:
                    raise
                raise ModelError(f"Couldn't download '{r.title}' ({_short(e)}).")
        f = next(iter(work.glob("song.flac")), None)
        if not f:
            raise ModelError(f"Downloaded '{r.title}' but couldn't convert it to audio.")
        name = re.sub(r'[<>:"/\\|?*]+', "_", r.title).strip() + ".flac"
        meta = register(f, name, None, {"title": r.title, "source_url": r.url, "youtube_id": r.id})
        k = _known()
        k[r.id] = meta["id"]
        (path("cache_dir") / "youtube_songs.json").write_text(json.dumps(k, indent=1), encoding="utf-8")
        return meta
    finally:
        shutil.rmtree(work, ignore_errors=True)
