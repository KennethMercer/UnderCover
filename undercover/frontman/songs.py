"""Find a song on YouTube and fetch its audio for a cover (yt-dlp).

Music videos often differ from the record (intros, skits, sound effects, extended endings), so the album track
is preferred: YouTube Music's song search comes first (exact album audio, with artist and album metadata; ~4 s),
then plain YouTube as a fallback, where music videos and anything noticeably longer than the album track are
ranked down and labelled. Live versions, covers, karaoke, remixes and sped-up edits are pushed down unless asked
for. Audio is saved as FLAC (a lossless copy of what YouTube serves).

Also the songs in uploads/ (dropped files and YouTube fetches alike): register_song, song_meta.
"""
import hashlib
import json
import math
import re
import shutil
import subprocess
import uuid
from pathlib import Path

import soundfile as sf

from ..core.config import path
from ..core.files import MAX_AUDIO_S
from ..core.fsutil import retry
from ..core.voices import ModelError
from ..core.youtube import SongResult, _music_tracks, _short, _ydl

MAX_SONG_S = 20 * 60   # a single song: a longer link is refused before the download, and after the decode
STOP = {"the", "in", "of", "by", "an", "and", "to", "on", "for", "my", "me", "you", "it", "is", "at", "with", "from"}
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


def search(query: str, limit: int = 8, style: str | None = None, words: list[str] | None = None) -> list[SongResult]:
    """Best matches for a song; the first one is what a one-shot cover uses. With a style ("acoustic"), the
    search asks for that version and only titles mentioning one of `words` rank well (the original studio
    recording drops to the bottom)."""
    if style:
        words = [w.lower() for w in (words or [style])]
        res = search(f"{query} {style} cover", limit=limit * 2)
        # it must be the same song: "Muse - Propaganda (Acoustic)" matched "acoustic" and the artist and got picked
        # for "acoustic Plug In Baby". Most of the request's meaningful words must be in the title.
        key = [w for w in re.findall(r"\w+", query.lower()) if len(w) > 1 and w not in STOP]
        for r in res:
            t = f"{r.raw_title} {r.title} {r.album}".lower()
            r.score = round(r.score + (5.0 if any(w in t for w in words) else -6.0), 2)
            have = sum(w in f"{t} {r.channel.lower()}" for w in key)
            if key and have < max(1, round(len(key) * 2 / 3)):
                r.score -= 8.0
                r.note = (r.note + " · " if r.note else "") + "probably another song"
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
    from ..core import safeurl
    safeurl.preflight(url.strip())   # (1.1 hardening: only the internet; yt-dlp starts from a checked address)
    with _ydl(skip_download=True, noplaylist=True) as ydl:
        try:
            e = ydl.extract_info(url.strip(), download=False)
        except safeurl.LinkRefused:
            raise
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
    if r.duration and r.duration > MAX_SONG_S:
        raise ModelError(f"'{r.title}' is {round(r.duration / 60)} minutes long. Pick a single song (under 20 minutes).")
    work = path("cache_dir") / f"yt_{uuid.uuid4().hex[:8]}"
    work.mkdir(parents=True)
    try:
        # the decode is bounded by time too (a link with no length can be small to download and huge as audio): a
        # second past the song rule, so audio that reaches it is known to run longer
        opts = {"format": "bestaudio/best", "outtmpl": str(work / "song.%(ext)s"), "noplaylist": True,
                "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "flac"}],
                "postprocessor_args": {"extractaudio": ["-t", str(MAX_SONG_S + 1)]}}
        if progress:
            def hook(d):
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                if d.get("status") == "downloading" and total:
                    progress(min(1.0, d.get("downloaded_bytes", 0) / total))
            opts["progress_hooks"] = [hook]
        with _ydl(**opts) as ydl:
            from ..core.youtube import _cancelled_in, check_blocked, download_retrying
            try:
                download_retrying(ydl, r.url)
            except Exception as e:
                if _cancelled_in(e) is not None:
                    raise
                check_blocked(e)
                raise ModelError(f"Couldn't download '{r.title}' ({_short(e)}). UnderCover updates its YouTube "
                                 "downloader by itself when YouTube changes; try again in a while.")
        got = next(iter(work.glob("song.flac")), None)
        if not got:
            raise ModelError(f"Downloaded '{r.title}' but couldn't convert it to audio.")
        # decoded once more by ffmpeg itself, to the bound: a file that came as FLAC skips yt-dlp's conversion (and its
        # -t), and its own header may say less than it holds; the length is judged from what ffmpeg wrote
        f = work / "bounded.flac"
        _bounded(got, f, MAX_SONG_S + 1)
        if sf.info(str(f)).duration > MAX_SONG_S + 0.5:   # it ran to the bound: longer than a song, whatever it said
            raise ModelError(f"'{r.title}' runs longer than 20 minutes. Pick a single song (under 20 minutes).")
        name = re.sub(r'[<>:"/\\|?*]+', "_", r.title).strip() + ".flac"
        meta = register(f, name, None, {"title": r.title, "source_url": r.url, "youtube_id": r.id})
        k = _known()
        k[r.id] = meta["id"]
        (path("cache_dir") / "youtube_songs.json").write_text(json.dumps(k, indent=1), encoding="utf-8")
        return meta
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _bounded(src: Path, dst: Path, seconds: float) -> None:
    """src decoded by ffmpeg into a FLAC of at most `seconds` (its header then says what's really in it)."""
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-t", str(seconds), "-vn", "-c:a", "flac", str(dst)],
                       capture_output=True, text=True)
    if r.returncode or not dst.exists():
        raise ModelError("Downloaded the song but couldn't read it as audio.")


# ---------------------------------------------------------------- songs in uploads/

def _ffprobe(p: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                          "format=duration:stream=sample_rate,channels", "-of", "json", str(p)],
                         capture_output=True, text=True)
    if out.returncode != 0:
        raise ValueError("not audio")
    d = json.loads(out.stdout)
    st = (d.get("streams") or [{}])[0]
    if not st:
        raise ValueError("no audio stream")
    return {"duration": float(d["format"]["duration"]), "sample_rate": int(st.get("sample_rate", 0)),
            "channels": int(st.get("channels", 0))}


def _peaks(p: Path, bins: int = 1200) -> list[float]:
    """Waveform overview: peak level per bin, decoded via ffmpeg (any format)."""
    import numpy as np
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(p), "-t", str(MAX_AUDIO_S), "-ac", "1", "-ar", "4000", "-f", "f32le", "-"],
                         capture_output=True).stdout
    a = np.abs(np.frombuffer(raw, dtype=np.float32))
    if not len(a):
        return []
    a = a[: len(a) // bins * bins].reshape(bins, -1).max(axis=1) if len(a) >= bins else a
    top = float(a.max()) or 1.0
    return [round(float(x) / top, 3) for x in a]


def register_song(tmp: Path, name: str, sha256_hex: str | None = None, extra: dict | None = None) -> dict:
    """Move an audio file into uploads/ (named by content hash, like the stem cache) and write its metadata.
    Shared by dropped files and songs fetched from YouTube. Raises ValueError if it isn't readable audio."""
    from ..core.files import file_url
    from .remix import lufs
    up = path("uploads_dir")
    if sha256_hex is None:
        h = hashlib.sha256()
        with open(tmp, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        sha256_hex = h.hexdigest()
    song_id = sha256_hex[:16]
    dest = up / f"{song_id}{Path(name).suffix.lower() or Path(tmp).suffix.lower()}"
    if dest.exists():
        Path(tmp).unlink(missing_ok=True)
    else:
        Path(tmp).replace(dest)
    try:
        info = _ffprobe(dest)
    except (ValueError, KeyError, json.JSONDecodeError):
        dest.unlink(missing_ok=True)
        raise ValueError(f"'{name}' couldn't be read as audio. Try converting it to mp3, wav or flac.")
    meta = {"id": song_id, "name": name, "title": Path(name).stem, "file": dest.name, **info,
            "lufs": round(lufs(dest), 1), "peaks": _peaks(dest), "url": file_url(dest), **(extra or {})}
    (up / f"{song_id}.json").write_text(json.dumps(meta), encoding="utf-8")
    return meta


def song_meta(song_id: str) -> dict:
    meta = path("uploads_dir") / f"{song_id}.json"
    if not re.fullmatch(r"[0-9a-f]{16}", song_id) or not meta.exists():
        raise LookupError("That song isn't loaded anymore. Drop it in again.")
    return json.loads(meta.read_text(encoding="utf-8"))


def remove_song(sid: str, stems_only: bool = False):
    """Delete a song's stems (and unless stems_only, the song file, its key and its YouTube link)."""
    retry(lambda: shutil.rmtree(path("cache_dir") / "stems" / sid) if (path("cache_dir") / "stems" / sid).exists() else None)
    if stems_only:
        return
    for f in path("uploads_dir").glob(f"{sid}.*"):
        retry(lambda f=f: f.unlink(missing_ok=True))
    (path("cache_dir") / "keys" / f"{sid}.json").unlink(missing_ok=True)
    yt = path("cache_dir") / "youtube_songs.json"
    if yt.exists():
        try:
            d = {k: v for k, v in json.loads(yt.read_text(encoding="utf-8")).items() if v != sid}
            yt.write_text(json.dumps(d, indent=1), encoding="utf-8")
        except ValueError:
            pass
