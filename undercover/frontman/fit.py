"""Fit a song's melody to a voice's range: octave (vocal only) and key (whole song).

Used by the one-shot and by the cover pipeline's "Fit pitch to the voice" option.
"""
import math

import numpy as np

MAX_KEY = 6          # whole-song transposition allowed when fitting a melody to a voice (semitones each way)
KEY_COST = 0.004     # per semitone of key change: the band gets transposed too, so only when it buys real fit
LOW_WEIGHT = 1.5     # a note under the voice's range (mumbled, growly) is worse than one above it (airier)


def pick_fit(f0_range: list, from_speech: bool, song_f0: np.ndarray, max_key: int = MAX_KEY) -> dict:
    """Octave (vocal only) + key (whole song) that put the melody as fully inside the voice's range as possible.
    Range = the voice's 5th-95th percentile pitch (-2 / +3 semitones of slack, +7 more for speech-trained voices).
    A melody wider than the voice can't fit whole: notes below count 1.5x notes above, and each semitone of
    key change costs a little. Example: Elliott Smith (149-280 Hz) on an acoustic Plug In Baby (165-716 Hz):
    octave -1 alone left the low notes growling at ~82 Hz, octave 0 left most of it straining up high."""
    lo, _, hi = f0_range
    lo, hi = lo * 2 ** (-2 / 12), hi * 2 ** ((3 + (7 if from_speech else 0)) / 12)
    f = song_f0[song_f0 > 0]
    if len(f) < 50:
        return {"octave": 0, "key": 0, "outside": 0.0, "below": 0.0, "above": 0.0, "cost": 0.0}
    best = None
    for octave in (0, -1, 1, -2, 2):
        for key in sorted(range(-max_key, max_key + 1), key=abs):   # nearest the original first wins ties
            g = f * 2.0 ** (octave + key / 12)
            below, above = float(np.mean(g < lo)), float(np.mean(g > hi))
            cost = LOW_WEIGHT * below + above + KEY_COST * abs(key) + 0.001 * abs(octave)
            if best is None or cost < best[0] - 1e-9:
                best = (cost, {"octave": octave, "key": key, "outside": round(below + above, 3),
                               "below": round(below, 3), "above": round(above, 3), "cost": round(cost, 4)})
    return best[1]


def pick_octave(voice_hz: float | None, from_speech: bool, song_hz: float | None) -> int:
    """Whole octaves that bring the song's melody nearest the voice's natural range. People sing roughly
    half an octave above their speaking pitch, so speech-trained voices are nudged up by that."""
    if not voice_hz or not song_hz:
        return 0
    target = voice_hz * (2 ** 0.5 if from_speech else 1.0)
    return int(np.clip(round(math.log2(target / song_hz)), -2, 2))
