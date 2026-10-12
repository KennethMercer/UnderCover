"""Load config.toml and resolve paths. Import this before torch so GPU env vars apply.

UNDERCOVER_CONFIG=<file> loads another config file instead (a sandbox test server whose [paths] point at scratch
folders); relative paths still resolve against the app folder.
"""
import os
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIG_FILE = Path(os.environ.get("UNDERCOVER_CONFIG") or ROOT / "config.toml")
CONFIG = tomllib.loads(CONFIG_FILE.read_text(encoding="utf-8"))

if CONFIG["gpu"].get("hip_visible_devices"):
    os.environ.setdefault("HIP_VISIBLE_DEVICES", CONFIG["gpu"]["hip_visible_devices"])
if CONFIG["gpu"].get("miopen_find_mode"):
    os.environ.setdefault("MIOPEN_FIND_MODE", CONFIG["gpu"]["miopen_find_mode"])
os.environ.setdefault("MIOPEN_LOG_LEVEL", "3")  # errors only; FAST find mode logs harmless workspace warnings


def set_values(section: str, values: dict, config_file: Path | None = None):
    """Set keys in one section of config.toml (values are TOML literals: '"x"', a literal-string path, "true"), keeping
    everything else (comments, order) as it is; CONFIG is updated to match. A missing key is added to the section."""
    import re
    from . import fsutil
    cf = Path(config_file or CONFIG_FILE)
    text = cf.read_text(encoding="utf-8")
    head = re.compile(rf"(?ms)^\[{re.escape(section)}\][ \t]*$(.*?)(?=^\[|\Z)")
    m = head.search(text)
    if not m:
        text = text.rstrip("\n") + f"\n\n[{section}]\n"
        m = head.search(text)
    body = m.group(1)
    for key, val in values.items():
        rx = re.compile(rf"(?m)^{re.escape(key)}[ \t]*=.*$")
        line = f"{key} = {val}"
        body = rx.sub(lambda _: line, body, count=1) if rx.search(body) else body.rstrip("\n") + f"\n{line}\n\n"
    new = text[:m.start(1)] + body + text[m.end(1):]
    tomllib.loads(new)   # never write a file the app can't read back
    fsutil.write_text(cf, new)
    if Path(cf).resolve() == Path(CONFIG_FILE).resolve():
        # only the keys written here: never re-read the section (a reload once put the real [paths] back into a test
        # that was moving scratch folders, and it deleted the user's songs and covers; 2026-10-11)
        written = tomllib.loads(new)[section]
        CONFIG.setdefault(section, {}).update({k: written[k] for k in values})


def path(key: str) -> Path:
    """Absolute path for a [paths] entry, created if missing. voices_dir (phase 4b) defaults to <models_dir>/voices and
    themes_dir (phase 14) to <the library's folder>/themes, so config files from before them keep working."""
    v = CONFIG["paths"].get(key)
    if v is None and key == "voices_dir":
        p = path("models_dir") / "voices"
    elif v is None and key == "themes_dir":
        p = path("uploads_dir").parent / "themes"
    else:
        p = Path(CONFIG["paths"][key])
        if not p.is_absolute():
            p = ROOT / p
    p.mkdir(parents=True, exist_ok=True)
    return p
