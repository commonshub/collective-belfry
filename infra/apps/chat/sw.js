// Offline app shell: the page and its assets are served from cache first and
// refreshed in the background. Relay traffic (WebSockets) never goes through here.
const CACHE = 'osv-shell-v1'

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(['./', './index.html', './manifest.webmanifest'])))
  self.skipWaiting()
})

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
  )
  self.clients.claim()
})

self.addEventListener('fetch', (e) => {
  const req = e.request
  const url = new URL(req.url)
  if (req.method !== 'GET' || url.origin !== self.location.origin) return
  const isPage = req.mode === 'navigate'
  e.respondWith(
    caches.open(CACHE).then(async (cache) => {
      const cached = await cache.match(isPage ? './index.html' : req)
      const fresh = fetch(req)
        .then((res) => {
          if (res.ok) cache.put(isPage ? './index.html' : req, res.clone())
          return res
        })
        .catch(() => cached)
      return cached || fresh
    })
  )
})
