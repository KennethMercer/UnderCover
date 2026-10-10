"""Load config.toml and resolve paths. Import this before torch so GPU env vars apply."""
import os
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = tomllib.loads((ROOT / "config.toml").read_text(encoding="utf-8"))

if CONFIG["gpu"].get("hip_visible_devices"):
    os.environ.setdefault("HIP_VISIBLE_DEVICES", CONFIG["gpu"]["hip_visible_devices"])
if CONFIG["gpu"].get("miopen_find_mode"):
    os.environ.setdefault("MIOPEN_FIND_MODE", CONFIG["gpu"]["miopen_find_mode"])
os.environ.setdefault("MIOPEN_LOG_LEVEL", "3")  # errors only; FAST find mode logs harmless workspace warnings


def path(key: str) -> Path:
    """Absolute path for a [paths] entry, created if missing."""
    p = Path(CONFIG["paths"][key])
    if not p.is_absolute():
        p = ROOT / p
    p.mkdir(parents=True, exist_ok=True)
    return p
