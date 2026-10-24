// The participant's signer (Keycast over NIP-46) and the voting identity on this device.
//
// The Semaphore identity never leaves the device: Keycast holds the participant's managed Nostr
// key, so an identity backed up encrypted to that key could be decrypted by Belfry and would link
// votes to people. Moving to another device is an explicit export code.
import { Identity } from "@semaphore-protocol/core"
import { BunkerSigner, parseBunkerInput } from "nostr-tools/nip46"
import { bytesToHex, hexToBytes } from "nostr-tools/utils"
import { generateSecretKey } from "nostr-tools/pure"

const SESSION_KEY = "belfry-vote-session"
const identityKey = pubkey => `belfry-vote-identity:${pubkey}`

let signer = null
let pubkey = null

function load(key) {
  try {
    return JSON.parse(localStorage.getItem(key) || "null")
  } catch {
    return null
  }
}

export function currentPubkey() {
  return pubkey
}

/** Restore a signer from an earlier sign-in on this device (no new connect needed). */
export function restore() {
  const saved = load(SESSION_KEY)
  if (!saved?.bp || !saved?.sk || !saved?.pubkey) return false
  signer = BunkerSigner.fromBunker(hexToBytes(saved.sk), saved.bp, { onauth: url => window.open(url, "_blank") })
  pubkey = saved.pubkey
  return true
}

/** Connect with a fresh bunker URL (from the sign-in) and remember the session. */
export async function connect(bunkerUrl) {
  const bp = await parseBunkerInput(bunkerUrl)
  if (!bp) throw new Error("not a bunker URL")
  const sk = generateSecretKey()
  const s = BunkerSigner.fromBunker(sk, bp, { onauth: url => window.open(url, "_blank") })
  await s.connect()
  const pk = await s.getPublicKey()
  // the secret is single-use; reconnecting later goes by the client key
  localStorage.setItem(SESSION_KEY, JSON.stringify({ bp: { ...bp, secret: null }, sk: bytesToHex(sk), pubkey: pk }))
  signer = s
  pubkey = pk
  return pk
}

export async function signOut() {
  try {
    await signer?.close()
  } catch {
    /* ignore */
  }
  signer = null
  pubkey = null
  localStorage.removeItem(SESSION_KEY)
}

export function sign(template) {
  if (!signer) throw new Error("not signed in")
  return signer.signEvent(template)
}

export function identity() {
  if (!pubkey) return null
  const saved = localStorage.getItem(identityKey(pubkey))
  return saved ? Identity.import(saved) : null
}

export function createIdentity() {
  const id = new Identity()
  localStorage.setItem(identityKey(pubkey), id.export())
  return id
}

/** Import an export code from another device; returns the identity or throws. */
export function importIdentity(code) {
  const id = Identity.import(code.trim())
  localStorage.setItem(identityKey(pubkey), id.export())
  return id
}

export function exportCode() {
  return identity()?.export() ?? null
}

const votedKey = id => `belfry-vote-voted:${pubkey}:${id}`
export const rememberVote = (pollId, option) => localStorage.setItem(votedKey(pollId), String(option))
export const rememberedVote = pollId => {
  const v = localStorage.getItem(votedKey(pollId))
  return v === null ? null : Number(v)
}
