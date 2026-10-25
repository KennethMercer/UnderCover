"""The game pause (ARCHITECTURE "UI": the game pause): when a full-screen game starts while
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

from . import jobs, lowprofile, platform, worklist
from .config import CONFIG

POLL_S = 5
GONE_S = 15
STATE = {"game": None, "asking": None, "answer": None, "paused_for": None, "gone_since": None, "reason": "",
         "orphan_since": None}
_LOCK = threading.Lock()


def settings() -> dict:
    g = CONFIG.get("game_pause") or {}
    return {"ask": g.get("ask", True) is not False, "never_ask": [str(x) for x in (g.get("never_ask") or [])]}


def _ours(app: dict) -> bool:
    """UnderCover's own page full-screen (a browser in F11 on it) is never a game."""
    return "UnderCover" in (app.get("title") or "")


def _gpu_busy() -> bool:
    """Real GPU work: a job waiting for the GPU or the one on it (downloads and the like never ask)."""
    return not jobs.GPU_QUEUE.empty() or jobs.CURRENT[0] is not None


def observe(app: dict | None, now: float, busy: bool, reason: str = "") -> str | None:
    """One look at the screen (the watcher's 5 s tick, a job about to start, or a test). Updates STATE; -> "resume" when
    the work held for a game that has now gone should start again (the caller resumes), else None. reason: what the
    question says UnderCover is doing ("… full-screen while <reason>")."""
    s = settings()
    low_for, gone = None, None   # (Low profile's on / off happen after the block: they write the settings)
    with _LOCK:
        if app and not _ours(app) and app.get("exe"):
            STATE["gone_since"] = None
            same = STATE["game"] and STATE["game"].get("exe") == app["exe"]
            if not same:   # a different game (or the first): earlier answers were for that one
                STATE.update(game=app, answer=None, asking=None, reason="")
            if busy and STATE["answer"] is None and STATE["asking"] is None and lowprofile.wants_for_game(app["exe"]):
                STATE["answer"] = "low"   # Low profile for this game, as answered before (or auto): no question
                low_for = app["exe"]
            if (s["ask"] and busy and app["exe"] not in s["never_ask"] and STATE["answer"] is None
                    and STATE["asking"] is None):
                STATE["asking"] = {"exe": app["exe"], "title": app.get("title") or app["exe"], "since": now}
                STATE["reason"] = reason or "UnderCover is working on the graphics card"
        elif STATE["game"] is not None:
            STATE["gone_since"] = STATE["gone_since"] or now
            if now - STATE["gone_since"] >= GONE_S:
                exe = STATE["game"]["exe"]
                gone = (exe, STATE["paused_for"] == exe)
                STATE.update(game=None, asking=None, answer=None, paused_for=None, gone_since=None, reason="")
    if low_for:
        lowprofile.game_on(low_for)
    if gone is None:
        return None
    lowprofile.game_off(gone[0])   # (only when it was this game that turned it on)
    return "resume" if gone[1] else None


def answer(choice: str) -> dict:
    """The user's answer to the question on the screen: "low" (Low profile while the game runs, remembered for that
    game), "pause", "both" or "never"."""
    from .voices import ModelError
    if choice not in ("low", "pause", "both", "never"):
        raise ModelError("Low profile, pause, keep both running, or don't ask for this game?")
    if choice == "low" and not lowprofile.available():
        raise ModelError("There's no graphics card to share on this PC.")
    with _LOCK:
        game = STATE["game"]
        if game is None:
            raise ModelError("The game has closed; there's nothing to answer.")
        STATE.update(asking=None, answer=choice)
        exe = game["exe"]
    if choice == "pause":
        if worklist.hold()["held"]:   # already held (by you): keep it yours, so the game's end resumes nothing
            return status()
        worklist.pause_all(by="game")
        with _LOCK:
            STATE["paused_for"] = exe
    elif choice == "never":
        set_never(exe, True)
    elif choice == "low":
        lowprofile.game_on(exe)
        lowprofile.remember_game(exe)
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
    return {**st, **s, "supported": ok, "why": why, **worklist.hold()}


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
            choice, asking = STATE["answer"], STATE["asking"]
        if asking and not getattr(job, "_game_said", False):   # the job says why it isn't starting (once)
            job._game_said = True
            try:
                job.push(stage="waiting", message=f"Waiting: {asking['title']} is full-screen. Answer in the tray, "
                                                  "on any UnderCover page or on your phone.")
            except Exception:   # noqa: BLE001 - only a message
                pass
        if choice in ("both", "never", "low"):   # (low: the work goes on, under Low profile)
            return "start"
        if choice == "pause":
            while worklist.hold() == {"held": True, "held_by": "game"} and not getattr(job, "cancel", False):
                sleep(1)   # the watcher resumes when the game closes; a Resume by hand lets it start too
            return "held"
        sleep(1)


def tick(app: dict | None, now: float, busy: bool) -> str | None:
    """The watcher's one step: look, and when the game the work was paused for has gone, resume it, but only if the
    hold is still the game's own (a Resume or a Pause by hand since then wins). -> "resumed" or None"""
    if observe(app, now, busy) == "resume":
        if worklist.hold() == {"held": True, "held_by": "game"}:
            worklist.resume_all()
            return "resumed"
        return None
    # a hold the game made that nothing here remembers (UnderCover restarted mid-game): no game on the screen for
    # GONE_S, and it resumes the same way
    if worklist.hold() == {"held": True, "held_by": "game"} and STATE["game"] is None and not (app and not _ours(app)):
        STATE["orphan_since"] = STATE["orphan_since"] or now
        if now - STATE["orphan_since"] >= GONE_S:
            STATE["orphan_since"] = None
            worklist.resume_all()
            return "resumed"
    else:
        STATE["orphan_since"] = None
    return None


def _look():
    try:
        return platform.fullscreen_app()
    except Exception:   # noqa: BLE001 - not on this OS, or the window went away mid-read
        return None


def game_up() -> bool:
    """A full-screen program other than UnderCover's own page is up right now (work that can wait, the preview lines'
    backfill, doesn't start then and never asks)."""
    app = _look()
    return bool(app and not _ours(app) and app.get("exe"))


def start():
    """The watcher (the server starts it on Windows): every POLL_S seconds, one look at the screen."""
    if not platform.supports("fullscreen")[0]:
        return
    jobs.START_GATES.append(before_start)

    def loop():
        while True:
            try:
                busy = _gpu_busy()
                if busy or STATE["game"] is not None or worklist.hold()["held_by"] == "game":
                    tick(_look(), time.time(), busy)   # nothing to ask about otherwise: no look at the screen
            except Exception:   # noqa: BLE001 - the watcher never dies
                pass
            time.sleep(POLL_S)
    threading.Thread(target=loop, daemon=True, name="game-pause").start()
