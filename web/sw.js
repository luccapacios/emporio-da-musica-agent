// Service worker: the chat shell opens offline; store data (/api) is always live.
const CACHE = "emporio-shell-v1";
const SHELL = [
  "/",
  "/manifest.webmanifest",
  "/static/duda.webp",
  "/static/duda-face.webp",
  "/static/icons/icon-192.png",
  "/static/icons/favicon-32.png",
];

self.addEventListener("install", event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(key => key !== CACHE).map(key => caches.delete(key))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", event => {
  const request = event.request;
  const url = new URL(request.url);
  // Prices, stock and orders must never come from a cache.
  if (request.method !== "GET" || url.origin !== location.origin || url.pathname.startsWith("/api/")) return;

  if (request.mode === "navigate") {
    // Network first, so a new version of the page shows up right away.
    event.respondWith(
      fetch(request)
        .then(response => { caches.open(CACHE).then(cache => cache.put("/", response.clone())); return response; })
        .catch(() => caches.match("/")),
    );
    return;
  }

  event.respondWith(
    caches.match(request).then(hit => hit || fetch(request).then(response => {
      if (response.ok) caches.open(CACHE).then(cache => cache.put(request, response.clone()));
      return response;
    })),
  );
});
