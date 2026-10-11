"use strict";
// Hidden classic-WinAmp skin, suite-wide. Triple-click the UnderCover wordmark (top bar), or type "llama" outside a text box.
// Uses helpers from app.js ($, $$, store, S, active, aCover, aOrig, togglePlay, stopAll, showResult, HIST, redrawAll)
// and speakeasy.js (SE, sePlayRecent): the main window plays covers and speech, prev/next walk both, newest first.

const WA = { on: false, raf: 0, ctx: null, analyser: null, sources: new WeakSet(), typed: "" };

function waSet(on, announce) {
  WA.on = on;
  if (on) document.documentElement.dataset.skin = "winamp"; else delete document.documentElement.dataset.skin;
  store.set("uc-skin", on ? "winamp" : null);
  cancelAnimationFrame(WA.raf);
  if (on) { waLoop(); if (announce) waToast("It really whips the llama's ass!"); }
  redrawAll();
}
function waToast(text) {
  const t = $("#wa-toast");
  t.textContent = text;
  t.style.animation = "none"; void t.offsetWidth; t.style.animation = "";   // restart the fade
}

// triggers
let waClicks = 0, waClickTimer = null;
$("#suite-logo").addEventListener("click", () => {
  waClicks++;
  clearTimeout(waClickTimer);
  waClickTimer = setTimeout(() => { waClicks = 0; }, 600);
  if (waClicks >= 3) { waClicks = 0; waSet(!WA.on, true); }
});
document.addEventListener("keydown", e => {
  if (e.target.closest("input, textarea, select, [contenteditable]") || e.ctrlKey || e.metaKey || e.altKey || e.key.length !== 1) return;
  WA.typed = (WA.typed + e.key.toLowerCase()).slice(-5);
  if (WA.typed === "llama") { WA.typed = ""; waSet(!WA.on, true); }
});
$("#wa-main .wa-x").onclick = () => waSet(false);

// spectrum analyser: route the players through Web Audio once the skin is on and something plays
function waConnect(el) {
  try {
    if (!WA.ctx) {
      WA.ctx = new (window.AudioContext || window.webkitAudioContext)();
      WA.analyser = WA.ctx.createAnalyser();
      WA.analyser.fftSize = 512; WA.analyser.smoothingTimeConstant = 0.6;
      WA.analyser.connect(WA.ctx.destination);
    }
    if (!WA.sources.has(el)) { WA.ctx.createMediaElementSource(el).connect(WA.analyser); WA.sources.add(el); }
    if (WA.ctx.state === "suspended") WA.ctx.resume();
  } catch (e) { /* no analyser: the display still works */ }
}
[aCover, aOrig, T.audio, SE.audio, SE.rec].forEach(el => el.addEventListener("play", () => { if (WA.on) waConnect(el); }));

function waNowPlaying() {
  const a = [T.audio, SE.audio, SE.rec].find(x => x && !x.paused) || (WA.kind === "speech" && SE.rec.src ? SE.rec : active());
  return { a, playing: a && !a.paused && a.src };
}
function waTitle(a) {
  if (a === SE.rec && SE.recItem) return `${SE.recItem.title} (${SE.recItem.voice})`;
  if (a === SE.audio && SE.jobPlan) return `${SE.sents[SE.cur] || "Speaking"} (${SE.jobPlan.voice})`;
  return S.result ? `${S.result.title} (${S.result.voice})` : S.song ? S.song.title || S.song.name : "UnderCover · AI voices";
}
function waLoop() {
  const { a, playing } = waNowPlaying();
  const t = a && a.src ? a.currentTime : 0;
  $("#wa-time").textContent = `${String(Math.floor(t / 60)).padStart(2, "0")}:${String(Math.floor(t % 60)).padStart(2, "0")}`;
  $(".wa-state").textContent = playing ? "▶" : a && a.src && t > 0 ? "❚❚" : "■";
  const title = waTitle(a);
  const line = `*** UNDERCOVER *** ${title} *** `;
  if ($("#wa-song").textContent !== line) $("#wa-song").textContent = line;
  const c = $("#wa-vis"), g = c.getContext("2d");
  const W = c.width = c.clientWidth || 96, H = c.height = 22;
  g.clearRect(0, 0, W, H);
  const bars = Math.floor(W / 4);
  let data = null;
  if (WA.analyser && playing) { data = new Uint8Array(WA.analyser.frequencyBinCount); WA.analyser.getByteFrequencyData(data); }
  for (let i = 0; i < bars; i++) {
    // log-spaced bins, like the original analyser
    const k = data ? Math.min(data.length - 1, Math.floor(Math.pow(data.length, i / bars))) : 0;
    const v = data ? data[k] / 255 : 0;
    const h = Math.max(1, Math.round(v * H));
    for (let y = 0; y < h; y += 2) {
      g.fillStyle = y > H * 0.75 ? "#d43" : y > H * 0.5 ? "#dc3" : "#2d2";
      g.fillRect(i * 4, H - y - 2, 3, 1);
    }
  }
  cancelAnimationFrame(WA.raf);
  WA.raf = requestAnimationFrame(waLoop);
}

// transport buttons drive the normal players; prev/next walk covers and speech together, newest first
function waList() {
  const when = x => Date.parse((x.created || "").replace(" ", "T")) || 0;
  return [...HIST.map(h => ({ kind: "cover", id: h.id, at: when(h), item: h })), ...SE.recent.map(r => ({ kind: "speech", id: r.id, at: when(r), item: r }))]
    .sort((a, b) => b.at - a.at);
}
$(".wa-transport").addEventListener("click", e => {
  const b = e.target.closest("button"); if (!b) return;
  const act = b.dataset.a, { a } = waNowPlaying();
  if (act === "play") { if (a.src && a.paused) { if (a === SE.rec) a.play().catch(() => {}); else if (a === active()) togglePlay(); else a.play().catch(() => {}); } else if (!a.src) waJump(0); }
  else if (act === "pause") { if (!a.paused) { if (a === active()) togglePlay(); else a.pause(); } }
  else if (act === "stop") { stopAll(); SE.rec.pause(); SE.audio.pause(); try { a.currentTime = 0; } catch (err) {} }
  else if (act === "prev" || act === "next") {
    const list = waList(); if (!list.length) return;
    const cur = WA.kind === "speech" ? SE.recId : S.result?.id;
    const i = list.findIndex(x => x.kind === (WA.kind || "cover") && x.id === cur);
    waJump(i < 0 ? 0 : (i + (act === "next" ? 1 : -1) + list.length) % list.length);
  }
});
aCover.addEventListener("play", () => { WA.kind = "cover"; });   // whatever was played last is where prev/next start
SE.rec.addEventListener("play", () => { WA.kind = "speech"; });
function waJump(i) {
  const x = waList()[i]; if (!x) return;
  WA.kind = x.kind;
  if (x.kind === "speech") { stopAll(); sePlayRecent(x.item); return; }
  SE.rec.pause(); SE.audio.pause();
  showResult(x.item).then(() => aCover.addEventListener("canplay", () => togglePlay(), { once: true }));
}

// FM / MP / SE / SH lights (where mono / stereo were): the module on screen is lit
function waLights() { $$(".wa-lights .wa-st").forEach(l => l.classList.toggle("on", l.dataset.m === document.body.dataset.module)); }
document.addEventListener("uc:module", waLights);
waLights();

if (store.get("uc-skin", null) === "winamp") waSet(true, false);
