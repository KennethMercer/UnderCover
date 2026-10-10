"""Stage 3: mix the converted vocal back with the instrumental using ffmpeg.

Loudness matching (EBU R128): the converted vocal is set to the original vocal's loudness so the
vocal/instrumental balance matches the original song; the final mix is matched to the song's loudness
and limited. Optional reverb uses a synthetic impulse response.
"""
import os
import re
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ..core import jobs
from ..core.config import CONFIG, path
from ..core.timing import timed

RCFG = CONFIG["remix"]


@jobs.register_user_error   # its messages are written for the user (jobs.friendly shows them as they are)
class RemixError(Exception):
    pass


@dataclass
class MixSettings:
    format: str = RCFG["format"]
    vocal_db: float = RCFG["vocal_db"]
    backing_db: float = RCFG["backing_db"]
    reverb: float | None = None if RCFG["reverb"] == "auto" else float(RCFG["reverb"])  # None = auto
    polish: bool = RCFG["polish"]
    key: float = 0.0     # semitones: transpose instrumental + backing to match the converted vocal


def _ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise RemixError("ffmpeg isn't installed or isn't on PATH. Install it (winget install Gyan.FFmpeg) and restart.")
    return exe


def _run(args: list[str]) -> str:
    p = subprocess.run([_ffmpeg(), "-hide_banner", "-nostdin", *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if p.returncode != 0:
        tail = "\n".join(p.stderr.strip().splitlines()[-5:])
        raise RemixError(f"ffmpeg failed while mixing:\n{tail}")
    return p.stderr


_LUFS: dict = {}
_KEY_LOCKS: dict = {}   # (path, size, mtime) -> LUFS; stems never change, and remix measures some files twice


def lufs(p: Path) -> float:
    """Integrated loudness (LUFS) of a file; -70 for silence. Remembered per file version."""
    st = Path(p).stat()
    key = (str(Path(p).resolve()), st.st_size, st.st_mtime_ns)
    if key in _LUFS:
        return _LUFS[key]
    err = _run(["-nostats", "-i", str(p), "-af", "ebur128=framelog=quiet", "-f", "null", "-"])
    m = re.findall(r"I:\s+(-?[\d.]+|-inf) LUFS", err)
    v = -70.0 if not m or m[-1] == "-inf" else max(float(m[-1]), -70.0)
    _LUFS[key] = v
    return v


def key_shifted(instrumental: Path, key: float, formant: bool = False) -> Path:
    """The instrumental transposed by `key` semitones with Rubber Band (tempo kept), cached on disk per stem
    and key: it's the slowest part of a mix (~10-20 s) and the same song/key is often remixed."""
    import hashlib
    st = Path(instrumental).stat()
    tag = hashlib.sha1(f"{Path(instrumental).resolve()}|{st.st_size}|{st.st_mtime_ns}".encode()).hexdigest()[:16]
    d = path("cache_dir") / "keyshift"
    d.mkdir(parents=True, exist_ok=True)
    out = d / f"{tag}_{key:+g}{'_f' if formant else ''}.wav"
    with _KEY_LOCKS.setdefault(str(out), threading.Lock()):   # the mix waits for a render the prefetch started
        if not out.exists():
            tmp = out.with_suffix(f".{os.getpid()}.part.wav")
            _run(["-y", "-i", str(instrumental), "-af",
                  f"rubberband=pitch={2 ** (key / 12):.6f}:pitchq=quality:channels=together"
                  + (":formant=preserved" if formant else ""),
                  "-c:a", "pcm_f32le", str(tmp)])
            tmp.replace(out)
    return out


def prefetch(files: list, key: float = 0.0, instrumental: Path | None = None, backing: Path | None = None):
    """Start the CPU-side measurements a mix will need (loudness of the stems, the key-shifted instrumental)
    on a background thread, so they overlap with the GPU conversion. Results land in the caches above."""
    import threading

    def work():
        for f in files:
            try:
                if f:
                    lufs(f)
            except Exception:
                pass
        for f, formant in ((instrumental, False), (backing, True)):
            if key and f:
                try:
                    key_shifted(f, key, formant)
                except Exception:
                    pass
    t = threading.Thread(target=work, daemon=True, name="mix-prefetch")
    t.start()
    return t


def _reverb_ir(seconds: float, sr: int) -> Path:
    """Stereo decaying-noise impulse response (simple plate-like reverb), cached on disk."""
    p = path("cache_dir") / f"reverb_ir_{seconds:.1f}s_{sr}.wav"
    if p.exists():
        return p
    import soundfile as sf
    rng = np.random.default_rng(7)
    n = int(seconds * sr)
    t = np.arange(n) / sr
    decay = np.exp(-6.9 * t / seconds)                  # -60 dB at `seconds`
    ir = rng.standard_normal((n, 2)) * decay[:, None]
    # darken the tail: one-pole low-pass whose cutoff falls over time
    a = np.clip(0.2 + 0.75 * t / seconds, 0, 0.95)
    for ch in range(2):
        y = 0.0
        for i in range(n):
            y = a[i] * y + (1 - a[i]) * ir[i, ch]
            ir[i, ch] = y
    pre = int(0.02 * sr)                                 # 20 ms pre-delay
    ir = np.vstack([np.zeros((pre, 2)), ir])
    ir /= np.sqrt((ir ** 2).sum(axis=0, keepdims=True))  # unit energy per channel
    sf.write(str(p), ir.astype(np.float32), sr, subtype="FLOAT")
    return p


def _encode_args(fmt: str) -> list[str]:
    if fmt == "mp3":
        return ["-c:a", "libmp3lame", "-b:a", RCFG["mp3_bitrate"]]
    if fmt == "flac":
        return ["-c:a", "flac", "-sample_fmt", "s32", "-bits_per_raw_sample", "24"]
    if fmt == "wav":
        return ["-c:a", "pcm_s24le"]
    raise RemixError(f"Unknown output format '{fmt}'. Use wav, flac or mp3.")


def _band_power(y: np.ndarray, sr: int, centers: np.ndarray) -> np.ndarray:
    """Average power per 1/3-octave band over the active (non-silent) frames."""
    import librosa
    S = np.abs(librosa.stft(y, n_fft=4096, hop_length=1024)) ** 2
    frame_e = S.sum(axis=0)
    active = frame_e > frame_e.max() * 1e-4  # within 40 dB of the loudest frame
    spec = S[:, active].mean(axis=1) if active.any() else S.mean(axis=1)
    freqs = librosa.fft_frequencies(sr=sr, n_fft=4096)
    lo, hi = centers / 2 ** (1 / 6), centers * 2 ** (1 / 6)
    return np.array([spec[(freqs >= a) & (freqs < b)].sum() + 1e-12 for a, b in zip(lo, hi)])


def polish_vocal(vocal: Path, reference: Path, out: Path) -> dict:
    """Tone-match `vocal` toward `reference` (the original singer's vocal) with a gentle EQ, then de-ess and
    lightly compress. Returns the EQ applied (dB per band) for logging."""
    import librosa
    v, vsr = librosa.load(str(vocal), sr=None, mono=True)
    r, _ = librosa.load(str(reference), sr=vsr, mono=True)
    centers = 1000 * 2 ** (np.arange(-10, 13) / 3)          # 1/3-octave bands, ~100 Hz .. 16 kHz
    centers = centers[centers < vsr * 0.45]                  # nothing to boost above the voice model's bandwidth
    diff = 10 * np.log10(_band_power(r, vsr, centers) / _band_power(v, vsr, centers))
    diff -= np.median(diff)                                  # shape only; loudness is matched separately
    lim = RCFG["eq_match_max_db"]
    gains = np.clip(diff * RCFG["eq_match_strength"], -lim, lim)
    gains[centers < 200] = np.minimum(gains[centers < 200], 0)  # never boost lows: muddy, and bass/kick live there
    # 200 Hz-1.5 kHz carries the voice's identity (formants): only cut there, so matching the original singer's
    # tone never pulls the new voice back toward them; full matching is for presence/air above that (review B6)
    ident = (centers >= 200) & (centers <= 1500)
    gains[ident] = np.minimum(gains[ident], 0)
    gains = np.convolve(np.pad(gains, 1, mode="edge"), [0.25, 0.5, 0.25], mode="valid")  # smooth across bands
    entries = ";".join(f"entry({f:.0f},{g:.2f})" for f, g in zip(centers, gains))
    _run(["-y", "-i", str(vocal), "-af",
          f"firequalizer=gain_entry='{entries}',"
          "atrim=start=0.01,asetpts=PTS-STARTPTS,"   # firequalizer is linear-phase: undo its 10 ms delay (measured 441 samples)
          "deesser=i=0.4:m=0.5:f=0.5,"
          "acompressor=threshold=0.1:ratio=2.5:attack=8:release=120:knee=4",
          "-c:a", "pcm_f32le", str(out)])
    return {f"{f:.0f}": round(float(g), 1) for f, g in zip(centers, gains)}


def remix(instrumental: Path, vocal: Path, original_vocal: Path, out: Path, song: Path | None = None,
          backing: Path | None = None, s: MixSettings | None = None, backing_ref: Path | None = None,
          reverb_ref: Path | None = None) -> Path:
    """reverb_ref: the reverb that de-reverb removed from the original vocal; with reverb 'auto' a matching
    amount is added back to the new vocal."""
    """backing_ref: the ORIGINAL backing stem when `backing` is a converted one; the converted backing is then
    loudness-matched to it and not key-shifted again (conversion already applied the key)."""
    s = s or MixSettings()
    sr = RCFG["sample_rate"]
    if out.suffix.lower() != "." + s.format:   # not with_suffix: titles like "Party In The U.S.A" contain dots
        out = out.with_name(out.name + "." + s.format)
    out.parent.mkdir(parents=True, exist_ok=True)

    tmp = Path(tempfile.mkdtemp(prefix="mix_", dir=path("cache_dir")))
    if s.polish:
        with timed("remix.polish") as rec:
            polished = tmp / "vocal_polished.wav"
            rec["eq"] = polish_vocal(vocal, original_vocal, polished)
            vocal = polished
    reverb = s.reverb
    if reverb is None:  # auto: put back what de-reverb took out (none if it didn't run)
        reverb = min(0.6, 10 ** ((lufs(reverb_ref) - lufs(original_vocal)) / 20)) if reverb_ref else 0.0

    with timed("remix.measure", key=s.key, reverb=round(reverb, 3)):
        vocal_gain = lufs(original_vocal) - lufs(vocal) + s.vocal_db
        target = lufs(song) if (song and RCFG["match_song_loudness"]) else None
        backing_gain = s.backing_db + ((lufs(backing_ref) - lufs(backing)) if (backing and backing_ref) else 0.0)

    if s.key:   # pre-rendered (and cached) while the vocal was converting
        instrumental = key_shifted(instrumental, s.key)
    inputs = ["-i", str(instrumental), "-i", str(vocal)]
    fmt = f"aresample={sr},aformat=sample_fmts=fltp:channel_layouts=stereo"
    # Key changes use Rubber Band pre-rendered by key_shifted() (tempo unchanged; backing vocals keep their formants).
    graph = [f"[0:a]{fmt}[inst]", f"[1:a]{fmt},volume={vocal_gain:.2f}dB[vox]"]
    mix_in = ["[inst]"]
    if reverb > 0:
        inputs += ["-i", str(_reverb_ir(RCFG["reverb_seconds"], sr))]
        graph += ["[vox]asplit[dry][wet_in]",
                  f"[2:a]{fmt}[ir]",
                  "[wet_in][ir]afir[wet]",
                  f"[dry][wet]amix=inputs=2:weights=1 {reverb:.3f}:normalize=0:duration=first[voxr]"]
        mix_in.append("[voxr]")
    else:
        mix_in.append("[vox]")
    if backing:
        idx = len(inputs) // 2
        inputs += ["-i", str(backing)]
        if s.key and not backing_ref:   # original backing: transpose with the song (formants kept), pre-rendered
            inputs[-1] = str(key_shifted(backing, s.key, formant=True))
        graph.append(f"[{idx}:a]{fmt},volume={backing_gain:.2f}dB[bk]")
        mix_in.append("[bk]")
    graph.append(f"{''.join(mix_in)}amix=inputs={len(mix_in)}:normalize=0:duration=first[mix]")

    try:
        premix = tmp / "premix.wav"
        with timed("remix.mix"):
            _run(["-y", *inputs, "-filter_complex", ";".join(graph), "-map", "[mix]",
                  "-c:a", "pcm_f32le", str(premix)])
        master_gain = (target - lufs(premix)) if target is not None else 0.0
        limit = 10 ** (RCFG["limiter_db"] / 20)
        with timed("remix.master", format=s.format):
            _run(["-y", "-i", str(premix), "-af",
                  # 4x oversampled limiting catches inter-sample peaks (a master measured +0.4 dBTP before);
                  # slower release avoids pumping
                  f"volume={master_gain:.2f}dB,aresample=176400,"
                  f"alimiter=limit={limit:.4f}:attack=5:release=150:level=disabled,aresample={sr}",
                  "-ar", str(sr), *_encode_args(s.format), str(out)])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return out
