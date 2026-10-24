"use strict";
// Low profile (the user, 2026-10-24; mockup docs/mockups/lowprofile in the dev repo, approved as shown): one click in the top bar keeps
// UnderCover to half the graphics card and working in bursts, so a game gets the card most of the time. The numbers live behind the "▾"
// in Deep Cover only (no Settings row, the user's choice): the memory limit in whole GB (the card's size as the top), the share of the
// time it works, and "on by itself for games". The engine side is the backend's (1.0.2): /api/status carries
// low_profile {on, limit_gb, burst (a 0-1 share), auto} when it's there; POST /api/lowprofile {on?, limit_gb?, burst?, auto?} changes it.
// A server without low_profile keeps the control hidden and the page as it was. Uses app.js ($, $$, api, esc, showError), work.js (glyph).

const LP = { known: false, on: false, limit: null, card: null, burst: null, auto: null, open: false };
const lowProfileOn = () => LP.known && LP.on;   // work.js's strip asks
const burstWord = b => b <= 0.29 ? "a quarter of the time" : b <= 0.4 ? "a third of the time" : b <= 0.6 ? "half the time" : "three quarters of the time";

// from the status: the button (hidden without the backend's side), its state, and the open panel's values (not under the user's hand)
function renderLowProfile(lp, s) {
  const chip = $("#lp-chip"); if (!chip) return;
  if (!lp) { LP.known = false; chip.hidden = true; lpClose(); return; }
  LP.known = true; LP.on = !!lp.on; LP.limit = lp.limit_gb ?? LP.limit; LP.burst = lp.burst ?? LP.burst; LP.auto = lp.auto ?? LP.auto;
  LP.card = Math.max(1, Math.round(+(s && s.vram_gb) || (LP.limit ? LP.limit * 2 : 24)));
  chip.hidden = false;
  const b = $("#lp-toggle");
  b.setAttribute("aria-checked", String(LP.on));
  b.title = LP.on ? "Low profile is on: half the card, in bursts" : "Low profile: half the card, in bursts";
  if (LP.open && !$("#lp-pop").contains(document.activeElement)) lpFill();
}
function lpFill() {
  $("#lp-switch").setAttribute("aria-checked", String(LP.on));
  const lim = $("#lp-limit"); lim.max = LP.card; lim.value = LP.limit ?? Math.round(LP.card / 2);
  $("#lp-limit-out").textContent = `${lim.value} GB of ${LP.card} GB`; $("#lp-limit-top").textContent = `${LP.card} GB`;
  const bu = $("#lp-burst"); bu.value = Math.round((LP.burst ?? 0.33) * 100);
  $("#lp-burst-out").textContent = burstWord(bu.value / 100);
  $("#lp-auto").setAttribute("aria-checked", String(!!LP.auto));
}
async function lpSet(patch) {
  try { const r = await api("/api/lowprofile", { json: patch }); renderLowProfile(r.low_profile || { ...LP, ...patch, limit_gb: patch.limit_gb ?? LP.limit }, { vram_gb: LP.card }); }
  catch (e) { showError(e.message, "Low profile"); api("/api/status").then(s => renderLowProfile(s.low_profile, s)).catch(() => {}); }
  if (typeof pollStatusSoon === "function") pollStatusSoon();   // the strip's words follow (work.js)
}
function lpOpen(focus = true) {
  const pop = $("#lp-pop"), r = $("#lp-chip").getBoundingClientRect();
  LP.open = true; lpFill(); pop.hidden = false; $("#lp-more").setAttribute("aria-expanded", "true");
  pop.style.top = `${Math.round(r.bottom + 10)}px`; pop.style.right = `${Math.max(12, Math.round(innerWidth - r.right))}px`;
  if (focus) $("#lp-switch").focus();
}
function lpClose() { LP.open = false; $("#lp-pop").hidden = true; $("#lp-more")?.setAttribute("aria-expanded", "false"); }
$("#lp-toggle").onclick = () => lpSet({ on: !LP.on });
$("#lp-more").onclick = () => LP.open ? lpClose() : lpOpen();
$("#lp-switch").onclick = () => lpSet({ on: !LP.on });
$("#lp-auto").onclick = () => lpSet({ auto: !LP.auto });
$("#lp-limit").addEventListener("input", () => { $("#lp-limit-out").textContent = `${$("#lp-limit").value} GB of ${LP.card} GB`; });
$("#lp-limit").addEventListener("change", () => lpSet({ limit_gb: +$("#lp-limit").value }));
$("#lp-burst").addEventListener("input", () => { $("#lp-burst-out").textContent = burstWord($("#lp-burst").value / 100); });
$("#lp-burst").addEventListener("change", () => lpSet({ burst: +$("#lp-burst").value / 100 }));
document.addEventListener("keydown", e => { if (e.key === "Escape" && LP.open) { lpClose(); $("#lp-more").focus(); } });
document.addEventListener("click", e => { if (LP.open && !$("#lp-pop").contains(e.target) && !$("#lp-chip").contains(e.target)) lpClose(); });
addEventListener("resize", () => { if (LP.open) lpOpen(false); });   // re-placed under the chip, the focus left where it is
