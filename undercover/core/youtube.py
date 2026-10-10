"""yt-dlp helpers shared by everything that fetches from YouTube: songs for covers, clips for voices.

YouTube's bot check (YouTubeBlocked) clears by itself after a while; downloads retry transient errors (403s).
"""
import re
from dataclasses import asdict, dataclass

from .voices import ModelError


def open_ydl(missing: str, **opts):
    """A quiet yt_dlp.YoutubeDL. `missing`: what to tell the user when yt-dlp isn't installed."""
    try:
        import yt_dlp
    except ImportError:
        raise ModelError(missing)
    base = {"quiet": True, "no_warnings": True, "noprogress": True, "logger": _QuietLogger()}
    return yt_dlp.YoutubeDL({**base, **opts})


SONGS_MISSING = "yt-dlp isn't installed in UnderCover's environment, so songs can't be fetched from YouTube."


def _ydl(**opts):
    return open_ydl(SONGS_MISSING, **opts)


class _QuietLogger:
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


def _short(e: Exception) -> str:
    msg = re.sub(r"\x1b\[[0-9;]*m", "", str(e)).replace("ERROR: ", "")
    return msg.split("\n")[0][:160]


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


def _music_tracks(query: str, n: int = 6) -> list[SongResult]:
    """YouTube Music song search: album tracks with artist/album metadata (details fetched in parallel)."""
    from concurrent.futures import ThreadPoolExecutor
    url = "https://music.youtube.com/search?q=" + "+".join(query.split()) + "#songs"
    try:
        with _ydl(extract_flat="in_playlist", skip_download=True, playlistend=n + 1) as ydl:
            ids = [e["id"] for e in (ydl.extract_info(url, download=False).get("entries") or [])[:n] if e.get("id")]
    except Exception:
        return []

    blocked = []

    def detail(vid):
        try:
            with _ydl(skip_download=True) as ydl:
                return ydl.extract_info(f"https://www.youtube.com/watch?v={vid}", download=False, process=False)
        except Exception as e:
            if "not a bot" in str(e) or "Sign in to confirm" in str(e):
                blocked.append(vid)
            return None
    with ThreadPoolExecutor(max_workers=4) as pool:
        infos = list(pool.map(detail, ids))
    if ids and len(blocked) == len(ids):
        # every lookup hit YouTube's bot check: say so, rather than "this artist has no music"
        raise YouTubeBlocked(BLOCKED_MSG)
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
