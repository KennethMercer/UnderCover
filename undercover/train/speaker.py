"""Speaker fingerprints: WeSpeaker ResNet34-LM (VoxCeleb, 256-d) through onnxruntime on the CPU.

Same front end as WeSpeaker: 16 kHz audio scaled to int16 range, Kaldi fbank (80 mel, 25/10 ms, hamming,
no dither), per-utterance mean removal. Embeddings are L2-normalised, so cosine similarity is a dot product.
Same-speaker pairs typically score > 0.5, different speakers < 0.3.
"""
import numpy as np
import torch

from ..config import path
from .. import setup_models

SR = 16000


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
