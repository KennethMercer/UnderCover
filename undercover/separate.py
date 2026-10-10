"""Stage 1: vocal/instrumental separation with audio-separator (Roformer models on the GPU).

Results are cached in cache/stems/<file sha256>/<model>/ so re-runs skip separation.

    python -m undercover.separate song.mp3 [--dereverb] [--karaoke]
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


def _quiet_tqdm(iterable, *args, **kwargs):
    """Stand-in for tqdm inside audio-separator: no console bar, reports chunk progress instead."""
    n = len(iterable) if hasattr(iterable, "__len__") else 0
    for i, item in enumerate(iterable, 1):
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
        cached = {p.stem: p for p in cache_dir.glob("*.wav")} if cache_dir.exists() else {}
        if len(cached) == 1:   # every model writes 2 stems: one alone is a leftover from an interrupted run
            for p in cached.values():
                p.unlink(missing_ok=True)
            cached = {}
        if cached:
            if timing.VERBOSE:
                print(f"[cache] {model} -> {cache_dir}")
            return cached

        sep = self._get(model)
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
            stems[stem] = dest
        return stems


def separate(audio: Path, dereverb: bool | None = None, karaoke: bool | None = None,
             separator: StemSeparator | None = None, quality: str | None = None,
             progress=None) -> dict[str, Path]:
    """Full separation stage. Returns paths for 'vocals' (the vocal to convert),
    'instrumental', and optionally 'backing' (backing vocals, to mix back in)."""
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
            result["vocals"], result["backing"] = k["vocals"], k["instrumental"]

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
