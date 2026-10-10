"use strict";
// Background voice work: the Pause / Resume bar at the top of every tab, and the overnight queue (Train a voice).
// Server side: undercover/worklist.py. The bar is fed by /api/status (polled by app.js); the queue by /api/work.

const W = { last: null, open: false };

function fmtMin(m) {
  m = Math.round(m || 0);
  return m >= 60 ? `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, "0")} min` : `${m} min`;
}
function clock(hhmm) {
  if (!hhmm) return "";
  const [h, m] = hhmm.split(":").map(Number);
  return `${(h % 12) || 12}:${String(m).padStart(2, "0")} ${h < 12 ? "am" : "pm"}`;
}

// ------------------------------------------------------------ the bar (called from app.js pollStatus)
function renderWorkBar(w) {
  const bar = $("#workbar");
  if (!w) { bar.hidden = true; return; }
  const run = (w.running || [])[0];
  let text = "", state = "";
  if (w.held) {
    const res = w.resume || [];
    text = "Voice work is paused" + (res.length ? ` · ${res.slice(0, 2).join(", ")}${res.length > 2 ? ` and ${res.length - 2} more` : ""}` : "") +
           (w.queued ? ` · ${w.queued} in the overnight queue` : "");
    state = "held";
  } else if (run) {
    const bits = [run.label];
    if (run.pausing) bits.push("pausing…");
    else if (run.epoch && run.epochs) bits.push(`epoch ${run.epoch} of ${run.epochs}`);
    else if (run.message) bits.push(run.message);
    if (run.eta && !run.pausing) bits.push(`~${fmtSecs(run.eta)} left`);
    text = bits.join(" · ");
    state = "run";
  } else if (w.armed && w.queued) {
    text = `Overnight queue: ${w.queued} job${w.queued > 1 ? "s" : ""}` + (w.start_at ? `, starts at ${clock(w.start_at)}` : ", starting when the GPU is free");
    state = "armed";
  }
  bar.hidden = !text;
  if (!text) return;
  bar.dataset.state = state;
  $("#wb-text").textContent = text;
  $("#wb-pause").hidden = state === "held";
  $("#wb-resume").hidden = state !== "held";
  $("#chip-train").hidden = true;   // the bar says it all
}
$("#wb-pause").onclick = async () => {
  $("#wb-pause").disabled = true;
  try { await api("/api/work/pause", { json: {} }); $("#wb-text").textContent = "Pausing… (training saves its place first)"; }
  catch (e) { showError(e.message, "Couldn't pause"); }
  $("#wb-pause").disabled = false;
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
$("#wb-open").onclick = () => { showTab("train"); $("#q-panel").scrollIntoView({ behavior: "smooth", block: "start" }); };

// ------------------------------------------------------------ the overnight queue (Train a voice tab)
async function loadQueue() {
  try { W.last = await api("/api/work"); } catch (e) { return; }
  renderQueue();
}
function renderQueue() {
  const w = W.last; if (!w) return;
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
    const what = i.kind === "improve" ? `Improve ${esc(i.target)}` : `Train a new voice: ${esc(i.target)}`;
    const live = run ? (w.running.find(r => r.job === i.job) || {}) : {};
    const chips = done ? `<span class="qchip ${i.state === "done" ? "ok" : "bad"}">${esc(i.state)} · ${fmtMin(i.minutes)}</span>`
      : [["prepare", "prepare"], ["train", "train"], ["compare", i.kind === "improve" ? "compare" : "check"]]
          .map(([k, l]) => `<span class="qchip">${l} ~${fmtMin(e[k])}</span>`).join("");
    const note = done ? esc(i.result?.note || "") : run ? esc(live.message || "running") : i.state === "paused" ? "paused · Resume (top of the page) continues it" : "";
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
    return `<div class="qres"><div><b>${esc(x.voice || x.target)}</b> ${x.kind === "improve" ? (x.replaced ? "improved" : "tried") : "new voice"}<p class="meta">${esc(x.note || "")}</p></div>
      <div class="mono ${x.replaced || (x.kind === "new" && x.state === "done") ? "up" : "same"}">${esc(score)}</div></div>`;
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
$("#q-add").onclick = async () => {
  const target = $("#q-target").value.trim();
  try { W.last = await api("/api/work/add", { json: { kind: $("#q-kind").value, target } }); $("#q-target").value = ""; renderQueue(); }
  catch (e) { showError(e.message, "Couldn't add that"); }
};
$("#q-target").addEventListener("keydown", e => { if (e.key === "Enter") $("#q-add").click(); });
$("#q-kind").onchange = () => { $("#q-target").placeholder = $("#q-kind").value === "improve" ? "A voice to improve" : "A person's name"; fillVoices(); };
function fillVoices() {
  const dl = $("#q-voices");
  dl.innerHTML = $("#q-kind").value === "improve" ? (S.voices || []).map(v => `<option value="${esc(v.name)}">`).join("") : "";
}
$("#q-start").onclick = async () => { W.last = await api("/api/work/start", { json: {} }); renderQueue(); pollStatusSoon(); };
$("#q-at").onclick = async () => { W.last = await api("/api/work/start", { json: { at: $("#q-at-time").value } }); renderQueue(); pollStatusSoon(); };
$("#q-at-time").onchange = () => renderQueue();
$("#q-stop").onclick = async () => { W.last = await api("/api/work/start", { json: { stop: true } }); renderQueue(); pollStatusSoon(); };
$("#q-finish").onchange = async e => { W.last = await api("/api/work/settings", { json: { finish_by: e.target.checked ? "07:00" : null } }); renderQueue(); };
$("#q-awake").onchange = async e => { W.last = await api("/api/work/settings", { json: { keep_awake: e.target.checked } }); renderQueue(); };
$("#q-dismiss").onclick = async () => { W.last = await api("/api/work/dismiss", { json: {} }); renderQueue(); };
function pollStatusSoon() { setTimeout(() => api("/api/status").then(s => renderWorkBar(s.work)).catch(() => {}), 300); }

// refresh the queue while the Train tab is visible
setInterval(() => { W.open = !$("#view-train").hidden; if (W.open) loadQueue(); }, 5000);
document.addEventListener("click", e => { if (e.target.closest("[data-tab=train]")) setTimeout(() => { fillVoices(); loadQueue(); }, 50); });
