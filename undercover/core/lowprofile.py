"""Low profile: UnderCover keeps to part of the graphics card and works in bursts, so a game gets the card most of the time.

The user sets one number, `limit_gb`: the whole of UnderCover on the card, both processes (the server and the cadence
worker). The split is this module's business: while the worker is loaded it is capped at its engine's share and that
share comes out of the server's cap; a training stops the worker first and gets the whole limit. `burst` (0.25-0.75) is
the share of the time UnderCover works: every step loop (training steps, conversion and separation chunks, sentences)
calls gap() after a step, which waits for the GPU and pauses in proportion, so a game really gets frames in between.

What each kind of job needs is learned from the jobs themselves (jobs.gpu_worker records every GPU job's allocated
peak); a job that can't fit says so plainly and waits for the cap to lift, never an out-of-memory error.

Settings: config.toml [low_profile] on, limit_gb, burst, auto, for_games (the games that turn it on by themselves,
answered "Low profile" once). Sandboxes and test servers (UNDERCOVER_TEST_ROOTS) run with it on at 12 GB unless
UNDERCOVER_LOW_PROFILE says otherwise ("0" off, or a limit in GB).
"""
import json
import os
import sys
import threading
import time
from pathlib import Path

from .config import CONFIG, path, set_values

SECTION = "low_profile"
DEFAULT_BURST = 0.5
MIN_LIMIT_GB = 2
TEST_LIMIT_GB = 12
STATE = {"by_game": None,       # the game (exe) that turned it on; the game closing turns it off again
         "server_gb": None,     # the cap the server process runs under now (None: the whole card)
         "worker_gb": None,     # the worker's share while it's loaded (None: no worker)
         "applied": None}       # (on, server_gb) last applied: nothing is re-set for nothing
_LOCK = threading.Lock()
NEED_FILE = "gpu_need.json"     # cache/: the allocated peak of the last runs of each kind of job, binary GB
NEED_MARGIN = 1.15              # what a kind needs: its typical peak plus a seventh (the allocator's working room)


# ---------------------------------------------------------------- the card and the settings

def card_gb() -> int | None:
    """The card's memory in whole binary GB, or None without a usable GPU (a CPU-only install: nothing to share)."""
    from . import jobs
    if jobs.STATUS.get("vram_gb"):
        return int(jobs.STATUS["vram_gb"])
    torch = sys.modules.get("torch")
    try:
        if torch is not None and torch.cuda.is_available():
            return round(torch.cuda.get_device_properties(0).total_memory / 2**30)
    except Exception:   # noqa: BLE001
        pass
    return None


def available() -> bool:
    return card_gb() is not None


def _test_default() -> float | None:
    """The cap test servers run under by default (their runs mustn't take the whole card from the user), or None."""
    env = os.environ.get("UNDERCOVER_LOW_PROFILE")
    if env is not None:
        try:
            v = float(env)
        except ValueError:
            return None
        return v if v > 0 else None
    return float(TEST_LIMIT_GB) if os.environ.get("UNDERCOVER_TEST_ROOTS") else None


def settings() -> dict:
    """{on, limit_gb, burst, auto, for_games} as configured (the defaults: off, half the card, half the time)."""
    s = CONFIG.get(SECTION) or {}
    card = card_gb() or 24
    test = _test_default()
    on = bool(s.get("on", False)) if "on" in s and test is None else test is not None
    lim = s.get("limit_gb")
    try:
        lim = int(lim) if lim is not None else None
    except (TypeError, ValueError):
        lim = None
    if test is not None and "limit_gb" not in s:
        lim = int(test)
    if lim is None:
        lim = max(MIN_LIMIT_GB, card // 2)
    lim = max(MIN_LIMIT_GB, min(card, lim))
    try:
        burst = float(s.get("burst", DEFAULT_BURST))
    except (TypeError, ValueError):
        burst = DEFAULT_BURST
    burst = min(0.75, max(0.25, burst))
    games = s.get("for_games") or []
    if isinstance(games, str):
        try:
            games = json.loads(games)
        except ValueError:
            games = []
    return {"on": on, "limit_gb": lim, "burst": burst, "auto": bool(s.get("auto", False)),
            "for_games": [str(g) for g in games]}


def status() -> dict | None:
    """What the page shows ({on, limit_gb, burst, auto}), or None on a PC with no card to share (the control hides)."""
    if not available():
        return None
    s = settings()
    return {k: s[k] for k in ("on", "limit_gb", "burst", "auto")}


def on() -> bool:
    return available() and settings()["on"]


def change(**patch) -> dict:
    """Change on / limit_gb / burst / auto (only the keys given), persist them, apply the cap. -> status()."""
    from .voices import ModelError
    if not available():
        raise ModelError("There's no graphics card to share on this PC.")
    vals = {}
    if "on" in patch:
        vals["on"] = "true" if patch["on"] else "false"
    if "limit_gb" in patch and patch["limit_gb"] is not None:
        try:
            lim = int(round(float(patch["limit_gb"])))
        except (TypeError, ValueError):
            raise ModelError("The limit is a number of GB.")
        card = card_gb() or 24
        if not MIN_LIMIT_GB <= lim <= card:
            raise ModelError(f"The limit is between {MIN_LIMIT_GB} and {card} GB on this card.")
        vals["limit_gb"] = str(lim)
    if "burst" in patch and patch["burst"] is not None:
        try:
            b = float(patch["burst"])
        except (TypeError, ValueError):
            raise ModelError("The share of the time is a number between 0.25 and 0.75.")
        if not 0.25 <= b <= 0.75:
            raise ModelError("The share of the time is between a quarter and three quarters.")
        vals["burst"] = f"{b:.2f}"
    if "auto" in patch:
        vals["auto"] = "true" if patch["auto"] else "false"
    if vals:
        set_values(SECTION, vals)
        _log(f"settings: {vals}")
    if "on" in patch and not patch["on"]:
        with _LOCK:
            STATE["by_game"] = None   # switched off by hand: no game's closing has anything to undo
    from . import jobs
    if jobs.CURRENT[0] is None or not on():   # (a lower cap under a running job would end it: it waits for the next start)
        apply()
    else:
        _log("on while a job runs: the cap applies when the next job starts")
    return status()


def remember_game(exe: str, want: bool = True):
    """A game answered "Low profile" (or taken off the list): [low_profile] for_games, as the game pause's never_ask."""
    names = [n for n in settings()["for_games"] if n.lower() != exe.lower()] + ([exe] if want else [])
    set_values(SECTION, {"for_games": json.dumps(sorted(set(names), key=str.lower))})


def wants_for_game(exe: str) -> bool:
    """A full-screen game that turns Low profile on without asking: on the list, or `auto`."""
    s = settings()
    return s["auto"] or exe.lower() in (g.lower() for g in s["for_games"])


def game_on(exe: str) -> dict | None:
    """A game turned it on (the question's answer, the list, or auto): remembered, so the game's closing turns it off."""
    if not available():
        return None
    already = settings()["on"]
    with _LOCK:
        STATE["by_game"] = None if already else exe   # on already by hand: the game's end leaves it on
    if not already:
        change(on=True)
    return status()


def game_off(exe: str | None = None) -> bool:
    """The game that turned it on closed: off again (only then). -> True when it was turned off."""
    with _LOCK:
        by = STATE["by_game"]
        if by is None or (exe is not None and by.lower() != exe.lower()):
            return False
        STATE["by_game"] = None
    change(on=False)
    return True


# ---------------------------------------------------------------- the cap, split between the two processes

WHISPER_GB = 5.0   # the worker's transcripts: Whisper large-v3-turbo loads at full precision (3.6 GB ran out, 2026-10-24)


def worker_running() -> bool:
    try:
        from .cadence import client
        return client.WORKER.running()
    except Exception:   # noqa: BLE001 - no worker here
        return False


def worker_share(engine: str | None = None) -> float:
    """An engine's share (the engines table's graphics memory); with none given, the share the running worker was
    started with (0 when there's no worker)."""
    from .cadence import engine_info
    if engine is None:
        return (STATE.get("worker_cap") or worker_need("speech")) if worker_running() else 0.0
    info = engine_info(engine) or {}
    return float(info.get("vram_gb") or 3.6)


def worker_engines() -> set:
    """The engines a worker started now may load: the configured one, and every engine a voice's pack is pinned to."""
    out = {(CONFIG.get("cadence") or {}).get("engine") or "chatterbox-turbo"}
    try:
        from .voices import listed_voices
        for v in listed_voices():
            pin = (((v.extra or {}).get("tts") or {}).get("engine_pin"))
            if pin:
                out.add(pin)
    except Exception:   # noqa: BLE001 - the configured one at least
        pass
    return out


def worker_need(kind: str) -> float:
    """What the worker holds for a kind of job: the largest engine it may read with (the configured one, every voice's
    pinned one) for speech; Whisper for transcripts; both at once for a cadence training (it reads, then hears each
    reading back: 3.6 + 5 GB, the engine and Whisper loaded together ran out of 5 GB, 2026-10-24)."""
    eng = max(worker_share(engine=e) for e in worker_engines())
    if kind == "train-transcribe":
        return WHISPER_GB
    if kind in ("cadence", "newcad"):
        return eng + WHISPER_GB
    return eng


def reserve_gb() -> float:
    """The share kept for the worker by the job about to use it (before_job sets it), else speech's."""
    return STATE.get("worker_next") or worker_need("speech")


def worker_limit_gb() -> float | None:
    """The cap a cadence worker about to start gets (its environment; it can't move later): the share of the job that
    starts it, or None when off."""
    if not on():
        return None
    s = settings()
    cap = round(min(float(s["limit_gb"]) - 1.0, reserve_gb()), 2)
    STATE["worker_cap"] = cap
    return cap


WORKER_KINDS = ("speech", "voicepreview", "speakwarm", "train-transcribe", "cadence", "newcad")   # jobs that read with the worker


def kind_of(job) -> str:
    """A job's kind as Low profile sees it: an overnight item's own (new, improve, cadence, newcad), not "night"."""
    return getattr(job, "lp_kind", None) or getattr(job, "kind", "") or ""


def server_limit_gb(reserve_worker: bool = False) -> float | None:
    """The server process's cap right now: the limit less the worker's share while it's loaded (or, with reserve_worker,
    as if it were: a job that will start the worker gets the cap it ends with, not one pulled from under it); None when
    off."""
    if not on():
        return None
    s = settings()
    lim = float(s["limit_gb"])
    share = max(reserve_gb(), worker_share()) if reserve_worker else worker_share()
    return round(max(1.0, lim - share), 2)


def apply(reserve_worker: bool = False):
    """Set the server's cap from the settings and the worker's state (called on every change, worker start/stop, job
    start). Raising the fraction is as cheap as lowering it; nothing is re-set when nothing changed."""
    from . import gpu
    lim = server_limit_gb(reserve_worker)
    key = (on(), lim)
    with _LOCK:
        if STATE["applied"] == key:
            return
        STATE["applied"] = key
        STATE["server_gb"] = lim
        STATE["worker_gb"] = worker_share() or None
    got = gpu.apply_cap(lim if lim is not None else float(card_gb() or 0) or None)
    from . import jobs
    jobs.STATUS["gpu_limit_gb"] = lim
    _log(f"cap: server {lim if lim is not None else 'whole card'} GB, worker {STATE['worker_gb'] or 'none'} GB"
         + ("" if got is not None or lim is None else " (couldn't be applied)"))


TRAIN_KINDS = ("train", "train-auto", "train-prepare", "new", "improve", "newcad", "night")   # (newcad: a training, then its cadence)


def before_job(job) -> bool:
    """The GPU worker, before a queued job runs: what the job doesn't need is freed, a training gets the whole limit.
    -> True to run; None when the job doesn't fit the cap (said once; the caller puts it back at the end of the queue,
    so the jobs behind it that do fit aren't held), False when it was cancelled."""
    if getattr(job, "cancel", False):
        return False
    if not on():
        return True
    kind = kind_of(job)
    need = 0.0
    if kind in WORKER_KINDS:   # (the worker's share for this job; a worker started with less is ended first: its cap can't grow)
        need = round(min(float(settings()["limit_gb"]) - 1.0, worker_need(kind)), 2)
        STATE["worker_next"] = need
        if worker_running() and (STATE.get("worker_cap") or 0) < need:
            try:
                from .cadence import client
                client.WORKER.stop(f"Low profile: a {kind} needs a bigger share ({need} GB)")
            except Exception:   # noqa: BLE001
                pass
    else:
        STATE["worker_next"] = None
    if kind in TRAIN_KINDS:
        before_training()
        free_for("train")
    elif kind in WORKER_KINDS:
        free_for("speak")
    apply(reserve_worker=kind in WORKER_KINDS and kind not in TRAIN_KINDS)
    ok, why = fits(kind)
    if not ok and worker_running() and (STATE.get("worker_cap") or 0) > need:
        # a worker loaded with a bigger share than this job's (a cadence training's, before a speech): its share comes
        # back; the next request starts it with the right one
        try:
            from .cadence import client
            client.WORKER.stop(f"Low profile: its share goes to a {kind}")
        except Exception:   # noqa: BLE001
            pass
        apply(reserve_worker=kind in WORKER_KINDS and kind not in TRAIN_KINDS)
        ok, why = fits(kind)
    if ok:
        return True
    if why != getattr(job, "_lp_said", None):   # (said once per reason; the job goes to the back of the queue, looked at again)
        job._lp_said = why
        try:
            job.push(stage="waiting", message=f"Waiting for Low profile: this {kind} {why}. Turn it off, or raise its limit, "
                                              "and it starts.", waits_for="Low profile")
        except Exception:   # noqa: BLE001 - only a message
            pass
    return None


def after_job(job):
    """A finished GPU job teaches what its kind needs (its allocated peak), under the cap or not."""
    if getattr(job, "state", None) == "done":
        record_need(kind_of(job), getattr(job, "gpu_alloc_gb", None))


def before_training():
    """A training gets the whole limit: the cadence worker stops first (its next request starts it again)."""
    if not on():
        return
    try:
        from .cadence import client
        if client.WORKER.running():
            client.WORKER.stop("Low profile: a training gets the whole limit")
    except Exception:   # noqa: BLE001
        pass
    apply()


def free_for(step: str):
    """Under the cap, what the next step doesn't need goes (the separator before a conversion, both before a training,
    listen-back's ear when nothing speaks), rather than the reserved pile counting against the user's limit."""
    if not on():
        return
    from . import gpu
    gpu.release(step)


# ---------------------------------------------------------------- bursts

_BURST = {"at": 0.0, "b": 1.0}


def burst() -> float:
    """The share of the time to work (1.0 when off), read from the settings once a second (a step loop asks on every step)."""
    now = time.monotonic()
    if now - _BURST["at"] > 1.0:
        _BURST["b"] = settings()["burst"] if on() else 1.0
        _BURST["at"] = now
    return _BURST["b"]


def gap(work_s: float):
    """After a step that took work_s seconds of GPU work: wait for the GPU, then pause so UnderCover works `burst` of
    the time. Gaps are between 20 ms and a second (a game renders dozens of frames in them; nothing stalls for long).
    A no-op when Low profile is off."""
    b = burst()
    if b >= 1.0 or work_s <= 0:
        return
    torch = sys.modules.get("torch")
    try:
        if torch is not None and torch.cuda.is_available():
            torch.cuda.synchronize()
    except Exception:   # noqa: BLE001
        pass
    time.sleep(min(1.0, max(0.02, work_s * (1.0 - b) / b)))


class Pacer:
    """One step loop's clock for gap(): tick() after each step pauses in proportion to the step's own time."""
    def __init__(self):
        self.t = time.perf_counter()

    def tick(self):
        now = time.perf_counter()
        gap(now - self.t)
        self.t = time.perf_counter()


def train_accum(bs: int) -> int:
    """How many micro-batches a training step is made of under the cap: 2 (the plan's batch in two halves, the gradients
    added up, one optimizer step: the same samples a step and the same steps as the plan, so the voice is the same
    voice) when the cap is below what a training needs here, or below 10 GB before any training has been measured
    (batch 8 reached 11.8 GB on the maintainers' card); else 1. Never a micro-batch under 2."""
    lim = server_limit_gb()
    if lim is None or bs < 4:
        return 1
    need = need_gb("train")
    return 2 if (need is not None and need > lim) or (need is None and lim < 10) else 1


def factor() -> float:
    """How much longer work takes under Low profile, for an estimate with no measurement of its own yet: 1 / burst."""
    return 1.0 / burst() if on() else 1.0


def speed_key(key: str) -> str:
    """Measured speeds learned under Low profile are kept apart from the full card's: the key gets '-lp'."""
    return f"{key}-lp" if on() else key


# ---------------------------------------------------------------- what a kind of job needs

def _need_file() -> Path:
    return path("cache_dir") / NEED_FILE


def record_need(kind: str, alloc_gb: float | None):
    """Remember a finished GPU job's allocated peak under its kind (the last eight)."""
    if not alloc_gb or alloc_gb <= 0:
        return
    from . import fsutil
    with _LOCK:
        try:
            d = json.loads(_need_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            d = {}
        d[kind] = (d.get(kind, []) + [round(float(alloc_gb), 2)])[-8:]
        try:
            fsutil.write_text(_need_file(), json.dumps(d))
        except OSError:
            pass


def need_gb(kind: str) -> float | None:
    """About what a kind of job needs on the card: the median of its last allocated peaks plus the margin, or None
    when it hasn't run here yet (then it runs, and a failure teaches it)."""
    try:
        xs = sorted(json.loads(_need_file().read_text(encoding="utf-8")).get(kind) or [])
    except (OSError, ValueError):
        return None
    return round(xs[len(xs) // 2] * NEED_MARGIN, 1) if xs else None


def fits(kind: str) -> tuple[bool, str]:
    """Whether a job of this kind fits the server's cap now (for a kind that reads with the worker: beside the worker's
    share). -> (True, "") or (False, the plain reason)."""
    lim = server_limit_gb(reserve_worker=kind in WORKER_KINDS and kind not in TRAIN_KINDS)
    if lim is None:
        return True, ""
    total = float(settings()["limit_gb"])
    if kind in WORKER_KINDS and worker_need(kind) + 1.0 > total:   # (the worker's part alone is more than the whole limit)
        return False, f"needs about {worker_need(kind) + 1.0:.0f} GB; Low profile allows {total:.0f}"
    need = need_gb(kind)
    if need is None or need <= lim:
        return True, ""
    return False, f"needs about {need:.0f} GB; Low profile allows {lim:.0f}"


def wait_to_fit(job, kind: str, sleep=time.sleep) -> bool:
    """A job that can't fit says so and waits for the cap to lift (Low profile off, or a higher limit); it doesn't run
    out of memory. -> True to run, False when it was cancelled meanwhile."""
    said = None
    while True:
        ok, why = fits(kind)
        if ok:
            return True
        if getattr(job, "cancel", False):
            return False
        if why != said:
            said = why
            try:
                job.push(stage="waiting", message=f"Waiting for Low profile: this {kind} {why}. Turn it off, or raise its limit, "
                                                  "and it starts.", waits_for="Low profile")
            except Exception:   # noqa: BLE001 - only a message
                pass
        sleep(2)


def learn_from_oom(kind: str, alloc_gb: float | None, lim: float | None):
    """An out-of-memory under the cap: the kind needs more than the cap allowed; remember at least that much."""
    need = max(float(alloc_gb or 0), float(lim or 0)) * 1.1
    if need > 0:
        record_need(kind, need / NEED_MARGIN)


def _log(msg: str):
    from .tray import log   # (one line in logs/server.log, through the server's own stdout when that is the file)
    try:
        log(f"Low profile: {msg}")
    except Exception:   # noqa: BLE001 - only a log line
        pass
