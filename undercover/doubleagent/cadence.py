"""DoubleAgent's cadence work (phase 4): transcripts of a project's clips, the person's measured cadence, and a
voice's cadence pack (the reference clip a cloning engine imitates, with the numbers SpeakEasy reads by).

No training: phase 3 found zero-shot Chatterbox + RVC closest to each person's delivery (the maintainers' notes, cadence),
so "Train cadence" means: transcribe the person's clips (Whisper), rebuild stretches of their real talk and measure
how they speak (rate, pauses, gaps between sentences), then try a few reference clips, read the same test sentences
with each (engine + the voice's RVC) and keep the one that sounds most like them. A new pack replaces the voice's
current one only if it measures better on the same test. Minutes of GPU, not hours.

Where things live:
  cache/training/<project>/transcripts.json   {"model", "clips": {clip id: text}}  (a separate file, so servers that
                                              predate it still read project.json)
  cache/training/<project>/cadence/           stretches r000.wav ... + words.json + person.json (their measured cadence)
  models/voices/<voice>/tts/ref.wav + cadence.json, and meta extra.tts (the pack; ARCHITECTURE "Data contracts")
  models/voices/<voice>/tts/pending/ previous/ older/   packs waiting for the user's ear, and set aside (phase 9, below)

Measures as phase 3 did (tools/research/measure.py): likeness = WeSpeaker cosine to the person's reference
embedding; WER = Whisper's transcript of the result vs the sentence; cadence = per-sentence speaking rate,
articulation rate, pauses, pause length, pitch range / spread / movement / declination; cadence gap = mean distance to
the person's own sentences in units of their spread (0 = theirs).
"""
import json
import math
import re
import shutil
import threading
import time
from pathlib import Path

import numpy as np
import soundfile as sf

from ..core import cadence as C
from ..core import fsutil, gpu, jobs, storage, worklist
from ..core.cadence import render
from ..core.config import CONFIG, path
from ..core.jobs import Cancelled, Paused
from ..core.voices import ModelError, get_voice, label_of, voice_person
from . import clipsound
from . import project as P

VERSION = 2   # 2 (phase 12): the person's pace per source and their median pitch
# The test sentences every candidate reads (from phase 3's ten: long, question, commas, long; no numbers)
TEST = [
    "When I was young, my grandmother used to tell me that hard work was never wasted.",
    "Is that the kind of country we want to leave to our children?",
    "Now, I know some of you are skeptical, and frankly, I understand why.",
    "So let us remember what brought us together, and let us finish the work we started.",
]
# Phase 12: everyday lines (what SpeakEasy mostly reads) for the pace and pitch of a pack's readings. The test sentences
# above are oratory and read slower; the user judges pace on lines like these (the A/B line is the first).
PACE_TEST = [
    "I have always believed that the best stories are the ones we tell each other at the end of a long day.",
    "Well, that is not what I expected, but it might just work.",
    "Come in, sit down, and tell me everything from the beginning.",
    "I called my brother last night, and we talked for hours about nothing in particular.",
    "You know, the funny thing is that nobody ever asked me what I thought.",
    "If you have a minute, I would love to show you what we have been working on.",
]
SEED = 1234
CANDIDATES = 5          # reference clips tried per voice (3 until phase 9b: more room for the clip-sound term)
STRETCHES = 40          # stretches of real talk measured (phase 3 used 35-60)
MIN_MINUTES = 2.0       # transcribed speech needed for a cadence
# score = likeness - W_GAP * cadence gap - W_WER * WER: likeness decides between similar deliveries, a clearly
# closer cadence or a mumbled word outweighs a small likeness difference (phase 3: likeness 0.73-0.83, gap 0.3-0.55)
W_GAP, W_WER = 0.25, 1.0
# No time-stretching of the engine's reading: the user heard it (2026-10-11, Freeman slowed 20% toward his measured
# rate) as "terrible, he sounds drunk". A pack's tempo stays 1.0; SpeakEasy's speed slider still works by hand. This
# selection (W_GAP 0.25, cfg 0.3 only, three clips) made the pack the user approved; later tuning (cadence weighed more,
# a cfg 0.2 try, a clip-sound score) measured better but sounded worse, so it was reverted (PLAN Phase 18).
# Phase 9: how the reference clip *sounds* (clipsound.py: muffled, bandlimited, noisy, roomy, clipped) costs its
# penalty in full, in the clips tried first and in the score (calibrated in the maintainers' notes, clip-sound: the muffled
# Freeman v2 clip loses to the approved one by it). Only new picks see it, and a new pick waits for the user's ear
# (tts/pending/) whenever the voice already has a cadence.
W_SOUND = 1.0
# A new pack is offered only when it beats the live one by more than the test's own noise: the same clip scores 0.02-0.03
# apart between runs (phase 9b: 0.668 and 0.694), and the best of five noisy scores is biased upward (was 0.005 with three)
KEEP_MARGIN = 0.02
# Phase 12: pace and pitch of a pack's readings against the person (the maintainers' notes, pace-pitch). Pitch is a property
# of the pack (steady across readings) and a register: the user heard Obama at 235 Hz as "a fire speech" and at 182 Hz
# as "a calmer interview", both like him (phase 12b). So a pitch inside the range of the person's own sources (his
# debate 137 Hz .. his rallies 233 Hz) is on pitch; only a pitch past it gets the nudge back to the nearest edge of it,
# and a pack heard past it (no nudge) loses W_PITCH per semitone past the dead zone when packs are compared. Pace is
# measured and kept, not scored: one pack's pace moves +-10% between readings (the engine samples), and as a penalty it
# would have overturned the user's blind verdict on Freeman.
PACE_MIN_N = 3       # a source counts toward the target pace once this many of its sentences were measured
PACE_DEAD = 0.06     # within 6% of their pace: "ok" (about the smallest tempo change people notice in speech)
PITCH_DEAD = 0.5     # semitones past the person's range of registers: no nudge, no penalty within it
PITCH_MIN_N = 8      # a source sets an edge of that range once this many of its sentences were measured
W_PITCH = 0.05       # score units per semitone heard off the person's pitch, past the dead zone
CAP_PITCH = 0.15
# 1.1: the pace of a fast-reading voice (Freeman's packs read 1.5-1.8x his measured pace). Turbo and Chatterbox copy the
# pace of their reference clip, and stretching the clip was heard as slurred (the maintainers' notes, phase23-outcome),
# so the lever is the choice of clip: each candidate's own pace is measured from its transcript, and SLOW_SLOTS of the
# CANDIDATES go to the person's slowest clean clips (within SLOW_MARGIN of the best clip's quality). In the pick, pace is
# a tie-break, not a penalty: past the readings' own noise (PACE_NOISE, +-10% between seeds) a pack loses W_PACE per unit
# of log-ratio, at most CAP_PACE, less than the 0.1 the approved Freeman pack wins its blind pair by (phase 12), so it
# can't overturn the ear's verdict; and a new pick still waits for the ear (tts/pending/).
SLOW_SLOTS = 2
SLOW_MARGIN = 0.08
SLOW_DUR = (6.0, 12.0)   # seconds: the slow slots' clips (the others keep phase 3's 7-11 s)
PACE_NOISE = 0.10
W_PACE = 0.15
CAP_PACE = 0.05


# ---------------------------------------------------------------- text helpers (same rules as phase 3)

def norm_text(s: str) -> str:
    s = s.lower().replace("’", "'")
    s = re.sub(r"[^a-z0-9' ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def syllables(text: str) -> int:
    n = 0
    for w in norm_text(text).split():
        w = w.strip("'")
        if not w:
            continue
        if w.isdigit():
            n += 2 * len(w)
            continue
        k = len(re.findall(r"[aeiouy]+", w))
        if w.endswith("e") and not w.endswith(("le", "ee", "ye")) and k > 1:
            k -= 1
        if w.endswith("ed") and not w.endswith(("ted", "ded")) and k > 1:
            k -= 1
        n += max(1, k)
    return n


def wer(refs: list[str], hyps: list[str]) -> float:
    errs = total = 0
    for r, h in zip(refs, hyps):
        r, h = norm_text(r).split(), norm_text(h).split()
        d = list(range(len(h) + 1))
        for i in range(1, len(r) + 1):
            prev, d[0] = d[0], i
            for j in range(1, len(h) + 1):
                cur = min(d[j] + 1, d[j - 1] + 1, prev + (r[i - 1] != h[j - 1]))
                prev, d[j] = d[j], cur
        errs += d[len(h)]
        total += len(r)
    return errs / max(1, total)


# ---------------------------------------------------------------- transcripts

def _tr_file(proj) -> Path:
    return proj.dir / "transcripts.json"


def transcripts(proj) -> dict:
    """{clip id: text} for the project's transcribed clips."""
    try:
        return json.loads(_tr_file(proj).read_text(encoding="utf-8")).get("clips", {})
    except (OSError, ValueError):
        return {}


def speech_clips(proj) -> list:
    """The person's kept speech (not singing): what a cadence is made from."""
    return [p for p in proj.training_phrases() if not p.sung]


def transcribe(proj_name: str, push, check) -> dict:
    """Transcribe the kept speech clips not done yet (resumable: saved every batch). push(frac, message);
    check() raises Paused / Cancelled between batches."""
    proj = P.Project.load(proj_name)
    done = transcripts(proj)
    todo = [p for p in speech_clips(proj) if p.id not in done and proj.clip_path(p.id).exists()]
    total = len(todo)
    model = (CONFIG.get("cadence") or {}).get("transcribe_model", "large-v3-turbo")
    t0, secs = time.time(), 0.0
    for k in range(0, total, 24):
        check()
        batch = todo[k:k + 24]
        texts = C.transcribe([proj.clip_path(p.id) for p in batch],
                             progress=lambda m, d=0, t=0: push((k + d) / max(1, total), m if not t else f"Transcribing {k + d} of {total} clips"))
        for p, t in zip(batch, texts):
            done[p.id] = t
        secs += sum(p.dur for p in batch)
        fsutil.write_text(_tr_file(proj), json.dumps({"model": model, "clips": done}, ensure_ascii=False, indent=0))
        el = time.time() - t0
        left = (total - k - len(batch)) * (el / (k + len(batch)))
        push((k + len(batch)) / max(1, total), f"Transcribing {k + len(batch)} of {total} clips", eta=round(left))
    kept = speech_clips(proj)
    return {"clips": sum(1 for p in kept if p.id in done), "of": len(kept), "new": total,
            "minutes": round(sum(p.dur for p in kept if p.id in done) / 60, 1), "seconds": round(time.time() - t0, 1)}


# ---------------------------------------------------------------- measuring speech (as phase 3's measure.py)

KEYS = ["rate", "artic", "pauses", "pause_ms", "range_st", "sd_st", "move_st", "slope"]


def _speech_stats(x16: np.ndarray, text: str) -> dict:
    """The timing numbers of one utterance (no pitch: no GPU): its duration, syllables, rate and pauses; a (start, end)
    in 10 ms frames for the pitch part of `features`."""
    hop = 160
    n = len(x16) // hop
    if n < 20:
        return {}
    fr = x16[:n * hop].reshape(n, hop)
    db = 10 * np.log10((fr ** 2).mean(1) + 1e-10)
    db = np.convolve(db, np.ones(3) / 3, "same")
    on = db > np.percentile(db, 95) - 35.0
    idx = np.where(on)[0]
    if len(idx) < 10:
        return {}
    a, b = idx[0], idx[-1] + 1
    pauses, run = [], 0
    for v in on[a:b]:
        if not v:
            run += 1
        else:
            if run >= 15:
                pauses.append(run)
            run = 0
    dur = (b - a) / 100
    ptot = sum(pauses) / 100
    syl = syllables(text)
    return {"dur": round(dur, 2), "syl": syl, "rate": syl / dur, "artic": syl / max(0.3, dur - ptot),
            "pauses": 10 * len(pauses) / max(1, syl), "plist": [10 * p for p in pauses], "span": (a, b)}


def features(x16: np.ndarray, text: str, rmvpe) -> dict:
    """One utterance's cadence numbers (16 kHz audio and what it says)."""
    out = _speech_stats(x16, text)
    if not out:
        return {}
    a, b = out.pop("span")
    f0 = rmvpe.infer_from_audio(x16, thred=0.03)
    f0 = f0[a:min(b, len(f0))]
    t = np.arange(len(f0)) / 100
    v = f0 > 0
    if v.sum() > 30:
        st = 12 * np.log2(f0[v] / np.median(f0[v]))
        out.update(range_st=float(np.percentile(st, 95) - np.percentile(st, 5)), sd_st=float(np.std(st)),
                   slope=float(np.polyfit(t[v], st, 1)[0]), f0_med=float(np.median(f0[v])))
        f5 = f0[: len(f0) // 5 * 5].reshape(-1, 5)
        vv = (f5 > 0).all(1)
        s5 = 12 * np.log2(np.where(vv, f5.mean(1), 1.0))
        d = np.abs(np.diff(s5))[(vv[1:] & vv[:-1])]
        out["move_st"] = float(d.mean()) if len(d) else 0.0
    return out


def summarize(rows: list[dict]) -> dict:
    s = {}
    pl = [p for r in rows for p in r.get("plist", [])]
    for r in rows:
        r["pause_ms"] = float(np.mean(pl)) if pl else 0.0
    for k in KEYS:
        vals = [r[k] for r in rows if k in r]
        if vals:
            s[k] = round(float(np.mean(vals)), 3)
            s[k + "_sd"] = round(float(np.std(vals)), 3)
    if len(pl) > 1:   # the pause mean is pooled over every pause, so its spread is that of single pauses (as phase 3)
        s["pause_ms_sd"] = round(float(np.std(pl)), 1)
    else:
        s.pop("pause_ms", None)
        s.pop("pause_ms_sd", None)
    return s


def _pace(rows: list[dict]) -> float | None:
    """Syllables per second over whole sentences (their pauses included): total syllables / total time."""
    dur = sum(r["dur"] for r in rows if r.get("dur"))
    return round(float(sum(r["syl"] for r in rows if r.get("dur")) / dur), 3) if dur > 0 else None


def voice_profile(rows: list[dict]) -> dict:
    """The person's pace per source (a speech, a debate, an interview: each its own register) and their median
    pitch, from their measured sentences (phase 12). `sents` keeps [source, syllables, seconds, f0 Hz] per sentence."""
    by = {}
    for r in rows:
        by.setdefault(r.get("src") or "?", []).append(r)
    f0 = [r["f0_med"] for r in rows if r.get("f0_med")]
    out = {"by_source": {src: {"n": len(rs), "pace": _pace(rs),
                               "f0_hz": round(float(np.median([r["f0_med"] for r in rs if r.get("f0_med")])), 1)
                               if any(r.get("f0_med") for r in rs) else None}
                         for src, rs in by.items()},
           "pace": _pace(rows),
           "sents": [[r.get("src") or "?", r["syl"], r["dur"], round(r.get("f0_med") or 0, 1)] for r in rows]}
    if f0:
        med = float(np.median(f0))
        out["f0_hz"] = round(med, 1)
        out["f0_sd_st"] = round(float(np.std(12 * np.log2(np.asarray(f0) / med))), 2)
    return out


def pace_target(person: dict) -> float | None:
    """The pace a pack's everyday lines should have: the median of the person's sources' paces (each source once, so
    one long slow speech doesn't decide), from sources with at least PACE_MIN_N measured sentences."""
    srcs = [b["pace"] for b in (person.get("by_source") or {}).values() if b.get("pace") and b.get("n", 0) >= PACE_MIN_N]
    return round(float(np.median(srcs)), 3) if srcs else person.get("pace") or person.get("rate")


def pace_pitch(pace: float | None, f0_hz: float | None, person: dict) -> dict:
    """A pack's readings against the person: pace as a ratio to their target pace ("fast" / "slow" / "ok" past the
    dead zone), pitch in semitones from their median (positive = faster / higher; phase 12)."""
    out = {"pace": pace, "f0_hz": f0_hz and round(f0_hz, 1)}
    target = pace_target(person)
    if pace and target:
        r = pace / target
        out.update(pace_target=target, pace_ratio=round(r, 3),
                   pace_heard="fast" if r > 1 + PACE_DEAD else "slow" if r < 1 / (1 + PACE_DEAD) else "ok")
    if f0_hz and person.get("f0_hz"):
        out["pitch_st"] = round(12 * math.log2(f0_hz / person["f0_hz"]), 2)   # from their median (for reading)
        out["pitch_off_st"] = pitch_off(f0_hz, person)   # past the range of their own sources: what counts
    return out


def pitch_range(person: dict) -> tuple[float, float]:
    """The person's own registers, lowest and highest: the median pitch of each source with PITCH_MIN_N+ measured
    sentences (Obama: a debate 137 Hz .. a rally 233 Hz). Without such sources, just their median."""
    f = [b["f0_hz"] for b in (person.get("by_source") or {}).values() if b.get("f0_hz") and b.get("n", 0) >= PITCH_MIN_N]
    return (min(f), max(f)) if f else (person["f0_hz"], person["f0_hz"])


def pitch_off(f0_hz: float, person: dict) -> float:
    """Semitones a pitch lies past the person's range of registers (0 inside it; negative = below)."""
    lo, hi = pitch_range(person)
    return round(12 * math.log2(f0_hz / hi) if f0_hz > hi else 12 * math.log2(f0_hz / lo) if f0_hz < lo else 0.0, 2)


def pace_pen(ratio: float | None) -> float:
    """Score units lost for readings whose pace is `ratio` x the person's, past the readings' own noise (a tie-break)."""
    if not ratio or ratio <= 0:
        return 0.0
    return round(min(CAP_PACE, W_PACE * max(0.0, abs(math.log(ratio)) - math.log(1 + PACE_NOISE))), 4)


def clip_pace(p, tr: dict) -> float | None:
    """A clip's own pace, syllables per second over the whole clip (its pauses included, as the person's pace is)."""
    text = tr.get(p.id) or ""
    return round(syllables(text) / p.dur, 3) if text and p.dur else None


def pitch_pen(st: float | None) -> float:
    """Score units lost for readings heard `st` semitones past the person's range of registers."""
    return round(min(CAP_PITCH, W_PITCH * max(0.0, abs(st or 0.0) - PITCH_DEAD)), 4)


def nudge_for(m: dict) -> float:
    """The pitch nudge (semitones, 0.1 steps) that brings a pack's readings back to the nearest edge of the person's
    range of registers; none inside it (a speech pitch or an interview pitch are both theirs)."""
    st = m.get("pitch_off_st")
    return -round(st, 1) if st is not None and abs(st) > PITCH_DEAD else 0.0


def heard(m: dict, nudge: float = 0.0) -> float:
    """A pack's score as it would be heard: m measured its readings (with whatever nudge they already had), `nudge`
    more semitones on top; readings heard past the person's range of registers lose pitch_pen, and readings well off
    their pace lose pace_pen (1.1: a tie-break, capped below the ear's margins)."""
    st = m.get("pitch_off_st")
    return round(m["score"] - pitch_pen(None if st is None else st + nudge) - pace_pen(m.get("pace_ratio")), 4)


def as_heard(m: dict, nudge: float):
    """m measured readings without `nudge`: from now on its pitch_st is as heard (with the nudge) and pitch_raw_st the
    engine's own offset, the same meanings as a live pack measured with its nudge."""
    if m.get("pitch_st") is not None:
        m["pitch_raw_st"] = m["pitch_st"]
        m["pitch_st"] = round(m["pitch_st"] + nudge, 2)
    if m.get("pitch_off_st") is not None and nudge:
        m["pitch_off_st"] = round(m["pitch_off_st"] + nudge, 2)


def cadence_gap(s: dict, person: dict) -> float | None:
    ds = [abs(s[k] - person[k]) / person[k + "_sd"] for k in KEYS if k in s and k in person and person.get(k + "_sd", 0) > 1e-3]
    return round(float(np.mean(ds)), 3) if ds else None


def _runs(proj, max_gap: float = 2.5, n: int = STRETCHES) -> list[list]:
    """Stretches of the person talking: consecutive kept speech phrases from one source, gaps < max_gap s."""
    kept = {p.id for p in speech_clips(proj)}
    ok = kept | {p.id for p in proj.phrases if p.reason == "too short" and not p.sung}
    runs, cur = [], []
    for p in sorted(proj.phrases, key=lambda p: (p.source, p.start)):
        if p.id in ok and cur and p.source == cur[-1].source and p.start - (cur[-1].start + cur[-1].dur) < max_gap:
            cur.append(p)
            continue
        if cur:
            runs.append(cur)
        cur = [p] if p.id in ok else []
    if cur:
        runs.append(cur)
    runs = [r for r in runs if r[-1].start + r[-1].dur - r[0].start >= 8.0 and sum(p.id in kept for p in r) >= 2]
    runs.sort(key=lambda r: r[0].id)
    step = max(1, len(runs) // n)
    return runs[::step][:n]


def person_cadence(proj_name: str, rmvpe, push, check) -> dict:
    """How the person really talks, from stretches of their speech rebuilt on the original timeline (clips alone
    don't work: they're cut at pauses). Cached in cadence/person.json while the clips don't change."""
    proj = P.Project.load(proj_name)
    runs = _runs(proj)
    d = proj.dir / "cadence"
    d.mkdir(exist_ok=True)
    key = [[p.id for p in r] for r in runs]
    try:
        old = json.loads((d / "person.json").read_text(encoding="utf-8"))
        if old.get("runs") == key and old.get("v") == VERSION:
            return old
    except (OSError, ValueError):
        pass
    if not runs:
        raise ModelError(f"There isn't enough continuous speech of {proj.person} to measure how they talk "
                         "(needs stretches of 8 s or more). Add interviews or speeches in DoubleAgent.")
    files = []
    for k, run in enumerate(runs):
        check()
        t0 = run[0].start
        y = np.zeros(int((run[-1].start + run[-1].dur - t0) * 16000) + 1, np.float32)
        for p in run:
            x = render.load16(proj.clip_path(p.id))
            a = int((p.start - t0) * 16000)
            seg = y[a:a + len(x)]
            y[a:a + len(seg)] = np.where(np.abs(x[:len(seg)]) > np.abs(seg), x[:len(seg)], seg)
        f = d / f"r{k:03d}.wav"
        sf.write(str(f), y, 16000, subtype="PCM_16")
        files.append(f)
    push(0.2, f"Listening to {len(files)} stretches of {proj.person} talking")
    words = C.transcribe(files, words=True, progress=lambda m, dn=0, t=0: push(0.2 + 0.6 * dn / max(1, t or 1), "Listening to how they talk"))
    rows, gaps = [], []
    for run, f, w in zip(runs, files, words):
        check()
        x = render.load16(f)
        sents, cur = [], []
        for wd in w["words"]:
            cur.append(wd)
            if wd[0].strip().endswith((".", "?", "!")):
                sents.append(cur)
                cur = []
        for a, b in zip(sents, sents[1:]):
            gaps.append(b[0][1] - a[-1][2])
        for snt in sents:
            text = "".join(wd[0] for wd in snt)
            if syllables(text) < 6:
                continue
            r = features(x[max(0, int((snt[0][1] - 0.1) * 16000)): int((snt[-1][2] + 0.15) * 16000)], text, rmvpe)
            if r:
                rows.append({**r, "src": run[0].source})
    if len(rows) < 8:
        raise ModelError(f"Only {len(rows)} whole sentences of {proj.person} could be measured (8+ needed). "
                         "Add more interviews or speeches in DoubleAgent.")
    gaps = [g for g in gaps if 0.05 <= g <= 3.0]
    out = {"v": VERSION, "runs": key, "sentences": len(rows), **summarize(rows),
           "sentence_gap_s": [round(float(np.percentile(gaps, q)), 3) for q in (25, 50, 75)] if len(gaps) >= 5 else None,
           **voice_profile(rows), "made": time.strftime("%Y-%m-%d %H:%M")}
    fsutil.write_text(d / "person.json", json.dumps(out, indent=1))
    return out


# ---------------------------------------------------------------- reference clips and testing them

def _plain(t: str) -> bool:
    """A whole sentence in plain words: engines that time their speech from the reference text read it right."""
    t = (t or "").strip()
    return bool(t and t[0].isupper() and t.endswith((".", "!", "?")) and not t.endswith("...")
                and not re.search(r"[0-9]|\betc\b|\b[A-Z]{2,}\b|-", t))


def clip_sound(f: Path) -> dict:
    """How a clip sounds (clipsound.py) with its penalty in score units (0 = clean)."""
    m = clipsound.measure_file(f)
    return {**m, "penalty": clipsound.penalty(m)}


def ref_candidates(proj, tr: dict, n: int = CANDIDATES, sound: bool = True, slow: int = SLOW_SLOTS) -> list:
    """Kept speech clips of 7-11 s, clean, clearly the person, plain transcript; best first, from different
    sources where possible (phase 3's pick_ref rule). sound: a clip that sounds muffled / noisy / roomy ranks lower
    (phase 9; False = the rule before it, for comparisons). slow (1.1): that many of the n go to the slowest of the
    clips within SLOW_MARGIN of the best one's quality (the engines copy their clip's pace), from different sources
    where possible; 0 = the rule before it."""
    pen = {}

    def quality(p):
        q = p.score - 2 * max(0.0, (p.noise if p.noise >= 0 else 0.1) - 0.05) - 0.01 * max(0.0, p.music_db + 30)
        if sound:
            if p.id not in pen:
                try:
                    pen[p.id] = clip_sound(proj.clip_path(p.id))["penalty"]
                except (OSError, RuntimeError, ValueError):
                    pen[p.id] = 0.0
            q -= W_SOUND * pen[p.id]
        return q
    cands = sorted([p for p in speech_clips(proj) if 7.0 <= p.dur <= 11.0 and _plain(tr.get(p.id, ""))], key=quality, reverse=True)
    if not cands:   # nothing ideal: the longest plain-ish clips of 5-12 s
        cands = sorted([p for p in speech_clips(proj) if 5.0 <= p.dur <= 12.0 and tr.get(p.id)], key=quality, reverse=True)
    slow = max(0, min(slow, n - 1)) if cands else 0
    picked_slow = []
    if slow:
        q0 = quality(cands[0])
        # (a wider window of lengths for these: a slow speaker's 7-11 s clips are few, Freeman 7 of his 23 at 6-12 s)
        pool = [p for p in speech_clips(proj) if SLOW_DUR[0] <= p.dur <= SLOW_DUR[1] and _plain(tr.get(p.id, ""))]
        near = sorted((p for p in pool if quality(p) >= q0 - SLOW_MARGIN and clip_pace(p, tr)), key=lambda p: clip_pace(p, tr))
        if near:   # the slowest of them, whatever their source; only clips slower than their median (a slot never takes a fast one)
            mid = clip_pace(near[len(near) // 2], tr)
            picked_slow = [p for p in near if clip_pace(p, tr) < mid][:slow]
    out, seen = [], set()
    for p in cands:
        if len(out) >= n - len(picked_slow):
            break
        if p not in picked_slow and p.source not in seen:
            out.append(p)
            seen.add(p.source)
    out += [p for p in cands if p not in out and p not in picked_slow][:n - len(picked_slow) - len(out)]
    return out + picked_slow


class Tester:
    """Reads the test sentences with a pack and measures the result like phase 3 did."""

    def __init__(self, voice, proj, person: dict, check):
        from ..core.speaker import SpeakerEmbedder
        from . import service
        self.v, self.proj, self.person, self.check = voice, proj, person, check
        self.conv = gpu.MODELS.converter
        self.lv = self.conv.voice(voice.name)
        sp = service.speaking_pitch(voice)
        self.hz = sp and sp["hz"]
        self._hz_of: dict = {}   # each reference clip's reading pitch (speech_target), measured once
        self.emb = SpeakerEmbedder()
        e = proj.load_embeddings().get(f"speaker:{proj.reference.get('cluster')}")
        if e is None:
            raise ModelError(f"The {proj.person} project has no confirmed voice to compare with. Open it in DoubleAgent first.")
        self.ref_vec = e / (np.linalg.norm(e) + 1e-9)

    def read(self, pack: dict, ref: Path) -> list[Path]:
        """The engine's readings of the test sentences and the everyday lines (kept, so variants reuse them)."""
        futs = [render.request(s, pack, ref, seed=SEED) for s in TEST + PACE_TEST]
        out = []
        for fut, wav in futs:
            render.client.wait(fut, cancel=lambda: (self.check(), False)[1])
            out.append(wav)
        return out

    def measure(self, wavs: list[Path], tempo: float = 1.0, ref: Path | None = None, nudge: float = 0.0) -> dict:
        """The readings' numbers and score; with ref, the reference clip's sound counts too (phase 9). Likeness, WER
        and the cadence gap come from the test sentences as before; pace from the everyday lines and pitch from all
        (phase 12). nudge: semitones the voice's RVC adds (a pack's pitch nudge)."""
        from scipy.signal import resample_poly
        rows, sims, outs, everyday, f0s = [], [], [], [], []
        if ref is not None and ref not in self._hz_of:   # pitched as SpeakEasy will read this pack: at its clip's pitch
            self._hz_of[ref] = speech_target(self.v, None, ref, self.conv.rmvpe)
        hz = self._hz_of.get(ref, self.hz)
        for i, (s, w) in enumerate(zip(TEST + PACE_TEST, wavs)):
            self.check()
            y, sr = render.finish(w, self.lv, self.conv, hz, tempo=tempo, nudge=nudge, keep_wav=True, engine=C.reading_engine(None))
            g = np.gcd(sr, 16000)
            y16 = resample_poly(y, 16000 // g, sr // g).astype(np.float32)
            r = features(y16, s, self.conv.rmvpe)
            if r and r.get("f0_med"):
                f0s.append(r["f0_med"])
            if i >= len(TEST):
                if r:
                    everyday.append(r)
                continue
            sims.append(float(self.emb.embed(y16) @ self.ref_vec))
            if r:
                rows.append(r)
            f = render.scratch() / f"{w.stem}_t{int(tempo * 100)}_n{int(round(nudge * 10))}.wav"
            sf.write(str(f), y16, 16000, subtype="PCM_16")
            outs.append(f)
        heard = C.transcribe(outs)
        for f in outs:
            f.unlink(missing_ok=True)
        s = summarize(rows)
        m = {"likeness": round(float(np.mean(sims)), 3), "wer": round(wer(TEST, heard), 3),
             "cadence_gap": cadence_gap(s, self.person), "tempo": round(tempo, 3),
             **{k: s[k] for k in ("rate", "pauses", "range_st") if k in s}}
        m["score"] = round(m["likeness"] - W_GAP * (m["cadence_gap"] or 1.0) - W_WER * m["wer"], 4)
        m.update(pace_pitch(_pace(everyday), float(np.median(f0s)) if f0s else None, self.person))
        if nudge:
            m["nudge"] = round(nudge, 2)
        if ref is not None:
            snd = clip_sound(ref)
            m["sound"] = snd["penalty"]
            m["sound_parts"] = {k: snd.get(k) for k in ("hf_db", "edge_hz", "floor_db", "decay_db_s", "clip_frac")}
            m["score_voice"] = m["score"]   # the score before the clip's sound (as phase 4 scored)
            m["score"] = round(m["score"] - W_SOUND * snd["penalty"], 4)
        return m


def _cleanup(wavs):
    for w in wavs:
        Path(w).unlink(missing_ok=True)


# ---------------------------------------------------------------- a voice's pack

def speech_target(v, pack: dict | None, ref: Path | None = None, rmvpe=None) -> float | None:
    """The pitch (Hz) a reading with this pack is moved onto: the person's own speaking pitch in the pack's reference
    clip (its speak_hz; measured here when the pack has none yet and rmvpe and ref are given), held inside the voice's
    own range; the voice's speaking pitch (service.speaking_pitch) when there's none. A voice's f0_median is its whole
    training data's: one trained on rallies sat at 180 Hz, its reading reference at 143, and the ear chose the lower
    one blind (2026-10-28). Speech only: songs never come here."""
    from . import service
    sp = service.speaking_pitch(v)
    pack = pack or {}
    hz = pack.get("speak_hz")
    if hz is None and "speak_hz" not in pack and rmvpe is not None and ref is not None:
        hz = render.speak_hz(ref, rmvpe)
    if not hz:
        return sp and sp["hz"]
    ex = v.extra or {}
    rng = ex.get("f0_range")
    if rng:   # RVC is never pushed past what it learned (a singer's range, an octave down, as speaking_pitch reads it)
        k = 1.0 if ex.get("from_speech") else service.SUNG_TO_SPEECH
        hz = min(max(float(hz), rng[0] * k), rng[-1] * k)
    return round(float(hz), 1)


def remember_speak_hz(voice_name: str, rmvpe) -> float | None:
    """The live pack's speak_hz, measured once and kept in extra.tts for a pack made before it was (None kept too, so
    a clip that can't be measured isn't tried on every reading). Writes meta.json only; moves and deletes nothing."""
    with _LOCK:
        v = get_voice(voice_name)
        pack = pack_of(v)
        if pack is None or "speak_hz" in pack:
            return pack and pack.get("speak_hz")
        ref = pack.get("ref", "ref.wav")
        pack.update(speak_hz=render.speak_hz(v.dir / "tts" / ref, rmvpe), speak_hz_from=ref)
        fsutil.write_text(v.dir / "meta.json", json.dumps(v.to_json(), indent=2))
    return pack["speak_hz"]


def pack_of(v) -> dict | None:
    t = (v.extra or {}).get("tts")
    return t if t and (v.dir / "tts" / t.get("ref", "ref.wav")).exists() else None


def project_for_voice(v, create_person: bool = False):
    """The training project with this voice's person's clips: the one it was trained from, the one its cadence came
    from, or one for its checked person."""
    from .night import _project_named, _person_from_voice
    ex = v.extra or {}
    for name in (ex.get("project"), (ex.get("tts") or {}).get("project")):
        if name:
            try:
                return P.Project.load(name)
            except ModelError:
                pass
    person = ex.get("person") or (_person_from_voice(v) if ex.get("trained") else None)
    return _project_named(person) if person else None


def can_train(v) -> tuple[bool, str]:
    """(possible?, why not / what it will do) for "Train cadence" on a voice."""
    ex = v.extra or {}
    if ex.get("demo"):
        return False, "The demo voice is a demo voice made for UnderCover, not a real person, so there's nobody to find clips of."
    if not C.engine_ready(C.default_engine()):
        return False, "The cadence engine isn't installed."
    proj = project_for_voice(v)
    if proj is not None:
        if proj.kept_seconds() - proj.sung_seconds() < MIN_MINUTES * 60:
            return False, (f"{proj.person}'s clips are mostly singing: a cadence needs a few minutes of them talking "
                           "(add interviews in DoubleAgent).")
        return True, "from the voice's clips"
    if not ex.get("trained") and voice_person(v):   # a downloaded voice (its checked person, or a guess from its name)
        return True, f"finds clips of {voice_person(v)} first"
    return False, "Downloaded voice: UnderCover doesn't know whose voice it is."


def train(job, voice_name: str, push, control=None) -> dict:
    """Train cadence of a voice (see the top of this file). push(stage, frac, message, **kw)."""
    from . import service
    raw = push
    span = {"clips": (0.0, 0.2), "transcripts": (0.2, 0.45), "measuring": (0.45, 0.6), "testing": (0.6, 0.98)}

    def push(stage, f, m, **kw):   # one bar for the whole job: each step fills its share
        a, b = span.get(stage, (0.0, 1.0))
        raw(stage, a + (b - a) * min(1.0, max(0.0, f)), m, **kw)

    def check():
        if job.cancel:
            raise Cancelled()
        if job.pausing or (control is not None and control.pause):
            raise Paused()
        if control is not None:
            control.service()   # covers and speech queued meanwhile run between steps
    v = get_voice(voice_name)
    engine = C.default_engine()
    if not C.engine_ready(engine):
        raise ModelError("The cadence engine (Chatterbox) isn't installed in this UnderCover. Run setup again to add it.")
    proj = project_for_voice(v)
    if proj is None and (v.extra or {}).get("demo"):
        raise ModelError(f"{label_of(v.name)} is a demo voice made for UnderCover, not a real person, so there's nobody to find clips of.")
    if proj is None:
        person = None if (v.extra or {}).get("trained") else voice_person(v)
        if not person:
            raise ModelError(f"UnderCover doesn't know whose voice {label_of(v.name)} is, so it can't find their clips.")
        push("clips", 0.0, f"Finding clips of {person} talking")
        name = service.train_new_voice(job, person, control or worklist.control_for(job),
                                       lambda st, f, m, **kw: push("clips", f, m, **kw), prepare_only=True,
                                       speaking=True)
        proj = P.Project.load(name)
    # 1. transcripts
    push("transcripts", 0.0, f"Transcribing {proj.person}'s clips")
    tr_info = transcribe(proj.name, lambda f, m, **kw: push("transcripts", f, m, **kw), check)
    if tr_info["minutes"] < MIN_MINUTES:
        raise ModelError(f"Only {tr_info['minutes']} min of {proj.person} talking ({MIN_MINUTES:.0f}+ needed for a cadence). "
                         "Add interviews or speeches in DoubleAgent.")
    proj = P.Project.load(proj.name)
    tr = transcripts(proj)
    # 2. how they talk
    push("measuring", 0.0, f"Measuring how {proj.person} talks")
    person = person_cadence(proj.name, gpu.MODELS.converter.rmvpe, lambda f, m: push("measuring", f, m), check)
    # 3. reference clips, each tested on the same sentences
    tester = Tester(v, proj, person, check)
    cfg = CONFIG.get("cadence") or {}
    base = {"engine": engine, "cfg_weight": float(cfg.get("cfg_weight", 0.3)), "exaggeration": float(cfg.get("exaggeration", 0.5))}
    cands = ref_candidates(proj, tr)
    if not cands:
        raise ModelError(f"None of {proj.person}'s clips suits a reference (7-11 s of clean, whole-sentence speech).")
    work = render.scratch() / f"refs_{job.id}"
    work.mkdir(exist_ok=True)
    tried = []
    try:
        for i, p in enumerate(cands):
            push("testing", i / (len(cands) + 1), f"Trying reference clip {i + 1} of {len(cands)}")
            ref = work / f"{p.id}.wav"
            x, sr = sf.read(str(proj.clip_path(p.id)), dtype="float32")
            sf.write(str(ref), x, sr, subtype="PCM_16")
            pack = {**base, "ref_text": tr.get(p.id, "")}
            wavs = tester.read(pack, ref)
            try:
                m = tester.measure(wavs, 1.0, ref=ref)
                m["pitch_nudge"] = nudge_for(m)
                m["heard"] = heard(m, m["pitch_nudge"])
                as_heard(m, m["pitch_nudge"])
                best = m
            finally:
                _cleanup(wavs)
            tried.append({"clip": p.id, "source": p.source, "dur": round(p.dur, 2), "text": tr.get(p.id, ""),
                          "clip_pace": clip_pace(p, tr), "pace_pen": pace_pen(m.get("pace_ratio")), "tempo1": m, **best})
        win = max(tried, key=lambda t: t["score"] - t["pace_pen"])   # (1.1: pace a tie-break, capped)
        # 4. the voice's current cadence, on the same test: keep the new one only if it's better
        old = pack_of(v)
        before = None
        if old:
            push("testing", len(cands) / (len(cands) + 1), "Comparing with the current cadence")
            try:
                wavs = tester.read(old, v.dir / "tts" / old.get("ref", "ref.wav"))
                try:
                    before = tester.measure(wavs, float(old.get("tempo", 1.0)), ref=v.dir / "tts" / old.get("ref", "ref.wav"),
                                            nudge=float(old.get("pitch_nudge") or 0))
                    before["heard"] = heard(before)   # as it sounds today, with its own nudge
                    before["pitch_raw_st"] = None if before.get("pitch_st") is None else round(
                        before["pitch_st"] - float(old.get("pitch_nudge") or 0), 2)
                finally:
                    _cleanup(wavs)
            except (ModelError, C.WorkerError):
                before = None   # e.g. made with an engine that's gone: the new one replaces it
        mins = tr_info["minutes"]
        result = {"voice": v.name, "after": win["heard"], "before": before and before["heard"], "likeness": win["likeness"],
                  "cadence_gap": win["cadence_gap"], "wer": win["wer"], "data_minutes": mins, "engine": engine}
        # compared as heard: a pack off the person's pitch loses points, and a new one comes with its nudge
        if before is not None and win["heard"] <= before["heard"] + KEEP_MARGIN:
            result.update(replaced=False, note=f"Tried {len(tried)} reference clips; none beat the current cadence, so it stays.")
            return result
        if (old is not None and win["clip"] == old.get("clip") and all(base[k] == old.get(k) for k in base)
                and abs(win["pitch_nudge"] - float(old.get("pitch_nudge") or 0)) < 0.25):
            result.update(replaced=False, note="The best reference clip is the one it already speaks with, so the cadence stays.")
            return result
        # 5. save the pack with the voice. A voice that already speaks with a cadence keeps it: the new one waits in
        # tts/pending/ for the user's ear ("Keep this one", phase 9), also when measuring the current one failed this
        # time (a worker hiccup must not replace a pack the user approved). Only a current one whose engine is gone
        # is replaced at once, and even then it's moved aside (tts/previous/), never deleted.
        gaps = person.get("sentence_gap_s") or [0.3, 0.45, 0.8]
        gap = float(np.clip(gaps[1], 0.25, 1.0))
        waits = old is not None and C.pack_ready(old.get("engine") or engine, old.get("engine_pin"))   # engine-agnostic inside the Chatterbox family
        pack = {**base, "v": VERSION, "ref": "ref.wav", "ref_text": win["text"], "clip": win["clip"], "project": proj.name,
                "tempo": win["tempo"], "gap_s": round(gap, 3), "para_gap_s": round(max(0.7, gap * 1.6), 3),
                "data_minutes": mins,
                "pitch_nudge": win["pitch_nudge"],
                "speak_hz": render.speak_hz(work / f"{win['clip']}.wav", tester.conv.rmvpe), "speak_hz_from": "ref.wav",
                "scores": {k: win.get(k) for k in ("likeness", "wer", "cadence_gap", "rate", "score", "score_voice", "sound",
                                                    "heard", "pace", "pace_target", "pace_ratio", "pace_heard", "pace_pen",
                                                    "clip_pace", "f0_hz", "pitch_st", "pitch_raw_st", "pitch_off_st")},
                "person": {k: person.get(k) for k in ("rate", "pauses", "pause_ms", "range_st", "sentences", "sentence_gap_s")},
                "before": before and {k: before.get(k) for k in ("likeness", "wer", "cadence_gap", "score", "score_voice", "sound",
                                                                 "heard", "pace", "pace_ratio", "pace_heard", "f0_hz", "pitch_st",
                                                                 "pitch_raw_st", "pitch_off_st")},
                "made": time.strftime("%Y-%m-%d %H:%M"), "verdicts": []}
        if C.engine_info(C.reading_engine(pack.get("engine"))).get("pace_by_ref"):   # phase 23: off for every engine today (it read slurred)
            push("testing", 0.97, f"Matching {proj.person}'s own pace")
            try:
                apply_pace(pack, calibrate_pack(pack, work / f"{win['clip']}.wav", check))
            except (Cancelled, Paused):
                raise
            except Exception:   # noqa: BLE001 - the pack is fine without it; the first speech calibrates it
                pass
        new = work / "pack"
        new.mkdir(exist_ok=True)
        shutil.copyfile(work / f"{win['clip']}.wav", new / "ref.wav")
        fsutil.write_text(new / "cadence.json", json.dumps({**pack, "tried": tried}, indent=1))
        if save_pack(v.name, new, pack, waits) == "pending":
            how = (f"that measures better ({before['heard']:.3f} -> {win['heard']:.3f})" if before is not None else
                   "(the current one couldn't be measured this time)")
            result.update(replaced=False, pending=True,
                          note=f"Found a cadence for {label_of(v.name)} {how}. Listen to both and keep the one you like: "
                               "the current one stays until you choose.")
            return result
        result.update(replaced=True, note=(f"{label_of(v.name)} now speaks with {proj.person}'s cadence" if old is None else
                                           f"New cadence for {label_of(v.name)} (the old one couldn't be read; it's kept in tts/previous)")
                      + f" (from {mins} min of their speech).")
        return result
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------- pace calibration (phase 23)
# Turbo ignores cfg_weight / exaggeration and copies the pace of its reference clip, so its natural pace differs from
# Chatterbox's per voice (Freeman: about 1.5x his measured pace on Chatterbox, 1.7x on Turbo). Each pack is calibrated once
# per engine: the everyday lines are read with the reference clip played at a few speeds (made once, pitch kept, never
# the finished reading stretched) until the natural pace is within PACE_TOL of the person's own (phase 12's target).
PACE_TOL = 0.10
PACE_RATE_LO, PACE_RATE_HI = 0.5, 1.25   # the reference clip is read at this many times its speed
PACE_AIM = 0.05    # aim this far inside the person's pace (on the side the first reading was): the smallest change that lands
PACE_K = 0.5       # measured (tools/research/turbo_pace.py): pace follows the reference's speed to about the power 0.5
PACE_READS = 4     # readings of the six lines at most (the first is at the clip's own speed)


def choose_rate(read, target: float, tol: float = PACE_TOL) -> tuple[float, dict]:
    """The reference speed whose pace lands within tol of target: read(rate) -> pace (syllables per second). Starts at
    1.0, then jumps by the pace-follows-rate-to-the-k model (k refitted from two readings), aiming PACE_AIM inside the
    target on the side it started (a pace within the tolerance already is left alone, and one just outside isn't pushed
    past the person's: Obama at +12% went to -7% when aimed at the target itself). -> (rate, {rate: pace})."""
    err = lambda r: abs(tried[r] / target - 1)   # noqa: E731
    tried = {1.0: read(1.0)}
    goal = target * (1 + PACE_AIM * (1 if tried[1.0] > target else -1))
    while len(tried) < PACE_READS:
        best = min(tried, key=err)
        if err(best) <= tol:
            break
        k = PACE_K
        pts = sorted(tried.items())
        if len(pts) >= 2 and pts[0][0] != pts[-1][0] and pts[0][1] > 0 and pts[-1][1] > 0:
            fit = math.log(pts[-1][1] / pts[0][1]) / math.log(pts[-1][0] / pts[0][0])
            k = fit if 0.1 <= fit <= 2.0 else PACE_K
        nxt = round(float(np.clip(best * (goal / tried[best]) ** (1 / k), PACE_RATE_LO, PACE_RATE_HI)), 3)
        if any(abs(nxt - r) < 0.02 for r in tried):
            break   # the range's edge, or already read
        tried[nxt] = read(nxt)
    return min(tried, key=err), tried


def pace_needs_calibration(pack: dict | None) -> bool:
    """A pack whose engine paces by its reference and hasn't been calibrated for the engine that reads it now."""
    if not pack:
        return False
    eng = C.reading_engine(pack.get("engine"))
    return bool(C.engine_info(eng).get("pace_by_ref")) and (pack.get("pace_cal") or {}).get("engine") != eng


def calibrate_pack(pack: dict, ref: Path, check=None) -> dict:
    """The pace fields for a pack (to merge into it): pace_cal (what was measured), pace_ref_rate (the reference speed;
    absent at 1.0). Reads the six everyday lines a few times through the worker."""
    eng = C.reading_engine(pack.get("engine"))
    base = {"engine": eng, "at": time.strftime("%Y-%m-%d %H:%M")}
    if not C.engine_info(eng).get("pace_by_ref"):
        return {"pace_cal": {**base, "note": "the engine paces itself"}, "pace_ref_rate": None}
    target = pace_target(pack.get("person") or {})
    if not target:
        return {"pace_cal": {**base, "note": "no measured pace for this person"}, "pace_ref_rate": None}

    def read(rate: float) -> float:
        rp = render.paced_ref(ref, rate)
        futs = [render.request(s, pack, rp, seed=SEED) for s in PACE_TEST]
        rows = []
        for (fut, wav), s in zip(futs, PACE_TEST):
            render.client.wait(fut, cancel=lambda: (check(), False)[1] if check else False)
            rows.append(_speech_stats(render.load16(wav), s))
            Path(wav).unlink(missing_ok=True)
        return _pace([r for r in rows if r]) or 0.0
    rate, tried = choose_rate(read, target)
    return {"pace_cal": {**base, "target": target, "before": tried[1.0], "after": tried[rate], "rate": rate,
                         "within": round(abs(tried[rate] / target - 1), 3), "tried": {str(r): p for r, p in tried.items()}},
            "pace_ref_rate": None if rate == 1.0 else rate}


def apply_pace(pack: dict, fields: dict) -> dict:
    """fields (calibrate_pack's) into the pack; a rate of None removes the key."""
    pack.update({k: v for k, v in fields.items() if v is not None})
    if fields.get("pace_ref_rate") is None:
        pack.pop("pace_ref_rate", None)
    return pack


def calibrate_voice_pace(voice_name: str, check=None, force: bool = False) -> dict | None:
    """The live pack's pace, calibrated once for the engine that reads it (SpeakEasy calls this on first use; logs
    logs/pacecal.jsonl). Never replaces the pack: it adds pace_cal / pace_ref_rate to it. -> what was measured, or None
    when there was nothing to do."""
    v = get_voice(voice_name)
    pack = (v.extra or {}).get("tts")
    if not pack or not (force or pace_needs_calibration(pack)):
        return None
    eng = C.reading_engine(pack.get("engine"))
    try:
        fields = calibrate_pack(pack, v.dir / "tts" / pack.get("ref", "ref.wav"), check)
    except (Cancelled, Paused):
        raise
    except Exception as e:   # noqa: BLE001 - noted, not retried on every sentence; Deep Cover's repair can force it
        fields = {"pace_cal": {"engine": eng, "at": time.strftime("%Y-%m-%d %H:%M"), "error": f"{type(e).__name__}: {e}"[:200]},
                  "pace_ref_rate": None}
    with _LOCK:
        fresh = get_voice(voice_name)
        cur = (fresh.extra or {}).get("tts")
        if cur and cur.get("clip") == pack.get("clip"):   # the pack may have been replaced meanwhile: then not this one
            apply_pace(cur, fields)
            fsutil.write_text(fresh.dir / "meta.json", json.dumps(fresh.to_json(), indent=2))
            try:
                fsutil.write_text(fresh.dir / "tts" / "cadence.json", json.dumps({**cur, "tried": json.loads(
                    (fresh.dir / "tts" / "cadence.json").read_text(encoding="utf-8")).get("tried")}, indent=1))
            except (OSError, ValueError):
                pass
    log = {"voice": voice_name, **fields["pace_cal"]}
    try:
        f = path("logs_dir") / "pacecal.jsonl"
        fsutil.guard(f)
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(log) + "\n")
    except OSError:
        pass
    return fields["pace_cal"]


def pack_id(pack: dict) -> str:
    """Names a pack in verdicts: its reference clip and settings."""
    n = float(pack.get("pitch_nudge") or 0)
    return f"{pack.get('engine')}:{pack.get('clip')}:cfg{pack.get('cfg_weight')}:t{pack.get('tempo')}" + (f":p{n:+.1f}" if n else "")


def approved_by_user(pack: dict) -> bool:
    """The user picked this pack by ear (its latest verdict where it was compared says it won)."""
    me = {pack_id(pack), f"{pack_id(pack)}@{pack.get('made')}"}   # the dated form: two packs with the same id (_ids)
    for vd in reversed(pack.get("verdicts") or []):
        hit = me.intersection(vd.get("compared", []))
        if hit:
            return vd.get("winner") in hit
    return False


# ---------------------------------------------------------------- "Keep this one" (phase 9): the ear steers
#
# tts/                       the live pack: ref.wav + cadence.json (+ sample.wav / sample.json: its A/B reading)
# tts/pending/               a new pack waiting for the user's ear (Train cadence or a re-run, when the voice has one)
# tts/previous/              the pack that "keep the new one" last replaced (pack.json = its extra.tts at that moment)
# tts/older/<stamp>-<why>/   whatever would otherwise be overwritten: an earlier previous ("previous"), a pending pack
#                            replaced by a newer one before anyone listened ("unheard"), one the user turned down
#                            ("turned-down"). This code never deletes a pack; it only moves them (core.storage.move_within).
#                            SafeHouse's "delete the cadence" still removes the whole tts/ folder, as before.

_LOCK = threading.RLock()
# The line both packs read for the A/B (short, plain, a comma and a full stop; not one of the test sentences)
AB_LINE = "I have always believed that the best stories are the ones we tell each other at the end of a long day."
AB_SEED = 4321


def _pack_files(d: Path) -> list[Path]:
    return sorted(f for f in d.iterdir() if f.is_file()) if d.is_dir() else []


def _move(v, src: Path, dst: Path):
    """Every file of the pack in src into dst (inside the voice's tts/ folder only; nothing overwritten)."""
    storage.move_within([(f, dst / f.name) for f in _pack_files(src)], v.dir / "tts")
    if src != v.dir / "tts":
        storage.remove_empty(src, v.dir / "tts")


def _set_aside(v, d: Path, why: str) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dst, k = v.dir / "tts" / "older" / f"{stamp}-{why}", 2
    while dst.exists():
        dst, k = v.dir / "tts" / "older" / f"{stamp}-{why}-{k}", k + 1
    _move(v, d, dst)
    return dst


def _to_previous(v, pack: dict | None):
    """The live pack -> tts/previous/ (a pack already there goes to older/ first)."""
    tts = v.dir / "tts"
    if _pack_files(tts / "previous"):
        _set_aside(v, tts / "previous", "previous")
    _move(v, tts, tts / "previous")
    if pack:
        fsutil.write_text(tts / "previous" / "pack.json", json.dumps(pack, indent=1))


def _put_pack(v, src: Path, dst: Path):
    """A newly made pack (scratch folder) into the voice: copied, into a place that holds no pack."""
    if _pack_files(dst):
        raise ModelError(f"{dst} already holds a cadence; nothing was written.")
    dst.mkdir(parents=True, exist_ok=True)
    for f in _pack_files(src):
        shutil.copyfile(f, dst / f.name)


def save_pack(voice_name: str, src: Path, pack: dict, waits: bool) -> str:
    """A newly made pack (src: ref.wav + cadence.json) into the voice. waits: the voice speaks with a cadence that
    still works, so the new one goes to tts/pending/ (an earlier one nobody listened to is set aside, "unheard");
    otherwise it goes live and whatever was live moves to tts/previous/. -> "pending" | "live". Deletes nothing."""
    with _LOCK:
        v = get_voice(voice_name)   # fresh meta (an export or a verdict may have happened meanwhile)
        tts = v.dir / "tts"
        waits = waits and pack_of(v) is not None   # its cadence may have been deleted while this one was made
        if waits:
            if _pack_files(tts / "pending"):
                _set_aside(v, tts / "pending", "unheard")
            _put_pack(v, src, tts / "pending")
            return "pending"
        old = (v.extra or {}).get("tts")
        if _pack_files(tts):
            _to_previous(v, old)
        pack = {**pack, "verdicts": list((old or {}).get("verdicts", []))}
        _put_pack(v, src, tts)
        v.extra["tts"] = pack
        fsutil.write_text(v.dir / "meta.json", json.dumps(v.to_json(), indent=2))
    gpu.forget_voice(v.name)
    return "live"


def pending_of(v) -> dict | None:
    """The pack waiting in tts/pending/ for the user's ear, or None."""
    d = v.dir / "tts" / "pending"
    try:
        pack = json.loads((d / "cadence.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    pack.pop("tried", None)
    return pack if (d / pack.get("ref", "ref.wav")).exists() else None


def _ids(cur: dict, pen: dict) -> tuple[str, str]:
    """The two packs' ids in a verdict; the same clip and settings are told apart by when each was made."""
    a, b = pack_id(cur), pack_id(pen)
    return (a, b) if a != b else (f"{a}@{cur.get('made')}", f"{b}@{pen.get('made')}")


def _sample_ok(d: Path, pid: str) -> bool:
    try:
        meta = json.loads((d / "sample.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return meta.get("line") == AB_LINE and meta.get("pack") == pid and (d / "sample.wav").is_file()


def ab_state(voice_name: str) -> dict:
    """What the A/B card shows: the live pack and the one waiting, their numbers, and whether both readings exist."""
    v = get_voice(voice_name)
    cur, pen = pack_of(v), pending_of(v)
    out = {"voice": v.name, "display": label_of(v.name), "line": AB_LINE, "current": None, "pending": None, "ready": False}
    tts = v.dir / "tts"
    ci, pi = _ids(cur, pen) if cur and pen else (cur and pack_id(cur), pen and pack_id(pen))
    if cur:
        out["current"] = {"id": ci, "clip": cur.get("clip"), "made": cur.get("made"), "scores": cur.get("scores"),
                          "approved": approved_by_user(cur), "sample": _sample_ok(tts, ci),
                          "nudge": float(cur.get("pitch_nudge") or 0)}
    if pen:
        out["pending"] = {"id": pi, "clip": pen.get("clip"), "made": pen.get("made"), "scores": pen.get("scores"),
                          "current_same_test": pen.get("before"), "sample": _sample_ok(tts / "pending", pi),
                          "nudge": float(pen.get("pitch_nudge") or 0)}
    out["ready"] = bool(cur and pen and out["current"]["sample"] and out["pending"]["sample"])
    return out


def sample_file(voice_name: str, which: str) -> Path:
    v = get_voice(voice_name)
    if which not in ("current", "pending"):
        raise LookupError("Not found")
    f = v.dir / "tts" / ("pending" if which == "pending" else "") / "sample.wav"
    if not f.is_file():
        raise LookupError("That reading hasn't been made yet.")
    return f


def make_samples(job, voice_name: str) -> dict:
    """Read AB_LINE once with the live pack and once with the pending one (engine + the voice's RVC), each saved with
    its pack (sample.wav + sample.json), so the A/B costs GPU time once. Readings already there are kept."""
    v = get_voice(voice_name)
    cur, pen = pack_of(v), pending_of(v)
    if not (cur and pen):
        raise ModelError(f"{label_of(v.name)} has no new cadence waiting to compare.")
    ids = _ids(cur, pen)
    tts = v.dir / "tts"
    todo = [(d, pack, pid) for d, pack, pid in ((tts, cur, ids[0]), (tts / "pending", pen, ids[1])) if not _sample_ok(d, pid)]
    conv = gpu.MODELS.converter
    lv = conv.voice(v.name)
    for i, (d, pack, pid) in enumerate(todo):
        if job.cancel:
            raise Cancelled()
        job.push(stage="reading", frac=i / max(1, len(todo)), message=f"Reading the line with the {'new' if d != tts else 'current'} cadence")
        ref = d / pack.get("ref", "ref.wav")
        fut, wav = render.request(AB_LINE, pack, ref, seed=AB_SEED)
        render.client.wait(fut, cancel=lambda: job.cancel)
        y, sr = render.finish(wav, lv, conv, speech_target(v, pack, ref, conv.rmvpe), tempo=float(pack.get("tempo", 1.0)),
                              nudge=float(pack.get("pitch_nudge") or 0), engine=C.pack_engine(pack))
        y = render.level(y)   # SpeakEasy's loudness, so both sound as they would there
        with _LOCK:
            fresh = get_voice(v.name)
            now = (pack_of(fresh), pending_of(fresh))
            if not all(now) or _ids(*now)[0 if d == tts else 1] != pid:   # a verdict moved it meanwhile
                continue
            sf.write(str(d / "sample.wav"), y, sr, subtype="PCM_16")
            fsutil.write_text(d / "sample.json", json.dumps({"line": AB_LINE, "pack": pid, "made": time.strftime("%Y-%m-%d %H:%M")}))
    return ab_state(v.name)


def verdict(voice_name: str, keep: str, why: str = "", by: str = "user") -> dict:
    """The user's choice between the live pack and the pending one. keep: "current" | "pending".
    Either way the verdict (both packs' ids and scores) is appended to the kept pack's extra.tts.verdicts.
    "pending": the live pack moves to tts/previous/ (an older previous to tts/older/), the pending one goes live.
    "current": the pending pack moves to tts/older/<stamp>-turned-down/. Nothing is deleted."""
    if keep not in ("current", "pending"):
        raise ModelError('Say which one to keep: "current" or "pending".')
    with _LOCK:
        v = get_voice(voice_name)
        cur, pen = pack_of(v), pending_of(v)
        if pen is None:
            raise ModelError(f"There's no new cadence waiting for {label_of(v.name)}.")
        if cur is None and keep == "current":
            raise ModelError(f"{label_of(v.name)} has no current cadence to keep; keep the new one or leave it waiting.")
        ci, pi = _ids(cur, pen) if cur else ("none", pack_id(pen))
        entry = {"date": time.strftime("%Y-%m-%d %H:%M"), "compared": [ci, pi], "winner": ci if keep == "current" else pi,
                 "why": (why or "").strip()[:1000], "by": by, "kept": keep,
                 # the numbers both packs had in the same test run (the pending pack measured the live one alongside)
                 "scores": {ci: pen.get("before") or (cur or {}).get("scores"), pi: pen.get("scores")}}
        tts = v.dir / "tts"
        if keep == "current":
            aside = _set_aside(v, tts / "pending", "turned-down")   # first: a refused move records nothing
            fsutil.write_text(aside / "verdict.json", json.dumps(entry, indent=1))
            cur.setdefault("verdicts", []).append(entry)
            fsutil.write_text(v.dir / "meta.json", json.dumps(v.to_json(), indent=2))
            return {"kept": "current", "verdict": entry, "state": ab_state(v.name)}
        new = dict(pen)
        new["verdicts"] = list((cur or {}).get("verdicts", [])) + [entry]
        if _pack_files(tts):
            _to_previous(v, cur and {**cur, "verdicts": new["verdicts"]})
        _move(v, tts / "pending", tts)
        v.extra["tts"] = new
        fsutil.write_text(v.dir / "meta.json", json.dumps(v.to_json(), indent=2))
    gpu.forget_voice(v.name)
    from ..core import voicepreview
    voicepreview.after_add(get_voice(v.name))   # it speaks its line with the pack it now has
    return {"kept": "pending", "verdict": entry, "state": ab_state(v.name)}


def record_verdict(voice_name: str, compared: list, winner: str, why: str, by: str = "user") -> dict:
    """A listening verdict (the packs or readings compared, which won, why) kept with the voice's cadence: the first
    data for a later "keep this one" A/B choice. compared / winner: pack ids (pack_id) or names like "kokoro_rvc"."""
    v = get_voice(voice_name)
    pack = (v.extra or {}).get("tts")
    if not pack:
        raise ModelError(f"{label_of(v.name)} has no cadence to record a verdict on.")
    pack.setdefault("verdicts", []).append({"date": time.strftime("%Y-%m-%d %H:%M"), "compared": list(compared),
                                            "winner": winner, "why": why, "by": by})
    fsutil.write_text(v.dir / "meta.json", json.dumps(v.to_json(), indent=2))
    return pack


def remove_pack(voice_name: str):
    """Delete a voice's cadence (SafeHouse); the voice itself stays."""
    v = get_voice(voice_name)
    shutil.rmtree(v.dir / "tts", ignore_errors=True)
    if (v.extra or {}).pop("tts", None) is not None:
        fsutil.write_text(v.dir / "meta.json", json.dumps(v.to_json(), indent=2))
    gpu.forget_voice(v.name)


def pack_size(v) -> int:
    d = v.dir / "tts"
    return sum(f.stat().st_size for f in d.rglob("*") if f.is_file()) if d.is_dir() else 0


# ---------------------------------------------------------------- the overnight queue's cadence items

TR_RT = 13.0   # transcription runs ~13x realtime on the GPU (phase 3)


def estimate(item: dict) -> dict:
    """Minutes: [find clips] + transcribe + measure + test the reference clips (four sentences each, 16-18 s a clip on
    a free GPU, measured in phase 9b; five clips + the live pack + loading the engine ~3 min; more when the GPU is shared)."""
    from .night import estimate as voice_estimate
    est = {"clips": 0, "transcribe": 0, "measure": 2, "test": 4}
    if item["kind"] == "newcad":
        v = voice_estimate({"kind": "new", "target": item["target"]})
        est.update(prepare=v["prepare"], train=v["train"], transcribe=3)
    else:
        try:
            v = get_voice(item["target"])
            proj = project_for_voice(v)
        except ModelError:
            proj = None
        if proj is None:
            est["clips"] = 15
            est["transcribe"] = 1
        else:
            done = transcripts(proj)
            todo = sum(p.dur for p in speech_clips(proj) if p.id not in done)
            est["transcribe"] = round(todo / 60 / TR_RT + 0.5)
    est["total"] = round(sum(v for v in est.values()))
    return est


def _target(t: str) -> str:
    v = get_voice(t)
    ok, why = can_train(v)
    if not ok:
        raise ModelError(f"Can't train a cadence for {label_of(v.name)}: {why}")
    return v.name


def _run(job, item) -> dict:
    return train(job, item["target"], worklist.pusher(job), worklist.control_for(job))


CADENCE_MIN = 9   # minutes a cadence adds after its voice (transcribe 3 + measure 2 + test 4, as estimate() says)


def cadence_after(v, asked: bool = False) -> tuple[bool, str]:
    """Does a voice just made get its cadence pack now? (yes?, why). Principle 10, the engine is smart: a voice made
    from speech reads in its own manner, not a stand-in Reader's (Trump's Do it all left SpeakEasy reading him with
    bf_emma, a British woman). A singing voice keeps the Reader. A retrained one waiting beside its voice ("-next")
    gets none: on Keep it takes that voice's cadence, or a cadence is queued for it then (improved.keep).
    asked: the cadence was asked for with the voice ("newcad"): made whatever the clips were."""
    ex = v.extra or {}
    if ex.get("pending_for"):
        return False, "it waits beside its voice for the blind A/B; its cadence comes when it's kept"
    if not ex.get("from_speech") and not asked:
        return False, "a singing voice: SpeakEasy reads it with a Reader"
    if pack_of(v):
        return False, "it has its cadence already"
    return can_train(v)


def chain_cadence(job, name: str, push, control=None, asked: bool = False) -> dict | None:
    """A voice's cadence right after the voice (Do it all, a queued new voice, newcad), when cadence_after says so.
    The voice is made by then, so a Pause or Stop here ends the job DONE, never undone or run again (a resumed item
    would train the whole voice again): Pause queues the cadence (held like the rest), Stop queues nothing.
    -> {cadence fields..., note}, {"note"} when paused / stopped, or None when there's none to make."""
    try:
        v = get_voice(name)
    except ModelError:   # not in the voices (nothing to read a cadence for)
        return None
    ok, _why = cadence_after(v, asked)
    if not ok:
        return None
    try:
        push("cadence", 0.0, f"Now {label_of(v.name)}'s cadence")
        c = train(job, v.name, push, control)
        freeze_test(v.name, push)   # R1: the voice's frozen test, now that its own lines can be read with its cadence
    except jobs.Paused:
        try:
            worklist.add("cadence", v.name)
            return {"note": "Paused before its cadence: it waits in tonight's queue."}
        except ModelError:
            return {"note": "Paused before its cadence: Train cadence on its card when you like."}
    except jobs.Cancelled:
        return {"note": "Stopped before its cadence: Train cadence on its card when you like."}
    return {**{k: c[k] for k in ("after", "likeness", "cadence_gap", "wer", "replaced", "data_minutes")
               if c.get(k) is not None}, "note": c.get("note", "")}


def freeze_test(name: str, push=None):
    """A voice's frozen test (phase 26, R1), made when its cadence is: never fails the job it rides on."""
    from . import frozen
    from ..core.speaker import SpeakerEmbedder
    try:
        v = get_voice(name)
        proj = project_for_voice(v)
        if proj is None or frozen.load_test(v):
            return
        conv = gpu.MODELS.converter
        frozen.ensure(v, proj, frozen.GpuEngine(conv, SpeakerEmbedder()),
                      push=lambda f, m: (push or (lambda *a, **k: None))("test", f, m))
    except (jobs.Cancelled, jobs.Paused):   # Stop means stop, and a Pause lands: never swallowed here
        raise
    except Exception as e:   # noqa: BLE001 - the test comes at the first Improve instead
        print(f"frozen test for {name}: not made now ({type(e).__name__}: {e}); the first Improve makes it", flush=True)


def _run_new(job, item) -> dict:
    """A new voice, then its cadence from the same clips (one item: the cadence follows its voice)."""
    from .night import _new
    r = _new(job, item)
    c = chain_cadence(job, r["voice"], worklist.pusher(job), worklist.control_for(job), asked=True)
    return {**r, "cadence": c, "note": r["note"] + (" " + c["note"] if c else "")} if c else r


def _follows(item: dict, other: dict) -> bool:
    """A voice's cadence runs after that voice's own queue job (improve / new), as designed."""
    from ..core.voices import slug
    if other["kind"] == "improve":
        return other["target"] == item["target"]
    if other["kind"] in ("new", "newcad"):
        return f"{slug(other['target'])}-uc" == item["target"]
    return False


def _slug(t: str) -> str:
    from ..core.voices import slug
    return slug(t)


def _shown_voice(item: dict) -> str:
    from .night import shown_voice
    return shown_voice(item)


worklist.register_kind("cadence", run=_run, estimate=estimate, target=_target, follows=_follows, shown=_shown_voice,
                       label=lambda item: f"Cadence for {_shown_voice(item)}")
def _live_person(person: str):
    from .night import live_person
    return live_person(person)


worklist.register_kind("newcad", run=_run_new, estimate=estimate,
                       label=lambda item: f"New voice with cadence: {item['target']}", person=_slug, live=_live_person)
