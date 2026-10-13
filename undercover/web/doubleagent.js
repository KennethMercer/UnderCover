"use strict";
// DoubleAgent's page (the approved redesign, docs/mockups/doubleagent2.html): the line to start a voice (Train the voice /
// Train tonight instead), the one voice in the works, the card after training, and "Your voices": each with its likeness
// in a word and its dot (the server's likeness_word: fair / good / great), the trained-here and can-speak marks, and
// Listen / Use in FrontMan / Improve tonight / Let it speak. Numbers are Deep Cover; so is the step-by-step path (train.js).
// Fed by /api/work/candidates (work.js loadCandidates calls renderVoiceCards) plus the voice samples from SafeHouse's
// /api/library/voices. Uses helpers from app.js ($, $$, esc, api, showError, loadVoices, VOL, store), work.js (W, put,
// renderQueue, loadCandidates, jobProgress, jobLine, jobName, pauseWork), jobs.js and shell.js (showTab).
// "Keep this one" (phase 9): a voice with a new cadence waiting shows "Listen and choose" ([data-cad-ab]); it opens the
// #cad-ab block in that card (one at a time): both readings of the same line, button[data-keep="current"|"pending"]
// and a textarea for why. Always blind (the user's call: the ear decides): the readings are only A and B, in an order
// fixed per pair; keeping one says which it was.

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
  if (!DA.cands.length) { box.innerHTML = `<p class="meta">No voices yet. Name a person above, or install one from FrontMan's search.</p>`; $("#da-more").hidden = true; renderDaOutcome(); return; }
  const running = new Set((W.last?.queue || []).filter(i => i.state === "running").map(i => i.target));
  // voices trained here first (they can be improved), then the downloaded ones
  const list = [...DA.cands].sort((a, b) => (b.can - a.can) || (b.trained - a.trained) || a.voice.localeCompare(b.voice));
  const shown = DA.all ? list : list.slice(0, 6);
  const kept = $("#cad-ab", box);   // the open A/B survives a redraw (a reading playing, the why text)
  box.innerHTML = shown.map(c => {
    const busy = running.has(c.target), cd = c.cadence || {};
    // basic: the likeness in a word (the server's; the page never sets the cut-offs), where it came from, what it's doing
    const origin = c.demo ? "demo voice · not a real person"
      : [likeWord(c.likeness_word, "likeness"), c.trained ? "trained here" : "downloaded"].filter(Boolean).join(" · ");
    const state = cd.queue === "running" ? "learning to speak" : cd.queue ? "learning to speak tonight" : "";
    const status = [state && `<span class="st now">${state}</span>`, cd.pending && `<span class="st now">new voice to hear</span>`].filter(Boolean).join("");
    const speaks = cd.has ? `<svg class="ico vc-speaks" role="img" aria-label="Can speak in SpeakEasy"><title>Can speak in SpeakEasy</title><use href="#g-speakeasy"/></svg>` : "";
    const facts = [c.trained ? (c.singing ? "singing" : c.singing === false ? "speech" : "") : c.person ? `downloaded · ${esc(c.person)}` : "",
                   c.minutes ? `${Math.round(c.minutes)} min` : "", c.likeness != null ? `likeness ${(+c.likeness).toFixed(2)}` : ""].filter(Boolean).join(" · ");
    const improve = c.demo ? "" : !c.can ? `<button class="mini" type="button" data-own="${esc(c.voice)}">Train my own</button>`
      : `<button class="mini" type="button" data-improve="${esc(c.voice)}" ${c.queued || busy ? "disabled" : ""}>${busy ? "Improving now" : c.queued ? "In tonight's queue" : "Improve tonight"}</button>`;
    const abOpen = DA.ab?.voice === c.voice;
    const abBtn = cd.pending ? `<button class="mini" type="button" data-cad-ab="${esc(c.voice)}" aria-expanded="${abOpen}">${abOpen ? "Close" : "Listen and choose"}</button>` : "";
    const cadDeep = cd.has ? `<small class="meta" data-deep>cadence: ${esc(cd.engine || "")} · ${cd.data_minutes ?? "?"} min · likeness ${cd.scores?.likeness ?? "?"} · gap ${cd.scores?.cadence_gap ?? "?"}</small>` : "";
    const cadBtn = !cd.can || cd.queue ? "" : cd.has ? `<button class="mini" type="button" data-cad="${esc(c.voice)}" title="${esc(cd.why || "")}" data-deep>Retrain its speaking</button>`
      : `<button class="mini" type="button" data-cad="${esc(c.voice)}" title="${esc(cd.why || "")}">Let it speak</button>`;
    return `<div class="vc ${busy ? "busy" : ""}">
      <div class="vc-top"><div class="avatar" aria-hidden="true">${vinitial(c.voice)}</div><div><b>${vname(c.voice)}${speaks}</b><small>${origin}</small></div></div>
      ${status ? `<div class="vc-status">${status}</div>` : ""}${facts ? `<small class="meta" data-deep>${facts}</small>` : ""}${cadDeep}
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
    const why = $("#cad-ab-why", ab);
    if (why) why.oninput = () => { DA.ab.why = why.value; };
    $$("[data-keep]", ab).forEach(b => b.onclick = () => keepAb(b.dataset.keep));
  }
  $$("[data-own]", box).forEach(b => b.onclick = () => {   // the line above, with the person filled in
    const c = DA.cands.find(x => x.voice === b.dataset.own);
    $("#da-person").value = c?.person || voiceDisplay(b.dataset.own);
    $("#da-start").scrollIntoView({ behavior: "smooth", block: "start" });
    $("#da-person").focus({ preventScroll: true });
  });
  renderDaOutcome();
}

const abUrl = (voice, rest = "") => `/api/train/voices/${encodeURIComponent(voice)}/cadence${rest}`;
// which reading is A: fixed per pair (from the new pack's id), so a redraw or a second look keeps the letters
const abOrder = st => [...String(st.pending.id)].reduce((n, c) => n + c.charCodeAt(0), 0) % 2 ? ["pending", "current"] : ["current", "pending"];
const abLetter = (st, w) => "AB"[abOrder(st).indexOf(w)];
function abHtml() {
  const a = DA.ab, st = a.st;
  if (!st || !st.ready) return `<div id="cad-ab" data-voice="${esc(a.voice)}"><p class="meta">${esc(a.err || (st ? "Making a short reading with each cadence…" : "Loading…"))}</p></div>`;
  const side = w => {
    const s = st[w], sc = s.scores || {}, L = abLetter(st, w);
    return `<div class="ab-side" data-side="${w}">
      <div class="ab-head"><span class="ab-letter">${L}</span></div>
      <div class="ab-player"><button class="ab-play" type="button" aria-label="Play ${L}"><svg class="ico i-play" aria-hidden="true"><use href="#g-play"/></svg><svg class="ico i-pause" aria-hidden="true"><use href="#g-pause"/></svg></button>
        <div class="ab-bar"><i></i></div></div>
      <audio preload="none" src="${abUrl(st.voice, `/sample/${w}`)}?v=${encodeURIComponent(s.id)}"></audio>
      <small class="meta" data-deep>${esc(s.clip || "")} · score ${sc.score ?? "?"} · likeness ${sc.likeness ?? "?"} · gap ${sc.cadence_gap ?? "?"}${sc.sound != null ? ` · clip sound −${sc.sound}` : ""}${sc.pace_ratio != null ? ` · pace ×${sc.pace_ratio} (${esc(sc.pace_heard || "")})` : ""}${sc.pitch_st != null ? ` · pitch ${sc.pitch_st > 0 ? "+" : ""}${sc.pitch_st} st from their median${sc.pitch_off_st == null ? "" : Math.abs(sc.pitch_off_st) > 0.5 ? `, ${sc.pitch_off_st > 0 ? "+" : ""}${sc.pitch_off_st} st past their registers` : ", within their registers"}` : ""}${s.nudge ? ` (nudged ${s.nudge > 0 ? "+" : ""}${s.nudge} st from ${sc.pitch_raw_st > 0 ? "+" : ""}${sc.pitch_raw_st ?? "?"})` : ""}</small>
      <button class="btn" type="button" data-keep="${w}" ${a.busy ? "disabled" : ""}>Keep ${L}</button></div>`;
  };
  return `<div id="cad-ab" data-voice="${esc(st.voice)}" data-key="${esc([st.current.id, st.pending.id, a.busy ? 1 : 0, a.err || ""].join("|"))}">
    <div class="ab-q"><b>Which sounds more like them?</b><span class="meta">Both read the same line: “${esc(st.line)}”</span></div>
    <div class="ab-sides">${abOrder(st).map(side).join("")}</div>
    <textarea id="cad-ab-why" rows="2" placeholder="What did you hear? (optional)">${esc(a.why || "")}</textarea>
    <p class="meta">The one you keep is the voice's cadence from then on; the other is set aside, not deleted.</p>
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

// ------------------------------------------------------------ the line: a new voice (one way in; Train tonight is the queue's door)
const DA_MODE = { v: "speaking" };
$$("#da-mode button").forEach(b => b.onclick = () => {
  DA_MODE.v = b.dataset.v;
  $$("#da-mode button").forEach(x => x.setAttribute("aria-pressed", x === b));
});
$("#da-person").addEventListener("keydown", e => { if (e.key === "Enter") $("#da-train").click(); });
$("#da-train").onclick = () => daStart(false);
$("#da-tonight").onclick = () => daStart(true);
async function daStart(tonight) {
  const person = $("#da-person").value.trim();
  if (!person) return $("#da-person").focus();
  const btn = tonight ? $("#da-tonight") : $("#da-train"), label = btn.innerHTML;
  btn.disabled = true; btn.textContent = tonight ? "Adding…" : "Finding clips…";
  try {
    await trainVoice(person, DA_MODE.v, tonight);   // train.js
    $("#da-person").value = "";
    if (tonight) showError(`${person} is in tonight's work: it trains while the PC is free. SafeHouse's Running room shows it.`, "Added to tonight", null, true);
    else { daOutcomeClear(); renderDaWork(); }
  } catch (e) { showError(e.message, tonight ? "Couldn't add it to tonight" : "Couldn't start the voice"); }
  finally { btn.disabled = false; btn.innerHTML = label; }
}

// ------------------------------------------------------------ the one voice in the works (any DoubleAgent job), the long form of the strip
const DA_KINDS = new Set(["train", "train-auto", "train-prepare", "train-export", "train-transcribe", "night"]);
const DA_STAGES = ["Finding clips", "Keeping only their voice", "Training", "Checking it", "Adding it to your voices"];
// Do it all reports one stage with its progress (doubleagent/service.py train_new_voice): searching up to 0.02, preparing
// the clips (separating their voice) to 0.12, training to 0.97, adding at 0.98; a search for more clips says so in words
const daStage = j => {
  if (j.kind !== "train-auto") return null;
  const f = jobProgress(j) ?? 0;
  if (f < 0.12 && /^Searching|more songs|trying other/i.test(j.message || "")) return 0;
  return f < 0.02 ? 0 : f < 0.12 ? 1 : f < 0.97 ? 2 : f < 0.98 ? 3 : 4;
};
function renderDaWork() {
  const box = $("#da-work"), j = byWait(runningJobs()).find(x => DA_KINDS.has(x.kind));
  box.hidden = !j;
  if (!j) { put(box, ""); return; }
  const f = jobProgress(j), left = j.total_left ?? j.eta, ix = daStage(j), line = jobLine(j, false);
  const stopping = DA.stopAsk === j.id;
  const html = `<div class="who">In the works</div>
    <div class="da-prog"><span><b>${esc(jobName(j))}</b>${line ? ` · ${esc(line)}` : ""}</span>${left != null && !j.pausing ? `<span class="meta">${esc(leftText(left))}</span>` : ""}</div>
    ${f != null ? `<div class="bar"><i style="width:${Math.round(f * 100)}%"></i></div>` : ""}
    ${ix != null ? `<div class="da-stages">${DA_STAGES.map((s, i) => `<i class="${i < ix ? "done" : i === ix ? "now" : ""}">${s}</i>`).join("")}</div>` : ""}
    <div class="row">${PAUSABLE_KINDS.has(j.kind) ? `<button class="btn" type="button" data-da-pause>Pause</button>` : ""}
      <button class="linkbtn quiet${stopping ? " confirm" : ""}" type="button" data-da-stop="${esc(j.id)}">${stopping ? "Stop it? Click again" : "Stop and forget it"}</button>
      <span class="meta mono" data-deep>job ${esc(j.id)}${j.project ? ` · project ${esc(j.project)}` : ""}</span></div>`;
  if (!put(box, html)) return;
  $$("[data-da-pause]", box).forEach(b => b.onclick = async () => { b.disabled = true; await pauseWork(); b.disabled = false; });
  $$("[data-da-stop]", box).forEach(b => b.onclick = async () => {   // two clicks; the first is remembered across redraws
    const id = b.dataset.daStop;
    clearTimeout(DA.stopTimer);
    if (DA.stopAsk !== id) {
      DA.stopAsk = id; DA.stopTimer = setTimeout(() => { DA.stopAsk = null; renderDaWork(); }, 4000);
      return renderDaWork();
    }
    DA.stopAsk = null; b.disabled = true; b.textContent = "Stopping…";
    try { await api(`/api/jobs/${id}/cancel`, { method: "POST" }); } catch (e) { showError(e.message, "Couldn't stop it"); }
    refreshJobs(true);
  });
}
onJobs(() => { if (!$("#view-train").hidden) renderDaWork(); });

// ------------------------------------------------------------ after training: one card, ready (next steps) or weak (one way out)
function daOutcome(voice) { store.set("uc-da-outcome", voice); loadCandidates(); }   // the card draws once the voice is in the list
function daOutcomeClear() { store.set("uc-da-outcome", null); $("#da-outcome").hidden = true; }
function renderDaOutcome() {
  const v = store.get("uc-da-outcome", null), box = $("#da-outcome");
  const c = v ? DA.cands.find(x => x.voice === v) : null;
  box.hidden = !c;
  if (!c) return;
  const name = esc(voiceDisplay(v)), w = LIKE_WORDS.includes(c.likeness_word) ? c.likeness_word : null;
  const weak = w === "fair" || w === "poor" || c.quality?.ok === false;
  const mins = c.minutes != null ? Math.round(c.minutes) : null;
  box.innerHTML = weak
    ? `<h3>${name} is ready, but only a ${likeWord(w || "fair")} likeness.</h3>
       <p class="meta">${mins != null && mins < 10 ? `Only ${mins} minute${mins === 1 ? "" : "s"} of their clean voice were found; 10 is the aim. ` : ""}It sounds somewhat like them. More clips would make it good, or great.</p>
       <div class="row"><button class="btn primary" type="button" data-da-more>Find more clips and keep training</button><span class="meta">about 1 hour</span>
         <button class="linkbtn quiet" type="button" data-da-close>Use it as it is</button></div>`
    : `<h3>${name} is ready${w ? `: a ${likeWord(w)} likeness` : ""}.</h3>
       <p class="meta">${mins ? `Trained from ${mins} minutes of their voice, checked against real clips. ` : ""}It's in your voices now.</p>
       <div class="row"><button class="btn primary" type="button" data-da-use>Use it in FrontMan</button>
         ${c.cadence?.can && !c.cadence?.has && !c.cadence?.queue ? `<button class="btn" type="button" data-da-speak>Let it speak in SpeakEasy</button>` : ""}
         ${c.can && !c.queued ? `<button class="linkbtn" type="button" data-da-tonight>Improve it tonight</button>` : ""}
         <button class="linkbtn quiet" type="button" data-da-close>Close</button></div>`;
  const add = async (b, body, done) => {
    b.disabled = true;
    try { W.last = await api("/api/work/add", { json: body }); renderQueue(); refreshJobs(true); daOutcomeClear(); loadCandidates(); if (done) showError(done, "On it", null, true); }
    catch (e) { b.disabled = false; showError(e.message, "Couldn't start it"); }
  };
  $$("[data-da-close]", box).forEach(b => b.onclick = daOutcomeClear);
  $$("[data-da-use]", box).forEach(b => b.onclick = async () => { daOutcomeClear(); await loadVoices(v); showTab("frontman"); window.scrollTo({ top: 0, behavior: "smooth" }); });
  $$("[data-da-more]", box).forEach(b => b.onclick = () => add(b, { kind: c.kind || "improve", target: c.target || v, now: true }, `Finding more clips of ${voiceDisplay(v)} and training again. The bar at the top shows how far.`));
  $$("[data-da-speak]", box).forEach(b => b.onclick = () => add(b, { kind: "cadence", target: v, now: true }, `${voiceDisplay(v)} is learning to speak. It shows in SpeakEasy when it's done.`));
  $$("[data-da-tonight]", box).forEach(b => b.onclick = () => add(b, { kind: c.kind || "improve", target: c.target || v }, `${voiceDisplay(v)} improves tonight.`));
}
document.addEventListener("uc:module", e => { if (e.detail === "doubleagent") { renderDaWork(); renderDaOutcome(); } });
