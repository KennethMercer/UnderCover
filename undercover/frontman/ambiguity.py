"""One-shot: ask when the song is ambiguous ("Hello" is Adele's and Lionel Richie's).

A bare title (no artist) whose best YouTube Music tracks have the same title but different artists is ambiguous. The
plan step (GET /api/auto/plan) then answers `ask: true` with 2-4 `choices` instead of silently picking; POST /api/auto
takes `choice` (the chosen song_url). A choice is remembered only until a different song is requested (in memory,
per server): the plan then returns it as `chosen` with the `others`, so the page can offer "the other one".
Never a silent guess: an ambiguous title that arrives with no choice runs with the first and says so
(`went_with`, "I went with Adele's Hello").

Detection is a pure function of search results (`detect`), so it is checked without the network.
"""
import re
import threading
import time

MAX_CHOICES = 4
WELL_KNOWN = 0.10      # a second artist's version counts when it has at least this share of the top one's views...
FAMOUS = 20_000_000    # ...or this many views anyway (Adele's Hello has 3.3 billion; Lionel Richie's 140 million)
DEEP = 20              # how many plain YouTube results the lookup reads (one flat search, no per-video lookups)
# an "Artist - Title" upload that is someone else's version, or not the record
NOT_THE_SONG = re.compile(r"\bcovers?\b|\bkaraoke\b|\binstrumental\b|\blive\b|\bremix\b|\breaction\b|\btutorial\b"
                          r"|\bsped up\b|\bslowed\b|\bnightcore\b|\bmashup\b|\bpiano\b|\bacoustic\b", re.I)
CACHE_SECONDS = 600    # a title's choices are looked up once per ten minutes (the plan step runs while typing)
FAILED_SECONDS = 120   # a failed lookup (offline, YouTube's bot check) isn't retried at once: retrying prolongs a block


def norm_title(s: str) -> str:
    s = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", s or "")                     # (feat. X) [Remastered]
    s = re.sub(r"\s[-–]\s.*$", " ", s)                                    # "Hello - Remastered 2011"
    s = re.sub(r"\s(?:feat|ft|featuring)\b.*$", " ", s, flags=re.I)        # "Hello ft. A Boogie"
    s = re.sub(r"[.'’`]", "", s.lower())                                  # U.S.A. -> usa, don't -> dont
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def norm_artist(s: str) -> str:
    first = re.split(r"\s*(?:,|&|\bfeat\.?\b|\bft\.?\b|\bwith\b|\band\b)\s*", (s or "").lower())[0]
    return re.sub(r"[^a-z0-9]+", " ", re.sub(r"[.'’`]", "", first)).strip()


def song_key(song: str, artist: str | None = None) -> str:
    return f"{norm_title(song)}|{norm_artist(artist or '')}"


def _tokens(s: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", re.sub(r"[.'’`]", "", (s or "").lower())))


def artist_matches(artist: str, r) -> bool:
    """Is `artist` the artist (or channel) of this search result? Every word of it must be in the result's words."""
    want = _tokens(artist)
    return bool(want) and want <= _tokens(f"{getattr(r, 'artist', '')} {r.channel} {r.title}")


_SETTLED: dict[str, tuple[float, bool]] = {}


def settle(req: dict, search=None) -> dict:
    """A " by " in a request is the song's artist only when the search for that split form puts a result by that artist
    on top; otherwise the whole phrase is the title ("Close By Night", "Killed By Death"). Titles with an obvious
    "by Me / by You ..." never get here (the grammar keeps them whole). No artist named: nothing to check. A search that
    fails (offline, blocked) keeps the split as typed and is not remembered."""
    artist = req.get("artist")
    if not artist:
        return req
    key, now = song_key(req["song"], artist), time.time()
    with _LOCK:
        hit = _SETTLED.get(key)
    if hit and now - hit[0] < CACHE_SECONDS:
        ok = hit[1]
    else:
        try:
            if search is None:
                from .songs import search
            found = search(f"{req['song']} {artist}", limit=8)
        except Exception:   # noqa: BLE001
            return req
        ok = bool(found) and artist_matches(artist, found[0])
        with _LOCK:
            _SETTLED[key] = (now, ok)
    return req if ok else {**req, "song": req.get("whole") or f"{req['song']} by {artist}", "artist": None}


def _artist_song(r) -> tuple[str, str]:
    """(artist, title) of a result: an album track carries them; a plain upload counts only as "Artist - Title"
    ("Lionel Richie - Hello (Official Music Video)") on the artist's channel, never a cover, karaoke or live one."""
    if getattr(r, "kind", "") == "track":
        return r.artist or "", r.song or r.raw_title
    m = re.match(r"\s*(.+?)\s+[-–—]\s+(.+)$", r.raw_title or "")
    if not m or NOT_THE_SONG.search(r.raw_title):
        return "", ""
    # only from the artist's own channel ("lionelrichie", "TLCVEVO"): Pentatonix's official Bohemian Rhapsody is a cover
    squash = lambda x: re.sub(r"[^a-z0-9]+", "", x.lower())
    own = squash(norm_artist(m.group(1)))
    return (m.group(1), m.group(2)) if own and own in squash(r.channel or "") else ("", "")


def detect(query: str, results: list) -> list[dict]:
    """The 2-4 choices when `query` (a bare title) matches songs by different artists, else []. Album tracks and
    "Artist - Title" uploads on the artist's own channel count; an artist's popularity is their best view count across both (album-track
    views on YouTube Music are a small share of a hit's, the official video carries most). Each choice links the
    album track when there is one; `track: false` means only a video was found (the run then searches for the
    album track by title and artist)."""
    qn = norm_title(query)
    if not qn:
        return []
    views: dict[str, int] = {}
    rep: dict[str, tuple] = {}    # artist key -> (result, artist as written, title)
    for r in results:
        artist, title = _artist_song(r)
        a = norm_artist(artist)
        if not a or norm_title(title) != qn:
            continue
        views[a] = max(views.get(a, 0), int(r.views or 0))
        have = rep.get(a)
        is_track = getattr(r, "kind", "") == "track"
        if have is None or (is_track and getattr(have[0], "kind", "") != "track") or                 (is_track == (getattr(have[0], "kind", "") == "track") and (r.views or 0) > (have[0].views or 0)):
            rep[a] = (r, artist, title)
    ranked = sorted(rep, key=lambda a: -views[a])
    if len(ranked) < 2:
        return []
    top = views[ranked[0]]
    well_known = [a for a in ranked if not top or views[a] >= min(WELL_KNOWN * top, FAMOUS)]
    if len(well_known) < 2:
        return []
    out = []
    for a in well_known[:MAX_CHOICES]:
        r, artist, title = rep[a]
        track = getattr(r, "kind", "") == "track"
        out.append({"artist": r.artist if track else artist, "year": (r.year or None) if track else None,
                    "title": (r.song or r.raw_title) if track else re.sub(r"\s*[\(\[].*$", "", title).strip() or title,
                    "song_url": r.url, "track": track})
    return out


def _lookup(song: str) -> list:
    """The album-track search plus a deeper flat YouTube search (one request): famous versions that aren't in
    YouTube Music's first few tracks still show up by their official uploads."""
    from ..core.youtube import SongResult, _ydl
    from .songs import search
    found = list(search(song, limit=30))
    seen = {r.id for r in found}
    with _ydl(extract_flat="in_playlist", skip_download=True) as ydl:
        info = ydl.extract_info(f"ytsearch{DEEP}:{song}", download=False)
    for e in info.get("entries") or []:
        if e.get("id") and e["id"] not in seen and e.get("title"):
            seen.add(e["id"])
            found.append(SongResult(id=e["id"], url=f"https://www.youtube.com/watch?v={e['id']}", raw_title=e["title"],
                                    title=e["title"], channel=e.get("channel") or e.get("uploader") or "",
                                    duration=float(e.get("duration") or 0), views=int(e.get("view_count") or 0)))
    return found


# ---------------------------------------------------------------- lookups (remembered ten minutes)

_LOOKUPS: dict[str, tuple[float, list]] = {}
_LOCK = threading.Lock()


def choices_for(song: str, search=None) -> list[dict]:
    """detect() on a live search; [] when the search fails (never blocks a request: the plan just doesn't ask)."""
    key = norm_title(song)
    now = time.time()
    with _LOCK:
        hit = _LOOKUPS.get(key)
        if hit and now - hit[0] < (CACHE_SECONDS if hit[1] is not None else FAILED_SECONDS):
            return hit[1] or []
    try:
        found = detect(song, search(song, limit=30) if search else _lookup(song))
    except Exception:   # noqa: BLE001 - YouTube blocked / offline: no question, the run reports its own errors
        with _LOCK:
            _LOOKUPS[key] = (now, None)
        return []
    with _LOCK:
        _LOOKUPS[key] = (now, found)
    return found


def cached_choices(song: str) -> list[dict]:
    """The choices already looked up for this title (no network)."""
    with _LOCK:
        hit = _LOOKUPS.get(norm_title(song))
    return list(hit[1] or []) if hit else []


def resolve(song: str, choice_url: str | None = None) -> tuple[dict | None, str | None]:
    """(the song to use, the sentence to show when it was a guess) for a bare title. The user's choice wins; an
    ambiguous title with no choice runs with the first and says so; an unambiguous one -> (None, None)."""
    choices = choices_for(song)
    if choice_url:
        known = next((c for c in choices if c["song_url"] == choice_url), None)
        return known or {"artist": "", "year": None, "title": "", "song_url": choice_url}, None
    if len(choices) >= 2:
        return choices[0], went_with(choices[0])
    return None, None


# ---------------------------------------------------------------- the answer, until a different song is requested

_MEM: dict = {"key": None, "chosen": None, "choices": []}


def remember(key: str, chosen_url: str, choices: list[dict]):
    with _LOCK:
        _MEM.update(key=key, chosen=chosen_url, choices=[dict(c) for c in choices])


def recall(key: str) -> dict | None:
    """{"chosen": choice, "others": [choice...], "choices": [choice...]} while `key` is still the song in effect."""
    with _LOCK:
        if _MEM["key"] != key or not _MEM["chosen"]:
            return None
        choices = list(_MEM["choices"])
        chosen = next((c for c in choices if c["song_url"] == _MEM["chosen"]), None)
        if chosen is None:   # a pasted link / a stale card: the song is known by its url only
            chosen = {"artist": "", "year": None, "title": "", "song_url": _MEM["chosen"]}
            choices = choices + [chosen]
        return {"chosen": chosen, "others": [c for c in choices if c is not chosen], "choices": choices}


def requested(key: str):
    """A one-shot for this song is being started: a different song than the remembered one forgets the answer."""
    with _LOCK:
        if _MEM["key"] != key:
            _MEM.update(key=None, chosen=None, choices=[])


def reset():
    with _LOCK:
        _MEM.update(key=None, chosen=None, choices=[])
        _LOOKUPS.clear()
        _SETTLED.clear()


def went_with(choice: dict) -> str:
    return f"I went with {choice['artist']}'s {choice['title']}"
