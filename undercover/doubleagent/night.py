"""DoubleAgent's overnight queue items: "improve a voice" and "train a new voice" (the queue itself is core.worklist).

Improving a voice adds singing (the person's album tracks first) or more speech, re-checks every clip, retrains from
scratch, and replaces the installed voice only if the new one measures better on the same likeness test. Also the
pickers for the queue: candidates (voices that can be improved) and lookup (names for a new voice).
"""
import json

from ..core.config import CONFIG, path
from ..core.voices import ModelError, slug
from ..core import gpu, jobs, worklist

PREP_RT = 3.5          # minutes of source audio prepared per minute (separation + lead split + de-reverb ran 4.3x
                       # realtime on Elliott Smith's album tracks; downloads and phrase cutting take the rest)
EARLY_STOP = 0.7       # auto-length trainings stop on their own at ~70% of the step budget (Elliott: 219 of 296 epochs)
COMPARE_MIN = 3.0      # export + likeness test of old and new voice
SAME_PERSON = 0.35     # the current voice scoring under this against the new clips = they're probably someone else
IMPROVE_SINGING_MIN = 30   # minutes of clean singing an "improve" aims for (more than ~30 rarely helps)


def _sec_per_step() -> float:
    from .project import list_projects
    vals = [p["train"].get("sec_per_step") for p in list_projects() if p["train"].get("sec_per_step")]
    vals = sorted(v for v in vals if 0.05 < v < 3)
    return vals[len(vals) // 2] if vals else 0.35


def estimate(item: dict) -> dict:
    """Minutes per step: prepare (download + separate), train (auto length, typical early stop), compare."""
    from .trainer import LENGTHS
    from . import project as P
    raw_min = 0.0
    proj = _project_for_item(item, create=False)
    if proj is not None:
        raw_min = sum(min(s.duration, CONFIG["training"]["max_minutes_per_source"] * 60) for s in proj.sources
                      if s.selected and s.status != "ready") / 60
        if item["kind"] == "improve":
            raw_min += _improve_extra_minutes(proj)
    else:
        raw_min = 90.0 if item["kind"] == "improve" else 45.0   # not searched yet: typical
    prep = raw_min / PREP_RT
    train = LENGTHS["auto"] * _sec_per_step() * EARLY_STOP / 60
    return {"prepare": round(prep), "train": round(train), "compare": round(COMPARE_MIN if item["kind"] == "improve" else 1),
            "total": round(prep + train + (COMPARE_MIN if item["kind"] == "improve" else 1))}


def _improve_extra_minutes(proj) -> float:
    """Raw minutes an improve would add: album tracks not in the project yet (singers), else a few interviews.
    A project that already has its album tracks only picks up the odd new one."""
    if proj.mode == "speaking":
        return 4 * 10.0
    albums = sum(1 for s in proj.sources if s.note == "album track")
    return 5.0 if albums >= 15 else 90.0


def _project_for_item(item: dict, create: bool):
    from . import project as P
    try:
        if item["kind"] == "project":   # a project queued from the Deep path: exactly that one
            try:
                return P.Project.load(item["target"])
            except ModelError:
                return None
        if item["kind"] == "improve":
            from ..core.voices import get_voice, is_demo
            v = get_voice(item["target"])
            if is_demo(v):
                raise ModelError(f"{v.name} is a demo voice made for UnderCover, not a real person, so there's nobody to find clips of.")
            name = (v.extra or {}).get("project")
            if name:
                try:
                    return P.Project.load(name)
                except ModelError:
                    pass
            person = (v.extra or {}).get("person") or _person_from_voice(v)
            speech = (v.extra or {}).get("from_speech")
        else:
            person, speech = item["target"], None
        found = _project_named(person)
        if found or not create:
            return found
        if speech is None:   # a new voice: singers (YouTube Music) get songs, everyone else speech
            from . import service
            return service.project_for(person)
        # rebuilding a cleaned-up project: same kind of material as the voice was trained on (a speech voice of an
        # actor with narration albums mustn't turn into a singing project), with a full search
        from . import sources as srcmod
        proj = P.Project.create(person, "speaking" if speech else "both")
        try:
            proj.sources = srcmod.search(person, proj.mode)
        except ModelError:
            pass
        proj.save()
        return proj
    except ModelError:
        return None


def _project_named(person: str):
    """The training project for a person, allowing small spelling differences (Elliot / Elliott Smith)."""
    import difflib
    from . import project as P
    key = slug(person)
    words = set(key.split("-"))
    best, score = None, 0.0
    for p in P.list_projects():
        other = slug(p["person"] or p["name"])
        r = difflib.SequenceMatcher(None, key, other).ratio()
        ow = set(other.split("-"))
        if min(len(words), len(ow)) and (words <= ow or ow <= words):   # "Obama" and "Barack Obama"
            r = max(r, 0.95)
        if r > score:
            best, score = p, r
    return P.Project.load(best["name"]) if best and score >= 0.9 else None


def _person_from_voice(v) -> str:
    from ..core.voices import trained_person
    return trained_person(v) or v.name.removesuffix("-uc").replace("-", " ")


def candidates() -> list:
    """Voices for the "Improve my voices" picker: what each was trained on, its last measured likeness, what an
    improve would do and roughly how long it takes."""
    from ..core.voices import list_voices, voice_info
    from . import service
    queued = {(i["kind"], i["target"].lower()) for i in worklist._state()["queue"] if i["state"] in ("queued", "paused", "running")}
    try:
        qcache = json.loads((path("cache_dir") / "voice_quality.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        qcache = {}
    out = []
    for v in list_voices():
        ex = v.extra or {}
        c = {"voice": v.name, **voice_info(v), "minutes": ex.get("train_minutes"),
             "singing": None if ex.get("from_speech") is None else not ex.get("from_speech"), "likeness": None}
        if c["demo"]:   # Agent Smoke: never offered for improving (its "person" is nobody to search for)
            out.append({**c, "can": False, "note": "a demo voice made for UnderCover, not a real person, so there's nobody to find clips of", "cadence": service.cadence_status(v)})
            continue
        if ex.get("trained"):
            person = _person_from_voice(v)
            proj = _project_for_item({"kind": "improve", "target": v.name}, create=False)
            if proj is not None:
                saves = {s["epoch"]: s for s in proj.train.get("saves", [])}
                c["likeness"] = (saves.get(ex.get("epoch")) or {}).get("identity")
            c["likeness"] = c["likeness"] or ex.get("likeness")
            item = {"kind": "improve", "target": v.name}
            c.update(person=person, kind="improve", target=v.name, can=True,
                     note=("more of their singing (album tracks), fresh training, kept only if better" if c["singing"]
                           else "more of their speech, fresh training, kept only if better")
                     + ("" if proj is not None else " · its clips were cleaned up, so they're prepared again"))
        elif ex.get("person"):
            person = ex["person"]
            hit = next((q for q in qcache.values() if q.get("voice") == v.name or q.get("voice") == ex.get("original_name")), None)
            c["likeness"] = (hit or {}).get("score")
            item = {"kind": "new", "target": person}
            c.update(person=person, kind="new", target=person, can=True,
                     note=f"downloaded voice: trains UnderCover's own {person} voice to compare with it")
        else:
            out.append({**c, "can": False, "note": "downloaded voice: UnderCover doesn't know whose voice it is",
                        "cadence": service.cadence_status(v)})
            continue
        c["estimate"] = estimate(item)["total"]
        c["cadence"] = service.cadence_status(v)
        c["queued"] = (item["kind"], item["target"].lower()) in queued
        out.append(c)
    out.sort(key=lambda c: (not c["can"], not c["trained"], c["voice"]))
    return out


def lookup(q: str) -> list:
    """Name suggestions for "Train a new voice": artists on YouTube Music (singers: their album tracks are used),
    plus the name as typed (a speaking voice)."""
    import re
    q = (q or "").strip()
    if len(q) < 2:
        return []
    out, seen = [], set()
    try:
        from ..core.youtube import _music_tracks
        words = [w for w in re.findall(r"\w+", q.lower()) if len(w) > 1]
        for r in _music_tracks(q, n=6):
            artist = r.channel.removesuffix(" - Topic").strip()   # YouTube Music: the artist (sometimes "Artist - Topic")
            if not artist:
                continue
            key = artist.lower()
            if key in seen or not any(w in key for w in words):
                continue
            seen.add(key)
            out.append({"name": artist, "kind": "artist", "detail": f"on YouTube Music · e.g. {r.title.split(' - ', 1)[-1][:40]}"})
    except Exception:   # noqa: BLE001 - suggestions are a nicety
        pass
    if not any(o["name"].lower() == q.lower() for o in out):
        out.append({"name": q, "kind": "person", "detail": "as typed · interviews and speeches (or songs, if they sing)"})
    return out[:6]


def _new(job, item) -> dict:
    from . import service
    from . import voicecheck
    from ..core.speaker import SpeakerEmbedder
    known = _project_named(item["target"])   # reuse what's already prepared for this person
    known = known.name if known is not None and not known.train.get("exported") else None
    name = service.train_new_voice(job, item["target"], worklist.control_for(job), worklist.pusher(job), project=known,
                                     target_minutes=IMPROVE_SINGING_MIN, mode=item.get("mode"))
    proj = _project_for_item({"kind": "improve", "target": name}, create=False)
    score = None
    if proj is not None:
        try:
            score = voicecheck.check(name, proj, gpu.MODELS.converter, SpeakerEmbedder()).score
        except Exception:   # noqa: BLE001 - only for the report
            pass
    return {"voice": name, "after": score, "note": f"'{name}' added to your voices."}


def _improve(job, item) -> dict:
    """More material, re-checked clips, a fresh training, and the new voice only if it measures better."""
    from . import voicecheck
    from ..core.voices import get_voice, remove_voice
    from . import project as P
    from . import sources as srcmod
    from ..core.speaker import SpeakerEmbedder
    from .trainer import Trainer, export_voice
    push, control = worklist.pusher(job), worklist.control_for(job)
    old = get_voice(item["target"])
    proj = _project_for_item(item, create=True)
    if proj is None:
        raise ModelError(f"Couldn't find or create a training project for {item['target']}.")
    cand = f"{old.name}-next"
    st = proj.train
    resuming = st.get("state") == "paused" and st.get("voice_name") == cand
    if not resuming:
        # 1. more material: singers get their album tracks, speakers a few more interviews
        push("night", 0.02, f"Looking for more of {proj.person}")
        with P.lock(proj.name):
            proj = P.Project.load(proj.name)
            have = {s.id for s in proj.sources}
            if proj.mode != "speaking":
                new = [a for a in srcmod.album_tracks(proj.person) if a.id not in have and 60 < a.duration < 600]
            else:
                try:
                    new = [s for s in srcmod.search(proj.person, "speaking") if s.id not in have and s.selected][:4]
                except ModelError:
                    new = []
            proj.sources += new
            if proj.mode != "speaking":   # all their album tracks, up to the singing goal
                budget = IMPROVE_SINGING_MIN * 60 * 3
                for s in proj.sources:
                    if s.note == "album track" and s.status == "found":
                        s.selected = budget > 0
                        budget -= min(s.duration, CONFIG["training"]["max_minutes_per_source"] * 60) if s.selected else 0
            proj.save()
        todo = [s.id for s in proj.sources if s.selected and s.status not in ("ready", "failed")]
        if todo:
            def prog(f, label, **kw):
                push("night", 0.05 + 0.35 * f, f"Preparing clips: {label}" + (f" ({kw['index']}/{kw['count']})" if kw.get("count") else ""))
            try:
                P.prepare_sources(proj.name, todo, gpu.MODELS.separator, SpeakerEmbedder(), progress=prog,
                                  between=control.service, cancel=lambda: job.cancel or job.pausing,
                                  stop=lambda: job.cancel or getattr(control, "stop", False),
                                  friendly=lambda e: jobs.friendly(e, "preparing a source"))
            except P.Cancelled:
                from ..core.jobs import Paused
                raise Paused() if job.pausing else jobs.Cancelled()
        with P.lock(proj.name):
            proj = P.Project.load(proj.name)
            if proj.speakers and not proj.reference.get("confirmed"):
                P.set_reference(proj, cluster=proj.reference["cluster"], auto=True)
            else:
                P.score(proj)
            proj.save()
            st = proj.train
            for k in ("epoch", "best", "saves", "state", "outcome", "exported", "exported_epoch", "epochs"):
                st.pop(k, None)
            st.update(sr=st.get("sr") or CONFIG["training"]["sample_rate"], length="auto", voice_name=cand)
            proj.save()
        (proj.dir / "train" / "resume.pt").unlink(missing_ok=True)
    # 2. train
    conv = gpu.MODELS.converter
    proj = P.Project.load(proj.name)
    push("night", 0.4, f"Training on {proj.kept_seconds() / 60:.1f} min of {proj.person}")

    def tprog(**ev):
        if ev.get("stage") == "training" and ev.get("epochs"):
            job.push(stage="training", frac=round(0.4 + 0.55 * ev.get("fraction", 0), 3), epoch=ev.get("epoch"),
                     epochs=ev["epochs"], eta=ev.get("eta"),
                     message=f"Training: epoch {ev.get('epoch', 0)} of {ev['epochs']}")
    st = Trainer(proj, conv.hubert, conv.rmvpe, device=str(conv.device), half=conv.half, converter=conv).run(control, progress=tprog)
    if st.get("state") == "paused":
        from ..core.jobs import Paused
        raise Paused()
    # 3. compare on the same test, keep the better one
    push("night", 0.96, "Comparing the new voice with the old one")
    proj = P.Project.load(proj.name)
    export_voice(proj, name=cand, overwrite=True)
    emb = SpeakerEmbedder()
    tests = voicecheck.test_phrases(proj)
    try:
        before = voicecheck.check(old.name, proj, conv, emb, tests=tests).score
    except ValueError:
        before = None
    after = voicecheck.check(cand, proj, conv, emb, tests=tests).score
    kept_min = round(proj.kept_seconds() / 60, 1)
    if before is not None and before < SAME_PERSON:
        # the current voice doesn't sound like the clips found now: they're probably someone else's (a debate
        # partner, an interviewer). Don't replace a working voice with that.
        remove_voice(cand)
        return {"voice": old.name, "before": before, "after": after, "replaced": False, "minutes_of_voice": kept_min,
                "note": f"The clips found this time don't sound like the current voice (it scores {before} against them), "
                        "so they may be someone else's. Kept the old voice; check the project in DoubleAgent."}
    if before is None or after > before + 0.005:
        proj = P.Project.load(proj.name)
        export_voice(proj, name=old.name, overwrite=True)
        remove_voice(cand)
        gpu.forget_voice(old.name)
        return {"voice": old.name, "before": before, "after": after, "replaced": True, "minutes_of_voice": kept_min,
                "note": f"Retrained on {kept_min} min of {'singing' if proj.mode != 'speaking' else 'speech'}; the new one sounds more like them."}
    remove_voice(cand)
    with P.lock(proj.name):
        proj = P.Project.load(proj.name)
        proj.train["exported"] = old.name   # the installed voice is still the old one
        proj.save()
    return {"voice": old.name, "before": before, "after": after, "replaced": False, "minutes_of_voice": kept_min,
            "note": f"Retrained on {kept_min} min, but it didn't sound more like them, so the old voice stays."}


def _improve_target(target: str) -> str:
    from ..core.voices import get_voice, is_demo
    v = get_voice(target)   # friendly error if it doesn't exist
    if is_demo(v):
        raise ModelError(f"{v.name} is a demo voice made for UnderCover, not a real person, so there's nobody to find clips "
                         "of to improve it.")
    return v.name


def shown_voice(item: dict) -> str:
    """A queue item whose target is a voice id, as people read it (cadence items use this too)."""
    from ..core.voices import label_of
    return label_of(item["target"])


def _project(job, item) -> dict:
    """A queued project: its own sources and choices (service.run_project)."""
    from . import service
    from . import voicecheck
    from ..core.speaker import SpeakerEmbedder
    name = service.run_project(job, item["target"], worklist.control_for(job), worklist.pusher(job))
    proj = _project_for_item(item, create=False)
    score = None
    if proj is not None:
        try:
            score = voicecheck.check(name, proj, gpu.MODELS.converter, SpeakerEmbedder()).score
        except Exception:   # noqa: BLE001 - only for the report
            pass
    return {"voice": name, "after": score, "note": f"'{name}' added to your voices, from your choices in DoubleAgent."}


def _project_target(target: str) -> str:
    from . import project as P
    proj = P.Project.load(target)   # friendly error if it isn't a project
    if not any(s.selected for s in proj.sources) and not proj.phrases:
        raise ModelError("Tick the sources to train on in DoubleAgent first.")
    return proj.name


def _project_options(target: str, b: dict):
    """The choices on screen when the project was queued (voice name, length, sample rate), checked and saved onto the
    project's training settings, which is what run_project reads: what's on screen is what runs. Nothing given keeps
    the project's own (or the defaults). Another person's voice name is refused now, not at 3 am."""
    from . import project as P
    from .api import check_voice_name
    from .trainer import LENGTHS, SR_CFG
    from ..core.voices import slug
    with P.lock(target):
        proj = P.Project.load(target)
        st = proj.train
        vname = slug(b.get("voice_name") or st.get("voice_name") or f"{proj.name}-uc")
        if not vname:
            raise ModelError("Give the new voice a name.")
        check_voice_name(proj, vname)
        length = b.get("length") or st.get("length") or "auto"
        if length not in LENGTHS:
            raise ModelError(f"Pick a training length: {', '.join(LENGTHS)}.")
        sr = int(b.get("sr") or st.get("sr") or CONFIG["training"]["sample_rate"])
        if sr not in SR_CFG:
            raise ModelError("Sample rate must be 32000, 40000 or 48000.")
        if st.get("state") == "paused" and st.get("sr") and int(st["sr"]) != sr:
            raise ModelError(f"This training is paused at {int(st['sr']) // 1000} kHz and continues at that rate. "
                             "Keep it, or start over in DoubleAgent.")
        st.update(voice_name=vname, length=length, sr=sr)
        proj.save()


def shown_project(item: dict) -> str:
    from . import project as P
    try:
        return P.Project.load(item["target"]).person or item["target"]
    except ModelError:
        return item["target"]


worklist.register_kind("project", run=_project, estimate=estimate, target=_project_target, shown=shown_project,
                       label=lambda item: f"Training {shown_project(item)} (your choices)", options=_project_options)
worklist.register_kind("improve", run=_improve, estimate=estimate, target=_improve_target, shown=shown_voice,
                       label=lambda item: f"Improving {shown_voice(item)}")
worklist.register_kind("new", run=_new, estimate=estimate, label=lambda item: f"Training a voice of {item['target']}",
                       modes=("speaking", "singing", "both"))
