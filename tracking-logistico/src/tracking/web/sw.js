/* Service worker: deja la app del conductor disponible sin señal.
 * - Páginas: red primero, caché si no hay señal.
 * - Archivos estáticos: red primero (así una versión nueva se ve al instante) y caché sin señal.
 * - Lector de facturas (/static/vendor/, ~28 MB): no pasa por aquí; lo guarda la caché HTTP del
 *   navegador, que respeta las cabeceras actuales del servidor (seguridad y aislamiento para hilos).
 * - API: nunca se cachea aquí (los datos sin señal los maneja IndexedDB en outbox.js).
 */
const VERSION = "tl-v1.28.1";
const SHELL = [
  "/", "/conductor", "/tracking",
  "/static/app.css", "/static/common.js", "/static/biometria.js", "/static/login.js", "/static/outbox.js", "/static/consumo.js", "/static/conductor.js",
  "/static/tracking.js", "/static/xlsx-lite.js", "/static/icons/icon.svg", "/static/icons/logo.png", "/static/icons/icon-192.png", "/static/icons/icon-512.png",
  "/manifest.webmanifest",
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(VERSION).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== VERSION).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/") || url.pathname === "/health") return;

  if (req.mode === "navigate") {
    event.respondWith(
      fetch(req)
        .then((res) => {
          const copy = res.clone();
          caches.open(VERSION).then((c) => c.put(url.pathname, copy));
          return res;
        })
        .catch(() => caches.match(url.pathname).then((r) => r || caches.match("/conductor")))
    );
    return;
  }

  if (url.pathname.startsWith("/static/vendor/")) return;

  if (url.pathname.startsWith("/static/") || url.pathname === "/manifest.webmanifest") {
    event.respondWith(
      fetch(req)
        .then((res) => {
          if (res.ok) { const copy = res.clone(); caches.open(VERSION).then((c) => c.put(req, copy)); }
          return res;
        })
        .catch(() => caches.match(req).then((r) => r || Response.error()))
    );
  }
});
