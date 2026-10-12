"""The YouTube downloader's self-heal from the command line (phase 16; core/ytdlp.py has the details).

    python -m undercover.ytdlp status                      the pair in use, the last check
    python -m undercover.ytdlp update [--dry-run] [--offline] [--json]
                                                           PyPI's newest yt-dlp + its solver: checked, staged, tested, used
                                                           only if at least as good (--dry-run: tests in a temp folder and
                                                           changes nothing; --offline: no PyPI, only the pairs here)
    python -m undercover.ytdlp install                     what setup.ps1 runs: update, and whatever happens the venv's
                                                           pinned pair stays as the fallback (always exits 0)
"""
import json
import sys


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    from .core import ytdlp
    ytdlp.activate()
    cmd = argv[0] if argv else "status"
    if cmd == "status":
        s = ytdlp.status()
        if "--json" in argv:
            print(json.dumps(s, indent=1, default=str))
        else:
            print(f"yt-dlp {s['yt']} + solver {s['ejs'] or 'missing'} ({'a checked update' if s['source'] == 'side' else 'the pinned pair'})")
            print(f"auto-update: {'on' if s['auto'] else 'off'}; last check: {s.get('last_check') or 'never'} {s.get('last_result') or ''}")
        return 0
    if cmd in ("update", "install"):
        try:
            r = ytdlp.heal(reason="setup" if cmd == "install" else "by hand", dry_run="--dry-run" in argv,
                           offline="--offline" in argv)
        except Exception as e:   # noqa: BLE001
            print(f"The YouTube downloader couldn't be checked: {e}. The pinned pair stays in use.")
            return 0 if cmd == "install" else 1
        if "--json" in argv:
            print(json.dumps(r, indent=1, default=str))
        else:
            for k, v in r["scores"].items():
                print(f"  {k:10} resolves {v['ok']} of {v['of']} known videos" + (f"  ({'; '.join(v['errors'])})" if v["errors"] else ""))
            b, a = r["before"], r["after"]
            print(("Would use " if r["dry_run"] and r["chose"] != "current" else "Using ") +
                  (f"yt-dlp {a['yt']} + solver {a['ejs']}" if r["changed"] else f"yt-dlp {b['yt']} + solver {b['ejs']} (no change)")
                  + (f" -- {r['chose']}" if r["dry_run"] else "") + (f". {r['note']}" if r["note"] else ""))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
