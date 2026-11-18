// Offline app shell (Xavier, 2026-11-18: a blank page after a new build until a refresh).
// - The page (navigations, index.html): network first, never a cached copy while online; the
//   cached copy only when the network fails (offline).
// - Built files (/assets/*, their names carry a hash): cache first, but only real files are kept
//   (never an HTML answer for a script); a new build's index drops the previous build's files.
// - Anything else of this origin (manifest, icons, boot.js): network first, cache when offline.
// Relay traffic (WebSockets) and other origins never go through here.
const CACHE = 'belfry-shell-v2'
const PAGE = './index.html'

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(['./', PAGE, './manifest.webmanifest', './boot.js']).catch(() => undefined)))
  self.skipWaiting()
})

self.addEventListener('activate', (e) => {
  e.waitUntil((async () => {
    for (const k of await caches.keys()) if (k !== CACHE) await caches.delete(k)
    await self.clients.claim()
  })())
})

const isHtml = (res) => (res.headers.get('content-type') || '').includes('text/html')

/** The built files a page names (its entry script, styles, preloads). */
const assetsIn = (html) => new Set([...html.matchAll(/\/assets\/[^"'\s)]+/g)].map((m) => m[0]))

async function page(req) {
  const cache = await caches.open(CACHE)
  try {
    const res = await fetch(req, { cache: 'no-store' })
    if (res.ok && isHtml(res)) {
      const html = await res.clone().text()
      const old = await cache.match(PAGE)
      const oldHtml = old ? await old.text() : ''
      await cache.put(PAGE, res.clone())
      // a new build: the previous build's files go (the server keeps them a while for open tabs)
      if (oldHtml && oldHtml !== html) {
        const keep = assetsIn(html)
        for (const r of await cache.keys()) {
          const p = new URL(r.url).pathname
          if (p.includes('/assets/') && ![...keep].some((k) => p.endsWith(k))) await cache.delete(r)
        }
      }
    }
    return res
  } catch {
    return (await cache.match(PAGE)) || Response.error()
  }
}

async function asset(req) {
  const cache = await caches.open(CACHE)
  const hit = await cache.match(req)
  if (hit) return hit
  const res = await fetch(req)
  if (res.ok && !isHtml(res)) await cache.put(req, res.clone())
  return res
}

async function other(req) {
  const cache = await caches.open(CACHE)
  try {
    const res = await fetch(req, { cache: 'no-cache' })
    if (res.ok && !isHtml(res)) await cache.put(req, res.clone())
    return res
  } catch {
    return (await cache.match(req)) || Response.error()
  }
}

self.addEventListener('fetch', (e) => {
  const req = e.request
  const url = new URL(req.url)
  if (req.method !== 'GET' || url.origin !== self.location.origin) return
  if (req.mode === 'navigate' || url.pathname.endsWith('/index.html')) return e.respondWith(page(req))
  if (url.pathname.includes('/assets/')) return e.respondWith(asset(req))
  e.respondWith(other(req))
})
