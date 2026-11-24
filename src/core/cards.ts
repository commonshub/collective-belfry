// Printed cards (canvas "Belfry · Group cards"; contract from the lead, 2026-11-23, after Xavier's
// go): an A6 card with a QR, chat.<domain>/g/<id> (the proxy makes it #/g/<id>). The first scan
// starts a group (portrait card) or a topic (landscape card); later scans join it.
//   GET  /cards/<id>                → {kind, state: unclaimed} | {kind, state: claimed, group, title, invite}
//                                     | {kind, state: retired | archived, title}; 404 unknown (and malformed), 429
//   GET  /cards/hashtag/<name>      → {state: free | taken | invalid}
//   POST /cards/<id>/claim          {title, about, hashtag?}       NIP-98 by the signed-in account → {group}
//   POST /cards/<id>/claim-start    {email}                         NIP-98 by this browser's new key → 202 (a 6-digit code by e-mail)
//   POST /cards/<id>/claim-verify   {email, code, name, title, about, hashtag?}  the same key
//                                     → {signed_in, bunker_url, pubkey, nip05?, group}: that key is the session's
// Errors by code, in the app's own words (never the server's `message`): 409 retired (the card's
// retired screen), 403 no_pass / agent / guest_not_saved / unverified (signed-in claim), 409 claimed {group, invite} (someone was first: join
// theirs through the guest link, no second code), 409 hashtag_taken, 409 own_key / agent / key_taken,
// 400 wrong_code {tries_left}, 410 expired, 422 invalid {field}, 429, 503 signer | relay.
// A claimed card IS a guest link: its invite goes through the usual path (auth.ts pendingInvite).
// Nothing from the address is ever shown: the id only picks what to ask the belfry.
import { bytesToHex } from '@noble/hashes/utils'
import { sha256 } from '@noble/hashes/sha256'
import { finalizeEvent } from 'nostr-tools'
import { BELFRY, getBackend } from '../config'
import { applicantKey, dropApplicantKey } from './direct'
import { signedInFrom, type SignedIn } from './guestjoin'
import { demoSession } from './auth'
import { plainLabel } from './model'

/** 6 characters, capitals and digits without 0 O 1 I L (read aloud). */
export const CARD_ID = /^[A-HJKMNP-Z2-9]{6}$/
const INVITE = /^[A-Za-z0-9_-]{20,200}$/
const GROUP = /^[A-Za-z0-9_-]{1,120}$/

export type CardKind = 'group' | 'topic'
export type CardInfo =
  | { state: 'unclaimed'; kind: CardKind }
  | { state: 'claimed'; kind: CardKind; group: string; title: string; invite: string }
  | { state: 'retired' | 'archived'; kind: CardKind; title: string }
  | { state: 'unknown' }

// ---- the card from the address ---------------------------------------------------------------
const PENDING = 'belfry-card'
export const pendingCard = (): string => { try { const v = localStorage.getItem(PENDING) ?? ''; return CARD_ID.test(v) ? v : '' } catch { return '' } }
export const setPendingCard = (id: string) => { try { if (id) localStorage.setItem(PENDING, id); else localStorage.removeItem(PENDING) } catch { /* private mode */ } }
/** #/g/<id> (or /g/<id> when no proxy rewrote it): kept until the card is dealt with, the address cleaned. */
let taken = ''
let confirmToken: { id: string; token: string } | null = null
/** The Confirm link's token for card ID, once (then forgotten). */
export function takeConfirm(id: string): string { const c = confirmToken; confirmToken = null; return c && c.id === id ? c.token : '' }
/** The card this page was opened for (a malformed one: 'unknown', shown as not known here). */
export const cardNow = (): string => taken || pendingCard()
export const cardDone = () => { taken = ''; setPendingCard('') }
export function takeCardFromUrl(): string {
  const m = /^#\/g\/(.{0,200})$/.exec(location.hash) ?? /^\/g\/(.{0,200})$/.exec(location.pathname)
  if (!m) return ''
  const id = m[1].replace(/\/$/, '').toUpperCase()
  // the e-mail's Confirm button (?confirm=<token>): kept in memory only, never in history or storage (the lead)
  const q = new URLSearchParams(location.search)
  const t = q.get('confirm')
  if (t && /^[A-Za-z0-9_-]{8,200}$/.test(t)) confirmToken = { id, token: t }
  q.delete('confirm')
  const rest = q.toString()
  history.replaceState(history.state, '', '/' + (rest ? '?' + rest : ''))
  if (!CARD_ID.test(id)) { setPendingCard(''); return (taken = 'unknown') }
  setPendingCard(id)
  return (taken = id)
}

// ---- a topic reached through a card: its guest may post there without an e-mail (Xavier, 23 Nov) --
const TOPIC = 'belfry-card-topic'
/** The topic (group id) this browser reached through a topic card, or ''. */
export const cardTopic = (): string => { try { const v = localStorage.getItem(TOPIC) ?? ''; return /^[A-Za-z0-9_-]{1,120}$/.test(v) ? v : '' } catch { return '' } }
export const setCardTopic = (group: string) => { try { if (group) localStorage.setItem(TOPIC, group); else localStorage.removeItem(TOPIC) } catch { /* private mode */ } }

// ---- the first open after a card: half pulled down, with a welcome ---------------------------
const MID = 'belfry-card-mid'
export type Welcome = 'started' | 'joined'
export function markMid(group: string, welcome: Welcome) { try { sessionStorage.setItem(MID, JSON.stringify({ group, welcome })) } catch { /* private mode */ } }
/** The first open of GROUP after its card: the welcome to show (once), or null. */
export function takeMid(group: string): Welcome | null {
  try {
    const v = JSON.parse(sessionStorage.getItem(MID) ?? 'null') as { group?: string; welcome?: string } | null
    if (!v || v.group !== group) return null
    sessionStorage.removeItem(MID)
    return v.welcome === 'started' ? 'started' : 'joined'
  } catch { return null }
}

// ---- what was typed, kept between the code's two steps and a closed tab ------------------------
export interface CardForm { title: string; about: string; hashtag: string; name: string; email: string; /** a topic: the e-mail summary (cards v45; the server's default: weekly) */ summary?: 'weekly' | 'monthly' }
const FORM = 'belfry-card-form'
export function keptForm(id: string): CardForm | null {
  try {
    const v = JSON.parse(localStorage.getItem(FORM) ?? 'null') as (CardForm & { id: string }) | null
    if (!v || v.id !== id) return null
    const s = (x: unknown, n: number) => (typeof x === 'string' ? x.slice(0, n) : '')
    return { title: s(v.title, 80), about: s(v.about, 500), hashtag: s(v.hashtag, 30), name: s(v.name, 100), email: s(v.email, 200), summary: v.summary === 'monthly' ? 'monthly' : 'weekly' }
  } catch { return null }
}
export const keepForm = (id: string, f: CardForm) => { try { localStorage.setItem(FORM, JSON.stringify({ id, ...f })) } catch { /* private mode */ } }
export const dropForm = () => { try { localStorage.removeItem(FORM) } catch { /* private mode */ } }

/** The hashtag as a channel id: lower case, letters, digits and dashes (2–30). */
export const hashtagSlug = (s: string) => s.trim().replace(/^#+/, '').toLowerCase().replace(/[^a-z0-9-]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 30).replace(/-+$/, '') // (trimmed again after the cut: no dash at the end, belfry-android)
/** The Nth suggestion when SLUG is taken: "<slug>-N", cut to 30 with no dash before the number. */
export const nextHashtag = (slug: string, n: number) => slug.slice(0, 30 - String(n).length - 1).replace(/-+$/, '') + '-' + n

// ---- errors -----------------------------------------------------------------------------------
export type CardError = 'claimed' | 'retired' | 'no_pass' | 'guest_not_saved' | 'unverified' | 'hashtag_taken' | 'own_key' | 'agent' | 'key_taken' | 'wrong_code' | 'expired' | 'invalid' | 'too_many' | 'signer' | 'unknown' | 'unsupported' | 'offline' | 'failed'
export class CardFailed extends Error {
  constructor(public code: CardError, public extra: { triesLeft?: number; field?: string; group?: string; invite?: string } = {}) { super(code) }
}
const KNOWN = new Set<CardError>(['claimed', 'retired', 'no_pass', 'guest_not_saved', 'unverified', 'hashtag_taken', 'own_key', 'agent', 'key_taken', 'wrong_code', 'expired', 'invalid', 'too_many', 'signer'])
export function cardFailure(status: number, d: Record<string, unknown>): CardFailed {
  if (status === 404) return new CardFailed('unknown')
  if (status === 429) return new CardFailed('too_many')
  const e = String(d.error ?? '')
  const code: CardError = e === 'relay' ? 'signer' : e === 'unsupported' ? 'unsupported' : KNOWN.has(e as CardError) ? (e as CardError) : 'failed'
  const left = typeof d.tries_left === 'number' && Number.isInteger(d.tries_left) && d.tries_left >= 0 && d.tries_left < 100 ? d.tries_left : undefined
  return new CardFailed(code, {
    ...(left !== undefined ? { triesLeft: left } : {}),
    ...(typeof d.field === 'string' && /^[a-z_]{1,20}$/.test(d.field) ? { field: d.field } : {}),
    ...(typeof d.group === 'string' && GROUP.test(d.group) ? { group: d.group } : {}),
    ...(typeof d.invite === 'string' && INVITE.test(d.invite) ? { invite: d.invite } : {}),
  })
}
/** The words for a refusal: the app's own, never the server's text. */
export function cardErrorText(f: CardFailed, kind: CardKind = 'group'): string {
  if (f.code === 'wrong_code' && f.extra.triesLeft !== undefined)
    return f.extra.triesLeft <= 0 ? 'That code is not right. Send a new one.' : `That code is not right. ${f.extra.triesLeft} ${f.extra.triesLeft === 1 ? 'try' : 'tries'} left.`
  if (f.code === 'invalid') return f.extra.field === 'email' ? 'That doesn’t look like an e-mail address.' : f.extra.field === 'hashtag' ? 'Use letters, digits and dashes, 2 to 30 characters.' : f.extra.field === 'name' ? 'Please give your name.' : `Please give the ${kind}’s name, up to 80 characters.`
  return ({
    claimed: `Someone just started this ${kind}.`,
    retired: `This card was retired: it doesn’t start a ${kind} any more.`,
    no_pass: `Only members of this belfry can start a ${kind} while signed in. Sign out to start it as a guest with your e-mail.`,
    guest_not_saved: `Save your account with your e-mail first, then scan the card again.`,
    unverified: `Confirm your e-mail address first, then scan the card again.`,
    hashtag_taken: 'This hashtag is taken. Try another one.',
    own_key: 'This address has an account that signs in with its own key. Sign in with it, then scan the card again.',
    agent: 'An AI agent can’t start it. Use a person’s account.',
    key_taken: 'That did not work. Send a new code and try again.',
    wrong_code: 'That code is not right. Check the e-mail and try again.',
    expired: 'That code has expired. Send a new one.',
    too_many: 'Too many tries for now. Try again in a few minutes.',
    signer: 'The belfry is busy. Try again in a minute.',
    unknown: 'This card isn’t known here.',
    unsupported: 'This belfry doesn’t take printed cards.',
    offline: 'No connection. Try again.',
    invalid: 'Please check the form.',
    failed: 'That did not work. Try again.',
  } as Record<CardError, string>)[f.code]
}

// ---- calls ------------------------------------------------------------------------------------
const demo = () => getBackend() === 'demo'
const api = (path: string) => `${BELFRY.api}/cards${path}`

/** What the belfry says about card ID (never throws for a wrong id: that is 'unknown'). */
export async function cardInfo(id: string): Promise<CardInfo> {
  if (!CARD_ID.test(id)) return { state: 'unknown' }
  if (demo()) return demoInfo(id)
  let r: Response
  try { r = await fetch(api('/' + id)) } catch { throw new CardFailed('offline') }
  if (r.status === 404) return { state: 'unknown' }
  const d = (await r.json().catch(() => ({}))) as Record<string, unknown>
  if (!r.ok) throw cardFailure(r.status, d)
  return infoOf(d)
}
/** A GET answer, kept only when every field is what it should be. */
export function infoOf(d: Record<string, unknown>): CardInfo {
  const kind: CardKind = d.kind === 'topic' ? 'topic' : 'group'
  const title = typeof d.title === 'string' ? plainLabel(d.title).slice(0, 80) : ''
  if (d.state === 'unclaimed') return { state: 'unclaimed', kind }
  if (d.state === 'claimed' && typeof d.group === 'string' && GROUP.test(d.group) && typeof d.invite === 'string' && INVITE.test(d.invite)) return { state: 'claimed', kind, group: d.group, title, invite: d.invite }
  if (d.state === 'retired' || d.state === 'archived') return { state: d.state, kind, title }
  return { state: 'unknown' }
}

/** Is HASHTAG free for a new topic? */
/** 'unknown': the belfry couldn't check just now (503, offline): try again. */
export async function hashtagState(hashtag: string): Promise<'free' | 'taken' | 'invalid' | 'unknown'> {
  const h = hashtagSlug(hashtag)
  if (h.length < 2) return 'invalid'
  if (demo()) return ['general', 'events', 'market', 'requests'].includes(h) ? 'taken' : 'free'
  try {
    const r = await fetch(api('/hashtag/' + encodeURIComponent(h)))
    if (r.status >= 500 || r.status === 429) return 'unknown'
    const d = (await r.json().catch(() => ({}))) as { state?: string }
    return d.state === 'free' || d.state === 'taken' ? d.state : 'invalid' // a reserved name answers taken
  } catch { return 'unknown' }
}

const claimBody = (kind: CardKind, f: CardForm) => ({ ...(kind === 'topic' ? {} : { title: plainLabel(f.title).slice(0, 80) }), about: f.about.trim().slice(0, 500), ...(kind === 'topic' ? { hashtag: hashtagSlug(f.hashtag), summary: f.summary === 'monthly' ? 'monthly' : 'weekly' } : {}) })

/** Signed in: start the group or topic now. SEND is sync.signedFetch (signed as the account). */
export async function claimSignedIn(id: string, kind: CardKind, f: CardForm, send: (url: string, method: string, body: unknown) => Promise<Response>): Promise<string> {
  if (demo()) return demoClaim(id, kind, f)
  let r: Response
  try { r = await send(api(`/${id}/claim`), 'POST', claimBody(kind, f)) } catch { throw new CardFailed('offline') }
  const d = (await r.json().catch(() => ({}))) as Record<string, unknown>
  if (!r.ok) throw cardFailure(r.status, d)
  if (typeof d.group !== 'string' || !GROUP.test(d.group)) throw new CardFailed('failed')
  return d.group
}

/** A request signed by this browser's new key (direct.ts's applicant key: it becomes the session's). */
async function postNew(path: string, body: Record<string, unknown>): Promise<Record<string, unknown>> {
  const url = api(path)
  const text = JSON.stringify(body)
  const auth = finalizeEvent({ kind: 27235, content: '', created_at: Math.floor(Date.now() / 1000),
    tags: [['u', url], ['method', 'POST'], ['payload', bytesToHex(sha256(new TextEncoder().encode(text)))]] }, applicantKey())
  let r: Response
  try { r = await fetch(url, { method: 'POST', headers: { Authorization: 'Nostr ' + btoa(JSON.stringify(auth)), 'Content-Type': 'application/json' }, body: text }) } catch { throw new CardFailed('offline') }
  const d = (await r.json().catch(() => ({}))) as Record<string, unknown>
  if (!r.ok) {
    const f = cardFailure(r.status, d)
    if (f.code === 'key_taken') dropApplicantKey() // one key, one identity: the next code goes with a new key
    throw f
  }
  return d
}

/** Not signed in: the code and the Confirm button go to the e-mail; the belfry keeps the form for 15
 *  minutes (cards-confirm: the button creates it from there). */
export async function claimStart(id: string, kind: CardKind, f: CardForm): Promise<void> {
  if (demo()) { const s = demoInfo(id); if (s.state === 'claimed') throw new CardFailed('claimed', { group: s.group, invite: s.invite }); return }
  await postNew(`/${id}/claim-start`, { email: f.email.trim(), name: plainLabel(f.name).slice(0, 100), ...claimBody(kind, f) })
}

/** The belfry's answer once the claim is made (claim-verify, claim-poll, claim-confirm on this browser). */
function signedFrom(d: Record<string, unknown>, email: string): { signed: SignedIn; group: string } {
  if (d.signed_in !== true || typeof d.group !== 'string' || !GROUP.test(d.group)) throw new CardFailed('failed')
  let signed: SignedIn
  try { signed = signedInFrom(d, applicantKey(), email) } catch { throw new CardFailed('failed') }
  return { signed: { ...signed, open: d.group }, group: d.group }
}

/** The waiting page asks every few seconds whether the e-mail's Confirm button was clicked: null while
 *  not yet (202), the session once it was (200, once). 410: expired. */
export async function claimPoll(id: string, email: string): Promise<{ signed: SignedIn; group: string } | null> {
  if (demo()) return null
  const d = await postNew(`/${id}/claim-poll`, {})
  if (d.pending === true) return null
  return signedFrom(d, email)
}

/** The e-mail's Confirm button opened this page: the claim is made. On the browser that started it the
 *  session comes with it; on another one, just "confirmed". */
export async function claimConfirm(id: string, token: string): Promise<{ group: string; signed?: SignedIn }> {
  if (demo()) throw new CardFailed('expired')
  const d = await postNew(`/${id}/claim-confirm`, { token })
  if (d.confirmed !== true || typeof d.group !== 'string' || !GROUP.test(d.group)) throw new CardFailed('failed')
  if (d.same_device === true && d.signed_in === true) return signedFrom(d, typeof d.address === 'string' ? d.address : '')
  return { group: d.group }
}

/** The code: the group or topic is made now, and this browser is signed in (as the account the
 *  address already had, or as a new guest who looks after this group). */
export async function claimVerify(id: string, kind: CardKind, f: CardForm, code: string): Promise<{ signed: SignedIn; group: string }> {
  if (demo()) {
    if (code.trim() !== '123456') throw new CardFailed('wrong_code', { triesLeft: 2 })
    // nobody is signed in yet, and signing in starts from a clean store: the demo opens # general
    const s = demoInfo(id)
    if (s.state === 'claimed') throw new CardFailed('claimed', { group: s.group, invite: s.invite })
    const group = 'general'
    try { localStorage.setItem(DEMO, JSON.stringify({ ...demoClaimed(), [id]: group })) } catch { /* private mode */ }
    return { signed: { session: { ...demoSession(f.email.trim()), clientSecret: bytesToHex(applicantKey()) }, address: f.email.trim(), open: group, moved: false }, group }
  }
  const d = await postNew(`/${id}/claim-verify`, { email: f.email.trim(), code: code.trim(), name: plainLabel(f.name).slice(0, 100), client: 'belfry-web', ...claimBody(kind, f) })
  return signedFrom(d, f.email)
}

// ---- demo: cards K7Q2MX (a group card) and T9PC4H (a topic card) are free; CXAWMS is claimed (TPCHAT: a claimed topic card),
// RETRDX retired, ARCH9V archived; anything else is unknown. Code 123456. ---------------------
const DEMO = 'demo-cards'
const demoClaimed = (): Record<string, string> => { try { return JSON.parse(localStorage.getItem(DEMO) ?? '{}') } catch { return {} } }
function demoInfo(id: string): CardInfo {
  const taken = demoClaimed()[id]
  const kind: CardKind = id === 'T9PC4H' ? 'topic' : 'group'
  if (taken && demoRetired().includes(id)) return { state: 'retired', kind, title: taken }
  if (taken) return { state: 'claimed', kind, group: taken, title: taken, invite: 'demo-card-invite-' + id.toLowerCase() + '-xxxxxx' }
  if (id === 'K7Q2MX' || id === 'T9PC4H' || id === 'G2RDNS') return { state: 'unclaimed', kind } // (G2RDNS: a second free group card, for a signed-in start)
  if (id === 'CXAWMS') return { state: 'claimed', kind, group: 'general', title: 'Townsquare', invite: 'demo-card-invite-cxawms-xxxxxxxx' }
  if (id === 'TPCHAT') return { state: 'claimed', kind: 'topic', group: 'general', title: 'Townsquare', invite: 'demo-card-invite-tpchat-xxxxxxxx' }
  if (id === 'RETRDX') return { state: 'retired', kind, title: 'Sourdough starters' }
  if (id === 'ARCH9V') return { state: 'archived', kind, title: 'Book club' }
  return { state: 'unknown' }
}
let demoMake: ((kind: CardKind, f: CardForm) => Promise<string>) | undefined
/** Demo only: how a claim makes its group here (actions.ts, set by the UI to avoid a cycle). */
export const setDemoMake = (fn: typeof demoMake) => { demoMake = fn }
async function demoClaim(id: string, kind: CardKind, f: CardForm): Promise<string> {
  const s = demoInfo(id)
  if (s.state === 'claimed') throw new CardFailed('claimed', { group: s.group, invite: s.invite })
  if (s.state !== 'unclaimed') throw new CardFailed('unknown')
  if (kind === 'topic' && (await hashtagState(f.hashtag)) !== 'free') throw new CardFailed('hashtag_taken')
  const group = demoMake ? await demoMake(kind, f) : 'general'
  try { localStorage.setItem(DEMO, JSON.stringify({ ...demoClaimed(), [id]: group })) } catch { /* private mode */ }
  return group
}

// ---- for the group's stewards (settings, "The card") -------------------------------------------
//   GET  /groups/<id>/card   → {id, started_by: {name, pubkey}, joined_by_scan}   (404: no card)
//   POST /cards/<id>/retire  → 200
export interface GroupCard { id: string; startedBy: string; joinedByScan: number }
type Send = (url: string, method: string, body?: unknown) => Promise<Response>
export async function groupCard(group: string, send: Send): Promise<GroupCard | null> {
  if (demo()) { const id = Object.entries(demoClaimed()).find(([, g]) => g === group)?.[0]; return id && !demoRetired().includes(id) ? { id, startedBy: '', joinedByScan: 0 } : null }
  let r: Response
  try { r = await send(`${BELFRY.api}/groups/${encodeURIComponent(group)}/card`, 'GET') } catch { throw new CardFailed('offline') }
  if (r.status === 404 || r.status === 403) return null
  const d = (await r.json().catch(() => ({}))) as Record<string, unknown>
  if (!r.ok) throw cardFailure(r.status, d)
  if (typeof d.id !== 'string' || !CARD_ID.test(d.id)) return null
  const n = typeof d.joined_by_scan === 'number' && Number.isInteger(d.joined_by_scan) && d.joined_by_scan >= 0 ? d.joined_by_scan : 0
  const by = d.started_by as { name?: unknown } | undefined
  // started_by = {name, pubkey}: the name only is shown
  const name = by && typeof by === 'object' && typeof by.name === 'string' ? plainLabel(by.name) : ''
  return { id: d.id, startedBy: name, joinedByScan: n }
}
export async function retireCard(id: string, send: Send): Promise<void> {
  if (!CARD_ID.test(id)) throw new CardFailed('unknown')
  if (demo()) { try { localStorage.setItem(DEMO + '-retired', JSON.stringify([...demoRetired(), id])) } catch { /* private mode */ } return }
  let r: Response
  try { r = await send(api(`/${id}/retire`), 'POST', {}) } catch { throw new CardFailed('offline') }
  if (!r.ok) throw cardFailure(r.status, (await r.json().catch(() => ({}))) as Record<string, unknown>)
}
const demoRetired = (): string[] => { try { const v = JSON.parse(localStorage.getItem(DEMO + '-retired') ?? '[]'); return Array.isArray(v) ? v : [] } catch { return [] } }
/** The card's address (its QR). */
export const cardUrl = (id: string) => `https://chat.${BELFRY.domain}/g/${id}`
