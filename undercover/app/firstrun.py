"""A fresh install's seeds (1.0: the First assignment is removed and parked on branch tutorial-redesign). The sample
song and the demo voice are there to try; the page's one-shot field starts empty, with an example placeholder (rc-page8).
The page reads `sample: true` on the song (/api/library/songs) and `demo: true` on the voice (/api/voices), nothing
from here; the one-shot planner knows them ("Hold On as Agent Smoke": the library's song, the installed voice).

- **Once per install** (`first_run`, at startup): `tutorial.json` next to the config file (the name it always had: the
  vault backs it up and restores it under that name) records that the seeding was decided. An install that already has
  covers or trained voices is an "existing install" (nothing seeded). A fresh one gets both seeds, once: a sample song
  or demo voice the user deletes isn't brought back at the next start.
- **The sample song** (`undercover/assets/sample/`: sample.json {title, artist, file, seconds, licence, notice} + the
  audio): registered as a normal song with `sample: true`. No audio file = no sample song.
- **The demo voice** "Agent Smoke" (`undercover/assets/demo-voice/agent-smoke/`, made by scripts/make_demo_voice.py):
  copied into voices_dir when it isn't there, its build (extra.demo_build, from the shipped manifest) recorded. Its meta
  says demo: true, trained: false; nothing treats its "person" as someone to search for (core/voices.is_demo).
Seeding only adds; it never replaces or deletes anything of the user's.
"""
import json
import shutil
import threading
import time
from pathlib import Path

from ..core import config, fsutil, storage
from ..core.config import path
from ..core.voices import ModelError

ASSETS = Path(__file__).resolve().parents[1] / "assets"
SAMPLE = ASSETS / "sample"
DEMO = ASSETS / "demo-voice" / "agent-smoke"
_LOCK = threading.Lock()


def state_file() -> Path:
    return Path(config.CONFIG_FILE).with_name("tutorial.json")


def load() -> dict | None:
    try:
        st = json.loads(state_file().read_text(encoding="utf-8"))
        return st if isinstance(st, dict) else None
    except (OSError, ValueError):
        return None


def _save(st: dict):
    f = state_file()
    if storage.ALLOWED_ROOTS is not None and not any(storage._inside(f, r) for r in storage.ALLOWED_ROOTS):
        raise ModelError(f"Test mode: {f} is outside the test's folders.")
    fsutil.write_text(f, json.dumps(st, indent=1))


def _existing_install() -> bool:
    """Covers made, or voices trained here: not a new copy, so nothing is seeded."""
    try:
        from ..frontman.pipeline import history
        if history():
            return True
    except Exception:   # noqa: BLE001
        pass
    from ..core.voices import list_voices
    return any((v.extra or {}).get("trained") for v in list_voices())


def first_run() -> dict:
    """Once per install, at startup. -> {"install": "fresh" | "existing install", "since", "seeded"?}; a state from an
    earlier start (any shape, the old {done} too) means it was decided already: nothing is seeded again."""
    with _LOCK:
        st = load()
        if st is not None:
            return st
        if _existing_install():
            st = {"install": "existing install", "since": time.strftime("%Y-%m-%d")}
        else:
            st = {"install": "fresh", "since": time.strftime("%Y-%m-%d"), "seeded": seed()}
        _save(st)
        return st


# ---------------------------------------------------------------- the seeds

def seed() -> dict:
    """Add the sample song and the demo voice when they're missing. Never replaces or removes anything."""
    out = {"song": None, "voice": None}
    try:
        out["song"] = seed_song()
    except (OSError, ValueError, ModelError) as e:
        print(f"The sample song couldn't be added: {e}", flush=True)
    try:
        out["voice"] = seed_voice()
    except (OSError, ModelError) as e:
        print(f"The demo voice couldn't be added: {e}", flush=True)
    return out


def sample_info() -> dict | None:
    """sample.json, when its audio file is there too (else None: no sample song yet)."""
    try:
        info = json.loads((SAMPLE / "sample.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    f = SAMPLE / str(info.get("file") or "")
    return info if info.get("file") and f.is_file() and f.parent == SAMPLE else None


def sample_song_id() -> str | None:
    """The sample song's id in this library, if it's there."""
    for m in path("uploads_dir").glob("*.json"):
        try:
            if json.loads(m.read_text(encoding="utf-8")).get("sample"):
                return m.stem
        except (OSError, ValueError):
            continue
    return None


def seed_song() -> str | None:
    info = sample_info()
    if info is None or sample_song_id():
        return sample_song_id() if info else None
    import hashlib
    from ..frontman.songs import register_song
    up = path("uploads_dir")
    if storage.ALLOWED_ROOTS is not None and storage.refuse([up]):
        raise ModelError(storage.refuse([up]))
    src = SAMPLE / info["file"]
    h = hashlib.sha256()
    with open(src, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    have = up / f"{h.hexdigest()[:16]}.json"
    if have.is_file():   # the user has this very recording already: mark it, keep their title and everything else
        meta = json.loads(have.read_text(encoding="utf-8"))
        meta["sample"] = True
        fsutil.write_text(have, json.dumps(meta))
        return have.stem
    # named like an upload in progress, so the Doctor's leftovers Fix covers a copy that fails halfway
    tmp = up / f"upload_sample_{int(time.time())}.part"
    shutil.copyfile(src, tmp)   # register_song moves it into place, named by its content
    meta = register_song(tmp, f"{info.get('title') or 'Sample'}{src.suffix.lower()}",
                         extra={"sample": True, "title": info.get("title") or "Sample", "artist": info.get("artist") or "",
                                "licence": info.get("licence") or "", "notice": info.get("notice") or ""})
    return meta["id"]


def demo_files() -> str | None:
    """Why the shipped demo voice can't be used (None: its files match manifest.json). The model files aren't in git:
    scripts/make_demo_voice.py makes them, the installer packs them."""
    import hashlib
    try:
        man = json.loads((DEMO / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "the demo voice has no manifest.json: run scripts/make_demo_voice.py"
    if not (DEMO / "meta.json").is_file():
        return "the demo voice's meta.json is missing: run scripts/make_demo_voice.py"
    for name, want in (man.get("files") or {}).items():
        f = DEMO / name
        if not f.is_file():
            return f"the demo voice's {name} isn't here (it isn't kept in git): run scripts/make_demo_voice.py"
        if f.stat().st_size != want.get("size"):
            return f"the demo voice's {name} isn't the one its manifest names: run scripts/make_demo_voice.py"
        h = hashlib.sha256()
        with open(f, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        if h.hexdigest() != want.get("sha256"):
            return f"the demo voice's {name} doesn't match its manifest's SHA-256: run scripts/make_demo_voice.py"
    if "model.pth" not in (man.get("files") or {}):
        return "the demo voice's manifest names no model.pth: run scripts/make_demo_voice.py"
    return None


def seed_voice() -> str | None:
    """Copy the demo voice into voices_dir when no demo voice is there (its files checked against manifest.json)."""
    from ..core.voices import list_voices
    have = next((v.name for v in list_voices() if (v.extra or {}).get("demo")), None)
    if have:
        return have
    why = demo_files()
    if why:
        print(f"The demo voice isn't available: {why}.", flush=True)
        return None
    dest = path("voices_dir") / DEMO.name
    if dest.exists():   # a folder of that name that isn't the demo: leave it alone
        return None
    if storage.ALLOWED_ROOTS is not None and storage.refuse([dest]):
        raise ModelError(storage.refuse([dest]))
    tmp = path("voices_dir") / f".{DEMO.name}-seeding"
    if tmp.exists():
        storage.remove_files([], [path("voices_dir")], dirs=[tmp])   # a seed an earlier start didn't finish
    # meta.json goes in last: the voice list only sees folders that have one (a dot folder too)
    shutil.copytree(DEMO, tmp, ignore=shutil.ignore_patterns("meta.json", "manifest.json"))
    meta = json.loads((DEMO / "meta.json").read_text(encoding="utf-8"))
    meta.update(name=DEMO.name, dir=str(dest), pth=str(dest / "model.pth"),
                index=str(dest / "model.index") if (tmp / "model.index").exists() else "")
    meta.setdefault("extra", {})["demo_build"] = _shipped_build()
    (tmp / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    fsutil.retry(lambda: tmp.replace(dest))
    from ..core import gpu
    gpu.forget_voice(DEMO.name)
    return DEMO.name


# ---------------------------------------------------------------- the shipped demo voice's build

def _shipped_build() -> str | None:
    """The shipped model.pth's SHA-256 (manifest.json), recorded at seeding: a future refresh of the demo voice can tell
    which build a copy has without hashing it."""
    try:
        return json.loads((DEMO / "manifest.json").read_text(encoding="utf-8"))["files"]["model.pth"]["sha256"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
