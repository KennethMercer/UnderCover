"""Phone access (phase 11): the switch, pairing a phone, and the per-device tokens. Off by default.

When it's on, the server also listens on the PC's home-network address (`lan_address()`, private addresses only) and
every request that doesn't come from this PC must carry a paired device's token (app/server.py `_gate`).

- **Pairing:** the PC asks for a code (`new_pairing()`): six characters, valid for 5 minutes and once, shown on the PC as
  a QR code of `http://<address>:<port>/pair#<code>` and as text to type. The phone sends it back with a name
  (`claim()`); at most 5 wrong codes, then the pairing is dropped; at most 10 tries per address and 30 in all every 10
  minutes.
- **Tokens:** 32 random bytes per device, given to the phone once (an HttpOnly cookie) and stored here only as their
  SHA-256 (`device_for()` compares hashes in constant time). Revoke removes the record, so the token stops working at
  its next request.
- **Where:** `phone.json` next to the config file (`{"enabled", "devices": [{id, name, hash, paired, seen, ip}]}`;
  git-ignored), so a sandbox server with its own config has its own. In test mode (storage.ALLOWED_ROOTS) it is only
  written inside the test's folders.
"""
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import socket
import threading
import time
from pathlib import Path

from . import config, fsutil

PAIR_SECONDS = 300
MAX_TRIES = 5             # wrong codes before the pairing is dropped
RATE = (600, 10, 30)      # window (s), tries per address, tries in all
MAX_DEVICES = 10
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # no 0/O, 1/I: easy to type from the screen
COOKIE = "uc_device"
SEEN_EVERY = 300          # write "last seen" at most this often (s)

_LOCK = threading.RLock()
_STATE: dict = {}
_FILE: list = [None]      # the file _STATE was read from (tests repoint the config)
PAIRING: dict | None = None
_TRIES: list = []         # (time, address) of every claim
_SEEN_AT: dict = {}       # token hash -> when "last seen" was last written


class PhoneError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def state_file() -> Path:
    return Path(config.CONFIG_FILE).with_name("phone.json")


def _load() -> dict:
    f = state_file()
    if _FILE[0] != f:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        _STATE.clear()
        _STATE.update({"enabled": bool(data.get("enabled")), "devices": [d for d in data.get("devices") or []
                                                                         if isinstance(d, dict) and d.get("hash")]})
        _FILE[0] = f
    return _STATE


def _save():
    from . import storage
    f = state_file()
    if storage.ALLOWED_ROOTS is not None and not any(storage._inside(f, r) for r in storage.ALLOWED_ROOTS):
        raise PhoneError(f"Test mode: {f} is outside the test's folders.", 500)
    fsutil.write_text(f, json.dumps(_STATE, indent=1))


def enabled() -> bool:
    with _LOCK:
        return _load()["enabled"]


def set_enabled(on: bool):
    global PAIRING
    with _LOCK:
        _load()["enabled"] = bool(on)
        if not on:
            PAIRING = None
        _save()


def lan_address() -> str | None:
    """The PC's address on the home network: the one its default route uses, if it is a private one (10.x, 172.16-31.x,
    192.168.x); None otherwise. UNDERCOVER_PHONE_ADDR overrides it (tests listen on 127.0.0.2, so nothing reaches the
    network and Windows asks nothing)."""
    forced = os.environ.get("UNDERCOVER_PHONE_ADDR")
    if forced:
        return forced
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.254.254.254", 1))   # UDP: picks the route, sends nothing
            a = s.getsockname()[0]
    except OSError:
        return None
    return a if usable(a) else None


def usable(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return ip.version == 4 and ip.is_private and not ip.is_loopback and not ip.is_link_local and not ip.is_unspecified


# ---------------------------------------------------------------- pairing

def new_pairing() -> dict:
    """A fresh code (any earlier one stops working)."""
    global PAIRING
    with _LOCK:
        if not _load()["enabled"]:
            raise PhoneError("Switch Phone access on first.")
        PAIRING = {"code": "".join(secrets.choice(ALPHABET) for _ in range(6)), "until": time.time() + PAIR_SECONDS,
                   "tries": 0}
        return dict(PAIRING)


def cancel_pairing():
    global PAIRING
    with _LOCK:
        PAIRING = None


def pairing() -> dict | None:
    with _LOCK:
        if PAIRING and PAIRING["until"] > time.time():
            return dict(PAIRING)
        return None


def _rate(addr: str):
    now = time.time()
    window, per_addr, total = RATE
    _TRIES[:] = [t for t in _TRIES if now - t[0] < window]
    if sum(a == addr for _, a in _TRIES) >= per_addr or len(_TRIES) >= total:
        raise PhoneError("Too many tries. Wait a few minutes, then make a new code on the PC.", 429)
    _TRIES.append((now, addr))


def clean_name(name: str) -> str:
    name = re.sub(r"[\x00-\x1f\x7f<>]", "", str(name or "")).strip()
    return re.sub(r"\s+", " ", name)[:40] or "Phone"


def claim(code: str, name: str, addr: str) -> tuple[str, dict]:
    """Pair a phone with the code the PC shows: -> (token, device). The token is only ever returned here."""
    global PAIRING
    code = re.sub(r"[^A-Z0-9]", "", str(code or "").upper())
    with _LOCK:
        _rate(addr)
        st = _load()
        if not st["enabled"]:
            raise PhoneError("Phone access is off on the PC.", 403)
        if not PAIRING or PAIRING["until"] <= time.time():
            PAIRING = None
            raise PhoneError("That code has run out. Make a new one on the PC.", 403)
        if not hmac.compare_digest(code.encode(), PAIRING["code"].encode()):
            PAIRING["tries"] += 1
            if PAIRING["tries"] >= MAX_TRIES:
                PAIRING = None
                raise PhoneError("Too many wrong codes: that one is cancelled. Make a new one on the PC.", 403)
            raise PhoneError("That isn't the code on the PC. Check it and try again.", 403)
        PAIRING = None   # once
        if len(st["devices"]) >= MAX_DEVICES:
            raise PhoneError(f"{MAX_DEVICES} phones are paired already. Revoke one on the PC first.", 409)
        token = secrets.token_urlsafe(32)
        now = time.strftime("%Y-%m-%d %H:%M")
        dev = {"id": secrets.token_hex(5), "name": clean_name(name), "hash": hash_token(token), "paired": now,
               "seen": now, "ip": addr}
        st["devices"].append(dev)
        _save()
        return token, public(dev)


# ---------------------------------------------------------------- devices

def hash_token(token: str) -> str:
    return hashlib.sha256(str(token).encode()).hexdigest()


def public(d: dict) -> dict:
    return {k: d.get(k) for k in ("id", "name", "paired", "seen", "ip")}


def devices() -> list[dict]:
    with _LOCK:
        return [public(d) for d in _load()["devices"]]


def device_for(token: str | None, addr: str = "") -> dict | None:
    """The paired device this token belongs to (None: no token, unknown, revoked, or phone access is off)."""
    if not token:
        return None
    h = hash_token(token)
    with _LOCK:
        st = _load()
        if not st["enabled"]:
            return None
        found = None
        for d in st["devices"]:
            if hmac.compare_digest(d["hash"], h):
                found = d
        if found is None:
            return None
        now = time.time()
        if now - _SEEN_AT.get(h, 0) > SEEN_EVERY:
            _SEEN_AT[h] = now
            found["seen"], found["ip"] = time.strftime("%Y-%m-%d %H:%M"), addr or found.get("ip")
            try:
                _save()
            except (OSError, PhoneError):
                pass
        return {**public(found), "hash": found["hash"]}


def still_valid(token_hash: str) -> bool:
    """For long answers (the job stream): is this device still paired and phone access still on?"""
    with _LOCK:
        st = _load()
        return st["enabled"] and any(hmac.compare_digest(d["hash"], token_hash) for d in st["devices"])


def revoke(device_id: str) -> dict:
    with _LOCK:
        st = _load()
        d = next((d for d in st["devices"] if d["id"] == device_id), None)
        if d is None:
            raise PhoneError("That phone isn't paired.", 404)
        st["devices"].remove(d)
        _save()
        return public(d)


def name_from_agent(ua: str) -> str:
    """A first guess at the phone's name from its browser ("Pixel 8", "iPhone"); the phone can change it."""
    ua = ua or ""
    for k in ("iPhone", "iPad"):
        if k in ua:
            return k
    m = re.search(r"Android [\d.]+; ([^;)]+?)(?: Build/[^;)]*)?\)", ua)
    if m and m.group(1).strip() not in ("K", "wv"):
        return clean_name(m.group(1))
    return "Android phone" if "Android" in ua else "Phone"
