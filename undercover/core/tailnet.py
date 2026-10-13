"""Away from home (phase 20; docs/research/phase20-plan.md, the spike in docs/research/tailscale.md): Tailscale, hidden.
People never meet it: one switch in SafeHouse, one sign-in, one approval, then a link that works anywhere.

  - the binaries: tailscaled + tailscale from the pinned MSI (downloaded on first use, the user's answer; SHA-256 of the
    MSI and of both binaries checked), in <models>/tailscale (they download again; never in the vault);
  - the daemon: userspace networking (no adapter, no driver, no admin), our own pipe, state in <cache>/tailscale-state
    (the node's keys: never in the vault, the diagnostics file or git), `up --unattended` (else it drops when the CLI
    exits), a random node name (uc-xxxxxx: Certificate Transparency logs are public);
  - Funnel: https://<name>.<tailnet>.ts.net -> 127.0.0.1:<the away listener> ONLY (funnel_target refuses anything
    else: never the PC's own 7860, never the home-network listener). That listener asks for an away token (pairing
    stays on the home Wi-Fi; app/phone_api).
A driver thread walks: fetching -> starting -> sign-in (Tailscale's login URL) -> approve (the tailnet's one-time
Funnel / HTTPS approval URL) -> ready (the link). Off: funnel reset, down. Forget: logout and the state folder gone."""
import json
import os
import re
import secrets
import shutil
import string
import subprocess
import threading
import time
from pathlib import Path

from . import platform, storage
from .config import path
from .voices import ModelError

VERSION = "1.104.1"
MSI = {"url": f"https://pkgs.tailscale.com/stable/tailscale-setup-{VERSION}-amd64.msi", "size": 38_718_976,
       "sha256": "6da8bcdb0d45a214cae0e8a88c2987ad48dec134bf04dffb9c05f1354c02b0b4"}
BINS = {"tailscaled.exe": "ae0195119cc8efbd88d11bff56394ff9f1c08dc9d51957a400d4cb35c74e4bb1",
        "tailscale.exe": "72a499c653da6c16a50908d6ceb244d12b5f4834ab8ac19a87f2ca3117e8c20a"}
CMD: dict = {}   # tests: {"tailscale": [...], "tailscaled": [...]} instead of the downloaded binaries
URL_RX = re.compile(r"https://login\.tailscale\.com/\S+")
STATE = {"want": False, "phase": "off", "auth_url": None, "approve_url": None, "host": None, "error": "",
         "progress": None, "port": None}
_P = {"daemon": None, "up": None, "funnel": None, "driver": None, "out": [], "fetch_failed": 0.0}
_LOCK = threading.RLock()   # "still wanted?" and starting a process happen together, so Off can't be overtaken by a tick
FETCH_RETRY_S = 600         # a failed download waits this long before the next try (39 MB each: never in a loop)


def bin_dir() -> Path:
    return path("models_dir") / "tailscale"


def state_dir() -> Path:
    return path("cache_dir") / "tailscale-state"


def _cmd(which: str) -> list:
    if which in CMD:
        return list(CMD[which])
    fake = os.environ.get("UNDERCOVER_TAILSCALE_FAKE")
    if fake and storage.ALLOWED_ROOTS is not None:   # a sandbox server in smoke: tools/fake_tailscale.py, test mode only
        import sys
        return [sys.executable, fake, "daemon" if which == "tailscaled" else "cli"]
    daemon, cli = platform.tailscale_names()
    return [str(bin_dir() / (daemon if which == "tailscaled" else cli))]


def have_binaries() -> bool:
    if CMD or (os.environ.get("UNDERCOVER_TAILSCALE_FAKE") and storage.ALLOWED_ROOTS is not None):
        return True
    return all((bin_dir() / n).is_file() for n in platform.tailscale_names())


def fetch_binaries(progress=None):
    """The pinned MSI -> both binaries, every hash checked; the MSI and the unpacked rest deleted after."""
    from .setup_models import fetch, sha256_of
    work = path("cache_dir") / "tailscale-fetch"
    msi = work / Path(MSI["url"]).name
    fetch(MSI["url"], msi, MSI["sha256"], "Tailscale (about 39 MB)", progress=progress)
    unpacked = work / "unpacked"
    platform.extract_msi(msi, unpacked)
    bin_dir().mkdir(parents=True, exist_ok=True)
    for name, want in BINS.items():
        found = next(iter(unpacked.rglob(name)), None)
        if found is None or sha256_of(found) != want:
            raise ModelError(f"Tailscale's {name} wasn't the expected file (checksum), so it wasn't used. Try again later.")
        shutil.copyfile(found, bin_dir() / (name + ".part"))
        os.replace(bin_dir() / (name + ".part"), bin_dir() / name)
    storage.remove_files([], within=[path("cache_dir")], dirs=[work])


def node_name() -> str:
    """This PC's name on the tailnet: uc- and six random letters, kept (CT logs are public: not the PC's own name)."""
    f = state_dir() / "uc-name"
    try:
        n = f.read_text(encoding="utf-8").strip()
        if re.fullmatch(r"uc-[a-z]{6}", n):
            return n
    except OSError:
        pass
    n = "uc-" + "".join(secrets.choice(string.ascii_lowercase) for _ in range(6))
    state_dir().mkdir(parents=True, exist_ok=True)
    from . import fsutil
    fsutil.write_text(f, n)
    return n


def _socket() -> str:
    return platform.tailscale_socket(state_dir())


def _cli(*args, timeout: float = 20) -> str:
    return platform.run_text(_cmd("tailscale") + [f"--socket={_socket()}", *args], timeout=timeout)


def status() -> dict:
    try:
        return json.loads(_cli("status", "--json") or "{}")
    except (ValueError, OSError, subprocess.TimeoutExpired):
        return {}


def funnel_target(port: int) -> str:
    """The one place a funnel target is written: only the away listener's port (never the PC's own UI, never the
    home-network phone listener)."""
    from .config import CONFIG
    if not STATE["port"] or port != STATE["port"] or port == CONFIG["ui"]["port"]:
        raise ModelError("Away from home only ever points at its own listener.")
    return f"http://127.0.0.1:{port}"


def _daemon_alive() -> bool:
    return bool(status().get("BackendState"))


def _start_daemon():
    state_dir().mkdir(parents=True, exist_ok=True)
    cmd = _cmd("tailscaled") + ["--tun=userspace-networking", f"--statedir={state_dir()}", f"--socket={_socket()}",
                                "--port=0", "--no-logs-no-support", *platform.tailscaled_os_args()]
    with open(path("logs_dir") / "tailscaled.log", "a", encoding="utf-8") as log:   # the daemon keeps its own copy
        _P["daemon"] = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                        creationflags=platform.NO_WINDOW)


def _run_bg(key: str, args: list):
    """A long CLI command (up, funnel) in the background, its output kept for the login / approval URL."""
    def go():
        try:
            with _LOCK:
                if not STATE["want"]:
                    return
                p = subprocess.Popen(_cmd("tailscale") + [f"--socket={_socket()}", *args], stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True, errors="replace",
                                     creationflags=platform.NO_WINDOW)
                _P[key] = p
            for line in p.stdout:
                _P["out"].append(line)
                m = URL_RX.search(line)
                if m:
                    STATE["approve_url" if key == "funnel" else "auth_url"] = m.group(0)
            p.wait()
        except OSError as e:
            STATE["error"] = f"{key}: {e}"
        finally:
            _P[key] = None
    threading.Thread(target=go, daemon=True, name=f"tailscale-{key}").start()


def _proxies(x) -> list:
    """Every "Proxy" target in Tailscale's serve config (Web -> host:443 -> Handlers -> path -> Proxy)."""
    if isinstance(x, dict):
        return [v for k, v in x.items() if k == "Proxy" and isinstance(v, str)] + [p for v in x.values() for p in _proxies(v)]
    return [p for v in x for p in _proxies(v)] if isinstance(x, list) else []


def _funnel_on(st: dict) -> bool:
    """Funnel is on for this node AND points at this run's away listener (after a restart the port may differ: then
    it's set again)."""
    try:
        f = json.loads(_cli("funnel", "status", "--json") or "{}")
    except (ValueError, OSError, subprocess.TimeoutExpired):
        return False
    host = (st.get("Self") or {}).get("DNSName", "").rstrip(".")
    allowed = any(k.startswith(host) and v for k, v in (f.get("AllowFunnel") or {}).items())
    targets = _proxies(f.get("Web") or {})
    return allowed and (not targets or set(targets) == {funnel_target(STATE["port"])})


def tick():
    """One step of the driver (every 2 s while wanted): -> the phase."""
    if not STATE["want"]:
        return STATE["phase"]
    if not have_binaries():
        STATE["phase"] = "fetching"
        if time.time() - _P["fetch_failed"] < FETCH_RETRY_S:
            return STATE["phase"]   # the last try failed (its error is shown): wait before downloading again
        try:
            fetch_binaries(progress=lambda name, done, total: STATE.update(progress={"done": done, "total": total}))
        except Exception:
            _P["fetch_failed"] = time.time()
            raise
        finally:
            STATE["progress"] = None
        STATE["error"] = ""
    if not _daemon_alive():
        with _LOCK:
            if not STATE["want"]:
                return STATE["phase"]
            if _P["daemon"] is None or _P["daemon"].poll() is not None:
                _start_daemon()
        STATE["phase"] = "starting"
        return STATE["phase"]
    st = status()
    bs = st.get("BackendState")
    if bs in ("NeedsLogin", "Stopped", "NoState"):
        if _P["up"] is None:
            _run_bg("up", ["up", "--unattended", f"--hostname={node_name()}", "--accept-dns=false",
                           "--accept-routes=false"])
        STATE["auth_url"] = st.get("AuthURL") or STATE["auth_url"]
        STATE["phase"] = "sign-in"
    elif bs == "Running":
        STATE["host"] = (st.get("Self") or {}).get("DNSName", "").rstrip(".") or None
        STATE["auth_url"] = None
        if _funnel_on(st):
            STATE.update(phase="ready", approve_url=None)
        else:
            if _P["funnel"] is None:
                _run_bg("funnel", ["funnel", "--bg", "--https=443", funnel_target(STATE["port"])])
            STATE["phase"] = "approve" if STATE["approve_url"] else "starting"
    else:
        STATE["phase"] = "starting"
    return STATE["phase"]


def start(port: int):
    """Switch on: the driver walks to ready (the away listener on `port` is the only funnel target)."""
    ok, why = platform.supports("away")
    if not ok:
        raise ModelError(why)
    STATE.update(want=True, port=port, error="")
    _P["fetch_failed"] = 0.0   # switched on by hand: try a failed download again now
    if _P["driver"] is not None and _P["driver"].is_alive():
        return

    def drive():
        while STATE["want"]:
            try:
                tick()
            except Exception as e:   # noqa: BLE001 - shown in SafeHouse; it tries again
                STATE["error"] = str(e)[:300]
            time.sleep(2)
    _P["driver"] = threading.Thread(target=drive, daemon=True, name="tailnet")
    _P["driver"].start()


def _end_processes():
    """Our tailscale processes, ended and waited for (the daemon's files are free after)."""
    for key in ("up", "funnel", "daemon"):
        p = _P.get(key)
        if p is not None and p.poll() is None:
            p.terminate()
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()
        _P[key] = None


def stop():
    """Switch off: nothing reaches this PC from outside any more (funnel reset, down); the sign-in is kept."""
    with _LOCK:
        STATE.update(want=False, phase="off", auth_url=None, approve_url=None)
    for args in (["funnel", "reset"], ["down"]):
        try:
            _cli(*args)
        except (OSError, subprocess.TimeoutExpired):
            pass
    with _LOCK:
        _end_processes()


def at_exit():
    """UnderCover is exiting (Quit, restart, the window closed): nothing left pointing at a listener that's gone, and
    no daemon left behind. Quick (a closing console gets a few seconds); the sign-in is kept for next time."""
    p = _P.get("daemon")
    if p is None or p.poll() is not None:
        return
    STATE["want"] = False
    try:
        _cli("funnel", "reset", timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        pass
    for key in ("up", "funnel", "daemon"):
        q = _P.get(key)
        if q is not None and q.poll() is None:
            q.terminate()   # no wait: this process is about to end, and a closing console has seconds


def forget():
    """Switch off and sign this PC out of Tailscale (the node leaves the tailnet); its keys (the state folder) are
    deleted. With away from home off the daemon isn't running: it's started for the logout."""
    if state_dir().exists() and have_binaries() and not _daemon_alive():
        with _LOCK:
            if _P["daemon"] is None or _P["daemon"].poll() is not None:
                _start_daemon()
        t0 = time.time()
        while not _daemon_alive() and time.time() - t0 < 15:
            time.sleep(0.3)
    if _daemon_alive():
        try:
            _cli("logout")
        except (OSError, subprocess.TimeoutExpired):
            pass
    stop()
    if state_dir().exists():
        storage.remove_files([], within=[path("cache_dir")], dirs=[state_dir()])
    STATE["host"] = None


def snapshot() -> dict:
    """For SafeHouse and the Doctor."""
    ok, why = platform.supports("away")
    return {"supported": ok, "why": why, "on": STATE["want"], "phase": STATE["phase"], "auth_url": STATE["auth_url"],
            "approve_url": STATE["approve_url"], "link": f"https://{STATE['host']}" if STATE["phase"] == "ready" else None,
            "host": STATE["host"], "error": STATE["error"], "progress": STATE["progress"],
            "downloaded": have_binaries(), "name": node_name() if state_dir().exists() else None}
