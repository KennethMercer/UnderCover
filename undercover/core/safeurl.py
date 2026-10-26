"""Links UnderCover fetches for someone (1.1 hardening): a voice install, a training source, a song from a link. They
may only reach the internet: a link whose host is, or resolves to, this PC, the home network or a link-local address
is refused with a plain sentence, and so is every redirect on the way. Downloads the app makes from its own pinned
sources (models, add-ons, yt-dlp) don't come here.

`check(url)` refuses a link by its name, early and plainly; `get(url, ...)` is requests.get with every redirect checked
before it is followed; `preflight(url)` follows a link's redirects (bodies unread) before yt-dlp gets it.

The guard (1.1.1 hardening): inside `guarded()`, every socket this thread connects or sends to is checked by the
address it actually uses, at that moment. A name is resolved there, each of its addresses checked, and the connection
pinned to a checked one, so a name that answers differently the second time (DNS rebinding) never gets through, and
neither does anything a tool finds by itself (yt-dlp's redirects, the addresses inside a page, fragments). Other
families than IPv4/IPv6 are refused. The switch is per thread: the server, the cadence worker on loopback, Tailscale
and the tray never see it. A thread started inside a guarded block starts unguarded; yt-dlp's requests re-enter the
guard in `youtube`'s urlopen, wherever they run.

With a proxy set (the environment or Windows' settings, which requests and yt-dlp both follow), the guard lets this
thread reach that proxy's own address and port, and links are then checked by name (`check`) before each request; the
proxy resolves names itself, so the rebinding race behind a proxy is the proxy's (the maintainers' notes, netguard)."""
import ipaddress
import socket
import threading
import time
from contextlib import contextmanager
from urllib.parse import urljoin, urlparse

from .voices import ModelError

MAX_REDIRECTS = 10
SAY = ("That link points to this PC or your home network, and UnderCover only fetches links from the internet. "
       "To use a file you have here, drop it onto the page instead.")
MARK = "UnderCover only fetches links from the internet"   # (in SAY: found in a wrapped error's text)
PROXY_TTL_S = 30


class LinkRefused(ModelError):
    """A link UnderCover won't fetch, said plainly. A refusal anywhere in a fetch ends as one of these."""


class Refused(OSError):
    """The socket-level refusal (an OSError, so every HTTP stack treats it as a failed connection)."""


_NAT64 = (ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("64:ff9b:1::/48"))


def _private(ip: str) -> bool:
    a = ipaddress.ip_address(ip.split("%", 1)[0])   # (a scoped IPv6 address: its zone dropped)
    if isinstance(a, ipaddress.IPv6Address):
        # an IPv4 address carried inside (mapped, 6to4, Teredo's client, NAT64) is checked as itself
        inner = a.ipv4_mapped or a.sixtofour or (a.teredo[1] if a.teredo else None)
        if inner is None and any(a in n for n in _NAT64):
            inner = ipaddress.IPv4Address(int(a) & 0xFFFFFFFF)
        if inner is not None and _private(str(inner)):
            return True
        if a.ipv4_mapped:
            a = a.ipv4_mapped
    return (a.is_private or a.is_loopback or a.is_link_local or a.is_multicast or a.is_reserved or a.is_unspecified
            or not a.is_global)


def check(url: str, resolve=socket.getaddrinfo) -> None:
    """Refuse a link that isn't http(s) to a host on the internet: its name resolved, every address it resolves to
    checked (a name with one public and one private address is refused). resolve: for the checks."""
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ModelError("That doesn't look like a link. Paste a full address starting with https://")
    host = u.hostname
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
        addrs = [host]
    except ValueError:
        try:
            addrs = sorted({info[4][0] for info in resolve(host, u.port or (443 if u.scheme == "https" else 80),
                                                           type=socket.SOCK_STREAM)})
        except (OSError, UnicodeError):
            raise ModelError(f"Couldn't find {host}. Check the link and your connection.")
    if not addrs or any(_private(a) for a in addrs):
        raise LinkRefused(SAY)


# ---------------------------------------------------------------- the guard

_T = threading.local()
_PROXIES = {"at": -PROXY_TTL_S, "addrs": frozenset()}
_PROXY_LOCK = threading.Lock()


def _norm(ip: str) -> str:
    return str(ipaddress.ip_address(ip.split("%", 1)[0]))


def _proxy_addrs() -> frozenset:
    """(ip, port) of every proxy set for this PC, resolved (cached PROXY_TTL_S)."""
    with _PROXY_LOCK:
        if time.monotonic() - _PROXIES["at"] < PROXY_TTL_S:
            return _PROXIES["addrs"]
    import urllib.request
    out = set()
    for scheme, p in urllib.request.getproxies().items():
        if scheme == "no" or not p:
            continue
        u = urlparse(p if "://" in p else "http://" + p)
        try:
            port = u.port or (1080 if u.scheme.startswith("socks") else 443 if u.scheme == "https" else 80)
            if u.hostname:
                out |= {(_norm(i[4][0]), port) for i in socket.getaddrinfo(u.hostname, port, type=socket.SOCK_STREAM)}
        except (OSError, ValueError, UnicodeError):
            continue
    with _PROXY_LOCK:
        _PROXIES.update(at=time.monotonic(), addrs=frozenset(out))
        return _PROXIES["addrs"]


def on() -> bool:
    """Is this thread guarded right now?"""
    return getattr(_T, "depth", 0) > 0


def proxied() -> bool:
    """A proxy is set for this thread's guarded block (links are then checked by name before each request)."""
    return on() and bool(getattr(_T, "proxies", None))


@contextmanager
def guarded():
    """Everything this thread connects or sends to inside the block reaches only the internet (see the module's
    notes). Nests."""
    d = getattr(_T, "depth", 0)
    if d == 0:
        _T.proxies = _proxy_addrs()
    _T.depth = d + 1
    try:
        yield
    finally:
        _T.depth = d


def _allowed(ip: str, port) -> bool:
    try:
        return not _private(ip) or (_norm(ip), port) in getattr(_T, "proxies", frozenset())
    except ValueError:
        return False


def _vet(sock, address):
    """The address a guarded socket may use in place of `address` (pinned to a checked one), or Refused."""
    if sock.family not in (socket.AF_INET, socket.AF_INET6) or not isinstance(address, tuple) or len(address) < 2:
        raise Refused(SAY)
    host, port = address[0], address[1]
    if isinstance(host, (bytes, bytearray)):
        host = bytes(host).decode("ascii", "replace")
    if not isinstance(host, str) or not host or host == "<broadcast>":
        raise Refused(SAY)
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:   # a name: resolved here, every answer checked, the first one used
        addrs = [i[4] for i in socket.getaddrinfo(host, port, sock.family, sock.type)]
        if not addrs or not all(_allowed(a[0], port) for a in addrs):
            raise Refused(SAY)
        return addrs[0]
    if not _allowed(host, port):
        raise Refused(SAY)
    return address


_SOCK = socket.socket
_ORIG: dict = {}


def install() -> None:
    """Put the guard over the socket class's connect and send-to calls, once per process (a no-op outside
    guarded()). ssl sockets and yt-dlp's socks proxy subclass this class, so they come through it too."""
    if getattr(_SOCK.connect, "_uc_guard", False):
        return
    _ORIG.update(connect=_SOCK.connect, connect_ex=_SOCK.connect_ex, sendto=_SOCK.sendto,
                 sendmsg=getattr(_SOCK, "sendmsg", None), sendmsg_afalg=getattr(_SOCK, "sendmsg_afalg", None))

    def connect(self, address):
        return _ORIG["connect"](self, _vet(self, address) if on() else address)

    def connect_ex(self, address):
        return _ORIG["connect_ex"](self, _vet(self, address) if on() else address)

    def sendto(self, data, *args):   # sendto(data, address) / sendto(data, flags, address)
        if on() and args:
            args = (*args[:-1], _vet(self, args[-1]))
        return _ORIG["sendto"](self, data, *args)

    def sendmsg(self, buffers, *args, **kw):   # sendmsg(buffers, ancdata, flags, address)
        if on():
            if len(args) >= 3:
                args = (*args[:2], _vet(self, args[2]), *args[3:])
            elif kw.get("address") is not None:
                kw["address"] = _vet(self, kw["address"])
            elif self.family not in (socket.AF_INET, socket.AF_INET6):
                raise Refused(SAY)
        return _ORIG["sendmsg"](self, buffers, *args, **kw)

    def sendmsg_afalg(self, *a, **kw):
        if on():
            raise Refused(SAY)
        return _ORIG["sendmsg_afalg"](self, *a, **kw)

    for name, fn in (("connect", connect), ("connect_ex", connect_ex), ("sendto", sendto), ("sendmsg", sendmsg),
                     ("sendmsg_afalg", sendmsg_afalg)):
        if _ORIG.get(name):
            fn._uc_guard = True
            setattr(_SOCK, name, fn)


install()


def refused_in(e: BaseException) -> LinkRefused | None:
    """The refusal inside an error a fetch wrapped (requests, yt-dlp), or None."""
    seen, stack = set(), [e]
    while stack:
        x = stack.pop()
        if not isinstance(x, BaseException) or id(x) in seen:
            continue
        seen.add(id(x))
        if isinstance(x, LinkRefused):
            return x
        if isinstance(x, Refused):
            return LinkRefused(SAY)
        info = getattr(x, "exc_info", None)
        stack += [x.__cause__, x.__context__, getattr(x, "reason", None), getattr(x, "cause", None),
                  info[1] if isinstance(info, tuple) and len(info) > 1 else None, *x.args]
    return LinkRefused(SAY) if MARK in str(e) else None


def get(url: str, **kw):
    """requests.get inside the guard, its redirects followed here, each one checked before it's fetched. -> the final
    response."""
    import requests
    kw.pop("allow_redirects", None)
    with guarded():
        for _ in range(MAX_REDIRECTS + 1):
            check(url)
            try:
                r = requests.get(url, allow_redirects=False, **kw)
            except requests.RequestException as e:
                no = refused_in(e)
                if no:
                    raise no from e
                raise
            if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                nxt = urljoin(url, r.headers["location"])
                r.close()
                url, kw = nxt, {k: v for k, v in kw.items() if k != "params"}   # (the query was for the first address)
                continue
            return r
    raise ModelError("That link redirects too many times.")


def preflight(url: str, timeout: float = 20) -> str:
    """Follow a link's redirects (each checked, no body read) before a tool that fetches it itself gets it. -> the last
    address. A site that can't be reached here is left to the tool to report: only a refused address stops it."""
    import requests
    check(url)
    try:
        r = get(url, stream=True, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
        r.close()
        return r.url or url
    except requests.RequestException:
        return url
