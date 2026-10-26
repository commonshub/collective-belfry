// The programme app's event formats: sessions (NIP-52 kind 31923), RSVPs (31925), comments
// (NIP-22 on the session's coordinate), photos taken at a session and the calendar file.
import assert from "node:assert/strict"
import { createHash } from "node:crypto"
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
import * as model from "../web/shared/model.js"

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
  const me = getPublicKey(sk)
  const t = rsvpTemplate(s, "accepted", me)
  assert.equal(t.kind, RSVP_KIND)
  assert.deepEqual(t.tags.find(x => x[0] === "a"), ["a", s.coordinate])
  assert.deepEqual(t.tags.find(x => x[0] === "status"), ["status", "accepted"])
  // the same d as Calendar by Form* (makeDTag of "<responder>:<author>:<d>"), so either app's
  // answer replaces the other's
  const expected = createHash("sha256").update(`${me}:${PROGRAMME}:osv-programme-2027-01-25-evening`).digest("hex").slice(0, 30)
  assert.deepEqual(t.tags.find(x => x[0] === "d"), ["d", expected])
  assert.throws(() => rsvpTemplate(s, "accepted", ""))
  const yes = finalizeEvent({ ...t, created_at: 100 }, sk)
  const no = finalizeEvent({ ...rsvpTemplate(s, "declined", me), created_at: 200 }, sk)
  const otherSk = generateSecretKey()
  const other = finalizeEvent({ ...rsvpTemplate(s, "tentative", getPublicKey(otherSk)), created_at: 50 }, otherSk)
  const elsewhere = finalizeEvent({ ...rsvpTemplate({ ...s, coordinate: "31923:aa:x" }, "accepted", me), created_at: 300 }, sk)
  const answers = rsvpsFor([yes, no, other, elsewhere], s.coordinate)
  assert.equal(answers.get(getPublicKey(sk)), "declined")
  assert.equal(answers.size, 2)
  assert.throws(() => rsvpTemplate(s, "yes", me))
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

test("announce: a participant's session in the village's time, edits keep the coordinate", () => {
  const { sessionTemplate, sessionDeletionTemplate, zonedTime } = model
  // winter (UTC+1) and summer (UTC+2) in Brussels, and the change night
  assert.equal(zonedTime("2027-01-25", "10:00", "Europe/Brussels"), Date.UTC(2027, 0, 25, 9) / 1000)
  assert.equal(zonedTime("2027-07-01", "10:00", "Europe/Brussels"), Date.UTC(2027, 6, 1, 8) / 1000)
  assert.equal(zonedTime("2027-03-28", "04:00", "Europe/Brussels"), Date.UTC(2027, 2, 28, 2) / 1000)
  assert.ok(Number.isNaN(zonedTime("2027-01-25", "", "Europe/Brussels")))
  const start = zonedTime("2027-01-25", "19:00", "Europe/Brussels")
  const t = sessionTemplate({ title: " Rust meetup ", start, end: start + 7200, tzid: "Europe/Brussels", location: "Garden", link: "https://rust.example" })
  const d = t.tags.find(x => x[0] === "d")[1]
  assert.match(d, /^session-/)
  const sk = generateSecretKey()
  const s = sessionFromEvent(finalizeEvent(t, sk), PROGRAMME)
  assert.equal(s.title, "Rust meetup")
  assert.equal(s.track.id, "community")
  assert.equal(s.end - s.start, 7200)
  assert.equal(s.location, "Garden")
  assert.deepEqual(s.links, ["https://rust.example"])
  const edited = sessionTemplate({ d, title: "Rust meetup (moved)", start, end: start + 3600 })
  assert.equal(edited.tags.find(x => x[0] === "d")[1], d)
  assert.throws(() => sessionTemplate({ title: "", start, end: start + 60 }))
  assert.throws(() => sessionTemplate({ title: "x", start, end: start }))
  assert.throws(() => sessionTemplate({ title: "x", start, end: start + 25 * 3600 }))
  // cancelled by the host: gone; a deletion by someone else does not count
  const del = finalizeEvent({ ...sessionDeletionTemplate(s), created_at: s.createdAt + 1 }, sk)
  assert.equal(currentSessions([finalizeEvent(t, sk)], [del], PROGRAMME).length, 0)
})

test("reminders: the calendar file alarms 15 minutes before; several sessions in one file", () => {
  const a = sessionFromEvent(slot("osv-room-x-1", "Talk A", Date.UTC(2027, 0, 25, 8, 30) / 1000, 1, [["t", "room-x"]]), PROGRAMME)
  const b = sessionFromEvent(slot("osv-room-x-2", "Talk B", Date.UTC(2027, 0, 26, 8, 30) / 1000, 1, [["t", "room-x"]]), PROGRAMME)
  assert.match(model.sessionIcs(a), /\r\nBEGIN:VALARM\r\nACTION:DISPLAY\r\nDESCRIPTION:Talk A\r\nTRIGGER:-PT15M\r\nEND:VALARM\r\nEND:VEVENT\r\n/)
  assert.doesNotMatch(model.sessionIcs(a, "", Date.now(), 0), /VALARM/)
  const both = model.sessionsIcs([a, b], s => `https://p.example/#/s/${s.d}`)
  assert.equal(both.match(/BEGIN:VEVENT/g).length, 2)
  assert.match(both, /URL:https:\/\/p.example\/#\/s\/osv-room-x-2\r\n/)
})

test("search: every word, case and accents ignored; starting soon: mine only", () => {
  const s = sessionFromEvent(slot("osv-room-x-1", "Café des logiciels libres", 1800000000, 1, [["t", "room-x"]]), PROGRAMME)
  assert.ok(model.sessionMatches(s, "cafe LIBRES"))
  assert.ok(model.sessionMatches(s, "madeleine")) // the place
  assert.ok(!model.sessionMatches(s, "cafe rust"))
  assert.ok(!model.sessionMatches(s, "  "))
  // word starts only, links left out: "ch" found a room booking through its note's
  // "discord.com/channels/…" link and "CHT" amount
  const booking = sessionFromEvent(
    { ...slot("osv-room-angel-9", "Impro - Kmeleons", 1800000000, 1, [["t", "room-angel"]]), content: "Booked by X for 1.00 xCHT\nhttps://discord.com/channels/1/2" },
    PROGRAMME,
  )
  assert.ok(!model.sessionMatches(booking, "ch"))
  assert.ok(model.sessionMatches(booking, "kmel"))
  assert.ok(model.sessionMatches(booking, "impro-kmeleons"))
  assert.ok(model.sessionMatches(s, "logi"))
  assert.ok(!model.sessionMatches(s, "ogiciel"))
  const sk = generateSecretKey()
  const me = getPublicKey(sk)
  const yes = finalizeEvent({ ...rsvpTemplate(s, "accepted", me), created_at: 10 }, sk)
  assert.deepEqual(model.upcomingFor([s], [yes], me, s.start - 20 * 60).map(x => x.d), ["osv-room-x-1"])
  assert.equal(model.upcomingFor([s], [yes], me, s.start - 60 * 60).length, 0, "too early")
  assert.equal(model.upcomingFor([s], [yes], me, s.end).length, 0, "over")
  assert.equal(model.upcomingFor([s], [], me, s.start).length, 0, "not going")
})

test("What's new: the polls key's notes (a poll opened, its result), not someone else's look-alike", () => {
  const pollsSk = generateSecretKey()
  const pollsKey = getPublicKey(pollsSk)
  const tags = [["subject", "Pizza or pasta?"], ["t", "poll"], ["r", "https://vote.example/#/poll/abc"]]
  const opened = finalizeEvent({ kind: 1, created_at: 1, tags, content: "New poll: Pizza or pasta?" }, pollsSk)
  assert.deepEqual(activityFromEvent(opened, { pollsKey }), { app: "vote", text: "opened a poll", detail: "Pizza or pasta?", url: "https://vote.example/#/poll/abc" })
  const result = finalizeEvent({ kind: 1, created_at: 2, tags: [["e", opened.id, "", "root"], ["subject", "Result: Pizza or pasta?"], ...tags.slice(1)], content: "Result" }, pollsSk)
  assert.equal(activityFromEvent(result, { pollsKey }).text, "published a result")
  assert.equal(activityFromEvent(result, { pollsKey }).detail, "Pizza or pasta?")
  const fake = finalizeEvent({ kind: 1, created_at: 1, tags, content: "New poll" }, generateSecretKey())
  assert.equal(activityFromEvent(fake, { pollsKey }).app, "chat")
})

test("edit: keeps the tags the form does not cover", () => {
  const sk = generateSecretKey()
  const start = 1800000000
  const made = at(sk, 31923, [
    ["d", "cal-1"], ["title", "Old"], ["start", String(start)], ["end", String(start + 3600)], ["location", "Garden"],
    ["summary", "Short"], ["image", "https://img.example/a.png"], ["p", "ab".repeat(32)], ["t", "rust"],
    ["r", "https://first.example"], ["r", "https://second.example"],
  ])
  const s = sessionFromEvent(made, PROGRAMME)
  const t = model.sessionTemplate({ d: s.d, title: "New", start, end: start + 7200, location: "Café", link: "https://new.example", keep: s.tags })
  const get = name => t.tags.filter(x => x[0] === name).map(x => x[1])
  assert.deepEqual(get("title"), ["New"])
  assert.deepEqual(get("location"), ["Café"])
  assert.deepEqual(get("end"), [String(start + 7200)])
  assert.deepEqual(get("summary"), ["Short"])
  assert.deepEqual(get("image"), ["https://img.example/a.png"])
  assert.deepEqual(get("p"), ["ab".repeat(32)])
  assert.deepEqual(get("t").sort(), ["osv", "rust"])
  assert.deepEqual(get("r"), ["https://new.example", "https://second.example"])
  assert.deepEqual(get("d"), ["cal-1"])
})
