"use strict";
// SafeHouse rooms (phase 4b, rooms build): the room strip (Library · Running · Storage · Add-ons · System · Settings, deep links
// #safehouse/<room>), the Add-ons room (every part UnderCover is built from; optional add-ons are accepted and installed here)
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
    const use = a.engine ? `<label class="check" style="font-size:13.5px" data-deep><input type="checkbox" data-use="${a.id}" ${a.in_use ? "checked" : ""}><span>Use for cadence speech</span></label>` : "";
    return `<span class="astate ok">Installed ✓</span><span class="meta">added ${day(a.installed_on) || "earlier"} · licence accepted ${day(a.accepted)}</span>${use}
            <button class="btn danger" type="button" data-rm="${a.id}">Remove · frees ${gb(a.disk || a.size_mb * 1e6)}</button>`;
  }
  if (R.open === a.id) return `<span class="astate">Not added</span>`;
  return `<span class="astate">Not added</span><button class="btn primary" type="button" data-add="${a.id}">Accept &amp; install…</button>
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
}

function renderPlaces() {
  const s = R.storage; if (!s) return;
  $("#st-places").innerHTML = Object.entries(s.groups).map(([k, x]) => `
    <div class="place" style="--gc:${GCOL[k]}" data-g="${k}">
      <i class="gsw"></i>
      <div style="display:grid;gap:2px;min-width:0"><b>${esc(x.label)}</b><span class="meta">${esc(x.about)}</span></div>
      <span class="size">${gb(x.size)}</span>
      <div class="full row spread"><span class="folder">${esc(x.folder)}</span>
        ${R.move?.group === k || R.moving ? "" : `<button class="btn" type="button" data-change="${k}">Change…</button>`}</div>
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
