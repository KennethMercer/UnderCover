"""The install manifest: what this copy of UnderCover was installed with (phase 10, ARCHITECTURE.md "Resilience and
self-healing"). The Doctor and `setup.ps1 -Repair` check the PC against it.

    <app folder>/manifest.json   (git-ignored; a sandbox / test server keeps its own in its logs folder)

kind "installed": written by the installer at the end of setup.ps1 (`python -m undercover.doctor manifest --installed`).
kind "observed":  an install from before the manifest existed (or a hand-built one, like the dev PC's): the app writes
                  it once, from what it finds, at startup in the background. It records what *was*, not what was meant.

{"v": 1, "kind", "made", "by", "app": {version, git}, "python": {version, exe}, "venv": {path, setup},
 "backend", "torch_file": "torch-rocm.txt", "gpu": {name, driver}, "packages": {name: version},
 "tools": {ffmpeg: {path, size}, deno: {...}, uv: {...}}, "models": {path under models_dir: {size, sha256, pinned}},
 "hashed": bool}

Expected package versions are the pins in installer/requirements/*.txt (common, cadence, torch-<backend>); the manifest
keeps the versions installed with them. A pinned update (yt-dlp) writes its new version here too (note_package).
"""
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path
from urllib.parse import unquote

from . import fsutil
from .config import CONFIG, ROOT, path

REQ = ROOT / "installer" / "requirements"
NEVER_FROM_APP = {"torch", "torchaudio", "torchvision", "rocm", "rocm-sdk-core", "rocm-sdk-devel",
                  "rocm-sdk-libraries-custom"}   # loaded by the running app: only setup.ps1 -Repair replaces them


def file() -> Path:
    """Where the manifest lives: the app folder; a sandbox / test server (UNDERCOVER_TEST_ROOTS) keeps its own."""
    from . import storage
    if storage.ALLOWED_ROOTS is not None:   # test mode (a sandbox server, isolated_data()): never the app's own record
        return path("logs_dir") / "manifest.json"
    return ROOT / "manifest.json"


def load() -> dict | None:
    try:
        m = json.loads(file().read_text(encoding="utf-8"))
        return m if isinstance(m, dict) else None
    except (OSError, ValueError):
        return None


def save(m: dict):
    fsutil.write_text(file(), json.dumps(m, indent=1))


# ---------------------------------------------------------------- the pins (installer/requirements)

def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_requirements(f: Path) -> dict:
    """{name: {"version", "line"}} from a requirements file (backslash-continued lines joined; `name==ver` or
    `name @ url#sha256=...`, the version then read from the wheel's file name)."""
    out = {}
    try:
        text = f.read_text(encoding="utf-8")
    except OSError:
        return out
    for logical in re.sub(r"\\\r?\n", " ", text).splitlines():
        line = logical.split(" #", 1)[0].strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"([A-Za-z0-9_.\-]+)\s*==\s*([^\s;]+)", line)
        if m:
            out[norm(m.group(1))] = {"version": m.group(2), "line": line}
            continue
        m = re.match(r"([A-Za-z0-9_.\-]+)\s*@\s*(\S+)", line)
        if m:
            fname = unquote(m.group(2).split("#", 1)[0].rsplit("/", 1)[-1])
            v = re.match(r"[A-Za-z0-9_.]+?-(\d[^-]*?)(?:-|\.tar\.gz$|\.zip$)", fname)
            out[norm(m.group(1))] = {"version": v.group(1) if v else None, "line": line}
    return out


def torch_file(backend: str | None = None, torch_version: str | None = None) -> str:
    """Which torch-<x>.txt this install uses: from the manifest, else the backend (+ the installed build's CUDA tag)."""
    m = load() or {}
    if m.get("torch_file"):
        return m["torch_file"]
    backend = backend or CONFIG["gpu"].get("backend", "rocm")
    if backend == "cuda":
        tv = torch_version or installed().get("torch") or ""
        return "torch-cuda126.txt" if "+cu126" in tv else "torch-cuda128.txt"
    return f"torch-{backend}.txt"


def pins(req_dir: Path = REQ) -> dict:
    """{name: {"version", "line", "file"}}: every package UnderCover pins for this install."""
    out = {}
    for fname in ("common.txt", "cadence.txt", torch_file()):
        for name, rec in parse_requirements(req_dir / fname).items():
            out[name] = {**rec, "file": fname}
    return out


def installed() -> dict:
    """{name: version} of every package in this venv (one scan of the metadata: fast, imports nothing)."""
    from importlib.metadata import distributions
    out = {}
    for d in distributions():
        try:
            out[norm(d.metadata["Name"])] = d.version
        except Exception:   # noqa: BLE001 - a broken dist-info: skip it (the packages check reports what's missing)
            pass
    return out


def expected_packages(m: dict | None = None, p: dict | None = None) -> dict:
    """{name: version} the checks hold this venv to: the manifest's record where it has one, else the pins. m / p: the
    record and the pins when the caller has them already."""
    m = (m if m is not None else load()) or {}
    exp = {n: r["version"] for n, r in (p if p is not None else pins()).items() if r.get("version")}
    exp.update({norm(k): v for k, v in (m.get("packages") or {}).items()})
    return exp


def note_package(name: str, version: str):
    """A pinned update (yt-dlp) changed a package: the manifest follows, so the Doctor doesn't call it wrong."""
    m = load()
    if m is not None:
        m.setdefault("packages", {})[norm(name)] = version
        save(m)


def note_model(local: str, size: int, sha256: str | None):
    """A model file was downloaded (and checked) after the record was written: record it, so damage to it later is
    found by its size at startup."""
    m = load()
    if m is not None:
        m.setdefault("models", {})[local] = {"size": size, "sha256": sha256, "pinned": True}
        save(m)


# ---------------------------------------------------------------- tools and model files

def tool(name: str) -> Path | None:
    """ffmpeg / deno / uv: the installer's copy in tools\\, else the one on PATH."""
    exe = name + (".exe" if os.name == "nt" else "")
    for p in (ROOT / "tools" / name / "bin" / exe, ROOT / "tools" / name / exe):
        if p.exists():
            return p
    w = shutil.which(name)
    return Path(w) if w else None


def model_files() -> list:
    """Every model file UnderCover downloaded (paths under models_dir), the voices and the add-ons record left out:
    voices are the user's own work, not downloads."""
    from . import storage
    mdir, vdir = path("models_dir"), path("voices_dir")
    out = []
    for dirpath, dirs, files in os.walk(mdir):
        d = Path(dirpath)
        dirs[:] = [x for x in dirs if not storage._inside(d / x, vdir)]
        for f in files:
            p = d / f
            if f.endswith((".part", ".tmp")) or (d == mdir and f == "addons.json"):
                continue
            out.append(p.relative_to(mdir).as_posix())
    return sorted(out)


def observe(kind: str = "observed", by: str = "", hash_files: bool = True, progress=None, cancel=None) -> dict:
    """Write the manifest from what this PC has now. hash_files=False records sizes only (a test server's quick one)."""
    from . import about, setup_models
    have = installed()
    p = pins()
    mdir = path("models_dir")
    models = {}
    files = model_files()
    total = sum((mdir / f).stat().st_size for f in files if (mdir / f).exists()) or 1
    done = 0
    for f in files:
        if cancel and cancel():
            raise InterruptedError
        fp = mdir / f
        try:
            size = fp.stat().st_size
        except OSError:
            continue
        rec = {"size": size, "pinned": f in setup_models.PINNED}
        if hash_files:
            rec["sha256"] = setup_models.sha256_of(fp)
        models[f] = rec
        done += size
        if progress:
            progress(f, done, total)
    marker = Path(sys.prefix) / "undercover-setup.txt"
    m = {"v": 1, "kind": kind, "made": time.strftime("%Y-%m-%dT%H:%M:%S"),
         "by": by or ("setup.ps1" if kind == "installed" else f"UnderCover {about.VERSION}, from what it found"),
         "app": {"version": about.VERSION, "git": about.git_commit()},
         "python": {"version": sys.version.split()[0], "exe": sys.executable},
         "venv": {"path": sys.prefix, "setup": marker.exists()},
         "backend": CONFIG["gpu"].get("backend", "rocm"), "torch_file": torch_file(),
         "gpu": {"name": CONFIG["gpu"].get("device_name") or None, "driver": gpu_driver()},
         "packages": {n: have[n] for n in p if n in have},
         "tools": {}, "models": models, "hashed": hash_files}
    for t in ("ffmpeg", "deno", "uv"):
        tp = tool(t)
        if tp:
            m["tools"][t] = {"path": str(tp), "size": tp.stat().st_size}
    save(m)
    return m


def gpu_driver() -> dict | None:
    """The graphics driver Windows reports (name, version, date), or None. ~1 s (PowerShell): full checks only."""
    if os.name != "nt":
        return None
    import subprocess
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command",
                            "Get-CimInstance Win32_VideoController | Select-Object Name,DriverVersion,DriverDate | "
                            "ConvertTo-Json -Compress"], capture_output=True, text=True, timeout=20,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        rows = json.loads(r.stdout or "null")
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    rows = rows if isinstance(rows, list) else [rows] if rows else []
    want = (CONFIG["gpu"].get("device_name") or "").lower()
    row = next((x for x in rows if want and want in (x.get("Name") or "").lower()), rows[0] if rows else None)
    if not row:
        return None
    date = row.get("DriverDate")
    if isinstance(date, str) and date.startswith("/Date("):   # Windows PowerShell 5.1's JSON date
        try:
            date = time.strftime("%Y-%m-%d", time.localtime(int(re.findall(r"\d+", date)[0]) / 1000))
        except (IndexError, ValueError):
            pass
    return {"name": row.get("Name"), "version": row.get("DriverVersion"), "date": date}
