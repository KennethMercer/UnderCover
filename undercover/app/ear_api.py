"""The ear's routes (thumbs on covers and speech; core/ear.py keeps them):

    GET  /api/ear            {items: {"cover:<id>" | "speech:<id>": {thumb, reasons, note, voice, settings, at}},
                              reasons: {cover|speech: {up|down: [{id, label}]}}, summary: {rated, up, down,
                              voices: [{voice, up, down, said}], settings: [{kind, setting, value, up, down}]}}
    POST /api/ear/rate       {kind: "cover"|"speech", id, thumb: "up"|"down", reasons?: [id], note?}
                             -> {item: {...}, fix: {reason, say, do, alt?, meta?} | null}
                             The voice and settings are read from the cover's or speech's own record, not the body.
    POST /api/ear/forget     {kind, id} one item, or {all: true} -> {forgot: n}
Phones (app/phone_api.ROUTES): all three.
"""
from ..core import ear
from ..core.ear import EarError


def handle(h, method: str, p: str) -> bool:
    if p != "/api/ear" and not p.startswith("/api/ear/"):
        return False
    try:
        _route(h, method, p)
    except EarError as e:
        h._error(str(e), 422)
    except LookupError as e:
        h._error(str(e), 404)
    return True


def _item(kind: str, item_id: str) -> tuple[str | None, dict]:
    """(voice, settings) of the cover or speech being rated (LookupError when it's gone)."""
    if kind == "cover":
        from ..frontman import history
        it = history.get(item_id)
        return it.get("voice"), dict(it.get("options") or {})
    if kind == "speech":
        from ..speakeasy import speak
        it = speak.get_speech(item_id)
        return it.get("voice"), {"pace": it.get("pace"), "mode": it.get("mode"), "reader": it.get("reader")}
    raise EarError("Only covers and speech can be rated.")


def _voice(name: str | None) -> dict | None:
    from ..core import voices as V
    from ..core.voices import ModelError, display_name, get_voice
    likeness_word = getattr(V, "likeness_word", lambda v: None)   # (phase 19's word; absent before it merges)
    if not name:
        return None
    try:
        v = get_voice(name)
    except ModelError:
        return {"name": name, "display": name}
    ex = v.extra or {}
    return {"name": v.name, "display": display_name(v), "trained": bool(ex.get("trained")), "person": ex.get("person"),
            "likeness_word": likeness_word(v)}


def _route(h, method, p):
    if p == "/api/ear" and method == "GET":
        return h._json({"items": ear.items(), "reasons": ear.reasons_json(), "summary": ear.summary()})
    if p == "/api/ear/rate" and method == "POST":
        b = h._body()
        kind, item_id = b.get("kind") or "", str(b.get("id") or "")
        voice, settings = _item(kind, item_id)
        reasons = b.get("reasons") or []
        if not isinstance(reasons, list):
            raise EarError("Reasons are a list.")
        rec = ear.rate(kind, item_id, b.get("thumb") or "", voice, settings, reasons, b.get("note") or "")
        fix = ear.fix_for(kind, reasons, _voice(voice), settings, item_id) if rec["thumb"] == "down" else None
        return h._json({"item": rec, "fix": fix})
    if p == "/api/ear/forget" and method == "POST":
        b = h._body()
        if b.get("all") is True:
            return h._json({"forgot": ear.forget()})
        if not b.get("kind") or not b.get("id"):
            raise EarError("Say which one to forget (kind and id), or all.")
        return h._json({"forgot": ear.forget(b["kind"], str(b["id"]))})
    raise LookupError("Not found")
