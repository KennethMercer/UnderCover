"use strict";
// Find a setting (the maintainers' design notes, settings-search): one box at the top of SafeHouse's Settings, System and Add-ons that
// searches all three and the top bar's Low profile and Deep Cover. A row matches on its name, its small text and its find-words
// (data-find, next to the row in index.html, so a new row brings its own and the old name of a renamed thing still finds it).
// Matches in the room you're in stay where they are and the rest fold away; matches elsewhere are listed under them with Go there.
// A Deep Cover row never shows in basic view: it's counted for one line that offers Deep Cover. Typed words are only ever text.
// Uses app.js ($, $$, setDeep) and rooms.js (R, ROOMS, MOD_NAMES, renderAddons). rooms.js calls findPlace() after every room change
// and every add-on redraw; app.js's setDeep calls findRun().

const FIND = { q: "", go: null, timer: 0 };
const FIND_ROOMS = { settings: "Settings", system: "System", addons: "Add-ons" };
const FIND_STOP = new Set(["a", "an", "the", "to", "of", "on", "in", "from", "for", "and", "or", "my", "is", "it", "i", "how", "where", "do"]);
const findFoldText = s => String(s || "").normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();
const findWords = s => findFoldText(s).split(/[^a-z0-9½]+/).filter(Boolean);
// the words asked: small words dropped, unless that leaves nothing
function findAsked(q) { const ws = findWords(q), keep = ws.filter(w => !FIND_STOP.has(w)); return keep.length ? keep : ws; }
// 0 unless every word asked starts a word of the row's name, find-words or small text; then 3 if the name has one, 2 the find-words, 1 the text
function findScore(e, qs) {
  if (!qs.length) return 0;
  const fields = [[findWords(e.name), 3], [findWords(e.find), 2], [findWords(e.say), 1]];
  const has = (ws, w) => ws.some(x => x.startsWith(w));
  if (!qs.every(w => fields.some(([ws]) => has(ws, w)))) return 0;
  return Math.max(...fields.filter(([ws]) => qs.some(w => has(ws, w))).map(([, s]) => s));
}

// a row is out of the index when something other than Deep Cover hides it: an attribute on the way up to its room (Your ear before a
// thumb, the phone's box while it's closed), or the other device's half (data-pc on a phone, data-phone on the PC)
function findHidden(el, room) {
  for (let n = el; n && n !== room; n = n.parentElement) if (n.hidden) return true;
  return !!el.closest(document.body.dataset.client === "phone" ? "[data-pc]" : "[data-phone]");
}
const findText = (el, sel) => [...el.querySelectorAll(sel)].map(s => s.textContent.trim()).filter(Boolean).join(" ");
// the index, from the page as it is now (rows come and go), the add-ons from the room's own list, and the top bar's two
function findIndex() {
  const out = [];
  for (const room of ["settings", "system"]) {
    const box = $(`#room-${room}`);
    for (const el of box.querySelectorAll("[data-find]")) {
      if (findHidden(el, box)) continue;
      const head = el.querySelector(":scope > span > b, :scope > .rn-lbl > b") || el.querySelector("b");
      out.push({ room, el, name: el.dataset.findName || head?.textContent.trim() || "", find: el.dataset.find,
                 say: findText(el, ":scope > span > small, :scope > .rn-lbl > small, :scope > .meta"), deep: !!el.closest("[data-deep]") });
    }
  }
  for (const a of R.addons || []) {
    const el = $(`#ad-list .addon[data-id="${CSS.escape(a.id)}"]`);
    out.push({ room: "addons", el, key: a.id, name: a.name, say: a.adds || "", deep: false,
               find: ["add-on", "addon", a.licence, a.optional ? "optional" : "included", ...(a.modules || []).map(m => MOD_NAMES[m] || m)].join(", ") });
  }
  const top = [["#lp-chip", "Low profile", "The trenchcoat with ½ in the top bar. Press it to turn it on or off.",
                "gpu, memory, vram, game, gaming, games, half, slow down, background"],
               ["#deep-toggle", "Deep Cover", "The glasses in the top bar.", "expert, advanced, glasses"]];
  for (const [sel, name, say, find] of top) {
    const el = $(sel);
    if (el && !el.hidden && el.getClientRects().length) out.push({ room: "top", el, name, say, find, deep: false });
  }
  return out;
}

// the typed words marked where they start a word, in text that is only text (a row's live line is set as text); nothing typed becomes markup
function findMark(el, qs) {
  if (!el || ![...el.childNodes].every(n => n.nodeType === 3)) return;
  const text = el.textContent, re = /[\p{L}\p{N}½]+/gu, frag = document.createDocumentFragment();
  let i = 0, m, hit = false;
  while ((m = re.exec(text))) {
    const w = findFoldText(m[0]), q = qs.filter(x => w.startsWith(x)).sort((a, b) => b.length - a.length)[0];
    if (!q) continue;
    frag.append(text.slice(i, m.index));
    const mk = document.createElement("mark"); mk.className = "sf-mark"; mk.textContent = m[0].slice(0, q.length);
    frag.append(mk); i = m.index + q.length; hit = true;
  }
  if (!hit) return;
  frag.append(text.slice(i)); el.replaceChildren(frag);
}
function findUnmark() {
  for (const mk of $$("mark.sf-mark")) { const p = mk.parentNode; mk.replaceWith(mk.textContent); p.normalize(); }
  $$(".sf-out").forEach(n => n.classList.remove("sf-out"));
}
// fold away what doesn't match in the room: every block that holds no matching row (an empty section goes with its rows); never re-ordered
function findFold(node, shown) {
  let any = false;
  for (const c of node.children) {
    let keep;
    if (c.matches(".roomhead, .sf, .sf-more, .alert")) keep = true;
    else if (shown.has(c)) keep = true;
    else if (c.matches("[data-find], .addon")) keep = false;
    else keep = !!c.querySelector("[data-find], .addon") && findFold(c, shown);
    if (!keep) c.classList.add("sf-out");
    any ||= keep;
  }
  return any;
}

const findEl = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
const findPlural = n => n === 1 ? "1 match" : `${n} matches`;

function findRun() {
  const room = R.room, box = $(`#room-${room}`), more = $("#sh-find-more"), count = $("#sh-find-n");
  if (!box || !FIND_ROOMS[room] || !more) return;
  findUnmark();
  more.replaceChildren();
  const q = FIND.q, qs = findAsked(q);
  $("#sh-find-x").hidden = !q;
  if (!qs.length) { count.hidden = true; return; }
  const deep = document.body.classList.contains("deep");
  const hits = findIndex().map(e => ({ ...e, s: findScore(e, qs) })).filter(e => e.s);
  const seen = hits.filter(e => deep || !e.deep), later = hits.filter(e => e.deep && !deep);
  const here = seen.filter(e => e.room === room && e.el), away = seen.filter(e => e.room !== room).sort((a, b) => b.s - a.s);
  findFold(box, new Set(here.map(e => e.el)));
  for (const e of here) for (const t of e.el.querySelectorAll(":scope > span > b, :scope > span > small, :scope > .rn-lbl > b, :scope > .rn-lbl > small, h4, .what")) findMark(t, qs);
  const n = here.length + away.length;
  count.hidden = !n; count.textContent = findPlural(n);
  if (away.length) {
    const list = findEl("div", "sf-elsewhere");
    list.append(findEl("span", "sf-h", "In other rooms"));
    for (const e of away) {
      const row = findEl("div", "sf-hit"), say = findEl("span"), b = findEl("b", "", e.name), small = findEl("small", "", e.say);
      findMark(b, qs); findMark(small, qs);
      say.append(b, " ", findEl("span", "sf-where", e.room === "top" ? "Top bar" : FIND_ROOMS[e.room]), small);
      const go = findEl("button", "btn", "Go there"); go.type = "button"; go.onclick = () => findGo(e);
      row.append(say, go); list.append(row);
    }
    more.append(list);
  }
  if (!n && !later.length) {
    const none = findEl("div", "sf-none");
    none.append(findEl("b", "", `Nothing here matches “${q}”.`),
                findEl("span", "meta", "Try another word, or ask the Doctor in System if something isn't working."));
    more.append(none);
  }
  if (later.length) {
    const p = findEl("p", "sf-deep"), on = findEl("button", "linkbtn", "Turn on Deep Cover");
    on.type = "button"; on.onclick = () => { setDeep(true); $("#sh-find")?.focus(); };
    p.append(later.length === 1 ? `${later[0].name} is in Deep Cover. ` : `${later.length}${n ? " more" : ""} matches are in Deep Cover. `,
             on, ` to see ${later.length === 1 ? "it" : "them"}.`);
    more.append(p);
  }
  findLight();
}

// Go there: the room opens with the search still in it, and the row lights for a moment; a top bar chip lights where it is
function findGo(e) {
  if (e.room === "top") { findFlash(e.el); e.el.focus(); return; }
  FIND.go = e.room === "addons" ? { room: "addons", key: e.key } : { room: e.room, el: e.el };
  if (e.room === "addons" && R.filter !== "all") R.filter = "all";
  location.hash = `safehouse/${e.room}`;
}
function findLight() {
  const g = FIND.go;
  if (!g || g.room !== R.room) return;
  const el = g.el || (g.key && $(`#ad-list .addon[data-id="${CSS.escape(g.key)}"]`));
  if (!el) return;   // (the add-ons list draws when it has loaded: findPlace runs again then)
  FIND.go = null;
  el.scrollIntoView({ block: "center", behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
  findFlash(el);
}
function findFlash(el) { el.classList.add("sf-lit"); setTimeout(() => el.classList.remove("sf-lit"), 1600); }

// the one box moves to the top of the room that's open, keeping what's typed
function findPlace() {
  const wrap = $("#sh-find-wrap"), room = $(`#room-${R.room}`);
  if (!wrap) return;
  if (!FIND_ROOMS[R.room]) { findUnmark(); return; }
  const head = room.querySelector(".roomhead");
  if (wrap.previousElementSibling !== head) head.after(wrap);
  const more = $("#sh-find-more");
  if (more.parentNode !== room || room.lastElementChild !== more) room.append(more);
  findRun();
}

(function () {
  const input = $("#sh-find");
  if (!input) return;
  const set = v => { FIND.q = v; findRun(); };
  input.addEventListener("input", () => set(input.value.trim()));
  input.addEventListener("keydown", e => { if (e.key === "Escape" && input.value) { e.preventDefault(); input.value = ""; set(""); } });
  $("#sh-find-x").addEventListener("click", () => { input.value = ""; set(""); input.focus(); });
})();
