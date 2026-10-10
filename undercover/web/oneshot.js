"use strict";
// One-shot covers ("cover of X by Y") and songs from YouTube. Uses helpers from app.js
// ($, $$, esc, api, showError, store, fmtTime, fmtSecs, S, renderSong, refreshEstimate, loadVoices, showResult, loadHistory).

const OS = { es: null, job: null, timer: null, t0: 0, planSeq: 0, planTimer: null, last: null };

// ------------------------------------------------------------ plan preview (no downloads)
function voiceLine(v) {
  if (!v) return "";
  if (v.how === "installed") return `<b>${esc(v.name)}</b> <span class="meta">your voice${v.trained ? ", trained here" : ""}</span>`;
  if (v.how === "check") return `<b>${v.candidates.map(esc).join("</b> or <b>")}</b> <span class="meta">checked against real clips first (~2 min); if neither sounds like them, it trains one (~45 min, ~2 GB while training)</span>`;
  return `<span class="warn">none yet: trains <b>${esc(v.name)}</b> first (~45 min, ~2 GB while training)</span>`;
}
function planTime(p) {
  const v = p.voice?.how;
  return v === "train" ? "about 45–50 min" : v === "check" ? "about 3–5 min (45+ if it has to train)" : "about 1–3 min";
}
function planKey() { return $("#os-text").value.trim() + "|" + $("#os-train").checked; }
function resetGo() { OS.confirmFor = null; $("#os-go").textContent = "Make it"; }
async function previewPlan() {
  const text = $("#os-text").value.trim();
  const seq = ++OS.planSeq;
  const key = planKey();
  if (!text) { $("#os-plan").innerHTML = "Name a song and a voice. UnderCover gets the album version of the song from YouTube, finds (or trains) the voice, and makes the cover with the best settings."; return; }
  try {
    const p = await api(`/api/auto/plan?text=${encodeURIComponent(text)}&train=${$("#os-train").checked ? 1 : 0}`);
    if (seq !== OS.planSeq) return;
    OS.last = { key, plan: p };
    const songLine = p.style ? `${/^[aeiou]/i.test(p.style) ? "an" : "a"} <b>${esc(p.style)}</b> version of “${esc(p.song_query)}” (a cover from YouTube that has singing in it)`
                             : `“${esc(p.song_query)}” (album version from YouTube)`;
    // "Hello by Adele": the last "by" is the voice, so a request naming the song's own artist would train them by mistake
    const hint = p.voice?.how !== "installed" && /\bby\b/i.test(text) && !/cover|version|rendition|sung by|voice of|sings|sing\b/i.test(text)
      ? `<p class="meta">Not what you meant? The name after the last “by” is the voice. Try “${esc(p.song_query)} sung by …”.</p>` : "";
    $("#os-plan").innerHTML = `<div class="os-plan-grid"><span>🎵 <b>Song</b><br>${songLine}</span><span>🎤 <b>Voice</b><br>${voiceLine(p.voice)}</span><span>⏱ <b>Time</b><br>${planTime(p)}</span></div>${hint}`;
  } catch (e) {
    if (seq === OS.planSeq) { $("#os-plan").textContent = e.message; OS.last = { key, error: e.message }; }
  }
  return OS.last && OS.last.key === key ? OS.last : null;
}
$("#os-text").addEventListener("input", () => { resetGo(); clearTimeout(OS.planTimer); OS.planTimer = setTimeout(previewPlan, 500); });
$("#os-text").addEventListener("keydown", e => { if (e.key === "Enter") $("#os-go").click(); });
$("#os-train").onchange = () => { resetGo(); previewPlan(); };
$$("#os-ex button").forEach(b => b.onclick = () => { $("#os-text").value = b.textContent; previewPlan(); $("#os-text").focus(); });

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
    const how = last.plan.voice?.how;
    if ((how === "train" || how === "check") && OS.confirmFor !== key) {
      OS.confirmFor = key;
      btn.textContent = how === "train" ? "Train a voice + make it (~45 min)" : "Make it (~3 min; ~45 only if no good voice exists)";
      $("#os-plan").insertAdjacentHTML("beforeend", ` <b>Press the button again to go ahead.</b>`);
      return;
    }
    resetGo();
    const r = await api("/api/auto", { json: { text, prefer_train: $("#os-train").checked } });
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
  if (on) {
    OS.t0 = performance.now();
    OS.timer = setInterval(() => {
      const el = (performance.now() - OS.t0) / 1000;
      $("#os-elapsed").textContent = `${fmtTime(el)} elapsed` + (OS.eta ? ` · ~${fmtSecs(OS.eta)} left` : "");
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
    ? `best of ${song.compared.length} versions for this voice` + (song.compared.find(x => x.chosen)?.outside != null
        ? ` (${Math.round(song.compared.find(x => x.chosen).outside * 100)}% of the melody outside its range)` : "")
    : (song?.compared ? "" : null);
  $("small", sc).textContent = ev.stage === "song" ? ev.message : song ? [song.channel, song.note, cmp].filter(Boolean).join(" · ") : "";
  sc.title = song?.compared?.length > 1 ? song.compared.map(x => `${x.chosen ? "✓" : "·"} ${x.title}` +
    (x.outside != null ? ` · ${Math.round(x.outside * 100)}% out of range` : "") + ` · bleed ${Math.round(x.bleed)} dB`).join("\n") : "";
  $("b", vc).textContent = voice ? (voice.how === "train" && ev.stage === "voice" ? `Training ${voice.name}` : voice.name) : (plan.person || "Waiting");
  $("small", vc).textContent = ev.stage === "voice" ? ev.message
    : voice ? (voice.checked || { installed: "your installed voice", online: `community voice${voice.repo ? " · " + voice.repo : ""}`, train: "trained in UnderCover" }[voice.how]) : "";
  $("b", cc).textContent = ev.stage === "cover" ? "Making the cover" : (ev.final && ev.state === "done" ? "Done" : "Waiting");
  $("small", cc).textContent = ev.stage === "cover" && !ev.final ? ev.message
    : set ? `Best settings · ${fitWords(set)} (${set.why})` + (set.backing === "original (stacked harmonies)" ? " · original backing harmonies kept" : "") : "Best settings";
  OS.eta = ev.eta || null;
  if (!ev.final) tabTitle(`⏳ ${ev.stage === "song" ? "Song" : ev.stage === "voice" ? "Voice" : ev.stage === "cover" ? "Cover" : "Waiting"}${ev.eta ? " · " + fmtSecs(ev.eta) : ""}`);
  $("#os-foot").textContent = voice?.how === "train" && ev.stage === "voice"
    ? "Covers you start meanwhile are made right away; training waits for them. You can watch it on the Train a voice tab."
    : ev.stage === "queued" ? "Waiting for the GPU to finish the current job…" : "";
}
function osFollow(id, title) {
  if (OS.es) OS.es.close();
  OS.job = id; OS.plan = null;
  setRunning(true, title || "Making your cover");
  const es = new EventSource(`/api/jobs/${id}/events`);
  OS.es = es;
  es.onmessage = async m => {
    const ev = JSON.parse(m.data);
    if (ev.plan?.request) $("#os-h").textContent = ev.plan.request;
    renderOS(ev);
    if (!ev.final) return;
    es.close(); OS.es = null; OS.job = null;
    const plan = ev.plan || OS.plan || {};
    setRunning(false);
    tabTitle(ev.state === "done" ? "✓ Cover ready" : null);
    if (ev.state === "done") {
      $("#os-plan").innerHTML = `Done in ${fmtSecs(ev.elapsed || 0)}: <b>${esc(plan.song?.title || "")}</b> with <b>${esc(plan.voice?.name || "")}</b>` +
        (plan.settings ? ` · ${esc(fitWords(plan.settings))} (${esc(plan.settings.why)})` : "") + ". Change anything below and press Convert to try a variation.";
      try {
        if (plan.song?.id) { S.song = await api(`/api/songs/${plan.song.id}`); store.set("uc-song", S.song.id); renderSong(); }
        await loadVoices(plan.voice?.name);
        if (plan.settings) {
          // For this session only: your saved settings stay as they were ("Back to my settings" restores them).
          if (!OS.mine) OS.mine = { ...S.opts };
          Object.assign(S.opts, { octave: plan.settings.octave, key: plan.settings.key || 0, backing_voice: plan.voice?.name, karaoke: true, dereverb: true, quality: "high" });
          syncControls(); refreshEstimate();
          $("#os-plan").insertAdjacentHTML("beforeend", ` <button class="linkbtn" id="os-mine" type="button">Back to my settings</button>`);
          $("#os-mine").onclick = () => { S.opts = OS.mine; OS.mine = null; syncControls(); refreshEstimate(); $("#os-mine").remove(); };
        }
      } catch (e) {}
      if (ev.result) showResult(ev.result, ev.seconds);
      const others = (plan.song?.compared || []).filter(x => !x.chosen && x.id);
      if (others.length) {   // the versions it compared: one click to hear the same voice on another
        $("#os-plan").insertAdjacentHTML("beforeend", `<div class="os-versions"><span class="meta">Other versions it compared:</span>` +
          others.map(x => `<div class="srow"><div><b>${esc(x.title)}</b><p class="meta">${x.outside != null ? `${Math.round(x.outside * 100)}% of the melody outside the voice's range` : ""}</p></div>
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
  };
  es.onerror = () => { if (es.readyState === EventSource.CLOSED) { OS.es = null; setRunning(false); } };
}
(async () => { try { const s = await api("/api/status"); if (s.auto_job) osFollow(s.auto_job); } catch (e) {} })();

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
