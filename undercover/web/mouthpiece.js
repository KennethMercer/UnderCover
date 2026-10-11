"use strict";
// MouthPiece's "Your voices" cards: each voice with its status (voice ✓ · cadence –, cadence from phase 4) and
// Listen / Use in FrontMan / Improve. Fed by /api/work/candidates (work.js loadCandidates calls renderVoiceCards)
// plus the voice samples from SafeHouse's /api/library/voices. Uses helpers from app.js ($, $$, esc, api, showError,
// loadVoices, VOL), work.js (W, renderQueue, loadCandidates, fmtMin) and shell.js (showTab).

const MP = { samples: {}, all: false, audio: new Audio(), playing: null, cands: [] };

async function loadSamples() {
  try { MP.samples = Object.fromEntries((await api("/api/library/voices")).filter(v => v.sample).map(v => [v.name, v.sample])); }
  catch (e) { MP.samples = {}; }
  renderVoiceCards(MP.cands);
}
function renderVoiceCards(cands) {
  MP.cands = cands || [];
  const box = $("#mp-cards");
  if (!MP.cands.length) { box.innerHTML = `<p class="meta">No voices yet. Train one below, or install one in FrontMan.</p>`; $("#mp-more").hidden = true; return; }
  const running = new Set((W.last?.queue || []).filter(i => i.state === "running").map(i => i.target));
  // voices trained here first (they can be improved), then the downloaded ones
  const list = [...MP.cands].sort((a, b) => (b.can - a.can) || (b.trained - a.trained) || a.voice.localeCompare(b.voice));
  const shown = MP.all ? list : list.slice(0, 6);
  box.innerHTML = shown.map(c => {
    const busy = running.has(c.target);
    const facts = c.trained ? [c.singing ? "singing" : c.singing === false ? "speech" : "", c.minutes ? `${Math.round(c.minutes)} min` : "",
                               c.likeness != null ? `likeness ${(+c.likeness).toFixed(2)}` : ""].filter(Boolean).join(" · ")
                            : c.can ? `downloaded · ${esc(c.person || "")}` : "downloaded";
    const improve = !c.can ? `<button class="mini" type="button" data-own="${esc(c.voice)}">Train my own</button>`
      : `<button class="mini" type="button" data-improve="${esc(c.voice)}" ${c.queued || busy ? "disabled" : ""}>${busy ? "Improving" : c.queued ? "Improve (queued)" : "Improve"}</button>`;
    return `<div class="vc ${busy ? "busy" : ""}">
      <div class="vc-top"><div class="avatar" aria-hidden="true">${esc(c.voice[0].toUpperCase())}</div><div><b>${esc(c.voice)}</b><small>${esc(facts)}</small></div></div>
      <div class="vc-status"><span class="st ok">voice ✓</span><span class="st later" title="Cadence arrives in phase 4">cadence –</span><span class="soon">phase 4</span>
        ${busy ? `<span class="st now">improving now</span>` : c.queued ? `<span class="st now">in the queue</span>` : ""}</div>
      <div class="vc-acts">${MP.samples[c.voice] ? `<button class="mini" type="button" data-listen="${esc(c.voice)}" aria-pressed="${MP.playing === c.voice}">${MP.playing === c.voice ? "Stop" : "Listen"}</button>` : ""}
        <button class="mini" type="button" data-use="${esc(c.voice)}">Use in FrontMan</button>${improve}
        <button class="mini" type="button" disabled title="Phase 4">Train cadence</button></div></div>`;
  }).join("");
  $("#mp-more").hidden = list.length <= 6;
  $("#mp-more").textContent = MP.all ? "Show fewer" : `Show all ${list.length} voices`;
  $$("[data-listen]", box).forEach(b => b.onclick = () => listen(b.dataset.listen));
  $$("[data-use]", box).forEach(b => b.onclick = async () => { await loadVoices(b.dataset.use); showTab("frontman"); window.scrollTo({ top: 0, behavior: "smooth" }); });
  $$("[data-improve]", box).forEach(b => b.onclick = async () => {
    const c = MP.cands.find(x => x.voice === b.dataset.improve);
    b.disabled = true;
    try { W.last = await api("/api/work/add", { json: { kind: c.kind, target: c.target } }); renderQueue(); loadCandidates(); }
    catch (e) { b.disabled = false; showError(e.message, `Couldn't add ${c.voice}`); }
  });
  $$("[data-own]", box).forEach(b => b.onclick = () => {
    $(`#q-mode button[data-v="new"]`).click();
    $("#q-panel").scrollIntoView({ behavior: "smooth", block: "start" });
  });
}
function listen(voice) {
  const a = MP.audio;
  a.pause();
  if (MP.playing === voice) { MP.playing = null; return renderVoiceCards(MP.cands); }
  a.src = MP.samples[voice];
  a.volume = VOL.muted ? 0 : VOL.level * VOL.level;
  a.play().catch(() => showError("The browser couldn't play this sample.", "Playback failed"));
  MP.playing = voice;
  renderVoiceCards(MP.cands);
}
MP.audio.addEventListener("ended", () => { MP.playing = null; renderVoiceCards(MP.cands); });
$("#mp-more").onclick = () => { MP.all = !MP.all; renderVoiceCards(MP.cands); };
document.addEventListener("uc:module", e => {
  if (e.detail === "mouthpiece") loadSamples();
  else if (MP.playing) { MP.audio.pause(); MP.playing = null; renderVoiceCards(MP.cands); }
});
