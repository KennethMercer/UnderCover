"""A stand-in for tailscale / tailscaled in smoke's checks (phase 20): no network, no account, nothing real. Its state is
a JSON file named by FAKE_TS_STATE; a check moves it along (signed in, Funnel approved) by writing that file.
    python fake_tailscale.py daemon [flags...]        the daemon: marks itself alive, then waits to be stopped
    python fake_tailscale.py cli --socket=X <command>  status --json | up ... | funnel ... | funnel status --json |
                                                        funnel reset | down | logout
    python fake_tailscale.py set '{"key": value}'      a check's own change (under the same lock as the fake's)
Every command is appended to the state's "calls", so a check can see exactly what the app asked for."""
import contextlib
import json
import os
import sys
import time
from pathlib import Path

F = Path(os.environ["FAKE_TS_STATE"])


def load() -> dict:
    try:
        return json.loads(F.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save(st: dict):
    tmp = F.with_name(F.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(st), encoding="utf-8")
    os.replace(tmp, F)


@contextlib.contextmanager
def locked():
    """Several fake processes (and the check itself) change the file at once: one at a time (a lock file)."""
    lock = F.with_name(F.name + ".lock")
    for _ in range(500):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            time.sleep(0.01)
    else:
        fd = None   # a lock left by a killed process: carry on
    try:
        yield
    finally:
        if fd is not None:
            os.close(fd)
        with contextlib.suppress(OSError):
            os.unlink(lock)


def update(**kw):
    with locked():
        st = load()
        st.update(kw)
        save(st)
    return st


def log(args):
    with locked():
        st = load()
        st.setdefault("calls", []).append(args)
        save(st)


def _lock_byte(fd) -> bool:
    """A lock the OS drops when the holder dies (so "is the daemon alive?" is real, even after a kill)."""
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def daemon_alive() -> bool:
    alive = F.with_name(F.name + ".alive")
    if not alive.exists():
        return False
    fd = os.open(alive, os.O_RDWR)
    try:
        return not _lock_byte(fd)   # it could be locked: nobody holds it, so no daemon
    finally:
        os.close(fd)   # closing drops a lock this process took


def wait_for(key):
    for _ in range(600):
        if load().get(key):
            return True
        time.sleep(0.2)
    return False


mode, args = sys.argv[1], [a for a in sys.argv[2:] if not a.startswith("--socket=")]
if mode == "set":   # a check moves the story along: python fake_tailscale.py set '{"signed_in": true}'
    update(**json.loads(args[0]))
    sys.exit(0)
if mode == "daemon":
    fd = os.open(F.with_name(F.name + ".alive"), os.O_RDWR | os.O_CREAT)
    if not _lock_byte(fd):
        sys.exit("another fake daemon is running")
    log(["tailscaled", *args])
    update(daemon=True, backend=load().get("backend") or "NeedsLogin")
    while True:
        time.sleep(1)
log(args)
st = load()
host = st.get("host", "uc-fakeone.tailfake.ts.net")
if not daemon_alive():
    sys.exit(1)
if args[:2] == ["status", "--json"]:
    out = {"BackendState": st.get("backend", "NeedsLogin"), "Self": {"DNSName": host + "." if st.get("backend") == "Running" else ""}}
    if st.get("backend") == "NeedsLogin" and st.get("up_called"):
        out["AuthURL"] = "https://login.tailscale.com/a/fake-login"
    print(json.dumps(out))
elif args[:1] == ["up"]:
    update(up_called=True, hostname=next((a.split("=", 1)[1] for a in args if a.startswith("--hostname=")), None))
    print("To authenticate, visit:\n\n\thttps://login.tailscale.com/a/fake-login\n", flush=True)
    if not wait_for("signed_in"):
        sys.exit(1)
    update(backend="Running")   # signed in (again: after `down`, `up` reconnects with the kept state)
elif args[:3] == ["funnel", "status", "--json"]:
    print(json.dumps({"TCP": {"443": {"HTTPS": True}}, "Web": {f"{host}:443": {"Handlers": {"/": {"Proxy": st.get("funnel_target")}}}},
                      "AllowFunnel": {f"{host}:443": True}} if st.get("funnel_on") else {}))
elif args[:2] == ["funnel", "reset"]:
    update(funnel_on=False, funnel_target=None)
elif args[:1] == ["funnel"]:
    target = args[-1]
    if not st.get("funnel_approved"):
        print("Funnel is not enabled on your tailnet.\nTo enable, visit:\n\n https://login.tailscale.com/f/funnel?node=fake\n",
              flush=True)
        if not wait_for("funnel_approved"):
            sys.exit(1)
    update(funnel_on=True, funnel_target=target)
elif args[:1] == ["down"]:
    update(backend="Stopped")
elif args[:1] == ["logout"]:
    update(backend="NeedsLogin", logged_out=True, signed_in=False)
