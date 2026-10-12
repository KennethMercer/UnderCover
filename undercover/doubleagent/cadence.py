"""DoubleAgent's cadence work (phase 4): transcripts of a project's clips, the person's measured cadence, and a
voice's cadence pack (the reference clip a cloning engine imitates, with the numbers SpeakEasy reads by).

No training: phase 3 found zero-shot Chatterbox + RVC closest to each person's delivery (docs/research/cadence.md),
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
from ..core.voices import ModelError, get_voice, label_of
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
# penalty in full, in the clips tried first and in the score (calibrated in docs/research/clip-sound.md: the muffled
# Freeman v2 clip loses to the approved one by it). Only new picks see it, and a new pick waits for the user's ear
# (tts/pending/) whenever the voice already has a cadence.
W_SOUND = 1.0
# A new pack is offered only when it beats the live one by more than the test's own noise: the same clip scores 0.02-0.03
# apart between runs (phase 9b: 0.668 and 0.694), and the best of five noisy scores is biased upward (was 0.005 with three)
KEEP_MARGIN = 0.02
# Phase 12: pace and pitch of a pack's readings against the person (calibrated in docs/research/pace-pitch.md)
PACE_MIN_N = 3       # a source counts toward the target pace once this many of its sentences were measured
PACE_DEAD = 0.06     # within 6% of their pace: no penalty (about the smallest tempo change people notice in speech)
W_PACE = 0.5         # score units per unit of log ratio past the dead zone (10% past it ~ 0.05)
PITCH_DEAD = 0.5     # semitones
W_PITCH = 0.05       # score units per semitone past the dead zone
CAP_PP = 0.15


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
    return [p for p in proj.phrases if p.kept and not p.sung]


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


def features(x16: np.ndarray, text: str, rmvpe) -> dict:
    """One utterance's cadence numbers (16 kHz audio and what it says)."""
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
    out = {"dur": round(dur, 2), "syl": syl, "rate": syl / dur, "artic": syl / max(0.3, dur - ptot),
           "pauses": 10 * len(pauses) / max(1, syl), "plist": [10 * p for p in pauses]}
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
    return round(sum(r["syl"] for r in rows if r.get("dur")) / dur, 3) if dur > 0 else None


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
    """A pack's readings against the person: pace as a ratio to their target pace, pitch in semitones from their
    median (positive = faster / higher), each with its penalty past a dead zone (phase 12)."""
    out = {"pace": pace, "f0_hz": f0_hz and round(f0_hz, 1)}
    target = pace_target(person)
    if pace and target:
        out["pace_target"] = target
        out["pace_ratio"] = round(pace / target, 3)
        out["pace_pen"] = round(min(CAP_PP, W_PACE * max(0.0, abs(math.log(pace / target)) - math.log(1 + PACE_DEAD))), 4)
    if f0_hz and person.get("f0_hz"):
        st = 12 * math.log2(f0_hz / person["f0_hz"])
        out["pitch_st"] = round(st, 2)
        out["pitch_pen"] = round(min(CAP_PP, W_PITCH * max(0.0, abs(st) - PITCH_DEAD)), 4)
    return out


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


def ref_candidates(proj, tr: dict, n: int = CANDIDATES, sound: bool = True) -> list:
    """Kept speech clips of 7-11 s, clean, clearly the person, plain transcript; best first, from different
    sources where possible (phase 3's pick_ref rule). sound: a clip that sounds muffled / noisy / roomy ranks lower
    (phase 9; False = the rule before it, for comparisons)."""
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
    out, seen = [], set()
    for p in cands:
        if p.source not in seen:
            out.append(p)
            seen.add(p.source)
        if len(out) >= n:
            return out
    return (out + [p for p in cands if p not in out])[:n]


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
        for i, (s, w) in enumerate(zip(TEST + PACE_TEST, wavs)):
            self.check()
            y, sr = render.finish(w, self.lv, self.conv, self.hz, tempo=tempo, nudge=nudge, keep_wav=True)
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
    if not C.engine_ready(C.default_engine()):
        return False, "The cadence engine isn't installed."
    proj = project_for_voice(v)
    if proj is not None:
        if proj.kept_seconds() - proj.sung_seconds() < MIN_MINUTES * 60:
            return False, (f"{proj.person}'s clips are mostly singing: a cadence needs a few minutes of them talking "
                           "(add interviews in DoubleAgent).")
        return True, "from the voice's clips"
    if ex.get("person") and not ex.get("trained"):   # a checked community voice; a trained voice with no clips left isn't searched for
        return True, f"finds clips of {ex['person']} first"
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
    if proj is None:
        person = None if (v.extra or {}).get("trained") else (v.extra or {}).get("person")
        if not person:
            raise ModelError(f"UnderCover doesn't know whose voice '{v.name}' is, so it can't find their clips.")
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
                best = m
            finally:
                _cleanup(wavs)
            tried.append({"clip": p.id, "source": p.source, "dur": round(p.dur, 2), "text": tr.get(p.id, ""),
                          "tempo1": m, **best})
        win = max(tried, key=lambda t: t["score"])
        # 4. the voice's current cadence, on the same test: keep the new one only if it's better
        old = pack_of(v)
        before = None
        if old:
            push("testing", len(cands) / (len(cands) + 1), "Comparing with the current cadence")
            try:
                wavs = tester.read(old, v.dir / "tts" / old.get("ref", "ref.wav"))
                try:
                    before = tester.measure(wavs, float(old.get("tempo", 1.0)), ref=v.dir / "tts" / old.get("ref", "ref.wav"))
                finally:
                    _cleanup(wavs)
            except (ModelError, C.WorkerError):
                before = None   # e.g. made with an engine that's gone: the new one replaces it
        mins = tr_info["minutes"]
        result = {"voice": v.name, "after": win["score"], "before": before and before["score"], "likeness": win["likeness"],
                  "cadence_gap": win["cadence_gap"], "wer": win["wer"], "data_minutes": mins, "engine": engine}
        if before is not None and win["score"] <= before["score"] + KEEP_MARGIN:
            result.update(replaced=False, note=f"Tried {len(tried)} reference clips; none beat the current cadence, so it stays.")
            return result
        if old is not None and win["clip"] == old.get("clip") and all(base[k] == old.get(k) for k in base):
            result.update(replaced=False, note="The best reference clip is the one it already speaks with, so the cadence stays.")
            return result
        # 5. save the pack with the voice. A voice that already speaks with a cadence keeps it: the new one waits in
        # tts/pending/ for the user's ear ("Keep this one", phase 9), also when measuring the current one failed this
        # time (a worker hiccup must not replace a pack the user approved). Only a current one whose engine is gone
        # is replaced at once, and even then it's moved aside (tts/previous/), never deleted.
        gaps = person.get("sentence_gap_s") or [0.3, 0.45, 0.8]
        gap = float(np.clip(gaps[1], 0.25, 1.0))
        waits = old is not None and C.engine_ready(old.get("engine") or engine)
        pack = {**base, "v": VERSION, "ref": "ref.wav", "ref_text": win["text"], "clip": win["clip"], "project": proj.name,
                "tempo": win["tempo"], "gap_s": round(gap, 3), "para_gap_s": round(max(0.7, gap * 1.6), 3),
                "data_minutes": mins,
                "scores": {k: win.get(k) for k in ("likeness", "wer", "cadence_gap", "rate", "score", "score_voice", "sound")},
                "person": {k: person.get(k) for k in ("rate", "pauses", "pause_ms", "range_st", "sentences", "sentence_gap_s")},
                "before": before and {k: before.get(k) for k in ("likeness", "wer", "cadence_gap", "score", "score_voice", "sound")},
                "made": time.strftime("%Y-%m-%d %H:%M"), "verdicts": []}
        new = work / "pack"
        new.mkdir(exist_ok=True)
        shutil.copyfile(work / f"{win['clip']}.wav", new / "ref.wav")
        fsutil.write_text(new / "cadence.json", json.dumps({**pack, "tried": tried}, indent=1))
        if save_pack(v.name, new, pack, waits) == "pending":
            how = (f"that measures better ({before['score']:.3f} -> {win['score']:.3f})" if before is not None else
                   "(the current one couldn't be measured this time)")
            result.update(replaced=False, pending=True,
                          note=f"Found a cadence for {label_of(v.name)} {how}. Listen to both and keep the one you like: "
                               "the current one stays until you choose.")
            return result
        result.update(replaced=True, note=(f"{v.name} now speaks with {proj.person}'s cadence" if old is None else
                                           f"New cadence for {v.name} (the old one couldn't be read; it's kept in tts/previous)")
                      + f" (from {mins} min of their speech).")
        return result
    finally:
        shutil.rmtree(work, ignore_errors=True)


def pack_id(pack: dict) -> str:
    """Names a pack in verdicts: its reference clip and settings."""
    return f"{pack.get('engine')}:{pack.get('clip')}:cfg{pack.get('cfg_weight')}:t{pack.get('tempo')}"


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
                          "approved": approved_by_user(cur), "sample": _sample_ok(tts, ci)}
    if pen:
        out["pending"] = {"id": pi, "clip": pen.get("clip"), "made": pen.get("made"), "scores": pen.get("scores"),
                          "current_same_test": pen.get("before"), "sample": _sample_ok(tts / "pending", pi)}
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
    from . import service
    v = get_voice(voice_name)
    cur, pen = pack_of(v), pending_of(v)
    if not (cur and pen):
        raise ModelError(f"{label_of(v.name)} has no new cadence waiting to compare.")
    ids = _ids(cur, pen)
    tts = v.dir / "tts"
    todo = [(d, pack, pid) for d, pack, pid in ((tts, cur, ids[0]), (tts / "pending", pen, ids[1])) if not _sample_ok(d, pid)]
    conv = gpu.MODELS.converter
    lv = conv.voice(v.name)
    sp = service.speaking_pitch(v)
    for i, (d, pack, pid) in enumerate(todo):
        if job.cancel:
            raise Cancelled()
        job.push(stage="reading", frac=i / max(1, len(todo)), message=f"Reading the line with the {'new' if d != tts else 'current'} cadence")
        fut, wav = render.request(AB_LINE, pack, d / pack.get("ref", "ref.wav"), seed=AB_SEED)
        render.client.wait(fut, cancel=lambda: job.cancel)
        y, sr = render.finish(wav, lv, conv, sp and sp["hz"], tempo=float(pack.get("tempo", 1.0)))
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
    return {"kept": "pending", "verdict": entry, "state": ab_state(v.name)}


def record_verdict(voice_name: str, compared: list, winner: str, why: str, by: str = "user") -> dict:
    """A listening verdict (the packs or readings compared, which won, why) kept with the voice's cadence: the first
    data for a later "keep this one" A/B choice. compared / winner: pack ids (pack_id) or names like "kokoro_rvc"."""
    v = get_voice(voice_name)
    pack = (v.extra or {}).get("tts")
    if not pack:
        raise ModelError(f"{v.name} has no cadence to record a verdict on.")
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
        raise ModelError(f"Can't train a cadence for {v.name}: {why}")
    return v.name


def _run(job, item) -> dict:
    return train(job, item["target"], worklist.pusher(job), worklist.control_for(job))


def _run_new(job, item) -> dict:
    """A new voice, then its cadence from the same clips (one item: the cadence follows its voice)."""
    from .night import _new
    r = _new(job, item)
    from ..core.voices import label_of
    job.push(stage="cadence", frac=0.0, message=f"Now {label_of(r['voice'])}'s cadence")
    c = train(job, r["voice"], worklist.pusher(job), worklist.control_for(job))
    return {**r, "cadence": {k: c.get(k) for k in ("after", "likeness", "cadence_gap", "wer", "replaced", "data_minutes")},
            "note": r["note"] + " " + c.get("note", "")}


def _follows(item: dict, other: dict) -> bool:
    """A voice's cadence runs after that voice's own queue job (improve / new), as designed."""
    from ..core.voices import slug
    if other["kind"] == "improve":
        return other["target"] == item["target"]
    if other["kind"] in ("new", "newcad"):
        return f"{slug(other['target'])}-uc" == item["target"]
    return False


def _shown_voice(item: dict) -> str:
    from .night import shown_voice
    return shown_voice(item)


worklist.register_kind("cadence", run=_run, estimate=estimate, target=_target, follows=_follows, shown=_shown_voice,
                       label=lambda item: f"Cadence for {_shown_voice(item)}")
worklist.register_kind("newcad", run=_run_new, estimate=estimate,
                       label=lambda item: f"New voice with cadence: {item['target']}")
