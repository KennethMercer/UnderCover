"""Pitch-track cleanup and optional pitch correction for RVC conversion.

RMVPE is accurate but on breathy/whispered/doubled vocals it sometimes jumps an octave for a few frames
or emits 10-20 ms voiced blips. Each becomes an audible crack in the converted voice. All functions work
on F0 in Hz at 100 frames/s, where 0 means unvoiced.
"""
import numpy as np

MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])  # Krumhansl-Kessler
MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])
SCALES = {"major": [0, 2, 4, 5, 7, 9, 11], "minor": [0, 2, 3, 5, 7, 8, 10]}
NOTE_NAMES = "C C# D D# E F F# G G# A A# B".split()


def _midi(f0):
    return 12 * np.log2(np.maximum(f0, 1e-6) / 440.0) + 69


def _hz(m):
    return 440.0 * 2 ** ((m - 69) / 12)


def _segments(voiced: np.ndarray):
    """(start, end) index pairs of contiguous True runs."""
    edges = np.diff(np.concatenate([[0], voiced.astype(np.int8), [0]]))
    return list(zip(np.where(edges == 1)[0], np.where(edges == -1)[0]))


def _rolling_median(x: np.ndarray, win: int) -> np.ndarray:
    h = win // 2
    xp = np.pad(x, h, mode="edge")
    return np.median(np.lib.stride_tricks.sliding_window_view(xp, win), axis=1)


def clean_f0(f0: np.ndarray, min_voiced: int = 3, max_gap: int = 3, octave_win: int = 31) -> np.ndarray:
    """Repair octave errors, drop tiny voiced blips, fill tiny unvoiced gaps, light median smoothing."""
    f0 = f0.astype(np.float64).copy()
    voiced = f0 > 0
    # 1. tiny voiced islands (< min_voiced frames) are almost always noise/breath -> unvoiced
    for s, e in _segments(voiced):
        if e - s < min_voiced:
            f0[s:e] = 0
    voiced = f0 > 0
    # 2. tiny unvoiced gaps inside singing (< max_gap frames) -> interpolate (avoids clicks mid-note)
    for s, e in _segments(~voiced):
        if 0 < s and e < len(f0) and e - s <= max_gap:
            a, b = f0[s - 1], f0[e]
            f0[s:e] = np.exp(np.linspace(np.log(a), np.log(b), e - s + 2)[1:-1])
    voiced = f0 > 0
    # 3. octave errors: frames ~12 semitones away from the local median of their segment get folded back
    for s, e in _segments(voiced):
        m = _midi(f0[s:e])
        if e - s >= 5:
            for _ in range(2):
                med = _rolling_median(m, min(octave_win, (e - s) | 1))
                d = m - med
                m = np.where(np.abs(d - 12) < 3, m - 12, np.where(np.abs(d + 12) < 3, m + 12, m))
            # 4. light smoothing (3 frames) removes single-frame jitter but keeps vibrato (5-7 Hz = 14+ frames)
            m = _rolling_median(m, 3)
        f0[s:e] = _hz(m)
    return f0


def count_glitches(f0: np.ndarray) -> dict:
    """Octave-sized frame-to-frame jumps and tiny voiced islands (for measuring)."""
    v = f0 > 0
    m = np.where(v, _midi(f0), np.nan)
    d = np.abs(np.diff(m))
    segs = _segments(v)
    return {"octave_jumps": int(np.nansum((d > 9) & (d < 15))),
            "tiny_islands": int(sum(1 for s, e in segs if e - s < 3)), "segments": len(segs)}


def detect_key(audio: np.ndarray, sr: int) -> tuple[int, str, float]:
    """(tonic pitch class 0-11, 'major'|'minor', confidence 0-1) via Krumhansl-Schmuckler on chroma."""
    import librosa
    chroma = librosa.feature.chroma_cqt(y=audio, sr=sr).mean(axis=1)
    best = (0, "major", -2.0)
    scores = []
    for mode, prof in (("major", MAJOR), ("minor", MINOR)):
        for tonic in range(12):
            r = float(np.corrcoef(chroma, np.roll(prof, tonic))[0, 1])
            scores.append(r)
            if r > best[2]:
                best = (tonic, mode, r)
    s = sorted(scores, reverse=True)
    confidence = max(0.0, min(1.0, (s[0] - s[1]) * 10))  # margin over the runner-up
    return best[0], best[1], round(confidence, 2)


def key_name(tonic: int, mode: str) -> str:
    return f"{NOTE_NAMES[tonic]} {mode}"


def snap_to_key(f0: np.ndarray, tonic: int, mode: str, strength: float) -> np.ndarray:
    """Pull each voiced frame toward the nearest note of the song's scale. strength 0 = off,
    ~0.3-0.5 = gentle correction, 1 = hard autotune (removes vibrato)."""
    if strength <= 0:
        return f0
    out = f0.copy()
    v = f0 > 0
    m = _midi(f0[v])
    allowed = np.array([(tonic + d) % 12 for d in SCALES[mode]])
    base = np.floor(m / 12) * 12
    cands = (base[:, None] + allowed[None, :] + np.array([-12, 0, 12])[:, None, None]).reshape(3, len(m), -1)
    cands = np.concatenate(list(cands), axis=1)                      # (frames, 3*7) candidate notes
    nearest = cands[np.arange(len(m)), np.argmin(np.abs(cands - m[:, None]), axis=1)]
    out[v] = _hz(m + strength * (nearest - m))
    return out
