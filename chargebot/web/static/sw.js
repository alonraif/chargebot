/* Service worker: makes the dashboard installable and lets the app shell open without a connection.
   Pages and assets are fetched fresh when online (network first); live data (/api) is never cached. */
const CACHE = "juice-v9";
const SHELL = [
  "/", "/stats", "/guide",
  "/static/app.css?v=9", "/static/app.js?v=9", "/static/dashboard.js?v=9", "/static/stats.js?v=9",
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

const NETWORK_WAIT_MS = 3000;

const OFFLINE_PAGE = `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Juice · Offline</title>
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0F0E17;color:#FFFFFE;
font:16px/1.5 system-ui,sans-serif;text-align:center;padding:24px}b{font-size:22px}p{color:#A7A5C6}
i{display:inline-block;width:56px;height:56px;border-radius:16px;background:#C6FF3D;color:#16142B;
font:800 32px/56px system-ui;font-style:normal;margin-bottom:12px}</style></head><body><div><i>⚡</i><br>
<b>You're offline</b><p>Juice will reconnect as soon as you're back online.</p></div>
<script>addEventListener("online",()=>location.reload());setInterval(()=>navigator.onLine&&location.reload(),5000)</script>
</body></html>`;

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/* A phone waking up often has no network for a moment: try twice before giving up. */
async function fetchWithRetry(request) {
  try {
    return await fetch(request);
  } catch (e) {
    await sleep(700);
    return fetch(request);
  }
}

async function remember(key, response) {
  if (response && response.ok) {
    const cache = await caches.open(CACHE);
    await cache.put(key, response.clone());
  }
  return response;
}

async function page(request, key) {
  const network = fetchWithRetry(request).then((response) => remember(key, response));
  const cached = await caches.match(key);
  if (!cached) {
    try {
      return await network;
    } catch (e) {
      return new Response(OFFLINE_PAGE, { headers: { "Content-Type": "text/html; charset=utf-8" } });
    }
  }
  // Fresh page if the network answers quickly, otherwise the saved copy; never the browser's error page.
  const timeout = sleep(NETWORK_WAIT_MS).then(() => cached);
  return Promise.race([network.catch(() => cached), timeout]);
}

async function asset(request) {
  try {
    return await remember(request, await fetchWithRetry(request));
  } catch (e) {
    return (await caches.match(request)) || Response.error();
  }
}

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.origin !== location.origin) return;
  if (url.pathname.startsWith("/api/") || url.pathname === "/healthz" || url.pathname.startsWith("/kiosk")) return;
  // Pages are saved without their query string, so a sign-in link never gets stored.
  event.respondWith(event.request.mode === "navigate" ? page(event.request, url.pathname) : asset(event.request));
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
