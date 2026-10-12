"use strict";
// DoubleAgent's "Your voices" cards: each voice with its status (voice ✓ · cadence ✓ / – / queued) and
// Listen / Use in FrontMan / Improve / Train cadence. Fed by /api/work/candidates (work.js loadCandidates calls renderVoiceCards)
// plus the voice samples from SafeHouse's /api/library/voices. Uses helpers from app.js ($, $$, esc, api, showError,
// loadVoices, VOL), work.js (W, renderQueue, loadCandidates, fmtMin) and shell.js (showTab).

const DA = { samples: {}, all: false, audio: new Audio(), playing: null, cands: [] };

async function loadSamples() {
  try { DA.samples = Object.fromEntries((await api("/api/library/voices")).filter(v => v.sample).map(v => [v.name, v.sample])); }
  catch (e) { DA.samples = {}; }
  renderVoiceCards(DA.cands);
}
function renderVoiceCards(cands) {
  DA.cands = cands || []; noteVoices(DA.cands);
  const box = $("#da-cards");
  if (!DA.cands.length) { box.innerHTML = `<p class="meta">No voices yet. Train one below, or install one in FrontMan.</p>`; $("#da-more").hidden = true; return; }
  const running = new Set((W.last?.queue || []).filter(i => i.state === "running").map(i => i.target));
  // voices trained here first (they can be improved), then the downloaded ones
  const list = [...DA.cands].sort((a, b) => (b.can - a.can) || (b.trained - a.trained) || a.voice.localeCompare(b.voice));
  const shown = DA.all ? list : list.slice(0, 6);
  box.innerHTML = shown.map(c => {
    const busy = running.has(c.target);
    const facts = c.trained ? [c.singing ? "singing" : c.singing === false ? "speech" : "", c.minutes ? `${Math.round(c.minutes)} min` : ""].filter(Boolean).join(" · ")
                            : c.can ? `downloaded · ${esc(c.person || "")}` : "downloaded";
    const likeness = c.likeness != null ? `<small data-deep>likeness ${(+c.likeness).toFixed(2)}</small>` : "";
    const improve = !c.can ? `<button class="mini" type="button" data-own="${esc(c.voice)}">Train my own</button>`
      : `<button class="mini" type="button" data-improve="${esc(c.voice)}" ${c.queued || busy ? "disabled" : ""}>${busy ? "Improving" : c.queued ? "Improve (queued)" : "Improve"}</button>`;
    const cd = c.cadence || {};
    const cadSt = cd.has ? `<span class="st ok">cadence ✓</span>` : cd.queue === "running" ? `<span class="st now">cadence: making it</span>`
      : cd.queue ? `<span class="st now">cadence queued</span>` : `<span class="st">cadence –</span>`;
    const cadDeep = cd.has ? `<small class="meta" data-deep>cadence: ${esc(cd.engine || "")} · ${cd.data_minutes ?? "?"} min · likeness ${cd.scores?.likeness ?? "?"} · gap ${cd.scores?.cadence_gap ?? "?"}</small>` : "";
    const cadBtn = cd.can && !cd.queue ? `<button class="mini" type="button" data-cad="${esc(c.voice)}" title="${esc(cd.why || "")}">${cd.has ? "Retrain cadence" : c.trained ? "Train cadence" : "Find clips + cadence"}</button>` : "";
    return `<div class="vc ${busy ? "busy" : ""}">
      <div class="vc-top"><div class="avatar" aria-hidden="true">${vinitial(c.voice)}</div><div><b>${vname(c.voice)}</b><small>${esc(facts)}</small>${likeness}</div></div>
      <div class="vc-status"><span class="st ok">voice ✓</span>${cadSt}
        ${busy ? `<span class="st now">improving now</span>` : c.queued ? `<span class="st now">in the queue</span>` : ""}</div>${cadDeep}
      <div class="vc-acts">${DA.samples[c.voice] ? `<button class="mini" type="button" data-listen="${esc(c.voice)}" aria-pressed="${DA.playing === c.voice}">${DA.playing === c.voice ? "Stop" : "Listen"}</button>` : ""}
        <button class="mini" type="button" data-use="${esc(c.voice)}">Use in FrontMan</button>${improve}
        ${cadBtn}</div></div>`;
  }).join("");
  $("#da-more").hidden = list.length <= 6;
  $("#da-more").textContent = DA.all ? "Show fewer" : `Show all ${list.length} voices`;
  $$("[data-listen]", box).forEach(b => b.onclick = () => listen(b.dataset.listen));
  $$("[data-use]", box).forEach(b => b.onclick = async () => { await loadVoices(b.dataset.use); showTab("frontman"); window.scrollTo({ top: 0, behavior: "smooth" }); });
  $$("[data-improve]", box).forEach(b => b.onclick = async () => {
    const c = DA.cands.find(x => x.voice === b.dataset.improve);
    b.disabled = true;
    try { W.last = await api("/api/work/add", { json: { kind: c.kind, target: c.target } }); renderQueue(); loadCandidates(); }
    catch (e) { b.disabled = false; showError(e.message, `Couldn't add ${c.voice}`); }
  });
  $$("[data-cad]", box).forEach(b => b.onclick = async () => {
    b.disabled = true; b.textContent = "Starting…";
    try { W.last = await api("/api/work/add", { json: { kind: "cadence", target: b.dataset.cad, now: true } }); renderQueue(); loadCandidates(); refreshJobs(true); }
    catch (e) { b.disabled = false; showError(e.message, "Couldn't start the cadence"); }
  });
  $$("[data-own]", box).forEach(b => b.onclick = () => {
    $(`#q-mode button[data-v="new"]`).click();
    $("#q-panel").scrollIntoView({ behavior: "smooth", block: "start" });
  });
}
function listen(voice) {
  const a = DA.audio;
  a.pause();
  if (DA.playing === voice) { DA.playing = null; return renderVoiceCards(DA.cands); }
  a.src = DA.samples[voice];
  a.volume = VOL.muted ? 0 : VOL.level * VOL.level;
  a.play().catch(() => showError("The browser couldn't play this sample.", "Playback failed"));
  DA.playing = voice;
  renderVoiceCards(DA.cands);
}
DA.audio.addEventListener("ended", () => { DA.playing = null; renderVoiceCards(DA.cands); });
$("#da-more").onclick = () => { DA.all = !DA.all; renderVoiceCards(DA.cands); };
document.addEventListener("uc:module", e => {
  if (e.detail === "doubleagent") loadSamples();
  else if (DA.playing) { DA.audio.pause(); DA.playing = null; renderVoiceCards(DA.cands); }
});
