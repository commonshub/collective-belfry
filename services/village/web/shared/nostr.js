// The community relay (NIP-42: reading and writing need AUTH as a participant, signed by the
// participant's signer), the participants list, profiles and Blossom uploads.
import { SimplePool } from "nostr-tools/pool"
import { nip19 } from "nostr-tools"
import { currentPubkey, sign } from "./session.js"
import { latestByAuthor, parseProfile } from "./model.js"

export const RELAY = import.meta.env.VITE_RELAY_URL
const BLOSSOM = (import.meta.env.VITE_BLOSSOM_URL || "").replace(/\/$/, "")
const authSigner = template => sign(template)
const pool = new SimplePool({ automaticallyAuth: () => authSigner })

/** Every event matching the filter until EOSE (or maxWait). */
export function query(filter, { maxWait = 8000 } = {}) {
  return new Promise(resolve => {
    const events = []
    pool.subscribeEose([RELAY], filter, {
      onevent: e => events.push(e),
      onclose: () => resolve(events),
      onauth: authSigner,
      maxWait,
    })
  })
}

/** Large author lists in chunks (relays cap filter sizes). */
export async function queryAuthors(filter, authors, chunk = 200) {
  const out = []
  for (let i = 0; i < authors.length; i += chunk) {
    out.push(...(await query({ ...filter, authors: authors.slice(i, i + chunk) })))
  }
  return out
}

export async function publish(template) {
  const event = await sign(template)
  await Promise.any(pool.publish([RELAY], event, { onauth: authSigner, maxWait: 8000 }))
  return event
}

async function sha256Hex(bytes) {
  const digest = await crypto.subtle.digest("SHA-256", bytes)
  return [...new Uint8Array(digest)].map(b => b.toString(16).padStart(2, "0")).join("")
}

/** A NIP-98 header for this app's API; with a body, its SHA-256 goes in the payload tag. */
export async function nip98(method, path, body = null) {
  const tags = [
    ["u", `${location.origin}${path}`],
    ["method", method],
  ]
  if (body) tags.push(["payload", await sha256Hex(new TextEncoder().encode(body))])
  const event = await sign({ kind: 27235, created_at: Math.floor(Date.now() / 1000), tags, content: "" })
  return `Nostr ${btoa(JSON.stringify(event))}`
}

let participantsCache = null
/** Keys of every active pass (from this app's server; only participants may ask). */
export async function participants() {
  if (participantsCache) return participantsCache
  const res = await fetch("/api/participants", { headers: { Authorization: await nip98("GET", "/api/participants") } })
  const body = await res.json().catch(() => ({}))
  if (!res.ok) {
    const err = new Error(body.error || `request failed (${res.status})`)
    err.status = res.status
    throw err
  }
  participantsCache = new Set(body.pubkeys)
  return participantsCache
}

const profiles = new Map()
/** Kind-0 profiles (cached per page load): Map pubkey -> parsed profile. */
export async function loadProfiles(pubkeys) {
  const missing = [...new Set(pubkeys)].filter(pk => !profiles.has(pk))
  if (missing.length) {
    const latest = latestByAuthor(await queryAuthors({ kinds: [0] }, missing))
    for (const pk of missing) profiles.set(pk, latest.has(pk) ? { ...parseProfile(latest.get(pk)), raw: latest.get(pk) } : null)
  }
  return new Map(pubkeys.map(pk => [pk, profiles.get(pk)]))
}
export const forgetProfile = pk => profiles.delete(pk)

export const npub = pk => nip19.npubEncode(pk)
export const isMe = pk => pk === currentPubkey()

/** Upload a file to Belfry's Blossom server (BUD-02, kind-24242 authorization); returns its URL. */
export async function uploadBlob(blob, content = "Upload a listing photo") {
  return (await uploadDescriptor(blob, content)).url
}

/** The same upload, returning Blossom's blob descriptor ({url, sha256, type, size}). */
export async function uploadDescriptor(blob, content) {
  if (!BLOSSOM) throw new Error("file uploads are not configured")
  const bytes = await blob.arrayBuffer()
  const x = await sha256Hex(bytes)
  const now = Math.floor(Date.now() / 1000)
  const auth = await sign({
    kind: 24242,
    created_at: now,
    tags: [
      ["t", "upload"],
      ["x", x],
      ["expiration", String(now + 300)],
    ],
    content,
  })
  const res = await fetch(`${BLOSSOM}/upload`, {
    method: "PUT",
    headers: { Authorization: `Nostr ${btoa(JSON.stringify(auth))}`, "Content-Type": blob.type || "application/octet-stream" },
    body: bytes,
  })
  const body = await res.json().catch(() => ({}))
  if (!res.ok || !body.url) throw new Error(res.headers.get("x-reason") || body.message || `upload failed (${res.status})`)
  return { ...body, sha256: body.sha256 || x, type: body.type || blob.type, size: body.size ?? bytes.byteLength }
}

/** Shrink a photo to at most `max` pixels on its long side, as JPEG (which also drops EXIF and GPS). */
export async function shrinkImage(file, max = 1600) {
  return (await shrinkImageWithSize(file, max)).blob
}

/** shrinkImage plus the result's width and height (for NIP-68's "dim"). */
export async function shrinkImageWithSize(file, max = 1600) {
  const img = await createImageBitmap(file, { imageOrientation: "from-image" })
  const scale = Math.min(1, max / Math.max(img.width, img.height))
  const canvas = document.createElement("canvas")
  canvas.width = Math.round(img.width * scale)
  canvas.height = Math.round(img.height * scale)
  canvas.getContext("2d").drawImage(img, 0, 0, canvas.width, canvas.height)
  const blob = await new Promise(resolve => canvas.toBlob(resolve, "image/jpeg", 0.85))
  return { blob, width: canvas.width, height: canvas.height }
}
