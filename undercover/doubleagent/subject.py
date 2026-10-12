"""Who or what a voice request names, read from what a search for it actually finds (phase 15).

"Toxic as SpongeBob" trained a voice on a podcast host, because the search treated every name as a person who gives
interviews. Now one quick search (titles and channels only, nothing downloaded) decides what kind of subject it is,
and that decides how its voice is found:

  person     a real person speaking            interviews and speeches
  singer     someone YouTube Music has songs by  their songs, vocals separated (interviews as backup)
  character  a fictional character (a cartoon, a show's character; also a game or anime character, or one voiced by
             a known actor)                      scenes and voice lines; never talk about them (podcasts, cast
                                                 interviews, the actor's own voice)
  group      a band or duo                      its lead vocals (whoever sings most), or a member you name
  ambiguous  several people share the name (Chris Evans the actor, the radio host)   the plan asks which

The decision is said in plain words in the one-shot's plan line (so the user can see it and correct it), and asked
when the search can't tell. Data-driven: the words come from the titles found, not from a list of names.
"""
import re
import time

from . import sources as S

ROLES = ("actor", "actress", "singer", "rapper", "comedian", "footballer", "soccer", "basketball", "baseball", "golfer",
         "tennis", "coach", "radio", "presenter", "broadcaster", "journalist", "politician", "senator", "composer",
         "guitarist", "drummer", "pianist", "author", "chef", "youtuber", "streamer", "boxer", "wrestler", "scientist",
         "astronaut", "dj", "producer", "director", "magician")
ROLE_RX = re.compile(r"\b(" + "|".join(ROLES) + r")s?\b", re.I)
GROUP_RX = re.compile(r"\bband\b|\bduo\b|\btrio\b|band members|lead singer|lead vocalist|frontman|reunion tour|"
                      r"\bthe band\b|\bmembers\b", re.I)
GAME_RX = re.compile(r"\banime\b|\bgame\b|gameplay|cutscenes?|\bvoice lines\b|\bnpc\b|\bplaythrough\b", re.I)
VOICED_RX = re.compile(r"voice actors?|voiced by|\bvoice of\b|\bvoices of\b", re.I)
TTL = 600.0
_CACHE: dict = {}


def _search(name: str) -> list:
    """Titles and channels of one search for the name (and one for its interviews), from the cache for 10 min."""
    key = name.strip().lower()
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < TTL:
        return hit[1]
    found: dict = {}
    with S._ydl(extract_flat="in_playlist", skip_download=True) as ydl:
        for q in (name, f"{name} interview"):
            try:
                info = ydl.extract_info(f"ytsearch12:{q}", download=False)
            except Exception:
                continue
            for e in info.get("entries") or []:
                if e.get("id") and e["id"] not in found:
                    found[e["id"]] = S.Source(id=e["id"], url="", title=e.get("title") or "", channel=e.get("channel") or "",
                                              duration=float(e.get("duration") or 0))
    out = list(found.values())
    _CACHE[key] = (time.time(), out)
    return out


def _roles(name: str, found) -> list[tuple[str, int]]:
    """Roles the titles give people of this name, with how many titles say each ("Chris Evans actor", "radio")."""
    from collections import Counter
    c = Counter()
    for s in found:
        roles = {m.group(1).lower() for m in ROLE_RX.finditer(f"{s.title} {s.channel}")}
        c.update(r.replace("actress", "actor") for r in roles)
    return [(r, n) for r, n in c.most_common() if n >= 2]


def classify(name: str, found=None, musician: bool | None = None) -> dict:
    """-> {kind, label (one plain sentence for the plan line), how (the strategy in a few words), evidence (a few
    titles), ask? {question, choices: [{id, label}]}, choices (corrections the user can pick)}.
    found: search results to read (default: one quick search); musician: YouTube Music has songs by them."""
    name = name.strip()
    if found is None:
        try:
            found = _search(name)
        except Exception:   # no network, yt-dlp missing: say nothing rather than guess
            return {"kind": "unknown", "label": None, "how": None, "evidence": [], "choices": []}
    found = list(found)
    titles = [f"{s.title} {s.channel}" for s in found]
    ev = lambda rx: [s.title for s in found if rx.search(f"{s.title} {s.channel}")][:3]
    fix = [{"id": "person", "label": f"{name} is a real person"}, {"id": "character", "label": f"{name} is a character"}]
    if S.looks_like_character(name, found):
        show = S.show_of(name, found)
        game = sum(bool(GAME_RX.search(t)) for t in titles) >= 2
        voiced = sum(bool(VOICED_RX.search(t)) for t in titles) >= 1
        what = ("a game or anime character" if game else
                f"a character from {show.title()}" if show else "a cartoon character")
        label = (f"{name} is {what}: looking for scenes and voice lines from the show, not interviews about it"
                 + (" (the voice actor's own interviews aren't used)" if voiced else "") + ".")
        return {"kind": "character", "label": label, "how": "scenes and voice lines", "show": show, "game": game,
                "voiced": voiced, "evidence": ev(S.CHARACTER_HINT), "choices": [fix[0]]}
    groups = sum(bool(GROUP_RX.search(t)) for t in titles)
    if groups >= 3:
        return {"kind": "group", "label": f"{name} looks like a band: the voice will be its lead vocals (whoever sings "
                                          f"most in their songs). To use one member's voice, name them instead.",
                "how": "the band's lead vocals", "evidence": ev(GROUP_RX), "choices": []}
    roles = _roles(name, found)
    if len(roles) >= 2 and roles[1][1] >= 2 and roles[0][0] != roles[1][0] and not {roles[0][0], roles[1][0]} <= {"singer", "rapper"}:
        choices = [{"id": f"role:{r}", "label": f"{name}, the {r}"} for r, _ in roles[:3]]
        return {"kind": "ambiguous", "label": f"More than one {name} turns up ({', '.join(r for r, _ in roles[:3])}).",
                "how": "asks which", "evidence": ev(ROLE_RX), "choices": choices,
                "ask": {"question": f"Which {name}?", "choices": choices}}
    if musician is None:
        try:
            from .service import is_musician
            musician = is_musician(name)
        except Exception:
            musician = False
    if musician:
        return {"kind": "singer", "label": f"{name} is a singer: learning from their songs (vocals separated), "
                                           f"interviews only as a backup.", "how": "their songs", "evidence": [],
                "choices": [fix[1]]}
    return {"kind": "person", "label": f"{name} is a real person: learning from interviews and speeches. A singing "
                                       f"cover will be less sure than with a singer's voice.",
            "how": "interviews and speeches", "evidence": [], "choices": [fix[1]]}


def search_kind(subject: dict | None) -> tuple[str | None, str]:
    """(force: "character" / "person" / None, extra words for the searches) from a classification or a user's pick."""
    if not subject:
        return None, ""
    k = subject.get("kind")
    if k == "character":
        return "character", ""
    if k in ("person", "singer"):
        return "person", ""
    if k == "role":
        return "person", subject.get("role", "")
    return None, ""
