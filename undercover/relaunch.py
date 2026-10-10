"""Entry point kept for servers started before phase 1: their Restart runs `python -m undercover.relaunch`. The
restart helper itself is undercover/core/relaunch.py."""
from .core.relaunch import main

if __name__ == "__main__":
    main()
