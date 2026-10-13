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
Away from home (phase 20; core/tailnet.py):
    GET  /api/away                   the switch and where it's got to (fetching / sign-in / approve / ready + link) (PC)
    POST /api/away                   {on} switch it on or off (PC)
    POST /api/away/forget            sign this PC out of Tailscale (PC)
    POST /api/phone/away/link        a paired phone on the home Wi-Fi asks for its away link (a single-use code)
    POST /api/phone/away             {code} at the away address: that phone's away token (cookie uc_away, Secure)
    GET  /away                       the page that redeems the code (no token needed)
"""
import re
import threading
import time

from ..core import phone, qr
from ..core.phone import PhoneError

_ID = r"[^/]+"
ROUTES = [
    # the shell
    ("GET", r"/|/index\.html", "phone", "the page"),
    ("GET", r"/static/(manifest\.webmanifest|icon-192\.png|icon-512\.png)", "open", "home-screen manifest and icons"),
    ("GET", r"/static/.+", "phone", "the page's scripts, styles, fonts"),
    ("GET", r"/files/.+", "phone", "playing and downloading results (audio files only for a phone)"),
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
    # the ear (thumbs on covers and speech)
    ("GET", r"/api/ear", "phone", "your thumbs"),
    ("POST", r"/api/ear/(rate|forget)", "phone", "a thumb, or forgetting thumbs"),
    # FrontMan
    ("GET", r"/api/config|/api/history|/api/songs/search|/api/auto/plan", "phone", "covers page data, song search, one-shot plan"),
    ("GET", rf"/api/songs/{_ID}(/key)?", "phone", "a song"),
    ("POST", r"/api/upload|/api/estimate|/api/covers|/api/preview|/api/songs/youtube|/api/auto", "phone", "make covers"),
    ("POST", rf"/api/history/{_ID}/again", "phone", "make a cover again (the ear's fix)"),
    ("POST", rf"/api/history/{_ID}/octaves", "phone", "two octaves of a cover, for a quick blind pick"),
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
    ("GET", r"/api/library/smaller", "pc", "Make smaller: what each voice and project would save"),
    ("POST", r"/api/library/smaller/(start|keep|undo)", "pc", "Make smaller, Keep, Undo"),
    ("POST", rf"/api/library/voices/{_ID}/full-index", "pc", "a new voice's full index back (Deep Cover)"),
    ("POST", r"/api/library/tools/ytdlp-(update|auto)", "pc", "the YouTube downloader's update and its switch"),
    ("GET", r"/api/addons|/api/storage", "phone", "the Add-ons and Storage rooms (reading)"),
    ("POST", rf"/api/addons/{_ID}/(install|remove|use)", "pc", "add-on installs"),
    ("POST", r"/api/storage/(check|move)", "pc", "storage moves"),
    # the Doctor
    ("GET", r"/api/doctor", "phone", "the System room's checks"),
    ("POST", r"/api/doctor/full", "phone", "Check everything"),
    ("POST", r"/api/doctor/fix/.+", "pc", "the Doctor's Fixes"),
    ("GET", r"/api/doctor/bundle|/api/doctor/bundle\.zip", "pc", "the diagnostics file"),
    ("POST", r"/api/doctor/open-logs", "pc", "opens the logs folder on the PC"),
    ("GET", r"/api/vault", "phone", "the vault's state"),
    ("GET", r"/api/gamepause", "phone", "the game pause's question"),
    ("POST", r"/api/gamepause", "phone", "answer the game pause from the sofa"),
    ("POST", r"/api/gamepause/settings", "pc", "the game pause's settings"),
    ("GET", r"/api/vault/check|/api/vault/contents", "pc", "the vault's folder check and what it holds"),
    ("POST", r"/api/vault/settings|/api/vault/run|/api/vault/restore", "pc", "the vault: its folder, a run, a restore"),
    # Themes (phase 14): a phone may read and use them; making, importing and deleting are done on the PC
    ("GET", r"/api/themes", "phone", "the theme list"),
    ("GET", rf"/api/themes/{_ID}/(export|theme\.css|preview\.png|fonts/[^/]+)", "phone", "a theme's css, fonts, preview, file"),
    ("POST", r"/api/themes|/api/themes/import", "pc", "saving or importing a theme"),
    ("DELETE", rf"/api/themes/{_ID}", "pc", "deleting a theme"),
    # the First assignment (the page side; its routes come with the tutorial backend): works at phone width too
    ("POST", r"/api/tutorial/(done|reset)", "phone", "the First assignment's done mark and Replay"),
    ("POST", r"/api/tutorial/demo/(update|undo)", "phone", "a newer Agent Smoke: Update, and back"),
    ("GET", r"/api/tutorial/greetings(/[^/]+\.mp3)?", "phone", "Agent Smoke's greeting clips"),
    # Phone access itself
    ("GET|POST", r"/api/phone", "pc", "Phone access settings"),
    ("POST", r"/api/phone/(pair|pair/cancel|revoke)", "pc", "pairing and Revoke"),
    ("GET|POST", r"/api/away", "pc", "away from home: its switch and its sign-in"),
    ("POST", r"/api/away/forget", "pc", "sign this PC out of Tailscale"),
    ("POST", r"/api/phone/away/link", "phone", "a paired phone asks for its away link (on the home Wi-Fi)"),
    ("POST", r"/api/phone/away", "phone", "redeem the away link (allowed only at the away address, before a token)"),
    ("GET", r"/away", "open", "the page that redeems the away link"),
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
_QR: dict = {}


def setup(make_server, port: int, make_away=None):
    """Called once at startup: make_server(addr, port) returns a server whose requests are all from the network.
    While Phone access is on, the listener is checked every minute: opened when the PC gets a home-network address after
    UnderCover started (Wi-Fi comes up after login), moved when the router hands out a new one. make_away: the away
    listener (phase 20), opened when away from home is on."""
    _L["make"], _L["port"] = make_server, port
    _A["make"] = make_away
    if phone.enabled():
        listen()
    if phone.enabled() and phone.away_on():
        try:
            away_open()
        except Exception as e:   # noqa: BLE001 - never block startup; the room shows the problem
            _A["problem"] = str(e)

    def keep():
        while True:
            try:
                if phone.enabled():
                    listen()
                    warm()
            except Exception:   # noqa: BLE001 - try again next minute
                pass
            time.sleep(60)
    threading.Thread(target=keep, daemon=True, name="phone-keep").start()


def warm():
    """Read the network type and the firewall now (~1 s, cached), so the Doctor's fast check finds them ready."""
    from ..core import doctor
    if _L["addr"] and phone.usable(_L["addr"]):
        phone.windows_check(_L["addr"], _L["port"], doctor.phone_programs())   # reads again only when older than WIN_KEEP


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


# ---------------------------------------------------------------- away from home (phase 20)

_A = {"srv": None, "port": None, "make": None, "problem": None}
AWAY_PORTS = range(7870, 7880)


def away_open():
    """The away listener on loopback (the first free port of AWAY_PORTS), then Tailscale toward it."""
    from ..core import tailnet
    with _LLOCK:
        if _A["srv"] is None:
            if _A["make"] is None:
                raise PhoneError("The server isn't running.", 409)
            for p in AWAY_PORTS:
                try:
                    _A["srv"] = _A["make"]("127.0.0.1", p)
                    _A["port"] = p
                    break
                except OSError:
                    continue
            if _A["srv"] is None:
                raise PhoneError("No free port for away from home (7870-7879).", 409)
            threading.Thread(target=_A["srv"].serve_forever, daemon=True, name="away-listener").start()
    tailnet.start(_A["port"])


def away_close():
    from ..core import tailnet
    tailnet.stop()
    with _LLOCK:
        srv, _A["srv"], _A["port"] = _A["srv"], None, None
        if srv is not None:
            srv.shutdown()
            srv.server_close()


def away_state() -> dict:
    from ..core import tailnet
    return {**tailnet.snapshot(), "listening": _A["srv"] is not None, "port": _A["port"], "problem": _A["problem"],
            "phone_on": phone.enabled()}


def state() -> dict:
    on = phone.enabled()
    if on:
        listen()   # the PC's address may have changed since (a new Wi-Fi, the router's DHCP)
    p = phone.pairing()
    out = {"enabled": on, "listening": _L["srv"] is not None, "url": url(), "problem": _L["problem"] if on else None,
           "devices": phone.devices(), "pairing": None}
    if p and url():
        link = f"{url()}/pair#{p['code']}"
        if _QR.get("link") != link:   # the room asks every few seconds while a code is shown
            _QR.update(link=link, svg=qr.svg(link))
        out["pairing"] = {"code": p["code"], "until": p["until"], "url": f"{url()}/pair", "link": link, "qr": _QR["svg"]}
    return out


# ---------------------------------------------------------------- routes

def handle(h, method: str, p: str) -> bool:
    if p in ("/api/away", "/api/away/forget"):
        try:
            _away_route(h, method, p)
        except PhoneError as e:
            h._error(str(e), e.status)
        return True
    if p != "/api/phone" and not p.startswith("/api/phone/"):
        return False
    try:
        _route(h, method, p)
    except PhoneError as e:
        h._error(str(e), e.status)
    return True


def _away_route(h, method, p):
    if h.remote:
        raise PhoneError("Away from home is managed on the PC.", 403)
    from ..core import tailnet
    if p == "/api/away" and method == "GET":
        return h._json(away_state())
    if method != "POST":
        raise LookupError("Not found")
    b = h._body()
    if p == "/api/away":
        if b.get("on"):
            if not phone.enabled():
                raise PhoneError("Switch Phone access on first: away from home carries a phone you've paired at home.", 409)
            phone.set_away(True)
            away_open()
        else:
            phone.set_away(False)
            away_close()
        return h._json(away_state())
    if p == "/api/away/forget":
        phone.set_away(False)
        tailnet.forget()   # first: the logout needs the daemon running
        away_close()
        return h._json(away_state())
    raise LookupError("Not found")


def _route(h, method, p):
    if p == "/api/phone/claim" and method == "POST":
        return _claim(h)
    if p == "/api/phone/away/link" and method == "POST":   # a paired phone, on the home Wi-Fi
        from ..core import tailnet
        if not h.remote or getattr(h, "away", False) or h.device is None:
            raise PhoneError("Ask for the away link from your paired phone, on the home Wi-Fi.", 400)
        snap = tailnet.snapshot()
        if not phone.away_on() or not snap["link"]:
            raise PhoneError("Away from home isn't ready on the PC yet (SafeHouse, Settings, Phone).", 409)
        h._body()
        return h._json({"url": f"{snap['link']}/away#{phone.away_code(h.device['id'])}"})
    if p == "/api/phone/away" and method == "POST":   # at the away address: the code -> this phone's away token
        if not getattr(h, "away", False):
            raise PhoneError("Open the away link on the phone itself.", 400)
        token, dev = phone.redeem_away(str(h._body().get("code") or ""))
        h.set_cookie = f"{phone.AWAY_COOKIE}={token}; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=31536000"
        return h._json({"ok": True, "device": dev})
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
            threading.Thread(target=warm, daemon=True, name="phone-warm").start()
        else:
            close()
            if phone.away_on() or _A["srv"] is not None:   # off means nothing reaches this PC from a phone: away too
                phone.set_away(False)
                away_close()
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
