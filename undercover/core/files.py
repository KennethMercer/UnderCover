"""Which folders the page may read files from, and the /files/ URL for a file inside one of them."""
from pathlib import Path
from urllib.parse import quote

from .config import ROOT, path

AUDIO_EXT = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wma", ".aiff", ".aif", ".webm", ".mp4"}
# the most of any audio file that's ever decoded (ffmpeg -t): a dropped, uploaded or fetched file can hold far more
# than its size or its own header suggests (near-silence compresses thousands of times)
MAX_AUDIO_S = 3 * 3600


def file_url(p: Path | str | None) -> str | None:
    """URL for a file inside an allowed folder, else None."""
    if not p:
        return None
    p = Path(p).resolve()
    for root_name, root in roots().items():
        try:
            rel = p.relative_to(root)
        except ValueError:
            continue
        return f"/files/{root_name}/{quote(rel.as_posix())}"
    return None


def roots() -> dict[str, Path]:
    return {"output": path("output_dir").resolve(), "uploads": path("uploads_dir").resolve(),
            "converted": (path("cache_dir") / "converted").resolve(), "test": (ROOT / "test").resolve(),
            "previews": (path("cache_dir") / "previews").resolve(),
            "training": (path("cache_dir") / "training").resolve(),
            "stems": (path("cache_dir") / "stems").resolve()}
