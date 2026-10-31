"""Duets by sections (1.2): one song, two voices taking turns. The lead vocal is converted once in each voice (the
pitch track read once, `Converter.shared_f0`), then the two takes are stitched by section. A voice changes only where
the lead itself is silent (SILENCE_S or more, SILENCE_DB under its loud level), in the middle of that silence, with a
short equal-power crossfade, so no join falls inside a word. The backing is converted the same way, in each section's
singer, when a cover would convert it (pipeline._duet_backing).

The sung lines are the lead's own: stretches of singing between those silences, a line too short to be sung on its own
joined to the next. Whisper's words on the whole vocal (lead and backing, so a thin lead still has its lyrics) only say
which lines are a chorus: where runs of NGRAM words come back later in the song. Its word times are never used for a
cut (they stop early on a held note, so a gap in them can be the singer still singing: research, 2026-10-29). Two ways
to share it (WAYS): "turns" (verses alternate; each chorus goes to the one who sang the verse before it; a section
whose melody fits one singer's range clearly better goes to that one, turns_by_register) and "lines" (the lines
alternate). DUET_DEFAULT is the one a duet gets when nobody says. Two voices singing the same line at once
sounded fake to the ear (2026-10-29), so a plan has one singer at a time. A song Whisper hears too little of
(MIN_WORDS) has no chorus to find: the lines alternate every two.

Songs sung as duets (two singers in harmony at once) are another thing: core.separate.lead_missing finds them and the
cover is sung by one voice, as before (pipeline.DUET_NOTE). The plan is `[[start_s, end_s, who], ...]`, who A or B,
covering the song from 0; `check_plan` is the one gate for a plan that comes from outside (the history's Make again)."""
import math
import re
from pathlib import Path

import numpy as np

DUET_DEFAULT = "turns"   # the ear decides (the blind pairs): a one-line change
WAYS = ("turns", "lines")
BACKINGS = ("singers", "original")   # a duet's backing: in the singers' voices (backing_plan), or the song's own
BACKING_DEFAULT = "singers"   # the user's favourite (2026-10-30): a singer harmonising with himself
WHO = ("A", "B")
SILENCE_DB, SILENCE_S = 35.0, 0.10            # where a voice may change: the lead this far down, this long
MIN_PHRASE_S = 0.3                            # shorter bursts of sound aren't a line
LINE_MIN_S, JOIN_GAP_S = 1.5, 0.6             # "Hello," alone isn't a sung line: it joins the next across a short gap
NGRAM, NGRAM_APART = 6, 15                    # a chorus: 6 words that come back 15+ words later
MIN_WORDS = 20                                # fewer heard: no chorus to find
FADE_S = 0.04                                 # the crossfade at a change
MAX_PARTS = 2000                              # a plan longer than this is no song's (check_plan)
FAR_ST = 3                                    # a singer this many semitones from their own best key is named (joint_fit)
REGISTER_MARGIN = 0.04                        # a section goes to the singer it fits when the other's fit there costs this
                                              # much more (fit.pick_fit). On three songs, notes under a range cost 0.04-0.14
                                              # (Hello's low verses: the user gave the first to the low voice, 2026-10-31);
                                              # two singers a semitone apart in the shared key differ by 0.025
MIN_SHARE = 0.25                              # each duet singer keeps this much of the sung melody or more (with_floor)
SR16 = 16000


def _db(x: np.ndarray, sr: int = SR16) -> np.ndarray:
    hop = sr // 100
    n = len(x) // hop
    return 10 * np.log10((x[:n * hop].reshape(n, hop).astype(np.float64) ** 2).mean(1) + 1e-12)


def _norm(w: str) -> str:
    return re.sub(r"[^a-z0-9']", "", w.lower())


def repeated(ws: list) -> set:
    """The indexes of words inside a run of NGRAM words that comes back NGRAM_APART or more words away."""
    t = [_norm(w[0]) for w in ws]
    seen: dict = {}
    for i in range(len(t) - NGRAM + 1):
        if all(t[i:i + NGRAM]):
            seen.setdefault(tuple(t[i:i + NGRAM]), []).append(i)
    out = set()
    for starts in seen.values():
        if len(starts) > 1 and max(starts) - min(starts) >= NGRAM_APART:
            for i in starts:
                out.update(range(i, i + NGRAM))
    return out


def _sung_runs(x16: np.ndarray) -> list:
    """A stem's singing: stretches between silences of SILENCE_S or more (SILENCE_DB under its loud level), [[t0, t1]]."""
    db = _db(x16)
    if not len(db):
        return []
    sung = db > np.percentile(db, 95) - SILENCE_DB
    need = int(round(SILENCE_S * 100))
    out, start, quiet = [], None, 0
    for i, v in enumerate(sung):
        if v:
            start = i if start is None else start
            quiet = 0
        elif start is not None:
            quiet += 1
            if quiet >= need:
                out.append([start / 100, (i - quiet + 1) / 100])
                start, quiet = None, 0
    if start is not None:
        out.append([start / 100, len(db) / 100])
    return out


def phrases(x16: np.ndarray) -> list:
    """The lead's sung lines: stretches of singing between silences of SILENCE_S or more (SILENCE_DB under its loud
    level). A line shorter than LINE_MIN_S joins the next when the silence between them is under JOIN_GAP_S. Each line
    but the last ends with `cut`, the middle of the silence after it: the only places a voice may change.
    -> [{t0, t1, text, rep, cut}]"""
    out = [r for r in _sung_runs(x16) if r[1] - r[0] >= MIN_PHRASE_S]
    merged = []
    for r in out:
        if merged and merged[-1][1] - merged[-1][0] < LINE_MIN_S and r[0] - merged[-1][1] < JOIN_GAP_S:
            merged[-1][1] = r[1]
        else:
            merged.append(r)
    rows = [{"t0": round(a, 2), "t1": round(b, 2), "text": "", "rep": False} for a, b in merged]
    for r, nxt in zip(rows, rows[1:]):
        r["cut"] = round((r["t1"] + nxt["t0"]) / 2, 3)
    return rows


def sections(x16: np.ndarray, ws: list) -> list:
    """The lead's lines (phrases), each with Whisper's words that fall in it and whether it's a chorus line (half its
    words or more are in a run that comes back). The words never move a cut."""
    rows = phrases(x16)
    rep = repeated(ws)
    for r in rows:
        mine = [i for i, w in enumerate(ws) if r["t0"] - 0.15 <= (w[1] + w[2]) / 2 <= r["t1"] + 0.15]
        r["text"] = " ".join(str(ws[i][0]).strip() for i in mine)
        r["rep"] = bool(mine) and sum(i in rep for i in mine) >= len(mine) / 2
    return rows


def blocks(rows: list) -> list:
    """Verses and choruses: a run of 2+ chorus lines is a chorus (one other line inside it still is), the rest verses.
    -> [{"kind": "verse" | "chorus", "rows": [row index, ...]}]"""
    kind = ["chorus" if r["rep"] else "verse" for r in rows]
    for i in range(1, len(kind) - 1):
        if kind[i] == "verse" and kind[i - 1] == kind[i + 1] == "chorus":
            kind[i] = "chorus"
    for i, k in enumerate(kind):   # a lone chorus line among verse lines is just a line
        if k == "chorus" and (i == 0 or kind[i - 1] != "chorus") and (i == len(kind) - 1 or kind[i + 1] != "chorus"):
            kind[i] = "verse"
    out = []
    for i, k in enumerate(kind):
        if out and out[-1]["kind"] == k:
            out[-1]["rows"].append(i)
        else:
            out.append({"kind": k, "rows": [i]})
    return out


def who_sings(rows: list, way: str, fallback: bool = False) -> list:
    """Who sings each line, one singer at a time. turns: verses alternate A, B, ... from A, and each chorus goes to
    the one who sang the verse before it (A when a chorus comes first). lines: A, B, A, B. The fallback (no chorus
    to find): every 2 lines."""
    if way not in WAYS:
        raise ValueError(f"A duet is shared one of these ways: {', '.join(WAYS)}.")
    if fallback:
        return ["A" if (i // 2) % 2 == 0 else "B" for i in range(len(rows))]
    if way == "lines":
        return ["A" if i % 2 == 0 else "B" for i in range(len(rows))]
    out, who = [None] * len(rows), None
    for b in blocks(rows):
        if b["kind"] == "verse":
            who = "A" if who in (None, "B") else "B"
        for i in b["rows"]:
            out[i] = who or "A"
    return out


def plan_of(rows: list, who: list, duration: float) -> list:
    """[[start, end, who], ...] from 0 to the song's end, a part per run of lines with the same singer; the parts
    meet at the cut between two lines (in the lead's silence)."""
    if not rows:
        return [[0.0, round(duration, 3), "A"]]
    parts = []
    for i, (r, w) in enumerate(zip(rows, who)):
        start = 0.0 if i == 0 else rows[i - 1]["cut"]
        end = r.get("cut", duration) if i < len(rows) - 1 else duration
        if parts and parts[-1][2] == w:
            parts[-1][1] = round(end, 3)
        else:
            parts.append([round(start, 3), round(end, 3), w])
    return parts


def check_plan(plan, duration: float | None = None) -> list:
    """The one gate for a plan from outside: a list of [start, end, who], who A or B, starting at 0, each part
    starting where the last ended, times finite and in order, at most MAX_PARTS. -> the plan as floats. ValueError."""
    bad = ValueError("That duet plan isn't one UnderCover can use. Make the duet again from its song.")
    if not isinstance(plan, list) or not 1 <= len(plan) <= MAX_PARTS:
        raise bad
    out, at = [], 0.0
    for p in plan:
        if not isinstance(p, (list, tuple)) or len(p) != 3 or p[2] not in WHO:
            raise bad
        try:
            a, b = float(p[0]), float(p[1])
        except (TypeError, ValueError, OverflowError):   # (an integer too big for a float)
            raise bad from None
        if not (math.isfinite(a) and math.isfinite(b)) or abs(a - at) > 1e-3 or b <= a:
            raise bad
        if duration is not None and b > duration + 1.0:
            raise bad
        out.append([round(a, 3), round(b, 3), p[2]])
        at = b
    return out


def find_plan(lead16: np.ndarray, words: list, way: str, duration: float) -> tuple[list, list, bool]:
    """-> (plan, its lines, fallback?) from the lead (16 kHz) and Whisper's words on the whole vocal."""
    fallback = len(words) < MIN_WORDS
    rows = phrases(lead16) if fallback else sections(lead16, words)
    return plan_of(rows, who_sings(rows, way, fallback), duration), rows, fallback


def _who_at(plan: list, a: float, b: float) -> str:
    """Who sings most of a..b in the plan (a tie: the earlier one)."""
    have = {}
    for s, e, w in plan:
        have[w] = have.get(w, 0.0) + max(0.0, min(b, e) - max(a, s))
    first = next((w for s, e, w in plan if e > a), plan[-1][2])
    return max(have, key=lambda w: (have[w], w == first))


def backing_plan(plan: list, x16: np.ndarray) -> list:
    """The backing's own plan: its phrases are its singing between its own silences (SILENCE_S or more, SILENCE_DB
    under its loud level), and each phrase is sung whole by whoever sings most of the lead under it. The voice changes
    only inside a silence between two phrases, at the point of it nearest the lead's change; a phrase that runs on past
    the lead's change mostly in the old singer is finished by the old singer. The user heard the backing change voice
    halfway through a phrase (2026-10-30): on Hello the backing sings from 3:26.9 to 3:51.2 with only 15-25 dB dips
    between its syllables, and its one silence near the lead's change at 3:28.7 ends at 3:26.9."""
    sang = _sung_runs(x16) if len(plan) >= 2 else []   # the backing's phrases [t0, t1]
    if not sang:
        return [list(p) for p in plan]
    who = [_who_at(plan, a, b) for a, b in sang]
    changes = [p[1] for p in plan[:-1]]
    half, end = FADE_S / 2, plan[-1][1]
    out, at, cur = [], plan[0][0], who[0]
    for k in range(1, len(sang)):
        if who[k] == cur:
            continue
        lo, hi = sang[k - 1][1] + half, sang[k][0] - half   # the silence between the two phrases
        near = min(changes, key=lambda c: 0.0 if lo <= c <= hi else min(abs(c - lo), abs(c - hi)))
        cut = round(min(max(near, lo), hi), 3)
        if at < cut < end:   # (a backing that runs on past the lead's end changes nothing there)
            out.append([at, cut, cur])
            at, cur = cut, who[k]
    out.append([at, end, cur])
    return out


def stitch(a: np.ndarray, b: np.ndarray, plan: list, sr: int) -> np.ndarray:
    """The duet's take: each part from take a or take b, with an equal-power crossfade of FADE_S centred on every
    change (inside the lead's silence). The length is the shorter take's."""
    n = min(len(a), len(b))
    weight = {"A": (1.0, 0.0), "B": (0.0, 1.0)}
    wa, wb = np.zeros(n, np.float32), np.zeros(n, np.float32)
    for s, e, w in plan:
        i, j = int(round(s * sr)), n if e >= plan[-1][1] else int(round(e * sr))
        wa[i:j], wb[i:j] = weight[w]
    half = int(sr * FADE_S / 2)
    t = np.linspace(0, np.pi / 2, 2 * half, dtype=np.float32)
    for (_, e, w0), (_, _, w1) in zip(plan, plan[1:]):
        c = int(round(e * sr))
        lo, hi = max(0, c - half), min(n, c + half)
        if hi - lo < 2:
            continue
        k = t[(lo - (c - half)):(hi - (c - half))]
        (a0, b0), (a1, b1) = weight[w0], weight[w1]
        wa[lo:hi] = a0 * np.cos(k) + a1 * np.sin(k)
        wb[lo:hi] = b0 * np.cos(k) + b1 * np.sin(k)
    return (a[:n] * wa + b[:n] * wb).astype(np.float32)


def singer_frames(plan: list, n: int) -> np.ndarray:
    """Who sings each 10 ms frame of the pitch track (n frames), by the plan: an array of "A" / "B"."""
    who = np.full(n, plan[-1][2] if plan else "A", dtype="<U1")
    for s, e, w in plan:
        who[int(round(s * 100)):int(round(e * 100))] = w
    return who


def joint_fit(ranges: dict, f0: np.ndarray, who: np.ndarray | None = None) -> dict | None:
    """One key for the song (the band moves once), each voice at its best octave in it: the key whose fit.pick_fit
    cost (at that fixed key) is lowest. With the plan's singer per frame (`who`, singer_frames), each voice's cost is
    read on the melody it sings and weighed by its share of the sung melody, so the singer who carries the most of
    the song sits nearest his own key (the user heard one sound out of place at a key shared evenly, 2026-10-30);
    without it, the costs on the whole melody are summed. ranges: {"A": (f0_range, from_speech), "B": ...}; a voice
    without a measured range is left out. -> {key, octave: {A, B}, outside: {A, B}, share: {A, B}, alone: {A: {key,
    octave}}, off: {A, B} (semitones each one sings from where he'd sing alone: + higher)} or None (neither has one)."""
    from .fit import MAX_KEY, pick_fit
    have = {k: v for k, v in ranges.items() if v and v[0]}
    if not have:
        return None
    sung = f0 > 0
    if who is None:
        mine, weight = {k: f0 for k in have}, {k: 1.0 for k in have}
    else:
        mine = {k: np.where(who[:len(f0)] == k, f0, 0.0) for k in have}
        weight = {k: float((mine[k] > 0).sum() / max(1, int(sung.sum()))) for k in have}
    best = None
    for key in sorted(range(-MAX_KEY, MAX_KEY + 1), key=abs):
        # (each cost at this key as a single voice's fit counts it: the key's own cost, and the voice ending up higher)
        per = {k: pick_fit(rng, bool(speech), mine[k], fixed_key=key) for k, (rng, speech) in have.items()}
        cost = sum(weight[k] * x["cost"] for k, x in per.items())
        if best is None or cost < best["cost"] - 1e-9:
            best = {"key": key, "cost": round(cost, 4), "octave": {k: x["octave"] for k, x in per.items()},
                    "outside": {k: x["outside"] for k, x in per.items()}}
    alone = {k: pick_fit(rng, bool(speech), mine[k]) for k, (rng, speech) in have.items()}
    best["share"] = {k: round(w, 3) for k, w in weight.items()} if who is not None else None
    best["alone"] = {k: {"key": a["key"], "octave": a["octave"]} for k, a in alone.items()}
    best["off"] = {k: 12 * (best["octave"][k] - a["octave"]) + best["key"] - a["key"] for k, a in alone.items()
                   if a["register_st"] is not None}   # (a voice the plan gives no melody to has no key of its own)
    return best


def section_costs(rows: list, bl: list, f0: np.ndarray, ranges: dict, key: int, octaves: dict) -> list:
    """Each verse and chorus (blocks) priced for each singer as they'd sing it: fit.pick_fit on the section's melody
    at the song's key and that singer's octave. A section with too little melody heard has no price. -> [{cost: {A, B},
    low: the section's median under the song's}]"""
    from .fit import pick_fit
    sung = f0[f0 > 0]
    med = float(np.median(sung)) if len(sung) else 0.0
    out = []
    for b in bl:
        i, j = int(round(rows[b["rows"][0]]["t0"] * 100)), int(round(rows[b["rows"][-1]]["t1"] * 100))
        g = np.zeros_like(f0)
        g[i:j] = f0[i:j]
        v = g[g > 0]
        per = {k: pick_fit(r, bool(s), g, fixed_key=key, fixed_octave=octaves.get(k, 0)) for k, (r, s) in ranges.items()}
        out.append({"cost": {k: x["cost"] for k, x in per.items() if x["register_st"] is not None},
                    "low": bool(len(v)) and float(np.median(v)) < med, "n": int(len(v))})
    return out


def by_register(bl: list, costs: list, n_rows: int, allow: set | None = None) -> tuple[list, list]:
    """Turns with the register (2026-10-31, the user gave Hello's low first verse to the low voice). A section goes
    to the singer it fits when the other's price there (section_costs) is REGISTER_MARGIN or more above; a chorus
    right after a verse the register decided goes to whichever of the two it fits better, by any margin (the low verse
    to the low voice, its chorus to the high one). The rest keeps Turns from there: a verse to the other singer than
    the verse before it, a chorus to the singer of the verse before it. allow: only these sections may be decided by
    the register (with_floor); the others follow Turns. -> (who sings each line, [(block index, who)] for the sections
    the register gave to someone Turns wouldn't have)"""
    who, moved = [None] * n_rows, []
    last_verse, cur, decided = None, None, False
    for k, (b, c) in enumerate(zip(bl, costs)):
        verse = b["kind"] == "verse"
        default = ("A" if last_verse in (None, "B") else "B") if verse else (cur or "A")
        fits = None
        if (allow is None or k in allow) and len(c["cost"]) == 2 and c["cost"]["A"] != c["cost"]["B"]:
            d = c["cost"]["B"] - c["cost"]["A"]
            if abs(d) >= (0.0 if not verse and decided else REGISTER_MARGIN):
                fits = "A" if d > 0 else "B"
        w = fits or default
        if w != default:
            moved.append((k, w))
        if verse:
            last_verse, decided = w, fits is not None
        else:
            decided = False
        cur = w
        for i in b["rows"]:
            who[i] = w
    return who, moved


def with_floor(bl: list, costs: list, n_rows: int) -> tuple[list, list]:
    """by_register, with each singer keeping MIN_SHARE or more of the sections' sung melody (a duet should sound like
    one: on Toxic the register alone gave the low voice 87%, MasterPlan 2026-10-31). When the register would push one
    of them under it, only its strongest calls are kept (the biggest price gaps first, as many as keep both at the
    floor) and the rest follow Turns; with none kept, it's Turns as it was."""
    def short(who):
        have = {"A": 0, "B": 0}
        for b, c in zip(bl, costs):
            have[who[b["rows"][0]]] += c.get("n", 0)
        total = have["A"] + have["B"]
        return total > 0 and min(have.values()) < MIN_SHARE * total
    who, moved = by_register(bl, costs, n_rows)
    if not short(who):
        return who, moved
    gap = lambda k: abs(costs[k]["cost"]["B"] - costs[k]["cost"]["A"]) if len(costs[k]["cost"]) == 2 else 0.0
    ranked = sorted((k for k in range(len(bl)) if gap(k) > 0), key=gap, reverse=True)
    for n in range(len(ranked) - 1, 0, -1):   # (all of them is the plan above, already short)
        who, moved = by_register(bl, costs, n_rows, set(ranked[:n]))
        if not short(who):
            return who, moved
    return by_register(bl, costs, n_rows, set())


def turns_by_register(rows: list, f0: np.ndarray, ranges: dict, duration: float, pitches: dict | None = None) -> tuple[list, list]:
    """Turns whose sections go to the singer whose register fits them (by_register), priced at the two voices' key on
    the whole song (joint_fit with no plan: the same whichever of them comes first), or at the cover's own key and
    octaves (`pitches` {key, octave: {A, B}}: the fit off). The fit for two then weighs the plan this gives. (Priced
    at the fit of the plan itself, found again as the plan moved, the key followed whoever held the most and then
    everything fitted them: on Toxic the plan swung between rounds, research 2026-10-31.) Both singers need a
    measured range, and each keeps MIN_SHARE of the song or more (with_floor). -> (plan, [(kind, who, low)] for each
    section the register moved)"""
    base = who_sings(rows, "turns")
    if not rows or len([k for k in ranges if ranges[k] and ranges[k][0]]) < 2:
        return plan_of(rows, base, duration), []
    bl = blocks(rows)
    at = pitches or joint_fit(ranges, f0)
    costs = section_costs(rows, bl, f0, ranges, at["key"], at["octave"])
    who, mv = with_floor(bl, costs, len(rows))
    return plan_of(rows, who, duration), [(bl[k]["kind"], w, costs[k]["low"]) for k, w in mv]


def describe(plan: list, names: dict, moved: list | None = None) -> str:
    """The cover's note: who sings when ("Duet: 0:00 <first> · 1:07 <second> · 2:01 <first>"); a long line-by-line
    plan in a sentence. With the sections the register moved (turns_by_register), the line says so: "By range:
    <second> takes the low verses and <first> the high choruses."."""
    if len(plan) <= 10:
        line = "Duet: " + " · ".join(f"{int(a // 60)}:{int(a % 60):02d} {names[w]}" for a, _, w in plan) + "."
    else:
        line = f"Duet: {names['A']} and {names['B']} take turns line by line ({len(plan) - 1} changes)."
    if moved:
        by: dict = {}   # who -> {(low, kind): how many}, in the song's order
        for kind, w, low in moved:
            got = by.setdefault(w, {})
            got[(low, kind)] = got.get((low, kind), 0) + 1
        both = lambda xs: xs[0] if len(xs) == 1 else ", ".join(xs[:-1]) + " and " + xs[-1]
        said = [f"{names[w]}{' takes' if not i else ''} " + both([
            f"the {'low' if low else 'high'} {kind if n == 1 else {'verse': 'verses', 'chorus': 'choruses'}[kind]}"
            for (low, kind), n in what.items()]) for i, (w, what) in enumerate(by.items())]
        line += " By range: " + (" and " if all(len(w) == 1 for w in by.values()) else "; ").join(said) + "."
    return line


def heard_words(lead: Path, backing: Path | None, work: Path, cancel=None) -> list:
    """Whisper's words on the whole vocal (the lead and the backing mixed), through the speech worker; the
    recogniser is let go after. -> [[word, start, end, confidence], ...]"""
    import soundfile as sf
    from ..core.cadence import client
    from ..core.engine.convert import load_16k
    x = load_16k(lead)
    if backing is not None and Path(backing).exists():
        y = load_16k(backing)
        n = max(len(x), len(y))
        x = np.pad(x, (0, n - len(x))) + np.pad(y, (0, n - len(y)))
        x = x / max(1.0, float(np.abs(x).max()) / 0.95)
    work.mkdir(parents=True, exist_ok=True)
    f = work / "duet-vocal.wav"
    sf.write(str(f), x.astype(np.float32), SR16)
    try:
        r = client.transcribe([f], words=True, cancel=cancel)
    finally:
        f.unlink(missing_ok=True)
        try:
            client.WORKER.call("free", what="whisper", timeout=30)
        except Exception:   # noqa: BLE001 - nothing loaded to free
            pass
    return [w for w in (r[0].get("words") or []) if len(w) >= 3] if r else []
