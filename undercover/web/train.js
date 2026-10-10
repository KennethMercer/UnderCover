"use strict";
// Train a voice: find clips of a person, keep only their voice, review samples, train an RVC model.
// Uses helpers from app.js ($, $$, esc, api, showError, store, fmtTime, VOL, loadVoices).

const T = { list: [], proj: null, filter: store.get("uc-train-filter", "kept"), mode: "speaking", es: null,
            audio: new Audio(), playing: null, live: null, busy: false };

// ------------------------------------------------------------ tabs
function showTab(tab) {
  const train = tab === "train";
  for (const t of ["covers", "train", "library"]) {
    $(`#view-${t}`).hidden = t !== tab;
    $(`#tab-${t}`).setAttribute("aria-selected", t === tab);
  }
  $("#drop-overlay div").textContent = train ? "Drop recordings of the person to add them as a source"
                                             : "Drop a song to load it, or a voice model (.zip / .pth) to install it";
  store.set("uc-tab", tab);
  if (train && !T.loaded) { T.loaded = true; loadProjects(); }
  if (!train) stopClip();
  if (tab === "library" && window.libShow) window.libShow(); else if (window.libHide) window.libHide();
}
$$(".tabs button").forEach(b => b.onclick = () => showTab(b.dataset.tab));
const STEP_TARGET = { 1: "#tr-find", 2: "#tr-ref", 3: "#tr-samples", 4: "#tr-train" };
$$("#tr-steps .step").forEach(el => {
  el.tabIndex = 0; el.setAttribute("role", "button");
  const go = () => { const t = $(STEP_TARGET[el.dataset.step]); if (t && !t.hidden) t.scrollIntoView({ behavior: "smooth", block: "start" }); };
  el.addEventListener("click", go);
  el.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); } });
});

// ------------------------------------------------------------ audio (one clip at a time)
function stopClip() { T.audio.pause(); if (T.playing) T.playing.setAttribute("aria-pressed", "false"); T.playing = null; }
function playClip(btn, url) {
  if (T.playing === btn) return stopClip();
  stopClip();
  if (!url) return;
  T.audio.src = url; T.audio.volume = VOL.muted ? 0 : VOL.level; T.audio.muted = VOL.muted;
  T.audio.play().catch(() => {});
  T.playing = btn; btn.setAttribute("aria-pressed", "true");
}
T.audio.onended = stopClip;
const playBtn = (url, label = "Play", main = false) =>
  `<button class="tplay${main ? " main" : ""}" data-url="${esc(url || "")}" aria-label="${esc(label)}" aria-pressed="false">▶</button>`;
document.addEventListener("click", e => {
  const b = e.target.closest("#view-train .tplay");
  if (b) playClip(b, b.dataset.url);
});

// ------------------------------------------------------------ projects
async function loadProjects(select) {
  try { T.list = await api("/api/train/projects"); } catch (e) { return showError(e.message, "Couldn't load training projects"); }
  const want = select ?? store.get("uc-train-proj", null);
  const pick = $("#tr-pick");
  pick.innerHTML = `<option value="">New voice…</option>` + T.list.map(p =>
    `<option value="${esc(p.name)}">${esc(p.person || p.name)}${p.train?.exported ? " ✓" : ""} · ${p.kept_minutes} min</option>`).join("");
  const name = T.list.some(p => p.name === want) ? want : "";
  pick.value = name;
  if (name) await openProject(name); else { T.proj = null; render(); }
}
$("#tr-pick").onchange = () => { const n = $("#tr-pick").value; store.set("uc-train-proj", n || null); n ? openProject(n) : newVoice(); };
$("#tr-new").onclick = newVoice;
function newVoice() {
  closeStream(); T.proj = null; $("#tr-pick").value = ""; store.set("uc-train-proj", null);
  $("#tr-person").value = ""; render(); $("#tr-person").focus();
}
async function openProject(name, data) {
  try { T.proj = data || await api(`/api/train/projects/${name}`); }
  catch (e) { store.set("uc-train-proj", null); T.proj = null; render(); return showError(e.message, "Couldn't open that project"); }
  store.set("uc-train-proj", T.proj.name);
  if ($("#tr-pick").value !== T.proj.name) {
    if (![...$("#tr-pick").options].some(o => o.value === T.proj.name)) await loadProjects(T.proj.name);
    $("#tr-pick").value = T.proj.name;
  }
  if (!data) { $("#tr-person").value = T.proj.person; T.mode = T.proj.mode; }
  render();
  if (T.proj.job && (!T.live || T.live.id !== T.proj.job.id)) follow(T.proj.job.id, T.proj.job.kind);
}
const refresh = async () => { if (T.proj) await openProject(T.proj.name); };
const P = (action, json) => api(`/api/train/projects/${T.proj.name}/${action}`, { json: json ?? {} });

// ------------------------------------------------------------ job events
function closeStream() { if (T.es) { T.es.close(); T.es = null; } T.live = null; }
function follow(id, kind) {
  closeStream();
  T.live = { id, kind };
  const es = new EventSource(`/api/jobs/${id}/events`);
  T.es = es;
  es.onmessage = m => {
    const ev = JSON.parse(m.data);
    if (kind === "prepare") onPrepareEvent(ev);
    else if (kind === "auto") onAutoEvent(ev);
    else if (kind === "train") onTrainEvent(ev);
    else if (kind === "export") onExportEvent(ev);
    if (ev.final) {
      closeStream();
      if (ev.state === "error") showError(ev.message, kind === "train" ? "Training stopped" : "That didn't work");
      else if (ev.message && kind === "prepare" && ev.message.length) showError(ev.message, "Some sources were skipped", null, true);
      if (kind === "auto") { clearInterval(T.autoTimer); $("#tr-prep-progress").hidden = true; if (ev.state === "done") loadVoices(); }
      refresh().then(() => { if (kind === "export" && ev.voice) loadVoices(); });
    }
  };
  es.onerror = () => { if (es.readyState === EventSource.CLOSED) { closeStream(); setTimeout(refresh, 1500); } };
  render();
}

// ------------------------------------------------------------ step 1: find
$$("#tr-mode button").forEach(b => b.onclick = () => { T.mode = b.dataset.v; syncMode(); });
function syncMode() { $$("#tr-mode button").forEach(b => b.setAttribute("aria-pressed", b.dataset.v === T.mode)); }
$("#tr-person").addEventListener("keydown", e => { if (e.key === "Enter") $("#tr-search").click(); });
$("#tr-search").onclick = async () => {
  const person = $("#tr-person").value.trim();
  if (!person) return $("#tr-person").focus();
  const btn = $("#tr-search");
  btn.disabled = true; btn.textContent = "Searching…";
  try {
    const same = T.proj && T.proj.person.toLowerCase() === person.toLowerCase();
    const data = same ? await P("search", { person, mode: T.mode })
                      : await api("/api/train/projects", { json: { person, mode: T.mode } });
    if (data.warning) showError(data.warning, "No clips found", null, true);
    await loadProjects(data.name);
  } catch (e) { showError(e.message, "Search failed"); }
  finally { btn.disabled = false; btn.textContent = "Find clips"; }
};
$("#tr-link-add").onclick = async () => {
  const url = $("#tr-link").value.trim();
  if (!url || !T.proj) return;
  const btn = $("#tr-link-add"); btn.disabled = true;
  try { await openProject(T.proj.name, await P("sources", { url })); $("#tr-link").value = ""; }
  catch (e) { showError(e.message, "Couldn't add that link"); }
  finally { btn.disabled = false; }
};
$("#tr-link").addEventListener("keydown", e => { if (e.key === "Enter") $("#tr-link-add").click(); });
$("#tr-file-add").onclick = () => $("#tr-file").click();
$("#tr-file").onchange = () => { addFiles([...$("#tr-file").files]); $("#tr-file").value = ""; };
async function addFiles(files) {
  if (!files.length) return;
  if (!T.proj) {
    const person = $("#tr-person").value.trim();
    if (!person) { $("#tr-person").focus(); return showError("Type the person's name first, then add recordings.", "Who is this?", null, true); }
    try { T.proj = await api("/api/train/projects", { json: { person, mode: T.mode, no_search: true } }); await loadProjects(T.proj.name); }
    catch (e) { return showError(e.message, "Couldn't start a project"); }
  }
  for (const f of files) {
    $("#tr-prepare-note").textContent = `Adding ${f.name}…`;
    try {
      const r = await fetch(`/api/train/projects/${T.proj.name}/upload`, { method: "POST", body: f, headers: { "X-Filename": encodeURIComponent(f.name) } });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(data.error || "Upload failed.");
      await openProject(T.proj.name, data);
    } catch (e) { showError(e.message, `Couldn't add ${f.name}`); }
  }
  render();
}
window.trainDrop = addFiles;

function renderSources() {
  const p = T.proj, box = $("#tr-sources");
  if (!p || !p.sources.length) { box.innerHTML = ""; return; }
  box.innerHTML = p.sources.map(s => {
    const mins = s.duration ? fmtTime(s.duration) : "";
    let side = `<span class="mono meta">${mins}</span>`;
    if (s.status === "ready") side = `<span class="tag ok">ready · ${s.extra?.phrases ?? 0} phrases</span>` + side;
    else if (s.status === "failed") side = `<span class="tag bad">failed</span>` + side;
    else if (s.status !== "found") side = `<span class="tag warn">${esc(s.status)}…</span>` + side;
    if (s.user || s.status === "ready" || s.status === "failed") side += `<button class="linkbtn" data-rm="${esc(s.id)}">Remove</button>`;
    const sub = [s.channel, s.note].filter(Boolean).map(esc).join(" · ");
    return `<label class="src"><input type="checkbox" data-id="${esc(s.id)}" ${s.selected ? "checked" : ""} ${s.status === "ready" ? "disabled" : ""}>
      <span><b>${esc(s.title)}</b><small>${sub}</small>${s.error ? `<small class="err">${esc(s.error)}</small>` : ""}</span>
      <span class="src-side">${side}</span></label>`;
  }).join("");
  $$("input[type=checkbox]", box).forEach(c => c.onchange = async () => {
    try { await openProject(T.proj.name, await P("sources", { id: c.dataset.id, selected: c.checked })); }
    catch (e) { showError(e.message); }
  });
  $$("[data-rm]", box).forEach(b => b.onclick = async e => {
    e.preventDefault();
    const src = T.proj.sources.find(s => s.id === b.dataset.rm);
    if (src && src.status === "ready" && !b.classList.contains("confirm")) {   // deletes its prepared phrases: confirm
      b.classList.add("confirm"); b.textContent = `Remove ${src.extra?.phrases ?? ""} phrases?`.replace("  ", " ");
      setTimeout(() => { b.classList.remove("confirm"); b.textContent = "Remove"; }, 4000);
      return;
    }
    try { await openProject(T.proj.name, await P("sources", { id: b.dataset.rm, remove: true })); }
    catch (err) { showError(err.message, "Couldn't remove that source"); }
  });
}

$("#tr-prepare").onclick = async () => {
  try { const r = await P("prepare"); follow(r.job, "prepare"); }
  catch (e) { showError(e.message, "Can't prepare yet"); }
};
$("#tr-auto").onclick = async () => {
  try { const r = await P("auto", { voice_name: $("#tr-name").value || undefined }); follow(r.job, "auto"); }
  catch (e) { showError(e.message, "Can't start"); }
};
function onAutoEvent(ev) {
  // "Do it all" (or a one-shot cover training this voice): one progress line, and the page refreshes itself
  // every 15 s so sources, samples, the chart and saves fill in as they happen.
  $("#tr-prep-progress").hidden = !!ev.final;
  if (ev.final) return;
  const f = ev.frac ?? ev.fraction ?? 0;
  $("#tr-prep-bar").style.width = Math.round(f * 100) + "%";
  $("#tr-prep-pct").textContent = Math.round(f * 100) + "%" + (ev.eta ? ` · ~${fmtSecs(ev.eta)} left` : "");
  $("#tr-prep-label").textContent = ev.message || ev.label || "Working…";
  if (!T.autoTimer) T.autoTimer = setInterval(() => { if (T.live?.kind === "auto") refresh(); else { clearInterval(T.autoTimer); T.autoTimer = null; } }, 15000);
}
$("#tr-prep-cancel").onclick = () => { if (T.live) api(`/api/jobs/${T.live.id}/cancel`, { method: "POST" }); };
function onPrepareEvent(ev) {
  $("#tr-prep-progress").hidden = !!ev.final;
  if (ev.final) return;
  const pct = Math.round((ev.fraction || 0) * 100);
  $("#tr-prep-bar").style.width = pct + "%";
  $("#tr-prep-pct").textContent = pct + "%";
  $("#tr-prep-label").textContent = ev.stage === "matching" ? "Finding the voice…"
    : `Source ${ev.index} of ${ev.count}: ${ev.label} · ${ev.title || ""}`;
}

// ------------------------------------------------------------ step 2: reference
function renderReference() {
  const p = T.proj, ref = p.reference || {};
  const sec = $("#tr-ref");
  sec.hidden = !p.speakers?.length;
  if (sec.hidden) return;
  const ph = p.phrases.find(x => x.id === ref.clip);
  const src = ph && p.sources.find(s => s.id === ph.source);
  const where = src ? `From “${esc(src.title.slice(0, 60))}” at ${fmtTime(ph.start)}` : "";
  const share = ref.cluster === -1 ? "picked by you" : `heard in ${ref.sources} of ${p.ready_sources} sources · ${Math.round(ref.share * 100)}% of all speech`;
  $("#tr-ref-main").innerHTML = `${playBtn(p.reference_url, "Play the reference voice", true)}
    <div><b>Reference${ph ? " · " + ph.dur.toFixed(1) + " s" : ""}</b><div class="meta">${[where, share].filter(Boolean).join(" · ")}</div></div>
    <div class="row">${ref.confirmed ? `<span class="tag ok">✓ confirmed</span>` : `<button class="btn primary" id="tr-ref-yes">Yes, that's them</button>`}
      <button class="btn" id="tr-ref-other">${T.showSpeakers ? "Hide others" : "Pick another"}</button></div>`;
  const yes = $("#tr-ref-yes");
  if (yes) yes.onclick = () => setReference({ cluster: ref.cluster });
  $("#tr-ref-other").onclick = () => { T.showSpeakers = !T.showSpeakers; renderReference(); };
  const others = p.speakers.filter(s => s.cluster !== ref.cluster);
  $("#tr-speakers").innerHTML = !T.showSpeakers ? "" :
    (others.length ? others.map((s, i) => `<div class="spk">${playBtn(s.url, "Play this voice")}
      <div><b>Voice ${String.fromCharCode(66 + i)}</b><div class="meta">${s.minutes} min · in ${s.sources} source${s.sources > 1 ? "s" : ""} · ${Math.round(s.share * 100)}%</div></div>
      <button class="btn" data-cluster="${s.cluster}">Use this voice</button></div>`).join("")
      : `<p class="meta">No other voices were found. If the reference is wrong, press play on a sample below that is the right person and choose “Use as reference”.</p>`);
  $$("#tr-speakers [data-cluster]").forEach(b => b.onclick = () => setReference({ cluster: +b.dataset.cluster }));
}
async function setReference(body) {
  try { T.showSpeakers = false; await openProject(T.proj.name, await P("reference", body)); }
  catch (e) { showError(e.message, "Couldn't change the reference"); }
}

// ------------------------------------------------------------ step 3: samples
$$("#tr-filter button").forEach(b => b.onclick = () => { T.filter = b.dataset.v; store.set("uc-train-filter", T.filter); renderSamples(); });
function renderSamples() {
  const p = T.proj, sec = $("#tr-samples");
  sec.hidden = !p.phrases.length || !p.reference?.cluster && p.reference?.cluster !== 0;
  if (sec.hidden) return;
  $$("#tr-filter button").forEach(b => {
    const n = b.dataset.v === "kept" ? p.phrases.filter(x => x.kept).length : b.dataset.v === "out" ? p.phrases.filter(x => !x.kept).length : p.phrases.length;
    b.textContent = `${b.dataset.v === "kept" ? "Kept" : b.dataset.v === "out" ? "Left out" : "All"} ${n}`;
    b.setAttribute("aria-pressed", b.dataset.v === T.filter);
  });
  const kept = p.phrases.filter(x => x.kept).reduce((a, x) => a + x.dur, 0) / 60;
  $("#tr-minutes").textContent = `${kept.toFixed(1)} min of clean voice`;
  $("#tr-target").textContent = `target ${p.target_minutes} min · more than 30 rarely helps`;
  const scale = Math.max(p.target_minutes * 2, kept);
  $("#tr-meter").style.width = Math.min(100, kept / scale * 100) + "%";
  $("#tr-meter").style.background = kept >= p.target_minutes ? "var(--ok)" : kept >= 3 ? "var(--warn)" : "var(--err)";
  $("#tr-meter-target").style.left = (p.target_minutes / scale * 100) + "%";
  const lo = Object.entries(p.left_out || {}).map(([k, v]) => `${k} ${v}`).join(" · ");
  $("#tr-leftout").textContent = lo ? `Left out automatically: ${lo}. You can put any of them back.` : "Nothing was left out automatically.";
  const titles = Object.fromEntries(p.sources.map(s => [s.id, s.title]));
  const rows = p.phrases.filter(x => T.filter === "all" || (T.filter === "kept") === x.kept);
  rows.sort((a, b) => T.filter === "kept" ? a.score - b.score : b.score - a.score);  // most doubtful first
  const box = $("#tr-list");
  box.innerHTML = rows.length ? rows.map(x => {
    const right = x.reason && !x.kept ? `<span class="tag ${x.reason === "other speaker" || x.reason === "two people at once" ? "bad" : "warn"}">${esc(x.reason)}</span>`
      : `match ${Math.round(Math.max(0, x.score) * 100)}%<div class="meter"><i style="width:${Math.max(0, Math.min(1, x.score)) * 100}%"></i></div>`;
    return `<div class="sample${x.kept ? "" : " off"}" data-id="${esc(x.id)}">${playBtn(x.url)}
      <div style="min-width:0"><div class="mono" style="font-size:13px">${x.dur.toFixed(1)} s ${x.choice !== null ? `<span class="chosen">${x.choice ? "kept by you" : "removed by you"}</span>` : ""}</div>
      <div class="stitle">${esc((titles[x.source] || "").slice(0, 70))} · ${fmtTime(x.start)}</div></div>
      <div class="score">${right}</div>
      <button class="stoggle">${x.kept ? "Remove" : "Put back"}</button></div>`;
  }).join("") : `<p class="meta">Nothing here.</p>`;
  $$(".stoggle", box).forEach(b => b.onclick = () => toggleSample(b.closest(".sample").dataset.id));
}
async function toggleSample(id) {
  const x = T.proj.phrases.find(p => p.id === id);
  const want = !x.kept;
  const auto = !x.reason;
  const choice = want === auto ? null : want;   // back to automatic when that matches
  x.choice = choice; x.kept = want;            // optimistic
  renderSamples(); renderSteps(); renderTrainForm();
  try { const d = await P("phrases", { ids: [id], choice }); T.proj = d; renderSteps(); renderTrainForm(); }
  catch (e) { showError(e.message, "Couldn't change that sample"); refresh(); }
}

// ------------------------------------------------------------ step 4: train
function renderTrainForm() {
  const p = T.proj, st = p.train || {};
  const kept = p.phrases.filter(x => x.kept).reduce((a, x) => a + x.dur, 0) / 60;
  $("#tr-train").hidden = !p.reference?.clip || !p.phrases.length;
  if ($("#tr-train").hidden) return;
  if (!$("#tr-name").value || $("#tr-name").dataset.proj !== p.name) {
    $("#tr-name").value = st.voice_name || `${p.name}-uc`; $("#tr-name").dataset.proj = p.name;
    $("#tr-length").value = st.length || "auto"; $("#tr-sr").value = String(st.sr || p.defaults.sr);
  }
  const running = T.live?.kind === "train";
  const paused = st.state === "paused";
  const btn = $("#tr-start");
  btn.textContent = paused ? "Resume training" : (st.epoch ? "Train again from scratch" : "Start training");
  btn.disabled = running || T.live?.kind === "prepare" || kept < 1;
  ["#tr-name", "#tr-length", "#tr-sr"].forEach(s => $(s).disabled = running || paused);
  const steps = p.defaults.steps[$("#tr-length").value] || p.defaults.steps.auto;
  const est = steps * (st.sec_per_step || 0.3) + 60;   // + data preparation
  $("#tr-start-note").innerHTML = kept < 1 ? "Keep at least 1 minute of samples first."
    : paused ? `Paused at epoch ${st.epoch}. <button class="linkbtn" id="tr-fresh">Start over instead</button>`
    : running ? "" : `≈ ${Math.max(1, Math.round(est / 60))} min on this GPU`;
  const fresh = $("#tr-fresh");
  if (fresh) fresh.onclick = () => startTraining(true);
}
$("#tr-start").onclick = () => startTraining(false);
async function startTraining(fresh) {
  const st = T.proj.train || {};
  if (!fresh && st.epoch && st.state !== "paused" && !confirm("Train again from scratch? The saves from the last run are replaced.")) return;
  try {
    const r = await P("train", { voice_name: $("#tr-name").value, length: $("#tr-length").value, sr: +$("#tr-sr").value, fresh });
    T.trainEv = null;
    follow(r.job, "train");
    refresh();
  } catch (e) { showError(e.message, "Can't start training"); }
}
$("#tr-pause").onclick = async () => { try { await P("pause"); $("#tr-run-h").textContent = "Pausing after this step…"; } catch (e) { showError(e.message); } };
$("#tr-stop").onclick = async () => { try { await P("stop"); $("#tr-run-h").textContent = "Finishing…"; } catch (e) { showError(e.message); } };
function onTrainEvent(ev) {
  if (ev.final) return;
  T.trainEv = ev;
  if (ev.sec_per_epoch) T.proj.train.sec_per_epoch = ev.sec_per_epoch;
  if (ev.history) { (T.proj.history = T.proj.history || []).push(ev.history); drawChart(); }
  if (ev.saved) refresh();
  renderRun();
}
function renderRun() {
  const p = T.proj, st = p.train || {}, ev = T.trainEv;
  const running = T.live?.kind === "train";
  const show = running || (p.history?.length || 0) > 0 || st.state === "paused";
  $("#tr-run").hidden = !show || $("#tr-train").hidden;
  if ($("#tr-run").hidden) return;
  const auto = T.live?.kind === "auto";
  $("#tr-pause").hidden = !(running || auto); $("#tr-stop").hidden = !(running || auto || st.state === "paused");
  const epoch = ev?.epoch ?? st.epoch ?? 0, total = ev?.epochs ?? st.epochs ?? 0;
  const h = $("#tr-run-h");
  if (running && ev?.stage === "preparing") h.textContent = `Getting ready: ${ev.note || ""}`;
  else if (running && ev?.note === "paused for a cover") h.textContent = "Making your cover first…";
  else if (running) h.textContent = "Training…";
  else h.textContent = { paused: "Paused", trained: "Training finished", exported: "Training finished", failed: "Training failed" }[st.state] || "Training";
  $("#tr-bar").style.width = (running && ev ? (ev.fraction || 0) : total ? epoch / total : 0) * 100 + "%";
  $("#tr-epoch").innerHTML = `${epoch}<span class="meta"> / ${total || "—"}</span>`;
  $("#tr-eta").textContent = running && ev?.eta ? fmtTime(ev.eta) : "—";
  $("#tr-speed").textContent = st.sec_per_epoch ? `${st.sec_per_epoch} s/ep` : "—";
  $("#tr-data").textContent = st.train_minutes ? `${st.train_minutes} min` : "—";
  drawChart();
}
function drawChart() {
  const svg = $("#tr-chart"), hist = (T.proj?.history || []).filter(h => h.mel === h.mel);
  if (hist.length < 2) { svg.innerHTML = `<text x="200" y="80" text-anchor="middle" fill="var(--muted)" font-size="12">The chart fills in after a few epochs</text>`; return; }
  const W = 400, H = 150, pad = 8;
  const maxE = Math.max(hist[hist.length - 1].epoch, T.proj.train?.epochs || 0);
  const norm = key => { const v = hist.map(h => h[key]); const lo = Math.min(...v), hi = Math.max(...v); return [lo, hi - lo || 1]; };
  const line = (key, cls) => {
    const [lo, span] = norm(key);
    // light smoothing so the trend reads through GAN noise
    let s = hist[0][key];
    const pts = hist.map(h => { s = s * 0.7 + h[key] * 0.3; return `${(h.epoch / maxE * W).toFixed(1)},${(pad + (1 - (s - lo) / span) * (H - 2 * pad)).toFixed(1)}`; });
    return `<polyline class="${cls}" points="${pts.join(" ")}"/>`;
  };
  const best = T.proj.train?.best;
  svg.innerHTML = [30, 75, 120].map(y => `<line class="grid" x1="0" y1="${y}" x2="${W}" y2="${y}"/>`).join("")
    + (best ? `<line class="mark" x1="${best / maxE * W}" y1="0" x2="${best / maxE * W}" y2="${H}"/>` : "")
    + line("mel", "l1") + line("val", "l2");
}

// ------------------------------------------------------------ listen + export
function renderListen() {
  const p = T.proj, st = p.train || {};
  const saves = (p.saves || []).filter(s => s.available);
  $("#tr-listen").hidden = !saves.length;
  if (!saves.length) return;
  const busy = !!T.live;
  $("#tr-checks").innerHTML = `<div class="ckpt"><div class="row spread"><b>Real voice</b><span class="tag">original</span></div>
      <div class="row">${playBtn(p.real_preview, "Play the real voice")}<span class="meta">held out from training</span></div></div>`
    + saves.slice().reverse().map(s => `<div class="ckpt${s.epoch === st.best ? " best" : ""}">
      <div class="row spread"><b>Epoch ${s.epoch}</b><span class="tag${s.epoch === st.best ? " ok" : ""}">${s.epoch === st.best ? "best · " : ""}${s.val.toFixed(3)}</span></div>
      <div class="row">${playBtn(s.preview, `Play epoch ${s.epoch}`)}${busy ? "" : `<button class="linkbtn" data-export="${s.epoch}">${st.exported_epoch === s.epoch ? "Added ✓" : "Use this one"}</button>`}</div></div>`).join("");
  $$("[data-export]").forEach(b => b.onclick = () => exportVoice(+b.dataset.export));
}
async function exportVoice(epoch) {
  const st = T.proj.train || {};
  const name = $("#tr-name").value || st.voice_name;
  const overwrite = !!st.exported && st.exported === name;
  if (!overwrite && (await api("/api/voices")).some(v => v.name === name) &&
      !confirm(`A voice called “${name}” already exists. Replace it?`)) return;
  try { const r = await P("export", { epoch, name, overwrite: true }); follow(r.job, "export"); }
  catch (e) { showError(e.message, "Couldn't add the voice"); }
}
function onExportEvent(ev) {
  const d = $("#tr-done");
  d.hidden = false;
  if (!ev.final) d.innerHTML = `<div class="row"><b>Adding the voice…</b><span class="meta">${esc(ev.label || "")}</span></div>`;
}
function renderDone() {
  const p = T.proj, st = p.train || {}, d = $("#tr-done");
  if (T.live?.kind === "export") return;
  if (st.state === "exported" && st.exported) {
    d.hidden = false;
    d.innerHTML = `<div class="row spread"><div><h3 class="tr-h3">${esc(st.exported)} is ready</h3>
      <p class="meta">v2 · ${(st.sr || 40000) / 1000} kHz · epoch ${st.exported_epoch} · ${st.train_minutes} min of training audio · added to your voices</p></div>
      <div class="row"><button class="btn" id="tr-show-files">Show files</button><button class="btn primary" id="tr-use">Make a cover with it</button></div></div>`;
    $("#tr-use").onclick = async () => { showTab("covers"); await loadVoices(st.exported); window.scrollTo({ top: 0, behavior: "smooth" }); };
    $("#tr-show-files").onclick = () => api("/api/open-folder", { json: { voice: st.exported } }).catch(e => showError(e.message));
  } else if (st.state === "trained") {
    d.hidden = false;
    d.innerHTML = `<div class="row spread"><div><h3 class="tr-h3">Training finished${st.outcome === "early stop" ? " (it stopped improving)" : ""}</h3>
      <p class="meta">Listen to the saves above, then add the one you like. The best-scoring save is ${st.best ? "epoch " + st.best : "the last one"}.</p></div>
      <button class="btn primary" id="tr-add-best">Add the best one to my voices</button></div>`;
    $("#tr-add-best").onclick = () => exportVoice(st.best || st.epoch);
  } else d.hidden = true;
}

// ------------------------------------------------------------ overall render
function renderSteps() {
  const p = T.proj;
  $("#tr-steps").hidden = !p;
  if (!p) return;
  const keptMin = p.phrases.filter(x => x.kept).reduce((a, x) => a + x.dur, 0) / 60;
  const st = p.train || {};
  const info = [
    [p.ready_sources ? "done" : (p.sources.length ? "now" : ""), p.sources.length ? `${p.sources.filter(s => s.selected).length} ticked · ${p.ready_sources} ready` : "type a name"],
    [p.reference?.confirmed ? "done" : (p.speakers?.length ? "now" : ""), p.reference?.confirmed ? "confirmed" : p.speakers?.length ? "check the reference" : "after preparing"],
    [keptMin >= p.target_minutes ? "done" : (p.phrases.length ? "now" : ""), p.phrases.length ? `${keptMin.toFixed(1)} min kept` : "—"],
    [st.state === "exported" ? "done" : (st.state ? "now" : ""), st.state === "exported" ? `added as ${st.exported}` : st.epoch ? `epoch ${st.epoch}` : "—"],
  ];
  $$("#tr-steps .step").forEach((el, i) => { el.className = "step " + info[i][0]; $("span", el).textContent = info[i][1]; });
}
function render() {
  syncMode();
  const p = T.proj;
  $("#tr-add").hidden = $("#tr-prepare-row").hidden = !p;
  $("#tr-auto-row").hidden = !p || (p.train?.state === "exported");
  $("#tr-auto").disabled = !!T.live || !p?.sources.length;
  renderSteps();
  if (!p) {
    ["#tr-ref", "#tr-samples", "#tr-train", "#tr-listen", "#tr-done"].forEach(s => $(s).hidden = true);
    $("#tr-sources").innerHTML = ""; $("#tr-prep-progress").hidden = true;
    return;
  }
  renderSources();
  const todo = p.sources.filter(s => s.selected && s.status !== "ready");
  const mins = todo.reduce((a, s) => a + Math.min(s.duration || 600, 12 * 60), 0) / 60;
  const preparing = T.live?.kind === "prepare";
  $("#tr-prepare").disabled = !todo.length || !!T.live;
  $("#tr-prepare").textContent = todo.length ? `Download & prepare ${todo.length} source${todo.length > 1 ? "s" : ""}` : "Download & prepare";
  $("#tr-prepare-note").textContent = preparing ? "" : todo.length
    ? `Download → remove music → split into phrases → match the voice · about ${Math.max(1, Math.round(mins / 7))} min`
    : p.ready_sources ? "All ticked sources are prepared. Tick more (or add links/recordings) to add samples." : "Tick at least one source.";
  $("#tr-prep-progress").hidden = !preparing && T.live?.kind !== "auto";
  renderReference(); renderSamples(); renderTrainForm(); renderRun(); renderListen(); renderDone();
}

if (store.get("uc-tab", "covers") === "train") showTab("train");
