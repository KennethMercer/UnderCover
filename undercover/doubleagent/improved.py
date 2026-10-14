"""An overnight improve's retrained voice, waiting beside the current one (Internal Affairs P2-1): the ear decides, and
the user's voice is never deleted.

  night._improve leaves `<voice>-next` beside `<voice>` (extra.improved_pending on the current one, extra.pending_for
  on the candidate). Here:

  state(voice)             what waits: the candidate, the numbers both had, the two listening copies when made
  make_samples(job, voice) the same spoken line through both voices (a GPU job), for a blind A/B through MasterPlan
  keep(voice, "new")       the candidate becomes <voice>; the current one is set aside (voices/.aside/, its meta renamed
                           so no list shows it), never deleted. The voice's cadence and display name carry over.
  keep(voice, "current")   the candidate is set aside the same way.
  undo(voice)              the last keep() undone: both back as they were, the candidate waiting again.

    GET  /api/train/voices/<v>/improved              state
    POST /api/train/voices/<v>/improved/ab           -> {job}: the two listening copies
    POST /api/train/voices/<v>/improved/keep         {keep: "new"|"current"}
    POST /api/train/voices/<v>/improved/undo
"""
import json
import os
import shutil
import threading
import time
from pathlib import Path

import numpy as np

from ..core import fsutil, gpu, jobs, storage
from ..core.config import path
from ..core.voices import ModelError, get_voice, label_of, voices_dir

LINE = Path(__file__).resolve().parents[1] / "assets" / "demo-voice" / "agent-smoke" / "tts" / "ref.wav"   # a spoken line
_LOCK = threading.Lock()


def _aside_dir() -> Path:
    return voices_dir() / ".aside"


def _move_voice(src: Path, dst: Path, name: str | None, listed: bool):
    """A voice folder moved within the voices folder (never over anything, never out of it; test mode: the test roots
    only). name: the voice's new name (its meta's paths follow). listed: False = set aside (meta.json renamed, so no
    voice list shows it); True = back in the lists."""
    root = voices_dir()
    if storage.ALLOWED_ROOTS is not None:
        why = storage.refuse([src, dst])
        if why:
            raise ModelError(why)
    for p in (src, dst):
        if not storage._inside(p, root) or storage._same(p, root):
            raise ModelError(f"{p} isn't inside the voices folder; nothing was moved.")
    if dst.exists():
        raise ModelError(f"{dst.name} already exists; nothing was moved.")
    dst.parent.mkdir(parents=True, exist_ok=True)
    fsutil.retry(lambda: os.replace(src, dst))
    meta_on, meta_off = dst / "meta.json", dst / "meta.json.aside"
    cur = meta_on if meta_on.exists() else meta_off
    meta = json.loads(cur.read_text(encoding="utf-8"))
    if name:
        meta.update(name=name, dir=str(dst), pth=str(dst / "model.pth"),
                    index=str(dst / "model.index") if meta.get("index") else meta.get("index"))
    want = meta_on if listed else meta_off
    fsutil.write_text(want, json.dumps(meta, indent=2))
    if cur != want:
        cur.unlink(missing_ok=True)


def _meta(d: Path) -> dict:
    f = d / "meta.json" if (d / "meta.json").exists() else d / "meta.json.aside"
    return json.loads(f.read_text(encoding="utf-8"))


def _write_meta(d: Path, meta: dict):
    fsutil.write_text(d / "meta.json", json.dumps(meta, indent=2))


def _samples(voice: str) -> dict:
    d = path("cache_dir") / "previews"
    return {k: d / f"improve-{voice}-{k}.flac" for k in ("current", "new")}


def state(voice: str) -> dict | None:
    """{voice, display, candidate, before, after, minutes, made, current_url, new_url, undo} or None."""
    from ..core.files import file_url
    v = get_voice(voice)
    ex = v.extra or {}
    p = ex.get("improved_pending")
    undo = bool(ex.get("improved_previous") or ex.get("improved_turned_down"))
    if not p:
        return {"voice": v.name, "display": label_of(v.name), "candidate": None, "undo": undo} if undo else None
    s = _samples(v.name)
    return {"voice": v.name, "display": label_of(v.name), "candidate": p["voice"], "before": p.get("before"),
            "after": p.get("after"), "minutes": p.get("minutes"), "made": p.get("made"), "undo": undo,
            "current_url": file_url(s["current"]) if s["current"].exists() else None,
            "new_url": file_url(s["new"]) if s["new"].exists() else None}


def make_samples(job, voice: str) -> dict:
    """The same spoken line through the current voice and the candidate (each at its own pitch), saved to hear: the
    blind A/B MasterPlan presents. A GPU job."""
    import soundfile as sf
    from ..core.cadence import render
    from ..core.engine.convert import LoadedVoice, Settings
    st = state(voice)
    if not st or not st["candidate"]:
        raise ModelError(f"No retrained {label_of(voice)} is waiting.")
    conv = gpu.MODELS.converter
    x = render.trim(render.load16(LINE))
    out = _samples(voice)
    out["current"].parent.mkdir(parents=True, exist_ok=True)
    for key, name in (("current", voice), ("new", st["candidate"])):
        job.push(stage="reading", fraction=0.1 if key == "current" else 0.55, label=f"Reading the line with the {key} one")
        v = get_voice(name)
        shift = render.octave_shift(x, conv.rmvpe, (v.extra or {}).get("f0_median"))
        lv = LoadedVoice(v, conv.device, conv.half)
        y, sr = conv.convert(x, lv, Settings(pitch=shift))
        del lv
        sf.write(str(out[key]), render.level(np.nan_to_num(y)), sr, subtype="PCM_16")
    return state(voice)


def keep(voice: str, which: str) -> dict:
    """The user's pick. "new": the candidate takes the voice's name, the current one is set aside. "current": the
    candidate is set aside. Nothing is deleted; undo() puts it back."""
    if which not in ("new", "current"):
        raise ModelError('Say which one to keep: "new" or "current".')
    with _LOCK:
        v = get_voice(voice)
        p = (v.extra or {}).get("improved_pending")
        if not p:
            raise ModelError(f"No retrained {label_of(voice)} is waiting.")
        cand_dir = voices_dir() / p["voice"]
        if not (cand_dir / "meta.json").exists():
            raise ModelError("The retrained voice isn't there any more.")
        stamp = time.strftime("%Y%m%d-%H%M%S")
        rec = {"at": time.strftime("%Y-%m-%d %H:%M"), "candidate": p["voice"], "before": p.get("before"), "after": p.get("after")}
        if which == "current":
            aside = _aside_dir() / f"{p['voice']}-{stamp}"
            _move_voice(cand_dir, aside, None, listed=False)
            meta = _meta(v.dir)
            meta["extra"].pop("improved_pending", None)
            meta["extra"]["improved_turned_down"] = {**rec, "aside": aside.name}
            _write_meta(v.dir, meta)
        else:
            old_meta = _meta(v.dir)
            aside = _aside_dir() / f"{voice}-{stamp}"
            _move_voice(v.dir, aside, None, listed=False)           # the current one, set aside (never deleted)
            _move_voice(cand_dir, voices_dir() / voice, voice, listed=True)
            new_dir = voices_dir() / voice
            meta = _meta(new_dir)
            ex = meta.setdefault("extra", {})
            ex.pop("pending_for", None)
            if old_meta.get("display"):
                meta["display"] = old_meta["display"]
            if (aside / "tts").is_dir() and not (new_dir / "tts").exists():   # the voice's cadence is the person's: it stays
                fsutil.retry(lambda: os.replace(aside / "tts", new_dir / "tts"))
                if (old_meta.get("extra") or {}).get("tts"):
                    ex["tts"] = old_meta["extra"]["tts"]
            ex["improved_previous"] = {**rec, "aside": aside.name}
            _write_meta(new_dir, meta)
        _drop_samples(voice)
    gpu.forget_voice(voice)
    gpu.forget_voice(p["voice"])
    return {"kept": which, "voice": voice}


def undo(voice: str) -> dict:
    """The last keep() undone: the two voices back as they were, the retrained one waiting again."""
    with _LOCK:
        v = get_voice(voice)
        ex = v.extra or {}
        if ex.get("improved_previous"):
            rec = ex["improved_previous"]
            aside = _aside_dir() / rec["aside"]
            if not aside.is_dir():
                raise ModelError("The previous voice isn't set aside any more.")
            cur = v.dir
            back_tts = (cur / "tts").is_dir() and not (aside / "tts").exists() and (_meta(aside).get("extra") or {}).get("tts")
            if back_tts:
                fsutil.retry(lambda: os.replace(cur / "tts", aside / "tts"))
            meta = _meta(cur)
            meta["extra"].pop("improved_previous", None)
            if back_tts:
                meta["extra"].pop("tts", None)
            meta["extra"]["pending_for"] = voice
            _write_meta(cur, meta)
            _move_voice(cur, voices_dir() / rec["candidate"], rec["candidate"], listed=True)
            _move_voice(aside, voices_dir() / voice, voice, listed=True)
            old = _meta(voices_dir() / voice)
            old.setdefault("extra", {})["improved_pending"] = {"voice": rec["candidate"], "before": rec.get("before"),
                                                               "after": rec.get("after"), "made": rec.get("at")}
            _write_meta(voices_dir() / voice, old)
        elif ex.get("improved_turned_down"):
            rec = ex["improved_turned_down"]
            aside = _aside_dir() / rec["aside"]
            if not aside.is_dir():
                raise ModelError("The retrained voice isn't set aside any more.")
            _move_voice(aside, voices_dir() / rec["candidate"], rec["candidate"], listed=True)
            meta = _meta(v.dir)
            meta["extra"].pop("improved_turned_down", None)
            meta["extra"]["improved_pending"] = {"voice": rec["candidate"], "before": rec.get("before"),
                                                 "after": rec.get("after"), "made": rec.get("at")}
            _write_meta(v.dir, meta)
        else:
            raise ModelError("There's nothing to undo for this voice.")
    gpu.forget_voice(voice)
    return {"undone": True, "voice": voice}


def _drop_samples(voice: str):
    files = [f for f in _samples(voice).values() if f.exists()]
    if files:
        storage.remove_files(files, within=[path("cache_dir") / "previews"])


def start_samples(voice: str) -> dict:
    job = jobs.Job("improve-ab", title=f"Listening copies: {label_of(voice)}")
    jobs.JOBS[job.id] = job

    def run(job):
        r = make_samples(job, voice)
        job.finish("done", message="Both readings are ready: listen and keep the one you like.", result=r)
    jobs.GPU_QUEUE.put((job, run))
    return {"job": job.id}
