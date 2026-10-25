// The programme app's event formats: sessions (NIP-52 kind 31923), RSVPs (31925), comments
// (NIP-22 on the session's coordinate), photos taken at a session and the calendar file.
import assert from "node:assert/strict"
import { test } from "node:test"
import { finalizeEvent, generateSecretKey, getPublicKey, verifyEvent } from "nostr-tools/pure"
import {
  RSVP_KIND,
  SESSION_KIND,
  activityFromEvent,
  currentSessions,
  photoFromEvent,
  photoTemplate,
  rsvpTemplate,
  rsvpsFor,
  sessionCommentTemplate,
  sessionFromEvent,
  sessionIcs,
} from "../web/shared/model.js"

const programmeSk = generateSecretKey()
const PROGRAMME = getPublicKey(programmeSk)
const at = (sk, kind, tags, created_at = 1800000000, content = "") => finalizeEvent({ kind, created_at, tags, content }, sk)

// as sync-programme publishes them (services/onboarding programme.py)
const slot = (d, title, start, created_at, extra = []) =>
  at(
    programmeSk,
    SESSION_KIND,
    [["d", d], ["title", title], ["start", String(start)], ["end", String(start + 3 * 3600)], ["start_tzid", "Europe/Brussels"], ["location", "Commons Hub, Rue de la Madeleine 51"], ["t", "osv"], ...extra],
    created_at,
    "Open Source Village programme, Monday morning.",
  )

test("session: the programme's events, their tracks and the newest version", () => {
  const old = slot("osv-programme-2027-01-25-morning", "Opening", 1800000000, 100, [["t", "programme"]])
  const fresh = slot("osv-programme-2027-01-25-morning", "Opening and welcome", 1800000000, 200, [["t", "programme"]])
  const room = slot("osv-room-angel-abc", "Workshop: Nostr", 1800003600, 100, [["t", "room"], ["t", "room-angel"]])
  room.tags.find(t => t[0] === "location")[1] = "Angel room, Commons Hub"
  const sk = generateSecretKey()
  const own = at(sk, SESSION_KIND, [["d", "meetup"], ["title", "Rust meetup"], ["start", "1800007200"]])
  const broken = at(sk, SESSION_KIND, [["d", "x"], ["title", "No start"]])
  const sessions = currentSessions([old, fresh, room, own, broken], [], PROGRAMME)
  assert.deepEqual(sessions.map(s => s.title), ["Opening and welcome", "Workshop: Nostr", "Rust meetup"])
  const [opening, workshop, meetup] = sessions
  assert.deepEqual(opening.track, { id: "programme", title: "Village programme" })
  assert.equal(opening.official, true)
  assert.equal(opening.coordinate, `31923:${PROGRAMME}:osv-programme-2027-01-25-morning`)
  assert.equal(opening.end - opening.start, 3 * 3600)
  assert.deepEqual(workshop.track, { id: "room-angel", title: "Angel room" })
  assert.deepEqual(meetup.track, { id: "community", title: "From participants" })
  assert.equal(meetup.official, false)
  assert.equal(meetup.end, meetup.start, "no end: a point in time")
  // a participant cannot pretend to be the programme by its tags
  const fake = at(sk, SESSION_KIND, [["d", "p"], ["title", "Fake"], ["start", "1800000000"], ["t", "programme"]])
  assert.deepEqual(sessionFromEvent(fake, PROGRAMME).track.id, "community")
})

test("session: deletions by the author remove it, others' deletions do not", () => {
  const ev = slot("osv-room-angel-1", "Booked", 1800000000, 100, [["t", "room-angel"]])
  const coord = `31923:${PROGRAMME}:osv-room-angel-1`
  const del = at(programmeSk, 5, [["a", coord]], 150)
  assert.equal(currentSessions([ev], [del], PROGRAMME).length, 0)
  const someone = at(generateSecretKey(), 5, [["a", coord]], 150)
  assert.equal(currentSessions([ev], [someone], PROGRAMME).length, 1)
  // a newer version after the deletion is back
  const again = slot("osv-room-angel-1", "Booked again", 1800000000, 200, [["t", "room-angel"]])
  assert.equal(currentSessions([ev, again], [del], PROGRAMME)[0].title, "Booked again")
})

test("rsvp: one replaceable answer per participant, the newest counts", () => {
  const s = currentSessions([slot("osv-programme-2027-01-25-evening", "Party", 1800000000, 1, [["t", "programme"]])], [], PROGRAMME)[0]
  const sk = generateSecretKey()
  const t = rsvpTemplate(s, "accepted")
  assert.equal(t.kind, RSVP_KIND)
  assert.deepEqual(t.tags.find(x => x[0] === "a"), ["a", s.coordinate])
  assert.deepEqual(t.tags.find(x => x[0] === "status"), ["status", "accepted"])
  assert.ok(t.tags.some(x => x[0] === "d" && x[1] === `rsvp-${s.coordinate}`))
  const yes = finalizeEvent({ ...t, created_at: 100 }, sk)
  const no = finalizeEvent({ ...rsvpTemplate(s, "declined"), created_at: 200 }, sk)
  const other = finalizeEvent({ ...rsvpTemplate(s, "tentative"), created_at: 50 }, generateSecretKey())
  const elsewhere = finalizeEvent({ ...rsvpTemplate({ ...s, coordinate: "31923:aa:x" }, "accepted"), created_at: 300 }, sk)
  const answers = rsvpsFor([yes, no, other, elsewhere], s.coordinate)
  assert.equal(answers.get(getPublicKey(sk)), "declined")
  assert.equal(answers.size, 2)
  assert.throws(() => rsvpTemplate(s, "yes"))
  // what Calendar by Form* writes is read too (its own d tag, a + status)
  const formstr = at(generateSecretKey(), RSVP_KIND, [["a", s.coordinate], ["status", "tentative"], ["d", "f0rm"]])
  assert.equal(rsvpsFor([formstr], s.coordinate).get(formstr.pubkey), "tentative")
})

test("comment: NIP-22 on the session's coordinate; shown in What's new", () => {
  const s = currentSessions([slot("osv-programme-2027-01-26-morning", "Talks", 1800000000, 1, [["t", "programme"]])], [], PROGRAMME)[0]
  const ev = finalizeEvent(sessionCommentTemplate(s, "Slides please"), generateSecretKey())
  assert.ok(verifyEvent(ev))
  assert.deepEqual(ev.tags.find(t => t[0] === "A"), ["A", s.coordinate])
  assert.deepEqual(ev.tags.find(t => t[0] === "K"), ["K", "31923"])
  const item = activityFromEvent(ev)
  assert.equal(item.app, "programme")
  assert.equal(item.session, s.coordinate)
})

test("photo: names the session it was taken at", () => {
  const X = "cd".repeat(32)
  const img = { url: `https://blossom.example/${X}.jpg`, m: "image/jpeg", x: X, width: 10, height: 10, alt: "" }
  const coord = `31923:${PROGRAMME}:osv-programme-2027-01-25-morning`
  const ev = finalizeEvent(photoTemplate({ images: [img], session: coord }), generateSecretKey())
  assert.deepEqual(ev.tags.find(t => t[0] === "a"), ["a", coord])
  assert.equal(photoFromEvent(ev).session, coord)
  const plain = finalizeEvent(photoTemplate({ images: [img], session: "not a coordinate" }), generateSecretKey())
  assert.equal(plain.tags.some(t => t[0] === "a"), false)
})

test("ics: one event in UTC, text escaped", () => {
  const s = sessionFromEvent(slot("osv-room-x-1", "Talk; with, commas", Date.UTC(2027, 0, 25, 8, 30) / 1000, 1, [["t", "room-x"]]), PROGRAMME)
  const ics = sessionIcs(s, "https://programme.example/#/s/naddr1x", Date.UTC(2026, 9, 25))
  assert.match(ics, /^BEGIN:VCALENDAR\r\n/)
  assert.match(ics, /\r\nDTSTART:20270125T083000Z\r\n/)
  assert.match(ics, /\r\nDTEND:20270125T113000Z\r\n/)
  assert.match(ics, /\r\nSUMMARY:Talk\\; with\\, commas\r\n/)
  assert.match(ics, /\r\nURL:https:\/\/programme.example\/#\/s\/naddr1x\r\n/)
  assert.match(ics, /END:VCALENDAR\r\n$/)
})
