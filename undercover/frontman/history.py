"""FrontMan's cover history: output/history.jsonl, one JSON object per finished cover (the data contract is in
ARCHITECTURE.md, "History items").

Every item has a permanent `id` (10 hex characters, like speech) written when the cover is recorded. Before phase 6 the id
was the line number, which shifted after a delete, and a new cover with the same output name revived a deleted line:
`migrate()` gives the old lines ids once (a backup copy is kept next to the file). Deleting an item removes its line.

`kept` / `kept_at` mark a cover sent to SafeHouse. `record()` is the one place a finished cover is added, and where the
recent tray rolls one cover off (core/tray.py explains the rule and its safety).

All reads and writes of the file go through LOCK; writes are crash-safe (core/fsutil).
"""
import json
import re
import shutil
import threading
import time
import uuid
from pathlib import Path

from ..core import fsutil, tray
from ..core.config import path

LOCK = threading.RLock()
ID = re.compile(r"[0-9a-f]{10}")
BACKUP = "history.before-ids.jsonl"
_checked: set = set()   # history files already looked at for lines without an id (this run)


def _file() -> Path:
    return path("output_dir") / "history.jsonl"


def new_id(taken=()) -> str:
    while True:
        i = uuid.uuid4().hex[:10]
        if i not in taken:
            return i


def _lines() -> list:
    """Every parsable line as a dict, in file order (a half-written last line after a power cut is skipped)."""
    f = _file()
    if not f.exists():
        return []
    out = []
    for line in f.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict):
                out.append(item)
    return out


def _write(items: list):
    fsutil.write_text(_file(), "".join(json.dumps(i) + "\n" for i in items))


def resolved_output(item: dict) -> Path | None:
    """The cover's file if it's there: where history says, or (stored before the app folder moved) in the output folder
    by name. None when it's gone."""
    o = Path(item.get("output") or "")
    if o.exists():
        return o
    by_name = path("output_dir") / o.name
    return by_name if o.name and by_name.exists() else None


def migrate() -> dict:
    """Give every line without an id one, once (a copy of the old file is kept as history.before-ids.jsonl). Lines whose
    cover file is gone are dropped then (they would come back to life when a new cover took the same name); the copy
    keeps them. When the output folder isn't there at all (a drive that isn't plugged in) nothing is dropped."""
    with LOCK:
        f = _file()
        _checked.add(str(f))
        items = _lines()
        if not items or all(isinstance(i.get("id"), str) and ID.fullmatch(i["id"]) for i in items):
            return {"migrated": 0, "dropped": 0}
        backup = f.with_name(BACKUP)
        if not backup.exists():
            shutil.copy2(f, backup)
        folder_there = f.parent.is_dir()
        taken = {i["id"] for i in items if isinstance(i.get("id"), str) and ID.fullmatch(i["id"])}
        out, dropped, n = [], 0, 0
        for i in items:
            if not (isinstance(i.get("id"), str) and ID.fullmatch(i["id"])):
                if folder_there and resolved_output(i) is None:
                    dropped += 1
                    continue
                i["id"] = new_id(taken)
                taken.add(i["id"])
                n += 1
            out.append(i)
        _write(out)
        tray.log(f"history: gave {n} covers a permanent id" + (f", dropped {dropped} lines whose file was gone" if dropped else "")
                 + f" (a copy of the old file: {backup.name})")
        return {"migrated": n, "dropped": dropped}


def _load() -> list:
    if str(_file()) not in _checked:
        migrate()
    return _lines()


def finished() -> list:
    """Every finished cover whose file is there, oldest first (history order), with its output path made current."""
    with LOCK:
        out = []
        for i in _load():
            o = resolved_output(i)
            if o is not None:
                out.append({**i, "output": str(o)})
        return out


def tray_ids() -> set:
    return {i["id"] for i in tray.in_tray(finished())}


def get(item_id: str) -> dict:
    for i in finished():
        if i["id"] == item_id:
            return i
    raise LookupError("That cover isn't there any more.")


def update(item_id: str, **fields) -> dict:
    """Change fields of one stored line (a field set to None is removed)."""
    with LOCK:
        items = _load()
        for i in items:
            if i.get("id") == item_id:
                for k, v in fields.items():
                    if v is None:
                        i.pop(k, None)
                    else:
                        i[k] = v
                _write(items)
                return dict(i)
    raise LookupError("That cover isn't there any more.")


def set_kept(item_id: str, kept: bool) -> dict:
    return update(item_id, kept=True if kept else None, kept_at=time.strftime("%Y-%m-%d %H:%M:%S") if kept else None)


def remove_line(item_id: str):
    """Remove one line (the cover's files are the caller's business)."""
    with LOCK:
        items = _load()
        left = [i for i in items if i.get("id") != item_id]
        if len(left) != len(items):
            _write(left)


def files_of(item: dict) -> list:
    return [Path(f) for f in (item.get("output"), item.get("converted_vocal"), item.get("converted_backing"))
            if f and str(f) != "None"]


def record(rec: dict) -> dict:
    """Add a finished cover, then roll the recent tray (never raises: a failed roll-off must not fail the cover).
    Returns the stored item (with its new id)."""
    with LOCK:
        items = _load()
        rec = {**rec, "id": new_id({i.get("id") for i in items})}
        with open(_file(), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
        try:
            roll_off(rec["id"])
        except Exception as e:   # noqa: BLE001
            tray.log(f"roll-off (covers): failed, nothing removed: {type(e).__name__}: {e}")
        return rec


def roll_off(new_id_: str) -> str | None:
    """After the cover `new_id_` finished: the tray's one leaver, if any, is deleted when it isn't kept. Returns its id
    when something was deleted."""
    from ..core import storage
    with LOCK:
        items = finished()
        lv = tray.leaver(items)
        if lv is None:
            return None
        assert lv.get("id") != new_id_ and not lv.get("kept"), "roll-off picked the new or a kept cover"
        within = [path("output_dir"), path("cache_dir") / "converted"]
        files = files_of(lv)
        storage.remove_files(files, within)   # refuses everything (and removes nothing) if one path is out of bounds
        remove_line(lv["id"])
        tray.log(f"roll-off (covers): removed {lv['id']} \"{lv.get('title') or Path(lv['output']).stem}\" "
                 f"({', '.join(p.name for p in files)}); the tray holds the latest {tray.TRAY_SIZE}")
        return lv["id"]
