"""Entry point: `python -m undercover.server` (UnderCover.bat, the installer's launcher, .claude/launch.json) runs the
suite's web server, undercover/app/server.py."""
from .app.server import main

if __name__ == "__main__":
    main()
