"""Keep working after the app folder is moved or renamed (e.g. D:\\UnderCover -> D:\\FrontMan).

Voices, the cover history, training projects and song metadata store absolute paths. At startup the server compares
the folder it runs from with the one recorded in `cache/root.txt`; if they differ it rewrites the old folder to the new
one in those files (crash-safe writes), then records the new folder. Paths elsewhere on disk are left alone.

Not when the recorded folder still exists and holds an UnderCover checkout (phase 9, 2026-10-12): that's a second
checkout (a git worktree, a copy) started on the same data, not a move, and rewriting would point the user's voices,
history and projects at it. Nothing is rewritten and root.txt keeps the original folder.
"""
import json
import re
from pathlib import Path

from .config import ROOT, path


def _variants(root: str) -> list[str]:
    """How a folder appears inside JSON: D:\\\\X (escaped backslashes) and D:/X."""
    return [json.dumps(root)[1:-1], root.replace("\\", "/")]


def _files() -> list[Path]:
    out = [path("output_dir") / "history.jsonl"]
    out += sorted(path("voices_dir").glob("*/meta.json"))
    out += sorted((path("cache_dir") / "training").glob("*/project.json"))
    out += sorted(path("uploads_dir").glob("*.json"))
    out += sorted(path("cache_dir").glob("*.json"))
    out.append(ROOT / ".claude" / "launch.json")
    return [f for f in out if f.exists()]


def rewrite(old: str, new: str, dry_run: bool = False, keep: tuple = ()) -> dict:
    """Replace the folder `old` with `new` wherever it starts a path in the app's JSON files. -> {file: count}.
    keep: folders inside `old` that did NOT move (the voices inside models_dir when only the model files moved)."""
    from . import fsutil
    changed = {}
    old = old.rstrip("\\/")
    rels = [Path(k).relative_to(old).parts for k in keep if Path(k).is_relative_to(old)]
    # not when a kept folder follows: a separator (\\ in JSON, or /), its parts, then a separator or the string's end
    sep = r'(?:\\\\|/)'
    stay = "".join(r"(?!" + sep + sep.join(re.escape(x) for x in r) + r'(?=[\\/"]))' for r in rels)
    pairs = list(zip(_variants(old), _variants(new.rstrip("\\/"))))
    for f in _files():
        text = f.read_text(encoding="utf-8")
        n = 0
        for a, b in pairs:
            # only whole folder names: followed by a separator or the end of the JSON string
            rx = re.compile(re.escape(a) + r'(?=[\\/"])' + stay, re.I)
            text, k = rx.subn(lambda m: b, text)
            n += k
        if n:
            changed[str(f)] = n
            if not dry_run:
                fsutil.write_text(f, text)
    return changed


def _guess_old_root() -> str | None:
    """Before root.txt existed: read it off a voice's stored folder (<root>\\models\\voices\\<name>)."""
    for meta in path("voices_dir").glob("*/meta.json"):
        try:
            d = Path(json.loads(meta.read_text(encoding="utf-8"))["dir"])
        except (OSError, ValueError, KeyError):
            continue
        if len(d.parts) >= 3 and d.parts[-3].lower() == "models" and d.parts[-2].lower() == "voices":
            return str(Path(*d.parts[:-3]))
    return None


def is_checkout(folder: Path) -> bool:
    """An UnderCover app folder (what the installer checks for too)."""
    try:
        return (folder / "undercover" / "server.py").is_file()
    except OSError:
        return False


def check_and_fix() -> dict:
    """Called at startup. Returns what was rewritten (empty when the folder hasn't moved)."""
    from . import fsutil
    mark = path("cache_dir") / "root.txt"
    here = str(ROOT)
    try:
        old = mark.read_text(encoding="utf-8").strip() if mark.exists() else _guess_old_root()
    except OSError:
        old = None
    changed = {}
    if old and Path(old) != Path(here) and is_checkout(Path(old)):
        print(f"Not rewriting stored paths: {old} still holds an UnderCover checkout, so this one ({here}) is a second "
              "checkout on the same data, not a move.", flush=True)
        return changed
    if old and Path(old) != Path(here):
        changed = rewrite(old, here)
        if changed:
            print(f"The app folder moved from {old} to {here}: updated paths in {len(changed)} files.", flush=True)
    if not mark.exists() or mark.read_text(encoding="utf-8").strip() != here:
        fsutil.write_text(mark, here)
    return changed
