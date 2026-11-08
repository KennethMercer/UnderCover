"""Your phone (phase 11; one link since 1.2.1): pairing a phone, and the one token per device.

One Phone setup at the PC gives every phone one https link (Tailscale Funnel's name for this PC, core/tailnet.py), the same
at home and away: the user never has to think about where they are. Pairing happens at that link, on the internet, so it's
built as a door the whole internet can see (the maintainers' notes, CounterIntelligence, one phone setup):

- **Pairing:** the PC asks for a window (`new_pairing()`, PAIR_SECONDS): a 128-bit code shown as a QR of
  `https://<name>/pair#<code>`. Nothing to guess, and a wrong claim can't end it. Only when the user presses "Type a code
  instead" does a 6-character code exist (`short_code()`), for SHORT_SECONDS and SHORT_TRIES wrong tries; wrong tries end
  the short code alone. With no window open, a claim is refused like any unpaired request (PhoneError.generic).
- **Allow:** a claim that works makes a pending device and hands the phone a nonce (`claim()`), no token. The PC allows
  it (`allow()`), which mints the token, and the phone collects it once on its nonce (`collect()`); `refuse()` removes it.
  A wrong, used or refused nonce looks exactly like "not yet". A photo or a stream of the QR gets a phone as far as that
  click, and no further.
- **Tokens:** 32 random bytes per device, given to the phone once (the `__Host-` cookie, MAIN_COOKIE: Secure, HttpOnly,
  SameSite=Strict, and the browser refuses it on anything but https with Path=/) and stored here only as their SHA-256
  (`device_for()` compares hashes in constant time). Remove removes the record, so the token stops working at its next
  request. A token unused for TOKEN_IDLE_S (30 days) stops by itself (a lost phone); each use renews it.
- **Phones paired on the home Wi-Fi before this** hold a LAN token (`lan_hash`, cookie LAN_COOKIE, good only from the home
  network). Their page offers the move: `move_code()` for a device that shows that token, redeemed at the link
  (`redeem_move()`) for a nonce, and the move then waits for the PC's Allow like a new phone (the code crossed the home
  Wi-Fi in clear: a sniffer who redeems it first gets no further than the PC's card, which names the phone). No cancels
  the move alone: the phone keeps its LAN token and may ask again. The LAN token is dropped the first time the new one is
  used (a phone that never arrives isn't stranded), and the LAN listener is needed only while any device still holds one
  (`lan_needed()`).
- **Where:** `phone.json` next to the config file (git-ignored): `{"enabled", "away_private", "lan", "devices": [{id, name,
  hash, lan_hash, pending, paired, seen, ip, used, lan_used}]}`. A file from before is read as it was: its away token becomes
  the token, its home token the LAN one. In test mode (storage.ALLOWED_ROOTS) it is only written inside the test's folders.
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

PAIR_SECONDS = 300        # the pairing window (the QR's 128-bit code)
SHORT_SECONDS = 120       # the typed code, from "Type a code instead": this long, and SHORT_TRIES wrong tries end it
SHORT_TRIES = 5
PENDING_S = 600           # a phone that claimed waits for Allow this long, then it's forgotten
TOKEN_IDLE_S = 30 * 86400  # a token unused this long stops working; each use renews it (and the cookie's life)
MAX_DEVICES = 10
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # no 0/O, 1/I: easy to type from the screen
MAIN_COOKIE = "__Host-uc"  # the one token's cookie: the browser takes a __Host- name only Secure, Path=/, no Domain
LAN_COOKIE = "uc_device"   # a token from before the move, on the home Wi-Fi only
COOKIE, AWAY_COOKIE = LAN_COOKIE, MAIN_COOKIE   # (the names the listeners used before 1.2.1)
MOVE_CODE_S = 600         # a move code (a LAN-paired phone -> the link) lasts 10 minutes, and works once
AWAY_CODE_S = MOVE_CODE_S

SEEN_EVERY = 300          # write "last seen" at most this often (s)

_LOCK = threading.RLock()
_STATE: dict = {}
_FILE: list = [None]      # the file _STATE was read from (tests repoint the config)
PAIRING: dict | None = None   # {"code": the QR's code (memory only), "until", "short": None | {"code", "until", "tries"}, "wrong"}
_MOVE_CODES: dict = {}    # code hash -> (device id, expiry)
_AWAY_CODES = _MOVE_CODES
_HANDOVER: dict = {}      # nonce hash -> (the token, expiry): minted at Allow, collected once by the phone that claimed
_SEEN_AT: dict = {}       # token hash -> when "last seen" was last written


class PhoneError(Exception):
    """generic: answer with the listener's generic refusal (no message, no hint of what exists)."""
    def __init__(self, message: str, status: int = 400, generic: bool = False):
        super().__init__(message)
        self.status = status
        self.generic = generic


def state_file() -> Path:
    return Path(config.CONFIG_FILE).with_name("phone.json")


def _file_key(f: Path):
    """The file and its version on disk: read again when either changes (the config repointed, or the file written by
    another process, restored from the vault, or edited by hand while UnderCover runs)."""
    try:
        st = f.stat()
        return (f, st.st_mtime_ns, st.st_size)
    except OSError:
        return (f, None, None)


def _load() -> dict:
    f = state_file()
    key = _file_key(f)
    if _FILE[0] != key:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        devices = []
        for d in data.get("devices") or []:
            if not isinstance(d, dict) or not (d.get("hash") or d.get("lan_hash") or d.get("away_hash")):
                continue
            if "version" not in d:   # a file from before the one link: its away token is the token, its home token the LAN one
                d = {**{k: v for k, v in d.items() if k not in ("away_hash", "away_used")},
                     "lan_hash": d.get("hash"), "lan_used": d.get("used"), "hash": d.get("away_hash"), "used": d.get("away_used"),
                     "version": 1}
            if d.get("pending") and time.time() - float(d.get("asked") or 0) > PENDING_S:
                continue   # (a phone that claimed and was never allowed: forgotten)
            if d.get("move_nonce") and time.time() - float(d.get("move_asked") or 0) > PENDING_S:
                d = {k: v for k, v in d.items() if k not in ("move_nonce", "move_asked")}   # (a move never allowed: forgotten)
            devices.append(d)
        _STATE.clear()
        _STATE.update({"enabled": bool(data.get("enabled")), "away_private": bool(data.get("away_private")), "devices": devices})
        _relan(_STATE)
        _FILE[0] = key
    return _STATE


def _save():
    from . import storage
    f = state_file()
    if storage.ALLOWED_ROOTS is not None and not any(storage._inside(f, r) for r in storage.ALLOWED_ROOTS):
        raise PhoneError(f"Test mode: {f} is outside the test's folders.", 500)
    fsutil.write_text(f, json.dumps(_STATE, indent=1))
    _FILE[0] = _file_key(f)   # (what's on disk is what's in memory: no read back)


def enabled() -> bool:
    """Is the phone link on? Setup switches it on; "Turn off the phone link" switches it off (then nothing reaches this PC
    from a phone, at home or away)."""
    with _LOCK:
        return _load()["enabled"]


def set_enabled(on: bool):
    global PAIRING
    with _LOCK:
        _load()["enabled"] = bool(on)
        if not on:
            PAIRING = None
        _save()


away_on, set_away = enabled, set_enabled   # (one switch since 1.2.1: the link is the phone's way in, at home and away)


def away_private() -> bool:
    """Private (Deep Cover): the link is on the tailnet only (Tailscale Serve): the phone runs Tailscale, signed in to
    the same tailnet, and nothing is on the public internet. Off (the default): Funnel's public link, no app on the phone.
    Either way, only the token lets a phone in."""
    with _LOCK:
        return _load()["away_private"]


def set_away_private(on: bool):
    with _LOCK:
        _load()["away_private"] = bool(on)
        _save()


def lan_needed() -> bool:
    """Does the home-network listener still have a job: the link on, and a phone that holds a LAN token from before the
    move? Stored in the file (`lan`), so a restart opens it only then, and never once the last LAN token is gone."""
    with _LOCK:
        return _load()["enabled"] and _load()["lan"]


def _relan(st: dict):
    st["lan"] = any(d.get("lan_hash") for d in st["devices"])


def move_code(device_id: str) -> str:
    """A single-use move code for a phone paired on the home Wi-Fi: redeemed at the link within MOVE_CODE_S, it gives that
    phone the one token. 128 random bits: nothing to guess on the open internet. Only for a device that still holds a LAN
    token (the request that asks carries it)."""
    with _LOCK:
        d = next((d for d in _load()["devices"] if d["id"] == device_id), None)
        if d is None or not d.get("lan_hash") or d.get("pending"):
            raise PhoneError("That phone isn't paired.", 404)
        now = time.time()
        for k in [k for k, (_, exp) in _MOVE_CODES.items() if exp < now]:
            _MOVE_CODES.pop(k)
        code = secrets.token_urlsafe(16)
        _MOVE_CODES[hash_token(code)] = (device_id, now + MOVE_CODE_S)
        return code


away_code = move_code


def redeem_move(code: str) -> tuple[str, dict]:
    """A move code -> (a nonce, the device): the move waits for the PC's Allow, then the phone collects the one token
    with the nonce (collect), as a new phone does. The code works once. The LAN token stays until the new one is used once
    (device_for), so a phone that never arrives at the link isn't stranded."""
    with _LOCK:
        hit = _MOVE_CODES.pop(hash_token(code or ""), None)
        if hit is None or hit[1] < time.time():
            raise PhoneError("That link has expired or was used already.", 403, generic=True)
        st = _load()
        d = next((d for d in st["devices"] if d["id"] == hit[0]), None)
        if d is None or not d.get("lan_hash"):
            raise PhoneError("That phone isn't paired any more.", 403, generic=True)
        nonce = secrets.token_urlsafe(16)
        d["move_nonce"], d["move_asked"] = hash_token(nonce), time.time()   # (a second move replaces a first one waiting)
        _save()
        return nonce, public(d)


redeem_away = redeem_move


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


_NETS: dict = {}   # the PC's address -> its home network (the prefix query takes ~0.5 s, so it's kept)
HOME_PREFIX = 24     # when the OS doesn't say: a home router's usual size, and the narrower guess is the safer one


def home_net(own: str | None = None):
    """The PC's own home network (its address and the interface's prefix): a home token is good only from inside it."""
    own = own or lan_address()
    if not own:
        return None
    if own not in _NETS:
        from . import platform
        try:
            n = platform.prefix_length(own)
        except Exception:   # noqa: BLE001 - not on this OS, or the query failed: the usual home size
            n = None
        _NETS[own] = ipaddress.ip_network(f"{own}/{n or HOME_PREFIX}", strict=False)
    return _NETS[own]


def on_home_net(addr: str, own: str | None = None) -> bool:
    try:
        net = home_net(own)
        return net is not None and ipaddress.ip_address(addr) in net
    except ValueError:
        return False


def usable(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return ip.version == 4 and ip.is_private and not ip.is_loopback and not ip.is_link_local and not ip.is_unspecified


# ---------------------------------------------------------------- pairing

def new_pairing() -> dict:
    """A fresh pairing window (any earlier one ends): the QR's 128-bit code, kept in memory only and returned here for the
    QR, never written or logged."""
    global PAIRING
    with _LOCK:
        if not _load()["enabled"]:
            raise PhoneError("Set up your phone first.")
        PAIRING = {"code": secrets.token_urlsafe(16), "until": time.time() + PAIR_SECONDS, "short": None, "wrong": 0}
        return dict(PAIRING)


def short_code() -> dict:
    """"Type a code instead": a 6-character code for the open window, SHORT_SECONDS long, SHORT_TRIES wrong tries."""
    with _LOCK:
        if not pairing():
            raise PhoneError("Show a code on the PC first.")
        PAIRING["short"] = {"code": "".join(secrets.choice(ALPHABET) for _ in range(6)), "until": time.time() + SHORT_SECONDS, "tries": 0}
        return dict(PAIRING["short"])


def cancel_pairing():
    global PAIRING
    with _LOCK:
        PAIRING = None


def pairing() -> dict | None:
    """The open window, or None. Its short code is None once it has run out or had its tries."""
    global PAIRING
    with _LOCK:
        if not PAIRING or PAIRING["until"] <= time.time():
            PAIRING = None
            return None
        s = PAIRING["short"]
        if s and (s["until"] <= time.time() or s["tries"] >= SHORT_TRIES):
            PAIRING["short"] = None
        return dict(PAIRING)


def clean_name(name: str) -> str:
    name = re.sub(r"[\x00-\x1f\x7f<>]", "", str(name or "")).strip()
    return re.sub(r"\s+", " ", name)[:40] or "Phone"


def claim(code: str, name: str, addr: str = "") -> tuple[str, dict]:
    """Pair a phone with the code the PC shows: the QR's, or the typed one -> (nonce, device). The device waits as pending
    until the PC allows it; the phone collects its token with the nonce then (collect). Wrong QR codes are counted and
    change nothing; wrong typed codes end the typed code after SHORT_TRIES. With no window open, the refusal is the
    generic one."""
    global PAIRING
    raw = str(code or "")
    with _LOCK:
        st = _load()
        win = pairing() if st["enabled"] else None
        if win is None:
            raise PhoneError("", 401, generic=True)
        short = PAIRING["short"]
        if hmac.compare_digest(raw.encode(), PAIRING["code"].encode()):
            pass
        elif short and hmac.compare_digest(re.sub(r"[^A-Z0-9]", "", raw.upper()).encode(), short["code"].encode()):
            pass
        elif short:   # (the refusals are generic on the wire: the phone's page says "that isn't the code" by the status)
            short["tries"] += 1
            if short["tries"] >= SHORT_TRIES:
                PAIRING["short"] = None
                raise PhoneError("Too many wrong codes: that typed code is finished.", 403, generic=True)
            raise PhoneError("That isn't the code on the PC.", 403, generic=True)
        else:
            PAIRING["wrong"] += 1
            raise PhoneError("That isn't the code on the PC.", 403, generic=True)
        PAIRING = None   # once
        live = [d for d in st["devices"] if not d.get("pending")]
        if len(live) >= MAX_DEVICES:
            raise PhoneError(f"{MAX_DEVICES} phones are connected already. Remove one on the PC first.", 409)
        nonce = secrets.token_urlsafe(16)
        now = time.strftime("%Y-%m-%d %H:%M")
        dev = {"id": secrets.token_hex(5), "name": clean_name(name), "hash": None, "lan_hash": None, "pending": True,
               "nonce": hash_token(nonce), "asked": time.time(), "paired": now, "seen": now, "ip": addr, "used": None,
               "lan_used": None, "version": 1}
        st["devices"].append(dev)
        _save()
        return nonce, public(dev)


def allow(device_id: str) -> dict:
    """The PC lets a phone in: a new one that claimed, or one from before that asked to move to the link. Its token is
    minted now, for the phone to collect once on its nonce."""
    with _LOCK:
        st = _load()
        d = next((d for d in st["devices"] if d["id"] == device_id and (d.get("pending") or d.get("move_nonce"))), None)
        if d is None:
            raise PhoneError("That phone isn't waiting.", 404)
        token = secrets.token_urlsafe(32)
        now = time.time()
        for k in [k for k, (_, exp) in _HANDOVER.items() if exp < now]:
            _HANDOVER.pop(k)
        if d.get("pending"):
            _HANDOVER[d["nonce"]] = (token, now + PENDING_S)
            d.update(hash=hash_token(token), pending=False, paired=time.strftime("%Y-%m-%d %H:%M"))
            d.pop("nonce", None)
        else:
            _HANDOVER[d["move_nonce"]] = (token, now + PENDING_S)
            d.update(hash=hash_token(token), used=None)   # (None: not used yet, so the LAN token stays for now)
            d.pop("move_nonce", None), d.pop("move_asked", None)
        _save()
        return public(d)


def collect(nonce: str) -> tuple[str, dict] | None:
    """The phone that claimed asks with its nonce: (the token, the device) once the PC has allowed it, handed over once;
    None for not yet, and exactly the same for a wrong, used, expired or refused nonce."""
    with _LOCK:
        hit = _HANDOVER.pop(hash_token(nonce or ""), None)
        if hit is None or hit[1] < time.time():
            return None
        h = hash_token(hit[0])
        d = next((d for d in _load()["devices"] if hmac.compare_digest(d.get("hash") or "", h)), None)
        return (hit[0], public(d)) if d is not None else None


def refuse(device_id: str) -> dict:
    """The PC says No: a new phone that claimed is forgotten (it never had a token); a move is cancelled, and the phone
    from before keeps its LAN token (it may ask again)."""
    with _LOCK:
        st = _load()
        d = next((d for d in st["devices"] if d["id"] == device_id), None)
        if d is not None and not d.get("pending") and d.get("move_nonce"):
            d.pop("move_nonce", None), d.pop("move_asked", None)
            _save()
            return public(d)
    return revoke(device_id)


def pending() -> list[dict]:
    """The phones waiting for Allow / No: new ones (asks "new") and moves (asks "move")."""
    with _LOCK:
        return [public(d) for d in _load()["devices"] if d.get("pending") or d.get("move_nonce")]


# ---------------------------------------------------------------- devices

def hash_token(token: str) -> str:
    return hashlib.sha256(str(token).encode()).hexdigest()


def public(d: dict) -> dict:
    out = {k: d.get(k) for k in ("id", "name", "paired", "seen", "ip")}
    out["pending"] = bool(d.get("pending"))
    out["lan"] = bool(d.get("lan_hash")) and not d.get("hash")   # (paired on the home Wi-Fi, not moved to the link yet)
    out["moving"] = bool(d.get("lan_hash")) and bool(d.get("hash"))   # (moved, the new token not used yet)
    out["asks"] = "new" if d.get("pending") else "move" if d.get("move_nonce") else None
    return out


def devices() -> list[dict]:
    with _LOCK:
        return [public(d) for d in _load()["devices"] if not d.get("pending")]


def device_for(token: str | None, addr: str = "", away: bool = False) -> dict | None:
    """The paired device this token belongs to (None: no token, unknown, pending, revoked, or the link is off). away:
    the one token, from the link. Else a LAN token from before the move, good only from the PC's own home network
    (addr), and only until the phone's new token has been used once."""
    if not token or (not away and (not addr or not on_home_net(addr))):
        return None
    h = hash_token(token)
    with _LOCK:
        st = _load()
        if not st["enabled"]:
            return None
        key = "hash" if away else "lan_hash"
        found = None
        for d in st["devices"]:
            if hmac.compare_digest(d.get(key) or "", h):
                found = d
        if found is None or found.get("pending"):
            return None
        now = time.time()
        used = "used" if away else "lan_used"
        if found.get(used) is not None and now - float(found[used]) > TOKEN_IDLE_S:
            return None   # unused for 30 days: stopped by itself (Remove isn't needed for a lost phone)
        found[used] = now   # (a record from before 1.1 starts its clock at its first use)
        changed = False
        if away and found.get("lan_hash"):   # the moved phone has arrived: its LAN token is finished
            found["lan_hash"], found["lan_used"] = None, None
            _relan(st)
            changed = True
        if changed or now - _SEEN_AT.get(h, 0) > SEEN_EVERY:
            _SEEN_AT[h] = now
            found["seen"], found["ip"] = time.strftime("%Y-%m-%d %H:%M"), addr or found.get("ip")
            try:
                _save()
            except (OSError, PhoneError):
                pass
        return {**public(found), "hash": found[key]}


def still_valid(token_hash: str) -> bool:
    """For long answers (the job stream): is this device still paired, allowed, and the link still on?"""
    with _LOCK:
        st = _load()
        return st["enabled"] and any(not d.get("pending") and (hmac.compare_digest(d.get("hash") or "", token_hash)
                                                                or hmac.compare_digest(d.get("lan_hash") or "", token_hash))
                                     for d in st["devices"])


def revoke(device_id: str) -> dict:
    """Remove: the device's record goes, and both its tokens with it."""
    with _LOCK:
        st = _load()
        d = next((d for d in st["devices"] if d["id"] == device_id), None)
        if d is None:
            raise PhoneError("That phone isn't paired.", 404)
        st["devices"].remove(d)
        _relan(st)
        _save()
        return public(d)


def revoke_all() -> int:
    """Remove all phones: every token stops at its next request."""
    global PAIRING
    with _LOCK:
        st = _load()
        n = len(st["devices"])
        st["devices"].clear()
        PAIRING = None
        _relan(st)
        _save()
        return n


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
# Read-only. The Doctor shows the result while a phone uses the home Wi-Fi; nothing here changes a Windows setting.

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
    addr = str(ipaddress.IPv4Address(addr))   # only a plain IPv4 address goes into the PowerShell line
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
