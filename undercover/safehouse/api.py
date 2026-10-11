"""SafeHouse HTTP routes (today's Library tab). The app's server routes /api/library/... here.

GET    /api/library/summary                 storage per category + free space
GET    /api/library/covers | speech | songs | voices | training
POST   /api/library/covers/<id>/rename      {name}   (ids are permanent: 10 hex characters)
POST   /api/library/covers/<id>/keep | unkeep    Send to SafeHouse (keep for good) / take it back
DELETE /api/library/covers/<id>             removes the files and the history line
POST   /api/library/speech/<id>/rename      {name}   (SpeakEasy's saved speech)
POST   /api/library/speech/<id>/keep | unkeep
DELETE /api/library/speech/<id>
(covers and speech lists carry kept, kept_at and recent: in the module's recent tray, the latest 15; the page filters
 Kept / Recent on them. Everything unkept that falls out of the tray is deleted by core/tray.py, never kept ones.)
GET    /api/library/covers/zip?ids=a1b2c3d4e5,...   zip download
DELETE /api/library/songs/<id>[?stems_only=1]
POST   /api/library/voices/<name>/rename    {name}
DELETE /api/library/voices/<name>/cadence   the voice's cadence only (the voice stays)
GET    /api/library/voices/<name>/export    zip download
GET    /api/library/cleanup                 what can be freed, per group
POST   /api/library/cleanup                 {groups: [...]}
(voices and training projects are deleted through the existing /api/voices/<name> and /api/train/projects/<p>)

The Add-ons and Storage rooms (phase 4b):
GET    /api/addons                          every part: included + optional, installed, licence, accepted, size
POST   /api/addons/<id>/install             {accept: true} -> {job}  (the job feed shows it: kind "addon")
POST   /api/addons/<id>/remove              -> its info + freed (bytes)
POST   /api/addons/<id>/use                 {on} an engine add-on: read cadence speech with it ([cadence] engine)
GET    /api/storage                         the three groups (folder, size), drives, a move running?
POST   /api/storage/check                   {group, folder} -> can it move there? (need, free, why)
POST   /api/storage/move                    {group, folder} -> {job}  (kind "move")
"""
import io
import json
import re
import zipfile
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from ..core import gpu, jobs
from ..core.config import path
from ..core.fsutil import retry as _retry
from ..core.voices import ModelError, slug
from ..frontman.songs import remove_song
from . import library
from .library import COVER_WORK, SPEECH_WORK, _cleanup_groups, _guard, _remove, _update_ytdlp, covers


def handle(h, method: str, p: str) -> bool:
    if not p.startswith(("/api/library/", "/api/addons", "/api/storage")):
        return False
    try:
        if p.startswith("/api/addons"):
            _addons_route(h, method, p)
        elif p.startswith("/api/storage"):
            _storage_route(h, method, p)
        else:
            _route(h, method, p)
    except ModelError as e:
        h._error(str(e))
    except LookupError as e:
        h._error(str(e), 404)
    return True


def _route(h, method, p):
    q = parse_qs(urlparse(h.path).query)
    rest = p[len("/api/library/"):]
    if method == "GET" and rest in ("summary", "covers", "speech", "songs", "voices", "training"):
        return h._json(getattr(library, rest)())
    if rest == "tools/ytdlp-update" and method == "POST":
        return h._json(_update_ytdlp())
    if rest == "cleanup":
        if method == "GET":
            return h._json({k: {kk: vv for kk, vv in v.items() if kk != "paths"} for k, v in _cleanup_groups().items()})
        _guard()
        want = set((h._body().get("groups") or []))
        freed = 0
        for k, v in _cleanup_groups().items():
            if k in want:
                freed += v["size"]
                for path_ in v["paths"]:
                    _remove(path_)
        return h._json({"freed": freed})
    if rest == "covers/zip" and method == "GET":
        ids = {x for x in (q.get("ids") or [""])[0].split(",") if re.fullmatch(r"[0-9a-f]{10}", x)}
        files = [(path("output_dir") / c["file"], c["file"]) for c in covers() if c["id"] in ids]
        if not files:
            raise LookupError("Nothing selected.")
        return _zip_response(h, f"UnderCover covers ({len(files)}).zip", files)
    m = re.fullmatch(r"covers/([0-9a-f]{10})(?:/(rename|keep|unkeep))?", rest)
    if m:
        from ..frontman import history as cover_history
        from ..frontman.pipeline import history
        item = next((x for x in history() if x["id"] == m.group(1)), None)
        if not item:
            raise LookupError("That cover isn't there any more.")
        if method == "DELETE" and not m.group(2):
            _guard(COVER_WORK)
            for f in (item["output"], item.get("converted_vocal"), item.get("converted_backing")):
                if f and f != "None":
                    _retry(lambda f=f: Path(f).unlink(missing_ok=True))
            cover_history.remove_line(item["id"])   # the line goes too: no hidden entries, ids are never reused
            return h._json({"ok": True})
        if method == "POST" and m.group(2) in ("keep", "unkeep"):
            h._body()   # read what the page sent, or it's taken for the start of its next request on the same connection
            cover_history.set_kept(item["id"], m.group(2) == "keep")
            return h._json(next(c for c in covers() if c["id"] == item["id"]))
        if method == "POST" and m.group(2) == "rename":
            _guard(COVER_WORK)
            name = re.sub(r'[<>:"/\\|?*]+', "_", (h._body().get("name") or "")).strip().strip(".")
            if not name:
                raise ModelError("Type a name.")
            old = Path(item["output"])
            new = old.with_name(name + old.suffix)
            if new.exists() and new != old:
                raise ModelError(f"There's already a file called \"{new.name}\".")
            _retry(lambda: old.rename(new))
            fields = {"output": str(new)}
            if not item.get("title_set"):
                fields.update(title=name, title_set=True)
            cover_history.update(item["id"], **fields)
            return h._json({"ok": True, "file": new.name})
    m = re.fullmatch(r"speech/([0-9a-f]{10})(?:/(rename|keep|unkeep))?", rest)
    if m:
        from ..speakeasy import speak
        try:
            if method == "POST" and m.group(2) in ("keep", "unkeep"):
                h._body()
                return h._json(speak.set_kept(m.group(1), m.group(2) == "keep"))
            if method == "DELETE" and not m.group(2):
                _guard(SPEECH_WORK)
                speak.delete_speech(m.group(1))
                return h._json({"ok": True})
            if method == "POST" and m.group(2) == "rename":
                _guard(SPEECH_WORK)
                return h._json(speak.rename_speech(m.group(1), h._body().get("name") or ""))
        except speak.SpeakError as e:
            raise ModelError(str(e))
    m = re.fullmatch(r"songs/([0-9a-f]{16})", rest)
    if m and method == "DELETE":
        _guard(COVER_WORK)
        remove_song(m.group(1), stems_only=(q.get("stems_only") or ["0"])[0] == "1")
        return h._json({"ok": True})
    m = re.fullmatch(r"voices/([a-z0-9-]+)/cadence", rest)
    if m and method == "DELETE":
        from ..doubleagent.cadence import remove_pack
        _guard(SPEECH_WORK + ("night",))   # speech may be reading with it; a queue job may be making one
        remove_pack(m.group(1))
        return h._json({"ok": True})
    m = re.fullmatch(r"voices/([a-z0-9-]+)/(rename|export)", rest)
    if m:
        from ..core.voices import get_voice, voices_dir
        v = get_voice(m.group(1))
        if m.group(2) == "export" and method == "GET":
            from ..core.engine.rvc.index import export_faiss
            tmp = path("cache_dir") / f"_export_{v.name}.index"
            try:   # stored compact (fp16): other RVC tools need the standard faiss layout
                idx = export_faiss(v.index, tmp) if v.index else None
                files = [(v.pth, f"{v.name}.pth")] + ([(idx, f"{v.name}.index")] if idx else [])
                return _zip_response(h, f"{v.name}.zip", files)
            finally:
                tmp.unlink(missing_ok=True)
        if m.group(2) == "rename" and method == "POST":
            _guard()
            new = slug(h._body().get("name") or "")
            if not new:
                raise ModelError("Use letters or numbers for the name.")
            target = voices_dir() / new
            if target.exists():
                raise ModelError(f"A voice called '{new}' already exists.")
            _retry(lambda: v.dir.rename(target))
            meta = json.loads((target / "meta.json").read_text(encoding="utf-8"))
            meta.update(name=new, dir=str(target), pth=str(target / "model.pth"),
                        index=str(target / "model.index") if meta.get("index") else None)
            from ..core import fsutil
            fsutil.write_text(target / "meta.json", json.dumps(meta, indent=2))
            gpu.forget_voice(v.name)
            return h._json({"ok": True, "name": new})
    raise LookupError("Not found")


def _engine_of(addon_id: str) -> str | None:
    """The cadence engine an add-on provides (F5-TTS -> "f5"), if it is one."""
    from ..core.cadence.engines import ENGINES
    return next((k for k, e in ENGINES.items() if e.get("addon") == addon_id), None)


def _addon_row(a: dict) -> dict:
    from ..core.config import CONFIG
    eng = _engine_of(a["id"])
    if eng:
        a["engine"] = eng
        a["in_use"] = (CONFIG.get("cadence") or {}).get("engine") == eng and a["installed"] and bool(a["accepted"])
    return a


def _set_engine(name: str):
    from ..core.config import set_values
    set_values("cadence", {"engine": f'"{name}"'})


def _addons_route(h, method, p):
    from ..core import addons
    if p == "/api/addons" and method == "GET":
        return h._json({"addons": [_addon_row(a) for a in addons.status()]})
    m = re.fullmatch(r"/api/addons/([a-z0-9-]+)/(install|remove|use)", p)
    if not m or method != "POST":
        raise LookupError("Not found")
    aid, act = m.groups()
    a = addons.get(aid)
    body = h._body()
    if act == "install":
        job = addons.start_install(aid, bool(body.get("accept")))
        return h._json({"job": job.id})
    if act == "remove":
        _guard(SPEECH_WORK + ("night", "train-transcribe"))   # cadence speech or training may be using it
        if any(j.kind == "addon" and not j.finished for j in jobs.JOBS.values()):
            raise ModelError("An add-on is being installed. Wait for it to finish.")
        gpu.free()   # the cadence worker may hold its files open
        eng = _engine_of(aid)
        r = addons.remove(aid)
        from ..core.config import CONFIG
        if eng and (CONFIG.get("cadence") or {}).get("engine") == eng:
            _set_engine("chatterbox")   # back to the open default
        return h._json(_addon_row(r))
    eng = _engine_of(aid)
    if not eng:
        raise ModelError(f"{a.name} isn't a speech engine; there's nothing to switch.")
    on = bool(body.get("on"))
    if on and not addons.available(aid):
        raise ModelError(f"Install {a.name} (and accept its licence) first.")
    _guard(SPEECH_WORK)
    _set_engine(eng if on else "chatterbox")
    gpu.free()   # the worker reloads with the new engine on next use
    return h._json(_addon_row(addons.info(a)))


def _storage_route(h, method, p):
    from ..core import storage
    if p == "/api/storage" and method == "GET":
        return h._json({"groups": storage.where(), "drives": storage.drives(), "app": str(storage.ROOT),
                        "moving": dict(storage.MOVING) or None})
    body = h._body() if method == "POST" else {}
    if p == "/api/storage/check" and method == "POST":
        return h._json(storage.check(body.get("group"), body.get("folder")))
    if p == "/api/storage/move" and method == "POST":
        job = storage.start_move(body.get("group"), body.get("folder"), confirmed=body.get("confirm") is True)
        return h._json({"job": job.id})
    raise LookupError("Not found")


def _zip_response(h, name: str, files: list[tuple[Path, str]]):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:   # audio/models don't compress
        for p, arc in files:
            if p.exists():
                z.write(p, arc)
    data = buf.getvalue()
    h.send_response(200)
    h.send_header("Content-Type", "application/zip")
    h.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(name)}")
    h.send_header("Content-Length", str(len(data)))
    h.end_headers()
    h.wfile.write(data)
