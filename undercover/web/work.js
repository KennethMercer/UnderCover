"use strict";
// Suite-level work: the running bar under the top bar on every module (any running job, live, with Pause / Resume), the
// overnight queue (its editor on DoubleAgent's page), SafeHouse's Running room, and the drawer from the strip. Server side:
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
// the queue's start time: the saved one when there is one, else the last one chosen here (Running room and DoubleAgent's panel share it)
const startTime = () => $("#rn-at-time").value || $("#q-at-time").value || "23:00";
function showTimes(w) {
  const put = (id, v) => { const el = $("#" + id); if (el && v && document.activeElement !== el) el.value = v; };
  const saved = w?.settings?.start_at || null;
  if (saved !== showTimes.was) { showTimes.was = saved; put("rn-at-time", saved || store.get("uc-queue-at", null)); put("q-at-time", $("#rn-at-time").value); }   // the saved time when it changes; an edit in the field stays
  put("rn-finish-time", w?.settings?.finish_by); put("q-finish-time", w?.settings?.finish_by);
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
function jobLine(j, withLeft = true) {
  const bits = [];
  if (j.pausing) bits.push("pausing…");
  else if (j.epoch && j.epochs) bits.push(`epoch ${j.epoch} of ${j.epochs}`);
  else if (j.kind === "speech" && j.of) bits.push(`sentence ${(j.index ?? -1) + 1} of ${j.of}`);
  else if (j.message) bits.push(j.message);
  else if (j.state === "queued") bits.push("waiting for the GPU");
  const left = j.total_left ?? j.eta;
  if (withLeft && left != null && !j.pausing) bits.push(leftText(left));
  return bits.join(" · ");
}
const jobName = j => j.title || JOB_KIND[j.kind] || j.kind;
const WAIT_ORDER = ["auto", "cover", "speech", "install"];   // what the user is waiting for first, then voice work
const byWait = run => run.sort((a, b) => ((WAIT_ORDER.indexOf(a.kind) + 1) || 9) - ((WAIT_ORDER.indexOf(b.kind) + 1) || 9));

// ------------------------------------------------------------ the bar: any running job, live (jobs.js), plus Pause / Resume
W.work = null; W.watch = {};
function renderWorkBar(w) { W.work = w; renderBar(); if (runRoomOpen()) renderRunRoom(); }   // called from app.js pollStatus with /api/status's "work"
function renderBar() {
  const bar = $("#workbar"), w = W.work || {};
  if (typeof queueCount !== "function") return;   // shell.js (loaded last) isn't there yet: the job feed answered early
  queueCount(W.last ? W.last.queue.filter(i => ["queued", "paused", "running"].includes(i.state)).length
                    : (w.queued || 0) + (w.armed ? (w.running || []).length : 0), W.last ? W.last.armed : w.armed);
  const run = byWait(runningJobs());
  const main = run[0];
  let text = "", state = "";
  if (main) {
    text = [jobName(main), jobLine(main)].filter(Boolean).join(" · ");
    state = "run";
  } else if (w.held) {
    const res = (w.resume || []).map(r => r.label ?? r);   // entries are {id, kind, label, ...} (stop-now)
    text = "Nothing is running · voice work is paused" + (res.length ? ` · ${res.slice(0, 2).join(", ")}${res.length > 2 ? ` and ${res.length - 2} more` : ""}` : "") +
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
// every running job is watched, so its events move the bar as they come; the drawer and the Running room redraw too
onJobs(list => {
  for (const j of runningJobs()) if (!W.watch[j.id]) W.watch[j.id] = watchJob(j.id, ev => { if (ev.final) { W.watch[j.id]?.(); delete W.watch[j.id]; } });
  renderBar();
  if (!$("#q-drawer").hidden) renderRunning($("#qd-run"), { heading: "Running now" });
  if (runRoomOpen()) renderRunRoom();
});
// running jobs as rows (the drawer's "Running now")
function renderRunning(box, opts = {}) {
  if (!box) return;
  const list = byWait(runningJobs());
  box.hidden = !list.length;
  box.innerHTML = !list.length ? "" : (opts.heading ? `<h3 class="qd-h">${opts.heading}</h3>` : "") + list.map(j => {
    const owner = ownerOf(j.kind), f = jobProgress(j), note = jobLine(j);
    return `<div class="qd-job running" data-owner="${owner}">
      <div><span class="kind">${MOD_NAME[owner]} · ${esc(JOB_KIND[j.kind] || j.kind)}</span><b>${esc(jobName(j))}</b>${note ? `<small>${esc(note)}</small>` : ""}
        <div class="bar"><i style="width:${Math.round((f || 0) * 100)}%"></i></div></div>
      <div class="qside"><button class="linkbtn" type="button" data-goto="${owner}">Open</button></div></div>`;
  }).join("");
}
async function pauseWork() {
  try { await api("/api/work/pause", { json: {} }); $("#wb-text").textContent = "Pausing… (training saves its place first)"; }
  catch (e) { showError(e.message, "Couldn't pause"); }
  if (W.open) loadQueue();
}
$("#wb-pause").onclick = async () => { $("#wb-pause").disabled = true; await pauseWork(); $("#wb-pause").disabled = false; };
$("#wb-forget").onclick = () => forgetWork($("#wb-forget"));
async function forgetWork(b) {   // Dismiss, in the bar and in the Running room's card: two clicks
  if (!b.classList.contains("confirm")) { b.classList.add("confirm"); arm(b); b.textContent = "Forget it?"; setTimeout(() => { b.classList.remove("confirm"); disarm(b); b.textContent = "Dismiss"; }, 4000); return; }
  if (!armedFor(b)) return;   // a double click isn't a confirmation
  b.classList.remove("confirm"); disarm(b); b.textContent = "Dismiss";
  try { const r = await api("/api/work/forget", { json: {} }); renderWorkBar({ ...r, queued: r.queue.filter(i => ["queued", "paused"].includes(i.state)).length }); }
  catch (e) { showError(e.message, "Couldn't dismiss it"); }
  if (W.open) loadQueue();
}
async function resumeWork() {
  try {
    const r = await api("/api/work/resume", { json: {} });
    if (r.problems?.length) showError(r.problems.join("\n"), "Some work couldn't resume");
    renderWorkBar({ ...r, queued: r.queue.filter(i => ["queued", "paused"].includes(i.state)).length });
  } catch (e) { showError(e.message, "Couldn't resume"); }
  if (W.open) loadQueue();
}
$("#wb-resume").onclick = async () => { $("#wb-resume").disabled = true; await resumeWork(); $("#wb-resume").disabled = false; };

// ------------------------------------------------------------ SafeHouse's Running room: now, tonight's queue, recent
const runRoomOpen = () => !$("#view-library").hidden && !$("#room-running").hidden;
function loadRunRoom() { renderRunRoom(); refreshJobs(true); loadQueue(); }   // rooms.js, when the room opens
const ENDED = { done: ["ok", "done"], error: ["bad", "failed"], cancelled: ["warn", "cancelled"], paused: ["warn", "paused"], lost: ["bad", "stopped"] };
const hhmm = t => new Date(t * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
// the room redraws on every job event: a part is rewritten only when its HTML changed, so focus and clicks survive
const shown = new WeakMap();
const put = (el, html) => { if (shown.get(el) === html) return false; el.innerHTML = html; shown.set(el, html); return true; };
function renderRunRoom() {
  const w = W.last, work = W.work || {};
  // now: the job the user waits for first is the lit one; any others are rows under it
  const [main, ...rest] = byWait(runningJobs());
  let now = `<section class="panel rn-idle-panel"><div class="rn-lbl"><b>Now</b></div><p class="meta rn-idle">Nothing running right now. Covers, speech and voice work show here while they run.</p></section>`;
  if (main) {
    const owner = ownerOf(main.kind), f = jobProgress(main), left = main.total_left ?? main.eta, line = jobLine(main, false);
    now = `<div class="rn-now" data-owner="${owner}"><div class="who">${MOD_NAME[owner]} · ${esc(JOB_KIND[main.kind] || main.kind)}</div>
      <div class="rn-pl"><span><b>${esc(jobName(main))}</b>${line ? ` · ${esc(line)}` : ""}</span>${left != null && !main.pausing ? `<span class="meta">${esc(leftText(left))}</span>` : ""}</div>
      ${f != null ? `<div class="bar"><i style="width:${Math.round(f * 100)}%"></i></div>` : ""}
      <div class="row">${PAUSABLE_KINDS.has(main.kind) ? `<button class="btn" type="button" data-rn-pause>Pause</button>`
        : W.cancelAsk === main.id ? `<button class="btn confirm" type="button" data-jcancel="${esc(main.id)}">Cancel it?</button>` : `<button class="btn" type="button" data-jcancel="${esc(main.id)}">Cancel</button>`}
        <button class="linkbtn" type="button" data-goto="${owner}">Open in ${MOD_NAME[owner]}</button>
        <span class="meta mono" data-deep>job ${esc(main.id)} · started ${hhmm(main.created)}</span></div></div>` +
      (rest.length ? `<div class="rn-list">${rest.map(j => {
        const o = ownerOf(j.kind);
        return `<div class="rn-li"><span><b>${esc(jobName(j))}</b><small>${MOD_NAME[o]} · ${esc(jobLine(j) || JOB_KIND[j.kind] || j.kind)}</small></span>
          <span class="acts"><button class="linkbtn" type="button" data-goto="${o}">Open</button></span></div>`; }).join("")}</div>` : "");
  } else if (work.held) {
    const res = (work.resume || []).map(r => typeof r === "string" ? { label: r } : r);   // {id, kind, label, held_at, queue_as}
    // held work is NOT running: no bar, no running look, a plain "not running" label, and what to do with it
    const when = t => { if (!t) return ""; const [d, h] = String(t).split(" "); return d === new Date().toLocaleDateString("sv") ? `stopped at ${h}` : `stopped ${d} ${h}`; };
    now = `<div class="rn-now held" data-owner="doubleagent"><div class="who">Not running</div>
      <p><b>Nothing is running.</b> This work was stopped (you paused it, or UnderCover closed). Where it stopped is saved.</p>
      ${res.length ? `<div class="rn-list">${res.map(r => `<div class="rn-li"><span><b>${esc(r.label)}</b>${r.held_at ? `<small>${esc(when(r.held_at))}</small>` : ""}</span>
        <span class="acts">${r.queue_as && r.id ? `<button class="linkbtn" type="button" data-rn-toq="${esc(r.id)}">Train tonight instead</button>` : ""}</span></div>`).join("")}</div>` : ""}
      <div class="row"><button class="btn primary" type="button" data-rn-resume>Resume now</button>
        ${res.length ? `<button class="linkbtn" type="button" data-rn-forget>Dismiss</button>` : ""}</div></div>`;
  }
  if (put($("#rn-now"), now)) {
    $$("[data-rn-pause]", $("#rn-now")).forEach(b => b.onclick = async () => { b.disabled = true; await pauseWork(); b.disabled = false; });
    $$("[data-rn-resume]", $("#rn-now")).forEach(b => b.onclick = async () => { b.disabled = true; await resumeWork(); b.disabled = false; });
    $$("[data-rn-forget]", $("#rn-now")).forEach(b => b.onclick = () => forgetWork(b));   // two clicks, then forgotten
    $$("[data-rn-toq]", $("#rn-now")).forEach(b => b.onclick = async () => {   // this held work moves to tonight's queue
      b.disabled = true;
      try {
        const r = await api("/api/work/to-queue", { json: { id: b.dataset.rnToq } });
        W.last = r; renderWorkBar({ ...r, queued: r.queue.filter(i => ["queued", "paused"].includes(i.state)).length });
        showError("It's in tonight's queue now. It runs while the PC is free.", "Moved", null, true);
        refreshJobs(true); renderQueue();
      } catch (e) { b.disabled = false; showError(e.message, "Couldn't move it to tonight"); }
    });
    $$("[data-jcancel]", $("#rn-now")).forEach(b => b.onclick = async () => {   // two clicks; the first is remembered across redraws
      const id = b.dataset.jcancel;
      clearTimeout(W.cancelTimer);
      if (W.cancelAsk !== id) {
        W.cancelAsk = id; W.cancelAskAt = Date.now(); W.cancelTimer = setTimeout(() => { W.cancelAsk = null; if (runRoomOpen()) renderRunRoom(); }, 4000);
        return renderRunRoom();
      }
      if (Date.now() - (W.cancelAskAt || 0) < CONFIRM_MS) { W.cancelTimer = setTimeout(() => { W.cancelAsk = null; if (runRoomOpen()) renderRunRoom(); }, 4000); return; }   // a double click isn't a confirmation
      W.cancelAsk = null; b.disabled = true; b.textContent = "Cancelling…";
      try { await api(`/api/jobs/${id}/cancel`, { method: "POST" }); } catch (e) { showError(e.message, "Couldn't cancel that"); }
      refreshJobs(true);
    });
  }

  // tonight: the overnight queue (its full editor stays on DoubleAgent's page)
  const left = w ? w.queue.filter(i => ["queued", "paused", "running"].includes(i.state)) : [];
  const at = !!(w?.armed && w.settings.start_at), going = !!(w?.armed && !w.settings.start_at);
  $("#rn-total").textContent = left.length ? `About ${fmtMin(w.total_min)} · done by about ${clock(w.done_by)}` : "";
  $("#rn-when").hidden = $("#rn-opts").hidden = !left.length;
  showTimes(w);
  const same = at && startTime() === w.settings.start_at;   // the field still says what's saved; edited, the button offers the new time
  $("#rn-at").textContent = same ? `Starts at ${clock(w.settings.start_at)}` : `Start at ${clock(startTime())}`;
  $("#rn-at").setAttribute("aria-pressed", same);
  $("#rn-go").setAttribute("aria-pressed", going);
  $("#rn-stop").hidden = !w?.armed;
  const list = !w ? `<p class="meta">Loading…</p>` : left.length ? left.map(i => {
    const run = i.state === "running";
    const note = run ? "running now" : i.state === "paused" ? "paused · Resume continues it" : i.note || "";
    return `<div class="rn-li"><span><b>${jobWhat(i)}</b><small>${jobKind(i)}${note ? ` · ${esc(note)}` : ""}</small></span>
      <span class="acts"><span class="when">~${fmtMin((i.estimate || {}).total)}</span>${run ? "" : `<button class="linkbtn" type="button" data-rn-rm="${esc(i.id)}">Remove</button>`}</span></div>`;
  }).join("") : `<p class="meta">Nothing queued for tonight. Add a voice to improve, or train a new one, and it runs while you sleep.</p>`;
  if (put($("#rn-list"), list)) $$("#rn-list [data-rn-rm]").forEach(b => b.onclick = async () => {
    try { W.last = await api("/api/work/remove", { json: { id: b.dataset.rnRm } }); renderQueue(); }
    catch (e) { showError(e.message, "Couldn't remove that"); }
  });
  if (w) { $("#rn-finish").checked = !!w.settings.finish_by; $("#rn-awake").checked = w.settings.keep_awake !== false; }

  // recent: the last five finished, and how they ended (from each job's final event)
  const ended = JF.list.filter(j => !isRunning(j) && j.kind !== "presep").slice(0, 5);
  put($("#rn-recent"), ended.length ? ended.map(j => {
    const owner = ownerOf(j.kind), st = j.final?.state || j.state, [cls, word] = ENDED[st] || ["warn", st];
    const how = st === "done" ? (j.ended ? `took ${fmtSecs(j.ended - j.created)}` : "") : j.final?.message || "";
    return `<div class="rn-li"><span><b>${esc(jobName(j))}</b><small>${MOD_NAME[owner]} · ${esc(JOB_KIND[j.kind] || j.kind)}${how ? ` · ${esc(how)}` : ""}</small></span>
      <span class="acts">${j.ended ? `<span class="when">${hhmm(j.ended)}</span>` : ""}<span class="dotw ${cls}">${esc(word)}</span>${j.retry ? `<button class="linkbtn" type="button" data-retry="${esc(j.id)}">Retry</button>` : ""}<button class="linkbtn" type="button" data-goto="${owner}">Open</button></span></div>`;
  }).join("") : `<p class="meta">Nothing has finished in the last hour.</p>`);
}
// Retry a failed cover / speech / one-shot / voice install (phase 10): the server starts it again from the same request
$("#rn-recent").addEventListener("click", async e => {
  const b = e.target.closest("[data-retry]"); if (!b) return;
  b.disabled = true;
  try { await api(`/api/jobs/${b.dataset.retry}/retry`, { method: "POST" }); refreshJobs(); }
  catch (err) { b.disabled = false; showError(err.message, "Couldn't start it again"); }
});
const startWork = async body => {
  try { W.last = await api("/api/work/start", { json: body }); renderQueue(); pollStatusSoon(); }
  catch (e) { showError(e.message, "Couldn't change the start"); }
};
$("#rn-at").onclick = () => startWork({ at: startTime() });
$("#rn-at-time").onchange = e => { store.set("uc-queue-at", e.target.value || null); $("#q-at-time").value = e.target.value; renderQueue(); };
$("#rn-go").onclick = () => startWork({});
$("#rn-stop").onclick = () => startWork({ stop: true });
const workSettings = async body => {
  try { W.last = await api("/api/work/settings", { json: body }); } catch (e) { showError(e.message, "Couldn't change that"); }
  renderQueue();
};
$("#rn-finish").onchange = e => workSettings({ finish_by: e.target.checked ? ($("#rn-finish-time").value || "07:00") : null });
$("#rn-finish-time").onchange = e => { if ($("#rn-finish").checked && e.target.value) workSettings({ finish_by: e.target.value }); };
$("#rn-awake").onchange = e => workSettings({ keep_awake: e.target.checked });

// ------------------------------------------------------------ the overnight queue (DoubleAgent's page + the drawer)
async function loadQueue() {
  try { W.last = await api("/api/work"); } catch (e) { return; }
  renderQueue();
}
const jobWhat = i => ({ improve: `Improve ${esc(i.display || i.target)}`, cadence: `Cadence for ${esc(i.display || i.target)}`,
                        newcad: `New voice with cadence: ${esc(i.target)}`,
                        project: `Training ${esc(i.display || i.target)} (your choices)` })[i.kind] || `Train a new voice: ${esc(i.target)}`;
const jobKind = i => ({ improve: "voice · improve", new: "voice · new", cadence: "cadence", newcad: "voice + cadence", project: "voice · your choices" })[i.kind] || esc(i.kind);
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
  if (runRoomOpen()) renderRunRoom();
  queueCount(w.queue.filter(i => ["queued", "paused", "running"].includes(i.state)).length, w.armed);
  const left = w.queue.filter(i => ["queued", "paused", "running"].includes(i.state));
  $("#q-total").innerHTML = left.length
    ? `<b class="mono">≈ ${fmtMin(w.total_min)}</b> <span class="meta">· done by about <span class="mono">${clock(w.done_by)}</span>${w.armed && w.settings.start_at ? ` if it starts at ${clock(w.settings.start_at)}` : ""}</span>`
    : `<span class="meta">Nothing queued. Add a voice to improve, or a person to train, and let it run overnight.</span>`;
  const st = $("#q-start"), at = $("#q-at");
  st.hidden = at.hidden = !left.length;
  $("#q-stop").hidden = !w.armed || !left.length;
  st.textContent = w.armed && !w.settings.start_at ? "Running" : "Start now";
  st.disabled = w.armed && !w.settings.start_at;
  at.textContent = w.armed && w.settings.start_at && startTime() === w.settings.start_at ? `Starts at ${clock(w.settings.start_at)}` : `Start at ${clock(startTime())}`;
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
// The report gathers everything since the last Dismiss, so it's "While you were out", with its real range in days ("yesterday 15:14 to today 13:20").
const reportDay = d => { const day = x => new Date(x.getFullYear(), x.getMonth(), x.getDate()), n = Math.round((day(new Date()) - day(d)) / 864e5);
  return n < 1 ? "today" : n < 2 ? "yesterday" : n < 7 ? d.toLocaleDateString(undefined, { weekday: "long" }) : d.toLocaleDateString(undefined, { day: "numeric", month: "short" }); };
function reportRange(a, z) {
  const at = s => s ? new Date(String(s).replace(" ", "T")) : null, s = at(a), e = at(z);
  if (!s || isNaN(s)) return "";
  const hm = d => d.toTimeString().slice(0, 5);
  if (!e || isNaN(e)) return `from ${reportDay(s)} ${hm(s)}`;
  return reportDay(s) === reportDay(e) ? `${reportDay(s)} ${hm(s)} to ${hm(e)}` : `${reportDay(s)} ${hm(s)} to ${reportDay(e)} ${hm(e)}`;
}
// What a result says in words. The server's `outcome` when it gives one (better | same | not_better | waiting | new); until it does, from what the result carries:
// a retrained voice waiting for the ear (pending) is not "kept the old", and a brand-new voice (new, new voice + cadence) had no old one to keep.
const REPORT_WORDS = { better: "better", same: "about the same", not_better: "not better: yours is kept", waiting: "waiting for your ear", new: "new voice" };
function reportOutcome(x) {
  if (x.outcome) return x.outcome;
  if (x.state !== "done") return "";
  if (["new", "newcad"].includes(x.kind)) return "new";
  if (x.pending) return "waiting";
  if (x.replaced) return "better";
  return x.before != null || x.after != null ? "not_better" : "";
}
const REPORT_PLAIN = { waiting: "Listen to both on its card and keep the one you like.", not_better: "The retrained one didn't beat it, so your voice stays." };
function renderReport(r) {
  const box = $("#q-report");
  box.hidden = !r || !r.results?.length;
  if (box.hidden) return;
  $("#q-report-when").textContent = `${reportRange(r.started, r.finished)} · ${r.results.length} job${r.results.length > 1 ? "s" : ""}`;
  $("#q-report-list").innerHTML = r.results.map(x => {
    const oc = reportOutcome(x);
    const what = x.kind === "improve" ? (x.replaced ? "improved" : x.pending ? "retrained" : "tried") : x.kind === "cadence" ? (x.replaced ? "cadence" : "cadence tried")
      : x.kind === "newcad" ? "new voice + cadence" : "new voice";
    const words = x.before_word && x.after_word && x.before_word !== x.after_word ? `${likeWord(x.before_word)} → ${likeWord(x.after_word)}` : likeWord(x.after_word || x.before_word);
    const numbers = x.before != null || x.after != null ? `${x.before != null ? x.before.toFixed(2) + " → " : ""}${x.after != null ? x.after.toFixed(2) : "?"}` : "";
    const cad = x.kind === "cadence" ? x : x.cadence;   // cadence numbers: Deep Cover only
    const deep = cad && cad.likeness != null ? `<p class="meta" data-deep>cadence: likeness ${cad.likeness} · gap ${cad.cadence_gap} · WER ${cad.wer} · from ${cad.data_minutes} min</p>` : "";
    const raw = x.note || "";
    const hasNum = /\d\.\d{2,}/.test(raw);   // the server's note quotes the scores: Deep Cover; basic says it plainly
    const plain = hasNum ? REPORT_PLAIN[oc] || "" : raw;
    const up = oc === "better" || oc === "new" || oc === "waiting";
    return `<div class="qres"><div><b>${esc(x.voice ? voiceDisplay(x.voice) : x.display || x.target)}</b> ${what}${plain ? `<p class="meta">${esc(plain)}</p>` : ""}${hasNum ? `<p class="meta" data-deep>${esc(raw)}</p>` : ""}${deep}</div>
      <div class="qres-r"><span class="${up ? "up" : "same"}">${oc ? esc(REPORT_WORDS[oc] || oc) : esc(x.state)}</span>${words ? `<span class="meta">${words}</span>` : ""}${numbers ? `<span class="mono meta" data-deep>${esc(numbers)}</span>` : ""}</div></div>`;
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
        <span class="qchips">${facts.map(f => `<span class="qchip" data-deep>${esc(f)}</span>`).join("")}${c.queued ? `<span class="qchip ok">in the queue</span>` : ""}</span>
        <span class="meta">${esc(c.note)}</span></span></label>`;
  }).join("") : `<p class="meta">No voices yet. Use "New voices" to train one.</p>`;
  $$("#q-cands input[type=checkbox]").forEach(cb => cb.onchange = () => { cb.checked ? PICK.sel.add(cb.dataset.v) : PICK.sel.delete(cb.dataset.v); pickSummary(); });
  $("#q-other").hidden = !off.length;
  $("#q-cands-off").textContent = off.map(c => voiceDisplay(c.voice)).join(", ") + ". These were downloaded and DoubleAgent doesn't know whose voices they are. To train your own, add the person under New voices.";
  pickSummary();
  renderCadPicker();
  renderVoiceCards(PICK.cands);   // doubleagent.js
  if (typeof loadImproved === "function") loadImproved();   // a retrained voice waiting beside a current one
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
    catch (e) { showError(e.message, `Couldn't add ${voiceDisplay(c.voice)}`); }
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
      try { W.last = await api("/api/work/add", { json: { kind: "new", target: b.dataset.name } }); b.textContent = "In the queue"; renderQueue(); workMoved(W.last); }
      catch (e) { b.disabled = false; showError(e.message, "Couldn't add that"); }
    });
  }, 450);
});
$("#q-start").onclick = async () => { W.last = await api("/api/work/start", { json: {} }); renderQueue(); pollStatusSoon(); };
$("#q-at").onclick = async () => { W.last = await api("/api/work/start", { json: { at: startTime() } }); renderQueue(); pollStatusSoon(); };
$("#q-at-time").onchange = e => { store.set("uc-queue-at", e.target.value || null); $("#rn-at-time").value = e.target.value; renderQueue(); };
$("#q-stop").onclick = async () => { W.last = await api("/api/work/start", { json: { stop: true } }); renderQueue(); pollStatusSoon(); };
$("#q-finish").onchange = async e => { W.last = await api("/api/work/settings", { json: { finish_by: e.target.checked ? ($("#q-finish-time").value || "07:00") : null } }); renderQueue(); };
$("#q-finish-time").onchange = async e => { if ($("#q-finish").checked && e.target.value) { W.last = await api("/api/work/settings", { json: { finish_by: e.target.value } }); renderQueue(); } };
$("#q-awake").onchange = async e => { W.last = await api("/api/work/settings", { json: { keep_awake: e.target.checked } }); renderQueue(); };
$("#q-dismiss").onclick = async () => { W.last = await api("/api/work/dismiss", { json: {} }); renderQueue(); };
function pollStatusSoon() { setTimeout(() => api("/api/status").then(s => renderWorkBar(s.work)).catch(() => {}), 300); }

// refresh the queue while DoubleAgent's page, the drawer or the Running room is open
setInterval(() => { W.open = !$("#view-train").hidden || !$("#q-drawer").hidden || runRoomOpen(); if (W.open) loadQueue(); }, 5000);
document.addEventListener("uc:module", e => { if (e.detail === "doubleagent") { loadQueue(); loadCandidates(); } });

// a job finished somewhere (this page, another tab, the queue, the API): refresh what it may have changed
// /api/work/add answers `moved` when that voice was training live right now: it is paused and put in tonight's queue (it goes on from where it stopped).
// Said instead of "Added to tonight"; returns true when it was.
function workMoved(r) {
  if (!r || !r.moved) return false;
  showError(r.moved, "Moved to tonight", null, true);
  return true;
}
document.addEventListener("uc:jobdone", e => {
  const k = e.detail.kind;
  if (!["train", "train-auto", "train-export", "train-prepare", "train-transcribe", "night", "install", "auto"].includes(k)) return;
  if (typeof loadVoices === "function" && ["train", "train-auto", "train-export", "night", "install", "auto"].includes(k)) loadVoices();
  loadQueue(); loadCandidates();
  if (typeof seLoad === "function" && !$("#view-speakeasy").hidden) seLoad();
  if (typeof refresh === "function" && typeof T !== "undefined" && T.proj) refresh();
});
