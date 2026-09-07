/* Pocket Dash service worker (mobile-dashboard-handoff.md, PR 2).
 *
 * Job for now: make the /mobile shell open instantly and survive a
 * dead signal. Live data (/api/*) is never cached -- it goes straight
 * to the network and the page shows its own "stale" flag on failure.
 *
 * It also handles the `push` event (PR 3): shows the notification and,
 * on tap, focuses an open Pocket Dash window or opens one.
 *
 * Served from the site root (see app.py's /sw.js route) so its scope
 * can cover /mobile; a worker under /static/ could only control
 * /static/*. Bump CACHE when the shell markup changes.
 */
const CACHE = "pocket-dash-shell-v3";
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

self.addEventListener("push", (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (e) { /* non-JSON */ }
  const title = data.title || "Pocket Dash";
  event.waitUntil(
    self.registration.showNotification(title, {
      body: data.body || "",
      icon: "/static/icon-192.png",
      badge: "/static/icon-192.png",
      tag: data.tag || "pocket-dash",
      renotify: !!data.tag,
      data: { url: data.url || "/mobile" },
    })
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = (event.notification.data && event.notification.data.url) || "/mobile";
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((wins) => {
      for (const w of wins) {
        if (w.url.indexOf("/mobile") !== -1) {
          // bring the existing Pocket Dash window up and point it at the
          // deep link (e.g. ?focus=<id>); navigate() may be unavailable.
          if ("navigate" in w) { try { w.navigate(target); } catch (e) {} }
          if ("focus" in w) return w.focus();
        }
      }
      return self.clients.openWindow(target);
    })
  );
});
