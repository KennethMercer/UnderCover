"""The theme routes (phase 14; core/themes.py has the checks, docs/THEMES.md the contract):

    GET    /api/themes                       {themes: [{id, name, author, licence, version, modes, based_on, fonts, shipped,
                                             css, preview, ok, why}]} (shipped + library)
    POST   /api/themes                       the maker's Save {json (with id), css, fonts: [{file, data}], preview}; saving a
                                             library theme again replaces it, a shipped theme's id is refused (409)
    POST   /api/themes/import[?replace=1]    the body is a .uctheme (zip, at most 4 MB), checked before anything is kept
    GET    /api/themes/<id>/export           the theme as <Name>.uctheme
    GET    /api/themes/<id>/theme.css        checked again at load (text/css)
    GET    /api/themes/<id>/fonts/<file>     a bundled font (or its licence .txt)
    GET    /api/themes/<id>/preview.png
    DELETE /api/themes/<id>                  library themes only, through the storage guard
Phones (app/phone_api.ROUTES): the GETs; saving, importing and deleting need the PC.
"""
import re
from urllib.parse import parse_qs, quote, urlparse

from ..core import themes
from ..core.themes import ThemeError

_ID = r"[a-z0-9][a-z0-9-]{0,38}[a-z0-9]"


def handle(h, method: str, p: str) -> bool:
    if p != "/api/themes" and not p.startswith("/api/themes/"):
        return False
    try:
        _route(h, method, p)
    except FileExistsError as e:
        h._error(str(e), 409)
    except ThemeError as e:
        h._error(str(e), 422)
    except LookupError as e:
        h._error(str(e), 404)
    return True


def _send(h, data: bytes, ctype: str, name: str | None = None):
    h.send_response(200)
    h.send_header("Content-Type", ctype)
    h.send_header("Content-Length", str(len(data)))
    h.send_header("Cache-Control", "no-cache")
    if name:
        h.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(name)}")
    h.end_headers()
    h.wfile.write(data)


def _route(h, method, p):
    if p == "/api/themes":
        if method == "GET":
            return h._json({"themes": themes.listing()})
        if method == "POST":
            return h._json(themes.save(h._body()))
        raise LookupError("Not found")
    if p == "/api/themes/import" and method == "POST":
        n = h._length()
        if not n or n > themes.MAX_BYTES:
            h.close_connection = True   # the body isn't read
            raise ThemeError("That file is empty or over 4 MB: a theme is a few KB, fonts a few hundred.")
        data = h.rfile.read(n)
        replace = (parse_qs(urlparse(h.path).query).get("replace") or [""])[0] == "1"
        return h._json(themes.import_zip(data, replace=replace))
    m = re.fullmatch(rf"/api/themes/({_ID})(?:/(export|theme\.css|preview\.png|fonts/([^/]+)))?", p)
    if not m:
        raise LookupError("Not found")
    tid, what, font = m.groups()
    if what is None and method == "DELETE":
        return h._json(themes.delete(tid))
    if method != "GET":
        raise LookupError("Not found")
    if what == "export":
        name, data = themes.export(tid)
        return _send(h, data, "application/zip", name)
    if what == "theme.css":
        return _send(h, themes.css_of(tid).encode("utf-8"), "text/css; charset=utf-8")
    if what == "preview.png":
        return _send(h, themes.file_of(tid, "preview.png").read_bytes(), "image/png")
    if font:
        f = themes.file_of(tid, f"fonts/{font}")
        return _send(h, f.read_bytes(), "font/woff2" if f.suffix == ".woff2" else "text/plain; charset=utf-8")
    raise LookupError("Not found")
