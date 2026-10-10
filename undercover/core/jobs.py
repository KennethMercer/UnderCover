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
        self.pausing = False      # Pause at the top of the page: stop cleanly, then resume later
        self.resume_spec = None   # how to start this work again (worklist.resume)
        self.control = None       # trainer Control, for jobs that train

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
        return self.state in ("done", "error", "cancelled", "paused")


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
    from .. import worklist
    if job.kind in worklist.TRAIN_KINDS and job.kind != "auto" and (job.pausing or worklist.held()):
        job.finish("paused", message="Voice work is paused (top of the page). Resume starts it.")
        worklist.remember(job)
        return
    if job.kind == "auto" and worklist.held():
        job.pausing = True   # a one-shot still makes its cover, but sets aside any voice training
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
