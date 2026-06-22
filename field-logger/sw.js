/* Service worker - offline app shell. Cache-first so the logger works with no signal at the venue.
 * Assets are added individually (allSettled) so a missing startlist.json never breaks the install. */
const CACHE = "eqfl-v18";
const ASSETS = ["./", "index.html", "app.js", "qrcode.js", "manifest.webmanifest",
  "icon-192.png", "icon-512.png", "startlist.json", "startlist.sample.json", "cheatsheet.html"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => Promise.allSettled(ASSETS.map((a) => c.add(a)))));
  self.skipWaiting();
});
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k)))));
  self.clients.claim();
});
self.addEventListener("fetch", (e) => {
  if (e.request.method !== "GET") return;
  e.respondWith(
    caches.match(e.request).then((hit) =>
      hit || fetch(e.request).then((resp) => {
        const cp = resp.clone();
        caches.open(CACHE).then((c) => c.put(e.request, cp)).catch(() => {});
        return resp;
      }).catch(() => caches.match("index.html"))
    )
  );
});
