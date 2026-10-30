"""Duets by sections (1.2): one song, two voices taking turns. The lead vocal is converted once in each voice (the
pitch track read once, `Converter.shared_f0`), then the two takes are stitched by section: a voice changes only at the
quietest point between two sung lines, with a short equal-power crossfade there, so no join falls inside a word.

Sections come from the vocal itself. Whisper's words on the whole vocal (lead and backing, so a thin lead still has its
lyrics) make lines: a line ends at a LINE_GAP_S gap between words or at LINE_MAX_WORDS, and a line too short to be sung
on its own joins the next. A chorus is where runs of NGRAM words come back later in the song (so it doesn't depend on
where lines fall). Two ways to share it (WAYS): "turns" (verses alternate, choruses together) and "lines" (lines
alternate). DUET_DEFAULT is the one a duet gets when nobody says: the ear's verdict on the blind pairs decides it.
A song Whisper hears too little of (MIN_WORDS) is shared by its phrases instead: every 2 phrases, never together.

Songs sung as duets (two singers in harmony at once) are another thing: core.separate.lead_missing finds them and the
cover is sung by one voice, as before (pipeline.DUET_NOTE). The plan is `[[start_s, end_s, who], ...]`, who A, B or AB,
covering the song from 0; `check_plan` is the one gate for a plan that comes from outside (the history's Make again)."""
import math
import re
from pathlib import Path

import numpy as np

DUET_DEFAULT = "turns"   # the ear decides (the duets29 blind pairs): a one-line change
WAYS = ("turns", "lines")
WHO = ("A", "B", "AB")
LINE_GAP_S, LINE_MAX_WORDS = 0.25, 10         # lines from words
LINE_MIN_WORDS, LINE_MIN_S, JOIN_GAP_S = 4, 1.5, 0.6   # "Hello," alone isn't a sung line: it joins the next
NGRAM, NGRAM_APART = 6, 15                    # a chorus: 6 words that come back 15+ words later
MIN_WORDS = 20                                # fewer heard: shared by phrases instead
QUIET_DB, GAP_S, MIN_PHRASE_S = 35.0, 0.25, 0.3   # phrases (the fallback): singing within 35 dB of the loud level
FADE_S, TOGETHER_DB = 0.04, -3.0              # the crossfade at a change; each voice's level when both sing
MAX_PARTS = 2000                              # a plan longer than this is no song's (check_plan)
SR16 = 16000


def _db(x: np.ndarray, sr: int = SR16) -> np.ndarray:
    hop = sr // 100
    n = len(x) // hop
    return 10 * np.log10((x[:n * hop].reshape(n, hop).astype(np.float64) ** 2).mean(1) + 1e-12)


def _norm(w: str) -> str:
    return re.sub(r"[^a-z0-9']", "", w.lower())


def lines(ws: list) -> list:
    """Whisper's words [[word, start, end, ...], ...] grouped into sung lines: [[word index, ...], ...]."""
    out, cur = [], []
    for i, w in enumerate(ws):
        if cur and w[1] - ws[cur[-1]][2] >= LINE_GAP_S:
            out.append(cur)
            cur = []
        cur.append(i)
    if cur:
        out.append(cur)
    split = []
    while out:   # none longer than LINE_MAX_WORDS: split at its widest gap, 3 or more words each side
        ln = out.pop(0)
        if len(ln) <= LINE_MAX_WORDS:
            split.append(ln)
            continue
        k = max(range(3, len(ln) - 2), key=lambda j: ws[ln[j]][1] - ws[ln[j - 1]][2])
        out[:0] = [ln[:k], ln[k:]]
    merged = []
    for ln in split:
        if merged:
            prev = merged[-1]
            short = len(prev) < LINE_MIN_WORDS or ws[prev[-1]][2] - ws[prev[0]][1] < LINE_MIN_S
            if short and ws[ln[0]][1] - ws[prev[-1]][2] < JOIN_GAP_S and len(prev) + len(ln) <= LINE_MAX_WORDS + LINE_MIN_WORDS:
                merged[-1] = prev + ln
                continue
        merged.append(ln)
    return merged


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


def quietest(x16: np.ndarray, a: float, b: float) -> float:
    """Where a voice may change between a and b (s): the middle of the longest run of the lead's quietest 10 ms frames
    (within 1 dB of the quietest), so the crossfade sits in the quiet, clear of the words either side."""
    db = _db(x16)
    lo, hi = max(0, int(a * 100)), max(int(a * 100) + 1, int(b * 100))
    seg = db[lo:min(hi, len(db))]
    if not len(seg):
        return round(max(a, 0.0), 3)
    low = seg <= seg.min() + 1.0
    best, start = (0, 0), None
    for i, v in enumerate(list(low) + [False]):
        if v and start is None:
            start = i
        elif not v and start is not None:
            best, start = max(best, (i - start, -start)), None
    n, s = best[0], -best[1]
    return round((lo + s) / 100 + n / 200, 3)


def sections(x16: np.ndarray, ws: list) -> list:
    """The sung lines: [{t0, t1, text, rep (a chorus line), cut (where the next line may start in another voice)}]."""
    rep = repeated(ws)
    rows = [{"t0": ws[ln[0]][1], "t1": ws[ln[-1]][2], "text": " ".join(str(ws[i][0]).strip() for i in ln),
             "rep": sum(i in rep for i in ln) >= len(ln) / 2} for ln in lines(ws)]
    for r, nxt in zip(rows, rows[1:]):
        r["cut"] = quietest(x16, r["t1"] - 0.05, nxt["t0"] + 0.05)
    return rows


def phrases(x16: np.ndarray) -> list:
    """The fallback's lines: runs of singing in the lead, split at GAP_S of quiet: [{t0, t1, text, rep, cut}]."""
    db = _db(x16)
    if not len(db):
        return []
    on = db > np.percentile(db, 95) - QUIET_DB
    out, start, quiet = [], None, 0
    for i, v in enumerate(on):
        if v:
            start = i if start is None else start
            quiet = 0
        elif start is not None:
            quiet += 1
            if quiet / 100 >= GAP_S:
                out.append((start / 100, (i - quiet + 1) / 100))
                start, quiet = None, 0
    if start is not None:
        out.append((start / 100, len(db) / 100))
    rows = [{"t0": round(a, 2), "t1": round(b, 2), "text": "", "rep": False} for a, b in out if b - a >= MIN_PHRASE_S]
    for r, nxt in zip(rows, rows[1:]):
        r["cut"] = quietest(x16, r["t1"], nxt["t0"])
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
    """Who sings each line. turns: verses alternate A, B, ... from A, choruses AB. lines: A, B, A, B. The fallback
    (phrases, no lyrics): every 2 phrases, never together."""
    if way not in WAYS:
        raise ValueError(f"A duet is shared one of these ways: {', '.join(WAYS)}.")
    if fallback:
        return ["A" if (i // 2) % 2 == 0 else "B" for i in range(len(rows))]
    if way == "lines":
        return ["A" if i % 2 == 0 else "B" for i in range(len(rows))]
    out, who = [None] * len(rows), "B"
    for b in blocks(rows):
        if b["kind"] == "verse":
            who = "A" if who == "B" else "B"
        for i in b["rows"]:
            out[i] = "AB" if b["kind"] == "chorus" else who
    return out


def plan_of(rows: list, who: list, duration: float) -> list:
    """[[start, end, who], ...] from 0 to the song's end, a part per run of lines with the same singer; the parts
    meet at the cut between two lines."""
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
    """The one gate for a plan from outside: a list of [start, end, who], who A / B / AB, starting at 0, each part
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
        except (TypeError, ValueError):
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


def stitch(a: np.ndarray, b: np.ndarray, plan: list, sr: int) -> np.ndarray:
    """The duet's lead: each part from take a, take b, or both (each TOGETHER_DB down), with an equal-power crossfade
    of FADE_S centred on every change. The length is the shorter take's."""
    n = min(len(a), len(b))
    g = 10 ** (TOGETHER_DB / 20)
    weight = {"A": (1.0, 0.0), "B": (0.0, 1.0), "AB": (g, g)}
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


def joint_fit(ranges: dict, f0: np.ndarray) -> dict | None:
    """One key for the song (the band moves once), each voice at its best octave in it: the key whose summed
    fit.pick_fit cost (at that fixed key) is lowest. ranges: {"A": (f0_range, from_speech), "B": ...}; a voice
    without a measured range is left out. -> {key, octave: {A, B}, outside: {A, B}} or None (neither has one)."""
    from .fit import KEY_COST, MAX_KEY, pick_fit
    have = {k: v for k, v in ranges.items() if v and v[0]}
    if not have:
        return None
    best = None
    for key in sorted(range(-MAX_KEY, MAX_KEY + 1), key=abs):
        g = f0 * 2.0 ** (key / 12)
        per = {k: pick_fit(rng, bool(speech), g, max_key=0) for k, (rng, speech) in have.items()}
        cost = sum(x["cost"] for x in per.values()) + len(per) * KEY_COST * abs(key)
        if best is None or cost < best["cost"] - 1e-9:
            best = {"key": key, "cost": round(cost, 4), "octave": {k: x["octave"] for k, x in per.items()},
                    "outside": {k: x["outside"] for k, x in per.items()}}
    return best


def describe(plan: list, names: dict) -> str:
    """The cover's note: who sings when ("Duet: 0:00 Obama · 1:07 both · 2:01 SpongeBob"); a long line-by-line plan
    in a sentence."""
    who = lambda w: "both" if w == "AB" else names[w]
    if len(plan) <= 10:
        return "Duet: " + " · ".join(f"{int(a // 60)}:{int(a % 60):02d} {who(w)}" for a, _, w in plan) + "."
    return (f"Duet: {names['A']} and {names['B']} take turns line by line ({len(plan) - 1} changes)"
            + ("; both on the choruses." if any(w == "AB" for _, _, w in plan) else "."))


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
