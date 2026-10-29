"use strict";
// UnderCover's service worker (1.2): registered by a paired phone at the away address only (a service worker needs
// HTTPS, so the home Wi-Fi address can't have one). It caches nothing of the user's and no page: every request goes to
// the PC as it would without it. It keeps one thing, the PC's home-network address (the page hands it over, checked
// against the private-address forms below), so that when the PC doesn't answer here, the phone gets a small page of
// its own that says so and offers that address. It also opens UnderCover when a job notification is tapped.
// Hooks for the look chat: the offline page's #sw-down, #sw-home.
const STORE = "uc-home-v1", HOME = "/__uc/home", WAIT_MS = 20000;   // (a busy PC over a slow phone line still makes it)
const HOME_RX = /^http:\/\/(10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}):\d{2,5}$/;

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", e => e.waitUntil(self.clients.claim()));

self.addEventListener("message", e => {   // {home: "http://192.168.1.20:7860"} from the page, after each /api/status
  const h = e.data && e.data.home;
  if (typeof h === "string" && HOME_RX.test(h)) e.waitUntil(caches.open(STORE).then(c => c.put(HOME, new Response(h))));
});

self.addEventListener("fetch", e => {
  const r = e.request;
  if (r.mode !== "navigate" || r.method !== "GET") return;   // the page's own requests: straight to the PC, never kept
  e.respondWith(answer(r));
});

async function answer(r) {
  try {
    return await Promise.race([fetch(r), new Promise((_, no) => setTimeout(() => no(new Error("no answer")), WAIT_MS))]);
  } catch {
    return down();
  }
}

async function down() {
  let home = "";
  try {
    const m = await (await caches.open(STORE)).match(HOME);
    if (m) home = await m.text();
  } catch { /* no address kept yet */ }
  if (!HOME_RX.test(home)) home = "";   // only an address of the form above is ever written into the page
  const html = `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#0a0908"><title>UnderCover</title>
<style>:root{color-scheme:dark}body{margin:0;min-height:100vh;display:grid;place-items:center;padding:24px 16px;background:#0a0908;color:#ece7de;font:16px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:380px;width:100%}.mark{font-size:13px;letter-spacing:.18em;text-transform:uppercase;color:#a39d93}h1{font-size:24px;margin:6px 0 4px;font-weight:600}p{color:#a39d93}a{color:#c9a45c}</style></head>
<body><main><div class="mark">UnderCover</div><h1 id="sw-down">Your PC isn't answering</h1>
<p>Nothing came back from it through the away link. Is the PC on, with away from home switched on?</p>
<p><a href="/">Try again</a></p>${home ? `
<p id="sw-home">Only if this phone is on your own home Wi-Fi now: <a href="${home}/">open the home address</a> (${home.slice(7)}). Anywhere else, leave it.</p>` : ""}
</main></body></html>`;
  return new Response(html, { status: 503, headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'" } });
}

self.addEventListener("notificationclick", e => {   // a job notification tapped: the open UnderCover, or a new one
  e.notification.close();
  e.waitUntil((async () => {
    const open = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const c of open) if ("focus" in c) return c.focus();
    return self.clients.openWindow("/");
  })());
});
