"""The First assignment's route (the spotlight tour, 1.1; the page is web/firstrun.js):

    POST /api/firstrun   {step: 1-4} | {skip: true} | {done: true} | {replay: true} -> {tour: {step, skipped, done}}

/api/status carries the same `tour` (app/firstrun.tour_status). `first_run` on the status is the models download's flag,
so the tour has its own name.
"""
from . import firstrun
from ..core.voices import ModelError


def handle(h, method: str, p: str) -> bool:
    if p != "/api/firstrun" or method != "POST":
        return False
    try:
        b = h._body()
        h._json({"tour": firstrun.tour_change(step=b.get("step"), skip=bool(b.get("skip")), done=bool(b.get("done")),
                                              replay=bool(b.get("replay")))})
    except ModelError as e:
        h._error(str(e))
    return True
