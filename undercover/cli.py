"""Entry point for tools/cover.bat: `python -m undercover.cli` runs FrontMan's command-line cover tool."""
from .frontman.cli import main

if __name__ == "__main__":
    main()
