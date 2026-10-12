"""Jobs: the single GPU worker thread, its queue, live progress events (SSE) and friendly error messages.

Every module's GPU work is a Job run by `gpu_worker`, so the models stay loaded and jobs queue. Long voice work
(training) runs queued jobs between its steps (`Control.service`), so covers don't wait for it.
"""
import queue
import shutil
import threading
import time
import traceback
import uuid
from pathlib import Path

from .config import CONFIG, ROOT, path


class Cancelled(Exception):
    pass


class Paused(Exception):
    """Training was paused (Pause at the top of the page, or on the Train tab): the job ends as "paused" and
    Resume starts it again from the same point."""


class Control:
    """Set from the UI thread, read by the training loop between steps."""
    def __init__(self):
        self.pause = False   # save a resume point and return
        self.stop = False    # finish now and export the best save
        self.service = lambda: None   # called between steps; the server runs queued covers here

    def wants_out(self) -> bool:
        return self.pause or self.stop


# ---------------------------------------------------------------- friendly errors

USER_ERRORS: list = []   # exception classes whose message is already meant for the user (modules add theirs)


def register_user_error(cls):
    if cls not in USER_ERRORS:
        USER_ERRORS.append(cls)
    return cls


def friendly(e: BaseException, stage: str = "") -> str:
    """Turn an exception into a message that says what to do. Full traceback goes to logs/errors.log."""
    from .voices import ModelError
    if isinstance(e, (ModelError, *USER_ERRORS)):
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


# ---------------------------------------------------------------- time left (phase 4c: an ETA on every bar)

# Kinds whose progress restarts at 0 in each stage, in stage order: their time left = this stage's (extrapolated) + the
# typical length of the stages still to come (learned from past runs). Every other kind reports whole-job progress.
STAGES = {"auto": ["song", "voice", "cover"]}
_ETA_FILE = None
_ETA_LOCK = threading.Lock()


def _eta_file():
    return path("cache_dir") / "eta.json"


def _eta_load() -> dict:
    global _ETA_FILE
    if _ETA_FILE is None:
        try:
            import json
            _ETA_FILE = json.loads(_eta_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _ETA_FILE = {}
    return _ETA_FILE


def _median(xs):
    xs = sorted(xs)
    return xs[len(xs) // 2] if xs else None


def typical(kind: str, stage: str | None = None) -> float | None:
    """How long a kind (or one of its stages) usually takes, in seconds: the median of its last runs."""
    rec = _eta_load().get(kind) or {}
    return _median(rec.get("stages", {}).get(stage, [])) if stage else _median(rec.get("total", []))


def _eta_learn(job: "Job"):
    """Remember how long a finished job (and each of its stages) took, for the next one's estimate."""
    if job.state != "done" or not job.started:
        return
    import json
    from . import fsutil
    with _ETA_LOCK:
        st = _eta_load()
        rec = st.setdefault(job.kind, {"total": [], "stages": {}})
        rec["total"] = (rec["total"] + [round(job.ended - job.started, 1)])[-12:]
        for name, secs in job.stage_secs.items():
            rec["stages"][name] = (rec["stages"].get(name, []) + [round(secs, 1)])[-12:]
        try:
            fsutil.write_text(_eta_file(), json.dumps(st))
        except OSError:
            pass


# ---------------------------------------------------------------- jobs

class Job:
    def __init__(self, kind: str, title: str | None = None):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.title = title        # what the page's job list calls it ("Hello · obama-uc"); None: the kind's label
        self.state = "queued"
        self.events: list[dict] = []
        self.cond = threading.Condition()
        self.cancel = False
        self.result = None
        self.pausing = False      # Pause at the top of the page: stop cleanly, then resume later
        self.resume_spec = None   # how to start this work again (worklist.resume)
        self.control = None       # trainer Control, for jobs that train
        self.created = time.time()
        self.ended = None
        self.started = None       # when it started running (time left is measured from here)
        self.stage_secs: dict = {}   # finished stages -> seconds (learned for the next estimate)
        self._stage = None        # (name, started, first progress (t, p))
        self._p0 = None           # first whole-job progress (t, p)

    def push(self, **ev):
        with self.cond:
            ev.setdefault("state", self.state)
            if not ev.get("final"):
                self._time_left(ev)
            self.events.append(ev)
            self.cond.notify_all()

    def _time_left(self, ev: dict):
        """Every progress event gets total_left (seconds), unless the job already said: from its own eta, else
        extrapolated from its progress so far (+ the typical length of the stages still to come)."""
        now = time.time()
        if self.started is None:
            self.started = now
        stage = ev.get("stage")
        if stage and (self._stage is None or self._stage[0] != stage):
            if self._stage is not None:
                self.stage_secs[self._stage[0]] = now - self._stage[1]
            self._stage = (stage, now, None)
        if ev.get("total_left") is not None:
            return
        if ev.get("eta") is not None:
            ev["total_left"] = ev["eta"]
            return
        p = ev.get("frac", ev.get("fraction"))
        if p is None or p < 0:
            return
        if p >= 1 and (not STAGES.get(self.kind) or stage == STAGES[self.kind][-1]):
            ev["total_left"] = 0.0   # all done but the saving
            return
        if p >= 1:
            return
        order = STAGES.get(self.kind)
        if order and stage in order:   # per-stage progress: this stage's left + the typical later stages
            name, t0, first = self._stage
            if first is None and p > 0:
                self._stage = (name, t0, (now, p))
                first = self._stage[2]
            left = None
            if first and p - first[1] > 0.01 and now - first[0] > 2:
                left = (now - first[0]) * (1 - p) / (p - first[1])
            elif typical(self.kind, stage):
                left = max(0.0, typical(self.kind, stage) - (now - t0))
            later = [typical(self.kind, x) for x in order[order.index(stage) + 1:]]
            if left is not None and all(x is not None for x in later):
                ev["total_left"] = round(left + sum(later), 1)
            return
        if self._p0 is None and p > 0:
            self._p0 = (now, p)
        if self._p0 and p - self._p0[1] > 0.01 and now - self._p0[0] > 2:
            ev["total_left"] = round((now - self._p0[0]) * (1 - p) / (p - self._p0[1]), 1)
        elif typical(self.kind):
            ev["total_left"] = round(max(0.0, typical(self.kind) - (now - self.started)), 1)

    def finish(self, state: str, **ev):
        with self.cond:
            self.state = state
            self.ended = time.time()
            if self._stage is not None:
                self.stage_secs[self._stage[0]] = self.ended - self._stage[1]
        self.push(state=state, final=True, **ev)
        _eta_learn(self)

    @property
    def finished(self):
        return self.state in ("done", "error", "cancelled", "paused")

    def snapshot(self) -> dict:
        """The job's latest progress in one small dict (GET /api/jobs): the page's fallback when its event stream
        stalls, how it finds running work after a reload, and SafeHouse's job list."""
        evs = list(self.events)
        last = evs[-1] if evs else {}
        ref = evs[-2] if last.get("final") and len(evs) > 1 else last   # a finished job: how far it got
        stage = ref.get("stage")
        snap = {"id": self.id, "kind": self.kind, "state": self.state, "title": self.title or (self.resume_spec or {}).get("label"),
                "project": (self.resume_spec or {}).get("project"), "created": round(self.created, 1),
                "ended": round(self.ended, 1) if self.ended else None, "pausing": self.pausing, "stage": stage,
                "message": last.get("message") or last.get("label") or last.get("note"), "events": len(evs)}
        # the newest value of each progress field within the current stage (some events carry only a note)
        for ev in reversed(evs[-40:]):
            if ev.get("stage") != stage and not ev.get("final"):
                break
            for k in ("frac", "fraction", "epoch", "epochs", "eta", "total_left", "stage_left", "index", "of", "elapsed"):
                if ev.get(k) is not None:
                    snap.setdefault(k, ev[k])
        frac, fraction = snap.pop("frac", None), snap.pop("fraction", None)
        snap["progress"] = frac if frac is not None else fraction
        plan = next((ev["plan"] for ev in reversed(evs) if isinstance(ev.get("plan"), dict)), None)
        if plan and self.kind == "auto":   # the one-shot: its request and what it chose
            name = lambda v, k: v.get(k) if isinstance(v, dict) else v
            snap["title"] = snap["title"] or plan.get("request")
            vid = name(plan.get("voice"), "name")
            snap["plan"] = {"request": plan.get("request"), "song": name(plan.get("song"), "title"), "voice": vid}
            if vid:   # ids stay in the data; this is what the strip and the drawer show
                from .voices import label_of
                snap["plan"]["voice_display"] = label_of(vid)
        if last.get("final"):
            snap["final"] = {"state": last.get("state"), "message": last.get("message")}
        return snap


JOBS: dict[str, Job] = {}
GPU_QUEUE: "queue.Queue[tuple[Job, callable]]" = queue.Queue()
STATUS = {"models": "starting", "gpu": None, "vram_gb": None, "started": time.time()}
PORT = None   # the port this server listens on (set at startup): a test server keeps its own queue file


def gpu_worker():
    """Single thread that owns the GPU: warm-up first, then queued jobs."""
    from .gpu import MODELS
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
        elif CONFIG["ui"]["preload_models"]:
            from . import setup_models
            mdir = path("models_dir")
            STATUS["first_run"] = not all((mdir / f).exists() for f in setup_models.FILES)   # ~600 MB to fetch, once
            STATUS["models"] = "loading"
            from .separate import SEP
            MODELS.separator._get(SEP["vocal_model"])
            MODELS.converter
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
    from . import storage
    if storage.MOVING:   # SafeHouse is moving UnderCover's files: nothing may read or write them meanwhile
        job.finish("error", message="UnderCover's files are being moved (SafeHouse → Storage). Try again when that's done.")
        return
    from . import worklist
    if job.kind in worklist.PAUSABLE and job.kind not in worklist.PARTLY_PAUSABLE and (job.pausing or worklist.held()):
        job.finish("paused", message="Voice work is paused (top of the page). Resume starts it.")
        worklist.remember(job)
        return
    if job.kind in worklist.PARTLY_PAUSABLE and worklist.held():
        job.pausing = True   # e.g. a one-shot still makes its cover, but sets aside any voice training
    with job.cond:
        job.state = "running"
    for attempt in (1, 2):
        try:
            fn(job)
        except Cancelled:
            job.finish("cancelled", message="Cancelled.")
        except Exception as e:
            if type(e).__name__ == "Paused":   # Pause at the top of the page
                if not job.finished:
                    job.finish("paused", message="Paused. Resume (top of the page) continues where it stopped.")
                worklist.remember(job)   # its resume point (before 2026-10-11 this returned first: Resume did nothing)
                return
            # Out of GPU memory: free what UnderCover holds (cached voices, allocator cache) and try once more.
            # Separations are cached, so the retry picks up where it was; mostly hits smaller cards or a game open.
            if attempt == 1 and _is_oom(e) and job.kind in ("cover", "preview", "auto"):
                from . import gpu
                gpu.free()
                job.push(stage=(job.events[-1].get("stage") if job.events else "") or "converting",
                         message="The GPU ran out of memory; freed some and trying again")
                continue
            job.finish("error", message=friendly(e, job.events[-1].get("stage", "") if job.events else ""))
        if job.state == "paused":
            worklist.remember(job)
        return


def _is_oom(e: Exception) -> bool:
    return type(e).__name__ == "OutOfMemoryError" or "out of memory" in str(e).lower()
