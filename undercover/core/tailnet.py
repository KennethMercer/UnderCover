"""Away from home (phase 20; the maintainers' notes, phase20-plan, the spike in the maintainers' notes, tailscale): Tailscale, hidden.
People never meet it: one switch in SafeHouse, one sign-in, one approval, then a link that works anywhere.

  - the binaries: tailscaled + tailscale from the pinned MSI (downloaded on first use, the user's answer; SHA-256 of the
    MSI and of both binaries checked), in <models>/tailscale (they download again; never in the vault);
  - the daemon: userspace networking (no adapter, no driver, no admin), our own pipe, state in <cache>/tailscale-state
    (the node's keys: never in the vault, the diagnostics file or git), `up --unattended` (else it drops when the CLI
    exits), a random node name (uc-xxxxxx: Certificate Transparency logs are public);
  - Funnel: https://<name>.<tailnet>.ts.net -> 127.0.0.1:<the away listener> ONLY (funnel_target refuses anything
    else: never the PC's own 7860, never the home-network listener). That listener asks for an away token (pairing
    stays on the home Wi-Fi; app/phone_api).
  - Private (Deep Cover, 1.2): Serve instead of Funnel, the same name and target, reachable only from devices on the
    tailnet (the phone runs Tailscale). Switching resets the serve config, so only one of the two is ever on.
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

# A release ships the phone link's setup only once it has been checked live with Tailscale (the maintainers' notes,
# release-1.2-plan; the user, 2026-11-08: 1.2 ships without it). False: no Set up on the card, no Phone link in the tray,
# the link never starts, and Forget never starts the daemon; phones paired on the home Wi-Fi before keep working there.
# The release that offers the link sets this to True (and its test with it).
OFFERED = False
if os.environ.get("UNDERCOVER_TEST_LINK") == "1" and os.environ.get("UNDERCOVER_TEST_ROOTS"):
    OFFERED = True   # (smoke's sandbox server, in test mode only: the phone's own checks run with the link)
NOT_OFFERED_WHY = "Your phone's link comes in a later UnderCover. Phones connected on the home Wi-Fi before keep working there."
VERSION = "1.104.1"
MSI = {"url": f"https://pkgs.tailscale.com/stable/tailscale-setup-{VERSION}-amd64.msi", "size": 38_718_976,
       "sha256": "6da8bcdb0d45a214cae0e8a88c2987ad48dec134bf04dffb9c05f1354c02b0b4"}
# the daemon's and the command line's checksums, in the order platform.tailscale_names() gives their file names.
# Only the Windows MSI's pair exists: another OS has none (never these hashes under its names) until it gets its own
# package, and its own pair, when the away feature comes there
BINS = dict(zip(platform.tailscale_names(), ("ae0195119cc8efbd88d11bff56394ff9f1c08dc9d51957a400d4cb35c74e4bb1",
                                             "72a499c653da6c16a50908d6ceb244d12b5f4834ab8ac19a87f2ca3117e8c20a"))) if platform.WINDOWS else {}
CMD: dict = {}   # tests: {"tailscale": [...], "tailscaled": [...]} instead of the downloaded binaries
URL_RX = re.compile(r"https://login\.tailscale\.com/\S+")
STATE = {"want": False, "phase": "off", "auth_url": None, "approve_url": None, "host": None, "error": "",
         "progress": None, "port": None, "private": False}
_P = {"daemon": None, "up": None, "funnel": None, "serve": None, "driver": None, "out": [], "fetch_failed": 0.0,
      "public": False}
OUT_LINES = 200          # the CLI's output kept for its sign-in and approval links (the newest lines)
LOG_MAX = 5 << 20        # tailscaled.log past this at a start: set aside as tailscaled.old.log
EXPOSE = ("funnel", "serve")   # the CLI commands that publish the away listener: public, or the tailnet only
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
    if fake and storage.ALLOWED_ROOTS is not None:   # a sandbox server in smoke: tests/fake_tailscale.py, test mode only
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


def _pipe_key(create: bool = False) -> str:
    """The state folder's own random key in its pipe's name: made once, by the daemon's start (create), kept beside the
    node's keys (it goes with Forget), read back so a daemon left running is found again. Anything else that asks (the
    CLI, a status) only reads it: with none there's no daemon of ours to reach, and asking never makes the state folder
    (it came back after Forget: a_link, 2026-11-09)."""
    f = state_dir() / "uc-pipe"
    try:
        k = f.read_text(encoding="utf-8").strip()
        if re.fullmatch(r"[0-9a-f]{32}", k):
            return k
    except OSError:
        pass
    if not create:
        return ""
    k = secrets.token_hex(16)
    state_dir().mkdir(parents=True, exist_ok=True)
    from . import fsutil
    fsutil.write_text(f, k)
    return k


def _socket(create: bool = False) -> str:
    return platform.tailscale_socket(state_dir(), _pipe_key(create))


def _end_orphans():
    """Before a daemon starts: the daemon this state folder last started (its pid kept beside it), if it's still
    running and is our own tailscaled program, is a leftover (a crash, an update under it). It's ended, so two never
    share the state folder and none keeps the PC on the tailnet unwatched. Only that one: another copy's daemon has its
    own state folder and pid."""
    f = state_dir() / "uc-daemon"
    try:
        pid, started = (int(x) for x in f.read_text(encoding="utf-8").split())
    except (OSError, ValueError):
        return
    prog_file = bin_dir() / platform.tailscale_names()[0]
    mine = _P["daemon"].pid if _P["daemon"] is not None and _P["daemon"].poll() is None else None
    if CMD or pid in (mine, os.getpid()) or not prog_file.is_file():
        return
    try:   # (the same process: its pid, its start time and our program, so never one Windows gave that pid since)
        same = platform.process_started(pid) == started and platform.process_path(pid)
    except platform.NotOnThisOS:
        return
    if same and os.path.normcase(same) == os.path.normcase(str(prog_file.resolve())):
        try:
            os.kill(pid, 15)   # (SIGTERM; Windows ends the process)
            from .doctor import log
            log("Your phone: a secure-link piece left running from before was stopped.")
        except OSError:
            pass


def end_leftovers() -> int:
    """At startup, while this release doesn't offer the link: a tailscaled still running from this install's own
    models folder is a leftover from an older UnderCover (a crash, End task, an update over a running copy), which kept
    no record of it, so _end_orphans can't find it. It's ended, so nothing keeps the PC on the tailnet after the update.
    Only our own program, matched by its full path and again (with the start time read at the listing) just before it's
    ended; never another Tailscale. Logged once. -> how many were ended."""
    if OFFERED or CMD:
        return 0
    prog = bin_dir() / platform.tailscale_names()[0]
    try:
        if not prog.is_file():
            return 0
        want = {os.path.normcase(str(prog.absolute())), os.path.normcase(str(prog.resolve()))}   # (a folder reached by a link: either name)
        found = {pid: platform.process_started(pid) for pid, p in platform.processes().items()
                 if pid != os.getpid() and os.path.normcase(p) in want}
    except (platform.NotOnThisOS, OSError):
        return 0
    ended = 0
    for pid, started in found.items():
        try:   # (the same process still: our program and the start time it had, never one given that pid since)
            if started is None or platform.process_started(pid) != started \
                    or os.path.normcase(platform.process_path(pid)) not in want:
                continue
            os.kill(pid, 15)   # (SIGTERM; Windows ends the process)
            ended += 1
        except OSError:
            pass
    if ended:
        from .doctor import log
        log("Your phone: a secure-link piece an older UnderCover left running was stopped (this version doesn't use it).")
    return ended


def _keep_pid(p):
    """The daemon this state folder started: its pid and start time, so the next start can tell a leftover."""
    from . import fsutil
    try:
        started = platform.process_started(p.pid)
    except platform.NotOnThisOS:
        started = None
    try:
        if started is not None:
            fsutil.write_text(state_dir() / "uc-daemon", f"{p.pid} {started}")
    except OSError:
        pass


def _drop_pid():
    """Our daemon ended cleanly: no record left for a later process Windows gives that pid."""
    try:
        (state_dir() / "uc-daemon").unlink()
    except OSError:
        pass


def _cli(*args, timeout: float = 20) -> str:
    if not _pipe_key():   # (no key: no daemon of ours to reach, and a name without one is no one's to talk to)
        return ""
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
        raise ModelError("Your phone's link only ever points at its own listener.")
    return f"http://127.0.0.1:{port}"


def _daemon_alive() -> bool:
    return bool(status().get("BackendState"))


def _start_daemon():
    state_dir().mkdir(parents=True, exist_ok=True)
    log_file = path("logs_dir") / "tailscaled.log"
    try:   # (the daemon's own log: the last run's kept beside it once it's past LOG_MAX)
        if log_file.stat().st_size > LOG_MAX:
            log_file.replace(log_file.with_name("tailscaled.old.log"))
    except OSError:
        pass
    _end_orphans()
    cmd = _cmd("tailscaled") + ["--tun=userspace-networking", f"--statedir={state_dir()}", f"--socket={_socket(create=True)}",
                                "--port=0", "--no-logs-no-support", *platform.tailscaled_os_args()]
    with open(log_file, "a", encoding="utf-8") as log:   # the daemon keeps its own copy
        _P["daemon"] = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                        creationflags=platform.NO_WINDOW)
    _keep_pid(_P["daemon"])


def _run_bg(key: str, args: list):
    """A long CLI command (up, funnel) in the background, its output kept for the login / approval URL."""
    def go():
        try:
            with _LOCK:
                if not STATE["want"] or (key in EXPOSE and key != _mode()):   # (Private switched on or off since the
                    return                                                    # step chose it: the next step chooses again)
                p = subprocess.Popen(_cmd("tailscale") + [f"--socket={_socket()}", *args], stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, **platform.TEXT, creationflags=platform.NO_WINDOW)
                _P[key] = p
            for line in p.stdout:
                _P["out"].append(line)
                del _P["out"][:-OUT_LINES]
                m = URL_RX.search(line)
                if m:
                    STATE["approve_url" if key in EXPOSE else "auth_url"] = m.group(0)
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


def _mode() -> str:
    return "serve" if STATE["private"] else "funnel"


def _funnel_on(st: dict) -> bool:
    """The away listener is published the way this run wants it AND points at this run's away listener (after a
    restart the port may differ: then it's set again). Funnel: allowed on the public internet. Private (Serve): the
    tailnet only, so Funnel must be off for this node."""
    try:
        f = json.loads(_cli(_mode(), "status", "--json") or "{}")
    except (ValueError, OSError, subprocess.TimeoutExpired):
        return False
    host = (st.get("Self") or {}).get("DNSName", "").rstrip(".")
    if not host:   # (no name yet: nothing is published, nothing is ready)
        return False
    allowed = any(k.startswith(host) and v for k, v in (f.get("AllowFunnel") or {}).items())
    _P["public"] = allowed
    targets = _proxies(f.get("Web") or {})
    if STATE["private"]:
        return not allowed and set(targets) == {funnel_target(STATE["port"])}
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
            m = _mode()
            if STATE["private"] and _P["public"]:   # a public flag left from before (a crash in Funnel mode): off first,
                _cli("funnel", "reset")              # never trusting serve to clear it
                _P["public"] = False
            if _P[m] is None:
                _run_bg(m, [m, "--bg", "--https=443", funnel_target(STATE["port"])])
            STATE["phase"] = "approve" if STATE["approve_url"] else "starting"
    else:
        STATE["phase"] = "starting"
    return STATE["phase"]


def start(port: int, private: bool = False):
    """Switch on: the driver walks to ready (the away listener on `port` is the only funnel target). private: Serve
    on the tailnet instead of Funnel."""
    if not OFFERED:
        raise ModelError(NOT_OFFERED_WHY)
    ok, why = platform.supports("away")
    if not ok:
        raise ModelError(why)
    if STATE["want"]:
        set_private(private)
    STATE.update(want=True, port=port, error="", private=bool(private),
                 phase="starting" if STATE["phase"] == "off" else STATE["phase"])   # (never "off" while it's on: the first step is up to 2 s away)
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


def set_private(on: bool):
    """Funnel or Serve. A change resets the serve config (both off) and ends a pending publish; the driver then sets
    up the other one (Serve may ask for the tailnet's HTTPS approval once)."""
    with _LOCK:
        if STATE["private"] == bool(on):
            return
        STATE.update(private=bool(on), approve_url=None)
        if STATE["phase"] == "ready":
            STATE["phase"] = "starting"
        for key in EXPOSE:
            p = _P.get(key)
            if p is not None and p.poll() is None:
                p.terminate()
            _P[key] = None
    if _daemon_alive():
        try:
            _cli("serve", "reset")
        except (OSError, subprocess.TimeoutExpired):
            pass


def _resets() -> list:
    """What takes the away listener off the net: funnel reset (the whole serve config, Funnel's and Serve's), and in
    private mode serve reset as well, so a Serve config can't outlive Off whatever the CLI's reset covers."""
    return [["funnel", "reset"]] + ([["serve", "reset"]] if STATE["private"] else [])


def _end_processes():
    """Our tailscale processes, ended and waited for (the daemon's files are free after)."""
    for key in ("up", *EXPOSE, "daemon"):
        p = _P.get(key)
        if p is not None and p.poll() is None:
            p.terminate()
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()
        _P[key] = None


def stop():
    """Switch off: nothing reaches this PC from outside any more (funnel reset, which clears Serve too; down); the
    sign-in is kept."""
    with _LOCK:
        STATE.update(want=False, phase="off", auth_url=None, approve_url=None)
    for args in (*_resets(), ["down"]):
        try:
            _cli(*args)
        except (OSError, subprocess.TimeoutExpired):
            pass
    with _LOCK:
        _end_processes()
    _drop_pid()


def at_exit():
    """UnderCover is exiting (Quit, restart, the window closed): nothing left pointing at a listener that's gone, and
    no daemon left behind. Quick (a closing console gets a few seconds); the sign-in is kept for next time."""
    p = _P.get("daemon")
    if p is None or p.poll() is not None:
        return
    STATE["want"] = False
    for args in _resets():
        try:
            _cli(*args, timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            pass
    for key in ("up", *EXPOSE, "daemon"):
        q = _P.get(key)
        if q is not None and q.poll() is None:
            q.terminate()   # no wait: this process is about to end, and a closing console has seconds
    _drop_pid()


def forget():
    """Switch off and sign this PC out of Tailscale (the node leaves the tailnet); its keys (the state folder) are
    deleted. With the phone link off the daemon isn't running: it's started for the logout. While the link isn't
    offered it never starts: the state folder (the node's keys) is deleted unread, so the node can't come online again
    and ages out of the tailnet's admin console by itself."""
    if not OFFERED:
        stop()
        if state_dir().exists():
            storage.remove_files([], within=[path("cache_dir")], dirs=[state_dir()])
        return
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
    if not OFFERED:
        ok, why = False, NOT_OFFERED_WHY
    return {"supported": ok, "why": why, "offered": OFFERED, "on": STATE["want"], "phase": STATE["phase"], "auth_url": STATE["auth_url"],
            "approve_url": STATE["approve_url"], "link": f"https://{STATE['host']}" if STATE["phase"] == "ready" else None,
            "host": STATE["host"], "error": STATE["error"], "progress": STATE["progress"],
            "downloaded": have_binaries(), "name": node_name() if state_dir().exists() else None,
            "private": STATE["private"]}
