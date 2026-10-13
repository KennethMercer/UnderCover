// The ear everywhere (ARCHITECTURE "Ideas from MasterPlan" 4; mockup docs/mockups/thumbs.html, the user's answers of
// 2026-10-12; the voice lab's contract in docs/design/thumbs-contract.md). A thumb on every cover and speech: under
// FrontMan's and SpeakEasy's players and in SafeHouse's detail pane; the thumb you gave shows as a small mark in lists.
// A thumbs down asks why (optional chips) and offers one fix when its reason has one. Liked voices rise in FrontMan's
// voice list. "Your ear" in SafeHouse's Settings says what it's for and forgets on request. Nothing changes on its own.
// No backend (/api/ear 404): nothing shows. Uses helpers from app.js ($, $$, esc, api, showError, voiceDisplay, showTab,
// S, startProgress), library.js (confirmClick), speakeasy.js (seStart) and jobs.js (refreshJobs).
const EAR = { on: false, items: {}, reasons: {}, summary: null, open: {}, fix: {} };
const EAR_ICON = { up: "g-up", down: "g-down" };
const earKey = (kind, id) => `${kind}:${id}`;
const earOf = (kind, id) => EAR.items[earKey(kind, id)] || null;
const earChips = (kind, thumb) => EAR.reasons?.[kind]?.[thumb] || [];

// ------------------------------------------------------------ the data (GET /api/ear)
async function earLoad() {
  try {
    const d = await api("/api/ear");
    Object.assign(EAR, { on: true, items: d.items || {}, reasons: d.reasons || {}, summary: d.summary || null });
  } catch (e) { EAR.on = false; }
  earRedraw();
}
// everything that shows a thumb or a mark draws again (each one is guarded: a page that isn't open just skips)
function earRedraw() {
  $$(".ear[data-ear-kind]").forEach(box => earDraw(box));
  if (typeof renderHistory === "function") renderHistory();
  if (typeof seRecent === "function" && typeof SE !== "undefined" && SE.recent) seRecent();
  if (typeof renderList === "function" && typeof L !== "undefined" && (L.cat === "covers" || L.cat === "speech")) renderList();
  if (typeof renderVoices === "function" && typeof S !== "undefined" && S.voicesLoaded) renderVoices();
  renderEarSetting();
}
// rating again replaces the earlier thumb (say why = the same thumb again with reasons); taking it back = forget
async function earRate(kind, id, thumb, reasons = [], note = "") {
  const k = earKey(kind, id);
  if (thumb) {
    const r = await api("/api/ear/rate", { json: { kind, id, thumb, reasons, note } });
    EAR.items[k] = r.item;
    EAR.fix[k] = thumb === "down" ? r.fix || null : null;
  } else {
    await api("/api/ear/forget", { json: { kind, id } });
    delete EAR.items[k]; EAR.fix[k] = null;
  }
  earLoad();   // the counts, the voices' order and the marks catch up
}

// ------------------------------------------------------------ the line under a player (#ear-cover, #se-ear, #lib-ear)
// earShow(box, "cover" | "speech", item): the cover or speech record (id, voice, options / text...), or null to hide
function earShow(box, kind, item) {
  if (!box) return;
  box.dataset.earKind = kind; box.dataset.earId = item?.id || "";
  box._earItem = item || null;
  earDraw(box);
}
function earDraw(box) {
  const kind = box.dataset.earKind, id = box.dataset.earId;
  box.hidden = !EAR.on || !id;
  if (box.hidden) return;
  const t = earOf(kind, id), k = earKey(kind, id), thumb = t?.thumb || null, said = new Set(t?.reasons || []);
  const btn = v => `<button class="thumb ${v}" type="button" data-thumb="${v}" aria-pressed="${thumb === v}" aria-label="${v === "up" ? "Good" : "Not good"}" title="${v === "up" ? "Good" : "Not good"}"><svg class="ico" aria-hidden="true"><use href="#${EAR_ICON[v]}"/></svg></button>`;
  const q = kind === "speech" ? "How did it sound?" : "How did it come out?";
  const chips = list => `<div class="ear-chips">${list.map(r => `<button type="button" data-why="${esc(r.id)}" aria-pressed="${said.has(r.id)}">${esc(r.label)}</button>`).join("")}
      ${EAR.open[k] === "note" ? `<span class="ear-note"><input type="text" maxlength="500" placeholder="What was it?" aria-label="What was it" value="${esc(t?.note || "")}"><button class="linkbtn" type="button" data-note-save>Save</button></span>`
        : `<button class="linkbtn" type="button" data-note>${t?.note ? `“${esc(t.note.length > 40 ? t.note.slice(0, 40) + "…" : t.note)}”` : "Something else…"}</button>`}</div>`;
  let html = `<div class="ear-line">${!thumb ? `<span class="ear-q">${q}</span>` : ""}<span class="ear-thumbs">${btn("up")}${btn("down")}</span>`;
  if (thumb === "up") html += `<span class="ear-said"><b>Noted for the file.</b> More like this.</span>${EAR.open[k] !== "why" && !said.size && !t?.note ? `<button class="linkbtn quiet" type="button" data-sayw>Say why</button>` : ""}`;
  if (thumb === "down") html += `<span class="ear-said"><b>Noted.</b> What was off? <span class="meta">(optional, tap any)</span></span>`;
  html += `</div>`;
  if (thumb === "down") html += chips(earChips(kind, "down"));
  if (thumb === "up" && (EAR.open[k] === "why" || EAR.open[k] === "note" || said.size || t?.note)) html += chips(earChips(kind, "up"));
  const fix = thumb === "down" ? EAR.fix[k] : null;
  if (fix?.do) html += `<div class="ear-fix"><span>${esc(fix.say || "One thing to try:")}</span><button class="btn" type="button" data-fix="do">${esc(fix.do.label)}</button>${fix.alt ? `<button class="linkbtn" type="button" data-fix="alt">${esc(fix.alt.label)}</button>` : ""}${fix.meta ? `<p class="meta">${esc(fix.meta)}</p>` : ""}</div>`;
  box.innerHTML = html;
  const rate = (th, reasons, note) => earRate(kind, id, th, reasons, note).catch(e => showError(e.message, "Couldn't save that"));
  $$("[data-thumb]", box).forEach(b => b.onclick = () => { EAR.open[k] = null; rate(thumb === b.dataset.thumb ? null : b.dataset.thumb, [], ""); });   // the same thumb again takes it back
  $$("[data-why]", box).forEach(b => b.onclick = () => {
    const next = new Set(said);
    next.has(b.dataset.why) ? next.delete(b.dataset.why) : next.add(b.dataset.why);
    rate(thumb, [...next], t?.note || "");
  });
  $$("[data-sayw]", box).forEach(b => b.onclick = () => { EAR.open[k] = "why"; earDraw(box); });
  $$("[data-note]", box).forEach(b => b.onclick = () => { EAR.open[k] = "note"; earDraw(box); $("input", box)?.focus(); });
  const saveNote = () => { const v = ($(".ear-note input", box)?.value || "").trim().slice(0, 500); EAR.open[k] = null; rate(thumb, [...said], v); };
  $$("[data-note-save]", box).forEach(b => b.onclick = saveNote);
  $(".ear-note input", box)?.addEventListener("keydown", e => { if (e.key === "Enter") saveNote(); if (e.key === "Escape") { EAR.open[k] = null; earDraw(box); } });
  $$("[data-fix]", box).forEach(b => b.onclick = async () => {
    b.disabled = true;
    try { await earDo(fix[b.dataset.fix], kind, box._earItem); EAR.fix[k] = null; earDraw(box); }
    catch (e) { b.disabled = false; showError(e.message, "Couldn't do that"); }
  });
}
// a fix: the server says what (fix.do / fix.alt = {label, action, ...}); the page does it with the routes it already uses
async function earDo(a, kind, item) {
  if (!a) return;
  if (a.action === "work") {   // Improve the voice tonight, or train one of the person
    W.last = await api("/api/work/add", { json: a.body });
    if (typeof renderQueue === "function") renderQueue();
    refreshJobs(true);
    return showError(a.body?.kind === "improve" ? `${voiceDisplay(a.body.target)} improves tonight. SafeHouse's Running room shows it.`
      : `${a.body?.target || "The voice"} is in tonight's work. SafeHouse's Running room shows it.`, "On it", null, true);
  }
  if (a.action === "pick_voice") {   // Try another voice: FrontMan's voice list
    showTab("frontman");
    $("#voices")?.scrollIntoView({ behavior: "smooth", block: "center" });
    return $("#q")?.focus({ preventScroll: true });
  }
  if (a.action === "remake" && kind === "cover" && item) {   // the same cover again (its own song, voice, settings), octave or key moved
    const { job } = await api(`/api/history/${encodeURIComponent(item.id)}/again`, { json: { options: a.options || {} } });
    showTab("frontman");
    if (!S.job) { S.job = job; summary(); startProgress(job); }
    return refreshJobs(true);
  }
  if (a.action === "respeak" && kind === "speech" && item) {   // the same text again, read the way it asks
    const r = await api("/api/speak", { json: { text: item.text, voice: item.voice, reader: item.reader, pace: a.options?.pace || item.pace,
                                                 nudge: item.nudge ?? item.pitch ?? 0, cadence: item.mode === "cadence" ? null : false } });
    showTab("speakeasy");
    seStart(r.job, r.plan, r.estimate);
    return refreshJobs(true);
  }
  throw new Error("This copy of UnderCover doesn't know that fix yet.");
}

// ------------------------------------------------------------ the mark in lists, the order in FrontMan's voice list
const earMark = (kind, id) => {
  if (!EAR.on) return "";
  const v = earOf(kind, id)?.thumb;
  return v ? ` <span class="ear-mk ${v}" role="img" aria-label="${v === "up" ? "you liked it" : "you didn't like it"}" title="${v === "up" ? "You liked it" : "You didn't like it"}"><svg class="ico" aria-hidden="true"><use href="#${EAR_ICON[v]}"/></svg></span>` : "";
};
const earVoice = v => v?.ear || (EAR.on ? (EAR.summary?.voices || []).find(x => x.voice === v?.name) : null) || null;
// liked voices first: (up - down) descending, no thumbs = 0, otherwise the list's own order (the voice lab's rule)
const earSort = list => !EAR.on ? list : list.map((v, i) => [v, i]).sort(([a, i], [b, j]) => {
  const sa = (earVoice(a)?.up || 0) - (earVoice(a)?.down || 0), sb = (earVoice(b)?.up || 0) - (earVoice(b)?.down || 0);
  return sb - sa || i - j;
}).map(([v]) => v);
const earHint = v => {
  const e = earVoice(v);
  if (!EAR.on || !e || !(e.up > 0) || e.up <= (e.down || 0)) return "";
  return ` · <span class="ear-hint"><svg class="ico" aria-hidden="true"><use href="#g-up"/></svg>${e.up} of ${e.up + (e.down || 0)} liked</span>`;
};

// ------------------------------------------------------------ Your ear, in SafeHouse's Settings
function renderEarSetting() {
  const row = $("#set-ear-row");
  if (!row) return;
  row.hidden = !EAR.on;
  if (!EAR.on) return;
  const s = EAR.summary || {}, n = s.rated || 0;
  $("#set-ear-say").textContent = n
    ? `${n} cover${n === 1 ? "" : "s"} and speech rated: ${s.up || 0} good, ${s.down || 0} not. The voices you like come first in FrontMan's list. Nothing is changed or deleted because of a thumb.`
    : "Give a cover or a speech a thumb and UnderCover learns what you like: the voices you like come first in FrontMan's list. Nothing is changed or deleted because of a thumb.";
  $("#set-ear-forget").hidden = !n;
  const voices = (s.voices || []).filter(v => v.up || v.down), settings = (s.settings || []).filter(x => x.up || x.down);
  $("#set-ear-deep").innerHTML = (voices.length ? `<div class="ear-tbl"><b>Voice</b><b class="n">good</b><b class="n">not</b><b class="w">most said</b>
      ${voices.map(v => `<span>${esc(voiceDisplay(v.voice))}</span><span class="n up">${v.up || 0}</span><span class="n down">${v.down || 0}</span><span class="w meta">${esc((v.said || []).join(" · ").toLowerCase())}</span>`).join("")}</div>` : "")
    + (settings.length ? `<div class="ear-tbl"><b>Setting</b><b class="n">good</b><b class="n">not</b><b class="w"></b>
      ${settings.map(x => `<span>${esc(x.setting)}${x.value != null && x.value !== "" ? `: ${esc(String(x.value))}` : ""}</span><span class="n up">${x.up || 0}</span><span class="n down">${x.down || 0}</span><span class="w meta">${esc(x.kind || "")}</span>`).join("")}</div>` : "");
}
confirmClick($("#set-ear-forget"), "Forget them all?", async () => {
  try { await api("/api/ear/forget", { json: { all: true } }); EAR.fix = {}; await earLoad(); }
  catch (e) { showError(e.message, "Couldn't forget them"); }
});
earLoad();
