"""The golden set (docs/research/golden-set.md): fixed songs and voices, scored after every voice-affecting phase, so a
quality regression is caught before the user hears it. Numbers flag; the ear decides (only verdicts move the baseline).

    set UNDERCOVER_DATA_ROOT=D:\\UnderCover            (the app folder: its models, voices and projects are read, never written)
    python tools/golden.py make [--song NAME=FILE@START+SECONDS [--with VOICE,VOICE]] [--voice NAME ...]
                                   freeze what isn't in the set yet (a section of each song; the voices), then run 3 times:
                                   the new items' baseline and noise bands (items already in the set keep theirs).
                                   The first make freezes "Hold On" and the default voices; the set grows the same way.
    python tools/golden.py run [--force]                 the set again -> runs/<stamp>/scores.json + renders
    python tools/golden.py compare [RUN]                 what moved past twice its noise band, the bad way
    python tools/golden.py pairs [RUN]                   blind pairs (every flag + one rotating item, at most 4) + key.json
    python tools/golden.py verdict RUN ITEM new|old|same the ear's answer
    python tools/golden.py accept RUN                    the items heard as new / same take the run's scores into the baseline
    python tools/golden.py rebands                       run the set twice more and widen every band to the largest spread seen
    python tools/golden.py refreeze VOICE                after the user approved a newer version of a voice: a fresh copy, and
                                                         that voice's items start a new baseline on the next run (MasterPlan, q2)

The golden folder: UNDERCOVER_GOLDEN, or <app folder>/golden. It has its own config (models read from the app folder,
everything written inside golden/), never the user's library folders. Not part of smoke (it needs the GPU and real songs);
smoke's u_golden checks the bookkeeping offline.
"""
import argparse
import hashlib
import json
import os
import random
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
APP = Path(os.environ.get("UNDERCOVER_DATA_ROOT") or ROOT)
GOLDEN = Path(os.environ.get("UNDERCOVER_GOLDEN") or APP / "golden")

# The proposal's set (MasterPlan, 2026-10-13: "Hold On" and the Toxic section first; more songs by the same make step)
DEFAULT_SONGS = [("hold-on", str(ROOT / "undercover" / "assets" / "sample" / "hold-on.mp3"), 0.0, 45.0)]
DEFAULT_VOICES = ["barack-obama-uc", "morgan-freeman-uc", "elliot-smith-uc", "spongebob-uc", "agent-smoke"]
DEFAULT_COVERS = {"hold-on": DEFAULT_VOICES, "toxic": ["spongebob-uc", "elliot-smith-uc", "morgan-freeman-uc"]}
LINES = [("rain", "The rain came in sideways that night, and nobody on the street was talking."),
         ("welcome", "Ladies and gentlemen, thank you for coming. We have a lot to cover, so let's begin."),
         ("again", "Wait, what did you just say? Say that again, slowly.")]
SPEECH_VOICES = [("barack-obama-uc", True), ("morgan-freeman-uc", True), ("morgan-freeman-uc", False)]

# Each score: which way is worse ("up": higher is better, "down": lower is better, "abs": any move matters) and the
# smallest band (a run twice in a row can agree to the digit; these are the differences an ear could plausibly hear)
SCORES = {
    "likeness": ("up", 0.02), "words_kept": ("up", 0.05), "cents_off": ("down", 5.0), "outside": ("down", 0.03),
    "hiss": ("down", 0.01), "bleed_db": ("down", 1.5), "loud_db": ("abs", 0.5), "peak_db": ("abs", 0.5),
    "clipped": ("down", 0.0005), "gate_s": ("abs", 1.0),
    "wer": ("down", 0.05), "pace": ("abs", 0.05), "f0_st": ("abs", 0.3),
}
MAX_PAIRS = 4
MAKE_RUNS = 3   # runs at make: the baseline + its noise bands (the largest spread of the three)
SEP = "__"   # between an item's song (or line) and its voice
KEEP_RUNS = 2


# ---------------------------------------------------------------- bookkeeping (no GPU; u_golden checks these)

def _read(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write(p: Path, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def bands(*runs: dict) -> dict:
    """Runs of the same set -> {item: {score: band}}: the largest spread between them, never under the score's floor.
    Three runs at make (phase 22): two that happened to agree once left Obama's likeness band at its floor, and the
    next run's ordinary 0.06 wobble was flagged."""
    out = {}
    for item in set.intersection(*(set(r) for r in runs)):
        out[item] = {}
        for k in runs[0][item]:
            vals = [r[item].get(k) for r in runs]
            if k in SCORES and all(isinstance(v, (int, float)) for v in vals):
                out[item][k] = round(max(max(vals) - min(vals), SCORES[k][1]), 4)
    return out


def flags(base: dict, run: dict) -> list:
    """What moved past twice its band the bad way: [{item, score, was, now}], the worst first (in bands)."""
    out = []
    for item, sc in run.items():
        b = (base.get("scores") or {}).get(item)
        bd = (base.get("bands") or {}).get(item, {})
        if not b:
            continue   # new to the set: no baseline yet
        for k, now in sc.items():
            was = b.get(k)
            if k not in SCORES or not isinstance(now, (int, float)) or not isinstance(was, (int, float)):
                continue
            way, floor = SCORES[k]
            band = bd.get(k, floor)
            d = now - was
            bad = -d if way == "up" else d if way == "down" else abs(d)
            if bad > 2 * band:
                out.append({"item": item, "score": k, "was": was, "now": now, "bands": round(bad / band, 1)})
    return sorted(out, key=lambda f: -f["bands"])


def pick_pairs(flagged: list, items: list, runs_so_far: int) -> list:
    """The items to hear: every flagged one (the worst first), plus one rotating unflagged item, at most MAX_PAIRS."""
    out = list(dict.fromkeys(f["item"] for f in flagged))[:MAX_PAIRS]
    rest = sorted(i for i in items if i not in out)
    if rest and len(out) < MAX_PAIRS:
        out.append(rest[runs_so_far % len(rest)])
    return out


def accepted(base: dict, run: str, run_scores: dict, verdicts: dict) -> dict:
    """The new baseline: items heard as "new" or "same" take the run's scores (and its audio for the next pairs); "old"
    and unheard ones keep theirs. Items new to the set (no baseline yet) come in as they are."""
    scores, renders = dict(base.get("scores") or {}), dict(base.get("renders") or {})
    for item, sc in run_scores.items():
        if verdicts.get(item) in ("new", "same") or item not in scores:
            scores[item], renders[item] = sc, run
    return {**base, "scores": scores, "renders": renders}


def render_run(base: dict, item: str) -> str | None:
    """The run whose audio is the baseline's for this item."""
    return (base.get("renders") or {}).get(item) or base.get("run")


def parts(item: str) -> set:
    """"cover:hold-on__spongebob-uc" -> {"hold-on", "spongebob-uc"}; "speech:rain__morgan-freeman-uc:reader" ->
    {"rain", "morgan-freeman-uc"} (names are slugs: no "__", no ":")."""
    body = item.split(":", 1)[1].split(":")[0]
    a, _, b = body.partition(SEP)
    return {a, b}


def merged_baseline(old: dict, run1: tuple, others: list, fresh: set) -> dict:
    """make: items already in the baseline whose song and voice weren't (re)frozen keep their scores and bands; the
    rest take the first run's scores and the runs' bands."""
    stamp, s1 = run1
    nb = bands(s1, *others)
    keep = lambda i: i in (old.get("scores") or {}) and not (parts(i) & fresh)
    return {"run": stamp if not old else old.get("run", stamp), "made": time.strftime("%Y-%m-%d %H:%M"),
            "scores": {i: (old["scores"][i] if keep(i) else sc) for i, sc in s1.items()},
            "bands": {i: ((old.get("bands") or {}).get(i, nb.get(i, {})) if keep(i) else nb.get(i, {})) for i in s1},
            "renders": {i: (old.get("renders", {}).get(i, old.get("run")) if keep(i) else stamp) for i in s1}}


def blocking(verdicts: dict, pending: list) -> list:
    """What holds the merge: an "old" verdict blocks; a pair not heard yet holds (MasterPlan, q4)."""
    return [i for i, v in verdicts.items() if v == "old"] + [i for i in pending if i not in verdicts]


def _verdicts(run: str) -> dict:
    out = {}
    for line in (GOLDEN / "verdicts.jsonl").read_text(encoding="utf-8").splitlines() if (GOLDEN / "verdicts.jsonl").exists() else []:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("run") == run:
            out[r["item"]] = r["verdict"]
    return out


# ---------------------------------------------------------------- the golden folder

def _config():
    """golden/config.toml: the app's config with every path inside golden/ (models read from the app folder)."""
    src = (APP / "config.toml") if (APP / "config.toml").exists() else ROOT / "config.toml"
    text = src.read_text(encoding="utf-8")
    import re
    w = (GOLDEN / "work").as_posix()
    paths = (f"[paths]\nmodels_dir = '{(APP / 'models').as_posix()}'\nvoices_dir = '{(GOLDEN / 'voices').as_posix()}'\n"
             f"cache_dir = '{w}/cache'\noutput_dir = '{w}/output'\nlogs_dir = '{w}/logs'\nuploads_dir = '{w}/uploads'\n")
    text = re.sub(r"\[paths\][^\[]*", paths + "\n", text, count=1)
    _write_text(GOLDEN / "config.toml", text)
    os.environ["UNDERCOVER_CONFIG"] = str(GOLDEN / "config.toml")


def _write_text(p: Path, text: str):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _cut(src: Path, dest: Path, start: float, seconds: float):
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(start), "-t", str(seconds), "-i", str(src), "-ac", "2",
                    "-ar", "44100", str(dest)], check=True)


def _freeze_song(spec: dict, name: str, src: Path, start: float, seconds: float):
    dest = GOLDEN / "songs" / f"{name}.flac"
    _cut(src, dest, start, seconds)
    spec["songs"] = [s for s in spec.get("songs", []) if s["name"] != name] + [
        {"name": name, "file": dest.name, "sha256": sha256(dest), "from": str(src), "start": start, "seconds": seconds}]


def _freeze_voice(spec: dict, name: str):
    """A copy of the voice (read from the app's voices) and its person's reference embedding (from its project)."""
    import numpy as np
    src = APP / "models" / "voices" / name
    if not src.is_dir():   # the demo voice: its model files live in the app folder's assets (not in git)
        src = next((d for d in (APP / "undercover" / "assets" / "demo-voice" / name, ROOT / "undercover" / "assets" / "demo-voice" / name)
                    if (d / "model.pth").exists()), None)
    if src is None:
        print(f"  {name}: not installed, left out")
        return
    dest = GOLDEN / "voices" / name
    shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(src, dest)
    meta = _read(dest / "meta.json", {})
    proj = (meta.get("extra") or {}).get("project")
    ref = None
    pj = APP / "cache" / "training" / str(proj)
    if proj and (pj / "embeddings.npz").exists():
        d = _read(pj / "project.json", {})
        with np.load(pj / "embeddings.npz") as z:
            k = f"speaker:{(d.get('reference') or {}).get('cluster')}"
            if k in z.files:
                ref = z[k].copy()
    if ref is not None:
        np.save(dest / "reference.npy", ref / (np.linalg.norm(ref) + 1e-9))
    spec["voices"] = [v for v in spec.get("voices", []) if v["name"] != name] + [
        {"name": name, "sha256": sha256(dest / "model.pth"), "person": (meta.get("extra") or {}).get("person"),
         "reference": ref is not None}]


def _check_frozen(spec: dict):
    for s in spec["songs"]:
        if sha256(GOLDEN / "songs" / s["file"]) != s["sha256"]:
            raise SystemExit(f"golden song {s['name']} changed on disk: the set isn't the set any more (make it again)")
    for v in spec["voices"]:
        if sha256(GOLDEN / "voices" / v["name"] / "model.pth") != v["sha256"]:
            raise SystemExit(f"golden voice {v['name']} changed on disk (refreeze it on purpose, or restore it)")


def items_of(spec: dict) -> list:
    have = {v["name"] for v in spec["voices"]}
    covers = [f"cover:{s}{SEP}{v}" for s, vs in spec["covers"].items() for v in vs if v in have]
    speech = [f"speech:{line}{SEP}{v}{'' if cad else ':reader'}" for line, _ in LINES for v, cad in SPEECH_VOICES if v in have]
    return covers + speech


# ---------------------------------------------------------------- running and scoring (GPU)

def _gpu_idle() -> bool:
    import urllib.request
    try:
        with urllib.request.urlopen("http://127.0.0.1:7860/api/jobs", timeout=3) as r:
            return not [j for j in json.loads(r.read()) if j.get("state") not in ("done", "error", "cancelled")]
    except Exception:
        return True   # the user's server isn't up


def _load16(p: Path):
    from undercover.core.engine.convert import load_16k
    return load_16k(Path(p))


def _likeness(x16, ref, emb) -> float | None:
    import numpy as np
    if ref is None or len(x16) < 16000:
        return None
    w = 3 * 16000
    segs = [x16[i:i + w] for i in range(0, len(x16) - w + 1, w // 2)]
    if not segs:
        return None
    rms = np.array([float(np.sqrt(np.mean(s ** 2))) for s in segs])
    act = [s for s, r in zip(segs, rms) if r > rms.max() * 0.1][:16]
    sims = [float(emb.embed(s) @ ref) for s in act]
    return round(float(np.mean(sims)), 3) if sims else None


def _words(t: str) -> list:
    import re
    return re.findall(r"[a-z0-9']+", t.lower())


def _wer(ref: list, hyp: list) -> float:
    if not ref:
        return 0.0
    d = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        prev, d[0] = d[0], i
        for j, h in enumerate(hyp, 1):
            prev, d[j] = d[j], min(d[j] + 1, d[j - 1] + 1, prev + (r != h))
    return d[-1] / len(ref)


def _kept(ref: list, hyp: list) -> float | None:
    """The share of the reference's words that come through, in order (longest common subsequence / reference).
    Not 1 - WER: the converted lead may rightly hold more words than a lead stem (the choruses the lead gate moved
    over from the backing), and those aren't errors."""
    if not ref:
        return None
    d = [0] * (len(hyp) + 1)
    for r in ref:
        prev = 0
        for j, h in enumerate(hyp, 1):
            prev, d[j] = d[j], (prev + 1 if r == h else max(d[j], d[j - 1]))
    return round(d[-1] / len(ref), 3)


def _mix_scores(path: Path) -> dict:
    """The file as the user's player decodes it: loudness (LUFS), true peak (dBTP, 4x oversampled) and the share of
    samples at full scale, per channel. Phase 21: decoding with -ac 1 read every cover 3 dB hot (ffmpeg's stereo to mono
    is (L + R) / sqrt 2), so the first make's +1 to +2 dB peaks and Toxic's 0.8% "clipped" were the measure's."""
    import re
    import numpy as np
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "f32le", "-ac", "2", "-ar", "44100", "-"],
                         capture_output=True, check=True).stdout
    x = np.frombuffer(raw, np.float32)
    err = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-af", "ebur128=peak=true:framelog=quiet",
                          "-f", "null", "-"], capture_output=True, text=True, errors="replace").stderr
    lu = re.findall(r"I:\s+(-?[\d.]+) LUFS", err)
    tp = re.findall(r"Peak:\s+(-?[\d.]+|-inf) dBFS", err)
    return {"loud_db": float(lu[-1]) if lu else None,
            "peak_db": float(tp[-1]) if tp and tp[-1] != "-inf" else None,
            "clipped": round(float(np.mean(np.abs(x) >= 0.999)), 5)}


def _render(src: Path, dest: Path):
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), str(dest)], check=True)


def run_set(stamp: str, spec: dict) -> dict:
    """Every item of the set with the current code -> {item: scores}; renders in runs/<stamp>/renders/."""
    import numpy as np
    import soundfile as sf
    from undercover.core import gpu, jobs
    from undercover.core.cadence import transcribe
    from undercover.core.separate import separate
    from undercover.core.speaker import SpeakerEmbedder
    from undercover.core.voices import get_voice
    from undercover.doubleagent.project import noisiness
    from undercover.doubleagent.service import voice_f0
    from undercover.frontman import leadgate
    from undercover.frontman.fit import pick_fit, pick_octave
    from undercover.frontman.oneshot import _LAST_F0, chordal_backing, song_vocal_f0, vocal_cleanliness
    from undercover.frontman.pipeline import ENGINE, CoverOptions
    from undercover.speakeasy import speak
    out_dir = GOLDEN / "runs" / stamp
    tmp = GOLDEN / "work" / "tmp"
    shutil.rmtree(GOLDEN / "work" / "cache", ignore_errors=True)   # every run separates afresh: a phase may have changed it
    tmp.mkdir(parents=True, exist_ok=True)
    emb = SpeakerEmbedder()
    rmvpe = gpu.MODELS.converter.rmvpe
    refs = {v["name"]: (np.load(GOLDEN / "voices" / v["name"] / "reference.npy") if v["reference"] else None)
            for v in spec["voices"]}
    scores, to_hear = {}, []
    for s in spec["songs"]:
        song = GOLDEN / "work" / "uploads" / f"{s['name']}.flac"
        song.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(GOLDEN / "songs" / s["file"], song)
        stems = separate(song, dereverb=True, karaoke=True, separator=gpu.MODELS.separator, quality="high")
        song_hz = song_vocal_f0(stems["vocals"], rmvpe)
        melody = _LAST_F0.get(str(stems["vocals"]))
        back16 = _load16(stems["backing"]) if stems.get("backing") else None
        g = leadgate.plan(_load16(stems["vocals"]), back16)
        gate_s = round(sum(b - a for a, b in g["instrument"]) + sum(b - a for a, b in g["chorus"]), 1)
        # the reference for words and pitch: the lead as it went into the conversion (after the gate)
        lead16 = _load16(leadgate.apply(stems, g, tmp, f"golden-{s['name']}")["vocals"])
        chordal = chordal_backing(stems.get("backing"), rmvpe)
        for vname in spec["covers"].get(s["name"], []):
            if vname not in refs:
                continue
            item = f"cover:{s['name']}{SEP}{vname}"
            v = get_voice(vname)
            vhz, speech_trained = voice_f0(v)
            rng = (v.extra or {}).get("f0_range")
            if rng and melody is not None:
                fit = pick_fit(rng, speech_trained, melody)
                octave, key, outside = fit["octave"], fit["key"], fit["outside"]
            else:
                octave, key, outside = pick_octave(vhz, speech_trained, song_hz), 0, None
            opts = CoverOptions(voice=v.name, key=key, octave=octave, quality="high", dereverb=True, karaoke=True,
                                backing_voice=None if chordal else v.name, backing_octave=octave,
                                backing_db=-3.0 if chordal else None, polish=True, pitch_cleanup=True)
            res = ENGINE.make_cover(song, opts, out=tmp / f"{item.replace(':', '_')}", stems=stems, record=False, title=s["name"])
            conv16 = _load16(res.converted_vocal)
            n = min(len(conv16), len(lead16))
            # words: Whisper on the original lead and on the converted lead
            a, b = tmp / "orig.wav", tmp / "conv.wav"
            sf.write(str(a), lead16[:n], 16000)
            sf.write(str(b), conv16[:n], 16000)
            to, tc = transcribe([a, b])
            wo = _words(to)
            # pitch: the converted lead against the original's melody moved by the fit
            f_o = rmvpe.infer_from_audio(lead16[:n], thred=0.03) * 2 ** ((octave * 12 + key) / 12)
            f_c = rmvpe.infer_from_audio(conv16[:n], thred=0.03)
            m = min(len(f_o), len(f_c))
            both = (f_o[:m] > 0) & (f_c[:m] > 0)
            cents = float(np.median(np.abs(1200 * np.log2(f_c[:m][both] / f_o[:m][both])))) if both.sum() > 20 else None
            sc = {"likeness": _likeness(conv16, refs[vname], emb),
                  "words_kept": _kept(wo, _words(tc)),
                  "cents_off": round(cents, 1) if cents is not None else None,
                  "outside": round(outside, 3) if outside is not None else None,
                  "hiss": noisiness(conv16, 16000), "bleed_db": vocal_cleanliness(res.converted_vocal).get("bleed"),
                  "gate_s": gate_s, **_mix_scores(res.output), "octave": octave, "key": key}
            scores[item] = sc
            _render(res.output, out_dir / "renders" / f"{item.replace(':', '_')}.flac")
            print(f"  {item}: {sc}", flush=True)
    for line, text in LINES:
        for vname, cad in SPEECH_VOICES:
            if vname not in refs:
                continue
            item = f"speech:{line}{SEP}{vname}{'' if cad else ':reader'}"
            job = jobs.Job("speak")
            r = speak.make_speech(job, text, vname, "auto", cadence=cad)
            wav = speak.speech_dir() / r["file"]
            x16 = _load16(wav)
            heard = transcribe([wav])[0]
            f0 = rmvpe.infer_from_audio(x16, thred=0.03)
            f0 = f0[f0 > 0]
            sc = {"likeness": _likeness(x16, refs[vname], emb), "wer": round(_wer(_words(text), _words(heard)), 3),
                  "pace": round(len(_words(text)) / (len(x16) / 16000), 3),
                  "f0_st": round(float(12 * np.log2(np.median(f0) / 100)), 2) if len(f0) else None,
                  "clipped": round(float(np.mean(np.abs(x16) >= 0.999)), 5)}
            scores[item] = sc
            _render(wav, out_dir / "renders" / f"{item.replace(':', '_')}.flac")
            print(f"  {item}: {sc}", flush=True)
    _write(out_dir / "scores.json", {"run": stamp, "scores": scores, "code": _code_stamp()})
    for d in ("cache", "output", "uploads", "tmp"):   # the renders are kept; the working files aren't (about 1 GB)
        shutil.rmtree(GOLDEN / "work" / d, ignore_errors=True)
    return scores


def _code_stamp() -> str:
    try:
        return subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    except OSError:
        return "?"


def _prune():
    b = _read(GOLDEN / "baseline.json", {})
    keep = {b.get("run")} | set((b.get("renders") or {}).values())
    runs = sorted(p for p in (GOLDEN / "runs").glob("*") if p.is_dir())
    for p in runs[:-KEEP_RUNS]:
        if p.name not in keep:
            shutil.rmtree(p / "renders", ignore_errors=True)   # scores stay; renders of old runs go (the baseline's stay)


# ---------------------------------------------------------------- pairs

def make_pairs(run: str, chosen: list) -> Path:
    """The blind pairs: the baseline's render and this run's, the same 20 s (from 5 s in), the same loudness."""
    import numpy as np
    import soundfile as sf
    baseline = _read(GOLDEN / "baseline.json", {})
    out = GOLDEN / "runs" / run / "pairs"
    shutil.rmtree(out, ignore_errors=True)
    key = {}
    for n, item in enumerate(chosen, 1):
        f = f"{item.replace(':', '_')}.flac"
        sides = {"old": GOLDEN / "runs" / str(render_run(baseline, item)) / "renders" / f, "new": GOLDEN / "runs" / run / "renders" / f}
        if not all(p.exists() for p in sides.values()):
            continue
        cuts = {}
        for k, p in sides.items():
            x, sr = sf.read(str(p), dtype="float32", always_2d=True)
            s0 = int(5 * sr) if item.startswith("cover:") else 0
            x = x[s0:s0 + 20 * sr]
            r = float(np.sqrt(np.mean(x ** 2))) or 1e-9
            x = x * min(10 ** (-18 / 20) / r, 0.89 / (float(np.abs(x).max()) or 1))
            fade = int(0.05 * sr)
            x[:fade] *= np.linspace(0, 1, fade)[:, None]
            x[-fade:] *= np.linspace(1, 0, fade)[:, None]
            cuts[k] = (x.astype(np.float32), sr)
        order = ["old", "new"]
        random.Random(f"{run}-{item}").shuffle(order)
        for label, which in zip("AB", order):
            d = out / f"{n}-{item.replace(':', '_')}"
            d.mkdir(parents=True, exist_ok=True)
            sf.write(str(d / f"{label}.wav"), cuts[which][0], cuts[which][1], subtype="PCM_16")
        key[item] = {"A": order[0], "B": order[1], "folder": f"{n}-{item.replace(':', '_')}"}
    _write(out / "key.json", key)
    return out


# ---------------------------------------------------------------- the commands

def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("make")
    m.add_argument("--song", action="append", default=[], help="NAME=FILE@START+SECONDS")
    m.add_argument("--with", dest="with_", default="", help="the voices for the --song(s), comma separated")
    m.add_argument("--voice", action="append", default=[])
    r = sub.add_parser("run")
    r.add_argument("--force", action="store_true")
    for c in ("compare", "pairs"):
        sub.add_parser(c).add_argument("run", nargs="?")
    v = sub.add_parser("verdict")
    v.add_argument("run")
    v.add_argument("item")
    v.add_argument("verdict", choices=("new", "old", "same"))
    sub.add_parser("accept").add_argument("run")
    sub.add_parser("rebands")
    rf = sub.add_parser("refreeze")
    rf.add_argument("voice")
    a = ap.parse_args()
    _config()
    spec = _read(GOLDEN / "set.json", {"songs": [], "voices": [], "covers": {}})
    last = lambda: sorted(p.name for p in (GOLDEN / "runs").glob("*") if (p / "scores.json").exists())[-1]

    if a.cmd == "make":
        have_s, have_v = {x["name"] for x in spec["songs"]}, {x["name"] for x in spec["voices"]}
        songs = [x for x in DEFAULT_SONGS if x[0] not in have_s]
        for x in a.song:
            name, rest = x.split("=", 1)
            f, sec = rest.rsplit("@", 1)
            start, secs = sec.split("+")
            songs.append((name, f, float(start), float(secs)))
            if a.with_:
                spec["covers"][name] = [v.strip() for v in a.with_.split(",") if v.strip()]
        for name, f, start, secs in songs:
            print(f"song {name}: {f} {start}+{secs} s")
            _freeze_song(spec, name, Path(f), start, secs)
            spec["covers"].setdefault(name, DEFAULT_COVERS.get(name, DEFAULT_VOICES[:3]))
        voices = a.voice or [v for v in DEFAULT_VOICES if v not in have_v]
        for name in voices:
            print(f"voice {name}")
            _freeze_voice(spec, name)
        fresh = {x[0] for x in songs} | set(voices)
        spec["made"] = time.strftime("%Y-%m-%d %H:%M")
        _write(GOLDEN / "set.json", spec)
        if not _gpu_idle() and not os.environ.get("GOLDEN_FORCE"):
            raise SystemExit("the user's UnderCover is busy on the GPU: run make again when it's idle")
        _check_frozen(spec)
        runs = []
        for k in range(MAKE_RUNS):
            stamp = time.strftime("%Y%m%d-%H%M%S") + f"-make{k + 1}"
            print(f"run {stamp}")
            runs.append((stamp, run_set(stamp, spec)))
        _write(GOLDEN / "baseline.json", merged_baseline(_read(GOLDEN / "baseline.json", {}), runs[0], [r[1] for r in runs[1:]], fresh))
        print(f"baseline: {len(runs[0][1])} items run; new or refrozen: {', '.join(sorted(fresh)) or 'none'}")
    elif a.cmd == "run":
        if not _gpu_idle() and not a.force:
            raise SystemExit("the user's UnderCover is busy on the GPU: run again when it's idle (or --force)")
        _check_frozen(spec)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        run_set(stamp, spec)
        _prune()
        print(f"run {stamp}: python tools/golden.py compare {stamp}")
    elif a.cmd == "compare":
        run = a.run or last()
        fl = flags(_read(GOLDEN / "baseline.json", {}), _read(GOLDEN / "runs" / run / "scores.json", {})["scores"])
        for f in fl:
            print(f"FLAG {f['item']}: {f['score']} {f['was']} -> {f['now']} ({f['bands']} bands)")
        print(f"{len(fl)} flag(s) in {run}")
    elif a.cmd == "pairs":
        run = a.run or last()
        sc = _read(GOLDEN / "runs" / run / "scores.json", {})["scores"]
        fl = flags(_read(GOLDEN / "baseline.json", {}), sc)
        n_runs = len(list((GOLDEN / "runs").glob("*")))
        chosen = pick_pairs(fl, sorted(sc), n_runs)
        out = make_pairs(run, chosen)
        print(f"{len(chosen)} pair(s) in {out} (key.json is for MasterPlan only)")
    elif a.cmd == "verdict":
        with open(GOLDEN / "verdicts.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"run": a.run, "item": a.item, "verdict": a.verdict, "at": time.strftime("%Y-%m-%d %H:%M")}) + "\n")
        print(f"{a.item}: {a.verdict}")
    elif a.cmd == "accept":
        base = _read(GOLDEN / "baseline.json", {})
        sc = _read(GOLDEN / "runs" / a.run / "scores.json", {})["scores"]
        vd = _verdicts(a.run)
        hold = blocking(vd, [f["item"] for f in flags(base, sc)])
        if hold:
            raise SystemExit(f"not accepted: {', '.join(hold)} (an 'old' verdict blocks; a flag not heard yet holds)")
        _write(GOLDEN / "baseline.json", accepted(base, a.run, sc, vd))
        print("baseline moved for: " + (", ".join(i for i in sc if vd.get(i) in ("new", "same")) or "nothing heard as new / same"))
    elif a.cmd == "rebands":
        if not _gpu_idle():
            raise SystemExit("the user's UnderCover is busy on the GPU: run again when it's idle")
        _check_frozen(spec)
        base = _read(GOLDEN / "baseline.json", {})
        extra = [run_set(time.strftime("%Y%m%d-%H%M%S") + f"-bands{k + 1}", spec) for k in range(MAKE_RUNS - 1)]
        wide = bands(base["scores"], *extra)
        base["bands"] = {i: {k: max(v, (base.get("bands") or {}).get(i, {}).get(k, 0)) for k, v in b.items()} for i, b in wide.items()}
        _write(GOLDEN / "baseline.json", base)
        _prune()
        print("bands widened from the baseline and 2 more runs")
    elif a.cmd == "refreeze":
        _freeze_voice(spec, a.voice)
        _write(GOLDEN / "set.json", spec)
        base = _read(GOLDEN / "baseline.json", {})
        for k in ("scores", "bands"):
            base[k] = {i: v for i, v in (base.get(k) or {}).items() if a.voice not in parts(i)}
        _write(GOLDEN / "baseline.json", base)
        print(f"{a.voice} refrozen; its items take their new baseline from the next run's accept")


if __name__ == "__main__":
    main()
