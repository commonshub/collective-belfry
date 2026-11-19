// Joining from guest mode (Belfry guests.py): a name and an e-mail, then the 6-digit code the
// belfry mails to that address. Both requests are signed with the guest's identity (NIP-98).
//   POST /guest/save         {"email", "name"}           → 202; a code goes out by e-mail
//   POST /guest/save/verify  {"email", "code", "name"}   → {"saved": true, "joined"}
// The right code puts name and address on the account; the channel gets the guest's 9000 with
// the name (the join line). 409 `taken`: the address is another account's here; for a guest who
// was only reading, that account got the channel ("moved") and this one is closed: sign in.
import { replaceHash } from './nav'
import { BELFRY } from '../config'
import { demoSession, guestInfo, setGuestInfo, type Session } from './auth'
import { bytesToHex } from '@noble/hashes/utils'
import { generateSecretKey, getPublicKey } from 'nostr-tools'
import { guestChanged } from './guestmode'
import { addEvents, all } from './store'
import { isLocalOnly, me, signedFetch } from './sync'
import { demoRoles, setDemoRole } from './roles'

export type JoinError = 'invalid' | 'name_needed' | 'too_many' | 'wrong_code' | 'expired' | 'taken' | 'not_guest' | 'key_taken' | 'signer' | 'offline' | 'failed'
export class JoinFailed extends Error {
  /** retried: the request went out more than once (an answer may have been lost on the way back) */
  constructor(public code: JoinError, public moved = false, public retried = false, /** a wrong code: how many tries are left (the belfry's tries_left) */ public triesLeft?: number, /** the HTTP status (a 422 invalid on verify = the session key) */ public status = 0) { super(code) }
}

/** The words for FAILED: a wrong code says how many tries are left when the belfry told
 *  (board onboarding/sign-in-code: "That code is not right. 3 tries left."). */
export function joinErrorText(f: JoinFailed): string {
  if (f.code === 'wrong_code' && f.triesLeft !== undefined) {
    if (f.triesLeft <= 0) return 'That code is not right, and no tries are left. Send a new one.'
    return `That code is not right. ${f.triesLeft} ${f.triesLeft === 1 ? 'try' : 'tries'} left.`
  }
  return JOIN_ERRORS[f.code]
}

/** What each answer means, in fixed words (never the server's text, never the address bar's). */
export const JOIN_ERRORS: Record<JoinError, string> = {
  invalid: 'That doesn’t look like an e-mail address.',
  name_needed: 'Your name is needed to join.',
  too_many: 'Too many codes for now. Try again in half an hour.',
  wrong_code: 'That code is not right. Check the e-mail and try again.',
  expired: 'That code has expired. Send a new one.',
  taken: 'This address already has an account here.',
  not_guest: 'This guest access has ended. Ask for a new invite.',
  key_taken: 'Signing in did not work. Send a new code.',
  signer: 'Signing in did not work just now. Send a new code.',
  offline: 'No connection. Try again.',
  failed: 'That did not work. Try again.',
}
const KNOWN = new Set(Object.keys(JOIN_ERRORS))

const wait = (ms: number) => new Promise((r) => setTimeout(r, ms))
const clean = (name: string) => name.trim().replace(/\s+/g, ' ').slice(0, 100)

async function post(path: string, body: unknown | (() => unknown)): Promise<Record<string, unknown>> {
  let r: Response | undefined
  let tries = 0
  // coming back from the mail app with the code, the phone may have dropped the connection (or
  // the signer's) while the page slept: try again for a few seconds before saying so (#issues
  // 2026-11-13, "Can't enter the 6-digit after join")
  for (let i = 0; ; i++) {
    try { tries++; r = await signedFetch(`${BELFRY.api}${path}`, 'POST', typeof body === 'function' ? body() : body); break } catch (e) {
      if (i >= 3) throw new JoinFailed(/timeout/i.test(String((e as Error)?.message ?? e)) ? 'failed' : 'offline')
      await wait(1000 * (i + 1))
    }
  }
  const d = (await r.json().catch(() => ({}))) as Record<string, unknown>
  const left = typeof d.tries_left === 'number' && Number.isInteger(d.tries_left) && d.tries_left >= 0 && d.tries_left < 100 ? d.tries_left : undefined
  if (!r.ok) throw new JoinFailed(KNOWN.has(String(d.error)) ? (d.error as JoinError) : 'failed', d.moved === true, tries > 1, left, r.status)
  return d
}


/** Ask for the code: it goes by e-mail to EMAIL. NAME is sent when the guest has none yet. */
export async function sendJoinCode(email: string, name?: string) {
  if (isLocalOnly()) { await wait(400); demoTries = 5; return } // demo: no mail; the code is 123456
  await post('/guest/save', { email: email.trim(), ...(name ? { name: clean(name) } : {}) })
}

let demoTries = 5 // demo: the belfry allows 5 tries per code

/** The code from the e-mail: the account is saved under that address (and name). */
export async function verifyJoinCode(email: string, code: string, name?: string, again = false): Promise<SignedIn | void> {
  const g = guestInfo()
  if (isLocalOnly()) { // demo: what the belfry would do
    await wait(400)
    if (email.trim().toLowerCase() === 'taken@example.org') throw new JoinFailed('taken', true)
    if (code !== '123456') { demoTries = Math.max(0, demoTries - 1); throw new JoinFailed('wrong_code', false, false, demoTries) }
    // an address with an account the belfry signs in (demo): signed in by the right code
    if (email.trim().toLowerCase() === 'member@example.org') return { session: demoSigned(email), address: email.trim(), open: g?.channel ?? null, moved: !!g?.reading }
    const hex = (n: number) => [...crypto.getRandomValues(new Uint8Array(n))].map((b) => b.toString(16).padStart(2, '0')).join('')
    if (g?.reading && name) addEvents([{ id: hex(32), pubkey: hex(32), sig: hex(64), kind: 9000, content: '', created_at: Math.floor(Date.now() / 1000),
      tags: [['h', g.channel], ['p', me(), 'member'], ['guest', ''], ['invited-by', hex(32)], ['name', clean(name)]] }])
  } else {
    let d: Record<string, unknown> = {}
    const started = Math.floor(Date.now() / 1000) - 5 // only what the belfry does from now on
    // a NEW session key for the account the address may belong to (the guest's key stays the
    // guest's: one key, one identity); its public key goes with the code (docs/guests.md, "An
    // address that has an account"). 422 invalid = that key was refused, the code not spent:
    // once more with another key.
    let sk = generateSecretKey()
    const body = () => verifyBody(email, code, name, getPublicKey(sk))
    try {
      try { d = await post('/guest/save/verify', body) } catch (e) {
        if (!(e instanceof JoinFailed && e.code === 'invalid' && e.status === 422)) throw e
        sk = generateSecretKey()
        try { d = await post('/guest/save/verify', body) } catch (e2) { throw e2 instanceof JoinFailed && e2.code === 'invalid' ? new JoinFailed('failed') : e2 }
      }
    } catch (e) {
      // the first try may have gone through and only its answer got lost: the code is used up
      // then, and the retry hears expired / wrong / not a guest. What the belfry did shows on the
      // relay (the saved label, or the join with the name): look before saying so.
      if (!(e instanceof JoinFailed && (e.retried || again) && ['expired', 'wrong_code', 'not_guest'].includes(e.code) && g && await savedOnRelay(g.channel, started))) throw e
    }
    if (d.signed_in === true) return signedInFrom(d, sk, email)
    if (d.member === true) return becameMember(g?.channel, undefined, email)
  }
  if (g?.member && isLocalOnly()) return becameMember(g.channel, g.member.role, email)
  if (g) setGuestInfo({ channel: g.channel, saved: true })
  guestChanged()
}

/** Did the belfry save this guest in CHANNEL (its label, or its join with a name)? Waits a few
 *  seconds for the relay to say so. */
async function savedOnRelay(channel: string, since: number): Promise<boolean> {
  for (let i = 0; i < 6; i++) {
    const mine = all(me()).filter((e) => e.created_at >= since && e.tags.some((t) => t[0] === 'h' && t[1] === channel) && e.tags.some((t) => t[0] === 'p' && t[1] === me()))
    if (mine.some((e) => (e.kind === 1985 && e.tags.some((t) => t[0] === 'L' && t[1] === 'belfry/guest') && e.tags.some((t) => t[0] === 'l' && (t[1] === 'saved' || t[1] === 'member')))
      || (e.kind === 9000 && e.tags.some((t) => t[0] === 'name') && !e.tags.some((t) => t[0] === 'reading')))) return true
    await wait(1000)
  }
  return false
}

/** A member link was used (roles.py): no guest any more. The session is the same key, now a
 *  member's; the app opens again so the relays and the belfry see it as one. */
function becameMember(channel?: string, demoRole?: string, email = '') {
  askNameNext(email) // board onboarding/member-link-name: how people will know them here
  setGuestInfo(null)
  guestChanged()
  if (isLocalOnly()) { demoRoles(demoRole === 'admin', []); if (demoRole) setDemoRole(demoRole); return }
  replaceHash(`#/c/${encodeURIComponent(channel ?? 'general')}`)
  location.reload()
}

/** Signed in as the account the address belongs to: the session to store (like any sign-in),
 *  the address to say, the channel to open (null: home). */
export interface SignedIn { session: Session; address: string; open: string | null; moved: boolean }

/** A signed_in answer (vectors/guest-signin.json): never stored unless bunker_url is a bunker
 *  address and pubkey a key; the address said is the nip05 only when it is name@<this domain>. */
export function signedInFrom(d: Record<string, unknown>, sk: Uint8Array, typed: string, domain = BELFRY.domain): SignedIn {
  const bunker = typeof d.bunker_url === 'string' ? d.bunker_url : ''
  const pk = typeof d.pubkey === 'string' ? d.pubkey : ''
  if (!/^bunker:\/\/[0-9a-f]{64}(\?|$)/.test(bunker) || bunker.length > 2000 || !/^[0-9a-f]{64}$/.test(pk)) throw new JoinFailed('failed')
  const nip05 = typeof d.nip05 === 'string' ? d.nip05.toLowerCase() : ''
  const own = /^[a-z0-9._-]{1,64}@/.test(nip05) && nip05.split('@')[1] === domain.toLowerCase()
  const ch = typeof d.channel === 'string' && /^[A-Za-z0-9_-]{1,120}$/.test(d.channel) ? d.channel : null
  return {
    session: { backend: 'belfry', bunkerUrl: bunker, clientSecret: bytesToHex(sk), pubkey: pk, email: typed.trim(), membership: { status: 'unknown' } },
    address: own ? nip05 : typed.trim(),
    open: ch,
    moved: d.moved === true,
  }
}

/** The body of POST /guest/save/verify: email and code trimmed, the name cleaned and only when
 *  the sheet asked for one, the new key's public key, this app's id. */
export const verifyBody = (email: string, code: string, name: string | undefined, sessionKey: string, client = 'belfry-web') =>
  ({ email: email.trim(), code: code.trim(), ...(name ? { name: clean(name) } : {}), session_key: sessionKey.toLowerCase(), client })

/** What a verify answer means (the vector's `next`), for the shared vector check. */
export function verifyNext(status: number, body: Record<string, unknown> | null): string {
  const b = body ?? {}
  if (status === 200) {
    if (b.signed_in === true) { try { signedInFrom(b, new Uint8Array(32), 'x@example.org'); return 'signed_in' } catch { return 'failed' } }
    return 'saved'
  }
  const e = String(b.error ?? '')
  if (status === 409 && e === 'taken') return 'sign_in_instead'
  if (status === 422 && e === 'invalid') return 'same_code'
  if (status === 409 && e === 'key_taken') return 'new_key_new_code'
  if (status === 503 && e === 'signer') return 'new_code'
  if (['wrong_code', 'expired', 'too_many', 'not_guest'].includes(e)) return e
  return 'failed'
}

const demoSigned = (email: string): Session => demoSession(email.trim())

// ---- after a member link: the name and the username, once (board onboarding/member-link-name) ----
const NAME_NEXT = 'belfry-name-next'
/** The member link made a member of this address: ask their name and username next (for 24 h). */
export function askNameNext(email: string) { try { localStorage.setItem(NAME_NEXT, JSON.stringify({ email: email.trim().slice(0, 200), at: Date.now() })) } catch { /* private mode */ } }
/** The address to say on the name screen, while it is due (joined < 24 h ago, not set yet). */
export function nameNext(): { email: string } | null {
  try {
    const v = JSON.parse(localStorage.getItem(NAME_NEXT) ?? 'null') as { email?: unknown; at?: unknown } | null
    if (!v || typeof v.at !== 'number' || Date.now() - v.at > 24 * 3600_000) { localStorage.removeItem(NAME_NEXT); return null }
    return { email: typeof v.email === 'string' ? v.email : '' }
  } catch { return null }
}
export const nameDone = () => { try { localStorage.removeItem(NAME_NEXT) } catch { /* private mode */ } }
