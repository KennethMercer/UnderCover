"use strict";
const aSong = document.getElementById("a-song");   // the song on the desk's player (with drawSongWave, below): first, since a theme redraw at load can reach it
// UnderCover web UI. Talks to undercover/server.py (JSON API + Server-Sent Events).

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
// A voice's Remove sets it aside (DELETE /api/voices/<v> -> {ok, aside, with}): say so, with the way back and where to delete it for good
function removedVoice(name, r) {
  const who = voiceDisplay(name);
  if (!r?.aside) return;
  showError(`${who} is set aside, not deleted${r.with ? `, with the retrained one that waited beside it` : ""}. To delete it for good, use SafeHouse > Storage > Set aside.`,
            "Removed", { label: "Bring it back", run: () => asideRestore(r.aside) }, true);
}
// "Click again to confirm" (one helper for every destructive two-click button): the second click counts only once the first has stood for
// CONFIRM_MS, so a double click (two clicks 100 ms apart on a button that turns into "Confirm") can never confirm by accident (IA P5-1)
const CONFIRM_MS = 600;
const arm = el => { el.dataset.armedAt = Date.now(); };
const disarm = el => { delete el.dataset.armedAt; };
const armedFor = el => { const t = +el.dataset.armedAt; return t > 0 && Date.now() - t >= CONFIRM_MS; };   // true only for a deliberate second click
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const paceWord = x => x.pace ? x.pace[0].toUpperCase() + x.pace.slice(1) : `${(+x.speed).toFixed(1)}×`;
// ------------------------------------------------------------ the line under a name, as chips (rc-ui1: the user's option A, the maintainers' mockup, meta-options)
// facts([...]) -> one soft pill per fact, in plain words. A fact is a string, or {t, html, muted, deep, title, cls}: muted for the least important one, cls a state (ok / warn / bad),
// deep for Deep Cover's (the key, the exact time, the sample rate, LUFS). Empty facts drop out; nothing at all gives "".
function facts(list) {
  const out = list.map(f => typeof f === "string" ? { t: f } : f).filter(f => f && (f.t || f.html))
    .map(f => `<span class="fact${f.muted ? " muted" : ""}${f.cls ? " " + f.cls : ""}"${f.deep ? " data-deep" : ""}${f.title ? ` title="${esc(f.title)}"` : ""}>${f.html ?? esc(f.t)}</span>`);
  return out.length ? `<span class="facts">${out.join("")}</span>` : "";
}
const dateOf = s => { if (!s) return null; const d = new Date(typeof s === "number" ? s * 1000 : String(s).replace(" ", "T")); return isNaN(d) ? null : d; };
// "today", "yesterday" or a date ("12 Oct", with the year when it isn't this one): no clock time, which is Deep Cover's (dayTime)
function dayWord(s) {
  const d = dateOf(s); if (!d) return "";
  const day = x => new Date(x.getFullYear(), x.getMonth(), x.getDate()), n = Math.round((day(new Date()) - day(d)) / 864e5);
  return n < 1 ? "today" : n < 2 ? "yesterday"
    : d.toLocaleDateString(undefined, { day: "numeric", month: "short", ...(d.getFullYear() === new Date().getFullYear() ? {} : { year: "numeric" }) });
}
const dayTime = s => { const d = dateOf(s); return d ? d.toLocaleString(undefined, { day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" }) : ""; };
const upFirst = t => t ? t[0].toUpperCase() + t.slice(1) : t;
// "a" or "an" by the sound the word starts with, not its letter (1.0.1: "an unique"): a "you"/"w" sound takes "a"
// (unique, European, one), a silent h "an" (honest, hour), letters read one by one go by the letter's name (an MJ, an
// R&B, a UK), and numbers by how they're said (an 80s, an 11, a 1980s)
function aOrAn(word) {
  const w = String(word || "").trim().replace(/^[^a-z0-9]+/i, "");
  if (!w) return "a";
  if (/^\d/.test(w)) return /^(8|1[18](\D|$))/.test(w) ? "an" : "a";
  const lead = w.match(/^[A-Z&]{1,4}(?=$|[^a-z])/)?.[0];   // initials (MJ, R&B, UK, EDM): letter by letter
  if (lead && (lead.length <= 3 || !/[AEIOU]/.test(lead))) return /^[AEFHILMNORSX]/.test(lead) ? "an" : "a";
  const l = w.toLowerCase();
  if (/^(hour|honest|honou?r|heir|herb)/.test(l)) return "an";
  if (/^(uni(?![nm])|us[aeu]|ut[eio]|uk|ur[aeiou]|eu|ewe|one\b|once|ouija)/.test(l)) return "a";
  return /^[aeiou]/.test(l) ? "an" : "a";
}
// "No covers yet" / "1 cover" / "3 covers": never "0 covers"
const countWord = (n, one, many = one + "s") => n ? `${n} ${n === 1 ? one : many}` : `No ${many} yet`;
// a song's source in plain words: "Your file" or "From YouTube" (the server says "dropped file" / "YouTube")
const sourceWord = s => /youtube/i.test(s || "") ? "From YouTube" : s ? "Your file" : "";
const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} },
};

// The first time a real person's voice is about to be made, one calm line, once (consult 6): on the one-shot's plan when it will train, and with
// DoubleAgent's first Train press.
const PERSON_WARN = "For personal use: don't pass the result off as the real person.";
const personWarned = () => !!store.get("uc-person-warned", false);
// Deep Cover (ARCHITECTURE "UI"): expert-only elements carry data-deep and stay hidden unless body.deep. The
// switch is remembered (localStorage uc-deep); a Settings room will carry it too.
// The quick switch in the top bar (#deep-toggle, phase 5) and typing "deepcover" both flip it.
function setDeep(on) {
  document.body.classList.toggle("deep", on); store.set("uc-deep", on);
  $("#deep-toggle")?.setAttribute("aria-pressed", on);
  // the first flip (and only the first) gets one line under the top bar (consult 8): the page doubles in a frame, and this says what happened
  const first = $("#deep-first");
  if (first) { if (on && !store.get("uc-deep-seen", false)) first.hidden = false; else first.hidden = true; }
  $("#set-deep")?.setAttribute("aria-checked", on);   // the switch in SafeHouse's Settings room
}
setDeep(!!store.get("uc-deep", false));
$("#deep-toggle")?.addEventListener("click", () => setDeep(!document.body.classList.contains("deep")));
$("#deep-first-ok")?.addEventListener("click", () => { store.set("uc-deep-seen", true); $("#deep-first").hidden = true; });
$("#set-deep")?.addEventListener("click", () => setDeep(!document.body.classList.contains("deep")));
let deepTyped = "";

// Voice display names (ARCHITECTURE "UI": voice display names). Wherever a voice is listed it shows the person's
// name, never its folder id; voices trained here carry a small voiceprint (CSS on .vname[data-trained="1"]). Every voice
// list the page loads goes through noteVoices(); the backend adds `display` and `trained` to the voice APIs, and until
// it does the id shows and the "-uc" ending marks a voice trained in UnderCover.
const VOICES = new Map();   // voice id -> { display, trained }
function noteVoices(list) {
  for (const v of list || []) {
    const id = v.name || v.voice; if (!id) continue;
    const was = VOICES.get(id) || {};
    VOICES.set(id, { display: v.display ?? was.display, trained: v.trained ?? v.extra?.trained ?? was.trained });
  }
}
const voiceDisplay = id => VOICES.get(id)?.display || id || "";
const voiceTrained = id => { const t = VOICES.get(id)?.trained; return t != null ? !!t : /-uc$/.test(id || ""); };
// sr: the screen-reader-only "(trained in UnderCover)"; lines that already say "trained here" next to the name pass false so it isn't read twice
const vname = (id, sr = true) => id ? `<span class="vname" data-voice="${esc(id)}"${voiceTrained(id) ? ' data-trained="1" title="Trained in UnderCover"' : ""}>${esc(voiceDisplay(id))}${sr && voiceTrained(id) ? '<span class="sr-only"> (trained in UnderCover)</span>' : ""}</span>` : "";
// How like the person a voice sounds, in a word and its dot (the server's likeness_word, on every voice API: "great" |
// "good" | "fair" | "poor"; poor = a downloaded voice the check judged bad or broken; null = not measured, so nothing).
// The page never sets the cut-offs. The word sits in the sentence's own font; the dot alone is the shorthand in lists.
const LIKE_WORDS = ["great", "good", "fair", "poor"];
const likeWord = (w, after = "") => LIKE_WORDS.includes(w) ? `<span class="like ${w}">${w}</span>${after ? " " + after : ""}` : "";
const likeDot = w => LIKE_WORDS.includes(w) ? ` <span class="like dot ${w}" role="img" aria-label="${w} likeness" title="${w} likeness"></span>` : "";
const initialOf = s => (String(s || "").replace(/^[^a-z0-9]+/i, "")[0] || "?").toUpperCase();
const vinitial = id => esc(initialOf(voiceDisplay(id)));
document.addEventListener("keydown", e => {
  if (e.target.closest("input, textarea, select, [contenteditable]") || e.ctrlKey || e.metaKey || e.altKey || e.key.length !== 1) return;
  deepTyped = (deepTyped + e.key.toLowerCase()).slice(-9);
  if (deepTyped === "deepcover") { deepTyped = ""; setDeep(!document.body.classList.contains("deep")); }
});

const S = {
  song: null, voice: store.get("uc-voice", null), voices: [], defaults: {}, opts: {},
  deskVoice: false, buildOpen: null, deskWas: false,   // the build path: open while something is on its desk (a song, a voice picked by hand), or by the link
  job: null, es: null, estimate: null, result: null, online: [], searchSeq: 0,
};

// ------------------------------------------------------------ api helpers
async function api(path, opts = {}) {
  const init = { ...opts, headers: { ...(opts.headers || {}) } };
  if (opts.json !== undefined) { init.method = init.method || "POST"; init.body = JSON.stringify(opts.json); init.headers["Content-Type"] = "application/json"; }
  let r;
  try { r = await fetch(path, init); }
  catch (e) { throw new Error("Lost contact with UnderCover. Is the UnderCover window still open? Start it again with UnderCover.bat."); }
  if (r.status === 401) { location.replace("/pair"); throw new Error("This phone isn't paired any more."); }   // only phones get 401 (phase 11)
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw Object.assign(new Error(data.error || `Request failed (${r.status}).`), { status: r.status, data });   // the body rides along (a 409 can say more)
  return data;
}
// action: optional { label, run } shown as a button in the banner; info: accent stripe instead of red
// No command lines in the page (IA P8-7): "... Search for one with: python -m undercover.core.voices search x" is for developers, so the sentence goes.
const plainMsg = m => String(m ?? "").replace(/\s*(?:Search for one with|Run|Try|Use):?\s*(?:python|py|pip)\s+-m\s.*$/i, "").trim();
function showError(msg, title = "Something went wrong", action = null, info = false) {
  msg = plainMsg(msg);
  if (/^One is already waiting/.test(msg || "")) title = "One is already waiting";   // a retrained voice waits to be heard first
  $("#err-title").textContent = title; $("#err-msg").textContent = msg; $("#err").hidden = false;
  $("#err").classList.toggle("info", info);
  clearTimeout(showError.t);   // an info note says what just happened: it goes after a while (IA P8-13: "Added to tonight" stayed up for half an hour); a real error stays
  if (info && !action) showError.t = setTimeout(() => { if ($("#err").classList.contains("info") && $("#err-msg").textContent === msg) $("#err").hidden = true; }, 15000);
  const b = $("#err-action");
  b.hidden = !action; b.disabled = false;
  if (action) { b.textContent = action.label; b.onclick = action.run; }
  $("#err").scrollIntoView({ block: "nearest", behavior: "smooth" });
}
$("#err-x").onclick = () => { $("#err").hidden = true; };

const fmtTime = s => { s = Math.max(0, Math.round(s || 0)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; };
const fmtSecs = s => s >= 90 ? `${Math.round(s / 60)} min` : `${Math.max(1, Math.round(s))} s`;
// the one way every bar says how long is left (phase 4c): "about 3 min left", "about 40 s left", "almost done"
const leftText = s => s == null ? "" : s < 5 ? "almost done" : `about ${fmtSecs(s)} left`;
// the job's said time left counted down since it said it (the server's jobs.counted_down: the strip and the stage panel
// read the same): a said 5 s or more holds at 5, so "almost done" comes only when the job says so (rc-fix4)
const countedDown = (said, sinceS) => said == null ? null : said < 5 ? Math.max(0, said - sinceS) : Math.max(5, said - sinceS);
// a job event as one line: what it's doing (the step, "n of m"), and the time left; a queued job says what it waits for
function progressSay(ev) {
  const base = ev.label || ev.message || "";
  if (!base) return "";
  const step = ev.steps > 1 && ev.step_n && !/\d+ of \d+/.test(base) ? ` (step ${ev.step_n} of ${ev.steps})` : "";
  const waiting = ev.stage === "preparing" || ev.state === "queued";
  const left = ev.total_left != null && ev.total_left > (waiting ? 5 : 0) ? ` · ${leftText(ev.total_left)}` : "";
  return base + step + left;
}
const fmtPitch = v => { v = +v; return (v > 0 ? "+" : v < 0 ? "−" : "±") + Math.abs(v); };
const fmtMB = b => b ? `${Math.round(b / 1e6)} MB` : "";

// ------------------------------------------------------------ progress in the browser tab title
// Long jobs (a training is ~45 min) are rarely watched: the tab says how far along it is, then "✓ Done" until seen.
function tabTitle(text) {
  document.title = text ? `${text} · UnderCover` : "UnderCover";
  if (text && text.startsWith("✓") && !document.hidden) setTimeout(() => { if (document.title.startsWith("✓")) document.title = "UnderCover"; }, 4000);
}
document.addEventListener("visibilitychange", () => { if (!document.hidden && document.title.startsWith("✓")) setTimeout(() => tabTitle(null), 4000); });

// ------------------------------------------------------------ status + theme
let pageBuild = null;
// the game pause (core/gamepause.py): while a full-screen game is up and UnderCover has GPU work, the question shows on
// every open page, a paired phone's too (in an exclusive full-screen game the phone is the one place it's seen)
function gameAsk(a) {
  const box = $("#gp-ask");
  box.hidden = !a;
  if (!a) return;
  const low = typeof LP !== "undefined" && LP.known;   // with Low profile there (lowprofile.js), it's the first answer
  $("#gp-q").textContent = `${a.title || a.exe} is running while ${a.reason || "UnderCover is working on the graphics card"}. ` +
    (low ? "Low profile while you play? UnderCover keeps to half the graphics card and works in bursts." : "Pause UnderCover while you play?");
  $("#gp-low").hidden = !low;
  $("#gp-never").textContent = `Don't ask for ${a.exe}`;
}
for (const [id, answer] of [["gp-low", "low"], ["gp-pause", "pause"], ["gp-both", "both"], ["gp-never", "never"]]) {
  $("#" + id).onclick = async () => {
    try { await api("/api/gamepause", { json: { answer } }); $("#gp-ask").hidden = true; }
    catch (e) { showError(e.message, "Couldn't answer the game question"); }
  };
}
$("#uc-updated-go").onclick = () => location.reload();
async function pollStatus() {
  try {
    const s = await api("/api/status");
    document.body.dataset.gpu = s.gpu_problem ? "bad" : s.gpu ? "ok" : s.cpu_mode ? "cpu" : "wait";
    // phase 11b: this tab remembers the server build it loaded with; a different one means the page is older than the
    // server (an update and a restart while the tab stayed open): one quiet line offers a reload (#uc-updated)
    if (s.build && !pageBuild) pageBuild = s.build;
    else if (s.build && s.build !== pageBuild) $("#uc-updated").hidden = false;
    document.body.dataset.client = s.client || "pc";   // "phone": a paired phone (phase 11); [data-pc] controls hide
    $("#set-away-me-row").hidden = !s.away_ready || location.protocol === "https:";   // phase 20: ready on the PC
    if (s.device) $("#set-phone-me").textContent = `This phone is paired as ${s.device}. Phone access is managed on the PC.`;
    sysStatus(s);
    if (typeof renderLowProfile === "function") renderLowProfile(s.low_profile, s);
    if (typeof tourStatus === "function") tourStatus(s.tour);   // the First assignment's tour: a fresh install's first visit to FrontMan (firstrun.js)   // the top bar's Low profile (lowprofile.js; hidden until the backend has it)
    if (typeof firstReady === "function") firstReady(s);   // the first start's download in the one-shot card (oneshot.js)
    if (s.stale) restartNeeded();
    if (s.last_abrupt && store.get("uc-abrupt-seen", null) !== s.last_abrupt.at) {   // once per incident
      store.set("uc-abrupt-seen", s.last_abrupt.at);
      showError(`Last time, UnderCover stopped suddenly at ${s.last_abrupt.at} while doing: ${s.last_abrupt.during}. That usually means the PC lost power or froze. ` +
                "An unfinished training can be resumed in DoubleAgent; logs/heartbeat.jsonl has the details.", "UnderCover didn't shut down cleanly", null, true);
    }
    const tr = $("#chip-train"), t = s.training;   // training is visible from any tab
    tr.hidden = !t;
    if (t) tr.innerHTML = `<i class="dot wait"></i><span>Training ${esc(t.project)}${t.eta ? ` · ${fmtSecs(t.eta)} left` : t.epoch ? ` · ${t.epoch}/${t.epochs}` : ""}</span>`;
    if (typeof renderWorkBar === "function") renderWorkBar(s.work);   // work.js: Pause / Resume bar (the tab title: jobs.js)
    gameAsk(s.work && s.work.game_ask);
    if (s.models.startsWith("error")) { showError(s.models.replace(/^error: /, ""), s.gpu_problem ? "GPU not available" : "Models failed to load"); return; }
    S.modelsReady = s.models === "ready"; S.firstRun = !!s.first_run;
    setTimeout(pollStatus, s.models === "ready" ? 5000 : 1000);
  } catch (e) {
    document.body.dataset.gpu = "bad";
    sysStatus(null);
    setTimeout(pollStatus, 3000);
  }
}
// SafeHouse's System room: the graphics card and the models in words, a GPU problem as an alert at the top of the room
// the first start's download in words: "about 1.3 GB" from the status's models_mb (the backend's measured total), "about 1.3 GB" before it says
const modelsSize = s => { const mb = +(s && s.models_mb) || 1283; return mb >= 950 ? `about ${(mb / 1024).toFixed(1)} GB` : `about ${Math.round(mb / 50) * 50} MB`; };
function sysStatus(s) {
  const row = (id, text, st, word) => {
    $(`#sys-${id}`).textContent = text;
    $(`#sys-${id}-st`).className = `dotw ${st}`; $(`#sys-${id}-st`).textContent = word;
  };
  if (!s) { row("gpu", "UnderCover isn't answering", "bad", "not connected"); row("models", "", "bad", "not connected"); return; }
  if (s.gpu) row("gpu", `${s.gpu} · ${s.vram_gb} GB`, "ok", "ready");
  else if (s.cpu_mode || s.gpu_problem) row("gpu", "Not found · processor only", "bad", "problem");
  else row("gpu", "Checking…", "now", "checking");
  if (s.models === "ready") row("models", s.queue ? `${s.queue} cover${s.queue > 1 ? "s" : ""} waiting their turn` : s.gpu ? "Separation, voice engine and speech are warm" : "Loaded on the processor",
                               s.gpu ? "ok" : "warn", s.gpu ? "warm" : "slow");
  else if (s.models.startsWith("error")) row("models", s.models.replace(/^error: /, ""), "bad", "problem");
  else row("models", s.first_run ? `Downloading missing AI models (${modelsSize(s)}${s.models_progress != null ? `, ${Math.round(s.models_progress * 100)}%` : ""})` : "Loading…", "now", "loading");
  const bad = !!(s.gpu_problem || s.cpu_mode);
  $("#sys-alert").hidden = !bad;
  if (bad) $("#sys-alert-p").textContent = s.gpu_problem ||
    "UnderCover is using the processor, so covers are slow and training is off. If this PC has an AMD card, restarting usually fixes it; if not, the driver needs a look.";
}
// Restart (System room, its GPU alert): the dialog says what happens and holds the button that does it
$$("[data-restart]").forEach(b => b.onclick = () => showError("Restarting takes about 10 seconds; your songs, voices and history are kept. Voice work pauses and carries on after.",
  "Restart UnderCover?", { label: "Restart now", run: () => restartServer() }, true));
let restartShown = false;
function restartNeeded() {
  if (restartShown) return;
  restartShown = true;
  showError("UnderCover was updated while it was running, so some settings won't take effect until it restarts. " +
            "Restarting takes about 10 seconds; your songs, voices and history are kept.", "Restart needed",
            { label: "Restart now", run: () => restartServer() }, true);
}
// now = true: stop the running work and restart at once (the server offers it, in a 409, while work runs)
async function restartServer(now = false) {
  now = now === true;
  const btn = $("#err-action");
  btn.disabled = true; btn.textContent = "Restarting…";
  let before = null;
  try { before = (await api("/api/status")).started; } catch (e) {}
  let pausing = false;
  try { pausing = !!(await api("/api/restart", now ? { json: { now: true } } : { method: "POST" })).pausing; }
  catch (e) {
    if (e.status === 409 && e.data?.now_ok) {   // work is running: wait for it, or stop it and restart now
      const what = (e.data.busy || []).join(", ");
      return showError(`${what ? `Running now: ${what}. ` : ""}${e.message}`, "Work is running",
                       { label: "Restart now (stops running work)", run: () => restartServer(true) }, false);
    }
    // A server from before this feature has no restart endpoint (404); anything else (e.g. busy) is explained by e.message.
    const old = /not found/i.test(e.message);
    showError(old ? "This copy of UnderCover is too old to restart itself. Start it again with UnderCover.bat or the desktop shortcut." : e.message,
              "Couldn't restart", old ? null : { label: "Try again", run: () => restartServer() }, !old);
    return;
  }
  $("#err-msg").textContent = pausing
    ? "Pausing the voice work (training keeps its place), restarting, then carrying on where it was. The page will reload by itself."
    : "Restarting UnderCover and loading the models. The page will reload by itself.";
  const t0 = Date.now();
  while (Date.now() - t0 < (pausing ? 300000 : 90000)) {
    await new Promise(r => setTimeout(r, 1000));
    try {
      // timeout: a request caught by the old process as it exits can otherwise hang forever
      const ctl = new AbortController(), timer = setTimeout(() => ctl.abort(), 3000);
      const s = await (await fetch("/api/status", { cache: "no-store", signal: ctl.signal })).json();
      clearTimeout(timer);
      if (s.started && s.started !== before) { location.reload(); return; }
    } catch (e) { /* down or timed out while it restarts */ }
  }
  showError("UnderCover didn't come back after restarting. Start it again with UnderCover.bat or the desktop shortcut. " +
            "If that fails, logs/server.log has the details.", "Restart failed");
}
// Look (SafeHouse's Settings room): follow Windows (the default), or always dark / light. uc-theme = "system" | "dark" | "light",
// read before first paint by the script in index.html's head
const lightQ = matchMedia("(prefers-color-scheme: light)");
function themePref() { try { return localStorage.getItem("uc-theme") || "system"; } catch (e) { return "system"; } }
function applyTheme() {
  const t = themePref();
  if (t === "light" || (t === "system" && lightQ.matches)) document.documentElement.dataset.theme = "light";
  else delete document.documentElement.dataset.theme;
  $$("#set-look button").forEach(b => b.setAttribute("aria-pressed", b.dataset.v === t));
  redrawAll();
  document.dispatchEvent(new Event("uc:themechange"));   // themes.js redraws its swatch cards for the mode
}
lightQ.addEventListener("change", () => { if (themePref() === "system") applyTheme(); });
$$("#set-look button").forEach(b => b.onclick = () => { try { localStorage.setItem("uc-theme", b.dataset.v); } catch (e) {} applyTheme(); });
$("#chip-train").onclick = () => { if (window.showTab) showTab("doubleagent"); };

// ------------------------------------------------------------ waveforms
function drawWave(canvas, peaks, played = 0, colorVar = "--wave-on") {
  const dpr = window.devicePixelRatio || 1, w = canvas.clientWidth, h = canvas.clientHeight;
  if (!w) return;
  canvas.width = w * dpr; canvas.height = h * dpr;
  const g = canvas.getContext("2d"); g.scale(dpr, dpr);
  const css = getComputedStyle(document.documentElement);
  const off = css.getPropertyValue("--wave").trim(), on = css.getPropertyValue(colorVar).trim();
  if (!peaks || !peaks.length) { g.fillStyle = off; g.fillRect(0, h / 2 - 1, w, 2); return; }
  const bars = Math.max(1, Math.floor(w / 3));
  for (let i = 0; i < bars; i++) {
    const a = Math.floor(i / bars * peaks.length), b = Math.max(a + 1, Math.floor((i + 1) / bars * peaks.length));
    let v = 0; for (let j = a; j < b; j++) v = Math.max(v, peaks[j]);
    v = Math.max(0.04, v);
    g.fillStyle = i / bars < played ? on : off;
    g.fillRect(i * 3, h / 2 - v * h / 2, 2, v * h);
  }
}
async function peaksFromUrl(url, bins = 1200) {
  const buf = await (await fetch(url)).arrayBuffer();
  const ctx = new (window.OfflineAudioContext || window.webkitOfflineAudioContext)(1, 2, 44100);
  const audio = await ctx.decodeAudioData(buf);
  const ch = audio.getChannelData(0), step = Math.max(1, Math.floor(ch.length / bins)), out = [];
  let top = 0;
  for (let i = 0; i < bins; i++) {
    let m = 0; const end = Math.min(ch.length, (i + 1) * step);
    for (let j = i * step; j < end; j += 4) { const v = Math.abs(ch[j]); if (v > m) m = v; }
    out.push(m); if (m > top) top = m;
  }
  return out.map(v => v / (top || 1));
}

// ------------------------------------------------------------ song upload
const fileInput = $("#file");
$("#drop").addEventListener("click", e => { if (!S.song || e.target.id === "song-replace") fileInput.click(); });
$("#drop").addEventListener("keydown", e => { if (e.target !== e.currentTarget) return; if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileInput.click(); } });   // a key on a button inside (play, Replace) is the button's
$("#song-replace").onclick = e => { e.stopPropagation(); fileInput.click(); };
fileInput.onchange = () => { if (fileInput.files[0]) upload(fileInput.files[0]); fileInput.value = ""; };

let dragDepth = 0;
addEventListener("dragenter", e => { if (e.dataTransfer?.types?.includes("Files")) { dragDepth++; $("#drop-overlay").hidden = false; } });
addEventListener("dragleave", () => { if (--dragDepth <= 0) { dragDepth = 0; $("#drop-overlay").hidden = true; } });
addEventListener("dragover", e => e.preventDefault());
addEventListener("drop", e => {
  e.preventDefault(); dragDepth = 0; $("#drop-overlay").hidden = true;
  if (!$("#view-train").hidden && window.trainDrop) return window.trainDrop([...(e.dataTransfer?.files || [])]);
  if (!$("#view-speakeasy").hidden && window.speakDrop) return window.speakDrop([...(e.dataTransfer?.files || [])]);
  const f = e.dataTransfer?.files?.[0]; if (!f) return;
  if (/\.(ucvoice|zip|pth)$/i.test(f.name)) installFile(f); else upload(f);
});

async function installFile(file) {
  const name = file.name.replace(/\.(ucvoice|zip|pth)$/i, "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 40) || "voice";
  $("#online-h").hidden = $("#online").hidden = false;
  $("#online-h").textContent = "Installing from file";
  $("#online").innerHTML = `<p class="meta">Installing ${esc(file.name)}…</p>`;
  try {
    const r = await fetch("/api/voices/upload", { method: "POST", body: file,
      headers: { "X-Filename": encodeURIComponent(file.name), "X-Name": encodeURIComponent(name) } });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || "Install failed.");
    $("#online-h").hidden = $("#online").hidden = true;
    await loadVoices(data.voice);
  } catch (e) {
    $("#online-h").hidden = $("#online").hidden = true;
    showError(e.message, "That voice couldn't be installed");
  }
}

function setDrop(mode) {
  $("#drop-empty").hidden = mode !== "empty";
  $("#drop-loaded").hidden = mode !== "loaded";
  $("#drop-uploading").hidden = mode !== "uploading";
  $("#drop").classList.toggle("loaded", mode !== "empty");
  $("#drop-or").hidden = mode !== "empty";   // "or use your own file" only above the empty drop zone; a chosen song is just there
}
// separate a new song in the background while a voice is picked (the server only does it when the GPU is idle)
function presep() {
  if (!S.song) return;
  const id = S.song.id;
  api(`/api/songs/${id}/presep`, { json: { options: S.opts } }).then(r => {
    if (!r.started) return;
    const stop = watchJob(r.job, async ev => {
      if (!ev.final) return;
      stop();
      if (ev.state === "done" && S.song?.id === id) {   // show "stems already separated" and the shorter estimate
        try { S.song = await api(`/api/songs/${id}`); renderSong(); } catch (e) {}
        refreshEstimate();
      }
    });
  }).catch(() => {});
}
function upload(file) {
  if (S.job || S.osBusy) return showError("Wait for the current cover to finish (or cancel it) before loading another song.", "Busy");
  setDrop("uploading");
  $("#up-name").textContent = file.name; $("#up-pct").textContent = "0%"; $("#up-bar").style.width = "0%";
  $("#up-note").textContent = "Loading…";
  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/upload");
  xhr.setRequestHeader("X-Filename", encodeURIComponent(file.name));
  xhr.upload.onprogress = e => { if (e.lengthComputable) { const p = Math.round(e.loaded / e.total * 100); $("#up-pct").textContent = p + "%"; $("#up-bar").style.width = p + "%"; if (p === 100) $("#up-note").textContent = "Reading the audio…"; } };
  xhr.onload = () => {
    let data = {}; try { data = JSON.parse(xhr.responseText); } catch (e) {}
    if (xhr.status !== 200) { setDrop(S.song ? "loaded" : "empty"); return showError(data.error || "The upload failed.", "Couldn't load that song"); }
    S.song = data; store.set("uc-song", data.id); renderSong(); refreshEstimate(); presep();
  };
  xhr.onerror = () => { setDrop(S.song ? "loaded" : "empty"); showError("Lost contact with UnderCover while uploading. Is its window still open?", "Couldn't load that song"); };
  xhr.send(file);
}
// The build path (Song / Voice / Convert) is a section of the one-shot card (consult 1): open while something is on its desk, a song loaded or a
// voice picked by hand, and closed by itself when the desk is empty; "or set it up yourself" opens it by hand. No memory: a first visit is empty.
function syncBuild() {
  const build = $("#build"), tog = $("#build-toggle"); if (!build) return;
  const desk = !!S.song || S.deskVoice;
  if (S.deskWas !== desk) S.buildOpen = null;   // the desk changed (a song arrived, or it emptied): the desk decides again; a hand choice holds only while it stays the same
  S.deskWas = desk;
  const open = S.buildOpen != null ? S.buildOpen : desk;
  build.hidden = !open;
  if (open) drawSongWave();   // its waveform gets its width now (the ResizeObserver needs a painted frame)
  tog.setAttribute("aria-expanded", open); tog.innerHTML = open ? "set it up yourself &#9662;" : "or set it up yourself &#8250;";
}
$("#build-toggle")?.addEventListener("click", () => { S.buildOpen = $("#build").hidden; syncBuild(); if (!$("#build").hidden) $("#build").scrollIntoView({ block: "nearest", behavior: "smooth" }); });
function renderSong() {
  previewSync();   // versions made for another song are put away
  syncBuild();
  if (!S.song) return setDrop("empty");
  setDrop("loaded");
  const s = S.song;
  $("#song-name").textContent = s.name;
  // the length is for everyone; the sample rate, channels and loudness (and the key below) are Deep Cover's
  const tech = [`${(s.sample_rate / 1000).toFixed(s.sample_rate % 1000 ? 1 : 0)} kHz ${s.channels === 1 ? "mono" : s.channels === 2 ? "stereo" : s.channels + " ch"}`, `${s.lufs} LUFS`];
  const meta = key => { $("#song-meta").innerHTML = facts([s.sample ? "Sample song" : s.youtube_id || s.source_url ? "From YouTube" : "Your file", fmtTime(s.duration),
    ...tech.map(t => ({ t, deep: true })), key ? { t: `Key ${key}`, deep: true } : ""]); };
  meta("");
  S.songKey = null;
  api(`/api/songs/${s.id}/key`).then(k => {
    if (S.song?.id !== s.id) return;
    S.songKey = k; meta(keyName(k.tonic, k.mode));
    $("#key-o").textContent = keyLabel(S.opts.key || 0);
  }).catch(() => {});
  if (aSong.dataset.song !== s.id) { aSong.pause(); aSong.dataset.song = s.id; aSong.src = s.url || ""; }   // a new song on the desk: its own audio, from the start
  $("#song-play").disabled = !s.url;
  drawSongWave();
  summary();
}

// ------------------------------------------------------------ voices
async function loadVoices(select) {
  S.voices = await api("/api/voices"); noteVoices(S.voices);
  S.voicesLoaded = true;
  if (select) { S.voice = select; S.deskVoice = true; }   // picked by hand (Use in FrontMan, Use these settings): it's on the desk
  if (S.voice && !S.voices.some(v => v.name === S.voice)) S.voice = null;
  if (!S.voice && S.voices.length === 1) S.voice = S.voices[0].name;
  store.set("uc-voice", S.voice);
  renderVoices(); renderBackingSelect(); summary(); refreshEstimate();
}
function voiceDesc(v) {
  const src = (v.source || "").match(/huggingface\.co\/([^/]+\/[^/]+)/);
  return `RVC ${v.version} · ${v.sr ? v.sr / 1000 + " kHz" : "?"}${src ? " · " + src[1].split("/")[1].replace(/_+/g, " ").slice(0, 40) : ""}`;
}
const rmArmed = name => S.rmArm?.name === name && Date.now() - S.rmArm.at < 4000;   // Remove's first click, still standing
function renderVoices() {
  const q = $("#q").value.trim().toLowerCase();
  const isLink = /^https?:\/\//i.test(q);
  const list = isLink ? [] : S.voices.filter(v => !q || v.name.includes(q.replace(/\s+/g, "-")) || v.name.includes(q) || voiceDisplay(v.name).toLowerCase().includes(q))
    .sort((a, b) => (a.likeness_word === "poor") - (b.likeness_word === "poor"));   // the ones that aren't really them sink
  const shownList = typeof earSort === "function" ? earSort(list) : list;   // the ones you've liked rise (ear.js)
  const box = $("#voices");
  const kept = document.activeElement?.dataset?.rm;   // a keyboard user on a Remove button stays on it when the list draws again
  if (!S.voices.length && !q) box.innerHTML = `<div class="empty"><b>No voices yet.</b><button class="btn" type="button" data-goto="doubleagent">Get a voice</button><small class="meta">or search above (try “spongebob”)</small></div>`;
  else if (!list.length && !isLink) box.innerHTML = `<p class="meta">No installed voice matches “${esc(q)}”.</p>`;
  else box.innerHTML = shownList.map(v => `
    <div class="voice pick" role="option" tabindex="0" aria-label="${esc(voiceDisplay(v.name))}" aria-selected="${v.name === S.voice}" data-v="${esc(v.name)}">
      <span class="avatar">${vinitial(v.name)}</span>
      <span class="vtext"><b>${vname(v.name, false)}${likeDot(v.likeness_word)}</b>${facts([...(v.demo ? ["Demo voice", { t: "Not a real person", muted: true }] : [voiceTrained(v.name) ? "Trained here" : "Downloaded"]),
        typeof earHint === "function" && earHint(v) ? { html: earHint(v), muted: true } : "", ...voiceDesc(v).split(" · ").map(t => ({ t, deep: true }))])}</span>
      <span class="vside">${voiceLineButton(v.name)}<button class="remove${rmArmed(v.name) ? " confirm" : ""}"${rmArmed(v.name) ? ` data-armed-at="${S.rmArm.at}"` : ""} data-rm="${esc(v.name)}" aria-label="Remove ${esc(voiceDisplay(v.name))}">${rmArmed(v.name) ? "Confirm remove" : "Remove"}</button><span class="tag" data-deep>${v.index ? "index" : "no index"}</span></span>
    </div>`).join("");
  $$(".voice.pick", box).forEach(el => {
    const pick = () => { S.voice = el.dataset.v; S.deskVoice = true; store.set("uc-voice", S.voice); renderVoices(); summary(); refreshEstimate(); syncBuild(); };
    el.addEventListener("click", e => { if (!e.target.closest(".remove, .vline")) pick(); });
    el.addEventListener("keydown", e => { if (e.target.closest(".remove, .vline")) return; if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(); } });   // Enter on Remove is Remove's own
  });
  bindVoiceLines(box);
  $$(".remove", box).forEach(b => b.onclick = async e => {
    e.stopPropagation();
    if (!b.classList.contains("confirm")) {
      S.rmArm = { name: b.dataset.rm, at: Date.now() };   // kept here, not on the button: the list draws again (a job ends, a thumb lands) and a new button would forget it
      b.classList.add("confirm"); b.textContent = "Confirm remove"; arm(b);
      clearTimeout(S.rmTimer); S.rmTimer = setTimeout(() => { S.rmArm = null; renderVoices(); }, 4000);
      return;
    }
    if (!armedFor(b)) return;   // a double click isn't a confirmation
    clearTimeout(S.rmTimer); S.rmArm = null; b.disabled = true; b.textContent = "Removing…";   // the server can take a few seconds when Windows holds a file
    try { removedVoice(b.dataset.rm, await api(`/api/voices/${encodeURIComponent(b.dataset.rm)}`, { method: "DELETE" })); await loadVoices(); }
    catch (err) { showError(err.message, "Couldn't remove the voice"); renderVoices(); }
  });
  if (kept) box.querySelector(`.remove[data-rm="${CSS.escape(kept)}"]`)?.focus();
  renderOnline(q, isLink);
}

let searchTimer = null;
$("#q").addEventListener("input", () => {
  renderVoices();
  clearTimeout(searchTimer);
  const q = $("#q").value.trim();
  if (q.length >= 3 && !/^https?:\/\//i.test(q)) searchTimer = setTimeout(() => searchOnline(q), 450);
});
// Enter or the Search button: online right away (a link is already offered by the list as it's typed)
function voiceSearchNow() { clearTimeout(searchTimer); const q = $("#q").value.trim(); if (!q) return $("#q").focus(); if (q.length >= 2 && !/^https?:\/\//i.test(q)) searchOnline(q); }
$("#q").addEventListener("keydown", e => { if (e.key === "Enter") voiceSearchNow(); });
$("#q-go").onclick = voiceSearchNow;

async function searchOnline(q) {
  const seq = ++S.searchSeq;
  S.online = null; renderOnline(q.toLowerCase(), false);
  try {
    const res = await api(`/api/search?q=${encodeURIComponent(q)}`);
    if (seq !== S.searchSeq) return;
    S.online = res; S.onlineQ = q.toLowerCase();
  } catch (e) { if (seq === S.searchSeq) { S.online = []; showError(e.message, "Search failed"); } }
  renderOnline($("#q").value.trim().toLowerCase(), false);
}
function renderOnline(q, isLink) {
  const head = $("#online-h"), box = $("#online");
  if (isLink) {
    head.hidden = box.hidden = false; head.textContent = "Install from link";
    const guess = (q.split(/[?#]/)[0].split("/").pop() || "voice").replace(/\.(zip|pth)$/i, "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 40) || "voice";
    box.innerHTML = `<div class="voice"><span class="avatar">↓</span>
      <span class="vtext"><b>Link</b><small class="mono">${esc(q.slice(0, 80))}</small></span>
      <span class="vside"><input class="name-in" id="link-name" value="${esc(guess)}" aria-label="Voice name"><button class="install" id="link-install">Install</button></span></div>`;
    $("#link-install").onclick = () => install($("#link-install"), { url: $("#q").value.trim(), name: $("#link-name").value.trim() });
    return;
  }
  if (q.length < 3) { head.hidden = box.hidden = true; return; }
  head.hidden = box.hidden = false;
  if (S.online === null) { head.textContent = "Searching Hugging Face…"; box.innerHTML = ""; return; }
  if (!S.online || S.onlineQ !== q) { head.textContent = "Press Enter to search online"; box.innerHTML = ""; return; }
  head.textContent = S.online.length ? `Online · Hugging Face · ${S.online.length} results` : "Nothing found online. Try a shorter name.";
  const installedNames = new Set(S.voices.map(v => v.name));
  box.innerHTML = S.online.map(r => `
    <div class="voice"><span class="avatar">${esc(r.name[0]?.toUpperCase() || "?")}</span>
      <span class="vtext"><b>${esc(r.name)}</b>${facts([r.size ? fmtMB(r.size) : "", { t: r.repo, muted: true, title: r.repo }])}${infoChips(r)}</span>
      <span class="vside">${installedNames.has(r.name) ? `<span class="tag">installed</span>` : `<button class="install" data-n="${r.n}">Install</button>`}</span></div>`).join("");
  // send the exact result (not its position): other searches (e.g. the one-shot plan) replace the server's last list
  $$(".install[data-n]", box).forEach(b => b.onclick = () => {
    const r = S.online.find(x => x.n === +b.dataset.n);
    install(b, r ? { url: r.url, name: r.name } : { n: +b.dataset.n });
  });
}

function install(btn, body) {
  btn.disabled = true; btn.textContent = "Starting…";
  api("/api/voices/install", { json: body }).then(({ job }) => {
    const stop = watchJob(job, async ev => {
      // each round named (1.0.1: it counted 0-100 % twice, the second for the index, with no word for it)
      const part = { index: "Index ", file: "Next file " }[ev.part] || "";
      if (ev.stage === "downloading") btn.textContent = ev.frac != null ? `${part}${Math.round(ev.frac * 100)}%` : (ev.message || "").replace("Downloading ", "");
      else if (ev.stage === "preview") btn.textContent = "Preview line…";
      if (ev.final) {
        stop();
        if (ev.state === "done") { btn.textContent = "Installed"; $("#q").value = ""; S.online = []; await loadVoices(ev.voice); }
        else { btn.disabled = false; btn.textContent = "Install"; if (ev.state === "error" || ev.state === "lost") showError(ev.message, "That voice couldn't be installed"); }
      }
    });
  }).catch(e => { btn.disabled = false; btn.textContent = "Install"; showError(e.message, "That voice couldn't be installed"); });
}

// ------------------------------------------------------------ advanced options
const SLIDERS = { key: v => keyLabel(+v),
                  pitch_correction: v => +v ? (+v >= 1 ? "full" : (+v).toFixed(1)) : "off", index_ratio: v => (+v).toFixed(2), protect: v => +v >= 0.5 ? "off" : (+v).toFixed(2),
                  volume_envelope: v => +v <= 0.15 ? "original's" : +v >= 0.85 ? "voice's own" : (+v).toFixed(2),
                  backing_db: v => +v ? `${+v > 0 ? "+" : "−"}${Math.abs(+v)} dB` : "0 dB" };
const SEGS = ["quality", "format", "octave", "backing_octave", "reverb"];
const CHECKS = ["karaoke", "dereverb", "pitch_cleanup", "polish", "fit"];
function loadOpts() {
  const saved = store.get("uc-opts", {});
  delete saved.pitch;  // old vocal-only pitch setting (put the vocal out of key); replaced by key + octave
  if (typeof saved.reverb === "number" && ![0.15, 0.3].includes(saved.reverb)) delete saved.reverb;  // old slider value -> new Auto default
  S.opts = { ...S.defaults, ...saved };
  syncControls();
}
function syncControls() {
  for (const k in SLIDERS) { $("#" + k).value = S.opts[k]; $(`#${k}-o`).textContent = SLIDERS[k](S.opts[k]); }
  for (const k of CHECKS) $("#" + k).checked = !!S.opts[k];
  renderBackingSelect();
  for (const id of SEGS) $$(`#${id} button`).forEach(b => b.setAttribute("aria-pressed", b.dataset.v === String(S.opts[id])));
  fitState();
  summary();
}
// "Fit pitch to the voice": octave and key are chosen per song, so the manual controls step back
function fitState() {
  $$("[data-pitch]").forEach(f => { f.classList.toggle("off", !!S.opts.fit); f.querySelectorAll("button, input").forEach(c => c.disabled = !!S.opts.fit); });
}
function saveOpts() { store.set("uc-opts", S.opts); summary(); refreshEstimate(); }
for (const k in SLIDERS) $("#" + k).addEventListener("input", e => { S.opts[k] = +e.target.value; $(`#${k}-o`).textContent = SLIDERS[k](e.target.value); saveOpts(); });
for (const k of CHECKS) $("#" + k).addEventListener("change", e => { S.opts[k] = e.target.checked; if (k === "fit") fitState(); saveOpts(); });
for (const id of SEGS) $("#" + id).addEventListener("click", e => {
  const b = e.target.closest("button"); if (!b) return;
  $$(`#${id} button`).forEach(x => x.setAttribute("aria-pressed", x === b));
  S.opts[id] = id.endsWith("octave") ? +b.dataset.v : id === "reverb" ? (b.dataset.v === "auto" ? "auto" : +b.dataset.v) : b.dataset.v;
  saveOpts();
});
// Backing vocals: optional second voice. It needs the lead/backing split, so that option is forced on.
function renderBackingSelect() {
  const sel = $("#backing_voice");
  if (S.voicesLoaded && S.opts.backing_voice && !S.voices.some(v => v.name === S.opts.backing_voice)) S.opts.backing_voice = "";
  sel.innerHTML = `<option value="">Original singer</option>` + S.voices.map(v => `<option value="${esc(v.name)}">${esc(voiceDisplay(v.name))}</option>`).join("");
  sel.value = S.opts.backing_voice || "";
  const on = !!S.opts.backing_voice;
  let hear = $("#backing-line");
  if (!hear) { sel.insertAdjacentHTML("afterend", `<span id="backing-line" class="vline-beside"></span>`); hear = $("#backing-line"); }
  hear.innerHTML = on ? voiceLineButton(S.opts.backing_voice) : "";
  bindVoiceLines(hear);
  $("#backing-opts").hidden = !on;
  $("#backing-note").textContent = on
    ? "Lead and backing are split automatically. The backing voice follows one note at a time, so big stacked harmonies come out thinner."
    : "Pick a second voice to sing the backing vocals, or keep the original singer's backing.";
  $("#karaoke").checked = on || !!S.opts.karaoke;
  $("#karaoke").disabled = on;
}
$("#backing_voice").addEventListener("change", e => { S.opts.backing_voice = e.target.value; renderBackingSelect(); saveOpts(); summary(); refreshEstimate(); });
$("#summary").addEventListener("click", e => {   // "Use the original singer's backing": one click to drop a carried-over backing voice
  if (!e.target.closest("#clear-backing")) return;
  S.opts.backing_voice = ""; renderBackingSelect(); saveOpts(); summary(); refreshEstimate();
});
$("#reset").onclick = () => { store.set("uc-opts", {}); loadOpts(); refreshEstimate(); };

function summary() {
  const o = S.opts;
  previewSync();
  $("#adv-sum").textContent = `${o.fit ? "pitch fit to the voice" : pitchText(o)} · likeness ${(+(o.index_ratio ?? 0)).toFixed(2)} · ${o.quality || "high"} quality` +
    (o.backing_voice ? ` · backing by ${voiceDisplay(o.backing_voice)}` : o.karaoke ? " · lead only" : "") + (o.dereverb ? " · de-reverb" : "") + (+o.reverb ? " · reverb" : "") + ` · ${(o.format || "mp3").toUpperCase()}`;
  const ready = S.song && S.voice;
  const busy = !!S.job || !!S.osBusy;   // a one-shot cover uses the GPU queue too
  $("#convert").disabled = !ready || busy;
  $("#preview-btn").disabled = !ready || busy;
  if (S.osBusy) { $("#summary").textContent = "Busy with the one-shot cover at the top of the page…"; return; }
  if (!S.song && !S.voice) $("#summary").textContent = "A song and a voice, and Convert is yours.";
  else if (!S.song) $("#summary").innerHTML = `Voice: <b>${vname(S.voice)}</b>. Now drop a song.`;
  else if (!S.voice) $("#summary").innerHTML = `<b>${esc(S.song.title)}</b>. Now pick a voice.`;
  else {
    const est = S.estimate ? ` · about ${fmtSecs(S.estimate.total)}${S.estimate.stems_cached ? " (stems cached)" : ""}` : "";
    // a backing voice carried over (Use these settings) stays on when the lead changes: it is said here, with a way to clear it, never silently (IA P9-3)
    const bk = S.opts.backing_voice ? ` with <b>${vname(S.opts.backing_voice)}</b> on backing vocals <button class="linkbtn" type="button" id="clear-backing">Use the original singer's backing</button>` : "";
    $("#summary").innerHTML = `<b>${esc(S.song.title)}</b> sung by <b>${vname(S.voice)}</b>${bk} <span class="meta" data-deep>· ${o.fit ? "pitch fit to the voice" : pitchText(o)}</span><span class="meta">${est}</span>`;
  }
}
function pitchText(o) {
  const k = +(o.key || 0), oct = +(o.octave || 0);
  const parts = [k ? `key ${fmtPitch(k)}` : "original key"];
  if (oct) parts.push(`voice ${oct > 0 ? "+" : "−"}${Math.abs(oct)} oct`);
  return parts.join(", ");
}
const NOTES = "C C# D D# E F F# G G# A A# B".split(" ");
// the conventional spelling of a key: minor keys say Eb and Bb (not D# and A#), C#, F# and G#; major keys say Db, Eb, Ab, Bb and F#
const KEY_MINOR = "C C# D Eb E F F# G G# A Bb B".split(" "), KEY_MAJOR = "C Db D Eb E F F# G Ab A Bb B".split(" ");
const keyName = (tonic, mode) => `${(mode === "minor" ? KEY_MINOR : KEY_MAJOR)[((tonic % 12) + 12) % 12]} ${mode}`;
function keyLabel(k) {
  const shift = k ? `${fmtPitch(k)} st` : "original";
  if (!S.songKey) return shift;
  const to = keyName(S.songKey.tonic + k, S.songKey.mode), from = keyName(S.songKey.tonic, S.songKey.mode);
  return k ? `${shift} (${from} → ${to})` : `original (${from})`;
}
function infoChips(r) {
  const c = [r.version, r.sr, r.epochs ? `${r.epochs >= 1000 ? r.epochs / 1000 + "k" : r.epochs} epochs` : null,
             r.steps ? `${Math.round(r.steps / 1000)}k steps` : null].filter(Boolean);
  return c.length ? `<span class="chiprow">${c.map(x => `<span class="tag">${esc(x)}</span>`).join("")}</span>` : "";
}
let estTimer = null;
function refreshEstimate() {
  clearTimeout(estTimer);
  if (!S.song || !S.voice) { S.estimate = null; summary(); return; }
  estTimer = setTimeout(async () => {
    try {
      S.estimate = await api("/api/estimate", { json: { song: S.song.id, voice: S.voice, options: S.opts } });
      $("#song-cached").hidden = !S.estimate.stems_cached;
    } catch (e) { S.estimate = null; }
    summary();
  }, 250);
}

// ------------------------------------------------------------ convert + progress
const ORDER = ["separating", "converting", "mixing"];
let elapsedTimer = null, jobStart = 0;
$("#convert").onclick = async () => {
  if (!S.song || !S.voice || S.job || S.osBusy) return;
  $("#err").hidden = true;
  try {
    const { job } = await api("/api/covers", { json: { song: S.song.id, voice: S.voice, options: S.opts } });
    S.job = job; summary(); startProgress(job);
  } catch (e) { showError(e.message, "Couldn't start the cover"); }
};
$("#cancel").onclick = () => { if (S.job) { api(`/api/jobs/${S.job}/cancel`, { method: "POST" }).catch(() => {}); $("#cancel").textContent = "Cancelling…"; } };

function setStage(name, cls, frac, eta) {
  const el = $(`.stage[data-s=${name}]`);
  el.className = "stage" + (cls ? " " + cls : "");
  if (frac != null) $(".bar i", el).style.width = Math.round(frac * 100) + "%";
  if (eta != null) $(".eta", el).textContent = eta;
}
// elsewhere: a cover this page didn't start (another tab, a reload): shown, but not loaded into the player at the end
function startProgress(job, elsewhere = false) {
  $("#progress").hidden = false; $("#progress-h").textContent = "Working"; $("#cancel").textContent = "Cancel"; $("#cancel").hidden = false;
  const est = S.estimate || {};
  $(".stage[data-s=separating] .sname").textContent = "Separating vocals";   // a finished cover left "Stems ready" here
  $(".stage[data-s=converting] .sname").textContent = "Converting the vocal";
  $(".stage[data-s=mixing] .sname").textContent = "Mixing & loudness match";
  ORDER.forEach(s => setStage(s, "", 0, est[s] != null ? (s === "separating" && est.stems_cached && !S.opts.karaoke && !S.opts.dereverb ? "cached" : `≈ ${fmtSecs(est[s])}`) : "—"));
  $(".stage[data-s=converting] .sname").textContent = `Converting to ${voiceDisplay(S.voice)}`;
  $("#progress").scrollIntoView({ block: "nearest", behavior: "smooth" });
  jobStart = performance.now(); let totalLeft = est.total, leftAt = performance.now();
  clearInterval(elapsedTimer);
  elapsedTimer = setInterval(() => {
    const el = (performance.now() - jobStart) / 1000;
    $("#elapsed").textContent = (totalLeft != null ? leftText(countedDown(totalLeft, (performance.now() - leftAt) / 1000)) + " · " : "") + `${el.toFixed(0)} s elapsed`;
  }, 250);
  let current = null, lastStageStart = 0;
  if (S.es) S.es();
  const stop = S.es = watchJob(job, ev => {   // jobs.js: the live stream, or progress polled while it's quiet
    if (ev.estimate) { S.estimate = ev.estimate; totalLeft = ev.estimate.total; leftAt = performance.now(); }
    if (ev.elapsed != null && !ev.final) jobStart = performance.now() - ev.elapsed * 1000;   // right after a reload too
    // queued behind the song's preparation (or other work): it says what it waits for, with the step and the time left
    if (!ORDER.includes(ev.stage) && !ev.final && (ev.stage === "preparing" || ev.state === "queued") && (ev.label || ev.message)) {
      $("#progress-h").textContent = progressSay(ev); $("#sr-live").textContent = ev.label || ev.message;
      $(".stage[data-s=separating] .sname").textContent = ev.label || ev.message;   // the waiting label only
      const f = ev.fraction ?? ev.frac;
      if (f != null) setStage("separating", "active", f, ev.total_left > 5 ? `${fmtSecs(ev.total_left)} left` : "waiting");
      if (ev.total_left != null) { totalLeft = ev.total_left; leftAt = performance.now(); }
    }
    if (ORDER.includes(ev.stage)) {
      $("#progress-h").textContent = "Working";
      if (ev.stage !== current) {
        ORDER.slice(0, ORDER.indexOf(ev.stage)).forEach(s => { const el = $(`.stage[data-s=${s}]`); if (!el.classList.contains("done")) setStage(s, "done", 1, el.dataset.took || "done"); });
        if (current) $(`.stage[data-s=${current}]`).dataset.took = `${(ev.elapsed - lastStageStart).toFixed(1)} s`;
        current = ev.stage; lastStageStart = ev.elapsed;
        $("#sr-live").textContent = $(`.stage[data-s=${ev.stage}] .sname`).textContent + "…";
      }
      $(`.stage[data-s=${ev.stage}] .sname`).textContent = ev.steps > 1 && ev.step_n && !/\d+ of \d+/.test(ev.message) ? `${ev.message} (${ev.step_n} of ${ev.steps})` : ev.message;
      setStage(ev.stage, ev.frac >= 1 ? "done" : "active", ev.frac, ev.frac >= 1 ? `${(ev.elapsed - lastStageStart).toFixed(1)} s` : `${fmtSecs(Math.max(1, ev.stage_left))} left`);
      totalLeft = ev.total_left; leftAt = performance.now();
    }
    if (ev.final) {
      stop(); S.es = null; clearInterval(elapsedTimer); S.job = null; summary();
      if (ev.state === "done") {
        const sec = ev.seconds || {};
        const took = { separating: sec.separate < 0.5 ? "cached" : sec.separate, converting: sec.convert, mixing: sec.remix };
        ORDER.forEach(s => setStage(s, "done", 1, typeof took[s] === "number" ? `${took[s].toFixed(1)} s` : (took[s] || "done")));
        $("#progress-h").textContent = "Done"; $("#cancel").hidden = true;
        $("#sr-live").textContent = "Your cover is ready.";
        $("#elapsed").textContent = `Finished in ${(ev.elapsed || (performance.now() - jobStart) / 1000).toFixed(1)} s`;
        // without the stream's last event (only the summary got through) the newest cover is this one
        if (elsewhere) loadHistory();
        else if (ev.result) showResult(ev.result, ev.seconds); else loadHistory().then(() => { if (ev.synthetic && HIST[0]) showResult(HIST[0]); });
        if (ev.notes?.length) showError(ev.notes.join(" "), "Heads up", null, true);
        refreshEstimate();
      } else if (ev.state === "cancelled") {
        $("#progress").hidden = true;
      } else {
        $("#progress").hidden = true;
        showError(ev.message, "The cover didn't finish");
      }
    }
  });
}

// ------------------------------------------------------------ result player (A/B)
const aCover = $("#a-cover"), aOrig = $("#a-orig");

// Volume: one level for both players so Cover/Original comparisons stay fair; remembered per browser.
const VOL = { level: store.get("uc-volume", 0.8), muted: store.get("uc-muted", false) };
// ------------------------------------------------------------ a voice's preview line (rc-preview)
// Every voice says "Go undercover as <its name>." (core/voicepreview.py, made with the voice): a ▶ wherever a voice is
// picked, only when its clip is there (S.voices[].preview). One player for the page: it stops anything else playing,
// and anything else starting stops it. The ▶ never picks the card it sits on.
const VLINE = { a: null, btn: null };
function voiceLineButton(name) {
  const v = (S.voices || []).find(x => x.name === name);
  if (!v?.preview) return "";
  const label = voiceDisplay(name);
  return `<button class="play vline" type="button" data-vline="${esc(name)}" data-label="${esc(label)}" aria-label="Hear ${esc(label)}" title="Hear ${esc(label)}">▶</button>`;
}
function bindVoiceLines(root) {
  $$("[data-vline]", root).forEach(b => {
    b.onclick = e => { e.stopPropagation(); e.preventDefault(); playVoiceLine(b); };
    b.onkeydown = e => { if (e.key === "Enter" || e.key === " ") e.stopPropagation(); };   // (the card's own Enter picks it)
  });
}
function playVoiceLine(b) {
  if (VLINE.btn === b && VLINE.a && !VLINE.a.paused) return VLINE.a.pause();
  if (VLINE.a) VLINE.a.pause();
  $$("audio").forEach(x => { if (!x.paused) x.pause(); });
  const a = new Audio(`/api/voices/${encodeURIComponent(b.dataset.vline)}/preview`);
  const lvl = VOL.muted ? 0 : VOL.level;
  a.volume = lvl * lvl;
  VLINE.a = a; VLINE.btn = b;
  const set = on => { b.textContent = on ? "❚❚" : "▶"; b.classList.toggle("playing", on); b.setAttribute("aria-label", `${on ? "Stop" : "Hear"} ${b.dataset.label}`); };
  a.addEventListener("play", () => set(true));
  a.addEventListener("pause", () => set(false));
  a.addEventListener("ended", () => set(false));
  a.play().catch(() => { set(false); b.title = "The browser couldn't play this voice's line."; });
}
document.addEventListener("play", e => { if (VLINE.a && e.target !== VLINE.a && !VLINE.a.paused) VLINE.a.pause(); }, true);   // another player started

function applyVolume() {
  const v = VOL.muted ? 0 : VOL.level;
  aCover.volume = aOrig.volume = aSong.volume = v * v;  // squared: a slider position sounds closer to proportional loudness
  $$("#preview-list audio").forEach(a => { a.volume = v * v; });
  $("#volume").value = VOL.muted ? 0 : VOL.level;
  const silent = VOL.muted || VOL.level === 0;
  $("#vol-waves").toggleAttribute("hidden", silent); $("#vol-x").toggleAttribute("hidden", !silent);
  $("#mute").setAttribute("aria-label", silent ? "Unmute" : "Mute");
  store.set("uc-volume", VOL.level); store.set("uc-muted", VOL.muted);
}
$("#volume").addEventListener("input", e => { VOL.level = +e.target.value; VOL.muted = false; applyVolume(); });
$("#mute").onclick = () => { if (VOL.level === 0) { VOL.level = 0.8; VOL.muted = false; } else VOL.muted = !VOL.muted; applyVolume(); };
applyVolume();
let ab = "cover", wavePeaks = { cover: null, orig: null }, playingHist = null;
const active = () => ab === "cover" ? aCover : aOrig;

// covers of the same song in the same voice(s) are told apart: the first keeps its title, the later ones say (2), (3)
function coverTitle(h) {
  const same = HIST.filter(x => x.title === h.title && x.voice === h.voice && (x.backing_voice || "") === (h.backing_voice || ""))
    .sort((a, b) => String(a.created).localeCompare(String(b.created)) || String(a.id).localeCompare(String(b.id)));
  const n = same.findIndex(x => x.id === h.id);
  return `${h.title}${n > 0 ? ` (${n + 1})` : ""}`;
}
const coverName = h => `${coverTitle(h)} · ${voiceDisplay(h.voice)}${h.backing_voice ? " + " + voiceDisplay(h.backing_voice) : ""}`;
async function showResult(item, seconds) {
  S.result = item; playingHist = item.id;
  $("#result").hidden = false;
  if (typeof earShow === "function") earShow($("#ear-cover"), "cover", item);   // How did it come out? (ear.js)
  $("#result-h").textContent = coverName(item);
  renderKeep();
  stopAll();
  aCover.src = item.output; aOrig.src = item.original || "";
  ab = "cover"; $$(".ab button").forEach(b => b.setAttribute("aria-pressed", b.dataset.ab === "cover"));
  $(".ab [data-ab=orig]").disabled = !item.original;
  $("#dl-cover").href = item.output + "?download"; $("#dl-cover").textContent = `Download ${item.output_name.split(".").pop().toUpperCase()}`;
  $("#dl-vocal").href = item.vocal ? item.vocal + "?download" : "#"; $("#dl-vocal").hidden = !item.vocal;
  const sec = seconds || item.seconds || {};
  $("#times").innerHTML = [["separate", sec.separate, sec.separate < 0.5 ? "cached" : null], ["convert", sec.convert], ["mix", sec.remix], ["total", sec.total]]
    .filter(([, v]) => v != null).map(([k, v, alt]) => `<span>${k} ${alt || v.toFixed(1) + " s"}</span>`).join("");
  wavePeaks = { cover: null, orig: S.song && item.original === S.song.url ? S.song.peaks : null };
  drawResultWave();
  peaksFromUrl(item.output).then(p => { wavePeaks.cover = p; drawResultWave(); }).catch(() => {});
  if (item.original && !wavePeaks.orig) peaksFromUrl(item.original).then(p => { wavePeaks.orig = p; drawResultWave(); }).catch(() => {});
  $("#result").scrollIntoView({ block: "nearest", behavior: "smooth" });
  loadHistory();
}
// Keep (phase 6): "Send to SafeHouse" keeps a cover for good; unkept ones roll off the recent tray (the latest 15)
function renderKeep() {
  const r = S.result, b = $("#keep");
  if (!r) return;
  b.setAttribute("aria-pressed", !!r.kept); b.dataset.kept = r.kept ? "true" : "false";
  b.textContent = r.kept ? "Kept in SafeHouse ✓" : "Send to SafeHouse";
}
async function toggleKeep(id, kept) {
  try { await api(`/api/library/covers/${id}/${kept ? "unkeep" : "keep"}`, { json: {} }); }
  catch (e) { return showError(e.message, "Couldn't change that"); }
  if (S.result?.id === id) S.result.kept = !kept;
  await loadHistory(); renderKeep();
}
$("#keep").onclick = () => S.result && toggleKeep(S.result.id, !!S.result.kept);
function drawResultWave() {
  const a = active(), frac = a.duration ? a.currentTime / a.duration : 0;
  drawWave($("#wave-res"), wavePeaks[ab] || wavePeaks.cover, frac, ab === "cover" ? "--wave-on" : "--wave-alt");
  $("#pos").textContent = `${fmtTime(a.currentTime)} / ${fmtTime(a.duration || 0)}`;
}
function stopAll() { aCover.pause(); aOrig.pause(); aSong.pause(); $("#play").textContent = "▶"; $("#play").setAttribute("aria-label", "Play"); }
function togglePlay() {
  const a = active();
  if (!a.src) return;
  if (a.paused) { $$("#preview-list audio").forEach(x => x.pause()); aSong.pause(); a.play().catch(e => showError("The browser couldn't play this file. Try downloading it instead.", "Playback failed")); $("#play").textContent = "❚❚"; $("#play").setAttribute("aria-label", "Pause"); }
  else stopAll();
  renderHistory();
}
$("#play").onclick = togglePlay;
$(".ab").addEventListener("click", e => {
  const b = e.target.closest("button"); if (!b || b.disabled || b.dataset.ab === ab) return;
  const from = active(), wasPlaying = !from.paused, t = from.currentTime;
  from.pause(); ab = b.dataset.ab;
  $$(".ab button").forEach(x => x.setAttribute("aria-pressed", x === b));
  const to = active();
  try { to.currentTime = Math.min(t, (to.duration || t)); } catch (err) {}
  if (wasPlaying) to.play().catch(() => {});
  drawResultWave();
});
$("#wave-res").tabIndex = 0;
$("#wave-res").setAttribute("role", "slider");
$("#wave-res").setAttribute("aria-label", "Playback position: Left/Right arrows move 5 seconds, Home goes to the start");
$("#wave-res").addEventListener("keydown", e => {
  const a = active(); if (!a.duration) return;
  const step = { ArrowLeft: -5, ArrowRight: 5, PageDown: -30, PageUp: 30 }[e.key];
  if (step !== undefined) a.currentTime = Math.min(a.duration, Math.max(0, a.currentTime + step));
  else if (e.key === "Home") a.currentTime = 0;
  else if (e.key === "End") a.currentTime = Math.max(0, a.duration - 1);
  else return;
  e.preventDefault(); drawResultWave();
  $("#wave-res").setAttribute("aria-valuetext", `${fmtTime(a.currentTime)} of ${fmtTime(a.duration)}`);
});
$("#wave-res").addEventListener("click", e => {
  const a = active(); if (!a.duration) return;
  const r = e.currentTarget.getBoundingClientRect();
  a.currentTime = (e.clientX - r.left) / r.width * a.duration; drawResultWave();
});
[aCover, aOrig].forEach(a => {
  a.addEventListener("timeupdate", () => { if (a === active()) drawResultWave(); });
  a.addEventListener("loadedmetadata", drawResultWave);
  a.addEventListener("ended", () => { stopAll(); renderHistory(); });
});
addEventListener("keydown", e => {
  if (e.code === "Space" && !$("#result").hidden && !$("#view-covers").hidden && !/INPUT|TEXTAREA|BUTTON|SUMMARY/.test(document.activeElement.tagName)) { e.preventDefault(); togglePlay(); }
});
$("#open-folder").onclick = () => api("/api/open-folder", { json: { history_id: S.result?.id } }).catch(e => showError(e.message));
$("#open-output").onclick = () => api("/api/open-folder", { json: {} }).catch(e => showError(e.message));

// ------------------------------------------------------------ preview (audition settings on a 15 s excerpt)
let previewKind = store.get("uc-preview-kind", "octave");
function previewVariants() {
  const o = S.opts, clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const uniq = arr => [...new Map(arr.map(v => [JSON.stringify(v), v])).values()];
  if (previewKind === "octave") {
    const cur = +(o.octave || 0);
    return uniq([cur - 1, cur, cur + 1].map(x => clamp(x, -2, 2))).map(x => ({
      octave: x, label: o.fit ? (x === cur ? "Fit's pick" : `${Math.abs(x - cur) > 1 ? "Two octaves" : "An octave"} ${x < cur ? "lower" : "higher"}`)   // around the pick, not absolute
        : x === 0 ? "Same octave" : `Voice ${x > 0 ? "+" : "−"}${Math.abs(x)} octave` }));
  }
  if (previewKind === "key") {
    const cur = +(o.key || 0);
    return uniq([cur - 2, cur, cur + 2].map(x => clamp(x, -6, 6))).map(x => ({ key: x, label: o.fit ? (x === cur ? "Fit's pick" : `${Math.abs(x - cur)} semitone${Math.abs(x - cur) === 1 ? "" : "s"} ${x < cur ? "lower" : "higher"}`) : keyLabel(x) }));
  }
  if (previewKind === "correction")
    return [{ pitch_correction: 0, label: "No correction" }, { pitch_correction: 0.4, label: "Gentle (0.4)" }, { pitch_correction: 1, label: "Full autotune" }];
  return [{ polish: false, label: "Polish off" }, { polish: true, label: "Polish on" }];
}
function syncPreviewKind() { $$("#preview-kind button").forEach(b => b.setAttribute("aria-pressed", b.dataset.v === previewKind)); }
// each tab keeps its own renders (one set of cards per kind), so a tab never shows another tab's versions; a set belongs to
// the song and voice it was made for and goes when either changes
// "Fit pitch to the voice" picks the octave and key itself, which would make every octave or key version the same
function pvByHand(kind) { return !!S.opts.fit && (kind === "octave" || kind === "key"); }
// brings the sets in line with the page (the song, the voice, the fit, the settings in use) without touching playback
function previewSync() {
  const box = $("#preview-list"); if (!box) return;
  $$(".pv-set", box).forEach(el => {
    if (el.dataset.song !== String(S.song?.id) || el.dataset.voice !== String(S.voice)) { $$("audio", el).forEach(a => a.pause()); el.remove(); }
    else el.hidden = el.dataset.kind !== previewKind;
  });
  $$(".pcard", box).forEach(c => {   // ✓ marks the version whose settings are the ones in use now
    const set = c.closest(".pv-set"), o = JSON.parse(c.dataset.opts || "{}");
    const used = Object.keys(o).length > 0 && !(pvByHand(set.dataset.kind) || S.opts.fit && ("octave" in o || "key" in o)) && Object.entries(o).every(([k, v]) => S.opts[k] === v);
    $(".use", c).textContent = used ? "✓ Using this" : "Use this";
  });
  if (!$("#preview-bar").hidden) return;   // a preview is rendering: its own progress line owns the status
  const set = $(`.pv-set[data-kind="${previewKind}"]`, box);
  $("#preview-status").textContent = (set ? "Switch between versions while playing: they stay in sync. Pick one with “Use this”."
    : "Renders 15 seconds of the song (the part with the most singing) a few ways, so you can pick by ear. The first preview of a new song takes about a minute (it separates the song once); after that, a few seconds each.")
    + (pvByHand(previewKind) ? " “Fit pitch to the voice” is on: the middle one is the fit's pick, the others are around it. Use this keeps that choice and turns the fit off." : "");
}
function showPreviewKind() { $$("#preview-list audio").forEach(a => a.pause()); previewSync(); }
$("#preview-kind").addEventListener("click", e => {
  const b = e.target.closest("button"); if (!b) return;
  previewKind = b.dataset.v; store.set("uc-preview-kind", previewKind); syncPreviewKind(); showPreviewKind();
});
$("#preview-btn").onclick = () => {
  $("#preview").hidden = false; syncPreviewKind(); showPreviewKind();
  $("#preview").scrollIntoView({ block: "nearest", behavior: "smooth" });
};
$("#preview-close").onclick = () => { $$("#preview-list audio").forEach(a => a.pause()); $("#preview").hidden = true; };

$("#preview-go").onclick = async () => {
  if (!S.song || !S.voice || S.job || S.osBusy) return;
  const variants = previewVariants(), kind = previewKind, made = { song: S.song.id, voice: S.voice, title: S.song.title };
  $$("#preview-list audio").forEach(a => a.pause());
  $$(`#preview-list .pv-set[data-kind="${kind}"]`).forEach(el => el.remove());
  $("#preview-go").disabled = true;
  $("#preview-bar").hidden = false; $("#preview-bar i").style.width = "0%";
  $("#preview-status").textContent = "Sending it to the queue…";
  try {
    const { job } = await api("/api/preview", { json: { song: S.song.id, voice: S.voice, options: S.opts, variants } });
    S.job = job; summary();
    $("#preview-status").textContent = "Waiting for the first word…";
    const stop = watchJob(job, ev => {
      const say = progressSay(ev); if (say) $("#preview-status").textContent = say;   // the step, and the time left
      const f = ev.frac ?? ev.fraction;
      if (f != null) $("#preview-bar i").style.width = Math.round((ev.stage === "rendering" ? 0.3 + 0.7 * f : 0.3 * f) * 100) + "%";
      if (!ev.final) return;
      stop(); S.job = null; summary(); $("#preview-go").disabled = false; $("#preview-bar").hidden = true;
      if (ev.state === "done" && (S.song?.id !== made.song || S.voice !== made.voice)) $("#preview-status").textContent = "The song or the voice changed while it rendered, so those versions were put away. Render again.";
      else if (ev.state === "done") { renderPreviews(ev.previews, kind, made, variants); refreshEstimate(); }
      else if (ev.state === "cancelled") $("#preview-status").textContent = "Cancelled.";
      else { $("#preview-status").textContent = ""; showError(ev.message, "Preview failed"); }
    });
  } catch (e) {
    $("#preview-go").disabled = false; $("#preview-bar").hidden = true; $("#preview-status").textContent = "";
    showError(e.message, "Preview failed");
  }
};

function renderPreviews(list, kind, made, variants) {
  // the server's options carry the octave AND key it settled on (the fit's pick, with the offset). An octave or key card keeps them
  // both (that's what was heard: Use this applies the pair); the other tabs keep only what their variant asked for
  const pitch = kind === "octave" || kind === "key";
  list = list.map((p, i) => ({ ...p, options: pitch ? { ...(p.options || {}) }
    : Object.fromEntries(Object.keys(variants[i] || {}).filter(k => k !== "label" && p.options && k in p.options).map(k => [k, p.options[k]])) }));
  const fitNote = list.find(p => p.fit)?.fit;   // "Fit to voice: octave -1, key +3 (…)"
  const box = document.createElement("div");   // this tab's cards; display: contents keeps them in the grid
  box.className = "pv-set"; box.dataset.kind = kind; box.dataset.song = made.song; box.dataset.voice = made.voice;
  $("#preview-list").append(box);
  box.innerHTML = `<p class="meta pv-for">${esc(made.title)} · ${esc(voiceDisplay(made.voice))}${fitNote ? `<span data-deep> · ${esc(fitNote)}</span>` : ""}</p>` + list.map((p, i) => `
    <div class="pcard" data-i="${i}" data-opts="${esc(JSON.stringify(list[i].options || {}))}">
      <div class="ptitle"><button class="play" aria-label="Play ${esc(p.label)}">▶</button><b>${esc(p.label)}</b></div>
      <button class="btn use">Use this</button>
      <audio preload="auto" src="${esc(p.url)}"></audio>
    </div>`).join("");
  applyVolume();
  showPreviewKind();
  const cards = $$(".pcard", box), audios = cards.map(c => $("audio", c));
  let pos = 0;
  cards.forEach((card, i) => {
    const a = audios[i];
    $(".play", card).onclick = () => {
      if (!a.paused) { a.pause(); return; }
      const playing = audios.find(x => !x.paused);
      if (playing) { pos = playing.currentTime; playing.pause(); }
      stopAll();  // pause the main result player too
      try { a.currentTime = Math.min(pos, a.duration || pos); } catch (e) {}
      a.play().catch(() => {});
    };
    a.addEventListener("play", () => { card.classList.add("playing"); $(".play", card).textContent = "❚❚"; });
    a.addEventListener("pause", () => { card.classList.remove("playing"); $(".play", card).textContent = "▶"; pos = a.currentTime; });
    a.addEventListener("ended", () => { pos = 0; });
    $(".use", card).onclick = () => {
      // by hand is decided now, not when the set was drawn: fit may have been turned on since
      Object.assign(S.opts, list[i].options); if (pvByHand(kind)) S.opts.fit = false; store.set("uc-opts", S.opts); syncControls(); summary(); refreshEstimate();
      previewSync();
    };
  });
}

// ------------------------------------------------------------ history
let HIST = [];
// Recent in a module (FrontMan's covers, SpeakEasy's speech; the user, 2026-10-15): the newest five, "Show more" opens the rest of the
// tray's 15, "Show fewer" folds it back. Everything older is in SafeHouse.
const RECENT_SHOW = 5, RECENT_TRAY = 15;
const recentMore = (id, open, total) => total > RECENT_SHOW
  ? `<button class="linkbtn recent-more" type="button" id="${id}" aria-expanded="${open}">${open ? "Show fewer" : "Show more"}</button>` : "";
async function loadHistory() {
  let ok = true;
  try { HIST = await api("/api/history"); } catch (e) { HIST = []; ok = false; }
  const mine = S.result && HIST.find(x => x.id === S.result.id);
  if (mine) { S.result.kept = mine.kept; renderKeep(); $("#result-h").textContent = coverName(S.result); }
  else if (ok && S.result) { stopAll(); $("#result").hidden = true; S.result = null; }   // it rolled off the tray (or was deleted)
  renderHistory();
}
function renderHistory() {
  const box = $("#hist");
  if (!HIST.length) { box.innerHTML = `<p class="meta">Your covers will appear here.</p>`; return; }
  const nowPlaying = !active().paused ? playingHist : null;
  box.innerHTML = HIST.slice(0, S.histAll ? RECENT_TRAY : RECENT_SHOW).map(h => {
    const o = h.options || {};
    const bits = [o.key != null || o.octave != null ? pitchText(o) : (o.pitch ? `vocal ${fmtPitch(o.pitch)} st (out of key)` : null), o.quality, o.karaoke ? "lead only" : null, o.dereverb ? "de-reverb" : null,
                  h.seconds?.total ? `made in ${h.seconds.total.toFixed(1)} s` : null].filter(Boolean).join(" · ");
    return `<div class="hrow${h.id === nowPlaying ? " playing" : ""}" data-kept="${h.kept ? "true" : "false"}"><div><strong>${esc(coverTitle(h))}${typeof earMark === "function" ? earMark("cover", h.id) : ""}</strong>${facts([
        { html: `${vname(h.voice)}${h.backing_voice ? " + " + vname(h.backing_voice) : ""}` }, { t: upFirst(dayWord(h.created)), muted: true }, { t: dayTime(h.created), deep: true }, ...(bits ? bits.split(" · ").map(t => ({ t, deep: true })) : [])])}</div>
      <div class="hbtns"><button class="icon-btn" data-play="${h.id}" aria-label="Play ${esc(h.title)}">${h.id === nowPlaying ? "❚❚" : "▶"}</button><button class="icon-btn" data-keep="${h.id}" aria-pressed="${h.kept}" title="${h.kept ? "Kept in SafeHouse. Click to take it back." : "Keep this cover for good"}">${h.kept ? "Kept ✓" : "Send to SafeHouse"}</button><a class="icon-btn" href="${esc(h.output)}?download">Download</a></div></div>`;
  }).join("") + recentMore("hist-more", !!S.histAll, HIST.length);
  $("#hist-more")?.addEventListener("click", () => { S.histAll = !S.histAll; renderHistory(); });
  $$("[data-play]", box).forEach(b => b.onclick = () => {
    const h = HIST.find(x => x.id === b.dataset.play);
    if (S.result?.id === h.id) return togglePlay();
    showResult(h).then(() => { aCover.addEventListener("canplay", () => togglePlay(), { once: true }); });
  });
  $$("[data-keep]", box).forEach(b => b.onclick = () => toggleKeep(b.dataset.keep, b.getAttribute("aria-pressed") === "true"));
}

// ------------------------------------------------------------ boot
function redrawAll() { drawSongWave(); if (!$("#result").hidden) drawResultWave(); }

// ------------------------------------------------------------ the song on the desk: play / pause, the waveform, seek (rc-page11)
// The user: "there is no ability to preview the song here, which we used to have". The waveform had gone too: it's drawn from the canvas's width,
// and since the build path folds into the one-shot card (qa18) the card is often hidden when the song loads, so it drew nothing and never again.
// A ResizeObserver draws it whenever the canvas gets (or changes) its size.
function drawSongWave() {
  if (!S.song) return;
  const d = aSong.duration || S.song.duration;   // the song's own length until the audio's has loaded (preload none)
  drawWave($("#wave-song"), S.song.peaks, d ? Math.min(1, aSong.currentTime / d) : 0);
  $("#song-pos").textContent = !aSong.paused || aSong.currentTime ? `${fmtTime(aSong.currentTime)} / ${fmtTime(aSong.duration || S.song.duration || 0)}` : "";
}
new ResizeObserver(() => drawSongWave()).observe($("#wave-song"));
$("#song-play").onclick = () => {
  if (!aSong.src) return;
  if (!aSong.paused) return aSong.pause();
  stopAll(); $$("#preview-list audio").forEach(x => x.pause());   // one thing plays at a time
  aSong.play().catch(() => showError("The browser couldn't play this file.", "Playback failed"));
};
const songBtn = on => { $("#song-play").textContent = on ? "❚❚" : "▶"; $("#song-play").setAttribute("aria-label", on ? "Pause the song" : "Play the song"); };
aSong.addEventListener("play", () => songBtn(true));
aSong.addEventListener("pause", () => { songBtn(false); drawSongWave(); });
aSong.addEventListener("ended", () => { aSong.currentTime = 0; songBtn(false); drawSongWave(); });
aSong.addEventListener("timeupdate", drawSongWave);
$("#wave-song").addEventListener("click", e => {
  e.stopPropagation();   // the drop zone around it would open the file picker
  const d = aSong.duration || S.song?.duration; if (!d || !aSong.src) return;
  const r = e.currentTarget.getBoundingClientRect();
  aSong.currentTime = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)) * d;
  drawSongWave();
});
addEventListener("resize", redrawAll);
(async function boot() {
  applyTheme();
  pollStatus();
  try {
    S.defaults = await api("/api/config");
    if (!("key" in S.defaults) || !("backing_voice" in S.defaults)) restartNeeded();  // server predates these settings
    loadOpts();
    await loadVoices();
    await loadHistory();
    // a cover started before a reload is still running: show its progress again instead of inviting a duplicate
    // and later: a cover started somewhere else (another tab, a one-shot's "Make it with this version")
    const follow = (list, elsewhere) => {
      const run = list.find(j => j.kind === "cover" && isRunning(j) && !/^Preview/.test(j.title || ""));
      if (run && !S.job) { S.job = run.id; summary(); startProgress(run.id, elsewhere); }
    };
    follow(await refreshJobs(true), false);
    onJobs(list => follow(list, true));
    const last = store.get("uc-song", null);
    if (last) { try { S.song = await api(`/api/songs/${last}`); renderSong(); refreshEstimate(); } catch (e) { store.set("uc-song", null); } }
  } catch (e) { showError(e.message, "UnderCover isn't responding"); }
})();
