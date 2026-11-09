"""Directions in SpeakEasy's text (tier 1): written in square brackets, plain English, applying to the sentence they sit
in. Ours are done around the engine: [pause] / [pause 2s] (silence at that spot), [louder] / [softer] (gain on the
sentence), [higher] / [lower] (a pitch offset on the sentence, held in the voice's range). The reading engine's own
tags ([laugh], [sigh], [whispering] ... Chatterbox-Turbo's, read from its tokenizer) would pass through to it, but only
those the ear has passed (ENGINE_ON: none yet). Pace and energy aren't faked: a stretched reading was heard as slurred
(2026-10-14, 2026-10-28).

An unknown tag is read as nothing and listed once under the result, never spoken; so is one of the engine's that isn't
used yet, listed apart. A bracket that isn't a direction ([sic], [1], an editor's [John]) is left as text."""
import json
import re

PAUSE_S, PAUSE_MAX_S = 0.8, 5.0   # [pause] alone; [pause 2s] up to the bound
GAIN_DB = 4.0                     # [louder] / [softer]: one step, no stacking past it
PITCH_ST = 2.0                    # [higher] / [lower]: the same
OURS = {"pause", "louder", "softer", "higher", "lower"}
ALIASES = {   # friendly short forms (plain words win: only where nothing else is meant)
    "whisper": "whispering", "whispers": "whispering", "whispered": "whispering", "laughs": "laugh", "laughing": "laugh",
    "chuckles": "chuckle", "sighs": "sigh", "coughs": "cough", "gasps": "gasp", "groans": "groan", "sniffs": "sniff",
    "cries": "crying", "cry": "crying", "clears throat": "clear throat", "surprise": "surprised",
    "sarcasm": "sarcastic", "angrily": "angry", "happily": "happy", "loud": "louder", "quieter": "softer",
    "quiet": "softer", "soft": "softer", "high": "higher", "low": "lower", "silence": "pause", "beat": "pause",
}
EDITORIAL = {"sic", "citation needed", "emphasis added", "emphasis mine", "inaudible", "ibid"}
# of the engine's own tags, the ones a reading takes: its sounds, and the styles the ear is asked about (a style ships
# only once the ear can tell it from none). The rest of its tokenizer's ([dramatic], [narration] ...) are unknown here.
SOUNDS = {"laugh", "chuckle", "sigh", "cough", "gasp", "groan", "sniff", "shush", "clear throat"}
STYLES = {"whispering", "happy", "angry"}
# the ones the ear has passed, which go to the engine. None yet (blind pairs, 2026-10-29): [laugh] was heard but not as
# real, the styles weren't told from none. The others are taken out and named as not used yet.
ENGINE_ON: frozenset = frozenset()
# a number after the name, its unit optional ([pause 2], [pause 500ms], [pause 1.5 s.]): seconds unless said otherwise
_TAG = re.compile(r"\[([A-Za-z][A-Za-z ]{0,24}?)(?:\s+(\d+(?:\.\d+)?)\s*([A-Za-z]+)?\.?)?\s*\]")
_UNITS = {**dict.fromkeys(("s", "sec", "secs", "second", "seconds"), 1.0),
          **dict.fromkeys(("ms", "msec", "msecs", "millisecond", "milliseconds"), 0.001)}
_ENGINE: dict = {}


def engine_tags(engine: str | None) -> frozenset:
    """The tags the reading engine itself knows (its tokenizer's added [word] tokens), or none."""
    if engine != "chatterbox-turbo":
        return frozenset()
    if engine not in _ENGINE:
        from ..core.config import path
        try:
            added = json.loads((path("models_dir") / "tts" / engine / "added_tokens.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            added = {}
        have = {m.group(1) for t in added if (m := re.fullmatch(r"\[([a-z][a-z ]*)\]", t))}
        _ENGINE[engine] = frozenset(have & ENGINE_ON)
    return _ENGINE[engine]


def _name(raw: str, known: frozenset) -> str | None:
    """A bracket's direction, canonical; None when it isn't one we or the engine know."""
    w = " ".join(raw.lower().split())
    if w in OURS or w in known:
        return w
    a = ALIASES.get(w)
    return a if a and (a in OURS or a in known) else None


def directions(sents: list, known: frozenset = frozenset()) -> tuple[list, list, list]:
    """SpeakEasy's sentences ([{"text", "pause"}]) with their directions applied: our tags taken out of the text and
    turned into fields (gain_db, pitch_st on the sentence; a [pause] splits it there, the piece before it ending in
    `hold` seconds of silence), the engine's tags (`known`) left in the text as it spells them, and `sounds` set on a
    piece that carries one. -> (pieces, the unknown tags in the order met, once each, the engine's tags not used yet the
    same way). A [pause] before the text's first words sets `lead` seconds of silence on the first piece."""
    out, unknown, held = [], [], []
    lead = 0.0
    for s in sents:
        gain = pitch = 0.0
        parts, cur, sounds = [], [], False
        pos = 0
        for m in _TAG.finditer(s["text"]):
            cur.append(s["text"][pos:m.start()])
            pos = m.end()
            raw, secs = m.group(1), m.group(2)
            name = _name(raw, known)
            if name is None:
                low = " ".join(raw.lower().split())
                eng = ALIASES.get(low, low)
                if eng in SOUNDS or eng in STYLES:   # the engine's, not used yet: never read out
                    if f"[{eng}]" not in held:
                        held.append(f"[{eng}]")
                elif low in EDITORIAL or raw[0].isupper() or secs:   # an editor's bracket, or a name: text
                    cur.append(m.group(0))
                elif f"[{low}]" not in unknown:
                    unknown.append(f"[{low}]")
                continue
            per = _UNITS.get((m.group(3) or "s").lower())
            if secs and per is None:   # a number we don't read as a length ([pause 2 minutes]): a pause named, else text
                if name != "pause":
                    cur.append(m.group(0))
                elif (t := " ".join(m.group(0).lower().split())) not in unknown:
                    unknown.append(t)
                continue
            if name == "pause":
                parts.append(("".join(cur), min(PAUSE_MAX_S, float(secs) * per) if secs else PAUSE_S, sounds))
                cur, sounds = [], False
            elif name in ("louder", "softer"):
                gain = max(-GAIN_DB, min(GAIN_DB, gain + (GAIN_DB if name == "louder" else -GAIN_DB)))
            elif name in ("higher", "lower"):
                pitch = max(-PITCH_ST, min(PITCH_ST, pitch + (PITCH_ST if name == "higher" else -PITCH_ST)))
            else:
                cur.append(f"[{name}]")
                sounds = True
        cur.append(s["text"][pos:])
        parts.append(("".join(cur), None, sounds))
        first = len(out)
        for k, (text, hold, snd) in enumerate(parts):
            text = re.sub(r"\s+([,.;:!?])", r"\1", " ".join(text.split()))   # (a tag taken out before a stop)
            if not re.search(r"[A-Za-z0-9\[]", text):   # nothing left to read: its stop and pause go to the piece before
                if out:
                    if len(out) > first and text:
                        out[-1]["text"] += text
                    if k == len(parts) - 1 and len(out) > first:
                        out[-1]["pause"] = s["pause"]
                    if hold:
                        out[-1]["hold"] = max(out[-1].get("hold") or 0.0, hold)
                elif hold:   # a pause before the first words: silence before them (`lead`)
                    lead = max(lead, hold)
                continue
            piece = {**s, "text": text, "pause": s["pause"] if k == len(parts) - 1 else 0.0}
            if lead:
                piece["lead"], lead = lead, 0.0
            if hold is not None:
                piece["hold"] = hold
            if gain:
                piece["gain_db"] = gain
            if pitch:
                piece["pitch_st"] = pitch
            if snd:
                piece["sounds"] = True
            out.append(piece)
    return out, unknown, held


def spoken(text: str, known: frozenset) -> tuple[str, bool]:
    """The words a listener should hear in a piece's text (the engine's tags out), and whether it carries one."""
    t = _TAG.sub(lambda m: " " if _name(m.group(1), known) in known else m.group(0), text)
    return " ".join(t.split()), t != text
