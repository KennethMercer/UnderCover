"use strict";
// SafeHouse (was the Library tab): everything UnderCover keeps on disk (covers, songs + stems, voices, training projects, clean up).
// Uses helpers from app.js ($, $$, esc, api, showError, store, fmtTime, fmtMB, S, VOL, renderSong, loadVoices,
// loadHistory, syncControls, saveOpts, refreshEstimate, peaksFromUrl, drawWave), showTab (shell.js) and openProject (train.js).

const L = { cat: store.get("uc-lib-cat", "covers"), data: {}, sel: null, checked: new Set(), summary: null,
            audio: new Audio(), peaks: null, src: "url" };
const CATS = [["covers", "Covers"], ["speech", "Speech"], ["songs", "Songs"], ["voices", "Voices"], ["training", "Training"]];
const CAT_LABELS = { covers: "Covers", speech: "Speech", songs: "Songs", stems: "Stems", voices: "Voices", training: "Training",
                     temporary: "Temporary files", app: "App models" };
const SORTS = {
  covers: [["new", "Newest first"], ["old", "Oldest first"], ["name", "Name"], ["big", "Biggest first"], ["voice", "Voice"]],
  speech: [["new", "Newest first"], ["old", "Oldest first"], ["name", "Name"], ["long", "Longest first"], ["voice", "Voice"]],
  songs: [["used", "Recently used"], ["name", "Name"], ["big", "Biggest first"], ["covers", "Most covers"]],
  voices: [["name", "Name"], ["used", "Most used"], ["big", "Biggest first"], ["new", "Newest first"]],
  training: [["new", "Recently changed"], ["name", "Name"], ["big", "Biggest first"]],
};
const gb = b => b >= 1e9 ? `${(b / 1e9).toFixed(1)} GB` : b >= 1e6 ? `${Math.round(b / 1e6)} MB` : `${Math.max(1, Math.round(b / 1e3))} kB`;
const hue = s => { let h = 0; for (const c of String(s)) h = (h * 31 + c.charCodeAt(0)) % 360; return h; };
const art = (name, big) => `<span class="art" style="background:linear-gradient(135deg,hsl(${hue(name)} 55% 45%),hsl(${(hue(name) + 70) % 360} 50% 35%))"${big ? "" : ' aria-hidden="true"'}>${esc(initialOf(name))}</span>`;
const when = s => { if (!s) return ""; const d = new Date(typeof s === "number" ? s * 1000 : s.replace(" ", "T")); if (isNaN(d)) return s;
  const day = x => new Date(x.getFullYear(), x.getMonth(), x.getDate()), days = Math.round((day(new Date()) - day(d)) / 864e5);   // calendar days, not 24-hour spans
  return days < 1 ? `today ${d.toTimeString().slice(0, 5)}` : days < 2 ? "yesterday" : days < 30 ? `${days} days ago` : d.toLocaleDateString(); };
const quality = v => v.trained ? (v.undertrained ? ["warn", "under-trained"] : ["ok", "trained here"])
  : v.verdict === "good" ? ["ok", "sounds like them ✓"] : v.verdict === "weak" ? ["warn", "weak likeness"]
  : v.verdict === "bad" ? ["bad", "doesn't sound like them"] : v.verdict === "broken" ? ["bad", "won't load"] : ["", "not checked"];

// ------------------------------------------------------------ loading
async function libShow() {
  L.visible = true;
  renderCats();
  await Promise.all([loadSummary(), loadCat()]);
}
function libHide() { L.visible = false; L.audio.pause(); }
document.addEventListener("uc:module", e => { if (e.detail === "safehouse") libShow(); else if (L.visible) libHide(); });
// a cover, speech, voice or training that finishes while the Library is open: its lists and counts catch up
const LIB_KINDS = new Set(["cover", "auto", "speech", "install", "train", "train-auto", "train-export", "night"]);
document.addEventListener("uc:jobdone", e => { if (L.visible && LIB_KINDS.has(e.detail?.kind)) refreshAll(true); });

async function loadSummary() {
  try { L.summary = await api("/api/library/summary"); } catch (e) { return; }
  const s = L.summary, total = s.total || 1;
  const order = ["covers", "speech", "songs", "stems", "voices", "training", "temporary", "app"];
  $("#lib-stack").innerHTML = order.map(k => `<i style="width:${(s.categories[k] / total * 100).toFixed(2)}%;background:var(--c-${k})" title="${CAT_LABELS[k]} ${gb(s.categories[k])}"></i>`).join("");
  $("#lib-legend").innerHTML = order.map(k => `<span><i class="sw" style="background:var(--c-${k})"></i>${CAT_LABELS[k]} <b>${gb(s.categories[k])}</b></span>`).join("");
  $("#lib-total").textContent = gb(s.total);
  $("#lib-free").textContent = `on drive ${s.drive} · ${gb(s.free)} free`;
  // System room (phase 16): the YouTube downloader heals itself; "Check for a fix" runs the same check by hand
  $("#lib-ytdlp").textContent = s.ytdlp ? `yt-dlp ${s.ytdlp} + solver ${s.ytdlp_ejs || "missing"} · ${s.ytdlp_source === "side" ? `a checked update (pinned ${s.ytdlp_pinned})` : "the pinned pair"}` +
    (s.ytdlp_checked ? ` · last check ${s.ytdlp_checked.replace("T", " ").slice(0, 16)}: ${s.ytdlp_last}` : "") : "yt-dlp isn't installed";   // Deep Cover
  $("#lib-ytdlp-st").textContent = !s.ytdlp ? "Not installed." : s.ytdlp_job ? "Looking for a working version…"
    : s.ytdlp_broken ? "YouTube is refusing videos: a newer version usually fixes it." : !s.ytdlp_ejs ? "Its puzzle solver is missing: most videos will be refused."
    : `Working (${s.ytdlp}).`;
  $("#lib-ytdlp-auto").checked = !!s.ytdlp_auto;
  if (s.ytdlp_job && !L.ytdlpWatch) ytdlpWatch(s.ytdlp_job);
  renderKf();   // the tabs' totals and the chips' numbers
}
// System room: YouTube changes often; the check fetches PyPI's newest yt-dlp + its solver, tests it, and uses it only if
// it works at least as well (else keeps or goes back to the last known good). A job: progress on the button.
function ytdlpWatch(id) {
  const go = $("#lib-ytdlp-go");
  L.ytdlpWatch = id; go.disabled = true;
  const off = watchJob(id, ev => {
    if (!ev.final) { go.textContent = ev.frac != null ? `Checking… ${Math.round(ev.frac * 100)}%` : "Checking…"; return; }
    off(); L.ytdlpWatch = null; go.disabled = false; go.textContent = "Check for a fix";
    showError(ev.message, ev.state === "error" ? "Couldn't check" : "YouTube downloader", null, ev.state !== "error");
    loadSummary();
  });
}
$("#lib-ytdlp-go").onclick = async () => {
  try { ytdlpWatch((await api("/api/library/tools/ytdlp-update", { json: {} })).job); }
  catch (e) { showError(e.message, "Couldn't check"); }
};
$("#lib-ytdlp-auto").onchange = async e => {
  try { await api("/api/library/tools/ytdlp-auto", { json: { on: e.target.checked } }); }
  catch (err) { showError(err.message, "Couldn't change it"); e.target.checked = !e.target.checked; }
};
confirmClick($("#lib-quit"), "Quit UnderCover?", async () => {
  try {
    const r = await api("/api/quit", { json: {} });
    document.body.innerHTML = `<div class="lib-empty" style="margin:20vh auto;max-width:420px;text-align:center"><b>UnderCover has stopped</b>${r.pausing ? "Voice work was paused; Resume continues it next time. " : ""}Start it again with UnderCover.bat, then reload this page.</div>`;
  } catch (e) { showError(e.message, "Couldn't quit"); }
});
async function loadCat(keepSel) {
  try { L.data[L.cat] = await api(`/api/library/${L.cat}`); if (L.cat === "voices") noteVoices(L.data.voices); }
  catch (e) { return showError(e.message, "Couldn't read the library"); }
  if (!keepSel) { L.sel = null; L.checked.clear(); }
  renderTools(); renderList(); renderDetail();
}
const refreshAll = async keepSel => { await Promise.all([loadSummary(), loadCat(keepSel)]); };

// ------------------------------------------------------------ chips, tools
// The numbers follow the tab that's showing: under Kept each chip counts its kept things, under Recent only covers and
// speech have a number (the others have no recent tray). The tabs count every category. (The user, 2026-10-12: "Covers 1"
// under an empty Kept made no sense.)
const kfNow = () => hasTray() ? L.kf : "kept";
function tabCounts(kf) {
  const t = L.summary?.tabs?.[kf];
  if (t) return t;
  const c = L.summary?.counts || {};   // a server from before the per-tab counts: totals, as before
  return kf === "kept" ? c : { covers: c.covers, speech: c.speech };
}
function renderCats() {
  const c = tabCounts(kfNow());
  $("#lib-cats").innerHTML = CATS.map(([k, label]) => `<button class="cat" data-cat="${k}" aria-pressed="${k === L.cat}" aria-label="${label}${c[k] != null ? `, ${c[k]}` : ""}"><b>${label}</b>${c[k] != null ? `<small>${c[k]}</small>` : ""}</button>`).join("");
  $$("#lib-cats .cat").forEach(b => b.onclick = () => { if (L.cat === b.dataset.cat) return; L.cat = b.dataset.cat; store.set("uc-lib-cat", L.cat); L.audio.pause(); $("#lib-q").value = ""; renderCats(); loadCat(); });
}
function renderTools() {
  $("#lib-q").placeholder = { covers: "Search covers by song or voice", speech: "Search speech by text or voice", songs: "Search songs", voices: "Search voices", training: "Search training projects" }[L.cat];
  const sort = $("#lib-sort"), cur = store.get(`uc-lib-sort-${L.cat}`, SORTS[L.cat][0][0]);
  sort.innerHTML = SORTS[L.cat].map(([v, t]) => `<option value="${v}"${v === cur ? " selected" : ""}>${t}</option>`).join("");
  const f = $("#lib-filter");
  if (L.cat === "covers" || L.cat === "speech") {
    const voices = [...new Set((L.data[L.cat] || []).map(c => c.voice))].sort();
    f.hidden = voices.length < 2;
    const keep = f.value;
    f.innerHTML = `<option value="">All voices</option>` + voices.map(v => `<option value="${esc(v)}"${v === keep ? " selected" : ""}>${esc(voiceDisplay(v))}</option>`).join("");
  } else f.hidden = true;
  renderKf();
}
// Kept / Recent (phase 6): two tabs over the library. Only covers and speech have a recent tray; songs, voices and training
// are always kept, so for them Recent is off and Kept counts everything.
L.kf = store.get("uc-lib-kf", "kept");
const hasTray = () => L.cat === "covers" || L.cat === "speech";
function renderKf() {
  const tray = hasTray(), sum = o => Object.values(o).reduce((n, v) => n + (+v || 0), 0);
  // Songs, voices and training are always kept: neither tab is lit for them (Kept used to switch itself on), and both say why
  $$("#lib-kfilter .tab").forEach(b => { b.setAttribute("aria-pressed", tray && b.dataset.v === L.kf); b.disabled = !tray; b.title = tray ? "" : "These are always kept"; });
  $("#lib-n-kept").textContent = L.summary ? sum(tabCounts("kept")) : "";
  $("#lib-n-recent").textContent = L.summary ? sum(tabCounts("recent")) : "";
  renderCats();   // the chips' numbers follow the tab
}
$$("#lib-kfilter .tab").forEach(b => b.onclick = () => { L.kf = b.dataset.v; store.set("uc-lib-kf", L.kf); L.sel = null; renderKf(); renderList(); renderDetail(); });
$("#lib-q").addEventListener("input", () => renderList());
$("#lib-sort").onchange = () => { store.set(`uc-lib-sort-${L.cat}`, $("#lib-sort").value); renderList(); };
$("#lib-filter").onchange = () => renderList();

function rows() {
  const q = $("#lib-q").value.trim().toLowerCase(), sort = $("#lib-sort").value, voice = $("#lib-filter").hidden ? "" : $("#lib-filter").value;
  let list = (L.data[L.cat] || []).slice();
  if (L.cat === "voices") list = list.filter(x => !x.waiting_beside);   // a retrained one waiting lives under its owner (renderList)
  const text = x => L.cat === "covers" ? `${x.title} ${x.voice} ${voiceDisplay(x.voice)} ${x.backing_voice || ""} ${voiceDisplay(x.backing_voice)}` : L.cat === "speech" ? `${x.title} ${x.text} ${x.voice} ${voiceDisplay(x.voice)}` : L.cat === "songs" ? `${x.title} ${x.source}`
    : L.cat === "voices" ? `${x.name} ${voiceDisplay(x.name)} ${x.source}` : `${x.person} ${x.name}`;
  if (q) list = list.filter(x => text(x).toLowerCase().includes(q));
  if (voice) list = list.filter(x => x.voice === voice);
  const kf = hasTray() ? L.kf : "";
  if (kf === "kept") list = list.filter(x => x.kept);
  else if (kf === "recent") list = list.filter(x => x.recent);
  const key = { new: x => -(L.cat === "covers" || L.cat === "speech" ? Date.parse((x.created || "").replace(" ", "T")) || 0 : (x.updated || Date.parse((x.installed || "").replace(" ", "T")) / 1000 || 0)),
                old: x => Date.parse((x.created || "").replace(" ", "T")) || 0, name: x => (x.title || x.name || x.person || "").toLowerCase(),
                big: x => -(x.size + (x.stems_size || 0)), long: x => -x.duration, voice: x => x.voice, used: x => L.cat === "songs" ? -x.last_used : -x.covers, covers: x => -x.covers }[sort];
  if (key) list.sort((a, b) => { const ka = key(a), kb = key(b); return ka < kb ? -1 : ka > kb ? 1 : 0; });
  return list;
}
// kept covers and speech were sent to SafeHouse for good; "recent" ones are in the module's tray and roll off unless kept
const keepTag = x => x.kept ? `<span class="tag ok" title="Sent to SafeHouse: kept for good">kept ✓</span>` : x.recent ? `<span class="tag" title="In the recent tray; rolls off when 15 newer ones are made">recent</span>` : "";
const keepLine = x => x.kept ? `Kept in SafeHouse${x.kept_at ? ` since ${when(x.kept_at)}` : ""}.`
  : x.recent ? "In the recent tray: it rolls off (is deleted) after 15 newer ones are made, unless you send it to SafeHouse." : "Not kept, and no longer in the recent tray.";
const idOf = x => String(L.cat === "covers" || L.cat === "speech" || L.cat === "songs" ? x.id : x.name);

// ------------------------------------------------------------ list
// on a phone the detail opens under the tapped row, not below the whole list
const narrowQ = matchMedia("(max-width: 680px)");
function placeDetail() {
  const d = $("#lib-detail"), row = narrowQ.matches && L.sel != null ? $('#lib-list .litem[aria-selected="true"]') : null;
  if (row) row.after(d); else if (d.parentNode === $("#lib-list")) $("#lib-list").after(d);
  d.hidden = narrowQ.matches && !row;
}
narrowQ.addEventListener("change", placeDetail);
// a retrained voice waiting beside this one (voice list's waiting_beside): nested under its owner, with the way to the A/B
function waitingRow(owner, all) {
  const w = all.find(v => v.waiting_beside === owner.name);
  if (!w) return "";
  return `<div class="lib-nest"><span>Retrained version waiting${w.size ? ` (${gb(w.size)})` : ""}</span>
    <button class="linkbtn" type="button" data-lib-ab="${esc(owner.name)}">Hear it</button></div>`;
}
async function libOpenAb(voice) {   // DoubleAgent's card for the voice, with the blind A/B open
  showTab("doubleagent");
  try { await loadCandidates(); await loadImproved(); if (DA.impAb?.voice !== voice) await openImp(voice); } catch (e) { showError(e.message, "Couldn't open it"); }
}
function renderList() {
  const box = $("#lib-list"), list = rows(), all = L.data[L.cat] || [];
  if (box.contains($("#lib-detail"))) box.after($("#lib-detail"));   // the list is redrawn below; the detail lives on
  if (!all.length) {
    // one line and one button, the same shape everywhere (consult 9): "Nothing on file yet." and the way to the first thing
    const go = { covers: ["Make a cover", "frontman"], speech: ["Say something", "speakeasy"], songs: ["Get a song", "frontman"], voices: ["Get a voice", "doubleagent"], training: ["Train a voice", "doubleagent"] }[L.cat];
    box.innerHTML = `<div class="lib-empty empty"><b>Nothing on file yet.</b><button class="btn" type="button" data-goto="${go[1]}">${go[0]}</button></div>`;
    placeDetail();
    return;
  }
  const bulk = L.cat === "covers" && L.checked.size ? `<div class="bulk"><span><b>${L.checked.size} selected</b> · ${gb(all.filter(c => L.checked.has(String(c.id))).reduce((a, c) => a + c.size, 0))}</span>
    <span class="lactions"><a class="btn" href="/api/library/covers/zip?ids=${[...L.checked].join(",")}">Download as .zip</a><button class="btn danger" id="lib-bulk-del">Delete ${L.checked.size}…</button><button class="linkbtn" id="lib-bulk-clear">Clear</button></span></div>` : "";
  box.innerHTML = bulk + (list.length ? list.map(x => {
    const id = idOf(x), check = L.cat === "covers";
    let title, sub, side;
    if (L.cat === "covers") {
      title = x.title; sub = [voiceDisplay(x.voice) + (x.backing_voice ? ` + ${voiceDisplay(x.backing_voice)}` : ""), x.options?.octave ? `octave ${x.options.octave > 0 ? "+" : ""}${x.options.octave}` : "", when(x.created)].filter(Boolean).join(" · ");
      side = keepTag(x) + `<span class="mono">${x.duration ? fmtTime(x.duration) : ""}</span><span class="tag">${esc(x.format)}</span>`;
    } else if (L.cat === "speech") {
      title = x.title; sub = [voiceDisplay(x.voice), paceWord(x), when(x.created)].join(" · ");
      side = keepTag(x) + `<span class="mono">${fmtTime(x.duration)}</span><span class="tag" data-deep>${esc(x.format)}</span>`;
    } else if (L.cat === "songs") {
      title = x.title; sub = [x.source, x.key ? `key ${x.key}` : "", `${x.covers} cover${x.covers === 1 ? "" : "s"}`, `used ${when(x.last_used)}`].filter(Boolean).join(" · ");
      side = (x.sample ? `<span class="tag ok" title="UnderCover's sample song">sample song</span>` : "") + (x.stems.length ? `<span class="tag ${x.stems_old_model ? "warn" : "ok"}">${x.stems_old_model ? "stems: older model" : "ready ✓"}</span>` : "") + `<span class="mono" data-deep>${gb(x.size + x.stems_size)}</span>`;
    } else if (L.cat === "voices") {
      const [cls, word] = quality(x);
      title = voiceDisplay(x.name); sub = [x.demo ? "demo voice · not a real person" : x.trained ? `trained in UnderCover${x.train_minutes ? ` · ${x.train_minutes} min of audio` : ""}` : (x.source.match(/huggingface\.co\/([^/]+\/[^/]+)/)?.[1] || "community"), `used in ${x.covers} cover${x.covers === 1 ? "" : "s"}`].join(" · ");
      side = (x.cadence ? `<span class="tag ok" title="Can speak in SpeakEasy">speaks</span>` : "") + (x.trained ? `<span class="tag ok">trained here</span>` : "") + `<span class="tag ${cls}" data-deep>${word}</span><span class="mono" data-deep>${gb(x.size)}</span>`;
    } else {
      title = x.person || x.name; const st = x.train || {};
      sub = [`${x.kept_minutes} min kept`, st.state === "exported" ? `voice ${st.exported}` : st.state ? st.state : "not trained yet", `changed ${when(x.updated)}`].join(" · ");
      side = `<span class="mono" data-deep>${gb(x.size)}</span>`;
    }
    return `<div class="litem${check ? "" : " nocheck"}" data-id="${esc(id)}" data-kept="${x.kept ? "true" : "false"}" role="option" tabindex="0" aria-selected="${L.sel === id}">
      ${check ? `<input type="checkbox" aria-label="Select ${esc(title)}" ${L.checked.has(id) ? "checked" : ""}>` : ""}${art(title)}
      <span style="min-width:0"><b>${L.cat === "voices" ? vname(x.name, false) : esc(title)}${(L.cat === "covers" || L.cat === "speech") && typeof earMark === "function" ? earMark(L.cat === "covers" ? "cover" : "speech", id) : ""}</b><small>${esc(sub)}</small></span><span class="lside">${side}</span></div>`
      + (L.cat === "voices" ? waitingRow(x, all) : "");
  }).join("") : `<div class="lib-empty">${$("#lib-q").value ? `Nothing matches “${esc($("#lib-q").value)}”.`
      : hasTray() && L.kf === "kept" ? `No ${L.cat === "covers" ? "covers" : "speech"} kept yet. Send to SafeHouse keeps one for good; until then it's under Recent.` : "Nothing here."}</div>`);
  $$("[data-lib-ab]", box).forEach(b => b.onclick = () => libOpenAb(b.dataset.libAb));
  $$(".litem", box).forEach(el => {
    const pick = () => { L.sel = el.dataset.id; $$(".litem", box).forEach(x => x.setAttribute("aria-selected", x === el)); renderDetail(); placeDetail(); };
    el.addEventListener("click", e => { if (e.target.matches("input")) return; pick(); });
    el.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(); } });
    const cb = $("input", el);
    if (cb) cb.onchange = () => { cb.checked ? L.checked.add(el.dataset.id) : L.checked.delete(el.dataset.id); renderList(); };
  });
  const bd = $("#lib-bulk-del");
  if (bd) confirmClick(bd, `Delete ${L.checked.size}?`, async () => {
    releaseAudio([...L.checked]);
    for (const id of [...L.checked]) { try { await api(`/api/library/covers/${id}`, { method: "DELETE" }); } catch (e) { showError(e.message, "Couldn't delete"); break; } }
    L.checked.clear(); afterChange();
  });
  const bc = $("#lib-bulk-clear"); if (bc) bc.onclick = () => { L.checked.clear(); renderList(); };
  if (!L.sel && list.length && innerWidth > 860) { L.sel = idOf(list[0]); box.querySelector(".litem")?.setAttribute("aria-selected", "true"); renderDetail(); }
  placeDetail();
}

// two clicks to do anything destructive
function confirmClick(btn, ask, run) {
  const label = btn.textContent;
  btn.onclick = async () => {
    if (!btn.classList.contains("confirm")) {
      btn.classList.add("confirm"); arm(btn); btn.textContent = ask;
      setTimeout(() => { if (btn.isConnected) { btn.classList.remove("confirm"); disarm(btn); btn.textContent = label; } }, 4000);
      return;
    }
    if (!armedFor(btn)) return;   // a double click isn't a confirmation
    btn.disabled = true;
    releaseAudio();
    try { await run(); } catch (e) { showError(e.message, "That didn't work"); } finally { if (btn.isConnected) btn.disabled = false; }
  };
}
// let go of any file the page is playing, so Windows allows renaming/deleting it
// Windows can't delete a file the page's players still stream: let go of it first (the selected item, or the given ids)
function releaseAudio(ids = L.sel ? [L.sel] : []) {
  const items = (L.data[L.cat] || []).filter(i => ids.includes(idOf(i)));
  const files = items.flatMap(i => [i.url, i.original, i.vocal, i.sample]).filter(Boolean).map(u => decodeURIComponent(u.split("/").pop()));
  const holds = a => files.some(f => decodeURIComponent(a.src || "").endsWith(f));
  if (holds(aCover) && S.result) { stopAll(); $("#result").hidden = true; S.result = null; }   // FrontMan's player had it open
  if (typeof seRelease === "function") seRelease(L.cat === "speech" ? ids[0] : "-");
  for (const a of [L.audio, T.audio, DA.audio, aCover, aOrig]) {
    const mine = a === L.audio || a === T.audio || holds(a);
    if (mine && a.src) { a.pause(); a.removeAttribute("src"); a.load(); }
  }
}
async function afterChange() {
  L.sel = null; L.audio.pause();
  await refreshAll();
  loadHistory(); loadVoices().catch(() => {});
}

// ------------------------------------------------------------ detail
function playerHtml(url) {
  return `<div class="lplayer"><button class="play" id="lib-play" aria-label="Play">▶</button><canvas id="lib-wave" aria-label="Waveform, click to seek"></canvas></div>`;
}
function wirePlayer(url) {
  L.audio.pause(); L.audio.src = url || ""; L.peaks = null;
  const btn = $("#lib-play"), cv = $("#lib-wave");
  const draw = () => drawWave(cv, L.peaks, L.audio.duration ? L.audio.currentTime / L.audio.duration : 0);
  draw();
  if (url) peaksFromUrl(url).then(p => { L.peaks = p; draw(); }).catch(() => {});
  btn.onclick = () => {
    if (!L.audio.src) return;
    if (L.audio.paused) { L.audio.volume = VOL.muted ? 0 : VOL.level; L.audio.muted = VOL.muted; L.audio.play().catch(() => {}); btn.textContent = "❚❚"; }
    else { L.audio.pause(); btn.textContent = "▶"; }
  };
  L.audio.ontimeupdate = draw; L.audio.onended = () => { btn.textContent = "▶"; draw(); };
  cv.onclick = e => { if (!L.audio.duration) return; const r = cv.getBoundingClientRect(); L.audio.currentTime = (e.clientX - r.left) / r.width * L.audio.duration; draw(); };
}
function switchSource(url) {
  const t = L.audio.currentTime, playing = !L.audio.paused;
  L.audio.src = url; L.peaks = null;
  L.audio.addEventListener("loadedmetadata", () => { try { L.audio.currentTime = Math.min(t, L.audio.duration || t); } catch (e) {} if (playing) L.audio.play().catch(() => {}); }, { once: true });
  peaksFromUrl(url).then(p => { L.peaks = p; drawWave($("#lib-wave"), p, 0); }).catch(() => {});
}

function renderDetail() {
  const d = $("#lib-detail"), x = (L.data[L.cat] || []).find(i => idOf(i) === L.sel);
  if (!x) { d.innerHTML = `<p class="meta">Pick something on the left to see it here.</p>`; return; }
  if (L.cat === "covers") return coverDetail(d, x);
  if (L.cat === "speech") return speechDetail(d, x);
  if (L.cat === "songs") return songDetail(d, x);
  if (L.cat === "voices") return voiceDetail(d, x);
  return trainingDetail(d, x);
}

function coverDetail(d, c) {
  const o = c.options || {};
  const settings = [o.octave ? `voice octave ${o.octave > 0 ? "+" : ""}${o.octave}` : "same octave", o.key ? `key ${o.key > 0 ? "+" : ""}${o.key}` : "key unchanged",
                    c.backing_voice ? `backing by ${voiceDisplay(c.backing_voice)}` : o.backing_voice ? "lead + backing converted" : o.karaoke ? "lead only" : "",
                    o.dereverb ? "de-reverb" : "", o.polish === false ? "" : "polish", o.quality ? `${o.quality} quality` : ""].filter(Boolean).join(" · ");
  d.innerHTML = `<div class="dhead">${art(c.title, true)}<div style="min-width:0"><h3>${esc(c.title)}</h3><p class="meta">sung by <b>${vname(c.voice)}</b>${c.backing_voice ? ` + ${vname(c.backing_voice)}` : ""} · ${esc(when(c.created))}</p></div></div>
    ${playerHtml()}
    <div class="row"><span class="meta">Listen to</span><span class="lactions" id="lib-src"><button class="btn" data-src="url" aria-pressed="true">Cover</button>${c.original ? `<button class="btn" data-src="original">Original</button>` : ""}${c.vocal ? `<button class="btn" data-src="vocal">Vocal only</button>` : ""}</span></div>
    <dl class="kv"><dt data-deep>Settings</dt><dd data-deep>${esc(settings)}</dd>
      ${c.seconds?.total ? `<dt data-deep>Made in</dt><dd data-deep>${c.seconds.total.toFixed(1)} s</dd>` : ""}
      ${c.notes?.length ? `<dt>Notes</dt><dd>${esc(c.notes.join(" "))}</dd>` : ""}
      <dt data-deep>File</dt><dd class="mono" data-deep>${esc(c.file)} · ${gb(c.size)}</dd></dl>
    <div class="ear" id="lib-ear" hidden></div>
    <p class="meta" id="lib-keepline">${esc(keepLine(c))}</p>
    <div id="lib-rename-box"></div>
    <div class="lactions">${c.song_id ? `<button class="btn primary" id="lib-reuse">Use these settings</button>` : ""}<button class="btn" id="lib-keep" type="button" data-kept="${!!c.kept}">${c.kept ? "Unkeep" : "Send to SafeHouse"}</button><a class="btn" href="${c.url}?download">Download</a>
      <button class="btn" id="lib-rename">Rename…</button><button class="btn danger" id="lib-del">Delete…</button></div>`;
  wirePlayer(c.url);
  if (typeof earShow === "function") earShow($("#lib-ear"), "cover", c);
  $$("#lib-src button").forEach(b => b.onclick = () => { $$("#lib-src button").forEach(x => x.setAttribute("aria-pressed", x === b)); switchSource(c[b.dataset.src]); });
  const reuse = $("#lib-reuse");
  if (reuse) reuse.onclick = async () => {
    try {
      S.song = await api(`/api/songs/${c.song_id}`); store.set("uc-song", S.song.id); renderSong();
      await loadVoices(c.voice);
      const keep = ["key", "octave", "index_ratio", "protect", "volume_envelope", "quality", "dereverb", "karaoke", "reverb", "format",
                    "backing_voice", "backing_octave", "backing_db", "pitch_cleanup", "pitch_correction", "polish"];
      S.opts = { ...S.defaults, ...Object.fromEntries(keep.filter(k => o[k] !== undefined && o[k] !== null).map(k => [k, o[k]])) };
      syncControls(); saveOpts(); refreshEstimate();
      showTab("frontman"); window.scrollTo({ top: 0, behavior: "smooth" });
    } catch (e) { showError(e.message, "Couldn't load that cover's settings"); }
  };
  $("#lib-keep").onclick = async () => { try { await api(`/api/library/covers/${c.id}/${c.kept ? "unkeep" : "keep"}`, { json: {} }); afterChangeKeep(); } catch (e) { showError(e.message, "Couldn't change that"); } };
  renameFlow(c.file.replace(/\.[^.]+$/, ""), async name => { await api(`/api/library/covers/${c.id}/rename`, { json: { name } }); });
  confirmClick($("#lib-del"), "Click again to delete", async () => { await api(`/api/library/covers/${c.id}`, { method: "DELETE" }); showError(`${c.title || "That cover"} is deleted.`, "Deleted", null, true); afterChange(); });
}

function speechDetail(d, x) {
  d.innerHTML = `<div class="dhead">${art(x.title, true)}<div style="min-width:0"><h3>${esc(x.title)}</h3><p class="meta">spoken by <b>${vname(x.voice)}</b> · ${esc(when(x.created))}</p></div></div>
    ${playerHtml()}
    <dl class="kv"><dt>Text</dt><dd class="lib-speech-text">${esc(x.text.length > 1200 ? x.text.slice(0, 1200) + "…" : x.text)}</dd>
      <dt data-deep>Settings</dt><dd data-deep>${esc([`${paceWord(x)} pace`, x.mode === "cadence" ? `their own cadence (${x.engine})` : `Reader ${x.reader}${x.reader_auto ? " (auto)" : ""}`, `pitch ${x.pitch > 0 ? "+" : ""}${x.pitch} st`].join(" · "))}</dd>
      ${x.seconds?.total ? `<dt data-deep>Made in</dt><dd data-deep>${x.seconds.total.toFixed(1)} s for ${fmtTime(x.duration)}</dd>` : ""}
      <dt data-deep>Files</dt><dd class="mono" data-deep>${esc([x.file, x.mp3].filter(Boolean).join(" · "))} · ${gb(x.size)}</dd></dl>
    <div class="ear" id="lib-ear" hidden></div>
    <p class="meta" id="lib-keepline">${esc(keepLine(x))}</p>
    <div id="lib-rename-box"></div>
    <div class="lactions"><button class="btn primary" id="lib-again">Open in SpeakEasy</button><button class="btn" id="lib-keep" type="button" data-kept="${!!x.kept}">${x.kept ? "Unkeep" : "Send to SafeHouse"}</button><a class="btn" href="${x.url}?download">Download ${esc((x.file || "").split(".").pop().toUpperCase() || "FLAC")}</a>
      ${x.mp3_url ? `<a class="btn" href="${x.mp3_url}?download">Download MP3</a>` : ""}
      <button class="btn" id="lib-rename">Rename…</button><button class="btn danger" id="lib-del">Delete…</button></div>`;
  wirePlayer(x.url);
  if (typeof earShow === "function") earShow($("#lib-ear"), "speech", x);
  $("#lib-again").onclick = () => {   // the same text and voice, ready to make again (or change)
    $("#se-text").value = x.text; $("#se-text").dispatchEvent(new Event("input"));
    if (typeof SE !== "undefined") { SE.voice = x.voice; store.set("uc-se-voice", x.voice); }
    showTab("speakeasy"); window.scrollTo({ top: 0, behavior: "smooth" });
  };
  $("#lib-keep").onclick = async () => { try { await api(`/api/library/speech/${x.id}/${x.kept ? "unkeep" : "keep"}`, { json: {} }); afterChangeKeep(); } catch (e) { showError(e.message, "Couldn't change that"); } };
  renameFlow(x.file.replace(/\.[^.]+$/, ""), async name => { await api(`/api/library/speech/${x.id}/rename`, { json: { name } }); });
  confirmClick($("#lib-del"), "Click again to delete", async () => { await api(`/api/library/speech/${x.id}`, { method: "DELETE" }); showError("That speech is deleted.", "Deleted", null, true); afterChange(); });
}

function songDetail(d, s) {
  d.innerHTML = `<div class="dhead">${art(s.title, true)}<div style="min-width:0"><h3>${esc(s.title)}</h3><p class="meta">${[s.duration ? fmtTime(s.duration) : "", s.sample_rate ? `${(s.sample_rate / 1000).toFixed(1)} kHz ${s.channels === 1 ? "mono" : "stereo"}` : "", s.key ? `key ${s.key}` : "", s.source].filter(Boolean).map(esc).join(" · ")}</p></div></div>
    ${s.url ? playerHtml() : ""}
    <h2>Stems</h2>
    ${s.stems.length ? `<div class="stems">${s.stems.map((t, i) => `<div class="stem"><button class="tplay" data-i="${i}" aria-label="Play ${esc(t.label)}" aria-pressed="false">▶</button><span>${esc(t.label)}${t.quality !== "high" ? ` <span class="meta">(${t.quality})</span>` : ""}</span><span class="meta mono">${gb(t.size)}</span><a class="linkbtn" href="${t.url}?download">Download</a></div>`).join("")}</div>
      <p class="meta">${s.stems_old_model ? "Made with an older separation model; deleting them means the next cover separates again with the current one." : "Kept so new covers of this song skip separation."}</p>`
      : `<p class="meta">Not separated yet. The next cover of this song does it (about a minute).</p>`}
    <dl class="kv">${s.sample ? `<dt>Sample</dt><dd>UnderCover's sample song${s.artist ? ` · ${esc(s.artist)}` : ""}</dd>` : ""}<dt>Covers</dt><dd>${s.covers ? s.cover_titles.map(esc).join("<br>") : "none yet"}</dd>
      <dt>Size</dt><dd>${gb(s.size)} song${s.stems_size ? ` + ${gb(s.stems_size)} stems` : ""}</dd>
      ${s.source_url ? `<dt>From</dt><dd><a class="linkbtn" href="${esc(s.source_url)}" target="_blank" rel="noopener">${esc(s.source_url)}</a></dd>` : ""}</dl>
    <div class="lactions"><button class="btn primary" id="lib-make">Make a cover</button>
      ${s.stems.length ? `<button class="btn" id="lib-del-stems">Delete stems only…</button>` : ""}<button class="btn danger" id="lib-del">Delete song…</button></div>`;
  if (s.url) wirePlayer(s.url);
  $$("#lib-detail .stem .tplay").forEach(b => b.onclick = () => {
    const t = s.stems[+b.dataset.i];
    const on = b.getAttribute("aria-pressed") === "true";
    $$("#lib-detail .stem .tplay").forEach(x => { x.setAttribute("aria-pressed", "false"); x.textContent = "▶"; });
    if (on) return L.audio.pause();
    L.audio.src = t.url; L.audio.volume = VOL.muted ? 0 : VOL.level; L.audio.muted = VOL.muted; L.audio.play().catch(() => {});
    b.setAttribute("aria-pressed", "true"); b.textContent = "❚❚";
  });
  $("#lib-make").onclick = async () => { try { S.song = await api(`/api/songs/${s.id}`); store.set("uc-song", s.id); renderSong(); refreshEstimate(); showTab("frontman"); window.scrollTo({ top: 0, behavior: "smooth" }); } catch (e) { showError(e.message); } };
  const ds = $("#lib-del-stems");
  if (ds) confirmClick(ds, `Delete ${gb(s.stems_size)} of stems?`, async () => { await api(`/api/library/songs/${s.id}?stems_only=1`, { method: "DELETE" }); showError(`${gb(s.stems_size)} of stems deleted. The song stays; its next cover separates it again.`, "Deleted", null, true); afterChange(); });
  confirmClick($("#lib-del"), "Click again to delete", async () => {
    await api(`/api/library/songs/${s.id}`, { method: "DELETE" });
    showError(`${s.title || "The song"} is deleted.`, "Deleted", null, true);
    if (S.song?.id === s.id) { S.song = null; store.set("uc-song", null); renderSong(); }
    afterChange();
  });
}

// "2026-11-12" as a plain date in the reader's own style
function fullIndexDate(u) { const d = new Date(`${u}T12:00:00`); return isNaN(d) ? u : d.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" }); }
function voiceDetail(d, v) {
  const [cls, word] = quality(v);
  const src = v.trained ? `trained in UnderCover${v.project ? ` · <button class="linkbtn" id="lib-open-proj">open project</button>` : ""}`
    : esc(v.source.replace(/^https?:\/\//, "").slice(0, 90));
  d.innerHTML = `<div class="dhead">${art(voiceDisplay(v.name), true)}<div style="min-width:0"><h3>${vname(v.name)}</h3><p class="meta" data-deep>folder <span class="mono">${esc(v.name)}</span> · RVC ${esc(v.version)} · ${v.sr ? v.sr / 1000 + " kHz" : "?"} · ${v.index ? "with index" : "no index"}</p><p class="meta">${v.demo ? "demo voice · made for UnderCover's first assignment, not a real person" : v.trained ? "trained in UnderCover" : "downloaded"} · added ${esc(when(v.installed))}</p></div></div>
    ${"display" in v ? `<div class="vname-edit"><label class="meta" for="lib-vname-edit">Shown as</label><div class="row"><input id="lib-vname-edit" type="text" autocomplete="off" value="${esc(v.display || "")}" placeholder="${esc(v.name)}"><button class="btn" id="lib-vname-save" type="button">Save</button></div></div>` : ""}
    ${v.sample ? `${playerHtml()}<p class="meta">A held-out phrase rebuilt by this voice</p>` : ""}
    <dl class="kv"><dt data-deep>Likeness</dt><dd data-deep><span class="tag ${cls}">${word}</span>${v.score != null ? ` <span class="meta">${v.score.toFixed(2)} (pass 0.45)</span>` : ""}</dd>
      <dt data-deep>From</dt><dd data-deep>${src}</dd>
      ${v.trained ? `<dt data-deep>Training</dt><dd data-deep>${v.train_minutes ? `${v.train_minutes} min of ${v.from_speech ? "speech" : "audio"}` : ""}${v.epoch ? ` · epoch ${v.epoch}` : ""}${v.f0_range ? ` · range ${Math.round(v.f0_range[0])}–${Math.round(v.f0_range[2])} Hz` : ""}</dd>` : ""}
      <dt>Used in</dt><dd>${v.covers ? v.cover_titles.map(esc).join("<br>") : "no covers yet"}</dd>
      <dt>Speaks</dt><dd>${v.cadence ? `yes, in SpeakEasy · ${gb(v.cadence.size)} <span class="meta" data-deep>· ${esc(v.cadence.engine || "")} · from ${v.cadence.data_minutes ?? "?"} min · made ${esc(when(v.cadence.made))}</span>
        <button class="linkbtn" id="lib-del-cad" type="button" data-deep data-pc>Delete cadence</button>` : "none"}</dd>
      ${v.full_index ? `<dt data-deep>Full index</dt><dd data-deep>Kept${v.full_index.until ? ` until ${esc(fullIndexDate(v.full_index.until))}` : ""} <span class="meta">· ${v.full_index.vectors_before ?? "?"} → ${v.full_index.vectors_after ?? "?"} vectors · ${gb(v.full_index.bytes_before || 0)} before</span>
        <button class="linkbtn" id="lib-full-idx" type="button" data-pc>Put the full index back</button></dd>` : ""}
      <dt data-deep>Size</dt><dd data-deep>${gb(v.size)}${v.cadence ? " (with its cadence)" : ""}</dd></dl>
    <div id="lib-rename-box"></div>
    <div class="lactions"><button class="btn primary" id="lib-make">Make a cover</button><button class="btn" id="lib-rename" data-deep data-pc>Rename folder…</button>
      <a class="btn" href="/api/library/voices/${encodeURIComponent(v.name)}/export" data-deep data-pc>Export .zip</a><button class="btn danger" id="lib-del">Remove…</button></div>`;
  if (v.sample) wirePlayer(v.sample);
  const op = $("#lib-open-proj");
  if (op) op.onclick = async () => { store.set("uc-train-proj", v.project); showTab("doubleagent"); try { await openProject(v.project); } catch (e) {} };
  $("#lib-make").onclick = async () => { await loadVoices(v.name); showTab("frontman"); window.scrollTo({ top: 0, behavior: "smooth" }); };
  const nameIn = $("#lib-vname-edit");   // the display name (the folder id stays); shown once the voice API carries `display`
  if (nameIn) {
    const save = async () => {
      try { await api(`/api/library/voices/${encodeURIComponent(v.name)}/display`, { json: { display: nameIn.value.trim() } }); }
      catch (e) { showError(e.message, "Couldn't rename the voice"); return; }
      afterChange(); loadVoices();
      if (SE.loaded) seLoad();   // SpeakEasy's picker; DoubleAgent's cards and the queue pickers come from the candidates
      loadCandidates();
    };
    $("#lib-vname-save").onclick = save;
    nameIn.onkeydown = e => { if (e.key === "Enter") save(); };
  }
  renameFlow(v.name, async name => { const r = await api(`/api/library/voices/${encodeURIComponent(v.name)}/rename`, { json: { name } }); L.sel = r.name; });
  confirmClick($("#lib-del"), "Click again to remove", async () => { removedVoice(v.name, await api(`/api/voices/${encodeURIComponent(v.name)}`, { method: "DELETE" })); afterChange(); loadVoices(); });
  if ($("#lib-full-idx")) $("#lib-full-idx").onclick = async e => {   // the shrunk voice's full index, kept 30 days
    e.target.disabled = true;
    try { await api(`/api/library/voices/${encodeURIComponent(v.name)}/full-index`, { json: {} }); showError("The full index is back. New covers use it.", "Put back", null, true); afterChange(); }
    catch (err) { e.target.disabled = false; showError(err.message, "Couldn't put the full index back"); }
  };
  if ($("#lib-del-cad")) confirmClick($("#lib-del-cad"), "Click again: the voice stays", async () => {
    await api(`/api/library/voices/${encodeURIComponent(v.name)}/cadence`, { method: "DELETE" }); afterChange(); });
}

function trainingDetail(d, p) {
  const st = p.train || {};
  d.innerHTML = `<div class="dhead">${art(p.person || p.name, true)}<div style="min-width:0"><h3>${esc(p.person || p.name)}</h3><p class="meta">${esc(p.mode)} · ${p.ready_sources} of ${p.sources} sources prepared · ${p.kept_minutes} min of voice kept</p></div></div>
    <dl class="kv"><dt>Status</dt><dd>${st.state === "exported" ? `finished: voice <b>${esc(st.exported)}</b>` : st.state === "paused" ? `paused at epoch ${st.epoch}` : st.state === "trained" ? "trained, not added to voices yet" : st.state || "not trained yet"}</dd>
      <dt>On disk</dt><dd>${Object.entries(p.parts).filter(([, b]) => b).map(([k, b]) => `${esc(k)} ${gb(b)}`).join(" · ") || "—"}</dd></dl>
    <p class="meta">${st.state === "exported" ? "Deleting the project keeps the voice; you'd only need these files to train it again." : "Deleting the project removes its clips and any unfinished training."}</p>
    <div class="lactions"><button class="btn primary" id="lib-open">Open in DoubleAgent</button><button class="btn danger" id="lib-del">Delete project…</button></div>`;
  $("#lib-open").onclick = async () => { store.set("uc-train-proj", p.name); showTab("doubleagent"); try { await openProject(p.name); } catch (e) {} };
  confirmClick($("#lib-del"), `Delete ${gb(p.size)}?`, async () => { await api(`/api/train/projects/${p.name}`, { method: "DELETE" }); afterChange(); });
}

function renameFlow(current, save) {
  const btn = $("#lib-rename"); if (!btn) return;
  btn.onclick = () => {
    const box = $("#lib-rename-box");
    box.innerHTML = `<div class="renamer"><input id="lib-new-name" value="${esc(current)}" aria-label="New name"><button class="btn primary" id="lib-rename-ok">Save</button><button class="linkbtn" id="lib-rename-x">Cancel</button></div>`;
    const inp = $("#lib-new-name"); inp.focus(); inp.select();
    const go = async () => { releaseAudio(); try { await save(inp.value.trim()); afterChangeKeep(); } catch (e) { showError(e.message, "Couldn't rename"); } };
    $("#lib-rename-ok").onclick = go;
    inp.addEventListener("keydown", e => { if (e.key === "Enter") go(); if (e.key === "Escape") box.innerHTML = ""; });
    $("#lib-rename-x").onclick = () => { box.innerHTML = ""; };
  };
}
async function afterChangeKeep() { L.audio.pause(); await refreshAll(true); loadHistory(); loadVoices().catch(() => {}); if (typeof seLoadRecent === "function") seLoadRecent(); }

// ------------------------------------------------------------ clean up
$("#lib-clean-btn").onclick = async () => {
  const panel = $("#lib-clean");
  if (!panel.hidden) { panel.hidden = true; stRowsSync?.(); return; }
  let g;
  try { g = await api("/api/library/cleanup"); } catch (e) { return showError(e.message); }
  const keys = Object.keys(g).filter(k => g[k].count);
  // a group frees space (size) or moves voices to Set aside (aside, aside_size): nothing a voice is deleted here, so its size is "moved", not "freed"
  // two voices can share a display name (a copy with no name of its own): then the folder name, in small type, tells them apart (in Deep Cover always)
  const rowsOf = x => {
    const ids = new Map();   // display name -> the voices that read it
    (x.aside || []).forEach(a => [[a.display || a.voice, a.voice], [a.keeps_display || a.keeps, a.keeps]].forEach(([d, v]) => ids.set(d, (ids.get(d) || new Set()).add(v))));
    const same = n => ids.get(n).size > 1;
    const tag = (d, v) => `<span class="mono"${same(d) ? "" : " data-deep"}> (${esc(v)})</span>`;
    return (x.aside || []).map(a => `<small>${esc(a.display || a.voice)}${tag(a.display || a.voice, a.voice)} goes to Set aside: the same model as ${esc(a.keeps_display || a.keeps)}${tag(a.keeps_display || a.keeps, a.keeps)}${a.because ? `, kept because ${esc(a.because)}` : ""}</small>`).join("");
  };
  $("#lib-clean-list").innerHTML = keys.length ? keys.map(k => `<label class="crow"><input type="checkbox" data-g="${k}" ${g[k].default ? "checked" : ""}>
      <span><b>${esc(g[k].title)}</b>${g[k].aside?.length ? rowsOf(g[k]) : `<small>${esc(g[k].detail)}</small>`}</span>
      <span class="mono">${g[k].aside_size ? `moved ${gb(g[k].aside_size)}` : gb(g[k].size)}</span></label>`).join("")
    : `<p class="meta">Nothing to clean up right now.</p>`;
  const sum = f => keys.filter(k => $(`[data-g=${k}]`).checked).reduce((a, k) => a + (g[k][f] || 0), 0);
  const go = $("#lib-clean-go");
  const upd = () => {
    const free = sum("size"), moved = sum("aside_size");
    go.textContent = free && moved ? `Free ${gb(free)} and move ${gb(moved)} to Set aside` : moved ? `Move ${gb(moved)} to Set aside` : free ? `Free ${gb(free)}` : "Nothing ticked";
    go.disabled = !free && !moved;
  };
  $$("#lib-clean-list input").forEach(i => i.onchange = upd);
  upd(); panel.hidden = false;
  go.onclick = async () => {
    const groups = keys.filter(k => $(`[data-g=${k}]`).checked);
    go.disabled = true; go.textContent = "Freeing…";
    try {
      const r = await api("/api/library/cleanup", { json: { groups } }); panel.hidden = true;
      // freed: space given back. set_aside: voices moved to Set aside (nothing deleted; Bring it back is there). skipped: what was left, and why
      const name = x => typeof x === "string" ? voiceDisplay(x) : (x.display || voiceDisplay(x.voice) || "");
      const moved = Array.isArray(r.set_aside) ? r.set_aside : [], skipped = Array.isArray(r.skipped) ? r.skipped : [];
      const say = [r.freed ? `Freed ${gb(r.freed)}.` : "",
        moved.length ? `Set aside: ${moved.map(name).join(", ")}. Bring it back, or delete it for good, in Storage > Set aside.` : "",
        skipped.length ? `Left alone: ${skipped.map(s => `${name(s)}${s.why ? ` (${s.why})` : ""}`).join("; ")}.` : ""].filter(Boolean).join(" ");
      showError(say || "Nothing needed doing.", "Cleaned up", null, true); afterChange();
      if (typeof asideLoad === "function") asideLoad();
    }
    catch (e) { showError(e.message, "Couldn't clean up"); upd(); }
  };
};
$("#lib-clean-close").onclick = () => { $("#lib-clean").hidden = true; stRowsSync?.(); };
$("#hist-library").onclick = () => { L.cat = "covers"; store.set("uc-lib-cat", "covers"); location.hash = "#safehouse/library"; showTab("safehouse"); };   // the Library room, not the last room (IA P9-12)

