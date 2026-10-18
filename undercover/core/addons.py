"""Add-ons: the parts UnderCover is built from, and the optional ones it never ships.

Included parts (Chatterbox, Whisper, Kokoro, RVC, separation, speaker check) come with UnderCover and are open source;
they're listed so SafeHouse's "Add-ons" room shows the full picture. Optional add-ons have a licence that isn't
OSI-approved (e.g. F5-TTS's weights are CC-BY-NC): a user adds one themselves and accepts its licence; the app keeps
working on its open default without it (ARCHITECTURE.md "Open-source goal").

One registry for every way of managing them: SafeHouse's "Add-ons" room (/api/addons), the installer's optional step,
and the command line (undercover/addons.py):

    python -m undercover.addons                                  list them (installed?, licence, size)
    python -m undercover.addons install f5                       shows the licence summary, asks, then installs
    python -m undercover.addons install f5 --accept-licence      (the installer, after the user ticked "I accept")
    python -m undercover.addons remove f5

A module registers its parts when it's imported (`register(Addon(...))`; the cadence engines: core/cadence/engines.py,
Kokoro: speakeasy/tts.py, the core's own parts at the bottom of this file).
What the user accepted, and when, is kept in models/addons.json (local only, never in git):
    {"f5": {"accepted": "2026-10-11 14:05", "licence": "CC-BY-NC-4.0", "installed": "2026-10-11 14:07"}}

An add-on installs pinned pip packages (`--no-deps -c installer/constraints.txt`, so the GPU's torch is never replaced; through
the installer's uv when there is one, since its venv has no pip) and model files (core.setup_models.ensure: pinned
revisions, SHA-256 checked). Removing it deletes its model files and the packages only it uses.

A part that is updated in place (yt-dlp: YouTube changes often) lists its pin in `packages` and the wheel's SHA-256 in
`hashes`; `update_pinned()` installs exactly that (`--no-deps --require-hashes -c constraints.txt`), never "latest".
Moving a pin is a code change MasterPlan approves; installer/requirements/common.txt must carry the same pin (smoke checks).
"""
import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import fsutil
from .config import CONSTRAINTS, ROOT, path
from .voices import ModelError


@dataclass
class Addon:
    id: str                    # short, stable ("f5")
    name: str                  # "F5-TTS"
    adds: str                  # what it gives the user, one plain sentence
    licence: str               # SPDX-ish name of the most restrictive part ("CC-BY-NC-4.0")
    licence_url: str
    licence_summary: str       # plain words: what accepting means
    size_mb: int               # download size, roughly
    packages: list = field(default_factory=list)   # pip pins installed --no-deps ("f5-tts==1.1.22")
    files: dict = field(default_factory=dict)      # path under models/ -> URL (pinned revision)
    sha256: dict = field(default_factory=dict)     # path under models/ -> SHA-256 of that file
    wired: bool = True         # False: registered (licence known) but nothing uses it yet, so it can't be installed
    check: object = None       # () -> bool, installed? (default: its packages import and its files exist)
    imports: list = field(default_factory=list)    # module names that must import for it to count as installed
    included: bool = False     # comes with UnderCover (open source): listed, never installed or removed here
    modules: list = field(default_factory=list)    # the suite modules that use it ("speakeasy", ...)
    paths: list = field(default_factory=list)      # folders/files under models/ it occupies (default: its files)
    open_note: str = ""        # included parts: anything to know about the licence ("credit the authors")
    short: str = ""            # the installer's one-line what ("lighter on graphics memory, slightly flatter speech")
    use: str = ""              # the installer's one-line who may use what it makes ("personal, non-commercial use only")
    hashes: dict = field(default_factory=dict)     # package name -> SHA-256 of its wheel: the pin "Update" installs

    def installed(self) -> bool:
        if self.check is not None:
            return bool(self.check())
        return _files_present(self) and _imports_ok(self.imports)

    def disk_bytes(self) -> int:
        total = 0
        for p in (self.paths or list(self.files)):
            f = path("models_dir") / p
            if f.is_file():
                total += f.stat().st_size
            elif f.is_dir():
                total += sum(x.stat().st_size for x in f.rglob("*") if x.is_file())
        return total


ADDONS: dict[str, Addon] = {}


def register(addon: Addon) -> Addon:
    ADDONS[addon.id] = addon
    return addon


def get(addon_id: str) -> Addon:
    if addon_id not in ADDONS:
        raise ModelError(f"There's no add-on called '{addon_id}' (there are: {', '.join(ADDONS) or 'none'}).")
    return ADDONS[addon_id]


def _file():
    return path("models_dir") / "addons.json"


def _state() -> dict:
    try:
        return json.loads(_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(st: dict):
    fsutil.write_text(_file(), json.dumps(st, indent=1))


def forget(addon_id: str):
    """Drop everything recorded about an add-on (tests clean up with this)."""
    st = _state()
    if st.pop(addon_id, None) is not None:
        _save(st) if st else _file().unlink(missing_ok=True)


def accepted(addon_id: str) -> dict | None:
    """{"accepted": date, "licence": ...} when the user accepted this add-on's licence (and it's the same licence)."""
    rec = _state().get(addon_id)
    a = ADDONS.get(addon_id)
    return rec if rec and rec.get("accepted") and (a is None or rec.get("licence") == a.licence) else None


def _files_present(a: Addon) -> bool:
    return all((path("models_dir") / f).exists() for f in a.files)


def _imports_ok(mods: list) -> bool:
    """Whether the modules can be found, without importing them (the server must not load an engine's stack)."""
    import importlib.util
    try:
        return all(importlib.util.find_spec(m) is not None for m in mods)
    except (ImportError, ValueError):
        return False


def available(addon_id: str) -> bool:
    """Ready for the app to use: an included part that's there, or an optional one installed and accepted."""
    a = ADDONS.get(addon_id)
    if not a:
        return False
    return a.installed() and (a.included or bool(accepted(addon_id)))


def info(a: Addon, st: dict | None = None) -> dict:
    rec = (st if st is not None else _state()).get(a.id) or {}
    inst = a.installed()
    return {"id": a.id, "name": a.name, "adds": a.adds, "licence": a.licence, "licence_url": a.licence_url,
            "licence_summary": a.licence_summary, "size_mb": a.size_mb, "wired": a.wired, "installed": inst,
            "included": a.included, "optional": not a.included, "modules": a.modules, "open_note": a.open_note,
            "short": a.short, "use": a.use,
            "installable": not a.included and a.wired, "disk": a.disk_bytes() if inst or a.included else 0,
            "packages": len(a.packages), "files": sorted(a.paths or a.files),
            "accepted": rec.get("accepted"), "accepted_licence": rec.get("licence"), "installed_on": rec.get("installed")}


def status() -> list[dict]:
    """Every part: optional add-ons first (the choices), then the included ones."""
    st = _state()
    return [info(a, st) for a in sorted(ADDONS.values(), key=lambda a: (a.included, not a.wired, a.name.lower()))]


def _uv() -> Path | None:
    """The installer's uv (tools/uv/uv.exe): its venvs have no pip."""
    for p in (ROOT / "tools" / "uv" / "uv.exe", ROOT / "tools" / "uv" / "uv"):
        if p.exists():
            return p
    return None


def _pip(*args, what="the add-on's Python packages") -> None:
    uv = _uv()
    cmd = [str(uv), "pip", *args, "--python", sys.executable] if uv else [sys.executable, "-m", "pip", *args]
    if uv and args and args[0] == "uninstall":
        cmd = [c for c in cmd if c != "-y"]   # uv never asks
    env = None
    if uv:   # like setup.ps1: uv's cache and config stay inside the app folder, never in the user's profile
        env = {**os.environ, "UV_CACHE_DIR": str(ROOT / "cache" / "uv"), "UV_NO_CONFIG": "1",
               "UV_PYTHON_INSTALL_DIR": str(ROOT / "tools" / "python")}
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, env=env,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode != 0:
        with open(path("logs_dir") / "errors.log", "a", encoding="utf-8") as f:
            f.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] add-on {' '.join(cmd)}\n{r.stdout}\n{r.stderr}")
        raise ModelError(f"Installing {what} failed (details in logs/errors.log). "
                         "Check your internet connection and try again.")


# ---------------------------------------------------------------- pinned updates (yt-dlp)

def package_version(name: str) -> str | None:
    """Installed version of a package, from its metadata (a loaded module may be older after an update)."""
    try:
        from importlib.metadata import version
        return version(name)
    except Exception:
        return None


def _vkey(v: str | None) -> tuple:
    import re
    return tuple(int(x) for x in re.findall(r"\d+", v or ""))


def pin_of(addon_id: str) -> tuple[str, str, str] | None:
    """(package, pinned version, wheel SHA-256) for a part that has a hashed pin, else None."""
    a = ADDONS.get(addon_id)
    if not a:
        return None
    for spec in a.packages:
        name, _, ver = spec.partition("==")
        sha = a.hashes.get(name)
        if ver and sha:
            return name, ver, sha
    return None


def pin_state(addon_id: str) -> dict:
    """{"package", "installed", "pinned", "update"}: update is True only when the package is installed and older
    than the pin (a missing one would be an install, which needs the user's OK; a newer one is left alone)."""
    pin = pin_of(addon_id)
    if not pin:
        return {"package": None, "installed": None, "pinned": None, "update": False}
    name, ver, _ = pin
    have = package_version(name)
    return {"package": name, "installed": have, "pinned": ver, "update": bool(have) and _vkey(have) < _vkey(ver)}


def updated(addon_id: str) -> str | None:
    """When the app last updated this part to its pin (ISO date and time, local), or None."""
    return (_state().get(addon_id) or {}).get("updated")


def _need_pin(addon_id: str) -> tuple[str, str, str]:
    pin = pin_of(addon_id)
    if not pin:
        raise ModelError(f"There's no pinned version for '{addon_id}', so it can't be updated. Updates only install a "
                         "version UnderCover has checked.")
    return pin


def pin_command(addon_id: str, req_file) -> list:
    """The pip arguments that install the pin: this one version, its wheel's SHA-256 required, torch and everything
    else held by constraints.txt, no dependencies. Raises ModelError when the part has no hashed pin."""
    name, ver, sha = _need_pin(addon_id)
    Path(req_file).write_text(f"{name}=={ver} --hash=sha256:{sha}\n", encoding="utf-8")
    return ["install", "--no-deps", "--require-hashes", "-c", str(CONSTRAINTS), "-r", str(req_file)]


def update_pinned(addon_id: str) -> dict:
    """Bring an installed part to its pinned version. Takes effect after a restart. -> before / after / pinned / changed."""
    import tempfile
    _need_pin(addon_id)
    st = pin_state(addon_id)
    if not st["installed"]:
        raise ModelError(f"{get(addon_id).name} isn't installed, and it comes with UnderCover, so this install is "
                         "damaged. Run UnderCover's setup again to repair it.")
    if not st["update"]:
        return {"before": st["installed"], "after": st["installed"], "pinned": st["pinned"], "changed": False}
    with tempfile.TemporaryDirectory() as d:
        _pip(*pin_command(addon_id, Path(d) / "pin.txt"), what=f"the pinned {get(addon_id).name}")
    after = package_version(st["package"])
    if after != st["pinned"]:
        raise ModelError(f"The update finished but {st['package']} reads {after or 'nothing'}, not {st['pinned']}. "
                         "Restart UnderCover and check again.")
    s = _state()
    s.setdefault(addon_id, {}).update({"updated": time.strftime("%Y-%m-%dT%H:%M:%S"), "from": st["installed"]})
    _save(s)
    from . import manifest
    manifest.note_package(st["package"], after)   # the install record follows, so the Doctor doesn't call it changed
    return {"before": st["installed"], "after": after, "pinned": st["pinned"], "changed": True}


def _optional(a: Addon, doing: str) -> None:
    if a.included:
        raise ModelError(f"{a.name} comes with UnderCover; there's nothing to {doing}.")
    if not a.wired:
        raise ModelError(f"{a.name} isn't used by anything yet, so there's nothing to {doing} for now.")


def install(addon_id: str, accept: bool, progress=None, cancel=None) -> dict:
    """Install an add-on. accept must be True: the user has read the licence summary and agreed (recorded with a date).
    progress(message, done_bytes, total_bytes) while it installs; cancel() -> True stops between files."""
    a = get(addon_id)
    _optional(a, "install")
    if not accept:
        raise ModelError(f"{a.name} is under {a.licence}: {a.licence_summary} Accept its licence to install it.")
    st = _state()
    st[a.id] = {**(st.get(a.id) or {}), "accepted": time.strftime("%Y-%m-%d %H:%M"), "licence": a.licence}
    _save(st)
    if a.packages:
        if progress:
            progress(f"Installing {a.name}'s Python packages", 0, 0)
        hashed = ROOT / "installer" / "requirements" / f"addon-{a.id}.txt"   # the same pins, each with its SHA-256
        if hashed.exists():
            _pip("install", "--no-deps", "--require-hashes", "-r", str(hashed))
        else:
            _pip("install", "--no-deps", "-c", str(CONSTRAINTS), *a.packages)
    if a.files:
        from . import setup_models

        def prog(f, d, t):
            if cancel and cancel():
                raise InterruptedError
            if progress:
                progress(f"Downloading {a.name}: {f.split('/')[-1]}", d, t)
        setup_models.ensure(prog, files=a.files, sha256=a.sha256)
    st = _state()
    st[a.id]["installed"] = time.strftime("%Y-%m-%d %H:%M")
    _save(st)
    return info(a)


def remove(addon_id: str) -> dict:
    """Remove an add-on's model files and the packages no other part of UnderCover uses. Its acceptance is kept
    (with the date), so re-adding it later doesn't ask again for the same licence. -> its info + "freed" (bytes)."""
    a = get(addon_id)
    if a.included:
        raise ModelError(f"{a.name} comes with UnderCover and can't be removed here.")
    freed = a.disk_bytes()
    for f in a.files:
        (path("models_dir") / f).unlink(missing_ok=True)
    for p in sorted({(path("models_dir") / f).parent for f in a.files}, key=lambda x: -len(x.parts)):
        while p != path("models_dir") and p.is_dir() and not any(p.iterdir()):   # empty folders it left
            p.rmdir()
            p = p.parent
    others = {p.split("==")[0].lower() for b in ADDONS.values() if b.id != a.id and b.installed() for p in b.packages}
    mine = [p.split("==")[0] for p in a.packages if p.split("==")[0].lower() not in others]
    if mine:
        _pip("uninstall", "-y", *mine)
    st = _state()
    if a.id in st:
        st[a.id].pop("installed", None)
        _save(st)
    return {**info(a), "freed": freed}


def start_install(addon_id: str, accept: bool):
    """Install as a job (the shared job feed shows it: kind "addon"), in its own thread: it's downloads and pip, not
    the GPU. -> the Job."""
    from . import jobs
    a = get(addon_id)
    _optional(a, "install")
    if not accept:
        raise ModelError(f"Tick \"I accept\" under {a.name}'s licence first.")
    if any(j.kind == "addon" and not j.finished for j in jobs.JOBS.values()):
        raise ModelError("Another add-on is being installed. Wait for it to finish.")
    job = jobs.Job("addon", title=f"Adding {a.name}")
    jobs.JOBS[job.id] = job

    def run():
        with job.cond:
            job.state = "running"
        last = [0.0]

        def progress(msg, done, total):
            now = time.time()
            if now - last[0] > 0.4 or (total and done >= total) or not total:
                last[0] = now
                job.push(stage="installing", frac=round(done / total, 3) if total else None,
                         message=msg + (f", {done / 1e6:.0f} of {total / 1e6:.0f} MB" if total else ""))
        try:
            r = install(a.id, True, progress, lambda: job.cancel)
            job.finish("done", message=f"{a.name} is installed.", addon=r)
        except InterruptedError:
            job.finish("cancelled", message=f"Cancelled. {a.name} wasn't installed.")
        except ModelError as e:
            job.finish("error", message=str(e))
        except Exception as e:
            job.finish("error", message=jobs.friendly(e, f"installing {a.name}"))

    threading.Thread(target=run, daemon=True).start()
    return job


# ---------------------------------------------------------------- the core's own included parts

OPEN = "Open source ({}): free to use, change and share, also commercially."
register(Addon(
    id="rvc", name="RVC voice engine", included=True, modules=["frontman", "doubleagent", "speakeasy"],
    adds="Turns a singer or reader into the chosen voice, and trains new voices.",
    licence="MIT", licence_url="https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI",
    licence_summary=OPEN.format("MIT") + " Its base models (lj1995/VoiceConversionWebUI) are MIT too.",
    size_mb=560, paths=["rvc"], check=lambda: (path("models_dir") / "rvc").is_dir()))
register(Addon(
    id="separation", name="Vocal separation", included=True, modules=["frontman", "doubleagent"],
    adds="Takes the singing out of a song (and the reverb out of the singing).",
    licence="MIT", licence_url="https://github.com/nomadkaraoke/python-audio-separator",
    licence_summary=OPEN.format("MIT") + " The separation models come from community authors.",
    open_note="Open-source code; the models' own licences are being checked (FOSS-readiness phase).",
    size_mb=1200, paths=["separator"], check=lambda: (path("models_dir") / "separator").is_dir()))
register(Addon(
    id="speaker", name="Speaker check", included=True, modules=["doubleagent"],
    adds="Keeps only the right person's voice when DoubleAgent collects clips.",
    licence="CC-BY-4.0", licence_url="https://huggingface.co/Wespeaker/wespeaker-voxceleb-resnet34-LM",
    licence_summary=("WeSpeaker's code is Apache-2.0 and this model is Creative Commons Attribution 4.0: free to use, "
                     "also commercially, as long as the authors are credited."),
    open_note="Attribution: the WeSpeaker authors (wenet-e2e).",
    size_mb=26, paths=["speaker"], check=lambda: (path("models_dir") / "speaker").is_dir()))

register(Addon(
    id="ytdlp", name="YouTube downloader (yt-dlp)", included=True, modules=["frontman", "doubleagent"],
    adds="Finds songs and clips on YouTube. System's Update installs the version pinned here.",
    licence="Unlicense", licence_url="https://github.com/yt-dlp/yt-dlp/blob/master/LICENSE",
    licence_summary="Public domain (The Unlicense): free to use, change and share, also commercially.",
    size_mb=3, packages=["yt-dlp==2026.8.19"],
    hashes={"yt-dlp": "1d57897e94c6665a0a6f9bc54b34e584284e32c034ffab3a7df25d8f7b24eedf"},   # yt_dlp-2026.8.19-py3-none-any.whl
    imports=["yt_dlp"], check=lambda: _imports_ok(["yt_dlp"])))


def register_test_addon(src) -> Addon:
    """A fake optional add-on for tests (smoke, a sandbox server with UNDERCOVER_TEST_ADDON=<a small file>): its one
    "model file" is a copy of src, checked by SHA-256; no packages. Never registered in normal use."""
    from .setup_models import sha256_of
    src = Path(src)
    return register(Addon(
        id="test", name="Test add-on", adds="Nothing: it only proves installing and removing works.",
        licence="TEST-1.0", licence_url="https://example.invalid/test-licence",
        licence_summary="A pretend licence for testing: accepting it means nothing.", size_mb=max(1, src.stat().st_size // 1_000_000),
        files={"_test/test-addon.bin": src.resolve().as_uri()}, sha256={"_test/test-addon.bin": sha256_of(src)},
        modules=["safehouse"]))


if os.environ.get("UNDERCOVER_TEST_ADDON"):
    register_test_addon(os.environ["UNDERCOVER_TEST_ADDON"])
