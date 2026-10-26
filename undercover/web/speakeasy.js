"use strict";
// SpeakEasy (the maintainers' mockup, speakeasy): text -> pick a voice -> Generate. Speech is made sentence by sentence and
// "Now speaking" plays each one as soon as it's ready (played / ready / striped = still being made). Recent lists saved
// speech (SafeHouse has them all). Listens for the shell's "uc:module" event. Uses helpers from app.js ($, $$, esc, api,
// showError, store, fmtTime, VOL), jobs.js (watchJob), train.js (newVoice) and shell.js (showTab, via the event's timing).

const SE = { voices: [], readers: [], voice: store.get("uc-se-voice", null), plan: null, recent: [], loaded: false,
             job: null, es: null, sents: [], parts: [], cur: -1, waiting: false, done: false, result: null, started: false,
             audio: new Audio(), rec: new Audio(), recId: null, visible: false };
const SE_GROUPS = { af: "American women", bf: "British women", am: "American men", bm: "British men" };

// ------------------------------------------------------------ show / hide
document.addEventListener("uc:module", e => {
  SE.visible = e.detail === "speakeasy";
  $("#drop-overlay > div").textContent = SE.visible ? "Drop a .txt file to read it out"
    : "Drop a song to load it, or a voice (.ucvoice, .zip or .pth) to install it";
  if (SE.visible) { seLoad(); api("/api/speak/warm", { json: {} }).catch(() => {}); }   // (its engine warmed while idle: 1.0.1)
  else { SE.rec.pause(); seRecIcons(); }   // the speech being made keeps playing only on this page
  if (!SE.visible && !SE.audio.paused) { SE.audio.pause(); sePlayIcon(); }
});
async function seLoad() {
  const first = !SE.loaded;
  SE.loaded = true;
  try {
    const [voices, readers] = await Promise.all([api("/api/speak/voices"), first ? api("/api/speak/readers") : SE.readers]);
    SE.voices = voices; SE.readers = readers; noteVoices(voices);
  } catch (e) { $("#se-voices").innerHTML = `<p class="meta">${esc(e.message)}</p>`; return; }
  if (first) seReaders();
  if (!SE.voices.some(v => v.name === SE.voice)) SE.voice = (SE.voices.find(v => v.trained && !v.undertrained) || SE.voices[0] || {}).name || null;
  seVoices(); sePlan(); seLoadRecent();
}

// ------------------------------------------------------------ 1 · text
const seText = $("#se-text");
seText.value = store.get("uc-se-text", "");
function seWords() { return (seText.value.match(/[\w'’-]+/g) || []).length; }
function seCount() {
  const w = seWords(), secs = w / 3.0 / sePaceFactor();
  $("#se-count").textContent = w ? `${w.toLocaleString()} word${w === 1 ? "" : "s"} · about ${fmtTime(Math.max(1, secs))}` : "0 words";
  seSummary();
  seEstimate();
}
// "Ready in about …" before Generate: the server's own estimate (GET/POST /api/speak/estimate: the engine's measured speed, the
// listen-back, a cold start, and how many jobs wait), asked a moment after the text, voice, Reader, pace, nudge or cadence changes
let seEstTimer = null, seEstSeq = 0;
const seEstKey = () => [SE.voice, $("#se-reader").value, SE.pace, $("#se-nudge").value, $("#se-cad").checked ? 1 : 0, seText.value.trim()].join("\u0001");
function seEstimate() {
  clearTimeout(seEstTimer);
  const text = seText.value.trim();
  if (!text || !SE.voice) { SE.est = null; return; }
  seEstTimer = setTimeout(async () => {
    const key = seEstKey(), seq = ++seEstSeq;
    try {
      const r = await api("/api/speak/estimate", { json: { text, voice: SE.voice, reader: $("#se-reader").value || "auto", pace: SE.pace,
                                                          nudge: +$("#se-nudge").value, mode: $("#se-cad").checked ? undefined : "reader" } });
      if (seq === seEstSeq) { SE.est = { ...r, key }; seSummary(); }
    } catch (e) { /* the page's own guess stands */ }
  }, 600);
}
let seSave = null;
seText.addEventListener("input", () => { seCount(); clearTimeout(seSave); seSave = setTimeout(() => store.set("uc-se-text", seText.value.slice(0, 200000)), 400); });
$("#se-clear").onclick = () => { seText.value = ""; store.set("uc-se-text", ""); seCount(); seText.focus(); };
window.speakDrop = async files => {   // app.js hands file drops to this page while it's shown
  const f = files.find(x => /\.(txt|md|text)$/i.test(x.name) || x.type.startsWith("text/"));
  if (!f) return showError("SpeakEasy reads plain text files (.txt). Paste anything else into the box.", "Not a text file", null, true);
  if (f.size > 2e6) return showError("That file is over 2 MB of text; split it into parts.", "Too long");
  seText.value = (await f.text()).trim(); store.set("uc-se-text", seText.value.slice(0, 200000)); seCount();
seLoadRecent();   // the WinAmp skin walks speech too, on any page
};

// ------------------------------------------------------------ 2 · voice
function seVoices() {
  const q = $("#se-q").value.trim().toLowerCase();
  const list = SE.voices.filter(v => !q || `${v.name} ${v.person || ""} ${voiceDisplay(v.name)}`.toLowerCase().includes(q));
  $("#se-vcount").textContent = `${SE.voices.length} voice${SE.voices.length === 1 ? "" : "s"}`;
  const row = v => {
    const bits = v.demo ? ["Demo voice", { t: "Not a real person", muted: true }] : v.trained ? [upFirst(v.kind), v.undertrained ? { t: "Under-trained", muted: true } : ""]
                           : ["Downloaded", v.person ? { t: `Checked: ${v.person}`, muted: true } : "", v.kind === "singing" ? { t: "Singing", muted: true } : ""];
    const c = v.cadence || {};
    const cad = c.has ? `<span class="y">Speaks</span>` : c.queue === "running" ? "Learning to speak" : c.queue ? "Learns to speak tonight" : "Can't speak yet";
    const act = c.has || c.queue || !c.can ? ""
      : `<button class="mini" type="button" data-cad="${esc(v.name)}" title="${esc(c.why || "")}">Let it speak</button>`;
    return `<div class="voice" role="option" tabindex="0" data-v="${esc(v.name)}" aria-label="${esc(voiceDisplay(v.name))}" aria-selected="${v.name === SE.voice}">
      <div class="avatar" aria-hidden="true">${vinitial(v.name)}</div>
      <div class="vtext"><b>${vname(v.name)}</b>${facts([{ html: `<span class="se-cad">${cad}</span>` }, ...bits, v.trained && v.likeness != null ? { t: `likeness ${(+v.likeness).toFixed(2)}`, deep: true } : ""])}</div>
      <div class="se-vact">${voiceLineButton(v.name)}${act}</div></div>`;
  };
  const trained = list.filter(v => v.trained), dl = list.filter(v => !v.trained);
  $("#se-voices").innerHTML = (trained.length ? `<div class="se-vgroup">Trained in DoubleAgent</div>` + trained.map(row).join("") : "")
    + (dl.length ? `<div class="se-vgroup">Downloaded (they learn to speak from clips first)</div>` + dl.map(row).join("") : "")
    || (SE.voices.length ? `<p class="meta">No voice matches “${esc(q)}”.</p>` : `<div class="empty"><b>No one to speak yet.</b><button class="btn" type="button" data-goto="doubleagent">Get a voice</button></div>`);
  $$("#se-voices [data-cad]").forEach(b => b.onclick = async () => {
    b.disabled = true; b.textContent = "Starting…";
    try { await api("/api/speak/cadence", { json: { voice: b.dataset.cad, now: true } }); }
    catch (e) { b.disabled = false; return showError(e.message, "Couldn't start it"); }
    seLoad(); if (typeof loadQueue === "function") loadQueue();
  });
  $$("#se-voices .voice").forEach(el => {
    const pick = () => { SE.voice = el.dataset.v; store.set("uc-se-voice", SE.voice); $$("#se-voices .voice").forEach(x => x.setAttribute("aria-selected", x === el)); sePlan(); };
    el.addEventListener("click", e => { if (!e.target.closest("button")) pick(); });
    el.addEventListener("keydown", e => { if (e.target.closest(".vline")) return; if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(); } });
  });
  bindVoiceLines($("#se-voices"));
}
$("#se-q").addEventListener("input", seVoices);
async function seNewVoice(now) {
  const name = $("#se-newname").value.trim();
  if (!name) return $("#se-newname").focus();
  let r;
  try { r = await api("/api/speak/newvoice", { json: { person: name, now } }); }
  catch (e) { return showError(e.message, "Couldn't start the new voice"); }
  $("#se-newname").value = "";
  const eta = (await estimateFor("new", name, "speaking"))?.total, about = eta ? etaWords({ low: eta * 60, high: eta * 60 }) : "a few hours";
  $("#se-newnote").textContent = now ? `DoubleAgent is on it: ${name}'s voice, and how they talk (${about}; the bar at the top shows how far). It appears here when it's done.`
    : `${name} is in tonight's queue (${about}).${startsNote(r) || " It runs at the queue's start time."} SafeHouse's Running room shows it.`;
  if (typeof loadQueue === "function") loadQueue();
  refreshJobs(true);
}
estimateFor("new", "", "speaking").then(e => { if (e?.total && $("#se-new-eta")) { $("#se-new-eta").textContent = etaWords({ low: e.total * 60, high: e.total * 60 }).replace(/^about /, ""); $("#se-new-eta-wrap").hidden = false; } });   // the time in the note under the box
$("#se-newgo").onclick = () => seNewVoice(true);
$("#se-newnight").onclick = () => seNewVoice(false);
$("#se-newsteps").onclick = () => {
  const name = $("#se-newname").value.trim();
  showTab("doubleagent");   // DoubleAgent's line, the name filled in
  if (name) $("#da-person").value = name;
  $("#da-start").scrollIntoView({ behavior: "smooth", block: "start" });
  $("#da-person").focus({ preventScroll: true });
};
$("#se-newname").addEventListener("keydown", e => { if (e.key === "Enter") seNewVoice(true); });

// ------------------------------------------------------------ 3 · how
function seReaders() {
  const groups = {};
  for (const r of SE.readers) (groups[r.id.slice(0, 2)] ||= []).push(r);
  $("#se-reader").innerHTML = `<option value="auto">Auto: picked from the voice's pitch</option>` + Object.entries(SE_GROUPS).filter(([k]) => groups[k])
    .map(([k, label]) => `<optgroup label="${label}">${groups[k].map(r => `<option value="${r.id}">${esc(r.id)} · ${esc(r.label.replace(/^(American|British) (wo)?man,? ?/, "") || "")} ${Math.round(r.hz)} Hz</option>`).join("")}</optgroup>`).join("");
  $("#se-reader").value = store.get("uc-se-reader", "auto");
  if (!$("#se-reader").value) $("#se-reader").value = "auto";
}
// Pause between sentences (phase 4c made it "pace"; phase 23: Turbo paces itself, so the control is Deep Cover's, "Shorter /
// Natural / Longer", values still faster / natural / slower for the API): made that way, never stretched afterwards
SE.pace = store.get("uc-se-pace", "natural");
const sePace = () => document.body.classList.contains("deep") ? SE.pace : "natural";   // out of Deep Cover: Natural, whatever was left on
const sePaceFactor = () => ({ slower: 0.87, natural: 1, faster: 1.12 })[sePace()] || 1;   // for the length estimate
function sePaceShow() { $$("#se-pace button").forEach(b => b.setAttribute("aria-pressed", b.dataset.p === SE.pace)); }
$$("#se-pace button").forEach(b => b.onclick = () => { SE.pace = b.dataset.p; store.set("uc-se-pace", SE.pace); sePaceShow(); seCount(); sePlan(); });
sePaceShow();
$("#se-reader").onchange = () => { store.set("uc-se-reader", $("#se-reader").value); sePlan(); };
$("#se-nudge").addEventListener("input", () => sePlan(true));

let sePlanTimer = null, sePlanSeq = 0;
function sePlan(soon) {
  clearTimeout(sePlanTimer);
  sePlanTimer = setTimeout(async () => {
    if (!SE.voice) { SE.plan = null; seSummary(); return; }
    const seq = ++sePlanSeq;
    const qs = new URLSearchParams({ voice: SE.voice, reader: $("#se-reader").value || "auto", nudge: $("#se-nudge").value,
                                     cadence: $("#se-cad").checked ? "1" : "0", pace: sePace() });
    try { const p = await api(`/api/speak/plan?${qs}`); if (seq === sePlanSeq) { SE.plan = p; seShowPlan(); } }
    catch (e) { if (seq === sePlanSeq) { SE.plan = null; seSummary(); } }
  }, soon ? 120 : 0);
}
// "Speak the way Morgan Freeman really does" (a name), "the way they really do" (none)
function seCadName(name) { $("#se-cad-name").textContent = name || "they"; $("#se-cad-verb").textContent = name ? "does" : "do"; $("#se-cad").setAttribute("aria-label", `Speak the way ${name || "they"} really ${name ? "does" : "do"}`); }
$("#se-cad").checked = store.get("uc-se-cad", true);
$("#se-cad").onchange = () => { store.set("uc-se-cad", $("#se-cad").checked); sePlan(); };
// Deep Cover's engine line under "Speak the way they really do": the engine that reads, and the original Chatterbox (an optional add-on since
// Turbo became the default) offered where it can be added, with its size. Nothing downloads from here: SafeHouse > Add-ons installs it.
async function seEngineLine(name, id) {
  const el = $("#se-engine-deep");
  if (!name) { el.hidden = true; return; }
  if (!SE.addons) try { SE.addons = (await api("/api/addons")).addons; } catch (e) { SE.addons = []; }
  const cb = SE.addons.find(a => a.id === "chatterbox"), go = `<a class="linkbtn" href="#safehouse/addons">SafeHouse › Add-ons</a>`;
  el.innerHTML = `Engine: ${esc(name)}.` + (!cb || id === "chatterbox" ? "" : !cb.installed ? ` The original Chatterbox is an add-on (${gb(cb.size_mb * 1e6)}) in ${go}.`
    : !cb.in_use ? ` The original Chatterbox is installed: switch to it in ${go}.` : "");
  el.hidden = false;
}
function seShowPlan() {
  seEstimate();
  const p = SE.plan, st = v => `${v > 0 ? "+" : v < 0 ? "−" : "±"}${Math.abs(v)} st`;
  const v0 = SE.voices.find(x => x.name === p.voice), c = v0?.cadence || {};
  const cadOn = p.mode === "cadence";
  $("#se-cad").disabled = !c.has;
  $("#se-cad-field").classList.toggle("off", !c.has);
  $("#se-cad-note").textContent = c.has ? (cadOn ? "Their pace and pauses, from their own clips."
      : "Off: the quick built-in Reader speaks, then the voice makes it sound like them.")
    : c.queue ? "They're learning to speak (DoubleAgent); it's used as soon as it's ready."
    : c.can ? `Their own pace and pauses, learned from their clips. "Let it speak" on the voice makes it (minutes).` : (c.why || "Not available for this voice.");
  $("#se-cad-deep").textContent = c.has ? `${p.engine_name || c.engine} · from ${c.data_minutes ?? "?"} min of speech · likeness ${c.scores?.likeness ?? "?"} · cadence gap ${c.scores?.cadence_gap ?? "?"} · gap between sentences ${c.gap_s ?? "?"} s · tempo ${c.tempo ?? 1}` : "";
  seEngineLine(c.has ? p.engine_name || c.engine : "", p.engine || c.engine);
  $("#se-reader-field").hidden = cadOn;
  $("#se-pace-deep").textContent = cadOn ? `${p.pace}: gap between sentences ${p.gap_s} s${p.engine === "chatterbox" ? ` · cfg_weight ${p.cfg_weight} · exaggeration ${p.exaggeration}` : ""} · no stretching`
    : `${p.pace}: the Reader reads at ${p.speed}× · pauses ×${p.gap_scale}`;
  if (cadOn) {
    $("#se-nudge-out").textContent = p.nudge ? st(p.nudge) : "their own";
    $("#se-pitch-note").textContent = "Speaking their own way, they use their own pitch. Nudge it only if it sounds off.";
    seCadName(v0?.person || voiceDisplay(p.voice));
    return seSummary();
  }
  $("#se-reader-out").textContent = `${p.auto ? "auto · " : ""}${p.reader}`;
  $("#se-nudge-out").textContent = p.nudge ? `${st(p.pitch)} (auto ${st(p.pitch - p.nudge)})` : `auto · ${st(p.pitch)}`;
  $("#se-pitch-note").textContent = p.target_hz
    ? `Speaks around ${Math.round(p.target_hz)} Hz (from ${p.pitch_from}); the Reader is at ${Math.round(p.reader_hz)} Hz. Nudge it if they sound too high or low.`
    : "This voice's speaking pitch isn't known (a downloaded voice nobody checked), so the Reader's own pitch is used. Nudge it, or pick another Reader, if it sounds off.";
  const v = SE.voices.find(x => x.name === p.voice);
  seCadName(v?.person || voiceDisplay(p.voice));
  seSummary();
}
function seSummary() {
  const w = seWords(), busy = SE.job && !SE.done;
  $("#se-generate").disabled = !w || !SE.voice || !!busy;
  $("#se-generate").textContent = busy ? "Speaking…" : "Generate";
  if (!SE.voice) { $("#se-summary").textContent = "Pick a voice first."; return; }
  const est = SE.est && SE.est.key === seEstKey() ? SE.est : null;   // the server's, while it still matches what's on the page
  const secs = est ? est.audio_seconds : w / 3.0 / sePaceFactor();
  const cad = est ? est.mode === "cadence" : SE.plan?.mode === "cadence" && SE.plan.voice === SE.voice;
  const first = est && est.first_s >= 5 ? `, ${fmtTime(est.first_s)} of it because ${esc(est.first_why)} (the first speech only)` : "";   // (1.0.1)
  const ready = est ? ` · ready in about ${fmtTime(Math.max(1, est.estimate))}${first}${est.waiting ? ` (${esc(est.waiting)} ahead in the queue)` : ""}`
    : cad ? ` · ready in about ${fmtTime(Math.max(5, secs / (SE.plan.rtf || 0.55)))}` : "";
  const when = `${cad ? "the way they really do" : "read by the Reader"}${ready} · ready to play after the ${cad ? "first few sentences" : "first sentence"}`;
  $("#se-summary").innerHTML = w ? `<b>${vname(SE.voice)}</b> reads ${w.toLocaleString()} word${w === 1 ? "" : "s"} <span class="meta">· about ${fmtTime(Math.max(1, secs))} of speech · ${when}</span>`
    : `<b>${vname(SE.voice)}</b> <span class="meta">· type or paste something for them to say</span>`;
}

// ------------------------------------------------------------ Generate + Now speaking
$("#se-generate").onclick = async () => {
  const text = seText.value.trim();
  if (!text || !SE.voice) return;
  $("#se-generate").disabled = true;
  let r;
  try { r = await api("/api/speak", { json: { text, voice: SE.voice, reader: $("#se-reader").value, pace: sePace(), nudge: +$("#se-nudge").value,
                                               cadence: $("#se-cad").checked ? null : false } }); }
  catch (e) { seSummary(); return showError(e.message, "SpeakEasy couldn't start"); }
  seStart(r.job, r.plan, r.estimate);
};
// Nothing plays by itself (the user, rc-page11): once enough is made to play through, the player is ready and says so; the user presses play.
// SE.started = "ready to play". resumed: a speech that was being made when the page reloaded, shown again (it never played by itself either).
function seStart(job, plan, estimate, resumed = false) {
  if (SE.es) SE.es();
  SE.audio.pause(); SE.rec.pause(); seRecIcons();
  Object.assign(SE, { job, sents: [], parts: [], cur: -1, waiting: false, done: false, result: null, started: false, estimate, jobPlan: plan,
                      startAfter: 1 });
  $("#se-now").hidden = false;
  if (typeof earShow === "function") earShow($("#se-ear"), "speech", null);   // a new reading: the last one's thumbs go
  $("#se-track").classList.remove("done");
  for (const id of ["#se-wav", "#se-mp3"]) { $(id).setAttribute("aria-disabled", "true"); $(id).removeAttribute("href"); }
  $("#se-dl-note").textContent = "downloads unlock when the last sentence is made";
  $("#se-stop").hidden = false; $("#se-stop").textContent = "Stop";
  $("#se-progress").textContent = "starting…";
  $("#se-reading").innerHTML = `<span class="pending">Getting ready…</span>`;
  seSummary(); seTrack();
  $("#se-now").scrollIntoView({ block: "nearest", behavior: "smooth" });
  const stop = SE.es = watchJob(job, ev => {   // jobs.js: the live stream, or progress polled while it's quiet
    if (ev.synthetic) {   // a summary while the stream is quiet: no sentence files in it, only how far along
      if (ev.of) $("#se-progress").textContent = `sentence ${(ev.index ?? -1) + 1} of ${ev.of} ready`;
      return;
    }
    if (ev.sentences && !SE.sents.length) { SE.sents = ev.sentences; seReading(); }
    if (ev.start_after) SE.startAfter = ev.start_after;
    if (ev.total_left != null) { SE.left = ev.total_left; seTrack(); }
    if (ev.stage === "reading") $("#se-progress").textContent = ev.message.toLowerCase();
    if (ev.stage === "speaking") {
      SE.parts[ev.index] = { url: ev.url, seconds: ev.seconds };
      SE.left = ev.total_left ?? ev.left;   // (the job's own, held to its count-down rule: rc-fix10)
      seReading();
      const ready = SE.parts.filter(Boolean).length, last = ev.index + 1 >= ev.of;
      if (!SE.started && (ready >= SE.startAfter || last)) SE.started = true;   // enough to play through: ready, never played by itself
      $("#se-progress").textContent = `sentence ${ev.index + 1} of ${ev.of} ready` + (!SE.started ? ` · ready to play after sentence ${SE.startAfter}`
        : SE.cur < 0 && SE.audio.paused && !last ? " · ready to play: the rest is still being made" : "");
      if (SE.waiting && SE.parts[SE.cur + 1]) { SE.waiting = false; sePlayPart(SE.cur + 1); }   // playing (the user pressed play) caught up: carries on
      sePlayIcon(); seTrack();
    }
    if (ev.final) {
      stop(); SE.es = null; SE.done = true;
      $("#se-stop").hidden = true;
      if (ev.state === "done" && ev.result) {
        SE.result = ev.result;
        $("#se-progress").innerHTML = `${SE.sents.length} sentence${SE.sents.length === 1 ? "" : "s"}<span data-deep> · made in ${(ev.result.seconds?.total || 0).toFixed(1)} s</span>`;
        $("#se-wav").href = ev.result.url + "?download"; $("#se-wav").removeAttribute("aria-disabled");
        $("#se-wav").textContent = `Download ${(ev.result.file || "").split(".").pop().toUpperCase() || "FLAC"}`;   // phase 24: lossless FLAC
        if (ev.result.mp3_url) { $("#se-mp3").href = ev.result.mp3_url + "?download"; $("#se-mp3").removeAttribute("aria-disabled"); }
        $("#se-dl-note").textContent = `saved as “${ev.result.file}” (SafeHouse → Speech)`;
        $("#se-track").classList.add("done");
        seKeepShow();
        seLoadRecent();
      } else {
        $("#se-progress").textContent = ev.state === "cancelled" ? "stopped" : "failed";
        $("#se-dl-note").textContent = ev.state === "cancelled" ? "stopped before the end, so nothing was saved" : "";
        if (ev.state === "error") showError(ev.message, "SpeakEasy couldn't finish");
        if (SE.waiting) { SE.waiting = false; sePlayIcon(); }
      }
      seSummary(); seTrack(); seReading();
    }
  });
}
$("#se-stop").onclick = async () => {
  SE.audio.pause(); SE.waiting = false; sePlayIcon();
  if (SE.job && !SE.done) { $("#se-stop").textContent = "Stopping…"; SE.left = null; seTrack(); try { await api(`/api/jobs/${SE.job}/cancel`, { json: {} }); } catch (e) {} }   // (no "about 10 min to make" after a Stop: rc-fix10)
};

function seReading() {
  const box = $("#se-reading");
  if (!SE.sents.length) return;
  box.innerHTML = SE.sents.map((s, i) => {
    const ready = !!SE.parts[i];
    return i === SE.cur ? `<mark data-i="${i}">${esc(s)}</mark>` : `<span data-i="${i}" class="${ready ? "ready" : SE.done ? "" : "pending"}">${esc(s)}</span>`;
  }).join(" ");
  $$("[data-i]", box).forEach(el => el.onclick = () => { const i = +el.dataset.i; if (SE.parts[i]) sePlayPart(i); });
  box.querySelector("mark")?.scrollIntoView({ block: "nearest" });
}
function seVol(a) { a.volume = VOL.muted ? 0 : VOL.level; a.muted = VOL.muted; }
function sePlayPart(i, offset = 0) {
  const p = SE.parts[i]; if (!p) return;
  SE.rec.pause(); seRecIcons();
  SE.cur = i; SE.waiting = false;
  SE.audio.src = p.url; seVol(SE.audio);
  if (offset) SE.audio.addEventListener("loadedmetadata", () => { try { SE.audio.currentTime = offset; } catch (e) {} }, { once: true });
  SE.audio.play().catch(() => {});
  sePlayIcon(); seReading(); seTrack();
}
SE.audio.addEventListener("ended", () => {
  if (SE.parts[SE.cur + 1]) sePlayPart(SE.cur + 1);
  else if (!SE.done) { SE.waiting = true; sePlayIcon(); }       // caught up with the making: carries on when the next is ready
  else { SE.cur = -1; sePlayIcon(); seReading(); seTrack(); }  // the end
});
SE.audio.addEventListener("timeupdate", () => seTrack());
function sePlayIcon() {
  const on = !SE.audio.paused || SE.waiting, fresh = SE.started && SE.cur < 0 && !on && !!SE.parts[0];   // ready and not played yet: the button says so
  $("#se-play").textContent = on ? "❚❚" : "▶"; $("#se-play").setAttribute("aria-label", on ? "Pause" : fresh ? "Play: ready" : "Play");
  $("#se-play").classList.toggle("ready", fresh);
}
$("#se-play").onclick = () => {
  if (SE.waiting) { SE.waiting = false; return sePlayIcon(); }
  if (!SE.audio.paused) { SE.audio.pause(); return sePlayIcon(); }
  if (SE.cur < 0 || !SE.audio.src) return sePlayPart(0);
  SE.rec.pause(); seRecIcons(); seVol(SE.audio); SE.audio.play().catch(() => {}); sePlayIcon();
};
function seTimes() {
  const ready = SE.parts.reduce((a, p) => a + (p ? p.seconds : 0), 0);
  const before = SE.parts.slice(0, Math.max(0, SE.cur)).reduce((a, p) => a + (p ? p.seconds : 0), 0);
  const played = SE.cur >= 0 ? before + (SE.audio.currentTime || 0) : 0;
  // what's still to make, from the text left (made seconds per character so far)
  const madeChars = SE.sents.reduce((a, s, i) => a + (SE.parts[i] ? s.length : 0), 0);
  const leftChars = SE.sents.reduce((a, s, i) => a + (SE.parts[i] ? 0 : s.length), 0);
  const total = SE.done || !madeChars ? Math.max(ready, SE.done ? ready : SE.estimate || 0) : ready + leftChars * ready / madeChars;
  return { ready, played, total };
}
function seTrack() {
  const { ready, played, total } = seTimes(), pct = x => total ? `${Math.min(100, x / total * 100).toFixed(2)}%` : "0%";
  const [pl, rd] = $$("#se-track i");
  pl.style.width = pct(played); rd.style.width = pct(Math.max(0, ready - played));
  $("#se-time").textContent = `${fmtTime(played)} / ${fmtTime(ready)} ready`;
  $("#se-left").textContent = SE.done ? (SE.result ? `${fmtTime(ready)} in all` : "") : SE.left != null ? `${leftText(SE.left)} to make · ~${fmtTime(total)} in all` : `~${fmtTime(total)} in all`;
  $("#se-track").setAttribute("aria-valuetext", `${fmtTime(played)} played of ${fmtTime(ready)} ready`);
}
function seSeek(t) {   // seconds from the start, within what's made
  let acc = 0;
  for (let i = 0; i < SE.parts.length; i++) {
    const p = SE.parts[i]; if (!p) return;
    if (t < acc + p.seconds) return sePlayPart(i, t - acc);
    acc += p.seconds;
  }
}
$("#se-track").addEventListener("click", e => {
  const r = e.currentTarget.getBoundingClientRect(), { total } = seTimes();
  seSeek((e.clientX - r.left) / r.width * total);
});
$("#se-track").addEventListener("keydown", e => {
  const step = { ArrowLeft: -5, ArrowRight: 5 }[e.key]; if (step === undefined) return;
  e.preventDefault(); seSeek(Math.max(0, seTimes().played + step));
});

// ------------------------------------------------------------ Recent
async function seLoadRecent() {
  try { SE.recent = await api("/api/speak/recent"); } catch (e) { SE.recent = []; }
  const mine = SE.result && SE.recent.find(r => r.id === SE.result.id);
  if (SE.result && !mine) {   // deleted (here or in SafeHouse) or rolled off the tray: its player goes too
    SE.audio.pause(); SE.audio.removeAttribute("src"); SE.result = null; SE.job = null; $("#se-now").hidden = true;
  } else if (mine) { SE.result.kept = mine.kept; seKeepShow(); }
  seRecent();
}
function seRecent() {
  const box = $("#se-recent");
  if (!SE.recent.length) { box.innerHTML = `<p class="meta">Speech you make collects here.</p>`; return; }
  box.innerHTML = SE.recent.slice(0, 15).map(r => `<div class="se-rrow" data-kept="${r.kept ? "true" : "false"}">
      <button class="play" type="button" data-rplay="${r.id}" aria-label="Play">${r.id === SE.recId && !SE.rec.paused ? "❚❚" : "▶"}</button>
      <div class="t"><b title="${esc(r.text)}">“${esc(r.text.length > 140 ? r.text.slice(0, 140) + "…" : r.text)}”</b>${facts([{ html: `${typeof earMark === "function" ? earMark("speech", r.id) : ""}${vname(r.voice)}` }, `${paceWord(r)} pace`, r.mode === "cadence" ? "Their own way" : "", { t: upFirst(dayWord(r.created)), muted: true }, { t: dayTime(r.created), deep: true }])}</div>
      <span class="mono meta dur">${fmtTime(r.duration)}</span>
      <span class="row"><button class="mini" type="button" data-rkeep="${r.id}" aria-pressed="${r.kept}" title="${r.kept ? "Kept in SafeHouse. Click to take it back." : "Keep this speech for good"}">${r.kept ? "Kept ✓" : "Send to SafeHouse"}</button><a class="mini" href="${r.mp3_url || r.url}?download">Download</a><button class="mini" type="button" data-rdel="${r.id}">Remove</button></span></div>`).join("");
  $$("[data-rplay]", box).forEach(b => b.onclick = () => sePlayRecent(SE.recent.find(r => r.id === b.dataset.rplay)));
  $$("[data-rkeep]", box).forEach(b => b.onclick = () => seToggleKeep(b.dataset.rkeep, b.getAttribute("aria-pressed") === "true"));
  $$("[data-rdel]", box).forEach(b => b.onclick = async () => {
    if (!b.classList.contains("confirm")) { b.classList.add("confirm"); arm(b); b.textContent = "Sure?"; setTimeout(() => { if (b.isConnected) { b.classList.remove("confirm"); disarm(b); b.textContent = "Remove"; } }, 4000); return; }
    if (!armedFor(b)) return;   // a double click isn't a confirmation
    const id = b.dataset.rdel;
    seRelease(id);
    try { await api(`/api/speak/${id}`, { method: "DELETE" }); } catch (e) { return showError(e.message, "Couldn't remove it"); }
    seLoadRecent();
  });
}
// Keep (phase 6): "Send to SafeHouse" keeps a speech for good; unkept ones roll off the recent tray (the latest 15)
function seKeepShow() {
  const b = $("#se-keep"), r = SE.result;
  b.hidden = !r;
  if (typeof earShow === "function") earShow($("#se-ear"), "speech", r);   // How did it sound? (ear.js; a result exists once it's done)
  if (!r) return;
  b.setAttribute("aria-pressed", !!r.kept); b.dataset.kept = r.kept ? "true" : "false";
  b.textContent = r.kept ? "Kept in SafeHouse ✓" : "Send to SafeHouse";
}
async function seToggleKeep(id, kept) {
  try { await api(`/api/speak/${id}/${kept ? "unkeep" : "keep"}`, { json: {} }); }
  catch (e) { return showError(e.message, "Couldn't change that"); }
  if (SE.result?.id === id) SE.result.kept = !kept;
  await seLoadRecent(); seKeepShow();
}
$("#se-keep").onclick = () => SE.result && seToggleKeep(SE.result.id, !!SE.result.kept);
function sePlayRecent(r) {
  if (!r) return;
  if (SE.recId === r.id && SE.rec.src) { if (SE.rec.paused) { seVol(SE.rec); SE.rec.play().catch(() => {}); } else SE.rec.pause(); return seRecIcons(); }
  SE.audio.pause(); SE.waiting = false; sePlayIcon();
  SE.recId = r.id; SE.recItem = r; SE.rec.src = r.url; seVol(SE.rec); SE.rec.play().catch(() => {});
  seRecIcons();
}
function seRecIcons() { $$("[data-rplay]").forEach(b => { const on = b.dataset.rplay === SE.recId && !SE.rec.paused; b.textContent = on ? "❚❚" : "▶"; b.setAttribute("aria-label", on ? "Pause" : "Play"); }); }
SE.rec.addEventListener("ended", seRecIcons);
SE.rec.addEventListener("pause", seRecIcons);
// let go of a file before it's renamed or deleted (Windows won't while a player streams it)
function seRelease(id) {
  const r = SE.recent.find(x => x.id === id) || {};
  for (const a of [SE.rec, SE.audio]) {
    const src = decodeURIComponent(a.src || "");
    if (a.src && (!id || [r.url, r.mp3_url, SE.result?.id === id ? SE.result.url : null].filter(Boolean).some(u => src.endsWith(decodeURIComponent(u))))) {
      a.pause(); a.removeAttribute("src"); a.load();
    }
  }
  if (SE.recId === id) SE.recId = null;
}
$("#se-all").onclick = () => { if (typeof L !== "undefined") L.cat = "speech"; store.set("uc-lib-cat", "speech"); location.hash = "#safehouse/library"; showTab("safehouse"); window.scrollTo({ top: 0 }); };

seCount();

// after a reload: a speech still being made shows again in "Now speaking" (it carries on from the server's events)
(async () => {
  const run = (await refreshJobs(true)).find(j => j.kind === "speech" && isRunning(j));
  if (run && !SE.job) seStart(run.id, null, 0, true);
})();
