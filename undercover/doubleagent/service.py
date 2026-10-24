"""DoubleAgent's public surface: find, check, make and improve the voice of a person.

Other modules (FrontMan's one-shot, SpeakEasy) use voices through this file only:
  installed_for / online_for   voices for a person (installed: trained here first; online community models)
  resolve_voice                a good voice of a person: use one trained here, else community voices that pass a
                               measured check against real clips (voicecheck.py; results remembered), else train one
  train_new_voice              find clips -> prepare -> auto-confirm the main voice -> train -> add to the voices
  project_for / is_musician    the person's training project (singers get their songs, not just interviews)
  voice_f0, undertrained       facts about a voice
  speaking_pitch               how the voice's person sounds when they talk (SpeakEasy pitches its Reader to it)
  cadence_pack / cadence_ref   a voice's cadence (phase 4: the reference clip + numbers SpeakEasy reads by)
  cadence_status               has / can make / queued, for a voice row
  start_cadence, start_new_voice   "Train cadence" / "New voice" as queue items (now or tonight)
"""
import json
import re
from pathlib import Path
import time

from ..core.config import CONFIG, path
from ..core.voices import ModelError, display_name, slug
from ..core import fsutil, gpu, jobs
from ..core.jobs import Paused


def find_installed(person: str):
    """Best installed voice for this person (trained here first, then the newest)."""
    c = installed_for(person)
    return c[0] if c else None


def undertrained(v, min_steps: int = 900) -> bool:
    """A voice trained here with less than about a Quick run (e.g. a smoke test) isn't used automatically."""
    ex = v.extra or {}
    if not ex.get("trained"):
        return False
    try:
        from .project import Project
        spe = Project.load(ex["project"]).train.get("steps_per_epoch") or 0
    except Exception:
        return False
    return bool(spe) and int(ex.get("epoch") or 0) * spe < min_steps


def installed_for(person: str) -> list:
    """Installed voices for this person: exact name, or a name built from it (morgan-freeman-uc, ...).
    Voices trained in UnderCover first, then the newest."""
    from ..core.voices import listed_voices
    key = slug(person)
    if not key:
        return None
    toks = key.split("-")
    generic = {"uc", "rvc", "v1", "v2", "model", "voice", "ai", "new", "hq"}

    def matches(name: str) -> bool:
        if name == key or name.startswith(key + "-") or name.endswith("-" + key):
            return True
        vt = [t for t in name.split("-") if t not in generic and not t.isdigit()]
        # all the person's words in the voice name, or a voice named by part of the name incl. the surname
        return bool(vt) and (all(t in vt for t in toks) or (set(vt) <= set(toks) and toks[-1] in vt))
    from ..core import ear
    cands = [v for v in listed_voices() if matches(v.name) and not undertrained(v)]
    # trained first; then the one the user's thumbs like (the ear everywhere), the exact name, the newest
    cands.sort(key=lambda v: (bool(v.extra.get("trained")), ear.liking(v.name), v.name == key, v.installed), reverse=True)
    return cands


def find_online(person: str):
    """Best community model whose name clearly names this person (all name words present)."""
    r = online_for(person, 1)
    return r[0] if r else None


def online_for(person: str, n: int = 2) -> list:
    """Community models online whose name contains every word of the person's name (best ranked first),
    skipping ones already installed and ones that failed the quality check before."""
    from ..core.voices import search, voices_dir
    toks = [w for w in re.findall(r"\w+", person.lower()) if len(w) > 1]
    try:
        results = search(person, limit=15)
    except ModelError:
        return []
    out = []
    for r in results:
        text = f"{r.name} {r.file} {r.repo}".lower()
        if not all(t in text for t in toks) or (r.version or "v2") != "v2" or (r.size or 0) >= 400e6:
            continue
        if (voices_dir() / slug(r.name)).exists():
            continue
        q = _quality_cache().get(_qkey(person, r.repo + "/" + r.file))
        if q and q["verdict"] != "good" and (q.get("v") == CHECK_VERSION or q["verdict"] == "broken"):
            continue
        out.append(r)
        if len(out) >= n:
            break
    return out


# ---------------------------------------------------------------- measured quality (voicecheck.py)

CHECK_VERSION = 3   # 2: 8 phrases, bootstrap lower bound, pitch + content (2026-10-09); 3: singers tested on singing (2026-10-10)


def _quality_file():
    from ..core.config import path
    return path("cache_dir") / "voice_quality.json"


def _quality_cache() -> dict:
    import json
    f = _quality_file()
    try:
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
    except ValueError:
        return {}


def _qkey(person: str, ident: str) -> str:
    return f"{slug(person)}|{ident}"


def clip_set(proj) -> str:
    """The real clips a check judges by: the reference voice and the kept phrases, as a short fingerprint. A verdict is
    reused only on the same clips (rc-fix3, IA pass 11 A3: a verdict made on another artist's songs kept a good voice
    out of every later check)."""
    import hashlib
    ids = sorted(p.id for p in proj.training_phrases())
    ref = (proj.reference or {}).get("clip") or ""
    return hashlib.sha1(("|".join([proj.name, ref] + ids)).encode("utf-8")).hexdigest()[:12]


def _remember(person: str, ident: str, q, clips: str | None = None):
    import json
    c = _quality_cache()
    c[_qkey(person, ident)] = {**q.to_json(), "v": CHECK_VERSION, "checked": time.strftime("%Y-%m-%d %H:%M"),
                               **({"clips": clips} if clips else {})}
    _quality_file().write_text(json.dumps(c, indent=1), encoding="utf-8")


def _remember_broken(person: str, ident: str, name: str, why: str):
    import json
    c = _quality_cache()
    c[_qkey(person, ident)] = {"voice": name, "score": 0.0, "baseline": 0.0, "phrases": 0, "seconds": 0.0,
                               "verdict": "broken", "note": why[:200], "checked": time.strftime("%Y-%m-%d %H:%M")}
    _quality_file().write_text(json.dumps(c, indent=1), encoding="utf-8")


def _failed_file():
    return path("cache_dir") / "voice_check_failed.json"


def _failed_all() -> dict:
    try:
        return json.loads(_failed_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _clips_of(person: str) -> str:
    from .night import _project_named
    proj = _project_named(person)
    return clip_set(proj) if proj is not None else ""


def check_failed(person: str) -> str | None:
    """Why the last check of this person's voices couldn't be done, while it still holds: on the same clips, within 30
    days. The plan then offers what can work instead (training, or adding clips; MASTERPLAN principle 10), not the same
    check again (rc-polish1: the retry offered "Check them against real clips first", which had just failed)."""
    rec = _failed_all().get(slug(person))
    if not rec or time.time() - rec.get("at", 0) > 30 * 86400:
        return None
    try:
        return rec["why"] if rec.get("clips") == _clips_of(person) else None   # clips added since: worth checking again
    except Exception:   # noqa: BLE001 - unreadable project: as if it held
        return rec["why"]


def _remember_failed(person: str, why: str | None, proj=None):
    """A check that couldn't be done (why), on these clips; why=None: one that ran, forgotten."""
    c = _failed_all()
    if why is None and slug(person) not in c:
        return
    if why is None:
        c.pop(slug(person), None)
    else:
        c[slug(person)] = {"why": why, "clips": clip_set(proj) if proj is not None else "", "at": round(time.time())}
    try:
        fsutil.write_text(_failed_file(), json.dumps(c, indent=1))
    except OSError:
        pass


def _ident(v) -> str:
    return f"installed:{v.name}:{v.installed}"


def is_musician(person: str) -> bool:
    """True when YouTube Music has album tracks by this person (so their singing voice is out there to learn
    from, not just interviews). ~3 s; any failure counts as "no"."""
    from ..core.youtube import YouTubeBlocked, _music_tracks
    toks = [w for w in re.findall(r"\w+", person.lower()) if len(w) > 1]
    for _ in range(2):
        try:
            tracks = _music_tracks(person, n=6)
        except YouTubeBlocked:
            raise   # unknown, not "no": deciding now would build the wrong kind of project
        except Exception:
            tracks = []
        from .sources import SPOKEN
        by = [t for t in tracks if all(tok in t.title.split(" - ")[0].lower() for tok in toks)
              and not SPOKEN.search(t.title)]   # speeches and audiobooks are listed as music too
        if len(by) >= 2:
            return True
        if tracks:   # got an answer, it's just not this person's music
            return False
    return False


def project_for(person: str, mode: str | None = None, subject: dict | None = None):
    """The Train-tab project for this person that isn't finished yet (or a new one). Musicians get "both":
    interviews (for the voice check's other speakers) plus isolated vocals from their songs, so the trained
    voice has actually sung. mode: the person's own choice (DoubleAgent's Speaking / Singing) instead of that guess:
    a new project starts in it; an existing one is left for apply_mode (train_new_voice calls it).
    subject: the request's reading (subject_of). A character's voice is in its scenes (rc-fix2): its project speaks,
    never gets songs (YouTube Music has "Plankton" tracks by other artists), and one made before is corrected."""
    from . import project as P
    character = (subject or {}).get("kind") == "character"
    key = slug(person)
    proj = next((P.Project.load(p["name"]) for p in P.list_projects()
                 if slug(p["person"]) == key and not p["train"].get("exported")), None)
    if proj is None:
        return P.Project.create(person, mode or ("speaking" if character else "both" if is_musician(person) else "speaking"))
    if character:
        return fit_character(proj, person)
    if not mode and proj.mode == "speaking" and not proj.train.get("epoch") and is_musician(person):
        proj = _add_songs(proj, person, "both")   # an interviews-only project for a singer (nothing trained yet)
    return proj


SONG_NOTE = "a song, not the character's scenes"


def fit_character(proj, person: str):
    """A project for a character that was made as a singer's (rc-fix2: "plankton" was "both", its first sources
    other artists' album tracks): it speaks now, its songs are unticked (never deleted: the user can tick one back),
    the voices are found again among what's left, and the show's scenes are searched when it has none. The user's
    own sources stay as they are, and a project that has trained is left alone."""
    from . import project as P
    from . import sources as srcmod
    if proj.train.get("epoch"):
        return proj
    with P.lock(proj.name):
        proj = P.Project.load(proj.name)
        changed = proj.mode != "speaking"
        proj.mode = "speaking"
        off = []
        for s in proj.sources:
            if s.selected and not s.user and srcmod.is_song(s):
                s.selected, s.note = False, SONG_NOTE
                off.append(s.id)
        changed = changed or bool(off)
        scenes = [s for s in proj.sources if s.selected and (s.extra or {}).get("kind") == "scene"]
        if not scenes:
            try:
                found = srcmod.search(person, "speaking", force="character")
            except ModelError:
                found = []
            have = {s.id for s in proj.sources}
            new = [s for s in found if s.id not in have]
            proj.sources += new
            changed = changed or bool(new)
        if off and any(p.source in off for p in proj.phrases):
            # the voices were clustered with the songs in: again without them (the reference may have been a singer)
            if not (proj.reference.get("confirmed") and not proj.reference.get("auto")):
                proj.reference = {}
            try:
                P.find_speakers(proj)
            except ModelError:
                proj.speakers, proj.reference = [], {}
        if changed:
            proj.save()
    return P.Project.load(proj.name)


def _add_songs(proj, person: str, mode: str):
    """Add sources of the person singing to a speech project and switch it to `mode` (both / singing)."""
    from . import project as P
    from . import sources as srcmod
    try:
        sung = srcmod.search(person, "singing")
    except ModelError:
        sung = []
    with P.lock(proj.name):
        proj = P.Project.load(proj.name)
        have = {s.id for s in proj.sources}
        proj.sources += [x for x in sung if x.id not in have]
        proj.mode = mode
        proj.save()
    return proj


def apply_mode(proj, person: str, mode: str | None):
    """The person's Speaking / Singing choice (DoubleAgent's line, a queued Train tonight) on a project that hasn't
    trained yet. Nothing searched: the search follows it. Speech so far and they chose singing: their songs are added.
    Chose speaking for a project that has songs: it keeps them (they can be unticked in Deep Cover's steps).
    A project that has trained keeps its mode."""
    from . import project as P
    if not mode or proj.mode == mode or proj.train.get("epoch"):
        return proj
    if not proj.sources:
        with P.lock(proj.name):
            proj = P.Project.load(proj.name)
            proj.mode = mode
            proj.save()
        return proj
    if proj.mode == "speaking":
        return _add_songs(proj, person, mode)
    return proj


def reference_clips(job, person: str, control, push, subject: dict | None = None):
    """Make sure the person's project has the real voice identified and some phrases by other people (the
    test material): prepares one source at a time, at most three. These are training clips too. For a character
    the reference must be the voice heard in its scenes (phase 15: community SpongeBob voices were judged against
    a podcast host), so scenes are prepared first and a reference without scene minutes isn't trusted."""
    from . import project as P
    from . import sources as srcmod
    from .subject import search_kind
    from ..core.speaker import SpeakerEmbedder
    proj = project_for(person, subject=subject)
    if not proj.sources:
        push("voice", 0.02, f"Finding clips of {person} to check voices against")
        force, words = search_kind(subject)
        found = srcmod.search(person, proj.mode, force=force, words=words)
        with P.lock(proj.name):
            proj = P.Project.load(proj.name)
            if not proj.sources:
                proj.sources = found
                proj.save()

    from . import voicecheck
    have: dict = {}

    def readers() -> bool:   # (only asked when the clips have no other speakers; made once and kept)
        if "r" not in have:
            have["r"] = bool(voicecheck.reader_phrases())
        return have["r"]

    def ready(pr, k):
        # The voice heard most could be an interviewer when only one source is in. Like the training page,
        # trust it once it's in 2+ sources, and only when there's other speech to test with: the clips' own other
        # speakers, or the Readers (rc-fix7: a person's solo speeches have none)
        ref = pr.reference
        if k and not ref.get("scene_minutes") and not (ref.get("confirmed") and not ref.get("auto")):
            return False   # a character's voice comes from its scenes, unless the user picked the voice themselves
        return (ref.get("clip") and ref.get("sources", 0) >= 2
                and (any(x.reason == "other speaker" for x in pr.phrases) or readers()))
    emb = SpeakerEmbedder()
    for _ in range(3):
        proj = P.Project.load(proj.name)
        k = srcmod.kinds(proj)
        if ready(proj, k):
            break
        todo = sorted((s for s in proj.sources if s.selected and s.status not in ("ready", "failed")
                       and (not k or k.get(s.id) == "scene" or s.user)),   # a character: only its scenes (or the user's own)
                      key=lambda s: k.get(s.id) != "scene")[:1]   # scenes first (stable: the search's order otherwise)
        todo = [s.id for s in todo]
        if not todo:
            break
        push("voice", 0.04, f"Getting real clips of {person} to check voices against")
        try:
            P.prepare_sources(proj.name, todo, gpu.MODELS.separator, emb, cancel=lambda: job.cancel or job.pausing,
                              stop=lambda: job.cancel or getattr(control, "stop", False),
                              progress=lambda f, label, **kw: push("voice", 0.04 + 0.1 * f, f"Getting real clips of {person}: {label}"),
                              between=control.service,
                              friendly=lambda e: jobs.friendly(e, "preparing a source"))
        except P.Cancelled:
            raise Paused() if job.pausing else jobs.Cancelled()
    proj = P.Project.load(proj.name)
    if proj.speakers and not proj.reference.get("confirmed"):
        with P.lock(proj.name):
            proj = P.Project.load(proj.name)
            P.set_reference(proj, cluster=proj.reference["cluster"], auto=True)
            proj.save()
    return P.Project.load(proj.name)


def subject_of(person: str, pick: str | None = None) -> dict:
    """What a voice request names, read from a quick search (subject.py), with the user's correction or answer.
    Stable per name (P9-6: "plankton" read as a cartoon character in one pass and a real person in the next): the
    first sure reading (or the user's pick) is kept in cache/subjects.json and used again; a downloaded voice named
    "<name> (<show>)" makes it a character from that show."""
    from .subject import apply_pick, classify
    key = slug(person)
    memo = _subjects()
    if pick:
        r = apply_pick(person, {}, pick)
        if r.get("kind") in ("person", "character", "role"):
            memo[key] = r
            _save_subjects(memo)
        return r
    if memo.get(key, {}).get("picked"):
        return memo[key]   # the user's own pick wins over everything
    # a downloaded voice of this name that names its show (in brackets, its folder or where it came from:
    # "plankton-spongebob", ".../Plankton__SpongeBob___RVC_v2") wins over a web guess, a remembered one too (RC pass A:
    # "pink pony club as plankton" read as a character, a person and a singer on three fresh installs)
    show = _show_hint(person)
    if show:
        m = memo.get(key) or {}
        if m.get("kind") == "character" and (m.get("show") or "").lower() == show.lower():
            return m
        r = {"kind": "character", "show": show.lower(), "how": "scenes and voice lines", "evidence": [], "from": "voice",
             "label": f"{person} is a character from {_show_title(show)}: looking for scenes and voice lines from the "
                      "show, not interviews about it.",
             "choices": [{"id": "person", "label": f"{person} is a real person"}]}
        memo[key] = r
        _save_subjects(memo)
        return r
    if key in memo:
        return memo[key]
    r = classify(person)
    if r.get("kind") in ("person", "character", "singer", "group"):
        memo[key] = r
        _save_subjects(memo)
    return r


def _subjects() -> dict:
    try:
        return json.loads((path("cache_dir") / "subjects.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_subjects(memo: dict):
    try:
        fsutil.write_text(path("cache_dir") / "subjects.json", json.dumps(memo, indent=1))
    except OSError:
        pass


# Shows a downloaded voice's folder or source may name ("plankton-spongebob", "Plankton__SpongeBob___RVC_v2"): famous
# animated shows and game series, plus every show a character was read from before (cache/subjects.json). A folder's
# other words alone ("morgan-freeman-narrator") aren't a show.
KNOWN_SHOWS = ("spongebob", "simpsons", "family guy", "south park", "futurama", "rick and morty", "bobs burgers",
               "american dad", "king of the hill", "adventure time", "regular show", "gravity falls", "avatar",
               "pokemon", "sesame street", "muppets", "looney tunes", "scooby doo", "peppa pig", "paw patrol", "bluey",
               "shrek", "toy story", "star wars", "mario", "sonic", "minecraft", "fortnite", "genshin", "overwatch",
               "league of legends", "naruto", "dragon ball", "one piece", "my little pony", "steven universe",
               "invader zim", "hey arnold", "rugrats", "phineas and ferb", "teen titans", "transformers",
               "teenage mutant ninja turtles", "winnie the pooh", "thomas the tank engine", "garfield")


def _show_in(text: str, person: str) -> str | None:
    """The known show a voice's folder name or source names, besides the person's own words."""
    words = re.findall(r"[a-z0-9]+", text.lower().replace("'", ""))
    joined = f" {' '.join(words)} "
    own = set(re.findall(r"[a-z0-9]+", person.lower()))
    seen = {(r.get("show") or "").lower() for r in _subjects().values() if r.get("kind") == "character" and r.get("show")}
    for show in sorted(set(KNOWN_SHOWS) | {s for s in seen if s}, key=len, reverse=True):   # the longest name first
        if f" {show} " in joined and not set(show.split()) <= own:
            return show
    return None


def _show_title(show: str) -> str:
    return {"spongebob": "SpongeBob", "bobs burgers": "Bob's Burgers"}.get(show.lower(), show if any(c.isupper() for c in show)
                                                                            else show.title())


def _show_hint(person: str) -> str | None:
    """The show a downloaded voice of this person names: in brackets in its display name ("Plankton (SpongeBob)"), or
    in its folder name or source link ("plankton-spongebob", ".../Plankton__SpongeBob___RVC_v2"). A voice counts when
    it's matched to the person by its folder name, or its display name, brackets aside, is the person."""
    from ..core.voices import list_voices
    mine = {v.name for v in installed_for(person) or []}
    for v in list_voices():
        shown = display_name(v)
        bare = slug(re.sub(r"\([^)]*\)", " ", shown))
        if not (v.name in mine or bare == slug(person) or bare.startswith(slug(person) + "-")):
            continue
        m = re.search(r"\(([^)]{3,40})\)", shown)
        if m:
            return m.group(1).strip()
        hit = _show_in(f"{v.name} {(v.source or '').split('?')[0]}", person)   # only known shows match: a host isn't one
        if hit:
            return hit
    return None


def _prefetch_voice_sources(person: str, subject: dict | None = None):
    """Create/find the person's project, search their sources and download the first two (network only)."""
    from . import sources as srcmod
    from .subject import search_kind
    try:
        proj = project_for(person, subject=subject)
        if not proj.sources:
            from . import project as P
            force, words = search_kind(subject)
            found = srcmod.search(person, proj.mode, force=force, words=words)
            with P.lock(proj.name):
                cur = P.Project.load(proj.name)
                if not cur.sources:
                    cur.sources = found
                    cur.save()
            proj = P.Project.load(proj.name)
        for s in [s for s in proj.sources if s.selected and s.status not in ("ready", "failed")][:2]:
            srcmod.download(s, proj.dir / "raw")
    except Exception:
        pass   # best effort: the normal path does all of this again if needed


CHECK_CLIPS_S = 300.0     # finding and preparing a few clips of the person when there are none yet (~5 min)
CHECK_CLIPS_READY_S = 60.0
CHECK_PER_VOICE_S = 50.0  # one voice checked against them (the reading tests, ~45 s on a free GPU)


def check_estimate_s(person: str, voices: int) -> float:
    """Seconds to check this many voices against real clips of the person (the clips first, when there are none)."""
    from .night import _project_named
    proj = _project_named(person)
    try:
        ready = proj is not None and proj.kept_seconds() >= 60
    except Exception:   # noqa: BLE001 - an odd project: the longer way
        ready = False
    m = _check_speed()   # measured on this PC (P8-4: 213-253 s promised, about 400 s measured)
    clips = m.get("clips_ready" if ready else "clips_new") or (CHECK_CLIPS_READY_S if ready else CHECK_CLIPS_S)
    return clips + (m.get("per_voice") or CHECK_PER_VOICE_S) * max(1, voices)


def _check_speed() -> dict:
    try:
        return json.loads((path("cache_dir") / "check_speed.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _record_check(key: str, seconds: float):
    """A check's measured time, kept as a running average (the next estimate)."""
    d = _check_speed()
    d[key] = round(seconds if d.get(key) is None else 0.6 * d[key] + 0.4 * seconds, 1)
    try:
        fsutil.write_text(path("cache_dir") / "check_speed.json", json.dumps(d))
    except OSError:
        pass


def train_estimate_s(person: str) -> float:
    """Seconds to train a new voice of the person (the queue's own estimate: find, prepare, train)."""
    from .night import estimate
    return estimate({"kind": "new", "target": person})["total"] * 60.0


def demo_for(person: str, cands: list | None = None):
    """The demo voice ("Agent Smoke"), when the request asks for it by its own name; else None. installed_for matches
    name endings, and a real artist called "Smoke" must never get it. Asked for, it's used as it is: there are no clips
    of nobody to check, nobody to look up, and nothing to ask (the plan, the one-shot and resolve_voice all use this)."""
    from ..core.voices import display_name
    if cands is None:
        cands = installed_for(person) or []
    return next((v for v in cands if (v.extra or {}).get("demo") and slug(person) in (v.name, slug(display_name(v)))), None)


def resolve_voice(job, person: str, prefer_train: bool, control, push, plan: dict, choice: str | None = None):
    """-> installed Voice to use (training one if nothing measured good enough).
    choice: the user's answer to the plan's voice question ("Ask me each time"): "use:<voice>" uses that installed
    voice now, unchecked; "check" checks the voices against real clips first; "train" trains a better one."""
    from . import voicecheck
    from ..core import voices as V
    from ..core.voices import get_voice
    from ..core.speaker import SpeakerEmbedder
    cands = installed_for(person) or []
    demo = demo_for(person, cands)   # "... as Agent Smoke": used as it is
    cands = [v for v in cands if not (v.extra or {}).get("demo")]
    if demo is not None:
        plan["voice"] = {"name": demo.name, "display": display_name(demo), "how": "installed", "checked": "the demo voice"}
        push("voice", 1.0, f"Using the demo voice “{display_name(demo)}”")
        return demo
    if choice and choice.startswith("use:"):   # the user's pick, used as it is
        v = next((c for c in cands if c.name == choice[4:]), None)
        if v is None:
            raise ModelError(f"That voice isn't in your voices any more. Ask again to pick another.")
        plan["voice"] = {"name": v.name, "display": display_name(v), "how": "installed",
                         "checked": "your pick (not checked against real clips)"}
        push("voice", 1.0, f"Using “{display_name(v)}”, as you picked")
        return v
    if choice == "train":
        prefer_train = True
    trained = [v for v in cands if v.extra.get("trained")]
    weak_trained = None
    if trained:
        tq = voice_quality(trained[0])
        if prefer_train:
            tq = train_note(trained[0], tq)
        if tq["ok"] or prefer_train:
            plan["voice"] = {"name": trained[0].name, "display": display_name(trained[0]), "how": "installed",
                             "checked": "trained in UnderCover", "quality": tq}
            push("voice", 1.0, f"Using your voice “{display_name(trained[0])}” (trained in UnderCover)")
            return trained[0]
        # a voice trained here that the gate calls weak (phase 15: the old SpongeBob voice learned a podcast host)
        # doesn't end the search: a community voice that checks better is used instead; it's the fallback
        weak_trained = trained[0]
        push("voice", 0.05, f"Your voice “{display_name(weak_trained)}” is weak ({tq['problems'][0]}); checking others first")
        cands = [v for v in cands if v.name != weak_trained.name]
    community = [] if prefer_train else cands
    # the last check couldn't be done on these clips (rc-polish1): with no voice of theirs here, it isn't tried again
    # unless asked ("check"); the plan offers the training (voice_choice "train": prefer_train above). Never a training
    # nobody asked for (rc-fix2): without either, the job says so and stops
    failed = None if prefer_train or choice == "check" or community else check_failed(person)
    if failed:
        raise ModelError(f"The last check of {person}'s voices couldn't be done ({failed}). Add clips of {person} in "
                         "DoubleAgent, or pick Train a new voice.")
    online = [] if prefer_train else online_for(person, 2)
    have = {slug(v.name) for v in community} | {v.source for v in community if v.source}
    seen, uniq = set(), []
    for r in online:   # P8-4: the same candidate was downloaded twice (installed already, or listed twice)
        key = f"{r.repo}/{r.file}"
        if key in seen or slug(r.name) in have or getattr(r, "url", None) in have:
            continue
        seen.add(key)
        uniq.append(r)
    online = uniq
    checked = []
    if community or online:
        # said as it is, with the check's own time (not a training's worst case: "about 177 min left" looked hung)
        n = len(community)
        plan["voice_until"] = time.time() + check_estimate_s(person, n + len(online))
        push("voice", 0.02, f"Checking your {n} {person} voice{'s' if n != 1 else ''} against real clips" if n
             else f"Checking community voices of {person} against real clips")
        t_clips = time.time()
        from .night import _project_named
        before = _project_named(person)
        try:
            ready_before = before is not None and before.kept_seconds() >= 60
        except Exception:   # noqa: BLE001 - as check_estimate_s
            ready_before = False
        proj = reference_clips(job, person, control, push, plan.get("subject"))
        _record_check("clips_ready" if ready_before else "clips_new", time.time() - t_clips)
        why = clips_missing(proj, plan.get("subject"))
        if why:   # rc-fix2: no clips to judge by is said plainly, never a failed check that goes on to train
            return no_check(person, why, community, plan, push, proj)
        t_voices = time.time()
        conv, emb = gpu.MODELS.converter, SpeakerEmbedder()

        clips = clip_set(proj)

        def test(v, ident, staged=False, owner=None):
            cached = _quality_cache().get(_qkey(person, ident))
            # older, weaker checks are redone once, and so is one made on other clips (A3)
            if cached and cached.get("v") == CHECK_VERSION and cached.get("clips") == clips:
                return voicecheck.Quality(**{k: cached.get(k) for k in ("voice", "score", "baseline", "phrases", "seconds",
                                                                        "verdict", "target_hz", "low", "pitch_cents", "content", "test")})
            push("voice", 0.16 + 0.04 * len(checked), f"Checking whether “{display_name(v)}”"
                 + (f" (from {owner})" if owner else "") + f" sounds like {person}")
            if staged:   # a candidate in the staging folder isn't a listed voice: loaded from there for its check
                from ..core.engine.convert import LoadedVoice
                lv = LoadedVoice(v, conv.device, conv.half)
                try:
                    q = voicecheck.check(lv, proj, conv, emb)
                finally:
                    del lv
            else:
                q = voicecheck.check(v.name, proj, conv, emb)
            _remember(person, ident, q, clips)
            return q
        # rc-fix7: a community voice downloaded for this check waits in the staging folder and joins the voices only if
        # it's the one chosen (promote); every other one, and all of them when the check fails or stops, is discarded.
        # (A failed check once left BananaBus223/Obama installed, unchecked, offered next time: two Obamas.)
        V.discard_staged()   # what a check that stopped before (the app closed mid-check) left there
        downloaded = []

        def drop_downloads(keep=None):
            for name in downloaded:
                if name != keep:
                    V.discard_staged(name)
        try:
            for v in community:
                q = test(v, _ident(v))
                checked.append({"name": v.name, "where": "installed", **q.to_json()})
            if any(c["verdict"] == "good" for c in checked):
                online = []   # an installed voice already passes: no need to download more
            for r in online:
                if job.cancel:
                    raise jobs.Cancelled()
                ident = r.repo + "/" + r.file
                cached = _quality_cache().get(_qkey(person, ident))
                if cached and (cached["verdict"] == "broken" or (cached["verdict"] != "good" and cached.get("clips") == clips)):
                    continue   # judged on these clips already (a broken file stays broken whatever the clips)
                owner = (r.repo or "").split("/")[0] or None
                push("voice", 0.16 + 0.04 * len(checked), f"Downloading a community voice of {person}"
                     + (f" (from {owner})" if owner else "") + " to check it")
                try:
                    v = V.download_staged(r, progress=lambda d, t: push("voice", 0.16, f"Downloading a community voice of {person}"
                                                                         + (f" (from {owner})" if owner else "")))
                except ModelError as e:   # broken/unsafe/unloadable file: note it, try the next one
                    checked.append({"name": r.name, "where": "online", "repo": r.repo, "verdict": "broken", "note": str(e)[:160]})
                    _remember_broken(person, ident, r.name, str(e))
                    continue
                downloaded.append(v.name)   # (before its check: a check that fails or stops discards it)
                # named as it will be listed if it's chosen (rc-polish1: "Plankton Spongebob", its folder, then
                # "Plankton"): a community voice checked for a person is shown as that person (_remember_range)
                v.extra = {**(v.extra or {}), "person": person}
                q = test(v, ident, staged=True, owner=owner)
                checked.append({"name": v.name, "where": "online", "repo": r.repo, **q.to_json()})
        except BaseException as e:
            if not isinstance(e, ValueError):   # cancelled or failed mid-check: remove the models fetched for it
                drop_downloads()
                raise
            # ValueError: no other-speaker phrases to test with, so we can't judge them and don't trust them
            checked.append({"name": "-", "verdict": "unknown", "note": "couldn't test (clips had only one speaker)"})
        plan["checked"] = checked
        if checked and all(c.get("verdict") in ("unknown", "broken") for c in checked) and any(c.get("verdict") == "unknown" for c in checked):
            drop_downloads()
            return no_check(person, f"the clips of {person} had no other voices to test against", community, plan, push, proj)
        tested = [c for c in checked if c.get("verdict") not in (None, "broken", "unknown")]
        if tested:
            _record_check("per_voice", (time.time() - t_voices) / len(tested))
            _remember_failed(person, None)   # a check that ran: the next plan may offer it again
        good = sorted([c for c in checked if c.get("verdict") == "good"], key=lambda c: -c["score"])
        from ..core import ear
        good = ear.prefer(good, name=lambda c: c["name"], score=lambda c: c["score"])   # equally good: the liked one
        quick = False
        if not good and not prefer_train:
            # quick first (phase 15): a voice that sounds somewhat like them makes a cover in minutes; training one
            # takes about an hour. Said plainly, with the way to a closer voice (tick Train a new voice).
            good = ear.prefer(sorted([c for c in checked if c.get("verdict") == "weak"], key=lambda c: -c["score"]),
                              name=lambda c: c["name"], score=lambda c: c["score"])[:1]
            quick = bool(good)
        drop_downloads(keep=good[0]["name"] if good else None)
        if good and good[0]["where"] == "online":   # the chosen download joins the voices only now, checked
            good[0]["name"] = V.promote(good[0]["name"]).name
        if good:
            # the person's pitch from their kept clips: singing for singers (no speech headroom then)
            rng = voicecheck.target_range(proj, conv.rmvpe)
            hz = rng[1] if rng else None
            plan["person_hz"], plan["person_speech"] = (hz, proj.from_speech()) if hz else (good[0].get("target_hz"), True)
            v = get_voice(good[0]["name"])
            if rng:
                plan["person_range"] = rng
                _remember_range(v, rng, plan["person_speech"], person)
            plan["voice"] = {"name": v.name, "display": display_name(v), "how": good[0]["where"] if good[0]["where"] == "online" else "installed",
                             "repo": good[0].get("repo"), "checked": f"sounds like {person}: {good[0]['score']:.2f} (pass {voicecheck.SUNG_GOOD if good[0].get('test') == 'sung' else voicecheck.GOOD}{', tested on singing' if good[0].get('test') == 'sung' else ', tested with Reader voices' if good[0].get('test') == 'readers' else ''})"}
            if quick:
                plan["voice"].update(quick=True, checked=f"sounds somewhat like {person}: {good[0]['score']:.2f} "
                                                         f"(a close match is {voicecheck.GOOD})")
                plan["voice"]["quality"] = {
                    "ok": False, "quick": True,
                    "summary": f"Quick: using “{display_name(v)}”, which sounds somewhat like {person} ({good[0]['score']:.2f}). "
                               f"For a closer voice, tick Train a new voice (about 1 hour).",
                    "advice": f"Train a proper voice for {person} tonight",
                    "action": {"kind": "new", "target": person, "now": False}}
                push("voice", 1.0, plan["voice"]["quality"]["summary"])
                return v
            push("voice", 1.0, f"Using “{display_name(v)}”: it sounds like {person} ({good[0]['score']:.2f})")
            return v
        push("voice", 0.2, "None of the existing voices sounded enough like " + person + ", so UnderCover will train one")
    if weak_trained is not None:   # nothing better: the weak voice makes a quick cover, said plainly (the gate)
        tq = voice_quality(weak_trained)
        plan["voice"] = {"name": weak_trained.name, "display": display_name(weak_trained), "how": "installed",
                         "checked": "trained in UnderCover", "quality": tq}
        push("voice", 1.0, f"Using your voice “{display_name(weak_trained)}”, though it's weak")
        return weak_trained
    plan["voice"] = {"name": f"{slug(person)}-uc", "display": person, "how": "train"}
    tr = train_estimate_s(person)   # the plan's and the button's own estimate (rc-fix7: this line said "about 1 hour")
    plan["voice_until"] = time.time() + tr
    push("voice", 0.2 if checked else 0.0, f"Training a voice for {person} ({jobs.eta_words(tr)})"
         + (f". {plan['subject']['label']}" if (plan.get("subject") or {}).get("label") else ""))
    name = train_new_voice(job, person, control, push, subject=plan.get("subject"))
    v = get_voice(name)
    plan["voice"]["name"] = v.name
    gpu.forget_voice(v.name)
    return v


def clips_missing(proj, subject: dict | None) -> str | None:
    """Why the real clips can't judge a voice, or None when they can: a reference voice heard in 2+ sources with other
    people's speech to test against; for a character, a reference heard in its scenes (not a song, not talk)."""
    from . import sources as srcmod
    ref = proj.reference or {}
    who = proj.person or "them"
    if (subject or {}).get("kind") == "character" or srcmod.kinds(proj):
        if not ref.get("scene_minutes") and not (ref.get("confirmed") and not ref.get("auto")):
            return f"no clean clips of {who} from the show's scenes came through"
    if not ref.get("clip"):
        return f"no clean clips of {who} came through"
    if not any(p.reason == "other speaker" for p in proj.phrases):
        from . import voicecheck
        if not voicecheck.reader_phrases():   # (the Readers stand in for other speakers: rc-fix7)
            return f"the clips of {who} had no other voices to test against"
    return None


def no_check(person: str, why: str, installed: list, plan: dict, push, proj=None):
    """The check couldn't be done: said plainly. With a voice of theirs installed, it's used as it is (the quick cover
    the user was promised, marked unchecked); with none, the job stops and says what to do. Never a training the user
    didn't ask for (rc-fix2: a failed check trained for "about 104 min" after "about 8-9 min"). Remembered on these
    clips (check_failed): the next plan offers training or adding clips instead of the same check."""
    from ..core import ear
    _remember_failed(person, why, proj)
    plan["checked"] = [{"name": "-", "verdict": "unknown", "note": f"couldn't check: {why}"}]
    if installed:
        v = ear.prefer(list(installed), name=lambda x: x.name)[0]   # the liked one first, else installed_for's order
        plan["voice"] = {"name": v.name, "display": display_name(v), "how": "installed",
                         "checked": f"not checked: {why}"}
        push("voice", 1.0, f"Couldn't check the voices ({why}), so “{display_name(v)}” is used as it is")
        return v
    raise ModelError(f"Couldn't check any voice of {person}: {why}. Add clips of {person} in DoubleAgent, or pick "
                     "Train a new voice.")


def voice_f0(v) -> tuple[float | None, bool]:
    """(median training pitch Hz, trained from speech) for voices trained here; (None, False) otherwise."""
    ex = v.extra or {}
    if ex.get("f0_median") and (ex.get("f0_range") or not ex.get("project")):
        return float(ex["f0_median"]), bool(ex.get("from_speech"))
    if ex.get("project"):   # trained before f0 was recorded: compute from the project's data once
        import json
        import torch
        from .project import Project
        from .trainer import f0_median, f0_range
        try:
            proj = Project.load(ex["project"])
            data = torch.load(proj.dir / "train" / "data.pt", map_location="cpu", weights_only=True)
            ex["f0_median"], ex["from_speech"] = f0_median(data), proj.from_speech()
            ex["f0_range"] = f0_range(data)
            from ..core import fsutil
            fsutil.write_text(v.dir / "meta.json", json.dumps(v.to_json(), indent=2))
            return ex["f0_median"], ex["from_speech"]
        except Exception:
            pass
    return None, False


SUNG_TO_SPEECH = 0.5   # singers speak about an octave below their singing median: Elliott Smith sings at 212 Hz (the
                       # voice's training data) and speaks at 104 Hz (2 min of an MTV interview, measured 2026-10-10)


def speaking_pitch(v) -> dict | None:
    """How this voice's person sounds when they TALK: {"hz": median, "low": p5, "how": where it comes from}, or None
    when nothing is known (a downloaded voice never checked against a person). SpeakEasy pitches its Reader to this."""
    ex = v.extra or {}
    hz, speech = voice_f0(v)
    rng = ex.get("f0_range")
    if hz and rng:
        if speech:
            return {"hz": hz, "low": rng[0], "how": "its training speech"}
        return {"hz": round(hz * SUNG_TO_SPEECH, 1), "low": round(rng[0] * SUNG_TO_SPEECH, 1), "how": "its singing, an octave down"}
    pr = ex.get("person_range")
    if pr:
        if ex.get("person_range_speech"):
            return {"hz": pr[1], "low": pr[0], "how": f"{ex.get('person') or 'the person'}'s real speech"}
        return {"hz": round(pr[1] * SUNG_TO_SPEECH, 1), "low": round(pr[0] * SUNG_TO_SPEECH, 1),
                "how": f"{ex.get('person') or 'the person'}'s singing, an octave down"}
    return None


def print_vote(v) -> int | None:
    """The voice-print sex vote (core.speaker.sex_vote) of the person a voice was trained on, from its project's
    reference voice; kept in meta.json (extra.print_vote) once measured. None for a voice without a project (a
    downloaded one), or when it can't be measured: then SpeakEasy's Reader goes by pitch alone."""
    ex = v.extra or {}
    if "print_vote" in ex:
        return ex["print_vote"]
    if not ex.get("project"):
        return None
    from ..core.speaker import labelled_prints, sex_vote
    from .project import Project
    if not labelled_prints():
        return None   # the prints file is missing: nothing to vote with (not kept, so it's measured once it's back)
    try:
        proj = Project.load(ex["project"])
        vote = sex_vote(proj.load_embeddings().get(f"speaker:{proj.reference.get('cluster')}"))
    except Exception:   # noqa: BLE001 - a project gone or unreadable: the pitch decides
        return None
    import json   # kept, a None too (a project with no reference voice): its embeddings are read once, not on every listing
    from ..core import fsutil
    try:
        v.extra = {**ex, "print_vote": vote}
        fsutil.write_text(v.dir / "meta.json", json.dumps(v.to_json(), indent=2))
    except Exception:   # noqa: BLE001 - measured again next time
        pass
    return vote


def _remember_range(v, rng: list, from_speech: bool, person: str):
    """A community voice that passed the check for a person: keep that person's measured range with the voice, so
    "Fit pitch to the voice" works for it on manual covers too."""
    import json
    from ..core import fsutil
    try:
        v.extra.update(person_range=rng, person_range_speech=bool(from_speech), person=person)
        fsutil.write_text(v.dir / "meta.json", json.dumps(v.to_json(), indent=2))
    except Exception:
        pass


# ---------------------------------------------------------------- how far to trust a voice (phase 15)

WEAK_LIKENESS = 0.65   # below: it doesn't sound like them yet (Obama's approved voice 0.70; Peter Griffin's 3.5-min 0.50)
WEAK_MINUTES = 5.0     # below: too little clean audio to learn the voice from
SCENE_SHARE = 0.5      # a character's voice: at least half of what it learned from should be the character in scenes


def voice_quality(v) -> dict:
    """How far to trust a voice trained here before a cover is made with it (the one-shot's gate, phase 15: "Toxic as
    SpongeBob" came from a voice trained on a podcast about SpongeBob, and nothing said so). Measures only what's on
    disk: the likeness at export, the minutes it learned from, and for a fictional character the share of those
    minutes that came from scenes (not from talk about the character). Voices from elsewhere aren't judged here
    (the one-shot checks them against the person already).
    -> {ok, problems: [plain sentences], summary, advice, action: {kind, target}, scene_share?}"""
    ex = v.extra or {}
    out = {"ok": True, "problems": []}
    if not ex.get("trained"):
        return out
    who = ex.get("person") or display_name(v)
    lk, mins = ex.get("likeness"), ex.get("train_minutes")
    if lk is not None and lk < WEAK_LIKENESS:
        out["problems"].append(f"it doesn't sound much like {who} yet (likeness {lk:.2f})")
    if mins is not None and mins < WEAK_MINUTES:
        out["problems"].append(f"it learned from only {mins:.1f} min of {who}")
    try:
        from . import project as P
        from .sources import kinds
        proj = P.Project.load(ex["project"]) if ex.get("project") else None
    except (ModelError, KeyError):
        proj = None
    k = kinds(proj) if proj else {}
    if k:
        kept = [p for p in proj.training_phrases() if not p.sung]
        total = sum(p.dur for p in kept)
        if total:
            share = sum(p.dur for p in kept if k.get(p.source) == "scene") / total
            out["scene_share"] = round(share, 2)
            if share < SCENE_SHARE:
                talk = sorted({s.title for s in proj.sources if k.get(s.id) == "talk" and any(p.source == s.id for p in kept)})
                out["problems"].append(f"{1 - share:.0%} of what it learned from isn't {who} in scenes but talk about "
                                       f"{who} (podcasts, interviews, cast videos" + (f": “{talk[0][:60]}”" if talk else "") + ")")
    if out["problems"]:
        out.update(ok=False, summary=f"This {who} voice is weak: " + "; ".join(out["problems"]) + ". A cover will sound like that.",
                   advice=f"Find better clips of {who} and train it again", action={"kind": "improve", "target": v.name})
    return out


def check_names(proj, person: str, push):
    """The sanity check of an automatic pick for a character (phase 19): the other characters say its name, it
    rarely does. When the voice picked says the name 2+ times and another voice heard at least half as much in the
    scenes never does, that one is the character: switch, and say so. A user's own pick is never second-guessed.
    Best effort: Whisper failing changes nothing."""
    from . import project as P
    from . import sources as srcmod
    proj = P.Project.load(proj.name)
    ref = proj.reference
    if not ref.get("auto") or not ref.get("scene_minutes") or not srcmod.is_character_project(proj):
        return proj
    if str(ref["cluster"]) in (ref.get("names") or {}) and ref.get("names_for") == ref.get("clip"):
        return proj   # already checked for this pick (a Resume, or the train step after a prepare)
    top = [ref] + [sp for sp in proj.speakers if sp["cluster"] != ref["cluster"]
                   and sp.get("scene_minutes", 0) >= 0.5 * ref["scene_minutes"]][:2]
    if len(top) < 2:
        return proj
    try:
        push("voice", 0.11, f"Checking that the voice picked is {person}: who says “{person}”")
        said = P.names_said(proj, P.name_words(person), top)
    except Exception:
        return proj
    me = said.get(ref["cluster"], {})
    quiet = [sp for sp in top[1:] if said.get(sp["cluster"], {}).get("said", 9) == 0
             and said[sp["cluster"]]["words"] >= 0.5 * me.get("words", 0)]
    with P.lock(proj.name):
        proj = P.Project.load(proj.name)
        proj.reference["names"] = {str(k): v for k, v in said.items()}
        proj.reference["names_for"] = proj.reference.get("clip")
        if me.get("said", 0) >= 2 and quiet:
            alt = quiet[0]
            note = (f"The voice heard most said “{person}” {me['said']} times, so it's most likely someone talking to "
                    f"{person}; using the voice that never says it ({alt['scene_minutes']} min in scenes).")
            P.set_reference(proj, cluster=alt["cluster"], auto=True)
            proj.reference.update(names={str(k): v for k, v in said.items()}, names_for=proj.reference.get("clip"),
                                  names_note=note)
            push("voice", 0.11, note)
        proj.save()
    return P.Project.load(proj.name)


def train_note(v, q: dict) -> dict:
    """Train a new voice ticked while a voice trained here exists: it's used, not trained over (that's Improve's job,
    which keeps the project's clips and choices). Said plainly in the plan instead of silently ignoring the box."""
    who = (v.extra or {}).get("person") or display_name(v)
    return {**q, "kept": f"You already have “{display_name(v)}”, trained here: Train a new voice uses it and doesn't "
                         f"train over it. To train it again on better clips, use Improve.",
            "advice": q.get("advice") or f"Find better clips of {who} and train it again",
            "action": q.get("action") or {"kind": "improve", "target": v.name}}


# ---------------------------------------------------------------- training a new voice

NOT_USABLE = ("may be", "a reaction", "edited", "no vocals", "probably", "talk about")   # source notes that mean someone else
# ("may be in Spanish (a dub...)" starts with "may be" too)


class PrepTime:
    """Do it all's preparation, said over the whole job (rc-fix3, IA pass 11 B2: "(1/2)" and "(2/2)" again every round
    for 19 min read like a loop, and the time left climbed from 3598 to 4354 s at "about 70 min").
    - The count runs over the job: "Preparing clips, source 7 of 12", where 12 is every ticked source not prepared
      yet plus the ones done (it grows when more are ticked, never resets).
    - The time left comes from the work: seconds per source so far, times the sources still needed to reach the
      target minutes at the clean minutes per source so far, then the rest of the plan (training, compare). The job
      says it through its usual rule (jobs.Job._said): it counts down and rises at most slowly.
    - Without a plan (a one-shot's own estimate covers it) there's no estimate here: the job extrapolates as before."""

    def __init__(self, plan_min: dict | None, target_min: float):
        self.plan = plan_min or {}
        self.target = float(target_min)
        self.done, self.secs, self.kept, self.total, self._frac = 0, 0.0, 0.0, 0, 0.02

    def line(self, index: int, label: str) -> str:
        return f"Preparing clips, source {self.done + index} of {max(self.total, self.done + index)}: {label}"

    def frac(self, f: float) -> float:
        self._frac = max(self._frac, f)   # never backwards, even when more sources are added
        return self._frac

    def finished(self, n: int, secs: float):
        self.done += n
        self.secs += secs

    def left(self, batch_left: float = 0.0) -> float | None:
        if not self.plan.get("total"):
            return None
        per = self.secs / self.done if self.done else self.plan.get("prepare", 0) * 60.0 / max(1, self.total)
        if self.done and self.kept > 0:
            need = max(0.0, self.target - self.kept) / (self.kept / self.done)
        else:
            need = max(0, self.total - self.done)
        return per * max(batch_left, need) + max(0.0, self.plan["total"] - self.plan.get("prepare", 0)) * 60.0


def train_new_voice(job, person: str, control, push, project: str | None = None,
                    target_minutes: float | None = None, prepare_only: bool = False, speaking: bool = False,
                    subject: dict | None = None, mode: str | None = None) -> str:
    """Find clips -> prepare (topping up sources until the target) -> auto-confirm -> train -> add. Returns voice name.
    project: continue that Train-tab project (its ticked sources, choices and reference are kept).
    prepare_only: stop once the clips are ready and return the PROJECT name (a cadence for a downloaded voice);
    speaking: a new project gathers speech even for a singer (a cadence is about how they talk);
    mode: speaking / singing / both as the person chose it (a queued "Train tonight"); a project that has searched
    already keeps its own."""
    from . import api as train_api
    from . import project as P
    from . import sources as srcmod
    from ..core.speaker import SpeakerEmbedder
    from .trainer import Trainer, epoch_line, export_voice
    TC = {**CONFIG["training"], **({"target_minutes": target_minutes} if target_minutes else {})}
    key = slug(person)
    if project:
        proj = P.Project.load(project)
    elif speaking:
        from .night import _project_named
        proj = _project_named(person) or P.Project.create(person, "speaking")
    else:
        proj = project_for(person, mode, subject)
    busy = next((n for n, a in train_api.ACTIVE.items() if a["job"] is not job and not a["job"].finished
                 and (n == proj.name or a["kind"] in ("train", "auto"))), None)
    if busy:
        raise ModelError(f"'{busy}' is being prepared or trained right now. One voice trains at a time; "
                         "try again when it's done (or pause it).")
    train_api.ACTIVE[proj.name] = {"job": job, "control": control, "kind": "auto"}
    try:
        proj = apply_mode(proj, person, mode)   # once the project is ours (a reused one too: the queue's)
        if not proj.sources:
            push("voice", 0.01, f"Searching YouTube for {person}")
            from .subject import search_kind
            force, words = search_kind(subject)   # the one-shot plan's reading of the request, or the user's correction
            proj.sources = srcmod.search(person, proj.mode, force=force, words=words)
            proj.save()
        emb = SpeakerEmbedder()
        # Singers: the voice will sing, so gather singing first; interviews only top up when songs run out.
        singer = proj.mode != "speaking"
        sung_first = (lambda s: not srcmod.is_sung(s)) if singer else (lambda s: False)
        searched_more = False
        searched_again = False
        # a character's scenes yield little clean voice (SpongeBob: about 0.3 min per upload, music, SFX and the
        # other characters), so it gets as many rounds as a singer
        rounds = 12 if singer or srcmod.is_character_project(proj) else 8

        def enough(pr):
            return (pr.sung_seconds() if singer and not songs_exhausted(pr) else pr.kept_seconds()) / 60 >= TC["target_minutes"]

        def songs_exhausted(pr):
            return not [s for s in pr.sources if srcmod.is_sung(s) and s.status == "found"
                        and not s.note.startswith(NOT_USABLE)]
        # rc-fix3 (IA pass 11 B2): one count over the whole job and a time left from the work itself (PrepTime)
        prep = PrepTime(getattr(job, "plan_min", None), TC["target_minutes"])

        def said(frac, msg, batch_left=0.0):
            left = prep.left(batch_left)
            push("voice", prep.frac(frac), msg, **({"left_est": round(left, 1)} if left is not None else {}))
        for rnd in range(rounds):
            proj = P.Project.load(proj.name)
            if proj.reference.get("clip") and enough(proj):
                break   # enough clean voice already (e.g. from the quality check's clips)
            prep.kept = (proj.sung_seconds() if singer and not songs_exhausted(proj) else proj.kept_seconds()) / 60
            # a few sources at a time, re-checking the total in between, instead of everything that's ticked
            # a channel whose videos turned out unavailable here (whatever yt-dlp's reason: why_unavailable) goes
            # last, so the next tries are other uploads of the same scenes
            locked = {s.channel for s in proj.sources if s.status == "failed" and srcmod.why_unavailable(s.error or "")}
            # the best first (smallfix7): the user's own picks, then by the clean voice each should give (a speech over
            # a rally, recent over decades old), not the order they were found in
            character = srcmod.is_character_project(proj)
            todo = [s.id for s in sorted(proj.sources, key=lambda s: (sung_first(s), s.channel in locked, not s.user,
                                                                      -srcmod.expected_clean(s, person, proj.mode, character)))
                    if s.selected and s.status not in ("ready", "failed")]
            if singer and not songs_exhausted(proj):
                todo = [i for i in todo if srcmod.is_sung(proj.source(i))]
            prep.total = prep.done + len(todo)   # every ticked source not prepared yet, and the ones done
            todo = todo[:3 if singer else 2]
            if todo:
                def prog(f, label, n=len(todo), **kw):
                    said(0.02 + 0.08 * min(1.0, (prep.done + f * n) / max(1, prep.total)),
                         prep.line(kw.get("index") or 1, label), batch_left=n * (1 - f))
                t_batch = time.time()
                try:
                    P.prepare_sources(proj.name, todo, gpu.MODELS.separator, emb, progress=prog, between=control.service,
                                      cancel=lambda: job.cancel or job.pausing,
                                      stop=lambda: job.cancel or getattr(control, "stop", False),
                                      friendly=lambda e: jobs.friendly(e, "preparing a source"))
                except P.Cancelled:
                    raise Paused() if job.pausing else jobs.Cancelled()
                prep.finished(len(todo), time.time() - t_batch)
            with P.lock(proj.name):
                proj = P.Project.load(proj.name)
                if proj.speakers and not proj.reference.get("confirmed"):
                    P.set_reference(proj, cluster=proj.reference["cluster"], auto=True)   # the dominant voice
                    proj.save()
                # a source being theirs is checked, not assumed (smallfix7): a prepared one whose clips aren't clearly
                # the person (talk about them, another voice) is left out by the same gate Improve uses. A real person
                # who speaks only: a character's scenes and a singer's songs hold other voices by nature.
                gate = None
                if proj.reference.get("confirmed") and proj.mode == "speaking" and not srcmod.is_character_project(proj):
                    from . import frozen
                    gate = frozen.gate_project(proj, proj.load_embeddings(), None, None, by="DoubleAgent")
                    if gate["left_out"]:
                        proj.save()
            if gate and gate["left_out"]:
                push("voice", 0.1, f"Left out {len(gate['left_out'])} source{'s' if len(gate['left_out']) != 1 else ''} "
                     f"that didn't sound clearly like {person} ({gate['left_out'][0]['title'][:40]}: {gate['left_out'][0]['why']})")
            if proj.reference.get("also"):   # a recast or another era: said, not decided silently
                push("voice", 0.1, proj.reference["also"]["note"])
            mins = proj.kept_seconds() / 60
            if enough(proj):
                break
            character = srcmod.is_character_project(proj)
            more = sorted([s for s in proj.sources if not s.selected
                           and not srcmod.left_out_for_a_reason(s)   # (talk about them, a younger voice...: rc-polish1)
                           and s.status == "found" and s.duration >= (90 if singer and srcmod.is_sung(s) else 120)],
                          key=lambda s: (sung_first(s), -srcmod.expected_clean(s, person, proj.mode, character)))
            if singer and not searched_more and not [s for s in more if srcmod.is_sung(s)]:
                # out of songs: look for more of them before settling for interviews
                searched_more = True
                try:
                    extra = srcmod.search(person, "singing")
                except ModelError:
                    extra = []
                with P.lock(proj.name):
                    proj = P.Project.load(proj.name)
                    have = {s.id for s in proj.sources}
                    new = [s for s in extra if s.id not in have and s.selected and srcmod.is_sung(s)]
                    proj.sources += new
                    proj.save()
                more = new + more
            blocked, tried = srcmod.blocked_share(proj.sources)
            if not more and not searched_again and not [s for s in proj.sources if s.selected and s.status not in ("ready", "failed")]:
                # everything found is used and it's still short (most of it couldn't be had here, or it yielded
                # little clean voice): other uploads, other words, other sites, once
                searched_again = True
                character = srcmod.is_character_project(proj)
                have = {s.id for s in proj.sources}
                try:
                    extra = srcmod.search_more(person, have, character)
                except ModelError:
                    extra = []
                try:   # not one site: what other sites yt-dlp searches have of them too
                    extra += srcmod.search_other_sites(person, character, have | {s.id for s in extra})
                except Exception:
                    pass
                if extra:
                    with P.lock(proj.name):
                        proj = P.Project.load(proj.name)
                        proj.sources += [s for s in extra if s.id not in {x.id for x in proj.sources}]
                        proj.save()
                    push("voice", 0.1, (f"{srcmod.unavailable_note(person, proj.sources)}; trying other uploads and other sites"
                                        if tried >= 3 and blocked >= tried / 2 else
                                        f"{mins:.1f} min of clean voice from everything found so far; looking for more of {person}"))
                    continue
            if not more and not [s for s in proj.sources if s.selected and s.status not in ("ready", "failed")]:
                break   # nothing ticked left to try, and nothing more to tick
            with P.lock(proj.name):
                proj = P.Project.load(proj.name)
                for s in proj.sources:
                    if s.id in {m.id for m in more[:3 if singer else 2]}:
                        s.selected = True
                proj.save()
            prep.kept = (proj.sung_seconds() if singer and not songs_exhausted(proj) else proj.kept_seconds()) / 60
            said(0.0, f"{proj.sung_seconds() / 60:.1f} min of {person} singing so far; adding more songs"
                 if singer else f"{mins:.1f} min of clean voice so far; adding more sources")
        proj = check_names(proj, person, push)
        mins = proj.kept_seconds() / 60
        if mins < 3:
            blocked, tried = srcmod.blocked_share(proj.sources)
            if tried >= 3 and blocked >= tried / 2:
                raise ModelError(f"{srcmod.unavailable_note(person, proj.sources)}, so only {mins:.1f} minutes of {person} "
                                 f"came through (3+ needed). Other uploads and other sites were tried too. If you have clips "
                                 f"of {person}, add them in DoubleAgent (links or recordings), then try again.")
            raise ModelError(f"Only {mins:.1f} minutes of {person}'s voice could be found online (3+ needed). "
                             "Open DoubleAgent to add links or recordings, then try again.")
        if prepare_only:
            return proj.name
        push("voice", 0.12, f"Training a voice from {mins:.1f} min of {person}")
        st = proj.train
        if st.get("state") != "paused":
            for k in ("epoch", "best", "saves", "state", "outcome", "epochs"):   # exported stays: the voice is still there
                st.pop(k, None)
            (proj.dir / "train" / "resume.pt").unlink(missing_ok=True)
        st.update(sr=st.get("sr") or TC["sample_rate"], length="auto", voice_name=st.get("voice_name") or f"{key}-uc")
        proj.save_train()
        conv = gpu.MODELS.converter
        tr = Trainer(proj, conv.hubert, conv.rmvpe, device=str(conv.device), half=conv.half, converter=conv)

        def tprog(**ev):
            if job.cancel:
                control.stop = True
            if ev.get("stage") == "training" and ev.get("epochs"):
                push("voice", 0.12 + 0.85 * ev.get("fraction", 0),
                     epoch_line(ev), eta=ev.get("eta"), epoch=ev.get("epoch"), epochs=ev["epochs"], planned=ev.get("planned"))
            elif ev.get("stage") == "preparing":
                push("voice", 0.12, f"Training: {ev.get('note', 'getting ready')}")
        st = tr.run(control, progress=tprog)
        if job.cancel:
            raise jobs.Cancelled()
        if st.get("state") == "paused":
            raise Paused()
        # "Checking it" (DoubleAgent's fourth stage): the saves were measured against real clips while it trained
        # (likeness, melody, words); the last one (an early stop's, between the regular saves) is measured now
        push("voice", 0.975, "Checking it against real clips")
        tr.check_last(st)
        best = next((s for s in st.get("saves", []) if s["epoch"] == st.get("best")), {})
        if best.get("identity") is not None:
            push("voice", 0.977, f"Checking it against real clips: epoch {best['epoch']} sounds most like {person}")
        push("voice", 0.98, "Adding the new voice")
        v = export_voice(P.Project.load(proj.name), overwrite=True)
        return v.name
    finally:
        train_api.ACTIVE.pop(proj.name, None)


def run_project(job, name: str, control, push) -> str:
    """A project queued from DoubleAgent's Deep path (the user's "queue Trump with specific videos selected"): its own
    steps with its own choices, overnight. The sources ticked there (no new search, no source changes), its mode,
    length, sample rate and voice name (another person's voice name is refused: train_api.check_voice_name). Prepare
    what isn't prepared, confirm the main voice only if nobody did, train (a paused training continues), add the
    voice. -> the voice name."""
    from . import api as train_api
    from . import project as P
    from ..core.speaker import SpeakerEmbedder
    from .trainer import LENGTHS, Trainer, epoch_line, export_voice
    proj = P.Project.load(name)
    busy = next((n for n, a in train_api.ACTIVE.items() if a["job"] is not job and not a["job"].finished
                 and (n == proj.name or a["kind"] in ("train", "auto"))), None)
    if busy:
        raise ModelError(f"'{busy}' is being prepared or trained right now. One voice trains at a time.")
    vname = slug(proj.train.get("voice_name") or f"{proj.name}-uc")
    train_api.check_voice_name(proj, vname)   # before any work: never another person's voice
    train_api.ACTIVE[proj.name] = {"job": job, "control": control, "kind": "auto"}
    try:
        todo = [s.id for s in proj.sources if s.selected and s.status not in ("ready", "failed")]
        if todo:
            def prog(f, label, **kw):
                push("voice", 0.02 + 0.1 * f, f"Preparing clips, source {kw['index']} of {kw['count']}: {label}" if kw.get("count")
                     else f"Preparing clips: {label}")
            try:
                P.prepare_sources(proj.name, todo, gpu.MODELS.separator, SpeakerEmbedder(), progress=prog,
                                  between=control.service, cancel=lambda: job.cancel or job.pausing,
                                  stop=lambda: job.cancel or getattr(control, "stop", False),
                                  friendly=lambda e: jobs.friendly(e, "preparing a source"))
            except P.Cancelled:
                raise Paused() if job.pausing else jobs.Cancelled()
        with P.lock(proj.name):
            proj = P.Project.load(proj.name)
            if proj.speakers and not proj.reference.get("confirmed"):
                P.set_reference(proj, cluster=proj.reference["cluster"], auto=True)   # nobody picked: the dominant voice
                proj.save()
        mins = proj.kept_seconds() / 60
        if mins < 3:
            raise ModelError(f"Only {mins:.1f} minutes of {proj.person or proj.name}'s voice are ready from the sources "
                             "ticked (3+ needed). Tick more sources in DoubleAgent, then queue it again.")
        st = proj.train
        if st.get("state") != "paused":
            for k in ("epoch", "best", "saves", "state", "outcome", "epochs"):   # exported stays: the voice is still there
                st.pop(k, None)
            (proj.dir / "train" / "resume.pt").unlink(missing_ok=True)
        st.update(sr=st.get("sr") or CONFIG["training"]["sample_rate"],
                  length=st.get("length") if st.get("length") in LENGTHS else "auto", voice_name=vname)
        proj.save_train()
        push("voice", 0.12, f"Training a voice from {mins:.1f} min of {proj.person or proj.name}")
        conv = gpu.MODELS.converter
        tr = Trainer(proj, conv.hubert, conv.rmvpe, device=str(conv.device), half=conv.half, converter=conv)

        def tprog(**ev):
            if job.cancel:
                control.stop = True
            if ev.get("stage") == "training" and ev.get("epochs"):
                push("voice", 0.12 + 0.85 * ev.get("fraction", 0),
                     epoch_line(ev), eta=ev.get("eta"), epoch=ev.get("epoch"), epochs=ev["epochs"], planned=ev.get("planned"))
        st = tr.run(control, progress=tprog)
        if job.cancel:
            raise jobs.Cancelled()
        if st.get("state") == "paused":
            raise Paused()
        push("voice", 0.975, "Checking it against real clips")
        tr.check_last(st)
        push("voice", 0.98, "Adding the new voice")
        return export_voice(P.Project.load(proj.name), name=vname, overwrite=True).name
    finally:
        train_api.ACTIVE.pop(proj.name, None)


def noisiness(x, sr: int) -> float:
    """How hissy a recording is (spectral flatness of its voiced parts; ~0.2 for a clean voice)."""
    from .project import noisiness as n
    return n(x, sr)


# ---------------------------------------------------------------- cadence (phase 4), for SpeakEasy

def cadence_pack(v) -> dict | None:
    """The voice's cadence (extra.tts + its reference clip), or None: SpeakEasy reads with it when present."""
    from .cadence import pack_of
    return pack_of(v)


def pace_needs_calibration(v) -> bool:
    """The voice's cadence hasn't had its pace matched to the person's for the engine that reads it now (phase 23)."""
    from .cadence import pace_needs_calibration as needs
    return needs((v.extra or {}).get("tts"))


def calibrate_pace(v, check=None):
    from .cadence import calibrate_voice_pace
    return calibrate_voice_pace(v.name, check)


def cadence_ref(v) -> Path:
    return v.dir / "tts" / ((v.extra or {}).get("tts") or {}).get("ref", "ref.wav")


def cadence_status(v) -> dict:
    """For a voice row: has a cadence? can one be made (and how)? queued or running in the queue?"""
    from ..core import worklist
    from .cadence import can_train, pending_of
    pack = cadence_pack(v)
    can, why = can_train(v)
    q = next((i for i in worklist._state()["queue"] if i["kind"] in ("cadence", "newcad") and i["state"] in ("queued", "paused", "running")
              and (i["target"] == v.name or f"{slug(i['target'])}-uc" == v.name)), None)
    out = {"has": bool(pack), "can": can, "why": why, "queue": q and q["state"]}
    if pack:
        out.update(engine=pack.get("engine"), made=pack.get("made"), data_minutes=pack.get("data_minutes"),
                   scores=pack.get("scores"), gap_s=pack.get("gap_s"), tempo=pack.get("tempo"),
                   pending=pending_of(v) is not None)   # a new one waits for the user's ear (phase 9, "Keep this one")
    return out


def start_cadence(voice: str, now: bool = True) -> dict:
    """"Train cadence" (or "Find clips + cadence"): a queue item, run now (when the GPU is free) or tonight."""
    from ..core import worklist
    return worklist.add("cadence", voice, now=now)


def start_new_voice(person: str, cadence: bool = True, now: bool = True) -> dict:
    """"New voice" from SpeakEasy: DoubleAgent's full pipeline (voice, then its cadence); the voice shows up in
    FrontMan too."""
    from ..core import worklist
    return worklist.add("newcad" if cadence else "new", person, now=now)
