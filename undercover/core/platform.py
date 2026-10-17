"""Everything that depends on the operating system, in one place (phase 18; CLAUDE.md: portable by design, a Linux version
is planned). Every Windows-only call in UnderCover goes through here, and smoke checks that no other module makes one.

Each function works on Windows and, on another OS, either does the portable thing (open a folder with xdg-open, exit
with os._exit) or raises NotOnThisOS with a plain sentence. `supports(feature)` -> (bool, why) lets the page, the tray
and the Doctor hide what can't work here.

Kept light on purpose: the standard library only at the top, so the restart helper and the tray can import it without
torch, the config or the jobs.
"""
import os
import subprocess
import sys
import time
from pathlib import Path

WINDOWS = os.name == "nt"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
DETACHED = NO_WINDOW | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


class NotOnThisOS(Exception):
    """This needs Windows (for now); the message says what, plainly."""


def _not(what: str) -> NotOnThisOS:
    return NotOnThisOS(f"{what} works on Windows only for now (this is {sys.platform}).")


def supports(feature: str) -> tuple[bool, str]:
    """(can it work here?, why not). Features: tray, startup, firewall, keep_awake, drives, gpu_driver, show_in_folder,
    fullscreen (seeing a full-screen game: the game pause), away (Tailscale for away from home, phase 20)."""
    if feature == "tray":
        if not WINDOWS:
            return False, "The tray icon is Windows-only for now."
        try:
            import pystray  # noqa: F401
        except ImportError:
            return False, "pystray isn't installed (installer/requirements/common.txt)."
        return True, ""
    if feature in ("startup", "firewall", "keep_awake", "drives", "gpu_driver", "show_in_folder", "fullscreen", "away"):
        return (True, "") if WINDOWS else (False, f"{feature} is Windows-only for now.")
    return False, f"Unknown feature '{feature}'."


# ---------------------------------------------------------------- away from home (phase 20)

def extract_msi(msi, target) -> None:
    """An administrative extraction of an .msi into `target` (msiexec /a: the files only, no install, no service, no
    driver, nothing in the registry). Tailscale publishes Windows binaries only inside its installer."""
    if not WINDOWS:
        raise _not("Unpacking a Windows installer")
    r = subprocess.run(["msiexec", "/a", str(msi), "/qn", f"TARGETDIR={target}"], capture_output=True, text=True,
                       errors="replace", timeout=300, creationflags=NO_WINDOW)
    if r.returncode != 0:
        raise OSError(f"msiexec /a ended with code {r.returncode}")


def tailscale_names() -> tuple[str, str]:
    """(the daemon's, the command line's) file names on this OS."""
    return ("tailscaled.exe", "tailscale.exe") if WINDOWS else ("tailscaled", "tailscale")


def tailscale_socket(state_dir) -> str:
    """Where our tailscaled listens for its CLI: our own named pipe on Windows (never the system Tailscale's), a
    socket in its state folder elsewhere."""
    return r"\\.\pipe\undercover-tailscaled" if WINDOWS else str(Path(state_dir) / "tailscaled.sock")


def tailscaled_os_args() -> list:
    """The daemon's OS-specific flags: on Windows, run as this user with our own pipe (dev mode: the pipe accepts only
    this user; measured in the phase 20 spike)."""
    return ["--windows-mode=dev"] if WINDOWS else []


# ---------------------------------------------------------------- a full-screen game (the game pause)

def fullscreen_app() -> dict | None:
    """The program filling the screen right now, or None: {"exe", "path", "title", "how", "pid"}. Read only.
    how: "exclusive" (a game holding the screen: Windows' QUNS_RUNNING_D3D_FULL_SCREEN), "busy" (Windows' own
    "something is full-screen", QUNS_BUSY), or "borderless" (the foreground window covers its whole monitor with no
    title bar: many modern games). The desktop and the taskbar are never it."""
    if not WINDOWS:
        raise _not("Seeing a full-screen game")
    import ctypes
    from ctypes import wintypes
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    quns = ctypes.c_int(0)
    how = None
    if ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(quns)) == 0:
        how = {3: "exclusive", 2: "busy"}.get(quns.value)
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    cls = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, cls, 256)
    if cls.value in ("Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"):
        return None
    if how is None:   # borderless: no title bar, and the window covers its whole monitor
        if user32.GetWindowLongW(hwnd, -16) & 0x00C00000 == 0x00C00000:   # GWL_STYLE has WS_CAPTION: a normal window
            return None
        if user32.IsZoomed(hwnd):   # maximized, not full-screen: a browser or an editor with its own title bar
            return None

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                        ("dwFlags", wintypes.DWORD)]
        rect, mi = wintypes.RECT(), MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)) or not user32.GetMonitorInfoW(
                user32.MonitorFromWindow(hwnd, 2), ctypes.byref(mi)):   # MONITOR_DEFAULTTONEAREST
            return None
        m = mi.rcMonitor
        if not (rect.left <= m.left and rect.top <= m.top and rect.right >= m.right and rect.bottom >= m.bottom):
            return None
        how = "borderless"
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    exe = ""
    h = kernel32.OpenProcess(0x1000, False, pid.value)   # PROCESS_QUERY_LIMITED_INFORMATION: no rights to change it
    if h:
        n, buf = wintypes.DWORD(1024), ctypes.create_unicode_buffer(1024)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
            exe = buf.value
        kernel32.CloseHandle(h)
    title = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, title, 512)
    from pathlib import Path
    return {"exe": Path(exe).name if exe else "", "path": exe, "title": title.value, "how": how, "pid": pid.value}


# ---------------------------------------------------------------- files and folders

def open_folder(path):
    """Show a folder in the file manager."""
    import shutil
    path = str(path)
    if WINDOWS:
        os.startfile(path)   # noqa: S606 - a folder of ours, never a URL from a request
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    elif shutil.which("xdg-open"):
        subprocess.Popen(["xdg-open", path])
    else:
        raise NotOnThisOS(f"There's no file manager to open here (xdg-open isn't installed). The folder is {path}.")


def show_in_folder(file):
    """Show a file selected in its folder (other OSes: just open the folder)."""
    if WINDOWS:
        subprocess.Popen(["explorer", f"/select,{Path(file)}"])
    else:
        open_folder(Path(file).parent)


def drives() -> list[dict]:
    """Drives to offer as storage targets: letter, free and total bytes, removable, whether `app_root` is on it.
    Windows: fixed and removable drives (not network, CD, RAM disk). Elsewhere: the root filesystem."""
    import shutil
    out = []
    if WINDOWS:
        import ctypes
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        for i in range(26):
            if not mask & (1 << i):
                continue
            root = f"{chr(65 + i)}:\\"
            kind = ctypes.windll.kernel32.GetDriveTypeW(root)
            if kind not in (2, 3):   # removable, fixed
                continue
            try:
                du = shutil.disk_usage(root)
            except OSError:
                continue
            out.append({"drive": root[:2], "free": du.free, "total": du.total, "removable": kind == 2})
    else:
        du = shutil.disk_usage("/")
        out.append({"drive": "/", "free": du.free, "total": du.total, "removable": False})
    return out


# ---------------------------------------------------------------- processes

def own_executable() -> str:
    """The program this process really runs (in a Windows venv, sys.executable is the venv's launcher, which only starts
    the base interpreter: the base one is what listens, and what a firewall rule must name)."""
    if WINDOWS:
        import ctypes
        buf = ctypes.create_unicode_buffer(32768)
        if ctypes.windll.kernel32.GetModuleFileNameW(None, buf, len(buf)):
            return buf.value
    return sys.executable


def wait_for_exit(pid: int, timeout: float = 60.0) -> bool:
    """Wait until process `pid` has exited (True) or the timeout passed (False)."""
    if WINDOWS:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k32.WaitForSingleObject.restype = wintypes.DWORD
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        h = k32.OpenProcess(0x00100000, False, pid)   # SYNCHRONIZE
        if not h:
            return True   # already gone
        try:
            return k32.WaitForSingleObject(h, int(timeout * 1000)) == 0   # WAIT_OBJECT_0
        finally:
            k32.CloseHandle(h)
    end = time.time() + timeout
    while time.time() < end:
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        time.sleep(0.2)
    return False


def hard_exit(code: int = 0):
    """End this process at once. On Windows a normal exit (even os._exit) can hang while the GPU runtime's DLLs shut down,
    leaving a zombie holding RAM / VRAM: TerminateProcess skips that (the OS still frees everything)."""
    if WINDOWS:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # declared types: without them the pseudo-handle (-1) is passed as a 32-bit int and TerminateProcess fails
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        k32.TerminateProcess.restype = wintypes.BOOL
        if not k32.TerminateProcess(k32.GetCurrentProcess(), code):
            print(f"TerminateProcess failed (error {ctypes.get_last_error()})", flush=True)
    os._exit(code)


_CONSOLE_HANDLER = None


def on_console_close(fn):
    """Call fn(reason) when the console window closes, the user logs off or the PC shuts down (Windows skips Python's
    own exit path then). Elsewhere: SIGHUP (terminal closed) and SIGTERM."""
    global _CONSOLE_HANDLER
    if WINDOWS:
        import ctypes
        from ctypes import wintypes
        reasons = {2: "window closed", 5: "log off", 6: "shutdown"}   # CTRL_CLOSE, CTRL_LOGOFF, CTRL_SHUTDOWN

        def handler(event):
            if event in reasons:
                fn(reasons[event])
            return False   # Ctrl+C / Ctrl+Break: Python raises KeyboardInterrupt as before
        _CONSOLE_HANDLER = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)(handler)   # kept, or it's collected
        ctypes.windll.kernel32.SetConsoleCtrlHandler(_CONSOLE_HANDLER, True)
        return
    import signal
    for sig, reason in ((getattr(signal, "SIGHUP", None), "terminal closed"), (signal.SIGTERM, "stopped")):
        if sig is not None:
            signal.signal(sig, lambda *_a, r=reason: fn(r))


def start_detached(cmd: list, cwd=None, log=None) -> subprocess.Popen:
    """A process that outlives this one, with no console window on Windows."""
    out = log if log is not None else subprocess.DEVNULL
    return subprocess.Popen(cmd, cwd=str(cwd) if cwd else None, stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                            creationflags=DETACHED, close_fds=True)


def windowless_python() -> str:
    """The interpreter to start a background helper with (pythonw.exe on Windows, so no console flashes)."""
    if WINDOWS:
        w = Path(sys.executable).with_name("pythonw.exe")
        if w.exists():
            return str(w)
    return sys.executable


def exclusive_bind(sock):
    """Bind a listening socket exclusively: on Windows a second server could otherwise take the same port (and requests
    would go to either at random). Elsewhere the default (no SO_REUSEADDR) is already exclusive."""
    import socket
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)


_INSTANCES: dict = {}


def single_instance(name: str) -> bool:
    """Claim a one-per-user name (the tray): True if this process has it, False if another one does. Held until exit
    (or release_instance)."""
    if name in _INSTANCES:
        return True
    if WINDOWS:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = wintypes.HANDLE
        k32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        h = k32.CreateMutexW(None, False, f"Local\\{name}")
        if not h:
            return False
        if ctypes.get_last_error() == 183:   # ERROR_ALREADY_EXISTS
            k32.CloseHandle(h)
            return False
        _INSTANCES[name] = h
        return True
    import fcntl
    import tempfile
    f = open(Path(tempfile.gettempdir()) / f"{name}.lock", "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return False
    _INSTANCES[name] = f
    return True


def release_instance(name: str):
    h = _INSTANCES.pop(name, None)
    if h is None:
        return
    if WINDOWS:
        import ctypes
        ctypes.WinDLL("kernel32").CloseHandle(h)
    else:
        h.close()


# ---------------------------------------------------------------- power

def keep_awake(on: bool) -> bool:
    """Don't let the PC sleep while voice work runs (a per-thread request, released when it ends). False when this OS
    can't (the caller carries on)."""
    if not WINDOWS:
        return False
    try:
        import ctypes
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))
        return True
    except (OSError, AttributeError):
        return False


# ---------------------------------------------------------------- starting with the OS

def startup_folder() -> Path:
    """The folder whose shortcuts start when the user signs in."""
    if not WINDOWS:
        raise _not("Starting with the computer")
    return Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


STARTUP_NAME = "UnderCover.lnk"


def startup_entry_state(folder=None) -> bool:
    folder = Path(folder) if folder else startup_folder()
    return (folder / STARTUP_NAME).exists()


def startup_entry(on: bool, target, folder=None, icon=None) -> bool:
    """Add or remove the "start UnderCover when I sign in" shortcut (only on the user's own click). folder: the Startup
    folder (tests pass their own). Returns the new state."""
    folder = Path(folder) if folder else startup_folder()
    link = folder / STARTUP_NAME
    if not on:
        link.unlink(missing_ok=True)
        return False
    if not WINDOWS:
        raise _not("Starting with the computer")
    target = Path(target)
    q = lambda s: str(s).replace("'", "''")   # noqa: E731 - a PowerShell single-quoted string
    ps = (f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut('{q(link)}'); $s.TargetPath = '{q(target)}'; "
          f"$s.WorkingDirectory = '{q(target.parent)}'; $s.WindowStyle = 7; "
          + (f"$s.IconLocation = '{q(icon)}'; " if icon else "")
          + "$s.Description = 'UnderCover'; $s.Save()")
    folder.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True, text=True,
                       timeout=30, creationflags=NO_WINDOW)
    if r.returncode or not link.exists():
        raise OSError(f"The startup shortcut couldn't be made: {(r.stderr or r.stdout).strip()[:200]}")
    return True


def repoint_shortcuts(old, new, folders=None) -> list[str]:
    """Shortcuts (.lnk) whose target is `old` aimed at `new` instead (their arguments, working folder and icon kept):
    the desktop (where Windows keeps it, OneDrive's too) and the Start menu with its Startup folder. folders: tests pass
    their own, and every folder is searched to any depth then. -> the shortcuts changed."""
    if not WINDOWS:
        raise _not("Moving shortcuts")
    q = lambda s: str(s).replace("'", "''")   # noqa: E731 - a PowerShell single-quoted string
    # (hashtables, not pairs: PowerShell flattens a one-item list of arrays into its items)
    if folders is not None:
        where = "@(" + ", ".join(f"@{{p='{q(f)}'; r=$true}}" for f in folders) + ")"
    else:   # the desktop's own shortcuts only; the Start menu (Startup is inside it) to any depth
        where = "@(@{p=[Environment]::GetFolderPath('Desktop'); r=$false}, @{p=[Environment]::GetFolderPath('Programs'); r=$true})"
    ps = (f"$sh = New-Object -ComObject WScript.Shell; $old = '{q(old)}'; $new = '{q(new)}'; "
          f"foreach ($w in {where}) {{ $d = $w.p; if (-not $d -or -not (Test-Path -LiteralPath $d)) {{ continue }}; "
          "Get-ChildItem -LiteralPath $d -Filter *.lnk -Recurse:$w.r -ErrorAction SilentlyContinue | ForEach-Object { "
          "$l = $sh.CreateShortcut($_.FullName); if ($l.TargetPath -ieq $old) { $l.TargetPath = $new; $l.Save(); "
          "Write-Output $_.FullName } } }")
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True, text=True,
                       timeout=60, creationflags=NO_WINDOW)
    if r.returncode:
        raise OSError(f"The shortcuts couldn't be moved: {(r.stderr or r.stdout).strip()[:200]}")
    return [line.strip() for line in r.stdout.splitlines() if line.strip()]


# ---------------------------------------------------------------- the system's own reports

def run_text(cmd: list, timeout: float = 20) -> str:
    """A system tool's output as text (no window on Windows)."""
    r = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=timeout, creationflags=NO_WINDOW)
    return r.stdout


def network_and_firewall(addr: str) -> tuple[str, str, str]:
    """(the network profile of the connection holding `addr` as JSON, the firewall profiles' state, the inbound rules):
    the raw reports core/phone.py parses. Read only; run side by side (~1 s)."""
    if not WINDOWS:
        raise _not("Checking the network type and the firewall")
    import concurrent.futures
    import ipaddress
    ipaddress.ip_address(addr)   # only a real address goes into the PowerShell line
    ps = (f"$i = (Get-NetIPAddress -IPAddress '{addr}' -ErrorAction SilentlyContinue | Select-Object -First 1).InterfaceIndex; "
          "if ($i) { Get-NetConnectionProfile -InterfaceIndex $i | Select-Object Name,InterfaceAlias,NetworkCategory | "
          "ConvertTo-Json -Compress }")
    with concurrent.futures.ThreadPoolExecutor(3) as ex:
        f_net = ex.submit(run_text, ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps])
        f_state = ex.submit(run_text, ["netsh", "advfirewall", "show", "allprofiles", "state"])
        f_rules = ex.submit(run_text, ["netsh", "advfirewall", "firewall", "show", "rule", "name=all", "dir=in", "verbose"])
        return f_net.result().strip(), f_state.result(), f_rules.result()


def video_controllers() -> list[dict]:
    """The graphics adapters Windows reports: [{Name, DriverVersion, DriverDate}] (~1 s, PowerShell)."""
    if not WINDOWS:
        raise _not("Reading the graphics driver")
    import json
    out = run_text(["powershell", "-NoProfile", "-Command",
                    "Get-CimInstance Win32_VideoController | Select-Object Name,DriverVersion,DriverDate | ConvertTo-Json -Compress"])
    rows = json.loads(out or "null")
    return rows if isinstance(rows, list) else [rows] if rows else []
