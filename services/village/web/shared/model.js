// Event formats shared by the directory and the marketplace (no browser APIs: the tests import it).
//
// Directory consent: kind 30078 (NIP-78 app data) with d = "osv-directory" and content
// {"listed": true}; the latest one per author counts, nobody is listed without one.
// Listings: NIP-99 classifieds, kind 30402, priced in village tokens: ["price", "<n>", "OSV"].
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
    images,
    createdAt: event.created_at,
  }
}

/** The unsigned kind-20 event for a photo post (`images`: {url, m, x, width, height, alt}). */
export function photoTemplate({ title = "", caption = "", hashtags = [], location = "", images }) {
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
export function activityFromEvent(event) {
  switch (event.kind) {
    case 1:
      if (event.tags.some(t => t[0] === "e")) return null // replies stay in the chat's threads
      return { app: "chat", text: "posted a note", detail: firstLine(event.content) }
    case 20: {
      const p = photoFromEvent(event)
      if (!p) return null
      return { app: "photos", text: p.images.length > 1 ? `shared ${p.images.length} photos` : "shared a photo", detail: firstLine(p.title || p.caption), image: p.images[0].url }
    }
    case 1111:
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
