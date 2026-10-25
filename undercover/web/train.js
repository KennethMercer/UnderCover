"use strict";
// DoubleAgent's new voice wizard: find clips of a person, keep only their voice, review samples, train an RVC model.
// Uses helpers from app.js ($, $$, esc, api, showError, store, fmtTime, VOL, loadVoices).

const T = { list: [], proj: null, filter: store.get("uc-train-filter", "kept"), mode: "speaking", es: null,
            audio: new Audio(), playing: null, live: null, busy: false };

// ------------------------------------------------------------ module switch (shell.js showTab)
document.addEventListener("uc:module", e => {
  const train = e.detail === "doubleagent";
  $("#drop-overlay div").textContent = train ? "Drop recordings of the person to add them as a source"
                                             : "Drop a song to load it, or a voice (.ucvoice, .zip or .pth) to install it";
  if (train && !T.loaded) { T.loaded = true; loadProjects(); }
  else if (train && !T.live) {   // something started training meanwhile (another page, the queue): show that project
    const p = trainingProject();
    if (p && p !== T.proj?.name) loadProjects(p);
  }
  if (!train) stopClip();
});
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
// the project a running job works on (training, preparing, adding the voice), if any
const trainingProject = () => runningJobs().find(j => j.project)?.project || null;
async function loadProjects(select) {
  try { T.list = await api("/api/train/projects"); } catch (e) { return showError(e.message, "Couldn't load training projects"); }
  if (select === undefined) { await refreshJobs(); select = trainingProject() ?? undefined; }
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
function closeStream() { if (T.es) { T.es(); T.es = null; } T.live = null; }
function follow(id, kind) {
  closeStream();
  T.live = { id, kind };
  T.es = watchJob(id, ev => {   // jobs.js: the live stream, or progress polled while it's quiet
    if (ev.synthetic && kind === "train" && !ev.epochs) return;   // a summary without epochs: nothing to chart
    if (kind === "prepare") onPrepareEvent(ev);
    else if (kind === "auto") onAutoEvent(ev);
    else if (kind === "train") onTrainEvent(ev);
    else if (kind === "export") onExportEvent(ev);
    if (ev.final) {
      closeStream();
      if (ev.state === "error" || ev.state === "lost") showError(ev.message, kind === "train" ? "Training stopped" : "That didn't work");
      else if (ev.state === "paused") { /* the Pause bar at the top says so, with Resume */ }
      else if (ev.state === "cancelled" && kind === "train" && ev.message) showError(ev.message, "Training stopped", null, true);   // "The saves made before the stop are kept."
      else if (ev.message && kind === "prepare" && ev.message.length) showError(ev.message, "Some sources were skipped", null, true);
      if (kind === "auto") {
        clearInterval(T.autoTimer); $("#tr-prep-progress").hidden = true;
        if (ev.state === "done") loadVoices();   // the card after training: doubleagent.js, on uc:jobdone
      }
      if (ev.state === "done" && ev.pending_for && ev.message)   // it landed beside a voice that was already there: it waits for the ear
        showError(ev.message, `Waiting beside ${voiceDisplay(ev.pending_for)}`, { label: "Listen to both", run: () => libOpenAb(ev.pending_for) }, true);
      if (kind === "export" && ev.state === "done" && T.replaced && !ev.pending_for && ev.voice)   // a confirmed replace: the old one is set aside, and Undo puts it back
        showError(`${voiceDisplay(ev.voice)} is the new one now. The old one is set aside, not deleted.`, "Replaced", { label: "Undo", run: () => undoImp(ev.voice) }, true);
      if (kind === "export") T.replaced = false;
      refresh().then(() => { if (kind === "export" && ev.voice) loadVoices(); });
    }
  });
  render();
}

// ------------------------------------------------------------ the one way to start a voice (DoubleAgent's line, doubleagent.js)
// Now: a project for the person (creating it searches for clips; an existing one searches again), then Do it all.
// Tonight: a new-voice item in the overnight queue, which SafeHouse's Running room shows.
async function trainVoice(person, mode, tonight) {
  if (tonight) {
    W.last = await api("/api/work/add", { json: { kind: "new", target: person, mode } });   // the queue keeps Speaking / Singing
    renderQueue(); refreshJobs(true);
    return W.last;   // `moved` is there when the voice was training live (it was paused and moved to tonight)
  }
  T.list = await api("/api/train/projects");
  const same = T.list.find(x => (x.person || "").toLowerCase() === person.toLowerCase());
  let data;
  if (same) {   // a fresh search; if it fails (offline, nothing new), the clips already prepared still train
    try { data = await api(`/api/train/projects/${same.name}/search`, { json: { person, mode } }); }
    catch (e) { if (!same.ready_sources) throw e; data = same; }
  } else data = await api("/api/train/projects", { json: { person, mode } });
  if (data.warning && !(data.sources || []).length) throw new Error(data.warning);
  await loadProjects(data.name);
  const r = await P("auto", {});
  follow(r.job, "auto");
  refreshJobs(true);
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

// the list stays short: the first SOURCES_SHOWN, and any source that's prepared, failed, under way or added by hand;
// "Show all" opens the rest (it says how many of them are ticked, since ticked sources get prepared shown or not)
const SOURCES_SHOWN = 10;
function renderSources() {
  const p = T.proj, box = $("#tr-sources"), more = $("#tr-sources-more");
  if (!p || !p.sources.length) { box.innerHTML = ""; more.hidden = true; return; }
  if (T.allFor !== p.name) { T.allFor = p.name; T.allSources = false; }   // another project starts short again
  const keep = (s, i) => T.allSources || i < SOURCES_SHOWN || s.status !== "found" || s.user;
  const shown = p.sources.filter(keep), hidden = p.sources.filter((s, i) => !keep(s, i));
  more.hidden = !hidden.length && !(T.allSources && p.sources.length > SOURCES_SHOWN);
  if (hidden.length) {
    const ticked = hidden.filter(s => s.selected).length;
    more.textContent = `Show all ${p.sources.length} (${hidden.length} more${ticked ? `, ${ticked} of them ticked` : ""})`;
  } else more.textContent = "Show fewer";
  box.innerHTML = shown.map(s => {
    const mins = s.duration ? fmtTime(s.duration) : "";
    let side = `<span class="mono meta">${mins}</span>`;
    if (s.status === "ready") side = `<span class="tag ok">ready · ${s.extra?.phrases ?? 0} phrases</span>` + side;
    else if (s.status === "failed") side = `<span class="tag bad">failed</span>` + side;
    else if (s.status !== "found") side = `<span class="tag warn">${esc(s.status)}…</span>` + side;
    if (s.user || s.status === "ready" || s.status === "failed") side += `<button class="linkbtn" data-rm="${esc(s.id)}">Remove</button>`;
    const sub = facts([s.channel || "", s.note ? { t: upFirst(s.note), muted: true } : ""]);
    return `<label class="src"><input type="checkbox" data-id="${esc(s.id)}" ${s.selected ? "checked" : ""} ${s.status === "ready" ? "disabled" : ""}>
      <span><b>${esc(s.title)}</b>${sub}${s.error ? `<small class="err">${esc(s.error)}</small>` : ""}</span>
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
      b.classList.add("confirm"); arm(b); b.textContent = `Remove ${src.extra?.phrases ?? ""} phrases?`.replace("  ", " ");
      setTimeout(() => { b.classList.remove("confirm"); disarm(b); b.textContent = "Remove"; }, 4000);
      return;
    }
    if (src && src.status === "ready" && !armedFor(b)) return;   // a double click isn't a confirmation
    try { await openProject(T.proj.name, await P("sources", { id: b.dataset.rm, remove: true })); }
    catch (err) { showError(err.message, "Couldn't remove that source"); }
  });
}

$("#tr-sources-more").onclick = () => { T.allSources = !T.allSources; renderSources(); };
$("#tr-prepare").onclick = async () => {
  try { const r = await P("prepare"); follow(r.job, "prepare"); }
  catch (e) { showError(e.message, "Can't prepare yet"); }
};
// the voice name only when the field is this project's (it keeps the last project's name while this one has no clips:
// a new Donald Trump project once sent "barack-obama-uc"; the server refuses another person's voice name too)
const ownName = () => ($("#tr-name").dataset.proj === T.proj?.name && $("#tr-name").value) || undefined;
$("#tr-auto").onclick = async () => {
  try { const r = await P("auto", { voice_name: ownName() }); follow(r.job, "auto"); }
  catch (e) { showError(e.message, "Can't start"); }
};
function onAutoEvent(ev) {
  // "Do it all" (or a one-shot cover training this voice): one progress line, and the page refreshes itself
  // every 15 s so sources, samples, the chart and saves fill in as they happen.
  $("#tr-prep-progress").hidden = !!ev.final;
  if (ev.final) return;
  const f = ev.frac ?? ev.fraction ?? 0;
  $("#tr-prep-bar").style.width = Math.round(f * 100) + "%";
  const left = ev.total_left ?? ev.eta;
  $("#tr-prep-pct").textContent = (left != null ? leftText(left) : Math.round(f * 100) + "%");
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
  const sung = p.phrases.filter(x => x.kept && x.sung).reduce((a, x) => a + x.dur, 0) / 60;
  $("#tr-minutes").textContent = `${kept.toFixed(1)} min of clean voice` + (sung ? ` (${sung.toFixed(1)} sung)` : "");
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
    // a retrained voice waiting beside its voice is named <voice>-next: a training must not be pointed at it (its export would replace
    // a candidate nobody has heard), so that name is never the default
    const own = st.voice_name && !/-next$/.test(st.voice_name) ? st.voice_name : `${p.name}-uc`;
    $("#tr-name").value = own; $("#tr-name").dataset.proj = p.name;
    $("#tr-length").value = st.length || "auto"; $("#tr-sr").value = String(st.sr || p.defaults.sr);
  }
  const running = T.live?.kind === "train";
  const paused = st.state === "paused";
  const btn = $("#tr-start");
  btn.textContent = paused ? "Resume training" : (st.epoch ? "Train again from scratch" : "Start training");
  const q = p.queued;   // this project is in tonight's queue with its own choices (the server's `queued`)
  btn.disabled = running || T.live?.kind === "prepare" || kept < 1 || !!q;
  $("#tr-tonight").hidden = !!q || running || paused; $("#tr-tonight").disabled = T.live?.kind === "prepare" || kept < 1;
  $("#tr-queued").hidden = !q;
  if (q) {
    $("#tr-queued-say").textContent = q.state === "running" ? "· training now, from the queue" : q.state === "paused" ? "· paused; Resume continues it"
      : q.now ? "· starts as soon as the GPU is free" : "· it trains while the PC is free, with the sources and choices you set here";
    $("#tr-unqueue").disabled = q.state === "running";
  }
  ["#tr-name", "#tr-length", "#tr-sr"].forEach(s => $(s).disabled = running || paused || !!q);
  const steps = p.defaults.steps[$("#tr-length").value] || p.defaults.steps.auto;
  const est = steps * (st.sec_per_step || 0.3) + 60;   // + data preparation
  $("#tr-start-note").innerHTML = q ? ""
    : kept < 1 ? "Keep at least 1 minute of samples first."
    : paused ? `Paused at epoch ${st.epoch}. <button class="linkbtn" id="tr-fresh">Start over instead</button>`
    : running ? "" : `≈ ${Math.max(1, Math.round(est / 60))} min on this GPU`;
  const fresh = $("#tr-fresh");
  if (fresh) fresh.onclick = () => startTraining(true);
}
$("#tr-start").onclick = () => startTraining(false);
// Train tonight (the Deep path): this project, with its ticked sources and these choices, goes into tonight's queue
$("#tr-tonight").onclick = async () => {
  const b = $("#tr-tonight"); b.disabled = true;
  try {
    const r = await api("/api/work/add", { json: { kind: "project", project: T.proj.name, voice_name: ownName(), length: $("#tr-length").value, sr: +$("#tr-sr").value } });
    if (!workMoved(r)) showError(`${T.proj.person || T.proj.name} trains tonight with your choices.${startsNote(r)} SafeHouse's Running room shows it.`, "On it", null, true);
    if (typeof refreshJobs === "function") refreshJobs(true);
    await refresh();
  } catch (e) { b.disabled = false; showError(e.message, "Couldn't queue it"); }
};
$("#tr-unqueue").onclick = async () => {
  const q = T.proj?.queued; if (!q) return;
  $("#tr-unqueue").disabled = true;
  try { await api("/api/work/remove", { json: { id: q.id } }); await refresh(); }
  catch (e) { $("#tr-unqueue").disabled = false; showError(e.message, "Couldn't take it out"); }
};
setInterval(() => { if (T.proj?.queued && !$("#view-train").hidden) refresh(); }, 10000);   // the queue starts it on its own: the line follows
async function startTraining(fresh) {
  const st = T.proj.train || {};
  if (!fresh && st.epoch && st.state !== "paused" && !confirm("Train again from scratch? The saves from the last run are replaced.")) return;
  try {
    const r = await P("train", { voice_name: ownName(), length: $("#tr-length").value, sr: +$("#tr-sr").value, fresh });
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
  if (ev.history) {   // after a reload the events are replayed: the project's saved history already has the early epochs
    const h = T.proj.history = T.proj.history || [];
    if (!h.length || ev.history.epoch > h[h.length - 1].epoch) { h.push(ev.history); drawChart(); }
  }
  if (ev.saved) refresh();
  renderRun();
}
function renderRun() {
  const p = T.proj, st = p.train || {}, ev = T.trainEv;
  const running = T.live?.kind === "train";
  const show = running || (p.history?.length || 0) > 0 || st.state === "paused" || st.state === "stopped";
  $("#tr-run").hidden = !show || $("#tr-train").hidden;
  if ($("#tr-run").hidden) return;
  const auto = T.live?.kind === "auto";
  $("#tr-pause").hidden = !(running || auto); $("#tr-stop").hidden = !(running || auto || st.state === "paused");
  const epoch = ev?.epoch ?? st.epoch ?? 0, total = ev?.epochs ?? st.epochs ?? 0;
  const h = $("#tr-run-h");
  if (running && ev?.stage === "preparing") h.textContent = `Getting ready: ${ev.note || ""}`;
  else if (running && ev?.note === "paused for a cover") h.textContent = "Making your cover first…";
  else if (running) h.textContent = "Training…";
  else h.textContent = { paused: "Paused", stopped: "Stopped", trained: "Training finished", exported: "Training finished", failed: "Training failed" }[st.state] || "Training";
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
      <div class="row">${playBtn(s.preview, `Play epoch ${s.epoch}`)}${busy ? "" : `<button class="linkbtn" data-export="${s.epoch}">${st.waiting && st.waiting.epoch === s.epoch ? `Waiting beside ${esc(voiceDisplay(st.waiting.voice))}` : st.exported_epoch === s.epoch ? "Added ✓" : "Use this one"}</button>`}</div></div>`).join("");
  $$("[data-export]").forEach(b => b.onclick = () => exportVoice(+b.dataset.export));
}
// "Use this one" onto a voice that's already there: replace it (the old one is set aside, Undo puts it back) or let the two wait side by
// side for the blind A/B (the new one lands as <voice>-next). Nothing is replaced unheard unless this is answered "Replace".
function askReplace(name) {
  return new Promise(resolve => {
    const dlg = $("#replace-dlg"), who = voiceDisplay(name);
    $("#replace-q").textContent = `Replace ${who} with this one?`;
    $("#replace-say").textContent = `The current voice is set aside, not deleted. Undo puts it back. Or let them wait side by side: you hear both read the same line, blind, and keep the one you like.`;
    const done = v => { dlg.close(); resolve(v); };
    $("#replace-yes").onclick = () => done("replace"); $("#replace-wait").onclick = () => done("wait"); $("#replace-no").onclick = () => done(null);
    dlg.oncancel = () => resolve(null);   // Esc
    dlg.showModal();
    $("#replace-wait").focus();
  });
}
async function exportVoice(epoch) {
  const st = T.proj.train || {};
  const name = $("#tr-name").value || st.voice_name;
  let replace = false, exists = false;
  try {
    exists = (await api("/api/voices")).some(v => v.name === name);
    if (exists) {
      const choice = await askReplace(name);
      if (!choice) return;
      replace = choice === "replace";
    }
    // overwrite without replace: it lands beside the voice as <voice>-next, for the A/B
    const r = await P("export", { epoch, name, ...(replace ? { replace: true } : exists ? { overwrite: true } : {}) });
    T.replaced = replace;
    follow(r.job, "export");
  } catch (e) { showError(e.message, "Couldn't add the voice"); }   // the server's own words: a voice in use, a retrained one already waiting
}
function onExportEvent(ev) {
  const d = $("#tr-done");
  d.hidden = false;
  if (!ev.final) d.innerHTML = `<div class="row"><b>Adding the voice…</b><span class="meta">${esc(ev.label || "")}</span></div>`;
}
function renderDone() {
  const p = T.proj, st = p.train || {}, d = $("#tr-done");
  if (T.live?.kind === "export") return;
  if (st.state === "exported" && st.waiting) {   // landed beside a voice that was already there: not "Added", waiting for the ear
    d.hidden = false;
    d.innerHTML = `<div class="row spread"><div><h3 class="tr-h3">Waiting beside ${esc(voiceDisplay(st.waiting.voice))}</h3>
      <p class="meta">${esc(voiceDisplay(st.waiting.voice))} was already in your voices, so this one waits beside it (epoch ${st.waiting.epoch}). Listen to both on its voice card and keep the one you like; the other is set aside, not deleted.</p></div>
      <button class="btn primary" id="tr-wait-ab">Listen to both</button></div>`;
    $("#tr-wait-ab").onclick = () => libOpenAb(st.waiting.voice);
  } else if (st.state === "exported" && st.exported) {
    d.hidden = false;
    d.innerHTML = `<div class="row spread"><div><h3 class="tr-h3">${esc(st.exported)} is ready</h3>
      <p class="meta">v2 · ${(st.sr || 40000) / 1000} kHz · epoch ${st.exported_epoch} · ${st.train_minutes} min of training audio · added to your voices</p></div>
      <div class="row"><button class="btn" id="tr-show-files">Show files</button><button class="btn primary" id="tr-use">Make a cover with it</button></div></div>`;
    $("#tr-use").onclick = async () => { showTab("frontman"); await loadVoices(st.exported); window.scrollTo({ top: 0, behavior: "smooth" }); };
    $("#tr-show-files").onclick = () => api("/api/open-folder", { json: { voice: st.exported } }).catch(e => showError(e.message));
  } else if (st.state === "trained") {
    d.hidden = false;
    d.innerHTML = `<div class="row spread"><div><h3 class="tr-h3">Training finished${st.outcome === "early stop" ? " (it stopped improving)" : ""}</h3>
      <p class="meta">Listen to the saves above, then add the one you like. The best-scoring save is ${st.best ? "epoch " + st.best : "the last one"}.</p></div>
      <button class="btn primary" id="tr-add-best">Add the best one to my voices</button></div>`;
    $("#tr-add-best").onclick = () => exportVoice(st.best || st.epoch);
  } else if (st.state === "stopped") {   // stopped on purpose: the saves are kept, nothing is offered as "the best one"
    d.hidden = false;
    d.innerHTML = `<div class="row spread"><div><h3 class="tr-h3">Training stopped</h3>
      <p class="meta">The saves made before the stop are kept.</p></div></div>`;
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


// a job on the open project that this page didn't start (Resume at the top, the queue, another tab): follow it too
const TRAIN_KIND = { train: "train", "train-prepare": "prepare", "train-auto": "auto", "train-export": "export" };
onJobs(() => {
  if (!T.proj || T.live) return;
  const j = runningJobs().find(x => x.project === T.proj.name && TRAIN_KIND[x.kind]);
  if (j && T.picked !== j.id) { T.picked = j.id; refresh(); }   // openProject picks up its job and follows it (once)
});
