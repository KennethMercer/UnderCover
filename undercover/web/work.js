"use strict";
// Suite-level work: the Pause / Resume bar under the top bar on every module, the overnight queue (its editor on
// MouthPiece's page, and the drawer from the Queue button). Server side: undercover/core/worklist.py. The bar is fed by
// /api/status (polled by app.js); the queue by /api/work.

const W = { last: null, open: false };

// which module owns a job: the one that started it (colour + label in the bar and the drawer)
const OWNER = { auto: "frontman" };   // the one-shot; voice work ("train", "train-*", queue items) is MouthPiece's
const MOD_NAME = { frontman: "FrontMan", mouthpiece: "MouthPiece", speakeasy: "SpeakEasy", safehouse: "SafeHouse" };
const ownerOf = kind => OWNER[kind] || "mouthpiece";
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

// ------------------------------------------------------------ the bar (called from app.js pollStatus)
function renderWorkBar(w) {
  const bar = $("#workbar");
  if (!w) { bar.hidden = true; return; }
  const run = (w.running || [])[0];
  // waiting + running queue jobs (the queue only runs while armed); /api/work, once loaded, knows exactly
  queueCount(W.last ? W.last.queue.filter(i => ["queued", "paused", "running"].includes(i.state)).length
                    : (w.queued || 0) + (w.armed ? (w.running || []).length : 0));
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
  const owner = run ? ownerOf(run.kind) : "mouthpiece";
  bar.dataset.owner = owner;
  $("#wb-who").innerHTML = `${glyph(owner)}${MOD_NAME[owner]}`;
  $("#wb-text").textContent = text;
  const frac = state === "run" && run.fraction != null ? run.fraction : null;
  $("#wb-bar").hidden = frac == null;
  if (frac != null) $("#wb-bar i").style.width = Math.round(frac * 100) + "%";
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

// ------------------------------------------------------------ the overnight queue (MouthPiece's page + the drawer)
async function loadQueue() {
  try { W.last = await api("/api/work"); } catch (e) { return; }
  renderQueue();
}
const jobWhat = i => i.kind === "improve" ? `Improve ${esc(i.target)}` : `Train a new voice: ${esc(i.target)}`;
const jobKind = i => i.kind === "improve" ? "voice · improve" : i.kind === "new" ? "voice · new" : esc(i.kind);
// the drawer: a short list, labelled by module and kind; the full editor stays on MouthPiece's page
function renderDrawer() {
  const w = W.last;
  if (!w) { $("#qd-list").innerHTML = `<p class="meta">Loading…</p>`; return; }
  const left = w.queue.filter(i => ["queued", "paused", "running"].includes(i.state));
  $("#qd-total").innerHTML = left.length
    ? `<b class="mono">≈ ${fmtMin(w.total_min)}</b> of work · done by about <span class="mono">${clock(w.done_by)}</span>` +
      (w.armed ? (w.settings.start_at ? ` · starts at ${clock(w.settings.start_at)}` : " · running") : " · not started yet (start it in MouthPiece)")
    : "Nothing queued. Add a voice to improve, or a person to train, and let it run overnight.";
  $("#qd-list").innerHTML = w.queue.map(i => {
    const run = i.state === "running", done = ["done", "failed", "skipped"].includes(i.state), owner = ownerOf(i.kind);
    const live = run ? (w.running.find(r => r.job === i.job) || {}) : {};
    const note = done ? `${i.state} · ${fmtMin(i.minutes)}${i.result?.note ? " · " + i.result.note : ""}` : run ? live.message || "running"
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
      : [["prepare", "prepare"], ["train", "train"], ["compare", i.kind === "improve" ? "compare" : "check"]]
          .map(([k, l]) => `<span class="qchip">${l} ~${fmtMin(e[k])}</span>`).join("");
    const note = done ? esc(i.result?.note || "") : run ? esc(live.message || "running") : i.state === "paused" ? "paused · Resume (top of the page) continues it" : esc(i.note || "");
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
// ------------------------------------------------------------ the picker: improve my voices / new voices
const PICK = { sel: new Set(), cands: [], timer: null, added: new Set() };
$("#q-mode").addEventListener("click", e => {
  const b = e.target.closest("button"); if (!b) return;
  $$("#q-mode button").forEach(x => x.setAttribute("aria-pressed", x === b));
  $("#q-improve").hidden = b.dataset.v !== "improve";
  $("#q-new").hidden = b.dataset.v !== "new";
  if (b.dataset.v === "new") $("#q-name").focus();
});
async function loadCandidates() {
  try { PICK.cands = await api("/api/work/candidates"); } catch (e) { return; }
  const can = PICK.cands.filter(c => c.can), off = PICK.cands.filter(c => !c.can);
  [...PICK.sel].forEach(k => { if (!can.some(c => c.voice === k && !c.queued)) PICK.sel.delete(k); });
  $("#q-cands").innerHTML = can.length ? can.map(c => {
    const facts = [c.minutes ? `${Math.round(c.minutes)} min of ${c.singing ? "singing" : c.singing === false ? "speech" : "voice"}` : c.trained ? "" : "downloaded",
                   c.likeness != null ? `likeness ${(+c.likeness).toFixed(2)}` : "", c.estimate ? `≈ ${fmtMin(c.estimate)}` : ""].filter(Boolean);
    return `<label class="vcard ${c.queued ? "queued" : ""}">
      <input type="checkbox" data-v="${esc(c.voice)}" ${c.queued ? "disabled" : PICK.sel.has(c.voice) ? "checked" : ""}>
      <span><b>${esc(c.voice)}</b>${c.person && c.person.toLowerCase() !== c.voice.replace(/-uc$/, "").replace(/-/g, " ") ? ` <span class="meta">${esc(c.person)}</span>` : ""}
        <span class="qchips">${facts.map(f => `<span class="qchip">${esc(f)}</span>`).join("")}${c.queued ? `<span class="qchip ok">in the queue</span>` : ""}</span>
        <span class="meta">${esc(c.note)}</span></span></label>`;
  }).join("") : `<p class="meta">No voices yet. Use "New voices" to train one.</p>`;
  $$("#q-cands input[type=checkbox]").forEach(cb => cb.onchange = () => { cb.checked ? PICK.sel.add(cb.dataset.v) : PICK.sel.delete(cb.dataset.v); pickSummary(); });
  $("#q-other").hidden = !off.length;
  $("#q-cands-off").textContent = off.map(c => c.voice).join(", ") + ". These were downloaded and MouthPiece doesn't know whose voices they are. To train your own, add the person under New voices.";
  pickSummary();
  renderVoiceCards(PICK.cands);   // mouthpiece.js
}
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

// refresh the queue while MouthPiece's page or the drawer is open
setInterval(() => { W.open = !$("#view-train").hidden || !$("#q-drawer").hidden; if (W.open) loadQueue(); }, 5000);
document.addEventListener("uc:module", e => { if (e.detail === "mouthpiece") { loadQueue(); loadCandidates(); } });
