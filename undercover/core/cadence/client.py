"""The app's side of the cadence worker: start it on demand, send requests, read replies, stop it when idle.

Protocol (worker.py): one JSON object per line. Request {"id", "op", ...}; replies {"id", "ok": true, ...} or
{"id", "ok": false, "error", "type"}; while a request runs the worker may send {"id", "progress": message, "done",
"total"} (model downloads). Audio goes through files, never the pipe.

Requests run one at a time in the worker, in order; `submit` returns a Future so a caller can queue the next
sentence while it converts this one (SpeakEasy reads one sentence ahead).
"""
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import Future

from ..config import CONFIG, ROOT, path


class WorkerError(Exception):
    """The worker reported an error (its message), or stopped unexpectedly."""
    def __init__(self, message: str, kind: str = ""):
        super().__init__(message)
        self.kind = kind


def _idle_s() -> float:
    return 60.0 * float((CONFIG.get("cadence") or {}).get("idle_minutes", 10))


class Worker:
    def __init__(self):
        self.proc = None
        self.lock = threading.RLock()
        self.pending: dict[int, tuple[Future, object, object]] = {}   # id -> (future, progress, the process it went to)
        self.n = 0
        self.last = time.time()
        self.log = None
        self._watch = None

    # ---------------------------------------------------------------- process
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _start(self):
        env = dict(os.environ, PYTHONUNBUFFERED="1", TQDM_DISABLE="1", HF_HUB_DISABLE_SYMLINKS_WARNING="1",
                   HF_HOME=str(path("models_dir") / "hf"), HF_HUB_DISABLE_TELEMETRY="1",
                   TRANSFORMERS_VERBOSITY="error", PYTHONIOENCODING="utf-8")
        from .. import gpu, lowprofile
        env.pop(gpu.LIMIT_ENV, None)   # (never the server's own cap: the worker's share, when Low profile is on)
        share = lowprofile.worker_limit_gb()
        if share:
            env[gpu.LIMIT_ENV] = str(share)
        self.log = open(path("logs_dir") / "cadence-worker.log", "a", encoding="utf-8", buffering=1)
        self.log.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] starting the cadence worker\n")
        self.proc = subprocess.Popen([sys.executable, "-m", "undercover.core.cadence.worker"], cwd=ROOT, env=env,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True, name="cadence-reader").start()
        lowprofile.apply()   # (the worker's share comes out of the server's cap while it's loaded)
        if self._watch is None:
            self._watch = threading.Thread(target=self._idle_loop, daemon=True, name="cadence-idle")
            self._watch.start()

    def _reader(self, proc):
        for raw in proc.stdout:
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            with self.lock:
                entry = self.pending.get(msg.get("id"))
            if entry is None:
                continue
            fut, on_progress, _ = entry
            if "progress" in msg:
                if on_progress:
                    try:
                        on_progress(msg["progress"], msg.get("done", 0), msg.get("total", 0))
                    except Exception:   # noqa: BLE001 - a progress callback must not kill the reader
                        pass
                continue
            with self.lock:
                self.pending.pop(msg["id"], None)
                self.last = time.time()
            if msg.get("ok"):
                fut.set_result(msg)
            else:
                fut.set_exception(WorkerError(msg.get("error") or "The speech engine failed.", msg.get("type", "")))
        # the pipe closed: the worker exited (crash, out of memory in the driver, or stop())
        code = proc.wait()
        with self.lock:
            if self.proc is proc:
                self.proc = None
            # only this process's requests: after a Stop ended it, a new one may already be reading the next speech
            mine = [k for k, e in self.pending.items() if e[2] is proc]
            lost = [self.pending.pop(k) for k in mine]
        for fut, _, _ in lost:
            if not fut.done():
                fut.set_exception(WorkerError(f"The speech engine stopped unexpectedly (exit code {code}). "
                                              "Details are in logs/cadence-worker.log; try again.", "Crashed"))

    def _idle_loop(self):
        while True:
            time.sleep(30)
            with self.lock:
                idle = self.running() and not self.pending and time.time() - self.last > _idle_s()
            if idle:
                self.stop("idle")

    def abort(self, why: str = "stopped") -> bool:
        """A Stop (rc-fix10, the friend's PC: SpeakEasy's Stop let the worker go on with its requests, the GPU at 100 % and
        "about 10 min" left): with a request in it, the worker is ended at once (an engine mid-sentence can't be called
        back) and its requests fail; the next request starts it again (the engine loads in about 20 s). Idle, it's left
        alone. -> True when it was ended."""
        with self.lock:
            proc = self.proc
            if proc is None or proc.poll() is not None or not any(e[2] is proc for e in self.pending.values()):
                return False
            self.proc = None   # (a request after this starts a new one, never writes to the dying one)
        try:
            proc.kill()
        except OSError:
            pass
        if self.log:
            self.log.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] ended mid-request ({why})\n")
        return True

    def stop(self, why: str = ""):
        """Stop the worker (frees its VRAM). Requests still waiting fail; the next request starts it again."""
        with self.lock:
            proc, self.proc = self.proc, None
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.stdin.write(b'{"op": "quit"}\n')
            proc.stdin.flush()
            proc.wait(timeout=10)
        except Exception:   # noqa: BLE001
            proc.kill()
        if self.log:
            self.log.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] stopped ({why or 'asked'})\n")
        try:
            from .. import lowprofile
            lowprofile.apply()   # (its share goes back to the server)
        except Exception:   # noqa: BLE001
            pass

    # ---------------------------------------------------------------- requests
    def submit(self, op: str, progress=None, **kw) -> Future:
        fut: Future = Future()
        with self.lock:
            if not self.running():
                self._start()
            self.n += 1
            rid = self.n
            self.pending[rid] = (fut, progress, self.proc)
            self.last = time.time()
            try:
                self.proc.stdin.write((json.dumps({"id": rid, "op": op, **kw}) + "\n").encode("utf-8"))
                self.proc.stdin.flush()
            except OSError:
                self.pending.pop(rid, None)
                raise WorkerError("The speech engine isn't running (it may have just crashed); try again.", "Crashed")
        return fut

    def call(self, op: str, timeout: float | None = None, progress=None, cancel=None, **kw) -> dict:
        """Run one request and wait for it. cancel() -> True (or raising) stops it: the wait, and the worker's work."""
        fut = self.submit(op, progress=progress, **kw)
        return wait(fut, timeout, cancel)


def wait(fut: Future, timeout: float | None = None, cancel=None) -> dict:
    """The request's reply. cancel() -> True, or a cancel() that raises (a job's own check), ends the worker's work as
    well as the wait (WORKER.abort): within about half a second, whatever the engine was in the middle of."""
    t0 = time.time()
    while True:
        try:
            return fut.result(timeout=0.5)
        except TimeoutError:
            pass
        if cancel:
            try:
                stop = cancel()
            except BaseException:
                WORKER.abort("cancelled")
                raise
            if stop:
                WORKER.abort("cancelled")
                from ..jobs import Cancelled
                raise Cancelled()
        if timeout and time.time() - t0 > timeout:
            raise WorkerError("The speech engine didn't answer in time.", "Timeout")


WORKER = Worker()


def transcribe(files: list, words: bool = False, progress=None, cancel=None) -> list:
    """Whisper on the GPU: [text, ...] for each file (words=True: [{"text", "words": [[word, start, end], ...]}])."""
    r = WORKER.call("transcribe", files=[str(f) for f in files], words=words, progress=progress, cancel=cancel,
                    model=(CONFIG.get("cadence") or {}).get("transcribe_model", "large-v3-turbo"))
    return r["results"]


def speak(text: str, ref: str, out: str, engine: str, ref_text: str = "", seed: int = 1234, wait_for: bool = True,
          progress=None, cancel=None, **opts):
    """One sentence -> out (a 24 kHz WAV). wait_for=False returns a Future (read the next while converting this one)."""
    c = CONFIG.get("cadence") or {}
    opts = {"cfg_weight": float(c.get("cfg_weight", 0.3)), "exaggeration": float(c.get("exaggeration", 0.5)), **opts}
    fut = WORKER.submit("tts", progress=progress, engine=engine, text=text, ref=str(ref), ref_text=ref_text,
                        out=str(out), seed=int(seed), opts=opts)
    return wait(fut, None, cancel) if wait_for else fut
