"""Phone access on the server (phase 11): the route table, the second listener, and the /api/phone routes.

ROUTES is the one list of every route and what a paired phone may do with it (the handoff's security table is made from
it; tools/smoke.py checks that every route the code answers is in it):
    "open"   no token needed from the network: the pairing page and its claim, the home-screen manifest and its icons
    "phone"  a paired phone may use it (everything on the basic pages)
    "pc"     refused for phones with a plain message: Restart, Quit, storage moves and clean-up, add-on installs, the
             Doctor's Fixes and diagnostics file, Phone access itself, things that open a window on the PC, and the
             Deep Cover actions that change a voice's files
A route a phone asks for that isn't in the table is refused too (a new route is PC-only until it's listed).

    GET  /api/phone                  the switch, the address, the paired phones, the code being shown (PC only)
    POST /api/phone                  {on} switch it on or off (the listener opens or closes at once)
    POST /api/phone/pair             a new code -> {code, url, until, qr (SVG)}
    POST /api/phone/pair/cancel
    POST /api/phone/revoke           {id}
    POST /api/phone/claim            {code, name} from the phone -> sets its token cookie (no token needed)
    GET  /pair                       the pairing page (no token needed)
"""
import re
import threading

from ..core import phone, qr
from ..core.phone import PhoneError

_ID = r"[^/]+"
ROUTES = [
    # the shell
    ("GET", r"/|/index\.html", "phone", "the page"),
    ("GET", r"/static/(manifest\.webmanifest|icon-192\.png|icon-512\.png)", "open", "home-screen manifest and icons"),
    ("GET", r"/static/.+", "phone", "the page's scripts, styles, fonts"),
    ("GET", r"/files/.+", "phone", "playing and downloading results"),
    ("GET", r"/pair", "open", "the pairing page"),
    ("POST", r"/api/phone/claim", "open", "pair with the code from the PC (rate-limited)"),
    ("GET", r"/api/status|/api/voices|/api/search|/api/jobs|/api/work", "phone", "status, voices, jobs, the queue"),
    ("GET", rf"/api/jobs/{_ID}/events", "phone", "a job's live progress"),
    ("POST", rf"/api/jobs/{_ID}/(cancel|retry)", "phone", "Cancel / Retry"),
    ("POST", r"/api/work/(pause|resume|forget|add|remove|order|start|settings|dismiss)", "phone", "Pause / Resume and the queue"),
    ("POST", r"/api/voices/(install|upload)", "phone", "get a voice"),
    ("DELETE", rf"/api/voices/{_ID}", "phone", "remove a voice (the page asks twice)"),
    ("POST", r"/api/open-folder", "pc", "opens Explorer on the PC"),
    ("POST", r"/api/restart", "pc", "Restart"),
    ("POST", r"/api/quit", "pc", "Quit"),
    # FrontMan
    ("GET", r"/api/config|/api/history|/api/songs/search|/api/auto/plan", "phone", "covers page data, song search, one-shot plan"),
    ("GET", rf"/api/songs/{_ID}(/key)?", "phone", "a song"),
    ("POST", r"/api/upload|/api/estimate|/api/covers|/api/preview|/api/songs/youtube|/api/auto", "phone", "make covers"),
    ("POST", rf"/api/songs/{_ID}/presep", "phone", "prepare a song"),
    # DoubleAgent
    ("GET", r"/api/work/candidates|/api/work/lookup", "phone", "voices to improve tonight"),
    ("GET|POST", r"/api/train/projects", "phone", "training projects"),
    ("GET|DELETE", rf"/api/train/projects/{_ID}", "phone", "a project (delete: the page asks twice)"),
    ("POST", rf"/api/train/projects/{_ID}/(search|sources|upload|prepare|reference|phrases|train|pause|stop|export|auto|transcribe)",
     "phone", "the training steps"),
    ("GET", rf"/api/train/voices/{_ID}/cadence(/sample/(current|pending))?", "phone", "Keep this one: the two readings"),
    ("POST", rf"/api/train/voices/{_ID}/cadence/(ab|verdict)", "phone", "Keep this one: listen and choose"),
    # SpeakEasy
    ("GET", r"/api/speak/(voices|readers|recent|plan)", "phone", "speech page data"),
    ("POST", r"/api/speak|/api/speak/(cadence|newvoice)", "phone", "make speech"),
    ("POST", rf"/api/speak/{_ID}/(rename|keep|unkeep)", "phone", "rename / keep speech"),
    ("DELETE", rf"/api/speak/{_ID}", "phone", "remove speech (the page asks twice)"),
    # SafeHouse
    ("GET", r"/api/library/(summary|covers|speech|songs|voices|training)", "phone", "the library"),
    ("GET", r"/api/library/covers/zip", "phone", "download covers"),
    ("POST", rf"/api/library/(covers|speech)/{_ID}/(rename|keep|unkeep)", "phone", "rename / keep"),
    ("DELETE", rf"/api/library/(covers|speech|songs)/{_ID}", "phone", "delete (the page asks twice)"),
    ("POST", rf"/api/library/voices/{_ID}/display", "phone", "a voice's display name"),
    ("POST", rf"/api/library/voices/{_ID}/rename", "pc", "Rename folder (Deep Cover)"),
    ("GET", rf"/api/library/voices/{_ID}/export", "pc", "Export .zip (Deep Cover)"),
    ("DELETE", rf"/api/library/voices/{_ID}/cadence", "pc", "Delete cadence (Deep Cover)"),
    ("GET|POST", r"/api/library/cleanup", "pc", "Storage: Clean up"),
    ("POST", r"/api/library/tools/ytdlp-update", "pc", "an install (yt-dlp Update)"),
    ("GET", r"/api/addons|/api/storage", "phone", "the Add-ons and Storage rooms (reading)"),
    ("POST", rf"/api/addons/{_ID}/(install|remove|use)", "pc", "add-on installs"),
    ("POST", r"/api/storage/(check|move)", "pc", "storage moves"),
    # the Doctor
    ("GET", r"/api/doctor", "phone", "the System room's checks"),
    ("POST", r"/api/doctor/full", "phone", "Check everything"),
    ("POST", r"/api/doctor/fix/.+", "pc", "the Doctor's Fixes"),
    ("GET", r"/api/doctor/bundle|/api/doctor/bundle\.zip", "pc", "the diagnostics file"),
    ("POST", r"/api/doctor/open-logs", "pc", "opens the logs folder on the PC"),
    # Phone access itself
    ("GET|POST", r"/api/phone", "pc", "Phone access settings"),
    ("POST", r"/api/phone/(pair|pair/cancel|revoke)", "pc", "pairing and Revoke"),
]
_COMPILED = [(set(m.split("|")), re.compile(rx), pol, what) for m, rx, pol, what in ROUTES]


def policy(method: str, p: str) -> tuple[str, str]:
    """(policy, what) for a request from the network; ("pc", ...) for a route the table doesn't list."""
    for methods, rx, pol, what in _COMPILED:
        if method in methods and rx.fullmatch(p):
            return pol, what
    return "pc", "not in the phone's list"


# ---------------------------------------------------------------- the second listener

_L = {"srv": None, "addr": None, "problem": None, "make": None, "port": None}
_LLOCK = threading.Lock()


def setup(make_server, port: int):
    """Called once at startup: make_server(addr, port) returns a server whose requests are all from the network."""
    _L["make"], _L["port"] = make_server, port
    if phone.enabled():
        listen()


def listen() -> bool:
    """Open the phone listener on the home-network address (again, if that address changed)."""
    with _LLOCK:
        addr = phone.lan_address()
        srv = _L["srv"]
        if srv is not None and _L["addr"] == addr:
            return True
        _close()
        if _L["make"] is None:
            _L["problem"] = "The server isn't running."
            return False
        if not addr:
            _L["problem"] = "This PC isn't on a home network (no private address found): connect it to your Wi-Fi or router."
            return False
        try:
            srv = _L["make"](addr, _L["port"])
        except OSError as e:
            _L["problem"] = f"Couldn't listen on {addr}:{_L['port']} ({e.strerror or e})."
            return False
        _L["srv"], _L["addr"], _L["problem"] = srv, addr, None
        threading.Thread(target=srv.serve_forever, daemon=True, name="phone-listener").start()
        return True


def _close():
    srv, _L["srv"], _L["addr"] = _L["srv"], None, None
    if srv is not None:
        srv.shutdown()
        srv.server_close()


def close():
    with _LLOCK:
        _close()
        _L["problem"] = None


def url() -> str | None:
    return f"http://{_L['addr']}:{_L['port']}" if _L["srv"] is not None else None


def state() -> dict:
    on = phone.enabled()
    if on:
        listen()   # the PC's address may have changed since (a new Wi-Fi, the router's DHCP)
    p = phone.pairing()
    out = {"enabled": on, "listening": _L["srv"] is not None, "url": url(), "problem": _L["problem"] if on else None,
           "devices": phone.devices(), "pairing": None}
    if p and url():
        link = f"{url()}/pair#{p['code']}"
        out["pairing"] = {"code": p["code"], "until": p["until"], "url": f"{url()}/pair", "link": link, "qr": qr.svg(link)}
    return out


# ---------------------------------------------------------------- routes

def handle(h, method: str, p: str) -> bool:
    if p != "/api/phone" and not p.startswith("/api/phone/"):
        return False
    try:
        _route(h, method, p)
    except PhoneError as e:
        h._error(str(e), e.status)
    return True


def _route(h, method, p):
    if p == "/api/phone/claim" and method == "POST":
        return _claim(h)
    if h.remote:   # the gate refuses these for phones already; never let a phone change Phone access
        raise PhoneError("Phone access is managed on the PC.", 403)
    if p == "/api/phone" and method == "GET":
        return h._json(state())
    if method != "POST":
        raise LookupError("Not found")
    b = h._body()
    if p == "/api/phone":
        phone.set_enabled(bool(b.get("on")))
        if b.get("on"):
            listen()
        else:
            close()
        return h._json(state())
    if p == "/api/phone/pair":
        if not listen():
            raise PhoneError(_L["problem"] or "Phone access isn't listening.", 409)
        phone.new_pairing()
        return h._json(state())
    if p == "/api/phone/pair/cancel":
        phone.cancel_pairing()
        return h._json(state())
    if p == "/api/phone/revoke":
        phone.revoke(str(b.get("id") or ""))
        return h._json(state())
    raise LookupError("Not found")


def _claim(h):
    b = h._body()
    if not h.remote:
        raise PhoneError("Pair from the phone: open the address shown on the PC there.", 400)
    token, dev = phone.claim(b.get("code"), b.get("name") or phone.name_from_agent(h.headers.get("User-Agent")),
                             h.client_address[0])
    h.set_cookie = f"{phone.COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000"
    h._json({"ok": True, "device": dev})
