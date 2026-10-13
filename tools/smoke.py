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
            assert args[args.index("-c") + 1] == str(ROOT / "constraints.txt") and "yt-dlp" not in args, args
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
    # the First assignment's (phase 17: only <voices>/.agent-smoke-seeding, an interrupted seed's leftover); Vaulting's
    # pruning (two calls: old snapshots, then unreferenced objects, only inside the vault folder); a new caller must be
    # added here on purpose
    assert sorted(c.split(":")[0] for c in calls["remove_files("]) == ["app/tutorial.py", "core/doctor.py", "core/themes.py",
                                                                        "core/vault.py", "core/vault.py", "core/ytdlp.py",
                                                                        "frontman/history.py", "speakeasy/speak.py"], calls
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
        ready, speak.engine_ready = speak.engine_ready, lambda e: True
        try:
            p = speak.plan("zz-nudge-uc", nudge=1.0)
        finally:
            speak.engine_ready = ready
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
            pool = sponge if any(w in q for w in ("interview", "speech", "podcast", "talks")) else sponge[4:]
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
    return f"hook {inst:.1f} s left as it is, chorus {ch:.1f} s converted, singing untouched, stems unchanged"


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
        assert V.voice_info(V.get_voice("barack-obama-uc")) == {"display": "Barack Obama", "trained": True, "demo": False, "ear": None}
        assert V.voice_info(V.get_voice("mj")) == {"display": "Michael Jackson", "trained": False, "demo": False, "ear": None}
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
        cold_cfg.write_text(re.sub(r"(?ms)^\[paths\][ \t]*$.*?(?=^\[|\Z)", lambda _: block,
                                   (ROOT / "config.toml").read_text(encoding="utf-8"), count=1), encoding="utf-8")
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
            assert r["resumed"] and st["resume"] == [train] and not st["held"], (r, st)
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


@check("First assignment: state once per install, the seeds (sample song, demo voice), done, reset; the demo voice is nobody to search for")
def u_tutorial():
    import numpy as np
    import soundfile as sf
    from undercover.app import tutorial
    from undercover.core import voices
    from undercover.core.voices import ModelError
    from undercover.doubleagent import cadence, night, service
    saved = (tutorial.SAMPLE, tutorial.DEMO)
    with isolated_data() as t:
        try:
            # no audio in the shipped folder: no sample song, nothing breaks
            empty = t / "assets" / "sample-none"
            empty.mkdir(parents=True)
            (empty / "sample.json").write_text(json.dumps({"title": "x", "file": "sample.flac"}), encoding="utf-8")
            tutorial.SAMPLE, tutorial.DEMO = empty, t / "assets" / "no-voice"
            assert tutorial.sample_info() is None and tutorial.seed() == {"song": None, "voice": None}
            # a fresh install: done = null, both seeds
            sample = t / "assets" / "sample"
            sample.mkdir(parents=True)
            sf.write(sample / "sample.wav", (0.1 * np.sin(np.linspace(0, 880 * np.pi, 44100 * 3))).astype(np.float32), 44100)
            (sample / "sample.json").write_text(json.dumps({"title": "The Drop", "artist": "UnderCover", "file": "sample.wav",
                                                            "seconds": 3, "licence": "ours", "notice": "n"}), encoding="utf-8")
            _fake_demo_voice(t / "assets" / "demo-voice" / "agent-smoke")
            tutorial.SAMPLE, tutorial.DEMO = sample, t / "assets" / "demo-voice" / "agent-smoke"
            assert tutorial.state_file().parent == t and tutorial.load() is None
            assert tutorial.first_run() == {"done": None}
            sid = tutorial.sample_song_id()
            assert sid, "the sample song wasn't added"
            meta = json.loads((config_path("uploads_dir") / f"{sid}.json").read_text(encoding="utf-8"))
            assert meta["sample"] is True and meta["title"] == "The Drop", meta
            assert (sample / "sample.wav").exists(), "the shipped audio was moved"
            v = voices.get_voice("agent-smoke")
            info = voices.voice_info(v)
            assert info["demo"] is True and info["trained"] is False, info
            assert not [d for d in voices.voices_dir().iterdir() if d.name.startswith(".")], "a seeding folder was left"
            assert tutorial.first_run() == {"done": None}   # once
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
            # done, then Replay after the user deleted both: they come back, the brief shows again
            assert tutorial.mark_done()["tutorial"]["done"] == time.strftime("%Y-%m-%d")
            for f in config_path("uploads_dir").glob(f"{sid}.*"):
                f.unlink()
            voices.remove_voice("agent-smoke")
            r = tutorial.reset()
            assert r["tutorial"] == {"done": None} and r["seeded"]["song"] and r["seeded"]["voice"] == "agent-smoke", r
            assert tutorial.reset()["seeded"] == {"song": tutorial.sample_song_id(), "voice": "agent-smoke"}   # nothing doubled
            assert len([m for m in config_path("uploads_dir").glob("*.json")
                        if json.loads(m.read_text(encoding="utf-8")).get("sample")]) == 1
            # the user already has the very recording: it's marked as the sample, their title kept, nothing doubled
            for f in config_path("uploads_dir").glob(f"{tutorial.sample_song_id()}.*"):
                if f.suffix == ".json":
                    m = json.loads(f.read_text(encoding="utf-8"))
                    m.pop("sample")
                    m["title"] = "My take"
                    f.write_text(json.dumps(m), encoding="utf-8")
            assert tutorial.sample_song_id() is None
            sid2 = tutorial.seed_song()
            m = json.loads((config_path("uploads_dir") / f"{sid2}.json").read_text(encoding="utf-8"))
            assert m["sample"] is True and m["title"] == "My take", m
            assert not list(config_path("uploads_dir").glob("upload_sample_*")), "a temp copy was left"
            # the shipped files must match their manifest (they aren't in git): a changed or missing one isn't seeded
            voices.remove_voice("agent-smoke")
            (tutorial.DEMO / "model.pth").write_bytes(b"something else entirely")
            assert "manifest" in (tutorial.demo_files() or "") and tutorial.seed_voice() is None
            (tutorial.DEMO / "model.pth").unlink()
            assert "make_demo_voice" in (tutorial.demo_files() or "") and tutorial.seed_voice() is None
            (tutorial.DEMO / "model.pth").write_bytes(b"not really a model")
            assert tutorial.demo_files() is None and tutorial.seed_voice() == "agent-smoke"
            # a folder of that name that isn't the demo is never touched
            voices.remove_voice("agent-smoke")
            other = voices.voices_dir() / "agent-smoke"
            other.mkdir()
            (other / "mine.txt").write_text("the user's", encoding="utf-8")
            assert tutorial.seed_voice() is None and (other / "mine.txt").exists()
        finally:
            tutorial.SAMPLE, tutorial.DEMO = saved
    with isolated_data():   # an install that already made covers / trained voices: marked, nothing seeded
        _fake_voice("someone-uc", extra={"trained": True, "person": "Someone"})
        assert tutorial.first_run() == {"done": "existing install"} and tutorial.sample_song_id() is None
        assert not voices.voices_dir().joinpath("agent-smoke").exists()
    import inspect
    from undercover.doubleagent import api as da_api   # "Do it all" called a local function named like the module it uses
    assert "def service(" not in inspect.getsource(da_api.start_auto), "start_auto shadows the service module again"
    why = tutorial.demo_files()   # the real shipped demo voice: its files are made by tools/make_demo_voice.py, not kept in git
    return ("fresh: done null + both seeds; existing install; done; reset re-seeds once; manifest checked; never searched for; "
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
            target = ROOT / "UnderCover.bat"
            assert not platform.startup_entry_state(d)
            assert platform.startup_entry(True, target, folder=d) and platform.startup_entry_state(d)
            assert not platform.startup_entry(False, target, folder=d) and not platform.startup_entry_state(d)
    # another OS: the plain answers
    saved = platform.WINDOWS
    platform.WINDOWS = False
    try:
        assert platform.supports("firewall")[0] is False and platform.supports("tray")[0] is False
        assert platform.keep_awake(True) is False
        for fn, args in ((platform.startup_folder, ()), (platform.network_and_firewall, ("192.168.1.5",)),
                         (platform.video_controllers, ())):
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
    ("POST", "/api/history/0123456789/again"),
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
    *[("GET", f"/api/speak/{a}") for a in ("voices", "readers", "recent", "plan")],
    ("POST", "/api/speak"), ("POST", "/api/speak/cadence"), ("POST", "/api/speak/newvoice"),
    *[("POST", f"/api/speak/0123456789/{a}") for a in ("rename", "keep", "unkeep")], ("DELETE", "/api/speak/0123456789"),
    *[("GET", f"/api/library/{a}") for a in ("summary", "covers", "speech", "songs", "voices", "training")],
    ("GET", "/api/library/covers/zip"),
    *[("POST", f"/api/library/{k}/0123456789/{a}") for k in ("covers", "speech") for a in ("rename", "keep", "unkeep")],
    ("DELETE", "/api/library/covers/0123456789"), ("DELETE", "/api/library/speech/0123456789"),
    ("DELETE", "/api/library/songs/0123456789abcdef"), ("POST", "/api/library/voices/v-uc/display"),
    ("POST", "/api/library/voices/v-uc/rename"), ("GET", "/api/library/voices/v-uc/export"),
    ("DELETE", "/api/library/voices/v-uc/cadence"), ("GET", "/api/library/cleanup"), ("POST", "/api/library/cleanup"),
    ("POST", "/api/library/tools/ytdlp-update"), ("POST", "/api/library/tools/ytdlp-auto"), ("GET", "/api/addons"),
    ("GET", "/api/storage"),
    *[("POST", f"/api/addons/f5-tts/{a}") for a in ("install", "remove", "use")],
    ("POST", "/api/storage/check"), ("POST", "/api/storage/move"),
    ("GET", "/api/doctor"), ("POST", "/api/doctor/full"), ("POST", "/api/doctor/fix/stale"), ("GET", "/api/doctor/bundle"),
    ("GET", "/api/doctor/bundle.zip"), ("POST", "/api/doctor/open-logs"),
    ("GET", "/api/vault"), ("GET", "/api/vault/check"), ("GET", "/api/vault/contents"), ("POST", "/api/vault/settings"),
    ("POST", "/api/vault/run"), ("POST", "/api/vault/restore"),
    ("GET", "/api/phone"), ("POST", "/api/phone"), ("POST", "/api/phone/pair"), ("POST", "/api/phone/pair/cancel"),
    ("POST", "/api/phone/revoke"),
    ("GET", "/api/themes"), ("POST", "/api/themes"), ("POST", "/api/themes/import"), ("GET", "/api/themes/midnight/export"),
    ("GET", "/api/themes/midnight/theme.css"), ("GET", "/api/themes/midnight/preview.png"),
    ("GET", "/api/themes/midnight/fonts/Manrope.woff2"), ("DELETE", "/api/themes/midnight"),
    ("POST", "/api/tutorial/done"), ("POST", "/api/tutorial/reset"),
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
    for p in ("/", "/static/app.js", "/api/jobs", "/api/work", "/api/voices", "/api/library/covers", "/api/speak/voices",
              "/api/doctor", "/api/addons", "/api/storage", "/api/history", "/api/config", "/api/train/projects"):
        status = _lan("GET", p, token)[0]
        assert status == 200, (p, status)
    assert _lan("POST", "/api/work/settings", token, {})[0] == 200
    from undercover.core.config import path   # the sandbox's own folders (sandbox_server points this process at them)
    proj = next(p for p in (path("cache_dir") / "training").iterdir() if (p / "project.json").exists())
    clip = next((proj / "clips").glob("*.wav"))
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


@check("api: the First assignment (status field, done, Replay's reset seeds the demo voice when it ships)")
def a_tutorial():
    from undercover.app import tutorial
    t = api("/api/status").get("tutorial")
    assert t is not None and "done" in t, t
    assert t["done"] == "existing install", t   # the sandbox holds a trained voice: an install from before the tutorial
    assert api("/api/tutorial/done", {})["tutorial"]["done"] == time.strftime("%Y-%m-%d")
    r = api("/api/tutorial/reset", {})
    assert r["tutorial"] == {"done": None}, r
    assert api("/api/status")["tutorial"] == {"done": None}
    shipped = (tutorial.DEMO / "model.pth").is_file()
    if shipped:
        assert r["seeded"]["voice"] == "agent-smoke", r
        v = next(x for x in api("/api/voices") if x["name"] == "agent-smoke")
        assert v["demo"] is True and v["trained"] is False and v["display"] == "Agent Smoke", v
        c = next(x for x in api("/api/work/candidates") if x["voice"] == "agent-smoke")
        assert c["can"] is False, c
    songs = api("/api/library/songs")
    assert all("sample" in x for x in songs)
    return f"existing install → done → reset; demo voice {'seeded' if shipped else 'not shipped yet'}; sample song {'yes' if r['seeded']['song'] else 'none (no audio yet)'}"


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
    for part in ('id="suite-logo"', 'id="uc-updated"', 'id="room-running"', 'id="workbar"', 'id="q-drawer"', 'id="q-panel"', 'id="wa-main"', 'id="assign"', 'id="assign-brief"', 'id="set-assign-replay"',
                 'id="da-start"', 'id="da-person"', 'id="da-train"', 'id="da-tonight"', 'id="da-work"', 'id="da-outcome"', 'id="da-steps"',
                 'id="wn"', 'id="wn-ok"', 'id="sys-news"', 'id="ear-cover"', 'id="se-ear"', 'id="set-ear-row"',
                 'id="vault"', 'id="vt-body"'):
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
    import tomllib
    base = Path(os.environ.get("UNDERCOVER_DATA_ROOT") or ROOT)
    cfg = tomllib.loads((base / "config.toml").read_text(encoding="utf-8"))["paths"]
    out = {k: (Path(v) if Path(v).is_absolute() else base / v) for k, v in cfg.items()}
    out.setdefault("voices_dir", out["models_dir"] / "voices")
    out.setdefault("themes_dir", out["uploads_dir"].parent / "themes")
    return out


def snapshot_real(dirs: dict) -> dict:
    """{file: (size, mtime_ns)} of everything in the user's library, model and working folders, and the settings files
    beside the config (config.toml, tutorial.json, phone.json, themes.json: a check once appended to the real config)."""
    import os
    snap = {}
    for name in ("config.toml", "tutorial.json", "phone.json", "themes.json", "ear.json"):
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
        env = {**os.environ, "UNDERCOVER_CONFIG": str(cfg), "UNDERCOVER_TEST_ROOTS": str(root), "UNDERCOVER_PHONE_ADDR": PHONE_ADDR}
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
    for fn in (u_imports, u_deps, u_fit, u_window, u_phone, u_stems, u_sources, u_speak, u_paused, u_da_mode, u_write_guard, u_vault, u_cadence, u_eta, u_addons, u_ytdlp, u_storage,
               u_rolloff_covers, u_rolloff_speech, u_stable_ids, u_migrate_ids,
               u_display_names, u_person, u_grammar, u_ambiguity, u_ask_flow, u_clipsound, u_keep_this_one, u_relocate_guard, u_pace_pitch, u_characters, u_leadgate, u_ear,
               u_doctor_fast, u_doctor_models, u_doctor_startup, u_doctor_resume, u_doctor_stale, u_doctor_packages,
               u_doctor_interrupted, u_doctor_bundle, u_qr, u_phone_pairing, u_phone_routes, u_phone_doctor, u_themes, u_themes_shipped, u_ytdlp_heal, u_tutorial, u_platform, u_tray):
        fn()
    if not a.unit_only:
        if a.port:   # someone else's server (7861 for a manual look): nothing that writes
            BASE = f"http://127.0.0.1:{a.port}"
            print(f"API checks ({BASE}, reads only: anything that makes, deletes or pauses work runs on the sandbox only)")
            for fn in (a_status, a_page, a_lists, a_library, a_train):
                fn()
        else:
            port = sandbox_port()
            print(f"API checks (a sandbox server on port {port}: scratch folders, the real models read-only)")
            try:
                with sandbox_server(port):
                    for fn in (a_status, a_page, a_lists, a_library, a_train, a_work, a_vault, a_doctor, a_retry, a_phone, a_themes, a_tutorial, a_tray):
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
