"""SafeHouse: everything UnderCover keeps on disk (covers, speech, songs and stems, voices, training projects), listed,
renamed, exported and cleaned up without Explorer. Its HTTP routes are in safehouse/api.py.
"""
import json
import shutil
import time
from pathlib import Path

from ..core import addons, jobs
from ..core.files import file_url
from ..core.config import CONFIG, ROOT, path
from ..core.voices import ModelError
from ..speakeasy import speak

DAY = 86400
TEMP_PREFIXES = ("dl_", "yt_", "mix_", "tup_", "vup_", "inst_")


# ---------------------------------------------------------------- helpers

def _size(p: Path) -> int:
    try:
        if p.is_file():
            return p.stat().st_size
        return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
    except OSError:
        return 0


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


ALL_WORK = ("cover", "preview", "speech", "auto", "train", "train-auto", "train-prepare", "train-export", "install")
COVER_WORK = ("cover", "preview", "auto")   # jobs that read songs and write covers + history
SPEECH_WORK = ("speech",)
_BUSY_WHAT = {"cover": "a cover", "preview": "a preview", "auto": "a one-shot cover", "speech": "speech",
              "install": "an install"}


def _busy(kinds=ALL_WORK) -> str | None:
    """A job of these kinds that may be using the files right now."""
    for j in jobs.JOBS.values():
        if not j.finished and j.kind in kinds:
            return j.kind
    return None


def _guard(kinds=ALL_WORK):
    """Refuse only while work that could use these files runs (a training doesn't block deleting a cover)."""
    from ..core import storage
    if storage.MOVING:
        raise ModelError("UnderCover's files are being moved (Storage). Try again when that's done.")
    kind = _busy(kinds)
    if kind:
        what = _BUSY_WHAT.get(kind, "a voice training")
        raise ModelError(f"Can't do that while {what} is running (it may be using these files). Try again when it's done.")
    for j in jobs.JOBS.values():   # a background separation would just be redone; stop it so its files aren't in use
        if j.kind == "presep" and not j.finished:
            j.cancel = True


def _song_metas() -> dict:
    out = {}
    for f in path("uploads_dir").glob("*.json"):
        try:
            m = json.loads(f.read_text(encoding="utf-8"))
            out[m["id"]] = m
        except (ValueError, KeyError, OSError):
            continue
    return out


def _quality() -> dict:
    f = path("cache_dir") / "voice_quality.json"
    try:
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
    except ValueError:
        return {}


def ytdlp_info() -> dict:
    """What System shows (phase 16, core/ytdlp): the yt-dlp and puzzle solver in use, whether it's a checked update or the
    pinned pair, the last check, the auto-update switch, a heal running."""
    from ..core import ytdlp
    s = ytdlp.status()
    return {"ytdlp": s["yt"], "ytdlp_ejs": s["ejs"], "ytdlp_source": s["source"], "ytdlp_pinned": s["pinned"]["yt"],
            "ytdlp_auto": s["auto"], "ytdlp_last": s.get("last_result"), "ytdlp_checked": s.get("last_check"),
            "ytdlp_updated": s.get("swapped") or addons.updated("ytdlp"), "ytdlp_job": s["job"], "ytdlp_broken": bool(s["broken"])}


def _update_ytdlp() -> dict:
    """The System room's Update (phase 16): the self-heal as a job: PyPI's newest yt-dlp + its pinned solver, hash
    checked, staged, tested on known videos, used only if it does at least as well; or back to the last known good."""
    from ..core import ytdlp
    return {"job": ytdlp.start_heal().id}


def _ytdlp_auto(on: bool) -> dict:
    from ..core import ytdlp
    ytdlp.set_auto(on)
    return ytdlp_info()


def _duplicate_voices(hist: list) -> list[dict]:
    """Voices whose model.pth is byte-identical to another voice's (e.g. 'biden' and 'joe-biden' from two sites).
    Internal Affairs P6-1 (the clean-up deleted the original, the voice covers, the project and the cadence point to):
    the one kept is the one with a cadence, then a project's exported voice, then a display name, then Undo/waiting
    records, then more covers, then the older install. The others are SET ASIDE (Clean up never deletes a voice).
    -> [{voice, display, keeps, keeps_display, because, size}] for the ones that go."""
    import hashlib
    from ..core.voices import DEMO_VOICE, display_name, is_demo, listed_voices
    from ..doubleagent.project import list_projects
    exported = {p["train"].get("exported") for p in list_projects()}
    by_size: dict = {}
    for v in listed_voices():
        try:
            by_size.setdefault(v.pth.stat().st_size, []).append(v)
        except OSError:
            pass
    used: dict = {}
    for h in hist:
        for name in {h.get("voice"), (h.get("options") or {}).get("backing_voice")} - {None}:
            used[name] = used.get(name, 0) + 1

    def why(v):   # what makes this the voice to keep, strongest first
        ex = v.extra or {}
        cadence = bool(ex.get("tts")) or ((v.dir / "tts").is_dir() and any((v.dir / "tts").iterdir()))   # not empty
        records = any(ex.get(k) for k in ("improved_pending", "improved_previous", "improved_turned_down"))
        # the demo voice the First assignment uses is always kept (a copy of it can't send it aside)
        return (is_demo(v) and v.name == DEMO_VOICE, cadence, v.name in exported, bool((v.display or "").strip()),
                records, used.get(v.name, 0))
    out = []
    for group in by_size.values():
        if len(group) < 2:
            continue
        by_hash: dict = {}
        for v in group:
            by_hash.setdefault(hashlib.sha1(v.pth.read_bytes()).hexdigest(), []).append(v)
        for same in by_hash.values():
            if len(same) < 2:
                continue
            same.sort(key=lambda v: v.installed or "")                 # the older install wins a tie
            same.sort(key=why, reverse=True)                            # stable: the strongest reason first
            keep = same[0]
            k = why(keep)
            words = ("it's the demo voice the First assignment uses", "it has the cadence",
                     "it's the trained project's voice", "it has your name for it",
                     "it has an Undo or a retrained version", f"it's used for {k[5]} cover{'s' * (k[5] != 1)}")
            for v in same[1:]:
                w = why(v)   # the reason that decided between these two (the first where the kept one is ahead)
                i = next((i for i in range(len(k)) if k[i] != w[i]), None)
                out.append({"voice": v.name, "display": display_name(v), "keeps": keep.name,
                            "keeps_display": display_name(keep), "because": words[i] if i is not None else
                            "it was installed first", "size": _size(v.dir)})
    return out


# ---------------------------------------------------------------- inventories

def summary() -> dict:
    cache = path("cache_dir")
    temp = [cache / "converted", cache / "keyshift", cache / "previews", cache / "_work", path("logs_dir")]
    temp += [p for p in cache.iterdir() if p.is_dir() and p.name.startswith(TEMP_PREFIXES)]
    out_files = [p for p in path("output_dir").iterdir() if p.is_file() and p.name != "history.jsonl"]
    cats = {
        "covers": sum(_size(p) for p in out_files),
        "speech": speak.storage(),
        "songs": _size(path("uploads_dir")),
        "stems": _size(cache / "stems"),
        "voices": _size(path("voices_dir")),
        "training": _size(cache / "training"),
        "temporary": sum(_size(p) for p in temp),
        "app": sum(_size(path("models_dir") / d) for d in ("rvc", "separator", "speaker", "tts")),
    }
    du = shutil.disk_usage(ROOT)
    cv, sp = covers(), speak.list_speech()
    counts = {"covers": len(cv), "speech": len(sp), "songs": len(_song_metas()),
              "voices": len(list(path("voices_dir").glob("*/meta.json"))),
              "training": len(list((cache / "training").glob("*/project.json")))}
    # what each of the Library's Kept / Recent tabs holds per category: covers and speech have a recent tray; songs,
    # voices and training are always kept (and have no Recent)
    tabs = {"kept": {**counts, "covers": sum(1 for x in cv if x["kept"]), "speech": sum(1 for x in sp if x["kept"])},
            "recent": {"covers": sum(1 for x in cv if x["recent"]), "speech": sum(1 for x in sp if x["recent"])}}
    return {"categories": cats, "total": sum(cats.values()), "free": du.free, "drive": str(ROOT.anchor).rstrip("\\"),
            **ytdlp_info(), "counts": counts, "tabs": tabs}


def covers() -> list[dict]:
    from ..frontman.pipeline import history
    metas = _song_metas()
    out = []
    for h in history():
        o = Path(h["output"])
        song_id = Path(h["song"]).stem
        m = metas.get(song_id, {})
        opts = h.get("options", {})
        out.append({
            "id": h["id"], "title": h.get("title") or o.stem, "voice": h["voice"],
            "kept": h["kept"], "kept_at": h.get("kept_at"), "recent": h["recent"],
            "backing_voice": opts.get("backing_voice") if opts.get("backing_voice") != h["voice"] else None,
            "created": h.get("created", ""), "seconds": h.get("seconds", {}), "options": opts, "notes": h.get("notes", []),
            "file": o.name, "format": o.suffix.lstrip(".").upper(), "size": _size(o), "duration": m.get("duration"),
            "song_id": song_id if m else None, "song_source": m.get("source_url"),
            "url": file_url(o), "original": file_url(h["song"]) if Path(h["song"]).exists() else None,
            "vocal": file_url(h.get("converted_vocal")) if h.get("converted_vocal") and Path(h["converted_vocal"]).exists() else None,
        })
    return out


def speech() -> list[dict]:
    """SpeakEasy's saved speech (output/speech/)."""
    return speak.list_speech()


STEM_LABELS = {"vocals": "Vocals", "instrumental": "Instrumental", "other": "Instrumental", "noreverb": "Vocals (dry)", "reverb": "Reverb (removed)"}


def _stems_for(song_id: str) -> list[dict]:
    root = path("cache_dir") / "stems" / song_id
    out = []
    if not root.exists():
        return out
    for f in sorted(x for x in root.rglob("*") if x.suffix.lower() in (".wav", ".flac")):
        rel = f.relative_to(root).parts
        label = STEM_LABELS.get(f.stem, f.stem.capitalize())
        if any(p.startswith("karaoke_") for p in rel[:-1]):
            label = {"vocals": "Lead vocal", "instrumental": "Backing vocals"}.get(f.stem, label)
            if any(p.startswith("dereverb_") for p in rel[:-1]):
                label = {"noreverb": "Lead vocal (dry)", "reverb": "Lead reverb (removed)"}.get(f.stem, label)
        elif any(p.startswith("dereverb_") for p in rel[:-1]):
            label = {"noreverb": "Vocals (dry)", "reverb": "Reverb (removed)"}.get(f.stem, label)
        quality = "fast" if rel[0].endswith("_fast") else "max" if rel[0].endswith("_max") else "high"
        out.append({"label": label, "size": _size(f), "url": file_url(f), "quality": quality, "model": rel[0]})
    return out


def songs() -> list[dict]:
    from ..frontman.pipeline import history
    hist = history()
    current = Path(CONFIG["separation"]["vocal_model"]).stem
    out = []
    for sid, m in _song_metas().items():
        f = path("uploads_dir") / m.get("file", "")
        used = [h for h in hist if Path(h["song"]).stem == sid]
        stems = _stems_for(sid)
        last = max([_mtime(Path(h["output"])) for h in used] + [_mtime(f)])
        key = None
        kf = path("cache_dir") / "keys" / f"{sid}.json"
        if kf.exists():
            try:
                key = json.loads(kf.read_text(encoding="utf-8")).get("name")
            except ValueError:
                pass
        old_model = bool(stems) and not any(s["model"].startswith(current) for s in stems)
        out.append({
            "id": sid, "title": m.get("title") or m.get("name"), "duration": m.get("duration"), "key": key,
            "sample_rate": m.get("sample_rate"), "channels": m.get("channels"), "lufs": m.get("lufs"),
            "source": "YouTube" if m.get("youtube_id") or m.get("source_url") else "dropped file", "source_url": m.get("source_url"),
            "size": _size(f), "stems": stems, "stems_size": sum(s["size"] for s in stems), "stems_old_model": old_model,
            "covers": len(used), "cover_titles": [Path(h["output"]).stem for h in used][:5], "last_used": last,
            "url": file_url(f) if f.exists() else None,
            "sample": bool(m.get("sample")), "artist": m.get("artist") or "",   # the First assignment's song (phase 17)
        })
    out.sort(key=lambda x: -x["last_used"])
    return out


def _full_index(vdir):
    from .smaller import full_index
    try:
        return full_index(vdir)
    except OSError:
        return None


def _aside_dir() -> Path:
    from ..core.voices import voices_dir
    return voices_dir() / ".aside"


def _aside_records() -> dict:
    """{aside folder: (voice, why)} from the Undo records: a voice in the list (its Undo would bring the folder back),
    or None when the voice holding the record is set aside itself (removed: the record only says why)."""
    from ..core.voices import list_voices
    holders = [(v.name, v.extra or {}) for v in list_voices()]
    d = _aside_dir()
    for f in (d.iterdir() if d.is_dir() else []):
        try:
            holders.append((None, json.loads((f / "meta.json.aside").read_text(encoding="utf-8")).get("extra") or {}))
        except (OSError, ValueError):
            continue
    out = {}
    for name, ex in holders:
        prev, down = ex.get("improved_previous") or {}, ex.get("improved_turned_down") or {}
        if prev.get("aside") and (name or prev["aside"] not in out):
            out[prev["aside"]] = (name, "replaced by a new model" if prev.get("how") == "replaced"
                                  else "the one before the retrained voice you kept")
        if down.get("aside") and (name or down["aside"] not in out):
            out[down["aside"]] = (name, "the retrained voice you didn't keep")
    return out


def aside() -> list[dict]:
    """Voices set aside, never deleted (Internal Affairs P4-1): a replaced voice, the one not kept after a blind A/B, a
    replace that didn't finish. Newest first: {id, voice, display, size, at, why, undo_for}. undo_for: the voice whose
    Undo would bring this one back (removing it ends that Undo)."""
    import re
    from ..core.voices import display_of_meta
    d = _aside_dir()
    if not d.is_dir():
        return []
    recs = _aside_records()
    out = []
    for f in d.iterdir():
        if not f.is_dir():
            continue
        meta = {}
        for m in (f / "meta.json.aside", f / "meta.json"):
            try:
                meta = json.loads(m.read_text(encoding="utf-8"))
                break
            except (OSError, ValueError):
                continue
        name = meta.get("name") or f.name
        undo_for, why = recs.get(f.name, (None, None))
        why = why or ((meta.get("extra") or {}).get("set_aside") or {}).get("why")
        if not why:
            why = "a replace that didn't finish" if f.name.endswith("-unfinished") else "set aside earlier"
        # when it was set aside: the stamp in its name (a moved folder keeps its old modified time)
        st = re.search(r"-(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})\d{2}(?:-\d+)?(?:-unfinished)?$", f.name)
        at = (f"{st[1]}-{st[2]}-{st[3]} {st[4]}:{st[5]}" if st
              else time.strftime("%Y-%m-%d %H:%M", time.localtime(f.stat().st_mtime)))
        display = (((meta.get("extra") or {}).get("set_aside") or {}).get("display")   # kept when it was set aside
                   or display_of_meta(meta) or name)
        out.append({"id": f.name, "voice": name, "display": display, "size": _size(f),
                    "at": at, "why": why, "undo_for": undo_for})
    out.sort(key=lambda x: x["at"], reverse=True)
    return out


def remove_aside(aid: str) -> dict:
    """The user's explicit choice: delete one set-aside voice (only a folder directly in voices/.aside; test mode: the
    test's own folders only). A voice whose Undo pointed at it loses that Undo. -> {removed, freed}."""
    import re
    from ..core import fsutil, storage
    from ..core.voices import get_voice
    d = _aside_dir() / aid
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", aid or "") or not d.is_dir():
        raise LookupError("That set-aside voice isn't there.")
    owner = _aside_records().get(aid, (None,))[0]
    freed = _size(d)
    storage.remove_files([], within=[_aside_dir()], dirs=[d])
    if owner:   # its Undo can't bring it back any more: the record goes too (no Undo button that can only fail)
        v = get_voice(owner)
        v.extra = v.extra or {}
        for k in ("improved_previous", "improved_turned_down"):
            if (v.extra.get(k) or {}).get("aside") == aid:
                v.extra.pop(k)
        fsutil.write_text(v.dir / "meta.json", json.dumps(v.to_json(), indent=2))
    return {"removed": aid, "freed": freed, "undo_ended_for": owner}


def restore_aside(aid: str) -> dict:
    """"Bring it back": a set-aside voice back in the voices. One that an Undo serves goes back through that Undo
    (its voice card), so the two stay a pair."""
    from ..core.voices import bring_back, label_of
    from .smaller import index_back
    try:
        note = ((json.loads((_aside_dir() / aid / "meta.json.aside").read_text(encoding="utf-8")).get("extra") or {})
                .get("set_aside") or {})
    except (OSError, ValueError):
        note = {}
    if note.get("kind") == "full-index":   # a voice's full index, set aside when it couldn't be rebuilt
        return index_back(aid)
    owner = _aside_records().get(aid, (None,))[0]
    if owner:
        raise ModelError(f"Use Undo on {label_of(owner)}'s voice card (DoubleAgent) to bring this one back.")
    return bring_back(aid)


def voices() -> list[dict]:
    from ..doubleagent.service import undertrained
    from ..doubleagent.cadence import pack_of, pack_size
    from ..core.voices import list_voices, voice_info
    from ..frontman.pipeline import history
    hist = history()
    q = _quality()
    out = []
    for v in list_voices():   # all of them: a retrained one waiting is listed too (waiting_beside), its storage shown
        ex = v.extra or {}
        score = verdict = None
        for k, r in q.items():   # a measured check for this voice (installed or downloaded for a check)
            if r.get("voice") == v.name or k.endswith(f"installed:{v.name}:{v.installed}"):
                score, verdict = r.get("score"), r.get("verdict")
        sample = None
        if ex.get("trained") and ex.get("project"):
            pv = path("cache_dir") / "training" / ex["project"] / "train" / "previews" / f"e{ex.get('epoch')}.wav"
            sample = file_url(pv) if pv.exists() else None
        used = [h for h in hist if h["voice"] == v.name or h.get("options", {}).get("backing_voice") == v.name]
        pack = pack_of(v)   # its cadence (phase 4), listed and deleted with the voice or on its own
        cad = {"size": pack_size(v), "engine": pack.get("engine"), "made": pack.get("made"),
               "data_minutes": pack.get("data_minutes")} if pack else None
        out.append({
            "name": v.name, "version": v.version, "sr": v.sr, "index": bool(v.index), "installed": v.installed,
            "source": v.source, **voice_info(v), "project": ex.get("project"),
            "epoch": ex.get("epoch"), "train_minutes": ex.get("train_minutes"), "from_speech": ex.get("from_speech"),
            "f0_range": ex.get("f0_range"), "undertrained": undertrained(v), "score": score, "verdict": verdict,
            "size": _size(v.dir), "covers": len(used), "cover_titles": [h.get("title") or Path(h["output"]).stem for h in used][:5],
            "sample": sample, "cadence": cad, "full_index": _full_index(v.dir),
        })
    out.sort(key=lambda x: x["name"])
    return out


def training() -> list[dict]:
    from ..doubleagent.project import list_projects, projects_dir
    out = []
    for p in list_projects():
        d = projects_dir() / p["name"]
        t = d / "train"
        parts = {"downloads": _size(d / "raw"), "clips": _size(d / "clips"),
                 "training data": _size(t / "data.pt"), "resume point": _size(t / "resume.pt"),
                 "saves": sum(_size(f) for f in t.glob("e*.pth")) if t.exists() else 0}
        out.append({**p, "size": _size(d), "parts": parts, "updated": _mtime(d / "project.json")})
    out.sort(key=lambda x: -x["updated"])
    return out


# ---------------------------------------------------------------- clean up

def _cleanup_groups() -> dict:
    from ..frontman.pipeline import history
    from ..doubleagent.project import list_projects, projects_dir
    cache = path("cache_dir")
    now = time.time()
    hist = history()
    g = {}
    conv = [f for f in (cache / "converted").glob("*") if f.is_file() and now - _mtime(f) > DAY]
    g["converted"] = {"title": "Temporary conversion files",
                      "detail": "Converted vocals from past covers (the covers are kept; their \"vocal only\" download goes)",
                      "paths": conv, "default": True}
    g["keyshift"] = {"title": "Saved key changes", "detail": "Transposed backing tracks, re-made in a few seconds when needed",
                     "paths": [f for f in (cache / "keyshift").glob("*") if f.is_file()], "default": True}
    old = []
    for s in songs():
        if s["stems"] and now - s["last_used"] > 30 * DAY:
            old.append(cache / "stems" / s["id"])
    g["old_stems"] = {"title": "Stems of songs not used for 30 days",
                      "detail": f"{len(old)} songs: separated again if you make another cover of them (~1 min each)",
                      "paths": old, "default": True}
    unused = []
    for s in songs():
        if s["covers"] == 0 and now - s["last_used"] > 30 * DAY:
            unused += [p for p in path("uploads_dir").glob(f"{s['id']}.*")] + [cache / "stems" / s["id"]]
    songs_known = {p.stem for p in path("uploads_dir").glob("*.json")}
    current = Path(CONFIG["separation"]["vocal_model"]).stem
    orphan = [d for d in (cache / "stems").iterdir() if d.is_dir() and d.name not in songs_known] if (cache / "stems").exists() else []
    g["orphan_stems"] = {"title": "Stems that belong to no song in the library",
                         "detail": "Left over from training downloads, tests and deleted songs", "paths": orphan, "default": True}
    other = [m for d in (cache / "stems").iterdir() if d.is_dir() and d.name in songs_known
             for m in d.iterdir() if m.is_dir() and not (m.name == current or m.name.startswith(current + "_"))] if (cache / "stems").exists() else []
    g["other_model_stems"] = {"title": "Stems made with older separation models",
                              "detail": "Covers use the current model, so these songs would be separated again anyway", "paths": other, "default": True}
    used_models = {CONFIG["separation"][k] for k in ("vocal_model", "dereverb_model", "karaoke_model")}
    sep_dir = path("models_dir") / "separator"
    unused_models = [f for f in sep_dir.glob("*.ckpt") if f.name not in used_models] if sep_dir.exists() else []
    unused_models += [f for f in sep_dir.glob("*.yaml") if not any(Path(u).stem.split("_sdr")[0] in f.stem for u in used_models)] if sep_dir.exists() else []
    g["unused_models"] = {"title": "Separation models UnderCover doesn't use",
                          "detail": "From earlier comparisons; downloaded again automatically if a setting ever picks one",
                          "paths": unused_models, "default": True}
    old_speech = []
    for x in speak.list_speech():
        if x.get("kept"):   # sent to SafeHouse: clean-up never takes a kept track
            continue
        try:
            age = now - time.mktime(time.strptime(x["created"], "%Y-%m-%d %H:%M:%S"))
        except (TypeError, ValueError):
            continue
        if age > 30 * DAY:
            d = speak.speech_dir()
            old_speech += [d / f for f in (x["file"], x["mp3"]) if f] + [d / f"{x['id']}.json"]
    g["old_speech"] = {"title": "Speech older than 30 days", "detail": "Made in SpeakEasy and not kept; it can be made again from the same text",
                       "paths": old_speech, "default": False}
    g["unused_songs"] = {"title": "Songs never used for a cover (30+ days)", "detail": "The song files and their stems",
                         "paths": unused, "default": False}
    dupes = _duplicate_voices(hist)
    g["duplicate_voices"] = {   # set aside, never deleted (P6-1): "paths" stays empty, so nothing here is _remove'd
        "title": "The same voice installed twice",
        "detail": ("; ".join(f"{d['display']} ({d['voice']}) goes to Set aside: the same model as {d['keeps_display']}"
                             f" ({d['keeps']}), kept because {d['because']}" for d in dupes)
                   if dupes else "Identical models under two names"),
        "paths": [], "aside": dupes, "default": False}
    done = [projects_dir() / p["name"] for p in list_projects() if p["train"].get("state") == "exported"]
    g["finished_training"] = {"title": "Training projects of finished voices",
                              "detail": f"{len(done)} projects: the voices stay, but improving or retraining them later means downloading and "
                                        "preparing all their clips again (about 30-60 min each)",
                              "paths": done, "default": False}
    paused = [projects_dir() / p["name"] / "train" / "resume.pt" for p in list_projects()
              if p["train"].get("state") != "exported" and (projects_dir() / p["name"] / "train" / "resume.pt").exists()]
    g["resume_points"] = {"title": "Resume points of unfinished trainings",
                          "detail": "Those trainings would have to start over", "paths": paused, "default": False}
    temp = [p for p in cache.iterdir() if p.is_dir() and p.name.startswith(TEMP_PREFIXES)]
    temp += [p for p in (cache / "previews").glob("*") if now - _mtime(p) > DAY]   # incl. SpeakEasy's sentences (speech_<job>)
    temp += [p for p in (cache / "_work").glob("*")]
    logs = [f for f in path("logs_dir").glob("*.log") if _size(f) > 10e6]
    g["temp"] = {"title": "Old previews, leftovers and big logs", "detail": "Preview renders older than a day, interrupted downloads, logs over 10 MB",
                 "paths": temp + logs, "default": True}
    for k, v in g.items():
        v["size"] = sum(_size(p) for p in v["paths"])
        v["count"] = len(v["paths"]) + len(v.get("aside", []))
        if v.get("aside"):   # moved to Set aside, not freed: its space comes back when it's deleted there
            v["aside_size"] = sum(d["size"] for d in v["aside"])
    return g


def clean_up(groups) -> dict:
    """Storage's Clean up, the ticked groups: files removed (_remove), duplicate voices SET ASIDE (P6-1).
    -> {freed, set_aside: [{voice, aside}], skipped: [{voice, why}]}"""
    from ..core import gpu
    from ..core.voices import remove_to_aside
    freed, moved, skipped = 0, [], []
    for k, v in _cleanup_groups().items():
        if k not in groups:
            continue
        freed += v["size"]
        for path_ in v["paths"]:
            _remove(path_)
        for d in v.get("aside", []):
            try:
                r = remove_to_aside(d["voice"], why=f"the same model as {d['keeps_display']}")
            except (ModelError, OSError) as e:   # in use right now, or a file held open: it stays, said so
                skipped.append({"voice": d["voice"], "why": str(e)})
                continue
            gpu.forget_voice(d["voice"])
            moved.append({"voice": d["voice"], "aside": r["aside"]})
    return {"freed": freed, "set_aside": moved, "skipped": skipped}


def _remove(p: Path):
    """Clean up's delete. Never a voice: anything in the voices folder (or its Set aside) is refused (P6-1)."""
    from ..core import storage
    from ..core.voices import voices_dir
    if storage._inside(Path(p), voices_dir()):
        raise ModelError(f"{p} is in your voices: Clean up sets voices aside, it never deletes them.")
    if p.suffix == ".log" and p.is_file():   # logs are truncated, not deleted (the server may be writing to it)
        p.write_text("", encoding="utf-8")
    elif p.is_dir():
        shutil.rmtree(p, ignore_errors=True)
    else:
        p.unlink(missing_ok=True)


# ---------------------------------------------------------------- routing


