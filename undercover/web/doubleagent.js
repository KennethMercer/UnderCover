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
             f: { like: "", speak: "", src: "" },   // "Your voices" filters: one chip pressed per group, or none
             cad: null,   // "Let it speak" open on a card: { voice } (Now or Tonight, with the time)
             own: null,   // "Train my own" open on a card: { voice, mode } (Speaking / Singing; taken from the voice where it says)
             ab: null };   // the open A/B: { voice, st (GET .../cadence), why, err, busy }

async function loadSamples() {
  try { DA.samples = Object.fromEntries((await api("/api/library/voices")).filter(v => v.sample).map(v => [v.name, v.sample])); }
  catch (e) { DA.samples = {}; }
  renderVoiceCards(DA.cands);
}
function renderVoiceCards(cands) {
  DA.cands = cands || []; noteVoices(DA.cands);
  const box = $("#da-cards");
  if (!DA.cands.length) {
    box.innerHTML = `<div class="empty"><b>No voices yet.</b>${typeof ASSIGNMENT_ON !== "undefined" && ASSIGNMENT_ON ? `<button class="btn" type="button" id="da-empty-assign">Take the first assignment</button>` : ""}<small class="meta">or name someone above</small></div>`;
    $("#da-empty-assign")?.addEventListener("click", () => assignBrief());
    $("#da-more").hidden = true; renderDaOutcome(); return;
  }
  const running = new Set((W.last?.queue || []).filter(i => i.state === "running").map(i => i.target));
  daFilters();
  // voices trained here first (they can be improved), then the downloaded ones
  const list = DA.cands.filter(daMatch).sort((a, b) => (b.can - a.can) || (b.trained - a.trained) || a.voice.localeCompare(b.voice));
  if (!list.length) { box.innerHTML = `<p class="meta">No voice matches these filters. <button class="linkbtn" type="button" id="da-fclear">Show every voice</button></p>`; $("#da-more").hidden = true; $("#da-fclear").onclick = () => { DA.f = { like: "", speak: "", src: "" }; renderVoiceCards(DA.cands); }; renderDaOutcome(); return; }
  const shown = DA.all ? list : list.slice(0, 6);
  const kept = $("#cad-ab", box);   // the open A/B survives a redraw (a reading playing, the why text)
  box.innerHTML = shown.map(c => {
    const busy = running.has(c.target), cd = c.cadence || {};
    const im = DA.imp[c.voice];
    // basic: the likeness in a word (the server's; the page never sets the cut-offs), where it came from, what it's doing
    const like = likeWord(c.likeness_word, "likeness"), num = c.likeness != null ? `likeness ${(+c.likeness).toFixed(2)}` : "";
    const origin = c.demo ? "demo voice · not a real person"
      : [like || num ? `${like ? `<span class="nodeep">${like}</span>` : ""}${num ? `<span data-deep>${num}</span>` : ""}` : "", c.trained ? "trained here" : "downloaded"].filter(Boolean).join(" · ");
    const state = cd.queue === "running" ? "learning to speak" : cd.queue ? "learns to speak tonight" : "";
    const status = [state && `<span class="st now">${state}</span>`, cd.pending && `<span class="st now">new voice to hear</span>`, im?.candidate && `<span class="st now">retrained voice to hear</span>`].filter(Boolean).join("");
    const speaks = cd.has || c.demo ? `<svg class="ico vc-speaks" role="img" aria-label="Can speak in SpeakEasy"><title>Can speak in SpeakEasy</title><use href="#g-speakeasy"/></svg>` : "";
    const facts = [c.trained ? (c.singing ? "singing" : c.singing === false ? "speech" : "") : c.person ? `downloaded · ${esc(c.person)}` : "",
                   c.minutes ? `${Math.round(c.minutes)} min` : ""].filter(Boolean).join(" · ");
    const improve = c.demo ? "" : !c.can ? `<button class="mini" type="button" data-own="${esc(c.voice)}" aria-expanded="${DA.own?.voice === c.voice}">${DA.own?.voice === c.voice ? "Close" : "Train my own"}</button>`
      : `<button class="mini" type="button" data-improve="${esc(c.voice)}" ${c.queued || busy || im?.candidate ? "disabled" : ""}>${busy ? "Improving now" : c.queued ? "In tonight's queue" : im?.candidate ? "Retrained one waiting" : "Improve tonight"}</button>`;
    const impOpen = DA.impAb?.voice === c.voice;
    const impBtn = im?.candidate ? `<button class="mini" type="button" data-imp-ab="${esc(c.voice)}" aria-expanded="${impOpen}">${impOpen ? "Close" : "Hear the retrained one"}</button>`
      : im?.undo ? `<button class="mini" type="button" data-imp-undo="${esc(c.voice)}">Undo my last choice</button>` : "";
    const ownOpen = DA.own?.voice === c.voice, cadOpen = DA.cad?.voice === c.voice;
    const abOpen = DA.ab?.voice === c.voice;
    const abBtn = cd.pending ? `<button class="mini" type="button" data-cad-ab="${esc(c.voice)}" aria-expanded="${abOpen}">${abOpen ? "Close" : "Listen and choose"}</button>` : "";
    const cadDeep = cd.has ? `<small class="meta" data-deep>cadence: ${esc(cd.engine || "")} · ${cd.data_minutes ?? "?"} min · likeness ${cd.scores?.likeness ?? "?"} · gap ${cd.scores?.cadence_gap ?? "?"}</small>` : "";
    const cadBtn = !cd.can || cd.queue ? "" : cd.has ? `<button class="mini" type="button" data-cad="${esc(c.voice)}" title="${esc(cd.why || "")}" data-deep>Retrain its cadence</button>`
      : `<button class="mini" type="button" data-cad="${esc(c.voice)}" aria-expanded="${DA.cad?.voice === c.voice}" title="${esc(cd.why || "")}">${DA.cad?.voice === c.voice ? "Close" : "Let it speak"}</button>`;
    return `<div class="vc ${busy ? "busy" : ""}">
      <div class="vc-top"><div class="avatar" aria-hidden="true">${vinitial(c.voice)}</div><div><b>${vname(c.voice, false)}${speaks}</b><small>${origin}</small></div></div>
      ${status ? `<div class="vc-status">${status}</div>` : ""}${facts ? `<small class="meta" data-deep>${facts}</small>` : ""}${cadDeep}
      <div class="vc-acts">${DA.samples[c.voice] ? `<button class="mini" type="button" data-listen="${esc(c.voice)}" aria-pressed="${DA.playing === c.voice}">${DA.playing === c.voice ? "Stop" : "Listen"}</button>` : ""}
        <button class="mini" type="button" data-use="${esc(c.voice)}">Use in FrontMan</button>${improve}
        ${cadBtn}${abBtn}${impBtn}</div>${ownOpen ? ownHtml(c) : ""}${cadOpen ? cadHtml(c) : ""}${abOpen ? abHtml() : ""}${impOpen ? impHtml(c.voice) : ""}</div>`;
  }).join("");
  $("#da-more").hidden = list.length <= 6;
  $("#da-more").textContent = DA.all ? "Show fewer" : `Show all ${list.length} voices`;
  $$("[data-listen]", box).forEach(b => b.onclick = () => listen(b.dataset.listen));
  $$("[data-use]", box).forEach(b => b.onclick = async () => { await loadVoices(b.dataset.use); showTab("frontman"); window.scrollTo({ top: 0, behavior: "smooth" }); });
  $$("[data-improve]", box).forEach(b => b.onclick = async () => {
    const c = DA.cands.find(x => x.voice === b.dataset.improve);
    b.disabled = true;
    try { W.last = await api("/api/work/add", { json: { kind: c.kind, target: c.target } }); renderQueue(); loadCandidates(); if (!workMoved(W.last)) showError(`${voiceDisplay(c.voice)} improves tonight.${startsNote(W.last)} SafeHouse's Running room shows it.`, "Added to tonight", null, true); }
    catch (e) { b.disabled = false; showError(e.message, `Couldn't add ${voiceDisplay(c.voice)}`); }
  });
  $$("[data-cad]", box).forEach(b => b.onclick = () => {   // Let it speak / Retrain its speaking: the choice opens on the card, Now or Tonight, with the time (IA P9-9)
    DA.cad = DA.cad?.voice === b.dataset.cad ? null : { voice: b.dataset.cad };
    renderVoiceCards(DA.cands);
  });
  $$("[data-cad-go]", box).forEach(b => b.onclick = async () => {
    const voice = DA.cad?.voice, now = b.dataset.cadGo === "now", name = voiceDisplay(voice);
    b.disabled = true; b.textContent = now ? "Starting…" : "Adding…";
    try {
      W.last = await api("/api/work/add", { json: { kind: "cadence", target: voice, ...(now ? { now: true } : {}) } });
      DA.cad = null; renderQueue(); loadCandidates(); refreshJobs(true); renderVoiceCards(DA.cands);
      const mins = (await estimateFor("cadence", voice))?.total;
      showError(now ? `${name} is learning how they talk${mins ? ` (${etaWords({ low: mins * 60, high: mins * 60 })})` : ""}. The bar at the top shows how far; Pause is there too.`
                    : `${name} learns how they talk tonight.${startsNote(W.last)} SafeHouse's Running room shows it.`, now ? "Learning to speak" : "Added to tonight", null, true);
    } catch (e) { b.disabled = false; b.textContent = now ? "Learn now" : "Tonight"; showError(e.message, "Couldn't start it"); }
  });
  $$("[data-cad-ab]", box).forEach(b => b.onclick = () => openAb(b.dataset.cadAb));
  const fresh = $("#cad-ab", box);
  if (fresh && kept && kept.dataset.key && fresh.dataset.key === kept.dataset.key) fresh.replaceWith(kept);
  $$("[data-imp-ab]", box).forEach(b => b.onclick = () => openImp(b.dataset.impAb));
  $$("[data-imp-undo]", box).forEach(b => b.onclick = () => undoImp(b.dataset.impUndo));
  const impAb = $("#imp-ab", box);
  if (impAb) { wireAbPlayers(impAb); $$("[data-imp-keep]", impAb).forEach(b => b.onclick = () => keepImp(b.dataset.impKeep)); }
  const ab = $("#cad-ab", box);
  if (ab) {
    wireAbPlayers(ab);
    const why = $("#cad-ab-why", ab);
    if (why) why.oninput = () => { DA.ab.why = why.value; };
    $$("[data-keep]", ab).forEach(b => b.onclick = () => keepAb(b.dataset.keep));
  }
  $$("[data-own]", box).forEach(b => b.onclick = () => {   // the choice opens on the card itself: Now or Tonight
    const c = DA.cands.find(x => x.voice === b.dataset.own);
    DA.own = DA.own?.voice === c.voice ? null : { voice: c.voice, mode: c.singing === true ? "singing" : c.singing === false ? "speaking" : DA_MODE.v };
    renderVoiceCards(DA.cands);
  });
  $$("[data-own-mode]", box).forEach(b => b.onclick = () => { DA.own.mode = b.dataset.ownMode; renderVoiceCards(DA.cands); });
  $$("[data-own-go]", box).forEach(b => b.onclick = () => {
    const c = DA.cands.find(x => x.voice === DA.own?.voice); if (!c) return;
    const person = ownPerson(c);
    if (!person) return $("[data-own-name]", box)?.focus();   // never search YouTube for a file name ("Model V2 Final"): ask whose voice it is
    daBegin(person, DA.own.mode, b.dataset.ownGo === "tonight", b);
  });
  const nameIn = $("[data-own-name]", box);
  if (nameIn) nameIn.oninput = () => {   // typing the name: the buttons wake up now; the time follows once the typing stops (a redraw would take the caret)
    DA.own.name = nameIn.value;
    $$("[data-own-go]", box).forEach(b => { b.disabled = !nameIn.value.trim(); });
    clearTimeout(DA.ownTimer);
    DA.ownTimer = setTimeout(() => {
      const p = nameIn.value.trim(); if (!p || !DA.own) return;
      estimateFor("new", p, DA.own.mode).then(() => {
        if (!DA.own || DA.own.name !== nameIn.value) return;
        const at = nameIn.selectionStart; renderVoiceCards(DA.cands);
        const again = $("#da-cards [data-own-name]"); if (again) { again.focus(); again.setSelectionRange(at, at); }
      });
    }, 700);
  };
  renderDaOutcome();
}

// "Your voices" filters (basic, a row of chips): likeness word, can it speak in SpeakEasy, trained here or downloaded. A group only shows when
// the voices differ in it; pressing the pressed chip clears it. (Male / female waits for the voice print.)
const daKey = {
  like: c => c.likeness_word || "",
  speak: c => c.demo || c.cadence?.has ? "yes" : "no",   // the demo voice speaks
  src: c => c.demo ? "" : c.trained ? "trained" : "downloaded",
};
const daMatch = c => Object.keys(DA.f).every(g => !DA.f[g] || daKey[g](c) === DA.f[g]);
function daFilters() {
  const box = $("#da-filters");
  const groups = [
    ["like", "Likeness", ["great", "good", "fair", "poor"].map(w => [w, w])],
    ["speak", "Speaks", [["yes", "Can speak"], ["no", "Can't speak yet"]]],
    ["src", "From", [["trained", "Trained here"], ["downloaded", "Downloaded"]]],
  ].map(([g, label, opts]) => [g, label, opts.filter(([v]) => DA.cands.some(c => daKey[g](c) === v))]).filter(([, , opts]) => opts.length > 1);
  Object.keys(DA.f).forEach(g => { if (DA.f[g] && !groups.some(([k, , opts]) => k === g && opts.some(([v]) => v === DA.f[g]))) DA.f[g] = ""; });   // a pressed chip that's gone lapses
  box.hidden = DA.cands.length < 4 || !groups.length;
  if (box.hidden) return;
  box.innerHTML = groups.map(([g, label, opts]) => `<span class="vf-group" role="group" aria-label="${label}"><small class="meta">${label}</small>${
    opts.map(([v, text]) => `<button class="fchip" type="button" data-fg="${g}" data-fv="${v}" aria-pressed="${DA.f[g] === v}">${text}</button>`).join("")}</span>`).join("");
  $$("[data-fg]", box).forEach(b => b.onclick = () => { DA.f[b.dataset.fg] = DA.f[b.dataset.fg] === b.dataset.fv ? "" : b.dataset.fv; DA.all = false; renderVoiceCards(DA.cands); });
}
const abUrl = (voice, rest = "") => `/api/train/voices/${encodeURIComponent(voice)}/cadence${rest}`;
// which reading is A: fixed per pair (from the new pack's id), so a redraw or a second look keeps the letters
const abOrder = st => [...String(st.pending.id)].reduce((n, c) => n + c.charCodeAt(0), 0) % 2 ? ["pending", "current"] : ["current", "pending"];
const abLetter = (st, w) => "AB"[abOrder(st).indexOf(w)];
function abHtml() {
  const a = DA.ab, st = a.st;
  if (!st || !st.ready) return `<div id="cad-ab" data-voice="${esc(a.voice)}"><p class="meta">${esc(a.err || (st ? "Making a short reading each way…" : "Loading…"))}</p></div>`;
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
    <p class="meta">The one you keep is how the voice speaks from then on; the other is set aside, not deleted.</p>
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
    showError(keep === "pending" ? `${L} was the new one. ${voiceDisplay(ab.voice)} speaks that way from now on; the old one is set aside.`
                                 : `${L} was the one it had, so nothing changes. The new one is set aside.`, `Kept ${L}`, null, true);
  } catch (e) { ab.busy = false; ab.err = e.message; renderVoiceCards(DA.cands); }
}
// the two players of a blind A/B (the cadence's and an improved voice's): one at a time, one playhead, at the page's volume
function wireAbPlayers(ab) {
    let pos = 0;   // switching to the other reading carries on at the same place   // one playhead: switching to the other reading carries on at the same place
    $$(".ab-side", ab).forEach(sd => {   // one at a time, at the page's volume (muted stays silent)
      const a = $("audio", sd), bar = $(".ab-bar i", sd);
      $(".ab-play", sd).onclick = () => {
        if (!a.paused) return a.pause();
        const other = $$("audio", ab).find(o => o !== a && !o.paused);
        if (other) pos = other.currentTime;
        try { a.currentTime = Math.min(pos, a.duration || pos); } catch (e) {}
        a.play().catch(() => showError("The browser couldn't play this reading.", "Playback failed"));
      };
      a.onplay = () => {
        a.volume = VOL.muted ? 0 : VOL.level * VOL.level;
        $$("audio", ab).forEach(o => { if (o !== a) o.pause(); });
        if (DA.playing) { DA.audio.pause(); DA.playing = null; }
        sd.classList.add("playing");
      };
      a.onpause = () => { sd.classList.remove("playing"); if (!a.ended) pos = a.currentTime; };
      a.onended = () => { sd.classList.remove("playing"); pos = 0; };
      a.ontimeupdate = () => { bar.style.width = `${Math.min(100, a.currentTime / (a.duration || 1) * 100)}%`; };
    });
}

// ------------------------------------------------------------ an improved voice waiting beside the current one (IA P2-1)
// GET /api/train/voices/<v>/improved -> {improved: {voice, display, candidate, before, after, minutes, made, current_url, new_url, undo}}
// (null when nothing waits). The overnight improve never replaces a voice: the retrained one waits, the user hears both of
// the same line, blind (A / B, an order fixed per candidate), and keeps one. Keep new: the old one is set aside, never deleted.
// POST .../improved/ab (a job: the two listening copies), .../keep {keep: "new"|"current"}, .../undo.
DA.imp = {}; DA.impAb = null;   // DA.impAb: the open A/B { voice, busy, err }
const impUrl = (voice, rest = "") => `/api/train/voices/${encodeURIComponent(voice)}/improved${rest}`;
const impOrder = st => [...String(st.candidate) + String(st.made || "")].reduce((n, c) => n + c.charCodeAt(0), 0) % 2 ? ["new", "current"] : ["current", "new"];
const impLetter = (st, w) => "AB"[impOrder(st).indexOf(w)];
async function loadImproved() {
  const vs = DA.cands.filter(c => c.trained && c.can);
  const got = await Promise.all(vs.map(c => api(impUrl(c.voice)).then(r => [c.voice, r.improved], () => [c.voice, null])));
  const next = Object.fromEntries(got.filter(([, s]) => s));
  if (JSON.stringify(next) !== JSON.stringify(DA.imp)) { DA.imp = next; renderVoiceCards(DA.cands); }
}
function impHtml(voice) {
  const a = DA.impAb, st = DA.imp[voice];
  if (!st?.candidate || a?.voice !== voice) return "";
  if (!st.current_url || !st.new_url)
    return `<div id="imp-ab" data-voice="${esc(voice)}"><p class="meta">${esc(a.err || a.say || "Reading one line with both voices…")}</p></div>`;
  const side = w => `<div class="ab-side" data-side="${w}">
      <div class="ab-head"><span class="ab-letter">${impLetter(st, w)}</span></div>
      <div class="ab-player"><button class="ab-play" type="button" aria-label="Play ${impLetter(st, w)}"><svg class="ico i-play" aria-hidden="true"><use href="#g-play"/></svg><svg class="ico i-pause" aria-hidden="true"><use href="#g-pause"/></svg></button>
        <div class="ab-bar"><i></i></div></div>
      <audio preload="none" src="${esc(w === "new" ? st.new_url : st.current_url)}?v=${a.stamp}"></audio>
      <button class="btn" type="button" data-imp-keep="${w}" ${a.busy ? "disabled" : ""}>Keep ${impLetter(st, w)}</button></div>`;
  return `<div id="imp-ab" data-voice="${esc(voice)}">
    <div class="ab-q"><b>Which sounds more like them?</b><span class="meta">Both read the same line. One is ${esc(voiceDisplay(voice))} as it is, the other was retrained overnight.</span>${st.words ? `<span class="ab-words">What changed, measured: ${esc(st.words)}. Your ear decides.</span>` : ""}</div>
    <div class="ab-sides">${impOrder(st).map(side).join("")}</div>
    <small class="meta" data-deep>likeness ${st.before ?? "?"} → ${st.after ?? "?"}${st.minutes ? ` · ${Math.round(st.minutes)} min of audio` : ""}${st.made ? ` · made ${esc(st.made)}` : ""}${st.measures ? ` · ${esc(measuresText(st.measures))}` : ""}</small>
    <p class="meta">The one you keep is the voice from then on; the other is set aside, never deleted, and you can undo it.</p>
    ${a.err ? `<p class="meta">${esc(a.err)}</p>` : ""}</div>`;
}
async function openImp(voice) {
  if (DA.impAb?.voice === voice) { DA.impAb = null; return renderVoiceCards(DA.cands); }
  const a = DA.impAb = { voice, busy: false, err: null, stamp: Date.now() };   // stamp: the listening copies get remade, so a stale cached one is never played
  renderVoiceCards(DA.cands);
  const st = DA.imp[voice];
  if (st && (!st.current_url || !st.new_url)) {   // the two listening copies aren't made yet: a short job
    try {
      const { job } = await api(impUrl(voice, "/ab"), { json: {} });
      refreshJobs(true);
      const off = watchJob(job, async ev => {
        if (!ev.final) {   // behind another job: say what it waits for, not "reading one line" (IA P8-9)
          const queued = ev.state === "queued" || ev.stage === "queued" || ev.stage === "preparing";
          const other = runningJobs().find(j => j.id !== job);
          const say = !queued ? "" : (ev.label || ev.message || (other ? `Waiting for “${jobName(other)}” to finish, then it reads one line with both voices…` : "Waiting its turn on the GPU, then it reads one line with both voices…"));
          if (say !== (a.say || "")) { a.say = say; if (DA.impAb === a) renderVoiceCards(DA.cands); }
          return;
        }
        off();
        if (ev.state !== "done") a.err = plainMsg(ev.message) || "The listening copies couldn't be made.";
        else { try { DA.imp[voice] = (await api(impUrl(voice))).improved; a.stamp = Date.now(); } catch (e) { a.err = e.message; } }
        if (DA.impAb === a) renderVoiceCards(DA.cands);
      });
    } catch (e) { a.err = e.message; renderVoiceCards(DA.cands); }
  }
}
async function keepImp(keep) {
  const a = DA.impAb, st = a && DA.imp[a.voice];
  if (!a || a.busy || !st) return;
  a.busy = true; a.err = null; renderVoiceCards(DA.cands);
  try {
    await api(impUrl(a.voice, "/keep"), { json: { keep } });
    const L = impLetter(st, keep);   // blind until now: say which it was
    DA.impAb = null; loadCandidates(); loadImproved(); loadVoices();
    showError(keep === "new" ? `${L} was the retrained voice. ${voiceDisplay(a.voice)} is the new one now; the old one is set aside, not deleted.`
                             : `${L} was the voice you had, so nothing changes. The retrained one is set aside, not deleted.`, `Kept ${L}`, null, true);
  } catch (e) { a.busy = false; a.err = e.message; renderVoiceCards(DA.cands); }
}
async function undoImp(voice) {
  try {
    await api(impUrl(voice, "/undo"), { json: {} });
    DA.impAb = null; loadCandidates(); loadImproved(); loadVoices();
    showError("Back to how it was before that choice.", "Undone", null, true);
  } catch (e) { showError(e.message, "Couldn't undo it"); }
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
// "Train the voice · about …": the estimator's own time for this kind (and the cadence a speaking voice gets), not a fixed hour
function daEta() {
  const p = $("#da-person").value.trim(), words = estWords("new", p, DA_MODE.v);
  if (words) $("#da-train-eta").textContent = words;
  else if (!EST.asked.has(estKey("new", p, DA_MODE.v))) estimateFor("new", p, DA_MODE.v).then(daEta);
}
$("#da-person").addEventListener("input", () => { clearTimeout(DA.etaTimer); DA.etaTimer = setTimeout(daEta, 600); });
daEta();
$("#da-tonight").onclick = () => daStart(true);
// "Let it speak": teaches the voice how the person talks (a cadence), for SpeakEasy. It uses the GPU, so the card says when and for how long first.
function cadHtml(c) {
  const eta = estWords("cadence", c.voice);
  if (!eta && !EST.asked.has(estKey("cadence", c.voice, null))) estimateFor("cadence", c.voice).then(() => { if (DA.cad) renderVoiceCards(DA.cands); });
  return `<div class="vc-own"><p class="meta">${esc(voiceDisplay(c.voice))} learns how ${esc(c.person ? "they" : "the person")} talk${c.person ? "" : "s"}: their pace and pauses, so SpeakEasy reads like them. It transcribes clips and measures them${c.cadence?.why ? `; ${esc(String(c.cadence.why).replace(/^./, m => m.toLowerCase()))}` : ""}.</p>
    <div class="row"><button class="btn primary" type="button" data-cad-go="now">Learn now${eta ? ` · ${eta}` : ""}</button><button class="btn" type="button" data-cad-go="tonight">Tonight</button></div></div>`;
}
// "Train my own" on a downloaded voice's card: the choice there, Now or Tonight; Speaking / Singing from the voice where known
// the person to search for: the voice's known (or guessed) person, else the name typed on the card. Never the voice's display name: for a downloaded file
// that's often "Model V2 Final", and UnderCover would search YouTube for it (RC pass 10, B).
const ownPerson = c => (c.person || (DA.own?.voice === c.voice ? DA.own.name || "" : "")).trim();
function ownHtml(c) {
  const person = ownPerson(c), m = DA.own.mode, eta = person ? estWords("new", person, m) : "";
  if (person && !eta && !EST.asked.has(estKey("new", person, m))) estimateFor("new", person, m).then(() => { if (DA.own) renderVoiceCards(DA.cands); });
  const ask = !c.person;
  return `<div class="vc-own">${ask ? `<label class="own-who"><span>Whose voice is this?</span><input type="text" data-own-name autocomplete="off" value="${esc(DA.own.name || "")}" placeholder="their name, e.g. Barack Obama" aria-label="Whose voice is this"></label>
    <p class="meta">UnderCover finds clips of them and trains your own voice of them, to compare with this one.</p>`
    : `<p class="meta">Train your own <b>${esc(person)}</b> voice to compare with this one: UnderCover finds clips of them and trains it${c.singing == null ? "" : ` (${c.singing ? "singing" : "speaking"}, like this one)`}.</p>`}
    <div class="seg" role="group" aria-label="Kind of audio"><button type="button" data-own-mode="speaking" aria-pressed="${m === "speaking"}">Speaking</button><button type="button" data-own-mode="singing" aria-pressed="${m === "singing"}">Singing</button></div>
    <div class="row"><button class="btn primary" type="button" data-own-go="now"${person ? "" : " disabled"}>Train now${eta ? ` · ${eta}` : ""}</button><button class="btn" type="button" data-own-go="tonight"${person ? "" : " disabled"}>Tonight</button></div></div>`;
}
async function daStart(tonight) {
  const person = $("#da-person").value.trim();
  if (!person) return $("#da-person").focus();
  if (await daBegin(person, DA_MODE.v, tonight, tonight ? $("#da-tonight") : $("#da-train"))) $("#da-person").value = "";
}
async function daBegin(person, mode, tonight, btn) {
  const label = btn.innerHTML;
  btn.disabled = true; btn.textContent = tonight ? "Adding…" : "Finding clips…";
  let ok = false;
  try {
    const r = await trainVoice(person, mode, tonight);   // train.js
    ok = true; DA.own = null; renderVoiceCards(DA.cands);
    const first = !personWarned(); if (first) store.set("uc-person-warned", true);   // the one calm line, once, with this toast (consult 6)
    if (tonight) {
      if (!workMoved(r)) showError(`${person} is in tonight's work.${startsNote(r) || " It trains while the PC is free."} SafeHouse's Running room shows it.${first ? ` ${PERSON_WARN}` : ""}`, "Added to tonight", null, true);
      else if (first) showError(PERSON_WARN, "One thing", null, true);
      document.dispatchEvent(new CustomEvent("uc:voicequeued", { detail: { person } }));   // the First assignment ends on it
    }
    else { daOutcomeClear(); renderDaWork(); if (first) showError(PERSON_WARN, "One thing", null, true); }
  } catch (e) { showError(e.message, tonight ? "Couldn't add it to tonight" : "Couldn't start the voice"); }
  finally { btn.disabled = false; btn.innerHTML = label; }
  return ok;
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
  // the pause has landed: the job isn't running any more, but it isn't gone. What waits is the held work (the resume list) and the queue's paused items.
  const w = W.work || {}, held = j ? [] : [...(w.held ? (w.resume || []).map(r => r.label ?? r) : []),
    ...(W.last?.queue || []).filter(i => i.state === "paused").map(i => jobWhat(i).replace(/&amp;/g, "&"))];
  box.hidden = !j && !held.length;
  if (!j && held.length) {
    if (!put(box, `<div class="who">Paused</div><div class="da-prog"><span><b>${esc([...new Set(held)].slice(0, 2).join(", "))}${held.length > 2 ? ` and ${held.length - 2} more` : ""}</b> stopped where it was; Resume picks it up.</span></div>
      <div class="row"><button class="btn primary" type="button" data-da-resume>Resume now</button></div>`)) return;
    $$("[data-da-resume]", box).forEach(b => b.onclick = async () => { b.disabled = true; await resumeWork(); b.disabled = false; });
    return;
  }
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
      DA.stopAsk = id; DA.stopAskAt = Date.now(); DA.stopTimer = setTimeout(() => { DA.stopAsk = null; renderDaWork(); }, 4000);
      return renderDaWork();
    }
    if (Date.now() - (DA.stopAskAt || 0) < CONFIRM_MS) { DA.stopTimer = setTimeout(() => { DA.stopAsk = null; renderDaWork(); }, 4000); return; }   // a double click isn't a confirmation
    DA.stopAsk = null; b.disabled = true; b.textContent = "Stopping…";
    try { await api(`/api/jobs/${id}/cancel`, { method: "POST" }); } catch (e) { showError(e.message, "Couldn't stop it"); }
    refreshJobs(true);
  });
}
onJobs(() => { if (!$("#view-train").hidden) renderDaWork(); });

// ------------------------------------------------------------ after training: one card, ready (next steps) or weak (one way out)
// any Do it all that finishes while the page is open (this tab's, another's, the phone's): its project names the voice
function daOutcome(voice) { store.set("uc-da-outcome", voice); loadCandidates(); }   // the card draws once the voice is in the list
document.addEventListener("uc:jobdone", async e => {
  const j = e.detail;
  if (j.kind !== "train-auto" || j.state !== "done") return;
  if (j.voice) return daOutcome(j.voice);   // the job names the voice it made
  if (!j.project) return;
  try {   // an older server: its project does
    const proj = (await api("/api/train/projects")).find(p => p.name === j.project);
    if (proj?.train?.exported) daOutcome(proj.train.exported);
  } catch (err) { /* the voice is in the list anyway; only the card is missed */ }
});
function daOutcomeClear() { store.set("uc-da-outcome", null); $("#da-outcome").hidden = true; }
function renderDaOutcome() {
  const v = store.get("uc-da-outcome", null), box = $("#da-outcome");
  const c = v ? DA.cands.find(x => x.voice === v) : null;
  box.hidden = !c;
  if (!c) return;
  const name = esc(voiceDisplay(v)), w = LIKE_WORDS.includes(c.likeness_word) ? c.likeness_word : null;
  const weak = w === "fair" || w === "poor" || c.quality?.ok === false;
  const mins = c.minutes != null ? Math.round(c.minutes) : null;
  if (weak && !EST.asked.has(estKey("improve", v))) estimateFor("improve", v).then(() => renderDaOutcome());   // the time fills in
  box.innerHTML = weak
    ? `<h3>${name} is ready${w ? `, but only a ${likeWord(w)} likeness` : ", but it's weak"}.</h3>
       <p class="meta">${mins != null && mins < 10 ? `Only ${mins} minute${mins === 1 ? "" : "s"} of their clean voice were found; 10 is the aim. ` : ""}It sounds somewhat like them. More clips would make it good, or great.</p>
       <div class="row"><button class="btn primary" type="button" data-da-more>Find more clips and keep training</button><span class="meta">${estWords("improve", v)}</span>
         <button class="linkbtn quiet" type="button" data-da-close>Use it as it is</button></div>`
    : `<h3>${name} is ready${w ? `: a ${likeWord(w)} likeness` : ""}.</h3>
       <p class="meta">${mins ? `Trained from ${mins} minutes of their voice, checked against real clips. ` : ""}It's in your voices now.</p>
       <div class="row"><button class="btn primary" type="button" data-da-use>Use it in FrontMan</button>
         ${c.cadence?.can && !c.cadence?.has && !c.cadence?.queue ? `<button class="btn" type="button" data-da-speak>Let it speak in SpeakEasy</button>` : ""}
         ${c.can && !c.queued ? `<button class="linkbtn" type="button" data-da-tonight>Improve it tonight</button>` : ""}
         <button class="linkbtn quiet" type="button" data-da-close>Close</button></div>`;
  const add = async (b, body, done) => {
    b.disabled = true;
    try { W.last = await api("/api/work/add", { json: body }); renderQueue(); refreshJobs(true); daOutcomeClear(); loadCandidates(); if (!workMoved(W.last) && done) showError(done + startsNote(W.last), "On it", null, true); }
    catch (e) { b.disabled = false; showError(e.message, "Couldn't start it"); }
  };
  $$("[data-da-close]", box).forEach(b => b.onclick = daOutcomeClear);
  $$("[data-da-use]", box).forEach(b => b.onclick = async () => { daOutcomeClear(); await loadVoices(v); showTab("frontman"); window.scrollTo({ top: 0, behavior: "smooth" }); });
  $$("[data-da-more]", box).forEach(b => b.onclick = () => add(b, { kind: c.kind || "improve", target: c.target || v, now: true }, `Finding more clips of ${voiceDisplay(v)} and training again. The bar at the top shows how far.`));
  $$("[data-da-speak]", box).forEach(b => b.onclick = () => add(b, { kind: "cadence", target: v, now: true }, `${voiceDisplay(v)} is learning to speak. It shows in SpeakEasy when it's done.`));
  $$("[data-da-tonight]", box).forEach(b => b.onclick = () => add(b, { kind: c.kind || "improve", target: c.target || v }, `${voiceDisplay(v)} improves tonight.`));
}
document.addEventListener("uc:module", e => { if (e.detail === "doubleagent") { renderDaWork(); renderDaOutcome(); } });
