// NIP-98 HTTP auth: `Authorization: Nostr <base64 kind-27235 event>` signed by the participant's
// key (through Keycast), bound to the absolute URL, the method and the SHA-256 of the raw body.
import { createHash } from "node:crypto"
import { verifyEvent } from "nostr-tools/pure"

const MAX_SKEW_S = 60
const seen = new Map() // event id -> expiry (ms); one use per token

export class AuthError extends Error {}

function tag(event, name) {
  return event.tags.find(t => t[0] === name)?.[1]
}

export function sha256Hex(bytes) {
  return createHash("sha256").update(bytes).digest("hex")
}

/** Returns the signer's pubkey (hex) or throws AuthError. */
export function verifyNip98(header, { url, method, body, now = Date.now() }) {
  if (!header || !header.startsWith("Nostr ")) throw new AuthError("missing Nostr authorization")
  let event
  try {
    event = JSON.parse(Buffer.from(header.slice(6).trim(), "base64").toString("utf8"))
  } catch {
    throw new AuthError("unreadable authorization event")
  }
  if (event?.kind !== 27235) throw new AuthError("wrong event kind")
  if (!verifyEvent(event)) throw new AuthError("bad signature")
  if (Math.abs(now / 1000 - event.created_at) > MAX_SKEW_S) throw new AuthError("expired")
  if (tag(event, "u") !== url) throw new AuthError("wrong url")
  if ((tag(event, "method") || "").toUpperCase() !== method.toUpperCase()) {
    throw new AuthError("wrong method")
  }
  if (body && body.length > 0 && tag(event, "payload") !== sha256Hex(body)) {
    throw new AuthError("payload hash does not match")
  }
  for (const [id, exp] of seen) if (exp < now) seen.delete(id)
  if (seen.has(event.id)) throw new AuthError("authorization already used")
  seen.set(event.id, now + 2 * MAX_SKEW_S * 1000)
  return event.pubkey
}
