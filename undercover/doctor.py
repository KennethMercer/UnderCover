"""python -m undercover.doctor: check this install and fix what's safe (the checks are core/doctor.py).

    python -m undercover.doctor                       fast checks (< 2 s, no GPU)
    python -m undercover.doctor --full                + every model file's checksum, the graphics card and its driver
    python -m undercover.doctor --json                the report as JSON (setup.ps1 -Repair reads it)
    python -m undercover.doctor fix <id> [--dry-run]  run one Fix: stale | models | model:<path> | packages | manifest
    python -m undercover.doctor bundle <file.zip>      save the diagnostics file (logs, record, report; no audio)
    python -m undercover.doctor manifest [--installed] [--no-hash]
                                                      write the install record (setup.ps1 runs it with --installed)

Exit code: 0 = no problems, 1 = warnings only, 2 = something is broken.
"""
import argparse
import json
import sys

from .core import doctor, manifest, setup_models
from .core import separate  # noqa: F401  (registers the separation models' pins)
from .core import cadence  # noqa: F401  (registers the cadence engines' model files)
from .speakeasy import tts  # noqa: F401  (registers Kokoro's)

MARK = {"ok": "  ok ", "info": " info", "warn": " WARN", "bad": "  BAD"}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m undercover.doctor", description="Check UnderCover's install")
    ap.add_argument("action", nargs="?", default="check", choices=["check", "fix", "manifest", "bundle"])
    ap.add_argument("id", nargs="?")
    ap.add_argument("--full", action="store_true", help="also every model file's checksum, the card and its driver")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="fix packages: show what would be installed, install nothing")
    ap.add_argument("--installed", action="store_true", help="manifest: written by the installer (not 'observed')")
    ap.add_argument("--no-hash", action="store_true", help="manifest: sizes only")
    a = ap.parse_args(argv)

    if a.action == "manifest":
        m = manifest.observe(kind="installed" if a.installed else "observed", hash_files=not a.no_hash,
                             progress=None if a.json else _bar)
        doctor.log(f"install record written ({m['kind']}) by the command line: {len(m['models'])} model files")
        print(json.dumps({"file": str(manifest.file()), "kind": m["kind"], "models": len(m["models"]),
                          "packages": len(m["packages"])}) if a.json else f"\nWrote {manifest.file()} ({m['kind']}).")
        return 0

    if a.action == "bundle":
        from .core import diagnostics
        rep = doctor.run(full=False)
        out = diagnostics.contents(rep)
        dest = a.id or out["name"]
        for f in out["files"]:
            print(f"  {f['name']:28} {f['size'] / 1e3:8.0f} KB  {f['about']}")
        print(out["never"])
        with open(dest, "wb") as fh:
            fh.write(diagnostics.build(rep))
        print(f"Saved {dest}")
        return 0

    if a.action == "fix":
        if not a.id:
            ap.error("which fix? (stale, models, model:<path>, packages, manifest)")
        return _fix(a.id, a.dry_run, a.json)

    rep = doctor.run(full=a.full, progress=None if a.json else _bar)
    if a.json:
        print(json.dumps(rep, indent=1))
    else:
        print(f"\rUnderCover {rep['about']['version']} · {rep['about']['folder']} · {rep['mode']} check in {rep['seconds']} s" + " " * 20)
        for r in rep["results"]:
            print(f"{MARK[r['state']]}  {r['title']}: {r['say']}")
            if r["fix"] and r["state"] in ("bad", "warn", "info"):
                print(f"        Fix: {r['fix']['label']}" + (f"  (python -m undercover.doctor fix {r['fix']['id']})"
                                                           if r["fix"]["how"] != "offer" else ""))
    c = rep["counts"]
    return 2 if c["bad"] else 1 if c["warn"] else 0


def _bar(f, done, total):
    print(f"\r  checking model files: {done / 1e9:.1f} of {total / 1e9:.1f} GB", end="", flush=True)


def _fix(fix_id: str, dry_run: bool, as_json: bool) -> int:
    out = {}
    try:
        if fix_id == "packages":
            out = doctor.fix_packages(dry_run=dry_run)
        elif fix_id == "stale":
            r = doctor.clear_stale()
            out = {"message": f"Cleared {len(r['removed'])} leftover(s)." if r["removed"] else (r["why"] or "Nothing to clear."), **r}
        elif fix_id == "manifest":
            m = manifest.observe(progress=_bar)
            out = {"message": f"Wrote {manifest.file()} ({len(m['models'])} model files)."}
        elif fix_id in ("models",) or fix_id.startswith("model:"):
            rep = doctor.run(full=False)
            todo = [r["id"][6:] for r in rep["results"] if r["id"].startswith("model:") and r["state"] == "bad"] \
                if fix_id == "models" else [fix_id[6:]]
            for local in todo:
                if dry_run:
                    print(f"would download {local} from {setup_models.PINNED[local]['urls'][0]}")
                    continue
                doctor.log(f"re-downloading {local} (command line)")
                setup_models.refetch(local, None if as_json else _bar)
                doctor.log(f"re-downloaded {local}: checksum ok")
            out = {"message": f"{'Would download' if dry_run else 'Downloaded'} {len(todo)} file(s) again.", "files": todo}
        else:
            print(f"There's no fix called '{fix_id}' here.", file=sys.stderr)
            return 2
    except Exception as e:   # noqa: BLE001
        print(json.dumps({"error": str(e)}) if as_json else f"\nFix failed: {e}", file=sys.stderr)
        return 2
    print(json.dumps(out) if as_json else "\n" + out.get("message", "Done."))
    return 0


if __name__ == "__main__":
    sys.exit(main())
