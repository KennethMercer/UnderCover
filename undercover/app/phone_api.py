"""Your phone, on the server (phase 11; one link since 1.2.1): the route table, the listeners, and the /api/phone routes.

ROUTES is the one list of every route and what a phone may do with it (the handoff's security table is made from it;
tests/smoke.py checks that every route the code answers is in it):
    "open"   no token needed from the network: the pairing page, its claim and its wait, the move page and its redeem,
             the home-screen manifest and icons, the service worker (no data in any of them)
    "phone"  a paired phone may use it (everything on the basic pages, the page itself included)
    "pc"     refused for phones with a plain message: Restart, Quit, storage moves and clean-up, add-on installs, the
             Doctor's Fixes and diagnostics file, Your phone itself, things that open a window on the PC, deletes, and
             the Deep Cover actions that change a voice's files
A route a phone asks for that isn't in the table is refused too (a new route is PC-only until it's listed).

Two listeners besides the PC's own (127.0.0.1): the link's, on loopback, the one target of Tailscale Funnel (or Serve,
in Private mode), where every request comes from outside and passes server._gate_away; and, only while a phone paired on
the home Wi-Fi before the link still holds its LAN token, the home-network one (server._gate). Nothing the network can
reach without a token says what exists: refusals are the generic 401 / 403.

    GET  /api/phone                  the link's phase and address, the phones (pending ones for Allow / No), the code shown (PC)
    POST /api/phone                  {on}: the link on (setup) or off ("Turn off the phone link": nothing reaches this PC) (PC)
    POST /api/phone/pair             a pairing window: the QR of https://<name>/pair#<128-bit code> (PC)
    POST /api/phone/pair/short       "Type a code instead": a 6-character code for 2 minutes (PC)
    POST /api/phone/pair/cancel
    POST /api/phone/allow | refuse   {id}: the phone that claimed may come in, or is forgotten (PC)
    POST /api/phone/revoke           {id}; /api/phone/revoke-all (PC)
    POST /api/phone/claim            {code, name} at the link -> {nonce} (no token yet)
    POST /api/phone/wait             {nonce} at the link -> {ok: false} until the PC allows; then the token, once
    GET  /pair                       the pairing page (static, generic)
    POST /api/phone/move/link        a phone from before, on the home Wi-Fi, asks for its move (a single-use code)
    POST /api/phone/move             {code} at the link -> {nonce}: the move waits for Allow, then /api/phone/wait
    GET  /move                       the page that redeems it
The link's setup (core/tailnet.py) is still driven by /api/away until the card folds it in: GET its phase; POST {on} or
{private}; /api/away/forget signs this PC out. PC only: the sign-in and approval URLs never leave the PC.
"""
import re
import threading
import time

from ..core import phone, qr
from ..core.doctor import log as doctor_log
from ..core.phone import PhoneError

_ID = r"[^/]+"
ROUTES = [
    # the shell
    ("GET", r"/|/index\.html", "phone", "the page (a token everywhere since 1.2.1)"),
    ("GET", r"/sw\.js", "open", "the phone app's service worker (no data; answered at the link only)"),
    ("GET", r"/static/(manifest\.webmanifest|icon-192\.png|icon-512\.png)", "open", "home-screen manifest and icons"),
    ("GET", r"/static/.+", "phone", "the page's scripts, styles, fonts"),
    ("GET", r"/files/.+", "phone", "playing and downloading results (audio files only for a phone)"),
    ("GET", r"/pair", "open", "the pairing page"),
    ("POST", r"/api/phone/claim", "open", "pair at the link with the code the PC shows (a nonce; the token comes on Allow)"),
    ("GET", r"/api/status|/api/voices|/api/search|/api/jobs|/api/work", "phone", "status, voices, jobs, the queue"),
    ("GET", rf"/api/jobs/{_ID}/events", "phone", "a job's live progress"),
    ("POST", rf"/api/jobs/{_ID}/(cancel|retry)", "phone", "Cancel / Retry"),
    ("POST", r"/api/work/(pause|resume|forget|add|remove|order|start|settings|dismiss|to-queue|estimate)", "phone", "Pause / Resume and the queue"),
    ("POST", r"/api/voices/(install|upload)", "phone", "get a voice"),
    ("DELETE", rf"/api/voices/{_ID}", "phone", "remove a voice (set aside; Bring it back undoes it)"),
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
    ("GET|POST", rf"/api/history/{_ID}/edits(/(open|undo|redo|keep|price))?", "pc", "a cover's edit list: the words editor is the PC's"),
    ("POST", rf"/api/songs/{_ID}/presep", "phone", "prepare a song"),
    # DoubleAgent
    ("GET", r"/api/work/candidates|/api/work/lookup", "phone", "voices to improve tonight"),
    ("GET|POST", r"/api/train/projects", "phone", "training projects"),
    ("GET", rf"/api/train/projects/{_ID}", "phone", "a project"),
    ("DELETE", rf"/api/train/projects/{_ID}", "pc", "delete a training project (the PC's: nothing brings it back)"),
    ("POST", rf"/api/train/projects/{_ID}/(search|sources|upload|prepare|reference|phrases|train|pause|stop|export|auto|transcribe)",
     "phone", "the training steps"),
    ("GET", rf"/api/train/voices/{_ID}/cadence(/sample/(current|pending))?", "phone", "Keep this one: the two readings"),
    ("POST", rf"/api/train/voices/{_ID}/cadence/(ab|verdict)", "phone", "Keep this one: listen and choose"),
    ("GET", rf"/api/train/voices/{_ID}/improved", "phone", "an improved voice waiting: the two readings"),
    ("POST", rf"/api/train/voices/{_ID}/improved/(ab|keep|undo)", "phone", "an improved voice: listen, keep, undo"),
    # SpeakEasy
    ("GET", r"/api/speak/(voices|readers|recent|plan|estimate)", "phone", "speech page data"),
    ("POST", r"/api/speak/estimate", "phone", "how long a speech would take"),
    ("POST", r"/api/speak/warm", "phone", "SpeakEasy opened: its speech engine warmed while idle"),
    ("POST", r"/api/speak|/api/speak/(cadence|newvoice)", "phone", "make speech"),
    ("POST", rf"/api/speak/{_ID}/(rename|keep|unkeep)", "phone", "rename / keep speech"),
    ("DELETE", rf"/api/speak/{_ID}", "pc", "remove speech (the PC's: nothing brings it back)"),
    # SafeHouse
    ("GET", r"/api/library/(summary|covers|speech|songs|voices|training)", "phone", "the library"),
    ("GET", r"/api/library/covers/zip", "phone", "download covers"),
    ("POST", rf"/api/library/(covers|speech)/{_ID}/(rename|keep|unkeep)", "phone", "rename / keep"),
    ("DELETE", rf"/api/library/(covers|speech|songs)/{_ID}", "pc", "delete a cover, a speech or a song (the PC's: nothing brings it back)"),
    ("POST", rf"/api/library/voices/{_ID}/display", "phone", "a voice's display name"),
    ("POST", rf"/api/library/voices/{_ID}/rename", "pc", "Rename folder (Deep Cover)"),
    ("GET", rf"/api/library/voices/{_ID}/export", "pc", "Export .zip (Deep Cover)"),
    ("GET", rf"/api/library/voices/{_ID}/pack", "pc", "a voice pack's export form"),
    ("POST", rf"/api/library/voices/{_ID}/pack", "pc", "Export as a voice pack (.ucvoice)"),
    ("DELETE", rf"/api/library/voices/{_ID}/cadence", "pc", "Delete cadence (Deep Cover)"),
    ("GET|POST", r"/api/library/cleanup", "pc", "Storage: Clean up"),
    ("GET", r"/api/library/aside", "pc", "Storage: voices set aside"),
    ("DELETE", r"/api/library/aside/[a-z0-9-]+", "pc", "delete a set-aside voice (the page asks twice)"),
    ("POST", r"/api/library/aside/[a-z0-9-]+/restore", "phone", "bring a removed voice back"),
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
    ("POST", r"/api/lowprofile", "phone", "Low profile from the sofa: half the card, in bursts"),
    ("POST", r"/api/firstrun", "phone", "the First assignment's tour: its step, skip, done, replay"),
    ("POST", r"/api/vault/check", "pc", "the vault's folder check"),
    ("GET", r"/api/vault/contents", "pc", "what the vault holds"),
    ("POST", r"/api/vault/settings|/api/vault/run|/api/vault/restore", "pc", "the vault: its folder, a run, a restore"),
    # Themes (phase 14): a phone may read and use them; making, importing and deleting are done on the PC
    ("GET", r"/api/themes", "phone", "the theme list"),
    ("GET", rf"/api/themes/{_ID}/(export|theme\.css|preview\.png|fonts/[^/]+)", "phone", "a theme's css, fonts, preview, file"),
    ("POST", r"/api/themes|/api/themes/import", "pc", "saving or importing a theme"),
    ("DELETE", rf"/api/themes/{_ID}", "pc", "deleting a theme"),
    # Your phone itself
    ("GET|POST", r"/api/phone", "pc", "Your phone: its state; the link on or off"),
    ("POST", r"/api/phone/(pair|pair/cancel|pair/short|allow|refuse|revoke|revoke-all)", "pc", "pairing, Allow / No, Remove"),
    ("POST", r"/api/phone/wait", "open", "the phone that claimed asks for its token with its nonce (at the link only)"),
    ("GET|POST", r"/api/away", "pc", "the link's setup: its phase, sign-in and approval (the sign-in URL never leaves the PC)"),
    ("POST", r"/api/away/forget", "pc", "sign this PC out of Tailscale"),
    ("POST", r"/api/phone/(move|away)/link", "phone", "a phone paired on the home Wi-Fi before asks for its move to the link"),
    ("POST", r"/api/phone/(move|away)", "open", "redeem the move code at the link (the code is the credential)"),
    ("GET", r"/move|/away", "open", "the page that redeems the move code"),
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
    """Called once at startup. make_server(addr, port): the home-network listener, opened only while a phone paired
    before the link still holds its LAN token (phone.lan_needed) and checked every minute then: opened when the PC gets a
    home-network address after UnderCover started (Wi-Fi comes up after login), moved when the router hands out a new one,
    closed for good when the last such phone has moved. make_away: the link's listener, opened while the link is on."""
    _L["make"], _L["port"] = make_server, port
    _A["make"] = make_away
    if phone.lan_needed():
        listen()
    if phone.enabled():
        try:
            away_open()
        except Exception as e:   # noqa: BLE001 - never block startup; the card shows the problem
            _A["problem"] = str(e)

    def keep():
        while True:
            try:
                tidy_lan()
            except Exception:   # noqa: BLE001 - try again next minute
                pass
            time.sleep(60)
    threading.Thread(target=keep, daemon=True, name="phone-keep").start()


def tidy_lan():
    """The home-network listener as the phones need it: open (and warm) while a phone from before holds its LAN token,
    closed for good once none does."""
    if phone.lan_needed():
        listen()
        warm()
    elif _L["srv"] is not None:
        close()
        doctor_log("Your phone: the home-network listener closed; every phone uses the link now.")


def warm():
    """Read the network type and the firewall now (~1 s, cached), so the Doctor's fast check finds them ready."""
    from ..core import doctor
    if _L["addr"]:
        phone.home_net(_L["addr"])   # (its prefix read ahead, so a phone's first request doesn't wait for it)
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
    tailnet.start(_A["port"], private=phone.away_private())


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
            "phone_on": phone.enabled(), "private": phone.away_private()}


def state() -> dict:
    """Your phone's state for the PC's card: the link (its phase and address, core/tailnet.py), the phones (the pending
    ones apart, for Allow / No), the pairing window (the QR of the link's pair page with the 128-bit code; the typed code
    only when asked for; how many wrong claims it has had), and the home-network listener while a phone from before
    still needs it. The keys the card read before 1.2.1 (listening, url, problem) are kept for it."""
    from ..core import tailnet
    on = phone.enabled()
    snap = tailnet.snapshot()
    lan = None
    if on and phone.lan_needed():
        listen()   # the PC's address may have changed since (a new Wi-Fi, the router's DHCP)
        lan = {"listening": _L["srv"] is not None, "url": url(), "problem": _L["problem"]}
    elif _L["srv"] is not None:
        tidy_lan()
    link = snap["link"] if on else None
    p = phone.pairing()
    out = {"enabled": on, "link": link, "phase": snap["phase"] if on else "off", "lan": lan, "devices": phone.devices(),
           "pending": phone.pending(), "pairing": None,
           "listening": bool(link) or bool(lan and lan["listening"]), "url": link or (lan and lan["url"]), "problem": lan and lan["problem"]}
    if p and link:
        plink = f"{link}/pair#{p['code']}"
        if _QR.get("link") != plink:   # the card asks every few seconds while a code is shown
            _QR.update(link=plink, svg=qr.svg(plink))
        s = p["short"]
        out["pairing"] = {"until": p["until"], "url": f"{link}/pair", "link": plink, "qr": _QR["svg"], "wrong": p["wrong"],
                          "short": {"code": s["code"], "until": s["until"]} if s else None, "code": s["code"] if s else ""}
    return out


# ---------------------------------------------------------------- routes

def handle(h, method: str, p: str) -> bool:
    if p in ("/api/away", "/api/away/forget"):
        try:
            _away_route(h, method, p)
        except PhoneError as e:
            _refuse(h, e)
        return True
    if p != "/api/phone" and not p.startswith("/api/phone/"):
        return False
    try:
        _route(h, method, p)
    except PhoneError as e:
        _refuse(h, e)
    return True


def _refuse(h, e: PhoneError):
    """A PhoneError as the listener answers it: the generic refusal where nothing may be said (the link, before a
    token), else the plain sentence."""
    if e.generic:
        from .server import AWAY_401, AWAY_403
        h._deny(e.status, (AWAY_401 if e.status == 401 else AWAY_403) if getattr(h, "away", False) else None)
    else:
        h._error(str(e), e.status)


def _away_route(h, method, p):
    if h.remote:
        raise PhoneError("Away from home is managed on the PC.", 403)
    from ..core import tailnet
    if p == "/api/away" and method == "GET":
        return h._json(away_state())
    if method != "POST":
        raise LookupError("Not found")
    b = h._body()
    if p == "/api/away" and "private" in b and "on" not in b:   # Deep Cover: "Private: my phone runs Tailscale"
        phone.set_away_private(bool(b["private"]))
        if tailnet.STATE["want"]:
            tailnet.set_private(bool(b["private"]))
        return h._json(away_state())
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
    if p == "/api/phone/wait" and method == "POST":   # the phone that claimed, at the link: its token once the PC allows it
        if not getattr(h, "away", False):
            raise PhoneError("", 401, generic=True)
        nonce = h._body().get("nonce")
        if not isinstance(nonce, str) or not nonce:
            raise PhoneError("", 401, generic=True)
        got = phone.collect(nonce)
        if got is None:
            return h._json({"ok": False})   # (not yet; the same bytes for a wrong, used, expired or refused nonce)
        token, dev = got
        h.set_cookie = main_cookie(token)
        return h._json({"ok": True, "device": dev})
    if p in ("/api/phone/move/link", "/api/phone/away/link") and method == "POST":   # a phone from before, on the home Wi-Fi
        from ..core import tailnet
        if not h.remote or getattr(h, "away", False) or h.device is None:
            raise PhoneError("Ask for the move from the phone itself, on the home Wi-Fi.", 400)
        snap = tailnet.snapshot()
        if not snap["link"]:
            raise PhoneError("The PC hasn't finished setting up your phone's link yet.", 409)
        h._body()
        return h._json({"url": f"{snap['link']}/move#{phone.move_code(h.device['id'])}"})   # (the node's name, never this request's Host)
    if p in ("/api/phone/move", "/api/phone/away") and method == "POST":   # at the link: the code -> a nonce, then Allow
        if not getattr(h, "away", False):
            raise PhoneError("", 401, generic=True)
        nonce, dev = phone.redeem_move(str(h._body().get("code") or ""))
        doctor_log(f"Your phone: {dev['name']} asked to move to the link.")
        return h._json({"ok": True, "nonce": nonce, "device": {"name": dev["name"]}})
    if h.remote:   # the gate refuses these for phones already; never let a phone change Your phone
        raise PhoneError("Your phone is managed on the PC.", 403)
    if p == "/api/phone" and method == "GET":
        return h._json(state())
    if method != "POST":
        raise LookupError("Not found")
    b = h._body()
    if p == "/api/phone":
        on = bool(b.get("on"))
        phone.set_enabled(on)
        if on:
            try:
                away_open()
            except Exception as e:   # noqa: BLE001 - the card shows the problem
                _A["problem"] = str(e)
            if phone.lan_needed():
                listen()
                threading.Thread(target=warm, daemon=True, name="phone-warm").start()
            doctor_log("Your phone: the link is on.")
        else:
            close()
            away_close()   # (funnel reset and down: nothing reaches this PC from a phone, at home or away)
            doctor_log("Your phone: the link is off; nothing reaches this PC from a phone.")
        return h._json(state())
    if p == "/api/phone/pair":
        if not phone.enabled():
            raise PhoneError("Set up your phone first.", 409)
        phone.new_pairing()
        return h._json(state())
    if p == "/api/phone/pair/short":
        phone.short_code()
        return h._json(state())
    if p == "/api/phone/pair/cancel":
        phone.cancel_pairing()
        return h._json(state())
    if p == "/api/phone/allow":
        d = phone.allow(str(b.get("id") or ""))
        doctor_log(f"Your phone: {d['name']} was allowed" + (" to move to the link." if d["moving"] else "."))
        return h._json(state())
    if p == "/api/phone/refuse":
        d = phone.refuse(str(b.get("id") or ""))
        doctor_log(f"Your phone: {d['name']} was refused.")
        return h._json(state())
    if p == "/api/phone/revoke":
        d = phone.revoke(str(b.get("id") or ""))
        doctor_log(f"Your phone: {d['name']} was removed.")
        return h._json(state())
    if p == "/api/phone/revoke-all":
        n = phone.revoke_all()
        doctor_log(f"Your phone: all phones removed ({n}).")
        return h._json(state())
    raise LookupError("Not found")


def home_cookie(token: str) -> str:
    """A LAN token (a phone from before the link) as a cookie: it lasts TOKEN_IDLE_S, and every answer to that phone
    sends it again, so its life runs from the last use (1.1)."""
    return f"{phone.LAN_COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={phone.TOKEN_IDLE_S}"


def main_cookie(token: str) -> str:
    """The one token as a cookie, at the link: a __Host- name, so the browser takes it only Secure, with Path=/ and no
    Domain; HttpOnly; SameSite=Strict; the same life as above."""
    return f"{phone.MAIN_COOKIE}={token}; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age={phone.TOKEN_IDLE_S}"


away_cookie = main_cookie


def _claim(h):
    """A claim at the link: the code the PC shows, and a name -> {ok, nonce, device}. No token yet: the phone waits for
    the PC's Allow (/api/phone/wait). Nowhere else, and nothing of it is logged (the code is a secret for its five
    minutes). The address is Funnel's loopback: no forwarded header is read for anything."""
    if not getattr(h, "away", False):
        raise PhoneError("", 401, generic=True)
    b = h._body()
    nonce, dev = phone.claim(b.get("code"), b.get("name") or phone.name_from_agent(h.headers.get("User-Agent")), "")
    h._json({"ok": True, "nonce": nonce, "device": dev})
