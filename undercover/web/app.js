"use strict";
// UnderCover web UI. Talks to undercover/server.py (JSON API + Server-Sent Events).

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} },
};

const S = {
  song: null, voice: store.get("uc-voice", null), voices: [], defaults: {}, opts: {},
  job: null, es: null, estimate: null, result: null, online: [], searchSeq: 0,
};

// ------------------------------------------------------------ api helpers
async function api(path, opts = {}) {
  const init = { ...opts, headers: { ...(opts.headers || {}) } };
  if (opts.json !== undefined) { init.method = init.method || "POST"; init.body = JSON.stringify(opts.json); init.headers["Content-Type"] = "application/json"; }
  let r;
  try { r = await fetch(path, init); }
  catch (e) { throw new Error("Lost contact with UnderCover. Is the UnderCover window still open? Start it again with UnderCover.bat."); }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `Request failed (${r.status}).`);
  return data;
}
// action: optional { label, run } shown as a button in the banner; info: accent stripe instead of red
function showError(msg, title = "Something went wrong", action = null, info = false) {
  $("#err-title").textContent = title; $("#err-msg").textContent = msg; $("#err").hidden = false;
  $("#err").classList.toggle("info", info);
  const b = $("#err-action");
  b.hidden = !action; b.disabled = false;
  if (action) { b.textContent = action.label; b.onclick = action.run; }
  $("#err").scrollIntoView({ block: "nearest", behavior: "smooth" });
}
$("#err-x").onclick = () => { $("#err").hidden = true; };

const fmtTime = s => { s = Math.max(0, Math.round(s || 0)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; };
const fmtSecs = s => s >= 90 ? `${Math.round(s / 60)} min` : `${Math.max(1, Math.round(s))} s`;
const fmtPitch = v => { v = +v; return (v > 0 ? "+" : v < 0 ? "−" : "±") + Math.abs(v); };
const fmtMB = b => b ? `${Math.round(b / 1e6)} MB` : "";

// ------------------------------------------------------------ progress in the browser tab title
// Long jobs (a training is ~45 min) are rarely watched: the tab says how far along it is, then "✓ Done" until seen.
function tabTitle(text) {
  document.title = text ? `${text} · UnderCover` : "UnderCover";
  if (text && text.startsWith("✓") && !document.hidden) setTimeout(() => { if (document.title.startsWith("✓")) document.title = "UnderCover"; }, 4000);
}
document.addEventListener("visibilitychange", () => { if (!document.hidden && document.title.startsWith("✓")) setTimeout(() => tabTitle(null), 4000); });

// ------------------------------------------------------------ status + theme
async function pollStatus() {
  try {
    const s = await api("/api/status");
    const gpu = $("#chip-gpu");
    gpu.innerHTML = s.gpu ? `<i class="dot"></i><span>${esc(s.gpu.replace("AMD Radeon ", ""))} · ${s.vram_gb} GB</span>`
                  : s.cpu_mode ? `<i class="dot wait"></i><span title="No supported graphics card: covers are slow and training is off">Processor only (slow)</span>`
                          : `<i class="dot wait"></i><span>Checking GPU…</span>`;
    if (s.stale) restartNeeded();
    if (s.last_abrupt && store.get("uc-abrupt-seen", null) !== s.last_abrupt.at) {   // once per incident
      store.set("uc-abrupt-seen", s.last_abrupt.at);
      showError(`Last time, UnderCover stopped suddenly at ${s.last_abrupt.at} while doing: ${s.last_abrupt.during}. That usually means the PC lost power or froze. ` +
                "An unfinished training can be resumed on Train a voice; logs/heartbeat.jsonl has the details.", "UnderCover didn't shut down cleanly", null, true);
    }
    const tr = $("#chip-train"), t = s.training;   // training is visible from any tab
    tr.hidden = !t;
    if (t) tr.innerHTML = `<i class="dot wait"></i><span>Training ${esc(t.project)}${t.eta ? ` · ${fmtSecs(t.eta)} left` : t.epoch ? ` · ${t.epoch}/${t.epochs}` : ""}</span>`;
    if (t && !S.job && !S.osBusy) tabTitle(`⏳ Training${t.eta ? " · " + fmtSecs(t.eta) : ""}`);
    else if (!t && S.wasTraining && !S.job && !S.osBusy) tabTitle("✓ Training done");
    S.wasTraining = !!t;
    const m = $("#chip-models");
    if (s.models === "ready") m.textContent = s.queue ? `${s.queue} queued` : "models warm";
    else if (s.models.startsWith("error")) { m.textContent = s.gpu_problem ? "no GPU" : "model error"; showError(s.models.replace(/^error: /, ""), s.gpu_problem ? "GPU not available" : "Models failed to load"); return; }
    else m.textContent = s.first_run ? "first start: downloading voice tools (~600 MB, once)…" : "loading models…";
    S.modelsReady = s.models === "ready"; S.firstRun = !!s.first_run;
    setTimeout(pollStatus, s.models === "ready" ? 5000 : 1000);
  } catch (e) {
    $("#chip-gpu").innerHTML = `<i class="dot bad"></i><span>Not connected</span>`;
    setTimeout(pollStatus, 3000);
  }
}
let restartShown = false;
function restartNeeded() {
  if (restartShown) return;
  restartShown = true;
  showError("UnderCover was updated while it was running, so some settings won't take effect until it restarts. " +
            "Restarting takes about 10 seconds; your songs, voices and history are kept.", "Restart needed",
            { label: "Restart now", run: restartServer }, true);
}
async function restartServer() {
  const btn = $("#err-action");
  btn.disabled = true; btn.textContent = "Restarting…";
  let before = null;
  try { before = (await api("/api/status")).started; } catch (e) {}
  try { await api("/api/restart", { method: "POST" }); }
  catch (e) {
    // A server from before this feature has no restart endpoint (404); anything else (e.g. busy) is explained by e.message.
    const old = /not found/i.test(e.message);
    showError(old ? "This copy of UnderCover is too old to restart itself. Start it again with UnderCover.bat or the desktop shortcut." : e.message,
              "Couldn't restart", old ? null : { label: "Try again", run: restartServer }, !old);
    return;
  }
  $("#err-msg").textContent = "Restarting UnderCover and loading the models. The page will reload by itself.";
  const t0 = Date.now();
  while (Date.now() - t0 < 90000) {
    await new Promise(r => setTimeout(r, 1000));
    try {
      // timeout: a request caught by the old process as it exits can otherwise hang forever
      const ctl = new AbortController(), timer = setTimeout(() => ctl.abort(), 3000);
      const s = await (await fetch("/api/status", { cache: "no-store", signal: ctl.signal })).json();
      clearTimeout(timer);
      if (s.started && s.started !== before) { location.reload(); return; }
    } catch (e) { /* down or timed out while it restarts */ }
  }
  showError("UnderCover didn't come back after restarting. Start it again with UnderCover.bat or the desktop shortcut. " +
            "If that fails, logs/server.log has the details.", "Restart failed");
}
function applyTheme() { $("#theme").textContent = document.documentElement.dataset.theme === "light" ? "Dark" : "Light"; redrawAll(); }
$("#chip-train").onclick = () => { if (window.showTab) showTab("train"); };
$("#theme").onclick = () => {
  const light = document.documentElement.dataset.theme !== "light";
  if (light) document.documentElement.dataset.theme = "light"; else delete document.documentElement.dataset.theme;
  try { localStorage.setItem("uc-theme", light ? "light" : "dark"); } catch (e) {}
  applyTheme();
};

// ------------------------------------------------------------ waveforms
function drawWave(canvas, peaks, played = 0, colorVar = "--wave-on") {
  const dpr = window.devicePixelRatio || 1, w = canvas.clientWidth, h = canvas.clientHeight;
  if (!w) return;
  canvas.width = w * dpr; canvas.height = h * dpr;
  const g = canvas.getContext("2d"); g.scale(dpr, dpr);
  const css = getComputedStyle(document.documentElement);
  const off = css.getPropertyValue("--wave").trim(), on = css.getPropertyValue(colorVar).trim();
  if (!peaks || !peaks.length) { g.fillStyle = off; g.fillRect(0, h / 2 - 1, w, 2); return; }
  const bars = Math.max(1, Math.floor(w / 3));
  for (let i = 0; i < bars; i++) {
    const a = Math.floor(i / bars * peaks.length), b = Math.max(a + 1, Math.floor((i + 1) / bars * peaks.length));
    let v = 0; for (let j = a; j < b; j++) v = Math.max(v, peaks[j]);
    v = Math.max(0.04, v);
    g.fillStyle = i / bars < played ? on : off;
    g.fillRect(i * 3, h / 2 - v * h / 2, 2, v * h);
  }
}
async function peaksFromUrl(url, bins = 1200) {
  const buf = await (await fetch(url)).arrayBuffer();
  const ctx = new (window.OfflineAudioContext || window.webkitOfflineAudioContext)(1, 2, 44100);
  const audio = await ctx.decodeAudioData(buf);
  const ch = audio.getChannelData(0), step = Math.max(1, Math.floor(ch.length / bins)), out = [];
  let top = 0;
  for (let i = 0; i < bins; i++) {
    let m = 0; const end = Math.min(ch.length, (i + 1) * step);
    for (let j = i * step; j < end; j += 4) { const v = Math.abs(ch[j]); if (v > m) m = v; }
    out.push(m); if (m > top) top = m;
  }
  return out.map(v => v / (top || 1));
}

// ------------------------------------------------------------ song upload
const fileInput = $("#file");
$("#drop").addEventListener("click", e => { if (!S.song || e.target.id === "song-replace") fileInput.click(); });
$("#drop").addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileInput.click(); } });
$("#song-replace").onclick = e => { e.stopPropagation(); fileInput.click(); };
fileInput.onchange = () => { if (fileInput.files[0]) upload(fileInput.files[0]); fileInput.value = ""; };

let dragDepth = 0;
addEventListener("dragenter", e => { if (e.dataTransfer?.types?.includes("Files")) { dragDepth++; $("#drop-overlay").hidden = false; } });
addEventListener("dragleave", () => { if (--dragDepth <= 0) { dragDepth = 0; $("#drop-overlay").hidden = true; } });
addEventListener("dragover", e => e.preventDefault());
addEventListener("drop", e => {
  e.preventDefault(); dragDepth = 0; $("#drop-overlay").hidden = true;
  if (!$("#view-train").hidden && window.trainDrop) return window.trainDrop([...(e.dataTransfer?.files || [])]);
  const f = e.dataTransfer?.files?.[0]; if (!f) return;
  if (/\.(zip|pth)$/i.test(f.name)) installFile(f); else upload(f);
});

async function installFile(file) {
  const name = file.name.replace(/\.(zip|pth)$/i, "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 40) || "voice";
  $("#online-h").hidden = $("#online").hidden = false;
  $("#online-h").textContent = "Installing from file";
  $("#online").innerHTML = `<p class="meta">Installing ${esc(file.name)}…</p>`;
  try {
    const r = await fetch("/api/voices/upload", { method: "POST", body: file,
      headers: { "X-Filename": encodeURIComponent(file.name), "X-Name": encodeURIComponent(name) } });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || "Install failed.");
    $("#online-h").hidden = $("#online").hidden = true;
    await loadVoices(data.voice);
  } catch (e) {
    $("#online-h").hidden = $("#online").hidden = true;
    showError(e.message, "That voice couldn't be installed");
  }
}

function setDrop(mode) {
  $("#drop-empty").hidden = mode !== "empty";
  $("#drop-loaded").hidden = mode !== "loaded";
  $("#drop-uploading").hidden = mode !== "uploading";
  $("#drop").classList.toggle("loaded", mode !== "empty");
}
// separate a new song in the background while a voice is picked (the server only does it when the GPU is idle)
function presep() {
  if (!S.song) return;
  const id = S.song.id;
  api(`/api/songs/${id}/presep`, { json: { options: S.opts } }).then(r => {
    if (!r.started) return;
    const es = new EventSource(`/api/jobs/${r.job}/events`);
    es.onmessage = async m => {
      const ev = JSON.parse(m.data);
      if (!ev.final) return;
      es.close();
      if (ev.state === "done" && S.song?.id === id) {   // show "stems already separated" and the shorter estimate
        try { S.song = await api(`/api/songs/${id}`); renderSong(); } catch (e) {}
        refreshEstimate();
      }
    };
    es.onerror = () => es.close();
  }).catch(() => {});
}
function upload(file) {
  if (S.job || S.osBusy) return showError("Wait for the current cover to finish (or cancel it) before loading another song.", "Busy");
  setDrop("uploading");
  $("#up-name").textContent = file.name; $("#up-pct").textContent = "0%"; $("#up-bar").style.width = "0%";
  $("#up-note").textContent = "Loading…";
  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/upload");
  xhr.setRequestHeader("X-Filename", encodeURIComponent(file.name));
  xhr.upload.onprogress = e => { if (e.lengthComputable) { const p = Math.round(e.loaded / e.total * 100); $("#up-pct").textContent = p + "%"; $("#up-bar").style.width = p + "%"; if (p === 100) $("#up-note").textContent = "Reading the audio…"; } };
  xhr.onload = () => {
    let data = {}; try { data = JSON.parse(xhr.responseText); } catch (e) {}
    if (xhr.status !== 200) { setDrop(S.song ? "loaded" : "empty"); return showError(data.error || "The upload failed.", "Couldn't load that song"); }
    S.song = data; store.set("uc-song", data.id); renderSong(); refreshEstimate(); presep();
  };
  xhr.onerror = () => { setDrop(S.song ? "loaded" : "empty"); showError("Lost contact with UnderCover while uploading. Is its window still open?", "Couldn't load that song"); };
  xhr.send(file);
}
function renderSong() {
  if (!S.song) return setDrop("empty");
  setDrop("loaded");
  const s = S.song;
  $("#song-name").textContent = s.name;
  const base = `${fmtTime(s.duration)} · ${(s.sample_rate / 1000).toFixed(s.sample_rate % 1000 ? 1 : 0)} kHz · ${s.channels === 1 ? "mono" : s.channels === 2 ? "stereo" : s.channels + " ch"} · ${s.lufs} LUFS`;
  $("#song-meta").textContent = base;
  S.songKey = null;
  api(`/api/songs/${s.id}/key`).then(k => {
    if (S.song?.id !== s.id) return;
    S.songKey = k; $("#song-meta").textContent = `${base} · key ${k.name}`;
    $("#key-o").textContent = keyLabel(S.opts.key || 0);
  }).catch(() => {});
  drawWave($("#wave-song"), s.peaks, 0);
  summary();
}

// ------------------------------------------------------------ voices
async function loadVoices(select) {
  S.voices = await api("/api/voices");
  S.voicesLoaded = true;
  if (select) S.voice = select;
  if (S.voice && !S.voices.some(v => v.name === S.voice)) S.voice = null;
  if (!S.voice && S.voices.length === 1) S.voice = S.voices[0].name;
  store.set("uc-voice", S.voice);
  renderVoices(); renderBackingSelect(); summary(); refreshEstimate();
}
function voiceDesc(v) {
  const src = (v.source || "").match(/huggingface\.co\/([^/]+\/[^/]+)/);
  return `RVC ${v.version} · ${v.sr ? v.sr / 1000 + " kHz" : "?"}${src ? " · " + src[1].split("/")[1].replace(/_+/g, " ").slice(0, 40) : ""}`;
}
function renderVoices() {
  const q = $("#q").value.trim().toLowerCase();
  const isLink = /^https?:\/\//i.test(q);
  const list = isLink ? [] : S.voices.filter(v => !q || v.name.includes(q.replace(/\s+/g, "-")) || v.name.includes(q));
  const box = $("#voices");
  if (!S.voices.length && !q) box.innerHTML = `<p class="meta">No voices yet. Search for one here (try “spongebob”), drop a .zip/.pth, train your own on <b>Train a voice</b>, or just type a request in the box at the top of the page.</p>`;
  else if (!list.length && !isLink) box.innerHTML = `<p class="meta">No installed voice matches “${esc(q)}”.</p>`;
  else box.innerHTML = list.map(v => `
    <div class="voice pick" role="option" tabindex="0" aria-selected="${v.name === S.voice}" data-v="${esc(v.name)}">
      <span class="avatar">${esc(v.name[0].toUpperCase())}</span>
      <span class="vtext"><b>${esc(v.name)}</b><small>${esc(voiceDesc(v))}</small></span>
      <span class="vside"><button class="remove" data-rm="${esc(v.name)}" aria-label="Remove ${esc(v.name)}">Remove</button><span class="tag">${v.index ? "index" : "no index"}</span></span>
    </div>`).join("");
  $$(".voice.pick", box).forEach(el => {
    const pick = () => { S.voice = el.dataset.v; store.set("uc-voice", S.voice); renderVoices(); summary(); refreshEstimate(); };
    el.addEventListener("click", e => { if (!e.target.closest(".remove")) pick(); });
    el.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(); } });
  });
  $$(".remove", box).forEach(b => b.onclick = async e => {
    e.stopPropagation();
    if (!b.classList.contains("confirm")) { b.classList.add("confirm"); b.textContent = "Confirm remove"; setTimeout(() => { b.classList.remove("confirm"); b.textContent = "Remove"; }, 4000); return; }
    try { await api(`/api/voices/${encodeURIComponent(b.dataset.rm)}`, { method: "DELETE" }); await loadVoices(); }
    catch (err) { showError(err.message, "Couldn't remove the voice"); }
  });
  renderOnline(q, isLink);
}

let searchTimer = null;
$("#q").addEventListener("input", () => {
  renderVoices();
  clearTimeout(searchTimer);
  const q = $("#q").value.trim();
  if (q.length >= 3 && !/^https?:\/\//i.test(q)) searchTimer = setTimeout(() => searchOnline(q), 450);
});
$("#q").addEventListener("keydown", e => { if (e.key === "Enter") { clearTimeout(searchTimer); const q = $("#q").value.trim(); if (q.length >= 2 && !/^https?:\/\//i.test(q)) searchOnline(q); } });

async function searchOnline(q) {
  const seq = ++S.searchSeq;
  S.online = null; renderOnline(q.toLowerCase(), false);
  try {
    const res = await api(`/api/search?q=${encodeURIComponent(q)}`);
    if (seq !== S.searchSeq) return;
    S.online = res; S.onlineQ = q.toLowerCase();
  } catch (e) { if (seq === S.searchSeq) { S.online = []; showError(e.message, "Search failed"); } }
  renderOnline($("#q").value.trim().toLowerCase(), false);
}
function renderOnline(q, isLink) {
  const head = $("#online-h"), box = $("#online");
  if (isLink) {
    head.hidden = box.hidden = false; head.textContent = "Install from link";
    const guess = (q.split(/[?#]/)[0].split("/").pop() || "voice").replace(/\.(zip|pth)$/i, "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 40) || "voice";
    box.innerHTML = `<div class="voice"><span class="avatar">↓</span>
      <span class="vtext"><b>Link</b><small class="mono">${esc(q.slice(0, 80))}</small></span>
      <span class="vside"><input class="name-in" id="link-name" value="${esc(guess)}" aria-label="Voice name"><button class="install" id="link-install">Install</button></span></div>`;
    $("#link-install").onclick = () => install($("#link-install"), { url: $("#q").value.trim(), name: $("#link-name").value.trim() });
    return;
  }
  if (q.length < 3) { head.hidden = box.hidden = true; return; }
  head.hidden = box.hidden = false;
  if (S.online === null) { head.textContent = "Searching Hugging Face…"; box.innerHTML = ""; return; }
  if (!S.online || S.onlineQ !== q) { head.textContent = "Press Enter to search online"; box.innerHTML = ""; return; }
  head.textContent = S.online.length ? `Online · Hugging Face · ${S.online.length} results` : "Nothing found online. Try a shorter name.";
  const installedNames = new Set(S.voices.map(v => v.name));
  box.innerHTML = S.online.map(r => `
    <div class="voice"><span class="avatar">${esc(r.name[0]?.toUpperCase() || "?")}</span>
      <span class="vtext"><b>${esc(r.name)}</b><small class="mono">${esc(r.repo)}${r.size ? " · " + fmtMB(r.size) : ""}</small>${infoChips(r)}</span>
      <span class="vside">${installedNames.has(r.name) ? `<span class="tag">installed</span>` : `<button class="install" data-n="${r.n}">Install</button>`}</span></div>`).join("");
  // send the exact result (not its position): other searches (e.g. the one-shot plan) replace the server's last list
  $$(".install[data-n]", box).forEach(b => b.onclick = () => {
    const r = S.online.find(x => x.n === +b.dataset.n);
    install(b, r ? { url: r.url, name: r.name } : { n: +b.dataset.n });
  });
}

function install(btn, body) {
  btn.disabled = true; btn.textContent = "Starting…";
  api("/api/voices/install", { json: body }).then(({ job }) => {
    const es = new EventSource(`/api/jobs/${job}/events`);
    es.onmessage = async m => {
      const ev = JSON.parse(m.data);
      if (ev.stage === "downloading") btn.textContent = ev.frac != null ? `${Math.round(ev.frac * 100)}%` : ev.message.replace("Downloading ", "");
      if (ev.final) {
        es.close();
        if (ev.state === "done") { btn.textContent = "Installed"; $("#q").value = ""; S.online = []; await loadVoices(ev.voice); }
        else { btn.disabled = false; btn.textContent = "Install"; if (ev.state === "error") showError(ev.message, "That voice couldn't be installed"); }
      }
    };
    es.onerror = () => { es.close(); btn.disabled = false; btn.textContent = "Install"; };
  }).catch(e => { btn.disabled = false; btn.textContent = "Install"; showError(e.message, "That voice couldn't be installed"); });
}

// ------------------------------------------------------------ advanced options
const SLIDERS = { key: v => keyLabel(+v),
                  pitch_correction: v => +v ? (+v >= 1 ? "full" : (+v).toFixed(1)) : "off", index_ratio: v => (+v).toFixed(2), protect: v => +v >= 0.5 ? "off" : (+v).toFixed(2),
                  volume_envelope: v => +v <= 0.15 ? "original's" : +v >= 0.85 ? "voice's own" : (+v).toFixed(2),
                  backing_db: v => +v ? `${+v > 0 ? "+" : "−"}${Math.abs(+v)} dB` : "0 dB" };
const SEGS = ["quality", "format", "octave", "backing_octave", "reverb"];
const CHECKS = ["karaoke", "dereverb", "pitch_cleanup", "polish", "fit"];
function loadOpts() {
  const saved = store.get("uc-opts", {});
  delete saved.pitch;  // old vocal-only pitch setting (put the vocal out of key); replaced by key + octave
  if (typeof saved.reverb === "number" && ![0.15, 0.3].includes(saved.reverb)) delete saved.reverb;  // old slider value -> new Auto default
  S.opts = { ...S.defaults, ...saved };
  syncControls();
}
function syncControls() {
  for (const k in SLIDERS) { $("#" + k).value = S.opts[k]; $(`#${k}-o`).textContent = SLIDERS[k](S.opts[k]); }
  for (const k of CHECKS) $("#" + k).checked = !!S.opts[k];
  renderBackingSelect();
  for (const id of SEGS) $$(`#${id} button`).forEach(b => b.setAttribute("aria-pressed", b.dataset.v === String(S.opts[id])));
  fitState();
  summary();
}
// "Fit pitch to the voice": octave and key are chosen per song, so the manual controls step back
function fitState() {
  $$("[data-pitch]").forEach(f => { f.classList.toggle("off", !!S.opts.fit); f.querySelectorAll("button, input").forEach(c => c.disabled = !!S.opts.fit); });
}
function saveOpts() { store.set("uc-opts", S.opts); summary(); refreshEstimate(); }
for (const k in SLIDERS) $("#" + k).addEventListener("input", e => { S.opts[k] = +e.target.value; $(`#${k}-o`).textContent = SLIDERS[k](e.target.value); saveOpts(); });
for (const k of CHECKS) $("#" + k).addEventListener("change", e => { S.opts[k] = e.target.checked; if (k === "fit") fitState(); saveOpts(); });
for (const id of SEGS) $("#" + id).addEventListener("click", e => {
  const b = e.target.closest("button"); if (!b) return;
  $$(`#${id} button`).forEach(x => x.setAttribute("aria-pressed", x === b));
  S.opts[id] = id.endsWith("octave") ? +b.dataset.v : id === "reverb" ? (b.dataset.v === "auto" ? "auto" : +b.dataset.v) : b.dataset.v;
  saveOpts();
});
// Backing vocals: optional second voice. It needs the lead/backing split, so that option is forced on.
function renderBackingSelect() {
  const sel = $("#backing_voice");
  if (S.voicesLoaded && S.opts.backing_voice && !S.voices.some(v => v.name === S.opts.backing_voice)) S.opts.backing_voice = "";
  sel.innerHTML = `<option value="">Original singer</option>` + S.voices.map(v => `<option value="${esc(v.name)}">${esc(v.name)}</option>`).join("");
  sel.value = S.opts.backing_voice || "";
  const on = !!S.opts.backing_voice;
  $("#backing-opts").hidden = !on;
  $("#backing-note").textContent = on
    ? "Lead and backing are split automatically. The backing voice follows one note at a time, so big stacked harmonies come out thinner."
    : "Pick a second voice to sing the backing vocals, or keep the original singer's backing.";
  $("#karaoke").checked = on || !!S.opts.karaoke;
  $("#karaoke").disabled = on;
}
$("#backing_voice").addEventListener("change", e => { S.opts.backing_voice = e.target.value; renderBackingSelect(); saveOpts(); });
$("#reset").onclick = () => { store.set("uc-opts", {}); loadOpts(); refreshEstimate(); };

function summary() {
  const o = S.opts;
  $("#adv-sum").textContent = `${o.fit ? "pitch fit to the voice" : pitchText(o)} · likeness ${(+(o.index_ratio ?? 0)).toFixed(2)} · ${o.quality || "high"} quality` +
    (o.backing_voice ? ` · backing by ${o.backing_voice}` : o.karaoke ? " · lead only" : "") + (o.dereverb ? " · de-reverb" : "") + (+o.reverb ? " · reverb" : "") + ` · ${(o.format || "mp3").toUpperCase()}`;
  const ready = S.song && S.voice;
  const busy = !!S.job || !!S.osBusy;   // a one-shot cover uses the GPU queue too
  $("#convert").disabled = !ready || busy;
  $("#preview-btn").disabled = !ready || busy;
  if (S.osBusy) { $("#summary").textContent = "Busy with the one-shot cover at the top of the page…"; return; }
  if (!S.song && !S.voice) $("#summary").textContent = "Drop a song and pick a voice.";
  else if (!S.song) $("#summary").innerHTML = `Voice: <b>${esc(S.voice)}</b>. Now drop a song.`;
  else if (!S.voice) $("#summary").innerHTML = `<b>${esc(S.song.title)}</b>. Now pick a voice.`;
  else {
    const est = S.estimate ? ` · about ${fmtSecs(S.estimate.total)}${S.estimate.stems_cached ? " (stems cached)" : ""}` : "";
    const bk = S.opts.backing_voice ? ` with <b>${esc(S.opts.backing_voice)}</b> on backing vocals` : "";
    $("#summary").innerHTML = `<b>${esc(S.song.title)}</b> sung by <b>${esc(S.voice)}</b>${bk} <span class="meta">· ${pitchText(o)}${est}</span>`;
  }
}
function pitchText(o) {
  const k = +(o.key || 0), oct = +(o.octave || 0);
  const parts = [k ? `key ${fmtPitch(k)}` : "original key"];
  if (oct) parts.push(`voice ${oct > 0 ? "+" : "−"}${Math.abs(oct)} oct`);
  return parts.join(", ");
}
const NOTES = "C C# D D# E F F# G G# A A# B".split(" ");
function keyLabel(k) {
  const shift = k ? `${fmtPitch(k)} st` : "original";
  if (!S.songKey) return shift;
  const to = `${NOTES[((S.songKey.tonic + k) % 12 + 12) % 12]} ${S.songKey.mode}`;
  return k ? `${shift} (${S.songKey.name} → ${to})` : `original (${S.songKey.name})`;
}
function infoChips(r) {
  const c = [r.version, r.sr, r.epochs ? `${r.epochs >= 1000 ? r.epochs / 1000 + "k" : r.epochs} epochs` : null,
             r.steps ? `${Math.round(r.steps / 1000)}k steps` : null].filter(Boolean);
  return c.length ? `<span class="chiprow">${c.map(x => `<span class="tag">${esc(x)}</span>`).join("")}</span>` : "";
}
let estTimer = null;
function refreshEstimate() {
  clearTimeout(estTimer);
  if (!S.song || !S.voice) { S.estimate = null; summary(); return; }
  estTimer = setTimeout(async () => {
    try {
      S.estimate = await api("/api/estimate", { json: { song: S.song.id, voice: S.voice, options: S.opts } });
      $("#song-cached").hidden = !S.estimate.stems_cached;
    } catch (e) { S.estimate = null; }
    summary();
  }, 250);
}

// ------------------------------------------------------------ convert + progress
const ORDER = ["separating", "converting", "mixing"];
let elapsedTimer = null, jobStart = 0;
$("#convert").onclick = async () => {
  if (!S.song || !S.voice || S.job || S.osBusy) return;
  $("#err").hidden = true;
  try {
    const { job } = await api("/api/covers", { json: { song: S.song.id, voice: S.voice, options: S.opts } });
    S.job = job; summary(); startProgress(job);
  } catch (e) { showError(e.message, "Couldn't start the cover"); }
};
$("#cancel").onclick = () => { if (S.job) { api(`/api/jobs/${S.job}/cancel`, { method: "POST" }).catch(() => {}); $("#cancel").textContent = "Cancelling…"; } };

function setStage(name, cls, frac, eta) {
  const el = $(`.stage[data-s=${name}]`);
  el.className = "stage" + (cls ? " " + cls : "");
  if (frac != null) $(".bar i", el).style.width = Math.round(frac * 100) + "%";
  if (eta != null) $(".eta", el).textContent = eta;
}
function startProgress(job) {
  $("#progress").hidden = false; $("#progress-h").textContent = "Working"; $("#cancel").textContent = "Cancel"; $("#cancel").hidden = false;
  const est = S.estimate || {};
  ORDER.forEach(s => setStage(s, "", 0, est[s] != null ? (s === "separating" && est.stems_cached && !S.opts.karaoke && !S.opts.dereverb ? "cached" : `≈ ${fmtSecs(est[s])}`) : "—"));
  $(".stage[data-s=converting] .sname").textContent = `Converting to ${S.voice}`;
  $("#progress").scrollIntoView({ block: "nearest", behavior: "smooth" });
  jobStart = performance.now(); let totalLeft = est.total;
  clearInterval(elapsedTimer);
  elapsedTimer = setInterval(() => {
    const el = (performance.now() - jobStart) / 1000;
    $("#elapsed").textContent = `${el.toFixed(0)} s elapsed` + (totalLeft != null ? ` · about ${fmtSecs(Math.max(1, totalLeft))} to go` : "");
  }, 250);
  let current = null, lastStageStart = 0;
  const es = new EventSource(`/api/jobs/${job}/events`);
  S.es = es;
  es.onmessage = m => {
    const ev = JSON.parse(m.data);
    if (ev.estimate) { S.estimate = ev.estimate; totalLeft = ev.estimate.total; }
    if (ORDER.includes(ev.stage)) {
      if (ev.stage !== current) {
        ORDER.slice(0, ORDER.indexOf(ev.stage)).forEach(s => { const el = $(`.stage[data-s=${s}]`); if (!el.classList.contains("done")) setStage(s, "done", 1, el.dataset.took || "done"); });
        if (current) $(`.stage[data-s=${current}]`).dataset.took = `${(ev.elapsed - lastStageStart).toFixed(1)} s`;
        current = ev.stage; lastStageStart = ev.elapsed;
        $("#sr-live").textContent = $(`.stage[data-s=${ev.stage}] .sname`).textContent + "…";
      }
      $(`.stage[data-s=${ev.stage}] .sname`).textContent = ev.message;
      setStage(ev.stage, ev.frac >= 1 ? "done" : "active", ev.frac, ev.frac >= 1 ? `${(ev.elapsed - lastStageStart).toFixed(1)} s` : `${fmtSecs(Math.max(1, ev.stage_left))} left`);
      totalLeft = ev.total_left;
      if (totalLeft != null) tabTitle(`⏳ ${fmtSecs(Math.max(1, totalLeft))}`);
    }
    if (ev.final) {
      es.close(); S.es = null; clearInterval(elapsedTimer); S.job = null; summary(); tabTitle(null);
      if (ev.state === "done") {
        const sec = ev.seconds || {};
        const took = { separating: sec.separate < 0.5 ? "cached" : sec.separate, converting: sec.convert, mixing: sec.remix };
        ORDER.forEach(s => setStage(s, "done", 1, typeof took[s] === "number" ? `${took[s].toFixed(1)} s` : (took[s] || "done")));
        $("#progress-h").textContent = "Done"; $("#cancel").hidden = true;
        $("#sr-live").textContent = "Your cover is ready.";
        tabTitle("✓ Cover ready");
        $("#elapsed").textContent = `Finished in ${ev.elapsed.toFixed(1)} s`;
        if (ev.result) showResult(ev.result, ev.seconds); else loadHistory();
        if (ev.notes?.length) showError(ev.notes.join(" "), "Heads up", null, true);
        refreshEstimate();
      } else if (ev.state === "cancelled") {
        $("#progress").hidden = true;
      } else {
        $("#progress").hidden = true;
        showError(ev.message, "The cover didn't finish");
      }
    }
  };
  es.onerror = () => {
    if (!S.job) return;
    es.close(); clearInterval(elapsedTimer); S.job = null; summary(); $("#progress").hidden = true;
    showError("Lost contact with UnderCover during the cover. If you closed its window, start it again with UnderCover.bat.", "The cover didn't finish");
  };
}

// ------------------------------------------------------------ result player (A/B)
const aCover = $("#a-cover"), aOrig = $("#a-orig");

// Volume: one level for both players so Cover/Original comparisons stay fair; remembered per browser.
const VOL = { level: store.get("uc-volume", 0.8), muted: store.get("uc-muted", false) };
function applyVolume() {
  const v = VOL.muted ? 0 : VOL.level;
  aCover.volume = aOrig.volume = v * v;  // squared: a slider position sounds closer to proportional loudness
  $$("#preview-list audio").forEach(a => { a.volume = v * v; });
  $("#volume").value = VOL.muted ? 0 : VOL.level;
  const silent = VOL.muted || VOL.level === 0;
  $("#vol-waves").toggleAttribute("hidden", silent); $("#vol-x").toggleAttribute("hidden", !silent);
  $("#mute").setAttribute("aria-label", silent ? "Unmute" : "Mute");
  store.set("uc-volume", VOL.level); store.set("uc-muted", VOL.muted);
}
$("#volume").addEventListener("input", e => { VOL.level = +e.target.value; VOL.muted = false; applyVolume(); });
$("#mute").onclick = () => { if (VOL.level === 0) { VOL.level = 0.8; VOL.muted = false; } else VOL.muted = !VOL.muted; applyVolume(); };
applyVolume();
let ab = "cover", wavePeaks = { cover: null, orig: null }, playingHist = null;
const active = () => ab === "cover" ? aCover : aOrig;

async function showResult(item, seconds) {
  S.result = item; playingHist = item.id;
  $("#result").hidden = false;
  $("#result-h").textContent = `${item.title} · ${item.voice}${item.backing_voice ? " + " + item.backing_voice : ""}`;
  stopAll();
  aCover.src = item.output; aOrig.src = item.original || "";
  ab = "cover"; $$(".ab button").forEach(b => b.setAttribute("aria-pressed", b.dataset.ab === "cover"));
  $(".ab [data-ab=orig]").disabled = !item.original;
  $("#dl-cover").href = item.output + "?download"; $("#dl-cover").textContent = `Download ${item.output_name.split(".").pop().toUpperCase()}`;
  $("#dl-vocal").href = item.vocal ? item.vocal + "?download" : "#"; $("#dl-vocal").hidden = !item.vocal;
  const sec = seconds || item.seconds || {};
  $("#times").innerHTML = [["separate", sec.separate, sec.separate < 0.5 ? "cached" : null], ["convert", sec.convert], ["mix", sec.remix], ["total", sec.total]]
    .filter(([, v]) => v != null).map(([k, v, alt]) => `<span>${k} ${alt || v.toFixed(1) + " s"}</span>`).join("");
  wavePeaks = { cover: null, orig: S.song && item.original === S.song.url ? S.song.peaks : null };
  drawResultWave();
  peaksFromUrl(item.output).then(p => { wavePeaks.cover = p; drawResultWave(); }).catch(() => {});
  if (item.original && !wavePeaks.orig) peaksFromUrl(item.original).then(p => { wavePeaks.orig = p; drawResultWave(); }).catch(() => {});
  $("#result").scrollIntoView({ block: "nearest", behavior: "smooth" });
  loadHistory();
}
function drawResultWave() {
  const a = active(), frac = a.duration ? a.currentTime / a.duration : 0;
  drawWave($("#wave-res"), wavePeaks[ab] || wavePeaks.cover, frac, ab === "cover" ? "--wave-on" : "--wave-alt");
  $("#pos").textContent = `${fmtTime(a.currentTime)} / ${fmtTime(a.duration || 0)}`;
}
function stopAll() { aCover.pause(); aOrig.pause(); $("#play").textContent = "▶"; $("#play").setAttribute("aria-label", "Play"); }
function togglePlay() {
  const a = active();
  if (!a.src) return;
  if (a.paused) { $$("#preview-list audio").forEach(x => x.pause()); a.play().catch(e => showError("The browser couldn't play this file. Try downloading it instead.", "Playback failed")); $("#play").textContent = "❚❚"; $("#play").setAttribute("aria-label", "Pause"); }
  else stopAll();
  renderHistory();
}
$("#play").onclick = togglePlay;
$(".ab").addEventListener("click", e => {
  const b = e.target.closest("button"); if (!b || b.disabled || b.dataset.ab === ab) return;
  const from = active(), wasPlaying = !from.paused, t = from.currentTime;
  from.pause(); ab = b.dataset.ab;
  $$(".ab button").forEach(x => x.setAttribute("aria-pressed", x === b));
  const to = active();
  try { to.currentTime = Math.min(t, (to.duration || t)); } catch (err) {}
  if (wasPlaying) to.play().catch(() => {});
  drawResultWave();
});
$("#wave-res").tabIndex = 0;
$("#wave-res").setAttribute("role", "slider");
$("#wave-res").setAttribute("aria-label", "Playback position: Left/Right arrows move 5 seconds, Home goes to the start");
$("#wave-res").addEventListener("keydown", e => {
  const a = active(); if (!a.duration) return;
  const step = { ArrowLeft: -5, ArrowRight: 5, PageDown: -30, PageUp: 30 }[e.key];
  if (step !== undefined) a.currentTime = Math.min(a.duration, Math.max(0, a.currentTime + step));
  else if (e.key === "Home") a.currentTime = 0;
  else if (e.key === "End") a.currentTime = Math.max(0, a.duration - 1);
  else return;
  e.preventDefault(); drawResultWave();
  $("#wave-res").setAttribute("aria-valuetext", `${fmtTime(a.currentTime)} of ${fmtTime(a.duration)}`);
});
$("#wave-res").addEventListener("click", e => {
  const a = active(); if (!a.duration) return;
  const r = e.currentTarget.getBoundingClientRect();
  a.currentTime = (e.clientX - r.left) / r.width * a.duration; drawResultWave();
});
[aCover, aOrig].forEach(a => {
  a.addEventListener("timeupdate", () => { if (a === active()) drawResultWave(); });
  a.addEventListener("loadedmetadata", drawResultWave);
  a.addEventListener("ended", () => { stopAll(); renderHistory(); });
});
addEventListener("keydown", e => {
  if (e.code === "Space" && !$("#result").hidden && !/INPUT|TEXTAREA|BUTTON|SUMMARY/.test(document.activeElement.tagName)) { e.preventDefault(); togglePlay(); }
});
$("#open-folder").onclick = () => api("/api/open-folder", { json: { history_id: S.result?.id } }).catch(e => showError(e.message));
$("#open-output").onclick = () => api("/api/open-folder", { json: {} }).catch(e => showError(e.message));

// ------------------------------------------------------------ preview (audition settings on a 15 s excerpt)
let previewKind = store.get("uc-preview-kind", "octave");
function previewVariants() {
  const o = S.opts, clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const uniq = arr => [...new Map(arr.map(v => [JSON.stringify(v), v])).values()];
  if (previewKind === "octave") {
    const cur = +(o.octave || 0);
    return uniq([cur - 1, cur, cur + 1].map(x => clamp(x, -2, 2))).map(x => ({
      octave: x, label: x === 0 ? "Same octave" : `Voice ${x > 0 ? "+" : "−"}${Math.abs(x)} octave` }));
  }
  if (previewKind === "key") {
    const cur = +(o.key || 0);
    return uniq([cur - 2, cur, cur + 2].map(x => clamp(x, -6, 6))).map(x => ({ key: x, label: keyLabel(x) }));
  }
  if (previewKind === "correction")
    return [{ pitch_correction: 0, label: "No correction" }, { pitch_correction: 0.4, label: "Gentle (0.4)" }, { pitch_correction: 1, label: "Full autotune" }];
  return [{ polish: false, label: "Polish off" }, { polish: true, label: "Polish on" }];
}
function syncPreviewKind() { $$("#preview-kind button").forEach(b => b.setAttribute("aria-pressed", b.dataset.v === previewKind)); }
$("#preview-kind").addEventListener("click", e => {
  const b = e.target.closest("button"); if (!b) return;
  previewKind = b.dataset.v; store.set("uc-preview-kind", previewKind); syncPreviewKind();
});
$("#preview-btn").onclick = () => {
  $("#preview").hidden = false; syncPreviewKind();
  $("#preview").scrollIntoView({ block: "nearest", behavior: "smooth" });
};
$("#preview-close").onclick = () => { $$("#preview-list audio").forEach(a => a.pause()); $("#preview").hidden = true; };

$("#preview-go").onclick = async () => {
  if (!S.song || !S.voice || S.job || S.osBusy) return;
  const variants = previewVariants();
  $$("#preview-list audio").forEach(a => a.pause());
  $("#preview-list").innerHTML = "";
  $("#preview-go").disabled = true;
  $("#preview-bar").hidden = false; $("#preview-bar i").style.width = "0%";
  $("#preview-status").textContent = "Starting…";
  try {
    const { job } = await api("/api/preview", { json: { song: S.song.id, voice: S.voice, options: S.opts, variants } });
    S.job = job; summary();
    const es = new EventSource(`/api/jobs/${job}/events`);
    es.onmessage = m => {
      const ev = JSON.parse(m.data);
      if (ev.message) $("#preview-status").textContent = ev.message;
      if (ev.frac != null) $("#preview-bar i").style.width = Math.round((ev.stage === "rendering" ? 0.3 + 0.7 * ev.frac : 0.3 * ev.frac) * 100) + "%";
      if (!ev.final) return;
      es.close(); S.job = null; summary(); $("#preview-go").disabled = false; $("#preview-bar").hidden = true;
      if (ev.state === "done") { renderPreviews(ev.previews); refreshEstimate(); }
      else if (ev.state === "cancelled") $("#preview-status").textContent = "Cancelled.";
      else { $("#preview-status").textContent = ""; showError(ev.message, "Preview failed"); }
    };
    es.onerror = () => { es.close(); S.job = null; summary(); $("#preview-go").disabled = false; $("#preview-bar").hidden = true; };
  } catch (e) {
    $("#preview-go").disabled = false; $("#preview-bar").hidden = true; $("#preview-status").textContent = "";
    showError(e.message, "Preview failed");
  }
};

function renderPreviews(list) {
  $("#preview-status").textContent = "Switch between versions while playing: they stay in sync. Pick one with “Use this”.";
  const box = $("#preview-list");
  box.innerHTML = list.map((p, i) => `
    <div class="pcard" data-i="${i}">
      <div class="ptitle"><button class="play" aria-label="Play ${esc(p.label)}">▶</button><b>${esc(p.label)}</b></div>
      <button class="btn use">Use this</button>
      <audio preload="auto" src="${esc(p.url)}"></audio>
    </div>`).join("");
  applyVolume();
  const cards = $$(".pcard", box), audios = cards.map(c => $("audio", c));
  let pos = 0;
  cards.forEach((card, i) => {
    const a = audios[i];
    $(".play", card).onclick = () => {
      if (!a.paused) { a.pause(); return; }
      const playing = audios.find(x => !x.paused);
      if (playing) { pos = playing.currentTime; playing.pause(); }
      stopAll();  // pause the main result player too
      try { a.currentTime = Math.min(pos, a.duration || pos); } catch (e) {}
      a.play().catch(() => {});
    };
    a.addEventListener("play", () => { card.classList.add("playing"); $(".play", card).textContent = "❚❚"; });
    a.addEventListener("pause", () => { card.classList.remove("playing"); $(".play", card).textContent = "▶"; pos = a.currentTime; });
    a.addEventListener("ended", () => { pos = 0; });
    $(".use", card).onclick = () => {
      Object.assign(S.opts, list[i].options); store.set("uc-opts", S.opts); syncControls(); summary(); refreshEstimate();
      $$(".use", box).forEach(b => b.textContent = "Use this"); $(".use", card).textContent = "✓ Using this";
    };
  });
}

// ------------------------------------------------------------ history
let HIST = [];
async function loadHistory() { try { HIST = await api("/api/history"); } catch (e) { HIST = []; } renderHistory(); }
function renderHistory() {
  const box = $("#hist");
  if (!HIST.length) { box.innerHTML = `<p class="meta">Your covers will appear here.</p>`; return; }
  const nowPlaying = !active().paused ? playingHist : null;
  box.innerHTML = HIST.slice(0, 50).map(h => {
    const o = h.options || {};
    const bits = [o.key != null || o.octave != null ? pitchText(o) : (o.pitch ? `vocal ${fmtPitch(o.pitch)} st (out of key)` : null), o.quality, o.karaoke ? "lead only" : null, o.dereverb ? "de-reverb" : null,
                  h.seconds?.total ? `made in ${h.seconds.total.toFixed(1)} s` : null].filter(Boolean).join(" · ");
    return `<div class="hrow${h.id === nowPlaying ? " playing" : ""}"><div><strong>${esc(h.title)} · ${esc(h.voice)}${h.backing_voice ? " + " + esc(h.backing_voice) : ""}</strong><small class="mono">${esc(bits)}</small></div>
      <small class="hdate">${esc((h.created || "").slice(0, 16))}</small>
      <div class="hbtns"><button class="icon-btn" data-play="${h.id}" aria-label="Play ${esc(h.title)}">${h.id === nowPlaying ? "❚❚" : "▶"}</button><a class="icon-btn" href="${esc(h.output)}?download">Download</a></div></div>`;
  }).join("");
  $$("[data-play]", box).forEach(b => b.onclick = () => {
    const h = HIST.find(x => x.id === +b.dataset.play);
    if (S.result?.id === h.id) return togglePlay();
    showResult(h).then(() => { aCover.addEventListener("canplay", () => togglePlay(), { once: true }); });
  });
}

// ------------------------------------------------------------ boot
function redrawAll() { if (S.song) drawWave($("#wave-song"), S.song.peaks, 0); if (!$("#result").hidden) drawResultWave(); }
addEventListener("resize", redrawAll);
(async function boot() {
  applyTheme();
  pollStatus();
  try {
    S.defaults = await api("/api/config");
    if (!("key" in S.defaults) || !("backing_voice" in S.defaults)) restartNeeded();  // server predates these settings
    loadOpts();
    await loadVoices();
    await loadHistory();
    // a cover started before a reload is still running: show its progress again instead of inviting a duplicate
    try { const st = await api("/api/status"); if (st.cover_job && !S.job) { S.job = st.cover_job; summary(); startProgress(st.cover_job); } } catch (e) {}
    const last = store.get("uc-song", null);
    if (last) { try { S.song = await api(`/api/songs/${last}`); renderSong(); refreshEstimate(); } catch (e) { store.set("uc-song", null); } }
  } catch (e) { showError(e.message, "UnderCover isn't responding"); }
})();
