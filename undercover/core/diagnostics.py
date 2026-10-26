"""The diagnostics file: one zip of logs + the install record + a Doctor report, for asking someone for help (phase 10).

No personal audio (only text files go in) and the user's folder paths shortened: the app folder reads <UnderCover>, each
data folder <library>, <working files>, <model files>, <voices>, and the user's home %USERPROFILE%. The home-network
address is masked (192.168.x.y), and "Leave names out" (names=False) replaces the names of the user's voices, covers,
speech and training sources too. The System room shows `contents()` (what goes in, how big, the shortenings and what
the file still holds) before anything is saved.

    GET /api/doctor/bundle       what would go in
    GET /api/doctor/bundle.zip   the file itself (the browser saves it)
    python -m undercover.doctor bundle <file.zip>
"""
import io
import json
import os
import re
import time
import zipfile
from pathlib import Path

from . import config
from .config import ROOT, path

TAIL = 2_000_000          # at most the last 2 MB of each log
LOGS = [("server.log", "What UnderCover printed while it ran"), ("errors.log", "Errors, with the code's own details"),
        ("doctor.log", "What the Doctor checked and fixed"), ("cadence-worker.log", "The cadence engine's own log"),
        ("setup.log", "The installer's log"), ("selftest.json", "The installer's self-test"),
        ("heartbeat.jsonl", "A line every few minutes: what was running, free space and memory"),
        ("timings.jsonl", "How long each step took")]


def _shorteners() -> list:
    """[(real path, short name)], longest first, so the most specific folder wins."""
    pairs = [(path("uploads_dir"), "<library>\\uploads"), (path("output_dir"), "<library>\\output"),
             (path("voices_dir"), "<voices>"), (path("cache_dir"), "<working files>"), (path("models_dir"), "<model files>"),
             (path("logs_dir"), "<logs>"), (ROOT, "<UnderCover>"), (Path.home(), "%USERPROFILE%")]
    out = []
    for p, short in pairs:
        s = str(p)
        if s not in [x for x, _ in out]:
            out.append((s, short))
    return sorted(out, key=lambda x: -len(x[0]))


def shorten(text: str, pairs: list | None = None) -> str:
    """Replace every spelling of the user's folders (\\, \\\\ as in JSON, /) with their short names, ignoring case."""
    for real, short in pairs or _shorteners():
        for variant in {real, real.replace("\\", "\\\\"), real.replace("\\", "/")}:
            text = re.sub(re.escape(variant), lambda _: short.replace("\\", "\\\\") if "\\\\" in variant else short,
                          text, flags=re.IGNORECASE)
    user = os.environ.get("USERNAME") or os.environ.get("USER")
    if user and len(user) > 2:   # the account name anywhere else (another drive's Users folder, a log line)
        text = re.sub(rf"(?i)(\\|/)Users(\\|/){re.escape(user)}\b", lambda m: f"{m.group(1)}Users{m.group(2)}%USERNAME%", text)
    return text


def _tail(p: Path) -> str:
    with open(p, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - TAIL))
        data = f.read().decode("utf-8", "replace")
    if size > TAIL:
        data = f"[the first {(size - TAIL) / 1e6:.1f} MB left out]\n" + data[data.find("\n") + 1:]
    return data


# ---------------------------------------------------------------- privacy (1.1)
# The home-network address is masked by default (192.168.x.y); "Leave names out" also replaces the names of the user's
# things (voices, covers, speech, training projects and their sources, the away-from-home node) with placeholders.
LAN = re.compile(r"\b(10|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.(\d{1,3})(?:\.(\d{1,3}))?(?:\.(\d{1,3}))?\b")
TS_NODE = re.compile(r"\b[a-z0-9-]+\.[a-z0-9-]+\.ts\.net\b", re.I)


def mask_lan(text: str) -> str:
    """Private IPv4 addresses with their last two parts hidden: 192.168.1.20 -> 192.168.x.y, 10.0.0.5 -> 10.0.x.y."""
    def one(m):
        head = m.group(1)
        if head == "10":
            return f"10.{m.group(2)}.x.y" if m.group(4) else m.group(0)
        return f"{head}.x.y" if m.group(3) else m.group(0)
    return LAN.sub(one, text)


def _json_lines(f: Path) -> list:
    try:
        return [json.loads(ln) for ln in f.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except (OSError, ValueError):
        return []


def names() -> list[str]:
    """The names of the user's things, longest first: what "Leave names out" replaces (read from the app's own files)."""
    from . import voices
    found = set()
    try:
        for v in voices.listed_voices():
            found |= {v.name, v.display or "", str((v.extra or {}).get("person") or "")}
    except Exception:   # noqa: BLE001 - a voice folder that won't read: the rest still count
        pass
    out = path("output_dir")
    for h in _json_lines(out / "history.jsonl"):
        for k in ("output", "title", "song_title", "raw_title"):
            v = str(h.get(k) or "")
            if v:
                stem = Path(v).stem if k == "output" else v
                found |= {stem, stem.split(" (", 1)[0]}
    for meta in (out / "speech").glob("*.json") if (out / "speech").is_dir() else []:
        try:
            m = json.loads(meta.read_text(encoding="utf-8"))
            found |= {str(m.get("title") or ""), str(m.get("voice") or "")}
        except (OSError, ValueError):
            pass
    tr = path("cache_dir") / "training"
    for pj in tr.glob("*/project.json") if tr.is_dir() else []:
        try:
            p = json.loads(pj.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        found |= {pj.parent.name, str(p.get("name") or ""), str(p.get("person") or "")}
        found |= {str(s.get("title") or "") for s in p.get("sources") or [] if isinstance(s, dict)}
    return sorted({n.strip() for n in found if len(n.strip()) >= 3}, key=len, reverse=True)


def hide_names(text: str, known: list[str]) -> str:
    if known:   # one pass, longest name first (a title that holds a voice's name goes whole)
        rx = re.compile("|".join(re.escape(n) for n in sorted(known, key=len, reverse=True)), re.IGNORECASE)
        text = rx.sub("<name>", text)
    return TS_NODE.sub("<node>.ts.net", text)


def _clean(text: str, pairs: list, known: list | None) -> str:
    text = mask_lan(shorten(text, pairs))
    return hide_names(text, known) if known is not None else text


def _items(report: dict | None) -> list:
    """[(name in the zip, text producer, what it is)]."""
    from . import about, manifest
    logs = path("logs_dir")
    items = []
    for name, what in LOGS:
        p = logs / name
        if p.exists() and p.is_file():
            items.append((f"logs/{name}", lambda p=p: _tail(p), what))
    if manifest.file().exists():
        items.append(("manifest.json", lambda: manifest.file().read_text(encoding="utf-8"), "The install record"))
    if config.CONFIG_FILE.exists():   # read when asked: a test's isolated config, not whatever was loaded at import
        items.append(("config.toml", lambda: config.CONFIG_FILE.read_text(encoding="utf-8"), "UnderCover's settings"))
    items.append(("doctor-report.json", lambda: json.dumps(report or {}, indent=1), "The Doctor's checks, run just now"))
    full = logs / "doctor-report.json"
    if full.exists():
        items.append(("doctor-full-report.json", lambda: full.read_text(encoding="utf-8"), "The last full check"))
    items.append(("about.json", lambda: json.dumps(about.about(), indent=1), "Version, Python, PyTorch, Windows"))
    return items


def kept(with_names: bool = True) -> list[str]:
    """What the file still holds that's the user's own, said plainly before it's saved (1.1)."""
    g = config.CONFIG.get("game_pause") or {}
    v = (config.CONFIG.get("vault") or {}).get("folder")
    out = (["the names of your voices, covers, speech and training sources (in the logs and the settings)"]
           if with_names else ["no names: your voices, covers, speech and training sources read <name>"])
    out.append("this PC's home-network address, masked (like 192.168.x.y), and its network adapter's name")
    if with_names:
        out.append("the away-from-home address's node name, if you set it up")
    if v:
        out.append("where your vault is kept")
    if g.get("never_ask"):
        out.append("the games you told the game pause not to ask about")
    return out


def contents(report: dict | None = None, with_names: bool = True) -> dict:
    """What would go in: [{name, size, about}], the shortenings shown to the user, what it still holds, what never goes in."""
    pairs, known = _shorteners(), (None if with_names else names())
    files = []
    for name, make, what in _items(report):
        try:
            files.append({"name": name, "size": len(_clean(make(), pairs, known).encode("utf-8")), "about": what})
        except OSError:
            pass
    return {"files": files, "size": sum(f["size"] for f in files),
            "shortened": [{"folder": real, "reads": short} for real, short in pairs],
            "kept": kept(with_names), "names": with_names,
            "never": "No songs, covers, speech, voices or other audio; no training clips; folder names shortened.",
            "name": f"UnderCover-diagnostics-{time.strftime('%Y%m%d-%H%M')}.zip"}


def build(report: dict | None = None, with_names: bool = True) -> bytes:
    """The zip, in memory (a few MB at most)."""
    pairs, known = _shorteners(), (None if with_names else names())
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        listing = []
        for name, make, what in _items(report):
            try:
                text = _clean(make(), pairs, known)
            except OSError:
                continue
            z.writestr(name, text)
            listing.append(f"{name}: {what}")
        z.writestr("README.txt", "UnderCover diagnostics, made " + time.strftime("%Y-%m-%d %H:%M") + ".\n\n"
                   + "\n".join(listing) + "\n\nNo audio is included. Folder names are shortened: "
                   + ", ".join(short for _, short in pairs) + ". The home-network address is masked."
                   + ("" if with_names else " Names are left out.") + "\n")
    return buf.getvalue()
