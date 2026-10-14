"""Stage 1: vocal/instrumental separation with audio-separator (Roformer models on the GPU).

Results are cached in cache/stems/<file sha256>/<model>/ so re-runs skip separation.

    python -m undercover.core.separate song.mp3 [--dereverb] [--karaoke]
"""
import argparse
import hashlib
import logging
import shutil
import sys
from pathlib import Path

from .config import CONFIG, path
from . import timing
from .timing import timed

SEP = CONFIG["separation"]
_progress = None  # current per-step callback(fraction), set by StemSeparator.run
_stop = None      # Stop (not Pause): checked before every chunk, set by separate(stop=...); raises Stopped


class Stopped(Exception):
    """Separation was stopped part-way (Stop: within one chunk, about a second of GPU work). Nothing is cached."""

# The separation models audio-separator fetches itself (it writes straight to the final name, so a cut-off download
# leaves a broken file that looks present). Pinned for the Doctor (phase 10): the SHA-256 of the copies this app was
# built and tested with, and the places audio-separator 0.47 takes them from (the first that serves those bytes wins).
_UVR = "https://github.com/TRvlvr/model_repo/releases/download/all_public_uvr_models"
_AS = "https://github.com/nomadkaraoke/python-audio-separator/releases/download/model-configs"
SEPARATOR_SHA256 = {
    "separator/mel_band_roformer_kim_ft_unwa.ckpt": "e6bd8d333880191254a6ef6be3cb0ffa4dda9d3282e36b0cce2e88a660e00d39",
    "separator/config_mel_band_roformer_kim_ft_unwa.yaml": "c910a0b1493fd3f9cee7a576a7498e44e660510dcaaf5d0d50d5363dde1d0010",
    "separator/dereverb_mel_band_roformer_anvuew_sdr_19.1729.ckpt": "9262877b87e9ebb0fb808a456b0a411fa677f5df31c8383c1254af531c078970",
    "separator/dereverb_mel_band_roformer_anvuew.yaml": "1599d9ea717ea2b5b3bc55d936f752b8d0f67baaa3de95acd6d03259a2f37784",
    "separator/mel_band_roformer_karaoke_aufr33_viperx_sdr_10.1956.ckpt": "1de20d459332fe8869aeb01327a31df0032262706e1365114e852dc271779813",
    "separator/mel_band_roformer_karaoke_aufr33_viperx_sdr_10.1956_config.yaml": "b35077d94861f068097cce1a5e54633c055e7dcc2613eade4e4dc7c7c9c3f48b",
}


SEPARATOR_SIZE = {   # bytes: a cut-off download is found from its size alone
    "separator/mel_band_roformer_kim_ft_unwa.ckpt": 913100690,
    "separator/dereverb_mel_band_roformer_anvuew_sdr_19.1729.ckpt": 913107578,
    "separator/mel_band_roformer_karaoke_aufr33_viperx_sdr_10.1956.ckpt": 913096801,
}


def _sources(name: str) -> list:
    return [f"{_UVR}/{name}", f"{_UVR}/mdx_model_data/mdx_c_configs/{name}", f"{_AS}/{name}"]


def _register_pins():
    from . import setup_models
    setup_models.register_pinned({k: _sources(k.split("/", 1)[1]) for k in SEPARATOR_SHA256}, SEPARATOR_SHA256,
                                 "separation", sizes=SEPARATOR_SIZE)


_register_pins()


def _quiet_tqdm(iterable, *args, **kwargs):
    """Stand-in for tqdm inside audio-separator: no console bar, reports chunk progress instead."""
    n = len(iterable) if hasattr(iterable, "__len__") else 0
    for i, item in enumerate(iterable, 1):
        if _stop is not None and _stop():
            raise Stopped()
        yield item
        if _progress and n:
            _progress(i / n)
QUALITY = {"fast": 2, "high": 4, "max": 8}  # preset -> Roformer overlap


def file_hash(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()[:16]


def stem_files(d: Path) -> list:
    """A stem folder's audio files: FLAC (current) or WAV (older caches)."""
    return [p for p in d.glob("*") if p.suffix.lower() in (".flac", ".wav") and ".part" not in p.name]


def to_flac(wav: Path) -> Path:
    """Re-encode a 24-bit stem as FLAC: the same samples, about half the size (stems were 3.5 GB of 12).
    Float stems (not what audio-separator writes here) are left as they are, since FLAC has no float format."""
    import soundfile as sf
    try:
        info = sf.info(str(wav))
        if info.subtype not in ("PCM_24", "PCM_16"):
            return wav
        x, sr = sf.read(str(wav), dtype="int32")
        flac = wav.with_suffix(".flac")
        tmp = wav.with_suffix(".part.flac")
        sf.write(str(tmp), x, sr, subtype=info.subtype, format="FLAC")
        tmp.replace(flac)
        wav.unlink()
        return flac
    except Exception:   # noqa: BLE001 - a WAV works too
        return wav


def audio_seconds(p: Path) -> float:
    import soundfile as sf
    try:
        return sf.info(str(p)).duration
    except Exception:  # mp3 etc. that soundfile can't read: ask librosa (ffmpeg/audioread)
        import librosa
        return librosa.get_duration(path=str(p))


class StemSeparator:
    """Keeps one audio-separator instance per model loaded, so repeat runs skip model load."""

    def __init__(self):
        self._loaded: dict[str, object] = {}
        self.work_dir = path("cache_dir") / "_work"
        self.model_dir = path("models_dir") / "separator"

    def _get(self, model: str):
        if model not in self._loaded:
            from audio_separator.separator import Separator
            from audio_separator.separator.architectures import mdxc_separator
            mdxc_separator.tqdm = _quiet_tqdm
            with timed("separate.load_model", model=model):
                s = Separator(
                    log_level=logging.ERROR,
                    model_file_dir=str(self.model_dir),
                    output_dir=str(self.work_dir),
                    output_format="WAV",
                    use_autocast=SEP["use_autocast"] and CONFIG["gpu"]["fp16"],
                    use_native_fp16=SEP["use_native_fp16"] and CONFIG["gpu"]["fp16"],
                    mdxc_params={
                        "segment_size": SEP["segment_size"],
                        "override_model_segment_size": False,
                        "batch_size": SEP["batch_size"],
                        "overlap": QUALITY[SEP["quality"]],
                        "pitch_shift": 0,
                    },
                )
                s.load_model(model_filename=model)
            self._loaded[model] = s
        return self._loaded[model]

    def run(self, audio: Path, model: str, cache_dir: Path, overlap: int, progress=None) -> dict[str, Path]:
        """Separate `audio` with `model`; returns {stem name (lowercase): wav path}. Cached."""
        cached = {p.stem: p for p in stem_files(cache_dir)} if cache_dir.exists() else {}
        if len(cached) == 1:   # every model writes 2 stems: one alone is a leftover from an interrupted run
            for p in cached.values():
                p.unlink(missing_ok=True)
            cached = {}
        if cached:
            if timing.VERBOSE:
                print(f"[cache] {model} -> {cache_dir}")
            return cached

        sep = self._get(model)
        if _stop is not None and _stop():   # Stop pressed while the model loaded: don't start this pass
            raise Stopped()
        sep.model_instance.overlap = overlap  # read per call, so no reload needed
        shutil.rmtree(self.work_dir, ignore_errors=True)
        self.work_dir.mkdir(parents=True)
        dur = audio_seconds(audio)
        global _progress
        _progress = progress
        try:
            with timed("separate.run", model=model, overlap=overlap, audio_seconds=round(dur, 1)):
                outputs = sep.separate(str(audio))
        finally:
            _progress = None

        cache_dir.mkdir(parents=True, exist_ok=True)
        stems = {}
        for out in outputs:
            out = Path(out)
            if not out.is_absolute():  # returns bare filenames despite its docstring
                out = self.work_dir / out
            # audio-separator names files "<input>_(<Stem>)_<model>.wav"
            stem = out.stem.split("_(")[-1].split(")")[0].lower() if "_(" in out.stem else out.stem.lower()
            dest = cache_dir / f"{stem}.wav"
            shutil.move(str(out), dest)
            stems[stem] = to_flac(dest)
        return stems


LEAD_MISSING_DB = 25.0   # a karaoke lead this far under its backing (RMS) is no lead: two singers in harmony


def lead_missing(lead: Path, backing: Path) -> bool:
    """The karaoke split found no lead: its lead stem is (nearly) silent while the backing sings. Two voices in
    harmony all the way (a duet) look like backing vocals to it (phase 22: The Sound of Silence's lead stem was
    digital silence, -164 dB, against -24 dB of backing)."""
    import numpy as np
    import soundfile as sf
    rms = lambda p: 20 * np.log10(np.sqrt(np.mean(sf.read(str(p), dtype="float32")[0] ** 2)) + 1e-9)
    return rms(lead) < rms(backing) - LEAD_MISSING_DB


def separate(audio: Path, dereverb: bool | None = None, karaoke: bool | None = None,
             separator: StemSeparator | None = None, quality: str | None = None,
             progress=None, one_voice_if_no_lead: bool = False, stop=None) -> dict[str, Path]:
    """Full separation stage. Returns paths for 'vocals' (the vocal to convert),
    'instrumental', and optionally 'backing' (backing vocals, to mix back in).
    stop: a Stop check (never Pause): true -> Stopped is raised within one chunk (the user: "the stop has to be a
    stop"; an 83-minute rally video took minutes to notice a Stop checked only between files)."""
    global _stop
    _stop = stop
    try:
        return _separate(audio, dereverb, karaoke, separator, quality, progress, one_voice_if_no_lead)
    finally:
        _stop = None


def _separate(audio, dereverb, karaoke, separator, quality, progress, one_voice_if_no_lead) -> dict[str, Path]:
    """separate() without the stop wiring.
    one_voice_if_no_lead (covers): when the karaoke split finds no lead (a duet, lead_missing), the whole vocal is the
    lead and there's no backing. Training leaves it off: a duet's blended voices aren't one person's clips."""
    audio = Path(audio).resolve()
    if not audio.exists():
        raise FileNotFoundError(f"Song not found: {audio}")
    dereverb = SEP["dereverb"] if dereverb is None else dereverb
    karaoke = SEP["karaoke"] if karaoke is None else karaoke
    separator = separator or StemSeparator()
    quality = quality or SEP["quality"]
    if quality not in QUALITY:
        raise ValueError(f"Unknown separation quality '{quality}' (use fast, high or max)")
    ov = QUALITY[quality]
    sfx = "" if quality == "high" else f"_{quality}"  # "high" keeps the original cache layout

    root = path("cache_dir") / "stems" / file_hash(audio)
    # progress weights per pass (the karaoke/dereverb passes run on the vocal stem only)
    steps = ["main"] + (["karaoke"] if karaoke else []) + (["dereverb"] if dereverb else [])
    weights = {"main": 1.0, "karaoke": 0.5, "dereverb": 0.5}
    total_w = sum(weights[x] for x in steps)

    def step_cb(name):
        before = sum(weights[x] for x in steps[:steps.index(name)])
        return (lambda f: progress((before + f * weights[name]) / total_w, name)) if progress else None
    with timed("separate.total", file=audio.name, dereverb=dereverb, karaoke=karaoke, quality=quality):
        main = separator.run(audio, SEP["vocal_model"], root / (Path(SEP["vocal_model"]).stem + sfx), ov,
                              step_cb("main"))
        # vocal-only models name the remainder "other" (or similar) instead of "instrumental"
        inst = main.get("instrumental") or next(v for k, v in main.items() if k != "vocals")
        result = {"vocals": main["vocals"], "instrumental": inst}

        if karaoke:  # lead vs backing vocals, run on the vocal stem
            k = separator.run(result["vocals"], SEP["karaoke_model"],
                              root / (Path(SEP["vocal_model"]).stem + sfx) / ("karaoke_" + Path(SEP["karaoke_model"]).stem), ov,
                              step_cb("karaoke"))
            if not (one_voice_if_no_lead and lead_missing(k["vocals"], k["instrumental"])):
                result["vocals"], result["backing"] = k["vocals"], k["instrumental"]
            # else: no lead to be found (a duet): the whole vocal is converted as one voice, with no backing stem

        if dereverb:  # dry lead vocal converts more cleanly
            parent = result["vocals"].parent
            d = separator.run(result["vocals"], SEP["dereverb_model"],
                              parent / ("dereverb_" + Path(SEP["dereverb_model"]).stem), ov,
                              step_cb("dereverb"))
            # anvuew's model outputs "noreverb" and "reverb" stems
            result["vocals"] = d.get("noreverb") or next(v for k, v in d.items() if k != "reverb")
            if d.get("reverb"):
                result["reverb"] = d["reverb"]  # what was removed; used to put a matching amount back
    return result


def main():
    ap = argparse.ArgumentParser(description="Separate a song into vocals and instrumental.")
    ap.add_argument("song", type=Path)
    ap.add_argument("--dereverb", action="store_true", default=None)
    ap.add_argument("--karaoke", action="store_true", default=None, help="split lead from backing vocals")
    ap.add_argument("--quality", choices=list(QUALITY), help="fast | high | max")
    a = ap.parse_args()
    try:
        stems = separate(a.song, a.dereverb, a.karaoke, quality=a.quality)
    except FileNotFoundError as e:
        sys.exit(str(e))
    for k, v in stems.items():
        print(f"{k:13s} {v}")


if __name__ == "__main__":
    main()
