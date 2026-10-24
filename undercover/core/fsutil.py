"""Crash-safe file writes. The maker's PC has had sudden power-offs: a file written in place
can be left half-written. These write to a temporary file in the same folder, flush it to disk, then swap it in,
so a power cut leaves either the old version or the new one, never a broken file.

Test mode (storage.ALLOWED_ROOTS, set by smoke's isolated_data() and every sandbox server): a write outside the test's own
folders raises before anything is written. On 2026-10-13 a vault check held the real config's path from an import made
before its sandbox and appended to the user's config.toml and phone.json; config and phone writes all come through here."""
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


def guard(dest: Path):
    """Test mode: refuse a write outside the test's folders (normal use: no-op)."""
    from . import storage   # here, not at the top: storage imports this module
    if storage.ALLOWED_ROOTS is not None and not any(storage._inside(Path(dest), r) for r in storage.ALLOWED_ROOTS):
        raise PermissionError(f"Test mode: {dest} is outside the test's folders; nothing was written.")


def write_text(dest: Path, text: str):
    dest = Path(dest)
    guard(dest)
    tmp = dest.with_name(f"{dest.name}.{uuid.uuid4().hex[:8]}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    _replace(tmp, dest)


def link_duplicate(keep: Path, dup: Path) -> bool:
    """dup becomes a hard link to keep when the two are byte-identical (phase 24): one copy on disk, both names work,
    and deleting either name leaves the other's data. Nothing writes these files in place (saves and installs write
    a new file and swap it in), so a later write splits them back into two copies. A copy elsewhere (Storage, the
    Vault) is simply two files. -> False, with nothing changed, when they differ or the drive can't link."""
    import filecmp
    keep, dup = Path(keep), Path(dup)
    guard(dup)
    try:
        if os.path.samefile(keep, dup):
            return True
        if keep.stat().st_size != dup.stat().st_size or not filecmp.cmp(keep, dup, shallow=False):
            return False
        tmp = dup.with_name(f"{dup.name}.{uuid.uuid4().hex[:8]}.link")
        os.link(keep, tmp)
    except OSError:
        return False
    try:
        _replace(tmp, dup)
    except OSError:
        tmp.unlink(missing_ok=True)
        return False
    return True


def torch_save(obj, dest: Path):
    import torch
    dest = Path(dest)
    guard(dest)
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
