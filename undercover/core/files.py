"""Which folders the page may read files from, and the /files/ URL for a file inside one of them."""
import os
import time
from pathlib import Path
from urllib.parse import quote

from .config import CONFIG, ROOT, path

AUDIO_EXT = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wma", ".aiff", ".aif", ".webm", ".mp4"}
# the most of any audio file that's ever decoded (ffmpeg -t): a dropped, uploaded or fetched file can hold far more
# than its size or its own header suggests (near-silence compresses thousands of times)
MAX_AUDIO_S = 3 * 3600


def file_url(p: Path | str | None) -> str | None:
    """URL for a file inside an allowed folder, else None."""
    if not p:
        return None
    s = str(Path(p).resolve())
    folded = os.path.normcase(s)
    for root_name, root in _kept_roots()["folded"].items():   # (a string compare per root: Path.relative_to cost a listing seconds)
        if folded.startswith(root):
            return f"/files/{root_name}/{quote(s[len(root):].replace(os.sep, '/'))}"
    return None


_ROOTS: dict = {"key": None, "at": 0.0, "roots": {}, "folded": {}}   # resolved per [paths] setting, again every ROOTS_S; see roots()
ROOTS_S = 5.0   # (hardening) a root folder re-pointed while the server runs (a junction) is followed within this


def roots() -> dict[str, Path]:
    """The folders the page may read from, resolved. Kept until a [paths] value changes (a Storage move, a test's
    own folders) or for ROOTS_S, so a root that is a junction re-pointed by hand is followed: resolving seven folders
    per file_url cost the Library seconds at 2,000 covers (the maintainers' notes, efficiency)."""
    return dict(_kept_roots()["roots"])


def _kept_roots() -> dict:
    global _ROOTS
    kept = _ROOTS
    key = tuple(str(CONFIG["paths"].get(k)) for k in ("output_dir", "uploads_dir", "cache_dir"))
    now = time.monotonic()
    if kept["key"] != key or now - kept["at"] > ROOTS_S:
        cache = path("cache_dir")
        rs = {"output": path("output_dir").resolve(), "uploads": path("uploads_dir").resolve(),
              "converted": (cache / "converted").resolve(), "test": (ROOT / "test").resolve(),
              "previews": (cache / "previews").resolve(), "training": (cache / "training").resolve(),
              "stems": (cache / "stems").resolve()}
        # each root as a case-folded prefix with its separator, so "D:\x\output2" is never inside "D:\x\output";
        # replaced whole, so another thread never pairs one setting's key with another's roots
        kept = _ROOTS = {"key": key, "at": now, "roots": rs,
                         "folded": {n: os.path.normcase(str(r)).rstrip("\\/") + os.sep for n, r in rs.items()}}
    return kept
