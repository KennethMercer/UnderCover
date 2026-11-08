"use strict";
// The phone in 1.2: "a job finished" while UnderCover is open on a paired phone, and the home-screen app at the away
// address. Fully local: nothing goes through a push service, so a phone that's locked, or has had the app in the
// background for a while, hears nothing (the browser has stopped the page by then; Android allows a few minutes, iOS
// seconds). What it does, on a phone only:
//   - page shown: a banner (#ph-alert) and, on Android, a short buzz;
//   - page hidden but still running: the app icon's badge counts the jobs, and at the link (HTTPS) a system
//     notification through the service worker (sw.js), once the phone has said yes (#ph-alerts);
//   - the overnight queue's results never buzz or ring (silent), whatever the hour;
//   - at the link: the service worker (sw.js) is registered, and #ph-install says how to keep the app on the home screen.
// Words are generic on purpose (a lock screen shows them): "Cover ready", never a song's or a voice's name. The look
// chat designs #ph-alert, #ph-alerts and #ph-install; the copy and the hooks are here. Uses DONE_WORDS (jobs.js), $ and
// store (app.js).
const PA = { unseen: 0, sw: null, refused: false, timer: 0 };
const paSecure = () => location.protocol === "https:";
const paStandalone = () => matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
const paIOS = () => /iPhone|iPad|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);

function paWords(j) {
  const st = j.final?.state || j.state;
  if (st === "done") return DONE_WORDS[j.kind] && !/^Preview/.test(j.title || "") ? DONE_WORDS[j.kind] : null;
  if (st === "error" && DONE_WORDS[j.kind]) return "A job stopped with a problem";
  return null;   // cancelled, paused, a listening copy, a preview: nothing to tell
}

function paBanner(text) {
  const b = $("#ph-alert");
  b.textContent = text;
  b.hidden = false;
  clearTimeout(PA.timer);
  PA.timer = setTimeout(() => { b.hidden = true; }, 6000);
}

document.addEventListener("uc:jobdone", async e => {
  if (document.body.dataset.client !== "phone") return;
  const j = e.detail, say = paWords(j);
  if (!say) return;
  const quiet = j.kind === "night";   // the overnight queue: no buzz at 04:00
  if (!document.hidden) {
    paBanner(say);
    if (!quiet && navigator.vibrate) navigator.vibrate(60);
    return;
  }
  PA.unseen++;
  try { await navigator.setAppBadge?.(PA.unseen); } catch { /* not an installed app */ }
  if (PA.sw && "Notification" in window && Notification.permission === "granted") {
    try { await PA.sw.showNotification(say, { body: "UnderCover", tag: `uc-${j.id}`, silent: quiet, icon: "/static/icon-192.png" }); }
    catch { /* the browser said no: the badge and the banner next time are enough */ }
  }
});
document.addEventListener("visibilitychange", () => {
  if (document.hidden || !PA.unseen) return;
  PA.unseen = 0;
  try { navigator.clearAppBadge?.(); } catch { /* not an installed app */ }
});

// app.js hands every /api/status here (s.client)
async function phoneApp(s) {
  if (s.client !== "phone" || !paSecure() || !("serviceWorker" in navigator) || PA.refused) return;
  if (!PA.sw) {
    try { await navigator.serviceWorker.register("/sw.js"); PA.sw = await navigator.serviceWorker.ready; }
    catch { PA.refused = true; return; }   // (a private tab, storage blocked: once is enough to ask)
    paCards();
  }
}

function paCards() {
  const ask = "Notification" in window && Notification.permission === "default" && (!paIOS() || paStandalone());
  $("#ph-alerts").hidden = !ask || !!store.get("uc-ph-alerts-no", false);
  $("#ph-install").hidden = paStandalone() || !!store.get("uc-ph-install-done", false);
  $("#ph-install-ios").hidden = !paIOS();
  $("#ph-install-android").hidden = paIOS();
}
$("#ph-alerts-yes").onclick = async () => {   // asked only after a tap: iOS insists, and it's only fair
  try { await Notification.requestPermission(); } catch { /* an old browser */ }
  $("#ph-alerts").hidden = true;
};
$("#ph-alerts-no").onclick = () => { store.set("uc-ph-alerts-no", true); $("#ph-alerts").hidden = true; };
$("#ph-install-ok").onclick = () => { store.set("uc-ph-install-done", true); $("#ph-install").hidden = true; };
