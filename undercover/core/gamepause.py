"""The game pause (ARCHITECTURE ideas 2; plan docs/research/gamepause-plan.md): when a full-screen game starts while
UnderCover has GPU work, ask "Pause UnderCover while you play?" (Pause / Keep both running / Don't ask for this game),
because some games leave enough GPU for both. Asked, never assumed: no answer changes nothing.

  - a game appears while a GPU job runs: ask;
  - a queued GPU job is about to start while a game is up (and nobody said "keep both"): it waits and asks first,
    instead of pausing something already going (jobs.START_GATES);
  - Pause: worklist.pause_all(by="game") (training stops at a clean point and resumes later, the queue and the vault
    hold); Keep both: no more asking until that game closes; Don't ask: its exe goes in [game_pause] never_ask;
  - the game gone for GONE_S: resume, only if the hold is still the game's own (a Resume or Pause by hand wins).

Detection is core/platform.fullscreen_app (Windows only for now); the tray, the pages and a paired phone show the
question from GET /api/gamepause and answer with POST /api/gamepause."""
import threading
import time

from . import jobs, platform, worklist
from .config import CONFIG

POLL_S = 5
GONE_S = 15
STATE = {"game": None, "asking": None, "answer": None, "paused_for": None, "gone_since": None, "reason": ""}
_LOCK = threading.Lock()


def settings() -> dict:
    g = CONFIG.get("game_pause") or {}
    return {"ask": g.get("ask", True) is not False, "never_ask": [str(x) for x in (g.get("never_ask") or [])]}


def _ours(app: dict) -> bool:
    """UnderCover's own page full-screen (a browser in F11 on it) is never a game."""
    return "UnderCover" in (app.get("title") or "")


def _gpu_busy() -> bool:
    return not jobs.GPU_QUEUE.empty() or any(not j.finished and j.kind != "doctor" for j in list(jobs.JOBS.values()))


def observe(app: dict | None, now: float, busy: bool, reason: str = "") -> str | None:
    """One look at the screen (the watcher's 5 s tick, a job about to start, or a test). Updates STATE; -> "resume" when
    the work held for a game that has now gone should start again (the caller resumes), else None. reason: what the
    question says UnderCover is doing ("… full-screen while <reason>")."""
    s = settings()
    with _LOCK:
        if app and not _ours(app) and app.get("exe"):
            STATE["gone_since"] = None
            same = STATE["game"] and STATE["game"].get("exe") == app["exe"]
            if not same:   # a different game (or the first): earlier answers were for that one
                STATE.update(game=app, answer=None, asking=None, reason="")
            if (s["ask"] and busy and app["exe"] not in s["never_ask"] and STATE["answer"] is None
                    and STATE["asking"] is None):
                STATE["asking"] = {"exe": app["exe"], "title": app.get("title") or app["exe"], "since": now}
                STATE["reason"] = reason or "UnderCover is working on the graphics card"
            return None
        if STATE["game"] is None:
            return None
        STATE["gone_since"] = STATE["gone_since"] or now
        if now - STATE["gone_since"] < GONE_S:
            return None
        exe = STATE["game"]["exe"]
        resume = STATE["paused_for"] == exe
        STATE.update(game=None, asking=None, answer=None, paused_for=None, gone_since=None, reason="")
        return "resume" if resume else None


def answer(choice: str) -> dict:
    """The user's answer to the question on the screen: "pause", "both" or "never"."""
    from .voices import ModelError
    if choice not in ("pause", "both", "never"):
        raise ModelError("Pause, keep both running, or don't ask for this game?")
    with _LOCK:
        game = STATE["game"]
        if game is None:
            raise ModelError("The game has closed; there's nothing to answer.")
        STATE.update(asking=None, answer=choice)
        exe = game["exe"]
    if choice == "pause":
        worklist.pause_all(by="game")
        with _LOCK:
            STATE["paused_for"] = exe
    elif choice == "never":
        set_never(exe, True)
    return status()


def set_never(exe: str, never: bool) -> dict:
    """Add a program to (or take it off) the "don't ask for this game" list in config.toml."""
    import json
    from .config import set_values
    names = [n for n in settings()["never_ask"] if n.lower() != exe.lower()] + ([exe] if never else [])
    set_values("game_pause", {"never_ask": json.dumps(sorted(set(names), key=str.lower))})
    return status()


def set_ask(on: bool) -> dict:
    from .config import set_values
    set_values("game_pause", {"ask": "true" if on else "false"})
    return status()


def status() -> dict:
    s = settings()
    ok, why = platform.supports("fullscreen")
    with _LOCK:
        st = {k: STATE[k] for k in ("game", "asking", "answer", "paused_for", "reason")}
    held = worklist._state()
    return {**st, **s, "supported": ok, "why": why, "held": bool(held.get("held")), "held_by": held.get("held_by")}


def before_start(job, sleep=time.sleep, look=None, clock=time.time) -> str:
    """jobs.START_GATES: a queued GPU job about to start while a game is up waits for an answer (asked once per game).
    -> "start" (no game, kept both, never asked for this game, the game closed, the job was cancelled) or "held"
    (paused for the game: it waits until that hold ends, by the game closing or a Resume by hand)."""
    look = look or _look
    s = settings()
    if not s["ask"]:
        return "start"
    reason = f"“{job.title or 'some voice work'}” is waiting to start"
    while True:
        if getattr(job, "cancel", False):
            return "start"
        app = look()
        if not app or _ours(app) or not app.get("exe") or app["exe"] in settings()["never_ask"]:
            return "start"
        observe(app, clock(), True, reason)
        with _LOCK:
            choice = STATE["answer"]
        if choice in ("both", "never"):
            return "start"
        if choice == "pause":
            while worklist._state().get("held") and worklist._state().get("held_by") == "game" and not getattr(job, "cancel", False):
                sleep(1)   # the watcher resumes when the game closes; a Resume by hand lets it start too
            return "held"
        sleep(1)


def tick(app: dict | None, now: float, busy: bool) -> str | None:
    """The watcher's one step: look, and when the game the work was paused for has gone, resume it, but only if the
    hold is still the game's own (a Resume or a Pause by hand since then wins). -> "resumed" or None"""
    if observe(app, now, busy) == "resume":
        st = worklist._state()
        if st.get("held") and st.get("held_by") == "game":
            worklist.resume_all()
            return "resumed"
    return None


def _look():
    try:
        return platform.fullscreen_app()
    except Exception:   # noqa: BLE001 - not on this OS, or the window went away mid-read
        return None


def start():
    """The watcher (the server starts it on Windows): every POLL_S seconds, one look at the screen."""
    if not platform.supports("fullscreen")[0]:
        return
    jobs.START_GATES.append(before_start)

    def loop():
        while True:
            try:
                tick(_look(), time.time(), _gpu_busy())
            except Exception:   # noqa: BLE001 - the watcher never dies
                pass
            time.sleep(POLL_S)
    threading.Thread(target=loop, daemon=True, name="game-pause").start()
