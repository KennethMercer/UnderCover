"""Make smaller (phase 24, "smaller, never worse"): the user's EXISTING voices and training projects, made smaller only
when the user asks, one at a time. New files are made small already (training clips as FLAC, the exported save
hard-linked, key changes and speech as FLAC); this is for what was made before.

  a voice     its index as 10k k-means centres (core/engine/rvc/index.shrink; Agent Smoke: 256 MB -> 15 MB). Checked
              by reading a fixed line through the voice with both indexes: how alike the two come out, and how like
              the person each is when the voice has its training project; both readings are kept to hear (Before /
              After: <cache>/previews/smaller-<voice>-full|small.flac, result.full_url / small_url). The full index
              waits aside (model.index.before-smaller) until the user keeps the smaller one (Keep) or puts it back
              (Undo); the readings go with either.
  a project   its WAV clips as FLAC (each decoded and compared: bit-identical), and once its voice is added, the
              prepared data (train/data.pt: rebuilt by the next Train more). The originals wait aside
              (<project>/before-smaller/) until Keep or Undo. The exported save, when it's the same bytes as the voice's
              model.pth, becomes one file on disk (a hard link: nothing is lost, nothing set aside).

Never automatic, never the only copy: nothing is deleted until the user presses Keep.

    GET  /api/library/smaller                    {voices: [...], projects: [...], saving}  (what each would save)
    POST /api/library/smaller/start   {kind, name} -> {job}   (kind "voice" | "project")
    POST /api/library/smaller/keep    {kind, name}            the set-aside originals deleted (the user's choice)
    POST /api/library/smaller/undo    {kind, name}            the originals back
"""
import json
import os
import struct
import time
from pathlib import Path

import numpy as np

from ..core import fsutil, jobs, storage
from ..core.voices import ModelError

INDEX_ASIDE = "model.index.before-smaller"
ASIDE = "before-smaller"
RESULT = "smaller.json"
CLIP_FLAC_RATIO = 0.44   # measured on 7,178 clips (docs/research/phase24-audit.md)
ALIKE_SAME = 0.98        # both indexes' readings at least this alike (Agent Smoke: 0.996): "sounds the same"
LIKENESS_DROP = 0.02     # likeness to the person may fall this much (the golden set's band for likeness)
TEST_LINE = Path(__file__).resolve().parents[1] / "assets" / "demo-voice" / "agent-smoke" / "tts" / "ref.wav"


def _voices_dir() -> Path:
    from ..core.voices import voices_dir
    return voices_dir()


def _projects_dir() -> Path:
    from ..doubleagent.project import projects_dir
    return projects_dir()


def _child(root: Path, name: str) -> Path:
    """root/name for a plain folder name (letters, digits, - _ . and spaces; never a path, a drive or a dot name)."""
    import re
    if not re.fullmatch(r"[\w][\w .-]*", name or "") or name.endswith((".", " ")):
        raise ModelError("Which voice or project?")
    p = Path(root) / name
    if p.parent != Path(root):
        raise ModelError("Which voice or project?")
    return p


def _read(f: Path, default=None):
    try:
        return json.loads(Path(f).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _index_shape(f: Path) -> tuple[int, int]:
    """(vectors, dims) without reading the vectors (a compact index's header; a faiss file estimated from its size)."""
    from ..core.engine.rvc.index import COMPACT_MAGIC, is_compact
    if is_compact(f):
        with open(f, "rb") as fh:
            fh.seek(len(COMPACT_MAGIC))
            n, d = struct.unpack("<qq", fh.read(16))
        return int(n), int(d)
    return f.stat().st_size // (768 * 4), 768


# ---------------------------------------------------------------- what could be smaller

def voice_row(d: Path) -> dict | None:
    from ..core.engine.rvc.index import CENTRES
    idx = d / "model.index"
    if not idx.is_file():
        return None
    size = idx.stat().st_size
    try:
        n, dims = _index_shape(idx)
    except (OSError, struct.error):
        return None
    after = min(n, CENTRES) * dims * 2 + 24
    pending = (d / INDEX_ASIDE).exists()
    result = _read(d / RESULT) if pending else None
    if result:
        from ..core.files import file_url
        for key, f in _readings(d.name).items():
            result[f"{key}_url"] = file_url(f) if f.exists() else None
    return {"name": d.name, "index_bytes": size, "vectors": n, "saving": max(0, size - after) if n > CENTRES else 0,
            "pending": pending, "result": result}


def _readings(name: str) -> dict:
    """The two test readings of a voice waiting for Keep or Undo: {"full": path, "small": path}."""
    from ..core.config import path
    d = path("cache_dir") / "previews"
    return {k: d / f"smaller-{name}-{k}.flac" for k in ("full", "small")}


def project_row(d: Path) -> dict | None:
    pj = _read(d / "project.json")
    if pj is None:
        return None
    wav = sum(f.stat().st_size for f in (d / "clips").glob("*.wav")) if (d / "clips").is_dir() else 0
    tr = pj.get("train") or {}
    data = d / "train" / "data.pt"
    data_bytes = data.stat().st_size if tr.get("exported") and data.is_file() else 0
    aside = d / ASIDE
    return {"name": d.name, "person": pj.get("person") or "", "clips_wav_bytes": wav, "data_bytes": data_bytes,
            "saving": int(wav * (1 - CLIP_FLAC_RATIO)) + data_bytes, "pending": aside.is_dir(),
            "result": _read(aside / RESULT) if aside.is_dir() else None}


def overview() -> dict:
    vd, pd = _voices_dir(), _projects_dir()
    voices = [r for d in sorted(vd.iterdir()) if d.is_dir() for r in [voice_row(d)] if r] if vd.is_dir() else []
    projects = [r for d in sorted(pd.iterdir()) if d.is_dir() for r in [project_row(d)] if r] if pd.is_dir() else []
    return {"voices": voices, "projects": projects,
            "saving": sum(r["saving"] for r in voices if not r["pending"]) + sum(r["saving"] for r in projects if not r["pending"])}


# ---------------------------------------------------------------- a voice

def _test_readings(name: str, small: Path) -> dict:
    """The fixed line through the voice with its full index and with the small one: how alike, and how like the person."""
    from dataclasses import replace
    from ..core import gpu
    from ..core.cadence import render
    from ..core.engine.convert import LoadedVoice, Settings
    from ..core.speaker import SpeakerEmbedder
    from ..core.voices import get_voice
    v = get_voice(name)
    conv = gpu.MODELS.converter
    x = render.trim(render.load16(TEST_LINE))
    shift = render.octave_shift(x, conv.rmvpe, (v.extra or {}).get("f0_median"))
    import soundfile as sf
    emb = SpeakerEmbedder()
    out = {}
    files = _readings(name)
    files["full"].parent.mkdir(parents=True, exist_ok=True)
    for key, voice in (("full", v), ("small", replace(v, index=small))):
        lv = LoadedVoice(voice, conv.device, conv.half)   # one at a time: the GPU is shared
        y, sr = conv.convert(x, lv, Settings(pitch=shift))
        del lv
        out[key] = emb.embed(_to16(y, sr))
        sf.write(str(files[key]), render.level(np.nan_to_num(y)), sr, subtype="PCM_16")   # to hear: Before / After
    alike = float(np.dot(out["full"], out["small"]) / (np.linalg.norm(out["full"]) * np.linalg.norm(out["small"]) + 1e-9))
    r = {"alike": round(alike, 4)}
    ref = _person(v)
    if ref is not None:
        for key in ("full", "small"):
            r[f"likeness_{key}"] = round(float(np.dot(out[key], ref) / (np.linalg.norm(out[key]) * np.linalg.norm(ref) + 1e-9)), 4)
    r["same"] = alike >= ALIKE_SAME and (ref is None or r["likeness_full"] - r["likeness_small"] <= LIKENESS_DROP)
    return r


def _to16(y: np.ndarray, sr: int) -> np.ndarray:
    import math
    from scipy.signal import resample_poly
    g = math.gcd(sr, 16000)
    return resample_poly(np.asarray(y, np.float32), 16000 // g, sr // g).astype(np.float32)


def _person(v) -> np.ndarray | None:
    """The person's reference voice print, from the voice's training project (None for a downloaded voice)."""
    proj = (v.extra or {}).get("project")
    pj = _projects_dir() / str(proj) if proj else None
    if not pj or not (pj / "embeddings.npz").exists():
        return None
    ref = (_read(pj / "project.json", {}).get("reference") or {}).get("cluster")
    with np.load(pj / "embeddings.npz") as z:
        k = f"speaker:{ref}"
        return z[k].copy() if k in z.files else None


def shrink_voice(name: str, check=True, k: int | None = None, device: str | None = None) -> dict:
    """The voice's index -> 10k centres; the full one aside. -> the result (also kept in <voice>/smaller.json)."""
    from ..core import gpu
    from ..core.engine.rvc.index import CENTRES, shrink
    d = _child(_voices_dir(), name)
    idx = d / "model.index"
    if not idx.is_file():
        raise ModelError("That voice has no index to make smaller.")
    if (d / INDEX_ASIDE).exists():
        raise ModelError("That voice is already smaller, waiting for Keep or Undo.")
    before = idx.stat().st_size
    tmp = d / "model.index.smaller.tmp"
    done = False
    try:
        n0, n1 = shrink(idx, k or CENTRES, dest=tmp, device=device)
        if n1 >= n0:
            raise ModelError("That voice's index is already small.")
        r = _test_readings(name, tmp) if check else {}
        r.update(vectors_before=n0, vectors_after=n1, bytes_before=before, bytes_after=tmp.stat().st_size,
                 made=time.strftime("%Y-%m-%d %H:%M"))
        storage.move_within([(idx, d / INDEX_ASIDE)], d)   # the full index aside,
        storage.move_within([(tmp, idx)], d)               # then the small one in its place (a target never overwritten)
        fsutil.write_text(d / RESULT, json.dumps(r, indent=1))
        done = True
    finally:
        tmp.unlink(missing_ok=True)
        if not done and not (d / INDEX_ASIDE).exists():
            _drop_readings(name)
    gpu.forget_voice(name)
    return r


def keep_voice(name: str):
    d = _child(_voices_dir(), name)
    if not (d / INDEX_ASIDE).exists():
        raise ModelError("There's nothing set aside for that voice.")
    storage.remove_files([d / INDEX_ASIDE, d / RESULT], within=[_voices_dir()])
    _drop_readings(name)


def _drop_readings(name: str):
    from ..core.config import path
    storage.remove_files([f for f in _readings(name).values() if f.exists()], within=[path("cache_dir") / "previews"])


def undo_voice(name: str):
    from ..core import gpu
    d = _child(_voices_dir(), name)
    if not (d / INDEX_ASIDE).exists():
        raise ModelError("There's nothing set aside for that voice.")
    storage.remove_files([d / "model.index", d / RESULT], within=[_voices_dir()])   # the small one (made here) goes
    storage.move_within([(d / INDEX_ASIDE, d / "model.index")], d)
    _drop_readings(name)
    gpu.forget_voice(name)


# ---------------------------------------------------------------- a training project

def shrink_project(name: str, progress=None) -> dict:
    """WAV clips -> FLAC (each checked bit-identical), the exported project's data.pt aside, the exported save
    linked to the voice's file. -> the result (kept in <project>/before-smaller/smaller.json)."""
    import soundfile as sf
    report = progress or (lambda f, m: None)
    d = _child(_projects_dir(), name)
    pj = _read(d / "project.json")
    if pj is None:
        raise ModelError("That project isn't there any more.")
    aside = d / ASIDE
    if aside.exists():
        raise ModelError("That project is already smaller, waiting for Keep or Undo.")
    wavs = sorted((d / "clips").glob("*.wav")) if (d / "clips").is_dir() else []
    r = {"clips": 0, "clips_bytes_before": 0, "clips_bytes_after": 0, "data_bytes": 0, "linked_bytes": 0}
    (aside / "clips").mkdir(parents=True, exist_ok=True)
    for i, w in enumerate(wavs):
        y, sr = sf.read(str(w), dtype="int16", always_2d=True)
        info = sf.info(str(w))
        if info.subtype != "PCM_16":   # only what FLAC keeps bit for bit as written here
            continue
        flac, part = w.with_suffix(".flac"), w.with_suffix(".smaller.part.flac")
        sf.write(str(part), y, sr, subtype="PCM_16")
        back, _ = sf.read(str(part), dtype="int16", always_2d=True)
        if not np.array_equal(back, y):
            part.unlink(missing_ok=True)
            raise ModelError(f"A clip didn't come back the same as FLAC ({w.name}); nothing more was changed.")
        os.replace(part, flac)   # a new file of ours
        r["clips_bytes_before"] += w.stat().st_size
        r["clips_bytes_after"] += flac.stat().st_size
        storage.move_within([(w, aside / "clips" / w.name)], d)
        r["clips"] += 1
        if i % 100 == 0:
            report(0.9 * i / max(1, len(wavs)), f"clip {i + 1} of {len(wavs)}")
    tr = pj.get("train") or {}
    data = d / "train" / "data.pt"
    if tr.get("exported") and data.is_file():
        r["data_bytes"] = data.stat().st_size
        storage.move_within([(data, aside / "train" / "data.pt")], d)
    if tr.get("exported") and tr.get("exported_epoch") is not None:
        save = d / "train" / f"e{tr['exported_epoch']}.pth"
        vp = _voices_dir() / str(tr["exported"]) / "model.pth"
        if save.is_file() and vp.is_file():
            same = os.path.samefile(save, vp)
            if not same and fsutil.link_duplicate(vp, save):
                r["linked_bytes"] = save.stat().st_size
    r["saving"] = r["clips_bytes_before"] - r["clips_bytes_after"] + r["data_bytes"] + r["linked_bytes"]
    r["made"] = time.strftime("%Y-%m-%d %H:%M")
    fsutil.write_text(aside / RESULT, json.dumps(r, indent=1))
    report(1.0, "done")
    return r


def keep_project(name: str):
    d = _child(_projects_dir(), name)
    if not (d / ASIDE).is_dir():
        raise ModelError("There's nothing set aside for that project.")
    storage.remove_files([], within=[_projects_dir()], dirs=[d / ASIDE])


def undo_project(name: str):
    """The WAV clips and data.pt back (never over a newer one: a data.pt made since stays); their FLAC copies go."""
    d = _child(_projects_dir(), name)
    aside = d / ASIDE
    if not aside.is_dir():
        raise ModelError("There's nothing set aside for that project.")
    moves = [(w, d / "clips" / w.name) for w in sorted((aside / "clips").glob("*.wav")) if not (d / "clips" / w.name).exists()]
    data = aside / "train" / "data.pt"
    if data.exists() and not (d / "train" / "data.pt").exists():
        moves.append((data, d / "train" / "data.pt"))
    if moves:
        storage.move_within(moves, d)
    gone = [dst.with_suffix(".flac") for _, dst in moves if dst.suffix == ".wav" and dst.with_suffix(".flac").exists()]
    storage.remove_files(gone, within=[_projects_dir()], dirs=[aside])


# ---------------------------------------------------------------- jobs and routes

def start(kind: str, name: str) -> dict:
    if kind == "voice":
        if not (_child(_voices_dir(), name) / "model.index").is_file():
            raise ModelError("That voice isn't there any more.")
        job = jobs.Job("smaller", title=f"Making {name} smaller")
        jobs.JOBS[job.id] = job

        def run(job):
            job.push(stage="smaller", fraction=0.1, label="shrinking the index and reading a test line both ways")
            r = shrink_voice(name)
            say = ("Smaller, and it sounds the same: keep it, or put the full index back."
                   if r.get("same") else "Smaller, but it came out a little different: Undo puts the full index back.")
            job.finish("done", message=say, result=r)
        jobs.GPU_QUEUE.put((job, run))   # the test readings use the GPU
        return {"job": job.id}
    if kind == "project":
        from ..doubleagent.api import ACTIVE, _busy
        if not (_child(_projects_dir(), name) / "project.json").exists():
            raise ModelError("That project isn't there any more.")
        job = jobs.Job("smaller", title=f"Making the {name} project smaller")
        if _busy(name):
            raise ModelError("That project is busy. Try again when it's done.")
        ACTIVE[name] = {"job": job, "control": None, "kind": "smaller"}   # Train more and Transcribe wait for it
        jobs.JOBS[job.id] = job

        def run():
            try:
                r = shrink_project(name, lambda f, m: job.push(stage="smaller", fraction=round(f, 3), label=m))
                job.finish("done", message="Smaller. The originals wait aside until you keep the smaller copy.", result=r)
            except Exception as e:   # noqa: BLE001 - shown on the job
                job.finish("error", message=jobs.friendly(e, "making the project smaller"))
        import threading
        threading.Thread(target=run, daemon=True, name="smaller").start()   # disk work only: no GPU
        return {"job": job.id}
    raise ModelError("Make smaller works on a voice or a training project.")


def handle(h, method: str, rest: str) -> bool:
    """rest: the path after /api/library/."""
    if rest == "smaller" and method == "GET":
        h._json(overview())
        return True
    if rest in ("smaller/start", "smaller/keep", "smaller/undo") and method == "POST":
        b = h._body()
        kind, name = str(b.get("kind") or ""), str(b.get("name") or "")
        _child(_voices_dir() if kind == "voice" else _projects_dir(), name)   # a plain name, or "Which voice or project?"
        if rest == "smaller/start":
            return h._json(start(kind, name)) or True
        fn = {("keep", "voice"): keep_voice, ("undo", "voice"): undo_voice,
              ("keep", "project"): keep_project, ("undo", "project"): undo_project}.get((rest.split("/")[1], kind))
        if fn is None:
            raise ModelError("Make smaller works on a voice or a training project.")
        fn(name)
        h._json(overview())
        return True
    return False
