// Service worker: the chat shell opens offline; store data (/api) is always live.
const CACHE = "emporio-shell-v3";   // v3: drops a shell an error page may have replaced
const FONTS = "emporio-fonts-v1";
const SHELL = [
  "/",
  "/manifest.webmanifest",
  "/static/duda.webp",
  "/static/duda-face.webp",
  "/static/icons/icon-192.png",
  "/static/icons/favicon-32.png",
];
const FONT_HOSTS = ["fonts.googleapis.com", "fonts.gstatic.com"];

self.addEventListener("install", event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(key => key !== CACHE && key !== FONTS).map(key => caches.delete(key))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", event => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== "GET") return;

  // The typeface keeps the offline shell readable: serve the cached copy, refresh it in the background.
  if (FONT_HOSTS.includes(url.hostname)) {
    event.respondWith(caches.open(FONTS).then(cache => cache.match(request).then(hit => {
      const fresh = fetch(request).then(response => {
        if (response.ok || response.type === "opaque") cache.put(request, response.clone());
        return response;
      }).catch(() => hit);
      return hit || fresh;
    })));
    return;
  }

  // Prices, stock and orders must never come from a cache.
  if (url.origin !== location.origin || url.pathname.startsWith("/api/")) return;

  if (request.mode === "navigate") {
    // Network first, so a new version of the page shows up right away. Only a good copy of the
    // chat page itself replaces the offline shell: an error page or /docs must never take its place.
    event.respondWith(
      fetch(request)
        .then(response => {
          if (response.ok && !response.redirected && url.pathname === "/") {
            const copy = response.clone();
            caches.open(CACHE).then(cache => cache.put("/", copy));
          }
          return response;
        })
        .catch(() => caches.match(request, { ignoreSearch: true }).then(hit => hit || caches.match("/"))),
    );
    return;
  }

  event.respondWith(
    caches.match(request).then(hit => hit || fetch(request).then(response => {
      if (response.ok) {
        const copy = response.clone();   // cloned now: the page may read the body before the cache opens
        caches.open(CACHE).then(cache => cache.put(request, copy));
      }
      return response;
    })),
  );
});
