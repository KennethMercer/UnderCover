"""python -m undercover.models: the AI models every feature uses, downloaded by setup (rc-fix4b, the user: "we absolutely
should not be downloading modules the first time they're called, it should be part of the install").

    python -m undercover.models install [--status] [--cancel-file <file>]   (setup's step; a repair runs it too)
    python -m undercover.models check [--json]                               (what's missing; exit 1 if anything is)

Every file is pinned (core/setup_models.PINNED: its sources, its SHA-256, its size), fetched through a .part that a
later run resumes, and swapped in only once its checksum matches. --status prints one plain line per file (setup keeps
them in its log) and '@@P <per mille>|<text>' at most once a second (setup turns them into the wizard's step bar).
--cancel-file: the installer's Cancel stops it within a megabyte (exit code 3), what's downloaded kept.

The set: covers (the vocal, de-reverb and backing-vocal separation models; HuBERT and RMVPE), training (the base models
for every sample rate; the speaker model), speech (the Reader's Kokoro voice and the Chatterbox Turbo engine) and
listen-back (Whisper). Not here: the optional add-ons (their own step when ticked) and the standard Chatterbox engine,
a Deep Cover choice that downloads when it's picked, saying so with its progress.
"""
import argparse
import json
import sys
from pathlib import Path

from .core import setup_models
from .core import separate   # noqa: F401  (registers the separation models)
from .core.cadence import engines   # noqa: F401  (registers the speech engines and Whisper)
from .speakeasy import tts   # noqa: F401  (registers Kokoro)

PARTS = {   # what a part is called on screen
    "separation": "the separation models",
    "rvc": "the voice models",
    "speaker": "the speaker model",
    "kokoro": "the Reader's voice",
    "chatterbox-turbo": "the speech engine",
    "whisper": "the speech recogniser",
}


def wanted() -> dict:
    """{path under models_dir: bytes} of every model setup installs."""
    return {f: r["size"] for f, r in setup_models.PINNED.items() if r["part"] in PARTS}


def missing() -> dict:
    return setup_models.missing(wanted())


def install(progress=None, cancel=None) -> dict:
    """Fetch every missing model file. progress(message, done, total) per chunk; cancel() -> True stops (InterruptedError).
    -> {"downloaded": [paths], "bytes": n}"""
    separate.place_model_list()
    todo = missing()
    files = list(todo)
    for i, local in enumerate(files, 1):
        what = PARTS[setup_models.PINNED[local]["part"]]
        msg = f"Downloading {what} (file {i} of {len(files)})"

        def cb(_f, done, total, msg=msg):
            if cancel and cancel():
                raise InterruptedError
            if progress:
                progress(msg, done, total)
        setup_models.refetch(local, progress=cb)
    return {"downloaded": files, "bytes": sum(todo.values())}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m undercover.models", description="UnderCover's AI models")
    ap.add_argument("action", choices=["install", "check"])
    ap.add_argument("--status", action="store_true", help="install, for the installer: '@@P' progress lines")
    ap.add_argument("--cancel-file", help="install: stop (exit code 3, what's downloaded kept) once this file exists")
    ap.add_argument("--json", action="store_true", help="check: machine-readable")
    a = ap.parse_args(argv)
    if a.action == "check":
        todo = missing()
        if a.json:
            print(json.dumps({"missing": todo, "wanted_bytes": sum(wanted().values())}))
        else:
            print("Every model is in place." if not todo else
                  f"Missing: {len(todo)} file(s), {sum(todo.values()) / 1e9:.1f} GB: " + ", ".join(todo))
        return 1 if todo else 0
    todo = missing()
    need = sum(todo.values())
    if not todo:
        separate.place_model_list()
        print(f"Every model is in place ({sum(wanted().values()) / 1e9:.1f} GB).", flush=True)
        return 0
    print(f"Downloading {len(todo)} model file(s), {need / 1e9:.1f} GB.", flush=True)
    if a.status:
        from .addons import status_printer
        progress = status_printer(need / 1e6)
    else:
        progress = lambda m, d, t: print(f"\r{m}: {d / 1e6:.0f}/{t / 1e6:.0f} MB", end="", flush=True)   # noqa: E731
    cancel = (lambda: Path(a.cancel_file).exists()) if a.cancel_file else None
    try:
        r = install(progress, cancel)
    except InterruptedError:
        print("\nStopped, as asked. What's downloaded is kept, and the next run carries on.", flush=True)
        return 3
    except setup_models.ModelError as e:
        print(f"\n{e}", file=sys.stderr, flush=True)
        return 2
    print(f"\n{len(r['downloaded'])} model file(s) downloaded, every one checked against its SHA-256.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
