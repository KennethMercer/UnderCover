"""One-shot covers: "cover of Party In The USA by Morgan Freeman" -> finished cover, with the best settings.

Steps (one GPU job, so covers started meanwhile still run between training steps):
  1. song   YouTube search -> best studio/official upload -> downloaded into uploads/ (reused if fetched before)
  2. voice  a voice trained here for that person -> else community models (installed, then online) that PASS a
            measured check against real clips of the person (voicecheck.py; results remembered) -> else train
            one: find clips, prepare (more sources if short of the target), auto-confirm the dominant voice,
            train ("auto" length), add the best save to the voices. The clips fetched for the check are the
            start of the training data, so nothing is wasted.
  3. cover  best settings: high-quality separation, lead/backing split with the same voice on both, de-reverb
            with matching reverb put back, polish on, pitch cleanup on; octave picked from the voice's and the
            song's typical pitch when the voice's range is known (voices trained here)
"""
import math
import re
import time
from pathlib import Path

import numpy as np

from .config import CONFIG
from .models import ModelError, slug

PATTERNS = [
    # "cover of X by Y" / "X cover by Y" / "make X sung by Y" (the LAST "by" wins: "X by Miley Cyrus by Y")
    r"^(?:please\s+)?(?:make|do|create|give me|generate)?\s*(?:me\s+)?(?:an?\s+)?(?:ai\s+)?cover\s+of\s+(?P<song>.+)\s+(?:by|in the voice of|as sung by|sung by|with the voice of|with|using|as)\s+(?P<person>[^,;]+?)$",
    r"^(?P<song>.+?)\s+cover\s+(?:by|in the voice of|with|using|as)\s+(?P<person>[^,;]+?)$",
    r"^(?P<person>.+?)\s+(?:sings|singing|covers|covering|performs|doing)\s+(?P<song>.+)$",
    r"^(?:make|do|have)?\s*(?P<person>.+?)\s+(?:sing|cover)\s+(?P<song>.+)$",
    r"^(?P<song>.+?)\s+(?:sung by|as sung by|in the voice of|but (?:it's|its|it is|sung by))\s+(?P<person>[^,;]+?)$",
    r"^(?P<song>.+)\s+by\s+(?P<person>[^,;]+?)$",
]


# Versions people ask for ("an acoustic cover of X", "a metal version of X", "X but bluegrass"). Any style words
# work; this table only adds the other words a matching YouTube title may use instead.
STYLES = {
    "acoustic": ["acoustic", "unplugged", "stripped"], "unplugged": ["unplugged", "acoustic"],
    "stripped": ["stripped", "acoustic"], "piano": ["piano"], "orchestral": ["orchestral", "orchestra", "symphonic"],
    "jazz": ["jazz"], "lofi": ["lofi", "lo-fi", "lo fi"], "lo-fi": ["lofi", "lo-fi", "lo fi"], "rock": ["rock"],
    "metal": ["metal"], "punk": ["punk"], "country": ["country"], "folk": ["folk"], "reggae": ["reggae"],
    "live": ["live"], "a cappella": ["a cappella", "acapella", "acappella"], "acapella": ["a cappella", "acapella"],
    "synthwave": ["synthwave", "80s"], "8-bit": ["8-bit", "8 bit", "chiptune"],
}
# words that describe the request, not a musical style ("an AI cover of", "a new version of")
NOT_STYLE = {"ai", "rvc", "new", "good", "great", "quick", "short", "full", "nice", "cool", "funny", "my", "the", "a", "an",
             "your", "cover", "song", "voice", "proper", "real", "simple", "fake"}
_STYLE_WORDS = r"[a-z0-9][a-z0-9\-']*(?:\s+[a-z0-9][a-z0-9\-']*){0,2}"   # 1-3 words


def style_words(style: str | None) -> list[str] | None:
    """Words a YouTube title for this version should contain (the style itself plus known synonyms)."""
    if not style:
        return None
    return list(dict.fromkeys(STYLES.get(style) or [style, style.replace("-", " "), style.replace(" ", "")]))


def _clean_style(st: str) -> str | None:
    words = [w for w in st.lower().split() if w not in NOT_STYLE]
    return " ".join(words) or None


def parse_request(text: str) -> dict:
    """-> {song, person, style}. style is None for "the record"; else a key of STYLES."""
    t = re.sub(r"\s+", " ", text.strip().strip(".!?")).strip()
    style = None
    m = re.match(rf"^(?:please\s+)?(?:make|do|create|give me|generate)?\s*(?:me\s+)?(?:an?\s+)?(?P<style>{_STYLE_WORDS})\s+(?:cover|version|rendition|remix)\s+of\s+", t, re.I)
    if m and _clean_style(m.group("style")):   # "a metal cover of X ..." -> "cover of X ...", style "metal"
        style = _clean_style(m.group("style"))
        t = "cover of " + t[m.end():]
    else:
        m = re.search(rf"\s+(?:but|in an?|as an?|in)\s+(?P<style>{_STYLE_WORDS})(?:\s+(?:version|style|cover))?$", t, re.I)
        if m and not re.search(r"\b(?:sung|sings|sing|by|voice)\b", m.group("style"), re.I) and _clean_style(m.group("style")):
            style = _clean_style(m.group("style"))   # "X sung by Y but bluegrass"
            t = t[:m.start()]
    song, person = parse(t)
    m = re.search(rf"\s*[\(\[]\s*(?P<style>{_STYLE_WORDS})\s+(?:version|cover)\s*[\)\]]$", song, re.I)
    if m and not style and _clean_style(m.group("style")):   # "Plug In Baby (acoustic version) by ..."
        style = _clean_style(m.group("style"))
        song = song[:m.start()]
    return {"song": song.strip(), "person": person, "style": style}


def parse(text: str) -> tuple[str, str]:
    """-> (song query, person). Raises ModelError with an example when it can't tell them apart."""
    t = re.sub(r"\s+", " ", text.strip().strip(".!?")).strip()
    for pat in PATTERNS:
        m = re.match(pat, t, re.I)
        if m:
            song = m.group("song").strip(" \"'“”‘’")
            person = re.sub(r"^(?:the voice of|voice of)\s+", "", m.group("person").strip(" \"'“”‘’"), flags=re.I)
            person = re.sub(r"'s voice$|\s+voice$", "", person, flags=re.I)
            person = re.sub(r"^(?:sung by|as sung by|by)\s+", "", person, flags=re.I).strip()
            song = re.sub(r"\s+(?:as\s+sung|sung|as|but|in the voice of)$", "", song, flags=re.I)   # "X as sung by Y"
            song = re.sub(r"[\(\[]\s*by\s+([^\)\]]+)[\)\]]", r"\1", song, flags=re.I).strip()   # "X (by Muse)" -> "X Muse"
            if song and person:
                return song, person
    raise ModelError('Say the song and the voice, for example: "cover of Party In The USA by Morgan Freeman".')


# ---------------------------------------------------------------- voice resolution

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
        from .train.project import Project
        spe = Project.load(ex["project"]).train.get("steps_per_epoch") or 0
    except Exception:
        return False
    return bool(spe) and int(ex.get("epoch") or 0) * spe < min_steps


def installed_for(person: str) -> list:
    """Installed voices for this person: exact name, or a name built from it (morgan-freeman-uc, ...).
    Voices trained in UnderCover first, then the newest."""
    from .models import list_voices
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
    cands = [v for v in list_voices() if matches(v.name) and not undertrained(v)]
    cands.sort(key=lambda v: (bool(v.extra.get("trained")), v.name == key, v.installed), reverse=True)
    return cands


def find_online(person: str):
    """Best community model whose name clearly names this person (all name words present)."""
    r = online_for(person, 1)
    return r[0] if r else None


def online_for(person: str, n: int = 2) -> list:
    """Community models online whose name contains every word of the person's name (best ranked first),
    skipping ones already installed and ones that failed the quality check before."""
    from .models import search, voices_dir
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
    from .config import path
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


def _remember(person: str, ident: str, q):
    import json
    c = _quality_cache()
    c[_qkey(person, ident)] = {**q.to_json(), "v": CHECK_VERSION, "checked": time.strftime("%Y-%m-%d %H:%M")}
    _quality_file().write_text(json.dumps(c, indent=1), encoding="utf-8")


def _remember_broken(person: str, ident: str, name: str, why: str):
    import json
    c = _quality_cache()
    c[_qkey(person, ident)] = {"voice": name, "score": 0.0, "baseline": 0.0, "phrases": 0, "seconds": 0.0,
                               "verdict": "broken", "note": why[:200], "checked": time.strftime("%Y-%m-%d %H:%M")}
    _quality_file().write_text(json.dumps(c, indent=1), encoding="utf-8")


def _ident(v) -> str:
    return f"installed:{v.name}:{v.installed}"


def is_musician(person: str) -> bool:
    """True when YouTube Music has album tracks by this person (so their singing voice is out there to learn
    from, not just interviews). ~3 s; any failure counts as "no"."""
    from . import songs
    toks = [w for w in re.findall(r"\w+", person.lower()) if len(w) > 1]
    for _ in range(2):
        try:
            tracks = songs._music_tracks(person, n=6)
        except Exception:
            tracks = []
        by = [t for t in tracks if all(tok in t.title.split(" - ")[0].lower() for tok in toks)]
        if len(by) >= 2:
            return True
        if tracks:   # got an answer, it's just not this person's music
            return False
    return False


def project_for(person: str):
    """The Train-tab project for this person that isn't finished yet (or a new one). Musicians get "both":
    interviews (for the voice check's other speakers) plus isolated vocals from their songs, so the trained
    voice has actually sung."""
    from .train import project as P
    key = slug(person)
    proj = next((P.Project.load(p["name"]) for p in P.list_projects()
                 if slug(p["person"]) == key and not p["train"].get("exported")), None)
    if proj is None:
        return P.Project.create(person, "both" if is_musician(person) else "speaking")
    if proj.mode == "speaking" and not proj.train.get("epoch") and is_musician(person):
        # an interviews-only project for a singer: add sources of them singing (nothing trained yet)
        from .train import source as srcmod
        try:
            sung = srcmod.search(person, "singing")
        except ModelError:
            sung = []
        with P.lock(proj.name):
            proj = P.Project.load(proj.name)
            have = {s.id for s in proj.sources}
            proj.sources += [x for x in sung if x.id not in have]
            proj.mode = "both"
            proj.save()
    return proj


def reference_clips(S, job, person: str, control, push):
    """Make sure the person's project has the real voice identified and some phrases by other people (the
    test material): prepares one source at a time, at most two. These are training clips too."""
    from .train import project as P
    from .train import source as srcmod
    from .train.speaker import SpeakerEmbedder
    proj = project_for(person)
    if not proj.sources:
        push("voice", 0.02, f"Finding clips of {person} to check voices against")
        proj.sources = srcmod.search(person, proj.mode)
        proj.save()

    def ready(pr):
        # The voice heard most could be an interviewer when only one source is in. Like the training page,
        # trust it once it's in 2+ sources, and only when there's other people's speech to test with.
        return (pr.reference.get("clip") and pr.reference.get("sources", 0) >= 2
                and any(x.reason == "other speaker" for x in pr.phrases))
    emb = SpeakerEmbedder()
    for _ in range(3):
        proj = P.Project.load(proj.name)
        if ready(proj):
            break
        todo = [s.id for s in proj.sources if s.selected and s.status not in ("ready", "failed")][:1]
        if not todo:
            break
        push("voice", 0.04, f"Getting real clips of {person} to check voices against")
        try:
            P.prepare_sources(proj.name, todo, S.ENGINE.separator, emb, cancel=lambda: job.cancel,
                              progress=lambda f, label, **kw: push("voice", 0.04 + 0.1 * f, f"Getting real clips of {person}: {label}"),
                              between=control.service,
                              friendly=lambda e: S.friendly(e, "preparing a source"))
        except P.Cancelled:
            raise S.Cancelled()
    proj = P.Project.load(proj.name)
    if proj.speakers and not proj.reference.get("confirmed"):
        with P.lock(proj.name):
            proj = P.Project.load(proj.name)
            P.set_reference(proj, cluster=proj.reference["cluster"])
            proj.save()
    return P.Project.load(proj.name)


def _prefetch_voice_sources(person: str):
    """Create/find the person's project, search their sources and download the first two (network only)."""
    from .train import source as srcmod
    try:
        proj = project_for(person)
        if not proj.sources:
            from .train import project as P
            found = srcmod.search(person, proj.mode)
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


def resolve_voice(S, job, person: str, prefer_train: bool, control, push, plan: dict):
    """-> installed Voice to use (training one if nothing measured good enough)."""
    from . import voicecheck
    from .models import download_and_install, get_voice, remove_voice
    from .train.speaker import SpeakerEmbedder
    cands = installed_for(person)
    trained = [v for v in cands if v.extra.get("trained")]
    if trained:
        plan["voice"] = {"name": trained[0].name, "how": "installed", "checked": "trained in UnderCover"}
        push("voice", 1.0, f"Using your voice “{trained[0].name}” (trained in UnderCover)")
        return trained[0]
    community = [] if prefer_train else cands
    online = [] if prefer_train else online_for(person, 2)
    checked = []
    if community or online:
        proj = reference_clips(S, job, person, control, push)
        conv, emb = S.ENGINE.converter, SpeakerEmbedder()

        def test(v, ident):
            cached = _quality_cache().get(_qkey(person, ident))
            if cached and cached.get("v") == CHECK_VERSION:   # older, weaker checks are redone once
                return voicecheck.Quality(**{k: cached.get(k) for k in ("voice", "score", "baseline", "phrases", "seconds",
                                                                        "verdict", "target_hz", "low", "pitch_cents", "content", "test")})
            push("voice", 0.16 + 0.04 * len(checked), f"Checking whether “{v.name}” sounds like {person}")
            q = voicecheck.check(v.name, proj, conv, emb)
            _remember(person, ident, q)
            return q
        downloaded = []

        def drop_downloads(keep=None):
            for name in downloaded:
                if name != keep:   # only the chosen download stays in the voice list
                    remove_voice(name)
                    if S.ENGINE._conv:
                        S.ENGINE._conv.voices.pop(name, None)
        try:
            for v in community:
                q = test(v, _ident(v))
                checked.append({"name": v.name, "where": "installed", **q.to_json()})
            if any(c["verdict"] == "good" for c in checked):
                online = []   # an installed voice already passes: no need to download more
            for r in online:
                if job.cancel:
                    raise S.Cancelled()
                ident = r.repo + "/" + r.file
                cached = _quality_cache().get(_qkey(person, ident))
                if cached and cached["verdict"] != "good":
                    continue
                push("voice", 0.16 + 0.04 * len(checked), f"Downloading the community voice “{r.name}” to check it")
                try:
                    v = download_and_install(r, progress=lambda d, t: push("voice", 0.16, f"Downloading “{r.name}”"))
                except ModelError as e:   # broken/unsafe/unloadable file: note it, try the next one
                    checked.append({"name": r.name, "where": "online", "repo": r.repo, "verdict": "broken", "note": str(e)[:160]})
                    _remember_broken(person, ident, r.name, str(e))
                    continue
                q = test(v, ident)
                checked.append({"name": v.name, "where": "online", "repo": r.repo, **q.to_json()})
                downloaded.append(v.name)
        except BaseException as e:
            if not isinstance(e, ValueError):   # cancelled or failed mid-check: remove the models fetched for it
                drop_downloads()
                raise
            # ValueError: no other-speaker phrases to test with, so we can't judge them and don't trust them
            checked.append({"name": "-", "verdict": "unknown", "note": "couldn't test (clips had only one speaker)"})
        plan["checked"] = checked
        good = sorted([c for c in checked if c.get("verdict") == "good"], key=lambda c: -c["score"])
        drop_downloads(keep=good[0]["name"] if good else None)
        if good:
            # the person's pitch from their kept clips: singing for singers (no speech headroom then)
            hz = voicecheck.target_pitch(proj, conv.rmvpe)
            plan["person_hz"], plan["person_speech"] = (hz, proj.from_speech()) if hz else (good[0].get("target_hz"), True)
            v = get_voice(good[0]["name"])
            plan["voice"] = {"name": v.name, "how": good[0]["where"] if good[0]["where"] == "online" else "installed",
                             "repo": good[0].get("repo"), "checked": f"sounds like {person}: {good[0]['score']:.2f} (pass {voicecheck.SUNG_GOOD if good[0].get('test') == 'sung' else voicecheck.GOOD}{', tested on singing' if good[0].get('test') == 'sung' else ''})"}
            push("voice", 1.0, f"Using “{v.name}”: it sounds like {person} ({good[0]['score']:.2f})")
            return v
        push("voice", 0.2, "None of the existing voices sounded enough like " + person + ", so UnderCover will train one")
    plan["voice"] = {"name": f"{slug(person)}-uc", "how": "train"}
    push("voice", 0.2 if checked else 0.0, f"Training a voice for {person} (about 45 minutes)")
    name = train_new_voice(S, job, person, control, push)
    v = get_voice(name)
    plan["voice"]["name"] = v.name
    if S.ENGINE._conv:
        S.ENGINE._conv.voices.pop(v.name, None)
    return v


def voice_f0(v) -> tuple[float | None, bool]:
    """(median training pitch Hz, trained from speech) for voices trained here; (None, False) otherwise."""
    ex = v.extra or {}
    if ex.get("f0_median") and (ex.get("f0_range") or not ex.get("project")):
        return float(ex["f0_median"]), bool(ex.get("from_speech"))
    if ex.get("project"):   # trained before f0 was recorded: compute from the project's data once
        import json
        import torch
        from .train.project import Project
        from .train.trainer import f0_median, f0_range
        try:
            proj = Project.load(ex["project"])
            data = torch.load(proj.dir / "train" / "data.pt", map_location="cpu", weights_only=True)
            ex["f0_median"], ex["from_speech"] = f0_median(data), proj.from_speech()
            ex["f0_range"] = f0_range(data)
            from . import fsutil
            fsutil.write_text(v.dir / "meta.json", json.dumps(v.to_json(), indent=2))
            return ex["f0_median"], ex["from_speech"]
        except Exception:
            pass
    return None, False


def pick_octave_range(f0_range: list, from_speech: bool, song_f0: np.ndarray) -> tuple[int, float]:
    """Octave that leaves the fewest sung notes outside what the voice was trained on (its 5th-95th percentile,
    -2 / +3 semitones of slack; +7 more on top for voices trained on speech, since people sing higher than they
    talk). Ties go to the octave nearest the singer's. -> (octave, share of notes still out of range)."""
    lo, _, hi = f0_range
    lo, hi = lo * 2 ** (-2 / 12), hi * 2 ** ((3 + (7 if from_speech else 0)) / 12)
    f = song_f0[song_f0 > 0]
    if len(f) < 50:
        return 0, 0.0
    best = None
    for octave in (0, -1, 1, -2, 2):   # nearest-to-the-singer first wins ties
        g = f * 2.0 ** octave
        out = float(np.mean((g < lo) | (g > hi)))
        if best is None or out < best[1] - 1e-9:
            best = (octave, out)
    return best[0], round(best[1], 3)


def pick_octave(voice_hz: float | None, from_speech: bool, song_hz: float | None) -> int:
    """Whole octaves that bring the song's melody nearest the voice's natural range. People sing roughly
    half an octave above their speaking pitch, so speech-trained voices are nudged up by that."""
    if not voice_hz or not song_hz:
        return 0
    target = voice_hz * (2 ** 0.5 if from_speech else 1.0)
    return int(np.clip(round(math.log2(target / song_hz)), -2, 2))


def song_vocal_f0(vocals: Path, rmvpe) -> float | None:
    return vocal_stats(vocals, rmvpe)[0]


def vocal_stats(vocals: Path, rmvpe) -> tuple[float | None, float]:
    """(median sung pitch Hz, share of the song with a voiced melody). Used to pick the octave, and to notice
    when a "cover" turns out to be instrumental (an acoustic guitar cover with no singing, say)."""
    from .convert import load_16k
    f0 = rmvpe.infer_from_audio(load_16k(vocals), thred=0.03)
    voiced = f0[f0 > 0]
    share = float(len(voiced)) / max(1, len(f0))
    _LAST_F0[str(vocals)] = voiced   # the melody's pitch track, reused to pick the octave
    return (round(float(np.median(voiced)), 1) if len(voiced) > 50 else None), round(share, 3)


_LAST_F0: dict = {}


def backing_monophony(backing: Path, rmvpe, seconds: int = 120) -> tuple[float, float]:
    """(share of the backing stem's energy explained by ONE pitch line, share of its loud frames that are pitched).
    RVC follows a single pitch, so stacked harmonies come out garbled when converted. Measured 2026-10-09:
    single lines 77-89% (acoustic cover, "Find Your Love"); stacked harmonies ~45% while 87-97% pitched
    (Bohemian Rhapsody, "when the party's over"); rap ad-libs score low because they're mostly unpitched."""
    from .convert import load_16k
    x = load_16k(backing)[: 16000 * seconds]
    f0 = rmvpe.infer_from_audio(x, thred=0.03)
    n_fft, hop = 2048, 160
    pad = np.pad(x, (n_fft // 2, n_fft // 2))
    frames = min(len(f0), (len(pad) - n_fft) // hop)
    if frames < 50:
        return 1.0, 0.0
    spec = np.abs(np.fft.rfft(pad[np.arange(frames)[:, None] * hop + np.arange(n_fft)[None]] * np.hanning(n_fft), axis=1)) ** 2
    freqs = np.fft.rfftfreq(n_fft, 1 / 16000)
    band = (freqs > 80) & (freqs < 6000)
    e = spec[:, band].sum(1)
    loud = e > np.percentile(e, 90) * 1e-3
    explained = 0.0
    for i in np.flatnonzero(loud & (f0[:frames] > 0)):
        h = f0[i] * np.arange(1, 40)
        near = (np.abs(freqs[None, :] - h[h < 6000][:, None]) < np.maximum(12.0, h[h < 6000][:, None] * 0.012)).any(0)
        explained += spec[i, near & band].sum()
    return float(explained / max(e[loud].sum(), 1e-12)), float(np.mean(f0[:frames][loud] > 0))


def chordal_backing(backing: Path | None, rmvpe) -> bool:
    if not backing:
        return False
    share, pitched = backing_monophony(backing, rmvpe)
    return pitched >= 0.8 and share < 0.5


MIN_SUNG = 0.12   # under ~12% of the song with a voiced melody = basically instrumental


# ---------------------------------------------------------------- training a new voice

NOT_USABLE = ("may be", "a reaction", "edited", "no vocals", "probably")   # source notes that mean someone else


def train_new_voice(S, job, person: str, control, push, project: str | None = None) -> str:
    """Find clips -> prepare (topping up sources until the target) -> auto-confirm -> train -> add. Returns voice name.
    project: continue that Train-tab project (its ticked sources, choices and reference are kept)."""
    from .train import api as train_api
    from .train import project as P
    from .train import source as srcmod
    from .train.speaker import SpeakerEmbedder
    from .train.trainer import Trainer, export_voice
    TC = CONFIG["training"]
    key = slug(person)
    proj = P.Project.load(project) if project else project_for(person)
    busy = next((n for n, a in train_api.ACTIVE.items() if a["job"] is not job and not a["job"].finished
                 and (n == proj.name or a["kind"] in ("train", "auto"))), None)
    if busy:
        raise ModelError(f"'{busy}' is being prepared or trained right now. One voice trains at a time; "
                         "try again when it's done (or pause it).")
    train_api.ACTIVE[proj.name] = {"job": job, "control": control, "kind": "auto"}
    try:
        if not proj.sources:
            push("voice", 0.01, f"Searching YouTube for {person}")
            proj.sources = srcmod.search(person, proj.mode)
            proj.save()
        emb = SpeakerEmbedder()
        # Singers: the voice will sing, so gather singing first; interviews only top up when songs run out.
        singer = proj.mode != "speaking"
        sung_first = (lambda s: not srcmod.is_sung(s.title)) if singer else (lambda s: False)
        searched_more = False

        def enough(pr):
            return (pr.sung_seconds() if singer and not songs_exhausted(pr) else pr.kept_seconds()) / 60 >= TC["target_minutes"]

        def songs_exhausted(pr):
            return not [s for s in pr.sources if srcmod.is_sung(s.title) and s.status == "found"
                        and not s.note.startswith(NOT_USABLE)]
        for rnd in range(12 if singer else 8):
            proj = P.Project.load(proj.name)
            if proj.reference.get("clip") and enough(proj):
                break   # enough clean voice already (e.g. from the quality check's clips)
            # a few sources at a time, re-checking the total in between, instead of everything that's ticked
            todo = [s.id for s in sorted(proj.sources, key=sung_first)
                    if s.selected and s.status not in ("ready", "failed")]
            if singer and not songs_exhausted(proj):
                todo = [i for i in todo if srcmod.is_sung(proj.source(i).title)]
            todo = todo[:3 if singer else 2]
            if todo:
                def prog(f, label, rnd=rnd, **kw):
                    push("voice", 0.02 + 0.08 * min(1, (rnd + f) / 2),
                         f"Preparing clips: {label}" + (f" ({kw['index']}/{kw['count']})" if kw.get("count") else ""))
                try:
                    P.prepare_sources(proj.name, todo, S.ENGINE.separator, emb, progress=prog, between=control.service,
                                      cancel=lambda: job.cancel, friendly=lambda e: S.friendly(e, "preparing a source"))
                except P.Cancelled:
                    raise S.Cancelled()
            with P.lock(proj.name):
                proj = P.Project.load(proj.name)
                if proj.speakers and not proj.reference.get("confirmed"):
                    P.set_reference(proj, cluster=proj.reference["cluster"])   # the dominant voice
                    proj.save()
            mins = proj.kept_seconds() / 60
            if enough(proj):
                break
            more = sorted([s for s in proj.sources if not s.selected and not s.note.startswith(NOT_USABLE)
                           and s.status == "found" and s.duration >= (90 if singer and srcmod.is_sung(s.title) else 120)],
                          key=sung_first)
            if singer and not searched_more and not [s for s in more if srcmod.is_sung(s.title)]:
                # out of songs: look for more of them before settling for interviews
                searched_more = True
                try:
                    extra = srcmod.search(person, "singing")
                except ModelError:
                    extra = []
                with P.lock(proj.name):
                    proj = P.Project.load(proj.name)
                    have = {s.id for s in proj.sources}
                    new = [s for s in extra if s.id not in have and s.selected and srcmod.is_sung(s.title)]
                    proj.sources += new
                    proj.save()
                more = new + more
            if not more:
                break
            with P.lock(proj.name):
                proj = P.Project.load(proj.name)
                for s in proj.sources:
                    if s.id in {m.id for m in more[:3 if singer else 2]}:
                        s.selected = True
                proj.save()
            push("voice", 0.1, f"{proj.sung_seconds() / 60:.1f} min of {person} singing so far; adding more songs"
                 if singer else f"{mins:.1f} min of clean voice so far; adding more sources")
        proj = P.Project.load(proj.name)
        mins = proj.kept_seconds() / 60
        if mins < 3:
            raise ModelError(f"Only {mins:.1f} minutes of {person}'s voice could be found online (3+ needed). "
                             "Open Train a voice to add links or recordings, then try again.")
        push("voice", 0.12, f"Training a voice from {mins:.1f} min of {person}")
        st = proj.train
        if st.get("state") != "paused":
            for k in ("epoch", "best", "saves", "state", "outcome", "exported", "exported_epoch", "epochs"):
                st.pop(k, None)
            (proj.dir / "train" / "resume.pt").unlink(missing_ok=True)
        st.update(sr=st.get("sr") or TC["sample_rate"], length="auto", voice_name=st.get("voice_name") or f"{key}-uc")
        proj.save_train()
        conv = S.ENGINE.converter
        tr = Trainer(proj, conv.hubert, conv.rmvpe, device=str(conv.device), half=conv.half, converter=conv)

        def tprog(**ev):
            if job.cancel:
                control.stop = True
            if ev.get("stage") == "training" and ev.get("epochs"):
                push("voice", 0.12 + 0.85 * ev.get("fraction", 0),
                     f"Training: epoch {ev.get('epoch', 0)} of {ev['epochs']}", eta=ev.get("eta"))
            elif ev.get("stage") == "preparing":
                push("voice", 0.12, f"Training: {ev.get('note', 'getting ready')}")
        st = tr.run(control, progress=tprog)
        if job.cancel:
            raise S.Cancelled()
        if st.get("state") == "paused":
            raise ModelError("Training was paused. Resume it on the Train a voice tab; the cover can be made after.")
        push("voice", 0.98, "Adding the new voice")
        v = export_voice(P.Project.load(proj.name), overwrite=True)
        return v.name
    finally:
        train_api.ACTIVE.pop(proj.name, None)


# ---------------------------------------------------------------- the job

def run(S, job, text: str, prefer_train: bool = False, song_url: str | None = None):
    from . import songs
    from .pipeline import CoverOptions
    from .separate import separate
    from .train.trainer import Control
    t0 = time.perf_counter()
    plan: dict = {"request": text}

    def push(stage, frac, message, **kw):
        if job.cancel:
            raise S.Cancelled()
        job.push(stage=stage, frac=round(frac, 3), message=message, plan=plan,
                 elapsed=round(time.perf_counter() - t0, 1), **kw)

    req = parse_request(text)
    song_q, person, style = req["song"], req["person"], req["style"]
    plan.update(song_query=song_q, person=person, style=style)

    # Network work for the voice starts now, alongside the song's download and separation (review C6): if there's no
    # voice trained here for this person, find their clips and pre-download the first sources (no GPU involved).
    early = None
    if not [v for v in installed_for(person) if v.extra.get("trained")]:
        import threading
        early = threading.Thread(target=_prefetch_voice_sources, args=(person,), daemon=True, name="voice-prefetch")
        early.start()

    # 1. song (a version like "acoustic" means a cover by someone else, so candidates are tried until one
    #    actually has singing in it)
    what = f"{style} version of “{song_q}”" if style else f"“{song_q}”"
    push("song", 0.0, f"Finding the {what} on YouTube" if style else f"Finding {what} on YouTube")
    cands = [songs.from_link(song_url)] if song_url else songs.search(song_q, style=style, words=style_words(style))[:3]
    skipped = []
    for i, pick in enumerate(cands):
        plan["song"] = {"title": pick.title, "channel": pick.channel, "url": pick.url, "note": pick.note, "style": style}
        push("song", 0.1, f"Downloading “{pick.title}”")
        meta = songs.download(pick, S.register_song, progress=lambda f: push("song", 0.1 + 0.4 * f, f"Downloading “{pick.title}”"))
        plan["song"]["id"] = meta["id"]
        song = Path(S.path("uploads_dir")) / meta["file"]
        # separate now (cached for the cover): catches a bad download early and gives the melody's pitch
        push("song", 0.55, "Separating the vocals")
        stems = separate(song, dereverb=True, karaoke=True, separator=S.ENGINE.separator, quality="high",
                         progress=lambda f, name: push("song", 0.55 + 0.4 * f, "Separating the vocals"))
        song_hz, sung = vocal_stats(stems["vocals"], S.ENGINE.converter.rmvpe)
        plan["song"]["sung"] = sung
        if sung >= MIN_SUNG or i == len(cands) - 1:
            break
        skipped.append(pick.title)
        push("song", 0.1, f"“{pick.title}” has hardly any singing (instrumental?), trying the next one")
    if plan["song"]["sung"] < MIN_SUNG:
        raise ModelError(f"Couldn't find a version of “{song_q}” with singing in it"
                         + (f" (tried {len(cands)})" if len(cands) > 1 else "") + ". Paste a link to one you like.")
    if skipped:
        plan["song"]["skipped"] = skipped

    # 2. voice
    if early:
        early.join()   # the project and its first downloads are ready (or failed quietly; resolve_voice redoes them)
    control = Control()

    def service():
        while not S.GPU_QUEUE.empty():
            try:
                other, fn = S.GPU_QUEUE.get_nowait()
            except Exception:
                return
            S.run_job(other, fn)
    control.service = service
    v = resolve_voice(S, job, person, prefer_train, control, push, plan)

    # 3. settings + cover
    vhz, speech = voice_f0(v)
    rng = (v.extra or {}).get("f0_range")
    song_f0 = _LAST_F0.get(str(stems["vocals"]))
    if rng and song_f0 is not None:   # voices trained here: octave from the range the voice can actually render
        octave, outside = pick_octave_range(rng, speech, song_f0)
        why = (f"{outside:.0%} of the melody outside the voice's trained range "
               f"({rng[0]:.0f}-{rng[2]:.0f} Hz{' speech' if speech else ''}) at this octave")
    else:
        if not vhz and plan.get("person_hz"):   # community voice: use the real person's measured speaking pitch
            vhz, speech = plan["person_hz"], plan.get("person_speech", True)
        octave = pick_octave(vhz, speech, song_hz)
        why = (f"song melody ~{song_hz:.0f} Hz, voice ~{vhz:.0f} Hz" if vhz and song_hz
               else "voice range unknown, kept the singer's octave")
    plan["settings"] = {"octave": octave, "song_pitch_hz": song_hz, "voice_pitch_hz": vhz,
                        "why": why}
    # Stacked backing harmonies don't survive RVC (it follows one pitch): keep the original singers there, a bit
    # quieter, and convert only the lead (review B5).
    chordal = chordal_backing(stems.get("backing"), S.ENGINE.converter.rmvpe)
    plan["settings"]["backing"] = "original (stacked harmonies)" if chordal else "converted"
    opts = CoverOptions(voice=v.name, key=0, octave=octave, quality="high", dereverb=True, karaoke=True,
                        backing_voice=None if chordal else v.name, backing_octave=octave,
                        backing_db=-3.0 if chordal else None, polish=True, pitch_cleanup=True)
    stage_w = {"separating": (0.0, 0.1), "converting": (0.1, 0.85), "mixing": (0.85, 1.0)}

    def cprog(stage, frac, msg):
        a, b = stage_w.get(stage, (0.95, 1.0))
        push("cover", a + (b - a) * frac, msg or stage.capitalize())
    push("cover", 0.0, f"Making the cover with “{v.name}”")
    res = S.ENGINE.make_cover(song, opts, progress=cprog, title=meta["title"])
    item = next((h for h in S.history_items() if h["output_name"] == res.output.name), None)
    job.finish("done", message="Finished", result=item, plan=plan, seconds=res.seconds, notes=res.notes,
               elapsed=round(time.perf_counter() - t0, 1))
