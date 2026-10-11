"""SpeakEasy: text -> Kokoro reads it (tts.py) -> one of our voices turns the Reader into the person (the shared RVC
converter, core.gpu.MODELS). Long text goes sentence by sentence: each one is written as soon as it's made, so the
page starts playing after the first while the rest are made (Kokoro reads the next sentence on the CPU while the
GPU converts this one).

Results live in output/speech/: <name>.wav (+ .mp3) and <id>.json describing it (text, voice, Reader, speed, ...).
SafeHouse lists, renames and deletes them through this file. A speech job is short GPU work like a cover: never paused.
"""
import json
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

import numpy as np
import soundfile as sf

from ..core import fsutil, jobs
from ..core.config import path
from ..core.files import file_url
from ..core.jobs import Cancelled
from ..core.voices import ModelError, get_voice
from ..doubleagent import service
from . import tts

MAX_CHARS = 50_000   # about 2 hours of speech
SPEEDS = (0.7, 1.4)


@jobs.register_user_error
class SpeakError(Exception):
    """Something the user can fix (no text, unknown voice, ...): shown as-is."""


def speech_dir() -> Path:
    d = path("output_dir") / "speech"
    d.mkdir(parents=True, exist_ok=True)
    return d


def parts_dir(job_id: str) -> Path:
    """Sentences as they're made (cache/previews/, so old ones are cleaned up after a day like previews)."""
    return path("cache_dir") / "previews" / f"speech_{job_id}"


# ---------------------------------------------------------------- plan: which Reader, which pitch

def plan(voice: str, reader: str = "auto", speed: float = 1.0, nudge: float = 0.0) -> dict:
    """How a voice will read: the Reader (auto-picked from the voice's speaking pitch unless chosen) and the pitch
    shift that puts the Reader's speech at the person's speaking pitch (+ nudge semitones)."""
    v = get_voice(voice)
    sp = service.speaking_pitch(v)
    auto = reader in (None, "", "auto")
    if not auto and reader not in tts.READERS:
        raise SpeakError(f"Unknown Reader '{reader}'.")
    r = tts.pick_reader(sp and sp["hz"], sp and sp["low"]) if auto else reader
    pitch = (tts.pitch_for(r, sp["hz"]) if sp else 0.0) + float(nudge or 0)
    return {"voice": v.name, "reader": r, "auto": auto, "reader_label": tts.READERS[r][0], "reader_hz": tts.READERS[r][1][1],
            "target_hz": sp and sp["hz"], "target_low": sp and sp["low"], "pitch_from": sp and sp["how"],
            "pitch": float(np.clip(pitch, -24, 24)), "nudge": float(nudge or 0),
            "speed": float(np.clip(speed or 1.0, *SPEEDS))}


def voices() -> list[dict]:
    """Every voice with what SpeakEasy needs to list it (group, likeness, kind, auto Reader, cadence status)."""
    from ..core.voices import list_voices
    out = []
    for v in list_voices():
        ex = v.extra or {}
        try:
            p = plan(v.name)
        except Exception:
            continue
        out.append({"name": v.name, "trained": bool(ex.get("trained")), "likeness": ex.get("likeness"),
                    "person": ex.get("person"), "kind": ("speech" if ex.get("from_speech") else "singing") if ex.get("trained")
                    else ("singing" if ex.get("person_range") and not ex.get("person_range_speech") else None),
                    "undertrained": service.undertrained(v), "cadence": bool(ex.get("tts")),
                    "reader": p["reader"], "reader_label": p["reader_label"], "pitch": p["pitch"],
                    "target_hz": p["target_hz"], "target_low": p["target_low"], "pitch_from": p["pitch_from"]})
    out.sort(key=lambda x: (not x["trained"], x["name"]))
    return out


def readers() -> list[dict]:
    return [{"id": k, "label": lab, "hz": f0[1], "grade": g, "auto": k in tts.AUTO} for k, (lab, f0, g) in tts.READERS.items()]


# ---------------------------------------------------------------- making speech

def _level(y: np.ndarray, rms_db: float = -20.0, peak: float = 0.89) -> np.ndarray:
    """The same loudness for every sentence (so playback doesn't jump between them), peaks under -1 dBFS."""
    if not len(y):
        return y
    r = float(np.sqrt(np.mean(y ** 2))) or 1e-9
    y = y * min(10 ** (rms_db / 20) / r, peak / max(1e-9, float(np.abs(y).max())))
    f = min(len(y) // 2, 240)   # 5-10 ms fades: no clicks at the joins
    if f:
        ramp = np.linspace(0, 1, f, dtype=np.float32)
        y[:f] *= ramp
        y[-f:] *= ramp[::-1]
    return y.astype(np.float32)


def make_speech(job, text: str, voice: str, reader: str = "auto", speed: float = 1.0, nudge: float = 0.0) -> dict:
    """The speech job: sentences out as they're made (events with a URL each), then the finished WAV + MP3."""
    from scipy.signal import resample_poly
    from ..core import gpu
    from ..core.engine.convert import Settings
    text = (text or "").strip()
    if not text:
        raise SpeakError("Type or paste something to say first.")
    if len(text) > MAX_CHARS:
        raise SpeakError(f"That's {len(text):,} characters; SpeakEasy reads up to {MAX_CHARS:,} at a time (about two hours). "
                         "Split it into parts.")
    p = plan(voice, reader, speed, nudge)
    sents = tts.sentences(text)
    if not sents:
        raise SpeakError("There's nothing to read in that text.")
    t0 = time.perf_counter()
    job.push(stage="reading", frac=0.0, message="Getting ready", plan=p, sentences=[s["text"] for s in sents])
    if not tts.ready():
        job.push(stage="reading", frac=0.0, message="Downloading the speech model (about 340 MB, once)")
        tts.kokoro(lambda f, d, t: job.push(stage="reading", frac=0.0, message=f"Downloading the speech model: {d / 1e6:.0f} of {t / 1e6:.0f} MB")
                   if t else None)
    conv = gpu.MODELS.converter
    lv = conv.voice(p["voice"])
    sr = lv.tgt_sr
    settings = Settings(pitch=p["pitch"])
    pdir = parts_dir(job.id)
    pdir.mkdir(parents=True, exist_ok=True)

    # Kokoro (CPU) reads ahead in its own thread while the GPU converts
    q: "queue.Queue" = queue.Queue(maxsize=3)
    stop = threading.Event()

    def reader_thread():
        try:
            for s in sents:
                if stop.is_set():
                    return
                t = time.perf_counter()
                a = tts.synth(s["text"], p["reader"], p["speed"])
                q.put((a, time.perf_counter() - t))
        except BaseException as e:   # handed to the job thread
            q.put(e)
    threading.Thread(target=reader_thread, daemon=True, name="kokoro").start()

    pieces, made, t_tts, t_rvc = [], 0.0, 0.0, 0.0
    try:
        for i, s in enumerate(sents):
            while True:
                if job.cancel:
                    raise Cancelled()
                try:
                    item = q.get(timeout=0.5)
                    break
                except queue.Empty:
                    continue
            if isinstance(item, BaseException):
                raise item
            a, dt = item
            t_tts += dt
            t = time.perf_counter()
            if len(a) < tts.SR // 20:   # nothing readable in it (just symbols)
                y = np.zeros(0, np.float32)
            else:
                a16 = resample_poly(a, 2, 3).astype(np.float32)   # 24 kHz -> 16 kHz
                y, _ = conv.convert(a16, lv, settings)
                y = _level(np.nan_to_num(y))
            t_rvc += time.perf_counter() - t
            gap = np.zeros(int(sr * s["pause"]), np.float32)
            y = np.concatenate([y, gap])
            f = pdir / f"s{i:04d}.wav"
            sf.write(str(f), y, sr, subtype="PCM_16")
            pieces.append(y)
            made += len(y) / sr
            el = time.perf_counter() - t0
            left = el / (i + 1) * (len(sents) - i - 1)
            job.push(stage="speaking", frac=round((i + 1) / len(sents), 3), index=i, of=len(sents), url=file_url(f),
                     seconds=round(len(y) / sr, 2), made=round(made, 1), left=round(left, 1),
                     message=f"Sentence {i + 1} of {len(sents)} ready")
    except BaseException:
        stop.set()
        shutil.rmtree(pdir, ignore_errors=True)
        raise
    job.push(stage="saving", frac=1.0, message="Saving")
    item = _save(np.concatenate(pieces) if pieces else np.zeros(sr, np.float32), sr, text, p,
                 {"total": round(time.perf_counter() - t0, 2), "reader": round(t_tts, 2), "voice": round(t_rvc, 2)}, job.id)
    return item


def _title(text: str, n: int = 6) -> str:
    words = re.findall(r"[\w'’-]+", text)
    return " ".join(words[:n]) + ("…" if len(words) > n else "") if words else "Speech"


def _safe(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", name).strip().strip(".")[:120]


def _save(y: np.ndarray, sr: int, text: str, p: dict, seconds: dict, job_id: str) -> dict:
    d = speech_dir()
    sid = uuid.uuid4().hex[:10]
    title = _title(text)
    stem = _safe(f"{title.rstrip('…')} ({p['voice']})") or sid
    base = stem
    for k in range(2, 1000):
        if not (d / f"{stem}.wav").exists() and not (d / f"{stem}.mp3").exists():
            break
        stem = f"{base} {k}"
    wav = d / f"{stem}.wav"
    sf.write(str(wav), y, sr, subtype="PCM_16")
    mp3 = _mp3(wav)
    meta = {"id": sid, "title": title, "file": wav.name, "mp3": mp3.name if mp3 else None, "text": text,
            "voice": p["voice"], "reader": p["reader"], "reader_auto": p["auto"], "speed": p["speed"], "pitch": p["pitch"],
            "created": time.strftime("%Y-%m-%d %H:%M:%S"), "duration": round(len(y) / sr, 2), "sr": sr, "seconds": seconds,
            "job": job_id}
    fsutil.write_text(d / f"{sid}.json", json.dumps(meta, indent=1))
    return _item(meta)


def _mp3(wav: Path) -> Path | None:
    exe = shutil.which("ffmpeg")
    if not exe:
        return None
    out = wav.with_suffix(".mp3")
    r = subprocess.run([exe, "-hide_banner", "-nostdin", "-y", "-i", str(wav), "-c:a", "libmp3lame", "-b:a", "192k", str(out)],
                       capture_output=True, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return out if r.returncode == 0 and out.exists() else None


# ---------------------------------------------------------------- the saved results (SpeakEasy's Recent, SafeHouse)

def _item(m: dict) -> dict:
    d = speech_dir()
    wav, mp3 = d / m["file"], d / m["mp3"] if m.get("mp3") else None
    size = sum(f.stat().st_size for f in (wav, mp3) if f and f.exists())
    return {**{k: m.get(k) for k in ("id", "title", "text", "voice", "reader", "reader_auto", "speed", "pitch", "created",
                                      "duration", "seconds", "file", "mp3")},
            "url": file_url(wav), "mp3_url": file_url(mp3) if mp3 and mp3.exists() else None, "size": size,
            "format": "WAV" + (" + MP3" if mp3 and mp3.exists() else "")}


def _meta(sid: str) -> tuple[Path, dict]:
    if not re.fullmatch(r"[0-9a-f]{10}", sid or ""):
        raise LookupError("That speech isn't there any more.")
    f = speech_dir() / f"{sid}.json"
    try:
        return f, json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise LookupError("That speech isn't there any more.")


def list_speech() -> list[dict]:
    out = []
    for f in speech_dir().glob("*.json"):
        try:
            m = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (speech_dir() / m.get("file", "")).exists():
            out.append(_item(m))
    out.sort(key=lambda x: x["created"] or "", reverse=True)
    return out


def get_speech(sid: str) -> dict:
    return _item(_meta(sid)[1])


def delete_speech(sid: str):
    f, m = _meta(sid)
    for name in (m.get("file"), m.get("mp3")):
        if name:
            fsutil.retry(lambda name=name: (speech_dir() / name).unlink(missing_ok=True))
    f.unlink(missing_ok=True)
    if re.fullmatch(r"\w+", m.get("job") or ""):
        shutil.rmtree(parts_dir(m["job"]), ignore_errors=True)   # its sentences, kept for the page's player


def rename_speech(sid: str, name: str) -> dict:
    f, m = _meta(sid)
    name = _safe(name or "")
    if not name:
        raise SpeakError("Type a name.")
    d = speech_dir()
    moves = [(d / m[k], d / (name + Path(m[k]).suffix)) for k in ("file", "mp3") if m.get(k)]
    for old, new in moves:
        if new.exists() and new != old:
            raise SpeakError(f"There's already a file called \"{new.name}\".")
    for old, new in moves:
        if old.exists():
            fsutil.retry(lambda old=old, new=new: old.rename(new))
    m["file"] = name + ".wav"
    if m.get("mp3"):
        m["mp3"] = name + ".mp3"
    m["title"] = name
    fsutil.write_text(f, json.dumps(m, indent=1))
    return _item(m)


def storage() -> int:
    return sum(f.stat().st_size for f in speech_dir().iterdir() if f.is_file())
