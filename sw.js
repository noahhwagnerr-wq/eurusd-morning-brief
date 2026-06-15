const CACHE = 'eurusd-v__BUILDTIME__';
const CACHE = 'eurusd-v20260615';
const ASSETS = ['./', './index.html', './manifest.json', './icon-192.svg', './icon-512.svg'];

// Install: pre-cache assets
self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(ASSETS).catch(() => {})));
  self.skipWaiting();
});

// Activate: delete ALL old caches
self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

// Fetch: Network-first for HTML/JSON, cache-first for static assets
self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  const isHtmlOrJson = url.pathname.endsWith('.html') || url.pathname.endsWith('.json') || url.pathname === '/' || url.pathname.endsWith('/');

  if (isHtmlOrJson) {
    // Network-first: always try network, fall back to cache
    e.respondWith(
      fetch(e.request)
        .then(res => {
          const clone = res.clone();
          caches.open(CACHE).then(c => c.put(e.request, clone));
          return res;
        })
        .catch(() => caches.match(e.request))
    );
  } else {
    // Cache-first for fonts, icons, scripts
    e.respondWith(
      caches.match(e.request).then(r => r || fetch(e.request))
    );
  }
});
