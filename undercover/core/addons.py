"""Add-ons: optional parts UnderCover can use but never ships, because their licence isn't OSI-approved (e.g. F5-TTS's
weights are CC-BY-NC). The user adds one themselves and accepts its licence; the app keeps working on its open default
without it (ARCHITECTURE.md "Open-source goal").

One registry for every way of managing them: SafeHouse's "Add-ons" room and the installer step (phase 4b), and

    python -m undercover.addons                  list them (installed?, licence, size)
    python -m undercover.addons install f5       shows the licence summary, asks, then installs
    python -m undercover.addons remove f5

A module registers its add-ons when it's imported (`register(Addon(...))`; the cadence engines: core/cadence/engines.py).
What the user accepted, and when, is kept in models/addons.json (local only, never in git):
    {"f5": {"accepted": "2026-10-11 14:05", "licence": "CC-BY-NC-4.0", "installed": "2026-10-11 14:07"}}

An add-on installs pinned pip packages (`--no-deps -c constraints.txt`, so ROCm torch is never replaced) and model files
(fetched like every model: core.setup_models.ensure). Removing it deletes its model files and the packages only it uses.
"""
import json
import subprocess
import sys
import time
from dataclasses import dataclass, field

from . import fsutil
from .config import ROOT, path
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
    wired: bool = True         # False: registered (licence known, can be installed) but nothing uses it yet
    check: object = None       # () -> bool, installed? (default: its packages import and its files exist)
    imports: list = field(default_factory=list)    # module names that must import for it to count as installed

    def installed(self) -> bool:
        if self.check is not None:
            return bool(self.check())
        return _files_present(self) and _imports_ok(self.imports)


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
    """Installed and accepted: safe for the app to use."""
    a = ADDONS.get(addon_id)
    return bool(a and accepted(addon_id) and a.installed())


def status() -> list[dict]:
    st = _state()
    return [{"id": a.id, "name": a.name, "adds": a.adds, "licence": a.licence, "licence_url": a.licence_url,
             "licence_summary": a.licence_summary, "size_mb": a.size_mb, "wired": a.wired, "installed": a.installed(),
             "accepted": (st.get(a.id) or {}).get("accepted"), "installed_on": (st.get(a.id) or {}).get("installed")}
            for a in ADDONS.values()]


def _pip(*args) -> None:
    cmd = [sys.executable, "-m", "pip", *args]
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode != 0:
        with open(path("logs_dir") / "errors.log", "a", encoding="utf-8") as f:
            f.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] add-on pip {' '.join(args)}\n{r.stdout}\n{r.stderr}")
        raise ModelError("Installing the add-on's Python packages failed (details in logs/errors.log). "
                         "Check your internet connection and try again.")


def install(addon_id: str, accept: bool, progress=None) -> dict:
    """Install an add-on. accept must be True: the user has read the licence summary and agreed (recorded with a date).
    progress(message, done_bytes, total_bytes) while files download."""
    a = get(addon_id)
    if not accept:
        raise ModelError(f"{a.name} is under {a.licence}: {a.licence_summary} Accept its licence to install it.")
    st = _state()
    st[a.id] = {**(st.get(a.id) or {}), "accepted": time.strftime("%Y-%m-%d %H:%M"), "licence": a.licence}
    _save(st)
    if a.packages:
        if progress:
            progress(f"Installing {a.name}'s Python packages", 0, 0)
        _pip("install", "--no-deps", "-c", str(ROOT / "constraints.txt"), *a.packages)
    if a.files:
        from . import setup_models
        setup_models.ensure(lambda f, d, t: progress and progress(f"Downloading {a.name}: {f.split('/')[-1]}", d, t), files=a.files)
    st = _state()
    st[a.id]["installed"] = time.strftime("%Y-%m-%d %H:%M")
    _save(st)
    return next(x for x in status() if x["id"] == a.id)


def remove(addon_id: str) -> dict:
    """Remove an add-on's model files and the packages no other part of UnderCover uses. Its acceptance is kept
    (with the date), so re-adding it later doesn't ask again for the same licence."""
    a = get(addon_id)
    for f in a.files:
        (path("models_dir") / f).unlink(missing_ok=True)
    others = {p.split("==")[0].lower() for b in ADDONS.values() if b.id != a.id and b.installed() for p in b.packages}
    mine = [p.split("==")[0] for p in a.packages if p.split("==")[0].lower() not in others]
    if mine:
        _pip("uninstall", "-y", *mine)
    st = _state()
    if a.id in st:
        st[a.id].pop("installed", None)
        _save(st)
    return next(x for x in status() if x["id"] == a.id)


def main(argv=None):
    import argparse
    from . import cadence  # noqa: F401  (registers the cadence engines' add-ons)
    ap = argparse.ArgumentParser(prog="python -m undercover.addons", description="Optional add-ons (licences other than OSI)")
    ap.add_argument("action", nargs="?", default="list", choices=["list", "install", "remove"])
    ap.add_argument("id", nargs="?")
    ap.add_argument("--yes", action="store_true", help="accept the licence without asking")
    args = ap.parse_args(argv)
    if args.action == "list":
        for x in status():
            mark = "installed" if x["installed"] else "not installed"
            print(f"{x['id']:8} {x['name']:12} {mark:14} {x['licence']:14} ~{x['size_mb']} MB  {x['adds']}"
                  + ("" if x["wired"] else "  (registered; not used yet)"))
        return 0
    if not args.id:
        ap.error("which add-on? (python -m undercover.addons list)")
    a = get(args.id)
    if args.action == "remove":
        remove(a.id)
        print(f"{a.name} removed.")
        return 0
    if not args.yes:
        print(f"{a.name}: {a.adds}\nLicence: {a.licence} ({a.licence_url})\n{a.licence_summary}\nAbout {a.size_mb} MB.")
        if input("Accept the licence and install? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Nothing installed.")
            return 1
    install(a.id, True, lambda m, d, t: print(f"\r{m}" + (f": {d / 1e6:.0f}/{t / 1e6:.0f} MB" if t else ""), end="", flush=True))
    print(f"\n{a.name} installed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
