"""The game pause's routes (core/gamepause.py; the tray, the pages and a paired phone show its question):

    GET  /api/gamepause            {game, asking: {exe, title, since}, answer, paused_for, reason, ask, never_ask, supported, held, held_by}
    POST /api/gamepause            {answer: pause|both|never}: the answer to the question on the screen
    POST /api/gamepause/settings   {ask: bool} and/or {exe, never: bool}: switch the asking off / on, edit "don't ask" list
"""
from ..core import gamepause
from ..core.voices import ModelError


def handle(h, method: str, p: str) -> bool:
    if p not in ("/api/gamepause", "/api/gamepause/settings"):
        return False
    try:
        if method == "GET" and p == "/api/gamepause":
            h._json(gamepause.status())
        elif method == "POST" and p == "/api/gamepause":
            h._json(gamepause.answer(str(h._body().get("answer") or "")))
        elif method == "POST" and p == "/api/gamepause/settings":
            b = h._body()
            if "ask" in b:
                gamepause.set_ask(bool(b["ask"]))
            if b.get("exe"):
                gamepause.set_never(str(b["exe"])[:200], bool(b.get("never", True)))
            h._json(gamepause.status())
        else:
            return False
    except ModelError as e:
        h._error(str(e))
    return True
