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
export const appliedEmail = () => { try { return localStorage.getItem(EMAIL) ?? '' } catch { return '' } }
const setAppliedEmail = (e: string) => { try { localStorage.setItem(EMAIL, e) } catch { /* private mode */ } }

export type DirectError = 'missing' | 'wrong_code' | 'expired' | 'taken' | 'key_taken' | 'signer' | 'too_many' | 'proof' | 'offline' | 'failed'
export class DirectFailed extends Error {
  constructor(public code: DirectError, public triesLeft?: number) { super(code) }
}
const KNOWN = new Set<DirectError>(['missing', 'wrong_code', 'expired', 'taken', 'key_taken', 'signer', 'too_many', 'proof'])
export function directErrorText(f: DirectFailed): string {
  if (f.code === 'wrong_code' && f.triesLeft !== undefined)
    return f.triesLeft <= 0 ? 'That code is not right, and no tries are left. Send a new one.' : `That code is not right. ${f.triesLeft} ${f.triesLeft === 1 ? 'try' : 'tries'} left.`
  return ({
    missing: 'Write why you would like to join, and tick each agreement.',
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
    throw new DirectFailed(KNOWN.has(e) ? e : 'failed', left)
  }
  return { status: r.status, d }
}

export type Verified = { state: 'pending' } | { state: 'signed_in'; signed: SignedIn } | { state: 'unknown'; proof: string }

/** A signed_in (or approved) answer: the session on the applicant key (never stored unless the
 *  bunker address and the key check out: guestjoin.signedInFrom). */
function signed(d: Record<string, unknown>, email: string): SignedIn {
  const r = signedInFrom(d, applicantKey(), email)
  dropApplicantKey() // it is the session's client key now
  return r
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
const demoSigned = (email: string): SignedIn => { dropApplicantKey(); return { session: demoSession(email), address: email, open: null, moved: false } }
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
export async function applyStart(email: string, motivation: string, agreed: string[], proof?: string): Promise<'sent' | 'pending'> {
  const e = email.trim()
  setAppliedEmail(e)
  applicantKey()
  if (demo()) { await wait(300); if (proof) { setDemoState('pending'); return 'pending' } return 'sent' }
  const { d } = await post('/apply/start', { email: e, motivation: motivation.trim(), agreed, ...(proof ? { proof } : {}) })
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

export type Status = { state: 'none' | 'pending' | 'declined' } | { state: 'approved'; signed?: SignedIn; nip05?: string }

/** Where this browser's application is (asked on opening the chat signed out, and every 5 s while pending). */
export async function applyStatus(): Promise<Status> {
  if (!hasApplicantKey()) return { state: 'none' }
  if (demo()) {
    await wait(150)
    const s = demoState()
    if (s === 'approved') { setDemoState('none'); return { state: 'approved', signed: demoSigned(appliedEmail() || 'new@example.org') } }
    return { state: s === 'pending' || s === 'declined' ? s : 'none' }
  }
  const { d } = await post('/apply/status', {})
  if (d.state === 'approved') {
    if (typeof d.bunker_url === 'string') return { state: 'approved', signed: signed(d, appliedEmail()) }
    return { state: 'approved', nip05: typeof d.nip05 === 'string' ? d.nip05 : undefined }
  }
  return { state: d.state === 'pending' || d.state === 'declined' ? d.state : 'none' }
}
