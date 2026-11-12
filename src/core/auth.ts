import { finalizeEvent, generateSecretKey, getPublicKey, nip04, nip44, type EventTemplate, type NostrEvent } from 'nostr-tools'
import { BunkerSigner, parseBunkerInput } from 'nostr-tools/nip46'
import { sha256 } from '@noble/hashes/sha256'
import { bytesToHex, hexToBytes } from '@noble/hashes/utils'
import { BELFRY, SIGNIN, type Backend } from '../config'
import { kvDel, kvGet, kvSet } from './db'

export interface Membership {
  status: 'active' | 'pending' | 'unknown'
  pass?: string // e.g. "week · 2027-01-25 → 2027-01-31"
  nip05?: string
}

export interface Session {
  backend: Backend
  bunkerUrl?: string // NIP-46 bunker from Keycast ("Belfry event apps")
  refreshToken?: string
  clientSecret: string // hex: this device's key for the bunker, kept and reused
  demoSecret?: string // hex: the user's key in demo mode (never leaves the device)
  pubkey?: string // hex: whose key the bunker signs for, when the belfry said so (a guest link)
  email?: string
  membership: Membership
}

export interface Signer {
  pubkey: string
  sign(t: EventTemplate): Promise<NostrEvent>
  // NIP-44 with the user's key: gift-wrapped requests and the admins' DMs (access.ts)
  encrypt(peer: string, text: string): Promise<string>
  decrypt(peer: string, payload: string): Promise<string>
  // NIP-04, only to read old direct messages (kind 4)
  decrypt04?(peer: string, payload: string): Promise<string>
}

// ---- "Sign in with Open Source Village" ----------------------------------------
// OAuth 2.0 code + PKCE against Keycast (Belfry skill, section 9). Belfry's sign-in page
// asks for the pass's e-mail address and the mailed 6-digit code; the token response
// carries a bunker:// URL that signs for the participant's key. Any client_id works.

const CLIENT_ID = 'osv-village-app'
const SCOPE = 'policy:belfry-event'
const FLOW_KEY = 'osv-oauth-flow'

const b64url = (bytes: Uint8Array) =>
  btoa(String.fromCharCode(...bytes)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')

// the app's own page, without query or hash: Keycast must get exactly the same value twice
const redirectUri = () => location.origin + location.pathname

export async function startSignIn(silent = false) {
  const flow = {
    state: b64url(crypto.getRandomValues(new Uint8Array(24))),
    verifier: b64url(crypto.getRandomValues(new Uint8Array(48))),
  }
  // localStorage, not sessionStorage: on iPhone the sign-in may come back in another tab
  localStorage.setItem(FLOW_KEY, JSON.stringify(flow))
  const challenge = b64url(new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(flow.verifier))))
  const q = new URLSearchParams({
    client_id: CLIENT_ID, redirect_uri: redirectUri(), scope: SCOPE,
    state: flow.state, code_challenge: challenge, code_challenge_method: 'S256',
  })
  if (silent) q.set('prompt', 'none')
  location.assign(`${SIGNIN.authorize}?${q}`)
}

/** Back from Keycast with ?code&state (or ?error): returns a session, or null if this
 *  page load is not a sign-in callback. */
export async function finishSignIn(): Promise<Session | null> {
  const q = new URLSearchParams(location.search)
  if (!q.has('code') && !q.has('error')) return null
  const flow = JSON.parse(localStorage.getItem(FLOW_KEY) ?? 'null') as { state: string; verifier: string } | null
  localStorage.removeItem(FLOW_KEY)
  history.replaceState(null, '', redirectUri() + location.hash)
  if (!flow || q.get('state') !== flow.state) throw new Error('This sign-in did not start here. Please try again.')
  const err = q.get('error')
  if (err) throw new Error(err === 'access_denied' ? 'Sign-in was cancelled.' : `Not signed in (${err}).`)
  const res = await fetch(SIGNIN.token, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      grant_type: 'authorization_code', code: q.get('code'), client_id: CLIENT_ID,
      redirect_uri: redirectUri(), code_verifier: flow.verifier,
    }),
  })
  const body = (await res.json().catch(() => ({}))) as { bunker_url?: string; refresh_token?: string; error?: string }
  if (!res.ok || !body.bunker_url) throw new Error(`Not signed in: ${body.error ?? 'token ' + res.status}`)
  await kvDel('pubkey')
  return {
    backend: 'belfry',
    bunkerUrl: body.bunker_url,
    refreshToken: body.refresh_token,
    clientSecret: bytesToHex(generateSecretKey()),
    membership: { status: 'unknown' },
  }
}

// ---- joining with a guest link (Belfry guests.py) ----------------------------------------------
// The link is chat.<domain>/join/<token> (the proxy turns it into #/join/<token>). The browser
// that will hold the session is the one that asks: one signed request (NIP-98), no code.
//   signed in        → signed with the account: it gets the channel (joinWithAccount, sync.ts)
//   not signed in    → this device makes its session key and signs with it, no name: the belfry
//                      makes a nameless guest and answers with its session (joinAsNew). They
//                      read the channel in guest mode (guestmode.ts) until they join with a name
//                      and an e-mail (guestjoin.ts)
const GUEST_KEY = 'belfry-guest'
const INVITE_KEY = 'belfry-invite'
const JOIN_KEY = 'belfry-join-key'
export interface GuestInfo { channel: string; saved: boolean; /** guest mode: only opened a link so far, no name: reads, sends nothing */ reading?: boolean }
export interface InviteInfo { community: string; channel: string; channel_name: string; about: string; inviter: string; state: string; days: number }

/** The invite waiting to be used (kept while someone signs in first). */
export const pendingInvite = (): string => { try { return localStorage.getItem(INVITE_KEY) ?? '' } catch { return '' } }
export const setPendingInvite = (token: string) => {
  try { if (token) localStorage.setItem(INVITE_KEY, token); else localStorage.removeItem(INVITE_KEY) } catch { /* private mode */ }
}
/** #/join/<token> in the address: remember the invite and clean the address. */
export function takeInviteFromUrl(): string {
  const m = /^#\/join\/([A-Za-z0-9_-]{20,})$/.exec(location.hash)
  if (!m) return ''
  history.replaceState(null, '', location.pathname + location.search)
  setPendingInvite(m[1])
  return m[1]
}

export async function inviteInfo(token: string): Promise<InviteInfo> {
  const res = await fetch(`${BELFRY.api}/channel-invites/${token}`)
  if (res.status === 404) throw new Error('This invite link is not known here. Ask for a new one.')
  if (!res.ok) throw new Error('The invite could not be read just now. Try again.')
  return (await res.json()) as InviteInfo
}

export const INVITE_CLOSED: Record<string, string> = {
  used: 'This invite was used already. Ask for a new one.',
  expired: 'This invite has expired. Ask for a new one.',
  revoked: 'This invite was withdrawn. Ask for a new one.',
}

/** What the join request can say (fixed messages by code; nothing from the address bar). */
const JOIN_SAID: Record<string, string> = {
  closed: 'This invite can’t be used any more. Ask for a new one.',
  for_agent: 'This link is for an AI agent, not a person.',
  unsupported: 'This belfry does not take guests.',
  name_needed: 'This belfry is being updated. Try again in a few minutes.',
}

/** A new guest: this device's new session key signs the request (no name: guest mode); the
 *  answer is the session and the guest's public key. */
export async function joinAsNew(token: string, retried = false): Promise<Session> {
  // one key per invite on this device, kept until the join succeeds: a retry or a reload signs
  // with the same key, and the belfry answers with the same account (never a second)
  let sk: Uint8Array | undefined
  try { const kept = JSON.parse(localStorage.getItem(JOIN_KEY) ?? 'null'); if (kept?.token === token && /^[0-9a-f]{64}$/.test(kept.sk)) sk = hexToBytes(kept.sk) } catch { /* none */ }
  if (!sk) {
    sk = generateSecretKey()
    try { localStorage.setItem(JOIN_KEY, JSON.stringify({ token, sk: bytesToHex(sk) })) } catch { /* private mode */ }
  }
  const url = `${BELFRY.api}/channel-invites/${token}/join`
  const text = '{}'
  const auth = finalizeEvent({
    kind: 27235, content: '', created_at: Math.floor(Date.now() / 1000),
    tags: [['u', url], ['method', 'POST'], ['payload', bytesToHex(sha256(new TextEncoder().encode(text)))]],
  }, sk)
  let res: Response
  try {
    res = await fetch(url, { method: 'POST', headers: { Authorization: 'Nostr ' + btoa(JSON.stringify(auth)), 'Content-Type': 'application/json' }, body: text })
  } catch { throw new Error('No connection to the belfry. Try again.') }
  const body = (await res.json().catch(() => ({}))) as { bunker_url?: string; pubkey?: string; channel?: string; error?: string; email_needed?: boolean; reading?: boolean }
  if (res.status === 409 && body.error === 'session_taken') {
    // this device's key is another account's in the signer: once more with a fresh key
    try { localStorage.removeItem(JOIN_KEY) } catch { /* private mode */ }
    if (!retried) return joinAsNew(token, true)
  }
  if (!res.ok || !body.bunker_url) throw new Error(JOIN_SAID[body.error ?? ''] ?? 'Joining did not work just now. Try again.')
  try { localStorage.removeItem(JOIN_KEY) } catch { /* private mode */ }
  setPendingInvite('')
  setGuestInfo({ channel: body.channel ?? 'general', saved: false, reading: body.reading !== false })
  // a co-host link (an event's): a host needs a proven e-mail; after joining with it, the link again
  setCoHostToken(body.email_needed ? token : '')
  if (body.channel) history.replaceState(null, '', `${location.pathname}${location.search}#/c/${encodeURIComponent(body.channel)}`)
  // the belfry said whose key the session signs for: the app opens without asking the signer first
  await kvDel('pubkey')
  return { backend: 'belfry', bunkerUrl: body.bunker_url, clientSecret: bytesToHex(sk), membership: { status: 'unknown' }, ...(body.pubkey && /^[0-9a-f]{64}$/.test(body.pubkey) ? { pubkey: body.pubkey } : {}) }
}

/** Where to land after signing in (an address that had an account already: guestjoin.ts). */
const OPEN_KEY = 'belfry-open-channel'
export const openAfterSignIn = (): string => { try { return localStorage.getItem(OPEN_KEY) ?? '' } catch { return '' } }
export const setOpenAfterSignIn = (channel: string) => { try { if (channel) localStorage.setItem(OPEN_KEY, channel); else localStorage.removeItem(OPEN_KEY) } catch { /* private mode */ } }

const COHOST_KEY = 'belfry-cohost-invite'
/** A co-host link used before saving an e-mail: used again once the e-mail is saved. */
export const coHostToken = (): string => { try { return localStorage.getItem(COHOST_KEY) ?? '' } catch { return '' } }
export const setCoHostToken = (t: string) => { try { if (t) localStorage.setItem(COHOST_KEY, t); else localStorage.removeItem(COHOST_KEY) } catch { /* private mode */ } }

export const guestInfo = (): GuestInfo | null => {
  try { return JSON.parse(localStorage.getItem(GUEST_KEY) ?? 'null') } catch { return null }
}
export const setGuestInfo = (g: GuestInfo | null) => {
  try { if (g) localStorage.setItem(GUEST_KEY, JSON.stringify(g)); else localStorage.removeItem(GUEST_KEY) } catch { /* private mode */ }
}

/** Ask the belfry to remove this device's session from the signer (NIP-98, signed by the session
 *  itself). Best effort: offline, or a session already gone, must not keep anyone signed in. */
export async function endSession(s: Session) {
  const bunker = /^bunker:\/\/([0-9a-f]{64})/i.exec(s.bunkerUrl ?? '')?.[1]?.toLowerCase()
  if (!bunker) return
  try {
    const { signedFetch } = await import('./sync')
    await Promise.race([
      signedFetch(`${BELFRY.api}/me/sessions/revoke`, 'POST', { bunker_pubkey: bunker }),
      new Promise((_, no) => setTimeout(() => no(new Error('timeout')), 5000)),
    ])
  } catch { /* the session expires by itself; signing out goes on */ }
}

/** Everything this app kept in the browser's own storage (join key, guest note, last channel,
 *  hidden pins, …): gone at sign-out, so the next person on this device starts clean. */
export function forgetDevice() {
  for (const store of [localStorage, sessionStorage]) {
    try {
      for (const k of Object.keys(store)) if (/^(belfry|osv)[-:]/.test(k)) store.removeItem(k)
    } catch { /* private mode */ }
  }
}

/** Ends this app's authorization and the sign-in for every village app, then comes back. */
export async function belfrySignOut(s: Session) {
  if (s.refreshToken) {
    await fetch(SIGNIN.revoke, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token: s.refreshToken, client_id: CLIENT_ID }),
      signal: AbortSignal.timeout(3000),
    }).catch(() => { /* offline: the token expires by itself */ })
  }
  location.assign(`${SIGNIN.logout}?return_to=${encodeURIComponent(redirectUri())}`)
}

// ---- demo (tests only: ?backend=demo) ------------------------------------------------

export function demoSession(email: string): Session {
  return {
    backend: 'demo', email,
    clientSecret: bytesToHex(generateSecretKey()),
    demoSecret: bytesToHex(sha256('osv-demo:' + email.toLowerCase())),
    membership: { status: 'active', pass: 'Demo pass' },
  }
}

// ---- session + signer -----------------------------------------------------------------

export const loadSession = () => kvGet<Session>('session')
export const saveSession = (s: Session) => kvSet('session', s)
export const clearSession = () => kvDel('session')

export async function makeSigner(s: Session): Promise<Signer> {
  if (s.demoSecret) {
    const sk = hexToBytes(s.demoSecret)
    const key = (peer: string) => nip44.v2.utils.getConversationKey(sk, peer)
    return {
      pubkey: getPublicKey(sk),
      sign: async (t) => finalizeEvent(t, sk),
      encrypt: async (peer, text) => nip44.v2.encrypt(text, key(peer)),
      decrypt: async (peer, payload) => nip44.v2.decrypt(payload, key(peer)),
      decrypt04: async (peer, payload) => nip04.decrypt(sk, peer, payload),
    }
  }
  if (!s.bunkerUrl) throw new Error('Not signed in.')
  const bp = await parseBunkerInput(s.bunkerUrl)
  if (!bp) throw new Error('Belfry sent a signer link this app does not understand.')
  // The connection to the signer is a WebSocket on the bunker relay: a phone that sleeps or
  // changes network can leave it dead without telling. Every request gets a timeout; on a
  // timeout the connection is rebuilt and the request fails (the outbox tries again).
  const secret = hexToBytes(s.clientSecret)
  let bunker = new BunkerSigner(secret, bp)
  let connected: Promise<void> | null = null
  const ensure = () => (connected ??= bunker.connect().catch((e) => { connected = null; throw e }))
  // A deploy restarts the proxy and every socket drops (Xavier, 2026-11-11: "very slow to send").
  // BunkerSigner never resubscribes after its socket closes: answers then go nowhere until a
  // timeout. So a dead socket is replaced before asking, and a request that times out is asked
  // once more on a fresh connection (signing and decrypting again is harmless).
  const TIMEOUT = 10_000
  const rebuild = () => {
    const old = bunker
    bunker = new BunkerSigner(secret, bp)
    connected = null
    void old.close().catch(() => { /* already gone */ })
  }
  const norm = (u: string) => u.replace(/\/+$/, '')
  const alive = () => {
    try {
      const st = (bunker as unknown as { pool: { listConnectionStatus(): Map<string, boolean> } }).pool.listConnectionStatus()
      return bp.relays.every((u) => [...st].some(([k, up]) => up && norm(k) === norm(u)))
    } catch { return true } // can't tell: ask, the timeout covers it
  }
  const once = async <T,>(fn: () => Promise<T>): Promise<T> => {
    let timer: ReturnType<typeof setTimeout> | undefined
    try {
      return await Promise.race([
        (async () => { await ensure(); return fn() })(),
        new Promise<never>((_, reject) => { timer = setTimeout(() => reject(new Error('timeout: the signer did not answer')), TIMEOUT) }),
      ])
    } finally {
      clearTimeout(timer)
    }
  }
  const call = async <T,>(fn: () => Promise<T>): Promise<T> => {
    if (connected && !alive()) rebuild()
    try {
      return await once(fn)
    } catch (e) {
      if (!(e instanceof Error && e.message.startsWith('timeout'))) throw e
      rebuild()
      return once(fn)
    }
  }
  // The pubkey is cached so the app opens offline; the bunker is reached only to sign.
  let pubkey = await kvGet<string>('pubkey')
  if (!pubkey && s.pubkey) { pubkey = s.pubkey; await kvSet('pubkey', pubkey) }
  if (!pubkey) {
    pubkey = await call(() => bunker.getPublicKey())
    await kvSet('pubkey', pubkey)
  }
  const pk = pubkey
  return {
    pubkey: pk,
    sign: (t) => call(() => bunker.signEvent({ ...t, pubkey: pk })),
    encrypt: (peer, text) => call(() => bunker.nip44Encrypt(peer, text)),
    decrypt: (peer, payload) => call(() => bunker.nip44Decrypt(peer, payload)),
    decrypt04: (peer, payload) => call(() => bunker.nip04Decrypt(peer, payload)),
  }
}
