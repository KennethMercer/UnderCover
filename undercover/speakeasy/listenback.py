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
import threading
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
DRAWL_S = 0.30         # a one-syllable word held this long, not the last and with no pause marked after it, is drawn out
                       # ("GOOD......morning": 0.36 s against 0.12-0.20 s natural; the user's ear, phase 24)
DRAWL_LINE = 4         # ...in a short line only: in flowing speech Whisper's word edges can't tell a held word from a
                       # stressed one (on the golden set's 18 speech readings, every line flagged a word at 0.30 s)
PAUSE_MARKS = ",.;:!?…—–-"
FADE_S = 0.015


LOW_VRAM_GB = 12        # under this, the worker's own Whisper is freed before the listen-back loads one here
_ASR = {"model": None, "failed": "", "fetching": None}
_ASR_LOCK = threading.Lock()


def _asr():
    """Whisper in this process (the server), kept warm like the converter: the listen-back hears sentence n while the
    cadence worker reads n+1. In the worker, its transcribe queued behind the prefetched readings and serialised the
    whole line (about 3x as-now; measured 2026-10-13). Same model file and prompt as the worker. None if it can't load
    (then re-reads are skipped and the log says why)."""
    if _ASR["model"] is not None or _ASR["failed"]:
        return _ASR["model"]
    with _ASR_LOCK:
        if _ASR["model"] is None and not _ASR["failed"]:
            try:
                import torch
                import whisper
                dev = "cuda" if torch.cuda.is_available() else "cpu"
                if dev == "cuda" and torch.cuda.get_device_properties(0).total_memory / 2**30 < LOW_VRAM_GB:
                    try:   # a small card: the worker's Whisper goes first (the cadence engine stays loaded there)
                        client.WORKER.call("free", what="whisper", timeout=30)
                    except Exception:   # noqa: BLE001 - no worker yet, nothing to free
                        pass
                f = path("models_dir") / "whisper" / "large-v3-turbo.pt"
                if not f.is_file():   # a fresh install (phase 25): fetched in the background, this speech goes on
                    _fetch_in_background()
                    _ASR["why"] = "the speech recogniser is downloading (once, about 1.6 GB): re-reads start after"
                    return None
                _ASR["model"] = whisper.load_model(str(f), device=dev)
            except Exception as e:   # noqa: BLE001 - out of memory, can't load: reading goes on without re-reads
                _ASR["failed"] = f"{type(e).__name__}: {e}"[:200]
    return _ASR["model"]


def _fetch_in_background():
    """The pinned Whisper file (the same one the cadence worker uses, SHA-256 checked), once, without holding up the
    speech that asked: listen-back is on by default, so a fresh install needs it without anyone training a cadence."""
    t = _ASR["fetching"]
    if t is not None and t.is_alive():
        return

    def go():
        try:
            from ..core import setup_models
            from ..core.cadence.engines import MODEL_SHA256, WHISPER_URL
            setup_models.ensure(files={"whisper/large-v3-turbo.pt": WHISPER_URL}, sha256=MODEL_SHA256)
        except Exception as e:   # noqa: BLE001 - offline, disk full: tried again on the next speech
            print(f"Listen-back: the speech recogniser couldn't be downloaded ({e}); trying again next time.", flush=True)
    _ASR["fetching"] = threading.Thread(target=go, daemon=True, name="listenback-whisper")
    _ASR["fetching"].start()


def hear(files: list) -> list:
    """[{"text", "words": [[word, start, end, confidence]]}] for each file, heard in this process."""
    from ..core.cadence.engines import WHISPER_PROMPT
    m = _asr()
    if m is None:
        raise RuntimeError(f"no speech recogniser for the listen-back ({_ASR['failed'] or _ASR.get('why') or 'not loaded'})")
    out = []
    with _ASR_LOCK:
        for f in files:
            r = m.transcribe(render.load16(Path(f)), language="en", fp16=m.device.type == "cuda",
                             condition_on_previous_text=False, temperature=0.0, initial_prompt=WHISPER_PROMPT,
                             word_timestamps=True)
            out.append({"text": r["text"].strip(), "words": [[w["word"], round(w["start"], 2), round(w["end"], 2),
                                                              round(float(w.get("probability", 1.0)), 3)]
                                                             for s in r["segments"] for w in s.get("words", [])]})
    return out


def settings() -> dict:
    s = CONFIG.get("speakeasy") or {}
    # defaults from the user's blind A/Bs (2026-10-13): re-reads on (they fixed "evening"); the word stretch fixed
    # nothing and the comma lengthening flubbed "insist", so both stay off unless switched on
    return {"listen_back": bool(s.get("listen_back", True)), "stretch_words": bool(s.get("stretch_words", False)),
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


def syllables(word: str) -> int:
    """About how many syllables (vowel groups, a silent final e dropped): enough to tell a one-syllable word."""
    n = len(re.findall(r"[aeiouy]+", word.lower()))
    if n > 1 and word.lower().endswith("e") and not word.lower().endswith(("le", "ee")):
        n -= 1
    return max(1, n)


def _pause_after(text: str) -> list[bool]:
    """For each word (text_words' order): does the text mark a pause after it (a comma, a full stop...)?"""
    return [tok.rstrip("\"')”’").endswith(tuple(PAUSE_MARKS)) for tok in text.split() if _clean(tok)]


def problems(text: str, words: list) -> list[str]:
    """What the ear would catch in a take: missing / misheard words, low-confidence ones, and a one-syllable word
    drawn out mid-phrase (Whisper's words with timing and confidence: [word, start, end, probability])."""
    want = [w for w, _ in text_words(text)]
    idx = _align(want, words)
    out = [f"missing {w!r}" for w, k in zip(want, idx) if k is None]
    out += [f"unsure {words[k][0].strip()!r} ({words[k][3]:.2f})" for k in idx
            if k is not None and len(words[k]) > 3 and words[k][3] < LOW_CONFIDENCE]
    pause = _pause_after(text)
    for i, (w, k) in enumerate(zip(want, idx)):   # Whisper's word end takes in a pause after it: not when one is marked
        if (len(want) <= DRAWL_LINE and k is not None and i < len(want) - 1 and not pause[i] and len(words[k]) > 2
                and syllables(w) == 1 and words[k][2] - words[k][1] > DRAWL_S):
            out.append(f"drawn-out {w!r} ({words[k][2] - words[k][1]:.2f} s)")
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
    heard = hear([str(wav)] + ([str(kref)] if timing else []))
    kwords = heard[1]["words"] if timing else []
    best = (Path(wav), seed, heard[0]["words"], problems(text, heard[0]["words"]))
    tries = 1
    while best[3] and tries < max(1, s["tries"]):   # read again with a new seed; keep the take with fewest problems
        nseed = seed + 1000 * tries
        fut, w2 = render.request(text, pack, ref, seed=nseed)
        client.wait(fut)
        h = hear([str(w2)])[0]["words"]
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
