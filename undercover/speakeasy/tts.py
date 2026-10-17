"""Kokoro text-to-speech (kokoro-onnx, Apache-2.0): text -> natural speech in a stock voice, the "Reader".

Runs on the CPU (onnxruntime); the voice model then turns the Reader into the person (speak.py). Model files are
fetched on first use into models/tts/kokoro/.
Measured 2026-10-10 (Ryzen CPU): fp32 model 5.8x realtime; the int8 model is 4.5x SLOWER on the CPU (1.3x realtime),
so it isn't used.
"""
import re
import threading

import numpy as np

from ..core import addons, setup_models
from ..core.config import path
from ..core.voices import ModelError

SR = 24000
URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
FILES = {"tts/kokoro/kokoro-v1.0.onnx": f"{URL}/kokoro-v1.0.onnx",      # ~310 MB
         "tts/kokoro/voices-v1.0.bin": f"{URL}/voices-v1.0.bin"}        # ~27 MB
SHA256 = {"tts/kokoro/kokoro-v1.0.onnx": "7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5",
          "tts/kokoro/voices-v1.0.bin": "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d"}
setup_models.register_pinned(FILES, SHA256, "kokoro")   # the Doctor checks and re-fetches them (phase 10)
addons.register(addons.Addon(
    id="kokoro", name="Kokoro", included=True, modules=["speakeasy"],
    adds="The quick Reader: SpeakEasy's speech for voices without a cadence.",
    licence="Apache-2.0", licence_url="https://huggingface.co/hexgrad/Kokoro-82M",
    licence_summary=addons.OPEN.format("Apache-2.0") + " Code (kokoro-onnx, MIT) and weights.",
    size_mb=340, paths=["tts/kokoro"], check=lambda: all((path("models_dir") / f).exists() for f in FILES)))

# The English Readers: (label, measured speaking pitch [p5, p50, p95] Hz, Kokoro's own quality grade).
# Pitch measured with RMVPE on the same 3 sentences (2026-10-10). `auto` = good enough to be picked automatically.
READERS = {
    "af_heart": ("American woman, warm", [148.7, 194.3, 279.6], "A"),
    "af_bella": ("American woman, bright", [161.8, 200.1, 274.8], "A-"),
    "af_nicole": ("American woman, soft", [136.2, 156.7, 189.8], "B-"),
    "af_aoede": ("American woman", [143.0, 172.3, 258.8], "C+"),
    "af_kore": ("American woman, low", [104.1, 149.4, 245.4], "C+"),
    "af_sarah": ("American woman", [138.9, 190.6, 282.2], "C+"),
    "af_nova": ("American woman", [125.7, 153.5, 205.4], "C"),
    "af_alloy": ("American woman, low", [99.4, 141.7, 182.5], "C"),
    "af_sky": ("American woman", [111.1, 149.6, 212.6], "C-"),
    "af_jessica": ("American woman, high", [153.0, 207.9, 303.7], "D"),
    "af_river": ("American woman", [142.0, 165.4, 220.8], "D"),
    "bf_emma": ("British woman", [146.9, 177.7, 216.4], "B-"),
    "bf_isabella": ("British woman", [164.9, 194.4, 257.9], "C"),
    "bf_alice": ("British woman", [141.4, 197.0, 294.0], "D"),
    "bf_lily": ("British woman", [139.5, 194.4, 257.9], "D"),
    "am_michael": ("American man", [85.5, 110.6, 163.8], "C+"),
    "am_fenrir": ("American man", [91.0, 128.9, 233.8], "C+"),
    "am_puck": ("American man, lively", [73.6, 104.9, 210.7], "C+"),
    "am_echo": ("American man, deep", [54.0, 105.6, 146.0], "D"),
    "am_eric": ("American man, higher", [105.2, 152.8, 214.2], "D"),
    "am_liam": ("American man", [85.5, 126.5, 191.4], "D"),
    "am_onyx": ("American man, very deep", [70.4, 82.6, 101.8], "D"),
    "am_adam": ("American man, deep", [68.9, 107.3, 144.9], "F+"),
    "bm_george": ("British man", [88.9, 135.9, 175.3], "C"),
    "bm_fable": ("British man", [76.3, 111.8, 161.0], "C"),
    "bm_lewis": ("British man, very deep", [61.7, 82.6, 126.5], "D+"),
    "bm_daniel": ("British man", [89.5, 118.9, 148.4], "D"),
}
AUTO = ("af_heart", "af_bella", "af_nicole", "bf_emma", "am_michael", "am_fenrir", "bm_george", "bm_fable")
# Male or female, from the voice's speaking pitch: median under 165 Hz, or a low end (p5) under 115 Hz. Pitch alone
# misreads men who speak high in public: Obama's speeches sit at 180 Hz (p5 109), and a male Reader shifted up
# matched him better than the closest female one (likeness 0.77 vs 0.72, measured 2026-10-10).
MALE_BELOW, MALE_LOW_BELOW = 165.0, 115.0
DEFAULT = "am_michael"  # a voice whose pitch is unknown


def lang(reader: str) -> str:
    return "en-gb" if reader.startswith("b") else "en-us"


PRINT_CLEAR = 3   # a voice-print vote this clear (4 of the 5 nearest Readers, or more) decides the sex; a closer one, the pitch


def is_male(target_hz: float | None, low_hz: float | None = None, vote: int | None = None) -> bool | None:
    """The person's sex for the Reader pick: their voice print's vote when it's clear (core.speaker.sex_vote), else
    their speaking pitch (median, or a low end under MALE_LOW_BELOW); None when neither is known. Pitch alone read
    Trump as a woman (167 Hz, raised by rallies; vote +3) and Kristen Schaal as a man (low end 110 Hz; vote -5)."""
    if vote is not None and abs(vote) >= PRINT_CLEAR:
        return vote > 0
    if not target_hz:
        return None
    return target_hz < MALE_BELOW or (low_hz is not None and low_hz < MALE_LOW_BELOW)


def pick_reader(target_hz: float | None, low_hz: float | None = None, vote: int | None = None,
                accent: str | None = None) -> str:
    """The automatic Reader for a voice that speaks around target_hz (low_hz: its p5): same sex (is_male), same accent,
    closest pitch. The accent is American unless the voice is known to be British (extra.accent): nothing measures an
    accent yet, and the British Readers were picked by pitch alone (Trump read by bf_emma, a British woman, smallfix7).
    Measured 2026-10-10: Morgan Freeman (105 Hz) 0.84 with am_michael, the best of six Readers for him."""
    male = is_male(target_hz, low_hz, vote)
    if male is None:
        return DEFAULT
    cands = [r for r in AUTO if r[0] == ("b" if accent == "british" else "a") and r[1] == ("m" if male else "f")]
    if not target_hz:
        return cands[0]
    return min(cands, key=lambda r: abs(np.log2(READERS[r][1][1] / target_hz)))


def pitch_for(reader: str, target_hz: float | None) -> float:
    """Semitones that move the Reader's speaking pitch onto the target's (half-semitone steps, at most an octave)."""
    if not target_hz:
        return 0.0
    st = 12 * np.log2(target_hz / READERS[reader][1][1])
    return float(np.clip(round(st * 2) / 2, -12, 12))


# ---------------------------------------------------------------- the model

_K = None
_LOCK = threading.Lock()


def ready() -> bool:
    return all((path("models_dir") / f).exists() for f in FILES)


def kokoro(progress=None):
    """The loaded Kokoro model (downloads ~340 MB the first time)."""
    global _K
    with _LOCK:
        if _K is None:
            try:
                setup_models.ensure(progress, files=FILES, sha256=SHA256)
            except Exception as e:
                raise ModelError(f"Couldn't download the speech model ({e.__class__.__name__}). "
                                 "Check your internet connection and try again.")
            from kokoro_onnx import Kokoro
            m = path("models_dir") / "tts" / "kokoro"
            _K = Kokoro(str(m / "kokoro-v1.0.onnx"), str(m / "voices-v1.0.bin"))
        return _K


def synth(text: str, reader: str, speed: float = 1.0) -> np.ndarray:
    """One sentence -> float32 mono speech at 24 kHz, leading/trailing silence trimmed."""
    if reader not in READERS:
        raise ModelError(f"Unknown Reader '{reader}'.")
    k = kokoro()
    with _LOCK:   # one synthesis at a time (onnxruntime already uses every core)
        a, _ = k.create(text, voice=reader, speed=float(np.clip(speed, 0.5, 2.0)), lang=lang(reader))
    return _trim(np.asarray(a, np.float32))


def _trim(a: np.ndarray, db: float = -45.0, keep: float = 0.05) -> np.ndarray:
    lvl = 10 ** (db / 20) * max(1e-4, float(np.abs(a).max()))
    on = np.where(np.abs(a) > lvl)[0]
    if not len(on):
        return a[:0]
    k = int(SR * keep)
    return a[max(0, on[0] - k):on[-1] + k]


# ---------------------------------------------------------------- sentences

_END = re.compile(r'(?<=[.!?…])["\')\]]*\s+(?=["\'(\[]?[A-Z0-9])')
_ABBR = re.compile(r"\b(?:Mr|Mrs|Ms|Dr|St|Jr|Sr|Prof|Mt|Gen|Col|Lt|Sgt|Rev|vs|etc|e\.g|i\.e|[A-Z])\.$")
MAX_CHARS = 280   # Kokoro reads up to ~510 phonemes at once; long sentences are split at a comma or semicolon


def sentences(text: str) -> list[dict]:
    """Text -> [{"text", "pause"}] in reading order. pause = seconds of silence after it (a paragraph pauses longer)."""
    out = []
    paras = [p.strip() for p in re.split(r"\n\s*\n", text.replace("\r", "")) if p.strip()]
    for pi, para in enumerate(paras):
        para = re.sub(r"\s+", " ", para)
        parts = []
        for s in (s.strip() for s in _END.split(para) if s.strip()):
            if parts and _ABBR.search(parts[-1]):   # "Mr. Smith" isn't two sentences
                parts[-1] += " " + s
            else:
                parts.append(s)
        for s in parts:
            for piece in _split_long(s):
                out.append({"text": piece, "pause": 0.3})
        if out:
            out[-1]["pause"] = 0.7 if pi < len(paras) - 1 else 0.0
    # very short bits ("Yes.") read better joined to the next sentence
    merged = []
    for s in out:
        if merged and len(merged[-1]["text"]) < 12 and merged[-1]["pause"] < 0.5 and len(merged[-1]["text"]) + len(s["text"]) < MAX_CHARS:
            merged[-1] = {"text": merged[-1]["text"] + " " + s["text"], "pause": s["pause"]}
        else:
            merged.append(s)
    return merged


def _split_long(s: str) -> list[str]:
    if len(s) <= MAX_CHARS:
        return [s]
    cut = max((m.end() for m in re.finditer(r"[;:,—–]\s", s[:MAX_CHARS])), default=0)
    if cut < 40:
        cut = s.rfind(" ", 0, MAX_CHARS) + 1 or MAX_CHARS
    return [s[:cut].strip()] + _split_long(s[cut:].strip())


def seconds_estimate(text: str, speed: float = 1.0) -> float:
    """Rough length of the speech: ~3 words per second at 1x (measured on Kokoro's American voices)."""
    return len(re.findall(r"\w+", text)) / 3.0 / max(0.5, speed)
