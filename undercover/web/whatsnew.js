// What's new (ARCHITECTURE "Ideas from MasterPlan" 6): after an update, a short note says what changed. The notes live in
// whatsnew.json (newest first; whoever merges a change the user will notice adds one). This browser remembers the newest
// note it showed (localStorage uc-whatsnew-seen); anything newer shows once in the card under the top bar (#wn), and
// SafeHouse's System room lists them all (#sys-news). A copy with no covers yet starts from here, quietly: nothing is "new" to someone
// who just arrived. Uses helpers from app.js ($, $$, esc, api, store).
const WN = { list: [], all: false };
const WN_SEEN = "uc-whatsnew-seen", WN_SHOW = 3, WN_ROOM = 5;
const wnDate = d => { try { return new Date(d + "T12:00").toLocaleDateString(undefined, { day: "numeric", month: "short" }); } catch (e) { return d; } };
const wnEntry = (e, h = "h3") => `<div class="wn-entry"><${h}>${esc(e.title)}</${h}><ul>${(e.lines || []).map(l => `<li>${esc(l)}</li>`).join("")}</ul></div>`;

async function wnBoot() {
  try { WN.list = (await (await fetch("/static/whatsnew.json", { cache: "no-cache" })).json()).entries || []; } catch (e) { return; }
  renderWnRoom();
  const newest = WN.list[0]?.id;
  if (!newest) return;
  const seen = store.get(WN_SEEN, null);
  if (seen === newest) return;
  let hist = null;
  try { hist = await api("/api/history"); } catch (e) {}
  if (Array.isArray(hist) && !hist.length) { store.set(WN_SEEN, newest); return; }   // a new copy: its first cover comes first
  const at = seen ? WN.list.findIndex(e => e.id === seen) : -1;
  const fresh = at > 0 ? WN.list.slice(0, at) : WN.list.slice(0, 1);   // never seen any (or an unknown one): just the newest
  renderWn(fresh);
}
function renderWn(fresh) {
  const shown = fresh.slice(0, WN_SHOW), more = fresh.length - shown.length;
  $("#wn-when").textContent = shown.length === 1 ? wnDate(shown[0].date) : "since you last looked";
  $("#wn-body").innerHTML = shown.map(e => wnEntry(e)).join("") + (more > 0 ? `<p class="meta">and ${more} more in SafeHouse's System room</p>` : "");
  $("#wn").hidden = false;
}
function wnDone() { if (WN.list[0]) store.set(WN_SEEN, WN.list[0].id); $("#wn").hidden = true; }
$("#wn-ok").onclick = wnDone;
$("#wn-all").onclick = () => { wnDone(); location.hash = "#safehouse/system"; window.scrollTo({ top: 0 }); };

// SafeHouse's System room: every note, newest first (the first five, then Show all)
function renderWnRoom() {
  const list = WN.all ? WN.list : WN.list.slice(0, WN_ROOM);
  $("#sys-news-list").innerHTML = list.length ? list.map(e => `<div class="sys wn-row"><span><b>${esc(e.title)}</b><small>${(e.lines || []).map(esc).join(" ")}</small></span><span class="meta mono">${esc(wnDate(e.date))}</span></div>`).join("")
    : `<p class="meta">No notes yet.</p>`;
  const b = $("#sys-news-more");
  b.hidden = WN.list.length <= WN_ROOM;
  b.textContent = WN.all ? "Show fewer" : `Show all ${WN.list.length}`;
}
$("#sys-news-more").onclick = () => { WN.all = !WN.all; renderWnRoom(); };
wnBoot();
