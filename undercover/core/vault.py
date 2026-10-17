"""Vaulting: a quiet, incremental backup of what can't be downloaded again, on another drive (docs/research/vaulting-plan.md;
the user's answers 2026-10-12: "What matters" by default, voices trained here only, after new keepers plus daily while
the GPU queue is idle, no encryption, compression instead).

The vault is plain files, so it works on any drive (NTFS, exFAT, a NAS share) and on any OS:

    <folder>/UnderCover Vault/
      objects/ab/<sha256>.<codec>   each file once, named by the SHA-256 of the ORIGINAL bytes (de-duplicated)
      snapshots/<stamp>.json        {key: [sha256, size, mtime_ns]} for one run: a complete picture of that moment
      vault.json                    format, the last run, the index that makes runs incremental

Compression is lossless and checked: every object is decoded again right after it's written and must re-hash to the
original's SHA-256, or it's stored raw instead. A restore re-hashes too, so what comes back is bit-identical.
  - WAV (16-bit PCM): FLAC of the samples + the WAV's own header and trailer bytes (rebuilt exactly);
  - already-compressed files (mp3, flac, zip-based model files, images): stored as they are;
  - everything else: lzma or zlib, per type, from measured ratio and speed (CODEC below).

It never deletes from the library; pruning only removes vault objects no kept snapshot names (storage.remove_files,
within the vault's objects folder). Restore writes only into the library's own folders and never overwrites without
being told how (keep both / replace / skip).
"""
import hashlib
import io
import json
import lzma
import os
import shutil
import struct
import threading
import time
import zlib
from datetime import datetime, timedelta
from pathlib import Path

from . import fsutil, storage
from . import config
from .config import CONFIG, path
from .voices import ModelError

FORMAT = 1
NAME = "UnderCover Vault"
STORED = {".mp3", ".flac", ".ogg", ".opus", ".m4a", ".aac", ".zip", ".npz", ".png", ".jpg", ".jpeg", ".webp", ".gz",
          ".xz", ".7z"}
CODEC = {}             # extension -> "lzma" / "zlib" (measured: set from the trial); anything else not STORED: "lzma"
LZMA_PRESET = 6
SECRETS = {"phone.json"}   # device tokens: never in the vault (after a restore, phones pair again)
CHUNK = 1 << 20
FLAC_MAX = 64 << 20     # bigger WAVs (raw sources in "Everything") stream through lzma instead of FLAC in memory
_RUN_LOCK = threading.Lock()   # one run at a time: a run's prune must never see another run's fresh, unnamed objects


# ---------------------------------------------------------------- what goes in

def items(size: str = "matters") -> list[tuple[str, str, Path]]:
    """[(kind, key, file)] of what this install would vault. key = the file's place in the vault, independent of where
    the library lives (so a restore lands in today's folders): voices/<name>/..., songs/..., covers/..., training/...,
    settings/... . "matters": voices trained here (not downloaded ones, not the demo), songs, covers and speech, each
    training project's choices (not its clips or saves), settings. "everything" adds the projects' clips and sources."""
    out = []

    def add(kind, root: Path, key: str, f: Path):
        if f.is_file() and f.name not in SECRETS and not f.name.endswith((".part", ".tmp")):
            out.append((kind, f"{key}/{f.relative_to(root).as_posix()}", f))

    voices = path("voices_dir")
    if voices.is_dir():
        for v in sorted(voices.iterdir()):
            meta = v / "meta.json"
            try:
                extra = json.loads(meta.read_text(encoding="utf-8")).get("extra") or {}
            except (OSError, ValueError):
                continue
            if extra.get("trained") and not extra.get("demo"):
                for f in sorted(v.rglob("*")):
                    add("voice", voices, "voices", f)
    for kind, key, root in (("song", "songs", path("uploads_dir")), ("cover", "covers", path("output_dir"))):
        if root.is_dir():
            for f in sorted(root.rglob("*")):
                add("speech" if kind == "cover" and "speech" in f.relative_to(root).parts[:1] else kind, root, key, f)
    tr = path("cache_dir") / "training"
    if tr.is_dir():
        for p in sorted(d for d in tr.iterdir() if d.is_dir()):
            keep = ["project.json", "transcripts.json", "embeddings.npz", "cadence"]
            if size == "everything":
                keep += ["clips", "raw"]
            for part in keep:
                q = p / part
                for f in ([q] if q.is_file() else sorted(q.rglob("*")) if q.is_dir() else []):
                    add("project", tr, "training", f)
    if config.CONFIG_FILE.is_file():   # whatever the file is called here, it's "the config" in the vault
        out.append(("settings", "settings/config.toml", config.CONFIG_FILE))
    add("settings", config.CONFIG_FILE.parent, "settings", config.CONFIG_FILE.parent / "tutorial.json")
    add("settings", path("cache_dir"), "settings", path("cache_dir") / "voice_quality.json")
    for root, key in ((path("cache_dir") / "cadence", "settings/cadence"), (path("themes_dir"), "settings/themes")):
        if root.is_dir():
            for f in sorted(root.rglob("*")):
                add("settings", root, key, f)
    return out


def target_of(key: str) -> Path:
    """Where a vault key lives in this install (a restore's destination). A key that steps out of its folder (".."), or
    names a drive or an absolute path, is refused: a vault on a shared drive is data, never a way to write elsewhere."""
    parts = key.split("/")
    if any(x in ("", ".", "..") or ":" in x or "\\" in x for x in parts) or len(parts) < 2:
        raise ModelError(f"The vault holds a name UnderCover won't restore ({key[:80]}).")
    top, _, rest = key.partition("/")
    roots = {"voices": path("voices_dir"), "songs": path("uploads_dir"), "covers": path("output_dir"),
             "training": path("cache_dir") / "training"}
    if top in roots:
        return roots[top] / rest
    if top == "settings":
        sub, _, more = rest.partition("/")
        if sub == "cadence" and more:
            return path("cache_dir") / "cadence" / more
        if sub == "themes" and more:
            return path("themes_dir") / more
        if rest == "voice_quality.json":
            return path("cache_dir") / rest
        if rest == "config.toml":
            return config.CONFIG_FILE
        if rest == "tutorial.json":
            return config.CONFIG_FILE.parent / rest
    raise ModelError(f"The vault holds something this version doesn't know where to put ({key}).")


# ---------------------------------------------------------------- objects: encode, decode, verify

def sha256_file(f: Path) -> str:
    h = hashlib.sha256()
    with open(f, "rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _wav_parts(b: bytes):
    """(header, PCM, trailer, channels, rate) of a 16-bit PCM WAV, or None for anything else."""
    if len(b) < 44 or b[:4] != b"RIFF" or b[8:12] != b"WAVE":
        return None
    i, fmt = 12, None
    while i + 8 <= len(b):
        cid, n = b[i:i + 4], struct.unpack("<I", b[i + 4:i + 8])[0]
        if cid == b"fmt " and n >= 16:
            tag, ch, rate, _, _, bits = struct.unpack("<HHIIHH", b[i + 8:i + 24])
            fmt = (tag, ch, rate, bits)
        elif cid == b"data":
            if not fmt or fmt[0] != 1 or fmt[3] != 16 or not fmt[1]:
                return None
            s, e = i + 8, min(i + 8 + n, len(b))
            usable = (e - s) - (e - s) % (2 * fmt[1])
            return b[:s], b[s:s + usable], b[s + usable:], fmt[1], fmt[2]
        i += 8 + n + (n & 1)
    return None


def _flac_pack(b: bytes) -> bytes | None:
    """A WAV as FLAC + its own header / trailer bytes (our container: b"UCVF" + 2 lengths + the parts)."""
    import numpy as np
    import soundfile as sf
    w = _wav_parts(b)
    if w is None or not w[1]:
        return None
    head, pcm, tail, ch, rate = w
    buf = io.BytesIO()
    sf.write(buf, np.frombuffer(pcm, dtype="<i2").reshape(-1, ch), rate, format="FLAC", subtype="PCM_16")
    return b"UCVF" + struct.pack("<II", len(head), len(tail)) + head + tail + buf.getvalue()


def _flac_unpack(b: bytes) -> bytes:
    import soundfile as sf
    if b[:4] != b"UCVF":
        raise ValueError("not a vault FLAC object")
    nh, nt = struct.unpack("<II", b[4:12])
    head, tail = b[12:12 + nh], b[12 + nh:12 + nh + nt]
    y, _ = sf.read(io.BytesIO(b[12 + nh + nt:]), dtype="int16", always_2d=True)
    return head + y.astype("<i2").tobytes() + tail


def codec_for(f: Path) -> str:
    ext = f.suffix.lower()
    if ext in STORED:
        return "raw"
    if ext == ".wav":
        return "flac"
    return CODEC.get(ext, "lzma")


def _encode(data: bytes, codec: str) -> bytes | None:
    if codec == "flac":
        return _flac_pack(data)
    if codec == "lzma":
        return lzma.compress(data, preset=LZMA_PRESET)
    if codec == "zlib":
        return zlib.compress(data, 6)
    return data


def decode(blob: bytes, codec: str) -> bytes:
    if codec == "flac":
        return _flac_unpack(blob)
    if codec == "lzma":
        return lzma.decompress(blob)
    if codec == "zlib":
        return zlib.decompress(blob)
    return blob


def _object(objects: Path, sha: str, codec: str) -> Path:
    return objects / sha[:2] / f"{sha}.{codec}"


def find_object(objects: Path, sha: str) -> Path | None:
    d = objects / sha[:2]
    hits = sorted(d.glob(f"{sha}.*")) if d.is_dir() else []
    hits = [h for h in hits if not h.name.endswith(".part")]
    return hits[0] if hits else None


def _write_part(dest: Path, chunks) -> int:
    """Write chunks to <dest>.part, fsync, rename into place. -> bytes written"""
    fsutil.guard(dest)   # test mode: never outside the test's folders
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    n = 0
    try:
        with open(part, "wb") as fh:
            for c in chunks:
                fh.write(c)
                n += len(c)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(part, dest)
    finally:
        part.unlink(missing_ok=True)
    return n


def _chunks(f: Path):
    with open(f, "rb") as fh:
        for c in iter(lambda: fh.read(CHUNK), b""):
            yield c


def _decoded(obj: Path):
    """An object's original bytes, in chunks (lzma and raw stream; FLAC objects are small and decode at once)."""
    codec = obj.suffix.lstrip(".")
    if codec == "lzma":
        d = lzma.LZMADecompressor()
        for c in _chunks(obj):
            yield d.decompress(c)
    elif codec == "raw":
        yield from _chunks(obj)
    else:
        yield decode(obj.read_bytes(), codec)


def _put_big(objects: Path, f: Path, sha: str) -> tuple[Path, int]:
    """A big file streamed through lzma (a few MB of memory), checked by decoding it back; raw if that's no smaller."""
    dest = _object(objects, sha, "lzma")
    comp, h = lzma.LZMACompressor(preset=LZMA_PRESET), hashlib.sha256()

    def gen():
        for c in _chunks(f):
            h.update(c)
            yield comp.compress(c)
        yield comp.flush()
    n = _write_part(dest, gen())
    back = hashlib.sha256()
    for c in _decoded(dest):
        back.update(c)
    if h.hexdigest() != sha:
        dest.unlink(missing_ok=True)
        raise OSError(f"{f.name} changed while it was being copied; it goes in on the next run")
    if back.hexdigest() != sha or n >= f.stat().st_size:
        dest.unlink(missing_ok=True)
        raw = _object(objects, sha, "raw")
        return raw, _write_part(raw, _chunks(f))
    return dest, n


def put(objects: Path, f: Path, sha: str) -> tuple[Path, int]:
    """Store one file's object (if the vault doesn't have it yet). Compressed copies are decoded and re-hashed before
    they count; anything that doesn't come back bit-identical, or doesn't get smaller, is stored raw.
    -> (the object, bytes written)"""
    have = find_object(objects, sha)
    if have is not None:
        return have, 0
    fsutil.guard(objects)
    codec = codec_for(f)
    if f.stat().st_size > FLAC_MAX:   # big: stream it, never the whole file in memory
        return _put_big(objects, f, sha) if codec != "raw" else (_object(objects, sha, "raw"),
                                                                 _write_part(_object(objects, sha, "raw"), _chunks(f)))
    data = f.read_bytes()
    if hashlib.sha256(data).hexdigest() != sha:
        raise OSError(f"{f.name} changed while it was being copied; it goes in on the next run")
    blob = None
    if codec != "raw":
        try:
            blob = _encode(data, codec)
            if blob is not None and (len(blob) >= len(data) or hashlib.sha256(decode(blob, codec)).hexdigest() != sha):
                blob = None
        except Exception:   # noqa: BLE001 - a codec that can't take this file: store it as it is
            blob = None
    if blob is None:
        codec, blob = "raw", data
    dest = _object(objects, sha, codec)
    return dest, _write_part(dest, [blob])


def get(objects: Path, sha: str) -> bytes:
    """An object's original bytes, re-hashed (a damaged object is an error, never a quiet bad restore)."""
    obj = find_object(objects, sha)
    if obj is None:
        raise ModelError(f"The vault is missing a piece ({sha[:12]}…). Run the vault again from the library, if it still has it.")
    data = decode(obj.read_bytes(), obj.suffix.lstrip("."))
    if hashlib.sha256(data).hexdigest() != sha:
        raise ModelError(f"A piece of the vault is damaged ({obj.name}); it wasn't restored.")
    return data


# ---------------------------------------------------------------- the vault folder

def settings() -> dict:
    v = CONFIG.get("vault") or {}
    return {"folder": v.get("folder") or "", "size": v.get("size") or "matters"}


def root_of(folder: str | Path) -> Path:
    return Path(folder) / NAME


def connected(folder: str | Path) -> bool:
    """Is the vault's drive there? (Its folder may not be yet: the first run makes it.) A drive letter, a Linux mount
    point or a share's root: the nearest folder above that exists, which must not be the bare filesystem root unless
    the folder itself is there."""
    p = Path(folder)
    if p.exists():
        return True
    q = p.parent
    while not q.exists() and q.parent != q:
        q = q.parent
    return q.exists() and (q != Path(q.anchor) or bool(p.drive))   # "E:\Vault" with E: there; not "/" for an unplugged mount


def check_folder(folder: str, size: str | None = None) -> dict:
    """Is this a good place for the vault? {ok, why, warn, free, need}. Refused: inside the app folder or any library /
    working / model folder, too little space (2x the vault's raw size), can't write and read back. Warned: the same
    drive as UnderCover's library (a disk failure takes both)."""
    if not folder:
        return {"ok": False, "why": "Pick a drive or folder for the vault."}
    p = Path(folder).expanduser()
    if not p.is_absolute():
        return {"ok": False, "why": "Give the vault's full place, with its drive."}
    from .config import ROOT
    for key in ("uploads_dir", "output_dir", "voices_dir", "cache_dir", "models_dir", "themes_dir", "logs_dir"):
        try:
            lib = path(key)
        except Exception:   # noqa: BLE001 - a path key this config doesn't have
            continue
        if storage._inside(p, lib) or storage._inside(lib, p):
            return {"ok": False, "why": f"The vault can't share a folder with UnderCover's own files ({lib})."}
    if storage._inside(p, ROOT):
        return {"ok": False, "why": "The vault can't live inside UnderCover's folder."}
    if storage.ALLOWED_ROOTS is not None and not any(storage._inside(p, r) for r in storage.ALLOWED_ROOTS):
        return {"ok": False, "why": "Test mode: that folder is outside the test's own."}
    probe = p
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    if not probe.exists():
        return {"ok": False, "why": "That drive isn't there."}
    need = sum(f.stat().st_size for _, _, f in items(size or settings()["size"]))
    free = shutil.disk_usage(probe).free
    have = 0
    if (root_of(p) / "objects").is_dir():   # space already used by this vault counts as there
        have = sum(f.stat().st_size for f in (root_of(p) / "objects").rglob("*") if f.is_file())
    if free + have < 2 * need:
        return {"ok": False, "why": f"Not enough space there: the vault wants about {2 * need / 1e9:.1f} GB free "
                                    f"(twice what it holds), {free / 1e9:.1f} GB is.", "free": free, "need": need}
    made = []   # folders this check creates: removed again, so looking at a drive leaves nothing on it
    d = root_of(p)
    while not d.exists():
        made.append(d)
        d = d.parent
    try:
        root_of(p).mkdir(parents=True, exist_ok=True)
        t = root_of(p) / ".write-test"
        t.write_bytes(b"vault")
        ok = t.read_bytes() == b"vault"
        t.unlink()
        if not ok:
            raise OSError("read back differently")
    except OSError as e:
        return {"ok": False, "why": f"Couldn't write there ({e.strerror or e})."}
    finally:
        for d in made:   # innermost first; only empty folders
            try:
                d.rmdir()
            except OSError:
                break
    warn = ""
    try:   # the same device as the library (any OS: st_dev), so a disk failure would take both
        same = os.stat(probe).st_dev == os.stat(path("uploads_dir")).st_dev
    except OSError:
        same = False
    if same:
        warn = "That's the same drive as your library: the vault won't survive that disk failing. Another drive is better."
    return {"ok": True, "why": "", "warn": warn, "free": free, "need": need}


def _state(root: Path) -> dict:
    try:
        return json.loads((root / "vault.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"format": FORMAT, "index": {}, "runs": []}


def _save_state(root: Path, st: dict):
    fsutil.write_text(root / "vault.json", json.dumps(st))


# ---------------------------------------------------------------- a run

def run(folder: str | None = None, size: str | None = None, progress=None, cancel=None) -> dict:
    """One vault run: every item hashed (only when its size or mtime changed), new objects stored, a snapshot written
    last (a run cut short leaves the previous snapshot whole), then pruning. -> a short report."""
    s = settings()
    folder, size = folder or s["folder"], size or s["size"]
    if not folder:
        raise ModelError("The vault is off: pick a drive for it in SafeHouse first.")
    root = root_of(folder)
    if storage.ALLOWED_ROOTS is not None and not any(storage._inside(root, r) for r in storage.ALLOWED_ROOTS):
        # a sandbox copies the user's settings, [vault] included: it must never write into their real vault
        raise ModelError("Test mode: the vault folder is outside the test's own; nothing was vaulted.")
    if not connected(folder):
        raise ModelError(f"The vault's drive isn't connected ({folder}).")
    with _RUN_LOCK:
        return _run(root, size, progress, cancel)


def _run(root: Path, size: str, progress, cancel) -> dict:
    objects = root / "objects"
    st = _state(root)
    index = st.setdefault("index", {})
    todo = items(size)
    total = sum(f.stat().st_size for _, _, f in todo) or 1
    snap, done, wrote, new = {}, 0, 0, 0
    t0 = time.time()
    for kind, key, f in todo:
        if cancel and cancel():
            raise InterruptedError
        try:
            stt = f.stat()
        except OSError:
            continue   # gone since the list was made
        sig = [stt.st_size, stt.st_mtime_ns]
        known = index.get(key)
        sha = known[2] if known and known[:2] == sig and find_object(objects, known[2]) else None
        try:
            if sha is None:
                sha = sha256_file(f)
            obj, n = put(objects, f, sha)
        except OSError:
            continue   # locked or changing right now: it goes in next time
        wrote += n
        new += bool(n)
        index[key] = sig + [sha]
        snap[key] = [sha, stt.st_size, stt.st_mtime_ns, kind]
        done += stt.st_size
        if progress:
            progress(f"Vaulting {f.name}", done / total)
    for k in [k for k in index if k not in snap]:
        index.pop(k)
    snaps = root / "snapshots"
    snaps.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    while (snaps / f"{stamp}.json").exists():   # names are per second: never overwrite an earlier picture
        time.sleep(0.25)
        stamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    fsutil.write_text(snaps / f"{stamp}.json", json.dumps({"made": stamp, "size": size, "files": snap}))
    rep = {"made": stamp, "files": len(snap), "new": new, "bytes_written": wrote, "seconds": round(time.time() - t0, 1),
           "raw": sum(v[1] for v in snap.values())}
    rep["pruned"] = prune(root)
    rep["stored"] = sum(f.stat().st_size for f in objects.rglob("*") if f.is_file()) if objects.is_dir() else 0
    st["runs"] = (st.get("runs") or [])[-49:] + [rep]
    st["format"] = FORMAT
    _save_state(root, st)
    return rep


def snapshots(root: Path) -> list[str]:
    d = root / "snapshots"
    return sorted(f.stem for f in d.glob("*.json")) if d.is_dir() else []


def load_snapshot(root: Path, stamp: str | None = None) -> dict:
    names = snapshots(root)
    if not names:
        raise ModelError("The vault is empty: nothing has been vaulted yet.")
    stamp = stamp or names[-1]
    if stamp not in names:
        raise ModelError("That vault snapshot isn't there.")
    return json.loads((root / "snapshots" / f"{stamp}.json").read_text(encoding="utf-8"))


def keep_which(stamps: list[str], now: datetime | None = None) -> list[str]:
    """Pruning: every snapshot of the last 7 days, then the newest of each week for 2 months, then of each month.
    The newest snapshot is always kept."""
    now = now or datetime.now()
    keep, weeks, months = set(stamps[-1:]), set(), set()
    for s in sorted(stamps, reverse=True):
        t = datetime.strptime(s, "%Y-%m-%dT%H-%M-%S")
        age = now - t
        if age <= timedelta(days=7):
            keep.add(s)
        elif age <= timedelta(days=61):
            w = t.isocalendar()[:2]
            if w not in weeks:
                weeks.add(w)
                keep.add(s)
        else:
            m = (t.year, t.month)
            if m not in months:
                months.add(m)
                keep.add(s)
    return sorted(keep)


def prune(root: Path, now: datetime | None = None) -> int:
    """Remove snapshots the policy lets go, then the objects no kept snapshot names (inside the vault only).
    -> objects removed"""
    stamps = snapshots(root)
    keep = keep_which(stamps, now)
    drop = [root / "snapshots" / f"{s}.json" for s in stamps if s not in keep]
    if drop:
        storage.remove_files(drop, within=[root / "snapshots"])
    named = set()
    for s in keep:
        named.update(v[0] for v in load_snapshot(root, s)["files"].values())
    objects = root / "objects"
    gone = [f for f in objects.rglob("*") if f.is_file() and f.name.split(".", 1)[0] not in named] if objects.is_dir() else []
    if gone:
        storage.remove_files(gone, within=[objects])
    return len(gone)


# ---------------------------------------------------------------- restore

def _records(root: Path, files: dict) -> tuple[dict, dict]:
    """The songs' and the speeches' own records in this snapshot (read from the vault, re-hashed like any restore):
    ({song id: title}, {speech file name: (speech id, title)}), so an item's files list as one row under its title."""
    songs, speech = {}, {}
    for key, (sha, size, *_rest) in files.items():
        if not key.endswith(".json") or size > 1 << 20:
            continue
        top, _, rest = key.partition("/")
        if not ((top == "songs" and "/" not in rest) or (top == "covers" and rest.startswith("speech/") and rest.count("/") == 1)):
            continue
        try:
            rec = json.loads(get(root / "objects", sha))
        except (ModelError, ValueError, OSError):
            continue
        if not isinstance(rec, dict):
            continue
        if top == "songs":
            songs[rest[:-5]] = str(rec.get("title") or rec.get("name") or rest[:-5])
        elif rec.get("id"):
            label = str(rec.get("title") or rec["id"]) + (f" ({rec['voice']})" if rec.get("voice") else "")
            for name in (rec.get("file"), rec.get("mp3"), f"{rec['id']}.json"):
                if name:
                    speech[name] = (rec["id"], label)
    return songs, speech


def _item_of(key: str, songs: dict, speech: dict) -> tuple[str, str]:
    """(item id, title) for a vault key: a voice / project is its folder, a song its id (audio and record together),
    a speech its id (wav, mp3 and record together), anything else its own name."""
    top, _, rest = key.partition("/")
    if top in ("voices", "training"):
        name = rest.split("/", 1)[0]
        return name, name
    if top == "songs" and "/" not in rest:
        sid = rest.split(".", 1)[0]
        return sid, songs.get(sid, rest)
    if top == "covers" and rest.startswith("speech/"):
        name = rest.split("/", 1)[1]
        if name in speech:
            return speech[name]
    return rest, rest


def contents(folder: str | None = None, stamp: str | None = None) -> dict:
    """What a snapshot holds, by kind and item (a voice, a song, a speech, a project, settings), each item with its id,
    its title (from its own record, for songs and speech) and every one of its files: for SafeHouse's Vault room."""
    root = root_of(folder or settings()["folder"])
    snap = load_snapshot(root, stamp)
    songs, speech = _records(root, snap["files"])
    groups: dict = {}
    for key, (sha, size, _, kind) in snap["files"].items():
        try:
            target_of(key)
        except ModelError:   # a name this version won't restore (a damaged or foreign snapshot): leave it out
            continue
        item, title = _item_of(key, songs, speech)
        g = groups.setdefault(kind, {}).setdefault(item, {"item": item, "title": title, "kind": kind, "files": 0,
                                                          "bytes": 0, "keys": []})
        g["files"] += 1
        g["bytes"] += size
        g["keys"].append(key)
    out = {}
    for kind, its in groups.items():
        for it in its.values():
            here = [target_of(k) for k in it["keys"]]
            it["here"] = "same" if all(t.is_file() and t.stat().st_size == snap["files"][k][1] for t, k in zip(here, it["keys"])) \
                else "missing" if not any(t.exists() for t in here) else "different"
        out[kind] = sorted(its.values(), key=lambda x: x["title"].lower())
    return {"snapshot": snap["made"], "snapshots": snapshots(root), "kinds": out}


def _both_name(t: Path, key: str) -> Path:
    """Keep both: the restored copy goes beside the current one. A voice keeps its folder shape (<name>-from-vault)."""
    top, _, rest = key.partition("/")
    if top == "voices":
        name, _, inner = rest.partition("/")
        return path("voices_dir") / f"{name}-from-vault" / inner
    if top == "training":
        name, _, inner = rest.partition("/")
        return path("cache_dir") / "training" / f"{name}-from-vault" / inner
    return t.with_name(f"{t.stem} (from vault){t.suffix}")


def restore(keys: list[str], on_conflict: str = "ask", folder: str | None = None, stamp: str | None = None) -> dict:
    """Bring files back, bit-identical (each re-hashed). on_conflict, for a file that's there and differs: "ask" (nothing
    is written, the conflicts come back), "both" (the restored copy beside it), "replace" (the current one is vaulted
    first, as a fresh snapshot), "skip". A file that's there and identical is left alone. Writes only into the library's
    own folders. -> {restored, same, skipped, conflicts}"""
    if on_conflict not in ("ask", "both", "replace", "skip"):
        raise ModelError("Keep both, replace or skip?")
    s = settings()
    folder = folder or s["folder"]
    root = root_of(folder)
    snap = load_snapshot(root, stamp)
    files = snap["files"]
    plan, conflicts, same = [], [], []
    for key in keys:
        if key not in files:
            raise ModelError("That isn't in this vault snapshot.")
    whole = lambda k: "/".join(k.split("/")[:2]) if k.split("/")[0] in ("voices", "training") else k   # a voice / project is one item
    status = {}
    for key in keys:
        t = target_of(key)
        status[key] = ("missing" if not t.is_file() else
                       "same" if t.stat().st_size == files[key][1] and sha256_file(t) == files[key][0] else "different")
    differ = {whole(k) for k, v in status.items() if v == "different"}
    for key in keys:
        sha, t = files[key][0], target_of(key)
        if on_conflict == "both" and whole(key) in differ:   # keep both: the whole item comes back beside the current one
            if status[key] == "different":
                conflicts.append(key)
            plan.append((key, sha, _both_name(t, key)))
            continue
        if status[key] == "same":
            same.append(key)
            continue
        if status[key] == "different":
            conflicts.append(key)
            if on_conflict in ("ask", "skip"):
                continue
        plan.append((key, sha, t))
    if on_conflict == "ask" and conflicts:
        return {"restored": [], "same": same, "skipped": [], "conflicts": conflicts}
    folders = [path("voices_dir"), path("uploads_dir"), path("output_dir"), path("cache_dir") / "training",
               path("cache_dir") / "cadence", path("themes_dir")]
    files_ok = [config.CONFIG_FILE, config.CONFIG_FILE.parent / "tutorial.json", path("cache_dir") / "voice_quality.json"]
    for _, _, t in plan:
        if not (any(storage._inside(t, a) and not storage._same(t, a) for a in folders) or any(storage._same(t, f) for f in files_ok)):
            raise ModelError(f"A restore may only write into UnderCover's library ({t} isn't); nothing was restored.")
        if storage.ALLOWED_ROOTS is not None and storage.refuse([t]):
            raise ModelError(storage.refuse([t]))
    if on_conflict == "replace" and any(t.exists() for _, _, t in plan):
        run(folder)   # what's there now goes into the vault first, so replacing loses nothing
    done = []
    for key, sha, t in plan:
        if storage._same(t, config.CONFIG_FILE):
            data = _config_keeping_paths(get(root / "objects", sha))
            _write_part(t, [data])
        else:
            obj = find_object(root / "objects", sha)
            if obj is None:
                raise ModelError(f"The vault is missing a piece ({sha[:12]}…); restored so far: {len(done)} files.")
            h = hashlib.sha256()

            def checked(obj=obj, h=h):
                for c in _decoded(obj):
                    h.update(c)
                    yield c
            part = t.with_name(t.name + ".vault-part")
            _write_part(part, checked())
            if h.hexdigest() != sha:
                part.unlink(missing_ok=True)
                raise ModelError(f"A piece of the vault is damaged ({obj.name}); that file wasn't restored.")
            os.replace(part, t)
        done.append(key)
    return {"restored": done, "same": same, "skipped": conflicts if on_conflict == "skip" else [], "conflicts": []}


def restore_now(keys: list[str], on_conflict: str, stamp: str | None = None) -> bool:
    """A restore in the background (Replace vaults everything first, which can take minutes): the outcome lands in
    STATE["restore"] for GET /api/vault. False if a restore is already running."""
    if (STATE.get("restore") or {}).get("running"):
        return False
    STATE["restore"] = {"running": True, "result": None, "error": ""}

    def go():
        try:
            STATE["restore"]["result"] = restore(keys, on_conflict, stamp=stamp)
        except Exception as e:   # noqa: BLE001 - shown in SafeHouse
            STATE["restore"]["error"] = str(e) or type(e).__name__
        finally:
            STATE["restore"]["running"] = False
    threading.Thread(target=go, daemon=True, name="vault-restore").start()
    return True


def _config_keeping_paths(data: bytes) -> bytes:
    """A restored config.toml keeps this install's [paths] (where its folders are), everything else from the vault."""
    import re
    old = data.decode("utf-8")
    cur = config.CONFIG_FILE.read_text(encoding="utf-8") if config.CONFIG_FILE.is_file() else ""
    # this install's [paths], or none when it uses the default folders (a vaulted [paths] from elsewhere never comes in)
    new = config.replace_section(old, "paths", config.section_text(cur, "paths"))
    import tomllib
    tomllib.loads(new)   # never write a config the app can't read back
    return new.encode("utf-8")


# ---------------------------------------------------------------- quietly: after new keepers, and daily, when idle

QUIET_MIN = 10      # a change settles this long before it's vaulted (a cover being kept, a voice being added)
DAILY_H = 24
STATE = {"running": False, "progress": None, "error": "", "sig": None, "since": 0.0, "status": None, "restore": None}


def signature(size: str) -> list:
    """A cheap fingerprint of what would be vaulted: how many files, how many bytes, the newest change."""
    n = b = m = 0
    for _, _, f in items(size):
        try:
            s = f.stat()
        except OSError:
            continue
        n, b, m = n + 1, b + s.st_size, max(m, s.st_mtime_ns)
    return [n, b, m]


def due(now: float | None = None) -> str | None:
    """Why a run is due now ("new keepers" / "daily"), or None. Off, or the drive away: None (the Doctor says so)."""
    now = now or time.time()
    s = settings()
    if not s["folder"]:
        return None
    root = root_of(s["folder"])
    if not connected(s["folder"]):
        STATE["error"] = f"The vault's drive isn't connected ({s['folder']})."
        return None
    STATE["error"] = "" if STATE["error"].startswith("The vault's drive") else STATE["error"]
    st = _state(root)
    sig = signature(s["size"])
    if sig != STATE["sig"]:
        STATE["sig"], STATE["since"] = sig, now
    runs = st.get("runs") or []
    if not runs:
        return "first"
    if sig != st.get("sig") and now - STATE["since"] >= QUIET_MIN * 60:
        return "new keepers"
    last = datetime.strptime(runs[-1]["made"], "%Y-%m-%dT%H-%M-%S").timestamp()
    return "daily" if now - last >= DAILY_H * 3600 else None


def run_now(reason: str = "asked") -> bool:
    """Start a run in the background (False if one is running). Its report lands in STATE and vault.json."""
    if STATE["running"]:
        return False
    STATE.update(running=True, progress={"message": "Starting the vault", "fraction": 0.0}, error="")

    def go():
        try:
            s = settings()
            rep = run(progress=lambda msg, frac: STATE.update(progress={"message": msg, "fraction": round(frac, 3)}))
            root = root_of(s["folder"])
            st = _state(root)
            st["sig"] = signature(s["size"])
            st["runs"][-1]["reason"] = reason
            _save_state(root, st)
            STATE["sig"] = st["sig"]
        except Exception as e:   # noqa: BLE001 - shown in SafeHouse and the Doctor, never a crash
            STATE["error"] = str(e) or type(e).__name__
        finally:
            STATE.update(running=False, progress=None)
    import threading
    threading.Thread(target=go, daemon=True, name="vault").start()
    return True


def _idle() -> bool:
    from . import worklist
    return worklist._gpu_idle() and not worklist.held() and not storage.MOVING


def start():
    """The watcher (the server starts it): once a minute, a due run starts when the GPU queue is idle."""
    import threading

    def loop():
        while True:
            try:   # the Doctor and the tray read this copy: a slow or sleeping drive never holds them up
                STATE["status"] = status()
                why = due()
                if why and _idle():
                    run_now(why)
            except Exception as e:   # noqa: BLE001 - the watcher never dies
                STATE["error"] = str(e)
            time.sleep(60)
    threading.Thread(target=loop, daemon=True, name="vault-watch").start()


def status() -> dict:
    """For SafeHouse and the Doctor: on/off, where, the last run, how big, what's happening."""
    s = settings()
    out = {**s, "on": bool(s["folder"]), "running": STATE["running"], "progress": STATE["progress"],
           "error": STATE["error"], "connected": bool(s["folder"]) and connected(s["folder"]), "last": None,
           "snapshots": 0, "stored": 0, "raw": 0, "restore": STATE.get("restore")}
    if out["connected"]:
        root = root_of(s["folder"])
        st = _state(root)
        runs = st.get("runs") or []
        out["last"] = runs[-1] if runs else None
        out["snapshots"] = len(snapshots(root))
        out["raw"] = runs[-1]["raw"] if runs else 0
        out["stored"] = runs[-1].get("stored", 0) if runs else 0
    return out


def set_folder(folder: str, size: str | None = None) -> dict:
    """Switch the vault on (a checked folder), change its size, or off (folder "")."""
    from .config import set_values
    size = size or settings()["size"]
    if size not in ("matters", "everything"):
        raise ModelError('Pick "What matters" or "Everything I\'d miss".')
    if folder:
        chk = check_folder(folder, size)
        if not chk["ok"]:
            raise ModelError(chk["why"])
    set_values("vault", {"folder": json.dumps(str(Path(folder)) if folder else ""), "size": json.dumps(size)})
    STATE.update(sig=None, error="")
    return status()
