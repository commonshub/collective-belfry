// Belfry vote service: anonymous polls for Open Source Village (Semaphore, one vote per active
// pass). Serves the API under /api and the web app (web/dist) on one port.
//
// | Variable | Meaning |
// | --- | --- |
// | `PORT` | listen port (8080) |
// | `PUBLIC_URL` | the service as browsers reach it; NIP-98 tokens must name `<PUBLIC_URL><path>` |
// | `ONBOARDING_URL`, `PASS_INFO_TOKEN` (or the file /run/secrets/pass_info_token) | the electorate: `GET <ONBOARDING_URL>/admin/active-passes` |
// | `DB_PATH` | SQLite file (`/data/vote.sqlite`) |
// | `TREE_DEPTH` | Semaphore tree depth (10: up to 1024 voters per poll); the image carries its artifacts |
// | `STATIC_DIR` | the built web app (`web/dist`) |
import { createServer } from "node:http"
import { readFileSync } from "node:fs"
import { readFile, stat } from "node:fs/promises"
import { extname, join, normalize } from "node:path"
import { fileURLToPath } from "node:url"
import { openDb } from "./db.js"
import { AuthError, verifyNip98 } from "./nip98.js"
import { onboardingPasses } from "./passes.js"
import { createPolls, PollError } from "./polls.js"

const MAX_BODY = 64 * 1024
const TYPES = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".json": "application/json",
  ".webmanifest": "application/manifest+json",
  ".wasm": "application/wasm",
  ".zkey": "application/octet-stream",
  ".woff2": "font/woff2",
}

function send(res, status, body, headers = {}) {
  const payload = typeof body === "string" || Buffer.isBuffer(body) ? body : JSON.stringify(body)
  res.writeHead(status, {
    "Content-Type": typeof body === "object" && !Buffer.isBuffer(body) ? "application/json" : "text/plain",
    "Cache-Control": "no-store",
    ...headers,
  })
  res.end(payload)
}

async function readBody(req) {
  const chunks = []
  let size = 0
  for await (const chunk of req) {
    size += chunk.length
    if (size > MAX_BODY) throw new PollError(413, "request too large")
    chunks.push(chunk)
  }
  return Buffer.concat(chunks)
}

function parseJson(body) {
  if (!body.length) return {}
  try {
    return JSON.parse(body.toString("utf8"))
  } catch {
    throw new PollError(400, "body is not JSON")
  }
}

export function createApp({ polls, passes, publicUrl, staticDir }) {
  const base = publicUrl.replace(/\/$/, "")

  /** The active pass behind a NIP-98 request, or an error. */
  async function participant(req, path, body) {
    const pubkey = verifyNip98(req.headers.authorization, { url: base + path, method: req.method, body })
    const active = await passes.active()
    const passId = active.get(pubkey)
    if (passId === undefined) throw new PollError(403, "no active pass for this key")
    return { passId, active: new Set(active.values()) }
  }

  async function api(req, res, path) {
    const body = req.method === "GET" ? Buffer.alloc(0) : await readBody(req)
    let m
    if (path === "/api/polls" && req.method === "GET") return send(res, 200, { polls: polls.list() })
    if (path === "/api/polls" && req.method === "POST") {
      const who = await participant(req, path, body)
      return send(res, 201, polls.create(who.passId, parseJson(body), who.active))
    }
    if (path === "/api/me" && req.method === "GET") {
      const who = await participant(req, path, body)
      return send(res, 200, { pass_id: who.passId, registration: polls.registration(who.passId) })
    }
    if (path === "/api/me/identity" && req.method === "PUT") {
      const who = await participant(req, path, body)
      const joined = polls.register(who.passId, parseJson(body).commitment, who.active)
      return send(res, 200, { registration: polls.registration(who.passId), joined })
    }
    if ((m = path.match(/^\/api\/polls\/([\w-]{1,32})$/)) && req.method === "GET") {
      return send(res, 200, polls.get(m[1]))
    }
    if ((m = path.match(/^\/api\/polls\/([\w-]{1,32})\/group$/)) && req.method === "GET") {
      return send(res, 200, { commitments: polls.group(m[1]) })
    }
    if ((m = path.match(/^\/api\/polls\/([\w-]{1,32})\/me$/)) && req.method === "GET") {
      const who = await participant(req, path, body)
      return send(res, 200, { commitment: polls.membership(m[1], who.passId) })
    }
    if ((m = path.match(/^\/api\/polls\/([\w-]{1,32})\/votes$/)) && req.method === "POST") {
      // Anonymous on purpose: no authorization header is read, the proof is the ballot.
      return send(res, 201, await polls.vote(m[1], parseJson(body).proof))
    }
    if ((m = path.match(/^\/api\/polls\/([\w-]{1,32})\/proofs$/)) && req.method === "GET") {
      return send(res, 200, { proofs: polls.proofs(m[1]) })
    }
    if (path === "/api/health") return send(res, 200, { ok: true })
    throw new PollError(404, "not found")
  }

  async function serveStatic(res, path) {
    if (!staticDir) return send(res, 404, "not found")
    const rel = normalize(decodeURIComponent(path)).replace(/^(\.\.[/\\])+/, "")
    let file = join(staticDir, rel)
    if (!file.startsWith(staticDir)) return send(res, 404, "not found")
    let info = await stat(file).catch(() => null)
    if (!info || info.isDirectory()) {
      // The app routes by hash, but any unknown path shows the app (deep links, /oauth/callback).
      file = join(staticDir, "index.html")
      info = await stat(file).catch(() => null)
      if (!info) return send(res, 404, "not found")
    }
    const type = TYPES[extname(file)] || "application/octet-stream"
    const immutable = /\/assets\/|\/artifacts\//.test(file)
    res.writeHead(200, {
      "Content-Type": type,
      "Cache-Control": immutable ? "public, max-age=31536000, immutable" : "no-cache",
    })
    res.end(await readFile(file))
  }

  return async function handler(req, res) {
    const url = new URL(req.url, "http://vote.local")
    try {
      if (url.pathname.startsWith("/api/")) return await api(req, res, url.pathname)
      if (req.method !== "GET" && req.method !== "HEAD") return send(res, 405, "method not allowed")
      return await serveStatic(res, url.pathname)
    } catch (err) {
      if (err instanceof AuthError) return send(res, 401, { error: err.message })
      if (err instanceof PollError) return send(res, err.status, { error: err.message })
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
  const required = ["PUBLIC_URL", "ONBOARDING_URL", "PASS_INFO_TOKEN"]
  for (const name of required) if (!env[name]) throw new Error(`${name} is not set`)
  const treeDepth = Number(env.TREE_DEPTH || 10)
  const db = openDb(env.DB_PATH || "/data/vote.sqlite")
  const handler = createApp({
    polls: createPolls(db, { treeDepth }),
    passes: onboardingPasses({ baseUrl: env.ONBOARDING_URL, token: env.PASS_INFO_TOKEN }),
    publicUrl: env.PUBLIC_URL,
    staticDir: env.STATIC_DIR || fileURLToPath(new URL("../web/dist", import.meta.url)),
  })
  const port = Number(env.PORT || 8080)
  createServer(handler).listen(port, () => console.log(`vote service on :${port}, public ${env.PUBLIC_URL}`))
}
