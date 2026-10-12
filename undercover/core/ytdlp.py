"""Self-healing yt-dlp (phase 16; ARCHITECTURE "Resilience and self-healing" -> "Self-healing yt-dlp"; the one exception
to "never latest" in CLAUDE.md). YouTube changes something every few weeks and yt-dlp ships the fix within days, so a
pinned yt-dlp breaks between our releases. Only yt-dlp and the yt-dlp-ejs its own `pin` extra names follow PyPI.

- **The pairs.** The venv keeps the pair pinned in `installer/requirements/common.txt`, untouched: it's the last resort.
  Newer pairs live in side folders, `<cache>/ytdlp/<yt-dlp version>+ejs<version>/` (each wheel unpacked, `pair.json`).
  `state.json` there names the **active** pair and the **last known good** one; `activate()` (at startup, before anything
  imports yt_dlp) puts the active folder first on sys.path. Swapping and rolling back only move that pointer.
- **Detect** (`note`, fed by core/youtube's watched YoutubeDL): the same breakage (challenge, signature, "unplayable",
  "format not available") on at least BROKEN_VIDEOS different videos within WINDOW, more failures than successes; YouTube's
  bot check, private or removed videos don't count.
- **Fetch** (`latest_pair`): PyPI's newest yt-dlp and the yt-dlp-ejs version its `pin` extra names; both wheels checked
  against PyPI's published SHA-256 (`download`).
- **Stage and test** (`stage`, `selftest`): unpacked into a staging folder, then a separate Python resolves KNOWN_GOOD
  public videos (metadata only, no downloads) with it; it's swapped in only if it resolves at least as many as the
  current pair, and at least one.
- **Roll back** (`heal`): every heal scores the current pair and the last known good (and the venv's) too; when the
  current one does worse than one of them, that one comes back.
- **Say so:** every step goes to logs/doctor.log; the Doctor shows the pair, a missing puzzle solver and an unhealed
  breakage (core/doctor.c_ytdlp); `auto` (a Deep Cover switch) lets a detected breakage start a heal by itself.
Nothing here touches the venv: the Doctor's package check keeps holding it to its pins.
"""
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

from . import fsutil, storage
from .config import ROOT, path
from .voices import ModelError

PKG, EJS = "yt-dlp", "yt-dlp-ejs"
KNOWN_GOOD = ["jNQXAC9IVRw", "dQw4w9WgXcQ", "9bZkp7q19f0"]   # public, long-lived, ordinary videos
BROKEN_VIDEOS = 3          # different videos failing the same way...
WINDOW = 30 * 60           # ...within this many seconds
HOLD = 6 * 3600            # after a heal that found nothing better, wait this long before trying again by itself
MAX_WHEEL = 50 * 1024 * 1024
SELFTEST_TIMEOUT = 180
BROKEN = ("Requested format is not available", "challenge", "nsig", "n-sig", "Signature extraction failed",
          "signature function", "Only images are available", "unplayable", "Unable to extract", "JavaScript runtime",
          "solver", "player response", "PO Token")
NOT_BROKEN = ("not a bot", "Sign in to confirm", "Private video", "has been removed", "copyright", "confirm your age",
              "members-only", "Premieres in", "live event", "This video is unavailable", "Video unavailable",
              "is not available in your country", "HTTP Error 429")

LOCK = threading.RLock()
_EVENTS: list = []          # (time, video id, "ok" | "broken")
_HEAL = {"job": None}


class HealError(ModelError):
    pass


# ---------------------------------------------------------------- where

def home() -> Path:
    return path("cache_dir") / "ytdlp"


def _guard(p: Path):
    """Test mode: write only inside the test's folders."""
    if storage.ALLOWED_ROOTS is not None:
        why = storage.refuse([p])
        if why:
            raise HealError(why)


def load() -> dict:
    try:
        st = json.loads((home() / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st = {}
    st.setdefault("auto", True)
    st.setdefault("active", None)
    st.setdefault("last_good", None)
    return st


def save(st: dict):
    _guard(home())
    home().mkdir(parents=True, exist_ok=True)
    fsutil.write_text(home() / "state.json", json.dumps(st, indent=1))


def log(msg: str):
    from . import doctor
    doctor.log(f"yt-dlp: {msg}")


def _pair_of(d: Path) -> dict | None:
    try:
        p = json.loads((d / "pair.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return p if (d / "yt_dlp" / "__init__.py").is_file() and p.get("yt") else None


def side(name: str | None) -> Path | None:
    """A side pair's folder, if it's whole."""
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return None
    d = home() / name
    return d if _pair_of(d) else None


def venv_pair() -> dict:
    """The pair installed in the venv (its own site-packages only, never a side folder)."""
    from importlib.metadata import distributions
    import sysconfig
    where = [sysconfig.get_paths()["purelib"], sysconfig.get_paths()["platlib"]]
    got = {}
    for dist in distributions(path=where):
        try:
            got[dist.metadata["Name"].lower().replace("_", "-")] = dist.version
        except Exception:   # noqa: BLE001
            pass
    return {"yt": got.get(PKG), "ejs": got.get(EJS), "dir": None, "source": "venv"}


def vkey(v: str | None) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v or ""))


# ---------------------------------------------------------------- activation (startup)

ACTIVE: dict = {"dir": None}


def activate() -> dict:
    """Put the active side pair first on sys.path (before anything imports yt_dlp). A missing or broken folder falls back
    to the last known good, then to the venv's pair, and says so in the log."""
    st = load()
    for name in (st["active"], st["last_good"]):
        d = side(name)
        if d is not None:
            if name != st["active"]:
                log(f"the active pair {st['active']} is missing or damaged: using the last known good {name}")
                st["active"] = name
                try:
                    save(st)
                except (OSError, HealError):
                    pass
            _use(d)
            return {**_pair_of(d), "dir": str(d), "source": "side"}
        if name:
            log(f"pair {name} isn't there any more")
    if st["active"]:
        log("no side pair is usable: using the venv's pinned pair")
    _use(None)
    return venv_pair()


def _use(d: Path | None):
    """Point imports at this pair (None = the venv's). Modules already imported stay until hot_swap clears them."""
    old = ACTIVE["dir"]
    if old and old in sys.path:
        sys.path.remove(old)
    if d is not None:
        sys.path.insert(0, str(d))
    ACTIVE["dir"] = str(d) if d is not None else None


def current() -> dict:
    """The pair in use now."""
    d = ACTIVE["dir"]
    if d and _pair_of(Path(d)):
        return {**_pair_of(Path(d)), "dir": d, "source": "side"}
    return venv_pair()


def hot_swap(d: Path | None) -> bool:
    """Use another pair from now on, without a restart, when nothing is using yt-dlp (no YouTube job, no open YoutubeDL);
    else nothing changes now and the next start activates the saved pair (never half old, half new modules).
    Returns True when it took effect now."""
    from . import jobs, youtube
    with youtube.USE_LOCK:
        if youtube.IN_USE[0] or any(not j.finished and j.kind in YOUTUBE_JOBS for j in list(jobs.JOBS.values())):
            return False
        _use(d)
        for m in [m for m in sys.modules if m == "yt_dlp" or m.startswith(("yt_dlp.", "yt_dlp_ejs"))]:
            del sys.modules[m]
        youtube.WATCHED.clear()
        return True


YOUTUBE_JOBS = ("auto", "train-prepare", "train-auto", "night")   # the jobs that fetch from YouTube


# ---------------------------------------------------------------- detection

def classify(err: str) -> str | None:
    """"broken" when the error looks like yt-dlp falling behind YouTube, else None."""
    if any(s.lower() in err.lower() for s in NOT_BROKEN):
        return None
    return "broken" if any(s.lower() in err.lower() for s in BROKEN) else None


def note(video: str, ok: bool, err: str = ""):
    """One YouTube video resolved (ok) or failed. Enough of the same breakage starts a heal (auto) or flags it."""
    kind = "ok" if ok else classify(err)
    if kind is None or not video:
        return
    now = time.time()
    with LOCK:
        _EVENTS.append((now, video, kind))
        _EVENTS[:] = [e for e in _EVENTS if now - e[0] < WINDOW][-200:]
        b = breakage()
    if b and not ok:
        _on_breakage(b)


def breakage() -> dict | None:
    """{"videos": n, "ok": n, "since": t} when several videos broke the same way lately, else None."""
    now = time.time()
    recent = [e for e in _EVENTS if now - e[0] < WINDOW]
    broken = {v for _, v, k in recent if k == "broken"}
    ok = sum(k == "ok" for _, _, k in recent)
    if len(broken) >= BROKEN_VIDEOS and len(broken) > ok:
        return {"videos": len(broken), "ok": ok, "since": min(t for t, _, k in recent if k == "broken")}
    return None


FLAG: dict = {"broken": None}   # the last breakage nobody healed yet (the Doctor offers the Fix)


def _on_breakage(b: dict):
    st = load()
    FLAG["broken"] = b
    if not st["auto"]:
        return
    if time.time() < float(st.get("held_until") or 0):
        return
    j = _HEAL["job"]
    if j is not None and not j.finished:
        return
    log(f"{b['videos']} videos failed the same way in {WINDOW // 60} min: looking for a newer yt-dlp")
    start_heal(reason="breakage")


# ---------------------------------------------------------------- PyPI

def fetch_json(name: str, version: str | None = None) -> dict:
    url = f"https://pypi.org/pypi/{name}/{version + '/' if version else ''}json"
    with urllib.request.urlopen(url, timeout=20) as r:
        return json.loads(r.read())


def _wheel(meta: dict) -> dict:
    for f in meta.get("urls") or []:
        if f.get("packagetype") == "bdist_wheel" and f["filename"].endswith("-py3-none-any.whl") and not f.get("yanked"):
            return {"url": f["url"], "sha": f["digests"]["sha256"], "file": f["filename"], "size": f.get("size") or 0}
    raise HealError(f"PyPI lists no plain wheel for {meta.get('info', {}).get('name')} {meta.get('info', {}).get('version')}.")


def ejs_pin(requires: list) -> str | None:
    """The yt-dlp-ejs version a yt-dlp release pins (its `pin` extra; `default` as a second choice)."""
    for extra in ("pin", "default"):
        for r in requires or []:
            m = re.match(r"yt-dlp-ejs\s*==\s*([\w.]+)\s*;.*extra\s*==\s*[\"']" + extra + r"[\"']", r)
            if m:
                return m.group(1)
    return None


def latest_pair() -> dict:
    """PyPI's newest yt-dlp and the yt-dlp-ejs it pins, each with its wheel's url and SHA-256."""
    yt = fetch_json(PKG)
    ver = yt["info"]["version"]
    ejs = ejs_pin(yt["info"].get("requires_dist"))
    if not ejs:
        raise HealError(f"yt-dlp {ver} doesn't name a yt-dlp-ejs version; not taking it.")
    return {"yt": ver, "ejs": ejs, "wheels": {PKG: _wheel(yt), EJS: _wheel(fetch_json(EJS, ejs))}}


def pinned_pair() -> dict:
    """The pair pinned in installer/requirements/common.txt (what the venv has): the installer's fallback."""
    text = (ROOT / "installer" / "requirements" / "common.txt").read_text(encoding="utf-8")
    out = {}
    for name in (PKG, EJS):
        m = re.search(rf"^{re.escape(name)}==([\w.]+)\s*\\\s*\n\s*--hash=sha256:([0-9a-f]{{64}})", text, re.M)
        if m:
            out[name] = (m.group(1), m.group(2))
    return out


def download(w: dict, dest: Path) -> Path:
    """One wheel, refused unless its SHA-256 is the one PyPI publishes."""
    f = dest / w["file"]
    h = hashlib.sha256()
    n = 0
    with urllib.request.urlopen(w["url"], timeout=60) as r, open(f, "wb") as out:
        while True:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            n += len(chunk)
            if n > MAX_WHEEL:
                raise HealError(f"{w['file']} is larger than any yt-dlp wheel should be.")
            h.update(chunk)
            out.write(chunk)
    if h.hexdigest() != w["sha"]:
        raise HealError(f"{w['file']}'s checksum isn't the one PyPI publishes: not using it.")
    return f


def unpack(wheel: Path, dest: Path):
    """A pure-Python wheel into dest (its .data part skipped; no paths that climb out)."""
    with zipfile.ZipFile(wheel) as z:
        for i in z.infolist():
            n = i.filename
            if n.endswith("/") or ".data/" in n.split("/", 1)[0] + "/":
                continue
            if n.startswith("/") or "\\" in n or ":" in n or any(p in ("", "..") for p in n.split("/")):
                raise HealError(f"{wheel.name} holds a path that climbs out: {n!r}")
            f = dest / n
            f.parent.mkdir(parents=True, exist_ok=True)
            with z.open(i) as src, open(f, "wb") as out:
                shutil.copyfileobj(src, out)


def stage(pair: dict, into: Path | None = None) -> Path:
    """Download, check and unpack a pair into a staging folder (inside home() unless `into`)."""
    root = into or home()
    _guard(root)
    root.mkdir(parents=True, exist_ok=True)
    d = root / f".stage-{secrets.token_hex(4)}"
    d.mkdir()
    dl = d / ".wheels"
    dl.mkdir()
    try:
        for name in (PKG, EJS):
            unpack(download(pair["wheels"][name], dl), d)
    except BaseException:   # a refused hash or a broken wheel: nothing of it stays
        if into is None:
            _drop(d)
        else:
            shutil.rmtree(d, ignore_errors=True)
        raise
    shutil.rmtree(dl, ignore_errors=True)
    (d / "pair.json").write_text(json.dumps({"yt": pair["yt"], "ejs": pair["ejs"], "made": time.strftime("%Y-%m-%d %H:%M"),
                                             "sha": {k: w["sha"] for k, w in pair["wheels"].items()}}), encoding="utf-8")
    return d


# ---------------------------------------------------------------- self-test

SELFTEST = r'''
import json, sys
folder, ids = sys.argv[1], sys.argv[2:]
if folder:
    sys.path.insert(0, folder)
import yt_dlp
out = {"version": yt_dlp.version.__version__, "file": yt_dlp.__file__, "ok": 0, "errors": []}
try:
    import yt_dlp_ejs
    out["ejs"] = getattr(yt_dlp_ejs, "__file__", "")
except ImportError:
    out["ejs"] = None
opts = {"quiet": True, "no_warnings": True, "noprogress": True, "skip_download": True, "format": "bestaudio/best"}
for vid in ids:
    try:
        with yt_dlp.YoutubeDL(opts) as y:
            info = y.extract_info("https://www.youtube.com/watch?v=" + vid, download=False)
        fmts = info.get("requested_formats") or [info]
        if any(f.get("url") for f in fmts):
            out["ok"] += 1
        else:
            out["errors"].append(vid + ": no playable format")
    except Exception as e:
        out["errors"].append(vid + ": " + str(e).splitlines()[0][:200])
print("SELFTEST " + json.dumps(out))
'''


def selftest(d: Path | None) -> dict:
    """Resolve KNOWN_GOOD with a pair in a separate Python (d None = the venv's pair). -> {"ok": n, "of", "errors", "version"}."""
    try:
        r = subprocess.run([sys.executable, "-I", "-c", SELFTEST, str(d) if d else "", *KNOWN_GOOD], capture_output=True,
                           text=True, errors="replace", timeout=SELFTEST_TIMEOUT,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        line = next((x for x in r.stdout.splitlines() if x.startswith("SELFTEST ")), None)
        if line is None:
            return {"ok": 0, "of": len(KNOWN_GOOD), "errors": [(r.stderr or "no answer").strip().splitlines()[-1][:200]]}
        res = json.loads(line[9:])
    except subprocess.TimeoutExpired:
        return {"ok": 0, "of": len(KNOWN_GOOD), "errors": ["the test took too long"]}
    return {**res, "of": len(KNOWN_GOOD)}


RESOLVE = selftest   # tests put a fake resolver here


# ---------------------------------------------------------------- heal

def _name(p: dict) -> str:
    return f"{p['yt']}+ejs{p['ejs']}"


def heal(job=None, reason: str = "", dry_run: bool = False, offline: bool = False) -> dict:
    """Find the best pair and use it: PyPI's newest (staged, checked, tested), the current one, the last known good, the
    venv's. A new pair must resolve at least as many known videos as the current one (and one at least). dry_run: test
    everything in a temporary folder and change nothing."""
    import tempfile

    def say(msg, frac=None):
        log(msg)
        if job is not None:
            job.push(stage="checking", message=msg, **({"frac": frac} if frac is not None else {}))
    with LOCK:
        st = load()
        cur = current()
        say(f"checking {cur['source']} pair yt-dlp {cur['yt']} + solver {cur.get('ejs') or 'missing'}"
            + (f" ({reason})" if reason else ""), 0.05)
        score = {"current": RESOLVE(Path(cur["dir"]) if cur["dir"] else None)}
        say(f"current pair resolves {score['current']['ok']} of {score['current']['of']} known videos", 0.3)
        tmp = tempfile.mkdtemp(prefix="ytdlp-dry-", dir=path("cache_dir")) if dry_run else None   # removed below
        new_dir, new, note_ = None, None, ""
        try:
            if not offline:
                try:
                    new = latest_pair()
                except (OSError, ValueError, KeyError, HealError) as e:
                    note_ = f"PyPI couldn't be reached or answered oddly ({type(e).__name__}): kept what works."
                    say(note_)
                    new = None
            if new and vkey(new["yt"]) <= vkey(cur["yt"]) and (cur.get("ejs") or "") == new["ejs"]:
                say(f"PyPI's newest is {new['yt']} + solver {new['ejs']}: nothing newer", 0.5)
                new = None
            if new:
                say(f"PyPI has yt-dlp {new['yt']} + solver {new['ejs']}: downloading and checking", 0.45)
                try:
                    new_dir = stage(new, Path(tmp) if dry_run else None)
                except (OSError, HealError) as e:
                    note_ = f"yt-dlp {new['yt']} couldn't be fetched safely ({e}): kept what works."
                    say(note_)
                    new_dir = None
                if new_dir is not None:
                    score["new"] = RESOLVE(new_dir)
                    say(f"yt-dlp {new['yt']} resolves {score['new']['ok']} of {score['new']['of']}", 0.75)
            options = []   # (score, preference, label, folder or None, pair)
            if new_dir is not None:
                options.append((score["new"]["ok"], 3, "new", new_dir, {"yt": new["yt"], "ejs": new["ejs"]}))
            options.append((score["current"]["ok"], 2, "current", Path(cur["dir"]) if cur["dir"] else None, cur))
            if score["current"]["ok"] < score["current"]["of"]:   # the current one falls short: try going back
                lg = side(st["last_good"])
                if lg is not None and str(lg) != cur["dir"]:
                    score["last_good"] = RESOLVE(lg)
                    options.append((score["last_good"]["ok"], 1, "last_good", lg, _pair_of(lg)))
                if cur["dir"]:
                    score["venv"] = RESOLVE(None)
                    options.append((score["venv"]["ok"], 0, "venv", None, venv_pair()))
            best = max(options, key=lambda o: (o[0], o[1]))
            if best[0] == 0:
                best = next(o for o in options if o[2] == "current")
            out = {"before": {"yt": cur["yt"], "ejs": cur.get("ejs"), "source": cur["source"]},
                   "scores": {k: {"ok": v["ok"], "of": v["of"], "errors": v.get("errors", [])[:3]} for k, v in score.items()},
                   "chose": best[2], "dry_run": dry_run, "note": note_}
            if best[2] == "current" or dry_run:
                out.update(after=out["before"], changed=False)
                verdict = ("would use " + best[2]) if dry_run and best[2] != "current" else "kept the current pair"
                say(f"{verdict} ({best[0]} of {len(KNOWN_GOOD)})", 1.0)
                if not dry_run:
                    st["held_until"] = time.time() + HOLD if breakage() else 0
                    st["last_check"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                    st["last_result"] = verdict
                    save(st)
                return out
            # use it
            prev = st["active"]
            if best[2] == "new":
                target = home() / _name(best[4])
                if target.exists():
                    _drop(target)
                fsutil.retry(lambda: os.replace(new_dir, target))
                new_dir = None
                use = target
            else:
                use = best[3]
            st["active"] = use.name if use is not None else None
            if best[2] == "new":
                st["last_good"] = prev   # what worked before stays as the way back (None = the venv's pair)
            st["swapped"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            st["last_check"], st["held_until"] = st["swapped"], 0
            label = {"new": "updated to", "last_good": "went back to the last known good", "venv": "went back to the pinned"}[best[2]]
            st["last_result"] = f"{label} yt-dlp {best[4]['yt']} + solver {best[4].get('ejs')}"
            save(st)
            now_ = hot_swap(use)
            FLAG["broken"] = None
            _EVENTS.clear()
            say(st["last_result"] + ("" if now_ else " (in use after a restart: YouTube work is running)"), 1.0)
            prune(st)
            out.update(after={"yt": best[4]["yt"], "ejs": best[4].get("ejs"), "source": "side" if use else "venv"},
                       changed=True, now=now_)
            return out
        finally:
            if new_dir is not None and Path(new_dir).exists():
                if dry_run:
                    shutil.rmtree(tmp, ignore_errors=True)
                else:
                    _drop(Path(new_dir))
            if tmp and Path(tmp).exists():
                shutil.rmtree(tmp, ignore_errors=True)


def _drop(d: Path):
    storage.remove_files([], [home()], dirs=[d])


def prune(st: dict | None = None):
    """Keep the active and the last known good pair; remove other pair folders and leftover staging."""
    st = st or load()
    keep = {st["active"], st["last_good"]}
    for d in home().iterdir() if home().exists() else []:
        if d.is_dir() and d.name not in keep:
            try:
                _drop(d)
            except (OSError, ModelError) as e:
                log(f"couldn't remove {d.name}: {e}")


def set_auto(on: bool) -> dict:
    st = load()
    st["auto"] = bool(on)
    save(st)
    log(f"auto-update {'on' if on else 'off'}")
    return status()


def status() -> dict:
    st, cur = load(), current()
    return {"yt": cur["yt"], "ejs": cur.get("ejs"), "source": cur["source"], "auto": st["auto"],
            "last_good": st["last_good"], "last_check": st.get("last_check"), "last_result": st.get("last_result"),
            "swapped": st.get("swapped"), "broken": FLAG["broken"],
            "job": _HEAL["job"].id if _HEAL["job"] is not None and not _HEAL["job"].finished else None,
            "pinned": venv_pair()}


def start_heal(reason: str = "", dry_run: bool = False):
    """A heal as a job (kind "ytdlp"); one at a time."""
    from . import jobs
    with LOCK:
        j = _HEAL["job"]
        if j is not None and not j.finished:
            return j
        job = jobs.Job("ytdlp", title="YouTube downloader: looking for a fix" if reason else "YouTube downloader: checking for a newer one")
        jobs.JOBS[job.id] = job
        _HEAL["job"] = job

    def go():
        with job.cond:
            job.state = "running"
        try:
            r = heal(job, reason=reason, dry_run=dry_run)
            msg = ("Now using " + f"yt-dlp {r['after']['yt']}" if r["changed"] else
                   f"Kept yt-dlp {r['before']['yt']}: " + (r["note"] or "nothing newer works better."))
            job.finish("done", message=msg, result=r)
        except Exception as e:   # noqa: BLE001
            log(f"heal failed: {e}")
            job.finish("error", message=jobs.friendly(e, "updating the YouTube downloader"))

    threading.Thread(target=go, daemon=True, name="ytdlp-heal").start()
    return job
