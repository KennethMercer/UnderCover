"""Improve that improves (phase 26): the frozen test per voice, the score as a vector, the offer bar, the recipe and the
budget, and the source gate. Built from the voice lab's research (docs/research/phase26-improve.md, R1-R8), whose ground
truth is eleven blind verdicts.

The user's words: "re-training shouldn't be able to make it worse like that". So:

  R1  Every voice gets ONE frozen test (voices/<name>/test/): a donor pool of other people's clean clips (same sex) and
      the person's own transcribed lines, read once through their cadence pack (the raw takes are frozen, so every
      candidate converts the same reading). Made lazily, at the first Improve; it stays for the life of the voice.
  R2  A score is a vector per input (likeness to the person, harmonicity, spectral contrast, noise, spectrum distance,
      content survival), compared as PAIRED differences with a bootstrap interval, never a mean of a few phrases.
  R3  A candidate is offered only when, on the person's own lines, likeness isn't worse (0.015 band) AND harmonicity
      is up by 0.05 AND spectral contrast by 0.35, each outside its interval, with donor likeness as a guard. Fitted
      11 of 11 on the record; a false offer costs more than a missed improvement, so tighten these, never loosen.
  R4  Refine (continue from the approved voice's weights) for a well-fed voice; scratch with the scaled budget for a
      thin one or a big data jump. The user sees one "Improve".
  R5  The training budget scales with the data (450 steps per kept minute, floor 8000, continue while the held-out
      loss falls, cap 2x). New voices too.
  R6  A source joins only if its kept phrases sit near the approved voice, in its register, and the pipeline kept more
      than a tenth of it.
  R7  The Improve A/B reads two of the frozen test's own lines through each voice.
  R8  Hygiene: scores from different tests never compare; a scratch retrain is a new voice of the same person.

The GPU sits behind `Engine` (pitch, convert, embed, content, read), so everything here runs on fixed numbers in
tools/smoke.py. Nothing here deletes a voice: a candidate that isn't offered is set aside by its caller.
"""
import hashlib
import json
import math
import time
from pathlib import Path

import numpy as np

from ..core import fsutil
from ..core.config import CONFIG, path
from ..core.voices import ModelError, label_of

# ---------------------------------------------------------------- the numbers (module constants: principle 10, no settings)

LIKENESS_BAND = 0.015    # the seed-to-seed band of a scratch retrain on identical data (own lines 0.010, donors 0.014)
HNR_MIN = 0.05           # harmonicity gain the two kept voices showed (+0.058, +0.096); the rejected Obama refine +0.020
CONTRAST_MIN = 0.35      # spectral-contrast gain (+0.40, +0.59 kept; the rejected Obama candidate +0.34)
BOOT = 2000              # bootstrap resamples; 90 % interval (5 / 95)

WELL_FED_MIN = 8.0       # minutes: a voice with less learns too little from a refine (SpongeBob 7 min, Griffin 3.5)
WELL_FED_LIKENESS = 0.6
REFINE_MAX_RATIO = 1.5   # new kept minutes over the old: above this, scratch (SpongeBob x1.8, Griffin x5, Obama x2.2)
REFINE_LR = 2e-5
REFINE_STEPS_PER_MIN = 2000 / 15   # about 2000 steps for 10-20 min of data, scaled; clipped below
REFINE_STEPS = (1000, 4000)

STEPS_PER_MIN = 450      # the fifth cause: 8000 fixed steps gave Obama's 130 min 8 passes; Freeman's 18 min 49
STEPS_MIN = 8000
BUDGET_CAP = 2.0         # keep going while the held-out loss falls, up to this many times the planned budget

GATE = {"cos": 0.62, "med": 0.50, "reg_st": 5.0, "drop_share": 0.90}
GATE_MIN_KEPT_MIN = 3.0  # a gate that leaves less than this can't train: "tried, nothing usable"

POOL_PER_SEX = 36        # donor clips wanted (the lab froze 36-40); below MIN the donor guard is skipped, said so
POOL_MIN = 12
MALE_HZ = 165.0          # a donor's median pitch under this = male (the lab's split)
N_LINES = 12             # the person's own lines (the lab read 9-14); the bar needs LINES_MIN
LINES_MIN = 4
TEST_VERSION = 1


# ---------------------------------------------------------------- R5: the budget, R4: the recipe (pure)

def budget_steps(kept_minutes: float) -> tuple[int, int]:
    """(planned steps, cap) for an auto-length training on this much kept speech: 450 a minute, floor 8000, and the
    run may go on to the cap while its held-out loss still falls."""
    planned = int(max(STEPS_MIN, round(STEPS_PER_MIN * max(0.0, kept_minutes))))
    return planned, int(round(planned * BUDGET_CAP))


def refine_steps(new_minutes: float) -> int:
    lo, hi = REFINE_STEPS
    return int(np.clip(round(REFINE_STEPS_PER_MIN * max(0.0, new_minutes)), lo, hi))


NOTHING_NEW_MIN = 0.5   # kept minutes grown by less than this, nothing newly prepared, nothing left out: the same clips again


def nothing_new(kept_minutes: float, old_minutes: float | None, left_out: list, new_ready: bool) -> bool:
    """True when an Improve would train on exactly the clips the approved voice learnt from (no new clips found or
    prepared, nothing left out by the gate): training again can only shuffle the dice, so the voice is kept untried.
    An unknown old minute count (0 / None) is never "nothing new": the voice's own numbers aren't known."""
    old = float(old_minutes or 0.0)
    if old <= 0 or left_out or new_ready:
        return False
    return kept_minutes - old < NOTHING_NEW_MIN


def recipe(old_minutes: float | None, old_likeness: float | None, kept_minutes: float) -> tuple[str, str]:
    """("refine" | "scratch", why). Refine keeps the approved identity (Freeman, Obama: weights 2 % away, drift 0.10);
    a thin voice or a big data jump learns too little from it (SpongeBob, Griffin: the user kept scratch retrains).
    An unknown likeness counts as fine: refine is the path that can't wander off."""
    old = float(old_minutes or 0.0)
    if old < WELL_FED_MIN:
        return "scratch", f"the current voice has only {old:.0f} min behind it: a fresh training learns the new clips"
    if old_likeness is not None and old_likeness < WELL_FED_LIKENESS:
        return "scratch", f"the current voice's likeness is {old_likeness:.2f}: a fresh training can do better"
    ratio = kept_minutes / old if old else 99.0
    if ratio >= REFINE_MAX_RATIO:
        return "scratch", f"the clips grew from {old:.0f} to {kept_minutes:.0f} min: a fresh training uses them all"
    return "refine", f"continues from your voice ({old:.0f} min, kept) on {kept_minutes:.0f} min of clips"


# ---------------------------------------------------------------- R2 / R3: paired differences and the bar (pure)

def paired(diffs, seed: int = 0) -> dict | None:
    """{mean, lo, hi, n} of paired differences (candidate minus approved) with a 90 % bootstrap interval over inputs."""
    d = np.asarray([x for x in diffs if x is not None and np.isfinite(x)], dtype=float)
    if len(d) == 0:
        return None
    rng = np.random.default_rng(seed)
    boots = rng.choice(d, (BOOT, len(d))).mean(1) if len(d) > 1 else np.repeat(d.mean(), BOOT)
    lo, hi = np.percentile(boots, [5, 95])
    return {"mean": round(float(d.mean()), 4), "lo": round(float(lo), 4), "hi": round(float(hi), 4), "n": int(len(d))}


MEASURES = ("id", "cent", "hnr", "contrast", "noise", "ltas", "content")
UP = {"id": True, "cent": True, "hnr": True, "contrast": True, "noise": False, "ltas": False, "content": True}


def compare(approved: dict, candidate: dict) -> dict:
    """The candidate against the approved voice on the same frozen test: per class (lines / donors), per measure, the
    paired difference with its interval. Inputs are matched by file; an input one of them lacks is left out."""
    out = {}
    for cls in ("lines", "donors"):
        a = {r["file"]: r for r in approved.get(cls) or [] if "id" in r}
        b = {r["file"]: r for r in candidate.get(cls) or [] if "id" in r}
        files = sorted(set(a) & set(b))
        out[cls] = {"n": len(files)}
        for k in MEASURES:
            d = [b[f][k] - a[f][k] for f in files if b[f].get(k) is not None and a[f].get(k) is not None]
            if d:
                out[cls][k] = paired(d)
    return out


def offer(cmp: dict, test: dict | None = None) -> dict:
    """R3, the conservative bar, on the person's own lines: likeness not worse by more than the band; harmonicity and
    spectral contrast both up by their margins and outside their intervals; donor likeness not worse (the guard).
    -> {offer: bool, reasons: [why not], words: what changed (R3a), terms: {name: pass}}."""
    lines, donors = cmp.get("lines") or {}, cmp.get("donors") or {}
    terms, why, words = {}, [], []
    if lines.get("n", 0) < LINES_MIN:
        why = ("no spoken lines to judge a singer by yet" if (test or {}).get("singer")
               else "too few of their own lines to judge on" + (f" ({(test or {}).get('lines_why')})" if (test or {}).get("lines_why") else ""))
        return {"offer": False, "reasons": [why], "words": [], "terms": {}}
    idl = lines.get("id")
    terms["likeness"] = bool(idl and idl["mean"] > -LIKENESS_BAND)
    if not terms["likeness"]:
        why.append("the likeness slipped")
    elif idl and idl["lo"] > LIKENESS_BAND:
        words.append("a closer likeness")
    else:
        words.append("the likeness is unchanged")
    h = lines.get("hnr")
    terms["harmonicity"] = bool(h and h["mean"] >= HNR_MIN and h["lo"] > 0)
    if terms["harmonicity"]:
        words.insert(0, "cleaner voicing")
    else:
        why.append("the voicing isn't clearer")
    c = lines.get("contrast")
    terms["contrast"] = bool(c and c["mean"] >= CONTRAST_MIN and c["lo"] > 0)
    if terms["contrast"]:
        words.insert(len(words) - 1, "more detail")
    else:
        why.append("no more detail in it")
    idd = donors.get("id")
    if donors.get("n", 0) >= POOL_MIN and idd is not None:
        terms["donor_guard"] = bool(idd["mean"] > -LIKENESS_BAND)
        if not terms["donor_guard"]:
            why.append("on other people's voices the likeness slipped")
    ok = all(terms.values())
    return {"offer": ok, "reasons": [] if ok else why, "words": words if ok else [], "terms": terms}


def words_of(verdict: dict) -> str:
    """R3a: what changed, in words, when a candidate is offered ("cleaner voicing, more detail; the likeness is
    unchanged")."""
    w = list(verdict.get("words") or [])
    if not w:
        return ""
    tail = w.pop() if w and w[-1].startswith(("the likeness", "a closer")) else ""
    return (", ".join(w) + ("; " if w and tail else "") + tail).strip()


# ---------------------------------------------------------------- R6: the source gate (pure, given the measures)

def gate_rows(rows: list[dict], voice_hz: float | None) -> list[dict]:
    """Mark each source row pass / why it fails against the approved voice (its pitch) and the person's reference:
    rows carry kept_min, cos, med_score, dropped and (when measured) f0_st. -> the rows, with `gate` and `pass`."""
    v_st = 12 * math.log2(voice_hz / 100.0) if voice_hz else None
    for r in rows:
        why = []
        if not r.get("kept_min", 0) > 0 or r.get("cos") is None or r["cos"] != r["cos"]:
            why.append("nothing usable")
        else:
            if r["cos"] < GATE["cos"]:
                why.append("another voice, or too far from theirs")
            if r.get("med_score", 1.0) < GATE["med"]:
                why.append("too few phrases like them")
            if v_st is not None and r.get("f0_st") is not None and abs(r["f0_st"] - v_st) > GATE["reg_st"]:
                why.append(("a much younger voice" if r["f0_st"] - v_st > GATE["reg_st"] else "a deeper, calmer register")
                           if abs(r["f0_st"] - v_st) > 7 else ("a higher register" if r["f0_st"] > v_st else "a lower register"))
            if r.get("dropped", 0.0) > GATE["drop_share"]:
                why.append("almost nothing of it was usable")
        r["gate"] = ", ".join(why) or "pass"
        r["pass"] = not why
    return rows


def source_table(proj, emb: dict) -> list[dict]:
    """Per source of a project: kept minutes, the median phrase score, the mean cosine of its kept phrases' embeddings
    against the person's reference, and the share the pipeline dropped (improve_sources.table, in production)."""
    key = f"speaker:{proj.reference.get('cluster')}"
    ref = emb.get(key)
    if ref is None:
        return []
    ref = ref / (np.linalg.norm(ref) + 1e-9)
    by: dict = {}
    for ph in proj.phrases:
        by.setdefault(ph.source, []).append(ph)
    rows = []
    for s in proj.sources:
        ph = by.get(s.id, [])
        if not ph:
            continue
        kept = [q for q in ph if q.kept]
        e = [emb[q.id] / (np.linalg.norm(emb[q.id]) + 1e-9) for q in kept if q.id in emb]
        rows.append({"id": s.id, "title": s.title, "kept_min": sum(q.dur for q in kept) / 60, "phrases": len(ph),
                     "med_score": float(np.median([q.score for q in ph])), "cos": float(np.mean([x @ ref for x in e])) if e else None,
                     "dropped": 1 - len(kept) / len(ph), "selected": bool(s.selected), "user": bool(s.user)})
    return rows


def gate_project(proj, emb: dict, voice_hz: float | None, pitch_of=None, per_source: int = 12, by: str = "Improve") -> dict:
    """R6 on a project, after score(): sources that fail are unticked with the reason, and their kept phrases marked
    left out (unticking a source alone leaves its cut phrases in training). The user's own sources and phrases
    (user=True / choice=True) are never touched. pitch_of(clip path) -> median pitch Hz, for the register term.
    -> {left_out: [{title, why}], kept_min, rows}."""
    rows = source_table(proj, emb)
    if pitch_of is not None:
        of: dict = {}
        for ph in proj.phrases:
            if ph.kept:
                of.setdefault(ph.source, []).append(ph)
        for r in rows:
            hz = []
            for ph in sorted(of.get(r["id"], []), key=lambda q: -q.score)[:per_source]:
                try:
                    h = pitch_of(proj.clip_path(ph.id))
                except Exception:   # noqa: BLE001 - an unreadable clip says nothing about the register
                    h = None
                if h:
                    hz.append(12 * math.log2(h / 100.0))
            if hz:
                r["f0_st"] = float(np.median(hz))
    gate_rows(rows, voice_hz)
    left = []
    for r in rows:
        if r["pass"] or r.get("user") or not r.get("selected", True):
            continue   # (one unticked already, by the user or an earlier gate, stays as it is: it doesn't train)
        src = proj.source(r["id"])
        src.selected = False
        src.note = f"left out by {by}: {r['gate']}"
        src.extra["gate"] = r["gate"]
        for ph in proj.phrases:
            if ph.source == r["id"] and ph.kept and ph.choice is None:
                ph.reason = f"source left out: {r['gate']}"
        left.append({"title": r["title"], "why": r["gate"], "minutes": round(r["kept_min"], 1)})
    return {"left_out": left, "kept_min": round(proj.kept_seconds() / 60, 1), "rows": rows}


# ---------------------------------------------------------------- the measures on one output (CPU)

def ltas(x16: np.ndarray) -> np.ndarray:
    """The long-term average log-mel spectrum of the voiced part of a clip (64 bands): a timbre fingerprint."""
    import librosa
    m = librosa.feature.melspectrogram(y=x16, sr=16000, n_fft=1024, hop_length=160, n_mels=64, fmin=50, fmax=7800)
    lm = np.log(m + 1e-8)
    e = lm.mean(0)
    keep = e > np.percentile(e, 30)
    return lm[:, keep].mean(1) if keep.any() else lm.mean(1)


def features(x16: np.ndarray, f0: np.ndarray) -> dict:
    """What the ear hears that likeness doesn't: harmonicity (periodicity of the voiced frames: solid, clean voicing)
    and spectral contrast (harmonic peaks over valleys: detail an under-trained vocoder smears into a hiss)."""
    import librosa
    v = f0 > 0
    hn = []
    for i in np.flatnonzero(v)[::10][:150]:
        seg = x16[i * 160:i * 160 + 640]
        if len(seg) < 640:
            continue
        seg = seg - seg.mean()
        ac = np.correlate(seg, seg, "full")[len(seg) - 1:]
        lag = int(16000 / max(f0[i], 50))
        if lag + 4 < len(ac) and ac[0] > 1e-9:
            hn.append(float(ac[lag - 3:lag + 4].max() / ac[0]))
    sc = librosa.feature.spectral_contrast(y=x16, sr=16000, n_fft=1024, hop_length=160, n_bands=6)
    tc = sc.shape[1]
    vc = np.zeros(tc, bool)
    vc[:min(tc, len(v))] = v[:tc]
    return {"hnr": float(np.median(hn)) if hn else 0.0, "contrast": float(sc[1:4][:, vc].mean()) if vc.sum() > 20 else 0.0}


def resample16(y: np.ndarray, sr: int) -> np.ndarray:
    if sr == 16000:
        return y.astype(np.float32)
    from scipy.signal import resample_poly
    g = math.gcd(int(sr), 16000)
    return resample_poly(y, 16000 // g, int(sr) // g).astype(np.float32)


# ---------------------------------------------------------------- the engine (the GPU behind one small interface)

class GpuEngine:
    """The app's converter, pitch tracker, HuBERT and speaker embedder, as the frozen test uses them."""

    def __init__(self, conv, emb):
        self.conv, self.emb = conv, emb

    def pitch(self, x16):
        return self.conv.rmvpe.infer_from_audio(x16, thred=0.03)

    def load_voice(self, voice):
        from ..core.engine.convert import LoadedVoice
        return LoadedVoice(voice, self.conv.device, self.conv.half)

    def convert(self, x16, lv, shift: float):
        from ..core.engine.convert import Settings
        return self.conv.convert(x16, lv, Settings(pitch=shift))

    def embed(self, x16):
        return self.emb.embed(x16)

    def content(self, x16):
        import torch
        dt = torch.float16 if self.conv.half else torch.float32
        with torch.no_grad():
            return self.conv.hubert.extract(torch.from_numpy(x16).to(self.conv.device, dt)[None], "v2")[0].float().cpu().numpy()

    def read(self, text: str, voice, proj, seed: int) -> tuple[np.ndarray, int, str]:
        """The person's line, read by the cadence engine: through their cadence pack when they have one, else from the
        project's own reference clip (what a pack is built from). -> (audio, rate, how). Raises when no engine is
        installed: the test then has no own lines, and says so."""
        from ..core.cadence import client, default_engine, pack_engine, pack_ready, render
        from .cadence import transcripts
        from .service import cadence_pack, cadence_ref
        import soundfile as sf
        pack = cadence_pack(voice)
        if pack and pack_ready(pack_engine(pack)):
            ref, how = cadence_ref(voice), f"pack:{pack_engine(pack)}"
        else:
            clip = (proj.reference or {}).get("clip")
            if not clip or not proj.clip_path(clip).exists() or not pack_ready(default_engine()):
                raise ModelError("No cadence engine to read their lines with.")
            ref, how = proj.clip_path(clip), f"clip:{default_engine()}"
            pack = {"engine": default_engine(), "ref_text": (transcripts(proj).get(clip) or "")}
        cfg = CONFIG.get("cadence") or {}
        full = {**pack, "cfg_weight": cfg.get("cfg_weight"), "exaggeration": cfg.get("exaggeration")}
        fut, wav = render.request(text, full, ref, seed=seed)
        client.wait(fut)
        try:
            y, sr = sf.read(str(wav), dtype="float32", always_2d=True)
        finally:
            Path(wav).unlink(missing_ok=True)
        return y.mean(1), int(sr), how

    def transcribe(self, files: list) -> list[str]:
        from ..core.cadence import client
        out = client.transcribe(files)
        return [(r.get("text") if isinstance(r, dict) else str(r or "")).strip() for r in out]


# ---------------------------------------------------------------- R1: the frozen test, on disk

def test_dir(voice) -> Path:
    return Path(voice.dir) / "test"


def model_hash(voice) -> str:
    h = hashlib.sha1()
    with open(voice.pth, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()[:12]


def load_test(voice) -> dict | None:
    f = test_dir(voice) / "test.json"
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_json(p: Path, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    fsutil.write_text(p, json.dumps(obj, indent=1))


def approved_score(voice) -> dict | None:
    t = load_test(voice)
    if not t or not t.get("approved"):
        return None
    f = test_dir(voice) / "scores" / f"{t['approved']}.json"
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _load16(f: Path) -> np.ndarray:
    import soundfile as sf
    x, sr = sf.read(str(f), dtype="float32", always_2d=True)
    return resample16(x.mean(1), sr)


def donor_candidates(own_project: str | None) -> list[tuple[str, str, dict, Path]]:
    """Other people's clean clips from every project but the person's own: "other speaker" phrases, 3-8 s, clearly not
    the project's person (score < 0.2), quiet (noise <= 0.15), at most two per source. (project name, person, phrase, clip)."""
    out = []
    root = path("cache_dir") / "training"
    for pj in sorted(root.iterdir()) if root.is_dir() else []:
        if pj.name == own_project or not (pj / "project.json").exists():
            continue
        try:
            p = json.loads((pj / "project.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        per_source: dict = {}
        for ph in sorted(p.get("phrases") or [], key=lambda q: -q.get("dur", 0)):
            if ph.get("reason") != "other speaker" or not 3.0 <= ph.get("dur", 0) <= 8.0 or ph.get("score", 1) >= 0.2 \
                    or ph.get("noise", 0) > 0.15 or per_source.get(ph["source"], 0) >= 2:
                continue
            c = next((x for x in (pj / "clips" / f"{ph['id']}.flac", pj / "clips" / f"{ph['id']}.wav") if x.exists()), None)
            if c is None:
                continue
            out.append((pj.name, p.get("person") or pj.name, ph, c))
            per_source[ph["source"]] = per_source.get(ph["source"], 0) + 1
    return out


def own_line_candidates(proj, texts: dict, pack_clip: str | None, n: int = N_LINES * 2) -> list[dict]:
    """The person's best kept speech phrases to read back: 3-9 s, score >= 0.6, not a training holdout, not the pack's
    reference, at most three per source, in a fixed order (sha1 of the id). texts: transcripts (may be empty)."""
    hold = set(proj.train.get("holdout") or [])
    good = [q for q in proj.training_phrases() if not q.sung and q.score >= 0.6 and 3 <= q.dur <= 9
            and q.id not in hold and q.id != pack_clip]
    good.sort(key=lambda q: hashlib.sha1(q.id.encode()).hexdigest())
    seen, out = {}, []
    for q in good:
        if seen.get(q.source, 0) >= 3:
            continue
        t = (texts.get(q.id) or "").strip()
        if t and not 6 <= len(t.split()) <= 24:
            continue
        seen[q.source] = seen.get(q.source, 0) + 1
        out.append({"id": q.id, "text": t})
        if len(out) >= n:
            break
    return out


def freeze(voice, proj, engine, push=None) -> dict:
    """R1: make the voice's frozen test (idempotent: an existing one is returned). Needs the project's embeddings and
    clips (the references), other projects' clips (the donors) and the person's lines read once (the engine)."""
    from .cadence import transcripts
    push = push or (lambda *a, **k: None)
    t = load_test(voice)
    if t:
        return t
    d = test_dir(voice)
    ex = voice.extra or {}
    tgt_hz = float(ex.get("f0_median") or 0) or None
    emb = proj.load_embeddings()
    key = f"speaker:{proj.reference.get('cluster')}"
    if key not in emb:
        raise ModelError(f"{label_of(voice.name)}'s project has no reference voice yet, so no test can be frozen.")
    ref = emb[key] / (np.linalg.norm(emb[key]) + 1e-9)
    # the person's references: embeddings and spectra of up to 40 of their best real clips, spread over sources
    push(0.0, "Freezing the test: their real clips")
    good = sorted([q for q in proj.training_phrases() if q.score >= 0.6 and 3 <= q.dur <= 9], key=lambda q: -q.score)
    seen, picks = {}, []
    for q in good:
        if seen.get(q.source, 0) >= 4:
            continue
        seen[q.source] = seen.get(q.source, 0) + 1
        picks.append(q)
        if len(picks) >= 40:
            break
    E, L, hz = [], [], []
    for q in picks:
        c = proj.clip_path(q.id)
        if not c.exists():
            continue
        x = _load16(c)
        E.append(engine.embed(x))
        L.append(ltas(x))
        f = engine.pitch(x)
        f = f[f > 0]
        if len(f) > 20:
            hz.append(float(np.median(f)))
    if len(E) < 5:
        raise ModelError(f"Too few clean clips of {proj.person or voice.name} are left to freeze a test on.")
    if not tgt_hz:
        tgt_hz = float(np.median(hz)) if hz else 120.0
    sex = "male" if tgt_hz < MALE_HZ + 10 else "female"
    d.mkdir(parents=True, exist_ok=True)
    np.savez(d / "refs.npz", ref=ref, real_emb=np.array(E), real_ltas=np.array(L))
    # the donor pool: other people's clean clips of the same sex, pitched once here
    push(0.2, "Freezing the test: other people's voices")
    import soundfile as sf
    rng = np.random.default_rng(26)
    cands = donor_candidates(ex.get("project") or proj.name)
    rng.shuffle(cands)
    donors, per_project = [], {}
    person_last = (proj.person or "").lower().split()[-1:] if proj.person else []
    (d / "donors").mkdir(exist_ok=True)
    for pname, person, ph, c in cands:
        if len(donors) >= POOL_PER_SEX:
            break
        if person_last and person.lower().split()[-1:] == person_last:
            continue
        if per_project.get(pname, 0) >= 10:
            continue
        x = _load16(c)
        f = engine.pitch(x)
        f = f[f > 0]
        if len(f) < 50:
            continue
        h = float(np.median(f))
        if ("male" if h < MALE_HZ else "female") != sex:
            continue
        name = f"{pname}__{ph['id']}.wav"
        sf.write(str(d / "donors" / name), x, 16000, subtype="PCM_16")
        donors.append({"file": name, "project": pname, "person": person, "dur": round(len(x) / 16000, 2), "f0": round(h, 1)})
        per_project[pname] = per_project.get(pname, 0) + 1
    # the person's own lines, read once; transcribed here when the project hasn't (the test is the voice's data)
    push(0.5, f"Freezing the test: {proj.person or label_of(voice.name)}'s own lines")
    texts = transcripts(proj)
    pack_clip = ((ex.get("tts") or {}).get("clip")) if isinstance(ex.get("tts"), dict) else None
    cands_l = own_line_candidates(proj, texts, pack_clip)
    todo = [q for q in cands_l if not q["text"]]
    if todo:
        try:
            heard = engine.transcribe([str(proj.clip_path(q["id"])) for q in todo])
            for q, h in zip(todo, heard):
                q["text"] = h if 6 <= len(h.split()) <= 24 else ""
        except Exception:   # noqa: BLE001 - no transcription: only the lines the project knows
            pass
    lines, how = [], None
    (d / "lines").mkdir(exist_ok=True)
    for i, q in enumerate([q for q in cands_l if q["text"]][:N_LINES]):
        try:
            y, sr, how = engine.read(q["text"], voice, proj, 1234 + i)
        except Exception as e:   # noqa: BLE001 - the engine refused a line: the test has one fewer
            how = how or f"failed: {type(e).__name__}"
            continue
        if y is None or len(y) < sr // 4:
            continue
        f = f"{i:02d}.wav"
        sf.write(str(d / "lines" / f), np.asarray(y, dtype=np.float32), int(sr), subtype="PCM_16")
        lines.append({"id": q["id"], "text": q["text"], "file": f, "sr": int(sr)})
    singer = not any(not q.sung for q in proj.training_phrases())   # a singer: no speech of theirs to read back (a known gap)
    t = {"version": TEST_VERSION, "made": time.strftime("%Y-%m-%d %H:%M"), "voice": voice.name, "project": proj.name,
         "sex": sex, "tgt_hz": round(tgt_hz, 1), "donors": donors, "lines": lines, "read_with": how, "approved": None,
         "singer": singer, "lines_why": (None if len(lines) >= LINES_MIN else "no spoken clips of theirs" if singer
                                         else "no cadence engine to read them" if how and how.startswith("failed") else
                                         "too few transcribed clips" if not cands_l else None),
         "notes": ([] if len(donors) >= POOL_MIN else [f"only {len(donors)} other voices: the donor guard is skipped"])
                  + ([] if len(lines) >= LINES_MIN else [f"only {len(lines)} of their own lines: Improve can't judge on them"])}
    _write_json(d / "test.json", t)
    return t


def score_voice(test_voice, voice, engine, push=None) -> dict:
    """R2: the voice's score vector on test_voice's frozen test, saved by its model hash (so it never mixes with a
    score from another test or another model). Donors convert to the APPROVED voice's pitch (candidates alike); the
    person's own lines to this voice's own pitch, as SpeakEasy reads them."""
    from .project import noisiness
    push = push or (lambda *a, **k: None)
    d = test_dir(test_voice)
    t = load_test(test_voice)
    if not t:
        raise ModelError("No frozen test for this voice yet.")
    h = model_hash(voice)
    out = d / "scores" / f"{h}.json"
    if out.exists():
        try:
            return json.loads(out.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    with np.load(d / "refs.npz") as z:   # closed at once: an open handle would block the folder moves Keep makes
        ref, real_emb, real_ltas = z["ref"], z["real_emb"], z["real_ltas"].mean(0)
    tgt_hz = float(t["tgt_hz"])
    own_hz = float((voice.extra or {}).get("f0_median") or tgt_hz)
    lv = engine.load_voice(voice)
    rows, lines = [], []
    total = len(t["donors"]) + len(t["lines"])
    try:
        for k, dn in enumerate(t["donors"]):
            push(k / max(1, total), f"Scoring {label_of(voice.name)} on other voices ({k + 1}/{len(t['donors'])})")
            x = _load16(d / "donors" / dn["file"])
            f0_in = engine.pitch(x)
            f = f0_in[f0_in > 0]
            shift = float(np.clip(12 * round(math.log2(tgt_hz / float(np.median(f)))), -24, 24)) if len(f) > 20 else 0.0
            y, sr = engine.convert(x, lv, shift)
            if y is None or not np.isfinite(y).all() or np.abs(y).max() < 1e-3:
                rows.append({"file": dn["file"], "dead": True})
                continue
            o16 = resample16(np.asarray(y), sr)
            e = engine.embed(o16)
            a, b = engine.content(x), engine.content(o16)
            m = min(len(a), len(b))
            content = float(np.mean(np.sum(a[:m] * b[:m], 1) / (np.linalg.norm(a[:m], axis=1) * np.linalg.norm(b[:m], axis=1) + 1e-9))) if m else None
            f0_out = engine.pitch(o16)
            rows.append({"file": dn["file"], "id": float(e @ ref), "cent": float(np.mean(real_emb @ e)), "content": content,
                         "noise": float(noisiness(o16, 16000)), "ltas": float(np.sqrt(np.mean((real_ltas - ltas(o16)) ** 2))),
                         **features(o16, f0_out)})
        for k, ln in enumerate(t["lines"]):
            push((len(t["donors"]) + k) / max(1, total), f"Scoring {label_of(voice.name)} on their own lines ({k + 1}/{len(t['lines'])})")
            x = _load16(d / "lines" / ln["file"])
            f0_in = engine.pitch(x)
            f = f0_in[f0_in > 0]
            shift = float(np.clip(round(12 * math.log2(own_hz / float(np.median(f))) * 2) / 2, -12, 12)) if len(f) > 20 else 0.0
            y, sr = engine.convert(np.concatenate([np.zeros(4800, np.float32), x]), lv, shift)
            if y is None:
                continue
            y = np.asarray(y)[int(sr * 0.3):]
            o16 = resample16(y, sr)
            if not np.isfinite(o16).all() or np.abs(o16).max() < 1e-3:
                continue
            e = engine.embed(o16)
            f0_out = engine.pitch(o16)
            lines.append({"file": ln["file"], "id": float(e @ ref), "cent": float(np.mean(real_emb @ e)), "shift": shift,
                          "noise": float(noisiness(o16, 16000)), "ltas": float(np.sqrt(np.mean((real_ltas - ltas(o16)) ** 2))),
                          **features(o16, f0_out)})
    finally:
        del lv
    s = {"model": h, "voice": voice.name, "made": time.strftime("%Y-%m-%d %H:%M"), "donors": rows, "lines": lines,
         "summary": {cls: {k: round(float(np.mean([r[k] for r in rs if r.get(k) is not None])), 4)
                           for k in MEASURES if any(r.get(k) is not None for r in rs)}
                     for cls, rs in (("donors", rows), ("lines", lines)) if rs}}
    _write_json(out, s)
    return s


# The compare's cost by the test's own size (smallfix7; the real Freeman run: 6 lines + 8 donors, freeze and both
# scorings in 70 s, against a flat 9 min said, so its time left jumped 46 -> 8 -> 20 s).
SCORE_S = 1.2        # s per test input scored (one conversion + its measures)
READ_S = 5.0         # s per own line read through the pack, once, at freeze time
FREEZE_BASE_S = 15.0  # the real clips' references and the donors' pick
JUDGE_BASE_S = 10.0   # loading the candidate
EXPORT_S = 20.0       # the candidate's export before it's judged


def compare_seconds(test: dict | None) -> dict:
    """{"ensure": s, "judge": s}: freezing the test and scoring the approved voice (0 when the test exists and the
    approved voice was scored on it), then scoring the candidate. A test not made yet is priced at the usual size
    (N_LINES lines, POOL_PER_SEX donors)."""
    if test:
        n_l, n_d = len(test.get("lines") or []), len(test.get("donors") or [])
        ens = 0.0 if test.get("approved") else SCORE_S * (n_l + n_d)
    else:
        n_l, n_d = N_LINES, POOL_PER_SEX
        ens = FREEZE_BASE_S + READ_S * n_l + SCORE_S * (n_l + n_d)
    return {"ensure": round(ens, 1), "judge": round(JUDGE_BASE_S + SCORE_S * (n_l + n_d), 1)}


def ensure(voice, proj, engine, push=None) -> tuple[dict, dict]:
    """The voice's frozen test and its approved score, made now if missing (lazily, at the first Improve).
    -> (test, approved score)."""
    push = push or (lambda *a, **k: None)
    t = freeze(voice, proj, engine, push=lambda f, m: push(0.5 * f, m))
    s = approved_score(voice)
    if s is None or s.get("model") != model_hash(voice):
        s = score_voice(voice, voice, engine, push=lambda f, m: push(0.5 + 0.5 * f, m))
        approve(voice, s["model"])
    return load_test(voice), s


def approve(voice, model: str):
    """The score that is "the approved voice" on this test: set at freeze, swapped on Keep (the kept candidate was
    scored on the same test already)."""
    t = load_test(voice)
    if t:
        t["approved"] = model
        _write_json(test_dir(voice) / "test.json", t)


def judge(test_voice, candidate, engine, push=None) -> dict:
    """R2 + R3 for a candidate on its voice's frozen test. -> {offer, reasons, words, terms, compare, before, after}:
    before / after are the likeness means on the person's own lines (the card's numbers)."""
    appr = approved_score(test_voice)
    if appr is None:
        raise ModelError("The approved voice hasn't been scored on its frozen test.")
    cand = score_voice(test_voice, candidate, engine, push)
    cmp = compare(appr, cand)
    v = offer(cmp, load_test(test_voice))
    before = (appr.get("summary") or {}).get("lines", {}).get("id")
    after = (cand.get("summary") or {}).get("lines", {}).get("id")
    if before is None:   # no own lines: the donor likeness, so the card still has a number
        before = (appr.get("summary") or {}).get("donors", {}).get("id")
        after = (cand.get("summary") or {}).get("donors", {}).get("id")
    return {**v, "compare": cmp, "before": before, "after": after, "candidate_model": cand["model"], "approved_model": appr["model"]}


def ab_inputs(voice, n: int = 2) -> list[dict]:
    """R7: the frozen test's own lines for the Improve A/B (the first n): [{file (path), text, sr}]."""
    t = load_test(voice)
    if not t:
        return []
    d = test_dir(voice) / "lines"
    return [{"file": d / ln["file"], "text": ln["text"], "sr": ln["sr"]} for ln in t["lines"][:n] if (d / ln["file"]).exists()]


def move_test(src_dir: Path, dst_dir: Path):
    """The frozen test follows the voice's name (Keep / Undo move folders): test/ from one folder to the other, never
    over an existing one."""
    import os
    a, b = Path(src_dir) / "test", Path(dst_dir) / "test"
    if a.is_dir() and not b.exists():
        fsutil.retry(lambda: os.replace(a, b))
