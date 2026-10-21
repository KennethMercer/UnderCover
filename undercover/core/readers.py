"""The Readers' model: Kokoro text-to-speech (kokoro-onnx, Apache-2.0), loaded once and shared.

Two modules read with it, so it lives in core (modules import core only): SpeakEasy's Reader (speakeasy/tts.py: which
Reader, sentences, pitch) and DoubleAgent's voice check (doubleagent/voicecheck.py: the Readers' phrases stand in for
other speakers when a person's clips have none, rc-fix7). Runs on the CPU (onnxruntime). Its files are installed with
the other models (models/tts/kokoro/); the Doctor checks them (pinned, SHA-256).
"""
import threading

import numpy as np

from . import setup_models
from .config import path
from .voices import ModelError

SR = 24000
URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
FILES = {"tts/kokoro/kokoro-v1.0.onnx": f"{URL}/kokoro-v1.0.onnx",      # ~310 MB
         "tts/kokoro/voices-v1.0.bin": f"{URL}/voices-v1.0.bin"}        # ~27 MB
SHA256 = {"tts/kokoro/kokoro-v1.0.onnx": "7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5",
          "tts/kokoro/voices-v1.0.bin": "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d"}
setup_models.register_pinned(FILES, SHA256, "kokoro")   # the Doctor checks and re-fetches them (phase 10)

_K = None
_LOCK = threading.Lock()


def ready() -> bool:
    return all((path("models_dir") / f).exists() for f in FILES)


def lang(reader: str) -> str:
    return "en-gb" if reader.startswith("b") else "en-us"


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
    """One sentence in one of Kokoro's voices -> float32 mono speech at 24 kHz, leading/trailing silence trimmed."""
    k = kokoro()
    with _LOCK:   # one synthesis at a time (onnxruntime already uses every core)
        a, _ = k.create(text, voice=reader, speed=float(np.clip(speed, 0.5, 2.0)), lang=lang(reader))
    return trim(np.asarray(a, np.float32))


def trim(a: np.ndarray, db: float = -45.0, keep: float = 0.05) -> np.ndarray:
    lvl = 10 ** (db / 20) * max(1e-4, float(np.abs(a).max()))
    on = np.where(np.abs(a) > lvl)[0]
    if not len(on):
        return a[:0]
    k = int(SR * keep)
    return a[max(0, on[0] - k):on[-1] + k]
