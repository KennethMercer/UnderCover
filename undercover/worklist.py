"""Background voice work: Pause / Resume for everything that trains, and the overnight queue.

Pause (top of the page) stops all voice work cleanly: a training saves its resume point, preparing clips stops
after the current one, a one-shot that's training a voice is set aside. What was paused is remembered (also across
a restart) and Resume starts it again in the same order. Covers aren't paused: they're short and always go first.

The overnight queue runs "improve a voice" / "train a new voice" items one after another when the GPU is free,
optionally from a start time and only if an item can finish before a "finish by" time. Improving a voice adds
singing (the person's album tracks first) or more speech, re-checks every clip, retrains from scratch, and
replaces the installed voice only if the new one measures better on the same likeness test.
"""
import json
import threading
import time
import uuid
from datetime import datetime, timedelta

from .config import CONFIG, path
from .models import ModelError, slug

TRAIN_KINDS = ("train", "train-prepare", "train-auto", "auto", "night")
_LOCK = threading.RLock()
_STATE: dict | None = None
S = None   # the server module, set by start()

PREP_RT = 3.5          # minutes of source audio prepared per minute (separation + lead split + de-reverb ran 4.3x
                       # realtime on Elliott Smith's album tracks; downloads and phrase cutting take the rest)
EARLY_STOP = 0.7       # auto-length trainings stop on their own at ~70% of the step budget (Elliott: 219 of 296 epochs)
COMPARE_MIN = 3.0      # export + likeness test of old and new voice
IMPROVE_SINGING_MIN = 30   # minutes of clean singing an "improve" aims for (more than ~30 rarely helps)


def _file():
    """One file per server port, so a test server (7861) never touches the real queue or its pause."""
    port = S.SERVER.server_address[1] if S is not None and getattr(S, "SERVER", None) else CONFIG["ui"]["port"]
    return path("cache_dir") / ("worklist.json" if port == CONFIG["ui"]["port"] else f"worklist-{port}.json")


def _state() -> dict:
    global _STATE
    if _STATE is None:
        try:
            _STATE = json.loads(_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _STATE = {}
        _STATE.setdefault("held", False)
        _STATE.setdefault("resume", [])
        _STATE.setdefault("queue", [])
        _STATE.setdefault("armed", False)
        _STATE.setdefault("settings", {"start_at": None, "finish_by": "07:00", "keep_awake": True})
        _STATE.setdefault("report", None)
        for it in _STATE["queue"]:   # a crash or restart mid-item: run it again (it continues where it was)
            if it.get("state") == "running":
                it["state"] = "queued"
    return _STATE


def _save():
    from . import fsutil
    fsutil.write_text(_file(), json.dumps(_state(), indent=1))


# ---------------------------------------------------------------- pause / resume

def _log(msg: str):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] worklist: {msg}", flush=True)


def _voice_jobs():
    return [j for j in list(S.JOBS.values()) if j.kind in TRAIN_KINDS and not j.finished]


def active() -> bool:
    """Voice work running or waiting in the GPU queue."""
    return bool(_voice_jobs())


def pause_all(resume_after_restart: bool = False) -> dict:
    """Stop all voice work cleanly and remember it for Resume (or, for a restart, resume it right after)."""
    with _LOCK:
        st = _state()
        st["held"] = True
        if resume_after_restart:
            st["resume_after_restart"] = True
        _save()
        _log(f"pause (restart={resume_after_restart}); running: {[j.kind for j in _voice_jobs()]}")
        from .train import api as train_api
        for j in _voice_jobs():
            j.pausing = True
            ctl = j.control or next((a["control"] for a in train_api.ACTIVE.values() if a["job"] is j and a.get("control")), None)
            if ctl is not None:
                ctl.pause = True
    return status()


def remember(job):
    """Called when a job ends as "paused": keep how to start it again."""
    spec = getattr(job, "resume_spec", None)
    if not spec:
        return
    with _LOCK:
        st = _state()
        if spec.get("kind") == "night":
            for it in st["queue"]:
                if it["id"] == spec.get("item"):
                    it["state"] = "paused"
        elif spec not in st["resume"]:
            st["resume"].append(spec)
        _save()
        _log(f"remembered {spec.get('label')}; held={st['held']} resume={len(st['resume'])}")


def resume_all() -> dict:
    """Start everything Pause stopped, in the same order, and let the queue run again."""
    with _LOCK:
        st = _state()
        st["held"] = False
        specs, st["resume"] = st["resume"], []
        _log(f"resume {[x.get('label') for x in specs]}")
        for it in st["queue"]:
            if it.get("state") == "paused":
                it["state"] = "queued"
        _save()
    problems = []
    for spec in specs:
        try:
            _start(spec)
        except Exception as e:   # noqa: BLE001 - shown to the user, the rest still resume
            problems.append(f"{spec.get('label', spec['kind'])}: {e}")
    out = status()
    if problems:
        out["problems"] = problems
    return out


def _start(spec: dict):
    from .train import api as train_api
    k = spec["kind"]
    if k == "prepare":
        return train_api.start_prepare(S, spec["project"])
    if k == "train":
        return train_api.start_train(S, spec["project"], {})
    if k == "auto":
        return train_api.start_auto(S, spec["project"], {})
    if k == "oneshot":
        return S.start_oneshot({"text": spec["text"], "prefer_train": spec.get("prefer_train"), "song_url": spec.get("song_url")})
    raise ModelError(f"Don't know how to resume '{k}'.")


def held() -> bool:
    return bool(_state().get("held"))


# ---------------------------------------------------------------- the overnight queue

def add(kind: str, target: str) -> dict:
    target = (target or "").strip()
    if kind not in ("improve", "new"):
        raise ModelError("Pick Improve a voice or Train a new voice.")
    if not target:
        raise ModelError("Type a voice or a person's name.")
    if kind == "improve":
        from .models import get_voice
        v = get_voice(target)   # friendly error if it doesn't exist
        target = v.name
    item = {"id": uuid.uuid4().hex[:8], "kind": kind, "target": target, "state": "queued",
            "added": time.strftime("%Y-%m-%d %H:%M")}
    item["estimate"] = estimate(item)
    with _LOCK:
        _state()["queue"].append(item)
        _save()
    return status()


def remove(item_id: str) -> dict:
    with _LOCK:
        st = _state()
        it = next((i for i in st["queue"] if i["id"] == item_id), None)
        if it and it["state"] == "running":
            raise ModelError("That one is running. Pause first, then remove it.")
        st["queue"] = [i for i in st["queue"] if i["id"] != item_id]
        _save()
    return status()


def reorder(ids: list) -> dict:
    with _LOCK:
        st = _state()
        by = {i["id"]: i for i in st["queue"]}
        st["queue"] = [by[i] for i in ids if i in by] + [i for i in st["queue"] if i["id"] not in ids]
        _save()
    return status()


def arm(when: str | None) -> dict:
    """Start the queue now (when=None) or at "HH:MM"; arm(False) stops it from starting more."""
    with _LOCK:
        st = _state()
        if when is False:
            st["armed"] = False
        else:
            st["armed"] = True
            st["settings"]["start_at"] = when or None
            st["held"] = False
        _save()
    return status()


def settings(**kw) -> dict:
    with _LOCK:
        st = _state()
        for k in ("finish_by", "keep_awake"):
            if k in kw:
                st["settings"][k] = kw[k]
        _save()
    return status()


def dismiss_report() -> dict:
    with _LOCK:
        _state()["report"] = None
        _save()
    return status()


def _sec_per_step() -> float:
    from .train.project import list_projects
    vals = [p["train"].get("sec_per_step") for p in list_projects() if p["train"].get("sec_per_step")]
    vals = sorted(v for v in vals if 0.05 < v < 3)
    return vals[len(vals) // 2] if vals else 0.35


def estimate(item: dict) -> dict:
    """Minutes per step: prepare (download + separate), train (auto length, typical early stop), compare."""
    from .train.trainer import LENGTHS
    from .train import project as P
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
    from .train import project as P
    try:
        if item["kind"] == "improve":
            from .models import get_voice
            v = get_voice(item["target"])
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
            from . import autopilot
            return autopilot.project_for(person)
        # rebuilding a cleaned-up project: same kind of material as the voice was trained on (a speech voice of an
        # actor with narration albums mustn't turn into a singing project), with a full search
        from .train import source as srcmod
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
    from .train import project as P
    key = slug(person)
    best, score = None, 0.0
    for p in P.list_projects():
        r = difflib.SequenceMatcher(None, key, slug(p["person"] or p["name"])).ratio()
        if r > score:
            best, score = p, r
    return P.Project.load(best["name"]) if best and score >= 0.9 else None


def _person_from_voice(v) -> str:
    src = (v.source or "")
    if src.startswith("trained in UnderCover: "):
        return src.split(": ", 1)[1].split(",")[0]
    return v.name.removesuffix("-uc").replace("-", " ")


def candidates() -> list:
    """Voices for the "Improve my voices" picker: what each was trained on, its last measured likeness, what an
    improve would do and roughly how long it takes."""
    from .models import list_voices
    from . import autopilot
    queued = {(i["kind"], i["target"].lower()) for i in _state()["queue"] if i["state"] in ("queued", "paused", "running")}
    try:
        qcache = json.loads((path("cache_dir") / "voice_quality.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        qcache = {}
    out = []
    for v in list_voices():
        ex = v.extra or {}
        c = {"voice": v.name, "trained": bool(ex.get("trained")), "minutes": ex.get("train_minutes"),
             "singing": None if ex.get("from_speech") is None else not ex.get("from_speech"), "likeness": None}
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
            out.append({**c, "can": False, "note": "downloaded voice: UnderCover doesn't know whose voice it is"})
            continue
        c["estimate"] = estimate(item)["total"]
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
        from .songs import _music_tracks
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


def status() -> dict:
    st = _state()
    now = datetime.now()
    running = []
    for j in _voice_jobs():
        ev = j.events[-1] if j.events else {}
        running.append({"job": j.id, "kind": j.kind, "label": (j.resume_spec or {}).get("label") or j.kind,
                        "message": ev.get("message") or ev.get("note") or ev.get("label") or ev.get("stage"),
                        "epoch": ev.get("epoch"), "epochs": ev.get("epochs"), "eta": ev.get("eta"),
                        "fraction": ev.get("fraction", ev.get("frac")), "pausing": j.pausing})
    left = [i for i in st["queue"] if i["state"] in ("queued", "paused", "running")]
    total = sum(i.get("estimate", {}).get("total", 0) for i in left)
    start = now
    if st["armed"] and st["settings"].get("start_at"):
        start = _next_time(st["settings"]["start_at"], now)
    return {"held": st["held"], "resume": [s.get("label", s["kind"]) for s in st["resume"]], "running": running,
            "queue": st["queue"], "armed": st["armed"], "settings": st["settings"], "report": st["report"],
            "total_min": total, "start": start.strftime("%H:%M") if left else None,
            "done_by": (start + timedelta(minutes=total)).strftime("%H:%M") if left else None}


def _next_time(hhmm: str, now: datetime) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    t = now.replace(hour=h, minute=m, second=0, microsecond=0)
    return t if t >= now - timedelta(minutes=1) else t + timedelta(days=1)


def _deadline(now: datetime) -> datetime | None:
    fb = _state()["settings"].get("finish_by")
    if not fb:
        return None
    d = _next_time(fb, now)
    return d


def _in_window(now: datetime) -> bool:
    st = _state()
    sa = st["settings"].get("start_at")
    if not sa:
        return True
    start = _next_time(sa, now)
    if start <= now + timedelta(minutes=1):
        return True
    # started earlier tonight and still before the deadline
    prev = start - timedelta(days=1)
    dl = _deadline(prev)
    return prev <= now and (dl is None or now < dl)


# ---------------------------------------------------------------- runner

def start(server_module):
    global S
    S = server_module
    _state()
    threading.Thread(target=_loop, daemon=True, name="worklist").start()


def _gpu_idle() -> bool:
    return S.GPU_QUEUE.empty() and not any(not j.finished for j in list(S.JOBS.values()))


def _loop():
    awake = False
    while True:
        try:
            st = _state()
            if S.STATUS.get("models") == "ready" and (st.get("resume_after_restart") or (st["resume"] and not st["held"])):
                # paused only to restart (or paused work left without a hold): carry on
                with _LOCK:
                    st.pop("resume_after_restart", None)
                    _save()
                r = resume_all()
                if r.get("problems"):
                    _log(f"resume problems: {r['problems']}")
            busy = bool(_voice_jobs())
            want_awake = busy and st["settings"].get("keep_awake", True)
            if want_awake != awake:
                _keep_awake(want_awake)
                awake = want_awake
            if not st["held"] and st["armed"] and _gpu_idle() and not S.STATUS.get("gpu_problem"):
                now = datetime.now()
                nxt = next((i for i in st["queue"] if i["state"] == "queued"), None)
                if nxt is None:
                    if any(i["state"] in ("done", "failed", "skipped") for i in st["queue"]):
                        with _LOCK:
                            st["armed"] = False   # queue finished
                            _save()
                elif _in_window(now):
                    dl = _deadline(now) if st["settings"].get("start_at") else None
                    nxt["estimate"] = estimate(nxt)
                    if dl is not None and now + timedelta(minutes=nxt["estimate"]["total"]) > dl:
                        pass   # wouldn't finish before the deadline: waits for the next night
                    else:
                        _run_item(nxt)
        except Exception as e:   # noqa: BLE001 - the loop must survive anything
            _log(f"loop error: {type(e).__name__}: {e}")
        time.sleep(10)


def _keep_awake(on: bool):
    """Windows: don't sleep while voice work runs (per-thread request; released when it ends)."""
    try:
        import ctypes
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))
    except Exception:   # noqa: BLE001 - not Windows
        pass


def _run_item(item: dict):
    job = S.Job("night")
    S.JOBS[job.id] = job
    label = (f"Improving {item['target']}" if item["kind"] == "improve" else f"Training a voice of {item['target']}")
    job.resume_spec = {"kind": "night", "item": item["id"], "label": label}
    with _LOCK:
        item.update(state="running", job=job.id, started=item.get("started") or time.strftime("%H:%M"))
        st = _state()
        if not st["report"] or st["report"].get("closed"):
            st["report"] = {"started": time.strftime("%Y-%m-%d %H:%M"), "results": []}
        _save()

    def run(job):
        t0 = time.time()
        try:
            res = (_improve if item["kind"] == "improve" else _new)(job, item)
            state = "done"
        except Exception as e:   # noqa: BLE001
            if type(e).__name__ == "Paused" or job.pausing:
                with _LOCK:
                    item["state"] = "paused"
                    _save()
                job.finish("paused", message="Paused. Resume continues where it stopped.")
                return
            if type(e).__name__ == "Cancelled":
                res, state = {"note": "cancelled"}, "skipped"
            else:
                res, state = {"note": S.friendly(e, "")}, "failed"
        mins = round((time.time() - t0) / 60)
        with _LOCK:
            item.update(state=state, finished=time.strftime("%H:%M"), result=res, minutes=mins)
            rep = _state()["report"]
            if rep is not None:
                rep["results"].append({"kind": item["kind"], "target": item["target"], "state": state, "minutes": mins, **res})
                rep["finished"] = time.strftime("%Y-%m-%d %H:%M")
            _save()
        job.finish("done" if state == "done" else "error", message=res.get("note") or "Done.", result=res)

    S.GPU_QUEUE.put((job, run))


def _control(job):
    from .train.trainer import Control
    control = Control()

    def service():
        while not S.GPU_QUEUE.empty():
            try:
                other, fn = S.GPU_QUEUE.get_nowait()
            except Exception:   # noqa: BLE001
                return
            S.run_job(other, fn)
    control.service = service
    job.control = control
    return control


def _push(job):
    def push(stage, frac, message, **kw):
        if job.cancel:
            raise S.Cancelled()
        job.push(stage=stage, frac=round(frac, 3), message=message, **kw)
    return push


def _new(job, item) -> dict:
    from . import autopilot, voicecheck
    from .train.speaker import SpeakerEmbedder
    name = autopilot.train_new_voice(S, job, item["target"], _control(job), _push(job), target_minutes=IMPROVE_SINGING_MIN)
    proj = _project_for_item({"kind": "improve", "target": name}, create=False)
    score = None
    if proj is not None:
        try:
            score = voicecheck.check(name, proj, S.ENGINE.converter, SpeakerEmbedder()).score
        except Exception:   # noqa: BLE001 - only for the report
            pass
    return {"voice": name, "after": score, "note": f"'{name}' added to your voices."}


def _improve(job, item) -> dict:
    """More material, re-checked clips, a fresh training, and the new voice only if it measures better."""
    from . import voicecheck
    from .models import get_voice, remove_voice
    from .train import project as P
    from .train import source as srcmod
    from .train.speaker import SpeakerEmbedder
    from .train.trainer import Trainer, export_voice
    push, control = _push(job), _control(job)
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
                P.prepare_sources(proj.name, todo, S.ENGINE.separator, SpeakerEmbedder(), progress=prog,
                                  between=control.service, cancel=lambda: job.cancel or job.pausing,
                                  friendly=lambda e: S.friendly(e, "preparing a source"))
            except P.Cancelled:
                from .autopilot import Paused
                raise Paused() if job.pausing else S.Cancelled()
        with P.lock(proj.name):
            proj = P.Project.load(proj.name)
            if proj.speakers and not proj.reference.get("confirmed"):
                P.set_reference(proj, cluster=proj.reference["cluster"])
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
    conv = S.ENGINE.converter
    proj = P.Project.load(proj.name)
    push("night", 0.4, f"Training on {proj.kept_seconds() / 60:.1f} min of {proj.person}")

    def tprog(**ev):
        if ev.get("stage") == "training" and ev.get("epochs"):
            job.push(stage="training", frac=round(0.4 + 0.55 * ev.get("fraction", 0), 3), epoch=ev.get("epoch"),
                     epochs=ev["epochs"], eta=ev.get("eta"),
                     message=f"Training: epoch {ev.get('epoch', 0)} of {ev['epochs']}")
    st = Trainer(proj, conv.hubert, conv.rmvpe, device=str(conv.device), half=conv.half, converter=conv).run(control, progress=tprog)
    if st.get("state") == "paused":
        from .autopilot import Paused
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
    if before is None or after > before + 0.005:
        proj = P.Project.load(proj.name)
        export_voice(proj, name=old.name, overwrite=True)
        remove_voice(cand)
        if S.ENGINE._conv:
            S.ENGINE._conv.voices.pop(old.name, None)
        return {"voice": old.name, "before": before, "after": after, "replaced": True, "minutes_of_voice": kept_min,
                "note": f"Improved: likeness {before if before is not None else '?'} -> {after}, trained on {kept_min} min."}
    remove_voice(cand)
    with P.lock(proj.name):
        proj = P.Project.load(proj.name)
        proj.train["exported"] = old.name   # the installed voice is still the old one
        proj.save()
    return {"voice": old.name, "before": before, "after": after, "replaced": False, "minutes_of_voice": kept_min,
            "note": f"No gain (likeness {before} vs new {after}): kept the old voice."}
