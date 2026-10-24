// Vaulting, the page (a section of SafeHouse's Storage room; the backend is core/vault.py + app/vault_api.py, contract in
// docs/handoffs/vaulting.md). Off until the user picks a drive. Then: where, the last copy, Vault now, what's in the vault
// (the basic view lists only what's missing or changed here) and Restore: missing things come back; a changed one asks
// Keep both / Replace (the current one is vaulted first) / Cancel. Deep Cover: an older copy, the sizes. PC-only actions
// carry data-pc (a phone sees the state). Uses helpers from app.js ($, $$, esc, api, showError, gb, voiceDisplay, store,
// loadVoices), library.js (when, confirmClick, L, loadCat) and rooms.js (R: the drives from /api/storage).
const VT = { s: null, c: null, pick: null, check: null, all: false, snap: null, ask: null, timer: 0, restoreSeen: null };
const VT_KINDS = [["voice", "Voices you trained"], ["song", "Songs"], ["cover", "Covers"], ["speech", "Speech"], ["project", "Training projects"], ["settings", "Settings"]];
const VT_SETTINGS = { "config.toml": "Your settings", "voice_quality.json": "Voice checks", "themes": "Your themes" };
const VT_RANK = { same: 0, missing: 1, different: 2 };
const vtStamp = s => String(s || "").replace(/T(\d\d)-(\d\d)-(\d\d).*$/, " $1:$2:$3");   // 2026-10-13T01-02-03 -> 2026-10-13 01:02:03
const vtWhen = s => { const t = vtStamp(s); return typeof when === "function" ? when(t) : t; };
const vtDrive = f => (String(f || "").match(/^[A-Za-z]:/) || [String(f || "")])[0];

async function vaultLoad() {
  clearTimeout(VT.timer);
  try { VT.s = await api("/api/vault"); } catch (e) { $("#vt-body").innerHTML = `<p class="meta">${esc(e.message)}</p>`; return; }
  const s = VT.s;
  if (s.on && s.connected && s.snapshots && (!(s.running || s.restore?.running) || !VT.c || VT.wasBusy === false)) {   // not every 2 s while a copy runs
    try { VT.c = await api(`/api/vault/contents${VT.snap ? `?snapshot=${encodeURIComponent(VT.snap)}` : ""}`); }
    catch (e) { VT.c = null; }
  } else if (!(s.on && s.connected && s.snapshots)) VT.c = null;
  VT.wasBusy = !!(s.running || s.restore?.running);
  vtRestoreOutcome();
  renderVault();
  if ((s.running || s.restore?.running) && !$("#room-storage").hidden) VT.timer = setTimeout(vaultLoad, 2000);   // while a copy or a restore runs
}
// one row per item, as the server groups them: a song's or speech's files are one item under its id, titled from its own
// record. Voices show their display name, covers their file name; settings group by their first folder (themes/...).
function vtItems(kind) {
  const list = VT.c?.kinds?.[kind] || [];
  if (kind !== "settings") return list.map(x => ({ ...x, name: kind === "voice" ? voiceDisplay(x.item)
    : kind === "cover" ? (x.item === "history.jsonl" ? "The list of your covers" : (x.title || x.item).replace(/\.[^.]+$/, "")) : x.title || x.item }));
  const by = {};
  for (const x of list) {
    const id = x.item.split("/")[0];
    const g = by[id] ||= { item: id, kind, files: 0, bytes: 0, keys: [], here: "same" };
    g.files += x.files; g.bytes += x.bytes; g.keys.push(...x.keys);
    if (VT_RANK[x.here] > VT_RANK[g.here]) g.here = x.here;   // changed beats missing beats here
  }
  return Object.values(by).map(g => ({ ...g, name: VT_SETTINGS[g.item] || g.item }));
}

// ------------------------------------------------------------ the section
function renderVault() {
  const s = VT.s, box = $("#vt-body");
  if (!s) return;
  $("#vt-say").textContent = s.on ? (s.connected ? `on ${vtDrive(s.folder)}` : "its drive isn't connected") : "off";
  if (VT.pick) { box.innerHTML = vtPicker(); return vtWirePicker(); }
  if (!s.on) {
    box.innerHTML = `<p class="vt-lead">A copy of what can't be downloaded again (the voices you trained, your songs, covers, speech and settings) on another drive, made quietly a little at a time, so one dead disk doesn't take them. It never deletes anything from your library.</p>
      <div class="row"><button class="btn" type="button" id="vt-choose" data-pc>Choose a drive…</button></div>`;
    $("#vt-choose")?.addEventListener("click", vtOpenPicker);
    return;
  }
  const last = s.last, p = s.progress || {};
  const line = s.running ? `<div class="inprog"><div class="row spread"><span class="meta">${esc(p.message || "Copying")}</span></div><div class="bar"><i style="width:${Math.round((p.fraction || 0) * 100)}%"></i></div></div>`
    : !s.connected ? `<p class="vt-warn">The vault's drive (${esc(vtDrive(s.folder))}) isn't connected. UnderCover copies again when it's back.</p>`
    : last ? `<p>Last copy ${esc(vtWhen(last.made))}: ${last.new ? `${last.new} new of ${last.files} files` : `all ${last.files} files, nothing new`}. ${s.snapshots} cop${s.snapshots === 1 ? "y" : "ies"} kept.</p>`
    : `<p>No copy yet. The first one starts when UnderCover is idle, or now.</p>`;
  box.innerHTML = `${line}${s.error ? `<p class="vt-warn">The last copy stopped: ${esc(s.error)}</p>` : ""}
    ${s.restore?.running ? `<p class="meta">Restoring: the current files are copied into the vault first, then the old ones come back…</p>` : ""}
    <p class="meta mono" data-deep>${esc(s.folder)} · ${s.size === "everything" ? "everything I'd miss" : "what matters"}${s.raw ? ` · ${gb(s.raw)} kept in ${gb(s.stored)} (${Math.round(s.stored / s.raw * 100)}%)` : ""}${last ? ` · last run ${last.seconds} s, ${last.reason || ""}` : ""}</p>
    <div class="row"><button class="btn" type="button" id="vt-now" data-pc ${s.running || !s.connected ? "disabled" : ""}>${s.running ? "Copying…" : "Vault now"}</button>
      <button class="linkbtn" type="button" id="vt-change" data-pc>Change…</button><button class="linkbtn quiet" type="button" id="vt-off" data-pc>Switch off</button></div>
    ${VT.c ? vtContents() : ""}`;
  $("#vt-now")?.addEventListener("click", async e => { e.target.disabled = true; try { VT.s = await api("/api/vault/run", { json: {} }); } catch (err) { showError(err.message, "Couldn't start the copy"); } vaultLoad(); });
  $("#vt-change")?.addEventListener("click", vtOpenPicker);
  if ($("#vt-off")) confirmClick($("#vt-off"), "Switch it off? The copies stay on the drive", async () => {
    try { await api("/api/vault/settings", { json: { folder: "" } }); } catch (e) { showError(e.message, "Couldn't switch it off"); }
    vaultLoad();
  });
  vtWireContents();
}

// ------------------------------------------------------------ what's in the vault, and Restore
function vtContents() {
  const c = VT.c, kinds = VT_KINDS.filter(([k]) => c.kinds?.[k]?.length);
  const rows = kinds.map(([k, label]) => {
    const items = vtItems(k), off = items.filter(x => x.here !== "same");
    const show = VT.all ? items : off;
    return `<div class="vt-kind"><div class="vt-kh"><b>${label}</b><span class="meta">${items.length}${off.length ? ` · ${off.filter(x => x.here === "missing").length ? `${off.filter(x => x.here === "missing").length} missing here` : ""}${off.filter(x => x.here === "missing").length && off.filter(x => x.here === "different").length ? ", " : ""}${off.filter(x => x.here === "different").length ? `${off.filter(x => x.here === "different").length} changed since` : ""}` : " · all here ✓"}</span></div>
      ${show.map(x => vtRow(x)).join("")}</div>`;
  }).join("");
  const total = kinds.reduce((n, [k]) => n + vtItems(k).length, 0), anyOff = kinds.some(([k]) => vtItems(k).some(x => x.here !== "same"));
  return `<div class="vt-in"><div class="rn-lbl"><b>What's in the vault</b><small>${esc(c.snapshot === c.snapshots?.at(-1) ? "the latest copy" : `the copy of ${vtWhen(c.snapshot)}`)}${anyOff ? "" : ": everything in it is here, as it was"}</small>
      ${c.snapshots?.length > 1 ? `<span class="end" data-deep><select id="vt-snap" aria-label="Which copy">${[...c.snapshots].reverse().map(x => `<option value="${esc(x)}" ${x === c.snapshot ? "selected" : ""}>${esc(vtStamp(x))}</option>`).join("")}</select></span>` : ""}</div>
    ${rows}
    <button class="linkbtn" type="button" id="vt-all">${VT.all ? "Show only what's missing or changed" : `Show everything in it (${total})`}</button></div>`;
}
function vtRow(x) {
  const key = `${x.kind}:${x.item}`, asking = VT.ask === key;
  const state = x.here === "same" ? `<span class="st ok">here ✓</span>` : x.here === "missing" ? `<span class="st warn">missing here</span>` : `<span class="st now">changed since this copy</span>`;
  const act = x.here === "missing" ? `<button class="btn" type="button" data-vt-back="${esc(key)}" data-pc>Bring it back</button>`
    : x.here === "different" && !asking ? `<button class="linkbtn" type="button" data-vt-ask="${esc(key)}" data-pc>Restore…</button>` : "";
  return `<div class="vt-row"><span><b>${esc(x.name)}</b>${facts([{ t: `${x.files} file${x.files === 1 ? "" : "s"}`, deep: true }, { t: gb(x.bytes), deep: true }])}</span><span class="row">${state}${act}</span>
    ${asking ? `<div class="vt-choice"><span>The one here has changed since this copy. Bring the vault's back how?</span>
      <button class="btn" type="button" data-vt-how="both">Keep both</button><button class="btn" type="button" data-vt-how="replace">Replace it</button><button class="linkbtn quiet" type="button" data-vt-how="cancel">Cancel</button>
      <p class="meta">Keep both puts the vault's copy beside yours${x.kind === "voice" ? ` (as “${esc(x.name)} from vault”)` : x.kind === "project" ? " (as its own project)" : ""}. Replace copies the current one into the vault first, so nothing is lost.</p></div>` : ""}</div>`;
}
function vtWireContents() {
  const find = key => { const [k, ...rest] = key.split(":"); return vtItems(k).find(x => x.item === rest.join(":")); };
  $("#vt-all")?.addEventListener("click", () => { VT.all = !VT.all; renderVault(); });
  $("#vt-snap")?.addEventListener("change", e => { VT.snap = e.target.value; VT.ask = null; vaultLoad(); });
  $$("[data-vt-ask]").forEach(b => b.onclick = () => { VT.ask = b.dataset.vtAsk; renderVault(); });
  $$("[data-vt-back]").forEach(b => b.onclick = () => vtRestore(find(b.dataset.vtBack), "skip", b));
  $$("[data-vt-how]").forEach(b => b.onclick = () => {
    if (b.dataset.vtHow === "cancel") { VT.ask = null; return renderVault(); }
    vtRestore(find(VT.ask), b.dataset.vtHow, b);
  });
}
async function vtRestore(x, how, b) {
  if (!x) return;
  if (b) { b.disabled = true; b.textContent = "Bringing it back…"; }
  try {
    const r = await api("/api/vault/restore", { json: { keys: x.keys, on_conflict: how, snapshot: VT.snap || undefined } });
    VT.ask = null;
    if (r.started) {   // Replace runs in the background (its outcome comes in GET's "restore"); it makes a new copy first,
      VT.snap = VT.snap || VT.c?.snapshot; VT.restoreName = x.name; VT.restoreSeen = null;   // so the view stays on the one restored from
    }
    else vtSaid(r, x.name, how);
  } catch (e) { showError(e.message, "Couldn't restore it"); }
  vaultLoad();
}
function vtSaid(r, name, how) {
  const n = (r.restored || []).length;
  showError(n && how === "both" ? `The vault's copy of ${name} is back beside yours, marked “from vault”.` : n ? `${name} is back${(r.skipped || []).length ? ` (${r.skipped.length} changed file${r.skipped.length === 1 ? "" : "s"} left as they are)` : ""}.` : `${name} was already here, as it was.`, "From the vault", null, true);
  if (n) { loadVoices().catch(() => {}); if (typeof L !== "undefined" && typeof loadCat === "function") loadCat(true); }
}
function vtRestoreOutcome() {   // a Replace that finished in the background: say so once
  const r = VT.s?.restore;
  if (!r || r.running || VT.restoreSeen === r || (!r.result && !r.error)) return;
  const sig = JSON.stringify(r);
  if (store.get("uc-vault-restore-seen", "") === sig) return;
  store.set("uc-vault-restore-seen", sig); VT.restoreSeen = r;
  if (r.error) showError(r.error, "The restore stopped");
  else vtSaid(r.result, VT.restoreName || "It");
}

// ------------------------------------------------------------ choosing the drive (the Storage room's drive picker, the vault's check)
function vtOpenPicker() {
  const drives = (typeof R !== "undefined" && R.storage?.drives) || [];
  const other = drives.find(d => !d.app && !d.removable) || drives.find(d => !d.app) || drives[0];
  VT.pick = { folder: VT.s?.folder || (other ? `${other.drive}\\UnderCover Vault` : ""), size: VT.s?.size || "matters" };
  VT.check = null; renderVault(); vtRunCheck();
}
function vtPicker() {
  const drives = (typeof R !== "undefined" && R.storage?.drives) || [], cur = vtDrive(VT.pick.folder).toUpperCase(), c = VT.check;
  return `<div class="move vt-pick">
    <div class="meta">Pick another drive (a USB disk is fine), or type a folder. The vault goes in a folder called “UnderCover Vault” there.</div>
    <div class="drives">${drives.map(d => `<button class="drive" type="button" data-drive="${esc(d.drive)}" aria-pressed="${cur === d.drive.toUpperCase()}">
      <b>${esc(d.drive)}${d.app ? ` <small>(where UnderCover is installed)</small>` : ""}</b>${facts([d.removable ? "USB" : "", `${gb(d.free)} free of ${gb(d.total)}`])}<span class="dbar"><i style="width:${Math.round((1 - d.free / d.total) * 100)}%"></i></span></button>`).join("")}</div>
    <div class="mfield"><input id="vt-folder" value="${esc(VT.pick.folder)}" aria-label="Vault folder" spellcheck="false" autocomplete="off"></div>
    <div class="vt-size"><span class="meta">What to keep</span><span class="seg" id="vt-size" role="group" aria-label="What to keep">
      <button type="button" data-v="matters" aria-pressed="${VT.pick.size === "matters"}">What matters<small>voices, songs, covers, speech, settings</small></button>
      <button type="button" data-v="everything" aria-pressed="${VT.pick.size === "everything"}">Everything I'd miss<small>also the training clips: bigger</small></button></span></div>
    <div class="verdict ${c ? (c.ok ? "ok" : "bad") : ""}" id="vt-verdict">${vtVerdict(c)}</div>
    <div class="row"><button class="btn primary" type="button" id="vt-go" ${c?.ok ? "" : "disabled"}>${VT.s?.on ? "Use this" : "Keep the vault here"}</button><button class="linkbtn" type="button" id="vt-cancel">Cancel</button></div>
  </div>`;
}
const vtVerdict = c => !c ? "Checking…" : c.ok ? `Needs about ${gb(c.need)} · ${esc(vtDrive(VT.pick.folder))} has ${gb(c.free)} free ✓${c.warn ? `<br><span class="vt-warn">${esc(c.warn)}</span>` : ""}` : esc(c.why);
let vtCheckTimer = 0;
function vtRunCheck() {
  clearTimeout(vtCheckTimer);
  vtCheckTimer = setTimeout(async () => {
    const p = VT.pick; if (!p) return;
    try { VT.check = await api(`/api/vault/check?folder=${encodeURIComponent(p.folder)}&size=${p.size}`); }
    catch (e) { VT.check = { ok: false, why: e.message }; }
    if (VT.pick !== p || !$("#vt-verdict")) return;
    $("#vt-verdict").className = `verdict ${VT.check.ok ? "ok" : "bad"}`; $("#vt-verdict").innerHTML = vtVerdict(VT.check);
    $("#vt-go").disabled = !VT.check.ok;
  }, 250);
}
function vtWirePicker() {
  const inp = $("#vt-folder");
  inp.oninput = () => {
    VT.pick.folder = inp.value; VT.check = null;
    $$(".vt-pick .drive").forEach(d => d.setAttribute("aria-pressed", vtDrive(inp.value).toUpperCase() === d.dataset.drive.toUpperCase()));
    $("#vt-go").disabled = true; $("#vt-verdict").className = "verdict"; $("#vt-verdict").textContent = "Checking…";
    vtRunCheck();
  };
  $$(".vt-pick .drive").forEach(d => d.onclick = () => { inp.value = `${d.dataset.drive}\\UnderCover Vault`; inp.oninput(); });
  $$("#vt-size button").forEach(b => b.onclick = () => { VT.pick.size = b.dataset.v; $$("#vt-size button").forEach(x => x.setAttribute("aria-pressed", x === b)); VT.check = null; vtRunCheck(); });
  $("#vt-cancel").onclick = () => { VT.pick = null; VT.check = null; renderVault(); };
  $("#vt-go").onclick = async () => {
    const p = VT.pick, b = $("#vt-go"), first = !VT.s?.on;
    b.disabled = true;
    try {
      VT.s = await api("/api/vault/settings", { json: { folder: p.folder, size: p.size } });
      VT.pick = null;
      if (first) await api("/api/vault/run", { json: {} });   // the first copy starts now; later ones come by themselves
    } catch (e) { b.disabled = false; return showError(e.message, "Couldn't use that folder"); }
    vaultLoad();
  };
}
