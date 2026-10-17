"use strict";
// The First assignment (mockup docs/mockups/first_assignment.html): a guided first run inside the real pages. A brief on
// FrontMan the first time, then a strip under the top bar (#assign) that names one action at a time; the thing to press
// carries the page's one lit edge (.aim). Five real actions: make a cover from the sample song with the demo voice,
// listen, keep it, see SafeHouse, start a voice of your own. Skippable at every step, replayable from Settings.
// Server: /api/status's {tutorial: {done}} (missing = no tutorial backend: nothing shows), POST /api/tutorial/done and
// /api/tutorial/reset (re-seeds the sample song and the demo voice); songs carry `sample: true`, voices `demo: true`.
// Uses app.js ($, $$, api, esc, showError, store, S, renderSong, refreshEstimate, loadVoices), shell.js (showTab),
// jobs.js (onJobs, runningJobs).

const AS = { known: false, done: null, step: store.get("uc-assign-step", 0), finished: false,
             desk: store.get("uc-assign-desk", { song: true, voice: true }) };   // what was on the desk when it started
const DESK_SAY = { "11": "The song and the voice are already on your desk. Press Convert.",
                   "01": "The voice is on your desk; bring a song: drop one in, or search for one. Then press Convert.",
                   "10": "The song is on your desk; pick any voice in the list, or find one. Then press Convert.",
                   "00": "Bring a song (drop one in, or search) and pick a voice below. Then press Convert." };
const deskKey = () => `${AS.desk.song ? 1 : 0}${AS.desk.voice ? 1 : 0}`;
const ASSIGN = [
  { head: "Make the cover.", say: () => DESK_SAY[deskKey()], tab: "frontman",
    aim: () => [$("#convert"), !AS.desk.song && $("#drop"), !AS.desk.voice && $("#voices")] },
  { head: "Listen.", say: "The same song in a different voice. Play it, then switch between the cover and the original.", tab: "frontman",
    aim: () => [$("#result .ab")] },
  { head: "Keep it.", say: "Send to SafeHouse keeps a cover for good; the rest roll off after fifteen.", tab: "frontman",
    aim: () => [$("#keep")] },
  { head: "This is SafeHouse.", say: "Everything you keep lives here, room by room. Your cover is in the Library.", tab: "safehouse",
    aim: () => [$('#lib-list .litem[data-kept="true"]')], next: "Next" },
  { head: "Last: a voice of your own.", say: "Name someone and press Train tonight. It trains while the PC is free, and you wake up to your first voice of your own.", tab: "doubleagent",
    aim: () => [$("#da-person"), $("#da-tonight")], next: "Done" },
];
const VOICE_KINDS = new Set(["train", "train-auto", "train-prepare", "train-export", "install", "night"]);   // "a voice of your own" started

// ------------------------------------------------------------ state: the server's flag, the step in this browser
async function assignBoot() {
  let s;
  try { s = await api("/api/status"); } catch (e) { return; }
  if (!s.tutorial) return;   // no tutorial backend in this copy: nothing shows
  AS.known = true; AS.done = s.tutorial.done || null;
  renderDemoOffer(s.tutorial);
  renderAssignSetting();
  if (AS.step > 0) return assignGo(AS.step, false);   // carry on where it was (a reload, a restart)
  if (AS.done) return;
  assignBrief();
}
// the briefing card and Agent Smoke's greeting: on a first run, and again on Replay
async function assignBrief() {
  const desk = await deskNow().catch(() => ({ song: null, voice: null }));
  $("#assign-brief-say").textContent = desk.song && desk.voice
    ? "A song and a voice are on your desk. Five steps, about ten minutes: make a cover, hear it, keep it, see where it lives, then get a voice of your own. The song is ours and the voice isn't anyone's, so nothing you make here is anyone else's."
    : desk.voice ? "A voice is on your desk; bring a song you like. Five steps, about ten minutes: make a cover, hear it, keep it, see where it lives, then get a voice of your own. The voice isn't anyone's."
    : "Bring a song and a voice. Five steps, about ten minutes: make a cover, hear it, keep it, see where it lives, then get a voice of your own.";
  $("#assign-brief").hidden = false;
  greetBoot(!!(desk.song && desk.voice));
}

// ------------------------------------------------------------ Agent Smoke's greeting on the brief (the backend's clips)
// GET /api/tutorial/greetings: {greetings: [{from, to, text, url}], followon: {text, url}}. The greeting is picked by the
// local hour (from inclusive, to exclusive); the follow-on ("A song and a voice are on your desk…") plays after it only
// when both really are there. A play button only: nothing plays by itself.
const GREET = { parts: [], audio: null, i: 0 };
async function greetBoot(full) {
  let g;
  try { g = await api("/api/tutorial/greetings"); } catch (e) { return; }
  const h = new Date().getHours(), pick = (g.greetings || []).find(x => h >= x.from && h < x.to);
  if (!pick?.url) return;
  GREET.parts = [pick, full && g.followon?.url ? g.followon : null].filter(Boolean);
  $("#assign-greet-say").textContent = `“${GREET.parts.map(p => p.text).join(" ")}”`;
  $("#assign-greet").hidden = false;
}
function greetStop() { GREET.audio?.pause(); GREET.audio = null; GREET.i = 0; $("#assign-greet-play").textContent = "▶"; $("#assign-greet-play").setAttribute("aria-label", "Play Agent Smoke's greeting"); }
function greetPart(i) {
  const p = GREET.parts[i];
  if (!p) return greetStop();
  GREET.i = i;
  const a = GREET.audio = new Audio(p.url);
  a.volume = VOL.muted ? 0 : VOL.level; a.muted = VOL.muted;   // the page's own volume
  a.onended = () => { if (GREET.audio === a) greetPart(i + 1); };
  a.play().catch(() => greetStop());
}
$("#assign-greet-play").onclick = () => {
  if (GREET.audio && !GREET.audio.paused) return greetStop();
  if (typeof stopAll === "function") stopAll();
  $("#assign-greet-play").textContent = "❚❚"; $("#assign-greet-play").setAttribute("aria-label", "Stop the greeting");
  greetPart(0);
};
// the demo voice, refreshed (the backend's demo refresh): a newer Agent Smoke can be swapped in (the old one waits aside,
// your name for him is kept), and the previous one brought back while it's kept. In the First assignment's Settings row.
function renderDemoOffer(t) {
  const box = $("#set-demo");
  if (!box) return;
  const up = t?.demo_update || null, prev = !!t?.demo_previous;
  box.hidden = !up && !prev;
  const made = up?.made ? new Date(up.made + "T12:00").toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" }) : "";
  box.innerHTML = up ? `A newer ${esc(up.display || "Agent Smoke")} is available${made ? ` (made ${esc(made)})` : ""}. <button class="linkbtn" type="button" data-demo="update">Update</button>`
    : prev ? `<button class="linkbtn quiet" type="button" data-demo="undo">Go back to the previous Agent Smoke</button>` : "";
  $$("[data-demo]", box).forEach(b => b.onclick = async () => {
    const how = b.dataset.demo; b.disabled = true;
    try {
      const r = await api(`/api/tutorial/demo/${how}`, { method: "POST" });
      renderDemoOffer(r.tutorial);
      if (typeof loadVoices === "function") loadVoices().catch(() => {});
      showError(how === "update" ? "Agent Smoke is updated. The earlier one waits aside if you'd rather have him back." : "The previous Agent Smoke is back.", "Agent Smoke", null, true);
    } catch (e) { b.disabled = false; showError(e.message, "Agent Smoke"); }
  });
}
// the sample song (sample: true) and the demo voice (demo: true), if this copy has them
async function deskNow() {
  const songs = await api("/api/library/songs"), voices = await api("/api/voices");
  return { song: (songs.songs || songs).find?.(x => x.sample) || null, voice: (voices.voices || voices).find?.(v => v.demo) || null };
}
async function assignMarkDone() {
  try { AS.done = (await api("/api/tutorial/done", { method: "POST" })).tutorial?.done || new Date().toISOString().slice(0, 10); } catch (e) {}
  renderAssignSetting();
}
function renderAssignSetting() {
  const b = $("#set-assign-replay"), say = $("#set-assign-say");
  b.disabled = !AS.known;
  const d = AS.done && /^\d{4}-\d\d-\d\d/.test(AS.done) ? new Date(AS.done.slice(0, 10) + "T12:00").toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" }) : "";
  say.textContent = "The guided first run: a cover from the sample song, a listen, a Send to SafeHouse, a look around." +
    (d ? ` Done on ${d}.` : AS.known && !AS.done ? " Not taken yet." : "");
}

// ------------------------------------------------------------ starting: the sample song and the demo voice on the desk
async function assignStart() {
  if (S.job || S.osBusy) return showError("Wait for the cover that's being made to finish (or cancel it), then take the assignment.", "Busy");
  $("#assign-brief").hidden = true;
  let desk;
  try { desk = await deskNow(); } catch (e) { return showError(e.message, "Couldn't start the assignment"); }
  if (!desk.song || !desk.voice) {   // deleted since: seeded again; if that can't be done, it starts with what's there
    try { await api("/api/tutorial/reset", { method: "POST" }); desk = await deskNow(); } catch (e) {}
  }
  // a copy without the sample song's audio (it comes later) or the demo voice's files: step 1 asks for what's missing
  S.opts = { ...S.defaults }; syncControls();   // clean settings for this run: a one-shot's or Deep Cover's leftovers (a backing voice, an octave) would make a cover that isn't Agent Smoke's alone; the saved ones stay for after
  try {
    if (desk.song) { S.song = await api(`/api/songs/${desk.song.id}`); store.set("uc-song", S.song.id); renderSong(); refreshEstimate(); }
    if (desk.voice) await loadVoices(desk.voice.name);
  } catch (e) { return showError(e.message, "Couldn't start the assignment"); }
  AS.desk = { song: !!desk.song, voice: !!desk.voice }; store.set("uc-assign-desk", AS.desk);
  AS.finished = false;
  assignGo(1, true);
}

// ------------------------------------------------------------ a step: the strip's line, the lit edge, the right page
function assignGo(n, move = true) {
  AS.step = n; store.set("uc-assign-step", n);
  if (n === 2) AS.heard = false;
  if (n === 5) AS.before = new Set(runningJobs().map(j => j.id));   // voice work already running doesn't count
  const st = ASSIGN[n - 1];
  $("#assign").hidden = false; $("#assign").classList.remove("done");
  $("#assign-n").textContent = `${n} of ${ASSIGN.length}`;
  const say = typeof st.say === "function" ? st.say() : st.say;
  $("#assign-say").innerHTML = `<b>${esc(st.head)}</b> <span class="say-full">${esc(say)}</span><span class="say-short">${esc(say.split(/(?<=[.!?])\s+/)[0])}</span>`;
  $("#assign-line").style.width = `${n / ASSIGN.length * 100}%`;
  $("#assign-next").hidden = !st.next; $("#assign-next").textContent = st.next || "";
  $("#assign-skip").hidden = n === ASSIGN.length; $("#assign-skip").textContent = "Skip the rest";   // no rest on the last step
  if (move) {
    if (document.body.dataset.module !== st.tab) showTab(st.tab);
    if (n === 1 && typeof syncBuild === "function") { S.buildOpen = true; syncBuild(); }   // the desk is laid: the build path is in sight
    if (n === 4) assignShowKept();
  }
  reaim();
  if (move) requestAnimationFrame(() => assignTargets()[0]?.scrollIntoView({ behavior: "smooth", block: "center" }));
}
const assignTargets = () => AS.step > 0 && !AS.finished ? (ASSIGN[AS.step - 1].aim() || []).filter(Boolean) : [];
// the lit edge follows its target through redraws (the library list, the result panel)
let aimFrame = 0, aimTimer = 0;
function reaim() {
  aimFrame = 0; clearTimeout(aimTimer);
  const want = new Set(assignTargets());
  $$(".aim").forEach(el => { if (!want.has(el)) el.classList.remove("aim"); });
  want.forEach(el => el.classList.add("aim"));
}
// a frame, or 200 ms when the window isn't painting (the frame never comes and aimFrame stayed set, so the kept cover was never lit: RC pass 10, as P9-4)
new MutationObserver(() => { if (AS.step > 0 && !aimFrame) { aimFrame = requestAnimationFrame(reaim); aimTimer = setTimeout(reaim, 200); } })
  .observe(document.body, { childList: true, subtree: true });

async function assignShowKept() {   // SafeHouse > Library > Covers > Kept, so the kept cover is in sight
  location.hash = "safehouse/library";   // the Library, not whichever room was open last (IA P9-14: the kept cover wasn't lit)
  const soon = async sel => { for (let i = 0; i < 40 && !$(sel); i++) await new Promise(r => setTimeout(r, 50)); return $(sel); };
  (await soon('#lib-cats .cat[data-cat="covers"]'))?.click();
  (await soon('#lib-kfilter .tab[data-v="kept"]'))?.click();
  const row = await soon('#lib-list .litem[data-kept="true"]');   // the list arrives after the clicks: light it and bring it into sight then
  if (row && AS.step === 4) { reaim(); row.scrollIntoView({ behavior: "smooth", block: "center" }); }
}

// ------------------------------------------------------------ what moves it on: the real actions
document.addEventListener("uc:jobdone", e => { if (AS.step === 1 && e.detail.kind === "cover" && (e.detail.final?.state || e.detail.state) === "done") setTimeout(() => assignGo(2), 300); });
// step 2 teaches two things: play, then switch between the cover and the original
$("#result").addEventListener("click", e => {
  if (AS.step !== 2) return;
  if (e.target.closest("[data-ab]")) { if (AS.heard) setTimeout(() => assignGo(3), 400); }
  else if (e.target.closest(".play, #res-play")) AS.heard = true;
});
new MutationObserver(() => { if (AS.step === 3 && $("#keep").dataset.kept === "true") assignGo(4); })
  .observe($("#keep"), { attributes: true, attributeFilter: ["data-kept", "aria-pressed"] });
onJobs(() => { if (AS.step === 5 && runningJobs().some(j => VOICE_KINDS.has(j.kind) && !AS.before?.has(j.id))) assignComplete(); });
// Train tonight (the suggested ending): the voice is queued, not running, so DoubleAgent says so
document.addEventListener("uc:voicequeued", e => {
  if (AS.step === 5) assignComplete(`<b>That's the whole job.</b> ${esc(e.detail?.person || "Your voice")} trains tonight; by morning it's in your voices, ready for FrontMan. Everything else is more of the same: a song, a voice, a listen.`);
});
$("#assign-next").onclick = () => { if (AS.step < ASSIGN.length) assignGo(AS.step + 1); else assignComplete(); };

// ------------------------------------------------------------ the end: complete, skip, not now, replay
function assignComplete(say) {
  AS.finished = true; AS.step = 0; store.set("uc-assign-step", 0);
  reaim();
  $("#assign").classList.add("done");
  $("#assign-n").textContent = "complete";
  $("#assign-say").innerHTML = say || "<b>That's the whole job.</b> Everything else is more of the same: a song, a voice, a listen. Deep Cover (the glasses) shows the numbers when you want them.";
  $("#assign-line").style.width = "100%";
  $("#assign-next").hidden = true;
  $("#assign-skip").textContent = "Close";
  assignMarkDone();
}
function assignClose() {
  AS.step = 0; AS.finished = false; store.set("uc-assign-step", 0);
  loadOpts();   // the run used clean settings; the saved ones come back (a later change would otherwise save the clean ones over them)
  $("#assign").hidden = true; $("#assign-brief").hidden = true;
  reaim();
}
$("#assign-skip").onclick = () => { if (!$("#assign").classList.contains("done")) assignMarkDone(); assignClose(); };
$("#assign-take").onclick = () => { greetStop(); assignStart(); };
$("#assign-later").onclick = () => { greetStop(); $("#assign-brief").hidden = true; assignMarkDone(); };
$("#set-assign-replay").onclick = async () => {
  if (S.job || S.osBusy) return showError("Wait for the cover that's being made to finish (or cancel it), then take the assignment.", "Busy");
  try { await api("/api/tutorial/reset", { method: "POST" }); } catch (e) { return showError(e.message, "Couldn't start the assignment"); }
  greetStop(); assignClose();   // a run in progress is put away; the briefing and the greeting open again
  AS.done = null; renderAssignSetting();
  showTab("frontman"); window.scrollTo({ top: 0 });
  assignBrief();
};

setTimeout(assignBoot, 0);
