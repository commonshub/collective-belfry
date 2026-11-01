import { finalizeEvent, generateSecretKey, getPublicKey, type EventTemplate, type NostrEvent } from 'nostr-tools'
import { BunkerSigner, parseBunkerInput } from 'nostr-tools/nip46'
import { sha256 } from '@noble/hashes/sha256'
import { bytesToHex, hexToBytes } from '@noble/hashes/utils'
import { BELFRY, type Backend } from '../config'
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
  location.assign(`${BELFRY.apps}/api/oauth/authorize?${q}`)
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
  const res = await fetch(`${BELFRY.apps}/api/oauth/token`, {
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
    await fetch(`${BELFRY.apps}/api/oauth/revoke`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token: s.refreshToken, client_id: CLIENT_ID }),
      signal: AbortSignal.timeout(3000),
    }).catch(() => { /* offline: the token expires by itself */ })
  }
  location.assign(`${BELFRY.apps}/api/auth/oidc/logout?return_to=${encodeURIComponent(redirectUri())}`)
}

// ---- where the key stands: GET /api/me (NIP-98) ------------------------------------
// Belfry only answers browsers from origins in its API_CORS_ORIGINS. When ours is not
// listed the request fails and the app falls back to what the relays say (they only
// let in keys with an active pass): see sync.ts.

type Me = { member: boolean; nip05?: string; email?: string | null; passes?: { state: string; tier: string; date_start: string; date_end: string }[] }

export async function fetchMembership(signer: Signer): Promise<Membership | null> {
  const url = `${BELFRY.pass}/api/me`
  try {
    const auth = await signer.sign({
      kind: 27235, content: '', created_at: Math.floor(Date.now() / 1000),
      tags: [['u', url], ['method', 'GET']],
    })
    const res = await fetch(url, { headers: { Authorization: 'Nostr ' + btoa(JSON.stringify(auth)) } })
    if (!res.ok) return null
    const me = (await res.json()) as Me
    const p = me.passes?.find((x) => x.state === 'active') ?? me.passes?.[0]
    return {
      status: me.member ? 'active' : 'pending',
      pass: p ? `${p.tier} pass · ${p.date_start} → ${p.date_end}` : undefined,
      nip05: me.nip05,
    }
  } catch {
    return null // CORS, offline or bunker unreachable
  }
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
    return { pubkey: getPublicKey(sk), sign: async (t) => finalizeEvent(t, sk) }
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
  }
}
