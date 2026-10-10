"""Does a voice model actually sound like the person? Measured, not guessed from its name or metadata.

Test: take phrases spoken by OTHER people in clips of the target (interviewers, hosts), convert them with
the model, and compare speaker fingerprints (WeSpeaker) of the result with the target's real voice. A good
model turns an interviewer into the target; a poor or mislabelled one doesn't. Each phrase is shifted by whole
octaves toward the target's pitch first, since fingerprints are partly pitch-sensitive. The other speakers'
own similarity to the target (before conversion) is the baseline.
"""
import math
from dataclasses import asdict, dataclass

import numpy as np


@dataclass
class Quality:
    voice: str
    score: float        # mean cosine similarity of converted phrases to the target (0..1)
    baseline: float     # the same phrases before conversion
    phrases: int
    seconds: float
    verdict: str        # good | weak | bad
    target_hz: float | None = None   # the real person's median speaking pitch (picks the cover's octave)
    low: float | None = None         # bootstrap 90% lower bound of `score` (few phrases = wide range)
    pitch_cents: float | None = None # median pitch error of the conversion vs the intended melody
    content: float | None = None     # mean HuBERT cosine, input vs output (how much of the words survive)

    def to_json(self):
        return asdict(self)


GOOD, WEAK = 0.45, 0.30   # same scale as the training match threshold (same speaker ~> 0.5)
LOW_OK = 0.40             # ...and the bootstrap lower bound must clear this (a lucky mean of 3 phrases isn't a pass)
MAX_CENTS = 50.0          # a voice that can't hold pitch (cracks, wrong notes) fails regardless of likeness


def test_phrases(proj, max_phrases: int = 8, max_seconds: float = 45.0):
    """(other-speaker phrases, target reference vector, target median pitch) from a prepared project."""
    import soundfile as sf
    from .train.project import _resample16
    emb = proj.load_embeddings()
    ref = emb.get(f"speaker:{proj.reference.get('cluster')}")
    if ref is None:
        return [], None
    others = sorted([p for p in proj.phrases if p.reason == "other speaker" and 2.0 <= p.dur <= 8.0 and p.score < 0.2],
                    key=lambda p: -p.dur)
    picked, total = [], 0.0
    used_sources: dict = {}
    for p in others:   # spread over sources and speakers
        if used_sources.get(p.source, 0) >= 3:
            continue
        x, sr = sf.read(str(proj.clip_path(p.id)), dtype="float32")
        picked.append(_resample16(x if x.ndim == 1 else x.mean(1), sr))
        used_sources[p.source] = used_sources.get(p.source, 0) + 1
        total += p.dur
        if len(picked) >= max_phrases or total >= max_seconds:
            break
    return picked, ref / (np.linalg.norm(ref) + 1e-9)


def target_pitch(proj, rmvpe, n: int = 8) -> float | None:
    import soundfile as sf
    from .train.project import _resample16
    kept = sorted([p for p in proj.phrases if p.kept and p.dur >= 2], key=lambda p: -p.score)[:n]
    f0s = []
    for p in kept:
        x, sr = sf.read(str(proj.clip_path(p.id)), dtype="float32")
        f = rmvpe.infer_from_audio(_resample16(x if x.ndim == 1 else x.mean(1), sr), thred=0.03)
        f0s.append(f[f > 0])
    f = np.concatenate(f0s) if f0s else np.zeros(0)
    return float(np.median(f)) if len(f) > 50 else None


def check(voice: str, proj, converter, embedder) -> Quality:
    from .convert import Settings
    from .train.project import _resample16
    phrases, ref = test_phrases(proj)
    if not phrases or ref is None:
        raise ValueError("no test phrases (the clips had only one speaker)")
    import torch
    tgt_hz = target_pitch(proj, converter.rmvpe)
    sims, base, cents, content, secs = [], [], [], [], 0.0
    for x in phrases:
        f0_in = converter.rmvpe.infer_from_audio(x, thred=0.03)
        f = f0_in[f0_in > 0]
        shift = 12 * round(math.log2(tgt_hz / float(np.median(f)))) if tgt_hz and len(f) > 20 else 0
        shift = float(np.clip(shift, -24, 24))
        out, sr = converter.convert(x, voice, Settings(pitch=shift))
        if not np.isfinite(out).all() or np.abs(out).max() < 1e-3:
            sims.append(0.0)
            continue
        o16 = _resample16(out, sr)
        sims.append(float(embedder.embed(o16) @ ref))
        base.append(float(embedder.embed(x) @ ref))
        secs += len(x) / 16000
        # pitch: did it sing the intended melody? (frames voiced in both)
        f0_out = converter.rmvpe.infer_from_audio(o16, thred=0.03)
        n = min(len(f0_in), len(f0_out))
        both = (f0_in[:n] > 0) & (f0_out[:n] > 0)
        if both.sum() > 20:
            want = f0_in[:n][both] * 2 ** (shift / 12)
            cents.append(float(np.median(np.abs(1200 * np.log2(f0_out[:n][both] / want)))))
        # content: are the words still there? (HuBERT features of input vs output, frame by frame)
        dt = torch.float16 if converter.half else torch.float32
        with torch.no_grad():
            a = converter.hubert.extract(torch.from_numpy(x).to(converter.device, dt)[None], "v2")[0].float()
            b = converter.hubert.extract(torch.from_numpy(o16).to(converter.device, dt)[None], "v2")[0].float()
        m = min(len(a), len(b))
        content.append(float(torch.nn.functional.cosine_similarity(a[:m], b[:m], dim=1).mean()))
    score = float(np.mean(sims))
    rng = np.random.default_rng(0)
    boots = [float(np.mean(rng.choice(sims, len(sims)))) for _ in range(1000)] if len(sims) > 1 else [score]
    low = float(np.percentile(boots, 5))
    pc = float(np.median(cents)) if cents else None
    verdict = ("good" if score >= GOOD and low >= LOW_OK else "weak" if score >= WEAK else "bad")
    if verdict == "good" and pc is not None and pc > MAX_CENTS:
        verdict = "weak"   # sounds like them but can't hold the melody
    return Quality(getattr(voice, "name", voice), round(score, 3), round(float(np.mean(base)) if base else 0.0, 3), len(phrases), round(secs, 1), verdict,
                   round(tgt_hz, 1) if tgt_hz else None, round(low, 3), round(pc, 1) if pc is not None else None,
                   round(float(np.mean(content)), 3) if content else None)
