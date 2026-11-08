"""Restart helper: wait for the old server process to fully exit, then start a new one.

Starting the new server (which initialises the GPU) while the old one is still tearing down its GPU context
leaves the old process stuck in the driver. So the old server spawns this small helper (no torch, no GPU),
exits, and the helper starts the new server only once the old process is gone.

    python -m undercover.core.relaunch <old_pid> [server args...]
    python -m undercover.core.relaunch 0          (scripts/UnderCover.bat double-clicked: start windowless, nothing to wait for)
"""
import sys
import time
from pathlib import Path

from . import platform   # the OS part (phase 18: every OS-specific call in core/platform; the standard library only)

ROOT = Path(__file__).resolve().parents[2]
wait_for_exit = platform.wait_for_exit


def main():
    old_pid, args = int(sys.argv[1]), sys.argv[2:]
    logs = ROOT / "logs"
    logs.mkdir(exist_ok=True)
    with open(logs / "server.log", "a", encoding="utf-8") as log:
        if old_pid == 0:   # scripts/UnderCover.bat double-clicked: a fresh start in the background, no window
            log.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] start: in the background (no window)\n")
        else:
            ok = wait_for_exit(old_pid)
            log.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] restart: old server {old_pid} "
                      f"{'exited' if ok else 'did NOT exit within 60 s'}; starting a new one\n")
        log.flush()
        # the console interpreter with a hidden console: tools the server runs (ffmpeg...) share it, no pop-ups
        platform.start_detached([platform.console_python(), "-m", "undercover.server", *args], cwd=ROOT, log=log)


if __name__ == "__main__":
    main()
