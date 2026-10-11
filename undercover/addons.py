"""python -m undercover.addons: list, install or remove optional add-ons (the registry is core/addons.py)."""
import sys

from .core.addons import main

if __name__ == "__main__":
    sys.exit(main())
