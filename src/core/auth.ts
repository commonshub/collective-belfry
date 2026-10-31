import { finalizeEvent, generateSecretKey, getPublicKey, type EventTemplate, type NostrEvent } from 'nostr-tools'
import { BunkerSigner, parseBunkerInput } from 'nostr-tools/nip46'
import { bytesToHex, hexToBytes } from '@noble/hashes/utils'
import { BELFRY, type Backend } from '../config'
import { kvDel, kvGet, kvSet } from './db'

export interface Membership {
  status: 'active' | 'pending' | 'revoked'
  pass?: string // e.g. "Two weeks, 25 Jan – 5 Feb"
  member?: string // e.g. "OSV-0042"
}

export interface Session {
  backend: Backend
  email: string
  token?: string // Belfry app token (belfry backend)
  bunkerUrl?: string // NIP-46 bunker for signing (belfry backend, once active)
  clientSecret: string // hex, this device's key for talking to the bunker
  demoSecret?: string // hex, the user's key in demo mode (never leaves the device)
  membership: Membership
}

export interface Signer {
  pubkey: string
  sign(t: EventTemplate): Promise<NostrEvent>
}

export interface AuthDriver {
  requestCode(email: string): Promise<void>
  verifyCode(email: string, code: string): Promise<Session>
  refreshMembership(s: Session): Promise<Membership>
  redeemInvite(s: Session, code: string): Promise<Session>
}

// ---- demo: everything stays on this device ---------------------------------

const demo: AuthDriver = {
  async requestCode() {
    await new Promise((r) => setTimeout(r, 400))
  },
  async verifyCode(email, code) {
    if (!/^\d{6}$/.test(code)) throw new Error('Enter the 6 digits from the e-mail.')
    const prev = await kvGet<Session>('session:' + email.toLowerCase())
    return (
      prev ?? {
        backend: 'demo',
        email,
        clientSecret: bytesToHex(generateSecretKey()),
        demoSecret: bytesToHex(generateSecretKey()),
        membership: { status: 'pending' },
      }
    )
  },
  async refreshMembership(s) {
    return s.membership
  },
  async redeemInvite(s, code) {
    if (code.trim().length < 4) throw new Error('That invite code looks too short.')
    const next: Session = { ...s, membership: { status: 'active', pass: 'Two weeks · 25 Jan – 5 Feb 2027', member: 'OSV-DEMO' } }
    await kvSet('session:' + s.email.toLowerCase(), next)
    return next
  },
}

// ---- belfry -------------------------------------------------------------------
// These endpoints don't exist on Belfry yet: they are what this app needs from it
// (see BELFRY-API.md). Belfry's own apps use the browser OAuth flow at
// /api/oauth/authorize instead, which can't show our own e-mail and code screens.

async function api<T>(path: string, body?: unknown, token?: string): Promise<T> {
  const res = await fetch(BELFRY.apps + path, {
    method: body === undefined ? 'GET' : 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: 'Bearer ' + token } : {}),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  const data = (await res.json().catch(() => ({}))) as T & { error?: string }
  if (!res.ok) throw new Error(data.error || `Belfry said ${res.status}`)
  return data
}

type AppSession = { token: string; bunker_url?: string; membership: Membership }

const belfry: AuthDriver = {
  async requestCode(email) {
    await api('/api/app/login/start', { email, client_id: 'osv-village' })
  },
  async verifyCode(email, code) {
    const clientSecret = bytesToHex(generateSecretKey())
    const r = await api<AppSession>('/api/app/login/verify', {
      email,
      code,
      client_id: 'osv-village',
      client_pubkey: getPublicKey(hexToBytes(clientSecret)),
    })
    return { backend: 'belfry', email, token: r.token, bunkerUrl: r.bunker_url, clientSecret, membership: r.membership }
  },
  async refreshMembership(s) {
    return (await api<{ membership: Membership }>('/api/app/membership', undefined, s.token)).membership
  },
  async redeemInvite(s, code) {
    const r = await api<AppSession>('/api/app/invite', { code }, s.token)
    return { ...s, bunkerUrl: r.bunker_url ?? s.bunkerUrl, membership: r.membership }
  },
}

export function driver(b: Backend): AuthDriver {
  return b === 'belfry' ? belfry : demo
}

// ---- session + signer ---------------------------------------------------------

export const loadSession = () => kvGet<Session>('session')
export async function saveSession(s: Session) {
  await kvSet('session', s)
  if (s.backend === 'demo') await kvSet('session:' + s.email.toLowerCase(), s)
}
export const clearSession = () => kvDel('session')

export async function makeSigner(s: Session): Promise<Signer> {
  if (s.demoSecret) {
    const sk = hexToBytes(s.demoSecret)
    return { pubkey: getPublicKey(sk), sign: async (t) => finalizeEvent(t, sk) }
  }
  if (!s.bunkerUrl) throw new Error('No signer yet: your pass is not active.')
  const bp = await parseBunkerInput(s.bunkerUrl)
  if (!bp) throw new Error('Belfry sent a signer link this app does not understand.')
  const bunker = new BunkerSigner(hexToBytes(s.clientSecret), bp)
  // The pubkey is cached per session so the app can render offline; the bunker
  // itself is only reached when something needs signing.
  let pubkey = await kvGet<string>('pubkey')
  let connected: Promise<void> | null = null
  const ensure = () => (connected ??= bunker.connect().catch((e) => { connected = null; throw e }))
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
