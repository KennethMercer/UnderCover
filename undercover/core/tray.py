"""Keep or roll off (phase 6): the recent tray.

Every module (covers, speech) has a *recent tray*: its latest TRAY_SIZE finished tracks, kept or not, newest first.
"Send to SafeHouse" keeps a track (`kept: true`, `kept_at`); a kept track lives on in SafeHouse's library. The tray is a
time window, not a shelf: when a track finishes successfully and the tray would hold TRAY_SIZE + 1, the one that just
fell out of the window leaves:
    kept   -> nothing happens (it stays in SafeHouse);
    unkept -> its files and its history line are deleted (through core/storage.remove_files, logged in logs/server.log).

Safety, by construction (each is proved in tests/smoke.py's "keep or roll off" check):
- `leaver()` returns at most ONE item per call: the TRAY_SIZE + 1'th newest. Anything older than that left the window at
  an earlier finish (or before this feature existed) and is never touched by a later roll-off, so a long history of
  unkept tracks from before the feature can't be swept away, and a failed delete can't cause a bigger one later.
- it never returns a kept item, and callers delete nothing but that item's own files (inside their module's folders).
- the roll-off only runs after a track was recorded successfully; failed, cancelled and paused work never reaches it.
- the delete goes through core/storage.remove_files: allow-listed folders, and in test mode the real folders refused.
"""
import os
import sys
import time

from .config import path

TRAY_SIZE = 15


def in_tray(items_oldest_first: list) -> list:
    """The items that are in the tray: the latest TRAY_SIZE, oldest first."""
    return items_oldest_first[-TRAY_SIZE:]


def leaver(items_oldest_first: list):
    """The one item that just fell out of the tray (None if the tray isn't over full). Never a kept item: a kept one
    that falls out simply stays in SafeHouse, so it isn't returned for deletion."""
    n = len(items_oldest_first)
    if n <= TRAY_SIZE:
        return None
    item = items_oldest_first[n - TRAY_SIZE - 1]
    return None if item.get("kept") else item


def log(message: str):
    """One line in logs/server.log. The windowless server's stdout *is* that file (an inherited handle with its own file
    position: a second writer would be overwritten), so there it prints; anywhere else the line is appended to the file."""
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    try:
        f = path("logs_dir") / "server.log"
        try:
            same = os.path.samestat(os.fstat(sys.stdout.fileno()), os.stat(f))
        except (OSError, ValueError, AttributeError):
            same = False
        if same:
            print(line, flush=True)
            return
        f.parent.mkdir(parents=True, exist_ok=True)
        with open(f, "a", encoding="utf-8") as out:
            out.write(line + "\n")
    except OSError:
        pass
