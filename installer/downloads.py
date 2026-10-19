"""Every file the installer downloads, resolved once at build time (build.ps1) into installer/downloads.tsv.

The installer's download page (UnderCover.iss) fetches them, each checked against its SHA-256: the tools and the
packages into <install>/cache/downloads, the AI models straight into their place in the models folder. After that page
nothing goes online: setup.ps1 installs Python, the packages (uv --offline, from that folder), the tools and the
add-ons from there. A cancelled or failed run starts again where it stopped (a file already there, at its size, isn't
fetched again); a repair uses the same list.

One tab-separated line per file:  group, dest, urls, sha256, bytes, pin
  group   base (uv, Python, ffmpeg, Deno), torch-<backend> (rocm, cuda128, cuda126, cpu: the one setup detects),
          packages (common.txt + cadence.txt), models (every model a feature uses), addon-<id> (only when its licence
          was accepted)
  dest    cache/<file name> or models/<path under the models folder>
  urls    one or more, separated by spaces: tried in order (a model's mirrors)
  pin     a package's offline requirement ("name==version"; its hash is the sha256 column), else empty

The packages are the exact files their pinned hashes name (requirements/*.txt): for each pin the build picks the file
whose SHA-256 is listed and that installs on CPython 3.12 for 64-bit Windows; a pin without one fails the build. Three
pins are source archives (openai-whisper, antlr4-python3-runtime, AMD's rocm): uv builds them offline with the pinned
setuptools, which is in the same folder.

  python installer/downloads.py            write installer/downloads.tsv (reads PyPI, the PyTorch index, the hosts)
  python installer/downloads.py --check    what the list would hold, by group; write nothing
"""
from __future__ import annotations

import argparse
import html.parser
import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
REQ = HERE / "requirements"
OUT = HERE / "downloads.tsv"
BACKENDS = ("rocm", "cuda128", "cuda126", "cpu")
UA = {"User-Agent": "UnderCover-build"}
COLUMNS = ("group", "dest", "urls", "sha256", "bytes", "pin")

# the tools, at the versions setup.ps1 names (smoke checks they agree); Python is the python-build-standalone build
# uv 0.12.18 installs for 3.12.10, its SHA-256 from that release's SHA256SUMS
PY_FILE = "cpython-3.12.10+20250529-x86_64-pc-windows-msvc-install_only_stripped.tar.gz"
TOOLS = [
    ("uv-x86_64-pc-windows-msvc.zip", "https://github.com/astral-sh/uv/releases/download/0.12.18/uv-x86_64-pc-windows-msvc.zip",
     "cae6a3bc25239f83dffb467a4b180508d9da23986c04639ebfa44e43e6a84bff"),
    (PY_FILE, "https://github.com/astral-sh/python-build-standalone/releases/download/20250529/" + urllib.parse.quote(PY_FILE),
     "ca22a9a9e64ecab6d0b5de7cdf8b679ccaa41e9def6aaa2b4aaa6bb23ec7aaba"),
    ("ffmpeg-8.0-essentials_build.zip", "https://github.com/GyanD/codexffmpeg/releases/download/8.0/ffmpeg-8.0-essentials_build.zip",
     "647e467caf82b9fa200a562769b5ff4d736aaf725804ed2c64ea9752106fa569"),
    ("deno-x86_64-pc-windows-msvc.zip", "https://github.com/denoland/deno/releases/download/v2.9.7/deno-x86_64-pc-windows-msvc.zip",
     "a0c3101b4158d1dfb7d6a78a7bf0f3de80c96bb423c152beec8beb22786f2238"),
]


def get(url: str, method: str = "GET"):
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA, method=method), timeout=60)


def size_of(url: str) -> int:
    with get(url, "HEAD") as r:
        n = int(r.headers.get("Content-Length") or 0)
    if not n:
        raise SystemExit(f"{url}: the server didn't say its size")
    return n


def read_pins(path: Path) -> list[dict]:
    """pip requirement lines (with \\ continuations) -> [{name, version, url, hashes}]"""
    text = re.sub(r"\\\r?\n", " ", path.read_text(encoding="utf-8"))
    pins = []
    for line in text.splitlines():
        line = line.split(" #", 1)[0].strip()
        if not line or line.startswith("#"):
            continue
        hashes = re.findall(r"--hash=sha256:([0-9a-f]{64})", line)
        m = re.match(r"([A-Za-z0-9_.\-]+)\s*@\s*(\S+)", line)
        if m:
            url, _, frag = m.group(2).partition("#")
            hashes += re.findall(r"sha256=([0-9a-f]{64})", frag)
            pins.append({"name": m.group(1), "version": None, "url": url, "hashes": hashes})
            continue
        m = re.match(r"([A-Za-z0-9_.\-]+)(\[[^\]]*\])?==([^\s;]+)", line)
        if not m:
            raise SystemExit(f"{path.name}: can't read '{line[:80]}'")
        pins.append({"name": m.group(1), "version": m.group(3), "url": None, "hashes": hashes})
    return pins


def fits(filename: str) -> int:
    """How well a file installs on CPython 3.12, 64-bit Windows: 0 = not at all, higher = more specific; a source
    archive is 1 (built offline, last choice)."""
    if not filename.endswith(".whl"):
        return 1 if filename.endswith((".tar.gz", ".zip")) else 0
    py, abi, plat = filename[:-4].split("-")[-3:]
    plats, pys = plat.split("."), py.split(".")
    if not any(p in ("win_amd64", "any") for p in plats):
        return 0
    native = "win_amd64" in plats
    if "cp312" in pys and abi in ("cp312", "abi3", "none"):
        return 6 if native else 5
    if abi == "abi3" and any(re.fullmatch(r"cp3\d+", p) and int(p[3:]) <= 12 for p in pys):
        return 4 if native else 3
    if abi == "none" and any(p == "py3" or re.fullmatch(r"py3\d*", p) or p == "py2" for p in pys) and any(p.startswith("py3") for p in pys):
        return 3 if native else 2
    return 0


def version_of(filename: str) -> str:
    """The version in a wheel's or a source archive's file name."""
    if filename.endswith(".whl"):
        return filename.split("-")[1]
    stem = re.sub(r"\.(tar\.gz|zip)$", "", filename)
    return stem.rsplit("-", 1)[1]


class _Links(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.links.append(dict(attrs).get("href", ""))


def candidates(pin: dict, index: str | None) -> list[dict]:
    """The files of one pinned version: [{file, url, sha256, bytes}]."""
    if index:   # a simple (PEP 503) index: PyTorch's
        name = re.sub(r"[-_.]+", "-", pin["name"]).lower()
        with get(f"{index}/{name}/") as r:
            p = _Links()
            p.feed(r.read().decode("utf-8", "replace"))
        out = []
        for href in p.links:
            url, _, frag = href.partition("#")
            url = urllib.parse.urljoin(f"{index}/{name}/", url)
            sha = re.search(r"sha256=([0-9a-f]{64})", frag)
            if sha:
                out.append({"file": urllib.parse.unquote(url.rsplit("/", 1)[-1]), "url": url, "sha256": sha.group(1), "bytes": None})
        return out
    with get(f"https://pypi.org/pypi/{pin['name']}/{pin['version']}/json") as r:
        data = json.load(r)
    return [{"file": u["filename"], "url": u["url"], "sha256": u["digests"]["sha256"], "bytes": u["size"]} for u in data["urls"]]


def resolve(pins: list[dict], index: str | None = None) -> tuple[list[dict], list[str]]:
    """[{file, url, sha256, bytes, pin}] for the pins, and what couldn't be resolved."""
    files, problems = [], []
    for pin in pins:
        if pin["url"]:
            file = urllib.parse.unquote(pin["url"].rsplit("/", 1)[-1])
            if not fits(file):
                problems.append(f"{pin['name']}: {file} doesn't install on CPython 3.12 / Windows x64")
            elif not pin["hashes"]:
                problems.append(f"{pin['name']}: no SHA-256 pinned")
            else:
                files.append({"file": file, "url": pin["url"], "sha256": pin["hashes"][0], "bytes": None,
                              "pin": f"{pin['name']}=={version_of(file)}"})
            continue
        good = [c for c in candidates(pin, index) if c["sha256"] in pin["hashes"] and fits(c["file"])]
        if not good:
            problems.append(f"{pin['name']}=={pin['version']}: no file with a pinned hash installs on CPython 3.12 / Windows x64")
            continue
        best = max(good, key=lambda c: fits(c["file"]))
        files.append({**best, "pin": f"{pin['name']}=={pin['version']}"})
    for f in files:
        f["bytes"] = f["bytes"] or size_of(f["url"])
    return files, problems


def package_rows(group: str, req: Path, index: str | None = None) -> tuple[list[dict], list[str]]:
    files, bad = resolve(read_pins(req), index)
    rows = [{"group": group, "dest": f"cache/{f['file']}", "urls": f["url"], "sha256": f["sha256"], "bytes": f["bytes"],
             "pin": f["pin"]} for f in files]
    return rows, [f"{req.name}: {b}" for b in bad]


def model_rows() -> list[dict]:
    """Every model a feature uses (undercover.models.wanted), and every optional add-on's model files."""
    sys.path.insert(0, str(ROOT))
    from undercover import models
    from undercover.core import addons, setup_models
    rows = []
    for local in sorted(models.wanted()):
        rec = setup_models.PINNED[local]
        rows.append({"group": "models", "dest": f"models/{local}", "urls": " ".join(rec["urls"]), "sha256": rec["sha256"],
                     "bytes": rec["size"] or size_of(rec["urls"][0]), "pin": ""})
    for a in sorted(addons.ADDONS.values(), key=lambda a: a.id):
        if a.included or not a.wired:
            continue
        for local, url in sorted(a.files.items()):
            want = a.sha256.get(local) or setup_models._expected(url, a.sha256)
            if not want:
                raise SystemExit(f"add-on {a.id}: {local} has no SHA-256")
            rows.append({"group": f"addon-{a.id}", "dest": f"models/{local}", "urls": url, "sha256": want,
                         "bytes": (setup_models.PINNED.get(local) or {}).get("size") or size_of(url), "pin": ""})
    return rows


def addon_package_rows() -> tuple[list[dict], list[str]]:
    rows, problems = [], []
    for req in sorted(REQ.glob("addon-*.txt")):
        r, p = package_rows(req.stem, req)
        rows += r
        problems += p
    return rows, problems


def build() -> tuple[list[dict], list[str]]:
    rows = [{"group": "base", "dest": f"cache/{f}", "urls": u, "sha256": s, "bytes": size_of(u), "pin": ""} for f, u, s in TOOLS]
    problems = []
    for group, req, index in [("packages", "common.txt", None), ("packages", "cadence.txt", None)] + \
            [(f"torch-{b}", f"torch-{b}.txt", None if b == "rocm" else f"https://download.pytorch.org/whl/{b.replace('cuda', 'cu')}")
             for b in BACKENDS]:
        r, p = package_rows(group, REQ / req, index)
        rows += r
        problems += p
    r, p = addon_package_rows()
    rows += r
    problems += p
    rows += model_rows()
    dests = [r["dest"] for r in rows]
    problems += [f"two files would land on {d}" for d in sorted({d for d in dests if dests.count(d) > 1})
                 if len({x["sha256"] for x in rows if x["dest"] == d}) > 1]
    return rows, problems


def write(rows: list[dict], out: Path = OUT) -> None:
    seen, lines = set(), ["\t".join(COLUMNS)]
    for r in rows:
        key = (r["group"], r["dest"])
        if key in seen:   # the same file pinned twice in one group (a package in common.txt and cadence.txt)
            continue
        seen.add(key)
        lines.append("\t".join(str(r[c]) for c in COLUMNS))
    out.write_text("\n".join(lines) + "\n", encoding="ascii", newline="\r\n")   # (CRLF, like the repo's working copies)


def read(path: Path = OUT) -> list[dict]:
    """The list as build() made it (setup.ps1 and the installer read the same file)."""
    lines = path.read_text(encoding="ascii").splitlines()
    head = lines[0].split("\t")
    return [dict(zip(head, ln.split("\t"))) for ln in lines[1:] if ln.strip()]


def summary(rows: list[dict]) -> list[str]:
    groups = {}
    for r in rows:
        g = groups.setdefault(r["group"], [0, 0])
        g[0] += 1
        g[1] += int(r["bytes"])
    return [f"{g}: {n} files, {b / 1e9:.2f} GB" for g, (n, b) in groups.items()]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args(argv)
    rows, problems = build()
    for line in summary(rows):
        print(line)
    for p in problems:
        print("PROBLEM:", p)
    if problems:
        return 1
    if not a.check:
        write(rows)
        print(f"wrote {OUT.relative_to(ROOT)} ({len(rows)} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
