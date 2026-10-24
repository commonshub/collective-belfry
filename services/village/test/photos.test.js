// The photos app's event formats: NIP-68 pictures, likes, comments, tips and deletions.
import assert from "node:assert/strict"
import { test } from "node:test"
import { finalizeEvent, generateSecretKey, getPublicKey, verifyEvent } from "nostr-tools/pure"
import {
  PHOTO_KIND,
  commentTemplate,
  deletedIds,
  likeTemplate,
  nutzapAmount,
  parseHashtags,
  photoFromEvent,
  photoTemplate,
} from "../web/shared/model.js"

const X = "ab".repeat(32)
const img = { url: "https://blossom.example/" + X + ".jpg", m: "image/jpeg", x: X, width: 2048, height: 1536, alt: "The lake at dawn" }

test("photo: template and parser round-trip (NIP-68 imeta)", () => {
  const sk = generateSecretKey()
  const t = photoTemplate({ title: "Dawn", caption: "First light", hashtags: ["lake", "morning"], location: "The lake", images: [img] })
  assert.equal(t.kind, PHOTO_KIND)
  assert.deepEqual(t.tags.find(x => x[0] === "imeta"), ["imeta", `url ${img.url}`, "m image/jpeg", `x ${X}`, "dim 2048x1536", "alt The lake at dawn"])
  assert.ok(t.tags.some(x => x[0] === "m" && x[1] === "image/jpeg"))
  assert.ok(t.tags.some(x => x[0] === "x" && x[1] === X))
  const ev = finalizeEvent(t, sk)
  assert.ok(verifyEvent(ev))
  const p = photoFromEvent(ev)
  assert.equal(p.pubkey, getPublicKey(sk))
  assert.equal(p.title, "Dawn")
  assert.equal(p.caption, "First light")
  assert.deepEqual(p.hashtags, ["lake", "morning"])
  assert.equal(p.location, "The lake")
  assert.deepEqual(p.images, [img])
})

test("photo: needs a picture; unsafe URLs and other media types are dropped", () => {
  assert.throws(() => photoTemplate({ images: [] }))
  const base = { kind: PHOTO_KIND, pubkey: "a".repeat(64), id: "b".repeat(64), created_at: 1, content: "" }
  assert.equal(photoFromEvent({ ...base, tags: [["imeta", "url javascript:alert(1)"]] }), null)
  assert.equal(photoFromEvent({ ...base, tags: [["imeta", "url https://x.example/v.mp4", "m video/mp4"]] }), null)
  assert.equal(photoFromEvent({ ...base, kind: 1, tags: [["imeta", "url https://x.example/a.jpg"]] }), null)
  const p = photoFromEvent({ ...base, tags: [["imeta", "url https://x.example/a.jpg", "dim huge", "x nothex"]] })
  assert.deepEqual(p.images, [{ url: "https://x.example/a.jpg", m: "", x: "", width: 0, height: 0, alt: "" }])
})

test("hashtags: split, lower-cased, deduplicated, junk dropped", () => {
  assert.deepEqual(parseHashtags("#Garden sunset, #garden  ##Night  <script> café"), ["garden", "sunset", "night", "café"])
  assert.equal(parseHashtags(Array.from({ length: 20 }, (_, i) => `t${i}`).join(" ")).length, 10)
})

test("like and comment name the photo and its author (NIP-25, NIP-22)", () => {
  const p = { id: "c".repeat(64), pubkey: "d".repeat(64) }
  const like = likeTemplate(p)
  assert.equal(like.kind, 7)
  assert.equal(like.content, "+")
  assert.deepEqual(like.tags, [["e", p.id], ["p", p.pubkey], ["k", "20"]])
  const c = commentTemplate(p, "Lovely")
  assert.equal(c.kind, 1111)
  assert.deepEqual(c.tags.filter(t => ["E", "K", "P"].includes(t[0])), [["E", p.id, "", p.pubkey], ["K", "20"], ["P", p.pubkey]])
  assert.deepEqual(c.tags.filter(t => ["e", "k", "p"].includes(t[0])), [["e", p.id, "", p.pubkey], ["k", "20"], ["p", p.pubkey]])
})

test("tips: a nutzap's amount is the sum of its proofs, junk counts zero", () => {
  const proof = a => ["proof", JSON.stringify({ amount: a, id: "00", secret: "s", C: "02" })]
  assert.equal(nutzapAmount({ tags: [proof(4), proof(1), ["proof", "{bad"], proof(-5), ["e", "x"]] }), 5)
})

test("deletions count only for the author's own events", () => {
  const mine = { id: "1".repeat(64), pubkey: "a".repeat(64) }
  const theirs = { id: "2".repeat(64), pubkey: "b".repeat(64) }
  const byId = new Map([mine, theirs].map(e => [e.id, e]))
  const del = { pubkey: mine.pubkey, tags: [["e", mine.id], ["e", theirs.id]] }
  assert.deepEqual([...deletedIds([del], byId)], [mine.id])
})
