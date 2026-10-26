"""The suite's web server: the HTTP shell (static files, /files/, status, restart), the routes every module shares
(jobs, voices, Pause / Resume and the queue) and the route assembly (FrontMan, DoubleAgent, SafeHouse). Python
standard library only.

    python -m undercover.server        (or scripts/UnderCover.bat; undercover/server.py runs main() here)

JSON API + Server-Sent Events for live progress; audio served with HTTP Range support so the
player can seek. All GPU work runs on one worker thread, so the models stay loaded and jobs queue.
"""
import ipaddress
import json
import mimetypes
import os
import re
import shutil
import sys
import threading
import time
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from ..core import gpu, jobs, phone, platform
from ..core.config import CONFIG, ROOT, path
from ..core.files import AUDIO_EXT, roots as _roots
from ..core.jobs import GPU_QUEUE, JOBS, STATUS, Cancelled, Job, friendly, gpu_worker
from ..safehouse import api as safehouse_api
from ..frontman import api as frontman_api
from ..doubleagent import api as train_api
from ..frontman import oneshot  # noqa: F401 - registers the one-shot with Pause / Resume
from ..doubleagent import night  # noqa: F401 - registers the overnight queue's improve / new items
from ..doubleagent import cadence  # noqa: F401 - registers the cadence items (phase 4) and the engines' add-ons
from ..speakeasy import api as speakeasy_api
from ..speakeasy import speak  # noqa: F401 - registers SpeakEasy's user-facing errors
from . import doctor_api, ear_api, firstrun, firstrun_api, gamepause_api, lowprofile_api, phone_api, themes_api, vault_api

UCFG = CONFIG["ui"]
PKG = Path(__file__).resolve().parents[1]   # the undercover package
WEB = PKG / "web"
for ext, mt in ((".flac", "audio/flac"), (".mp3", "audio/mpeg"), (".wav", "audio/wav"), (".m4a", "audio/mp4"),
                (".opus", "audio/ogg"), (".js", "text/javascript"), (".css", "text/css"), (".woff2", "font/woff2"),
                (".webmanifest", "application/manifest+json")):
    mimetypes.add_type(mt, ext)


def code_stamp() -> float:
    """Newest modification time of the server's Python code and config. If it changes after startup, the
    running process is out of date (Python doesn't reload code), and the page tells the user to restart."""
    from ..core import config
    files = list(PKG.rglob("*.py")) + [config.CONFIG_FILE, config.DEFAULT_FILE]
    return max((f.stat().st_mtime for f in files if f.exists()), default=0.0)


STARTUP_STAMP = code_stamp()


def build_stamp() -> str:
    """What this server runs and serves, fixed at startup: version, commit, and the newest code or page file. A page
    remembers the one it loaded with; when /api/status says another, the tab is older than the server and offers a
    reload (phase 11b: a tab left open across an update ran the old page against the new server)."""
    from ..core import about
    files = [f for f in PKG.rglob("*") if f.suffix in (".py", ".js", ".css", ".html") and "__pycache__" not in f.parts]
    newest = max((f.stat().st_mtime for f in files), default=0.0)
    return f"{about.VERSION}-{(about.git_commit() or {}).get('commit') or 'copy'}-{int(newest)}"


BUILD = build_stamp()


SERVER = None          # the running ThreadingHTTPServer
RESTART = threading.Event()

# JSON request bodies (1.1 hardening): read whole, so capped. The upload routes read their bodies in chunks under their
# own limits and never come here.
JSON_MAX = 1 << 20                        # 1 MB: the longest speech (50,000 characters) is a few hundred KB
JSON_MAX_FOR = {"/api/themes": 8 << 20}   # a theme saved from the editor (its files are capped at 4 MB)
# job kinds only the PC starts (a Storage move, an add-on, Make smaller, the yt-dlp update): never cancelled or retried
# from a phone, whatever route started them
PC_ONLY_KINDS = ("move", "addon", "smaller", "ytdlp")


AWAY_401, AWAY_403 = b"Unauthorized", b"Forbidden"   # the away listener's refusals before a token: no names, no state
PC_ONLY_JOB = "That work was started on the PC and can only be stopped or retried there."


def _pc_only(job) -> bool:
    """A job a phone may not cancel or retry: started by a PC-only route, or of a kind only the PC starts."""
    return job.kind in PC_ONLY_KINDS or bool(getattr(job, "pc_only", False))


class BodyTooLarge(Exception):
    """A JSON body over its route's cap: answered 413, the body never read."""


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
                         "message": (ev.get("message") or "")[:80], "id": j.id,
                         "spec": j.resume_spec})   # the Doctor resumes (or offers) it after a crash (phase 10)
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


def ytdlp_swapped() -> str | None:
    """When the YouTube downloader last changed (a self-heal swap, phase 16; or the old pinned Update)."""
    from ..core import addons, ytdlp
    try:
        return ytdlp.load().get("swapped") or addons.updated("ytdlp")
    except Exception:   # noqa: BLE001
        return addons.updated("ytdlp")


def voices_json() -> list[dict]:
    from ..core.voices import listed_voices, voice_info
    return [{"name": v.name, "version": v.version, "sr": v.sr, "index": bool(v.index), "f0": v.f0,
             "source": v.source, "installed": v.installed, **voice_info(v)} for v in listed_voices()]


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
        if getattr(self, "set_cookie", None):
            self.send_header("Set-Cookie", self.set_cookie)
        self.end_headers()
        self.wfile.write(body)

    def _error(self, message, status=400):
        self._json({"error": message}, status)

    def _body(self) -> dict:
        if getattr(self, "_read", None) is not None:   # read once per request (Retry replays what was sent)
            return self._read
        n = int(self.headers.get("Content-Length") or 0)
        cap = JSON_MAX_FOR.get(urlparse(self.path).path, JSON_MAX)
        if n < 0 or n > cap:
            self.close_connection = True   # (the body isn't read)
            raise BodyTooLarge(f"That request is too large ({max(n, 0) // 1024} KB; this one takes up to {cap // 1024} KB).")
        if not n:
            self._read = {}
            return {}
        try:
            self._read = json.loads(self.rfile.read(n))
        except ValueError:
            self._read = {}
        return self._read

    def _guard_host(self) -> bool:
        """Refuse requests whose Host isn't this server (a DNS-rebinding page could otherwise READ local files)."""
        port = self.server.server_address[1]
        host = (self.headers.get("Host") or "").lower()
        if host not in (f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"):
            self._error("Forbidden", 403)
            return False
        return True

    def _guard_origin(self) -> bool:
        """Block cross-site requests from other web pages (only this page may call the API): an Origin, when there is
        one, must be this server's own, scheme, host and port (1.1 hardening: as the home-network gate compares it).
        A request with no Origin (the tray, the command line) isn't a web page's."""
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        port = self.server.server_address[1]
        if origin.rstrip("/").lower() not in (f"http://127.0.0.1:{port}", f"http://localhost:{port}", f"http://[::1]:{port}"):
            self._error("Forbidden", 403)
            return False
        return True

    def version_string(self) -> str:
        """The Server header: the away-from-home listener (what the internet reaches) names nothing."""
        return "web" if getattr(self.server, "away", False) else super().version_string()

    # Phone access (phase 11)
    remote = False   # the request came from the network (a paired phone), not from this PC
    device = None    # that phone ({id, name, hash, ...})

    def _gate(self, method: str) -> bool:
        """Who's asking, and may they? From this PC (the loopback listener, a loopback address): exactly the checks from
        before phase 11. From the network: Phone access on, this server's own address in Host (DNS rebinding), a
        same-origin Origin on anything but GET, then app/phone_api.ROUTES: "open" routes (pairing) need nothing more,
        the rest a paired device's token, and "pc" routes are refused for phones."""
        self._read, self.set_cookie, self.device = None, None, None
        self.remote = getattr(self.server, "phone", False) or not _loopback(self.client_address[0])
        self.away = getattr(self.server, "away", False)
        if not self.remote:
            return self._guard_host() and (method == "GET" or self._guard_origin())
        if self.away:
            return self._gate_away(method)
        if not phone.enabled():
            return self._deny(401, b"Phone access is off on the PC. Switch it on there: SafeHouse, Settings, Phone access.")
        own, port = self.server.server_address[:2]
        if own in ("0.0.0.0", ""):   # [ui] host set to every address by hand: the home-network one is ours
            own = phone.lan_address()
        host = (self.headers.get("Host") or "").lower()
        if not own or host != f"{own}:{port}":
            return self._deny(403)
        if method != "GET":
            o = urlparse(self.headers.get("Origin") or "")
            if o.scheme != "http" or o.netloc.lower() != host:
                return self._deny(403)
        pol, what = phone_api.policy(method, urlparse(self.path).path)
        if pol == "open":
            return True
        self.device = phone.device_for(self._cookie(phone.COOKIE), self.client_address[0])
        if self.device is None:
            return self._deny(401)
        if pol == "pc":
            return self._deny(403, json.dumps({"error": f"That needs the PC ({what}): it can't be done from a phone."}).encode(),
                              "application/json")
        return True

    def _gate_away(self, method: str) -> bool:
        """The away-from-home listener (phase 20): what Tailscale Funnel brings in from the whole internet. Its own
        ts.net name in Host, https:// that name as Origin on anything but GET, never the pairing page or its claim
        (pairing stays on the home Wi-Fi), the redeem route and the page around it open, everything else an away token
        (cookie uc_away, a token of its own) and the phone policy (PC-only routes refused)."""
        from ..core import tailnet
        # (1.1 hardening: what the internet can reach without a token says nothing about the app or its settings)
        if not phone.enabled() or not phone.away_on() or not tailnet.STATE["host"]:
            return self._deny(401, AWAY_401)
        host = (self.headers.get("Host") or "").lower()
        if host != tailnet.STATE["host"].lower():
            return self._deny(403, AWAY_403)
        if method != "GET":
            o = urlparse(self.headers.get("Origin") or "")
            if o.scheme != "https" or o.netloc.lower() != host:
                return self._deny(403, AWAY_403)
        p = urlparse(self.path).path
        if p in ("/pair", "/api/phone/claim"):
            return self._deny(403, AWAY_403)
        if (method, p) in (("GET", "/away"), ("POST", "/api/phone/away")):
            return True
        pol, what = phone_api.policy(method, p)
        if pol == "open":
            return True
        self.device = phone.device_for(self._cookie(phone.AWAY_COOKIE), "", away=True)   # Funnel's loopback: no address to keep
        if self.device is None:
            return self._deny(401, AWAY_401)
        if pol == "pc":
            return self._deny(403, json.dumps({"error": f"That needs the PC ({what}): it can't be done from a phone."}).encode(),
                              "application/json")
        return True

    def _deny(self, status: int, body: bytes | None = None, ctype: str = "text/plain; charset=utf-8") -> bool:
        """401 / 403 and nothing else: no page, no data, the connection closed (the body is never read)."""
        if body is None:
            body = (b"This phone isn't paired with UnderCover. On the PC: SafeHouse, Settings, Phone access."
                    if status == 401 else b"Forbidden")
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        return False

    def _cookie(self, name: str) -> str | None:
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == name:
                return v
        return None

    def _other_method(self):
        if getattr(self.server, "phone", False) or not _loopback(self.client_address[0]):
            return self._deny(401, AWAY_401 if getattr(self.server, "away", False) else None)
        self.send_error(HTTPStatus.NOT_IMPLEMENTED, f"Unsupported method ({self.command!r})")

    do_HEAD = do_PUT = do_PATCH = do_OPTIONS = do_TRACE = do_CONNECT = _other_method

    # routing
    def do_GET(self):
        if not self._gate("GET"):   # also resets the last request's body: a keep-alive connection reuses this handler
            return
        u = urlparse(self.path)
        p, q = u.path, parse_qs(u.query)
        try:
            if p == "/api/work":
                from ..core import worklist
                return self._json(worklist.status())
            if (train_api.handle(self, "GET", p) or safehouse_api.handle(self, "GET", p)
                    or frontman_api.handle(self, "GET", p) or speakeasy_api.handle(self, "GET", p)
                    or doctor_api.handle(self, "GET", p) or phone_api.handle(self, "GET", p)
                    or themes_api.handle(self, "GET", p) or ear_api.handle(self, "GET", p)
                    or vault_api.handle(self, "GET", p) or gamepause_api.handle(self, "GET", p)):
                return
            if p in ("/", "/index.html"):
                return self._static(WEB / "index.html")
            if p == "/away":
                return self._static(WEB / "away.html")
            if p == "/pair":
                return self._static(WEB / "pair.html")
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
                        "start": w["start"], "start_at": w["settings"].get("start_at"),
                        "waits_until": w.get("waits_until"), "waits_for": w.get("waits_for")}   # B3: the strip says the wait
                from ..core import addons, gamepause, lowprofile
                ga = gamepause.STATE.get("asking")   # the game pause's open question: every page and a paired phone show it
                work["game_ask"] = {**ga, "reason": gamepause.STATE.get("reason") or ""} if ga else None
                return self._json({**STATUS, "queue": GPU_QUEUE.qsize(), "stale": code_stamp() != STARTUP_STAMP,
                                   "auto_job": auto, "cover_job": cover, "training": training, "work": work,
                                   "ytdlp_updated": ytdlp_swapped(), "build": BUILD,
                                   "low_profile": lowprofile.status(),   # (absent = None: no card to share, the control hides)
                                   "tour": firstrun.tour_status(),   # the First assignment's spotlight tour (web/firstrun.js)
                                   "client": "phone" if self.remote else "pc", "device": self.device and self.device["name"],
                                   "away_ready": self.remote and _away_ready(),
                                   # which install answers (phase 25: setup's yt-dlp step waits only for its own server)
                                   "app_folder": None if self.remote else str(ROOT)})
            if p == "/api/voices":
                return self._json(voices_json())
            m = re.fullmatch(r"/api/voices/([a-z0-9-]+)/preview", p)
            if m:   # its "Go undercover as ..." line (core/voicepreview), made with the voice
                from ..core import voicepreview
                from ..core.voices import ModelError as _ME, get_voice as _gv
                try:
                    v = _gv(m.group(1))
                except _ME:
                    return self._error("Not found", 404)
                if not voicepreview.fresh(v):
                    return self._error("Its preview line isn't made yet.", 404)
                return self._send_file(voicepreview.clip(v), cache="no-store")
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
        if not self._gate("POST"):
            return
        p = urlparse(self.path).path
        before = set(JOBS)
        jobs.ORIGIN.h = self   # jobs made while answering this request remember it (a concurrent request's don't)
        try:
            m = re.fullmatch(r"/api/jobs/(\w+)/retry", p)
            if m:
                return self._retry(m.group(1))
            return self._route_post(p)
        except BodyTooLarge as e:
            self._error(str(e), 413)
        except LookupError as e:
            self._error(str(e), 404)
        except Exception as e:
            self._error(friendly(e), 500)
        finally:
            jobs.ORIGIN.h = None
            self._remember_request(p, before)

    def _route_post(self, p: str):
        if p.startswith("/api/work/"):
            return self._work(p[len("/api/work/"):], self._body())
        if (train_api.handle(self, "POST", p) or safehouse_api.handle(self, "POST", p)
                or frontman_api.handle(self, "POST", p) or speakeasy_api.handle(self, "POST", p)
                or doctor_api.handle(self, "POST", p) or phone_api.handle(self, "POST", p)
                or themes_api.handle(self, "POST", p)
                or ear_api.handle(self, "POST", p) or vault_api.handle(self, "POST", p)
                or gamepause_api.handle(self, "POST", p) or lowprofile_api.handle(self, "POST", p)
                or firstrun_api.handle(self, "POST", p)):
            return
        if p == "/api/voices/install":
            return self._start_install(self._body())
        if p == "/api/voices/upload":
            return self._voice_upload()
        if p == "/api/open-folder":
            return self._open_folder(self._body())
        if p == "/api/restart":
            return self._restart()
        if p == "/api/quit":
            return self._quit()
        m = re.fullmatch(r"/api/jobs/(\w+)/cancel", p)
        if m:
            job = JOBS.get(m.group(1))
            if job and self.remote and _pc_only(job):
                return self._error(PC_ONLY_JOB, 403)
            if job:
                job.cancel = True
            return self._json({"ok": True})
        self._error("Not found", 404)

    # Retry (phase 10): a failed cover, speech, one-shot or voice install starts again from the request that started it
    RETRY_KINDS = ("cover", "speech", "auto", "install")

    def _remember_request(self, p: str, before: set):
        for jid in set(JOBS) - before:
            j = JOBS.get(jid)
            if j is not None and j.origin is self and j.kind in self.RETRY_KINDS and isinstance(self._read, dict):
                j.retry_req = (p, dict(self._read))
            if j is not None and j.origin is self:
                j.pc_only = phone_api.policy("POST", p)[0] == "pc"   # started by a PC-only route: the PC's to stop
                j.origin = None   # only needed until now: don't keep the request (and its connection) alive

    def _retry(self, job_id: str):
        job = JOBS.get(job_id)
        if job is None or not getattr(job, "retry_req", None):
            return self._error("That job can't be started again from here: start it from its module.", 404)
        if job.state != "error":
            return self._error("Only work that failed can be retried.")
        p, body = job.retry_req
        if self.remote and (_pc_only(job) or phone_api.policy("POST", p)[0] == "pc"):   # (the replay runs as the PC)
            return self._error(PC_ONLY_JOB, 403)
        r = _Replay(p, body)
        before = set(JOBS)
        jobs.ORIGIN.h = r
        try:
            r._route_post(p)
        except LookupError as e:
            return self._error(str(e), 404)
        finally:
            jobs.ORIGIN.h = self
        new = [JOBS[j] for j in set(JOBS) - before if j in JOBS and JOBS[j].origin is r]
        if r.status >= 400 or not new:
            return self._error((r.out or {}).get("error") or "It couldn't be started again.", r.status if r.status >= 400 else 409)
        for j in new:
            j.retry_req, j.origin, j.pc_only = (p, body), None, _pc_only(job)
        self._json({"ok": True, "job": new[0].id, "was": job_id})

    def do_DELETE(self):
        if not self._gate("DELETE"):
            return
        from ..core.voices import ModelError, remove_to_aside
        p = urlparse(self.path).path
        try:
            if (train_api.handle(self, "DELETE", p) or safehouse_api.handle(self, "DELETE", p)
                    or speakeasy_api.handle(self, "DELETE", p) or themes_api.handle(self, "DELETE", p)):
                return
            m = re.fullmatch(r"/api/voices/([a-z0-9-]+)", p)
            if not m:
                return self._error("Not found", 404)
            # set aside, not deleted (Internal Affairs P5-1): POST /api/library/aside/<aside>/restore brings it back
            r = remove_to_aside(m.group(1))
            gpu.forget_voice(m.group(1))
            if r.get("with"):
                gpu.forget_voice(r["with"])
            self._json({"ok": True, **r})
        except BodyTooLarge as e:
            self._error(str(e), 413)
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
        if f.suffix == ".html":
            self.send_header("Content-Security-Policy", csp(body))
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
        if self.remote and f.suffix.lower() not in AUDIO_EXT:   # a phone plays and downloads audio, nothing else (phase 11)
            return self._error("That file no longer exists.", 404)
        self._send_file(f, download)

    def _send_file(self, f: Path, download: bool = False, cache: str | None = None):
        """A file the caller has already checked, with HTTP Range (audio seeks) and an optional Cache-Control."""
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
        if cache:
            self.send_header("Cache-Control", cache)
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
            if self.remote and not phone.still_valid(self.device["hash"]):   # revoked, or Phone access switched off
                self.close_connection = True
                return
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
            if action == "to-queue":   # "Train tonight instead": a held entry becomes an overnight queue item
                return self._json(worklist.to_queue(str(b.get("id") or "")))
            if action == "forget":   # Dismiss the paused work instead of resuming it
                return self._json(worklist.forget_paused())
            if action == "add":
                # a project queued from the Deep path names it as "project" (the target is the project's id)
                return self._json(worklist.add(b.get("kind", ""), b.get("target") or b.get("project") or "", now=bool(b.get("now")),
                                               mode=b.get("mode") or None,
                                               options={k: b[k] for k in ("voice_name", "length", "sr") if k in b}))
            if action == "remove":
                return self._json(worklist.remove(b.get("id", "")))
            if action == "estimate":   # {kind, target, mode}: the one "how long" every page shows (P8-10)
                return self._json(worklist.estimate_for(b.get("kind", ""), b.get("target") or "", b.get("mode") or None))
            if action == "order":
                return self._json(worklist.reorder(b.get("ids") or []))
            if action == "start":   # {"at": "23:00"} or {} = now; {"stop": true} = don't start more
                return self._json(worklist.arm(False if b.get("stop") else (b.get("at") or None)))
            if action == "settings":
                return self._json(worklist.settings(**{k: b[k] for k in ("finish_by", "keep_awake", "start_at") if k in b}))
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
            last, seen = [0.0], []

            def progress(done, total):
                if job.cancel:
                    raise Cancelled()
                if progress.file not in seen:
                    seen.append(progress.file)
                now = time.time()
                if now - last[0] > 0.25 or (total and done >= total):
                    last[0] = now
                    # each file is its own round, named (1.0.1: the button counted 0-100 % twice, the second time
                    # for the voice's index, with no word for it)
                    part = ("index" if progress.file.lower().endswith(".index") else "file") if len(seen) > 1 else ""
                    what = {"index": "its index", "file": "its next file"}.get(part)
                    job.push(stage="downloading", frac=round(done / total, 3) if total else None, part=part,
                             message=f"Downloading {what + ': ' if what else ''}{done / 1e6:.0f} MB"
                                     + (f" of {total / 1e6:.0f} MB" if total else ""))
            progress.file = ""   # (set by the download for each file it fetches)
            try:
                with job.cond:
                    job.state = "running"
                from ..core import voicepreview
                from ..core.voices import download_staged, slug, voices_dir
                if name and (voices_dir() / slug(name)).exists():   # (before the download, as a direct install said it)
                    raise ModelError(f"A voice called '{slug(name)}' is already installed. Pick another name or remove it first.")
                v = voicepreview.import_voice(lambda: download_staged(src, name, progress), job)
                from ..core.voices import display_name
                job.finish("done", message=f"Installed {display_name(v)}", voice=v.name)
            except Cancelled:
                job.finish("cancelled", message="Cancelled.")
            except ModelError as e:
                job.finish("error", message=str(e))
            except Exception as e:
                job.finish("error", message=friendly(e, "installing the voice"))

        threading.Thread(target=run, daemon=True).start()  # network-bound; doesn't need the GPU thread
        self._json({"job": job.id})

    def _restart(self):
        """Restart the server process to load updated code. {"now": true}: stop whatever is running (Stop, not Pause:
        the user's "the stop has to be a stop") and restart at once. Without it, a running cover is refused with what's
        running and "now_ok" (the page offers "Restart now (stops running work)"); voice work pauses first as before."""
        now = bool(self._body().get("now"))
        running = [j for j in JOBS.values() if not j.finished]
        if now and running:
            for j in running:
                j.cancel = True
                if j.control is not None:
                    j.control.stop = True
            try:
                from ..doubleagent.api import ACTIVE
                for a in list(ACTIVE.values()):
                    if a.get("control") is not None:
                        a["control"].stop = True
            except Exception:   # noqa: BLE001 - the restart ends them anyway
                pass
            self._json({"ok": True, "stopped": [j.title or j.kind for j in running]})
            RESTART.set()
            threading.Thread(target=SERVER.shutdown, daemon=True).start()
            return
        if any(j.kind == "cover" and not j.finished for j in JOBS.values()):
            return self._json({"error": "A cover is still being made. Wait for it to finish, or restart now (it stops).",
                               "busy": [j.title or j.kind for j in running], "now_ok": True}, 409)
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
            return self._json({"ok": True, "pausing": True, "busy": [j.title or j.kind for j in running], "now_ok": True})
        self._json({"ok": True})
        RESTART.set()
        threading.Thread(target=SERVER.shutdown, daemon=True).start()  # makes serve_forever() return in main()

    def _quit(self):
        """Stop UnderCover (there may be no window to close when it was started by double-click). Refused while a
        cover or speech is being made; voice work is paused first (it keeps its place for Resume next time)."""
        if any(j.kind in ("cover", "preview", "speech", "auto") and not j.finished for j in JOBS.values()):
            return self._error("Something is still being made. Wait for it to finish (or cancel it), then quit.", 409)
        from ..core import worklist

        def stop():
            if worklist.active():
                worklist.pause_all()
                t0 = time.time()
                while worklist.active() and time.time() - t0 < 180:
                    time.sleep(1)
            SERVER.shutdown()   # serve_forever() returns in main(), which exits
        self._json({"ok": True, "pausing": worklist.active()})
        threading.Thread(target=stop, daemon=True).start()

    def _voice_upload(self):
        """Install a voice from a dropped .ucvoice/.zip/.pth file (body = raw file bytes)."""
        import tempfile
        from ..core.voices import ModelError, _guess_name, install
        n = int(self.headers.get("Content-Length") or 0)
        fname = Path(unquote(self.headers.get("X-Filename") or "voice.zip")).name
        if Path(fname).suffix.lower() not in (".ucvoice", ".zip", ".pth"):
            return self._error("Drop a voice as a .ucvoice voice pack, or a model as a .zip or .pth file.")
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
            try:   # staged, its preview line made there, then listed (rc-preview)
                from ..core import voicepack, voicepreview
                from ..core.voices import staged_name, staging_dir
                if f.suffix.lower() == voicepack.SUFFIX:   # (a voice pack: its manifest, sizes and SHA-256 first, 1.1)
                    v = voicepack.import_pack(f, wait=20.0)
                else:
                    v = voicepreview.import_voice(lambda: install(f, staged_name(name), source=f"file: {fname}", root=staging_dir()),
                                                  wait=20.0)   # (this request waits: past that the backfill makes it)
            except ModelError as e:
                return self._error(str(e))
            self._json({"voice": v.name})
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _open_folder(self, body: dict):
        """Show a cover in the file manager (by history id), or open the output folder (core/platform)."""
        from ..frontman.pipeline import history
        hid = body.get("history_id")
        item = next((h for h in history() if h["id"] == hid), None) if isinstance(hid, str) else None
        vdir = path("voices_dir") / str(body.get("voice") or "")
        if body.get("voice") and re.fullmatch(r"[a-z0-9-]+", body["voice"]) and vdir.is_dir():
            platform.open_folder(vdir)
        elif item:
            platform.show_in_folder(item["output"])
        else:
            platform.open_folder(path("output_dir"))
        self._json({"ok": True})


def csp(html: bytes) -> str:
    """The pages' Content-Security-Policy (phase 14, for themes): scripts, styles, fonts and everything else from this
    origin only. Inline scripts are allowed by their hash (the head's theme script, the pairing page), inline styles
    because the pages use style attributes and the theme maker previews through a <style>; data: images for the favicon
    and the CSS masks. A theme's css can't fetch anything from elsewhere, even if a check missed a url()."""
    import base64
    import hashlib
    page = html.replace(b"\r\n", b"\n").replace(b"\r", b"\n")   # the HTML parser turns CRLF into LF before hashing
    hashes = " ".join(f"'sha256-{base64.b64encode(hashlib.sha256(m.group(1)).digest()).decode()}'"
                      for m in re.finditer(rb"<script>(.*?)</script>", page, re.S))
    return ("default-src 'self'; script-src 'self'" + (f" {hashes}" if hashes else "") + "; style-src 'self' 'unsafe-inline'; "
            "font-src 'self'; img-src 'self' data:; media-src 'self'; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


class _Replay(Handler):
    """A request played again without a connection (Retry): the same routes, the answer kept instead of sent."""
    def __init__(self, p: str, body: dict):   # noqa: D107 - no socket, so BaseHTTPRequestHandler's own setup is skipped
        self.path, self._read, self.out, self.status = p, dict(body), None, 200
        self.headers = {"Content-Length": "0"}

    def _json(self, obj, status=200):
        self.out, self.status = obj, status

    def _error(self, message, status=400):
        self.out, self.status = {"error": message}, status


class ExclusiveServer(ThreadingHTTPServer):
    """On Windows, Python's default SO_REUSEADDR lets a SECOND server bind the same port, so two copies of
    UnderCover would answer requests at random. Bind exclusively so a second launch detects the first."""
    allow_reuse_address = False

    def _exclusive(self):
        platform.exclusive_bind(self.socket)

    def server_bind(self):
        self._exclusive()
        super().server_bind()


class PhoneServer(ExclusiveServer):
    """The second listener, on the home-network address, only while Phone access is on (app/phone_api.py)."""
    phone = True   # every request it answers comes from the network
    daemon_threads = True

    def server_bind(self):
        """As ExclusiveServer, without HTTPServer's reverse DNS lookup of the address (getfqdn: ~5 s for an address
        with no name, so switching Phone access on hung that long)."""
        import socketserver
        self._exclusive()
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


def _away_ready() -> bool:
    """For a phone: is away from home on and ready on the PC (its 'Use this phone away from home' shows only then)."""
    from ..core import tailnet
    return phone.away_on() and tailnet.STATE["phase"] == "ready"


class AwayServer(PhoneServer):
    """The third listener (phase 20): loopback only, the one target of Tailscale Funnel; every request comes from the
    internet and passes _gate_away."""
    away = True


def _loopback(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return ip.is_loopback or bool(getattr(ip, "ipv4_mapped", None) and ip.ipv4_mapped.is_loopback)


def _clean_old_previews(max_age_h: float = 24):
    d = path("cache_dir") / "previews"
    if d.exists():
        for sub in d.iterdir():
            if sub.is_dir() and time.time() - sub.stat().st_mtime > max_age_h * 3600:
                shutil.rmtree(sub, ignore_errors=True)


def who_has_port(host: str, port: int, timeout: float = 2.0) -> dict:
    """The port UnderCover wants is taken: by whom. Asks the holder's /api/status, which an UnderCover answers with its
    app_folder. -> {"kind": "this"} (this same copy: open it), {"kind": "other", "folder"} (another copy of UnderCover:
    never open it as if it were this one) or {"kind": "foreign", "owner"} (another program; its name when the OS says)."""
    import urllib.request
    probe = "127.0.0.1" if host in ("", "0.0.0.0", "::") else host
    st = None
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # never through a proxy
        with opener.open(f"http://{probe}:{port}/api/status", timeout=timeout) as r:
            st = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:   # noqa: BLE001 - not answering, or not with JSON: not an UnderCover
        st = None
    if isinstance(st, dict) and "build" in st:
        folder = st.get("app_folder")
        if folder and os.path.normcase(os.path.realpath(folder)) == os.path.normcase(os.path.realpath(str(ROOT))):
            return {"kind": "this"}
        return {"kind": "other", "folder": folder or ""}
    owner = None
    try:
        owner = platform.port_owner(port)
    except Exception:   # noqa: BLE001 - NotOnThisOS, or a tool that failed: the page doesn't name it
        owner = None
    return {"kind": "foreign", "owner": owner}


def port_taken_text(port: int, who: dict) -> tuple[str, str]:
    """(the headline, what to do) for a port another copy or another program holds: plain words, for the page and log."""
    if who["kind"] == "other":
        where = f" ({who['folder']})" if who.get("folder") else ""
        return (f"Another copy of UnderCover is already running{where}.",
                "Only one copy can run at a time. Quit that one first (in its page: SafeHouse, then Quit UnderCover; or "
                "from its tray icon), then start this one again. Or open that one instead.")
    owner = f" ({who['owner']})" if who.get("owner") else ""
    return (f"Another program{owner} is using port {port}, the one UnderCover needs.",
            "Close that program, or restart the PC, then start UnderCover again. If it keeps happening, UnderCover can "
            "use another port: change port in the [ui] part of config.toml in the UnderCover folder.")


def port_taken_page(port: int, who: dict) -> str:
    """The small page a start opens instead of someone else's: what's going on and what to do."""
    import html
    head, advice = port_taken_text(port, who)
    link = (f'<p><a class="b" href="http://127.0.0.1:{port}/">Open that copy</a></p>' if who["kind"] == "other" else "")
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><title>UnderCover can't start</title>"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"><style>"
            ":root{color-scheme:light dark;--bg:#14161a;--fg:#e8e6e1;--mute:#9a978f;--acc:#c9a227}"
            "@media (prefers-color-scheme: light){:root{--bg:#f6f4ef;--fg:#1d1c1a;--mute:#5d5a54;--acc:#8a6d12}}"
            "body{background:var(--bg);color:var(--fg);font:16px/1.55 system-ui,sans-serif;margin:0;padding:12vh 20px}"
            "main{max-width:34rem;margin:auto}h1{font-size:1.35rem;margin:0 0 .3rem}.k{color:var(--mute);margin:0 0 1.4rem}"
            ".b{display:inline-block;padding:.55rem 1rem;border:1px solid var(--acc);color:var(--acc);border-radius:6px;"
            "text-decoration:none}</style></head><body><main>"
            "<p class=\"k\">UnderCover couldn't take its line.</p>"
            f"<h1>{html.escape(head)}</h1><p>{html.escape(advice)}</p>{link}</main></body></html>")


def _say_port_taken(host: str, port: int):
    """A start that can't bind: this same copy already running is opened; anything else is said, never opened as if it
    were this copy (a fresh install once opened the user's everyday copy, and they thought the new one was broken)."""
    url = f"http://{host}:{port}/"
    who = who_has_port(host, port)
    if who["kind"] == "this":
        print(f"UnderCover is already running at {url}. Opening it.", flush=True)
        if UCFG["open_browser"]:
            webbrowser.open(url)
        return
    head, advice = port_taken_text(port, who)
    print(f"{head} {advice}", flush=True)
    if UCFG["open_browser"] and "--no-browser" not in sys.argv:
        try:
            page = path("logs_dir") / "cant-start.html"
            page.parent.mkdir(parents=True, exist_ok=True)
            page.write_text(port_taken_page(port, who), encoding="utf-8")
            webbrowser.open(page.as_uri())
        except OSError as e:
            print(f"The page saying so couldn't be opened: {e}", flush=True)


def _relaunch(extra_args: list[str]):
    """Hand off to undercover/core/relaunch.py, which waits for THIS process to exit before starting a fresh,
    windowless server (output in logs/server.log). Starting the new server while this one is still releasing
    the GPU leaves this process stuck in the driver."""
    platform.start_detached([sys.executable, "-m", "undercover.core.relaunch", str(os.getpid()),
                             "--no-browser", "--restarted", *extra_args], cwd=ROOT)


def main():
    global SERVER
    try:   # the app folder may have been moved or renamed: fix stored paths before anything reads them
        from ..core import relocate
        relocate.check_and_fix()
    except Exception as e:   # noqa: BLE001 - never block startup
        print(f"Couldn't check the app folder location: {e}", flush=True)
    try:   # voices trained here learn whose they are once (extra.person; each meta.json is copied first)
        from ..core import voices as voices_mod
        voices_mod.backfill_person()
    except Exception as e:   # noqa: BLE001
        print(f"Couldn't add the person to the trained voices: {e}", flush=True)
    try:   # the yt-dlp pair to use (phase 16): a side pair goes first on sys.path before anything imports yt_dlp
        from ..core import ytdlp
        p = ytdlp.activate()
        print(f"YouTube downloader: yt-dlp {p['yt']} + solver {p.get('ejs') or 'missing'} ({p['source']})", flush=True)
    except Exception as e:   # noqa: BLE001 - never block startup: the venv's pair is used
        print(f"Couldn't choose the YouTube downloader: {e}", flush=True)
    try:   # the first run: once per install, an existing one is marked, a fresh one gets the sample song and Agent Smoke
        firstrun.first_run()
    except Exception as e:   # noqa: BLE001 - never block startup
        print(f"Couldn't set up the first run: {e}", flush=True)
    try:   # covers get permanent ids once (a copy of the old history file is kept next to it)
        from ..frontman import history as cover_history
        cover_history.migrate()
    except Exception as e:   # noqa: BLE001
        print(f"Couldn't check the cover history: {e}", flush=True)
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
        _say_port_taken(host, port)
        return
    srv.daemon_threads = True
    SERVER = srv
    jobs.PORT = srv.server_address[1]
    try:   # the Doctor's startup check and its safe steps (< 2 s; downloads and the first install record in the background)
        from ..core import doctor, storage
        hb = path("logs_dir") / ("heartbeat.jsonl" if jobs.PORT == UCFG["port"] else f"heartbeat-{jobs.PORT}.jsonl")
        doctor.heal_at_startup(hb, STATUS, hash_new_manifest=storage.ALLOWED_ROOTS is None)
    except Exception as e:   # noqa: BLE001 - never block startup
        print(f"The Doctor's startup check failed: {e}", flush=True)
    try:   # candidates a check left in voices\.staging when the app stopped mid-check (Storage counted them): only inside
        # .staging, through storage.remove_files, once the port is ours; only ones staged hours ago (a check takes
        # minutes), so a server on another port with the same folders keeps the one it's checking
        from ..core import voices as voices_mod
        voices_mod.discard_staged(older_than=6 * 3600)
    except Exception as e:   # noqa: BLE001 - never block startup
        print(f"Couldn't clear the voice check's leftovers: {e}", flush=True)
    _install_console_handler()
    _clean_old_previews()
    threading.Thread(target=gpu_worker, daemon=True, name="gpu").start()
    from ..core import vault, voicepreview, worklist
    from ..speakeasy import speak as speak_mod
    voicepreview.RENDER[0] = speak_mod.render_line   # (core and DoubleAgent add voices; only the app may reach SpeakEasy)
    from ..core import voicepack
    from ..doubleagent import service as da_service
    voicepack.PRINT[0] = da_service.person_print   # (a pack's voice print, when the exporter ticks it: DoubleAgent's)
    voicepreview.start()   # voices from before get their preview line, one at a time, only while nothing else runs
    speak_mod.start_warm_up()   # the speech engine ready before the first speech, once idle (1.0.1)
    worklist.start()
    vault.start()   # Vaulting: quiet backups when the GPU queue is idle (off until a vault folder is picked)
    try:   # phase 24: the full indexes kept 30 days after an export go when their time is up (daily)
        from ..safehouse import smaller
        smaller.start_expiry()
    except Exception as e:   # noqa: BLE001 - never block startup
        print(f"The kept full indexes couldn't be checked: {e}", flush=True)
    from ..core import gamepause
    gamepause.start()   # the game pause: asks before UnderCover's GPU work competes with a game
    threading.Thread(target=heartbeat_loop, daemon=True, name="heartbeat").start()
    try:   # Phone access (phase 11): the second listener, if it's switched on
        phone_api.setup(lambda a, p: PhoneServer((a, p), Handler), port, lambda a, p: AwayServer((a, p), Handler))
    except Exception as e:   # noqa: BLE001 - never block startup
        print(f"Phone access couldn't start: {e}", flush=True)
    _start_tray(port)
    url = f"http://{host}:{port}/"
    print(f"UnderCover is running at {url}\nKeep this window open while you use it. Press Ctrl+C to stop.")
    if phone_api.url():
        print(f"Phone access is on: {phone_api.url()}/ (paired phones only)", flush=True)
    if UCFG["open_browser"] and "--no-browser" not in sys.argv:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.", flush=True)
        _hb_write({"event": "stop"})
        _hard_exit()   # a normal exit can hang forever in the GPU runtime's DLL teardown
    if RESTART.is_set():
        _hb_write({"event": "stop", "reason": "restart"})
        srv.server_close()  # free the port for the new process
        _relaunch(["--port", str(port)] if "--port" in sys.argv else [])
        print("Restarting UnderCover...", flush=True)
        _hard_exit()
    print("UnderCover stopped (Quit).", flush=True)   # /api/quit
    _hb_write({"event": "stop", "reason": "quit"})
    _hard_exit()


def _start_tray(port: int):
    """The tray icon (phase 18): its own light process, for the user's server only (never a sandbox or test server, not
    with --no-tray or [ui] tray = false). A restart starts it again; a second one just exits (one per user)."""
    from ..core import storage
    if ("--no-tray" in sys.argv or not UCFG.get("tray", True) or port != UCFG["port"]
            or storage.ALLOWED_ROOTS is not None or not platform.supports("tray")[0]):
        return
    try:
        platform.start_detached([platform.windowless_python(), "-m", "undercover.tray"], cwd=ROOT)
    except OSError as e:
        print(f"The tray icon couldn't start: {e}", flush=True)


def _install_console_handler():
    """Closing the UnderCover window (or logging off / shutting down) skips Python's Ctrl+C path: catch it, write the
    heartbeat's clean "stop" and exit at once (a normal exit can hang in the GPU driver). core/platform does the OS part."""
    def closing(reason: str):
        _hb_write({"event": "stop", "reason": reason})
        _hard_exit()
    platform.on_console_close(closing)


def _hard_exit():
    """End this process immediately (core/platform.hard_exit: a normal exit can hang in the GPU runtime's teardown).
    Away from home's Tailscale daemon is ours: it goes too (a restart starts it again)."""
    try:
        from ..core import tailnet
        tailnet.at_exit()
    except Exception:   # noqa: BLE001 - exiting regardless
        pass
    platform.hard_exit(0)


if __name__ == "__main__":
    main()
