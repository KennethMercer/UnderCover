"""Background voice work: Pause / Resume for everything that trains, and the overnight queue.

Pause (top of the page) stops all voice work cleanly: a training saves its resume point, preparing clips stops
after the current one, a one-shot that's training a voice is set aside. What was paused is remembered (also across
a restart) and Resume starts it again in the same order. Covers aren't paused: they're short and always go first.

The overnight queue runs items one after another when the GPU is free, optionally from a start time and only if an
item can finish before a "finish by" time. The kinds of items, how paused work is resumed and which job kinds Pause
stops are registered by the modules (DoubleAgent's improve / new voice items: doubleagent/night.py).
"""
import json
import threading
import time
import uuid
from datetime import datetime, timedelta

from . import jobs
from .config import CONFIG, path
from .voices import ModelError

_LOCK = threading.RLock()
_STATE: dict | None = None

# ---------------------------------------------------------------- what the modules register

KINDS: dict = {}          # queue item kind -> {"run", "estimate", "target", "label"}
RESUMERS: dict = {}       # resume spec kind -> start(spec) -> Job
PAUSABLE: set = {"night"}   # job kinds that Pause stops (and that count as voice work)
PARTLY_PAUSABLE: set = set()   # job kinds that keep running when held, but set their voice training aside
CONTROL_LOOKUPS: list = []    # job -> trainer Control (for jobs that don't carry their own job.control)


def register_kind(kind: str, run, estimate, target=lambda t: t, label=lambda item: item["target"]):
    """A kind of queue item. run(job, item) -> result dict; estimate(item) -> minutes per step and "total";
    target(text) -> the normalised target (raise ModelError if it's not valid); label(item) -> what the job shows."""
    KINDS[kind] = {"run": run, "estimate": estimate, "target": target, "label": label}


def register_resume(kind: str, start):
    RESUMERS[kind] = start


def register_pausable(*kinds: str, partly: bool = False):
    PAUSABLE.update(kinds)
    if partly:
        PARTLY_PAUSABLE.update(kinds)


def register_control_lookup(fn):
    CONTROL_LOOKUPS.append(fn)


def estimate(item: dict) -> dict:
    return KINDS[item["kind"]]["estimate"](item)



def _file():
    """One file per server port, so a test server (7861) never touches the real queue or its pause."""
    port = jobs.PORT or CONFIG["ui"]["port"]
    return path("cache_dir") / ("worklist.json" if port == CONFIG["ui"]["port"] else f"worklist-{port}.json")


def _state() -> dict:
    global _STATE
    if _STATE is None:
        try:
            _STATE = json.loads(_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _STATE = {}
        _STATE.setdefault("held", False)
        _STATE.setdefault("resume", [])
        _STATE.setdefault("queue", [])
        _STATE.setdefault("armed", False)
        _STATE.setdefault("settings", {"start_at": None, "finish_by": "07:00", "keep_awake": True})
        _STATE.setdefault("report", None)
        for it in _STATE["queue"]:   # a crash or restart mid-item: run it again (it continues where it was)
            if it.get("state") == "running":
                it["state"] = "queued"
    return _STATE


def _save():
    from . import fsutil
    fsutil.write_text(_file(), json.dumps(_state(), indent=1))


# ---------------------------------------------------------------- pause / resume

def _log(msg: str):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] worklist: {msg}", flush=True)


def _voice_jobs():
    return [j for j in list(jobs.JOBS.values()) if j.kind in PAUSABLE and not j.finished]


def active() -> bool:
    """Voice work running or waiting in the GPU queue."""
    return bool(_voice_jobs())


def pause_all(resume_after_restart: bool = False) -> dict:
    """Stop all voice work cleanly and remember it for Resume (or, for a restart, resume it right after)."""
    with _LOCK:
        st = _state()
        st["held"] = True
        if resume_after_restart:
            st["resume_after_restart"] = True
        _save()
        _log(f"pause (restart={resume_after_restart}); running: {[j.kind for j in _voice_jobs()]}")
        for j in _voice_jobs():
            j.pausing = True
            ctl = j.control or next((c for c in (look(j) for look in CONTROL_LOOKUPS) if c is not None), None)
            if ctl is not None:
                ctl.pause = True
    return status()


def remember(job):
    """Called when a job ends as "paused": keep how to start it again."""
    spec = getattr(job, "resume_spec", None)
    if not spec:
        return
    with _LOCK:
        st = _state()
        if spec.get("kind") == "night":
            for it in st["queue"]:
                if it["id"] == spec.get("item"):
                    it["state"] = "paused"
        elif spec not in st["resume"]:
            st["resume"].append(spec)
        _save()
        _log(f"remembered {spec.get('label')}; held={st['held']} resume={len(st['resume'])}")


def resume_all() -> dict:
    """Start everything Pause stopped, in the same order, and let the queue run again."""
    with _LOCK:
        st = _state()
        st["held"] = False
        specs, st["resume"] = st["resume"], []
        _log(f"resume {[x.get('label') for x in specs]}")
        for it in st["queue"]:
            if it.get("state") == "paused":
                it["state"] = "queued"
        _save()
    problems = []
    for spec in specs:
        try:
            _start(spec)
        except Exception as e:   # noqa: BLE001 - shown to the user, the rest still resume
            problems.append(f"{spec.get('label', spec['kind'])}: {e}")
    out = status()
    if problems:
        out["problems"] = problems
    return out


def _start(spec: dict):
    k = spec["kind"]
    if k in RESUMERS:
        return RESUMERS[k](spec)
    raise ModelError(f"Don't know how to resume '{k}'.")


def held() -> bool:
    return bool(_state().get("held"))


# ---------------------------------------------------------------- the overnight queue

def add(kind: str, target: str) -> dict:
    target = (target or "").strip()
    if kind not in KINDS:
        raise ModelError("Pick Improve a voice or Train a new voice.")
    if not target:
        raise ModelError("Type a voice or a person's name.")
    target = KINDS[kind]["target"](target)   # e.g. a voice that must exist (friendly error if it doesn't)
    item = {"id": uuid.uuid4().hex[:8], "kind": kind, "target": target, "state": "queued",
            "added": time.strftime("%Y-%m-%d %H:%M")}
    item["estimate"] = estimate(item)
    with _LOCK:
        _state()["queue"].append(item)
        _save()
    return status()


def remove(item_id: str) -> dict:
    with _LOCK:
        st = _state()
        it = next((i for i in st["queue"] if i["id"] == item_id), None)
        if it and it["state"] == "running":
            raise ModelError("That one is running. Pause first, then remove it.")
        st["queue"] = [i for i in st["queue"] if i["id"] != item_id]
        _save()
    return status()


def reorder(ids: list) -> dict:
    with _LOCK:
        st = _state()
        by = {i["id"]: i for i in st["queue"]}
        st["queue"] = [by[i] for i in ids if i in by] + [i for i in st["queue"] if i["id"] not in ids]
        _save()
    return status()


def arm(when: str | None) -> dict:
    """Start the queue now (when=None) or at "HH:MM"; arm(False) stops it from starting more."""
    with _LOCK:
        st = _state()
        if when is False:
            st["armed"] = False
        else:
            st["armed"] = True
            st["settings"]["start_at"] = when or None
            st["held"] = False
        _save()
    return status()


def settings(**kw) -> dict:
    with _LOCK:
        st = _state()
        for k in ("finish_by", "keep_awake"):
            if k in kw:
                st["settings"][k] = kw[k]
        _save()
    return status()


def dismiss_report() -> dict:
    with _LOCK:
        _state()["report"] = None
        _save()
    return status()


def status() -> dict:
    st = _state()
    now = datetime.now()
    running = []
    for j in _voice_jobs():
        ev = j.events[-1] if j.events else {}
        running.append({"job": j.id, "kind": j.kind, "label": (j.resume_spec or {}).get("label") or j.kind,
                        "message": ev.get("message") or ev.get("note") or ev.get("label") or ev.get("stage"),
                        "epoch": ev.get("epoch"), "epochs": ev.get("epochs"), "eta": ev.get("eta"),
                        "fraction": ev.get("fraction", ev.get("frac")), "pausing": j.pausing})
    left = [i for i in st["queue"] if i["state"] in ("queued", "paused", "running")]
    total = sum(i.get("estimate", {}).get("total", 0) for i in left)
    start = now
    if st["armed"] and st["settings"].get("start_at"):
        start = _next_time(st["settings"]["start_at"], now)
    return {"held": st["held"], "resume": [s.get("label", s["kind"]) for s in st["resume"]], "running": running,
            "queue": st["queue"], "armed": st["armed"], "settings": st["settings"], "report": st["report"],
            "total_min": total, "start": start.strftime("%H:%M") if left else None,
            "done_by": (start + timedelta(minutes=total)).strftime("%H:%M") if left else None}


def _next_time(hhmm: str, now: datetime) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    t = now.replace(hour=h, minute=m, second=0, microsecond=0)
    return t if t >= now - timedelta(minutes=1) else t + timedelta(days=1)


def _deadline(now: datetime) -> datetime | None:
    fb = _state()["settings"].get("finish_by")
    if not fb:
        return None
    d = _next_time(fb, now)
    return d


def _in_window(now: datetime) -> bool:
    st = _state()
    sa = st["settings"].get("start_at")
    if not sa:
        return True
    start = _next_time(sa, now)
    if start <= now + timedelta(minutes=1):
        return True
    # started earlier tonight and still before the deadline
    prev = start - timedelta(days=1)
    dl = _deadline(prev)
    return prev <= now and (dl is None or now < dl)


# ---------------------------------------------------------------- runner

def start():
    _state()
    threading.Thread(target=_loop, daemon=True, name="worklist").start()


def _gpu_idle() -> bool:
    return jobs.GPU_QUEUE.empty() and not any(not j.finished for j in list(jobs.JOBS.values()))


def _loop():
    awake = False
    while True:
        try:
            st = _state()
            if jobs.STATUS.get("models") == "ready" and (st.get("resume_after_restart") or (st["resume"] and not st["held"])):
                # paused only to restart (or paused work left without a hold): carry on
                with _LOCK:
                    st.pop("resume_after_restart", None)
                    _save()
                r = resume_all()
                if r.get("problems"):
                    _log(f"resume problems: {r['problems']}")
            busy = bool(_voice_jobs())
            want_awake = busy and st["settings"].get("keep_awake", True)
            if want_awake != awake:
                _keep_awake(want_awake)
                awake = want_awake
            if not st["held"] and st["armed"] and _gpu_idle() and not jobs.STATUS.get("gpu_problem"):
                now = datetime.now()
                nxt = next((i for i in st["queue"] if i["state"] == "queued" and i.get("retry_after", 0) <= time.time()), None)
                waiting = any(i["state"] == "queued" for i in st["queue"])
                if nxt is None:
                    if not waiting and any(i["state"] in ("done", "failed", "skipped") for i in st["queue"]):
                        with _LOCK:
                            st["armed"] = False   # queue finished
                            _save()
                elif _in_window(now):
                    dl = _deadline(now) if st["settings"].get("start_at") else None
                    nxt["estimate"] = estimate(nxt)
                    if dl is not None and now + timedelta(minutes=nxt["estimate"]["total"]) > dl:
                        pass   # wouldn't finish before the deadline: waits for the next night
                    else:
                        _run_item(nxt)
        except Exception as e:   # noqa: BLE001 - the loop must survive anything
            _log(f"loop error: {type(e).__name__}: {e}")
        time.sleep(10)


def _keep_awake(on: bool):
    """Windows: don't sleep while voice work runs (per-thread request; released when it ends)."""
    try:
        import ctypes
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))
    except Exception:   # noqa: BLE001 - not Windows
        pass


def _run_item(item: dict):
    job = jobs.Job("night")
    jobs.JOBS[job.id] = job
    label = KINDS[item["kind"]]["label"](item)
    job.resume_spec = {"kind": "night", "item": item["id"], "label": label}
    with _LOCK:
        item.update(state="running", job=job.id, started=item.get("started") or time.strftime("%H:%M"))
        st = _state()
        if not st["report"] or st["report"].get("closed"):
            st["report"] = {"started": time.strftime("%Y-%m-%d %H:%M"), "results": []}
        _save()

    def run(job):
        t0 = time.time()
        try:
            res = KINDS[item["kind"]]["run"](job, item)
            state = "done"
        except Exception as e:   # noqa: BLE001
            if type(e).__name__ == "Paused" or job.pausing:
                with _LOCK:
                    item["state"] = "paused"
                    _save()
                job.finish("paused", message="Paused. Resume continues where it stopped.")
                return
            if type(e).__name__ == "YouTubeBlocked":   # try again in an hour, not "failed"
                with _LOCK:
                    item.update(state="queued", retry_after=time.time() + 3600, note=str(e) + " Trying again in an hour.")
                    _save()
                job.finish("error", message=str(e) + " The queue tries again in an hour.")
                return
            if type(e).__name__ == "Cancelled":
                res, state = {"note": "cancelled"}, "skipped"
            else:
                res, state = {"note": jobs.friendly(e, "")}, "failed"
        mins = round((time.time() - t0) / 60)
        with _LOCK:
            item.update(state=state, finished=time.strftime("%H:%M"), result=res, minutes=mins)
            rep = _state()["report"]
            if rep is not None:
                rep["results"].append({"kind": item["kind"], "target": item["target"], "state": state, "minutes": mins, **res})
                rep["finished"] = time.strftime("%Y-%m-%d %H:%M")
            _save()
        job.finish("done" if state == "done" else "error", message=res.get("note") or "Done.", result=res)

    jobs.GPU_QUEUE.put((job, run))


def control_for(job):
    """A trainer Control for a queue item's job: Pause reaches it, and queued covers run between training steps."""
    control = jobs.Control()

    def service():
        while not jobs.GPU_QUEUE.empty():
            try:
                other, fn = jobs.GPU_QUEUE.get_nowait()
            except Exception:   # noqa: BLE001
                return
            jobs.run_job(other, fn)
    control.service = service
    job.control = control
    return control


def pusher(job):
    """push(stage, frac, message) for a queue item's job (raises Cancelled once it's cancelled)."""
    def push(stage, frac, message, **kw):
        if job.cancel:
            raise jobs.Cancelled()
        job.push(stage=stage, frac=round(frac, 3), message=message, **kw)
    return push


