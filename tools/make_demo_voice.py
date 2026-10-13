"""Build the demo voice "Agent Smoke" (phase 17): an RVC voice trained from Kokoro's synthetic reader, not a real person.

    .venv\\Scripts\\python.exe tools\\make_demo_voice.py [--minutes 20] [--reader bm_george]

Everything happens in smoke's sandbox server (scratch library, cache and voices; the real models read only), never in the
user's folders:
  1. the text: Joseph Conrad's "The Secret Agent" (1907; public domain everywhere), Project Gutenberg #974;
  2. Kokoro (Apache-2.0) reads it sentence by sentence with one built-in reader (default bm_george) into ~2-minute files;
  3. DoubleAgent's normal pipeline in the sandbox: a project in speaking mode, the files uploaded as its sources (so it
     never searches YouTube), every one of them prepared (Do it all alone stops at [training] target_minutes, 10, and
     the demo voice wants all the speech: it needs the range), then "Do it all" (confirm, train, add the voice);
  4. the voice folder is copied to undercover/assets/demo-voice/agent-smoke/ with its meta marked as the demo, and a
     manifest.json (each file's size and SHA-256, the reader, the text) is written next to it.
A pause (the GPU wanted for something else) keeps the training: the project is copied out of the sandbox before it goes
(to <audio-dir>/agent-smoke-paused, or the temp folder), and --resume <that folder> carries on from its last epoch in a
new sandbox (a pause on 2026-10-12 lost a training 10 epochs from the end, because the sandbox took resume.pt with it).
The model files are git-ignored (too large for the repository, MasterPlan 2026-10-12): meta.json and manifest.json are
committed, the installer (built on this PC) packs the folder from disk, and a fresh clone runs this script once
(--manifest-only rewrites the manifest for files already there). app/tutorial.py seeds it into the user's voices on a
first run and on Replay, after checking the files against the manifest.
"""
import argparse
import json
import re
import shutil
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

GUTENBERG = "https://www.gutenberg.org/cache/epub/974/pg974.txt"
OUT = ROOT / "undercover" / "assets" / "demo-voice" / "agent-smoke"
NOTE = "demo voice, made for UnderCover, not a real person"


def text_of_book() -> str:
    with urllib.request.urlopen(GUTENBERG, timeout=60) as r:
        raw = r.read().decode("utf-8", "replace")
    body = raw.split("*** START OF", 1)[1].split("***", 1)[1].split("*** END OF", 1)[0]
    body = body[body.find("CHAPTER I"):]   # after the author's note and contents
    body = re.sub(r"\r?\n(?!\r?\n)", " ", body)       # unwrap the lines, keep the paragraphs
    body = re.sub(r"\bCHAPTER [IVXL]+\b", "", body)
    return re.sub(r"[ \t]+", " ", body).strip()


def synthesize(text: str, reader: str, minutes: float, into: Path) -> list[Path]:
    """Kokoro reads sentences until `minutes` of speech in `into` (counting what's there already); ~2-minute WAV files
    with short pauses between sentences. agent-smoke-text.json beside them says where the reading stopped, so a top-up
    (more minutes later) reads on, new text, never the same sentences twice. -> every recording, old and new."""
    import numpy as np
    import soundfile as sf
    from undercover.speakeasy import tts
    have = sorted(into.glob("agent-smoke-*.wav"))
    total = sum(sf.info(str(f)).duration for f in have)
    sents = list(tts.sentences(text))
    mark = into / "agent-smoke-text.json"
    if mark.is_file():
        m = json.loads(mark.read_text(encoding="utf-8"))
        if m["reader"] != reader:
            sys.exit(f"{into} holds {m['reader']}'s reading, not {reader}'s: use another --audio-dir")
        start = m["next_sentence"]
    else:   # recordings from before the mark (the first 30 min, 2026-10-12): read on from the middle of the book
        start = len(sents) // 2 if have else 0
    if have:
        print(f"  {len(have)} recordings, {total / 60:.1f} min; reading on from sentence {start} of {len(sents)}")
    files, chunk, gap = list(have), [], np.zeros(int(tts.SR * 0.35), np.float32)
    for i in range(start, len(sents)):
        if total >= minutes * 60:
            break
        a = tts.synth(sents[i]["text"], reader)
        chunk += [a, gap]
        total += (len(a) + len(gap)) / tts.SR
        if sum(map(len, chunk)) / tts.SR >= 120 or total >= minutes * 60:
            f = into / f"agent-smoke-{len(files) + 1:02d}.wav"
            sf.write(f, np.concatenate(chunk), tts.SR)
            files.append(f)
            chunk = []
            mark.write_text(json.dumps({"reader": reader, "next_sentence": i + 1, "minutes": round(total / 60, 1)}),
                            encoding="utf-8")
            print(f"  {f.name}: {total / 60:.1f} min so far", flush=True)
    return files


INDEX_CENTRES = 10000   # build 4's 166,906 vectors (256 MB) -> 10k k-means centres (15 MB): likeness 0.928 -> 0.923,
                        # within run-to-run noise; Hold On sung through both: 0.996 alike (measured 2026-10-13)
PACK_TEXT = ("The city was quiet that night, and the lamps along the river burned low. "
             "Nobody saw him cross the bridge, and nobody asked where he was going.")


def shrink_index(folder: Path, k: int = INDEX_CENTRES) -> tuple[int, int]:
    """The voice's index as k k-means centres (core/engine/rvc/index.shrink, shared with exports and Make smaller)."""
    from undercover.core.engine.rvc import index as rvcindex
    return rvcindex.shrink(folder / "model.index", k)


def add_cadence_pack(folder: Path, reader: str, meta: dict) -> dict:
    """SpeakEasy reads him with Chatterbox (the user's blind pick over plain Kokoro, 2026-10-13): a cadence pack whose
    reference is his own reader saying PACK_TEXT (synthetic speech, nobody's voice). -> meta with extra.tts"""
    import numpy as np
    import soundfile as sf
    from undercover.speakeasy import tts
    (folder / "tts").mkdir(exist_ok=True)
    sf.write(folder / "tts" / "ref.wav", np.asarray(tts.synth(PACK_TEXT, reader), np.float32), tts.SR, subtype="PCM_16")
    meta.setdefault("extra", {})["tts"] = {"engine": "chatterbox", "cfg_weight": 0.3, "exaggeration": 0.5, "v": 1,
                                           "ref": "ref.wav", "ref_text": PACK_TEXT, "tempo": 1.0, "gap_s": 0.5,
                                           "para_gap_s": 0.8, "made": time.strftime("%Y-%m-%d"),
                                           "source": f"Kokoro {reader} (Apache-2.0), synthetic"}
    return meta


def finish_voice(folder: Path, reader: str, meta: dict) -> dict:
    """The steps after training: the index shrunk, the cadence pack added. -> the meta to write"""
    before, after = shrink_index(folder)
    print(f"index: {before} vectors -> {after} k-means centres ({(folder / 'model.index').stat().st_size / 1e6:.1f} MB)")
    return add_cadence_pack(folder, reader, meta)


def write_manifest(reader: str):
    import hashlib
    files = {}
    for name in ("model.pth", "model.index", "tts/ref.wav"):
        f = OUT / name
        if f.exists():
            h = hashlib.sha256()
            with open(f, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
            files[name] = {"size": f.stat().st_size, "sha256": h.hexdigest()}
    (OUT / "manifest.json").write_text(json.dumps({
        "voice": "agent-smoke", "made": time.strftime("%Y-%m-%d"), "reader": f"Kokoro {reader} (Apache-2.0)",
        "text": "Joseph Conrad, The Secret Agent (1907), Project Gutenberg #974, public domain",
        "recipe": "tools/make_demo_voice.py", "files": files}, indent=1) + "\n", encoding="utf-8")
    return files


def keep_paused(project: Path, keep: Path) -> Path:
    """A paused training's whole project (resume.pt, the clips, its choices) copied to `keep`, which outlives the
    sandbox. -> keep"""
    if keep.exists():
        shutil.rmtree(keep)
    shutil.copytree(project, keep)
    return keep


def restore_paused(kept: Path, projects: Path) -> str:
    """A kept project back into a (new) sandbox's training folder. -> the project's name"""
    name = json.loads((kept / "project.json").read_text(encoding="utf-8"))["name"]
    shutil.copytree(kept, projects / name)
    return name


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=20)
    ap.add_argument("--reader", default="bm_george")
    ap.add_argument("--audio-dir", help="keep the synthesized speech here and reuse it next time (it takes ~25 min)")
    ap.add_argument("--manifest-only", action="store_true", help="only (re)write manifest.json for the files already there")
    ap.add_argument("--resume", help="a paused training's project folder (printed when it paused): carry on from there")
    ap.add_argument("--refit", action="store_true",
                    help="no training: shrink the shipped voice's index and (re)make its cadence pack, then the manifest")
    a = ap.parse_args()
    if a.manifest_only:
        print(write_manifest(a.reader))
        return
    if a.refit:
        new, old = OUT.with_name(".agent-smoke-new"), OUT.with_name(".agent-smoke-old")
        for d in (new, old):
            if d.exists():
                shutil.rmtree(d)
        shutil.copytree(OUT, new, ignore=shutil.ignore_patterns("manifest.json"))
        meta = finish_voice(new, a.reader, json.loads((new / "meta.json").read_text(encoding="utf-8")))
        (new / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
        OUT.replace(old)
        new.replace(OUT)
        shutil.rmtree(old)
        print(write_manifest(a.reader))
        return
    import smoke
    import tempfile
    keep = Path(a.audio_dir or tempfile.gettempdir()) / "agent-smoke-paused"
    resume = Path(a.resume).resolve() if a.resume else None
    if resume and not (resume / "project.json").is_file():
        sys.exit(f"--resume: no project.json in {resume}")
    with smoke.sandbox_server(smoke.sandbox_port()) as root:
        from undercover.core.config import path
        from undercover.doubleagent import project as P

        def follow(job: str, what: str):
            t0, last = time.time(), ""
            while True:
                j = next((x for x in smoke.api("/api/jobs") if x["id"] == job), None)
                if j and j.get("final"):
                    break
                msg = (j or {}).get("message") or (j or {}).get("label") or ""
                if msg != last:
                    print(f"  [{(time.time() - t0) / 60:5.1f} min] {msg}", flush=True)
                    last = msg
                time.sleep(15)
            if j["final"]["state"] == "paused" and what == "training":
                # the sandbox goes when this block ends: the project (resume.pt and all) goes somewhere that stays
                keep_paused(P.projects_dir() / name, keep)
                sys.exit(f"training paused; kept in {keep}. Carry on with: --resume \"{keep}\"")
            if j["final"]["state"] != "done":
                log = path("logs_dir") / "errors.log"   # the sandbox goes when this block ends: show why first
                if log.exists():
                    print(log.read_text(encoding="utf-8", errors="replace")[-3000:])
                sys.exit(f"{what} ended as {j['final']['state']}: {j['final'].get('message')}")

        if resume:
            assert smoke.storage_inside(P.projects_dir(), root), P.projects_dir()
            name = restore_paused(resume, P.projects_dir())
            st = P.Project.load(name).train
            print(f"resuming {name} from epoch {st.get('epoch')} of its training (state {st.get('state')})")
        else:
            print("the text: The Secret Agent (Conrad, 1907), Project Gutenberg #974")
            work = Path(a.audio_dir) if a.audio_dir else root / "demo-voice-src"
            work.mkdir(parents=True, exist_ok=True)
            import soundfile as sf
            files = sorted(work.glob("agent-smoke-*.wav"))
            if files and sum(sf.info(str(f)).duration for f in files) >= a.minutes * 60 - 30:
                print(f"reusing {len(files)} recordings in {work}")
            else:
                print(f"Kokoro {a.reader} reads up to {a.minutes:.0f} min into {work}")
                files = synthesize(text_of_book(), a.reader, a.minutes, work)
            name = smoke.api("/api/train/projects", {"person": "Agent Smoke", "mode": "speaking", "no_search": True})["name"]
            for f in files:
                req = urllib.request.Request(f"{smoke.BASE}/api/train/projects/{name}/upload", data=f.read_bytes(), method="POST",
                                             headers={"X-Filename": f.name, "Origin": smoke.BASE})
                with urllib.request.urlopen(req, timeout=300) as r:
                    r.read()
            print(f"project {name}: {len(files)} recordings uploaded; preparing all of them")
            follow(smoke.api(f"/api/train/projects/{name}/prepare", {})["job"], "preparing")
            pr = P.Project.load(name)
            print(f"kept {pr.kept_seconds() / 60:.1f} min of {sum(s.duration for s in pr.sources) / 60:.1f}; "
                  "Do it all (confirm, train, add)")
        follow(smoke.api(f"/api/train/projects/{name}/auto", {"voice_name": "agent-smoke"})["job"], "training")
        src = path("voices_dir") / "agent-smoke"
        assert smoke.storage_inside(src, root), src   # the sandbox's voice, never a real one
        meta = json.loads((src / "meta.json").read_text(encoding="utf-8"))
        meta.update(name="agent-smoke", dir="", pth="model.pth", index="model.index" if (src / "model.index").exists() else "",
                    source=f"demo voice: Kokoro {a.reader} reading The Secret Agent (Conrad, public domain)",
                    display="Agent Smoke")
        meta.setdefault("extra", {}).update(demo=True, trained=False, person="Agent Smoke", note=NOTE, reader=a.reader,
                                            made=time.strftime("%Y-%m-%d"))
        meta["extra"].pop("project", None)
        # the model files aren't in git: build the new folder beside the old one and swap, so a failed copy loses nothing
        new, old = OUT.with_name(".agent-smoke-new"), OUT.with_name(".agent-smoke-old")
        for d in (new, old):
            if d.exists():
                shutil.rmtree(d)
        new.mkdir(parents=True)
        for f in ("model.pth", "model.index"):
            if (src / f).exists():
                shutil.copyfile(src / f, new / f)
        meta = finish_voice(new, a.reader, meta)
        (new / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
        if OUT.exists():
            OUT.replace(old)
        new.replace(OUT)
        if old.exists():
            shutil.rmtree(old)
    if resume and resume == keep.resolve():   # our own copy of a paused training (about 2 GB): done with it
        shutil.rmtree(resume)
    write_manifest(a.reader)
    size = sum(f.stat().st_size for f in OUT.iterdir())
    print(f"done: {OUT} ({size / 1e6:.0f} MB: " + ", ".join(f"{f.name} {f.stat().st_size / 1e6:.0f} MB" for f in OUT.iterdir()) + ")")


if __name__ == "__main__":
    main()
