"""DoubleAgent's HTTP routes (the Train a voice page and the overnight queue's pickers). The app's server routes
/api/train/... and the two /api/work/ lookups here.

GET    /api/work/candidates                     voices that can be improved overnight (doubleagent/night.py)
GET    /api/work/lookup?q=                      name suggestions for a new voice
GET    /api/train/projects                      list
POST   /api/train/projects                      {person, mode} -> create + search
GET    /api/train/projects/<p>                  full state
DELETE /api/train/projects/<p>
POST   /api/train/projects/<p>/search           {person?, mode?} -> fresh source list (keeps your own)
POST   /api/train/projects/<p>/sources          {url} add a link | {id, selected} tick/untick
POST   /api/train/projects/<p>/upload           raw body + X-Filename: add your own recording
POST   /api/train/projects/<p>/prepare          job: download, clean, split, match
POST   /api/train/projects/<p>/reference        {cluster} | {clip}
POST   /api/train/projects/<p>/phrases          {ids: [...], choice: true|false|null}
POST   /api/train/projects/<p>/train            {voice_name, length, sr, fresh} -> job
POST   /api/train/projects/<p>/pause | /stop
POST   /api/train/projects/<p>/export           {epoch?, name?, overwrite?} -> job
POST   /api/train/projects/<p>/transcribe       job: transcripts of the kept speech clips (phase 4; resumable)

"Keep this one" (phase 9): a new cadence for a voice that has one waits in tts/pending/ until the user picks by ear
GET    /api/train/voices/<v>/cadence             the live pack and the waiting one (ids, scores, readings made?)
POST   /api/train/voices/<v>/cadence/ab          job: both packs read the same short line (once; kept with each pack)
GET    /api/train/voices/<v>/cadence/sample/<current|pending>   that reading (WAV)
POST   /api/train/voices/<v>/cadence/verdict     {keep: "current"|"pending", why?} -> swap or set aside; verdict saved
"""
import json
import re
import threading
import uuid
from pathlib import Path
from urllib.parse import unquote

from ..core import files, gpu, jobs, worklist
from ..core.config import CONFIG, path
from ..core.voices import ModelError, label_of, slug
from . import project as P
from .sources import Source

TCFG = CONFIG["training"]
ACTIVE: dict = {}   # project name -> {"job": Job, "control": Control | None, "kind": str}


def _heal(proj):
    """Called under the project lock. After a crash or server restart the saved state can say "training" with no
    job running; offer Resume when a resume point exists (otherwise forget the run)."""
    st = proj.train
    if st.get("state") in ("training", "queued") and not _busy(proj.name):
        if (proj.dir / "train" / "resume.pt").exists() and st.get("epoch", 0) > 0:
            st["state"] = "paused"
            st["outcome"] = "interrupted"
        else:
            st.pop("state", None)
        proj.save()


def _busy(name: str) -> dict | None:
    a = ACTIVE.get(name)
    return a if a and not a["job"].finished else None


def project_json(proj: P.Project) -> dict:
    tdir = proj.dir / "train"
    hist = []
    if (tdir / "history.jsonl").exists():
        hist = [json.loads(l) for l in (tdir / "history.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    saves = []
    for s in proj.train.get("saves", []):
        pv = tdir / "previews" / f"e{s['epoch']}.wav"
        saves.append({**s, "preview": files.file_url(pv) if pv.exists() else None,
                      "available": (tdir / f"e{s['epoch']}.pth").exists()})
    real = tdir / "previews" / "real.wav"
    a = _busy(proj.name)
    from ..core import worklist
    queued = worklist.queued_for("project", proj.name)   # "Queued for tonight" on the Deep path
    return {
        "queued": queued,
        **proj.summary(),
        "sources": [{**s.to_json(), "raw": files.file_url(proj.dir / "raw" / f"{s.id}.wav")} for s in proj.sources],
        "phrases": [{"id": p.id, "source": p.source, "start": p.start, "dur": p.dur, "score": p.score, "mix": p.mix,
                     "music_db": p.music_db, "reason": p.reason, "choice": p.choice, "kept": p.kept, "sung": p.sung,
                     "url": files.file_url(proj.clip_path(p.id))} for p in proj.phrases],
        "speakers": [{**sp, "url": files.file_url(proj.clip_path(sp["clip"]))} for sp in proj.speakers],
        "reference_url": files.file_url(proj.clip_path(proj.reference["clip"])) if proj.reference.get("clip") else None,
        "history": hist, "saves": saves, "real_preview": files.file_url(real) if real.exists() else None,
        "job": {"id": a["job"].id, "kind": a["kind"]} if a else None,
        "defaults": {"sr": TCFG["sample_rate"], "length": "auto", "steps": _lengths()},
    }


def _lengths() -> dict:
    from .trainer import LENGTHS
    return LENGTHS


def handle(h, method: str, p: str) -> bool:
    """Route one request. h: the BaseHTTPRequestHandler. Returns False if the path isn't ours."""
    if method == "GET" and p == "/api/work/candidates":
        from . import night
        h._json(night.candidates())
        return True
    if method == "GET" and p == "/api/work/lookup":
        from urllib.parse import parse_qs, urlparse
        from . import night
        h._json(night.lookup((parse_qs(urlparse(h.path).query).get("q") or [""])[0]))
        return True
    if not p.startswith("/api/train/"):
        return False
    try:
        _route(h, method, p)
    except ModelError as e:
        h._error(str(e))
    except LookupError as e:
        h._error(str(e), 404)
    return True


def _route(h, method, p):
    if p == "/api/train/projects":
        if method == "GET":
            return h._json(P.list_projects())
        if method == "POST":
            b = h._body()
            mode = b.get("mode") if b.get("mode") in ("speaking", "singing", "both") else "speaking"
            proj = P.Project.create(b.get("person", ""), mode)
            if b.get("no_search"):
                return h._json(project_json(proj))
            try:
                from . import sources as source
                proj.sources = source.search(proj.person, mode)
            except ModelError as e:
                proj.save()
                return h._json({**project_json(proj), "warning": str(e)})
            proj.save()
            return h._json(project_json(proj))
    m = re.fullmatch(r"/api/train/voices/([A-Za-z0-9][A-Za-z0-9_-]*)/cadence(?:/(ab|verdict|sample/current|sample/pending))?", p)
    if m:
        return _cadence_ab(h, method, unquote(m.group(1)), m.group(2))
    m = re.fullmatch(r"/api/train/voices/([A-Za-z0-9][A-Za-z0-9_-]*)/improved(?:/(ab|keep|undo))?", p)
    if m:   # an overnight improve's retrained voice: the blind A/B, Keep, Undo (Internal Affairs P2-1)
        from . import improved
        voice, act = unquote(m.group(1)), m.group(2)
        if act is None and method == "GET":
            return h._json({"improved": improved.state(voice)})
        if method != "POST" or act is None:
            raise LookupError("Not found")
        b = h._body()
        if act == "ab":
            return h._json(improved.start_samples(voice))
        if act == "keep":
            return h._json(improved.keep(voice, str(b.get("keep") or "")))
        return h._json(improved.undo(voice))
    m = re.fullmatch(r"/api/train/projects/([a-z0-9-]+)(?:/(\w+))?", p)
    if not m:
        raise LookupError("Not found")
    name, action = m.group(1), m.group(2)
    with P.lock(name):
        proj = P.Project.load(name)
        _heal(proj)
    if action is None:
        if method == "GET":
            return h._json(project_json(proj))
        if method == "DELETE":
            if _busy(name):
                raise ModelError("That project is busy. Stop or cancel it first.")
            proj.delete()
            return h._json({"ok": True})
    if method != "POST":
        raise LookupError("Not found")
    b = {} if action == "upload" else h._body()
    fn = {"search": _search, "sources": _sources, "upload": _upload, "prepare": _prepare, "reference": _reference,
          "phrases": _phrases, "train": _train, "pause": _pause, "stop": _stop, "export": _export,
          "auto": _auto, "transcribe": lambda h, proj, b: h._json({"job": start_transcribe(proj.name).id})}.get(action)
    if fn is None:
        raise LookupError("Not found")
    return fn(h, proj, b)


# ---------------------------------------------------------------- sources

def _search(h, proj, b):
    from . import sources as source
    if _busy(proj.name):
        raise ModelError("Wait for the current step to finish before searching again.")
    person = (b.get("person") or proj.person).strip()
    mode = b.get("mode") if b.get("mode") in ("speaking", "singing", "both") else proj.mode
    found = source.search(person, mode)
    with P.lock(proj.name):
        proj = P.Project.load(proj.name)
        keep = [s for s in proj.sources if s.user or s.status == "ready"]
        ids = {s.id for s in keep}
        proj.sources = keep + [s for s in found if s.id not in ids]
        proj.person, proj.mode = person, mode
        proj.save()
    h._json(project_json(proj))


def _sources(h, proj, b):
    with P.lock(proj.name):
        proj = P.Project.load(proj.name)
        if b.get("url"):
            from . import sources as source
            new = source.from_link(b["url"])
            ids = {s.id for s in proj.sources}
            proj.sources += [s for s in new if s.id not in ids]
        elif b.get("id"):
            s = proj.source(b["id"])
            if "selected" in b:
                s.selected = bool(b["selected"])
            if b.get("remove"):
                if _busy(proj.name):
                    raise ModelError("Wait for the current step to finish before removing a source.")
                proj.sources.remove(s)
                drop = {x.id for x in proj.phrases if x.source == s.id}
                proj.phrases = [x for x in proj.phrases if x.source != s.id]
                for pid in drop:
                    proj.clip_path(pid).unlink(missing_ok=True)
                (proj.dir / "raw" / f"{s.id}.wav").unlink(missing_ok=True)
        proj.save()
    h._json(project_json(proj))


def _upload(h, proj, b):
    n = int(h.headers.get("Content-Length") or 0)
    fname = Path(unquote(h.headers.get("X-Filename") or "recording.wav")).name
    if Path(fname).suffix.lower() not in files.AUDIO_EXT:
        raise ModelError(f"'{fname}' doesn't look like an audio file. Use mp3, wav, flac, m4a or ogg.")
    if not n or n > CONFIG["ui"]["max_upload_mb"] * 1e6:
        raise ModelError("That file is empty or too large.")
    tmp = path("cache_dir") / f"tup_{uuid.uuid4().hex}{Path(fname).suffix.lower()}"
    try:
        with open(tmp, "wb") as f:
            left = n
            while left > 0:
                chunk = h.rfile.read(min(1 << 20, left))
                if not chunk:
                    break
                f.write(chunk)
                left -= len(chunk)
        if left:
            raise ModelError("The upload was interrupted. Add the recording again.")
        with P.lock(proj.name):
            proj = P.Project.load(proj.name)
            P.add_file(proj, tmp, fname)
            proj.save()
    finally:
        tmp.unlink(missing_ok=True)
    h._json(project_json(proj))


# ---------------------------------------------------------------- prepare (GPU job)

def _prepare(h, proj, b):
    h._json({"job": start_prepare(proj.name).id})


def start_prepare(name: str):
    """Prepare the ticked sources of a project (GPU job). Pausing stops after the current source; resuming
    prepares the rest."""
    proj = P.Project.load(name)
    if _busy(proj.name):
        raise ModelError("This project is already busy.")
    todo = [s.id for s in proj.sources if s.selected and s.status != "ready"]
    if not todo and not proj.phrases:
        raise ModelError("Tick at least one source (or add a recording) first.")
    job = jobs.Job("train-prepare")
    jobs.JOBS[job.id] = job
    ACTIVE[proj.name] = {"job": job, "control": None, "kind": "prepare"}

    def run(job):
        from ..core.speaker import SpeakerEmbedder

        def progress(f, label, **kw):
            job.push(stage="matching" if label == "finding the voice" else "prepare", fraction=f, label=label, **kw)
        try:
            failed = P.prepare_sources(proj.name, todo, gpu.MODELS.separator, SpeakerEmbedder(), progress=progress,
                                       cancel=lambda: job.cancel or job.pausing, stop=lambda: job.cancel,
                                       friendly=lambda e: jobs.friendly(e, "preparing a source"))
        except P.Cancelled:
            if job.pausing:
                return job.finish("paused", message="Paused. The rest is prepared when you resume.", project=proj.name)
            raise jobs.Cancelled()
        note = f"Couldn't use: {', '.join(failed)}." if failed else ""
        job.finish("done", message=note, project=proj.name)

    job.resume_spec = {"kind": "prepare", "project": proj.name, "label": f"Preparing clips for {proj.person or proj.name}"}
    jobs.GPU_QUEUE.put((job, run))
    return job


def _reference(h, proj, b):
    with P.lock(proj.name):
        proj = P.Project.load(proj.name)
        if b.get("clip"):
            P.set_reference(proj, clip=b["clip"])
        else:
            P.set_reference(proj, cluster=int(b.get("cluster", -2)))
        proj.save()
    h._json(project_json(proj))


def _phrases(h, proj, b):
    choice = b.get("choice")
    if choice not in (True, False, None):
        raise ModelError("choice must be true, false or null")
    ids = set(b.get("ids") or [])
    with P.lock(proj.name):
        proj = P.Project.load(proj.name)
        for x in proj.phrases:
            if x.id in ids:
                x.choice = choice
        proj.save()
    h._json(project_json(proj))


# ---------------------------------------------------------------- training (GPU job)

def _train(h, proj, b):
    h._json({"job": start_train(proj.name, b).id})


def start_train(name: str, b: dict):
    """Train a project (GPU job). A paused run resumes unless b["fresh"]."""
    from .trainer import LENGTHS, SR_CFG, Control, Trainer
    proj = P.Project.load(name)
    if _busy(proj.name):
        raise ModelError("This project is already busy.")
    other = next((n for n, a in ACTIVE.items() if a["kind"] in ("train", "auto") and not a["job"].finished), None)
    if other:
        raise ModelError(f"'{other}' is training right now. Pause it first; one voice trains at a time.")
    with P.lock(proj.name):
        proj = P.Project.load(proj.name)
        st = proj.train
        resume = st.get("state") == "paused" and not b.get("fresh")
        if resume:
            from .trainer import data_key_for
            if st.get("data_key_trained") and data_key_for(proj, int(st.get("sr") or TCFG["sample_rate"]))[0] != st["data_key_trained"]:
                raise ModelError("The samples changed since training paused, so it can't continue where it left off. "
                                 "Put the samples back the way they were to resume, or press \"Start over instead\".")
        if not resume:
            sr = int(b.get("sr") or TCFG["sample_rate"])
            if sr not in SR_CFG:
                raise ModelError("Sample rate must be 32000, 40000 or 48000.")
            length = b.get("length") if b.get("length") in LENGTHS else "auto"
            vname = slug(b.get("voice_name") or f"{proj.name}-uc")
            if not vname:
                raise ModelError("Give the new voice a name.")
            check_voice_name(proj, vname)
            for k in ("epoch", "best", "saves", "state", "outcome", "epochs"):   # exported stays: the voice is still there
                st.pop(k, None)
            st.update(sr=sr, length=length, voice_name=vname, epochs=None)
            (proj.dir / "train" / "resume.pt").unlink(missing_ok=True)
        st["state"] = "queued"
        proj.save()
    control = Control()
    job = jobs.Job("train")
    jobs.JOBS[job.id] = job
    ACTIVE[proj.name] = {"job": job, "control": control, "kind": "train"}

    def service():
        """Covers queued while training run right away, between training steps (models stay in VRAM)."""
        while not jobs.GPU_QUEUE.empty():
            try:
                other_job, fn = jobs.GPU_QUEUE.get_nowait()
            except Exception:
                return
            job.push(stage="training", note="paused for a cover")
            jobs.run_job(other_job, fn)
    control.service = service

    def run(job):
        conv = gpu.MODELS.converter
        with P.lock(proj.name):
            pr = P.Project.load(proj.name)
        tr = Trainer(pr, conv.hubert, conv.rmvpe, device=str(conv.device), half=conv.half, converter=conv)

        def progress(**ev):
            if job.cancel:
                control.stop = True
            job.push(**ev)
        try:
            st = tr.run(control, progress=progress)
        except BaseException:
            pr.train["state"] = "paused" if pr.train.get("epoch") else "failed"
            pr.save_train()
            raise
        if st.get("state") == "paused":
            return job.finish("paused", message="Training paused. Resume continues from the same point.",
                              train_state="paused", epoch=st.get("epoch"), best=st.get("best"))
        if st.get("state") == "stopped":
            return job.finish("cancelled", message="Training stopped. The saves made before the stop are kept.",
                              train_state="stopped", epoch=st.get("epoch"), best=st.get("best"), outcome="stopped")
        job.finish("done", message=f"Training {st['state']}.", train_state=st["state"], epoch=st.get("epoch"),
                   best=st.get("best"), outcome=st.get("outcome"))

    job.resume_spec = {"kind": "train", "project": proj.name, "label": f"Training {label_of(proj.train.get('voice_name')) or proj.name}"}
    job.control = control
    jobs.GPU_QUEUE.put((job, run))
    return job


def _auto(h, proj, b):
    h._json({"job": start_auto(proj.name, b).id})


def start_auto(name: str, b: dict):
    """Do it all: prepare the ticked sources (and more if short of the target), confirm the main voice,
    train (auto length) and add the best save to the voices. Same steps as a one-shot cover's training."""
    from . import service
    from .trainer import Control
    proj = P.Project.load(name)
    if _busy(proj.name):
        raise ModelError("This project is already busy.")
    if any(a["kind"] in ("train", "auto") and not a["job"].finished for a in ACTIVE.values()):
        raise ModelError("Another voice is training right now. One voice trains at a time.")
    if not proj.sources and not proj.person:
        raise ModelError("Type a name and find clips first.")
    if b.get("voice_name"):
        check_voice_name(proj, slug(b["voice_name"]))
        with P.lock(proj.name):
            pr = P.Project.load(proj.name)
            pr.train["voice_name"] = slug(b["voice_name"])
            pr.save()
    control = Control()
    job = jobs.Job("train-auto")
    jobs.JOBS[job.id] = job
    ACTIVE[proj.name] = {"job": job, "control": control, "kind": "auto"}

    def between():   # not `service`: that name is the module this function calls below ("Do it all" failed with an AttributeError)
        while not jobs.GPU_QUEUE.empty():
            try:
                other, fn = jobs.GPU_QUEUE.get_nowait()
            except Exception:
                return
            jobs.run_job(other, fn)
    control.service = between

    def run(job):
        def push(stage, frac, message, **kw):
            if job.cancel:
                raise jobs.Cancelled()
            job.push(stage=stage, frac=round(frac, 3), message=message, **kw)
        try:
            name = service.train_new_voice(job, proj.person, control, push, project=proj.name)
        except jobs.Paused:
            ACTIVE.pop(proj.name, None)
            return job.finish("paused", message="Paused. Resume continues where it stopped.")
        except BaseException:
            ACTIVE.pop(proj.name, None)
            raise
        gpu.forget_voice(name)
        owner = pending_of(name)   # it landed on a voice that's there: it waits
        done = waiting_note(owner) if owner else f"{label_of(name)} is ready in your voices."
        # a voice made from speech goes on to its cadence (principle 10: its own manner, not a Reader's accent)
        try:   # a Pause or Stop in it ends the job done (the voice is made): cadence.chain_cadence
            from . import cadence
            c = cadence.chain_cadence(job, name, push, control)
        finally:
            ACTIVE.pop(proj.name, None)
        job.finish("done", message=f"{done} {c['note']}" if c else done, voice=name, pending_for=owner,
                   cadence=c)

    try:   # its time left is the whole voice's (P8-10: "about 6 min left" was the download step's), the queue's estimator
        from .night import estimate
        est = estimate({"kind": "new", "target": proj.person or proj.name,
                        **({"mode": "speaking"} if proj.mode == "speaking" else {})})
        job.planned_s = est["total"] * 60.0
        job.after_eta_s = max(0.0, est["total"] - est.get("prepare", 0) - est.get("train", 0)) * 60.0
    except Exception:   # noqa: BLE001 - extrapolated from its progress instead
        pass
    job.resume_spec = {"kind": "auto", "project": proj.name, "label": f"Making a voice of {proj.person or proj.name}"}
    job.control = control
    jobs.GPU_QUEUE.put((job, run))
    return job


def _pause(h, proj, b):
    """Pause this project's work and hold it (the queue doesn't start it again by itself; Resume does)."""
    from ..core import worklist
    a = _busy(proj.name)
    if not a:
        raise ModelError("This voice isn't training right now.")
    if a["control"]:
        a["control"].pause = True   # (hold_one finds it through the job too; set here first for a job without one)
        a["job"].control = a["job"].control or a["control"]
    worklist.hold_one(a["job"])
    h._json({"ok": True})


def pending_of(name: str) -> str | None:
    """The voice a just-made voice waits beside (it landed on one that was there: Internal Affairs P4-1), else None."""
    from ..core.voices import get_voice
    try:
        return (get_voice(name).extra or {}).get("pending_for")
    except ModelError:
        return None


def waiting_note(voice: str) -> str:
    """What a training that landed on an existing voice says: the new one waits beside it (Internal Affairs P4-1)."""
    return (f"{label_of(voice)} was already in your voices, so the new one waits beside it. Listen to both on its "
            "voice card (DoubleAgent) and keep the one you like; the other is set aside, not deleted.")


def check_voice_name(proj, vname: str, replace: str | None = None):
    """A training never writes over another person's voice: a name already taken by a voice that isn't this project's
    is refused here, before any work (export refuses it too). replace: the one voice this run may replace on purpose
    (an overnight improve's own voice, a downloaded one included)."""
    from ..core.voices import label_of, pending_candidate, voices_dir
    owner = pending_candidate(vname)
    if owner:   # an unheard retrained voice (Internal Affairs P3-2): not even this project's own run writes over it
        raise ModelError(f"\"{vname}\" is the retrained {label_of(owner)} waiting to be heard. Give this training its own "
                         "name, or keep or set aside the waiting one first (DoubleAgent, its voice card).")
    if replace and vname == replace:
        return
    try:
        here = json.loads((voices_dir() / vname / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        here = {}
    if (here.get("extra") or {}).get("improved_pending"):   # its new one would wait beside it, and one already does
        raise ModelError(f"One is already waiting: a retrained {label_of(vname)} waits beside it. Listen to the two and "
                         "keep one first (DoubleAgent, its voice card); a new one can join it after that.")
    d = voices_dir() / vname
    meta = d / "meta.json"
    if not meta.exists():
        if d.exists():   # a folder of that name that isn't a whole voice (an interrupted install): not ours to replace
            raise ModelError(f"There's already a folder called \"{vname}\" in your voices. Give this one its own name.")
        return
    try:
        ex = (json.loads(meta.read_text(encoding="utf-8")).get("extra") or {})
    except (OSError, ValueError):
        ex = {}
    if ex.get("project") != proj.name:
        who = ex.get("person") or "another voice"
        raise ModelError(f"\"{vname}\" is already {who}'s voice. Give this one its own name "
                         f"(for example {proj.name}-uc).")


def _stop(h, proj, b):
    a = _busy(proj.name)
    if a:   # every kind of project job stops now (prepare and transcribe have no trainer control: the job's own flag)
        a["job"].cancel = True
        if a["control"]:
            a["control"].stop = True
        return h._json({"ok": True})
    with P.lock(proj.name):
        proj = P.Project.load(proj.name)
        if proj.train.get("state") == "paused":   # stopping a paused run: keep what's saved
            proj.train["state"] = "trained"
            proj.train["outcome"] = "stopped"
            proj.save()
    h._json(project_json(proj))


def _export(h, proj, b):
    from .trainer import export_voice
    if _busy(proj.name):
        raise ModelError("Wait for training to finish (or stop it) before adding the voice.")
    if not proj.train.get("saves"):
        raise ModelError("There's nothing to add yet. Train first.")
    job = jobs.Job("train-export", title=f"Adding the voice from {proj.person or proj.name}")
    jobs.JOBS[job.id] = job
    ACTIVE[proj.name] = {"job": job, "control": None, "kind": "export"}

    def run():
        try:
            job.push(stage="export", fraction=0.2, label="building the search index")
            with P.lock(proj.name):
                pr = P.Project.load(proj.name)
            # replace: the user's "Replace my voice" confirm, just now on the page (the old one is set aside, Undo
            # puts it back). Without it, a voice that's already there isn't replaced: the new one waits beside it
            replace = b.get("replace") is True
            v = export_voice(pr, epoch=b.get("epoch"), name=b.get("name"), overwrite=bool(b.get("overwrite")) or replace,
                             confirmed=replace)
            gpu.forget_voice(v.name)   # reload if an older version was cached
            owner = (v.extra or {}).get("pending_for")
            job.finish("done", message=waiting_note(owner) if owner else f"{label_of(v.name)} is ready in your voices.",
                       voice=v.name, pending_for=owner)
        except Exception as e:
            job.finish("error", message=jobs.friendly(e, "adding the voice"))

    threading.Thread(target=run, daemon=True).start()
    h._json({"job": job.id})


def start_transcribe(name: str):
    """Transcripts of a project's kept speech (cadence.py): GPU job, pausable; resuming does the clips left."""
    from . import cadence
    proj = P.Project.load(name)
    if _busy(proj.name):
        raise ModelError("This project is already busy.")
    job = jobs.Job("train-transcribe", title=f"Transcribing {proj.person or proj.name}")
    jobs.JOBS[job.id] = job
    ACTIVE[proj.name] = {"job": job, "control": None, "kind": "transcribe"}

    def run(job):
        def check():
            if job.cancel:
                raise jobs.Cancelled()
            if job.pausing:
                raise jobs.Paused()
        r = cadence.transcribe(proj.name, lambda f, m, **kw: job.push(stage="transcribe", fraction=round(f, 3), label=m, **kw), check)
        job.finish("done", message=f"{r['clips']} of {r['of']} clips transcribed ({r['minutes']} min of speech).",
                   project=proj.name, result=r)

    job.resume_spec = {"kind": "transcribe", "project": proj.name, "label": f"Transcribing {proj.person or proj.name}"}
    jobs.GPU_QUEUE.put((job, run))
    return job


# ---------------------------------------------------------------- "Keep this one" (phase 9)

def _cadence_ab(h, method, voice: str, action):
    from . import cadence
    if action is None and method == "GET":
        return h._json(cadence.ab_state(voice))
    if action and action.startswith("sample/") and method == "GET":
        # with Range (206) and Accept-Ranges like /files: a phone's player seeks (Internal Affairs P2-3)
        return h._send_file(cadence.sample_file(voice, action.split("/")[1]), cache="no-store")
    if method != "POST":
        raise LookupError("Not found")
    b = h._body()   # always read: an unread body would be taken for the start of the next request on this connection
    if action == "ab":
        return h._json(start_ab(voice))
    if action == "verdict":
        return h._json(cadence.verdict(voice, str(b.get("keep") or ""), str(b.get("why") or "")))
    raise LookupError("Not found")


def start_ab(voice: str) -> dict:
    """Both packs read the A/B line (a GPU job), unless both readings are already there."""
    from . import cadence
    st = cadence.ab_state(voice)
    if not (st["current"] and st["pending"]):
        raise ModelError(f"{st['display']} has no new cadence waiting to compare.")
    if st["ready"]:
        return {"ready": True, "state": st}
    running = next((j for j in list(jobs.JOBS.values()) if j.kind == "cadence-ab" and getattr(j, "voice", None) == st["voice"]
                    and not j.finished), None)
    if running:
        return {"job": running.id, "state": st}
    job = jobs.Job("cadence-ab", title=f"Listening copies: {st['display']}")
    job.voice = st["voice"]
    jobs.JOBS[job.id] = job

    def run(job):
        r = cadence.make_samples(job, st["voice"])
        job.finish("done", message="Both readings are ready: listen and keep the one you like.", result=r)

    jobs.GPU_QUEUE.put((job, run))
    return {"job": job.id, "state": st}


# Pause / Resume (core.worklist): these jobs are voice work; paused ones start again from their resume spec
worklist.register_pausable("train", "train-prepare", "train-auto", "train-transcribe")
def _as_project(spec: dict):
    """Held project work as an overnight item: the project with its own choices ("Train tonight instead")."""
    return ("project", spec["project"]) if spec.get("project") else None


def _as_cadence(spec: dict):
    """A held transcription as its voice's overnight cadence item (only once the project's voice exists)."""
    try:
        voice = P.Project.load(spec["project"]).train.get("exported")
    except (ModelError, KeyError):
        return None
    return ("cadence", voice) if voice else None


worklist.register_resume("transcribe", lambda spec: start_transcribe(spec["project"]), queue_as=_as_cadence)
worklist.register_resume("prepare", lambda spec: start_prepare(spec["project"]), queue_as=_as_project)
worklist.register_resume("train", lambda spec: start_train(spec["project"], {}), queue_as=_as_project)
worklist.register_resume("auto", lambda spec: start_auto(spec["project"], {}), queue_as=_as_project)
worklist.register_control_lookup(lambda j: next((a["control"] for a in ACTIVE.values() if a["job"] is j and a.get("control")), None))
