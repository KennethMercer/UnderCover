"""Is the lead stem actually singing? (phase 15, before the conversion)

"Toxic as SpongeBob": in 6 of the 13 windows where the lead stem was active the "vocal" was a pitched instrument (the
song's string hook: spectral flatness 0.001-0.03 against 0.09-0.42 for singing, nearly always pitched, no words), and
RVC turned it into singing that isn't there. And from 56 s on the karaoke split put the choruses in the backing stem
(at the lead's level, flatness 0.2-0.4) while the lead stem stayed silent, so those lines were never converted: the
cover kept the original singer there. So, per 2 s window (0.5 s steps):

  instrument   the lead stem is active but tonal (flatness < FLAT_INSTRUMENT): not converted; it goes back into the
               instrumental as it was, so the hook stays in the song
  chorus       the backing stem is singing (flatness >= FLAT_SINGING) at about the lead's level (within BACK_DB) while
               the lead stem is silent or an instrument: moved into the lead's conversion, out of the backing

Plain numpy on the separated stems; the plan is in seconds, applied to the stems at their own rate with short fades.
"""
from pathlib import Path

import numpy as np

SR = 16000
WIN, HOP = 2.0, 0.5
# Calibrated on all 19 cached separations (2026-10-12): Toxic's string hook is 0.001-0.004, but a deep sustained real
# vocal goes down to ~0.006-0.013 (Whisper heard lyrics there), and one song's whole lead sits under 0.05. So only a
# near-pure tone counts, and only in a song that sings normally elsewhere: leaving a hook to be "sung" is the lesser
# harm than muting a real line.
FLAT_INSTRUMENT = 0.005
SONG_SINGS = 0.15        # a clearly sung window (Toxic's singing 0.2-0.4)
SUNG_SHARE = 0.2         # the gate acts only when at least this share of the lead's active windows is clearly sung
FLAT_SINGING = 0.10
ACTIVE_DB = 30.0         # a frame counts as sound within 30 dB of the lead's loud level
BACK_DB = 12.0           # a backing "chorus" peaks within 12 dB of the lead's loud level (Toxic's median: -4..+1 dB)
MIN_RUN = 1.5            # seconds: shorter runs aren't acted on
FADE = 0.03


def _db(x: np.ndarray, hop: int = 160) -> np.ndarray:
    m = len(x) // hop
    return 10 * np.log10((x[:m * hop].reshape(m, hop) ** 2).mean(1) + 1e-12)


def flatness(x: np.ndarray) -> float:
    """Median spectral flatness of the loud 32 ms frames (as project.noisiness): ~0.2-0.4 for a voice, near 0 for a
    pure tone."""
    fr = x[: len(x) // 512 * 512].reshape(-1, 512)
    if not len(fr):
        return 1.0
    e = (fr ** 2).mean(1)
    loud = e > e.max() * 0.01
    s = np.abs(np.fft.rfft(fr[loud] * np.hanning(512), axis=1)) + 1e-9
    return float(np.median(np.exp(np.log(s).mean(1)) / s.mean(1)))


def _runs(flags: list[bool], min_len: int) -> list[tuple[int, int]]:
    out, start = [], None
    for i, f in enumerate(flags + [False]):
        if f and start is None:
            start = i
        elif not f and start is not None:
            if i - start >= min_len:
                out.append((start, i))
            start = None
    return out


def plan(lead: np.ndarray, backing: np.ndarray | None = None) -> dict:
    """lead / backing: 16 kHz mono. -> {"instrument": [(t0, t1)], "chorus": [(t0, t1)]} in seconds (merged runs)."""
    n = len(lead) if backing is None else min(len(lead), len(backing))
    lead = lead[:n]
    ld = _db(lead)
    ref = float(np.percentile(ld, 95)) if len(ld) else -100.0
    la = ld > ref - ACTIVE_DB
    bd = _db(backing[:n]) if backing is not None else None
    steps = max(0, int((n / SR - WIN) / HOP) + 1)
    inst, chorus = [], []
    flats = []
    for k in range(steps):
        a, b = int(k * HOP * 100), int((k * HOP + WIN) * 100)
        s0, s1 = int(k * HOP * SR), int((k * HOP + WIN) * SR)
        lact = float(la[a:b].mean()) if b <= len(la) else 0.0
        fl = flatness(lead[s0:s1]) if lact > 0.3 else None
        if fl is not None:
            flats.append(fl)
        is_inst = fl is not None and fl < FLAT_INSTRUMENT
        inst.append(is_inst)
        ch = False
        if bd is not None and (lact < 0.3 or is_inst) and b <= len(bd):
            bact = float((bd[a:b] > ref - ACTIVE_DB).mean())
            if bact > 0.5 and float(np.percentile(bd[a:b], 90)) > ref - BACK_DB and flatness(backing[s0:s1]) >= FLAT_SINGING:
                ch = True
        chorus.append(ch)
    if not flats or float(np.mean(np.asarray(flats) >= SONG_SINGS)) < SUNG_SHARE:
        inst = [False] * len(inst)   # a lead that's tonal throughout (heavy processing, a deep sustained voice): leave it be
    need = int(MIN_RUN / HOP)
    # a run of steps -> the middle of its windows (each window's edges overlap its neighbours, singing or not)
    to_s = lambda r: (round(r[0] * HOP + (WIN - HOP) / 2, 2), round((r[1] - 1) * HOP + (WIN + HOP) / 2, 2))
    return {"instrument": _merge([to_s(r) for r in _runs(inst, need)]),
            "chorus": _merge([to_s(r) for r in _runs(chorus, need)])}


def _merge(ranges: list[tuple[float, float]], gap: float = HOP) -> list[tuple[float, float]]:
    """Overlapping or nearly touching ranges as one."""
    out: list = []
    for a, b in sorted(ranges):
        if out and a <= out[-1][1] + gap:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def _mask(ranges, n: int, sr: int) -> np.ndarray:
    """1 inside the ranges, 0 outside, with short linear fades."""
    m = np.zeros(n, np.float32)
    f = max(1, int(FADE * sr))
    for t0, t1 in ranges:
        a, b = max(0, int(t0 * sr)), min(n, int(t1 * sr))
        if b <= a:
            continue
        m[a:b] = 1.0
        m[max(0, a - f):a] = np.maximum(m[max(0, a - f):a], np.linspace(0, 1, a - max(0, a - f), dtype=np.float32))
        m[b:min(n, b + f)] = np.maximum(m[b:min(n, b + f)], np.linspace(1, 0, min(n, b + f) - b, dtype=np.float32))
    return m


CHORUS_MODES = ("convert", "top", "keep")
# What happens to lines the karaoke split put in the backing stem while the lead is silent (phase 22): "convert" moves
# all of it into the conversion (phase 15; on Toxic's stacked choruses the user heard it as "atrocious": RVC follows
# one pitch), "top" splits the backing again with the karaoke model and converts only its top voice, the harmonies
# under it staying as they were, "keep" leaves the choruses to the original singers.


def split_top(backing: Path, separator, cache: Path) -> dict:
    """The backing stem split again by the karaoke model: {"top": its main voice, "rest": the layers under it}."""
    from ..core.separate import QUALITY, SEP
    k = separator.run(Path(backing), SEP["karaoke_model"], cache / ("top_" + Path(SEP["karaoke_model"]).stem), QUALITY["high"])
    return {"top": k["vocals"], "rest": k["instrumental"]}


def apply(stems: dict, gate: dict, work: Path, stamp: str, chorus: str = "convert", top: dict | None = None) -> dict:
    """New stem files for the conversion and the mix: the lead without the instrument parts and with the choruses
    from the backing (chorus "convert": all of it; "top": only `top["top"]`, the layers under it stay in the backing;
    "keep": none); the backing without what moved; the instrumental with the instrument parts back in.
    Returns a copy of `stems` with the changed paths (the originals stay as they were)."""
    import soundfile as sf
    if chorus == "keep" or (chorus == "top" and not top):
        gate = {**gate, "chorus": [] if chorus == "keep" else gate["chorus"]}
        chorus = "convert" if chorus == "top" else chorus
    if not gate["instrument"] and not gate["chorus"]:
        return stems
    lead, sr = sf.read(str(stems["vocals"]), dtype="float32", always_2d=True)
    inst, sr_i = sf.read(str(stems["instrumental"]), dtype="float32", always_2d=True)
    back = None
    if stems.get("backing") and gate["chorus"]:
        back, sr_b = sf.read(str(stems["backing"]), dtype="float32", always_2d=True)
        assert sr_b == sr, (sr_b, sr)
    assert sr_i == sr, (sr_i, sr)
    n = len(lead)
    mi = _mask(gate["instrument"], n, sr)[:, None]
    out = dict(stems)
    new_lead = lead * (1 - mi)
    if gate["instrument"]:
        m = min(len(inst), n)
        inst2 = inst.copy()
        inst2[:m] += lead[:m] * mi[:m] if lead.shape[1] == inst.shape[1] else lead[:m].mean(1, keepdims=True) * mi[:m]
        p = work / f"gate_instrumental_{stamp}.flac"
        sf.write(str(p), np.clip(inst2, -1, 1), sr, subtype="PCM_24")
        out["instrumental"] = p
    if back is not None:
        m = min(len(back), n)
        mc = _mask(gate["chorus"], m, sr)[:, None]
        fit = lambda x: x if x.shape[1] == lead.shape[1] else np.repeat(x.mean(1, keepdims=True), lead.shape[1], 1)
        back2 = back.copy()
        if chorus == "top":
            tv, sr_t = sf.read(str(top["top"]), dtype="float32", always_2d=True)
            rest, sr_r = sf.read(str(top["rest"]), dtype="float32", always_2d=True)
            assert sr_t == sr and sr_r == sr, (sr_t, sr_r, sr)
            m = min(m, len(tv), len(rest))
            mc = mc[:m]
            new_lead[:m] += fit(tv[:m]) * mc
            rest = rest if rest.shape[1] == back.shape[1] else np.repeat(rest.mean(1, keepdims=True), back.shape[1], 1)
            back2[:m] = back[:m] * (1 - mc) + rest[:m] * mc   # the harmonies stay as they were
        else:
            new_lead[:m] += fit(back[:m]) * mc
            back2[:m] *= (1 - mc)
        p = work / f"gate_backing_{stamp}.flac"
        sf.write(str(p), np.clip(back2, -1, 1), sr, subtype="PCM_24")
        out["backing"] = p
    p = work / f"gate_lead_{stamp}.flac"
    sf.write(str(p), np.clip(new_lead, -1, 1), sr, subtype="PCM_24")
    out["vocals"] = p
    return out


def note(gate: dict, chorus: str = "convert") -> list[str]:
    """Plain sentences for the cover's notes."""
    out = []
    span = lambda rs: ", ".join(f"{int(a) // 60}:{int(a) % 60:02d}-{int(b) // 60}:{int(b) % 60:02d}" for a, b in rs[:4]) + (" ..." if len(rs) > 4 else "")
    if gate["instrument"]:
        out.append(f"Parts of the lead track were an instrument, not singing ({span(gate['instrument'])}): left as they are, "
                   "not turned into singing.")
    if gate["chorus"] and chorus == "top":
        out.append(f"Some lines were in the backing track ({span(gate['chorus'])}): their top voice was converted too, "
                   "the harmonies under it kept as they were.")
    elif gate["chorus"] and chorus != "keep":
        out.append(f"Some lines were in the backing track ({span(gate['chorus'])}): converted with the voice too.")
    return out
