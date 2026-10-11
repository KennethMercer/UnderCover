"""Restart helper: wait for the old server process to fully exit, then start a new one.

Starting the new server (which initialises the GPU) while the old one is still tearing down its GPU context
leaves the old process stuck in the driver. So the old server spawns this small helper (no torch, no GPU),
exits, and the helper starts the new server only once the old process is gone.

    python -m undercover.core.relaunch <old_pid> [server args...]
    python -m undercover.core.relaunch 0          (UnderCover.bat double-clicked: start windowless, nothing to wait for)
"""
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def wait_for_exit(pid: int, timeout: float = 60.0) -> bool:
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k32.WaitForSingleObject.restype = wintypes.DWORD
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        SYNCHRONIZE = 0x00100000
        h = k32.OpenProcess(SYNCHRONIZE, False, pid)
        if not h:
            return True  # already gone
        try:
            return k32.WaitForSingleObject(h, int(timeout * 1000)) == 0  # WAIT_OBJECT_0
        finally:
            k32.CloseHandle(h)
    import os
    end = time.time() + timeout
    while time.time() < end:
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        time.sleep(0.2)
    return False


def main():
    old_pid, args = int(sys.argv[1]), sys.argv[2:]
    logs = ROOT / "logs"
    logs.mkdir(exist_ok=True)
    with open(logs / "server.log", "a", encoding="utf-8") as log:
        if old_pid == 0:   # UnderCover.bat double-clicked: a fresh start in the background, no window
            log.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] start: in the background (no window)\n")
        else:
            ok = wait_for_exit(old_pid)
            log.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] restart: old server {old_pid} "
                      f"{'exited' if ok else 'did NOT exit within 60 s'}; starting a new one\n")
        log.flush()
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        # python.exe (not pythonw) with a hidden console: tools the server runs (ffmpeg...) share it, no pop-ups
        exe = Path(sys.executable)
        exe = exe.with_name("python.exe") if exe.name.lower() == "pythonw.exe" else exe
        subprocess.Popen([str(exe), "-m", "undercover.server", *args], cwd=str(ROOT),
                         stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                         creationflags=flags, close_fds=True)


if __name__ == "__main__":
    main()
