"""One sentence with a voice's cadence: the engine reads it (worker), then the voice's RVC model makes it the person's
exact timbre (in the server, the shared converter). Used by SpeakEasy (reading) and DoubleAgent (measuring a
cadence before keeping it), so both hear exactly the same thing.

pack = the voice's extra.tts (written by DoubleAgent): engine, ref (file in the voice's tts/ folder), ref_text,
cfg_weight, exaggeration, tempo (1.0 = as the engine reads; < 1 slows it toward the person's measured rate).
"""
import math
import uuid
from pathlib import Path

import numpy as np
import soundfile as sf

from ..config import path
from . import client


def scratch() -> Path:
    d = path("cache_dir") / "cadence"
    d.mkdir(parents=True, exist_ok=True)
    return d


def request(text: str, pack: dict, ref: Path, seed: int = 1234, progress=None):
    """Ask the worker for the engine's reading (doesn't wait): -> (future, wav path)."""
    out = scratch() / f"{uuid.uuid4().hex[:12]}.wav"
    opts = {k: pack[k] for k in ("cfg_weight", "exaggeration") if pack.get(k) is not None}
    fut = client.speak(text, ref, out, pack.get("engine", "chatterbox"), ref_text=pack.get("ref_text", ""), seed=seed,
                       wait_for=False, progress=progress, **opts)
    return fut, out


def load16(f: Path) -> np.ndarray:
    from scipy.signal import resample_poly
    x, sr = sf.read(str(f), dtype="float32", always_2d=True)
    x = x.mean(1)
    if sr != 16000:
        g = math.gcd(sr, 16000)
        x = resample_poly(x, 16000 // g, sr // g).astype(np.float32)
    return x


def trim(x: np.ndarray, sr: int = 16000, db: float = -45.0, keep: float = 0.05) -> np.ndarray:
    """Leading/trailing silence off (the sentence gap is added by the reader, from the person's own gaps)."""
    if not len(x):
        return x
    lvl = 10 ** (db / 20) * max(1e-4, float(np.abs(x).max()))
    on = np.where(np.abs(x) > lvl)[0]
    if not len(on):
        return x[:0]
    k = int(sr * keep)
    return x[max(0, on[0] - k):on[-1] + k]


def stretch(x: np.ndarray, tempo: float) -> np.ndarray:
    """Slow down (tempo < 1) or speed up without changing pitch, before RVC (which re-synthesises the voice, so the
    stretch's own artefacts mostly go). 1.0 = unchanged."""
    if not tempo or abs(tempo - 1.0) < 0.01 or len(x) < 2048:
        return x
    import librosa
    return librosa.effects.time_stretch(x, rate=float(tempo)).astype(np.float32)


def octave_shift(x16: np.ndarray, rmvpe, target_hz: float | None) -> float:
    """The engine already speaks at the person's pitch: only a whole-octave correction (normally 0)."""
    if not target_hz:
        return 0.0
    f0 = rmvpe.infer_from_audio(x16, thred=0.03)
    f0 = f0[f0 > 0]
    if len(f0) <= 20:
        return 0.0
    return float(12 * round(math.log2(target_hz / float(np.median(f0)))))


def finish(wav: Path, lv, conv, target_hz: float | None, tempo: float = 1.0, nudge: float = 0.0,
           keep_wav: bool = False) -> tuple[np.ndarray, int]:
    """The engine's WAV -> RVC in the voice -> float32 audio at the voice's rate (not levelled)."""
    from ..engine.convert import Settings
    try:
        x = trim(load16(wav))
    finally:
        if not keep_wav:
            Path(wav).unlink(missing_ok=True)
    if len(x) < 1600:   # nothing readable in it
        return np.zeros(0, np.float32), lv.tgt_sr
    x = stretch(x, tempo)
    shift = octave_shift(x, conv.rmvpe, target_hz) + float(nudge or 0)
    y, sr = conv.convert(x, lv, Settings(pitch=shift))
    return np.nan_to_num(y).astype(np.float32), sr
