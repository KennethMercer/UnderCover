"""Stage 4: voice model manager. Search Hugging Face, download, unpack, register RVC voices.

    python -m undercover.core.voices search plankton
    python -m undercover.core.voices download 1            # result number from the last search
    python -m undercover.core.voices download <url> --name plankton
    python -m undercover.core.voices list
    python -m undercover.core.voices remove plankton

Supported sources: Hugging Face (repo/file refs and URLs), Google Drive, Pixeldrain, direct links.
Downloaded .pth files are loaded with torch.load(weights_only=True) so a malicious file can't run code.
"""
import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import time
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, quote, unquote, urlparse

import requests

from .config import CONFIG, path

VCFG = CONFIG["voices"]
HF = "https://huggingface.co"
UA = {"User-Agent": "UnderCover/0.1 (local voice-cover tool)"}
# .pth files that are NOT inference voice models
NOT_VOICE = re.compile(r"(^|/)(G|D)_\d+\.pth$|hubert|rmvpe|contentvec|f0G|f0D|rng_state|optimizer|scheduler"
                       r"|training_args|pytorch_model|adapter_model|example|dataset", re.I)
# Repos with these tags are other kinds of ML models, not RVC voices
NOT_RVC_TAGS = {"transformers", "diffusers", "gguf", "timm", "keras", "sklearn", "peft", "text-generation",
                "image-classification", "sentence-transformers", "onnx"}

Progress = Callable[[int, int | None], None]  # (bytes_done, bytes_total)


class ModelError(Exception):
    """User-facing error: the message says what went wrong and what to do."""


def voices_dir() -> Path:
    d = path("voices_dir")
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------- search

@dataclass
class Result:
    name: str            # suggested voice name
    repo: str
    file: str            # path inside the repo
    size: int | None = None
    downloads: int = 0
    likes: int = 0
    updated: str = ""
    source: str = "hf"   # "hf" (repo search) or "collection"
    score: float = 0.0
    version: str | None = None   # parsed from names: "v1"/"v2"
    sr: str | None = None        # "32k"/"40k"/"48k"
    epochs: int | None = None
    steps: int | None = None

    @property
    def url(self) -> str:
        return f"{HF}/{self.repo}/resolve/main/{quote(self.file)}"


def _tokens(s: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", s.lower()) if t]


def _guess_name(repo: str, file: str) -> str:
    stem = Path(file).name
    for ext in (".zip", ".pth"):
        stem = stem.removesuffix(ext).removesuffix(ext)
    if stem.lower() in ("model", "weights", "archive") or not stem:
        stem = repo.split("/")[-1]
    # drop common noise like "(RVC)", "RVC v2", "400 Epochs", "69K Steps", "Weights.gg Model"
    stem = re.sub(r"[_.]+", " ", stem)
    stem = re.sub(r"weights( gg)?( model)?|\bmodel\b|\(\d+\)", " ", stem, flags=re.I)
    stem = re.sub(r"[\(\[]?\b(rvc|v[12]|rvc ?v[12]|\d+k?\s*(epochs?|epoch|epoth|steps?))\b[\)\]]?", " ", stem, flags=re.I)
    s = slug(stem)
    if s in ("", "archive", "model", "weights", "voice", "rvc") and repo:
        return _guess_name("", repo.split("/")[-1] + ".zip")
    return s or "voice"


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:60]


def parse_model_info(text: str) -> dict:
    """Training details people put in RVC model names, e.g. 'Plankton (RVC v2) 1K Epochs 69K Steps 40k'."""
    t = re.sub(r"[_\-]+", " ", text.lower())

    def num(m):
        return int(float(m.group(1)) * (1000 if m.group(2) else 1)) if m else None
    ver = re.search(r"(?:rvc\s*)?\b(v[12])\b|rvc(v[12])\b", t)
    sr = re.search(r"\b(32|40|48)\s?k(?:hz)?\b(?!\s*(?:steps?|epochs?))", t)
    return {"version": (ver.group(1) or ver.group(2)) if ver else None,
            "sr": f"{sr.group(1)}k" if sr else None,
            "epochs": num(re.search(r"\b(\d+(?:\.\d+)?)\s*(k)?\s*(?:epochs?|epoth|ep)\b", t)),
            "steps": num(re.search(r"\b(\d+(?:\.\d+)?)\s*(k)?\s*steps?\b", t))}


def _quality_score(r: "Result") -> float:
    """Prefer well-trained v2 models at higher sample rates; avoid huge downloads (often training dumps)."""
    q = 0.0
    q += {"v2": 0.6, "v1": -0.3}.get(r.version or "", 0)
    q += {"48k": 0.5, "40k": 0.5, "32k": 0.0}.get(r.sr or "", 0)
    if r.epochs:
        q += 0.6 if 200 <= r.epochs <= 2000 else (-0.6 if r.epochs < 100 else 0.2)
    if r.size and r.size > 400e6:
        q -= 1.0
    return q


def _match_score(query_tokens: list[str], text: str) -> float:
    t = text.lower()
    toks = set(_tokens(text))
    hits = sum(1.0 if q in toks else 0.6 if q in t else 0 for q in query_tokens)
    return hits / max(len(query_tokens), 1)


def _get_json(url: str, **params):
    try:
        r = requests.get(url, params=params, headers=UA, timeout=60)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        raise ModelError(f"Couldn't reach Hugging Face ({e.__class__.__name__}). Check your internet connection and try again.")


def _search_repos(query: str, limit: int) -> list[Result]:
    data = _get_json(f"{HF}/api/models", search=query, full="true", limit=100)
    out = []
    for m in data:
        if NOT_RVC_TAGS & set(m.get("tags", [])):
            continue
        files = [s["rfilename"] for s in m.get("siblings", [])]
        for f in files:
            fl = f.lower()
            if not fl.endswith((".zip", ".pth")) or NOT_VOICE.search(f):
                continue
            out.append(Result(name=_guess_name(m["id"], f), repo=m["id"], file=f,
                              downloads=m.get("downloads", 0) or 0, likes=m.get("likes", 0) or 0,
                              updated=(m.get("lastModified") or "")[:10]))
    return out


def _collection_files(repo: str) -> list[dict]:
    """File list (name + size) of a big collection repo, cached on disk."""
    cache = path("cache_dir") / "hf_lists" / (slug(repo) + ".json")
    cache.parent.mkdir(parents=True, exist_ok=True)
    if cache.exists() and time.time() - cache.stat().st_mtime < VCFG["collection_cache_hours"] * 3600:
        return json.loads(cache.read_text(encoding="utf-8"))
    d = _get_json(f"{HF}/api/models/{repo}", blobs="true")
    files = [{"f": s["rfilename"], "s": s.get("size")} for s in d.get("siblings", [])
             if s["rfilename"].lower().endswith((".zip", ".pth")) and not NOT_VOICE.search(s["rfilename"])]
    cache.write_text(json.dumps(files), encoding="utf-8")
    return files


def search(query: str, limit: int | None = None) -> list[Result]:
    """Search HF repo names plus the configured collection repos. Best matches first."""
    limit = limit or VCFG["search_limit"]
    q = _tokens(query)
    if not q:
        raise ModelError("Type a voice name to search for, e.g. 'plankton'.")
    results = _search_repos(query, limit)
    for repo in VCFG["collections"]:
        try:
            files = _collection_files(repo)
        except ModelError:
            continue  # one unreachable collection shouldn't break search
        for e in files:
            if _match_score(q, e["f"]) >= 1.0:
                results.append(Result(name=_guess_name(repo, e["f"]), repo=repo, file=e["f"],
                                      size=e["s"], source="collection"))
    max_bytes = VCFG["max_download_gb"] * 1e9
    seen, ranked = set(), []
    for r in results:
        key = (r.repo, r.file)
        if key in seen or (r.size and r.size > max_bytes):
            continue
        seen.add(key)
        for k, v in parse_model_info(f"{r.repo} {r.file}").items():
            setattr(r, k, v)
        r.score = (_match_score(q, f"{r.repo} {r.file}") * 10
                   + min(r.downloads, 1000) / 200 + min(r.likes, 50) / 10 + _quality_score(r))
        ranked.append(r)
    ranked.sort(key=lambda r: r.score, reverse=True)
    ranked = ranked[:limit]
    _fill_sizes(ranked)
    ranked = [r for r in ranked if not (r.size and r.size > max_bytes)]
    for r in ranked:  # sizes are known now; re-rank with the size penalty
        r.score = _match_score(q, f"{r.repo} {r.file}") * 10 + min(r.downloads, 1000) / 200 + min(r.likes, 50) / 10 + _quality_score(r)
    ranked.sort(key=lambda r: r.score, reverse=True)
    _save_last_search(ranked)
    return ranked


def _fill_sizes(results: list[Result]):
    """Repo search doesn't return file sizes; fetch them per repo, in parallel."""
    from concurrent.futures import ThreadPoolExecutor
    repos = {r.repo for r in results if r.size is None}

    def sizes(repo):
        try:
            d = _get_json(f"{HF}/api/models/{repo}", blobs="true")
            return repo, {s["rfilename"]: s.get("size") for s in d.get("siblings", [])}
        except ModelError:
            return repo, {}
    with ThreadPoolExecutor(8) as ex:
        table = dict(ex.map(sizes, repos))
    for r in results:
        if r.size is None:
            r.size = table.get(r.repo, {}).get(r.file)


def _save_last_search(results: list[Result]):
    p = path("cache_dir") / "last_search.json"
    p.write_text(json.dumps([asdict(r) for r in results]), encoding="utf-8")


def last_search() -> list[Result]:
    p = path("cache_dir") / "last_search.json"
    if not p.exists():
        return []
    return [Result(**d) for d in json.loads(p.read_text(encoding="utf-8"))]


# ---------------------------------------------------------------- download

def resolve_url(src: str) -> tuple[str, str]:
    """Turn a user-supplied link/ref into (direct download URL, kind)."""
    src = src.strip()
    u = urlparse(src)
    host = (u.netloc or "").lower()
    if not u.scheme:
        # "owner/repo/path/to/file.zip" Hugging Face ref
        parts = src.split("/")
        if len(parts) >= 3:
            return f"{HF}/{parts[0]}/{parts[1]}/resolve/main/{quote('/'.join(parts[2:]))}", "hf"
        raise ModelError(f"Don't know how to download '{src}'. Paste a full link (Hugging Face, Google Drive or Pixeldrain).")
    if host.endswith("huggingface.co"):
        p = u.path.replace("/blob/", "/resolve/", 1)
        if "/resolve/" not in p:
            return _repo_voice_file(p.strip("/")), "hf"
        return f"{HF}{p}", "hf"
    if "drive.google.com" in host or "drive.usercontent.google.com" in host:
        m = re.search(r"/d/([\w-]{10,})", u.path)
        fid = m.group(1) if m else (parse_qs(u.query).get("id") or [None])[0]
        if not fid:
            raise ModelError("Couldn't find the file ID in that Google Drive link. Use a link to a single file (not a folder).")
        return fid, "gdrive"
    if "pixeldrain" in host:
        m = re.search(r"/(u|l|api/file)/([\w-]+)", u.path)
        if not m:
            raise ModelError("Couldn't read that Pixeldrain link. It should look like https://pixeldrain.com/u/XXXXXX")
        if m.group(1) == "l":
            return m.group(2), "pixeldrain_list"
        return f"https://pixeldrain.com/api/file/{m.group(2)}?download", "pixeldrain"
    if "mega.nz" in host:
        raise ModelError("MEGA links are encrypted and aren't supported. Download the file in your browser, then drop the .zip or .pth onto UnderCover to install it.")
    return src, "direct"


def _repo_voice_file(repo_path: str) -> str:
    """A link to a whole HF repo: use it if it holds exactly one voice file."""
    repo = "/".join(repo_path.split("/")[:2])
    try:
        d = requests.get(f"{HF}/api/models/{repo}", headers=UA, timeout=60)
    except requests.RequestException:
        raise ModelError("Couldn't reach Hugging Face. Check your connection and try again.")
    if d.status_code != 200:
        raise ModelError(f"Hugging Face repo '{repo}' wasn't found (or it's private). Check the link.")
    files = [s["rfilename"] for s in d.json().get("siblings", [])
             if s["rfilename"].lower().endswith((".zip", ".pth")) and not NOT_VOICE.search(s["rfilename"])]
    if len(files) == 1:
        return f"{HF}/{repo}/resolve/main/{quote(files[0])}"
    if not files:
        raise ModelError(f"No voice model (.zip/.pth) found in '{repo}'.")
    raise ModelError(f"'{repo}' holds {len(files)} models. Search for the one you want, or copy the link of that specific file.")


def _filename_from(resp: requests.Response, fallback: str) -> str:
    cd = resp.headers.get("content-disposition", "")
    m = re.search(r"filename\*=UTF-8''([^;]+)", cd) or re.search(r'filename="?([^";]+)"?', cd)
    name = unquote(m.group(1)) if m else fallback
    return Path(name).name or "download.bin"


def _stream(resp: requests.Response, dest_dir: Path, fallback_name: str, progress: Progress | None) -> Path:
    total = int(resp.headers.get("content-length") or 0) or None
    if total and total > VCFG["max_download_gb"] * 1e9:
        raise ModelError(f"That file is {total / 1e9:.1f} GB, over the {VCFG['max_download_gb']} GB limit. "
                         "It's probably a training checkpoint, not a voice model. Pick a smaller result.")
    if "text/html" in resp.headers.get("content-type", ""):
        raise ModelError("The link returned a web page instead of a file. It may be private, deleted, or need a login.")
    out = dest_dir / _filename_from(resp, fallback_name)
    done = 0
    with open(out, "wb") as f:
        for chunk in resp.iter_content(1 << 20):
            f.write(chunk)
            done += len(chunk)
            if progress:
                progress(done, total)
    return out


def _get(url: str, **kw) -> requests.Response:
    try:
        r = requests.get(url, headers=UA, stream=True, timeout=60, allow_redirects=True, **kw)
    except requests.RequestException as e:
        raise ModelError(f"Download failed ({e.__class__.__name__}). Check your connection and try again.")
    if r.status_code == 404:
        raise ModelError("The file wasn't found (404). It may have been deleted. Try another result.")
    if r.status_code in (401, 403):
        raise ModelError("The file isn't available: it doesn't exist, is private, or needs a login. Try another result.")
    if r.status_code == 429:
        raise ModelError("The site is rate-limiting downloads. Wait a few minutes and try again.")
    r.raise_for_status()
    return r


def _download_gdrive(fid: str, dest: Path, progress: Progress | None) -> Path:
    base = "https://drive.usercontent.google.com/download"
    r = _get(base, params={"id": fid, "export": "download", "confirm": "t"})
    if "text/html" in r.headers.get("content-type", ""):
        # Large-file warning page: resubmit its form fields
        html = r.text
        fields = dict(re.findall(r'name="(\w+)" value="([^"]*)"', html))
        if not fields:
            raise ModelError("Google Drive wouldn't release the file. It may be private or over its download quota. "
                             "Try again later or download it in your browser.")
        r = _get(base, params=fields)
    return _stream(r, dest, f"{fid}.zip", progress)


def download(src: str, progress: Progress | None = None) -> Path:
    """Download a link/ref into a temp folder; returns the downloaded file path."""
    url, kind = resolve_url(src)
    dest = Path(tempfile.mkdtemp(prefix="dl_", dir=path("cache_dir")))
    try:
        return _download_into(url, kind, dest, progress)
    except BaseException as e:   # incl. Cancelled from the progress callback: don't leave up to 2 GB behind
        shutil.rmtree(dest, ignore_errors=True)
        if isinstance(e, requests.RequestException):
            raise ModelError(f"The download failed ({e.__class__.__name__}). Check your connection and try again.") from e
        raise


def _download_into(url: str, kind: str, dest: Path, progress: Progress | None) -> Path:
    if kind == "gdrive":
        return _download_gdrive(url, dest, progress)
    if kind == "pixeldrain_list":
        info = _get_json(f"https://pixeldrain.com/api/list/{url}")
        files = [f for f in info.get("files", []) if f["name"].lower().endswith((".zip", ".pth", ".index"))]
        if not files:
            raise ModelError("That Pixeldrain list has no .zip/.pth/.index files.")
        for f in files:  # put all of them in one folder; install() picks them up
            _stream(_get(f"https://pixeldrain.com/api/file/{f['id']}?download"), dest, f["name"], progress)
        return dest
    out = _stream(_get(url), dest, Path(urlparse(url).path).name or "download.bin", progress)
    if kind == "hf" and out.suffix.lower() == ".pth":
        _fetch_sibling_index(url, dest, progress)
        return dest
    return out


def _fetch_sibling_index(url: str, dest: Path, progress: Progress | None):
    """A bare .pth on Hugging Face often has its .index next to it in the repo; grab that too."""
    m = re.match(rf"{HF}/([^/]+/[^/]+)/resolve/([^/]+)/(.+)$", url)
    if not m:
        return
    repo, rev, file = m.group(1), m.group(2), unquote(m.group(3))
    folder = str(Path(file).parent).replace("\\", "/").strip(".")
    try:
        sib = [s["rfilename"] for s in _get_json(f"{HF}/api/models/{repo}").get("siblings", [])]
    except ModelError:
        return
    for f in sib:
        if f.lower().endswith(".index") and str(Path(f).parent).replace("\\", "/").strip(".") == folder:
            _stream(_get(f"{HF}/{repo}/resolve/{rev}/{quote(f)}"), dest, Path(f).name, progress)


# ---------------------------------------------------------------- install / registry

@dataclass
class Voice:
    name: str
    dir: Path
    pth: Path
    index: Path | None
    version: str = "?"
    sr: int | None = None
    f0: bool = True
    source: str = ""
    installed: str = ""
    extra: dict = field(default_factory=dict)
    display: str = ""   # what the pages show instead of the folder name; "" = the default (see display_name)

    def to_json(self) -> dict:
        d = asdict(self)
        d.update(dir=str(self.dir), pth=str(self.pth), index=str(self.index) if self.index else None)
        return d


def inspect_pth(p: Path) -> dict:
    """Validate an RVC inference .pth safely and return its metadata."""
    import torch
    try:
        ck = torch.load(p, map_location="cpu", weights_only=True)
    except Exception as e:
        raise ModelError(f"'{p.name}' isn't a loadable voice model (or contains unsafe code), so it was rejected. ({e.__class__.__name__})")
    if not isinstance(ck, dict) or "weight" not in ck or "config" not in ck:
        raise ModelError(f"'{p.name}' isn't an RVC inference model. It may be a training checkpoint (G_/D_ file).")
    cfg = ck["config"]
    version = ck.get("version") or ("v2" if any(t.shape[-1] == 768 for k, t in ck["weight"].items() if k.endswith("emb_phone.weight")) else "v1")
    sr = ck.get("sr")
    if isinstance(sr, str):
        sr = int(sr.lower().rstrip("k")) * 1000
    return {"version": version, "sr": sr or (cfg[-1] if isinstance(cfg[-1], int) else None), "f0": bool(ck.get("f0", 1))}


def _safe_extract(zf: zipfile.ZipFile, dest: Path):
    """Extract only .pth/.index members, refusing path traversal."""
    for m in zf.infolist():
        name = m.filename.replace("\\", "/")
        if m.is_dir() or not name.lower().endswith((".pth", ".index")):
            continue
        target = (dest / Path(name).name).resolve()  # flatten: ignore folders inside the zip
        if dest.resolve() not in target.parents:
            continue
        if target.exists():  # same file name in two folders: keep both
            target = target.with_name(f"{target.stem}_{m.CRC:x}{target.suffix}")
        with zf.open(m) as src, open(target, "wb") as out:
            shutil.copyfileobj(src, out)


def _collect(paths: list[Path], work: Path) -> tuple[list[Path], list[Path]]:
    for p in paths:
        if p.is_dir():
            _collect(list(p.iterdir()), work)
        elif p.suffix.lower() in (".pth", ".index"):  # check first: torch .pth files are zips internally
            shutil.copy2(p, work / p.name)
        elif p.suffix.lower() == ".zip" or zipfile.is_zipfile(p):
            try:
                with zipfile.ZipFile(p) as zf:
                    _safe_extract(zf, work)
            except zipfile.BadZipFile:
                raise ModelError(f"'{p.name}' is a damaged zip file. Try downloading it again.")
    pths = [p for p in work.glob("*.pth") if not NOT_VOICE.search(p.name)]
    idx = list(work.glob("*.index"))
    return pths, idx


def _pick_index(indexes: list[Path]) -> Path | None:
    if not indexes:
        return None
    # RVC writes "trained_*.index" (no vectors) and "added_*.index" (the usable one)
    added = [p for p in indexes if "added" in p.name.lower()]
    pool = added or [p for p in indexes if "trained" not in p.name.lower()] or indexes
    return max(pool, key=lambda p: p.stat().st_size)


def set_aside(d: Path, tag: str = "", note: dict | None = None) -> Path:
    """A voice folder moved to voices/.aside/<name>-<stamp> (its meta.json renamed, so no list shows it), never
    deleted (Internal Affairs P4-1). note: why, kept in its meta (extra.set_aside) for SafeHouse's list. Test mode:
    the test's own folders only. -> where it went."""
    from . import fsutil, storage
    if storage.ALLOWED_ROOTS is not None:
        why = storage.refuse([d])
        if why:
            raise ModelError(why)
    base = voices_dir() / ".aside" / f"{d.name}-{time.strftime('%Y%m%d-%H%M%S')}{tag}"
    dst, n = base, 1
    while dst.exists():
        dst, n = base.with_name(f"{base.name}-{n}"), n + 1
    dst.parent.mkdir(parents=True, exist_ok=True)
    fsutil.retry(lambda: os.replace(d, dst))
    if (dst / "meta.json").exists():
        os.replace(dst / "meta.json", dst / "meta.json.aside")
    if (dst / "meta.json.aside").exists():
        try:   # why, and the name people knew it by (out of the lists, its default display can't be worked out later)
            meta = json.loads((dst / "meta.json.aside").read_text(encoding="utf-8"))
            meta.setdefault("extra", {})["set_aside"] = {"display": display_of_meta(meta), **(note or {}),
                                                         "at": time.strftime("%Y-%m-%d %H:%M")}
            fsutil.write_text(dst / "meta.json.aside", json.dumps(meta, indent=2))
        except (OSError, ValueError):
            pass   # an unreadable meta: it's still set aside, just without the note
    return dst


def display_of_meta(meta: dict) -> str:
    """A voice's display name from its meta alone (a voice set aside isn't in the lists): the one given, else the
    default (the person it was trained on, or its tidied name)."""
    if (meta.get("display") or "").strip():
        return meta["display"].strip()
    try:
        return display_name(Voice(**{**meta, "dir": Path(meta.get("dir") or "."), "pth": Path(meta.get("pth") or "."),
                                      "index": Path(meta["index"]) if meta.get("index") else None}))
    except (TypeError, ValueError, KeyError):
        return meta.get("name") or ""


def remove_to_aside(name: str, why: str = "you removed it") -> dict:
    """The user's Delete (FrontMan's Remove, SafeHouse's Delete): the voice is set aside, not deleted (Internal
    Affairs P5-1: a double-click on Remove deleted a trained voice for good). A retrained one waiting beside it goes
    with it. bring_back() puts them back; deleting for good is SafeHouse's Set aside section (its own control).
    -> {aside, with}"""
    from . import jobs
    v = get_voice(name)
    busy = jobs.jobs_using(v.name)
    if busy:
        raise ModelError(f"{busy[0].title or 'A cover or speech'} is using {label_of(v.name)} right now. Try again when "
                         "it's done (or cancel it).")
    cand = ((v.extra or {}).get("improved_pending") or {}).get("voice")
    try:   # only the candidate that says it's this voice's (a folder copy carries the original's improved_pending: P7-1)
        mine = json.loads((voices_dir() / cand / "meta.json").read_text(encoding="utf-8")).get("extra", {}).get(
            "pending_for") == v.name if cand else False
    except (OSError, ValueError, AttributeError):
        mine = False
    with_ = None
    if mine:
        with_ = set_aside(voices_dir() / cand, note={"why": f"waited beside {label_of(v.name)} when you removed it"}).name
    try:
        a = set_aside(v.dir, note={"why": why, **({"with": with_} if with_ else {})})
    except BaseException:
        if with_:   # the voice couldn't move (a file held open): its waiting one back beside it, as before
            bring_back(with_)
        raise
    return {"aside": a.name, "with": with_}


def bring_back(aid: str) -> dict:
    """A voice set aside, back in the voices under its own name (and a retrained one that went aside with it).
    Refused when that name is taken now. -> {voice, with}"""
    d = voices_dir() / ".aside" / (aid or "")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", aid or "") or not d.is_dir():
        raise LookupError("That set-aside voice isn't there.")
    from . import fsutil
    f = d / "meta.json.aside" if (d / "meta.json.aside").exists() else d / "meta.json"
    try:
        meta = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ModelError("That set-aside voice's details can't be read, so it can't be put back.")
    name = slug(meta.get("name") or "")
    target = voices_dir() / name
    if not name or target.exists():
        raise ModelError(f"There's a voice called {label_of(name)} in your voices now. Rename or remove it first.")
    note = (meta.get("extra") or {}).pop("set_aside", None) or {}
    fsutil.retry(lambda: os.replace(d, target))
    meta.update(dir=str(target), pth=str(target / "model.pth"),
                index=str(target / "model.index") if meta.get("index") else meta.get("index"))
    fsutil.write_text(target / "meta.json", json.dumps(meta, indent=2))
    (target / "meta.json.aside").unlink(missing_ok=True)
    with_ = None
    if note.get("with") and (voices_dir() / ".aside" / note["with"]).is_dir():
        try:
            with_ = bring_back(note["with"])["voice"]
        except ModelError:
            pass   # its name is taken now: it stays aside, listed in SafeHouse
    pending = ((meta.get("extra") or {}).get("improved_pending") or {}).get("voice")
    if pending and pending != with_:
        # what it was waiting on didn't come back with it: no "waiting" pointing at nothing (or at another voice)
        meta["extra"].pop("improved_pending", None)
        fsutil.write_text(target / "meta.json", json.dumps(meta, indent=2))
    return {"voice": name, "with": with_}


def _put_back(aside: Path, target: Path):
    """set_aside() undone (a replace that failed half-way): the voice as it was, the half-made one set aside."""
    from . import fsutil
    if target.exists():
        if (target / "tts").is_dir() and not (aside / "tts").exists():
            os.replace(target / "tts", aside / "tts")
        set_aside(target, "-unfinished")
    fsutil.retry(lambda: os.replace(aside, target))
    if (target / "meta.json.aside").exists():
        os.replace(target / "meta.json.aside", target / "meta.json")


def install(files: list[Path] | Path, name: str, source: str = "", overwrite: bool = False, root: Path | None = None) -> Voice:
    """Unpack downloaded file(s), find the voice .pth/.index, validate, and register under `name`.
    root: install under another folder than the voices (staging_dir(), for a candidate being checked)."""
    files = [files] if isinstance(files, Path) else files
    name = slug(name)
    if not name:
        raise ModelError("Give the voice a name using letters or numbers.")
    target = (root or voices_dir()) / name
    if target.exists() and not overwrite:
        raise ModelError(f"A voice called '{name}' is already installed. Pick another name or remove it first.")
    owner = pending_candidate(name) if root is None else None
    if owner:   # an unheard retrained voice: never written over, whatever overwrite says
        raise ModelError(f"\"{name}\" is the retrained {label_of(owner)} waiting to be heard. Give this one its own name, "
                         "or keep or set aside the waiting one first.")

    work = Path(tempfile.mkdtemp(prefix="inst_", dir=path("cache_dir")))
    try:
        pths, indexes = _collect(files, work)
        good = []
        for p in pths:
            try:
                good.append((p, inspect_pth(p)))
            except ModelError as e:
                last_err = e
        if not good:
            if pths:
                raise last_err
            raise ModelError("No voice model (.pth) was found in that download. It may not be an RVC model.")
        # Several valid models: prefer the largest (full precision / latest)
        pth, meta = max(good, key=lambda g: g[0].stat().st_size)
        index = _pick_index(indexes)

        # a voice's cadence (tts/ + extra.tts, made from the person's clips) outlives a new model of the same voice
        keep_tts, old_tts, old_display, old_pending, aside, old_meta = None, None, "", None, None, {}
        if target.exists():
            try:   # the name the user gave it outlives a new model of the same voice, like its cadence
                old_meta = json.loads((target / "meta.json").read_text(encoding="utf-8"))
                old_display = old_meta.get("display") or ""
            except (OSError, ValueError):
                old_meta = {}
            # a retrained one waiting beside this voice keeps waiting beside the new model (else it'd be orphaned)
            old_pending = (old_meta.get("extra") or {}).get("improved_pending")
            # the voice being replaced is set aside, never deleted (Internal Affairs P4-1); Undo puts it back
            aside = set_aside(target)
            if (aside / "tts").is_dir():   # the voice's cadence is the person's: it goes on with the new model
                keep_tts = aside / "tts"
                old_tts = (old_meta.get("extra") or {}).get("tts")
        try:
            target.mkdir(parents=True)
            if keep_tts is not None:
                shutil.move(str(keep_tts), target / "tts")
            shutil.move(str(pth), target / "model.pth")
            if index:
                shutil.move(str(index), target / "model.index")
                try:   # half the size on disk (fp16); exports rebuild a faiss file for other tools
                    from .engine.rvc.index import compact_in_place
                    compact_in_place(target / "model.index")
                except Exception:
                    pass   # unreadable/unusual index: keep it as it came (loading says so later)
            # Undo (DoubleAgent's improved-voice card, improved.undo) puts the old one back; the new one then waits
            # beside it as <name>-next, like a retrained voice
            prev = ({"improved_previous": {"at": time.strftime("%Y-%m-%d %H:%M"), "candidate": f"{name}-next",
                                           "before": (old_meta.get("extra") or {}).get("likeness"), "after": None,
                                           "aside": aside.name, "how": "replaced"}} if aside is not None else {})
            voice = Voice(name=name, dir=target, pth=target / "model.pth",
                          index=(target / "model.index") if index else None,
                          version=meta["version"], sr=meta["sr"], f0=meta["f0"], source=source,
                          installed=time.strftime("%Y-%m-%d %H:%M"), display=old_display,
                          extra={"original_pth": pth.name, "original_index": index.name if index else None,
                                 **({"tts": old_tts} if keep_tts is not None and old_tts else {}),
                                 **({"improved_pending": old_pending} if old_pending else {}), **prev})
            from . import fsutil
            fsutil.write_text(target / "meta.json", json.dumps(voice.to_json(), indent=2))
        except BaseException:
            if aside is not None:   # the old voice back as it was
                _put_back(aside, target)
            raise
        return voice
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------- candidates being checked (rc-fix7)
# A community voice downloaded for a one-shot's check waits in voices/.staging/<name> (one level deeper than the voices,
# so no list sees it) and joins the voices only once its check passes (promote); one that fails, or a check that stops,
# is discarded. These are downloads, never the user's voices: discard deletes only inside the staging folder, through
# storage.remove_files' allow-list (refused in test mode outside the test's folders).

def staging_dir() -> Path:
    return voices_dir() / ".staging"


def download_staged(src, name: str | None = None, progress: Progress | None = None) -> Voice:
    """A search result / link -> a candidate in the staging folder (not in any voice list). Another candidate of the
    same name there (two community voices both called "obama") keeps its folder: this one takes <name>-2, -3..."""
    if isinstance(src, Result):
        name = name or src.name
        src = src.url
    dl = download(src, progress)
    try:
        if not name:
            base = dl.name if dl.is_file() else dl.parent.name
            name = _guess_name("", base)
        return install(dl, staged_name(name), source=src, root=staging_dir())
    finally:
        shutil.rmtree(dl if dl.is_dir() else dl.parent, ignore_errors=True)


def staged_name(name: str) -> str:
    """A free folder name in the staging folder: <name>, else <name>-2, -3..."""
    base, n, final = slug(name), 1, slug(name)
    while (staging_dir() / final).exists():
        n += 1
        final = f"{base}-{n}"
    return final


def promote(name: str) -> Voice:
    """A candidate that passed its check joins the voices, under its own name, or the first free <name>-2, -3... when a
    voice of that name is there already (nothing is ever written over). -> the voice, as listed."""
    src = staging_dir() / slug(name)
    if not (src / "meta.json").is_file():
        raise ModelError(f"There's no candidate voice called {name!r} waiting to be checked.")
    base, n, final = slug(name), 1, slug(name)
    while (voices_dir() / final).exists() or pending_candidate(final):
        n += 1
        final = f"{base}-{n}"
    os.replace(src, voices_dir() / final)
    dst = voices_dir() / final
    meta = json.loads((dst / "meta.json").read_text(encoding="utf-8"))
    meta.update(name=final, dir=str(dst), pth=str(dst / Path(meta["pth"]).name))
    if meta.get("index"):
        meta["index"] = str(dst / Path(meta["index"]).name)
    from . import fsutil
    fsutil.write_text(dst / "meta.json", json.dumps(meta, indent=2))
    return get_voice(final)


def discard_staged(name: str | None = None, older_than: float | None = None):
    """Delete a candidate from the staging folder (name), or every candidate there (None: what a stopped check left).
    older_than (seconds): only candidates staged at least that long ago (server start: another server on the same
    folders may be checking one right now). Only ever inside the staging folder (storage.remove_files refuses anything
    else)."""
    root = staging_dir()
    if not root.is_dir():
        return
    dirs = [root / slug(name)] if name else [d for d in root.iterdir() if d.is_dir()]
    if older_than is not None:
        dirs = [d for d in dirs if d.is_dir() and time.time() - d.stat().st_mtime >= older_than]
    from . import storage
    storage.remove_files([], within=[root], dirs=[d for d in dirs if d.is_dir()])


def download_and_install(src: str, name: str | None = None, progress: Progress | None = None,
                         overwrite: bool = False) -> Voice:
    """One step: search result / link -> installed voice."""
    if isinstance(src, Result):
        name = name or src.name
        src = src.url
    if name and (voices_dir() / slug(name)).exists() and not overwrite:
        raise ModelError(f"A voice called '{slug(name)}' is already installed. Pick another name or remove it first.")
    dl = download(src, progress)
    try:
        if not name:
            base = dl.name if dl.is_file() else dl.parent.name
            name = _guess_name("", base)
        return install(dl, name, source=src, overwrite=overwrite)
    finally:
        shutil.rmtree(dl if dl.is_dir() else dl.parent, ignore_errors=True)


def list_voices() -> list[Voice]:
    out = []
    for meta in sorted(voices_dir().glob("*/meta.json")):
        d = json.loads(meta.read_text(encoding="utf-8"))
        # files are found next to meta.json, whatever folder was stored (the app folder may have been moved)
        here = meta.parent
        d["dir"] = here
        d["pth"] = here / Path(d["pth"]).name if (here / Path(d["pth"]).name).exists() else Path(d["pth"])
        d["index"] = (here / Path(d["index"]).name if (here / Path(d["index"]).name).exists() else Path(d["index"]))             if d.get("index") else None
        out.append(Voice(**d))
    return out


def waiting(v: "Voice", by_name: dict | None = None) -> str | None:
    """The voice a retrained candidate (<voice>-next) waits beside, while it waits (its owner's improved_pending names
    it); else None. A waiting candidate lives beside its voice only: no picker shows it on its own (SafeHouse lists it,
    marked, beside its voice). by_name: the voices already loaded, by name (else the owner's meta is read)."""
    owner = (v.extra or {}).get("pending_for")
    if not owner:
        return None
    if by_name is not None:
        o = by_name.get(owner)
        ex = (o.extra or {}) if o else {}
    else:
        try:
            ex = json.loads((voices_dir() / owner / "meta.json").read_text(encoding="utf-8")).get("extra") or {}
        except (OSError, ValueError):
            ex = {}
    # its voice gone (or no longer waiting on it): the candidate is listed like any voice, the only copy left
    return owner if (ex.get("improved_pending") or {}).get("voice") == v.name else None


def pending_candidate(name: str) -> str | None:
    """The voice a retrained candidate called `name` belongs to, while one waits under that name (its meta says
    pending_for, or it's <voice>-next while <voice>'s improved_pending names it); else None. Nothing writes over it:
    training, export and install all refuse (Internal Affairs P3-2)."""
    def extra(n: str) -> dict:
        try:
            return json.loads((voices_dir() / n / "meta.json").read_text(encoding="utf-8")).get("extra") or {}
        except (OSError, ValueError):
            return {}
    name = slug(name)
    if not name:
        return None
    if own := extra(name).get("pending_for"):
        return own
    base = name.removesuffix("-next")
    if base != name and (extra(base).get("improved_pending") or {}).get("voice") == name:
        return base
    return None


def listed_voices() -> list["Voice"]:
    """The voices people pick from: every voice but a retrained candidate waiting beside its own (list_voices has all)."""
    vs = list_voices()
    by_name = {v.name: v for v in vs}
    return [v for v in vs if not waiting(v, by_name)]


def get_voice(name: str) -> Voice:
    for v in list_voices():
        if v.name == slug(name):
            return v
    raise ModelError(f"There's no voice called {name!r} in your voices (it may have been set aside or removed).")


def _titled(s: str) -> str:
    """Title-case a name typed in lower case ("barack obama" -> "Barack Obama"); a name with capitals is kept."""
    s = re.sub(r"\s+", " ", s).strip()
    return s if any(c.isupper() for c in s) else s.title()


_TRAINED_SOURCE = re.compile(r"^trained in UnderCover: (?P<person>.+), [\d.]+ min, epoch \d+$")


def person_in_source(source: str) -> str:
    """The person named by the `source` of a voice trained here ("trained in UnderCover: Earth, Wind & Fire, 9.0 min,
    epoch 30"): everything before the minutes and epoch, commas included. "" when the text isn't in that form."""
    m = _TRAINED_SOURCE.match((source or "").strip())
    return m.group("person").strip() if m else ""


def trained_person(v: Voice) -> str:
    """The person a voice trained here was made for: `extra.person`, stored at export (phase 8), else read from
    `source` for voices exported before that."""
    stored = str((v.extra or {}).get("person") or "").strip()
    if stored:
        return stored
    src = v.source or ""
    return person_in_source(src) or (src.split(": ", 1)[1].split(",")[0].strip() if src.startswith("trained in UnderCover: ") else "")


def backfill_person() -> list[str]:
    """One time per voice: add `extra.person` to voices trained here before export stored it. Only that key is written
    into meta.json (nothing renamed or moved), after a byte-for-byte copy of the file next to it
    (`meta.json.before-person`, kept); the rewrite is checked to equal the old content plus the one key. Voices whose
    `source` doesn't name the person in the known form are left as they are. -> the voices changed."""
    from . import fsutil
    done = []
    for meta in sorted(voices_dir().glob("*/meta.json")):
        try:
            d = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        ex = d.get("extra") or {}
        person = person_in_source(d.get("source") or "")
        if not ex.get("trained") or str(ex.get("person") or "").strip() or not person:
            continue
        new = {**d, "extra": {**ex, "person": person}}
        text = json.dumps(new, indent=2)
        back = json.loads(text)
        back["extra"].pop("person")
        if back != d:   # never write anything but the one new key
            continue
        bak = meta.with_name("meta.json.before-person")
        if not bak.exists():
            shutil.copyfile(meta, bak)
        fsutil.write_text(meta, text)
        done.append(meta.parent.name)
    return done


def default_display(v: Voice) -> str:
    """Display name when none was set: the person for a voice trained here; for a downloaded one extra.person if
    known, else the folder name cleaned up ("michael-jackson-botdf-era" -> "Michael Jackson Botdf Era")."""
    ex = v.extra or {}
    person = trained_person(v) if ex.get("trained") else str(ex.get("person") or "").strip()
    if person:
        return _titled(person)
    folder = v.name.removesuffix("-uc") if ex.get("trained") else v.name
    return _titled(re.sub(r"[-_\s]+", " ", folder)) or v.name


def display_name(v: Voice) -> str:
    return (v.display or "").strip() or default_display(v)


_LABELS: dict = {"dir": None, "at": 0.0, "map": {}}


def voice_labels() -> dict[str, str]:
    """{voice id: display name} for every installed voice (server-side strings show these; ids stay in the data).
    Kept for two seconds per voices folder: job lists and the queue status ask for it on every poll."""
    d = str(voices_dir())
    if _LABELS["dir"] != d or time.time() - _LABELS["at"] > 2.0:
        _LABELS.update(dir=d, at=time.time(), map={v.name: display_name(v) for v in list_voices()})
    return _LABELS["map"]


def label_of(name: str | None, labels: dict[str, str] | None = None) -> str:
    """One voice id as shown to people; an unknown id (a removed voice) shows as it is."""
    if not name:
        return name or ""
    return (labels if labels is not None else voice_labels()).get(name, name)


DEMO_VOICE = "agent-smoke"   # the demo voice's folder (app/firstrun.py seeds it from assets/demo-voice/agent-smoke)


def is_demo(v: Voice) -> bool:
    """The demo voice (phase 17, "Agent Smoke"): synthetic, not a real person. Nothing searches for its "person"."""
    return bool((v.extra or {}).get("demo"))


# How alike a voice sounds, in a word for the pages (phase 19). Two scales: a voice trained here has its likeness at
# export (its own clips; on the user's voices: Peter Griffin 0.50 "weak" by the gate, Obama 0.70 and SpongeBob 0.77
# approved by ear, Freeman 0.80); a downloaded voice has the voice check's score against the person (good from 0.45,
# weak from 0.30; the ones it called bad or broken sit near 0, so they're "poor", not unmeasured).
# One scale for every voice, trained or downloaded (Internal Affairs P8-11: a downloaded Plankton at 0.757 read "great"
# beside Morgan Freeman's 0.795 "good"). Both numbers are the same kind: speaker-embedding likeness to the person. The
# voice lab's frozen test (docs/research/phase26-improve.md, branch phase26-improve) finds a person's voices saturate
# at 0.75-0.85 with about +-0.01-0.015 scoring noise, so "great" starts at 0.80, "good" covers the usual 0.70-0.80
# band, "fair" is a voice that's like them but clearly short, and "poor" is unlike them.
LIKENESS_WORDS = ((0.80, "great"), (0.70, "good"), (0.55, "fair"), (-9.0, "poor"))
TRAINED_WORDS = CHECKED_WORDS = LIKENESS_WORDS
_QCACHE: dict = {"key": None, "data": {}}


def _checked_score(v: Voice) -> float | None:
    """The voice check's score for a downloaded voice (the DoubleAgent quality cache, read only): for its own person
    when the voice knows it (None if never checked for them: another person's check says nothing), else its best."""
    f = path("cache_dir") / "voice_quality.json"
    try:
        key = (str(f), f.stat().st_mtime_ns)
        if _QCACHE["key"] != key:
            _QCACHE.update(key=key, data=json.loads(f.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None
    ex = v.extra or {}
    names = {v.name, ex.get("original_name")} - {None}
    hits = [(k, q) for k, q in _QCACHE["data"].items() if q.get("voice") in names and q.get("score") is not None]
    pick = [q for k, q in hits if k.split("|")[0] == slug(ex["person"])] if ex.get("person") else [q for k, q in hits]
    return max(float(q["score"]) for q in pick) if pick else None


def word_of(score, words=TRAINED_WORDS) -> str | None:
    """A likeness number as people read it ("great" / "good" / "fair"), None when there's none."""
    if not isinstance(score, (int, float)):
        return None
    return next(w for cut, w in words if score >= cut)


_LIKE: dict = {}   # project.json (path, mtime) -> its saves' identities by epoch


def trained_likeness(v: Voice) -> float | None:
    """A trained voice's likeness: extra.likeness (written at export), else the identity its training project measured
    for the very save it was made from (same run: the project still names this voice and that epoch's save is there).
    Read from the project's file (core reads no module)."""
    ex = v.extra or {}
    if ex.get("likeness") is not None:
        return ex["likeness"]
    if not ex.get("project") or ex.get("epoch") is None:
        return None
    f = path("cache_dir") / "training" / ex["project"] / "project.json"
    try:
        key = (str(f), f.stat().st_mtime_ns)
        if key not in _LIKE:
            st = (json.loads(f.read_text(encoding="utf-8")).get("train") or {})
            _LIKE.clear()   # one project at a time is plenty: lists read each voice's own once per change
            _LIKE[key] = (st.get("exported"), {s.get("epoch"): s.get("identity") for s in st.get("saves", [])})
        exported, ids = _LIKE[key]
    except (OSError, ValueError, AttributeError):
        return None
    return ids.get(ex["epoch"]) if exported in (v.name, ex.get("pending_for")) else None


def likeness_word(v: Voice) -> str | None:
    """"great" | "good" | "fair" ("poor": a downloaded voice the check found unlike the person) | None (not measured)."""
    ex = v.extra or {}
    if ex.get("trained"):
        return word_of(trained_likeness(v))
    return word_of(_checked_score(v), CHECKED_WORDS)


_GUESS_NOISE = re.compile(r"(?i)\b(rvc\d*|v\d+|uc|model|voice|ai|hq|e\d+|s\d+|\d+k?)\b|rvc\d*$")
# words that are never a name (P9-5: "model-v2-final" became "Final", and Let it speak searched YouTube for it)
_NOT_A_NAME = {"final", "best", "new", "test", "my", "latest", "old", "fixed", "clean", "good", "better", "official",
               "edit", "remix", "version", "beta", "alpha", "copy", "backup", "sample", "trained", "pretrained", "pth",
               "index", "the", "a", "of", "and", "full", "small", "large", "big", "mini", "lite", "pro", "hd", "default",
               "singing", "speaking", "talking", "speech", "vocals", "vocal", "male", "female", "deep", "high", "low",
               "un", "nouveau", "neu", "train", "training", "epoch", "step", "steps", "mix", "custom"}


def _known_names() -> list[str]:
    """People and characters UnderCover has met: its projects' persons and the voice checks' persons (files only)."""
    out = []
    for f in (path("cache_dir") / "training").glob("*/project.json"):
        try:
            p = json.loads(f.read_text(encoding="utf-8")).get("person")
        except (OSError, ValueError):
            continue
        if p:
            out.append(str(p))
    try:
        for k in json.loads((path("cache_dir") / "voice_quality.json").read_text(encoding="utf-8")):
            out.append(k.split("|")[0].replace("-", " "))
    except (OSError, ValueError, AttributeError):
        pass
    return out


def voice_person(v: Voice) -> str | None:
    """Whose voice it is: a trained voice's person; a downloaded one's checked person (extra.person); else, for a
    downloaded voice that isn't the demo, a GUESS from its display name (the user's name for it, else the tidied folder
    name: "Squidward Tentacles"). Never stored, never the folder: renaming the display corrects it."""
    ex = v.extra or {}
    if ex.get("trained"):
        return trained_person(v) or None
    if str(ex.get("person") or "").strip():
        return ex["person"].strip()
    if is_demo(v):
        return None
    shown = re.sub(r"\([^)]*\)", " ", display_name(v))   # "Plankton (SpongeBob)": the show isn't the name
    words = [w for w in re.sub(r"\s+", " ", _GUESS_NOISE.sub(" ", shown)).strip(" -_").split()
             if w.lower().strip(".,'") not in _NOT_A_NAME and not w.isdigit()]
    if len(words) >= 2:
        return " ".join(words)
    if len(words) == 1 and len(words[0]) >= 3:   # one word: only a name UnderCover has met ("Obama" -> "Barack Obama")
        w = words[0].lower()
        for known in _known_names():
            kw = [x.lower() for x in re.split(r"[\s-]+", known) if x]
            if w in kw:
                return known.title() if known == known.lower() else known
    return None   # no good guess: the card offers Train my own


def person_guessed(v: Voice) -> bool:
    ex = v.extra or {}
    return not ex.get("trained") and not str(ex.get("person") or "").strip() and voice_person(v) is not None


def voice_info(v: Voice) -> dict:
    """The keys every voice API carries: `display` (string, may equal the id), `trained` (bool), `demo` (bool),
    `likeness_word` (see likeness_word) and `ear` ({up, down}: the user's thumbs on its covers and speech, or null;
    FrontMan's basic voice list puts liked voices first)."""
    from . import ear, voicepreview
    return {"display": display_name(v), "trained": bool((v.extra or {}).get("trained")), "demo": is_demo(v),
            "likeness_word": likeness_word(v), "ear": ear.taste(v.name), "waiting_beside": waiting(v),
            "preview": voicepreview.fresh(v)}   # its "Go undercover as ..." clip (the ▶ shows only then)


def set_display(name: str, text: str) -> Voice:
    """Set a voice's display name; empty clears it (back to the default). The folder never changes."""
    from . import fsutil
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) > 80:
        raise ModelError("That name is too long. Keep it under 80 characters.")
    v = get_voice(name)
    v.display = text
    fsutil.write_text(v.dir / "meta.json", json.dumps(v.to_json(), indent=2))
    _LABELS["at"] = 0.0   # the next label lookup reads the new name
    return v


def remove_voice(name: str):
    """The one code path that deletes a voice folder, and only for the CLI's remove and a one-shot's own
    just-downloaded community voices (tests/smoke.py u_no_voice_deletes holds the callers). The user's Delete sets the
    voice aside (remove_to_aside) and a replace does too (set_aside); deleting for good is SafeHouse's Set aside
    section (safehouse/library.remove_aside)."""
    v = get_voice(name)
    from . import storage
    if storage.ALLOWED_ROOTS is not None and (why := storage.refuse([v.dir])):
        raise ModelError(why)
    shutil.rmtree(v.dir)


# ---------------------------------------------------------------- CLI

def _fmt_size(n):
    return f"{n / 1e6:6.0f} MB" if n else "     ? MB"


def _cli_progress():
    last = [0.0]

    def cb(done, total):
        now = time.time()
        if now - last[0] > 0.5 or (total and done >= total):
            last[0] = now
            pct = f"{done / total * 100:5.1f}%" if total else ""
            print(f"\r  {done / 1e6:7.1f} MB {pct}", end="", flush=True)
    return cb


def main():
    ap = argparse.ArgumentParser(description="Manage RVC voice models.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("search"); s.add_argument("query", nargs="+"); s.add_argument("--limit", type=int)
    d = sub.add_parser("download", help="result number from last search, or a link")
    d.add_argument("src"); d.add_argument("--name"); d.add_argument("--overwrite", action="store_true")
    f = sub.add_parser("install", help="install from local file(s)")
    f.add_argument("files", nargs="+", type=Path); f.add_argument("--name", required=True)
    sub.add_parser("list")
    r = sub.add_parser("remove"); r.add_argument("name")
    a = ap.parse_args()

    try:
        if a.cmd == "search":
            res = search(" ".join(a.query), a.limit)
            if not res:
                print("No voices found. Try a shorter or different name.")
            for i, x in enumerate(res, 1):
                print(f"{i:3d}. {x.name:28.28s} {_fmt_size(x.size)}  {x.repo}/{x.file}"[:150])
            if res:
                print("\nInstall one with: python -m undercover.core.voices download <number> [--name NAME]")
        elif a.cmd == "download":
            src = a.src
            if src.isdigit():
                res = last_search()
                if not 1 <= int(src) <= len(res):
                    raise ModelError("That number isn't in the last search results. Run a search first.")
                src = res[int(src) - 1]
            v = download_and_install(src, a.name, _cli_progress(), a.overwrite)
            print(f"\nInstalled '{v.name}' ({v.version}, {v.sr} Hz, index: {'yes' if v.index else 'no'})")
        elif a.cmd == "install":
            v = install(a.files, a.name, source="local")
            print(f"Installed '{v.name}' ({v.version}, {v.sr} Hz, index: {'yes' if v.index else 'no'})")
        elif a.cmd == "list":
            vs = list_voices()
            if not vs:
                print("No voices installed yet. Try: python -m undercover.core.voices search <name>")
            for v in vs:
                print(f"{v.name:28s} {v.version:3s} {str(v.sr or '?'):>6s} Hz  index: {'yes' if v.index else 'no '}  {v.source[:70]}")
        elif a.cmd == "remove":
            remove_voice(a.name)
            print(f"Removed '{a.name}'.")
    except ModelError as e:
        sys.exit(f"\n{e}")


if __name__ == "__main__":
    main()
