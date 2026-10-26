// Belfry village apps: the directory (people.<domain>), the marketplace (market.<domain>), the
// photos (photos.<domain>) and the programme (programme.<domain>).
// Both are browser apps that read and write the community relay with the participant's signer;
// this process serves them, one port each, and tells signed-in participants which keys hold an
// active pass (the relay keeps the events of expired passes, the apps hide them).
//
// | Variable | Meaning |
// | --- | --- |
// | `PEOPLE_PORT`, `MARKET_PORT`, `PHOTOS_PORT`, `PROGRAMME_PORT` | listen ports (8080, 8081, 8082, 8083) |
// | `PEOPLE_PUBLIC_URL`, `MARKET_PUBLIC_URL`, `PHOTOS_PUBLIC_URL`, `PROGRAMME_PUBLIC_URL` | the apps as browsers reach them; NIP-98 tokens name `<URL><path>` |
// | `ONBOARDING_URL`, `PASS_INFO_TOKEN` (or the file /run/secrets/pass_info_token) | the active passes (`GET /admin/active-passes`), reports (`POST /admin/reports`), hidden content (`GET /admin/moderation/hidden`) and, for the programme's Shifts tab, volunteers' shifts (`/admin/shifts`) |
// | `PROGRAMME_CORS_ORIGINS` | origins (the calendar) whose pages may read the programme port's `/api/participants` and `/api/hidden` |
// | `STATIC_ROOT` | the built web apps (`web`, with `people/dist`, `market/dist`, `photos/dist` and `programme/dist`) |
import { createServer } from "node:http"
import { readFileSync } from "node:fs"
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
const HIDDEN_TTL_MS = 30_000
const MAX_REPORT_BYTES = 8192

export class HttpError extends Error {
  constructor(status, message) {
    super(message)
    this.status = status
  }
}

/** Active passes from the onboarding service: a Set of pubkeys (hex), and the partner behind
 * each (for the Shifts tab), cached for 30 s. */
export function onboardingPasses({ baseUrl, token, fetchImpl = fetch }) {
  let cache = null
  let partners = new Map()
  let cachedAt = 0
  async function load() {
    if (cache && Date.now() - cachedAt < PASSES_TTL_MS) return
    const res = await fetchImpl(`${baseUrl.replace(/\/$/, "")}/admin/active-passes`, {
      headers: { Authorization: `Bearer ${token}` },
      signal: AbortSignal.timeout(5000),
    })
    if (!res.ok) throw new Error(`onboarding answered ${res.status}`)
    const { passes } = await res.json()
    cache = new Set(passes.map(p => p.pubkey).filter(Boolean))
    partners = new Map(passes.filter(p => p.pubkey && p.partner_id).map(p => [p.pubkey, p.partner_id]))
    cachedAt = Date.now()
  }
  return {
    async activePubkeys() {
      await load()
      return cache
    },
    async partnerOf(pubkey) {
      await load()
      return partners.get(pubkey) || null
    },
  }
}

/** Volunteers' shifts, kept in Odoo and served by the onboarding service for one partner:
 * `board(partner)` is `{role, count, min_shifts, change_hours, shifts}` (role null: no tab),
 * `act(partner, id, "signup"|"leave")` is `{result: "ok"|"refused", ...}`. */
export function onboardingShifts({ baseUrl, token, fetchImpl = fetch }) {
  const root = baseUrl.replace(/\/$/, "")
  const headers = { Authorization: `Bearer ${token}` }
  const call = async (path, method = "GET") => {
    const res = await fetchImpl(`${root}${path}`, { method, headers, signal: AbortSignal.timeout(10000) })
    const answer = await res.json().catch(() => ({}))
    if (!res.ok) throw new HttpError(res.status === 404 ? 404 : 502, answer.detail || `onboarding answered ${res.status}`)
    return answer
  }
  return {
    board: partner => call(`/admin/shifts?partner=${partner}`),
    act: (partner, id, action) => call(`/admin/shifts/${id}/${action}?partner=${partner}`, "POST"),
  }
}

/** Reports and hidden content, through the onboarding service (which stores them in Odoo and
 * mails the stewards). The hidden list is cached for 30 s. */
export function onboardingModeration({ baseUrl, token, fetchImpl = fetch }) {
  const root = baseUrl.replace(/\/$/, "")
  const headers = { Authorization: `Bearer ${token}` }
  let cache = null
  let cachedAt = 0
  return {
    async hidden() {
      if (cache && Date.now() - cachedAt < HIDDEN_TTL_MS) return cache
      const res = await fetchImpl(`${root}/admin/moderation/hidden`, { headers, signal: AbortSignal.timeout(5000) })
      if (!res.ok) throw new Error(`onboarding answered ${res.status}`)
      cache = (await res.json()).hidden || []
      cachedAt = Date.now()
      return cache
    },
    async report(body) {
      const res = await fetchImpl(`${root}/admin/reports`, {
        method: "POST",
        headers: { ...headers, "Content-Type": "application/json" },
        body: JSON.stringify(body),
        signal: AbortSignal.timeout(10000),
      })
      const answer = await res.json().catch(() => ({}))
      if (res.status === 400 || res.status === 403) throw new HttpError(res.status, answer.detail || "report refused")
      if (!res.ok) throw new Error(`onboarding answered ${res.status}`)
      return answer
    },
  }
}

async function readBody(req, limit) {
  const chunks = []
  let size = 0
  for await (const chunk of req) {
    size += chunk.length
    if (size > limit) throw new HttpError(413, "too large")
    chunks.push(chunk)
  }
  return Buffer.concat(chunks)
}

function send(res, status, body, headers = {}) {
  res.writeHead(status, { "Content-Type": "application/json", "Cache-Control": "no-store", ...headers })
  res.end(JSON.stringify(body))
}

// Read by other apps' pages (CORS for `corsOrigins`): the calendar shows only active participants'
// sessions and leaves out hidden ones.
const SHARED_READS = new Set(["/api/participants", "/api/hidden"])

export function createApp({ passes, moderation, shifts, app, publicUrl, staticDir, corsOrigins = [] }) {
  const base = publicUrl.replace(/\/$/, "")
  const cors = req => {
    const origin = req.headers.origin
    return origin && corsOrigins.includes(origin) ? { "Access-Control-Allow-Origin": origin, Vary: "Origin" } : {}
  }

  async function api(req, res, path) {
    if (path === "/api/health") return send(res, 200, { ok: true })
    if (path === "/api/participants" && req.method === "GET") {
      // Only participants learn who the others are.
      const pubkey = verifyNip98(req.headers.authorization, { url: base + path, method: "GET" })
      const active = await passes.activePubkeys()
      if (!active.has(pubkey)) throw new HttpError(403, "no active pass for this key")
      return send(res, 200, { pubkeys: [...active].sort() })
    }
    if (shifts && path === "/api/shifts" && req.method === "GET") {
      // The volunteers' shifts, for active volunteers and stewards only (the role decides);
      // anyone else gets no role and the app shows no Shifts tab.
      const pubkey = verifyNip98(req.headers.authorization, { url: base + path, method: "GET" })
      const partner = await passes.partnerOf(pubkey)
      if (!partner) return send(res, 200, { role: null, shifts: [] })
      return send(res, 200, await shifts.board(partner))
    }
    const shift = shifts && req.method === "POST" && path.match(/^\/api\/shifts\/(\d+)\/(signup|leave)$/)
    if (shift) {
      const pubkey = verifyNip98(req.headers.authorization, { url: base + path, method: "POST" })
      const partner = await passes.partnerOf(pubkey)
      if (!partner) throw new HttpError(403, "no active pass for this key")
      return send(res, 200, await shifts.act(partner, Number(shift[1]), shift[2]))
    }
    if (path === "/api/hidden" && req.method === "GET") {
      // What stewards hid after a report: event ids, offer coordinates, pubkeys of directory
      // entries. Only references, nothing to protect; the apps filter on it.
      return send(res, 200, { hidden: moderation ? await moderation.hidden() : [] })
    }
    if (path === "/api/report" && req.method === "POST") {
      if (!moderation) throw new HttpError(503, "reports are not configured")
      const raw = await readBody(req, MAX_REPORT_BYTES)
      const pubkey = verifyNip98(req.headers.authorization, { url: base + path, method: "POST", body: raw })
      if (!(await passes.activePubkeys()).has(pubkey)) throw new HttpError(403, "no active pass for this key")
      let report
      try {
        report = JSON.parse(raw.toString("utf8"))
      } catch {
        throw new HttpError(400, "body is not JSON")
      }
      const pick = k => (typeof report?.[k] === "string" ? report[k] : "")
      const answer = await moderation.report({
        reporter_pubkey: pubkey, // the signer, never what the body claims
        app,
        target_kind: pick("target_kind"),
        target_ref: pick("target_ref"),
        target_pubkey: pick("target_pubkey"),
        target_url: pick("target_url"),
        target_excerpt: pick("target_excerpt"),
        reason: pick("reason"),
        note: pick("note"),
      })
      return send(res, 200, { ok: true, id: answer.id })
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
      if (SHARED_READS.has(url.pathname)) {
        const headers = cors(req)
        if (req.method === "OPTIONS") {
          const allow = headers["Access-Control-Allow-Origin"]
            ? { ...headers, "Access-Control-Allow-Methods": "GET", "Access-Control-Allow-Headers": "Authorization", "Access-Control-Max-Age": "600" }
            : {}
          res.writeHead(204, allow)
          return res.end()
        }
        const end = res.writeHead.bind(res)
        res.writeHead = (status, h = {}) => end(status, { ...h, ...headers })
      }
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

// Secrets come as Compose secrets (/run/secrets/<name in lower case>); a variable of the same name wins.
function withSecrets(env, names) {
  const out = { ...env }
  for (const name of names) {
    if (out[name]) continue
    try {
      out[name] = readFileSync(join(env.SECRETS_DIR || "/run/secrets", name.toLowerCase()), "utf8").trim()
    } catch {}
  }
  return out
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const env = withSecrets(process.env, ["PASS_INFO_TOKEN"])
  for (const name of ["PEOPLE_PUBLIC_URL", "MARKET_PUBLIC_URL", "PHOTOS_PUBLIC_URL", "PROGRAMME_PUBLIC_URL", "ONBOARDING_URL", "PASS_INFO_TOKEN"]) {
    if (!env[name]) throw new Error(`${name} is not set`)
  }
  const passes = onboardingPasses({ baseUrl: env.ONBOARDING_URL, token: env.PASS_INFO_TOKEN })
  const moderation = onboardingModeration({ baseUrl: env.ONBOARDING_URL, token: env.PASS_INFO_TOKEN })
  const shifts = onboardingShifts({ baseUrl: env.ONBOARDING_URL, token: env.PASS_INFO_TOKEN })
  const root = env.STATIC_ROOT || fileURLToPath(new URL("../web", import.meta.url))
  for (const [app, port, publicUrl] of [
    ["people", Number(env.PEOPLE_PORT || 8080), env.PEOPLE_PUBLIC_URL],
    ["market", Number(env.MARKET_PORT || 8081), env.MARKET_PUBLIC_URL],
    ["photos", Number(env.PHOTOS_PORT || 8082), env.PHOTOS_PUBLIC_URL],
    ["programme", Number(env.PROGRAMME_PORT || 8083), env.PROGRAMME_PUBLIC_URL],
  ]) {
    const corsOrigins = app === "programme" ? (env.PROGRAMME_CORS_ORIGINS || "").split(/[\s,]+/).map(o => o.replace(/\/$/, "")).filter(Boolean) : []
    const handler = createApp({
      passes,
      moderation,
      shifts: app === "programme" ? shifts : null,
      app,
      publicUrl,
      staticDir: join(root, app, "dist"),
      corsOrigins,
    })
    createServer(handler).listen(port, () => console.log(`${app} on :${port}, public ${publicUrl}`))
  }
}
