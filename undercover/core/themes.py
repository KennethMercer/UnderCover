"""Themes (phase 14; the contract is docs/THEMES.md): a theme is a grading of the look plus a type change, tokens
only, so a friend's theme can't break a page or hide anything. Nothing in a theme executes.

    themes/<id>/   theme.json, theme.css (two token blocks + @font-face), fonts/*.woff2 (+ OFL .txt), preview.png

- **Shipped** themes live in `undercover/web/themes/` (read only here); the user's own and imported ones in the library
  group, `[paths] themes_dir` (default `<library>/themes`), so they move with the library.
- **One check for both** (`check_css`, `check_folder`): at import, at save, and every time a library theme's css is served.
  The look chat's smoke check runs `check_folder` on the shipped ones.
- **Import** (`import_zip`): a `.uctheme` (the folder zipped, one top folder = the id): at most 4 MB in and out, only
  `.css .json .woff2 .png .txt`, no absolute or climbing paths, no links, nothing but theme.json / theme.css /
  preview.png / *.txt at the top and fonts/*.woff2|*.txt below; each file's own magic checked. Unpacked by hand into a
  staging folder inside the themes folder, checked, then put in place.
- **Delete / replace** of a library theme go through `storage.remove_files` with the themes folder as the only root;
  shipped themes are never touched.
"""
import io
import json
import os
import re
import secrets
import shutil
import stat
import zipfile
from pathlib import Path

from . import fsutil, storage
from .config import path
from .voices import ModelError

SHIPPED = Path(__file__).resolve().parents[1] / "web" / "themes"
ID = re.compile(r"[a-z0-9][a-z0-9-]{0,38}[a-z0-9]")      # 2-40 characters
MAX_BYTES = 4 * 1024 * 1024
EXTS = {".css", ".json", ".woff2", ".png", ".txt"}
TOP_FILES = {"theme.json", "theme.css", "preview.png"}
FONT_FILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,80}\.(woff2|txt)")
TXT_FILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,80}\.txt")

# the public token contract (docs/THEMES.md, "theme.css: the token contract")
TOKENS = frozenset(
    "--base --panel-base --panel-2-base --panel-3-base --line-base --fg --muted --accent-ink "
    "--c-frontman --c-doubleagent --c-speakeasy --c-safehouse "
    "--c-frontman-deep --c-doubleagent-deep --c-speakeasy-deep --c-safehouse-deep "
    "--ok --warn --err --wave --wave-on --wave-alt --display --body --mono --r --lit --tint --tint2 --lamp "
    "--t-fast --t-slow".split())
FONT_PROPS = frozenset({"font-family", "src", "font-weight", "font-style", "font-display", "font-stretch", "unicode-range"})
VALUE = re.compile(r"""[A-Za-z0-9#%.,()\-+/*\s"']+""")    # no backslash, no braces, no !, no ;, no @, no <, no :
FONT_SRC = re.compile(r"""url\(\s*(["']?)fonts/([A-Za-z0-9][A-Za-z0-9._-]{0,80}\.woff2)\1\s*\)(\s+format\(\s*["']woff2["']\s*\))?""")
BLOCKED = re.compile(r"url\(|src\(|expression|javascript|@import|behavior|-moz-binding|image-set|\\", re.I)


class ThemeError(ModelError):
    pass


# ---------------------------------------------------------------- the css check

def _blocks(text: str):
    """Top-level `prelude { body }` pairs of a comment-free stylesheet; ThemeError on anything else (nesting, stray text)."""
    out, i, n = [], 0, len(text)
    while True:
        while i < n and text[i].isspace():
            i += 1
        if i >= n:
            return out
        j = text.find("{", i)
        if j < 0:
            raise ThemeError(f"Text outside a rule: {text[i:i + 40]!r}")
        k = text.find("}", j)
        if k < 0 or "{" in text[j + 1:k]:
            raise ThemeError("A rule isn't closed, or has a rule inside it.")
        out.append((text[i:j].strip(), text[j + 1:k]))
        i = k + 1


def _decls(body: str):
    for part in body.split(";"):
        if not part.strip():
            continue
        name, colon, value = part.partition(":")
        if not colon:
            raise ThemeError(f"Not a property: {part.strip()[:40]!r}")
        yield name.strip(), value.strip()


def check_css(text: str, theme_id: str, fonts: set | None = None) -> list[str]:
    """Problems with a theme.css against the contract (an empty list: it checks out). fonts: the files in the theme's
    fonts/ folder; every @font-face url must name one of them (None: don't look)."""
    if len(text.encode("utf-8")) > 256 * 1024:
        return ["theme.css is over 256 KB: a token sheet is a few KB."]
    if "\\" in text:
        return ["A backslash in theme.css: escapes could hide a url or an @import."]
    if "<" in text:
        return ["A '<' in theme.css."]
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    if "/*" in text or "*/" in text:
        return ["A comment isn't closed."]
    try:
        blocks = _blocks(text)
    except ThemeError as e:
        return [str(e)]
    tid = re.escape(theme_id)
    dark = re.compile(rf""":root\[data-uc-theme=(["']){tid}\1\]""")
    light = re.compile(rf""":root\[data-uc-theme=(["']){tid}\1\]\[data-theme=(["'])light\2\]""")
    problems, seen = [], set()
    for prelude, body in blocks:
        sel = re.sub(r"\s+", "", prelude)
        if sel == "@font-face":
            props = {}
            for name, value in _decls(body):
                if name not in FONT_PROPS:
                    problems.append(f"@font-face may not set {name!r}.")
                    continue
                props[name] = value
                if name == "src":
                    srcs = [s.strip() for s in value.split(",")]
                    for s in srcs:
                        m = FONT_SRC.fullmatch(s)
                        if not m:
                            problems.append(f"@font-face src must be url(\"fonts/<file>.woff2\"): {s[:60]!r}")
                        elif fonts is not None and m.group(2) not in fonts:
                            problems.append(f"@font-face names fonts/{m.group(2)}, which the theme doesn't have.")
                elif BLOCKED.search(value) or not VALUE.fullmatch(value):
                    problems.append(f"@font-face {name}: {value[:60]!r} isn't allowed.")
            if "src" not in props or "font-family" not in props:
                problems.append("@font-face needs font-family and src.")
            continue
        which = "dark" if dark.fullmatch(sel) else "light" if light.fullmatch(sel) else None
        if which is None:
            problems.append(f"Only the theme's two :root blocks and @font-face are allowed, not {prelude[:60]!r}.")
            continue
        if which in seen:
            problems.append(f"The {which} block appears twice.")
        seen.add(which)
        for name, value in _decls(body):
            if name not in TOKENS:
                problems.append(f"{name!r} isn't one of the theme tokens.")
            elif not value or BLOCKED.search(value) or not VALUE.fullmatch(value):
                problems.append(f"{name}: {value[:60]!r} isn't allowed (colours, lengths, percentages, times, font names).")
    if blocks and "dark" not in seen:   # no rules at all is allowed: Case File's own sheet is a comment (app.css holds it)
        problems.append(f'theme.css has no :root[data-uc-theme="{theme_id}"] block.')
    return problems


# ---------------------------------------------------------------- the folder check

def _magic_ok(name: str, data: bytes) -> bool:
    ext = Path(name).suffix.lower()
    if ext == ".png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if ext == ".woff2":
        return data.startswith(b"wOF2")
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def allowed_name(rel: str) -> bool:
    """A file a theme may hold, by its path inside the theme folder ('theme.css', 'fonts/X.woff2', ...)."""
    parts = rel.split("/")
    if len(parts) == 1:
        return parts[0] in TOP_FILES or bool(TXT_FILE.fullmatch(parts[0]))
    return len(parts) == 2 and parts[0] == "fonts" and bool(FONT_FILE.fullmatch(parts[1]))


PLAIN = re.compile(r"[^<>\x00-\x1f\x7f]*")   # theme.json texts: no markup, no control characters


def check_meta(meta, theme_id: str | None = None) -> list[str]:
    if not isinstance(meta, dict):
        return ["theme.json isn't an object."]
    p = []
    texts = [meta.get(k) for k in ("name", "author", "licence", "version", "based_on")] + list(meta.get("fonts") or [])
    if any(isinstance(t, str) and not PLAIN.fullmatch(t) for t in texts):
        p.append("theme.json texts may not hold < > or control characters.")
    if "id" in meta and theme_id and meta["id"] != theme_id:
        p.append(f"theme.json says id {meta['id']!r}, but the folder is {theme_id!r}.")
    if not isinstance(meta.get("name"), str) or not 1 <= len(meta["name"].strip()) <= 60:
        p.append("theme.json needs a name (up to 60 characters).")
    for k in ("author", "licence", "version", "based_on"):
        if k in meta and not (isinstance(meta[k], str) and len(meta[k]) <= 120):
            p.append(f"theme.json: {k} must be a short text.")
    modes = meta.get("modes", ["dark"])
    if not (isinstance(modes, list) and modes and set(modes) <= {"dark", "light"}):
        p.append('theme.json: modes must be ["dark"] or ["dark", "light"].')
    fonts = meta.get("fonts", [])
    if not (isinstance(fonts, list) and all(isinstance(f, str) and len(f) <= 80 for f in fonts)):
        p.append("theme.json: fonts must be a list of family names.")
    return p


def check_folder(d: Path, theme_id: str | None = None) -> list[str]:
    """Problems with a theme folder (an empty list: it checks out). The id is the folder's name unless given."""
    d = Path(d)
    tid = theme_id or d.name
    if not ID.fullmatch(tid):
        return [f"{tid!r} isn't a theme id (2-40 of a-z, 0-9 and -)."]
    if not d.is_dir() or d.is_symlink():
        return [f"{d} isn't a folder."]
    problems, total, files = [], 0, {}
    for dirpath, dirs, names in os.walk(d, followlinks=False):
        for x in dirs:
            p = Path(dirpath) / x
            if p.is_symlink() or _reparse(p):
                problems.append(f"A link in the theme: {p.name}")
        dirs[:] = [x for x in dirs if not (Path(dirpath) / x).is_symlink()]
        for x in names:
            p = Path(dirpath) / x
            rel = p.relative_to(d).as_posix()
            if p.is_symlink() or _reparse(p):
                problems.append(f"A link in the theme: {rel}")
                continue
            if not allowed_name(rel):
                problems.append(f"{rel} doesn't belong in a theme (theme.json, theme.css, preview.png, fonts/*.woff2, *.txt).")
                continue
            total += p.stat().st_size
            files[rel] = p
    if total > MAX_BYTES:
        problems.append(f"The theme is {total / 1e6:.1f} MB; at most 4 MB.")
    for need in ("theme.json", "theme.css"):
        if need not in files:
            problems.append(f"{need} is missing.")
    if problems:
        return problems
    for rel, p in files.items():
        if not _magic_ok(rel, p.read_bytes()):
            problems.append(f"{rel} isn't what its name says.")
    try:
        problems += check_meta(json.loads(files["theme.json"].read_text(encoding="utf-8")), tid)
    except ValueError:
        problems.append("theme.json isn't valid JSON.")
    fonts = {rel.split("/", 1)[1] for rel in files if rel.startswith("fonts/") and rel.endswith(".woff2")}
    problems += check_css(files["theme.css"].read_text(encoding="utf-8", errors="replace"), tid, fonts)
    return problems


_CHECKED: dict = {}   # folder -> (its files' names, sizes and times, problems)


def checked(d: Path) -> list[str]:
    """check_folder, kept until a file in the folder changes (served css and fonts ask on every request)."""
    try:
        sig = tuple(sorted((str(f.relative_to(d)), f.stat().st_size, f.stat().st_mtime_ns) for f in d.rglob("*")))
    except OSError:
        sig = None
    hit = _CHECKED.get(str(d))
    if sig is not None and hit and hit[0] == sig:
        return hit[1]
    problems = check_folder(d)
    if sig is not None:
        _CHECKED[str(d)] = (sig, problems)
    return problems


def _reparse(p: Path) -> bool:
    """A Windows junction or other reparse point (is_symlink misses junctions)."""
    try:
        return bool(getattr(os.lstat(p), "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    except OSError:
        return False


# ---------------------------------------------------------------- where they are

def library() -> Path:
    return path("themes_dir")


def find(theme_id: str) -> tuple[Path, bool]:
    """-> (folder, shipped). LookupError if there's no such theme."""
    if not ID.fullmatch(theme_id or ""):
        raise LookupError("No such theme.")
    for root, shipped in ((SHIPPED, True), (library(), False)):
        d = root / theme_id
        if d.is_dir() and not d.is_symlink():
            return d, shipped
    raise LookupError("No such theme.")


def listing() -> list[dict]:
    """Shipped + library themes: id, shipped, theme.json (what's readable), ok + problems, the urls the page uses. A library
    theme whose id a shipped one took later (an update) is listed too, marked, so it can be deleted or shared."""
    _sweep()
    out, seen = [], set()
    for root, shipped in ((SHIPPED, True), (library(), False)):
        if not root.is_dir():
            continue
        for d in sorted(root.iterdir()):
            if not d.is_dir() or not ID.fullmatch(d.name):
                continue
            shadowed = d.name in seen
            seen.add(d.name)
            problems = checked(d)
            if shadowed:
                problems = [f"A theme that comes with UnderCover is now called {d.name} too: this one can't be used. Delete "
                            "it here, or export it and import it again under another name."] + problems
            try:
                meta = json.loads((d / "theme.json").read_text(encoding="utf-8"))
                meta = meta if isinstance(meta, dict) else {}
            except (OSError, ValueError):
                meta = {}
            out.append({"id": d.name, "name": str(meta.get("name") or d.name)[:60],
                        **{k: meta.get(k) for k in ("author", "licence", "version", "based_on")},
                        "modes": meta.get("modes") or ["dark"], "fonts": meta.get("fonts") or [], "shipped": shipped,
                        "css": f"/static/themes/{d.name}/theme.css" if shipped else f"/api/themes/{d.name}/theme.css",
                        "preview": f"/api/themes/{d.name}/preview.png" if (d / "preview.png").is_file() else None,
                        "ok": not problems, "why": problems[0] if problems else None, "problems": problems[:5],
                        **({"shadowed": True} if shadowed else {})})
    return out


def css_of(theme_id: str) -> str:
    """The theme's css, checked again at load (a library folder can be edited by hand)."""
    d, _ = find(theme_id)
    problems = checked(d)
    if problems:
        raise ThemeError(f"The theme {theme_id} doesn't check out: {problems[0]}")
    return (d / "theme.css").read_text(encoding="utf-8")


def file_of(theme_id: str, rel: str) -> Path:
    """A font or the preview of a checked theme."""
    d, _ = find(theme_id)
    if not allowed_name(rel) or rel.endswith((".css", ".json")) or checked(d):
        raise LookupError("No such file.")
    f = d / rel
    if not f.is_file() or not storage._inside(f, d):
        raise LookupError("No such file.")
    return f


# ---------------------------------------------------------------- import, save, export, delete

def _guard():
    """Test mode: themes are written only inside the test's folders (the storage guard covers the deletes)."""
    if storage.ALLOWED_ROOTS is not None:
        why = storage.refuse([library()])
        if why:
            raise ThemeError(why)


def _sweep(older: float = 3600):
    """Staging and set-aside folders an interrupted import or save left behind (.incoming-*, .old-*)."""
    import time
    root = library()
    for d in root.glob(".*-*"):
        if d.is_dir() and d.name.startswith((".incoming-", ".old-")) and time.time() - d.stat().st_mtime > older:
            try:
                _drop(d)
            except (OSError, ModelError):
                pass


def _stage() -> Path:
    _guard()
    _sweep()
    s = library() / f".incoming-{secrets.token_hex(4)}"
    s.mkdir(parents=True)
    return s


def _drop(d: Path):
    storage.remove_files([], [library()], dirs=[d])


def _place(staged: Path, theme_id: str, replace: bool) -> Path:
    """Check the staged folder, then put it in place as library/<id> (replacing a library theme only when asked)."""
    target = library() / theme_id
    try:
        problems = check_folder(staged, theme_id)
        if problems:
            raise ThemeError("That theme doesn't check out: " + "; ".join(problems[:3]))
        if (SHIPPED / theme_id).exists():
            raise FileExistsError(f"{theme_id} is the name of a theme that comes with UnderCover; give yours another name.")
        _guard()
        old = None
        if target.exists():
            if not replace:
                raise FileExistsError(f"You already have a theme called {theme_id}.")
            old = library() / f".old-{secrets.token_hex(4)}"   # aside first: if the new one can't go in, it comes back
            fsutil.retry(lambda: os.replace(target, old))
        try:
            fsutil.retry(lambda: os.replace(staged, target))
        except OSError:
            if old is not None:
                fsutil.retry(lambda: os.replace(old, target))
            raise
        if old is not None:
            _drop(old)
    finally:
        if staged.exists():
            _drop(staged)
    return target


def import_zip(data: bytes, replace: bool = False) -> dict:
    """A .uctheme -> the theme's listing row. Everything is checked before a byte is written outside the staging folder."""
    if len(data) > MAX_BYTES:
        raise ThemeError("That file is over 4 MB: a theme is a few KB, fonts a few hundred.")
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        infos = z.infolist()
    except (zipfile.BadZipFile, ValueError):
        raise ThemeError("That isn't a theme file (.uctheme is a zip).") from None
    tops, total, files = set(), 0, []
    for i in infos:
        n = i.filename
        if n.endswith("/"):
            continue
        mode = (i.external_attr >> 16) & 0o170000
        if mode == stat.S_IFLNK:
            raise ThemeError(f"A link in the theme file: {n}")
        if "\\" in n or n.startswith("/") or ":" in n or any(part in ("", ".", "..") for part in n.split("/")):
            raise ThemeError(f"A path that climbs out or isn't plain: {n!r}")
        parts = n.split("/")
        if len(parts) < 2:
            raise ThemeError("The theme file must hold one folder named after the theme.")
        tops.add(parts[0])
        rel = "/".join(parts[1:])
        if Path(n).suffix.lower() not in EXTS or not allowed_name(rel):
            raise ThemeError(f"{n} doesn't belong in a theme (.css .json .woff2 .png .txt only, in their places).")
        total += i.file_size
        if total > MAX_BYTES:
            raise ThemeError("The theme unpacks to over 4 MB.")
        files.append((i, rel))
    if len(tops) != 1:
        raise ThemeError("The theme file must hold exactly one folder.")
    theme_id = tops.pop()
    if not ID.fullmatch(theme_id):
        raise ThemeError(f"{theme_id!r} isn't a theme id (2-40 of a-z, 0-9 and -).")
    staged, written = _stage(), 0
    try:
        for i, rel in files:
            with z.open(i) as src:   # the real bytes, whatever the zip's headers say
                blob = src.read(MAX_BYTES - written + 1)
            written += len(blob)
            if written > MAX_BYTES:
                raise ThemeError("The theme unpacks to over 4 MB.")
            f = staged / rel
            if not storage._inside(f, staged):
                raise ThemeError(f"A path that climbs out: {rel!r}")
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(blob)
    except BaseException:
        _drop(staged)
        raise
    _place(staged, theme_id, replace)
    return next(t for t in listing() if t["id"] == theme_id and not t["shipped"])


def save(body: dict) -> dict:
    """The maker's Save: {json: {theme.json, with "id"}, css: "...", fonts: [{file, data: base64}], preview: base64 | None}.
    Saving a library theme again replaces it; a shipped theme's id is refused (FileExistsError)."""
    import base64
    meta, css = body.get("json"), body.get("css")
    if not isinstance(meta, dict) or not isinstance(css, str):
        raise ThemeError("A theme needs its theme.json and theme.css.")
    theme_id = str(meta.get("id") or "")
    if not ID.fullmatch(theme_id):
        raise ThemeError("A theme id is 2-40 of a-z, 0-9 and -.")
    out = {"theme.json": json.dumps(meta, indent=1).encode(), "theme.css": css.encode("utf-8")}
    try:
        if body.get("preview"):
            out["preview.png"] = base64.b64decode(body["preview"], validate=True)
        for f in body.get("fonts") or []:
            out[f"fonts/{f['file']}"] = base64.b64decode(f["data"], validate=True)
    except (ValueError, TypeError, AttributeError, KeyError):
        raise ThemeError("A font or the preview isn't valid ({file, data} with base64 data).") from None
    if sum(map(len, out.values())) > MAX_BYTES:
        raise ThemeError("The theme is over 4 MB.")
    for rel in out:
        if not allowed_name(rel):
            raise ThemeError(f"{rel} doesn't belong in a theme.")
    staged = _stage()
    try:
        for rel, blob in out.items():
            f = staged / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(blob)
    except BaseException:
        _drop(staged)
        raise
    _place(staged, theme_id, replace=True)
    return next(t for t in listing() if t["id"] == theme_id and not t["shipped"])


def export(theme_id: str) -> tuple[str, bytes]:
    """-> ("<Name>.uctheme", the zip): the checked folder under one top folder named by the id."""
    d, _ = find(theme_id)
    problems = check_folder(d)
    if problems:
        raise ThemeError(f"That theme doesn't check out, so it can't be shared: {problems[0]}")
    name = json.loads((d / "theme.json").read_text(encoding="utf-8")).get("name") or theme_id
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(d.rglob("*")):
            if f.is_file():
                z.write(f, f"{theme_id}/{f.relative_to(d).as_posix()}")
    safe = re.sub(r"[^A-Za-z0-9 ._-]", "", str(name)).strip() or theme_id
    return f"{safe}.uctheme", buf.getvalue()


def delete(theme_id: str) -> dict:
    """Remove a library theme (never a shipped one), through the storage guard. The library's copy goes even when a
    shipped theme has the same id."""
    if not ID.fullmatch(theme_id or ""):
        raise LookupError("No such theme.")
    d = library() / theme_id
    if not d.is_dir() or d.is_symlink():
        find(theme_id)   # LookupError if there's none at all
        raise ThemeError("Themes that come with UnderCover can't be deleted.")
    _drop(d)
    return {"ok": True, "id": theme_id}
