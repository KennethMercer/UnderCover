"""One sentence with a voice's cadence: the engine reads it (worker), then the voice's RVC model makes it the person's
exact timbre (in the server, the shared converter). Used by SpeakEasy (reading) and DoubleAgent (measuring a
cadence before keeping it), so both hear exactly the same thing.

pack = the voice's extra.tts (written by DoubleAgent): engine, ref (file in the voice's tts/ folder), ref_text,
cfg_weight, exaggeration, tempo (1.0 = as the engine reads; < 1 slows it toward the person's measured rate).
"""
import math
import re
import uuid
from pathlib import Path

import numpy as np
import soundfile as sf

from ..config import path
from . import client


LEAD_S = 0.3        # silence put before a sentence for the voice conversion, cut off again after (see finish)
SHORT_WORDS = 3     # a sentence with fewer words is read together with its neighbour (see join_short)


def join_short(sents: list, min_words: int = SHORT_WORDS) -> list:
    """[{"text", "pause"}] with each very short sentence read together with the next one in its paragraph (the
    paragraph's last one with the one before). Chatterbox read alone "Listen carefully." as "E. Listen carefully." and
    "Good morning." as "Duh, good morning." (2 of 24 short sentences, against 0 of 40 longer lines; measured
    2026-10-13): a stray sound before a sentence too short to settle into. Never across a paragraph's end."""
    out = [dict(s) for s in sents]
    words = lambda s: len(s["text"].split())
    i = 0
    while i < len(out):
        s = out[i]
        if words(s) < min_words and len(out) > 1:
            para_end = s["pause"] >= 0.5
            if not para_end and i + 1 < len(out):   # with the next one: its pause after both
                nxt = out.pop(i + 1)
                out[i] = {**s, "text": f"{s['text']} {nxt['text']}", "pause": nxt["pause"]}
                continue
            if i > 0 and out[i - 1]["pause"] < 0.5:   # the paragraph's last: with the one before
                prev = out.pop(i - 1)
                out[i - 1] = {**s, "text": f"{prev['text']} {s['text']}", "pause": s["pause"]}
                i -= 1
                continue
        i += 1
    return out


def split_clauses(sents: list, gap: float = 0.12) -> list:
    """Each sentence's comma clauses read as their own pieces (a comma's contour from the engine's own phrase end),
    joined by a short pause: one of the comma treatments the user compares blind ([speakeasy] clause_split)."""
    out = []
    for s in sents:
        parts = [p.strip() for p in re.split(r"(?<=,)\s+", s["text"]) if p.strip()]
        for k, p in enumerate(parts):
            out.append({**s, "text": p, "pause": s["pause"] if k == len(parts) - 1 else gap})
    return out


def scratch() -> Path:
    d = path("cache_dir") / "cadence"
    d.mkdir(parents=True, exist_ok=True)
    return d


def request(text: str, pack: dict, ref: Path, seed: int = 1234, progress=None):
    """Ask the worker for the engine's reading (doesn't wait): -> (future, wav path)."""
    out = scratch() / f"{uuid.uuid4().hex[:12]}.wav"
    opts = {k: pack[k] for k in ("cfg_weight", "exaggeration") if pack.get(k) is not None}
    from .engines import reading_engine
    fut = client.speak(text, ref, out, reading_engine(pack.get("engine", "chatterbox")), ref_text=pack.get("ref_text", ""), seed=seed,
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


def octave_shift(x16: np.ndarray, rmvpe, target_hz: float | None, fine: bool = False, history: list | None = None) -> float:
    """The engine already speaks at the person's pitch: only a whole-octave correction (normally 0). fine (an engine that
    reads lower or higher than the person, Turbo: Obama 9 semitones under his 235 Hz): the median onto the person's, to half
    a semitone, at most an octave either way. history: the offsets of the sentences read so far (this one is added): the
    median of them is used, so a question's higher pitch isn't flattened onto the person's median sentence by sentence."""
    if not target_hz:
        return 0.0
    f0 = rmvpe.infer_from_audio(x16, thred=0.03)
    f0 = f0[f0 > 0]
    if len(f0) <= 20:
        return 0.0
    st = 12 * math.log2(target_hz / float(np.median(f0)))
    if not fine:
        return float(12 * round(st / 12))
    if history is not None:
        history.append(st)
        st = float(np.median(history))
    return float(np.clip(round(st * 2) / 2, -12, 12))


def level(y: np.ndarray, rms_db: float = -20.0, peak: float = 0.89) -> np.ndarray:
    """The same loudness for every sentence (so playback doesn't jump between them), peaks under -1 dBFS, 5 ms fades.
    SpeakEasy levels every sentence with this; DoubleAgent's A/B readings too, so they sound as they would there."""
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


def finish(wav: Path, lv, conv, target_hz: float | None, tempo: float = 1.0, nudge: float = 0.0,
           keep_wav: bool = False, engine: str | None = None, history: list | None = None) -> tuple[np.ndarray, int]:
    """The engine's WAV -> RVC in the voice -> float32 audio at the voice's rate (not levelled)."""
    from ..engine.convert import Settings
    from .engines import ENGINES
    try:
        x = trim(load16(wav))
    finally:
        if not keep_wav:
            Path(wav).unlink(missing_ok=True)
    if len(x) < 1600:   # nothing readable in it
        return np.zeros(0, np.float32), lv.tgt_sr
    x = stretch(x, tempo)
    shift = octave_shift(x, conv.rmvpe, target_hz, fine=bool(ENGINES.get(engine or "", {}).get("pitch_to_target")),
                          history=history) + float(nudge or 0)
    # a lead-in of silence: converted straight from its first 50 ms, a sentence's opening came out garbled ("Good
    # evening." heard as "Grieving.", measured 2026-10-13); with context before it, it comes out as read
    y, sr = conv.convert(np.concatenate([np.zeros(int(16000 * LEAD_S), np.float32), x]), lv, Settings(pitch=shift))
    return np.nan_to_num(y[int(sr * LEAD_S):]).astype(np.float32), sr
