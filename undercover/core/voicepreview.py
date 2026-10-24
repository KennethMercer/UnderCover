"""Every voice's preview line (rc-preview, the user's ask for 1.0): a ▶ on each voice card plays the voice saying
"Go undercover as <its name>.", the same line for every voice, so they compare by ear before one is picked.

- **Made with the voice**, as the last step of whatever added or changed it: a training's export, an import (search,
  link or file: in the staging folder, before it's listed), a one-shot's chosen candidate (at promote; the others are
  discarded unrendered), an Improve kept, a speech pack kept, a rename. Voices from before get theirs from the backfill,
  one at a time, only while nothing else runs.
- **Rendered by SpeakEasy** (speak.render_line, the voice's own Reader or its speech pack, then its RVC), registered
  here by the app at start: DoubleAgent and core add voices and may not import SpeakEasy (the dependency rule).
- **Kept in the voice's folder** (preview.mp3 + preview.json), so Keep, Undo, set aside, the Vault and Storage moves
  carry it with the voice. It isn't part of the model: the size shown, the voice export and sharing leave FILES out.
- **Fresh** while preview.json's key matches: the line as said (the display name in it), the model's and index's sizes,
  and the speech pack's files. A change made through UnderCover re-renders it outright (force); the key catches the
  rest (an edited LINE, a pack added by hand), and the backfill makes those again.
"""
import hashlib
import json
import shutil
import subprocess
import threading
import time
from pathlib import Path

LINE = "Go undercover as {name}."   # the user's line (2026-10-24): the voice's display name, read as it is
FILES = ("preview.mp3", "preview.json")   # the clip and its record: not part of the model (sizes, exports, sharing)
ESTIMATE_S = 5.0   # one render: the Reader's line (about 2 s of audio) and the voice's conversion, its load included
RENDER = [None]    # (job, voice) -> (samples, sample rate): SpeakEasy's render_line, set by the app at start
_LOCK = threading.Lock()


def line_for(v) -> str:
    from .voices import display_name
    return LINE.format(name=display_name(v))


def _stat(p) -> int:
    try:
        return Path(p).stat().st_size if p else 0
    except OSError:
        return 0


def key(v) -> str:
    """What the clip was made from: the line as said, the model, the index and the live speech pack (when it was made,
    by which engine: a pack still waiting to be heard, tts/pending, isn't the voice's yet)."""
    from .cadence import pack_ready
    rec = (v.extra or {}).get("tts") or {}
    pack = {k: rec.get(k) for k in ("made", "engine", "engine_pin", "project")} if (Path(v.dir) / "tts").is_dir() and rec else None
    if pack is not None:   # (a pack whose engine isn't installed reads nothing: the Reader said the line instead)
        pack["ready"] = bool(pack_ready(rec.get("engine", "chatterbox"), rec.get("engine_pin")))
    raw = json.dumps([line_for(v), _stat(v.pth), _stat(v.index), pack], sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def clip(v) -> Path:
    return Path(v.dir) / FILES[0]


def fresh(v) -> bool:
    """The voice has its clip, made from what it is now (the ▶ shows only then)."""
    try:
        rec = json.loads((Path(v.dir) / FILES[1]).read_text(encoding="utf-8"))
        return clip(v).is_file() and rec.get("key") == key(v)
    except (OSError, ValueError):
        return False


class _Quiet:
    """The job a render runs under: the job that added the voice (its Cancel stops it), without SpeakEasy's
    sentence-by-sentence events in that job's own progress."""

    def __init__(self, parent=None):
        self._parent = parent
        self.id = f"{getattr(parent, 'id', 'preview')}-line"

    @property
    def cancel(self):
        return bool(getattr(self._parent, "cancel", False))

    def push(self, **ev):
        pass


def make(v, job=None, force: bool = False) -> bool:
    """Render the voice's clip now (on the GPU worker). -> True when it's there and fresh. A failure is said in the
    log and never raised: the voice it belongs to is added all the same, and the backfill tries again later."""
    if not force and fresh(v):
        return True
    if RENDER[0] is None:
        print(f"Preview line for {v.name}: nothing to render it with here (the app registers SpeakEasy at start).", flush=True)
        return False
    from . import fsutil
    d = Path(v.dir)
    try:
        y, sr = RENDER[0](_Quiet(job), v)
        import soundfile as sf
        wav = d / f"preview.{time.time_ns()}.wav"
        fsutil.guard(wav)
        sf.write(str(wav), y, sr, subtype="PCM_16")
        try:
            tmp = d / f"preview.{time.time_ns()}.mp3"
            exe = shutil.which("ffmpeg")
            if not exe:
                raise RuntimeError("ffmpeg isn't here")
            r = subprocess.run([exe, "-hide_banner", "-nostdin", "-y", "-i", str(wav), "-c:a", "libmp3lame", "-b:a", "96k", str(tmp)],
                               capture_output=True, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if r.returncode != 0 or not tmp.is_file():
                raise RuntimeError(f"ffmpeg couldn't write the clip ({(r.stderr or '').strip()[-200:]})")
            fsutil._replace(tmp, clip(v))
        finally:
            wav.unlink(missing_ok=True)
        fsutil.write_text(d / FILES[1], json.dumps({"key": key(v), "line": line_for(v), "seconds": round(len(y) / sr, 2),
                                                    "made": time.strftime("%Y-%m-%d %H:%M")}, indent=1))
        _broken(v.name, None)
        return True
    except BaseException as e:   # noqa: BLE001 - a Cancel of the job that added the voice ends here too: said, not raised
        from .jobs import Cancelled
        if isinstance(e, (KeyboardInterrupt, SystemExit)):
            raise
        print(f"Preview line for {v.name}: couldn't be made ({type(e).__name__}: {e})", flush=True)
        if not isinstance(e, Cancelled):
            _broken(v.name, key(v) if d.is_dir() else None)
        return False


def on_gpu() -> bool:
    """This thread owns the GPU (the queue's worker): a render can run here and now."""
    return threading.current_thread().name == "gpu"


def after_add(v, job=None, force: bool = True, wait: float | None = None):
    """The last step of a path that added or changed a voice. On the GPU worker it renders now, in that job; anywhere
    else (an HTTP request, a download thread) it queues a short GPU job for it. With wait (seconds) it waits for that
    job, so an import lists its voice with its clip; a job that hasn't started by then (the GPU busy with a long
    cover, the queue held) is called off before it touches the folder, and the backfill makes the clip later.
    -> True (made), False (couldn't), or None (queued, not waited for, or called off)."""
    if not force and fresh(v):
        return True
    if RENDER[0] is None:   # (no server here: a tool, a unit test; the server's backfill makes it later)
        return False
    if on_gpu():
        return make(v, job, force)
    q = queue_job(v, force)
    if wait is None:
        return None
    t0 = time.time()
    while not q.finished and (q.state == "running" or time.time() - t0 < wait):
        time.sleep(0.2)
    if not q.finished:
        q.cancel = True   # (still queued: run_job finishes it as cancelled, untouched)
        while q.state == "running" and not q.finished:   # (it started just now: its Cancel lands within the line)
            time.sleep(0.2)
        return None
    return fresh(v)


def queue_job(v, force: bool = True):
    """A GPU-queue job that renders one voice's clip ("Preview line for X" in the Running room)."""
    from . import jobs
    from .voices import display_name
    job = jobs.Job("voicepreview", title=f"Preview line for {display_name(v)}")
    job.voices = {v.name}
    jobs.JOBS[job.id] = job

    def run(j):
        ok = make(v, j, force)
        j.finish("done" if ok else "error", message="Made" if ok else "Couldn't make the preview line (see the log)")
    jobs.GPU_QUEUE.put((job, run))
    return job


def import_voice(stage, job=None, wait: float = 120.0):
    """An import (a search result, a link, a file): stage() puts it in the staging folder (-> the staged voice), its
    clip is made there, and only then it's listed (promote), so it appears with its ▶. A name already taken is refused
    as a direct install would be; a failure on the way discards the staged copy (inside staging only)."""
    from .voices import ModelError, discard_staged, pending_candidate, promote, voices_dir
    v = stage()
    try:
        if (voices_dir() / v.name).exists() or pending_candidate(v.name):
            raise ModelError(f"A voice called '{v.name}' is already installed. Pick another name or remove it first.")
        if job is not None:
            job.push(stage="preview", frac=1.0, message="Making its preview line", total_left=ESTIMATE_S)
        after_add(v, job, force=True, wait=wait)
        return promote(v.name)
    except BaseException:
        discard_staged(v.name)
        raise


# ---------------------------------------------------------------- voices from before: the backfill

def _broken_file() -> Path:
    from .config import path
    return path("cache_dir") / "voicepreview.json"


RETRY_S = 86400.0   # a voice whose line failed is tried again a day later (or at once when its files change)


def _broken(name: str, k: str | None):
    """A voice whose clip couldn't be made, by the key it had and when; None clears it."""
    from . import fsutil
    with _LOCK:
        try:
            d = json.loads(_broken_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            d = {}
        if not k and name not in d:
            return
        if k:
            d[name] = {"key": k, "at": round(time.time())}
        else:
            d.pop(name, None)
        try:
            fsutil.write_text(_broken_file(), json.dumps(d, indent=1))
        except OSError:
            pass


def todo() -> list:
    """The listed voices still without a fresh clip, the liked ones first; ones that failed on these files skipped."""
    from . import ear
    from .voices import listed_voices
    try:
        broken = json.loads(_broken_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        broken = {}
    out = []
    for v in listed_voices():
        b = broken.get(v.name)
        held = isinstance(b, dict) and b.get("key") == key(v) and time.time() - b.get("at", 0) < RETRY_S
        if not fresh(v) and not held:
            out.append(v)
    taste = {v.name: (ear.taste(v.name) or {}).get("up", 0) for v in out}
    return sorted(out, key=lambda v: (-taste[v.name], v.name))


def idle() -> bool:
    """Nothing else runs or waits for the GPU, nothing is held (Pause, the game pause), no Storage move, no game up."""
    from . import gamepause, storage, worklist
    return (worklist._gpu_idle() and not worklist.held() and not storage.MOVING and gamepause.STATE["game"] is None
            and not gamepause.game_up())


def start(period: float = 60.0, between: float = 10.0):
    """The backfill's watcher (the server starts it): while idle, one voice's clip at a time, re-checking in between;
    resumable by nature (each voice's own fresh clip is the record). Off when UNDERCOVER_NO_BACKFILL is set (smoke's
    sandbox: its checks expect the queue as they left it). Its finished jobs leave the job feed after two minutes (the
    page has seen them end by then: a first start's thirty would fill the Running room's list for an hour)."""
    import os
    if os.environ.get("UNDERCOVER_NO_BACKFILL"):
        return
    from . import jobs
    made: list = []

    def loop():
        while True:
            wait = period
            try:
                for j in [j for j in made if j.finished and j.ended and time.time() - j.ended > 120]:
                    jobs.JOBS.pop(j.id, None)
                    made.remove(j)
                if RENDER[0] is not None and idle():
                    left = todo()
                    if left:
                        j = queue_job(left[0], force=False)
                        made.append(j)
                        while not j.finished:
                            time.sleep(1)
                        wait = between if len(left) > 1 else period
            except Exception as e:   # noqa: BLE001 - the watcher never dies
                print(f"Preview lines: {type(e).__name__}: {e}", flush=True)
            time.sleep(wait)
    threading.Thread(target=loop, daemon=True, name="voicepreview").start()
