"""Background voice work: Pause / Resume for everything that trains, and the overnight queue.

Pause (top of the page) stops all voice work cleanly: a training saves its resume point, preparing clips stops
after the current one, a one-shot that's training a voice is set aside. What was paused is remembered (also across
a restart) and Resume starts it again in the same order. Covers aren't paused: they're short and always go first.

The overnight queue runs items one after another when the GPU is free, optionally from a start time and only if an
item can finish before a "finish by" time. The kinds of items, how paused work is resumed and which job kinds Pause
stops are registered by the modules (DoubleAgent's improve / new voice items: doubleagent/night.py).
"""
import json
import re
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
QUEUE_AS: dict = {}       # resume spec kind -> spec -> (queue item kind, target) | None: "Train tonight instead"
PAUSABLE: set = {"night"}   # job kinds that Pause stops (and that count as voice work)
PARTLY_PAUSABLE: set = set()   # job kinds that keep running when held, but set their voice training aside
CONTROL_LOOKUPS: list = []    # job -> trainer Control (for jobs that don't carry their own job.control)


def register_kind(kind: str, run, estimate, target=lambda t: t, label=lambda item: item["target"], follows=None,
                  shown=lambda item: item["target"], modes: tuple = (), options=None, person=None, live=None):
    """A kind of queue item. run(job, item) -> result dict; estimate(item) -> minutes per step and "total";
    target(text) -> the normalised target (raise ModelError if it's not valid); label(item) -> what the job shows;
    follows(item, other) -> True when item must run after other (a voice's cadence after that voice's training);
    shown(item) -> the target as people read it (a voice's display name; the stored target stays the id);
    modes: the item's optional `mode` values this kind takes (a new voice: speaking / singing / both);
    options(target, body): the add request's own choices for this kind, checked and kept where the run reads them
    (a queued project's voice name, length, sample rate: what's on screen is what runs); raise ModelError to refuse;
    person(target) -> whose voice it makes (one queue item per person across these kinds); live(target) -> the job
    making that voice live right now, if any (adding it pauses that job and moves it to the queue instead)."""
    KINDS[kind] = {"run": run, "estimate": estimate, "target": target, "label": label, "follows": follows, "shown": shown,
                   "modes": tuple(modes), "options": options, "person": person, "live": live}


def register_resume(kind: str, start, queue_as=None):
    """start(spec) -> Job: Resume. queue_as(spec) -> (queue kind, target) | None: the same work as an overnight queue
    item instead ("Train tonight instead"); None = it can't run overnight."""
    RESUMERS[kind] = start
    if queue_as:
        QUEUE_AS[kind] = queue_as


_SPEC_EXTRA = ("id", "held_at")   # added to a held entry; not part of what the work is


def _same(a: dict, b: dict) -> bool:
    return {k: v for k, v in a.items() if k not in _SPEC_EXTRA} == {k: v for k, v in b.items() if k not in _SPEC_EXTRA}


def _held(spec: dict) -> dict:
    """A resume entry as kept: the spec plus an id (the page targets it) and when it was held."""
    return {**spec, "id": spec.get("id") or uuid.uuid4().hex[:8], "held_at": spec.get("held_at") or time.strftime("%Y-%m-%d %H:%M")}


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
        _STATE["resume"] = [_held(s) for s in _STATE["resume"]]   # entries saved before they had an id
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


def pause_all(resume_after_restart: bool = False, by: str = "you") -> dict:
    """Stop all voice work cleanly and remember it for Resume (or, for a restart, resume it right after). by: who held
    it ("you", or "game" for the game pause, which only resumes a hold that is still its own)."""
    with _LOCK:
        st = _state()
        st["held"] = True
        st["held_by"] = by
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


def hold_one(job, by: str = "you") -> dict:
    """Pause ONE job and hold, as pause_all does (Internal Affairs P2-2: DoubleAgent's project Pause set only the
    trainer's flag, so the queue loop started the work again 17 s later). The other running work isn't touched."""
    with _LOCK:
        st = _state()
        st["held"] = True
        st["held_by"] = by
        _save()
        _log(f"pause one: {job.kind} {(getattr(job, 'resume_spec', None) or {}).get('label')}")
        _pause_job(job)
    return status()


def _pause_job(job):
    """Ask one job to pause at its next step (its trainer's control too)."""
    job.pausing = True
    ctl = job.control or next((c for c in (look(job) for look in CONTROL_LOOKUPS) if c is not None), None)
    if ctl is not None:
        ctl.pause = True


def remember(job):
    """Called when a job ends as "paused": keep how to start it again."""
    spec = getattr(job, "resume_spec", None)
    if not spec:
        return
    if getattr(job, "then_queue", False) and spec.get("kind") in QUEUE_AS:   # paused to go to tonight's queue (add())
        try:   # straight to the queue: never on the resume list, where the loop could start it again live
            add(*QUEUE_AS[spec["kind"]](spec))
            _log(f"to the queue: {spec.get('label')}")
            return
        except ModelError as e:   # held instead, so it waits for the user (Train tonight instead / Resume)
            _log(f"couldn't move {spec.get('label')} to the queue: {e}")
            with _LOCK:
                _state()["held"] = True
                _save()
    with _LOCK:
        st = _state()
        if spec.get("kind") == "night":
            for it in st["queue"]:
                if it["id"] == spec.get("item"):
                    it["state"] = "paused"
        elif not any(_same(spec, s) for s in st["resume"]):
            st["resume"].append(_held(spec))
        _save()
        _log(f"remembered {spec.get('label')}; held={st['held']} resume={len(st['resume'])}")


def resume_all() -> dict:
    """Start everything Pause stopped, in the same order, and let the queue run again."""
    with _LOCK:
        st = _state()
        st["held"] = False
        st.pop("held_by", None)
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


def to_queue(entry_id: str) -> dict:
    """"Train tonight instead": a held entry becomes an overnight queue item (a project's prepare / training / do-it-all
    -> its "project" item with the project's own choices; a paused training continues from its resume point when the
    queue runs it) and leaves the held list. The hold lifts when nothing else is held, as with Dismiss."""
    with _LOCK:
        spec = next((s for s in _state()["resume"] if s.get("id") == entry_id), None)
    if spec is None:
        raise ModelError("That paused work isn't there any more.")
    mapped = QUEUE_AS[spec["kind"]](spec) if spec["kind"] in QUEUE_AS else None
    if not mapped:
        raise ModelError("This one can't run overnight. Resume it, or dismiss it.")
    add(*mapped)   # checked as any add is (the voice name guard, nothing ticked, already queued)
    with _LOCK:
        st = _state()
        st["resume"] = [s for s in st["resume"] if s.get("id") != entry_id]
        if not st["resume"] and not any(i.get("state") == "paused" for i in st["queue"]):
            st["held"] = False
            st.pop("held_by", None)
        _save()
        _log(f"to the queue: {spec.get('label')} as {mapped[0]} {mapped[1]}")
    return status()


def _resume_row(s: dict) -> dict:
    q = QUEUE_AS.get(s["kind"])
    try:
        as_ = q(s) if q else None
    except Exception:   # noqa: BLE001 - shown as "can't go overnight"
        as_ = None
    return {"id": s.get("id"), "kind": s["kind"], "label": s.get("label", s["kind"]), "project": s.get("project"),
            "held_at": s.get("held_at"), "queue_as": as_[0] if as_ else None}


def forget_paused() -> dict:
    """Dismiss: drop the paused work Resume would start again (its files stay: a training project keeps its resume
    point and can be continued from DoubleAgent). The hold is lifted unless queue items are still paused."""
    with _LOCK:
        st = _state()
        _log(f"forget {[x.get('label') for x in st['resume']]}")
        st["resume"] = []
        if not any(i.get("state") == "paused" for i in st["queue"]):
            st["held"] = False
        _save()
    return status()


def restore(specs: list, start: bool) -> dict:
    """Work a crash interrupted (the Doctor, at startup): put it back on the resume list. start=True: it carries on by
    itself once the models are ready (unless the user's own Pause holds it); False: it waits, held, for Resume."""
    with _LOCK:
        st = _state()
        for s in specs:
            if not any(_same(s, x) for x in st["resume"]):
                st["resume"].append(_held(s))
        if not start:
            st["held"] = True
        _save()
        _log(f"restore {[x.get('label') for x in specs]} start={start}")
    return status()


def _start(spec: dict):
    k = spec["kind"]
    if k in RESUMERS:
        return RESUMERS[k](spec)
    raise ModelError(f"Don't know how to resume '{k}'.")


def held() -> bool:
    return bool(_state().get("held"))


def hold() -> dict:
    """{held, held_by} read under the lock (who paused: "you", or "game" for the game pause)."""
    with _LOCK:
        st = _state()
        return {"held": bool(st.get("held")), "held_by": st.get("held_by")}


# ---------------------------------------------------------------- the overnight queue

def add(kind: str, target: str, now: bool = False, mode: str | None = None, options: dict | None = None) -> dict:
    """Add a queue item. now=True: run it as soon as the GPU is free, without starting the rest of the queue
    ("Start now" in SpeakEasy / DoubleAgent; it still waits for a training that's running). mode: kept on the item
    for kinds that take one (a new voice: speaking / singing / both); None lets the run decide."""
    target = (target or "").strip()
    if kind not in KINDS:
        raise ModelError("Pick Improve a voice or Train a new voice.")
    if not target:
        raise ModelError("Type a voice or a person's name.")
    if mode and mode not in KINDS[kind]["modes"]:
        raise ModelError("Pick Speaking or Singing." if KINDS[kind]["modes"] else "This kind of work has no Speaking / Singing choice.")
    target = KINDS[kind]["target"](target)   # e.g. a voice that must exist (friendly error if it doesn't)
    live = KINDS[kind].get("live")
    job = live(target) if live else None
    if job is not None and not job.finished:
        # "Train tonight instead" while that voice trains live: not both. It's paused as Pause does and goes to the
        # queue as the held list's "Train tonight instead" does (remember -> to_queue), from its resume point.
        if job.kind == "night":
            raise ModelError("That voice is being made from tonight's queue right now.")
        job.then_queue = True
        _pause_job(job)   # no queue-wide hold: if it ends some other way (done, stopped) nothing is left held
        _log(f"to the queue when paused: {job.kind} {(getattr(job, 'resume_spec', None) or {}).get('label')}")
        out = status()
        out["moved"] = "Paused and moved to tonight's queue: it goes on from where it stopped."
        return out
    who = KINDS[kind].get("person")
    key = who(target) if who else None
    with _LOCK:
        q = _state()["queue"]
        for i in q:
            if i["state"] not in ("queued", "paused", "running"):
                continue
            same = i["kind"] == kind and i["target"].lower() == target.lower()
            if not same and key:   # one item per person: a new voice, a new voice with cadence, a project
                other = KINDS.get(i["kind"], {}).get("person")
                same = bool(other) and _person_of(other, i["target"]) == key
            if same:
                raise ModelError(f"That's already in the queue "
                                 f"({KINDS[i['kind']]['label']({'kind': i['kind'], 'target': i['target'], **i})}).")
    if KINDS[kind].get("options"):
        KINDS[kind]["options"](target, options or {})   # checked and kept before the item exists
    item = {"id": uuid.uuid4().hex[:8], "kind": kind, "target": target, "state": "queued",
            "added": time.strftime("%Y-%m-%d %H:%M")}
    if now:
        item["now"] = True
    else:
        item["night"] = True   # tonight's work: waits for the start time, even while a Start now runs (P8-2)
    if mode:
        item["mode"] = mode
    item["estimate"] = estimate(item)
    with _LOCK:
        q = _state()["queue"]
        active = [k for k, i in enumerate(q) if i["state"] in ("queued", "paused", "running")]
        follows = KINDS[kind].get("follows")
        after = [k for k in active if follows and follows(item, q[k])]
        if after:   # e.g. a voice's cadence goes right after that voice's training
            q.insert(after[-1] + 1, item)
        elif now:   # first among the waiting ones
            q.insert(next((k for k in active if q[k]["state"] == "queued" and not q[k].get("now")), len(q)), item)
        else:
            q.append(item)
        default = False
        if not now and not _state()["armed"]:   # "tonight" arms the queue for its start time, never for now (P8-2)
            st = _state()
            default = not st["settings"].get("start_at")
            st["settings"]["start_at"] = st["settings"].get("start_at") or DEFAULT_START
            st.update(armed=True, start_now=False)
        _save()
    out = status()
    if not now:
        if out.get("starts_now"):   # the night is on already (P9-1: "starts at 11:00 pm" said at 01:36)
            out["starts_at"] = datetime.now().strftime("%H:%M")
        else:
            out["starts_at"] = _state()["settings"]["start_at"]
            if default:
                out["start_default"] = True   # no start time was saved: it starts at DEFAULT_START (say so on the page)
        row = next((r for r in out["queue"] if r["id"] == item["id"]), {})
        if row.get("waits_next_night"):   # P9-2: never promise a run its own rule refuses
            out["waits_next_night"] = True
    return out


def _person_of(fn, target: str) -> str | None:
    try:
        return fn(target)
    except Exception:   # noqa: BLE001 - a project gone meanwhile: nobody to compare
        return None


def estimate_for(kind: str, target: str, mode: str | None = None) -> dict:
    """How long this queue work takes, in minutes per step and "total": the one estimator every page asks
    (P8-10: "about 1 hour", "~50 min" and "about 3 h" for the same new voice)."""
    if kind not in KINDS:
        raise ModelError("Pick Improve a voice or Train a new voice.")
    item = {"kind": kind, "target": (target or "").strip(), **({"mode": mode} if mode else {})}
    return estimate(item)


def queued_for(kind: str, target: str) -> dict | None:
    """The queue item of this kind for this target that hasn't run yet (queued, paused or running), else None."""
    with _LOCK:
        return next(({k: i.get(k) for k in ("id", "state", "now", "added")} for i in _state()["queue"]
                     if i["kind"] == kind and i["target"] == target and i["state"] in ("queued", "paused", "running")), None)


def remove(item_id: str) -> dict:
    with _LOCK:
        st = _state()
        it = next((i for i in st["queue"] if i["id"] == item_id), None)
        if it and it["state"] == "running":
            raise ModelError("That one is running. Pause first, then remove it.")
        st["queue"] = [i for i in st["queue"] if i["id"] != item_id]
        if st["held"] and not st["resume"] and not any(i.get("state") == "paused" for i in st["queue"]):
            st["held"] = False   # nothing held is left (P8-6: "Train the voice" did nothing after the last Remove)
            st.pop("held_by", None)
        _save()
    out = status()
    if it and it["kind"] in ("new", "newcad", "project", "improve"):   # never deleted by a Remove
        out["note"] = ("Its training project and downloads stay. Storage → Clean up can tidy them "
                       "(\"Training projects of finished voices\", \"Left over from training downloads\").")
    return out


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
            st.update(armed=False, start_now=False)
        else:
            # Start now (when=None) is a one-time start: the saved start time stays, and the queue goes back to it
            # once this run is dry; tonight's items in it now run now too
            st.update(armed=True, start_now=when is None)
            if not st["resume"]:   # only the queue's own pause lifts: held work the user paused waits for Resume
                st.update(held=False)
                st.pop("held_by", None)
            st["settings"]["start_at"] = when or st["settings"].get("start_at") or DEFAULT_START
            for it in st["queue"]:
                if when is None and it.get("state") == "queued":
                    it.pop("night", None)
            for it in st["queue"]:   # paused items go on first, in queue order, from their resume point (as Resume)
                if it.get("state") == "paused":
                    it["state"] = "queued"
        _save()
    return status()


def settings(**kw) -> dict:
    """The queue's settings. start_at ("HH:MM") is saved without arming: the Running room sets it while the queue is
    empty, and the next "tonight" add arms for it."""
    if "start_at" in kw and not (isinstance(kw["start_at"], str) and re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", kw["start_at"])):
        raise ModelError("Give the start time as HH:MM (for example 02:00).")
    with _LOCK:
        st = _state()
        for k in ("finish_by", "keep_awake", "start_at"):
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
                        "epoch": ev.get("epoch"), "epochs": ev.get("epochs"), "planned": ev.get("planned"), "eta": ev.get("eta"),
                        "fraction": ev.get("fraction", ev.get("frac")), "pausing": j.pausing,
                        # P8-5: what a pause waits for (it lands at the end of the step under way)
                        "pausing_note": (f"Pausing at the end of this step ({ev.get('label') or ev.get('message') or 'the current step'})"
                                         if j.pausing else None),
                        "total_left": next((e["total_left"] for e in reversed(j.events[-20:]) if e.get("total_left") is not None), None)})
    left = [i for i in st["queue"] if i["state"] in ("queued", "paused", "running")]
    # when it really starts (P9-1: an open night window starts now, so "done by" counts from now) and what the
    # Finish by rule leaves for the next night (P9-2: "done by 7:18" for a job that won't start before 7:00)
    starts_now = bool(st["armed"] and (st.get("start_now") or _in_window(now)))
    start = now
    if st["settings"].get("start_at") and not starts_now and not all(i.get("now") for i in left):
        start = _next_time(st["settings"]["start_at"], now)   # "done by" = the shown start time + the work, armed or stopped
    # rc-fix3 (IA pass 11 B3): nothing runs while every item left waits to try again (YouTube blocked downloads): the
    # queue starts at the earliest retry, and says so ("Waits for YouTube until 2:09 am"), not "starts now"
    retry = {i["id"]: datetime.fromtimestamp(i["retry_after"]) for i in left
             if i["state"] == "queued" and i.get("retry_after", 0) > now.timestamp()}
    waits_until = None
    if left and retry and not running and all(i["id"] in retry for i in left if i["state"] in ("queued", "paused")):
        waits_until = min(retry.values())
        start, starts_now = max(start, waits_until), False
    dl = _deadline(start) if st["armed"] and not st.get("start_now") else None
    t, waits = start, set()
    for i in left:   # as pick() goes: one that won't finish in time waits; the ones after it may still fit
        m = timedelta(minutes=(i.get("estimate") or {}).get("total", 0))
        if dl is not None and i["state"] != "running" and t + m > dl:
            waits.add(i["id"])
        else:
            t += m
    total = sum(i.get("estimate", {}).get("total", 0) for i in left if i["id"] not in waits)
    return {"held": st["held"], "resume": [_resume_row(s) for s in st["resume"]], "running": running,
            "queue": [{**i, "display": _shown(i), **({"waits_next_night": True} if i["id"] in waits else {}),
                       **({"retry_at": retry[i["id"]].strftime("%H:%M")} if i["id"] in retry else {})}
                      for i in st["queue"]], "armed": st["armed"],
            "waits_until": waits_until.strftime("%H:%M") if waits_until else None,
            "waits_for": "YouTube" if waits_until else None,
            "start_now": bool(st.get("start_now")), "starts_now": starts_now and bool(left), "settings": st["settings"],
            "report": st["report"], "total_min": total, "start": start.strftime("%H:%M") if left else None,
            "done_by": t.strftime("%H:%M") if left and len(waits) < len(left) else None, "waits": len(waits)}


def _shown(item: dict) -> str:
    try:
        return KINDS[item["kind"]]["shown"](item)
    except Exception:   # noqa: BLE001 - a label must never break the status
        return item.get("target", "")


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
            urgent = next((i for i in st["queue"] if i["state"] == "queued" and i.get("now")), None)
            if urgent is not None and not st["held"] and _gpu_idle() and not jobs.STATUS.get("gpu_problem"):
                if not any(i["state"] == "queued" and not i.get("now") and _before(i, urgent, st["queue"])
                           for i in st["queue"] if KINDS.get(urgent["kind"], {}).get("follows")):
                    _run_item(urgent)   # "Start now": runs without starting the rest of the queue
            elif not st["held"] and st["armed"] and _gpu_idle() and not jobs.STATUS.get("gpu_problem"):
                nxt = next_item(st, datetime.now())
                if nxt is not None:
                    _run_item(nxt)
        except Exception as e:   # noqa: BLE001 - the loop must survive anything
            _log(f"loop error: {type(e).__name__}: {e}")
        time.sleep(10)


def pick(q: list, now: datetime, deadline: datetime | None, night: bool = True) -> dict | None:
    """The queue item to start now: the first queued one that finishes before the deadline (the others wait for the
    next night) and that follows nothing still waiting. A long first item no longer holds back the shorter ones behind
    it; a cadence never runs before its voice's own item, wherever that sits in the queue."""
    for i in q:
        if i["state"] != "queued" or i.get("retry_after", 0) > time.time():
            continue
        if i.get("night") and not night:
            continue   # tonight's: not during a Start now outside the night
        f = KINDS.get(i["kind"], {}).get("follows")
        if f and any(o is not i and o["state"] in ("queued", "paused", "running") and f(i, o) for o in q):
            continue   # its voice comes first
        i["estimate"] = estimate(i)
        if deadline is not None and now + timedelta(minutes=i["estimate"]["total"]) > deadline:
            continue   # wouldn't finish tonight: waits for the next night
        return i
    return None


def next_item(st: dict, now: datetime) -> dict | None:
    """The armed queue's next item now, or None (P8-2). A queue with nothing queued, paused or running disarms; a
    Start now that's run dry goes back to the saved start time; tonight's items wait for their night."""
    q = st["queue"]
    if not st["settings"].get("start_at"):   # armed by an older Start now (it cleared the start time): it was one
        with _LOCK:
            st["settings"]["start_at"] = DEFAULT_START
            st["start_now"] = True
            _save()
    start_now = bool(st.get("start_now"))
    if not any(i["state"] in ("queued", "paused", "running") for i in q):
        with _LOCK:
            st.update(armed=False, start_now=False)   # emptied, not only finished: a later "tonight" waits for the night
            _save()
        return None
    night = _in_window(now)
    if start_now and not night and not any(i["state"] in ("queued", "running") and not i.get("night") for i in q):
        with _LOCK:
            st["start_now"] = False   # the one-time start ran dry: tonight's items wait for the start time
            _save()
        return None
    if not (night or start_now):
        return None
    return pick(q, now, _deadline(now) if night else None, night=night)


def _before(i: dict, item: dict, q: list) -> bool:
    """i must run before item (item follows it, e.g. a cadence after its voice's training)."""
    f = KINDS.get(item["kind"], {}).get("follows")
    return bool(f and f(item, i) and q.index(i) < q.index(item))


def _keep_awake(on: bool):
    """Don't sleep while voice work runs (core/platform: a per-thread request on Windows; elsewhere nothing yet)."""
    from . import platform
    platform.keep_awake(on)


def _run_item(item: dict):
    job = jobs.Job("night")
    job.lp_kind = item["kind"]   # (Low profile sizes the card's shares by the item's own kind)
    jobs.JOBS[job.id] = job
    est = item.get("estimate") or {}
    if est.get("total"):   # its time left: the whole item's, from its own estimate (P8-3, P8-10)
        job.planned_s = est["total"] * 60.0
        job.after_eta_s = max(0.0, est["total"] - est.get("prepare", 0) - est.get("train", 0)) * 60.0
        job.plan_min = dict(est)
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
                    at = time.time() + 3600
                    item.update(state="queued", retry_after=at,
                                note=f"{e} Trying again at {time.strftime('%H:%M', time.localtime(at))}.")
                    _save()
                job.finish("error", message=f"{e} The queue tries again at {time.strftime('%H:%M', time.localtime(at))}.",
                           retry_at=time.strftime("%H:%M", time.localtime(at)))   # Recent: "will retry", not "failed" (B3)
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
                words = (report_words(item["kind"], res, _shown(item)) if state == "done"
                         else {"outcome": state, "note_plain": _no_numbers(res.get("note") or "") or None})
                rep["results"].append({"kind": item["kind"], "target": item["target"], "display": _shown(item), "state": state,
                                       "minutes": mins, **res, **words})
                rep["finished"] = time.strftime("%Y-%m-%d %H:%M")
            _save()
        job.finish("done" if state == "done" else "error", message=res.get("note") or "Done.", result=res)

    jobs.GPU_QUEUE.put((job, run))


DEFAULT_START = "23:00"   # "tonight" with no start time saved yet (said so in the answer; the Running room changes it)


SAME = 0.02   # likeness points: a change smaller than this is "the same" (run-to-run noise on one voice is ~0.01)


def _no_numbers(note: str) -> str:
    """A note for the basic page: its measured numbers left out ("(measured 0.76 against 0.7)", "0.31")."""
    note = re.sub(r"\s*\([^()]*\d\.\d+[^()]*\)", "", note)
    return re.sub(r"\s+", " ", re.sub(r"\s*-?\d+\.\d+", "", note)).strip()


def report_words(kind: str, res: dict, display: str) -> dict:
    """The overnight report in words (no decimals for the basic page; `note` keeps the numbers for Deep Cover):
    before_word / after_word (the trained cut-offs) and outcome: "new" (a voice made from nothing: never "kept the
    old"), "waiting" (a retrained one waits beside the voice for the ear), "better" / "same" / "not_better"."""
    from .voices import word_of
    b, a = res.get("before"), res.get("after")
    bw, aw = word_of(b), word_of(a)
    num = isinstance(a, (int, float)) and isinstance(b, (int, float))
    if res.get("nothing_new") and kind == "improve":   # phase 26: nothing new found, nothing left out: not even tried
        out, plain = "same", f"Nothing new of {display} was found and every source still belongs, so yours is kept as it is."
    elif res.get("kept") and kind == "improve":   # phase 26: Improve tried, the bar wasn't cleared, yours is kept
        tried = res.get("tried") or {}
        left = tried.get("left_out") or []
        out = "not_better"
        # (the page's line already says "<voice> · tried": this one starts at the outcome; whole minutes, 1.0.1: it said
        # "Improve tried <voice>" again and "0.0 min of new clips")
        bits = []
        if tried.get("added_min") is not None:
            m = float(tried["added_min"] or 0)
            bits.append("no new clips" if m < 0.5 else f"{round(m)} min of new clips")
        if left:
            bits.append(f"{len(left)} source{'s' if len(left) != 1 else ''} left out")
        plain = "No improvement, so yours is kept" + (f" ({'; '.join(bits)})." if bits else ".")
    elif res.get("pending") or res.get("pending_for"):
        out, plain = "waiting", (f"A retrained {display} waits beside the current one"
                                 + (f" ({aw} against {bw})" if aw and bw else "") + ". Listen to both and keep the one you like.")
    elif kind == "cadence" and res.get("replaced") is not None:   # a cadence: replaced, or the one it had stays
        out, plain = (("better", f"{display} speaks with a better cadence now.") if res["replaced"] else
                      ("not_better", f"{display}'s cadence stays as it was: the new one wasn't better."))
    elif kind in ("new", "newcad") or (a is not None and b is None and kind != "cadence"):
        out, plain = "new", f"{display} is new in your voices" + (f", with a {aw} likeness." if aw else ".")
    elif num and a - b >= SAME:
        out, plain = "better", f"{display} got better" + (f" ({bw} to {aw})." if aw != bw else ".")
    elif num and a - b > -SAME:
        out, plain = "same", f"{display} sounds about the same" + (f" ({aw})." if aw else ".")
    elif num:
        out, plain = "not_better", f"{display} didn't get better" + (f" ({bw} to {aw})" if aw != bw else "") + \
            "; the one you had stays."
    else:
        out, plain = None, _no_numbers(res.get("note") or "") or None
    cad = res.get("cadence")
    if out == "new" and isinstance(cad, dict):
        plain += " Its cadence is ready too." if cad.get("after") is not None or cad.get("likeness") is not None \
            else " " + _no_numbers(cad.get("note") or "")
    return {"before_word": bw, "after_word": aw, "outcome": out, "note_plain": plain.strip() if plain else plain}


def control_for(job):
    """A trainer Control for a queue item's job: Pause reaches it, and queued covers run between training steps."""
    control = jobs.Control()

    def service():
        jobs.serve_between()
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


