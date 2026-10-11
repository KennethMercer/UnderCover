"""FrontMan's HTTP routes (the Covers tab): songs, covers, previews and the one-shot. The app's server routes here.

GET    /api/config                          the page's default cover options (config.toml)
GET    /api/history                         covers made so far
GET    /api/songs/search?q=                 YouTube song search
GET    /api/songs/<id> | /api/songs/<id>/key
GET    /api/auto/plan?text=&train=          what a one-shot would do (nothing downloaded)
POST   /api/upload                          raw body + X-Filename: a song dropped on the page
POST   /api/songs/youtube                   {url} or a search result -> fetched into uploads/
POST   /api/songs/<id>/presep               separate in the background while the GPU is idle
POST   /api/estimate | /api/covers | /api/preview
POST   /api/auto                            {text, prefer_train, song_url} -> one-shot job
"""
import hashlib
import re
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from ..core.config import CONFIG, path
from ..core.files import AUDIO_EXT, file_url
from ..core.jobs import GPU_QUEUE, JOBS, Cancelled, Job
from .pipeline import ENGINE, cancel_presep as _cancel_presep, history_items
from .songs import register_song, song_meta

UCFG = CONFIG["ui"]


def handle(h, method: str, p: str) -> bool:
    """Route one request. h: the BaseHTTPRequestHandler. Returns False if the path isn't ours."""
    if method == "GET":
        q = parse_qs(urlparse(h.path).query)
        if p == "/api/config":
            h._json(_ui_defaults())
        elif p == "/api/history":
            h._json(history_items())
        elif p == "/api/songs/search":
            from ..core.voices import ModelError
            from . import songs
            try:
                h._json([r.to_json() for r in songs.search((q.get("q") or [""])[0])])
            except ModelError as e:
                h._error(str(e))
        elif p == "/api/auto/plan":
            _auto_plan(h, (q.get("text") or [""])[0], (q.get("train") or ["0"])[0] == "1")
        elif m := re.fullmatch(r"/api/songs/([0-9a-f]{16})/key", p):
            from .pipeline import song_key
            h._json(song_key(path("uploads_dir") / song_meta(m.group(1))["file"]))
        elif m := re.fullmatch(r"/api/songs/([0-9a-f]{16})", p):
            h._json(song_meta(m.group(1)))
        else:
            return False
        return True
    if method == "POST":
        if p == "/api/upload":
            _upload(h)
        elif p == "/api/estimate":
            _estimate(h, h._body())
        elif p == "/api/covers":
            _start_cover(h, h._body())
        elif p == "/api/preview":
            _start_preview(h, h._body())
        elif p == "/api/songs/youtube":
            _song_from_youtube(h, h._body())
        elif m := re.fullmatch(r"/api/songs/([0-9a-f]{16})/presep", p):
            _presep(h, m.group(1), h._body())
        elif p == "/api/auto":
            _start_auto(h, h._body())
        else:
            return False
        return True
    return False


def _upload(h):
    n = int(h.headers.get("Content-Length") or 0)
    name = unquote(h.headers.get("X-Filename") or "song")
    ext = Path(name).suffix.lower()
    if ext not in AUDIO_EXT:
        return h._error(f"'{name}' doesn't look like an audio file. Use mp3, wav, flac, m4a or ogg.")
    if not n:
        return h._error("The file was empty.")
    if n > UCFG["max_upload_mb"] * 1e6:
        return h._error(f"That file is over {UCFG['max_upload_mb']} MB.")
    up = path("uploads_dir")
    tmp = up / f"upload_{uuid.uuid4().hex}.part"
    sha = hashlib.sha256()
    try:
        with open(tmp, "wb") as f:
            left = n
            while left > 0:
                chunk = h.rfile.read(min(1 << 20, left))
                if not chunk:
                    break
                sha.update(chunk)
                f.write(chunk)
                left -= len(chunk)
        if left:
            return h._error("The upload was interrupted. Drop the file again.")
        try:
            meta = register_song(tmp, name, sha.hexdigest())
        except ValueError as e:
            return h._error(str(e))
        h._json(meta)
    finally:
        tmp.unlink(missing_ok=True)   # moved into place by register_song when it worked


def _estimate(h, body: dict):
    from .pipeline import estimate
    meta = song_meta(body.get("song", ""))
    opts = _options(body)
    h._json(estimate(path("uploads_dir") / meta["file"], opts))


def _start_cover(h, body: dict):
    from ..core.voices import get_voice
    meta = song_meta(body.get("song", ""))
    opts = _options(body)
    get_voice(opts.voice)  # friendly error now rather than inside the job
    if opts.backing_voice:
        get_voice(opts.backing_voice)
    song = path("uploads_dir") / meta["file"]
    _cancel_presep(keep=(meta["id"], opts.quality, bool(opts.dereverb), bool(opts.karaoke or opts.backing_voice)))
    job = Job("cover", title=f"{meta['title']} · {opts.voice}")
    JOBS[job.id] = job

    def run(job: Job):
        from .pipeline import estimate
        est = estimate(song, opts)
        order = ["separating", "converting", "mixing"]
        t0 = time.perf_counter()
        stage_start = {"stage": None, "t": t0}

        def progress(stage, frac, msg):
            if job.cancel:
                raise Cancelled()
            now = time.perf_counter()
            if stage != stage_start["stage"]:
                stage_start.update(stage=stage, t=now)
            if stage == "done":
                return
            in_stage = now - stage_start["t"]
            left_stage = (in_stage / frac * (1 - frac)) if frac > 0.05 else max(est[stage] - in_stage, 0)
            later = sum(est[s] for s in order[order.index(stage) + 1:])
            job.push(stage=stage, frac=round(frac, 3), message=msg, elapsed=round(now - t0, 1),
                     stage_left=round(left_stage, 1), total_left=round(left_stage + later, 1))

        job.push(stage="queued", frac=0, message="Starting", estimate=est, elapsed=0)
        res = ENGINE.make_cover(song, opts, progress=progress, title=meta["title"])
        hist = history_items()
        item = next((h for h in hist if h["output_name"] == res.output.name), None)
        job.finish("done", message="Finished", result=item, seconds=res.seconds, notes=res.notes,
                   elapsed=round(time.perf_counter() - t0, 1))

    GPU_QUEUE.put((job, run))
    h._json({"job": job.id, "position": GPU_QUEUE.qsize()})


def _presep(h, song_id: str, body: dict):
    """Separate a just-loaded song in the background with the page's saved settings, so the first cover skips
    that step (review C3). Only when the GPU is idle; a cover of another song, or with other separation settings,
    cancels it."""
    meta = song_meta(song_id)
    o = body.get("options") or {}
    want = (song_id, o.get("quality") if o.get("quality") in ("fast", "high", "max") else None,
            bool(o.get("dereverb")), bool(o.get("karaoke") or o.get("backing_voice")))
    busy = GPU_QUEUE.qsize() or any(not j.finished for j in JOBS.values() if j.kind != "presep")
    if busy or any(not j.finished and getattr(j, "presep", None) == want for j in JOBS.values()):
        return h._json({"started": False})
    job = Job("presep")
    job.presep = want
    JOBS[job.id] = job

    def run(job: Job):
        from ..core.separate import separate

        def progress(frac, step):
            if job.cancel:
                raise Cancelled()
        separate(path("uploads_dir") / meta["file"], dereverb=want[2], karaoke=want[3],
                 separator=ENGINE.separator, quality=want[1], progress=progress)
        job.finish("done", message="Stems ready")
    GPU_QUEUE.put((job, run))
    h._json({"started": True, "job": job.id})


def _song_from_youtube(h, body: dict):
    """Fetch a song from YouTube (a search result or a pasted link) into uploads/, like a dropped file."""
    from ..core.voices import ModelError
    from . import songs
    try:
        if body.get("url") and not body.get("id"):
            r = songs.from_link(body["url"])
        else:
            r = songs.SongResult(**{k: body[k] for k in ("id", "url", "title", "raw_title", "channel", "duration")})
        h._json(songs.download(r, register_song))
    except (ModelError, ValueError, KeyError, TypeError) as e:
        h._error(str(e) if not isinstance(e, (KeyError, TypeError)) else "That song result is incomplete. Search again.")


def _auto_plan(h, text: str, prefer_train: bool):
    """What a one-shot request would do, without downloading anything (shown before it starts)."""
    from ..core.voices import ModelError, slug
    from . import oneshot
    from ..doubleagent import service
    try:
        req = oneshot.parse_request(text)
    except ModelError as e:
        return h._error(str(e))
    song, person = req["song"], req["person"]
    inst = service.installed_for(person)
    trained = [v for v in inst if v.extra.get("trained")]
    if trained:
        voice = {"how": "installed", "name": trained[0].name, "trained": True}
    else:
        online = [] if prefer_train else service.online_for(person, 2)
        community = [] if prefer_train else [v.name for v in inst]
        voice = ({"how": "check", "candidates": community + [r.name for r in online], "name": f"{slug(person)}-uc"}
                 if community or online else {"how": "train", "name": f"{slug(person)}-uc"})
    h._json({"song_query": song, "person": person, "style": req["style"], "voice": voice})


def _start_auto(h, body: dict):
    from ..core.voices import ModelError
    try:
        from .oneshot import start_oneshot
        job = start_oneshot(body)
    except ModelError as e:
        return h._error(str(e), 409 if "already running" in str(e) else 400)
    h._json({"job": job.id, "position": GPU_QUEUE.qsize()})


def _start_preview(h, body: dict):
    from ..core.voices import get_voice
    meta = song_meta(body.get("song", ""))
    opts = _options(body)
    get_voice(opts.voice)
    variants = body.get("variants") or []
    if not 1 <= len(variants) <= 6:
        return h._error("Pick between 1 and 6 versions to preview.")
    song = path("uploads_dir") / meta["file"]
    job = Job("cover", title=f"Preview: {meta['title']} · {opts.voice}")  # same GPU queue as covers
    JOBS[job.id] = job

    def run(job: Job):
        def progress(stage, frac, msg):
            if job.cancel:
                raise Cancelled()
            job.push(stage=stage, frac=round(frac, 3), message=msg)
        res = ENGINE.make_preview(song, opts, variants, progress=progress)
        job.finish("done", message="Previews ready",
                   previews=[{"label": r["label"], "url": file_url(r["file"]), "options": r["options"]} for r in res])

    GPU_QUEUE.put((job, run))
    h._json({"job": job.id})


def _options(body: dict):
    from .pipeline import CoverOptions
    o = body.get("options") or {}
    num = lambda k: float(o[k]) if o.get(k) not in (None, "") else None
    fmt = o.get("format")
    if fmt not in (None, "mp3", "flac", "wav"):
        fmt = None
    q = o.get("quality") if o.get("quality") in ("fast", "high", "max") else None
    voice = body.get("voice") or ""
    if not voice:
        raise LookupError("Pick a voice first.")
    octave = int(num("octave")) if num("octave") is not None else None
    boct = int(num("backing_octave")) if num("backing_octave") is not None else None
    bvoice = o.get("backing_voice") or None
    return CoverOptions(voice=voice, key=num("key"), octave=octave, index_ratio=num("index_ratio"), protect=num("protect"),
                        volume_envelope=num("volume_envelope"), dereverb=bool(o.get("dereverb")),
                        karaoke=bool(o.get("karaoke")), quality=q, format=fmt,
                        reverb=None if o.get("reverb") in (None, "", "auto") else num("reverb"),
                        backing_voice=bvoice, backing_octave=boct, backing_db=num("backing_db"),
                        pitch_cleanup=o.get("pitch_cleanup", True) is not False,
                        pitch_correction=num("pitch_correction"),
                        polish=o.get("polish", True) is not False, fit=bool(o.get("fit")))


def _ui_defaults() -> dict:
    c, r, s = CONFIG["conversion"], CONFIG["remix"], CONFIG["separation"]
    return {"key": c["key"], "octave": c["octave"], "index_ratio": c["index_ratio"], "protect": c["protect"],
            "volume_envelope": c["volume_envelope"], "quality": s["quality"], "dereverb": s["dereverb"],
            "karaoke": s["karaoke"], "reverb": r["reverb"], "format": r["format"],
            "backing_voice": "", "backing_octave": 0, "backing_db": r["backing_db"],
            "pitch_cleanup": c["pitch_cleanup"], "pitch_correction": c["pitch_correction"], "polish": r["polish"],
            "fit": False}
