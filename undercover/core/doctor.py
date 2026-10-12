"""The Doctor: checks this install, explains each problem in plain words, and fixes what's safe to fix (phase 10,
ARCHITECTURE.md "Resilience and self-healing").

    python -m undercover.doctor [--full] [--json]            (undercover/doctor.py, the command line)
    GET /api/doctor, POST /api/doctor/fix/<id>             (SafeHouse's System room)

Fast checks (startup, < 2 s, no GPU, no torch import): Python and the venv, the PyTorch build, every pinned package,
ffmpeg and Deno, the model files (present, size), disk space, the [paths] folders, the port, stale files, interrupted
work. Full checks add every model file's SHA-256, the graphics card and its driver.

Each result: {"id", "group", "state": ok | info | warn | bad, "title", "say" (plain words), "detail" (Deep Cover),
"fix": {"id", "label", "how": now | job | offer} or None}. "offer" fixes are words only (a step for the user, like
setup.ps1 -Repair); the others run when the user presses the button.

Safe automatic healing at startup (heal_at_startup), only reversible steps:
- a pinned model file that's missing or the wrong size against the manifest is fetched again from its pinned source;
  the new copy is checked (SHA-256) before it replaces the old one, so nothing is deleted first;
- stale leftovers (*.part, *.tmp, voice-upload temp folders) older than STALE_HOURS in the model and working-files
  folders are removed, through core/storage.remove_files (allow-list; refused in test mode outside the test's folders),
  never inside a training project, never while any server reports running work, never in a folder that running or
  paused work owns;
- work a crash interrupted: training and transcripts (which continue from their saved point) resume once; anything
  else (one-shots, preparing clips) is put back as paused, for Resume. If the same work was resumed after the last crash
  and the PC died again, it's held for Resume too, so a crash can't repeat itself in a loop.
Everything is logged to logs/doctor.log. The Doctor never touches voices, songs, covers, speech or training projects,
never installs anything by itself and never changes Windows or driver settings.
"""
import json
import os
import re
import shutil
import sys
import threading
import time
from pathlib import Path

from . import manifest, setup_models, storage
from .config import CONFIG, ROOT, path
from .voices import ModelError

STALE_HOURS = 6
AUTO_RESUME = {"train", "transcribe"}   # resume specs that continue from a saved point (MasterPlan, 2026-10-12)
LAST: dict = {"fast": None, "full": None, "startup": None}   # the latest reports (the System room shows them)
_LOCK = threading.Lock()


# ---------------------------------------------------------------- the log

def log(msg: str):
    """One line in logs/doctor.log (and the console)."""
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        with open(path("logs_dir") / "doctor.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    try:
        print(f"doctor: {msg}", flush=True)
    except (UnicodeEncodeError, OSError):   # a console or log file in the Windows code page (an arrow, a name's accent)
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        print(f"doctor: {msg}".encode(enc, "replace").decode(enc), flush=True)


def _r(id, group, state, title, say, detail="", fix=None) -> dict:
    return {"id": id, "group": group, "state": state, "title": title, "say": say, "detail": detail, "fix": fix}


def _fix(id, label, how="now") -> dict:
    return {"id": id, "label": label, "how": how}


REPAIR = _fix("repair", "Run UnderCover's setup again with -Repair", "offer")


# ---------------------------------------------------------------- the checks

def c_python(ctx) -> list:
    m = ctx["manifest"] or {}
    want = (m.get("python") or {}).get("version")
    have = sys.version.split()[0]
    venv = Path(sys.prefix)
    in_app = storage._inside(venv, ROOT)
    out = []
    if want and want != have:
        out.append(_r("python", "install", "bad", "Python",
                      f"UnderCover is running on Python {have}, but it was installed with {want}.",
                      f"{sys.executable}", REPAIR))
    elif not have.startswith("3.12."):
        out.append(_r("python", "install", "warn", "Python", f"Python {have}: UnderCover is built and tested on 3.12.",
                      sys.executable, REPAIR))
    else:
        out.append(_r("python", "install", "ok", "Python", f"Python {have}.", sys.executable))
    made = (venv / "undercover-setup.txt").exists()
    out.append(_r("venv", "install", "ok" if in_app else "warn", "Python environment",
                  "UnderCover's own Python environment." if in_app else
                  "UnderCover isn't running from its own Python environment (.venv in its folder).",
                  f"{venv} · {'made by setup' if made else 'made by hand (setup and -Repair leave it alone)'}"))
    return out


def c_torch(ctx) -> list:
    have = ctx["installed"].get("torch")
    want_backend = CONFIG["gpu"].get("backend", "rocm")
    if not have:
        return [_r("torch", "install", "bad", "PyTorch", "PyTorch isn't installed, so nothing can run.", "", REPAIR)]
    build = "rocm" if "+rocm" in have else "cuda" if "+cu" in have else "cpu"
    pin = (ctx["pins"].get("torch") or {}).get("version")
    names = {"rocm": "AMD's ROCm build", "cuda": "the NVIDIA CUDA build", "cpu": "the processor-only build"}
    if build != want_backend:
        return [_r("torch", "install", "bad", "PyTorch",
                   f"PyTorch is {names[build]}, but this install is set up for {names.get(want_backend, want_backend)}. "
                   "Something replaced it, so the graphics card can't be used.", f"torch {have}", REPAIR)]
    if pin and pin != have:
        return [_r("torch", "install", "warn", "PyTorch", f"PyTorch {have} is installed; UnderCover pins {pin}.",
                   f"torch {have} · pinned {pin}", REPAIR)]
    return [_r("torch", "install", "ok", "PyTorch", f"PyTorch {have} ({names[build]}).", f"torch {have}")]


def c_gpu(ctx) -> list:
    """The graphics card: from the running server's own check (STATUS), or, on the command line in full mode, a small
    test of its own (a few seconds; loads torch)."""
    st = ctx.get("status")
    out = []
    if st is not None:
        if st.get("gpu_problem"):
            fix = _fix("cpu", "Use the processor until UnderCover restarts") if _cpu_fallback_ok(st) else None
            out.append(_r("gpu", "gpu", "bad", "Graphics card", st["gpu_problem"], "", fix))
        elif st.get("cpu_mode"):
            out.append(_r("gpu", "gpu", "info", "Graphics card", "Running on the processor: covers are slow, training is off.",
                          f"backend {st.get('backend')}"))
        elif st.get("gpu"):
            out.append(_r("gpu", "gpu", "ok", "Graphics card", f"{st['gpu']} · {st.get('vram_gb')} GB",
                          f"backend {st.get('backend')}"))
        else:
            out.append(_r("gpu", "gpu", "info", "Graphics card", "Still checking (UnderCover is starting).", ""))
    elif ctx["full"]:
        out.append(_gpu_probe())
    if ctx["full"]:
        drv = manifest.gpu_driver()
        old = ((ctx["manifest"] or {}).get("gpu") or {}).get("driver") or {}
        if drv:
            changed = old.get("version") and old.get("version") != drv.get("version")
            out.append(_r("driver", "gpu", "info" if changed else "ok", "Graphics driver",
                          f"{drv.get('name')}: driver {drv.get('version')}" +
                          (f" (it was {old['version']} when UnderCover was set up; if covers or training started failing "
                           "since, the new driver is the first suspect)" if changed else ""),
                          f"{drv.get('version')} · {drv.get('date')}"))
    return out


def _gpu_probe() -> dict:
    try:
        import torch
        if not torch.cuda.is_available():
            return _r("gpu", "gpu", "bad" if CONFIG["gpu"].get("backend") != "cpu" else "info", "Graphics card",
                      "PyTorch can't see the graphics card. Check Device Manager (Display adapters), update or reinstall the "
                      "driver if it shows an error, restart the PC, then UnderCover.", f"torch {torch.__version__}")
        name = torch.cuda.get_device_name(0)
        want = (CONFIG["gpu"].get("device_name") or "").lower()
        x = torch.ones(64, 64, device="cuda", dtype=torch.float16)
        ok = (x @ x)[0, 0].item() == 64
        if not ok:
            return _r("gpu", "gpu", "bad", "Graphics card", f"{name} gave a wrong answer to a simple sum: the driver needs a look.", "")
        if want and want not in name.lower():
            return _r("gpu", "gpu", "warn", "Graphics card", f"Found {name}, but UnderCover is set up for \"{want}\".",
                      "config.toml [gpu] device_name")
        return _r("gpu", "gpu", "ok", "Graphics card", f"{name}: working.", f"torch {torch.__version__}")
    except Exception as e:   # noqa: BLE001
        return _r("gpu", "gpu", "bad", "Graphics card", f"Testing the graphics card failed ({type(e).__name__}).", str(e)[:200])


def _cpu_fallback_ok(st: dict) -> bool:
    """The processor fallback only makes sense when torch itself works (the card is missing or the build is CPU-only)."""
    return bool(st.get("gpu_problem")) and not st.get("cpu_mode")


def c_packages(ctx) -> list:
    exp, have, pins = ctx["expected"], ctx["installed"], ctx["pins"]
    wrong, missing, drift = [], [], []
    for name, ver in exp.items():
        if name in manifest.NEVER_FROM_APP:
            continue   # PyTorch has its own check
        got = have.get(name)
        if got is None:
            missing.append(name)
        elif got != ver and got != (pins.get(name) or {}).get("version"):   # the version pinned now is fine too
            wrong.append(f"{name} {got} (expected {ver})")
    for name, rec in pins.items():
        if name not in manifest.NEVER_FROM_APP and have.get(name) and rec.get("version") and have[name] != rec["version"]:
            drift.append(f"{name} {have[name]} · pinned {rec['version']}")
    out = []
    if missing or wrong:
        fixable = all(n.split()[0] in pins for n in missing + wrong)
        say = (f"{len(missing)} package{'s' if len(missing) != 1 else ''} missing" if missing else "") + \
              (" and " if missing and wrong else "") + \
              (f"{len(wrong)} at a different version than installed" if wrong else "") + \
              ". Something changed UnderCover's Python packages; features that use them may fail."
        out.append(_r("packages", "install", "bad" if missing else "warn", "Python packages", say,
                      "; ".join(missing + wrong)[:1500],
                      _fix("packages", "Reinstall the pinned versions", "job") if fixable else REPAIR))
    else:
        out.append(_r("packages", "install", "ok", "Python packages",
                      f"All {len([n for n in exp if n not in manifest.NEVER_FROM_APP])} pinned packages are as installed.", ""))
    if drift and not (missing or wrong):
        out.append(_r("pins", "install", "info", "Pinned versions",
                      f"{len(drift)} package{'s' if len(drift) != 1 else ''} differ from the versions UnderCover now pins "
                      "(an update moved the pins). setup.ps1 -Repair brings them in line.", "; ".join(drift)[:1500]))
    return out


def c_tools(ctx) -> list:
    out = []
    for name, need, why in (("ffmpeg", "bad", "reads and writes audio: covers and speech need it"),
                            ("deno", "warn", "lets the YouTube downloader read YouTube")):
        p = manifest.tool(name)
        if p:
            out.append(_r(name, "install", "ok", name.capitalize() if name == "deno" else "ffmpeg", f"Found ({why.split(':')[0]}).", str(p)))
        else:
            out.append(_r(name, "install", need, name.capitalize() if name == "deno" else "ffmpeg",
                          f"Not found. It {why}.", "", REPAIR))
    return out


def c_models(ctx) -> list:
    """Pinned model files: present and the right size (fast); the right SHA-256 (full). Files the manifest never saw
    aren't a problem: they're fetched on first use."""
    mdir = path("models_dir")
    rec = (ctx["manifest"] or {}).get("models") or {}
    out, fine, later = [], 0, 0
    broken = []
    for local, pin in sorted(setup_models.PINNED.items()):
        p = mdir / local
        seen = rec.get(local)
        exists = p.exists()
        size = p.stat().st_size if exists else None
        what = _part_name(pin["part"])
        if not exists:
            if seen:
                broken.append(local)
                out.append(_r(f"model:{local}", "models", "bad", f"{what}: a model file is missing",
                              f"{Path(local).name} was there before and is gone. UnderCover can download it again.",
                              local, _fix(f"model:{local}", "Download it again", "job")))
            else:
                later += 1
            continue
        bad = None
        if size == 0:
            bad = "is empty"
        elif seen and seen.get("size") and seen["size"] != size and (not seen.get("sha256") or seen["sha256"] == pin["sha256"]):
            bad = f"is {size:,} bytes, not {seen['size']:,} (cut off or damaged)"
        elif ctx["full"]:
            h = ctx["hash"](p)
            if h != pin["sha256"]:
                bad = "doesn't match its checksum (damaged)"
        if bad:
            broken.append(local)
            out.append(_r(f"model:{local}", "models", "bad", f"{what}: a model file is damaged",
                          f"{Path(local).name} {bad}. UnderCover can download a good copy; the damaged one is replaced "
                          "only once the new one checks out.", local, _fix(f"model:{local}", "Download it again", "job")))
        else:
            fine += 1
    ctx["broken_models"] = broken
    out.insert(0, _r("models", "models", "ok" if not broken else "bad", "Model files",
                     f"{fine} model file{'s' if fine != 1 else ''} {'checked against their checksums' if ctx['full'] else 'present'}"
                     + (f"; {later} more download the first time a feature needs them" if later else "") + ".",
                     f"{len(setup_models.PINNED)} pinned files", _fix("models", "Download the damaged ones again", "job")
                     if len(broken) > 1 else None))
    return out


def _part_name(part: str) -> str:
    return {"rvc": "Voice engine", "separation": "Vocal separation", "speaker": "Speaker check", "kokoro": "Kokoro",
            "chatterbox": "Cadence engine", "whisper": "Speech recogniser", "f5": "F5-TTS"}.get(part, part)


def c_disk(ctx) -> list:
    out, seen = [], set()
    for key in ("uploads_dir", "output_dir", "cache_dir", "models_dir"):
        try:
            p = path(key)
            drive = os.path.splitdrive(str(p))[0].upper() or "/"
            if drive in seen:
                continue
            seen.add(drive)
            free = shutil.disk_usage(p).free
        except OSError:
            continue
        state = "bad" if free < 3e9 else "warn" if free < 10e9 else "ok"
        out.append(_r(f"disk:{drive}", "files", state, f"Free space on {drive}",
                      f"{free / 1e9:.1f} GB free." + {"bad": " Covers and training refuse to start under 3 GB: free some "
                                                             "space (SafeHouse → Storage → Clean up).",
                                                      "warn": " Getting low: training and separation write several GB."}.get(state, ""),
                      str(p)))
    return out


def c_paths(ctx) -> list:
    out = []
    for g, keys in storage.GROUPS.items():
        for key, _, _ in keys:
            raw = CONFIG["paths"].get(key)
            p = Path(raw) if raw else None
            if p is not None and not p.is_absolute():
                p = ROOT / p
            if p is None:   # voices_dir default
                continue
            anchor = Path(p.anchor) if p.anchor else None
            if anchor is not None and not anchor.exists():
                out.append(_r(f"path:{key}", "files", "bad", storage.LABELS[g],
                              f"Drive {p.anchor.rstrip(chr(92))} isn't connected, so {storage.LABELS[g].lower()} can't be "
                              "reached. Plug the drive in and restart UnderCover.", f"[paths] {key} = {raw}"))
            elif p.exists() and not os.access(p, os.W_OK):
                out.append(_r(f"path:{key}", "files", "bad", storage.LABELS[g],
                              f"UnderCover can't write to {p}. Check the folder's permissions.", f"[paths] {key}"))
    if not out:
        out.append(_r("paths", "files", "ok", "Folders", "Every folder UnderCover uses is there and writable.", ""))
    return out


def c_port(ctx) -> list:
    if ctx.get("status") is not None:
        return []   # we are the server
    port = CONFIG["ui"]["port"]
    import socket
    s = socket.socket()
    s.settimeout(0.5)
    try:
        busy = s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()
    if not busy:
        return [_r("port", "files", "ok", f"Port {port}", "Free: UnderCover can start.", "")]
    try:
        import urllib.request
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/status")
        with urllib.request.urlopen(req, timeout=2) as r:
            json.loads(r.read())
        return [_r("port", "files", "ok", f"Port {port}", "UnderCover is running there.", "")]
    except Exception:   # noqa: BLE001
        return [_r("port", "files", "bad", f"Port {port}",
                   f"Another program is using port {port}, so UnderCover can't start there. Close it, or change [ui] port "
                   "in config.toml.", "")]


def c_stale(ctx) -> list:
    auto, offer, why_not = stale_files()
    ctx["stale"] = (auto, offer)
    items = auto + offer
    if not items:
        return [_r("stale", "files", "ok", "Leftovers", "No half-finished downloads or temporary files lying around.",
                   why_not or "")]
    size = sum(_size(p) for p in items)
    return [_r("stale", "files", "info", "Leftovers",
               f"{len(items)} leftover{'s' if len(items) != 1 else ''} from interrupted downloads or uploads "
               f"({size / 1e6:.0f} MB).", "\n".join(str(p) for p in items[:30]),
               _fix("stale", "Clear them") if not why_not else None)]


def c_work(ctx) -> list:
    info = LAST.get("interrupted")
    if not info:
        return []
    return [_r("interrupted", "work", "info", "Interrupted work",
               f"UnderCover stopped suddenly at {info['at']} while it was {info['during']}. " + info["done"], "")]


def c_manifest(ctx) -> list:
    m = ctx["manifest"]
    if not m:
        return [_r("manifest", "install", "info", "Install record",
                   "There's no record of this install yet; UnderCover writes one from what it finds.", str(manifest.file()),
                   _fix("manifest", "Write it now", "job"))]
    kind = "made by setup" if m.get("kind") == "installed" else "written from what UnderCover found"
    return [_r("manifest", "install", "ok", "Install record", f"Recorded {m.get('made', '?')[:10]} ({kind}).",
               str(manifest.file()))]


CHECKS = [c_manifest, c_python, c_torch, c_gpu, c_packages, c_tools, c_models, c_disk, c_paths, c_port, c_stale, c_work]


def run(full: bool = False, status: dict | None = None, progress=None, cancel=None) -> dict:
    """Every check. status: the running server's STATUS (None on the command line). -> the report."""
    from . import about
    t0 = time.time()
    hashed = [0]
    total = sum(((path("models_dir") / f).stat().st_size for f in setup_models.PINNED
                 if (path("models_dir") / f).exists()), 0) or 1

    def hash_one(p):
        if cancel and cancel():
            raise InterruptedError
        h = setup_models.sha256_of(p)
        hashed[0] += p.stat().st_size
        if progress:
            progress(f"Checking {p.name}", hashed[0], total)
        return h

    ctx = {"full": full, "status": status, "manifest": manifest.load(), "installed": manifest.installed(),
           "pins": manifest.pins(), "hash": hash_one}
    ctx["expected"] = manifest.expected_packages()
    results = []
    for c in CHECKS:
        try:
            results += c(ctx)
        except InterruptedError:
            raise
        except Exception as e:   # noqa: BLE001 - one broken check mustn't hide the others
            results.append(_r(c.__name__[2:], "install", "warn", c.__name__[2:], f"This check couldn't run ({type(e).__name__}).", str(e)[:300]))
    order = {"bad": 0, "warn": 1, "info": 2, "ok": 3}
    counts = {k: sum(r["state"] == k for r in results) for k in order}
    rep = {"made": time.strftime("%Y-%m-%dT%H:%M:%S"), "mode": "full" if full else "fast", "seconds": round(time.time() - t0, 2),
           "counts": counts, "problems": sorted([r for r in results if r["state"] in ("bad", "warn")], key=lambda r: order[r["state"]]),
           "results": results, "about": about.about()}
    LAST["full" if full else "fast"] = rep
    if full:
        try:
            from . import fsutil
            fsutil.write_text(path("logs_dir") / "doctor-report.json", json.dumps(rep, indent=1))
        except OSError:
            pass
    return rep


def last_full() -> dict | None:
    if LAST.get("full"):
        return LAST["full"]
    try:
        return json.loads((path("logs_dir") / "doctor-report.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------- stale files

_TMP = re.compile(r".+\.[0-9a-f]{8}\.tmp$")   # fsutil.write_text's temporary files


def _size(p: Path) -> int:
    try:
        return storage.size_of(p)[0] if p.is_dir() else p.stat().st_size
    except OSError:
        return 0


def _busy_elsewhere() -> str | None:
    """A server (this one or another, on another port, sharing these folders) with work running, from its heartbeat."""
    from . import jobs
    if any(not j.finished for j in list(jobs.JOBS.values())):
        return "work is running"
    logs = path("logs_dir")
    for f in logs.glob("heartbeat*.jsonl"):
        last = _last_line(f)
        if not last or last.get("event") == "stop":
            continue
        try:
            age = time.time() - time.mktime(time.strptime(last["t"], "%Y-%m-%d %H:%M:%S"))
        except (KeyError, ValueError):
            continue
        if age < 600 and last.get("jobs"):
            return f"another UnderCover ({f.name}) has work running"
    return None


def _last_line(f: Path) -> dict | None:
    try:
        with open(f, "rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - 65536))
            lines = fh.read().decode("utf-8", "replace").strip().splitlines()
        return json.loads(lines[-1]) if lines else None
    except (OSError, ValueError, IndexError):
        return None


def owned_dirs() -> list:
    """Folders that running or paused work owns (MasterPlan: a paused training can sit for days): training projects of
    the work on every resume list and queue, and the preview folders of running jobs."""
    out = [path("cache_dir") / "training"]   # projects are never cleaned by the Doctor at all
    from . import jobs
    for wl in path("cache_dir").glob("worklist*.json"):
        try:
            st = json.loads(wl.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for spec in st.get("resume") or []:
            if spec.get("project"):
                out.append(path("cache_dir") / "training" / spec["project"])
    for j in list(jobs.JOBS.values()):
        if not j.finished:
            out += list((path("cache_dir") / "previews").glob(f"*{j.id}*"))
    return out


def stale_files(now: float | None = None) -> tuple[list, list, str | None]:
    """(cleared automatically, offered, why nothing is cleared now). Leftovers older than STALE_HOURS:
    models_dir and cache_dir: *.part (downloads, separations), fsutil *.tmp, cache/vup_* (voice uploads) -> automatic;
    uploads_dir upload_*.part and output_dir *.tmp -> offered (the library is the user's: only on their click)."""
    now = now or time.time()
    old = now - STALE_HOURS * 3600
    why = _busy_elsewhere()
    owned = owned_dirs()
    skip = owned + [path("voices_dir"), path("cache_dir") / "previews", path("cache_dir") / "uv"]

    def ok(p: Path) -> bool:
        try:
            return p.stat().st_mtime < old and not any(storage._inside(p, s) for s in skip)
        except OSError:
            return False

    def scan(root: Path, deep: bool, pick) -> list:
        found = []
        if not root.exists():
            return found
        for dirpath, dirs, files in os.walk(root):
            d = Path(dirpath)
            dirs[:] = [x for x in dirs if not any(storage._inside(d / x, s) for s in skip)] if deep else []
            found += [d / f for f in files if pick(f) and ok(d / f)]
        return found

    leftover = lambda f: f.endswith(".part") or ".part." in f or bool(_TMP.match(f))   # noqa: E731
    auto = scan(path("models_dir"), True, leftover) + scan(path("cache_dir"), True, leftover)
    auto += [p for p in path("cache_dir").glob("vup_*") if p.is_dir() and ok(p)]
    offer = scan(path("uploads_dir"), False, lambda f: f.startswith("upload_") and f.endswith(".part"))
    offer += scan(path("output_dir"), False, lambda f: bool(_TMP.match(f)))
    return auto, offer, why


def clear_stale(include_offered: bool = True) -> dict:
    """Remove the leftovers (the Fix, and startup with include_offered=False). Through storage.remove_files: every
    path checked inside the folder it came from before anything goes; refused in test mode outside the test's folders."""
    auto, offer, why = stale_files()
    if why:
        return {"removed": [], "why": f"Not now: {why}."}
    removed, problems = [], []
    groups = [(auto, [path("models_dir"), path("cache_dir")])]
    if include_offered:
        groups.append((offer, [path("uploads_dir"), path("output_dir")]))
    for items, within in groups:
        files = [p for p in items if p.is_file()]
        dirs = [p for p in items if p.is_dir()]
        if not files and not dirs:
            continue
        try:
            storage.remove_files(files, within, dirs)
            removed += [str(p) for p in files + dirs]
        except (OSError, ModelError) as e:
            problems.append(str(e))
    if removed:
        log(f"cleared {len(removed)} leftover(s): " + "; ".join(removed[:20]))
    for p in problems:
        log(f"clearing leftovers: {p}")
    return {"removed": removed, "why": "; ".join(problems) or None}


# ---------------------------------------------------------------- fixes

FIX_JOBS: dict = {}   # fix id -> the running Job (one at a time per fix)


def fix(fix_id: str, status: dict | None = None, dry_run: bool = False) -> dict:
    """Run a Fix. Quick ones return {"ok", "message"}; long ones start a job and return {"job"}."""
    if fix_id == "stale":
        r = clear_stale()
        return {"ok": not r["why"], "message": f"Cleared {len(r['removed'])} leftover(s)." if r["removed"] else (r["why"] or "Nothing to clear.")}
    if fix_id == "cpu":
        if status is None or not _cpu_fallback_ok(status):
            raise ModelError("There's nothing to fall back from: the graphics card isn't reported as a problem.")
        log(f"processor fallback until restart (was: {status.get('gpu_problem')})")
        status["gpu_problem_was"] = status.pop("gpu_problem")
        status["cpu_mode"] = True
        if str(status.get("models", "")).startswith("error"):
            status["models"] = "ready"   # nothing preloaded: separation and conversion load on the processor on first use
        return {"ok": True, "message": "UnderCover uses the processor until it restarts: covers are slow, training is off."}
    if fix_id == "resume":
        from . import worklist
        return {"ok": True, **worklist.resume_all()}
    if fix_id == "packages":
        return _job("packages", "Reinstalling the pinned packages", lambda job: fix_packages(dry_run=dry_run))
    if fix_id == "manifest":
        return _job("manifest", "Writing the install record", lambda job: _observe_job(job))
    if fix_id == "models":
        rep = run(False, status)
        todo = [r["id"][6:] for r in rep["results"] if r["id"].startswith("model:") and r["state"] == "bad"]
        if not todo:
            return {"ok": True, "message": "Every model file is fine."}
        return _job("models", "Downloading model files again", lambda job: _refetch(job, todo))
    if fix_id.startswith("model:"):
        local = fix_id[6:]
        if local not in setup_models.PINNED:
            raise ModelError(f"{local} isn't a file UnderCover knows how to download again.")
        return _job(fix_id, f"Downloading {Path(local).name} again", lambda job: _refetch(job, [local]))
    raise ModelError(f"There's no fix called '{fix_id}'.")


def _job(key: str, title: str, work) -> dict:
    from . import jobs
    with _LOCK:
        j = FIX_JOBS.get(key)
        if j is not None and not j.finished:
            return {"job": j.id, "already": True}
        job = jobs.Job("doctor", title=title)
        jobs.JOBS[job.id] = job
        FIX_JOBS[key] = job

    def go():
        with job.cond:
            job.state = "running"
        try:
            r = work(job) or {}
            job.finish("done", message=r.get("message") or "Done.", result=r)
        except InterruptedError:
            job.finish("cancelled", message="Cancelled.")
        except ModelError as e:
            log(f"{title}: {e}")
            job.finish("error", message=str(e))
        except Exception as e:   # noqa: BLE001
            log(f"{title}: {type(e).__name__}: {e}")
            job.finish("error", message=jobs.friendly(e, title.lower()))

    threading.Thread(target=go, daemon=True, name=f"doctor-{key}").start()
    return {"job": job.id}


def _refetch(job, todo: list) -> dict:
    done = []
    last = [0.0]
    for i, local in enumerate(todo):
        def prog(f, d, t, i=i):
            if job.cancel:
                raise InterruptedError
            if time.time() - last[0] > 0.4:
                last[0] = time.time()
                job.push(stage="downloading", frac=round(d / t, 3) if t else None,
                         message=f"Downloading {Path(f).name} ({i + 1} of {len(todo)}): {d / 1e6:.0f}" + (f" of {t / 1e6:.0f} MB" if t else " MB"))
        log(f"re-downloading {local}")
        setup_models.refetch(local, prog)
        _note_model(local)
        log(f"re-downloaded {local}: checksum ok")
        done.append(local)
    return {"message": f"Downloaded {len(done)} model file{'s' if len(done) != 1 else ''} again; checksums match.", "files": done}


def _note_model(local: str):
    """The manifest follows a re-downloaded file (its size and the pinned checksum)."""
    m = manifest.load()
    if m is not None:
        p = path("models_dir") / local
        m.setdefault("models", {})[local] = {"size": p.stat().st_size, "sha256": setup_models.PINNED[local]["sha256"], "pinned": True}
        manifest.save(m)


def _observe_job(job) -> dict:
    last = [0.0]

    def prog(f, d, t):
        if time.time() - last[0] > 0.5:
            last[0] = time.time()
            job.push(stage="checking", frac=round(d / t, 3), message=f"Recording model files: {d / 1e9:.1f} of {t / 1e9:.1f} GB")
    m = manifest.observe(progress=prog, cancel=lambda: job.cancel)
    log(f"wrote the install record ({len(m['models'])} model files, {len(m['packages'])} packages)")
    return {"message": "The install record is written."}


def package_fix_lines() -> tuple[list, list]:
    """(requirement lines that would put the differing packages back, names that the app can't fix itself)."""
    exp, have, pins = manifest.expected_packages(), manifest.installed(), manifest.pins()
    lines, refused = [], []
    for name, ver in exp.items():
        if have.get(name) == ver:
            continue
        if name in manifest.NEVER_FROM_APP:
            refused.append(name)
            continue
        rec = pins.get(name)
        if not rec or "--hash" not in rec["line"]:
            refused.append(name)   # no hashed pin to install: setup -Repair's job
            continue
        if have.get(name) == rec.get("version"):
            continue   # already the version pinned now (the record is older than an update): nothing to install
        lines.append(rec["line"])
    return lines, refused


def fix_packages(dry_run: bool = False) -> dict:
    """Put back the pinned version of every package that differs: exactly those lines from installer/requirements, each
    with its SHA-256, --no-deps --require-hashes, the torch pins held by constraints.txt. Only on the user's click (or
    --dry-run, which installs nothing). PyTorch is never touched here (setup.ps1 -Repair)."""
    import tempfile
    from . import addons
    lines, refused = package_fix_lines()
    if not lines:
        return {"message": "Nothing to reinstall." + (f" {', '.join(refused)} need setup.ps1 -Repair." if refused else ""),
                "lines": [], "refused": refused}
    with tempfile.TemporaryDirectory() as d:
        req = Path(d) / "repair.txt"
        req.write_text("\n".join(lines) + "\n", encoding="utf-8")
        args = ["install", "--no-deps", "--require-hashes", "-c", str(ROOT / "constraints.txt"), "-r", str(req)]
        if dry_run:
            args.insert(1, "--dry-run")
        log(("dry run: " if dry_run else "") + "reinstalling pinned packages: " + ", ".join(l.split()[0] for l in lines))
        addons._pip(*args, what="the pinned packages")
    if not dry_run:
        m = manifest.load()
        if m is not None:
            have = manifest.installed()
            for l in lines:
                n = manifest.norm(re.split(r"[=@ ]", l, 1)[0])
                if n in have:
                    m.setdefault("packages", {})[n] = have[n]
            manifest.save(m)
    return {"message": ("Checked (dry run, nothing installed): " if dry_run else "Reinstalled ") +
            ", ".join(l.split()[0] for l in lines) + ("." if dry_run else ". Restart UnderCover to use them."),
            "lines": lines, "refused": refused, "dry_run": dry_run}


# ---------------------------------------------------------------- startup: the safe automatic steps

def _state_file() -> Path:
    return path("logs_dir") / "doctor-state.json"


def _state() -> dict:
    try:
        return json.loads(_state_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(st: dict):
    from . import fsutil
    try:
        fsutil.write_text(_state_file(), json.dumps(st, indent=1))
    except OSError:
        pass


def interrupted(heartbeat: Path) -> dict | None:
    """The last session's end, if it was sudden with work running: {"at", "jobs": [...]}. Read before this session
    writes its own "start"."""
    last = _last_line(heartbeat)
    if not last or last.get("event") == "stop" or not last.get("jobs"):
        return None
    return {"at": last.get("t"), "jobs": last["jobs"]}


def restore_interrupted(heartbeat: Path) -> dict | None:
    """Put work a crash interrupted back (see the module's notes). -> what was done, for the log and the System room."""
    from . import worklist
    info = interrupted(heartbeat)
    if not info:
        return None
    specs = [j["spec"] for j in info["jobs"] if isinstance(j.get("spec"), dict) and j["spec"].get("kind") != "night"]
    during = ", ".join((j.get("message") or j.get("kind") or "?") for j in info["jobs"])[:200]
    st = _state()
    again = st.get("autoresumed") is not None and any(s in (st["autoresumed"].get("specs") or []) for s in specs)
    usable = []
    for s in specs:
        if s.get("kind") == "train" and s.get("project") and \
                not (path("cache_dir") / "training" / s["project"] / "train" / "resume.pt").exists():
            continue   # finished (or never saved a point): nothing to continue, and starting it would train afresh
        if s.get("kind") in worklist.RESUMERS:
            usable.append(s)
    start = bool(usable) and not again and all(s["kind"] in AUTO_RESUME for s in usable)
    if usable:
        worklist.restore(usable, start=start)
    labels = ", ".join(s.get("label") or s["kind"] for s in usable)
    if not usable:
        done = "There was nothing to pick up again." if not any(j.get("kind") == "night" for j in info["jobs"]) else \
            "The overnight queue carries on with it by itself."
    elif start:
        done = f"It carries on by itself: {labels}."
    elif again:
        done = f"It stopped the same way after being resumed once, so it waits for you: Resume in SafeHouse → Running ({labels})."
    else:
        done = f"It waits for you: Resume in SafeHouse → Running ({labels})."
    st["autoresumed"] = {"at": info["at"], "specs": usable} if start else None
    _save_state(st)
    rec = {"at": info["at"], "during": during, "done": done, "resumed": start, "specs": usable}
    LAST["interrupted"] = rec
    log(f"the last session ended suddenly at {info['at']} during {during}. {done}")
    return rec


def heal_at_startup(heartbeat: Path, status: dict | None = None, hash_new_manifest: bool = True) -> dict:
    """Called once by the server before it starts listening (< 2 s: fast checks only). Heavy steps (re-downloads, the
    first manifest's hashes) go to a background thread. -> the startup report."""
    t0 = time.time()
    did = []
    try:
        r = restore_interrupted(heartbeat)
        if r:
            did.append("interrupted work: " + r["done"])
    except Exception as e:   # noqa: BLE001
        log(f"couldn't check for interrupted work: {type(e).__name__}: {e}")
    if os.environ.get("UNDERCOVER_DOCTOR_NO_CLEAN") != "1":
        try:
            r = clear_stale(include_offered=False)
            if r["removed"]:
                did.append(f"cleared {len(r['removed'])} leftover(s)")
        except Exception as e:   # noqa: BLE001
            log(f"couldn't clear leftovers: {type(e).__name__}: {e}")
    rep = run(False, status)
    m = manifest.load()
    todo = [r["id"][6:] for r in rep["results"] if r["id"].startswith("model:") and r["state"] == "bad"]
    background = []
    if m is None:
        background.append(("manifest", None))
    if todo:
        background.append(("models", todo))
    if background:
        threading.Thread(target=_background, args=(background, hash_new_manifest), daemon=True, name="doctor").start()
    bad = rep["counts"]["bad"] + rep["counts"]["warn"]
    log(f"startup check: {bad} problem(s) in {time.time() - t0:.1f} s" + (f"; {'; '.join(did)}" if did else "")
        + (f"; in the background: {', '.join(k for k, _ in background)}" if background else ""))
    LAST["startup"] = {"made": rep["made"], "did": did, "background": [k for k, _ in background]}
    return rep


def _background(todo: list, hash_files: bool):
    for what, arg in todo:
        try:
            if what == "manifest":
                log("no install record yet: writing one from what's here (observed)" + ("" if hash_files else ", sizes only"))
                m = manifest.observe(hash_files=hash_files)
                log(f"install record written: {len(m['models'])} model files, {len(m['packages'])} packages")
            elif what == "models":
                for local in arg:
                    try:
                        log(f"re-downloading {local} (missing or damaged at startup)")
                        setup_models.refetch(local)
                        _note_model(local)
                        log(f"re-downloaded {local}: checksum ok")
                    except Exception as e:   # noqa: BLE001 - offline, source gone: offered in the System room instead
                        log(f"couldn't re-download {local}: {e}")
        except Exception as e:   # noqa: BLE001
            log(f"{what} in the background: {type(e).__name__}: {e}")
