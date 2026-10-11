"""The suite's web server: the HTTP shell (static files, /files/, status, restart), the routes every module shares
(jobs, voices, Pause / Resume and the queue) and the route assembly (FrontMan, DoubleAgent, SafeHouse). Python
standard library only.

    python -m undercover.server        (or UnderCover.bat; undercover/server.py runs main() here)

JSON API + Server-Sent Events for live progress; audio served with HTTP Range support so the
player can seek. All GPU work runs on one worker thread, so the models stay loaded and jobs queue.
"""
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from ..core import gpu, jobs
from ..core.config import CONFIG, ROOT, path
from ..core.files import roots as _roots
from ..core.jobs import GPU_QUEUE, JOBS, STATUS, Cancelled, Job, friendly, gpu_worker
from ..safehouse import api as safehouse_api
from ..frontman import api as frontman_api
from ..doubleagent import api as train_api
from ..frontman import oneshot  # noqa: F401 - registers the one-shot with Pause / Resume
from ..doubleagent import night  # noqa: F401 - registers the overnight queue's improve / new items
from ..speakeasy import api as speakeasy_api
from ..speakeasy import speak  # noqa: F401 - registers SpeakEasy's user-facing errors

UCFG = CONFIG["ui"]
PKG = Path(__file__).resolve().parents[1]   # the undercover package
WEB = PKG / "web"
for ext, mt in ((".flac", "audio/flac"), (".mp3", "audio/mpeg"), (".wav", "audio/wav"), (".m4a", "audio/mp4"),
                (".opus", "audio/ogg"), (".js", "text/javascript"), (".css", "text/css")):
    mimetypes.add_type(mt, ext)


def code_stamp() -> float:
    """Newest modification time of the server's Python code and config. If it changes after startup, the
    running process is out of date (Python doesn't reload code), and the page tells the user to restart."""
    files = list(PKG.rglob("*.py")) + [ROOT / "config.toml"]
    return max((f.stat().st_mtime for f in files if f.exists()), default=0.0)


STARTUP_STAMP = code_stamp()


SERVER = None          # the running ThreadingHTTPServer
RESTART = threading.Event()


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


def voices_json() -> list[dict]:
    from ..core.voices import list_voices
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
            if p == "/api/work":
                from ..core import worklist
                return self._json(worklist.status())
            if (train_api.handle(self, "GET", p) or safehouse_api.handle(self, "GET", p)
                    or frontman_api.handle(self, "GET", p) or speakeasy_api.handle(self, "GET", p)):
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
                from ..core import worklist
                w = worklist.status()
                work = {"held": w["held"], "resume": w["resume"], "running": w["running"][:1],
                        "queued": sum(i["state"] in ("queued", "paused") for i in w["queue"]), "armed": w["armed"],
                        "start": w["start"], "start_at": w["settings"].get("start_at")}
                return self._json({**STATUS, "queue": GPU_QUEUE.qsize(), "stale": code_stamp() != STARTUP_STAMP,
                                   "auto_job": auto, "cover_job": cover, "training": training, "work": work})
            if p == "/api/voices":
                return self._json(voices_json())
            if p == "/api/search":
                from ..core.voices import ModelError, search
                term = (q.get("q") or [""])[0]
                try:
                    res = search(term, limit=12)
                except ModelError as e:
                    return self._error(str(e))
                return self._json([{"n": i + 1, "name": r.name, "repo": r.repo, "file": r.file,
                                    "size": r.size, "url": r.url, "version": r.version, "sr": r.sr,
                                    "epochs": r.epochs, "steps": r.steps} for i, r in enumerate(res)])
            if p == "/api/jobs":   # running jobs + those finished in the last hour, newest first (the page's job feed)
                now, out = time.time(), []
                for j in sorted(list(JOBS.values()), key=lambda j: -j.created):
                    if not j.finished or (j.ended and now - j.ended < 3600):
                        try:
                            out.append(j.snapshot())
                        except Exception:   # noqa: BLE001 - one odd job mustn't hide the others
                            out.append({"id": j.id, "kind": j.kind, "state": j.state, "title": j.title})
                return self._json(out)
            m = re.fullmatch(r"/api/jobs/(\w+)/events", p)
            if m:
                return self._sse(m.group(1))
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
            if p.startswith("/api/work/"):
                return self._work(p[len("/api/work/"):], self._body())
            if (train_api.handle(self, "POST", p) or safehouse_api.handle(self, "POST", p)
                    or frontman_api.handle(self, "POST", p) or speakeasy_api.handle(self, "POST", p)):
                return
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
        from ..core.voices import ModelError, remove_voice
        p = urlparse(self.path).path
        try:
            if (train_api.handle(self, "DELETE", p) or safehouse_api.handle(self, "DELETE", p)
                    or speakeasy_api.handle(self, "DELETE", p)):
                return
            m = re.fullmatch(r"/api/voices/([a-z0-9-]+)", p)
            if not m:
                return self._error("Not found", 404)
            remove_voice(m.group(1))
            gpu.forget_voice(m.group(1))
            self._json({"ok": True})
        except ModelError as e:
            self._error(str(e))
        except LookupError as e:
            self._error(str(e), 404)
        except PermissionError:
            # still open after fsutil.retry's ~3 s: usually a player streaming it, here or in another app
            self._error("Windows wouldn't let go of that file: something still has it open (a player on this page, or "
                        "another app such as a media player). Close it, wait a few seconds and try again.", 409)
        except Exception as e:
            self._error(friendly(e), 500)

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
        self.send_header("X-Accel-Buffering", "no")       # proxies / security software: pass events on as they come
        self.send_header("Content-Encoding", "identity")
        self.end_headers()
        self.wfile.write(b":" + b" " * 2048 + b"\n\n")   # some filters hold a stream back until a few KB have passed
        self.wfile.flush()
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


    def _work(self, action: str, b: dict):
        """Pause / Resume and the overnight queue (core/worklist.py)."""
        from ..core import worklist
        from ..core.voices import ModelError
        try:
            if action == "pause":
                return self._json(worklist.pause_all())
            if action == "resume":
                return self._json(worklist.resume_all())
            if action == "add":
                return self._json(worklist.add(b.get("kind", ""), b.get("target", "")))
            if action == "remove":
                return self._json(worklist.remove(b.get("id", "")))
            if action == "order":
                return self._json(worklist.reorder(b.get("ids") or []))
            if action == "start":   # {"at": "23:00"} or {} = now; {"stop": true} = don't start more
                return self._json(worklist.arm(False if b.get("stop") else (b.get("at") or None)))
            if action == "settings":
                return self._json(worklist.settings(**{k: b[k] for k in ("finish_by", "keep_awake") if k in b}))
            if action == "dismiss":
                return self._json(worklist.dismiss_report())
        except (ModelError, LookupError) as e:
            return self._error(str(e))
        self._error("Not found", 404)


    def _start_install(self, body: dict):
        from ..core.voices import ModelError, download_and_install, last_search
        src, name = body.get("url"), body.get("name")
        n = body.get("n")   # legacy: position in the last search (pages from before 2026-10-09)
        if n and not src:
            res = last_search()
            if not 1 <= int(n) <= len(res):
                return self._error("Search again; those results are out of date.")
            src = res[int(n) - 1]
        if not src:
            return self._error("Paste a link or pick a search result.")
        job = Job("install", title=f"Installing {name or getattr(src, 'name', None) or 'a voice'}")
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
        from ..core import worklist
        if worklist.active():
            # Voice work: pause it (training keeps its place), restart, and resume it automatically after
            worklist.pause_all(resume_after_restart=True)

            def when_paused():
                t0 = time.time()
                while worklist.active() and time.time() - t0 < 180:
                    time.sleep(1)
                RESTART.set()
                SERVER.shutdown()
            threading.Thread(target=when_paused, daemon=True).start()
            return self._json({"ok": True, "pausing": True})
        self._json({"ok": True})
        RESTART.set()
        threading.Thread(target=SERVER.shutdown, daemon=True).start()  # makes serve_forever() return in main()

    def _voice_upload(self):
        """Install a voice from a dropped .zip/.pth/.index file (body = raw file bytes)."""
        import tempfile
        from ..core.voices import ModelError, _guess_name, install
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
        from ..frontman.pipeline import history
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
    """Hand off to undercover/core/relaunch.py, which waits for THIS process to exit before starting a fresh,
    windowless server (output in logs/server.log). Starting the new server while this one is still releasing
    the GPU leaves this process stuck in the driver."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen([sys.executable, "-m", "undercover.core.relaunch", str(os.getpid()),
                      "--no-browser", "--restarted", *extra_args],
                     cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=flags, close_fds=True)


def main():
    global SERVER
    try:   # the app folder may have been moved or renamed: fix stored paths before anything reads them
        from ..core import relocate
        relocate.check_and_fix()
    except Exception as e:   # noqa: BLE001 - never block startup
        print(f"Couldn't check the app folder location: {e}", flush=True)
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
    jobs.PORT = srv.server_address[1]
    _install_console_handler()
    _clean_old_previews()
    threading.Thread(target=gpu_worker, daemon=True, name="gpu").start()
    from ..core import worklist
    worklist.start()
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
