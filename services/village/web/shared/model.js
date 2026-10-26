// Event formats shared by the directory and the marketplace (no browser APIs: the tests import it).
//
// Directory consent: kind 30078 (NIP-78 app data) with d = "osv-directory" and content
// {"listed": true}; the latest one per author counts, nobody is listed without one.
// Listings: NIP-99 classifieds, kind 30402, priced in village tokens: ["price", "<n>", "OSV"].
import { sha256 } from "@noble/hashes/sha2.js"
import { bytesToHex, utf8ToBytes } from "@noble/hashes/utils.js"
export const DIRECTORY_D = "osv-directory"
export const LISTING_KIND = 30402
export const CURRENCY = "OSV"
export const CATEGORIES = [
  ["stuff", "Things"],
  ["food", "Food and drink"],
  ["service", "Services and help"],
  ["skill", "Skills and workshops"],
  ["ride", "Rides"],
  ["other", "Other"],
]

const tag = (event, name) => event.tags.find(t => t[0] === name)?.[1]
const httpsUrl = v => (typeof v === "string" && /^https?:\/\//i.test(v) ? v : "")

/** The newest event per author (replaceable events as relays should keep them). */
export function latestByAuthor(events) {
  const out = new Map()
  for (const e of events) {
    const prev = out.get(e.pubkey)
    if (!prev || e.created_at > prev.created_at) out.set(e.pubkey, e)
  }
  return out
}

export function directoryConsent(event) {
  try {
    return JSON.parse(event?.content || "{}").listed === true
  } catch {
    return false
  }
}

/** A kind-0 profile as the apps show it; unsafe URLs are dropped. */
export function parseProfile(event) {
  let c = {}
  try {
    c = JSON.parse(event?.content || "{}") || {}
  } catch {
    c = {}
  }
  const str = v => (typeof v === "string" ? v.trim() : "")
  return {
    name: str(c.name),
    displayName: str(c.display_name) || str(c.displayName),
    about: str(c.about),
    picture: httpsUrl(c.picture),
    website: httpsUrl(c.website),
    nip05: str(c.nip05),
  }
}

export const profileName = (p, fallback) => p?.displayName || p?.name || p?.nip05?.split("@")[0] || fallback

/** A listing from a kind-30402 event, or null when it lacks a title or d tag. */
export function listingFromEvent(event) {
  const d = tag(event, "d")
  const title = tag(event, "title")
  if (!d || !title) return null
  const priceTag = event.tags.find(t => t[0] === "price")
  let price = null
  if (priceTag && /^\d{1,9}$/.test(priceTag[1] || "") && (priceTag[2] || CURRENCY).toUpperCase() === CURRENCY) {
    price = Number(priceTag[1])
  }
  const category = event.tags.find(t => t[0] === "t" && CATEGORIES.some(([k]) => k === t[1]))?.[1] || "other"
  return {
    id: event.id,
    pubkey: event.pubkey,
    d,
    coordinate: `${LISTING_KIND}:${event.pubkey}:${d}`,
    title,
    summary: tag(event, "summary") || "",
    description: event.content || "",
    price,
    category,
    location: tag(event, "location") || "",
    images: event.tags.filter(t => t[0] === "image").map(t => httpsUrl(t[1])).filter(Boolean),
    status: tag(event, "status") === "sold" ? "sold" : "active",
    publishedAt: Number(tag(event, "published_at")) || event.created_at,
    createdAt: event.created_at,
  }
}

/** The unsigned kind-30402 event for a listing. */
export function listingTemplate(l) {
  const tags = [
    ["d", l.d],
    ["title", l.title],
    ["published_at", String(l.publishedAt || Math.floor(Date.now() / 1000))],
    ["price", String(Math.max(0, Math.floor(l.price || 0))), CURRENCY],
    ["t", l.category || "other"],
    ["status", l.status === "sold" ? "sold" : "active"],
  ]
  if (l.summary) tags.push(["summary", l.summary])
  if (l.location) tags.push(["location", l.location])
  for (const url of l.images || []) tags.push(["image", url])
  return { kind: LISTING_KIND, created_at: Math.floor(Date.now() / 1000), tags, content: l.description || "" }
}

// Photos: NIP-68 picture events, kind 20. The pictures are imeta tags (url, m, x, dim, alt) of
// files on Belfry's Blossom server; the caption is the content, hashtags are "t" tags. Likes are
// NIP-25 reactions (kind 7), comments NIP-22 (kind 1111) on the picture, tips nutzaps (kind 9321)
// with the picture's id in an "e" tag.
export const PHOTO_KIND = 20
export const PHOTO_TYPES = ["image/jpeg", "image/png", "image/webp", "image/gif", "image/avif"]
export const MAX_PHOTOS = 8

const HASHTAG = /^[\p{L}\p{N}_-]{1,40}$/u

/** Hashtags from free text ("#garden sunset, #Night" -> ["garden", "sunset", "night"]). */
export function parseHashtags(text) {
  const out = []
  for (const raw of String(text || "").split(/[\s,]+/)) {
    const t = raw.replace(/^#+/, "").toLowerCase()
    if (HASHTAG.test(t) && !out.includes(t)) out.push(t)
  }
  return out.slice(0, 10)
}

/** The fields of one imeta tag ("imeta", "url https://…", "m image/jpeg", …). */
function imetaFields(t) {
  const f = {}
  for (const part of t.slice(1)) {
    const i = part.indexOf(" ")
    if (i > 0 && !(part.slice(0, i) in f)) f[part.slice(0, i)] = part.slice(i + 1)
  }
  return f
}

/** A photo post from a kind-20 event, or null when it has no usable picture. */
export function photoFromEvent(event) {
  if (event?.kind !== PHOTO_KIND) return null
  const images = event.tags
    .filter(t => t[0] === "imeta")
    .map(imetaFields)
    .filter(f => httpsUrl(f.url) && (!f.m || PHOTO_TYPES.includes(f.m)))
    .map(f => {
      const [w, h] = /^\d{1,5}x\d{1,5}$/.test(f.dim || "") ? f.dim.split("x").map(Number) : [0, 0]
      return { url: f.url, m: f.m || "", x: /^[0-9a-f]{64}$/.test(f.x || "") ? f.x : "", width: w, height: h, alt: f.alt || "" }
    })
  if (!images.length) return null
  return {
    id: event.id,
    pubkey: event.pubkey,
    title: tag(event, "title") || "",
    caption: event.content || "",
    hashtags: [...new Set(event.tags.filter(t => t[0] === "t" && HASHTAG.test(t[1] || "")).map(t => t[1].toLowerCase()))],
    location: tag(event, "location") || "",
    session: event.tags.find(t => t[0] === "a" && /^31923:[0-9a-f]{64}:.+/.test(t[1] || ""))?.[1] || "",
    images,
    createdAt: event.created_at,
  }
}

/** The unsigned kind-20 event for a photo post (`images`: {url, m, x, width, height, alt}). */
export function photoTemplate({ title = "", caption = "", hashtags = [], location = "", images, session = "" }) {
  if (!images?.length) throw new Error("a photo post needs at least one picture")
  const tags = []
  if (title) tags.push(["title", title])
  for (const img of images.slice(0, MAX_PHOTOS)) {
    const t = ["imeta", `url ${img.url}`]
    if (img.m) t.push(`m ${img.m}`)
    if (img.x) t.push(`x ${img.x}`)
    if (img.width && img.height) t.push(`dim ${img.width}x${img.height}`)
    if (img.alt) t.push(`alt ${img.alt}`)
    tags.push(t)
  }
  // NIP-68: the "m" and "x" tags let relays and clients filter by type and hash
  for (const m of new Set(images.map(i => i.m).filter(Boolean))) tags.push(["m", m])
  for (const img of images) if (img.x) tags.push(["x", img.x])
  for (const h of hashtags) tags.push(["t", h])
  if (location) tags.push(["location", location])
  // the programme session it was taken at (its kind-31923 coordinate)
  if (/^31923:[0-9a-f]{64}:.+/.test(session)) tags.push(["a", session])
  return { kind: PHOTO_KIND, created_at: Math.floor(Date.now() / 1000), tags, content: caption }
}

/** A like (NIP-25) of a photo. */
export const likeTemplate = photo => ({
  kind: 7,
  created_at: Math.floor(Date.now() / 1000),
  tags: [
    ["e", photo.id],
    ["p", photo.pubkey],
    ["k", String(PHOTO_KIND)],
  ],
  content: "+",
})

/** A top-level comment (NIP-22) on a photo. */
export const commentTemplate = (photo, text) => ({
  kind: 1111,
  created_at: Math.floor(Date.now() / 1000),
  tags: [
    ["E", photo.id, "", photo.pubkey],
    ["K", String(PHOTO_KIND)],
    ["P", photo.pubkey],
    ["e", photo.id, "", photo.pubkey],
    ["k", String(PHOTO_KIND)],
    ["p", photo.pubkey],
  ],
  content: text,
})

/** Tokens in a nutzap (kind 9321): the sum of its proofs' amounts. */
export function nutzapAmount(event) {
  return event.tags
    .filter(t => t[0] === "proof")
    .reduce((sum, t) => {
      try {
        const a = JSON.parse(t[1]).amount
        return sum + (Number.isSafeInteger(a) && a > 0 ? a : 0)
      } catch {
        return sum
      }
    }, 0)
}

/** Event ids the authors deleted (NIP-09 kind 5, "e" tags, only for their own events). */
export function deletedIds(deletions, eventsById) {
  const out = new Set()
  for (const d of deletions) {
    for (const t of d.tags) if (t[0] === "e" && eventsById.get(t[1])?.pubkey === d.pubkey) out.add(t[1])
  }
  return out
}

// Activity ("What's new" in the directory): the readable events of the apps, one line each.
// Left out on purpose: private messages and private calendar entries (encrypted), likes,
// profile edits, opting out of the directory, and token payments (who paid whom stays in the
// wallets and on the offer pages).
export const ACTIVITY_KINDS = [1, 20, 1111, 30023, 30078, 30402, 31922, 31923]
const firstLine = (s, max = 140) => {
  const line = String(s || "").trim().split("\n")[0]
  return line.length > max ? `${line.slice(0, max - 1)}…` : line
}

/** {app, text, detail, image, start} for an activity event, or null when it is not shown. */
export function activityFromEvent(event, { pollsKey = "" } = {}) {
  switch (event.kind) {
    case 1:
      // the polls app's notes (services/vote): a poll opened, a poll's result (a reply)
      if (pollsKey && event.pubkey === pollsKey && event.tags.some(t => t[0] === "t" && t[1] === "poll")) {
        const result = event.tags.some(t => t[0] === "e")
        const subject = (tag(event, "subject") || "").replace(/^Result: /, "")
        return { app: "vote", text: result ? "published a result" : "opened a poll", detail: subject, url: httpsUrl(tag(event, "r")) }
      }
      if (event.tags.some(t => t[0] === "e")) return null // replies stay in the chat's threads
      return { app: "chat", text: "posted a note", detail: firstLine(event.content) }
    case 20: {
      const p = photoFromEvent(event)
      if (!p) return null
      return { app: "photos", text: p.images.length > 1 ? `shared ${p.images.length} photos` : "shared a photo", detail: firstLine(p.title || p.caption), image: p.images[0].url }
    }
    case 1111:
      if (event.tags.some(t => t[0] === "K" && t[1] === "31923"))
        return { app: "programme", text: "commented on a session", detail: firstLine(event.content), session: event.tags.find(t => t[0] === "A")?.[1] }
      if (!event.tags.some(t => t[0] === "K" && t[1] === String(PHOTO_KIND))) return null
      return { app: "photos", text: "commented on a photo", detail: firstLine(event.content), photoId: event.tags.find(t => t[0] === "E")?.[1] }
    case 30023:
      return { app: "chat", text: "published an article", detail: firstLine(tag(event, "title") || event.content) }
    case 30078:
      if (tag(event, "d") !== DIRECTORY_D || !directoryConsent(event)) return null
      return { app: "people", text: "joined the directory", detail: "" }
    case LISTING_KIND: {
      const l = listingFromEvent(event)
      if (!l) return null
      const price = l.price === null ? "" : l.price === 0 ? " (free)" : ` for ${l.price} token${l.price === 1 ? "" : "s"}`
      return { app: "market", text: l.status === "sold" ? "marked an offer as gone" : `offers ${l.title}${price}`, detail: l.status === "sold" ? l.title : l.summary, image: l.images[0] || "", listing: l }
    }
    case 31922:
    case 31923: {
      const title = tag(event, "title") || tag(event, "name")
      if (!title) return null
      const start = tag(event, "start")
      return { app: "calendar", text: `added an event: ${title}`, detail: firstLine(tag(event, "location") || event.content), start: event.kind === 31923 ? Number(start) || 0 : start || "" }
    }
    default:
      return null
  }
}

// Moderation: what stewards hid after a report (event ids, offer coordinates, and pubkeys for
// directory entries). The content stays on the relay; the apps do not show it.
export const REPORT_REASONS = [
  ["spam", "Spam"],
  ["harassment", "Harassment or hate"],
  ["illegal", "Illegal content"],
  ["scam", "Scam or fraud"],
  ["other", "Something else"],
]

/** True when a steward hid this event: by id, by its addressable coordinate, or (directory
 * entries) by its author. */
export function isHidden(hidden, event) {
  if (!hidden?.size || !event) return false
  if (hidden.has(event.id)) return true
  if (event.kind >= 30000 && event.kind < 40000) {
    const d = event.tags?.find(t => t[0] === "d")?.[1]
    if (d !== undefined && hidden.has(`${event.kind}:${event.pubkey}:${d}`)) return true
  }
  return (event.kind === 0 || event.kind === 30078) && hidden.has(event.pubkey)
}

// Programme (programme.<domain>): NIP-52 time-based calendar events (kind 31923). The village
// programme and the room bookings come from Belfry's programme key (onboarding `sync-programme`,
// d = "osv-programme-…" / "osv-room-…", t = "programme" / "room-<slug>"); participants' own
// public events are shown next to them. A session is addressed by its coordinate
// "31923:<pubkey>:<d>", and everything else points at that: RSVPs (kind 31925, `a` tag, status
// accepted / tentative / declined, as Calendar by Form* writes them), comments (NIP-22 kind 1111
// with an `A` root), photos (kind 20 with an `a` tag) and tips (nutzaps with the coordinate as
// `ref`, which the wallet puts in an `a` tag).
export const SESSION_KIND = 31923
export const RSVP_KIND = 31925
export const RSVP_STATUSES = ["accepted", "tentative", "declined"]

/** The calendar a session belongs to: {id, title} from its `t` tags and location. */
export function sessionTrack(event, programmeKey) {
  const ts = event.tags.filter(t => t[0] === "t").map(t => t[1])
  if (event.pubkey === programmeKey) {
    if (ts.includes("programme")) return { id: "programme", title: "Village programme" }
    const room = ts.find(t => t?.startsWith("room-"))
    if (room) return { id: room, title: (tag(event, "location") || "").split(",")[0].trim() || room.slice(5) }
  }
  return { id: "community", title: "From participants" }
}

/** A session from a kind-31923 event, or null without a d tag, title or valid times. */
export function sessionFromEvent(event, programmeKey = "") {
  if (event?.kind !== SESSION_KIND) return null
  const d = tag(event, "d")
  const title = (tag(event, "title") || tag(event, "name") || "").trim()
  const start = Number(tag(event, "start"))
  let end = Number(tag(event, "end"))
  if (!d || !title || !Number.isSafeInteger(start) || start <= 0) return null
  if (!Number.isSafeInteger(end) || end < start) end = start
  return {
    id: event.id,
    pubkey: event.pubkey,
    d,
    coordinate: `${SESSION_KIND}:${event.pubkey}:${d}`,
    title,
    summary: tag(event, "summary") || "",
    description: event.content || "",
    location: tag(event, "location") || "",
    start,
    end,
    tzid: tag(event, "start_tzid") || "",
    links: event.tags.filter(t => t[0] === "r").map(t => httpsUrl(t[1])).filter(Boolean),
    image: httpsUrl(tag(event, "image")),
    track: sessionTrack(event, programmeKey),
    official: Boolean(programmeKey) && event.pubkey === programmeKey,
    createdAt: event.created_at,
    // all of them, so an edit keeps what this app does not show (sessionTemplate `keep`)
    tags: event.tags,
  }
}

/** The newest version per coordinate, without those their author deleted (kind 5 with `a` or
 * `e` tags, NIP-09; a deletion counts for versions up to its own time). */
export function currentSessions(events, deletions, programmeKey = "") {
  const newest = new Map()
  for (const e of events) {
    const s = sessionFromEvent(e, programmeKey)
    if (!s) continue
    const prev = newest.get(s.coordinate)
    if (!prev || s.createdAt > prev.createdAt) newest.set(s.coordinate, s)
  }
  for (const del of deletions) {
    for (const t of del.tags) {
      for (const [coord, s] of newest) {
        if (s.pubkey !== del.pubkey || del.created_at < s.createdAt) continue
        if ((t[0] === "a" && t[1] === coord) || (t[0] === "e" && t[1] === s.id)) newest.delete(coord)
      }
    }
  }
  return [...newest.values()].sort((a, b) => a.start - b.start || a.title.localeCompare(b.title))
}

/** The newest RSVP per participant for one session: Map pubkey -> status. */
export function rsvpsFor(events, coordinate) {
  const newest = new Map()
  for (const e of events) {
    if (e.kind !== RSVP_KIND || !e.tags.some(t => t[0] === "a" && t[1] === coordinate)) continue
    const prev = newest.get(e.pubkey)
    if (!prev || e.created_at > prev.created_at) newest.set(e.pubkey, e)
  }
  const out = new Map()
  for (const [pk, e] of newest) {
    const status = tag(e, "status")
    if (RSVP_STATUSES.includes(status)) out.set(pk, status)
  }
  return out
}

/** The RSVP's `d` as Calendar by Form* makes it (makeDTag: the first 30 hex digits of
 * SHA-256("<responder>:<author>:<event d>")), so an answer in either app replaces the other's. */
export const rsvpD = (me, session) => bytesToHex(sha256(utf8ToBytes(`${me}:${session.pubkey}:${session.d}`))).slice(0, 30)

/** The unsigned RSVP (kind 31925) to a session; one replaceable event per participant and session. */
export const rsvpTemplate = (session, status, me) => {
  if (!RSVP_STATUSES.includes(status)) throw new Error(`unknown RSVP status ${status}`)
  if (!/^[0-9a-f]{64}$/.test(me || "")) throw new Error("an RSVP needs the participant's key")
  return {
    kind: RSVP_KIND,
    created_at: Math.floor(Date.now() / 1000),
    tags: [
      ["d", rsvpD(me, session)],
      ["a", session.coordinate],
      ["e", session.id],
      ["p", session.pubkey],
      ["status", status],
    ],
    content: "",
  }
}

/** A top-level comment (NIP-22) on a session, addressed by its coordinate. */
export const sessionCommentTemplate = (session, text) => ({
  kind: 1111,
  created_at: Math.floor(Date.now() / 1000),
  tags: [
    ["A", session.coordinate],
    ["E", session.id, "", session.pubkey],
    ["K", String(SESSION_KIND)],
    ["P", session.pubkey],
    ["a", session.coordinate],
    ["e", session.id, "", session.pubkey],
    ["k", String(SESSION_KIND)],
    ["p", session.pubkey],
  ],
  content: text,
})

const icsText = s => String(s).replace(/\\/g, "\\\\").replace(/\n/g, "\\n").replace(/[,;]/g, m => `\\${m}`)
const icsTime = t => new Date(t * 1000).toISOString().replace(/[-:]/g, "").replace(/\.\d{3}/, "")

/** An iCalendar file with sessions (`urlOf(session)` names each one's link), each with a reminder
 * `remind` minutes before it starts (0: none), for "Add to my calendar". */
export function sessionsIcs(sessions, urlOf = () => "", now = Date.now(), remind = 15) {
  const lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Belfry//Open Source Village programme//EN", "X-WR-CALNAME:Open Source Village"]
  for (const session of sessions) {
    const url = urlOf(session)
    lines.push(
      "BEGIN:VEVENT",
      `UID:${session.coordinate.replace(/:/g, "-")}@opensourcevillage`,
      `DTSTAMP:${icsTime(Math.floor(now / 1000))}`,
      `DTSTART:${icsTime(session.start)}`,
      `DTEND:${icsTime(session.end > session.start ? session.end : session.start + 3600)}`,
      `SUMMARY:${icsText(session.title)}`,
    )
    if (session.location) lines.push(`LOCATION:${icsText(session.location)}`)
    const description = [session.description, url].filter(Boolean).join("\n\n")
    if (description) lines.push(`DESCRIPTION:${icsText(description)}`)
    if (url) lines.push(`URL:${url}`)
    if (remind > 0) lines.push("BEGIN:VALARM", "ACTION:DISPLAY", `DESCRIPTION:${icsText(session.title)}`, `TRIGGER:-PT${remind}M`, "END:VALARM")
    lines.push("END:VEVENT")
  }
  lines.push("END:VCALENDAR")
  return lines.join("\r\n") + "\r\n"
}

/** The calendar file for one session. */
export const sessionIcs = (session, url = "", now = Date.now(), remind = 15) => sessionsIcs([session], () => url, now, remind)

/** Unix time of a wall-clock date and time (`YYYY-MM-DD`, `HH:MM`) in a time zone. */
export function zonedTime(date, hhmm, tz) {
  const [y, mo, d] = date.split("-").map(Number)
  const [h, mi] = hhmm.split(":").map(Number)
  if (![y, mo, d, h, mi].every(Number.isInteger)) return NaN
  const wall = Date.UTC(y, mo - 1, d, h, mi) / 1000
  const parts = t =>
    Object.fromEntries(
      new Intl.DateTimeFormat("en-US", { timeZone: tz, hourCycle: "h23", year: "numeric", month: "numeric", day: "numeric", hour: "numeric", minute: "numeric" })
        .formatToParts(new Date(t * 1000))
        .map(p => [p.type, Number(p.value)]),
    )
  // the zone's offset at the guess, twice (once more across a daylight-saving change)
  let t = wall
  for (let i = 0; i < 2; i++) {
    const p = parts(t)
    t = wall - (Date.UTC(p.year, p.month - 1, p.day, p.hour, p.minute) / 1000 - t)
  }
  return t
}

/** The unsigned public session (NIP-52 kind 31923) a participant announces; `d` keeps an edit on
 * the same coordinate. New ones get `session-…` (never the programme key's `osv-programme-` or
 * `osv-room-`). */
export function sessionTemplate({ d = "", title, start, end, tzid = "", location = "", description = "", link = "", keep = [] }) {
  title = String(title || "").trim()
  if (!title) throw new Error("a session needs a title")
  if (!Number.isSafeInteger(start) || start <= 0) throw new Error("a session needs a start")
  if (!Number.isSafeInteger(end) || end <= start) throw new Error("the end must be after the start")
  if (end - start > 24 * 3600) throw new Error("a session lasts at most a day")
  const tags = [
    ["d", d || `session-${Math.floor(Date.now() / 1000).toString(36)}-${Math.random().toString(36).slice(2, 8)}`],
    ["title", title.slice(0, 200)],
    ["start", String(start)],
    ["end", String(end)],
  ]
  if (tzid) tags.push(["start_tzid", tzid], ["end_tzid", tzid])
  if (location.trim()) tags.push(["location", location.trim().slice(0, 200)])
  const url = httpsUrl(link.trim())
  if (url) tags.push(["r", url])
  // an edit keeps the tags the form does not cover (summary, image, hashtags, invitees, more links
  // from the calendar app); the form's link replaces the first `r`
  let firstLink = true
  for (const t of keep) {
    if (!Array.isArray(t) || EDITED_TAGS.has(t[0])) continue
    if (t[0] === "r" && firstLink) {
      firstLink = false
      continue
    }
    if (t[0] === "r" && t[1] === url) continue
    tags.push(t)
  }
  if (!tags.some(t => t[0] === "t" && t[1] === "osv")) tags.push(["t", "osv"])
  return { kind: SESSION_KIND, created_at: Math.floor(Date.now() / 1000), tags, content: String(description).trim().slice(0, 4000) }
}

const EDITED_TAGS = new Set(["d", "title", "name", "start", "end", "start_tzid", "end_tzid", "location"])

/** Cancelling a session: a deletion (NIP-09) of its coordinate and this version. */
export const sessionDeletionTemplate = session => ({
  kind: 5,
  created_at: Math.floor(Date.now() / 1000),
  tags: [
    ["a", session.coordinate],
    ["e", session.id],
    ["k", String(SESSION_KIND)],
  ],
  content: "cancelled by the host",
})

const fold = s =>
  String(s || "")
    .normalize("NFD")
    .replace(/\p{M}/gu, "")
    .toLowerCase()

/** Whether every word of the query starts a word of the session's title, description, place or
 * track (case and accents ignored; links left out). "ch" finds "Chess", not "CHT" inside a
 * booking note's "1.00 CHT" or "discord.com/channels/…". */
export function sessionMatches(session, query) {
  const words = fold(query).split(/[^\p{L}\p{N}]+/u).filter(Boolean)
  if (!words.length) return false
  const text = fold([session.title, session.summary, session.description, session.location, session.track?.title].join(" ")).replace(/\b(?:https?:\/\/|www\.)\S+/g, " ")
  const have = text.split(/[^\p{L}\p{N}]+/u).filter(Boolean)
  return words.every(w => have.some(h => h.startsWith(w)))
}

/** Sessions the participant is going to, might go to or hosts that are on now or start within
 * `soon` seconds: for the "Starting soon" note. */
export function upcomingFor(sessions, rsvps, me, t, soon = 30 * 60) {
  return sessions.filter(s => {
    if (s.end <= t || s.start > t + soon) return false
    if (s.pubkey === me) return true
    const a = rsvpsFor(rsvps, s.coordinate).get(me)
    return a === "accepted" || a === "tentative"
  })
}
