// Belfry village apps: the directory (people.<domain>), the marketplace (market.<domain>) and the
// photos (photos.<domain>).
// Both are browser apps that read and write the community relay with the participant's signer;
// this process serves them, one port each, and tells signed-in participants which keys hold an
// active pass (the relay keeps the events of expired passes, the apps hide them).
//
// | Variable | Meaning |
// | --- | --- |
// | `PEOPLE_PORT`, `MARKET_PORT`, `PHOTOS_PORT` | listen ports (8080, 8081, 8082) |
// | `PEOPLE_PUBLIC_URL`, `MARKET_PUBLIC_URL`, `PHOTOS_PUBLIC_URL` | the apps as browsers reach them; NIP-98 tokens name `<URL><path>` |
// | `ONBOARDING_URL`, `PASS_INFO_TOKEN` | the active passes: `GET <ONBOARDING_URL>/admin/active-passes` |
// | `STATIC_ROOT` | the built web apps (`web`, with `people/dist`, `market/dist` and `photos/dist`) |
import { createServer } from "node:http"
import { readFile, stat } from "node:fs/promises"
import { extname, join, normalize } from "node:path"
import { fileURLToPath } from "node:url"
import { AuthError, verifyNip98 } from "./nip98.js"

const TYPES = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".json": "application/json",
  ".webmanifest": "application/manifest+json",
  ".woff2": "font/woff2",
}
const PASSES_TTL_MS = 30_000

export class HttpError extends Error {
  constructor(status, message) {
    super(message)
    this.status = status
  }
}

/** Active passes from the onboarding service: a Set of pubkeys (hex), cached for 30 s. */
export function onboardingPasses({ baseUrl, token, fetchImpl = fetch }) {
  let cache = null
  let cachedAt = 0
  return {
    async activePubkeys() {
      if (cache && Date.now() - cachedAt < PASSES_TTL_MS) return cache
      const res = await fetchImpl(`${baseUrl.replace(/\/$/, "")}/admin/active-passes`, {
        headers: { Authorization: `Bearer ${token}` },
        signal: AbortSignal.timeout(5000),
      })
      if (!res.ok) throw new Error(`onboarding answered ${res.status}`)
      const { passes } = await res.json()
      cache = new Set(passes.map(p => p.pubkey).filter(Boolean))
      cachedAt = Date.now()
      return cache
    },
  }
}

function send(res, status, body) {
  res.writeHead(status, { "Content-Type": "application/json", "Cache-Control": "no-store" })
  res.end(JSON.stringify(body))
}

export function createApp({ passes, publicUrl, staticDir }) {
  const base = publicUrl.replace(/\/$/, "")

  async function api(req, res, path) {
    if (path === "/api/health") return send(res, 200, { ok: true })
    if (path === "/api/participants" && req.method === "GET") {
      // Only participants learn who the others are.
      const pubkey = verifyNip98(req.headers.authorization, { url: base + path, method: "GET" })
      const active = await passes.activePubkeys()
      if (!active.has(pubkey)) throw new HttpError(403, "no active pass for this key")
      return send(res, 200, { pubkeys: [...active].sort() })
    }
    throw new HttpError(404, "not found")
  }

  async function serveStatic(res, path) {
    if (!staticDir) throw new HttpError(404, "not found")
    const rel = normalize(decodeURIComponent(path)).replace(/^(\.\.[/\\])+/, "")
    let file = join(staticDir, rel)
    if (!file.startsWith(staticDir)) throw new HttpError(404, "not found")
    let info = await stat(file).catch(() => null)
    if (!info || info.isDirectory()) {
      // hash routes; any other path (deep links, /oauth/callback) shows the app
      file = join(staticDir, "index.html")
      info = await stat(file).catch(() => null)
      if (!info) throw new HttpError(404, "not found")
    }
    res.writeHead(200, {
      "Content-Type": TYPES[extname(file)] || "application/octet-stream",
      "Cache-Control": file.includes("/assets/") ? "public, max-age=31536000, immutable" : "no-cache",
    })
    res.end(await readFile(file))
  }

  return async function handler(req, res) {
    const url = new URL(req.url, "http://village.local")
    try {
      if (url.pathname.startsWith("/api/")) return await api(req, res, url.pathname)
      if (req.method !== "GET" && req.method !== "HEAD") throw new HttpError(405, "method not allowed")
      return await serveStatic(res, url.pathname)
    } catch (err) {
      if (err instanceof AuthError) return send(res, 401, { error: err.message })
      if (err instanceof HttpError) return send(res, err.status, { error: err.message })
      console.error(err)
      return send(res, 500, { error: "internal error" })
    }
  }
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const env = process.env
  for (const name of ["PEOPLE_PUBLIC_URL", "MARKET_PUBLIC_URL", "PHOTOS_PUBLIC_URL", "ONBOARDING_URL", "PASS_INFO_TOKEN"]) {
    if (!env[name]) throw new Error(`${name} is not set`)
  }
  const passes = onboardingPasses({ baseUrl: env.ONBOARDING_URL, token: env.PASS_INFO_TOKEN })
  const root = env.STATIC_ROOT || fileURLToPath(new URL("../web", import.meta.url))
  for (const [app, port, publicUrl] of [
    ["people", Number(env.PEOPLE_PORT || 8080), env.PEOPLE_PUBLIC_URL],
    ["market", Number(env.MARKET_PORT || 8081), env.MARKET_PUBLIC_URL],
    ["photos", Number(env.PHOTOS_PORT || 8082), env.PHOTOS_PUBLIC_URL],
  ]) {
    const handler = createApp({ passes, publicUrl, staticDir: join(root, app, "dist") })
    createServer(handler).listen(port, () => console.log(`${app} on :${port}, public ${publicUrl}`))
  }
}
