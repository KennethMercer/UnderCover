"""The tray icon (phase 18; not core/tray.py, which is the recent tray of covers and speech; ARCHITECTURE "System tray", plan docs/research/tray-plan.md).

    pythonw -m undercover.tray [--port N]      (the server starts it; one per user, core/platform.single_instance)

Its own light process: no torch, no jobs. It talks to the server the way the page does (HTTP on 127.0.0.1, the page's
Origin) and uses the same routes, so it survives restarts and shows them:
  every 5 s: /api/status + /api/jobs (+ /api/phone for the tick); every 5 min: /api/doctor (a "bad" result = the dot)
  actions:   Open, Pause / Resume (/api/work/pause|resume), Phone access (/api/phone), Restart, Quit, Start with
             Windows (a Startup-folder shortcut to the launcher, only on the user's click); when the server is down:
             Start UnderCover, Close this icon.
`menu_model` and `icon_state` are pure functions of what the server said (smoke tests them without a desktop); `run`
turns them into pystray objects. After the server quits (its heartbeat says "quit"), the icon goes too.
"""
import json
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

from . import platform

ROOT = Path(__file__).resolve().parents[2]
OWNER = {"cover": "frontman", "presep": "frontman", "auto": "frontman", "install": "frontman", "speech": "speakeasy",
         "addon": "safehouse", "move": "safehouse", "doctor": "safehouse", "ytdlp": "safehouse"}   # else DoubleAgent
COLOURS = {"frontman": "#bf9760", "doubleagent": "#b98893", "speakeasy": "#8aa9c0", "safehouse": "#a4a7a9"}
DEEP = {"frontman": "#7a4f1a", "doubleagent": "#66293a", "speakeasy": "#1f3b54", "safehouse": "#35383b"}
BARS = ("frontman", "doubleagent", "speakeasy", "safehouse")
WORDS = {"cover": "Making a cover", "presep": "Separating a song", "auto": "One-shot", "install": "Installing a voice",
         "speech": "Speaking", "train": "Training", "train-prepare": "Preparing clips", "train-auto": "Making a voice",
         "train-export": "Adding a voice", "train-transcribe": "Transcripts", "cadence-ab": "Listening copies",
         "night": "Voice work", "addon": "Installing an add-on", "move": "Moving files", "doctor": "Check-up",
         "ytdlp": "YouTube downloader"}


# ---------------------------------------------------------------- the model (pure)

def _left(j: dict) -> str:
    s = j.get("total_left") if j.get("total_left") is not None else j.get("eta")
    if s is None:
        return ""
    s = float(s)
    return "almost done" if s < 5 else f"about {round(s / 60)} min left" if s >= 90 else f"about {max(1, round(s))} s left"


def job_line(j: dict) -> str:
    """One running job as a short line: "Training Morgan Freeman · epoch 120 of 400 · about 30 min left"."""
    what = j.get("title") or WORDS.get(j.get("kind"), "Working")
    parts = [what]
    if j.get("epoch") and j.get("epochs"):
        parts.append(f"epoch {j['epoch']} of {j['epochs']}")
    if _left(j):
        parts.append(_left(j))
    return " · ".join(parts)[:120]


def running(jobs: list | None) -> list[dict]:
    return [j for j in jobs or [] if not j.get("final") and j.get("state") not in ("done", "error", "cancelled", "paused")]


def icon_state(status: dict | None, jobs: list | None = None, doctor_bad: bool = False) -> tuple[str, str | None]:
    """(stopped | problem | working | idle, the module whose bar is lit while working)."""
    if status is None:
        return "stopped", None
    if status.get("gpu_problem") or doctor_bad:
        return "problem", None
    run = running(jobs)
    if run:
        return "working", OWNER.get(run[0].get("kind"), "doubleagent")
    return "idle", None


def tooltip(status: dict | None, jobs: list | None = None, doctor_bad: bool = False) -> str:
    state, _ = icon_state(status, jobs, doctor_bad)
    if state == "stopped":
        return "UnderCover isn't running"
    if state == "problem":
        return "UnderCover · " + ("the graphics card has a problem" if status.get("gpu_problem") else "the Doctor found a problem")
    run = running(jobs)
    if run:
        return ("UnderCover · " + job_line(run[0]) + (f" (+{len(run) - 1} more)" if len(run) > 1 else ""))[:127]
    return "UnderCover · ready"


def game_question(game: dict | None) -> str | None:
    """The game pause's question as one line (None when nothing is being asked)."""
    a = (game or {}).get("asking")
    if not a:
        return None
    return f"{a.get('title') or a.get('exe')} is full-screen: pause UnderCover while you play?"


def menu_model(status: dict | None, jobs: list | None = None, phone_on: bool | None = None,
               startup_on: bool | None = None, game: dict | None = None) -> list[dict]:
    """The menu as data: [{"id", "text", "enabled", "checked", "default"}] (separators: {"id": "-"}). While the game
    pause asks, its question and three answers come first."""
    if status is None:
        return [{"id": "start", "text": "Start UnderCover", "default": True},
                {"id": "-"},
                {"id": "close", "text": "Close this icon"}]
    items = []
    q = game_question(game)
    if q:
        exe = game["asking"].get("exe") or "this game"
        items += [{"id": "game-q", "text": q[:120], "enabled": False},
                  {"id": "game-pause", "text": "Pause while I play"},
                  {"id": "game-both", "text": "Keep both running"},
                  {"id": "game-never", "text": f"Don't ask for {exe[:60]}"},
                  {"id": "-"}]
    items.append({"id": "open", "text": "Open UnderCover", "default": True})
    run = running(jobs)
    for j in run[:4]:
        items.append({"id": "running", "text": job_line(j), "enabled": False})
    if len(run) > 4:
        items.append({"id": "running", "text": f"and {len(run) - 4} more", "enabled": False})
    if not run:
        items.append({"id": "running", "text": "Nothing running", "enabled": False})
    w = status.get("work") or {}
    items.append({"id": "-"})
    if w.get("held") or w.get("resume"):
        items.append({"id": "resume", "text": "Resume voice work"})
    elif any(j.get("kind") in ("train", "train-prepare", "train-auto", "train-transcribe", "night") for j in run):
        items.append({"id": "pause", "text": "Pause voice work"})
    if phone_on is not None:
        items.append({"id": "phone", "text": "Phone access", "checked": bool(phone_on)})
    items += [{"id": "-"}, {"id": "restart", "text": "Restart"}, {"id": "quit", "text": "Quit UnderCover"}]
    if startup_on is not None:
        items += [{"id": "-"}, {"id": "startup", "text": "Start with Windows", "checked": bool(startup_on)}]
    return items


# ---------------------------------------------------------------- the icon

def draw_icon(state: str, module: str | None = None, size: int = 64):
    """The four-bar mark (the favicon's folder) for a state; drawn at 4x and scaled down."""
    from PIL import Image, ImageDraw
    k = 4
    S = size * k
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    u = S / 22
    X = lambda x: (x - 1) * u   # noqa: E731
    Y = lambda y: (y - 2) * u   # noqa: E731
    folder = [(3, 7.5), (4.5, 6), (9, 6), (10.8, 7.8), (19.5, 7.8), (21, 9.3), (21, 18), (19.5, 19.5), (4.5, 19.5), (3, 18)]
    body = "#8c867c" if state == "stopped" else "#ece7de"
    d.polygon([(X(x), Y(y)) for x, y in folder], fill=body)
    w = 2.4 * u
    for (x, y1), m in zip(((7.5, 14), (10.5, 11.5), (13.5, 13), (16.5, 12.5)), BARS):
        c = "#5a564f" if state == "stopped" else COLOURS[m] if (state == "working" and m == module) else DEEP[m]
        top = 9.5 if state == "working" and m == module else y1   # the working module's bar stands tall
        d.line([(X(x), Y(16.5)), (X(x), Y(top))], fill=c, width=round(w))
        for yy in (16.5, top):
            d.ellipse([X(x) - w / 2, Y(yy) - w / 2, X(x) + w / 2, Y(yy) + w / 2], fill=c)
    if state == "problem":
        r = 4.2 * u
        cx, cy = S - r - u * 0.5, r + u * 0.5
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill="#d0837a", outline="#0a0908", width=round(u * 0.8))
    return img.resize((size, size), Image.LANCZOS)


# ---------------------------------------------------------------- talking to the server

class Server:
    def __init__(self, port: int):
        self.base = f"http://127.0.0.1:{port}"

    def get(self, p: str, timeout: float = 5):
        try:
            with urllib.request.urlopen(self.base + p, timeout=timeout) as r:
                return json.loads(r.read())
        except (OSError, ValueError):
            return None

    def post(self, p: str, body: dict | None = None, timeout: float = 30) -> tuple[bool, str]:
        req = urllib.request.Request(self.base + p, data=json.dumps(body or {}).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "Origin": self.base})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return True, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            try:
                return False, json.loads(e.read()).get("error") or f"refused ({e.code})"
            except ValueError:
                return False, f"refused ({e.code})"
        except OSError:
            return False, "UnderCover isn't answering."


def quit_reason(logs_dir: Path, port: int) -> str | None:
    """The server's last word in its heartbeat: None when it didn't stop on purpose (a crash, or still running), "" for
    a stop without a reason (Ctrl+C), else the reason ("quit", "restart", "window closed", "log off", "shutdown")."""
    from .config import CONFIG
    f = logs_dir / ("heartbeat.jsonl" if port == CONFIG["ui"]["port"] else f"heartbeat-{port}.jsonl")
    try:
        last = json.loads(f.read_text(encoding="utf-8").strip().splitlines()[-1])
    except (OSError, ValueError, IndexError):
        return None
    return (last.get("reason") or "") if last.get("event") == "stop" else None


def leaves_with_server(reason: str | None) -> bool:
    """Does the icon go when the server has gone? Yes after any stop on purpose but a restart; a crash keeps it (grey,
    with Start)."""
    return reason is not None and reason != "restart"


def launcher() -> Path:
    """What "Start with Windows" points at: the installer's UnderCover.cmd, or the repo's UnderCover.bat."""
    for name in ("UnderCover.cmd", "UnderCover.bat"):
        if (ROOT / name).exists():
            return ROOT / name
    return ROOT / "UnderCover.bat"


# ---------------------------------------------------------------- the icon process

def run(port: int):
    import pystray
    from .config import path
    srv = Server(port)
    st = {"status": None, "jobs": [], "phone": None, "phone_at": 0.0, "doctor_bad": False, "doctor_at": 0.0,
          "down_since": None, "startup": None, "game": None, "asked_at": None}
    lock = threading.Lock()   # one icon / menu update at a time (the poll and the actions both update)
    try:
        st["startup"] = platform.startup_entry_state()
    except platform.NotOnThisOS:
        st["startup"] = None

    def refresh():
        st["status"] = srv.get("/api/status")
        if st["status"] is None:
            st["jobs"], st["phone"] = [], None
            st["down_since"] = st["down_since"] or time.time()
            return
        st["down_since"] = None
        st["jobs"] = srv.get("/api/jobs") or []
        game = srv.get("/api/gamepause")
        st["game"] = game if isinstance(game, dict) else None
        a = (st["game"] or {}).get("asking")
        if a and a.get("since") != st["asked_at"]:   # a new question: one balloon (Windows holds it back in an
            st["asked_at"] = a.get("since")          # exclusive full-screen game; the menu and the phone still ask)
            say(f"{game_question(st['game'])} Choose in this icon's menu, or on your phone.")
        if time.time() - st["phone_at"] > 60:   # the tick changes only when someone flips it (the action re-reads it)
            ph = srv.get("/api/phone")
            st["phone"] = ph.get("enabled") if isinstance(ph, dict) else None
            st["phone_at"] = time.time()
        if time.time() - st["doctor_at"] > 300:
            d = srv.get("/api/doctor", timeout=15)
            st["doctor_bad"] = bool(d and (d.get("counts") or {}).get("bad"))
            st["doctor_at"] = time.time()

    def say(msg: str):
        try:
            icon.notify(msg[:250], "UnderCover")
        except Exception:   # noqa: BLE001 - notifications are a nicety
            pass

    def act(item_id: str):
        def go(icon_, _item=None):
            if item_id == "open":
                webbrowser.open(srv.base + "/")
            elif item_id in ("pause", "resume"):
                ok, msg = srv.post(f"/api/work/{item_id}")
                if not ok:
                    say(msg)
            elif item_id in ("game-pause", "game-both", "game-never"):
                ok, msg = srv.post("/api/gamepause", {"answer": item_id.split("-", 1)[1]})
                if not ok:
                    say(msg)
            elif item_id == "phone":
                ok, msg = srv.post("/api/phone", {"on": not st["phone"]})
                st["phone_at"] = 0.0
                if not ok:
                    say(msg)
            elif item_id == "restart":
                ok, msg = srv.post("/api/restart")
                say("Restarting UnderCover…" if ok else msg)
            elif item_id == "quit":
                ok, msg = srv.post("/api/quit")
                if not ok:
                    return say(msg)
                say("UnderCover is stopping…")
            elif item_id == "startup":
                try:
                    st["startup"] = platform.startup_entry(not st["startup"], launcher(),
                                                           icon=ROOT / "undercover" / "web" / "icon.ico")
                except (OSError, platform.NotOnThisOS) as e:
                    say(str(e))
            elif item_id == "start":
                platform.start_detached([platform.windowless_python(), "-m", "undercover.core.relaunch", "0"], cwd=ROOT)
                say("Starting UnderCover…")
            elif item_id == "close":
                icon_.stop()
            threading.Thread(target=lambda: (refresh(), update()), daemon=True).start()
        return go

    def items():
        model = menu_model(st["status"], st["jobs"], st["phone"], st["startup"], st["game"])
        out = []
        for it in model:
            if it["id"] == "-":
                out.append(pystray.Menu.SEPARATOR)
                continue
            kw = {"enabled": it.get("enabled", True), "default": it.get("default", False)}
            if "checked" in it:
                kw["checked"] = (lambda _i, v=it["checked"]: v)
            out.append(pystray.MenuItem(it["text"], act(it["id"]), **kw))
        return out

    icons = {}

    def update():
        with lock:
            state, module = icon_state(st["status"], st["jobs"], st["doctor_bad"])
            key = (state, module)
            if key not in icons:
                icons[key] = draw_icon(state, module)
            icon.icon = icons[key]
            icon.title = tooltip(st["status"], st["jobs"], st["doctor_bad"])
            icon.update_menu()

    def loop():
        while True:
            refresh()
            update()
            if (st["down_since"] and time.time() - st["down_since"] > 10
                    and leaves_with_server(quit_reason(path("logs_dir"), port))):
                icon.stop()   # UnderCover was stopped on purpose (Quit, its window closed, Ctrl+C): the icon goes with it
                return
            time.sleep(5)

    icon = pystray.Icon("UnderCover", draw_icon("stopped"), "UnderCover", menu=pystray.Menu(items))

    def setup(icon_):
        icon_.visible = True
        threading.Thread(target=loop, daemon=True, name="tray-poll").start()
    icon.run(setup)


def main(argv=None):
    import sys
    from .config import CONFIG
    argv = list(sys.argv[1:] if argv is None else argv)
    port = int(argv[argv.index("--port") + 1]) if "--port" in argv else CONFIG["ui"]["port"]
    ok, why = platform.supports("tray")
    if not ok:
        print(f"No tray icon: {why}")
        return 0
    if not platform.single_instance(f"UnderCover-tray-{port}"):
        return 0   # one is showing already
    run(port)
    return 0
