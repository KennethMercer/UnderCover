"""End-to-end cover pipeline shared by the CLI and the web UI.

separate -> convert -> remix, with models kept loaded between runs, per-stage progress callbacks,
timings, and a history log (output/history.jsonl).
"""
import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from ..core import tray
from ..core.config import CONFIG, path
from . import history as hstore


DUET_NOTE = "Two singers in harmony: UnderCover can't separate them yet, so they're sung as one voice."   # phase 22: said plainly (the user: both ways were bad)


@dataclass
class CoverOptions:
    voice: str
    pitch: float | None = None      # legacy: vocal-only semitones (out of key unless a multiple of 12)
    key: float | None = None        # semitones, transposes the whole song
    octave: int | None = None       # new vocal only, whole octaves
    index_ratio: float | None = None
    protect: float | None = None
    volume_envelope: float | None = None
    dereverb: bool | None = None
    karaoke: bool | None = None
    quality: str | None = None      # separation: fast | high | max
    reverb: float | None = None      # None = auto (match what de-reverb removed)
    polish: bool | None = None       # tone-match EQ + de-ess + gentle compression on the new vocal
    vocal_db: float | None = None
    backing_db: float | None = None
    format: str | None = None
    pitch_cleanup: bool | None = None     # repair pitch-tracker glitches (default on)
    pitch_correction: float | None = None # snap to the song's scale, 0..1
    backing_voice: str | None = None  # second voice for the backing vocals (needs the lead/backing split)
    backing_octave: int | None = None # backing voice only, whole octaves (on top of the song key)
    fit: bool | None = None           # set octave + key from the voice's range and the song's melody (overrides both)
    lead_gate: bool | None = None     # only singing gets converted (phase 15; default on): see leadgate.py
    chorus: str | None = None         # lines in the backing stem: convert | top | keep (leadgate.CHORUS_MODES; None: config)


@dataclass
class CoverResult:
    output: Path
    song: Path
    voice: str
    stems: dict
    converted_vocal: Path
    converted_backing: Path | None = None
    notes: list = field(default_factory=list)
    seconds: dict = field(default_factory=dict)
    options: dict = field(default_factory=dict)
    created: str = ""
    title: str = ""
    id: str = ""   # permanent history id, set when the cover is recorded (frontman/history.py)


# progress(stage, fraction 0..1, message)
Progress = Callable[[str, float, str], None]


def _safe(s: str) -> str:
    return re.sub(r'[<>:"/\\|?*]+', "_", s).strip() or "cover"


class CoverEngine:
    """Makes covers with the shared, long-lived separator and converter models (core.gpu)."""

    @property
    def separator(self):
        from ..core.gpu import MODELS
        return MODELS.separator

    @property
    def converter(self):
        from ..core.gpu import MODELS
        return MODELS.converter

    @property
    def _conv(self):
        from ..core.gpu import MODELS
        return MODELS._conv

    def _fit(self, opts: "CoverOptions", voice, stems: dict, notes: list, bvoice) -> "CoverOptions":
        """Octave + key that put the melody as fully inside the voice's range as possible (fit.pick_fit).
        Needs a voice trained here (its range is measured); others keep the chosen octave/key."""
        from dataclasses import replace
        from .fit import pick_fit
        from ..core.engine.convert import load_16k
        ex = voice.extra or {}
        rng = ex.get("f0_range") or ex.get("person_range")   # trained here, or the checked person's range
        if not rng:
            notes.append(f"Fit to voice: {voice.name} has no measured range yet (voices trained in UnderCover have one, "
                         "and voices a one-shot cover checked against a person), so your octave and key were used.")
            return opts
        f0 = self.converter.rmvpe.infer_from_audio(load_16k(stems["vocals"]), thred=0.03)
        speech = ex.get("from_speech") if ex.get("f0_range") else ex.get("person_range_speech")
        fit = pick_fit(rng, bool(speech), f0)
        notes.append(f"Fit to voice: octave {fit['octave']:+d}, key {fit['key']:+d} "
                     f"({fit['outside']:.0%} of the melody outside {voice.name}'s range {rng[0]:.0f}-{rng[2]:.0f} Hz).")
        same_backing = bvoice is not None and bvoice.name == voice.name
        return replace(opts, octave=fit["octave"], key=fit["key"], pitch=None,
                       backing_octave=fit["octave"] if same_backing else opts.backing_octave)

    def make_cover(self, song: Path, opts: CoverOptions, out: Path | None = None,
                   progress: Progress | None = None, title: str | None = None,
                   stems: dict | None = None, record: bool = True, key_song: Path | None = None) -> CoverResult:
        """stems: pre-separated (e.g. sliced) stems, skips separation. record: add to history.
        key_song: file to detect the musical key from (defaults to `song`)."""
        import numpy as np
        import soundfile as sf
        from ..core.engine.convert import Settings, load_16k
        from ..core.voices import get_voice
        from .remix import MixSettings, remix
        from ..core.separate import separate

        report = progress or (lambda *a: None)
        song = Path(song).resolve()
        title = title or upload_title(song) or song.stem
        stamp = time.strftime("%Y%m%d-%H%M%S")
        voice = get_voice(opts.voice)  # fail fast if not installed
        bvoice = get_voice(opts.backing_voice) if opts.backing_voice else None
        karaoke = True if bvoice else opts.karaoke  # a backing voice needs the lead/backing split
        secs, notes = {}, []

        report("separating", 0.0, "Separating vocals from the instrumental")
        t = time.perf_counter()
        stems = stems or separate(song, opts.dereverb, karaoke, separator=self.separator, quality=opts.quality, one_voice_if_no_lead=True,
                         progress=lambda f, step: report("separating", f, {"main": "Separating vocals", "karaoke": "Splitting lead and backing vocals", "dereverb": "Removing reverb"}[step]))
        secs["separate"] = round(time.perf_counter() - t, 2)
        report("separating", 1.0, "Stems ready")
        if karaoke and not stems.get("backing"):   # core.separate found no lead to split from the backing (a duet)
            notes.append(DUET_NOTE)
        if opts.fit:
            opts = self._fit(opts, voice, stems, notes, bvoice)

        report("converting", 0.0, f"Converting the vocal to {voice.name}")
        t = time.perf_counter()
        s = Settings()
        for k in ("index_ratio", "protect", "volume_envelope", "pitch_cleanup", "pitch_correction"):
            if getattr(opts, k) is not None:
                setattr(s, k, getattr(opts, k))
        if s.pitch_correction > 0:
            k = song_key(key_song or song)
            s.song_key = (k["tonic"], k["mode"])
        key, s.pitch = pitch_plan(opts)
        # is the lead stem singing? (phase 15) an instrument left in it isn't turned into singing, and lines the
        # karaoke split put in the backing are converted too; the stems themselves stay as they were
        cstems = stems
        if opts.lead_gate is not False:
            from .leadgate import CHORUS_MODES, apply as gate_apply, note as gate_note, plan as gate_plan, split_top
            g = gate_plan(load_16k(stems["vocals"]), load_16k(stems["backing"]) if stems.get("backing") else None)
            if g["instrument"] or g["chorus"]:
                gwork = path("cache_dir") / "converted"
                gwork.mkdir(parents=True, exist_ok=True)
                mode = opts.chorus or CONFIG["conversion"].get("chorus", "keep")
                mode = mode if mode in CHORUS_MODES else "keep"
                top = (split_top(stems["backing"], self.separator, Path(stems["backing"]).parent)
                       if mode == "top" and g["chorus"] and stems.get("backing") else None)
                cstems = gate_apply(stems, g, gwork, stamp, chorus=mode, top=top)
                notes += gate_note(g, mode)
        from .remix import lufs, prefetch
        # mix prep (stem loudness, key-shifted instrumental) runs on the CPU while the GPU converts
        prefetch([cstems["vocals"], cstems.get("backing"), cstems.get("reverb"), song], key=key,
                 instrumental=cstems["instrumental"], backing=None if bvoice else cstems.get("backing"))
        if bvoice and not cstems.get("backing"):   # sung as one voice (a duet): no backing stem to convert
            bvoice = None
        elif bvoice and lufs(cstems["backing"]) < -45:  # nothing worth converting
            notes.append("This song has almost no backing vocals, so the backing voice wasn't used.")
            bvoice = None
        lead_share = 0.5 if bvoice else 1.0
        audio, sr = self.converter.convert(load_16k(cstems["vocals"]), voice.name, s,
                                           progress=lambda f: report("converting", f * lead_share, f"Converting the lead vocal to {voice.name}"))
        work = path("cache_dir") / "converted"
        work.mkdir(parents=True, exist_ok=True)
        # 24-bit FLAC: ~half the size of float WAV, far below audible precision (kept for "vocal only" downloads)
        conv_path = work / f"{_safe(title)}__{voice.name}__{stamp}.flac"
        sf.write(str(conv_path), np.clip(audio, -1, 1), sr, subtype="PCM_24")
        conv_backing = None
        if bvoice:
            from dataclasses import replace
            boct = int(_conv_cfg()["octave"] if opts.backing_octave is None else opts.backing_octave)
            bs = replace(s, pitch=key + 12 * boct)
            baudio, bsr = self.converter.convert(load_16k(cstems["backing"]), bvoice.name, bs,
                                                 progress=lambda f: report("converting", 0.5 + 0.5 * f, f"Converting the backing vocals to {bvoice.name}"))
            conv_backing = work / f"{_safe(title)}__{bvoice.name}__backing__{stamp}.flac"
            sf.write(str(conv_backing), np.clip(baudio, -1, 1), bsr, subtype="PCM_24")
        secs["convert"] = round(time.perf_counter() - t, 2)
        report("converting", 1.0, "Vocal converted")

        report("mixing", 0.0, "Mixing and matching loudness")
        t = time.perf_counter()
        ms = MixSettings(key=key)
        for k in ("reverb", "vocal_db", "backing_db", "format", "polish"):
            if getattr(opts, k) is not None:
                setattr(ms, k, getattr(opts, k))
        names = voice.name + (f" + {bvoice.name}" if bvoice and bvoice.name != voice.name else "")
        out = out or _unique(path("output_dir") / f"{_safe(title)} ({names})", ms.format)
        final = remix(cstems["instrumental"], conv_path, cstems["vocals"], out, song=song,
                      backing=conv_backing or cstems.get("backing"), s=ms,
                      backing_ref=cstems.get("backing") if conv_backing else None,
                      reverb_ref=cstems.get("reverb"))
        secs["remix"] = round(time.perf_counter() - t, 2)
        secs["total"] = round(sum(secs.values()), 2)
        report("done", 1.0, "Finished")

        res = CoverResult(output=final, song=song, voice=voice.name,
                          stems={k: str(v) for k, v in stems.items()}, converted_vocal=conv_path,
                          converted_backing=conv_backing, notes=notes,
                          seconds=secs, options={k: v for k, v in asdict(opts).items() if v is not None},
                          created=time.strftime("%Y-%m-%d %H:%M:%S"), title=title)
        if record:
            res.id = _append_history(res)["id"]
        return res

    def make_preview(self, song: Path, opts: CoverOptions, variants: list[dict], seconds: float = 15.0,
                     progress: Progress | None = None) -> list[dict]:
        """Render a short excerpt (the part with the most singing) once per variant (option overrides),
        so settings can be compared by ear. Separation is shared with (and cached for) the full cover."""
        import subprocess
        import uuid
        from dataclasses import replace
        from ..core.separate import separate
        report = progress or (lambda *a: None)
        song = Path(song).resolve()
        karaoke = True if opts.backing_voice else opts.karaoke
        report("separating", 0.0, "Separating vocals (needed once; the full cover reuses it)")
        stems = separate(song, opts.dereverb, karaoke, separator=self.separator, quality=opts.quality, one_voice_if_no_lead=True,
                         progress=lambda f, step: report("separating", f, "Separating vocals"))
        start, dur = busiest_window(stems["vocals"], seconds)
        work = path("cache_dir") / "previews" / uuid.uuid4().hex[:10]
        work.mkdir(parents=True, exist_ok=True)

        def cut(src: Path, name: str) -> Path:
            dst = work / f"{name}.wav"
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{start:.2f}", "-t", f"{dur:.2f}", "-i", str(src),
                            "-c:a", "pcm_f32le", str(dst)], check=True)
            return dst
        sliced = {k: cut(Path(v), k) for k, v in stems.items()}
        song_cut = cut(song, "song")
        out = []
        for i, v in enumerate(variants):
            report("rendering", i / len(variants), f"Rendering {v.get('label', i + 1)}")
            overrides = {k: v[k] for k in v if k != "label" and hasattr(opts, k)}
            o = replace(opts, **overrides, format="mp3")
            r = self.make_cover(song_cut, o, out=work / f"variant_{i}", stems=sliced, record=False,
                                key_song=song, title="preview")
            out.append({"label": v.get("label", str(i + 1)), "file": r.output, "options": overrides})
        report("rendering", 1.0, "Previews ready")
        return out


def busiest_window(vocals: Path, seconds: float) -> tuple[float, float]:
    """(start, duration) of the stretch with the most vocal energy."""
    import librosa
    import numpy as np
    y, sr = librosa.load(str(vocals), sr=4000, mono=True)
    total = len(y) / sr
    if total <= seconds:
        return 0.0, total
    block = np.add.reduceat(y ** 2, np.arange(0, len(y), sr // 4))  # energy per 0.25 s
    w = int(seconds * 4)
    start = int(np.argmax(np.convolve(block, np.ones(w), mode="valid"))) / 4
    return start, seconds


def song_key(song: Path) -> dict:
    """Detected musical key of a song (from the full mix), cached per file hash."""
    from ..core.separate import file_hash
    from ..core.engine.rvc.f0fix import detect_key, key_name
    cache = path("cache_dir") / "keys" / f"{file_hash(Path(song))}.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    import librosa
    y, sr = librosa.load(str(song), sr=22050, mono=True, duration=240)
    tonic, mode, conf = detect_key(y, sr)
    k = {"tonic": tonic, "mode": mode, "name": key_name(tonic, mode), "confidence": conf}
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(k), encoding="utf-8")
    return k


def upload_title(song: Path) -> str | None:
    """Songs added through the web UI are stored by hash; their real name is in the sidecar JSON."""
    meta = Path(song).with_suffix(".json")
    if re.fullmatch(r"[0-9a-f]{16}", Path(song).stem) and meta.exists():
        try:
            return json.loads(meta.read_text(encoding="utf-8")).get("title")
        except ValueError:
            return None
    return None


def _conv_cfg() -> dict:
    from ..core.config import CONFIG
    return CONFIG["conversion"]


def pitch_plan(opts: CoverOptions) -> tuple[float, float]:
    """(whole-song key shift, vocal shift) in semitones. Vocal = key + 12 * octave keeps it in key.
    A legacy vocal-only `pitch` is used as-is only when key/octave aren't given."""
    from ..core.config import CONFIG
    c = CONFIG["conversion"]
    if opts.key is None and opts.octave is None and opts.pitch is not None:
        return 0.0, float(opts.pitch)
    key = float(c["key"] if opts.key is None else opts.key)
    octave = int(c["octave"] if opts.octave is None else opts.octave)
    return key, key + 12 * octave


def _unique(base: Path, fmt: str) -> Path:
    """'Song (voice)' or, if that file exists, 'Song (voice) 2', 'Song (voice) 3', ..."""
    cand, n = base, 2
    while cand.with_name(f"{cand.name}.{fmt}").exists():   # titles can contain dots, so never with_suffix
        cand = base.with_name(f"{base.name} {n}")
        n += 1
    return cand.with_name(f"{cand.name}.{fmt}")


def _append_history(r: CoverResult) -> dict:
    rec = asdict(r)
    rec.pop("id", None)
    rec.update(output=str(r.output), song=str(r.song), converted_vocal=str(r.converted_vocal),
               converted_backing=str(r.converted_backing) if r.converted_backing else None)
    return hstore.record(rec)   # adds the permanent id, then rolls the recent tray (frontman/history.py)


def history() -> list[dict]:
    """Every finished cover whose file is there, newest first, each with its permanent `id`, `kept` / `kept_at` and
    `recent` (it is in the recent tray: the latest 15)."""
    items = hstore.finished()
    recent = {i["id"] for i in tray.in_tray(items)}
    for i in items:
        if not i.get("title") or re.fullmatch(r"[0-9a-f]{16}", i["title"]):
            i["title"] = upload_title(Path(i["song"])) or Path(i["song"]).stem
        i["kept"] = bool(i.get("kept"))
        i["recent"] = i["id"] in recent
    return list(reversed(items))


def history_items() -> list[dict]:
    """The covers history as the page shows it (file URLs instead of paths): the recent tray, newest first (the latest
    15 finished covers, kept or not)."""
    from ..core.files import file_url
    out = []
    for h in history():
        if not h["recent"]:
            continue
        out.append({
            "id": h["id"], "title": h.get("title") or Path(h["song"]).stem, "voice": h["voice"],
            "kept": h["kept"], "kept_at": h.get("kept_at"), "recent": h["recent"],
            "created": h.get("created", ""), "seconds": h.get("seconds", {}), "options": h.get("options", {}),
            "output": file_url(h["output"]), "output_name": Path(h["output"]).name,
            "original": file_url(h["song"]), "vocal": file_url(h.get("converted_vocal")),
            "backing": file_url(h.get("converted_backing")),
            "backing_voice": (h.get("options", {}).get("backing_voice")
                              if h.get("converted_backing") and h.get("options", {}).get("backing_voice") != h["voice"] else None),
            "notes": h.get("notes", []),
        })
    return out


def cancel_presep(keep=None):
    """Cancel background separations, except one producing exactly the stems `keep` = (song id, quality, dereverb,
    karaoke) needs (that one is about to be used)."""
    from ..core.jobs import JOBS
    for j in JOBS.values():
        if j.kind == "presep" and not j.finished and getattr(j, "presep", None) != keep:
            j.cancel = True

def estimate(song: Path, opts: CoverOptions) -> dict:
    """Expected seconds per stage for this song, from measured speeds in logs/timings.jsonl
    (falls back to Phase 5 measurements). Separation is 0 when stems are cached."""
    import statistics
    from ..core.config import CONFIG
    from ..core.separate import QUALITY, SEP, audio_seconds, file_hash
    song = Path(song)
    dur = audio_seconds(song)
    quality = opts.quality or SEP["quality"]
    ov = QUALITY[quality]
    speeds = {"sep": [], "synth": [], "f0": []}
    log = path("logs_dir") / "timings.jsonl"
    if log.exists():
        for line in log.read_text(encoding="utf-8").splitlines()[-500:]:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("stage") == "separate.run" and r.get("overlap") == ov and r.get("model") == SEP["vocal_model"] and r.get("realtime"):
                speeds["sep"].append(r["realtime"])
            elif r.get("stage") == "separate.run" and r.get("model") in (SEP["karaoke_model"], SEP["dereverb_model"]) and r.get("realtime"):
                speeds.setdefault("extra", []).append(r["realtime"])
            elif r.get("stage") == "convert.synth" and r.get("realtime") and r.get("audio_seconds", 0) >= 60:  # not short check clips
                speeds["synth"].append(r["realtime"])
            elif r.get("stage") == "convert.f0" and r.get("seconds_audio"):
                speeds["f0"].append(r["seconds_audio"] / max(r["seconds"], 1e-3))
    med = lambda xs, d: statistics.median(xs[-10:]) if xs else d
    sep_rt = med(speeds["sep"], {2: 10.6, 4: 5.1, 8: 2.7}[ov])
    stem_dir = path("cache_dir") / "stems" / file_hash(song) / (Path(SEP["vocal_model"]).stem + ("" if quality == "high" else f"_{quality}"))
    from ..core.separate import stem_files
    cached = stem_dir.exists() and bool(stem_files(stem_dir))
    sep = 0.0 if cached else dur / sep_rt
    # extra passes run on the vocal stem; each is skipped when its own output is already cached
    karaoke = bool(opts.backing_voice or (opts.karaoke if opts.karaoke is not None else SEP["karaoke"]))
    dereverb = bool(opts.dereverb if opts.dereverb is not None else SEP["dereverb"])
    k_dir = stem_dir / ("karaoke_" + Path(SEP["karaoke_model"]).stem)
    d_dir = (k_dir if karaoke else stem_dir) / ("dereverb_" + Path(SEP["dereverb_model"]).stem)
    done = lambda d: d.exists() and bool(stem_files(d))
    passes = (karaoke and not done(k_dir)) + (dereverb and not done(d_dir))
    sep += passes * dur / med(speeds.get("extra", []), 13.6)  # measured ~13.6x realtime (2026-10-09)
    conv = dur / med(speeds["synth"], 40.0) + dur / med(speeds["f0"], 90.0) + 2.0
    if opts.backing_voice:
        conv = conv * 2 - 1.5  # second conversion; shared models are already loaded
    key, _ = pitch_plan(opts)
    mix = 1.0 + dur / 120.0 + (dur / 50.0 if key else 0.0)  # Rubber Band key shift ~50x realtime
    return {"separating": round(sep, 1), "converting": round(conv, 1), "mixing": round(mix, 1),
            "total": round(sep + conv + mix, 1), "stems_cached": cached, "song_seconds": round(dur, 1)}


ENGINE = CoverEngine()   # the one the server uses (state lives in core.gpu)
