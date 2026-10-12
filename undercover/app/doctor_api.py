"""The Doctor's routes (phase 10; the checks are core/doctor.py, the System room shows them):

    GET  /api/doctor                 fast checks (cached a few seconds) + the last full run, startup notes, the UnderCover row
    POST /api/doctor/full            a full run as a job (kind "doctor": every model file's checksum)
    GET  /api/doctor/fix/<id>        what a Fix would do, read only (the package Fix: each package and version)
    POST /api/doctor/fix/<id>        run a Fix: {"ok", "message"} or {"job"}
    GET  /api/doctor/bundle          what the diagnostics file would hold (shown before saving)
    GET  /api/doctor/bundle.zip      the diagnostics file
    POST /api/doctor/open-logs       open the logs folder in Explorer
"""
import os
import threading
import time
from urllib.parse import unquote

from ..core import diagnostics, doctor, jobs
from ..core.config import path
from ..core.jobs import JOBS, STATUS, Job
from ..core.voices import ModelError

_CACHE = {"at": 0.0, "rep": None}
_FULL = {"job": None}


def fast(max_age: float = 5.0) -> dict:
    if _CACHE["rep"] is None or time.time() - _CACHE["at"] > max_age:
        _CACHE["rep"] = doctor.run(False, STATUS)
        _CACHE["at"] = time.time()
    return _CACHE["rep"]


def handle(h, method: str, p: str) -> bool:
    if not p.startswith("/api/doctor"):
        return False
    try:
        if method == "GET" and p == "/api/doctor":
            rep = fast()
            full = doctor.last_full()
            j = _FULL["job"]
            h._json({**rep, "last_full": {k: full[k] for k in ("made", "counts", "problems", "seconds")} if full else None,
                     "full_job": j.id if j is not None and not j.finished else None,
                     "startup": doctor.LAST.get("startup"), "interrupted": doctor.LAST.get("interrupted")})
        elif method == "POST" and p == "/api/doctor/full":
            h._body()
            h._json({"job": _start_full().id})
        elif method == "GET" and p.startswith("/api/doctor/fix/"):
            h._json(doctor.fix_preview(unquote(p[len("/api/doctor/fix/"):])))
        elif method == "POST" and p.startswith("/api/doctor/fix/"):
            h._body()
            r = doctor.fix(unquote(p[len("/api/doctor/fix/"):]), STATUS)
            _CACHE["rep"] = None   # the next GET shows the result
            h._json(r)
        elif method == "GET" and p == "/api/doctor/bundle":
            h._json(diagnostics.contents(fast(0)))
        elif method == "GET" and p == "/api/doctor/bundle.zip":
            data = diagnostics.build(fast(0))
            name = f"UnderCover-diagnostics-{time.strftime('%Y%m%d-%H%M')}.zip"
            h.send_response(200)
            h.send_header("Content-Type", "application/zip")
            h.send_header("Content-Length", str(len(data)))
            h.send_header("Content-Disposition", f'attachment; filename="{name}"')
            h.send_header("Cache-Control", "no-store")
            h.end_headers()
            h.wfile.write(data)
            doctor.log(f"diagnostics file saved ({len(data) / 1e6:.1f} MB)")
        elif method == "POST" and p == "/api/doctor/open-logs":
            h._body()
            os.startfile(path("logs_dir"))
            h._json({"ok": True})
        else:
            raise LookupError("Not found")
    except ModelError as e:
        h._error(str(e))
    return True


def _start_full() -> Job:
    j = _FULL["job"]
    if j is not None and not j.finished:
        return j
    job = Job("doctor", title="Doctor: checking everything")
    JOBS[job.id] = job
    _FULL["job"] = job

    def go():
        with job.cond:
            job.state = "running"
        last = [0.0]

        def prog(msg, done, total):
            if time.time() - last[0] > 0.5:
                last[0] = time.time()
                job.push(stage="checking", frac=round(done / total, 3), message=f"{msg}: {done / 1e9:.1f} of {total / 1e9:.1f} GB")
        try:
            job.push(stage="checking", frac=0.0, message="Checking the model files against their checksums")
            rep = doctor.run(True, STATUS, prog, lambda: job.cancel)
            _CACHE["rep"] = None
            c = rep["counts"]
            n = c["bad"] + c["warn"]
            doctor.log(f"full check: {n} problem(s) in {rep['seconds']:.0f} s")
            job.finish("done", message="Everything checks out." if not n else f"Found {n} problem{'s' if n != 1 else ''}.",
                       counts=c)
        except InterruptedError:
            job.finish("cancelled", message="Cancelled.")
        except Exception as e:   # noqa: BLE001
            job.finish("error", message=jobs.friendly(e, "checking everything"))

    threading.Thread(target=go, daemon=True, name="doctor-full").start()
    return job
