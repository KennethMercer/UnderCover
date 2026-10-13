"""The ear everywhere (thumbs): a thumb up or down on a cover or a piece of speech, with optional reasons and a note, so
UnderCover learns the user's taste: which voices and settings to offer first. It never changes or deletes anything on
its own; the ear decides (the user's answers, 2026-10-12: a thumbs down offers one fix when its reason has one; thumbs
on the players and the detail pane, marks in lists; liked voices rise in FrontMan's basic voice list).

Stored in `ear.json` next to the config: one JSON line per thumb, appended ({item, kind, id, thumb, voice, settings,
reasons, note, at}); the latest line for an item wins. Forgetting writes the file again without that item (or empty).
A half-written last line (a power cut) is skipped. Goes with a Vault backup; never leaves this PC.

Callers (app/ear_api.py) look the item up themselves and pass its voice and settings: core imports no module.
"""
import json
import threading
import time
from pathlib import Path

from . import config, fsutil

KINDS = ("cover", "speech")
THUMBS = ("up", "down")
# The chips, by kind and thumb (ids are stable; the page may say "him" / "her" for "them")
REASONS = {
    "cover": {"down": [("unlike", "Doesn't sound like them"), ("robotic", "Robotic or warbly"), ("key", "Wrong key"),
                       ("music", "Music drowns the voice"), ("mushy", "Words are mushy")],
              "up": [("like", "Sounds like them"), ("clean", "Clean voice"), ("mix", "Good mix")]},
    "speech": {"down": [("unlike", "Doesn't sound like them"), ("robotic", "Robotic"), ("pace", "Pace is off"),
                        ("words", "Mispronounced words"), ("pauses", "Odd pauses")],
               "up": [("like", "Sounds like them"), ("natural", "Natural pace")]},
}
# The settings counted in "what it learned" (Deep Cover), per kind
SETTINGS = {"cover": ("octave", "key", "quality", "dereverb", "karaoke", "polish", "pitch_cleanup"),
            "speech": ("pace", "mode", "reader")}
NOTE_MAX = 500
PREFER_MARGIN = 0.03   # "equally good": voice-check scores this close are a tie the ear breaks

_LOCK = threading.RLock()
_CACHE: dict = {"key": None, "items": {}, "voices": {}, "lines": 0}


class EarError(ValueError):
    """A request the ear can't take (said to the user as written)."""


def ear_file() -> Path:
    return Path(config.CONFIG_FILE).with_name("ear.json")


def _key(kind: str, item_id: str) -> str:
    return f"{kind}:{item_id}"


def items() -> dict:
    """{"cover:<id>": the latest thumb's record} (cached until the file changes)."""
    f = ear_file()
    with _LOCK:
        try:
            st = f.stat()
        except OSError:
            return {}
        key = (str(f), st.st_mtime_ns, st.st_size)
        if _CACHE["key"] != key:
            out, lines = {}, 0
            for line in f.read_text(encoding="utf-8").splitlines():
                lines += 1
                try:
                    r = json.loads(line)
                except ValueError:
                    continue   # a half-written line
                if isinstance(r, dict) and r.get("item"):
                    out[r["item"]] = r
            voices: dict = {}
            for r in out.values():
                if r.get("voice"):
                    t = voices.setdefault(r["voice"], {"up": 0, "down": 0})
                    t[r["thumb"]] += 1
            _CACHE.update(key=key, items=out, voices=voices, lines=lines)
        return dict(_CACHE["items"])


def thumb_of(kind: str, item_id: str) -> dict | None:
    return items().get(_key(kind, item_id))


def rate(kind: str, item_id: str, thumb: str, voice: str | None, settings: dict | None = None,
         reasons=(), note: str = "") -> dict:
    """Record a thumb (a new one for the same item replaces it). Reasons are chip ids for that kind and thumb."""
    if kind not in KINDS:
        raise EarError("Only covers and speech can be rated.")
    if thumb not in THUMBS:
        raise EarError("A thumb is up or down.")
    known = {r for r, _ in REASONS[kind][thumb]}
    reasons = list(dict.fromkeys(reasons or []))
    bad = [r for r in reasons if r not in known]
    if bad:
        raise EarError(f"Unknown reason: {bad[0]}.")
    note = " ".join(str(note or "").split())[:NOTE_MAX]
    keep = SETTINGS[kind]
    rec = {"item": _key(kind, item_id), "kind": kind, "id": item_id, "thumb": thumb, "voice": voice or None,
           "settings": {k: v for k, v in (settings or {}).items() if k in keep}, "reasons": reasons, "note": note,
           "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    with _LOCK:
        f = ear_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        cur = items()
        if _CACHE["lines"] > 2 * len(cur) + 200:   # mostly superseded thumbs: write it again, latest per item
            cur[rec["item"]] = rec
            fsutil.write_text(f, "".join(json.dumps(r) + "\n" for r in cur.values()))
        else:
            with open(f, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(rec) + "\n")
    return rec


def forget(kind: str | None = None, item_id: str | None = None) -> int:
    """Forget one item's thumb (kind + id), or every thumb (no arguments). Returns how many items were forgotten."""
    with _LOCK:
        cur = items()
        if kind is None and item_id is None:
            gone = list(cur)
        else:
            gone = [k for k in cur if k == _key(kind or "", item_id or "")]
        if gone:
            left = [r for k, r in cur.items() if k not in gone]
            f = ear_file()
            if left:
                fsutil.write_text(f, "".join(json.dumps(r) + "\n" for r in left))
            else:
                f.unlink(missing_ok=True)
        return len(gone)


def _label(kind: str, reason: str) -> str:
    for thumb in THUMBS:
        for r, text in REASONS[kind][thumb]:
            if r == reason:
                return text
    return reason


def summary() -> dict:
    """What the thumbs taught, for Settings → Your ear: counts, and (Deep Cover) per voice and per setting."""
    recs = list(items().values())
    voices: dict = {}
    sets: dict = {}
    for r in recs:
        if r.get("voice"):
            v = voices.setdefault(r["voice"], {"voice": r["voice"], "up": 0, "down": 0, "said": {}})
            v[r["thumb"]] += 1
            for x in r.get("reasons") or []:
                lab = _label(r["kind"], x)
                v["said"][lab] = v["said"].get(lab, 0) + 1
        for k, val in (r.get("settings") or {}).items():
            s = sets.setdefault((r["kind"], k, json.dumps(val)), {"kind": r["kind"], "setting": k, "value": val, "up": 0, "down": 0})
            s[r["thumb"]] += 1
    for v in voices.values():
        v["said"] = [lab for lab, _ in sorted(v["said"].items(), key=lambda x: -x[1])[:2]]
    return {"rated": len(recs), "up": sum(r["thumb"] == "up" for r in recs), "down": sum(r["thumb"] == "down" for r in recs),
            "voices": sorted(voices.values(), key=lambda v: (-(v["up"] - v["down"]), v["voice"])),
            "settings": sorted(sets.values(), key=lambda s: (s["kind"], s["setting"], -(s["up"] + s["down"])))}


def taste(voice: str) -> dict | None:
    """{up, down} for a voice, or None when it has no thumbs (every voice API carries it as `ear`)."""
    with _LOCK:
        items()   # (re)loads the per-voice counts with the file
        t = _CACHE["voices"].get(voice)
        return dict(t) if t else None


def liking(voice: str) -> int:
    """Thumbs up minus thumbs down: the order hook (liked voices first; a voice without thumbs is 0)."""
    t = taste(voice)
    return t["up"] - t["down"] if t else 0


def prefer(cands: list, name=lambda c: c, score=None, margin: float = PREFER_MARGIN) -> list:
    """Reorder candidates the ear's way without overruling the numbers: with `score`, only among those within `margin`
    of the best (equally good) does the liked one go first; without, liked first, the given order otherwise."""
    if not cands:
        return list(cands)
    if score is None:
        return sorted(cands, key=lambda c: -liking(name(c)))
    best = max(score(c) for c in cands)
    tie = [c for c in cands if score(c) >= best - margin]
    rest = [c for c in cands if c not in tie]
    return sorted(tie, key=lambda c: (-liking(name(c)), -score(c))) + rest


def fix_for(kind: str, reasons, voice: dict | None, settings: dict | None, item_id: str) -> dict | None:
    """The one fix a thumbs down offers when one of its reasons has one (the user's answer), else None.
    voice: {name, display, trained, person, likeness_word} as the caller knows it.
    -> {reason, say, do: {label, action, ...}, alt?: {label, action, ...}, meta?}
    Actions: "work" (POST /api/work/add with `body`), "pick_voice" (open the voice list), "remake" (POST
    /api/history/<id>/again {options}: the cover again with those changed), "respeak" (POST /api/speak with the speech's
    text, voice and reader and `options`)."""
    reasons = list(reasons or [])
    settings = settings or {}
    v = voice or {}
    name = v.get("display") or v.get("name") or "This voice"
    if kind == "speech" and "unlike" in reasons and v.get("trained") and settings.get("mode") != "cadence":
        # read by a stock Reader, not in their own cadence: their cadence is the likelier fix than more training
        return {"reason": "unlike", "say": f"This was read without {name}'s own cadence.",
                "meta": "Their cadence makes it sound like them, not just their timbre.",
                "do": {"label": "Learn their cadence tonight", "action": "work",
                       "body": {"kind": "cadence", "target": v["name"], "now": False}},
                "alt": {"label": "Try another voice", "action": "pick_voice"}}
    if "unlike" in reasons and v.get("name"):
        word = v.get("likeness_word")
        meta = (f"The voice is only a {word} likeness so far. More clips usually fix this." if word in ("fair", "poor")
                else "More clips usually fix this.")
        if v.get("trained"):
            return {"reason": "unlike", "say": f"{name} could be closer.", "meta": meta,
                    "do": {"label": "Improve the voice tonight", "action": "work",
                           "body": {"kind": "improve", "target": v["name"], "now": False}},
                    "alt": {"label": "Try another voice", "action": "pick_voice"}}
        if v.get("person"):
            return {"reason": "unlike", "say": f"{name} isn't close enough.", "meta": "A voice trained on their own clips usually is.",
                    "do": {"label": f"Train a proper {v['person']} voice tonight", "action": "work",
                           "body": {"kind": "new", "target": v["person"], "now": False}},
                    "alt": {"label": "Try another voice", "action": "pick_voice"}}
        return {"reason": "unlike", "say": f"{name} isn't close enough.", "do": {"label": "Try another voice", "action": "pick_voice"}}
    if kind == "cover" and "key" in reasons:
        octave = int(settings.get("octave") or 0)
        lower, higher = max(-2, octave - 1), min(2, octave + 1)
        do = {"label": "Make it again, an octave lower", "action": "remake", "id": item_id, "options": {"octave": lower}}
        alt = {"label": "An octave higher", "action": "remake", "id": item_id, "options": {"octave": higher}}
        if lower == octave:   # already as low as it goes
            do, alt = {**alt, "label": "Make it again, an octave higher"}, None
        out = {"reason": "key", "say": f"Too high for {name}?" if lower != octave else f"Too low for {name}?", "do": do}
        return {**out, "alt": alt} if alt and higher != octave else out
    if kind == "speech" and "pace" in reasons:
        cur = settings.get("pace") or "natural"
        slower = "natural" if cur == "slower" else "slower"
        faster = "natural" if cur == "faster" else "faster"
        return {"reason": "pace", "say": "Too fast, or too slow?",
                "do": {"label": "Read it again slower" if slower == "slower" else "Read it again at the natural pace",
                       "action": "respeak", "id": item_id, "options": {"pace": slower}},
                "alt": {"label": "Faster" if faster == "faster" else "At the natural pace", "action": "respeak", "id": item_id,
                        "options": {"pace": faster}}}
    return None


def reasons_json() -> dict:
    """The chips for the page: {kind: {thumb: [{id, label}]}}."""
    return {k: {t: [{"id": r, "label": lab} for r, lab in REASONS[k][t]] for t in THUMBS} for k in KINDS}
