"use strict";
// UnderCover's service worker (1.2; one link since 1.2.1): registered by a connected phone at its link (a service worker
// needs HTTPS). It caches nothing and keeps nothing: every request goes to the PC as it would without it, and a phone that
// was removed has its storage cleared by the server's answer (Clear-Site-Data on that 401). When the PC doesn't answer,
// the phone gets a small page of its own that says so, with a link to try this same address again and nothing else. It
// also opens UnderCover when a job notification is tapped. Hooks for the look chat: the offline page's #sw-down.
const WAIT_MS = 20000;   // (a busy PC over a slow phone line still makes it)
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", e => e.waitUntil((async () => {
  for (const k of await caches.keys()) await caches.delete(k);   // (what 1.2 kept, the home address, goes with the one link)
  await self.clients.claim();
})()));
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
function down() {
  const html = `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#0a0908"><title>UnderCover</title>
<style>:root{color-scheme:dark}body{margin:0;min-height:100vh;display:grid;place-items:center;padding:24px 16px;background:#0a0908;color:#ece7de;font:16px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:380px;width:100%}.mark{font-size:13px;letter-spacing:.18em;text-transform:uppercase;color:#a39d93}h1{font-size:24px;margin:6px 0 4px;font-weight:600}p{color:#a39d93}a{color:#c9a45c}</style></head>
<body><main><div class="mark">UnderCover</div><h1 id="sw-down">Your PC isn't answering</h1>
<p>Nothing came back from it. Is the PC on, with UnderCover running?</p>
<p><a href="/">Try again</a></p>
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
