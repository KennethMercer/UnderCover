"""The First assignment's backend (phase 17; the page side is tutorial.js, contract docs/handoffs/tutorial.md).

    /api/status            tutorial: {done: "YYYY-MM-DD" | "existing install" | null,
                                      demo_update: {voice, display, made} | null, demo_previous: bool}
    POST /api/tutorial/demo/update   a newer Agent Smoke than the one installed ships: swap it in (the old one aside)
    POST /api/tutorial/demo/undo     the previous Agent Smoke back
    POST /api/tutorial/done    marks it done today            -> {tutorial: {done}}
    POST /api/tutorial/reset   seeds the sample song and the demo voice again if they're gone, done = null -> {tutorial}
    GET  /api/tutorial/greetings         Agent Smoke's greeting clips -> {greetings: [{from, to, text, url}],
                                         followon: {text, url} | null} ({greetings: [], followon: null} until shipped)
    GET  /api/tutorial/greetings/<file>  one of those clips (only files greetings.json names), cached a day

- **State:** `tutorial.json` next to the config file (git-ignored; a sandbox has its own).
- **First run** (`first_run`, at startup): no state yet. An install that already has covers or trained voices is an
  "existing install" (no brief, nothing seeded; Replay seeds). A fresh one gets done = null and both seeds.
- **The sample song** (`undercover/assets/sample/`: sample.json {title, artist, file, seconds, licence, notice} + the
  audio): registered as a normal song with `sample: true`. No audio file = no sample song (the first step then says
  "pick any song"). The user may delete it; Replay brings it back.
- **The greetings** (`undercover/assets/demo-voice/greetings/`: greetings.json {greetings: [{file, from, to, text}],
  followon: {file, text}} + the mp3s): the page picks one by the browser's local hour (from inclusive, to exclusive)
  and plays the follow-on when the song and the voice are both on the desk. Nothing else in assets is reachable.
- **A newer demo voice** (the user's words: "A newer Agent Smoke is available: Update"): the installed demo voice's
  build (extra.demo_build: the SHA-256 of its model.pth, written at seeding; an older seed is hashed once) against the
  shipped manifest's. Update is the user's choice: the installed folder moves aside to voices/.agent-smoke-previous (its
  meta.json renamed, so the voice list doesn't show it), the shipped one goes in, the name the user gave it stays.
  Undo swaps them back. One previous build is kept; the next update replaces it.
- **The demo voice** "Agent Smoke" (`undercover/assets/demo-voice/agent-smoke/`, made by tools/make_demo_voice.py):
  copied into voices_dir when it isn't there. Its meta says demo: true, trained: false; nothing treats its "person" as
  someone to search for (core/voices.is_demo). The user may delete it; Replay brings it back.
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
PREVIOUS = ".agent-smoke-previous"   # the demo voice before its last update (meta.json renamed: not in the voice list)
_BUILD: dict = {}                    # (model.pth, size, mtime) -> its SHA-256: hashed once
GREETINGS = ASSETS / "demo-voice" / "greetings"
GREETINGS_URL = "/api/tutorial/greetings/"
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


_CACHE: dict = {}   # (file, mtime) -> the state: /api/status asks on every poll


def status() -> dict:
    f = state_file()
    try:
        key = (str(f), f.stat().st_mtime_ns)
    except OSError:
        key = (str(f), None)
    if _CACHE.get("key") != key:
        _CACHE.update(key=key, done=(load() or {}).get("done"))
    try:
        upd = demo_update()
    except Exception:   # noqa: BLE001 - the status never fails over the offer
        upd = None
    return {"done": _CACHE["done"], "demo_update": upd, "demo_previous": (path("voices_dir") / PREVIOUS).is_dir()}


def _existing_install() -> bool:
    """Covers made, or voices trained here, before the tutorial existed."""
    try:
        from ..frontman.pipeline import history
        if history():
            return True
    except Exception:   # noqa: BLE001
        pass
    from ..core.voices import list_voices
    return any((v.extra or {}).get("trained") for v in list_voices())


def first_run() -> dict:
    """Once per install, at startup."""
    with _LOCK:
        if load() is not None:
            return status()
        if _existing_install():
            _save({"done": "existing install", "since": time.strftime("%Y-%m-%d")})
        else:
            _save({"done": None, "since": time.strftime("%Y-%m-%d")})
            seed()
        return status()


def mark_done() -> dict:
    with _LOCK:
        st = load() or {}
        st["done"] = time.strftime("%Y-%m-%d")
        _save(st)
        return {"tutorial": status()}


def reset() -> dict:
    """Replay: the seeds back if they're gone, and the brief shows again."""
    with _LOCK:
        seeded = seed()
        st = load() or {}
        st["done"] = None
        _save(st)
        return {"tutorial": status(), "seeded": seeded}


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
    tools/make_demo_voice.py makes them, the installer packs them."""
    import hashlib
    try:
        man = json.loads((DEMO / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "the demo voice has no manifest.json: run tools/make_demo_voice.py"
    if not (DEMO / "meta.json").is_file():
        return "the demo voice's meta.json is missing: run tools/make_demo_voice.py"
    for name, want in (man.get("files") or {}).items():
        f = DEMO / name
        if not f.is_file():
            return f"the demo voice's {name} isn't here (it isn't kept in git): run tools/make_demo_voice.py"
        if f.stat().st_size != want.get("size"):
            return f"the demo voice's {name} isn't the one its manifest names: run tools/make_demo_voice.py"
        h = hashlib.sha256()
        with open(f, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        if h.hexdigest() != want.get("sha256"):
            return f"the demo voice's {name} doesn't match its manifest's SHA-256: run tools/make_demo_voice.py"
    if "model.pth" not in (man.get("files") or {}):
        return "the demo voice's manifest names no model.pth: run tools/make_demo_voice.py"
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


# ---------------------------------------------------------------- a newer demo voice

def _sha(f: Path) -> str:
    import hashlib
    st = f.stat()
    key = (str(f), st.st_size, st.st_mtime_ns)
    if key not in _BUILD:
        h = hashlib.sha256()
        with open(f, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        _BUILD[key] = h.hexdigest()
    return _BUILD[key]


def _shipped_build() -> str | None:
    try:
        return json.loads((DEMO / "manifest.json").read_text(encoding="utf-8"))["files"]["model.pth"]["sha256"]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _installed_demo():
    from ..core.voices import get_voice
    if not (path("voices_dir") / DEMO.name / "meta.json").is_file():
        return None
    try:
        v = get_voice(DEMO.name)
    except Exception:   # noqa: BLE001 - not a readable voice: nothing to offer
        return None
    return v if (v.extra or {}).get("demo") else None


def demo_update() -> dict | None:
    """{voice, display, made} when the shipped demo voice is a newer build than the installed one; else None. Asked on
    every /api/status: answered from a cache until the installed meta, its model or the shipped manifest changes."""
    def mtime(f):
        try:
            return f.stat().st_mtime_ns
        except OSError:
            return None
    dest = path("voices_dir") / DEMO.name
    key = (str(dest), mtime(dest / "meta.json"), mtime(dest / "model.pth"), mtime(DEMO / "manifest.json"))
    if _CACHE.get("demo_key") != key:
        _CACHE.update(demo_key=key, demo=_demo_update())
    return _CACHE["demo"]


def _demo_update() -> dict | None:
    v = _installed_demo()
    ship = _shipped_build()
    if v is None or not ship or not Path(v.pth).is_file():
        return None
    have = (v.extra or {}).get("demo_build") or _sha(Path(v.pth))
    if have == ship or demo_files():   # the same build, or the shipped files aren't whole (nothing to offer)
        return None
    try:
        made = json.loads((DEMO / "manifest.json").read_text(encoding="utf-8")).get("made")
    except (OSError, ValueError):
        made = None
    return {"voice": v.name, "display": v.display or "Agent Smoke", "made": made}


def update_demo() -> dict:
    """The newer Agent Smoke in, the installed one aside (PREVIOUS, replacing an older previous). The user's choice."""
    from ..core import gpu
    with _LOCK:
        upd = demo_update()
        if upd is None:
            raise ModelError("Agent Smoke is already the newest.")
        vd = path("voices_dir")
        dest, prev, tmp = vd / DEMO.name, vd / PREVIOUS, vd / f".{DEMO.name}-updating"
        if storage.ALLOWED_ROOTS is not None and storage.refuse([dest, prev, tmp]):
            raise ModelError(storage.refuse([dest, prev, tmp]))
        old = json.loads((dest / "meta.json").read_text(encoding="utf-8"))
        for d in (tmp, prev):
            if d.exists():   # an update an earlier try didn't finish; the previous build before this one
                storage.remove_files([], [vd], dirs=[d])
        shutil.copytree(DEMO, tmp, ignore=shutil.ignore_patterns("meta.json", "manifest.json"))
        meta = json.loads((DEMO / "meta.json").read_text(encoding="utf-8"))
        meta.update(name=DEMO.name, dir=str(dest), pth=str(dest / "model.pth"),
                    index=str(dest / "model.index") if (tmp / "model.index").exists() else "",
                    display=old.get("display") or meta.get("display") or "")
        meta.setdefault("extra", {})["demo_build"] = _shipped_build()
        (tmp / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        fsutil.retry(lambda: dest.replace(prev))
        fsutil.retry(lambda: (prev / "meta.json").replace(prev / "meta.json.previous"))   # out of the voice list
        fsutil.retry(lambda: tmp.replace(dest))
        gpu.forget_voice(DEMO.name)
        return {"tutorial": status(), "updated": DEMO.name}


def undo_demo() -> dict:
    """The previous Agent Smoke back (the newer one goes: it ships with UnderCover, Update brings it again)."""
    from ..core import gpu
    with _LOCK:
        vd = path("voices_dir")
        dest, prev = vd / DEMO.name, vd / PREVIOUS
        if not (prev / "meta.json.previous").is_file():
            raise ModelError("There's no earlier Agent Smoke to go back to.")
        if storage.ALLOWED_ROOTS is not None and storage.refuse([dest, prev]):
            raise ModelError(storage.refuse([dest, prev]))
        if dest.exists():
            storage.remove_files([], [vd], dirs=[dest])
        fsutil.retry(lambda: (prev / "meta.json.previous").replace(prev / "meta.json"))
        fsutil.retry(lambda: prev.replace(dest))
        gpu.forget_voice(DEMO.name)
        return {"tutorial": status()}


# ---------------------------------------------------------------- the greetings

def _greetings_manifest() -> dict:
    try:
        m = json.loads((GREETINGS / "greetings.json").read_text(encoding="utf-8"))
        return m if isinstance(m, dict) else {}
    except (OSError, ValueError):
        return {}


def _shipped(entry) -> bool:
    return (isinstance(entry, dict) and isinstance(entry.get("file"), str) and "/" not in entry["file"]
            and "\\" not in entry["file"] and (GREETINGS / entry["file"]).is_file())


def _url(name: str) -> str:
    """The clip's address; ?v= changes when the file does (the clips are cached a day)."""
    from urllib.parse import quote
    return f"{GREETINGS_URL}{quote(name)}?v={int((GREETINGS / name).stat().st_mtime)}"


def greetings() -> dict:
    """greetings.json with a url in place of each file; entries whose clip isn't there are left out."""
    m = _greetings_manifest()
    out = [{"from": int(g["from"]), "to": int(g["to"]), "text": str(g.get("text") or ""), "url": _url(g["file"])}
           for g in m.get("greetings") or [] if _shipped(g) and isinstance(g.get("from"), int) and isinstance(g.get("to"), int)]
    f = m.get("followon")
    follow = {"text": str(f.get("text") or ""), "url": _url(f["file"])} if out and _shipped(f) else None
    return {"greetings": out, "followon": follow}


def greeting_file(name: str) -> Path | None:
    """A clip greetings.json names (the allow-list), or None."""
    m = _greetings_manifest()
    named = {g.get("file") for g in m.get("greetings") or [] if _shipped(g)}
    if _shipped(m.get("followon")):
        named.add(m["followon"]["file"])
    return GREETINGS / name if name in named else None


# ---------------------------------------------------------------- routes

def handle(h, method: str, p: str) -> bool:
    if not p.startswith("/api/tutorial/"):
        return False
    if method == "GET" and p == "/api/tutorial/greetings":
        h._json(greetings())
        return True
    if method == "GET" and p.startswith(GREETINGS_URL):
        from urllib.parse import unquote
        f = greeting_file(unquote(p[len(GREETINGS_URL):]))
        if f is None:
            h._error("Not found", 404)
        else:
            h._send_file(f, cache="public, max-age=86400")
        return True
    if method != "POST":
        h._error("Not found", 404)
        return True
    h._body()
    if p == "/api/tutorial/done":
        h._json(mark_done())
    elif p == "/api/tutorial/reset":
        h._json(reset())
    elif p in ("/api/tutorial/demo/update", "/api/tutorial/demo/undo"):
        try:
            h._json(update_demo() if p.endswith("update") else undo_demo())
        except ModelError as e:
            h._error(str(e), 409)
    else:
        h._error("Not found", 404)
    return True
