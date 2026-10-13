"""Fit a song's melody to a voice's range: octave (vocal only) and key (whole song).

Used by the one-shot and by the cover pipeline's "Fit pitch to the voice" option.
"""
import math

import numpy as np

MAX_KEY = 7          # whole-song transposition allowed (semitones each way): the ear takes the band moved to where the
                     # voice is comfortable over a voice out of its register (phase 22)
KEY_COST = 0.002     # per semitone of key change (the band is transposed too): breaks ties toward the original key
OCTAVE_COST = 0.001  # per octave the vocal alone moves (cheaper than moving the band: same pitch, the band moved less)
LOW_WEIGHT = 1.5     # a note under the voice's range (mumbled, growly) is worse than one above it (airier)
REGISTER_COST = 0.02  # per semitone the melody's median sits from where the voice is comfortable
SPEECH_LIFT = 4.0    # a speech-trained voice sings comfortably ~4 st above its speaking median (phase 22, by ear:
                     # Agent Smoke's "Hold On" wanted 4-6 st down from the original, between +10 (too high) and 0 (too
                     # low) against his median; SpongeBob's "Pink Pony Club" between +8.7 (a tad high) and -3.3 (low))
CLOSE = 0.03         # two registers this close in cost: both are worth hearing
ALT_APART = 5        # ...when they're at least this many semitones apart (not the same pitch reached another way)


def pick_fit(f0_range: list, from_speech: bool, song_f0: np.ndarray, max_key: int = MAX_KEY) -> dict:
    """Octave (vocal only) + key (whole song) that put the melody where the voice is comfortable: its median at the
    voice's own register (the training median, +SPEECH_LIFT for a voice trained on speech) and as much of it as
    possible inside the voice's range (5th-95th percentile, -2 / +3 semitones of slack, +7 more for speech).
    Phase 22: the fit used to think in octaves with small key moves; the user's ear wants the whole song moved to the
    voice's register, even half an octave down. Notes below the range count 1.5x; each semitone of key costs a
    little (ties keep the original key). -> {octave, key, outside, below, above, cost, register_st, alt?}: register_st
    is the melody's median after the shift against the voice's comfortable pitch; `alt` is the best with another
    octave when it's within CLOSE (the page can offer both as a quick blind pick)."""
    lo, mid, hi = f0_range
    lo, hi = lo * 2 ** (-2 / 12), hi * 2 ** ((3 + (7 if from_speech else 0)) / 12)
    f = song_f0[song_f0 > 0]
    if len(f) < 50:
        return {"octave": 0, "key": 0, "outside": 0.0, "below": 0.0, "above": 0.0, "cost": 0.0, "register_st": None}
    med = float(np.median(f))
    comfy = mid * 2 ** ((SPEECH_LIFT if from_speech else 0.0) / 12) if mid else None
    per_octave = {}
    for octave in (0, -1, 1, -2, 2):
        best = None
        for key in sorted(range(-max_key, max_key + 1), key=abs):   # nearest the original first wins ties
            shift = octave + key / 12
            g = f * 2.0 ** shift
            below, above = float(np.mean(g < lo)), float(np.mean(g > hi))
            reg = 12 * math.log2(med * 2.0 ** shift / comfy) if comfy else 0.0
            cost = (LOW_WEIGHT * below + above + KEY_COST * abs(key) + OCTAVE_COST * abs(octave)
                    + REGISTER_COST * abs(reg))
            if best is None or cost < best["cost"] - 1e-9:
                best = {"octave": octave, "key": key, "outside": round(below + above, 3), "below": round(below, 3),
                        "above": round(above, 3), "cost": round(cost, 4), "register_st": round(reg, 1)}
        per_octave[octave] = best
    ranked = sorted(per_octave.values(), key=lambda x: x["cost"])
    out = dict(ranked[0])
    net = lambda x: 12 * x["octave"] + x["key"]
    other = next((x for x in ranked[1:] if abs(net(x) - net(out)) >= ALT_APART), None)
    if other and other["cost"] - out["cost"] <= CLOSE:
        out["alt"] = dict(other)
    return out


def pick_octave(voice_hz: float | None, from_speech: bool, song_hz: float | None) -> int:
    """Whole octaves that bring the song's melody nearest the voice's natural range. People sing roughly
    half an octave above their speaking pitch, so speech-trained voices are nudged up by that."""
    if not voice_hz or not song_hz:
        return 0
    target = voice_hz * (2 ** 0.5 if from_speech else 1.0)
    return int(np.clip(round(math.log2(target / song_hz)), -2, 2))
