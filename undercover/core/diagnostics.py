"""The diagnostics file: one zip of logs + the install record + a Doctor report, for asking someone for help (phase 10).

No personal audio (only text files go in) and the user's folder paths shortened: the app folder reads <UnderCover>, each
data folder <library>, <working files>, <model files>, <voices>, and the user's home %USERPROFILE%. The System room
shows `contents()` (what goes in, how big, and the shortenings) before anything is saved.

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

from .config import CONFIG_FILE, ROOT, path

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
    if CONFIG_FILE.exists():
        items.append(("config.toml", lambda: CONFIG_FILE.read_text(encoding="utf-8"), "UnderCover's settings"))
    items.append(("doctor-report.json", lambda: json.dumps(report or {}, indent=1), "The Doctor's checks, run just now"))
    full = logs / "doctor-report.json"
    if full.exists():
        items.append(("doctor-full-report.json", lambda: full.read_text(encoding="utf-8"), "The last full check"))
    items.append(("about.json", lambda: json.dumps(about.about(), indent=1), "Version, Python, PyTorch, Windows"))
    return items


def contents(report: dict | None = None) -> dict:
    """What would go in: [{name, size, about}], the shortenings shown to the user, and what never goes in."""
    pairs = _shorteners()
    files = []
    for name, make, what in _items(report):
        try:
            files.append({"name": name, "size": len(shorten(make(), pairs).encode("utf-8")), "about": what})
        except OSError:
            pass
    return {"files": files, "size": sum(f["size"] for f in files),
            "shortened": [{"folder": real, "reads": short} for real, short in pairs],
            "never": "No songs, covers, speech, voices or other audio; no training clips; folder names shortened.",
            "name": f"UnderCover-diagnostics-{time.strftime('%Y%m%d-%H%M')}.zip"}


def build(report: dict | None = None) -> bytes:
    """The zip, in memory (a few MB at most)."""
    pairs = _shorteners()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        listing = []
        for name, make, what in _items(report):
            try:
                text = shorten(make(), pairs)
            except OSError:
                continue
            z.writestr(name, text)
            listing.append(f"{name}: {what}")
        z.writestr("README.txt", "UnderCover diagnostics, made " + time.strftime("%Y-%m-%d %H:%M") + ".\n\n"
                   + "\n".join(listing) + "\n\nNo audio is included. Folder names are shortened: "
                   + ", ".join(short for _, short in pairs) + ".\n")
    return buf.getvalue()
