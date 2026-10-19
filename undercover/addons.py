"""python -m undercover.addons: list, install or remove the add-ons (the registry is core/addons.py).

    python -m undercover.addons [list] [--json]
    python -m undercover.addons install <id> [--accept-licence]     (without it: shows the licence and asks)
        [--status] [--cancel-file <file>]   (the installer: progress as status lines; stops when the file appears)
    python -m undercover.addons remove <id>

The installer's optional add-ons step runs `install <id> --accept-licence` after the user ticked "I accept".
"""
import argparse
import json
import sys
import time
from pathlib import Path

from .core import addons
from .core import cadence  # noqa: F401  (registers the cadence engines' parts)
from .speakeasy import tts  # noqa: F401  (registers Kokoro)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m undercover.addons", description="UnderCover's parts and optional add-ons")
    ap.add_argument("action", nargs="?", default="list", choices=["list", "install", "remove"])
    ap.add_argument("id", nargs="?")
    ap.add_argument("--accept-licence", "--yes", dest="accept", action="store_true",
                    help="accept the add-on's licence without asking (only after its user has read it)")
    ap.add_argument("--json", action="store_true", help="list: machine-readable (the installer reads this when it's built)")
    ap.add_argument("--status", action="store_true",
                    help="install, for the installer: '@@P <per mille>|<text>' progress lines and one plain line per file")
    ap.add_argument("--cancel-file", help="install: stop (exit code 3, what's downloaded kept) once this file exists")
    args = ap.parse_args(argv)
    if args.action == "list":
        rows = addons.status()
        if args.json:
            print(json.dumps(rows, indent=1))
            return 0
        for x in rows:
            mark = "included" if x["included"] else "installed" if x["installed"] else "not installed"
            print(f"{x['id']:11} {x['name']:17} {mark:14} {x['licence']:13} ~{x['size_mb']:>5} MB  {x['adds']}"
                  + ("" if x["wired"] else "  (not used yet)"))
        return 0
    if not args.id:
        ap.error("which add-on? (python -m undercover.addons list)")
    try:
        a = addons.get(args.id)
        if args.action == "remove":
            r = addons.remove(a.id)
            print(f"{a.name} removed ({r['freed'] / 1e6:.0f} MB freed).")
            return 0
        addons._optional(a, "install")
        if not args.accept:
            print(f"{a.name}: {a.adds}\nLicence: {a.licence} ({a.licence_url})\n{a.licence_summary}\nAbout {a.size_mb} MB.")
            if input("Accept the licence and install? [y/N] ").strip().lower() not in ("y", "yes"):
                print("Nothing installed.")
                return 1
        progress = status_printer(a.size_mb) if args.status else (
            lambda m, d, t: print(f"\r{m}" + (f": {d / 1e6:.0f}/{t / 1e6:.0f} MB" if t else ""), end="", flush=True))
        cancel = (lambda: Path(args.cancel_file).exists()) if args.cancel_file else None
        try:
            addons.install(a.id, True, progress, cancel=cancel)
        except InterruptedError:
            print(f"\n{a.name}: stopped, as asked. What's downloaded is kept, and adding it again carries on.")
            return 3
        print(f"\n{a.name} installed.")
        return 0
    except addons.ModelError as e:
        print(f"\n{e}", file=sys.stderr)
        return 2


def status_printer(size_mb: float, out=None, clock=time.monotonic):
    """The installer's view of an add-on's install: one plain line per file (setup keeps those in its log), and at most
    one '@@P <per mille>|<text>' line a second (setup turns them into the wizard's bar; the user never sees one per MB).
    The per mille runs over the whole add-on (size_mb), not each file. -> progress(message, done, total)."""
    out = out or sys.stdout
    st = {"msg": None, "before": 0, "t": 0, "last": None}
    whole = max(1.0, size_mb * 1e6)

    def mb(n) -> bool:   # a size worth saying in MB (a small config file read "0 of 0 MB": say nothing for it)
        return bool(n) and n >= 1e6

    def progress(msg: str, done: int, total: int):
        if msg != st["msg"]:   # a new file (or the packages step): its line, once
            st["before"] += st["t"]
            st["msg"], st["t"] = msg, total
            print(msg + (f" ({total / 1e6:.0f} MB)" if mb(total) else ""), file=out, flush=True)
        st["t"] = total or st["t"]
        now = clock()
        if st["last"] is None or now - st["last"] >= 1 or (total and done >= total):
            st["last"] = now
            pm = int(min(0.99, (st["before"] + done) / whole) * 1000)
            print(f"@@P {pm}|{msg}" + (f": {done / 1e6:.0f} of {total / 1e6:.0f} MB" if mb(total) else ""), file=out, flush=True)
    return progress


if __name__ == "__main__":
    sys.exit(main())
