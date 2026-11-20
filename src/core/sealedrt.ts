// Sealed conversations, on this device (docs/sealed.md v1; the core is sealedcore.ts): the key
// chain and what this device remembers, the chains of the people I write to, opening and sealing
// direct messages, and this device's own path (first device, asking, allowing, removing, rotating).
//
// Where things live (plan approved by the lead, 20 Nov): the device key is the session's
// clientSecret (kv 'session'); the key chain, pins, sealed conversations and "shared by" marks are
// kv entries of this account's own IndexedDB, wiped exactly when the device key changes (sign-out,
// a new sign-in, Forget this device delete the database). Never in localStorage, never logged,
// never in an error, toast or outbox label. Roster writes run under navigator.locks.
import { getPublicKey, nip44, type Filter, type NostrEvent } from 'nostr-tools'
import { bytesToHex, hexToBytes, randomBytes } from '@noble/hashes/utils'
import { BELFRY } from '../config'
import { kvGet, kvSet } from './db'
import * as C from './sealedcore'
import { addEvents, all, outboxItems, putOutbox, subscribe, touch } from './store'
import { enqueue, enqueueSigned, flush, isLocalOnly, me, queryComplete, replayWraps, setExtraMainFilters, signedFetch } from './sync'

type Conv = string

interface Pin { root: string; head: string; heads: Record<string, string> /* lb of every head seen, by roster id */; prevRoot?: string /* the root pinned before a fresh start */ }
interface Fresh { root: string; at: number; shared: Conv[] }

let device: { secret: Uint8Array; pub: string } | null = null
const keys = new Map<string, string>() // lb → secret hex
let pins: Record<string, Pin> = {}
let sealedConvs = new Set<Conv>()
let shared: Record<string, string> = {} // rumor id → sharer
let freshOf: Record<string, Fresh> = {} // person → their latest fresh start I noticed
let firstSeen: Record<Conv, number> = {} // conv → when this device first saw it sealed (device lines from then on)
let askedAt = 0
let started = false
let partners: string[] = []
/** dm.ts: the people of my conversations (up to 200, most recent first): their chains stay live. */
export function setPartners(list: string[]) {
  const next = [...new Set(list)].filter((p) => p !== me()).slice(0, 200).sort()
  if (next.join() === partners.join()) return
  partners = next
  if (started) setExtraMainFilters(currentFilters)
}
let currentFilters: () => Filter[] = () => []

const K = { device: 'sealed:device', chain: 'sealed:chain', pins: 'sealed:pins', convs: 'sealed:convs', shared: 'sealed:shared', fresh: 'sealed:fresh', first: 'sealed:first', asked: 'sealed:asked' }
const save = (k: string, v: unknown) => void kvSet(k, v).catch(() => { /* full or closed: kept in memory */ })
const nowS = () => Math.floor(Date.now() / 1000)

export const active = () => started && !!device
export const devicePub = () => device?.pub ?? ''

// ---- start ----------------------------------------------------------------------------------

/** Once signed in on a belfry (not demo), after sync.start: CLIENT_SECRET is this device's key. */
export async function startSealed(clientSecret: string) {
  if (started || isLocalOnly() || !/^[0-9a-f]{64}$/.test(clientSecret)) return
  const secret = hexToBytes(clientSecret)
  device = { secret, pub: getPublicKey(secret) }
  // the chain belongs to this device key: another key (should not happen: a new session wipes
  // the database) means none of it is ours
  const stored = await kvGet<string>(K.device)
  if (stored && stored !== device.pub) for (const k of Object.values(K)) save(k, undefined)
  else {
    for (const k of (await kvGet<C.LbKey[]>(K.chain)) ?? []) keys.set(k.lb, k.secret)
    pins = (await kvGet<Record<string, Pin>>(K.pins)) ?? {}
    sealedConvs = new Set((await kvGet<Conv[]>(K.convs)) ?? [])
    shared = (await kvGet<Record<string, string>>(K.shared)) ?? {}
    freshOf = (await kvGet<Record<string, Fresh>>(K.fresh)) ?? {}
    firstSeen = (await kvGet<Record<Conv, number>>(K.first)) ?? {}
    askedAt = (await kvGet<number>(K.asked)) ?? 0
  }
  save(K.device, device.pub)
  started = true
  currentFilters = () => device ? [
    { kinds: [C.ANCHOR], authors: [me()] }, // my anchor and my devices' name records (d prefix: not filterable)
    { kinds: [C.ROSTER], '#p': [me()] },
    // the people I write to: their rosters and anchors live, so a removed device of theirs gets
    // nothing new from me (a message goes to the head I know)
    ...(partners.length ? [{ kinds: [C.ROSTER], '#p': partners }, { kinds: [C.ANCHOR], authors: partners, '#d': [C.ANCHOR_D] }] : []),
    { kinds: [C.REQUEST], '#p': [me()], since: nowS() - 7 * 86400 },
    { kinds: [1059], '#p': [device.pub] }, // hand-overs to this device (never filtered by lb: the relay ignores #lb)
  ] as Filter[] : []
  setExtraMainFilters(currentFilters)
  let t: ReturnType<typeof setTimeout> | null = null
  subscribe(() => { t ??= setTimeout(() => { t = null; void onStore() }, 400) })
  setTimeout(() => void myDevice(), 3000)
  setInterval(() => void myDevice(), 6 * 3600e3)
}

function persistKeys() {
  if (keys.size) void navigator.storage?.persist?.().catch(() => false) // Safari evicts unvisited sites' storage: ask once we hold keys
  save(K.chain, [...keys].map(([lb, secret]) => ({ lb, secret })))
}

// ---- chains ---------------------------------------------------------------------------------

let memo: { v: number; of: Map<string, C.Chain | null> } = { v: -1, of: new Map() }
let storeV = 0
subscribe(() => { storeV++ })

/** A person's valid chain from what the store holds (null: none, or not loaded yet). */
export function chainOf(pk: string): C.Chain | null {
  if (memo.v !== storeV) memo = { v: storeV, of: new Map() }
  if (memo.of.has(pk)) return memo.of.get(pk)!
  const evs = all()
  const anchor = C.newestAnchor(evs.filter((e) => e.kind === C.ANCHOR && e.pubkey === pk), pk)
  const c = anchor ? C.validChain(anchor, evs.filter((e) => e.kind === C.ROSTER && e.tags.some((t) => t[0] === 'p' && t[1] === pk))) : null
  memo.of.set(pk, c)
  if (c) notePin(pk, c)
  return c
}

const loaded = new Map<string, number>() // pk → when its chain was last asked for
/** Ask the relay for a person's anchor and rosters, always without since (§3.3). */
export async function loadChain(pk: string, maxAgeMs = 10 * 60e3): Promise<C.Chain | null> {
  if (Date.now() - (loaded.get(pk) ?? 0) < maxAgeMs) return chainOf(pk)
  loaded.set(pk, Date.now())
  const got = await queryComplete('main', [{ kinds: [C.ANCHOR], authors: [pk], '#d': [C.ANCHOR_D] }, { kinds: [C.ROSTER], '#p': [pk] }] as Filter[])
  if (got === null) { loaded.delete(pk); return chainOf(pk) }
  addEvents(got)
  return chainOf(pk)
}

/** §3.4: remember the root first accepted and every head seen; a new root = they started fresh. */
function notePin(pk: string, c: C.Chain) {
  const head = C.headOf(c)
  const p = pins[pk]
  if (!p) { pins[pk] = { root: c.rosters[0].id, head: head.id, heads: { [head.id]: head.lb } }; save(K.pins, pins); return }
  if (p.root !== c.rosters[0].id) {
    freshOf[pk] = { root: c.rosters[0].id, at: c.rosters[0].created_at * 1000, shared: [] }
    save(K.fresh, freshOf)
    pins[pk] = { root: c.rosters[0].id, head: head.id, heads: { ...p.heads, [head.id]: head.lb }, prevRoot: p.root }
    save(K.pins, pins)
    return
  }
  if (p.head !== head.id) { p.head = head.id; p.heads[head.id] = head.lb; save(K.pins, pins) }
}

/** The letterbox to seal to for PK: the head's, else the last head this device knew (§6: never fall back). */
function lbFor(pk: string): string | undefined {
  const c = chainOf(pk)
  if (c) return C.headOf(c).lb
  const p = pins[pk]
  return p ? p.heads[p.head] : undefined
}

export const readyChain = (pk: string) => { const c = chainOf(pk); return !!c && C.isReady(c) }

// ---- conversations --------------------------------------------------------------------------

const participants = (conv: Conv): string[] => (conv.startsWith('n:') ? conv.slice(2).split(',') : [])

/** §6: sealed when every participant, me included, has a ready chain; then never again the old
 *  way (remembered here, not published). An e-mail thread: when my own chain is ready (§7). */
export function isSealedConv(conv: Conv): boolean {
  if (!active()) return false
  if (sealedConvs.has(conv)) { if (firstSeen[conv] === undefined) { firstSeen[conv] = Date.now(); save(K.first, firstSeen) } return true }
  const people = conv.startsWith('m:') ? [me()] : [...new Set([...participants(conv), me()])]
  if (!people.every(readyChain)) { void ensureChains(conv); return false }
  sealedConvs.add(conv)
  save(K.convs, [...sealedConvs])
  if (firstSeen[conv] === undefined) { firstSeen[conv] = Date.now(); save(K.first, firstSeen) } // device lines from here on
  return true
}

/** Ask the relay for the chains of a conversation's people not ready here yet (each at most every
 *  10 minutes); the views read again when one arrives. */
export async function ensureChains(conv: Conv) {
  if (!active()) return
  const people = conv.startsWith('m:') ? [me()] : [...new Set([...participants(conv), me()])]
  const before = people.filter(readyChain).length
  await Promise.all(people.filter((p) => !readyChain(p)).map((p) => loadChain(p)))
  if (people.filter(readyChain).length !== before) touch()
}

/** §4: before each send and when a sealed conversation opens, every participant's anchor and
 *  rosters asked for again (they stay live too); at most 3 s, then what is known is used. */
export async function refreshChains(conv: Conv, waitMs = 3000) {
  if (!active()) return
  const people = conv.startsWith('m:') ? [me()] : [...new Set([...participants(conv), me()])]
  const got = await queryComplete('main', [{ kinds: [C.ANCHOR], authors: people, '#d': [C.ANCHOR_D] }, { kinds: [C.ROSTER], '#p': people }] as Filter[], waitMs)
  if (got?.length) { addEvents(got); for (const p of people) loaded.set(p, Date.now()) }
}

/** This device is in my head and holds its letterbox secret. */
export function deviceIn(): boolean {
  const c = chainOf(me())
  return !!c && !!device && C.headOf(c).devices.includes(device.pub) && keys.has(C.headOf(c).lb)
}

/** §5.3: the list says "Locked on this device" for a conversation this device knows whose
 *  participants all have a ready chain, while this device isn't in my head. */
export const lockedConv = (conv: Conv) => active() && !!chainOf(me()) && !deviceIn() && (conv.startsWith('m:') ? readyChain(me()) : participants(conv).every(readyChain) && readyChain(me()))
/** The banner: not in my head, or a wrap is waiting for a key this device lacks. */
export const showBanner = () => active() && ((!!chainOf(me()) && !deviceIn()) || waitingForKey)
let waitingForKey = false

// ---- opening (dm.ts) ------------------------------------------------------------------------

export type Opened = { ok: true; rumor: C.Rumor; seal: NostrEvent } | { ok: false; retry: boolean }

const KEEP = 30 * 86400 // §4: a wrap waiting for its author's chain is kept 30 days, then given up
/** wraps waiting for their author's chain: wrap id → (author, the head it was tried against) */
const waiting = new Map<string, { author: string; head: string }>()
const headId = (pk: string) => { const c = chainOf(pk); return c ? C.headOf(c).id : '' }

/** A sealed wrap to me: its rumor when it opens and is its author's. retry: keep the wrap and try
 *  again (no key yet; the author's device not in a chain I have, or their chain unreadable: again
 *  at each new roster of theirs and at each start, for 30 days). Not retried: a wrap that doesn't
 *  decrypt with the right letterbox secret, or whose seal signature is bad. */
export async function openDm(wrap: NostrEvent, belfry?: string): Promise<Opened> {
  const lb = C.wrapLb(wrap)
  if (!lb || !active()) return { ok: false, retry: true }
  const w = waiting.get(wrap.id)
  if (w && headId(w.author) === w.head) return { ok: false, retry: nowS() - wrap.created_at < KEEP } // nothing new for that author
  const secret = keys.get(lb)
  if (!secret) { waitingForKey = true; return { ok: false, retry: true } }
  let o: { seal: NostrEvent; rumor: C.Rumor }
  try { o = C.openSealed(hexToBytes(secret), wrap) } catch { return { ok: false, retry: false } }
  if (belfry && o.seal.pubkey === belfry) return C.accept(null, o.seal, o.rumor, belfry) ? { ok: true, ...o } : { ok: false, retry: false }
  let author = chainOf(o.rumor.pubkey)
  if (!author || !C.hasDevice(author, o.seal.pubkey, o.rumor.created_at)) author = await loadChain(o.rumor.pubkey, author ? 60e3 : 0)
  if (!author || !C.accept(author, o.seal, o.rumor, belfry)) {
    waiting.set(wrap.id, { author: o.rumor.pubkey, head: headId(o.rumor.pubkey) })
    return { ok: false, retry: nowS() - wrap.created_at < KEEP }
  }
  waiting.delete(wrap.id)
  if (o.rumor.kind === C.HISTORY) { takeHistory(o.rumor, o.seal); return { ok: false, retry: false } }
  return { ok: true, ...o }
}

/** §7: a delivery report encrypted to one of my letterboxes (its lb tag); undefined without the key. */
export function decryptForLb(lb: string, peer: string, content: string): string | undefined {
  const secret = keys.get(lb)
  if (!secret) return undefined
  return nip44.v2.decrypt(content, nip44.v2.utils.getConversationKey(hexToBytes(secret), peer))
}

// ---- sealing (dm.ts) ------------------------------------------------------------------------

/** The wraps of a sealed message: one per participant's head letterbox and one to mine, sealed by
 *  this device (no signer call). Throws (no text in it) when a letterbox is unknown or it's too big. */
export function sealWraps(rumor: C.Rumor, to: string[]): NostrEvent[] {
  if (!device) throw new Error('This device has no key for sealed messages.')
  const r = C.withId(rumor)
  if (!C.fits(r)) throw new Error('This message is too long to send sealed.')
  return [...new Set([...to, me()])].map((p) => {
    const lb = lbFor(p)
    if (!lb) throw new Error('A letterbox is missing: try again in a moment.')
    return C.sealToLetterbox(device!.secret, r, p, lb)
  })
}

/** §7 out: a mail to the belfry's key sealed by this device (wrap to the belfry, no lb), when my
 *  chain is ready; undefined otherwise (today's path). */
export function sealMailOut(rumor: C.Rumor, belfry: string): NostrEvent | undefined {
  if (!device || !readyChain(me())) return undefined
  return C.sealToKey(device.secret, C.withId(rumor), belfry, [['p', belfry]])
}

// ---- my device (§5) -----------------------------------------------------------------------

export interface SessionRow extends C.SessionRow { bunker_pubkey?: string }
let sessionsCache: { at: number; rows: SessionRow[] } | null = null
export const sessionsNow = () => sessionsCache?.rows
export async function mySessions(maxAgeMs = 60e3): Promise<SessionRow[] | null> {
  if (sessionsCache && Date.now() - sessionsCache.at < maxAgeMs) return sessionsCache.rows
  try {
    const r = await signedFetch(`${BELFRY.api}/me/sessions`)
    if (!r.ok) return null
    const d = await r.json() as { sessions?: SessionRow[] }
    sessionsCache = { at: Date.now(), rows: d.sessions ?? [] }
    return sessionsCache.rows
  } catch { return null }
}

const withLock = <T,>(f: () => Promise<T>): Promise<T> =>
  (navigator.locks?.request ? navigator.locks.request('belfry-sealed-' + me(), f) as Promise<T> : f())

async function publishRoster(a: { n: number; prev?: string; lb: string; devices: string[]; ready?: boolean }) {
  const ev = C.makeRoster(device!.secret, { account: me(), ...a })
  addEvents([ev])
  await enqueueSigned('main', ev)
  void flush()
  return ev
}

/** This device's own path, at start and every 6 h (and after a store change that matters). */
async function myDevice() {
  if (!active()) return
  await withLock(async () => {
    const got = await queryComplete('main', [{ kinds: [C.ANCHOR], authors: [me()], '#d': [C.ANCHOR_D] }, { kinds: [C.ROSTER], '#p': [me()] }] as Filter[])
    if (got === null) return // nothing known: try later
    addEvents(got)
    const sessions = await mySessions()
    const c = chainOf(me())
    if (!c) return firstDevice(sessions)
    const head = C.headOf(c)
    if (!head.devices.includes(device!.pub) || !keys.has(head.lb)) return ask()
    await writeMyName()
    // §6: ready once every counted chat session is a device of the head
    if (sessions && !C.isReady(c) && C.mayCarryReady(sessions, head.devices)) await publishRoster({ n: head.n + 1, prev: head.id, lb: head.lb, devices: head.devices, ready: true })
    // §5.6: a device whose session is gone leaves (wait 0–60 s, re-read the head, skip if done)
    if (sessions) {
      const live = new Set(sessions.map((s) => s.client_pubkey).filter(Boolean))
      const gone = head.devices.filter((d) => d !== device!.pub && !live.has(d))
      if (gone.length) { await new Promise((r) => setTimeout(r, Math.random() * 60e3)); await rotate(gone) }
    }
  })
}

/** §5.1: no anchor for my account: a letterbox, roster 1 with this device, then the anchor. */
async function firstDevice(sessions: SessionRow[] | null) {
  const lbSecret = randomBytes(32)
  const lb = getPublicKey(lbSecret)
  const ready = !!sessions && C.mayCarryReady(sessions, [device!.pub])
  // an anchor that appeared meanwhile (another device at once): drop mine, ask instead
  const again = await queryComplete('main', [{ kinds: [C.ANCHOR], authors: [me()], '#d': [C.ANCHOR_D] }] as Filter[])
  if (again === null) return
  if (again.some((e) => C.anchorRoot(e, me()))) { addEvents(again); return ask() }
  keys.set(lb, bytesToHex(lbSecret)); persistKeys()
  const root = await publishRoster({ n: 1, lb, devices: [device!.pub], ready })
  await enqueue('main', { kind: C.ANCHOR, tags: [['d', C.ANCHOR_D], ['root', root.id]] })
  void flush()
}

let linkSecret: string | undefined // §5.2: from #l= of a Link-a-phone address (kept until the request is out)
const LINK = 'belfry-sealed-link'
/** At page load, before the router: chat.<domain>/login/<username>#l=<64 hex> (or #/login/<username>#l=…):
 *  the secret and the username it was for go to sessionStorage, and leave the address. */
export function takeLinkSecret() {
  const m = /(?:^#|[#&?])l=([0-9a-f]{64})(?![0-9a-f])/i.exec(location.hash)
  if (m) {
    const user = (/^\/login\/([a-z0-9._-]{1,30})\/?$/i.exec(location.pathname)?.[1] ?? /^#\/login\/([a-z0-9._-]{1,30})/i.exec(location.hash)?.[1] ?? '').toLowerCase()
    try { sessionStorage.setItem(LINK, JSON.stringify({ s: m[1].toLowerCase(), u: user })) } catch { /* private mode */ }
    const rest = location.hash.replace(m[0], '').replace(/^#?$/, '')
    history.replaceState(history.state, '', location.pathname + location.search + (rest && rest !== '#' ? rest : ''))
  }
}
/** The link secret, only when it was for the account now signed in (else it goes). */
function linkFor(username: string): string | undefined {
  let v: { s?: string; u?: string } = {}
  try { v = JSON.parse(sessionStorage.getItem(LINK) ?? '{}') } catch { /* private mode */ }
  if (!v.s) return undefined
  if (v.u && v.u !== username) { dropLinkSecret(); return undefined }
  return v.s
}
export const dropLinkSecret = () => { linkSecret = undefined; try { sessionStorage.removeItem(LINK) } catch { /* private mode */ } }
let myUsername = ''
export const setSealedUsername = (u: string) => { myUsername = u.toLowerCase() }

/** §5.2: this device isn't in my head: ask (again every 24 h while waiting). */
async function ask() {
  linkSecret = linkFor(myUsername)
  if (!linkSecret && Date.now() - askedAt < 24 * 3600e3) return
  const ev = C.deviceRequest(device!.secret, me(), linkSecret)
  addEvents([ev])
  await enqueueSigned('main', ev)
  void flush()
  askedAt = Date.now(); save(K.asked, askedAt)
  dropLinkSecret()
}

// requests on my other devices ------------------------------------------------------------
export interface Asking { id: string; device: string; name: string; at: number }
let asking: Asking | undefined
const answered = new Set<string>()
export const pendingAsk = () => asking
let linked: string | undefined // Link a phone: the device that came in by scanning my code
export const linkedName = () => linked
export const clearLinkedName = () => { linked = undefined; touch() }
const shownCodes: { secret: string; at: number }[] = [] // Link a phone: codes shown in the last 15 min (memory only)
export function newLinkCode(): string { const s = bytesToHex(randomBytes(32)); shownCodes.push({ secret: s, at: Date.now() }); return s }

let headsSeen = ''
async function onStore() {
  if (!active()) return
  // a head changed (mine or someone's): what waits in the outbox goes to the new letterboxes
  const heads = Object.entries(pins).map(([pk, p]) => pk + p.head).join()
  if (heads !== headsSeen) { headsSeen = heads; await resealQueued(outboxItems() as unknown as OutboxItemLike[], (o) => putOutbox(o as never)); void writeMyName() }
  // a hand-over to this device (§5.5)
  for (const w of all().filter((e) => e.kind === 1059 && e.tags.some((t) => t[0] === 'p' && t[1] === device!.pub))) {
    if (answered.has(w.id)) continue
    answered.add(w.id)
    try {
      const { seal, rumor } = C.openKeys(device!.secret, w)
      // the giver: a device of any roster of my chain, or after a fresh start of the one pinned before
      const prev = pins[me()]?.prevRoot
      const old = prev ? C.chainFromRoot(me(), prev, all().filter((e) => e.kind === C.ROSTER && e.tags.some((t) => t[0] === 'p' && t[1] === me()))) : null
      const chains = [chainOf(me()), old].filter(Boolean) as C.Chain[]
      const got = C.acceptKeys(me(), chains, seal, rumor)
      let added = false
      for (const k of got) if (!keys.has(k.lb)) { keys.set(k.lb, k.secret); added = true }
      if (added) { persistKeys(); waitingForKey = false; replayWraps(); touch(); setTimeout(() => void myDevice(), 2000) /* in the head now: my name (§3.5) */ }
    } catch { /* not a hand-over, or not for us */ }
  }
  // a request from a new device of mine (§5.2)
  const c = chainOf(me())
  if (!c || !deviceIn()) return
  const head = C.headOf(c)
  for (const r of all().filter((e) => e.kind === C.REQUEST && e.tags.some((t) => t[0] === 'p' && t[1] === me())).sort((a, b) => b.created_at - a.created_at)) {
    if (head.devices.includes(r.pubkey)) { if (asking?.device === r.pubkey) { asking = undefined; touch() } continue }
    if (answered.has(r.id) || asking?.id === r.id) continue
    if (!C.validEvent(r as unknown)) { answered.add(r.id); continue }
    const sessions = await mySessions(0)
    const row = sessions?.find((s) => s.client_pubkey === r.pubkey && s.chat === true)
    if (!row) { answered.add(r.id); continue } // not a live chat session of mine: ignored
    const fresh = shownCodes.filter((x) => Date.now() - x.at < 15 * 60e3)
    if (fresh.some((x) => C.linkOk(x.secret, r))) { answered.add(r.id); await allow(r.pubkey); linked = row.device || row.application_name || 'Your new device'; touch(); continue }
    asking = { id: r.id, device: r.pubkey, name: row.device || row.application_name || 'A new device', at: r.created_at * 1000 }
    touch()
    break
  }
}

/** Allow: hand the whole key chain to the new device, then roster n+1 with it (same letterbox). */
export async function allow(newDevice: string) {
  await withLock(async () => {
    await loadChain(me(), 0)
    const c = chainOf(me())
    if (!c || !deviceIn()) throw new Error('This device can’t allow others right now.')
    const head = C.headOf(c)
    if (head.devices.includes(newDevice)) return // another device answered first
    const w = C.handKeys(device!.secret, me(), [...keys].map(([lb, secret]) => ({ lb, secret })), newDevice)
    await enqueueSigned('main', w)
    const sessions = await mySessions(0)
    const devices = [...head.devices, newDevice]
    await publishRoster({ n: head.n + 1, prev: head.id, lb: head.lb, devices, ready: C.isReady(c) || (!!sessions && C.mayCarryReady(sessions, devices)) })
  })
  if (asking?.device === newDevice) { answered.add(asking.id); asking = undefined }
  touch()
}

/** Not me: end that device's session; no roster change. */
export async function refuse(newDevice: string) {
  const sessions = await mySessions(0)
  const row = sessions?.find((s) => s.client_pubkey === newDevice)
  if (row?.bunker_pubkey) {
    const r = await signedFetch(`${BELFRY.api}/me/sessions/revoke`, 'POST', { bunker_pubkey: row.bunker_pubkey })
    if (!r.ok) throw new Error('The belfry did not end that session.')
  }
  if (asking?.device === newDevice) { answered.add(asking.id); asking = undefined }
  touch()
}

/** §5.6: a new letterbox, sent to each remaining device, roster n+1 without the removed ones. */
export async function rotate(gone: string[]) {
  await loadChain(me(), 0)
  const c = chainOf(me())
  if (!c || !deviceIn()) return
  const head = C.headOf(c)
  const left = head.devices.filter((d) => !gone.includes(d))
  if (left.length === head.devices.length || !left.length) return // done already, or nobody left
  const lbSecret = randomBytes(32)
  const lb = getPublicKey(lbSecret)
  keys.set(lb, bytesToHex(lbSecret)); persistKeys()
  for (const d of left) if (d !== device!.pub) await enqueueSigned('main', C.handKeys(device!.secret, me(), [{ lb, secret: bytesToHex(lbSecret) }], d))
  await publishRoster({ n: head.n + 1, prev: head.id, lb, devices: left, ready: C.isReady(c) })
}

/** Remove one of my devices (Me → Devices): its session ends, then the letterbox changes. */
export async function removeMyDevice(d: string) {
  if (d === device?.pub) throw new Error('Use Sign out for this device.')
  const sessions = await mySessions(0)
  const row = sessions?.find((s) => s.client_pubkey === d)
  if (row?.bunker_pubkey) {
    const r = await signedFetch(`${BELFRY.api}/me/sessions/revoke`, 'POST', { bunker_pubkey: row.bunker_pubkey })
    if (!r.ok) throw new Error('The belfry did not end that session.')
  }
  sessionsCache = null
  await withLock(() => rotate([d]))
}

// ---- shared history (§8) ----------------------------------------------------------------------

let historyListener: ((rumors: C.Rumor[]) => void) | undefined
export const onHistory = (f: (rumors: C.Rumor[]) => void) => { historyListener = f }

function takeHistory(h: C.Rumor, seal: NostrEvent) {
  void seal
  const rumors = C.historyRumors(h, me())
  for (const r of rumors) shared[r.id] ??= h.pubkey
  save(K.shared, shared)
  historyListener?.(rumors)
}
export const sharedByOf = (rid: string) => shared[rid]

/** Share a conversation's history with PERSON's new head letterbox (any participant, by a tap). */
export async function shareHistory(person: string, rumors: C.Rumor[], conv: Conv) {
  if (!device) throw new Error('This device has no key for sealed messages.')
  const c = await loadChain(person, 0)
  if (!c) throw new Error('Their new device isn’t ready yet.')
  const { packs } = C.packHistory(me(), person, rumors.map((r) => C.withId(r)), nowS())
  for (const p of packs) await enqueueSigned('main', C.sealToLetterbox(device.secret, p, person, C.headOf(c).lb))
  void flush()
  const f = freshOf[person]
  if (f && !f.shared.includes(conv)) { f.shared.push(conv); save(K.fresh, freshOf) }
  touch()
}

// ---- what the screens show ------------------------------------------------------------------

export const freshStart = (pk: string) => freshOf[pk]
export const firstSeenOf = (conv: Conv) => firstSeen[conv]

// ---- my device names, one kind 30078 per device, d = belfry:device:<device pubkey> (§3.5) ----------

const NAME_D = 'belfry:device:'
interface DeviceName { v: 1; name?: string; kind?: string; added?: number; [k: string]: unknown }
const nameEvents = () => all().filter((e) => e.kind === C.ANCHOR && e.pubkey === me() && e.tags.some((t) => t[0] === 'd' && t[1].startsWith(NAME_D)))
/** The newest name event of each device: device → event. */
function newestNames(): Map<string, NostrEvent> {
  const out = new Map<string, NostrEvent>()
  for (const e of nameEvents()) {
    const d = e.tags.find((t) => t[0] === 'd')![1].slice(NAME_D.length)
    if (!C.isKey(d)) continue
    const o = out.get(d)
    if (!o || e.created_at > o.created_at || (e.created_at === o.created_at && e.id < o.id)) out.set(d, e)
  }
  return out
}

/** The names my devices gave themselves (each opened with the key chain), only devices of a roster of my chain. */
export function deviceNames(): Record<string, DeviceName> {
  const c = chainOf(me())
  if (!c) return {}
  const inAny = C.allDevices(c)
  const out: Record<string, DeviceName> = {}
  for (const [d, e] of newestNames()) {
    if (!inAny.has(d)) continue
    const lb = e.tags.find((t) => t[0] === 'lb')?.[1]
    const secret = lb && keys.get(lb)
    if (!lb || !secret) continue
    try {
      const v = JSON.parse(nip44.v2.decrypt(e.content, nip44.v2.utils.getConversationKey(hexToBytes(secret), lb))) as DeviceName
      if (v && v.v === 1) out[d] = v
    } catch { /* not readable here */ }
  }
  return out
}

/** This device's own name: its row of /api/me/sessions (what the Allow prompt showed), else the browser's. */
function ownName(sessions: SessionRow[] | null): { name: string; kind: string } {
  const row = sessions?.find((x) => x.client_pubkey === device?.pub)
  const ua = navigator.userAgent
  const browser = /Edg\//.test(ua) ? 'Edge' : /Firefox\//.test(ua) ? 'Firefox' : /Chrome\//.test(ua) ? 'Chrome' : /Safari\//.test(ua) ? 'Safari' : 'Browser'
  const os = /iPhone/.test(ua) ? 'iPhone' : /iPad/.test(ua) ? 'iPad' : /Android/.test(ua) ? 'Android' : /Mac OS X/.test(ua) ? 'Mac' : /Windows/.test(ua) ? 'Windows' : /Linux/.test(ua) ? 'Linux' : ''
  const name = row?.device || (os ? `${browser} on ${os}` : browser)
  const kind = /iPhone|Android.*Mobile/.test(ua) ? 'phone' : /iPad|Android/.test(ua) ? 'tablet' : 'computer'
  return { name, kind }
}

let nameWriting = false
/** §3.5: my own name record, written when this device is in the head and its record isn't under
 *  the head letterbox (it joined, or the letterbox changed). Only this device writes it. */
export async function writeMyName() {
  const c = chainOf(me())
  if (!c || !deviceIn() || nameWriting) return
  const head = C.headOf(c)
  const mine = newestNames().get(device!.pub)
  if (mine && mine.tags.some((t) => t[0] === 'lb' && t[1] === head.lb)) return
  nameWriting = true
  try {
    const sessions = await mySessions()
    const doc: DeviceName = { v: 1, ...ownName(sessions), added: c.added.get(device!.pub)?.created_at ?? nowS() }
    const sk = hexToBytes(keys.get(head.lb)!)
    const content = nip44.v2.encrypt(JSON.stringify(doc), nip44.v2.utils.getConversationKey(sk, head.lb))
    await enqueue('main', { kind: C.ANCHOR, tags: [['d', NAME_D + device!.pub], ['lb', head.lb]], content })
    void flush()
  } finally { nameWriting = false }
}

// ---- re-sealing what waits in the outbox (§9) ---------------------------------------------------

let rumorSource: ((rid: string) => C.Rumor | undefined) | undefined
/** dm.ts: the rumor of a message still in the outbox, rebuilt from what it keeps (never stored in the outbox). */
export const setRumorSource = (f: (rid: string) => C.Rumor | undefined) => { rumorSource = f }

/** A queued sealed wrap whose letterbox is no longer its recipient's head: sealed again to the head. */
export async function resealQueued(items: OutboxItemLike[], put: (o: OutboxItemLike) => Promise<void>) {
  if (!device || !rumorSource) return
  for (const o of items) {
    const w = o.signed
    if (!w || w.kind !== 1059 || !o.dm || o.status === 'failed') continue
    const lb = C.wrapLb(w)
    const to = w.tags.find((t) => t[0] === 'p')?.[1]
    if (!lb || !to) continue
    const want = lbFor(to)
    if (!want || want === lb) continue
    const r = rumorSource(o.dm)
    if (!r) continue
    try {
      const nw = C.sealToLetterbox(device.secret, r, to, want)
      await put({ ...o, signed: nw, content: nw.content, tags: nw.tags, created_at: nw.created_at })
    } catch { /* too big or broken: left as it was */ }
  }
}
export interface OutboxItemLike { localId: string; status: string; dm?: string; signed?: NostrEvent; content: string; tags: string[][]; created_at: number; [k: string]: unknown }
