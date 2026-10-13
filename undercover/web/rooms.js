"use strict";
// SafeHouse rooms (phase 4b, rooms build): the room strip (Library · Running · Storage · Add-ons · System · Settings, deep links
// #safehouse/<room>), Phone access in Settings (phase 11, at the end), the Add-ons room (every part UnderCover is built from; optional add-ons are accepted and installed here)
// and the Storage room (where the three groups of files live, and moving them). The Library room is library.js, Running is
// work.js (renderRunRoom), System's rows come from app.js (sysStatus) and library.js (yt-dlp, Quit), Settings from app.js.
// Uses helpers from app.js ($, $$, esc, api, showError, store, leftText), jobs.js (watchJob, runningJobs, onJobs) and
// library.js (gb, confirmClick, loadSummary).

const ROOMS = ["library", "running", "storage", "addons", "system", "settings"];
const R = { room: null, addons: null, filter: store.get("uc-ad-filter", "all"), open: null, jobs: {}, storage: null,
            move: null, check: null, moving: null };
const MOD_NAMES = { frontman: "FrontMan", doubleagent: "DoubleAgent", speakeasy: "SpeakEasy", safehouse: "SafeHouse" };

function roomOf(hash) {
  const r = String(hash || "").split("/")[1];
  return ROOMS.includes(r) ? r : null;
}
function showRoom(room) {
  room = ROOMS.includes(room) ? room : "library";
  R.room = room;
  store.set("uc-sh-room", room);
  $$(".room").forEach(b => b.setAttribute("aria-selected", b.dataset.room === room));
  for (const r of ROOMS) $(`#room-${r}`).hidden = r !== room;
  roomInSight();
  if (room === "running") loadRunRoom();
  if (room === "addons") loadAddons();
  if (room === "storage") loadStorage();
  if (room === "system") loadDoctor();
  if (room === "settings") loadPhone();
}
// on a phone the row scrolls: keep the open room in sight, again whenever a tab changes width (the font arriving, a count)
function roomInSight() {
  const tab = $('.room[aria-selected="true"]'), row = tab?.parentNode;
  if (!tab || !row.clientWidth) return;
  const x = tab.getBoundingClientRect().left - row.getBoundingClientRect().left;
  if (x < 0 || x + tab.offsetWidth > row.clientWidth) row.scrollLeft += x - 16;
}
const roomSizes = new ResizeObserver(roomInSight);
$$(".room").forEach(b => roomSizes.observe(b));
$$(".room").forEach(b => b.onclick = () => { location.hash = b.dataset.room === "library" ? "safehouse" : `safehouse/${b.dataset.room}`; });
document.addEventListener("uc:module", e => {
  if (e.detail !== "safehouse") return;
  const r = roomOf(location.hash.slice(1)) || store.get("uc-sh-room", "library");
  if (r !== "library" && !location.hash.includes("/")) history.replaceState(null, "", `#safehouse/${r}`);
  showRoom(r);
  roomCounts();
});
document.addEventListener("uc:subpage", e => { if (String(e.detail).startsWith("safehouse")) showRoom(roomOf(e.detail) || "library"); });

async function roomCounts() {
  try {
    if (!R.addons) R.addons = (await api("/api/addons")).addons;
    const opt = R.addons.filter(a => a.optional && a.installable);
    const inst = opt.filter(a => a.installed).length;
    $("#room-n-addons").textContent = inst ? `${inst} added` : `${opt.length} optional`;
  } catch (e) { /* the room shows its own error */ }
}

// ------------------------------------------------------------ Add-ons
const FILTERS = [["all", "All", () => true], ["included", "Included", a => a.included], ["optional", "Optional", a => a.optional],
                 ["installed", "Installed", a => a.installed]];

async function loadAddons() {
  try { R.addons = (await api("/api/addons")).addons; }
  catch (e) { $("#ad-list").innerHTML = `<p class="meta">${esc(e.message)}</p>`; return; }
  for (const j of runningJobs()) if (j.kind === "addon") {   // an install started before a reload (or in another tab)
    const a = R.addons.find(x => j.title === `Adding ${x.name}`);
    if (a && !R.jobs[a.id]) followInstall(a.id, j.id);
  }
  renderAddons();
  roomCounts();
}

function licenceChip(a) {
  if (a.optional) return `<span class="chip nc">personal use only</span>`;
  if (a.open_note && a.id === "separation") return `<span class="chip open">open-source code · models being checked</span>`;
  if (a.licence.startsWith("CC-BY-4")) return `<span class="chip open">open · credit the authors</span>`;
  return `<span class="chip open">open source</span>`;
}
function modChips(a) {
  return (a.modules || []).map(m => `<span class="chip mod" style="--mc:var(--c-${m})">${MOD_NAMES[m] || m}${a.wired ? "" : " (later)"}</span>`).join("");
}
const sizeText = a => gb(a.disk || a.size_mb * 1e6);
const day = s => s ? new Date(s.replace(" ", "T")).toLocaleDateString(undefined, { day: "numeric", month: "short" }) : "";

function addonSide(a) {
  const job = R.jobs[a.id];
  if (a.included) return `<span class="astate ${a.installed ? "ok" : ""}">${a.installed ? "Included ✓" : "Included · downloads when first used"}</span><a class="linkbtn" href="${esc(a.licence_url)}" target="_blank" rel="noopener">Licence</a>`;
  if (!a.installable) return `<span class="astate">Not available yet</span><a class="linkbtn" href="${esc(a.licence_url)}" target="_blank" rel="noopener">Read licence</a>`;
  if (job) return `<span class="astate">Installing…</span>`;
  if (a.installed && a.accepted) {
    const use = a.engine ? `<label class="check" style="font-size:13.5px" data-deep data-pc><input type="checkbox" data-use="${a.id}" ${a.in_use ? "checked" : ""}><span>Use for cadence speech</span></label>` : "";
    return `<span class="astate ok">Installed ✓</span><span class="meta">added ${day(a.installed_on) || "earlier"} · licence accepted ${day(a.accepted)}</span>${use}
            <button class="btn danger" type="button" data-rm="${a.id}" data-pc>Remove · frees ${gb(a.disk || a.size_mb * 1e6)}</button>`;
  }
  if (R.open === a.id) return `<span class="astate">Not added</span>`;
  return `<span class="astate">Not added</span><button class="btn primary" type="button" data-add="${a.id}" data-pc>Accept &amp; install…</button>
          <a class="linkbtn" href="${esc(a.licence_url)}" target="_blank" rel="noopener">Read licence</a>`;
}

function renderAddons() {
  const list = R.addons || [];
  $("#ad-filters").innerHTML = FILTERS.map(([k, label, f]) => `<button class="cat" type="button" data-f="${k}" aria-pressed="${R.filter === k}"><b>${label}</b><small>${list.filter(f).length}</small></button>`).join("");
  $$("#ad-filters .cat").forEach(b => b.onclick = () => { R.filter = b.dataset.f; store.set("uc-ad-filter", R.filter); renderAddons(); });
  const show = FILTERS.find(f => f[0] === R.filter)?.[2] || (() => true);
  const rows = list.filter(show);
  $("#ad-list").innerHTML = rows.length ? rows.map(a => `
    <article class="addon${a.optional ? " optional" : ""}" data-id="${a.id}">
      <div style="display:grid;gap:6px;min-width:0">
        <div class="row"><h4>${esc(a.name)}</h4>${licenceChip(a)}</div>
        <p class="what">${esc(a.adds)}</p>
        <div class="tags">${modChips(a)}<span class="chip">${sizeText(a)}</span><span class="chip" data-deep>${esc(a.licence)}</span></div>
      </div>
      <div class="side">${addonSide(a)}</div>
      ${R.open === a.id && !R.jobs[a.id] ? `<div class="accept">
        <p class="summary">${esc(a.licence_summary)}</p>
        <p class="meta"><a href="${esc(a.licence_url)}" target="_blank" rel="noopener">Read the full licence ↗</a></p>
        <label class="check"><input type="checkbox" id="ad-ok"><span>I've read the licence and accept it (${esc(a.licence)}).</span></label>
        <div class="row"><button class="btn primary" type="button" id="ad-go" disabled>Install · ${sizeText(a)}</button>
          <button class="linkbtn" type="button" id="ad-cancel">Cancel</button>
          <span class="meta">Downloads in the background; you can keep using UnderCover.</span></div>
      </div>` : ""}
      ${R.jobs[a.id] ? `<div class="inprog"><div class="row spread"><span class="meta" id="ad-msg-${a.id}">${esc(R.jobs[a.id].msg || "Starting")}</span>
        <span class="meta" id="ad-left-${a.id}">${esc(R.jobs[a.id].left || "")}</span></div>
        <div class="bar"><i id="ad-bar-${a.id}" style="width:${Math.round((R.jobs[a.id].frac || 0) * 100)}%"></i></div></div>` : ""}
      <div class="deepbox" data-deep>id ${esc(a.id)}${a.packages ? ` · ${a.packages} pip packages (pinned, --no-deps)` : ""}${a.files?.length ? ` · ${esc(a.files.join(", "))}` : ""}${a.accepted ? ` · accepted ${esc(a.accepted)} (${esc(a.accepted_licence || "")})` : ""}${a.open_note ? ` · ${esc(a.open_note)}` : ""}${a.installable ? ` · <code>python -m undercover.addons install ${esc(a.id)} --accept-licence</code>` : ""}</div>
    </article>`).join("") : `<p class="meta">Nothing here.</p>`;
  $$("#ad-list [data-add]").forEach(b => b.onclick = () => { R.open = b.dataset.add; renderAddons(); $("#ad-ok")?.focus(); });
  const ok = $("#ad-ok"), go = $("#ad-go");
  if (ok) ok.onchange = () => { go.disabled = !ok.checked; };
  if ($("#ad-cancel")) $("#ad-cancel").onclick = () => { R.open = null; renderAddons(); };
  if (go) go.onclick = async () => {
    const id = R.open;
    go.disabled = true;
    try { const r = await api(`/api/addons/${id}/install`, { json: { accept: ok.checked } }); R.open = null; followInstall(id, r.job); }
    catch (e) { go.disabled = false; showError(e.message, "Couldn't install"); }
  };
  $$("#ad-list [data-rm]").forEach(b => {
    const a = list.find(x => x.id === b.dataset.rm);
    confirmClick(b, `Remove ${a.name}? Click again`, async () => {
      const r = await api(`/api/addons/${a.id}/remove`, { json: {} });
      showError(`${a.name} is removed (${gb(r.freed || 0)} freed).${a.engine ? " SpeakEasy reads with Chatterbox again." : ""}`, "Add-on removed", null, true);
      loadAddons();
    });
  });
  $$("#ad-list [data-use]").forEach(c => c.onchange = async () => {
    c.disabled = true;
    try { await api(`/api/addons/${c.dataset.use}/use`, { json: { on: c.checked } }); }
    catch (e) { c.checked = !c.checked; showError(e.message, "Couldn't switch"); }
    loadAddons();
  });
}

function followInstall(id, jobId) {
  R.jobs[id] = { job: jobId, frac: 0, msg: "Starting" };
  renderAddons();
  const off = watchJob(jobId, ev => {
    const st = R.jobs[id]; if (!st) return;
    const f = ev.frac ?? ev.fraction ?? ev.progress;
    if (f != null) st.frac = f;
    if (ev.message) st.msg = ev.message;
    if (ev.total_left != null) st.left = leftText(ev.total_left);
    const bar = $(`#ad-bar-${id}`), msg = $(`#ad-msg-${id}`), left = $(`#ad-left-${id}`);
    if (bar) bar.style.width = `${Math.round((st.frac || 0) * 100)}%`;
    if (msg) msg.textContent = st.msg;
    if (left) left.textContent = st.left || "";
    if (ev.final) {
      off();
      delete R.jobs[id];
      if (ev.state === "error") showError(ev.message, "Couldn't install");
      else if (ev.state === "done") showError(ev.message, "Add-on installed", null, true);
      loadAddons();
    }
  });
}

// ------------------------------------------------------------ Storage
const GCOL = { library: "var(--c-glib)", working: "var(--c-gwork)", models: "var(--c-gmodels)" };

async function loadStorage() {
  let s;
  try { s = R.storage = await api("/api/storage"); }
  catch (e) { $("#st-places").innerHTML = `<p class="meta">${esc(e.message)}</p>`; return; }
  const g = s.groups, total = Object.values(g).reduce((n, x) => n + x.size, 0) || 1;
  $("#st-stack").innerHTML = Object.entries(g).map(([k, x]) => `<i style="width:${(x.size / total * 100).toFixed(2)}%;background:${GCOL[k]}" title="${esc(x.label)} ${gb(x.size)}"></i>`).join("");
  $("#st-legend").innerHTML = Object.entries(g).map(([k, x]) => `<span><i class="sw" style="background:${GCOL[k]}"></i>${esc(x.label)} <b>${gb(x.size)}</b></span>`).join("");
  $("#room-n-storage").textContent = gb(total);
  const moving = runningJobs().find(j => j.kind === "move");
  if (moving && !R.moving) followMove(moving);
  renderPlaces();
  if (typeof vaultLoad === "function") vaultLoad();   // the Vault section (vault.js)
}

function renderPlaces() {
  const s = R.storage; if (!s) return;
  $("#st-places").innerHTML = Object.entries(s.groups).map(([k, x]) => `
    <div class="place" style="--gc:${GCOL[k]}" data-g="${k}">
      <i class="gsw"></i>
      <div style="display:grid;gap:2px;min-width:0"><b>${esc(x.label)}</b><span class="meta">${esc(x.about)}</span></div>
      <span class="size">${gb(x.size)}</span>
      <div class="full row spread"><span class="folder">${esc(x.folder)}</span>
        ${R.move?.group === k || R.moving ? "" : `<button class="btn" type="button" data-change="${k}" data-pc>Change…</button>`}</div>
      <div class="full meta mono" data-deep>${Object.entries(x.keys).map(([kk, v]) => `[paths] ${esc(kk)} = ${esc(v)}`).join(" · ")} · ${x.files} files</div>
      ${R.move?.group === k ? movePanel(k, x) : ""}
      ${R.moving?.group === k ? `<div class="full inprog"><div class="row spread"><span class="meta" id="mv-msg">${esc(R.moving.msg || "Starting")}</span>
        <span class="meta" id="mv-left">${esc(R.moving.left || "")}</span></div>
        <div class="bar"><i id="mv-bar" style="width:${Math.round((R.moving.frac || 0) * 100)}%"></i></div>
        <span class="meta">Copied, then every file checked, then the old copies removed.</span></div>` : ""}
    </div>`).join("");
  $$("#st-places [data-change]").forEach(b => b.onclick = () => {
    const k = b.dataset.change, here = s.groups[k].folder;
    const other = s.drives.find(d => !here.toUpperCase().startsWith(d.drive.toUpperCase()) && !d.removable) || s.drives[0];
    R.move = { group: k, folder: other ? `${other.drive}\\UnderCover` : here };
    R.check = null;
    renderPlaces(); runCheck();
  });
  wireMove();
}

function movePanel(k, x) {
  const s = R.storage, cur = (R.move.folder || "").slice(0, 2).toUpperCase();
  const subs = Object.keys(x.keys).map(v => v.replace(/_dir$/, "")).join(", ");
  const c = R.check;
  const verdict = !c ? "Checking…" : c.ok ? `Needs ${gb(c.need)} · ${esc(c.folder.slice(0, 2))} has ${gb(c.free)} free ✓${c.same_drive ? " · same drive, so it's instant" : c.minutes ? ` · about ${Math.max(1, Math.round(c.minutes))} min` : ""}` : esc(c.why);
  return `<div class="full move">
    <div class="meta">Pick a drive, or type a folder. ${esc(x.label)}'s folders (<span class="mono">${esc(subs)}</span>) go inside it.</div>
    <div class="drives">${s.drives.map(d => `<button class="drive" type="button" data-drive="${esc(d.drive)}" aria-pressed="${cur === d.drive.toUpperCase()}">
      <b>${esc(d.drive)}${x.folder.toUpperCase().startsWith(d.drive.toUpperCase()) ? ` <small>(now)</small>` : ""}</b>
      <small>${d.removable ? "USB · " : ""}${gb(d.free)} free of ${gb(d.total)}</small><span class="dbar"><i style="width:${Math.round((1 - d.free / d.total) * 100)}%"></i></span></button>`).join("")}</div>
    <div class="mfield"><input id="mv-folder" value="${esc(R.move.folder)}" aria-label="New folder" spellcheck="false" autocomplete="off">
      <button class="btn primary" type="button" id="mv-go" ${c?.ok ? "" : "disabled"}>Move ${esc(x.label.toLowerCase())} here</button></div>
    <div class="verdict ${c ? (c.ok ? "ok" : "bad") : ""}" id="mv-verdict">${verdict}</div>
    <p class="meta">Your files are copied, checked, and only then removed from the old place. Nothing else can run while they move.
      <button class="linkbtn" type="button" id="mv-cancel">Cancel</button></p>
  </div>`;
}

let checkTimer = 0;
function runCheck() {
  clearTimeout(checkTimer);
  checkTimer = setTimeout(async () => {
    const m = R.move; if (!m) return;
    try { R.check = await api("/api/storage/check", { json: { group: m.group, folder: m.folder } }); }
    catch (e) { R.check = { ok: false, why: e.message }; }
    if (R.move !== m) return;
    const v = $("#mv-verdict"), go = $("#mv-go"), c = R.check;
    if (!v) return;
    v.className = `verdict ${c.ok ? "ok" : "bad"}`;
    v.innerHTML = c.ok ? `Needs ${gb(c.need)} · ${esc(c.folder.slice(0, 2))} has ${gb(c.free)} free ✓${c.same_drive ? " · same drive, so it's instant" : c.minutes ? ` · about ${Math.max(1, Math.round(c.minutes))} min` : ""}` : esc(c.why);
    go.disabled = !c.ok;
  }, 250);
}

function wireMove() {
  const inp = $("#mv-folder"); if (!inp) return;
  inp.oninput = () => {
    R.move.folder = inp.value; R.check = null;
    $$("#st-places .drive").forEach(d => d.setAttribute("aria-pressed", inp.value.slice(0, 2).toUpperCase() === d.dataset.drive.toUpperCase()));
    $("#mv-go").disabled = true; $("#mv-verdict").className = "verdict"; $("#mv-verdict").textContent = "Checking…";
    runCheck();
  };
  $$("#st-places .drive").forEach(d => d.onclick = () => {
    const rest = (R.move.folder || "").slice(2) || "\\UnderCover";
    inp.value = d.dataset.drive + (rest.startsWith("\\") ? rest : "\\" + rest);
    inp.oninput();
  });
  $("#mv-cancel").onclick = () => { R.move = null; R.check = null; renderPlaces(); };
  // two clicks: moving the user's files is never one slip away
  confirmClick($("#mv-go"), "Move them now? Click again", async () => {
    const m = R.move;
    try {
      const r = await api("/api/storage/move", { json: { group: m.group, folder: m.folder, confirm: true } });
      R.move = null;
      followMove({ id: r.job, title: "" }, m.group);
    } catch (e) { showError(e.message, "Couldn't move"); runCheck(); }
  });
}

function followMove(job, group) {
  const g = group || Object.keys(R.storage?.groups || {}).find(k => (job.title || "").toLowerCase().includes(R.storage.groups[k].label.toLowerCase())) || "library";
  R.moving = { group: g, frac: 0, msg: "Starting" };
  renderPlaces();
  const off = watchJob(job.id, ev => {
    const st = R.moving; if (!st) return;
    const f = ev.frac ?? ev.fraction ?? ev.progress;
    if (f != null) st.frac = f;
    if (ev.message) st.msg = ev.message;
    if (ev.total_left != null) st.left = leftText(ev.total_left);
    const bar = $("#mv-bar"), msg = $("#mv-msg"), left = $("#mv-left");
    if (bar) bar.style.width = `${Math.round((st.frac || 0) * 100)}%`;
    if (msg) msg.textContent = st.msg;
    if (left) left.textContent = st.left || "";
    if (ev.final) {
      off();
      R.moving = null;
      if (ev.state === "error") showError(ev.message, "Couldn't move");
      else if (ev.state !== "cancelled") showError(ev.message, "Moved", null, true);
      loadStorage();
      if (typeof loadSummary === "function") loadSummary();
    }
  });
}

// ------------------------------------------------------------ System: the UnderCover row and the Doctor's sweep (phase 10)
// /api/doctor: fast checks (+ the last full run); a row per problem with button[data-fix]; long fixes and "Check everything"
// are jobs (kind "doctor") on the shared feed. The diagnostics file: its contents are shown before it's saved.
const D = { rep: null, watching: {}, ask: null };   // ask: a Fix waiting for its second button ({ id, preview })
const ASK_FIRST = new Set(["packages"]);   // Fixes that install something: named first, installed on the second click
const DSTATE = { bad: ["bad", "problem"], warn: ["warn", "check"], info: ["now", "note"], ok: ["ok", "fine"] };
async function loadDoctor() {
  try { D.rep = await api("/api/doctor"); }
  catch (e) { $("#sys-doctor").innerHTML = `<p class="meta">${esc(e.message)}</p>`; return; }
  renderDoctor();
  if (D.rep.full_job) watchFix(D.rep.full_job, "#sys-doctor-full", "Check everything");
}
function doctorRow(r) {
  const [cls, word] = DSTATE[r.state] || DSTATE.info, f = r.fix;
  const asking = f && D.ask?.id === f.id;
  const act = !f ? "" : f.how === "offer" ? `<small class="meta">${esc(f.label)}</small>`
    : asking ? "" : `<button class="btn" type="button" data-fix="${esc(f.id)}" data-pc>${esc(f.label)}</button>`;
  return `<div class="sys" data-check="${esc(r.id)}"><span><b>${esc(r.title)}</b><small>${esc(r.say)}</small>${r.detail ? `<span class="kv2" data-deep>${esc(r.detail)}</span>` : ""}</span>
    <span class="acts"><span class="dotw ${cls}">${esc(word)}</span>${act}</span>${asking ? fixAsk(D.ask) : ""}</div>`;
}
// a Fix that installs says exactly what first (each package, the version it puts back, the version there now)
function fixAsk(a) {
  const pv = a.preview, list = pv?.packages || [], refused = pv?.refused || [];
  if (!pv) return `<div class="fix-ask"><p class="meta">Checking what it would install…</p></div>`;
  const rest = refused.length ? ` ${esc(refused.join(", "))} need${refused.length === 1 ? "s" : ""} the installer's repair instead.` : "";
  return `<div class="fix-ask">${list.length ? `<p>This installs ${list.length === 1 ? "one package" : `${list.length} packages`} from UnderCover's pinned list, each checked against its checksum:</p>
      <ul class="fix-pkgs">${list.map(x => `<li><b>${esc(x.name)}</b><span class="mono">${esc(x.version || "")}</span><small class="meta">${x.now ? `now ${esc(x.now)}` : "missing"}</small></li>`).join("")}</ul>
      <p class="meta">Nothing else changes, and PyTorch never does here.${rest} Restart UnderCover afterwards.</p>`
    : `<p class="meta">Nothing to reinstall right now.${rest}</p>`}
    <div class="row">${list.length ? `<button class="btn primary" type="button" data-fix-go="${esc(a.id)}">Install ${list.length === 1 ? "it" : `these ${list.length}`}</button>` : ""}
      <button class="linkbtn" type="button" data-fix-cancel>Cancel</button></div></div>`;
}
function renderDoctor() {
  const r = D.rep, a = r.about || {};
  $("#sys-version").textContent = `Version ${a.version}${a.git && a.git.branch ? ` · ${a.git.branch}` : ""}`;
  $("#sys-about-deep").textContent = [a.folder, `Python ${a.python}`, `PyTorch ${a.torch || "not found"}`, a.git && a.git.commit, a.os].filter(Boolean).join(" · ");
  // a row per problem; notes that carry a Fix (leftovers, the install record) or tell what happened (interrupted work) too
  const rows = r.results.filter(x => x.state === "bad" || x.state === "warn" || (x.state === "info" && (x.fix || x.id === "interrupted")));
  const order = { bad: 0, warn: 1, info: 2 };
  rows.sort((x, y) => order[x.state] - order[y.state]);
  const fine = r.results.filter(x => x.state === "ok");
  $("#sys-doctor").innerHTML = (rows.length ? rows.map(doctorRow).join("")
    : `<p class="meta">All clear: ${r.results.length} checks passed.</p>`) +
    (fine.length ? `<div data-deep>${fine.map(doctorRow).join("")}</div>` : "");
  const lf = r.last_full;
  $("#sys-doctor-when").textContent = `Quick check ${r.seconds} s` + (lf ? ` · last full check ${lf.made.replace("T", " ").slice(0, 16)}, ${lf.counts.bad + lf.counts.warn} problem(s)` : " · no full check yet");
  document.body.dataset.doctor = r.counts.bad ? "bad" : r.counts.warn ? "warn" : "ok";   // hook: a dot on SafeHouse / System
}
// sel: the button's selector, looked up at every event (the Sweep list is redrawn while a fix runs)
function watchFix(jobId, sel, label) {
  const btn = () => $(sel);
  if (btn()) btn().disabled = true;
  if (D.watching[jobId]) return;
  D.watching[jobId] = true;
  const off = watchJob(jobId, ev => {
    const f = ev.frac ?? ev.fraction ?? ev.progress, b = btn();
    if (b && !ev.final) { b.disabled = true; b.textContent = f != null ? `${Math.round(f * 100)}%` : "Working…"; }
    if (ev.final) {
      off();
      delete D.watching[jobId];
      if (b) { b.disabled = false; b.textContent = label; }
      if (ev.state === "error") showError(ev.message, "That didn't work");
      else if (ev.state === "done") showError(ev.message, "Done", null, true);
      loadDoctor();
    }
  });
}
$("#sys-doctor").addEventListener("click", async e => {
  if (e.target.closest("[data-fix-cancel]")) { D.ask = null; return renderDoctor(); }
  const go = e.target.closest("[data-fix-go]");
  if (go) { D.ask = null; renderDoctor(); return runFix(go.dataset.fixGo); }
  const b = e.target.closest("[data-fix]"); if (!b) return;
  const id = b.dataset.fix;
  if (!ASK_FIRST.has(id)) return runFix(id);
  const ask = D.ask = { id, preview: null };
  renderDoctor();
  try { ask.preview = await api(`/api/doctor/fix/${encodeURIComponent(id)}`); }
  catch (err) { if (D.ask === ask) D.ask = null; showError(err.message, "Couldn't check what it installs"); }
  renderDoctor();
});
async function runFix(id) {
  const sel = `[data-fix="${CSS.escape(id)}"]`, label = D.rep?.results.find(r => r.fix?.id === id)?.fix.label || "Fix";
  if ($(sel)) $(sel).disabled = true;
  try {
    const r = await api(`/api/doctor/fix/${encodeURIComponent(id)}`, { method: "POST" });
    if (r.job) return watchFix(r.job, sel, label);
    if (r.message) showError(r.message, r.ok === false ? "Not now" : "Done", null, r.ok !== false);
    loadDoctor();
  } catch (err) { showError(err.message, "That didn't work"); if ($(sel)) $(sel).disabled = false; }
}
$("#sys-doctor-full").onclick = async () => {
  try { watchFix((await api("/api/doctor/full", { method: "POST" })).job, "#sys-doctor-full", "Check everything"); }
  catch (err) { showError(err.message, "Couldn't start the check"); }
};
$("#sys-logs").onclick = async () => {
  try { await api("/api/doctor/open-logs", { method: "POST" }); } catch (err) { showError(err.message, "Couldn't open the logs"); }
};
$("#sys-bundle").onclick = async () => {
  let c;
  try { c = await api("/api/doctor/bundle"); } catch (err) { return showError(err.message, "Couldn't list it"); }
  $("#sys-bundle-list").innerHTML = c.files.map(f => `<li><b>${esc(f.about)}</b> <span class="meta" data-deep>${esc(f.name)} · ${(f.size / 1e3).toFixed(0)} KB</span></li>`).join("");
  $("#sys-bundle-size").textContent = `about ${(c.size / 1e6).toFixed(1)} MB before it's zipped`;
  $("#sys-bundle-note").innerHTML = `${esc(c.never)}<span data-deep> ${c.shortened.map(s => `${esc(s.folder)} → ${esc(s.reads)}`).join(" · ")}</span>`;
  $("#sys-bundle-save").setAttribute("download", c.name);
  $("#sys-bundle-box").hidden = false;
};
$("#sys-bundle-cancel").onclick = () => { $("#sys-bundle-box").hidden = true; };
$("#sys-bundle-save").addEventListener("click", () => setTimeout(() => { $("#sys-bundle-box").hidden = true; }, 500));
setTimeout(loadDoctor, 4000);   // once after the page opens, so a problem shows on the tab before the room is opened

// ------------------------------------------------------------ Settings: Phone access (phase 11)
// /api/phone (PC only): the switch, where it listens, the code being shown (a QR code of <url>/pair#<code>), the paired
// phones with Revoke. While a code is shown the room checks every few seconds for the phone that used it.
const PH = { st: null, tick: null, poll: null, seen: null };
async function loadPhone(after) {
  if (document.body.dataset.client === "phone") return;
  try { PH.st = after || await api("/api/phone"); } catch (e) { $("#phone-where").textContent = e.message; return; }
  renderPhone();
}
function renderPhone() {
  const s = PH.st, p = s.pairing;
  $("#set-phone").setAttribute("aria-checked", s.enabled);
  $("#phone-box").hidden = !s.enabled;
  $("#phone-where").textContent = !s.enabled ? "" : s.problem ? s.problem
    : `Listening at ${s.url} on your home network. Only paired phones get in. The first time, Windows may ask whether Python may use the network: allow it on private networks only.`;
  $("#phone-pair").disabled = !s.listening;
  if (s.enabled) phoneChecks(); else { $("#phone-checks").innerHTML = ""; phoneChecksAt = 0; }   // switched on again: read at once
  $("#phone-pairing").hidden = !p;
  if (p) {
    $("#phone-qr").innerHTML = p.qr;   // an SVG made by the server (core/qr.py)
    $("#phone-url").textContent = p.url;
    $("#phone-code").textContent = p.code;
  }
  clearInterval(PH.tick); clearInterval(PH.poll);
  if (p) {
    const left = () => {
      const t = Math.round(p.until - Date.now() / 1000);
      if (t <= 0) { $("#phone-left").textContent = "That code has run out."; clearInterval(PH.tick); return loadPhone(); }
      $("#phone-left").textContent = `Works once, for another ${Math.floor(t / 60)}:${String(t % 60).padStart(2, "0")}`;
    };
    left(); PH.tick = setInterval(left, 1000);
    PH.poll = setInterval(() => { if (R.room === "settings" && !document.hidden) loadPhone(); }, 3000);
  }
  const was = PH.seen; PH.seen = new Set(s.devices.map(d => d.id));
  const fresh = was ? s.devices.find(d => !was.has(d.id)) : null;
  if (fresh) showError(`${fresh.name} is paired. It can use UnderCover while this PC is on and Phone access stays on.`, "Phone paired", null, true);
  $("#phone-devices").innerHTML = s.devices.length ? s.devices.map(d => `
    <div class="sys" data-device="${esc(d.id)}"><span><b>${esc(d.name)}</b><small>paired ${esc(d.paired)} · last seen ${esc(d.seen)}</small>
      <span class="kv2" data-deep>${esc(d.ip || "")}</span></span>
      <span class="acts"><button class="btn" type="button" data-revoke="${esc(d.id)}">Revoke</button></span></div>`).join("")
    : `<p class="meta">No phones paired yet.</p>`;
  $$("#phone-devices [data-revoke]").forEach(b => confirmClick(b, "Revoke? Click again", async () => {
    PH.st = await api("/api/phone/revoke", { json: { id: b.dataset.revoke } }); renderPhone();
  }));
}
// the Doctor's two network rows (home network type, Windows Firewall) where Phone access is switched on; at most every 10 s
let phoneChecksAt = 0;
async function phoneChecks() {
  if (Date.now() - phoneChecksAt < 10000) return;
  phoneChecksAt = Date.now();
  try {
    const rows = (await api("/api/doctor")).results.filter(r => r.group === "network");
    $("#phone-checks").innerHTML = rows.map(doctorRow).join("");
  } catch (e) { $("#phone-checks").innerHTML = ""; }
}
$("#set-phone").onclick = async () => {
  const on = $("#set-phone").getAttribute("aria-checked") !== "true";
  try { PH.st = await api("/api/phone", { json: { on } }); renderPhone(); } catch (e) { showError(e.message, "Couldn't change Phone access"); }
};
$("#phone-pair").onclick = async () => {
  try { PH.st = await api("/api/phone/pair", { json: {} }); renderPhone(); } catch (e) { showError(e.message, "Couldn't make a code"); }
};
$("#phone-pair-x").onclick = async () => {
  try { PH.st = await api("/api/phone/pair/cancel", { json: {} }); renderPhone(); } catch (e) { showError(e.message); }
};
