"use strict";
// Themes (docs/THEMES.md): a theme is a grading of the look plus its type, tokens only. index.html's head applies
// the remembered one before first paint (html[data-uc-theme] + the #uc-theme-css link); this file lists the installed
// themes in SafeHouse's Settings room (Deep Cover), imports and exports them as one file (Name.uctheme), and holds the
// maker: pick a few things, see the page change, save, share. The maker works out the rest (deep tones, the light mode)
// with the rules in themes.md. Uses helpers from app.js ($, $$, esc, api, showError, redrawAll) and library.js (confirmClick).

const TH = { list: [], tokens: {}, base: null, maker: null };
const THEME_KEYS = ["--base", "--panel-base", "--panel-2-base", "--panel-3-base", "--line-base", "--fg", "--muted", "--accent-ink",
  "--c-frontman", "--c-doubleagent", "--c-speakeasy", "--c-safehouse", "--c-frontman-deep", "--c-doubleagent-deep",
  "--c-speakeasy-deep", "--c-safehouse-deep", "--ok", "--warn", "--err", "--wave", "--wave-on", "--wave-alt",
  "--display", "--body", "--mono", "--r", "--lit", "--tint", "--tint2", "--lamp", "--t-fast", "--t-slow"];
const ONE_PER_THEME = ["--display", "--body", "--mono", "--r", "--t-fast", "--t-slow"];   // not per mode: the dark block holds them
const MODS4 = [["frontman", "FrontMan"], ["doubleagent", "DoubleAgent"], ["speakeasy", "SpeakEasy"], ["safehouse", "SafeHouse"]];
// the families UnderCover ships (app.css @font-face); a maker can add its own OFL font on top
const FAMILIES = { text: ["Schibsted Grotesk", "Manrope", "Figtree", "Hanken Grotesk", "Geist"],
                   numbers: ["JetBrains Mono", "DM Mono", "IBM Plex Mono", "Red Hat Mono", "Geist Mono"] };
const SANS = '"Segoe UI", system-ui, sans-serif', MONO = "Consolas, ui-monospace, monospace";

// ------------------------------------------------------------ applying one
function themeId() { try { return localStorage.getItem("uc-theme-id") || "casefile"; } catch (e) { return "casefile"; } }
function applyThemeEntry(t) {
  const root = document.documentElement, link = $("#uc-theme-css");
  $("#uc-theme-derived")?.remove();
  if (!t || t.id === "casefile") { delete root.dataset.ucTheme; link.removeAttribute("href"); }
  else {
    root.dataset.ucTheme = t.id; link.href = t.css;
    if (!(t.modes || []).includes("light")) deriveLightFor(t);   // a theme that gives only dark: the page works out light
  }
  try { localStorage.setItem("uc-theme-id", t ? t.id : "casefile"); if (t && t.id !== "casefile") localStorage.setItem("uc-theme-css", t.css); else localStorage.removeItem("uc-theme-css"); } catch (e) {}
  link.onload = () => redrawAll();
  redrawAll();
}
async function deriveLightFor(t) {
  const tok = await themeTokens(t); if (!tok?.dark) return;
  const light = deriveLight(tok.dark, (await baseTokens()).light);
  const st = document.createElement("style"); st.id = "uc-theme-derived";
  st.textContent = cssBlock(`:root[data-uc-theme="${t.id}"][data-theme="light"]`, light, true);
  document.head.append(st);
}

// ------------------------------------------------------------ reading tokens out of a theme.css (and app.css for Case File)
function parseBlocks(css) {
  const out = { dark: {}, light: {} };
  css = css.replace(/\/\*[\s\S]*?\*\//g, " ");   // comments out first: a theme starts with one
  for (const m of css.matchAll(/([^{}]*)\{([^{}]*)\}/g)) {
    const sel = m[1].trim(); if (sel.startsWith("@")) continue;
    const mode = /data-theme="light"/.test(sel) ? "light" : "dark";
    if (!/^:root/.test(sel)) continue;
    for (const d of m[2].matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)) if (THEME_KEYS.includes(d[1])) out[mode][d[1]] = d[2].trim();
  }
  return out;
}
async function baseTokens() {   // Case File: app.css's own :root and :root[data-theme="light"]
  if (TH.base) return TH.base;
  const css = await (await fetch("/static/app.css")).text();
  const root = parseBlocks(css.split(/\n:root\[data-theme="light"\]/)[0].match(/:root\s*\{[^}]*\}/)[0]).dark;
  const light = parseBlocks(css.match(/:root\[data-theme="light"\]\s*\{[^}]*\}/)[0]).light;
  return (TH.base = { dark: root, light: { ...root, ...light } });
}
async function themeTokens(t) {
  if (TH.tokens[t.id]) return TH.tokens[t.id];
  const base = await baseTokens();
  if (t.id === "casefile") return (TH.tokens[t.id] = base);
  try {
    const tok = parseBlocks(await (await fetch(t.css)).text());
    const dark = { ...base.dark, ...tok.dark };
    const light = Object.keys(tok.light).length ? { ...base.light, ...tok.light } : deriveLight(dark, base.light);
    return (TH.tokens[t.id] = { dark, light });
  } catch (e) { return null; }
}
const cssBlock = (sel, tok, light = false) => `${sel} {\n${Object.entries(tok).filter(([k]) => THEME_KEYS.includes(k) && !(light && ONE_PER_THEME.includes(k))).map(([k, v]) => `  ${k}: ${v};`).join("\n")}\n}\n`;

// ------------------------------------------------------------ colour: OKLCH, and the derivation rules (themes.md)
const lin = c => (c /= 255) <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
const unlin = c => Math.max(0, Math.min(255, Math.round((c <= 0.0031308 ? c * 12.92 : 1.055 * c ** (1 / 2.4) - 0.055) * 255)));
function toOklch(hex) {
  const n = hex.replace("#", ""), [r, g, b] = [0, 2, 4].map(i => lin(parseInt(n.slice(i, i + 2), 16)));
  const l = Math.cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b), m = Math.cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b),
        s = Math.cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b);
  const L = 0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s, A = 1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s,
        B = 0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s;
  return { L, C: Math.hypot(A, B), h: Math.atan2(B, A) };
}
function fromOklch({ L, C, h }) {
  let rgb;
  for (let i = 0; i < 40; i++, C *= 0.92) {   // pull chroma in until the colour fits sRGB
    const a = C * Math.cos(h), b = C * Math.sin(h);
    const l = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3, m = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3, s = (L - 0.0894841775 * a - 1.2914855480 * b) ** 3;
    rgb = [4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s, -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s, -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s];
    if (rgb.every(c => c >= -0.0005 && c <= 1.0005)) break;
  }
  return "#" + rgb.map(c => unlin(Math.min(1, Math.max(0, c))).toString(16).padStart(2, "0")).join("");
}
const isHex = v => /^#[0-9a-f]{6}$/i.test(v || "");
const yellowish = h => { const d = (h * 180 / Math.PI + 360) % 360; return d > 50 && d < 115; };
// a module's deep tone (the air of its page) from its colour: dark, same hue, a little more chroma
const deepOf = hex => { const c = toOklch(hex); return fromOklch({ L: yellowish(c.h) ? 0.46 : 0.36, C: Math.min(c.C * 1.3, c.C + 0.03), h: c.h }); };
// the panels from the near-black the user picked
function airFrom(base) {
  const c = toOklch(base), at = (dl, dc = 0.001) => fromOklch({ L: c.L + dl, C: c.C + dc, h: c.h });
  return { "--base": base, "--panel-base": at(0.037), "--panel-2-base": at(0.073), "--panel-3-base": at(0.111), "--line-base": at(0.125), "--wave": at(0.17, 0.004) };
}
// the light mode from the dark one: colours to reading lightness on a light panel, deep tones lifted, the air mirrored
function deriveLight(dark, lightBase) {
  const out = { ...lightBase };
  for (const [m] of MODS4) {
    const v = dark[`--c-${m}`]; if (!isHex(v)) continue;
    const c = toOklch(v);
    out[`--c-${m}`] = fromOklch({ L: 0.42, C: Math.min(c.C * 1.25 + 0.002, 0.13), h: c.h });
    out[`--c-${m}-deep`] = fromOklch({ L: 0.58, C: Math.min(c.C * 1.3 + 0.002, 0.15), h: c.h });
  }
  if (isHex(dark["--base"])) {
    const b = toOklch(dark["--base"]), at = L => fromOklch({ L, C: b.C * 2 + 0.003, h: b.h });
    Object.assign(out, { "--base": at(0.926), "--panel-base": at(0.971), "--panel-2-base": at(0.932), "--panel-3-base": at(0.892), "--line-base": at(0.855), "--wave": at(0.83) });
  }
  out["--wave-on"] = out["--c-frontman"]; out["--wave-alt"] = out["--c-speakeasy"];
  for (const [k, f] of [["--lit", 0.55], ["--tint", 0.55], ["--tint2", 0.4], ["--lamp", 0.65]]) {
    const p = parseFloat(dark[k]); if (!isNaN(p)) out[k] = `${Math.round(p * f)}%`;
  }
  for (const k of ["--display", "--body", "--mono", "--r", "--t-fast", "--t-slow"]) delete out[k];   // not per mode
  return out;
}
// contrast (WCAG), for the maker's "hard to read" note
const lum = hex => { const n = hex.replace("#", ""); const [r, g, b] = [0, 2, 4].map(i => lin(parseInt(n.slice(i, i + 2), 16))); return 0.2126 * r + 0.7152 * g + 0.0722 * b; };
const contrast = (a, b) => { const x = lum(a), y = lum(b); return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05); };

// ------------------------------------------------------------ the Settings row (Deep Cover): installed themes
async function loadThemes() {
  try { TH.list = (await api("/api/themes")).themes || []; }
  catch (e) { $("#set-themes").innerHTML = `<p class="meta">${esc(e.message)}</p>`; return; }
  const cur = TH.list.find(t => t.id === themeId());
  if (!cur && themeId() !== "casefile") applyThemeEntry(null);   // the remembered theme is gone: back to Case File
  renderThemes();
}
async function renderThemes() {
  const cur = themeId(), mode = document.documentElement.dataset.theme === "light" ? "light" : "dark";
  const toks = await Promise.all(TH.list.map(t => themeTokens(t)));
  $("#set-themes").innerHTML = TH.list.map((t, i) => {
    const tok = toks[i]?.[mode] || {}, sw = k => esc(tok[k] || "transparent");
    return `<button class="theme-card" type="button" data-theme-id="${esc(t.id)}" aria-pressed="${t.id === cur}" ${t.ok === false ? "disabled" : ""}>
      <span class="theme-sw" style="background:${sw("--base")}">${MODS4.map(([m]) => `<i style="background:${sw(`--c-${m}`)}"></i>`).join("")}</span>
      <span><b>${esc(t.name)}${t.id === cur ? ` <span class="theme-on">on</span>` : ""}</b><small>${esc(t.author || "")}${t.fonts?.[0] ? ` · ${esc(t.fonts[0])}` : ""}</small>${t.ok === false ? `<small class="bad">${esc(t.why || "can't be used")}</small>` : ""}</span></button>`;
  }).join("") || `<p class="meta">No themes found.</p>`;
  const t = TH.list.find(x => x.id === cur) || TH.list.find(x => x.id === "casefile");
  const ex = $("#set-theme-export");
  ex.hidden = !t;
  if (t) { ex.href = `/api/themes/${encodeURIComponent(t.id)}/export`; ex.textContent = `Export ${t.name}`; }
  const rm = $("#set-theme-remove");
  rm.hidden = !t || t.shipped;
  if (t && !t.shipped) rm.dataset.id = t.id;
}
$("#set-themes").addEventListener("click", e => {
  const b = e.target.closest("[data-theme-id]"); if (!b) return;
  if (TH.maker) closeMaker(false);   // picking a theme ends the one being made
  applyThemeEntry(TH.list.find(t => t.id === b.dataset.themeId));
  renderThemes();
});
$("#set-theme-import").onclick = () => $("#set-theme-file").click();
$("#set-theme-file").onchange = e => { const f = e.target.files[0]; e.target.value = ""; if (f) importTheme(f, false); };
async function importTheme(f, replace) {
  let r, data;
  try {
    r = await fetch(`/api/themes/import${replace ? "?replace=1" : ""}`, { method: "POST", body: f, headers: { "Content-Type": "application/zip" } });
    data = await r.json().catch(() => ({}));
  } catch (err) { return showError("Lost contact with UnderCover.", "Couldn't import that theme"); }
  if (r.status === 409 && !replace)
    return showError(`${data.error || "You already have a theme with this name."} Replace yours with the one in ${f.name}?`, "Already in your themes",
                     { label: "Replace it", run: () => { $("#err").hidden = true; importTheme(f, true); } });
  if (!r.ok) return showError(data.error || `Request failed (${r.status}).`, "Couldn't import that theme");
  delete TH.tokens[data.id];
  await loadThemes();
  showError(`${data.name}${data.author ? ` by ${data.author}` : ""} is in your themes. Pick it to use it.`, "Theme imported", null, true);
}
confirmClick($("#set-theme-remove"), "Remove it?", async () => {
  const id = $("#set-theme-remove").dataset.id;
  await api(`/api/themes/${encodeURIComponent(id)}`, { method: "DELETE" });
  delete TH.tokens[id];
  if (themeId() === id) applyThemeEntry(null);
  loadThemes();
});
document.addEventListener("uc:themechange", () => { if (!$("#room-settings").hidden) renderThemes(); });   // dark/light flipped

// ------------------------------------------------------------ the maker
const CORNERS = [["Round", "10px"], ["Soft", "8px"], ["Square", "5px"]];
$("#set-theme-make").onclick = () => openMaker();
async function openMaker() {
  if (TH.maker) return;
  const from = TH.list.find(t => t.id === themeId()) || TH.list[0];
  TH.maker = { prev: themeId(), fonts: [], lightEdited: {}, name: "", author: "" };
  $("#set-theme-make").disabled = true;
  await startFrom(from?.id || "casefile");
  $("#theme-maker").hidden = false;
  $("#theme-maker").scrollIntoView({ behavior: "smooth", block: "start" });
}
async function startFrom(id) {
  const t = TH.list.find(x => x.id === id) || { id: "casefile" }, tok = await themeTokens(t) || await baseTokens();
  const mk = TH.maker;
  mk.from = id; mk.dark = { ...tok.dark }; mk.light = { ...tok.light }; mk.lightStart = { ...tok.light }; mk.lightEdited = {};
  renderMaker(); preview();
}
const famOf = v => (v || "").match(/"([^"]+)"/)?.[1] || "";
function renderMaker() {
  const mk = TH.maker, d = mk.dark;
  const fams = kind => [...FAMILIES[kind], ...mk.fonts.map(f => f.family)];
  const sel = (id, kind, cur) => `<span class="in"><select id="${id}" aria-label="${kind === "text" ? "Text" : "Numbers"}">${[...new Set(fams(kind))].map(f => `<option${f === cur ? " selected" : ""}>${esc(f)}</option>`).join("")}</select></span>`;
  const well = (m, name) => {
    const v = d[`--c-${m}`], low = isHex(v) && isHex(d["--panel-base"]) && contrast(v, d["--panel-base"]) < 4.5;
    return `<label class="mk-well"><input type="color" data-mk-c="${m}" value="${esc(v)}" aria-label="${name} colour"><b>${name}</b>
      <small class="mono">${esc(v)}</small>${low ? `<small class="bad">hard to read</small>` : ""}</label>`;
  };
  const lightWell = (k, name) => `<label class="mk-well"><input type="color" data-mk-l="${k}" value="${esc(mk.light[k])}" aria-label="${name} in light mode"><b>${name}</b>
      <small class="mono">${esc(mk.light[k])}${mk.lightEdited[k] ? " · yours" : ""}</small></label>`;
  $("#theme-maker").innerHTML = `
    <div class="rn-lbl"><b>Make a theme</b><small>Start from one you have, change what you like and watch the page follow. The rest is worked out for you.</small></div>
    <div class="maker-grid"><div class="maker-ctl">
      <div class="mk"><span class="mk-h">Name <small>and who made it</small></span>
        <div class="row"><span class="in"><input id="mk-name" maxlength="40" placeholder="Harbour" value="${esc(mk.name)}" aria-label="Theme name"></span><span class="in"><input id="mk-author" maxlength="40" placeholder="Your name" value="${esc(mk.author)}" aria-label="Who made it"></span></div></div>
      <div class="mk"><span class="mk-h">Start from</span><span class="seg" id="mk-from">${TH.list.filter(t => t.ok !== false).map(t => `<button type="button" data-v="${esc(t.id)}" aria-pressed="${t.id === mk.from}">${esc(t.name)}</button>`).join("")}</span></div>
      <div class="mk"><span class="mk-h">The air <small>the near-black everything sits in, and the lamp</small></span>
        <div class="row"><label class="mk-well mk-base"><input type="color" id="mk-base" value="${esc(d["--base"])}" aria-label="The air"><small class="mono">${esc(d["--base"])}</small></label>
          <label class="mk-range"><span>Lamp <output id="mk-lamp-out">${parseInt(d["--lamp"]) || 0}%</output></span><input type="range" id="mk-lamp" min="0" max="30" value="${parseInt(d["--lamp"]) || 0}"></label></div></div>
      <div class="mk"><span class="mk-h">The four colours <small>one per module; their deep and light tones follow</small></span>
        <div class="mk-wells">${MODS4.map(([m, n]) => well(m, n)).join("")}</div></div>
      <div class="mk"><span class="mk-h">Type <small>text and numbers; or add an OFL font file</small></span>
        <div class="row">${sel("mk-body", "text", famOf(d["--body"]))}${sel("mk-mono", "numbers", famOf(d["--mono"]))}
          <button class="linkbtn" type="button" id="mk-font-add">Add a font…</button><input type="file" id="mk-font" accept=".woff2" multiple hidden></div>
        ${mk.fonts.length ? `<small class="meta">Bundled with this theme: ${mk.fonts.map(f => esc(f.family)).join(", ")}. Fonts you add must be free to share (SIL Open Font License).</small>` : ""}</div>
      <div class="mk"><span class="mk-h">Corners</span><span class="seg" id="mk-corners">${CORNERS.map(([n, v]) => `<button type="button" data-v="${v}" aria-pressed="${(d["--r"] || "10px") === v}">${n}</button>`).join("")}</span></div>
      <details class="mk" id="mk-light"><summary class="mk-h">Light mode <small>worked out from the dark one; change any of it</small></summary>
        <div class="mk-wells">${lightWell("--base", "The air")}${MODS4.map(([m, n]) => lightWell(`--c-${m}`, n)).join("")}</div></details>
      <div class="row mk-out"><button class="btn primary" type="button" id="mk-save">Save as a theme</button><button class="btn" type="button" id="mk-share">Save and share as a file</button>
        <button class="linkbtn" type="button" id="mk-cancel">Cancel</button></div>
    </div>
    <div class="maker-pv"><span class="mk-h">Live preview <small>the whole page follows while you make it</small></span>
      ${MODS4.map(([m, n]) => `<div class="mk-pv" data-module="${m}"><b>${n}</b><span class="btn primary">Make it</span><span class="mk-pv-wave">${Array.from({ length: 24 }, (_, i) => `<i class="${i < 9 ? "on" : ""}" style="height:${30 + ((i * 37) % 70)}%"></i>`).join("")}</span><small>Hello · Morgan Freeman</small></div>`).join("")}
    </div></div>`;
  wireMaker();
}
function wireMaker() {
  const mk = TH.maker, d = mk.dark;
  // what a change in dark mode carries over to the light mode (the start theme's light values stay for everything else)
  const FOLLOWS = { base: ["--base", "--panel-base", "--panel-2-base", "--panel-3-base", "--line-base", "--wave"], lamp: ["--lit", "--tint", "--tint2", "--lamp"],
                    ...Object.fromEntries(MODS4.map(([m]) => [m, [`--c-${m}`, `--c-${m}-deep`, ...({ frontman: ["--wave-on"], speakeasy: ["--wave-alt"] }[m] || [])]])) };
  const relight = what => { const L = deriveLight(d, mk.lightStart); for (const k of FOLLOWS[what]) if (!mk.lightEdited[k] && L[k]) mk.light[k] = L[k]; };
  $("#mk-name").oninput = e => { mk.name = e.target.value; };
  $("#mk-author").oninput = e => { mk.author = e.target.value; };
  $$("#mk-from button").forEach(b => b.onclick = () => startFrom(b.dataset.v));
  $("#mk-base").oninput = e => { Object.assign(d, airFrom(e.target.value)); relight("base"); preview(); };
  $("#mk-base").onchange = () => renderMaker();
  $("#mk-lamp").oninput = e => { d["--lamp"] = `${e.target.value}%`; $("#mk-lamp-out").textContent = `${e.target.value}%`; relight("lamp"); preview(); };
  $$("[data-mk-c]").forEach(inp => {
    inp.oninput = () => {
      const m = inp.dataset.mkC;
      d[`--c-${m}`] = inp.value; d[`--c-${m}-deep`] = deepOf(inp.value);
      if (m === "frontman") d["--wave-on"] = inp.value;
      if (m === "speakeasy") d["--wave-alt"] = inp.value;
      relight(m); preview();
    };
    inp.onchange = () => renderMaker();   // the hex and the "hard to read" note
  });
  $$("[data-mk-l]").forEach(inp => {
    inp.oninput = () => {
      const k = inp.dataset.mkL;
      mk.light[k] = inp.value; mk.lightEdited[k] = true;
      if (k === "--base") { const a = toOklch(inp.value), at = L => fromOklch({ L, C: a.C, h: a.h }); Object.assign(mk.light, { "--panel-base": at(a.L + 0.045), "--panel-2-base": at(a.L + 0.006), "--panel-3-base": at(a.L - 0.034), "--line-base": at(a.L - 0.071) }); }
      else mk.light[`${k}-deep`] = fromOklch({ ...toOklch(inp.value), L: 0.58 });
      preview();
    };
    inp.onchange = () => { const open = $("#mk-light").open; renderMaker(); $("#mk-light").open = open; };
  });
  $("#mk-body").onchange = e => { d["--body"] = d["--display"] = `"${e.target.value}", ${SANS}`; preview(); };
  $("#mk-mono").onchange = e => { d["--mono"] = `"${e.target.value}", ${MONO}`; preview(); };
  $("#mk-font-add").onclick = () => $("#mk-font").click();
  $("#mk-font").onchange = async e => {
    for (const f of e.target.files) {
      if (f.size > 1.5e6) { showError(`${f.name} is over 1.5 MB; a theme's fonts should be small web fonts (woff2).`, "Font too big"); continue; }
      const buf = await f.arrayBuffer();
      const family = f.name.replace(/\.woff2$/i, "").replace(/[-_](wght|variable|regular|vf)$/i, "").replace(/[-_]+/g, " ")
        .normalize("NFKD").replace(/[^A-Za-z0-9 ]/g, "").trim().slice(0, 40) || `Font ${mk.fonts.length + 1}`;   // theme.css allows plain names only
      try { const face = new FontFace(family, buf); await face.load(); document.fonts.add(face); }
      catch (err) { showError(`${f.name} isn't a font this browser can read.`, "Couldn't add the font"); continue; }
      mk.fonts.push({ family, file: `${family.replace(/ /g, "")}.woff2`, data: buf });   // starts with a letter or digit, as the folder rules want
    }
    e.target.value = ""; renderMaker();
  };
  $$("#mk-corners button").forEach(b => b.onclick = () => { d["--r"] = b.dataset.v; $$("#mk-corners button").forEach(x => x.setAttribute("aria-pressed", x === b)); preview(); });
  $("#mk-cancel").onclick = () => closeMaker(true);
  $("#mk-save").onclick = () => saveMaker(false);
  $("#mk-share").onclick = () => saveMaker(true);
}
// the whole page shows the draft while the maker is open (html[data-uc-theme="__draft"])
function preview() {
  const mk = TH.maker; if (!mk) return;
  let st = $("#uc-theme-draft");
  if (!st) { st = document.createElement("style"); st.id = "uc-theme-draft"; document.head.append(st); }
  st.textContent = cssBlock(':root[data-uc-theme="__draft"]', mk.dark) + cssBlock(':root[data-uc-theme="__draft"][data-theme="light"]', mk.light, true);
  document.documentElement.dataset.ucTheme = "__draft";
  redrawAll();
}
function closeMaker(restore) {
  $("#uc-theme-draft")?.remove();
  $("#theme-maker").hidden = true; $("#theme-maker").innerHTML = "";
  $("#set-theme-make").disabled = false;
  const prev = TH.maker?.prev; TH.maker = null;
  if (restore) applyThemeEntry(TH.list.find(t => t.id === prev) || null);
}
const slug = s => s.toLowerCase().normalize("NFKD").replace(/[^\w\s-]/g, "").trim().replace(/[\s_]+/g, "-").replace(/-+/g, "-").slice(0, 40);
const b64 = buf => { let s = ""; const a = new Uint8Array(buf); for (let i = 0; i < a.length; i += 0x8000) s += String.fromCharCode(...a.subarray(i, i + 0x8000)); return btoa(s); };
// the swatch card as a png: the four colours over the theme's base, dark above light
function previewPng() {
  const mk = TH.maker, c = document.createElement("canvas"); c.width = 320; c.height = 120;
  const g = c.getContext("2d");
  [[mk.dark, 0], [mk.light, 60]].forEach(([tok, y]) => {
    g.fillStyle = tok["--base"]; g.fillRect(0, y, 320, 60);
    MODS4.forEach(([m], i) => { g.fillStyle = tok[`--c-${m}`]; g.fillRect(16 + i * 74, y + 14, 66, 32); });
  });
  return c.toDataURL("image/png").split(",")[1];
}
async function saveMaker(share) {
  const mk = TH.maker, name = mk.name.trim(), id = slug(name);
  if (!name || id.length < 2) { $("#mk-name").focus(); return showError("Give the theme a name first.", "Name it"); }
  if (TH.list.some(t => t.id === id && t.shipped)) return showError(`${name} is one of UnderCover's own themes; pick another name.`, "Name taken");
  const mine = TH.list.find(t => t.id === id && !t.shipped);
  if (mine && mk.replaceOk !== id)
    return showError(`You already have a theme called ${mine.name}${mine.author ? ` (by ${mine.author})` : ""}. Replace it with this one?`, "Name taken",
                     { label: "Replace it", run: () => { $("#err").hidden = true; mk.replaceOk = id; saveMaker(share); } });
  const sel = `:root[data-uc-theme="${id}"]`;
  const fontCss = mk.fonts.map(f => `@font-face { font-family: "${f.family}"; src: url("fonts/${f.file}") format("woff2"); font-weight: 100 900; font-display: swap; }\n`).join("");
  const said = s => s.replace(/\*\/|\/\*/g, "");   // a name can't end the comment early
  const css = `/* ${said(name)}${mk.author.trim() ? ` by ${said(mk.author.trim())}` : ""}: made in UnderCover's theme maker, from ${mk.from}. Tokens only (docs/THEMES.md). */\n` +
    fontCss + cssBlock(sel, mk.dark) + cssBlock(`${sel}[data-theme="light"]`, mk.light, true);
  const json = { id, name, author: mk.author.trim() || "someone", licence: "free to share", version: "1.0", modes: ["dark", "light"],
                 based_on: mk.from, fonts: [famOf(mk.dark["--body"]), famOf(mk.dark["--mono"])].filter(Boolean) };
  const btn = share ? $("#mk-share") : $("#mk-save"); btn.disabled = true;
  try {
    const t = await api("/api/themes", { json: { json, css, preview: previewPng(), fonts: mk.fonts.map(f => ({ file: f.file, data: b64(f.data) })) } });
    delete TH.tokens[t.id];
    closeMaker(false);
    await loadThemes();
    applyThemeEntry(TH.list.find(x => x.id === t.id) || t);
    renderThemes();
    if (share) location.href = `/api/themes/${encodeURIComponent(t.id)}/export`;   // the browser saves Name.uctheme
    else showError(`${t.name} is saved and in use. Export it any time to send it to a friend.`, "Theme saved", null, true);
  } catch (err) { btn.disabled = false; showError(err.message, "Couldn't save the theme"); }
}

// the WinAmp skin is its own thing: while it's on, the theme steps aside (its light block would outrank the skin's colours)
let skinHeld = null;
new MutationObserver(() => {
  const root = document.documentElement, skin = root.dataset.skin === "winamp";
  if (skin && root.dataset.ucTheme) { skinHeld = root.dataset.ucTheme; delete root.dataset.ucTheme; }
  else if (!skin && skinHeld) { root.dataset.ucTheme = skinHeld; skinHeld = null; }
}).observe(document.documentElement, { attributes: true, attributeFilter: ["data-skin", "data-uc-theme"] });
if (document.documentElement.dataset.skin === "winamp" && document.documentElement.dataset.ucTheme) { skinHeld = document.documentElement.dataset.ucTheme; delete document.documentElement.dataset.ucTheme; }
// theming is Deep Cover: switching it off while a theme is being made puts the theme in use back
new MutationObserver(() => { if (TH.maker && !document.body.classList.contains("deep")) closeMaker(true); })
  .observe(document.body, { attributes: true, attributeFilter: ["class"] });
// at load: the head script already applied the remembered theme; check it still exists and give a dark-only one its light mode
setTimeout(async () => {
  const id = themeId(); if (id === "casefile") return;
  try { TH.list = (await api("/api/themes")).themes || []; } catch (e) { return; }
  const t = TH.list.find(x => x.id === id);
  if (!t || t.ok === false) applyThemeEntry(null);
  else if (!(t.modes || []).includes("light")) deriveLightFor(t);
}, 0);
// the room opens: list the themes (Deep Cover shows the row)
document.addEventListener("uc:module", e => { if (e.detail === "safehouse" && document.body.classList.contains("deep")) loadThemes(); });
document.addEventListener("uc:subpage", e => { if (String(e.detail) === "safehouse/settings") loadThemes(); });
