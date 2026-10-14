// Set aside (a section of SafeHouse's Storage room): the voices UnderCover moved out of the way instead of deleting them (a replace, a kept
// retrained voice, a replace that didn't finish), each with why, when and what it takes. Nothing here is deleted unless you say so, twice.
// GET /api/library/aside -> [{id, voice, display, size, at, why, undo_for}] newest first; DELETE /api/library/aside/<id> -> {removed, freed,
// undo_ended_for}. Uses helpers from app.js ($, $$, esc, api, showError, gb) and library.js (when, loadStorage).
const AS = { list: [], ask: null, timer: null };

async function asideLoad() {
  try { AS.list = await api("/api/library/aside"); } catch (e) { AS.list = []; }
  renderAside();
}
function renderAside() {
  const box = $("#as-body"), sec = $("#aside");
  sec.hidden = !AS.list.length;
  if (!AS.list.length) return;
  const total = AS.list.reduce((a, x) => a + (x.size || 0), 0);
  $("#as-say").textContent = `${AS.list.length} set aside · ${gb(total)}`;
  box.innerHTML = `<p class="vt-lead">When a voice is replaced or you keep the other one, the old voice is moved here, never deleted. Delete only what you're sure you won't want back.</p>`
    + AS.list.map(x => {
      const asking = AS.ask === x.id;
      return `<div class="vt-row aside-row"><span><b>${esc(x.display || x.voice)}</b><small class="meta">${esc(x.why || "set aside")} · ${esc(when(x.at))}</small>
        ${asking && x.undo_for ? `<small class="meta aside-warn">Undo for ${esc(voiceDisplay(x.undo_for))} won't be able to bring it back.</small>` : ""}</span>
        <span class="row"><span class="meta mono">${gb(x.size || 0)}</span>
        <button class="btn${asking ? " confirm" : ""}" type="button" data-aside-rm="${esc(x.id)}" data-pc>${asking ? "Delete it?" : "Delete…"}</button></span></div>`;
    }).join("");
  $$("[data-aside-rm]", box).forEach(b => b.onclick = () => asideDelete(b.dataset.asideRm, b));
}
// two clicks within 4 s; the first one says what it will cost (an Undo that would no longer work)
async function asideDelete(id, btn) {
  if (AS.ask !== id) {
    AS.ask = id; AS.askAt = Date.now(); renderAside();
    clearTimeout(AS.timer); AS.timer = setTimeout(() => { if (AS.ask === id) { AS.ask = null; renderAside(); } }, 4000);
    return;
  }
  if (Date.now() - (AS.askAt || 0) < CONFIRM_MS) return;   // a double click isn't a confirmation
  clearTimeout(AS.timer); AS.ask = null; btn.disabled = true;
  try {
    const r = await api(`/api/library/aside/${encodeURIComponent(id)}`, { method: "DELETE" });
    showError(`${r.freed != null ? `${gb(r.freed)} freed. ` : ""}${r.undo_ended_for ? `Undo for ${voiceDisplay(r.undo_ended_for)} can't bring it back any more.` : ""}`.trim() || "Deleted.", "Deleted", null, true);
  } catch (e) { btn.disabled = false; showError(e.message, "Couldn't delete it"); }
  asideLoad();
  if (typeof loadStorage === "function") loadStorage();
}
