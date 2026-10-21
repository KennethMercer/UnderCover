"use strict";
// One-shot covers ("cover of X by Y") and songs from YouTube. Uses helpers from app.js
// ($, $$, esc, api, showError, store, fmtTime, fmtSecs, S, renderSong, refreshEstimate, loadVoices, showResult, loadHistory).

const OS = { es: null, job: null, timer: null, t0: 0, planSeq: 0, planTimer: null, last: null, vchoice: null };
// the first time a real person's voice is about to be made, one calm line (consult 6: PERSON_WARN in app.js): here on the plan when it will train

// ------------------------------------------------------------ plan preview (no downloads)
// Phase 15: the voice's quality gate. A weak voice (it doesn't sound like them, too little clean audio, or a character
// trained on talk about them instead of their scenes) is said plainly before the cover starts and again with it, with
// the way out: find better clips and train it again (DoubleAgent's Improve, now), or for a quick cover made with a
// voice that only sounds somewhat like them, "Train a proper voice for X tonight" (a new-voice item in the overnight
// queue). Plain hooks: .os-quality, button[data-os-act] with data-kind / data-now.
function qualityHtml(v) {
  const q = v && v.quality;
  if (!q || (q.ok && !q.kept)) return "";
  const a = q.action || { kind: "improve", target: v.name, now: true };
  const text = [q.ok ? "" : (q.summary || "").replace(/^Quick: /, ""), q.kept || ""].filter(Boolean).join(" ");
  return `<div class="os-quality" role="alert"><b>${q.quick ? "Quick:" : q.ok ? "Note:" : "Heads-up:"}</b> ${esc(text)}
    <button class="linkbtn" type="button" data-os-act="${esc(a.target || v.name || "")}" data-kind="${esc(a.kind)}" data-now="${a.now === false ? 0 : 1}">${esc(q.advice || "Find better clips and train it again")}</button></div>`;
}
function bindQuality() {
  $$("#os-plan [data-os-act]").forEach(b => b.onclick = async () => {
    b.disabled = true;
    const now = b.dataset.now === "1";
    try {
      await api("/api/work/add", { json: { kind: b.dataset.kind, target: b.dataset.osAct, now } });
      b.textContent = now ? "On it: finding better clips and training again (see Running)" : "Queued for tonight (see Running)";
      refreshJobs(true);
    } catch (e) { b.disabled = false; showError(e.message, "Couldn't queue it"); }
  });
}
// Phase 15: what the request names (a real person, a singer, a character, a band, or a name several people share), in
// plain words, with a correction ("SpongeBob is a real person") or, when it can't tell, the question ("Which Chris
// Evans?"). The pick goes with the plan request and the one-shot. Plain hooks: #os-subject, button[data-os-subject].
function subjectHtml(p) {
  const sj = p.subject;
  if (!sj || !sj.label) return "";
  if (sj.ask && !OS.subject) {   // the same card as "Which Hello?": the question, then one button per reading
    const split = l => { const m = String(l).match(/^(.*?),\s+(.+)$/); return m ? [m[1], m[2]] : [l, ""]; };   // "Chris Evans, the actor"
    return `<div id="os-subject" class="os-subject-ask" role="alert"><div class="choice-q"><b>${esc(sj.ask.question)}</b> ${esc(sj.label)}</div>
      <div class="choices">${sj.ask.choices.map(c => { const [name, who] = split(c.label);
        return `<button class="choice" type="button" data-os-subject="${esc(c.id)}" aria-pressed="false"><b>${esc(name)}</b>${who ? `<small>${esc(who)}</small>` : ""}</button>`; }).join("")}</div></div>`;
  }
  // a correction is offered as a question ("Is SpongeBob a real person?"), not stated back as a fact
  const ask = l => { const m = String(l).match(/^(.+?) is (an? .+)$/); return m ? `Is ${m[1]} ${m[2]}?` : String(l); };
  return `<div id="os-subject" class="meta">${esc(sj.label)}
    ${(sj.choices || []).map(c => `<button class="linkbtn" type="button" data-os-subject="${esc(c.id)}">${esc(`Not right? ${ask(c.label)}`)}</button>`).join(" ")}</div>`;
}
function bindVoiceAsk() {
  $$("#os-plan [data-os-vchoice]").forEach(b => b.onclick = () => { OS.vchoice = b.dataset.osVchoice; resetGo(); if (OS.last?.plan) drawPlan(OS.last.plan); });
}
function bindSubject() {
  $$("#os-plan [data-os-subject]").forEach(b => b.onclick = () => { OS.subject = b.dataset.osSubject; resetGo(); previewPlan(); });
}
// "Ask me each time" (the user's decision): when voices installed from elsewhere match the person, the plan asks before anything starts
// (voice_ask + voice_choices [{choice, label, eta_s, detail?}]: use:<id> per voice, check, train). The pick goes to /api/auto as voice_choice.
const etaWords = e => {
  const lo = Math.max(1, Math.round(e.low / 60)), hi = Math.round(e.high / 60);
  if (hi < 1) return "under a minute";
  if (hi >= 90) {   // hours, in halves: "about 1–1½ hours"
    const half = m => { const x = Math.round(m / 30) / 2; return Number.isInteger(x) ? String(x) : `${Math.floor(x) || ""}½`; };
    const a = half(e.low / 60), z = half(e.high / 60);
    return a === z ? `about ${z} hour${z === "1" ? "" : "s"}` : `about ${a}–${z} hours`;
  }
  return lo >= hi ? `about ${Math.max(1, hi)} min` : `about ${lo}–${hi} min`;
};
const pickedVoice = p => p.voice_ask ? (p.voice_choices || []).find(c => c.choice === OS.vchoice) : null;
function voiceAskHtml(p) {
  if (!p.voice_ask) return "";
  const n = (p.voice_choices || []).filter(c => c.choice.startsWith("use:")).length;
  const count = n === 1 ? "One voice from the community already matches." : `${["", "", "Two", "Three", "Four", "Five"][n] || n} voices from the community already match.`;
  return `<div id="os-voice-ask" class="os-subject-ask"${OS.vchoice ? "" : ' role="alert"'}><div class="choice-q"><b>Which ${esc(p.person)}?</b> ${count}</div>
    <div class="choices">${p.voice_choices.map(c => `<button class="choice" type="button" data-os-vchoice="${esc(c.choice)}" aria-pressed="${c.choice === OS.vchoice}"><b>${esc(c.label)}</b><small>${esc(etaWords(c.eta_s))}${c.detail ? ` · ${esc(c.detail)}` : ""}</small></button>`).join("")}</div></div>`;
}
function voiceLine(v, p) {
  const who = esc(p?.person || v?.display || v?.name || "");
  if (p?.voice_ask) {
    const c = pickedVoice(p);
    return !c ? `<span class="warn">pick one below</span>` : c.voice ? `<b>${vname(c.voice)}</b>, used as it is`
      : c.choice === "check" ? `a ${who} voice from the community, checked against real clips first`
      : `a new <b>${who}</b> voice, trained first`;
  }
  if (!v) return "";
  if (v.how === "installed") return `<b>${vname(v.name)}</b>, your voice${v.trained ? ", trained here" : ""}`;
  if (v.how === "check") return `a ${who} voice from the community, checked against real clips first<span class="meta" data-deep> · candidates: <b>${v.candidates.map(esc).join("</b> or <b>")}</b>; none close enough trains one (~2 GB while training)</span>`;
  return `none yet: a new <b>${who}</b> voice is trained first<span class="meta" data-deep> (~2 GB while training)</span>`;
}
// how the plan goes: "train" (a voice first), "check" (community voices first), "installed" (a voice of yours), or "" (no plan, or a pick missing)
function planPath(p) {
  if (!p) return "";
  if (p.voice_ask) { const c = pickedVoice(p); return !c ? "" : c.choice === "train" ? "train" : c.choice === "check" ? "check" : "installed"; }
  return p.voice?.how || "";
}
// the button says what one press does, with its time inside (consult 2: no second press, no sentence with a semicolon)
function syncGo(p) {
  const btn = $("#os-go"), path = planPath(p);
  const t = p && path ? planTime(p) : "";
  const short = t ? t.split(";")[0].replace(/ to check$/, "") : "";   // a time with a branch in it ("a few minutes to check; about 47 min if…") keeps the branch on the card
  btn.textContent = !path ? "Make it" : path === "train" ? `Train a voice, then make it${short ? ` · ${short}` : ""}`
    : path === "check" && p.voice_ask ? `Check, then make it${short ? ` · ${short}` : ""}` : `Make it${short ? ` · ${short}` : ""}`;
}
function planTime(p) {
  if (p.voice_ask) { const c = pickedVoice(p); return c ? etaWords(c.eta_s) : "depends on your pick"; }
  const eta = p.eta_s;   // an installed voice: the server's own range, in seconds
  if (eta && p.voice?.how === "installed") {
    const lo = Math.max(1, Math.round(eta.low / 60)), hi = Math.round(eta.high / 60);
    return hi < 1 ? "under a minute" : lo >= hi ? `about ${Math.max(1, hi)} min` : `about ${lo}–${hi} min`;
  }
  const v = p.voice?.how, tr = trainWords(p);   // no number of the page's own: training's is the estimator's, a quick cover's the plan's
  return v === "train" ? tr || "about an hour" : v === "check" ? `a few minutes to check${tr ? `; ${tr} if it has to train` : ""}` : "a few minutes";
}
// the plan's own training estimate (train_s: the one the job and the voice question use), else the estimator's
const trainWords = p => { if (p.train_s != null) return etaWords({ low: p.train_s, high: p.train_s }); const e = estWords("new", p.person, "singing"); if (!e && p.person && !EST.asked.has(estKey("new", p.person, "singing"))) estimateFor("new", p.person, "singing").then(() => { if (OS.last?.plan === p) drawPlan(p); }); return e; };
// An ambiguous song title ("Hello"): the plan lists the songs it could be as a choice card (#auto-choices: a question, then one
// button[data-choice] per song with artist, year and title); the one picked goes with the request. While an earlier answer is
// in effect the plan returns it as `chosen`, the rest as `others`, and the card says so with the others offered as a switch.
const choiceName = c => c.artist || c.title || "this one";   // who it's by, as the card names it
function choiceLabel(c) { return `${choiceName(c)}${c.year ? " (" + c.year + ")" : ""}${c.title ? " · " + c.title : ""}`; }
function choiceButton(c, on) {
  return `<button class="choice" type="button" data-choice="${esc(c.song_url)}" data-name="${esc(choiceName(c))}" aria-pressed="${on ? "true" : "false"}">
    <b>${esc(choiceName(c))}</b><small>${[c.year, c.title].filter(Boolean).map(esc).join(" · ")}</small></button>`;
}
function renderChoices(p) {
  const box = $("#auto-choices");
  OS.choice = p.chosen?.song_url || null;
  // the song in effect stays a (pressed) button, so it can be seen and the others picked instead
  const list = p.ask ? p.choices : p.chosen ? [p.chosen, ...(p.others || [])] : [];
  box.hidden = !list?.length;
  if (box.hidden) return;
  const song = p.song ? `“${esc(p.song)}”` : "this song";
  const going = c => `Going with <b>${esc(choiceName(c))}</b> for ${song}.`;
  box.innerHTML = `<div class="choice-q">${p.ask ? `Which ${song}?` : going(p.chosen)}</div><div class="choices">${list.map(c => choiceButton(c, c.song_url === OS.choice)).join("")}</div>`;
  $$("button[data-choice]", box).forEach(b => b.onclick = () => {
    OS.choice = b.dataset.choice; resetGo();
    $$("button[data-choice]", box).forEach(x => x.setAttribute("aria-pressed", String(x === b)));
    $(".choice-q", box).innerHTML = going({ artist: b.dataset.name });
  });
}
const PLAN_IDLE = "Name a song and a voice. UnderCover finds the song, finds or trains the voice, and does the rest.";   // the card's line with nothing typed
function planKey() { return $("#os-text").value.trim() + "|" + $("#os-train").checked + "|" + (OS.subject || ""); }
function resetGo() { syncGo(OS.last?.plan && OS.last.key === planKey() ? OS.last.plan : null); }   // the button follows the plan in hand, else it's plain "Make it"
async function previewPlan() {
  const text = $("#os-text").value.trim();
  const seq = ++OS.planSeq;
  const key = planKey();
  if (!text) { $("#os-plan").textContent = PLAN_IDLE; syncGo(null); return; }
  try {
    const p = await api(`/api/auto/plan?text=${encodeURIComponent(text)}&train=${$("#os-train").checked ? 1 : 0}${OS.subject ? "&subject=" + encodeURIComponent(OS.subject) : ""}`);
    if (seq !== OS.planSeq) return;
    OS.last = { key, plan: p };
    if (!p.voice_ask || !p.voice_choices.some(c => c.choice === OS.vchoice)) OS.vchoice = null;   // a pick that's no longer offered lapses
    drawPlan(p);
  } catch (e) {
    if (seq === OS.planSeq) { $("#os-plan").textContent = e.message; OS.last = { key, error: e.message }; }
  }
  return OS.last && OS.last.key === key ? OS.last : null;
}
// the plan card from a plan (again on a pick, from the plan already in hand: no new request, so "pick one below" is gone at once)
function drawPlan(p) {
  // three plain lines (consult 2): the song, the voice, the time. No emoji, no branch list; the branches are Deep Cover's.
  const songLine = p.style ? `<b>${esc(p.song_query)}</b>, ${/^[aeiou]/i.test(p.style) ? "an" : "a"} ${esc(p.style)} version (a cover with singing in it)`
                           : `<b>${esc(p.song_query)}</b>, the album version`;
  const hint = p.note ? `<p class="meta">${esc(p.note)}</p>` : "";   // "<song> by <name>": which of the two it was read as
  const warn = planPath(p) === "train" && !personWarned() ? `<p class="meta" id="os-warn">${PERSON_WARN}</p>` : "";
  $("#os-plan").innerHTML = `<div class="os-plan-grid"><span class="k">Song</span><span>${songLine}</span><span class="k">Voice</span><span>${voiceLine(p.voice, p)}</span><span class="k">Time</span><span>${planTime(p)}</span></div>${hint}${subjectHtml(p)}${voiceAskHtml(p)}${p.voice_ask ? "" : qualityHtml(p.voice)}${warn}`;
  bindQuality(); bindSubject(); bindVoiceAsk();
  renderChoices(p);
  syncGo(p);
}
$("#os-text").addEventListener("input", () => { resetGo(); OS.choice = null; OS.subject = null; OS.vchoice = null; $("#auto-choices").hidden = true; clearTimeout(OS.planTimer); OS.planTimer = setTimeout(previewPlan, 500); });
$("#os-text").addEventListener("keydown", e => { if (e.key === "Enter") $("#os-go").click(); });
$("#os-train").onchange = () => { resetGo(); previewPlan(); };
// (work.js, which owns estimateFor, loads after this file: ask once the page is all there)
document.addEventListener("DOMContentLoaded", () => estimateFor("new", "", "singing").then(e => { if (e?.total && $("#os-train-eta")) $("#os-train-eta").textContent = ` · ${etaWords({ low: e.total * 60, high: e.total * 60 })}`; }));   // the checkbox's time is the estimator's, not a fixed hour
$$("#os-ex button").forEach(b => b.onclick = () => { $("#os-text").value = b.textContent; previewPlan(); $("#os-text").focus(); });

// ------------------------------------------------------------ the first run
// The field is always empty, with "Pink Pony Club as Plankton" as its placeholder (index.html: grey, never sent; the user, rc-page8 and
// rc-page14: one placeholder, on a first run and after). A filled-in suggestion read as random words once the tutorial was gone. The first
// start's download sits under the card's line until it's done. The sample song and Agent Smoke stay installed for the tutorial redesign (the
// tutorial-redesign branch), where any first-run hint needs a dismiss, and skipping it makes it go for good.
async function firstBoot() {
  let s, hist;
  try { [s, hist] = await Promise.all([api("/api/status"), api("/api/history")]); } catch (e) { return; }
  if (hist.length) return;
  // A brand-new install (the voice tools still to download): what this browser remembers (the last tab and room, a song or voice) belongs to
  // another install on the same address, so it goes. It once opened a fresh 1.0 on SafeHouse > System (the user, rc-page3).
  if (s.first_run) { ["uc-tab", "uc-sh-room", "uc-song", "uc-voice"].forEach(k => store.set(k, null)); showTab("frontman"); window.scrollTo({ top: 0 }); }
}
// Models still to download (the status's first_run and models_mb): setup installs every model since rc-fix4b, so this is a repair (a cut-off or
// deleted model), said in the one-shot card, not only in SafeHouse > System. The bar moves with models_progress (0-1); until there's a number it
// slides, to show that something is happening.
// (app.js's status poll calls it)
function firstReady(s) {
  const box = $("#first-ready"); if (!box) return;
  const on = !!(s && s.first_run && s.models === "loading");
  box.hidden = !on;
  if (!on) return;
  const p = s.models_progress, bar = $("#first-ready-bar");
  $("#first-ready-size").textContent = modelsSize(s);   // the real size, from the status (app.js)
  bar.classList.toggle("indet", p == null);
  $("i", bar).style.width = p != null ? `${Math.round(p * 100)}%` : "";
}
document.addEventListener("DOMContentLoaded", firstBoot);   // after shell.js (showTab) has loaded and opened the last tab

// ------------------------------------------------------------ running
$("#os-go").onclick = async () => {
  const text = $("#os-text").value.trim();
  if (!text) return $("#os-text").focus();
  const btn = $("#os-go"); btn.disabled = true;
  try {
    // Never start blind: wait for the plan, and ask once when it may train a voice (~45 min).
    const key = planKey();
    const last = OS.last && OS.last.key === key ? OS.last : await previewPlan();
    if (!last || last.error) return;
    if (last.plan.ask && !OS.choice) { $("#os-plan").insertAdjacentHTML("beforeend", ` <b>Pick which song first.</b>`); return; }
    if (last.plan.subject?.ask && !OS.subject) { $("#os-plan").insertAdjacentHTML("beforeend", ` <b>Pick which one first.</b>`); return; }
    if (last.plan.voice_ask && !OS.vchoice) { $("#os-plan").insertAdjacentHTML("beforeend", ` <b>Pick a voice first.</b>`); return; }
    if (planPath(last.plan) === "train") store.set("uc-person-warned", true);   // the line was on the card; said once
    const picked = last.plan.voice_ask ? pickedVoice(last.plan) : null;
    OS.pathEta = picked ? (picked.eta_s.low + picked.eta_s.high) / 2 : null; OS.pathAt = performance.now();   // the countdown starts from the whole picked path
    const r = await api("/api/auto", { json: { text, prefer_train: $("#os-train").checked, choice: OS.choice || undefined, subject: OS.subject || undefined, voice_choice: last.plan.voice_ask ? OS.vchoice : undefined } });
    osFollow(r.job, text);
  } catch (e) { showError(e.message, "Can't make that cover"); }
  finally { btn.disabled = false; }
};
$("#os-cancel").onclick = () => { if (OS.job) api(`/api/jobs/${OS.job}/cancel`, { method: "POST" }).catch(() => {}); $("#os-cancel").textContent = "Cancelling…"; };

function setRunning(on, title) {
  S.osBusy = on; summary();
  $("#os-idle").hidden = on; $("#os-running").hidden = !on; $("#os-run-meta").hidden = !on; $("#os-alt").hidden = on;
  $("#os-h").textContent = on ? title : "Make a cover in one go";
  $("#os-cancel").textContent = "Cancel";
  clearInterval(OS.timer);
  if (!on) OS.pathEta = null;
  if (on && $("#progress-h").textContent === "Done") $("#progress").hidden = true;   // the last manual cover's "Finished in 20.1 s" isn't this cover's (IA P8-13)
  if (on) {
    OS.t0 = performance.now();
    OS.timer = setInterval(() => {
      const el = (performance.now() - OS.t0) / 1000;
      const left = OS.eta != null ? countedDown(OS.eta, (performance.now() - OS.etaAt) / 1000) : null;
      $("#os-elapsed").textContent = (left != null ? leftText(left) + " · " : "") + `${fmtTime(el)} elapsed`;
    }, 500);
  }
}
const ORDER_OS = ["song", "voice", "cover"];
const octaveWords = o => !o ? "the singer's octave" : `voice ${Math.abs(o) === 1 ? "one octave" : "two octaves"} ${o < 0 ? "lower" : "higher"}`;
const fitWords = set => octaveWords(set.octave) + (set.key ? ` · song ${Math.abs(set.key)} semitone${Math.abs(set.key) === 1 ? "" : "s"} ${set.key < 0 ? "lower" : "higher"}` : "");
function card(stage) { return $(`#os-running .pstep[data-s=${stage}]`); }
function renderOS(ev) {
  const plan = ev.plan || OS.plan || {};
  OS.plan = plan;
  const cur = ORDER_OS.indexOf(ev.stage);
  ORDER_OS.forEach((s, i) => {
    const c = card(s);
    c.className = "pstep" + (ev.final && ev.state === "done" ? " done" : i < cur ? " done" : i === cur ? (ev.state === "error" ? " bad" : " now") : "");
    const bar = $(".bar i", c);
    bar.style.width = (i < cur || (ev.final && ev.state === "done") ? 100 : i === cur ? Math.round((ev.frac || 0) * 100) : 0) + "%";
  });
  const song = plan.song, voice = plan.voice, set = plan.settings;
  const sc = card("song"), vc = card("voice"), cc = card("cover");
  $("b", sc).textContent = song ? song.title : (plan.song_query ? `“${plan.song_query}”` : "Waiting");
  const cmp = song?.compared?.length > 1
    ? esc(`best of ${song.compared.length} versions for this voice`) + (song.compared.find(x => x.chosen)?.outside != null
        ? `<span data-deep> (${Math.round(song.compared.find(x => x.chosen).outside * 100)}% of the melody outside its range)</span>` : "")
    : (song?.compared ? "" : null);
  $("small", sc).innerHTML = ev.stage === "song" ? esc(ev.message) : song ? [plan.went_with, song.channel, song.note].filter(Boolean).map(esc).concat(cmp || []).join(" · ") : esc(plan.went_with || "");
  sc.title = song?.compared?.length > 1 ? song.compared.map(x => `${x.chosen ? "✓" : "·"} ${x.title}` +
    (x.outside != null ? ` · ${Math.round(x.outside * 100)}% out of range` : "") + ` · bleed ${Math.round(x.bleed)} dB`).join("\n") : "";
  $("b", vc).textContent = voice ? (voice.how === "train" && ev.stage === "voice" ? `Training ${voice.display || voice.name}` : voice.display || voiceDisplay(voice.name)) : (plan.person || "Waiting");
  $("small", vc).textContent = ev.stage === "voice" ? ev.message
    : voice ? (voice.checked || { installed: "your installed voice", online: `community voice${voice.repo ? " · " + voice.repo : ""}`, train: "trained in UnderCover" }[voice.how]) : "";
  $("b", cc).textContent = ev.stage === "cover" ? "Making the cover" : (ev.final && ev.state === "done" ? "Done" : "Waiting");
  $("small", cc).innerHTML = ev.stage === "cover" && !ev.final ? esc(ev.message)
    : set ? esc(`Best settings · ${fitWords(set)}`) + `<span data-deep> (${esc(set.why)})</span>` + (set.backing === "original (stacked harmonies)" ? " · original backing harmonies kept" : "") : "Best settings";
  OS.eta = ev.total_left ?? ev.eta ?? null; OS.etaAt = performance.now();
  // before the cover itself starts, never count less than what's left of the picked path (a check's whole time, then the cover): the server's first number was the cover's alone
  if (OS.pathEta && !ev.final && ev.stage !== "cover") OS.eta = Math.max(OS.eta ?? 0, OS.pathEta - (OS.etaAt - OS.pathAt) / 1000);
  $("#os-foot").textContent = voice?.how === "train" && ev.stage === "voice"
    ? "Covers you start meanwhile are made right away; training waits for them. You can watch it in DoubleAgent."
    : ev.stage === "queued" ? "Waiting for the GPU to finish the current job…" : "";
}
// Phase 22 (the voice lab's contract): when two octaves fit this voice about as well (plan.settings_fit.alt), offer a
// quick blind pick. "Hear both" renders 15 s of the cover's most-sung part at each (POST /api/history/<id>/octaves);
// the two come back shuffled as A and B, unlabelled, in sync like the previews. Picking the one the cover already has
// keeps it; picking the other makes the cover again at that octave (/again). Rendering costs the GPU half a minute, so
// it waits for "Hear both" instead of starting after every cover.
function osOctaves(plan, item) {
  const fit = plan.settings_fit, alt = fit?.alt;
  $("#os-oct")?.remove();
  if (!alt || !item?.id) return;
  const cur = { octave: fit.octave ?? 0, key: fit.key || 0 }, other = { octave: alt.octave ?? 0, key: alt.key || 0 };
  const who = plan.voice ? plan.voice.display || voiceDisplay(plan.voice.name) : "this voice";
  $("#os-plan").insertAdjacentHTML("beforeend", `<div class="os-oct" id="os-oct">
    <p><b>Two octaves suit ${esc(who)} about as well.</b> Hear both and pick the one that sounds more like them.</p>
    <div class="row"><button class="btn" type="button" id="os-oct-go">Hear both</button><span class="meta" id="os-oct-st">about half a minute</span></div>
    <div class="os-oct-ab" id="os-oct-ab" hidden></div></div>`);
  $("#os-oct-go").onclick = async () => {
    const go = $("#os-oct-go"), st = $("#os-oct-st");
    go.disabled = true; st.textContent = "Rendering both…";
    try {
      const { job } = await api(`/api/history/${item.id}/octaves`, { json: { variants: [cur, other] } });
      const stop = watchJob(job, ev => {
        if (!ev.final) { if (ev.message) st.textContent = ev.message; return; }
        stop();
        if (ev.state !== "done" || !ev.previews?.length) { go.disabled = false; st.textContent = ""; return showError(ev.message || "The previews didn't come out.", "Couldn't render both"); }
        go.hidden = true; st.textContent = "";
        osOctAB([...ev.previews].sort(() => Math.random() - 0.5), cur, item);   // shuffled: blind
      });
    } catch (e) { go.disabled = false; st.textContent = ""; showError(e.message, "Couldn't render both"); }
  };
}
function osOctAB(list, cur, item) {
  const box = $("#os-oct-ab");
  box.hidden = false;
  box.innerHTML = list.map((p, i) => `<div class="pcard" data-i="${i}">
      <div class="ptitle"><button class="play" type="button" aria-label="Play ${"AB"[i]}">▶</button><b>${"AB"[i]}</b></div>
      <button class="btn" type="button" data-oct-pick="${i}">This one</button>
      <audio preload="auto" src="${esc(p.url)}"></audio></div>`).join("") +
    `<p class="meta">Switch between them while one plays: they stay in sync. <button class="linkbtn quiet" type="button" id="os-oct-keep">Keep the cover as it is</button></p>`;
  applyVolume();
  const cards = $$(".pcard", box), audios = cards.map(c => $("audio", c));
  let pos = 0;
  cards.forEach((card, i) => {
    const a = audios[i];
    $(".play", card).onclick = () => {
      if (!a.paused) return a.pause();
      const playing = audios.find(x => !x.paused);
      if (playing) { pos = playing.currentTime; playing.pause(); }
      stopAll();
      try { a.currentTime = Math.min(pos, a.duration || pos); } catch (e) {}
      a.play().catch(() => {});
    };
    a.addEventListener("play", () => { card.classList.add("playing"); $(".play", card).textContent = "❚❚"; });
    a.addEventListener("pause", () => { card.classList.remove("playing"); $(".play", card).textContent = "▶"; if (!a.ended) pos = a.currentTime; });   // the playhead is where the last one stopped, playing or paused
    a.addEventListener("ended", () => { pos = 0; });
  });
  const done = say => { audios.forEach(a => a.pause()); $("#os-oct").innerHTML = `<p class="meta">${say}</p>`; };
  $("#os-oct-keep").onclick = () => done("Kept as it is.");
  $$("[data-oct-pick]", box).forEach(b => b.onclick = async () => {
    const o = list[+b.dataset.octPick].options || {}, pick = { octave: o.octave ?? 0, key: o.key || 0 };
    if (pick.octave === cur.octave && pick.key === cur.key) return done("That's the one you have. Kept.");
    b.disabled = true;
    try {
      const { job } = await api(`/api/history/${item.id}/again`, { json: { options: pick } });
      done("Making it again at that octave. It replaces the player when it's done; the first one stays in your covers.");
      if (!S.job) { S.job = job; summary(); startProgress(job); }
      refreshJobs(true);
    } catch (e) { b.disabled = false; showError(e.message, "Couldn't make it again"); }
  });
}
function osFollow(id, title) {
  if (OS.es) OS.es();
  OS.job = id; OS.plan = null;
  store.set("uc-os-seen", id);
  setRunning(true, title || "Making your cover");
  const stop = OS.es = watchJob(id, async ev => {   // jobs.js: the live stream, or progress polled while it's quiet
    if (ev.plan?.request) $("#os-h").textContent = ev.plan.request;
    if (ev.elapsed != null && !ev.final) OS.t0 = performance.now() - ev.elapsed * 1000;   // right after a reload too
    renderOS(ev);
    if (!ev.final) return;
    stop(); OS.es = null; OS.job = null;
    const plan = ev.plan || OS.plan || {};
    setRunning(false);
    if (ev.state === "done") {
      $("#os-plan").innerHTML = `Done in ${fmtSecs(ev.elapsed || 0)}: <b>${esc(plan.song?.title || "")}</b> with <b>${esc(plan.voice ? plan.voice.display || voiceDisplay(plan.voice.name) : "")}</b>` +
        (plan.settings ? ` · ${esc(fitWords(plan.settings))}<span data-deep> (${esc(plan.settings.why)})</span>` : "") + ". Change anything below and press Convert to try a variation.";
      try {
        if (plan.song?.id) { S.song = await api(`/api/songs/${plan.song.id}`); store.set("uc-song", S.song.id); renderSong(); }
        await loadVoices(plan.voice?.name);
        if (plan.settings) {
          // For this session only: your saved settings stay as they were ("Back to my settings" restores them).
          if (!OS.mine) OS.mine = { ...S.opts };
          // the cover's exact options when the plan carries them (the original backing kept for stacked harmonies stays kept)
          if (plan.settings.options) S.opts = { ...S.defaults, ...plan.settings.options };
          else Object.assign(S.opts, { octave: plan.settings.octave, key: plan.settings.key || 0, backing_voice: plan.voice?.name, karaoke: true, dereverb: true, quality: "high" });
          syncControls(); refreshEstimate();
          $("#os-plan").insertAdjacentHTML("beforeend", ` <button class="linkbtn" id="os-mine" type="button">Back to my settings</button>`);
          $("#os-mine").onclick = () => { S.opts = OS.mine; OS.mine = null; syncControls(); refreshEstimate(); $("#os-mine").remove(); };
        }
      } catch (e) {}
      if (ev.result) showResult(ev.result, ev.seconds);
      osOctaves(plan, ev.result);   // two octaves fit about as well: a quick blind pick (phase 22)
      $("#os-plan").insertAdjacentHTML("beforeend", qualityHtml(plan.voice));   // a weak voice is said with the cover too
      bindQuality();
      const others = (plan.song?.compared || []).filter(x => !x.chosen && x.id);
      if (others.length) {   // the versions it compared: one click to hear the same voice on another
        $("#os-plan").insertAdjacentHTML("beforeend", `<div class="os-versions"><span class="meta">Other versions it compared:</span>` +
          others.map(x => `<div class="srow"><div><b>${esc(x.title)}</b><p class="meta" data-deep>${x.outside != null ? `${Math.round(x.outside * 100)}% of the melody outside the voice's range` : ""}</p></div>
            <button class="btn" type="button" data-ver="${esc(x.id)}">Make it with this version</button></div>`).join("") + `</div>`);
        $$("#os-plan [data-ver]").forEach(b => b.onclick = async () => {
          try {
            S.song = await api(`/api/songs/${b.dataset.ver}`); store.set("uc-song", S.song.id); renderSong();
            S.opts = { ...S.opts, fit: true };   // each version gets its own key and octave
            syncControls();
            $("#convert").click();
          } catch (e) { showError(e.message, "Couldn't load that version"); }
        });
      }
    } else if (ev.state === "cancelled") {
      $("#os-plan").textContent = "Cancelled. Anything already downloaded or trained is kept, so trying again is quicker.";
    } else if (ev.state === "paused") {
      $("#os-plan").textContent = "Paused while training the voice. Resume (top of the page) carries on from the same point and then makes the cover.";
    } else {
      showError(ev.message, "The one-shot cover stopped");
      previewPlan();
    }
  });
}
// after a reload: follow the one-shot that's running, or show how the last one ended if this page hasn't yet
(async () => {
  const list = await refreshJobs(true);
  const run = list.find(j => j.kind === "auto" && isRunning(j));
  const ended = list.find(j => j.kind === "auto" && !isRunning(j));
  if (run) osFollow(run.id, run.title);
  else if (ended && store.get("uc-os-seen", null) !== ended.id) osFollow(ended.id, ended.title);
})();

// ------------------------------------------------------------ songs from YouTube
async function ytSearch() {
  const q = $("#yt-q").value.trim();
  if (!q) return $("#yt-q").focus();
  const list = $("#yt-list"), btn = $("#yt-go");
  if (/^https?:\/\//i.test(q)) return ytUse({ url: q }, btn);
  btn.disabled = true; btn.textContent = "Searching…";
  try {
    const res = await api(`/api/songs/search?q=${encodeURIComponent(q)}`);
    list.hidden = false;
    list.innerHTML = res.map((r, i) => {
      const warn = /music video|longer|live/.test(r.note);
      const sub = r.kind === "track"
        ? `<span class="tag ok">album track</span> ${esc(r.album || "")}${r.views ? ` · ${fmtPlays(r.views)} plays` : ""}`
        : `${esc(r.channel)}${r.note ? ` · <span class="tag${warn ? " warn" : ""}">${esc(r.note)}</span>` : ""}`;
      return `<div class="ytr${i === 0 ? " best" : ""}"><span><b>${esc(r.title)}</b><small>${sub}</small></span>
        <span class="mono meta">${fmtTime(r.duration)}</span><button class="btn${i === 0 ? " primary" : ""}" data-i="${i}">Use</button></div>`;
    }).join("");
    $$("button[data-i]", list).forEach(b => b.onclick = () => ytUse(res[+b.dataset.i], b));
  } catch (e) { showError(e.message, "YouTube search failed"); }
  finally { btn.disabled = false; btn.textContent = "Search"; }
}
const fmtPlays = n => n >= 1e9 ? (n / 1e9).toFixed(1) + "B" : n >= 1e6 ? Math.round(n / 1e6) + "M" : n >= 1e3 ? Math.round(n / 1e3) + "K" : n;
async function ytUse(result, btn) {
  if (S.job || S.osBusy) return showError("Wait for the current cover to finish (or cancel it) before loading another song.", "Busy");
  const label = btn.textContent;
  btn.disabled = true; btn.textContent = "Getting…";
  try {
    const meta = await api("/api/songs/youtube", { json: result });
    S.song = meta; store.set("uc-song", meta.id); renderSong(); refreshEstimate(); presep();
    $("#yt-list").hidden = true; $("#yt-q").value = "";
  } catch (e) { showError(e.message, "Couldn't get that song"); }
  finally { btn.disabled = false; btn.textContent = label; }
}
$("#yt-go").onclick = () => ytSearch();
$("#yt-q").addEventListener("keydown", e => { if (e.key === "Enter") ytSearch(); });
