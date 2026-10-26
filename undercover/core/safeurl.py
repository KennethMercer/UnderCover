"""Links UnderCover fetches for someone (1.1 hardening): a voice install, a training source, a song from a link. They
may only reach the internet: a link whose host is, or resolves to, this PC, the home network or a link-local address
is refused with a plain sentence, and so is every redirect on the way. Downloads the app makes from its own pinned
sources (models, add-ons, yt-dlp) don't come here.

`check(url)` refuses a link; `get(url, ...)` is requests.get with every redirect checked before it is followed;
`preflight(url)` follows a link's redirects (bodies unread) so a tool that fetches it itself (yt-dlp) starts from a
checked address."""
import ipaddress
import socket
from urllib.parse import urljoin, urlparse

from .voices import ModelError

MAX_REDIRECTS = 10
SAY = ("That link points to this PC or your home network, and UnderCover only fetches links from the internet. "
       "To use a file you have here, drop it onto the page instead.")


def _private(ip: str) -> bool:
    a = ipaddress.ip_address(ip.split("%", 1)[0])   # (a scoped IPv6 address: its zone dropped)
    if isinstance(a, ipaddress.IPv6Address) and a.ipv4_mapped:
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
        raise ModelError(SAY)


def get(url: str, **kw):
    """requests.get, its redirects followed here, each one checked before it's fetched. -> the final response."""
    import requests
    kw.pop("allow_redirects", None)
    for _ in range(MAX_REDIRECTS + 1):
        check(url)
        r = requests.get(url, allow_redirects=False, **kw)
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
