"""Entry point: `python -m undercover.server` (UnderCover.bat, the installer's launcher, .claude/launch.json) runs the
suite's web server, undercover/app/server.py."""
import os
from pathlib import Path

from .core import config


def _migrate():
    """A config.toml from before the defaults split becomes only what differs (kept beside it as it was)."""
    try:
        r = config.migrate()
        if r:
            print(f"Settings: config.toml now holds only what differs from the defaults; the full file is {r['backup']}",
                  flush=True)
    except Exception as e:   # noqa: BLE001 - never block startup: the full file still reads the same
        print(f"Settings: config.toml left as it is ({type(e).__name__}: {e})", flush=True)


LAUNCHER_MOVED = "launcher-moved.json"   # in the cache folder: the shortcuts were looked at once (layout2)


def repoint_launchers(root: Path = config.ROOT, env=None) -> list[str] | None:
    """UnderCover.bat moved into scripts/ (layout2, 2026-10-17): the user's shortcuts to the old one (the desktop, the
    Start menu, "Start with Windows") are aimed at the new one, once, at a real start. Never in a test, a sandbox or a
    golden run (they set UNDERCOVER_TEST_ROOTS or UNDERCOVER_CONFIG), and not in an installed copy (its shortcuts point
    at UnderCover.cmd, which didn't move). A failure is said and tried again at the next start.
    -> the shortcuts changed, or None when it didn't look."""
    import json
    import time
    from .core import fsutil, platform
    env = os.environ if env is None else env
    old, new = root / "UnderCover.bat", root / "scripts" / "UnderCover.bat"
    if env.get("UNDERCOVER_TEST_ROOTS") or env.get("UNDERCOVER_CONFIG") or old.exists() or not new.exists():
        return None
    mark = config.path("cache_dir") / LAUNCHER_MOVED
    if mark.exists():
        return None
    try:
        changed = platform.repoint_shortcuts(old, new)
    except platform.NotOnThisOS:
        changed = []
    except Exception as e:   # noqa: BLE001 - never block startup; the next start tries again
        print(f"Shortcuts: couldn't aim them at scripts\\UnderCover.bat yet ({type(e).__name__}: {e})", flush=True)
        return None
    fsutil.write_text(mark, json.dumps({"at": time.strftime("%Y-%m-%d %H:%M"), "from": str(old), "to": str(new),
                                       "changed": changed}, indent=1))
    if changed:
        print(f"Shortcuts: {len(changed)} now start scripts\\UnderCover.bat: {', '.join(changed)}", flush=True)
    return changed


def main():
    """Start the server. Only a real start migrates the settings: importing this module (a test that loads every
    module) must never rewrite a config.toml. The migration comes before app.server is imported, which stamps the code
    and config (a write after the stamp would read as "out of date, restart")."""
    _migrate()
    try:
        repoint_launchers()
    except Exception as e:   # noqa: BLE001 - never block startup
        print(f"Shortcuts: left as they are ({type(e).__name__}: {e})", flush=True)
    from .app.server import main as run
    run()


if __name__ == "__main__":
    main()
