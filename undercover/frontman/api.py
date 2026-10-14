"""FrontMan's HTTP routes (the Covers tab): songs, covers, previews and the one-shot. The app's server routes here.

GET    /api/config                          the page's default cover options (config.toml)
GET    /api/history                         the recent tray: the latest 15 finished covers, newest first (kept or not;
                                            each has id, kept, kept_at). Keep/unkeep: POST /api/library/covers/<id>/keep|unkeep
GET    /api/songs/search?q=                 YouTube song search
GET    /api/songs/<id> | /api/songs/<id>/key
GET    /api/auto/plan?text=&train=          what a one-shot would do (nothing downloaded)
POST   /api/upload                          raw body + X-Filename: a song dropped on the page
POST   /api/songs/youtube                   {url} or a search result -> fetched into uploads/
POST   /api/songs/<id>/presep               separate in the background while the GPU is idle
POST   /api/estimate | /api/covers | /api/preview
POST   /api/history/<id>/octaves            {variants: [{octave, key}, {octave, key}]} 15 s previews of the cover at
                                            each (the one-shot's plan.settings_fit.alt: two octaves that fit about as
                                            well, for a quick blind pick; the pick then goes to /again) -> {job}
POST   /api/history/<id>/again              {options} the cover made again: its song, voice and settings, with these
                                            changed (the ear's fix: {octave: -1}; the backing follows the lead's octave)
POST   /api/auto                            {text, prefer_train, song_url, choice} -> one-shot job (choice: the song_url picked from the plan's `choices`)
"""
import hashlib
import re
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from ..core.config import CONFIG, path
from ..core.files import AUDIO_EXT, file_url
from ..core.voices import label_of
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
            _auto_plan(h, (q.get("text") or [""])[0], (q.get("train") or ["0"])[0] == "1", (q.get("subject") or [""])[0] or None)
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
        elif m := re.fullmatch(r"/api/history/([0-9a-f]{10})/again", p):
            _cover_again(h, m.group(1), h._body())
        elif m := re.fullmatch(r"/api/history/([0-9a-f]{10})/octaves", p):
            _cover_octaves(h, m.group(1), h._body())
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
    job = Job("cover", title=f"{meta['title']} · {label_of(opts.voice)}")
    job.voices = {v for v in (opts.voice, opts.backing_voice) if v}   # what it reads with (jobs.jobs_using)
    JOBS[job.id] = job
    try:   # its own time after the song's preparation (which does the separating), added to that wait
        from .pipeline import estimate
        e = estimate(song, opts)
        job.own_estimate = e["converting"] + e["mixing"]
    except Exception:   # noqa: BLE001 - the wait is still said, without this part
        job.own_estimate = 0
    _say_why_waiting(job, meta["id"])

    def run(job: Job):
        from .pipeline import Countdown, estimate
        est = estimate(song, opts)
        clock = Countdown(est)
        t0 = time.perf_counter()

        def progress(stage, frac, msg):
            if job.cancel:
                raise Cancelled()
            if stage == "done":
                return
            left = clock.left(stage, frac)
            job.push(stage=stage, frac=round(frac, 3), message=msg, elapsed=round(time.perf_counter() - t0, 1),
                     **({"stage_left": left[0], "total_left": left[1]} if left else {}))

        job.push(stage="queued", frac=0, message="Starting", estimate=est, elapsed=0)
        res = ENGINE.make_cover(song, opts, progress=progress, title=meta["title"])
        hist = history_items()
        item = next((h for h in hist if h["id"] == res.id), None)
        job.finish("done", message="Finished", result=item, seconds=res.seconds, notes=res.notes,
                   elapsed=round(time.perf_counter() - t0, 1))

    GPU_QUEUE.put((job, run))
    h._json({"job": job.id, "position": GPU_QUEUE.qsize()})


AGAIN_KEYS = ("octave", "key")   # what a "make it again" may change (the ear's fixes)


def _cover_again(h, item_id: str, body: dict):
    """Make a finished cover again with its own song, voice and settings, `options` changed (only AGAIN_KEYS). When
    the backing was at the lead's octave, it moves with it."""
    from .history import get
    it = get(item_id)   # LookupError when it's gone
    old = dict(it.get("options") or {})
    change = {k: v for k, v in (body.get("options") or {}).items() if k in AGAIN_KEYS}
    opts = {**old, **change}
    if "octave" in change and old.get("backing_octave") == old.get("octave"):
        opts["backing_octave"] = change["octave"]
    _start_cover(h, {"song": Path(it.get("song") or "").stem, "voice": opts.pop("voice", None) or it.get("voice"),
                     "options": opts})


def _cover_octaves(h, item_id: str, body: dict):
    """Short previews of a finished cover at two octave / key settings (phase 22: when two octaves fit a voice about as
    well, the user picks by ear). Only octave and key change; the backing follows the lead's octave as in /again."""
    from .history import get
    it = get(item_id)
    variants = []
    old = dict(it.get("options") or {})
    for v in (body.get("variants") or [])[:2]:
        change = {k: int(v[k]) for k in AGAIN_KEYS if k in v}
        if "octave" in change and old.get("backing_octave") == old.get("octave"):
            change["backing_octave"] = change["octave"]
        variants.append({"label": f"octave {change.get('octave', old.get('octave', 0)):+d}, key {change.get('key', old.get('key', 0)):+d}",
                         **change})
    if len(variants) != 2:
        return h._error("Give the two octave / key settings to compare.")
    opts = {k: v for k, v in old.items() if k != "voice"}
    _start_preview(h, {"song": Path(it.get("song") or "").stem, "voice": old.get("voice") or it.get("voice"),
                       "options": opts, "variants": variants})


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
    job = Job("presep", title=f"Preparing {meta['title']}")
    job.presep = want
    job.followers = []   # covers / previews of this song queued behind it: they show its steps
    JOBS[job.id] = job

    def run(job: Job):
        from .pipeline import CoverOptions, estimate
        from ..core.separate import separate
        steps = ["main"] + (["karaoke"] if want[3] else []) + (["dereverb"] if want[2] else [])
        song = path("uploads_dir") / meta["file"]
        from .pipeline import blend_left
        try:
            est = estimate(song, CoverOptions(voice="", quality=want[1], dereverb=want[2], karaoke=want[3]))["separating"]
        except Exception:   # noqa: BLE001 - extrapolated from progress instead
            est = None
        t0 = time.perf_counter()

        def progress(frac, step):
            if job.cancel:
                raise Cancelled()
            elapsed = time.perf_counter() - t0
            left = (blend_left(est, elapsed, frac) if est else
                    (elapsed / frac * (1 - frac)) if frac > 0.05 else None)
            n = steps.index(step) + 1 if step in steps else 1
            ev = {"stage": "separating", "fraction": round(frac, 3), "label": PRESEP_LABELS.get(step, "Separating the vocal"),
                  "step": step, "step_n": n, "steps": len(steps)}
            if left is not None:
                ev["total_left"] = round(left, 1)
            job.push(**ev)
            for f in list(getattr(job, "followers", [])):
                if not f.finished and f.state == "queued":
                    f.push(stage="preparing", fraction=ev["fraction"], step_n=n, steps=len(steps), waits_for=job.id,
                           label=f"Preparing the song first ({n} of {len(steps)})…",
                           **({"total_left": round(left + (getattr(f, "own_estimate", 0) or 0), 1)} if left is not None else {}))
        progress(0.0, steps[0])
        separate(song, dereverb=want[2], karaoke=want[3], separator=ENGINE.separator, quality=want[1],
                 progress=progress, one_voice_if_no_lead=True)
        job.finish("done", message="Stems ready")
    GPU_QUEUE.put((job, run))
    h._json({"started": True, "job": job.id})


PRESEP_LABELS = {"main": "Separating the vocal", "karaoke": "Splitting lead and backing", "dereverb": "Removing reverb"}


def _say_why_waiting(job: Job, song_id: str):
    """A cover or preview that has to wait says why: behind its own song's preparation (and follows its steps: the
    presep pushes them on), or behind other work by name. Never a bare "starting" when the reason is known."""
    from ..core import jobs as J
    pre = next((j for j in list(JOBS.values()) if j.kind == "presep" and not j.finished and not j.cancel
                and (getattr(j, "presep", None) or (None,))[0] == song_id), None)
    if pre is not None:
        pre.followers = getattr(pre, "followers", [])
        pre.followers.append(job)
        last = next((e for e in reversed(pre.events) if e.get("stage") == "separating"), {})
        n, steps = last.get("step_n", 1), last.get("steps", 1)
        job.push(stage="preparing", fraction=last.get("fraction", 0), step_n=n, steps=steps, waits_for=pre.id,
                 label=f"Preparing the song first ({n} of {steps})…",
                 **({"total_left": round(last["total_left"] + (getattr(job, "own_estimate", 0) or 0), 1)}
                    if last.get("total_left") is not None else {}))
        return
    cur = J.CURRENT[0]
    if cur is not None and not cur.finished:
        job.push(stage="queued", fraction=0, waits_for=cur.id, label=f"Waiting for: {cur.title or cur.kind}")


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


def _auto_plan(h, text: str, prefer_train: bool, subject: str | None = None):
    """What a one-shot request would do, without downloading anything (shown before it starts). An ambiguous bare
    title answers `ask: true` with 2-3 `choices` ({artist, year, title, song_url}) instead of silently picking; while
    an earlier answer is in effect (until a different song is requested) it comes back as `chosen` + `others`."""
    from ..core.voices import ModelError, display_name, slug
    from . import ambiguity, oneshot
    from ..doubleagent import service
    try:
        req = ambiguity.settle(oneshot.parse_request(text))
    except ModelError as e:
        return h._error(str(e))
    song, person, artist = req["song"], req["person"], req["artist"]
    inst = service.installed_for(person)
    trained = [v for v in inst if v.extra.get("trained")]
    if trained:
        q = service.voice_quality(trained[0])
        voice = {"how": "installed", "name": trained[0].name, "display": display_name(trained[0]), "trained": True,
                 "quality": service.train_note(trained[0], q) if prefer_train else q}   # said before anything starts (phase 15)
    else:
        online = [] if prefer_train else service.online_for(person, 2)
        community = [] if prefer_train else inst
        voice = ({"how": "check", "candidates": [display_name(v) for v in community] + [r.name for r in online],
                  "candidate_ids": [v.name for v in community] + [r.name for r in online], "name": f"{slug(person)}-uc",
                  "display": person}
                 if community or online else {"how": "train", "name": f"{slug(person)}-uc", "display": person})
    out = {"song_query": f"{song} by {artist}" if artist else song, "song": song, "artist": artist, "person": person,
           "style": req["style"], "voice": voice, "ask": False}
    if voice["how"] == "installed":   # from the measured speeds (timings.jsonl), not a fixed "1-3 min"
        out["eta_s"] = oneshot.cover_eta(voice["name"], oneshot.library_song(song, artist))
    if voice["how"] != "installed" or not voice["quality"]["ok"] or subject:
        # what the request names, in plain words (phase 15): a person, a singer, a character, a band; asked when it
        # can't tell ("Which Chris Evans?"), correctable either way (subject=person / character / role:<role>)
        try:
            out["subject"] = service.subject_of(person, subject)
        except Exception:
            out["subject"] = None
    if req["by_only"]:   # "<song> by <name>": the name is the voice, said out loud because it could be the artist
        out["note"] = (f"I read “by {person}” as the voice. If {person} is the song's artist, say "
                       f"“{song} by {person} as <voice>”.")
    if not artist:
        known = ambiguity.recall(ambiguity.song_key(song))
        if known:
            out.update(chosen=known["chosen"], others=known["others"], choices=known["choices"])
        else:
            choices = ambiguity.choices_for(song)
            if len(choices) >= 2:
                out.update(ask=True, choices=choices)
    h._json(out)


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
    job = Job("cover", title=f"Preview: {meta['title']} · {label_of(opts.voice)}")  # same GPU queue as covers
    job.voices = {v for v in (opts.voice, opts.backing_voice) if v}
    JOBS[job.id] = job
    from .pipeline import Countdown, preview_estimate
    try:   # its own time from the start, so the wait behind its song's preparation doesn't jump up when it begins
        est = preview_estimate(song, opts, len(variants))
    except Exception:   # noqa: BLE001 - extrapolated from progress instead
        est = None
    job.own_estimate = est["rendering"] if est else 0
    _say_why_waiting(job, meta["id"])

    def run(job: Job):
        clock = Countdown(est, ("separating", "rendering")) if est else None

        def progress(stage, frac, msg):
            if job.cancel:
                raise Cancelled()
            left = clock.left(stage, frac) if clock else None
            job.push(stage=stage, frac=round(frac, 3), message=msg,
                     **({"stage_left": left[0], "total_left": left[1]} if left else {}))
        res = ENGINE.make_preview(song, opts, variants, progress=progress)
        job.finish("done", message="Previews ready",
                   previews=[{"label": r["label"], "url": file_url(r["file"]), "options": r["options"], "fit": r.get("fit")}
                             for r in res])

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
