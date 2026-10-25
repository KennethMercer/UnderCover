"""DoubleAgent's overnight queue items: "improve a voice" and "train a new voice" (the queue itself is core.worklist).

Improving a voice adds singing (the person's album tracks first) or more speech, re-checks every clip, refines or
retrains it (doubleagent/frozen.py), and offers the result beside the current voice only when it measures better on
the person's own lines; the ear decides (doubleagent/improved.py). Also the pickers for the queue: candidates (voices that can be improved) and lookup (names for a new voice).
"""
import json

import numpy as np

from ..core.config import CONFIG, path
from ..core.voices import ModelError, label_of, person_guessed, slug, voice_person
from ..core import gpu, jobs, worklist

PREP_RT = 3.5          # minutes of source audio prepared per minute (separation + lead split + de-reverb ran 4.3x
                       # realtime on Elliott Smith's album tracks; downloads and phrase cutting take the rest)
SAME_PERSON = 0.35     # the current voice scoring under this against the new clips = they're probably someone else
IMPROVE_SINGING_MIN = 30   # minutes of clean singing an "improve" aims for (more than ~30 rarely helps)


SEC_PER_STEP = 0.35   # a training step on a current mid-range card before this PC has measured one (this RX: 0.31)


def _sec_per_step() -> float:
    """Seconds a training step takes on this PC: the median of the projects' measured speeds, else the last trainings'
    (trainer's speed file), else SEC_PER_STEP."""
    from ..core import lowprofile
    from .project import list_projects
    vals = [] if lowprofile.on() else [p["train"].get("sec_per_step") for p in list_projects() if p["train"].get("sec_per_step")]
    if not vals:   # (under Low profile: only speeds measured under it, in the speed file's "-lp" keys)
        try:
            from ..core import lowprofile
            from .trainer import SPEED_FILE
            d = json.loads((path("cache_dir") / SPEED_FILE).read_text(encoding="utf-8"))
            vals = [v for k, v in d.items() if k.endswith("-lp") == lowprofile.on()]   # (this mode's own speeds)
        except (OSError, ValueError, ImportError):
            vals = []
    vals = sorted(v for v in vals if isinstance(v, (int, float)) and 0.05 < v < 3)
    if vals:
        return vals[len(vals) // 2]
    from ..core import lowprofile
    return SEC_PER_STEP * lowprofile.factor()   # (nothing measured under Low profile yet: the burst's share, as a first guess)


# What a new voice's training turned out to be, measured from every one (rc-eta, IA's training proof: "about 61 min"
# planned, 2 h 22 min taken; 2b the other way): the kept minutes it ended with against the target, the kept minutes
# prepared per minute, and how far past its planned epochs it ran before the stall rule stopped it. Defaults: 2d's.
TRAIN_SHAPE = {"kept_over_target": 1.4, "kept_per_prep_min": 3.2, "past_plan": 1.1}   # (2d ran 1.2x its plan, rc-eta's proof 1.0x)
# a character's scenes and a singer's songs yield less clean voice a minute than a person's speeches (SpongeBob: about
# 0.3 min an upload, music and the other characters cut out): their own preparation rate until one is measured
KIND_PREP = {"character": 0.8, "singer": 2.0}


def _shape_file():
    return path("cache_dir") / "train_shape.json"


def train_shape(kind: str = "speaker") -> dict:
    """The medians of the last trainings' shape for this kind of person ("speaker", "singer", "character"; the
    preparation rate is kept per kind, the rest for all), TRAIN_SHAPE / KIND_PREP until one is measured."""
    try:
        d = json.loads(_shape_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        d = {}
    out = {}
    for k, dflt in TRAIN_SHAPE.items():
        key = f"{k}:{kind}" if k == "kept_per_prep_min" else k
        xs = sorted(x for x in d.get(key, []) if isinstance(x, (int, float)) and x > 0)
        out[k] = xs[len(xs) // 2] if xs else (KIND_PREP.get(kind, dflt) if k == "kept_per_prep_min" else dflt)
    return out


def person_kind(proj, person: str | None = None) -> str:
    """ "character", "singer" or "speaker": from the project when there is one, else what the request was read as."""
    if proj is not None:
        from . import sources as srcmod
        return "character" if srcmod.is_character_project(proj) else "singer" if proj.mode != "speaking" else "speaker"
    try:
        from .service import _subjects
        return "character" if (_subjects().get(slug(person or "")) or {}).get("kind") == "character" else "speaker"
    except Exception:   # noqa: BLE001 - the common case
        return "speaker"


def record_shape(kind: str = "speaker", **vals):
    """One training's shape (kept_over_target, kept_per_prep_min, past_plan; any of them), the last 12 kept."""
    from ..core import fsutil
    try:
        d = json.loads(_shape_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        d = {}
    for k, v in vals.items():
        if k in TRAIN_SHAPE and v and v > 0:
            key = f"{k}:{kind}" if k == "kept_per_prep_min" else k
            d[key] = (d.get(key, []) + [round(float(v), 3)])[-12:]
    try:
        fsutil.write_text(_shape_file(), json.dumps(d))
    except OSError:
        pass


def estimate(item: dict) -> dict:
    """Minutes per step: prepare (download + separate), train (auto length, typical early stop), compare."""
    from .trainer import LENGTHS
    from . import project as P
    raw_min = 0.0
    proj = _project_for_item(item, create=False)
    if item["kind"] == "new":
        return _estimate_new(proj, item)
    if item["kind"] == "improve" and proj is not None:
        return _estimate_improve(proj, item)
    if proj is not None:
        raw_min = sum(min(s.duration, CONFIG["training"]["max_minutes_per_source"] * 60) for s in proj.sources
                      if s.selected and s.status != "ready") / 60
        if item["kind"] == "improve":
            raw_min += _improve_extra_minutes(proj)
    else:
        raw_min = 90.0 if item["kind"] == "improve" else 45.0   # not searched yet: typical
    prep = raw_min / PREP_RT
    # a queued project trains at its own length (the options are saved before this runs: a Quick item says Quick's time)
    length = (proj.train.get("length") if item["kind"] == "project" and proj is not None else None) or "auto"
    if length == "auto":   # R5: the budget scales with the kept minutes (what's kept already + about a quarter of the new raw)
        from .frozen import budget_steps
        kept_min = (proj.kept_seconds() / 60 if proj is not None else 0.0) + raw_min / 4
        steps = budget_steps(kept_min)[0]
        if item["kind"] == "improve" and proj is not None:   # R4: a well-fed voice refines, far below scratch's budget
            steps = _improve_steps(item["target"], proj, raw_min / 4) or steps
    else:
        steps = LENGTHS.get(length, LENGTHS["auto"])
    train = steps * _sec_per_step() / 60   # the planned budget runs in full (R5: early stop only past it): a 6-hour plan says 6 hours
    compare = 1
    if item["kind"] == "improve":   # export + the frozen test (made at a voice's first Improve), priced by its size
        from .frozen import EXPORT_S, compare_seconds, load_test
        try:
            from ..core.voices import get_voice
            test = load_test(get_voice(item["target"]))
        except ModelError:
            test = None
        c = compare_seconds(test)
        compare = (EXPORT_S + c["ensure"] + c["judge"]) / 60
    out = {"prepare": round(prep), "train": round(train), "compare": round(compare)}
    if item["kind"] == "new" and item.get("mode") == "speaking":   # its cadence follows (cadence.chain_cadence)
        from .cadence import CADENCE_MIN
        out["cadence"] = CADENCE_MIN
    out["total"] = round(prep + train + compare + out.get("cadence", 0))
    return out


def _estimate_new(proj, item: dict) -> dict:
    """A new voice, in minutes, the one estimate the plan, the job and the queue all say (rc-eta T1): it prepares clips
    until about the target's worth is kept (what it ends with, measured: kept_over_target), at this PC's preparation
    rate, then trains the budget those minutes give (450 steps a minute) at this PC's step speed, on past the plan as
    far as trainings here go (past_plan). The same whether the search has run yet or not: what's found doesn't decide
    how much is kept."""
    from .frozen import budget_steps
    tc = CONFIG["training"]
    sh = train_shape(person_kind(proj, item.get("target")))
    kept_now = proj.kept_seconds() / 60 if proj is not None else 0.0
    final = max(kept_now, tc["target_minutes"] * sh["kept_over_target"])
    prep = max(0.0, final - kept_now) / sh["kept_per_prep_min"] if kept_now < tc["target_minutes"] else 0.0
    train = budget_steps(final)[0] * sh["past_plan"] * _sec_per_step() / 60
    out = {"prepare": round(prep), "train": round(train), "compare": 1}
    if item.get("mode") == "speaking":   # its cadence follows (cadence.chain_cadence)
        from .cadence import CADENCE_MIN
        out["cadence"] = CADENCE_MIN
    out["total"] = round(prep + train + 1 + out.get("cadence", 0))
    return out


def _estimate_improve(proj, item: dict) -> dict:
    """An Improve, in minutes, on the new voice's estimator (1.0.1, IA pass 11: 31 min said, 14 taken: every selected
    source's raw minutes were priced at the old preparation rate): the clean minutes it should add (about a quarter of
    the new raw) at this PC's measured preparation rate for this kind of person; then a refine's fixed budget, or a
    retrain's budget for all the kept minutes run past its plan as trainings here do; then the export and the frozen
    test, priced by its size."""
    from .frozen import EXPORT_S, budget_steps, compare_seconds, load_test
    raw = sum(min(s.duration, CONFIG["training"]["max_minutes_per_source"] * 60) for s in proj.sources
              if s.selected and s.status != "ready") / 60 + _improve_extra_minutes(proj)
    new_kept = raw / 4
    sh = train_shape(person_kind(proj, item.get("target")))
    prep = new_kept / sh["kept_per_prep_min"]
    refine = _improve_steps(item["target"], proj, new_kept)
    if refine:   # (a given budget: it runs exactly that, no further)
        steps = refine
    else:
        steps = budget_steps(proj.kept_seconds() / 60 + new_kept)[0] * sh["past_plan"]
    train = steps * _sec_per_step() / 60
    try:
        from ..core.voices import get_voice
        test = load_test(get_voice(item["target"]))
    except ModelError:
        test = None
    c = compare_seconds(test)
    compare = (EXPORT_S + c["ensure"] + c["judge"]) / 60
    out = {"prepare": round(prep), "train": round(train), "compare": round(compare), "refine": bool(refine)}
    out["total"] = round(prep + train + compare)
    return out


def _improve_steps(target: str, proj, new_min: float) -> int | None:
    """The steps an Improve of this voice would most likely plan (R4), for the estimate: a refine's when the voice's own
    numbers say refine (well fed, a fair likeness, the same sample rate), else None (scratch's budget). Whether the
    clips grow enough for scratch depends on what the search finds, so the estimate doesn't guess that: the real
    Freeman Improve said 99 min on scratch's budget and took 12 (refine, nothing new found)."""
    from ..core.voices import get_voice, trained_likeness
    from .frozen import recipe, refine_steps
    try:
        v = get_voice(target)
    except ModelError:
        return None
    old_min = float((v.extra or {}).get("train_minutes") or 0)
    sr = int(proj.train.get("sr") or CONFIG["training"]["sample_rate"])
    if recipe(old_min, trained_likeness(v), old_min)[0] != "refine" or (v.sr and int(v.sr) != sr):
        return None
    return refine_steps(new_min or old_min)


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
                raise ModelError(f"{label_of(v.name)} is a demo voice made for UnderCover, not a real person, so there's nobody to find clips of.")
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
    from ..core.voices import listed_voices, voice_info
    from . import service
    queued = {(i["kind"], i["target"].lower()) for i in worklist._state()["queue"] if i["state"] in ("queued", "paused", "running")}
    try:
        qcache = json.loads((path("cache_dir") / "voice_quality.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        qcache = {}
    out = []
    for v in listed_voices():
        ex = v.extra or {}
        c = {"voice": v.name, **voice_info(v), "minutes": ex.get("train_minutes"),
             "singing": None if ex.get("from_speech") is None else not ex.get("from_speech"), "likeness": None}
        if c["demo"]:   # Agent Smoke: never offered for improving (its "person" is nobody to search for)
            out.append({**c, "can": False, "note": "a demo voice made for UnderCover, not a real person, so there's nobody to find clips of", "cadence": service.cadence_status(v)})
            continue
        if ex.get("trained"):
            person = _person_from_voice(v)
            proj = _project_for_item({"kind": "improve", "target": v.name}, create=False)
            from ..core.voices import trained_likeness
            c["likeness"] = trained_likeness(v)
            item = {"kind": "improve", "target": v.name}
            c.update(person=person, kind="improve", target=v.name, can=True,
                     note=("more of their singing (album tracks), fresh training; the new one waits beside it for you "
                           "to listen" if c["singing"]
                           else "more of their speech, fresh training; the new one waits beside it for you to listen")
                     + ("" if proj is not None else " · its clips were cleaned up, so they're prepared again"))
        elif voice_person(v):   # checked, or a guess from its name (renaming the display corrects it)
            person = voice_person(v)
            c["person_guessed"] = person_guessed(v)
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
    from .api import pending_of, waiting_note
    owner = pending_of(name)   # a voice of that name was there: it waits beside it
    r = {"voice": name, "after": score, "pending_for": owner,
         "note": waiting_note(owner) if owner else f"'{name}' added to your voices."}
    if item["kind"] == "new":   # a speaking voice goes on to its cadence ("newcad" does its own, after this)
        from . import cadence
        c = cadence.chain_cadence(job, name, worklist.pusher(job), worklist.control_for(job))
        if c is not None:
            r.update(cadence=c, note=r["note"] + " " + c["note"])
    return r


def _improve(job, item) -> dict:
    """Improve that improves (phase 26): more material through the source gate (R6), refine or scratch as the engine
    decides (R4) with the scaled budget (R5), then the candidate scored on the voice's frozen test (R1, R2) and offered
    only if it clears the conservative bar (R3); otherwise "tried, no improvement: yours is kept" and the candidate
    parks in Set aside. The ear decides anything offered; nothing here deletes a voice."""
    from . import frozen
    from ..core.voices import get_voice, set_aside
    from . import project as P
    from . import sources as srcmod
    from ..core.speaker import SpeakerEmbedder
    from .trainer import Trainer, epoch_line, export_voice
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
                push("night", 0.05 + 0.35 * f, f"Preparing clips, source {kw['index']} of {kw['count']}: {label}" if kw.get("count")
                     else f"Preparing clips: {label}")
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
            # R6: the source gate, against the approved voice (its pitch) and the person's reference; the user's own
            # sources and phrases are never touched. Unticking alone would leave a source's cut phrases in training.
            push("night", 0.38, "Checking which clips belong to this voice")
            rm = gpu.MODELS.converter.rmvpe

            def pitch_of(clip):
                f = rm.infer_from_audio(frozen._load16(clip), thred=0.03)
                f = f[f > 0]
                return float(np.median(f)) if len(f) > 20 else None
            gate = frozen.gate_project(proj, proj.load_embeddings(), (old.extra or {}).get("f0_median"), pitch_of)
            proj.save()
            st = proj.train
            for k in ("epoch", "best", "saves", "state", "outcome", "epochs", "planned_epochs", "init_from", "lr", "steps"):
                st.pop(k, None)   # exported stays: the voice is still there
            kept_min = proj.kept_seconds() / 60
            old_min = float((old.extra or {}).get("train_minutes") or 0)
            from ..core.voices import trained_likeness
            how, why = frozen.recipe(old_min, trained_likeness(old), kept_min)
            st["improve"] = {"left_out": gate["left_out"], "kept_min": round(kept_min, 1), "old_min": round(old_min, 1),
                             "added_min": round(max(0.0, kept_min - old_min), 1), "recipe": how, "why": why}
            st.update(sr=st.get("sr") or CONFIG["training"]["sample_rate"], length="auto", voice_name=cand, seed=1234)
            if how == "refine" and old.sr and int(old.sr) != int(st["sr"]):   # the weights wouldn't fit another rate's model
                how, why = "scratch", f"your voice was made at {int(old.sr) // 1000} kHz and this project trains at {int(st['sr']) // 1000} kHz"
                st["improve"].update(recipe=how, why=why)
            if how == "refine":   # R4: continue from the approved voice's weights, gently, on the new clips
                st.update(init_from=str(old.pth), lr=frozen.REFINE_LR, steps=frozen.refine_steps(max(0.0, kept_min - old_min) or kept_min))
            proj.save()
        if gate["kept_min"] < frozen.GATE_MIN_KEPT_MIN:
            with P.lock(proj.name):
                proj = P.Project.load(proj.name)
                proj.train["voice_name"] = old.name   # the form's name stays the voice's own (P3-2)
                proj.save()
            left = "; ".join(f"{x['title'][:40]} ({x['why']})" for x in gate["left_out"][:4])
            return {"voice": old.name, "candidate": None, "replaced": False, "pending": False, "kept": True,
                    "tried": st.get("improve"), "note": f"Tried, nothing usable: the new clips were left out ({left}), so "
                    f"{label_of(old.name)} is kept as it is."}
        # the same clips the approved voice learnt from (nothing new found or prepared, nothing left out): training
        # again can only shuffle the dice, so nothing is trained and the voice is kept untried
        new_ready = any(s.id in set(todo) and s.status == "ready" for s in proj.sources)
        if frozen.nothing_new(kept_min, old_min, gate["left_out"], new_ready):
            with P.lock(proj.name):
                proj = P.Project.load(proj.name)
                proj.train["voice_name"] = old.name
                for k in ("init_from", "lr", "steps"):
                    proj.train.pop(k, None)
                proj.save()
            return {"voice": old.name, "candidate": None, "replaced": False, "pending": False, "kept": True, "nothing_new": True,
                    "tried": st.get("improve"), "note": f"Nothing new of {proj.person} to learn from: no new clips were found and "
                    f"every source still belongs, so {label_of(old.name)} is kept as it is."}
        (proj.dir / "train" / "resume.pt").unlink(missing_ok=True)
    # 2. train (R4 / R5: the recipe and the budget were set above, or kept from the paused run)
    conv = gpu.MODELS.converter
    # (1.1) the frozen test is made before the candidate trains, not after: a singer's test freezes their own sung
    # phrases out of training, and made at the comparing step the first candidate would have learned what it's judged on
    if frozen.load_test(old) is None:
        push("night", 0.39, "Setting aside the test this voice is judged on")
        frozen.ensure(old, P.Project.load(proj.name), frozen.GpuEngine(conv, SpeakerEmbedder()))
    proj = P.Project.load(proj.name)
    plan = proj.train.get("improve") or {}
    push("night", 0.4, (f"Refining {label_of(old.name)} on {proj.kept_seconds() / 60:.1f} min of {proj.person}"
                        if plan.get("recipe") == "refine" else f"Training on {proj.kept_seconds() / 60:.1f} min of {proj.person}"))

    def tprog(**ev):
        if ev.get("stage") == "training" and ev.get("epochs"):
            job.push(stage="training", frac=round(0.4 + 0.55 * ev.get("fraction", 0), 3), epoch=ev.get("epoch"),
                     epochs=ev["epochs"], eta=ev.get("eta"), new_work=bool(ev.get("extended")),
                     planned=ev.get("planned"), message=epoch_line(ev))
    st = Trainer(proj, conv.hubert, conv.rmvpe, device=str(conv.device), half=conv.half, converter=conv).run(control, progress=tprog)
    if st.get("state") == "paused":
        from ..core.jobs import Paused
        raise Paused()
    # 3. the candidate on the voice's frozen test (R1, R2), offered only past the bar (R3)
    push("night", 0.93, "Comparing the new voice with yours on the same test")
    proj = P.Project.load(proj.name)
    export_voice(proj, name=cand, overwrite=True)
    engine = frozen.GpuEngine(conv, SpeakerEmbedder())
    old = get_voice(old.name)
    kept_min = round(proj.kept_seconds() / 60, 1)
    plan = proj.train.get("improve") or {}
    with P.lock(proj.name):
        pr = P.Project.load(proj.name)
        pr.train["exported"] = old.name   # the installed voice is still the old one
        pr.train["voice_name"] = old.name   # the training form's name again (not the candidate's: P3-2)
        pr.save()
    cost = frozen.compare_seconds(frozen.load_test(old))   # the time left said from the test's own size, not extrapolated
    try:
        frozen.ensure(old, proj, engine, push=lambda f, m: push("night", 0.93 + 0.03 * f, m,   # lazily: its first Improve
                                                                total_left=round((1 - f) * cost["ensure"] + cost["judge"], 1)))
        verdict = frozen.judge(old, get_voice(cand), engine, push=lambda f, m: push("night", 0.96 + 0.03 * f, m,
                                                                                    total_left=round((1 - f) * cost["judge"], 1)))
    except (jobs.Cancelled, jobs.Paused):
        raise
    except Exception as e:   # noqa: BLE001 - a candidate that can't be judged is never left loose as "<voice>-next"
        why = jobs.friendly(e, "comparing the voices")
        aside = set_aside(get_voice(cand).dir, note={"why": f"Improve's attempt, not judged: {why}",
                                                    "display": f"{label_of(old.name)}: Improve's attempt", "measures": plan})
        gpu.forget_voice(cand)
        return {"voice": old.name, "candidate": None, "replaced": False, "pending": False, "kept": True, "tried": plan,
                "aside": aside.name, "note": f"The new voice couldn't be compared with yours ({why}), so {label_of(old.name)} "
                "is kept as it is. The attempt is in Storage → Set aside."}
    before, after = verdict["before"], verdict["after"]
    # Internal Affairs P2-1: the ear decides, and the user's voice is never deleted. An offered candidate waits BESIDE
    # the current one (pending); one that doesn't clear the bar parks in Set aside with its numbers. Neither touches
    # the current voice.
    tried = {**plan, "minutes": kept_min, "terms": verdict["terms"], "compare": {c: {k: v for k, v in d.items() if k in ("n", "id", "hnr", "contrast")}
                                                                                for c, d in verdict["compare"].items()}}
    left = "; ".join(f"{x['title'][:40]} ({x['why']})" for x in plan.get("left_out", [])[:3])
    left_s = f" {len(plan['left_out'])} source{'s' if len(plan['left_out']) != 1 else ''} left out ({left})." if plan.get("left_out") else ""
    if verdict["offer"]:
        words = frozen.words_of(verdict)
        mark_improved_pending(old.name, cand, before, after, kept_min, words=words, measures=tried)
        note = (f"{'Refined' if plan.get('recipe') == 'refine' else 'Retrained'} on {kept_min} min of "
                f"{'singing' if proj.mode != 'speaking' else 'speech'}: {words}. The new one waits beside yours; listen to both "
                f"and keep the one you like.{left_s}")
        return {"voice": old.name, "candidate": cand, "before": before, "after": after, "replaced": False, "pending": True,
                "minutes_of_voice": kept_min, "words": words, "tried": tried, "note": note}
    cand_dir = get_voice(cand).dir
    aside = set_aside(cand_dir, note={"why": f"Improve tried, no improvement: {', '.join(verdict['reasons'])}",
                                      "display": f"{label_of(old.name)}: Improve's attempt", "measures": tried})
    gpu.forget_voice(cand)
    note = (f"Tried {'refining' if plan.get('recipe') == 'refine' else 'retraining'} on {kept_min} min "
            f"({plan.get('added_min', 0)} min new): no improvement ({', '.join(verdict['reasons'])}), so "
            f"{label_of(old.name)} is kept as it is.{left_s} The attempt is in Storage → Set aside.")
    return {"voice": old.name, "candidate": None, "before": before, "after": after, "replaced": False, "pending": False,
            "kept": True, "minutes_of_voice": kept_min, "tried": tried, "aside": aside.name, "note": note}


def mark_improved_pending(voice: str, cand: str, before, after, minutes, **more):
    """The current voice knows a retrained one waits beside it (extra.improved_pending), and the candidate knows whose
    it is (extra.pending_for). Nothing is replaced or removed. more: words (what changed, R3a) and measures."""
    import json
    import time
    from ..core import fsutil
    from ..core.voices import get_voice
    v = get_voice(voice)
    v.extra = {**(v.extra or {}), "improved_pending": {"voice": cand, "before": before, "after": after,
                                                       "minutes": minutes, "made": time.strftime("%Y-%m-%d %H:%M"), **more}}
    fsutil.write_text(v.dir / "meta.json", json.dumps(v.to_json(), indent=2))
    c = get_voice(cand)
    c.extra = {**(c.extra or {}), "pending_for": voice}
    fsutil.write_text(c.dir / "meta.json", json.dumps(c.to_json(), indent=2))


def _improve_target(target: str) -> str:
    from ..core.voices import get_voice, is_demo
    v = get_voice(target)   # friendly error if it doesn't exist
    if is_demo(v):
        raise ModelError(f"{label_of(v.name)} is a demo voice made for UnderCover, not a real person, so there's nobody to find clips "
                         "of to improve it.")
    if (v.extra or {}).get("pending_for"):
        raise ModelError("That's a retrained version waiting to be compared, not a voice to improve.")
    pending = (v.extra or {}).get("improved_pending")
    if pending:   # never over a candidate nobody has listened to yet
        raise ModelError(f"A retrained {label_of(v.name)} is already waiting beside it. Compare the two first.")
    from ..core.voices import pending_candidate, voices_dir
    if pending_candidate(f"{v.name}-next") or (voices_dir() / f"{v.name}-next").exists():
        raise ModelError(f"A retrained {label_of(v.name)} from an earlier improve ({v.name}-next) is still in your "
                         "voices. Remove it first (SafeHouse) if you don't want it.")
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
    from .api import pending_of, waiting_note
    owner = pending_of(name)
    return {"voice": name, "after": score, "pending_for": owner,
            "note": waiting_note(owner) if owner else f"'{name}' added to your voices, from your choices in DoubleAgent."}


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


def person_of_project(target: str) -> str:
    from . import project as P
    return slug(P.Project.load(target).person or target)


def live_person(person: str):
    """The job making this person's voice live right now (prepare / train / Do it all on their project), else None."""
    proj = _project_named(person)
    return live_project(proj.name) if proj is not None else None


def live_project(name: str):
    from . import api as DA
    a = DA.ACTIVE.get(name)
    return a["job"] if a and not a["job"].finished else None


worklist.register_kind("project", run=_project, estimate=estimate, target=_project_target, shown=shown_project,
                       label=lambda item: f"Training {shown_project(item)} (your choices)", options=_project_options,
                       person=person_of_project, live=live_project)
worklist.register_kind("improve", run=_improve, estimate=estimate, target=_improve_target, shown=shown_voice,
                       label=lambda item: f"Improving {shown_voice(item)}")
worklist.register_kind("new", run=_new, estimate=estimate,
                       label=lambda item: f"Training a voice of {item['target']}" + (
                           f", then {item['target']}'s cadence" if item.get("mode") == "speaking" else ""),
                       modes=("speaking", "singing", "both"), person=slug, live=live_person)
