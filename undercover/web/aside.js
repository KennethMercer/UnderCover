// Set aside (a section of SafeHouse's Storage room): the voices UnderCover moved out of the way instead of deleting them (a replace, a kept
// retrained voice, a replace that didn't finish), each with why, when and what it takes. Nothing here is deleted unless you say so, twice.
// GET /api/library/aside -> [{id, voice, display, size, at, why, undo_for}] newest first; DELETE /api/library/aside/<id> -> {removed, freed,
// undo_ended_for}. Uses helpers from app.js ($, $$, esc, api, showError, gb) and library.js (when, loadStorage).
const ASIDE = { list: [], ask: null, timer: null };

async function asideLoad() {
  try { ASIDE.list = await api("/api/library/aside"); } catch (e) { ASIDE.list = []; }
  renderAside();
}
function renderAside() {
  const box = $("#as-body");   // its row in Storage's Make room group says the count and the size; the row opens it
  const total = ASIDE.list.reduce((a, x) => a + (x.size || 0), 0), n = ASIDE.list.length;
  $("#as-say").textContent = n ? gb(total) : "none";
  $("#as-what").textContent = n ? `${n} voice${n > 1 ? "s" : ""} UnderCover moved out of the way` : "voices UnderCover moved out of the way, never deleted";
  if (!n) { box.innerHTML = `<p class="meta">Nothing set aside. When a voice is replaced or you keep the other one, the old voice is moved here, never deleted.</p>`; return; }
  // rows that read the same (two copies of one voice) get the folder name beside them, in basic view too
  const same = x => ASIDE.list.filter(y => (y.display || y.voice) === (x.display || x.voice)).length > 1;
  box.innerHTML = `<p class="vt-lead">When a voice is replaced or you keep the other one, the old voice is moved here, never deleted. Delete only what you're sure you won't want back.</p>`
    + ASIDE.list.map(x => {
      const asking = ASIDE.ask === x.id;
      return `<div class="vt-row aside-row"><span><b>${esc(x.display || x.voice)}</b>${same(x) ? `<small class="meta mono">${esc(x.voice)}</small>` : ""}<small class="meta">${esc(x.why || "set aside")} · ${esc(when(x.at))}</small>
        ${asking && x.undo_for ? `<small class="meta aside-warn">Undo for ${esc(voiceDisplay(x.undo_for))} won't be able to bring it back.</small>` : ""}</span>
        <span class="row"><span class="meta mono">${gb(x.size || 0)}</span>
        <button class="btn" type="button" data-aside-back="${esc(x.id)}" data-pc>Bring it back</button>
        <button class="btn${asking ? " confirm" : ""}" type="button" data-aside-rm="${esc(x.id)}" data-pc data-deep>${asking ? "Delete it?" : "Delete…"}</button></span></div>`;
    }).join("");
  $$("[data-aside-rm]", box).forEach(b => b.onclick = () => asideDelete(b.dataset.asideRm, b));
  $$("[data-aside-back]", box).forEach(b => b.onclick = async () => { b.disabled = true; await asideRestore(b.dataset.asideBack); if (b.isConnected) b.disabled = false; });
}
// Bring it back: POST /api/library/aside/<id>/restore -> {voice, with}. It's refused when the name is taken now, or when the voice's own
// Undo serves that folder ("Use Undo on …"): the server's words are shown as they come.
async function asideRestore(id) {
  try {
    const r = await api(`/api/library/aside/${encodeURIComponent(id)}/restore`, { json: {} });
    showError(`${voiceDisplay(r.voice)} is back${r.with ? `, with the retrained one that waited beside it` : ""}.`, "Brought back", null, true);
    if (typeof loadVoices === "function") loadVoices();
    if (typeof loadCandidates === "function") loadCandidates();
  } catch (e) { showError(e.message, "Couldn't bring it back"); }
  asideLoad();
  if (typeof loadStorage === "function") loadStorage();
  if (typeof afterChange === "function" && typeof L !== "undefined" && L.visible) afterChange();
}
// two clicks within 4 s; the first one says what it will cost (an Undo that would no longer work)
async function asideDelete(id, btn) {
  if (ASIDE.ask !== id) {
    ASIDE.ask = id; ASIDE.askAt = Date.now(); renderAside();
    clearTimeout(ASIDE.timer); ASIDE.timer = setTimeout(() => { if (ASIDE.ask === id) { ASIDE.ask = null; renderAside(); } }, 4000);
    return;
  }
  if (Date.now() - (ASIDE.askAt || 0) < CONFIRM_MS) return;   // a double click isn't a confirmation
  clearTimeout(ASIDE.timer); ASIDE.ask = null; btn.disabled = true;
  try {
    const r = await api(`/api/library/aside/${encodeURIComponent(id)}`, { method: "DELETE" });
    showError(`${r.freed != null ? `${gb(r.freed)} freed. ` : ""}${r.undo_ended_for ? `Undo for ${voiceDisplay(r.undo_ended_for)} can't bring it back any more.` : ""}`.trim() || "Deleted.", "Deleted", null, true);
  } catch (e) { btn.disabled = false; showError(e.message, "Couldn't delete it"); }
  asideLoad();
  if (typeof loadStorage === "function") loadStorage();
}
