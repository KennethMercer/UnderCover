"""python -m undercover.addons: list, install or remove the add-ons (the registry is core/addons.py).

    python -m undercover.addons [list] [--json]
    python -m undercover.addons install <id> [--accept-licence]     (without it: shows the licence and asks)
    python -m undercover.addons remove <id>

The installer's optional add-ons step runs `install <id> --accept-licence` after the user ticked "I accept".
"""
import argparse
import json
import sys

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
        addons.install(a.id, True, lambda m, d, t: print(f"\r{m}" + (f": {d / 1e6:.0f}/{t / 1e6:.0f} MB" if t else ""),
                                                          end="", flush=True))
        print(f"\n{a.name} installed.")
        return 0
    except addons.ModelError as e:
        print(f"\n{e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
