import { finalizeEvent, generateSecretKey, getPublicKey, nip44, type EventTemplate, type NostrEvent } from 'nostr-tools'
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
  email?: string
  membership: Membership
}

export interface Signer {
  pubkey: string
  sign(t: EventTemplate): Promise<NostrEvent>
  // NIP-44 with the user's key: gift-wrapped requests and the admins' DMs (access.ts)
  encrypt(peer: string, text: string): Promise<string>
  decrypt(peer: string, payload: string): Promise<string>
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
    }
  }
  if (!s.bunkerUrl) throw new Error('Not signed in.')
  const bp = await parseBunkerInput(s.bunkerUrl)
  if (!bp) throw new Error('Belfry sent a signer link this app does not understand.')
  const bunker = new BunkerSigner(hexToBytes(s.clientSecret), bp)
  // The pubkey is cached so the app opens offline; the bunker is reached only to sign.
  let connected: Promise<void> | null = null
  const ensure = () => (connected ??= bunker.connect().catch((e) => { connected = null; throw e }))
  let pubkey = await kvGet<string>('pubkey')
  if (!pubkey) {
    await ensure()
    pubkey = await bunker.getPublicKey()
    await kvSet('pubkey', pubkey)
  }
  const pk = pubkey
  return {
    pubkey: pk,
    async sign(t) {
      await ensure()
      return bunker.signEvent({ ...t, pubkey: pk })
    },
    async encrypt(peer, text) {
      await ensure()
      return bunker.nip44Encrypt(peer, text)
    },
    async decrypt(peer, payload) {
      await ensure()
      return bunker.nip44Decrypt(peer, payload)
    },
  }
}
