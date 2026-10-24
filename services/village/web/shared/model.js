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
