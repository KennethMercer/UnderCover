"""A voice as one file: `.ucvoice` (1.1). A zip holding the voice's RVC weights as safetensors (never a pickle), its
search index in the compact fp16 form, and a JSON manifest that says what the voice is, where it came from, who it is
of and on what basis it is shared. Export and import live in SafeHouse (Library, a voice's detail).

    manifest.json        the manifest, canonical JSON (sorted keys, no whitespace, UTF-8): signable later as it is
    weights.safetensors  the generator's tensors; the .pth's other entries (config, sr, f0, version, info) as JSON in
                         its metadata ("rvc")
    index.ucindex        optional: the compact fp16 index (core/engine/rvc/index.py), raw numbers, no code

Import never unpickles anything from a pack: a member ending .pth / .pt / .ckpt / .pkl / .pickle / .bin refuses the whole
pack; the weights are read with safetensors and the voice's model.pth is written here, from those tensors, then checked
like a community download's (voices.inspect_pth, weights_only). Every member is checked against the manifest's size and
SHA-256 before anything is written, and the import goes through the staging folder (voicepreview.import_voice): it never
lands over an existing voice.

The manifest carries, from day one, what the sharing network drafted for 2.0 needs (the maintainers' notes, p2p design,
section 2), filled where 1.1 can and left empty where 2.0 fills them: pack_id, publisher, signatures [], lineage,
subject, licence, engine, test, min_app, created, replaces, files. A cadence pack never travels in 1.1 (it holds real
clips of the person): `cadence.included` is false and says why.
"""
import hashlib
import json
import re
import shutil
import time
import zipfile
from pathlib import Path

from . import fsutil
from .config import CONFIG, path
from .voices import ModelError

FORMAT = "ucvoice"
FORMAT_VERSION = 1
MIN_APP = "1.1.0"                 # the first UnderCover that reads format 1
SUFFIX = ".ucvoice"
MANIFEST, WEIGHTS, INDEX = "manifest.json", "weights.safetensors", "index.ucindex"
MEMBERS = (MANIFEST, WEIGHTS, INDEX)
PICKLES = (".pth", ".pt", ".ckpt", ".pkl", ".pickle", ".bin", ".npy", ".npz", ".joblib")
SUBJECT_KINDS = ("self", "public_figure", "private_person", "fictional", "synthetic")
CONSENTS = ("self", "written", "none", "unknown")
BASIS_MAX = 500
MANIFEST_MAX = 1 << 20            # a manifest bigger than a megabyte isn't one
CADENCE_NOTE = "Cadence packs hold real clips of the person, so they stay on the PC that made them (UnderCover 1.1)."
PRINT = [None]                    # (voice) -> 256 floats or None: the person's voice print (DoubleAgent's, set by the app)


class PackError(ModelError):
    """A pack that can't be read, made or used: said as it is (a ModelError: the message is meant for the user)."""


def _err(msg: str):
    raise PackError(msg)


# ---------------------------------------------------------------- the manifest

def canonical(obj) -> bytes:
    """The manifest's one byte form: sorted keys, no insignificant whitespace, UTF-8 (what a signature covers)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def pack_id(manifest: dict) -> str:
    """The pack's content address: SHA-256 of the canonical manifest without `pack_id` and `signatures` (so it can be
    written into the manifest it names, and signatures added later don't change it)."""
    body = {k: v for k, v in manifest.items() if k not in ("pack_id", "signatures")}
    return hashlib.sha256(canonical(body)).hexdigest()


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check_subject(subject, publishing: bool = True) -> dict:
    """The likeness declaration, checked: kind and consent from their lists, basis 1-500 characters, contact optional.
    A private person shared without their consent (none / unknown) is refused (the p2p design's default table)."""
    if not isinstance(subject, dict):
        _err("Say whose voice this is before sharing it.")
    kind, consent = subject.get("kind"), subject.get("consent")
    if kind not in SUBJECT_KINDS:
        _err("Whose voice is it: yours, a public figure's, a private person's, a fictional character's or a made-up one?")
    if consent not in CONSENTS:
        _err("Did they agree to it being shared: it's yours, you hold their written agreement, no, or you don't know?")
    basis = str(subject.get("basis") or "").strip()
    if not 1 <= len(basis) <= BASIS_MAX:
        _err(f"Say in a few words where the clips came from and why it's fine to share (up to {BASIS_MAX} characters).")
    if kind == "private_person" and consent in ("none", "unknown"):
        _err("A private person's voice is only shared with their agreement: it's theirs to give." if publishing else
             "This pack is a private person's voice shared without their agreement, so it isn't opened.")
    if kind == "private_person" and not str(subject.get("contact") or "").strip():
        _err("A private person's voice needs a way for them to reach you (the contact), in case they want it taken down.")
    return {"kind": kind, "name": str(subject.get("name") or "").strip()[:120], "consent": consent, "basis": basis,
            "contact": str(subject.get("contact") or "").strip()[:200]}


def default_subject(v) -> dict:
    """What the export form starts from: the demo voice is synthetic; a voice trained here or downloaded is of the
    person it was made for (a public figure until the exporter says otherwise), consent unknown, the basis empty."""
    from .voices import is_demo, voice_person
    if is_demo(v):
        return {"kind": "synthetic", "name": "", "consent": "self",
                "basis": "Made for UnderCover from a public-domain reading by a synthetic voice; not a real person.", "contact": ""}
    return {"kind": "public_figure", "name": voice_person(v) or "", "consent": "unknown", "basis": "", "contact": ""}


# ---------------------------------------------------------------- export

def _rvc_meta(ck: dict) -> dict:
    """The .pth's entries other than the weights, as JSON (anything that isn't JSON is left out: a voice needs none)."""
    out = {}
    for k, v in ck.items():
        if k == "weight":
            continue
        try:
            json.dumps(v)
            out[k] = v
        except (TypeError, ValueError):
            pass
    return out


def export(name: str, subject: dict, licence: dict | None = None, include_print: bool = False,
           dest_dir: Path | None = None) -> Path:
    """The voice `name` as one `.ucvoice` file in dest_dir (default cache/packs). -> its path."""
    import torch
    from safetensors.torch import save_file
    from . import about, voicepreview
    from .engine.rvc.index import compact_in_place, is_compact
    from .voices import display_name, get_voice, person_guessed, voice_person
    v = get_voice(name)
    sub = check_subject(subject)
    ck = torch.load(v.pth, map_location="cpu", weights_only=True)
    if not isinstance(ck, dict) or not isinstance(ck.get("weight"), dict):
        _err(f"{display_name(v)}'s model file isn't the kind UnderCover can pack (no weights in it).")
    out_dir = Path(dest_dir or (path("cache_dir") / "packs"))
    out_dir.mkdir(parents=True, exist_ok=True)
    work = out_dir / f".pack-{v.name}-{time.time_ns()}"
    work.mkdir()
    try:
        tensors = {k: t.detach().contiguous().clone() for k, t in ck["weight"].items() if hasattr(t, "detach")}
        save_file(tensors, str(work / WEIGHTS), metadata={"rvc": json.dumps(_rvc_meta(ck), sort_keys=True)})
        files = [work / WEIGHTS]
        if v.index and Path(v.index).is_file():
            shutil.copyfile(v.index, work / INDEX)
            if not is_compact(work / INDEX):
                compact_in_place(work / INDEX)   # (the compact fp16 form: half the size, no faiss)
            files.append(work / INDEX)
        ex = v.extra or {}
        lic = licence if isinstance(licence, dict) else {}
        print_ = None
        if include_print and PRINT[0] is not None:
            try:
                p = PRINT[0](v)
                print_ = [round(float(x), 6) for x in p] if p is not None else None
            except Exception:   # noqa: BLE001 - the print is optional
                print_ = None
        rng = ex.get("person_range") or ex.get("f0_range")
        manifest = {
            "format": FORMAT, "format_version": FORMAT_VERSION, "min_app": MIN_APP,
            "app": about.VERSION, "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "replaces": None,
            "publisher": {"key": "", "name": "", "feed": ""}, "signatures": [],
            "lineage": {"parent": None, "root": None, "generation": 0},
            "voice": {"name": v.name, "display": display_name(v),   # (a checked or trained person only, never the app's guess)
                      "person": "" if person_guessed(v) else (voice_person(v) or "")},
            "subject": sub,
            "provenance": {"origin": "trained here" if ex.get("trained") else "downloaded", "source": v.source or "",
                           "installed": v.installed or "", "demo_of": "agent-smoke" if ex.get("demo") else ""},
            "licence": {"id": str(lic.get("id") or "").strip()[:64], "url": str(lic.get("url") or "").strip()[:300],
                        "text": str(lic.get("text") or "").strip()[:2000]},
            "engine": {"family": "rvc", "version": v.version, "sr": v.sr, "f0": bool(v.f0),
                       "f0_range": [round(float(x), 1) for x in rng][:3] if isinstance(rng, (list, tuple)) and len(rng) >= 3 else None},
            "test": {"print": print_, "publisher_scores": {k: ex.get(k) for k in ("likeness", "likeness_word", "word_error")
                                                          if ex.get(k) is not None}},
            "preview": {"line": voicepreview.line_for(v)},
            "cadence": {"included": False, "why": CADENCE_NOTE},
            "files": [{"name": f.name, "bytes": f.stat().st_size, "sha256": _sha256(f)} for f in files],
        }
        manifest["pack_id"] = pack_id(manifest)
        dest = out_dir / f"{v.name}-{manifest['pack_id'][:8]}{SUFFIX}"
        tmp = out_dir / f"{dest.name}.part"
        with zipfile.ZipFile(tmp, "w") as z:
            z.writestr(zipfile.ZipInfo(MANIFEST, date_time=(2026, 1, 1, 0, 0, 0)), canonical(manifest), zipfile.ZIP_DEFLATED)
            for f in files:   # (the weights and index barely compress: stored, which also makes them quick to check)
                z.write(f, f.name, compress_type=zipfile.ZIP_STORED)
        fsutil._replace(tmp, dest)
        return dest
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------- reading a pack

def read_manifest(pack: Path) -> dict:
    """The manifest of a pack, checked as far as can be without its files (the zip, the members, the format, the
    subject, the sizes). Raises PackError with the reason."""
    try:
        z = zipfile.ZipFile(pack)
    except (zipfile.BadZipFile, OSError) as e:
        _err(f"That isn't a voice pack (it isn't a zip file: {type(e).__name__}).")
    with z:
        names = [i.filename for i in z.infolist()]
        if len(set(names)) != len(names):   # (two members of one name: which one a reader takes is up to the reader)
            _err("The pack holds two files of the same name, so it isn't opened.")
        bad = [n for n in names if n.lower().endswith(PICKLES)]
        if bad:
            _err(f"The pack holds {bad[0]}, a kind of file that can carry code, so it isn't opened. A voice pack carries "
                 "its weights as safetensors only.")
        odd = [n for n in names if n not in MEMBERS]
        if odd:
            _err(f"The pack holds {odd[0]}, which a voice pack doesn't have, so it isn't opened.")
        if MANIFEST not in names or WEIGHTS not in names:
            _err("The pack has no manifest or no weights, so it isn't a voice pack.")
        info = z.getinfo(MANIFEST)
        if info.file_size > MANIFEST_MAX:
            _err("The pack's manifest is too big to be one.")
        try:
            m = json.loads(z.read(MANIFEST).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            _err("The pack's manifest can't be read.")
        if not isinstance(m, dict) or m.get("format") != FORMAT:
            _err("That isn't a voice pack (its manifest says another format).")
        if not isinstance(m.get("format_version"), int) or m["format_version"] > FORMAT_VERSION:
            _err(f"This voice pack needs a newer UnderCover ({m.get('min_app') or 'a later version'}).")
        files = m.get("files")
        if not isinstance(files, list) or not files:
            _err("The pack's manifest lists no files.")
        listed = {f.get("name"): f for f in files if isinstance(f, dict)}
        cap = float((CONFIG.get("voices") or {}).get("max_download_gb", 2.0)) * 1e9
        total = 0
        for n in names:
            if n == MANIFEST:
                continue
            f = listed.get(n)
            if f is None or not isinstance(f.get("bytes"), int) or not re.fullmatch(r"[0-9a-f]{64}", str(f.get("sha256") or "")):
                _err(f"The pack's manifest doesn't vouch for {n} (its size and SHA-256).")
            if z.getinfo(n).file_size != f["bytes"]:
                _err(f"{n} in the pack isn't the size its manifest says.")
            total += f["bytes"]
        if set(listed) != set(names) - {MANIFEST}:
            _err("The pack's manifest lists a file the pack doesn't have.")
        if total > cap:
            _err(f"The pack is {total / 1e9:.1f} GB, over the {cap / 1e9:.0f} GB limit for a voice.")
    if m.get("pack_id") != pack_id(m):
        _err("The pack's manifest has been changed since it was made (its pack id doesn't match), so it isn't opened.")
    m["subject"] = check_subject(m.get("subject"), publishing=False)
    eng = m.get("engine") or {}
    if eng.get("family") != "rvc":
        _err(f"This pack is a {eng.get('family') or 'different'} voice; UnderCover reads RVC voices.")
    return m


def stage(pack: Path, name: str | None = None):
    """A pack -> a candidate voice in the staging folder (not in any voice list), each file checked against the
    manifest first; the weights read with safetensors and written here as the voice's model.pth, then checked like a
    community download's. For voicepreview.import_voice: it makes the preview line and lists it. -> the staged Voice."""
    import torch
    from safetensors import safe_open
    from .engine.rvc.index import IndexFormatError, is_compact, read_vectors
    from .voices import Voice, inspect_pth, slug, staging_dir, voices_dir
    m = read_manifest(pack)
    vi = m.get("voice") or {}
    root = staging_dir()
    base = slug(name or vi.get("name") or "voice") or "voice"
    final, n = base, 1
    while (voices_dir() / final).exists() or (root / final).exists():   # (never over a voice: its own name, or <name>-2...)
        n += 1
        final = f"{base}-{n}"
    root.mkdir(parents=True, exist_ok=True)
    work = root / f".unpack-{time.time_ns()}"
    work.mkdir()
    try:
        listed = {f["name"]: f for f in m["files"]}
        with zipfile.ZipFile(pack) as z:
            for n in listed:
                h, size = hashlib.sha256(), 0
                with z.open(n) as src, open(work / n, "wb") as out:
                    for chunk in iter(lambda: src.read(1 << 20), b""):
                        size += len(chunk)
                        if size > listed[n]["bytes"]:
                            _err(f"{n} in the pack is bigger than its manifest says.")
                        h.update(chunk)
                        out.write(chunk)
                if size != listed[n]["bytes"] or h.hexdigest() != listed[n]["sha256"]:
                    _err(f"{n} in the pack isn't the file its manifest names (SHA-256), so the pack isn't opened.")
        with safe_open(str(work / WEIGHTS), framework="pt", device="cpu") as f:
            meta = json.loads((f.metadata() or {}).get("rvc") or "{}")
            weight = {k: f.get_tensor(k) for k in f.keys()}
        if not isinstance(meta, dict) or "config" not in meta:
            _err("The pack's weights don't say how the voice is built (no config), so it can't be used.")
        target = root / final
        target.mkdir()
        fsutil.torch_save({**meta, "weight": weight}, target / "model.pth")
        info = inspect_pth(target / "model.pth")   # (the community download's check: weights_only, an RVC model)
        index = None
        if INDEX in listed:
            if not is_compact(work / INDEX):
                _err("The pack's index isn't in UnderCover's compact form, so it isn't used.")
            try:
                read_vectors(work / INDEX)
            except IndexFormatError as e:
                _err(f"The pack's index can't be read ({e}).")
            shutil.move(str(work / INDEX), target / "model.index")
            index = target / "model.index"
        sub, prov = m["subject"], m.get("provenance") or {}
        eng = m.get("engine") or {}
        extra = {"pack": {"pack_id": m["pack_id"], "created": m.get("created"), "lineage": m.get("lineage"),
                          "subject": sub, "licence": m.get("licence"), "publisher": m.get("publisher"),
                          "origin": prov.get("origin"), "source": prov.get("source")},
                 "trained": False}
        if sub["kind"] != "synthetic" and (sub.get("name") or vi.get("person")):
            extra["person"] = sub.get("name") or vi.get("person")
        if eng.get("f0_range"):
            extra["f0_range"] = eng["f0_range"]
        v = Voice(name=final, dir=target, pth=target / "model.pth", index=index, version=info["version"],
                  sr=info["sr"] or eng.get("sr") or 40000, f0=info["f0"],
                  source=f"voice pack {m['pack_id'][:12]} ({Path(pack).name})", installed=time.strftime("%Y-%m-%d %H:%M"),
                  extra=extra, display=str(vi.get("display") or "")[:80])
        fsutil.write_text(target / "meta.json", json.dumps(v.to_json(), indent=2))
        return v
    except BaseException:
        from .voices import discard_staged
        discard_staged(final)   # (inside the staging folder only)
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)


def import_pack(pack: Path, job=None, name: str | None = None, wait: float = 120.0):
    """A pack into the voices, as a community download is: staged, its preview line made, then listed under its own
    name or the first free <name>-2 (never over an existing voice). -> the listed Voice."""
    from . import voicepreview
    return voicepreview.import_voice(lambda: stage(Path(pack), name), job, wait=wait)
