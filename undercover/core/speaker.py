"""Speaker fingerprints: WeSpeaker ResNet34-LM (VoxCeleb, 256-d) through onnxruntime on the CPU.

Same front end as WeSpeaker: 16 kHz audio scaled to int16 range, Kaldi fbank (80 mel, 25/10 ms, hamming,
no dither), per-utterance mean removal. Embeddings are L2-normalised, so cosine similarity is a dot product.
Same-speaker pairs typically score > 0.5, different speakers < 0.3.
"""
import json
from pathlib import Path

import numpy as np
import torch

from .config import path
from . import setup_models

SR = 16000
# Voice prints of voices whose sex is known: SpeakEasy's Readers, rendered once (tools/reader_prints.py). Prints carry sex
# only weakly across recording conditions (a centroid split of the Readers got Freeman and Obama wrong on their real
# interviews, 2026-10-17), so the vote of the nearest few is a hint that decides only when it's clear (tts.pick_reader).
PRINTS = Path(__file__).resolve().parents[1] / "assets" / "reader-prints.json"
NEAREST = 5
_PRINTS = None


def labelled_prints() -> list[tuple[str, str, np.ndarray]]:
    """[(id, sex, unit vector)] from the shipped prints; [] when the file is missing or unreadable."""
    global _PRINTS
    if _PRINTS is None:
        try:
            d = json.loads(PRINTS.read_text(encoding="utf-8"))["prints"]
            _PRINTS = [(k, x["sex"], np.asarray(x["v"], np.float32) / (np.linalg.norm(x["v"]) + 1e-9)) for k, x in d.items()]
        except (OSError, ValueError, KeyError, TypeError):
            _PRINTS = []
    return _PRINTS


def sex_vote(vec, k: int = NEAREST) -> int | None:
    """+k (the k nearest labelled voices all male) .. -k (all female), or None without prints or a vector. Measured on
    the user's people (2026-10-17): Trump +3, Kristen Schaal -5, Peter Griffin +5, Obama -1, Freeman +1."""
    prints = labelled_prints()
    if vec is None or not prints:
        return None
    v = np.asarray(vec, np.float32)
    v = v / (np.linalg.norm(v) + 1e-9)
    near = sorted(prints, key=lambda p: -float(p[2] @ v))[:k]
    return int(sum(1 if sex == "male" else -1 for _, sex, _ in near))


class SpeakerEmbedder:
    def __init__(self):
        import onnxruntime as ort
        setup_models.ensure(files={setup_models.SPEAKER_MODEL: setup_models.SPEAKER_URL})
        opts = ort.SessionOptions()
        opts.log_severity_level = 3
        self.sess = ort.InferenceSession(str(path("models_dir") / setup_models.SPEAKER_MODEL), opts,
                                         providers=["CPUExecutionProvider"])

    @staticmethod
    def _fbank(audio16: np.ndarray) -> np.ndarray:
        import torchaudio.compliance.kaldi as kaldi
        wav = torch.from_numpy(np.ascontiguousarray(audio16, dtype=np.float32))[None] * (1 << 15)
        feats = kaldi.fbank(wav, num_mel_bins=80, frame_length=25, frame_shift=10, dither=0.0,
                            sample_frequency=SR, window_type="hamming", use_energy=False)
        return (feats - feats.mean(0, keepdim=True)).numpy()

    def embed(self, audio16: np.ndarray) -> np.ndarray:
        """Mono 16 kHz float audio (>= ~0.5 s) -> unit-length 256-d embedding."""
        e = self.sess.run(None, {"feats": self._fbank(audio16)[None]})[0][0]
        return e / (np.linalg.norm(e) + 1e-9)

    def embed_windows(self, audio16: np.ndarray, win: float = 3.0, hop: float = 1.5) -> np.ndarray:
        """Embeddings of overlapping windows (for spotting a second speaker inside one phrase)."""
        w, h = int(win * SR), int(hop * SR)
        if len(audio16) <= w + h // 2:
            return self.embed(audio16)[None]
        starts = list(range(0, len(audio16) - w + 1, h))
        if starts[-1] + w < len(audio16):
            starts.append(len(audio16) - w)
        return np.stack([self.embed(audio16[s:s + w]) for s in starts])
