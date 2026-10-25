"use strict";
// The First assignment (1.1): a spotlight tour in four steps, the user's pick B from docs/mockups/firstrun (dev repo) plus a waiting
// state, built for "the dumbest user". The page dims, the control in question is cut out of the dark and lit, and one card beside it
// (above or below, never over it) says what to do. Where there's something to do, doing it moves the tour on; Next only where there's
// nothing to do. Skip is on every card and asks once; yes ends it for good (server-side).
//   1. FrontMan's one-shot line, with a one-press "Hold On as Agent Smoke" (the sample song in the demo voice, through the one-shot).
//   While that cover is made the page stays dark: the app's own progress (the running strip, the one-shot's steps) is cut out and lit
//   but can't be pressed, and a panel in the dark says what it's doing in the job's own words, with a tip every ten seconds.
//   2. The result: listen, then Send to SafeHouse.   3. The SafeHouse tab, then the kept cover in the Library.
//   4. DoubleAgent's "Who should it sound like?", then one closing line.
// Server: /api/status's `tour` {step, skipped, done}; POST /api/firstrun {step | skip | done | replay} (app/firstrun.py).
// Uses app.js ($, $$, api, esc, S, HIST, showResult), shell.js (showTab), jobs.js (runningJobs), work.js (jobName, jobLine, glyph).

const TOUR = { known: null, step: 0, sub: 0, on: false, wait: false, job: null, failed: null, confirm: false, tip: 0, tipAt: 0, timer: 0, began: false };
const TOUR_TIPS = [
  "The glasses in the top bar are Deep Cover: every number and setting. You never need them to make a cover.",
  "Train tonight puts a voice in the night's queue: it trains while the PC is free, and it's ready in the morning.",
  "Low profile, beside Deep Cover, keeps UnderCover to half the graphics card, so you can play a game while it works.",
  "The last fifteen covers stay in Recent. Send to SafeHouse keeps one for good.",
  "Thumbs up or down under a cover teach UnderCover your ear: the voices you like come first.",
  "Phone access, in SafeHouse's Settings, lets you use UnderCover from your phone on your home Wi-Fi.",
  "Themes, in Settings, change the look. Five come with UnderCover, and you can make your own.",
  "\"Or set it up yourself\", under the one-shot line, takes your own file and the exact voice.",
];
const TOUR_SAMPLE = "Hold On as Agent Smoke";
const tourReduced = () => matchMedia("(prefers-reduced-motion: reduce)").matches;

// ------------------------------------------------------------ the overlay: a veil with holes (clicks only reach the holes), lit rings, one card
function tourDom() {
  if ($("#tour")) return;
  document.body.insertAdjacentHTML("beforeend", `<div class="tour" id="tour" hidden>
    <div class="tour-veil" id="tour-veil"></div><div id="tour-rings"></div>
    <div class="tour-card" id="tour-card" role="dialog" aria-modal="true" aria-labelledby="tour-h"></div></div>`);
}
const tourRect = el => { const r = el.getBoundingClientRect(); return r.width && r.height ? { x: r.left - 8, y: r.top - 8, w: r.width + 16, h: r.height + 16 } : null; };
function tourHoles(targets, shield) {
  const holes = targets.filter(Boolean).map(tourRect).filter(Boolean), W = innerWidth, H = innerHeight;
  $("#tour-veil").style.clipPath = `path(evenodd, "M0 0H${W}V${H}H0Z${holes.map(r => `M${r.x} ${r.y}h${r.w}v${r.h}h${-r.w}Z`).join("")}")`;
  $("#tour-rings").innerHTML = holes.map(r => `<div class="tour-ring${shield ? " shield" : ""}" style="left:${r.x}px;top:${r.y}px;width:${r.w}px;height:${r.h}px"></div>`).join("");
  return holes;
}
// the card in the dark: below the lit area if it fits, else above, else the page scrolls the lit area up; never over a hole
function tourPlace(holes) {
  const card = $("#tour-card"), phone = innerWidth < 520, h = card.offsetHeight, top = ($(".top")?.getBoundingClientRect().bottom || 50) + 8;
  const bar = phone ? ($(".mods")?.getBoundingClientRect().top || innerHeight) : innerHeight, bottom = Math.min(innerHeight, bar) - 8;
  if (!holes.length) { card.style.left = phone ? "12px" : `${Math.round((innerWidth - card.offsetWidth) / 2)}px`; card.style.top = `${Math.round(Math.max(top, (innerHeight - h) / 2))}px`; return; }
  const u = { top: Math.min(...holes.map(r => r.y)), bottom: Math.max(...holes.map(r => r.y + r.h)), left: Math.min(...holes.map(r => r.x)) };
  let y;
  if (bottom - (u.bottom + 12) >= h) y = u.bottom + 12;
  else if (u.top - 12 - top >= h) y = u.top - 12 - h;
  else if (u.top > top + 4 && !TOUR.scrolled) { TOUR.scrolled = true; window.scrollBy(0, u.top - top - 4); return; }   // the next tick places it
  else y = Math.max(top, bottom - h);
  card.style.top = `${Math.round(y)}px`;
  card.style.left = phone ? "12px" : `${Math.round(Math.max(12, Math.min(innerWidth - card.offsetWidth - 12, u.left)))}px`;
}

// ------------------------------------------------------------ the steps: what's lit, and the card's words
function tourTargets() {
  if (TOUR.wait) {   // the app's own progress, lit: the strip and the cover's steps. On a phone the steps don't fit beside the panel: the strip (its bar)
    // and the one-shot's own time line under it (the strip's second line is cut short there)
    const strip = $("#workbar:not([hidden])"), steps = $("#os-running:not([hidden])") || $("#progress:not([hidden])");
    return innerWidth < 520 ? [strip, $("#os-run-meta:not([hidden])") || (strip ? null : steps)] : [strip, steps];
  }
  if (TOUR.step === 1) return [$("#os-idle .row")];
  if (TOUR.step === 2) return [$("#result:not([hidden])")];
  if (TOUR.step === 3) return TOUR.sub ? [$('#lib-list .litem[data-kept="true"]')] : [$("#tab-safehouse")];
  if (TOUR.step === 4) return [$("#da-start .da-cmd"), $("#da-start .da-under")];   // the field and "Train tonight" under it (the card names both)
  return [];
}
const tourSkip = `<button class="linkbtn quiet tour-skip" type="button" data-tour="skip">Skip the tour</button>`;
function tourCard() {
  const c = $("#tour-card"), n = TOUR.step, k = n ? `<small>${n} of 4</small>` : "";
  if (TOUR.confirm) {
    c.innerHTML = `<div class="tour-k"><b id="tour-h">Skip the tour?</b></div><p>You can replay it from SafeHouse's Settings.</p>
      <div class="tour-acts"><button class="btn primary" type="button" data-tour="skip-yes">Skip the tour</button><button class="btn" type="button" data-tour="skip-no">Keep going</button></div>`;
  } else if (TOUR.failed) {
    c.innerHTML = `<div class="tour-k"><b id="tour-h">The cover didn't finish</b>${k}</div><p>${esc(TOUR.failed.message || "Something stopped it.")}</p>
      <div class="tour-acts"><button class="btn primary" type="button" data-tour="retry">Try again</button>${tourSkip}</div>`;
  } else if (TOUR.wait) {
    c.innerHTML = `<div class="tour-k"><b id="tour-h">Making your first cover</b>${k}</div><p class="tour-now" id="tour-now"></p>
      <p class="tour-tip" id="tour-tip"><span class="tour-tip-k">While you wait</span><span id="tour-tip-t"></span></p><div class="tour-acts">${tourSkip}</div>`;
    tourNow(); tourTip(true);
  } else if (n === 1) {
    c.innerHTML = `<div class="tour-k"><b id="tour-h">Your first assignment: a cover</b>${k}</div>
      <p>Type a song and a voice and press Make it. Or press the sample below: our own song, sung by Agent Smoke, the demo voice.</p>
      <div class="tour-acts"><button class="tour-try" type="button" data-tour="sample">${glyph("play")}${TOUR_SAMPLE}<small id="tour-try-t">about a minute</small></button>${tourSkip}</div>`;
  } else if (n === 2) {
    c.innerHTML = `<div class="tour-k"><b id="tour-h">Listen, then keep it</b>${k}</div>
      <p>Press play. Switch between Cover and Original to hear what changed. Then press Send to SafeHouse: it keeps the cover for good, and the rest roll off after fifteen.</p>
      <div class="tour-acts">${tourSkip}</div>`;
  } else if (n === 3 && !TOUR.sub) {
    c.innerHTML = `<div class="tour-k"><b id="tour-h">It lives in SafeHouse</b>${k}</div><p>Everything you keep is in SafeHouse. Open it.</p><div class="tour-acts">${tourSkip}</div>`;
  } else if (n === 3) {
    c.innerHTML = `<div class="tour-k"><b id="tour-h">Here it is</b>${k}</div><p>Your cover, kept in the Library. The other rooms hold tonight's work, storage and settings.</p>
      <div class="tour-acts"><button class="btn primary" type="button" data-tour="next">Next</button>${tourSkip}</div>`;
  } else if (n === 4) {
    c.innerHTML = `<div class="tour-k"><b id="tour-h">Last: a voice of your own</b>${k}</div>
      <p>Name someone here and press Train the voice, or Train tonight to let it train while the PC is free. Try it whenever you're ready.</p>
      <div class="tour-acts"><button class="btn primary" type="button" data-tour="next">Done</button>${tourSkip}</div>`;
  } else {
    c.innerHTML = `<div class="tour-k"><b id="tour-h">That's the job</b></div><p>Replay this any time from SafeHouse's Settings.</p>
      <div class="tour-acts"><button class="btn primary" type="button" data-tour="end">Done</button></div>`;
  }
  c.querySelector("[data-tour].btn.primary, .tour-try")?.focus({ preventScroll: true });
}
// the waiting panel's line: the job's own words (the stage it's at), never a made-up timer; its bar and time stay the app's, lit
function tourNow() {
  const el = $("#tour-now"); if (!el) return;
  const j = (typeof runningJobs === "function" ? runningJobs() : []).find(x => x.id === TOUR.job) || (typeof runningJobs === "function" ? runningJobs()[0] : null);
  const line = j ? (typeof jobLine === "function" ? jobLine(j, false) : "") || j.message || "" : "";
  const said = line ? line.replace(/^./, c => c.toUpperCase()).replace(/[.\s]+$/, "") + "." : "Getting started.";   // one full stop, whatever the job's words end with
  el.textContent = `${said} The bar and the time left are lit on the page.`;
}
function tourTip(first) {
  const t = $("#tour-tip-t"); if (!t) return;
  const tips = TOUR_TIPS.filter(x => !/^Low profile/.test(x) || !$("#lp-chip")?.hidden);   // only what this copy shows
  if (!first) TOUR.tip = (TOUR.tip + 1) % tips.length;
  TOUR.tipAt = Date.now();
  if (tourReduced() || first) { t.textContent = tips[TOUR.tip % tips.length]; return; }
  t.classList.add("out"); setTimeout(() => { t.textContent = tips[TOUR.tip % tips.length]; t.classList.remove("out"); }, 250);
}

// ------------------------------------------------------------ moving on: the real actions, polled while the tour is on
function tourTick() {
  if (!TOUR.on) return;
  if (TOUR.step === 1 && !TOUR.wait && !TOUR.failed && (S.osBusy || S.job)) { TOUR.wait = true; TOUR.job = (typeof OS !== "undefined" && OS.job) || S.job; tourCard(); }
  if (TOUR.wait) { tourNow(); if (Date.now() - TOUR.tipAt > 10000) tourTip(false); }
  // a cover that ends between two looks at the job feed never fires uc:jobdone (it was never seen running): once the page isn't busy, its end is read
  if (TOUR.wait && !S.osBusy && !S.job) {
    const j = (typeof JF !== "undefined" ? JF.list : []).find(x => x.id === TOUR.job && (x.final || ["done", "error", "cancelled"].includes(x.state)));
    if (j) tourJobEnded(j); else if (!TOUR.asked) { TOUR.asked = true; if (typeof refreshJobs === "function") refreshJobs(true).finally(() => { TOUR.asked = false; }); }
  }
  if (TOUR.step === 2 && $("#keep")?.dataset.kept === "true") return tourGo(3);
  if (TOUR.step === 3 && !TOUR.sub && document.body.dataset.module === "safehouse") { TOUR.sub = 1; tourShowKept(); tourCard(); }
  tourPlace(tourHoles(tourTargets(), TOUR.wait));
}
document.addEventListener("uc:jobdone", e => tourJobEnded(e.detail));
function tourJobEnded(j) {   // once: the event, or the tick's look at the feed
  if (!TOUR.on || !TOUR.wait || !["auto", "cover"].includes(j.kind)) return;
  const st = j.final?.state || j.state;
  if (st === "done") { TOUR.wait = false; TOUR.job = null; setTimeout(() => tourGo(2), 600); }   // the result panel is drawn by then
  else { TOUR.wait = false; TOUR.failed = { id: j.id, message: typeof plainMsg === "function" ? plainMsg(j.final?.message || j.message || "") : (j.final?.message || j.message || "") }; tourCard(); }
}
async function tourShowKept() {   // SafeHouse > Library > Covers > Kept, so the kept cover is in sight
  location.hash = "safehouse/library";
  const soon = async sel => { for (let i = 0; i < 40 && !$(sel); i++) await new Promise(r => setTimeout(r, 50)); return $(sel); };
  (await soon('#lib-cats .cat[data-cat="covers"]'))?.click();
  (await soon('#lib-kfilter [data-v="kept"]'))?.click();
  (await soon('#lib-list .litem[data-kept="true"]'))?.scrollIntoView({ block: "center" });
}
function tourGo(n) {
  TOUR.step = n; TOUR.sub = 0; TOUR.failed = null; TOUR.confirm = false; TOUR.scrolled = false;
  if (n >= 1 && n <= 4) api("/api/firstrun", { json: { step: n } }).catch(() => {});
  if (n === 2 && $("#result").hidden && HIST[0]) showResult(HIST[0]);   // after a reload: the newest cover
  if (n === 2) $("#result").scrollIntoView({ block: "center" });
  if (n === 4) { showTab("doubleagent"); window.scrollTo({ top: 0 }); }
  tourCard(); tourTick();
}
function tourStart(step) {
  tourDom(); TOUR.on = true; TOUR.began = true; $("#tour").hidden = false; document.body.classList.add("touring");
  clearInterval(TOUR.timer); TOUR.timer = setInterval(tourTick, 300);
  if (step <= 1) { showTab("frontman"); window.scrollTo({ top: 0 }); }
  tourGo(Math.max(1, step));
}
function tourEnd(how) {
  TOUR.on = false; clearInterval(TOUR.timer); $("#tour").hidden = true; document.body.classList.remove("touring");
  TOUR.known = { ...(TOUR.known || {}), done: true, skipped: how === "skip" };
  api("/api/firstrun", { json: how === "skip" ? { skip: true } : { done: true } }).catch(() => {});
}

// ------------------------------------------------------------ the card's buttons; Esc and the dark do nothing
document.addEventListener("click", e => {
  const b = e.target.closest("[data-tour]"); if (!b || !TOUR.on) return;
  const a = b.dataset.tour;
  if (a === "skip") { TOUR.confirm = true; tourCard(); }
  else if (a === "skip-no") { TOUR.confirm = false; tourCard(); }
  else if (a === "skip-yes") tourEnd("skip");
  else if (a === "sample") { $("#os-text").value = TOUR_SAMPLE; $("#os-text").dispatchEvent(new Event("input")); $("#os-go").click(); }
  else if (a === "retry") { const id = TOUR.failed?.id; TOUR.failed = null; TOUR.wait = true; tourCard();
    api(`/api/jobs/${id}/retry`, { method: "POST" }).then(r => { TOUR.job = r?.job || TOUR.job; if (typeof refreshJobs === "function") refreshJobs(true); })
      .catch(() => { TOUR.wait = false; $("#os-go").click(); }); }   // no retry for it: the same request again
  else if (a === "next") tourGo(TOUR.step + 1);
  else if (a === "end") tourEnd("done");
});
document.addEventListener("keydown", e => { if (TOUR.on && e.key === "Escape") e.stopPropagation(); }, true);
addEventListener("resize", () => { if (TOUR.on) { TOUR.scrolled = false; tourTick(); } });
addEventListener("scroll", () => { if (TOUR.on) tourTick(); }, { passive: true });

// ------------------------------------------------------------ when it starts: a fresh install's first visit to FrontMan (app.js's status poll)
function tourStatus(t) {
  if (!t) return;
  TOUR.known = t;
  if (TOUR.on || TOUR.began || t.done || t.skipped) return;
  if (document.body.dataset.module === "frontman" || t.step > 1) tourStart(t.step || 1);
}
document.addEventListener("uc:module", e => { if (e.detail === "frontman" && TOUR.known && !TOUR.known.done && !TOUR.on && !TOUR.began) tourStart(TOUR.known.step || 1); });
// Settings' Replay
$("#set-tour-replay").onclick = async () => {
  try { TOUR.known = (await api("/api/firstrun", { json: { replay: true } })).tour; } catch (e) { return showError(e.message, "Couldn't start the tour"); }
  TOUR.began = false; tourStart(1);
};
