"use strict";
// Suite-level work: the running bar under the top bar on every module (any running job, live, with Pause / Resume), the
// overnight queue (its editor on DoubleAgent's page, and the drawer from the Queue button). Server side:
// undercover/core/worklist.py. Running jobs come from jobs.js; Pause / Resume and the queue state from /api/status
// (polled by app.js); the queue from /api/work.

const W = { last: null, open: false };

// which module owns a job (colour + label in the bar and the drawer): jobOwner in jobs.js
const ownerOf = kind => jobOwner(kind);
const MOD_NAME = { frontman: "FrontMan", doubleagent: "DoubleAgent", speakeasy: "SpeakEasy", safehouse: "SafeHouse" };
const glyph = m => `<svg class="ico" aria-hidden="true"><use href="#g-${m}"/></svg>`;

function fmtMin(m) {
  m = Math.round(m || 0);
  return m >= 60 ? `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, "0")} min` : `${m} min`;
}
function clock(hhmm) {
  if (!hhmm) return "";
  const [h, m] = hhmm.split(":").map(Number);
  return `${(h % 12) || 12}:${String(m).padStart(2, "0")} ${h < 12 ? "am" : "pm"}`;
}

// ------------------------------------------------------------ one running job, in words (the bar, the drawer, SafeHouse)
const COVER_STAGES = ["separating", "converting", "mixing"];
function jobProgress(j) {   // 0..1 over the whole job (a cover's events count each stage from 0)
  const p = j.progress;
  if (j.kind === "cover" && COVER_STAGES.includes(j.stage)) return (COVER_STAGES.indexOf(j.stage) + (p || 0)) / 3;
  if (j.kind === "speech" && j.of) return ((j.index ?? -1) + 1) / j.of;
  return p;
}
function jobLine(j) {
  const bits = [];
  if (j.pausing) bits.push("pausing…");
  else if (j.epoch && j.epochs) bits.push(`epoch ${j.epoch} of ${j.epochs}`);
  else if (j.kind === "speech" && j.of) bits.push(`sentence ${(j.index ?? -1) + 1} of ${j.of}`);
  else if (j.message) bits.push(j.message);
  else if (j.state === "queued") bits.push("waiting for the GPU");
  const left = j.total_left ?? j.eta;
  if (left != null && !j.pausing) bits.push(leftText(left));
  return bits.join(" · ");
}
const jobName = j => j.title || JOB_KIND[j.kind] || j.kind;

// ------------------------------------------------------------ the bar: any running job, live (jobs.js), plus Pause / Resume
W.work = null; W.watch = {};
function renderWorkBar(w) { W.work = w; renderBar(); }   // called from app.js pollStatus with /api/status's "work"
function renderBar() {
  const bar = $("#workbar"), w = W.work || {};
  if (typeof queueCount !== "function") return;   // shell.js (loaded last) isn't there yet: the job feed answered early
  queueCount(W.last ? W.last.queue.filter(i => ["queued", "paused", "running"].includes(i.state)).length
                    : (w.queued || 0) + (w.armed ? (w.running || []).length : 0));
  const run = runningJobs();
  const ORDER = ["auto", "cover", "speech", "install"];   // what the user is waiting for first, then voice work
  run.sort((a, b) => ((ORDER.indexOf(a.kind) + 1) || 9) - ((ORDER.indexOf(b.kind) + 1) || 9));
  const main = run[0];
  let text = "", state = "";
  if (main) {
    text = [jobName(main), jobLine(main)].filter(Boolean).join(" · ");
    state = "run";
  } else if (w.held) {
    const res = w.resume || [];
    text = "Voice work is paused" + (res.length ? ` · ${res.slice(0, 2).join(", ")}${res.length > 2 ? ` and ${res.length - 2} more` : ""}` : "") +
           (w.queued ? ` · ${w.queued} in the overnight queue` : "");
    state = "held";
  } else if (w.armed && w.queued) {
    text = `Overnight queue: ${w.queued} job${w.queued > 1 ? "s" : ""}` + (w.start_at ? `, starts at ${clock(w.start_at)}` : ", starting when the GPU is free");
    state = "armed";
  }
  bar.hidden = !text;
  document.documentElement.style.setProperty("--wb-h", text ? $(".workbar-wrap").offsetHeight + "px" : "0px");
  if (!text) return;
  bar.dataset.state = state;
  const owner = main ? ownerOf(main.kind) : "doubleagent";
  bar.dataset.owner = owner;
  $("#wb-who").innerHTML = `${glyph(owner)}${MOD_NAME[owner]}`;
  $("#wb-text").textContent = text;
  $("#wb-more").hidden = run.length < 2;
  $("#wb-more").textContent = run.length > 1 ? `+${run.length - 1} more` : "";
  const frac = main ? jobProgress(main) : null;
  $("#wb-bar").hidden = frac == null;
  if (frac != null) $("#wb-bar i").style.width = Math.round(frac * 100) + "%";
  const pausable = run.some(j => PAUSABLE_KINDS.has(j.kind)) || (w.running || []).length > 0;
  $("#wb-pause").hidden = state === "held" || !pausable;
  $("#wb-resume").hidden = !w.held;
  $("#wb-forget").hidden = !w.held || !(w.resume || []).length;
  $("#wb-main").dataset.goto = main ? owner : "doubleagent";
  $("#wb-main").title = main ? `Open ${MOD_NAME[owner]}` : "";
  $("#chip-train").hidden = true;   // the bar says it all
  document.documentElement.style.setProperty("--wb-h", $(".workbar-wrap").offsetHeight + "px");   // now its text is in
}
// every running job is watched, so its events move the bar as they come; the drawer and SafeHouse redraw too
onJobs(list => {
  for (const j of runningJobs()) if (!W.watch[j.id]) W.watch[j.id] = watchJob(j.id, ev => { if (ev.final) { W.watch[j.id]?.(); delete W.watch[j.id]; } });
  renderBar();
  if (!$("#q-drawer").hidden) renderRunning($("#qd-run"), { heading: "Running now" });
  if (!$("#view-library").hidden) renderRunning($("#lib-jobs"), { recent: true, always: true });
});
document.addEventListener("uc:module", e => { if (e.detail === "safehouse") { renderRunning($("#lib-jobs"), { recent: true, always: true }); refreshJobs(true); } });
// running jobs as rows (the drawer's "Running now"; SafeHouse uses the same with finished ones too)
function renderRunning(box, opts = {}) {
  if (!box) return;
  const ended = JF.list.filter(j => !isRunning(j) && j.kind !== "presep").slice(0, 5);   // the newest few that finished
  const list = opts.recent ? [...runningJobs(), ...ended] : runningJobs();
  box.hidden = !list.length && !opts.always;
  if (!list.length) { box.innerHTML = opts.always ? `<p class="meta">Nothing running. Covers, speech, training and queue jobs show here while they run, and for an hour after.</p>` : ""; return; }
  box.innerHTML = (opts.heading ? `<h3 class="qd-h">${opts.heading}</h3>` : "") + list.map(j => {
    const live = isRunning(j), owner = ownerOf(j.kind), f = live ? jobProgress(j) : null;
    const st = j.final?.state || j.state;
    const note = live ? jobLine(j) : `${{ done: "done", error: "failed", cancelled: "cancelled", paused: "paused", lost: "stopped" }[st] || st}` +
      (j.ended ? ` · ${new Date(j.ended * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}` : "") +
      (st !== "done" && j.final?.message ? ` · ${j.final.message}` : "");
    return `<div class="qd-job ${live ? "running" : "done"}" data-owner="${owner}">
      <div><span class="kind">${MOD_NAME[owner]} · ${esc(JOB_KIND[j.kind] || j.kind)}</span><b>${esc(jobName(j))}</b>${note ? `<small>${esc(note)}</small>` : ""}
        ${live ? `<div class="bar"><i style="width:${Math.round((f || 0) * 100)}%"></i></div>` : ""}</div>
      <div class="qside">${live && !opts.recent ? "" : live ? `<button class="linkbtn" type="button" data-jcancel="${j.id}">Cancel</button>` : ""}
        <button class="linkbtn" type="button" data-goto="${owner}">Open</button></div></div>`;
  }).join("");
  $$("[data-jcancel]", box).forEach(b => b.onclick = async () => {
    b.disabled = true; b.textContent = "Cancelling…";
    try { await api(`/api/jobs/${b.dataset.jcancel}/cancel`, { method: "POST" }); } catch (e) { showError(e.message, "Couldn't cancel that"); }
    refreshJobs(true);
  });
}
$("#wb-pause").onclick = async () => {
  $("#wb-pause").disabled = true;
  try { await api("/api/work/pause", { json: {} }); $("#wb-text").textContent = "Pausing… (training saves its place first)"; }
  catch (e) { showError(e.message, "Couldn't pause"); }
  $("#wb-pause").disabled = false;
  if (W.open) loadQueue();
};
$("#wb-forget").onclick = async () => {
  const b = $("#wb-forget");
  if (!b.classList.contains("confirm")) { b.classList.add("confirm"); b.textContent = "Forget it?"; setTimeout(() => { b.classList.remove("confirm"); b.textContent = "Dismiss"; }, 4000); return; }
  b.classList.remove("confirm"); b.textContent = "Dismiss";
  try { const r = await api("/api/work/forget", { json: {} }); renderWorkBar({ ...r, queued: r.queue.filter(i => ["queued", "paused"].includes(i.state)).length }); }
  catch (e) { showError(e.message, "Couldn't dismiss it"); }
  if (W.open) loadQueue();
};
$("#wb-resume").onclick = async () => {
  $("#wb-resume").disabled = true;
  try {
    const r = await api("/api/work/resume", { json: {} });
    if (r.problems?.length) showError(r.problems.join("\n"), "Some work couldn't resume");
    renderWorkBar({ ...r, queued: r.queue.filter(i => ["queued", "paused"].includes(i.state)).length });
  } catch (e) { showError(e.message, "Couldn't resume"); }
  $("#wb-resume").disabled = false;
  if (W.open) loadQueue();
};

// ------------------------------------------------------------ the overnight queue (DoubleAgent's page + the drawer)
async function loadQueue() {
  try { W.last = await api("/api/work"); } catch (e) { return; }
  renderQueue();
}
const jobWhat = i => ({ improve: `Improve ${esc(i.display || i.target)}`, cadence: `Cadence for ${esc(i.display || i.target)}`,
                        newcad: `New voice with cadence: ${esc(i.target)}` })[i.kind] || `Train a new voice: ${esc(i.target)}`;
const jobKind = i => ({ improve: "voice · improve", new: "voice · new", cadence: "cadence", newcad: "voice + cadence" })[i.kind] || esc(i.kind);
// the drawer: a short list, labelled by module and kind; the full editor stays on DoubleAgent's page
function renderDrawer() {
  renderRunning($("#qd-run"), { heading: "Running now" });
  const w = W.last;
  if (!w) { $("#qd-list").innerHTML = `<p class="meta">Loading…</p>`; return; }
  const left = w.queue.filter(i => ["queued", "paused", "running"].includes(i.state));
  $("#qd-total").innerHTML = left.length
    ? `<b class="mono">≈ ${fmtMin(w.total_min)}</b> of work · done by about <span class="mono">${clock(w.done_by)}</span>` +
      (w.armed ? (w.settings.start_at ? ` · starts at ${clock(w.settings.start_at)}` : " · running") : " · not started yet (start it in DoubleAgent)")
    : "Nothing queued. Add a voice to improve, or a person to train, and let it run overnight.";
  $("#qd-list").innerHTML = w.queue.map(i => {
    const run = i.state === "running", done = ["done", "failed", "skipped"].includes(i.state), owner = ownerOf(i.kind);
    const live = run ? (w.running.find(r => r.job === i.job) || {}) : {};
    const note = done ? `${i.state} · ${fmtMin(i.minutes)}${i.result?.note ? " · " + i.result.note : ""}` : run ? [live.message || "running", leftText(live.total_left ?? live.eta)].filter(Boolean).join(" · ")
      : i.state === "paused" ? "paused · Resume continues it" : i.note || "";
    return `<div class="qd-job ${run ? "running" : ""} ${done ? "done" : ""}" data-owner="${owner}">
      <div><span class="kind">${MOD_NAME[owner]} · ${jobKind(i)}</span><b>${jobWhat(i)}</b>${note ? `<small>${esc(note)}</small>` : ""}
        ${run ? `<div class="bar"><i style="width:${Math.round((live.fraction || 0) * 100)}%"></i></div>` : ""}</div>
      <div class="qside">${done ? "" : `<span>~${fmtMin((i.estimate || {}).total)}</span>`}
        ${run ? "" : `<button class="linkbtn" type="button" data-qd-rm="${i.id}">${done ? "Clear" : "Remove"}</button>`}</div></div>`;
  }).join("") || "";
  $$("#qd-list [data-qd-rm]").forEach(b => b.onclick = async () => {
    try { W.last = await api("/api/work/remove", { json: { id: b.dataset.qdRm } }); renderQueue(); }
    catch (e) { showError(e.message, "Couldn't remove that"); }
  });
}
function renderQueue() {
  const w = W.last; if (!w) return;
  if (!$("#q-drawer").hidden) renderDrawer();
  queueCount(w.queue.filter(i => ["queued", "paused", "running"].includes(i.state)).length);
  const left = w.queue.filter(i => ["queued", "paused", "running"].includes(i.state));
  $("#q-total").innerHTML = left.length
    ? `<b class="mono">≈ ${fmtMin(w.total_min)}</b> <span class="meta">· done by about <span class="mono">${clock(w.done_by)}</span>${w.armed && w.settings.start_at ? ` if it starts at ${clock(w.settings.start_at)}` : ""}</span>`
    : `<span class="meta">Nothing queued. Add a voice to improve, or a person to train, and let it run overnight.</span>`;
  const st = $("#q-start"), at = $("#q-at");
  st.hidden = at.hidden = !left.length;
  $("#q-stop").hidden = !w.armed || !left.length;
  st.textContent = w.armed && !w.settings.start_at ? "Running" : "Start now";
  st.disabled = w.armed && !w.settings.start_at;
  at.textContent = w.armed && w.settings.start_at ? `Starts at ${clock(w.settings.start_at)}` : `Start at ${clock($("#q-at-time").value)}`;
  $("#q-list").innerHTML = w.queue.map(i => {
    const e = i.estimate || {}, run = i.state === "running", done = ["done", "failed", "skipped"].includes(i.state);
    const what = jobWhat(i);
    const live = run ? (w.running.find(r => r.job === i.job) || {}) : {};
    const chips = done ? `<span class="qchip ${i.state === "done" ? "ok" : "bad"}">${esc(i.state)} · ${fmtMin(i.minutes)}</span>`
      : (i.kind === "cadence" ? [["clips", "find clips"], ["transcribe", "transcribe"], ["measure", "measure"], ["test", "test"]]
         : i.kind === "newcad" ? [["prepare", "prepare"], ["train", "train"], ["transcribe", "transcribe"], ["test", "cadence"]]
         : [["prepare", "prepare"], ["train", "train"], ["compare", i.kind === "improve" ? "compare" : "check"]])
          .filter(([k]) => e[k]).map(([k, l]) => `<span class="qchip">${l} ~${fmtMin(e[k])}</span>`).join("");
    const note = done ? esc(i.result?.note || "") : run ? esc([live.message || "running", leftText(live.total_left ?? live.eta)].filter(Boolean).join(" · ")) : i.state === "paused" ? "paused · Resume (top of the page) continues it"
      : i.now ? "starts as soon as the GPU is free" : esc(i.note || "");
    return `<div class="qjob ${run ? "running" : ""} ${done ? "done" : ""}" draggable="${!run && !done}" data-id="${i.id}">
      <span class="grip" aria-hidden="true">${run || done ? "" : "⋮⋮"}</span>
      <div><b>${what}</b>${note ? `<p class="meta">${note}</p>` : ""}<div class="qchips">${chips}</div>
        ${run ? `<div class="bar"><i style="width:${Math.round((live.fraction || 0) * 100)}%"></i></div>` : ""}</div>
      <div class="qside">${done ? "" : `<span class="mono">~${fmtMin(e.total)}</span>`}
        ${run ? "" : `<button class="linkbtn" data-rm="${i.id}">${done ? "Clear" : "Remove"}</button>`}</div></div>`;
  }).join("");
  $$("#q-list [data-rm]").forEach(b => b.onclick = async () => { W.last = await api("/api/work/remove", { json: { id: b.dataset.rm } }); renderQueue(); });
  dragSort();
  $("#q-finish").checked = !!w.settings.finish_by;
  $("#q-awake").checked = w.settings.keep_awake !== false;
  renderReport(w.report);
}
function renderReport(r) {
  const box = $("#q-report");
  box.hidden = !r || !r.results?.length;
  if (box.hidden) return;
  $("#q-report-when").textContent = `Ran ${r.started}${r.finished ? " – " + r.finished.slice(11) : ""} · ${r.results.length} job${r.results.length > 1 ? "s" : ""}`;
  $("#q-report-list").innerHTML = r.results.map(x => {
    const score = x.before != null || x.after != null
      ? `${x.before != null ? x.before.toFixed(2) + " → " : ""}${x.after != null ? x.after.toFixed(2) : "?"}${x.replaced ? " ✓ replaced" : x.kind === "new" ? " ✓ new" : x.state === "done" ? " · kept the old" : ""}` : x.state;
    const what = x.kind === "improve" ? (x.replaced ? "improved" : "tried") : x.kind === "cadence" ? (x.replaced ? "cadence" : "cadence tried")
      : x.kind === "newcad" ? "new voice + cadence" : "new voice";
    const cad = x.kind === "cadence" ? x : x.cadence;   // cadence numbers: Deep Cover only
    const deep = cad && cad.likeness != null ? `<p class="meta" data-deep>cadence: likeness ${cad.likeness} · gap ${cad.cadence_gap} · WER ${cad.wer} · from ${cad.data_minutes} min</p>` : "";
    const up = x.replaced || (["new", "newcad"].includes(x.kind) && x.state === "done");
    return `<div class="qres"><div><b>${esc(x.voice ? voiceDisplay(x.voice) : x.display || x.target)}</b> ${what}<p class="meta">${esc(x.note || "")}</p>${deep}</div>
      <div class="mono ${up ? "up" : "same"}" ${x.kind === "cadence" ? "data-deep" : ""}>${esc(score)}</div></div>`;
  }).join("");
}
function dragSort() {
  let dragged = null;
  $$("#q-list .qjob[draggable=true]").forEach(el => {
    el.ondragstart = () => { dragged = el; el.classList.add("dragging"); };
    el.ondragend = async () => {
      el.classList.remove("dragging"); dragged = null;
      W.last = await api("/api/work/order", { json: { ids: $$("#q-list .qjob").map(x => x.dataset.id) } });
      renderQueue();
    };
    el.ondragover = e => {
      e.preventDefault();
      if (!dragged || dragged === el) return;
      const r = el.getBoundingClientRect();
      el.parentNode.insertBefore(dragged, e.clientY < r.top + r.height / 2 ? el : el.nextSibling);
    };
  });
}
// ------------------------------------------------------------ the picker: improve my voices / new voices
const PICK = { sel: new Set(), cands: [], timer: null, added: new Set() };
$("#q-mode").addEventListener("click", e => {
  const b = e.target.closest("button"); if (!b) return;
  $$("#q-mode button").forEach(x => x.setAttribute("aria-pressed", x === b));
  $("#q-improve").hidden = b.dataset.v !== "improve";
  $("#q-new").hidden = b.dataset.v !== "new";
  $("#q-cadence").hidden = b.dataset.v !== "cadence";
  if (b.dataset.v === "new") $("#q-name").focus();
});
async function loadCandidates() {
  try { PICK.cands = await api("/api/work/candidates"); noteVoices(PICK.cands); } catch (e) { return; }
  const can = PICK.cands.filter(c => c.can), off = PICK.cands.filter(c => !c.can);
  [...PICK.sel].forEach(k => { if (!can.some(c => c.voice === k && !c.queued)) PICK.sel.delete(k); });
  $("#q-cands").innerHTML = can.length ? can.map(c => {
    const facts = [c.minutes ? `${Math.round(c.minutes)} min of ${c.singing ? "singing" : c.singing === false ? "speech" : "voice"}` : c.trained ? "" : "downloaded",
                   c.likeness != null ? `likeness ${(+c.likeness).toFixed(2)}` : "", c.estimate ? `≈ ${fmtMin(c.estimate)}` : ""].filter(Boolean);
    return `<label class="vcard ${c.queued ? "queued" : ""}">
      <input type="checkbox" data-v="${esc(c.voice)}" ${c.queued ? "disabled" : PICK.sel.has(c.voice) ? "checked" : ""}>
      <span><b>${vname(c.voice)}</b>${c.person && c.person.toLowerCase() !== voiceDisplay(c.voice).replace(/-uc$/, "").replace(/-/g, " ").toLowerCase() ? ` <span class="meta">${esc(c.person)}</span>` : ""}
        <span class="qchips"><span data-deep>${facts.map(f => `<span class="qchip">${esc(f)}</span>`).join("")}</span>${c.queued ? `<span class="qchip ok">in the queue</span>` : ""}</span>
        <span class="meta">${esc(c.note)}</span></span></label>`;
  }).join("") : `<p class="meta">No voices yet. Use "New voices" to train one.</p>`;
  $$("#q-cands input[type=checkbox]").forEach(cb => cb.onchange = () => { cb.checked ? PICK.sel.add(cb.dataset.v) : PICK.sel.delete(cb.dataset.v); pickSummary(); });
  $("#q-other").hidden = !off.length;
  $("#q-cands-off").textContent = off.map(c => voiceDisplay(c.voice)).join(", ") + ". These were downloaded and DoubleAgent doesn't know whose voices they are. To train your own, add the person under New voices.";
  pickSummary();
  renderCadPicker();
  renderVoiceCards(PICK.cands);   // doubleagent.js
}
// "Cadence": voices that can get one (or a better one), ticked and added to tonight's queue
const CADPICK = new Set();
function renderCadPicker() {
  const list = PICK.cands.filter(c => c.cadence?.can);
  [...CADPICK].forEach(k => { if (!list.some(c => c.voice === k && !c.cadence.queue)) CADPICK.delete(k); });
  $("#q-cad-cands").innerHTML = list.length ? list.map(c => {
    const cd = c.cadence;
    return `<label class="vcard ${cd.queue ? "queued" : ""}"><input type="checkbox" data-cv="${esc(c.voice)}" ${cd.queue ? "disabled" : CADPICK.has(c.voice) ? "checked" : ""}>
      <span><b>${vname(c.voice)}</b> <span class="qchips">${cd.has ? `<span class="qchip ok">has one</span>` : ""}${cd.queue ? `<span class="qchip ok">in the queue</span>` : ""}</span>
        <span class="meta">${cd.has ? "tries again; kept only if better" : esc(cd.why || "")}</span></span></label>`;
  }).join("") : `<p class="meta">No voice can get a cadence yet: it needs a voice trained on speech, or a downloaded voice DoubleAgent has checked.</p>`;
  $$("#q-cad-cands input[type=checkbox]").forEach(cb => cb.onchange = () => { cb.checked ? CADPICK.add(cb.dataset.cv) : CADPICK.delete(cb.dataset.cv); cadSummary(); });
  cadSummary();
}
function cadSummary() {
  $("#q-cad-sum").textContent = CADPICK.size ? `${CADPICK.size} selected · a few minutes each` : "Tick the voices.";
  $("#q-cad-add").disabled = !CADPICK.size;
}
$("#q-cad-add").onclick = async () => {
  for (const v of [...CADPICK]) {
    try { W.last = await api("/api/work/add", { json: { kind: "cadence", target: v } }); }
    catch (e) { showError(e.message, `Couldn't add ${v}`); }
  }
  CADPICK.clear(); renderQueue(); loadCandidates();
};
function pickSummary() {
  const chosen = PICK.cands.filter(c => PICK.sel.has(c.voice));
  const mins = chosen.reduce((a, c) => a + (c.estimate || 0), 0);
  $("#q-sel-sum").textContent = chosen.length ? `${chosen.length} selected · ≈ ${fmtMin(mins)}` : "Tick the voices to improve.";
  $("#q-add-sel").disabled = !chosen.length;
  $("#q-add-sel").textContent = chosen.length ? `Add ${chosen.length} to the queue` : "Add to the queue";
}
$("#q-add-sel").onclick = async () => {
  const chosen = PICK.cands.filter(c => PICK.sel.has(c.voice));
  for (const c of chosen) {
    try { W.last = await api("/api/work/add", { json: { kind: c.kind, target: c.target } }); }
    catch (e) { showError(e.message, `Couldn't add ${c.voice}`); }
  }
  PICK.sel.clear(); renderQueue(); loadCandidates();
};
$("#q-name").addEventListener("input", () => {
  clearTimeout(PICK.timer);
  const q = $("#q-name").value.trim();
  if (q.length < 2) { $("#q-sugg").innerHTML = ""; return; }
  $("#q-sugg").innerHTML = `<p class="meta">Looking up “${esc(q)}”…</p>`;
  PICK.timer = setTimeout(async () => {
    let list = [];
    try { list = await api(`/api/work/lookup?q=${encodeURIComponent(q)}`); } catch (e) {}
    if ($("#q-name").value.trim() !== q) return;   // typed on meanwhile
    const queued = new Set((W.last?.queue || []).filter(i => i.kind === "new" && ["queued", "paused", "running"].includes(i.state)).map(i => i.target.toLowerCase()));
    $("#q-sugg").innerHTML = list.map(x => {
      const inq = queued.has(x.name.toLowerCase());
      return `<div class="srow"><div><b>${esc(x.name)}</b> <span class="qchip">${x.kind === "artist" ? "artist" : "person"}</span><p class="meta">${esc(x.detail)}</p></div>
        <button class="btn" type="button" data-name="${esc(x.name)}" ${inq ? "disabled" : ""}>${inq ? "In the queue" : "Add"}</button></div>`;
    }).join("") || `<p class="meta">Nothing found.</p>`;
    $$("#q-sugg [data-name]").forEach(b => b.onclick = async () => {
      b.disabled = true;
      try { W.last = await api("/api/work/add", { json: { kind: "new", target: b.dataset.name } }); b.textContent = "In the queue"; renderQueue(); }
      catch (e) { b.disabled = false; showError(e.message, "Couldn't add that"); }
    });
  }, 450);
});
$("#q-start").onclick = async () => { W.last = await api("/api/work/start", { json: {} }); renderQueue(); pollStatusSoon(); };
$("#q-at").onclick = async () => { W.last = await api("/api/work/start", { json: { at: $("#q-at-time").value } }); renderQueue(); pollStatusSoon(); };
$("#q-at-time").onchange = () => renderQueue();
$("#q-stop").onclick = async () => { W.last = await api("/api/work/start", { json: { stop: true } }); renderQueue(); pollStatusSoon(); };
$("#q-finish").onchange = async e => { W.last = await api("/api/work/settings", { json: { finish_by: e.target.checked ? "07:00" : null } }); renderQueue(); };
$("#q-awake").onchange = async e => { W.last = await api("/api/work/settings", { json: { keep_awake: e.target.checked } }); renderQueue(); };
$("#q-dismiss").onclick = async () => { W.last = await api("/api/work/dismiss", { json: {} }); renderQueue(); };
function pollStatusSoon() { setTimeout(() => api("/api/status").then(s => renderWorkBar(s.work)).catch(() => {}), 300); }

// refresh the queue while DoubleAgent's page or the drawer is open
setInterval(() => { W.open = !$("#view-train").hidden || !$("#q-drawer").hidden; if (W.open) loadQueue(); }, 5000);
document.addEventListener("uc:module", e => { if (e.detail === "doubleagent") { loadQueue(); loadCandidates(); } });

// a job finished somewhere (this page, another tab, the queue, the API): refresh what it may have changed
document.addEventListener("uc:jobdone", e => {
  const k = e.detail.kind;
  if (!["train", "train-auto", "train-export", "train-prepare", "train-transcribe", "night", "install", "auto"].includes(k)) return;
  if (typeof loadVoices === "function" && ["train", "train-auto", "train-export", "night", "install", "auto"].includes(k)) loadVoices();
  loadQueue(); loadCandidates();
  if (typeof seLoad === "function" && !$("#view-speakeasy").hidden) seLoad();
  if (typeof refresh === "function" && typeof T !== "undefined" && T.proj) refresh();
});
