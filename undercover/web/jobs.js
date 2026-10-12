"use strict";
// The page's job feed: one place that knows what's running, for every bar on the page (FrontMan's stages, the one-shot,
// SpeakEasy, DoubleAgent, the running bar under the top bar, the queue drawer, SafeHouse's list and the tab title).
//
// watchJob(id, fn) hands fn every event of a job, in order, from its live stream (/api/jobs/<id>/events). A stream can
// stall (something between the server and Chrome holding it back) or drop (sleep, Wi-Fi): then, while the job runs, the
// feed asks /api/jobs once a second and hands fn a summary event ({synthetic: true}) so bars keep moving, and it reopens
// the stream for the real events (the server replays them; ones already handed on are skipped). The list of jobs
// (JF.list) is refreshed every 5 s, every second while a stream is quiet, and at once when the tab is shown again.
// Uses helpers from app.js (api, tabTitle, fmtSecs).

const JF = { list: [], at: 0, ch: {}, subs: new Set(), polling: null, seen: new Set(), titled: null };

// which module a job belongs to (colour, label, where the bar's click goes)
const JOB_OWNER = { cover: "frontman", presep: "frontman", auto: "frontman", install: "frontman", speech: "speakeasy",
                    addon: "safehouse", move: "safehouse", doctor: "safehouse", ytdlp: "safehouse" };
const jobOwner = kind => JOB_OWNER[kind] || "doubleagent";   // train, train-*, night (the overnight queue)
const JOB_KIND = { cover: "cover", presep: "separating", auto: "one-shot", install: "voice install", speech: "speech",
                   train: "training", "train-prepare": "preparing clips", "train-auto": "new voice", "train-export": "adding a voice",
                   "train-transcribe": "transcripts", "cadence-ab": "listening copies", night: "voice work",
                   addon: "add-on", move: "moving files", doctor: "check-up", ytdlp: "YouTube downloader" };
const PAUSABLE_KINDS = new Set(["train", "train-prepare", "train-auto", "train-transcribe", "night", "auto"]);
const isRunning = j => j && !j.final && !["done", "error", "cancelled", "paused", "lost"].includes(j.state);

// ------------------------------------------------------------ the job list (/api/jobs)
let jfLoading = null;
function refreshJobs(force) {
  if (!force && Date.now() - JF.at < 800) return jfLoading || Promise.resolve(JF.list);
  if (jfLoading) return jfLoading;
  jfLoading = api("/api/jobs").then(list => {
    JF.list = list; JF.at = Date.now();
    for (const j of list) {   // live streams know more than the snapshot: keep their newest progress
      const ch = JF.ch[j.id];
      if (ch?.live && ch.live.events >= j.events) Object.assign(j, ch.live, { events: Math.max(ch.live.events, j.events) });
      if (ch) ch.snap = j;
    }
    for (const ch of Object.values(JF.ch)) jfCheckQuiet(ch);
    jfAnnounce(list);
    jfNotify();
    return list;
  }).catch(() => JF.list).finally(() => { jfLoading = null; });
  return jfLoading;
}
// "uc:jobdone" (detail: the job) once for every job that finishes while this page is open, whoever started it (the page,
// another tab, the queue, the API): voice lists, cards and projects refresh on it
const JF_RUN = new Set();
function jfAnnounce(list) {
  for (const j of list) {
    if (isRunning(j)) JF_RUN.add(j.id);
    else if (JF_RUN.delete(j.id)) document.dispatchEvent(new CustomEvent("uc:jobdone", { detail: j }));
  }
}
// fn(list) after every change (new list, or a live event moved a job on)
// (not called at once: scripts subscribe while the page loads, before the ones they draw with are there)
function onJobs(fn) { JF.subs.add(fn); return () => JF.subs.delete(fn); }
let jfFrame = 0;
function jfNotify() {   // at most once a frame; at once in a hidden tab (no frames there, but the title still counts)
  if (jfFrame) return;
  const run = () => { jfFrame = 0; jfTitle(); JF.subs.forEach(fn => { try { fn(JF.list); } catch (e) { console.error(e); } }); };
  if (document.hidden) run(); else jfFrame = requestAnimationFrame(run);
}
const runningJobs = (opts = {}) => JF.list.filter(j => isRunning(j) && (opts.background || j.kind !== "presep"));

// ------------------------------------------------------------ one job's events
function watchJob(id, fn) {
  const ch = jfChannel(id);
  let off = false;
  const past = [...ch.events, ...(ch.done && !ch.events.some(e => e.final) && ch.lastFinal ? [ch.lastFinal] : [])];
  queueMicrotask(() => {   // a late watcher catches up (after watchJob returned, so fn can call what it returns)
    for (const ev of past) { if (off) return; fn(ev); }
    if (!off) ch.fns.add(fn);
  });
  return () => { off = true; ch.fns.delete(fn); if (!ch.fns.size && ch.done) jfClose(ch); };
}
function jfChannel(id) {
  if (JF.ch[id]) return JF.ch[id];
  const ch = JF.ch[id] = { id, events: [], fns: new Set(), es: null, lastAt: Date.now(), done: false, live: null, snap: null, retry: 0,
                           since: Date.now() };
  jfOpen(ch);
  jfPoller();
  return ch;
}
function jfOpen(ch) {
  if (ch.es) ch.es.close();
  let i = 0;   // position in this stream; the server starts every stream (and Chrome's own reconnects) from the first event
  const es = ch.es = new EventSource(`/api/jobs/${ch.id}/events`);
  es.onopen = () => { i = 0; };
  es.onmessage = m => {
    ch.lastAt = Date.now();
    if (i++ < ch.events.length) return;          // replayed after a reconnect: handed on already
    let ev; try { ev = JSON.parse(m.data); } catch (e) { return; }
    jfDeliver(ch, ev, true);
  };
  es.onerror = () => {
    if (es.readyState === EventSource.CLOSED && ch.es === es) { ch.es = null; if (!ch.done) refreshJobs(true).then(() => jfCheckQuiet(ch)); }
  };
}
function jfDeliver(ch, ev, real) {
  if (ch.done) return;
  if (real) ch.events.push(ev);
  const prog = ev.frac ?? ev.fraction;
  ch.live = { ...(ch.live || {}), state: ev.state || "running", events: ch.events.length };
  for (const k of ["stage", "epoch", "epochs", "eta", "total_left", "stage_left", "index", "of", "elapsed"]) if (ev[k] != null) ch.live[k] = ev[k];
  if (prog != null) ch.live.progress = prog;
  const msg = ev.message || ev.label || ev.note; if (msg) ch.live.message = msg;
  if (ev.final) { ch.done = true; ch.lastFinal = ev; ch.live.final = { state: ev.state, message: ev.message }; ch.live.state = ev.state; if (ch.es) { ch.es.close(); ch.es = null; } }
  const j = JF.list.find(x => x.id === ch.id);
  if (j) Object.assign(j, ch.live);
  ch.fns.forEach(fn => { try { fn(ev); } catch (e) { console.error(e); } });
  jfNotify();
  if (ch.done && !ch.fns.size) jfClose(ch);
}
function jfClose(ch) { if (ch.es) ch.es.close(); delete JF.ch[ch.id]; }
// a stream that's quiet while the job runs: hand on the snapshot, and try the stream again
function jfCheckQuiet(ch) {
  if (ch.done) return;
  const s = ch.snap;
  if (JF.at > ch.since + 1500 && !JF.list.some(j => j.id === ch.id)) {
    // the server doesn't know this job (it restarted): it's gone
    return jfDeliver(ch, { final: true, state: "lost", message: "UnderCover restarted while this was running, so it stopped." }, false);
  }
  if (!s) return;
  if (s.final || !isRunning(s)) {   // it finished: the real final event (with its result) comes from a fresh stream
    if (!ch.es) jfOpen(ch);
    if (++ch.retry > 2) jfDeliver(ch, { ...jfSynth(s), final: true, state: s.final?.state || s.state }, false);
    return;
  }
  if (Date.now() - ch.lastAt > 3000 && s.events > ch.events.length) {
    jfDeliver(ch, jfSynth(s), false);
    if (!ch.es || Date.now() - ch.lastAt > 15000) { ch.lastAt = Date.now(); jfOpen(ch); }
  }
}
const jfSynth = s => ({ synthetic: true, stage: s.stage, frac: s.progress, fraction: s.progress, message: s.message, state: s.state,
                        epoch: s.epoch, epochs: s.epochs, eta: s.eta, total_left: s.total_left, stage_left: s.stage_left,
                        index: s.index, of: s.of, elapsed: s.elapsed });
// every second while the tab is shown and some watched job's stream is quiet; every 5 s otherwise
function jfPoller() {
  if (JF.polling) return;
  JF.polling = setInterval(() => {
    const quiet = Object.values(JF.ch).some(ch => !ch.done && Date.now() - ch.lastAt > 3000);
    const due = Date.now() - JF.at > (quiet && !document.hidden ? 1000 : 5000);
    if (due) refreshJobs(true);
  }, 1000);
}
addEventListener("visibilitychange", () => { if (!document.hidden) refreshJobs(true); });
addEventListener("focus", () => refreshJobs(true));

// ------------------------------------------------------------ the tab title: the running job, then "✓ … done"
const DONE_WORDS = { cover: "Cover ready", auto: "Cover ready", speech: "Speech ready", install: "Voice installed", train: "Training done",
                     "train-auto": "Voice ready", "train-prepare": "Clips ready", "train-export": "Voice added", "train-transcribe": "Transcripts ready", night: "Voice work done" };
function jfTitle() {
  const run = runningJobs();
  const main = run.find(j => j.kind === "cover" || j.kind === "auto" || j.kind === "speech") || run[0];
  run.forEach(j => JF.seen.add(j.id));
  if (main) {
    const left = main.total_left ?? main.eta;
    const what = main.kind === "speech" && main.of ? `🗣 ${(main.index ?? -1) + 1}/${main.of}`
      : main.kind === "cover" ? (/^Preview/.test(main.title || "") ? "⏳ Preview" : "⏳ Cover")
      : `⏳ ${JOB_KIND[main.kind] ? JOB_KIND[main.kind][0].toUpperCase() + JOB_KIND[main.kind].slice(1) : "Working"}`;
    const pct = left == null && main.progress != null && main.kind !== "speech" ? ` ${Math.round(main.progress * 100)}%` : "";
    JF.titled = main.id;
    return tabTitle(`${what}${left != null ? " · " + fmtSecs(Math.max(1, left)) : pct}`);
  }
  if (JF.titled) {   // the job the title was showing just ended
    const j = JF.list.find(x => x.id === JF.titled);
    JF.titled = null;
    const st = j?.final?.state || j?.state;
    tabTitle(st === "done" && DONE_WORDS[j.kind] && !/^Preview/.test(j.title || "") ? "✓ " + DONE_WORDS[j.kind] : null);
  }
}

refreshJobs(true);
jfPoller();
