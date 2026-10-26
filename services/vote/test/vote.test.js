// End to end through the HTTP handler with real Semaphore proofs (the depth-10 artifacts are
// fetched by scripts/fetch-artifacts.sh into web/public/artifacts).
import assert from "node:assert/strict"
import { createHash } from "node:crypto"
import { existsSync, mkdtempSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { DatabaseSync } from "node:sqlite"
import { createServer } from "node:http"
import { test } from "node:test"
import { fileURLToPath } from "node:url"
import { Group, Identity, generateProof } from "@semaphore-protocol/core"
import { finalizeEvent, generateSecretKey, getPublicKey } from "nostr-tools/pure"
import { openDb } from "../server/db.js"
import { createApp } from "../server/index.js"
import { verifyNip98 } from "../server/nip98.js"
import { createPolls, pollScope } from "../server/polls.js"

const ART = fileURLToPath(new URL("../web/public/artifacts/", import.meta.url))
const artifacts = { wasm: `${ART}semaphore-10.wasm`, zkey: `${ART}semaphore-10.zkey` }
const haveArtifacts = existsSync(artifacts.zkey)

function nip98(sk, url, method, body = "") {
  const tags = [
    ["u", url],
    ["method", method],
  ]
  if (body) tags.push(["payload", createHash("sha256").update(body).digest("hex")])
  const ev = finalizeEvent({ kind: 27235, created_at: Math.floor(Date.now() / 1000), tags, content: "" }, sk)
  return `Nostr ${Buffer.from(JSON.stringify(ev)).toString("base64")}`
}

async function harness() {
  const keys = [generateSecretKey(), generateSecretKey(), generateSecretKey(), generateSecretKey()]
  const active = new Map(keys.slice(0, 3).map((sk, i) => [getPublicKey(sk), 100 + i])) // keys[3]: no pass
  const polls = createPolls(openDb(":memory:"), { treeDepth: 10 })
  const server = createServer(
    createApp({ polls, passes: { active: async () => active }, publicUrl: "http://vote.test", staticDir: null }),
  )
  await new Promise(r => server.listen(0, r))
  const port = server.address().port
  async function call(method, path, { body, sk } = {}) {
    const raw = body === undefined ? "" : JSON.stringify(body)
    const headers = { "Content-Type": "application/json" }
    if (sk) headers.Authorization = nip98(sk, `http://vote.test${path}`, method, raw)
    const res = await fetch(`http://127.0.0.1:${port}${path}`, { method, headers, body: raw || undefined })
    return { status: res.status, json: await res.json().catch(() => null) }
  }
  return { keys, active, polls, call, port, close: () => server.close() }
}

const inAnHour = () => Math.floor(Date.now() / 1000) + 3600

test("NIP-98: url, method, payload, expiry and replay are checked", () => {
  const sk = generateSecretKey()
  const body = Buffer.from('{"a":1}')
  const h = nip98(sk, "http://vote.test/api/me", "PUT", body.toString())
  assert.equal(verifyNip98(h, { url: "http://vote.test/api/me", method: "PUT", body }), getPublicKey(sk))
  assert.throws(() => verifyNip98(h, { url: "http://vote.test/api/me", method: "PUT", body }), /already used/)
  const h2 = nip98(sk, "http://vote.test/api/me", "PUT", body.toString())
  assert.throws(() => verifyNip98(h2, { url: "http://other/api/me", method: "PUT", body }), /wrong url/)
  assert.throws(() => verifyNip98(h2, { url: "http://vote.test/api/me", method: "POST", body }), /wrong method/)
  assert.throws(
    () => verifyNip98(h2, { url: "http://vote.test/api/me", method: "PUT", body: Buffer.from("{}") }),
    /payload/,
  )
  assert.throws(
    () => verifyNip98(h2, { url: "http://vote.test/api/me", method: "PUT", body, now: Date.now() + 120_000 }),
    /expired/,
  )
  assert.throws(() => verifyNip98(undefined, { url: "x", method: "GET" }), /missing/)
})

test("polls: only active passes create polls and register; input is validated", async () => {
  const h = await harness()
  try {
    const poll = { title: "Lunch", options: ["Soup", "Salad"], closes_at: inAnHour() }
    assert.equal((await h.call("POST", "/api/polls", { body: poll })).status, 401)
    assert.equal((await h.call("POST", "/api/polls", { body: poll, sk: h.keys[3] })).status, 403)
    assert.equal((await h.call("POST", "/api/polls", { body: { ...poll, options: ["One"] }, sk: h.keys[0] })).status, 400)
    assert.equal((await h.call("POST", "/api/polls", { body: { ...poll, closes_at: 5 }, sk: h.keys[0] })).status, 400)
    const created = await h.call("POST", "/api/polls", { body: poll, sk: h.keys[0] })
    assert.equal(created.status, 201)
    assert.equal(created.json.scope, pollScope(created.json.id))
    assert.equal(created.json.members, 0)
    const list = await h.call("GET", "/api/polls")
    assert.equal(list.json.polls.length, 1)
    assert.equal((await h.call("PUT", "/api/me/identity", { body: { commitment: "x" }, sk: h.keys[0] })).status, 400)
  } finally {
    h.close()
  }
})

test("one anonymous vote per pass; replaced identities count from the next poll", { skip: !haveArtifacts && "no artifacts" }, async () => {
  const h = await harness()
  try {
    const ids = [new Identity(), new Identity(), new Identity()]
    // passes 100 and 101 register before the poll, 102 after it opened
    for (const i of [0, 1]) {
      const r = await h.call("PUT", "/api/me/identity", { body: { commitment: ids[i].commitment.toString() }, sk: h.keys[i] })
      assert.equal(r.status, 200)
    }
    const poll = (await h.call("POST", "/api/polls", { body: { title: "Q", options: ["A", "B", "C"], closes_at: inAnHour() }, sk: h.keys[0] })).json
    assert.equal(poll.members, 2)
    const late = await h.call("PUT", "/api/me/identity", { body: { commitment: ids[2].commitment.toString() }, sk: h.keys[2] })
    assert.deepEqual(late.json.joined, [poll.id])

    const group = async () => new Group((await h.call("GET", `/api/polls/${poll.id}/group`)).json.commitments.map(BigInt))
    const prove = async (identity, option, g) => generateProof(identity, g ?? (await group()), option, poll.scope, 10, artifacts)

    // pass 100 proves against the group before 102 joined: earlier roots stay valid
    const early = await prove(ids[0], 2, new Group([ids[0].commitment, ids[1].commitment]))
    assert.equal((await h.call("POST", `/api/polls/${poll.id}/votes`, { body: { proof: early } })).status, 201)
    assert.equal((await h.call("POST", `/api/polls/${poll.id}/votes`, { body: { proof: early } })).status, 409)
    const again = await prove(ids[0], 1)
    assert.equal((await h.call("POST", `/api/polls/${poll.id}/votes`, { body: { proof: again } })).status, 409)

    // pass 101 replaces its identity during the poll: the new one is not in this poll's group
    const fresh = new Identity()
    await h.call("PUT", "/api/me/identity", { body: { commitment: fresh.commitment.toString() }, sk: h.keys[1] })
    assert.equal((await h.call("GET", `/api/polls/${poll.id}/me`, { sk: h.keys[1] })).json.commitment, ids[1].commitment.toString())
    assert.equal((await group()).size, 3)
    // a proof by an identity outside the group cannot be built against the poll's group, and one
    // against a group of its own has an unknown root
    const outsider = await prove(fresh, 0, new Group([fresh.commitment, ids[2].commitment]))
    assert.match((await h.call("POST", `/api/polls/${poll.id}/votes`, { body: { proof: outsider } })).json.error, /group/)
    const ok = await prove(ids[1], 0)
    assert.equal((await h.call("POST", `/api/polls/${poll.id}/votes`, { body: { proof: ok } })).status, 201)

    // tampering: another option with the same proof, another poll's scope
    const late2 = await prove(ids[2], 1)
    assert.equal((await h.call("POST", `/api/polls/${poll.id}/votes`, { body: { proof: { ...late2, message: "2" } } })).json.error, "invalid proof")
    assert.equal((await h.call("POST", `/api/polls/${poll.id}/votes`, { body: { proof: { ...late2, scope: "1" } } })).status, 400)
    assert.equal((await h.call("POST", `/api/polls/${poll.id}/votes`, { body: { proof: { ...late2, message: "7" } } })).json.error, "no such option")
    assert.equal((await h.call("POST", `/api/polls/${poll.id}/votes`, { body: { proof: late2 } })).status, 201)

    const open = (await h.call("GET", `/api/polls/${poll.id}`)).json
    assert.equal(open.votes, 3)
    assert.equal(open.results, undefined, "no results while open")
    assert.equal((await h.call("GET", `/api/polls/${poll.id}/proofs`)).status, 409)

  } finally {
    h.close()
  }
})

test("closed polls publish results and proofs", { skip: !haveArtifacts && "no artifacts" }, async () => {
  const dbh = openDb(":memory:")
  const polls = createPolls(dbh, { treeDepth: 10 })
  const ids = [new Identity(), new Identity()]
  polls.register(1, ids[0].commitment.toString(), new Set([1, 2]))
  polls.register(2, ids[1].commitment.toString(), new Set([1, 2]))
  const poll = polls.create(1, { title: "Q", options: ["A", "B"], closes_at: inAnHour() }, new Set([1, 2]))
  const g = new Group(polls.group(poll.id).map(BigInt))
  await polls.vote(poll.id, await generateProof(ids[0], g, 1, poll.scope, 10, artifacts))
  await polls.vote(poll.id, await generateProof(ids[1], g, 1, poll.scope, 10, artifacts))
  dbh.prepare("UPDATE polls SET closes_at = ? WHERE id = ?").run(Math.floor(Date.now() / 1000) - 1, poll.id)
  const closed = polls.get(poll.id)
  assert.equal(closed.open, false)
  assert.deepEqual(closed.results, [0, 2])
  assert.equal(polls.proofs(poll.id).length, 2)
  await assert.rejects(polls.vote(poll.id, {}), /closed/)
})

test("revoked passes do not join polls", () => {
  const polls = createPolls(openDb(":memory:"), { treeDepth: 10 })
  const id = new Identity()
  polls.register(5, id.commitment.toString(), new Set()) // registered, but no active pass
  const poll = polls.create(1, { title: "Q", options: ["A", "B"], closes_at: inAnHour() }, new Set([1]))
  assert.equal(poll.members, 0)
})

test("polls about a programme session: stored, listed by session, readable from other origins", async () => {
  const h = await harness()
  try {
    const coord = `31923:${"ab".repeat(32)}:osv-programme-2027-01-25-morning`
    const base = { options: ["Yes", "No"], closes_at: inAnHour() }
    const made = await h.call("POST", "/api/polls", { body: { ...base, title: "More time for questions?", session: coord, session_title: "Opening" }, sk: h.keys[0] })
    assert.equal(made.status, 201)
    assert.equal(made.json.session, coord)
    assert.equal(made.json.session_title, "Opening")
    await h.call("POST", "/api/polls", { body: { ...base, title: "Unrelated", session_title: "ignored without a session" }, sk: h.keys[0] })
    const bad = await h.call("POST", "/api/polls", { body: { ...base, title: "x", session: "30023:aa:x" }, sk: h.keys[0] })
    assert.equal(bad.status, 400)
    const res = await fetch(`http://127.0.0.1:${h.port}/api/polls?session=${encodeURIComponent(coord)}`)
    assert.equal(res.headers.get("access-control-allow-origin"), "*")
    assert.deepEqual((await res.json()).polls.map(p => p.title), ["More time for questions?"])
    const all = (await h.call("GET", "/api/polls")).json.polls
    assert.equal(all.length, 2)
    assert.equal(all.find(p => p.title === "Unrelated").session_title, "")
  } finally {
    h.close()
  }
})

test("an older database gains the session columns", () => {
  const file = join(mkdtempSync(join(tmpdir(), "vote-")), "vote.sqlite")
  const old = new DatabaseSync(file)
  old.exec(`CREATE TABLE polls (id TEXT PRIMARY KEY, scope TEXT NOT NULL UNIQUE, title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
    options TEXT NOT NULL, creator_pass_id INTEGER NOT NULL, created_at INTEGER NOT NULL, closes_at INTEGER NOT NULL)`)
  old.exec(`INSERT INTO polls VALUES ('old', '${pollScope("old")}', 'Old poll', '', '["a","b"]', 1, 1, 2)`)
  old.close()
  const polls = createPolls(openDb(file), { treeDepth: 10 })
  const [p] = polls.list()
  assert.equal(p.title, "Old poll")
  assert.equal(p.session, "")
  openDb(file) // a second start leaves it as it is
})
