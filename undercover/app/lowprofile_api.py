"""Low profile's route (core/lowprofile.py; the page's switch and its Deep Cover panel, the tray, a paired phone):

    POST /api/lowprofile   {on?, limit_gb?, burst?, auto?} -> {low_profile: {on, limit_gb, burst, auto}}

/api/status carries the same `low_profile` (absent on a PC with no graphics card to share: the control stays hidden).
"""
from ..core import lowprofile
from ..core.voices import ModelError


def handle(h, method: str, p: str) -> bool:
    if p != "/api/lowprofile":
        return False
    if method != "POST":
        return False
    try:
        b = h._body()
        patch = {k: b[k] for k in ("on", "limit_gb", "burst", "auto") if k in b}
        h._json({"low_profile": lowprofile.change(**patch)})
    except ModelError as e:
        h._error(str(e))
    return True
