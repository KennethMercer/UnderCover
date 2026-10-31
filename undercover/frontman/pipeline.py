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
from ..core.voices import label_of
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
    duet_voice: str | None = None     # a second singer taking turns with `voice` (1.2, duets.py)
    duet_way: str | None = None       # how they share it: duets.WAYS (None: duets.DUET_DEFAULT)
    duet_octave: int | None = None    # the second singer's octave on top of the song key (None: the lead's)
    duet_plan: list | None = None     # who sings when, [[start, end, who], ...] (found by the engine; kept for Make again)
    duet_backing: str | None = None   # a duet's backing: duets.BACKINGS (None: duets.BACKING_DEFAULT, the singers' voices)


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
            notes.append(f"Fit to voice: {label_of(voice.name)} has no measured range yet (voices trained in UnderCover have one, "
                         "and voices a one-shot cover checked against a person), so your octave and key were used.")
            return opts
        f0 = self.converter.rmvpe.infer_from_audio(load_16k(stems["vocals"]), thred=0.03)
        speech = ex.get("from_speech") if ex.get("f0_range") else ex.get("person_range_speech")
        fit = pick_fit(rng, bool(speech), f0)
        notes.append(f"Fit to voice: octave {fit['octave']:+d}, key {fit['key']:+d} "
                     f"({fit['outside']:.0%} of the melody outside {label_of(voice.name)}'s range {rng[0]:.0f}-{rng[2]:.0f} Hz).")
        same_backing = bvoice is not None and bvoice.name == voice.name
        return replace(opts, octave=fit["octave"], key=fit["key"], pitch=None,
                       backing_octave=fit["octave"] if same_backing else opts.backing_octave)

    def _fit_duet(self, opts: "CoverOptions", voice, dvoice, lead_f0, notes: list) -> "CoverOptions":
        """The fit for two (duets.joint_fit): one key for the song, each voice at its own best octave in it, each one
        weighed by how much of the song the plan gives it (so it runs after _duet_plan). A voice with no measured
        range keeps the chosen octave. When one of them sings FAR_ST or more from where they'd sing alone, the note
        says so. lead_f0: the lead's pitch track, read once (make_cover)."""
        from dataclasses import replace
        from .duets import FAR_ST, joint_fit, singer_frames
        f0 = lead_f0()
        fit = joint_fit({"A": _range_of(voice), "B": _range_of(dvoice)}, f0, singer_frames(opts.duet_plan, len(f0)))
        if fit is None:
            notes.append(f"Fit to voice: neither {label_of(voice.name)} nor {label_of(dvoice.name)} has a measured range yet, "
                         "so your octave and key were used.")
            return opts
        a = fit["octave"].get("A", opts.octave if opts.octave is not None else 0)
        b = fit["octave"].get("B", opts.duet_octave if opts.duet_octave is not None else a)
        names = {"A": label_of(voice.name), "B": label_of(dvoice.name)}
        line = f"Fit for two: key {fit['key']:+d}, {names['A']} at octave {a:+d}, {names['B']} at octave {b:+d}."
        far = max(fit["off"], key=lambda k: abs(fit["off"][k])) if len(fit["off"]) == 2 else None   # (both must sing)
        if far and abs(fit["off"][far]) >= FAR_ST:   # one sits far from their own key: said plainly
            other = "B" if far == "A" else "A"
            line += (f" {names[far]} sings {abs(fit['off'][far])} semitones {'higher' if fit['off'][far] > 0 else 'lower'}"
                     f" than their own best key" + (f": the key follows {names[other]}, who sings {fit['share'][other]:.0%} "
                                                    "of the song." if fit["share"][other] > 0.5 else ", to share one with "
                                                    f"{names[other]}."))
        notes.append(line)
        return replace(opts, key=fit["key"], octave=a, duet_octave=b, pitch=None)

    def _duet_backing(self, opts: "CoverOptions", cstems: dict, notes: list) -> bool:
        """Is a duet's backing converted (each phrase in the singer of its section, duets.backing_plan)? Unless the
        cover asks for the original (duet_backing, duets.BACKING_DEFAULT: the singers' voices); the one-shot asks for
        it when the backing is stacked harmony (oneshot.chordal_backing). Nothing when there's hardly any backing."""
        from .duets import BACKING_DEFAULT
        from .remix import lufs
        back = cstems.get("backing")
        if (opts.duet_backing or BACKING_DEFAULT) == "original" or not back:
            return False
        if lufs(back) < -45:
            notes.append("This song has almost no backing vocals, so they weren't converted.")
            return False
        return True

    def _duet_plan(self, opts: "CoverOptions", voice, dvoice, cstems: dict, work: Path, notes: list, report,
                   lead_f0=None) -> "CoverOptions":
        """Who sings when (duets.py): the cover's own plan from Make again, checked; else found from the song's words
        (Whisper on the whole vocal) and the lead's quiet; with too few words heard, by its phrases. Turns puts one
        singer on the verses and the other on the choruses; when both have a measured range, the range says which
        (duets.turns_by_register, on the lead's pitch track `lead_f0`: at the two voices' key on the whole song, or at
        the cover's own key and octaves with the fit off). The plan and the way go into the options (the history keeps
        them), and the notes say who sings what."""
        from dataclasses import replace
        from .duets import DUET_DEFAULT, check_plan, describe, find_plan, heard_words, turns_by_register
        from ..core.engine.convert import load_16k
        lead16 = load_16k(cstems["vocals"])
        dur = len(lead16) / 16000
        way = opts.duet_way or DUET_DEFAULT
        said = None
        if opts.duet_plan:
            plan = check_plan(opts.duet_plan, dur)
        else:
            report("converting", 0.0, "Listening for the verses and choruses")
            try:
                words = heard_words(cstems["vocals"], cstems.get("backing"), work)
            except Exception as e:   # noqa: BLE001 - no recogniser here (or it failed): shared by phrases instead
                print(f"Duet: the words couldn't be heard ({type(e).__name__}: {e}); sharing it by phrases.", flush=True)
                words = []
            plan, rows, fallback = find_plan(lead16, words, way, dur)
            if fallback:
                notes.append("UnderCover couldn't make out enough of the words, so the two voices take turns every "
                             "two phrases.")
            if way == "turns" and not fallback:
                ranges = {"A": _range_of(voice), "B": _range_of(dvoice)}
                f0 = lead_f0() if lead_f0 is not None and ranges["A"] and ranges["B"] else None
                plan, said = turns_by_register(rows, f0, ranges, dur, None if opts.fit else _duet_pitches(opts))
        notes.append(describe(plan, {"A": label_of(voice.name), "B": label_of(dvoice.name)}, said))
        return replace(opts, duet_way=way, duet_plan=plan)

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
        dvoice = get_voice(opts.duet_voice) if opts.duet_voice else None   # a duet's second singer (duets.py)
        if dvoice:   # a duet's backing is its own choice (duet_backing: the singers' voices or the original), never a third voice
            bvoice = None
        karaoke = True if bvoice or dvoice else opts.karaoke  # a backing voice, and a duet's one lead line, need the split
        secs, notes = {}, []

        report("separating", 0.0, "Separating vocals from the instrumental")
        t = time.perf_counter()
        stems = stems or separate(song, opts.dereverb, karaoke, separator=self.separator, quality=opts.quality, one_voice_if_no_lead=True,
                         progress=lambda f, step: report("separating", f, {"main": "Separating vocals", "karaoke": "Splitting lead and backing vocals", "dereverb": "Removing reverb"}[step]))
        secs["separate"] = round(time.perf_counter() - t, 2)
        report("separating", 1.0, "Stems ready")
        if karaoke and not stems.get("backing"):   # core.separate found no lead to split from the backing (a duet)
            # (a cover sings the whole vocal as one voice; a duet's two take turns on it, as one sung with the split
            # off did before duets3: the second singer asked for is never dropped)
            notes.append(DUET_NOTE if not dvoice else
                         f"Two singers in harmony: UnderCover can't separate them yet, so {label_of(voice.name)} and "
                         f"{label_of(dvoice.name)} take turns on the whole vocal, its backing with it.")
        if opts.fit and not dvoice:   # (a duet's fit waits for its plan: each singer weighed by their share)
            opts = self._fit(opts, voice, stems, notes, bvoice)

        report("converting", 0.0, f"Converting the vocal to {label_of(voice.name)}")
        t = time.perf_counter()
        s = Settings()
        for k in ("index_ratio", "protect", "volume_envelope", "pitch_cleanup", "pitch_correction"):
            if getattr(opts, k) is not None:
                setattr(s, k, getattr(opts, k))
        if s.pitch_correction > 0:
            k = song_key(key_song or song)
            s.song_key = (k["tonic"], k["mode"])
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
        work = path("cache_dir") / "converted"
        work.mkdir(parents=True, exist_ok=True)
        if dvoice:   # who sings when: the plan from Make again (checked), else found in the song itself; then the fit
            import functools
            lead_f0 = functools.cache(lambda: self.converter.rmvpe.infer_from_audio(load_16k(stems["vocals"]), thred=0.03))
            opts = self._duet_plan(opts, voice, dvoice, cstems, work, notes, report, lead_f0)
            if opts.fit:
                opts = self._fit_duet(opts, voice, dvoice, lead_f0, notes)
        key, s.pitch = pitch_plan(opts)
        from .remix import lufs, prefetch
        # mix prep (stem loudness, key-shifted instrumental) runs on the CPU while the GPU converts
        prefetch([cstems["vocals"], cstems.get("backing"), cstems.get("reverb"), song], key=key,
                 instrumental=cstems["instrumental"], backing=None if bvoice else cstems.get("backing"))
        if bvoice and not cstems.get("backing"):   # sung as one voice (a duet): no backing stem to convert
            bvoice = None
        elif bvoice and lufs(cstems["backing"]) < -45:  # nothing worth converting
            notes.append("This song has almost no backing vocals, so the backing voice wasn't used.")
            bvoice = None
        dback = bool(dvoice) and self._duet_backing(opts, cstems, notes)   # the backing in each section's singer too
        if dback:
            bvoice = None   # (one backing voice gives way to the duet's two)
        passes = 1 + bool(dvoice) + bool(bvoice) + 2 * dback
        share = 1.0 / passes
        from ..core import lowprofile
        lowprofile.free_for("convert")   # (under the cap: the separator goes before the conversion; back on first use)
        import contextlib
        lead16 = load_16k(cstems["vocals"])
        with self.converter.shared_f0() if dvoice else contextlib.nullcontext():   # (a duet: the pitch track read once)
            audio, sr = self.converter.convert(lead16, voice.name, s,
                                               progress=lambda f: report("converting", f * share, f"Converting the lead vocal to {label_of(voice.name)}"))
            if dvoice:
                from dataclasses import replace as _replace
                from .duets import stitch
                doct = opts.duet_octave if opts.duet_octave is not None else (s.pitch - key) / 12
                daudio, dsr = self.converter.convert(lead16, dvoice.name, _replace(s, pitch=key + 12 * doct),
                                                     progress=lambda f: report("converting", share * (1 + f), f"Converting the lead vocal to {label_of(dvoice.name)} too"))
                if dsr != sr:   # two voices trained at different rates: the second one's take at the first one's
                    from math import gcd
                    from scipy.signal import resample_poly
                    g = gcd(sr, dsr)
                    daudio = resample_poly(daudio, sr // g, dsr // g).astype(np.float32)
                audio = stitch(audio, daudio, opts.duet_plan, sr)
        # 24-bit FLAC: ~half the size of float WAV, far below audible precision (kept for "vocal only" downloads)
        conv_path = work / f"{_safe(title)}__{voice.name}__{stamp}.flac"
        sf.write(str(conv_path), np.clip(audio, -1, 1), sr, subtype="PCM_24")
        conv_backing = None
        if bvoice:
            from dataclasses import replace
            boct = int(_conv_cfg()["octave"] if opts.backing_octave is None else opts.backing_octave)
            bs = replace(s, pitch=key + 12 * boct)
            baudio, bsr = self.converter.convert(load_16k(cstems["backing"]), bvoice.name, bs,
                                                 progress=lambda f: report("converting", 1 - share + share * f, f"Converting the backing vocals to {label_of(bvoice.name)}"))
            conv_backing = work / f"{_safe(title)}__{bvoice.name}__backing__{stamp}.flac"
            sf.write(str(conv_backing), np.clip(baudio, -1, 1), bsr, subtype="PCM_24")
        if dback:   # the backing as the lead: each section's backing in that section's singer, at that singer's octave
            from dataclasses import replace as _replace
            from .duets import backing_plan, stitch
            back16 = load_16k(cstems["backing"])
            aoct = (s.pitch - key) / 12
            doct = opts.duet_octave if opts.duet_octave is not None else aoct
            with self.converter.shared_f0():
                ba, bsr = self.converter.convert(back16, voice.name, s, progress=lambda f: report(
                    "converting", 1 - 2 * share + share * f, f"Converting the backing vocals to {label_of(voice.name)}"))
                bb, dbsr = self.converter.convert(back16, dvoice.name, _replace(s, pitch=key + 12 * doct), progress=lambda f: report(
                    "converting", 1 - share + share * f, f"Converting the backing vocals to {label_of(dvoice.name)} too"))
            if dbsr != bsr:
                from math import gcd
                from scipy.signal import resample_poly
                g = gcd(bsr, dbsr)
                bb = resample_poly(bb, bsr // g, dbsr // g).astype(np.float32)
            conv_backing = work / f"{_safe(title)}__{voice.name}__{dvoice.name}__backing__{stamp}.flac"
            bplan = backing_plan(opts.duet_plan, back16)   # (each backing phrase whole, by whoever sings most of it)
            sf.write(str(conv_backing), np.clip(stitch(ba, bb, bplan, bsr), -1, 1), bsr, subtype="PCM_24")
        secs["convert"] = round(time.perf_counter() - t, 2)
        report("converting", 1.0, "Vocal converted")

        report("mixing", 0.0, "Mixing and matching loudness")
        t = time.perf_counter()
        ms = MixSettings(key=key)
        for k in ("reverb", "vocal_db", "backing_db", "format", "polish"):
            if getattr(opts, k) is not None:
                setattr(ms, k, getattr(opts, k))
        # the file people download says who sings, as the pages name them (the voice ids stay in the history)
        names = (label_of(voice.name) + (f" & {label_of(dvoice.name)}" if dvoice else "")
                 + (f" + {label_of(bvoice.name)}" if bvoice and bvoice.name not in (voice.name, getattr(dvoice, "name", None)) else ""))
        out = out or _unique(path("output_dir") / f"{_safe(title)} ({_safe(names)})", ms.format)   # "AC/DC" is a name, not a path
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
        karaoke = True if opts.backing_voice or opts.duet_voice else opts.karaoke   # (as the cover would split it)
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
        base, fit_note = preview_fit(self, opts, stems)
        out = []
        for i, v in enumerate(variants):
            report("rendering", i / len(variants), f"Rendering {v.get('label', i + 1)}")
            overrides = variant_overrides(opts, base, v)
            o = replace(base, **overrides, format="mp3", fit=False)   # the fit is settled once, above
            r = self.make_cover(song_cut, o, out=work / f"variant_{i}", stems=sliced, record=False,
                                key_song=song, title="preview")
            out.append({"label": v.get("label", str(i + 1)), "file": r.output,
                        "options": {**overrides, "octave": o.octave, "key": o.key}, "fit": fit_note})
        report("rendering", 1.0, "Previews ready")
        return out


def preview_fit(engine, opts: "CoverOptions", stems: dict) -> tuple["CoverOptions", str | None]:
    """With "Fit pitch to the voice" on, the fit is worked out ONCE, on the full stems (the same melody the full cover
    fits), not inside each 15 s variant: there it overwrote each variant's octave and key, so every octave or key
    preview came out the same (Internal Affairs, Compare). -> (the options with the fitted octave / key, its note)."""
    if not opts.fit:
        return opts, None
    from ..core.voices import get_voice
    notes: list = []
    voice = get_voice(opts.voice)
    bvoice = get_voice(opts.backing_voice) if opts.backing_voice else None
    fitted = engine._fit(opts, voice, stems, notes, bvoice)
    return fitted, (notes[0] if notes else None)


def variant_overrides(opts: "CoverOptions", base: "CoverOptions", v: dict) -> dict:
    """A variant's option overrides. With the fit on, an octave or key is read relative to the page's own setting and
    applied to the fitted one ("Same octave" = the fit's pick, +1 / -1 around it), clamped to the page's ranges."""
    ov = {k: v[k] for k in v if k not in ("label", "fit") and hasattr(opts, k)}
    if opts.fit:
        if "octave" in ov:
            ov["octave"] = max(-2, min(2, int(base.octave or 0) + int(ov["octave"]) - int(opts.octave or 0)))
        if "key" in ov:
            ov["key"] = max(-6, min(6, float(base.key or 0) + float(ov["key"]) - float(opts.key or 0)))
    return ov


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


def _range_of(voice) -> tuple | None:
    """A voice's measured range for the fit: (f0_range, trained on speech?), trained here or the checked person's;
    None when it has none."""
    ex = voice.extra or {}
    r = ex.get("f0_range") or ex.get("person_range")
    return (r, ex.get("from_speech") if ex.get("f0_range") else ex.get("person_range_speech")) if r else None


def _duet_pitches(opts: CoverOptions) -> dict:
    """A duet's key and each singer's octave as the cover will sing them with the fit off (pitch_plan; the second
    singer at the lead's octave unless given): {key, octave: {A, B}} for duets.turns_by_register."""
    key, vocal = pitch_plan(opts)
    a = int(round((vocal - key) / 12))
    return {"key": int(round(key)), "octave": {"A": a, "B": a if opts.duet_octave is None else int(opts.duet_octave)}}


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
        o = h.get("options") if isinstance(h.get("options"), dict) else {}   # (the history's own file, read as data)
        out.append({
            "id": h["id"], "title": h.get("title") or Path(h["song"]).stem, "voice": h["voice"],
            "kept": h["kept"], "kept_at": h.get("kept_at"), "recent": h["recent"],
            "created": h.get("created", ""), "seconds": h.get("seconds", {}), "options": o,
            "output": file_url(h["output"]), "output_name": Path(h["output"]).name,
            "original": file_url(h["song"]), "vocal": file_url(h.get("converted_vocal")),
            "backing": file_url(h.get("converted_backing")),
            "backing_voice": (o.get("backing_voice") if h.get("converted_backing") and o.get("backing_voice") != h["voice"]
                              and not o.get("duet_voice") else None),   # (a duet's backing is its singers')
            "duet_voice": o.get("duet_voice") or None,   # a duet's second singer (duets.py)
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

def blend_left(planned: float, elapsed: float, frac: float) -> float:
    """Seconds left in a step from its estimate (planned) and its own progress: the estimate at first, the measured
    rate taking over as the step gets going. Internal Affairs P3-4: a convert's slow first second extrapolated alone
    read 20 s -> 184 s -> 18 s left."""
    plan_left = max(planned * (1 - frac), planned - elapsed, 0.0)
    if frac <= 0.05 or elapsed <= 0:
        return plan_left
    w = min(1.0, (frac - 0.05) / 0.25)
    return w * (elapsed / frac * (1 - frac)) + (1 - w) * plan_left


class Countdown:
    """A job's time left from its estimate per stage ({stage: seconds}, stages in order): this stage's blend_left
    plus the later stages' estimates. left(stage, frac) -> (stage_left, total_left), or None for a stage it doesn't
    know."""

    def __init__(self, est: dict, order: tuple = ("separating", "converting", "mixing")):
        self.est, self.order, self.stage, self.t = est, order, None, time.perf_counter()

    def left(self, stage: str, frac: float, now: float | None = None):
        now = time.perf_counter() if now is None else now
        if stage != self.stage:
            self.stage, self.t = stage, now
        if stage not in self.order:
            return None
        here = blend_left(float(self.est.get(stage) or 0), now - self.t, frac)
        later = sum(float(self.est.get(s) or 0) for s in self.order[self.order.index(stage) + 1:])
        return round(here, 1), round(here + later, 1)


def preview_estimate(song: Path, opts: CoverOptions, variants: int, seconds: float = 15.0) -> dict:
    """Expected seconds for a preview: the song's separation (0 when cached) and the versions' rendering (a short
    excerpt each, plus cutting and the fit)."""
    full = estimate(song, opts)
    clip = estimate_for(seconds, opts)
    render = variants * (clip["converting"] + clip["mixing"]) + 2.0 + (3.0 if opts.fit else 0.0)
    return {"separating": full["separating"], "rendering": round(render, 1), "total": round(full["separating"] + render, 1)}


def estimate(song: Path, opts: CoverOptions) -> dict:
    """Expected seconds per stage for this song, from measured speeds in logs/timings.jsonl
    (falls back to Phase 5 measurements). Separation is 0 when stems are cached."""
    from ..core.separate import audio_seconds
    return estimate_for(audio_seconds(Path(song)), opts, song=Path(song))


def estimate_for(dur: float, opts: CoverOptions, song: Path | None = None) -> dict:
    """estimate() for `dur` seconds of song; song=None: nothing is cached yet (the one-shot plan, before the song)."""
    import statistics
    from ..core.config import CONFIG
    from ..core.separate import QUALITY, SEP, file_hash
    quality = opts.quality or SEP["quality"]
    ov = QUALITY[quality]
    from ..core import lowprofile
    low = lowprofile.on()
    speeds = {"sep": [], "synth": [], "f0": []}
    log = path("logs_dir") / "timings.jsonl"
    if log.exists():
        for line in log.read_text(encoding="utf-8").splitlines()[-500:]:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if bool(r.get("low_profile")) != low:   # (this mode's own measurements only)
                continue
            if r.get("stage") == "separate.run" and r.get("overlap") == ov and r.get("model") == SEP["vocal_model"] and r.get("realtime"):
                speeds["sep"].append(r["realtime"])
            elif r.get("stage") == "separate.run" and r.get("model") in (SEP["karaoke_model"], SEP["dereverb_model"]) and r.get("realtime"):
                speeds.setdefault("extra", []).append(r["realtime"])
            elif r.get("stage") == "convert.synth" and r.get("realtime") and r.get("audio_seconds", 0) >= 60:  # not short check clips
                speeds["synth"].append(r["realtime"])
            elif r.get("stage") == "convert.f0" and r.get("seconds_audio"):
                speeds["f0"].append(r["seconds_audio"] / max(r["seconds"], 1e-3))
    slow = lowprofile.factor()   # (no measurement under Low profile yet: the burst's share of the time, as a first guess)
    med = lambda xs, d: statistics.median(xs[-10:]) if xs else d / slow
    sep_rt = med(speeds["sep"], {2: 10.6, 4: 5.1, 8: 2.7}[ov])
    stem_dir = (path("cache_dir") / "stems" / (file_hash(song) if song is not None else "none-yet")
                / (Path(SEP["vocal_model"]).stem + ("" if quality == "high" else f"_{quality}")))
    from ..core.separate import stem_files
    cached = stem_dir.exists() and bool(stem_files(stem_dir))
    sep = 0.0 if cached else dur / sep_rt
    # extra passes run on the vocal stem; each is skipped when its own output is already cached
    karaoke = bool(opts.backing_voice or opts.duet_voice or (opts.karaoke if opts.karaoke is not None else SEP["karaoke"]))
    dereverb = bool(opts.dereverb if opts.dereverb is not None else SEP["dereverb"])
    k_dir = stem_dir / ("karaoke_" + Path(SEP["karaoke_model"]).stem)
    d_dir = (k_dir if karaoke else stem_dir) / ("dereverb_" + Path(SEP["dereverb_model"]).stem)
    done = lambda d: d.exists() and bool(stem_files(d))
    passes = (karaoke and not done(k_dir)) + (dereverb and not done(d_dir))
    sep += passes * dur / med(speeds.get("extra", []), 13.6)  # measured ~13.6x realtime (2026-10-09)
    conv = dur / med(speeds["synth"], 40.0) + dur / med(speeds["f0"], 90.0) + 2.0
    if opts.backing_voice and not opts.duet_voice:
        conv = conv * 2 - 1.5  # second conversion; shared models are already loaded
    if opts.duet_voice:   # a duet: the lead converted again (its pitch track shared), and its words heard once
        from .duets import BACKING_DEFAULT
        conv += dur / med(speeds["synth"], 40.0) + (0.0 if opts.duet_plan else 5.0 + dur / 30.0)
        if (opts.duet_backing or BACKING_DEFAULT) != "original":   # and the backing in both singers
            conv += 2 * dur / med(speeds["synth"], 40.0)
    key, _ = pitch_plan(opts)
    mix = 1.0 + dur / 120.0 + (dur / 50.0 if key else 0.0)  # Rubber Band key shift ~50x realtime
    return {"separating": round(sep, 1), "converting": round(conv, 1), "mixing": round(mix, 1),
            "total": round(sep + conv + mix, 1), "stems_cached": cached, "song_seconds": round(dur, 1)}


ENGINE = CoverEngine()   # the one the server uses (state lives in core.gpu)
