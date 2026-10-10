"""Crash-safe file writes. This PC has had sudden power-offs (PLAN.md, incident 2026-10-09): a file written in place
can be left half-written. These write to a temporary file in the same folder, flush it to disk, then swap it in,
so a power cut leaves either the old version or the new one, never a broken file."""
import os
import time
import uuid
from pathlib import Path


def _replace(tmp: Path, dest: Path, tries: int = 50):
    for i in range(tries):   # Windows refuses while another handle (a reader) has dest open: retry ~2.5 s
        try:
            os.replace(tmp, dest)
            return
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(0.05)


def write_text(dest: Path, text: str):
    dest = Path(dest)
    tmp = dest.with_name(f"{dest.name}.{uuid.uuid4().hex[:8]}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    _replace(tmp, dest)


def torch_save(obj, dest: Path):
    import torch
    dest = Path(dest)
    tmp = dest.with_name(f"{dest.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with open(tmp, "wb") as f:
            torch.save(obj, f)
            f.flush()
            os.fsync(f.fileno())
        _replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)


def retry(fn, tries: int = 30):
    """Windows refuses to rename/delete a file another handle has open (e.g. the page's player stream, which the
    server is still finishing); retry for up to ~3 s."""
    for i in range(tries):
        try:
            return fn()
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(0.1)
