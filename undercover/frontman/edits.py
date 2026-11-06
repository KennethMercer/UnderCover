"""A cover's edit list (ARCHITECTURE "Edit list"): one file per cover, cache/converted/edits/<cover id>/edit.json, its snippets
beside it. The Deep Cover words editor and a future AI edit it the same way: an edit asks for a singer or an octave over a
stretch, given by times, by words or by a section. check_edit is the one gate, and it answers before any GPU. A snippet is
converted, laid over the cover's base takes, and the whole song is mixed again, since a stretch mixed alone comes out of
balance. Undo moves a cursor. Nothing touches the cover until Keep, which writes a new one beside it.

A file here is read back as data, never trusted: load() checks every field, and file names come from an edit's id and
nowhere else.
"""
import hashlib
import json
import math
import os
import re
import shutil
import time
import unicodedata
from dataclasses import fields, replace
from pathlib import Path

import numpy as np

from ..core import fsutil, storage
from ..core.config import path
from ..core.voices import ModelError, get_voice, label_of
from . import duets
from . import history as hstore
from .duets import FADE_S, WHO

VERSION = 1
OPS = ("singer", "octave", "key")
PARTS = ("lead", "backing")
BY = ("user", "ai")
ORIGINAL = "original"            # a stretch of backing as the song has it
OCTAVE_BOUND = 2
MAX_EDITS = 500                  # an edit list longer than this is no one's (the list is refused beyond it)
MAX_FILE = 4_000_000             # edit.json over this isn't one the app wrote
MAX_BYTES = 2_000_000_000        # a list's snippets, on disk
MAX_SAID = 400                   # a stored sentence (`moved`)
MAX_WORD = 40                    # a heard word quoted in a sentence (Whisper can invent a long one, and a song is anyone's)
MAX_NOTE = 200
MIN_EDIT_S = 0.3
MAX_SNAP_S = 2.0                 # an edge has to find a place to land this close, or the edit is refused
MOVED_S = 0.1                    # an edge moved less than this isn't worth saying
PAD_S = 0.5                      # context either side of a snippet, for the pitch tracker at its edges
EDGE_S = 0.05                    # this close to the song's start or end, an edge is that end
EDIT_ID = re.compile(r"e[0-9]{1,7}")   # (ids only go up, undo or not)
STATE = re.compile(r"[0-9a-f]{16}")
FIELDS = ("op", "part", "who", "octave", "t0", "t1", "words", "words_hash", "section", "by", "note")
STORED = ("id", "op", "part", "who", "octave", "by", "note", "t0", "t1", "words", "at", "moved", "files", "from_s")
SECTION_KINDS = ("verse", "chorus")
PITCH = "pitch.npz"              # the lead's and the backing's pitch tracks (Hz, 100 a second), for pricing an octave edit
MAX_PITCH = 8_000_000            # pitch.npz over this isn't one the app wrote
POOR_COST = 0.25                 # an octave edit is a poor fit when the fit's cost (fit.pick_fit) rises this much over the singer's
                                 # own octave there: a verse sung an octave up, which the ear called awful, rose 0.58;
                                 # octaves that sat well rose under 0.06 or fell (the maintainers' notes, octprice)
MIN_PRICED = 50                  # sung frames (half a second) under which a stretch isn't priced


def folder(cover_id: str) -> Path:
    if not isinstance(cover_id, str) or not hstore.ID.fullmatch(cover_id):
        raise ModelError("That isn't a cover.")
    return path("cache_dir") / "converted" / "edits" / cover_id


def _file(cover_id: str) -> Path:
    return folder(cover_id) / "edit.json"


def hash_words(words: list) -> str:
    return hashlib.sha256(json.dumps(words, separators=(",", ":")).encode("utf-8")).hexdigest()[:16]


def _num(v, say: str) -> float:
    """A finite number (the same care as duets.check_plan: NaN, Infinity, an integer too big for a float, and a bool)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ModelError(say)
    try:
        f = float(v)
    except OverflowError:
        raise ModelError(say) from None
    if not math.isfinite(f):
        raise ModelError(say)
    return f


def _octave(v) -> int:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not -OCTAVE_BOUND <= v <= OCTAVE_BOUND or v != int(v):
        raise ModelError(f"An octave is a whole number from -{OCTAVE_BOUND} to {OCTAVE_BOUND}.")
    return int(v)


def _spans(v, end: float) -> list:
    """[[a, b], ...], inside the song; anything else isn't one of ours."""
    if not isinstance(v, list):
        raise ValueError("spans")
    out = []
    for p in v:
        if not isinstance(p, list) or len(p) != 2:
            raise ValueError("spans")
        a, b = _num(p[0], "spans"), _num(p[1], "spans")
        if not 0 <= a <= b <= end + 1:
            raise ValueError("spans")
        out.append([a, b])
    return out


def _times(v, end: float) -> list:
    if not isinstance(v, list):
        raise ValueError("times")
    out = [_num(t, "times") for t in v]
    if any(not 0 <= t <= end + 1 for t in out):
        raise ValueError("times")
    return out


def _fields_of(e: dict) -> dict:
    """An edit's own fields, checked: what a stored edit and a request both must be."""
    if e.get("op") not in OPS:
        raise ModelError("UnderCover doesn't know that kind of edit.")
    if e["op"] == "key":
        raise ModelError("A change of key for part of a song isn't something UnderCover can do yet.")
    if e.get("part") not in PARTS:
        raise ModelError("An edit is to the lead or the backing.")
    out = {"op": e["op"], "part": e["part"]}
    if e["op"] == "singer":
        if e.get("who") not in (WHO if e["part"] == "lead" else WHO + (ORIGINAL,)):
            raise ModelError("That isn't one of this cover's singers.")
        out["who"] = e["who"]
    else:
        out["octave"] = _octave(e.get("octave"))
    by = e.get("by", "user")
    if by not in BY:
        raise ModelError("An edit is by the user or the AI.")
    note = e.get("note", "")
    if not isinstance(note, str) or len(note) > MAX_NOTE:
        raise ModelError(f"A note is plain text, at most {MAX_NOTE} characters.")
    return {**out, "by": by, "note": "".join(c for c in note if unicodedata.category(c)[0] != "C")}   # (no controls, no direction overrides)


# ------------------------------------------------------------ the file


def load(cover_id: str) -> dict | None:
    """The cover's list, every field checked, or None: no list, or one that isn't sound (the cover is as it was made)."""
    f = _file(cover_id)
    try:
        if not f.is_file() or f.stat().st_size > MAX_FILE:
            return None
        doc = json.loads(f.read_text(encoding="utf-8"))
        return _sound(doc, cover_id)
    except (OSError, ValueError, KeyError, TypeError, RecursionError, ModelError):
        return None


def _sound(doc, cover_id: str) -> dict:
    if not isinstance(doc, dict) or doc.get("version") != VERSION or doc.get("cover") != cover_id:
        raise ValueError("not ours")
    words = doc["words"]
    if not isinstance(words, list) or doc["words_hash"] != hash_words(words):
        raise ValueError("words")
    for w in words:
        if not isinstance(w, list) or not 3 <= len(w) <= 4 or not isinstance(w[0], str):
            raise ValueError("words")
        for x in w[1:]:
            _num(x, "words")
    duration = _num(doc["duration"], "duration")
    if duration <= 0:
        raise ValueError("duration")
    doc["plan"], doc["backing_plan"] = duets.check_plan(doc["plan"], duration), duets.check_plan(doc["backing_plan"], duration)
    if not isinstance(doc["sections"], list):
        raise ValueError("sections")
    for s in doc["sections"]:
        if s["kind"] not in SECTION_KINDS:
            raise ValueError("sections")
        _num(s["t0"], "sections"), _num(s["t1"], "sections")
    octs = doc["octaves"]
    if not isinstance(octs, dict) or set(octs) != set(WHO):
        raise ValueError("octaves")
    for k in WHO:
        _octave(octs[k])
    pts = doc["points"]
    _spans(pts["lead"], duration), _spans(pts["backing"], duration), _times(pts["dips"], duration)
    if not isinstance(doc["duet"], bool) or not isinstance(doc["backing"], bool):
        raise ValueError("flags")
    from .leadgate import CHORUS_MODES
    gate = doc["gate"]
    _spans(gate["instrument"], duration), _spans(gate["chorus"], duration)
    if gate["mode"] not in CHORUS_MODES:
        raise ValueError("gate")
    kept = doc.get("kept", [])
    if not isinstance(kept, list) or len(kept) > MAX_EDITS or any(not isinstance(k, str) or not STATE.fullmatch(k) for k in kept):
        raise ValueError("kept")
    if not isinstance(doc["edits"], list) or len(doc["edits"]) > MAX_EDITS:
        raise ValueError("edits")
    seen = set()
    for e in doc["edits"]:
        if not isinstance(e, dict) or not isinstance(e.get("id"), str) or not EDIT_ID.fullmatch(e["id"]) or e["id"] in seen:
            raise ValueError("edits")
        seen.add(e["id"])
        _fields_of(e)
        t0, t1 = _num(e["t0"], "edits"), _num(e["t1"], "edits")
        if not 0 <= t0 < t1 <= duration + 1:
            raise ValueError("edits")
        if not isinstance(e.get("files"), dict) or set(e["files"]) - {e["part"]}:
            raise ValueError("edits")
        if any(n != snippet_name(e) for n in e["files"].values()):   # (no other file name is ever read from this list)
            raise ValueError("edits")
        if e["files"] and not 0 <= _num(e["from_s"], "edits") <= t0:
            raise ValueError("edits")
        moved, at, ws = e.get("moved"), e.get("at", ""), e.get("words")
        if not (moved is None or (isinstance(moved, str) and len(moved) <= MAX_SAID)) or not isinstance(at, str) or len(at) > 32:
            raise ValueError("edits")
        if ws is not None and (not isinstance(ws, list) or len(ws) != 2 or any(isinstance(i, bool) or not isinstance(i, int) or not 0 <= i < len(words) for i in ws)):
            raise ValueError("edits")
        if set(e) - set(STORED):
            raise ValueError("edits")
    c = doc["cursor"]
    if isinstance(c, bool) or not isinstance(c, int) or not 0 <= c <= len(doc["edits"]):
        raise ValueError("cursor")
    return doc


def _drop(files: list, within: Path | None = None):
    """Delete through the allow-list, as every delete here does."""
    storage.remove_files(files, [within or path("cache_dir") / "converted"])


def save(doc: dict):
    d = folder(doc["cover"])
    d.mkdir(parents=True, exist_ok=True)
    fsutil.write_text(d / "edit.json", json.dumps(doc))


def snippet_name(edit: dict) -> str:
    return f"{edit['id']}-{edit['part']}.flac"


def snap_points(lead16: np.ndarray, back16: np.ndarray | None, words: list) -> dict:
    """Where an edit's edges may land: the lead's silences and its dips at a word's start, and the backing's own silences."""
    def gaps(x):
        sang = duets._sung_runs(x)
        return [[a[1], b[0]] for a, b in zip(sang, sang[1:])]
    db = duets._db(lead16)
    floor = float(np.percentile(db, 95)) - duets.DIP_DB if len(db) else 0.0
    dips = {duets._dip(db, floor, float(w[1])) for w in words}
    return {"lead": gaps(lead16), "backing": gaps(back16) if back16 is not None else [], "dips": sorted(d for d in dips if d is not None)}


def new_doc(cover_id: str, words: list, rows: list, plan: list, bplan: list, duration: float, octaves: dict, points: dict,
            gate: dict, duet: bool, backing: bool) -> dict:
    """A cover's list before any edit. rows: duets.sections' (or phrases'), which give the verses and choruses."""
    words = [[str(w[0]), round(float(w[1]), 3), round(float(w[2]), 3), *[round(float(x), 3) for x in w[3:4]]] for w in words]
    secs = [{"kind": b["kind"], "t0": rows[b["rows"][0]]["t0"], "t1": rows[b["rows"][-1]]["t1"]} for b in duets.blocks(rows)] if rows else []
    return {"version": VERSION, "cover": cover_id, "duration": round(float(duration), 3), "words": words, "words_hash": hash_words(words),
            "sections": secs, "plan": [list(p) for p in plan], "backing_plan": [list(p) for p in bplan], "octaves": dict(octaves),
            "points": points, "gate": gate, "duet": bool(duet), "backing": bool(backing), "edits": [], "cursor": 0}


# ------------------------------------------------------------ the gate


def _address(req: dict, doc: dict) -> tuple[float, float]:
    """A request's stretch, from one of times, words or a section."""
    have = [k for k in ("words", "section") if k in req] + (["times"] if "t0" in req or "t1" in req else [])
    if len(have) != 1:
        raise ModelError("Say where: by times, by words or by a section.")
    if have == ["times"]:
        say = "Those times aren't in the song."
        return _num(req.get("t0"), say), _num(req.get("t1"), say)
    if have == ["section"]:
        s = req["section"]
        if isinstance(s, bool) or not isinstance(s, int) or not 0 <= s < len(doc["sections"]):
            raise ModelError("This song has no such section.")
        return doc["sections"][s]["t0"], doc["sections"][s]["t1"]
    if req.get("words_hash") != doc["words_hash"]:
        raise ModelError("Those words are from another version of this cover. Open it again.")
    w = req["words"]
    if not isinstance(w, list) or len(w) != 2 or any(isinstance(i, bool) or not isinstance(i, int) for i in w) \
            or not 0 <= w[0] <= w[1] < len(doc["words"]):
        raise ModelError("Those words aren't in the song.")
    return doc["words"][w[0]][1], doc["words"][w[1]][2]


def snap(t: float, part: str, doc: dict) -> tuple[float, str] | None:
    """The nearest place an edge may land -> (time, "silence" | "dip" | "end"), a silence first: nothing proves a dip is between
    two words, though the ear didn't hear one at a join (the maintainers' notes, snap point). None: no place within MAX_SNAP_S."""
    dur = doc["duration"]
    if t <= EDGE_S:
        return 0.0, "end"
    if t >= dur - EDGE_S:
        return dur, "end"
    half = FADE_S / 2
    best = None
    for a, b in doc["points"][part]:
        if b - a < FADE_S:
            continue
        p = min(max(t, a + half), b - half)
        if abs(p - t) <= MAX_SNAP_S and (best is None or abs(p - t) < abs(best - t)):
            best = p
    if best is not None:
        return round(best, 3), "silence"
    if part == "lead":
        near = [d for d in doc["points"]["dips"] if abs(d - t) <= MAX_SNAP_S]
        if near:
            return round(min(near, key=lambda d: abs(d - t)), 3), "dip"
    return None


def _word_at(doc: dict, t: float) -> str | None:
    for w in doc["words"]:
        if w[1] <= t <= w[2]:
            return w[0].strip()[:MAX_WORD]
    return None


def _passed(doc: dict, a: float, b: float) -> str | None:
    """The word an edge moved past, going from a to b."""
    lo, hi = min(a, b), max(a, b)
    got = [w[0].strip()[:MAX_WORD] for w in doc["words"] if lo <= w[1] <= hi or lo <= w[2] <= hi]
    return got[0 if b > a else -1] if got else None


def _moved(name: str, was: float, now: float, kind: str, doc: dict) -> str | None:
    say = []
    if abs(now - was) > MOVED_S:
        word = _passed(doc, was, now)
        say.append(f"The {name} moved past \"{word}\"." if word else f"The {name} moved {abs(now - was):.1f} s.")
    if kind == "dip":
        say.append(f"The {name} lands between two sung sounds, with no silence there.")
    return " ".join(say) or None


def check_edit(req, doc: dict) -> dict:
    """The one gate for an edit, from the page or an AI: every field, the stretch resolved and its edges snapped, and the plan
    it leaves checked -> the edit as it will be stored (no files yet). ModelError with a plain sentence; the list is unchanged."""
    if not isinstance(req, dict) or set(req) - set(FIELDS):
        raise ModelError("That edit has something UnderCover doesn't know in it.")
    clean = _fields_of(req)
    if clean["op"] == "singer" and clean["part"] == "lead" and not doc["duet"]:
        raise ModelError("This cover has one singer. Make it a duet first.")
    if clean["part"] == "backing" and not (doc["duet"] and doc["backing"]):
        raise ModelError("This cover's backing isn't one of its singers, so it can't be edited here.")
    live = doc["edits"][:doc["cursor"]]
    if len(live) >= MAX_EDITS:
        raise ModelError(f"A cover holds at most {MAX_EDITS} edits. Keep this one and start from it.")
    t0, t1 = _address(req, doc)
    if not 0 <= t0 < t1 <= doc["duration"] + EDGE_S:
        raise ModelError("Those times aren't in the song.")
    a, b = snap(t0, clean["part"], doc), snap(t1, clean["part"], doc)
    for got, t, name in ((a, t0, "start"), (b, t1, "end")):
        if got is None:
            word = _word_at(doc, t)
            raise ModelError(f"There's no place to change near the {name}" + (f", at \"{word}\"." if word else "."))
    if b[0] - a[0] < MIN_EDIT_S:
        raise ModelError("That's too short a stretch to change.")
    moved = " ".join(m for m in (_moved("start", t0, a[0], a[1], doc), _moved("end", t1, b[0], b[1], doc)) if m)[:MAX_SAID]
    words = [i for i, w in enumerate(doc["words"]) if a[0] <= (w[1] + w[2]) / 2 <= b[0]]
    n = 1 + max((int(e["id"][1:]) for e in doc["edits"]), default=0) if doc["edits"] else 1
    edit = {"id": f"e{n}", **clean, "t0": a[0], "t1": b[0], "words": [words[0], words[-1]] if words else None,
            "at": time.strftime("%Y-%m-%d %H:%M:%S"), "moved": moved, "files": {}}
    done = compile_list(doc, live + [edit])
    if any(p[2] == ORIGINAL and p[3] is not None for p in done["backing"]):
        raise ModelError("The song's own backing stays at its own pitch.")
    try:
        duets.check_plan(plan_of(done["lead"]), doc["duration"])
    except ValueError:
        raise ModelError("That edit would leave the song with no singer somewhere.") from None
    return edit


# ------------------------------------------------------------ what the list comes to


def _paint(parts: list, edit: dict, own: dict) -> list:
    """One edit over parts [s, e, who, octave, src, base]. octave: None is the singer's own for the song. src: the edit that
    set this part, None when it's the base's. A part painted back to what the base has there is the base's again."""
    t0, t1 = edit["t0"], edit["t1"]
    out = []
    for s, e, who, oct_, src, base in parts:
        cuts = sorted({s, e, *[t for t in (t0, t1) if s < t < e]})
        for a, b in zip(cuts, cuts[1:]):
            w, o, c = who, oct_, src
            if a >= t0 and b <= t1:
                if edit["op"] == "singer":
                    w, o = edit["who"], None
                else:
                    o = None if w != ORIGINAL and edit["octave"] == own[w] else edit["octave"]
                c = None if (w, o) == (base, None) else edit["id"]
            out.append([a, b, w, o, c, base])
    return out


def _merged(parts: list) -> list:
    out = []
    for p in parts:
        if out and out[-1][2:] == p[2:]:
            out[-1][1] = p[1]
        else:
            out.append(list(p))
    return out


def plan_of(parts: list) -> list:
    """Compiled lead parts as a plan: who sings when, in check_plan's shape."""
    out = []
    for s, e, who, *_ in parts:
        if out and out[-1][2] == who:
            out[-1][1] = e
        else:
            out.append([s, e, who])
    return out


def compile_list(doc: dict, edits: list | None = None) -> dict:
    """The edits as far as the cursor (or `edits`) over the plans -> {"lead": parts, "backing": parts}, each [s, e, who, octave,
    src, base] in order, neighbours that agree merged. No files, no GPU. plan_of makes the lead a plan in check_plan's shape, so
    the history's duet_plan stays true."""
    live = doc["edits"][:doc["cursor"]] if edits is None else edits
    out = {}
    for part, plan in (("lead", doc["plan"]), ("backing", doc["backing_plan"])):
        parts = [[s, e, w, None, None, w] for s, e, w in plan]
        for ed in live:
            if ed["part"] == part:
                parts = _paint(parts, ed, doc["octaves"])
        out[part] = _merged(parts)
    return out


def view(doc: dict) -> dict:
    """The list as the editor and an AI read it: no paths."""
    done = compile_list(doc)
    return {k: doc[k] for k in ("cover", "duration", "words", "words_hash", "sections", "plan", "backing_plan", "octaves", "duet",
                                "backing", "edits", "cursor")} | {"compiled": {k: [p[:5] for p in v] for k, v in done.items()},
                                                                  "points": doc["points"]}


# ------------------------------------------------------------ rendering


def _options(item: dict):
    from .pipeline import CoverOptions
    o = item.get("options") if isinstance(item.get("options"), dict) else {}
    known = {f.name for f in fields(CoverOptions)}
    return CoverOptions(**{"voice": item.get("voice"), **{k: v for k, v in o.items() if k in known}})


def _stems(engine, item: dict, doc: dict, opts) -> dict:
    """The stems the cover was sung from, as make_cover saw them: separated again if they've been cleaned up (about a minute), and
    the lead gate's changes made once in the list's folder."""
    from .leadgate import apply as gate_apply
    from ..core.separate import separate
    st = {k: Path(v) for k, v in (item.get("stems") or {}).items() if v}
    if not st.get("vocals") or not st.get("instrumental") or not all(p.exists() for p in st.values()):
        song = Path(item.get("song") or "")
        if not song.exists():
            raise ModelError("This cover's song isn't there any more, so it can't be edited.")
        st = separate(song, opts.dereverb, True if opts.duet_voice or opts.backing_voice else opts.karaoke, separator=engine.separator,
                      quality=opts.quality, one_voice_if_no_lead=True)
    gate = doc["gate"]
    if not gate["instrument"] and not gate["chorus"]:
        return st
    d = folder(doc["cover"])
    if gate["mode"] == "top":
        raise ModelError("This cover moved lines out of its backing in a way the editor can't follow yet.")
    made = {"vocals": d / "gate_lead_base.flac", "instrumental": d / "gate_instrumental_base.flac", "backing": d / "gate_backing_base.flac"}
    if made["vocals"].exists():   # (written last)
        return {**st, **{k: p for k, p in made.items() if p.exists()}}
    d.mkdir(parents=True, exist_ok=True)
    return gate_apply(st, {"instrument": [tuple(x) for x in gate["instrument"]], "chorus": [tuple(x) for x in gate["chorus"]]}, d, "base",
                      chorus=gate["mode"])


def base_files(item: dict, doc: dict) -> dict:
    """The two takes an edit is laid over -> {"lead": path, "backing": path or None}. A kept edit's list has its own copies."""
    d = folder(doc["cover"])
    lead, back = d / "base-lead.flac", d / "base-backing.flac"
    out = {"lead": lead if lead.exists() else Path(item.get("converted_vocal") or ""),
           "backing": back if back.exists() else (Path(item["converted_backing"]) if item.get("converted_backing") else None)}
    if not out["lead"].is_file() or (out["backing"] is not None and not out["backing"].is_file()):
        raise ModelError("This cover's working files have been cleared, so it can't be edited. Make it again, and edit that.")
    return out


def overlay(base: np.ndarray, piece: np.ndarray, at: float, s: float, e: float, sr: int) -> None:
    """Lay piece (which starts at `at` s in the song) over base from s to e, in place, with the equal-power crossfade of a voice
    change (FADE_S, centred on each edge)."""
    i, j = int(round(s * sr)), min(len(base), int(round(e * sr)))
    half = int(sr * FADE_S / 2)
    off = int(round(at * sr))
    lo, hi = max(0, i - half), min(len(base), j + half)
    lo = max(lo, off)
    hi = min(hi, off + len(piece))
    if hi <= lo:
        return
    t = np.arange(lo, hi)
    up = np.clip((t - (i - half)) / max(1, 2 * half), 0.0, 1.0) if i > 0 else np.ones(hi - lo)
    down = np.clip(((j + half) - t) / max(1, 2 * half), 0.0, 1.0) if j < len(base) else np.ones(hi - lo)
    k = np.minimum(up, down) * np.pi / 2
    base[lo:hi] = base[lo:hi] * np.cos(k) + piece[lo - off:hi - off] * np.sin(k)


def _to_rate(y: np.ndarray, sr: int, want: int) -> np.ndarray:
    if sr == want:
        return y
    from math import gcd
    from scipy.signal import resample_poly
    g = gcd(sr, want)
    return resample_poly(y, want // g, sr // g).astype(np.float32)


def _voice_of(who: str, opts) -> str:
    return opts.duet_voice if who == "B" else opts.voice


def _render(engine, item: dict, doc: dict, edit: dict, report) -> None:
    """Convert what the edit changed, one file for its stretch with PAD_S either side, the song's stems sliced and each value of
    (singer, octave) in it converted once. A stretch the edit gave back to the base needs nothing."""
    import soundfile as sf
    from ..core.engine.convert import load_16k
    mine = [p for p in compile_list(doc, doc["edits"][:doc["cursor"]] + [edit])[edit["part"]] if p[4] == edit["id"]]
    if not mine:
        return
    opts = _options(item)
    song = Path(item.get("song") or "")
    from .pipeline import conversion_settings, pitch_plan
    key, _ = pitch_plan(opts)
    s = conversion_settings(opts, song)
    stems = _stems(engine, item, doc, opts)
    src = load_16k(stems["vocals" if edit["part"] == "lead" else "backing"])
    sr = sf.info(str(base_files(item, doc)[edit["part"]])).samplerate
    at = max(0.0, edit["t0"] - PAD_S)
    end = min(len(src) / duets.SR16, edit["t1"] + PAD_S)
    chunk = src[int(at * duets.SR16):int(end * duets.SR16)]
    out = np.zeros(int(round((end - at) * sr)), np.float32)
    takes = {}
    each = sorted({(p[2], p[3]) for p in mine}, key=str)
    for n, (who, oct_) in enumerate(each):
        if who == ORIGINAL:
            takes[(who, oct_)] = _original(stems, key, at, end, sr)
            continue
        o = doc["octaves"][who] if oct_ is None else oct_
        y, ysr = engine.converter.convert(chunk, _voice_of(who, opts), replace(s, pitch=key + 12 * o),
                                          progress=lambda f, n=n: report(0.1 + 0.8 * (n + f) / len(each), "Singing the change"))
        takes[(who, oct_)] = _to_rate(y, ysr, sr)
    for p in mine:
        y = takes[(p[2], p[3])]
        y = y[:len(out)]
        s0 = at if p is mine[0] else p[0]
        s1 = end if p is mine[-1] else p[1]
        overlay(out, y, 0.0, max(0.0, s0 - at), s1 - at, sr)
    d = folder(doc["cover"])
    d.mkdir(parents=True, exist_ok=True)
    sf.write(str(d / snippet_name(edit)), np.clip(out, -1, 1), sr, subtype="PCM_24")
    edit["files"] = {edit["part"]: snippet_name(edit)}
    edit["from_s"] = round(at, 3)


def _original(stems: dict, key: float, at: float, end: float, sr: int) -> np.ndarray:
    """The song's own backing for a stretch, in the cover's key, as the mix would have it."""
    import soundfile as sf
    from .remix import key_shifted
    f = key_shifted(Path(stems["backing"]), key, formant=True) if key else Path(stems["backing"])
    fsr = sf.info(str(f)).samplerate
    y, _ = sf.read(str(f), start=int(at * fsr), stop=int(end * fsr), dtype="float32", always_2d=True)
    return _to_rate(y.mean(1), fsr, sr)


def _take(item: dict, doc: dict, part: str, base: Path, parts: list) -> Path:
    """The base with every edited part laid over it, as a file for the mix (the base itself when nothing's edited). It's the next
    take until the mix has worked (_promote)."""
    import soundfile as sf
    d = folder(doc["cover"])
    f = d / f"next-{part}.flac"
    if not any(p[4] for p in parts):
        return base
    y, sr = sf.read(str(base), dtype="float32", always_2d=False)
    if y.ndim > 1:
        y = y.mean(1)
    y = y.copy()
    by_id = {e["id"]: e for e in doc["edits"]}
    for p in parts:
        if p[4]:
            e = by_id[p[4]]
            z, zsr = sf.read(str(d / snippet_name(e)), dtype="float32", always_2d=False)
            if zsr != sr:
                raise ModelError("An edit's snippet doesn't match this cover. Undo it and make it again.")
            overlay(y, z, e["from_s"], p[0], p[1], sr)
    sf.write(str(f), np.clip(y, -1, 1), sr, subtype="PCM_24")
    return f


def _promote(d: Path, mixed: Path | None) -> Path | None:
    """The takes and the result this mix made take over from the last one's, only now that it has worked: Keep takes what's here.
    The result first: a player may hold it open, and then the takes stay with it."""
    res = None if mixed is None else d / f"result{mixed.suffix}"
    if mixed is not None:
        fsutil.retry(lambda: os.replace(mixed, res))
    _drop([f for f in d.glob("result.*") if f != res])
    for part in PARTS:
        nxt, f = d / f"next-{part}.flac", d / f"take-{part}.flac"
        if nxt.exists():
            fsutil.retry(lambda n=nxt, f=f: os.replace(n, f))
        else:
            _drop([f])
    return res


def mix(engine, item: dict, doc: dict) -> Path | None:
    """The whole song mixed again with the list as far as its cursor -> the result, in the list's folder (None when nothing's
    edited: the cover itself stands). Whole, because a stretch mixed alone comes out of balance; the rest of the song stays
    where it was (0.014 dB measured)."""
    from .pipeline import mix_settings, pitch_plan
    from .remix import remix
    d = folder(doc["cover"])
    done = compile_list(doc)
    _drop(list(d.glob("next*")))   # (what a mix that failed left: only this one's are promoted)
    if not any(p[4] for v in done.values() for p in v):
        return _promote(d, None)
    opts = _options(item)
    base = base_files(item, doc)
    stems = _stems(engine, item, doc, opts)
    key, _ = pitch_plan(opts)
    ms = mix_settings(opts, key)
    lead = _take(item, doc, "lead", base["lead"], done["lead"])
    back = _take(item, doc, "backing", base["backing"], done["backing"]) if base["backing"] else None
    return _promote(d, remix(stems["instrumental"], lead, stems["vocals"], d / "next", song=Path(item["song"]),
                             backing=back or stems.get("backing"), s=ms, backing_ref=stems.get("backing") if back else None,
                             reverb_ref=stems.get("reverb")))


def result_of(cover_id: str) -> Path | None:
    got = sorted(folder(cover_id).glob("result.*"))
    return got[0] if got else None


def apply(engine, item: dict, req: dict, report) -> dict:
    """Run one checked edit: convert it, mix with it, and only then add it, so the list never runs ahead of the audio. Anything
    past the cursor is cut off (a new edit after an undo)."""
    doc = load(item["id"])
    if doc is None:
        raise ModelError("This cover's edit list has gone. Open it again.")
    try:
        edit = check_edit(req, doc)
    except ValueError as e:
        raise ModelError(str(e)) from None
    d = folder(item["id"])
    if sum(f.stat().st_size for f in d.glob("e*.flac")) > MAX_BYTES:
        raise ModelError("This cover's edits have grown too large. Keep it, and go on from the new one.")
    report(0.05, "Singing the change")
    mine = d / snippet_name(edit)
    live = doc["edits"][:doc["cursor"]]
    new = {**doc, "edits": live + [edit], "cursor": len(live) + 1}
    try:
        _render(engine, item, doc, edit, report)
        report(0.9, "Mixing the song")
        mix(engine, item, new)
    except BaseException:   # (cancelled too)
        _drop([mine])
        raise
    save(new)
    _drop([d / snippet_name(e) for e in doc["edits"][doc["cursor"]:]])
    hstore.update(item["id"], edits=len(new["edits"]))
    if not pitch_of(new):   # (a list from before pitch tracks: read now, so the next octave edit is priced)
        try:
            from ..core.engine.convert import load_16k
            st = _stems(engine, item, new, _options(item))
            read_pitch(engine, new, load_16k(st["vocals"]), load_16k(st["backing"]) if st.get("backing") else None)
        except Exception as e:   # noqa: BLE001 - the edit stands; the next one is unpriced
            print(f"Edit list: pitch not read ({type(e).__name__}: {e}).", flush=True)
    return new


def step(engine, item: dict, to: int) -> dict:
    """Undo or redo: the cursor goes to `to` (the same answer if this runs twice), and the song is mixed again. No conversion."""
    doc = load(item["id"])
    if doc is None:
        raise ModelError("This cover's edit list has gone. Open it again.")
    if not 0 <= to <= len(doc["edits"]):
        raise ModelError("There's nothing more to undo or redo.")
    new = {**doc, "cursor": to}
    mix(engine, item, new)
    save(new)
    return new


def _state(doc: dict) -> str:
    return hashlib.sha256(",".join(e["id"] for e in doc["edits"][:doc["cursor"]]).encode("utf-8")).hexdigest()[:16]


def keep(item: dict) -> dict:
    """The edited song as a new cover in the history, the one it came from untouched. Its list comes with it, with its own copy of
    the base takes, so an undo past Keep still works when the old cover has gone. Each version is kept once, whichever way the list
    has moved between them: each new cover rolls the oldest off the tray, and the one this came from is spared that."""
    import uuid
    from .pipeline import _append_history, _safe, _unique, CoverResult
    doc, res = load(item["id"]), result_of(item["id"])
    if doc is None or res is None or not doc["cursor"]:
        raise ModelError("There's nothing edited to keep.")
    was = doc.get("kept", [])
    if _state(doc) in was:
        raise ModelError("This version is already kept.")
    if len(was) >= MAX_EDITS:
        raise ModelError("This cover has been kept as often as it can be.")
    done = compile_list(doc)
    base = base_files(item, doc)
    d = folder(doc["cover"])
    fmt = res.suffix.lstrip(".")
    title = item.get("title") or Path(item["output"]).stem
    name, tag = _safe(title)[:80], uuid.uuid4().hex[:10]
    out = _unique(path("output_dir") / f"{name} (edited)", fmt)
    work = path("cache_dir") / "converted"
    vocal, backing = work / f"{name}__edited__{tag}.flac", work / f"{name}__edited__backing__{tag}.flac" if base["backing"] else None
    o = dict(item.get("options") if isinstance(item.get("options"), dict) else {})
    if doc["duet"]:
        o["duet_plan"] = plan_of(done["lead"])
    tmp = d.parent / f"keep-{tag}"
    live = doc["edits"][:doc["cursor"]]
    try:
        shutil.copy2(res, out)
        shutil.copy2(d / "take-lead.flac" if (d / "take-lead.flac").exists() else base["lead"], vocal)
        if backing:
            shutil.copy2(d / "take-backing.flac" if (d / "take-backing.flac").exists() else base["backing"], backing)
        tmp.mkdir(parents=True)
        for e in live:
            for f in e["files"].values():
                shutil.copy2(d / f, tmp / f)
        for n, src in (("base-lead.flac", base["lead"]), ("base-backing.flac", base["backing"])):
            if src is not None:
                shutil.copy2(src, tmp / n)
        if (d / PITCH).is_file():   # (the same song: its octave edits priced from the start)
            shutil.copy2(d / PITCH, tmp / PITCH)
    except OSError:
        _drop([out], path("output_dir"))
        _drop([p for p in (vocal, backing) if p])
        storage.remove_files([], [work], dirs=[tmp])
        raise
    r = CoverResult(output=out, song=Path(item["song"]), voice=item["voice"], stems=item.get("stems") or {}, converted_vocal=vocal,
                    converted_backing=backing, notes=list(item.get("notes") or []) + ["Edited from a cover: its list came with it."],
                    seconds={}, options=o, created=time.strftime("%Y-%m-%d %H:%M:%S"), title=title)
    new = _append_history(r, spare=(item["id"],))
    try:   # (past here the new cover stands, so nothing may fail the job and run it twice)
        tmp.rename(folder(new["id"]))
        now = was + [_state(doc)]
        save({**doc, "cover": new["id"], "edits": live, "kept": now})
        save({**doc, "kept": now})
        return hstore.update(new["id"], edits=doc["cursor"], edited_from=item["id"])
    except OSError as e:
        print(f"Edit list: kept, but its list stayed behind ({type(e).__name__}: {e}).", flush=True)
        return new


def strays() -> list:
    """Folders here with no line in the history: a Keep that stopped short, or a delete that found a file in use. Every line counts,
    its cover's file there or not, and no history at all means nothing can be said: the library may be on a drive that's away."""
    root = path("cache_dir") / "converted" / "edits"
    known = {i.get("id") for i in hstore.every_line() or []}
    return [p for p in root.iterdir() if p.is_dir() and p.name not in known] if known and root.exists() else []


def clear_strays(dirs: list):
    storage.remove_files([], [path("cache_dir") / "converted" / "edits"], dirs=dirs)


def dir_of(item: dict) -> Path | None:
    """The list's folder for a history line, for whoever removes the cover."""
    i = item.get("id")
    return folder(i) if isinstance(i, str) and hstore.ID.fullmatch(i) else None


def voices_of(item: dict) -> set:
    opts = item.get("options") if isinstance(item.get("options"), dict) else {}
    return {v for v in (item.get("voice"), opts.get("duet_voice")) if v}


def title_of(item: dict) -> str:
    return f"Editing {item.get('title') or label_of(item.get('voice') or '')}"


def prepare(engine, item: dict, report) -> dict:
    """The list of a cover that has none: an older one, Make again's, or one voice's. Its words are heard here (about 18 s)."""
    from ..core.engine.convert import load_16k
    from .leadgate import plan as gate_plan
    have = load(item["id"])
    if have is not None:   # (it may have waited its turn)
        return have
    d = folder(item["id"])
    if (d / "edit.json").exists():   # (a list that can't be read is set aside, never written over)
        fsutil.retry(lambda: os.replace(d / "edit.json", d / "edit.json.bad"))
    if (d / "base-lead.flac").exists():   # a kept cover: a fresh list would put the edited plan on the original base
        raise ModelError("This edited cover's list can't be read, so it can't be edited. Make it again, and edit that.")
    if d.exists():
        _drop([*d.glob("result.*"), *d.glob("take-*.flac")])
    opts = _options(item)
    get_voice(opts.voice)
    base_files(item, {"cover": item["id"]})
    st = _stems(engine, item, {"gate": {"instrument": [], "chorus": []}, "cover": item["id"]}, opts)
    lead, back = load_16k(st["vocals"]), load_16k(st["backing"]) if st.get("backing") else None
    report(0.2, "Listening to the words")
    words = duets.heard_words(st["vocals"], st.get("backing"), path("cache_dir") / "converted")
    rows = duets.sections(lead, words) if len(words) >= duets.MIN_WORDS else duets.phrases(lead)
    dur = len(lead) / duets.SR16
    try:
        plan = duets.check_plan(opts.duet_plan, dur) if opts.duet_plan else [[0.0, round(dur, 3), "A"]]
    except ValueError as e:
        raise ModelError(str(e)) from None
    conv_back = bool(item.get("converted_backing"))
    bplan = duets.backing_plan(plan, back) if opts.duet_voice and conv_back and back is not None else [list(p) for p in plan]
    if opts.duet_backing == "other" and conv_back:
        bplan = duets.other_singer(bplan)
    gate = _gate_of(gate_plan(lead, back) if opts.lead_gate is not False else {}, opts)
    doc = new_doc(item["id"], words, rows, plan, bplan, dur, _octaves(opts), snap_points(lead, back, words), gate, bool(opts.duet_voice), conv_back)
    save(doc)
    report(0.9, "Reading the pitch")
    try:
        read_pitch(engine, doc, lead, back)
    except Exception as e:   # noqa: BLE001 - the list stands; its octave edits are priced once the next edit reads the tracks
        print(f"Edit list: pitch not read ({type(e).__name__}: {e}).", flush=True)
    return doc


def _gate_of(g: dict, opts) -> dict:
    """What the lead gate did to the stems, in the list: what it took from the lead, and what it brought in from the backing."""
    from ..core.config import CONFIG
    from .leadgate import CHORUS_MODES
    mode = opts.chorus or CONFIG["conversion"].get("chorus", "keep")
    mode = mode if mode in CHORUS_MODES else "keep"
    return {"instrument": [list(x) for x in g.get("instrument", [])], "chorus": [] if mode == "keep" else [list(x) for x in g.get("chorus", [])],
            "mode": mode}


def save_pitch(cover_id: str, tracks: dict):
    """The list's pitch tracks: {"lead": Hz per 10 ms, "backing": the same, one line held (as the backing's take reads it)}."""
    d = folder(cover_id)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / (PITCH + ".tmp")
    with open(tmp, "wb") as f:
        np.savez(f, **{k: np.nan_to_num(np.asarray(v, np.float32), nan=0.0).clip(0, 4000).astype(np.float16)
                       for k, v in tracks.items() if k in PARTS and v is not None})
    fsutil.retry(lambda: os.replace(tmp, d / PITCH))


def pitch_of(doc: dict) -> dict:
    """The list's pitch tracks, read back as data: only lead and backing, flat float16 arrays as long as the song, nothing else. {}
    when there are none, or they aren't sound (an octave edit is then not priced, and the next edit reads them again)."""
    f = folder(doc["cover"]) / PITCH
    try:
        if not f.is_file() or f.stat().st_size > MAX_PITCH:
            return {}
        out = {}
        with np.load(f, allow_pickle=False) as z:
            for k in z.files:
                a = z[k]
                if k not in PARTS or a.dtype != np.float16 or a.ndim != 1 or abs(len(a) - doc["duration"] * 100) > 200:
                    return {}
                a = a.astype(np.float32)
                if not np.isfinite(a).all() or (a < 0).any():
                    return {}
                out[k] = a
        return out
    except (OSError, ValueError, KeyError, TypeError, EOFError):
        return {}


def read_pitch(engine, doc: dict, lead: np.ndarray, back: np.ndarray | None):
    """The tracks from the stems (16 kHz), saved with the list: one RMVPE pass each, a second or two."""
    rm = engine.converter.rmvpe
    tracks = {"lead": rm.infer_from_audio(lead, thred=0.03)}
    if doc["backing"] and back is not None:
        tracks["backing"] = rm.infer_from_audio(back, thred=0.03, one_line=True)
    save_pitch(doc["cover"], tracks)


def price(item: dict, doc: dict, edit: dict) -> dict | None:
    """What the fit says of an octave edit before it's sung: for each singer it moves, the share of the stretch's notes outside that
    voice's range and how far their middle sits from where the voice is comfortable (fit.pick_fit at the cover's key), at the edit's
    octave against the singer's own. -> {"poor": bool, "said": a sentence or None, "rise": the cost over the own octave's, "outside":
    share, "own_outside": share}, the worst singer's; None when it can't be priced (not an octave edit, no pitch tracks yet, no measured range, too little singing)."""
    if edit.get("op") != "octave":
        return None
    f0 = pitch_of(doc).get(edit["part"])
    if f0 is None:
        return None
    from .fit import pick_fit
    from .pipeline import _range_of, pitch_plan
    opts = _options(item)
    key, _ = pitch_plan(opts)
    done = compile_list(doc, doc["edits"][:doc["cursor"]] + [edit])[edit["part"]]
    worst = None
    for who in sorted({p[2] for p in done if p[4] == edit["id"] and p[2] != ORIGINAL}):
        try:
            rng = _range_of(get_voice(_voice_of(who, opts)))
        except ModelError:   # (a voice that's gone isn't priced; the edit's own job says so)
            continue
        f = np.concatenate([f0[int(p[0] * 100):int(p[1] * 100)] for p in done if p[4] == edit["id"] and p[2] == who] or [np.zeros(0)])
        if rng is None or (f > 0).sum() < MIN_PRICED:
            continue
        here = pick_fit(rng[0], bool(rng[1]), f, fixed_key=key, fixed_octave=edit["octave"])
        own = pick_fit(rng[0], bool(rng[1]), f, fixed_key=key, fixed_octave=doc["octaves"][who])
        rise = here["cost"] - own["cost"]
        said = None
        if rise >= POOR_COST:
            name, apart = label_of(_voice_of(who, opts)), edit["octave"] - doc["octaves"][who]
            way = f"an octave {'up' if apart > 0 else 'down'}" if abs(apart) == 1 else f"{abs(apart)} octaves {'up' if apart > 0 else 'down'}"
            if here["outside"] > own["outside"]:
                where = "above" if here["above"] >= here["below"] else "below"
                why = f"{round(100 * here['outside'])}% of these notes are {where} the voice's range"
            else:
                why = f"these notes sit {abs(round(here['register_st'] or 0))} semitones {'above' if (here['register_st'] or 0) > 0 else 'below'} where the voice is comfortable"
            said = f"{name} {way} here is a poor fit: {why}, so it may sound strained."
        row = {"poor": rise >= POOR_COST, "said": said, "rise": round(rise, 3), "outside": here["outside"], "own_outside": own["outside"]}
        if worst is None or rise > worst["rise"]:
            worst = row
    return worst


def _octaves(opts) -> dict:
    from .pipeline import pitch_plan
    key, pitch = pitch_plan(opts)
    a = int(round((pitch - key) / 12))
    return {"A": a, "B": a if opts.duet_octave is None else int(opts.duet_octave)}


def at_make(item_id: str, opts, words: list, rows: list, lead: np.ndarray, back: np.ndarray | None, bplan: list | None, gate: dict,
            engine=None):
    """A duet's list, written when the cover is recorded: the words it heard are kept, at no cost, and the pitch tracks read (a
    second or two). bplan: the backing's plan when its singers sing it. A failure here costs a cover nothing: the words are heard
    again when the editor opens."""
    doc = new_doc(item_id, words, rows, opts.duet_plan, bplan or [list(p) for p in opts.duet_plan], len(lead) / duets.SR16, _octaves(opts),
                  snap_points(lead, back, words), _gate_of(gate, opts), True, bplan is not None)
    save(doc)
    if engine is not None:
        read_pitch(engine, doc, lead, back)
