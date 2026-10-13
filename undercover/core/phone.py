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
AWAY_COOKIE = "uc_away"   # the away-from-home address's own token (phase 20): a cookie can't cross origins
AWAY_CODE_S = 600         # a transfer code (home Wi-Fi -> the away address) lasts 10 minutes, and works once
_AWAY_CODES: dict = {}    # sha256(code) -> (device id, expires)
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
        _STATE.update({"enabled": bool(data.get("enabled")), "away": bool(data.get("away")),
                       "devices": [d for d in data.get("devices") or [] if isinstance(d, dict) and d.get("hash")]})
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


def away_on() -> bool:
    with _LOCK:
        return _load()["away"]


def set_away(on: bool):
    with _LOCK:
        _load()["away"] = bool(on)
        _save()


def away_code(device_id: str) -> str:
    """A single-use transfer code for a paired phone (asked on the home Wi-Fi): redeemed at the away address within
    AWAY_CODE_S, it gives that phone a token there too. 128 random bits: nothing to guess on the open internet."""
    with _LOCK:
        if not any(d["id"] == device_id for d in _load()["devices"]):
            raise PhoneError("That phone isn't paired.", 404)
        now = time.time()
        for k in [k for k, (_, exp) in _AWAY_CODES.items() if exp < now]:
            _AWAY_CODES.pop(k)
        code = secrets.token_urlsafe(16)
        _AWAY_CODES[hash_token(code)] = (device_id, now + AWAY_CODE_S)
        return code


def redeem_away(code: str) -> tuple[str, dict]:
    """A transfer code -> (a new away token, the device). The code works once; the device keeps one away token (a new
    one replaces the old). Revoke removes the device, and both its tokens with it."""
    with _LOCK:
        hit = _AWAY_CODES.pop(hash_token(code or ""), None)
        if hit is None or hit[1] < time.time():
            raise PhoneError("That link has expired or was used already. On the home Wi-Fi, ask for a new one.", 403)
        st = _load()
        d = next((d for d in st["devices"] if d["id"] == hit[0]), None)
        if d is None:
            raise PhoneError("That phone isn't paired any more.", 403)
        token = secrets.token_urlsafe(32)
        d["away_hash"] = hash_token(token)
        _save()
        return token, public(d)


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


def device_for(token: str | None, addr: str = "", away: bool = False) -> dict | None:
    """The paired device this token belongs to (None: no token, unknown, revoked, or phone access is off). away: the
    token is checked against the device's away token (the away-from-home address), never its home one."""
    if not token:
        return None
    h = hash_token(token)
    with _LOCK:
        st = _load()
        if not st["enabled"] or (away and not st["away"]):
            return None
        found = None
        for d in st["devices"]:
            if hmac.compare_digest(d.get("away_hash" if away else "hash") or "", h):
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
        return {**public(found), "hash": found["away_hash" if away else "hash"]}


def still_valid(token_hash: str) -> bool:
    """For long answers (the job stream): is this device still paired and phone access still on?"""
    with _LOCK:
        st = _load()
        return st["enabled"] and any(hmac.compare_digest(d["hash"], token_hash) or
                                     (st["away"] and hmac.compare_digest(d.get("away_hash") or "", token_hash))
                                     for d in st["devices"])


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


# ---------------------------------------------------------------- Windows: the network type and the firewall (phase 11b)
# Read-only. The Doctor shows the result while Phone access is on; nothing here changes a Windows setting.

CATEGORY = {0: "Public", 1: "Private", 2: "Domain"}
_WIN: dict = {}   # (address, port) -> (when, result): the queries take ~1 s, so they're kept a while
WIN_KEEP = 600    # the phone listener reads them again when they're older (app/phone_api.warm); "Check everything" now
_WIN_BUSY: set = set()


def parse_rules(text: str) -> list[dict]:
    """`netsh advfirewall firewall show rule ... verbose` -> one {key: value} per rule (keys lower-case)."""
    rules, cur = [], None
    for line in text.splitlines():
        if line.startswith("Rule Name:"):
            cur = {"rule name": line.split(":", 1)[1].strip()}
            rules.append(cur)
        elif cur is not None and ":" in line and not line.startswith("-"):
            k, v = line.split(":", 1)
            cur.setdefault(k.strip().lower(), v.strip())
    return rules


def parse_states(text: str) -> dict:
    """`netsh advfirewall show allprofiles state` -> {"Domain": True, "Private": True, "Public": False} (True = on)."""
    out, prof = {}, None
    for line in text.splitlines():
        m = re.match(r"(Domain|Private|Public) Profile Settings", line.strip())
        if m:
            prof = m.group(1)
        elif prof and line.strip().startswith("State"):
            out[prof] = line.split()[-1].upper() == "ON"
            prof = None
    return out


def _port_in(spec: str, port: int) -> bool:
    if not spec or spec.lower() == "any":
        return True
    for part in spec.split(","):
        a, _, b = part.strip().partition("-")
        if a.isdigit() and (int(a) <= port <= int(b or a)):
            return True
    return False


def firewall_verdict(rules: list[dict], programs: set, profile: str, on: bool | None, port: int) -> tuple[str, str]:
    """-> (state, plain words) for "may phones reach this program on a `profile` network?". Block rules win in Windows."""
    if not rules:   # every Windows has dozens of inbound rules: none read means netsh spoke another language
        return "warn", ("Couldn't read Windows Firewall's rules (a Windows in another language?), so the Doctor can't tell "
                        "whether phones get through.")
    if on is False:
        return "info", (f"Windows Firewall is off for {profile.lower()} networks, so nothing stops a phone reaching UnderCover "
                        "(the pairing code and the phone's key still do).")
    progs = {os.path.normcase(os.path.normpath(p)) for p in programs}

    def applies(r):
        prog = r.get("program", "Any")
        profs = r.get("profiles", "Any")
        return (r.get("enabled", "").lower() == "yes" and r.get("direction", "").lower() == "in"
                and (prog.lower() == "any" or os.path.normcase(os.path.normpath(prog)) in progs)
                and r.get("protocol", "Any").upper() in ("TCP", "ANY") and _port_in(r.get("localport", "Any"), port)
                and (profs.lower() == "any" or profile.lower() in profs.lower()))
    mine = [r for r in rules if applies(r)]
    how = ("Windows Security, Firewall & network protection, Allow an app through firewall, find Python, and tick "
           "Private (not Public).")
    if profile == "Public":
        how = "Make the home network Private first (see Home network), then: " + how
    if any(r.get("action", "").lower() == "block" for r in mine):
        return "bad", f"Windows Firewall blocks UnderCover (Python) on {profile.lower()} networks, so phones can't connect. {how}"
    if any(r.get("action", "").lower() == "allow" for r in mine):
        return "ok", f"Windows Firewall lets UnderCover (Python) in on {profile.lower()} networks."
    return "warn", (f"Windows Firewall has no rule letting UnderCover (Python) in on {profile.lower()} networks, so phones "
                    f"can't connect. Windows usually asks the first time; if it didn't (or the answer was Cancel): {how}")


def windows_check(addr: str, port: int, programs: set, wait: bool = True, fresh: bool = False) -> dict | None:
    """The network type of the connection holding `addr` and the firewall's verdict for it:
    {"network": {name, alias, category} | None, "firewall": (state, words) | None, "error": str | None}. Cached WIN_KEEP s
    (fresh: read again now). wait=False (the server's fast check, startup): never blocks; with nothing cached it starts
    the reading in the background and returns None."""
    import time as _t
    ipaddress.ip_address(addr)   # only a real address goes into the PowerShell line
    hit = _WIN.get((addr, port))
    if hit and not fresh and _t.time() - hit[0] < WIN_KEEP:
        return hit[1]
    if not wait:
        if (addr, port) not in _WIN_BUSY:
            _WIN_BUSY.add((addr, port))
            threading.Thread(target=_read_windows, args=(addr, port, programs), daemon=True, name="phone-windows").start()
        return hit[1] if hit else None
    return _read_windows(addr, port, programs)


def _read_windows(addr: str, port: int, programs: set) -> dict:
    import time as _t
    from . import platform
    out = {"network": None, "firewall": None, "error": None}
    try:
        raw, states_text, rules_text = platform.network_and_firewall(addr)   # the OS part (PowerShell + netsh)
        states, rules = parse_states(states_text), parse_rules(rules_text)
        if raw:
            n = json.loads(raw)
            n = n[0] if isinstance(n, list) else n
            out["network"] = {"name": n.get("Name"), "alias": n.get("InterfaceAlias"),
                              "category": CATEGORY.get(n.get("NetworkCategory"), str(n.get("NetworkCategory")))}
        if out["network"]:   # unknown network: no verdict for a profile it may not have
            profile = out["network"]["category"]
            out["firewall"] = firewall_verdict(rules, programs, profile, states.get(profile), port)
    except Exception as e:   # noqa: BLE001 - say we couldn't tell, never fail the Doctor
        out["error"] = f"{type(e).__name__}: {e}"[:200]
    _WIN[(addr, port)] = (_t.time(), out)
    _WIN_BUSY.discard((addr, port))
    return out
