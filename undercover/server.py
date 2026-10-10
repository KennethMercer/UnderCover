"""Local web UI server (Python standard library only).

    python -m undercover.server        (or UnderCover.bat)

JSON API + Server-Sent Events for live progress; audio served with HTTP Range support so the
player can seek. All GPU work runs on one worker thread, so the models stay loaded and jobs queue.
"""
import hashlib
import json
import mimetypes
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from .config import CONFIG, ROOT, path
from . import library
from .train import api as train_api

UCFG = CONFIG["ui"]
THIS = sys.modules[__name__]   # passed to the training API (this file usually runs as __main__)
WEB = Path(__file__).parent / "web"
AUDIO_EXT = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wma", ".aiff", ".aif", ".webm", ".mp4"}
for ext, mt in ((".flac", "audio/flac"), (".mp3", "audio/mpeg"), (".wav", "audio/wav"), (".m4a", "audio/mp4"),
                (".opus", "audio/ogg"), (".js", "text/javascript"), (".css", "text/css")):
    mimetypes.add_type(mt, ext)


class Cancelled(Exception):
    pass


def code_stamp() -> float:
    """Newest modification time of the server's Python code and config. If it changes after startup, the
    running process is out of date (Python doesn't reload code), and the page tells the user to restart."""
    files = list(Path(__file__).parent.rglob("*.py")) + [ROOT / "config.toml"]
    return max((f.stat().st_mtime for f in files if f.exists()), default=0.0)


STARTUP_STAMP = code_stamp()


# ---------------------------------------------------------------- friendly errors

def friendly(e: BaseException, stage: str = "") -> str:
    """Turn an exception into a message that says what to do. Full traceback goes to logs/errors.log."""
    from .models import ModelError
    from .remix import RemixError
    if isinstance(e, (ModelError, RemixError)):
        return str(e)
    if isinstance(e, Cancelled):
        return "Cancelled."
    name = e.__class__.__name__
    text = str(e)
    if name == "OutOfMemoryError" or "out of memory" in text.lower():
        return ("The GPU ran out of memory. Close other apps using the GPU (games, other AI tools) and try again, "
                "or pick 'Fast' separation quality.")
    if name in ("NoBackendError", "LibsndfileError") or "could not be opened" in text.lower():
        return "That audio file couldn't be read. Try converting it to mp3, wav or flac first."
    if isinstance(e, FileNotFoundError):
        return f"A file is missing: {Path(getattr(e, 'filename', '') or text).name}. If you deleted something from the output folder, run the cover again."
    if isinstance(e, PermissionError):
        return "Windows blocked writing a file. Close any app that has the output file open (e.g. a media player) and try again."
    with open(path("logs_dir") / "errors.log", "a", encoding="utf-8") as f:
        f.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] {stage}\n{traceback.format_exc()}")
    where = f" while {stage}" if stage else ""
    return f"Something went wrong{where} ({name}). Details were saved to logs/errors.log."


# ---------------------------------------------------------------- jobs

class Job:
    def __init__(self, kind: str):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.state = "queued"
        self.events: list[dict] = []
        self.cond = threading.Condition()
        self.cancel = False
        self.result = None

    def push(self, **ev):
        with self.cond:
            ev.setdefault("state", self.state)
            self.events.append(ev)
            self.cond.notify_all()

    def finish(self, state: str, **ev):
        with self.cond:
            self.state = state
        self.push(state=state, final=True, **ev)

    @property
    def finished(self):
        return self.state in ("done", "error", "cancelled")


JOBS: dict[str, Job] = {}
GPU_QUEUE: "queue.Queue[tuple[Job, callable]]" = queue.Queue()
STATUS = {"models": "starting", "gpu": None, "vram_gb": None, "started": time.time()}
SERVER = None          # the running ThreadingHTTPServer
RESTART = threading.Event()
ENGINE = None  # CoverEngine, created on the worker thread


def gpu_worker():
    """Single thread that owns the GPU: warm-up first, then queued cover jobs."""
    global ENGINE
    from .pipeline import CoverEngine
    ENGINE = CoverEngine()
    try:
        import torch
        if torch.cuda.is_available():
            STATUS["gpu"] = torch.cuda.get_device_name(0)
            STATUS["vram_gb"] = round(torch.cuda.get_device_properties(0).total_memory / 2**30)
        # Which torch build this install expects ([gpu].backend; the installer writes it). Never fall back to the
        # CPU silently when a GPU build was expected (a cover would take ages, training hours): say what's wrong instead.
        want = CONFIG["gpu"].get("backend", "rocm")
        have = "rocm" if "+rocm" in torch.__version__ else "cuda" if "+cu" in torch.__version__ else "cpu"
        STATUS["backend"] = have
        if have != want:
            names = {"rocm": "AMD's ROCm build", "cuda": "the NVIDIA CUDA build", "cpu": "the CPU-only build"}
            STATUS["gpu_problem"] = (f"UnderCover's PyTorch ({torch.__version__}) isn't {names[want]}, so the GPU can't be "
                                     "used. Something reinstalled torch; run setup again to restore it.")
        elif have == "cpu":
            STATUS["cpu_mode"] = True   # no supported GPU: covers work but slowly; training is refused (trainer.py)
        elif not torch.cuda.is_available():
            STATUS["gpu_problem"] = ("The graphics card isn't available. Check Device Manager → Display adapters (it may be "
                                     "disabled or showing an error after a crash), update the graphics driver or restart the "
                                     "PC if needed, then restart UnderCover.")
        if STATUS.get("gpu_problem"):
            STATUS["models"] = "error: " + STATUS["gpu_problem"]   # the loop below keeps running, refusing jobs
        elif UCFG["preload_models"]:
            from . import setup_models
            mdir = path("models_dir")
            STATUS["first_run"] = not all((mdir / f).exists() for f in setup_models.FILES)   # ~600 MB to fetch, once
            STATUS["models"] = "loading"
            from .separate import SEP
            ENGINE.separator._get(SEP["vocal_model"])
            ENGINE.converter
        if not STATUS.get("gpu_problem"):
            STATUS["models"] = "ready"
    except Exception as e:  # warm-up failure shouldn't kill the server; the job will report it
        STATUS["models"] = "error: " + friendly(e, "loading models")
    while True:
        job, fn = GPU_QUEUE.get()
        try:
            run_job(job, fn)
        except BaseException as e:   # never let the only GPU thread die: later jobs would wait forever
            try:
                job.finish("error", message=f"Something went wrong ({e.__class__.__name__}).")
            except Exception:
                pass


# ---------------------------------------------------------------- heartbeat (what was happening if the PC dies)

HEARTBEAT = None


def _hb_write(rec: dict):
    """Append one fsync'd line to logs/heartbeat.jsonl (rotated at 5 MB) so the last one survives a power cut."""
    global HEARTBEAT
    if HEARTBEAT is None:
        port = SERVER.server_address[1] if SERVER else UCFG["port"]
        HEARTBEAT = path("logs_dir") / ("heartbeat.jsonl" if port == UCFG["port"] else f"heartbeat-{port}.jsonl")
    try:
        if HEARTBEAT.exists() and HEARTBEAT.stat().st_size > 5e6:
            HEARTBEAT.replace(HEARTBEAT.with_name("heartbeat.1.jsonl"))
        with open(HEARTBEAT, "a", encoding="utf-8") as f:
            f.write(json.dumps({"t": time.strftime("%Y-%m-%d %H:%M:%S"), **rec}) + "\n")
            f.flush()
            os.fsync(f.fileno())
    except OSError:
        pass


def _hb_state() -> dict:
    jobs = []
    for j in list(JOBS.values()):
        if not j.finished:
            ev = j.events[-1] if j.events else {}
            jobs.append({"kind": j.kind, "stage": ev.get("stage"), "epoch": ev.get("epoch"),
                         "message": (ev.get("message") or "")[:80]})
    rec = {"jobs": jobs, "disk_free_gb": round(shutil.disk_usage(ROOT).free / 1e9, 1)}
    torch = sys.modules.get("torch")
    if torch is not None and STATUS.get("gpu"):
        try:
            free, total = torch.cuda.mem_get_info()
            rec["vram_used_gb"] = round((total - free) / 2**30, 2)
        except Exception:
            pass
    return rec


def heartbeat_loop():
    """Every 30 s while a job runs, every 5 min when idle. On start: if the last session didn't end with a clean
    "stop", remember when it died and what it was doing (shown once on the page)."""
    port = SERVER.server_address[1] if SERVER else UCFG["port"]
    f = path("logs_dir") / ("heartbeat.jsonl" if port == UCFG["port"] else f"heartbeat-{port}.jsonl")
    try:
        last = json.loads(f.read_text(encoding="utf-8").strip().splitlines()[-1]) if f.exists() and f.stat().st_size else None
    except (ValueError, IndexError, OSError):
        last = None
    # Only worth a notice when work was in progress (an idle stop without "stop" is just logged)
    if last and last.get("event") != "stop" and last.get("jobs"):
        during = ", ".join((f"{j['kind']} ({j.get('message') or j.get('stage') or ''})").replace(" ()", "")
                           for j in last.get("jobs", [])) or "nothing (idle)"
        STATUS["last_abrupt"] = {"at": last["t"], "during": during}
    _hb_write({"event": "start"})
    while True:
        _hb_write({"event": "beat", **_hb_state()})
        busy = any(not j.finished for j in list(JOBS.values()))
        time.sleep(30 if busy else 300)


def run_job(job: "Job", fn):
    """Run one queued GPU job (also called between training steps, so covers don't wait for training)."""
    if STATUS.get("gpu_problem"):
        job.finish("error", message=STATUS["gpu_problem"])
        return
    free = shutil.disk_usage(ROOT).free
    if free < 3e9:   # separations, conversions and trainings write GBs; failing halfway leaves partial files
        job.finish("error", message=f"Only {free / 1e9:.1f} GB free on drive {ROOT.anchor.rstrip(chr(92))}. "
                                    "Free some space (Library → Clean up) and try again.")
        return
    if job.cancel:
        job.finish("cancelled", message="Cancelled.")
        return
    with job.cond:
        job.state = "running"
    for attempt in (1, 2):
        try:
            fn(job)
        except Cancelled:
            job.finish("cancelled", message="Cancelled.")
        except Exception as e:
            # Out of GPU memory: free what UnderCover holds (cached voices, allocator cache) and try once more.
            # Separations are cached, so the retry picks up where it was; mostly hits smaller cards or a game open.
            if attempt == 1 and _is_oom(e) and job.kind in ("cover", "preview", "auto"):
                _free_gpu()
                job.push(stage=(job.events[-1].get("stage") if job.events else "") or "converting",
                         message="The GPU ran out of memory; freed some and trying again")
                continue
            job.finish("error", message=friendly(e, job.events[-1].get("stage", "") if job.events else ""))
        return


def _is_oom(e: Exception) -> bool:
    return type(e).__name__ == "OutOfMemoryError" or "out of memory" in str(e).lower()


def _free_gpu():
    import gc
    try:
        if ENGINE and ENGINE._conv:
            ENGINE._conv.voices.clear()   # loaded voices (+ their indexes) are re-loaded on demand
    except Exception:
        pass
    gc.collect()
    torch = sys.modules.get("torch")
    if torch is not None:
        try:
            torch.cuda.empty_cache()
        except Exception:
            pass


# ---------------------------------------------------------------- songs (uploads)

def _ffprobe(p: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                          "format=duration:stream=sample_rate,channels", "-of", "json", str(p)],
                         capture_output=True, text=True)
    if out.returncode != 0:
        raise ValueError("not audio")
    d = json.loads(out.stdout)
    st = (d.get("streams") or [{}])[0]
    if not st:
        raise ValueError("no audio stream")
    return {"duration": float(d["format"]["duration"]), "sample_rate": int(st.get("sample_rate", 0)),
            "channels": int(st.get("channels", 0))}


def _peaks(p: Path, bins: int = 1200) -> list[float]:
    """Waveform overview: peak level per bin, decoded via ffmpeg (any format)."""
    import numpy as np
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(p), "-ac", "1", "-ar", "4000", "-f", "f32le", "-"],
                         capture_output=True).stdout
    a = np.abs(np.frombuffer(raw, dtype=np.float32))
    if not len(a):
        return []
    a = a[: len(a) // bins * bins].reshape(bins, -1).max(axis=1) if len(a) >= bins else a
    top = float(a.max()) or 1.0
    return [round(float(x) / top, 3) for x in a]


def _cancel_presep(keep=None):
    """Cancel background separations, except one producing exactly the stems `keep` = (song id, quality, dereverb,
    karaoke) needs (that one is about to be used)."""
    for j in JOBS.values():
        if j.kind == "presep" and not j.finished and getattr(j, "presep", None) != keep:
            j.cancel = True


def register_song(tmp: Path, name: str, sha256_hex: str | None = None, extra: dict | None = None) -> dict:
    """Move an audio file into uploads/ (named by content hash, like the stem cache) and write its metadata.
    Shared by dropped files and songs fetched from YouTube. Raises ValueError if it isn't readable audio."""
    from .remix import lufs
    up = path("uploads_dir")
    if sha256_hex is None:
        h = hashlib.sha256()
        with open(tmp, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        sha256_hex = h.hexdigest()
    song_id = sha256_hex[:16]
    dest = up / f"{song_id}{Path(name).suffix.lower() or Path(tmp).suffix.lower()}"
    if dest.exists():
        Path(tmp).unlink(missing_ok=True)
    else:
        Path(tmp).replace(dest)
    try:
        info = _ffprobe(dest)
    except (ValueError, KeyError, json.JSONDecodeError):
        dest.unlink(missing_ok=True)
        raise ValueError(f"'{name}' couldn't be read as audio. Try converting it to mp3, wav or flac.")
    meta = {"id": song_id, "name": name, "title": Path(name).stem, "file": dest.name, **info,
            "lufs": round(lufs(dest), 1), "peaks": _peaks(dest), "url": file_url(dest), **(extra or {})}
    (up / f"{song_id}.json").write_text(json.dumps(meta), encoding="utf-8")
    return meta


def song_meta(song_id: str) -> dict:
    meta = path("uploads_dir") / f"{song_id}.json"
    if not re.fullmatch(r"[0-9a-f]{16}", song_id) or not meta.exists():
        raise LookupError("That song isn't loaded anymore. Drop it in again.")
    return json.loads(meta.read_text(encoding="utf-8"))


def file_url(p: Path | str | None) -> str | None:
    """URL for a file inside an allowed folder, else None."""
    if not p:
        return None
    p = Path(p).resolve()
    for root_name, root in _roots().items():
        try:
            rel = p.relative_to(root)
        except ValueError:
            continue
        return f"/files/{root_name}/{quote(rel.as_posix())}"
    return None


def _roots() -> dict[str, Path]:
    return {"output": path("output_dir").resolve(), "uploads": path("uploads_dir").resolve(),
            "converted": (path("cache_dir") / "converted").resolve(), "test": (ROOT / "test").resolve(),
            "previews": (path("cache_dir") / "previews").resolve(),
            "training": (path("cache_dir") / "training").resolve(),
            "stems": (path("cache_dir") / "stems").resolve()}


def history_items() -> list[dict]:
    from .pipeline import history
    out = []
    for h in history():
        out.append({
            "id": h["id"], "title": h.get("title") or Path(h["song"]).stem, "voice": h["voice"],
            "created": h.get("created", ""), "seconds": h.get("seconds", {}), "options": h.get("options", {}),
            "output": file_url(h["output"]), "output_name": Path(h["output"]).name,
            "original": file_url(h["song"]), "vocal": file_url(h.get("converted_vocal")),
            "backing": file_url(h.get("converted_backing")),
            "backing_voice": (h.get("options", {}).get("backing_voice")
                              if h.get("converted_backing") and h.get("options", {}).get("backing_voice") != h["voice"] else None),
            "notes": h.get("notes", []),
        })
    return out


def voices_json() -> list[dict]:
    from .models import list_voices
    return [{"name": v.name, "version": v.version, "sr": v.sr, "index": bool(v.index), "f0": v.f0,
             "source": v.source, "installed": v.installed} for v in list_voices()]


# ---------------------------------------------------------------- HTTP handler

class Handler(BaseHTTPRequestHandler):
    server_version = "UnderCover"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # keep the console quiet; errors are reported in the UI
        pass

    # helpers
    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, message, status=400):
        self._json({"error": message}, status)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n))
        except ValueError:
            return {}

    def _guard_host(self) -> bool:
        """Refuse requests whose Host isn't this server (a DNS-rebinding page could otherwise READ local files)."""
        port = self.server.server_address[1]
        host = (self.headers.get("Host") or "").lower()
        if host not in (f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"):
            self._error("Forbidden", 403)
            return False
        return True

    def _guard_origin(self) -> bool:
        """Block cross-site requests from other web pages (only this page may call the API)."""
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).hostname not in ("127.0.0.1", "localhost"):
            self._error("Forbidden", 403)
            return False
        return True

    # routing
    def do_GET(self):
        if not self._guard_host():
            return
        u = urlparse(self.path)
        p, q = u.path, parse_qs(u.query)
        try:
            if train_api.handle(THIS, self, "GET", p) or library.handle(THIS, self, "GET", p):
                return
            if p in ("/", "/index.html"):
                return self._static(WEB / "index.html")
            if p.startswith("/static/"):
                return self._static(WEB / p.removeprefix("/static/"))
            if p.startswith("/files/"):
                return self._file(p, download="download" in q)
            if p == "/api/status":
                auto = next((j.id for j in JOBS.values() if j.kind == "auto" and not j.finished), None)
                cover = next((j.id for j in JOBS.values() if j.kind == "cover" and not j.finished), None)
                training = None   # a voice training anywhere (Train tab, "Do it all" or a one-shot): for the header chip
                for name, a in list(train_api.ACTIVE.items()):
                    if a["kind"] in ("train", "auto") and not a["job"].finished:
                        ev = next((e for e in reversed(a["job"].events) if e.get("epoch") or e.get("eta")), {})
                        training = {"project": name, "epoch": ev.get("epoch"), "epochs": ev.get("epochs"), "eta": ev.get("eta")}
                        break
                return self._json({**STATUS, "queue": GPU_QUEUE.qsize(), "stale": code_stamp() != STARTUP_STAMP,
                                   "auto_job": auto, "cover_job": cover, "training": training})
            if p == "/api/config":
                return self._json(_ui_defaults())
            if p == "/api/voices":
                return self._json(voices_json())
            if p == "/api/search":
                from .models import ModelError, search
                term = (q.get("q") or [""])[0]
                try:
                    res = search(term, limit=12)
                except ModelError as e:
                    return self._error(str(e))
                return self._json([{"n": i + 1, "name": r.name, "repo": r.repo, "file": r.file,
                                    "size": r.size, "url": r.url, "version": r.version, "sr": r.sr,
                                    "epochs": r.epochs, "steps": r.steps} for i, r in enumerate(res)])
            if p == "/api/history":
                return self._json(history_items())
            if p == "/api/songs/search":
                from .models import ModelError
                from . import songs
                try:
                    return self._json([r.to_json() for r in songs.search((q.get("q") or [""])[0])])
                except ModelError as e:
                    return self._error(str(e))
            if p == "/api/auto/plan":
                return self._auto_plan((q.get("text") or [""])[0], (q.get("train") or ["0"])[0] == "1")
            m = re.fullmatch(r"/api/jobs/(\w+)/events", p)
            if m:
                return self._sse(m.group(1))
            m = re.fullmatch(r"/api/songs/([0-9a-f]{16})/key", p)
            if m:
                from .pipeline import song_key
                return self._json(song_key(path("uploads_dir") / song_meta(m.group(1))["file"]))
            m = re.fullmatch(r"/api/songs/([0-9a-f]{16})", p)
            if m:
                return self._json(song_meta(m.group(1)))
            self._error("Not found", 404)
        except LookupError as e:
            self._error(str(e), 404)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def do_POST(self):
        if not self._guard_host() or not self._guard_origin():
            return
        p = urlparse(self.path).path
        try:
            if train_api.handle(THIS, self, "POST", p) or library.handle(THIS, self, "POST", p):
                return
            if p == "/api/upload":
                return self._upload()
            if p == "/api/estimate":
                return self._estimate(self._body())
            if p == "/api/covers":
                return self._start_cover(self._body())
            if p == "/api/preview":
                return self._start_preview(self._body())
            if p == "/api/songs/youtube":
                return self._song_from_youtube(self._body())
            m = re.fullmatch(r"/api/songs/([0-9a-f]{16})/presep", p)
            if m:
                return self._presep(m.group(1), self._body())
            if p == "/api/auto":
                return self._start_auto(self._body())
            if p == "/api/voices/install":
                return self._start_install(self._body())
            if p == "/api/voices/upload":
                return self._voice_upload()
            if p == "/api/open-folder":
                return self._open_folder(self._body())
            if p == "/api/restart":
                return self._restart()
            m = re.fullmatch(r"/api/jobs/(\w+)/cancel", p)
            if m:
                job = JOBS.get(m.group(1))
                if job:
                    job.cancel = True
                return self._json({"ok": True})
            self._error("Not found", 404)
        except LookupError as e:
            self._error(str(e), 404)
        except Exception as e:
            self._error(friendly(e), 500)

    def do_DELETE(self):
        if not self._guard_host() or not self._guard_origin():
            return
        if train_api.handle(THIS, self, "DELETE", urlparse(self.path).path) or library.handle(THIS, self, "DELETE", urlparse(self.path).path):
            return
        m = re.fullmatch(r"/api/voices/([a-z0-9-]+)", urlparse(self.path).path)
        if not m:
            return self._error("Not found", 404)
        from .models import ModelError, remove_voice
        try:
            remove_voice(m.group(1))
            if ENGINE and ENGINE._conv:
                ENGINE._conv.voices.pop(m.group(1), None)
            self._json({"ok": True})
        except ModelError as e:
            self._error(str(e))

    # static + files
    def _static(self, f: Path):
        f = f.resolve()
        if WEB.resolve() not in f.parents or not f.is_file():
            return self._error("Not found", 404)
        body = f.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", (mimetypes.guess_type(f.name)[0] or "application/octet-stream") + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, url_path: str, download: bool):
        parts = url_path.split("/", 3)  # ['', 'files', root, rel]
        roots = _roots()
        if len(parts) < 4 or parts[2] not in roots:
            return self._error("Not found", 404)
        root = roots[parts[2]]
        f = (root / unquote(parts[3])).resolve()
        if root not in f.parents or not f.is_file():
            return self._error("That file no longer exists.", 404)
        size = f.stat().st_size
        start, end = 0, size - 1
        rng = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        if rng and (rng.group(1) or rng.group(2)):
            if rng.group(1):
                start = int(rng.group(1))
                end = int(rng.group(2)) if rng.group(2) else size - 1
            else:  # suffix range: last N bytes
                start = max(0, size - int(rng.group(2)))
            end = min(end, size - 1)
            if start > end:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(f.name)[0] or "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if download:
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(f.name)}")
        self.end_headers()
        with open(f, "rb") as fh:
            fh.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = fh.read(min(1 << 20, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)

    # SSE
    def _sse(self, job_id: str):
        job = JOBS.get(job_id)
        if not job:
            return self._error("That job isn't running anymore.", 404)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        i = 0
        while True:
            with job.cond:
                if i >= len(job.events) and not job.finished:
                    job.cond.wait(timeout=15)
                new = job.events[i:]
                i += len(new)
                done = job.finished and i >= len(job.events)
            if not new:
                self.wfile.write(b": keepalive\n\n")
            for ev in new:
                self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode())
            self.wfile.flush()
            if done:
                self.close_connection = True
                return

    # actions
    def _upload(self):
        n = int(self.headers.get("Content-Length") or 0)
        name = unquote(self.headers.get("X-Filename") or "song")
        ext = Path(name).suffix.lower()
        if ext not in AUDIO_EXT:
            return self._error(f"'{name}' doesn't look like an audio file. Use mp3, wav, flac, m4a or ogg.")
        if not n:
            return self._error("The file was empty.")
        if n > UCFG["max_upload_mb"] * 1e6:
            return self._error(f"That file is over {UCFG['max_upload_mb']} MB.")
        up = path("uploads_dir")
        tmp = up / f"upload_{uuid.uuid4().hex}.part"
        h = hashlib.sha256()
        try:
            with open(tmp, "wb") as f:
                left = n
                while left > 0:
                    chunk = self.rfile.read(min(1 << 20, left))
                    if not chunk:
                        break
                    h.update(chunk)
                    f.write(chunk)
                    left -= len(chunk)
            if left:
                return self._error("The upload was interrupted. Drop the file again.")
            try:
                meta = register_song(tmp, name, h.hexdigest())
            except ValueError as e:
                return self._error(str(e))
            self._json(meta)
        finally:
            tmp.unlink(missing_ok=True)   # moved into place by register_song when it worked

    def _estimate(self, body: dict):
        from .pipeline import estimate
        meta = song_meta(body.get("song", ""))
        opts = _options(body)
        self._json(estimate(path("uploads_dir") / meta["file"], opts))

    def _start_cover(self, body: dict):
        from .models import get_voice
        meta = song_meta(body.get("song", ""))
        opts = _options(body)
        get_voice(opts.voice)  # friendly error now rather than inside the job
        if opts.backing_voice:
            get_voice(opts.backing_voice)
        song = path("uploads_dir") / meta["file"]
        _cancel_presep(keep=(meta["id"], opts.quality, bool(opts.dereverb), bool(opts.karaoke or opts.backing_voice)))
        job = Job("cover")
        JOBS[job.id] = job

        def run(job: Job):
            from .pipeline import estimate
            est = estimate(song, opts)
            order = ["separating", "converting", "mixing"]
            t0 = time.perf_counter()
            stage_start = {"stage": None, "t": t0}

            def progress(stage, frac, msg):
                if job.cancel:
                    raise Cancelled()
                now = time.perf_counter()
                if stage != stage_start["stage"]:
                    stage_start.update(stage=stage, t=now)
                if stage == "done":
                    return
                in_stage = now - stage_start["t"]
                left_stage = (in_stage / frac * (1 - frac)) if frac > 0.05 else max(est[stage] - in_stage, 0)
                later = sum(est[s] for s in order[order.index(stage) + 1:])
                job.push(stage=stage, frac=round(frac, 3), message=msg, elapsed=round(now - t0, 1),
                         stage_left=round(left_stage, 1), total_left=round(left_stage + later, 1))

            job.push(stage="queued", frac=0, message="Starting", estimate=est, elapsed=0)
            res = ENGINE.make_cover(song, opts, progress=progress, title=meta["title"])
            hist = history_items()
            item = next((h for h in hist if h["output_name"] == res.output.name), None)
            job.finish("done", message="Finished", result=item, seconds=res.seconds, notes=res.notes,
                       elapsed=round(time.perf_counter() - t0, 1))

        GPU_QUEUE.put((job, run))
        self._json({"job": job.id, "position": GPU_QUEUE.qsize()})

    def _presep(self, song_id: str, body: dict):
        """Separate a just-loaded song in the background with the page's saved settings, so the first cover skips
        that step (review C3). Only when the GPU is idle; a cover of another song, or with other separation settings,
        cancels it."""
        meta = song_meta(song_id)
        o = body.get("options") or {}
        want = (song_id, o.get("quality") if o.get("quality") in ("fast", "high", "max") else None,
                bool(o.get("dereverb")), bool(o.get("karaoke") or o.get("backing_voice")))
        busy = GPU_QUEUE.qsize() or any(not j.finished for j in JOBS.values() if j.kind != "presep")
        if busy or any(not j.finished and getattr(j, "presep", None) == want for j in JOBS.values()):
            return self._json({"started": False})
        job = Job("presep")
        job.presep = want
        JOBS[job.id] = job

        def run(job: Job):
            from .separate import separate

            def progress(frac, step):
                if job.cancel:
                    raise Cancelled()
            separate(path("uploads_dir") / meta["file"], dereverb=want[2], karaoke=want[3],
                     separator=ENGINE.separator, quality=want[1], progress=progress)
            job.finish("done", message="Stems ready")
        GPU_QUEUE.put((job, run))
        self._json({"started": True, "job": job.id})

    def _song_from_youtube(self, body: dict):
        """Fetch a song from YouTube (a search result or a pasted link) into uploads/, like a dropped file."""
        from .models import ModelError
        from . import songs
        try:
            if body.get("url") and not body.get("id"):
                r = songs.from_link(body["url"])
            else:
                r = songs.SongResult(**{k: body[k] for k in ("id", "url", "title", "raw_title", "channel", "duration")})
            self._json(songs.download(r, register_song))
        except (ModelError, ValueError, KeyError, TypeError) as e:
            self._error(str(e) if not isinstance(e, (KeyError, TypeError)) else "That song result is incomplete. Search again.")

    def _auto_plan(self, text: str, prefer_train: bool):
        """What a one-shot request would do, without downloading anything (shown before it starts)."""
        from .models import ModelError, slug
        from . import autopilot
        try:
            req = autopilot.parse_request(text)
        except ModelError as e:
            return self._error(str(e))
        song, person = req["song"], req["person"]
        inst = autopilot.installed_for(person)
        trained = [v for v in inst if v.extra.get("trained")]
        if trained:
            voice = {"how": "installed", "name": trained[0].name, "trained": True}
        else:
            online = [] if prefer_train else autopilot.online_for(person, 2)
            community = [] if prefer_train else [v.name for v in inst]
            voice = ({"how": "check", "candidates": community + [r.name for r in online], "name": f"{slug(person)}-uc"}
                     if community or online else {"how": "train", "name": f"{slug(person)}-uc"})
        self._json({"song_query": song, "person": person, "style": req["style"], "voice": voice})

    def _start_auto(self, body: dict):
        from . import autopilot
        text = (body.get("text") or "").strip()
        autopilot.parse(text)   # friendly error now rather than inside the job
        if any(j.kind == "auto" and not j.finished for j in JOBS.values()):
            return self._error("A one-shot cover is already running. Wait for it, or cancel it.", 409)
        _cancel_presep()
        job = Job("auto")
        JOBS[job.id] = job
        job.push(stage="queued", frac=0, message="Waiting for the GPU", plan={"request": text})
        GPU_QUEUE.put((job, lambda job: autopilot.run(THIS, job, text, bool(body.get("prefer_train")),
                                                      body.get("song_url") or None)))
        self._json({"job": job.id, "position": GPU_QUEUE.qsize()})

    def _start_preview(self, body: dict):
        from .models import get_voice
        meta = song_meta(body.get("song", ""))
        opts = _options(body)
        get_voice(opts.voice)
        variants = body.get("variants") or []
        if not 1 <= len(variants) <= 6:
            return self._error("Pick between 1 and 6 versions to preview.")
        song = path("uploads_dir") / meta["file"]
        job = Job("cover")  # same GPU queue as covers
        JOBS[job.id] = job

        def run(job: Job):
            def progress(stage, frac, msg):
                if job.cancel:
                    raise Cancelled()
                job.push(stage=stage, frac=round(frac, 3), message=msg)
            res = ENGINE.make_preview(song, opts, variants, progress=progress)
            job.finish("done", message="Previews ready",
                       previews=[{"label": r["label"], "url": file_url(r["file"]), "options": r["options"]} for r in res])

        GPU_QUEUE.put((job, run))
        self._json({"job": job.id})

    def _start_install(self, body: dict):
        from .models import ModelError, download_and_install, last_search
        src, name = body.get("url"), body.get("name")
        n = body.get("n")   # legacy: position in the last search (pages from before 2026-10-09)
        if n and not src:
            res = last_search()
            if not 1 <= int(n) <= len(res):
                return self._error("Search again; those results are out of date.")
            src = res[int(n) - 1]
        if not src:
            return self._error("Paste a link or pick a search result.")
        job = Job("install")
        JOBS[job.id] = job

        def run():
            last = [0.0]

            def progress(done, total):
                if job.cancel:
                    raise Cancelled()
                now = time.time()
                if now - last[0] > 0.25 or (total and done >= total):
                    last[0] = now
                    job.push(stage="downloading", frac=round(done / total, 3) if total else None,
                             message=f"Downloading {done / 1e6:.0f} MB" + (f" of {total / 1e6:.0f} MB" if total else ""))
            try:
                with job.cond:
                    job.state = "running"
                v = download_and_install(src, name, progress)
                job.finish("done", message=f"Installed {v.name}", voice=v.name)
            except Cancelled:
                job.finish("cancelled", message="Cancelled.")
            except ModelError as e:
                job.finish("error", message=str(e))
            except Exception as e:
                job.finish("error", message=friendly(e, "installing the voice"))

        threading.Thread(target=run, daemon=True).start()  # network-bound; doesn't need the GPU thread
        self._json({"job": job.id})

    def _restart(self):
        """Restart the server process to load updated code. Refused while a cover is running or queued."""
        if any(j.kind == "cover" and not j.finished for j in JOBS.values()):
            return self._error("A cover is still being made. Wait for it to finish (or cancel it), then restart.", 409)
        if any((j.kind.startswith("train") or j.kind == "auto") and not j.finished for j in JOBS.values()):
            return self._error("A voice is training or being prepared. Pause it (progress is kept), then restart.", 409)
        self._json({"ok": True})
        RESTART.set()
        threading.Thread(target=SERVER.shutdown, daemon=True).start()  # makes serve_forever() return in main()

    def _voice_upload(self):
        """Install a voice from a dropped .zip/.pth/.index file (body = raw file bytes)."""
        import tempfile
        from .models import ModelError, _guess_name, install
        n = int(self.headers.get("Content-Length") or 0)
        fname = Path(unquote(self.headers.get("X-Filename") or "voice.zip")).name
        if Path(fname).suffix.lower() not in (".zip", ".pth"):
            return self._error("Drop a voice model as a .zip or .pth file.")
        if not n or n > CONFIG["voices"]["max_download_gb"] * 1e9:
            return self._error("That file is empty or too large to be a voice model.")
        name = unquote(self.headers.get("X-Name") or "") or _guess_name("", fname)
        work = Path(tempfile.mkdtemp(prefix="vup_", dir=path("cache_dir")))
        try:
            f = work / fname
            with open(f, "wb") as out:
                left = n
                while left > 0:
                    chunk = self.rfile.read(min(1 << 20, left))
                    if not chunk:
                        break
                    out.write(chunk)
                    left -= len(chunk)
            try:
                v = install(f, name, source=f"file: {fname}")
            except ModelError as e:
                return self._error(str(e))
            self._json({"voice": v.name})
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _open_folder(self, body: dict):
        """Show a cover in Explorer (by history id), or open the output folder."""
        from .pipeline import history
        hid = body.get("history_id")
        item = next((h for h in history() if h["id"] == hid), None) if hid is not None else None
        vdir = path("models_dir") / "voices" / str(body.get("voice") or "")
        if body.get("voice") and re.fullmatch(r"[a-z0-9-]+", body["voice"]) and vdir.is_dir():
            os.startfile(vdir)
        elif item:
            subprocess.Popen(["explorer", f"/select,{Path(item['output'])}"])
        else:
            os.startfile(path("output_dir"))
        self._json({"ok": True})


def _options(body: dict):
    from .pipeline import CoverOptions
    o = body.get("options") or {}
    num = lambda k: float(o[k]) if o.get(k) not in (None, "") else None
    fmt = o.get("format")
    if fmt not in (None, "mp3", "flac", "wav"):
        fmt = None
    q = o.get("quality") if o.get("quality") in ("fast", "high", "max") else None
    voice = body.get("voice") or ""
    if not voice:
        raise LookupError("Pick a voice first.")
    octave = int(num("octave")) if num("octave") is not None else None
    boct = int(num("backing_octave")) if num("backing_octave") is not None else None
    bvoice = o.get("backing_voice") or None
    return CoverOptions(voice=voice, key=num("key"), octave=octave, index_ratio=num("index_ratio"), protect=num("protect"),
                        volume_envelope=num("volume_envelope"), dereverb=bool(o.get("dereverb")),
                        karaoke=bool(o.get("karaoke")), quality=q, format=fmt,
                        reverb=None if o.get("reverb") in (None, "", "auto") else num("reverb"),
                        backing_voice=bvoice, backing_octave=boct, backing_db=num("backing_db"),
                        pitch_cleanup=o.get("pitch_cleanup", True) is not False,
                        pitch_correction=num("pitch_correction"),
                        polish=o.get("polish", True) is not False)


def _ui_defaults() -> dict:
    c, r, s = CONFIG["conversion"], CONFIG["remix"], CONFIG["separation"]
    return {"key": c["key"], "octave": c["octave"], "index_ratio": c["index_ratio"], "protect": c["protect"],
            "volume_envelope": c["volume_envelope"], "quality": s["quality"], "dereverb": s["dereverb"],
            "karaoke": s["karaoke"], "reverb": r["reverb"], "format": r["format"],
            "backing_voice": "", "backing_octave": 0, "backing_db": r["backing_db"],
            "pitch_cleanup": c["pitch_cleanup"], "pitch_correction": c["pitch_correction"], "polish": r["polish"]}


class ExclusiveServer(ThreadingHTTPServer):
    """On Windows, Python's default SO_REUSEADDR lets a SECOND server bind the same port, so two copies of
    UnderCover would answer requests at random. Bind exclusively so a second launch detects the first."""
    allow_reuse_address = False

    def server_bind(self):
        import socket
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def _clean_old_previews(max_age_h: float = 24):
    d = path("cache_dir") / "previews"
    if d.exists():
        for sub in d.iterdir():
            if sub.is_dir() and time.time() - sub.stat().st_mtime > max_age_h * 3600:
                shutil.rmtree(sub, ignore_errors=True)


def _relaunch(extra_args: list[str]):
    """Hand off to undercover/relaunch.py, which waits for THIS process to exit before starting a fresh,
    windowless server (output in logs/server.log). Starting the new server while this one is still releasing
    the GPU leaves this process stuck in the driver."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen([sys.executable, "-m", "undercover.relaunch", str(os.getpid()),
                      "--no-browser", "--restarted", *extra_args],
                     cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=flags, close_fds=True)


def main():
    global SERVER
    host, port = UCFG["host"], UCFG["port"]
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    srv = None
    for _ in range(40 if "--restarted" in sys.argv else 1):  # after a restart, wait for the old process to free the port
        try:
            srv = ExclusiveServer((host, port), Handler)
            break
        except OSError:
            time.sleep(0.5)
    if srv is None:
        url = f"http://{host}:{port}/"
        print(f"UnderCover already seems to be running at {url} (port {port} is in use). Opening it.")
        if UCFG["open_browser"]:
            webbrowser.open(url)
        return
    srv.daemon_threads = True
    SERVER = srv
    _install_console_handler()
    _clean_old_previews()
    threading.Thread(target=gpu_worker, daemon=True, name="gpu").start()
    threading.Thread(target=heartbeat_loop, daemon=True, name="heartbeat").start()
    url = f"http://{host}:{port}/"
    print(f"UnderCover is running at {url}\nKeep this window open while you use it. Press Ctrl+C to stop.")
    if UCFG["open_browser"] and "--no-browser" not in sys.argv:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.", flush=True)
        _hb_write({"event": "stop"})
        _hard_exit()   # a normal exit can hang forever in the GPU runtime's DLL teardown (see PLAN.md)
    if RESTART.is_set():
        _hb_write({"event": "stop", "reason": "restart"})
        srv.server_close()  # free the port for the new process
        _relaunch(["--port", str(port)] if "--port" in sys.argv else [])
        print("Restarting UnderCover...", flush=True)
        _hard_exit()


def _install_console_handler():
    """Closing the UnderCover window (or logging off / shutting down) skips Python's Ctrl+C path: catch it, write the
    heartbeat's clean "stop" and exit via TerminateProcess (a normal exit can hang in the GPU driver)."""
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes
    HANDLER = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

    def handler(event):
        if event in (2, 5, 6):   # CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT
            _hb_write({"event": "stop", "reason": {2: "window closed", 5: "log off", 6: "shutdown"}[event]})
            _hard_exit()
        return False             # Ctrl+C / Ctrl+Break: let Python raise KeyboardInterrupt as before
    global _CONSOLE_HANDLER
    _CONSOLE_HANDLER = HANDLER(handler)   # keep a reference, or it's garbage-collected
    ctypes.windll.kernel32.SetConsoleCtrlHandler(_CONSOLE_HANDLER, True)


_CONSOLE_HANDLER = None


def _hard_exit():
    """End this process immediately. A normal exit (even os._exit) can hang forever on Windows while the GPU
    runtime's DLLs shut down, leaving a zombie holding RAM/VRAM. TerminateProcess skips that; the OS still frees
    all memory, including VRAM."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # Declare types: without them the pseudo-handle (-1) is passed as a 32-bit int, becomes an invalid
        # handle, TerminateProcess silently fails and we fall through to the exit that hangs.
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        k32.TerminateProcess.restype = wintypes.BOOL
        if not k32.TerminateProcess(k32.GetCurrentProcess(), 0):
            print(f"TerminateProcess failed (error {ctypes.get_last_error()})", flush=True)
    os._exit(0)


if __name__ == "__main__":
    main()
