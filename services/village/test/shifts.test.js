// /api/shifts on the programme port: the signer's partner from the active passes, the board and
// sign-ups through the onboarding service; nobody else's partner id ever comes from the browser.
import assert from "node:assert/strict"
import { createServer } from "node:http"
import { test } from "node:test"
import { finalizeEvent, generateSecretKey, getPublicKey } from "nostr-tools/pure"
import { createApp, onboardingPasses, onboardingShifts } from "../server/index.js"

const URL_BASE = "http://programme.test"

function nip98(sk, url, method) {
  const tags = [["u", url], ["method", method]]
  const ev = finalizeEvent({ kind: 27235, created_at: Math.floor(Date.now() / 1000), tags, content: "" }, sk)
  return `Nostr ${Buffer.from(JSON.stringify(ev)).toString("base64")}`
}

async function harness({ shifts = true } = {}) {
  const volunteer = generateSecretKey()
  const visitor = generateSecretKey()
  const outsider = generateSecretKey()
  const partners = new Map([[getPublicKey(volunteer), 7], [getPublicKey(visitor), 8]])
  const calls = []
  const board = {
    7: { role: "volunteer", count: 1, min_shifts: 3, change_hours: 48, shifts: [{ id: 3, title: "Kitchen", mine: true }] },
    8: { role: null, shifts: [] },
  }
  const server = createServer(
    createApp({
      passes: { activePubkeys: async () => new Set(partners.keys()), partnerOf: async pk => partners.get(pk) || null },
      shifts: shifts
        ? {
            board: async partner => (calls.push(["board", partner]), board[partner]),
            act: async (partner, id, action) => (calls.push([action, partner, id]), { result: "ok", count: 2, min_shifts: 3 }),
          }
        : null,
      app: "programme",
      publicUrl: URL_BASE,
      staticDir: null,
    }),
  )
  await new Promise(r => server.listen(0, r))
  const base = `http://127.0.0.1:${server.address().port}`
  const get = sk => fetch(`${base}/api/shifts`, { headers: sk ? { Authorization: nip98(sk, `${URL_BASE}/api/shifts`, "GET") } : {} })
  const post = (sk, path, signedPath = path) =>
    fetch(`${base}${path}`, { method: "POST", headers: { Authorization: nip98(sk, `${URL_BASE}${signedPath}`, "POST") } })
  return { volunteer, visitor, outsider, calls, get, post, close: () => server.close() }
}

test("shifts: the board for the signer's own partner; no role for others", async () => {
  const h = await harness()
  try {
    const res = await h.get(h.volunteer)
    assert.equal(res.status, 200)
    assert.equal((await res.json()).role, "volunteer")
    assert.deepEqual((await (await h.get(h.visitor)).json()).role, null)
    assert.deepEqual(await (await h.get(h.outsider)).json(), { role: null, shifts: [] })
    assert.deepEqual(h.calls, [["board", 7], ["board", 8]], "no call for a key without a pass")
    assert.equal((await h.get(null)).status, 401)
  } finally {
    h.close()
  }
})

test("shifts: sign up and leave as oneself, signed for that very path", async () => {
  const h = await harness()
  try {
    assert.equal((await h.post(h.volunteer, "/api/shifts/3/signup")).status, 200)
    assert.equal((await h.post(h.volunteer, "/api/shifts/3/leave")).status, 200)
    assert.deepEqual(h.calls, [["signup", 7, 3], ["leave", 7, 3]])
    assert.equal((await h.post(h.volunteer, "/api/shifts/4/signup", "/api/shifts/3/signup")).status, 401)
    assert.equal((await h.post(h.outsider, "/api/shifts/3/signup")).status, 403)
    assert.equal((await h.post(h.volunteer, "/api/shifts/3/swap")).status, 404)
    assert.equal(h.calls.length, 2)
  } finally {
    h.close()
  }
})

test("shifts: only the programme port serves them", async () => {
  const h = await harness({ shifts: false })
  try {
    assert.equal((await h.get(h.volunteer)).status, 404)
  } finally {
    h.close()
  }
})

test("onboarding clients: the partner behind a key, the shifts calls with the token", async () => {
  const seen = []
  const fetchImpl = async (url, init = {}) => {
    seen.push([init.method || "GET", url, init.headers?.Authorization])
    if (url.endsWith("/admin/active-passes"))
      return new Response(JSON.stringify({ passes: [{ pass_id: 1, partner_id: 7, pubkey: "aa".repeat(32) }, { pass_id: 2, pubkey: "bb".repeat(32) }] }))
    if (url.includes("/admin/shifts/9/"))
      return new Response(JSON.stringify({ detail: "unknown shift" }), { status: 404 })
    return new Response(JSON.stringify({ result: "ok" }))
  }
  const passes = onboardingPasses({ baseUrl: "http://onb/", token: "t", fetchImpl })
  assert.equal(await passes.partnerOf("aa".repeat(32)), 7)
  assert.equal(await passes.partnerOf("bb".repeat(32)), null)
  assert.ok((await passes.activePubkeys()).has("bb".repeat(32)))
  assert.equal(seen.length, 1, "one fetch, cached")
  const shifts = onboardingShifts({ baseUrl: "http://onb", token: "t", fetchImpl })
  await shifts.board(7)
  await shifts.act(7, 3, "signup")
  assert.deepEqual(seen.slice(1), [
    ["GET", "http://onb/admin/shifts?partner=7", "Bearer t"],
    ["POST", "http://onb/admin/shifts/3/signup?partner=7", "Bearer t"],
  ])
  await assert.rejects(shifts.act(7, 9, "leave"), { status: 404 })
})
