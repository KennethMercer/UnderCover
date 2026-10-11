"""Smoke test: does UnderCover still work end to end? Run after every phase of a restructure and before merging.

    .venv\\Scripts\\python.exe tools\\smoke.py               # unit + API + one cover + one speech (~2 min, test server on 7861)
    .venv\\Scripts\\python.exe tools\\smoke.py --full        # + a quick training with Pause/Resume, transcripts + cadence (~13 min)
    .venv\\Scripts\\python.exe tools\\smoke.py --unit-only   # no server needed (~20 s)

Needs the TEST server running on port 7861 (preview config "undercover-test"); refuses the user's server on 7860.
Leaves nothing behind: the test cover and test speech are deleted, the scratch training project is removed.
Exit code 0 = all passed. Prints one line per check.

RULE (after 2026-10-11, when a check here deleted the user's songs and covers): a check must never be able to reach
the user's data folders (uploads, output, models, cache, training projects). Unit checks that create, move or delete
files run inside `isolated_data()`: every [paths] folder, config.toml and the storage allow-list point into a temp
dir, and core/storage.py refuses the app's real folders in test mode. Never copy the real config.toml into a test.
"""
import argparse
import contextlib
import json
import shutil
import sys
import time
import traceback
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

RESULTS: list = []


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
    return f"low voice -> octave {fit['octave']} key {fit['key']:+d}"


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


# ---------------------------------------------------------------- API checks (test server)

@check("jobs: a paused job saves its resume point; a paused one-shot resumes its own song and training")
def u_paused():
    """Known bug fixed in phase 4: run_job's Paused branch returned before worklist.remember(job)."""
    from undercover.core import jobs, worklist
    from undercover.core.jobs import Job, Paused
    from undercover.core.config import path
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
        assert worklist._state()["resume"] == [job.resume_spec], worklist._state()["resume"]
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
    assert {"chatterbox", "f5"} <= set(ENGINES) and default_engine() in ENGINES
    ids = {a["id"] for a in addons.status()}
    assert {"f5", "xtts"} <= ids, ids
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
    import tempfile
    from undercover.core import config, storage
    saved = (dict(config.CONFIG["paths"]), config.CONFIG_FILE, storage.ALLOWED_ROOTS)
    with tempfile.TemporaryDirectory(prefix="uc-smoke-") as td:
        t = Path(td)
        app = t / "app"
        keys = {"models_dir": "models", "cache_dir": "cache", "output_dir": "output", "logs_dir": "logs", "uploads_dir": "uploads"}
        cfg = t / "config.toml"   # a minimal config of its own: never a copy of the real one (it holds the real paths)
        cfg.write_text("[paths]\n" + "".join(f"{k} = '{app / v}'\n" for k, v in keys.items())
                       + '\n[cadence]\nengine = "chatterbox"\n', encoding="utf-8")
        try:
            config.CONFIG_FILE = cfg
            config.CONFIG["paths"] = {k: str(app / v) for k, v in keys.items()}
            storage.ALLOWED_ROOTS = [t]
            for k in (*keys, "voices_dir"):
                p = config.path(k)
                assert storage._inside(p, t) and not storage._inside(p, ROOT), f"{k} -> {p} is outside the test's folder"
            yield t
        finally:
            config.CONFIG["paths"], config.CONFIG_FILE, storage.ALLOWED_ROOTS = saved


@check("add-ons: registry, CLI list, a fake add-on installs (checksum) and removes cleanly")
def u_addons():
    import subprocess
    from undercover.core import addons
    from undercover.core.config import path
    out = subprocess.run([sys.executable, "-m", "undercover.addons", "list", "--json"], cwd=ROOT, capture_output=True,
                         text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-400:]
    rows = {r["id"]: r for r in json.loads(out.stdout)}
    assert {"f5", "xtts", "chatterbox", "whisper", "kokoro", "rvc", "separation", "speaker"} <= set(rows), sorted(rows)
    assert rows["f5"]["installable"] and not rows["xtts"]["installable"] and rows["kokoro"]["included"], rows["xtts"]
    with isolated_data() as t:
        src = t / "fake.bin"
        src.write_bytes(b"undercover smoke " * 4096)
        fake = addons.register_test_addon(src)
        dest = path("models_dir") / "_test" / "test-addon.bin"
        try:
            for aid, ok in (("test", False), ("chatterbox", True)):   # no acceptance; an included part
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
        real = ROOT / "constraints.txt"
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
                storage.remove_files([], within=[t], dirs=[ROOT / "output"])
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
    assert sorted(c.split(":")[0] for c in calls["remove_files("]) == ["frontman/history.py", "speakeasy/speak.py"], calls
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
            wavs = lambda: sorted(p.name for p in speak.speech_dir().glob("*.wav"))
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


@check("api: status ready")
def a_status():
    s = wait_for(lambda: (lambda d: d if d.get("models") == "ready" else None)(api("/api/status")), 180, what="models ready")
    assert s.get("gpu"), "no GPU reported"
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
    for part in ('id="suite-logo"', 'id="chip-queue"', 'id="workbar"', 'id="q-drawer"', 'id="q-panel"', 'id="wa-main"'):
        assert part in html, f"missing {part}"
    files = re.findall(r'(?:src|href)="(/static/[^"]+)"', html)
    for f in files:
        assert len(api(f)) > 100, f"{f} is empty"
    return f"4 modules, {len(files)} static files"


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
    for k in ("covers", "speech", "songs", "voices", "training"):
        assert isinstance(api(f"/api/library/{k}"), list), k
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
def a_work():
    w = api("/api/work")
    assert "queue" in w and "held" in w
    api("/api/work/candidates")
    p = api("/api/work/pause", {})
    assert p["held"] is True
    r = api("/api/work/resume", {})
    assert r["held"] is False
    return f"{len(w['queue'])} queued items"


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


@check("cover: end to end from cached stems (then deleted)")
def a_cover():
    _tray_room("/api/history")
    song, voice = _cover_target()
    added = None
    if not song and (ROOT / "test" / "smoke.wav").exists():   # an empty library: bring the test song, remove it after
        data = (ROOT / "test" / "smoke.wav").read_bytes()
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
        assert snap["state"] in ("queued", "running") and v["name"] in snap["title"], f"unexpected snapshot {snap}"
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
        f = pdir / src / "clips" / f"{p['id']}.wav"
        if f.exists():
            shutil.copyfile(f, pdir / CAD_PROJ / "clips" / f.name)
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
        return f"{len(tr)} clips transcribed; likeness {sc.get('likeness')}, cadence gap {sc.get('cadence_gap')}, WER {sc.get('wer')}"
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


def main():
    global BASE
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=7861)
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--unit-only", action="store_true")
    ap.add_argument("--no-cover", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()
    if a.port == 7860:
        sys.exit("Refusing to run against the user's server (7860). Start the test server on 7861.")
    BASE = f"http://127.0.0.1:{a.port}"
    t0 = time.time()
    print("unit checks")
    for fn in (u_imports, u_deps, u_fit, u_window, u_phone, u_stems, u_sources, u_speak, u_paused, u_cadence, u_eta, u_addons, u_storage,
               u_rolloff_covers, u_rolloff_speech, u_stable_ids, u_migrate_ids):
        fn()
    if not a.unit_only:
        print(f"API checks ({BASE})")
        for fn in (a_status, a_page, a_lists, a_library, a_train, a_work):
            fn()
        if not a.no_cover:
            a_cover()
            a_speech()
            a_jobs_feed()
        if a.full:
            a_training()
            a_cadence()
    failed = [n for ok, n in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed in {time.time() - t0:.0f} s" + (f"; FAILED: {', '.join(failed)}" if failed else ""))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
