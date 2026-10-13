// Make smaller (phase 24; the backend is safehouse/smaller.py): a section of SafeHouse's Storage room. Per item, never
// automatic: a voice's big search file (its index) shrinks to a fraction and is read both ways to check it sounds the
// same; a training project's WAV clips become FLAC (each checked identical). The originals wait aside until Keep (they
// go) or Undo (they come back). Numbers are Deep Cover; the actions are PC-only. Uses helpers from app.js ($, $$, esc,
// api, showError, voiceDisplay, VOL), library.js (gb, confirmClick) and jobs.js (watchJob, runningJobs, onJobs).
const SM = { o: null, all: false, jobs: {}, audio: null };
const SM_MIN = 20e6;   // below this, an item isn't worth a row (it still shows while it waits aside)
const smKey = (kind, name) => `${kind}:${name}`;

async function smallerLoad() {
  try { SM.o = await api("/api/library/smaller"); } catch (e) { SM.o = null; $("#smaller").hidden = true; return; }
  $("#smaller").hidden = false;
  renderSmaller();
}
function smItems() {
  const o = SM.o || {};
  const v = (o.voices || []).map(x => ({ ...x, kind: "voice", label: voiceDisplay(x.name), what: "voice" }));
  const p = (o.projects || []).map(x => ({ ...x, kind: "project", label: x.person ? `${x.person}'s training project` : x.name, what: "project" }));
  return [...v, ...p].filter(x => x.pending || x.saving >= SM_MIN || SM.jobs[smKey(x.kind, x.name)]).sort((a, b) => (b.pending - a.pending) || (b.saving - a.saving));
}
function renderSmaller() {
  const box = $("#sm-body"), items = smItems(), o = SM.o || {};
  $("#sm-say").textContent = o.saving >= SM_MIN ? `could save about ${gb(o.saving)}` : "";
  if (!items.length) { box.innerHTML = `<p class="meta">Nothing big enough to be worth it right now.</p>`; return; }
  const shown = SM.all ? items : items.slice(0, 5);
  box.innerHTML = `<p class="vt-lead">The voices you trained keep a big search file, and training projects keep their clips uncompressed. Both shrink a lot. The originals wait aside until you choose Keep, so nothing goes until you're happy with it.</p>
    ${shown.map(smRow).join("")}
    ${items.length > 5 ? `<button class="linkbtn" type="button" id="sm-all">${SM.all ? "Show fewer" : `Show all ${items.length}`}</button>` : ""}`;
  smWire();
}
function smRow(x) {
  const k = smKey(x.kind, x.name), job = SM.jobs[k], r = x.result || {};
  let state, acts = "", deep = "";
  if (job) {
    state = `<span class="st now">${esc(job.msg || "making it smaller…")}</span>`;
  } else if (x.pending) {
    const saved = x.kind === "voice" ? (r.bytes_before || 0) - (r.bytes_after || 0) : r.saving || 0;
    state = x.kind === "voice"
      ? (r.same === false ? `<span class="st warn">came out a little different</span>` : `<span class="st ok">sounds the same ✓</span>`) + `<span class="meta">saves ${gb(saved)}</span>`
      : `<span class="st ok">every clip checked ✓</span><span class="meta">saves ${gb(saved)}</span>`;
    const hear = r.full_url || r.small_url ? `<span class="sm-hear"><span class="meta">Hear it:</span>${r.full_url ? `<button class="linkbtn" type="button" data-sm-play="${esc(r.full_url)}">Before</button>` : ""}${r.small_url ? `<button class="linkbtn" type="button" data-sm-play="${esc(r.small_url)}">After</button>` : ""}</span>` : "";   // a reading cleaned away: its button goes
    acts = `${hear}<button class="btn${r.same === false ? "" : " primary"}" type="button" data-sm-keep="${esc(k)}" data-pc>Keep it smaller</button><button class="${r.same === false ? "btn primary" : "linkbtn"}" type="button" data-sm-undo="${esc(k)}" data-pc>Undo</button>`;
    deep = x.kind === "voice" ? `${r.vectors_before ?? "?"} → ${r.vectors_after ?? "?"} vectors · alike ${r.alike ?? "?"}${r.likeness_full != null ? ` · likeness ${r.likeness_full} → ${r.likeness_small}` : ""} · ${gb(r.bytes_before || 0)} → ${gb(r.bytes_after || 0)}`
      : `${r.clips ?? "?"} clips · ${gb(r.clips_bytes_before || 0)} → ${gb(r.clips_bytes_after || 0)}${r.data_bytes ? ` · prepared data ${gb(r.data_bytes)} aside` : ""}`;
  } else {
    state = `<span class="meta">could save about ${gb(x.saving)}</span>`;
    acts = `<button class="btn" type="button" data-sm-start="${esc(k)}" data-pc>Make smaller</button>`;
    deep = x.kind === "voice" ? `index ${gb(x.index_bytes || 0)} · ${x.vectors ?? "?"} vectors` : `clips ${gb(x.clips_wav_bytes || 0)} as WAV${x.data_bytes ? ` · prepared data ${gb(x.data_bytes)}` : ""}`;
  }
  return `<div class="vt-row sm-row"><span><b>${esc(x.label)}</b><small class="meta">${x.kind === "voice" ? "voice" : "training project"}</small><small class="mono" data-deep>${esc(deep)}</small></span>
    <span class="row">${state}${acts}</span></div>`;
}
function smWire() {
  const parse = k => { const [kind, ...n] = k.split(":"); return { kind, name: n.join(":") }; };
  $("#sm-all")?.addEventListener("click", () => { SM.all = !SM.all; renderSmaller(); });
  $$("[data-sm-start]").forEach(b => b.onclick = async () => {
    const it = parse(b.dataset.smStart); b.disabled = true;
    try { const { job } = await api("/api/library/smaller/start", { json: it }); smFollow(job, it); }
    catch (e) { b.disabled = false; showError(e.message, "Couldn't make it smaller"); }
  });
  $$("[data-sm-undo]").forEach(b => b.onclick = () => smAct("undo", parse(b.dataset.smUndo), b));
  // Keep deletes the originals: two clicks
  $$("[data-sm-keep]").forEach(b => confirmClick(b, "Delete the originals?", () => smAct("keep", parse(b.dataset.smKeep), b)));
  $$("[data-sm-play]").forEach(b => b.onclick = () => {
    const same = SM.audio && SM.audio.src.endsWith(b.dataset.smPlay) && !SM.audio.paused;
    SM.audio?.pause();
    if (same) return;
    SM.audio = new Audio(b.dataset.smPlay);
    SM.audio.volume = VOL.muted ? 0 : VOL.level; SM.audio.muted = VOL.muted;   // the page's own volume
    SM.audio.play().catch(() => {});
  });
}
async function smAct(how, it, b) {
  b.disabled = true; SM.audio?.pause();
  try { SM.o = await api(`/api/library/smaller/${how}`, { json: it }); renderSmaller(); if (typeof loadStorage === "function") loadStorage(); }
  catch (e) { b.disabled = false; showError(e.message, how === "keep" ? "Couldn't keep it" : "Couldn't put it back"); }
}
function smFollow(job, it) {
  const k = smKey(it.kind, it.name);
  SM.jobs[k] = { id: job, msg: "Starting…" };
  renderSmaller();
  const stop = watchJob(job, ev => {
    if (!ev.final) { if (ev.label || ev.message) { SM.jobs[k].msg = ev.label || ev.message; const s = $(`#sm-body`); if (s) renderSmaller(); } return; }
    stop(); delete SM.jobs[k];
    if (ev.state === "error") showError(ev.message, "Couldn't make it smaller");
    else if (ev.message) showError(ev.message, "Made smaller", null, true);
    smallerLoad();
  });
}
// a job started elsewhere (another tab, before a reload): follow it here too
onJobs(() => {
  if (!SM.o || $("#room-storage")?.hidden) return;
  for (const j of runningJobs()) {
    if (j.kind !== "smaller" || Object.values(SM.jobs).some(x => x.id === j.id)) continue;
    const m = (j.title || "").match(/^Making (?:the )?(.+?)(?: project)? smaller$/);
    if (m) smFollow(j.id, { kind: /project smaller$/.test(j.title) ? "project" : "voice", name: m[1] });
  }
});
