// Sealed conversations, on this device (docs/sealed.md v1; the core is sealedcore.ts): the key
// chain and what this device remembers, the chains of the people I write to, opening and sealing
// direct messages, and this device's own path (first device, asking, allowing, removing, rotating).
//
// Where things live (plan approved by the lead, 20 Nov): the device key is the session's
// clientSecret (kv 'session'); the key chain, pins, sealed conversations and "shared by" marks are
// ONE kv entry of this account's own IndexedDB ('sealed:state'), wiped exactly when the device key
// changes (sign-out, a new sign-in, Forget this device delete the database). Every write is a
// read-merge-write under a lock, and other tabs of this device reload it (BroadcastChannel): two
// tabs never overwrite each other. Never in localStorage, never logged, never in an error, toast or
// outbox label. Roster writes run under their own lock.
import { getPublicKey, nip44, type Filter, type NostrEvent } from 'nostr-tools'
import { bytesToHex, hexToBytes, randomBytes } from '@noble/hashes/utils'
import { BELFRY } from '../config'
import { kvGet, kvSet } from './db'
import * as C from './sealedcore'
import { addEvents, all, outboxItems, putOutbox, subscribe, touch } from './store'
import { currentSigner, enqueue, enqueueSigned, flush, isLocalOnly, me, onOutbox, publishNow, queryComplete, relayAuthed, replayWraps, setBeforeFlush, setExtraMainFilters, signedFetch } from './sync'

type Conv = string

interface Pin { root: string; head: string; heads: Record<string, string> /* lb of every head accepted, by roster id */; prevRoot?: string /* the root pinned before a fresh start */ }
interface Fresh { root: string; at: number; shared: Conv[] }
/** What this device remembers (one kv entry, merged on every write). */
interface State {
  keys: Record<string, string> // lb → secret hex: my key chain
  pins: Record<string, Pin>
  convs: Conv[] // sealed conversations (§6), never undone
  shared: Record<string, string> // rumor id → who shared it (§8)
  unshared: string[] // marks taken back (the rumor came sealed in the normal way)
  fresh: Record<string, Fresh> // person → their latest fresh start noticed
  first: Record<Conv, number> // conv → when this device first saw it sealed
  asked: number // my last device request
  packs: Record<string, C.Rumor> // history packs still in the outbox (rebuilt if a head changes)
}
const emptyState = (): State => ({ keys: {}, pins: {}, convs: [], shared: {}, unshared: [], fresh: {}, first: {}, asked: 0, packs: {} })
let st: State = emptyState()

/** B into A: the union of what two tabs know (keys, sealed conversations, marks), the earliest
 *  "first seen", the latest request, the pin that knows more heads. */
function merge(a: State, b: State): State {
  const pins: Record<string, Pin> = { ...a.pins }
  for (const [pk, p] of Object.entries(b.pins)) {
    const q = pins[pk]
    if (!q) { pins[pk] = p; continue }
    const heads = { ...q.heads, ...p.heads }
    const winner = q.root === p.root ? (Object.keys(p.heads).length > Object.keys(q.heads).length ? p : q) : (p.prevRoot === q.root ? p : q)
    pins[pk] = { ...winner, heads, prevRoot: winner.prevRoot ?? (q.root !== p.root ? (winner === p ? q.root : p.root) : undefined) }
  }
  const fresh = { ...a.fresh }
  for (const [pk, f] of Object.entries(b.fresh)) {
    const g = fresh[pk]
    fresh[pk] = !g || f.at > g.at ? { ...f, shared: [...new Set([...(g?.root === f.root ? g.shared : []), ...f.shared])] } : { ...g, shared: [...new Set([...g.shared, ...(g.root === f.root ? f.shared : [])])] }
  }
  const first = { ...a.first }
  for (const [c, t] of Object.entries(b.first)) first[c] = first[c] === undefined ? t : Math.min(first[c], t)
  const unshared = [...new Set([...a.unshared, ...b.unshared])]
  const shared = { ...b.shared, ...a.shared }
  for (const r of unshared) delete shared[r]
  return { keys: { ...a.keys, ...b.keys }, pins, convs: [...new Set([...a.convs, ...b.convs])], shared, unshared, fresh, first, asked: Math.max(a.asked, b.asked), packs: { ...b.packs, ...a.packs } }
}

let device: { secret: Uint8Array; pub: string } | null = null
let started = false
let partners: string[] = []
let currentFilters: () => Filter[] = () => []
const nowS = () => Math.floor(Date.now() / 1000)
const KV = 'sealed:state', KV_DEVICE = 'sealed:device'
let channel: BroadcastChannel | undefined

const lockOf = (name: string) => <T,>(f: () => Promise<T>): Promise<T> =>
  (navigator.locks?.request ? navigator.locks.request(`belfry-sealed-${name}-${me()}`, f) as Promise<T> : f())
const withRosterLock = <T,>(f: () => Promise<T>) => lockOf('roster')(f)
const withKvLock = <T,>(f: () => Promise<T>) => lockOf('kv')(f)

/** Change what this device remembers: read what is stored (another tab may have written), merge,
 *  apply CHANGE, write, tell the other tabs. */
async function commit(change: (s: State) => void) {
  await withKvLock(async () => {
    const stored = await kvGet<State>(KV).catch(() => undefined)
    if (stored) st = merge(st, { ...emptyState(), ...stored })
    change(st)
    await kvSet(KV, st).catch(() => { /* full or closed: kept in memory */ })
  })
  channel?.postMessage('changed')
  touch() // the views read again (a fresh start, a new key, a mark)
}

async function reloadFromKv() {
  const before = Object.keys(st.keys).length
  const stored = await kvGet<State>(KV).catch(() => undefined)
  if (stored) st = merge(st, { ...emptyState(), ...stored })
  if (Object.keys(st.keys).length > before) keysArrived()
  touch()
}

let loadedResolve: () => void = () => {}
/** Resolved once startSealed has read what this device remembers (sends wait for it). */
let loaded: Promise<void> = Promise.resolve()

export const active = () => started && !!device
export const devicePub = () => device?.pub ?? ''
/** How many letterbox keys this device holds (tests: a count, never a key). */
export const keyCount = () => Object.keys(st.keys).length
const sealedSet = () => new Set(st.convs)

/** dm.ts: the people of my conversations (up to 200, most recent first): their chains stay live. */
export function setPartners(list: string[]) {
  const next = [...new Set(list)].filter((p) => p !== me()).slice(0, 200).sort()
  if (next.join() === partners.join()) return
  partners = next
  if (started) setExtraMainFilters(currentFilters)
}

// ---- start ----------------------------------------------------------------------------------

/** Called synchronously at sign-in (before anything can be sent) for a member's session on a
 *  belfry: sends wait until startSealed has loaded. Never for a guest or demo session. */
export function expectSealed() { loaded = new Promise((r) => { loadedResolve = r }) }

/** Once signed in on a belfry (a member, not a guest, not demo), after sync.start: CLIENT_SECRET
 *  is this device's key. */
export async function startSealed(clientSecret: string) {
  try {
    if (started || isLocalOnly() || !/^[0-9a-f]{64}$/.test(clientSecret)) return
    const secret = hexToBytes(clientSecret)
    device = { secret, pub: getPublicKey(secret) }
    // the chain belongs to this device key: another key (should not happen: a new session wipes
    // the database) means none of it is ours
    const stored = await kvGet<string>(KV_DEVICE).catch(() => undefined)
    if (stored && stored !== device.pub) await kvSet(KV, emptyState()).catch(() => {})
    else { const s = await kvGet<State>(KV).catch(() => undefined); if (s) st = { ...emptyState(), ...s } }
    await kvSet(KV_DEVICE, device.pub).catch(() => {})
    started = true
    try { channel = new BroadcastChannel('belfry-sealed-' + me()); channel.onmessage = () => void reloadFromKv() } catch { /* old browser: no other tabs told */ }
    currentFilters = () => device ? [
      { kinds: [C.ANCHOR], authors: [me()] }, // my anchor and my devices' name records (d prefix: not filterable)
      { kinds: [C.ROSTER], '#p': [me()] },
      // the people I write to: their rosters and anchors live (§4)
      ...(partners.length ? [{ kinds: [C.ROSTER], '#p': partners }, { kinds: [C.ANCHOR], authors: partners, '#d': [C.ANCHOR_D] }] : []),
      { kinds: [C.REQUEST], '#p': [me()], since: nowS() - 7 * 86400 },
      { kinds: [1059], '#p': [device.pub] }, // hand-overs to this device (never filtered by lb: the relay ignores #lb)
    ] as Filter[] : []
    setExtraMainFilters(currentFilters)
    setBeforeFlush(beforeFlush)
    onOutbox((item) => { if (item.dm && st.packs[item.dm]) void commit((s) => { delete s.packs[item.dm!] }) }, () => {})
    let t: ReturnType<typeof setTimeout> | null = null
    subscribe(() => { t ??= setTimeout(() => { t = null; void onStore() }, 400) })
    setTimeout(() => void myDevice(), 3000)
    setInterval(() => void myDevice(), 6 * 3600e3)
    keysArrived() // once at start: wraps a previous version gave up on are read again
  } finally { loadedResolve() }
}

const keyOf = (lb: string) => st.keys[lb]
async function addKeys(list: C.LbKey[]): Promise<boolean> {
  const fresh = list.filter((k) => !st.keys[k.lb])
  if (!fresh.length) return false
  await commit((s) => { for (const k of fresh) s.keys[k.lb] = k.secret })
  void navigator.storage?.persist?.().catch(() => false) // Safari evicts unvisited sites' storage: ask once we hold keys
  return true
}

let keysListener: (() => void) | undefined
/** dm.ts: keys arrived (or this start): wraps with an lb that were marked seen are read again. */
export const onKeysArrived = (f: () => void) => { keysListener = f }
function keysArrived() { missingLbs.clear(); keysListener?.(); replayWraps(); touch() }

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
  const pin = st.pins[pk]
  const c = anchor ? C.validChain(anchor, evs.filter((e) => e.kind === C.ROSTER && e.tags.some((t) => t[0] === 'p' && t[1] === pk)), pin && pin.root === C.anchorRoot(anchor) ? pin.head : undefined) : null
  memo.of.set(pk, c)
  if (c) notePin(pk, c)
  return c
}

/** People for whom this device holds a complete answer of the relay (after AUTH, to EOSE) since
 *  this start: "no ready chain" is known for them, not guessed (§6). */
const answered = new Set<string>()
const inflight = new Map<string, Promise<C.Chain | null>>()
const askedFor = new Map<string, number>()

/** Ask the relay for these people's anchors and rosters, always without since (§3.3); at most
 *  WAIT ms, then what is known. One question per person at a time. */
async function fetchChains(people: string[], waitMs = 3000): Promise<void> {
  const authed = relayAuthed('main')
  const got = await queryComplete('main', [{ kinds: [C.ANCHOR], authors: people, '#d': [C.ANCHOR_D] }, { kinds: [C.ROSTER], '#p': people }] as Filter[], waitMs)
  if (got === null) return
  addEvents(got)
  if (authed && relayAuthed('main')) for (const p of people) { answered.add(p); askedFor.set(p, Date.now()) }
}

/** One person's chain, asked again when not asked in the last MAX_AGE ms (never twice at once). */
export function loadChain(pk: string, maxAgeMs = 15e3): Promise<C.Chain | null> {
  if (Date.now() - (askedFor.get(pk) ?? 0) < maxAgeMs) return Promise.resolve(chainOf(pk))
  const running = inflight.get(pk)
  if (running) return running
  const p = fetchChains([pk]).then(() => chainOf(pk)).finally(() => inflight.delete(pk))
  inflight.set(pk, p)
  return p
}

/** §3.4: remember the root first accepted and the head last accepted; a new root = they started fresh. */
function notePin(pk: string, c: C.Chain) {
  const head = C.headOf(c)
  const p = st.pins[pk]
  if (p && p.root === c.rosters[0].id && p.head === head.id) return
  void commit((s) => {
    const q = s.pins[pk]
    if (!q) { s.pins[pk] = { root: c.rosters[0].id, head: head.id, heads: { [head.id]: head.lb } }; return }
    if (q.root !== c.rosters[0].id) {
      s.fresh[pk] = { root: c.rosters[0].id, at: c.rosters[0].created_at * 1000, shared: [] }
      s.pins[pk] = { root: c.rosters[0].id, head: head.id, heads: { ...q.heads, [head.id]: head.lb }, prevRoot: q.root }
      return
    }
    q.head = head.id; q.heads[head.id] = head.lb
  })
  if (p && p.root === c.rosters[0].id) p.head = head.id // at once in memory too (commit merges)
}

/** The letterbox to seal to for PK: the head's, else the last head this device accepted (§6). */
function lbFor(pk: string): string | undefined {
  const c = chainOf(pk)
  if (c) return C.headOf(c).lb
  const p = st.pins[pk]
  return p ? p.heads[p.head] : undefined
}

export const readyChain = (pk: string) => { const c = chainOf(pk); return !!c && C.isReady(c) }

// ---- conversations --------------------------------------------------------------------------

const participants = (conv: Conv): string[] => (conv.startsWith('n:') ? conv.slice(2).split(',') : [])
const peopleOf = (conv: Conv) => (conv.startsWith('m:') ? [me()] : [...new Set([...participants(conv), me()])])

function markSealed(conv: Conv) {
  if (sealedSet().has(conv) && st.first[conv] !== undefined) return
  st.convs = [...new Set([...st.convs, conv])]
  if (st.first[conv] === undefined) st.first[conv] = Date.now()
  void commit((s) => { s.convs = [...new Set([...s.convs, conv])]; s.first[conv] ??= Date.now() })
}

/** The lock on a conversation (views): sealed when remembered so, or every participant (me
 *  included) has a ready chain here. An e-mail thread: my own chain (§7). */
export function isSealedConv(conv: Conv): boolean {
  if (!active()) return false
  if (sealedSet().has(conv)) return true
  if (!peopleOf(conv).every(readyChain)) { void ensureChains(conv); return false }
  markSealed(conv)
  return true
}

/** Ask for the chains of a conversation's people not ready here yet (views). */
export async function ensureChains(conv: Conv) {
  if (!active()) return
  const missing = peopleOf(conv).filter((p) => !readyChain(p) && Date.now() - (askedFor.get(p) ?? 0) > 10 * 60e3)
  if (!missing.length) return
  const before = peopleOf(conv).filter(readyChain).length
  await fetchChains(missing)
  if (peopleOf(conv).filter(readyChain).length !== before) touch()
}

/** §4: before each send and when a sealed conversation opens, every participant's anchor and
 *  rosters asked for again (they stay live too); at most 3 s, then what is known is used. */
export async function refreshChains(conv: Conv, waitMs = 3000) {
  if (!active()) return
  await fetchChains(peopleOf(conv), waitMs)
}

/** §6: how a message to CONV goes. 'sealed' when the conversation is remembered sealed or every
 *  participant has a ready chain; 'old' only on knowledge: a complete answer of the relay since
 *  this start shows someone with no ready chain. Otherwise it throws (no text in it): the
 *  composer keeps the text. Waits for startSealed to have loaded. */
export async function sendMode(conv: Conv): Promise<'sealed' | 'old'> {
  await loaded
  if (!active()) return 'old' // demo, guest, or no device key: today's way
  await refreshChains(conv)
  if (sealedSet().has(conv)) return 'sealed'
  const people = peopleOf(conv)
  if (people.every(readyChain)) { markSealed(conv); return 'sealed' }
  if (people.some((p) => !readyChain(p) && answered.has(p))) return 'old'
  throw new Error('Not sent: this device can’t tell yet whether the conversation is locked. Try again in a moment.')
}

/** A sealed rumor was accepted in CONV: the conversation is sealed from now on (§6). */
export const acceptedSealed = (conv: Conv) => { if (active()) markSealed(conv) }

/** This device is in my head and holds its letterbox secret. */
export function deviceIn(): boolean {
  const c = chainOf(me())
  return !!c && !!device && C.headOf(c).devices.includes(device.pub) && !!keyOf(C.headOf(c).lb)
}

/** §5.3: the list says "Locked on this device" for a conversation this device knows whose
 *  participants all have a ready chain, while this device isn't in my head. */
export const lockedConv = (conv: Conv) => active() && !!chainOf(me()) && !deviceIn() && peopleOf(conv).every(readyChain)
/** The banner: this device isn't in my head, or it holds a wrap whose lb is one of MY chain's
 *  letterboxes and lacks its secret (a made-up lb pins nothing). */
const missingLbs = new Set<string>()
export function showBanner(): boolean {
  if (!active()) return false
  const c = chainOf(me())
  if (!c) return false
  if (!deviceIn()) return true
  const mine = new Set(c.rosters.map((r) => r.lb))
  return [...missingLbs].some((lb) => mine.has(lb) && !keyOf(lb))
}
/** Remove is offered only on a device that can change the list. */
export const canRemove = () => deviceIn()

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
  const secret = keyOf(lb)
  if (!secret) { missingLbs.add(lb); return { ok: false, retry: true } }
  let o: { seal: NostrEvent; rumor: C.Rumor }
  try { o = C.openSealed(hexToBytes(secret), wrap) } catch { return { ok: false, retry: false } }
  if (belfry && o.seal.pubkey === belfry) return C.accept(null, o.seal, o.rumor, belfry) ? { ok: true, ...o } : { ok: false, retry: false }
  let author = chainOf(o.rumor.pubkey)
  if (!author || !C.hasDevice(author, o.seal.pubkey, o.rumor.created_at)) author = await loadChain(o.rumor.pubkey)
  if (!author || !C.accept(author, o.seal, o.rumor, belfry)) {
    waiting.set(wrap.id, { author: o.rumor.pubkey, head: headId(o.rumor.pubkey) })
    return { ok: false, retry: nowS() - wrap.created_at < KEEP }
  }
  waiting.delete(wrap.id)
  if (o.rumor.kind === C.HISTORY) { takeHistory(o.rumor); return { ok: false, retry: false } }
  if (st.shared[o.rumor.id]) void commit((s) => { delete s.shared[o.rumor.id]; s.unshared = [...new Set([...s.unshared, o.rumor.id])] }) // came sealed the normal way: no longer "shared"
  return { ok: true, ...o }
}

/** §7: a delivery report encrypted to one of my letterboxes (its lb tag); undefined without the key. */
export function decryptForLb(lb: string, peer: string, content: string): string | undefined {
  const secret = keyOf(lb)
  if (!secret) return undefined
  return nip44.v2.decrypt(content, nip44.v2.utils.getConversationKey(hexToBytes(secret), peer))
}

// ---- sealing (dm.ts) ------------------------------------------------------------------------

/** The wraps of a sealed message: one per participant's head letterbox and one to mine, sealed by
 *  this device (no signer call). Throws (no text in it) when a letterbox is unknown or it's too big:
 *  never the old way. */
export function sealWraps(rumor: C.Rumor, to: string[]): NostrEvent[] {
  if (!device) throw new Error('This device has no key for sealed messages.')
  const r = C.withId(rumor)
  if (!C.fits(r)) throw new Error('This message is too long to send sealed.')
  return [...new Set([...to, me()])].map((p) => {
    const lb = lbFor(p)
    if (!lb) throw new Error('Not sent: a letterbox isn’t known on this device yet. Try again in a moment.')
    return C.sealToLetterbox(device!.secret, r, p, lb)
  })
}

/** §7 out: a mail to the belfry's key sealed by this device (wrap to the belfry, no lb). */
export function sealMailOut(rumor: C.Rumor, belfry: string): NostrEvent {
  if (!device) throw new Error('This device has no key for sealed messages.')
  const r = C.withId(rumor)
  if (!C.fits(r)) throw new Error('This e-mail is too long to send sealed.')
  return C.sealToKey(device.secret, r, belfry, [['p', belfry]])
}

// ---- my device (§5) -----------------------------------------------------------------------

export interface SessionRow extends C.SessionRow { bunker_pubkey?: string }
let sessionsCache: { at: number; rows: SessionRow[] } | null = null
export const sessionsNow = () => sessionsCache?.rows
/** /api/me/sessions, only when well formed (a list of rows); null otherwise. */
export async function mySessions(maxAgeMs = 60e3): Promise<SessionRow[] | null> {
  if (sessionsCache && Date.now() - sessionsCache.at < maxAgeMs) return sessionsCache.rows
  try {
    const r = await signedFetch(`${BELFRY.api}/me/sessions`)
    if (!r.ok) return null
    const d = await r.json() as { sessions?: unknown }
    if (!Array.isArray(d.sessions) || !d.sessions.every((x) => x && typeof x === 'object')) return null
    sessionsCache = { at: Date.now(), rows: d.sessions as SessionRow[] }
    return sessionsCache.rows
  } catch { return null }
}

/** A roster, then the anchor naming it as head (§3.2: a device always anchors its own roster). */
async function publishRoster(c: C.Chain | null, a: { n: number; prev?: string; lb: string; devices: string[]; ready?: boolean }, now = false) {
  const ev = C.makeRoster(device!.secret, { account: me(), ...a })
  const root = c ? c.rosters[0].id : ev.id
  addEvents([ev])
  if (now) {
    await publishNow('main', ev)
    const anchor = await currentSigner().sign({ kind: C.ANCHOR, created_at: nowS(), tags: C.anchorTags(root, ev.id), content: '' })
    await publishNow('main', anchor)
    addEvents([anchor])
  } else {
    await enqueueSigned('main', ev)
    await enqueue('main', { kind: C.ANCHOR, tags: C.anchorTags(root, ev.id) })
    void flush()
  }
  return ev
}

/** This device's own path, at start and every 6 h. */
async function myDevice() {
  if (!active()) return
  const gone = await withRosterLock(async (): Promise<string[]> => {
    if (!relayAuthed('main')) return [] // only an answer given after AUTH says anything (§5.1)
    const got = await queryComplete('main', [{ kinds: [C.ANCHOR], authors: [me()], '#d': [C.ANCHOR_D] }, { kinds: [C.ROSTER], '#p': [me()] }] as Filter[])
    if (got === null || !relayAuthed('main')) return [] // nothing known: try later
    addEvents(got)
    answered.add(me())
    const sessions = await mySessions()
    const c = chainOf(me())
    if (!c) { await firstDevice(sessions); return [] }
    const head = C.headOf(c)
    if (!head.devices.includes(device!.pub) || !keyOf(head.lb)) { await ask(); return [] }
    await writeMyName()
    // §6: ready once every counted chat session is a device of the head
    if (sessions && !C.isReady(c) && C.mayCarryReady(sessions, head.devices)) await publishRoster(c, { n: head.n + 1, prev: head.id, lb: head.lb, devices: head.devices, ready: true })
    // §5.6: removal by absence needs a well-formed list that has this device's own session
    if (!sessions || !sessions.some((s) => s.client_pubkey === device!.pub)) return []
    const live = new Set(sessions.map((s) => s.client_pubkey).filter(Boolean))
    return head.devices.filter((d) => d !== device!.pub && !live.has(d))
  })
  if (!gone.length) return
  await new Promise((r) => setTimeout(r, Math.random() * 60e3)) // outside the lock (§5.6)
  await withRosterLock(async () => {
    const sessions = await mySessions(0)
    if (!sessions || !sessions.some((s) => s.client_pubkey === device!.pub)) return
    const live = new Set(sessions.map((s) => s.client_pubkey).filter(Boolean))
    const still = gone.filter((d) => !live.has(d))
    if (still.length) await rotate(still).catch(() => { /* not in the head any more: nothing to do */ })
  })
}

/** §5.1: no anchor for my account (a complete answer after AUTH said so): a letterbox, roster 1
 *  with this device, then the anchor, both PUBLISHED while the lock is held, then checked again. */
async function firstDevice(sessions: SessionRow[] | null) {
  const again = await queryComplete('main', [{ kinds: [C.ANCHOR], authors: [me()], '#d': [C.ANCHOR_D] }] as Filter[])
  if (again === null || !relayAuthed('main')) return
  if (again.some((e) => C.anchorRoot(e, me()))) { addEvents(again); return ask() } // another device was first
  const lbSecret = randomBytes(32)
  const lb = getPublicKey(lbSecret)
  await addKeys([{ lb, secret: bytesToHex(lbSecret) }])
  const ready = !!sessions && C.mayCarryReady(sessions, [device!.pub])
  try { await publishRoster(null, { n: 1, lb, devices: [device!.pub], ready }, true) } catch { return } // not out: next time
  // two devices at once: the newest anchor decides; if it isn't mine, ask like any new device
  const check = await queryComplete('main', [{ kinds: [C.ANCHOR], authors: [me()], '#d': [C.ANCHOR_D] }] as Filter[])
  if (check) addEvents(check)
  const c = chainOf(me())
  if (c && !C.headOf(c).devices.includes(device!.pub)) await ask()
}

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
/** The link secret, only when its address named the account now signed in (else it goes). */
function linkFor(username: string): string | undefined {
  let v: { s?: string; u?: string } = {}
  try { v = JSON.parse(sessionStorage.getItem(LINK) ?? '{}') } catch { /* private mode */ }
  if (!v.s) return undefined
  if (!v.u || !username || v.u !== username) { dropLinkSecret(); return undefined }
  return v.s
}
export const dropLinkSecret = () => { try { sessionStorage.removeItem(LINK) } catch { /* private mode */ } }
let myUsername = ''
export const setSealedUsername = (u: string) => { myUsername = u.toLowerCase() }

/** §5.2: this device isn't in my head: ask (again every 24 h while waiting). */
async function ask() {
  const linkSecret = linkFor(myUsername)
  if (!linkSecret && Date.now() - st.asked < 24 * 3600e3) return
  const ev = C.deviceRequest(device!.secret, me(), linkSecret)
  addEvents([ev])
  await enqueueSigned('main', ev)
  void flush()
  await commit((s) => { s.asked = Date.now() })
  dropLinkSecret()
}

// requests on my other devices ------------------------------------------------------------
export interface Asking { id: string; device: string; name: string; at: number }
let asking: Asking | undefined
const doneRequests = new Set<string>()
export const pendingAsk = () => asking
let linked: string | undefined // Link a phone: the device that came in by scanning my code
export const linkedName = () => linked
export const clearLinkedName = () => { linked = undefined; touch() }
const shownCodes: { secret: string; at: number }[] = [] // Link a phone: codes shown in the last 15 min (memory only)
export function newLinkCode(): string { const s = bytesToHex(randomBytes(32)); shownCodes.push({ secret: s, at: Date.now() }); return s }

/** hand-overs tried: wrap id → my head when tried (tried again when my chain changes) */
const handoverTried = new Map<string, string>()
let headsSeen = ''

async function onStore() {
  if (!active()) return
  // a head changed (mine or someone's): what waits in the outbox goes to the new letterboxes
  const recipients = queuedRecipients()
  const heads = [me(), ...recipients].map((pk) => pk + headId(pk)).join()
  if (heads !== headsSeen) { headsSeen = heads; await resealQueued(); void writeMyName() }
  await takeHandovers()
  // a request from a new device of mine (§5.2)
  const c = chainOf(me())
  if (!c || !deviceIn()) return
  const head = C.headOf(c)
  for (const r of all().filter((e) => e.kind === C.REQUEST && e.tags.some((t) => t[0] === 'p' && t[1] === me())).sort((a, b) => b.created_at - a.created_at)) {
    if (head.devices.includes(r.pubkey)) { if (asking?.device === r.pubkey) { asking = undefined; touch() } continue }
    if (doneRequests.has(r.id) || asking?.id === r.id) continue
    if (!C.validEvent(r as unknown)) { doneRequests.add(r.id); continue }
    const sessions = await mySessions(30e3)
    if (!sessions) continue // can't tell now: next time
    const row = sessions.find((s) => s.client_pubkey === r.pubkey && s.chat === true)
    if (!row) { doneRequests.add(r.id); continue } // not a live chat session of mine: ignored
    const fresh = shownCodes.filter((x) => Date.now() - x.at < 15 * 60e3)
    if (fresh.some((x) => C.linkOk(x.secret, r))) {
      doneRequests.add(r.id)
      try { await allow(r.pubkey); linked = row.device || row.application_name || 'Your new device' } catch { /* asked again below next time */ }
      touch(); continue
    }
    asking = { id: r.id, device: r.pubkey, name: row.device || row.application_name || 'A new device', at: r.created_at * 1000 }
    touch()
    break
  }
}

/** §5.5: hand-overs to this device; one my chain can't vouch for yet is tried again when my chain changes. */
async function takeHandovers() {
  const myHead = headId(me())
  for (const w of all().filter((e) => e.kind === 1059 && e.tags.some((t) => t[0] === 'p' && t[1] === device!.pub))) {
    if (handoverTried.get(w.id) === myHead || handoverTried.get(w.id) === 'done') continue
    let opened: { seal: NostrEvent; rumor: C.Rumor }
    try { opened = C.openKeys(device!.secret, w) } catch { handoverTried.set(w.id, 'done'); continue } // not one, or not for us
    handoverTried.set(w.id, myHead)
    // the giver: a device of any roster of my chain, or after a fresh start of the one pinned before
    const mineRosters = all().filter((e) => e.kind === C.ROSTER && e.tags.some((t) => t[0] === 'p' && t[1] === me()))
    const prev = st.pins[me()]?.prevRoot
    const chains = [chainOf(me()), prev ? C.chainFromRoot(me(), prev, mineRosters) : null].filter(Boolean) as C.Chain[]
    const got = C.acceptKeys(me(), chains, opened.seal, opened.rumor)
    if (!got.length) { if (!chains.length) void loadChain(me(), 0); continue }
    handoverTried.set(w.id, 'done')
    if (await addKeys(got)) { keysArrived(); setTimeout(() => void myDevice(), 2000) /* in the head now: my name (§3.5) */ }
  }
}

/** Canvas v90: Allow on a 21st device. */
export const FULL = `You already have ${C.MAX_DEVICES} devices. Remove one in Devices, then try again.`

/** Allow: hand the whole key chain to the new device (several 8561 if needed), then roster n+1
 *  with it (same letterbox) and the anchor. At 20 devices: refused, nothing sent. */
export async function allow(newDevice: string) {
  await withRosterLock(async () => {
    await loadChain(me(), 0)
    const c = chainOf(me())
    if (!c || !deviceIn()) throw new Error('This device can’t allow others right now.')
    const head = C.headOf(c)
    if (head.devices.includes(newDevice)) return // another device answered first
    if (head.devices.length >= C.MAX_DEVICES) throw new Error(FULL)
    const sessions = await mySessions(0)
    if (!sessions?.some((s) => s.client_pubkey === newDevice && s.chat === true)) throw new Error('That device isn’t signed in any more.')
    const all_ = Object.entries(st.keys).map(([lb, secret]) => ({ lb, secret }))
    for (const chunk of C.keyChunks(device!.pub, me(), all_, nowS())) await enqueueSigned('main', C.handKeys(device!.secret, me(), chunk, newDevice))
    const devices = [...head.devices, newDevice]
    await publishRoster(c, { n: head.n + 1, prev: head.id, lb: head.lb, devices, ready: C.isReady(c) || C.mayCarryReady(sessions, devices) })
  })
  if (asking?.device === newDevice) { doneRequests.add(asking.id); asking = undefined }
  touch()
}

/** Not me: end that device's session; no roster change. Says so when it can't. */
export async function refuse(newDevice: string) {
  const sessions = await mySessions(0)
  if (!sessions) throw new Error('The belfry didn’t answer: try again in a moment.')
  const row = sessions.find((s) => s.client_pubkey === newDevice)
  if (!row?.bunker_pubkey) { if (asking?.device === newDevice) { doneRequests.add(asking.id); asking = undefined; touch() }; throw new Error('That device isn’t signed in any more.') }
  const r = await signedFetch(`${BELFRY.api}/me/sessions/revoke`, 'POST', { bunker_pubkey: row.bunker_pubkey })
  if (!r.ok) throw new Error('The belfry did not end that session.')
  sessionsCache = null
  if (asking?.device === newDevice) { doneRequests.add(asking.id); asking = undefined }
  touch()
}
/** The Allow card closes without an answer (it comes back next time the app opens). */
export const dismissAsk = () => { asking = undefined; touch() }

/** §5.6: a new letterbox, sent to each remaining device, roster n+1 without the removed ones, the
 *  anchor. Throws when this device can't (not in the head). Run under the roster lock. */
async function rotate(gone: string[]) {
  await loadChain(me(), 0)
  const c = chainOf(me())
  if (!c || !deviceIn()) throw new Error('This device can’t change the list: it isn’t allowed yet.')
  const head = C.headOf(c)
  const left = head.devices.filter((d) => !gone.includes(d))
  if (left.length === head.devices.length) return // done already
  if (!left.length) throw new Error('No device would be left.')
  const lbSecret = randomBytes(32)
  const lb = getPublicKey(lbSecret)
  await addKeys([{ lb, secret: bytesToHex(lbSecret) }]) // this device's other tabs get it through kv
  for (const d of left) if (d !== device!.pub) await enqueueSigned('main', C.handKeys(device!.secret, me(), [{ lb, secret: bytesToHex(lbSecret) }], d))
  await publishRoster(c, { n: head.n + 1, prev: head.id, lb, devices: left, ready: C.isReady(c) })
}

/** Remove one of my devices (Me → Devices): its session ends, then the letterbox changes at once. */
export async function removeMyDevice(d: string) {
  if (d === device?.pub) throw new Error('Use Sign out for this device.')
  const sessions = await mySessions(0)
  if (!sessions) throw new Error('The belfry didn’t answer: try again in a moment.')
  const row = sessions.find((s) => s.client_pubkey === d)
  if (row?.bunker_pubkey) {
    const r = await signedFetch(`${BELFRY.api}/me/sessions/revoke`, 'POST', { bunker_pubkey: row.bunker_pubkey })
    if (!r.ok) throw new Error('The belfry did not end that session.')
  }
  sessionsCache = null
  if (chainOf(me())) await withRosterLock(() => rotate([d]))
  touch()
}

// ---- shared history (§8) ----------------------------------------------------------------------

let historyListener: ((rumors: C.Rumor[]) => void) | undefined
let haveRumor: (rid: string) => boolean = () => false
/** dm.ts: what to add, and which rumors it already holds (those are left as they are). */
export const onHistory = (f: (rumors: C.Rumor[]) => void, have: (rid: string) => boolean) => { historyListener = f; haveRumor = have }

function takeHistory(h: C.Rumor) {
  const rumors = C.historyRumors(h, me()).filter((r) => !haveRumor(r.id))
  if (!rumors.length) return
  void commit((s) => { for (const r of rumors) if (!s.unshared.includes(r.id)) s.shared[r.id] ??= h.pubkey })
  historyListener?.(rumors)
}
export const sharedByOf = (rid: string) => st.shared[rid]

/** Share a conversation's sealed history with PERSON's new head letterbox (any participant, by a tap). */
export async function shareHistory(person: string, rumors: C.Rumor[], conv: Conv) {
  if (!device) throw new Error('This device has no key for sealed messages.')
  const c = await loadChain(person, 0)
  if (!c) throw new Error('Their new device isn’t ready yet.')
  const { packs } = C.packHistory(me(), person, rumors.map((r) => C.withId(r)), nowS())
  await commit((s) => { for (const p of packs) s.packs[p.id] = p })
  for (const p of packs) await enqueueSigned('main', C.sealToLetterbox(device.secret, p, person, C.headOf(c).lb), p.id)
  void flush()
  await commit((s) => { const f = s.fresh[person]; if (f && !f.shared.includes(conv)) f.shared.push(conv) })
  touch()
}

// ---- what the screens show ------------------------------------------------------------------

export const freshStart = (pk: string) => st.fresh[pk]
export const firstSeenOf = (conv: Conv) => st.first[conv]

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
    const secret = lb && keyOf(lb)
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
    const sk = hexToBytes(keyOf(head.lb)!)
    const content = nip44.v2.encrypt(JSON.stringify(doc), nip44.v2.utils.getConversationKey(sk, head.lb))
    await enqueue('main', { kind: C.ANCHOR, tags: [['d', NAME_D + device!.pub], ['lb', head.lb]], content })
    void flush()
  } finally { nameWriting = false }
}

// ---- re-sealing what waits in the outbox (§9) ---------------------------------------------------

let rumorSource: ((rid: string) => C.Rumor | undefined) | undefined
/** dm.ts: the rumor of a message still in the outbox (14, 1301, 7, 5), kept by dm.ts until it is
 *  out; never stored in the outbox itself. */
export const setRumorSource = (f: (rid: string) => C.Rumor | undefined) => { rumorSource = f }

const sealedQueued = () => outboxItems().filter((o) => o.signed?.kind === 1059 && !!o.dm && o.signed.tags.some((t) => t[0] === 'lb'))
function queuedRecipients(): string[] {
  return [...new Set(sealedQueued().map((o) => o.signed!.tags.find((t) => t[0] === 'p')?.[1]).filter((p): p is string => !!p))]
}

/** Queued sealed wraps (failed ones too) whose letterbox is no longer their recipient's head:
 *  sealed again to the head, from the rumor dm.ts keeps or the history pack kept here. */
async function resealQueued() {
  if (!device) return
  for (const o of sealedQueued()) {
    const w = o.signed!
    const lb = C.wrapLb(w)
    const to = w.tags.find((t) => t[0] === 'p')?.[1]
    if (!lb || !to) continue
    const want = lbFor(to)
    if (!want || want === lb) continue
    const r = st.packs[o.dm!] ?? rumorSource?.(o.dm!)
    if (!r) continue
    try {
      const nw = C.sealToLetterbox(device.secret, r, to, want)
      await putOutbox({ ...o, signed: nw, content: nw.content, tags: nw.tags, created_at: nw.created_at, status: o.status === 'failed' ? 'queued' : o.status, error: undefined })
    } catch { /* too big or broken: left as it was */ }
  }
}

/** sync.flush: before sealed wraps go out (a reconnect, a retry), their recipients' heads are
 *  asked for again (3 s at most) and the wraps re-sealed when a head moved. */
async function beforeFlush() {
  if (!active()) return
  const to = queuedRecipients()
  if (!to.length) return
  await fetchChains(to)
  await resealQueued()
}
