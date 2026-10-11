"""Where UnderCover keeps its files, and moving them somewhere else (SafeHouse's Storage room, the installer's folder step).

Three groups, each one folder holding today's sub-folders, so every default is the app folder itself:

    library   your library: songs (uploads), covers + speech (output), voices (+ their cadence packs)
    working   working files: stems, training projects, caches (cache)
    models    model files: everything UnderCover downloads (models)

Voices belong to the library, not the model files: they're the user's own work (hours of training, a person's
likeness), their cadence packs hold real clips, and nothing can download them again; model files all can be. So
[paths] voices_dir (default <models_dir>/voices) moves with the library.

A move is a job: copy, check every file arrived (sizes + count), switch config.toml [paths], fix stored absolute
paths (relocate.rewrite), and only then remove the old copy. Same drive: the folders are renamed instead (instant).
Refused when the target lacks space or while other work runs; while a move runs, new jobs are refused.

Safety (after 2026-10-11, when a smoke check reached the real folders and deleted the user's songs and covers):
- a move whose files are under the app folder only runs when the Storage room asks for it and the user confirmed
  (`from_room=True, confirmed=True`); nothing else (tests, scripts) can move or delete the app's real data;
- test mode: ALLOWED_ROOTS (or UNDERCOVER_TEST_ROOTS=<dir>;<dir>) is an allow-list; every folder a move reads, writes or
  deletes must be inside it, and anything under the app folder is refused outright.
"""
import os
import shutil
import threading
import time
from pathlib import Path

from . import fsutil
from .config import CONFIG, ROOT, path, set_values
from .voices import ModelError

# group -> [(paths key, sub-folder inside the group's folder, default value in config.toml)]
GROUPS = {
    "library": [("uploads_dir", "uploads", "uploads"), ("output_dir", "output", "output"),
                ("voices_dir", "voices", "models/voices")],
    "working": [("cache_dir", "cache", "cache")],
    "models": [("models_dir", "models", "models")],
}
LABELS = {"library": "Your library", "working": "Working files", "models": "Model files"}
ABOUT = {"library": "Songs, covers, speech and voices (with their cadence). Yours: they can't be downloaded again.",
         "working": ("Training projects (the clips collected for each voice and its training saves: hours of work, and "
                     "source videos can vanish) plus song stems and caches, which are re-made when needed."),
         "models": "The AI models UnderCover downloads (separation, voice engine, speech, add-ons). Can be downloaded again."}
SPARE = 1e9            # keep this much free on the target beyond what's copied
MOVING: dict = {}      # while a move runs: {"group", "job"}; jobs.run_job refuses new work
# test mode: the only folders a move may touch (None = normal use). Tests set it; a sandbox server gets it from the env.
ALLOWED_ROOTS: list | None = ([Path(p) for p in os.environ["UNDERCOVER_TEST_ROOTS"].split(os.pathsep) if p]
                              if os.environ.get("UNDERCOVER_TEST_ROOTS") else None)


def _same(a: Path, b: Path) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _inside(a: Path, b: Path) -> bool:
    """a is b or inside b."""
    a, b = os.path.normcase(os.path.abspath(a)), os.path.normcase(os.path.abspath(b))
    return a == b or a.startswith(b.rstrip("\\/") + os.sep)


def refuse(paths, from_room: bool = False, confirmed: bool = False) -> str | None:
    """Why these folders may not be moved or deleted (None = they may). See the module's "Safety" notes."""
    for p in paths:
        p = Path(p)
        if ALLOWED_ROOTS is not None:
            if _inside(p, ROOT):
                return f"Test mode: {p} is in the app folder; tests never touch it."
            if not any(_inside(p, r) for r in ALLOWED_ROOTS):
                return f"Test mode: {p} is outside the folders this test may touch."
        elif _inside(p, ROOT) and not (from_room and confirmed):
            return f"{p} holds UnderCover's own files: only SafeHouse's Storage room can move them, after you confirm."
    return None


def size_of(p: Path, skip: tuple = ()) -> tuple[int, int]:
    """(bytes, files) under p, leaving out the folders in skip."""
    total = n = 0
    if p.is_file():
        return p.stat().st_size, 1
    for dirpath, dirs, files in os.walk(p):
        dirs[:] = [d for d in dirs if not any(_same(Path(dirpath) / d, s) for s in skip)]
        for f in files:
            try:
                total += os.stat(os.path.join(dirpath, f)).st_size
                n += 1
            except OSError:
                pass
    return total, n


def _skips(key: str) -> tuple:
    """Folders inside this key's folder that belong to another key (voices inside models, by default)."""
    me = path(key)
    return tuple(path(k) for g in GROUPS.values() for k, _, _ in g if k != key and _inside(path(k), me) and not _same(path(k), me))


def folder_of(group: str) -> Path:
    """The group's folder: the parent its sub-folders share (the app folder by default). A config edited by hand so
    they don't share one: the first key's parent."""
    parents = []
    for key, sub, default in GROUPS[group]:
        p = path(key)
        parents.append(ROOT if _same(p, ROOT / default) else p.parent)
    return parents[0]


def where() -> dict:
    """The three groups: folder, size, what's in them, the [paths] keys (Deep Cover)."""
    out = {}
    for g, keys in GROUPS.items():
        b = n = 0
        for key, _, _ in keys:
            bb, nn = size_of(path(key), _skips(key))
            b, n = b + bb, n + nn
        out[g] = {"label": LABELS[g], "about": ABOUT[g], "folder": str(folder_of(g)), "size": b, "files": n,
                  "keys": {k: str(path(k)) for k, _, _ in keys}}
    return out


def drives() -> list[dict]:
    """Drives to offer as targets: letter, free and total bytes, whether the app is on it."""
    out = []
    if os.name == "nt":
        import ctypes
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        for i in range(26):
            if not mask & (1 << i):
                continue
            root = f"{chr(65 + i)}:\\"
            kind = ctypes.windll.kernel32.GetDriveTypeW(root)
            if kind not in (2, 3):   # removable, fixed (not network, CD, RAM disk)
                continue
            try:
                du = shutil.disk_usage(root)
            except OSError:
                continue
            out.append({"drive": root[:2], "free": du.free, "total": du.total, "removable": kind == 2,
                        "app": ROOT.anchor.upper().startswith(root[:2].upper())})
    else:
        du = shutil.disk_usage("/")
        out.append({"drive": "/", "free": du.free, "total": du.total, "removable": False, "app": True})
    return out


def targets(group: str, folder: Path) -> list[tuple[str, Path, Path]]:
    """[(key, now, new)] for moving the group into folder. Back into the app folder = the defaults."""
    out = []
    for key, sub, default in GROUPS[group]:
        new = ROOT / default if _same(folder, ROOT) else folder / sub
        out.append((key, path(key), new))
    return out


def _busy() -> str | None:
    from . import jobs, worklist
    for j in jobs.JOBS.values():
        if not j.finished and j.kind != "move":
            return j.title or j.kind
    if worklist.active():
        return "voice work"
    return None


def check(group: str, folder: str) -> dict:
    """Can the group move to folder? -> {ok, why, need, free, same_drive, folder, moves: [{key, now, new}]}"""
    if group not in GROUPS:
        raise ModelError(f"There's no storage group called '{group}'.")
    f = Path(os.path.expandvars(str(folder or "").strip().strip('"'))).expanduser()
    res = {"group": group, "folder": str(f), "ok": False, "why": None}
    if not str(folder or "").strip() or not f.is_absolute():
        res["why"] = "Type a full folder, starting with a drive (like E:\\UnderCover)."
        return res
    moves = targets(group, f)
    res["moves"] = [{"key": k, "now": str(a), "new": str(b)} for k, a, b in moves]
    if all(_same(a, b) for _, a, b in moves):
        res["why"] = "That's where it is now."
        return res
    if MOVING:
        res["why"] = "Files are being moved already. Wait for that to finish."
        return res
    need = 0
    defaults = {k: ROOT / d for g in GROUPS.values() for k, _, d in g}
    for key, now, new in moves:
        if _same(now, new):
            continue
        if _inside(new, now) or _inside(now, new):
            res["why"] = "Pick a folder outside the one it's in now."
            return res
        for other in (k for g, ks in GROUPS.items() if g != group for k, _, _ in ks):
            if _inside(new, path(other)) and not _inside(path(other), now) and not _same(new, defaults[key]):
                res["why"] = "That's inside another of UnderCover's folders. Pick a folder of its own."
                return res
        if new.exists() and any(new.iterdir()):
            res["why"] = f"{new} already has files in it. Pick an empty or new folder."
            return res
        need += size_of(now, _skips(key))[0]
    anchor = Path(f.anchor or "/")
    if not anchor.exists():
        res["why"] = f"There's no drive {f.anchor.rstrip(chr(92))} on this PC."
        return res
    same_drive = all(os.path.splitdrive(str(a))[0].lower() == os.path.splitdrive(str(b))[0].lower() for _, a, b in moves)
    free = shutil.disk_usage(anchor).free
    res.update(need=need, free=free, same_drive=same_drive, minutes=round(need / 120e6 / 60, 1) if not same_drive else 0)
    if not same_drive and free < need + SPARE:
        res["why"] = (f"Needs {need / 1e9:.1f} GB (plus 1 GB to spare), but {f.anchor.rstrip(chr(92))} has only "
                      f"{free / 1e9:.1f} GB free.")
        return res
    busy = _busy()
    if busy:
        res["why"] = f"Wait for {busy} to finish first: nothing else can run while files move."
        return res
    res["ok"] = True
    return res


# ---------------------------------------------------------------- config.toml

def _toml_value(key: str, default: str, new: Path) -> str:
    if _same(new, ROOT / default):
        return f'"{default}"'
    return "'" + str(new) + "'"   # a TOML literal string: backslashes stay as they are


def write_paths(values: dict, config_file: Path = None):
    """Set [paths] keys in config.toml (and CONFIG), keeping everything else as it is."""
    set_values("paths", values, config_file)


# ---------------------------------------------------------------- moving

def copy_tree(src: Path, dst: Path, skip: tuple = (), progress=None, cancel=None) -> tuple[int, int]:
    """Copy src into dst (created), leaving out skip. progress(bytes done). -> (bytes, files) copied."""
    done = n = 0
    for dirpath, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if not any(_same(Path(dirpath) / d, s) for s in skip)]
        out = dst / Path(dirpath).relative_to(src)
        out.mkdir(parents=True, exist_ok=True)
        for f in files:
            if cancel and cancel():
                raise InterruptedError
            a, b = Path(dirpath) / f, out / f
            fsutil.retry(lambda: shutil.copy2(a, b))
            done += b.stat().st_size
            n += 1
            if progress:
                progress(done)
    return done, n


def verify(src: Path, dst: Path, skip: tuple = ()) -> list[str]:
    """Files missing or a different size in dst (empty = every file arrived)."""
    bad = []
    for dirpath, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if not any(_same(Path(dirpath) / d, s) for s in skip)]
        for f in files:
            a = Path(dirpath) / f
            b = dst / a.relative_to(src)
            try:
                if b.stat().st_size != a.stat().st_size:
                    bad.append(str(b))
            except OSError:
                bad.append(str(b))
    return bad


def remove_tree(p: Path, skip: tuple = (), from_room: bool = False, confirmed: bool = False):
    """Delete p, except the folders in skip (and the folders that lead to them). Refused like a move (refuse())."""
    why = refuse([p], from_room, confirmed)
    if why:
        raise ModelError(why)
    if not skip:
        fsutil.retry(lambda: shutil.rmtree(p))
        return
    for child in list(p.iterdir()):
        if any(_same(child, s) for s in skip):
            continue
        if child.is_dir() and any(_inside(s, child) for s in skip):
            remove_tree(child, skip, True, True)   # p itself was checked
        elif child.is_dir():
            fsutil.retry(lambda: shutil.rmtree(child))
        else:
            fsutil.retry(child.unlink)


def remove_files(files, within, dirs=()):
    """The roll-off's delete (core/tray.py): remove these files, and these folders with what's in them, and nothing else.
    Everything is checked before anything is removed, and one bad path refuses the whole call:
    - every path must be inside one of `within` (the output / converted folders of the item's own module), and never
      one of those folders itself;
    - a file must be a plain file, never a folder (a folder only through `dirs`);
    - test mode (ALLOWED_ROOTS): refuse() applies, so the app's real folders and anything outside the test's own are out
      of reach, whatever the history says.
    Normal use is allowed under the app folder on purpose: this is the user's own "keep or roll off" rule, not a move."""
    todo = [(Path(p), False) for p in files if p] + [(Path(p), True) for p in dirs if p]
    todo = [(p, d) for p, d in todo if p.exists()]   # what isn't there needs no removing (and can do no harm)
    for p, is_dir in todo:
        if ALLOWED_ROOTS is not None:
            why = refuse([p])
            if why:
                raise ModelError(why)
        if not any(_inside(p, w) and not _same(p, w) for w in within):
            raise ModelError(f"{p} isn't inside the folders this may delete from; nothing was removed.")
        if p.is_dir() != is_dir:
            raise ModelError(f"{p} isn't the kind of thing expected here; nothing was removed.")
    first = None
    for p, is_dir in todo:   # a locked file doesn't stop the others: remove what can go, then report
        try:
            if is_dir:
                fsutil.retry(lambda p=p: shutil.rmtree(p))
            else:
                fsutil.retry(lambda p=p: p.unlink(missing_ok=True))
        except OSError as e:
            first = first or e
    if first:
        raise first


def _release(group: str):
    """Let go of what's loaded from the folders that move, so it's loaded again from the new place."""
    from . import gpu
    gpu.free()   # cached voices + the cadence worker (it runs from models_dir)
    if group == "models":
        gpu.MODELS._sep = None    # separator and converter keep their model folders: rebuilt on next use
        gpu.MODELS._conv = None
    import gc
    gc.collect()


def move(group: str, folder: str, job, from_room: bool = False, confirmed: bool = False) -> dict:
    """The move job (runs in its own thread). Pushes progress events on job. from_room + confirmed: the Storage room
    asked and the user confirmed (needed for anything under the app folder)."""
    from . import relocate
    plan = check(group, folder)
    if not plan["ok"]:
        raise ModelError(plan["why"])
    moves = [(m["key"], Path(m["now"]), Path(m["new"])) for m in plan["moves"]]
    why = refuse([p for _, now, new in moves for p in (now, new)], from_room, confirmed)
    if why:
        raise ModelError(why)
    skips = {k: _skips(k) for k, _, _ in moves}
    MOVING.update(group=group, job=job.id)
    done_keys, copied = [], []
    try:
        _release(group)
        total, base = max(plan["need"], 1), 0
        if plan["same_drive"]:
            job.push(stage="moving", frac=0.0, message="Moving the folders (same drive: no copying needed)")
            for key, now, new in moves:
                if _same(now, new) or not now.exists():
                    continue
                new.parent.mkdir(parents=True, exist_ok=True)
                if new.exists():
                    new.rmdir()   # empty (checked)
                if skips[key]:   # another group's folder lives inside: move the rest piece by piece
                    new.mkdir(parents=True, exist_ok=True)
                    for child in now.iterdir():
                        if not any(_same(child, s) for s in skips[key]):
                            fsutil.retry(lambda: os.replace(child, new / child.name))
                else:
                    fsutil.retry(lambda: os.replace(now, new))
                done_keys.append(key)
        else:
            last = [0.0]
            for key, now, new in moves:
                if _same(now, new) or not now.exists():
                    continue
                part = size_of(now, skips[key])[0]

                def prog(d, base=base):
                    t = time.time()
                    if t - last[0] > 0.5:
                        last[0] = t
                        job.push(stage="copying", frac=round(min(1.0, (base + d) / total), 3),
                                 message=f"Copying {LABELS[group].lower()}: {(base + d) / 1e9:.1f} of {total / 1e9:.1f} GB")
                copied.append(new)
                copy_tree(now, new, skips[key], prog, lambda: job.cancel)
                base += part
            job.push(stage="checking", frac=1.0, message="Checking every file arrived")
            for key, now, new in moves:
                if _same(now, new) or not now.exists():
                    continue
                bad = verify(now, new, skips[key])
                if bad:
                    raise ModelError(f"{len(bad)} files didn't copy correctly (first: {bad[0]}). Nothing was changed; "
                                     "the copy was removed.")
        # switch the setting (in memory and in config.toml), then fix stored absolute paths
        defaults = {k: d for g in GROUPS.values() for k, _, d in g}
        vals = {k: _toml_value(k, defaults[k], new) for k, _, new in moves}
        if group == "models" and "voices_dir" not in CONFIG["paths"]:   # voices are the library's: they stay put
            vals["voices_dir"] = _toml_value("voices_dir", "models/voices", path("voices_dir"))
        write_paths(vals)
        fixed = {}
        for key, now, new in moves:
            fixed.update(relocate.rewrite(str(now), str(new), keep=tuple(str(s) for s in skips[key])))
        # the old copies go last, once everything points at the new place
        left = []
        if not plan["same_drive"]:
            job.push(stage="cleaning", frac=1.0, message="Removing the old copy")
            for key, now, new in moves:
                try:
                    if now.exists() and not _same(now, new):
                        remove_tree(now, skips[key], from_room, confirmed)
                except OSError:
                    left.append(str(now))
        _release(group)
        msg = f"{LABELS[group]} now lives in {plan['folder']}."
        if left:
            msg += f" The old copy couldn't be removed (a file is in use): delete {left[0]} yourself."
        return {"message": msg, "folder": plan["folder"], "fixed": len(fixed), "left": left}
    except BaseException:
        for c in copied:   # a failed or cancelled copy: the original is untouched, drop the partial copy
            try:
                shutil.rmtree(c, ignore_errors=True)
            except Exception:
                pass
        raise
    finally:
        MOVING.clear()


def start_move(group: str, folder: str, confirmed: bool = False):
    """The Storage room's move: start the job in its own thread (disk work, not the GPU). -> the Job."""
    from . import jobs
    plan = check(group, folder)
    if not plan["ok"]:
        raise ModelError(plan["why"])
    if not confirmed:
        raise ModelError("Confirm the move first.")
    why = refuse([Path(m[k]) for m in plan["moves"] for k in ("now", "new")], True, True)
    if why:
        raise ModelError(why)
    job = jobs.Job("move", title=f"Moving {LABELS[group].lower()} to {plan['folder']}")
    jobs.JOBS[job.id] = job

    def run():
        with job.cond:
            job.state = "running"
        try:
            r = move(group, folder, job, from_room=True, confirmed=True)
            job.finish("done", **r)
        except InterruptedError:
            job.finish("cancelled", message="Cancelled. Nothing was moved; your files are where they were.")
        except ModelError as e:
            job.finish("error", message=str(e))
        except Exception as e:
            job.finish("error", message=jobs.friendly(e, "moving files"))

    threading.Thread(target=run, daemon=True).start()
    return job
