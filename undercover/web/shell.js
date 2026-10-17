"use strict";
// The suite shell: the four module tabs (top bar; a bottom tab bar on a phone), deep links (#frontman, ...), the
// remembered tab, and the queue drawer (the running strip's "+N more"). Each module page announces itself with a
// "uc:module" event, which train.js, library.js and work.js listen to. Uses helpers from app.js ($, $$, store) and
// work.js (loadQueue, renderDrawer).

// module -> its view; the old tab names (deep links, localStorage "uc-tab") still work
const MODULES = { frontman: "covers", doubleagent: "train", speakeasy: "speakeasy", safehouse: "library" };
const OLD_TABS = { covers: "frontman", train: "doubleagent", mouthpiece: "doubleagent", library: "safehouse" };   // mouthpiece: the name before phase 2b
// a module's own sub-page rides after a slash (#safehouse/addons: SafeHouse's Add-ons room)
const moduleOf = t => { t = String(t || "").split("/")[0]; return MODULES[t] ? t : OLD_TABS[t] || null; };

function showTab(tab) {
  const mod = moduleOf(tab) || "frontman";
  for (const [m, view] of Object.entries(MODULES)) {
    $(`#view-${view}`).hidden = m !== mod;
    $(`#tab-${m}`).setAttribute("aria-selected", m === mod);
  }
  document.body.dataset.module = mod;
  store.set("uc-tab", mod);
  if (moduleOf(location.hash.slice(1)) !== mod || !location.hash.startsWith("#" + mod)) history.replaceState(null, "", "#" + mod);
  document.dispatchEvent(new CustomEvent("uc:module", { detail: mod }));
}
$$(".mods .mod").forEach(a => a.addEventListener("click", e => {
  if (e.ctrlKey || e.metaKey || e.shiftKey || e.button) return;   // let "open in a new tab" work
  e.preventDefault(); showTab(a.dataset.tab); window.scrollTo({ top: 0 });
}));
addEventListener("hashchange", () => {
  const m = moduleOf(location.hash.slice(1));
  if (m && m === document.body.dataset.module) document.dispatchEvent(new CustomEvent("uc:subpage", { detail: location.hash.slice(1) }));
  else if (m) showTab(m);
});
document.addEventListener("click", e => {
  const b = e.target.closest("[data-goto]"); if (!b) return;
  queueDrawer(false); showTab(b.dataset.goto); window.scrollTo({ top: 0 });
});

// ------------------------------------------------------------ the queue drawer: running now + the overnight queue
function queueDrawer(open) {
  const was = !$("#q-drawer").hidden;
  $("#q-drawer").hidden = $("#q-scrim").hidden = !open;
  document.body.classList.toggle("drawer-open", open);
  if (open && !was) { renderDrawer(); loadQueue(); $("#qd-close").focus(); }
  if (!open && was && !$("#wb-more").hidden) $("#wb-more").focus({ preventScroll: true });
}
$("#wb-more").onclick = () => queueDrawer(true);
// the strip's "Running" link: SafeHouse's Running room (now, tonight's queue, recent)
$("#wb-open").onclick = () => { queueDrawer(false); location.hash = "#safehouse/running"; window.scrollTo({ top: 0 }); };
$("#qd-open").onclick = () => { queueDrawer(false); location.hash = "#safehouse/running"; window.scrollTo({ top: 0 }); };   // Tonight is edited in one place, SafeHouse's Running room (consult 4)
$("#qd-close").onclick = $("#q-scrim").onclick = () => queueDrawer(false);
document.addEventListener("keydown", e => { if (e.key === "Escape" && !$("#q-drawer").hidden) queueDrawer(false); });
// "+ New voice" / "Train a new voice tonight": DoubleAgent's line; "+ Improve a voice": its voice cards (Improve tonight)
$$("[data-qadd]").forEach(b => b.onclick = () => {
  queueDrawer(false); showTab("doubleagent");
  const target = b.dataset.qadd === "improve" ? $("#da-voices") : $("#da-start");
  target.scrollIntoView({ behavior: "smooth", block: "start" });
  if (b.dataset.qadd !== "improve") $("#da-person").focus({ preventScroll: true });
});

// the count on the DoubleAgent tab and SafeHouse's Running room: queue jobs waiting or running; a pip while the queue is set to run
function queueCount(n, armed) {
  $("#da-badge").hidden = !n; $("#da-badge").textContent = n || "";
  $("#room-n-running").textContent = n ? `${n} queued` : "";
  $("#room-pip-running").hidden = !(armed && n);
}

// the top bar's height, for the running bar that sticks under it
const topH = () => document.documentElement.style.setProperty("--top-h", $(".top").offsetHeight + "px");
addEventListener("resize", topH); topH();

// first page: deep link, else the remembered tab (old names map to the modules)
showTab(moduleOf(location.hash.slice(1)) || moduleOf(store.get("uc-tab", "frontman")) || "frontman");
