// The programme app's service worker: the page opens without a network (the venue's WiFi), and
// then shows the programme the phone kept (main.js, localStorage). The built assets are cached
// once (their names change with every build); the page, the app bar and the brand files (fonts,
// colours, from pass.<domain>/brand) come from the network and fall back to the cached copy.
// Nothing else is touched: the API, the sign-in callback and the relay go to the network as usual.
const CACHE = "programme-shell-v1"

async function precache() {
  const cache = await caches.open(CACHE)
  const res = await fetch("/", { cache: "no-cache" })
  if (!res.ok) return
  const html = await res.clone().text()
  await cache.put("/", res)
  const assets = [...new Set(html.match(/\/assets\/[^"']+/g) || [])]
  await cache.addAll([...assets, "/osv-bar.js", "/manifest.webmanifest"]).catch(() => {})
}

self.addEventListener("install", event => {
  event.waitUntil(precache().catch(() => {}))
  self.skipWaiting()
})

self.addEventListener("activate", event => {
  event.waitUntil(
    (async () => {
      for (const key of await caches.keys()) if (key !== CACHE) await caches.delete(key)
      await self.clients.claim()
    })(),
  )
})

self.addEventListener("fetch", event => {
  const req = event.request
  if (req.method !== "GET") return
  const url = new URL(req.url)
  const own = url.origin === self.location.origin
  if (own && (url.pathname.startsWith("/api/") || url.pathname.startsWith("/oauth/"))) return
  if (!own && !url.pathname.includes("/brand/")) return
  event.respondWith(
    (async () => {
      const cache = await caches.open(CACHE)
      if (own && url.pathname.startsWith("/assets/")) {
        const hit = await cache.match(req)
        if (hit) return hit
        const res = await fetch(req)
        if (res.ok) cache.put(req, res.clone())
        return res
      }
      // the page itself: one copy, whatever the path (hash routes)
      const key = own && req.mode === "navigate" ? "/" : req
      try {
        const res = await fetch(req)
        if (res.ok || res.type === "opaque") cache.put(key, res.clone())
        return res
      } catch (err) {
        const hit = await cache.match(key)
        if (hit) return hit
        throw err
      }
    })(),
  )
})
