// Joining from the chat's own start screen (Xavier 19 Nov, via the lead: "It has to be that
// simple"). Everything on chat.<domain>: Log in = e-mail → 6-digit code → in; Apply = why + the
// charter → e-mail → code → "Application pending"; once approved, this browser is let in by itself.
// Server (collective-belfry branch apply-direct), every request NIP-98 signed by the "applicant
// key": made once by this browser and kept until it becomes the session's client key.
//   POST /login/start  {email}                          → 202 (always)
//   POST /login/verify {email, code}                    → {state: signed_in | pending | unknown (+proof)}
//   POST /apply/start  {email, motivation, agreed, proof?} → 202 {sent} | 200 {state: pending} (with proof: no code)
//   POST /apply/verify {email, code}                    → {state: pending | signed_in}
//   POST /apply/status {}                               → {state: none | pending | declined | approved (+session once)}
// Errors in fixed words by code (never the server's text): 422 wrong_code {tries_left}, 410
// expired, 409 taken {nip05} (an account holding its own key), 409 key_taken (a new key, a new
// code), 503 signer, 429 too_many, 400 missing, 422 proof (stale: back to the code step).
import { finalizeEvent, generateSecretKey } from 'nostr-tools'
import { sha256 } from '@noble/hashes/sha256'
import { bytesToHex, hexToBytes } from '@noble/hashes/utils'
import { BELFRY, getBackend } from '../config'
import { demoSession } from './auth'
import { signedInFrom, type SignedIn } from './guestjoin'

const KEY = 'belfry-applicant' // hex secret key, this browser only
const EMAIL = 'belfry-applied-email' // the address proven here, for "Signed in as" and Log in after approval

/** This browser's applicant key (made the first time it is needed). */
export function applicantKey(): Uint8Array {
  try {
    const h = localStorage.getItem(KEY)
    if (h && /^[0-9a-f]{64}$/.test(h)) return hexToBytes(h)
  } catch { /* private mode */ }
  const sk = generateSecretKey()
  try { localStorage.setItem(KEY, bytesToHex(sk)) } catch { /* private mode: this page only */ }
  return sk
}
export const hasApplicantKey = () => { try { return /^[0-9a-f]{64}$/.test(localStorage.getItem(KEY) ?? '') } catch { return false } }
/** The key became a session's (or was refused): the next request makes a new one. */
export const dropApplicantKey = () => { try { localStorage.removeItem(KEY) } catch { /* private mode */ } }
/** The session SECRET (hex) is this browser's applicant key, and it has just signed: the key is
 *  the session's now, and leaves the applicant slot. */
export function releaseApplicantKey(secret: string) {
  try { if (localStorage.getItem(KEY) === secret) localStorage.removeItem(KEY) } catch { /* private mode */ }
}
export const appliedEmail = () => { try { return localStorage.getItem(EMAIL) ?? '' } catch { return '' } }
const setAppliedEmail = (e: string) => { try { localStorage.setItem(EMAIL, e) } catch { /* private mode */ } }

export type DirectError = 'missing' | 'unticked' | 'invalid' | 'unsupported' | 'username_invalid' | 'username_taken' | 'wrong_code' | 'expired' | 'taken' | 'key_taken' | 'signer' | 'too_many' | 'proof' | 'offline' | 'failed'
export class DirectFailed extends Error {
  constructor(public code: DirectError, public triesLeft?: number, /** username_taken: the belfry's suggestions */ public suggestions: string[] = []) { super(code) }
}
const KNOWN = new Set<DirectError>(['missing', 'unticked', 'invalid', 'unsupported', 'username_invalid', 'username_taken', 'wrong_code', 'expired', 'taken', 'key_taken', 'signer', 'too_many', 'proof'])
export function directErrorText(f: DirectFailed): string {
  if (f.code === 'wrong_code' && f.triesLeft !== undefined)
    return f.triesLeft <= 0 ? 'That code is not right, and no tries are left. Send a new one.' : `That code is not right. ${f.triesLeft} ${f.triesLeft === 1 ? 'try' : 'tries'} left.`
  return ({
    missing: 'Write why you would like to join.',
    unticked: 'Tick each agreement of the charter.',
    invalid: 'That doesn’t look like an e-mail address.',
    unsupported: 'This belfry takes applications another way.',
    username_invalid: 'Use lowercase letters, digits and dashes, 3 to 30 characters.',
    username_taken: 'That username was just taken. Pick another.',
    wrong_code: 'That code is not right. Check the e-mail and try again.',
    expired: 'That code has expired. Send a new one.',
    taken: 'This address has an account that signs in with its own key.',
    key_taken: 'That did not work. Send a new code.',
    signer: 'That did not work just now. Send a new code.',
    too_many: 'Too many codes for now. Try again in half an hour.',
    proof: 'That took too long. We sent you a new code.',
    offline: 'No connection. Try again.',
    failed: 'That did not work. Try again.',
  } as Record<DirectError, string>)[f.code]
}

const demo = () => getBackend() === 'demo'
/** The belfry's suggested usernames (at most 3, each a plain username). */
export const suggestionsOf = (d: Record<string, unknown>): string[] =>
  (Array.isArray(d.suggestions) ? d.suggestions : []).filter((x): x is string => typeof x === 'string' && /^[a-z0-9][a-z0-9._-]{0,29}$/.test(x)).slice(0, 3)
const wait = (ms: number) => new Promise((r) => setTimeout(r, ms))

async function post(path: string, body: Record<string, unknown>): Promise<{ status: number; d: Record<string, unknown> }> {
  const url = `${BELFRY.api}${path}`
  const text = JSON.stringify(body)
  const auth = finalizeEvent({ kind: 27235, content: '', created_at: Math.floor(Date.now() / 1000),
    tags: [['u', url], ['method', 'POST'], ['payload', bytesToHex(sha256(new TextEncoder().encode(text)))]] }, applicantKey())
  let r: Response
  try { r = await fetch(url, { method: 'POST', headers: { Authorization: 'Nostr ' + btoa(JSON.stringify(auth)), 'Content-Type': 'application/json' }, body: text }) } catch { throw new DirectFailed('offline') }
  const d = (await r.json().catch(() => ({}))) as Record<string, unknown>
  if (!r.ok) {
    const e = String(d.error ?? '') as DirectError
    const left = typeof d.tries_left === 'number' && Number.isInteger(d.tries_left) && d.tries_left >= 0 && d.tries_left < 100 ? d.tries_left : undefined
    if (e === 'key_taken') dropApplicantKey() // one key, one identity: the next code goes with a new key
    throw new DirectFailed(KNOWN.has(e) ? e : 'failed', left, suggestionsOf(d))
  }
  return { status: r.status, d }
}

export type Verified = { state: 'pending' } | { state: 'signed_in'; signed: SignedIn } | { state: 'unknown'; proof: string }

/** A signed_in (or approved) answer: the session on the applicant key (never stored unless the
 *  bunker address and the key check out: guestjoin.signedInFrom). */
function signed(d: Record<string, unknown>, email: string): SignedIn {
  // the applicant key stays stored until the session is saved and has signed once (App.tsx:
  // releaseApplicantKey): a tab that dies in between can still be let in by /apply/status
  return signedInFrom(d, applicantKey(), email)
}

function verified(d: Record<string, unknown>, email: string): Verified {
  if (d.state === 'signed_in') return { state: 'signed_in', signed: signed(d, email) }
  if (d.state === 'pending') return { state: 'pending' }
  if (d.state === 'unknown' && typeof d.proof === 'string' && d.proof.length > 0 && d.proof.length < 2000) return { state: 'unknown', proof: d.proof }
  throw new DirectFailed('failed')
}

// ---- demo: what the belfry would answer (code 123456; member@example.org has an account;
// pending@example.org applied already; window.demoApplyDecide('approved' | 'declined')) ----------
const DEMO = 'demo-apply-state'
const demoState = (): string => { try { return localStorage.getItem(DEMO) ?? 'none' } catch { return 'none' } }
const setDemoState = (s: string) => { try { localStorage.setItem(DEMO, s) } catch { /* private mode */ } }
const demoSigned = (email: string): SignedIn => ({ session: { ...demoSession(email), clientSecret: bytesToHex(applicantKey()) }, address: email, open: null, moved: false })
let demoTries = 5
function demoCode(code: string) {
  if (code !== '123456') { demoTries = Math.max(0, demoTries - 1); throw new DirectFailed('wrong_code', demoTries) }
  demoTries = 5
}
if (demo()) Object.assign(window, { demoApplyDecide: (s: 'approved' | 'declined' | 'pending' | 'none') => setDemoState(s) })

export async function loginStart(email: string) {
  applicantKey() // made now (the demo signs nothing, but status needs to know this browser applied)
  if (demo()) { await wait(300); return }
  await post('/login/start', { email: email.trim() })
}

export async function loginVerify(email: string, code: string): Promise<Verified> {
  const e = email.trim()
  setAppliedEmail(e)
  if (demo()) {
    await wait(300); demoCode(code.trim())
    if (e.toLowerCase() === 'member@example.org') return { state: 'signed_in', signed: demoSigned(e) }
    if (e.toLowerCase() === 'pending@example.org') { setDemoState('pending'); return { state: 'pending' } }
    return { state: 'unknown', proof: 'demo-proof' }
  }
  return verified((await post('/login/verify', { email: e, code: code.trim() })).d, e)
}

/** Apply: with PROOF (a login code just proved the address) → pending at once; without → a code goes out. */
export async function applyStart(email: string, motivation: string, agreed: string[], proof?: string, who?: { name: string; username: string }): Promise<'sent' | 'pending'> {
  const e = email.trim()
  setAppliedEmail(e)
  applicantKey()
  if (who) setDemoWho(who, motivation, e)
  if (demo()) { await wait(300); if (who && demoTaken.has(who.username)) throw new DirectFailed('username_taken', undefined, demoSuggest(who.name).filter((x) => x !== who.username)); if (proof) { setDemoState('pending'); return 'pending' } return 'sent' }
  const { d } = await post('/apply/start', { email: e, motivation: motivation.trim(), agreed, ...(proof ? { proof } : {}), ...(who ? { name: who.name.trim().replace(/\s+/g, ' ').slice(0, 100), username: who.username } : {}) })
  return d.state === 'pending' ? 'pending' : 'sent'
}

export async function applyVerify(email: string, code: string): Promise<Verified> {
  const e = email.trim()
  if (demo()) {
    await wait(300); demoCode(code.trim())
    if (e.toLowerCase() === 'member@example.org') return { state: 'signed_in', signed: demoSigned(e) }
    setDemoState('pending'); return { state: 'pending' }
  }
  const v = verified((await post('/apply/verify', { email: e, code: code.trim() })).d, e)
  if (v.state === 'unknown') throw new DirectFailed('failed')
  return v
}

/** What a pending request says (for its screen): the name, the username asked for, the e-mail, the message. */
export interface Pending { name: string; username: string; email: string; motivation: string }
export type Status = { state: 'none' } | { state: 'pending'; pending?: Pending } | { state: 'declined'; note?: string } | { state: 'approved'; signed?: SignedIn; nip05?: string }
const text = (x: unknown, max: number) => (typeof x === 'string' ? x.slice(0, max) : '')

/** Where this browser's application is (asked on opening the chat signed out, and every 5 s while pending). */
export async function applyStatus(): Promise<Status> {
  if (!hasApplicantKey()) return { state: 'none' }
  if (demo()) {
    await wait(150)
    const s = demoState()
    if (s === 'approved') { setDemoState('none'); return { state: 'approved', signed: demoSigned(appliedEmail() || 'new@example.org') } }
    if (s === 'pending') { const w = demoWho(); return { state: 'pending', ...(w ? { pending: w } : {}) } }
    if (s === 'declined') return { state: 'declined' }
    return { state: 'none' }
  }
  const { d } = await post('/apply/status', {})
  if (d.state === 'approved') {
    if (typeof d.bunker_url === 'string') return { state: 'approved', signed: signed(d, appliedEmail()) }
    return { state: 'approved', nip05: typeof d.nip05 === 'string' ? d.nip05 : undefined }
  }
  if (d.state === 'pending') {
    const p: Pending = { name: text(d.name, 100), username: text(d.username, 30), email: text(d.email, 200), motivation: text(d.motivation, 4000) }
    return { state: 'pending', ...(p.name || p.email || p.motivation ? { pending: p } : {}) }
  }
  if (d.state === 'declined') return { state: 'declined', ...(text(d.note, 2000) ? { note: text(d.note, 2000) } : {}) }
  return { state: 'none' }
}

/** A belfry without its own register (409 unsupported): the old application, unsigned
 *  (POST /apply; the stewards answer by e-mail, no code here). */
export async function applyOld(email: string, motivation: string, agreed: string[]) {
  if (demo()) { await wait(300); return }
  let r: Response
  try { r = await fetch(`${BELFRY.api}/apply`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ email: email.trim(), motivation: motivation.trim(), agreed, website: '' }) }) } catch { throw new DirectFailed('offline') }
  if (!r.ok) { const d = (await r.json().catch(() => ({}))) as Record<string, unknown>; const e = String(d.error ?? '') as DirectError; throw new DirectFailed(KNOWN.has(e) && e !== 'unsupported' ? e : 'failed') }
}

// ---- one poll per browser (the proxy allows 60 API calls a minute per client) -------------------
const LOCK = 'belfry-apply-poll'
const TAB = Math.random().toString(36).slice(2)
/** May this tab ask now? Only the tab holding the lock (renewed on each ask; free after 20 s). */
export function takePollTurn(): boolean {
  try {
    const l = JSON.parse(localStorage.getItem(LOCK) ?? 'null') as { tab: string; at: number } | null
    if (l && l.tab !== TAB && Date.now() - l.at < 20_000) return false
    localStorage.setItem(LOCK, JSON.stringify({ tab: TAB, at: Date.now() }))
    return true
  } catch { return true }
}

// ---- the name and the username (canvas v54, Xavier 19 Nov; POST /username/check, /welcome) -------
export type Check = { state: 'available' | 'taken' | 'invalid'; username: string; reason?: string; suggestions: string[] }
const USERNAME = /^[a-z0-9](?:[a-z0-9._-]{1,28}[a-z0-9])$/
const demoTaken = new Set(['admin', 'ana', 'stewards'])
const slug = (w: string) => w.toLowerCase().normalize('NFKD').replace(/[\u0300-\u036f]/g, '').replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '')
/** Demo only: the suggestions the belfry would make (first name, first-last, first-2). */
function demoSuggest(name: string): string[] {
  const w = name.trim().split(/\s+/).map(slug).filter(Boolean)
  if (!w.length) return []
  const out = [w[0], w.length > 1 ? `${w[0]}-${w[w.length - 1]}` : '', `${w[0]}-2`, `${w[0]}-3`].filter((x) => x && USERNAME.test(x) && !demoTaken.has(x))
  return [...new Set(out)].slice(0, 3)
}
type DemoWho = Pending
const setDemoWho = (who: { name: string; username: string }, motivation: string, email: string) => { if (demo()) try { localStorage.setItem('demo-apply-who', JSON.stringify({ ...who, motivation, email })) } catch { /* private mode */ } }
const demoWho = (): DemoWho | null => { try { return JSON.parse(localStorage.getItem('demo-apply-who') ?? 'null') } catch { return null } }

/** Ask the belfry about a username (or, with only a name, for the one to suggest). VIA: the
 *  applicant key (apply) or the session (after a member link). 429 too_many: null (keep the last state). */
export async function checkUsername(body: { username?: string; name?: string }, via: 'applicant' | 'session'): Promise<{ check?: Check; suggested?: string; suggestions: string[] } | null> {
  if (demo()) {
    await wait(150)
    if (body.username !== undefined) {
      const u = body.username.trim().toLowerCase()
      const sug = demoSuggest(body.name ?? u).filter((x) => x !== u)
      if (!USERNAME.test(u)) return { check: { state: 'invalid', username: u, reason: u.length < 3 ? 'short' : u.length > 30 ? 'long' : 'chars', suggestions: [] }, suggestions: [] }
      return { check: { state: demoTaken.has(u) ? 'taken' : 'available', username: u, suggestions: demoTaken.has(u) ? sug : [] }, suggestions: sug }
    }
    const sug = demoSuggest(body.name ?? '')
    return { suggested: sug[0], suggestions: sug }
  }
  let d: Record<string, unknown>
  try {
    if (via === 'applicant') d = (await post('/username/check', body)).d
    else {
      const { signedFetch } = await import('./sync')
      const r = await signedFetch(`${BELFRY.api}/username/check`, 'POST', body)
      if (r.status === 429) return null
      if (!r.ok) throw new DirectFailed('failed')
      d = (await r.json().catch(() => ({}))) as Record<string, unknown>
    }
  } catch (e) {
    if (e instanceof DirectFailed && e.code === 'too_many') return null
    throw e
  }
  const sug = suggestionsOf(d)
  if (body.username !== undefined) {
    const st = d.state === 'available' || d.state === 'taken' || d.state === 'invalid' ? d.state : 'invalid'
    return { check: { state: st, username: typeof d.username === 'string' ? d.username.slice(0, 30) : body.username, reason: typeof d.reason === 'string' ? d.reason : undefined, suggestions: sug }, suggestions: sug }
  }
  const suggested = typeof d.suggested === 'string' && /^[a-z0-9][a-z0-9._-]{0,29}$/.test(d.suggested) ? d.suggested : undefined
  return { suggested, suggestions: sug }
}

/** After a member link: the member's name and username (POST /welcome through the session). */
export async function welcomeName(name: string, username: string): Promise<{ name: string; username: string; nip05?: string } | 'already_set'> {
  if (demo()) {
    await wait(250)
    if (demoTaken.has(username)) throw new DirectFailed('username_taken', undefined, demoSuggest(name).filter((x) => x !== username))
    return { name, username, nip05: `${username}@${BELFRY.domain}` }
  }
  const { signedFetch } = await import('./sync')
  let r: Response
  try { r = await signedFetch(`${BELFRY.api}/welcome`, 'POST', { name: name.trim().replace(/\s+/g, ' ').slice(0, 100), username }) } catch { throw new DirectFailed('offline') }
  const d = (await r.json().catch(() => ({}))) as Record<string, unknown>
  // already set, an account holding its own key, a guest or an unverified address: no screen
  if (r.status === 409 && (d.error === 'already_set' || d.error === 'own_key')) return 'already_set'
  if (r.status === 403) return 'already_set'
  if (!r.ok) { const e = String(d.error ?? '') as DirectError; throw new DirectFailed(e === 'username_invalid' || e === 'username_taken' ? e : 'failed', undefined, suggestionsOf(d)) }
  return { name: text(d.name, 100) || name, username: text(d.username, 30) || username, nip05: typeof d.nip05 === 'string' ? d.nip05 : undefined }
}
