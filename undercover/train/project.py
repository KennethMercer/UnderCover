"""A voice-training project: sources -> clean phrases -> reference voice -> kept samples.

Layout (<cache_dir>/training/<name>/):
  project.json      sources, phrases (with scores, reasons, operator choices), reference, training state
  embeddings.npz    speaker embeddings per phrase (+ per-window minimum for mixed-speaker detection)
  raw/<src>.wav     downloaded or added audio (44.1 kHz mono)
  clips/<id>.wav    one phrase of separated, de-reverbed voice (44.1 kHz mono, 16-bit)
"""
import hashlib
import json
import uuid
import shutil
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from ..config import CONFIG, path
from ..models import ModelError, slug
from ..timing import timed
from .source import Source

TCFG = CONFIG["training"]
CLIP_SR = 44100


def _replace(tmp: Path, dest: Path, tries: int = 50):
    """Atomic replace that survives Windows' sharing violation: os.replace fails while another thread
    (e.g. the web page polling the project) has the destination open, so retry for up to ~2.5 s."""
    for i in range(tries):
        try:
            tmp.replace(dest)
            return
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(0.05)


def projects_dir() -> Path:
    d = path("cache_dir") / "training"
    d.mkdir(parents=True, exist_ok=True)
    return d


@dataclass
class Phrase:
    id: str
    source: str              # Source.id
    start: float             # seconds into the source audio
    dur: float
    score: float = 0.0       # similarity to the reference voice (cosine)
    mix: float = 1.0         # lowest similarity of any 3 s window (a second voice drags this down)
    music_db: float = -99.0  # leftover music/noise relative to the voice, dB
    clipped: bool = False
    reason: str = ""         # why it's left out automatically ("" = kept)
    choice: bool | None = None   # operator override: True keep, False remove, None automatic
    sung: bool = False       # matched the person's singing voice better than their speaking voice

    @property
    def kept(self) -> bool:
        return self.choice if self.choice is not None else not self.reason


@dataclass
class Project:
    name: str
    person: str = ""
    mode: str = "speaking"
    created: str = ""
    sources: list = field(default_factory=list)     # [Source]
    phrases: list = field(default_factory=list)     # [Phrase]
    reference: dict = field(default_factory=dict)   # {cluster, clip, share, sources, confirmed}
    speakers: list = field(default_factory=list)    # [{cluster, clip, share, sources, minutes}]
    train: dict = field(default_factory=dict)       # trainer state (see trainer.py)

    # ------------------------------------------------------------ storage
    @property
    def dir(self) -> Path:
        return projects_dir() / self.name

    def save(self):
        d = asdict(self)
        d["sources"] = [s.to_json() if isinstance(s, Source) else s for s in self.sources]
        d["phrases"] = [asdict(p) if isinstance(p, Phrase) else p for p in self.phrases]
        from .. import fsutil
        fsutil.write_text(self.dir / "project.json", json.dumps(d, indent=1))   # unique temp + fsync + retrying swap

    def save_train(self):
        """Write back only the training state, onto the latest saved project (under the lock), so edits made
        on the page meanwhile (samples, sources, reference) aren't overwritten by a long-running job."""
        with lock(self.name):
            cur = Project.load(self.name)
            cur.train = self.train
            cur.save()

    @classmethod
    def load(cls, name: str) -> "Project":
        f = projects_dir() / slug(name) / "project.json"
        if not f.exists():
            raise ModelError(f"No training project called '{name}'.")
        d = json.loads(f.read_text(encoding="utf-8"))
        d["sources"] = [Source(**s) for s in d.get("sources", [])]
        d["phrases"] = [Phrase(**p) for p in d.get("phrases", [])]
        return cls(**d)

    @classmethod
    def create(cls, person: str, mode: str = "speaking", name: str | None = None) -> "Project":
        name = slug(name or person)
        if not name:
            raise ModelError("Type the name of the person whose voice you want.")
        base, n = name, 2
        while (projects_dir() / name).exists():
            name, n = f"{base}-{n}", n + 1
        p = cls(name=name, person=person.strip(), mode=mode, created=time.strftime("%Y-%m-%d %H:%M"))
        for sub in ("raw", "clips"):
            (p.dir / sub).mkdir(parents=True, exist_ok=True)
        p.save()
        return p

    def delete(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def source(self, sid: str) -> Source:
        for s in self.sources:
            if s.id == sid:
                return s
        raise ModelError("That source isn't in this project.")

    def phrase(self, pid: str) -> Phrase:
        for p in self.phrases:
            if p.id == pid:
                return p
        raise ModelError("That sample isn't in this project.")

    def clip_path(self, pid: str) -> Path:
        return self.dir / "clips" / f"{pid}.wav"

    # ------------------------------------------------------------ embeddings
    def _emb_file(self) -> Path:
        return self.dir / "embeddings.npz"

    def load_embeddings(self) -> dict:
        f = self._emb_file()
        if not f.exists():
            return {}
        with np.load(f) as z:
            return {k: z[k] for k in z.files}

    def save_embeddings(self, emb: dict):
        tmp = self.dir / "embeddings.tmp.npz"
        np.savez(tmp, **emb)
        _replace(tmp, self._emb_file())

    # ------------------------------------------------------------ summary
    def kept_seconds(self) -> float:
        return sum(p.dur for p in self.phrases if p.kept)

    def sung_seconds(self) -> float:
        return sum(p.dur for p in self.phrases if p.kept and p.sung)

    def from_speech(self) -> bool:
        """Mostly speech in the training data (the octave choice gives speech-trained voices extra headroom)."""
        return self.sung_seconds() < 0.5 * self.kept_seconds()

    def summary(self) -> dict:
        reasons: dict = {}
        for p in self.phrases:
            if not p.kept and p.choice is None:
                reasons[p.reason] = reasons.get(p.reason, 0) + 1
        return {"name": self.name, "person": self.person, "mode": self.mode, "created": self.created,
                "sources": len(self.sources), "ready_sources": sum(s.status == "ready" for s in self.sources),
                "phrases": len(self.phrases), "kept": sum(p.kept for p in self.phrases),
                "kept_minutes": round(self.kept_seconds() / 60, 2), "sung_minutes": round(self.sung_seconds() / 60, 2),
                "target_minutes": TCFG["target_minutes"],
                "left_out": reasons, "reference": self.reference,
                "train": {k: v for k, v in self.train.items() if k not in ("history",)}}


def list_projects() -> list[dict]:
    out = []
    for f in sorted(projects_dir().glob("*/project.json"), key=lambda f: -f.stat().st_mtime):
        try:
            out.append(Project.load(f.parent.name).summary())
        except Exception:
            continue
    return out


# ================================================================ phrase splitting and checks

def _frames_db(x: np.ndarray, sr: int, hop_s=0.01, win_s=0.03) -> np.ndarray:
    hop, win = int(sr * hop_s), int(sr * win_s)
    if len(x) < win:
        return np.full(1, -120.0)
    c = np.concatenate([[0.0], np.cumsum(x.astype(np.float64) ** 2)])
    starts = np.arange(0, len(x) - win + 1, hop)
    rms = np.sqrt((c[starts + win] - c[starts]) / win)
    return 20 * np.log10(rms + 1e-9)


def split_phrases(voice: np.ndarray, sr: int, max_len: float) -> list[tuple[int, int]]:
    """Split a separated voice track at pauses. Returns (start, end) sample ranges."""
    db = _frames_db(voice, sr)
    active = db[db > -70]
    if len(active) < 50:
        return []
    loud = np.percentile(active, 95)
    thr = max(-50.0, loud - 32.0)
    on = db > thr
    hop = int(sr * 0.01)
    min_gap = 30           # frames (300 ms) of quiet ends a phrase
    pad = 10               # keep 100 ms of air around each phrase
    spans, i, n = [], 0, len(on)
    while i < n:
        if not on[i]:
            i += 1
            continue
        j, quiet = i, 0
        while j < n and quiet < min_gap:
            quiet = 0 if on[j] else quiet + 1
            j += 1
        end = j - quiet
        spans.append((max(0, i - pad), min(n, end + pad)))
        i = j

    def cut(a, b):  # recursively split over-long spans at their quietest frame (middle 60%)
        if (b - a) * 0.01 <= max_len:
            return [(a, b)]
        lo, hi = a + int((b - a) * 0.2), a + int((b - a) * 0.8)
        m = lo + int(np.argmin(db[lo:hi]))
        return cut(a, m) + cut(m, b)

    out = []  # short spans are kept too; scoring marks them "too short" so the operator can still see them
    for a, b in spans:
        for x, y in cut(a, b):
            if (y - x) * 0.01 >= 0.3:
                out.append((x * hop, min(len(voice), y * hop + int(sr * 0.03))))
    return out


def _resample16(x: np.ndarray, sr: int) -> np.ndarray:
    from scipy.signal import resample_poly
    if sr == 16000:
        return x.astype(np.float32)
    g = np.gcd(16000, sr)
    return resample_poly(x, 16000 // g, sr // g).astype(np.float32)


# ================================================================ processing a source

def process_source(proj: Project, src: Source, separator, embedder, progress=None, cancel=None) -> int:
    """Download (if needed) -> separate voice from music/noise -> de-reverb -> split into phrases -> checks
    and speaker embeddings. Returns the number of phrases added. progress(fraction, label)."""
    import soundfile as sf
    from ..separate import separate
    from . import source as srcmod
    report = progress or (lambda f, label: None)
    raw = proj.dir / "raw" / f"{src.id}.wav"
    if not raw.exists():
        src.status = "downloading"
        report(0.0, "downloading")
        srcmod.download(src, proj.dir / "raw", progress=lambda f: report(0.25 * f, "downloading"))
    if cancel and cancel():
        return 0
    src.status = "separating"
    report(0.25, "removing music and noise")
    stems = separate(raw, dereverb=TCFG["dereverb"], karaoke=(proj.mode == "singing"), separator=separator,
                     quality=TCFG["separation_quality"],
                     progress=lambda f, name: report(0.25 + 0.5 * f, "removing music and noise"))
    if cancel and cancel():
        return 0
    report(0.75, "splitting into phrases")
    with timed("train.phrases", source=src.id):
        voice, sr = sf.read(str(stems["vocals"]), dtype="float32", always_2d=True)
        voice = voice.mean(1)
        rest = None
        if stems.get("instrumental"):
            rest, _ = sf.read(str(stems["instrumental"]), dtype="float32", always_2d=True)
            rest = rest.mean(1)
        orig, osr = sf.read(str(raw), dtype="float32", always_2d=True)
        orig = orig.mean(1)
        if sr != CLIP_SR:
            from scipy.signal import resample_poly
            g = np.gcd(CLIP_SR, sr)
            voice = resample_poly(voice, CLIP_SR // g, sr // g).astype(np.float32)
            if rest is not None:
                rest = resample_poly(rest, CLIP_SR // g, sr // g).astype(np.float32)
            sr = CLIP_SR
        spans = split_phrases(voice, sr, TCFG["max_phrase"])
        proj.phrases = [p for p in proj.phrases if p.source != src.id]
        emb = {k: v for k, v in proj.load_embeddings().items() if not k.startswith(src.id + "_")}
        added = 0
        for n, (a, b) in enumerate(spans):
            if cancel and cancel():
                return added
            seg = voice[a:b]
            dur = len(seg) / sr
            pid = f"{src.id}_{n:04d}"
            # "not checked yet" until score() compares it with the reference voice: an interrupted run must never
            # leave unchecked phrases (an interviewer, say) counted as kept
            ph = Phrase(id=pid, source=src.id, start=round(a / sr, 3), dur=round(dur, 2), reason="not checked yet")
            v_db = 20 * np.log10(np.sqrt(np.mean(seg ** 2)) + 1e-9)
            if rest is not None:
                r = rest[a:b]
                ph.music_db = round(float(20 * np.log10(np.sqrt(np.mean(r ** 2)) + 1e-9) - v_db), 1)
            oa, ob = int(a / sr * osr), int(b / sr * osr)
            o = orig[oa:ob]
            ph.clipped = bool(len(o) and np.mean(np.abs(o) >= 0.999) > 0.001)
            if dur >= TCFG["min_phrase"]:
                s16 = _resample16(seg, sr)
                emb[pid] = embedder.embed(s16)
                win = embedder.embed_windows(s16) if dur > 4.5 else emb[pid][None]
                emb[pid + ":w"] = win
            peak = float(np.abs(seg).max())
            out = seg / peak * 0.9 if peak > 0.9 else seg
            sf.write(str(proj.clip_path(pid)), out, sr, subtype="PCM_16")
            proj.phrases.append(ph)
            added += 1
            if n % 20 == 0:
                report(0.75 + 0.25 * n / max(1, len(spans)), "matching voices")
        proj.save_embeddings(emb)
    # The phrases are cut, so this source's separated stems (a few hundred MB) and the download itself are no longer
    # needed (re-downloading takes seconds if the source is ever re-processed).
    from ..separate import file_hash
    shutil.rmtree(path("cache_dir") / "stems" / file_hash(raw), ignore_errors=True)
    if src.url:   # the operator's own recordings have no other copy: keep those
        raw.unlink(missing_ok=True)
    src.status = "ready"
    src.extra["phrases"] = added
    src.extra["voice_minutes"] = round(sum(b - a for a, b in spans) / sr / 60, 1)
    return added


class Cancelled(Exception):
    pass


def prepare_sources(name: str, ids: list[str], separator, embedder, progress=None, cancel=None,
                    friendly=str, between=None) -> list[str]:
    """Process several sources of project `name`, downloading ahead (network) while the GPU separates the
    current one. Each finished source is merged into the saved project right away (the web page may be
    editing the same project). Re-scores at the end. Returns titles that failed."""
    from concurrent.futures import ThreadPoolExecutor
    from . import source as srcmod
    report = progress or (lambda f, label, **kw: None)
    cancel = cancel or (lambda: False)
    with lock(name):
        proj = Project.load(name)
    todo = [proj.source(i) for i in ids]
    failed = []
    pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="dl")
    fut = {}
    for src in todo:
        if not (proj.dir / "raw" / f"{src.id}.wav").exists() and src.url:
            fut[src.id] = pool.submit(srcmod.download, src, proj.dir / "raw")
    try:
        for k, src0 in enumerate(todo):
            if between:
                between()   # e.g. run covers queued meanwhile; never from progress (that fires mid-separation)
            if cancel():
                raise Cancelled()
            with lock(name):
                pr = Project.load(name)
            src = pr.source(src0.id)

            def step(f, label, k=k, src=src):
                report((k + f) / len(todo), label, source=src.id, title=src.title, index=k + 1, count=len(todo))
            try:
                if src.id in fut:
                    step(0.0, "downloading")
                    fut[src.id].result()   # raises the download's ModelError
                process_source(pr, src, separator, embedder, progress=step, cancel=cancel)
                src.error = ""
            except ModelError as e:
                src.status, src.error = "failed", str(e)
                failed.append(src.title)
            except Exception as e:
                src.status, src.error = "failed", friendly(e)
                failed.append(src.title)
            if cancel():
                src.status = "found"
            with lock(name):
                cur = Project.load(name)
                cur.phrases = [x for x in cur.phrases if x.source != src.id] + [x for x in pr.phrases if x.source == src.id]
                cur.sources = [src if s.id == src.id else s for s in cur.sources]
                cur.save()
            if cancel():
                raise Cancelled()
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    report(1.0, "finding the voice")
    with lock(name):
        pr = Project.load(name)
        if pr.phrases:
            find_speakers(pr)
            score(pr)
        pr.save()
    return failed


def add_file(proj: Project, file: Path, title: str | None = None) -> Source:
    """Register an operator's own recording as a source (processed like a download)."""
    h = hashlib.sha256(Path(file).read_bytes()).hexdigest()[:10]
    sid = f"file_{h}"
    if any(s.id == sid for s in proj.sources):
        raise ModelError("That recording is already in this project.")
    dest = proj.dir / "raw" / f"{sid}.wav"
    import subprocess
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(file), "-ac", "1", "-ar", str(CLIP_SR), str(dest)],
                       capture_output=True, text=True)
    if r.returncode or not dest.exists():
        raise ModelError(f"Couldn't read '{title or Path(file).name}' as audio.")
    import soundfile as sf
    src = Source(id=sid, url="", title=title or Path(file).name, duration=sf.info(str(dest)).duration,
                 user=True, note="yours", selected=True)
    proj.sources.append(src)
    return src


# ================================================================ reference voice and scoring

def find_speakers(proj: Project):
    """Cluster all phrase embeddings into speakers; the reference is the voice heard most, weighted toward
    voices present in several sources. Fills proj.speakers and (unless confirmed) proj.reference."""
    from sklearn.cluster import AgglomerativeClustering
    emb = proj.load_embeddings()
    usable = [p for p in proj.phrases if p.id in emb and p.dur >= 1.5 and p.music_db < TCFG["music_db"] and not p.clipped]
    if len(usable) < 3:
        raise ModelError("Not enough clean speech was found to identify the voice. Add more sources or recordings.")
    usable.sort(key=lambda p: -p.dur)
    usable = usable[:1500]
    X = np.stack([emb[p.id] for p in usable])
    labels = AgglomerativeClustering(n_clusters=None, metric="cosine", linkage="average",
                                     distance_threshold=1 - TCFG["cluster_threshold"]).fit_predict(X)
    total = sum(p.dur for p in usable)
    n_sources = max(1, len({p.source for p in usable}))
    clusters = []
    for k in set(labels):
        members = [p for p, l in zip(usable, labels) if l == k]
        secs = sum(p.dur for p in members)
        c = X[labels == k].mean(0)
        c /= np.linalg.norm(c) + 1e-9
        srcs = len({p.source for p in members})
        best = max((p for p in members if 3.0 <= p.dur <= 12.0), key=lambda p: float(emb[p.id] @ c), default=None) \
            or max(members, key=lambda p: float(emb[p.id] @ c))
        clusters.append({"cluster": int(k), "clip": best.id, "share": round(secs / total, 3), "sources": srcs,
                         "minutes": round(secs / 60, 1), "weight": secs * (1 + srcs / n_sources), "centroid": c})
    clusters.sort(key=lambda c: -c["weight"])
    clusters = [c for c in clusters if c["share"] >= 0.02][:5] or clusters[:1]
    for c in clusters:
        emb[f"speaker:{c['cluster']}"] = c.pop("centroid")
        c.pop("weight")
    for k in [k for k in emb if k.startswith("speaker:") and int(k.split(":")[1]) not in {c["cluster"] for c in clusters}]:
        del emb[k]
    proj.speakers = clusters
    if not proj.reference.get("confirmed") or proj.reference.get("cluster") not in {c["cluster"] for c in clusters}:
        proj.reference = {**clusters[0], "confirmed": False}
    proj.save_embeddings(emb)


def set_reference(proj: Project, cluster: int | None = None, clip: str | None = None):
    """Operator picks a speaker (cluster) or a specific phrase as the voice to train."""
    emb = proj.load_embeddings()
    if clip:
        if clip not in emb:
            raise ModelError("That sample is too short to use as the reference.")
        emb["speaker:-1"] = emb[clip]
        proj.reference = {"cluster": -1, "clip": clip, "share": 0, "sources": 1, "minutes": 0, "confirmed": True}
        proj.save_embeddings(emb)
    else:
        sp = next((s for s in proj.speakers if s["cluster"] == cluster), None)
        if sp is None:
            raise ModelError("That voice isn't in the list any more. Refresh and pick again.")
        proj.reference = {**sp, "confirmed": True}
    score(proj)


def score(proj: Project):
    """Score every phrase against the reference voice and set the automatic keep/leave-out reason."""
    emb = proj.load_embeddings()
    key = f"speaker:{proj.reference.get('cluster')}"
    if key not in emb:
        return
    ref = emb[key]
    # one refinement pass: re-centre on the phrases that clearly match (more robust than one cluster mean)
    close = [emb[p.id] for p in proj.phrases if p.id in emb and float(emb[p.id] @ ref) >= TCFG["match_threshold"] + 0.1]
    if len(close) >= 5:
        r2 = np.mean(close, 0)
        ref = r2 / (np.linalg.norm(r2) + 1e-9)
    sref = _sung_reference(proj, emb, ref)
    for p in proj.phrases:
        if p.id not in emb:
            p.score, p.mix, p.sung = 0.0, 0.0, False
        else:
            e = emb[p.id]
            p.sung = sref is not None and float(e @ sref) > float(e @ ref)
            r = sref if p.sung else ref
            p.score = round(float(e @ r), 3)
            w = emb.get(p.id + ":w")
            p.mix = round(float((w @ r).min()), 3) if w is not None else p.score
        if p.dur < TCFG["min_phrase"]:
            p.reason = "too short"
        elif p.clipped:
            p.reason = "clipped"
        elif p.music_db > TCFG["music_db"]:
            p.reason = "music or noise"
        elif p.score < TCFG["match_threshold"]:
            p.reason = "other speaker"
        elif p.mix < TCFG["mix_threshold"]:
            p.reason = "two people at once"
        else:
            p.reason = ""
    _mark_duplicates(proj, emb)
    if sref is not None:
        _speech_as_backup(proj)


def _sung_reference(proj: Project, emb: dict, speech_ref: np.ndarray) -> np.ndarray | None:
    """The person's singing voice. Speaker embeddings of singing sit far from the same person's speech (Elliott
    Smith: his acapellas scored 0.22-0.32 against his interview voice, so every sung phrase was thrown out as
    "other speaker"), so singing needs its own reference: the main voice across the sung sources, accepted only if
    it's recognisably the same person as the speaking reference (his: 0.40; other people: about 0.1)."""
    from sklearn.cluster import AgglomerativeClustering
    from .source import is_sung
    sung_src = {s.id for s in proj.sources if is_sung(s.title)}
    usable = [p for p in proj.phrases if p.source in sung_src and p.id in emb and p.dur >= 1.5
              and p.music_db < TCFG["music_db"] and not p.clipped]
    if len(usable) < 8 or len({p.source for p in usable}) < 2:
        return None
    X = np.stack([emb[p.id] for p in usable])
    labels = AgglomerativeClustering(n_clusters=None, metric="cosine", linkage="average",
                                     distance_threshold=1 - TCFG["cluster_threshold"]).fit_predict(X)
    best, best_w = None, 0.0
    for k in set(labels):
        members = [p for p, l in zip(usable, labels) if l == k]
        srcs = len({p.source for p in members})
        w = sum(p.dur for p in members) * srcs
        if srcs >= 2 and w > best_w:   # in 2+ sources: the artist, not a one-off guest or cover singer
            c = X[labels == k].mean(0)
            best, best_w = c / (np.linalg.norm(c) + 1e-9), w
    if best is None or float(best @ speech_ref) < SUNG_LINK:
        return None
    close = [emb[p.id] for p in usable if float(emb[p.id] @ best) >= TCFG["match_threshold"] + 0.1]
    if len(close) >= 5:   # same refinement as the speaking reference
        r2 = np.mean(close, 0)
        best = r2 / (np.linalg.norm(r2) + 1e-9)
    return best


SUNG_LINK = 0.25   # sung reference vs speaking reference: same person (measured 0.40) vs someone else (~0.1)


def _speech_as_backup(proj: Project):
    """Voices are used on song vocals, so for singers the singing is what counts: speech only tops the kept audio up
    to the target when there isn't enough singing (best-matching speech first). Operator choices still win."""
    sung = sum(p.dur for p in proj.phrases if not p.reason and p.sung)
    if sung < 60:
        return   # hardly any clean singing: speech is all there is
    room = max(0.0, TCFG["target_minutes"] * 60 - sung)
    for p in sorted([p for p in proj.phrases if not p.reason and not p.sung], key=lambda p: -p.score):
        if room >= p.dur:
            room -= p.dur
        else:
            p.reason = "speech (enough singing)"


def _mark_duplicates(proj: Project, emb: dict, sim: float = 0.95, dur_tol: float = 0.05):
    """The same moment often appears in several videos (compilations re-use interviews): 30 of 151 kept Elliott
    Smith phrases were copies. Keep the best-scoring copy; mark the rest "duplicate" so training doesn't over-weight
    them and a copy can't end up in the held-out set."""
    kept = sorted([p for p in proj.phrases if not p.reason and p.id in emb], key=lambda p: -p.score)
    if len(kept) < 2:
        return
    X = np.stack([emb[p.id] for p in kept])
    X = X / (np.linalg.norm(X, axis=1, keepdims=True) + 1e-9)
    durs = np.array([p.dur for p in kept])
    taken = np.zeros(len(kept), bool)
    for i in range(len(kept)):
        if taken[i]:
            continue
        cos = X[i + 1:] @ X[i]
        close = (cos > sim) & (np.abs(durs[i + 1:] - durs[i]) <= dur_tol * durs[i])
        for j in np.flatnonzero(close) + i + 1:
            if not taken[j]:
                taken[j] = True
                kept[j].reason = "duplicate"


# Projects are edited from the web UI while a GPU job may be filling them; one lock per project.
_locks: dict = {}


def lock(name: str) -> threading.Lock:
    return _locks.setdefault(name, threading.Lock())
