"""What this copy of UnderCover is: its version, where it lives, what it runs on (the System room's UnderCover row,
the Doctor's report, the diagnostics file). Cheap: no torch import, no subprocess."""
import platform
import sys
from pathlib import Path

from .config import ROOT

VERSION = "0.3.0"   # the installer's AppVersion (installer/UnderCover.iss) says the same


def git_commit(root: Path = ROOT) -> dict | None:
    """{"branch", "commit"} read straight from .git (no git needed), or None for an installed copy without one."""
    g = root / ".git"
    try:
        if g.is_file():   # a worktree: "gitdir: <path>"
            g = Path(g.read_text(encoding="utf-8").split(":", 1)[1].strip())
        head = (g / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref: "):
            return {"branch": None, "commit": head[:7]}
        ref = head[5:]
        common = g
        if (g / "commondir").exists():
            common = (g / (g / "commondir").read_text(encoding="utf-8").strip()).resolve()
        for base in (g, common):
            f = base / ref
            if f.exists():
                return {"branch": ref.rsplit("/", 1)[-1], "commit": f.read_text(encoding="utf-8").strip()[:7]}
        packed = common / "packed-refs"
        if packed.exists():
            for line in packed.read_text(encoding="utf-8").splitlines():
                if line.endswith(" " + ref):
                    return {"branch": ref.rsplit("/", 1)[-1], "commit": line[:7]}
        return {"branch": ref.rsplit("/", 1)[-1], "commit": None}
    except (OSError, IndexError):
        return None


def package_version(name: str) -> str | None:
    try:
        from importlib.metadata import version
        return version(name)
    except Exception:   # noqa: BLE001
        return None


def about() -> dict:
    """The UnderCover row: version (+ branch and commit when it's a git checkout), app folder, Python, PyTorch."""
    return {"version": VERSION, "git": git_commit(), "folder": str(ROOT), "python": platform.python_version(),
            "venv": sys.prefix, "torch": package_version("torch"), "os": platform.platform(terse=True)}
