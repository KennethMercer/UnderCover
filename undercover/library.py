"""Library tab API: everything UnderCover keeps on disk, managed without Explorer.

server.py routes /api/library/... here and passes itself as `S` (it usually runs as __main__).

GET    /api/library/summary                 storage per category + free space
GET    /api/library/covers | songs | voices | training
POST   /api/library/covers/<id>/rename      {name}
DELETE /api/library/covers/<id>
GET    /api/library/covers/zip?ids=1,2      zip download
DELETE /api/library/songs/<id>[?stems_only=1]
POST   /api/library/voices/<name>/rename    {name}
GET    /api/library/voices/<name>/export    zip download
GET    /api/library/cleanup                 what can be freed, per group
POST   /api/library/cleanup                 {groups: [...]}
(voices and training projects are deleted through the existing /api/voices/<name> and /api/train/projects/<p>)
"""
import io
import json
import re
import shutil
import time
import zipfile
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from .config import CONFIG, ROOT, path
from .models import ModelError, slug

DAY = 86400
TEMP_PREFIXES = ("dl_", "yt_", "mix_", "tup_", "vup_", "inst_")


# ---------------------------------------------------------------- helpers

def _size(p: Path) -> int:
    try:
        if p.is_file():
            return p.stat().st_size
        return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
    except OSError:
        return 0


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def _retry(fn, tries: int = 30):
    """Windows refuses to rename/delete a file another handle has open (e.g. the page's player stream, which the
    server is still finishing); retry for up to ~3 s."""
    for i in range(tries):
        try:
            return fn()
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(0.1)


def _busy(S) -> str | None:
    """A job that may be using these files right now."""
    for j in S.JOBS.values():
        if not j.finished and j.kind in ("cover", "auto", "train", "train-auto", "train-prepare", "train-export", "install"):
            return j.kind
    return None


def _guard(S):
    kind = _busy(S)
    if kind:
        raise ModelError("UnderCover is busy (a cover, training or install is running). Try again when it's done.")
    for j in S.JOBS.values():   # a background separation would just be redone; stop it so its files aren't in use
        if j.kind == "presep" and not j.finished:
            j.cancel = True


def _history_lines() -> list[str]:
    f = path("output_dir") / "history.jsonl"
    return f.read_text(encoding="utf-8").splitlines() if f.exists() else []


def _write_history_line(n: int, item: dict):
    f = path("output_dir") / "history.jsonl"
    lines = _history_lines()
    lines[n] = json.dumps({k: v for k, v in item.items() if k != "id"})
    tmp = f.with_suffix(".jsonl.tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(f)


def _song_metas() -> dict:
    out = {}
    for f in path("uploads_dir").glob("*.json"):
        try:
            m = json.loads(f.read_text(encoding="utf-8"))
            out[m["id"]] = m
        except (ValueError, KeyError, OSError):
            continue
    return out


def _quality() -> dict:
    f = path("cache_dir") / "voice_quality.json"
    try:
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
    except ValueError:
        return {}


def _duplicate_voices(hist: list) -> tuple[list, str]:
    """Voice folders whose model.pth is byte-identical to another voice's (e.g. 'biden' and 'joe-biden' from two
    sites). Keeps the copy used for more covers (then the older install); returns (folders to remove, names)."""
    import hashlib
    from .models import list_voices
    by_size: dict = {}
    for v in list_voices():
        try:
            by_size.setdefault(v.pth.stat().st_size, []).append(v)
        except OSError:
            pass
    used: dict = {}
    for h in hist:
        used[h.get("voice")] = used.get(h.get("voice"), 0) + 1
    drop, pairs = [], []
    for group in by_size.values():
        if len(group) < 2:
            continue
        by_hash: dict = {}
        for v in group:
            by_hash.setdefault(hashlib.sha1(v.pth.read_bytes()).hexdigest(), []).append(v)
        for same in by_hash.values():
            if len(same) > 1:
                same.sort(key=lambda v: (-used.get(v.name, 0), v.installed or ""))
                drop += [v.dir for v in same[1:]]
                pairs.append(" = ".join(v.name for v in same))
    return drop, "; ".join(pairs)


def remove_song(sid: str, stems_only: bool = False):
    """Delete a song's stems (and unless stems_only, the song file, its key and its YouTube link)."""
    _retry(lambda: shutil.rmtree(path("cache_dir") / "stems" / sid) if (path("cache_dir") / "stems" / sid).exists() else None)
    if stems_only:
        return
    for f in path("uploads_dir").glob(f"{sid}.*"):
        _retry(lambda f=f: f.unlink(missing_ok=True))
    (path("cache_dir") / "keys" / f"{sid}.json").unlink(missing_ok=True)
    yt = path("cache_dir") / "youtube_songs.json"
    if yt.exists():
        try:
            d = {k: v for k, v in json.loads(yt.read_text(encoding="utf-8")).items() if v != sid}
            yt.write_text(json.dumps(d, indent=1), encoding="utf-8")
        except ValueError:
            pass


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


# ---------------------------------------------------------------- inventories

def summary(S) -> dict:
    cache = path("cache_dir")
    temp = [cache / "converted", cache / "keyshift", cache / "previews", cache / "_work", path("logs_dir")]
    temp += [p for p in cache.iterdir() if p.is_dir() and p.name.startswith(TEMP_PREFIXES)]
    out_files = [p for p in path("output_dir").iterdir() if p.is_file() and p.name != "history.jsonl"]
    cats = {
        "covers": sum(_size(p) for p in out_files),
        "songs": _size(path("uploads_dir")),
        "stems": _size(cache / "stems"),
        "voices": _size(path("models_dir") / "voices"),
        "training": _size(cache / "training"),
        "temporary": sum(_size(p) for p in temp),
        "app": sum(_size(path("models_dir") / d) for d in ("rvc", "separator", "speaker")),
    }
    du = shutil.disk_usage(ROOT)
    return {"categories": cats, "total": sum(cats.values()), "free": du.free, "drive": str(ROOT.anchor).rstrip("\\"),
            "counts": {"covers": len(covers(S)), "songs": len(_song_metas()),
                       "voices": len(list((path("models_dir") / "voices").glob("*/meta.json"))),
                       "training": len(list((cache / "training").glob("*/project.json")))}}


def covers(S) -> list[dict]:
    from .pipeline import history
    metas = _song_metas()
    out = []
    for h in history():
        o = Path(h["output"])
        song_id = Path(h["song"]).stem
        m = metas.get(song_id, {})
        opts = h.get("options", {})
        out.append({
            "id": h["id"], "title": h.get("title") or o.stem, "voice": h["voice"],
            "backing_voice": opts.get("backing_voice") if opts.get("backing_voice") != h["voice"] else None,
            "created": h.get("created", ""), "seconds": h.get("seconds", {}), "options": opts, "notes": h.get("notes", []),
            "file": o.name, "format": o.suffix.lstrip(".").upper(), "size": _size(o), "duration": m.get("duration"),
            "song_id": song_id if m else None, "song_source": m.get("source_url"),
            "url": S.file_url(o), "original": S.file_url(h["song"]) if Path(h["song"]).exists() else None,
            "vocal": S.file_url(h.get("converted_vocal")) if h.get("converted_vocal") and Path(h["converted_vocal"]).exists() else None,
        })
    return out


STEM_LABELS = {"vocals": "Vocals", "instrumental": "Instrumental", "other": "Instrumental", "noreverb": "Vocals (dry)", "reverb": "Reverb (removed)"}


def _stems_for(song_id: str, S) -> list[dict]:
    root = path("cache_dir") / "stems" / song_id
    out = []
    if not root.exists():
        return out
    for f in sorted(root.rglob("*.wav")):
        rel = f.relative_to(root).parts
        label = STEM_LABELS.get(f.stem, f.stem.capitalize())
        if any(p.startswith("karaoke_") for p in rel[:-1]):
            label = {"vocals": "Lead vocal", "instrumental": "Backing vocals"}.get(f.stem, label)
            if any(p.startswith("dereverb_") for p in rel[:-1]):
                label = {"noreverb": "Lead vocal (dry)", "reverb": "Lead reverb (removed)"}.get(f.stem, label)
        elif any(p.startswith("dereverb_") for p in rel[:-1]):
            label = {"noreverb": "Vocals (dry)", "reverb": "Reverb (removed)"}.get(f.stem, label)
        quality = "fast" if rel[0].endswith("_fast") else "max" if rel[0].endswith("_max") else "high"
        out.append({"label": label, "size": _size(f), "url": S.file_url(f), "quality": quality, "model": rel[0]})
    return out


def songs(S) -> list[dict]:
    from .pipeline import history
    hist = history()
    current = Path(CONFIG["separation"]["vocal_model"]).stem
    out = []
    for sid, m in _song_metas().items():
        f = path("uploads_dir") / m.get("file", "")
        used = [h for h in hist if Path(h["song"]).stem == sid]
        stems = _stems_for(sid, S)
        last = max([_mtime(Path(h["output"])) for h in used] + [_mtime(f)])
        key = None
        kf = path("cache_dir") / "keys" / f"{sid}.json"
        if kf.exists():
            try:
                key = json.loads(kf.read_text(encoding="utf-8")).get("name")
            except ValueError:
                pass
        old_model = bool(stems) and not any(s["model"].startswith(current) for s in stems)
        out.append({
            "id": sid, "title": m.get("title") or m.get("name"), "duration": m.get("duration"), "key": key,
            "sample_rate": m.get("sample_rate"), "channels": m.get("channels"), "lufs": m.get("lufs"),
            "source": "YouTube" if m.get("youtube_id") or m.get("source_url") else "dropped file", "source_url": m.get("source_url"),
            "size": _size(f), "stems": stems, "stems_size": sum(s["size"] for s in stems), "stems_old_model": old_model,
            "covers": len(used), "cover_titles": [Path(h["output"]).stem for h in used][:5], "last_used": last,
            "url": S.file_url(f) if f.exists() else None,
        })
    out.sort(key=lambda x: -x["last_used"])
    return out


def voices(S) -> list[dict]:
    from .autopilot import undertrained
    from .models import list_voices
    from .pipeline import history
    hist = history()
    q = _quality()
    out = []
    for v in list_voices():
        ex = v.extra or {}
        score = verdict = None
        for k, r in q.items():   # a measured check for this voice (installed or downloaded for a check)
            if r.get("voice") == v.name or k.endswith(f"installed:{v.name}:{v.installed}"):
                score, verdict = r.get("score"), r.get("verdict")
        sample = None
        if ex.get("trained") and ex.get("project"):
            pv = path("cache_dir") / "training" / ex["project"] / "train" / "previews" / f"e{ex.get('epoch')}.wav"
            sample = S.file_url(pv) if pv.exists() else None
        used = [h for h in hist if h["voice"] == v.name or h.get("options", {}).get("backing_voice") == v.name]
        out.append({
            "name": v.name, "version": v.version, "sr": v.sr, "index": bool(v.index), "installed": v.installed,
            "source": v.source, "trained": bool(ex.get("trained")), "project": ex.get("project"),
            "epoch": ex.get("epoch"), "train_minutes": ex.get("train_minutes"), "from_speech": ex.get("from_speech"),
            "f0_range": ex.get("f0_range"), "undertrained": undertrained(v), "score": score, "verdict": verdict,
            "size": _size(v.dir), "covers": len(used), "cover_titles": [h.get("title") or Path(h["output"]).stem for h in used][:5],
            "sample": sample,
        })
    out.sort(key=lambda x: x["name"])
    return out


def training(S) -> list[dict]:
    from .train.project import list_projects, projects_dir
    out = []
    for p in list_projects():
        d = projects_dir() / p["name"]
        t = d / "train"
        parts = {"downloads": _size(d / "raw"), "clips": _size(d / "clips"),
                 "training data": _size(t / "data.pt"), "resume point": _size(t / "resume.pt"),
                 "saves": sum(_size(f) for f in t.glob("e*.pth")) if t.exists() else 0}
        out.append({**p, "size": _size(d), "parts": parts, "updated": _mtime(d / "project.json")})
    out.sort(key=lambda x: -x["updated"])
    return out


# ---------------------------------------------------------------- clean up

def _cleanup_groups(S) -> dict:
    from .pipeline import history
    from .train.project import list_projects, projects_dir
    cache = path("cache_dir")
    now = time.time()
    hist = history()
    g = {}
    conv = [f for f in (cache / "converted").glob("*") if f.is_file() and now - _mtime(f) > DAY]
    g["converted"] = {"title": "Temporary conversion files",
                      "detail": "Converted vocals from past covers (the covers are kept; their \"vocal only\" download goes)",
                      "paths": conv, "default": True}
    g["keyshift"] = {"title": "Saved key changes", "detail": "Transposed backing tracks, re-made in a few seconds when needed",
                     "paths": [f for f in (cache / "keyshift").glob("*") if f.is_file()], "default": True}
    old = []
    for s in songs(S):
        if s["stems"] and now - s["last_used"] > 30 * DAY:
            old.append(cache / "stems" / s["id"])
    g["old_stems"] = {"title": "Stems of songs not used for 30 days",
                      "detail": f"{len(old)} songs: separated again if you make another cover of them (~1 min each)",
                      "paths": old, "default": True}
    unused = []
    for s in songs(S):
        if s["covers"] == 0 and now - s["last_used"] > 30 * DAY:
            unused += [p for p in path("uploads_dir").glob(f"{s['id']}.*")] + [cache / "stems" / s["id"]]
    songs_known = {p.stem for p in path("uploads_dir").glob("*.json")}
    current = Path(CONFIG["separation"]["vocal_model"]).stem
    orphan = [d for d in (cache / "stems").iterdir() if d.is_dir() and d.name not in songs_known] if (cache / "stems").exists() else []
    g["orphan_stems"] = {"title": "Stems that belong to no song in the library",
                         "detail": "Left over from training downloads, tests and deleted songs", "paths": orphan, "default": True}
    other = [m for d in (cache / "stems").iterdir() if d.is_dir() and d.name in songs_known
             for m in d.iterdir() if m.is_dir() and not (m.name == current or m.name.startswith(current + "_"))] if (cache / "stems").exists() else []
    g["other_model_stems"] = {"title": "Stems made with older separation models",
                              "detail": "Covers use the current model, so these songs would be separated again anyway", "paths": other, "default": True}
    used_models = {CONFIG["separation"][k] for k in ("vocal_model", "dereverb_model", "karaoke_model")}
    sep_dir = path("models_dir") / "separator"
    unused_models = [f for f in sep_dir.glob("*.ckpt") if f.name not in used_models] if sep_dir.exists() else []
    unused_models += [f for f in sep_dir.glob("*.yaml") if not any(Path(u).stem.split("_sdr")[0] in f.stem for u in used_models)] if sep_dir.exists() else []
    g["unused_models"] = {"title": "Separation models UnderCover doesn't use",
                          "detail": "From earlier comparisons; downloaded again automatically if a setting ever picks one",
                          "paths": unused_models, "default": True}
    g["unused_songs"] = {"title": "Songs never used for a cover (30+ days)", "detail": "The song files and their stems",
                         "paths": unused, "default": False}
    dupes, names = _duplicate_voices(hist)
    g["duplicate_voices"] = {"title": "The same voice installed twice",
                             "detail": (f"Identical models under two names ({names}); the copy used for fewer covers goes"
                                        if dupes else "Identical models under two names"),
                             "paths": dupes, "default": False}
    done = [projects_dir() / p["name"] for p in list_projects() if p["train"].get("state") == "exported"]
    g["finished_training"] = {"title": "Training projects of finished voices",
                              "detail": f"{len(done)} projects: their clips are only needed to train those voices again (the voices stay)",
                              "paths": done, "default": False}
    paused = [projects_dir() / p["name"] / "train" / "resume.pt" for p in list_projects()
              if p["train"].get("state") != "exported" and (projects_dir() / p["name"] / "train" / "resume.pt").exists()]
    g["resume_points"] = {"title": "Resume points of unfinished trainings",
                          "detail": "Those trainings would have to start over", "paths": paused, "default": False}
    temp = [p for p in cache.iterdir() if p.is_dir() and p.name.startswith(TEMP_PREFIXES)]
    temp += [p for p in (cache / "previews").glob("*") if now - _mtime(p) > DAY]
    temp += [p for p in (cache / "_work").glob("*")]
    logs = [f for f in path("logs_dir").glob("*.log") if _size(f) > 10e6]
    g["temp"] = {"title": "Old previews, leftovers and big logs", "detail": "Preview renders older than a day, interrupted downloads, logs over 10 MB",
                 "paths": temp + logs, "default": True}
    for k, v in g.items():
        v["size"] = sum(_size(p) for p in v["paths"])
        v["count"] = len(v["paths"])
    return g


def _remove(p: Path):
    if p.suffix == ".log" and p.is_file():   # logs are truncated, not deleted (the server may be writing to it)
        p.write_text("", encoding="utf-8")
    elif p.is_dir():
        shutil.rmtree(p, ignore_errors=True)
    else:
        p.unlink(missing_ok=True)


# ---------------------------------------------------------------- routing

def handle(S, h, method: str, p: str) -> bool:
    if not p.startswith("/api/library/"):
        return False
    try:
        _route(S, h, method, p)
    except ModelError as e:
        h._error(str(e))
    except LookupError as e:
        h._error(str(e), 404)
    return True


def _route(S, h, method, p):
    q = parse_qs(urlparse(h.path).query)
    rest = p[len("/api/library/"):]
    if method == "GET" and rest in ("summary", "covers", "songs", "voices", "training"):
        return h._json(globals()[rest](S))
    if rest == "cleanup":
        if method == "GET":
            return h._json({k: {kk: vv for kk, vv in v.items() if kk != "paths"} for k, v in _cleanup_groups(S).items()})
        _guard(S)
        want = set((h._body().get("groups") or []))
        freed = 0
        for k, v in _cleanup_groups(S).items():
            if k in want:
                freed += v["size"]
                for path_ in v["paths"]:
                    _remove(path_)
        return h._json({"freed": freed})
    if rest == "covers/zip" and method == "GET":
        ids = {int(x) for x in (q.get("ids") or [""])[0].split(",") if x.isdigit()}
        files = [(path("output_dir") / c["file"], c["file"]) for c in covers(S) if c["id"] in ids]
        if not files:
            raise LookupError("Nothing selected.")
        return _zip_response(h, f"UnderCover covers ({len(files)}).zip", files)
    m = re.fullmatch(r"covers/(\d+)(?:/(rename))?", rest)
    if m:
        from .pipeline import history
        item = next((x for x in history() if x["id"] == int(m.group(1))), None)
        if not item:
            raise LookupError("That cover isn't there any more.")
        if method == "DELETE":
            _guard(S)
            for f in (item["output"], item.get("converted_vocal"), item.get("converted_backing")):
                if f:   # history hides entries whose file is gone (ids stay stable)
                    _retry(lambda f=f: Path(f).unlink(missing_ok=True))
            return h._json({"ok": True})
        if method == "POST" and m.group(2) == "rename":
            _guard(S)
            name = re.sub(r'[<>:"/\\|?*]+', "_", (h._body().get("name") or "")).strip().strip(".")
            if not name:
                raise ModelError("Type a name.")
            old = Path(item["output"])
            new = old.with_name(name + old.suffix)
            if new.exists() and new != old:
                raise ModelError(f"There's already a file called \"{new.name}\".")
            _retry(lambda: old.rename(new))
            item["output"] = str(new)
            if not item.get("title_set"):
                item["title"], item["title_set"] = name, True
            _write_history_line(item["id"], item)
            return h._json({"ok": True, "file": new.name})
    m = re.fullmatch(r"songs/([0-9a-f]{16})", rest)
    if m and method == "DELETE":
        _guard(S)
        remove_song(m.group(1), stems_only=(q.get("stems_only") or ["0"])[0] == "1")
        return h._json({"ok": True})
    m = re.fullmatch(r"voices/([a-z0-9-]+)/(rename|export)", rest)
    if m:
        from .models import get_voice, voices_dir
        v = get_voice(m.group(1))
        if m.group(2) == "export" and method == "GET":
            from .rvc.index import export_faiss
            tmp = path("cache_dir") / f"_export_{v.name}.index"
            try:   # stored compact (fp16): other RVC tools need the standard faiss layout
                idx = export_faiss(v.index, tmp) if v.index else None
                files = [(v.pth, f"{v.name}.pth")] + ([(idx, f"{v.name}.index")] if idx else [])
                return _zip_response(h, f"{v.name}.zip", files)
            finally:
                tmp.unlink(missing_ok=True)
        if m.group(2) == "rename" and method == "POST":
            _guard(S)
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
            from . import fsutil
            fsutil.write_text(target / "meta.json", json.dumps(meta, indent=2))
            if S.ENGINE and S.ENGINE._conv:
                S.ENGINE._conv.voices.pop(v.name, None)
            return h._json({"ok": True, "name": new})
    raise LookupError("Not found")
