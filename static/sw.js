/* Pocket Dash service worker (mobile-dashboard-handoff.md, PR 2).
 *
 * Job for now: make the /mobile shell open instantly and survive a
 * dead signal. Live data (/api/*) is never cached -- it goes straight
 * to the network and the page shows its own "stale" flag on failure.
 *
 * Push-event handling is added in PR 3, alongside the subscribe flow.
 *
 * Served from the site root (see app.py's /sw.js route) so its scope
 * can cover /mobile; a worker under /static/ could only control
 * /static/*. Bump CACHE when the shell markup changes.
 */
const CACHE = "pocket-dash-shell-v1";
const SHELL = [
  "/mobile",
  "/static/manifest.json",
  "/static/icon-192.png",
  "/static/icon-512.png",
  "/static/icon-maskable-512.png",
  "/static/icon-180.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE)
      .then((cache) => cache.addAll(SHELL))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))
      ))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;

  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  // Live data: bypass the worker entirely.
  if (url.pathname.startsWith("/api/")) return;

  // The mobile shell: network-first so updates show, cache as fallback.
  if (req.mode === "navigate") {
    if (url.pathname !== "/mobile") return;  // only /mobile is offline-capable
    event.respondWith(
      fetch(req)
        .then((res) => {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put("/mobile", copy));
          return res;
        })
        .catch(() => caches.match("/mobile"))
    );
    return;
  }

  // Static assets (icons, manifest): cache-first.
  if (url.pathname.startsWith("/static/")) {
    event.respondWith(
      caches.match(req).then((hit) =>
        hit || fetch(req).then((res) => {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(req, copy));
          return res;
        })
      )
    );
  }
});
