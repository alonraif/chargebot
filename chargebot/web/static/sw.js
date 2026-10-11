/* Service worker: makes the dashboard installable and lets the app shell open without a connection.
   Pages and assets are fetched fresh when online (network first); live data (/api) is never cached. */
const CACHE = "juice-v6";
const SHELL = [
  "/", "/stats",
  "/static/app.css?v=6", "/static/app.js?v=6", "/static/dashboard.js?v=6", "/static/stats.js?v=6",
  "/static/icon-192.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.origin !== location.origin) return;
  if (url.pathname.startsWith("/api/") || url.pathname === "/healthz" || url.pathname.startsWith("/kiosk")) return;
  event.respondWith(
    fetch(event.request)
      .then((response) => {
        if (response.ok) {
          const copy = response.clone();
          // Cache pages without their query string, so a sign-in link doesn't get stored.
          const key = event.request.mode === "navigate" ? url.pathname : event.request;
          caches.open(CACHE).then((cache) => cache.put(key, copy));
        }
        return response;
      })
      .catch(() => caches.match(event.request.mode === "navigate" ? url.pathname : event.request)),
  );
});

/* Push notifications. The payload carries the text, optional action buttons, and a short-lived
   sign-in token so a button (e.g. "I'm plugged in") can act without opening the app. */
self.addEventListener("push", (event) => {
  let msg = {};
  try { msg = event.data ? event.data.json() : {}; } catch (e) { msg = { title: "ChargeBot", body: event.data && event.data.text() }; }
  event.waitUntil(self.registration.showNotification(msg.title || "ChargeBot", {
    body: msg.body || "",
    tag: msg.tag || "cb",
    renotify: true,
    requireInteraction: !!msg.requireInteraction,
    icon: "/static/icon-192.png",
    badge: "/static/icon-192.png",
    actions: (msg.actions || []).slice(0, 2),
    data: { url: msg.url || "/", token: msg.token || null },
  }));
});

const DONE_TEXT = {
  confirmed: "⚡ Charging. Enjoy the juice!", skipped: "⏭ Turn skipped.", done: "🏁 Session ended. Thanks!",
  unplugged: "👍 Thanks for moving your car!", extended: "➕ Extended.", reclaimed: "🔁 It's yours again!",
};

async function openApp(url) {
  const all = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
  for (const client of all) {
    if (new URL(client.url).origin === location.origin) {
      await client.focus();
      return;
    }
  }
  await self.clients.openWindow(url || "/");
}

self.addEventListener("notificationclick", (event) => {
  const note = event.notification;
  const { url, token } = note.data || {};
  note.close();
  if (!event.action || !token) {
    event.waitUntil(openApp(url));
    return;
  }
  event.waitUntil((async () => {
    try {
      const res = await fetch("/api/action", {
        method: "POST", headers: { "Content-Type": "application/json", "X-CB-Token": token },
        body: JSON.stringify({ action: event.action }),
      });
      const data = await res.json();
      if (data.ok) {
        await self.registration.showNotification(DONE_TEXT[data.code] || "Done.", { tag: note.tag, icon: "/static/icon-192.png" });
        return;
      }
    } catch (e) { /* fall through and open the app */ }
    await openApp(url);
  })());
});
