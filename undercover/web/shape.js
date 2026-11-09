// Shaping a cover: the edit list's page (frontman/edits.py, ARCHITECTURE "Edit list"). Pick a stretch of the cover's words, say what
// changes there, hear it in about five seconds; undo, redo, and Keep makes a new cover beside this one. PC only: the routes aren't the
// paired phone's, and its row says so.
// Everything the cover heard or said (its words, the gate's sentences, a note, a title) is outside text: shown through esc() or
// textContent, never as HTML (the maintainers' notes, editlist).
// Uses app.js ($, $$, esc, api, showError, HIST, vname, voiceDisplay, vinitial, coverTitle, loadHistory, stopAll, ownPlayer), jobs.js (watchJob).

const SH = { id: null, item: null, doc: null, state: null, pick: null, probes: null, seq: 0, busy: null, live: null, said: null, error: null,
             retry: false, confirm: false, keptAs: null, jobs: new Set(), audio: ownPlayer(new Audio()), stopAt: null, focus: 0 };
// a job that ended in one of these can go differently next time; a plain refusal (the gate's or prepare's sentence) can't
const SH_RETRY = /ran out of memory|Something went wrong|blocked writing|try again/i;
const SH_GAP_S = 0.6;   // a pause this long between two heard words starts a new line (duets.phrases joins under it)
const SH_FADE_S = 0.04;   // duets.FADE_S: a pause shorter than the crossfade isn't a place to land
const SH_OCT = 2;   // edits.OCTAVE_BOUND

SH.audio.addEventListener("timeupdate", () => { if (SH.stopAt != null && SH.audio.currentTime >= SH.stopAt) { SH.audio.pause(); SH.stopAt = null; } });
const shName = who => voiceDisplay(who === "B" ? SH.item?.duet_voice : SH.item?.voice);
const shClock = s => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
const shWord = i => String(SH.doc.words[i]?.[0] ?? "").trim();
const shBare = s => s.replace(/[,.!?;:]+$/, "");
const shLive = () => SH.doc.edits.slice(0, SH.doc.cursor);
// the compiled part ([s, e, who, octave, src]) under a time, and the absolute octave it sings at
const shPartAt = (part, t) => SH.doc.compiled[part].find(p => p[0] <= t && t < p[1]) || SH.doc.compiled[part].at(-1);
const shOctOf = p => p[3] ?? SH.doc.octaves[p[2]] ?? 0;

function shapeOpen(id) {
  const item = HIST.find(h => h.id === id);
  if (!item) return;
  Object.assign(SH, { id, item, doc: null, state: "loading", pick: null, probes: null, busy: null, live: null, said: null, error: null, retry: false, confirm: false, keptAs: null, focus: 0, heard: 0 });
  $("#shape").hidden = false;
  shDraw();
  $("#shape").scrollIntoView({ block: "start", behavior: "smooth" });
  shLoad(true);
}
function shapeClose() { SH.audio.pause(); SH.id = null; $("#shape").hidden = true; $("#shape").innerHTML = ""; }

async function shLoad(first) {
  const id = SH.id;
  let v;
  try { v = await api(`/api/history/${id}/edits`); }
  catch (e) { if (SH.id === id) { SH.state = "error"; SH.error = e.message; SH.retry = true; shDraw(); } return; }   // (the server didn't answer: worth another try)
  if (SH.id !== id) return;
  if (v.none) {
    if (!first) { SH.state = "error"; SH.error = "This cover's words couldn't be heard."; SH.retry = true; return shDraw(); }
    SH.state = "listening"; SH.live = null; shDraw();
    try { const { job } = await api(`/api/history/${id}/edits/open`, { json: {} }); if (job) return shFollow(job, () => shLoad(false)); return shLoad(false); }
    catch (e) { SH.state = "error"; SH.error = e.message; SH.retry = false; return shDraw(); }   // (refused outright: trying again gives the same answer)
  }
  SH.doc = v; SH.state = "ready"; SH.heard++;
  if (SH.pick && !(SH.pick[1] < v.words.length)) SH.pick = null;
  shDraw();
  if (SH.pick) shProbe();
}

// follow a job of ours to its end: then(ev) on done, its sentence on anything else
function shFollow(job, then) {
  SH.jobs.add(job);
  const id = SH.id;
  const stop = watchJob(job, ev => {
    if (SH.id !== id) return;
    if (!ev.final) { if (ev.message && (SH.busy || SH.state === "listening")) { SH.live = ev.message; shDraw(); } return; }   // the job's own words
    stop();
    SH.busy = SH.live = null;
    if (ev.state === "done") return then(ev);
    if (SH.state === "listening") { SH.state = "error"; SH.error = ev.message || "The words couldn't be heard."; SH.retry = SH_RETRY.test(SH.error); }
    else if (ev.state !== "cancelled") SH.said = { k: "no", s: ev.message || "That didn't finish." };
    shDraw();
  });
}

// ---------------------------------------------------------------- what can change over the picked words, and what the gate says of each
function shChoices() {
  const d = SH.doc, [a, b] = SH.pick, t0 = d.words[a][1], t1 = d.words[b][2];
  const over = part => d.compiled[part].filter(p => p[0] < t1 && p[1] > t0);
  const out = [];
  const add = (part, list) => list.forEach(c => out.push({ part, ...c }));
  const octs = (part, parts) => {
    const at = [...new Set(parts.map(shOctOf))];
    if (at.length !== 1) return [];   // two singers at their own octaves: one octave for both isn't "up" for each
    return [1, -1].filter(s => Math.abs(at[0] + s) <= SH_OCT).map(s => ({ op: "octave", octave: at[0] + s, label: `An octave ${s > 0 ? "up" : "down"}` }));
  };
  const lead = over("lead"), whos = new Set(lead.map(p => p[2]));
  add("lead", [...(d.duet ? ["A", "B"].filter(w => !(whos.size === 1 && whos.has(w))).map(w => ({ op: "singer", who: w, label: shName(w) + " takes it" })) : []), ...octs("lead", lead)]);
  if (d.duet && d.backing) {
    const back = over("backing"), bw = new Set(back.map(p => p[2]));
    add("backing", [...["A", "B"].filter(w => !(bw.size === 1 && bw.has(w))).map(w => ({ op: "singer", who: w, label: shName(w) + " takes it" })),
                    ...(bw.size === 1 && bw.has("original") ? [] : [{ op: "singer", who: "original", label: "The original" }]),
                    ...(bw.has("original") ? [] : octs("backing", back))]);
  }
  return { out, mixed: { lead: new Set(lead.map(shOctOf)).size > 1 } };
}
const shBody = c => ({ op: c.op, part: c.part, ...(c.op === "singer" ? { who: c.who } : { octave: c.octave }), words: SH.pick, words_hash: SH.doc.words_hash });

// every choice through the gate and the price (edits/price: nothing is sung), so the bar says what the gate will say
async function shProbe() {
  const seq = ++SH.seq, { out } = shChoices();
  SH.probes = null; shDraw();
  const got = await Promise.all(out.map(c => api(`/api/history/${SH.id}/edits/price`, { json: shBody(c) }).then(r => ({ c, ok: r }), e => ({ c, no: e.message }))));
  if (seq !== SH.seq) return;
  SH.probes = got; shDraw();
}

// the gate's landing for a part, as a sentence: fine, a join that may be heard, or no place (with which way to move)
function shLanding(part) {
  const p = (SH.probes || []).filter(x => x.c.part === part);
  if (!p.length) return null;
  const ok = p.find(x => x.ok);
  if (!ok) {
    const s = p[0].no;
    // which way to move, only when the pick doesn't already go there (Internal Affairs D2: a pick from a line's first word was told to start at the line's start)
    const lines = shLines(), [a, b] = SH.pick;
    const way = /near the start/.test(s) && !lines.some(l => l.words[0] === a) ? " Take the stretch from the start of the line."
      : /near the end/.test(s) && !lines.some(l => l.words.at(-1) === b) ? " Take the stretch a little further, to the end of the line." : "";
    return { k: "no", s: s + way };
  }
  const e = ok.ok.edit;
  if (e.moved) return { k: /may be heard/.test(e.moved) ? "warn" : "ok", s: e.moved };   // (the gate's one sentence: where it landed)
  const w = e.words || SH.pick, d = SH.doc;
  const from = e.t0 <= 0.05 ? "Starts at the top of the song" : "Starts in the pause before “" + shBare(shWord(w[0])) + "”";   // (plain text)
  const to = e.t1 >= d.duration - 0.05 ? "ends at the end of the song" : "ends in the pause after “" + shBare(shWord(w[1])) + "”";
  return { k: "ok", s: `${from} and ${to}.` };
}

async function shSend(c) {
  try {
    const r = await api(`/api/history/${SH.id}/edits`, { json: shBody(c) });
    SH.busy = "Singing it again…"; SH.live = null; SH.said = r.fit?.said ? { k: "warn", s: r.fit.said } : null; SH.keptAs = null; SH.probes = null;
    shDraw();
    shFollow(r.job, () => { shLoad(false); loadHistory(); });
  } catch (e) { SH.said = { k: "no", s: e.message }; shDraw(); }
}
async function shStep(what) {
  try {
    const { job } = await api(`/api/history/${SH.id}/edits/${what}`, { json: {} });
    SH.busy = "Mixing it again…"; SH.live = null; SH.said = null; SH.keptAs = null; SH.probes = null; shDraw();
    shFollow(job, () => shLoad(false));
  } catch (e) { SH.said = { k: "no", s: e.message }; shDraw(); }
}
async function shKeep() {
  SH.confirm = false;
  try {
    const { job } = await api(`/api/history/${SH.id}/edits/keep`, { json: {} });
    SH.busy = "Keeping it…"; SH.live = null; shDraw();
    shFollow(job, ev => { SH.keptAs = ev.result?.output_name ? ev.result.output_name.replace(/\.[^.]+$/, "") : SH.item.title + " (edited)"; shDraw(); loadHistory(); });
  } catch (e) { SH.said = { k: "no", s: e.message }; shDraw(); }
}

// "Hear it": the result (the cover itself before any change) from a second before to a second after
function shHear(t0, t1) {
  stopAll();
  const src = SH.doc?.result ? `${SH.doc.result}?v=${SH.heard}` : SH.item.output;   // (the result keeps its file name from edit to edit)
  if (!SH.audio.src.endsWith(src)) SH.audio.src = src;
  SH.audio.currentTime = Math.max(0, t0 - 1); SH.stopAt = t1 + 1;
  SH.audio.play().catch(e => showError("The browser couldn't play this file.", "Couldn't play it"));
}

// ---------------------------------------------------------------- drawing
const shChangeName = e => {
  const part = e.part === "lead" ? "singing" : "backing";
  if (e.op === "singer") return e.who === "original" ? "The original backing" : shName(e.who) + " takes the " + part;   // (plain text: esc'd where it's drawn)
  const p = shPartAt(e.part, (e.t0 + e.t1) / 2), apart = e.octave - (SH.doc.octaves[p[2]] ?? 0);
  return !apart ? `The ${part} at its own octave` : `The ${part} ${Math.abs(apart) > 1 ? `${Math.abs(apart)} octaves` : "an octave"} ${apart > 0 ? "up" : "down"}`;
};
function shLines() {
  const d = SH.doc, lines = [];
  let cur = null;
  d.words.forEach((w, i) => {
    const mid = (w[1] + w[2]) / 2, sec = d.sections.findIndex(s => s.t0 <= mid && mid < s.t1);
    const prev = d.words[i - 1];
    if (!cur || sec !== cur.sec || (prev && w[1] - prev[2] > SH_GAP_S)) lines.push(cur = { sec, words: [] });
    cur.words.push(i);
  });
  return lines;
}
// Deep Cover: where a change may land before word i (a lead pause, a dip only, neither)
function shLand(i) {
  const d = SH.doc, a = d.words[i - 1]?.[2] ?? 0, b = d.words[i][1];
  if (d.points.lead.some(([s, e]) => e - s >= SH_FADE_S && s <= b + 0.05 && e >= a - 0.05)) return "silence";
  return d.points.dips.some(x => x >= a - 0.05 && x <= b + 0.05) ? "dip" : "none";
}
const shChip = (who, id) => `<span class="vchip ${who.toLowerCase()}"><span class="av">${vinitial(id)}</span>${vname(id)}</span>`;

function shDraw() {
  const box = $("#shape");
  if (!SH.id || !box) return;
  const it = SH.item, d = SH.doc;
  const chips = shChip("A", it.voice) + (it.duet_voice ? shChip("B", it.duet_voice) : "");
  const head = `<div class="sh-head"><div><h2>Shaping ${esc(coverTitle(it))}</h2><div class="sh-who">${chips}</div></div><div class="sh-acts">${d ? `
    <button class="btn" type="button" data-sh="undo"${d.cursor && !SH.busy ? "" : " disabled"}>Undo</button>
    <button class="btn" type="button" data-sh="redo"${d.cursor < d.edits.length && !SH.busy ? "" : " disabled"}>Redo</button>
    <button class="btn primary" type="button" data-sh="keep"${d.cursor && !SH.busy ? "" : " disabled"}>Keep this version</button>` : ""}
    <button class="linkbtn quiet" type="button" data-sh="done">Done</button></div></div>`;
  if (SH.state !== "ready") {
    const st = SH.state === "listening" ? `<b>Listening for the words…</b><span>About 20 s, once. The app hears the song through, then keeps the words with this cover.</span>${SH.live ? `<span class="meta">${esc(SH.live)}</span>` : ""}`
      : SH.state === "error" ? `<b>${esc(SH.error || "This cover can't be shaped.")}</b>${SH.retry ? `<span><button class="btn" type="button" data-sh="retry">Try again</button></span>` : ""}`
      : `<b>Opening…</b>`;
    box.innerHTML = `<div class="ed">${head}<div class="state" role="status">${st}</div></div>`;
    return shWire();
  }
  const live = shLive(), pick = SH.pick;
  let h = `<div class="ed">${head}<p class="meta">Pick a stretch of the words and say what should change there. Each change is sung again in about 5 seconds. Nothing touches the cover until you keep it.</p>`;
  if (!d.duet) h += `<p class="meta">One singer here, so the changes are octaves. For a second singer, make it a duet first.</p>`;
  if (d.duet) h += `<div data-deep><div class="strip" aria-hidden="true">${d.compiled.lead.map(p => `<i class="${p[2].toLowerCase()}" style="flex-grow:${p[1] - p[0]}"></i>`).join("")}</div>
    <div class="sh-legend"><span><i class="sh-dot a"></i>${esc(shName("A"))}</span><span><i class="sh-dot b"></i>${esc(shName("B"))}</span></div></div>`;
  if (!d.words.length) h += `<p class="meta">No words were heard in this cover, so there's nothing to pick here.</p>`;
  else {
    h += `<div class="lyr" role="group" aria-label="The words: pick the first and the last word of a stretch">`;
    let last = null;
    shLines().forEach(ln => {
      const kind = ln.sec >= 0 ? d.sections[ln.sec].kind : null;
      if (kind && ln.sec !== last) h += `<div class="sec">${esc(kind)}</div>`;
      last = ln.sec;
      const w0 = d.words[ln.words[0]], lead0 = shPartAt("lead", (w0[1] + w0[2]) / 2);
      h += `<div class="ln ${d.duet ? lead0[2].toLowerCase() : "a"}">` + ln.words.map(i => {
        const w = d.words[i], mid = (w[1] + w[2]) / 2, lead = shPartAt("lead", mid), back = d.backing ? shPartAt("backing", mid) : null;
        let cls = `sh-w s${lead[2].toLowerCase()}`;
        if (pick && i >= pick[0] && i <= pick[1]) cls += ` in${i === pick[0] ? " first" : ""}${i === pick[1] ? " last" : ""}`;
        if (lead[4]) cls += ` e${lead[2].toLowerCase()}`;
        else if (back?.[4]) cls += back[2] === "original" ? " eo" : ` e${back[2].toLowerCase()}`;
        return `${i ? `<span class="land ${shLand(i)}" aria-hidden="true"></span>` : ""}<span class="${cls}" data-w="${i}" role="button" tabindex="${i === SH.focus ? 0 : -1}"${pick && i >= pick[0] && i <= pick[1] ? ' aria-pressed="true"' : ""}>${esc(shWord(i))}</span>`;
      }).join(" ") + `</div>`;
    });
    h += `</div><div class="deeplegend" data-deep><span><i class="silence"></i>a pause: a change lands here</span><span><i class="dip"></i>a dip only: it may be heard</span><span><i class="none"></i>no place</span></div>
      <p class="meta">The words are what the app heard, so one may be off. The sound is what counts.</p>`;
  }
  h += shBar();
  if (d.edits.length) {
    h += `<div class="chg"><div class="hd"><b>Changes · ${live.length}</b></div>` + d.edits.map((e, k) => ({ e, k })).reverse().map(({ e, k }) => {
      const off = k >= d.cursor, at = String(e.at || "").slice(11, 16);
      return `<div class="sh-row${off ? " sh-off" : ""}"><span class="sh-t">${shClock(e.t0)}–${shClock(e.t1)}</span><span class="sh-what"><b>${esc(shChangeName(e))}</b>${e.moved ? `<span class="say">${esc(e.moved)}</span>` : ""}
        <span class="by" data-deep>by ${e.by === "ai" ? "your AI" : "you"}${at ? ` · ${esc(at)}` : ""} · ${esc(e.id)}${e.note ? ` · “${esc(e.note)}”` : ""}</span>${off ? `<span class="say">Undone. Redo brings it back.</span>` : ""}</span>
        ${off || !d.result ? "<span></span>" : `<button class="sh-play" type="button" data-hear="${e.t0},${e.t1}" aria-label="Hear this change">▶</button>`}</div>`;
    }).join("") + `</div>`;
    if (SH.keptAs) h += `<div class="kept" role="status">Kept as <b>${esc(SH.keptAs)}</b>. The changes went with it, and this cover is as it was. Undo still works here, and a new Keep makes another version.</div>`;
    else if (live.length && d.kept_now) h += `<div class="kept" role="status">This version is kept: it's a cover of its own in Recent covers.</div>`;
    else if (live.length) h += `<div class="unkept"><span>Not kept yet. These changes stay with this cover, and roll off with it when it leaves Recent covers.</span><button class="btn primary" type="button" data-sh="keep"${SH.busy ? " disabled" : ""}>Keep this version</button></div>`;
  }
  if (SH.confirm) h += `<div class="sheet" role="dialog" aria-label="Keep this version?"><h3>Keep this version?</h3><p>It becomes a new cover, <b>${esc(it.title + " (edited)")}</b>, next to the one it came from, with its changes. Recent covers holds 15, so the oldest one there rolls off to make room. Covers sent to SafeHouse stay.</p>
    <div class="row"><button class="btn primary" type="button" data-sh="keep-yes">Keep it</button><button class="btn" type="button" data-sh="keep-no">Not yet</button></div></div>`;
  box.innerHTML = h + `</div>`;
  shWire();
}
function shBar() {
  const d = SH.doc, pick = SH.pick;
  if (!pick) return SH.said ? `<p class="land-say ${SH.said.k}" role="status">${esc(SH.said.s)}</p>` : `<p class="meta pickhint">Pick a word, then the last word of the stretch.</p>`;
  const [a, b] = pick, quote = a === b ? shWord(a) : shWord(a) + (b - a > 1 ? " … " : " ") + shWord(b);   // (plain text: esc'd below)
  let h = `<div class="sh-bar"><div class="pick"><button class="sh-play" type="button" data-hear="${d.words[a][1]},${d.words[b][2]}" aria-label="Hear this stretch">▶</button><q>${esc(quote)}</q>
    <span class="sh-t">${shClock(d.words[a][1])}–${shClock(d.words[b][2])}</span><button class="x" type="button" data-sh="unpick" aria-label="Drop the stretch">×</button></div>`;
  if (SH.busy) return h + `<div class="busy" role="status"><i></i>${esc(SH.live || SH.busy)}</div>${SH.said ? `<p class="land-say ${SH.said.k}">${esc(SH.said.s)}</p>` : ""}</div>`;
  if (SH.said) h += `<p class="land-say ${SH.said.k}" role="status">${esc(SH.said.s)}</p>`;
  if (!SH.probes) return h + `<p class="meta">Asking where it would land…</p></div>`;
  const land = shLanding("lead");
  if (land) h += `<p class="land-say ${land.k}">${esc(land.s)}</p>`;
  const { mixed } = shChoices();
  const row = (part, lab) => {
    const p = SH.probes.filter(x => x.c.part === part);
    if (!p.length) return "";
    const ok = p.filter(x => x.ok), bl = part === "backing" ? shLanding("backing") : null;
    if (!ok.length) return part === "lead" ? "" : `<div class="sh-choice"><span class="lab">${lab}</span><span class="land-say no">${esc(bl.s)}</span></div>`;
    const poor = ok.map(x => x.ok.fit?.said).filter(Boolean);
    return `<div class="sh-choice"><span class="lab">${lab}</span>${ok.map(x => `<button class="pill" type="button" data-pick-c="${SH.probes.indexOf(x)}">${esc(x.c.label)}</button>`).join("")}</div>`
      + (bl && bl.s !== land?.s && bl.k !== "ok" ? `<p class="land-say ${bl.k}">${esc(bl.s)}</p>` : "")
      + poor.map(s => `<p class="land-say warn">${esc(s)}</p>`).join("");
  };
  h += `<div class="sh-choices">${row("lead", "The singing")}${row("backing", "The backing")}</div>`;
  if (mixed.lead) h += `<p class="meta">The two singers sit at different octaves here, so pick one singer's stretch to change its octave.</p>`;
  return h + `</div>`;
}
function shWire() {
  const box = $("#shape");
  $$("[data-sh]", box).forEach(b => b.onclick = () => {
    const k = b.dataset.sh;
    if (k === "done") return shapeClose();
    if (k === "retry") { SH.state = "loading"; SH.error = null; shDraw(); return shLoad(true); }
    if (k === "undo" || k === "redo") return shStep(k);
    if (k === "keep") { SH.confirm = true; shDraw(); return $('[data-sh="keep-yes"]', box)?.focus(); }
    if (k === "keep-yes") return shKeep();
    if (k === "keep-no") { SH.confirm = false; return shDraw(); }
    if (k === "unpick") { SH.pick = null; SH.probes = null; SH.said = null; SH.seq++; return shDraw(); }
  });
  $$("[data-hear]", box).forEach(b => b.onclick = () => { const [t0, t1] = b.dataset.hear.split(",").map(Number); shHear(t0, t1); });
  $$("[data-pick-c]", box).forEach(b => b.onclick = () => shSend(SH.probes[+b.dataset.pickC].c));
  box.onkeydown = e => { if (e.key === "Escape" && SH.confirm) { SH.confirm = false; shDraw(); $('[data-sh="keep"]', box)?.focus(); } };
  const lyr = $(".lyr", box);
  if (!lyr) return;
  const choose = i => {
    const p = SH.pick;
    SH.pick = !p || p[0] !== p[1] || i < p[0] ? [i, i] : [p[0], i];
    SH.focus = i; SH.said = null;
    shDraw(); $(`[data-w="${i}"]`, box)?.focus();
    if (!SH.busy) shProbe();
  };
  lyr.onclick = e => { const w = e.target.closest("[data-w]"); if (w) choose(+w.dataset.w); };
  lyr.onkeydown = e => {   // one stop in the tab order; the arrows move along the words (a roving tabindex)
    const w = e.target.closest("[data-w]"); if (!w) return;
    const i = +w.dataset.w, n = SH.doc.words.length;
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); return choose(i); }
    const to = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: n - 1 }[e.key];
    if (to == null || to < 0 || to >= n) return;
    e.preventDefault();
    w.tabIndex = -1; SH.focus = to;
    const next = $(`[data-w="${to}"]`, box); next.tabIndex = 0; next.focus();
  };
}
