"""Find and download source audio for a person with yt-dlp (YouTube search, or any link yt-dlp supports)."""
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..core.config import CONFIG
from ..core.voices import ModelError
from ..core.youtube import (YouTubeBlocked, _cancelled_in, _music_tracks, _short, check_blocked, download_retrying,
                            open_ydl)

TCFG = CONFIG["training"]

QUERIES = {
    "speaking": ["{name} interview", "{name} full speech", "{name} podcast", "{name} talks"],
    "singing": ["{name} isolated vocals", "{name} acapella", "{name} live vocals", "{name} live"],
}
# Singers: their singing is what the voice will be used for, so search mostly for sung material (isolated vocals
# and acapellas separate cleanly); one interview search is the backup if not enough singing turns up.
QUERIES["both"] = ["{name} isolated vocals", "{name} acapella", "{name} vocals only", "{name} live acoustic",
                   "{name} interview"]
# Titles that usually mean someone else's voice, a fake, or a mash-up. Shown unticked with the reason.
SUSPECT = [
    (re.compile(r"\bimpressions?\b|\bimpersonat|\bdoes an? .*voice|\bparody\b|\bsnl\b|\brap battles?\b", re.I), "may be an impersonator"),
    (re.compile(r"\bai\b|\bdeepfake|\bvoice ?clon|\brvc\b|\bai cover\b|\bsynthesi[sz]ed\b|\btts\b|text.to.speech|"
                r"voice changer|soundboard|\bso-?vits\b|\bvocaloid\b", re.I), "may be an AI voice"),
    (re.compile(r"\breacts?\b|\breaction\b|\breacting\b", re.I), "a reaction video (mostly someone else)"),
    (re.compile(r"\bcompilation\b|\bmashup\b|\bmeme\b|\bremix\b|\bnightcore\b|\bsped up\b|\bslowed\b", re.I),
     "edited or mixed audio"),
    (re.compile(r"\bkaraoke\b|\binstrumental\b|\bbacking track\b", re.I), "no vocals"),
    (re.compile(r"\bcover\b", re.I), "probably another singer"),
]
COMMENTARY = re.compile(r"destroys|obliterates|slams|owns|claps back|shuts down|what were they thinking|baffling|"
                        r"breaks down|explained|analysis|tier list|ranking|top \d+", re.I)
GOOD = {"speaking": re.compile(r"interview|podcast|speech|address|conversation|talk|keynote|q&a|lecture", re.I),
        "singing": re.compile(r"acapella|a cappella|isolated|vocals? only|live|unplugged|session", re.I)}


# Dubs and other languages (phase 15: "SpongeBob en español" is another actor): unticked unless the request names
# the language. The words are the titles' own; the request's language is English unless it says otherwise.
LANGUAGES = {"spanish": r"spanish|espa[nñ]ol|latino|latinoam[eé]rica|castellano|doblaje|doblado",
             "portuguese": r"portugu[eê]s|brasil|dublado|dublagem", "german": r"deutsch|german|synchron",
             "french": r"fran[cç]ais|french|doublage|vf\b", "italian": r"italiano|italian|doppiaggio",
             "russian": r"russian|русск", "polish": r"polski|polish", "turkish": r"t[uü]rk[cç]e|turkish",
             "hindi": r"hindi", "japanese": r"japanese|日本語", "korean": r"korean|한국어"}
DUB_RX = {k: re.compile(r"\b(" + v + r")\b|\b(" + v + r") dub|\bdub(bed)? (in )?(" + v + r")", re.I) for k, v in LANGUAGES.items()}


def other_language(title: str, request: str) -> str | None:
    """The language a title is in or dubbed into, when it isn't English and the request didn't ask for it."""
    for lang, rx in DUB_RX.items():
        if rx.search(title) and not rx.search(request):
            return lang
    return None


# Fictional characters (phase 15: "Toxic as SpongeBob" trained on a podcast host talking ABOUT SpongeBob). A character
# is recognised from what the search finds (cast, voice actors, episodes, cartoons), then searched for in scenes and
# voice lines; talk about the character (podcasts, cast interviews, reactions, reviews) is left unticked: for a voiced
# character, even the voice actor's own interviews are the wrong voice.
CHARACTER_HINT = re.compile(r"\bcast\b|voice actors?|\bvoices? of\b|\bepisodes?\b|\bscenes?\b|cartoon|\banimat|nickelodeon|"
                            r"nicktoons|disney|pixar|dreamworks|cartoon network|\banime\b|movie clip|\bseason \d|"
                            r"full episode|\bcharacters?\b|out of character", re.I)
CHAR_QUERIES = ["{name} scenes", "{name} best moments", "{name} voice lines", "{name} quotes"]
# A second round when most of what was found can't be downloaded here (blocked in this region: nearly every SpongeBob
# video was): other uploads of the same material, under other words
MORE_QUERIES = {"character": ["{name} full episode", "{name} episode clip", "{name} movie scene", "{name} funny moments"],
                "speaking": ["{name} speech", "{name} conversation", "{name} on stage", "{name} keynote"]}
# Why a video couldn't be had, from yt-dlp's own words (the cause decides the advice; a region block is never got
# around). On 2026-10-12 17 of 18 SpongeBob videos said "This video is not available": not a region block but a
# missing piece of the downloader (yt-dlp-ejs), so "not available" alone points at the downloader, not the region.
UNAVAILABLE = [
    ("geo", re.compile(r"not made this video available in your country|available in your country|geo.?restrict|"
                       r"blocked it in your country|not available in your (country|region)", re.I),
     "blocked in this region (UnderCover doesn't get around that)"),
    ("gone", re.compile(r"private video|has been removed|terminated|no longer available|account associated .* closed|"
                        r"video unavailable\. this video has been removed", re.I), "removed or private"),
    ("age", re.compile(r"confirm your age|age.?restricted|inappropriate for some users", re.I), "age-restricted"),
    ("signin", re.compile(r"sign in to confirm you.re not a bot|confirm you.re not a bot|sign in to confirm", re.I),
     "YouTube asked for a sign-in (a bot check)"),
    ("player", re.compile(r"this video is not available|unplayable|nsig|signature|challenge|player", re.I),
     "YouTube refused the downloader (usually fixed by updating it: SafeHouse, System room)"),
]
BLOCKED = "not available"   # kept for callers from before the diagnosis


def why_unavailable(error: str) -> tuple[str, str] | None:
    """(kind, plain words) for a failed download's error, or None when it isn't about availability."""
    for kind, rx, words in UNAVAILABLE:
        if rx.search(error or ""):
            return kind, words
    return None
CHAR_GOOD = re.compile(r"\bscenes?\b|\bmoments\b|\bclips?\b|\bepisodes?\b|\bquotes?\b|voice lines|\blines\b|"
                       r"compilation|\bofficial\b|full episode|\bmovie\b|\bspeech\b|\bsong\b", re.I)
CHAR_TALK = re.compile(r"podcast|interview|\bcast\b|voice actors?|\bvoices? of\b|\birl\b|in real life|\breacts?\b|reaction|\breview|explained|"
                       r"theor(y|ies)|behind the scenes|out of character|true crime|trivia|reunion|reunit|\blies\b|\bsins\b|"
                       r"analysis|\bai\b|politics|commentary|documentary|ranking|tier list|everything wrong|\bfacts\b|"
                       r"\bdiaries\b|\bthe zone\b|co-?star test|guest", re.I)
# Talk that stays talk even with the show's name in the title: an interview, the voice actor, the cast, a reaction
STRONG_TALK = re.compile(r"interview|voice actors?|\bcast\b|\breacts?\b|reaction|\breview|behind the scenes|out of character|"
                         r"\bvoices? of\b|\birl\b|in real life|trivia|reunion|reunit", re.I)
GENERIC = set("the a an of and to in on with for at by from is full live new best top his her their my your interview "
              "speech podcast talks talk video official hd 4k part episode clip clips scene scenes movie show".split())
STUDIOS = re.compile(r"nickelodeon|nicktoons|disney|pixar|dreamworks|cartoon network|warner|adult swim|netflix|"
                     r"paramount|\bofficial\b", re.I)
TALK_NOTE = "talk about the character"


def show_of(name: str, found) -> str | None:
    """A show the titles keep naming besides the person ("Family Guy - Peter Makes A Podcast": "family guy" in 10 of
    Peter Griffin's 12 titles). Two words other than the name, in at least 4 titles and half of them. People don't
    have one (the most for a real person here: "president barack", a third of the "obama" titles)."""
    from collections import Counter
    names = set(re.findall(r"\w+", name.lower()))
    c = Counter()
    for src in found:
        w = re.findall(r"[a-z0-9]+", src.title.lower())
        c.update({" ".join(w[i:i + 2]) for i in range(len(w) - 1)
                  if not set(w[i:i + 2]) & names and not set(w[i:i + 2]) <= GENERIC and not any(t.isdigit() for t in w[i:i + 2])})
    best = c.most_common(1)
    return best[0][0] if best and best[0][1] >= 4 and best[0][1] >= len(found) / 2 else None


def looks_like_character(name: str, found) -> bool:
    """Does what a search for this name turned up look like a fictional character rather than a person? Titles
    about a cast, voice actors, episodes, cartoons (three or more, and a quarter of them), or a show they keep naming."""
    found = list(found)
    hits = sum(bool(CHARACTER_HINT.search(f"{s.title} {s.channel}")) for s in found)
    return hits >= max(3, len(found) // 4) or show_of(name, found) is not None


def char_kind(src, show: str | None = None) -> str:
    """For a character: "talk" (about the character: podcasts, cast interviews, reactions, reviews), else "scene"
    (the character speaking: episodes, clips, quotes, a movie scene). A title naming the show is a scene unless it's
    plainly an interview or the cast ("Family Guy - Peter Starts a Podcast" is a scene). Your own links and files
    count as scenes."""
    if getattr(src, "user", False):
        return "scene"
    t = f"{src.title} {src.channel}"
    if show and show in src.title.lower():
        return "talk" if STRONG_TALK.search(t) else "scene"
    return "talk" if CHAR_TALK.search(t) else "scene"


def is_character_project(proj) -> bool:
    """A project whose sources were found as a character's (phase 15), or whose titles say so (projects from before)."""
    return any((s.extra or {}).get("kind") for s in proj.sources) or (proj.mode == "speaking" and looks_like_character(proj.person, proj.sources))


def kinds(proj) -> dict:
    """{source id: "scene" | "talk"} for a character project ({} for a person): stored at search time, else read
    from the titles (projects from before phase 15)."""
    if not is_character_project(proj):
        return {}
    show = show_of(proj.person, proj.sources)
    return {s.id: (s.extra or {}).get("kind") or char_kind(s, show) for s in proj.sources}


def _rank_character(src, name: str, show: str | None = None) -> float:
    t = f"{src.title} {src.channel}".lower()
    tokens = [w for w in re.findall(r"\w+", name.lower()) if len(w) > 1]
    score = 2.0 * sum(w in t for w in tokens) / max(1, len(tokens))
    if char_kind(src, show) == "talk":
        score -= 4.0
    elif CHAR_GOOD.search(t):
        score += 1.5
    if STUDIOS.search(src.channel) or (tokens and all(w in src.channel.lower() for w in tokens)):
        score += 1.0   # the show's own channel
    mins = src.duration / 60
    score += 1.0 if 1.5 <= mins <= 40 else (-1.0 if mins < 0.75 else 0.0)
    return score


# Spoken-word "album tracks": YouTube Music lists speeches, audiobooks and narration as music (Barack Obama:
# "Obama DNC Speech", "Presidential Oath Of Office"; Morgan Freeman's narration albums). Not singing.
SPOKEN = re.compile(r"speech|address|remarks|rally|debate|oath|interview|keynote|sermon|commencement|audiobook|"
                    r"chapter|lecture|conversation|podcast|town hall|state of the union|press conference|q ?& ?a|"
                    r"narrat|poem|reading|spoken|monologue|stand-?up|comedy|testimony|eulogy|introduc", re.I)


def is_sung(src) -> bool:
    """A source of the person singing: their own album tracks, or a title saying so (acapella, isolated vocals,
    live...). Used to find their singing voice and to prefer singing. Accepts a Source or a title."""
    if not isinstance(src, str):
        if (src.extra or {}).get("sung"):
            return True
        src = src.title
    return bool(GOOD["singing"].search(src)) and not GOOD["speaking"].search(src)


def album_tracks(name: str, n: int = 20) -> list:
    """The person's own studio recordings from YouTube Music (their "Artist - Topic" channel): consistent,
    well-recorded singing, separated here with the same model as covers. Fan acapellas vary a lot (Elliott Smith's
    Clementine upload was too hissy to use) and there are only a handful per artist."""
    words = [w for w in re.findall(r"\w+", name.lower()) if len(w) > 2]
    out: dict = {}
    for q in (name, f"{name} songs"):
        try:
            found = _music_tracks(q, n=n)
        except YouTubeBlocked:
            raise
        except Exception:
            continue
        for r in found:
            artist = r.channel.lower().replace(" - topic", "")
            if not words or not all(w in artist for w in words) or r.id in out:
                continue   # only recordings by the person themselves
            if SPOKEN.search(r.title):
                continue   # a speech or audiobook listed as music
            out[r.id] = Source(id=r.id, url=r.url, title=r.title, channel=r.channel, duration=float(r.duration or 0),
                               note="album track", extra={"sung": True})
    return list(out.values())


@dataclass
class Source:
    id: str
    url: str
    title: str
    channel: str = ""
    duration: float = 0.0          # seconds
    selected: bool = True
    note: str = ""                 # why it was unticked, or the kind of video
    status: str = "found"          # found | downloading | separating | ready | failed
    error: str = ""
    user: bool = False             # added by the operator (link or file)
    extra: dict = field(default_factory=dict)

    def to_json(self):
        return asdict(self)


def _ydl(**opts):
    return open_ydl("yt-dlp isn't installed in UnderCover's environment, so clips can't be found online. "
                    "You can still add your own recordings.", **opts)


def _rank(src: Source, name: str, mode: str) -> float:
    t = f"{src.title} {src.channel}".lower()
    score = 0.0
    name_tokens = [w for w in re.findall(r"\w+", name.lower()) if len(w) > 1]
    score += 2.0 * sum(w in t for w in name_tokens) / max(1, len(name_tokens))
    sung = mode != "speaking" and is_sung(src)
    if mode == "both":   # singers: sung sources first, interviews only as backup
        score += 2.0 if sung else (0.5 if GOOD["speaking"].search(t) else 0.0)
    elif GOOD["singing" if mode == "singing" else "speaking"].search(t):
        score += 1.0
    mins = src.duration / 60
    lo, hi = (1.5, 15) if mode == "singing" or sung else (8, 120)
    score += 1.0 if lo <= mins <= hi else (-1.0 if mins < 1.5 else 0.0)
    if any(k in t for k in ("official audio", "lyrics", "lyric video")):
        score -= 0.5  # studio mixes separate fine, but fan uploads are often re-encoded
    # Commentary/clickbait uploads ("X OBLITERATES Y", "What were they thinking?") are mostly someone else talking.
    shouty = [w for w in re.findall(r"\b[A-Z]{4,}\b", src.title) if w.lower() not in name_tokens]
    if shouty or COMMENTARY.search(t):
        score -= 1.5
    return score


def blocked_share(sources) -> tuple[int, int]:
    """(videos that couldn't be had here, videos tried)."""
    tried = [s for s in sources if s.status in ("ready", "failed") and not s.user]
    return sum(s.status == "failed" and why_unavailable(s.error) is not None for s in tried), len(tried)


def unavailable_note(person: str, sources) -> str:
    """One plain sentence: how many of the person's videos couldn't be had, and the likeliest cause."""
    from collections import Counter
    tried = [s for s in sources if s.status in ("ready", "failed") and not s.user]
    why = Counter(w[1] for w in (why_unavailable(s.error) for s in tried if s.status == "failed") if w)
    if not why:
        return ""
    words, n = why.most_common(1)[0]
    return f"{sum(why.values())} of the {len(tried)} videos of {person} UnderCover tried couldn't be downloaded ({n}: {words})"


# Other sites yt-dlp can search, for when YouTube can't supply enough (ARCHITECTURE "not one site"): a fallback only,
# and a find must name the subject in its title (these searches are loose: SoundCloud returns any "voice lines")
OTHER_SITES = [("scsearch", "SoundCloud"), ("nicosearch", "Niconico"), ("bilisearch", "Bilibili")]


def search_other_sites(name: str, character: bool, have: set) -> list:
    """Sources from other sites (each search best effort: an API that refuses is skipped)."""
    tokens = [w for w in re.findall(r"\w+", name.lower()) if len(w) > 1]
    queries = [f"{name} voice lines", f"{name} scenes"] if character else [f"{name} interview", f"{name} speech"]
    out: dict = {}
    with _ydl(extract_flat="in_playlist", skip_download=True) as ydl:
        for key, site in OTHER_SITES:
            for q in queries:
                try:
                    info = ydl.extract_info(f"{key}8:{q}", download=False)
                except Exception:
                    break   # the site refused (Bilibili's search answers 412 here): the next site
                for e in info.get("entries") or []:
                    url = e.get("webpage_url") or e.get("url")
                    title = e.get("title") or ""
                    sid = re.sub(r"[^\w-]", "_", f"{site[:2].lower()}_{e.get('id') or ''}")[:40]
                    if (not url or sid in have or sid in out or not e.get("duration") or float(e["duration"]) < 60
                            or not all(t in title.lower() for t in tokens)):
                        continue
                    src = Source(id=sid, url=url, title=title, channel=f"{site}: {e.get('uploader') or e.get('channel') or ''}",
                                 duration=float(e["duration"]), extra={"site": site})
                    if character:
                        src.extra["kind"] = char_kind(src)
                    if any(rx.search(title) for rx, why in SUSPECT if why != "edited or mixed audio") or other_language(title, name) \
                            or (character and src.extra["kind"] == "talk"):
                        continue
                    out[sid] = src
    return list(out.values())[:6]


def search_more(name: str, have: set, character: bool) -> list:
    """Other uploads of the same person or character, for when most of the first finds were blocked here."""
    found: dict = {}
    with _ydl(extract_flat="in_playlist", skip_download=True) as ydl:
        for q in MORE_QUERIES["character" if character else "speaking"]:
            try:
                info = ydl.extract_info(f"ytsearch{TCFG['search_per_query']}:{q.format(name=name)}", download=False)
            except Exception:
                continue
            for e in info.get("entries") or []:
                vid = e.get("id")
                if vid and vid not in have and vid not in found and e.get("duration"):
                    found[vid] = Source(id=vid, url=f"https://www.youtube.com/watch?v={vid}", title=e.get("title") or vid,
                                        channel=e.get("channel") or e.get("uploader") or "", duration=float(e["duration"]))
    out = []
    for src in found.values():
        if character:
            src.extra["kind"] = char_kind(src)
        for rx, why in SUSPECT:
            if rx.search(src.title) and not (why == "probably another singer") and not (character and why == "edited or mixed audio"):
                src.selected, src.note = False, why
                break
        if character and src.selected and src.extra["kind"] == "talk":
            src.selected, src.note = False, TALK_NOTE
        if src.selected and other_language(src.title, name):
            continue   # a dub or another language
        if src.selected and src.duration >= 60:
            out.append(src)
    rank = (lambda s: -_rank_character(s, name)) if character else (lambda s: -_rank(s, name, "speaking"))
    return sorted(out, key=rank)[:6]


def search(name: str, mode: str = "speaking", per_query: int | None = None, force: str | None = None,
           words: str = "") -> list[Source]:
    """Search YouTube with a few query variants, dedupe, flag suspicious titles, rank best first. A speaking search
    that turns out to be about a fictional character searches again for the character's scenes (phase 15).
    force: "character" / "person" (the user's correction in the one-shot's plan) instead of deciding from the finds;
    words: added to every query ("actor" for "Chris Evans, the actor")."""
    name = name.strip()
    q_name = f"{name} {words}".strip()
    if not name:
        raise ModelError("Type the name of the person whose voice you want.")
    per_query = per_query or TCFG["search_per_query"]
    found: dict[str, Source] = {}
    character = False
    with _ydl(extract_flat="in_playlist", skip_download=True) as ydl:
        def run(queries):
            for q in queries:
                try:
                    info = ydl.extract_info(f"ytsearch{per_query}:{q.format(name=q_name)}", download=False)
                except Exception:
                    continue
                for e in info.get("entries") or []:
                    vid = e.get("id")
                    if not vid or vid in found or not e.get("duration"):
                        continue  # live streams and shorts without a duration are skipped
                    found[vid] = Source(id=vid, url=f"https://www.youtube.com/watch?v={vid}", title=e.get("title") or vid,
                                        channel=e.get("channel") or e.get("uploader") or "", duration=float(e["duration"]))
        if force == "character":
            character = True
            run(CHAR_QUERIES)
        else:
            run(QUERIES.get(mode, QUERIES["speaking"]))
            if mode == "speaking" and force != "person" and looks_like_character(name, list(found.values())):
                character = True
                run(CHAR_QUERIES)
    show = show_of(name, list(found.values())) if character else None
    for src in found.values():
        if character:
            src.extra["kind"] = char_kind(src, show)
        for rx, why in SUSPECT:
            if character and why == "edited or mixed audio" and not rx.search(re.sub("compilation", "", src.title, flags=re.I)):
                continue   # a compilation of a character's scenes is just what a character's voice needs
            if rx.search(src.title) and not (why == "probably another singer" and mode == "speaking"):
                src.selected, src.note = False, why
                break
        if character and src.selected and src.extra["kind"] == "talk":
            src.selected, src.note = False, TALK_NOTE
        lang = other_language(src.title, f"{name} {words}")
        if lang and src.selected:
            src.selected, src.note = False, f"may be in {lang.title()} (a dub or another language)"
    albums = album_tracks(name) if mode != "speaking" else []
    if not found and not albums:
        raise ModelError(f"No videos found for '{name}'. Check the spelling, or paste links / add recordings.")
    rank = (lambda s: -_rank_character(s, name, show)) if character else (lambda s: -_rank(s, name, mode))
    ranked = albums + sorted([s for s in found.values() if s.id not in {a.id for a in albums}], key=rank)
    # Tick the best sources until there is comfortably enough raw audio for the target.
    want = TCFG["target_minutes"] * 60 * 4   # assume ~1/4 of the used audio survives as clean target voice
    have = 0.0
    for s in ranked:
        if not s.selected:
            continue
        if have >= want or s.duration < 60:
            s.selected = False
            continue
        have += min(s.duration, TCFG["max_minutes_per_source"] * 60)
        if not s.note:
            m = (CHAR_GOOD.search(s.title) if character else None) or GOOD["singing"].search(s.title) or GOOD["speaking"].search(s.title)
            s.note = m.group(0).lower() if m else ""
    # sung sources are songs (2-4 min each), so singers need more of them listed to reach the target
    return ranked[:TCFG["search_results"] * (2 if mode == "both" else 1) + len(albums)]


def from_link(url: str) -> list[Source]:
    """A pasted video or playlist link -> sources (metadata only)."""
    if not re.match(r"https?://", url.strip()):
        raise ModelError("That doesn't look like a link. Paste a full address starting with https://")
    with _ydl(extract_flat="in_playlist", skip_download=True) as ydl:
        try:
            info = ydl.extract_info(url.strip(), download=False)
        except Exception as e:
            raise ModelError(f"Couldn't read that link ({_short(e)}).")
    entries = info.get("entries") or [info]
    out = []
    for e in entries[:50]:
        vid = e.get("id") or "link"
        out.append(Source(id=re.sub(r"[^\w-]", "_", vid)[:40], url=e.get("webpage_url") or e.get("url") or url,
                          title=e.get("title") or vid, channel=e.get("channel") or e.get("uploader") or "",
                          duration=float(e.get("duration") or 0), user=True, note="added by you"))
    return out


STRETCH_GAP = 3.0   # s of silence between stretches: more than cadence's 2.5 s phrase gap, so no run spans a cut


def stretches(duration: float, cap: float, length: float) -> list[tuple[float, float]]:
    """[(start, seconds)] of a long video to keep: max_minutes_per_source in stretches of `length` spread evenly
    between its first and last minute (intros, outros), instead of its first 12 min. Phase 19: a 4 h SpongeBob
    compilation's first 12 min gave 0.0-0.4 min of clean voice; one episode's scenes run long, so spreading
    samples more scenes. A video up to 2 min longer than the cap keeps one stretch from the start."""
    if not duration or duration <= cap:
        return []
    if duration <= cap + 120 or length <= 0 or length >= cap:
        return [(60.0 if duration > cap + 120 else 0.0, cap)]
    k = max(1, round(cap / length))
    span = duration - 120 - length
    return [(round(60 + span * i / (k - 1), 1) if k > 1 else 60.0, length) for i in range(k)]


def trim_args(duration: float, spread: bool = True) -> list[str]:
    """ffmpeg arguments that cut a long video's audio down to its stretches (with STRETCH_GAP of silence between);
    spread=False: its first 12 minutes from minute 1, as before."""
    keep = stretches(duration, TCFG["max_minutes_per_source"] * 60, TCFG.get("stretch_minutes", 2.0) * 60 if spread else 0)
    if not keep:
        return []
    if len(keep) == 1:
        return ["-ss", str(keep[0][0]), "-t", str(keep[0][1])]
    parts = ";".join(f"[0:a]atrim=start={a}:duration={d},asetpts=PTS-STARTPTS,apad=pad_dur={STRETCH_GAP}[s{i}]"
                     for i, (a, d) in enumerate(keep))
    joined = "".join(f"[s{i}]" for i in range(len(keep)))
    return ["-filter_complex", f"{parts};{joined}concat=n={len(keep)}:v=0:a=1[kept]", "-map", "[kept]"]


def download(src: Source, dest_dir: Path, progress=None) -> Path:
    """Download the audio as 44.1 kHz mono wav. Long videos are cut to max_minutes_per_source, in stretches spread
    through the video (see stretches()). Returns the wav path."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = dest_dir / f"{src.id}.wav"
    if out.exists() and out.stat().st_size > 0:
        return out
    # The whole audio stream is fetched (yt-dlp's range download streams through ffmpeg at about real time,
    # ~10x slower) and trimmed locally.
    # Spread only through a character's scenes (a compilation of many). A person's long video is mostly one talk, and
    # measured on the user's projects spreading gained nothing (Obama 51.9 / 51.8 min) or lost (Freeman's Q&As: the
    # audience's questions, 11.8 -> 10.3).
    trim = trim_args(src.duration, spread=(src.extra or {}).get("kind") == "scene")
    # Download into a private temp folder and move the finished wav into place last: an interrupted download (crash,
    # power cut) then never leaves a truncated "<id>.wav" that later runs would trust.
    import shutil
    work = dest_dir / f".dl_{src.id}"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    opts = {
        "format": "bestaudio[abr<=160]/bestaudio/best",
        "outtmpl": str(work / f"{src.id}.%(ext)s"),
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "wav"}],
        "postprocessor_args": {"extractaudio": trim + ["-ac", "1", "-ar", "44100"]},
        "overwrites": True,
    }
    if progress:
        def hook(d):
            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                if total:
                    progress(min(1.0, d.get("downloaded_bytes", 0) / total))
        opts["progress_hooks"] = [hook]
    try:
        with _ydl(**opts) as ydl:
            try:
                download_retrying(ydl, src.url)
            except Exception as e:
                if _cancelled_in(e) is not None:
                    raise
                check_blocked(e)
                raise ModelError(f"Couldn't download '{src.title}' ({_short(e)}). UnderCover updates its YouTube "
                                 "downloader by itself when YouTube changes; try again in a while.")
        cands = [p for p in work.glob(f"{src.id}.*") if p.suffix == ".wav"]
        if not cands or cands[0].stat().st_size == 0:
            raise ModelError(f"Downloaded '{src.title}' but couldn't convert it to audio.")
        cands[0].replace(out)
        return out
    finally:
        shutil.rmtree(work, ignore_errors=True)
