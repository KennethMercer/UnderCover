"""Smoke test: does UnderCover still work end to end? Run after every phase of a restructure and before merging.

    .venv\\Scripts\\python.exe tools\\smoke.py               # unit + API + one cover + one speech (~3 min, on its own sandbox server)
    .venv\\Scripts\\python.exe tools\\smoke.py --full        # + a quick training with Pause/Resume, transcripts + cadence (~13 min)
    .venv\\Scripts\\python.exe tools\\smoke.py --unit-only   # no server needed (~20 s)
    .venv\\Scripts\\python.exe tools\\smoke.py --port 7861   # read-only API checks against a server that is already running

Since phase 8 the checks that make and delete things (cover, speech, training, cadence) run on a SANDBOX server that smoke
starts itself on the first free port in 7862-7869 (or UNDERCOVER_SANDBOX_PORT), so runs from several worktrees can overlap:
scratch uploads / output / cache / logs / voices (a copy of one voice and its training project), the real models/
read-only, and the app's delete guards limited to the scratch folder. The
user's folders are listed before and after the whole run; any difference fails it. Refuses the user's server on 7860;
7861 stays free for people's manual checks.
Exit code 0 = all passed. Prints one line per check.

RULE (after 2026-10-11, when a check here deleted the user's songs and covers): a check must never be able to reach
the user's data folders (uploads, output, models, cache, training projects). Unit checks that create, move or delete
files run inside `isolated_data()`: every [paths] folder, config.toml and the storage allow-list point into a temp
dir, and core/storage.py refuses the app's real folders in test mode. Never copy the real config.toml into a test.
(The one config derived from the real one is the sandbox server's: the real settings with [paths] replaced, and the
result asserted to point only into the scratch folder, apart from the read-only model files, before anything starts.)
"""
import argparse
import contextlib
import json
import os
import shutil
import sys
import time
import traceback
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(1, str(ROOT / "scripts"))   # make_demo_voice (the demo voice's builder) lives there

RESULTS: list = []
try:   # a check's note with a non-Windows character (≥) must not fail the check when output goes to a file or pipe
    sys.stdout.reconfigure(errors="replace")
except (AttributeError, ValueError):
    pass


def check(name):
    def deco(fn):
        def run(*a, **kw):
            t0 = time.time()
            try:
                note = fn(*a, **kw)
                RESULTS.append((True, name))
                print(f"  PASS  {name}" + (f"  ({note})" if note else "") + f"  [{time.time() - t0:.1f}s]", flush=True)
            except Exception as e:   # noqa: BLE001 - report and carry on
                RESULTS.append((False, name))
                print(f"  FAIL  {name}: {type(e).__name__}: {e}", flush=True)
                if "--verbose" in sys.argv:
                    traceback.print_exc()
        return run
    return deco


def _bare(entries: list) -> list:
    """Held-work entries without what holding adds (id, held_at), to compare with the spec that went in."""
    return [{k: v for k, v in e.items() if k not in ("id", "held_at")} for e in entries]


# ---------------------------------------------------------------- HTTP helpers

BASE = ""


def api(path, body=None, method=None, timeout=120):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, method=method or ("POST" if body is not None else "GET"),
                                 headers={"Content-Type": "application/json", "Origin": BASE})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return json.loads(raw) if raw[:1] in (b"{", b"[") else raw


def wait_for(fn, timeout, every=2.0, what="condition"):
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = fn()
        if v:
            return v
        time.sleep(every)
    raise TimeoutError(f"{what} not reached in {timeout:.0f} s")


# ---------------------------------------------------------------- unit checks (no server)

@check("imports: every module loads")
def u_imports():
    import importlib
    mods = [p.relative_to(ROOT).with_suffix("").as_posix().replace("/", ".")
            for p in (ROOT / "undercover").rglob("*.py") if "__pycache__" not in p.parts]
    bad = []
    for m in mods:
        if m.endswith("__main__"):
            continue
        try:
            importlib.import_module(m)
        except Exception as e:   # noqa: BLE001
            bad.append(f"{m}: {e}")
    assert not bad, "; ".join(bad)
    return f"{len(mods)} modules"


MODULES = ("frontman", "doubleagent", "speakeasy")   # the suite's modules (safehouse and app have their own rules)


def import_edges(pkg: Path) -> list:
    """(importing module, imported module, file:line) for every import inside the package, including the ones inside
    functions. Relative imports are resolved; `from X import name` counts as importing X.name when that's a module."""
    import ast
    names = {".".join(p.relative_to(pkg.parent).with_suffix("").parts).removesuffix(".__init__")
             for p in pkg.rglob("*.py") if "__pycache__" not in p.parts}
    edges = []
    for f in pkg.rglob("*.py"):
        if "__pycache__" in f.parts:
            continue
        mod = ".".join(f.relative_to(pkg.parent).with_suffix("").parts)
        here = mod.removesuffix(".__init__") if f.name == "__init__.py" else mod.rsplit(".", 1)[0]
        for n in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if isinstance(n, ast.Import):
                targets = [a.name for a in n.names]
            elif isinstance(n, ast.ImportFrom):
                base = n.module or ""
                if n.level:
                    up = here.split(".")[: len(here.split(".")) - (n.level - 1)]
                    base = ".".join(up + ([n.module] if n.module else []))
                targets = [f"{base}.{a.name}" if f"{base}.{a.name}" in names else base for a in n.names]
            else:
                continue
            where = f"{f.relative_to(pkg.parent).as_posix()}:{n.lineno}"
            edges += [(mod.removesuffix(".__init__"), t, where) for t in targets if t.split(".")[0] == pkg.name]
    return edges


def dependency_problems(pkg: Path) -> list:
    """ARCHITECTURE.md's dependency rules, checked on the import graph."""
    def part(m):   # "core", "frontman", ..., "app", or "" for the package's own top level (the thin entry points)
        bits = m.split(".")
        return bits[1] if len(bits) > 2 or (len(bits) == 2 and (pkg / bits[1]).is_dir()) else ""
    bad = []
    for src, dst, where in import_edges(pkg):
        s, d = part(src), part(dst)
        if s == d:
            continue
        why = None
        if s == "core" and d in (*MODULES, "safehouse", "app"):
            why = "core imports nothing from the modules, safehouse or app"
        elif s == "doubleagent" and d != "core":
            why = "doubleagent imports core only"
        elif s in ("frontman", "speakeasy") and d == "doubleagent" and dst != f"{pkg.name}.doubleagent.service":
            why = f"{s} uses DoubleAgent through doubleagent.service only"
        elif s in ("frontman", "speakeasy") and d in ("frontman", "speakeasy"):
            why = "frontman and speakeasy never import each other"
        elif d == "safehouse" and s != "app":
            why = "only app imports safehouse"
        elif d == "app" and src != f"{pkg.name}.server":
            why = "nothing imports app except the undercover/server.py entry point"
        if why:
            bad.append(f"{where} imports {dst} ({why})")
    return bad


@check("dependencies: core / modules / safehouse / app rules (ARCHITECTURE.md)")
def u_deps():
    edges = import_edges(ROOT / "undercover")
    bad = dependency_problems(ROOT / "undercover")
    assert not bad, "; ".join(bad)
    return f"{len(edges)} imports checked"


@check("pick_fit: key + octave fit")
def u_fit():
    import numpy as np
    from undercover.frontman.fit import pick_fit
    f0 = np.full(500, 440.0)   # A4 melody
    fit = pick_fit([110.0, 150.0, 220.0], False, f0)   # a low voice
    assert fit["octave"] <= -1 and fit["outside"] < 0.5, fit
    fit0 = pick_fit([300.0, 440.0, 600.0], False, f0)
    assert fit0["octave"] == 0 and fit0["key"] == 0, fit0
    # the register (phase 22, by ear): the melody's median goes where the voice is comfortable, a speech-trained voice
    # ~4 st over its speaking median, by octave (the vocal) and up to 7 st of key (the whole song)
    rng = np.random.default_rng(22)
    hold_on = 200.0 * 2 ** (rng.uniform(-5, 5, 800) / 12)
    smoke = pick_fit([85.7, 110.3, 159.6], True, hold_on)      # Agent Smoke: the user wanted 4-6 st down
    assert 12 * smoke["octave"] + smoke["key"] in range(-7, -3) and abs(smoke["register_st"]) < 1.5, smoke
    deep = pick_fit([72.0, 104.6, 174.7], True, hold_on)       # Freeman: well under the original
    assert 12 * deep["octave"] + deep["key"] <= -6 and abs(deep["register_st"]) < 1.5, deep
    sung = pick_fit([140.0, 200.0, 300.0], False, hold_on)     # a singer at that pitch keeps the song as it is
    sb = pick_fit([115.4, 229.7, 420.3], True, hold_on)       # SpongeBob: not raised to his register (by ear: 0 beat +6)
    toxic = 387.0 * 2 ** (rng.uniform(-10, 4, 800) / 12)      # a wide, high melody (Toxic: low notes ~216 Hz)
    fr = pick_fit([72.0, 104.6, 174.7], True, toxic)           # Freeman: not so deep the low notes growl (by ear: -15 beat -18)
    assert -17 <= 12 * fr["octave"] + fr["key"] <= -13, fr
    assert 12 * sb["octave"] + sb["key"] == 0, sb
    assert sung["octave"] == 0 and sung["key"] == 0, sung
    from undercover.frontman import fit as F
    assert "alt" not in deep or abs((12 * deep["alt"]["octave"] + deep["alt"]["key"]) - (12 * deep["octave"] + deep["key"])) >= F.ALT_APART
    return (f"low voice -> octave {fit['octave']} key {fit['key']:+d}; Agent Smoke on a 200 Hz melody -> "
            f"{12 * smoke['octave'] + smoke['key']:+d} st, Freeman -> {12 * deep['octave'] + deep['key']:+d} st, a singer at that pitch -> 0")


@check("worklist: overnight window and deadline")
def u_window():
    from datetime import datetime
    import undercover.core.worklist as w
    saved = w._STATE
    try:
        w._STATE = {"held": False, "resume": [], "queue": [], "armed": True, "report": None,
                    "settings": {"start_at": "23:00", "finish_by": "07:00", "keep_awake": True}}
        assert not w._in_window(datetime(2026, 1, 1, 22, 30))
        assert w._in_window(datetime(2026, 1, 1, 23, 5))
        assert w._in_window(datetime(2026, 1, 2, 3, 0))
        assert not w._in_window(datetime(2026, 1, 2, 7, 30))
    finally:
        w._STATE = saved


@check("training data: compact features expand bit-identically")
def u_phone():
    import torch
    from undercover.doubleagent.trainer import _phone, index_feats
    feats = torch.randn(10, 768).half()
    x = {"phone50": feats, "pitch": torch.zeros(19, dtype=torch.long)}
    assert torch.equal(_phone(x), feats.repeat_interleave(2, 0)[:19])
    assert index_feats({"items": [x]}).shape == (10, 768)


@check("stems: FLAC and WAV lookup")
def u_stems():
    import tempfile
    from undercover.core.separate import stem_files
    d = Path(tempfile.mkdtemp())
    try:
        for n in ("vocals.flac", "other.wav", "x.part.flac", "notes.txt"):
            (d / n).write_bytes(b"")
        names = sorted(p.name for p in stem_files(d))
        assert names == ["other.wav", "vocals.flac"], names
    finally:
        shutil.rmtree(d, ignore_errors=True)


@check("speakeasy: sentences and Reader auto-pick")
def u_speak():
    from undercover.speakeasy import tts
    s = tts.sentences("Hello there. How are you? I'm fine!\n\nNew paragraph, Mr. Smith said. " + "word, " * 80 + "end.")
    assert [x["text"] for x in s[:3]] == ["Hello there.", "How are you?", "I'm fine!"], s[:3]
    assert s[2]["pause"] > s[0]["pause"], "no paragraph pause"
    assert s[3]["text"].startswith("New paragraph, Mr. Smith said."), f"split at an abbreviation: {s[3]['text'][:40]}"
    assert all(len(x["text"]) <= tts.MAX_CHARS for x in s), "a sentence is too long for Kokoro"
    assert tts.pick_reader(104.6, 72.0).startswith("am_"), "deep male voice got a female Reader"
    assert tts.pick_reader(180.0, 109.3)[1] == "m", "Obama (180 Hz, p5 109) got a female Reader"
    assert tts.pick_reader(210.0, 160.0)[1] == "f", "a female voice got a male Reader"
    assert tts.pitch_for("am_michael", 110.6) == 0.0
    return f"{len(s)} sentences"


@check("sources: YouTube block and spoken-word detection")
def u_sources():
    from undercover.doubleagent import sources as s
    from undercover.core import youtube as yt
    try:
        yt.check_blocked(Exception("ERROR: Sign in to confirm you're not a bot"))
        raise AssertionError("block not detected")
    except yt.YouTubeBlocked:
        pass
    assert s.SPOKEN.search("Barack Obama - Obama DNC Speech")
    assert not s.SPOKEN.search("Elliott Smith - Between the Bars")
    assert s.is_sung("Elliott Smith - Clementine (isolated vocals)")
    assert not s.is_sung("Elliott Smith Interview 1998")


@check("demo voice builder: a paused training leaves a folder that resumes (resume.pt and all, byte for byte)")
def u_demo_pause():
    """2026-10-12: a pause lost a training 10 epochs from the end (the builder's sandbox took resume.pt with it)."""
    import hashlib
    import inspect
    import make_demo_voice as mdv
    with isolated_data() as box:
        proj = box / "sandbox" / "training" / "agent-smoke"
        (proj / "train").mkdir(parents=True)
        (proj / "clips").mkdir()
        (proj / "project.json").write_text(json.dumps({"name": "agent-smoke", "train": {"state": "paused", "epoch": 88}}),
                                           encoding="utf-8")
        (proj / "train" / "resume.pt").write_bytes(os.urandom(1 << 16))
        (proj / "train" / "e80.pth").write_bytes(os.urandom(4096))
        (proj / "clips" / "p0001.wav").write_bytes(os.urandom(2048))
        digest = lambda d: {str(f.relative_to(d)): hashlib.sha256(f.read_bytes()).hexdigest() for f in d.rglob("*") if f.is_file()}
        before = digest(proj)
        keep = box / "kept" / "agent-smoke-paused"
        (keep / "stale").mkdir(parents=True)   # an older kept copy is replaced, not merged
        mdv.keep_paused(proj, keep)
        shutil.rmtree(box / "sandbox")   # the builder's sandbox goes
        assert digest(keep) == before, "the kept folder isn't the project byte for byte"
        projects = box / "new-sandbox" / "training"
        projects.mkdir(parents=True)
        assert mdv.restore_paused(keep, projects) == "agent-smoke"
        assert digest(projects / "agent-smoke") == before, "the resumed project isn't the kept one"
    src = inspect.getsource(mdv.main)
    i = src.index('"paused" and what == "training"')
    assert src.index("keep_paused(", i) < src.index("sys.exit(", i), "a paused training exits before it's kept"
    assert "restore_paused(resume" in src and '"--resume"' in src
    return "kept before the sandbox goes; restored byte for byte into a new one; --resume wired"


@check("demo voice builder: more minutes later reads on (new sentences, never the same twice, the reader checked)")
def u_demo_topup():
    import numpy as np
    import make_demo_voice as mdv
    from undercover.speakeasy import tts
    real = tts.synth
    read = []

    def fake(text, reader):   # 30 s of silence per sentence, and a note of what was read
        read.append(text)
        return np.zeros(int(tts.SR * 30), np.float32)
    text = " ".join(f"Sentence number {i} is here." for i in range(40))
    try:
        tts.synth = fake
        with isolated_data() as box:
            work = box / "speech"
            work.mkdir()
            first = mdv.synthesize(text, "am_michael", 4, work)
            got1 = list(read)
            read.clear()
            more = mdv.synthesize(text, "am_michael", 8, work)
            assert len(first) == 2 and len(more) == 4 and more[:2] == first, (first, more)
            assert got1 and read and not set(got1) & set(read), "a sentence was read twice"
            assert read[0] == f"Sentence number {len(got1)} is here.", read[0]
            read.clear()
            assert mdv.synthesize(text, "am_michael", 8, work) == more and not read, "enough already: nothing more read"
            try:
                mdv.synthesize(text, "bm_george", 12, work)
                raise AssertionError("another reader was mixed in")
            except SystemExit as e:
                assert "am_michael" in str(e), e
    finally:
        tts.synth = real
    return "4 min, then 8 min reads on from the next sentence; 8 again reads nothing; another reader refused"
@check("vault: what goes in (trained voices, songs, covers, speech, choices, settings; no secrets), compressed and bit-identical, incremental, cut-short safe, pruning, restore that asks")
def u_vault():
    import hashlib
    import numpy as np
    import soundfile as sf
    from datetime import datetime
    from undercover.core import vault, storage
    from undercover.core.voices import ModelError
    sha = lambda f: hashlib.sha256(Path(f).read_bytes()).hexdigest()
    with isolated_data() as box:
        # imported inside: config.CONFIG_FILE is the sandbox's only now (imported earlier, it was the user's real
        # config.toml, and this check appended to it on 2026-10-13 before the guard below existed)
        from undercover.core.config import CONFIG, CONFIG_FILE, path
        assert storage._inside(CONFIG_FILE, box), f"the config isn't the sandbox's: {CONFIG_FILE}"
        saved_vault = dict(CONFIG.get("vault") or {})
        try:
            vd = path("voices_dir")
            for name, extra in (("mine-uc", {"trained": True}), ("downloaded", {}), ("agent-smoke", {"trained": False, "demo": True})):
                (vd / name).mkdir(parents=True)
                (vd / name / "meta.json").write_text(json.dumps({"name": name, "extra": extra}), encoding="utf-8")
                (vd / name / "model.pth").write_bytes(os.urandom(50_000) + bytes(200_000))
                (vd / name / "model.index").write_bytes(np.arange(60_000, dtype=np.float32).tobytes())
            up, out = path("uploads_dir"), path("output_dir")
            for d in (up, out / "speech"):
                d.mkdir(parents=True, exist_ok=True)
            (up / "abc.flac").write_bytes(os.urandom(30_000))
            (up / "abc.json").write_text('{"title": "A song"}', encoding="utf-8")
            (out / "A song (mine-uc).mp3").write_bytes(os.urandom(20_000))
            tone = (np.sin(np.arange(44100) / 20) * 8000).astype(np.int16)
            sf.write(out / "speech" / "line.wav", tone, 44100, subtype="PCM_16")
            sf.write(out / "speech" / "float.wav", tone.astype(np.float32) / 32768, 44100, subtype="FLOAT")
            # a speech as SpeakEasy keeps it: its wav and mp3 named by title, its record by id
            (out / "speech" / "Good evening (mine-uc).mp3").write_bytes(os.urandom(5_000))
            sf.write(out / "speech" / "Good evening (mine-uc).wav", tone, 44100, subtype="PCM_16")
            (out / "speech" / "0123456789.json").write_text(json.dumps({"id": "0123456789", "title": "Good evening",
                "voice": "mine-uc", "file": "Good evening (mine-uc).wav", "mp3": "Good evening (mine-uc).mp3"}), encoding="utf-8")
            tr = path("cache_dir") / "training" / "mine"
            (tr / "clips").mkdir(parents=True)
            (tr / "project.json").write_text('{"name": "mine", "mode": "speaking"}', encoding="utf-8")
            sf.write(tr / "clips" / "c1.wav", tone, 44100, subtype="PCM_16")
            (CONFIG_FILE.parent / "phone.json").write_text('{"devices": [{"token": "SECRET"}]}', encoding="utf-8")
            keys = {k: kind for kind, k, _ in vault.items("matters")}
            assert "voices/mine-uc/model.pth" in keys and not any(k.startswith(("voices/downloaded", "voices/agent-smoke")) for k in keys), keys
            assert "songs/abc.flac" in keys and keys["covers/speech/line.wav"] == "speech" and "settings/config.toml" in keys, keys
            assert "training/mine/project.json" in keys and not any("clips" in k for k in keys), "clips are for Everything"
            assert any("clips" in k for _, k, _ in vault.items("everything"))
            assert not any(k.endswith("phone.json") for _, k, _ in vault.items("everything")), "a secret went in"
            # the folder: refused inside the library, fine beside it
            assert not vault.check_folder(str(path("uploads_dir") / "v"))["ok"]
            vf = box / "vault-drive"
            chk = vault.check_folder(str(vf))
            assert chk["ok"], chk
            vault.set_folder(str(vf))
            r1 = vault.run()
            root = vault.root_of(vf)
            objs = {f.name.split(".", 1)[1] for f in (root / "objects").rglob("*") if f.is_file()}
            assert {"flac", "raw"} <= objs and ("lzma" in objs or "zlib" in objs), objs
            snap = vault.load_snapshot(root)["files"]
            for _, k, f in vault.items("matters"):   # every object comes back bit-identical
                assert vault.get(root / "objects", snap[k][0]) == f.read_bytes(), k
            line = next((root / "objects").rglob(sha(out / "speech" / "line.wav") + ".*"))
            assert line.suffix == ".flac", line
            assert r1["stored"] < r1["raw"], r1
            # incremental: nothing new, nothing written; one change, one object
            r2 = vault.run()   # straight after: a second snapshot, never the first one overwritten
            assert len(vault.snapshots(vault.root_of(vf))) == 2, vault.snapshots(vault.root_of(vf))
            assert r2["new"] == 0 and r2["bytes_written"] == 0, r2
            (up / "abc.json").write_text('{"title": "A song, renamed"}', encoding="utf-8")
            assert vault.run()["new"] == 1
            st = vault.status()   # what SafeHouse and the Doctor read: the stored size is saved with the run
            assert st["stored"] > 0 and st["stored"] < st["raw"] and st["last"]["pruned"] == 0, st
            # a run cut short leaves the last snapshot whole, and no new one
            before = vault.snapshots(root)
            try:
                vault.run(cancel=lambda: True)
                raise AssertionError("a cancelled run finished")
            except InterruptedError:
                pass
            assert vault.snapshots(root) == before
            # a damaged object is an error, never a quiet bad restore
            victim = next(f for f in (root / "objects").rglob("*.raw"))
            good = victim.read_bytes()
            victim.write_bytes(b"x" + good[1:])
            try:
                vault.get(root / "objects", victim.name.split(".")[0])
                raise AssertionError("a damaged object restored")
            except ModelError:
                pass
            victim.write_bytes(good)
            # restore: missing comes back; a different one asks; both / replace / skip
            orig = (out / "A song (mine-uc).mp3").read_bytes()
            (out / "A song (mine-uc).mp3").unlink()
            c = vault.contents()
            cover = next(i for i in c["kinds"]["cover"] if i["item"] == "A song (mine-uc).mp3")
            assert cover["here"] == "missing", cover
            assert vault.restore(cover["keys"])["restored"] == cover["keys"]
            assert (out / "A song (mine-uc).mp3").read_bytes() == orig
            (out / "A song (mine-uc).mp3").write_bytes(b"changed")
            asked = vault.restore(cover["keys"])
            assert asked["conflicts"] == cover["keys"] and not asked["restored"] and (out / "A song (mine-uc).mp3").read_bytes() == b"changed"
            vault.restore(cover["keys"], "both")
            assert (out / "A song (mine-uc) (from vault).mp3").read_bytes() == orig and (out / "A song (mine-uc).mp3").read_bytes() == b"changed"
            n_snaps = len(vault.snapshots(root))
            vault.restore(cover["keys"], "replace")
            assert (out / "A song (mine-uc).mp3").read_bytes() == orig and len(vault.snapshots(root)) == n_snaps + 1, "replace didn't vault first"
            voice = [k for k in snap if k.startswith("voices/mine-uc/")]
            (vd / "mine-uc" / "model.pth").write_bytes(b"retrained")
            vault.restore(voice, "both")
            assert sha(vd / "mine-uc-from-vault" / "model.pth") == snap["voices/mine-uc/model.pth"][0]
            assert (vd / "mine-uc-from-vault" / "meta.json").is_file(), "keep both brought back part of a voice"
            # a restored config keeps this install's [paths]
            here_paths = CONFIG_FILE.read_text(encoding="utf-8")
            CONFIG_FILE.write_text(here_paths + "\n[extra]\nx = 1\n", encoding="utf-8")
            vault.restore(["settings/config.toml"], "replace")
            import tomllib
            got = tomllib.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            assert got["paths"] == tomllib.loads(here_paths)["paths"] and "vault" in got, got
            # restore never writes outside the library
            snap2 = vault.load_snapshot(root)
            snap2["files"]["covers/../../escape.txt"] = snap2["files"]["songs/abc.json"]
            stamp = vault.snapshots(root)[-1]
            (root / "snapshots" / f"{stamp}.json").write_text(json.dumps(snap2), encoding="utf-8")
            try:
                vault.restore(["covers/../../escape.txt"], "both")
                raise AssertionError("a restore wrote outside the library")
            except ModelError:
                pass
            # one row per song and per speech, titled from its own record, even when its files are gone (Q's pages)
            for f in [*(out / "speech").glob("Good evening*"), out / "speech" / "0123456789.json"]:
                f.unlink()
            c = vault.contents()
            sp = [i for i in c["kinds"]["speech"] if i["item"] == "0123456789"]
            assert len(sp) == 1 and sp[0]["title"] == "Good evening (mine-uc)" and sp[0]["files"] == 3 and sp[0]["here"] == "missing", sp
            assert not [i for i in c["kinds"]["speech"] if i["item"].startswith(("Good evening", "0123456789.json"))]
            sg = [i for i in c["kinds"]["song"] if i["item"] == "abc"]
            assert len(sg) == 1 and sg[0]["title"] == "A song, renamed" and sg[0]["files"] == 2, c["kinds"]["song"]
            assert set(vault.restore(sp[0]["keys"])["restored"]) == set(sp[0]["keys"])
            assert (out / "speech" / "0123456789.json").is_file() and (out / "speech" / "Good evening (mine-uc).wav").is_file()
            listed = [k for its in vault.contents()["kinds"].values() for it in its for k in it["keys"]]
            assert listed and "covers/../../escape.txt" not in listed, "one bad name broke or entered the listing"
            # big files stream (a few MB of memory) and still come back bit-identical
            saved_max = vault.FLAC_MAX
            try:
                vault.FLAC_MAX = 1000
                big = out / "speech" / "big.wav"
                sf.write(big, np.tile(tone, 4), 44100, subtype="PCM_16")
                vault.run()
                obj = next((root / "objects").rglob(sha(big) + ".*"))
                assert obj.suffix in (".lzma", ".raw"), obj
                orig_big = big.read_bytes()
                big.unlink()
                key = "covers/speech/big.wav"
                vault.restore([key])
                assert big.read_bytes() == orig_big
            finally:
                vault.FLAC_MAX = saved_max
            # one run at a time: two at once both finish, and every object a kept snapshot names is there
            import threading
            (up / "two.flac").write_bytes(os.urandom(40_000))
            ths = [threading.Thread(target=vault.run) for _ in range(2)]
            for t in ths:
                t.start()
            for t in ths:
                t.join()
            for stamp in vault.snapshots(root):
                for k, v in vault.load_snapshot(root, stamp)["files"].items():
                    assert vault.find_object(root / "objects", v[0]), (stamp, k)
            # pruning: the policy, and only unreferenced objects go (the library untouched)
            stamps = ["2026-01-05T10-00-00", "2026-01-20T10-00-00", "2026-08-01T10-00-00", "2026-08-03T10-00-00",
                      "2026-10-01T10-00-00", "2026-10-02T10-00-00", "2026-10-11T09-00-00", "2026-10-12T09-00-00"]
            kept = vault.keep_which(stamps, datetime(2026, 10, 13, 12))
            assert kept == ["2026-01-20T10-00-00", "2026-08-03T10-00-00", "2026-10-02T10-00-00", "2026-10-11T09-00-00",
                            "2026-10-12T09-00-00"], kept
            orphan = root / "objects" / "ff" / ("f" * 64 + ".raw")
            orphan.parent.mkdir(exist_ok=True)
            orphan.write_bytes(b"nobody's")
            lib_before = {str(f): f.stat().st_size for d in (vd, up, out) for f in d.rglob("*") if f.is_file()}
            assert vault.prune(root) >= 1 and not orphan.exists()
            assert {str(f): f.stat().st_size for d in (vd, up, out) for f in d.rglob("*") if f.is_file()} == lib_before
            for _, k, f in vault.items("matters"):   # still all restorable
                if k in vault.load_snapshot(root)["files"]:
                    vault.get(root / "objects", vault.load_snapshot(root)["files"][k][0])
            # test mode: a vault folder outside the test's own (a sandbox inherits the user's [vault]) is refused
            # before anything is written
            outside = ROOT.parent / "zz-uc-smoke-vault"
            try:
                vault.run(folder=str(outside))
                raise AssertionError("a test-mode run wrote outside the test's folders")
            except ModelError as e:
                assert "Test mode" in str(e), e
            assert not outside.exists()
            # a check leaves nothing behind, and sizes the choice being made
            fresh = box / "fresh-drive" / "deep" / "vault"
            ok = vault.check_folder(str(fresh))
            assert ok["ok"] and not (box / "fresh-drive").exists(), "a folder check left folders behind"
            assert vault.check_folder(str(fresh), "everything")["need"] > ok["need"]
            vault.set_folder("")
            assert not vault.status()["on"]
        finally:
            CONFIG["vault"] = saved_vault
    return f"{r1['files']} files: {r1['raw'] / 1e3:.0f} kB kept in {r1['stored'] / 1e3:.0f} kB; incremental, safe, asks, prunes"


@check("doubleagent: Train tonight keeps Speaking / Singing (the queue item's mode reaches the training), a "
       "Checking it stage, the voice a finished training made in its job snapshot")
def u_da_mode():
    import inspect
    from undercover.core import jobs, worklist
    from undercover.core.jobs import Job
    from undercover.core.voices import ModelError
    from undercover.doubleagent import night, service
    from undercover.doubleagent import project as P
    with isolated_data() as box:
        old_port, old_state = jobs.PORT, worklist._STATE
        jobs.PORT, worklist._STATE = 7999, None   # a scratch queue file inside the test's folder
        real_train = service.train_new_voice
        try:
            worklist.add("new", "Smoke Test Singer", mode="singing")
            worklist.add("new", "Smoke Test Talker")
            q = {i["target"]: i for i in worklist._state()["queue"]}
            assert q["Smoke Test Singer"]["mode"] == "singing" and "mode" not in q["Smoke Test Talker"], q
            for kind, mode, words in (("new", "humming", "Speaking or Singing"), ("improve", "singing", "no Speaking")):
                try:
                    worklist.add(kind, "Smoke Test Other", mode=mode)
                    raise AssertionError(f"{kind} took mode {mode}")
                except ModelError as e:
                    assert words in str(e), e
            # the queue's run passes the item's mode on to the training
            got = {}

            def fake(job, person, control, push, **kw):
                got.update(kw, person=person)
                return "smoke-test-singer-uc"
            service.train_new_voice = fake
            night._new(Job("zz-smoke"), q["Smoke Test Singer"])
            assert got.get("mode") == "singing" and got["person"] == "Smoke Test Singer", got
            # a new project starts in the chosen mode, and the choice is applied to an existing one (apply_mode)
            proj = service.project_for("Smoke Test Singer", "singing")
            assert proj.mode == "singing", proj.mode
            talker = service.project_for("Smoke Test Talker", "speaking")
            assert service.apply_mode(talker, "Smoke Test Talker", "both").mode == "both", "nothing searched: the mode follows"
            from undercover.doubleagent import sources as srcmod
            real_search = srcmod.search
            sung = P.Source(id="sung1", title="Smoke Test Talker - A Song (Official Audio)", url="https://example.invalid/1",
                            channel="x", duration=200.0)
            srcmod.search = lambda person, mode, **kw: [sung] if mode == "singing" else []
            try:
                spoken = P.Project.load(talker.name)
                spoken.mode, spoken.sources = "speaking", [P.Source(id="talk1", title="Interview", url="https://example.invalid/2",
                                                                    channel="x", duration=600.0)]
                spoken.save()
                got = service.apply_mode(spoken, "Smoke Test Talker", "singing")   # speech so far: their songs are added
                assert got.mode == "singing" and [s.id for s in got.sources] == ["talk1", "sung1"], (got.mode, got.sources)
                assert service.apply_mode(got, "Smoke Test Talker", "speaking").sources == got.sources, "songs were dropped"
                got.train["epoch"] = 40
                got.save_train()
                assert service.apply_mode(P.Project.load(got.name), "Smoke Test Talker", "speaking").mode == "singing", \
                    "a project that has trained changed mode"
            finally:
                srcmod.search = real_search
        finally:
            service.train_new_voice = real_train
            jobs.PORT, worklist._STATE = old_port, old_state
        # Checking it: the last save (an early stop's) is measured like the others and can win
        from undercover.doubleagent.trainer import Trainer
        tr = Trainer.__new__(Trainer)
        tr.tdir = box / "zz-train"
        tr.tdir.mkdir(parents=True)
        (tr.tdir / "e63.pth").write_bytes(b"x")
        saved = []
        tr.proj = type("Proj", (), {"save_train": lambda self: saved.append(1)})()
        tr._probe = lambda epoch, sr: {"identity": 0.91, "low": 0.8, "cents": 20.0, "content": 0.95, "test": "speech"}
        st = {"epoch": 63, "best": 50, "sr": 40000, "saves": [
            {"epoch": 50, "val": 0.3, "identity": 0.85, "low": 0.8, "cents": 21.0, "content": 0.95, "test": "speech"},
            {"epoch": 63, "val": 0.29}]}
        tr.check_last(st)
        assert st["best"] == 63 and st["saves"][1]["identity"] == 0.91 and saved, st
        tr.check_last(st)
        assert len(saved) == 1, "a measured save was measured again"
    src = inspect.getsource(service.train_new_voice)
    assert src.index("tr.check_last(st)") < src.index('push("voice", 0.98,'), "nothing checked before Adding"
    assert src.index("ACTIVE[proj.name] = ") < src.index("apply_mode(proj, person, mode)"), "the mode changed before the busy check"
    # the page's card after training reads the voice from the finished job
    a, b, c = Job("train-auto"), Job("zz-work"), Job("train-prepare")
    a.finish("done", message="ready", voice="v-auto")
    b.finish("done", message="ready", result={"voice": "v-queue", "after": None})
    c.finish("done", message="ready")
    assert (a.snapshot().get("voice"), b.snapshot().get("voice"), c.snapshot().get("voice")) == ("v-auto", "v-queue", None)
    return "mode kept and passed on, refused where it doesn't apply; Checking it at 0.975; voice in the snapshot"
@check("test mode refuses writes outside the test's folders (the real config.toml, phone.json, anywhere in the app); no module binds the config's path at import")
def u_write_guard():
    """2026-10-13: a vault check held the real config's path from an import made before its sandbox, and appended to the
    user's config.toml and phone.json. Core writes now refuse that in test mode (fsutil.guard), and the path is read at
    call time everywhere."""
    import re
    from undercover.core import config, fsutil, phone
    real = ROOT / "config.toml"
    before = {f: (f.stat().st_size, f.stat().st_mtime_ns) for f in (real, ROOT / "phone.json") if f.is_file()}
    with isolated_data() as box:
        tries = {
            "config set_values": lambda: config.set_values("ui", {"zz_smoke": "1"}, config_file=real),
            "fsutil.write_text": lambda: fsutil.write_text(ROOT / "zz-smoke-guard.txt", "no"),
            "fsutil.torch_save": lambda: fsutil.torch_save({"x": 1}, ROOT / "zz-smoke-guard.pt"),
            "phone save": lambda: (phone.state_file.__globals__["config"].__setattr__("CONFIG_FILE", real), phone._save()),
        }
        saved_cfg = config.CONFIG_FILE
        try:
            for name, fn in tries.items():
                try:
                    fn()
                    raise AssertionError(f"{name} wrote outside the test's folders")
                except (PermissionError, phone.PhoneError) as e:
                    assert "Test mode" in str(e), (name, e)
                finally:
                    config.CONFIG_FILE = saved_cfg
        finally:
            config.CONFIG_FILE = saved_cfg
            phone._FILE[0] = None
        fsutil.write_text(box / "inside.txt", "yes")   # inside its own folder a test writes as usual
        assert (box / "inside.txt").read_text(encoding="utf-8") == "yes"
    assert not (ROOT / "zz-smoke-guard.txt").exists() and not (ROOT / "zz-smoke-guard.pt").exists()
    assert {f: (f.stat().st_size, f.stat().st_mtime_ns) for f in before} == before, "a real settings file changed"
    # the root cause: a module that binds config.CONFIG_FILE at import keeps the real path when a test repoints it
    bad = [f"{f.relative_to(ROOT).as_posix()}" for f in (ROOT / "undercover").rglob("*.py")
           if re.search(r"^\s*from\s+\S*config\s+import\s+[^\n]*\bCONFIG_FILE\b", f.read_text(encoding="utf-8"), re.M)]
    assert not bad, f"these bind CONFIG_FILE at import (use config.CONFIG_FILE at call time): {bad}"
    return "config, fsutil (text, torch) and phone writes refused outside; inside works; real files unchanged; no import-time binding"


@check("game pause: asks (never assumes) when a full-screen game meets GPU work; Pause / Keep both / Don't ask; resumes only its own hold when the game closes; a job about to start waits for the answer")
def u_gamepause():
    import threading
    from undercover.core import gamepause as gp, jobs, worklist
    from undercover.core.config import CONFIG
    from undercover.core.jobs import Job
    from undercover.core.voices import ModelError
    game = {"exe": "Cyberpunk2077.exe", "title": "Cyberpunk 2077", "how": "exclusive"}
    other = {"exe": "eldenring.exe", "title": "ELDEN RING", "how": "borderless"}
    with isolated_data():
        saved = (dict(CONFIG.get("game_pause") or {}), jobs.PORT, worklist._STATE, dict(gp.STATE))
        jobs.PORT, worklist._STATE = 7999, None   # a scratch queue in the test's own cache
        CONFIG["game_pause"] = {}
        reset = lambda: gp.STATE.update(game=None, asking=None, answer=None, paused_for=None, gone_since=None, reason="")
        try:
            reset()
            # no GPU work: a game is just a game
            gp.tick(game, 0, busy=False)
            assert gp.STATE["asking"] is None
            # GPU work: ask, and keep asking with no answer: nothing is ever paused by itself
            for t in range(0, 60, 5):
                gp.tick(game, 10 + t, busy=True)
            assert gp.STATE["asking"]["exe"] == "Cyberpunk2077.exe" and not worklist._state().get("held")
            # UnderCover's own page full-screen is never a game
            reset()
            gp.tick({"exe": "chrome.exe", "title": "UnderCover — Google Chrome", "how": "busy"}, 0, busy=True)
            assert gp.STATE["asking"] is None and gp.STATE["game"] is None
            # Pause: the work holds as the game's; the game gone 15 s: it resumes
            reset()
            gp.tick(game, 0, busy=True)
            gp.answer("pause")
            st = worklist._state()
            assert st["held"] and st["held_by"] == "game" and gp.STATE["paused_for"] == "Cyberpunk2077.exe"
            assert gp.tick(None, 100, busy=False) is None and worklist._state()["held"], "resumed before the game was gone 15 s"
            assert gp.tick(None, 116, busy=False) == "resumed" and not worklist._state()["held"]
            # a hand Resume, then a hand Pause during the game: its end resumes nothing (the hold is yours)
            gp.tick(game, 200, busy=True)
            gp.answer("pause")
            worklist.resume_all()
            worklist.pause_all()
            assert gp.tick(None, 300, busy=False) is None and gp.tick(None, 316, busy=False) is None
            assert worklist._state()["held"] and worklist._state()["held_by"] == "you"
            # already paused by you when a game asks: Pause keeps the hold yours (the game's end resumes nothing)
            gp.tick(game, 330, busy=True)
            gp.answer("pause")
            assert worklist.hold() == {"held": True, "held_by": "you"} and gp.STATE["paused_for"] is None
            gp.tick(None, 340, busy=False)
            assert gp.tick(None, 356, busy=False) is None and worklist.hold()["held"]
            worklist.resume_all()
            # UnderCover restarted mid-game (its memory gone, the game's hold saved): no game for 15 s, it resumes
            worklist.pause_all(by="game")
            reset()
            assert gp.tick(None, 360, busy=False) is None and gp.tick(None, 376, busy=False) == "resumed"
            assert not worklist.hold()["held"]
            # Keep both: no more asking until that game closes; a different game asks afresh
            gp.tick(game, 400, busy=True)
            gp.answer("both")
            gp.tick(game, 405, busy=True)
            assert gp.STATE["asking"] is None and not worklist._state()["held"]
            gp.tick(other, 410, busy=True)
            assert gp.STATE["asking"]["exe"] == "eldenring.exe"
            # Don't ask for this game: kept in the (test's) config, never asked again
            gp.answer("never")
            assert "eldenring.exe" in gp.settings()["never_ask"]
            gp.tick(None, 500, busy=False)
            gp.tick(None, 516, busy=False)
            gp.tick(other, 520, busy=True)
            assert gp.STATE["asking"] is None
            gp.set_never("eldenring.exe", False)
            assert "eldenring.exe" not in gp.settings()["never_ask"]
            try:
                reset()
                gp.answer("pause")
                raise AssertionError("an answer with no game on the screen")
            except ModelError:
                pass
            # asking switched off: nothing asks, nothing waits
            gp.set_ask(False)
            reset()
            gp.tick(game, 600, busy=True)
            assert gp.STATE["asking"] is None
            assert gp.before_start(Job("zz-smoke"), sleep=lambda _s: None, look=lambda: game) == "start"
            gp.set_ask(True)
            # a queued job about to start while a game is up waits for the answer: Keep both lets it start
            reset()
            job = Job("zz-smoke")
            job.title = "Making a voice of Agent Smoke"
            out = {}
            th = threading.Thread(target=lambda: out.update(r=gp.before_start(job, sleep=lambda _s: time.sleep(0.01),
                                                                               look=lambda: game, clock=lambda: 700)))
            th.start()
            wait_for(lambda: gp.STATE["asking"], 5, every=0.02, what="the question")
            assert "waiting to start" in gp.STATE["reason"] and th.is_alive(), "the job started without an answer"
            gp.answer("both")
            th.join(5)
            assert out.get("r") == "start"
            assert any("Waiting: Cyberpunk 2077 is full-screen" in (e.get("message") or "") for e in job.events), \
                "the waiting job didn't say why"
            # ... and Pause makes it wait until the game is gone (the watcher resumes), then it starts
            reset()
            job2 = Job("zz-smoke")
            th = threading.Thread(target=lambda: out.update(r2=gp.before_start(job2, sleep=lambda _s: time.sleep(0.01),
                                                                                look=lambda: game, clock=lambda: 800)))
            th.start()
            wait_for(lambda: gp.STATE["asking"], 5, every=0.02, what="the question")
            gp.answer("pause")
            time.sleep(0.2)
            assert th.is_alive() and worklist._state()["held_by"] == "game", "it started while paused for the game"
            gp.tick(None, 900, busy=True)
            assert gp.tick(None, 916, busy=True) == "resumed"
            th.join(5)
            assert out.get("r2") == "held" and not th.is_alive()
            # cancelling a waiting job lets it go (run_job then finishes it as cancelled)
            reset()
            job3 = Job("zz-smoke")
            job3.cancel = True
            assert gp.before_start(job3, sleep=lambda _s: None, look=lambda: game) == "start"
            # the GPU worker asks the gates before every job, and says which job is on the GPU (what "busy" means)
            import inspect
            src = inspect.getsource(jobs.gpu_worker)
            assert src.index("gate(job)") < src.index("CURRENT[0] = job") < src.index("run_job(job, fn)"), "the gate isn't wired"
            assert not gp._gpu_busy()
        finally:
            CONFIG["game_pause"], jobs.PORT, worklist._STATE = saved[0], saved[1], saved[2]
            gp.STATE.clear()
            gp.STATE.update(saved[3])
    return "no work: no question; no answer: no pause; Pause / both / never; resumes only its own hold; a starting job waits"


@check("cadence reading: very short sentences are read with a neighbour (never across a paragraph), the voice conversion gets a lead-in; Agent Smoke ships a shrunk index")
def u_cadence_starts():
    """2026-10-13: "Good evening." came out as "Grieving." (the conversion, no context before the onset) and short
    sentences read alone got stray sounds ("E. Listen carefully."); measured fixes in core/cadence/render.py."""
    import inspect
    import struct
    import numpy as np
    from undercover.core.cadence import render
    S = lambda t, p=0.3: {"text": t, "pause": p}
    j = render.join_short([S("Good evening."), S("The name is Smoke, Agent Smoke."), S("Go.", 0.0)])
    assert [x["text"] for x in j] == ["Good evening. The name is Smoke, Agent Smoke. Go."], j
    j = render.join_short([S("Listen carefully.", 0.7), S("A new paragraph starts here.", 0.0)])
    assert [x["text"] for x in j] == ["Listen carefully.", "A new paragraph starts here."], "joined across a paragraph"
    j = render.join_short([S("One more thing before you go.", 0.7), S("Right.", 0.3), S("Then go.", 0.0)])
    assert [x["text"] for x in j] == ["One more thing before you go.", "Right. Then go."], j
    assert render.join_short([S("Hi.", 0.0)]) == [S("Hi.", 0.0)]
    src = inspect.getsource(render.finish)
    assert "LEAD_S" in src and "y[int(sr * LEAD_S):]" in src, "the conversion lost its lead-in"
    import make_demo_voice as mdv
    from undercover.core.engine.rvc import index as rvcindex
    with isolated_data() as box:
        f = box / "model.index"
        v = np.random.default_rng(0).normal(size=(3000, 16)).astype(np.float16)
        f.write_bytes(rvcindex.COMPACT_MAGIC + struct.pack("<qq", *v.shape) + v.tobytes())
        assert mdv.shrink_index(box, k=200) == (3000, 200)
        back = rvcindex.read_vectors(f)
        assert back.shape == (200, 16) and np.isfinite(back).all()
        assert mdv.shrink_index(box, k=500) == (200, 200), "a small index was changed"
    man = json.loads((ROOT / "undercover" / "assets" / "demo-voice" / "agent-smoke" / "manifest.json").read_text(encoding="utf-8"))
    assert {"model.pth", "model.index"} <= set(man["files"]) and man["files"]["model.index"]["size"] < 32e6, man["files"]
    return "joins short sentences inside a paragraph only; lead-in in finish; index k-means; the shipped index small"


@check("listen-back: finds missing / unsure / extra words, stretches one word only (capped, the rest untouched), comma clauses split; re-reads on by default, stretches off")
def u_listenback():
    import numpy as np
    from undercover.core.cadence import render
    from undercover.core.config import CONFIG
    from undercover.speakeasy import listenback as lb
    saved = CONFIG.get("speakeasy")
    try:
        CONFIG.pop("speakeasy", None)
        assert lb.settings() == {"listen_back": True, "stretch_words": False, "comma_lengthen": False, "tries": 3}
    finally:
        if saved is not None:
            CONFIG["speakeasy"] = saved
    assert lb.text_words("The name is Smoke, Agent Smoke.") == [("the", False), ("name", False), ("is", False),
                                                               ("smoke", True), ("agent", False), ("smoke", False)]
    W = lambda *ws: [[w, i * 0.3, i * 0.3 + 0.25, p] for i, (w, p) in enumerate(ws)]
    text = "Good evening, agent."
    assert lb.problems(text, W((" Good", .99), (" evening,", .95), (" agent.", .97))) == []
    assert lb.problems(text, W((" Good", .99), (" agent.", .97))) == ["missing 'evening'"]
    assert lb.problems(text, W((" Grieving.", .17), (" agent.", .9))) == ["missing 'good'", "missing 'evening'", "1 extra word(s)"]
    assert lb.problems(text, W((" Good", .99), (" evening,", .3), (" agent.", .97))) == ["unsure 'evening,' (0.30)"]
    sr = 24000
    t = np.arange(sr, dtype=np.float32) / sr
    x = (0.3 * np.sin(2 * np.pi * 150 * t)).astype(np.float32)
    y = lb.stretch_span(x, sr, 0.4, 0.6, 1.4)
    assert abs(len(y) - (len(x) + 0.2 * sr * 0.4)) < sr * 0.01, len(y)
    assert np.allclose(y[: int(0.4 * sr) - 400], x[: int(0.4 * sr) - 400]), "the words before were changed"
    assert np.allclose(y[-int(0.4 * sr) + 400:], x[-int(0.4 * sr) + 400:]), "the words after were changed"
    assert lb.MAX_STRETCH <= 1.5
    assert len(lb.insert_pause(x, sr, 0.5, 0.12)) == len(x) + int(0.12 * sr)
    parts = render.split_clauses([{"text": "Well, if you insist, we start tonight.", "pause": 0.3}])
    assert [q["text"] for q in parts] == ["Well,", "if you insist,", "we start tonight."] and parts[-1]["pause"] == 0.3
    return "problems found; one word stretched, the rest untouched; clauses split; re-reads on, stretches off"


def _fake_ts_set(state_file, **kw):
    """Change the fake Tailscale's state (signed in, Funnel approved...) through the fake itself: its processes change
    the same file at the same time, under its lock."""
    import subprocess
    subprocess.run([sys.executable, str(ROOT / "tests" / "fake_tailscale.py"), "set", json.dumps(kw)], check=True,
                   env={**os.environ, "FAKE_TS_STATE": str(state_file)}, timeout=30)


@check("away from home (phase 20): the hidden Tailscale walks starting -> sign-in -> approve -> ready on a fake CLI; Funnel only ever to the away listener (re-aimed after a restart); exit, off and forget clean up; a wrong download is refused")
def u_tailnet():
    import re
    from undercover.core import tailnet
    from undercover.core.voices import ModelError
    fake = ROOT / "tests" / "fake_tailscale.py"
    with isolated_data() as box:
        st = box / "fake-ts.json"
        os.environ["FAKE_TS_STATE"] = str(st)
        saved = (dict(tailnet.CMD), dict(tailnet.STATE), tailnet.bin_dir)
        tailnet.CMD.update(tailscale=[sys.executable, str(fake), "cli"], tailscaled=[sys.executable, str(fake), "daemon"])
        read = lambda: json.loads(st.read_text(encoding="utf-8")) if st.exists() else {}
        write = lambda **kw: _fake_ts_set(st, **kw)
        try:
            tailnet.STATE.update(want=True, port=7871, phase="off", auth_url=None, approve_url=None, host=None, error="")
            assert tailnet.tick() == "starting"
            wait_for(lambda: read().get("daemon"), 20, every=0.2, what="the fake daemon")
            daemon = next(c for c in read()["calls"] if c[0] == "tailscaled")
            assert "--tun=userspace-networking" in daemon and any(a.startswith("--statedir=") and str(box) in a for a in daemon), daemon
            assert tailnet.tick() == "sign-in"
            wait_for(lambda: tailnet.tick() == "sign-in" and tailnet.STATE["auth_url"], 20, every=0.2, what="the login URL")
            assert tailnet.STATE["auth_url"].startswith("https://login.tailscale.com/"), tailnet.STATE
            assert re.fullmatch(r"uc-[a-z]{6}", read()["hostname"]), "the node name isn't a random uc- name"
            up = next(c for c in read()["calls"] if c[:1] == ["up"])
            assert "--unattended" in up and "--accept-dns=false" in up, up
            write(backend="Running", signed_in=True, host="uc-fakeone.tailfake.ts.net")
            wait_for(lambda: tailnet.tick() == "approve", 20, every=0.2, what="the approval step")
            assert tailnet.STATE["approve_url"].startswith("https://login.tailscale.com/f/") and tailnet.STATE["host"] == "uc-fakeone.tailfake.ts.net"
            write(funnel_approved=True)
            wait_for(lambda: tailnet.tick() == "ready", 20, every=0.2, what="ready")
            assert read()["funnel_target"] == "http://127.0.0.1:7871", read()
            assert tailnet.snapshot()["link"] == "https://uc-fakeone.tailfake.ts.net"
            for bad in (7860, 7862, 7870):   # the PC's own UI, a home listener, any other port: never a funnel target
                try:
                    tailnet.funnel_target(bad)
                    raise AssertionError(f"funnel aimed at {bad}")
                except ModelError:
                    pass
            # after a restart the away listener may have another port: a funnel aimed elsewhere isn't "ready", it's re-aimed
            write(funnel_target="http://127.0.0.1:7999")
            assert tailnet.tick() != "ready"
            wait_for(lambda: tailnet.tick() == "ready", 20, every=0.2, what="the funnel re-aimed")
            assert read()["funnel_target"] == "http://127.0.0.1:7871", read()
            name = (tailnet.state_dir() / "uc-name").read_text(encoding="utf-8")
            # UnderCover exiting: the funnel reset and our daemon gone (none left behind pointing at a dead listener)
            daemon_proc = tailnet._P["daemon"]
            resets = read()["calls"].count(["funnel", "reset"])
            tailnet.at_exit()
            daemon_proc.wait(10)
            assert read()["calls"].count(["funnel", "reset"]) == resets + 1 and not read().get("funnel_on")
            tailnet.stop()
            r = read()
            assert r["calls"].count(["funnel", "reset"]) == resets + 2 and ["down"] in r["calls"] and not r.get("funnel_on"), r["calls"][-3:]
            assert tailnet.snapshot()["phase"] == "off" and tailnet.snapshot()["name"] == name
            daemons = sum(c[0] == "tailscaled" for c in read()["calls"])
            tailnet.forget()   # with away off: the daemon is started for the logout, then the node's keys gone
            assert sum(c[0] == "tailscaled" for c in read()["calls"]) == daemons + 1, "forget didn't start the daemon to log out"
            assert read().get("logged_out") and not tailnet.state_dir().exists(), read()
            assert tailnet._P["daemon"] is None, "forget left a daemon running"
            # a download that isn't the pinned file is refused (the checksum), and nothing is installed
            junk = box / "junk.msi"
            junk.write_bytes(os.urandom(1000))
            real = dict(tailnet.MSI)
            tailnet.MSI["url"] = junk.as_uri()
            tailnet.CMD.clear()
            tailnet.bin_dir = lambda: box / "tailscale-bin"   # never the real models/tailscale (it may hold real binaries)
            try:
                tailnet.fetch_binaries()
                raise AssertionError("a wrong MSI was used")
            except ModelError as e:
                assert "checksum" in str(e)
                # in the driver: a failed download waits FETCH_RETRY_S before the next one (never 39 MB every 2 s)
                tailnet.STATE.update(want=True, port=7871)
                tailnet._P["fetch_failed"] = 0.0
                try:
                    tailnet.tick()
                    raise AssertionError("the driver used a wrong MSI")
                except ModelError:
                    pass
                tailnet.MSI["url"] = "file:///nonexistent/never-fetched.msi"   # a second download would raise
                assert tailnet.tick() == "fetching" and tailnet._P["fetch_failed"] > 0
                tailnet.STATE["want"] = False
            finally:
                tailnet.MSI.update(real)
            assert not tailnet.have_binaries()
        finally:
            tailnet.bin_dir = saved[2]
            tailnet.CMD.clear()
            tailnet.CMD.update(saved[0])
            tailnet.STATE.clear()
            tailnet.STATE.update(saved[1])
            os.environ.pop("FAKE_TS_STATE", None)
    return "starting -> sign-in -> approve -> ready; funnel only to the away port; off, forget; wrong download refused"


@check("away tokens (phase 20): a paired phone's single-use transfer code becomes an away token; never the home one; codes expire and work once; Revoke ends both")
def u_phone_away():
    from undercover.core import phone
    from undercover.core.phone import PhoneError
    with isolated_data():
        phone._FILE[0] = None
        phone.set_enabled(True)
        code = phone.new_pairing()["code"]
        home, dev = phone.claim(code, "Test phone", "192.168.1.50")
        assert phone.device_for(home) and phone.device_for(home, away=True) is None
        phone.set_away(True)
        c = phone.away_code(dev["id"])
        assert len(c) >= 20
        away, d2 = phone.redeem_away(c)
        assert d2["id"] == dev["id"] and away != home
        assert phone.device_for(away, away=True)["id"] == dev["id"]
        assert phone.device_for(away) is None and phone.device_for(home, away=True) is None, "home and away tokens mixed"
        for bad in (c, "x" * 22, ""):   # used once already, made up, empty
            try:
                phone.redeem_away(bad)
                raise AssertionError(f"redeemed {bad!r}")
            except PhoneError:
                pass
        old = phone.away_code(dev["id"])
        phone._AWAY_CODES[phone.hash_token(old)] = (dev["id"], time.time() - 1)
        try:
            phone.redeem_away(old)
            raise AssertionError("an expired code worked")
        except PhoneError:
            pass
        phone.set_away(False)
        assert phone.device_for(away, away=True) is None, "away off, yet the away token works"
        phone.set_away(True)
        assert "away_hash" not in json.dumps(phone.devices()), "a hash in the public list"
        phone.revoke(dev["id"])
        assert phone.device_for(home) is None and phone.device_for(away, away=True) is None
        phone._FILE[0] = None
    return "code -> away token; home and away kept apart; once only; expiry; away off; Revoke ends both"


@check("overnight improve never replaces or deletes the user's voice (Internal Affairs P2-1): the retrained one waits beside it as pending, whatever the numbers; no improve over a pending one")
def u_improve_pending():
    import inspect
    from undercover.core.voices import ModelError, get_voice, voices_dir
    from undercover.doubleagent import night
    src = inspect.getsource(night._improve)
    assert "remove_voice(" not in src and "name=old.name" not in src, "_improve can still remove or overwrite a voice"
    with isolated_data():
        for name, extra in (("morgan-freeman-uc", {"trained": True, "project": "morgan-freeman"}),
                            ("morgan-freeman-uc-next", {"trained": True, "project": "morgan-freeman"})):
            d = voices_dir() / name
            d.mkdir(parents=True)
            (d / "model.pth").write_bytes(b"m-" + name.encode())
            (d / "meta.json").write_text(json.dumps({"name": name, "dir": str(d), "pth": str(d / "model.pth"),
                                                      "version": "v2", "sr": 40000, "f0": True, "extra": extra}), encoding="utf-8")
        night.mark_improved_pending("morgan-freeman-uc", "morgan-freeman-uc-next", 0.71, 0.74, 32.5)
        old, new = get_voice("morgan-freeman-uc"), get_voice("morgan-freeman-uc-next")
        assert old.extra["improved_pending"]["voice"] == "morgan-freeman-uc-next" and new.extra["pending_for"] == "morgan-freeman-uc"
        assert (old.dir / "model.pth").read_bytes() == b"m-morgan-freeman-uc", "the current voice changed"
        from undercover.core.voices import list_voices, listed_voices, voice_info
        assert [v.name for v in listed_voices()] == ["morgan-freeman-uc"], "the waiting candidate is listed as its own voice"
        assert len(list_voices()) == 2 and voice_info(new)["waiting_beside"] == "morgan-freeman-uc"
        assert voice_info(old)["waiting_beside"] is None
        from undercover.app import server
        from undercover.safehouse import library
        assert [v["name"] for v in server.voices_json()] == ["morgan-freeman-uc"], "/api/voices lists the candidate"
        assert "morgan-freeman-uc-next" not in {c["voice"] for c in night.candidates()}, "the improve picker lists the candidate"
        lib = {v["name"]: v for v in library.voices()}                 # the library shows it, beside its voice
        assert lib["morgan-freeman-uc-next"]["waiting_beside"] == "morgan-freeman-uc" and lib["morgan-freeman-uc"]["waiting_beside"] is None
        for target, word in (("morgan-freeman-uc", "already waiting"), ("morgan-freeman-uc-next", "waiting to be compared")):
            try:
                night._improve_target(target)
                raise AssertionError(f"improve queued for {target}")
            except ModelError as e:
                assert word in str(e), e
    return "pending beside, nothing replaced or removed; no improve of a pending voice or its candidate"


@check("an improved voice waiting (Internal Affairs P2-1): Keep the new one -> it takes the name, the old one set aside (not listed, not deleted), the cadence and display name carried over; Undo puts both back; Keep the current one -> the candidate set aside; Undo again")
def u_improved_keep():
    from undercover.core.voices import get_voice, list_voices, listed_voices, voices_dir
    from undercover.doubleagent import improved, night
    with isolated_data():
        def voice(name, extra, display=""):
            d = voices_dir() / name
            d.mkdir(parents=True)
            (d / "model.pth").write_bytes(b"m-" + name.encode())
            (d / "meta.json").write_text(json.dumps({"name": name, "dir": str(d), "pth": str(d / "model.pth"), "version": "v2",
                                                      "sr": 40000, "f0": True, "display": display, "extra": extra}), encoding="utf-8")
            return d
        cur = voice("morgan-freeman-uc", {"trained": True, "project": "morgan-freeman", "tts": {"ref": "ref.wav"}}, "Morgan")
        (cur / "tts").mkdir()
        (cur / "tts" / "ref.wav").write_bytes(b"cadence")
        voice("morgan-freeman-uc-next", {"trained": True, "project": "morgan-freeman"})
        night.mark_improved_pending("morgan-freeman-uc", "morgan-freeman-uc-next", 0.71, 0.74, 30)
        st = improved.state("morgan-freeman-uc")
        assert st["candidate"] == "morgan-freeman-uc-next" and st["after"] == 0.74 and not st["undo"], st
        from undercover.core import jobs as J
        busy = J.Job("cover", title="Hold On · Morgan")         # a cover reading with the candidate: Keep waits
        busy.voices = {"morgan-freeman-uc-next"}
        J.JOBS[busy.id] = busy
        other = J.Job("speech", title="Hello · Someone")         # another voice's work doesn't block it
        other.voices = {"someone-else"}
        J.JOBS[other.id] = other
        try:
            improved.keep("morgan-freeman-uc", "new")
            raise AssertionError("kept while a cover used the voice")
        except Exception as e:
            assert "using this voice" in str(e), e
        finally:
            busy.finish("done")
        assert (cur / "model.pth").read_bytes() == b"m-morgan-freeman-uc", "a refused keep moved something"
        improved.keep("morgan-freeman-uc", "new")
        other.finish("done")
        v = get_voice("morgan-freeman-uc")
        assert (v.dir / "model.pth").read_bytes() == b"m-morgan-freeman-uc-next", "the new one didn't take the name"
        assert [x.name for x in listed_voices()] == ["morgan-freeman-uc"], "the kept voice isn't listed"
        assert v.display == "Morgan" and (v.dir / "tts" / "ref.wav").read_bytes() == b"cadence" and v.extra["tts"], v.extra
        names = {x.name for x in list_voices()}
        assert names == {"morgan-freeman-uc"}, names                               # the old one isn't listed...
        aside = voices_dir() / ".aside" / v.extra["improved_previous"]["aside"]
        assert (aside / "model.pth").read_bytes() == b"m-morgan-freeman-uc"        # ...and isn't deleted
        newer = voices_dir() / "morgan-freeman-uc-next"                          # a newer improve's candidate: Undo waits
        newer.mkdir()
        try:
            improved.undo("morgan-freeman-uc")
            raise AssertionError("undo over a newer candidate")
        except Exception as e:
            assert "newer retrained" in str(e), e
        assert (v.dir / "tts" / "ref.wav").exists() and not get_voice("morgan-freeman-uc").extra.get("pending_for"), "a refused undo moved something"
        newer.rmdir()
        improved.undo("morgan-freeman-uc")
        assert improved.state("morgan-freeman-uc")["undo"] is False, "Undo offered again after an Undo"
        v, c = get_voice("morgan-freeman-uc"), get_voice("morgan-freeman-uc-next")
        assert (v.dir / "model.pth").read_bytes() == b"m-morgan-freeman-uc" and (c.dir / "model.pth").read_bytes() == b"m-morgan-freeman-uc-next"
        assert (v.dir / "tts" / "ref.wav").exists() and v.extra["improved_pending"]["voice"] == "morgan-freeman-uc-next"
        improved.keep("morgan-freeman-uc", "current")                              # the other way
        assert {x.name for x in list_voices()} == {"morgan-freeman-uc"} and not get_voice("morgan-freeman-uc").extra.get("improved_pending")
        improved.undo("morgan-freeman-uc")
        assert improved.state("morgan-freeman-uc")["undo"] is False, "Undo offered again after an Undo"
        assert {x.name for x in list_voices()} == {"morgan-freeman-uc", "morgan-freeman-uc-next"}
        assert get_voice("morgan-freeman-uc").extra["improved_pending"]["voice"] == "morgan-freeman-uc-next"
        assert [x.name for x in listed_voices()] == ["morgan-freeman-uc"], "the candidate waiting again is listed"
    return "keep new (old aside, cadence and name carried), undo; keep current (candidate aside), undo; nothing deleted"


def _voice_dir(name, extra):
    """A minimal voice folder for unit checks (a fake model; nothing loads it)."""
    from undercover.core.voices import voices_dir
    d = voices_dir() / name
    d.mkdir(parents=True)
    (d / "model.pth").write_bytes(b"m-" + name.encode())
    (d / "meta.json").write_text(json.dumps({"name": name, "dir": str(d), "pth": str(d / "model.pth"), "version": "v2",
                                              "sr": 40000, "f0": True, "extra": extra}), encoding="utf-8")
    return d


@check("an unheard retrained voice is never written over (Internal Affairs P3-2): a training's name, the export's install(overwrite=True) and an improve all refuse it; its own voice may still be retrained, and the candidate keeps waiting beside the new model")
def u_pending_guard():
    import inspect
    from types import SimpleNamespace
    from undercover.core import voices as V
    from undercover.core.config import path
    from undercover.core.voices import ModelError, voices_dir
    from undercover.doubleagent import night
    from undercover.doubleagent.api import check_voice_name

    def refused(fn, word="waiting to be heard"):
        try:
            fn()
        except ModelError as e:
            assert word in str(e), e
            return
        raise AssertionError(f"not refused ({word})")
    assert 'proj.train["voice_name"] = old.name' in inspect.getsource(night._improve), "the form's name stays the candidate's"
    with isolated_data():
        _voice_dir("morgan-freeman-uc", {"trained": True, "project": "morgan-freeman"})
        cand = _voice_dir("morgan-freeman-uc-next", {"trained": True, "project": "morgan-freeman"})
        night.mark_improved_pending("morgan-freeman-uc", "morgan-freeman-uc-next", 0.71, 0.74, 30)
        proj = SimpleNamespace(name="morgan-freeman")
        refused(lambda: check_voice_name(proj, "morgan-freeman-uc-next"))                    # this project's own candidate
        refused(lambda: check_voice_name(proj, "morgan-freeman-uc-next", replace="morgan-freeman-uc-next"))
        refused(lambda: V.install([voices_dir() / "x.pth"], "morgan-freeman-uc-next", overwrite=True))
        assert (cand / "model.pth").read_bytes() == b"m-morgan-freeman-uc-next", "the candidate was touched"
        refused(lambda: check_voice_name(proj, "morgan-freeman-uc"), "already waiting")   # P4-1: one at a time
        refused(lambda: night._improve_target("morgan-freeman-uc"), "already waiting")
        # a new model of the voice itself (a manual retrain): the candidate keeps waiting beside it
        new = path("cache_dir") / "new.pth"
        new.write_bytes(b"m-manual")
        real = V._collect, V.inspect_pth
        V._collect = lambda files, work: ([new], [])
        V.inspect_pth = lambda p: {"version": "v2", "sr": 40000, "f0": True}
        try:
            v = V.install([new], "morgan-freeman-uc", overwrite=True)
        finally:
            V._collect, V.inspect_pth = real
        assert v.extra["improved_pending"]["voice"] == "morgan-freeman-uc-next", v.extra
        assert [x.name for x in V.listed_voices()] == ["morgan-freeman-uc"], "the candidate was orphaned"
        # its folder away but its voice still waiting on it: the name stays refused
        cand.rename(voices_dir() / "_away")
        refused(lambda: check_voice_name(proj, "morgan-freeman-uc-next"))
        (voices_dir() / "_away").rename(cand)
        # an earlier candidate no longer marked on its voice: no improve over it either
        meta = json.loads((voices_dir() / "morgan-freeman-uc" / "meta.json").read_text(encoding="utf-8"))
        meta["extra"].pop("improved_pending")
        (voices_dir() / "morgan-freeman-uc" / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        refused(lambda: night._improve_target("morgan-freeman-uc"), "earlier improve")
        assert (cand / "model.pth").read_bytes() == b"m-morgan-freeman-uc-next"
    return "the candidate's name refused (even as replace, even with its folder away); install over it refused; a retrain of its voice keeps it waiting; no improve over an earlier one"


@check("estimates that mean something (Internal Affairs P3-4, qa5): a one-shot of a song already in the library is planned from that song (narrow); the countdown blends the estimate with progress (no 20 -> 184 -> 18 s); a preview's own time is known from the start")
def u_eta_narrow():
    import inspect
    import numpy as np
    import soundfile as sf
    from undercover.core.config import path
    from undercover.core.separate import SEP, file_hash
    from undercover.frontman import api as FA, oneshot, pipeline as PL
    with isolated_data():
        up = path("uploads_dir")
        up.mkdir(parents=True, exist_ok=True)
        f = up / "abc123.flac"
        sf.write(str(f), np.zeros(16000 * 200, dtype="float32"), 16000)
        (up / "abc123.json").write_text(json.dumps({"id": "abc123", "title": "Britney Spears - Toxic", "file": f.name}), encoding="utf-8")
        assert oneshot.library_song("Toxic", "Britney Spears") == f and oneshot.library_song("toxic", None) == f
        assert oneshot.library_song("Hello", None) is None and oneshot.library_song("Toxic", "Adele") is None
        import inspect as _i
        assert 'None if req["style"]' in _i.getsource(FA._auto_plan), "a version request planned from the library's song"
        far = oneshot.cover_eta("v")
        fresh = oneshot.cover_eta("v", f)                       # here, not separated yet
        root = path("cache_dir") / "stems" / file_hash(f) / Path(SEP["vocal_model"]).stem
        for d in (root, root / ("karaoke_" + Path(SEP["karaoke_model"]).stem),
                  root / ("karaoke_" + Path(SEP["karaoke_model"]).stem) / ("dereverb_" + Path(SEP["dereverb_model"]).stem)):
            d.mkdir(parents=True, exist_ok=True)
            (d / "vocals.flac").write_bytes(b"x")
        here = oneshot.cover_eta("v", f)
        assert here["song_here"] and not far["song_here"] and here["low"] < fresh["low"], (here, fresh)
        assert here["high"] <= 2 * here["low"] + 20 and here["high"] < far["high"] / 2, (here, far)
        pe = PL.preview_estimate(f, PL.CoverOptions(voice="v", quality="high", dereverb=True, karaoke=True, fit=True), 3)
        assert pe["separating"] == 0 and pe["rendering"] > 0, pe
    src = inspect.getsource(FA._start_preview)
    assert src.index("job.own_estimate") < src.index("_say_why_waiting"), "the preview's own time isn't known while it waits"
    c = PL.Countdown({"separating": 0.0, "converting": 20.0, "mixing": 5.0})
    t = 1000.0
    seq = [c.left("separating", 1.0, t)[1], c.left("converting", 0.0, t)[1], c.left("converting", 0.06, t + 12)[1],
           c.left("converting", 0.5, t + 14)[1], c.left("converting", 0.9, t + 25)[1], c.left("mixing", 0.5, t + 27)[1]]
    assert all(b - a < 8 for a, b in zip(seq, seq[1:])) and max(seq) < 40, seq   # never 184 s from a slow first second
    assert PL.blend_left(20, 30, 0.5) == 30.0 and PL.blend_left(20, 0, 0) == 20.0
    from undercover.doubleagent.trainer import train_eta   # pass 5: 4993 s said for an 8-minute run
    assert train_eta(1000, 0, 0.0, None) is None and train_eta(1000, 0, 0.0, 0.5) == 500
    assert train_eta(1000, 2, 1.0, None) is None and train_eta(1000, 5, 2.5, None) == 500
    assert train_eta(1000, 25, 25.0, 0.5) == 750 and train_eta(1000, 100, 50.0, 0.9) == 500
    return f"library song {here['low']}-{here['high']} s (not here {far['low']}-{far['high']} s); countdown {seq}"


@check("SpeakEasy says how long before Generate (Internal Affairs P3-5): GET /api/speak/estimate?text=&voice=&mode= and POST with a body give the job's own estimate")
def u_speak_estimate():
    from undercover.speakeasy import api as SA, speak
    with isolated_data():
        _voice_dir("barack-obama-uc", {"trained": True, "project": "barack-obama", "from_speech": True})
        text = "One two three. Four five six seven eight nine."

        class H:
            path = "/api/speak/estimate?text=One%20two%20three.%20Four%20five%20six%20seven%20eight%20nine.&voice=barack-obama-uc&mode=reader"
            out = err = None
            body = {"text": text, "voice": "barack-obama-uc", "mode": "reader"}

            def _json(self, obj, status=200):
                self.out = obj

            def _error(self, msg, status=400):
                self.err = msg

            def _body(self):
                return self.body
        h = H()
        assert SA.handle(h, "GET", "/api/speak/estimate") and h.out, h.err
        want = speak.eta_seconds(text, speak.plan("barack-obama-uc", cadence=False))
        assert h.out["mode"] == "reader" and h.out["estimate"] == want and h.out["sentences"] == 2, h.out
        got = dict(h.out)
        assert SA.handle(h, "POST", "/api/speak/estimate") and h.out == got, (h.out, got)
        h.path, h.out = "/api/speak/estimate?voice=nobody&text=hi", None
        SA.handle(h, "GET", "/api/speak/estimate")
        assert h.err and h.out is None, "an unknown voice gave an estimate"
    return f"reader estimate {want} s for 10 words; GET and POST agree; an unknown voice refused"


DELETERS = {   # every call that can delete a folder or file (u_no_voice_deletes): a new one is reviewed and listed here
    # the only code that deletes a VOICE folder: remove_voice, and only from these (P4-1); the user's Delete sets it
    # aside (P5-1), and deleting for good is Storage's Set aside section (remove_aside, below)
    "core/voices.py::remove_voice::rmtree::v.dir",
    "core/voices.py::main::remove_voice::a.name",                                # the CLI's remove
    # a one-shot's own candidates (rc-fix7): downloads waiting in voices/.staging for their check, never a listed voice;
    # deleted only inside the staging folder, through remove_files' allow-list
    "core/voices.py::discard_staged::remove_files",
    "safehouse/library.py::_remove::rmtree::p",                                  # Clean up's files; refuses voices/ (P6-1)
    "safehouse/library.py::remove_aside::remove_files",                          # a set-aside voice the user deletes
    "doubleagent/cadence.py::remove_pack::rmtree::v.dir / 'tts'",                # a cadence the user deletes
    # temp and work folders, caches, projects, speech parts
    "app/server.py::Handler._voice_upload::rmtree::work", "app/server.py::_clean_old_previews::rmtree::sub",
    "core/separate.py::StemSeparator.run::rmtree::self.work_dir", "core/voices.py::download::rmtree::dest",
    "core/voices.py::install::rmtree::work", "core/voices.py::download_and_install::rmtree::dl if dl.is_dir() else dl.parent",
    "core/voices.py::download_staged::rmtree::dl if dl.is_dir() else dl.parent",
    "core/ytdlp.py::stage::rmtree::d", "core/ytdlp.py::stage::rmtree::dl", "core/ytdlp.py::heal::rmtree::tmp",
    "doubleagent/cadence.py::train::rmtree::work", "doubleagent/project.py::Project.delete::rmtree::self.dir",
    "doubleagent/project.py::process_source::rmtree::path('cache_dir') / 'stems' / file_hash(raw)",
    "doubleagent/sources.py::download::rmtree::work", "frontman/remix.py::remix::rmtree::tmp",
    "frontman/songs.py::download::rmtree::work", "frontman/songs.py::remove_song::rmtree::path('cache_dir') / 'stems' / sid",
    "speakeasy/speak.py::make_speech::rmtree::pdir", "speakeasy/speak.py::_make_cadence::rmtree::pdir",
    "speakeasy/speak.py::delete_speech::rmtree::parts_dir(m['job'])",
    # core.storage itself (allow-lists, test-mode refusals) and its guarded callers
    "core/storage.py::remove_tree::rmtree::p", "core/storage.py::remove_tree::rmtree::child",
    "core/storage.py::remove_files::rmtree::p", "core/storage.py::move::rmtree::c",
    "core/storage.py::remove_tree::remove_tree::child", "core/storage.py::move::remove_tree::now",
    "app/firstrun.py::seed_voice::remove_files",
    "core/doctor.py::clear_stale::remove_files",
    "core/tailnet.py::fetch_binaries::remove_files", "core/tailnet.py::forget::remove_files",
    "core/themes.py::_drop::remove_files", "core/vault.py::prune::remove_files", "core/ytdlp.py::_drop::remove_files",
    "doubleagent/improved.py::_drop_samples::remove_files", "frontman/history.py::roll_off::remove_files",
    "safehouse/smaller.py::keep_voice::remove_files", "safehouse/smaller.py::_drop_readings::remove_files",
    "safehouse/smaller.py::undo_voice::remove_files", "safehouse/smaller.py::keep_project::remove_files",
    "safehouse/smaller.py::undo_project::remove_files", "safehouse/smaller.py::restore_full_index::remove_files",
    "safehouse/smaller.py::expire_full_indexes::remove_files", "safehouse/smaller.py::index_back::remove_files", "speakeasy/speak.py::roll_off::remove_files",
}


def _deleters() -> set:
    """Every rmtree / remove_voice / remove_tree call in the app (file::function::call::first argument) and every
    remove_files call (file::function::remove_files: storage's own allow-list guards those)."""
    import ast
    root = Path(__file__).resolve().parents[1] / "undercover"
    out = set()
    for f in sorted(root.rglob("*.py")):
        stack: list = []

        class V(ast.NodeVisitor):
            def generic_visit(self, node):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    stack.append(node.name)
                    super().generic_visit(node)
                    stack.pop()
                    return
                if isinstance(node, ast.Call):
                    fn = node.func
                    name = fn.attr if isinstance(fn, ast.Attribute) else fn.id if isinstance(fn, ast.Name) else None
                    where = f"{f.relative_to(root).as_posix()}::{'.'.join(stack) or '<module>'}::{name}"
                    if name == "remove_files":
                        out.add(where)
                    elif name in ("rmtree", "remove_voice", "remove_tree"):
                        out.add(f"{where}::{ast.unparse(node.args[0]) if node.args else ''}")
                super().generic_visit(node)
        V().visit(ast.parse(f.read_text(encoding="utf-8")))
    return out


@check("nothing deletes a voice folder but the user's own Delete (Internal Affairs P4-1): a structural scan of every rmtree / remove_voice / remove_tree / remove_files call against the reviewed list; install() has none on the voice")
def u_no_voice_deletes():
    found = _deleters()
    new, gone = sorted(found - DELETERS), sorted(DELETERS - found)
    assert not new, "new delete calls (review them: a voice folder is set aside, never deleted; then list them in DELETERS): " + "; ".join(new)
    assert not gone, "listed delete calls no longer there (drop them from DELETERS): " + "; ".join(gone)
    voice = sorted(k for k in found if "remove_voice" in k)
    return f"{len(found)} delete calls, all reviewed; remove_voice only from: {', '.join(k.split('::')[1] for k in voice)}"


@check("a run onto a voice that's already there never replaces it (Internal Affairs P4-1): queued / overnight / Do it all land as <voice>-next waiting for the blind A/B; one waits at a time; only a just-now confirmed replace takes the name, the old one set aside with Undo; a failed replace puts it back")
def u_export_lands():
    import ast
    import inspect
    import torch
    from undercover.core import fsutil, voices as V
    from undercover.core.engine.rvc import index as RI
    from undercover.core.voices import ModelError, get_voice, listed_voices, voices_dir
    from undercover.doubleagent import api as DA, improved, project as P, service, trainer as T
    # only the page's export (its "Replace my voice" confirm) may pass confirmed; the runs never do
    for mod in (service, T, DA, __import__("undercover.doubleagent.night", fromlist=["x"])):
        for node in ast.walk(ast.parse(inspect.getsource(mod))):
            if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", None)) == "export_voice":
                kws = {k.arg for k in node.keywords}
                assert "confirmed" not in kws or mod is DA, f"{mod.__name__} exports with confirmed"
    assert 'b.get("replace") is True' in inspect.getsource(DA._export)

    def refused(fn, word):
        try:
            fn()
        except ModelError as e:
            assert word in str(e), e
            return
        raise AssertionError(f"not refused ({word})")
    patches = [(torch, "load", lambda *a, **k: {}), (T, "index_feats", lambda d: torch.zeros((8, 4))),
               (RI, "write_ivf_flat", lambda p, feats: Path(p).write_bytes(b"idx")), (T, "f0_median", lambda d: None),
               (T, "f0_range", lambda d: None), (T, "shrink_exported_index", lambda d: None),
               (V, "inspect_pth", lambda p: {"version": "v2", "sr": 40000, "f0": True})]

    def collect(files, work):
        out = Path(work) / "model.pth"
        shutil.copyfile(files[0], out)
        return [out], []
    patches.append((V, "_collect", collect))
    real = [(m, n, getattr(m, n)) for m, n, _ in patches]
    for m, n, f in patches:
        setattr(m, n, f)
    try:
        with isolated_data():
            proj = P.Project.create("Morgan Freeman", "speaking")
            (proj.dir / "train").mkdir(parents=True, exist_ok=True)
            (proj.dir / "train" / "data.pt").write_bytes(b"d")
            proj.train.update(saves=[{"epoch": 5, "identity": 0.81, "val": 1.0}], best=5, voice_name="morgan-freeman-uc",
                              train_minutes=3.0, exported="morgan-freeman-uc")
            proj.save()

            def save(epoch, data):
                (proj.dir / "train" / f"e{epoch}.pth").write_bytes(data)
            cur = _voice_dir("morgan-freeman-uc", {"trained": True, "project": proj.name, "likeness": 0.7, "tts": {"ref": "ref.wav"}})
            (cur / "tts").mkdir()
            (cur / "tts" / "ref.wav").write_bytes(b"cadence")
            # 1. a queued / overnight run and Do it all (overwrite, no confirm): it waits beside the voice
            save(5, b"new-model-1")
            v = T.export_voice(P.Project.load(proj.name), name="morgan-freeman-uc", overwrite=True)
            assert v.name == "morgan-freeman-uc-next" and v.extra["pending_for"] == "morgan-freeman-uc", v.extra
            assert (cur / "model.pth").read_bytes() == b"m-morgan-freeman-uc", "the voice was replaced"
            assert get_voice("morgan-freeman-uc").extra["improved_pending"]["voice"] == "morgan-freeman-uc-next"
            assert P.Project.load(proj.name).train["waiting"]["candidate"] == "morgan-freeman-uc-next"
            assert [x.name for x in listed_voices()] == ["morgan-freeman-uc"]
            # 2. one waits at a time: another run onto the voice is refused before any work, nothing touched
            refused(lambda: DA.check_voice_name(proj, "morgan-freeman-uc"), "already waiting")
            refused(lambda: T.export_voice(P.Project.load(proj.name), name="morgan-freeman-uc", overwrite=True), "already waiting")
            # 3. the user's "Replace my voice" (confirmed): it takes the name, the old one set aside with Undo
            improved.keep("morgan-freeman-uc", "current")                       # (the waiting one decided first)
            save(5, b"new-model-2")
            from undercover.core import jobs as J
            busy = J.Job("cover", title="Hold On · Morgan")                     # a cover reading with it: the replace waits
            busy.voices = {"morgan-freeman-uc"}
            J.JOBS[busy.id] = busy
            try:
                refused(lambda: T.export_voice(P.Project.load(proj.name), name="morgan-freeman-uc", overwrite=True,
                                               confirmed=True), "using")
            finally:
                busy.finish("done")
            assert (cur / "model.pth").read_bytes() == b"m-morgan-freeman-uc"
            v = T.export_voice(P.Project.load(proj.name), name="morgan-freeman-uc", overwrite=True, confirmed=True)
            assert v.name == "morgan-freeman-uc" and (v.dir / "model.pth").read_bytes() == b"new-model-2"
            assert (v.dir / "tts" / "ref.wav").read_bytes() == b"cadence", "the cadence didn't carry over"
            rec = get_voice("morgan-freeman-uc").extra["improved_previous"]
            aside = voices_dir() / ".aside" / rec["aside"]
            assert (aside / "model.pth").read_bytes() == b"m-morgan-freeman-uc" and (aside / "meta.json.aside").exists()
            improved.undo("morgan-freeman-uc")                                  # Undo: the old one back, the new one waits
            assert "set_aside" not in get_voice("morgan-freeman-uc").extra, "its aside note stayed"
            assert (get_voice("morgan-freeman-uc").dir / "model.pth").read_bytes() == b"m-morgan-freeman-uc"
            assert (get_voice("morgan-freeman-uc-next").dir / "model.pth").read_bytes() == b"new-model-2"
            assert (get_voice("morgan-freeman-uc").dir / "tts" / "ref.wav").exists()
            improved.keep("morgan-freeman-uc", "current")
            # 4. a replace that fails half-way puts the voice back as it was
            real_write = fsutil.write_text

            def boom(p, text):
                if Path(p).name == "meta.json" and Path(p).parent.name == "morgan-freeman-uc":
                    raise OSError("disk full")
                return real_write(p, text)
            fsutil.write_text = boom
            try:
                save(5, b"new-model-3")
                try:
                    T.export_voice(P.Project.load(proj.name), name="morgan-freeman-uc", overwrite=True, confirmed=True)
                    raise AssertionError("the failing replace didn't fail")
                except OSError:
                    pass
            finally:
                fsutil.write_text = real_write
            back = get_voice("morgan-freeman-uc")
            assert (back.dir / "model.pth").read_bytes() == b"m-morgan-freeman-uc" and (back.dir / "tts" / "ref.wav").exists()
            assert any(d.name.endswith("-unfinished") for d in (voices_dir() / ".aside").iterdir()), "the half-made one"
            # 5. a new name (nothing there) is just added
            save(5, b"new-model-4")
            assert T.export_voice(P.Project.load(proj.name), name="morgan-freeman-2", overwrite=True).name == "morgan-freeman-2"
    finally:
        for m, n, f in real:
            setattr(m, n, f)
    return "queued / Do it all -> waits as -next; a second refused; confirmed replace -> old aside, Undo; failed replace -> put back"


@check("SafeHouse lists the voices set aside (never deleted, P4-1): replaced, not kept, unfinished, with sizes, dates and the Undo each serves; deleting one is the user's own choice, only inside voices/.aside, and ends that Undo")
def u_aside_list():
    from undercover.core.voices import get_voice, voices_dir
    from undercover.doubleagent import improved, night
    from undercover.safehouse import api as SA, library
    with isolated_data():
        assert library.aside() == []
        _voice_dir("morgan-freeman-uc", {"trained": True, "project": "morgan-freeman"})
        _voice_dir("morgan-freeman-uc-next", {"trained": True, "project": "morgan-freeman"})
        night.mark_improved_pending("morgan-freeman-uc", "morgan-freeman-uc-next", 0.71, 0.74, 30)
        improved.keep("morgan-freeman-uc", "new")                       # the old one goes aside
        odd = voices_dir() / ".aside" / "someone-20261001-010101-unfinished"
        odd.mkdir(parents=True)
        (odd / "model.pth").write_bytes(b"x" * 10)
        from undercover.core.voices import set_aside
        _voice_dir("morgan-freeman-uc-try", {"trained": True})
        tried = {"recipe": "refine", "minutes": 11.9, "terms": {"likeness": True, "harmonicity": False}}
        attempt = set_aside(voices_dir() / "morgan-freeman-uc-try", note={"why": "Improve tried, no improvement: x",
                                                                          "display": "Morgan Freeman: Improve's attempt", "measures": tried})
        items = {x["id"]: x for x in library.aside()}
        assert items[attempt.name]["measures"] == tried, items[attempt.name]     # Deep Cover shows an attempt's numbers (Q, smallfix7)
        kept = next(x for x in items.values() if x["undo_for"])
        assert kept["voice"] == "morgan-freeman-uc" and kept["size"] > 0 and "kept" in kept["why"], kept
        assert kept["measures"] is None, kept
        assert kept["display"] == "Morgan Freeman", kept   # from its meta (set aside by a Keep, no note)
        assert items[odd.name]["why"] == "a replace that didn't finish" and items[odd.name]["undo_for"] is None
        assert items[odd.name]["at"] == "2026-10-01 01:01", items[odd.name]   # from its stamp, not the folder's time

        class H:
            path, out, err = "/api/library/aside", None, None

            def _json(self, obj, status=200):
                self.out = obj

            def _error(self, msg, status=400):
                self.err = (msg, status)
        h = H()
        assert SA.handle(h, "GET", "/api/library/aside") and {x["id"] for x in h.out} == set(items)
        for bad in ("..", "x/../..", "nope"):
            h.err = None
            SA.handle(h, "DELETE", f"/api/library/aside/{bad}")
            assert h.err, f"{bad} accepted"
        assert get_voice("morgan-freeman-uc").dir.exists()
        SA.handle(h, "DELETE", f"/api/library/aside/{kept['id']}")
        assert h.out["undo_ended_for"] == "morgan-freeman-uc" and h.out["freed"] > 0, h.out
        assert not (voices_dir() / ".aside" / kept["id"]).exists()
        assert not improved.state("morgan-freeman-uc"), "an Undo with nothing behind it"
        assert (get_voice("morgan-freeman-uc").dir / "model.pth").read_bytes() == b"m-morgan-freeman-uc-next", "the voice was touched"
        library.remove_aside(odd.name)
        library.remove_aside(attempt.name)
        assert library.aside() == []
    return "listed with why / size / Undo / an attempt's measures; bad ids refused; delete frees it and ends its Undo; the voice untouched"


@check("the user's Delete sets a voice aside, never deletes it (Internal Affairs P5-1): a retrained one waiting goes with it; Bring it back restores both; refused while a job reads it, when the name is taken, and for one an Undo serves")
def u_remove_aside():
    import inspect
    from undercover.app import server
    from undercover.core import jobs as J
    from undercover.core.voices import ModelError, get_voice, listed_voices, remove_to_aside, voices_dir
    from undercover.doubleagent import improved, night
    from undercover.safehouse import api as SA, library
    assert "remove_to_aside(" in inspect.getsource(server.Handler.do_DELETE)

    class H:
        path, out, err = "/api/library/aside", None, None

        def _json(self, obj, status=200):
            self.out = obj

        def _error(self, msg, status=400):
            self.err = msg

        def _body(self):
            return {}
    with isolated_data():
        _voice_dir("morgan-freeman-uc", {"trained": True, "project": "morgan-freeman"})
        _voice_dir("morgan-freeman-uc-next", {"trained": True, "project": "morgan-freeman"})
        night.mark_improved_pending("morgan-freeman-uc", "morgan-freeman-uc-next", 0.71, 0.74, 30)
        busy = J.Job("cover", title="Hold On · Morgan")
        busy.voices = {"morgan-freeman-uc"}
        J.JOBS[busy.id] = busy
        try:
            remove_to_aside("morgan-freeman-uc")
            raise AssertionError("removed while a cover read it")
        except ModelError as e:
            assert "using" in str(e), e
        finally:
            busy.finish("done")
        r = remove_to_aside("morgan-freeman-uc")                 # what a double-click on Remove does now
        assert r["with"] and listed_voices() == [] and not (voices_dir() / "morgan-freeman-uc").exists()
        items = {x["id"]: x for x in library.aside()}
        assert items[r["aside"]]["why"] == "you removed it" and "when you removed it" in items[r["with"]]["why"], items
        assert items[r["aside"]]["display"] == "Morgan Freeman", items[r["aside"]]   # its name, not its id (qa10)
        assert (voices_dir() / ".aside" / r["aside"] / "model.pth").read_bytes() == b"m-morgan-freeman-uc", "not kept"
        h = H()
        SA.handle(h, "POST", f"/api/library/aside/{r['aside']}/restore")    # Bring it back: both, still a pair
        assert h.out == {"voice": "morgan-freeman-uc", "with": "morgan-freeman-uc-next"}, (h.out, h.err)
        assert [v.name for v in listed_voices()] == ["morgan-freeman-uc"] and library.aside() == []
        assert get_voice("morgan-freeman-uc").extra["improved_pending"]["voice"] == "morgan-freeman-uc-next"
        assert "set_aside" not in get_voice("morgan-freeman-uc").extra
        # the name taken meanwhile: refused, nothing moved
        improved.keep("morgan-freeman-uc", "current")
        r = remove_to_aside("morgan-freeman-uc")
        _voice_dir("morgan-freeman-uc", {"trained": True, "project": "other"})
        h.err = None
        SA.handle(h, "POST", f"/api/library/aside/{r['aside']}/restore")
        assert h.err and "in your voices now" in h.err and (voices_dir() / ".aside" / r["aside"]).is_dir(), h.err
        # its waiting one can't come back (its name taken): the voice comes back without a "waiting" pointing at it
        _voice_dir("bo-uc", {"trained": True, "project": "bo"})
        _voice_dir("bo-uc-next", {"trained": True, "project": "bo"})
        night.mark_improved_pending("bo-uc", "bo-uc-next", 0.7, 0.7, 10)
        rb = remove_to_aside("bo-uc")
        _voice_dir("bo-uc-next", {"trained": True, "project": "someone-else"})        # the name taken meanwhile
        assert library.restore_aside(rb["aside"]) == {"voice": "bo-uc", "with": None}
        assert not get_voice("bo-uc").extra.get("improved_pending"), "waiting on a voice that isn't its candidate"
        held = next(x for x in library.aside() if x["id"] != r["aside"] and "didn't keep" in x["why"])
        assert held["undo_for"] is None, held   # its voice was removed: the record (in .aside) still says why
        # one an Undo serves goes back through that Undo
        _voice_dir("ann-uc", {"trained": True, "project": "ann"})
        _voice_dir("ann-uc-next", {"trained": True, "project": "ann"})
        night.mark_improved_pending("ann-uc", "ann-uc-next", 0.7, 0.7, 10)
        improved.keep("ann-uc", "current")
        served = next(x for x in library.aside() if x["undo_for"] == "ann-uc")
        h.err = None
        SA.handle(h, "POST", f"/api/library/aside/{served['id']}/restore")
        assert h.err and "Use Undo" in h.err, h.err
        h.err = None
        SA.handle(h, "POST", "/api/library/aside/nope/restore")
        assert h.err, "an unknown id restored"
    return "Remove -> aside (with its waiting one); Bring it back -> both; busy / name taken / unknown refused"


@check("Clean up's 'the same voice installed twice' never deletes a voice (Internal Affairs P6-1): the copy goes to Set aside, and the one kept is the one with a cadence, the project's voice, a display name, records, more covers; _remove refuses anything in the voices folder; no group lists a voice path")
def u_duplicate_aside():
    from undercover.core import storage
    from undercover.core.voices import ModelError, get_voice, listed_voices, voices_dir
    from undercover.doubleagent import project as P
    from undercover.safehouse import library
    with isolated_data():
        orig = _voice_dir("spongebob-uc", {"trained": True, "project": "spongebob"})
        (orig / "tts").mkdir()
        copy = _voice_dir("spongebob-copy", {})
        (copy / "tts").mkdir()                                     # the copy has a cadence marker and a name too
        meta = json.loads((copy / "meta.json").read_text(encoding="utf-8"))
        meta["display"] = "SpongeBob (my cadence copy)"
        (copy / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        (copy / "model.pth").write_bytes((orig / "model.pth").read_bytes())
        proj = P.Project.create("SpongeBob", "speaking")
        proj.train["exported"] = "spongebob-uc"
        proj.save()
        # a pair where only the newer one has a cadence: it's the one kept
        a = _voice_dir("ann-uc", {})
        b = _voice_dir("ann-two", {})
        (b / "tts").mkdir()
        (b / "tts" / "ref.wav").write_bytes(b"cadence")
        (b / "model.pth").write_bytes((a / "model.pth").read_bytes())
        # and an empty tts/ folder isn't a cadence (pass 7): the older install is kept over it
        for name, when in (("bea-uc", "2026-01-01 10:00"), ("bea-two", "2026-02-01 10:00")):
            vd = _voice_dir(name, {})
            m = json.loads((vd / "meta.json").read_text(encoding="utf-8"))
            m["installed"] = when
            (vd / "meta.json").write_text(json.dumps(m), encoding="utf-8")
        (voices_dir() / "bea-two" / "tts").mkdir()
        (voices_dir() / "bea-two" / "model.pth").write_bytes((voices_dir() / "bea-uc" / "model.pth").read_bytes())
        # the demo voice is always kept, even against a named copy of it with a cadence
        from undercover.app import firstrun
        from undercover.core.voices import DEMO_VOICE
        assert firstrun.DEMO.name == DEMO_VOICE
        demo = _voice_dir(DEMO_VOICE, {"demo": True})
        mine = _voice_dir("my-smoke", {"demo": True})
        (mine / "tts").mkdir()
        meta = json.loads((mine / "meta.json").read_text(encoding="utf-8"))
        meta["display"] = "My Smoke"
        (mine / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        (mine / "model.pth").write_bytes((demo / "model.pth").read_bytes())
        g = library._cleanup_groups()
        for k, v in g.items():
            assert not any(storage._inside(Path(x), voices_dir()) for x in v["paths"]), f"{k} lists a voice path"
        d = {x["voice"]: x for x in g["duplicate_voices"]["aside"]}
        assert d.pop("my-smoke")["keeps"] == DEMO_VOICE and DEMO_VOICE not in d, d
        (mine / "model.pth").write_bytes(b"its own now")                 # (out of the way for the rest)
        g = library._cleanup_groups()
        d = {x["voice"]: x for x in g["duplicate_voices"]["aside"]}
        assert d.get("bea-two", {}).get("because") == "it was installed first", d
        d.pop("bea-two")
        (voices_dir() / "bea-two" / "model.pth").write_bytes(b"its own")
        g = library._cleanup_groups()                                   # (without that pair)
        assert set(d) == {"spongebob-copy", "ann-uc"}, d
        assert d["spongebob-copy"]["keeps"] == "spongebob-uc" and "project" in d["spongebob-copy"]["because"], d
        assert d["ann-uc"]["keeps"] == "ann-two" and "cadence" in d["ann-uc"]["because"], d
        assert "SpongeBob (my cadence copy) (spongebob-copy) goes to Set aside" in g["duplicate_voices"]["detail"]
        assert g["duplicate_voices"]["count"] == 2 and g["duplicate_voices"]["size"] == 0 and g["duplicate_voices"]["aside_size"] > 0
        from undercover.core import jobs as J
        busy = J.Job("cover", title="Hold On · Ann")                       # one in use stays, said so
        busy.voices = {"ann-uc"}
        J.JOBS[busy.id] = busy
        r = library.clean_up({"duplicate_voices"})
        busy.finish("done")
        assert [x["voice"] for x in r["skipped"]] == ["ann-uc"] and "using" in r["skipped"][0]["why"], r
        assert [x["voice"] for x in r["set_aside"]] == ["spongebob-copy"], r
        r = library.clean_up({"duplicate_voices"})
        r["set_aside"].append({"voice": "spongebob-copy"})
        assert r["freed"] == 0 and {x["voice"] for x in r["set_aside"]} == {"spongebob-copy", "ann-uc"}, r
        assert {v.name for v in listed_voices()} == {"spongebob-uc", "ann-two", "agent-smoke", "my-smoke", "bea-uc", "bea-two"}
        assert (get_voice("spongebob-uc").dir / "tts").is_dir(), "the original was touched"
        items = {x["voice"]: x for x in library.aside()}
        assert items["spongebob-copy"]["why"] == "the same model as Spongebob", items["spongebob-copy"]
        assert items["spongebob-copy"]["display"] == "SpongeBob (my cadence copy)"
        for p_ in (voices_dir() / "spongebob-uc", voices_dir() / ".aside"):
            try:
                library._remove(p_)
                raise AssertionError(f"_remove took {p_}")
            except ModelError as e:
                assert "never deletes" in str(e), e
        assert get_voice("spongebob-uc").dir.exists() and (voices_dir() / ".aside").is_dir()
    return "the project's voice kept over a named copy; a cadence kept over an older install; copies set aside; _remove refuses voices"


@check("Storage's folder move is transactional (Internal Affairs pass 7): a failure after the setting switched puts the setting back FIRST, then drops the new copy (or moves the folders back on the same drive); nothing the setting points at is removed")
def u_move_rollback():
    from undercover.core import config, relocate, storage

    class FakeJob:
        id, cancel = "smoke", False

        def push(self, **ev):
            pass
    with isolated_data() as t:
        app = t / "app"
        for d in ("uploads", "output", "models/voices/v1"):
            (app / d).mkdir(parents=True, exist_ok=True)
        (app / "uploads/s.mp3").write_bytes(b"s" * 3000)
        (app / "models/voices/v1/meta.json").write_text(json.dumps({"dir": str(app / "models/voices/v1")}), encoding="utf-8")
        before = {k: str(config.path(k)) for k in ("uploads_dir", "output_dir", "voices_dir")}
        real_rw, real_check = relocate.rewrite, storage.check

        def boom(old, new, **kw):   # the switch happened; rewriting the stored paths fails
            if Path(new).is_relative_to(t / "lib2") or Path(new).is_relative_to(t / "lib3"):
                raise OSError("disk full")
            return real_rw(old, new, **kw)
        relocate.rewrite = boom
        try:
            for dest, other_drive in ((t / "lib2", True), (t / "lib3", False)):
                storage.check = (lambda g, f: {**real_check(g, f), "same_drive": False}) if other_drive else real_check
                try:
                    storage.move("library", str(dest), FakeJob())
                    raise AssertionError("the injected failure didn't fail the move")
                except OSError:
                    pass
                after = {k: str(config.path(k)) for k in before}
                assert after == before, (other_drive, after, before)          # the setting is back
                assert (app / "uploads/s.mp3").read_bytes() == b"s" * 3000, "the original went"
                assert json.loads((app / "models/voices/v1/meta.json").read_text(encoding="utf-8"))["dir"] == str(app / "models/voices/v1")
                assert not (dest / "uploads").exists(), f"the new place kept files ({other_drive})"
        finally:
            relocate.rewrite, storage.check = real_rw, real_check
        # a failure after the switch completed (removing the old copy): the new place is kept, nothing undone
        real_rt = storage.remove_tree
        storage.check = lambda g, f: {**real_check(g, f), "same_drive": False}
        storage.remove_tree = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("late"))
        try:
            try:
                storage.move("library", str(t / "lib5"), FakeJob())
                raise AssertionError("the late failure didn't fail the move")
            except RuntimeError:
                pass
        finally:
            storage.remove_tree, storage.check = real_rt, real_check
        assert (t / "lib5/uploads/s.mp3").exists() and storage._inside(config.path("uploads_dir"), t / "lib5"), "the new copy went"
        storage.move("library", str(t / "lib4"), FakeJob())                    # and it still moves when nothing fails
        assert (t / "lib4/uploads/s.mp3").exists() and storage._inside(config.path("uploads_dir"), t / "lib4")
    return "copy (other drive) and move (same drive): setting back first, the original whole, the new place empty"


@check("the last set-aside gaps (Internal Affairs pass 7): a 30-day full index goes only when its project can rebuild it, else to Set aside (and back)")
def u_aside_gaps():
    import time as _t
    from undercover.core.engine.rvc.index import EXPORT_ASIDE, EXPORT_RECORD
    from undercover.core.voices import voices_dir
    from undercover.safehouse import library, smaller
    with isolated_data():
        # a full index past its 30 days: its project is gone -> set aside, listed, and Bring it back restores it
        v = _voice_dir("zed-uc", {"trained": True, "project": "zed"})
        (v / "model.index").write_bytes(b"10k")
        (v / EXPORT_ASIDE).write_bytes(b"full")
        (v / EXPORT_RECORD).write_text(json.dumps({"until": _t.time() - 10}), encoding="utf-8")
        assert smaller.expire_full_indexes() == ["zed-uc"]
        assert not (v / EXPORT_ASIDE).exists() and (v / "model.index").read_bytes() == b"10k"
        row = next(x for x in library.aside() if x["voice"] == "zed-uc")
        assert "full search index" in row["display"] and "project is gone" in row["why"], row
        assert library.restore_aside(row["id"]) == {"voice": "zed-uc", "with": None}
        assert (v / "model.index").read_bytes() == b"full" and not (voices_dir() / ".aside" / row["id"]).exists()
        # with its project still able to rebuild it, it's removed as before
        from undercover.doubleagent.project import projects_dir
        (projects_dir() / "zed" / "train").mkdir(parents=True)
        (projects_dir() / "zed" / "train" / "data.pt").write_bytes(b"d")
        (v / EXPORT_ASIDE).write_bytes(b"full2")
        (v / EXPORT_RECORD).write_text(json.dumps({"until": _t.time() - 10}), encoding="utf-8")
        assert smaller.expire_full_indexes() == ["zed-uc"] and not (v / EXPORT_ASIDE).exists()
        assert not [x for x in library.aside() if x["voice"] == "zed-uc"]
    return "full index: project gone -> aside + back; project there -> removed"


@check("a folder copy of a voice doesn't take the original's waiting candidate when it's set aside (Internal Affairs P7-1)")
def u_p7_1():
    from undercover.core.voices import get_voice, remove_to_aside, voices_dir
    from undercover.doubleagent import night
    with isolated_data():
        _voice_dir("morgan-freeman-uc", {"trained": True, "project": "morgan-freeman"})
        _voice_dir("morgan-freeman-uc-next", {"trained": True, "project": "morgan-freeman"})
        night.mark_improved_pending("morgan-freeman-uc", "morgan-freeman-uc-next", 0.71, 0.74, 30)
        meta = json.loads((voices_dir() / "morgan-freeman-uc" / "meta.json").read_text(encoding="utf-8"))
        cp = voices_dir() / "morgan-copy"
        cp.mkdir()
        (cp / "model.pth").write_bytes(b"m")
        (cp / "meta.json").write_text(json.dumps({**meta, "name": "morgan-copy", "dir": str(cp), "pth": str(cp / "model.pth")}), encoding="utf-8")
        r = remove_to_aside("morgan-copy")
        assert r["with"] is None and (voices_dir() / "morgan-freeman-uc-next" / "meta.json").exists(), r
        assert get_voice("morgan-freeman-uc").extra["improved_pending"]["voice"] == "morgan-freeman-uc-next"
    return "the copy went aside alone; the original's candidate still waits beside it"


@check("a speaking voice gets its cadence pack after it's made (principle 10, smallfix1): Do it all and a queued new voice chain it; a singing voice, a waiting -next and a voice with a pack don't; newcad doesn't run it twice; a voice kept with no pack gets one queued")
def u_cadence_chain():
    import inspect
    from undercover.core import jobs as J, worklist
    from undercover.core.voices import get_voice
    from undercover.doubleagent import api as DA, cadence, improved, night, service
    assert "cadence.chain_cadence(job, name, push, control)" in inspect.getsource(DA.start_auto), "Do it all doesn't chain it"
    real = cadence.can_train, cadence.train, service.train_new_voice
    calls = []
    cadence.can_train = lambda v: (True, "from the voice's clips")
    cadence.train = lambda job, name, push, control=None: calls.append(name) or {"note": "Cadence made."}
    try:
        with isolated_data():
            worklist._STATE = None
            _voice_dir("kris-uc", {"trained": True, "project": "kris", "from_speech": True})
            _voice_dir("sing-uc", {"trained": True, "project": "sing", "from_speech": False})
            _voice_dir("kris-uc-next", {"trained": True, "project": "kris", "from_speech": True, "pending_for": "kris-uc"})
            packed = _voice_dir("talk-uc", {"trained": True, "project": "talk", "from_speech": True, "tts": {"ref": "ref.wav"}})
            (packed / "tts").mkdir()
            (packed / "tts" / "ref.wav").write_bytes(b"cadence")
            want = {"kris-uc": True, "sing-uc": False, "kris-uc-next": False, "talk-uc": False}
            got = {n: cadence.cadence_after(get_voice(n))[0] for n in want}
            assert got == want, got
            assert "singing" in cadence.cadence_after(get_voice("sing-uc"))[1]
            # the queue's "new": a speaking voice chains it, a singing one doesn't, "newcad" leaves it to its own step
            for made, kind, n in (("kris-uc", "new", 1), ("sing-uc", "new", 1), ("kris-uc", "newcad", 1)):
                service.train_new_voice = lambda *a, made=made, **k: made
                job = J.Job("night")
                r = night._new(job, {"kind": kind, "target": "Kris", "mode": "speaking"})
                assert len(calls) == n and (("cadence" in r) == (made == "kris-uc" and kind == "new")), (made, kind, r, calls)
            # a Pause or Stop during the cadence: the item ends done (the voice is made), never run again
            service.train_new_voice = lambda *a, **k: "kris-uc"
            for exc, word, queued in ((J.Paused, "Paused before its cadence", True), (J.Cancelled, "Stopped before", False)):
                def stop(job, name, push, control=None, exc=exc):
                    raise exc()
                cadence.train = stop
                worklist._state()["queue"].clear()
                worklist._save()
                for kind in ("new", "newcad"):
                    r = night._new(J.Job("night"), {"kind": kind, "target": "Kris", "mode": "speaking"}) if kind == "new" \
                        else cadence._run_new(J.Job("night"), {"kind": kind, "target": "Kris"})
                    assert r["voice"] == "kris-uc" and word in r["note"], (kind, r)
                q = [i for i in worklist._state()["queue"] if i["kind"] == "cadence" and i["target"] == "kris-uc"]
                assert bool(q) == queued, (exc, q)
            cadence.train = lambda job, name, push, control=None: calls.append(name) or {"note": "Cadence made."}
            worklist._state()["queue"].clear()
            worklist._save()
            est = night.estimate({"kind": "new", "target": "Kris", "mode": "speaking"})
            assert est["cadence"] == cadence.CADENCE_MIN and "cadence" not in night.estimate({"kind": "new", "target": "Kris"})
            assert "then Kris's cadence" in worklist.KINDS["new"]["label"]({"target": "Kris", "mode": "speaking"})
            # Keep the retrained one, the old one with no pack: a cadence is queued for it
            night.mark_improved_pending("kris-uc", "kris-uc-next", 0.7, 0.72, 10)
            r = improved.keep("kris-uc", "new")
            assert r["queued"] == "cadence" and any(i["kind"] == "cadence" and i["target"] == "kris-uc"
                                                     for i in worklist._state()["queue"]), r
            worklist._STATE = None
    finally:
        cadence.can_train, cadence.train, service.train_new_voice = real
        worklist._STATE = None
    return "speaking chains, singing / waiting / packed don't; newcad once; Keep with no pack queues one"


@check("queue packing (smallfix1): the first item that finishes before finish_by starts, not only the first in line; a cadence never runs before its own voice's item, wherever it sits")
def u_queue_pack():
    from datetime import datetime, timedelta
    from undercover.core import worklist
    import undercover.doubleagent.cadence  # noqa: F401 - registers cadence (follows its voice)
    real = worklist.estimate
    worklist.estimate = lambda i: {"total": i["_est"]}
    try:
        now = datetime(2026, 10, 15, 1, 0)
        dl = now + timedelta(minutes=120)

        def q(*items):
            return [{"id": str(n), "state": "queued", **it} for n, it in enumerate(items)]
        big, small = {"kind": "new", "target": "Big", "_est": 300}, {"kind": "new", "target": "Small", "_est": 60}
        assert worklist.pick(q(big, small), now, dl)["target"] == "Small"          # the long one waits; the short one runs
        assert worklist.pick(q(big, small), now, None)["target"] == "Big"          # no deadline: in order
        assert worklist.pick(q(big), now, dl) is None
        kris, kcad = {"kind": "new", "target": "Kristen Schaal", "_est": 300}, {"kind": "cadence", "target": "kristen-schaal-uc", "_est": 10}
        assert worklist.pick(q(kris, kcad), now, dl) is None                        # its voice doesn't fit: nor does it
        assert worklist.pick(q(kcad, {**kris, "_est": 60}), now, dl)["kind"] == "new"   # before it in line: still after
        paused = q(kris, kcad)
        paused[0]["state"] = "paused"
        assert worklist.pick(paused, now, dl) is None                               # its voice paused: it waits
        done = q(kris, kcad)
        done[0]["state"] = "done"
        assert worklist.pick(done, now, dl)["kind"] == "cadence"                    # its voice done: it runs
        retry = q({**small, "retry_after": 9e12}, {"kind": "improve", "target": "x-uc", "_est": 30})
        assert worklist.pick(retry, now, dl)["kind"] == "improve"
    finally:
        worklist.estimate = real
    return "short item past a long one; no deadline in order; cadence after its voice (queued, paused, done); retry_after"


@check("Train tonight instead while that voice trains live isn't both (smallfix2): adding it pauses the live job and moves it to tonight's queue from its resume point; one queue item per person; a voice made from the queue right now is refused")
def u_queue_live():
    from undercover.core import jobs as J, worklist
    from undercover.core.voices import ModelError
    from undercover.doubleagent import api as DA, night, project as P
    import undercover.doubleagent.cadence  # noqa: F401 - registers newcad

    def refused(fn, word):
        try:
            fn()
        except ModelError as e:
            assert word in str(e), e
            return
        raise AssertionError(f"not refused ({word})")
    with isolated_data():
        worklist._STATE = None
        proj = P.Project.create("Plankton from SpongeBob", "speaking")
        proj.phrases = [P.Phrase(id="p1", source="s", start=0.0, dur=1.0)]   # something to train on
        proj.save()
        job = J.Job("train-auto")
        job.resume_spec = {"kind": "auto", "project": proj.name, "label": "Making a voice of Plankton"}

        class Ctl:
            pause = stop = False
        DA.ACTIVE[proj.name] = {"job": job, "control": Ctl(), "kind": "auto"}
        try:
            out = worklist.add("new", "plankton from spongebob", mode="speaking")   # what #da-tonight sends
            assert "moved" in out and job.pausing, out
            assert not [i for i in worklist._state()["queue"] if i["kind"] == "new"], "queued as well as live"
            job.finish("paused", message="Paused.")                              # the training stops at its next step
            worklist.remember(job)                                               # (jobs.run_job does this)
        finally:
            DA.ACTIVE.pop(proj.name, None)
        q = [i for i in worklist._state()["queue"] if i["state"] == "queued"]
        assert [(i["kind"], i["target"]) for i in q] == [("project", proj.name)], q
        assert not worklist._state()["resume"] and not worklist.held(), worklist._state()
        # one item per person, whichever kind: nothing running now
        refused(lambda: worklist.add("new", "Plankton from SpongeBob"), "already in the queue")
        refused(lambda: worklist.add("newcad", "plankton from spongebob"), "already in the queue")
        worklist.add("newcad", "Kristen Schaal")
        refused(lambda: worklist.add("new", "kristen schaal", mode="speaking"), "already in the queue")
        # the voice being made from the queue right now: refused, not paused
        night_job = J.Job("night")
        DA.ACTIVE[proj.name] = {"job": night_job, "control": None, "kind": "auto"}
        try:
            refused(lambda: worklist.add("new", "Plankton from SpongeBob"), "from tonight's queue right now")
            assert not night_job.pausing
        finally:
            DA.ACTIVE.pop(proj.name, None)
        worklist._STATE = None
    return "live Do it all -> paused + moved as its project item; no duplicate; one per person; a queue run refused"


@check("starting the queue goes on with its paused items first (smallfix2, ARCHITECTURE known bug 'Paused queue item is skipped'): arming turns them back to queued, in queue order")
def u_arm_paused():
    from undercover.core import worklist
    with isolated_data():
        worklist._STATE = None
        st = worklist._state()
        st["queue"] = [{"id": "a", "kind": "new", "target": "A", "state": "done"},
                       {"id": "b", "kind": "new", "target": "B", "state": "paused"},
                       {"id": "c", "kind": "new", "target": "C", "state": "queued"},
                       {"id": "d", "kind": "new", "target": "D", "state": "paused"}]
        st["held"] = True
        worklist.arm("02:00")
        q = worklist._state()["queue"]
        assert [i["state"] for i in q] == ["done", "queued", "queued", "queued"] and [i["id"] for i in q] == list("abcd"), q
        assert worklist._state()["armed"] and not worklist.held()
        worklist._STATE = None
    return "paused B and D queued again in place; armed, not held"


@check("a one-shot asks which voice when downloaded voices match (smallfix3, 'Ask me each time'): use one now / check against real clips / train a better one, each with its time; /api/auto takes voice_choice and does exactly that; candidate ids listed once; the check's own countdown")
def u_voice_ask():
    import inspect
    from undercover.core.voices import ModelError, get_voice
    from undercover.frontman import ambiguity, api as FA, oneshot
    from undercover.doubleagent import service
    with isolated_data():
        _voice_dir("plankton-spongebob", {"person": "Plankton"})
        _voice_dir("plankton-2", {"person": "Plankton"})
        vs = [get_voice("plankton-spongebob"), get_voice("plankton-2")]
        ch = oneshot.voice_choices("plankton", vs)
        assert [c["choice"] for c in ch] == ["use:plankton-spongebob", "use:plankton-2", "check", "train"], ch
        use, chk, tr = ch[0]["eta_s"], ch[2]["eta_s"], ch[3]["eta_s"]
        assert use["low"] < chk["low"] < tr["low"] and "trains one" in ch[2]["detail"], ch
        assert all(oneshot.VOICE_CHOICE.fullmatch(c["choice"]) for c in ch)
        assert not oneshot.VOICE_CHOICE.fullmatch("use:../x") and not oneshot.VOICE_CHOICE.fullmatch("delete")
        # the plan: asks, and lists each candidate once (an online result already installed was listed twice)
        real = (ambiguity.settle, ambiguity.recall, ambiguity.choices_for, service.installed_for, service.online_for,
                service.subject_of)
        ambiguity.settle = lambda req, search=None: req
        ambiguity.recall = lambda *a, **k: None
        ambiguity.choices_for = lambda *a, **k: []
        service.installed_for = lambda person: vs
        service.online_for = lambda person, n: [type("R", (), {"name": "plankton-spongebob"})()]
        service.subject_of = lambda *a, **k: None

        class H:
            out = err = None

            def _json(self, obj, status=200):
                self.out = obj

            def _error(self, msg, status=400):
                self.err = msg
        h = H()
        try:
            FA._auto_plan(h, "pink pony club as plankton", False)
        finally:
            (ambiguity.settle, ambiguity.recall, ambiguity.choices_for, service.installed_for, service.online_for,
             service.subject_of) = real
        v = (h.out or {}).get("voice") or {}
        assert v.get("how") == "check" and v["candidate_ids"] == ["plankton-spongebob", "plankton-2"], (h.out, h.err)
        assert h.out["voice_ask"] and [c["choice"] for c in h.out["voice_choices"]][-2:] == ["check", "train"]
        # the answer: "use:" uses that voice as it is, no check; one that's gone is refused
        real_inst = service.installed_for
        service.installed_for = lambda person: vs
        pushed, plan = [], {}
        try:
            got = service.resolve_voice(None, "plankton", False, None, lambda *a, **k: pushed.append(a[2]), plan,
                                        choice="use:plankton-2")
            assert got.name == "plankton-2" and "your pick" in plan["voice"]["checked"] and "as you picked" in pushed[-1]
            try:
                service.resolve_voice(None, "plankton", False, None, lambda *a, **k: None, {}, choice="use:gone-uc")
                raise AssertionError("a gone voice was used")
            except ModelError:
                pass
        finally:
            service.installed_for = real_inst
    src = inspect.getsource(service.resolve_voice)
    assert "against real clips" in src and 'plan["voice_until"]' in src, "the check's message / its own time"
    assert 'plan.get("voice_until")' in inspect.getsource(oneshot.run) and "voice_choice" in inspect.getsource(oneshot.start_oneshot)
    return f"choices use x2 / check / train ({use['low']} < {chk['low']} < {tr['low']} s); ids once; use: used as picked"


@check("the overnight report in words (smallfix4): before_word / after_word, outcome new / waiting / better / same / not_better (SAME = 0.02), and a note_plain with no decimals")
def u_report_words():
    import re as _re
    from undercover.core.worklist import SAME, report_words
    cases = [
        ("improve", {"before": 0.70, "after": 0.76, "pending": True, "note": "measured 0.76 against 0.7"}, "waiting"),
        ("project", {"after": 0.81, "pending_for": "x-uc"}, "waiting"),
        ("new", {"after": 0.82, "note": "'x-uc' added"}, "new"),
        ("newcad", {"after": None, "cadence": {"after": 0.3, "note": "cadence 0.31"}}, "new"),
        ("improve", {"before": 0.66, "after": 0.66 + SAME + 0.01}, "better"),
        ("improve", {"before": 0.70, "after": 0.71}, "same"),
        ("improve", {"before": 0.72, "after": 0.66}, "not_better"),
        ("cadence", {"after": 0.3, "replaced": False, "note": "none beat the current cadence 0.31"}, "not_better"),
        ("cadence", {"after": 0.3, "replaced": True}, "better"),
        ("cadence", {"after": 0.3, "replaced": False, "pending": True}, "waiting"),
        ("smaller", {"note": "measured 0.76 against 0.7, kept"}, None),
    ]
    for kind, res, want in cases:
        w = report_words(kind, res, "Morgan Freeman")
        assert w["outcome"] == want, (kind, res, w)
        assert w["note_plain"] and not _re.search(r"\d\.\d", w["note_plain"]), w
    w = report_words("improve", {"before": 0.70, "after": 0.76, "pending": True}, "Morgan Freeman")
    assert (w["before_word"], w["after_word"]) == ("good", "good") and "waits beside" in w["note_plain"], w
    assert "kept" not in report_words("new", {"after": 0.82}, "X")["note_plain"].lower()
    return "waiting / new / better / same / not_better, words on the trained cut-offs, no decimals"


@check("likeness and person for the voice cards (smallfix4): a trained voice's word falls back to its own save's identity (same run only); a downloaded voice without a checked person gets one guessed from its display name (never stored, never the folder; renaming corrects it), so Improve and Let it speak open up; the name guard is unchanged")
def u_person_guess():
    from types import SimpleNamespace
    from undercover.core import cadence as C
    from undercover.core.config import path
    from undercover.core.voices import (ModelError, get_voice, likeness_word, person_guessed, set_display, voice_person,
                                        voices_dir)
    from undercover.doubleagent import cadence, night
    from undercover.doubleagent.api import check_voice_name
    with isolated_data():
        # likeness: the project's own save for that epoch, only while the project still names this voice
        _voice_dir("tr-uc", {"trained": True, "project": "tr", "epoch": 8, "likeness": None})
        pj = path("cache_dir") / "training" / "tr"
        pj.mkdir(parents=True)

        def proj(exported):
            (pj / "project.json").write_text(json.dumps({"name": "tr", "person": "Tr", "train": {"exported": exported, "saves": [
                {"epoch": 8, "identity": 0.81}, {"epoch": 16, "identity": 0.5}]}}), encoding="utf-8")
            import os as _os, time as _t
            _os.utime(pj / "project.json", (_t.time() + len(exported), _t.time() + len(exported)))
        proj("tr-uc")
        assert likeness_word(get_voice("tr-uc")) == "great"
        proj("tr-other-uc")                                   # a later run of the project: its saves aren't this voice's
        assert likeness_word(get_voice("tr-uc")) is None
        # person: checked, guessed, trained, demo
        _voice_dir("squidward-tentacles", {})
        _voice_dir("charliekirkrvc2", {})
        _voice_dir("plankton-x", {"person": "Plankton"})
        _voice_dir("agent-smoke", {"demo": True})
        got = {n: (voice_person(get_voice(n)), person_guessed(get_voice(n))) for n in
               ("squidward-tentacles", "charliekirkrvc2", "plankton-x", "agent-smoke", "tr-uc")}
        assert got["squidward-tentacles"] == ("Squidward Tentacles", True) and got["charliekirkrvc2"] == (None, False), got   # one unknown word: no guess (P9-5)
        assert got["plankton-x"] == ("Plankton", False) and got["agent-smoke"] == (None, False), got
        set_display("squidward-tentacles", "Squid Ward")      # the user corrects it by renaming the display
        v = get_voice("squidward-tentacles")
        assert voice_person(v) == "Squid Ward" and v.dir.name == "squidward-tentacles" and "person" not in v.extra
        real = C.engine_ready
        C.engine_ready = lambda e: True
        try:
            assert cadence.can_train(v) == (True, "finds clips of Squid Ward first")
            assert not cadence.can_train(get_voice("agent-smoke"))[0]
            rows = {c["voice"]: c for c in night.candidates()}
            assert rows["squidward-tentacles"]["can"] and rows["squidward-tentacles"]["person_guessed"], rows["squidward-tentacles"]
            assert rows["squidward-tentacles"]["target"] == "Squid Ward" and not rows["plankton-x"]["person_guessed"]
        finally:
            C.engine_ready = real
        # the name guard is as before: another voice's folder is refused, a new name isn't
        p = SimpleNamespace(name="squidward")
        try:
            check_voice_name(p, "squidward-tentacles")
            raise AssertionError("another voice's folder accepted")
        except ModelError:
            pass
        check_voice_name(p, "squidward-uc")
    return "likeness from its own save; guessed persons; Squidward corrected by its display; cards open; guard unchanged"


@check("'tonight' is tonight (Internal Affairs P8-2): a tonight add arms the queue for the saved start time (23:00 when none, said so), never for now; an emptied queue disarms; Start now is one-time and goes back to the start time once dry; tonight's items (a move to the queue too) wait for the night")
def u_tonight():
    from datetime import datetime
    from undercover.core import worklist
    real = worklist.estimate, worklist.datetime
    evening, night = datetime(2026, 10, 16, 20, 0), datetime(2026, 10, 17, 2, 30)

    class Evening(datetime):   # the answers are read in the evening, whatever the clock says
        @classmethod
        def now(cls, tz=None):
            return evening
    worklist.estimate = lambda i: {"total": 30}
    worklist.datetime = Evening
    try:
        with isolated_data():
            worklist._STATE = None
            out = worklist.add("new", "Kristen Schaal")
            st = worklist._state()
            assert st["armed"] and not st.get("start_now") and out["starts_at"] == "23:00" and out.get("start_default"), out
            assert st["queue"][0].get("night")
            st["settings"]["start_at"] = "02:00"                       # the user's own start time
            st["queue"].clear(); st["armed"] = False
            out = worklist.add("new", "Kristen Schaal")
            assert out["starts_at"] == "02:00" and not out.get("start_default"), out
            assert worklist.next_item(st, evening) is None and st["armed"], "tonight ran in the evening"
            assert worklist.next_item(st, night)["target"] == "Kristen Schaal"
            # an emptied queue disarms: a later tonight add waits for the night instead of starting at once
            st["queue"].clear()
            assert worklist.next_item(st, evening) is None and not st["armed"]
            # Start now: one-time; tonight's adds meanwhile wait; dry -> back to the saved start time
            worklist.add("new", "Jon Stewart")
            worklist.arm(None)
            a = worklist.next_item(st, evening)
            assert a and a["target"] == "Jon Stewart" and st["start_now"] and st["settings"]["start_at"] == "02:00"
            a["state"] = "running"
            worklist.add("new", "Kristen Schaal")                      # e.g. a paused job moved to the queue
            assert worklist.next_item(st, evening) is None, "a tonight add ran during the Start now"
            a["state"] = "done"
            assert worklist.next_item(st, evening) is None and not st["start_now"] and st["armed"]
            assert worklist.next_item(st, night)["target"] == "Kristen Schaal"
            # an older Start now (it cleared the start time) is read as one
            st["settings"]["start_at"] = None
            st.pop("start_now", None)
            for i in st["queue"]:
                i.pop("night", None)                                  # (items from before had no flag)
            assert worklist.next_item(st, evening)["target"] == "Kristen Schaal" and st["settings"]["start_at"] == "23:00"
            worklist._STATE = None
    finally:
        worklist.estimate, worklist.datetime = real
    return "arms for 23:00 (default, said) / 02:00 (saved); evening no, night yes; empty disarms; Start now one-time"


@check("the small P8 fixes: the hold lifts when nothing held is left (P8-6), a Remove says what stays; time left never climbs (P8-3); plain messages with a clock time (P8-7/9); a gone candidate isn't offered (P8-7); measured check times and 'them' (P8-4); one estimator (P8-10)")
def u_p8_small():
    import inspect
    from unittest import mock
    from undercover.core import jobs as J, voices as V, worklist
    from undercover.core.voices import ModelError, get_voice
    from undercover.doubleagent import improved, night, service, sources
    from undercover.frontman import oneshot, songs
    with isolated_data():
        worklist._STATE = None
        st = worklist._state()
        st["queue"] = [{"id": "p1", "kind": "new", "target": "Jon Stewart", "state": "paused"}]
        st["held"] = True
        out = worklist.remove("p1")
        assert not out["held"] and "Storage" in out["note"], out
        worklist._STATE = None
        # a candidate whose folder is gone isn't offered
        _voice_dir("spongebob-uc", {"trained": True, "improved_pending": {"voice": "spongebob-uc-next"}})
        assert improved.state("spongebob-uc") is None
        try:
            get_voice("nope-uc")
            raise AssertionError("found")
        except ModelError as e:
            assert "python" not in str(e) and "installed:" not in str(e), e
        # measured check times lead the estimate
        service._record_check("search", 20.0)
        service._record_check("per_source", 130.0)
        service._record_check("per_voice", 120.0)
        from undercover.core.voicepreview import ESTIMATE_S
        # (a fresh person: one search, two sources, three candidates, the chosen one's line; IA pass 13)
        assert service.check_estimate_s("Nobody Here", 3) == 20.0 + 2 * 130.0 + 360.0 + ESTIMATE_S
        ch = oneshot.voice_choices("plankton", [get_voice("spongebob-uc")], others=2)
        assert ch[1]["label"].startswith("Check them")
        e = worklist.estimate_for("new", "Kristen Schaal", "speaking")
        assert e["cadence"] and e["total"] == e["prepare"] + e["train"] + e["compare"] + e["cadence"], e
    for mod in (sources, songs):
        assert "top right" not in inspect.getsource(mod), mod.__name__
    assert "Trying again at" in inspect.getsource(worklist) and "in an hour." not in inspect.getsource(worklist)
    # time left: one step's progress stalling and restarting can't make it climb (766 s -> 4817 s)
    job = J.Job("night")
    job.planned_s = 900.0
    t = [1000.0]
    with mock.patch("undercover.core.jobs.time.time", lambda: t[0]):
        lefts = []
        for k in range(80):
            t[0] += 1.5
            p = 0.26 + 0.002 * (k % 20)                                 # crawls, restarts every 20 updates
            job.push(stage="night", frac=p, message="Preparing clips: removing music and noise")
            lefts.append(job.events[-1]["total_left"])
    rises = [b - a for a, b in zip(lefts, lefts[1:])]
    assert max(lefts) <= lefts[0] * 1.15 and max(rises) < 10, (lefts[0], max(lefts), max(rises))
    return f"hold lifted; gone candidate hidden; plain texts; check 640 s measured; time left {lefts[0]:.0f}-{max(lefts):.0f} s"


@check("Internal Affairs pass 9 (smallfix6): an open night starts now and counts from now (P9-1); Finish by's waiting items said, never promised (P9-2); a person guessed only from a name (P9-5); one reading per name, a show from a downloaded voice, no sources about the word (P9-6); no 'kept only if better'; failed rows say failed")
def u_p9():
    import inspect
    from datetime import datetime
    from undercover.core import worklist
    from undercover.core.voices import get_voice, voice_person, voices_dir
    from undercover.doubleagent import cadence, night, project as P, service, sources, subject
    real = worklist.estimate, worklist.datetime
    clock = [datetime(2026, 10, 17, 1, 36)]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0]
    worklist.datetime = Clock
    try:
        with isolated_data():
            worklist._STATE = None
            worklist.estimate = lambda i: {"total": i.get("_m", 30) if "_m" in i else (48 if i["target"].startswith("Morgan") else 20)}
            st = worklist._state()
            st["settings"]["start_at"] = "23:00"
            out = worklist.add("new", "Kristen Schaal")          # 01:36, the night already on: it starts now
            assert out["starts_at"] == "01:36" and out["starts_now"] and out["done_by"] == "01:56", out
            st["queue"].clear(); st["armed"] = False
            clock[0] = datetime(2026, 10, 16, 20, 0)
            st["settings"].update(start_at="06:30", finish_by="07:00")
            out = worklist.add("improve" if "improve" in worklist.KINDS and False else "new", "Morgan Freeman")
            assert out.get("waits_next_night") and out["done_by"] is None and out["waits"] == 1, out
            out = worklist.add("new", "Jon Stewart")             # 20 min fits before 7:00
            rows = {r["target"]: r for r in out["queue"]}
            assert rows["Morgan Freeman"].get("waits_next_night") and not rows["Jon Stewart"].get("waits_next_night")
            assert out["start"] == "06:30" and out["done_by"] == "06:50" and not out.get("waits_next_night"), out
            worklist._STATE = None
            # P9-5: a person guessed only from a name
            _voice_dir("model-v2-final", {})
            _voice_dir("obama", {})
            _voice_dir("squid-fan", {})
            m = json.loads((voices_dir() / "squid-fan" / "meta.json").read_text(encoding="utf-8"))
            m["display"] = "Squid Ward (SpongeBob)"
            (voices_dir() / "squid-fan" / "meta.json").write_text(json.dumps(m), encoding="utf-8")
            assert voice_person(get_voice("model-v2-final")) is None and voice_person(get_voice("obama")) is None
            P.Project.create("Barack Obama", "speaking")
            assert voice_person(get_voice("obama")) == "Barack Obama"   # one word, a name UnderCover has met
            assert voice_person(get_voice("squid-fan")) == "Squid Ward"
            rows = {c["voice"]: c for c in night.candidates()}
            assert not rows["model-v2-final"]["can"], rows["model-v2-final"]      # no guess: Train my own
            # P9-6: one reading per name; a downloaded "(SpongeBob)" makes a character
            reads = iter([{"kind": "person", "label": "x is a real person"}, {"kind": "character", "label": "x is a character"}])
            real_cls = subject.classify
            subject.classify = lambda name, *a, **k: next(reads)
            try:
                first = service.subject_of("Kristen Schaal")
                assert service.subject_of("Kristen Schaal") == first and first["kind"] == "person", "the reading flipped"
                assert service.subject_of("Squid Ward")["kind"] == "character"          # the shown show wins
                assert service.subject_of("Squid Ward")["show"] == "spongebob"
                service.subject_of("Kristen Schaal", "character")                       # the user's pick is kept
                assert service.subject_of("Kristen Schaal")["kind"] == "character"
            finally:
                subject.classify = real_cls
    finally:
        worklist.estimate, worklist.datetime = real
        worklist._STATE = None
    rx = sources.ABOUT_THE_WORD
    assert rx.search("The Secret Life of Plankton | TED-Ed") and rx.search("Episode 216 - Plankton | Common Descent")
    assert not rx.search("Plankton's best moments | SpongeBob") and not rx.search("Plankton: every scene, season 3")
    assert "only if better" not in inspect.getsource(night.candidates)
    assert "if c.get(k) is not None" in inspect.getsource(cadence.chain_cadence)
    assert '"outcome": state' in inspect.getsource(worklist._run_item)
    return "01:36 starts now (done 01:56); 48-min waits, 20-min runs (done 06:50); Final -> none, Obama -> Barack Obama; reading kept"


@check("the start time sticks (user, 2026-10-17: 02:00 went back to 23:00 and 'done by' counted from 23:00): settings(start_at) survives a reload of the saved state (a restart), a tonight add, Start now and the runner; done_by = the shown start + the work at noon and at 01:30, from now inside the window, from the start time for a stopped queue's tonight jobs; the page saves the field at once")
def u_start_time_sticks():
    from datetime import datetime
    from undercover.core import worklist
    real = worklist.estimate, worklist.datetime
    clock = [datetime(2026, 10, 17, 12, 0)]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0]
    worklist.estimate = lambda i: {"total": 211}                           # 3 h 31 min, the user's queue
    worklist.datetime = Clock
    try:
        with isolated_data():
            worklist._STATE = None
            out = worklist.settings(start_at="02:00")                     # set in the Running room, nothing queued
            assert out["settings"]["start_at"] == "02:00" and not out["armed"]
            worklist._STATE = None                                         # a restart: the state comes back from its file
            assert worklist._state()["settings"]["start_at"] == "02:00", "the saved start time didn't survive a restart"
            out = worklist.add("new", "Kristen Schaal")                    # tonight: arms for 02:00, never for 23:00
            assert out["starts_at"] == "02:00" and not out.get("start_default") and out["settings"]["start_at"] == "02:00", out
            assert out["start"] == "02:00" and out["done_by"] == "05:31" and out["total_min"] == 211, out   # noon: 02:00 + 3 h 31
            clock[0] = datetime(2026, 10, 17, 1, 30)
            out = worklist.status()
            assert out["start"] == "02:00" and out["done_by"] == "05:31" and not out["starts_now"], out    # 01:30: still 02:00 + 3 h 31
            clock[0] = datetime(2026, 10, 17, 2, 30)
            out = worklist.status()
            assert out["starts_now"] and out["start"] == "02:30" and out["done_by"] == "06:01", out         # inside the window: from now (P9-1)
            clock[0] = datetime(2026, 10, 17, 12, 0)
            out = worklist.settings(start_at="03:00")                     # changed with a job queued: saved and counted from at once
            assert out["settings"]["start_at"] == "03:00" and out["start"] == "03:00" and out["done_by"] == "06:31", out
            st = worklist._state()
            worklist.arm(None)                                             # Start now keeps the saved time
            assert st["settings"]["start_at"] == "03:00" and st["start_now"]
            worklist.next_item(st, clock[0])
            assert st["settings"]["start_at"] == "03:00", "the runner changed the start time"
            worklist.arm(False)                                            # stopped, a tonight job left: "done by" from the start time, not the clock
            st["queue"][0].pop("now", None)
            out = worklist.status()
            assert not out["armed"] and out["start"] == "03:00" and out["done_by"] == "06:31", out
            worklist._STATE = None
            assert worklist._state()["settings"]["start_at"] == "03:00"
            worklist._STATE = None
    finally:
        worklist.estimate, worklist.datetime = real
    work = (ROOT / "undercover" / "web" / "work.js").read_text(encoding="utf-8")
    handler = work[work.index('$("#rn-at-time").onchange'):work.index("async function saveStartTime")]
    assert "if (e.target.value) saveStartTime(e.target.value)" in handler and ".some(" not in handler, "the page saves the start time only while nothing is queued again"
    return "02:00 saved, back after a restart, kept by tonight / Start now / the runner; done by 05:31 at noon and 01:30, 06:01 at 02:30; the page saves at once"


@check("Start at never resumes held work the user paused (smallfix6 addendum): arm() lifts only the queue's own pause; settings() saves a valid HH:MM start_at without arming")
def u_start_at_hold():
    from undercover.core import worklist
    from undercover.core.voices import ModelError
    with isolated_data():
        worklist._STATE = None
        st = worklist._state()
        st["held"], st["held_by"] = True, "you"
        st["resume"] = [{"id": "h1", "kind": "oneshot", "text": "pink pony club as plankton", "label": "One-shot"}]
        out = worklist.arm("02:00")                                    # Start at, the queue empty
        assert out["held"] and st["resume"] and st["armed"], "Start at lifted the user's hold"
        worklist.arm(None)                                             # Start now: the same
        assert st["held"] and st["resume"]
        st["resume"] = []
        st["queue"] = [{"id": "q1", "kind": "new", "target": "A", "state": "paused"}]
        worklist.arm("02:00")                                          # the queue's own pause only: it goes on
        assert not st["held"] and st["queue"][0]["state"] == "queued"
        worklist.arm(False)
        out = worklist.settings(start_at="03:15")
        assert out["settings"]["start_at"] == "03:15" and not out["armed"], out
        for bad in ("25:00", "3:15", "soon", 315):
            try:
                worklist.settings(start_at=bad)
                raise AssertionError(f"{bad!r} accepted")
            except ModelError:
                pass
        worklist._STATE = None
    return "held one-shot stays held through Start at / Start now; queue pause lifts; start_at saved, bad ones refused"


def _diffs(mean, spread, n=12, seed=1):
    """Paired differences around a mean with a spread small enough that the bootstrap interval sits on one side."""
    import numpy as np
    rng = np.random.default_rng(seed)
    return list(mean + rng.normal(0, spread, n))


def _cmp(id_l, hnr, contrast, id_d=0.0, n=12, nd=30):
    """A compare() result built from the research table's own-lines deltas (likeness, harmonicity, contrast) and a donor delta."""
    from undercover.doubleagent.frozen import paired
    return {"lines": {"n": n, "id": paired(_diffs(id_l, 0.01, n)), "hnr": paired(_diffs(hnr, 0.01, n)),
                      "contrast": paired(_diffs(contrast, 0.08, n))},
            "donors": {"n": nd, "id": paired(_diffs(id_d, 0.01, nd))}}


@check("Improve's offer bar (phase 26, R3) reproduces the eleven blind verdicts on the research table: the two kept voices are offered, every rejected, 'same' or 'level' one fails; too few own lines or a slipped donor likeness refuses; the words say what changed (R3a)")
def u_frozen_bar():
    from undercover.doubleagent.frozen import offer, words_of
    cases = [   # (name, likeness own lines, hnr, contrast, donor likeness, ear kept the NEW one?)
        ("Griffin 3.5->18 scratch", +0.03, +0.096, +0.59, +0.03, True),
        ("SpongeBob 7->13 scratch", 0.0, +0.058, +0.40, 0.0, True),
        ("Obama 59->130 scratch (8 passes)", +0.03, +0.007, +0.34, +0.028, False),
        ("Freeman 13->18 scratch", -0.02, 0.0, -0.39, -0.025, False),
        ("Chappelle 32->38 scratch", -0.01, -0.017, -0.45, -0.019, False),
        ("Chappelle calm 34", +0.01, 0.0, +0.19, +0.036, False),
        ("Chappelle stand-up 21", -0.09, +0.03, +0.40, -0.008, False),
        ("Trump 36->77 scratch (8 passes)", 0.0, +0.024, -0.88, +0.003, False),
        ("Freeman refine (level)", 0.0, +0.040, 0.0, +0.003, False),
        ("Obama refine", 0.0, +0.020, +0.43, -0.004, False),
        ("Trump rallies-only vs hand-picked", -0.104, +0.01, -0.3, -0.096, False),
    ]
    for name, idl, hnr, con, idd, kept in cases:
        v = offer(_cmp(idl, hnr, con, idd))
        assert v["offer"] == kept, (name, v)
    v = offer(_cmp(0.0, 0.058, 0.40))
    assert words_of(v) == "cleaner voicing, more detail; the likeness is unchanged", words_of(v)
    v = offer(_cmp(0.05, 0.08, 0.5))
    assert words_of(v) == "cleaner voicing, more detail; a closer likeness", words_of(v)
    assert not offer(_cmp(0.0, 0.08, 0.5, id_d=-0.05))["offer"], "a slipped donor likeness was offered"
    assert "other people" in offer(_cmp(0.0, 0.08, 0.5, id_d=-0.05))["reasons"][0]
    assert not offer(_cmp(0.0, 0.08, 0.5, n=3))["offer"] and "too few" in offer(_cmp(0.0, 0.08, 0.5, n=3))["reasons"][0]
    assert offer(_cmp(0.0, 0.08, 0.5, n=0), {"singer": True})["reasons"] == ["no spoken lines to judge a singer by yet"]
    assert offer(_cmp(0.0, 0.08, 0.5, nd=5))["offer"], "too few donors must skip the guard, not refuse"
    r = offer(_cmp(-0.02, 0.0, -0.39))["reasons"]
    assert r == ["the likeness slipped", "the voicing isn't clearer", "no more detail in it"], r
    return "11 of 11 verdicts; words; donor guard; too few lines refuses; too few donors skips the guard"


@check("Improve's recipe and budget (phase 26, R4 / R5): refine for a well-fed voice with a modest data jump, scratch for a thin one or a big jump; 450 steps a kept minute (floor 8000, cap 2x); refine steps scale 1000-4000; the gate rows (R6) on the research's examples")
def u_frozen_recipe():
    from undercover.doubleagent.frozen import budget_steps, gate_rows, recipe, refine_steps
    assert recipe(13, 0.78, 18)[0] == "refine"                      # Freeman: x1.4
    assert recipe(59, 0.75, 130)[0] == "scratch"                    # Obama: x2.2
    assert recipe(7, 0.77, 13)[0] == "scratch"                      # SpongeBob: thin
    assert recipe(3.5, 0.50, 18)[0] == "scratch"                    # Griffin: thin and x5
    assert recipe(20, None, 25)[0] == "refine"                      # likeness unknown: the path that can't wander off
    assert recipe(20, 0.55, 25)[0] == "scratch"                     # a weak voice: fresh
    assert recipe(0, None, 10)[0] == "scratch"
    assert budget_steps(13) == (8000, 16000) and budget_steps(36) == (16200, 32400) and budget_steps(0) == (8000, 16000)
    assert refine_steps(15) == 2000 and refine_steps(3) == 1000 and refine_steps(60) == 4000
    from undercover.doubleagent.frozen import nothing_new
    assert nothing_new(13.3, 13.3, [], False) and nothing_new(13.6, 13.3, [], False)       # the real Freeman run: 0 new, nothing out
    assert not nothing_new(11.9, 13.3, [{"id": "x"}], False), "sources left out: the set changed, it trains"
    assert not nothing_new(13.3, 13.3, [], True), "a source newly prepared trains"
    assert not nothing_new(14.0, 13.3, [], False) and not nothing_new(13.3, 0, [], False) and not nothing_new(5.0, None, [], False)
    rows = [{"id": "a", "title": "NPR", "kept_min": 4.0, "cos": 0.70, "med_score": 0.6, "dropped": 0.3, "f0_st": 0.5},
            {"id": "b", "title": "2001 interview", "kept_min": 2.0, "cos": 0.57, "med_score": 0.6, "dropped": 0.4, "f0_st": 8.4},
            {"id": "c", "title": "Conan", "kept_min": 2.0, "cos": 0.70, "med_score": 0.55, "dropped": 0.3, "f0_st": 9.1},
            {"id": "d", "title": "about him", "kept_min": 0.0, "cos": None, "med_score": 0.1, "dropped": 1.0},
            {"id": "e", "title": "noisy", "kept_min": 0.5, "cos": 0.7, "med_score": 0.6, "dropped": 0.95, "f0_st": 0.0},
            {"id": "f", "title": "no pitch", "kept_min": 3.0, "cos": 0.7, "med_score": 0.6, "dropped": 0.2}]
    gate_rows(rows, 101.0)   # the approved voice at 101 Hz = +0.2 st
    got = {r["id"]: r["pass"] for r in rows}
    assert got == {"a": True, "b": False, "c": False, "d": False, "e": False, "f": True}, got
    assert "another voice" in rows[1]["gate"] and "younger" in rows[1]["gate"], rows[1]["gate"]
    assert rows[2]["gate"] == "a much younger voice" and rows[3]["gate"] == "nothing usable" and "usable" in rows[4]["gate"]
    return "refine x1.4 / scratch x2.2, thin, weak, unknown; budget 8000..32400; refine 1000..4000; nothing-new rule; gate 6 rows"


TRUMP_FINDS = [   # the titles of Trump's real new-voice search and hand-picks (2026-10-14), with their minutes
    ("t01", "FULL SPEECH: President Donald Trump Holds Midterm Campaign Rally In Syracuse", 84),
    ("t02", "President Donald Trump's extended 60 Minutes interview", 73),
    ("t03", "President Donald Trump 2025 Inaugural Address FULL SPEECH", 30),
    ("t04", "President Trump Addresses Joint Session of Congress, March 4, 2025", 120),
    ("t05", "Donald Trump's address to the nation from Oval Office | December 2025", 18),
    ("t06", "Donald Trump Interview | Lex Fridman Podcast #442", 64),
    ("t07", "Canada\u2019s Plans for Donald Trump | The New Yorker Radio Hour", 31),
    ("t08", "How Trump Just Became America's Biggest Traitor | The Daily Beast Podcast", 40),
    ("t09", "Interview: Donald Trump with Rona Barrett - October 6, 1980", 49),
    ("t10", "Zelenskyy talks Trump, rising diesel prices and the ongoing war in his country", 16),
    ("t11", "Joe Rogan Experience #2219 - Donald Trump", 178),
]


@check("the real-person search (smallfix7, Trump's new run 2026-10-14): talk ABOUT the person is unticked, material decades before their newest is a younger voice, and speeches / addresses / recent rank above rallies, chambers and old interviews; Do it all prepares by that rank and gates what it prepared")
def u_person_search():
    import inspect
    from undercover.doubleagent import service, sources as S
    about = {t for i, t, m in TRUMP_FINDS if i in ("t07", "t08", "t10")}
    for i, t, m in TRUMP_FINDS:
        assert S.about_them(t, "Donald Trump") == (t in about), t
    assert S.about_them("The Truth About Morgan Freeman", "Morgan Freeman") and not S.about_them("Morgan Freeman on The Daily Show", "Morgan Freeman")

    class FakeYdl:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def extract_info(self, q, download=False):
            return {"entries": [{"id": i, "title": t, "channel": "c", "duration": m * 60.0} for i, t, m in TRUMP_FINDS]}
    ydl, S._ydl = S._ydl, FakeYdl
    try:
        found = S.search("Donald Trump", "speaking", force="person")
    finally:
        S._ydl = ydl
    by = {x.id: x for x in found}
    for i in ("t07", "t08", "t10"):
        assert not by[i].selected and by[i].note == S.ABOUT_NOTE, (by[i].title, by[i].selected, by[i].note)
    assert not by["t09"].selected and "younger voice" in by["t09"].note and "1980" in by["t09"].note, by["t09"].note
    order = [x.id for x in found]
    assert order.index("t05") < order.index("t01") and order.index("t03") < order.index("t01"), order   # addresses before the rally
    assert order.index("t04") > order.index("t05"), order                                              # the chamber below the Oval Office
    assert order.index("t09") > order.index("t06"), order                                              # 1980 below a recent interview
    assert by["t05"].selected and by["t03"].selected
    rank = lambda i: S.expected_clean(by[i], "Donald Trump")                                           # noqa: E731
    assert rank("t05") > rank("t02") > rank("t09") and rank("t05") > rank("t04"), [rank(i) for i in ("t05", "t02", "t09", "t04")]
    assert "{name} talks" not in S.QUERIES["speaking"] and "{name} address" in S.QUERIES["speaking"]
    src = inspect.getsource(service.train_new_voice)
    assert "expected_clean(" in src and 'by="DoubleAgent"' in src and "not s.user" in src, "Do it all lost its rank or its gate"
    import types
    assert "and not srcmod.left_out_for_a_reason(s)" in src and S.left_out_for_a_reason(types.SimpleNamespace(note=S.ABOUT_NOTE)), \
        "Do it all may tick talk about the person back in when it's short"
    return "3 'about him' and 1980 unticked; addresses > rally > chamber; recent > 1980; Do it all ranks and gates"


@check("the trainer honours source.selected (smallfix7, the phase 26 known bug): unticking a prepared source takes its phrases out of the training set, its holdout, the kept minutes and the cadence's clips; a phrase the user ticked by hand still trains; the gate leaves an already-unticked source alone and says who left a source out")
def u_source_selected():
    from undercover.doubleagent import cadence, frozen, project as P, trainer
    with isolated_data():
        proj = _fake_project("sel", "Sel Person", n_phrases=30, other=0, n_sources=3)
        all_ids = {p.id for p in proj.phrases if p.kept}
        before = proj.kept_seconds()
        key0 = trainer.data_key_for(proj, 40000)[0]
        proj.source("s1").selected = False
        s1 = {p.id for p in proj.phrases if p.source == "s1"}
        hand = next(p for p in proj.phrases if p.source == "s1")
        hand.choice = True                                                    # ticked by hand inside the unticked source
        proj.save()
        proj = P.Project.load("sel")
        key, kept, hold = trainer.data_key_for(proj, 40000)
        assert key != key0 and set(kept) == (all_ids - s1) | {hand.id}, "an unticked source's phrases still train"
        assert not (set(hold) & (s1 - {hand.id})) and not (set(trainer.choose_holdout(proj)) & (s1 - {hand.id}))
        assert abs(proj.kept_seconds() - (before - sum(p.dur for p in proj.phrases if p.id in s1 - {hand.id}))) < 1e-6
        assert {p.id for p in cadence.speech_clips(proj)} == set(kept)
        assert proj.summary()["kept"] == len(kept)
        proj.source("s1").selected = True
        assert P.Project.load("sel").source("s1").selected is False and proj.kept_seconds() == before   # ticked again: back
        # the gate: an unticked source stays as the user left it (its phrases untouched); the label says who left one out
        proj = P.Project.load("sel")
        emb = proj.load_embeddings()
        for p in proj.phrases:
            if p.source == "s2":
                emb[p.id] = -emb[p.id]                                        # s2 is someone else
        g = frozen.gate_project(proj, emb, None, by="DoubleAgent")
        assert [x["title"] for x in g["left_out"]] == ["Source 2"], g["left_out"]
        assert proj.source("s2").note.startswith("left out by DoubleAgent:") and not proj.source("s1").selected
        assert all(p.reason == "" for p in proj.phrases if p.source == "s1"), "the gate marked an unticked source's phrases"
    return "unticked: out of data key, holdout, kept minutes, cadence clips; hand-ticked stays; the gate skips it, labels by who"


@check("the Reader by voice print and accent (smallfix7: Trump read by bf_emma, a British woman): a clear voice-print vote decides the sex, else the pitch; automatic picks are American unless the voice is known British; the vote is measured from the project's reference voice once and kept with the voice")
def u_reader_print():
    import json as _json
    import numpy as np
    from undercover.core import speaker
    from undercover.core.voices import get_voice
    from undercover.doubleagent import service
    from undercover.speakeasy import speak, tts
    import inspect
    prints = speaker.labelled_prints()
    assert len(prints) == len(tts.READERS) and {k for k, _, _ in prints} == set(tts.READERS), "the shipped prints don't match the Readers"
    wrong = [k for k, sex, v in prints if (speaker.sex_vote(v + 0.0) > 0) != (sex == "male")]
    right = len(prints) - len(wrong)
    assert wrong in ([], ["af_alloy"]), f"the prints vote their own Readers' sex wrong: {wrong}"   # af_alloy: 'American woman, low'
    assert speaker.sex_vote(None) is None
    # the user's people, as measured on their real projects (2026-10-17): vote, median Hz, low end Hz
    people = {"Trump": (3, 167.1, 126.4, "am_"), "Obama": (-1, 180.0, 109.3, "am_"), "Freeman": (1, 104.6, 72.0, "am_"),
              "Kristen Schaal": (-5, 195.0, 109.6, "af_"), "Peter Griffin": (5, 177.9, 88.1, "am_"), "Chappelle": (3, 101.2, 64.7, "am_")}
    for who, (vote, hz, low, want) in people.items():
        r = tts.pick_reader(hz, low, vote=vote)
        assert r.startswith(want), (who, r)
    assert tts.pick_reader(167.1, 126.4, vote=3) == "am_fenrir"
    assert not any(tts.pick_reader(hz, lo, vote=vt).startswith("b") for hz in (90, 130, 167.1, 178.6, 200, 260)
                   for lo in (70, 120, 160) for vt in (None, -5, -1, 1, 5)), "a British Reader was picked for a voice not known British"
    assert tts.pick_reader(178.6, 130.0).startswith("af_") and tts.pick_reader(178.6, 130.0, vote=4).startswith("am_")
    assert tts.pick_reader(120.0, 80.0, accent="british").startswith("bm_") and tts.pick_reader(None, None, vote=5) == "am_michael"
    assert tts.pick_reader(None, None) == tts.DEFAULT
    assert "vote=service.print_vote(v)" in inspect.getsource(speak.plan)
    # measured once from the project's reference voice, kept in meta.json; no project: None (pitch decides)
    with isolated_data():
        proj = _fake_project("pv", "Print Voice", n_phrases=12, other=0, n_sources=2)
        emb = proj.load_embeddings()
        emb["speaker:1"] = next(v for k, sex, v in prints if k == "am_michael")
        proj.save_embeddings(emb)
        _voice_dir("pv-uc", {"project": "pv"})
        _voice_dir("downloaded", {})
        vote = service.print_vote(get_voice("pv-uc"))
        assert vote is not None and vote > 0, vote
        meta = _json.loads((get_voice("pv-uc").dir / "meta.json").read_text(encoding="utf-8"))
        assert meta["extra"]["print_vote"] == vote and service.print_vote(get_voice("pv-uc")) == vote
        assert service.print_vote(get_voice("downloaded")) is None
        _fake_project("noref", "No Ref", n_phrases=6, other=0, n_sources=1)
        P2 = __import__("undercover.doubleagent.project", fromlist=["Project"]).Project
        pr = P2.load("noref")
        pr.reference = {}
        pr.save()
        _voice_dir("noref-uc", {"project": "noref"})
        assert service.print_vote(get_voice("noref-uc")) is None
        assert "print_vote" in _json.loads((get_voice("noref-uc").dir / "meta.json").read_text(encoding="utf-8"))["extra"], \
            "a project without a reference voice is read again on every listing"
    return f"prints vote their Readers right ({right}/{len(prints)}); Trump -> am_fenrir, Kristen -> af_; never British unasked; kept once"


@check("the compare is priced by the frozen test's size (smallfix7; the real Freeman run said 9 min and took 70 s, its time left jumping 46 -> 8 -> 20 s): an Improve's estimate and its time left during the compare come from the test's own lines and donors")
def u_compare_priced():
    import inspect
    from undercover.doubleagent import frozen, night
    small = {"lines": [0] * 6, "donors": [0] * 8, "approved": "abc"}
    c = frozen.compare_seconds(small)
    assert c["ensure"] == 0.0 and c["judge"] == round(frozen.JUDGE_BASE_S + frozen.SCORE_S * 14, 1), c
    assert 20 < c["judge"] < 60, c                                            # the real run: ~25 s for the candidate's score
    unscored = frozen.compare_seconds({**small, "approved": None})
    assert unscored["ensure"] == round(frozen.SCORE_S * 14, 1)
    first = frozen.compare_seconds(None)                                      # a first Improve: freeze at the usual size
    assert first["ensure"] > first["judge"] > c["judge"] and (first["ensure"] + first["judge"] + frozen.EXPORT_S) / 60 < 5, first
    assert not hasattr(night, "COMPARE_MIN") and not hasattr(night, "FREEZE_MIN")
    src = inspect.getsource(night._improve)
    assert "compare_seconds(frozen.load_test(old))" in src and src.count("total_left=") >= 2, "the compare's time left is extrapolated again"
    with isolated_data():
        _fake_project("cmp", "Cmp Person", n_phrases=40, other=0, n_sources=2)
        _voice_dir("cmp-uc", {"project": "cmp", "train_minutes": 3.0})
        est = night.estimate({"kind": "improve", "target": "cmp-uc"})
        assert est["compare"] == round((frozen.EXPORT_S + first["ensure"] + first["judge"]) / 60), est
    return f"6 lines + 8 donors: {c['judge']:.0f} s; a first Improve's freeze + compare: {(first['ensure'] + first['judge']) / 60:.1f} min"


@check("one version (release-prep): undercover/VERSION is the only place it's written; about.VERSION, the System room / Doctor / diagnostics (about.about), the installer (UnderCover.iss reads the file, build.ps1 checks it) and the friends' guide and README all say it; What's new has the 1.0 entry")
def u_version():
    import re
    from undercover.core import about
    web = ROOT / "undercover" / "web"
    v = (ROOT / "undercover" / "VERSION").read_text(encoding="utf-8").strip()
    assert re.fullmatch(r"\d+\.\d+\.\d+", v), v
    assert about.VERSION == v and about.about()["version"] == v, (about.VERSION, v)
    iss = (ROOT / "installer" / "UnderCover.iss").read_text(encoding="utf-8-sig")
    assert 'undercover\\VERSION' in iss and "FileRead(" in iss, "the installer doesn't read undercover\\VERSION"
    assert not re.search(r'#define\s+AppVersion\s+"', iss), "the installer writes its own version again"
    assert "undercover\\VERSION" in (ROOT / "installer" / "build.ps1").read_text(encoding="utf-8")
    for doc in ("installer/GUIDE.md", "README.md"):
        assert f"UnderCover-Setup-{v}.exe" in (ROOT / doc).read_text(encoding="utf-8"), f"{doc} names another installer"
    # no other file writes the app's version as a literal (the System room reads about.version from the Doctor's report)
    for f in [ROOT / "undercover" / "core" / "about.py", ROOT / "undercover" / "core" / "manifest.py", ROOT / "undercover" / "core" / "diagnostics.py",
              web / "rooms.js", web / "index.html"]:
        txt = f.read_text(encoding="utf-8")
        assert not re.search(r"""(VERSION|version)\s*[:=]\s*["']\d+\.\d+\.\d+["']""", txt), f"{f.name} writes a version of its own"
    assert "a.version" in (web / "rooms.js").read_text(encoding="utf-8")
    wn = json.loads((web / "whatsnew.json").read_text(encoding="utf-8"))["entries"]
    ids = [e["id"] for e in wn]
    assert len(ids) == len(set(ids)) and wn[0]["title"] == f"UnderCover {v.rsplit('.', 1)[0] if v.endswith('.0') else v}", wn[0]
    assert 1 <= len(wn[0]["lines"]) <= 4
    assert f"## {v}" in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    return f"{v} from one file: app, System room, Doctor, diagnostics, installer, guides, What's new, changelog"


@check("one name, one reading (RC pass A: 'pink pony club as plankton' read as a character, a person and a singer on three fresh installs): a downloaded voice's folder or source naming a known show makes it a character from that show, over a web guess and over a remembered one; a folder's other words aren't a show; the user's pick still wins")
def u_plankton_reads():
    import json as _json
    from undercover.core.voices import voices_dir
    from undercover.doubleagent import service, subject
    asked = []
    real = subject.classify

    def web(name, *a, **k):                                                   # the web's guess: a singer, every time
        asked.append(name)
        return {"kind": "singer", "label": f"{name} is a singer", "how": "their songs", "evidence": [], "choices": []}
    subject.classify = web
    try:
        with isolated_data():
            src = "https://huggingface.co/binant/Plankton__SpongeBob___RVC_v2__1K_Epochs_69K_Steps/resolve/main/model.pth"
            for name, source in (("plankton-spongebob", ""), ("plankton", src)):
                d = _voice_dir(name, {})
                m = _json.loads((d / "meta.json").read_text(encoding="utf-8"))
                m["source"] = source
                (d / "meta.json").write_text(_json.dumps(m), encoding="utf-8")
            r = service.subject_of("plankton")
            assert r["kind"] == "character" and r["show"] == "spongebob" and "SpongeBob" in r["label"], r
            assert not asked, "the web was asked although a downloaded voice names the show"
            # a fresh install that remembered a web guess: the voice still wins, and keeps winning
            service._save_subjects({"plankton": {"kind": "singer", "label": "plankton is a singer"}})
            assert service.subject_of("plankton")["kind"] == "character" and service.subject_of("plankton")["show"] == "spongebob"
            # only the source link names it (the folder is just "plankton")
            import shutil
            shutil.rmtree(voices_dir() / "plankton-spongebob")
            service._save_subjects({})
            assert service.subject_of("Plankton")["kind"] == "character"
            # a folder's other words aren't a show; with no voice, the web's reading stands (and is remembered)
            _voice_dir("morgan-freeman-narrator", {})
            assert service.subject_of("Morgan Freeman")["kind"] == "singer" and asked == ["Morgan Freeman"]
            assert service._show_in("spongebob-squarepants", "SpongeBob") is None, "the person's own name read as a show"
            # the user's pick still wins over the voice
            service.subject_of("plankton", "person")
            assert service.subject_of("plankton")["kind"] == "person"
    finally:
        subject.classify = real
    return "folder 'plankton-spongebob' or its source -> a character from SpongeBob, over a remembered 'singer'; narrator isn't a show; a pick wins"


@check("the phrase list follows what trains (RC pass C: Morgan's 91 phrases said kept while 12.7 of 17.8 min trained): a phrase of an unticked source is listed as left out with why, unless ticked by hand; the listed kept minutes equal kept_minutes")
def u_phrase_list_trains():
    from undercover.doubleagent import api as da_api, project as P
    with isolated_data():
        proj = _fake_project("listed", "Listed Person", n_phrases=24, other=3, n_sources=3)
        proj.source("s1").selected = False
        hand = next(p for p in proj.phrases if p.source == "s1" and p.kept)
        hand.choice = True
        proj.save()
        out = da_api.project_json(P.Project.load("listed"))
        rows = {r["id"]: r for r in out["phrases"]}
        for p in proj.phrases:
            if p.source == "s1" and p.kept and p.id != hand.id:
                assert not rows[p.id]["kept"] and rows[p.id]["reason"] == da_api.UNTICKED, rows[p.id]
        assert rows[hand.id]["kept"], "a hand-ticked phrase listed as left out"
        assert all(rows[p.id]["reason"] == p.reason for p in proj.phrases if p.source != "s1")
        listed = sum(r["dur"] for r in out["phrases"] if r["kept"]) / 60
        assert abs(listed - out["kept_minutes"]) < 0.02, (listed, out["kept_minutes"])
        assert P.Project.load("listed").phrases[0].reason == proj.phrases[0].reason, "the listing wrote the project"
    return f"unticked source: listed left out ({da_api.UNTICKED}); hand-ticked stays; {listed:.2f} min = kept_minutes"


FULL_CONFIG = r"""# UnderCover configuration: all paths and defaults live here.

[gpu]
device_name = "7900 XTX"   # a comment
backend = "rocm"

[paths]
models_dir = "models"
cache_dir = 'D:\Work\cache'

[training]
target_minutes = 15
max_minutes_per_source = 12

[game_pause]
never_ask = ["explorer.exe", "C:/Games/x \"quoted\".exe"]
ask = true
"""


@check("settings in two layers (layout): undercover/config.default.toml, then the user's config.toml on top (tables key by key); a full file from before is migrated to only what differs, values exactly kept, the full one kept beside it, once; every settings write lands in the user's file, never the defaults")
def u_config_layers():
    import tomllib
    from undercover.core import config, gamepause, storage
    base = {"gpu": {"device_name": "7900 XTX", "backend": "rocm", "fp16": True}, "training": {"target_minutes": 15}}
    got = config.overlay(base, {"gpu": {"backend": "cpu"}, "game_pause": {"ask": False}})
    assert got == {"gpu": {"device_name": "7900 XTX", "backend": "cpu", "fp16": True}, "training": {"target_minutes": 15},
                   "game_pause": {"ask": False}}, got
    assert config.differing({"gpu": {"backend": "rocm", "x": 1}}, base) == {"gpu": {"x": 1}}
    d = config.defaults()
    assert d["gpu"] and d["paths"]["models_dir"] == "models" and config.DEFAULT_FILE.parent.name == "undercover"
    assert config.CONSTRAINTS == ROOT / "installer" / "constraints.txt" and config.CONSTRAINTS.is_file()
    import copy
    outside = copy.deepcopy(config.CONFIG)
    with isolated_data() as t:
        assert config.CONFIG.get("game_pause", {}) == config.read(config.CONFIG_FILE).get("game_pause", {}), \
            "the user's own settings reach a check inside isolated_data"
        # a fresh install: no user file, the defaults alone; a new default reaches it
        none = t / "fresh" / "config.toml"
        none.parent.mkdir()
        assert config.read(none) == d and config.migrate(none) is None
        # an existing install's full file, with a value of its own in each kind (a path, a list with quotes, a bool)
        full = t / "old" / "config.toml"
        full.parent.mkdir()
        text = FULL_CONFIG.replace('target_minutes = 15', f'target_minutes = {d["training"]["target_minutes"]}')
        full.write_text(text, encoding="utf-8")
        was = config.read(full)
        dry = config.migrate(full, dry_run=True)
        assert full.read_text(encoding="utf-8") == text and dry["backup"] is None, "a dry run wrote"
        r = config.migrate(full)
        bak = Path(r["backup"])
        assert bak.name == config.BACKUP and bak.read_text(encoding="utf-8") == text, "the full file wasn't kept as it was"
        assert config.read(full) == was, "the migrated file gives other settings"
        slim = tomllib.loads(full.read_text(encoding="utf-8"))
        assert full.read_text(encoding="utf-8").startswith(config.HEADER) and r["after"] == full.read_text(encoding="utf-8")
        assert slim["game_pause"] == {"never_ask": ["explorer.exe", 'C:/Games/x "quoted".exe'], "ask": True}, slim
        assert slim["paths"] == {"cache_dir": r"D:\Work\cache"} and "training" not in slim and "backend" not in slim.get("gpu", {}), slim
        assert config.migrate(full) is None, "migrated twice"
        full.write_text(text, encoding="utf-8")                     # (a second full file later: the first backup stays)
        assert Path(config.migrate(full)["backup"]).name == "config.before-defaults-2.toml" and bak.read_text(encoding="utf-8") == text
        # writes: the user's file (made with the header if missing), never the defaults
        before = config.DEFAULT_FILE.read_bytes()
        config.CONFIG_FILE.unlink()
        gamepause.set_never("game.exe", True)
        gamepause.set_ask(False)
        storage.write_paths({"uploads_dir": f"'{t / 'app' / 'uploads'}'"})
        mine = config.CONFIG_FILE.read_text(encoding="utf-8")
        assert mine.startswith(config.HEADER) and tomllib.loads(mine)["game_pause"] == {"never_ask": ["game.exe"], "ask": False}, mine
        assert "uploads_dir" in tomllib.loads(mine)["paths"] and config.DEFAULT_FILE.read_bytes() == before
        try:
            config.set_values("gpu", {"backend": '"cpu"'}, config_file=config.DEFAULT_FILE)
            raise AssertionError("the defaults file was written")
        except ValueError:
            pass
        # the command line setup.ps1 uses: strings, one key=value each, spaces kept
        assert config._cli(["set", "gpu", "device_name=RX 7900 XTX", "backend=rocm"]) == 0
        assert config.CONFIG["gpu"]["device_name"] == "RX 7900 XTX" and config.read(config.CONFIG_FILE)["gpu"]["backend"] == "rocm"
        # a vault restore keeps this install's [paths], and brings none from elsewhere when this one has none
        from undercover.core import vault
        other = '[paths]\nmodels_dir = "D:/elsewhere"\n\n[ui]\nport = 7860\n'
        assert "[paths]" in vault._config_keeping_paths(other.encode()).decode()     # ours has [paths] (set above)
        crlf = vault._config_keeping_paths(other.replace('\n', '\r\n').encode()).decode()
        assert crlf.count("[paths]") == 1 and "elsewhere" not in crlf, crlf   # a CRLF file's own [paths] is replaced, not doubled
        config.CONFIG_FILE.write_text(config.HEADER + "[ui]\nport = 7860\n", encoding="utf-8")
        assert "elsewhere" not in vault._config_keeping_paths(other.encode()).decode()
        # importing the entry point (as the imports check does) never migrates: only a real start does
        import importlib
        import inspect
        config.CONFIG_FILE.write_text(text, encoding="utf-8")
        import undercover.server as entry
        importlib.reload(entry)
        assert config.CONFIG_FILE.read_text(encoding="utf-8") == text, "importing undercover.server rewrote a config.toml"
        src = inspect.getsource(entry.main)
        assert src.index("_migrate()") < src.index("from .app.server import"), "the migration must come before the server stamps itself"
    assert config.CONFIG == outside, "a check's settings writes leaked out of isolated_data"
    return "overlay; fresh = defaults; migrate keeps values (path, quoted list, bool), the full file, once; writes go to the user file"


@check("the product repo's root (layout + layout2, user 2026-10-17: 'shouldn't root be basically the readme, changelog, and project folder', 'do it'): README, CHANGELOG, .gitignore, docs/MASTERPLAN.md and the undercover / installer / scripts / tests folders; every reference to a moved file follows; the user's config.toml is ignored")
def u_layout():
    import re
    import subprocess
    files = [f for f in subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, text=True, encoding="utf-8").stdout.split("\0") if f]
    if not files:
        return "not a git checkout: skipped"
    top = sorted({f.split("/")[0] for f in files})
    assert top == [".gitignore", "CHANGELOG.md", "CONTRIBUTING.md", "LICENSE", "NOTICE", "README.md", "docs", "installer", "scripts", "tests",
                   "undercover"], top
    assert sorted(f for f in files if f.startswith("docs/")) == ["docs/ARCHITECTURE.md", "docs/MASTERPLAN.md", "docs/THEMES.md"]
    # layout2 (user, 2026-10-17: "do it"): the tests and what they need in tests/, the things you run in scripts/
    assert sorted(f for f in files if f.startswith("tests/")) == ["tests/fake_tailscale.py", "tests/installer_e2e.ps1", "tests/smoke.py"]   # (installer_e2e: the installer's release check)
    assert sorted(f for f in files if f.startswith("scripts/")) == ["scripts/UnderCover.bat", "scripts/check_env.py", "scripts/cover.bat",
                                                                    "scripts/make_demo_voice.py", "scripts/reader_prints.py"]
    for f in ("installer/constraints.txt", "undercover/config.default.toml"):
        assert f in files, f
    ign = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "/config.toml" in ign.splitlines() and "/config.before-defaults*.toml" in ign.splitlines()
    iss = (ROOT / "installer" / "UnderCover.iss").read_text(encoding="utf-8-sig")
    assert '"..\\scripts\\check_env.py"; DestDir: "{app}\\scripts"' in iss and 'Source: "constraints.txt"; DestDir: "{app}\\installer"' in iss
    assert '"..\\config.toml"' not in iss, "the installer ships a config.toml again"
    setup = (ROOT / "installer" / "setup.ps1").read_text(encoding="utf-8-sig")
    assert setup.count("-m undercover.core.config set") == 2 and "WriteAllText($cfg" not in setup, "setup edits config.toml text again"
    assert "-m undercover.core.config get gpu backend" in setup
    cover = (ROOT / "scripts" / "cover.bat").read_text(encoding="utf-8")
    assert r'"%~dp0..\.venv\Scripts\python.exe" -m undercover.cli' in cover and "PYTHONPATH=%~dp0.." in cover
    assert "from undercover.core.config import CONFIG" in (ROOT / "scripts" / "check_env.py").read_text(encoding="utf-8")
    bat = (ROOT / "scripts" / "UnderCover.bat").read_text(encoding="utf-8")
    assert '".venv\\Scripts\\python.exe" -m undercover.server' in bat and 'cd /d "%~dp0.."' in bat, "UnderCover.bat must start from the app folder"
    # nothing still looks for the moved files at the root
    stale = []
    for f in files:
        if f.endswith((".py", ".ps1", ".iss", ".bat", ".js")) and not f.startswith("tools/research"):
            txt = (ROOT / f).read_text(encoding="utf-8-sig", errors="replace")
            if re.search(r'ROOT\s*/\s*"constraints\.txt"|"\.\.\\\\constraints\.txt"|ROOT\s*/\s*"check_env\.py"|ROOT\s*/\s*"UnderCover\.bat"|'   # (an installed app's tools\ holds uv, ffmpeg, Deno)
                         r'tools[/\\](smoke|golden|fake_tailscale|make_demo_voice|reader_prints|check_env|cover)\b', txt):
                stale.append(f)
    assert not stale, f"these still look for a moved file at the root: {stale}"
    return f"root: {', '.join(top)}; moves followed in the installer, setup, tools and code"


@check("the user's shortcuts follow UnderCover.bat into scripts\\ (layout2): once, at a real start, the desktop / Start menu / Startup shortcuts aimed at the old one are aimed at the new one; never in a test, a sandbox or a golden run, nor in an installed copy; a failure is tried again next start (mocked: the real shortcuts are never touched)")
def u_launcher_moved():
    import inspect
    import json as _json
    import subprocess
    import tempfile
    import undercover.server as entry
    from undercover.core import config, platform
    calls, real = [], platform.repoint_shortcuts
    outcome = {"raise": None}

    def fake(old, new, folders=None):
        assert folders is None   # the entry point asks for the user's own folders: this fake is all that answers it
        calls.append((Path(old), Path(new)))
        if outcome["raise"]:
            raise outcome["raise"]
        return [r"C:\\Users\\x\\Desktop\\UnderCover.lnk"]
    platform.repoint_shortcuts = fake
    try:
        with isolated_data():
            with tempfile.TemporaryDirectory(prefix="uc-launch-") as td:
                app = Path(td)
                (app / "scripts").mkdir()
                (app / "scripts" / "UnderCover.bat").write_text("@echo off\n", encoding="utf-8")
                assert entry.repoint_launchers(app, env={"UNDERCOVER_TEST_ROOTS": td}) is None and not calls, "a test run looked at shortcuts"
                assert entry.repoint_launchers(app, env={"UNDERCOVER_CONFIG": "x"}) is None and not calls, "a sandbox / golden run looked"
                outcome["raise"] = OSError("PowerShell said no")
                assert entry.repoint_launchers(app, env={}) is None and len(calls) == 1
                mark = config.path("cache_dir") / entry.LAUNCHER_MOVED
                assert not mark.exists(), "a failure was marked done"
                outcome["raise"] = None
                assert entry.repoint_launchers(app, env={}) == [r"C:\\Users\\x\\Desktop\\UnderCover.lnk"]
                assert calls[-1] == (app / "UnderCover.bat", app / "scripts" / "UnderCover.bat"), calls[-1]
                assert _json.loads(mark.read_text(encoding="utf-8"))["changed"], "the mark doesn't say what moved"
                assert entry.repoint_launchers(app, env={}) is None and len(calls) == 2, "looked twice"
                mark.unlink()
                (app / "UnderCover.bat").write_text("@echo off\n", encoding="utf-8")   # the old one still there: nothing to do
                assert entry.repoint_launchers(app, env={}) is None and len(calls) == 2
                inst = Path(td) / "installed"
                inst.mkdir()
                (inst / "UnderCover.cmd").write_text("@echo off\n", encoding="utf-8")   # an installed copy: no scripts\UnderCover.bat
                assert entry.repoint_launchers(inst, env={}) is None and len(calls) == 2
    finally:
        platform.repoint_shortcuts = real
    src = inspect.getsource(entry.main)
    assert src.index("_migrate()") < src.index("repoint_launchers()") < src.index("from .app.server import"), "start order"
    # the real helper on shortcuts of its own, in a temp folder (Windows only): only the ones aimed at the old file move
    if os.name != "nt":
        return "repointed once, never in tests / sandboxes / installed copies, retried after a failure (no Windows here for the .lnk round trip)"
    with tempfile.TemporaryDirectory(prefix="uc-lnk-") as td:
        d = Path(td)
        old, new, other = d / "UnderCover.bat", d / "scripts" / "UnderCover.bat", d / "Other.bat"
        sub = d / "Startup"
        platform.startup_entry(True, old, folder=d)                  # UnderCover.lnk -> the old .bat
        platform.startup_entry(True, old, folder=sub)                # one deeper, as "Start with Windows" sits in the Start menu
        (d / "keep").mkdir()
        platform.startup_entry(True, other, folder=d / "keep")      # a shortcut to something else
        changed = platform.repoint_shortcuts(old, new, folders=[d])
        assert sorted(Path(c).parent.name for c in changed) == sorted([d.name, "Startup"]), changed

        def target(link):
            r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                                f"(New-Object -ComObject WScript.Shell).CreateShortcut('{link}').TargetPath"],
                               capture_output=True, text=True, timeout=30)
            return Path(r.stdout.strip())
        assert target(d / "UnderCover.lnk") == new and target(sub / "UnderCover.lnk") == new
        assert target(d / "keep" / "UnderCover.lnk") == other, "a shortcut to another file moved"
        assert platform.repoint_shortcuts(old, new, folders=[d]) == []
    return "repointed once (the old .bat's shortcuts, deep too; others kept); never in tests / sandboxes / installed copies; retried after a failure"


@check("a character's check uses its scenes (RC pass 11 A2: 'plankton' read as a character, but its project was a singer's, other artists' album tracks made the 'real clips', every voice failed, and it trained): no album tracks for a character, its project speaks, an existing one is corrected (songs unticked, the user's own kept, the voices found again without them, scenes searched when there are none), the check prepares only scenes, and no clips to judge by is said plainly with the installed voice used unchecked, never a training")
def u_character_check():
    import inspect
    from undercover.core.voices import ModelError, get_voice
    from undercover.doubleagent import project as P, service, sources as S
    from undercover.doubleagent.sources import Source

    def album(i, user=False):
        return Source(id=f"al{i}", url="u", title=f"Plankton - Krapinoo {i}", channel="planktons - Topic", duration=200.0,
                      note="album track", extra={"sung": True}, user=user)

    def scene(i):
        return Source(id=f"sc{i}", url="u", title=f"Plankton's best moments {i} | SpongeBob", channel="Nickelodeon",
                      duration=600.0, extra={"kind": "scene"})
    # search(): a character never gets album tracks, whatever the mode
    class FakeYdl:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def extract_info(self, q, download=False):
            return {"entries": [{"id": f"v{i}", "title": f"Plankton scenes {i} | SpongeBob SquarePants", "channel": "Nickelodeon",
                                 "duration": 600.0} for i in range(5)]}
    real = (S._ydl, S.album_tracks, service.is_musician, S.search)
    S._ydl, S.album_tracks = FakeYdl, lambda name, n=20: [album(1), album(2), album(3)]
    service.is_musician = lambda person: True                           # YouTube Music has "Plankton" tracks
    try:
        found = S.search("plankton", "both", force="character")
        assert found and not any(S.is_song(x) for x in found), [x.title for x in found]
        searched = []

        def fake_search(name, mode="speaking", force=None, **k):
            searched.append((name, mode, force))
            return [scene(7), scene(8)]
        S.search = fake_search
        character = {"kind": "character", "show": "spongebob"}
        with isolated_data():
            # a new project for a character speaks, even though the name is on YouTube Music
            assert service.project_for("Plankton Fresh", subject=character).mode == "speaking"
            assert service.project_for("Singer Fresh").mode == "both"    # (a singer's still gets songs)
            # an existing project made as a singer's: 2 prepared album tracks, 1 prepared scene, 1 album the user added
            proj = _fake_project("plankton", "plankton", n_phrases=30, other=6, n_sources=3)
            a1, a2 = album(1), album(2)
            a1.id, a2.id = "s0", "s1"                                     # the prepared phrases' sources
            sc = scene(2)
            sc.id = "s2"
            mine = album(9, user=True)
            proj.sources = [a1, a2, sc, mine]
            for x in proj.sources[:3]:
                x.status = "ready"
            proj.mode = "both"
            proj.reference = {"cluster": 1, "clip": "p000", "confirmed": True, "auto": True}   # picked with the songs in
            proj.save()
            fixed = service.project_for("plankton", subject=character)
            by = {x.id: x for x in fixed.sources}
            assert fixed.mode == "speaking" and not by["s0"].selected and not by["s1"].selected, fixed.mode
            assert by["s0"].note == service.SONG_NOTE and by["s2"].selected and by["al9"].selected, "the user's own pick was unticked"
            assert not searched, "re-searched although the project has scenes"
            assert S.kinds(fixed)["s0"] == "song" and S.kinds(fixed)["s2"] == "scene"
            assert fixed.reference.get("scene_minutes"), f"the voice wasn't found again in the scenes: {fixed.reference}"
            assert not any(p.source in ("s0", "s1") for p in fixed.training_phrases())
            assert service.project_for("plankton", subject=character).sources == fixed.sources, "corrected twice"
            # one with no scenes at all: the show's scenes are searched and added, nothing removed
            only = _fake_project("squidward", "squidward", n_phrases=6, other=0, n_sources=1)
            only.sources = [album(4)]
            only.mode = "both"
            only.save()
            got = service.project_for("squidward", subject=character)
            assert searched == [("squidward", "speaking", "character")] and {x.id for x in got.sources} == {"al4", "sc7", "sc8"}
            # a project that has trained is left as it is
            done = _fake_project("larry", "larry", n_phrases=6, other=0, n_sources=1)
            done.sources, done.mode, done.train = [album(5)], "both", {"epoch": 40}
            done.save()
            assert service.project_for("larry", subject=character).mode == "both"
            # no clips to judge by: said plainly; the installed voice is used unchecked, else the job stops; never a training
            bare = P.Project.load("squidward")
            bare.reference = {"clip": "p000", "cluster": 1}
            assert "scenes" in service.clips_missing(bare, character)
            _voice_dir("plankton-spongebob", {})
            plan, said = {}, []
            v = service.no_check("plankton", "no clean clips of plankton from the show's scenes came through",
                                 [get_voice("plankton-spongebob")], plan, lambda *a: said.append(a[-1]))
            assert v.name == "plankton-spongebob" and plan["voice"]["checked"].startswith("not checked:") and "used as it is" in said[-1]
            try:
                service.no_check("plankton", "x", [], {}, lambda *a: None)
                raise AssertionError("no voice and no check went on")
            except ModelError as e:
                assert "Couldn't check any voice" in str(e)
    finally:
        S._ydl, S.album_tracks, service.is_musician, S.search = real
    src = inspect.getsource(service.resolve_voice)
    assert "clips_missing(proj" in src and src.index("clips_missing(proj") < src.index("def test(") and "no_check(" in src
    rsrc = inspect.getsource(service.reference_clips)
    assert "project_for(person, subject=subject)" in rsrc and 'k.get(s.id) == "scene" or s.user' in rsrc
    return "no songs for a character; its project speaks; songs unticked (user's kept), voices found again, scenes searched if none; plain when unchecked"


@check("Do it all's preparation reads as progress (IA pass 11 B2: '(1/2) / (2/2)' six times in 19 min, the time left climbing 3598 -> 4354 s): 'source k of n' over the whole job, the bar never goes back, the time left comes from the work and counts down, said through the job's rule; a check's verdict is kept per clip set (A3); an auto training says its planned end, not the cap")
def u_prep_progress():
    import inspect
    from undercover.core import jobs as J
    from undercover.doubleagent import night, service, trainer
    plan = {"prepare": 24, "train": 45, "compare": 1, "total": 70}
    t = service.PrepTime(plan, 30.0)
    lines, lefts, fracs = [], [], []
    kept = 0.0
    for rnd in range(6):                                   # 12 ticked sources, two a round, 120 s and 2.5 clean min each
        t.kept, t.total = kept, 12
        n = 2
        for f in (0.0, 0.5):
            lines.append(t.line(int(f * n) + 1, "downloading"))
            fracs.append(t.frac(0.02 + 0.08 * (t.done + f * n) / t.total))
        lefts.append(t.left(batch_left=n))
        t.finished(n, 240.0)
        kept += 5.0
    assert lines[0] == "Preparing clips, source 1 of 12: downloading" and lines[-1].startswith("Preparing clips, source 12 of 12"), lines
    assert all("(1/2)" not in x for x in lines)
    assert fracs == sorted(fracs), "the bar went backwards"
    assert all(a > b for a, b in zip(lefts, lefts[1:])), f"the time left didn't count down: {lefts}"
    assert abs(lefts[-1] - (2 * 120 + 46 * 60)) < 1, lefts[-1]          # the last batch, then training + compare
    assert service.PrepTime(None, 30).left(2) is None                    # no plan: the job extrapolates as before
    # more sources ticked later: the count grows, the bar stays
    t2 = service.PrepTime(plan, 30.0)
    t2.total = 2
    assert t2.frac(0.06) == 0.06 and t2.frac(0.04) == 0.06
    # the job says a step's own estimate through its rule: it counts down, and a rise is held to a few seconds
    job = J.Job("night")
    job.push(stage="voice", frac=0.05, message="x", left_est=1000.0)
    assert job.events[-1]["total_left"] == 1000.0
    job.push(stage="voice", frac=0.05, message="x", left_est=1400.0)
    assert job.events[-1]["total_left"] <= 1006.0, job.events[-1]["total_left"]
    job.push(stage="voice", frac=0.05, message="x", left_est=900.0)
    assert job.events[-1]["total_left"] == 900.0 and "left_est" not in job.events[-1]
    src = inspect.getsource(service.train_new_voice)
    assert "PrepTime(" in src and "left_est" in src and "kw['index']}/{kw['count']}" not in src
    assert "kw['index']}/{kw['count']}" not in inspect.getsource(night._improve)
    # A3: a cached verdict is keyed to the clips it was judged on
    with isolated_data():
        proj = _fake_project("a3", "A3 Person", n_phrases=12, other=0, n_sources=2)
        before = service.clip_set(proj)
        proj.source("s1").selected = False
        assert service.clip_set(proj) != before, "the clip set ignores what trains"
    rv = inspect.getsource(service.resolve_voice)
    assert 'cached.get("clips") == clips' in rv and "_remember(person, ident, q, clips)" in rv
    # the training's line: the planned end of an auto run, "up to" the cap past it
    assert trainer.epoch_line({"epoch": 10, "epochs": 1000, "planned": 525}) == "Training: epoch 10 of about 525"
    assert trainer.epoch_line({"epoch": 530, "epochs": 1000, "planned": 525}) == "Training: epoch 530, past the planned 525 (up to 1000)"
    assert trainer.epoch_line({"epoch": 10, "epochs": 300}) == "Training: epoch 10 of 300"
    tsrc = inspect.getsource(trainer.Trainer.run)
    assert tsrc.count("planned=planned_shown") == 2
    return f"source 1..12 of 12, bar forward, time left {lefts[0]:.0f} -> {lefts[-1]:.0f} s; rises held; verdicts per clip set; 'of about 525'"


@check("a queue that waits to try again says so (IA pass 11 B3, the backend's half): every item left waiting on a retry -> the status' start is the retry time, waits_until / waits_for are set, starts_now is off, done_by counts from the retry; each waiting row carries retry_at; the job's final event carries retry_at (Recent: 'will retry')")
def u_retry_status():
    import inspect
    from datetime import datetime
    from undercover.core import worklist
    real = worklist.datetime

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 18, 1, 10)
    worklist.datetime = Clock
    try:
        with isolated_data():
            worklist._STATE = None
            st = worklist._state()
            st["settings"].update(start_at="23:00", finish_by=None)
            st["armed"] = True
            at = datetime(2026, 10, 18, 2, 9).timestamp()
            st["queue"] = [{"id": "k1", "kind": "new", "target": "Someone", "state": "queued", "retry_after": at,
                            "estimate": {"total": 67}}]
            out = worklist.status()
            assert out["waits_until"] == "02:09" and out["waits_for"] == "YouTube" and not out["starts_now"], out
            assert out["start"] == "02:09" and out["done_by"] == "03:16", out
            assert out["queue"][0]["retry_at"] == "02:09"
            st["queue"].append({"id": "k2", "kind": "new", "target": "Another", "state": "queued", "estimate": {"total": 10}})
            out = worklist.status()                                    # something can run now: no wait said
            assert out["waits_until"] is None and out["starts_now"] and out["start"] == "01:10", out
            worklist._STATE = None
    finally:
        worklist.datetime = real
    assert 'retry_at=time.strftime("%H:%M", time.localtime(at))' in inspect.getsource(worklist._run_item)
    return "all waiting: starts 02:09, done 03:16, waits_until/waits_for/retry_at; one runnable: starts now"


@check("the installer's Python stays private and survives a refused link (the user's 1.0 install test: 'untrusted mount point', os error 448): uv installs it with --no-bin --no-registry; setup finds the full-version folder itself (never the minor link), checks it runs, and makes the venv from it; a failed install with a working Python goes on, else one retry then a plain stop; earlier registry entries inside this install are removed; selftest warns if the venv sits on anything else; the add-ons page has one checkbox per add-on that accepts its licence, with its own link")
def u_installer_python():
    import importlib.util
    import re
    import subprocess
    import tempfile
    inst = ROOT / "installer"
    setup = (inst / "setup.ps1").read_text(encoding="utf-8-sig")
    # rc-fix6: setup unpacks the pinned python-build-standalone archive itself (no uv python install: no shim, no registry,
    # no minor-version link at all)
    assert not re.findall(r"@\('python', 'install'", setup) and "$BasePy = Join-Path $pyHome 'python.exe'" in setup
    # the venv: Python's own venv module from the full-version python.exe (uv's venvs sit on the minor link: the user's
    # second run failed at PyTorch with os error 448 on .venv\Scripts\python.exe); one built on the link is made again
    assert "Run $BasePy @('-m', 'venv', '--clear', '--without-pip', $Venv)" in setup and "@('venv'," not in setup
    assert "$venvGood = (-not $DryRun) -and (Test-Path $Py) -and (Test-VenvBase $Venv $PythonVer)" in setup
    assert "if ($Repair -and $venvGood)" in setup and "if ($venvGood -and -not $Force)" in setup
    assert "DisableDirPage=no" in (inst / "UnderCover.iss").read_text(encoding="utf-8-sig")
    assert ". (Join-Path $PSScriptRoot 'python.ps1')" in setup and "Test-OwnPython $BasePy $PythonVer" in setup
    assert "Remove-OwnPythonRegistry $InstallDir" in setup
    assert "--default" not in setup, "uv must never make this Python the user's default"
    iss = (inst / "UnderCover.iss").read_text(encoding="utf-8-sig")
    assert 'Source: "python.ps1"; DestDir: "{app}\\installer"' in iss
    helper = (inst / "python.ps1").read_text(encoding="utf-8")
    assert "StartsWith($root, [StringComparison]::OrdinalIgnoreCase)" in helper, "the registry cleanup must be limited to this install"
    if os.name == "nt":
        with tempfile.TemporaryDirectory(prefix="uc-py-") as td:
            d = Path(td)
            (d / "cpython-3.12-windows-x86_64-none").mkdir()           # the minor link (here a plain folder)
            (d / "cpython-3.12-windows-x86_64-none" / "python.exe").write_bytes(b"x")

            def ps(cmd):
                r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                                    f". '{inst / 'python.ps1'}'; {cmd}"], capture_output=True, text=True, timeout=60)
                return r.stdout.strip()
            assert ps(f"Get-OwnPython '{d}' '3.12.10'") == "", "the minor link was taken for the install"
            (d / "cpython-3.12.10-windows-x86_64-none").mkdir()
            (d / "cpython-3.12.10-windows-x86_64-none" / "python.exe").write_bytes(b"x")
            assert ps(f"Get-OwnPython '{d}' '3.12.10'") == str(d / "cpython-3.12.10-windows-x86_64-none" / "python.exe")
            ver = "%d.%d.%d" % sys.version_info[:3]
            assert ps(f"Test-OwnPython '{sys.executable}' '{ver}'") == "True", "a working Python wasn't recognised"
            full = d / "cpython-3.12.10-windows-x86_64-none"
            for home, want in ((full, "True"), (d / "cpython-3.12-windows-x86_64-none", "False"), (d / "nowhere", "False")):
                v = d / f"venv-{want}-{home.name}"
                v.mkdir()
                (v / "pyvenv.cfg").write_text(f"home = {home}" + chr(10) + "include-system-site-packages = false" + chr(10), encoding="utf-8")
                assert ps(f"Test-VenvBase '{v}' '3.12.10'") == want, (home, want)
            assert ps(f"Test-VenvBase '{d / 'no-venv'}' '3.12.10'") == "False"
            # the minor-version link: a junction (no admin needed) is removed as a link only, its target untouched;
            # a usable one stays unless asked; a real folder of that name is never removed
            link = d / "cpython-3.12-windows-x86_64-none"
            import shutil as _sh
            _sh.rmtree(link)                                            # (the plain folder made above)
            subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(full)], capture_output=True, check=True)
            assert ps(f"@(Remove-MinorLink '{d}' '3.12.10').Count") == "0", "a usable link was removed unasked"
            assert ps(f"Remove-MinorLink '{d}' '3.12.10' -EvenIfUsable") == link.name
            assert not link.exists() and (full / "python.exe").exists(), "removing the link touched its target"
            link.mkdir()
            assert ps(f"@(Remove-MinorLink '{d}' '3.12.10' -EvenIfUsable).Count") == "0" and link.is_dir(), "a real folder was removed"
            setup = (inst / "setup.ps1").read_text(encoding="utf-8-sig")
            assert "Remove-MinorLink $env:UV_PYTHON_INSTALL_DIR $PythonVer" in setup
            assert ps(f"Test-OwnPython '{sys.executable}' '3.9.0'") == "False" and ps(f"Test-OwnPython '{d / 'cpython-3.12.10-windows-x86_64-none' / 'python.exe'}' '3.12.10'") == "False"
    # selftest: the venv must sit on the installer's own full-version Python (an installed copy only)
    spec = importlib.util.spec_from_file_location("uc_selftest", inst / "selftest.py")
    st = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(st)
    with tempfile.TemporaryDirectory(prefix="uc-app-") as td:
        app = Path(td)
        assert st.private_python(r"C:\Python312", app, "3.12.10") == []          # a dev copy: nothing to check
        (app / "tools" / "python").mkdir(parents=True)
        own = app / "tools" / "python"
        assert st.private_python(str(own / "cpython-3.12.10-windows-x86_64-none"), app, "3.12.10") == []
        assert "full 3.12.10" in st.private_python(str(own / "cpython-3.12-windows-x86_64-none"), app, "3.12.10")[0]
        assert "not on UnderCover's own Python" in st.private_python(r"C:\Users\x\AppData\Local\Programs\Python\Python312", app, "3.12.10")[0]
    # the add-ons page: one checkbox per add-on that accepts its licence, its own link; one short line from the registry
    assert "AcceptBox" not in iss and "I have read the licences" not in iss
    assert "'Add ' + Name + ' and accept its licence'" in iss and "'Read the licence'" in iss and "Link.Tag := I" in iss
    build = (inst / "build.ps1").read_text(encoding="utf-8-sig")
    assert "[char]0xB7" in build and all(ord(c) < 128 for c in build), "build.ps1 must stay ASCII (Windows PowerShell reads it as ANSI)"
    from undercover.core import addons
    f5 = addons.ADDONS["f5"]
    assert f5.short and f5.use and "4x" not in f5.adds and "non-commercial" in f5.use, (f5.adds, f5.short, f5.use)
    assert {"short", "use"} <= set(addons.info(f5))
    return "--no-bin --no-registry; the full-version folder found, the link never; runs-and-version check; selftest rule; one licence checkbox per add-on"


@check("a failed setup never reads as installed (the user's 1.0 install test: setup.ps1 failed, and the wizard said 'UnderCover is installed' with 'Start UnderCover now'): setup.ps1 exits non-zero and leaves 'failed: <what>' in logs/setup-result.txt ('ok' only at the end); the installer runs it from its code, reads the exit code, makes the launch shortcuts only after it worked, offers 'Start UnderCover now' only then, and otherwise says what stopped, where the log is, and Try again; uninstall removes the shortcuts it made")
def u_installer_failure():
    import subprocess
    import tempfile
    inst = ROOT / "installer"
    setup = (inst / "setup.ps1").read_text(encoding="utf-8-sig")
    assert 'Result "failed: $msg"' in setup and 'Result "ok"' in setup
    assert setup.index('Result "ok"') > setup.index("Writing the install record"), "'ok' must be the last thing setup says"
    assert "Remove-Item -LiteralPath $ResultFile" in setup, "an earlier run's answer must not stand"
    iss = (inst / "UnderCover.iss").read_text(encoding="utf-8-sig")
    run = iss[iss.index("[Run]"):iss.index("[UninstallDelete]")]
    assert 'Filename: "powershell.exe"' not in run, "setup.ps1 runs unchecked from [Run] again"
    assert 'Description: "Start UnderCover now"' in run and "Check: SetupWorked" in run
    icons = iss[iss.index("[Icons]"):iss.index("[Tasks]")]
    assert 'Filename: "{app}\\UnderCover.cmd"' not in icons, "a launch shortcut is made before setup has worked"
    code = iss[iss.index("[Code]"):]
    assert "if CurStep = ssPostInstall then RunSetup;" in code and "SetupCode := StrToIntDef(Trim(String(Said)), -1)" in code
    assert "if SetupOK then CreateLaunchers;" in code and "'Try again'" in code and "setup-result.txt" in code
    assert "isn''t ready yet" in code and "logs\\setup.log" in code
    # the launch shortcuts go at uninstall, only those that start this install (u_installer_quiet)
    assert "RemoveOurLinks(ExpandConstant('{group}'))" in code and "RemoveOurLinks(ExpandConstant('{userdesktop}'))" in code
    assert all(ord(c) < 128 for c in iss + setup), "the installer's scripts must stay ASCII"
    if os.name != "nt":
        return "the code path (no Windows here to run setup.ps1)"
    # setup.ps1 failing for real, in a folder of its own (it stops before any download: no app code there)
    with tempfile.TemporaryDirectory(prefix="uc-setupfail-") as td:
        r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(inst / "setup.ps1"),
                            "-InstallDir", td], capture_output=True, text=True, timeout=120)
        res = Path(td) / "logs" / "setup-result.txt"
        assert r.returncode != 0, "a failed setup exited 0"
        assert res.read_text(encoding="utf-8").startswith("failed: the app code isn't in"), res.read_text(encoding="utf-8")
    return "setup exits 1 with 'failed: …'; the wizard reads it: no launchers, no 'Start now', what stopped + log + Try again"


@check("the installer is honest about size and shows its progress (installer-fix4, the user: 'it just says it's like 84mb ... extremely misleading', 'to a dumb user it will look stuck'): installer\\space.ps1's need per drive from the measured sizes with a 20 % margin, each drive's figure the sum of what it lists, the shortfall said plainly (free space stubbed); setup checks before downloading; the wizard states the real size, checks each page's drives, runs setup hidden and drives its bar from setup's status lines, with Show details and Cancel")
def u_installer_space():
    import json as _json
    import math
    import tempfile
    import re
    import subprocess
    inst = ROOT / "installer"
    sizes = _json.loads((inst / "sizes.json").read_text(encoding="utf-8"))
    assert sizes["margin"] >= 1.2 and sizes["python"]["rocm"] >= 6.4 and sizes["models_gb"] >= 8.2, "below what was measured"
    if os.name == "nt":
        def ps(body):
            r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                                f". '{inst / 'space.ps1'}'; $s = Read-Sizes '{inst}'; {body}"], capture_output=True, text=True, timeout=60)
            assert r.returncode == 0, r.stderr
            return r.stdout.strip()
        # dot-sourced by setup.ps1: it must leave setup's own variables alone (a $InstallDir parameter here emptied
        # setup's, and setup died right after finding the graphics card: the first release check of installer-fix4)
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                            f"$InstallDir = 'KEEP'; $Backend = 'rocm'; $Out = 'x'; . '{inst / 'space.ps1'}'; $InstallDir + '|' + $Backend + '|' + $Out"],
                           capture_output=True, text=True, timeout=60)
        assert r.stdout.strip() == "KEEP|rocm|x", f"dot-sourcing space.ps1 changed the caller's variables: {r.stdout.strip()!r}"
        one = ps("$n = Get-SpaceNeeds 'D:\\UC' '' '' '' 'rocm' $s; $n['D:\\'].need")
        M = sizes["models_gb"]   # the AI models setup installs (rc-fix4b)
        g = lambda gb: f"{gb:g}"   # noqa: E731  (PowerShell writes 22, not 22.0)
        parts = [0.1 + 0.5 + 6.5, M, 0.5, 1.0]
        up = lambda gb: math.ceil(gb * sizes["margin"] * 2) / 2          # noqa: E731
        total = sum(up(x) for x in parts)
        assert float(one) == total, (one, [up(x) for x in parts])
        split = ps("$n = Get-SpaceNeeds 'C:\\UC' 'D:\\Lib' 'D:\\Work' 'E:\\M' 'cpu' $s @('f5'); ($n.Keys | ForEach-Object { $_ + '=' + $n[$_].need }) -join ';'")
        g = lambda gb: f"{gb:g}"   # noqa: E731  (PowerShell writes 3, not 3.0)
        assert split == f"C:\\={g(up(0.1 + 0.5 + 1.5))};E:\\={g(up(M + 1.5))};D:\\={g(up(0.5) + up(1.0))}", split
        setup_only = ps("$n = Get-SpaceNeeds 'D:\\UC' 'E:\\L' 'E:\\W' 'E:\\M' 'rocm' $s -SetupOnly; $n.Keys -join ';'")
        assert setup_only == "D:\\;E:\\", "setup's own check: the app on D:, the models it downloads on E:, never the files that come later"
        unknown = ps("(Get-SpaceNeeds 'D:\\UC' '' '' '' '' $s -SetupOnly)['D:\\'].need")
        assert float(unknown) == up(0.1 + 0.5 + max(sizes["python"].values())) + up(M), "an unknown card isn't sized as the largest"
        short = ps("$n = Get-SpaceNeeds 'D:\\UC' '' '' '' 'rocm' $s; (Find-Shortfalls $n { param($r) 12 } | ForEach-Object { Format-Shortfall $_ }) -join '/'")
        assert short == f"D: has 12 GB free, and UnderCover needs about {g(total)} GB there.", short
        assert ps("$n = Get-SpaceNeeds 'D:\\UC' '' '' '' 'rocm' $s; @(Find-Shortfalls $n { param($r) 500 }).Count") == "0"
        assert ps("$n = Get-SpaceNeeds 'D:\\UC' '' '' '' 'rocm' $s; @(Find-Shortfalls $n { param($r) -1 }).Count") == "0", "an unreadable drive was called full"
        # the pages' two short lines (the user: the paragraph was "excessively verbose, people will skip")
        lines = ps("$n = Get-SpaceNeeds 'D:\\UC' '' '' '' 'rocm' $s; (Format-NeedLines $n { param($r) 190.5 }) -join '/'")
        assert lines == f"UnderCover needs about {g(total)} GB on D: (190 GB free).", lines
        parts = ps("$n = Get-SpaceNeeds 'D:\\UC' '' '' '' 'rocm' $s; (Format-Parts $n) -join '/'")
        assert parts == f"App {g(up(7.1))} GB/AI models {g(up(M))} GB/your files {g(up(0.5) + up(1.0))} GB", parts
        split2 = ps("$n = Get-SpaceNeeds 'C:\\UC' 'D:\\L' 'D:\\W' 'D:\\M' 'rocm' $s; (Format-NeedLines $n { param($r) 5.47 }) -join '/'")
        assert split2 == (f"UnderCover needs about {g(up(7.1))} GB on C: (5.4 GB free)./UnderCover needs about "
                          f"{g(up(M) + up(0.5) + up(1.0))} GB on D: (5.4 GB free)."), split2
        assert ps("$n = Get-SpaceNeeds 'D:\\UC' '' '' '' 'rocm' $s; (Format-NeedLines $n { param($r) -1 }) -join '/'") == f"UnderCover needs about {g(total)} GB on D:."
        # what's there already comes off (a re-run after Cancel, a reinstall): 3 GB of venv/cache, all the models
        BS = chr(92)
        have = "{ param($k, $d) if ($k -eq 'setup') { 3 } else { 9 } }"
        part = ps("$n = Get-SpaceNeeds 'D:" + BS + "UC' '' '' '' 'rocm' $s -Have " + have + "; $n['D:" + BS + "'].need")
        assert float(part) == up(0.1 + 0.5 + 6.5 - 3) + up(0.5) + up(1.0), ("a re-run is asked for what it has", part)
        done = ps("$n = Get-SpaceNeeds 'D:" + BS + "UC' '' '' '' 'rocm' $s -SetupOnly -Have { param($k, $d) 50 }; $n.Count")
        assert done == "0", "a finished install still asks for room"
        assert "already set up" in ps("$n = Get-SpaceNeeds 'D:" + BS + "UC' '' '' '' 'rocm' $s -SetupOnly -Have { param($k, $d) 50 }; (Format-NeedLines $n { param($r) 5 }) -join '/'")
        tmp = Path(tempfile.mkdtemp(prefix="uc-space-"))
        try:
            (tmp / ".venv" / "Lib").mkdir(parents=True)
            (tmp / ".venv" / "Lib" / "a.bin").write_bytes(bytes(1024 * 1024))
            (tmp / "cache" / "uv").mkdir(parents=True)
            (tmp / "cache" / "uv" / "b.bin").write_bytes(bytes(512 * 1024))
            (tmp / "models").mkdir()
            got = ps(f"[math]::Round((Get-HaveGB 'setup' '{tmp}') * 1GB); [math]::Round((Get-HaveGB 'models' '{tmp}') * 1GB); Get-DirBytes '{tmp}" + BS + "nothing'")
            assert got.split() == [str(1024 * 1024 + 512 * 1024), "0", "0"], got
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        isi = (inst / "sizes.isi").read_text(encoding="utf-8")
        total = ps("$n = Get-SpaceNeeds 'C:\\UnderCover' '' '' '' '' $s; $t = 0; foreach ($k in $n.Keys) { $t += $n[$k].need }; Format-GB $t")
        assert f'#define SizeTotalGB "{total}"' in isi, ("sizes.isi is stale: run installer\\build.ps1", total, isi)
    setup = (inst / "setup.ps1").read_text(encoding="utf-8-sig")
    assert setup.count("RunWatched $Uv") == 1, "the packages' uv step runs without progress"
    assert "Get-SpaceNeeds $InstallDir '' '' $ModelsDir $Backend $Sizes @() -SetupOnly -Have { param($k, $d) Get-HaveGB $k $d }" in setup and "Fail (\"there isn't enough space. \"" in setup
    assert setup.index("Find-Shortfalls $needs") < setup.index("# 2. The files"), "setup checks the space after it downloads"
    assert "Test-Path $Py" not in setup[setup.index("Space: what setup"):setup.index("Find-Shortfalls $needs")], "a re-run after Cancel skips the space check"
    assert "function DirBytes" not in setup and "Get-DirBytes $watch" in setup
    # an untyped 0 makes PowerShell pick Max(Int32, Int32): sizes past 2 GB fail, 7.1 GB becomes 7 (a real install died)
    assert not re.search(r"\[math\]::M(ax|in)\(0,", setup + (inst / "space.ps1").read_text(encoding="utf-8")), "an untyped 0 in [math]::Max/Min"
    assert "(Test-Path -LiteralPath $CancelFile)) { Cancelled }" in setup and "& taskkill.exe /PID $p.Id /T /F" in setup, "Cancel is not checked between steps, or a running download is not stopped"
    assert 'Result "cancelled: $kept"' in setup and "exit 3" in setup
    assert "[Console]::Out.WriteLine($line)" in setup and "Add-Content -Path $StatusFile" in setup
    iss = (inst / "UnderCover.iss").read_text(encoding="utf-8-sig")
    run = iss[iss.index("function RunSetup"):iss.index("procedure CurStepChanged")]
    # rc-fix8 (the first NVIDIA install: the wizard held still for minutes while PyTorch unpacked): setup starts through
    # WScript.Shell, hidden, not waited for; the wizard follows setup-status.txt and pumps its own messages; the exit
    # code comes from setup-exit.txt, and the process gone without one is a failure
    assert "ExecAndLogOutput" not in run and "Exec(" not in run, "Inno disables the wizard while a program it started runs"
    assert "Shell := CreateOleObject('WScript.Shell');" in run and "Shell.Run(Cmd, 0, False);" in run
    assert "PumpMessages;" in run and "FollowStatus(StatusFile, Seen);" in run and "ProcessEnded(Pid)" in run
    assert "' -InstallDir ' + PSQuote(ExpandConstant('{app}'))" in run and "if GetAddons('') <> '' then Params := Params + ' -Addons ' + PSQuote(GetAddons(''));" in run
    assert "' $LASTEXITCODE; Move-Item -LiteralPath ' + PSQuote(ExitFile + '.part')" in run and "Set-Content -LiteralPath ' + PSQuote(PidFile) + ' $PID; '" in run
    assert "if LoadStringsFromFile(StatusFile, Lines) then Seen := GetArrayLength(Lines);" in run, "a Try again replays the last run's steps"
    hold = run[run.index("#ifdef ReleaseCheck"):run.index("#endif")]
    assert "{param:setuphold|0}" in hold and "Start-Sleep -Seconds" in hold, "the release check can't hold setup long enough to judge"
    assert "WizardForm.CancelButton.Enabled := False;" in run
    assert run.index("SaveStringToFile(Logs + '" + chr(92) + "setup.log'") < run.index("Shell.Run(Cmd, 0, False);"), "Show details opens nothing until setup writes its log"
    pump = iss[iss.index("procedure PumpMessages"):iss.index("function ProcessEnded")]
    assert "while PeekMessage(Msg, 0, 0, 0, 1) do begin" in pump and "DispatchMessage(Msg);" in pump
    assert "IntToStr(DownloadPage.AbortButton.Handle) + ' ' + IntToStr(DetailsLink.Handle));" in iss
    assert "-Pause" not in run, "the hidden setup would wait for Enter forever"
    assert "WizardForm.ProgressGauge.Position := Progress" in iss and "if Pos1 > Progress then Progress := Pos1" in iss
    assert "Show details" in iss and "logs\\setup.log" in iss and "DownloadPage.Show;" in iss
    assert "WizardForm.DiskSpaceLabel.Caption := Need;" in iss and "DirParts.Caption := Parts;" in iss and "{#SizeTotalGB} GB of free space in all" in iss
    assert "StringChangeEx(S, '|', ' ' + #183 + ' ', True);" in iss, "the parts line isn't joined with a middle dot"
    assert "FilesNeed.Caption := Need;" in iss and iss.count("OnChange := @DataEditChange;") == 3 and "CreateCallback(@SpaceTimerFired)" in iss
    # installer-fix5: a bar for the current step under the main one (bytes, or a marquee); Show details below both
    assert "StepGauge := TNewProgressBar.Create(WizardForm);" in iss and "StepGauge.Style := npbstMarquee" in iss
    assert "StepGauge.Top := WizardForm.ProgressGauge.Top + WizardForm.ProgressGauge.Height" in iss
    assert "DetailsLink.Top := StepGauge.Top + StepGauge.Height" in iss, "Show details sits on a bar (it did, and a click there did nothing)"
    assert "notepad.exe" in iss[iss.index("procedure OpenSetupLog"):iss.index("function Field")]
    # the uninstaller's last word: the folders it kept (moved ones read from config.toml), and why
    un = iss[iss.index("function KeptPath"):iss.index("procedure CurUninstallStepChanged")]
    for key in ("'uploads_dir'", "'output_dir'", "'voices_dir'", "'cache_dir'", "'models_dir'"):
        assert key in iss[iss.index("procedure CurUninstallStepChanged"):], key
    assert "config.toml" in un and "StringChangeEx(S, '" + chr(92) * 2 + "', '" + chr(92) + "', True);" in un
    assert "(CurUninstallStep = usPostUninstall) and (KeptMsg <> '') and (not UninstallSilent)" in iss
    # the shipped docs give the measured size (installer/sizes.json through sizes.isi), not an old guess
    tot = re.search(r'#define SizeTotalGB "([0-9.]+)"', (inst / "sizes.isi").read_text(encoding="utf-8")).group(1)
    # the download per graphics card (rc-polish1: one size, AMD's, was said for every card): sizes.isi = downloads.tsv's
    # rows, and the docs say each
    isi_txt = (inst / "sizes.isi").read_text(encoding="utf-8")
    dl = {k: re.search(rf'#define SizeDownload{k} "([0-9.]+)"', isi_txt).group(1) for k in ("AMD", "NVIDIA", "CPU")}
    by: dict = {}
    for r in (inst / "downloads.tsv").read_text(encoding="utf-8").splitlines()[1:]:
        if r.strip():
            by[r.split("\t")[0]] = by.get(r.split("\t")[0], 0) + int(r.split("\t")[4] or 0)
    common = by["base"] + by["packages"] + by["models"]
    want = {"AMD": common + by["torch-rocm"], "NVIDIA": common + max(by["torch-cuda128"], by["torch-cuda126"]), "CPU": common + by["torch-cpu"]}
    for k, n in want.items():
        assert dl[k] == f"{round(n / 1e9, 1):.1f}", f"sizes.isi's {k} download {dl[k]} GB isn't downloads.tsv's {n / 1e9:.2f} GB (run build.ps1)"
    assert "SizeDownloadGB" not in iss and "function UpdateReadyMemo" in iss and "Pending(Lines[I], Bytes)" in iss[iss.index("function UpdateReadyMemo"):]
    assert "if CurPageID = wpReady then Hook('ready.txt', WizardForm.ReadyMemo.Text);" in iss, "the release check can't read the Ready page"
    for name, doc in (("README", (ROOT / "README.md").read_text(encoding="utf-8")), ("GUIDE.md", (inst / "GUIDE.md").read_text(encoding="utf-8"))):
        assert f"About {tot} GB of free disk space" in doc, f"{name}'s disk size isn't sizes.isi's {tot} GB"
        flat = " ".join(doc.split())
        assert (f"downloads about {dl['AMD']} GB with an AMD card, {dl['NVIDIA']} GB with an NVIDIA card or {dl['CPU']} GB without a supported card"
                in flat), f"{name}'s download sizes aren't sizes.isi's {dl} (rc-fix4b: setup downloads everything)"
        assert "{{TIME}}" not in doc and "first time each feature" not in doc and "First assignment" not in doc, name
    nb = iss[iss.index("function NextButtonClick"):iss.index("function ShouldSkipPage")]
    assert nb.count("Result := SpaceOK(Short)") == 3 and "wpSelectDir" in nb and "DataPage.ID" in nb and "AddonPage.ID" in nb
    assert 'Source: "space.ps1"; Flags: dontcopy' in iss and '#include "sizes.isi"' in iss
    assert not re.search(r"(?m)^\s*#13", iss), "a line starting with # is a preprocessor directive"
    assert all(ord(c) < 128 for c in iss + setup), "the installer's scripts must stay ASCII"
    e2e = (ROOT / "tests" / "installer_e2e.ps1").read_text(encoding="utf-8")
    assert "steps never reported" in e2e and "-CancelCheck" in e2e
    # rc-fix6: the release check runs the real installer (its test variant), so it can't miss a file the installer ships
    assert "& $iscc /Q /DReleaseCheck=1 \"/O$launch\" (Join-Path $repo 'installer\\UnderCover.iss')" in e2e and "$issFiles" not in e2e
    return f"one drive: {one} GB (22 = 9 + 10.5 + 1 + 1.5); a short drive said plainly; setup hidden, its status driving the bar; Cancel"


@check("the installer shows only plain steps and touches only its own shortcuts, and a second copy is said plainly (installer-fix4, from the user's install: 'Press Enter to close' blocked the wizard, an add-on printed one line per MB, JSON reached the screen; a fresh copy's 'Start now' opened the user's everyday copy; uninstalling a test install would have deleted the everyday copy's desktop shortcut)")
def u_installer_quiet():
    import http.server
    import io
    import re
    import subprocess
    import tempfile
    import threading
    from undercover import addons as addons_cli
    from undercover.app import server
    # an add-on's install, as the installer sees it: one plain line per file, at most one status line a second
    out, now = io.StringIO(), [0.0]
    prog = addons_cli.status_printer(108, out=out, clock=lambda: now[0])
    prog("Installing F5-TTS's Python packages", 0, 0)
    for f in ("model.bin", "vocab.txt"):
        for mb in range(1, 55):
            now[0] += 0.25
            prog(f"Downloading F5-TTS: {f}", mb * 1_000_000, 54_000_000)
    lines = out.getvalue().splitlines()
    plain = [x for x in lines if not x.startswith("@@P ")]
    assert plain == ["Installing F5-TTS's Python packages", "Downloading F5-TTS: model.bin (54 MB)",
                     "Downloading F5-TTS: vocab.txt (54 MB)"], plain
    pm = [int(re.match(r"@@P (\d+)\|", x).group(1)) for x in lines if x.startswith("@@P ")]
    assert 20 <= len(pm) <= 35, f"{len(pm)} status lines for 27 s of downloading (one a second, plus each file's end)"
    assert pm == sorted(pm) and pm[-1] == 990 and pm[len(pm) // 2] > 300, pm
    assert "@@P 500|Downloading F5-TTS: model.bin: 54 of 54 MB" in lines, "a file's end is always said"
    # a small file (a model's config) says no MB at all: it read "0 of 0 MB" (MasterPlan, rc-fix5b)
    small_out = io.StringIO()
    small = addons_cli.status_printer(1, out=small_out, clock=lambda: 0.0)
    small("Downloading the separation models (file 15 of 29)", 1726, 1726)
    said = small_out.getvalue().splitlines()
    assert said == ["Downloading the separation models (file 15 of 29)", "@@P 1|Downloading the separation models (file 15 of 29)"], said
    cli = Path(addons_cli.__file__).read_text(encoding="utf-8")
    assert "return 3" in cli and "--cancel-file" in cli and "except InterruptedError" in cli

    # the port is taken: by this copy (open it), another copy (say so, never open it as this one), or another program
    class Fake(http.server.BaseHTTPRequestHandler):
        answer = None

        def do_GET(self):   # noqa: N802
            body = json.dumps(Fake.answer).encode() if Fake.answer is not None else b"<html>not here</html>"
            self.send_response(200 if Fake.answer is not None else 404)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), Fake)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        Fake.answer = {"build": "x", "app_folder": "X:\\Other <copy>"}
        other = server.who_has_port("127.0.0.1", port)
        assert other == {"kind": "other", "folder": "X:\\Other <copy>"}, other
        head, advice = server.port_taken_text(port, other)
        assert head == "Another copy of UnderCover is already running (X:\\Other <copy>)." and "Quit that one first" in advice
        page = server.port_taken_page(port, other)
        assert "X:\\Other &lt;copy&gt;" in page and f'href="http://127.0.0.1:{port}/">Open that copy' in page
        Fake.answer = {"build": "x", "app_folder": str(server.ROOT)}
        assert server.who_has_port("127.0.0.1", port) == {"kind": "this"}
        Fake.answer = {"build": "x"}   # an UnderCover from before app_folder
        assert server.port_taken_text(port, server.who_has_port("127.0.0.1", port))[0] == "Another copy of UnderCover is already running."
        Fake.answer = None
        foreign = server.who_has_port("127.0.0.1", port)
        assert foreign["kind"] == "foreign", foreign
        if os.name == "nt":
            assert (foreign["owner"] or "").lower() in ("python.exe", "pythonw.exe"), ("the port's holder isn't named", foreign)
        head = server.port_taken_text(port, foreign)[0]
        assert head.startswith("Another program") and f"port {port}" in head and "Open that copy" not in server.port_taken_page(port, foreign)
    finally:
        srv.shutdown()
        srv.server_close()
    src = Path(server.__file__).read_text(encoding="utf-8")
    start = src[src.index("    if srv is None:"):src.index("    srv.daemon_threads = True")]
    assert "_say_port_taken(host, port)" in start and "webbrowser" not in start, "a taken port opens the page there, whoever has it"

    # setup: nothing waits for input, no tool output or JSON on screen, the add-on's progress drives the bar
    inst = ROOT / "installer"
    setup = (inst / "setup.ps1").read_text(encoding="utf-8-sig")
    run_code = setup[setup.index("function RunCode"):setup.index("function Run(")]
    assert "Out-Host" not in run_code and "Write-Host \"    [dry-run]" in run_code, "a tool's output reaches the screen"
    assert "'--status', '--cancel-file', $CancelFile" in setup and "if ($code -eq 3) { Cancelled }" in setup
    assert "RunStatus $Py @('-m', 'undercover.addons', 'install'" in setup and "Run $Py @('-m', 'undercover.addons'" not in setup
    assert "1-3 GB" not in setup, "the closing words still guess the models' size"
    if os.name == "nt":   # setup's own RunStatus, run for real on a child printing an add-on's progress
        fn = setup[setup.index("function RunStatus"):]
        fn = fn[:fn.index("\n}\n") + 3]
        tmp = Path(tempfile.mkdtemp(prefix="uc-runstatus-"))
        try:
            child = tmp / "child.py"
            child.write_text("import sys\nprint('Downloading F5-TTS: model.bin (54 MB)', flush=True)\n"
                             "print('@@P 250|Downloading F5-TTS: model.bin: 27 of 54 MB', flush=True)\n"
                             "print('a warning', file=sys.stderr, flush=True)\n"
                             "print('@@P 990|Downloading F5-TTS: model.bin: 54 of 54 MB', flush=True)\nsys.exit(3)\n", encoding="utf-8")
            ps1 = tmp / "t.ps1"
            ps1.write_text("$ErrorActionPreference = 'Stop'\n$DryRun = $false\n$script:logged = @(); $script:said = @()\n"
                           "function Log([string]$m) { $script:logged += $m.Trim() }\n"
                           "function Status([string]$d = '', [double]$f = -1) { $script:said += ('{0:0.000}|{1}' -f $f, $d) }\n"
                           + fn.replace("\r\n", "\n") +
                           f"\n$code = RunStatus '{sys.executable}' @('{child}') 0.5 0.5\n"
                           "'code=' + $code\n$script:said | ForEach-Object { 'said=' + $_ }\n$script:logged | ForEach-Object { 'log=' + $_ }\n",
                           encoding="utf-8")
            r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(ps1)],
                               capture_output=True, text=True, timeout=60)
            got = r.stdout.splitlines()
            assert "code=3" in got, (got, r.stderr)   # the add-on's "stopped" reaches setup, which then says Cancelled
            assert [x for x in got if x.startswith("said=")] == ["said=0.625|Downloading F5-TTS: model.bin: 27 of 54 MB",
                                                                 "said=0.995|Downloading F5-TTS: model.bin: 54 of 54 MB"], got
            logs = [x for x in got if x.startswith("log=")]
            assert sorted(logs[1:]) == ["log=Downloading F5-TTS: model.bin (54 MB)", "log=a warning"] and logs[0].startswith("log=RUN "), got
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    iss = (inst / "UnderCover.iss").read_text(encoding="utf-8-sig")
    run = iss[iss.index("function RunSetup"):iss.index("procedure CurStepChanged")]
    assert "-NonInteractive" in run and "-Pause" not in run, "the hidden setup could wait for input no one sees"
    e2e = (ROOT / "tests" / "installer_e2e.ps1").read_text(encoding="utf-8")
    # rc-fix6 (the user's install: the wizard refused every click; a check wizard on the user's desktop confused them):
    # the real installer, interactive, clicked through by a driver; on the download page every second the wizard and
    # Stop enabled, answers timed, moved and minimised; the test variant is out of the taskbar and Alt+Tab and shows no
    # box; -CancelCheck clicks Stop in a silent run and the next run carries on
    assert "$run = RunInstaller 'full' @(" in e2e and "$a = RunInstaller 'abort' @('/SILENT', '/SUPPRESSMSGBOXES')" in e2e
    assert "if (-not [W]::IsWindowEnabled($h)) { $wizOff++ }" in e2e and "[W]::IsWindowEnabled($abort) -and [W]::IsWindowVisible($abort)" in e2e
    assert "IsHungAppWindow" in e2e and "SetWindowPos($h, [IntPtr]::Zero, $a.L + 40, $a.T + 30" in e2e and "PostMessage($h, 0x112, [IntPtr]0xF020" in e2e
    assert "if ($resp['toolwindow'] -ne 'True')" in e2e and "if ($resp['boxes'] -or $resp['ourboxes'])" in e2e
    assert "it didn't carry on" in e2e and "if (Test-Path $cacheDir)" in e2e and "the test install made shortcuts" in e2e
    # the first release check hung: AMD's server closed a 645 MB download at 6 MB, the hash failed, and the error box
    # (written down by the test variant) sent the wizard back to its Ready page while the driver waited. Now the
    # installer tries a broken-off download again by itself, the check proves it with a server that stops early, and
    # the driver ends the check on any box or a fall back to the Ready page
    assert "trying again by itself (%d of 3)" in iss and "if Tries < 3 then begin" in iss and "Tries := 0   // the user's Try again" in iss
    assert "$run = RunInstaller 'full' @(\"/SHORTURL=$shortUrl\", \"/BADURL=$badUrl\", '/SETUPHOLD=40')" in e2e and "$size = 104857600; $send = 1048576" in e2e
    # rc-fix8: the release check fails on any disabled second while setup runs, and moves, minimises and opens Show details then
    for k in ("if ([int]$resp['setupwizoff'] -gt 0)", "if ([int]$resp['setupsecs'] -lt 60)", "if ($resp['setupmoved'] -ne 'True')",
              "if ($resp['setupminimise'] -ne 'True/True')", "if ($resp['setupdetails'] -ne 'True/True')"):
        assert k in e2e, k
    assert "a download that came back short wasn't tried again by itself" in e2e and "the run after Stop downloaded $n again" in e2e
    assert 'if ($logged) { Quit "a box came up:' in e2e and "the wizard went back to its Ready page during the downloads" in e2e
    assert "isn't the listed file (size or SHA-256)" in e2e
    # the second release check: Inno hashing a gigabyte on its own thread kept the window from answering for seconds.
    # A big file's SHA-256 is now checked by a hidden PowerShell before it is moved into place (a failure deletes it and
    # the retry fetches it again), and the check proves it with a big file of the right size and the wrong bytes
    assert "if Big then InnoSha := '' else InnoSha := TabField(Line, 3);" in iss and "Big := Bytes >= 67108864;" in iss
    place = iss[iss.index("function PlaceFile("):iss.index("function FetchFile(")]
    assert "Shell := CreateOleObject('WScript.Shell');" in place and "Exec(" not in place, "Inno's Exec disables the wizard while it runs"
    # started from PowerShell 7, Windows PowerShell inherits its module path and can't load Get-FileHash / Expand-Archive
    assert "GetEnvironmentVariable(' +" in place and "''PSModulePath'', ''Machine'')" in place
    for f in ("setup.ps1", "space.ps1", "detect_gpu.ps1"):
        assert "if ($PSVersionTable.PSEdition -ne 'Core') { $env:PSModulePath = [Environment]::GetEnvironmentVariable('PSModulePath', 'Machine') }" in (inst / f).read_text(encoding="utf-8-sig"), f
    assert "throw ''its SHA-256 is wrong''" in iss and "Remove-Item -LiteralPath $f -Force" in iss and "Get-FileHash -Algorithm SHA256 -LiteralPath $f" in iss
    assert "Why := PlaceFile(ExpandConstant('{tmp}\\') + Name, Dest, Bytes, CheckSha);" in iss
    # the user's box says it in plain words; the log keeps the reason (rc-polish1: "its SHA-256 is wrong" was shown)
    ask = iss[iss.index("Log(Format('%s: %s; asking the user'"):iss.index("until (Why = '') or Stopped;")]
    assert "': ' + PlainWhy(Why) + '.'" in ask and "+ Why +" not in ask and "+ Why)" not in ask, "the download box shows the technical reason"
    pw = iss[iss.index("function PlainWhy"):iss.index("function FetchFile")]
    assert "Result := 'it arrived damaged'" in pw and "(Pos('sha-256', L) > 0)" in pw and "(Pos('it came to', L) = 1)" in pw
    assert "(a few minutes)" not in iss and "(a minute or two)" not in (inst / "setup.ps1").read_text(encoding="utf-8-sig"), "a fixed time in setup's lines"
    assert '"/BADURL=$badUrl"' in e2e and "a big file with the wrong bytes wasn't caught by its SHA-256 and fetched again" in e2e
    assert "if ($line -match '/bad\\?bytes=(\\d+)')" in e2e and "$res['hangs']" in e2e
    # the real installer passes the three folders even when they are its own: the step counts only when it runs
    setup = (inst / "setup.ps1").read_text(encoding="utf-8-sig")
    assert "if (-not ((SameDir $LibraryDir $InstallDir) -and (SameDir $WorkDir $InstallDir) -and (SameDir $ModelsDir $InstallDir))) { $script:StepsTotal += 1 }" in setup
    iss = (inst / "UnderCover.iss").read_text(encoding="utf-8-sig")
    assert "#ifdef ReleaseCheck" in iss and "AppName=UnderCover release check (test)" in iss and "Uninstallable=no" in iss
    assert "SaveStringToFile(ExpandConstant('{param:hooks|}\\boxes.txt'), Text + #13#10, True);" in iss
    assert "MsgBox(" not in iss.replace("SuppressibleMsgBox(", "").replace("MsgBox('UnderCover is removed.'", ""), "a box the test variant would show"
    assert "#ifndef ReleaseCheck\n[Icons]" in iss and "#ifndef ReleaseCheck\n  MakeLink(" in iss, "the test variant makes shortcuts"
    # rc-fix4b (the machine came back from a reboot with LiveKernelEvent 141 reports): the release check fails on a driver
    # reset or a lost device during the run, and the selftest's GPU work is gentle (small steps, each waited for)
    assert "function GpuTrouble" in e2e and "P1: (141|117)" in e2e and "Id = 4101" in e2e and "$gpu = @(GpuTrouble)" in e2e
    # (MasterPlan, run 4: Windows resent 32 old reports during an update scan; a report fails the run only when its dump
    # was made during the run, or it has no dump and real parameters; older ones are a warning)
    assert "if (-not $made -or $made -ge $runStart.AddMinutes(-1)) { $new +=" in e2e and "Windows resent" in e2e
    st = (ROOT / "installer" / "selftest.py").read_text(encoding="utf-8")
    assert "n, dt = 1024," in st and "if steps[-1] > 0.2:" in st and "stopped answering during the self-test" in st
    assert "if ([double]$resp['maxms'] -gt 1000)" in e2e
    # shortcuts: only ever this install's
    undelete = iss[iss.index("[UninstallDelete]"):iss.index("[Code]")]
    assert ".lnk" not in undelete, "the uninstaller deletes a shortcut by name, whoever it belongs to"
    assert iss.count("CreateShellLink(") == 1 and "CreateShellLink(P," in iss, "a shortcut made without the ownership check"
    assert "if OurLink(LinkPath(Folder, I)) then DeleteFile(LinkPath(Folder, I));" in iss
    assert "Pos(Lowercase(AddBackslash(ExpandConstant('{app}'))), Lowercase(LinkTarget(Path))) = 1" in iss
    assert "(not FileExists(LinkPath(Folder, I))) or OurLink(LinkPath(Folder, I))" in iss
    assert "CurUninstallStep = usUninstall" in iss and "RemoveOurLinks(ExpandConstant('{userdesktop}'))" in iss
    assert "SetupMessage(msgFinishedLabel) + LinkNote" in iss
    return "add-on: 3 plain lines, %d status lines for 108 MB; a taken port named (this copy / another / %s); shortcuts checked by target" % (len(pm), foreign["owner"])


@check("the installer downloads everything first, on Inno's own page, then sets up offline (rc-fix6, the user's install: the wizard refused every click for all of setup, Inno disables it while a program it started runs; the 200 ms re-enable was rejected, 'this absolutely cannot be how it is done by the pros'): installer/downloads.tsv lists every file (each pin's exact file by its SHA-256, the models, the add-ons, the tools), the wizard's download page fetches this PC's groups before anything is installed and puts each file in place (a file there at its size is skipped), setup.ps1 -Offline installs from them with uv --offline and never goes online; Cancel is off for its short steps")
def u_installer_downloads():
    import re
    import subprocess
    import tempfile
    inst = ROOT / "installer"
    sys.path.insert(0, str(inst))
    try:
        import downloads as D
    finally:
        sys.path.remove(str(inst))
    rows = D.read()
    assert rows and list(rows[0]) == list(D.COLUMNS), rows[:1]
    for r in rows:
        assert re.fullmatch(r"[0-9a-f]{64}", r["sha256"]) and int(r["bytes"]) > 0, r
        assert r["dest"].startswith(("cache/", "models/")) and ".." not in r["dest"] and "\\" not in r["dest"], r
        assert all(u.startswith("https://") for u in r["urls"].split(" ")), r
        assert (r["pin"] != "") == (r["dest"].startswith("cache/") and r["group"] != "base"), r
    groups = {r["group"] for r in rows}
    assert {"base", "packages", "models", "torch-rocm", "torch-cuda128", "torch-cuda126", "torch-cpu"} <= groups, groups
    # every pin has its file, the one its hash names, in its group (a stale list after pin_hashes.py fails here)
    for g, req in [("packages", "common.txt"), ("packages", "cadence.txt")] + [(f"torch-{b}", f"torch-{b}.txt") for b in D.BACKENDS] + \
            [(p.stem, p.name) for p in (inst / "requirements").glob("addon-*.txt")]:
        mine = [r for r in rows if r["group"] == g]
        for pin in D.read_pins(inst / "requirements" / req):
            hit = [r for r in mine if r["sha256"] in pin["hashes"]]
            assert len(hit) == 1, (req, pin["name"], "downloads.tsv is stale: run installer\\build.ps1")
            assert re.sub(r"[-_.]+", "-", hit[0]["pin"].split("==")[0]).lower() == re.sub(r"[-_.]+", "-", pin["name"]).lower(), (pin, hit)
    # every model a feature uses, and every add-on's files, with the registry's SHA-256 and size
    from undercover import models
    from undercover.core import addons as A, setup_models
    for local in models.wanted():
        hit = [r for r in rows if r["group"] == "models" and r["dest"] == f"models/{local}"]
        assert len(hit) == 1 and hit[0]["sha256"] == setup_models.PINNED[local]["sha256"], local
        assert int(hit[0]["bytes"]) == setup_models.PINNED[local]["size"], local
        assert hit[0]["urls"].split(" ") == setup_models.PINNED[local]["urls"], local
    for a in A.ADDONS.values():
        if a.included or not a.wired:
            continue
        for local in a.files:
            assert [r for r in rows if r["group"] == f"addon-{a.id}" and r["dest"] == f"models/{local}"], (a.id, local)
        assert not a.packages or (inst / "requirements" / f"addon-{a.id}.txt").exists(), a.id
    # the tools: the versions setup.ps1 names
    setup = (inst / "setup.ps1").read_text(encoding="utf-8-sig")
    base = {r["dest"][len("cache/"):] for r in rows if r["group"] == "base"}
    ver = {k: re.search(rf"\${k}\s*=\s*'([^']+)'", setup).group(1) for k in ("UvVersion", "DenoVersion", "PythonVer", "FfmpegVer")}
    assert any(f"/{ver['UvVersion']}/" in r["urls"] for r in rows if r["dest"] == "cache/uv-x86_64-pc-windows-msvc.zip"), ver
    assert any(f"/v{ver['DenoVersion']}/" in r["urls"] for r in rows if r["dest"] == "cache/deno-x86_64-pc-windows-msvc.zip"), ver
    assert f"ffmpeg-{ver['FfmpegVer']}-essentials_build.zip" in base and D.PY_FILE in base and D.PY_FILE.startswith(f"cpython-{ver['PythonVer']}+"), base
    assert f"CacheFile '{D.PY_FILE}'" in setup and "CacheFile \"ffmpeg-$FfmpegVer-essentials_build.zip\"" in setup
    # setup -Offline: one uv run from the cache, hashes required, nothing online; Python unpacked by setup, not uv
    assert "'--offline', '--no-index', '--find-links', $Cache, '--no-deps', '--require-hashes'" in setup and "$env:UV_OFFLINE            = '1'" in setup
    assert setup.count("RunWatched $Uv") == 1 and "--index-url" not in setup and "'python', 'install'" not in setup
    assert "Run (Join-Path $env:SystemRoot 'System32\\tar.exe') @('-xzf', $archive, '-C', $unpack)" in setup
    assert "if ($Offline) { Info \"YouTube downloader: the pinned one" in setup, "the installer's setup goes online for yt-dlp"
    assert "if ($Offline) { $code = RunCode $Py @('-m', 'undercover.models', 'install', '--offline') }" in setup
    # the fourth release check: audio-separator's model list appeared at first use (the app copies it in from its
    # assets before a separation; setup's model step always had): install --offline puts it there, fetches nothing
    msrc = (ROOT / "undercover" / "models.py").read_text(encoding="utf-8")
    assert "if a.offline and todo:" in msrc and msrc.index("if a.offline and todo:") < msrc.index("r = install(progress, cancel)")
    assert "'--from', $Cache" in setup and "Fetch " in setup[setup.index("function Get-Files"):setup.index("# -----", setup.index("function Get-Files"))]
    for line in setup.splitlines():   # nothing else fetches from the network
        assert not re.search(r"\bFetch ['\"]https?:", line), line
    # setup's list functions, run for real on a list of three files: the one at its size is in place, a short one isn't;
    # -Offline names what's missing and stops
    fn = setup[setup.index("$Downloads = @(Import-Csv"):setup.index("# -----", setup.index("$Downloads = @(Import-Csv"))]
    d = Path(tempfile.mkdtemp(prefix="uc-dl-"))
    try:
        (d / "cache" / "downloads").mkdir(parents=True)
        (d / "cache" / "downloads" / "a.whl").write_bytes(b"x" * 10)
        (d / "cache" / "downloads" / "b.whl").write_bytes(b"x" * 3)
        (d / "downloads.tsv").write_text("\t".join(D.COLUMNS) + "\n" + "".join(
            f"packages\tcache/{n}.whl\thttps://example.invalid/{n}\t{'0' * 64}\t10\t{n}==1\n" for n in ("a", "b", "c")) +
            f"models\tmodels/m.bin\thttps://example.invalid/m\t{'0' * 64}\t5\t\n", encoding="ascii")
        script = (f"$ErrorActionPreference = 'Stop'; $Cache = '{d / 'cache' / 'downloads'}'; $Offline = $true\n"
                  "function Fail($m) { Write-Output \"FAIL=$m\"; exit 1 }\nfunction Info($m) { Write-Output \"INFO=$m\" }\n"
                  "function Status($a, $b) { }\n" + fn.replace("$PSScriptRoot", f"'{d}'") +
                  "\nWrite-Output \"inplace=$(@(Rows @('packages') | Where-Object { InPlace $_ } | ForEach-Object { $_.pin }) -join ',')\"\n"
                  "Get-Files @('packages', 'models') 'the packages'\n")
        f = d / "t.ps1"
        f.write_text(script, encoding="utf-8")
        r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(f)], capture_output=True, text=True, timeout=120)
        assert "inplace=a==1" in r.stdout, (r.stdout, r.stderr)
        assert "FAIL=2 of the files setup needs weren't downloaded (b.whl, ...). Run the installer again" in r.stdout, (r.stdout, r.stderr)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    # the wizard: the download page on Install, before anything is installed; setup -Offline with this card's backend;
    # Cancel off for setup's short steps; no timer, no hooks re-enabling the wizard
    iss = (inst / "UnderCover.iss").read_text(encoding="utf-8-sig")
    assert "end else if CurPageID = wpReady then\n    Result := DownloadAll;" in iss.replace("\r\n", "\n")
    assert "DownloadPage := CreateDownloadPage(" in iss and "DownloadPage.Add(Url, Name, Sha);" in iss and "DownloadPage.Download;" in iss
    assert "if DownloadPage.AbortedByUser then" in iss and "MB_RETRYCANCEL" in iss
    pend = iss[iss.index("function Pending("):iss.index("procedure EnsureBackend")]
    assert "Result := not (FileSize64(DownloadDest(TabField(Line, 1)), Have) and (Have = Bytes));" in pend, "a file already downloaded is fetched again"
    assert "if not Pending(Lines[I], Bytes) then Continue;" in iss[iss.index("function DownloadAll"):], "a file already downloaded is fetched again"
    assert "DownloadPage.SetProgress(DownloadIndex, DownloadCount);   // (keeps the window answering)" in iss
    assert "-Backend ' + Backend + ' -Offline';" in iss and "WizardForm.CancelButton.Enabled := False;" in iss
    assert "EnableWindow" not in iss and "AwakeTimer" not in iss and "procedure CancelButtonClick" not in iss and "setup-cancel" not in iss
    assert 'Source: "downloads.tsv"; Flags: dontcopy' in iss and 'Source: "downloads.tsv"; DestDir: "{app}\\installer"' in iss
    assert 'Type: filesandordirs; Name: "{app}\\cache\\downloads"' in iss
    code = iss.replace(chr(13), "")[iss.replace(chr(13), "").index(chr(10) + "[Code]" + chr(10)) + 8:]
    assert not [ln for ln in code.splitlines() if ln.lstrip().startswith("[")], "a [Code] line starting with [ is read as a section"
    assert "StopDownload=Stop downloading? What's downloaded is kept" in iss
    # an add-on from the installer's downloads: packages offline from the folder, its files must be in place already
    src = (ROOT / "undercover" / "core" / "addons.py").read_text(encoding="utf-8")
    assert '_pip("install", "--offline", "--no-index", "--find-links", str(source), "--no-deps", "--require-hashes",' in src
    fake = A.Addon(id="uc-smoke-dl", name="Smoke DL", adds="x", licence="MIT", licence_url="https://example.invalid",
                   licence_summary="x", size_mb=1, files={"uc-smoke-dl/none.bin": "https://example.invalid/none.bin"})
    A.register(fake)
    try:
        with isolated_data():
            try:
                A.install("uc-smoke-dl", True, source=Path(tempfile.gettempdir()))
                raise AssertionError("an add-on with missing files installed from the downloads")
            except A.ModelError as e:
                assert "weren't downloaded" in str(e), e
            A.forget("uc-smoke-dl")
    finally:
        A.ADDONS.pop("uc-smoke-dl", None)
    return f"{len(rows)} files listed; every pin, model and add-on file matched; setup offline; the wizard downloads first"


@check("the voice check always has other voices, its downloads wait in staging, and the training path says one time (rc-fix7, IA's examples: 'Freek-a-Leek as Obama' failed with 'no other voices to test against', left BananaBus223/Obama installed unchecked, and said 'about 1 hour' / 'about 5 min' / 6372 s at epoch 137 of 141 for a 2-hour training): fewer than 4 of the clips' own other speakers -> the Readers' 8 fixed phrases (made once, kept); a candidate waits in voices/.staging, joins the voices only when chosen (never over another), and is discarded only inside the staging folder; the job takes the one-shot's own estimate for its later stages; the trainer's time left runs to the expected end")
def u_rcfix7():
    import inspect
    import numpy as np
    from undercover.core import jobs, readers, voices as V
    from undercover.doubleagent import service, trainer, voicecheck
    from undercover.frontman import api as FA, oneshot
    with isolated_data():
        # 1. the Readers stand in when the clips have too few other speakers; their phrases are made once and kept
        made = []
        real = (readers.ready, readers.synth)
        readers.ready = lambda: True
        readers.synth = lambda text, reader, speed=1.0: (made.append(reader), np.full(48000, 0.1, np.float32))[1]
        try:
            first = voicecheck.reader_phrases()
            again = voicecheck.reader_phrases()
        finally:
            readers.ready, readers.synth = real
        assert len(first) == 8 and len(made) == 8 and len(again) == 8, (len(first), made)   # (the second time: from the file)
        assert sorted(made) == sorted(r for r, _ in voicecheck.READER_LINES) and len({r[:2] for r in made}) == 4, made

        class Ph:
            def __init__(self, i, reason):
                self.id, self.reason, self.dur, self.score, self.source = f"p{i}", reason, 4.0, 0.1, f"s{i % 2}"

        class Proj:
            person = name = "nobody"
            reference = {"cluster": 0, "clip": "x"}
            sources, mode = [], "speaking"   # a person (not a character), their speeches
            phrases = [Ph(i, "target") for i in range(6)] + [Ph(9, "other speaker")]   # one other speaker: too few

            def load_embeddings(self):
                return {"speaker:0": np.ones(4, np.float32)}

            def clip_path(self, pid):
                return Path(__file__)   # (never read: the Readers' phrases are used)
        real_rp = voicecheck.reader_phrases
        voicecheck.reader_phrases = lambda: [np.zeros(32000, np.float32)] * 8
        try:
            ph, ref, kind = voicecheck.test_phrases(Proj())
            assert kind == "readers" and len(ph) == 8 and abs(float(np.linalg.norm(ref)) - 1) < 1e-6, (kind, len(ph))
            assert service.clips_missing(Proj(), None) is None, "the clips' lack of other speakers still stops the check"
            voicecheck.reader_phrases = lambda: []   # no Readers: said as before, never a silent pass
            Proj.phrases = [Ph(i, "target") for i in range(6)]
            assert "no other voices to test against" in service.clips_missing(Proj(), None)
        finally:
            voicecheck.reader_phrases = real_rp
        src = inspect.getsource(service.reference_clips)
        assert 'any(x.reason == "other speaker" for x in pr.phrases) or readers())' in src, "reference_clips still waits for other speakers"
        # 2. a candidate waits in staging: not listed; promoted only when chosen, never over a voice; discarded only there
        st = V.staging_dir() / "obama"
        st.mkdir(parents=True)
        (st / "model.pth").write_bytes(b"m")
        (st / "meta.json").write_text(json.dumps({"name": "obama", "dir": str(st), "pth": str(st / "model.pth"), "version": "v2",
                                                 "sr": 40000, "f0": True, "extra": {}}), encoding="utf-8")
        assert [v.name for v in V.list_voices()] == []   # (waiting: no list sees it)
        _voice_dir("obama", {})   # a voice of that name is there already: the candidate takes the next free name
        got = V.promote("obama")
        assert got.name == "obama-2" and not st.exists() and (V.voices_dir() / "obama-2" / "model.pth").is_file(), got
        assert {v.name for v in V.list_voices()} == {"obama", "obama-2"} and got.pth == V.voices_dir() / "obama-2" / "model.pth"
        for n in ("a", "b"):
            (V.staging_dir() / n).mkdir(parents=True)
            (V.staging_dir() / n / "model.pth").write_bytes(b"m")
        V.discard_staged("a")
        assert not (V.staging_dir() / "a").exists() and (V.staging_dir() / "b").exists()
        V.discard_staged()   # (what a stopped check left)
        assert not any(V.staging_dir().iterdir()) and (V.voices_dir() / "obama").is_dir() and (V.voices_dir() / "obama-2").is_dir()
        # two candidates of one name (BananaBus223/Obama and 0x3e9/obama.zip): each keeps its own folder
        ds = inspect.getsource(V.download_staged)
        assert "staged_name(name)" in ds and "while (staging_dir() / final).exists():" in inspect.getsource(V.staged_name) and "discard_staged" not in ds, "a same-name candidate discards the first"
        rv = inspect.getsource(service.resolve_voice)
        assert "V.download_staged(r," in rv and "download_and_install" not in rv and "remove_voice" not in rv
        assert rv.index("downloaded.append(v.name)") < rv.index("q = test(v, ident, staged=True, owner=owner)"), "a candidate is checked before it's tracked"
        assert "V.discard_staged()   # what a check that stopped" in rv and 'good[0]["name"] = V.promote(good[0]["name"]).name' in rv
        # rc-polish1: what a crash left goes at server start too (Storage counted it), once the port is this server's
        from undercover.app import server as srv_mod
        sm = inspect.getsource(srv_mod.main)
        # (only what's been staged for hours: a server on another port with the same folders may be checking one now)
        (V.staging_dir() / "fresh").mkdir(parents=True)
        (V.staging_dir() / "stale").mkdir(parents=True)
        os.utime(V.staging_dir() / "stale", (time.time() - 7 * 3600, time.time() - 7 * 3600))
        V.discard_staged(older_than=6 * 3600)
        assert (V.staging_dir() / "fresh").is_dir() and not (V.staging_dir() / "stale").exists()
        V.discard_staged()
        assert "voices_mod.discard_staged(older_than=6 * 3600)" in sm and sm.index("SERVER = srv") < sm.index("voices_mod.discard_staged(") < sm.index("gpu_worker"), \
            "the staging leftovers aren't cleared at start, or are cleared before the port is ours"
        from undercover.core import readers as rd
        assert "the first time" not in (rd.kokoro.__doc__ or ""), "readers.kokoro() says it downloads the first time (setup installs it)"
    # 3. one estimate: the page's words; the plan carries the training's; the job uses the one-shot's own stage estimates
    assert [jobs.eta_words(x) for x in (20, 300, 3660, 8180, 10800)] == [
        "under a minute", "about 5 min", "about 61 min", "about 2\u00bd hours", "about 3 hours"]
    assert "jobs.eta_words(tr)" in rv and "(about 1 hour)\"" not in inspect.getsource(service)
    assert 'out["train_s"] = round(service.train_estimate_s(person) + oneshot.cover_eta("", here)["high"])' in inspect.getsource(FA._auto_plan)
    assert "if (p.train_s != null) return etaWords({ low: p.train_s, high: p.train_s });" in (ROOT / "undercover" / "web" / "oneshot.js").read_text(encoding="utf-8")
    run = inspect.getsource(oneshot.run)
    assert 'trains = prefer_train or voice_choice == "train" or not (n or online_for(person, 2))' in run
    assert 'job.after_eta_s = plan.get("cover_s", 60.0) + 60.0' in run
    # the job's countdown during the song step: past runs say a voice step takes 5 min (quick checks); this one-shot's
    # own estimate says 2 hours of training, and that's what the countdown carries
    saved = jobs._ETA_FILE
    jobs._ETA_FILE = {"auto": {"stages": {"song": [70.0] * 3, "voice": [300.0] * 3, "cover": [120.0] * 3}, "total": [490.0] * 3}}
    try:
        def song_left(est):
            j = jobs.Job("auto")
            j.stage_est = est
            j.push(stage="song", frac=0.0, message="x")
            return [e.get("total_left") for e in j.events if e.get("total_left") is not None][-1]
        old, new = song_left({}), song_left({"voice": 7200.0, "cover": 120.0})
    finally:
        jobs._ETA_FILE = saved
    assert old < 600 and new >= 7320, (old, new)   # was "about 5 min"; now the song + the training + the cover
    tsrc = inspect.getsource(trainer.Trainer.run)
    assert "left = end - epoch + 1 - (bi + 1) / len(batches)" in tsrc and "eta=spe * max(0, end - epoch)" in tsrc
    assert "return int(min(max_epochs, max(planned_epochs, best + patience, epoch)))" in tsrc
    return "Readers stand in (8 phrases, made once); staging: promote never over a voice, discard only there; one estimate"


@check("SpeakEasy's Stop ends the GPU work (rc-fix10, the friend's PC: a long paste, Stop did nothing, the GPU at 100 % and 'about 10 min' left): a cancelled wait ends the cadence worker mid-request (a worker that never answers is gone within 2 s, by a cancel that says so or one that raises), the next request starts it again; listen-back, its re-reads and the RVC finish check Stop; a Stop between requests ends the one read ahead; no piece of a run-on paste is longer than 40 words or 280 characters; the page drops its time left at Stop")
def u_rcfix10():
    import inspect
    import subprocess
    import threading
    from undercover.core.cadence import client
    from undercover.core.jobs import Cancelled
    from undercover.speakeasy import listenback, speak, tts
    started = []

    def fake_start(self):   # a worker that reads requests and never answers (an engine stuck on a run-on sentence)
        self.proc = subprocess.Popen([sys.executable, "-c", "import sys, time\nfor line in sys.stdin:\n    time.sleep(3600)"],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        started.append(self.proc)
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()
    w = client.Worker()
    w._start = fake_start.__get__(w)
    saved = client.WORKER
    client.WORKER = w
    try:
        flag = [False]
        fut = w.submit("tts", text="a sentence that never ends")
        threading.Timer(0.3, lambda: flag.__setitem__(0, True)).start()
        t0 = time.time()
        try:
            client.wait(fut, cancel=lambda: flag[0])
            raise AssertionError("the wait didn't stop")
        except Cancelled:
            pass
        first = started[-1]
        for _ in range(40):
            if first.poll() is not None and fut.done():
                break
            time.sleep(0.05)
        took = time.time() - t0
        assert first.poll() is not None and fut.done() and took < 2.0, (first.poll(), fut.done(), took)
        assert w.proc is None or w.proc is not first
        # a job's own check that raises (cadence training's check(), calibration's _stop): the same
        fut2 = w.submit("tts", text="again")   # (a new worker: the next request starts one)
        assert len(started) == 2 and started[1] is not first

        def raising():
            raise Cancelled()
        try:
            client.wait(fut2, cancel=raising)
            raise AssertionError("a raising cancel didn't stop the wait")
        except Cancelled:
            pass
        for _ in range(40):
            if started[1].poll() is not None:
                break
            time.sleep(0.05)
        assert started[1].poll() is not None, "a raising cancel left the worker generating"
        # idle (nothing in it), a Stop leaves the worker alone
        w2 = client.Worker()
        assert w2.abort("x") is False
    finally:
        client.WORKER = saved
        for p in started:
            if p.poll() is None:
                p.kill()
    # every slow step checks Stop
    mc = inspect.getsource(speak._make_cadence)
    assert 'render.client.WORKER.abort("speech stopped")' in mc and mc.index("if job.cancel:\r\n                raise Cancelled()") < mc.index("render.finish(") if "\r\n" in mc else mc.index("if job.cancel:\n                raise Cancelled()") < mc.index("render.finish(")
    assert "cancel=lambda: job.cancel)\n                except Cancelled:\n                    raise" in mc.replace("\r\n", "\n"), "listen-back swallows a Stop"
    rv = inspect.getsource(listenback.review)
    assert "client.wait(fut, cancel=cancel)" in rv and "if stopped():\n            raise Cancelled()" in rv.replace("\r\n", "\n")
    # no piece of a run-on paste is huge: at a comma or dash first, else at a word limit
    runon = " ".join(["and then the thing happened again" for _ in range(30)])   # 180 words, no stop at all
    pieces = [s["text"] for s in tts.sentences(runon)]
    assert len(pieces) >= 5 and all(len(p.split()) <= tts.MAX_WORDS and len(p) <= tts.MAX_CHARS for p in pieces), [len(p.split()) for p in pieces]
    assert " ".join(pieces) == runon, "the split lost words"
    commas = "this goes on, " * 25 + "and ends"
    cp = [s["text"] for s in tts.sentences(commas)]
    assert all(p.endswith(",") for p in cp[:-1]) and all(len(p.split()) <= tts.MAX_WORDS for p in cp), cp
    assert tts.sentences("A short one. Another.")[0]["text"].startswith("A short one."), "normal text changed"
    page = (ROOT / "undercover" / "web" / "speakeasy.js").read_text(encoding="utf-8")
    assert '$("#se-stop").textContent = "Stopping…"; SE.left = null; seTrack();' in page
    assert "SE.left = ev.total_left ?? ev.left;" in page, "the page shows the raw time left, not the job's"
    # the time left while reading: planned seconds a word until measured; the first piece (the engine's start, the
    # calibration) isn't a measurement; measured pieces blend in over five; the start estimate counts the pieces
    clock = speak.PieceClock([30] * 31, 0.2, 0.8)
    first = clock.done(0, 100.0)   # (piece 0 after 90 s of start-up: not counted)
    assert abs(first - (30 * 30 * 0.2 + 30 * 0.8)) < 1e-6, first   # (the old way: 90 s x 30 pieces = 2700 s)
    lefts = [clock.done(i, 100.0 + i * 9.0) for i in range(1, 11)]   # really 9 s a piece: 0.3 s a word, all costs in
    assert lefts[0] > lefts[-1] and abs(lefts[-1] - 20 * 30 * 0.3) < 1e-6, lefts[-1]   # measured, after five
    assert clock.measured() and abs(clock.measured() - 0.3) < 1e-6
    quick = speak.PieceClock([30] * 5, 0.2, 0.8)   # (pieces 1 s apart, the engine reading ahead: a real speed, not 0)
    for i in range(5):
        quick.done(i, float(i))
    assert abs(quick.measured() - 1 / 30) < 1e-6, quick.rates
    ms = inspect.getsource(speak._make_cadence)
    assert "clock = PieceClock(" in ms and "left_est=round(left, 1)" in ms and "el / (i + 1)" not in ms
    assert "honest = clock.n >= 2 and shown is not None and left > max(shown * 1.25, shown + 10)" in ms and "new_work=honest" in ms
    slow = speak.PieceClock([40] * 10, 0.1, 1.0)
    for i, t in enumerate((0, 15, 30, 200, 215)):   # one piece took 170 s: the median keeps the rest's speed
        slow.done(i, float(t))
    assert abs(slow.measured() - (15 / 40)) < 1e-6, slow.rates
    # listen-back hears British and American spellings as one (every "realised" piece was read three times)
    assert [listenback._clean(w) for w in ("realised", "realized", "colour", "four", "your", "hour")] == ["realized", "realized", "color", "four", "your", "hour"]
    assert listenback.problems("I realised it", [["I", 0, .1, .9], ["realized", .1, .5, .9], ["it", .5, .6, .9]]) == []
    es = inspect.getsource(speak.eta_seconds)
    assert "cadence_pieces(tts.sentences(text))" in es and "CALIBRATE_S" in es and "per_word(p)" in es
    return f"a stuck worker gone in {took:.1f} s (and by a raising cancel); Stop checked in listen-back and the finish; run-ons in {len(pieces)} pieces"


@check("a training's time, said once and kept (rc-eta, IA's training proof: 'about 61 min' planned, 2 h 22 min taken; +53 min at the voice step, +29 min at epoch 2; past the plan the bar went 0.927 -> 0.799 and the time left 382 -> 1174 s): one estimator for a new voice from this PC's measured shape (kept minutes against the target, preparation rate, epochs past the plan, step speed), the same before and after the search; the job keeps the plan's number; a first epoch's speed blends in; the bar never goes back and the time left rises only by epochs added, said as such; an install with no name is named from its source")
def u_rceta():
    import inspect
    import tempfile
    import types
    from undercover.core import jobs, voices as V
    from undercover.doubleagent import night, service, trainer
    from undercover.doubleagent.frozen import budget_steps
    from undercover.frontman import api as FA, oneshot
    # the name from the source, never the temp folder ("cache": a model and its index came down to cache_dir/dl_x/)
    hf = "https://huggingface.co/binant/Plankton__SpongeBob___RVC_v2__1K_Epochs_69K_Steps/resolve/main/model.pth"
    assert V.name_from_source(hf, Path("C:/x/cache/dl_ab")) == "plankton-spongebob"
    assert V.name_from_source("https://example.com/files/barack%20obama.zip") == "barack-obama"
    with tempfile.TemporaryDirectory() as td:   # a Drive link's file keeps its own name (its URL ends in "uc")
        f = Path(td) / "Trump_RVC_v2.zip"
        f.write_bytes(b"z")
        assert V.name_from_source("https://drive.google.com/uc?id=1AbC", f) == "trump"
        (Path(td) / "dl").mkdir()
        (Path(td) / "dl" / "Morgan_Freeman_e300.pth").write_bytes(b"p")
        assert V.name_from_source("https://pixeldrain.com/l/abc", Path(td) / "dl").startswith("morgan-freeman")
    ds = inspect.getsource(V.download_staged) + inspect.getsource(V.download_and_install)
    assert ds.count("name = name or name_from_source(src, dl)") == 2 and "dl.parent.name" not in ds
    # one estimator: the same whether the search has run yet (no project) or found 13 sources (nothing kept yet)
    with isolated_data():
        sps = 42 / (budget_steps(42.2)[0] / 142)   # 2d: 142 planned epochs at about 42 s
        real = night._sec_per_step
        night._sec_per_step = lambda: sps
        try:
            bare = night._estimate_new(None, {"kind": "new", "target": "Obama"})
            src = types.SimpleNamespace(extra={}, title="Obama speech", channel="", note="", selected=True, status="found", duration=600.0)
            found = types.SimpleNamespace(kept_seconds=lambda: 0.0, sources=[src] * 13, mode="speaking", person="Obama", name="obama", reference={})
            assert night._estimate_new(found, {"kind": "new", "target": "Obama"}) == bare, "the search's finds change the estimate"
            total = bare["total"] + (80 + 80) / 60   # (+ 2d's song step and its cover)
            # 2d: 142 min (was "about 61"), 2b: 118 min the other way, rc-eta's own proof: 121 min (each within 15 %)
            assert all(abs(total - real) / real < 0.15 for real in (142, 118, 121)), (bare, total)
            enough = types.SimpleNamespace(kept_seconds=lambda: 35 * 60.0, sources=[], mode="speaking", person="Obama", name="obama", reference={})
            assert night._estimate_new(enough, {"kind": "new", "target": "x"})["prepare"] == 0
            assert night.estimate({"kind": "new", "target": "Nobody Yet"})["total"] == night._estimate_new(None, {"kind": "new"})["total"]
            # the shape is measured from every training (medians of the last 12), the defaults until then
            assert night.train_shape() == night.TRAIN_SHAPE
            assert night.train_shape("character")["kept_per_prep_min"] == night.KIND_PREP["character"]   # (scenes yield less)
            night.record_shape("character", kept_per_prep_min=0.5)
            assert night.train_shape("character")["kept_per_prep_min"] == 0.5 and night.train_shape()["kept_per_prep_min"] == 3.2
            for k in (1.2, 1.6, 1.5):
                night.record_shape(kept_over_target=k, past_plan=None)
            assert night.train_shape()["kept_over_target"] == 1.5 and night.train_shape()["past_plan"] == night.TRAIN_SHAPE["past_plan"]
        finally:
            night._sec_per_step = real
    tn = inspect.getsource(service.train_new_voice)
    assert "record_shape(person_kind(proj), kept_over_target=" in tn and "past_plan=(st.get(\"epoch\") or 0) / st[\"planned_epochs\"]" in tn
    # the plan's number is the job's: taken once at the start, the voice step trains by it (no +53 min)
    run = inspect.getsource(oneshot.run)
    assert 'plan["train_est_s"] = train_estimate_s(person)' in run and '"train_est_s"):' in inspect.getsource(oneshot)
    assert 'tr = plan.get("train_est_s") or train_estimate_s(person)' in inspect.getsource(service.resolve_voice)
    assert 'plan["voice_until"] = time.time() + float(kw["eta"]) + AFTER_TRAINING_S' in run, "after the last epoch the old deadline comes back"
    assert 'round(service.train_estimate_s(person) + oneshot.cover_eta("", here)["high"])' in inspect.getsource(FA._auto_plan)
    # a first epoch's speed blends in (no +29 min): the run's first epoch left out once there are more
    hist = [{"epoch": 1, "secs": 120.0}]
    assert trainer.epoch_speed([], 0, 40.0) is None and trainer.epoch_speed(hist, 0, None) == 120.0
    assert trainer.epoch_speed(hist, 0, 40.0) == 0.2 * 120 + 0.8 * 40   # one epoch: a fifth of the way
    hist += [{"epoch": e, "secs": 42.0} for e in range(2, 8)]
    assert trainer.epoch_speed(hist, 0, 40.0) == 42.0   # the warm-up epoch gone, five measured: the measured speed
    assert trainer.epoch_speed([{"epoch": 3, "secs": 99.0}], 3, 40.0) is None   # (a resumed run's earlier epochs aren't its own)
    # the job holds every time left to what it said last, whoever said it: the first eta can't jump
    j = jobs.Job("auto")
    j.push(stage="voice", frac=0.1, message="x", total_left=6000.0)
    j.push(stage="voice", frac=0.2, message="x", eta=7700.0)
    assert j.events[-1]["total_left"] <= 6000 + 10, j.events[-1]["total_left"]
    # ... but a training extended past its plan says its added epochs at once (and only those)
    j.push(stage="voice", frac=0.3, message="x", eta=7700.0, new_work=True)
    assert j.events[-1]["total_left"] == 7700.0 and "new_work" not in j.events[-1]
    # past the plan: the bar holds, the extension is said
    tsrc = inspect.getsource(trainer.Trainer.run)
    assert tsrc.count('shown["frac"] = ') == 2 and "max(shown[\"frac\"]" in tsrc and tsrc.count("end >= shown[\"end\"] + EXTEND_SAID") == 2
    assert tsrc.count("extended=extended") == 2 and tsrc.count("fraction=shown[\"frac\"]") == 2
    assert trainer.epoch_line({"epoch": 136, "epochs": 283, "planned": 142, "end": 171}) == \
        "Training: epoch 136 of about 171: a little longer, it's still improving (planned 142)"
    assert inspect.getsource(service).count('new_work=bool(ev.get("extended"))') == 2 and 'new_work=bool(ev.get("extended"))' in inspect.getsource(night)
    return f"2d said {total:.0f} min (took 142, was 61); one number from plan to job; first epoch blended; the bar holds past the plan"


@check("every voice's preview line (rc-preview, the user's ask for 1.0): 'Go undercover as <its name>.' kept in the voice's folder, fresh by what it was made from; made as the last step of each path that adds or changes a voice (a training's export, an import in staging before it's listed, the one-shot's chosen candidate, Improve's Keep, a pack kept, a rename), never off the GPU thread; an idle backfill for voices from before; not part of the model's size; a ▶ in the five voice lists only when the clip is there; the check's estimate and countdown from its real work (IA pass 13)")
def u_rcpreview():
    import inspect
    import threading
    import numpy as np
    from undercover.core import gamepause, jobs, voicepreview as VP, voices as V
    from undercover.app import server as srv
    from undercover.doubleagent import cadence, improved, service, trainer
    from undercover.safehouse import api as sh_api, library
    from undercover.speakeasy import speak
    from undercover.frontman import api as FA
    web = ROOT / "undercover" / "web"
    assert VP.LINE == "Go undercover as {name}." and VP.FILES == ("preview.mp3", "preview.json")
    saved_render = VP.RENDER[0]
    try:
        with isolated_data():
            _voice_dir("plankton-spongebob", {"person": "Plankton"})
            v = V.get_voice("plankton-spongebob")
            assert VP.line_for(v) == "Go undercover as Plankton." and not VP.fresh(v)
            # no renderer (a tool, a unit test): nothing is queued, the backfill makes it later
            VP.RENDER[0] = None
            n0 = jobs.GPU_QUEUE.qsize()
            assert VP.after_add(v) is False and jobs.GPU_QUEUE.qsize() == n0
            # a renderer: made in the voice's folder, fresh; the voice's API says so
            said = []
            VP.RENDER[0] = lambda job, vv: (said.append((vv.name, job.cancel)), (np.zeros(16000, np.float32) + 0.01, 16000))[1]
            if shutil.which("ffmpeg"):
                assert VP.make(v) and VP.fresh(v) and VP.clip(v).is_file() and said == [("plankton-spongebob", False)]
                rec = json.loads((v.dir / "preview.json").read_text(encoding="utf-8"))
                assert rec["line"] == "Go undercover as Plankton." and rec["seconds"] == 1.0
                assert V.voice_info(V.get_voice("plankton-spongebob"))["preview"] is True
                # stale when what it says or is made from changes: a rename, another model
                V.set_display("plankton-spongebob", "Sheldon Plankton")
                assert not VP.fresh(V.get_voice("plankton-spongebob"))
                V.set_display("plankton-spongebob", "")
                assert VP.fresh(V.get_voice("plankton-spongebob"))
                (v.dir / "model.pth").write_bytes(b"another model, another size")
                assert not VP.fresh(V.get_voice("plankton-spongebob"))
                # not part of the model: the size shown leaves the clip out
                vd = V.get_voice("plankton-spongebob").dir
                assert library._voice_size(vd) == library._size(vd) - sum(library._size(vd / f) for f in VP.FILES)
                assert library._voice_size(vd) < library._size(vd)
            # off the GPU thread it never renders: it queues a GPU job (a page's Keep, a rename) ...
            said.clear()
            q = VP.after_add(V.get_voice("plankton-spongebob"))
            assert q is None and not said
            job, fn = jobs.GPU_QUEUE.get_nowait()
            assert job.kind == "voicepreview" and job.title == "Preview line for Plankton" and job.voices == {"plankton-spongebob"}
            jobs.JOBS.pop(job.id, None)
            # ... and on the GPU worker's thread it renders there and then, in the job that added the voice
            out = {}
            t = threading.Thread(target=lambda: out.update(r=VP.after_add(V.get_voice("plankton-spongebob"), force=True)), name="gpu")
            t.start(); t.join(30)
            assert said and (out.get("r") is True or not shutil.which("ffmpeg"))
            # an import: staged, its clip made there (on the GPU thread here), then listed with it
            st = V.staging_dir()
            def stage():
                d = st / V.staged_name("obama")
                d.mkdir(parents=True)
                (d / "model.pth").write_bytes(b"m-obama")
                (d / "meta.json").write_text(json.dumps({"name": "obama", "dir": str(d), "pth": str(d / "model.pth"), "version": "v2",
                                                          "sr": 40000, "f0": True, "extra": {}}), encoding="utf-8")
                return V.Voice(**{**json.loads((d / "meta.json").read_text(encoding="utf-8")), "dir": d, "pth": d / "model.pth", "index": None})
            out.clear()
            t = threading.Thread(target=lambda: out.update(v=VP.import_voice(stage)), name="gpu")
            t.start(); t.join(30)
            got = out["v"]
            assert got.name == "obama" and (got.dir / "model.pth").is_file() and not (st / "obama").exists()
            if shutil.which("ffmpeg"):
                assert VP.fresh(got) and (got.dir / "preview.mp3").is_file(), "the import is listed before its clip"
            # a taken name is refused as a direct install would be, and the staged copy is discarded
            try:
                VP.import_voice(stage)
                raise AssertionError("an import over a voice")
            except V.ModelError as e:
                assert "already installed" in str(e)
            assert not any(p.is_dir() for p in st.iterdir())
            # off the GPU thread, an import waits a while for its job; one that hasn't started is called off untouched
            def stage2():
                d = st / V.staged_name("trump")
                d.mkdir(parents=True)
                (d / "model.pth").write_bytes(b"m-trump")
                (d / "meta.json").write_text(json.dumps({"name": "trump", "dir": str(d), "pth": str(d / "model.pth"), "version": "v2",
                                                          "sr": 40000, "f0": True, "extra": {}}), encoding="utf-8")
                return V.Voice(**{**json.loads((d / "meta.json").read_text(encoding="utf-8")), "dir": d, "pth": d / "model.pth", "index": None})
            v2 = VP.import_voice(stage2, wait=0.5)
            job, fn = jobs.GPU_QUEUE.get_nowait()
            assert v2.name == "trump" and job.cancel and not VP.fresh(v2), "an import's line wasn't called off"
            jobs.JOBS.pop(job.id, None)
            # the backfill: what's missing, the liked first; a voice that failed on these files is skipped
            names = [x.name for x in VP.todo()]
            assert "trump" in names and "obama" not in names, names
            VP._broken("trump", VP.key(V.get_voice("trump")))
            assert "trump" not in [x.name for x in VP.todo()]
            VP._broken("trump", None)
            assert "trump" in [x.name for x in VP.todo()]
            # (skipped for a day, not for good: a GPU out of memory at an export passes)
            VP._broken("trump", VP.key(V.get_voice("trump")))
            bf = json.loads(VP._broken_file().read_text(encoding="utf-8"))
            bf["trump"]["at"] -= VP.RETRY_S + 1
            VP._broken_file().write_text(json.dumps(bf), encoding="utf-8")
            assert "trump" in [x.name for x in VP.todo()], "a voice whose line failed once is never tried again"
            # idle only: nothing queued or running, no hold, no Storage move, no game on the screen
            real = gamepause._look
            gamepause._look = lambda: None
            try:
                assert VP.idle() == (jobs.GPU_QUEUE.empty() and not any(not j.finished for j in jobs.JOBS.values()))
                gamepause._look = lambda: {"exe": "game.exe", "title": "A game"}
                assert gamepause.game_up() and not VP.idle()
                gamepause._look = lambda: {"exe": "chrome.exe", "title": "UnderCover"}
                assert not gamepause.game_up()
            finally:
                gamepause._look = real
            # the check's estimate from its work: nothing yet (a search, two sources), a reference in two (none)
            assert service.clip_work(None) == (1, 2)

            class P1:
                sources, reference = [], {}
            assert service.clip_work(P1()) == (1, 2)

            class S1:
                selected, status = True, "found"

            class P2:
                sources, reference = [S1(), S1(), S1()], {"clip": "x", "sources": 1}

            class P3:
                sources, reference = [S1()], {"clip": "x", "sources": 2}
            assert service.clip_work(P2()) == (0, 1) and service.clip_work(P3()) == (0, 0)
    finally:
        VP.RENDER[0] = saved_render
    # wiring: SpeakEasy renders (the app registers it), the paths that add or change a voice end with it
    m = inspect.getsource(srv.main)
    assert "voicepreview.RENDER[0] = speak_mod.render_line" in m and "voicepreview.start()" in m
    ms = inspect.getsource(speak.make_speech)
    assert "if not save:" in ms and 'return {"y": np.concatenate(pieces)' in ms and "raise SpeakError(\"The speech model isn't here" in ms
    assert "make_speech(job, voicepreview.line_for(v), v.name, save=False, v=v)" in inspect.getsource(speak.render_line)
    assert "if not save:" in inspect.getsource(speak._make_cadence), "a pack voice's line is saved as a speech"
    ex = inspect.getsource(trainer.export_voice)
    assert ex.rindex("voicepreview.after_add(v)") > ex.rindex("proj.save_train()"), "a training's export doesn't end with the line"
    rv = inspect.getsource(service.resolve_voice)
    assert rv.index('good[0]["name"] = V.promote(good[0]["name"]).name') < rv.index("voicepreview.after_add(get_voice(v.name), job)")
    assert "voicepreview.after_add(get_voice(voice), force=which == \"new\")" in inspect.getsource(improved.keep)
    assert "voicepreview.after_add(get_voice(voice), force=False)" in inspect.getsource(improved.undo)
    assert "voicepreview.after_add(get_voice(v.name))" in inspect.getsource(cadence.verdict)
    assert "voicepreview.after_add(v)   # its line says the new name" in inspect.getsource(sh_api)
    si = inspect.getsource(srv.Handler._start_install)
    assert "voicepreview.import_voice(lambda: download_staged(src, name, progress), job)" in si and "download_and_install(src" not in si
    assert "voicepreview.import_voice(lambda: install(f, staged_name(name)" in inspect.getsource(srv.Handler._voice_upload)
    assert 'm = re.fullmatch(r"/api/voices/([a-z0-9-]+)/preview", p)' in inspect.getsource(srv.Handler)
    # the check's countdown moves with the work: the clip step says what's left, each candidate comes off
    rc = inspect.getsource(service.reference_clips)
    assert "def prog(f, label, n=n, done=done, **kw):" in rc and "say(max(0.0, n - done - f) * per)" in rc and '_record_check("per_source"' in rc
    assert "reference_clips(job, person, control, push, plan.get(\"subject\"), left=until)" in rv and rv.count("one_done()") >= 4
    assert "until(0.0, 0.5 * d / t if t else 0.0)" in rv
    assert "per_voice * max(0.0, voices_left[0] - part)" in rv, "the countdown adds a candidate after the last one"
    assert 'if all(s.status == "ready" for s in P.Project.load(proj.name).sources if s.id in todo):' in rc
    st = inspect.getsource(VP.start)
    assert 'if os.environ.get("UNDERCOVER_NO_BACKFILL"):' in st and "jobs.JOBS.pop(j.id, None)" in st
    assert '"UNDERCOVER_NO_BACKFILL": "1"' in inspect.getsource(sys.modules[__name__])
    assert 'pack["ready"] = bool(pack_ready(' in inspect.getsource(VP.key) and "wait=20.0)" in inspect.getsource(srv.Handler._voice_upload)
    assert 'out["check_s"] = round(service.check_estimate_s(person, len(cands))' in inspect.getsource(FA._auto_plan)
    page = {f: (web / f).read_text(encoding="utf-8") for f in ("app.js", "oneshot.js", "speakeasy.js", "doubleagent.js")}
    assert "const chk = p.check_s != null ? etaWords" in page["oneshot.js"]
    # the ▶: one player, in the five lists, never picking the card it sits on
    app = page["app.js"]
    assert "if (!v?.preview) return \"\";" in app and "e.stopPropagation(); e.preventDefault(); playVoiceLine(b);" in app
    assert '$$("audio").forEach(x => { if (!x.paused) x.pause(); });' in app and 'document.addEventListener("play", e =>' in app
    assert '<span class="vside">${voiceLineButton(v.name)}' in app and 'e.target.closest(".remove, .vline")' in app
    assert 'hear.innerHTML = on ? voiceLineButton(S.opts.backing_voice) : "";' in app
    assert '${c.voice ? voiceLineButton(c.voice) : ""}</span>' in page["oneshot.js"] and 'bindVoiceLines($("#os-plan"))' in page["oneshot.js"]
    assert "${voiceLineButton(v.name)}${act}" in page["speakeasy.js"] and 'bindVoiceLines($("#se-voices"))' in page["speakeasy.js"]
    assert "${voiceLineButton(c.voice)}</div>" in page["doubleagent.js"] and "bindVoiceLines(box);" in page["doubleagent.js"]
    # Agent Smoke's clip ships made: fresh for the copy a first run seeds (the same model, index, pack and name)
    from undercover.app import firstrun
    d = firstrun.DEMO
    assert json.loads((d / "preview.json").read_text(encoding="utf-8"))["line"] == "Go undercover as Agent Smoke." and (d / "preview.mp3").is_file()
    if (d / "model.pth").is_file():   # (not in git: a fresh checkout makes it with scripts/make_demo_voice.py)
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        demo = V.Voice(**{**meta, "name": d.name, "dir": d, "pth": d / "model.pth", "index": d / "model.index"})
        assert VP.key(demo) == json.loads((d / "preview.json").read_text(encoding="utf-8"))["key"], "Agent Smoke's shipped line is stale"
    return "made with the voice, in its folder; staged imports listed with it; the backfill when idle; five lists; the check's real work"


@check("the one-shot's polish for 1.0 (rc-polish1, IA's examples): the song step has a time left on a fresh install; a stopped one-shot's alert goes when the next starts; a check that couldn't be done isn't offered again on the same clips (training or adding clips instead); a source the engine left out for a reason is never ticked back in to top up a training; a candidate is named by its display name throughout the check")
def u_rcpolish1():
    import inspect
    import types
    from undercover.core import jobs, voices as V
    from undercover.doubleagent import service, sources as srcmod
    from undercover.frontman import api as FA, oneshot
    page = (ROOT / "undercover" / "web" / "oneshot.js").read_text(encoding="utf-8")
    run = inspect.getsource(oneshot.run)
    rv = inspect.getsource(service.resolve_voice)
    # 1. a fresh install (no past runs): the song step's time left from the one-shot's own estimates of each stage
    saved = jobs._ETA_FILE
    jobs._ETA_FILE = {}
    try:
        def first_left(est):
            j = jobs.Job("auto")
            j.stage_est = est
            j.push(stage="song", frac=0.02, message="x")
            return j.events[-1].get("total_left")
        bare, own = first_left({}), first_left({"song": 80.0, "voice": 10.0, "cover": 60.0})
    finally:
        jobs._ETA_FILE = saved
    assert bare is None and own is not None and 145 <= own <= 150, (bare, own)   # (was: no time left at all)
    # the quick download (0.02 -> 0.38 in 10 s) doesn't make a 70 s separation "almost done": the estimate leads
    clock = [1000.0]
    real_time = jobs.time
    jobs.time = types.SimpleNamespace(time=lambda: clock[0])
    try:
        j = jobs.Job("auto")
        j.stage_est = {"song": 80.0, "voice": 10.0, "cover": 60.0}
        for t, p in ((0, 0.02), (5, 0.2), (10, 0.38), (40, 0.65)):
            clock[0] = 1000.0 + t
            j.push(stage="song", frac=p, message="x")
        said = [e["total_left"] for e in j.events]
    finally:
        jobs.time = real_time
    assert said[2] - 70 >= 60 and said[3] - 70 >= 25, said   # (the song's own part: about 70 s, then about 40 s; extrapolated, 17 s)
    assert 'job.stage_est["song"] = e["separating"] + (5.0 if here else FETCH_S)' in run and 'job.stage_est["cover"] = e["converting"] + e["mixing"]' in run
    assert run.index('job.stage_est["song"]') < run.index('push("song", 0.02 + 0.9 * n / per'), "the song's estimate comes after the song step starts"
    assert 'job.stage_est["voice"] = 10.0' in run
    # 2. a stopped one-shot's alert belongs to that run
    fol = page[page.index("function osFollow("):]
    assert fol.index("osClearStopped();") < fol.index("OS.job = id;") and "showError(ev.message, OS_STOPPED);" in page
    assert '$("#err-title").textContent === OS_STOPPED' in page
    # 3. a check that couldn't be done: remembered on its clips, not offered again until clips change
    with isolated_data():
        real = service._clips_of
        service._clips_of = lambda person: "clips-1"
        try:
            assert service.check_failed("Obama") is None
            service._remember_failed("Obama", "no clean clips of Obama came through", None)
            assert service.check_failed("Obama") is None, "a failure without its clips held on other clips"
            service._remember_failed("Obama", "the clips had no other voices", types.SimpleNamespace(
                name="obama", reference={"clip": "r"}, training_phrases=lambda: []))
            service._clips_of = lambda person: service.clip_set(types.SimpleNamespace(name="obama", reference={"clip": "r"}, training_phrases=lambda: []))
            assert service.check_failed("obama") == "the clips had no other voices"
            service._clips_of = lambda person: "clips-2"   # clips added since: worth checking again
            assert service.check_failed("Obama") is None
            service._remember_failed("Obama", None)
            assert "obama" not in service._failed_all()
        finally:
            service._clips_of = real
        est = (service.check_estimate_s, service.train_estimate_s)
        service.check_estimate_s, service.train_estimate_s = (lambda person, n: 300.0), (lambda person: 3600.0)
        try:
            vs = [types.SimpleNamespace(name="obama", display="Obama", extra={})]
            plain = [c["choice"] for c in oneshot.voice_choices("Obama", vs)]
            after = oneshot.voice_choices("Obama", vs, failed="the clips had no other voices")
        finally:
            service.check_estimate_s, service.train_estimate_s = est
        assert plain == ["use:obama", "check", "train"] and [c["choice"] for c in after] == ["use:obama", "train"], (plain, after)
        assert "couldn't be done (the clips had no other voices)" in after[-1]["detail"] and "Adding clips" in after[-1]["detail"]
    assert 'failed = None if prefer_train or choice == "check" or community else check_failed(person)' in rv
    # never a training nobody asked for (rc-fix2): without the plan's "train", a held failure stops the job and says so
    fl = rv[rv.index("failed = None if prefer_train"):]
    assert fl.index("if failed:\n        raise ModelError(") < fl.index("online = [] if prefer_train else online_for(person, 2)") and "_remember_failed(person, None)" in rv
    assert rv.index('if choice == "train":\n        prefer_train = True') < rv.index("failed = None if prefer_train")
    assert 'voice_choice: last.plan.voice_ask ? OS.vchoice : last.plan.voice?.after_failed ? "train" : undefined' in page
    assert "_remember_failed(person, why, proj)" in inspect.getsource(service.no_check) and rv.count("community, plan, push, proj)") == 2
    ap = inspect.getsource(FA._auto_plan)
    assert 'online = [] if prefer_train or (failed and not inst) else service.online_for(person, 2)' in ap and '"after_failed": failed' in ap and "failed=failed)" in ap
    assert "if (v.after_failed)" in page
    # 4. left out for a reason: stays out, the reason shown; a training short of voice tops up only from the rest
    S = lambda note: types.SimpleNamespace(note=note)
    flagged = ["from 2004, decades before their newest (2020): a younger voice", srcmod.ABOUT_NOTE, service.SONG_NOTE,
               "left out by DoubleAgent: another voice", "may be in Spanish (a dub or another language)", srcmod.TALK_NOTE, "yours"]
    assert all(srcmod.left_out_for_a_reason(S(n)) for n in flagged), [n for n in flagged if not srcmod.left_out_for_a_reason(S(n))]
    assert not any(srcmod.left_out_for_a_reason(S(n)) for n in ("", "interview", "speech", "scenes", "acapella", "voice lines", "album track"))
    tn = inspect.getsource(service.train_new_voice)
    assert "and not srcmod.left_out_for_a_reason(s)" in tn and "NOT_USABLE)\n" not in tn[tn.index("more = sorted("):tn.index("more = sorted(") + 200]
    old = types.SimpleNamespace(id="a", title="Barack Obama 2004 DNC keynote", extra={}, selected=True, user=False, note="")
    new = types.SimpleNamespace(id="b", title="Obama interview 2020", extra={}, selected=True, user=False, note="")
    srcmod.mark_era([old, new], "Barack Obama")
    assert not old.selected and "a younger voice" in old.note and srcmod.left_out_for_a_reason(old) and new.selected
    # 5. one name for a candidate: the one it's listed under once chosen (the person, _remember_range)
    assert 'v.extra = {**(v.extra or {}), "person": person}' in rv and rv.index('"person": person}') < rv.index("q = test(v, ident, staged=True, owner=owner)")
    assert V.display_name(types.SimpleNamespace(name="plankton-spongebob", display="", extra={"person": "Plankton"})) == "Plankton"
    assert "“{r.name}”" not in rv, "a candidate is named by its search result while it downloads"
    return "song step said from the start; the stopped alert clears; a failed check offers training; left-out sources stay out; one name"


@check("the first start's model download has a moving bar (MasterPlan for the First assignment: its bar only slid): the status carries models_progress (0-1) and models_mb while the separation and conversion models download, from the files and their .part against the pinned sizes; a short file counts as missing")
def u_models_progress():
    from undercover.core import setup_models as S
    from undercover.core.separate import SEP, SEPARATOR_SIZE
    with isolated_data():
        m = S.path("models_dir")
        todo = {"a/full.bin": 100, "a/cut.bin": 200, "a/part.bin": 400, "a/none.bin": 300}
        (m / "a").mkdir(parents=True)
        (m / "a" / "full.bin").write_bytes(bytes(100))
        (m / "a" / "cut.bin").write_bytes(bytes(50))        # audio-separator's way: the final name, cut off
        (m / "a" / "part.bin.part").write_bytes(bytes(100))   # UnderCover's way: .part until it's whole
        left = S.missing(todo)
        assert left == {"a/cut.bin": 200, "a/part.bin": 400, "a/none.bin": 300}, left
        share = S.fetched_share(left)
        assert abs(share - (50 + 100) / 900) < 1e-9, share
        assert S.fetched_share({}) == 0
    # the first start's list: the vocal model and the conversion models, sized as pinned (about 1.3 GB)
    vocal = f"separator/{SEP['vocal_model']}"
    assert vocal in SEPARATOR_SIZE and all(S.PINNED[f]["size"] == n for f, n in S.FILE_SIZES.items())
    assert set(S.FILE_SIZES) == set(S.FILES), "a first-start file without a size: the bar would ignore it"
    mb = round((sum(S.FILE_SIZES.values()) + SEPARATOR_SIZE[vocal]) / 1e6)
    src = (ROOT / "undercover" / "core" / "jobs.py").read_text(encoding="utf-8")
    pre = src[src.index("setup_models.missing("):src.index("MODELS.converter")]
    assert "share = round(min(0.99, setup_models.fetched_share(todo)), 3)" in pre and 'STATUS["models_progress"] = share' in pre
    assert 'STATUS["models_mb"]' in pre and 'STATUS["first_run"] = bool(todo)' in pre
    after = src[src.index("MODELS.converter"):]
    assert after.index("watcher.join(5)") < after.index('STATUS.pop("models_progress", None)'), "the bar outlives the download"
    return f"the first start fetches about {mb} MB; progress from the files and their .part"


@check("the planner sees the desk (MasterPlan, from Q's rc-page6): 'Hold On as Agent Smoke' plans the library's sample song and the installed demo voice with no question, no voice check and no lookup; the sample matches only its own title (and artist); a real 'Smoke' isn't the demo voice")
def u_desk_plan():
    import numpy as np
    import soundfile as sf
    from undercover.app import firstrun
    from undercover.doubleagent import service
    from undercover.frontman import ambiguity, oneshot
    from undercover.frontman import api as fm_api

    class H:
        def _json(self, o, status=200):
            self.out = o

        def _error(self, m, status=400):
            self.out = {"error": m}

    def plan(text):
        h = H()
        fm_api._auto_plan(h, text, False)
        return h.out

    def boom(*a, **k):
        raise AssertionError("the plan looked something up for the desk")
    saved = (firstrun.SAMPLE, firstrun.DEMO, service.online_for, service.subject_of, ambiguity.choices_for)
    with isolated_data() as t:
        try:
            sample = t / "assets" / "sample"
            sample.mkdir(parents=True)
            sf.write(sample / "hold-on.wav", (0.1 * np.sin(np.linspace(0, 880 * np.pi, 44100 * 3))).astype(np.float32), 44100)
            (sample / "sample.json").write_text(json.dumps({"title": "Hold On", "artist": "Ken Mercer", "file": "hold-on.wav",
                                                            "seconds": 3, "licence": "ours", "notice": "n"}), encoding="utf-8")
            _fake_demo_voice(t / "assets" / "demo-voice" / "agent-smoke")
            firstrun.SAMPLE, firstrun.DEMO = sample, t / "assets" / "demo-voice" / "agent-smoke"
            assert firstrun.seed()["voice"] == "agent-smoke"
            sid = firstrun.sample_song_id()
            service.online_for = service.subject_of = ambiguity.choices_for = boom
            o = plan("Hold On as Agent Smoke")
            assert "error" not in o, o
            assert o["ask"] is False and not o.get("voice_ask") and "choices" not in o, o
            assert o["voice"]["how"] == "installed" and o["voice"]["name"] == "agent-smoke" and o["voice"]["demo"], o
            assert o["from_library"]["id"] == sid and "sample song in your library" in o["note"] and "subject" not in o, o
            assert oneshot.sample_song("hold on", "Ken Mercer")["id"] == sid
            for title, artist in (("Hold On", "Wilson Phillips"), ("Hold", None), ("Hold On Tight", None)):
                assert oneshot.sample_song(title, artist) is None, (title, artist)
            # the same song with someone else's voice: still the library's sample, the voice the usual way
            service.online_for = lambda *a, **k: []
            service.subject_of = lambda *a, **k: None
            o = plan("Hold On as Morgan Freeman")
            assert o["from_library"]["id"] == sid and o["ask"] is False and o["voice"]["how"] == "train", o
            # a real "Smoke" never gets the demo voice
            assert service.demo_for("Smoke") is None and service.demo_for("Agent Smoke").name == "agent-smoke"
            src = (ROOT / "undercover" / "frontman" / "oneshot.py").read_text(encoding="utf-8")
            assert "lib = None if (song_url or choice or style) else sample_song(title_q, artist)" in src
            assert "if not song_url and not artist and not lib:" in src and "if (not trained_ok or subject) and not demo:" in src
            assert 'if getattr(pick, "meta", None):   # the library\'s own copy' in src
        finally:
            firstrun.SAMPLE, firstrun.DEMO, service.online_for, service.subject_of, ambiguity.choices_for = saved
    return "sample song + demo voice: no question, no check, no lookup; other titles and artists aren't the sample"


@check("a model fetched mid-step is said, with its progress and its time (rc-fix4, the user's first cover: 913 MB came behind 'Separating vocals', the strip said 22 s left and the stage panel 'almost done'): the first start fetches every model a first cover uses; a download inside a job says 'Downloading the backing-vocal model: x of 913 MB, once' with a measured time left that may rise for it; the strip and the stage panel count down by one rule; audio-separator's own bar is silenced")
def u_model_download_said():
    import time as _t
    from undercover.core import jobs as J, separate as S, setup_models
    # one countdown rule (the snapshot here, countedDown on the page)
    assert J.counted_down(22, 10) == 12 and J.counted_down(22, 30) == 5 and J.counted_down(4, 2) == 2 and J.counted_down(4, 9) == 0
    # a measured new piece of work may raise the time left; anything else rises at most 5 % a minute
    j = J.Job("cover", title="t")
    j.push(stage="separating", frac=0.1, total_left=20.0)
    j._left = (20.0, _t.time())
    j.push(stage="separating", frac=0.1, left_est=120.0)
    assert j.events[-1]["total_left"] <= 25.1, j.events[-1]
    j.push(stage="separating", frac=0.1, left_est=120.0, new_work=True)
    assert j.events[-1]["total_left"] == 120.0 and "new_work" not in j.events[-1], j.events[-1]
    # the strip's snapshot counts down from when it was said, as the page does
    k = J.Job("cover", title="t")
    k.push(stage="separating", frac=0.2, total_left=30.0)
    k.events[-1]["at"] = round(_t.time() - 10, 1)
    assert 19 <= k.snapshot()["total_left"] <= 20.5, k.snapshot()
    k.events[-1]["at"] = round(_t.time() - 60, 1)
    assert k.snapshot()["total_left"] == 5.0, "the strip said almost done before the job did"
    # a download inside a job: said once a second with real numbers, the step's and the job's time left included
    real_current, real_after, real_from = J.CURRENT[0], S.SAY_AFTER_S, S.SPEED_FROM_S
    S.SAY_AFTER_S, S.SPEED_FROM_S = 1.0, 0.0   # (5 s and 2 s in real use: the test needn't wait that long)
    m = J.Job("cover", title="first cover")
    m.push(stage="separating", frac=0.3, total_left=40.0, stage_left=20.0, message="Separating vocals")
    J.CURRENT[0] = m
    try:
        say = S._say_download(S.SEP["karaoke_model"])
        say(1_000_000, 913_000_000)        # too soon: the speed isn't known yet (a connection starts slowly), nothing said
        assert m.events[-1]["message"] == "Separating vocals"
        _t.sleep(1.2)
        say(100_000_000, 913_000_000)
        ev = m.events[-1]
        assert ev["message"] == "Downloading the backing-vocal model: 100 of 913 MB, once", ev
        assert ev["stage"] == "separating" and ev["frac"] == 0.3 and ev["download"] == {"done": 100_000_000, "total": 913_000_000}
        assert ev["total_left"] > 40 and ev["stage_left"] > 20 and abs(ev["total_left"] - ev["stage_left"] - 20.0) <= 0.2, ev
        # a job that hadn't said a time left (the one-shot's song step): the step's own, never a whole-job time that
        # would run down to "almost done" while the cover is still to come
        n = J.Job("auto", title="one-shot")
        n.push(stage="song", frac=0.4, message="Separating the vocals")
        J.CURRENT[0] = n
        say2 = S._say_download(S.SEP["karaoke_model"])
        say2(1_000_000, 913_000_000)
        _t.sleep(1.2)
        say2(300_000_000, 913_000_000)
        ev2 = n.events[-1]
        assert ev2["message"].startswith("Downloading the backing-vocal model") and ev2["stage_left"] > 0, ev2
        assert ev2.get("total_left") is None, ev2
        # Cancel during the download stops it (raised through refetch, which re-raises InterruptedError), never a
        # fallback to audio-separator's own download
        n.cancel = True
        try:
            say2(400_000_000, 913_000_000)
            raise AssertionError("a cancelled job's download went on")
        except InterruptedError as e:
            assert str(e) == "cancel"
        sep_src = (ROOT / "undercover" / "core" / "separate.py").read_text(encoding="utf-8")
        g = sep_src[sep_src.index("    def _get(self, model: str):"):sep_src.index("from audio_separator.separator import Separator")]
        assert g.index("except InterruptedError") < g.index("except Exception") and "raise Cancelled() if" in g
    finally:
        J.CURRENT[0], S.SAY_AFTER_S, S.SPEED_FROM_S = real_current, real_after, real_from
    J.CURRENT[0] = None
    try:
        assert S._say_download(S.SEP["karaoke_model"]) is None   # no job: the first start counts the bytes itself
    finally:
        J.CURRENT[0] = real_current
    # fetching a model: its checkpoint and config from the pinned sources, said as one download; unpinned: left alone
    with isolated_data():
        calls, said = [], []
        real = setup_models.refetch

        def fake(local, progress=None):
            calls.append(local)
            dest = setup_models.path("models_dir") / local
            dest.parent.mkdir(parents=True, exist_ok=True)
            n = S.SEPARATOR_SIZE.get(local, 10)
            if progress:
                progress(local, n // 2, n)
                progress(local, n, n)
            dest.write_bytes(b"x")   # (a stand-in file; its size is the pinned one only in the say)
        setup_models.refetch = fake
        try:
            model = S.SEP["karaoke_model"]
            assert S.fetch_model(model, lambda d, t: said.append((d, t))) is True
            assert calls == [f"separator/{model}", f"separator/{S.MODEL_FILES[model]}"], calls
            assert said and 0 <= said[-1][1] - S.SEPARATOR_SIZE[f"separator/{model}"] < 1000, said   # (+ its small config)
            assert S.fetch_model("not-pinned.ckpt") is False and calls[-1] != "separator/not-pinned.ckpt"
        finally:
            setup_models.refetch = real
    # the first start fetches all three separation models; audio-separator's console bar is silenced
    src = (ROOT / "undercover" / "core" / "jobs.py").read_text(encoding="utf-8")
    assert 'first = [SEP["vocal_model"], SEP["dereverb_model"], SEP["karaoke_model"]]' in src and "fetch_model(m)" in src
    sep = (ROOT / "undercover" / "core" / "separate.py").read_text(encoding="utf-8")
    assert '"audio_separator.separator.separator").tqdm = _QuietBar' in sep and "fetch_model(model, _say_download(model))" in sep
    assert set(S.MODEL_FILES) == {S.SEP["vocal_model"], S.SEP["dereverb_model"], S.SEP["karaoke_model"]}
    assert all(f"separator/{c}" in setup_models.PINNED and f"separator/{y}" in setup_models.PINNED for c, y in S.MODEL_FILES.items())
    page = (ROOT / "undercover" / "web" / "app.js").read_text(encoding="utf-8")
    assert "leftText(countedDown(totalLeft, (performance.now() - leftAt) / 1000))" in page
    assert "countedDown(OS.eta, (performance.now() - OS.etaAt) / 1000)" in (ROOT / "undercover" / "web" / "oneshot.js").read_text(encoding="utf-8")
    return "one countdown rule; a measured download raises the time left; said once a second; fetched from the pins; first start fetches all three"


@check("a voice's meta with more in it never stops the voice list (the demo voice's 'licence' key made Voice() fail, and with it every list of voices on a fresh install): Agent Smoke seeded into a temp voices folder lists; a meta with a key Voice doesn't know keeps it in extra; the shipped demo meta has only Voice's own top-level keys")
def u_meta_keys():
    import dataclasses
    from undercover.app import firstrun
    from undercover.core import voices as V
    own = {f.name for f in dataclasses.fields(V.Voice)}
    shipped = json.loads((firstrun.DEMO / "meta.json").read_text(encoding="utf-8"))
    assert set(shipped) <= own, f"the demo voice's meta has keys Voice doesn't know: {sorted(set(shipped) - own)}"
    with isolated_data():
        seeded = firstrun.seed_voice() if (firstrun.DEMO / "model.pth").is_file() else None   # (not in git: made by a script)
        d = _voice_dir("newer-voice", {"person": "Someone"})
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        meta["licence"] = "a note from a newer version"
        (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        names = [v.name for v in V.list_voices()]
        assert "newer-voice" in names and (seeded is None or seeded in names), names
        v = V.get_voice("newer-voice")
        assert v.extra.get("licence") == "a note from a newer version" and v.extra.get("person") == "Someone"
        assert V.display_of_meta(meta) == "Someone"
    return f"seeded {seeded or '(no demo model here)'} and a meta with a newer key both list; the key kept in extra"


@check("the uninstaller lists the folders a Storage move chose (Internal Affairs pass 12, A1: it named the default folders after a move): Storage writes [paths] as basic TOML strings, as config set does, and the uninstaller's KeptPath reads both basic and 'literal' ones (run for real in a throwaway Inno program, installing nothing)")
def u_uninstall_kept_paths():
    import subprocess
    import tempfile
    import tomllib
    from undercover.core import storage
    from undercover.core.config import ROOT as APP
    v = storage._toml_value("uploads_dir", "uploads", Path("D:/Lib dir/uploads"))
    assert v == '"D:\\\\Lib dir\\\\uploads"' and tomllib.loads(f"a = {v}")["a"] == "D:\\Lib dir\\uploads", v
    assert storage._toml_value("uploads_dir", "uploads", APP / "uploads") == '"uploads"'
    iscc = next((c for c in (Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/Inno Setup 6/ISCC.exe",
                             Path(os.environ.get("ProgramFiles(x86)", "")) / "Inno Setup 6/ISCC.exe") if c.is_file()), None)
    if os.name != "nt" or iscc is None:
        return "values checked; the Inno probe needs Windows and Inno Setup 6 (skipped)"
    iss = (ROOT / "installer" / "UnderCover.iss").read_text(encoding="utf-8-sig")
    fn = iss[iss.index("function KeptPath("):iss.index("// the folder holds at least one file or folder")]
    fn = fn.replace("ExpandConstant('{app}\\config.toml')", "AppDir + '\\config.toml'").replace("ExpandConstant('{app}\\')", "AppDir + '\\'")
    with tempfile.TemporaryDirectory(prefix="uc-keptpath-") as td:
        t = Path(td)
        app = t / "app"
        app.mkdir()
        (app / "config.toml").write_text("[paths]\n"
                                         'uploads_dir = "D:\\\\Lib dir\\\\uploads"\n'      # basic (config set, Storage now)
                                         "output_dir = 'E:\\Lib\\output'\n"              # literal (Storage before rc-fix5)
                                         'voices_dir = "models/voices"\n', encoding="utf-8")   # relative: in the app folder
        out = t / "out.txt"
        probe = ("[Setup]\nAppName=probe\nAppVersion=1\nCreateAppDir=no\nUninstallable=no\nPrivilegesRequired=lowest\n"
                 f"OutputDir={t}\nOutputBaseFilename=probe\n\n[Code]\nvar\n  AppDir: String;\n\n{fn}\n"
                 "function InitializeSetup: Boolean;\nbegin\n"
                 f"  AppDir := '{app}';\n"
                 f"  SaveStringToFile('{out}', KeptPath('uploads_dir', 'U') + '|' + KeptPath('output_dir', 'O') + '|' + "
                 "KeptPath('voices_dir', 'V') + '|' + KeptPath('models_dir', 'M-default') + '|' + KeptPath('cache_dir', 'C'), False);\n"
                 "  Result := False;\nend;\n")
        (t / "probe.iss").write_text(probe, encoding="utf-8")
        r = subprocess.run([str(iscc), "/Q", str(t / "probe.iss")], capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stdout[-500:] + r.stderr[-500:]
        subprocess.run([str(t / "probe.exe"), "/VERYSILENT", "/SUPPRESSMSGBOXES"], timeout=120)
        got = out.read_text(encoding="utf-8").split("|")
    assert got == ["D:\\Lib dir\\uploads", "E:\\Lib\\output", f"{app}\\models\\voices", "M-default", "C"], got
    return "basic, literal, relative and missing [paths] values read as the app reads them, by the uninstaller's own code"


@check("Improve's estimate follows the recipe (found by the real Freeman run: 99 min said, 12 taken): a well-fed voice's Improve is estimated as a refine (1000-4000 steps on the new minutes), a thin one as scratch's budget; a manual training after a refine starts clean (init_from / lr / steps popped)")
def u_improve_estimate():
    import inspect
    from undercover.doubleagent import api as da_api, night
    from undercover.doubleagent.frozen import budget_steps, refine_steps
    with isolated_data():
        proj = _fake_project("freeman", "Morgan Freeman", n_phrases=160, other=6, n_sources=3)   # ~13 min kept
        for src in proj.sources:
            src.status = "ready"
        proj.save()
        kept = proj.kept_seconds() / 60
        assert 12.5 < kept < 14.5, kept
        _voice_dir("freeman-uc", {"project": "freeman", "train_minutes": round(kept, 1), "likeness": 0.795, "f0_median": 104.6})
        _voice_dir("thin-uc", {"project": "freeman", "train_minutes": 3.0, "likeness": 0.7})
        spe = night._sec_per_step()
        well = night.estimate({"kind": "improve", "target": "freeman-uc"})
        thin = night.estimate({"kind": "improve", "target": "thin-uc"})
        new_min = night._improve_extra_minutes(proj) / 4                                        # the estimate's own growth guess
        assert well["train"] == round(refine_steps(new_min) * spe / 60), (well, refine_steps(new_min), spe)
        assert thin["train"] == round(budget_steps(kept + new_min)[0] * spe / 60), (thin, spe)
        assert well["train"] * 4 < thin["train"], (well, thin)
        assert well["prepare"] == thin["prepare"] and well["compare"] == thin["compare"]
    src = inspect.getsource(da_api.start_train)
    pops = src[src.index("for k in (") : src.index("st.pop(k, None)")]
    assert all(k in pops for k in ("init_from", "lr", "steps", "planned_epochs")), "a manual training keeps an Improve's refine plan"
    return f"well-fed: refine {well['train']} min; thin: scratch {thin['train']} min (at {spe:.2f} s/step); the training form pops the refine plan"


class _FakeEngine:
    """The frozen test's engine on fixed numbers: a 'voice' is a dict {gain, buzz}; convert() scales and adds a hum, so
    likeness / harmonicity / contrast differ between voices deterministically; embed() is a fixed projection."""

    def __init__(self):
        import numpy as np
        self.rng = np.random.default_rng(7)
        self.P = self.rng.normal(0, 1, (64, 256)).astype("float32")

    def pitch(self, x16):
        import numpy as np
        n = len(x16) // 160
        f = np.full(n, 120.0, dtype="float32")
        f[:: 7] = 0.0
        return f

    def load_voice(self, voice):
        ex = voice.extra or {}
        return {"gain": float(ex.get("fake_gain", 1.0)), "buzz": float(ex.get("fake_buzz", 0.0))}

    def convert(self, x16, lv, shift):
        import numpy as np
        t = np.arange(len(x16)) / 16000
        return (x16 * lv["gain"] + lv["buzz"] * np.sin(2 * np.pi * 120 * t)).astype("float32"), 16000

    def embed(self, x16):
        import numpy as np
        from undercover.doubleagent.frozen import ltas
        v = self.P.T @ ltas(x16)
        return (v / (np.linalg.norm(v) + 1e-9)).astype("float32")

    def content(self, x16):
        import numpy as np
        n = max(1, len(x16) // 320)
        return np.tile(np.linspace(0, 1, 16, dtype="float32"), (n, 1)) + 1e-3 * x16[:n, None] if len(x16) >= n else np.ones((n, 16), "float32")

    def read(self, text, voice, proj, seed):
        import numpy as np
        t = np.arange(24000 * 2) / 24000
        return (0.3 * np.sin(2 * np.pi * 140 * t) * (1 + 0.3 * np.sin(2 * np.pi * 3 * t))).astype("float32"), 24000, "clip:fake"

    def transcribe(self, files):
        return ["these are the words I really said that day, nothing more"] * len(files)


def _fake_project(name, person, n_phrases=30, other=6, n_sources=3):
    """A training project on disk with clips (short tones), a reference cluster, scores and embeddings: enough to freeze a test."""
    import numpy as np
    import soundfile as sf
    from undercover.doubleagent import project as P
    proj = P.Project.create(person, "speaking", name=name)
    rng = np.random.default_rng(3)
    (proj.dir / "clips").mkdir(parents=True, exist_ok=True)
    emb = {}
    ref = rng.normal(0, 1, 256).astype("float32")
    ref /= np.linalg.norm(ref)
    for k in range(n_phrases + other):
        pid = f"p{k:03d}"
        dur = 4.0 + (k % 3)
        t = np.arange(int(16000 * dur)) / 16000
        sf.write(str(proj.dir / "clips" / f"{pid}.flac"), (0.2 * np.sin(2 * np.pi * (110 + 5 * (k % 4)) * t)).astype("float32"), 16000)
        own = k < n_phrases
        e = (ref + rng.normal(0, 0.03 if own else 1.5, 256)).astype("float32")   # own: cosine ~0.9; others ~0
        emb[pid] = e / np.linalg.norm(e)
        proj.phrases.append(P.Phrase(id=pid, source=f"s{k % n_sources}", start=0.0, dur=dur, score=0.8 if own else 0.1,
                                     reason="" if own else "other speaker", noise=0.1))
    emb["speaker:1"] = ref
    from undercover.doubleagent.sources import Source
    proj.sources = [Source(id=f"s{i}", url="u", title=f"Source {i}", duration=600.0) for i in range(n_sources)]
    proj.reference = {"cluster": 1, "clip": "p000", "confirmed": True}
    proj.save()
    proj.save_embeddings(emb)
    return P.Project.load(name)


@check("the frozen test on disk (phase 26, R1 / R2 / R7): freeze writes refs, donors from other projects, the person's own lines and test.json; the approved voice is scored and set; a candidate is judged on the same test by its model hash; Keep moves the test with the name and swaps the approved pointer; the A/B reads the frozen lines")
def u_frozen_files():
    import numpy as np
    from undercover.core.voices import get_voice, voices_dir
    from undercover.doubleagent import frozen, improved, night
    eng = _FakeEngine()
    with isolated_data():
        proj = _fake_project("morgan-freeman", "Morgan Freeman")
        _fake_project("other-person", "Other Person", n_phrases=6, other=30, n_sources=12)   # the donors: two per source,
        _fake_project("third-person", "Third Person", n_phrases=6, other=30, n_sources=12)   # at most ten per project
        cur = _voice_dir("morgan-freeman-uc", {"trained": True, "project": "morgan-freeman", "f0_median": 105.0, "train_minutes": 13.0})
        (cur / "model.pth").write_bytes(b"approved-model")
        v = get_voice("morgan-freeman-uc")
        t, s = frozen.ensure(v, proj, eng)
        assert t["sex"] == "male" and t["project"] == "morgan-freeman" and t["approved"] == s["model"], (t.get("approved"), s["model"])
        assert len(t["donors"]) >= frozen.POOL_MIN and {d["project"] for d in t["donors"]} == {"other-person", "third-person"}, (len(t["donors"]), t["notes"])
        assert len(t["lines"]) >= frozen.LINES_MIN and t["read_with"] == "clip:fake" and t["notes"] == [], t
        d = frozen.test_dir(v)
        assert (d / "refs.npz").exists() and (d / "scores" / f"{s['model']}.json").exists()
        assert len(s["lines"]) == len(t["lines"]) and all(k in s["lines"][0] for k in ("id", "hnr", "contrast", "noise", "ltas"))
        assert all(k in s["donors"][0] for k in ("id", "cent", "content", "hnr", "contrast")), s["donors"][0]
        t2, s2 = frozen.ensure(v, proj, eng)                                  # idempotent, no second score
        assert t2["made"] == t["made"] and s2["model"] == s["model"]
        # a candidate: the same voice (gain 1) scores level and is NOT offered; its score is kept by model hash
        cand = _voice_dir("morgan-freeman-uc-next", {"trained": True, "project": "morgan-freeman", "f0_median": 105.0, "pending_for": "morgan-freeman-uc"})
        (cand / "model.pth").write_bytes(b"candidate-model")
        verdict = frozen.judge(v, get_voice("morgan-freeman-uc-next"), eng)
        assert not verdict["offer"] and verdict["candidate_model"] != verdict["approved_model"], verdict["terms"]
        assert abs(verdict["compare"]["lines"]["id"]["mean"]) < 0.02 and verdict["before"] is not None
        assert (d / "scores" / f"{verdict['candidate_model']}.json").exists()
        assert "likeness is unchanged" not in frozen.words_of(verdict)                 # not offered: no words
        # R7: the A/B inputs are the frozen lines
        ab = frozen.ab_inputs(v, n=2)
        assert len(ab) == 2 and ab[0]["file"].exists() and ab[0]["text"]
        # Keep the candidate: the test follows the name, the approved pointer is the kept model
        night.mark_improved_pending("morgan-freeman-uc", "morgan-freeman-uc-next", verdict["before"], verdict["after"], 13.0,
                                    words="cleaner voicing", measures={"terms": verdict["terms"]})
        st = improved.state("morgan-freeman-uc")
        assert st["words"] == "cleaner voicing" and st["measures"]["terms"], st
        improved.keep("morgan-freeman-uc", "new")
        v2 = get_voice("morgan-freeman-uc")
        assert (v2.dir / "model.pth").read_bytes() == b"candidate-model" and (v2.dir / "test" / "test.json").exists()
        assert frozen.load_test(v2)["approved"] == verdict["candidate_model"], frozen.load_test(v2)["approved"]
        assert not any(".aside" in str(p) and (p / "test").exists() for p in (voices_dir() / ".aside").iterdir())
        improved.undo("morgan-freeman-uc")                                            # Undo: back with the old name and pointer
        v3 = get_voice("morgan-freeman-uc")
        assert (v3.dir / "model.pth").read_bytes() == b"approved-model" and frozen.load_test(v3)["approved"] == verdict["approved_model"]
        assert not (voices_dir() / "morgan-freeman-uc-next" / "test").exists()
    return f"{len(t['donors'])} donors, {len(t['lines'])} lines, scored by hash; level candidate not offered; keep / undo carry the test"


@check("the source gate on a project (phase 26, R6): a failing source is unticked with the reason AND its cut phrases are left out of training; the user's own sources and ticked phrases are never touched; the report says kept (R3a)")
def u_frozen_gate_project():
    import numpy as np
    from undercover.core.worklist import report_words
    from undercover.doubleagent import frozen
    with isolated_data():
        proj = _fake_project("dave", "Dave Chappelle")
        emb = proj.load_embeddings()
        # source s2's phrases pulled far from the reference: it fails on cos; s1 is the user's own
        for ph in proj.phrases:
            if ph.source == "s2" and ph.kept:
                e = np.random.default_rng(int(ph.id[1:])).normal(0, 1, 256).astype("float32")
                emb[ph.id] = e / np.linalg.norm(e)
        proj.sources[1].user = True
        ticked = next(ph for ph in proj.phrases if ph.source == "s2" and ph.kept)
        ticked.choice = True                                                         # the user ticked this one by hand
        proj.save_embeddings(emb)
        before = proj.kept_seconds()
        g = frozen.gate_project(proj, emb, 101.0, pitch_of=lambda clip: 320.0 if "p001" in str(clip) else 101.0)
        assert [x["title"] for x in g["left_out"]] == ["Source 2"], g["left_out"]
        assert "another voice" in g["left_out"][0]["why"]
        assert not proj.source("s2").selected and proj.source("s2").note.startswith("left out by Improve"), (proj.source("s2").selected, proj.source("s2").note)
        assert all(not ph.kept for ph in proj.phrases if ph.source == "s2" and ph.choice is None), "s2's phrases still train"
        assert ticked.kept and ticked.reason == "", "the user's hand-ticked phrase was touched"
        assert proj.source("s1").selected and all(ph.kept for ph in proj.phrases if ph.source == "s1" and ph.reason == "")
        assert g["kept_min"] < before / 60 and abs(proj.kept_seconds() / 60 - g["kept_min"]) < 0.1, (g["kept_min"], before)
        w = report_words("improve", {"kept": True, "before": 0.78, "after": 0.79, "tried": {"added_min": 5.0, "left_out": [{"title": "x", "why": "y"}]}}, "Dave Chappelle")
        assert w["outcome"] == "not_better" and "yours is kept" in w["note_plain"] and "1 source left out" in w["note_plain"], w
        w = report_words("improve", {"kept": True, "nothing_new": True, "tried": {"added_min": 0.0, "left_out": []}}, "Dave Chappelle")
        assert w["outcome"] == "same" and w["note_plain"].startswith("Nothing new of Dave Chappelle") and "kept as it is" in w["note_plain"], w
    return "s2 unticked with its phrases left out; the user's s1 untouched; kept row"


@check("DoubleAgent's project Pause holds (Internal Affairs P2-2): it pauses that job and sets the hold, so the queue loop doesn't start it again; prepare jobs (no trainer control) pause too")
def u_project_pause_holds():
    from undercover.core import jobs as J, worklist
    from undercover.doubleagent import api as DA, project as P
    with isolated_data():
        worklist._STATE = None
        proj = P.Project.create("Pause Test", "speaking")
        class Ctl:
            pause = stop = False
        for kind, ctl in (("train-auto", Ctl()), ("train-prepare", None)):
            job = J.Job(kind)
            DA.ACTIVE[proj.name] = {"job": job, "control": ctl, "kind": "auto" if ctl else "prepare"}
            class H:
                def _json(self, obj, status=200):
                    self.out = obj
            try:
                worklist._state()["held"] = False
                DA._pause(H(), proj, {})
                assert job.pausing and worklist.held() and worklist.hold()["held_by"] == "you", kind
                if ctl:
                    assert ctl.pause
            finally:
                DA.ACTIVE.pop(proj.name, None)
        worklist._STATE = None
    return "the trained job and the prepare job both pause and hold"


@check("the cadence A/B samples are served with Range like /files (Internal Affairs P2-3): through the server's _send_file (206, Accept-Ranges)")
def u_cadence_sample_range():
    import tempfile
    from undercover.doubleagent import api as DA, cadence
    f = Path(tempfile.mkdtemp()) / "sample.wav"
    f.write_bytes(b"RIFF....WAVE")
    real = cadence.sample_file
    cadence.sample_file = lambda voice, which: f
    sent = []
    class H:
        def _send_file(self, path, download=False, cache=None):
            sent.append((path, cache))
    try:
        DA._cadence_ab(H(), "GET", "some-voice", "sample/current")
    finally:
        cadence.sample_file = real
        f.unlink(missing_ok=True)
    assert sent == [(f, "no-store")], sent
    return "served by _send_file (Range, 206)"


@check("Stop during a model download aborts it within a chunk (Internal Affairs P2-4); a stopped training says stopped and keeps the project's exported voice (P2-5); in test mode a model download never writes outside the test's folders (the 48k base models once landed in the real models folder)")
def u_stop_download():
    import inspect
    import threading
    import http.server
    from undercover.core import setup_models
    from undercover.core.voices import ModelError
    from undercover.doubleagent import api as DA, night, service, trainer
    with isolated_data() as box:
        # a slow local server: 64 MB in 1 MB chunks, so a Stop after the first chunks must end it early
        class Slow(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", str(64 << 20))
                self.end_headers()
                try:
                    for _ in range(64):
                        self.wfile.write(b"\0" * (1 << 20))
                        time.sleep(0.05)
                except OSError:
                    pass
            def log_message(self, *a):
                pass
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Slow)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            got = {"n": 0}
            def stop_soon():
                got["n"] += 1
                return got["n"] > 3
            t0 = time.time()
            try:
                setup_models.fetch(f"http://127.0.0.1:{srv.server_port}/big.bin", box / "big.bin", None, "big.bin", cancel=stop_soon)
                raise AssertionError("the download ran to the end")
            except setup_models.DownloadStopped:
                pass
            assert time.time() - t0 < 3 and not (box / "big.bin").exists(), "Stop didn't end the download early"
        finally:
            srv.shutdown()
        # test mode: a missing model under the real models folder is refused, nothing written there
        real = real_dirs()["models_dir"] / "smoke-never-here.bin"   # the user's models folder, outside the test roots
        try:
            setup_models.ensure(files={str(real): "file:///nonexistent"})
            raise AssertionError("a test downloaded into the real models folder")
        except ModelError as e:
            assert "Test mode" in str(e) or "outside" in str(e), e
        assert not real.exists()
    # P2-5: no reset clears the project's exported voice; a stopped run is "stopped"
    for fn in (DA.start_train, night._improve, service.train_new_voice, service.run_project):
        src = inspect.getsource(fn)
        assert '"exported", "exported_epoch"' not in src, f"{fn.__name__} still clears exported"
    assert 'st["state"] = "stopped" if outcome == "stopped"' in inspect.getsource(trainer.Trainer.run)
    return "a download stops within a chunk; test mode refuses the real models folder; exported kept; stopped said"


# ---------------------------------------------------------------- API checks (test server)

@check("jobs: a paused job saves its resume point; a paused one-shot resumes its own song and training")
def u_paused():
    """Known bug fixed in phase 4: run_job's Paused branch returned before worklist.remember(job)."""
    from undercover.core import jobs, worklist
    from undercover.core.jobs import Job, Paused
    from undercover.core.config import path
    with isolated_data():   # the scratch queue file goes in the test's own cache, never the user's
        old_port, old_state = jobs.PORT, worklist._STATE
        jobs.PORT, worklist._STATE = 7999, None   # a scratch queue file: never the real one
        f = path("cache_dir") / "worklist-7999.json"
        try:
            worklist.PAUSABLE.add("zz-smoke")
            job = Job("zz-smoke")
            job.resume_spec = {"kind": "zz-smoke", "label": "smoke"}

            def fn(job):
                raise Paused()
            jobs.run_job(job, fn)
            assert job.state == "paused", job.state
            assert _bare(worklist._state()["resume"]) == [job.resume_spec], worklist._state()["resume"]
            worklist.forget_paused()
            assert worklist._state()["resume"] == [], "Dismiss didn't forget it"
            # the one-shot pins what it chose before pausing (oneshot.run's except Paused)
            import inspect
            from undercover.frontman import oneshot
            src = inspect.getsource(oneshot.run)
            assert 'spec["song_url"]' in src and 'spec["prefer_train"] = True' in src
        finally:
            worklist.PAUSABLE.discard("zz-smoke")
            jobs.PORT, worklist._STATE = old_port, old_state
            f.unlink(missing_ok=True)


@check("time left: every progress event gets total_left (whole-job and per-stage kinds, learned stage times)")
def u_eta():
    """Phase 4c: an ETA on every bar. Job.push adds total_left from the job's own eta, else extrapolates its progress
    (a staged kind adds the typical length of the stages still to come, learned from past runs)."""
    from undercover.core import jobs
    t = [1000.0]
    real = jobs.time.time
    jobs.time.time = lambda: t[0]
    old = jobs._ETA_FILE
    try:
        jobs._ETA_FILE = {"auto": {"total": [100], "stages": {"song": [20], "voice": [10], "cover": [60]}}}
        j = jobs.Job("zz-whole")
        for k, p in enumerate((0.1, 0.2, 0.3, 0.5)):
            j.push(stage="x", frac=p)
            t[0] += 5
        # 0.1 at t0, 0.5 at t0 + 15 s: 15 s per 0.4, so the 0.5 left takes 18.75 s
        assert abs(j.events[-1]["total_left"] - 18.8) < 0.11, j.events[-1]
        a = jobs.Job("auto")
        a.push(stage="song", frac=0.0)
        t[0] += 4
        a.push(stage="song", frac=0.5)   # no rate yet within the stage: typical song left (20-4) + voice + cover
        assert a.events[-1]["total_left"] == 86.0, a.events[-1]
        e = jobs.Job("speech")
        e.push(stage="reading", frac=0.0, total_left=12.0)
        assert e.events[-1]["total_left"] == 12.0
        tr = jobs.Job("train")
        tr.push(stage="training", fraction=0.3, eta=300)
        assert tr.events[-1]["total_left"] == 300
    finally:
        jobs.time.time = real
        jobs._ETA_FILE = old


@check("cadence: text measures, reference rule, add-ons registry")
def u_cadence():
    from undercover.doubleagent import cadence
    from undercover.core import addons
    from undercover.core.cadence import ENGINES, default_engine
    assert cadence.syllables("Is that the kind of country we want to leave to our children?") == 15
    assert cadence.wer(["it was not easy"], ["it was not easy."]) == 0.0
    assert cadence._plain("We did it together.") and not cadence._plain("In 2008, etc.")
    assert {"chatterbox", "chatterbox-turbo", "f5"} <= set(ENGINES) and default_engine() in ENGINES
    # phase 23: the Chatterbox family reads each other's packs, so a pack outlives the engine it was made with; Turbo is the
    # default, a setup that only has Chatterbox's files keeps reading with it (no 2.8 GB download unasked), and a voice can
    # be pinned to one engine by the user's ear (the pack's engine_pin)
    from undercover.core.cadence import engines as E
    have = set()
    saved = (E.weights_present, E.engine_ready, E.CONFIG)
    E.weights_present = lambda e: e in have
    # (the original Chatterbox is an open add-on since rc-fix4b: ready when its files are there)
    E.engine_ready = lambda e: e in ENGINES and (ENGINES[e].get("addon") is None or (e == "chatterbox" and e in have))
    try:
        E.CONFIG = {"cadence": {"engine": "chatterbox-turbo"}}
        assert E.default_engine() == "chatterbox-turbo" and E.reading_engine("chatterbox") == "chatterbox-turbo"   # a fresh install
        have.add("chatterbox")
        assert E.default_engine() == "chatterbox" and E.reading_engine("chatterbox-turbo") == "chatterbox"   # only the old files
        have.add("chatterbox-turbo")
        assert E.default_engine() == "chatterbox-turbo" and E.reading_engine("chatterbox") == "chatterbox-turbo"
        assert E.pack_ready("chatterbox") and E.reading_engine("f5") == "f5"
        assert E.pack_engine({"engine": "chatterbox", "engine_pin": "chatterbox"}) == "chatterbox"   # a voice pinned to the old engine
        assert E.pack_engine({"engine": "chatterbox"}) == "chatterbox-turbo" and E.pack_engine(None) == "chatterbox-turbo"
        assert E.pack_engine({"engine_pin": "f5"}) == "chatterbox-turbo"   # a pin only names a family member
        E.CONFIG = {"cadence": {"engine": "f5"}}   # another engine is the default: a Chatterbox pack still reads with the family
        E.engine_ready = lambda e: e in ("f5", "chatterbox")
        assert E.default_engine() == "f5" and E.reading_engine("chatterbox-turbo") == "chatterbox"
        assert E.pack_engine({"engine_pin": "chatterbox-turbo"}) == "chatterbox"   # a pin to an engine that isn't there is ignored
    finally:
        E.weights_present, E.engine_ready, E.CONFIG = saved
    assert not ENGINES["chatterbox-turbo"].get("pace_by_ref")   # the pace calibration is off (it read slurred)
    # phase 23: Turbo's reading sits lower than the person's (fine: the median onto theirs, half semitones, an octave at most)
    import numpy as np
    from undercover.core.cadence import render as R

    class _Rm:
        def __init__(self, hz):
            self.hz = hz

        def infer_from_audio(self, x, thred=0.03):
            return np.full(100, self.hz, np.float32)
    x = np.zeros(16000, np.float32)
    assert R.octave_shift(x, _Rm(240.0), 235.0) == 0.0 and R.octave_shift(x, _Rm(120.0), 235.0) == 12.0   # octaves only, as before
    assert R.octave_shift(x, _Rm(138.0), 235.0, fine=True) == 9.0 and R.octave_shift(x, _Rm(235.0), 235.0, fine=True) == 0.0
    assert R.octave_shift(x, _Rm(30.0), 235.0, fine=True) == 12.0 and R.octave_shift(x, _Rm(900.0), 235.0, fine=True) == -12.0
    hist = []   # the running median: a higher sentence (a question) among lower ones isn't pulled onto the median
    assert [R.octave_shift(x, _Rm(hz), 235.0, fine=True, history=hist) for hz in (138.0, 138.0, 200.0)] == [9.0, 9.0, 9.0], hist
    assert ENGINES["chatterbox-turbo"]["pitch_to_target"] and not ENGINES["chatterbox"].get("pitch_to_target")
    # phase 23: a pack's pace is calibrated once per engine (the reference clip read at a speed that lands the natural pace
    # near the person's own); the search, with a made-up engine whose pace follows the reference's speed to the power 0.5
    from undercover.doubleagent import cadence as DK
    calls = []

    def fake(target_at_1):
        def read(r):
            calls.append(r)
            return target_at_1 * r ** 0.5
        return read
    r, tried = DK.choose_rate(fake(5.4), 3.6)   # 1.5x too fast (Freeman's case): about 0.44 wanted, the range stops at 0.5
    assert r == 0.5 and len(tried) <= DK.PACE_READS and abs(tried[r] / 3.6 - 1) < 0.30, (r, tried)
    r, tried = DK.choose_rate(fake(4.2), 3.6)   # 17% fast: one jump lands it
    assert 0.6 < r < 0.85 and abs(tried[r] / 3.6 - 1) <= DK.PACE_TOL, (r, tried)
    calls.clear()
    r, tried = DK.choose_rate(fake(3.8), 3.6)   # already within 10%: one reading, the clip as it is
    assert r == 1.0 and calls == [1.0], (r, calls)
    r, tried = DK.choose_rate(fake(3.0), 3.6)   # slow: a faster reference, but not past the range
    assert 1.0 < r <= DK.PACE_RATE_HI, (r, tried)
    eng = DK.C.reading_engine("chatterbox")
    assert DK.pace_needs_calibration(None) is False
    assert DK.pace_needs_calibration({"engine": "chatterbox", "pace_cal": {"engine": eng}}) is False   # done once per engine
    assert DK.pace_needs_calibration({"engine": "chatterbox"}) is bool(DK.C.engine_info(eng).get("pace_by_ref"))
    x = np.zeros(32000, np.float32)
    x[8000:24000] = np.random.RandomState(1).randn(16000).astype(np.float32) * 0.1
    st = DK._speech_stats(x, "one two three four")
    assert st and abs(st["dur"] - 1.0) < 0.1 and st["rate"] == st["syl"] / st["dur"] and DK._speech_stats(x[:100], "a") == {}, st
    ids = {a["id"] for a in addons.status()}
    assert "f5" in ids, ids
    # nothing listed that can't be had (MasterPlan, the user's fresh install: XTTS-v2 sat there as 'Not available yet')
    assert "xtts" not in ids and all(a.wired for a in addons.ADDONS.values()), [a.id for a in addons.ADDONS.values() if not a.wired]
    # the accept / install / remove path, with a tiny fake add-on (no packages, no files), in its own data folders
    with isolated_data():
        _fake_addon_roundtrip(addons)
    return f"engine {default_engine()}, add-ons: {', '.join(sorted(ids))}"


def _fake_addon_roundtrip(addons):
    state = {"on": False}
    fake = addons.register(addons.Addon(id="zz-smoke", name="Smoke", adds="nothing", licence="TEST-1.0", licence_url="-",
                                        licence_summary="test only", size_mb=0, check=lambda: state["on"]))
    try:
        try:
            addons.install(fake.id, accept=False)
            raise AssertionError("installed without accepting")
        except addons.ModelError:
            pass
        assert not addons.available(fake.id)
        state["on"] = True   # what its packages/files would do
        r = addons.install(fake.id, accept=True)
        assert r["accepted"] and r["installed_on"] and addons.available(fake.id), r
        state["on"] = False
        r = addons.remove(fake.id)
        assert not r["installed_on"] and r["accepted"] and not addons.available(fake.id), r
    finally:
        addons.ADDONS.pop(fake.id, None)


@contextlib.contextmanager
def isolated_data():
    """Every [paths] folder, config.toml and the storage allow-list point into a fresh temp dir for the duration (the
    rule in this file's header). Asserts nothing points outside it before handing over; restores everything after."""
    import copy
    import tempfile
    from undercover.core import config, storage
    # the whole in-memory settings, not just [paths]: run from the main checkout they're the user's (layout-fix: a game
    # pause test saw the user's never-ask list), and a test's set_values must not leak into the checks after it
    saved = (copy.deepcopy(config.CONFIG), config.CONFIG_FILE, storage.ALLOWED_ROOTS)
    with tempfile.TemporaryDirectory(prefix="uc-smoke-") as td:
        t = Path(td)
        app = t / "app"
        keys = {"models_dir": "models", "cache_dir": "cache", "output_dir": "output", "logs_dir": "logs", "uploads_dir": "uploads"}
        cfg = t / "config.toml"   # a minimal config of its own: never a copy of the real one (it holds the real paths)
        cfg.write_text("[paths]\n" + "".join(f"{k} = '{app / v}'\n" for k, v in keys.items())
                       + '\n[cadence]\nengine = "chatterbox"\n', encoding="utf-8")
        try:
            config.CONFIG_FILE = cfg
            fresh = config.read(cfg)   # the defaults + this test's own file: the user's settings never steer a check
            config.CONFIG.clear()
            config.CONFIG.update(fresh)   # (in place: modules hold this very dict)
            config.CONFIG["paths"] = {k: str(app / v) for k, v in keys.items()}
            storage.ALLOWED_ROOTS = [t]
            for k in (*keys, "voices_dir"):
                p = config.path(k)
                assert storage._inside(p, t) and not storage._inside(p, ROOT), f"{k} -> {p} is outside the test's folder"
            yield t
        finally:
            config.CONFIG.clear()
            config.CONFIG.update(saved[0])
            config.CONFIG_FILE, storage.ALLOWED_ROOTS = saved[1], saved[2]


@check("add-ons: registry, CLI list, a fake add-on installs (checksum) and removes cleanly")
def u_addons():
    import subprocess
    from undercover.core import addons
    from undercover.core.config import path
    out = subprocess.run([sys.executable, "-m", "undercover.addons", "list", "--json"], cwd=ROOT, capture_output=True,
                         text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-400:]
    rows = {r["id"]: r for r in json.loads(out.stdout)}
    assert {"f5", "chatterbox", "whisper", "kokoro", "rvc", "separation", "speaker"} <= set(rows) and "xtts" not in rows, sorted(rows)
    assert rows["f5"]["installable"] and rows["kokoro"]["included"], rows["f5"]
    # rc-fix4b: the original Chatterbox is an optional add-on (open licence: nothing to accept; Turbo comes with setup)
    assert rows["chatterbox"]["installable"] and not rows["chatterbox"]["included"] and rows["chatterbox-turbo"]["included"]
    assert addons.ADDONS["chatterbox"].open and addons.ADDONS["chatterbox"].files
    with isolated_data() as t:
        src = t / "fake.bin"
        src.write_bytes(b"undercover smoke " * 4096)
        fake = addons.register_test_addon(src)
        dest = path("models_dir") / "_test" / "test-addon.bin"
        try:
            for aid, ok in (("test", False), ("whisper", True)):   # no acceptance; an included part
                try:
                    addons.install(aid, accept=ok)
                    raise AssertionError(f"{aid} installed (accept={ok})")
                except addons.ModelError:
                    pass
            r = addons.install("test", accept=True)
            assert r["installed"] and r["accepted"] and addons.available("test") and dest.exists(), r
            r = addons.remove("test")
            assert not r["installed"] and r["accepted"] and r["freed"] == src.stat().st_size and not dest.parent.exists(), r
            fake.sha256["_test/test-addon.bin"] = "0" * 64   # a download that doesn't match its checksum is refused
            try:
                addons.install("test", accept=True)
                raise AssertionError("a file with the wrong checksum was installed")
            except addons.ModelError:
                assert not dest.exists()
        finally:
            addons.ADDONS.pop("test", None)
    return f"{len(rows)} parts, {sum(not r['included'] for r in rows.values())} optional"


@check("yt-dlp pinned install (phase 8; since phase 16 the Update button is the self-heal): hashed, no pin or nothing newer = no install")
def u_ytdlp():
    from undercover.core import addons
    from undercover.safehouse import api as shapi, library
    name, ver, sha = addons.pin_of("ytdlp")
    common = [ln.strip() for ln in (ROOT / "installer" / "requirements" / "common.txt").read_text(encoding="utf-8").splitlines()]
    i = common.index(f"yt-dlp=={ver} \\")   # the installer carries the same pin as the registry
    assert common[i + 1] == f"--hash=sha256:{sha}", "installer/requirements/common.txt and core/addons.py pin different yt-dlp wheels"
    calls, state = [], {"v": "2026.1.1", "installs": True}
    real_pip, real_ver = addons._pip, addons.package_version

    def fake_pip(*args, what=""):   # never a real install: record the arguments and the requirements file it was given
        calls.append((args, Path(args[args.index("-r") + 1]).read_text(encoding="utf-8")))
        if state["installs"]:
            state["v"] = ver

    class _R:
        def __init__(self):
            self.out = self.err = None
            try:
                self.out = addons.update_pinned("ytdlp")
            except Exception as e:   # noqa: BLE001
                self.err = str(e)

    def post():   # the pinned install itself (the System room's button runs core/ytdlp's heal since phase 16)
        return _R()
    addons._pip, addons.package_version = fake_pip, lambda n: state["v"]
    try:
        with isolated_data():
            assert addons.updated("ytdlp") is None and addons.pin_state("ytdlp")["update"] is True
            h = post()
            assert not h.err and h.out == {"before": "2026.1.1", "after": ver, "pinned": ver, "changed": True}, (h.out, h.err)
            args, req = calls[-1]
            assert len(calls) == 1 and req == f"yt-dlp=={ver} --hash=sha256:{sha}\n", (calls, req)
            assert "--require-hashes" in args and "--no-deps" in args and "--upgrade" not in args, args
            assert args[args.index("-c") + 1] == str(ROOT / "installer" / "constraints.txt") and "yt-dlp" not in args, args
            import re
            assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d", addons.updated("ytdlp") or ""), addons.updated("ytdlp")
            info = addons.pin_state("ytdlp")
            assert info["installed"] == info["pinned"] == ver and info["update"] is False and addons.updated("ytdlp"), info
            h = post()   # equal: nothing to do
            assert h.out["changed"] is False and len(calls) == 1, (h.out, calls)
            state["v"] = "2099.1.1"   # newer than the pin: left alone
            h = post()
            assert h.out["changed"] is False and len(calls) == 1 and addons.pin_state("ytdlp")["update"] is False
            state["v"] = None   # not installed: that would be an install, which needs an OK
            h = post()
            assert h.err and len(calls) == 1 and addons.pin_state("ytdlp")["update"] is False, (h.err, calls)
            state.update(v="2026.1.1", installs=False)   # pip "succeeds" but the version didn't move: reported, not recorded
            addons.forget("ytdlp")
            h = post()
            assert h.err and len(calls) == 2 and addons.updated("ytdlp") is None, (h.err, calls)
            hashes = dict(addons.ADDONS["ytdlp"].hashes)
            addons.ADDONS["ytdlp"].hashes.clear()   # no pin: refused before pip is reached
            try:
                h = post()
                assert h.err and "pinned" in h.err and len(calls) == 2, (h.err, calls)
            finally:
                addons.ADDONS["ytdlp"].hashes.update(hashes)
    finally:
        addons._pip, addons.package_version = real_pip, real_ver
    return f"pin {ver}, hash checked against the installer's list; 5 cases, no real install"


@check("data locations: guards, then a scratch move of model files and the library (voices stay the library's)")
def u_storage():
    from undercover.core import config, storage

    class FakeJob:
        id, cancel = "smoke", False

        def push(self, **ev):
            pass
    # the guards, checked without touching anything: the app's real folders are out of reach
    assert storage.refuse([ROOT / "uploads"]) and storage.refuse([ROOT / "output"], from_room=True)
    assert storage.refuse([ROOT / "models"], from_room=True, confirmed=True) is None   # only the room, confirmed
    with isolated_data() as t:
        app, lib, mod = t / "app", t / "lib2", t / "models2"
        assert storage.refuse([ROOT / "cache"], True, True), "test mode must refuse the app folder"
        assert storage.refuse([t.parent / "not-this-test"]), "test mode must refuse folders outside the test's own"
        # regression (2026-10-11): writing [paths] must not reload the file's other keys into the running config
        text = config.CONFIG_FILE.read_text(encoding="utf-8").replace(f"'{app / 'uploads'}'", '"uploads"')
        config.CONFIG_FILE.write_text(text, encoding="utf-8")
        config.set_values("paths", {"logs_dir": f"'{app / 'logs'}'"})
        assert config.CONFIG["paths"]["uploads_dir"] == str(app / "uploads"), config.CONFIG["paths"]
        assert storage._inside(config.path("uploads_dir"), t)
        for d in ("uploads", "output", "models/voices/v1", "models/tts"):
            (app / d).mkdir(parents=True, exist_ok=True)
        (app / "models/tts/m.bin").write_bytes(b"m" * 5000)
        (app / "models/voices/v1/meta.json").write_text(json.dumps({"dir": str(app / "models/voices/v1")}), encoding="utf-8")
        (app / "uploads/s.mp3").write_bytes(b"s" * 3000)
        (app / "output/c.mp3").write_bytes(b"c" * 2000)
        (app / "output/history.jsonl").write_text(json.dumps({"output": str(app / "output/c.mp3")}) + "\n", encoding="utf-8")
        storage.move("models", str(mod), FakeJob())                      # model files go; voices stay
        assert (mod / "models/tts/m.bin").exists() and not (mod / "models/voices").exists()
        assert (app / "models/voices/v1/meta.json").exists() and not (app / "models/tts").exists()
        meta = json.loads((app / "models/voices/v1/meta.json").read_text(encoding="utf-8"))
        assert meta["dir"] == str(app / "models/voices/v1"), meta   # voices stayed: their stored paths too
        storage.move("library", str(lib), FakeJob())                     # songs, covers, voices together
        assert (lib / "voices/v1/meta.json").exists() and (lib / "uploads/s.mp3").exists() and not (app / "output").exists()
        hist = json.loads((lib / "output/history.jsonl").read_text(encoding="utf-8"))
        assert hist["output"] == str(lib / "output" / "c.mp3"), hist
        assert json.loads((lib / "voices/v1/meta.json").read_text(encoding="utf-8"))["dir"] == str(lib / "voices" / "v1")
        storage.move("library", str(t / "lib3"), FakeJob())              # and on again
        assert (t / "lib3/voices/v1/meta.json").exists() and not (lib / "voices").exists()
        assert not storage.check("library", str(t / "lib3" / "output"))["ok"]
        for k in ("models_dir", "uploads_dir", "output_dir", "voices_dir", "cache_dir"):
            assert storage._inside(config.path(k), t), (k, config.path(k))
        text = config.CONFIG_FILE.read_text(encoding="utf-8")
        assert "models2" in text and "lib3" in text and "[cadence]" in text, text
    return "guards hold; models, then library (paths rewritten), then library again"


def _fake_cover(i, tag="c"):
    """A finished cover's history record with real (tiny) files, in the isolated output / cache folders."""
    from undercover.core.config import path
    out, conv = path("output_dir"), path("cache_dir") / "converted"
    out.mkdir(parents=True, exist_ok=True)
    conv.mkdir(parents=True, exist_ok=True)
    o, v = out / f"{tag}{i:02d}.mp3", conv / f"{tag}{i:02d}__v.flac"
    o.write_bytes(b"x" * 100)
    v.write_bytes(b"v" * 50)
    return {"output": str(o), "song": str(out / "song.wav"), "voice": "zz", "stems": {}, "converted_vocal": str(v),
            "converted_backing": None, "notes": [], "seconds": {}, "options": {}, "title": f"Cover {i}",
            "created": f"2026-01-01 {i // 3600:02d}:{i // 60 % 60:02d}:{i % 60:02d}"}


def _fake_speech(i, speak, tag="s"):
    """A finished speech made by the real _save (files, JSON, roll-off), with its sentences folder."""
    import numpy as np
    from undercover.core.config import path
    job = f"{tag}job{i:02d}"
    parts = speak.parts_dir(job)
    parts.mkdir(parents=True, exist_ok=True)
    (parts / "s0000.wav").write_bytes(b"p" * 10)
    p = {"voice": "zz", "reader": "am_michael", "auto": True, "speed": 1.0, "pitch": 0.0}
    item = speak._save(np.zeros(2400, np.float32), 24000, f"Speech number {i} {tag}", p, {"total": 1.0}, job)
    return item


@check("keep or roll off (covers): the 16th finish takes the kept oldest out of the tray, the 17th deletes the next, never more than one")
def u_rolloff_covers():
    """Phase 6 guard rails (core/tray.py): at most one track leaves per finish, never a kept one, never outside the
    module's folders, and never anywhere near the real folders from a test. Everything runs in isolated_data()."""
    from undercover.core import storage, tray
    from undercover.core.config import path
    from undercover.frontman import history as H, pipeline
    ids_of = lambda: [i["id"] for i in H.finished()]
    files_left = lambda: sorted(p.name for p in path("output_dir").glob("*.mp3"))
    with isolated_data() as t:
        # the rule itself, on every shape of list: at most one, never kept, always the 16th newest
        import random
        rnd = random.Random(6)
        for n in range(0, 41):
            items = [{"id": f"{k:010x}", "kept": rnd.random() < 0.4} for k in range(n)]
            lv = tray.leaver(items)
            assert lv is None or (lv is items[n - 16] and not lv["kept"] and n > 15), (n, lv)
            if n <= 15 or items[n - 16]["kept"]:
                assert lv is None, (n, lv)
        recs = []
        for i in range(15):                       # the tray fills up: nothing leaves
            recs.append(H.record(_fake_cover(i)))
        assert len(ids_of()) == 15 and len(files_left()) == 15
        H.set_kept(recs[0]["id"], True)           # keep the oldest
        assert H.get(recs[0]["id"])["kept"] and H.get(recs[0]["id"])["kept_at"]
        recs.append(H.record(_fake_cover(15)))    # the 16th finish: the oldest (kept) leaves the tray, nothing is deleted
        assert len(ids_of()) == 16 and len(files_left()) == 16, "the kept oldest was deleted or lost"
        hist = pipeline.history()                 # newest first, as SafeHouse lists it
        assert [h["id"] for h in hist if h["recent"]] == [r["id"] for r in reversed(recs[1:])], "the tray isn't the latest 15"
        assert hist[-1]["id"] == recs[0]["id"] and hist[-1]["kept"] and not hist[-1]["recent"], "the kept oldest isn't in the library"
        assert H.resolved_output(H.get(recs[0]["id"])).exists() and Path(H.get(recs[0]["id"])["converted_vocal"]).exists()
        assert len(pipeline.history_items()) == 15, "the page's list is the tray"
        recs.append(H.record(_fake_cover(16)))    # the 17th: the next oldest (unkept) is deleted, exactly one
        assert ids_of() == [r["id"] for r in recs if r["id"] != recs[1]["id"]], "not exactly the oldest unkept one left"
        assert "c01.mp3" not in files_left() and len(files_left()) == 16, files_left()
        assert not (path("cache_dir") / "converted" / "c01__v.flac").exists(), "its converted vocal stayed behind"
        assert (path("cache_dir") / "converted" / "c02__v.flac").exists() and "c00.mp3" in files_left()
        log = (path("logs_dir") / "server.log").read_text(encoding="utf-8")
        assert log.count("roll-off (covers): removed") == 1 and recs[1]["id"] in log, log
        H.set_kept(recs[5]["id"], True)           # a kept one in the middle of the tray is passed over when its turn comes
        deleted = 1
        for i in range(17, 30):                   # many finishes: exactly one leaves the tray each time, a kept one is never deleted
            before = H.finished()
            fell = before[-15]                    # (old list + the new one)[-16]: the one that falls out of the latest 15
            n_files = len(files_left())
            recs.append(H.record(_fake_cover(i)))
            assert len(files_left()) == n_files + 1 - (0 if fell.get("kept") else 1), (i, "wrong number of covers removed")
            assert (fell["id"] in ids_of()) == bool(fell.get("kept")), (i, "the one that fell out is kept or deleted wrongly")
            deleted += 0 if fell.get("kept") else 1
        assert {recs[0]["id"], recs[5]["id"]} <= set(ids_of()) and {"c00.mp3", "c05.mp3"} <= set(files_left()), "a kept cover was deleted"
        want = {recs[0]["id"], recs[5]["id"]} | {r["id"] for r in recs[-15:]}
        assert set(ids_of()) == want, "the library should be the kept ones plus the latest 15"
        assert (path("logs_dir") / "server.log").read_text(encoding="utf-8").count("roll-off (covers): removed") == deleted
        H.set_kept(recs[0]["id"], False)          # unkeeping what already left the tray does NOT delete it (only a finish rolls)
        assert recs[0]["id"] in ids_of() and "c00.mp3" in files_left()
        # a failing delete never fails the cover and removes nothing: a line pointing outside the module's folders
        evil = path("output_dir").parent / "elsewhere"
        evil.mkdir()
        (evil / "precious.mp3").write_bytes(b"keep me")
        target = H.finished()[-15]                # the one that falls out on the next finish
        assert not target.get("kept")
        H.update(target["id"], output=str(evil / "precious.mp3"))
        before = set(ids_of())
        H.record(_fake_cover(41))                 # its output is outside output_dir: refused, nothing removed, the cover still saved
        assert (evil / "precious.mp3").read_bytes() == b"keep me", "roll-off deleted a file outside the output folder"
        assert before <= set(ids_of()) and len(ids_of()) == len(before) + 1, "a line vanished although its delete was refused"
        assert Path(target["converted_vocal"]).exists(), "a refused roll-off removed part of the cover"
        assert "roll-off (covers): failed" in (path("logs_dir") / "server.log").read_text(encoding="utf-8")
        # test mode refuses the app's real folders whatever a history line says (stubbed delete: nothing real can go)
        real = ROOT / "installer" / "constraints.txt"
        assert real.exists()
        calls = []
        old_retry, storage.fsutil.retry = storage.fsutil.retry, lambda fn, *a, **k: calls.append(1)
        try:
            for within in ([ROOT], [real.parent], [t]):
                try:
                    storage.remove_files([real], within)
                    raise AssertionError(f"a real file inside {within} wasn't refused in test mode")
                except storage.ModelError:
                    pass
            try:
                # (a folder that exists: a fresh checkout or worktree has no output/, and what isn't there isn't refused)
                storage.remove_files([], within=[t], dirs=[ROOT / "output" if (ROOT / "output").is_dir() else ROOT / "docs"])
                raise AssertionError("a real folder wasn't refused in test mode")
            except storage.ModelError:
                pass
            try:   # a folder where a file is expected, even inside the allowed place, is not removed as a file
                storage.remove_files([path("output_dir")], [t])
                raise AssertionError("a folder was accepted as a file")
            except storage.ModelError:
                pass
        finally:
            storage.fsutil.retry = old_retry
        assert not calls, "something was deleted although every call was refused"
    # structure: roll-offs run only after a successful record, and only the guarded delete removes anything
    import re
    calls = {}
    for f in (ROOT / "undercover").rglob("*.py"):
        text = f.read_text(encoding="utf-8")
        for name in ("roll_off(", "remove_files("):
            for m in re.finditer(r"(?<![\w.])(?:\w+\.)?" + re.escape(name), text):
                line = text[: m.start()].count("\n") + 1
                if not text.splitlines()[line - 1].lstrip().startswith(("def ", "#", '"""')):
                    calls.setdefault(name, []).append(f"{f.relative_to(ROOT / 'undercover').as_posix()}:{line}")
    assert sorted(c.split(":")[0] for c in calls["roll_off("]) == ["frontman/history.py", "speakeasy/speak.py"], calls
    # the guarded delete's callers: the two roll-offs, and the Doctor's leftovers (phase 10: *.part / *.tmp only, read by
    # MasterPlan), the themes' (phase 14: a library theme's folder, only inside [paths] themes_dir: delete, replace,
    # staging leftovers) and the yt-dlp self-heal's (phase 16: pair and staging folders, only inside <cache>/ytdlp);
    # the First assignment's (phase 17: only <voices>/.agent-smoke-seeding, an interrupted seed's leftover; the demo
    # refresh's: its own .agent-smoke-updating, the previous build it set aside, and on Undo the shipped copy); Vaulting's
    # pruning (two calls: old snapshots, then unreferenced objects, only inside the vault folder); away from home's
    # (phase 20, two: the fetch's leftovers and, on Forget, the node's state folder, both only inside the cache); Make
    # smaller's (phase 24, five: Keep and Undo, a voice's set-aside index and its readings, a project's set-aside folder,
    # only what Make smaller itself set aside or made, and three more for a new voice's kept full index: put back, and (improved.py) an improved voice's listening copies;
    # expired after its 30 days); a one-shot's candidates that weren't chosen (rc-fix7: only inside voices/.staging); a
    # new caller must be added here on purpose
    assert sorted(c.split(":")[0] for c in calls["remove_files("]) == ["app/firstrun.py", "core/doctor.py", "core/tailnet.py",
                                                                        "core/tailnet.py", "core/themes.py",
                                                                        "core/vault.py", "core/vault.py", "core/voices.py", "core/ytdlp.py", "doubleagent/improved.py",
                                                                        "frontman/history.py", "safehouse/library.py", *["safehouse/smaller.py"] * 10,
                                                                        "speakeasy/speak.py"], calls
    return "16th: kept oldest leaves the tray, 17th: next oldest deleted; 30 finishes, one at a time; guards hold"


@check("keep or roll off (speech): the same rule for SpeakEasy; a failed or empty run never rolls anything off")
def u_rolloff_speech():
    from undercover.core import tray
    from undercover.core.config import path
    from undercover.speakeasy import speak
    real_mp3, speak._mp3 = speak._mp3, lambda wav: None   # (ffmpeg is exercised by the real speech check)
    try:
        with isolated_data() as t:
            items = []
            for i in range(15):
                items.append(_fake_speech(i, speak))
            wavs = lambda: sorted(p.name for p in speak.speech_dir().iterdir() if p.suffix in (".flac", ".wav"))   # FLAC since phase 24
            assert len(wavs()) == 15 and len(speak.list_speech(everything=False)) == 15
            speak.set_kept(items[0]["id"], True)
            items.append(_fake_speech(15, speak))                 # the 16th: the kept oldest leaves the tray, nothing deleted
            assert len(wavs()) == 16 and len(speak.list_speech(everything=False)) == 15
            lib = {x["id"]: x for x in speak.list_speech()}
            assert lib[items[0]["id"]]["kept"] and not lib[items[0]["id"]]["recent"] and len(lib) == 16, "kept oldest left the library"
            assert not speak.list_speech(everything=False)[-1]["id"] == items[0]["id"]
            # a run that fails (nothing to read) or an empty one never rolls: the tray is over full by one, so a roll would show
            class J:
                id, cancel = "failjob", False
                def push(self, **k): pass
            try:
                speak.make_speech(J(), "", "zz")
                raise AssertionError("empty text should fail")
            except speak.SpeakError:
                pass
            assert len(wavs()) == 16, "a failed run deleted something"
            # cancelling or failing midway reaches no roll-off: roll_off() is called only from _save (checked in the covers check)
            items.append(_fake_speech(16, speak))                 # the 17th: the next oldest, unkept, is deleted with its sentences
            left = {x["id"] for x in speak.list_speech()}
            assert left == {i["id"] for i in items if i["id"] != items[1]["id"]}, "not exactly the oldest unkept speech left"
            assert len(wavs()) == 16 and not (speak.speech_dir() / f"{items[1]['id']}.json").exists()
            assert not speak.parts_dir("sjob01").exists() and speak.parts_dir("sjob02").exists(), "its sentences folder should go, and only its"
            log = (path("logs_dir") / "server.log").read_text(encoding="utf-8")
            assert log.count("roll-off (speech): removed") == 1 and items[1]["id"] in log, log
            for i in range(17, 25):                               # one at a time, the kept one stays
                _fake_speech(i, speak)
            assert items[0]["id"] in {x["id"] for x in speak.list_speech()} and len(wavs()) == 16
            speak.set_kept(items[0]["id"], False)                 # unkeeping what is out of the tray doesn't delete it
            assert items[0]["id"] in {x["id"] for x in speak.list_speech()}
    finally:
        speak._mp3 = real_mp3
    return "the same rule: kept oldest passed over on the 16th, next deleted on the 17th, failure deletes nothing"


@check("stable ids: deleting one leaves the others' ids alone; a new cover with the same name doesn't revive a deleted line")
def u_stable_ids():
    from undercover.core.config import path
    from undercover.frontman import history as H, pipeline
    with isolated_data():
        recs = [H.record(_fake_cover(i)) for i in range(6)]
        assert all(H.ID.fullmatch(r["id"]) for r in recs) and len({r["id"] for r in recs}) == 6
        before = [h["id"] for h in pipeline.history()]
        gone = recs[2]
        H.remove_line(gone["id"])
        Path(gone["output"]).unlink()
        after = [h["id"] for h in pipeline.history()]
        assert after == [i for i in before if i != gone["id"]], "ids of the others changed"
        assert len(H._lines()) == 5, "deleting must remove the line, not hide it"
        again = H.record({**_fake_cover(2), "title": "Cover 2 again"})   # the same output name as the deleted one
        assert again["id"] not in {r["id"] for r in recs}, "an id was reused"
        same = [h for h in pipeline.history() if Path(h["output"]).name == Path(gone["output"]).name]
        assert [h["id"] for h in same] == [again["id"]], "the deleted line came back to life"
        H.update(recs[0]["id"], title="Renamed")
        assert [h["title"] for h in pipeline.history() if h["id"] == recs[0]["id"]] == ["Renamed"]
        # a power cut can leave a half-written last line: the next cover must not be swallowed by it
        with open(path("output_dir") / "history.jsonl", "a", encoding="utf-8") as f:
            f.write('{"half": "writt')
        late = H.record(_fake_cover(9))
        assert late["id"] in [h["id"] for h in pipeline.history()], "a cover recorded after a half-written line was lost"
        # an older server may still append a line without an id: it gets one at the next read
        with open(path("output_dir") / "history.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(_fake_cover(10)) + "\n")
        assert all(H.ID.fullmatch(h["id"]) for h in pipeline.history()) and len(pipeline.history()) == 8
        # a roll-off that can only remove part of a cover (a locked leftover) still reports it and never hides the rest
        from undercover.core import storage
        a, b = _fake_cover(11), _fake_cover(12)
        real_retry = storage.fsutil.retry
        def locked(fn, *args, **kw):
            if "__v.flac" in str((fn.__defaults__ or [""])[0]):   # remove_files passes each path as the lambda's default
                raise PermissionError("locked")
            return real_retry(fn, *args, **kw)
        storage.fsutil.retry = locked
        try:
            storage.remove_files([Path(b["output"]), Path(b["converted_vocal"])], [path("output_dir"), path("cache_dir") / "converted"])
            raise AssertionError("a locked file should be reported")
        except PermissionError:
            assert not Path(b["output"]).exists() and Path(b["converted_vocal"]).exists(), "the other file should still have been removed"
        finally:
            storage.fsutil.retry = real_retry
    return "ids stable across a delete; no revival"


@check("stable ids: the history file migrates once (backup kept), then stays put")
def u_migrate_ids():
    from undercover.core.config import path
    from undercover.frontman import history as H
    with isolated_data():
        out = path("output_dir")
        out.mkdir(parents=True, exist_ok=True)
        lines = []
        for i in range(4):   # a sample of the old file: no ids, in file order; the third one's cover is gone
            rec = _fake_cover(i)
            if i == 2:
                Path(rec["output"]).unlink()
            lines.append(json.dumps(rec))
        f = out / "history.jsonl"
        f.write_text("\n".join(lines) + "\n" + '{"half": "writt', encoding="utf-8")   # and a half-written last line
        original = f.read_bytes()
        r = H.migrate()
        assert r == {"migrated": 3, "dropped": 1}, r
        assert (out / H.BACKUP).read_bytes() == original, "the backup isn't the old file"
        items = H._lines()
        assert [i["title"] for i in items] == ["Cover 0", "Cover 1", "Cover 3"], "order or content changed"
        assert all(H.ID.fullmatch(i["id"]) for i in items) and len({i["id"] for i in items}) == 3
        ids = [i["id"] for i in items]
        assert H.migrate() == {"migrated": 0, "dropped": 0} and [i["id"] for i in H._lines()] == ids, "a second run changed ids"
        assert (out / H.BACKUP).read_bytes() == original, "the backup was overwritten"
    return "3 lines got ids, 1 gone line dropped (kept in the backup), second run a no-op"


def _fake_voice(name: str, source: str = "", extra: dict | None = None, display: str = ""):
    """A voice folder with a meta.json (and an empty model.pth) in the isolated models/voices; no model is loaded."""
    from undercover.core.voices import Voice, voices_dir
    d = voices_dir() / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "model.pth").write_bytes(b"x")
    v = Voice(name=name, dir=d, pth=d / "model.pth", index=None, source=source, installed="2026-10-11 00:00",
              extra=extra or {}, display=display)
    (d / "meta.json").write_text(json.dumps(v.to_json(), indent=2), encoding="utf-8")
    return v


class _FakeHandler:
    """Just enough of the HTTP handler for the route functions: path, _body, _json, _error."""

    def __init__(self, path="", body=None):
        self.path, self._b, self.out, self.err = path, body or {}, None, None

    def _body(self):
        return self._b

    def _json(self, obj, status=200):
        self.out = obj

    def _error(self, msg, status=400):
        self.err = msg


def _speechlike(sr: int = 44100, secs: float = 8.0, seed: int = 3):
    """A synthetic stand-in for a clean clip of someone talking: voiced syllables (a gliding harmonic tone through two
    formants) with fricatives (bright noise bursts) and short pauses. Full band, no hiss, no room."""
    import numpy as np
    from scipy.signal import butter, sosfilt
    rng = np.random.default_rng(seed)
    n = int(sr * secs)
    y = np.zeros(n, np.float32)
    t0 = 0
    while t0 < n - sr // 4:
        dur = int(sr * rng.uniform(0.12, 0.3))
        t = np.arange(dur) / sr
        f0 = rng.uniform(95, 140) * (1 + 0.15 * np.sin(2 * np.pi * rng.uniform(1, 3) * t))
        ph = 2 * np.pi * np.cumsum(f0) / sr
        v = sum(np.sin(k * ph) / k for k in range(1, 60) if k * f0.max() < sr / 2.2)
        for lo, hi in ((rng.uniform(300, 800), 1200), (rng.uniform(1500, 2500), 3500)):
            v = v + 0.5 * sosfilt(butter(2, [lo, hi], "band", fs=sr, output="sos"), v)
        v *= np.hanning(dur)
        y[t0:t0 + dur] += v.astype(np.float32)
        t0 += dur
        if rng.random() < 0.5:   # a fricative
            d = min(int(sr * rng.uniform(0.05, 0.12)), n - t0)   # never past the end
            if d <= 0:
                break
            fr = sosfilt(butter(4, [4000, 12000], "band", fs=sr, output="sos"), rng.standard_normal(d)) * np.hanning(d)
            y[t0:t0 + d] += (0.3 * fr).astype(np.float32)
            t0 += d
        t0 += int(sr * (rng.uniform(0.25, 0.6) if rng.random() < 0.2 else rng.uniform(0.02, 0.06)))
    y += (rng.standard_normal(n) * 1e-5).astype(np.float32)
    return (0.8 * y / np.abs(y).max()).astype(np.float32), sr


@check("pace + pitch: the person per source, a pack's readings against them, registers, the nudge and the score as heard")
def u_pace_pitch():
    from undercover.doubleagent import cadence as K
    from undercover.speakeasy import speak
    # the person: two speeches, a debate; a source with too few sentences doesn't vote on the pace
    rows = ([{"src": "speech1", "syl": 40, "dur": 10.0, "f0_med": 180.0}] * 8 + [{"src": "speech2", "syl": 42, "dur": 10.0, "f0_med": 190.0}] * 8
            + [{"src": "debate", "syl": 46, "dur": 10.0, "f0_med": 140.0}] * 8 + [{"src": "clip", "syl": 90, "dur": 10.0, "f0_med": 300.0}] * 1
            + [{"src": "rally", "syl": 50, "dur": 10.0, "f0_med": 260.0}] * 4)
    prof = K.voice_profile(rows)
    assert prof["by_source"]["speech1"] == {"n": 8, "pace": 4.0, "f0_hz": 180.0} and prof["f0_hz"] == 180.0, prof
    assert K.pace_target(prof) == 4.4, K.pace_target(prof)   # the median of 4.0, 4.2, 4.6, 5.0; "clip" (n 1) left out
    assert len(prof["sents"]) == 29 and prof["sents"][0] == ["speech1", 40, 10.0, 180.0]
    # readings: fast and high (Obama's live pack in phase 12: x1.14, +4.5 st), slow, on pitch
    fast = K.pace_pitch(5.0, 233.0, prof)
    assert fast["pace_heard"] == "fast" and abs(fast["pitch_st"] - 4.47) < 0.01, fast
    assert K.pace_pitch(4.0, 180.0, prof)["pace_heard"] == "slow" and K.pace_pitch(4.5, 181.0, prof)["pace_heard"] == "ok"
    # pitch is a register (phase 12b): inside the range of their own sources (140 .. 190 Hz here) it's theirs, and only
    # past it does a pack get a nudge, back to the nearest edge (not to the median)
    assert K.pitch_range(prof) == (140.0, 190.0)   # "rally" (4 sentences) is too thin to set an edge of the range
    assert abs(fast["pitch_off_st"] - 3.53) < 0.01, fast
    m = {"score": 0.67, **fast}
    assert K.nudge_for(m) == -3.5 and K.nudge_for({"pitch_off_st": 0.4}) == 0.0
    assert K.heard(m) == round(0.67 - min(K.CAP_PITCH, K.W_PITCH * (3.53 - K.PITCH_DEAD)), 4) and K.heard(m, K.nudge_for(m)) == 0.67
    debate = K.pace_pitch(4.5, 145.0, prof)   # 3.7 st under the median, but where their debate voice is
    assert debate["pitch_st"] < -3 and debate["pitch_off_st"] == 0.0 and K.nudge_for(debate) == 0.0, debate
    assert K.heard({"score": 0.6, **debate}) == 0.6
    low = K.pace_pitch(4.5, 120.0, prof)
    assert abs(low["pitch_off_st"] + 2.67) < 0.01 and K.nudge_for(low) == 2.7, low
    m3 = dict(m)
    K.as_heard(m3, -3.5)
    assert m3["pitch_off_st"] == 0.03 and m3["pitch_raw_st"] == 4.47 and m3["pitch_st"] == 0.97, m3
    # the user's ear on Obama (phase 12b): his live pack at 235 Hz is "a fire speech", within his registers (137 .. 233 Hz)
    obama = {"f0_hz": 181.1, "by_source": {"debate": {"n": 18, "f0_hz": 137.0}, "rally": {"n": 24, "f0_hz": 232.9},
                                           "speech": {"n": 46, "f0_hz": 161.5}, "few": {"n": 2, "f0_hz": 300.0}}}
    a = K.pace_pitch(None, 234.8, obama)
    assert a["pitch_st"] > 4 and K.nudge_for(a) == 0.0 and K.pitch_pen(a["pitch_off_st"]) == 0.0, a
    # a pack id names its nudge (none: the ids verdicts already use)
    pk = {"engine": "chatterbox", "clip": "c1", "cfg_weight": 0.3, "tempo": 1.0}
    assert K.pack_id(pk) == "chatterbox:c1:cfg0.3:t1.0" and K.pack_id({**pk, "pitch_nudge": -4.4}) == "chatterbox:c1:cfg0.3:t1.0:p-4.4"
    assert K.approved_by_user({**pk, "pitch_nudge": -4.4, "verdicts": [{"compared": ["chatterbox:c1:cfg0.3:t1.0", "chatterbox:c1:cfg0.3:t1.0:p-4.4"],
                                                                        "winner": "chatterbox:c1:cfg0.3:t1.0:p-4.4"}]})
    # SpeakEasy's plan for a cadence voice carries the pack's nudge next to the user's own
    m2 = {"pitch_st": 4.47}
    K.as_heard(m2, -4.5)
    assert m2 == {"pitch_st": -0.03, "pitch_raw_st": 4.47}, m2
    with isolated_data():
        v = _fake_voice("zz-nudge-uc", "trained in UnderCover: Zed, 9.0 min, epoch 5", {"trained": True})
        _fake_pack(v.dir / "tts", "c1", 0.7)
        meta = json.loads((v.dir / "meta.json").read_text(encoding="utf-8"))
        meta["extra"]["tts"] = {**json.loads((v.dir / "tts" / "cadence.json").read_text(encoding="utf-8")), "pitch_nudge": -4.5}
        (v.dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        ready, speak.pack_ready = speak.pack_ready, lambda e, pin=None: True
        try:
            p = speak.plan("zz-nudge-uc", nudge=1.0)
        finally:
            speak.pack_ready = ready
        assert p["mode"] == "cadence" and p["pack_nudge"] == -4.5 and p["nudge"] == 1.0, p
    assert len(K.PACE_TEST) == 6 and K.PACE_TEST[0] == K.AB_LINE
    return (f"target {K.pace_target(prof)} syl/s, fast x{fast['pace_ratio']}; +{fast['pitch_st']} st from the median is "
            f"+{fast['pitch_off_st']} past their registers -> nudge {K.nudge_for(m)}; a debate pitch and Obama's 235 Hz: no nudge")


@check("characters: detected from the search, scenes ranked first and talk unticked, the scenes' voice is the target, the one-shot's gate")
def u_characters():
    import contextlib
    import numpy as np
    from undercover.doubleagent import project as P, service, sources as S
    from undercover.core.voices import get_voice
    mk = lambda i, t, ch="", d=600: S.Source(id=f"v{i:02d}", url="", title=t, channel=ch, duration=d)
    sponge = [mk(1, "SpongeBob Diaries Wants to Be SpongeTruths so Bad!", "Jackson Fanelli Podcast", 3000),
              mk(2, "SpongeBob Cast Reunites To Talk 25 Years of Krabby Patties", "Esquire", 900),
              mk(3, '"SpongeBob" Cast Takes a SpongeBob Trivia Quiz', "BuzzFeed Celeb", 700),
              mk(4, "The Voices Of SpongeBob And Patrick Take The Co-Star Test", "BuzzFeed Celeb", 600),
              mk(5, "Inspirational speech + song (Movie clip) The SpongeBob Movie", "TristeClips", 300),
              mk(6, "Spongebob - Out of Character #114", "Out of Character", 1500),
              mk(7, "SpongeBob Quotes for Every Occasion", "SpongeBob SquarePants Official", 600),
              mk(8, "Best of SpongeBob Season 1 Compilation", "Nickelodeon", 1800)]
    peter = [mk(i, t) for i, t in enumerate(["Family Guy - Peter Starts a Podcast with his Friends", "Family Guy - Peter Makes A Podcast",
                                             "Family Guy - Peter Griffin on TED Talk.", "Family Guy - Peter Griffin does public radio",
                                             "1999 Seth MacFarlane Interview before Family Guy premiered!", "Family Guy- Peter Learns how to be Black",
                                             "Peter Griffin government speech"])]
    obama = [mk(i, t) for i, t in enumerate(["Barack Obama full speech at the DNC", "President Barack Obama interview with Steve Kroft",
                                             "Obama in conversation with Bruce Springsteen", "President Barack Obama town hall",
                                             "Barack Obama 2008 victory speech", "Obama talks basketball podcast"])]
    assert S.looks_like_character("SpongeBob", sponge) and S.show_of("SpongeBob", sponge) is None
    assert S.looks_like_character("Peter Griffin", peter) and S.show_of("Peter Griffin", peter) == "family guy"
    assert not S.looks_like_character("Barack Obama", obama) and S.show_of("Barack Obama", obama) is None
    kind = {x.id: S.char_kind(x) for x in sponge}
    assert [i for i, k in kind.items() if k == "scene"] == ["v05", "v07", "v08"], kind
    assert S.char_kind(peter[0], "family guy") == "scene" and S.char_kind(peter[4], "family guy") == "talk"
    # search(): a speaking search that finds a character searches again for scenes; talk is unticked, scenes first
    class FakeYdl:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def extract_info(self, q, download=False):
            pool = sponge if any(w in q for w in ("interview", "speech", "podcast", "talks", "address")) else sponge[4:]
            return {"entries": [{"id": x.id, "title": x.title, "channel": x.channel, "duration": x.duration} for x in pool]}
    ydl, S._ydl = S._ydl, FakeYdl
    try:
        found = S.search("SpongeBob", "speaking")
    finally:
        S._ydl = ydl
    assert all((x.extra or {}).get("kind") in ("scene", "talk") for x in found)
    talk = [x for x in found if x.extra["kind"] == "talk"]
    assert talk and all(not x.selected and x.note == S.TALK_NOTE for x in talk), [(x.title, x.selected, x.note) for x in talk]
    assert all(x.extra["kind"] == "scene" for x in found[:3]) and any(x.selected for x in found[:3]), [x.title for x in found]
    assert not [x for x in found if x.id == "v08" and x.note.startswith("edited")], "a scene compilation was unticked"
    # the target speaker: the podcast host talks most overall, the character is who the scenes are
    with isolated_data():
        proj = P.Project.create("SpongeBob", "speaking", name="zz-char")
        proj.sources = [S.Source(id="pod", url="", title="SpongeBob Diaries podcast", duration=3000, extra={"kind": "talk"}),
                        S.Source(id="sc1", url="", title="SpongeBob scenes", duration=600, extra={"kind": "scene"}),
                        S.Source(id="sc2", url="", title="SpongeBob quotes", duration=600, extra={"kind": "scene"})]
        rng = np.random.default_rng(5)
        host, char, pat = (rng.standard_normal(64) for _ in range(3))
        emb, phrases = {}, []
        for src, voice, n in (("pod", host, 60), ("sc1", char, 12), ("sc2", char, 10), ("sc1", pat, 4)):
            for i in range(n):
                pid = f"{src}_{len(phrases):04d}"
                e = voice + 0.15 * rng.standard_normal(64)
                emb[pid] = (e / np.linalg.norm(e)).astype(np.float32)
                phrases.append(P.Phrase(id=pid, source=src, start=i * 5.0, dur=4.0, noise=0.1))
        proj.phrases = phrases
        proj.save_embeddings(emb)
        P.find_speakers(proj)
        P.score(proj)
        ref = proj.reference
        assert ref["clip"].startswith("sc") and ref.get("scene_minutes", 0) > 1, (ref, proj.speakers)
        kept = [p for p in proj.phrases if p.kept]
        assert kept and all(p.source != "pod" for p in kept), sum(p.source == "pod" for p in kept)
        proj.save()
        # the gate: the same project trained on the host would be "weak: talk about SpongeBob"; on the scenes it's fine
        v = _fake_voice("zz-char-uc", "trained in UnderCover: SpongeBob, 9.0 min, epoch 50",
                        {"trained": True, "project": "zz-char", "likeness": 0.8, "train_minutes": 9.0, "person": "SpongeBob"})
        q = service.voice_quality(get_voice(v.name))
        assert q["ok"] and q["scene_share"] == 1.0, q
        for p in proj.phrases:
            p.choice = p.source == "pod"
        proj.save()
        q = service.voice_quality(get_voice(v.name))
        assert not q["ok"] and q["scene_share"] == 0.0 and "talk about SpongeBob" in q["summary"] and q["action"] == {"kind": "improve", "target": v.name}, q
        w = _fake_voice("zz-weak-uc", "trained in UnderCover: Zed, 3.0 min, epoch 5", {"trained": True, "likeness": 0.5, "train_minutes": 3.0})
        q = service.voice_quality(get_voice(w.name))
        assert not q["ok"] and len(q["problems"]) == 2, q
        # Train a new voice ticked with a trained voice there: it's used, said plainly, Improve offered (not trained over)
        n = service.train_note(get_voice(w.name), {"ok": True, "problems": []})
        assert n["ok"] and "doesn't train over it" in n["kept"] and n["action"] == {"kind": "improve", "target": w.name}, n
        assert service.voice_quality(get_voice(_fake_voice("zz-dl", "https://x/y").name))["ok"]   # not trained here: not judged
    # the subject, in plain words, from what the search finds (subject.py), and the user's picks over it
    from undercover.doubleagent import subject as SJ
    sb = SJ.classify("SpongeBob", sponge, musician=False)
    assert sb["kind"] == "character" and "cartoon character" in sb["label"] and "not interviews" in sb["label"] and sb["voiced"], sb
    pg = SJ.classify("Peter Griffin", peter, musician=False)
    assert pg["kind"] == "character" and "Family Guy" in pg["label"], pg
    assert SJ.classify("Barack Obama", obama, musician=False)["kind"] == "person"
    assert SJ.classify("Adele", [mk(i, t) for i, t in enumerate(["Adele interview", "Adele live"])], musician=True)["kind"] == "singer"
    queen = [mk(i, t) for i, t in enumerate(["Queen - the band's best live moments", "Queen band members reunite",
                                             "Queen lead singer Freddie Mercury interview", "Queen reunion tour 2024", "Queen live at Wembley"])]
    assert SJ.classify("Queen", queen, musician=True)["kind"] == "group"
    evans = [mk(i, t) for i, t in enumerate(["Chris Evans actor interview Captain America", "Chris Evans the actor on his dog",
                                             "Chris Evans radio breakfast show", "Chris Evans radio presenter TFI Friday",
                                             "Chris Evans actor Knives Out"])]
    ce = SJ.classify("Chris Evans", evans, musician=False)
    assert ce["kind"] == "ambiguous" and ce["ask"]["question"] == "Which Chris Evans?" and {c["id"] for c in ce["ask"]["choices"]} >= {"role:actor", "role:radio"}, ce
    assert SJ.search_kind(SJ.apply_pick("Chris Evans", ce, "role:actor")) == ("person", "actor")
    assert SJ.search_kind(SJ.apply_pick("SpongeBob", sb, "person")) == ("person", "")
    assert SJ.search_kind(sb) == ("character", "") and SJ.search_kind(None) == (None, "")
    # pitfalls in the titles: AI / TTS voices and dubs are left out (the request's own language is kept)
    for t in ("AI SpongeBob sings Toxic", "SpongeBob TTS voice", "SpongeBob soundboard", "SpongeBob RVC model"):
        assert any(rx.search(t) and why == "may be an AI voice" for rx, why in S.SUSPECT), t
    assert S.other_language("Bob Esponja en español latino | escenas", "SpongeBob") == "spanish"
    assert S.other_language("Bob Esponja en español latino", "SpongeBob in Spanish") is None
    assert S.other_language("SpongeBob best moments", "SpongeBob") is None
    # a recast: two voices in the scenes, each in its own uploads, is said (and the one heard most is used)
    with isolated_data():
        proj = P.Project.create("Zed", "speaking", name="zz-recast")
        proj.sources = [S.Source(id=f"s{i}", url="", title=f"Zed scenes {i}", duration=600, extra={"kind": "scene"}) for i in range(4)]
        rng = np.random.default_rng(7)
        old, new = rng.standard_normal(64), rng.standard_normal(64)
        emb, phrases = {}, []
        for src, voice, n in (("s0", old, 14), ("s1", old, 12), ("s2", new, 10), ("s3", new, 9)):
            for i in range(n):
                pid = f"{src}_{len(phrases):04d}"
                e = voice + 0.15 * rng.standard_normal(64)
                emb[pid] = (e / np.linalg.norm(e)).astype(np.float32)
                phrases.append(P.Phrase(id=pid, source=src, start=i * 5.0, dur=4.0, noise=0.1))
        proj.phrases = phrases
        proj.save_embeddings(emb)
        P.find_speakers(proj)
        also = proj.reference.get("also")
        assert also and "Two different voices play Zed" in also["note"] and proj.reference["clip"][:2] in ("s0", "s1"), proj.reference
    return ("SpongeBob / Peter Griffin characters, Obama a person, Adele a singer, Queen a band, Chris Evans asks; search: talk "
            "unticked, scenes first; target = the scenes' voice; AI/TTS and dubs out; a recast said; gate")


@check("the ear: thumbs on covers and speech, one fix for a reason that has one, forget, liked voices first (phone allowed)")
def u_ear():
    import time
    from undercover.app import ear_api, phone_api
    from undercover.core import config, ear
    from undercover.core.config import path
    from undercover.core.voices import get_voice, voice_info
    from undercover.doubleagent import service
    from undercover.frontman import history
    from undercover.speakeasy import speak
    with isolated_data():
        assert ear.ear_file() == Path(config.CONFIG_FILE).with_name("ear.json")   # next to the (isolated) config
        z = _fake_voice("zed-uc", "trained", {"trained": True, "person": "Zed", "likeness": 0.7})
        z.installed = "2026-10-12 00:00"   # the newer one: first without thumbs
        (z.dir / "meta.json").write_text(json.dumps(z.to_json(), indent=2), encoding="utf-8")
        _fake_voice("zed-2-uc", "trained", {"trained": True, "person": "Zed", "likeness": 0.7})
        _fake_voice("zed", "https://x/y", {"person": "Zed"})
        out = path("output_dir") / "Song (zed-2-uc).mp3"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"x")
        cov = history.record({"output": str(out), "song": "", "voice": "zed-2-uc", "stems": {}, "title": "Song",
                              "options": {"voice": "zed-2-uc", "octave": 0, "key": 1, "quality": "high", "karaoke": True}})
        sid = "abcdef0123"
        (speak.speech_dir() / "s.wav").write_bytes(b"x")
        (speak.speech_dir() / f"{sid}.json").write_text(json.dumps({"id": sid, "file": "s.wav", "text": "hi", "voice": "zed-uc",
                                                                    "pace": "natural", "mode": "cadence", "reader": "am_michael"}), encoding="utf-8")

        def call(method, p, body=None):
            h = _FakeHandler(p, body)
            assert ear_api.handle(h, method, p)
            return h
        before = [v.name for v in service.installed_for("Zed")]
        # a thumbs up: stored with the cover's own voice and settings (not the body's), marked in the items
        h = call("POST", "/api/ear/rate", {"kind": "cover", "id": cov["id"], "thumb": "up", "reasons": ["like"], "voice": "zed"})
        assert h.err is None and h.out["fix"] is None and h.out["item"]["voice"] == "zed-2-uc", (h.err, h.out)
        assert h.out["item"]["settings"] == {"octave": 0, "key": 1, "quality": "high", "karaoke": True}, h.out["item"]
        g = call("GET", "/api/ear").out
        assert g["items"][f"cover:{cov['id']}"]["thumb"] == "up" and g["summary"]["up"] == 1 and g["reasons"]["cover"]["down"][0]["id"] == "unlike"
        # liked voices first: the voice APIs carry it, the one-shot's choice of a person's voices leans to it
        assert voice_info(get_voice("zed-2-uc"))["ear"] == {"up": 1, "down": 0} and voice_info(get_voice("zed-uc"))["ear"] is None
        after = [v.name for v in service.installed_for("Zed")]
        assert after[0] == "zed-2-uc" and before[0] == "zed-uc", (before, after)
        # ...and among equally good checked voices only (a clearly better score still wins)
        c = [{"name": "zed", "score": 0.60}, {"name": "zed-2-uc", "score": 0.58}, {"name": "zed-uc", "score": 0.50}]
        assert [x["name"] for x in ear.prefer(c, name=lambda x: x["name"], score=lambda x: x["score"])] == ["zed-2-uc", "zed", "zed-uc"]
        c[0]["score"] = 0.70
        assert ear.prefer(c, name=lambda x: x["name"], score=lambda x: x["score"])[0]["name"] == "zed"
        # a thumbs down replaces it; "doesn't sound like them" on a trained voice offers Improve tonight
        h = call("POST", "/api/ear/rate", {"kind": "cover", "id": cov["id"], "thumb": "down", "reasons": ["unlike", "key"], "note": " too   far "})
        f = h.out["fix"]
        assert f["reason"] == "unlike" and f["do"]["body"] == {"kind": "improve", "target": "zed-2-uc", "now": False} and f["alt"]["action"] == "pick_voice", f
        assert h.out["item"]["note"] == "too far" and ear.taste("zed-2-uc") == {"up": 0, "down": 1}
        # wrong key: make it again an octave lower (or higher); at the lowest only higher
        f = call("POST", "/api/ear/rate", {"kind": "cover", "id": cov["id"], "thumb": "down", "reasons": ["key"]}).out["fix"]
        assert f["do"]["action"] == "remake" and f["do"]["options"] == {"octave": -1} and f["alt"]["options"] == {"octave": 1}, f
        # ...which FrontMan makes again with the cover's own song, voice and settings (the backing moves with the lead)
        from undercover.frontman import api as fapi
        got, real = {}, fapi._start_cover
        fapi._start_cover = lambda h, body: got.update(body)
        try:
            h = _FakeHandler()
            fapi._cover_again(h, cov["id"], {"options": {"octave": -1, "voice": "x"}})   # (a voice in options is ignored)
        finally:
            fapi._start_cover = real
        assert got["voice"] == "zed-2-uc" and got["options"]["octave"] == -1 and got["options"]["key"] == 1 and "voice" not in got["options"], got
        # two octaves of it, previewed for a quick blind pick (phase 22): only octave / key change
        pv, realp = {}, fapi._start_preview
        fapi._start_preview = lambda h, body: pv.update(body)
        try:
            fapi._cover_octaves(_FakeHandler(), cov["id"], {"variants": [{"octave": 0, "key": 1}, {"octave": -1, "key": 0, "voice": "x"}]})
        finally:
            fapi._start_preview = realp
        assert [(v["octave"], v["key"]) for v in pv["variants"]] == [(0, 1), (-1, 0)] and pv["voice"] == "zed-2-uc" and "voice" not in pv["variants"][1], pv
        f = ear.fix_for("cover", ["key"], {"name": "zed-uc"}, {"octave": -2}, "x")
        assert f["do"]["options"] == {"octave": -1} and "alt" not in f and f["say"].startswith("Too low"), f
        # a downloaded voice: train a proper one; speech: pace has a fix; a reason without one: no fix
        f = ear.fix_for("cover", ["unlike"], {"name": "zed", "person": "Zed"}, {}, "x")
        assert f["do"]["body"] == {"kind": "new", "target": "Zed", "now": False}, f
        h = call("POST", "/api/ear/rate", {"kind": "speech", "id": sid, "thumb": "down", "reasons": ["pace"]})
        assert h.out["fix"]["do"]["options"] == {"pace": "slower"} and h.out["item"]["settings"] == {"pace": "natural", "mode": "cadence", "reader": "am_michael"}, h.out
        assert call("POST", "/api/ear/rate", {"kind": "speech", "id": sid, "thumb": "down", "reasons": ["pauses"]}).out["fix"] is None
        # speech read by a stock Reader (no cadence): learning their cadence is the fix, not more training
        f = ear.fix_for("speech", ["unlike"], {"name": "zed-uc", "trained": True}, {"mode": "reader"}, sid)
        assert f["do"]["body"] == {"kind": "cadence", "target": "zed-uc", "now": False}, f
        assert ear.fix_for("speech", ["unlike"], {"name": "zed-uc", "trained": True}, {"mode": "cadence"}, sid)["do"]["body"]["kind"] == "improve"
        # superseded thumbs are written away once they dominate the file
        for _ in range(260):
            ear.rate("speech", sid, "up", "zed-uc", {}, [], "")
        assert len(ear.ear_file().read_text(encoding="utf-8").splitlines()) < 260 and ear.thumb_of("speech", sid)["thumb"] == "up"
        assert ear.taste("zed-uc") == {"up": 1, "down": 0}, ear.taste("zed-uc")
        # refused: unknown reasons, a thumb that isn't up or down, a cover that's gone
        assert call("POST", "/api/ear/rate", {"kind": "cover", "id": cov["id"], "thumb": "down", "reasons": ["meh"]}).err
        assert call("POST", "/api/ear/rate", {"kind": "cover", "id": cov["id"], "thumb": "sideways"}).err
        assert call("POST", "/api/ear/rate", {"kind": "cover", "id": "0000000000", "thumb": "up"}).err
        # a half-written last line (a power cut) is skipped; forget one, then all
        with open(ear.ear_file(), "a", encoding="utf-8") as fh:
            fh.write('{"item": "cover:x", "thu')
        time.sleep(0.01)
        assert set(ear.items()) == {f"cover:{cov['id']}", f"speech:{sid}"}, ear.items()
        assert call("POST", "/api/ear/forget", {"kind": "speech", "id": sid}).out == {"forgot": 1}
        assert set(ear.items()) == {f"cover:{cov['id']}"}
        assert call("POST", "/api/ear/forget", {}).err and call("POST", "/api/ear/forget", {"all": True}).out == {"forgot": 1}
        assert ear.items() == {} and not ear.ear_file().exists()
        for m, r in (("GET", "/api/ear"), ("POST", "/api/ear/rate"), ("POST", "/api/ear/forget")):
            assert phone_api.policy(m, r)[0] == "phone", (m, r, phone_api.policy(m, r))
    return "up / down with reasons, the cover's own voice and settings; fixes: Improve tonight, a new voice, an octave, slower; forget one / all; liked first"


@check("golden set bookkeeping: noise bands, flags past twice the band, at most 4 pairs, only verdicts move the baseline")
def u_golden():
    import os
    import tempfile
    # the golden set lives in the dev repo since the repo went public (2026-10-24): it reads the maker's own voices
    dev = Path(os.environ.get("UNDERCOVER_DEV_ROOT") or r"D:\UnderCover-work\dev") / "tools" / "research"
    if not (dev / "golden.py").is_file():
        return "skipped: no dev repo with tools/research/golden.py here"
    sys.path.insert(0, str(dev))
    import golden as G
    # words kept: the share of the lead's words that come through in order; extra words (moved choruses) aren't errors
    assert G._kept("i'm loving it".split(), "so i'm really loving it baby".split()) == 1.0 and G._kept("a b c d".split(), "a x c".split()) == 0.5
    assert G.parts("cover:toxic__spongebob-uc") == {"toxic", "spongebob-uc"} and G.parts("speech:rain__morgan-freeman-uc:reader") == {"rain", "morgan-freeman-uc"}
    a = {"cover:toxic__sb": {"likeness": 0.70, "words_kept": 0.80, "cents_off": 20.0, "loud_db": -14.0, "octave": 0}}
    b = {"cover:toxic__sb": {"likeness": 0.75, "words_kept": 0.81, "cents_off": 21.0, "loud_db": -14.1, "octave": 0}}
    bd = G.bands(a, b)["cover:toxic__sb"]
    assert bd == {"likeness": 0.05, "words_kept": 0.05, "cents_off": 5.0, "loud_db": 0.5}, bd   # the spread, never under the floor
    c = {"cover:toxic__sb": {"likeness": 0.68, "words_kept": 0.8, "cents_off": 20.0, "loud_db": -14.0, "octave": 0}}
    assert G.bands(a, b, c)["cover:toxic__sb"]["likeness"] == 0.07   # three runs: the largest spread
    base = {"run": "r0", "scores": a, "bands": {"cover:toxic__sb": bd}}
    same = {"cover:toxic__sb": {"likeness": 0.62, "words_kept": 0.95, "cents_off": 29.0, "loud_db": -14.9}}
    assert G.flags(base, same) == [], G.flags(base, same)   # within twice the bands, or better
    worse = {"cover:toxic__sb": {"likeness": 0.55, "words_kept": 0.85, "cents_off": 33.0, "loud_db": -12.5}, "cover:new__x": {"likeness": 0.1}}
    fl = G.flags(base, worse)
    assert {f["score"] for f in fl[:2]} == {"loud_db", "likeness"} and fl[2]["score"] == "cents_off" and len(fl) == 3, fl   # worst first; a new item isn't judged
    many = [{"item": f"cover:s{i}__v"} for i in range(6)]
    assert G.pick_pairs(many, [f"cover:s{i}__v" for i in range(9)], 3) == [f"cover:s{i}__v" for i in range(4)]
    items = ["cover:a__v", "cover:b__v", "cover:c__v"]
    assert G.pick_pairs([], items, 0) == ["cover:a__v"] and G.pick_pairs([], items, 1) == ["cover:b__v"]   # one rotating item
    assert G.pick_pairs([{"item": "cover:b__v"}], items, 0) == ["cover:b__v", "cover:a__v"]
    # only verdicts move the baseline; "old" blocks, a flag not heard yet holds
    nb = G.accepted(base, "r1", {"cover:toxic__sb": {"likeness": 0.9}, "cover:new__x": {"likeness": 0.3}}, {})
    assert nb["scores"]["cover:toxic__sb"] == a["cover:toxic__sb"] and nb["scores"]["cover:new__x"] == {"likeness": 0.3}
    assert G.render_run(nb, "cover:new__x") == "r1" and G.render_run(nb, "cover:toxic__sb") == "r0"
    nb = G.accepted(base, "r1", {"cover:toxic__sb": {"likeness": 0.9}}, {"cover:toxic__sb": "same"})
    assert nb["scores"]["cover:toxic__sb"] == {"likeness": 0.9} and G.render_run(nb, "cover:toxic__sb") == "r1"
    assert G.blocking({"cover:a__v": "old", "cover:b__v": "new"}, ["cover:b__v", "cover:c__v"]) == ["cover:a__v", "cover:c__v"]
    # make: items whose song and voice weren't (re)frozen keep their baseline; a refrozen voice's items start again
    old = {"run": "r0", "scores": {"cover:a__v1": {"likeness": 0.7}, "cover:a__v2": {"likeness": 0.6}},
           "bands": {"cover:a__v1": {"likeness": 0.02}, "cover:a__v2": {"likeness": 0.02}}}
    m = G.merged_baseline(old, ("r5", {"cover:a__v1": {"likeness": 0.1}, "cover:a__v2": {"likeness": 0.2}, "cover:b__v1": {"likeness": 0.3}}),
                          [{"cover:a__v1": {"likeness": 0.1}, "cover:a__v2": {"likeness": 0.25}, "cover:b__v1": {"likeness": 0.3}},
                           {"cover:a__v1": {"likeness": 0.1}, "cover:a__v2": {"likeness": 0.21}, "cover:b__v1": {"likeness": 0.3}}], {"v2", "b"})
    assert m["scores"]["cover:a__v1"] == {"likeness": 0.7} and m["scores"]["cover:a__v2"] == {"likeness": 0.2}, m
    assert m["bands"]["cover:a__v2"] == {"likeness": 0.05} and G.render_run(m, "cover:b__v1") == "r5" and G.render_run(m, "cover:a__v1") == "r0"
    old["retired"] = {"s": {"scores": {"cover:s__v1": {"likeness": 0.5}}}}   # a retired song's scores outlive a make
    assert G.merged_baseline(old, ("r6", {"cover:a__v1": {"likeness": 0.1}}), [], set())["retired"] == old["retired"]
    spec = {"voices": [{"name": "v1"}, {"name": "morgan-freeman-uc"}], "covers": {"a": ["v1", "gone"]}}
    its = G.items_of(spec)
    assert its[0] == "cover:a__v1" and "cover:a__gone" not in its and "speech:rain__morgan-freeman-uc:reader" in its, its
    # its own config: every written path inside the golden folder; models read from the app folder
    saved_env, saved_g = os.environ.get("UNDERCOVER_CONFIG"), G.GOLDEN
    with tempfile.TemporaryDirectory() as td:
        G.GOLDEN = Path(td) / "golden"
        try:
            G._config()
            text = (G.GOLDEN / "config.toml").read_text(encoding="utf-8")
        finally:
            G.GOLDEN = saved_g
            if saved_env is None:
                os.environ.pop("UNDERCOVER_CONFIG", None)
            else:
                os.environ["UNDERCOVER_CONFIG"] = saved_env
    import tomllib
    paths = tomllib.loads(text)["paths"]
    inside = Path(td).as_posix() + "/golden"
    assert all(paths[k].startswith(inside) for k in ("voices_dir", "cache_dir", "output_dir", "logs_dir", "uploads_dir")), paths
    assert paths["models_dir"].endswith("/models") and not paths["models_dir"].startswith(inside), paths
    return "bands with floors; flags worst first; 4 pairs + 1 rotating; verdicts move the baseline, old blocks; make keeps untouched items; its own folders"


@check("lead gate: an instrument in the lead stem isn't converted, lines left in the backing are; the stems stay as they were")
def u_leadgate():
    import numpy as np
    import soundfile as sf
    from undercover.frontman import leadgate as G
    from scipy.signal import resample_poly
    voice = resample_poly(_speechlike(sr=32000, secs=8.0, seed=11)[0], 1, 2).astype(np.float32)[:16000 * 8]
    sr = 16000
    t = np.arange(int(sr * 8)) / sr
    tone = (0.5 * np.sin(2 * np.pi * 440 * t) + 0.2 * np.sin(2 * np.pi * 880 * t)).astype(np.float32)   # a string hook
    z = np.zeros(int(sr * 8), np.float32)
    # 0-8 s the lead sings; 8-16 s the "lead" is the hook; 16-24 s the lead is silent and the backing sings the chorus
    lead = np.concatenate([voice, tone, z]) + (np.random.default_rng(1).standard_normal(sr * 24) * 1e-5).astype(np.float32)
    back = np.concatenate([z, z, voice])
    g = G.plan(lead, back)
    inst = sum(b - a for a, b in g["instrument"])
    ch = sum(b - a for a, b in g["chorus"])
    assert g["instrument"] and all(7.5 <= a and b <= 16.5 for a, b in g["instrument"]) and inst >= 6, g
    assert g["chorus"] and all(15.5 <= a for a, b in g["chorus"]) and ch >= 3, g   # (the synthetic voice pauses more than singing)
    assert not [r for r in g["instrument"] if r[0] < 7.5], "singing was taken for an instrument"
    with isolated_data() as tmp:
        stems = {}
        for k, x in (("vocals", lead), ("backing", back), ("instrumental", np.zeros_like(lead))):
            stems[k] = tmp / f"{k}.wav"
            sf.write(str(stems[k]), x, sr)
        before = {k: Path(v).read_bytes() for k, v in stems.items()}
        out = G.apply(stems, g, tmp, "t")
        assert all(Path(v).read_bytes() == before[k] for k, v in stems.items()), "the original stems were changed"
        l2, _ = sf.read(str(out["vocals"]), dtype="float32")
        i2, _ = sf.read(str(out["instrumental"]), dtype="float32")
        b2, _ = sf.read(str(out["backing"]), dtype="float32")
        rms = lambda x, a, b: float(np.sqrt(np.mean(x[int(a * sr):int(b * sr)] ** 2)))
        assert rms(l2, 9, 15) < 0.01 * rms(lead, 9, 15), "the hook is still in the lead to convert"
        assert rms(i2, 9, 15) > 0.9 * rms(lead, 9, 15), "the hook didn't go back into the instrumental"
        c0, c1 = g["chorus"][0][0] + 0.1, g["chorus"][-1][1] - 0.1
        assert rms(l2, c0, c1) > 0.9 * rms(back, c0, c1) and rms(b2, c0, c1) < 0.01 * rms(back, c0, c1), "the chorus wasn't moved"
        assert abs(rms(l2, 1, 7) - rms(lead, 1, 7)) < 1e-3, "the singing changed"
    assert G.note(g) and G.plan(voice)["instrument"] == [], G.plan(voice)
    # a duet (phase 22): two voices in harmony all the way are "backing" to the karaoke model and its lead is silence;
    # then there's no lead to split off and the whole vocal is converted as one voice
    import tempfile
    from undercover.core.separate import lead_missing
    with tempfile.TemporaryDirectory() as td:
        silent, lead_f, back_f, quiet = (Path(td) / n for n in ("silent.wav", "lead.wav", "back.wav", "quiet.wav"))
        sf.write(str(silent), np.zeros(16000 * 4, np.float32), 16000)
        sf.write(str(lead_f), voice[:16000 * 4], 16000)
        sf.write(str(back_f), 0.3 * voice[16000:16000 * 5], 16000)
        sf.write(str(quiet), 0.1 * voice[:16000 * 4], 16000)
        assert lead_missing(silent, back_f) and not lead_missing(lead_f, back_f) and not lead_missing(quiet, back_f)
    return f"hook {inst:.1f} s left as it is, chorus {ch:.1f} s converted, singing untouched, stems unchanged"


@check("likeness word: every voice API says fair / good / great (poor: checked unlike the person), null when unmeasured")
def u_likeness_word():
    from undercover.core.config import path
    from undercover.core.voices import get_voice, likeness_word, voice_info
    with isolated_data():
        for name, lk in (("zz-t1", 0.6), ("zz-t2", 0.7), ("zz-t3", 0.8)):
            _fake_voice(name, "trained", {"trained": True, "likeness": lk})
        _fake_voice("zz-t0", "trained", {"trained": True})
        for name in ("zz-c1", "zz-c2", "zz-c3", "zz-c4", "zz-c5"):
            _fake_voice(name, "https://x/y", {"person": "Zed"} if name == "zz-c5" else {})
        (path("cache_dir") / "voice_quality.json").write_text(json.dumps({
            "zed|a": {"voice": "zz-c1", "score": 0.6}, "zed|b": {"voice": "zz-c2", "score": 0.75},
            "zed|c": {"voice": "zz-c3", "score": 0.85}, "zed|d": {"voice": "zz-c4", "score": -0.02, "verdict": "bad"},
            "other|e": {"voice": "zz-c5", "score": 0.9}, "zed|f": {"voice": "zz-c5", "score": 0.6}}), encoding="utf-8")
        got = {n: likeness_word(get_voice(n)) for n in ("zz-t0", "zz-t1", "zz-t2", "zz-t3", "zz-c1", "zz-c2", "zz-c3", "zz-c4", "zz-c5")}
        assert got == {"zz-t0": None, "zz-t1": "fair", "zz-t2": "good", "zz-t3": "great", "zz-c1": "fair", "zz-c2": "good",
                       "zz-c3": "great", "zz-c4": "poor", "zz-c5": "fair"}, got   # c5: its own person's check, not another's
        _fake_voice("zz-c6", "https://x/y")
        assert voice_info(get_voice("zz-c6"))["likeness_word"] is None
        _fake_voice("zz-c7", "https://x/y", {"person": "Plankton", "original_name": "zz-c4"})   # checked only for Zed
        assert likeness_word(get_voice("zz-c7")) is None, "another person's check was used"
        from undercover.app.server import voices_json
        from undercover.doubleagent import night
        from undercover.safehouse import library
        from undercover.speakeasy import speak
        for api, rows, key in (("/api/voices", voices_json(), "name"), ("/api/work/candidates", night.candidates(), "voice"),
                               ("/api/library/voices", library.voices(), "name"), ("/api/speak/voices", speak.voices(), "name")):
            w = {r[key]: r.get("likeness_word", "missing") for r in rows}
            assert w.get("zz-t2") == "good" and w.get("zz-c4") == "poor" and w.get("zz-c6", None) is None, (api, w)
    return "trained 0.50 fair / 0.70 good / 0.80 great; checked 0.35 fair / 0.50 good / 0.60 great / -0.02 poor; on all four voice APIs"


@check("yield: long videos in spread stretches, the hiss rule per voice (real hiss still out), music under speech when the clip came out clean, the names check of a character's voice")
def u_yield():
    import numpy as np
    from undercover.doubleagent import project as P, service, sources as S
    # stretches: a 4 h compilation gives six 2 min stretches from its first minute to its last; short ones as before
    st = S.stretches(4 * 3600, 720, 120)
    assert len(st) == 6 and st[0][0] == 60 and abs(st[-1][0] + st[-1][1] - (4 * 3600 - 60)) < 1, st
    assert S.stretches(600, 720, 120) == [] and S.stretches(780, 720, 120) == [(0.0, 720)] and S.stretches(3600, 720, 0) == [(60.0, 720)]
    args = S.trim_args(4 * 3600)
    assert args[0] == "-filter_complex" and "concat=n=6" in args[1] and f"apad=pad_dur={S.STRETCH_GAP}" in args[1], args
    assert S.trim_args(4 * 3600, spread=False) == ["-ss", "60.0", "-t", "720"], S.trim_args(4 * 3600, spread=False)   # a person's long video
    # the noise floor: a voice with gaps between its words is clean there; white noise at 20 dB SNR fills them
    rng = np.random.default_rng(19)
    x, sr = _speechlike(sr=32000, secs=4.0, seed=19)
    hissy = x + rng.standard_normal(len(x)).astype(np.float32) * np.sqrt(float(np.mean(x ** 2)) / 100)
    assert P.noise_floor(x, sr) < P.HISS_FLOOR_DB < P.noise_floor(hissy, sr), (P.noise_floor(x, sr), P.noise_floor(hissy, sr))
    with isolated_data():
        # a raspy voice (its own clean-gap phrases at 0.33) gets a higher limit (capped at 0.40); hiss over 0.30 with filled gaps stays out
        proj = P.Project.create("Rasp", "speaking", name="zz-rasp")
        proj.sources = [S.Source(id="a", url="", title="a", duration=600)]
        emb, phrases = {}, []
        v = rng.standard_normal(64)
        v /= np.linalg.norm(v)
        for i, (nz, fl) in enumerate([(0.33, -45.0)] * 30 + [(0.36, -44.0), (0.36, -28.0), (0.45, -50.0)]):
            e = v + 0.1 * rng.standard_normal(64)
            emb[f"a_{i:04d}"] = (e / np.linalg.norm(e)).astype(np.float32)
            phrases.append(P.Phrase(id=f"a_{i:04d}", source="a", start=i * 5.0, dur=4.0, noise=nz, floor=fl))
        proj.phrases = phrases
        proj.save_embeddings(emb)
        P.find_speakers(proj)
        P.score(proj)
        why = [p.reason for p in proj.phrases[-3:]]
        assert proj.reference["hiss_limit"] == 0.40 and proj.phrases[0].kept and why == ["", "hiss or artifacts", "hiss or artifacts"], (proj.reference, why)
        # a person's voice (0.20) keeps the fixed limit
        for p in proj.phrases[:30]:
            p.noise = 0.2
        P.score(proj)
        assert proj.reference["hiss_limit"] == 0.30 and proj.phrases[-3].reason == "hiss or artifacts", proj.reference
        # music under speech: off by default (until the ear rules); on, kept up to -3 dB when the clip came out clean
        for p, (m, fl) in zip(proj.phrases[:4], [(-6.0, -45.0), (-6.0, -25.0), (-1.0, -45.0), (-15.0, -25.0)]):
            p.music_db, p.floor = m, fl
        P.score(proj)
        assert [p.reason for p in proj.phrases[:4]] == ["music or noise", "music or noise", "music or noise", ""]
        P.TCFG["music_db_clean"] = -3.0
        try:
            P.score(proj)
        finally:
            P.TCFG.pop("music_db_clean")
        why = [p.reason for p in proj.phrases[:4]]
        assert why == ["", "music or noise", "music or noise", ""], why
        # the names check: the voice heard most keeps saying "SpongeBob", the other never does -> it's switched, and said
        proj = P.Project.create("SpongeBob", "speaking", name="zz-names")
        proj.sources = [S.Source(id=f"s{k}", url="", title=f"SpongeBob scenes {k}", duration=600, extra={"kind": "scene"}) for k in range(2)]
        a, b = rng.standard_normal(64), rng.standard_normal(64)
        emb, phrases = {}, []
        for who, voice, n in (("pat", a, 30), ("sb", b, 20)):
            for i in range(n):
                pid = f"s{i % 2}_{who}{i:03d}"
                e = voice + 0.1 * rng.standard_normal(64)
                emb[pid] = (e / np.linalg.norm(e)).astype(np.float32)
                phrases.append(P.Phrase(id=pid, source=f"s{i % 2}", start=i * 5.0, dur=4.0, noise=0.2, floor=-50.0))
        proj.phrases = phrases
        proj.save_embeddings(emb)
        P.find_speakers(proj)
        P.score(proj)
        proj.save()
        with P.lock(proj.name):   # the automatic pick, confirmed as the app does
            P.set_reference(proj, cluster=proj.reference["cluster"], auto=True)
            proj.save()
        picked = proj.reference["clip"]
        assert "pat" in picked, proj.reference   # the voice heard most: the other character
        for p in proj.phrases:
            proj.clip_path(p.id).parent.mkdir(parents=True, exist_ok=True)
            proj.clip_path(p.id).write_bytes(b"")
        import undercover.core.cadence as C
        real, notes = C.transcribe, []
        C.transcribe = lambda files, **k: ["Hey SpongeBob, wait up!" if "pat" in str(f) else "I'm ready, I'm ready!" for f in files]
        try:
            proj = service.check_names(proj, "SpongeBob", lambda *a: notes.append(a[-1]))
        finally:
            C.transcribe = real
        assert "sb" in proj.reference["clip"] and proj.reference.get("names_note") and any("never says it" in n for n in notes), (proj.reference, notes)
        assert proj.phrases and all("sb" in p.id for p in proj.phrases if p.kept), [p.id for p in proj.phrases if p.kept][:5]
    return (f"6 stretches over 4 h; floor {P.noise_floor(x, sr):.0f} dB clean, {P.noise_floor(hissy, sr):.0f} dB with hiss; "
            f"a raspy voice's limit 0.40, a person's 0.30; the voice saying \"SpongeBob\" swapped for the one that doesn't")


@check("clip sound: degraded copies (lowpassed, noise, clipped, reverb) rank below the original")
def u_clipsound():
    import numpy as np
    from scipy.signal import butter, fftconvolve, sosfiltfilt
    from undercover.doubleagent import clipsound as CS
    x, sr = _speechlike()
    base = CS.penalty(CS.measure(x, sr))
    assert base < 0.01, (base, CS.measure(x, sr))
    rng = np.random.default_rng(1)
    t = np.arange(int(sr * 0.9)) / sr
    ir = rng.standard_normal(len(t)) * np.exp(-6.91 * t / 0.9)
    ir[0] = 1.0
    pk = float(np.abs(x).max())
    degraded = {"lowpass 3.5k": sosfiltfilt(butter(8, 3500, "low", fs=sr, output="sos"), x),
                "noise 15 dB": x + rng.standard_normal(len(x)) * np.sqrt(np.mean(x ** 2) / 10 ** 1.5),
                "clipped x8": np.clip(x * 8, -pk, pk),
                "reverb 0.9 s": fftconvolve(x, ir)[:len(x)]}
    pens = {k: CS.penalty(CS.measure(np.asarray(y, np.float32), sr)) for k, y in degraded.items()}
    low = {k: p for k, p in pens.items() if p <= base + 0.02}
    assert not low, f"not heard: {low} (original {base})"
    # (the clips the user judged by ear live in the real folders: tools/research/clipsound.py checks those, not smoke)
    return f"original {base}, degraded " + ", ".join(f"{k} {p}" for k, p in pens.items())


def _fake_pack(d, clip: str, score: float, before: float | None = None, verdicts=None) -> dict:
    """A cadence pack on disk (ref.wav + cadence.json) and its extra.tts dict."""
    import numpy as np
    import soundfile as sf
    d.mkdir(parents=True, exist_ok=True)
    sf.write(str(d / "ref.wav"), (np.random.default_rng(len(clip)).standard_normal(16000) * 0.1).astype(np.float32), 16000)
    pack = {"engine": "chatterbox", "cfg_weight": 0.3, "exaggeration": 0.5, "v": 1, "ref": "ref.wav", "ref_text": "Hello.",
            "clip": clip, "tempo": 1.0, "scores": {"likeness": 0.8, "score": score},
            "before": before is not None and {"likeness": 0.8, "score": before} or None, "made": "2026-10-12 10:00",
            "verdicts": list(verdicts or [])}
    (d / "cadence.json").write_text(json.dumps({**pack, "tried": []}), encoding="utf-8")
    return pack


@check("keep this one: a new pack waits in tts/pending/, the A/B state, verdicts swap or set aside, nothing deleted")
def u_keep_this_one():
    from undercover.core import storage
    from undercover.core.voices import get_voice
    from undercover.doubleagent import api as daapi, cadence as K
    with isolated_data() as tmp:
        v = _fake_voice("zz-ab-uc", "trained in UnderCover: Zed, 9.0 min, epoch 5", {"trained": True})
        work = tmp / "work"
        tts = v.dir / "tts"
        # 1. first pack: straight in (nothing to compare with); a second one waits; a third replaces the unheard second
        _fake_pack(work / "a", "clipA", 0.60, verdicts=[{"compared": ["kokoro_rvc", "chatterbox:clipA:cfg0.3:t1.0"],
                                                         "winner": "chatterbox:clipA:cfg0.3:t1.0"}])
        pa = json.loads((work / "a" / "cadence.json").read_text(encoding="utf-8"))
        assert K.save_pack(v.name, work / "a", pa, waits=False) == "live"
        v = get_voice(v.name)
        assert K.pack_of(v)["clip"] == "clipA" and K.pending_of(v) is None
        assert not K.pack_of(v)["verdicts"], "a new live pack starts from the voice's verdicts (none yet)"
        K.record_verdict(v.name, ["kokoro_rvc", K.pack_id(K.pack_of(v))], K.pack_id(K.pack_of(v)), "approved by ear")
        assert K.approved_by_user(K.pack_of(get_voice(v.name)))
        for clip, sc in (("clipB", 0.65), ("clipC", 0.66)):
            pk = _fake_pack(work / clip, clip, sc, before=0.62)
            assert K.save_pack(v.name, work / clip, pk, waits=True) == "pending"
        v = get_voice(v.name)
        assert K.pack_of(v)["clip"] == "clipA" and K.pending_of(v)["clip"] == "clipC", (K.pack_of(v), K.pending_of(v))
        unheard = list((tts / "older").glob("*-unheard"))
        assert len(unheard) == 1 and json.loads((unheard[0] / "cadence.json").read_text(encoding="utf-8"))["clip"] == "clipB"
        st = K.ab_state(v.name)
        assert st["current"]["approved"] and st["pending"]["clip"] == "clipC" and not st["ready"], st
        # 2. the readings (made by the GPU job; written by hand here) make it ready
        for d, pid in ((tts, st["current"]["id"]), (tts / "pending", st["pending"]["id"])):
            (d / "sample.wav").write_bytes((d / "ref.wav").read_bytes())
            (d / "sample.json").write_text(json.dumps({"line": K.AB_LINE, "pack": pid}), encoding="utf-8")
        assert K.ab_state(v.name)["ready"] and K.sample_file(v.name, "pending").parent.name == "pending"
        h = _FakeHandler()
        assert daapi.handle(h, "GET", f"/api/train/voices/{v.name}/cadence") and h.out["ready"], (h.out, h.err)
        h = _FakeHandler(body={"keep": "maybe"})
        daapi.handle(h, "POST", f"/api/train/voices/{v.name}/cadence/verdict")
        assert h.err and K.pending_of(get_voice(v.name)), "a verdict that isn't current/pending was taken"
        # 3. keep the current one: the new one is set aside (not deleted), the verdict lands on the current pack
        h = _FakeHandler(body={"keep": "current", "why": "the new one is muffled"})
        daapi.handle(h, "POST", f"/api/train/voices/{v.name}/cadence/verdict")
        assert h.out and h.out["kept"] == "current", (h.out, h.err)
        v = get_voice(v.name)
        cur = K.pack_of(v)
        vd = cur["verdicts"][-1]
        assert cur["clip"] == "clipA" and K.pending_of(v) is None and K.approved_by_user(cur), cur
        assert vd["winner"] == vd["compared"][0] and vd["why"] == "the new one is muffled" and vd["kept"] == "current"
        assert set(vd["scores"]) == set(vd["compared"]) and vd["scores"][vd["compared"][1]]["score"] == 0.66, vd
        assert len(list((tts / "older").glob("*-turned-down"))) == 1
        h = _FakeHandler(body={"keep": "pending"})
        daapi.handle(h, "POST", f"/api/train/voices/{v.name}/cadence/verdict")
        assert h.err, "a verdict with nothing waiting was taken"
        # 4. a new one, kept: it goes live, the old live pack goes to previous/ with its verdicts; a third swap moves
        # that previous to older/ first
        for clip in ("clipD", "clipE"):
            pk = _fake_pack(work / clip, clip, 0.7, before=0.6)
            assert K.save_pack(v.name, work / clip, pk, waits=True) == "pending"
            (tts / "sample.wav").write_bytes(b"RIFF")   # the live pack's reading moves with it
            r = K.verdict(v.name, "pending", "warmer")
            v = get_voice(v.name)
            assert r["kept"] == "pending" and K.pack_of(v)["clip"] == clip and K.pending_of(v) is None, r
            assert K.approved_by_user(K.pack_of(v)) and K.pack_of(v)["verdicts"][-1]["why"] == "warmer"
            prev = json.loads((tts / "previous" / "pack.json").read_text(encoding="utf-8"))
            assert prev["clip"] == {"clipD": "clipA", "clipE": "clipD"}[clip] and (tts / "previous" / "ref.wav").exists()
            assert (tts / "previous" / "sample.wav").exists() and not (tts / "pending").exists()
        assert len(list((tts / "older").glob("*-previous"))) == 1
        refs = sorted(json.loads((f.parent / "cadence.json").read_text(encoding="utf-8"))["clip"] for f in tts.rglob("ref.wav"))
        assert refs == ["clipA", "clipB", "clipC", "clipD", "clipE"], f"a pack went missing: {refs}"
        # 5. the same clip and settings again, made later: told apart by date, and the kept one counts as approved
        pk = _fake_pack(work / "clipE2", "clipE", 0.71, before=0.7)
        pk["made"] = "2026-10-12 11:00"
        (work / "clipE2" / "cadence.json").write_text(json.dumps(pk), encoding="utf-8")
        assert K.save_pack(v.name, work / "clipE2", pk, waits=True) == "pending"
        K.verdict(v.name, "pending")
        v = get_voice(v.name)
        assert K.pack_of(v)["made"] == "2026-10-12 11:00" and K.approved_by_user(K.pack_of(v)), K.pack_of(v)["verdicts"][-1]
        # 6. a voice whose cadence was deleted while a new one was made: the new one goes live, not to pending
        w = _fake_voice("zz-ab2-uc", "trained in UnderCover: Zed, 9.0 min, epoch 5", {"trained": True})
        pk = _fake_pack(work / "w1", "clipW", 0.6)
        assert K.save_pack(w.name, work / "w1", pk, waits=True) == "live" and K.pack_of(get_voice(w.name))["clip"] == "clipW"
        # 7. the move guard: a target outside the folder, or an occupied one, refuses the whole call
        outside = tmp / "elsewhere" / "ref.wav"
        for bad in ([(tts / "ref.wav", outside)], [(tts / "ref.wav", tts / "previous" / "ref.wav")],
                    [(tts / "ref.wav", tts / "x" / "a.wav"), (tts / "cadence.json", tts / "x" / "a.wav")]):
            try:
                storage.move_within(bad, tts)
                raise AssertionError(f"moved: {bad}")
            except storage.ModelError:
                pass
        assert (tts / "ref.wav").exists() and not outside.exists() and not (tts / "x").exists()
    return "save live / pending / unheard set aside, A/B state + route, keep current and keep new (x2), 5 packs all kept, guard"


@check("relocate: a second checkout on the same data rewrites nothing; a real move still rewrites stored paths")
def u_relocate_guard():
    import contextlib
    import io
    from undercover.core import relocate
    from undercover.core.config import ROOT, path
    with isolated_data() as t:
        old = t / "OldCheckout"
        (old / "undercover").mkdir(parents=True)
        (old / "undercover" / "server.py").write_text("# an UnderCover checkout", encoding="utf-8")
        v = _fake_voice("zz-reloc")
        meta = v.dir / "meta.json"
        d = json.loads(meta.read_text(encoding="utf-8"))
        d["dir"] = str(old / "models" / "voices" / "zz-reloc")
        meta.write_text(json.dumps(d), encoding="utf-8")
        mark = path("cache_dir") / "root.txt"
        mark.write_text(str(old), encoding="utf-8")
        before = meta.read_text(encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            got = relocate.check_and_fix()
        assert got == {} and meta.read_text(encoding="utf-8") == before, got
        assert mark.read_text(encoding="utf-8").strip() == str(old), "root.txt took the second checkout's folder"
        assert "Not rewriting stored paths" in out.getvalue(), out.getvalue()
        # the old folder is gone: a real move, rewritten as before
        (old / "undercover" / "server.py").unlink()
        (old / "undercover").rmdir()
        old.rmdir()
        with contextlib.redirect_stdout(io.StringIO()):
            got = relocate.check_and_fix()
        assert str(meta) in got and json.loads(meta.read_text(encoding="utf-8"))["dir"].startswith(str(ROOT)), got
        assert mark.read_text(encoding="utf-8").strip() == str(ROOT)
    return "second checkout: nothing rewritten, root.txt kept, one line logged; a move: rewritten"


@check("voice display names: defaults (trained / downloaded / explicit), the /display route, every voice API")
def u_display_names():
    from undercover.core import voices as V
    from undercover.safehouse import api as shapi, library
    with isolated_data():
        _fake_voice("barack-obama-uc", "trained in UnderCover: barack obama, 12.0 min, epoch 5", {"trained": True, "project": "p1"})
        _fake_voice("ac-dc-uc", "trained in UnderCover: AC/DC, 30.0 min, epoch 9", {"trained": True})
        _fake_voice("mj", "https://huggingface.co/x/y", {"person": "Michael Jackson"})
        _fake_voice("michael-jackson-botdf-era", "https://huggingface.co/x/z")
        _fake_voice("kermit", "https://huggingface.co/x/k", display="The Frog")
        got = {v.name: V.display_name(v) for v in V.list_voices()}
        assert got == {"barack-obama-uc": "Barack Obama", "ac-dc-uc": "AC/DC", "mj": "Michael Jackson",
                       "michael-jackson-botdf-era": "Michael Jackson Botdf Era", "kermit": "The Frog"}, got
        assert V.voice_info(V.get_voice("barack-obama-uc")) == {"display": "Barack Obama", "trained": True, "demo": False, "likeness_word": None, "ear": None, "waiting_beside": None, "preview": False}
        assert V.voice_info(V.get_voice("mj")) == {"display": "Michael Jackson", "trained": False, "demo": False, "likeness_word": None, "ear": None, "waiting_beside": None, "preview": False}
        # the route: set, read back through the APIs, clear
        h = _FakeHandler(body={"display": "  Barry   O  "})
        assert shapi.handle(h, "POST", "/api/library/voices/barack-obama-uc/display") and h.out["display"] == "Barry O", (h.out, h.err)
        assert V.get_voice("barack-obama-uc").display == "Barry O" and V.get_voice("barack-obama-uc").name == "barack-obama-uc"
        lib = {v["name"]: v for v in library.voices()}
        assert lib["barack-obama-uc"]["display"] == "Barry O" and lib["barack-obama-uc"]["trained"] is True, lib["barack-obama-uc"]
        assert lib["mj"]["display"] == "Michael Jackson" and lib["mj"]["trained"] is False
        assert V.voice_labels()["barack-obama-uc"] == "Barry O" and V.label_of("gone-voice") == "gone-voice"
        from undercover.app import server
        assert {v["name"]: v["display"] for v in server.voices_json()}["barack-obama-uc"] == "Barry O"
        # a rewrite of meta.json (cadence, export, ...) keeps the display and the rest
        v = V.get_voice("barack-obama-uc")
        (v.dir / "meta.json").write_text(json.dumps(v.to_json(), indent=2), encoding="utf-8")
        assert V.get_voice("barack-obama-uc").display == "Barry O" and V.get_voice("barack-obama-uc").extra["project"] == "p1"
        for text in ("", "   "):   # empty clears: back to the default
            h = _FakeHandler(body={"display": text})
            shapi.handle(h, "POST", "/api/library/voices/barack-obama-uc/display")
            assert h.out["display"] == "Barack Obama" and h.out["custom"] is False and V.get_voice("barack-obama-uc").display == "", (text, h.out)
        h = _FakeHandler(body={"display": "x" * 81})
        shapi.handle(h, "POST", "/api/library/voices/mj/display")
        assert h.err and V.get_voice("mj").display == "", "an 81-character name was accepted"
        h = _FakeHandler(body={"display": "x"})
        shapi.handle(h, "POST", "/api/library/voices/nobody/display")
        assert h.err, "a voice that doesn't exist took a display name"
    return "5 defaults, set / clear round-trip, library + app APIs carry display and trained"


@check("voice person (phase 8): stored at export, a name with commas survives, the one-time backfill (backup first, one key)")
def u_person():
    from undercover.core import voices as V
    from undercover.doubleagent import night, service, trainer, project as P
    from undercover.core.engine.rvc import index as rvc_index
    comma = "Earth, Wind & Fire"
    with isolated_data():
        # 1. export stores the person it was made for (the heavy parts stubbed: no torch data, no real RVC model)
        proj = P.Project.create(comma, "speaking")
        tdir = proj.dir / "train"
        tdir.mkdir(parents=True, exist_ok=True)
        (tdir / "e5.pth").write_bytes(b"x")
        (tdir / "data.pt").write_bytes(b"")   # (torch.load is stubbed; export checks the file is there, phase 24)
        proj.train = {"best": 5, "train_minutes": 9.0, "saves": [{"epoch": 5, "identity": 0.8}]}
        proj.save()
        real = (trainer.torch.load, trainer.index_feats, trainer.f0_median, trainer.f0_range, V.install, rvc_index.write_ivf_flat)
        trainer.torch.load = lambda *a, **k: {}
        trainer.index_feats = lambda data: trainer.torch.zeros(4, 4)
        trainer.f0_median, trainer.f0_range = (lambda d: 110.0), (lambda d: [90.0, 110.0, 150.0])
        rvc_index.write_ivf_flat = lambda dest, feats: Path(dest).write_bytes(b"i")
        V.install = lambda files, name, source="", overwrite=False: _fake_voice(name, source)
        try:
            trainer.export_voice(proj, 5, "earth-wind-fire-uc")
        finally:
            trainer.torch.load, trainer.index_feats, trainer.f0_median, trainer.f0_range, V.install, rvc_index.write_ivf_flat = real
        v = V.get_voice("earth-wind-fire-uc")
        assert v.extra["person"] == comma and v.extra["trained"] and v.extra["project"] == proj.name, v.extra
        assert V.trained_person(v) == comma and V.display_name(v) == comma and night._person_from_voice(v) == comma
        # 2. voices exported before it: the person is read from `source` (whole, commas included); backfill adds the key
        old = {"hello-uc": ("trained in UnderCover: Hello, Goodbye, 12.0 min, epoch 7", {"trained": True, "project": "p1"}),
               "plain-uc": ("trained in UnderCover: barack obama, 12.0 min, epoch 5", {"trained": True}),
               "odd-uc": ("trained in UnderCover: Somebody", {"trained": True}),
               "mixed-uc": ("trained in UnderCover: Old Name, 3.0 min, epoch 2", {"trained": True, "person": "Kept Name"}),
               "down-uc": ("https://huggingface.co/x/y", {"person": "Michael Jackson"})}
        for n, (src, ex) in old.items():
            _fake_voice(n, src, ex)
        meta = lambda n: V.voices_dir() / n / "meta.json"
        before = {n: meta(n).read_bytes() for n in old}
        assert V.trained_person(V.get_voice("hello-uc")) == "Hello, Goodbye" and V.trained_person(V.get_voice("odd-uc")) == "Somebody"
        assert service.installed_for(comma) and service.installed_for(comma)[0].name == "earth-wind-fire-uc"
        names = V.backfill_person()
        assert sorted(names) == ["hello-uc", "plain-uc"], names
        for n in old:
            bak = meta(n).with_name("meta.json.before-person")
            if n in names:   # the old file kept byte for byte; the new one is the old content plus the one key
                assert bak.exists() and bak.read_bytes() == before[n], f"{n}: no exact backup"
                new, was = json.loads(meta(n).read_text(encoding="utf-8")), json.loads(before[n])
                assert new["extra"].pop("person") and new == was, f"{n}: more than the one key changed"
            else:
                assert meta(n).read_bytes() == before[n] and not bak.exists(), f"{n} was touched"
        assert V.get_voice("hello-uc").extra["person"] == "Hello, Goodbye" and V.get_voice("plain-uc").extra["person"] == "barack obama"
        assert V.display_name(V.get_voice("plain-uc")) == "Barack Obama"
        assert V.backfill_person() == [] and (V.voices_dir() / "hello-uc" / "meta.json.before-person").read_bytes() == before["hello-uc"]
        assert [v.name for v in V.list_voices()].count("hello-uc") == 1, "a backup folder or file became a voice"
    return "export stores it, \"Hello, Goodbye\" read whole, 2 of 5 old voices backfilled (exact backups), the others untouched"


@check("one-shot grammar: '<song> by <artist> as <person>', '<song> as <person>', and today's forms")
def u_grammar():
    from undercover.frontman.oneshot import parse_request as pr
    from undercover.core.voices import ModelError
    want = {
        "Hello by Adele as Peter Griffin": ("Hello", "Peter Griffin", "Adele", False, None),
        "cover of Hello by Adele as Peter Griffin": ("Hello", "Peter Griffin", "Adele", False, None),
        "acoustic cover of Hello by Adele as Peter Griffin": ("Hello", "Peter Griffin", "Adele", False, "acoustic"),
        "Hello as Peter Griffin": ("Hello", "Peter Griffin", None, False, None),
        "Hello as sung by Peter Griffin": ("Hello", "Peter Griffin", None, False, None),
        "Stand By Me as Peter Griffin": ("Stand By Me", "Peter Griffin", None, False, None),
        "Stand By Me by Ben E. King as Peter Griffin": ("Stand By Me", "Peter Griffin", "Ben E. King", False, None),
        "As It Was as A Tribe Called Quest": ("As It Was", "A Tribe Called Quest", None, False, None),
        "Hello by Peter Griffin": ("Hello", "Peter Griffin", None, True, None),               # ambiguous: read as the voice
        "acoustic version of Hello by Peter Griffin": ("Hello", "Peter Griffin", None, True, "acoustic"),
        "cover of Party In The USA by Morgan Freeman": ("Party In The USA", "Morgan Freeman", None, True, None),
        "Party in the USA by Miley Cyrus by Morgan Freeman": ("Party in the USA", "Morgan Freeman", "Miley Cyrus", False, None),
        "Bohemian Rhapsody sung by Obama": ("Bohemian Rhapsody", "Obama", None, False, None),
        "have SpongeBob sing Toxic": ("Toxic", "SpongeBob", None, False, None),
        "Obama sings Hello as a ballad": ("Hello", "Obama", None, False, "ballad"),
        "Hello cover by Peter Griffin": ("Hello", "Peter Griffin", None, False, None),
        "Hello cover as Peter Griffin": ("Hello", "Peter Griffin", None, False, None),
        "Peter Griffin sings Hello by Adele": ("Hello", "Peter Griffin", "Adele", False, None),
    }
    for text, w in want.items():
        r = pr(text)
        got = (r["song"], r["person"], r["artist"], r["by_only"], r["style"])
        assert got == w, f"{text!r}: {got} != {w}"
    try:
        pr("just some words")
        raise AssertionError("an unreadable request was accepted")
    except ModelError as e:
        assert "Hello by Adele as Peter Griffin" in str(e)
    return f"{len(want)} requests read as expected"


def _track(title, artist, views, url=None, year=0, kind="track"):
    from undercover.core.youtube import SongResult
    return SongResult(id=url or f"{artist}-{title}", url=f"https://youtu.be/{url or artist}-{title}".replace(" ", "_"),
                      title=f"{artist} - {title}", raw_title=title, channel=artist, duration=240.0, views=views,
                      kind=kind, artist=artist if kind == "track" else "", song=title if kind == "track" else "", year=year)


@check("one-shot asks: ambiguity detection on fixed search results (no network), the answer lasts until a different song")
def u_ambiguity():
    from undercover.frontman import ambiguity as A
    hello = [_track("Hello", "Adele", 1_500_000_000, year=2015), _track("Hello", "Lionel Richie", 300_000_000, year=1984),
             _track("Hello - Remastered 2011", "Martin Solveig", 200_000_000, year=2010),
             _track("Hello (Live)", "Adele", 5_000_000, kind="video"), _track("Hello Again", "Neil Diamond", 9_000_000)]
    ch = A.detect("Hello", hello)
    assert [c["artist"] for c in ch] == ["Adele", "Lionel Richie", "Martin Solveig"], ch
    assert ch[0]["year"] == 2015 and ch[1]["year"] == 1984 and all(c["song_url"].startswith("https://") for c in ch)
    # not ambiguous: one artist (live / duplicate uploads don't count), a nobody's namesake, a different title
    assert A.detect("Hello", [_track("Hello", "Adele", 9e8), _track("Hello", "Adele", 1e6, url="b"), _track("Hello (Live)", "Adele", 1e5, kind="video")]) == []
    assert A.detect("Hello", [_track("Hello", "Adele", 1_500_000_000), _track("Hello", "Some Garage Band", 4_000)]) == []
    assert A.detect("Toxic", hello) == [] and A.detect("", hello) == []
    assert A.detect("Party in the USA", [_track("Party in the U.S.A.", "Miley Cyrus", 8e8), _track("Party in the USA", "Cover Kid", 2e5)]) == []
    assert len(A.detect("HELLO!", hello[:2])) == 2   # case and punctuation don't matter
    # deeper: a famous version known only by its official upload on the artist's channel counts (by its video views,
    # far below the top one's but famous); the same upload on someone else's channel, a cover or karaoke does not
    def vid(raw, channel, views):
        r = _track(raw, channel, views, url=raw[:12], kind="video")
        r.raw_title = raw
        return r
    deep = [_track("Hello", "Adele", 40_000_000, year=2015), vid("Adele - Hello (Official Music Video)", "Adele", 3_300_000_000),
            vid("Lionel Richie - Hello (Official Music Video)", "lionelrichie", 140_000_000),
            vid("Pop Smoke - Hello (Official) ft. A Boogie wit da Hoodie", "POP SMOKE", 218_000_000),
            vid("Cardi B - Hello [Official Audio]", "Cardi B", 7_700_000),
            vid("Lionel Richie - Hello (Karaoke Version)", "Sing King", 9_600_000),
            vid("Martin Solveig & Dragonette - Hello (Official Music Video)", "Spinnin' Records", 27_000_000)]
    ch4 = A.detect("Hello", deep)
    assert [(c["artist"], c["track"]) for c in ch4] == [("Adele", True), ("Pop Smoke", False), ("Lionel Richie", False)], ch4
    assert ch4[0]["song_url"] == deep[0].url and ch4[2]["title"] == "Hello", ch4   # the album track is the one linked
    assert A.detect("Hello", deep[:2] + deep[4:]) == []   # Adele twice, a nobody, karaoke, a label's upload
    assert A.MAX_CHOICES == 4
    # the lookup: searched once per title (cached); a failing search never raises (the plan just doesn't ask)
    A.reset()
    calls = []

    def fake_search(q, limit=8):
        calls.append(q)
        return hello
    assert A.choices_for("Hello", fake_search) == ch and A.choices_for("hello", fake_search) == ch and len(calls) == 1, calls
    assert A.cached_choices("Hello") == ch and A.cached_choices("Toxic") == []

    def broken(q, limit=8):
        raise RuntimeError("offline")
    assert A.choices_for("Something", broken) == [] and A.choices_for("Something", fake_search) == [], "a failed lookup was retried at once"
    assert A.choices_for("Hello", fake_search) == ch
    # never a silent guess: no choice -> the first, with the sentence; a choice -> that one, no sentence
    pick, went = A.resolve("Hello")
    assert pick["artist"] == "Adele" and went == "I went with Adele's Hello", (pick, went)
    pick, went = A.resolve("Hello", ch[1]["song_url"])
    assert pick["artist"] == "Lionel Richie" and went is None
    # a " by " is the artist only when the search for the split form puts that artist on top (else the whole phrase)
    from undercover.frontman.oneshot import parse_request as pr
    searches = []

    def by_search(q, limit=8):
        searches.append(q)
        return {"Hello Adele": [_track("Hello", "Adele", 9e8)], "Close Night": [_track("Closer", "Nine Inch Nails", 9e8)],
                "Killed Death": []}.get(q, [])
    r = A.settle(pr("Stand By Me as Barack Obama"), by_search)
    assert (r["song"], r["artist"]) == ("Stand By Me", None) and not searches, (r, searches)   # whole, no search needed
    r = A.settle(pr("Hello by Adele as Barack Obama"), by_search)
    assert (r["song"], r["artist"], r["person"]) == ("Hello", "Adele", "Barack Obama"), r
    r = A.settle(pr("Close By Night as Barack Obama"), by_search)
    assert (r["song"], r["artist"], r["person"]) == ("Close By Night", None, "Barack Obama"), r
    r = A.settle(pr("Killed By Death as Barack Obama"), by_search)   # nothing found: the whole phrase
    assert (r["song"], r["artist"]) == ("Killed By Death", None), r
    A.settle(pr("Hello by Adele as Barack Obama"), by_search)
    assert searches.count("Hello Adele") == 1, "the same check was searched twice"
    r = A.settle(pr("Hello by Adele as Barack Obama"), broken)   # offline: the split as typed
    assert r["artist"] == "Adele"
    assert A.settle(pr("Hello as Barack Obama"), broken)["artist"] is None
    # the answer lasts until a different song is requested
    k = A.song_key("Hello")
    assert A.recall(k) is None
    A.remember(k, ch[1]["song_url"], ch)
    r = A.recall(k)
    assert r["chosen"]["artist"] == "Lionel Richie" and [c["artist"] for c in r["others"]] == ["Adele", "Martin Solveig"], r
    assert A.recall(A.song_key("hello!")) is not None, "the same title typed differently must be the same song"
    A.requested(k)                                    # the same song again: still in effect
    assert A.recall(k) is not None
    assert A.recall(A.song_key("Toxic")) is None      # asking about another song doesn't show Hello's answer
    A.requested(A.song_key("Toxic"))                  # ...and a request for another song forgets it
    assert A.recall(k) is None, "the answer outlived a request for a different song"
    A.remember(k, ch[0]["song_url"], ch)
    A.requested(A.song_key("Hello", "Adele"))         # the same title with an artist named is a different request
    assert A.recall(k) is None
    A.remember(k, "https://youtu.be/pasted", [])      # a pasted link that was never a choice: known by its url alone
    assert A.recall(k)["chosen"]["song_url"] == "https://youtu.be/pasted" and A.recall(k)["others"] == []
    A.reset()
    return "3 choices for Hello; one artist / a nobody / covers are not ambiguous; went-with sentence; answer kept until a different song"


@check("one-shot asks: the plan step asks, the choice is remembered across requests, a different song forgets it")
def u_ask_flow():
    from undercover.core import jobs
    from undercover.doubleagent import service
    from undercover.frontman import ambiguity as A, api as fapi, oneshot
    hello = [_track("Hello", "Adele", 1_500_000_000, year=2015), _track("Hello", "Lionel Richie", 300_000_000, year=1984)]
    A.reset()
    A.choices_for("Hello", lambda q, limit=8: hello)       # the lookup the plan step does, answered from the fixture
    A.choices_for("Toxic", lambda q, limit=8: [_track("Toxic", "Britney Spears", 9e8)])   # no network: one artist
    A.settle({"song": "Hello", "artist": "Adele"}, lambda q, limit=8: [hello[0]])   # "Hello by Adele" checks out
    from undercover.frontman import oneshot as os_mod
    keep = (service.installed_for, service.online_for, service.subject_of, os_mod.subject_of)
    service.installed_for, service.online_for = (lambda p: []), (lambda p, n=2: [])   # no network, no voices
    service.subject_of = os_mod.subject_of = lambda p, pick=None: {"kind": "person", "label": f"{p} is a real person.", "choices": []}
    made = []
    iso = isolated_data()   # starting a job must never reach the real folders, whatever it writes
    iso.__enter__()

    def plan(text):
        h = _FakeHandler()
        fapi._auto_plan(h, text, False)
        assert h.err is None, h.err
        return h.out

    def start(body):
        job = oneshot.start_oneshot(body)
        made.append(job)
        job.finish("cancelled", message="smoke")             # not run: the GPU queue is only drained below
        return job
    try:
        p = plan("Hello as Peter Griffin")
        assert p["ask"] is True and [c["artist"] for c in p["choices"]] == ["Adele", "Lionel Richie"] and "chosen" not in p, p
        assert p["voice"]["how"] == "train" and p["voice"]["display"] == "Peter Griffin" and p["song_query"] == "Hello", p
        assert plan("Hello by Adele as Peter Griffin")["ask"] is False, "an artist named: nothing to ask"
        assert plan("Toxic as Peter Griffin")["ask"] is False and "chosen" not in plan("Toxic as Peter Griffin")
        pn = plan("Hello by Peter Griffin")
        assert pn["ask"] is True and "voice" in pn["note"] and pn["person"] == "Peter Griffin", pn   # said out loud
        assert "note" not in p
        # no choice: the job still starts (the run goes with the first and says so); nothing is remembered
        j = start({"text": "Hello as Peter Griffin"})
        assert j.resume_spec["choice"] is None and plan("Hello as Peter Griffin")["ask"] is True
        # the user chose Lionel Richie: remembered, so the next plan doesn't ask and offers "the other one"
        lionel = hello[1].url
        j = start({"text": "Hello as Peter Griffin", "choice": lionel})
        assert j.resume_spec["choice"] == lionel
        p = plan("Hello as Obama")
        assert p["ask"] is False and p["chosen"]["artist"] == "Lionel Richie" and [c["artist"] for c in p["others"]] == ["Adele"], p
        # a later request for the same song, no choice sent: the answer given is still in effect
        j = start({"text": "Hello as Obama"})
        assert j.resume_spec["choice"] == lionel, j.resume_spec
        # the other one, chosen from the offer
        j = start({"text": "Hello as Obama", "choice": hello[0].url})
        p = plan("Hello as Obama")
        assert p["chosen"]["artist"] == "Adele" and [c["artist"] for c in p["others"]] == ["Lionel Richie"], p
        # a different song is requested: the answer is forgotten, Hello asks again
        start({"text": "Toxic as Obama"})
        assert plan("Hello as Obama")["ask"] is True
    finally:
        service.installed_for, service.online_for, service.subject_of, os_mod.subject_of = keep
        iso.__exit__(None, None, None)
        while not jobs.GPU_QUEUE.empty():
            jobs.GPU_QUEUE.get_nowait()
        for j in made:
            jobs.JOBS.pop(j.id, None)
        A.reset()
    return "asks for Hello, remembers Lionel Richie, offers Adele, forgets on Toxic"


def _old(p: Path, hours: float = 8):
    import os
    t = time.time() - hours * 3600
    os.utime(p, (t, t))


def best_time(run, limit: float, tries: int = 3) -> tuple[float, list]:
    """A timing check that a busy PC can't fail by itself: run() -> (seconds, value), up to `tries` times, stopping at
    the first under `limit`. Load only ever adds time, so one fast run proves the code is fast; a real slowdown is slow
    every time and still fails. -> (the best seconds, every (seconds, value) tried)."""
    tried = []
    for _ in range(tries):
        tried.append(run())
        if tried[-1][0] < limit:
            break
    return min(t for t, _ in tried), tried


def _slowest_doctor_checks(n: int = 3) -> str:
    """Each fast check timed on its own: which ones made the Doctor slow (for a timing failure's message)."""
    from undercover.core import doctor, manifest
    ctx = {"full": False, "status": None, "manifest": manifest.load(), "installed": manifest.installed(),
           "pins": manifest.pins(), "hash": lambda p: ""}
    ctx["expected"] = manifest.expected_packages(ctx["manifest"], ctx["pins"])
    took = []
    for c in doctor.CHECKS:
        t0 = time.time()
        try:
            c(ctx)
        except Exception:   # noqa: BLE001 - timing only; u_doctor_fast checks the results
            pass
        took.append((time.time() - t0, c.__name__))
    return ", ".join(f"{name} {t:.2f} s" for t, name in sorted(took, reverse=True)[:n])


@check("doctor (phase 10): fast checks run in < 2 s with no GPU, every result well-formed, the CLI answers")
def u_doctor_fast():
    import subprocess
    from undercover.core import doctor

    def here():
        t0 = time.time()
        rep = doctor.run(False)
        return time.time() - t0, rep

    def cold():   # a fresh Python: imports included, as the command line runs it
        out = subprocess.run([sys.executable, "-c", "import sys, time; t=time.time(); from undercover import doctor as d; "
                              "r=d.doctor.run(False); print(round(time.time()-t,2), 'torch' in sys.modules, r['counts']['bad'])"],
                             cwd=ROOT, capture_output=True, text=True, timeout=120, env=env)
        secs, torch_loaded, _ = out.stdout.split()
        return float(secs), torch_loaded

    with isolated_data() as box:
        took, tried = best_time(here, 2)
        rep = tried[-1][1]
        for r in rep["results"]:
            assert set(r) >= {"id", "group", "state", "title", "say", "detail", "fix"} and r["state"] in ("ok", "info", "warn", "bad"), r
        assert took < 2, (f"fast checks took {', '.join(f'{t:.2f}' for t, _ in tried)} s in {len(tried)} tries; "
                          f"slowest: {_slowest_doctor_checks()}")
        # the fresh Python gets this sandbox too (the settings with these [paths], as _sandbox_config does): the user's
        # own state (phone access on means a PowerShell firewall read the command line waits for, ~1 s) mustn't decide
        # the result, and it never reads the real folders
        import re
        from undercover.core import config
        block = "[paths]\n" + "".join(f"{k} = '{v}'\n" for k, v in config.CONFIG["paths"].items()) + "\n"
        cold_cfg = box / "cold.toml"
        mine = (ROOT / "config.toml").read_text(encoding="utf-8") if (ROOT / "config.toml").is_file() else ""   # the user's file, if any
        cold_cfg.write_text(config.replace_section(mine, "paths", block), encoding="utf-8")
        env = {**os.environ, "UNDERCOVER_CONFIG": str(cold_cfg), "UNDERCOVER_TEST_ROOTS": str(box)}
        secs, tried_cold = best_time(cold, 2)
    assert all(t == "False" for _, t in tried_cold), "the fast checks imported torch"
    assert secs < 2, f"the command line's fast check took {', '.join(f'{t:.2f}' for t, _ in tried_cold)} s in {len(tried_cold)} tries"
    retries = f" (best of {len(tried)} / {len(tried_cold)})" if len(tried) + len(tried_cold) > 2 else ""
    return f"{len(rep['results'])} checks in {took:.2f} s here, {secs:.2f} s from a cold start{retries}, no torch"


@check("doctor: a damaged model file is found (size, then checksum) and fetched again: replaced only once the new copy checks out")
def u_doctor_models():
    from undercover.core import doctor, manifest, setup_models
    from undercover.core.config import path
    with isolated_data() as t:
        good = t / "good.bin"
        good.write_bytes(b"pinned model " * 50000)
        bad_src = t / "bad.bin"
        bad_src.write_bytes(b"something else" * 1000)
        local = "_test/model.bin"
        sha = setup_models.sha256_of(good)
        setup_models.PINNED[local] = {"urls": [bad_src.as_uri(), good.as_uri()], "sha256": sha, "part": "test", "about": ""}
        try:
            dest = path("models_dir") / local
            dest.parent.mkdir(parents=True)
            dest.write_bytes(good.read_bytes()[:1000])   # cut off
            manifest.save({"v": 1, "kind": "observed", "packages": {}, "models": {local: {"size": good.stat().st_size, "sha256": sha}}})
            row = lambda rep: next(r for r in rep["results"] if r["id"] == f"model:{local}")   # noqa: E731
            assert row(doctor.run(False))["state"] == "bad", "a cut-off file wasn't seen by the fast check"
            # the same size but different bytes: only the full check (checksum) sees it
            dest.write_bytes(bytes(b ^ 1 for b in good.read_bytes()))
            assert not any(r["id"] == f"model:{local}" for r in doctor.run(False)["results"]), "fast check flagged a same-size file"
            assert row(doctor.run(True))["state"] == "bad", "the checksum check missed a damaged file"
            damaged = dest.read_bytes()
            # the first source serves the wrong bytes: refused, the damaged copy untouched, no .part left; the second is right
            setup_models.PINNED[local]["urls"] = [bad_src.as_uri()]
            try:
                setup_models.refetch(local)
                raise AssertionError("a download with the wrong checksum replaced the file")
            except setup_models.ModelError:
                assert dest.read_bytes() == damaged and not dest.with_name(dest.name + ".part").exists()
            setup_models.PINNED[local]["urls"] = [bad_src.as_uri(), good.as_uri()]
            setup_models.refetch(local)
            assert dest.read_bytes() == good.read_bytes(), "the good copy didn't replace the damaged one"
            assert not any(r["id"] == f"model:{local}" for r in doctor.run(True)["results"]), "still flagged after the fix"
            # a missing file the record knew about is offered (and fetched at startup); one it never saw waits for first use
            dest.unlink()
            assert row(doctor.run(False))["fix"]["id"] == f"model:{local}"
            # test mode: a re-download into the app's real model folder is refused, whatever the list says
            from undercover.core import config
            saved = dict(config.CONFIG["paths"])
            config.CONFIG["paths"]["models_dir"] = str(ROOT / "models")
            try:
                setup_models.refetch(local)
                raise AssertionError("a test re-downloaded into the real models folder")
            except setup_models.ModelError as e:
                assert "Test mode" in str(e), e
            finally:
                config.CONFIG["paths"] = saved
        finally:
            setup_models.PINNED.pop(local, None)
    return "cut-off and same-size damage found; wrong source refused; good copy swapped in"


@check("doctor: startup healing: a cut-off model is fetched again in the background, a new good one is recorded, a failed fetch waits a day")
def u_doctor_startup():
    import threading
    from undercover.core import doctor, manifest, setup_models
    from undercover.core.config import path

    def settle():
        for t in [t for t in threading.enumerate() if t.name == "doctor"]:
            t.join(60)
    with isolated_data() as t:
        good = t / "good.bin"
        good.write_bytes(b"pinned model " * 40000)
        sha = setup_models.sha256_of(good)
        cut, new = "_test/cut.bin", "_test/new.bin"
        for local in (cut, new):
            setup_models.PINNED[local] = {"urls": [good.as_uri()], "sha256": sha, "part": "test", "about": ""}
        try:
            m = path("models_dir")
            (m / "_test").mkdir(parents=True)
            (m / cut).write_bytes(good.read_bytes()[:500])
            (m / new).write_bytes(good.read_bytes())
            manifest.save({"v": 1, "kind": "observed", "packages": {}, "models": {cut: {"size": good.stat().st_size, "sha256": sha}}})
            hb = path("logs_dir") / "heartbeat-7899.jsonl"
            doctor.heal_at_startup(hb, None)
            settle()
            assert (m / cut).read_bytes() == good.read_bytes(), "the cut-off file wasn't fetched again"
            assert manifest.load()["models"][new]["sha256"] == sha, "the new good file wasn't recorded"
            # the source is gone: the fetch fails, is remembered, and the next start doesn't try again (no download loop)
            (m / cut).write_bytes(b"x")
            setup_models.PINNED[cut]["urls"] = [(t / "missing.bin").as_uri()]
            doctor.heal_at_startup(hb, None)
            settle()
            assert cut in (doctor._state().get("refetch_failed") or {}), doctor._state()
            doctor.heal_at_startup(hb, None)
            settle()
            log = (path("logs_dir") / "doctor.log").read_text(encoding="utf-8")
            assert "couldn't re-download" in log and log.count("less than a day ago") == 1, log   # tried, failed, then held
        finally:
            for local in (cut, new):
                setup_models.PINNED.pop(local, None)
    return "fetched in the background, recorded, then held after a failure"


@check("doctor: downloads resume from a .part (HTTP Range) and still pass their checksum")
def u_doctor_resume():
    import http.server
    import threading
    from undercover.core import setup_models
    with isolated_data() as t:
        data = bytes(range(256)) * 16384   # 4 MB
        seen, drop = [], {"once": False}

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                rng = self.headers.get("Range")
                seen.append(rng)
                start = int(rng.split("=")[1].split("-")[0]) if rng else 0
                self.send_response(206 if rng else 200)
                self.send_header("Content-Length", str(len(data) - start))
                self.end_headers()
                if drop["once"]:   # the connection dies 2.5 MB in (the download keeps whole 1 MB pieces: 2 MB)
                    drop["once"] = False
                    self.wfile.write(data[start:start + 2621440])
                    self.wfile.flush()
                    self.connection.shutdown(2)
                    return
                self.wfile.write(data[start:])
        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            dest = t / "app" / "models" / "_test" / "r.bin"
            dest.parent.mkdir(parents=True)
            dest.with_name("r.bin.part").write_bytes(data[:300000])   # an earlier try stopped here
            sha = __import__("hashlib").sha256(data).hexdigest()
            setup_models.fetch(f"http://127.0.0.1:{srv.server_port}/r.bin", dest, sha, "_test/r.bin")
            assert dest.read_bytes() == data and seen == ["bytes=300000-"], seen
            # a connection that dies mid-file: the next try carries on from the .part
            dest.unlink()
            seen.clear()
            drop["once"] = True
            setup_models.fetch(f"http://127.0.0.1:{srv.server_port}/r.bin", dest, sha, "_test/r.bin")
            assert dest.read_bytes() == data and seen == [None, "bytes=2097152-"], seen
        finally:
            srv.shutdown()
    return "resumed from an old .part and after a dropped connection; complete and checked"


@check("doctor: stale leftovers are cleared through the guarded delete; young ones, projects, owned folders and the library are left alone")
def u_doctor_stale():
    import json as _json
    from undercover.core import doctor
    from undercover.core.config import path
    with isolated_data():
        m, c, up = path("models_dir"), path("cache_dir"), path("uploads_dir")
        old_part = m / "rvc" / "x.pth.part"
        old_tmp = c / "eta.json.0a1b2c3d.tmp"
        vup = c / "vup_abc"
        young = c / "stems" / "s.part.flac"
        proj = c / "training" / "someone" / "embeddings.tmp.npz.part"
        owned = c / "stems" / "owned.part"
        upload = up / "upload_123.part"
        song = up / "song.mp3"
        for p in (old_part, old_tmp, young, proj, owned, upload, song):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"x" * 100)
        vup.mkdir()
        (vup / "voice.zip").write_bytes(b"z")
        for p in (old_part, old_tmp, vup, proj, owned, upload, song):
            _old(p)
        auto, offer, why = doctor.stale_files()
        assert why is None, why
        assert set(auto) == {old_part, old_tmp, vup, owned} and set(offer) == {upload}, (auto, offer)
        # a paused job's folder isn't swept (MasterPlan: a paused training can sit for days); here a preview folder of a
        # running job id and a resume spec's project stand in for it
        (c / "worklist.json").write_text(_json.dumps({"resume": [{"kind": "train", "project": "someone"}]}), encoding="utf-8")
        assert proj not in doctor.stale_files()[0]
        # another server with work running (its heartbeat): nothing is cleared
        hb = path("logs_dir") / "heartbeat-7899.jsonl"
        hb.write_text(_json.dumps({"t": time.strftime("%Y-%m-%d %H:%M:%S"), "event": "beat", "jobs": [{"kind": "cover"}]}) + "\n",
                      encoding="utf-8")
        r = doctor.clear_stale()
        assert not r["removed"] and "another" in r["why"] and old_part.exists(), r
        hb.unlink()
        r = doctor.clear_stale(include_offered=False)   # what startup does: the library's leftovers are only offered
        assert not old_part.exists() and not old_tmp.exists() and not vup.exists() and upload.exists(), r
        assert young.exists() and proj.exists() and song.exists(), "something it shouldn't touch was removed"
        r = doctor.clear_stale()   # the Fix (the user's click): the upload leftover goes too, never the song
        assert not upload.exists() and song.exists(), r
        log = (path("logs_dir") / "doctor.log").read_text(encoding="utf-8")
        assert "cleared" in log, log
    return "4 old leftovers cleared, the upload's only on the Fix; young, project, owned and the song kept"


@check("themes: every shipped theme checks out (tokens only), its fonts are bundled, and it reads well in dark and light")
def u_themes_shipped():
    import re
    from undercover.core.themes import SHIPPED, check_folder

    def lum(h):
        c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        c = [x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]

    def ratio(a, b):
        x, y = lum(a), lum(b)
        return (max(x, y) + 0.05) / (min(x, y) + 0.05)

    def tokens(css, mode):   # a block's custom properties; light = the dark block, then the light one over it
        out = {}
        for sel, body in re.findall(r"([^{}]*)\{([^{}]*)\}", re.sub(r"/\*.*?\*/", " ", css, flags=re.S)):
            if "@font-face" in sel or (mode == "dark" and 'data-theme="light"' in sel):
                continue
            out.update(dict(re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", body)))
        return {k: v.strip() for k, v in out.items()}

    app = (ROOT / "undercover" / "web" / "app.css").read_text(encoding="utf-8")
    root = re.search(r":root\s*\{[^}]*\}", app).group(0)
    base = {"dark": tokens(root, "dark"), "light": {**tokens(root, "dark"), **tokens(re.search(r':root\[data-theme="light"\]\s*\{[^}]*\}', app).group(0), "light")}}
    faces = set(re.findall(r'@font-face \{ font-family: "([^"]+)"', app))
    need = [("--fg", 7)] + [(k, 4.5) for k in ("--muted", "--c-frontman", "--c-doubleagent", "--c-speakeasy", "--c-safehouse", "--ok", "--warn", "--err")]
    names = []
    for d in sorted(x for x in SHIPPED.iterdir() if x.is_dir()):
        problems = check_folder(d, d.name)
        assert not problems, (d.name, problems)
        meta = json.loads((d / "theme.json").read_text(encoding="utf-8"))
        missing = [f for f in meta.get("fonts", []) if f not in faces]
        assert not missing, f"{d.name} uses {missing}, which app.css doesn't bundle"
        css = (d / "theme.css").read_text(encoding="utf-8")
        for mode in ("dark", "light"):
            t = {**base[mode], **tokens(css, mode)}
            for surface in ("--panel-base", "--panel-2-base"):
                for k, least in need:
                    assert ratio(t[k], t[surface]) >= least, f"{d.name} {mode}: {k} {ratio(t[k], t[surface]):.2f}:1 on {surface}, needs {least}"
            for m in ("frontman", "doubleagent", "speakeasy", "safehouse"):
                assert ratio(t["--accent-ink"], t[f"--c-{m}"]) >= 4.5, f"{d.name} {mode}: button text on --c-{m}"
        names.append(d.name)
    assert {"casefile", "midnight", "dossier", "gaslight", "carbon"} <= set(names), names
    return f"{len(names)} themes ({', '.join(names)}): checked, fonts bundled, text >= 7:1 and colours >= 4.5:1 in both modes"


@check("doctor: a package version faked through the install record is reported; the Fix installs only its hashed pin (fake pip)")
def u_doctor_packages():
    from undercover.core import addons, doctor, manifest
    with isolated_data():
        pins = manifest.pins()
        have = manifest.installed()
        name = "soundfile" if "soundfile" in pins else next(n for n in pins if n not in manifest.NEVER_FROM_APP and n in have)
        # the record (written by setup) says the pinned version; the venv now reports another one
        manifest.save({"v": 1, "kind": "installed", "models": {}, "packages": {n: have[n] for n in pins if n in have}})
        real = manifest.installed
        calls, real_pip = [], addons._pip
        addons._pip = lambda *a, what="": calls.append((a, Path(a[a.index("-r") + 1]).read_text(encoding="utf-8")))
        try:
            manifest.save({**manifest.load(), "packages": {**manifest.load()["packages"], name: pins[name]["version"]}})
            manifest.installed = lambda: {**real(), name: "0.0.1"}
            row = next(r for r in doctor.run(False)["results"] if r["id"] == "packages")
            assert row["state"] == "warn" and name in row["detail"] and row["fix"]["id"] == "packages", row
            lines, refused = doctor.package_fix_lines()
            assert lines == [pins[name]["line"]] and not refused, (lines, refused)
            pv = doctor.fix_preview("packages")   # what the page names before the user's click installs anything
            assert pv == {"packages": [{"name": name, "version": pins[name]["version"], "now": "0.0.1"}], "refused": []}, pv
            assert doctor.fix_preview("models") == {}
            r = doctor.fix_packages(dry_run=True)
            args, req = calls[-1]
            assert "--dry-run" in args and "--no-deps" in args and "--require-hashes" in args and "--hash=sha256:" in req, args
            assert req.strip() == pins[name]["line"], req
            # torch is never reinstalled by the app: it's offered as setup.ps1 -Repair
            manifest.installed = lambda: {**real(), "torch": "9.9.9+rocm"}
            assert "torch" in doctor.package_fix_lines()[1]
            row = next(r for r in doctor.run(False)["results"] if r["id"] == "torch")
            assert row["state"] in ("warn", "bad") and row["fix"]["how"] == "offer", row
        finally:
            manifest.installed, addons._pip = real, real_pip
    return f"{name}: reported, one hashed line, --dry-run; torch offered only"


@check("doctor: work a crash interrupted: training resumes once, a one-shot waits for Resume, a repeat crash waits too")
def u_doctor_interrupted():
    import json as _json
    from undercover.core import doctor, worklist
    from undercover.core.config import path
    with isolated_data():
        saved = (worklist._STATE, dict(worklist.RESUMERS))
        worklist._STATE = None
        worklist.RESUMERS.setdefault("train", lambda s: None)
        worklist.RESUMERS.setdefault("oneshot", lambda s: None)
        try:
            hb = path("logs_dir") / "heartbeat-7899.jsonl"
            (path("cache_dir") / "training" / "p1" / "train").mkdir(parents=True)
            (path("cache_dir") / "training" / "p1" / "train" / "resume.pt").write_bytes(b"r")
            train = {"kind": "train", "project": "p1", "label": "Training P1"}

            def crash(*specs):
                hb.write_text(_json.dumps({"t": "2026-10-12 03:00:00", "event": "start"}) + "\n" +
                              _json.dumps({"t": "2026-10-12 03:10:00", "event": "beat",
                                           "jobs": [{"kind": "x", "message": "m", "spec": s} for s in specs]}) + "\n", encoding="utf-8")
            crash(train)
            r = doctor.restore_interrupted(hb)
            st = worklist._state()
            assert r["resumed"] and _bare(st["resume"]) == [train] and not st["held"], (r, st)
            # it died again during the same work: held this time
            worklist._STATE = None
            (path("cache_dir") / "worklist.json").unlink(missing_ok=True)
            r = doctor.restore_interrupted(hb)
            assert not r["resumed"] and worklist._state()["held"], r
            # a one-shot (it would make a new cover) is offered, never started
            worklist._STATE = None
            (path("cache_dir") / "worklist.json").unlink(missing_ok=True)
            (path("logs_dir") / "doctor-state.json").unlink(missing_ok=True)
            crash({"kind": "oneshot", "text": "Hello as X", "label": "One-shot"})
            r = doctor.restore_interrupted(hb)
            assert not r["resumed"] and worklist._state()["held"], r
            # a training without a saved point (finished, or never saved one) isn't started afresh
            worklist._STATE = None
            (path("cache_dir") / "worklist.json").unlink(missing_ok=True)
            (path("cache_dir") / "training" / "p1" / "train" / "resume.pt").unlink()
            crash(train)
            r = doctor.restore_interrupted(hb)
            assert not r["specs"] and not worklist._state()["resume"], r
            # a clean stop: nothing to do
            hb.write_text(_json.dumps({"t": "2026-10-12 03:00:00", "event": "stop"}) + "\n", encoding="utf-8")
            assert doctor.restore_interrupted(hb) is None
            st = doctor._state()
            st["autoresumed"] = {"at": "x", "specs": [train]}
            doctor._save_state(st)
            assert doctor.restore_interrupted(hb) is None
            assert doctor._state().get("autoresumed") is None, "a clean session didn't clear the crash-loop guard"
        finally:
            worklist._STATE = saved[0]
            worklist.RESUMERS.clear()
            worklist.RESUMERS.update(saved[1])
            doctor.LAST.pop("interrupted", None)
    return "resumed once, then held; one-shot held; no saved point = nothing started"


@check("doctor: the diagnostics file has the logs, the record and a report, no audio, and no full folder paths")
def u_doctor_bundle():
    import io
    import zipfile
    from undercover.core import diagnostics, doctor, manifest
    from undercover.core.config import path
    with isolated_data() as t:
        logs = path("logs_dir")
        (logs / "server.log").write_text(f"made a cover in {path('output_dir')}\\x.mp3 from {path('uploads_dir')}\n"
                                         f"app at {ROOT}\nhome {Path.home()}\n", encoding="utf-8")
        (logs / "errors.log").write_text(json.dumps({"p": str(path("cache_dir") / "stems")}), encoding="utf-8")
        (path("output_dir") / "cover.mp3").write_bytes(b"ID3audio")
        manifest.save({"v": 1, "kind": "observed", "venv": {"path": str(ROOT / ".venv")}, "packages": {}, "models": {}})
        rep = doctor.run(False)
        c = diagnostics.contents(rep)
        names = [f["name"] for f in c["files"]]
        assert "logs/server.log" in names and "manifest.json" in names and "doctor-report.json" in names, names
        z = zipfile.ZipFile(io.BytesIO(diagnostics.build(rep)))
        allnames = z.namelist()
        assert not any(n.lower().endswith((".mp3", ".wav", ".flac")) for n in allnames), allnames
        text = "\n".join(z.read(n).decode("utf-8") for n in allnames)
        for real in (str(t), str(ROOT), str(Path.home()), str(t).replace("\\", "\\\\"), str(ROOT).replace("\\", "\\\\")):
            assert real.lower() not in text.lower(), f"{real} is in the file"
        assert "<library>" in text and "<UnderCover>" in text and "%USERPROFILE%" in text, text[:400]
    return f"{len(allnames)} files, paths shortened, no audio"


# ---------------------------------------------------------------- Phone access (phase 11)

@check("phone: QR code (Reed-Solomon, format and version bits against the standard's worked examples; sizes)")
def u_qr():
    from undercover.core import qr
    # thonky.com's "HELLO WORLD" 1-M worked example; the format bits for M / mask 0 and version 7's bits from the standard's tables
    assert qr.rs_ecc([32, 91, 11, 120, 209, 114, 220, 77, 67, 64, 236, 17, 236, 17, 236, 17], 10) == [196, 35, 39, 119, 235, 215, 231, 226, 93, 23]
    assert format(qr.format_bits(0), "015b") == "101010000010010"
    assert format(qr.version_bits(7), "018b") == "000111110010010100"
    m = qr.matrix("http://192.168.1.23:7860/pair#K7P4QX")
    n = len(m)
    assert n == 29 and all(len(r) == n for r in m), n   # version 3
    for r0, c0 in ((0, 0), (0, n - 7), (n - 7, 0)):   # the three finder patterns
        ring = [m[r0][c0 + i] for i in range(7)] + [m[r0 + 6][c0 + i] for i in range(7)]
        assert all(ring) and m[r0 + 3][c0 + 3] and not m[r0 + 1][c0 + 1], (r0, c0)
    assert len(qr.matrix("x" * 200)) == 57   # version 10, the largest here
    assert qr.svg("abc").startswith("<svg") and 'viewBox="0 0 29 29"' in qr.svg("abc")
    return "Reed-Solomon, format, version bits; versions 3 and 10"


@check("phone: pairing (single use, expiry, wrong codes, rate limit), tokens stored hashed, Revoke, the switch")
def u_phone_pairing():
    from undercover.core import phone
    with isolated_data() as t:
        saved = (None, phone.PAIRING, list(phone._TRIES))
        phone._FILE[0], phone.PAIRING, phone._TRIES[:] = None, None, []
        try:
            assert phone.state_file().parent == t, phone.state_file()
            assert not phone.enabled() and phone.device_for("anything") is None
            try:
                phone.new_pairing()
                raise AssertionError("a code was made with Phone access off")
            except phone.PhoneError:
                pass
            phone.set_enabled(True)
            p = phone.new_pairing()
            assert len(p["code"]) == 6 and set(p["code"]) <= set(phone.ALPHABET) and 290 < p["until"] - time.time() <= 300

            def refused(code, addr="10.0.0.9", want=403):
                try:
                    phone.claim(code, "x", addr)
                except phone.PhoneError as e:
                    assert e.status == want, (e.status, str(e))
                    return str(e)
                raise AssertionError(f"{code!r} was accepted")
            wrong = "".join(c for c in phone.ALPHABET if c not in p["code"])[:6]
            refused(wrong)
            token, dev = phone.claim(" " + p["code"].lower()[:3] + "-" + p["code"][3:], "Pixel 8", "10.0.0.9")   # typed loosely
            assert dev["name"] == "Pixel 8" and len(token) >= 40
            refused(p["code"])   # once only
            text = phone.state_file().read_text(encoding="utf-8")
            assert token not in text and phone.hash_token(token) in text, "the token isn't stored as its hash"
            assert phone.device_for(token)["id"] == dev["id"] and phone.device_for(token + "x") is None
            p2 = phone.new_pairing()   # expiry
            phone.PAIRING["until"] = time.time() - 1
            assert "run out" in refused(p2["code"])
            p3 = phone.new_pairing()   # five wrong codes cancel it
            for _ in range(phone.MAX_TRIES):
                refused(wrong, addr="10.0.0.8")
            assert phone.pairing() is None and "run out" in refused(p3["code"], addr="10.0.0.7")
            phone._TRIES[:] = []   # the rate limit: 10 tries per address in 10 minutes, then 429 even with the right code
            p4 = phone.new_pairing()
            for _ in range(phone.RATE[1]):
                try:
                    phone.claim(wrong, "x", "10.0.0.6")
                except phone.PhoneError:
                    pass
                phone.PAIRING = p4 if phone.PAIRING is None else phone.PAIRING
            refused(p4["code"], addr="10.0.0.6", want=429)
            phone._TRIES[:] = []
            phone._FILE[0] = None   # read back from the file
            assert phone.device_for(token)["name"] == "Pixel 8"
            phone.revoke(dev["id"])
            assert phone.device_for(token) is None and phone.devices() == []
            token2, dev2 = phone.claim(phone.new_pairing()["code"], "<b>Tab</b>\n", "10.0.0.5")
            assert dev2["name"] == "bTab/b", dev2["name"]
            phone.set_enabled(False)
            assert phone.device_for(token2) is None and phone.pairing() is None
            phone.set_enabled(True)
            assert phone.device_for(token2)["id"] == dev2["id"]
            assert phone.name_from_agent("Mozilla/5.0 (Linux; Android 14; Pixel 8 Build/AP1A) AppleWebKit") == "Pixel 8"
            assert phone.name_from_agent("Mozilla/5.0 (Linux; Android 10; K) AppleWebKit") == "Android phone"
            assert phone.name_from_agent("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)") == "iPhone"
            assert phone.usable("192.168.1.5") and phone.usable("10.1.2.3") and not phone.usable("8.8.8.8")
            assert not phone.usable("127.0.0.1") and not phone.usable("169.254.1.1")
        finally:   # forget the test's state: the next use reads its own file again
            phone._FILE[0], phone.PAIRING, phone._TRIES[:] = None, saved[1], saved[2]
            phone._STATE.clear()
            phone._SEEN_AT.clear()
    return "6-character codes, hashed tokens, 5 wrong codes, 10 tries per address"


NETSH_RULE = """Rule Name:                            {name}
----------------------------------------------------------------------
Enabled:                              {enabled}
Direction:                            In
Profiles:                             {profiles}
LocalIP:                              Any
Protocol:                             {proto}
LocalPort:                            {port}
Program:                              {program}
Action:                               {action}
"""
NETSH_STATES = """Domain Profile Settings:
----------------------------------------------------------------------
State                                 ON

Private Profile Settings:
----------------------------------------------------------------------
State                                 {private}

Public Profile Settings:
----------------------------------------------------------------------
State                                 ON
Ok.
"""


@check("phone: the Doctor's network and firewall check (netsh parsing, every verdict, the rows; read only)")
def u_phone_doctor():
    from undercover.core import doctor, phone
    py = r"C:\Users\x\AppData\Local\Programs\Python\Python312\python.exe"

    def rule(action="Allow", profiles="Private", program=py, proto="TCP", port="Any", enabled="Yes", name="python.exe"):
        return NETSH_RULE.format(**locals())
    assert phone.parse_states(NETSH_STATES.format(private="OFF")) == {"Domain": True, "Private": False, "Public": True}
    rules = phone.parse_rules(rule() + "\n" + rule(proto="UDP") + "\n" + rule(name="Other", program=r"C:\other.exe"))
    assert len(rules) == 3 and rules[0]["program"] == py and rules[0]["action"] == "Allow", rules[0]
    v = lambda text, profile="Private", on=True: phone.firewall_verdict(phone.parse_rules(text), {py.upper()}, profile, on, 7860)  # noqa: E731
    assert v(rule())[0] == "ok"
    assert v(rule() + "\n" + rule(action="Block"))[0] == "bad", "a block rule must win"
    assert v(rule(profiles="Public"))[0] == "warn"
    assert v(rule(proto="UDP"))[0] == "warn" and v(rule(enabled="No"))[0] == "warn"
    assert v(rule(port="7000-7900"))[0] == "ok" and v(rule(port="80,443"))[0] == "warn"
    assert v(rule(program="Any", port="7860"))[0] == "ok"
    assert v(rule(profiles="Domain,Private,Public"))[0] == "ok"
    assert v(rule(), on=False)[0] == "info"
    assert v("")[0] == "warn" and "Couldn't read" in v("")[1], "no rules read at all (another language) must say so"
    pub = v(rule(profiles="Private"), profile="Public")
    assert pub[0] == "warn" and "tick Public" not in pub[1] and "Private" in pub[1], pub
    progs = doctor.phone_programs()
    assert len(progs) == 1 and ".venv" not in next(iter(progs)).lower(), f"the listening program is the base python: {progs}"
    with isolated_data():
        saved = (phone._FILE[0], phone.windows_check, phone.lan_address)
        phone._FILE[0] = None
        try:
            assert doctor.c_phone({}) == [], "the check ran with Phone access off"
            phone.set_enabled(True)
            fake = {"network": {"name": "Home", "alias": "Wi-Fi", "category": "Private"}, "firewall": ("ok", "fine"), "error": None}
            phone.lan_address = lambda: "192.168.1.5"
            calls = []
            phone.windows_check = lambda a, p, progs, wait=True, fresh=False: calls.append((wait, fresh)) or fake
            rows = {r["id"]: r for r in doctor.c_phone({})}
            assert rows["phone-network"]["state"] == "ok" and rows["phone-firewall"]["state"] == "ok", rows
            fake["network"]["category"] = "Public"
            fake["firewall"] = ("bad", "blocked")
            rows = {r["id"]: r for r in doctor.c_phone({})}
            assert rows["phone-network"]["state"] == "bad" and "Private" in rows["phone-network"]["say"], rows
            assert all(r["fix"] is None for r in rows.values()), "a Fix that would change Windows"
            doctor.c_phone({"status": {}})
            doctor.c_phone({"full": True})
            assert calls[-2:] == [(False, False), (True, True)], calls   # the server never waits; a full check reads fresh
            fake["network"], fake["firewall"] = None, None   # what windows_check gives for an unknown network
            assert [r["id"] for r in doctor.c_phone({})] == ["phone-network"], "a firewall verdict for an unknown network"
            phone.windows_check = lambda *a, **kw: None
            assert doctor.c_phone({"status": {}})[0]["state"] == "info"   # still reading in the background
            phone.windows_check = lambda *a, **kw: fake
            fake["network"], fake["firewall"] = {"name": "Home", "alias": "Wi-Fi", "category": "Public"}, ("bad", "blocked")
            fake["error"] = "TimeoutExpired"
            assert doctor.c_phone({})[0]["state"] == "warn"
            phone.lan_address = lambda: None
            assert doctor.c_phone({})[0]["state"] == "warn"
            phone.lan_address = lambda: "127.0.0.2"
            assert doctor.c_phone({})[0]["state"] == "info"
        finally:
            phone._FILE[0], phone.windows_check, phone.lan_address = None, saved[1], saved[2]
            phone._STATE.clear()
    try:
        phone.windows_check("1.2.3.4'; Remove-Item x; '", 7860, {py})
        raise AssertionError("a non-address went into the PowerShell line")
    except ValueError:
        pass
    return "allow, block wins, no rule, unreadable, other profile, UDP only, disabled, ports, any program, firewall off; rows"


# ---------------------------------------------------------------- themes (phase 14)

def _theme_css(tid, extra_dark="", extra=""):
    return (f':root[data-uc-theme="{tid}"] {{ --base: #0b0a09; --fg: oklch(0.95 0.01 80); --c-frontman: #c9a45c; '
            f'--display: "Manrope", system-ui, sans-serif; --r: 8px; --lit: 22%; {extra_dark} }}\n'
            f":root[data-uc-theme='{tid}'][data-theme='light'] {{ --base: #f2efe9; --fg: #1a1714; }}\n"
            '@font-face { font-family: "Manrope"; src: url("fonts/Manrope.woff2") format("woff2"); font-weight: 200 800; }\n'
            + extra)


def _theme_zip(tid="smoke-noir", css=None, files=None, top=None, raw=None):
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:   # compressed: a bomb is small on the outside
        if raw:
            for zi, data in raw:
                z.writestr(zi, data)
        else:
            t = top or tid
            z.writestr(f"{t}/theme.json", json.dumps({"id": tid, "name": "Smoke Noir", "author": "smoke", "modes": ["dark", "light"]}))
            z.writestr(f"{t}/theme.css", css if css is not None else _theme_css(tid))
            z.writestr(f"{t}/fonts/Manrope.woff2", b"wOF2" + b"\0" * 64)
            z.writestr(f"{t}/fonts/OFL-Manrope.txt", "SIL Open Font License")
            for name, data in (files or {}).items():
                z.writestr(f"{t}/{name}", data)
    return buf.getvalue()


@check("themes: the css contract, hostile .uctheme files refused, round trip, load-time check, shipped ones untouchable")
def u_themes():
    import zipfile
    from undercover.core import themes
    ok = themes.check_css(_theme_css("x1"), "x1", {"Manrope.woff2"})
    assert ok == [], ok
    assert themes.check_css("/* Case File: the tokens are app.css's */", "casefile") == []
    bad_css = {
        "url(http)": _theme_css("x1", "--base: url(http://evil.example/a.png);"),
        "url() in @font-face": _theme_css("x1", extra='@font-face { font-family: "A"; src: url("https://evil.example/a.woff2"); }'),
        "@import": '@import url("x.css");\n' + _theme_css("x1"),
        "expression": _theme_css("x1", "--fg: expression(alert(1));"),
        "escape": _theme_css("x1", "--fg: \\75 rl(x);"),
        "another selector": _theme_css("x1", extra="body { display: none; }"),
        "another theme's block": _theme_css("x1").replace('"x1"', '"x2"', 1),
        "a non-token property": _theme_css("x1", "display: none;"),
        "!important": _theme_css("x1", "--fg: red !important;"),
        "nesting": ':root[data-uc-theme="x1"] { --fg: red; @media (x) { --fg: blue; } }',
        "a font it doesn't have": _theme_css("x1").replace("Manrope.woff2", "Other.woff2"),
        "a climbing font url": _theme_css("x1").replace('fonts/Manrope.woff2', 'fonts/../../x.woff2'),
        "<style> break-out": _theme_css("x1", "--fg: red;</style><script>"),
        "mismatched quotes": _theme_css("x1").replace(':root[data-uc-theme="x1"]', ':root[data-uc-theme="x1\']', 1),
    }
    for why, css in bad_css.items():
        assert themes.check_css(css, "x1", {"Manrope.woff2"}), f"{why} passed the css check"
    with isolated_data() as t:
        saved = themes.SHIPPED
        shipped = t / "shipped"
        (shipped / "midnight" / "fonts").mkdir(parents=True)
        (shipped / "midnight" / "theme.json").write_text(json.dumps({"id": "midnight", "name": "Midnight"}), encoding="utf-8")
        (shipped / "midnight" / "theme.css").write_text(_theme_css("midnight").split("@font-face")[0], encoding="utf-8")
        themes.SHIPPED = shipped
        try:
            assert storage_inside(themes.library(), t), themes.library()
            hostile = {
                "a climbing path": _theme_zip(raw=[("smoke-x/../../evil.css", "x"), ("smoke-x/theme.json", "{}")]),
                "an absolute path": _theme_zip(raw=[("/smoke-x/theme.css", "x")]),
                "a backslash path": _theme_zip(raw=[("smoke-x\\..\\evil.css", "x")]),
                "a script": _theme_zip(files={"evil.js": "alert(1)"}),
                "an svg": _theme_zip(files={"preview.svg": "<svg/>"}),
                "an exe in fonts": _theme_zip(files={"fonts/x.exe": "MZ"}),
                "a png that isn't": _theme_zip(files={"preview.png": "not a png"}),
                "url(http) in the css": _theme_zip(css=_theme_css("smoke-noir", "--base: url(http://evil.example/x);")),
                "@import": _theme_zip(css='@import "x.css";'),
                "two folders": _theme_zip(raw=[("a1/theme.css", "x"), ("b2/theme.css", "y")]),
                "a bad id": _theme_zip(top="Bad_Id!"),
                "not a zip": b"PK not really",
                "over 4 MB": b"PK" + b"\0" * (themes.MAX_BYTES + 1),
                "a zip bomb": _theme_zip(files={"OFL-big.txt": "a" * (themes.MAX_BYTES + 10)}),
            }
            link = zipfile.ZipInfo("smoke-x/theme.css")
            link.external_attr = (0o120777 << 16)
            hostile["a symlink"] = _theme_zip(raw=[(link, "../../../config.toml")])
            for why, data in hostile.items():
                try:
                    themes.import_zip(data)
                    raise AssertionError(f"a theme with {why} was imported")
                except themes.ThemeError:
                    pass
            assert sorted(x.name for x in themes.library().iterdir()) == [], "something was left behind by a refused import"
            row = themes.import_zip(_theme_zip())
            assert row["ok"] and row["id"] == "smoke-noir" and row["name"] == "Smoke Noir" and not row["shipped"], row
            assert row["css"] == "/api/themes/smoke-noir/theme.css" and row["why"] is None, row
            try:
                themes.import_zip(_theme_zip())
                raise AssertionError("an existing theme was replaced without asking")
            except FileExistsError:
                pass
            themes.import_zip(_theme_zip(), replace=True)
            name, data = themes.export("smoke-noir")   # round trip: export, delete, import the file again
            assert name == "Smoke Noir.uctheme", name
            themes.delete("smoke-noir")
            assert not (themes.library() / "smoke-noir").exists()
            again = themes.import_zip(data)
            assert again["ok"] and themes.css_of("smoke-noir") == _theme_css("smoke-noir")
            assert themes.file_of("smoke-noir", "fonts/Manrope.woff2").read_bytes().startswith(b"wOF2")
            (themes.library() / "smoke-noir" / "theme.css").write_text(_theme_css("smoke-noir", "--base: url(//evil.example/x);"),
                                                                         encoding="utf-8")
            try:
                themes.css_of("smoke-noir")
                raise AssertionError("a hand-edited css with url() was served")
            except themes.ThemeError:
                pass
            assert not [x for x in themes.listing() if x["id"] == "smoke-noir"][0]["ok"]
            try:
                themes.delete("midnight")
                raise AssertionError("a shipped theme was deleted")
            except themes.ThemeError:
                pass
            assert (shipped / "midnight" / "theme.css").exists()
            try:
                themes.import_zip(_theme_zip(tid="midnight"))
                raise AssertionError("an import took a shipped theme's name")
            except FileExistsError:
                pass
            import base64
            made = themes.save({"json": {"id": "smoke-made", "name": "Made"}, "css": _theme_css("smoke-made"),
                                "fonts": [{"file": "Manrope.woff2", "data": base64.b64encode(b"wOF2xx").decode()}]})
            assert made["ok"], made
            themes.save({"json": {"id": "smoke-made", "name": "Made again"}, "css": _theme_css("smoke-made"),
                         "fonts": [{"file": "Manrope.woff2", "data": base64.b64encode(b"wOF2xx").decode()}]})
            assert [x["name"] for x in themes.listing() if x["id"] == "smoke-made"] == ["Made again"]
            try:
                themes.save({"json": {"id": "midnight", "name": "Mine"}, "css": ""})
                raise AssertionError("the maker saved over a shipped theme")
            except FileExistsError:
                pass
            ids = {x["id"]: x["shipped"] for x in themes.listing()}
            assert ids == {"midnight": True, "smoke-noir": False, "smoke-made": False}, ids
            assert themes.export("midnight")[0] == "Midnight.uctheme"   # a friend can start from a shipped one
            try:   # markup in theme.json's texts
                themes.save({"json": {"id": "smoke-xss", "name": "<img src=x onerror=alert(1)>"}, "css": ""})
                raise AssertionError("markup in a theme's name was accepted")
            except themes.ThemeError:
                pass
            # a replace that can't put the new folder in place keeps the old one
            import types
            real_fsutil, calls = themes.fsutil, []

            def flaky(fn, tries=30):
                calls.append(1)
                if len(calls) == 2:   # 1: the old one aside, 2: the new one in place (fails), 3: the old one back
                    raise PermissionError("locked")
                return real_fsutil.retry(fn)
            themes.fsutil = types.SimpleNamespace(retry=flaky)
            try:
                themes.import_zip(_theme_zip(), replace=True)
                raise AssertionError("the failing replace reported success")
            except PermissionError:
                pass
            finally:
                themes.fsutil = real_fsutil
            assert themes.listing() and (themes.library() / "smoke-noir" / "theme.css").exists(), "the old theme was lost"
            assert not [x for x in themes.library().iterdir() if x.name.startswith(".")], "staging or set-aside folders left"
            # leftovers of an interrupted import are swept after an hour
            stale = themes.library() / ".incoming-deadbeef"
            (stale / "fonts").mkdir(parents=True)
            os.utime(stale, (time.time() - 7200, time.time() - 7200))
            themes.listing()
            assert not stale.exists(), "an old staging folder wasn't swept"
            # a library theme whose id a shipped one took later: listed, marked, deletable
            (shipped / "smoke-noir").mkdir()
            (shipped / "smoke-noir" / "theme.json").write_text(json.dumps({"id": "smoke-noir", "name": "Shipped"}), encoding="utf-8")
            (shipped / "smoke-noir" / "theme.css").write_text("", encoding="utf-8")
            rows = [x for x in themes.listing() if x["id"] == "smoke-noir"]
            assert [(x["shipped"], x.get("shadowed", False), x["ok"]) for x in rows] == [(True, False, True), (False, True, False)], rows
            themes.delete("smoke-noir")
            assert not (themes.library() / "smoke-noir").exists() and (shipped / "smoke-noir").exists()
            shutil.rmtree(shipped / "smoke-noir")
            # the write side refuses folders outside a test's own (the storage guard covers the deletes)
            saved_roots = storage_roots()
            try:
                set_storage_roots([t / "elsewhere"])
                themes.import_zip(_theme_zip(tid="smoke-guard"))
                raise AssertionError("a theme was written outside the test's folders")
            except themes.ThemeError:
                pass
            finally:
                set_storage_roots(saved_roots)
        finally:
            themes.SHIPPED = saved
    real = [d for d in (ROOT / "undercover" / "web" / "themes").glob("*") if d.is_dir()] if (ROOT / "undercover" / "web" / "themes").is_dir() else []
    for d in real:   # the shipped themes (they arrive with the look chat's build): the same check as an import
        problems = themes.check_folder(d)
        assert not problems, f"shipped theme {d.name}: {problems}"
    return f"{len(bad_css)} bad css and {len(hostile)} hostile files refused; round trip; {len(real)} shipped themes checked"


def storage_inside(a, b):
    from undercover.core import storage
    return storage._inside(a, b)


def storage_roots():
    from undercover.core import storage
    return storage.ALLOWED_ROOTS


def set_storage_roots(v):
    from undercover.core import storage
    storage.ALLOWED_ROOTS = v


# ---------------------------------------------------------------- self-healing yt-dlp (phase 16)

def _fake_wheel(folder: Path, pkg: str, mod: str, ver: str, requires=(), extra_files=None) -> dict:
    """A tiny pure-Python wheel on disk (a fake yt_dlp or yt_dlp_ejs) -> PyPI's {"url" (file://), "sha", "file"}."""
    import hashlib
    import zipfile
    f = folder / f"{mod}-{ver}-py3-none-any.whl"
    with zipfile.ZipFile(f, "w") as z:
        z.writestr(f"{mod}/__init__.py", f"__version__ = '{ver}'\n")
        z.writestr(f"{mod}/version.py", f"__version__ = '{ver}'\n")
        z.writestr(f"{mod}-{ver}.dist-info/METADATA", f"Metadata-Version: 2.1\nName: {pkg}\nVersion: {ver}\n"
                   + "".join(f"Requires-Dist: {r}\n" for r in requires))
        z.writestr(f"{mod}-{ver}.data/data/share/x.txt", "skipped")
        for n, data in (extra_files or {}).items():
            z.writestr(n, data)
    return {"url": f.as_uri(), "sha": hashlib.sha256(f.read_bytes()).hexdigest(), "file": f.name, "size": f.stat().st_size}


@check("yt-dlp self-heal: fake PyPI + fake resolvers: swap, bad hash, failed test, rollback, offline, detection, dry run")
def u_ytdlp_heal():
    import sys as _sys
    from undercover.core import doctor, manifest, ytdlp
    saved = (ytdlp.fetch_json, ytdlp.RESOLVE, dict(ytdlp.ACTIVE), list(_sys.path), dict(ytdlp.FLAG))

    def purge():
        for m in [m for m in _sys.modules if m == "yt_dlp" or m.startswith(("yt_dlp.", "yt_dlp_ejs"))]:
            del _sys.modules[m]
    assert ytdlp.ejs_pin(['yt-dlp-ejs==0.8.0; extra == "default"', 'certifi==1; extra == "pin"', 'yt-dlp-ejs==0.8.0; extra == "pin"']) == "0.8.0"
    assert ytdlp.ejs_pin(['certifi==1; extra == "pin"']) is None
    assert ytdlp.classify("ERROR: [youtube] x: Requested format is not available") == "broken"
    assert ytdlp.classify("n challenge solving failed: Some formats may be missing") == "broken"
    assert ytdlp.classify("Sign in to confirm you're not a bot") is None and ytdlp.classify("Private video") is None
    pinned = ytdlp.pinned_pair()
    assert set(pinned) == {"yt-dlp", "yt-dlp-ejs"} and all(len(h) == 64 for _, h in pinned.values()), pinned
    with isolated_data() as t:
        pypi = t / "pypi"
        pypi.mkdir()
        venv_yt = ytdlp.venv_pair()["yt"]
        newer = "2099.1.2"
        wheels = {("yt-dlp", newer): _fake_wheel(pypi, "yt-dlp", "yt_dlp", newer, ['yt-dlp-ejs==9.9.0; extra == "pin"']),
                  ("yt-dlp-ejs", "9.9.0"): _fake_wheel(pypi, "yt-dlp-ejs", "yt_dlp_ejs", "9.9.0")}
        state = {"latest": newer, "offline": False}

        def fake_json(name, version=None):
            if state["offline"]:
                raise OSError("no network")
            ver = version or state["latest"]
            w = wheels[(name, ver)]
            req = ['yt-dlp-ejs==9.9.0; extra == "pin"'] if name == "yt-dlp" else []
            return {"info": {"name": name, "version": ver, "requires_dist": req},
                    "urls": [{"packagetype": "bdist_wheel", "filename": w["file"], "url": w["url"],
                              "digests": {"sha256": w["sha"]}, "yanked": False}]}
        scores = {}

        def fake_resolve(d):
            key = Path(d).name if d else "venv"
            return {"ok": scores.get(key, scores.get("*", 0)), "of": 3, "errors": [], "version": key}
        ytdlp.fetch_json, ytdlp.RESOLVE = fake_json, fake_resolve
        new_name = f"{newer}+ejs9.9.0"
        try:
            assert ytdlp.home().parent == Path(config_path("cache_dir")) and storage_inside(ytdlp.home(), t)
            # 1. a newer pair that resolves as many: used, the venv's pair is the way back
            scores.update({"venv": 3, "*": 3})
            r = ytdlp.heal(reason="test")
            assert r["changed"] and r["chose"] == "new" and r["after"]["yt"] == newer, r
            st = ytdlp.load()
            assert st["active"] == new_name and st["last_good"] is None, st
            assert _sys.path[0] == str(ytdlp.home() / new_name) and ytdlp.current()["yt"] == newer
            import yt_dlp as fake_mod   # the swap took effect now: the side pair is what imports
            assert fake_mod.__version__ == newer, fake_mod.__file__
            assert manifest.installed().get("yt-dlp") == venv_yt, "the record's package scan saw the side pair"
            assert not [d for d in ytdlp.home().iterdir() if d.name.startswith(".")], "staging left behind"
            # 2. PyPI's checksum doesn't match: refused, nothing changes
            newest = "2099.2.3"
            wheels[("yt-dlp", newest)] = dict(_fake_wheel(pypi, "yt-dlp", "yt_dlp", newest), sha="0" * 64)
            state["latest"] = newest
            r = ytdlp.heal()
            assert not r["changed"] and "checksum" in r["note"] and ytdlp.load()["active"] == new_name, r
            # 3. a newer pair that resolves fewer: kept the current one, its folder removed
            wheels[("yt-dlp", newest)] = _fake_wheel(pypi, "yt-dlp", "yt_dlp", newest, ['yt-dlp-ejs==9.9.0; extra == "pin"'])
            scores.update({f"{newest}+ejs9.9.0": 1, ".stage": 1})
            scores["*"] = 1   # staging folders have random names: the new one resolves 1
            scores[new_name] = 3
            r = ytdlp.heal()
            assert not r["changed"] and r["scores"]["new"]["ok"] == 1, r
            assert sorted(d.name for d in ytdlp.home().iterdir() if d.is_dir()) == [new_name], list(ytdlp.home().iterdir())
            # 4. the current pair breaks; nothing newer; the last known good comes back (here: the venv's)
            state["latest"] = newer
            scores.update({new_name: 0, "venv": 3, "*": 0})
            r = ytdlp.heal(reason="breakage")
            assert r["changed"] and r["chose"] == "venv" and ytdlp.load()["active"] is None, r
            assert str(ytdlp.home() / new_name) not in _sys.path
            # 5. offline: PyPI unreachable, the current pair kept, said so
            state["offline"] = True
            r = ytdlp.heal()
            assert not r["changed"] and "PyPI" in r["note"], r
            r = ytdlp.heal(offline=True)
            assert not r["changed"] and "new" not in r["scores"]
            state["offline"] = False
            # 6. dry run: tests everything, changes nothing
            scores.update({"*": 3, "venv": 3})
            before = (json.dumps(ytdlp.load(), sort_keys=True), sorted(x.name for x in ytdlp.home().iterdir()))
            r = ytdlp.heal(dry_run=True)
            assert r["dry_run"] and not r["changed"] and r["chose"] == "new", r
            assert (json.dumps(ytdlp.load(), sort_keys=True), sorted(x.name for x in ytdlp.home().iterdir())) == before
            # 7. activation: the active folder gone -> the last known good -> the venv's pair
            ytdlp.heal()
            assert ytdlp.load()["active"] == new_name
            st = ytdlp.load()
            st["last_good"], st["active"] = new_name, "2001.1.1+ejs0.1"
            ytdlp.save(st)
            assert ytdlp.activate()["yt"] == newer and ytdlp.load()["active"] == new_name
            st["active"], st["last_good"] = "gone+ejs1", None
            ytdlp.save(st)
            assert ytdlp.activate()["source"] == "venv"
            # 8. a wheel that climbs out is refused
            evil = _fake_wheel(pypi, "yt-dlp", "yt_dlp", "2099.3.4", extra_files={"../../evil.py": "x"})
            try:
                ytdlp.unpack(Path(evil["url"].replace("file:///", "")), t / "unpacked")
                raise AssertionError("a wheel with a climbing path was unpacked")
            except ytdlp.HealError:
                pass
            # 9. detection: three different videos breaking the same way; the bot check and one bad link don't count
            ytdlp._EVENTS.clear()
            st = ytdlp.load()
            st["auto"] = False
            ytdlp.save(st)
            ytdlp.FLAG["broken"] = None
            ytdlp.note("aaaaaaaaaaa", False, "Sign in to confirm you're not a bot")
            ytdlp.note("bbbbbbbbbbb", False, "Requested format is not available")
            ytdlp.note("bbbbbbbbbbb", False, "Requested format is not available")
            assert ytdlp.breakage() is None
            ytdlp.note("ccccccccccc", False, "nsig extraction failed")
            ytdlp.note("ddddddddddd", False, "This video is unplayable")
            assert ytdlp.breakage()["videos"] == 3 and ytdlp.FLAG["broken"], ytdlp._EVENTS
            assert ytdlp._HEAL["job"] is None or ytdlp._HEAL["job"].finished, "auto is off: no heal by itself"
            rows = {r["id"]: r for r in doctor.c_ytdlp({})}
            assert rows["ytdlp-broken"]["state"] == "warn" and rows["ytdlp-broken"]["fix"]["id"] == "ytdlp", rows
            for v in ("eeeeeeeeeee", "fffffffffff", "ggggggggggg", "hhhhhhhhhhh"):
                ytdlp.note(v, True)
            assert ytdlp.breakage() is None, "successes outweigh the failures"
            assert ytdlp.status()["broken"] is None and ytdlp.FLAG["broken"] is None, "the flag outlived the breakage"
            # one video counted once, however often yt-dlp re-enters extract_info for it
            ytdlp._EVENTS.clear()
            for v in ("ccccccccccc", "ddddddddddd", "iiiiiiiiiii"):
                ytdlp.note(v, False, "nsig extraction failed")
            for _ in range(5):
                ytdlp.note("jjjjjjjjjjj", True)
            assert ytdlp.breakage() and ytdlp.breakage()["ok"] == 1, ytdlp.breakage()
            # recording never waits for a heal (which holds LOCK for minutes)
            import threading as _th
            held, release = _th.Event(), _th.Event()

            def hold():
                with ytdlp.LOCK:
                    held.set()
                    release.wait(10)
            th = _th.Thread(target=hold)
            th.start()
            held.wait(5)
            t0 = time.time()
            ytdlp.note("kkkkkkkkkkk", True)
            release.set()
            th.join()
            assert time.time() - t0 < 1, "note() waited for the heal's lock"
            ytdlp._EVENTS.clear()
            # prune keeps the pair this process has loaded, whatever the state says
            ytdlp.ACTIVE["dir"] = str(ytdlp.home() / new_name)
            st = ytdlp.load()
            st["active"], st["last_good"] = None, None
            ytdlp.save(st)
            ytdlp.prune(st)
            assert (ytdlp.home() / new_name).exists(), "prune removed the loaded pair"
            ytdlp.ACTIVE["dir"] = None
            # leftovers of an interrupted check are swept
            old_dry = ytdlp.home() / ".dry-deadbeef"
            old_dry.mkdir()
            os.utime(old_dry, (time.time() - 7200, time.time() - 7200))
            ytdlp._sweep()
            assert not old_dry.exists()
            # the command line won't heal while UnderCover runs (one heal at a time)
            from undercover import ytdlp as cli
            real_running = cli._server_running
            cli._server_running = lambda: True
            try:
                assert cli.main(["install"]) == 0 and cli.main(["update"]) == 1
            finally:
                cli._server_running = real_running
            # 10b. the System room's button: the same heal as a job
            from undercover.safehouse import api as shapi
            h = _FakeHandler()
            assert shapi.handle(h, "POST", "/api/library/tools/ytdlp-update") and not h.err, h.err
            jid = h.out["job"]
            from undercover.core import jobs as _jobs
            wait_for(lambda: _jobs.JOBS[jid].finished, 30, every=0.2, what="the heal job")
            assert _jobs.JOBS[jid].state == "done", _jobs.JOBS[jid].events[-1:]
            h = _FakeHandler()
            assert shapi.handle(h, "POST", "/api/library/tools/ytdlp-auto") and h.out["ytdlp_auto"] is False
            # 10. the solver missing: the Doctor says so, with the Fix
            real_status = ytdlp.status
            ytdlp.status = lambda: {**real_status(), "ejs": None, "broken": None}
            try:
                row = next(r for r in doctor.c_ytdlp({}) if r["id"] == "ytdlp-ejs")
                assert row["state"] == "bad" and row["title"] == "YouTube's puzzle solver is missing" and row["fix"]["id"] == "ytdlp"
            finally:
                ytdlp.status = real_status
        finally:
            ytdlp.fetch_json, ytdlp.RESOLVE = saved[0], saved[1]
            ytdlp._use(None)
            _sys.path[:] = saved[3]
            ytdlp.ACTIVE.clear()
            ytdlp.ACTIVE.update(saved[2])
            ytdlp.FLAG.clear()
            ytdlp.FLAG.update(saved[4])
            ytdlp._EVENTS.clear()
            purge()
    import yt_dlp
    assert "site-packages" in yt_dlp.__file__, f"the real yt_dlp isn't back: {yt_dlp.__file__}"
    return "swap, bad hash, worse test kept, rollback to the pinned pair, offline, dry run, activation fallback, detection"


def config_path(key):
    from undercover.core.config import path
    return path(key)


# ---------------------------------------------------------------- the First assignment (phase 17)

def _fake_demo_voice(d: Path):
    d.mkdir(parents=True)
    (d / "model.pth").write_bytes(b"not really a model")
    (d / "meta.json").write_text(json.dumps({"name": "agent-smoke", "version": "v2", "sr": 40000, "f0": True, "source": "demo",
                                             "dir": "", "pth": "model.pth", "index": "", "installed": "2026-10-12",
                                             "extra": {"demo": True, "trained": False, "person": "Agent Smoke",
                                                       "note": "demo voice, made for UnderCover, not a real person"}}),
                                 encoding="utf-8")
    import hashlib
    pth = (d / "model.pth").read_bytes()
    (d / "manifest.json").write_text(json.dumps({"files": {"model.pth": {"size": len(pth), "sha256": hashlib.sha256(pth).hexdigest()}}}),
                                     encoding="utf-8")


def _chrome():
    import shutil
    return shutil.which("chrome") or shutil.which("google-chrome") or next(
        (c for c in (r"C:\Program Files\Google\Chrome\Application\chrome.exe", r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe") if Path(c).is_file()), None)


@check("page scripts all parse and load (headless Chrome on the static page; skipped when there is no Chrome)")
def u_page_syntax():
    """A script that fails to parse or redeclares a name takes its whole feature with it (P8-1: tutorial.js, two days; a bad regex in app.js
    would take the page). No server: the web folder is served as /static, the index as /, and the console is read for SyntaxErrors."""
    import http.server, subprocess, tempfile, threading
    exe = _chrome()
    if not exe:
        return "no Chrome here: skipped"
    web = ROOT / "undercover" / "web"

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?")[0]
            f = web / "index.html" if path == "/" else web / path[len("/static/"):] if path.startswith("/static/") else None
            if f is None or not f.is_file():
                self.send_response(404); self.end_headers(); return
            body = f.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", {".js": "text/javascript", ".css": "text/css", ".html": "text/html", ".json": "application/json"}.get(f.suffix, "application/octet-stream"))
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory() as prof:
            r = subprocess.run([exe, "--headless=new", "--disable-gpu", "--mute-audio", f"--user-data-dir={prof}", "--enable-logging=stderr", "--v=0",
                                "--virtual-time-budget=6000", "--dump-dom", f"http://127.0.0.1:{srv.server_address[1]}/"], capture_output=True, text=True, timeout=90)
    finally:
        srv.shutdown()
    bad = [ln.split("CONSOLE", 1)[1][:200] for ln in r.stderr.splitlines() if ":CONSOLE" in ln and ("SyntaxError" in ln or "ReferenceError" in ln)]
    assert not bad, "a page script doesn't parse or load: " + " | ".join(bad)
    return "every page script parses and loads"


@check("page rules from Internal Affairs pass 1 and the Compare fix (source guards: outcomes were tested in a browser)")
def u_page_rules():
    web = ROOT / "undercover" / "web"
    import re
    # the Compare panel: one set of cards per tab, and the octave / key previews never ride with fit on (it overrides them)
    app = (web / "app.js").read_text(encoding="utf-8")
    assert "pv-set" in app and "pvByHand" in app and "Fit's pick" in app and "fit: false" not in app.split("function renderPreviews")[0].split("preview-go")[1], "the Compare panel lost its per-tab sets, or the fit:false workaround is back"
    # M3: result titles and library rows say display names
    lib = (web / "library.js").read_text(encoding="utf-8")
    assert "voiceDisplay(x.voice)" in lib and "voiceDisplay(h.voice)" in app, "voice ids are back in basic-view titles"
    # M2: at phone width the history and Recent rows put their buttons under the title
    assert "previewSync" in app and "pv-for" in app and "keyName" in app, "the Compare panel lost its song / voice tagging or the key spelling"
    work = (web / "work.js").read_text(encoding="utf-8")
    assert 'id="rn-finish-time"' in (web / "index.html").read_text(encoding="utf-8") and "rn-finish-time" in work and "startTime()" in work, "the Running room's Start at / Finish by lost their time fields"
    assert "tr-tonight" in (web / "train.js").read_text(encoding="utf-8") and 'id="tr-tonight"' in (web / "index.html").read_text(encoding="utf-8"), "step 4 lost its Train tonight"
    assert "pvByHand(kind)) S.opts.fit = false; store.set" in app, "Use this must decide by-hand at the click (and save: a comment once swallowed the rest of the line)"
    assert "const day = x => new Date(x.getFullYear()" in lib, "the Library's today / yesterday must use calendar days"
    da = (web / "doubleagent.js").read_text(encoding="utf-8")
    assert "impHtml" in da and 'data-imp-keep' in da and '"/keep"' in da, "DoubleAgent lost the retrained voice's blind A/B"
    assert "progressSay" in app and "step_n" in app, "preview / cover progress lost its step and time-left line"
    # P8-1: two page scripts declaring the same top-level name make the second one not load at all (a `const AS` in aside.js and tutorial.js killed
    # the First assignment for two days). Every top-level const / let / class / var / function name must be unique across the scripts index.html loads.
    ixh = (web / "index.html").read_text(encoding="utf-8")
    seen = {}
    for src in re.findall(r'<script src="/static/([^"]+\.js)"', ixh):
        for nm in re.findall(r"^(?:async\s+)?(?:const|let|var|class|function\*?)\s+([A-Za-z_$][\w$]*)", (web / src).read_text(encoding="utf-8"), re.M):
            seen.setdefault(nm, []).append(src)
    clash = {k: v for k, v in seen.items() if len(v) > 1}
    assert not clash, f"page scripts declare the same top-level name twice (the second script won't load): {clash}"
    assert "const ASIDE = {" in (web / "aside.js").read_text(encoding="utf-8"), "aside.js's state is ASIDE"
    # qa14: the switch's 16 px box is for the checkbox only; the Finish by time field sits inside the same label and drew as a knob
    css = (web / "app.css").read_text(encoding="utf-8")
    # qa15: the one-shot asks which voice (voice_ask / voice_choice), a moved work item says so, Your voices has filters, Train my own opens Now / Tonight on the card
    ix = (web / "index.html").read_text(encoding="utf-8")
    osj = (web / "oneshot.js").read_text(encoding="utf-8")
    assert "voice_choice:" in osj and "data-os-vchoice" in osj and "Pick a voice first." in osj, "the one-shot lost its voice question"
    assert "function workMoved" in work and "workMoved(" in (web / "train.js").read_text(encoding="utf-8"), "work/add's `moved` is no longer said"
    assert "daFilters" in da and "data-own-go" in da and 'id="da-filters"' in ix, "Your voices lost its filter chips, or Train my own its Now / Tonight"
    assert "reportOutcome" in work and "reportName(x)" in work and "While you were out" in ix and "REPORT_WORDS" in work, "the overnight report lost its words"
    # smallfix5 hooks: the one estimator, the tonight start note, the pause note, the paused card, the remove note
    assert all(k in work for k in ("function estimateFor", "const startsNote", "const pausingNote", "function removedNote")) and "/api/work/estimate" in work, "work.js lost a smallfix5 hook"
    assert "data-da-resume" in da and 'id="da-train-eta"' in ix and "estWords(" in da, "DoubleAgent lost its paused card or the estimator's time"
    # qa16 (pass 9): a carried backing voice is said and clearable, a one-shot's pick is drawn at once, Let it speak asks Now / Tonight, the job notifier has a fallback
    jb = (web / "jobs.js").read_text(encoding="utf-8")
    assert 'id="clear-backing"' in app and "function drawPlan" in osj and "data-cad-go" in da and "jfTimer" in jb and "saveStartTime" in work, "a pass-9 page fix is gone"
    # smallfix6 hooks: a tonight add that starts now / waits, queue rows that wait, no times of the page's own in the one-shot
    assert "starts_now" in work and "waits_next_night" in work and "trainWords" in osj and "about 1 hour" not in osj and "about 1 hour" not in ix, "a smallfix6 hook or an estimator time is gone"
    # qa17 (the consultant's small batch): one press on the one-shot with the time in the button; basic says "speak", Deep says "cadence"; the night's work is "tonight";
    # the real-person line once; Deep Cover's first flip has its line; empty states are one line and one button; no legend under Your voices
    sej = (web / "speakeasy.js").read_text(encoding="utf-8")
    assert "function syncGo" in osj and "Press the button again" not in osj and "const PERSON_WARN" in app and 'id="deep-first"' in ix, "the one-shot's one press, the warning line or the first-flip line is gone"
    assert "cadence ✓" not in sej and "Find clips + cadence" not in sej and "Speak the way" in ix and "tonight instead" not in ix.lower() and "Overnight queue" not in ix and "both open DoubleAgent" not in ix, "a retired word is back (cadence in basic, 'instead', the overnight queue)"
    assert "Nothing on file yet." in lib and 'class="empty"' in da and "No one to speak yet." in sej and "Nothing for tonight." in work, "an empty state lost its one line and one button"
    assert ".nodeep" in css and 'id="da-voices-say" hidden' in ix, "Your voices has its legend back, or Deep no longer replaces the word with the number"
    # qa18 (the consultant's four structural changes): the build path in the one-shot card, one Tonight, Storage in two groups, one sticky band on a phone
    rooms_js = (web / "rooms.js").read_text(encoding="utf-8")
    assert 'id="build"' in ix and "function syncBuild" in app and 'id="build-toggle"' in ix and ix.index('id="build"') > ix.index('id="oneshot"'), "the build path is no longer a section of the one-shot card"
    assert 'id="q-at-time"' not in ix and 'id="q-finish"' not in ix and 'id="q-start"' not in ix and 'id="qd-glance"' in ix and 'id="qd-list"' not in ix and "function jobSource" in work, "Tonight is edited in more than one place again, or the drawer is an editor again"
    assert 'class="frow"' in ix and "function stOpen" in rooms_js and 'id="st-make"' in ix and 'id="st-keep"' in ix, "Storage lost its two groups of rows"
    assert "Starts ${clock(w.start)}" in work and "starts at ${clock(w.settings.start_at)}" not in work, "Tonight's start must be the status's real start (qa19), not the saved setting"
    # qa20 (phase 26's page side): Improve's words on the A/B, its numbers in Deep, "nothing new to learn from" in the report
    assert "function measuresText" in work and "nothing_new" in work and "ab-words" in da and "measuresText(st.measures)" in da, "Improve's words or its Deep numbers are gone from the page"
    # RC pass 10: Train my own never searches for a display name (it asks whose voice it is)
    assert "const ownPerson" in da and "data-own-name" in da and "c.person || voiceDisplay(c.voice)" not in da, "Train my own would search YouTube for a voice's file name again"
    # IA pass 11 B3: the retrying state says the time once, waits for YouTube in the header, the strip and the drawer, and Recent says "waiting to retry"
    assert "function retryWait" in work and "const willRetry" in work and "Waiting for YouTube" in work and "trying again at" in work, "the retrying state lost its words"
    # rc-page3 (the user's fresh 1.0 install): a first run lands on FrontMan with the download said there, not on a stale tab
    osj = (web / "oneshot.js").read_text(encoding="utf-8")
    fb = osj[osj.index("async function firstBoot"):osj.index("function firstReady")]
    assert "s.first_run" in fb and 'showTab("frontman")' in fb and "function firstReady" in osj and 'id="first-ready"' in ix and "600 MB" not in ix and "600 MB" not in app and "const modelsSize" in app, "a fresh install no longer lands on FrontMan with the download said"
    # rc-page6 (the user: "remove the tutorial from 1.0"; MasterPlan: remove it, it waits on the tutorial-redesign branch): no First assignment left in the
    # page, and nothing calls its routes. What's new stays quiet for a copy with no covers.
    assert not (web / "tutorial.js").exists() and "tutorial.js" not in ix, "tutorial.js is back"
    pages = {f.name: f.read_text(encoding="utf-8") for f in web.iterdir() if f.suffix in (".js", ".html", ".css")}
    left = sorted(n for n, src in pages.items() if re.search(r"/api/tutorial|\.tutorial\b|assign-|assignBrief|ASSIGNMENT_ON|wb-fold|First assignment", src))
    assert not left, f"the First assignment is still referenced in {left}"
    # rc-page8 (the user, on the fresh install: "Hold On as Agent Smoke ... with no tutorial it's just random words"): the field is EMPTY, no suggestion
    # line. rc-page14 (the user): its placeholder is ALWAYS "Pink Pony Club as Plankton", on a first run and after; nothing in the page changes it
    assert 'id="os-text" autocomplete="off" placeholder="Pink Pony Club as Plankton"' in ix and "Hello by Adele as Peter Griffin\"" not in ix.split('id="os-ex"')[0], "the one-shot placeholder isn't Pink Pony Club as Plankton"
    assert not re.search(r'os-text"\)\.placeholder\s*=|f\.placeholder\s*=', osj), "something swaps the one-shot placeholder again"
    assert "Hold On as Agent Smoke" not in osj and "New here?" not in osj and ".value = FIRST" not in osj and "function firstGo" not in osj, "the first run fills in a suggestion again"
    assert "!hist.length) { store.set(WN_SEEN" in pages["whatsnew.js"], "What's new must stay quiet for a copy with no covers"
    v1 = json.loads((web / "whatsnew.json").read_text(encoding="utf-8"))["entries"][0]
    assert not any(w in l.lower() for l in v1["lines"] for w in ("assignment", "tutorial")), "What's new 1.0 mentions the tutorial"
    # rc-page10: setup installs every model, so the download line is a repair ("Downloading missing AI models"), and Deep Cover's engine line offers the
    # original Chatterbox as an add-on with its size (never a download from SpeakEasy); the Add-ons room calls an open add-on open source
    assert "Downloading missing AI models" in ix and "Getting UnderCover ready" not in ix and "once." not in ix.split('id="first-ready"')[1][:200] and "First start: downloading" not in app, "the models line still talks of a first start"
    se_js = (web / "speakeasy.js").read_text(encoding="utf-8")
    assert "async function seEngineLine" in se_js and 'id="se-engine-deep" data-deep' in ix and "#safehouse/addons" in se_js and "/install" not in se_js, "Deep Cover's engine line lost its Add-ons offer, or SpeakEasy installs something"
    assert "const openAddon" in rooms_js and "freeLicence(a.licence)" in rooms_js and '["installed", "Installed", a => addonReady(a)]' in rooms_js and "opt.filter(addonReady)" in rooms_js and '"open": a.open' in (ROOT / "undercover" / "core" / "addons.py").read_text(encoding="utf-8"), "the Add-ons room no longer knows an open add-on"
    # rc-page11 (the user's fresh install: "there is no ability to preview the song here"): the song on the desk has play / pause, a seekable waveform
    # redrawn when its canvas gets a size (it drew nothing while the build path was folded), and a key on a button inside the drop zone isn't the drop zone's
    assert 'id="song-play"' in ix and 'id="a-song"' in ix and 'id="wave-song"' in ix and "function drawSongWave" in app and "ResizeObserver(() => drawSongWave())" in app, "the song on the desk lost its play button or its waveform"
    assert "if (open) drawSongWave();" in app and ".setup > .panel { grid-template-columns: minmax(0, 1fr); }" in css, "the song's waveform isn't drawn when the build path opens, or the song card sticks out at phone width"
    assert "aSong.src = s.url" in app and "aSong.pause(); $(\"#play\")" in app and "e.target !== e.currentTarget) return; if (e.key" in app, "the song's player lost its source, its stop with the others, or the drop zone takes the play button's keys again"
    # rc-page11 (the user): SpeakEasy never starts talking by itself; when the first sentences are made the player is ready and says so. The
    # cadence note doesn't promise it starts playing. SafeHouse's examples: "Peter Griffin" on SpeakEasy's new-voice field
    se_js11 = (web / "speakeasy.js").read_text(encoding="utf-8")
    assert "SE.started = true; sePlayPart(0)" not in se_js11 and "starts playing" not in se_js11 and "ready to play: the rest is still being made" in se_js11 and '"Play: ready"' in se_js11, "SpeakEasy plays by itself again"
    assert 'id="se-newname" placeholder="Peter Griffin"' in ix, "SpeakEasy's name field lost its example"
    # rc-page12 (the user's install on D:): no copy assumes the app is on C: ("keep UnderCover on C: and its files somewhere roomier")
    assert not re.search(r"\bon C:|\bC: drive|drive C\b", ix), "the page assumes UnderCover is on C: again"
    # rc-page13 (the user's pick, after Internal Affairs proved them on a fresh library): FrontMan's three examples, in this order
    assert re.findall(r'<button type="button">([^<]+)</button>', ix[ix.index('id="os-ex"'):].split("</div>")[0]) == ["Pink Pony Club as Plankton", "Freek-a-Leek as Obama", "Hello by Adele as Peter Griffin"], "FrontMan's examples aren't the three the user picked"
    # rc-ui1 (the user's option A, the maintainers' mockup, meta-options): the line under a name is chips (facts()), in plain words, Deep's facts as data-deep chips;
    # FrontMan's two "Set it up yourself" cards open on a search field (icon, border, Search button), the song's above the chosen song
    assert "function facts(list)" in app and "const countWord" in app and "const sourceWord" in app and "function dayWord" in app and ".fact {" in css and ".fact.muted" in css, "the chips helper or their look is gone"
    lib_js = (web / "library.js").read_text(encoding="utf-8")
    rows = lib_js[lib_js.index('if (L.cat === "covers") {\n      title = x.title;'):lib_js.index("return `<div class=\"litem")]
    assert rows.count("facts(") >= 5 and '.join(" · ")' not in rows and "<small>${esc(sub)}</small>" not in lib_js, "a Library row is a dot line again"
    assert "cover${x.covers === 1" not in lib_js and "countWord(x.covers" in lib_js and "sourceWord(x.source)" in lib_js, "the Library says '0 covers' or 'dropped file' again"
    for f in ("doubleagent.js", "speakeasy.js", "work.js", "aside.js", "vault.js", "rooms.js"):
        assert "facts(" in (web / f).read_text(encoding="utf-8"), f"{f} lost its chips"
    assert 'demo voice · not a real person' not in app + lib_js + (web / "doubleagent.js").read_text(encoding="utf-8"), "a demo voice's dot line is back"
    song_card = ix[ix.index("<h2>1 · Song</h2>"):ix.index("<h2>2 · Voice</h2>")]
    voice_card = ix[ix.index("<h2>2 · Voice</h2>"):ix.index('id="voices"')]
    assert song_card.index('class="search sfield"') < song_card.index('id="drop"') and 'id="yt-go" type="button">Search</button>' in song_card, "the song card's search isn't at the top"
    assert 'class="search sfield"' in voice_card and 'id="q-go" type="button">Search</button>' in voice_card and voice_card.count('href="#g-search"') == 1 and song_card.count('href="#g-search"') == 1, "the voice card's search isn't a search field"
    assert '<symbol id="g-search"' in ix and ".sfield {" in css and '$("#q-go").onclick = voiceSearchNow' in app, "the search fields lost their icon, border or button"
    # Low profile (the user's mockup, approved 2026-10-24): the control in the top bar, hidden without the backend's status field; the numbers behind the
    # Deep "▾"; the strip's words; the game question's first answer; the hooks in ARCHITECTURE "UI"; a What's new entry
    lpj = (web / "lowprofile.js").read_text(encoding="utf-8")
    assert '<script src="/static/lowprofile.js">' in ix and 'id="lp-chip" hidden' in ix and 'id="lp-pop" hidden' in ix and 'id="gp-low" type="button" hidden' in ix and '<symbol id="g-lowprofile"' in ix, "the Low profile control lost its markup"
    assert 'id="lp-more"' in ix and 'data-deep>▾' in ix and 'id="set-lowprofile' not in ix, "the numbers must live behind the Deep ▾ only (no Settings row)"
    assert "renderLowProfile(s.low_profile, s)" in app and 'if (!lp) { LP.known = false; chip.hidden = true;' in lpj and '"/api/lowprofile"' in lpj and "const lowProfileOn" in lpj, "the page doesn't follow the status's low_profile"
    assert '["gp-low", "low"]' in app and "Low profile while you play?" in app and "lowProfileOn()" in work and "Working at a low profile" in work, "the game question or the strip lost Low profile"
    arch = (ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    assert "POST /api/lowprofile" in arch and "`low_profile`" in arch, "ARCHITECTURE's UI section doesn't carry the Low profile hooks"
    wn_lp = next((e for e in json.loads((web / "whatsnew.json").read_text(encoding="utf-8"))["entries"] if e["title"] == "Low profile"), None)   # below the release's entry (the top is the version's)
    assert wn_lp and all("Low profile" in l or "Deep Cover" in l for l in wn_lp["lines"]), "What's new doesn't say Low profile"
    # rc-page9 (IA pass 12, A3): What's new reaches friends; no internal names (the chats, phases, QA passes, branch names)
    inside = re.compile(r"Internal Affairs|MasterPlan|\bIA\b|\bQA\b|\bpass \d|\bphase \d|smallfix|\brc-|\bqa\d|handoff|sandbox|voice lab", re.I)
    leaks = [l for e in json.loads((web / "whatsnew.json").read_text(encoding="utf-8"))["entries"] for l in [e["title"], *e["lines"]] if inside.search(l)]
    assert not leaks, f"What's new names something internal: {leaks}"
    assert ".toggle > input[type=checkbox] {" in css and "\n.toggle input {" not in css, "the .toggle switch styling must not reach the time field inside the label"
    # qa13: the Preview panel sits above the convert card; Remove survives a redraw and Enter on it is its own (the row's key handler ate it)
    idx = (web / "index.html").read_text(encoding="utf-8")
    assert idx.index('id="preview"') < idx.index('id="summary"'), "the Preview panel goes above the summary / Convert card"
    assert 'if (e.target.closest(".remove, .vline")) return;' in app and "S.rmArm" in app and "rmArmed(v.name)" in app, "FrontMan's Remove lost its keyboard path or its armed state across a redraw"
    # IA pass 7: two Set aside / Clean up rows that read the same carry their folder name; nothing ticked never reads "Free 1 kB"
    asd = (web / "aside.js").read_text(encoding="utf-8")
    assert "same(x)" in asd and '"Nothing ticked"' in lib and "const same = n =>" in lib, "identical Set aside / Clean up rows lost their folder name, or the empty Clean up button its words"
    assert "waiting_beside" in lib and "data-lib-ab" in lib, "SafeHouse lost the nested retrained-voice line"
    assert "/api/speak/estimate" in (web / "speakeasy.js").read_text(encoding="utf-8"), "SpeakEasy lost the server's estimate before Generate"
    assert "replace-dlg" in (web / "index.html").read_text(encoding="utf-8") and "askReplace" in (web / "train.js").read_text(encoding="utf-8"), "Use this one lost its Replace / wait side by side question"
    assert "api/library/aside" in (web / "aside.js").read_text(encoding="utf-8") and "/static/aside.js" in (web / "index.html").read_text(encoding="utf-8"), "Storage lost its Set aside section"
    for f in ("app.js", "speakeasy.js", "train.js", "work.js", "library.js", "aside.js", "doubleagent.js"):   # IA P5-1: no two-click confirm without the delay
        src = (web / f).read_text(encoding="utf-8")
        assert f == "app.js" or "armedFor(" in src or "CONFIRM_MS" in src, f"{f} has a two-click confirm that a double click could confirm"
    assert "asideRestore" in (web / "aside.js").read_text(encoding="utf-8") and "removedVoice" in app, "Remove lost its Bring it back"
    assert "aside_size" in lib and "goes to Set aside" in lib, "Clean up lost the duplicate voices going to Set aside"
    assert ".hbtns { flex-wrap: wrap; }" in (web / "app.css").read_text(encoding="utf-8")
    assert ".se-rrow .row { grid-column: 2; flex-wrap: wrap; }" in (web / "speakeasy.css").read_text(encoding="utf-8")
    return "tutorial reset, Replay briefing, Compare sets, display names, phone rows"


@check("a fresh install's seeds (app/firstrun.py; the First assignment is removed for 1.0): decided once per install, the sample song and the demo voice seeded once (a deleted one isn't brought back), an existing install gets none; the demo voice is nobody to search for")
def u_firstrun():
    import numpy as np
    import soundfile as sf
    from undercover.app import firstrun
    from undercover.core import voices
    from undercover.core.voices import ModelError
    from undercover.doubleagent import cadence, night, service
    saved = (firstrun.SAMPLE, firstrun.DEMO)
    with isolated_data() as t:
        try:
            # no audio in the shipped folder: no sample song, nothing breaks
            empty = t / "assets" / "sample-none"
            empty.mkdir(parents=True)
            (empty / "sample.json").write_text(json.dumps({"title": "x", "file": "sample.flac"}), encoding="utf-8")
            firstrun.SAMPLE, firstrun.DEMO = empty, t / "assets" / "no-voice"
            assert firstrun.sample_info() is None and firstrun.seed() == {"song": None, "voice": None}
            # a fresh install: both seeds
            sample = t / "assets" / "sample"
            sample.mkdir(parents=True)
            sf.write(sample / "sample.wav", (0.1 * np.sin(np.linspace(0, 880 * np.pi, 44100 * 3))).astype(np.float32), 44100)
            (sample / "sample.json").write_text(json.dumps({"title": "The Drop", "artist": "UnderCover", "file": "sample.wav",
                                                            "seconds": 3, "licence": "ours", "notice": "n"}), encoding="utf-8")
            _fake_demo_voice(t / "assets" / "demo-voice" / "agent-smoke")
            firstrun.SAMPLE, firstrun.DEMO = sample, t / "assets" / "demo-voice" / "agent-smoke"
            assert firstrun.state_file().parent == t and firstrun.load() is None
            st = firstrun.first_run()
            assert st["install"] == "fresh" and st["seeded"]["song"] and st["seeded"]["voice"] == "agent-smoke", st
            sid = firstrun.sample_song_id()
            assert sid, "the sample song wasn't added"
            meta = json.loads((config_path("uploads_dir") / f"{sid}.json").read_text(encoding="utf-8"))
            assert meta["sample"] is True and meta["title"] == "The Drop", meta
            assert (sample / "sample.wav").exists(), "the shipped audio was moved"
            v = voices.get_voice("agent-smoke")
            info = voices.voice_info(v)
            assert info["demo"] is True and info["trained"] is False, info
            assert not [d for d in voices.voices_dir().iterdir() if d.name.startswith(".")], "a seeding folder was left"
            assert firstrun.first_run() == st and v.extra.get("demo_build")   # once; the shipped build recorded
            # the demo voice is nobody to search for
            c = next(x for x in night.candidates() if x["voice"] == "agent-smoke")
            assert c["can"] is False and c["demo"] is True and "not a real person" in c["note"], c
            assert cadence.can_train(v)[0] is False
            assert night._project_for_item({"kind": "improve", "target": "agent-smoke"}, create=True) is None
            from undercover.core import worklist
            try:
                worklist.add("improve", "agent-smoke")
                raise AssertionError("an improve of the demo voice was queued")
            except ModelError as e:
                assert "not a real person" in str(e), e
            plan = {}
            got = service.resolve_voice(None, "Agent Smoke", False, None, lambda *a, **k: None, plan)
            assert got.name == "agent-smoke" and plan["voice"]["checked"] == "the demo voice", plan
            assert [v.name for v in service.installed_for("Smoke")] == ["agent-smoke"]   # name endings match...
            real_ref = service.online_for
            service.online_for = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("looked for the real Smoke"))
            try:   # ...but a real artist called "Smoke" never gets the demo voice: it looks for them instead
                service.resolve_voice(None, "Smoke", False, None, lambda *a, **k: None, {})
                raise AssertionError("resolve_voice returned without looking for the real Smoke")
            except RuntimeError as e:
                assert "real Smoke" in str(e), e
            finally:
                service.online_for = real_ref
            # the user deleted both: the next start doesn't bring them back (decided once); seeding itself adds them once
            for f in config_path("uploads_dir").glob(f"{sid}.*"):
                f.unlink()
            voices.remove_voice("agent-smoke")
            assert firstrun.first_run() == st and firstrun.sample_song_id() is None
            assert not voices.voices_dir().joinpath("agent-smoke").exists(), "a deleted demo voice came back at the next start"
            r = firstrun.seed()
            assert r["song"] and r["voice"] == "agent-smoke", r
            assert firstrun.seed() == {"song": firstrun.sample_song_id(), "voice": "agent-smoke"}   # nothing doubled
            assert len([m for m in config_path("uploads_dir").glob("*.json")
                        if json.loads(m.read_text(encoding="utf-8")).get("sample")]) == 1
            # the user already has the very recording: it's marked as the sample, their title kept, nothing doubled
            for f in config_path("uploads_dir").glob(f"{firstrun.sample_song_id()}.*"):
                if f.suffix == ".json":
                    m = json.loads(f.read_text(encoding="utf-8"))
                    m.pop("sample")
                    m["title"] = "My take"
                    f.write_text(json.dumps(m), encoding="utf-8")
            assert firstrun.sample_song_id() is None
            sid2 = firstrun.seed_song()
            m = json.loads((config_path("uploads_dir") / f"{sid2}.json").read_text(encoding="utf-8"))
            assert m["sample"] is True and m["title"] == "My take", m
            assert not list(config_path("uploads_dir").glob("upload_sample_*")), "a temp copy was left"
            # the shipped files must match their manifest (they aren't in git): a changed or missing one isn't seeded
            voices.remove_voice("agent-smoke")
            (firstrun.DEMO / "model.pth").write_bytes(b"something else entirely")
            assert "manifest" in (firstrun.demo_files() or "") and firstrun.seed_voice() is None
            (firstrun.DEMO / "model.pth").unlink()
            assert "make_demo_voice" in (firstrun.demo_files() or "") and firstrun.seed_voice() is None
            (firstrun.DEMO / "model.pth").write_bytes(b"not really a model")
            assert firstrun.demo_files() is None and firstrun.seed_voice() == "agent-smoke"
            # a folder of that name that isn't the demo is never touched
            voices.remove_voice("agent-smoke")
            other = voices.voices_dir() / "agent-smoke"
            other.mkdir()
            (other / "mine.txt").write_text("the user's", encoding="utf-8")
            assert firstrun.seed_voice() is None and (other / "mine.txt").exists()
        finally:
            firstrun.SAMPLE, firstrun.DEMO = saved
    with isolated_data():   # an install that already made covers / trained voices: marked, nothing seeded
        _fake_voice("someone-uc", extra={"trained": True, "person": "Someone"})
        assert firstrun.first_run()["install"] == "existing install" and firstrun.sample_song_id() is None
        assert not voices.voices_dir().joinpath("agent-smoke").exists()
    with isolated_data():   # an install from before 1.0 (the old {done} state): decided already, nothing seeded
        firstrun.state_file().write_text(json.dumps({"done": None, "since": "2026-10-13"}), encoding="utf-8")
        assert firstrun.first_run() == {"done": None, "since": "2026-10-13"} and firstrun.sample_song_id() is None
    import inspect
    from undercover.doubleagent import api as da_api   # "Do it all" called a local function named like the module it uses
    assert "def service(" not in inspect.getsource(da_api.start_auto), "start_auto shadows the service module again"
    why = firstrun.demo_files()   # the real shipped demo voice: its files are made by scripts/make_demo_voice.py, not kept in git
    return ("fresh: both seeds, once (a deleted one stays deleted); existing install and an old state: none; manifest checked; never searched for; "
            + ("the shipped demo voice matches its manifest" if why is None else f"NOTE: {why}"))


# ---------------------------------------------------------------- the platform helper and the tray (phase 18)

WINDOWS_ONLY = ("ctypes.windll", "WinDLL(", "os.startfile", '"netsh"', '"powershell"', '"explorer"',
                "SetThreadExecutionState", "GetModuleFileNameW", "SetConsoleCtrlHandler", "TerminateProcess(")


@check("platform helper: every Windows-only call lives in core/platform.py; the helper works here and says 'not on this OS' elsewhere")
def u_platform():
    import subprocess as sp
    import tempfile
    from undercover.core import platform
    bad = []
    for f in (ROOT / "undercover").rglob("*.py"):
        if f.name == "platform.py" and f.parent.name == "core":
            continue
        text = f.read_text(encoding="utf-8")
        bad += [f"{f.relative_to(ROOT).as_posix()}: {w}" for w in WINDOWS_ONLY if w in text]
    assert not bad, f"Windows-only calls outside core/platform.py: {bad}"
    if platform.WINDOWS:
        assert platform.supports("startup") == (True, "") and platform.drives() and platform.own_executable().endswith(".exe")
        assert platform.keep_awake(False) is True
        assert any(r.get("Name") for r in platform.video_controllers())
    # one per user: a second process can't claim the name while this one holds it
    name = f"UnderCover-smoke-{os.getpid()}"
    probe = (f"import sys; sys.path.insert(0, {str(ROOT)!r}); from undercover.core import platform; "
             f"print(platform.single_instance({name!r}))")
    assert platform.single_instance(name)
    out = sp.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=60).stdout.strip()
    assert out == "False", f"a second process claimed it: {out!r}"
    platform.release_instance(name)
    out = sp.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=60).stdout.strip()
    assert out == "True", f"released, but a second process still couldn't: {out!r}"
    # Start with Windows: only ever in a folder of the test's own
    if platform.WINDOWS:
        with tempfile.TemporaryDirectory(prefix="uc-startup-") as d:
            target = ROOT / "scripts" / "UnderCover.bat"
            assert not platform.startup_entry_state(d)
            assert platform.startup_entry(True, target, folder=d) and platform.startup_entry_state(d)
            assert not platform.startup_entry(False, target, folder=d) and not platform.startup_entry_state(d)
    # another OS: the plain answers
    saved = platform.WINDOWS
    platform.WINDOWS = False
    try:
        assert platform.supports("firewall")[0] is False and platform.supports("tray")[0] is False
        assert platform.keep_awake(True) is False
        assert platform.supports("fullscreen")[0] is False
        for fn, args in ((platform.startup_folder, ()), (platform.network_and_firewall, ("192.168.1.5",)),
                         (platform.video_controllers, ()), (platform.fullscreen_app, ())):
            try:
                fn(*args)
                raise AssertionError(f"{fn.__name__} ran as if on Windows")
            except platform.NotOnThisOS as e:
                assert "Windows only" in str(e), e
        assert platform.own_executable() == sys.executable
        try:   # no file manager opener: said plainly (and the job system passes it on as written)
            platform.open_folder(ROOT)
            raise AssertionError("open_folder ran without a way to open folders")
        except platform.NotOnThisOS as e:
            from undercover.core import jobs as _jobs
            assert _jobs.friendly(e) == str(e) and "xdg-open" in str(e), e
        from undercover.core import doctor, phone
        real = (phone.enabled, phone.lan_address)
        phone.enabled, phone.lan_address = (lambda: True), (lambda: "192.168.1.5")
        try:
            rows = doctor.c_phone({})
            assert [r["state"] for r in rows] == ["info"] and "Windows only" in rows[0]["say"], rows
        finally:
            phone.enabled, phone.lan_address = real
    finally:
        platform.WINDOWS = saved
    return "no Windows-only calls elsewhere; one instance; the startup shortcut in a temp folder; the other-OS answers"


@check("tray: the menu and the icon from what the server says (stopped, idle, working, paused, a problem); no torch")
def u_tray():
    import subprocess as sp
    from undercover.core import trayicon as tray
    assert tray.icon_state(None) == ("stopped", None) and tray.tooltip(None) == "UnderCover isn't running"
    assert [i["id"] for i in tray.menu_model(None) if i["id"] != "-"] == ["start", "close"]
    idle = {"work": {"held": False, "resume": []}, "gpu_problem": None}
    assert tray.icon_state(idle, []) == ("idle", None) and tray.tooltip(idle, []) == "UnderCover · ready"
    m = tray.menu_model(idle, [], phone_on=False, startup_on=True)
    ids = [i["id"] for i in m if i["id"] != "-"]
    assert ids == ["open", "running", "phone", "restart", "quit", "startup"], ids
    assert m[0]["default"] and next(i for i in m if i["id"] == "phone")["checked"] is False
    asking = {"asking": {"exe": "eldenring.exe", "title": "ELDEN RING", "since": 1.0}}
    g = [i["id"] for i in tray.menu_model(idle, [], game=asking) if i["id"] != "-"]
    assert g[:5] == ["game-q", "game-pause", "game-both", "game-never", "open"], g
    assert tray.game_question(asking).startswith("ELDEN RING is full-screen") and tray.game_question({"asking": None}) is None
    assert next(i for i in m if i["id"] == "startup")["checked"] is True
    cover = {"id": "c1", "kind": "cover", "state": "running", "title": "Cover: Hello as Agent Smoke", "total_left": 130}
    train = {"id": "t1", "kind": "train", "state": "running", "title": "Training Morgan Freeman", "epoch": 120,
             "epochs": 400, "total_left": 1800}
    assert tray.icon_state(idle, [cover]) == ("working", "frontman")
    assert tray.icon_state(idle, [train, cover]) == ("working", "doubleagent")
    assert tray.job_line(train) == "Training Morgan Freeman · epoch 120 of 400 · about 30 min left"
    assert tray.tooltip(idle, [cover, train]).startswith("UnderCover · Cover: Hello as Agent Smoke · about 2 min left (+1 more)")
    m = tray.menu_model(idle, [train, cover])
    assert [i["text"] for i in m if i["id"] == "running"] == [tray.job_line(train), tray.job_line(cover)]
    assert any(i["id"] == "pause" for i in m) and not any(i["id"] == "resume" for i in m)
    held = {"work": {"held": True, "resume": [{"kind": "train"}]}}
    assert any(i["id"] == "resume" for i in tray.menu_model(held, []))
    assert tray.icon_state({"gpu_problem": "no GPU"}, [cover]) == ("problem", None)
    assert tray.icon_state(idle, [], doctor_bad=True)[0] == "problem"
    finished = {**cover, "state": "done", "final": {"state": "done"}}
    assert tray.icon_state(idle, [finished]) == ("idle", None)
    # the icon leaves with UnderCover after any stop on purpose, stays through a restart or a crash
    assert [tray.leaves_with_server(r) for r in ("quit", "window closed", "", "log off", "restart", None)] == \
        [True, True, True, True, False, False]
    imgs = [tray.draw_icon(st, mod).tobytes() for st, mod in (("idle", None), ("working", "frontman"), ("problem", None),
                                                              ("stopped", None))]
    assert len(set(imgs)) == 4 and tray.draw_icon("idle", size=16).size == (16, 16)
    out = sp.run([sys.executable, "-c", f"import sys; sys.path.insert(0, {str(ROOT)!r}); import undercover.core.trayicon; "
                  "print('torch' in sys.modules, 'undercover.core.jobs' in sys.modules)"],
                 capture_output=True, text=True, timeout=120).stdout.strip()
    assert out == "False False", f"the tray loads heavy modules: {out}"
    return "stopped / idle / working (the module's bar) / paused / problem; job lines; icons differ; light import"


# every route the server answers, as (method, an example path): the walk below and a_phone go through all of them
PHONE_SAMPLES = [
    ("GET", "/"), ("GET", "/index.html"), ("GET", "/static/app.js"), ("GET", "/static/manifest.webmanifest"),
    ("GET", "/static/icon-192.png"), ("GET", "/static/icon-512.png"), ("GET", "/files/output/x.mp3"), ("GET", "/pair"),
    ("POST", "/api/phone/claim"), ("GET", "/api/status"), ("GET", "/api/voices"), ("GET", "/api/search"), ("GET", "/api/jobs"),
    ("GET", "/api/work"), ("GET", "/api/jobs/abc123/events"), ("POST", "/api/jobs/abc123/cancel"), ("POST", "/api/jobs/abc123/retry"),
    *[("POST", f"/api/work/{a}") for a in ("pause", "resume", "forget", "add", "remove", "order", "start", "settings", "dismiss")],
    ("POST", "/api/voices/install"), ("POST", "/api/voices/upload"), ("DELETE", "/api/voices/some-voice"),
    ("POST", "/api/open-folder"), ("POST", "/api/restart"), ("POST", "/api/quit"),
    ("GET", "/api/ear"), ("POST", "/api/ear/rate"), ("POST", "/api/ear/forget"),
    ("POST", "/api/history/0123456789/again"), ("POST", "/api/history/0123456789/octaves"),
    ("GET", "/api/config"), ("GET", "/api/history"), ("GET", "/api/songs/search"), ("GET", "/api/auto/plan"),
    ("GET", "/api/songs/0123456789abcdef"), ("GET", "/api/songs/0123456789abcdef/key"),
    *[("POST", p) for p in ("/api/upload", "/api/estimate", "/api/covers", "/api/preview", "/api/songs/youtube", "/api/auto",
                            "/api/songs/0123456789abcdef/presep")],
    ("GET", "/api/work/candidates"), ("GET", "/api/work/lookup"), ("GET", "/api/train/projects"), ("POST", "/api/train/projects"),
    ("GET", "/api/train/projects/someone"), ("DELETE", "/api/train/projects/someone"),
    *[("POST", f"/api/train/projects/someone/{a}") for a in ("search", "sources", "upload", "prepare", "reference", "phrases",
                                                             "train", "pause", "stop", "export", "auto", "transcribe")],
    ("GET", "/api/train/voices/v-uc/cadence"), ("GET", "/api/train/voices/v-uc/cadence/sample/current"),
    ("GET", "/api/train/voices/v-uc/cadence/sample/pending"), ("POST", "/api/train/voices/v-uc/cadence/ab"),
    ("POST", "/api/train/voices/v-uc/cadence/verdict"),
    *[("GET", f"/api/speak/{a}") for a in ("voices", "readers", "recent", "plan", "estimate")],
    ("POST", "/api/speak"), ("POST", "/api/speak/estimate"), ("POST", "/api/speak/cadence"), ("POST", "/api/speak/newvoice"),
    *[("POST", f"/api/speak/0123456789/{a}") for a in ("rename", "keep", "unkeep")], ("DELETE", "/api/speak/0123456789"),
    *[("GET", f"/api/library/{a}") for a in ("summary", "covers", "speech", "songs", "voices", "training")],
    ("GET", "/api/library/covers/zip"),
    *[("POST", f"/api/library/{k}/0123456789/{a}") for k in ("covers", "speech") for a in ("rename", "keep", "unkeep")],
    ("DELETE", "/api/library/covers/0123456789"), ("DELETE", "/api/library/speech/0123456789"),
    ("DELETE", "/api/library/songs/0123456789abcdef"), ("POST", "/api/library/voices/v-uc/display"),
    ("POST", "/api/library/voices/v-uc/rename"), ("GET", "/api/library/voices/v-uc/export"),
    ("DELETE", "/api/library/voices/v-uc/cadence"), ("GET", "/api/library/cleanup"), ("POST", "/api/library/cleanup"),
    ("GET", "/api/library/aside"), ("DELETE", "/api/library/aside/morgan-20261014-120000"),
    ("POST", "/api/library/aside/morgan-20261014-120000/restore"),
    ("POST", "/api/library/tools/ytdlp-update"), ("POST", "/api/library/tools/ytdlp-auto"), ("GET", "/api/addons"),
    ("GET", "/api/storage"),
    *[("POST", f"/api/addons/f5-tts/{a}") for a in ("install", "remove", "use")],
    ("POST", "/api/storage/check"), ("POST", "/api/storage/move"),
    ("GET", "/api/doctor"), ("POST", "/api/doctor/full"), ("POST", "/api/doctor/fix/stale"), ("GET", "/api/doctor/bundle"),
    ("GET", "/api/doctor/bundle.zip"), ("POST", "/api/doctor/open-logs"),
    ("GET", "/api/vault"), ("GET", "/api/vault/check"), ("GET", "/api/vault/contents"), ("POST", "/api/vault/settings"),
    ("GET", "/api/gamepause"), ("POST", "/api/gamepause"), ("POST", "/api/gamepause/settings"), ("POST", "/api/lowprofile"),
    ("POST", "/api/vault/run"), ("POST", "/api/vault/restore"),
    ("GET", "/api/phone"), ("POST", "/api/phone"), ("POST", "/api/phone/pair"), ("POST", "/api/phone/pair/cancel"),
    ("POST", "/api/phone/revoke"), ("GET", "/api/away"), ("POST", "/api/away"), ("POST", "/api/away/forget"),
    ("POST", "/api/phone/away/link"), ("POST", "/api/phone/away"), ("GET", "/away"),
    ("GET", "/api/themes"), ("POST", "/api/themes"), ("POST", "/api/themes/import"), ("GET", "/api/themes/midnight/export"),
    ("GET", "/api/themes/midnight/theme.css"), ("GET", "/api/themes/midnight/preview.png"),
    ("GET", "/api/themes/midnight/fonts/Manrope.woff2"), ("DELETE", "/api/themes/midnight"),
    ("GET", "/api/library/smaller"), ("POST", "/api/library/smaller/start"), ("POST", "/api/library/smaller/keep"),
    ("POST", "/api/library/smaller/undo"),
    ("POST", "/api/library/voices/some-voice/full-index"),
    ("POST", "/api/work/to-queue"),
    ("POST", "/api/work/estimate"),
    ("GET", "/api/train/voices/some-voice/improved"), ("POST", "/api/train/voices/some-voice/improved/keep"),
]


@check("phone: the route table covers every route (each /api path in the code and the pages has a row and an example)")
def u_phone_routes():
    import re
    from undercover.app import phone_api
    unlisted = [f"{m} {p}" for m, p in PHONE_SAMPLES if phone_api.policy(m, p)[1] == "not in the phone's list"]
    assert not unlisted, f"examples the table doesn't list: {unlisted}"
    unused = [f"{m} {rx}" for m, rx, _, _ in phone_api.ROUTES
              if not any(mm in m.split("|") and re.fullmatch(rx, p) for mm, p in PHONE_SAMPLES)]
    assert not unused, f"table rows with no example here: {unused}"
    # every literal /api/... path in the server code and the pages starts at least one example (a new route needs a row)
    lits = set()
    for f in [*(ROOT / "undercover").rglob("*.py"), *(ROOT / "undercover" / "web").glob("*.js"), *(ROOT / "undercover" / "web").glob("*.html")]:
        for lit in re.findall(r"""["'`](/api/[A-Za-z0-9_./-]*)""", f.read_text(encoding="utf-8")):
            lits.add(lit.rstrip("/."))
    paths = [p for _, p in PHONE_SAMPLES]
    missing = sorted(lit for lit in lits if not any(p.startswith(lit) for p in paths))
    assert not missing, f"/api paths in the code with no example (add a row to app/phone_api.ROUTES and PHONE_SAMPLES): {missing}"
    pc = sum(phone_api.policy(m, p)[0] == "pc" for m, p in PHONE_SAMPLES)
    return f"{len(phone_api.ROUTES)} rows, {len(PHONE_SAMPLES)} examples ({pc} PC-only), {len(lits)} paths in the code"


@check("api: the first run's one-shot, for real (Q's Make it): 'Hold On as Agent Smoke' plans and makes a cover of the library's sample song in the demo voice, with nothing searched or downloaded")
def a_desk_oneshot():
    from undercover.app import firstrun
    if firstrun.sample_info() is None or firstrun.demo_files():
        return "skipped: the sample song or the demo voice isn't shipped in this checkout"
    seeded = firstrun.seed()   # into the sandbox's folders (this process points at them)
    assert seeded["song"] and seeded["voice"] == "agent-smoke", seeded
    o = api("/api/auto/plan?text=" + urllib.parse.quote("Hold On as Agent Smoke"))
    assert o["ask"] is False and not o.get("voice_ask") and o["voice"]["how"] == "installed", o
    assert o["from_library"]["id"] == seeded["song"], o
    before = {c["id"] for c in api("/api/library/covers")}
    r = api("/api/auto", {"text": "Hold On as Agent Smoke"})
    req = urllib.request.Request(BASE + f"/api/jobs/{r['job']}/events")
    final, said, plan = None, [], {}
    with urllib.request.urlopen(req, timeout=900) as resp:
        for raw in resp:
            line = raw.decode("utf-8").strip()
            if not line.startswith("data: "):
                continue
            ev = json.loads(line[6:])
            plan = ev.get("plan") or plan
            if ev.get("message"):
                said.append(ev["message"])
            if ev.get("final"):
                final = ev
                break
    assert final and final["state"] == "done", f"the one-shot ended {final and final.get('state')}: {final and final.get('message')}"
    assert not [m for m in said if "YouTube" in m or "Downloading" in m or "Looking for" in m], said
    assert (plan.get("song") or {}).get("id") == seeded["song"] and (plan.get("voice") or {}).get("name") == "agent-smoke", plan
    new = [c for c in api("/api/library/covers") if c["id"] not in before]
    assert new, "no new cover"
    return f"plan and one-shot from the desk: a cover of the sample song in Agent Smoke ({len(said)} messages, none searched)"


@check("api: the Doctor (fast report, the UnderCover row, the diagnostics file's contents and the file itself)")
def a_doctor():
    import io
    import zipfile
    def report():   # the server keeps a fast report 5 s: a retry waits it out
        if report.n:
            time.sleep(5.2)
        report.n += 1
        rep = api("/api/doctor")
        return rep["seconds"], rep
    report.n = 0
    secs, tried = best_time(report, 2)
    r = tried[-1][1]
    assert r["about"]["version"] and r["results"] and r["mode"] == "fast", r.get("mode")
    assert secs < 2, f"the server's fast check took {', '.join(f'{t:.2f}' for t, _ in tried)} s in {len(tried)} tries"
    assert all(x["state"] in ("ok", "info", "warn", "bad") for x in r["results"])
    pv = api("/api/doctor/fix/packages")   # read only: installs nothing
    assert isinstance(pv.get("packages"), list) and isinstance(pv.get("refused"), list), pv
    c = api("/api/doctor/bundle")
    assert c["files"] and c["never"], c
    z = zipfile.ZipFile(io.BytesIO(api("/api/doctor/bundle.zip")))
    text = "\n".join(z.read(n).decode("utf-8") for n in z.namelist())
    assert str(ROOT).lower() not in text.lower() and str(Path.home()).lower() not in text.lower(), "a full path is in the file"
    return f"{len(r['results'])} checks in {r['seconds']} s, {r['counts']['bad']} bad; bundle {len(z.namelist())} files"


@check("api: Retry starts a failed job again from the same request (a voice install from a dead address, twice)")
def a_retry():
    def final(jid):
        return wait_for(lambda: next((j for j in api("/api/jobs") if j["id"] == jid and j.get("final")), None), 90,
                        every=1, what="the job to end")
    j1 = api("/api/voices/install", {"url": "http://127.0.0.1:9/smoke-retry.zip", "name": "smoke-retry"})["job"]
    s1 = final(j1)
    assert s1["state"] == "error" and s1["retry"] is True, s1
    r = api(f"/api/jobs/{j1}/retry", {})
    assert r["job"] != j1 and r["was"] == j1, r
    s2 = final(r["job"])
    assert s2["state"] == "error" and s2["retry"] is True, s2
    assert "smoke-retry" not in [v["name"] for v in api("/api/voices")]
    try:
        api(f"/api/jobs/{api('/api/jobs')[0]['id']}x/retry", {})
        raise AssertionError("an unknown job was retried")
    except urllib.error.HTTPError as e:
        assert e.code == 404
    return "failed, retried as a new job, failed again; nothing installed"


PHONE_ADDR = "127.0.0.2"   # the sandbox's "home network" address: loopback, so nothing reaches the network and Windows asks nothing


def _lan(method, path, token=None, body=None, origin=True, host=None):
    """A request as a phone would send it, to the sandbox's phone listener: -> (status, headers, body)."""
    import http.client
    port = int(BASE.rsplit(":", 1)[1])
    c = http.client.HTTPConnection(PHONE_ADDR, port, timeout=30)
    h = {"Host": host or f"{PHONE_ADDR}:{port}"}
    if origin and method != "GET":
        h["Origin"] = origin if isinstance(origin, str) else f"http://{PHONE_ADDR}:{port}"
    if token:
        h["Cookie"] = f"other=1; uc_device={token}"
    data = None if body is None else json.dumps(body).encode()
    if data is not None:
        h["Content-Type"] = "application/json"
    try:
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        return r.status, {k.lower(): v for k, v in r.getheaders()}, r.read()
    finally:
        c.close()


def _away(method, path, port, host, cookie=None, body=None, origin=True):
    """A request as Tailscale Funnel would bring it to the away listener: -> (status, headers, body)."""
    import http.client
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    h = {"Host": host}
    if origin and method != "GET":
        h["Origin"] = origin if isinstance(origin, str) else f"https://{host}"
    if cookie:
        h["Cookie"] = cookie
    data = None if body is None else json.dumps(body).encode()
    if data is not None:
        h["Content-Type"] = "application/json"
    try:
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        return r.status, {k.lower(): v for k, v in r.getheaders()}, r.read()
    finally:
        c.close()


@check("api: away from home (phase 20) on a fake Tailscale: sign-in, approval, ready; Funnel only to the away listener; its gate (Host, Origin, no pairing, away tokens only); a home phone's link becomes its away pass; off closes it")
def a_away():
    st_file = Path(os.environ["FAKE_TS_STATE"])
    st_file.write_text("{}", encoding="utf-8")
    read = lambda: json.loads(st_file.read_text(encoding="utf-8") or "{}")
    write = lambda **kw: _fake_ts_set(st_file, **kw)
    api("/api/phone", {"on": True})
    pr = api("/api/phone/pair", {})["pairing"]
    status, hd, _ = _lan("POST", "/api/phone/claim", body={"code": pr["code"], "name": "Away phone"})
    assert status == 200
    home = hd["set-cookie"].split("uc_device=", 1)[1].split(";", 1)[0]
    try:
        st = api("/api/away", {"on": True})
        port = st["port"]
        assert st["on"] and st["listening"] and 7870 <= port < 7880, st
        wait_for(lambda: api("/api/away")["auth_url"], 60, every=0.5, what="the Tailscale sign-in link")
        assert api("/api/away")["phase"] == "sign-in"
        write(backend="Running", signed_in=True, host="uc-fakeaway.tailfake.ts.net")
        wait_for(lambda: api("/api/away")["approve_url"], 60, every=0.5, what="the approval link")
        write(funnel_approved=True)
        st = wait_for(lambda: (lambda x: x if x["phase"] == "ready" else None)(api("/api/away")), 60, every=0.5, what="ready")
        host = st["host"]
        assert st["link"] == f"https://{host}", st
        sandbox_port = int(BASE.rsplit(":", 1)[1])
        assert read()["funnel_target"] == f"http://127.0.0.1:{port}" and port != sandbox_port, read()   # never 7860, never the home listener
        # the gate
        assert _away("GET", "/", port, host)[0] == 401, "the away address answered without a pass"
        assert _away("GET", "/", port, f"127.0.0.1:{port}")[0] == 403, "the away address took a Host that isn't its name"
        assert _away("GET", "/pair", port, host)[0] == 403 and _away("POST", "/api/phone/claim", port, host, body={"code": "X"})[0] == 403
        status, _, page = _away("GET", "/away", port, host)
        assert status == 200 and b"Away from home" in page
        # a paired phone, at home, asks for its link (its button shows only now); at the away address the code becomes its pass
        assert json.loads(_lan("GET", "/api/status", home)[2])["away_ready"] is True
        assert api("/api/status").get("away_ready") is False, "the PC's own page offers the phone's away button"
        status, _, body = _lan("POST", "/api/phone/away/link", home, {})
        assert status == 200, body
        url = json.loads(body)["url"]
        assert url.startswith(f"https://{host}/away#"), url
        code = url.split("#", 1)[1]
        assert _away("POST", "/api/phone/away", port, host, body={"code": code}, origin=f"http://{host}")[0] == 403
        status, hd, body = _away("POST", "/api/phone/away", port, host, body={"code": code})
        assert status == 200, body
        cookie = hd.get("set-cookie", "")
        assert all(x in cookie for x in ("uc_away=", "Secure", "HttpOnly", "SameSite=Strict")), cookie
        away = cookie.split("uc_away=", 1)[1].split(";", 1)[0]
        assert _away("POST", "/api/phone/away", port, host, body={"code": code})[0] == 403, "a code worked twice"
        s_ = json.loads(_away("GET", "/api/status", port, host, cookie=f"uc_away={away}")[2])
        assert s_["client"] == "phone" and s_["device"] == "Away phone", s_
        assert _away("GET", "/api/status", port, host, cookie=f"uc_away={home}")[0] == 401, "the home token works away"
        assert _lan("GET", "/api/status", away)[0] == 401, "the away pass works at home"
        status, _, body = _away("POST", "/api/restart", port, host, cookie=f"uc_away={away}", body={})
        assert status == 403 and b"needs the PC" in body, (status, body)
        # off: nothing reaches this PC from outside any more
        api("/api/away", {"on": False})
        calls = read()["calls"]
        assert ["funnel", "reset"] in calls and ["down"] in calls and not read().get("funnel_on")
        assert json.loads(_lan("GET", "/api/status", home)[2])["away_ready"] is False
        try:
            _away("GET", "/", port, host)
            raise AssertionError("the away listener still answers after Off")
        except ConnectionRefusedError:
            pass
        # on again (the sign-in is kept: no new sign-in), then Phone access off takes away from home with it
        st = api("/api/away", {"on": True})
        port = st["port"]
        wait_for(lambda: api("/api/away")["phase"] == "ready", 60, every=0.5, what="ready again")
        resets = read()["calls"].count(["funnel", "reset"])
        api("/api/phone", {"on": False})
        st = api("/api/away")
        assert not st["on"] and not st["listening"] and read()["calls"].count(["funnel", "reset"]) == resets + 1, st
        try:
            _away("GET", "/", port, host)
            raise AssertionError("the away listener still answers after Phone access was switched off")
        except ConnectionRefusedError:
            pass
        # Forget with away off: the daemon comes up just for the logout, then goes, and the node's keys with it
        daemons = sum(c[0] == "tailscaled" for c in read()["calls"])
        api("/api/phone", {"on": True})
        st = api("/api/away/forget", {})
        r = read()
        assert r.get("logged_out") and sum(c[0] == "tailscaled" for c in r["calls"]) == daemons + 1 and not st["name"], (st, r["calls"][-4:])
    finally:
        api("/api/phone", {"on": True})
        api("/api/away", {"on": False})
        dev = next((d for d in api("/api/phone")["devices"] if d["name"] == "Away phone"), None)
        if dev:
            api("/api/phone/revoke", {"id": dev["id"]})
        api("/api/phone", {"on": False})
    return f"sign-in -> approve -> ready; funnel to {port} only; the gate holds; a home link became an away pass; off and Phone access off close it"


@check("api: Stop is a stop: a long separation (as in a training's clip preparation) ends within 2 s of Stop, nothing cached")
def a_stop_separation():
    import threading
    import numpy as np
    import soundfile as sf
    from undercover.core import gpu
    from undercover.core.config import path as config_path
    from undercover.core.separate import Stopped, separate
    f = config_path("uploads_dir") / "stop-test.wav"
    f.parent.mkdir(parents=True, exist_ok=True)
    t = np.arange(44100 * 240) / 44100.0                         # 4 minutes: a separation of tens of seconds
    y = 0.3 * np.sin(2 * np.pi * 220 * t) * (1 + np.sin(2 * np.pi * 0.5 * t)) + 0.05 * np.random.default_rng(0).standard_normal(len(t))
    sf.write(str(f), np.stack([y, y], 1).astype(np.float32), 44100)
    flag = {"stop": False, "at": None}
    def working(frac, step):   # Stop 1 s after the separation is really under way (models loaded, chunks running)
        if frac > 0 and flag["at"] is None:
            flag["at"] = -1.0
            threading.Timer(1.0, lambda: flag.update(stop=True, at=time.perf_counter())).start()
    t0 = time.perf_counter()
    try:
        separate(f, dereverb=True, karaoke=False, separator=gpu.MODELS.separator, quality="high", stop=lambda: flag["stop"],
                 progress=working)
        raise AssertionError("the separation finished before Stop (make the test file longer)")
    except Stopped:
        ended = time.perf_counter()
    lag = ended - flag["at"]
    assert lag < 2.0, f"Stop took {lag:.1f} s"
    from undercover.core.separate import file_hash
    stems = config_path("cache_dir") / "stems" / file_hash(f)
    assert not any(stems.rglob("*.flac")) if stems.exists() else True, "a stopped separation cached a stem"
    f.unlink(missing_ok=True)
    return f"stopped {lag:.2f} s after Stop (ran {flag['at'] - t0:.1f} s first)"


@check("api: a song's preparation says what it's doing (Separating the vocal / Removing reverb, step n of m, time left), and a preview queued behind it says \"Preparing the song first (n of m)\" with waits_for, never a bare \"starting\"")
def a_presep_progress():
    import io
    import numpy as np
    import soundfile as sf
    t = np.arange(44100 * 90) / 44100.0
    y = 0.3 * np.sin(2 * np.pi * 196 * t) * (1 + np.sin(2 * np.pi * 0.3 * t)) + 0.03 * np.random.default_rng(5).standard_normal(len(t))
    buf = io.BytesIO()
    sf.write(buf, np.stack([y, y], 1).astype(np.float32), 44100, format="WAV")
    req = urllib.request.Request(BASE + "/api/upload", data=buf.getvalue(), method="POST",
                                 headers={"X-Filename": "presep-test.wav", "Origin": BASE, "Content-Type": "application/octet-stream"})
    with urllib.request.urlopen(req, timeout=120) as r:
        song = json.loads(r.read())
    sid = song.get("id") or song.get("song", {}).get("id")
    try:
        wait_for(lambda: not any(j["state"] in ("queued", "running") for j in api("/api/jobs")), 300, every=2, what="an idle GPU")
        r = api(f"/api/songs/{sid}/presep", {"options": {"quality": "high", "dereverb": True}})
        assert r.get("started"), r
        pre = r["job"]
        voice = next(v["name"] for v in api("/api/voices") if not v.get("demo"))
        prev = api("/api/preview", {"song": sid, "voice": voice, "variants": [{"label": "A", "options": {}}]})["job"]
        seen = {"pre": None, "prev": None}

        def look():
            js = {j["id"]: j for j in api("/api/jobs")}
            p, q = js.get(pre), js.get(prev)
            if p and p.get("stage") == "separating" and p.get("steps"):
                seen["pre"] = p
            if q and q.get("stage") == "preparing":
                seen["prev"] = q
            return seen["pre"] and seen["prev"] and seen["prev"].get("step_n")
        wait_for(look, 300, every=0.5, what="both jobs reporting")
        p, q = seen["pre"], seen["prev"]
        assert p["steps"] == 2 and p["message"] in ("Separating the vocal", "Removing reverb"), p
        assert q["waits_for"] == pre and q["message"].startswith("Preparing the song first (") and "starting" not in q["message"].lower(), q
        api(f"/api/jobs/{prev}/cancel", {})
        wait_for(lambda: next((j for j in api("/api/jobs") if j["id"] == pre), {}).get("state") in ("done", "error", "cancelled"),
                 600, every=2, what="the preparation finished")
    finally:
        try:
            api(f"/api/library/songs/{sid}", method="DELETE")
        except Exception:
            pass
    return f"presep: {p['message']} (step {p['step_n']} of {p['steps']}); the preview: {q['message']}"


@check("api: Phone access (off = no listener; every route refused without a token; pairing; PC-only routes refused for a "
       "phone; Revoke; switching off closes it; this PC's own requests unchanged)")
def a_phone():
    from undercover.app import phone_api
    from undercover.core import phone
    unpaired = b"This phone isn't paired with UnderCover. On the PC: SafeHouse, Settings, Phone access."
    try:
        _lan("GET", "/")
        raise AssertionError("something answers on the phone address while Phone access is off")
    except ConnectionRefusedError:
        pass
    st = api("/api/phone", {"on": True})
    port = int(BASE.rsplit(":", 1)[1])
    assert st["enabled"] and st["listening"] and st["url"] == f"http://{PHONE_ADDR}:{port}", st
    # 1. the walk: every route, no token
    opened = 0
    for m, p in PHONE_SAMPLES + [("GET", "/nope"), ("POST", "/api/nope"), ("DELETE", "/api/nope")]:
        status, hd, body = _lan(m, p, body={} if m == "POST" else None)
        pol = phone_api.policy(m, p)[0]
        if pol == "open":
            opened += 1
            assert status == (403 if p == "/api/phone/claim" else 200), (m, p, status, body[:200])
        else:
            assert status == 401 and body == unpaired and hd.get("connection") == "close", (m, p, status, body[:200])
    # 2. the guards before the token: another Host (DNS rebinding), a missing or foreign Origin
    assert _lan("GET", "/", host="evil.example:7862")[0] == 403
    for m in ("HEAD", "PUT", "OPTIONS"):   # any other method: the same 401
        status, _, body = _lan(m, "/api/status")
        assert status == 401, (m, status)
    assert _lan("POST", "/api/phone/claim", body={}, origin=False)[0] == 403
    assert _lan("POST", "/api/phone/claim", body={}, origin="http://evil.example")[0] == 403
    # 3. pairing: the code from the PC, wrong then right, then never again
    pr = api("/api/phone/pair", {})["pairing"]
    assert pr["link"] == f"http://{PHONE_ADDR}:{port}/pair#{pr['code']}" and pr["qr"].startswith("<svg"), pr
    wrong = "".join(c for c in phone.ALPHABET if c not in pr["code"])[:6]
    assert _lan("POST", "/api/phone/claim", body={"code": wrong, "name": "Smoke phone"})[0] == 403
    status, hd, body = _lan("POST", "/api/phone/claim", body={"code": pr["code"], "name": "Smoke phone"})
    assert status == 200, (status, body)
    cookie = hd.get("set-cookie", "")
    assert all(x in cookie for x in ("uc_device=", "HttpOnly", "SameSite=Strict", "Path=/")), cookie
    token = cookie.split("uc_device=", 1)[1].split(";", 1)[0]
    assert _lan("POST", "/api/phone/claim", body={"code": pr["code"], "name": "Again"})[0] == 403, "a code worked twice"
    devs = api("/api/phone")["devices"]
    assert [d["name"] for d in devs] == ["Smoke phone"] and "hash" not in devs[0], devs
    # 4. with the token: the basic pages work, the PC-only routes are refused with a plain message
    s = json.loads(_lan("GET", "/api/status", token)[2])
    assert s["client"] == "phone" and s["device"] == "Smoke phone", s
    assert s.get("app_folder") is None, "a phone was told the PC's app folder"
    assert Path(api("/api/status")["app_folder"]).resolve() == ROOT.resolve(), "this PC's status doesn't name its app folder"
    for p in ("/", "/static/app.js", "/api/jobs", "/api/work", "/api/voices", "/api/library/covers", "/api/speak/voices",
              "/api/doctor", "/api/addons", "/api/storage", "/api/history", "/api/config", "/api/train/projects"):
        status = _lan("GET", p, token)[0]
        assert status == 200, (p, status)
    assert _lan("POST", "/api/work/settings", token, {})[0] == 200
    from undercover.core.config import path   # the sandbox's own folders (sandbox_server points this process at them)
    proj = next(p for p in (path("cache_dir") / "training").iterdir() if (p / "project.json").exists())
    clip = next(f for f in (proj / "clips").iterdir() if f.suffix in (".flac", ".wav"))
    assert _lan("GET", f"/files/training/{proj.name}/clips/{clip.name}", token)[0] == 200, "a phone can't play a clip"
    assert _lan("GET", f"/files/training/{proj.name}/project.json", token)[0] == 404, "a phone read a project file"
    assert api(f"/files/training/{proj.name}/project.json"), "this PC can't read the project file any more"
    assert _lan("DELETE", "/api/voices/no-such-voice", token)[0] not in (401, 403)   # reaches the route (the page asks twice)
    refused = 0
    for m, p in PHONE_SAMPLES:
        if phone_api.policy(m, p)[0] == "pc":
            status, hd, body = _lan(m, p, token, {} if m == "POST" else None)
            assert status == 403 and b"needs the PC" in body and hd.get("connection") == "close", (m, p, status, body[:200])
            refused += 1
    assert api("/api/status")["client"] == "pc", "the sandbox server stopped (Quit or Restart went through from the phone?)"
    # 5. this PC's own requests: exactly as before (a phone's cookie means nothing here; Host and Origin checks as they were)
    req = urllib.request.Request(BASE + "/api/status", headers={"Cookie": f"uc_device={token}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        assert json.loads(r.read())["client"] == "pc"
    for hdrs, method in (({"Host": "evil.example"}, "GET"), ({"Origin": "http://evil.example"}, "POST")):
        req = urllib.request.Request(BASE + "/api/work/settings", data=b"{}" if method == "POST" else None, method=method,
                                     headers={"Content-Type": "application/json", **hdrs})
        try:
            urllib.request.urlopen(req, timeout=30)
            raise AssertionError(f"this PC's {method} with {hdrs} went through")
        except urllib.error.HTTPError as e:
            assert e.code == 403 and json.loads(e.read()) == {"error": "Forbidden"}, e.code
    # 6. Revoke: the token stops working at its next request
    api("/api/phone/revoke", {"id": devs[0]["id"]})
    assert _lan("GET", "/api/status", token)[0] == 401
    # 7. switching off closes the listener at once; a paired phone works again when it's switched back on
    pr = api("/api/phone/pair", {})["pairing"]
    hd = _lan("POST", "/api/phone/claim", body={"code": pr["code"], "name": "Second"})[1]
    token2 = hd["set-cookie"].split("uc_device=", 1)[1].split(";", 1)[0]
    assert _lan("GET", "/api/status", token2)[0] == 200
    st = api("/api/phone", {"on": False})
    assert not st["enabled"] and not st["listening"], st
    try:
        _lan("GET", "/api/status", token2)
        raise AssertionError("the phone listener still answers after switching off")
    except ConnectionRefusedError:
        pass
    api("/api/phone", {"on": True})
    assert _lan("GET", "/api/status", token2)[0] == 200
    api("/api/phone", {"on": False})
    return f"{len(PHONE_SAMPLES)} routes walked ({opened} open), {refused} PC-only refused, revoke, switch off"


@check("api: themes (list, import, css at load, export, delete) and the pages' Content-Security-Policy")
def a_themes():
    import urllib.request
    r = api("/api/themes")
    assert isinstance(r.get("themes"), list), r
    req = urllib.request.Request(BASE + "/api/themes/import", data=_theme_zip("smoke-api"), method="POST",
                                 headers={"Content-Type": "application/zip", "Origin": BASE})
    with urllib.request.urlopen(req, timeout=30) as resp:
        row = json.loads(resp.read())
    assert row["ok"] and row["id"] == "smoke-api", row
    with urllib.request.urlopen(BASE + "/api/themes/smoke-api/theme.css", timeout=30) as resp:
        assert resp.headers["Content-Type"].startswith("text/css") and b'data-uc-theme="smoke-api"' in resp.read()
    with urllib.request.urlopen(BASE + "/api/themes/smoke-api/export", timeout=30) as resp:
        assert ".uctheme" in resp.headers["Content-Disposition"] and resp.read()[:2] == b"PK"
    req = urllib.request.Request(BASE + "/api/themes/import", data=_theme_zip(files={"evil.js": "x"}), method="POST",
                                 headers={"Content-Type": "application/zip", "Origin": BASE})
    try:
        urllib.request.urlopen(req, timeout=30)
        raise AssertionError("a hostile theme was imported over the API")
    except urllib.error.HTTPError as e:
        assert e.code == 422 and "error" in json.loads(e.read())
    assert api("/api/themes/smoke-api", method="DELETE")["ok"]
    assert "smoke-api" not in [x["id"] for x in api("/api/themes")["themes"]]
    for page in ("/", "/pair"):
        with urllib.request.urlopen(BASE + page, timeout=30) as resp:
            pol = resp.headers.get("Content-Security-Policy") or ""
        for part in ("default-src 'self'", "style-src 'self' 'unsafe-inline'", "font-src 'self'", "object-src 'none'",
                     "frame-ancestors 'none'", "'sha256-"):
            assert part in pol, (page, part, pol)
    return f"{len(r['themes'])} themes listed; import, css, export, refusal, delete; CSP on both pages"


@check("api: the First assignment's routes and status field are gone in 1.0; the seeds the first run uses are listed (sample: true, demo: true)")
def a_firstrun():
    assert "tutorial" not in api("/api/status"), "the status still carries the tutorial field"
    for path, body in (("/api/tutorial/done", {}), ("/api/tutorial/reset", {}), ("/api/tutorial/demo/update", {}),
                       ("/api/tutorial/demo/undo", {}), ("/api/tutorial/greetings", None),
                       ("/api/tutorial/greetings/good-morning.mp3", None)):
        try:
            api(path, body)
            raise AssertionError(f"{path} still answers")
        except urllib.error.HTTPError as e:
            assert e.code == 404, (path, e.code)
    demo = next((x for x in api("/api/voices") if x["name"] == "agent-smoke"), None)
    if demo:
        assert demo["demo"] is True and demo["trained"] is False, demo
        c = next(x for x in api("/api/work/candidates") if x["voice"] == "agent-smoke")
        assert c["can"] is False, c
    songs = api("/api/library/songs")
    assert all("sample" in x for x in songs)
    return f"6 routes 404, no status field; demo voice {'listed' if demo else 'not in this sandbox'}"


@check("Make smaller (phase 24): a project's WAV clips become FLAC (bit-identical) and its data.pt waits aside, Undo puts them back, Keep deletes only what was set aside; a voice's index shrinks with the full one aside; never automatic")
def u_smaller():
    import numpy as np
    import soundfile as sf
    from undercover.core.engine.rvc import index as ri
    from undercover.doubleagent.project import Project, projects_dir
    from undercover.core.voices import voices_dir
    import struct
    from undercover.safehouse import smaller
    with isolated_data():
        proj = Project(name="smaller-test", person="Test")
        proj.train = {"exported": "smaller-voice", "exported_epoch": 5, "state": "exported"}
        proj.dir.mkdir(parents=True)
        proj.save()
        d = proj.dir
        (d / "clips").mkdir(parents=True, exist_ok=True)
        (d / "train").mkdir(parents=True, exist_ok=True)
        rng = np.random.default_rng(1)
        clips = {}
        for i in range(3):
            y = (rng.standard_normal(22050) * 2000).astype(np.int16)
            sf.write(str(d / "clips" / f"c{i}.wav"), y, 44100, subtype="PCM_16")
            clips[f"c{i}"] = y
        (d / "train" / "data.pt").write_bytes(b"d" * 5000)
        vd = voices_dir() / "smaller-voice"
        vd.mkdir(parents=True)
        pth = os.urandom(4000)
        (vd / "model.pth").write_bytes(pth)
        (d / "train" / "e5.pth").write_bytes(pth)
        vecs = rng.standard_normal((300, 8)).astype(np.float16)
        with open(vd / "model.index", "wb") as f:
            f.write(ri.COMPACT_MAGIC + struct.pack("<qq", *vecs.shape) + vecs.tobytes())
        ov = smaller.overview()
        row = next(p for p in ov["projects"] if p["name"] == "smaller-test")
        assert row["clips_wav_bytes"] > 0 and row["data_bytes"] == 5000 and not row["pending"], row
        assert next(v for v in ov["voices"] if v["name"] == "smaller-voice")["saving"] == 0   # 300 vectors: already small
        # the project: clips FLAC, data.pt aside, the save linked; nothing deleted
        r = smaller.shrink_project("smaller-test")
        assert r["clips"] == 3 and r["clips_bytes_after"] < r["clips_bytes_before"] and r["data_bytes"] == 5000, r
        for pid, y in clips.items():
            assert proj.clip_path(pid).suffix == ".flac"
            back, _ = sf.read(str(proj.clip_path(pid)), dtype="int16")
            assert np.array_equal(back, y)
            assert (d / smaller.ASIDE / "clips" / f"{pid}.wav").exists(), "an original WAV went"
        assert not (d / "train" / "data.pt").exists() and (d / smaller.ASIDE / "train" / "data.pt").exists()
        assert os.path.samefile(d / "train" / "e5.pth", vd / "model.pth") and r["linked_bytes"] == 4000
        try:
            smaller.shrink_project("smaller-test")
            raise AssertionError("made smaller twice while waiting")
        except Exception as e:
            assert "waiting" in str(e)
        smaller.undo_project("smaller-test")                       # Undo: the WAVs and data.pt back, the FLACs gone
        assert all(proj.clip_path(p).suffix == ".wav" for p in clips) and not list((d / "clips").glob("*.flac"))
        assert (d / "train" / "data.pt").read_bytes() == b"d" * 5000 and not (d / smaller.ASIDE).exists()
        smaller.shrink_project("smaller-test")                     # again, then Keep: only what was set aside goes
        from undercover.doubleagent.trainer import export_voice
        smaller.keep_project("smaller-test")
        try:                                                       # adding another save now says what to do
            export_voice(Project.load("smaller-test"), epoch=5)
            raise AssertionError("export ran without its prepared data")
        except Exception as e:
            assert "Train more" in str(e), e
        assert not (d / smaller.ASIDE).exists() and all(proj.clip_path(p).suffix == ".flac" for p in clips)
        assert (vd / "model.pth").read_bytes() == pth and (d / "train" / "e5.pth").read_bytes() == pth
        # a voice: its index shrunk (here 300 -> 50 on the CPU), the full one aside; its readings to hear; Undo and Keep
        full = (vd / "model.index").read_bytes()
        r = smaller.shrink_voice("smaller-voice", check=False, k=50, device="cpu")
        assert r["vectors_before"] == 300 and r["vectors_after"] == 50 and (vd / smaller.INDEX_ASIDE).read_bytes() == full
        assert len(ri.read_vectors(vd / "model.index")) == 50
        heard = smaller._readings("smaller-voice")                # (the real job writes them; here stand-ins)
        for f in heard.values():
            f.parent.mkdir(parents=True, exist_ok=True)
            sf.write(str(f), np.zeros(800, np.int16), 16000, subtype="PCM_16")
        res = next(v for v in smaller.overview()["voices"] if v["name"] == "smaller-voice")["result"]
        assert res["full_url"].startswith("/files/previews/") and res["small_url"].endswith("-small.flac"), res
        smaller.undo_voice("smaller-voice")
        assert (vd / "model.index").read_bytes() == full and not (vd / smaller.INDEX_ASIDE).exists()
        assert not any(f.exists() for f in heard.values()), "Undo left the readings"
        smaller.shrink_voice("smaller-voice", check=False, k=50, device="cpu")
        smaller.keep_voice("smaller-voice")
        assert not (vd / smaller.INDEX_ASIDE).exists() and len(ri.read_vectors(vd / "model.index")) == 50
    return "project: clips FLAC, data.pt aside, save linked, Undo, Keep; voice: index 300 -> 50, Undo, Keep"


@check("listen-back on a fresh install (phase 25): no speech recogniser yet starts its pinned download in the background, the speech goes on, and the next one tries again (not a lasting failure)")
def u_listenback_fetch():
    from undercover.core import setup_models
    from undercover.speakeasy import listenback as lb
    with isolated_data():
        saved = (dict(lb._ASR), setup_models.ensure)
        asked = []
        setup_models.ensure = lambda files=None, sha256=None, progress=None: asked.append((files, sha256)) or []
        try:
            lb._ASR.update(model=None, failed="", fetching=None)
            assert lb._asr() is None and "downloading" in lb._ASR["why"] and not lb._ASR["failed"]
            lb._ASR["fetching"].join(10)
            (files, sha), = asked
            assert list(files) == ["whisper/large-v3-turbo.pt"] and sha["whisper/large-v3-turbo.pt"], (files, sha)
            assert lb._asr() is None and len(asked) == 2, "the next speech didn't try again"
            lb._ASR["fetching"].join(10)
            try:
                lb.hear(["x.wav"])
                raise AssertionError("heard without a recogniser")
            except RuntimeError as e:
                assert "downloading" in str(e), e
            lb._ASR["fetching"].join(10)
            n = len(asked)                                             # a failed download waits before the next try
            setup_models.ensure = lambda **k: (_ for _ in ()).throw(OSError("offline"))
            lb._asr()
            lb._ASR["fetching"].join(10)
            assert lb._ASR["fetch_failed"] > 0
            setup_models.ensure = lambda files=None, sha256=None, progress=None: asked.append((files, sha256)) or []
            lb._asr()
            assert len(asked) == n and not lb._ASR["fetching"].is_alive(), "retried at once after a failure"
        finally:
            lb._ASR.clear()
            lb._ASR.update(saved[0])
            setup_models.ensure = saved[1]
    return "missing -> a pinned, hashed fetch in the background; the speech goes on; tried again next time"
@check("a new voice's index at export (phase 24 follow-up): 10k k-means centres, quietly (not in Make smaller's Keep / Undo); the full one kept 30 days (its date in the voice list, put back from the voice's detail), then removed, guarded and logged; a broken index leaves the export as it was; 0 = off")
def u_shrink_export():
    import struct
    import numpy as np
    from undercover.core.engine.rvc import index as ri
    from undercover.core.voices import voices_dir
    from undercover.doubleagent import trainer
    from undercover.safehouse import smaller
    with isolated_data():
        vd = voices_dir() / "export-voice"
        vd.mkdir(parents=True)
        (vd / "model.pth").write_bytes(b"p")
        vecs = np.random.default_rng(2).standard_normal((300, 8)).astype(np.float16)
        full = ri.COMPACT_MAGIC + struct.pack("<qq", *vecs.shape) + vecs.tobytes()
        (vd / "model.index").write_bytes(full)
        assert trainer.shrink_exported_index(vd, k=0) is None and (vd / "model.index").read_bytes() == full   # off
        t0 = time.time()
        r = trainer.shrink_exported_index(vd, k=50, device="cpu")
        assert r["vectors_before"] == 300 and r["vectors_after"] == 50 and 29.9 * 86400 < r["until"] - t0 < 30.1 * 86400, r
        assert (vd / ri.EXPORT_ASIDE).read_bytes() == full and len(ri.read_vectors(vd / "model.index")) == 50
        assert not (vd / ri.ASIDE).exists(), "the export left a Make smaller Keep / Undo"
        row = next(v for v in smaller.overview()["voices"] if v["name"] == "export-voice")
        assert not row["pending"] and row["saving"] == 0, row               # nothing for the user to decide
        fi = smaller.full_index(vd)
        assert fi["until"] == time.strftime("%Y-%m-%d", time.localtime(r["until"])) and fi["vectors_before"] == 300, fi
        assert trainer.shrink_exported_index(vd, k=50, device="cpu") is None, "shrunk twice"
        smaller.restore_full_index("export-voice")                            # Deep Cover: put the full one back
        assert (vd / "model.index").read_bytes() == full and smaller.full_index(vd) is None
        assert not (vd / ri.EXPORT_RECORD).exists()
        try:
            smaller.restore_full_index("export-voice")
            raise AssertionError("restored twice")
        except Exception as e:
            assert "no full index" in str(e), e
        # 30 days later: removed (guarded, logged); the 10k index and the voice stay; a younger one is left alone
        trainer.shrink_exported_index(vd, k=50, device="cpu")
        young = voices_dir() / "young-voice"
        young.mkdir()
        (young / "model.index").write_bytes(full)
        trainer.shrink_exported_index(young, k=50, device="cpu")
        assert smaller.expire_full_indexes(now=t0 + 31 * 86400) == ["export-voice", "young-voice"]
        assert not (vd / ri.EXPORT_ASIDE).exists() and not (vd / ri.EXPORT_RECORD).exists()
        assert len(ri.read_vectors(vd / "model.index")) == 50 and (vd / "model.pth").exists()
        trainer.shrink_exported_index(vd, k=50, device="cpu") is None   # (already small: nothing to keep)
        (young / "model.index").write_bytes(full)
        trainer.shrink_exported_index(young, k=50, device="cpu")
        assert smaller.expire_full_indexes(now=t0 + 86400) == [] and (young / ri.EXPORT_ASIDE).exists()
        (vd / "model.index").write_bytes(b"not an index")              # it can't shrink: the export is untouched
        assert trainer.shrink_exported_index(vd, k=50, device="cpu") is None
        assert (vd / "model.index").read_bytes() == b"not an index" and not (vd / ri.EXPORT_ASIDE).exists()
        assert not (vd / "model.index.smaller.tmp").exists()
    return "300 -> 50 quietly (not pending), full kept 30 days with its date, put back, expired guarded; bad index left alone"


@check("estimates (Internal Affairs m10): SpeakEasy gives the time to make it (one cold sentence with listen-back near the 39 s measured, not its 5 s of audio), and a one-shot with an installed voice a measured range")
def u_estimates():
    from undercover.frontman import oneshot
    from undercover.speakeasy import speak, tts
    text = "Good evening, the rain came in sideways that night."
    audio = tts.seconds_estimate(text, 1.0)
    cold = speak.eta_seconds(text, {"mode": "cadence", "rtf": 1.0, "speed": 1.0})
    assert audio < 8 and 25 <= cold <= 60, (audio, cold)            # Internal Affairs: 39 s on an idle GPU, cold
    reader = speak.eta_seconds(text, {"mode": "reader", "rtf": 3.0, "speed": 1.0})
    assert reader < cold and reader >= audio / 3, (reader, cold)
    with isolated_data():
        r = oneshot.cover_eta("some-voice")
    assert 0 < r["low"] < r["high"] < 900, r
    return f"one sentence: {audio:.0f} s of audio, about {cold:.0f} s to make cold (reader {reader:.0f} s); one-shot {r['low']}-{r['high']} s"


@check("a one-shot's variation starts from what it used (Internal Affairs m1): plan.settings.options carries the cover's exact options, the original backing kept for stacked harmonies")
def u_oneshot_options():
    from undercover.frontman.oneshot import cover_options_of
    from undercover.frontman.pipeline import CoverOptions
    chordal = cover_options_of(CoverOptions(voice="v", key=2, octave=-1, quality="high", dereverb=True, karaoke=True,
                                            backing_voice=None, backing_octave=-1, backing_db=-3.0, polish=True))
    assert chordal["backing_db"] == -3.0 and "backing_voice" not in chordal and "voice" not in chordal, chordal
    assert (chordal["octave"], chordal["key"], chordal["karaoke"]) == (-1, 2, True), chordal
    plain = cover_options_of(CoverOptions(voice="v", backing_voice="v", karaoke=True))
    assert plain["backing_voice"] == "v", plain
    return "the stacked-harmony cover's variation keeps the original backing; a plain one converts it"


@check("Compare with Fit on (Internal Affairs): the fit is worked out once on the full stems; octave / key versions are read relative to the page's setting and land around the fit's pick, so they differ; with Fit off they're as sent")
def u_preview_fit():
    from dataclasses import replace
    from undercover.frontman import pipeline as PL
    opts = PL.CoverOptions(voice="v", octave=0, key=0, fit=True)
    calls = []

    class Engine:
        def _fit(self, o, voice, stems, notes, bvoice):
            calls.append(1)
            notes.append("Fit to voice: octave -1, key +2")
            return replace(o, octave=-1, key=2)
    from undercover.core import voices
    real = voices.get_voice
    voices.get_voice = lambda name: type("V", (), {"name": name, "extra": {}})()
    try:
        base, note = PL.preview_fit(Engine(), opts, {"vocals": "x"})
    finally:
        voices.get_voice = real
    assert (base.octave, base.key) == (-1, 2) and note.startswith("Fit to voice") and len(calls) == 1
    octs = [PL.variant_overrides(opts, base, {"octave": x, "label": "?"})["octave"] for x in (-1, 0, 1)]
    assert octs == [-2, -1, 0], octs                      # around the fit's pick, all different
    keys = [PL.variant_overrides(opts, base, {"key": x})["key"] for x in (-2, 0, 2)]
    assert keys == [0, 2, 4], keys
    assert PL.variant_overrides(opts, base, {"octave": 2})["octave"] == 1     # clamped to the page's range
    assert PL.variant_overrides(opts, base, {"polish": False}) == {"polish": False}   # other kinds untouched
    off = replace(opts, fit=False)
    base_off, note_off = PL.preview_fit(Engine(), off, {})
    assert base_off is off and note_off is None and len(calls) == 1         # Fit off: no fit, versions as sent
    assert PL.variant_overrides(off, base_off, {"octave": 1})["octave"] == 1
    import inspect
    from undercover.frontman import api as FA
    assert '"fit": r.get("fit")' in inspect.getsource(FA._start_preview), "POST /api/preview drops the fit note"
    return "one fit; octaves -2/-1/0 and keys 0/2/4 around it; clamped; Fit off as sent; the results carry the note"


@check("Train tonight instead: held work carries {id, kind, label, project, held_at, queue_as}; /api/work/to-queue moves a held project entry into the overnight queue as its project item (its own choices) and off the held list, the hold lifting when nothing else is held; a one-shot can't; an improve may replace its own voice")
def u_to_queue():
    from undercover.core import worklist
    from undercover.core.voices import ModelError, voices_dir
    from undercover.doubleagent import api as DA, night, project as P  # night registers the queue kinds
    with isolated_data():
        worklist._STATE = None
        proj = P.Project.create("Donald Trump", "speaking")
        proj.sources = [P.Source(id="v1", title="Rally", url="https://example.invalid/v1", duration=600.0, selected=True)]
        proj.train = {"voice_name": "donald-trump-uc", "length": "quick", "sr": 48000, "state": "paused"}
        proj.save()
        class J:
            resume_spec = {"kind": "train", "project": "donald-trump", "label": "Training Donald Trump"}
        worklist.hold()
        worklist._state()["held"] = True
        worklist.remember(J())
        worklist.remember(J())                                   # the same work twice: one entry
        J.resume_spec = {"kind": "oneshot", "text": "Hold On by Agent Smoke", "label": "One-shot: Hold On"}
        worklist.remember(J())
        rows = worklist.status()["resume"]
        assert [r["kind"] for r in rows] == ["train", "oneshot"], rows
        train, one = rows
        assert train["id"] and train["project"] == "donald-trump" and train["held_at"] and train["queue_as"] == "project", train
        assert one["queue_as"] is None, one
        try:
            worklist.to_queue(one["id"])
            raise AssertionError("a one-shot went overnight")
        except ModelError as e:
            assert "can't run overnight" in str(e), e
        st = worklist.to_queue(train["id"])
        item = next(i for i in st["queue"] if i["kind"] == "project")
        assert item["target"] == "donald-trump" and [r["kind"] for r in st["resume"]] == ["oneshot"] and st["held"], st
        tr = P.Project.load("donald-trump").train
        assert (tr["length"], tr["sr"], tr["state"]) == ("quick", 48000, "paused"), tr   # its own choices, still resumable
        worklist.forget_paused()
        assert not worklist.status()["held"]
        # a restart's saved entries without ids get them
        worklist._state()["resume"] = [{"kind": "prepare", "project": "donald-trump", "label": "Preparing"}]
        worklist._save()
        worklist._STATE = None
        assert worklist.status()["resume"][0]["id"]
        # an overnight improve may replace its own voice (a downloaded one has no project); other names stay guarded
        d = voices_dir() / "obama"
        d.mkdir(parents=True)
        (d / "meta.json").write_text(json.dumps({"name": "obama", "extra": {}}), encoding="utf-8")
        DA.check_voice_name(proj, "obama", replace="obama")
        for bad in ("obama", "half-installed"):
            if bad == "half-installed":
                (voices_dir() / bad).mkdir()
            try:
                DA.check_voice_name(proj, bad)
                raise AssertionError(f"{bad} was allowed")
            except ModelError:
                pass
        worklist._STATE = None
    return "held entries with ids; train -> the project item (choices kept, still paused), off the held list; one-shot refused; ids backfilled; improve replaces its own"


@check("queue a project (the user: \"queue Trump with specific videos selected\"): /api/work/add {kind: project} takes an existing project with its own choices; nothing ticked or another person's voice name is refused when queued, not overnight; the project shows queued")
def u_project_queue():
    from undercover.core import worklist
    from undercover.core.voices import ModelError, voices_dir
    from undercover.doubleagent import api as DA, night, project as P, service
    with isolated_data():
        worklist._STATE = None
        proj = P.Project.create("Donald Trump", "speaking")
        try:
            worklist.add("project", proj.name)
            raise AssertionError("queued with nothing ticked")
        except ModelError as e:
            assert "Tick" in str(e), e
        proj.sources = [P.Source(id="v1", title="Rally", url="https://example.invalid/v1", duration=600.0, selected=True)]
        proj.train = {"voice_name": "barack-obama-uc", "length": "long"}
        proj.save()
        d = voices_dir() / "barack-obama-uc"
        d.mkdir(parents=True)
        (d / "meta.json").write_text(json.dumps({"name": "barack-obama-uc", "extra": {"project": "barack-obama",
                                                 "person": "Barack Obama"}}), encoding="utf-8")
        try:
            worklist.add("project", proj.name)
            raise AssertionError("queued over Barack Obama's voice")
        except ModelError as e:
            assert "Barack Obama" in str(e), e
        try:   # what's on screen when queued is what runs: the page sends its choices with the add
            worklist.add("project", proj.name, options={"voice_name": "trump-rallies", "length": "forever", "sr": 40000})
            raise AssertionError("an unknown length was queued")
        except ModelError as e:
            assert "length" in str(e), e
        st = worklist.add("project", proj.name, options={"voice_name": "Trump Rallies", "length": "quick", "sr": 48000})
        tr = P.Project.load(proj.name).train
        assert (tr["voice_name"], tr["length"], tr["sr"]) == ("trump-rallies", "quick", 48000), tr
        quick = next(i for i in worklist._state()["queue"] if i["kind"] == "project")["estimate"]["train"]
        auto = night.estimate({"kind": "new", "target": "nobody"})["train"]
        assert quick < auto, (quick, auto)   # the item's estimate is Quick's, not the auto length's
        item = next(i for i in worklist._state()["queue"] if i["kind"] == "project")
        assert item["target"] == "donald-trump" and item["estimate"]["total"] >= 0, item
        assert worklist.KINDS["project"]["label"](item) == "Training Donald Trump (your choices)"
        q = DA.project_json(P.Project.load(proj.name))["queued"]
        assert q and q["state"] == "queued" and q["id"] == item["id"], q
        try:
            worklist.add("project", proj.name)
            raise AssertionError("queued twice")
        except ModelError as e:
            assert "already in the queue" in str(e), e
        worklist.remove(next(i for i in worklist._state()["queue"] if i["kind"] == "project")["id"])
        pp = P.Project.load(proj.name)                              # a paused training continues at its own rate
        pp.train.update(state="paused", sr=40000)
        pp.save()
        try:
            worklist.add("project", proj.name, options={"sr": 32000})
            raise AssertionError("a paused training's rate changed")
        except ModelError as e:
            assert "paused at 40 kHz" in str(e), e
        pp.train.pop("state")
        pp.save()
        st = worklist.add("project", proj.name, options={"voice_name": "donald-trump-uc"})
        item = next(i for i in worklist._state()["queue"] if i["kind"] == "project")
        # the run itself refuses another person's voice name before any work (the name changed after queueing)
        proj = P.Project.load(proj.name)
        proj.train["voice_name"] = "barack-obama-uc"
        proj.save()
        class J:
            cancel = pausing = False
            finished = False
        try:
            service.run_project(J(), proj.name, object(), lambda *a, **k: None)
            raise AssertionError("the run started over Barack Obama's voice")
        except ModelError as e:
            assert "Barack Obama" in str(e), e
        assert proj.name not in DA.ACTIVE
        worklist.remove(item["id"])
        assert DA.project_json(P.Project.load(proj.name))["queued"] is None
        worklist._STATE = None
    return "nothing ticked / another person's voice refused at queue time; queued once, shown on the project; the run checks again"


@check("a training never writes over another person's voice: a name owned by another project's voice (or a downloaded one) is refused at the start and at export; Stop reaches every kind of project job (prepare has no trainer control)")
def u_voice_name_guard():
    from undercover.core import jobs as J
    from undercover.core.voices import ModelError, voices_dir
    from undercover.doubleagent import api as DA, project as P, trainer
    with isolated_data():
        def voice(name, **extra):
            d = voices_dir() / name
            d.mkdir(parents=True)
            (d / "meta.json").write_text(json.dumps({"name": name, "extra": extra}), encoding="utf-8")
        voice("barack-obama-uc", project="barack-obama", person="Barack Obama")
        voice("downloaded-voice")
        voice("donald-trump-uc", project="donald-trump", person="Donald Trump")
        proj = P.Project.create("Donald Trump", "speaking")
        assert proj.name == "donald-trump"
        DA.check_voice_name(proj, "donald-trump-uc")   # its own voice: a re-export may replace it
        DA.check_voice_name(proj, "brand-new-name")    # nobody's yet
        for taken in ("barack-obama-uc", "downloaded-voice"):
            try:
                DA.check_voice_name(proj, taken)
                raise AssertionError(f"{taken} was allowed")
            except ModelError as e:
                assert "already" in str(e), e
        tdir = proj.dir / "train"
        tdir.mkdir(parents=True, exist_ok=True)
        (tdir / "e5.pth").write_bytes(b"x")
        (tdir / "data.pt").write_bytes(b"")
        proj.train = {"voice_name": "barack-obama-uc", "saves": [{"epoch": 5}]}   # what the stale page once stored
        proj.save()
        try:
            trainer.export_voice(proj, 5)
            raise AssertionError("exported over Barack Obama's voice")
        except ModelError as e:
            assert "Barack Obama" in str(e), e
        assert json.loads((voices_dir() / "barack-obama-uc" / "meta.json").read_text(encoding="utf-8"))["extra"]["project"] == "barack-obama"
        # Stop on a prepare job (no trainer control) sets the job's own flag
        job = J.Job("train-prepare")
        DA.ACTIVE["donald-trump"] = {"job": job, "control": None, "kind": "prepare"}
        class H:
            def _json(self, obj, status=200):
                self.out = obj
        try:
            h = H()
            DA._stop(h, proj, {})
            assert job.cancel and h.out == {"ok": True}, "Stop didn't reach the prepare job"
        finally:
            DA.ACTIVE.pop("donald-trump", None)
    return "another person's voice refused at start and export; own voice and new names fine; Stop reaches prepare"


@check("listen-back's drawn-out word (phase 24): in a short line (up to 4 words), a one-syllable word held over 0.30 s asks for a re-read; a marked pause after it, the last word, a longer word or a long sentence don't")
def u_drawl():
    from undercover.speakeasy import listenback as lb
    w = lambda *t: [[a, s, e, 0.9] for a, s, e in t]
    assert lb.problems("Good morning.", w(("Good", 0.0, 0.36), ("morning.", 0.36, 0.76))) == ["drawn-out 'good' (0.36 s)"]
    assert lb.problems("Good morning.", w(("Good", 0.0, 0.16), ("morning.", 0.16, 0.44))) == []
    assert lb.problems("Good, morning.", w(("Good,", 0.0, 0.40), ("morning.", 0.40, 0.76))) == [], "a comma's pause counted"
    assert lb.problems("It was late.", w(("It", 0, 0.1), ("was", 0.1, 0.2), ("late.", 0.2, 0.8))) == [], "the last word counted"
    assert lb.problems("An evening walk.", w(("An", 0, 0.1), ("evening", 0.1, 0.6), ("walk.", 0.6, 0.9))) == [], "a longer word counted"
    long_line = "Ladies and gentlemen we have a lot to cover today."   # flowing speech: Whisper's edges can't tell
    ws = [[t, 0.4 * i, 0.4 * i + 0.4, 0.9] for i, t in enumerate(long_line.split())]
    assert lb.problems(long_line, ws) == [], "a long line flagged a drawn-out word"
    assert [lb.syllables(x) for x in ("good", "you're", "late", "morning", "evening", "little")] == [1, 1, 1, 2, 3, 2]
    return "drawn-out caught; comma, last word and longer words left alone"


@check("smaller, never worse (phase 24): training clips as FLAC (old WAV clips still read); key changes as 24-bit FLAC, float WAV kept over full scale; the exported save hard-linked to the voice, and each copy survives the other's delete, a move and a restore; speech as FLAC")
def u_lossless():
    import numpy as np
    import soundfile as sf
    from undercover.core import fsutil, storage
    from undercover.doubleagent.project import Project
    from undercover.frontman import remix
    with isolated_data() as box:
        # clips: the project's clip path, FLAC first, an older WAV clip as it is
        proj = Project(name="lossless-test")   # in the isolated cache
        (proj.dir / "clips").mkdir(parents=True)
        assert proj.clip_path("a").name == "a.flac"   # a new clip
        y = (np.random.default_rng(0).standard_normal(44100) * 3000).astype(np.int16)
        sf.write(str(proj.dir / "clips" / "b.wav"), y, 44100, subtype="PCM_16")
        assert proj.clip_path("b").name == "b.wav"    # an older project's clip
        sf.write(str(proj.clip_path("a")), y, 44100, subtype="PCM_16")
        z, _ = sf.read(str(proj.clip_path("a")), dtype="int16")
        assert np.array_equal(y, z), "a FLAC clip isn't bit-identical"
        assert (proj.dir / "clips" / "a.flac").stat().st_size < (proj.dir / "clips" / "b.wav").stat().st_size
        sf.write(str(proj.dir / "clips" / "b.flac"), y, 44100, subtype="PCM_16")
        assert proj.clip_path("b").name == "b.flac"   # made smaller: the FLAC is the one read
        # key changes: 24-bit FLAC within the 24-bit floor; over full scale stays float WAV (FLAC would clip)
        t = np.linspace(0, 1, 44100, dtype=np.float32)
        for peak, want in ((0.9, ".flac"), (1.3, ".wav")):
            x = np.stack([np.sin(2 * np.pi * 220 * t), np.sin(2 * np.pi * 330 * t)], 1) * peak
            tmp = box / f"k{peak}.part.wav"
            sf.write(str(tmp), x, 44100, subtype="FLOAT")
            out = remix._keep_small(tmp, box / f"k{peak}.flac", box / f"k{peak}.wav")
            assert out.suffix == want and not tmp.exists(), (peak, out)
            back, _ = sf.read(str(out), dtype="float32", always_2d=True)
            err = float(np.abs(back - x).max())
            assert err < (2 ** -23 if want == ".flac" else 1e-12), (peak, err)
        # the exported save and the voice's model.pth: one copy on disk
        a, b = box / "voices" / "v" / "model.pth", box / "train" / "e10.pth"
        a.parent.mkdir(parents=True)
        b.parent.mkdir(parents=True)
        data = os.urandom(200_000)
        a.write_bytes(data)
        b.write_bytes(data)
        assert fsutil.link_duplicate(a, b) and os.path.samefile(a, b)
        c = box / "train" / "e11.pth"
        c.write_bytes(os.urandom(200_000))
        assert not fsutil.link_duplicate(a, c) and not os.path.samefile(a, c), "different files linked"
        # a move or a Vault-style copy is two files; a restore (a new file swapped in) splits them; deleting one keeps the other
        moved = box / "elsewhere"
        storage.copy_tree(a.parent, moved)
        assert (moved / "model.pth").read_bytes() == data and not os.path.samefile(moved / "model.pth", a)
        part = a.with_name("model.pth.vault-part")
        part.write_bytes(data)
        os.replace(part, a)
        assert not os.path.samefile(a, b) and b.read_bytes() == data, "a restore changed the other copy"
        assert fsutil.link_duplicate(a, b)
        storage.remove_files([], within=[box], dirs=[a.parent])   # the voice deleted
        assert b.read_bytes() == data, "deleting the voice took the training save's data"
        a.parent.mkdir(parents=True)
        a.write_bytes(data)
        assert fsutil.link_duplicate(a, b)
        storage.remove_files([b], within=[box])                    # the save deleted
        assert a.read_bytes() == data, "deleting the save took the voice's data"
        fsutil.torch_save({"w": 1}, a)                             # a later save is a new file: no shared write
    return "clips FLAC (bit-identical, WAV still read); key changes FLAC-24 / float over 1.0; save linked, each copy survives delete, move, restore"


@check("the First assignment is removed for 1.0 (MasterPlan; parked on branch tutorial-redesign): no tutorial module, routes, status field, clips or page script; app/firstrun.py keeps only the seeds")
def u_no_assignment():
    from undercover.app import firstrun, phone_api
    assert not (ROOT / "undercover" / "app" / "tutorial.py").exists()
    assert not (ROOT / "undercover" / "web" / "tutorial.js").exists()
    assert not (ROOT / "undercover" / "assets" / "demo-voice" / "greetings").exists(), "the greeting clips still ship"
    for gone in ("greetings", "reset", "status", "mark_done", "handle", "demo_update", "update_demo", "undo_demo", "PREVIOUS"):
        assert not hasattr(firstrun, gone), gone
    for keep in ("first_run", "seed", "seed_song", "seed_voice", "sample_song_id", "demo_files"):
        assert callable(getattr(firstrun, keep)), keep
    rows = " ".join(rx for _, rx, _, _ in phone_api.ROUTES)
    assert "/api/tutorial" not in rows, rows
    srv = (ROOT / "undercover" / "app" / "server.py").read_text(encoding="utf-8")
    assert "firstrun.handle" not in srv and '"tutorial":' not in srv
    src = (ROOT / "installer" / "build.ps1").read_text(encoding="utf-8")
    assert "greeting" not in src.lower() and "from undercover.app import firstrun" in src
    return "module, routes, status field, clips and page script gone; the seeds kept"


@check("api: the tray's client against the sandbox (status, jobs, phone, pause / resume, a refusal in plain words)")
def a_tray():
    from undercover.core import trayicon as tray
    port = int(BASE.rsplit(":", 1)[1])
    srv = tray.Server(port)
    st = srv.get("/api/status")
    assert st and "work" in st, st
    model = tray.menu_model(st, srv.get("/api/jobs"), (srv.get("/api/phone") or {}).get("enabled"))
    assert model[0]["id"] == "open" and any(i["id"] == "phone" for i in model), model
    ok, _ = srv.post("/api/work/pause")
    assert ok
    ok, _ = srv.post("/api/work/resume")
    assert ok
    ok, msg = srv.post("/api/phone/revoke", {"id": "nobody"})
    assert not ok and "paired" in msg, msg   # the server's own words come back for the notification
    assert tray.Server(1).get("/api/status") is None and tray.icon_state(None)[0] == "stopped"
    return "status → menu; pause, resume; a refusal's words; a server that's down reads as stopped"


@check("api: status ready")
def a_status():
    s = wait_for(lambda: (lambda d: d if d.get("models") == "ready" else None)(api("/api/status")), 180, what="models ready")
    assert s.get("gpu"), "no GPU reported"
    assert "ytdlp_updated" in s, "/api/status doesn't carry ytdlp_updated"
    assert s.get("build") and s["build"] == api("/api/status")["build"], "/api/status doesn't carry a steady build stamp"
    return s.get("gpu")


@check("page: the suite shell serves, with every module tab and its page")
def a_page():
    import re
    html = api("/").decode("utf-8")
    assert "<title>UnderCover</title>" in html, "browser title isn't UnderCover"
    views = {"frontman": "covers", "doubleagent": "train", "speakeasy": "speakeasy", "safehouse": "library"}
    for mod, view in views.items():
        assert f'id="tab-{mod}"' in html and f'data-tab="{mod}"' in html, f"no {mod} tab"
        assert f'id="view-{view}"' in html, f"no page for {mod}"
    for part in ('id="suite-logo"', 'id="uc-updated"', 'id="room-running"', 'id="workbar"', 'id="q-drawer"', 'id="q-panel"', 'id="wa-main"',
                 'id="da-start"', 'id="da-person"', 'id="da-train"', 'id="da-tonight"', 'id="da-work"', 'id="da-outcome"', 'id="da-steps"',
                 'id="wn"', 'id="wn-ok"', 'id="sys-news"', 'id="ear-cover"', 'id="se-ear"', 'id="set-ear-row"',
                 'id="vault"', 'id="vt-body"', 'id="smaller"', 'id="sm-body"', 'id="first-ready"', 'id="lp-chip"', 'id="lp-pop"', 'id="gp-low"'):
        assert part in html, f"missing {part}"
    # the overnight queue lives in SafeHouse's Running room (the DoubleAgent redesign); DoubleAgent keeps only its door
    room = html.index('id="room-running"')
    assert html.index('id="q-panel"') > room and html.index('id="q-report"') > room, "the queue panel isn't in the Running room"
    # What's new: the notes file is well formed, newest first, one id each, short plain lines (whatsnew.js reads it)
    wn = api("/static/whatsnew.json")
    wn = json.loads(wn) if isinstance(wn, (bytes, str)) else wn
    ents = wn["entries"]
    assert ents and len({e["id"] for e in ents}) == len(ents), "What's new: ids must be unique"
    assert [e["date"] for e in ents] == sorted((e["date"] for e in ents), reverse=True), "What's new: newest first"
    for e in ents:
        assert re.fullmatch(r"\d{4}-\d\d-\d\d", e["date"]) and e["title"].strip(), f"What's new: {e['id']} needs a date and a title"
        assert 1 <= len(e["lines"]) <= 4 and all(0 < len(l) <= 200 for l in e["lines"]), f"What's new: {e['id']} takes 1-4 short lines"
    files = re.findall(r'(?:src|href)="(/static/[^"]+)"', html)
    for f in files:
        body = api(f)   # the home-screen manifest (phase 11) comes back parsed
        assert (body.get("name") == "UnderCover") if isinstance(body, dict) else len(body) > 100, f"{f} is empty"
    return f"4 modules, {len(files)} static files"


@check("page: loads in a browser with no uncaught error (headless Chrome; skipped when there isn't one)")
def a_page_console():
    import shutil, subprocess, tempfile
    exe = shutil.which("chrome") or shutil.which("google-chrome") or next(
        (c for c in (r"C:\Program Files\Google\Chrome\Application\chrome.exe", r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe") if Path(c).is_file()), None)
    if not exe:
        return "no Chrome here: skipped (the unit scan for clashing top-level names still ran)"
    with tempfile.TemporaryDirectory() as prof:
        r = subprocess.run([exe, "--headless=new", "--disable-gpu", "--mute-audio", f"--user-data-dir={prof}", "--enable-logging=stderr", "--v=0",
                            "--virtual-time-budget=8000", "--dump-dom", BASE + "/"], capture_output=True, text=True, timeout=90)
    bad = [ln.split("CONSOLE", 1)[1][:220] for ln in r.stderr.splitlines() if ":CONSOLE" in ln and "Uncaught" in ln]
    assert not bad, "the page threw on load (a script that fails to load takes its whole feature with it): " + " | ".join(bad)
    return "no uncaught error on load"


@check("api: voices, history, config")
def a_lists():
    v = api("/api/voices")
    assert isinstance(v, list) and v, "no voices"
    h = api("/api/history")
    api("/api/config")
    return f"{len(v)} voices, {len(h) if isinstance(h, list) else '?'} covers"


@check("api: library lists and clean-up groups")
def a_library():
    s = api("/api/library/summary")
    assert s["total"] > 0
    lists = {}
    for k in ("covers", "speech", "songs", "voices", "training"):
        lists[k] = api(f"/api/library/{k}")
        assert isinstance(lists[k], list), k
    # the Kept / Recent tabs' counts per category match the lists (the chips follow the tab showing)
    t = s["tabs"]
    for k in ("covers", "speech"):
        assert t["kept"][k] == sum(1 for x in lists[k] if x["kept"]), f"kept {k}"
        assert t["recent"][k] == sum(1 for x in lists[k] if x["recent"]), f"recent {k}"
    assert all(t["kept"][k] == s["counts"][k] for k in ("songs", "voices", "training")), "songs, voices and training are always kept"
    assert set(t["recent"]) == {"covers", "speech"}, "only covers and speech have a recent tray"
    groups = api("/api/library/cleanup")
    assert "orphan_stems" in groups and "finished_training" in groups
    return f"{s['total'] / 1e9:.1f} GB in library"


@check("api: train projects")
def a_train():
    ps = api("/api/train/projects")
    assert isinstance(ps, list)
    if ps:
        full = api(f"/api/train/projects/{ps[0]['name']}")
        assert "phrases" in full and "sources" in full
    return f"{len(ps)} projects"


@check("api: work queue, candidates, pause/resume")
@check("api: the vault (a folder inside the test's own, a run, what it holds, a restore that finds everything already here)")
def a_vault():
    from undercover.core import storage
    vf = storage.ALLOWED_ROOTS[0] / "vault-drive"
    bad = api("/api/vault/check?folder=" + urllib.parse.quote(str(ROOT / "output")))
    assert bad["ok"] is False, bad
    st = api("/api/vault/settings", {"folder": str(vf), "size": "matters"})
    assert st["on"] and st["connected"], st
    try:
        assert api("/api/vault/run", {})["started"] is True
        wait_for(lambda: not api("/api/vault")["running"], 600, what="the vault run")
        st = api("/api/vault")
        assert st["last"] and not st["error"], st
        c = api("/api/vault/contents")
        keys = [k for its in c["kinds"].values() for it in its for k in it["keys"]]
        r = api("/api/vault/restore", {"keys": keys[:20], "on_conflict": "ask"})
        assert not r["conflicts"] and not r["restored"] and r["same"], r
    finally:
        api("/api/vault/settings", {"folder": ""})
    return f"{st['last']['files']} files, {st['raw'] / 1e6:.1f} MB kept in {st['stored'] / 1e6:.1f} MB"


def a_work():
    w = api("/api/work")
    assert "queue" in w and "held" in w
    try:   # Train tonight's Speaking / Singing reaches the queue (refused here, so nothing is queued)
        api("/api/work/add", {"kind": "new", "target": "Smoke Test", "mode": "humming"})
        raise AssertionError("a new voice was queued with mode humming")
    except urllib.error.HTTPError as e:
        assert "Speaking or Singing" in json.loads(e.read()).get("error", ""), e.code
    api("/api/work/candidates")
    p = api("/api/work/pause", {})
    assert p["held"] is True
    r = api("/api/work/resume", {})
    assert r["held"] is False
    was = w["settings"].get("start_at")
    s = api("/api/work/settings", {"start_at": "02:00"})                 # the Running room's time field: saved as set, read back as set
    assert s["settings"]["start_at"] == "02:00" and api("/api/work")["settings"]["start_at"] == "02:00" and not s["armed"], s["settings"]
    if was:
        api("/api/work/settings", {"start_at": was})
    return f"{len(w['queue'])} queued items; start time saved and read back"


def _tray_room(listing: str):
    """Roll-off deletes the oldest unkept track when a finish would make the tray 16, so a check that finishes a track
    on a server using the real folders must never run on a full tray (it would delete one of the user's tracks)."""
    n = len(api(listing))
    assert n < 15, (f"{listing} holds {n} finished tracks: finishing a test track would roll one of yours off. "
                    "Nothing was run; keep or delete some first, or test on the sandbox server.")


def _cover_target():
    """A song with cached stems (so no separation) and an installed voice."""
    songs = api("/api/library/songs")
    song = next((s for s in songs if s.get("stems")), None)
    voices = [v["name"] for v in api("/api/voices")]
    voice = next((v for v in voices if v.endswith("-uc")), voices[0] if voices else None)
    return song, voice


@check("cover: end to end, from a song with cached stems or the test song (then deleted)")
def a_cover():
    _tray_room("/api/history")
    song, voice = _cover_target()
    added = None
    test_song = Path(os.environ.get("UNDERCOVER_DATA_ROOT") or ROOT) / "test" / "smoke.wav"   # test/ isn't in git
    if not song and test_song.exists():   # an empty library: bring the test song, remove it after
        data = test_song.read_bytes()
        req = urllib.request.Request(BASE + "/api/upload", data=data, method="POST",
                                     headers={"X-Filename": "smoke-test-song.wav", "Origin": BASE,
                                              "Content-Type": "application/octet-stream"})
        with urllib.request.urlopen(req, timeout=120) as r:
            added = song = json.loads(r.read())
        song.setdefault("title", "smoke-test-song")
    try:
        return _cover_with(song, voice)
    finally:
        if added:
            api(f"/api/library/songs/{added['id']}", method="DELETE")   # the song and its stems


def _cover_with(song, voice):
    assert song and voice, "need a song (or test/smoke.wav) and a voice"
    before = {c["id"] for c in api("/api/library/covers")}
    job = api("/api/covers", {"song": song["id"], "voice": voice,
                              "options": {"fit": True, "karaoke": True, "dereverb": True, "quality": "high"}})["job"]
    wait_for(lambda: api("/api/status").get("cover_job") is None, 600, every=3, what="cover finished")
    new = [c for c in api("/api/library/covers") if c["id"] not in before]
    assert new, f"no new cover in the library (job {job})"
    c = new[-1]
    out = Path(c.get("output") or c.get("file") or "")
    size = out.stat().st_size if out.exists() else c.get("size", 0)
    assert size > 100_000, f"cover output looks empty ({size} bytes)"
    from undercover.frontman.remix import true_peak
    tp = true_peak(out) if out.exists() else None
    assert tp is None or tp <= -0.95, f"the cover peaks at {tp} dBTP as a player decodes it (ceiling -1)"   # phase 21
    import re
    assert re.fullmatch(r"[0-9a-f]{10}", c["id"]) and not c["kept"] and c["recent"], f"a new cover should be unkept and recent: {c}"
    assert c["id"] in {x["id"] for x in api("/api/history")}, "the new cover isn't in the recent tray"
    k = api(f"/api/library/covers/{c['id']}/keep", {})
    assert k["kept"] and k["kept_at"], k
    assert next(x for x in api("/api/history") if x["id"] == c["id"])["kept"], "kept isn't in the history list"
    assert not api(f"/api/library/covers/{c['id']}/unkeep", {})["kept"]
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", int(BASE.rsplit(":", 1)[1]), timeout=30)   # one connection, as the page uses
    try:
        conn.request("POST", f"/api/library/covers/{c['id']}/keep", body=b"{}", headers={"Content-Type": "application/json", "Origin": BASE})
        assert conn.getresponse().read()
        conn.request("GET", "/api/history", headers={"Origin": BASE})
        r = conn.getresponse()
        assert r.status == 200 and r.read().startswith(b"["), f"the request after a keep on the same connection failed: {r.status}"
        conn.request("POST", f"/api/library/covers/{c['id']}/unkeep", body=b"{}", headers={"Content-Type": "application/json", "Origin": BASE})
        conn.getresponse().read()
    finally:
        conn.close()
    api(f"/api/library/covers/{c['id']}", method="DELETE")
    assert c["id"] not in {x["id"] for x in api("/api/library/covers")}, "test cover not deleted"
    assert c["id"] not in {x["id"] for x in api("/api/history")}
    return f"{song['title'][:30]} with {voice}, {size / 1e6:.1f} MB"


@check("speech: SpeakEasy end to end, sentence by sentence (then deleted)")
def a_speech():
    from undercover.core.config import path
    _tray_room("/api/speak/recent")
    voices = api("/api/speak/voices")
    v = next((x for x in voices if x["trained"] and x["kind"] == "speech" and not x["undertrained"]), voices[0] if voices else None)
    assert v, "no voices"
    plan = api(f"/api/speak/plan?voice={v['name']}&cadence=0")   # the quick Reader path (cadence: a_cadence)
    assert plan["reader"] and plan["target_hz"], f"no speaking pitch / Reader for {v['name']}"
    est = api(f"/api/speak/estimate?voice={v['name']}&mode=reader&text=This%20is%20a%20smoke%20test.")   # before Generate
    assert est["estimate"] > 0 and est["mode"] == "reader", est
    r = api("/api/speak", {"text": "This is a smoke test. One, two, three.", "voice": v["name"], "cadence": False})
    req = urllib.request.Request(BASE + f"/api/jobs/{r['job']}/events")
    parts, final, progress = [], None, []
    with urllib.request.urlopen(req, timeout=300) as resp:
        for raw in resp:
            line = raw.decode("utf-8").strip()
            if not line.startswith("data: "):
                continue
            ev = json.loads(line[6:])
            if ev.get("frac") is not None and not ev.get("final"):
                progress.append(ev)
            if ev.get("stage") == "speaking":
                parts.append(ev)
            if ev.get("final"):
                final = ev
                break
    assert final and final["state"] == "done", f"speech job ended {final and final.get('state')}: {final and final.get('message')}"
    item = final["result"]
    assert len(parts) == 2 and all(p.get("url") for p in parts), f"expected 2 sentences with URLs, got {len(parts)}"
    assert progress and all(e.get("total_left") is not None for e in progress), "a speech progress event without total_left"
    wav = path("output_dir") / "speech" / item["file"]
    assert wav.exists() and wav.stat().st_size > 50_000, "speech WAV missing or empty"
    assert item.get("mp3_url"), "no MP3"
    assert item["id"] in {x["id"] for x in api("/api/library/speech")}, "not listed in SafeHouse"
    assert api(f"/api/speak/{item['id']}/keep", {})["kept"] and next(x for x in api("/api/speak/recent") if x["id"] == item["id"])["kept"]
    assert not api(f"/api/speak/{item['id']}/unkeep", {})["kept"]
    # the page reuses one connection: a POST route that doesn't read its body breaks the NEXT request ("501 {}GET")
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", int(BASE.rsplit(":", 1)[1]), timeout=30)
    try:
        for route in (f"/api/speak/{item['id']}/keep", f"/api/library/speech/{item['id']}/unkeep"):
            conn.request("POST", route, body=b"{}", headers={"Content-Type": "application/json", "Origin": BASE})
            resp = conn.getresponse()
            assert resp.status == 200, f"{route}: {resp.status}"
            resp.read()
            conn.request("GET", "/api/speak/recent", headers={"Origin": BASE})
            resp = conn.getresponse()
            assert resp.status == 200 and resp.read().startswith(b"["), f"the request after {route} on the same connection failed: {resp.status}"
    finally:
        conn.close()
    api(f"/api/speak/{item['id']}", method="DELETE")
    assert item["id"] not in {x["id"] for x in api("/api/speak/recent")} and not wav.exists(), "test speech not deleted"
    assert not (path("cache_dir") / "previews" / f"speech_{r['job']}").exists(), "sentence files left behind"
    return f"{v['name']} via {plan['reader']} {plan['pitch']:+} st, {item['duration']:.1f} s of speech in {item['seconds']['total']:.1f} s"


@check("jobs feed: /api/jobs while running and after, unbuffered stream, every page bar on the feed (phase 2c)")
def a_jobs_feed():
    _tray_room("/api/speak/recent")
    # Phase 2c's bug: bars didn't move live and work vanished on reload. The page now reads every job through jobs.js
    # (the stream, with /api/jobs as fallback and for finding work after a reload); these are the parts it relies on.
    web = ROOT / "undercover" / "web"
    own = [f.name for f in web.glob("*.js") if f.name != "jobs.js" and "new EventSource" in f.read_text(encoding="utf-8")]
    assert not own, f"{own} open their own job streams (no fallback, lost on reload): use watchJob from jobs.js"
    html = (web / "index.html").read_text(encoding="utf-8")
    assert html.index("/static/app.js") < html.index("/static/jobs.js") < html.index("/static/train.js"), "jobs.js must load after app.js, before the modules"
    voices = api("/api/speak/voices")
    v = next((x for x in voices if x["trained"] and x["kind"] == "speech" and not x["undertrained"]), voices[0] if voices else None)
    r = api("/api/speak", {"text": "Smoke test for the job list. It has two sentences.", "voice": v["name"]})
    job = r["job"]
    try:
        snap = next((j for j in api("/api/jobs") if j["id"] == job), None)
        assert snap and snap["kind"] == "speech", "a new job isn't in /api/jobs"
        assert snap["state"] in ("queued", "running") and v["display"] in snap["title"], f"unexpected snapshot {snap}"   # titles show the display name (phase 7)
        t0 = time.time()
        with urllib.request.urlopen(BASE + f"/api/jobs/{job}/events", timeout=60) as resp:
            assert resp.headers.get("X-Accel-Buffering") == "no", "event stream may be buffered on the way"
            first = resp.read(1024)
            assert first.startswith(b":") and time.time() - t0 < 5, "the stream's padding didn't arrive at once"
            for raw in resp:
                if raw.startswith(b"data: ") and json.loads(raw[6:]).get("final"):
                    break
        snap = next(j for j in api("/api/jobs") if j["id"] == job)
        assert snap["state"] == "done" and snap["final"]["state"] == "done" and snap["ended"], f"finished job shows as {snap}"
        assert snap["events"] >= 3 and snap["stage"] and (snap["progress"] is not None or snap.get("of")), \
            f"snapshot lacks progress fields: {snap}"
    finally:
        wait_for(lambda: next((j for j in api("/api/jobs") if j["id"] == job), {}).get("ended"), 120, what="speech finished")
        for s in api("/api/speak/recent"):
            if s["text"].startswith("Smoke test for the job list"):
                api(f"/api/speak/{s['id']}", method="DELETE")
    return f"{snap['events']} events, listed while running and after"


# ---------------------------------------------------------------- full: training with pause / resume

@check("training: quick run, pause, resume, cancel (scratch project)")
def a_training():
    from undercover.core.config import path
    src = None
    for p in api("/api/train/projects"):
        if p.get("kept_minutes", 0) >= 2 and p["train"].get("state") not in ("training", "queued"):
            src = p["name"]
            break
    assert src, "need a project with 2+ minutes kept"
    pdir = path("cache_dir") / "training"
    scratch = "zz-smoke-test"
    shutil.rmtree(pdir / scratch, ignore_errors=True)
    shutil.copytree(pdir / src, pdir / scratch, ignore=shutil.ignore_patterns("*.pth", "resume.pt", "previews", "raw"))
    pj = json.loads((pdir / scratch / "project.json").read_text(encoding="utf-8"))
    pj["name"], pj["train"] = scratch, {}
    (pdir / scratch / "project.json").write_text(json.dumps(pj), encoding="utf-8")
    try:
        api(f"/api/train/projects/{scratch}/train", {"voice_name": "zz-smoke-voice", "length": "quick", "fresh": True})
        epoch = lambda: api(f"/api/train/projects/{scratch}")["train"].get("epoch") or 0
        wait_for(lambda: epoch() >= 2, 900, every=5, what="epoch 2")
        api("/api/work/pause", {})
        wait_for(lambda: api(f"/api/train/projects/{scratch}")["train"].get("state") == "paused", 180, what="paused")
        e1 = epoch()
        api("/api/work/resume", {})
        wait_for(lambda: epoch() > e1, 300, every=5, what="training continued")
        st = api(f"/api/train/projects/{scratch}")
        if st.get("job"):
            api(f"/api/jobs/{st['job']['id']}/cancel", {})
        wait_for(lambda: not api(f"/api/train/projects/{scratch}").get("job"), 180, what="stopped")
        return f"paused at {e1}, continued past it"
    finally:
        time.sleep(2)
        shutil.rmtree(pdir / scratch, ignore_errors=True)
        from undercover.core.voices import remove_voice
        try:
            remove_voice("zz-smoke-voice")
        except Exception:   # noqa: BLE001 - only exists if export happened
            pass


CAD_VOICE, CAD_PROJ = "zz-smoke-cadence", "zz-smoke-cadence"


@check("cadence: transcripts + Train cadence on a scratch voice, then SpeakEasy reads with it (cleaned up)")
def a_cadence():
    """A small copy of a speech project (~100 clips) and of its voice: transcribe, measure, pick a reference, keep it;
    SpeakEasy then reads two sentences with the cadence. Everything is removed afterwards."""
    from undercover.core.config import path
    from undercover.core.voices import voices_dir, remove_voice
    pdir, vdir = path("cache_dir") / "training", voices_dir()
    src = next((v for v in ("morgan-freeman", "barack-obama") if (pdir / v / "project.json").exists()), None)
    assert src, "needs the morgan-freeman or barack-obama project"
    voice = {"morgan-freeman": "morgan-freeman-uc", "barack-obama": "barack-obama-uc"}[src]
    shutil.rmtree(pdir / CAD_PROJ, ignore_errors=True)
    shutil.rmtree(vdir / CAD_VOICE, ignore_errors=True)
    pj = json.loads((pdir / src / "project.json").read_text(encoding="utf-8"))
    kept = lambda p: (p.get("choice") if p.get("choice") is not None else not p.get("reason")) and not p.get("sung")
    phr = sorted(pj["phrases"], key=lambda p: (p["source"], p["start"]))
    take, n = [], 0
    for p in phr:   # whole sources until ~100 kept clips (stretches of talk need neighbours)
        if n >= 100 and take and p["source"] != take[-1]["source"]:
            break
        take.append(p)
        n += kept(p)
    (pdir / CAD_PROJ / "clips").mkdir(parents=True)
    for p in take:
        for f in (pdir / src / "clips" / f"{p['id']}{ext}" for ext in (".flac", ".wav")):   # FLAC since phase 24
            if f.exists():
                shutil.copyfile(f, pdir / CAD_PROJ / "clips" / f.name)
                break
    shutil.copyfile(pdir / src / "embeddings.npz", pdir / CAD_PROJ / "embeddings.npz")
    pj.update(name=CAD_PROJ, phrases=take, train={})
    (pdir / CAD_PROJ / "project.json").write_text(json.dumps(pj), encoding="utf-8")
    shutil.copytree(vdir / voice, vdir / CAD_VOICE, ignore=shutil.ignore_patterns("tts"))
    meta = json.loads((vdir / CAD_VOICE / "meta.json").read_text(encoding="utf-8"))
    meta["name"], meta["dir"] = CAD_VOICE, str(vdir / CAD_VOICE)
    meta["extra"]["project"] = CAD_PROJ
    meta["extra"].pop("tts", None)
    (vdir / CAD_VOICE / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    item = None
    try:
        # 1. transcripts as their own job (pausable; resuming does the rest)
        j = api(f"/api/train/projects/{CAD_PROJ}/transcribe", {})["job"]
        wait_for(lambda: next((x for x in api("/api/jobs") if x["id"] == j and x["state"] in ("done", "error")), None), 900, every=3,
                 what="transcripts")
        done = next(x for x in api("/api/jobs") if x["id"] == j)
        assert done["state"] == "done", done.get("final")
        tr = json.loads((pdir / CAD_PROJ / "transcripts.json").read_text(encoding="utf-8"))["clips"]
        assert len(tr) >= 50, f"only {len(tr)} transcripts"
        # 2. Train cadence, as SpeakEasy's button does (a queue item that runs now)
        w = api("/api/speak/cadence", {"voice": CAD_VOICE, "now": True})
        item = next(i for i in w["queue"] if i["kind"] == "cadence" and i["target"] == CAD_VOICE)
        st = lambda: next((i for i in api("/api/work")["queue"] if i["id"] == item["id"]), {})
        wait_for(lambda: st().get("state") in ("done", "failed", "skipped"), 1200, every=5, what="cadence item")
        res = st()
        assert res["state"] == "done", res.get("result")
        v = next(x for x in api("/api/speak/voices") if x["name"] == CAD_VOICE)
        assert v["cadence"]["has"], v["cadence"]
        sc = v["cadence"]["scores"]
        # phase 12: the readings' pace and pitch against the person, and the pack's nudge from it
        meta = json.loads((vdir / CAD_VOICE / "meta.json").read_text(encoding="utf-8"))["extra"]["tts"]
        assert sc.get("pace_ratio") and sc.get("pitch_st") is not None and sc.get("pace_heard") in ("fast", "slow", "ok"), sc
        assert abs(sc["pitch_off_st"]) <= 0.55 and (not meta.get("pitch_nudge") or abs(sc["pitch_off_st"]) <= 0.06), (meta.get("pitch_nudge"), sc)
        # 3. SpeakEasy reads with it
        _tray_room("/api/speak/recent")
        r = api("/api/speak", {"text": "It was not easy. But we did it together, and we finished the work.", "voice": CAD_VOICE})
        assert r["plan"]["mode"] == "cadence", r["plan"]
        job = r["job"]
        wait_for(lambda: next((x for x in api("/api/jobs") if x["id"] == job and x["state"] in ("done", "error")), None), 300, every=2,
                 what="cadence speech")
        sj = next(x for x in api("/api/jobs") if x["id"] == job)
        assert sj["state"] == "done", sj.get("final")
        sid = next(x for x in api("/api/speak/recent") if x["voice"] == CAD_VOICE)["id"]
        api(f"/api/speak/{sid}", method="DELETE")
        # 4. "Keep this one": a second pack waiting (the live one, copied as another clip), both read the A/B line on
        # the GPU, the page's verdict route keeps the new one; the old one is in tts/previous/
        tts = vdir / CAD_VOICE / "tts"
        live = json.loads((tts / "cadence.json").read_text(encoding="utf-8"))
        (tts / "pending").mkdir()
        shutil.copyfile(tts / "ref.wav", tts / "pending" / "ref.wav")
        (tts / "pending" / "cadence.json").write_text(json.dumps({**live, "clip": "smoke-other"}), encoding="utf-8")
        ab = api(f"/api/train/voices/{CAD_VOICE}/cadence")
        assert ab["pending"] and not ab["ready"], ab
        j = api(f"/api/train/voices/{CAD_VOICE}/cadence/ab", {})["job"]
        wait_for(lambda: next((x for x in api("/api/jobs") if x["id"] == j and x["state"] in ("done", "error")), None), 300, every=2,
                 what="A/B readings")
        assert next(x for x in api("/api/jobs") if x["id"] == j)["state"] == "done"
        ab = api(f"/api/train/voices/{CAD_VOICE}/cadence")
        assert ab["ready"], ab
        with urllib.request.urlopen(f"{BASE}/api/train/voices/{CAD_VOICE}/cadence/sample/pending", timeout=30) as r:
            wav = r.read()
        assert wav[:4] == b"RIFF" and len(wav) > 20000, len(wav)
        assert api(f"/api/train/voices/{CAD_VOICE}/cadence/ab", {}).get("ready"), "the readings were made again"
        r = api(f"/api/train/voices/{CAD_VOICE}/cadence/verdict", {"keep": "pending", "why": "smoke"})
        assert r["kept"] == "pending" and (tts / "previous" / "ref.wav").exists(), r
        v = next(x for x in api("/api/speak/voices") if x["name"] == CAD_VOICE)
        assert v["cadence"]["has"] and not v["cadence"].get("pending"), v["cadence"]
        return (f"{len(tr)} clips transcribed; likeness {sc.get('likeness')}, cadence gap {sc.get('cadence_gap')}, WER {sc.get('wer')}, "
                f"clip sound -{sc.get('sound')}, pace x{sc.get('pace_ratio')} ({sc.get('pace_heard')}), pitch {sc.get('pitch_raw_st'):+} st "
                f"(nudge {meta.get('pitch_nudge')}); A/B readings {len(wav) // 1000} KB, new pack kept")
    finally:
        if item:
            try:
                api("/api/work/remove", {"id": item["id"]})
            except Exception:   # noqa: BLE001
                pass
        for x in api("/api/speak/recent"):
            if x["voice"] == CAD_VOICE:
                api(f"/api/speak/{x['id']}", method="DELETE")
        time.sleep(1)
        try:
            remove_voice(CAD_VOICE)
        except Exception:   # noqa: BLE001
            shutil.rmtree(vdir / CAD_VOICE, ignore_errors=True)
        shutil.rmtree(pdir / CAD_PROJ, ignore_errors=True)


# ---------------------------------------------------------------- the sandbox server (phase 8)
#
# The end-to-end checks make and delete covers, speech, voices and training projects through the server's own routes.
# On a server that uses the real folders that is a risk (the incident of 2026-10-11), so smoke starts its own server on
# a port of its own (sandbox_port) with a config of its own: uploads, output, cache, logs and voices in a scratch folder, the model files
# read from the real models/ (never written), UNDERCOVER_TEST_ROOTS set so the app's delete/move guards refuse anything
# outside the scratch folder. The real folders are listed before and after the whole run and any difference fails it.

SANDBOX_PORTS = range(7862, 7870)   # chats in other worktrees run smoke too: each run takes the first free one


def _port_taken(port: int) -> bool:
    """Anything on 127.0.0.1:<port>: an UnderCover answering /api/status (as smoke always checked), or anything bound."""
    import socket
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=2)
        return True
    except OSError:
        pass
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def sandbox_port() -> int:
    """UNDERCOVER_SANDBOX_PORT if set (it must be free), else the first free port in 7862-7869."""
    import os
    want = os.environ.get("UNDERCOVER_SANDBOX_PORT")
    if want:
        port = int(want)
        if port in (7860, 7861):
            sys.exit(f"UNDERCOVER_SANDBOX_PORT={port}: 7860 is the user's server and 7861 is for manual looks. Pick another.")
        if _port_taken(port):
            sys.exit(f"Something already answers on port {port} (UNDERCOVER_SANDBOX_PORT). The sandbox server must be smoke's own.")
        return port
    for port in SANDBOX_PORTS:
        if not _port_taken(port):
            return port
    sys.exit(f"Ports {SANDBOX_PORTS.start}-{SANDBOX_PORTS.stop - 1} are all taken (other smoke runs?). Wait, or set UNDERCOVER_SANDBOX_PORT.")
WATCHED = ("uploads_dir", "output_dir", "models_dir", "cache_dir", "voices_dir", "themes_dir")


def real_dirs() -> dict:
    """The user's folders, read straight from the real config.toml (never through a config a test may have repointed).
    From a git worktree, UNDERCOVER_DATA_ROOT=<the main checkout> names the app folder whose data the sandbox copies
    from and whose folders are checked unchanged (the worktree's own config points at its empty folders); the sandbox
    server still runs the worktree's code."""
    import os
    from undercover.core import config
    base = Path(os.environ.get("UNDERCOVER_DATA_ROOT") or ROOT)
    cfg = config.read(base / "config.toml")["paths"]   # the defaults + that install's own file (only what differs)
    out = {k: (Path(v) if Path(v).is_absolute() else base / v) for k, v in cfg.items()}
    out.setdefault("voices_dir", out["models_dir"] / "voices")
    out.setdefault("themes_dir", out["uploads_dir"].parent / "themes")
    return out


def snapshot_real(dirs: dict) -> dict:
    """{file: (size, mtime_ns)} of everything in the user's library, model and working folders, and the settings files
    beside the config (config.toml, tutorial.json, phone.json, themes.json: a check once appended to the real config)."""
    import os
    snap = {}
    for name in ("config.toml", "config.before-defaults.toml", "tutorial.json", "phone.json", "themes.json", "ear.json"):
        p = ROOT / name
        if p.is_file():
            st = p.stat()
            snap[str(p)] = (st.st_size, st.st_mtime_ns)
    for k in WATCHED:
        for dp, _, files in os.walk(dirs[k]):
            for f in files:
                p = os.path.join(dp, f)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                snap[p] = (st.st_size, st.st_mtime_ns)
    return snap


def _sandbox_config(root: Path, real: dict) -> tuple[Path, dict]:
    """The sandbox's config.toml: the real settings with [paths] replaced. Asserted before anything starts: every
    folder but the read-only model files is inside `root`."""
    import re
    import tomllib
    mine = {"uploads_dir": root / "uploads", "output_dir": root / "output", "cache_dir": root / "cache",
            "logs_dir": root / "logs", "voices_dir": root / "voices", "themes_dir": root / "themes",
            "models_dir": real["models_dir"]}
    from undercover.core import config
    block = "[paths]\n" + "".join(f"{k} = '{v}'\n" for k, v in mine.items()) + "\n"
    user = (ROOT / "config.toml").read_text(encoding="utf-8") if (ROOT / "config.toml").is_file() else ""   # only what differs
    text = config.replace_section(user, "paths", block)
    cfg = root / "config.toml"
    cfg.write_text(text, encoding="utf-8")
    got = {k: Path(v) for k, v in tomllib.loads(text)["paths"].items()}
    assert got == mine, f"the sandbox config's [paths] isn't what was meant: {got}"
    from undercover.core import storage
    for k, p in got.items():
        p.mkdir(parents=True, exist_ok=True)
        if k == "models_dir":
            assert p == real["models_dir"], p
        else:
            assert storage._inside(p, root) and not storage._inside(p, ROOT), f"{k} -> {p} is outside the sandbox"
    return cfg, mine


def _sandbox_fixtures(root: Path, real: dict, mine: dict) -> str:
    """Copies (read from the real folders, written only into the sandbox) of one trained voice and its training
    project, so the cover, speech, training and cadence checks have something to work with."""
    tr = real["cache_dir"] / "training"
    src = next((v for v in ("morgan-freeman", "barack-obama") if (tr / v / "project.json").exists()), None)
    assert src, "the sandbox's training and cadence checks need the morgan-freeman or barack-obama project"
    voice = f"{src}-uc"
    vdst = mine["voices_dir"] / voice
    shutil.copytree(real["voices_dir"] / voice, vdst)
    meta = json.loads((vdst / "meta.json").read_text(encoding="utf-8"))
    meta["dir"], meta["pth"] = str(vdst), str(vdst / "model.pth")
    if meta.get("index"):
        meta["index"] = str(vdst / "model.index")
    (vdst / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    assert str(ROOT) not in json.dumps(meta), "a path into the real app folder is left in the copied voice's meta.json"
    pdst = mine["cache_dir"] / "training" / src
    pdst.mkdir(parents=True)
    for f in ("project.json", "embeddings.npz"):
        shutil.copyfile(tr / src / f, pdst / f)
    shutil.copytree(tr / src / "clips", pdst / "clips")
    return src


@contextlib.contextmanager
def sandbox_server(port: int):
    """Start a sandbox server, point this process's own config and the app's guards at the same scratch folders (so
    checks that touch folders directly stay inside it too), and stop and remove everything afterwards."""
    import os
    import subprocess
    import tempfile
    from undercover.core import config, storage
    global BASE
    if _port_taken(port):   # taken since it was picked (another run started in between)
        sys.exit(f"Something already answers on port {port}. The sandbox server must be smoke's own: try again.")
    real = real_dirs()
    root = Path(tempfile.mkdtemp(prefix="uc-sandbox-")).resolve()
    assert root.name.startswith("uc-sandbox-") and storage._inside(root, Path(tempfile.gettempdir()).resolve()) and not storage._inside(root, ROOT)
    saved = (dict(config.CONFIG["paths"]), config.CONFIG_FILE, storage.ALLOWED_ROOTS, BASE)
    proc = log = None
    try:
        cfg, mine = _sandbox_config(root, real)
        _sandbox_fixtures(root, real, mine)
        env = {**os.environ, "UNDERCOVER_CONFIG": str(cfg), "UNDERCOVER_TEST_ROOTS": str(root), "UNDERCOVER_PHONE_ADDR": PHONE_ADDR,
               "UNDERCOVER_NO_BACKFILL": "1"}   # (rc-preview: the checks expect the queue as they left it)
        env.pop("UNDERCOVER_TEST_ADDON", None)
        log = open(root / "server.log", "w", encoding="utf-8")
        proc = subprocess.Popen([sys.executable, "-m", "undercover.server", "--no-browser", "--port", str(port)], cwd=ROOT, env=env,
                                stdout=log, stderr=subprocess.STDOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        BASE = f"http://127.0.0.1:{port}"

        def up():
            if proc.poll() is not None:
                raise RuntimeError(f"the sandbox server stopped at start-up (exit {proc.returncode}); see {root / 'server.log'}")
            try:
                return api("/api/status", timeout=5)
            except OSError:
                return None
        wait_for(up, 120, every=1, what="the sandbox server answering")
        groups = api("/api/storage")["groups"]   # the running server's own idea of its folders, before any check touches it
        keys = {k: Path(v) for g in groups.values() for k, v in g["keys"].items()}
        for k, p in keys.items():
            if k == "models_dir":
                assert p == real["models_dir"], f"{k} -> {p}"
            else:
                assert storage._inside(p, root), f"the sandbox server's {k} is {p}, outside {root}: stopping before any check runs"
        config.CONFIG["paths"], config.CONFIG_FILE, storage.ALLOWED_ROOTS = {k: str(v) for k, v in mine.items()}, cfg, [root]
        yield root
    finally:
        config.CONFIG["paths"], config.CONFIG_FILE, storage.ALLOWED_ROOTS = saved[:3]
        stopped = proc is None
        if proc is not None:
            try:
                api("/api/quit", {}, timeout=10)
            except Exception:   # noqa: BLE001 - busy (409) or already gone: the process is ours, end it below
                pass
            try:
                proc.wait(60)
            except subprocess.TimeoutExpired:
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
                try:
                    proc.wait(30)
                except subprocess.TimeoutExpired:
                    pass
            stopped = proc.poll() is not None
        BASE = saved[3]
        if log:
            log.close()
        if stopped and storage._inside(root, Path(tempfile.gettempdir()).resolve()) and root.name.startswith("uc-sandbox-"):
            for _ in range(15):   # the cadence worker may still be closing its log when the server has gone
                shutil.rmtree(root, ignore_errors=True)
                if not root.exists():
                    break
                time.sleep(1)
            else:
                print(f"  (the sandbox's scratch folder couldn't be removed: {root})")
        else:
            print(f"  (the sandbox server is still running or its folder was left: {root})")


@check("the real folders (uploads, output, models, cache, voices) and settings files are exactly as they were when this run started")
def a_untouched(before: dict, dirs: dict):
    after = snapshot_real(dirs)
    added = sorted(set(after) - set(before))
    gone = sorted(set(before) - set(after))
    changed = sorted(p for p in set(before) & set(after) if before[p] != after[p])
    if added or gone or changed:
        lines = [f"+ {p}" for p in added[:6]] + [f"- {p}" for p in gone[:6]] + [f"~ {p}" for p in changed[:6]]
        raise AssertionError(f"{len(added)} added, {len(gone)} removed, {len(changed)} changed:\n    " + "\n    ".join(lines))
    return f"{len(after)} files compared"


def main():
    global BASE
    for stream in (sys.stdout, sys.stderr):   # a Windows console (cp1252) can't print "≥" in a check's note
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, help="use a server that is already running (read-only API checks only)")
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--unit-only", action="store_true")
    ap.add_argument("--no-cover", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()
    if a.port == 7860:
        sys.exit("Refusing to run against the user's server (7860).")
    if a.port and a.full:
        sys.exit("--full makes and deletes covers, speech and voices: it only runs on the sandbox server smoke starts itself "
                 f"(ports {SANDBOX_PORTS.start}-{SANDBOX_PORTS.stop - 1}). Leave --port out.")
    t0 = time.time()
    dirs = real_dirs()
    before = snapshot_real(dirs)   # the whole run is judged against this: the user's folders must come out the same
    print("unit checks")
    for fn in (u_imports, u_deps, u_fit, u_window, u_phone, u_stems, u_sources, u_speak, u_paused, u_vault, u_gamepause, u_da_mode, u_write_guard, u_demo_pause, u_demo_topup, u_cadence_starts, u_listenback, u_improve_pending, u_improved_keep, u_pending_guard, u_no_voice_deletes, u_export_lands, u_aside_list, u_remove_aside, u_duplicate_aside, u_move_rollback, u_aside_gaps, u_p7_1, u_cadence_chain, u_queue_pack, u_report_words, u_person_guess, u_tonight, u_p8_small, u_p9, u_start_time_sticks, u_start_at_hold, u_frozen_bar, u_frozen_recipe, u_improve_estimate, u_person_search, u_source_selected, u_reader_print, u_compare_priced, u_version, u_plankton_reads, u_phrase_list_trains, u_config_layers, u_layout, u_launcher_moved, u_character_check, u_prep_progress, u_retry_status, u_installer_python, u_installer_failure, u_installer_space, u_installer_quiet, u_installer_downloads, u_rcfix7, u_rcpolish1, u_rcpreview, u_rceta, u_rcfix10, u_meta_keys, u_uninstall_kept_paths, u_models_progress, u_model_download_said, u_frozen_files, u_frozen_gate_project, u_queue_live, u_arm_paused, u_voice_ask, u_eta_narrow, u_speak_estimate, u_project_pause_holds, u_cadence_sample_range, u_stop_download, u_drawl, u_voice_name_guard, u_project_queue, u_to_queue, u_preview_fit, u_oneshot_options, u_estimates, u_listenback_fetch, u_lossless, u_smaller, u_shrink_export, u_tailnet, u_phone_away, u_cadence, u_eta, u_addons, u_ytdlp, u_storage,
               u_rolloff_covers, u_rolloff_speech, u_stable_ids, u_migrate_ids,
               u_display_names, u_person, u_grammar, u_ambiguity, u_ask_flow, u_clipsound, u_keep_this_one, u_relocate_guard, u_pace_pitch, u_characters, u_leadgate, u_yield, u_likeness_word, u_ear, u_golden,
               u_doctor_fast, u_doctor_models, u_doctor_startup, u_doctor_resume, u_doctor_stale, u_doctor_packages,
               u_doctor_interrupted, u_doctor_bundle, u_qr, u_phone_pairing, u_phone_routes, u_phone_doctor, u_themes, u_themes_shipped, u_ytdlp_heal, u_firstrun, u_no_assignment, u_desk_plan, u_page_rules, u_page_syntax, u_platform, u_tray):
        fn()
    if not a.unit_only:
        if a.port:   # someone else's server (7861 for a manual look): nothing that writes
            BASE = f"http://127.0.0.1:{a.port}"
            print(f"API checks ({BASE}, reads only: anything that makes, deletes or pauses work runs on the sandbox only)")
            for fn in (a_status, a_page, a_page_console, a_lists, a_library, a_train):
                fn()
        else:
            port = sandbox_port()
            print(f"API checks (a sandbox server on port {port}: scratch folders, the real models read-only)")
            try:
                # away from home's checks drive a fake Tailscale (tests/fake_tailscale.py): the sandbox server inherits these
                import tempfile
                os.environ["UNDERCOVER_TAILSCALE_FAKE"] = str(ROOT / "tests" / "fake_tailscale.py")
                fake_ts = Path(tempfile.mkdtemp(prefix="uc-fake-ts-"))   # smoke's own scratch folder, removed after
                os.environ["FAKE_TS_STATE"] = str(fake_ts / "state.json")
                with sandbox_server(port):
                    for fn in (a_status, a_page, a_page_console, a_lists, a_library, a_train, a_work, a_vault, a_doctor, a_retry, a_stop_separation, a_presep_progress, a_phone, a_away, a_themes, a_firstrun, a_tray):
                        fn()
                    if not a.no_cover:
                        a_cover()
                        a_desk_oneshot()
                        a_speech()
                        a_jobs_feed()
                    if a.full:
                        a_training()
                        a_cadence()
            except Exception as e:   # noqa: BLE001 - the sandbox couldn't start: that is a failed run, not a crash
                RESULTS.append((False, "sandbox server"))
                print(f"  FAIL  sandbox server: {type(e).__name__}: {e}", flush=True)
            finally:
                fake_ts = Path(os.environ.get("FAKE_TS_STATE", "")).parent
                if fake_ts.name.startswith("uc-fake-ts-"):
                    import shutil
                    shutil.rmtree(fake_ts, ignore_errors=True)
    a_untouched(before, dirs)
    failed = [n for ok, n in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed in {time.time() - t0:.0f} s" + (f"; FAILED: {', '.join(failed)}" if failed else ""))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
