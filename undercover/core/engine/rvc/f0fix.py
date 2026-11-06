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


RIVAL, APART = 0.1, 10   # a second line: this share of the strongest frame's peak, this many bins (20 cents each) off it
NEAR, JUMP = 4, 8.0      # hold_line's path: bins it moves a frame for nothing, and what it pays to go further
GAP = 30                 # frames of quiet (0.3 s) that still join two notes into one sung run


def _second_line(s: np.ndarray) -> np.ndarray:
    """Per frame: the strongest peak more than APART bins from the frame's strongest, as a share of it (0 when there's none)."""
    frames, bins = s.shape
    strongest = s.max(1)
    rest = s.copy()   # (a stem of ten minutes is a matrix of 86 MB here: no wider type, and no mask of the whole of it)
    rest[np.arange(frames)[:, None], np.clip(s.argmax(1)[:, None] + np.arange(-APART, APART + 1), 0, bins - 1)] = 0.0
    return rest.max(1) / np.maximum(strongest, 1e-9)


def hold_line(salience: np.ndarray, thred: float = 0.03) -> np.ndarray:
    """A stack of voices (harmony in a backing stem): RMVPE's strongest line changes every few frames, and one voice sung along
    that track leaps between the stack's notes about ten times a second. Every frame of a sung run (notes joined over quiet of
    up to GAP frames) that has a second line somewhere in it keeps only the line of the path that scores most over the whole
    stretch: where that line rests for a moment while another part sings on, the voice rests too, rather than leap to the other
    part and back. The path stays where it was through quiet, so the next note pays to move. A run with one line all through is
    left exactly as it was. `salience` is RMVPE's (frames, bins)."""
    frames, bins = salience.shape
    s = np.where(salience > thred, salience, 0.0).astype(np.float32)
    at = np.arange(bins)
    strongest = s.max(1)
    share = _second_line(s)
    stacked = np.flatnonzero(share > RIVAL)
    if not len(stacked):
        return salience
    sung = np.flatnonzero(strongest > 0)
    run = np.cumsum(np.r_[True, np.diff(sung) - 1 > GAP])   # (each sung frame's run)
    stacked = sung[np.isin(run, np.unique(run[np.isin(sung, stacked)]))]
    best = s[0].astype(np.float64)
    back = np.zeros((frames, bins), np.int16)
    for t in range(1, frames):
        if strongest[t] == 0:   # quiet: the path stays put
            back[t] = at
            continue
        win = np.lib.stride_tricks.sliding_window_view(np.pad(best, NEAR, constant_values=-np.inf), 2 * NEAR + 1)
        i = win.argmax(1)
        near = win[at, i]
        lead = int(best.argmax())
        far = best[lead] - JUMP
        back[t] = np.where(near >= far, at + i - NEAR, lead)
        best = np.maximum(near, far) + s[t]
        best -= best.max()   # (only the differences count: the sum stays small)
    path = np.zeros(frames, np.int64)
    path[-1] = best.argmax()
    for t in range(frames - 1, 0, -1):
        path[t - 1] = back[t, path[t]]
    out = salience.copy()
    out[stacked] = np.where(np.abs(at[None] - path[stacked, None]) <= NEAR, salience[stacked], 0.0)
    return out


SETTLE, STEP, DIP = 5, 2.0, 6.0   # hold_parts: frames a note settles in, the move (st) that counts, a dip (dB) that starts a new note
LOOK, LAG = 5, 3                 # ...the frames it looks at either side of a move, and how far back it looks (clean_f0's smoothing lag)
GONE, GONE_FRAMES = 0.02, 8      # ...a held part has stopped singing under this salience, this many frames running


def _line(salience: np.ndarray, t: int, m: float) -> float:
    """The salience at MIDI m (within 2 bins) at frame t."""
    k = int(round((1200 * np.log2(440 * 2 ** ((m - 69) / 12) / 10) - 1997.3794084376191) / 20))
    return float(salience[t, max(0, k - 2):min(salience.shape[1], k + 3)].max()) if 0 <= t < len(salience) else 0.0


def _crossing(salience: np.ndarray, t: int, frm: float, to: float, rival: float = 0.05) -> bool:
    """Is the track's move at frame t from MIDI frm to MIDI to a change of part (two parts sounding at once), not a step of the
    tune? The line it moves to was already sounding at a fifth of the one it was on, in 3 of the LOOK frames ending LAG frames
    before the move; and the line it leaves still sounds at a tenth of the new one in 2 of the LOOK frames after."""
    def frames(a, b, m, other, share):
        n = 0
        for i in range(max(0, a), min(len(salience), b)):
            x = _line(salience, i, m)
            n += x >= rival and x >= share * max(_line(salience, i, other), 1e-9)
        return n
    return frames(t - LAG - LOOK, t - LAG, to, frm, 0.2) >= 3 and frames(t + 1, t + 1 + LOOK, frm, to, 0.1) >= 2


def hold_parts(f0: np.ndarray, salience: np.ndarray, level_db: np.ndarray) -> np.ndarray:
    """A backing's held track (after hold_line and clean_f0): inside one sung note (no unvoiced gap, no dip of DIP dB), where
    two parts cross and the track would move from one to the other mid-word, it stays on the part it was on until the note
    ends, or until that part stops singing (under GONE for GONE_FRAMES frames), and then follows the track again. A step of
    the tune, with no second part under it, is left as it was sung. level_db: the audio's level per frame."""
    m = 12 * np.log2(np.maximum(f0, 1e-6) / 440.0) + 69
    m[f0 <= 0] = np.nan
    out = f0.copy()
    voiced = f0 > 0
    t, n = 0, len(f0)
    level_db = np.r_[level_db[:n], np.full(max(0, n - len(level_db)), -120.0)]
    while t < n:
        if not voiced[t]:
            t += 1
            continue
        s = t
        while t < n and voiced[t] and not (t > s + 2 and level_db[t] < np.max(level_db[max(s, t - 10):t]) - DIP):
            t += 1
        held, low = None, 0
        for i in range(s + SETTLE, t):
            ref = float(np.nanmedian(m[max(s, i - SETTLE):i]))
            if held is None and abs(m[i] - ref) > STEP and _crossing(salience, i, ref, m[i]):
                held = float(np.nanmedian(m[max(s, i - 2 * SETTLE):max(s + 1, i - 1)]))   # (where it sat before the move)
                low = 0
            if held is not None:
                low = low + 1 if _line(salience, i, held) < GONE else 0
                if low >= GONE_FRAMES:
                    held = None
                    continue
                out[i] = _hz(held)
    return out


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
