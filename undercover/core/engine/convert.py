"""Stage 2: RVC v1/v2 voice conversion (RMVPE pitch, HuBERT content features, optional .index).

Port of RVC's inference pipeline (infer/vc/pipeline.py) onto the vendored modules in undercover/rvc,
with HuBERT, RMVPE and recently used voices kept loaded on the GPU.

    python -m undercover.core.engine.convert vocals.wav --voice plankton --pitch 4
"""
import argparse
import sys
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import torch
import torch.nn.functional as F
from scipy import signal

from ..config import CONFIG, path
from ..voices import ModelError, Voice, get_voice
from ..timing import timed

CCFG = CONFIG["conversion"]
SR16 = 16000
WINDOW = 160  # samples per f0 frame at 16 kHz (10 ms)
F0_MIN, F0_MAX = 50, 1100
F0_MEL_MIN = 1127 * np.log(1 + F0_MIN / 700)
F0_MEL_MAX = 1127 * np.log(1 + F0_MAX / 700)
_BH, _AH = signal.butter(N=5, Wn=48, btype="high", fs=SR16)


@dataclass
class Settings:
    pitch: float = CCFG["pitch"]
    index_ratio: float = CCFG["index_ratio"]
    protect: float = CCFG["protect"]
    volume_envelope: float = CCFG["volume_envelope"]
    pitch_cleanup: bool = CCFG["pitch_cleanup"]          # repair octave glitches / blips in the pitch track
    pitch_correction: float = CCFG["pitch_correction"]   # snap to the song's scale: 0 off .. 1 hard autotune
    song_key: tuple | None = None                          # (tonic 0-11, 'major'|'minor') of the ORIGINAL song


class LoadedVoice:
    def __init__(self, voice: Voice, device, half: bool):
        from .rvc.index import FeatureIndex, IndexFormatError
        from .rvc.synth import (SynthesizerTrnMs256NSFsid, SynthesizerTrnMs256NSFsid_nono, SynthesizerTrnMs768NSFsid,
                                SynthesizerTrnMs768NSFsid_nono)
        ck = torch.load(voice.pth, map_location="cpu", weights_only=True)
        from ..voices import check_config, display_name
        check_config(ck, display_name(voice))   # (before the network is built from it: RVC's own shapes only)
        self.version = ck.get("version", "v1")
        self.f0 = int(ck.get("f0", 1))
        cfg = list(ck["config"])
        cfg[-3] = ck["weight"]["emb_g.weight"].shape[0]  # n_spk
        self.tgt_sr = cfg[-1] if isinstance(cfg[-1], int) else int(str(cfg[-1]).rstrip("k")) * 1000
        cls = {("v1", 1): SynthesizerTrnMs256NSFsid, ("v1", 0): SynthesizerTrnMs256NSFsid_nono,
               ("v2", 1): SynthesizerTrnMs768NSFsid, ("v2", 0): SynthesizerTrnMs768NSFsid_nono}[(self.version, self.f0)]
        net = cls(*cfg, is_half=half)
        del net.enc_q
        net.load_state_dict(ck["weight"], strict=False)
        net = net.eval().to(device)
        self.net = net.half() if half else net.float()
        self.index = None
        self.index_note = "no index"
        if voice.index:
            try:
                self.index = FeatureIndex(voice.index, device)
                self.index_note = f"index {len(self.index.vecs)} vectors"
            except (IndexFormatError, OSError) as e:
                self.index_note = f"index unreadable ({e}); converting without it"
        self.name = voice.name


class Converter:
    """Holds the shared models (HuBERT, RMVPE) and an LRU of loaded voices."""

    def __init__(self, device: str = "cuda"):
        from .rvc.hubert import load_hubert
        from .rvc.rmvpe import RMVPE
        self.device = torch.device(device if torch.cuda.is_available() else "cpu")
        self.half = CONFIG["gpu"]["fp16"] and self.device.type == "cuda"
        mdir = path("models_dir")
        from .. import setup_models
        try:
            setup_models.ensure()  # no-op when present; first run downloads ~370 MB
        except Exception as e:
            raise ModelError(f"Couldn't download the shared voice-conversion models ({e.__class__.__name__}). "
                             "Check your internet connection and try again.")
        with timed("convert.load_shared"):
            self.hubert = load_hubert(mdir / CCFG["hubert_dir"], self.device, self.half)
            self.rmvpe = RMVPE(mdir / CCFG["rmvpe"], self.device, self.half)
        self.voices: OrderedDict[str, LoadedVoice] = OrderedDict()

    def voice(self, name: str) -> LoadedVoice:
        if name in self.voices:
            self.voices.move_to_end(name)
            return self.voices[name]
        v = get_voice(name)
        with timed("convert.load_voice", voice=v.name):
            lv = LoadedVoice(v, self.device, self.half)
        self.voices[name] = lv
        while len(self.voices) > CCFG["voice_cache"]:
            self.voices.popitem(last=False)
            torch.cuda.empty_cache()
        return lv

    # ------------------------------------------------------------ pipeline pieces

    def _f0(self, audio16: np.ndarray, p_len: int, s: "Settings"):
        from .rvc.f0fix import clean_f0, snap_to_key
        pitch = s.pitch
        f0 = self.rmvpe.infer_from_audio(audio16, thred=0.03)[:p_len]
        if len(f0) < p_len:
            f0 = np.pad(f0, (0, p_len - len(f0)))
        if s.pitch_cleanup:
            f0 = clean_f0(f0)
        if s.pitch_correction > 0 and s.song_key:
            f0 = snap_to_key(f0, s.song_key[0], s.song_key[1], s.pitch_correction)  # in the original key, before shifting
        # Unvoiced frames stay 0 (classic RVC behaviour; the current upstream interpolates them,
        # which disables `protect` and pitches breaths/consonants).
        f0 = f0 * pow(2, pitch / 12)
        f0_mel = 1127 * np.log(1 + f0 / 700)
        voiced = f0_mel > 0
        f0_mel[voiced] = (f0_mel[voiced] - F0_MEL_MIN) * 254 / (F0_MEL_MAX - F0_MEL_MIN) + 1
        f0_mel = np.clip(f0_mel, 1, 255)
        coarse = np.rint(f0_mel).astype(np.int64)
        return (torch.from_numpy(coarse).to(self.device)[None],
                torch.from_numpy(f0.astype(np.float32)).to(self.device)[None])

    @torch.no_grad()
    def _vc_chunk(self, lv: LoadedVoice, audio: np.ndarray, pitch, pitchf, s: Settings) -> np.ndarray:
        # Round the chunk up to a fixed size bucket (silence at the end, trimmed off the output): the GPU tunes
        # kernels per input shape, and a new length costs ~3 s the first time (measured 3.3-4x slower), so
        # reusing a few bucket sizes keeps later chunks and later songs warm.
        n = audio.shape[0]
        bucket = int(SR16 * CCFG.get("shape_bucket", 0))
        if bucket:
            padded = -(-n // bucket) * bucket
            if padded > n:
                audio = np.pad(audio, (0, padded - n))
                if pitch is not None:
                    extra = padded // WINDOW - pitch.shape[1]
                    if extra > 0:
                        pitch = F.pad(pitch, (0, extra), value=1)      # coarse pitch 1 = unvoiced
                        pitchf = F.pad(pitchf, (0, extra), value=0.0)
        out = self._vc_chunk_raw(lv, audio, pitch, pitchf, s)
        return out[: (n // WINDOW) * (lv.tgt_sr // 100)] if bucket else out

    def _vc_chunk_raw(self, lv: LoadedVoice, audio: np.ndarray, pitch, pitchf, s: Settings) -> np.ndarray:
        dtype = torch.float16 if self.half else torch.float32
        wav = torch.from_numpy(audio).to(self.device, dtype)[None]
        feats = self.hubert.extract(wav, lv.version)                    # (1, T50, D)
        protect_on = s.protect < 0.5 and pitch is not None
        feats0 = feats.clone() if protect_on else None
        if lv.index is not None and s.index_ratio > 0:
            retrieved = lv.index.retrieve(feats[0])[None].to(feats.dtype)
            feats = retrieved * s.index_ratio + feats * (1 - s.index_ratio)
        feats = F.interpolate(feats.transpose(1, 2), scale_factor=2).transpose(1, 2)   # -> 100 fps
        if protect_on:
            feats0 = F.interpolate(feats0.transpose(1, 2), scale_factor=2).transpose(1, 2)
        p_len = min(audio.shape[0] // WINDOW, feats.shape[1])
        feats = feats[:, :p_len]
        if pitch is not None:
            pitch, pitchf = pitch[:, :p_len], pitchf[:, :p_len]
        if protect_on:
            mask = torch.where(pitchf > 0, 1.0, s.protect).unsqueeze(-1).to(feats.dtype)
            feats = feats * mask + feats0[:, :p_len] * (1 - mask)
        lengths = torch.tensor([p_len], device=self.device)
        sid = torch.tensor([0], device=self.device)
        if pitch is not None:
            out = lv.net.infer(feats, lengths, pitch, pitchf, sid)[0]  # pitchf stays fp32 (phase accuracy)
        else:
            out = lv.net.infer(feats, lengths, sid)[0]
        return out[0, 0].float().cpu().numpy()

    def convert(self, audio16: np.ndarray, voice: str, s: Settings | None = None,
                progress=None) -> tuple[np.ndarray, int]:
        """16 kHz mono float vocal -> converted vocal (float32) at the voice's sample rate.
        progress(fraction) is called as pitch extraction and each chunk finish.
        Long silences (common: ~half of a separated lead vocal) are skipped: only the sung regions are
        converted, each on its own, and placed back at their exact positions."""
        s = s or Settings()
        report = progress or (lambda f: None)
        peak = np.abs(audio16).max() / 0.95
        if peak > 1:   # once for the whole vocal, so every region shares one scale
            audio16 = audio16 / peak
        regions = sung_regions(audio16) if CCFG.get("skip_silence", True) else [(0, len(audio16))]
        if len(regions) <= 1 and (not regions or regions[0] == (0, len(audio16))):
            return self._convert_one(audio16, voice, s, report)
        tgt_sr = (voice if isinstance(voice, LoadedVoice) else self.voice(voice)).tgt_sr
        out = np.zeros(int(round(len(audio16) / SR16 * tgt_sr)), np.float32)
        total, done = sum(b - a for a, b in regions), 0
        with timed("convert.regions", regions=len(regions), sung_seconds=round(total / SR16, 1),
                   audio_seconds=round(len(audio16) / SR16, 1)):
            for a, b in regions:
                y, _ = self._convert_one(audio16[a:b], voice, s,
                                         lambda f, a=a, b=b, done=done: report((done + f * (b - a)) / total),
                                         pad_s=CCFG.get("region_pad", 1.0))
                o = int(round(a / SR16 * tgt_sr))
                y = y[: len(out) - o]
                out[o:o + len(y)] = y
                done += b - a
        return out, tgt_sr

    def _convert_one(self, audio16: np.ndarray, voice: str, s: Settings, progress=None,
                     pad_s: float | None = None) -> tuple[np.ndarray, int]:
        """Convert one continuous stretch (chunked at quiet points every ~chunk_center s). pad_s: reflected context
        added at each end (chunk_pad by default; sung regions already start and end in silence, so need less)."""
        report = progress or (lambda f: None)
        lv = voice if isinstance(voice, LoadedVoice) else self.voice(voice)
        tgt_sr = lv.tgt_sr
        pad_s = CCFG["chunk_pad"] if pad_s is None else pad_s
        t_pad, t_pad_tgt = int(SR16 * pad_s), int(tgt_sr * pad_s)
        t_query, t_center, t_max = SR16 * CCFG["chunk_query"], SR16 * CCFG["chunk_center"], SR16 * CCFG["chunk_max"]

        peak = np.abs(audio16).max() / 0.95
        if peak > 1:
            audio16 = audio16 / peak
        audio = signal.filtfilt(_BH, _AH, audio16).astype(np.float32)

        # split points: quietest spot within +-query of every `center` seconds
        opt_ts = []
        audio_pad = np.pad(audio, (WINDOW // 2, WINDOW // 2), mode="reflect")
        if audio_pad.shape[0] > t_max:
            energy = np.convolve(np.abs(audio_pad), np.ones(WINDOW), mode="valid")[:len(audio)]
            for t in range(t_center, audio.shape[0], t_center):
                seg = energy[t - t_query:t + t_query]
                opt_ts.append(t - t_query + int(np.argmin(seg)))

        audio_pad = np.pad(audio, (t_pad, t_pad), mode="reflect")
        p_len = audio_pad.shape[0] // WINDOW
        pitch = pitchf = None
        with timed("convert.f0", seconds_audio=round(len(audio) / SR16, 1)):
            if lv.f0:
                pitch, pitchf = self._f0(audio_pad, p_len, s)
        report(0.15)

        pieces, start = [], 0
        bounds = [t // WINDOW * WINDOW for t in opt_ts]
        from .. import lowprofile
        pacer = lowprofile.Pacer()   # (Low profile: a pause after each chunk, in proportion to its work)
        with timed("convert.synth", voice=lv.name, chunks=len(bounds) + 1, audio_seconds=round(len(audio) / SR16, 1)):
            for t in bounds + [None]:
                end = None if t is None else t + 2 * t_pad + WINDOW
                fs, fe = start // WINDOW, None if t is None else (t + 2 * t_pad) // WINDOW
                chunk = audio_pad[start:end]
                pc = pitch[:, fs:fe] if pitch is not None else None
                pfc = pitchf[:, fs:fe] if pitchf is not None else None
                piece = self._vc_chunk(lv, chunk, pc, pfc, s)[t_pad_tgt:-t_pad_tgt]
                pacer.tick()
                if t is not None:
                    # the chunk runs one frame (10 ms) past the next boundary; without this trim every seam pushed
                    # the rest of the vocal 10 ms later (measured ~20 ms drift by 2 min after exact chunk lengths)
                    piece = piece[: (t - start) // WINDOW * (tgt_sr // 100)]
                    start = t
                pieces.append(piece)
                report(0.15 + 0.85 * len(pieces) / (len(bounds) + 1))
        out = np.concatenate(pieces)
        if s.volume_envelope != 1:
            out = _match_envelope(audio, out, tgt_sr, s.volume_envelope)
        return out.astype(np.float32), tgt_sr


def sung_regions(audio16: np.ndarray, gap: float = 1.5, margin: float = 0.3) -> list[tuple[int, int]]:
    """Sample ranges that contain voice, split at silences of at least `gap` seconds and padded by `margin`.
    Silence = more than 45 dB below the vocal's loud level (and below -60 dBFS at most)."""
    hop, win = SR16 // 100, SR16 // 50
    if len(audio16) < win * 4:
        return [(0, len(audio16))]
    c = np.concatenate([[0.0], np.cumsum(audio16.astype(np.float64) ** 2)])
    st = np.arange(0, len(audio16) - win + 1, hop)
    db = 10 * np.log10((c[st + win] - c[st]) / win + 1e-12)
    loud = np.percentile(db[db > -90], 95) if np.any(db > -90) else -90
    active = db > min(-60.0, loud - 45.0)
    if not active.any():
        return []
    m, g = int(margin * 100), int(gap * 100)
    idx = np.flatnonzero(active)
    regions, a, prev = [], idx[0], idx[0]
    for i in idx[1:]:
        if i - prev > g:
            regions.append((a, prev))
            a = i
        prev = i
    regions.append((a, prev))
    out = []
    for a, b in regions:
        s0, s1 = max(0, (a - m) * hop), min(len(audio16), (b + m) * hop + win)
        if out and s0 <= out[-1][1]:
            out[-1] = (out[-1][0], s1)
        else:
            out.append((s0, s1))
    return out


def _match_envelope(src16: np.ndarray, out: np.ndarray, out_sr: int, rate: float) -> np.ndarray:
    """RVC's change_rms: blend the output's loudness envelope toward the source's."""
    import librosa
    r1 = librosa.feature.rms(y=src16, frame_length=SR16 // 2 * 2, hop_length=SR16 // 2)
    r2 = librosa.feature.rms(y=out, frame_length=out_sr // 2 * 2, hop_length=out_sr // 2)
    r1 = F.interpolate(torch.from_numpy(r1)[None], size=out.shape[0], mode="linear").squeeze().numpy()
    r2 = F.interpolate(torch.from_numpy(r2)[None], size=out.shape[0], mode="linear").squeeze().numpy()
    r2 = np.maximum(r2, 1e-6)
    return out * (np.power(r1, 1 - rate) * np.power(r2, rate - 1))


def load_16k(p: Path) -> np.ndarray:
    import librosa
    audio, _ = librosa.load(str(p), sr=SR16, mono=True)
    return audio


def convert_file(src: Path, voice: str, out: Path, settings: Settings | None = None,
                 converter: Converter | None = None) -> Path:
    import soundfile as sf
    converter = converter or Converter()
    with timed("convert.total", file=Path(src).name, voice=voice):
        audio, sr = converter.convert(load_16k(src), voice, settings)
        out.parent.mkdir(parents=True, exist_ok=True)
        sf.write(str(out), audio, sr, subtype="FLOAT")
    return out


def main():
    ap = argparse.ArgumentParser(description="Convert a vocal track to another voice (RVC).")
    ap.add_argument("vocal", type=Path)
    ap.add_argument("--voice", required=True)
    ap.add_argument("--pitch", type=float, default=CCFG["pitch"], help="semitones")
    ap.add_argument("--index-ratio", type=float, default=CCFG["index_ratio"])
    ap.add_argument("--protect", type=float, default=CCFG["protect"])
    ap.add_argument("--volume-envelope", type=float, default=CCFG["volume_envelope"])
    ap.add_argument("-o", "--out", type=Path)
    a = ap.parse_args()
    out = a.out or path("output_dir") / f"{a.vocal.stem}_{a.voice}.wav"
    try:
        convert_file(a.vocal, a.voice, out, Settings(a.pitch, a.index_ratio, a.protect, a.volume_envelope))
    except ModelError as e:
        sys.exit(str(e))
    print(out)


if __name__ == "__main__":
    main()
