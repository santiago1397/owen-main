/* OWEN service worker — deliberately minimal.
 *
 * Purpose is installability + surviving a flaky signal, NOT offline use. Two rules keep it
 * from ever serving you a stale dashboard or leaking data:
 *   1. /api and /webhooks are NEVER cached (auth'd, always-changing, sometimes secret).
 *   2. Navigations are network-first, so a deploy is picked up on the next load; only
 *      Vite's content-hashed build assets are cache-first (a new build = a new filename,
 *      so they can never go stale).
 */
const CACHE = "owen-shell-v1";

self.addEventListener("install", () => self.skipWaiting());

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  const url = new URL(req.url);

  // Only same-origin GETs are ours to handle. Everything else falls through to the network.
  if (req.method !== "GET" || url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api") || url.pathname.startsWith("/webhooks")) return;

  if (req.mode === "navigate") {
    // Network-first: always prefer the freshly deployed index.html.
    e.respondWith(
      fetch(req)
        .then((res) => {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put("/index.html", copy));
          return res;
        })
        .catch(() => caches.match("/index.html").then((r) => r || Response.error()))
    );
    return;
  }

  // Content-hashed assets: cache-first is safe and makes cold starts fast.
  e.respondWith(
    caches.match(req).then(
      (hit) =>
        hit ||
        fetch(req).then((res) => {
          if (res.ok && res.type === "basic") {
            const copy = res.clone();
            caches.open(CACHE).then((c) => c.put(req, copy));
          }
          return res;
        })
    )
  );
});
