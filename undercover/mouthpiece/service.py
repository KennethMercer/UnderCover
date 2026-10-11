"""MouthPiece's public surface: find, check, make and improve the voice of a person.

Other modules (FrontMan's one-shot, SpeakEasy) use voices through this file only:
  installed_for / online_for   voices for a person (installed: trained here first; online community models)
  resolve_voice                a good voice of a person: use one trained here, else community voices that pass a
                               measured check against real clips (voicecheck.py; results remembered), else train one
  train_new_voice              find clips -> prepare -> auto-confirm the main voice -> train -> add to the voices
  project_for / is_musician    the person's training project (singers get their songs, not just interviews)
  voice_f0, undertrained       facts about a voice
"""
import re
import time

from ..core.config import CONFIG
from ..core.voices import ModelError, slug
from ..core import gpu, jobs
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
    from ..core.voices import list_voices
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


def project_for(person: str):
    """The Train-tab project for this person that isn't finished yet (or a new one). Musicians get "both":
    interviews (for the voice check's other speakers) plus isolated vocals from their songs, so the trained
    voice has actually sung."""
    from . import project as P
    key = slug(person)
    proj = next((P.Project.load(p["name"]) for p in P.list_projects()
                 if slug(p["person"]) == key and not p["train"].get("exported")), None)
    if proj is None:
        return P.Project.create(person, "both" if is_musician(person) else "speaking")
    if proj.mode == "speaking" and not proj.train.get("epoch") and is_musician(person):
        # an interviews-only project for a singer: add sources of them singing (nothing trained yet)
        from . import sources as srcmod
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


def reference_clips(job, person: str, control, push):
    """Make sure the person's project has the real voice identified and some phrases by other people (the
    test material): prepares one source at a time, at most two. These are training clips too."""
    from . import project as P
    from . import sources as srcmod
    from ..core.speaker import SpeakerEmbedder
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
            P.prepare_sources(proj.name, todo, gpu.MODELS.separator, emb, cancel=lambda: job.cancel or job.pausing,
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


def _prefetch_voice_sources(person: str):
    """Create/find the person's project, search their sources and download the first two (network only)."""
    from . import sources as srcmod
    try:
        proj = project_for(person)
        if not proj.sources:
            from . import project as P
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


def resolve_voice(job, person: str, prefer_train: bool, control, push, plan: dict):
    """-> installed Voice to use (training one if nothing measured good enough)."""
    from . import voicecheck
    from ..core.voices import download_and_install, get_voice, remove_voice
    from ..core.speaker import SpeakerEmbedder
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
        proj = reference_clips(job, person, control, push)
        conv, emb = gpu.MODELS.converter, SpeakerEmbedder()

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
                    gpu.forget_voice(name)
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
            rng = voicecheck.target_range(proj, conv.rmvpe)
            hz = rng[1] if rng else None
            plan["person_hz"], plan["person_speech"] = (hz, proj.from_speech()) if hz else (good[0].get("target_hz"), True)
            v = get_voice(good[0]["name"])
            if rng:
                plan["person_range"] = rng
                _remember_range(v, rng, plan["person_speech"], person)
            plan["voice"] = {"name": v.name, "how": good[0]["where"] if good[0]["where"] == "online" else "installed",
                             "repo": good[0].get("repo"), "checked": f"sounds like {person}: {good[0]['score']:.2f} (pass {voicecheck.SUNG_GOOD if good[0].get('test') == 'sung' else voicecheck.GOOD}{', tested on singing' if good[0].get('test') == 'sung' else ''})"}
            push("voice", 1.0, f"Using “{v.name}”: it sounds like {person} ({good[0]['score']:.2f})")
            return v
        push("voice", 0.2, "None of the existing voices sounded enough like " + person + ", so UnderCover will train one")
    plan["voice"] = {"name": f"{slug(person)}-uc", "how": "train"}
    push("voice", 0.2 if checked else 0.0, f"Training a voice for {person} (about 45 minutes)")
    name = train_new_voice(job, person, control, push)
    v = get_voice(name)
    plan["voice"]["name"] = v.name
    gpu.forget_voice(v.name)
    return v


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


# ---------------------------------------------------------------- training a new voice

NOT_USABLE = ("may be", "a reaction", "edited", "no vocals", "probably")   # source notes that mean someone else


def train_new_voice(job, person: str, control, push, project: str | None = None,
                    target_minutes: float | None = None) -> str:
    """Find clips -> prepare (topping up sources until the target) -> auto-confirm -> train -> add. Returns voice name.
    project: continue that Train-tab project (its ticked sources, choices and reference are kept)."""
    from . import api as train_api
    from . import project as P
    from . import sources as srcmod
    from ..core.speaker import SpeakerEmbedder
    from .trainer import Trainer, export_voice
    TC = {**CONFIG["training"], **({"target_minutes": target_minutes} if target_minutes else {})}
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
        sung_first = (lambda s: not srcmod.is_sung(s)) if singer else (lambda s: False)
        searched_more = False

        def enough(pr):
            return (pr.sung_seconds() if singer and not songs_exhausted(pr) else pr.kept_seconds()) / 60 >= TC["target_minutes"]

        def songs_exhausted(pr):
            return not [s for s in pr.sources if srcmod.is_sung(s) and s.status == "found"
                        and not s.note.startswith(NOT_USABLE)]
        for rnd in range(12 if singer else 8):
            proj = P.Project.load(proj.name)
            if proj.reference.get("clip") and enough(proj):
                break   # enough clean voice already (e.g. from the quality check's clips)
            # a few sources at a time, re-checking the total in between, instead of everything that's ticked
            todo = [s.id for s in sorted(proj.sources, key=sung_first)
                    if s.selected and s.status not in ("ready", "failed")]
            if singer and not songs_exhausted(proj):
                todo = [i for i in todo if srcmod.is_sung(proj.source(i))]
            todo = todo[:3 if singer else 2]
            if todo:
                def prog(f, label, rnd=rnd, **kw):
                    push("voice", 0.02 + 0.08 * min(1, (rnd + f) / 2),
                         f"Preparing clips: {label}" + (f" ({kw['index']}/{kw['count']})" if kw.get("count") else ""))
                try:
                    P.prepare_sources(proj.name, todo, gpu.MODELS.separator, emb, progress=prog, between=control.service,
                                      cancel=lambda: job.cancel or job.pausing,
                                      friendly=lambda e: jobs.friendly(e, "preparing a source"))
                except P.Cancelled:
                    raise Paused() if job.pausing else jobs.Cancelled()
            with P.lock(proj.name):
                proj = P.Project.load(proj.name)
                if proj.speakers and not proj.reference.get("confirmed"):
                    P.set_reference(proj, cluster=proj.reference["cluster"], auto=True)   # the dominant voice
                    proj.save()
            mins = proj.kept_seconds() / 60
            if enough(proj):
                break
            more = sorted([s for s in proj.sources if not s.selected and not s.note.startswith(NOT_USABLE)
                           and s.status == "found" and s.duration >= (90 if singer and srcmod.is_sung(s) else 120)],
                          key=sung_first)
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
                             "Open MouthPiece to add links or recordings, then try again.")
        push("voice", 0.12, f"Training a voice from {mins:.1f} min of {person}")
        st = proj.train
        if st.get("state") != "paused":
            for k in ("epoch", "best", "saves", "state", "outcome", "exported", "exported_epoch", "epochs"):
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
                     f"Training: epoch {ev.get('epoch', 0)} of {ev['epochs']}", eta=ev.get("eta"), epoch=ev.get("epoch"), epochs=ev["epochs"])
            elif ev.get("stage") == "preparing":
                push("voice", 0.12, f"Training: {ev.get('note', 'getting ready')}")
        st = tr.run(control, progress=tprog)
        if job.cancel:
            raise jobs.Cancelled()
        if st.get("state") == "paused":
            raise Paused()
        push("voice", 0.98, "Adding the new voice")
        v = export_voice(P.Project.load(proj.name), overwrite=True)
        return v.name
    finally:
        train_api.ACTIVE.pop(proj.name, None)


def noisiness(x, sr: int) -> float:
    """How hissy a recording is (spectral flatness of its voiced parts; ~0.2 for a clean voice)."""
    from .project import noisiness as n
    return n(x, sr)
