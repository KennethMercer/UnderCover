"""Entry point: `python -m undercover.server` (UnderCover.bat, the installer's launcher, .claude/launch.json) runs the
suite's web server, undercover/app/server.py."""
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


_migrate()
from .app.server import main  # noqa: E402  (after the migration: the server stamps its code and config at import)

if __name__ == "__main__":
    main()
