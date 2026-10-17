"""Settings: the shipped defaults (undercover/config.default.toml), then the user's own config.toml on top. Import this
before torch so GPU env vars apply.

- **The defaults ship with the code** and are never edited on a PC: a new default (a new key, a better value) reaches
  every install with the update.
- **The user's file** (`config.toml` in the app folder) holds only what differs from the defaults: what setup wrote
  (the GPU, where the files live) and what the user changed (game pause, the vault, the cadence engine, ...). It's
  theirs: gitignored, never shipped, and the only file the app writes settings to (`set_values`).
- **An install from before the split** had a full config.toml: `migrate()` (at server start) keeps a backup and
  rewrites it to only the keys that differ, values exactly as they were.

UNDERCOVER_CONFIG=<file> uses another user file instead (a sandbox test server whose [paths] point at scratch
folders); the defaults still come from the code, and relative paths still resolve against the app folder.

    python -m undercover.core.config get gpu backend          prints the merged value
    python -m undercover.core.config set gpu device_name=RX 7900 XTX backend=rocm   (strings; setup.ps1 uses this)
    python -m undercover.core.config migrate                  the server does this at start
"""
import json
import os
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FILE = Path(__file__).resolve().parents[1] / "config.default.toml"
CONFIG_FILE = Path(os.environ.get("UNDERCOVER_CONFIG") or ROOT / "config.toml")
HEADER = ("# Your UnderCover settings: only what differs from the defaults (undercover/config.default.toml).\n"
          "# UnderCover writes here when you change a setting; a key left out takes the default.\n")
BACKUP = "config.before-defaults.toml"   # the full file as it was, kept beside it by migrate()
CONSTRAINTS = ROOT / "installer" / "constraints.txt"   # the package pins every pip install is held to


def defaults() -> dict:
    return tomllib.loads(DEFAULT_FILE.read_text(encoding="utf-8"))


def overlay(base: dict, mine: dict) -> dict:
    """base with mine on top: tables merge key by key (at any depth), anything else is replaced."""
    out = dict(base)
    for k, v in mine.items():
        out[k] = overlay(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def read(config_file: Path | None = None) -> dict:
    """The settings an install runs with: the defaults, then its user file (if it has one)."""
    f = Path(config_file or CONFIG_FILE)
    mine = tomllib.loads(f.read_text(encoding="utf-8")) if f.is_file() else {}
    return overlay(defaults(), mine)


CONFIG = read()

if CONFIG["gpu"].get("hip_visible_devices"):
    os.environ.setdefault("HIP_VISIBLE_DEVICES", CONFIG["gpu"]["hip_visible_devices"])
if CONFIG["gpu"].get("miopen_find_mode"):
    os.environ.setdefault("MIOPEN_FIND_MODE", CONFIG["gpu"]["miopen_find_mode"])
os.environ.setdefault("MIOPEN_LOG_LEVEL", "3")  # errors only; FAST find mode logs harmless workspace warnings


def set_in_text(text: str, section: str, values: dict) -> str:
    """config text with keys set in one section (values are TOML literals: '"x"', a literal-string path, "true"),
    keeping everything else (comments, order) as it is. A missing key or section is added."""
    head = re.compile(rf"(?ms)^\[{re.escape(section)}\][ \t]*\r?$(.*?)(?=^\[|\Z)")   # (a CRLF file too)
    m = head.search(text)
    if not m:
        text = (text.rstrip("\n") + "\n\n" if text.strip() else text) + f"[{section}]\n"
        m = head.search(text)
    body = m.group(1)
    for key, val in values.items():
        rx = re.compile(rf"(?m)^{re.escape(key)}[ \t]*=.*$")
        line = f"{key} = {val}"
        body = rx.sub(lambda _: line, body, count=1) if rx.search(body) else body.rstrip("\n") + f"\n{line}\n\n"
    return text[:m.start(1)] + body + text[m.end(1):]


def _section_rx(section: str):
    return re.compile(rf"(?ms)^\[{re.escape(section)}\][ \t]*\r?$.*?(?=^\[|\Z)")


def section_text(text: str, section: str) -> str | None:
    """One whole section of config text ("[paths]\\n..."), or None."""
    m = _section_rx(section).search(text)
    return m.group(0) if m else None


def replace_section(text: str, section: str, block: str | None) -> str:
    """config text with one whole section replaced by block ("[paths]\\n..."), added when missing, or removed (None)."""
    rx = _section_rx(section)
    if rx.search(text):
        return rx.sub(lambda _: block or "", text, count=1)
    return text if block is None else (text.rstrip("\n") + "\n\n" if text.strip() else text) + block


def set_values(section: str, values: dict, config_file: Path | None = None):
    """Set keys in one section of the USER's config.toml (made with its header if missing); CONFIG is updated to match.
    The defaults file is never written."""
    from . import fsutil
    cf = Path(config_file or CONFIG_FILE)
    if cf.resolve() == DEFAULT_FILE.resolve():
        raise ValueError("the defaults file is never written: settings go in the user's config.toml")
    text = cf.read_text(encoding="utf-8") if cf.is_file() else HEADER
    new = set_in_text(text, section, values)
    tomllib.loads(new)   # never write a file the app can't read back
    fsutil.write_text(cf, new)
    if Path(cf).resolve() == Path(CONFIG_FILE).resolve():
        # only the keys written here: never re-read the section (a reload once put the real [paths] back into a test
        # that was moving scratch folders, and it deleted the user's songs and covers; 2026-10-11)
        written = tomllib.loads(new)[section]
        CONFIG.setdefault(section, {}).update({k: written[k] for k in values})


# ---------------------------------------------------------------- the user's file: only what differs

def _toml(v) -> str:
    """A TOML literal for a value read from TOML (strings, numbers, booleans, arrays, inline tables, dates)."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)   # a TOML basic string: JSON's escapes are TOML's
    if isinstance(v, list):
        return "[" + ", ".join(_toml(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ", ".join(f"{json.dumps(k)} = {_toml(x)}" for k, x in v.items()) + "}"
    return v.isoformat()   # dates and times


def differing(mine: dict, base: dict) -> dict:
    """The part of mine that differs from base (tables compared key by key; a key base doesn't have always differs)."""
    out = {}
    for k, v in mine.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            d = differing(v, base[k])
            if d:
                out[k] = d
        elif k not in base or base[k] != v:
            out[k] = v
    return out


def _render(diff: dict) -> str:
    text = HEADER
    for sec, vals in diff.items():
        if not isinstance(vals, dict):
            text = text + f"{sec} = {_toml(vals)}\n"   # (a top-level key: none today)
    for sec, vals in diff.items():
        if isinstance(vals, dict):
            text += f"\n[{sec}]\n" + "".join(f"{json.dumps(k) if not re.fullmatch(r'[A-Za-z0-9_-]+', k) else k} = {_toml(v)}\n"
                                             for k, v in vals.items())
    return text


def migrate(config_file: Path | None = None, dry_run: bool = False) -> dict | None:
    """A full config.toml from before the defaults split -> only the keys that differ, values exactly as they were.
    The file as it was is kept beside it (config.before-defaults.toml) before anything is written, and the result is
    checked to give the very same settings. A file that already starts with the header is left alone.
    -> {"before", "after", "backup", "kept": the differing keys} or None when there was nothing to do."""
    from . import fsutil
    cf = Path(config_file or CONFIG_FILE)
    if not cf.is_file():
        return None
    old = cf.read_text(encoding="utf-8")
    if old.startswith(HEADER.splitlines()[0]):
        return None
    base = defaults()
    mine = tomllib.loads(old)
    new = _render(differing(mine, base))
    if overlay(base, tomllib.loads(new)) != overlay(base, mine):   # never: the same settings, or nothing is written
        raise ValueError("the slimmed config wouldn't give the same settings; left as it is")
    out = {"before": old, "after": new, "kept": differing(mine, base), "backup": None}
    if dry_run:
        return out
    bak = cf.with_name(BACKUP)
    n = 2
    while bak.exists():
        bak, n = cf.with_name(f"config.before-defaults-{n}.toml"), n + 1
    fsutil.write_text(bak, old)
    if bak.read_text(encoding="utf-8") != old:
        raise OSError("the backup didn't read back the same; the config was left as it is")
    fsutil.write_text(cf, new)
    out["backup"] = str(bak)
    return out


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


def _cli(argv: list[str]) -> int:
    if argv[:1] == ["get"] and len(argv) == 3:
        v = CONFIG.get(argv[1], {}).get(argv[2])
        print("" if v is None else v)
        return 0
    if argv[:1] == ["set"] and len(argv) >= 3:
        set_values(argv[1], {k: json.dumps(v) for k, v in (a.split("=", 1) for a in argv[2:])})
        return 0
    if argv[:1] == ["migrate"]:
        r = migrate()
        print("nothing to do" if r is None else f"kept {sum(len(v) if isinstance(v, dict) else 1 for v in r['kept'].values())} "
                                                f"setting(s); the old file is {r['backup']}")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
