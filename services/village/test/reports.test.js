// /api/report and /api/hidden: NIP-98 with the body's hash, the signer as reporter, active passes only.
import assert from "node:assert/strict"
import { createHash } from "node:crypto"
import { createServer } from "node:http"
import { test } from "node:test"
import { finalizeEvent, generateSecretKey, getPublicKey } from "nostr-tools/pure"
import { createApp, onboardingModeration } from "../server/index.js"
import { isHidden } from "../web/shared/model.js"

const URL_BASE = "http://photos.test"

function nip98(sk, url, method, body) {
  const tags = [["u", url], ["method", method]]
  if (body) tags.push(["payload", createHash("sha256").update(body).digest("hex")])
  const ev = finalizeEvent({ kind: 27235, created_at: Math.floor(Date.now() / 1000), tags, content: "" }, sk)
  return `Nostr ${Buffer.from(JSON.stringify(ev)).toString("base64")}`
}

async function harness() {
  const member = generateSecretKey()
  const outsider = generateSecretKey()
  const sent = []
  const moderation = {
    hidden: async () => ["ee".repeat(32)],
    report: async body => (sent.push(body), { id: sent.length }),
  }
  const server = createServer(
    createApp({
      passes: { activePubkeys: async () => new Set([getPublicKey(member)]) },
      moderation,
      app: "photos",
      publicUrl: URL_BASE,
      staticDir: null,
    }),
  )
  await new Promise(r => server.listen(0, r))
  const base = `http://127.0.0.1:${server.address().port}`
  const post = (sk, body, signedBody = body) =>
    fetch(`${base}/api/report`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: nip98(sk, `${URL_BASE}/api/report`, "POST", signedBody) },
      body,
    })
  return { member, outsider, sent, base, post, close: () => server.close() }
}

const REPORT = JSON.stringify({ target_kind: "photo", target_ref: "cc".repeat(32), target_pubkey: "bb".repeat(32), reason: "spam", note: "x", reporter_pubkey: "ff".repeat(32), app: "market" })

test("report: forwarded with the signer as reporter and the server's app", async () => {
  const h = await harness()
  try {
    const res = await h.post(h.member, REPORT)
    assert.equal(res.status, 200)
    assert.equal(h.sent.length, 1)
    assert.equal(h.sent[0].reporter_pubkey, getPublicKey(h.member), "never the body's claim")
    assert.equal(h.sent[0].app, "photos")
    assert.equal(h.sent[0].reason, "spam")
  } finally {
    h.close()
  }
})

test("report: refused without a pass, with a wrong body hash, or unsigned", async () => {
  const h = await harness()
  try {
    assert.equal((await h.post(h.outsider, REPORT)).status, 403)
    assert.equal((await h.post(h.member, REPORT, REPORT + " ")).status, 401)
    assert.equal((await fetch(`${h.base}/api/report`, { method: "POST", body: REPORT })).status, 401)
    assert.equal(h.sent.length, 0)
  } finally {
    h.close()
  }
})

test("hidden: the list, unauthenticated", async () => {
  const h = await harness()
  try {
    assert.deepEqual(await (await fetch(`${h.base}/api/hidden`)).json(), { hidden: ["ee".repeat(32)] })
  } finally {
    h.close()
  }
})

test("onboardingModeration passes 400/403 on and caches the hidden list", async () => {
  let calls = 0
  const fetchImpl = async (url, opts) => {
    calls++
    if (url.endsWith("/admin/moderation/hidden")) return new Response(JSON.stringify({ hidden: ["a"] }))
    assert.equal(opts.headers.Authorization, "Bearer t")
    return new Response(JSON.stringify({ detail: "unknown reason" }), { status: 400 })
  }
  const m = onboardingModeration({ baseUrl: "http://onb/", token: "t", fetchImpl })
  assert.deepEqual(await m.hidden(), ["a"])
  await m.hidden()
  assert.equal(calls, 1)
  await assert.rejects(m.report({}), e => e.status === 400 && /unknown reason/.test(e.message))
})

test("isHidden: by id, by offer coordinate, directory entries by author", () => {
  const pk = "bb".repeat(32)
  const hidden = new Set(["cc".repeat(32), `30402:${pk}:mug`, "dd".repeat(32)])
  assert.ok(isHidden(hidden, { id: "cc".repeat(32), kind: 20, pubkey: pk, tags: [] }))
  assert.ok(isHidden(hidden, { id: "x", kind: 30402, pubkey: pk, tags: [["d", "mug"]] }))
  assert.ok(isHidden(hidden, { id: "x", kind: 30078, pubkey: "dd".repeat(32), tags: [["d", "osv-directory"]] }))
  assert.ok(!isHidden(hidden, { id: "x", kind: 1, pubkey: "dd".repeat(32), tags: [] }))
  assert.ok(!isHidden(new Set(), { id: "cc".repeat(32), kind: 20, pubkey: pk, tags: [] }))
})
