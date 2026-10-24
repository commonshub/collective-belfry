// The participants endpoint through the HTTP handler, and the shared listing/profile parsers.
import assert from "node:assert/strict"
import { createServer } from "node:http"
import { test } from "node:test"
import { finalizeEvent, generateSecretKey, getPublicKey } from "nostr-tools/pure"
import { createApp, onboardingPasses } from "../server/index.js"
import { listingFromEvent, listingTemplate, parseProfile, latestByAuthor, directoryConsent } from "../web/shared/model.js"

function nip98(sk, url, method = "GET") {
  const ev = finalizeEvent(
    { kind: 27235, created_at: Math.floor(Date.now() / 1000), tags: [["u", url], ["method", method]], content: "" },
    sk,
  )
  return `Nostr ${Buffer.from(JSON.stringify(ev)).toString("base64")}`
}

async function harness() {
  const member = generateSecretKey()
  const outsider = generateSecretKey()
  const active = new Set([getPublicKey(member), "a".repeat(64)])
  const server = createServer(
    createApp({ passes: { activePubkeys: async () => active }, publicUrl: "http://people.test", staticDir: null }),
  )
  await new Promise(r => server.listen(0, r))
  const base = `http://127.0.0.1:${server.address().port}`
  const get = (path, headers = {}) => fetch(base + path, { headers })
  return { member, outsider, get, close: () => server.close() }
}

test("participants: only an active pass learns the active keys", async () => {
  const h = await harness()
  try {
    assert.equal((await h.get("/api/participants")).status, 401)
    const wrongUrl = await h.get("/api/participants", { Authorization: nip98(h.member, "http://other.test/api/participants") })
    assert.equal(wrongUrl.status, 401)
    const outsider = await h.get("/api/participants", { Authorization: nip98(h.outsider, "http://people.test/api/participants") })
    assert.equal(outsider.status, 403)
    const res = await h.get("/api/participants", { Authorization: nip98(h.member, "http://people.test/api/participants") })
    assert.equal(res.status, 200)
    const { pubkeys } = await res.json()
    assert.deepEqual(pubkeys, [getPublicKey(h.member), "a".repeat(64)].sort())
    assert.equal((await h.get("/api/health")).status, 200)
  } finally {
    h.close()
  }
})

test("onboardingPasses caches and keeps only keys", async () => {
  let calls = 0
  const passes = onboardingPasses({
    baseUrl: "http://onb/",
    token: "t",
    fetchImpl: async (url, opts) => {
      calls++
      assert.equal(url, "http://onb/admin/active-passes")
      assert.equal(opts.headers.Authorization, "Bearer t")
      return { ok: true, json: async () => ({ passes: [{ pass_id: 1, pubkey: "b".repeat(64) }, { pass_id: 2, pubkey: null }] }) }
    },
  })
  assert.deepEqual([...(await passes.activePubkeys())], ["b".repeat(64)])
  await passes.activePubkeys()
  assert.equal(calls, 1)
})

test("listings round-trip through the NIP-99 template", () => {
  const sk = generateSecretKey()
  const t = listingTemplate({
    d: "abc",
    title: "Hand-made mug",
    summary: "Blue glaze",
    description: "Pick up at the workshop tent.",
    price: 12,
    category: "stuff",
    location: "Workshop tent",
    images: ["https://blossom.test/x.jpg"],
    status: "active",
    publishedAt: 1700000000,
  })
  const ev = finalizeEvent({ ...t, created_at: 1700000100 }, sk)
  const l = listingFromEvent(ev)
  assert.equal(l.title, "Hand-made mug")
  assert.equal(l.price, 12)
  assert.equal(l.category, "stuff")
  assert.equal(l.status, "active")
  assert.deepEqual(l.images, ["https://blossom.test/x.jpg"])
  assert.equal(l.coordinate, `30402:${getPublicKey(sk)}:abc`)
  assert.equal(l.publishedAt, 1700000000)
  // free items and odd prices
  const free = listingFromEvent(finalizeEvent({ ...listingTemplate({ d: "f", title: "Seeds", price: 0 }), created_at: 1 }, sk))
  assert.equal(free.price, 0)
  const bad = { ...ev, tags: ev.tags.map(x => (x[0] === "price" ? ["price", "abc", "OSV"] : x)) }
  assert.equal(listingFromEvent(bad).price, null)
  const noTitle = { ...ev, tags: ev.tags.filter(x => x[0] !== "title") }
  assert.equal(listingFromEvent(noTitle), null)
})

test("profiles and directory consent", () => {
  assert.deepEqual(parseProfile({ content: '{"name":"ada","display_name":"Ada","picture":"javascript:x","website":"https://a.dev"}' }), {
    name: "ada",
    displayName: "Ada",
    about: "",
    picture: "",
    website: "https://a.dev",
    nip05: "",
  })
  assert.equal(parseProfile({ content: "not json" }).displayName, "")
  const a = "a".repeat(64)
  const events = [
    { pubkey: a, created_at: 1, content: '{"listed":true}', tags: [["d", "osv-directory"]] },
    { pubkey: a, created_at: 2, content: '{"listed":false}', tags: [["d", "osv-directory"]] },
  ]
  assert.equal(directoryConsent(latestByAuthor(events).get(a)), false)
  assert.equal(directoryConsent(events[0]), true)
  assert.equal(directoryConsent({ content: "{}" }), false)
})
