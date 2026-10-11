/* Service worker: makes the dashboard installable and lets the app shell open without a connection.
   Pages and assets are fetched fresh when online (network first); live data (/api) is never cached. */
const CACHE = "juice-v3";
const SHELL = [
  "/", "/stats",
  "/static/app.css?v=3", "/static/app.js?v=3", "/static/dashboard.js?v=3", "/static/stats.js?v=3",
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
