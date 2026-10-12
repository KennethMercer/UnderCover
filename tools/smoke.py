"""Smoke test: does UnderCover still work end to end? Run after every phase of a restructure and before merging.

    .venv\\Scripts\\python.exe tools\\smoke.py               # unit + API + one cover + one speech (~3 min, on its own sandbox server)
    .venv\\Scripts\\python.exe tools\\smoke.py --full        # + a quick training with Pause/Resume, transcripts + cadence (~13 min)
    .venv\\Scripts\\python.exe tools\\smoke.py --unit-only   # no server needed (~20 s)
    .venv\\Scripts\\python.exe tools\\smoke.py --port 7861   # read-only API checks against a server that is already running

Since phase 8 the checks that make and delete things (cover, speech, training, cadence) run on a SANDBOX server that smoke
starts itself on port 7862: scratch uploads / output / cache / logs / voices (a copy of one voice and its training project),
the real models/ read-only, and the app's delete guards limited to the scratch folder. Stop anything on 7862 first. The
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


@check("yt-dlp Update (phase 8): a pinned, hashed install; no pin or nothing newer = no install; the date is recorded")
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

    def post():
        h = _FakeHandler()
        assert shapi.handle(h, "POST", "/api/library/tools/ytdlp-update")
        return h
    addons._pip, addons.package_version = fake_pip, lambda n: state["v"]
    try:
        with isolated_data():
            assert addons.updated("ytdlp") is None and library.ytdlp_info()["ytdlp_update"] is True
            h = post()
            assert not h.err and h.out == {"before": "2026.1.1", "after": ver, "pinned": ver, "changed": True}, (h.out, h.err)
            args, req = calls[-1]
            assert len(calls) == 1 and req == f"yt-dlp=={ver} --hash=sha256:{sha}\n", (calls, req)
            assert "--require-hashes" in args and "--no-deps" in args and "--upgrade" not in args, args
            assert args[args.index("-c") + 1] == str(ROOT / "constraints.txt") and "yt-dlp" not in args, args
            import re
            assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d", addons.updated("ytdlp") or ""), addons.updated("ytdlp")
            info = library.ytdlp_info()
            assert info["ytdlp"] == info["ytdlp_pinned"] == ver and info["ytdlp_update"] is False and info["ytdlp_updated"], info
            h = post()   # equal: nothing to do
            assert h.out["changed"] is False and len(calls) == 1, (h.out, calls)
            state["v"] = "2099.1.1"   # newer than the pin: left alone
            h = post()
            assert h.out["changed"] is False and len(calls) == 1 and library.ytdlp_info()["ytdlp_update"] is False
            state["v"] = None   # not installed: that would be an install, which needs an OK
            h = post()
            assert h.err and len(calls) == 1 and library.ytdlp_info()["ytdlp_update"] is False, (h.err, calls)
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
            d = int(sr * rng.uniform(0.05, 0.12))
            fr = sosfilt(butter(4, [4000, 12000], "band", fs=sr, output="sos"), rng.standard_normal(d)) * np.hanning(d)
            y[t0:t0 + d] += (0.3 * fr).astype(np.float32)
            t0 += d
        t0 += int(sr * (rng.uniform(0.25, 0.6) if rng.random() < 0.2 else rng.uniform(0.02, 0.06)))
    y += (rng.standard_normal(n) * 1e-5).astype(np.float32)
    return (0.8 * y / np.abs(y).max()).astype(np.float32), sr


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
        assert V.voice_info(V.get_voice("barack-obama-uc")) == {"display": "Barack Obama", "trained": True}
        assert V.voice_info(V.get_voice("mj")) == {"display": "Michael Jackson", "trained": False}
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
    keep = (service.installed_for, service.online_for)
    service.installed_for, service.online_for = (lambda p: []), (lambda p, n=2: [])   # no network, no voices
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
        service.installed_for, service.online_for = keep
        iso.__exit__(None, None, None)
        while not jobs.GPU_QUEUE.empty():
            jobs.GPU_QUEUE.get_nowait()
        for j in made:
            jobs.JOBS.pop(j.id, None)
        A.reset()
    return "asks for Hello, remembers Lionel Richie, offers Adele, forgets on Toxic"


@check("api: status ready")
def a_status():
    s = wait_for(lambda: (lambda d: d if d.get("models") == "ready" else None)(api("/api/status")), 180, what="models ready")
    assert s.get("gpu"), "no GPU reported"
    assert "ytdlp_updated" in s, "/api/status doesn't carry ytdlp_updated"
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
    for part in ('id="suite-logo"', 'id="room-running"', 'id="workbar"', 'id="q-drawer"', 'id="q-panel"', 'id="wa-main"'):
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


@check("cover: end to end, from a song with cached stems or the test song (then deleted)")
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
                f"clip sound -{sc.get('sound')}; A/B readings {len(wav) // 1000} KB, new pack kept")
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
# SANDBOX_PORT with a config of its own: uploads, output, cache, logs and voices in a scratch folder, the model files
# read from the real models/ (never written), UNDERCOVER_TEST_ROOTS set so the app's delete/move guards refuse anything
# outside the scratch folder. The real folders are listed before and after the whole run and any difference fails it.

SANDBOX_PORT = 7862
WATCHED = ("uploads_dir", "output_dir", "models_dir", "cache_dir", "voices_dir")


def real_dirs() -> dict:
    """The user's folders, read straight from the real config.toml (never through a config a test may have repointed)."""
    import tomllib
    cfg = tomllib.loads((ROOT / "config.toml").read_text(encoding="utf-8"))["paths"]
    out = {k: (Path(v) if Path(v).is_absolute() else ROOT / v) for k, v in cfg.items()}
    out.setdefault("voices_dir", out["models_dir"] / "voices")
    return out


def snapshot_real(dirs: dict) -> dict:
    """{file: (size, mtime_ns)} of everything in the user's library, model and working folders."""
    import os
    snap = {}
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
            "logs_dir": root / "logs", "voices_dir": root / "voices", "models_dir": real["models_dir"]}
    block = "[paths]\n" + "".join(f"{k} = '{v}'\n" for k, v in mine.items()) + "\n"
    text = re.sub(r"(?ms)^\[paths\][ \t]*$.*?(?=^\[|\Z)", lambda _: block, (ROOT / "config.toml").read_text(encoding="utf-8"), count=1)
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
def sandbox_server(port: int = SANDBOX_PORT):
    """Start a sandbox server, point this process's own config and the app's guards at the same scratch folders (so
    checks that touch folders directly stay inside it too), and stop and remove everything afterwards."""
    import os
    import subprocess
    import tempfile
    from undercover.core import config, storage
    global BASE
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=2)
        sys.exit(f"Something already answers on port {port}. The sandbox server must be smoke's own: stop that one first.")
    except OSError:
        pass
    real = real_dirs()
    root = Path(tempfile.mkdtemp(prefix="uc-sandbox-")).resolve()
    assert root.name.startswith("uc-sandbox-") and storage._inside(root, Path(tempfile.gettempdir()).resolve()) and not storage._inside(root, ROOT)
    saved = (dict(config.CONFIG["paths"]), config.CONFIG_FILE, storage.ALLOWED_ROOTS, BASE)
    proc = log = None
    try:
        cfg, mine = _sandbox_config(root, real)
        _sandbox_fixtures(root, real, mine)
        env = {**os.environ, "UNDERCOVER_CONFIG": str(cfg), "UNDERCOVER_TEST_ROOTS": str(root)}
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


@check("the real folders (uploads, output, models, cache, voices) are exactly as they were when this run started")
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
                 f"(port {SANDBOX_PORT}). Leave --port out.")
    t0 = time.time()
    dirs = real_dirs()
    before = snapshot_real(dirs)   # the whole run is judged against this: the user's folders must come out the same
    print("unit checks")
    for fn in (u_imports, u_deps, u_fit, u_window, u_phone, u_stems, u_sources, u_speak, u_paused, u_cadence, u_eta, u_addons, u_ytdlp, u_storage,
               u_rolloff_covers, u_rolloff_speech, u_stable_ids, u_migrate_ids,
               u_display_names, u_person, u_grammar, u_ambiguity, u_ask_flow, u_clipsound, u_keep_this_one, u_relocate_guard):
        fn()
    if not a.unit_only:
        if a.port:   # someone else's server (7861 for a manual look): nothing that writes
            BASE = f"http://127.0.0.1:{a.port}"
            print(f"API checks ({BASE}, reads only: anything that makes, deletes or pauses work runs on the sandbox only)")
            for fn in (a_status, a_page, a_lists, a_library, a_train):
                fn()
        else:
            print(f"API checks (a sandbox server on port {SANDBOX_PORT}: scratch folders, the real models read-only)")
            try:
                with sandbox_server():
                    for fn in (a_status, a_page, a_lists, a_library, a_train, a_work):
                        fn()
                    if not a.no_cover:
                        a_cover()
                        a_speech()
                        a_jobs_feed()
                    if a.full:
                        a_training()
                        a_cadence()
            except Exception as e:   # noqa: BLE001 - the sandbox couldn't start: that is a failed run, not a crash
                RESULTS.append((False, "sandbox server"))
                print(f"  FAIL  sandbox server: {type(e).__name__}: {e}", flush=True)
    a_untouched(before, dirs)
    failed = [n for ok, n in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed in {time.time() - t0:.0f} s" + (f"; FAILED: {', '.join(failed)}" if failed else ""))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
