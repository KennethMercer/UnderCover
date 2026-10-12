"""Build the demo voice "Agent Smoke" (phase 17): an RVC voice trained from Kokoro's synthetic reader, not a real person.

    .venv\\Scripts\\python.exe tools\\make_demo_voice.py [--minutes 20] [--reader bm_george]

Everything happens in smoke's sandbox server (scratch library, cache and voices; the real models read only), never in the
user's folders:
  1. the text: Joseph Conrad's "The Secret Agent" (1907; public domain everywhere), Project Gutenberg #974;
  2. Kokoro (Apache-2.0) reads it sentence by sentence with one built-in reader (default bm_george) into ~2-minute files;
  3. DoubleAgent's normal pipeline in the sandbox: a project in speaking mode, the files uploaded as its sources (so it
     never searches YouTube), "Do it all" (prepare, confirm, train, add the voice);
  4. the voice folder is copied to undercover/assets/demo-voice/agent-smoke/ with its meta marked as the demo, and a
     manifest.json (each file's size and SHA-256, the reader, the text) is written next to it.
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
    """Kokoro reads sentences until `minutes` of speech; ~2-minute WAV files with short pauses between sentences."""
    import numpy as np
    import soundfile as sf
    from undercover.speakeasy import tts
    files, chunk, total, gap = [], [], 0.0, np.zeros(int(tts.SR * 0.35), np.float32)
    for s in tts.sentences(text):
        a = tts.synth(s["text"], reader)
        chunk += [a, gap]
        total += (len(a) + len(gap)) / tts.SR
        if sum(map(len, chunk)) / tts.SR >= 120 or total >= minutes * 60:
            f = into / f"agent-smoke-{len(files) + 1:02d}.wav"
            sf.write(f, np.concatenate(chunk), tts.SR)
            files.append(f)
            chunk = []
            print(f"  {f.name}: {total / 60:.1f} min so far", flush=True)
        if total >= minutes * 60:
            break
    return files


def write_manifest(reader: str):
    import hashlib
    files = {}
    for name in ("model.pth", "model.index"):
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
        "recipe": "tools/make_demo_voice.py", "files": files}, indent=1) + "
", encoding="utf-8")
    return files


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=20)
    ap.add_argument("--reader", default="bm_george")
    ap.add_argument("--audio-dir", help="keep the synthesized speech here and reuse it next time (it takes ~25 min)")
    ap.add_argument("--manifest-only", action="store_true", help="only (re)write manifest.json for the files already there")
    a = ap.parse_args()
    if a.manifest_only:
        print(write_manifest(a.reader))
        return
    import smoke
    print("the text: The Secret Agent (Conrad, 1907), Project Gutenberg #974")
    text = text_of_book()
    with smoke.sandbox_server(smoke.sandbox_port()) as root:
        from undercover.core.config import path
        work = Path(a.audio_dir) if a.audio_dir else root / "demo-voice-src"
        work.mkdir(parents=True, exist_ok=True)
        files = sorted(work.glob("agent-smoke-*.wav"))
        if files:
            print(f"reusing {len(files)} recordings in {work}")
        else:
            print(f"Kokoro {a.reader} reads {a.minutes:.0f} min into {work}")
            files = synthesize(text, a.reader, a.minutes, work)
        p = smoke.api("/api/train/projects", {"person": "Agent Smoke", "mode": "speaking", "no_search": True})
        name = p["name"]
        for f in files:
            req = urllib.request.Request(f"{smoke.BASE}/api/train/projects/{name}/upload", data=f.read_bytes(), method="POST",
                                         headers={"X-Filename": f.name, "Origin": smoke.BASE})
            with urllib.request.urlopen(req, timeout=300) as r:
                r.read()
        print(f"project {name}: {len(files)} recordings uploaded; Do it all (prepare, confirm, train, add)")
        job = smoke.api(f"/api/train/projects/{name}/auto", {"voice_name": "agent-smoke"})["job"]
        t0, last = time.time(), ""
        while True:
            j = next((x for x in smoke.api("/api/jobs") if x["id"] == job), None)
            if j and j.get("final"):
                break
            msg = (j or {}).get("message") or ""
            if msg != last:
                print(f"  [{(time.time() - t0) / 60:5.1f} min] {msg}", flush=True)
                last = msg
            time.sleep(15)
        if j["final"]["state"] != "done":
            log = path("logs_dir") / "errors.log"   # the sandbox goes when this block ends: show why first
            if log.exists():
                print(log.read_text(encoding="utf-8", errors="replace")[-3000:])
            sys.exit(f"training ended as {j['final']['state']}: {j['final'].get('message')}")
        src = path("voices_dir") / "agent-smoke"
        assert smoke.storage_inside(src, root), src   # the sandbox's voice, never a real one
        meta = json.loads((src / "meta.json").read_text(encoding="utf-8"))
        meta.update(name="agent-smoke", dir="", pth="model.pth", index="model.index" if (src / "model.index").exists() else "",
                    source=f"demo voice: Kokoro {a.reader} reading The Secret Agent (Conrad, public domain)",
                    display="Agent Smoke")
        meta.setdefault("extra", {}).update(demo=True, trained=False, person="Agent Smoke", note=NOTE, reader=a.reader,
                                            made=time.strftime("%Y-%m-%d"))
        meta["extra"].pop("project", None)
        if OUT.exists():
            shutil.rmtree(OUT)
        OUT.mkdir(parents=True)
        for f in ("model.pth", "model.index"):
            if (src / f).exists():
                shutil.copyfile(src / f, OUT / f)
        (OUT / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    write_manifest(a.reader)
    size = sum(f.stat().st_size for f in OUT.iterdir())
    print(f"done: {OUT} ({size / 1e6:.0f} MB: " + ", ".join(f"{f.name} {f.stat().st_size / 1e6:.0f} MB" for f in OUT.iterdir()) + ")")


if __name__ == "__main__":
    main()
