"""How a reference clip *sounds* (phase 9): the clip's own sound (room, mic, hiss, a muffled band) carries into the
cloning engine, and likeness can't hear it (phase 4: Freeman's v2 clip scored higher and sounded worse: muffled).

Plain numpy / scipy, a few milliseconds per clip. Four measures, each from the clip's frames where the person talks:
  hf_db      energy above 4 kHz relative to 0.3-4 kHz (dB): low = muffled (a dull mic, a lowpassed upload)
  edge_hz    where the long-term spectrum falls 50 dB under its speech band: low = bandlimited (phone, old codec)
  floor_db   the quietest frames under the loudest (dB, negative): near 0 = hiss or a noisy room under the voice
  clip_frac  share of samples stuck at the peak (hard clipping); distortion beyond that isn't measured
  decay_db_s how fast the sound dies after the voice stops (dB/s, the steep end): slow = a reverberant room

`penalty(m)` turns them into one number in score units (0 = clean, larger = worse), calibrated in
the maintainers' notes, clip-sound on clips the user judged and on degraded copies.
"""
import math
from pathlib import Path

import numpy as np

# Calibration (the maintainers' notes, clip-sound): where a clean clip of a person talking sits, as UnderCover's clips come
# (the speaking projects' 103 reference candidates: hf_db median -16.5, floor_db -34.6, decay_db_s -304)
HF_OK = -22.0        # hf_db at or above this is not muffled (Freeman's approved clip -14.9, the rejected v2 -26.5)
EDGE_OK = 9000.0     # Hz: a full-band clip reaches well past this (one Obama source stops at 5.4 kHz)
FLOOR_OK = -30.0     # dB: a band's quiet moments this far under its average = no steady noise under the voice
DECAY_OK = -240.0    # dB/s: dry speech decays at least this steeply (95% of the candidates do)
CLIP_OK = 2e-4       # share of samples at the peak that natural speech can have (seen: at most 1.4e-5)
# penalty per unit past the threshold, in score units (likeness differences between candidates are 0.01-0.08):
# Freeman's v2 clip (4.5 dB under HF_OK) costs 0.054, noise at 25 dB SNR ~0.03, a 0.4 s room ~0.03
W = {"hf_db": 0.012, "edge_hz": 0.010 / 1000, "floor_db": 0.006, "decay_db_s": 0.0004, "clip_frac": 20.0}
CAP = 0.15           # one bad measure can't outweigh everything else


def _mono(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, np.float32)
    return x.mean(1) if x.ndim == 2 else x


def measure(x: np.ndarray, sr: int) -> dict:
    """The clip's sound measures (see the module's header). x: float audio, mono or (n, channels)."""
    from scipy.signal import stft
    x = _mono(x)
    x = x - float(np.mean(x))
    out = {}
    peak = float(np.abs(x).max()) if len(x) else 0.0
    if peak < 1e-4 or len(x) < sr // 2:
        return {"ok": False}
    # clipping: samples stuck at the peak (natural speech touches it once or twice)
    out["clip_frac"] = float(np.mean(np.abs(x) >= 0.999 * peak))
    # 10 ms frame levels
    hop = max(1, sr // 100)
    n = len(x) // hop
    fr = x[:n * hop].reshape(n, hop)
    db = 10 * np.log10((fr ** 2).mean(1) + 1e-12)
    top = float(np.percentile(db, 95))
    # decay after the voice stops: slope of the level over 60 ms windows, where it falls from speech level
    k = 6
    if n > 3 * k:
        t = np.arange(k) / 100
        tc = t - t.mean()
        win = np.lib.stride_tricks.sliding_window_view(db, k)
        slopes = (win - win.mean(1, keepdims=True)) @ tc / float(tc @ tc)
        start = win[:, 0]
        fall = slopes[(start > top - 25) & (slopes < 0)]
        out["decay_db_s"] = float(np.percentile(fall, 10)) if len(fall) >= 5 else None
    # the long-term spectrum where the person talks
    nfft = 1 << int(math.ceil(math.log2(sr * 0.046)))
    f, _, Z = stft(x, sr, nperseg=nfft, noverlap=nfft * 3 // 4, boundary=None, padded=False)
    P = np.abs(Z) ** 2
    lv = 10 * np.log10(P.sum(0) + 1e-12)
    act = lv > np.percentile(lv, 95) - 30
    if act.sum() < 5:
        return {"ok": False, **out}
    S = P[:, act].mean(1)
    band = (f >= 300) & (f < 4000)
    hi = (f >= 4000) & (f < min(8000, sr / 2))
    out["hf_db"] = float(10 * np.log10(S[hi].sum() / (S[band].sum() + 1e-20) + 1e-12)) if hi.any() else None
    # the noise floor: each band's quietest moments (speech leaves every band now and then; a steady hiss or room
    # noise never does) under that band's average, over 0.5-8 kHz
    nb = (f >= 500) & (f < min(8000, sr / 2))
    low = np.percentile(P[nb], 10, axis=1)
    out["floor_db"] = float(np.mean(10 * np.log10((low + 1e-20) / (P[nb].mean(1) + 1e-20))))
    Sd = 10 * np.log10(np.convolve(S, np.ones(9) / 9, "same") + 1e-20)
    ref = float(Sd[band].max())
    above = np.where((Sd >= ref - 50) & (f >= 300))[0]
    out["edge_hz"] = float(f[above[-1]]) if len(above) else 0.0
    out["ok"] = True
    return {k: (round(v, 6) if k == "clip_frac" else round(v, 2)) if isinstance(v, float) else v for k, v in out.items()}


def measure_file(p: Path) -> dict:
    import soundfile as sf
    x, sr = sf.read(str(p), dtype="float32", always_2d=True)
    return measure(x, sr)


def parts(m: dict) -> dict:
    """Each measure's share of the penalty (score units)."""
    if not m.get("ok"):
        return {}
    out = {}
    if m.get("hf_db") is not None:
        out["hf_db"] = max(0.0, HF_OK - m["hf_db"]) * W["hf_db"]
    if m.get("edge_hz") is not None:
        out["edge_hz"] = max(0.0, EDGE_OK - m["edge_hz"]) * W["edge_hz"]
    out["floor_db"] = max(0.0, m["floor_db"] - FLOOR_OK) * W["floor_db"]
    if m.get("decay_db_s") is not None:
        out["decay_db_s"] = max(0.0, m["decay_db_s"] - DECAY_OK) * W["decay_db_s"]
    out["clip_frac"] = max(0.0, m["clip_frac"] - CLIP_OK) * W["clip_frac"]
    return {k: round(min(CAP, v), 4) for k, v in out.items()}


def penalty(m: dict) -> float:
    """One number in score units: 0 = the clip sounds clean; subtracted from a candidate's score."""
    return round(min(2 * CAP, sum(parts(m).values())), 4)
