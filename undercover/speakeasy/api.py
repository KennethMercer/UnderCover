"""SpeakEasy's HTTP routes. The app's server routes /api/speak/... here.

GET    /api/speak/voices                     every voice: group, likeness, auto Reader + pitch, cadence status
GET    /api/speak/readers                    the Kokoro Readers (English)
GET    /api/speak/plan?voice=&reader=&speed=&nudge=   which Reader and pitch a voice would use
GET    /api/speak/recent                     saved speech, newest first
POST   /api/speak                            {text, voice, reader, speed, nudge, cadence} -> speech job (events: one URL per
                                             sentence; cadence false = the Reader even when the voice has a cadence)
POST   /api/speak/cadence                    {voice, now} "Train cadence" / "Find clips + cadence" (DoubleAgent's queue)
POST   /api/speak/newvoice                   {person, now} a new voice with its cadence (DoubleAgent's full pipeline)
POST   /api/speak/<id>/rename                {name}
DELETE /api/speak/<id>
"""
import re
from urllib.parse import parse_qs, urlparse

from ..core.jobs import GPU_QUEUE, JOBS, Job
from ..core.voices import ModelError
from ..doubleagent import service
from . import speak, tts


def handle(h, method: str, p: str) -> bool:
    if p != "/api/speak" and not p.startswith("/api/speak/"):
        return False
    try:
        _route(h, method, p)
    except (ModelError, speak.SpeakError) as e:
        h._error(str(e))
    except LookupError as e:
        h._error(str(e), 404)
    return True


def _route(h, method, p):
    q = {k: v[0] for k, v in parse_qs(urlparse(h.path).query).items()}
    rest = p[len("/api/speak"):].strip("/")
    if method == "GET":
        if rest == "voices":
            return h._json(speak.voices())
        if rest == "readers":
            return h._json(speak.readers())
        if rest == "recent":
            return h._json(speak.list_speech())
        if rest == "plan":
            return h._json(speak.plan(q.get("voice", ""), q.get("reader", "auto"), _num(q.get("speed"), 1.0), _num(q.get("nudge"), 0.0),
                                      False if q.get("cadence") in ("0", "false") else None))
        raise LookupError("Not found")
    if method == "POST" and rest == "":
        return _start(h, h._body())
    if method == "POST" and rest == "cadence":
        b = h._body()
        return h._json(service.start_cadence(b.get("voice") or "", now=b.get("now", True) is not False))
    if method == "POST" and rest == "newvoice":
        b = h._body()
        return h._json(service.start_new_voice(b.get("person") or "", cadence=True, now=b.get("now", True) is not False))
    m = re.fullmatch(r"([0-9a-f]{10})(?:/(rename))?", rest)
    if m and method == "POST" and m.group(2) == "rename":
        return h._json(speak.rename_speech(m.group(1), h._body().get("name", "")))
    if m and method == "DELETE" and not m.group(2):
        speak.delete_speech(m.group(1))
        return h._json({"ok": True})
    raise LookupError("Not found")


def _num(v, default: float) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _start(h, body: dict):
    text = (body.get("text") or "").strip()
    if not text:
        raise speak.SpeakError("Type or paste something to say first.")
    if len(text) > speak.MAX_CHARS:
        raise speak.SpeakError(f"That's {len(text):,} characters; SpeakEasy reads up to {speak.MAX_CHARS:,} at a time. Split it into parts.")
    args = dict(text=text, voice=body.get("voice") or "", reader=body.get("reader") or "auto",
                speed=_num(body.get("speed"), 1.0), nudge=_num(body.get("nudge"), 0.0),
                cadence=False if body.get("cadence") is False else None)
    p = speak.plan(args["voice"], args["reader"], args["speed"], args["nudge"], args["cadence"])   # a bad voice/Reader fails now
    job = Job("speech", title=f"{text[:40].strip()}{'…' if len(text) > 40 else ''} · {args['voice']}")
    JOBS[job.id] = job

    def run(job: Job):
        item = speak.make_speech(job, **args)
        job.finish("done", message="Finished", result=item)

    GPU_QUEUE.put((job, run))
    h._json({"job": job.id, "position": GPU_QUEUE.qsize(), "plan": p, "sentences": len(tts.sentences(text)),
             "estimate": round(tts.seconds_estimate(text, p["speed"]), 1)})
