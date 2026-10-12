"""The tray icon (phase 18): `pythonw -m undercover.tray [--port N]`. A thin entry point; the code is undercover/core/trayicon.py (core/tray.py is the recent tray of covers)."""
import sys

from undercover.core.trayicon import main

if __name__ == "__main__":
    sys.exit(main())
