"use strict";
// The First assignment (mockup docs/mockups/first_assignment.html): a guided first run inside the real pages. A brief on
// FrontMan the first time, then a strip under the top bar (#assign) that names one action at a time; the thing to press
// carries the page's one lit edge (.aim). Five real actions: make a cover from the sample song with the demo voice,
// listen, keep it, see SafeHouse, start a voice of your own. Skippable at every step, replayable from Settings.
// Server: /api/status's {tutorial: {done}} (missing = no tutorial backend: nothing shows), POST /api/tutorial/done and
// /api/tutorial/reset (re-seeds the sample song and the demo voice); songs carry `sample: true`, voices `demo: true`.
// Uses app.js ($, $$, api, esc, showError, store, S, renderSong, refreshEstimate, loadVoices), shell.js (showTab),
// jobs.js (onJobs, runningJobs).

const AS = { known: false, done: null, step: store.get("uc-assign-step", 0), finished: false };
const ASSIGN = [
  { head: "Make the cover.", say: "The song and the voice are already on your desk. Press Convert.", tab: "frontman",
    aim: () => [$("#convert")] },
  { head: "Listen.", say: "The same song in a different voice. Play it, then switch between the cover and the original.", tab: "frontman",
    aim: () => [$("#result .ab")] },
  { head: "Keep it.", say: "Send to SafeHouse keeps a cover for good; the rest roll off after fifteen.", tab: "frontman",
    aim: () => [$("#keep")] },
  { head: "This is SafeHouse.", say: "Everything you keep lives here, room by room. Your cover is in the Library.", tab: "safehouse",
    aim: () => [$('#lib-list .litem[data-kept="true"]')], next: "Next" },
  { head: "Last: a voice of your own.", say: "Find one that's already made, or train one from clips of a person. Both start here.", tab: "doubleagent",
    aim: () => [$("#q-panel"), $("#tr-head")], next: "Done" },
];
const VOICE_KINDS = new Set(["train", "train-auto", "train-prepare", "train-export", "install", "night"]);   // "a voice of your own" started

// ------------------------------------------------------------ state: the server's flag, the step in this browser
async function assignBoot() {
  let s;
  try { s = await api("/api/status"); } catch (e) { return; }
  if (!s.tutorial) return;   // no tutorial backend in this copy: nothing shows
  AS.known = true; AS.done = s.tutorial.done || null;
  renderAssignSetting();
  if (AS.step > 0) return assignGo(AS.step, false);   // carry on where it was (a reload, a restart)
  $("#assign-brief").hidden = !!AS.done;
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
  let song, voice;
  const find = async () => {
    const songs = await api("/api/library/songs"), voices = await api("/api/voices");
    song = (songs.songs || songs).find?.(x => x.sample);
    voice = (voices.voices || voices).find?.(v => v.demo);
  };
  try {
    await find();
    if (!song || !voice) { await api("/api/tutorial/reset", { method: "POST" }); await find(); }   // deleted since: seeded again
  } catch (e) { return showError(e.message, "Couldn't start the assignment"); }
  if (!song || !voice) return showError("The sample song or the demo voice isn't there, and putting them back didn't work. Restart UnderCover and try again from Settings.", "Couldn't start the assignment");
  try {
    S.song = await api(`/api/songs/${song.id}`); store.set("uc-song", S.song.id); renderSong(); refreshEstimate();
    await loadVoices(voice.name);
  } catch (e) { return showError(e.message, "Couldn't start the assignment"); }
  AS.finished = false;
  assignGo(1, true);
}

// ------------------------------------------------------------ a step: the strip's line, the lit edge, the right page
function assignGo(n, move = true) {
  AS.step = n; store.set("uc-assign-step", n);
  if (n === 5) AS.before = new Set(runningJobs().map(j => j.id));   // voice work already running doesn't count
  const st = ASSIGN[n - 1];
  $("#assign").hidden = false; $("#assign").classList.remove("done");
  $("#assign-n").textContent = `${n} of ${ASSIGN.length}`;
  $("#assign-say").innerHTML = `<b>${esc(st.head)}</b> ${esc(st.say)}`;
  $("#assign-line").style.width = `${n / ASSIGN.length * 100}%`;
  $("#assign-next").hidden = !st.next; $("#assign-next").textContent = st.next || "";
  $("#assign-skip").hidden = false; $("#assign-skip").textContent = "Skip the rest";
  if (move) {
    if (document.body.dataset.module !== st.tab) showTab(st.tab);
    if (n === 4) assignShowKept();
    if (n === 5) $('#q-mode button[data-v="new"]')?.click();   // a new voice, not improving one
  }
  reaim();
  if (move) requestAnimationFrame(() => assignTargets()[0]?.scrollIntoView({ behavior: "smooth", block: "center" }));
}
const assignTargets = () => AS.step > 0 && !AS.finished ? (ASSIGN[AS.step - 1].aim() || []).filter(Boolean) : [];
// the lit edge follows its target through redraws (the library list, the result panel)
let aimFrame = 0;
function reaim() {
  aimFrame = 0;
  const want = new Set(assignTargets());
  $$(".aim").forEach(el => { if (!want.has(el)) el.classList.remove("aim"); });
  want.forEach(el => el.classList.add("aim"));
}
new MutationObserver(() => { if (AS.step > 0 && !aimFrame) aimFrame = requestAnimationFrame(reaim); })
  .observe(document.body, { childList: true, subtree: true });

async function assignShowKept() {   // SafeHouse > Library > Covers > Kept, so the kept cover is in sight
  location.hash = "safehouse";
  const soon = async sel => { for (let i = 0; i < 40 && !$(sel); i++) await new Promise(r => setTimeout(r, 50)); return $(sel); };
  (await soon('#lib-cats .cat[data-cat="covers"]'))?.click();
  (await soon('#lib-kfilter .tab[data-v="kept"]'))?.click();
}

// ------------------------------------------------------------ what moves it on: the real actions
document.addEventListener("uc:jobdone", e => { if (AS.step === 1 && e.detail.kind === "cover" && (e.detail.final?.state || e.detail.state) === "done") setTimeout(() => assignGo(2), 300); });
$("#result").addEventListener("click", e => {
  if (AS.step === 2 && e.target.closest("[data-ab], .play, #res-play")) setTimeout(() => assignGo(3), 400);
});
new MutationObserver(() => { if (AS.step === 3 && $("#keep").dataset.kept === "true") assignGo(4); })
  .observe($("#keep"), { attributes: true, attributeFilter: ["data-kept", "aria-pressed"] });
onJobs(() => { if (AS.step === 5 && runningJobs().some(j => VOICE_KINDS.has(j.kind) && !AS.before?.has(j.id))) assignComplete(); });
$("#assign-next").onclick = () => { if (AS.step < ASSIGN.length) assignGo(AS.step + 1); else assignComplete(); };

// ------------------------------------------------------------ the end: complete, skip, not now, replay
function assignComplete() {
  AS.finished = true; AS.step = 0; store.set("uc-assign-step", 0);
  reaim();
  $("#assign").classList.add("done");
  $("#assign-n").textContent = "complete";
  $("#assign-say").innerHTML = "<b>That's the whole job.</b> Everything else is more of the same: a song, a voice, a listen. Deep Cover (the glasses) shows the numbers when you want them.";
  $("#assign-line").style.width = "100%";
  $("#assign-next").hidden = true;
  $("#assign-skip").textContent = "Close";
  assignMarkDone();
}
function assignClose() {
  AS.step = 0; AS.finished = false; store.set("uc-assign-step", 0);
  $("#assign").hidden = true; $("#assign-brief").hidden = true;
  reaim();
}
$("#assign-skip").onclick = () => { if (!$("#assign").classList.contains("done")) assignMarkDone(); assignClose(); };
$("#assign-take").onclick = () => assignStart();
$("#assign-later").onclick = () => { $("#assign-brief").hidden = true; assignMarkDone(); };
$("#set-assign-replay").onclick = async () => {
  try { await api("/api/tutorial/reset", { method: "POST" }); } catch (e) { return showError(e.message, "Couldn't start the assignment"); }
  AS.done = null; renderAssignSetting();
  showTab("frontman"); window.scrollTo({ top: 0 });
  assignStart();
};

setTimeout(assignBoot, 0);
