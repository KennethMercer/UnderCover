"""Listen back (2026-10-13): after the cadence engine (Chatterbox) reads a sentence, before the voice conversion, hear
what it said and mend what the ear would catch. Measured on Agent Smoke, and the user heard it:
  - a word missing, misheard or said with low confidence (Chatterbox is stochastic): the sentence is read again with a
    new seed, up to `tries`, and the best take is kept;
  - a squeezed word ("evening" said "eving": 9% of its line where Kokoro gives it 12.5%, on every seed, setting and
    reference style): stretched on its own toward Kokoro's share, pitch kept, capped at MAX_STRETCH;
  - a comma not heard ("Smoke, Agent Smoke"): Chatterbox leaves a pause there (0.22 s on average, more than Kokoro's
    0.09 s), so it isn't silence: English hears a comma in the word before it, drawn out. That word gets lengthened
    toward Kokoro's, and a pause is only added where there's almost none.
Kokoro (fast) reads the same sentence as the timing reference; Whisper (the cadence worker) gives words, times and
confidence for both. Word-local only, never the whole line (whole-line tempo stretching was "drunk", phase 4).
Every part has a switch in [speakeasy]; what fired is logged per sentence (logs/listenback.jsonl) for the golden set."""
import json
import re
import time
from pathlib import Path

import numpy as np
import soundfile as sf

from ..core import fsutil
from ..core.config import CONFIG, path
from ..core.cadence import client, render
from . import tts

MAX_STRETCH = 1.5
MAX_COMMA_STRETCH = 1.35
SQUEEZED = 0.7         # a content word under this share of Kokoro's (relative to its line) is squeezed
COMMA_SHORT = 0.8      # the word before a comma, when under this share of Kokoro's, is drawn out toward Kokoro's
STOP = set("a an the and or but if of to in on at by for with from as is are was were be been am it its this that "
           "these those he she we you they i me him her us them my your our their his not no so do does did "
           "has have had will would can could shall should may might must".split())
NUMBERS = {str(i): w for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve "
                                           "thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty".split())}
NUMBERS.update({"30": "thirty", "40": "forty", "50": "fifty", "60": "sixty", "70": "seventy", "80": "eighty",
                "90": "ninety", "100": "hundred"})
MIN_COMMA_PAUSE = 0.08
LOW_CONFIDENCE = 0.5
FADE_S = 0.015


def settings() -> dict:
    s = CONFIG.get("speakeasy") or {}
    return {"listen_back": bool(s.get("listen_back", False)), "stretch_words": bool(s.get("stretch_words", False)),
            "comma_lengthen": bool(s.get("comma_lengthen", False)), "tries": int(s.get("listen_back_tries", 3))}


def _clean(w: str) -> str:
    """A word as compared: lower case, letters / digits / apostrophes only, and a number Whisper wrote as digits
    ("9") read as the word it says ("nine")."""
    w = re.sub(r"[^a-z0-9']", "", w.lower())
    return NUMBERS.get(w, w)


def text_words(text: str) -> list[tuple[str, bool]]:
    """[(word, a comma follows it)] in reading order."""
    out = []
    for tok in text.split():
        w = _clean(tok)
        if w:
            out.append((w, tok.rstrip("\"')”’").endswith(",")))
    return out


def _align(want: list, heard: list) -> list:
    """For each wanted word, the index of the heard word that is it (None when missing): in order, greedy."""
    out, j = [], 0
    for w in want:
        k = next((i for i in range(j, min(len(heard), j + 4)) if _clean(heard[i][0]) == w), None)
        out.append(k)
        if k is not None:
            j = k + 1
    return out


def problems(text: str, words: list) -> list[str]:
    """What the ear would catch in a take: missing / misheard words and low-confidence ones (Whisper's words with
    confidence: [word, start, end, probability])."""
    want = [w for w, _ in text_words(text)]
    idx = _align(want, words)
    out = [f"missing {w!r}" for w, k in zip(want, idx) if k is None]
    out += [f"unsure {words[k][0].strip()!r} ({words[k][3]:.2f})" for k in idx
            if k is not None and len(words[k]) > 3 and words[k][3] < LOW_CONFIDENCE]
    extra = len(words) - sum(k is not None for k in idx)
    if extra > 0:
        out.append(f"{extra} extra word(s)")
    return out


def _voiced(x: np.ndarray, sr: int, a: float, b: float) -> tuple[float, float]:
    """[a, b) with quiet frames (under -35 dB vs the clip's peak) trimmed off both ends: Whisper's word edges swallow
    short pauses, which made a word measure longer than it is spoken."""
    hop = int(sr * 0.01)
    i, j = int(a * sr), int(b * sr)
    seg = x[i:j]
    if len(seg) < 2 * hop:
        return a, b
    rms = np.sqrt(np.mean(seg[: len(seg) // hop * hop].reshape(-1, hop) ** 2, axis=1))
    loud = np.flatnonzero(20 * np.log10(rms + 1e-9) - 20 * np.log10(float(np.abs(x).max()) + 1e-9) > -35)
    if not len(loud):
        return a, b
    return a + loud[0] * 0.01, a + (loud[-1] + 1) * 0.01


def _shares(text: str, words: list, x: np.ndarray, sr: int) -> dict:
    """{wanted word index: (start, end, share of the line)} for the words that were heard, by their voiced span."""
    want = [w for w, _ in text_words(text)]
    idx = _align(want, words)
    if not words:
        return {}
    line = _voiced(x, sr, words[0][1], words[-1][2])
    span = max(1e-3, line[1] - line[0])
    out = {}
    for i, k in enumerate(idx):
        if k is not None:
            a, b = _voiced(x, sr, words[k][1], words[k][2])
            out[i] = (a, b, (b - a) / span)
    return out


def _quiet_run(x: np.ndarray, sr: int, a: float, b: float) -> float:
    """The longest run of 10 ms frames under -35 dB (vs the clip's peak) between a and b seconds."""
    hop = int(sr * 0.01)
    seg = x[int(a * sr):int(b * sr)]
    if len(seg) < hop:
        return 0.0
    rms = np.sqrt(np.mean(seg[: len(seg) // hop * hop].reshape(-1, hop) ** 2, axis=1))
    db = 20 * np.log10(rms + 1e-9) - 20 * np.log10(float(np.abs(x).max()) + 1e-9)
    run = best = 0
    for q in db < -35:
        run = run + 1 if q else 0
        best = max(best, run)
    return best / 100


def stretch_span(x: np.ndarray, sr: int, a: float, b: float, factor: float) -> np.ndarray:
    """x with only [a, b) seconds made `factor` times longer (pitch kept), short crossfades at both joins."""
    import librosa
    i, j = int(a * sr), int(b * sr)
    if j - i < int(0.05 * sr) or factor <= 1.01:
        return x
    mid = librosa.effects.time_stretch(x[i:j], rate=1.0 / factor).astype(np.float32)
    f = min(int(FADE_S * sr), len(mid) // 4, i, len(x) - j)
    out = np.concatenate([x[:i], mid, x[j:]])
    if f > 8:   # equal-power crossfade into and out of the stretched part
        ramp = np.linspace(0, 1, f, dtype=np.float32)
        out[i:i + f] = x[i:i + f] * (1 - ramp) + mid[:f] * ramp
        k = i + len(mid)
        out[k - f:k] = mid[-f:] * (1 - ramp) + x[j - f:j] * ramp
    return out


def insert_pause(x: np.ndarray, sr: int, at: float, seconds: float) -> np.ndarray:
    i = int(at * sr)
    return np.concatenate([x[:i], np.zeros(int(seconds * sr), np.float32), x[i:]])


def review(text: str, wav: Path, pack: dict, ref: Path, seed: int, reader: str, log: dict) -> Path:
    """The listen-back for one sentence: -> the take to convert (a new WAV when something was mended)."""
    s = settings()
    t0 = time.perf_counter()
    timing = s["stretch_words"] or s["comma_lengthen"]   # Kokoro's timing reference only when a stretch may follow
    kref = Path(wav).with_name(Path(wav).stem + "_kokoro.wav")
    if timing:
        sf.write(kref, np.asarray(tts.synth(text, reader), np.float32), tts.SR, subtype="PCM_16")
    takes = [(Path(wav), seed)]
    heard = client.transcribe([str(wav)] + ([str(kref)] if timing else []), words=True)
    kwords = heard[1]["words"] if timing else []
    best = (Path(wav), seed, heard[0]["words"], problems(text, heard[0]["words"]))
    tries = 1
    while best[3] and tries < max(1, s["tries"]):   # read again with a new seed; keep the take with fewest problems
        nseed = seed + 1000 * tries
        fut, w2 = render.request(text, pack, ref, seed=nseed)
        client.wait(fut)
        h = client.transcribe([str(w2)], words=True)[0]["words"]
        p = problems(text, h)
        takes.append((Path(w2), nseed))
        if len(p) < len(best[3]):
            best = (Path(w2), nseed, h, p)
        tries += 1
    take, words = best[0], best[2]
    x, sr = sf.read(str(take), dtype="float32")
    if x.ndim > 1:
        x = x.mean(1)
    tw = text_words(text)
    mine, ko = {}, {}
    if timing:
        kx, ksr = sf.read(str(kref), dtype="float32")
        mine, ko = _shares(text, words, x, sr), _shares(text, kwords, kx, ksr)
    edits = []   # (start, end, factor, why) and pauses, applied last to first
    for i, (w, comma) in enumerate(tw):
        if i not in mine or i not in ko:
            continue
        a, b, sh = mine[i]
        kshare = ko[i][2]
        if comma and s["comma_lengthen"] and sh < kshare * COMMA_SHORT:
            edits.append((a, b, min(MAX_COMMA_STRETCH, kshare / max(sh, 1e-3)), f"comma {w}"))
        elif s["stretch_words"] and len(w) >= 4 and w not in STOP and sh < kshare * SQUEEZED:
            edits.append((a, b, min(MAX_STRETCH, 0.9 * kshare / max(sh, 1e-3)), f"squeezed {w}"))
        if comma and s["comma_lengthen"] and i + 1 in mine and _quiet_run(x, sr, a, mine[i + 1][1]) < MIN_COMMA_PAUSE:
            edits.append((b, b, 0.12, f"pause {w}"))
    for a, b, f, why in sorted(edits, key=lambda e: e[0], reverse=True):
        x = insert_pause(x, sr, a, f) if why.startswith("pause") else stretch_span(x, sr, a, b, f)
    log.update(tries=tries, seed=best[1], first_problems=problems(text, heard[0]["words"]), problems=best[3],
               edits=[(why, round(f, 2)) for *_, f, why in edits],
               seconds=round(time.perf_counter() - t0, 2))
    kref.unlink(missing_ok=True)
    for t, _ in takes:
        if t != take:
            t.unlink(missing_ok=True)
    if not edits and take == Path(wav):
        return Path(wav)
    out = Path(wav).with_name(Path(wav).stem + "_lb.wav")
    sf.write(out, x, sr, subtype="FLOAT")
    if take != out:
        take.unlink(missing_ok=True)
    return out


def write_log(entry: dict):
    """One line per sentence in logs/listenback.jsonl (the golden set scores what fired)."""
    f = path("logs_dir") / "listenback.jsonl"
    try:
        fsutil.guard(f)
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError:
        pass
