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
import re
import time
from pathlib import Path

import numpy as np

from ..core import gpu, jobs, worklist
from ..core.config import path
from ..core.voices import ModelError
from . import ambiguity
from .fit import pick_fit, pick_octave
from ..doubleagent.service import _prefetch_voice_sources, installed_for, resolve_voice, voice_f0, voice_quality

PATTERNS = [
    # "cover of X by Y" / "X cover by Y" / "make X sung by Y" (the LAST "by" wins: "X by Miley Cyrus by Y")
    r"^(?:please\s+)?(?:make|do|create|give me|generate)?\s*(?:me\s+)?(?:an?\s+)?(?:ai\s+)?cover\s+of\s+(?P<song>.+)\s+(?P<sep>by|in the voice of|as sung by|sung by|with the voice of|with|using|as)\s+(?P<person>[^,;]+?)$",
    r"^(?P<song>.+?)\s+cover\s+(?:by|in the voice of|with|using|as)\s+(?P<person>[^,;]+?)$",
    r"^(?P<person>.+?)\s+(?:sings|singing|covers|covering|performs|doing)\s+(?P<song>.+)$",
    r"^(?:make|do|have)?\s*(?P<person>.+?)\s+(?:sing|cover)\s+(?P<song>.+)$",
    r"^(?P<song>.+?)\s+(?:sung by|as sung by|in the voice of|but (?:it's|its|it is|sung by))\s+(?P<person>[^,;]+?)$",
    r"^(?P<song>.+)\s+(?P<sep>by)\s+(?P<person>[^,;]+?)$",
]

# The forms that name the song's artist and/or mark the voice with "as" (checked before PATTERNS):
#   "Hello by Adele as Peter Griffin"   "cover of Hello by Adele as Peter Griffin"   "Hello as Peter Griffin"
_COVER_OF = r"(?:(?:please\s+)?(?:(?:make|do|create|give me|generate)\s+)?(?:me\s+)?(?:an?\s+)?(?:ai\s+)?cover\s+of\s+)?"
_NOT_SINGING = r"(?!.*\b(?:sings|singing|covers|covering|performs|doing)\b)"   # "Obama sings Hello as ..." is another form
# "Stand By Me as Peter Griffin": "by Me" is part of the title, not an artist called Me
_PRONOUNS = r"(?:me|you|your|him|her|us|them|my|our|it|this|that|the|a|an)"
_NAME = rf"(?!{_PRONOUNS}\b)"
AS_PATTERNS = [
    rf"^{_COVER_OF}{_NOT_SINGING}(?P<song>.+?)\s+by\s+{_NAME}(?P<artist>.+?)\s+as\s+(?P<person>[^,;]+?)$",
    rf"^{_COVER_OF}{_NOT_SINGING}(?P<song>.+?)\s+as\s+(?P<person>[^,;]+?)$",
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
        m = re.search(rf"\s+(?:but|in an?|(?-i:as an?)|in)\s+(?P<style>{_STYLE_WORDS})(?:\s+(?:version|style|cover))?$", t, re.I)
        if m and not re.search(r"\b(?:sung|sings|sing|by|voice)\b", m.group("style"), re.I) and _clean_style(m.group("style")):
            style = _clean_style(m.group("style"))   # "X sung by Y but bluegrass"
            t = t[:m.start()]
    song, person, artist, by_only = parse_full(t)
    m = re.search(rf"\s*[\(\[]\s*(?P<style>{_STYLE_WORDS})\s+(?:version|cover)\s*[\)\]]$", song, re.I)
    if m and not style and _clean_style(m.group("style")):   # "Plug In Baby (acoustic version) by ..."
        style = _clean_style(m.group("style"))
        song = song[:m.start()]
    whole = None   # the title with its "by" as typed, for when the "artist" turns out to be part of it (ambiguity.settle)
    if artist and (w := re.search(re.escape(song.strip()) + r"\s+by\s+" + re.escape(artist), text, re.I)):
        whole = re.sub(r"\s+", " ", w.group(0))
    return {"song": song.strip(), "person": person, "style": style, "artist": artist, "by_only": by_only, "whole": whole}


def parse(text: str) -> tuple[str, str]:
    """-> (song query, person). Raises ModelError with an example when it can't tell them apart."""
    song, person, _artist, _by_only = parse_full(text)
    return song, person


def _split_artist(song: str) -> tuple[str, str | None]:
    """The artist written into the song's words: "Hello (by Adele)", "Hello by Adele" -> ("Hello", "Adele")."""
    m = re.search(r"\s*[\(\[]\s*by\s+([^\)\]]+)[\)\]]", song, re.I)
    if m:
        return (song[:m.start()] + song[m.end():]).strip(), m.group(1).strip()
    m = re.match(rf"^(.+?)\s+by\s+{_NAME}(.+)$", song, re.I)   # "Stand By Me" keeps its "by"
    return (m.group(1).strip(), m.group(2).strip()) if m else (song.strip(), None)


def parse_full(text: str) -> tuple[str, str, str | None, bool]:
    """-> (song, person, artist, by_only). `artist` is named only by "<song> by <artist> as <person>". `by_only` is
    True for "<song> by <name>" with no "as": the name is read as the voice (as it always was) but it could be the
    song's artist, which the plan step says out loud. Raises ModelError with an example when it can't tell the song
    and the voice apart."""
    t = re.sub(r"\s+", " ", text.strip().strip(".!?")).strip()
    for n, pat in enumerate(AS_PATTERNS + PATTERNS):
        m = re.match(pat, t, re.I)
        if m:
            g = m.groupdict()
            artist = (g.get("artist") or "").strip(" \"'“”‘’") or None
            song = m.group("song").strip(" \"'“”‘’")
            if n < len(AS_PATTERNS):   # "Hello cover as Peter Griffin" is still the song "Hello"
                song = re.sub(r"\s+cover$", "", song, flags=re.I)
            person = re.sub(r"^(?:the voice of|voice of)\s+", "", m.group("person").strip(" \"'“”‘’"), flags=re.I)
            person = re.sub(r"'s voice$|\s+voice$", "", person, flags=re.I)
            person = re.sub(r"^(?:sung by|as sung by|by)\s+", "", person, flags=re.I).strip()
            song = re.sub(r"\s+(?:as\s+sung|sung|as|but|in the voice of)$", "", song, flags=re.I)   # "X as sung by Y"
            if not artist:
                song, artist = _split_artist(song)
            if song and person:
                return song, person, artist, bool(not artist and (g.get("sep") or "").lower() == "by")
    raise ModelError('Say the song and the voice, for example: "Hello by Adele as Peter Griffin" or '
                     '"cover of Party In The USA by Morgan Freeman".')


# ---------------------------------------------------------------- voice resolution


# ---------------------------------------------------------------- measured quality (voicecheck.py)


def song_vocal_f0(vocals: Path, rmvpe) -> float | None:
    return vocal_stats(vocals, rmvpe)[0]


def vocal_stats(vocals: Path, rmvpe) -> tuple[float | None, float]:
    """(median sung pitch Hz, share of the song with a voiced melody). Used to pick the octave, and to notice
    when a "cover" turns out to be instrumental (an acoustic guitar cover with no singing, say)."""
    from ..core.engine.convert import load_16k
    f0 = rmvpe.infer_from_audio(load_16k(vocals), thred=0.03)
    voiced = f0[f0 > 0]
    share = float(len(voiced)) / max(1, len(f0))
    _LAST_F0[str(vocals)] = voiced   # the melody's pitch track, reused to pick the octave
    _LAST_F0[str(vocals) + "#all"] = f0   # frame-aligned, for vocal_cleanliness
    return (round(float(np.median(voiced)), 1) if len(voiced) > 50 else None), round(share, 3)


_LAST_F0: dict = {}


def _drop_song(sid: str):
    """Remove a song this one-shot downloaded only to compare versions."""
    try:
        from .songs import remove_song
        remove_song(sid)
    except Exception:
        pass


def vocal_cleanliness(vocals: Path) -> dict:
    """How clean a separated lead vocal is: `bleed` = level (dB, vs the singing) of the stem where nobody sings
    (guitar or piano leaking through), `hiss` = spectral flatness of the sung parts (doubleagent.service.noisiness)."""
    from ..core.engine.convert import load_16k
    from ..doubleagent.service import noisiness
    x = load_16k(vocals)
    f0 = _LAST_F0.get(str(vocals) + "#all")
    fr = x[: len(x) // 160 * 160].reshape(-1, 160)
    e = 10 * np.log10((fr ** 2).mean(1) + 1e-12)
    m = min(len(e), len(f0)) if f0 is not None else 0
    if m < 100:
        return {"bleed": -99.0, "hiss": noisiness(x, 16000)}
    e, sung = e[:m], f0[:m] > 0
    quiet = e[~sung]
    bleed = float(np.percentile(quiet, 75) - np.median(e[sung])) if len(quiet) > 50 and sung.any() else -99.0
    return {"bleed": round(bleed, 1), "hiss": noisiness(x, 16000)}


def version_penalty(q: dict | None) -> float:
    """Extra cost for a messy separation, on the same scale as pick_fit's cost (share of notes out of range)."""
    if not q:
        return 0.0
    return BLEED_W * max(0.0, q["bleed"] - BLEED_OK) + HISS_W * max(0.0, q["hiss"] - HISS_OK)


BLEED_OK, BLEED_W = -40.0, 0.01   # each dB of bleed above -40 dB costs like 1% of the melody out of range
HISS_OK, HISS_W = 0.22, 1.0       # each 0.01 of flatness above a clean voice's ~0.2 costs like 1%
VERSIONS = 3                      # style versions ("acoustic") compared before picking one


def backing_monophony(backing: Path, rmvpe, seconds: int = 120) -> tuple[float, float]:
    """(share of the backing stem's energy explained by ONE pitch line, share of its loud frames that are pitched).
    RVC follows a single pitch, so stacked harmonies come out garbled when converted. Measured 2026-10-09:
    single lines 77-89% (acoustic cover, "Find Your Love"); stacked harmonies ~45% while 87-97% pitched
    (Bohemian Rhapsody, "when the party's over"); rap ad-libs score low because they're mostly unpitched."""
    from ..core.engine.convert import load_16k
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


# ---------------------------------------------------------------- the job

def run(job, text: str, prefer_train: bool = False, song_url: str | None = None, choice: str | None = None):
    from . import songs
    from .pipeline import ENGINE, CoverOptions, history_items
    from ..core.separate import separate
    from ..core.jobs import Control
    t0 = time.perf_counter()
    plan: dict = {"request": text}

    def push(stage, frac, message, **kw):
        if job.cancel:
            raise jobs.Cancelled()
        job.push(stage=stage, frac=round(frac, 3), message=message, plan=plan,
                 elapsed=round(time.perf_counter() - t0, 1), **kw)

    req = ambiguity.settle(parse_request(text))
    title_q, person, style, artist = req["song"], req["person"], req["style"], req["artist"]
    search_q = f"{title_q} {artist}" if artist else title_q
    # An ambiguous bare title ("Hello"): the answer the user gave, else the first choice, said out loud (never silent)
    if not song_url and not artist:
        pick, went = ambiguity.resolve(title_q, choice)
        if pick and pick["artist"] and pick["title"]:
            title_q, artist = pick["title"], pick["artist"]
            if style or not pick.get("track", True):   # a version of that song, or only its video was found:
                search_q = f"{title_q} {artist}"            # search for it by its artist (the album track wins)
            else:
                song_url = pick["song_url"]
        elif pick:
            song_url = pick["song_url"]
        if went:
            plan["went_with"] = went
            job.title = f"One-shot: {text} · {went}"
    song_q = f"{title_q} by {artist}" if artist else title_q   # what people read; the search uses search_q
    plan.update(song_query=song_q, person=person, style=style, artist=artist)

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
    push("song", 0.0, (plan["went_with"] + ". " if plan.get("went_with") else "") +
         (f"Finding the {what} on YouTube" if style else f"Finding {what} on YouTube"))
    cands = [songs.from_link(song_url)] if song_url else songs.search(search_q, style=style, words=style_words(style))[:VERSIONS + 2]
    # A version ("acoustic") is someone's cover, and they differ a lot: compare up to VERSIONS that have singing
    # and pick the one that suits the voice once it's known (melody range, clean separation). The album track
    # (no style) or a pasted link: the first one with singing.
    compare = bool(style) and not song_url
    per = VERSIONS if compare else 1
    skipped, versions = [], []
    ahead: dict = {}   # comparing versions: the next one downloads while the GPU separates this one

    def fetch_ahead(pk):
        import threading
        box = {}

        def go():
            try:
                box["had"] = songs.known_song(pk.id)
                box["meta"] = songs.download(pk, songs.register_song)
            except Exception as e:   # noqa: BLE001 - re-raised on the main thread
                box["err"] = e
        t = threading.Thread(target=go, daemon=True, name="song-ahead")
        t.start()
        ahead[pk.id] = (t, box)

    for ci, pick in enumerate(cands):
        n = len(versions)
        label = f"version {n + 1} of up to {VERSIONS}: " if compare else ""
        push("song", 0.02 + 0.9 * n / per, f"Downloading {label}“{pick.title}”")
        try:
            if pick.id in ahead:
                t, box = ahead.pop(pick.id)
                t.join()
                if "err" in box:
                    raise box["err"]
                had, meta = box["had"], box["meta"]
            else:
                had = songs.known_song(pick.id)
                meta = songs.download(pick, songs.register_song, progress=lambda f, pick=pick, label=label, n=n: push(
                    "song", 0.02 + 0.9 * (n + 0.4 * f) / per, f"Downloading {label}“{pick.title}”"))
            if compare and ci + 1 < len(cands) and n + 1 < per:
                fetch_ahead(cands[ci + 1])
        except ModelError:
            if pick is cands[-1] and not versions:
                raise
            skipped.append(pick.title)   # unavailable (removed, region-locked): try the next candidate
            continue
        song = Path(path("uploads_dir")) / meta["file"]
        # separate now (cached for the cover): catches a bad download early and gives the melody's pitch
        stems = separate(song, dereverb=True, karaoke=True, separator=gpu.MODELS.separator, quality="high",
                         progress=lambda f, name, pick=pick, label=label, n=n: push(
                             "song", 0.02 + 0.9 * (n + 0.4 + 0.6 * f) / per, f"Separating the vocals of {label}“{pick.title}”"))
        song_hz, sung = vocal_stats(stems["vocals"], gpu.MODELS.converter.rmvpe)
        if sung < MIN_SUNG:
            skipped.append(pick.title)
            if not had and compare:
                _drop_song(meta["id"])
            push("song", 0.1, f"“{pick.title}” has hardly any singing (instrumental?), trying the next one")
            continue
        versions.append({"pick": pick, "meta": meta, "song": song, "stems": stems, "hz": song_hz, "sung": sung,
                         "new": not had, "clean": vocal_cleanliness(stems["vocals"]) if compare else None})
        if len(versions) >= per:
            break
    if not versions:
        raise ModelError(f"Couldn't find a version of “{song_q}” with singing in it"
                         + (f" (tried {len(cands)})" if len(cands) > 1 else "") + ". Paste a link to one you like.")

    def use(x):
        pk = x["pick"]
        plan["song"] = {"title": pk.title, "channel": pk.channel, "url": pk.url, "note": pk.note, "style": style,
                        "id": x["meta"]["id"], "sung": x["sung"]}
        if skipped:
            plan["song"]["skipped"] = skipped
        return x["meta"], x["song"], x["stems"], x["hz"]
    meta, song, stems, song_hz = use(versions[0])

    # 2. voice
    if early:
        early.join()   # the project and its first downloads are ready (or failed quietly; resolve_voice redoes them)
    control = Control()
    job.control = control   # Pause (top of the page) reaches a one-shot's training through this

    def service():
        while not jobs.GPU_QUEUE.empty():
            try:
                other, fn = jobs.GPU_QUEUE.get_nowait()
            except Exception:
                return
            jobs.run_job(other, fn)
    control.service = service
    try:
        v = resolve_voice(job, person, prefer_train, control, push, plan)
    except jobs.Paused:
        # Resume continues this one-shot, not a new one: the same song, and straight back to its voice's training
        # (the project keeps its clips and its training's resume point) when that had started
        spec = job.resume_spec or {}
        spec["song_url"] = (plan.get("song") or {}).get("url") or spec.get("song_url")
        if (plan.get("voice") or {}).get("how") == "train":
            spec["prefer_train"] = True
        raise

    # the gate (phase 15): a weak voice is said plainly, here and with the cover, never handed over silently
    q = voice_quality(v)
    pv = plan.get("voice")
    if pv is not None:
        if q["ok"] and pv.get("quality"):   # a quick cover's honest label from resolve_voice (a voice from elsewhere)
            q = pv["quality"]
        pv["quality"] = q
    if not q["ok"] and not q.get("quick"):   # (a quick cover's label was said already)
        push("voice", 1.0, f"Heads-up: {q['summary']}")

    # 3. settings + cover
    vhz, speech = voice_f0(v)
    rng = (v.extra or {}).get("f0_range")
    if len(versions) > 1:   # pick the version that suits this voice: fits its range, separates cleanly
        r = rng or plan.get("person_range")
        sp = speech if rng else plan.get("person_speech", True)
        for x in versions:
            f0 = _LAST_F0.get(str(x["stems"]["vocals"]))
            x["fit"] = pick_fit(r, sp, f0) if r and f0 is not None else None
            x["cost"] = (x["fit"]["cost"] if x["fit"] else 0.0) + version_penalty(x["clean"])
        best = min(versions, key=lambda x: x["cost"])
        meta, song, stems, song_hz = use(best)
        # the other versions stay in the library (one click to try them; taste beats the numbers: the user preferred
        # a version ranked second). Unused ones go with the Library's "songs never used" clean-up after 30 days.
        plan["song"]["compared"] = [{"title": x["pick"].title, "url": x["pick"].url, "id": x["meta"]["id"],
                                     "chosen": x is best, "outside": x["fit"]["outside"] if x["fit"] else None,
                                     **x["clean"]} for x in versions]
    song_f0 = _LAST_F0.get(str(stems["vocals"]))
    if not rng and plan.get("person_range"):   # community voice: the real person's range, from their kept clips
        rng, speech = plan["person_range"], plan.get("person_speech", True)
    key = 0
    if rng and song_f0 is not None:   # fit the melody into the range the voice can actually render
        fit = pick_fit(rng, speech, song_f0)
        octave, key = fit["octave"], fit["key"]
        why = (f"{fit['outside']:.0%} of the melody outside the voice's range "
               f"({rng[0]:.0f}-{rng[2]:.0f} Hz{', from speech' if speech else ''})")
        plan["settings_fit"] = fit
    else:
        if not vhz and plan.get("person_hz"):   # community voice: use the real person's measured speaking pitch
            vhz, speech = plan["person_hz"], plan.get("person_speech", True)
        octave = pick_octave(vhz, speech, song_hz)
        why = (f"song melody ~{song_hz:.0f} Hz, voice ~{vhz:.0f} Hz" if vhz and song_hz
               else "voice range unknown, kept the singer's octave")
    plan["settings"] = {"octave": octave, "key": key, "song_pitch_hz": song_hz, "voice_pitch_hz": vhz,
                        "why": why}
    # Stacked backing harmonies don't survive RVC (it follows one pitch): keep the original singers there, a bit
    # quieter, and convert only the lead (review B5).
    chordal = chordal_backing(stems.get("backing"), gpu.MODELS.converter.rmvpe)
    plan["settings"]["backing"] = "original (stacked harmonies)" if chordal else "converted"
    opts = CoverOptions(voice=v.name, key=key, octave=octave, quality="high", dereverb=True, karaoke=True,
                        backing_voice=None if chordal else v.name, backing_octave=octave,
                        backing_db=-3.0 if chordal else None, polish=True, pitch_cleanup=True)
    stage_w = {"separating": (0.0, 0.1), "converting": (0.1, 0.85), "mixing": (0.85, 1.0)}

    def cprog(stage, frac, msg):
        a, b = stage_w.get(stage, (0.95, 1.0))
        push("cover", a + (b - a) * frac, msg or stage.capitalize())
    push("cover", 0.0, f"Making the cover with “{v.name}”")
    res = ENGINE.make_cover(song, opts, progress=cprog, title=meta["title"])
    item = next((h for h in history_items() if h["id"] == res.id), None)
    job.finish("done", message="Finished" if q["ok"] else f"Finished, with a weak voice: {q['summary']}", result=item,
               plan=plan, seconds=res.seconds, notes=res.notes, elapsed=round(time.perf_counter() - t0, 1), quality=q)


def start_oneshot(body: dict):
    """Queue a one-shot cover (from the page, or Resume after a pause)."""
    from .pipeline import cancel_presep
    text = (body.get("text") or "").strip()
    req = ambiguity.settle(parse_request(text))   # friendly error now rather than inside the job
    choice = (body.get("choice") or "").strip() or None
    key = ambiguity.song_key(req["song"], req["artist"])
    if req["artist"] is None and choice:
        ambiguity.remember(key, choice, ambiguity.cached_choices(req["song"]))   # until a different song is requested
    elif req["artist"] is None and (known := ambiguity.recall(key)):
        choice = known["chosen"]["song_url"]   # the same song again: the answer given stays in effect
    else:
        ambiguity.requested(key)
    if any(j.kind == "auto" and not j.finished for j in jobs.JOBS.values()):
        raise ModelError("A one-shot cover is already running. Wait for it, or cancel it.")
    cancel_presep()
    job = jobs.Job("auto")
    jobs.JOBS[job.id] = job
    job.resume_spec = {"kind": "oneshot", "text": text, "prefer_train": bool(body.get("prefer_train")),
                       "song_url": body.get("song_url") or None, "choice": choice, "label": f"One-shot: {text}"}
    job.push(stage="queued", frac=0, message="Waiting for the GPU", plan={"request": text})
    jobs.GPU_QUEUE.put((job, lambda job: run(job, text, bool(body.get("prefer_train")), body.get("song_url") or None, choice)))
    return job


# Pause / Resume (core.worklist): a one-shot keeps making its cover while voice work is held, but sets its
# training aside; Resume starts a paused one-shot again
worklist.register_pausable("auto", partly=True)
worklist.register_resume("oneshot", lambda spec: start_oneshot({"text": spec["text"], "prefer_train": spec.get("prefer_train"),
                                                                 "song_url": spec.get("song_url"), "choice": spec.get("choice")}))
