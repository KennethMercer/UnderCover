"use strict";
// DoubleAgent's "Your voices" cards: each voice with its status (voice ✓ · cadence ✓ / – / queued) and
// Listen / Use in FrontMan / Improve / Train cadence. Fed by /api/work/candidates (work.js loadCandidates calls renderVoiceCards)
// plus the voice samples from SafeHouse's /api/library/voices. Uses helpers from app.js ($, $$, esc, api, showError,
// loadVoices, VOL), work.js (W, renderQueue, loadCandidates, fmtMin) and shell.js (showTab).
// "Keep this one" (phase 9): a voice with a new cadence waiting shows "Listen and choose" ([data-cad-ab]); it opens the
// #cad-ab block in that card (one at a time): both readings of the same line, button[data-keep="current"|"pending"]
// and a textarea for why. Blind by default (the ear decides): the readings are A and B, in an order fixed per pair;
// "Which is which?" names them, and keeping one says which it was. AB_BLIND = false labels them plainly.

const DA = { samples: {}, all: false, audio: new Audio(), playing: null, cands: [],
             ab: null };   // the open A/B: { voice, st (GET .../cadence), why, err, busy }

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
  const kept = $("#cad-ab", box);   // the open A/B survives a redraw (a reading playing, the why text)
  box.innerHTML = shown.map(c => {
    const busy = running.has(c.target);
    const facts = c.trained ? [c.singing ? "singing" : c.singing === false ? "speech" : "", c.minutes ? `${Math.round(c.minutes)} min` : ""].filter(Boolean).join(" · ")
                            : c.can ? `downloaded · ${esc(c.person || "")}` : "downloaded";
    const likeness = c.likeness != null ? `<small data-deep>likeness ${(+c.likeness).toFixed(2)}</small>` : "";
    const improve = !c.can ? `<button class="mini" type="button" data-own="${esc(c.voice)}">Train my own</button>`
      : `<button class="mini" type="button" data-improve="${esc(c.voice)}" ${c.queued || busy ? "disabled" : ""}>${busy ? "Improving" : c.queued ? "Improve (queued)" : "Improve"}</button>`;
    const cd = c.cadence || {};
    const cadSt = (cd.has ? `<span class="st ok">cadence ✓</span>` : cd.queue === "running" ? `<span class="st now">cadence: making it</span>`
      : cd.queue ? `<span class="st now">cadence queued</span>` : `<span class="st">cadence –</span>`)
      + (cd.pending ? `<span class="st now">new cadence to hear</span>` : "");
    const abOpen = DA.ab?.voice === c.voice;
    const abBtn = cd.pending ? `<button class="mini" type="button" data-cad-ab="${esc(c.voice)}" aria-expanded="${abOpen}">${abOpen ? "Close" : "Listen and choose"}</button>` : "";
    const cadDeep = cd.has ? `<small class="meta" data-deep>cadence: ${esc(cd.engine || "")} · ${cd.data_minutes ?? "?"} min · likeness ${cd.scores?.likeness ?? "?"} · gap ${cd.scores?.cadence_gap ?? "?"}</small>` : "";
    const cadBtn = cd.can && !cd.queue ? `<button class="mini" type="button" data-cad="${esc(c.voice)}" title="${esc(cd.why || "")}">${cd.has ? "Retrain cadence" : c.trained ? "Train cadence" : "Find clips + cadence"}</button>` : "";
    return `<div class="vc ${busy ? "busy" : ""}">
      <div class="vc-top"><div class="avatar" aria-hidden="true">${vinitial(c.voice)}</div><div><b>${vname(c.voice)}</b><small>${esc(facts)}</small>${likeness}</div></div>
      <div class="vc-status"><span class="st ok">voice ✓</span>${cadSt}
        ${busy ? `<span class="st now">improving now</span>` : c.queued ? `<span class="st now">in the queue</span>` : ""}</div>${cadDeep}
      <div class="vc-acts">${DA.samples[c.voice] ? `<button class="mini" type="button" data-listen="${esc(c.voice)}" aria-pressed="${DA.playing === c.voice}">${DA.playing === c.voice ? "Stop" : "Listen"}</button>` : ""}
        <button class="mini" type="button" data-use="${esc(c.voice)}">Use in FrontMan</button>${improve}
        ${cadBtn}${abBtn}</div>${abOpen ? abHtml() : ""}</div>`;
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
  $$("[data-cad-ab]", box).forEach(b => b.onclick = () => openAb(b.dataset.cadAb));
  const fresh = $("#cad-ab", box);
  if (fresh && kept && kept.dataset.key && fresh.dataset.key === kept.dataset.key) fresh.replaceWith(kept);
  const ab = $("#cad-ab", box);
  if (ab) {
    $$(".ab-side", ab).forEach(sd => {   // one at a time, at the page's volume (muted stays silent)
      const a = $("audio", sd), bar = $(".ab-bar i", sd);
      $(".ab-play", sd).onclick = () => { if (a.paused) a.play().catch(() => showError("The browser couldn't play this reading.", "Playback failed")); else a.pause(); };
      a.onplay = () => {
        a.volume = VOL.muted ? 0 : VOL.level * VOL.level;
        $$("audio", ab).forEach(o => { if (o !== a) o.pause(); });
        if (DA.playing) { DA.audio.pause(); DA.playing = null; }
        sd.classList.add("playing");
      };
      a.onpause = a.onended = () => sd.classList.remove("playing");
      a.ontimeupdate = () => { bar.style.width = `${Math.min(100, a.currentTime / (a.duration || 1) * 100)}%`; };
    });
    const rv = $("[data-ab-reveal]", ab);
    if (rv) rv.onclick = () => { DA.ab.reveal = !DA.ab.reveal; renderVoiceCards(DA.cands); };
    const why = $("#cad-ab-why", ab);
    if (why) why.oninput = () => { DA.ab.why = why.value; };
    $$("[data-keep]", ab).forEach(b => b.onclick = () => keepAb(b.dataset.keep));
  }
  $$("[data-own]", box).forEach(b => b.onclick = () => {
    $(`#q-mode button[data-v="new"]`).click();
    $("#q-panel").scrollIntoView({ behavior: "smooth", block: "start" });
  });
}
const abUrl = (voice, rest = "") => `/api/train/voices/${encodeURIComponent(voice)}/cadence${rest}`;
const AB_BLIND = true;
// which reading is A: fixed per pair (from the new pack's id), so a redraw or a second look keeps the letters
const abOrder = st => [...String(st.pending.id)].reduce((n, c) => n + c.charCodeAt(0), 0) % 2 ? ["pending", "current"] : ["current", "pending"];
const abLetter = (st, w) => "AB"[abOrder(st).indexOf(w)];
function abHtml() {
  const a = DA.ab, st = a.st;
  if (!st || !st.ready) return `<div id="cad-ab" data-voice="${esc(a.voice)}"><p class="meta">${esc(a.err || (st ? "Making a short reading with each cadence…" : "Loading…"))}</p></div>`;
  const named = !AB_BLIND || a.reveal;
  const side = w => {
    const s = st[w], sc = s.scores || {}, L = abLetter(st, w);
    return `<div class="ab-side" data-side="${w}">
      <div class="ab-head"><span class="ab-letter">${L}</span>${named ? `<span class="ab-name">${w === "current" ? "Current" : "New"}</span>` : ""}</div>
      <div class="ab-player"><button class="ab-play" type="button" aria-label="Play ${L}"><svg class="ico i-play" aria-hidden="true"><use href="#g-play"/></svg><svg class="ico i-pause" aria-hidden="true"><use href="#g-pause"/></svg></button>
        <div class="ab-bar"><i></i></div></div>
      <audio preload="none" src="${abUrl(st.voice, `/sample/${w}`)}?v=${encodeURIComponent(s.id)}"></audio>
      <small class="meta" data-deep>${esc(s.clip || "")} · score ${sc.score ?? "?"} · likeness ${sc.likeness ?? "?"} · gap ${sc.cadence_gap ?? "?"}${sc.sound != null ? ` · clip sound −${sc.sound}` : ""}</small>
      <button class="btn" type="button" data-keep="${w}" ${a.busy ? "disabled" : ""}>Keep ${L}</button></div>`;
  };
  return `<div id="cad-ab" data-voice="${esc(st.voice)}" data-key="${esc([st.current.id, st.pending.id, a.busy ? 1 : 0, a.err || "", named ? 1 : 0].join("|"))}">
    <div class="ab-q"><b>Which sounds more like them?</b><span class="meta">Both read the same line: “${esc(st.line)}”</span></div>
    <div class="ab-sides">${abOrder(st).map(side).join("")}</div>
    <textarea id="cad-ab-why" rows="2" placeholder="What did you hear? (optional)">${esc(a.why || "")}</textarea>
    <div class="ab-foot">${AB_BLIND ? `<button class="linkbtn" type="button" data-ab-reveal>${named ? "Hide which is which" : "Which is which?"}</button>` : ""}
      <span class="meta">The one you keep is the voice's cadence from then on; the other is set aside, not deleted.</span></div>
    ${a.err ? `<p class="meta">${esc(a.err)}</p>` : ""}</div>`;
}
async function openAb(voice) {
  if (DA.ab?.voice === voice) { DA.ab = null; return renderVoiceCards(DA.cands); }
  const ab = DA.ab = { voice, st: null, why: "" };
  renderVoiceCards(DA.cands);
  try {
    ab.st = await api(abUrl(voice));
    if (!ab.st.ready) {
      const r = await api(abUrl(voice, "/ab"), { json: {} });
      if (r.ready) ab.st = r.state;
      else {
        refreshJobs(true);
        const off = watchJob(r.job, async ev => {
          if (!ev.final) return;
          off();
          if (ev.state !== "done") ab.err = ev.message || "The readings couldn't be made.";
          else { try { ab.st = await api(abUrl(voice)); } catch (e) { ab.err = e.message; } }
          if (DA.ab === ab) renderVoiceCards(DA.cands);
        });
      }
    }
  } catch (e) { ab.err = e.message; }
  if (DA.ab === ab) renderVoiceCards(DA.cands);
}
async function keepAb(keep) {
  const ab = DA.ab;
  if (!ab || ab.busy) return;
  ab.busy = true; ab.err = null; renderVoiceCards(DA.cands);
  try {
    await api(abUrl(ab.voice, "/verdict"), { json: { keep, why: ab.why || "" } });
    DA.ab = null;
    loadCandidates();
    const L = abLetter(ab.st, keep);   // blind until now: say which it was
    showError(keep === "pending" ? `${L} was the new cadence. ${voiceDisplay(ab.voice)} speaks with it from now on; the old one is set aside.`
                                 : `${L} was the current cadence, so nothing changes. The new one is set aside.`, `Kept ${L}`, null, true);
  } catch (e) { ab.busy = false; ab.err = e.message; renderVoiceCards(DA.cands); }
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
