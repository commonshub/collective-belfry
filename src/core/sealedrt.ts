// Sealed conversations, on this device (docs/sealed.md v1; the core is sealedcore.ts): the key
// chain and what this device remembers, the chains of the people I write to, opening and sealing
// direct messages, and this device's own path (first device, asking, allowing, removing, rotating).
//
// Where things live (plan approved by the lead, 20 Nov): the device key is the session's
// clientSecret (kv 'session'); the key chain, pins, sealed conversations and "shared by" marks are
// ONE kv entry of this account's own IndexedDB ('sealed:state'), wiped exactly when the device key
// changes (sign-out, a new sign-in, Forget this device delete the database). Every write is a
// read-merge-write under a lock, and other tabs of this device reload it (BroadcastChannel); what
// can be deleted (history packs) has one kv entry per item, so a delete never comes back. Never in
// localStorage, never logged, never in an error, toast or outbox label.
//
// A change of my roster (§3.2) is done only when the relay accepted the roster and the anchor
// naming it and, read again, the roster is on the anchored path; otherwise it is done again on the
// new head, and the action shows as not done until then.
import { getPublicKey, nip44, type Filter, type NostrEvent } from 'nostr-tools'
import { bytesToHex, hexToBytes, randomBytes } from '@noble/hashes/utils'
import { BELFRY } from '../config'
import { kvDel, kvDelPrefix, kvGet, kvPrefix, kvSet } from './db'
import * as C from './sealedcore'
import { addEvents, all, outboxItems, putOutbox, subscribe, touch } from './store'
import { connectionOf, queryPaged, setPagedMainFilters, currentSigner, enqueue, enqueueSigned, flush, isLocalOnly, me, onEose, onOutbox, publishNow, queryComplete, relayAuthed, replayWraps, setBeforeFlush, setExtraMainFilters, signedFetch } from './sync'

type Conv = string

/** What this client remembers of a person's chain: the core's pin (sealedcore.follow, the vectors'
 *  reference), plus the letterbox of every head it accepted (sealing falls back to the last one). */
type Pin = C.Pin & { heads: Record<string, string> }
interface Fresh { root: string; at: number; shared: Conv[]; head?: string /* a switch: the new branch's head */ }
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
}
const emptyState = (): State => ({ keys: {}, pins: {}, convs: [], shared: {}, unshared: [], fresh: {}, first: {}, asked: 0 })
let st: State = emptyState()

/** B into A: the union of what two tabs know (keys, sealed conversations, marks), the earliest
 *  "first seen", the latest request, the pin that knows more of its path. */
function merge(a: State, b: State): State {
  const pins: Record<string, Pin> = { ...a.pins }
  for (const [pk, p] of Object.entries(b.pins)) {
    const q = pins[pk]
    if (!q) { pins[pk] = p; continue }
    // the same root: the pin that has seen further; another root: the one that started fresh from the other
    const winner = q.root === p.root ? (p.path.length > q.path.length ? p : q) : (p.prev_root === q.root ? p : q)
    pins[pk] = { ...winner, heads: { ...q.heads, ...p.heads } }
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
  return { keys: { ...a.keys, ...b.keys }, pins, convs: [...new Set([...a.convs, ...b.convs])], shared, unshared, fresh, first, asked: Math.max(a.asked, b.asked) }
}

let device: { secret: Uint8Array; pub: string } | null = null
let started = false
let partners: string[] = []
let currentFilters: () => Filter[] = () => []
const nowS = () => Math.floor(Date.now() / 1000)
const KV = 'sealed:state', KV_DEVICE = 'sealed:device', KV_PACK = 'sealed:pack:', KV_PATH = 'sealed:path:'
/** The pinned head's rosters per person (their own kv entry each: kept so the pin holds even if
 *  the store lacks them; only for me and the people of my conversations). */
const pinPaths = new Map<string, NostrEvent[]>()
function savePath(pk: string, path: NostrEvent[]) {
  pinPaths.set(pk, path)
  if (pk === me() || convPeople.has(pk)) void kvSet(KV_PATH + pk, path).catch(() => {})
}
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
  const before = new Set(Object.keys(st.keys))
  const stored = await kvGet<State>(KV).catch(() => undefined)
  if (stored) st = merge(st, { ...emptyState(), ...stored })
  const fresh = Object.keys(st.keys).filter((lb) => !before.has(lb))
  if (fresh.length) keysArrived(fresh)
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
  const all_ = new Set([...list].filter((p) => p !== me()))
  const next = [...all_].slice(0, 200).sort()
  // someone who became a partner after their chain was pinned: their path kept from now on
  for (const pk of all_) if (!convPeople.has(pk) && pinPaths.has(pk)) void kvSet(KV_PATH + pk, pinPaths.get(pk)).catch(() => {})
  // people with no conversation at all any more: their kept paths go (not the 200-cut: that is only the subscription)
  for (const pk of [...pinPaths.keys()]) if (pk !== me() && !all_.has(pk)) { pinPaths.delete(pk); void kvDel(KV_PATH + pk).catch(() => {}) }
  convPeople = all_
  if (next.join() === partners.join()) return
  partners = next
  if (started) setExtraMainFilters(currentFilters)
}
let convPeople = new Set<string>()

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
    if (stored && stored !== device.pub) {
      await kvSet(KV, emptyState()).catch(() => {})
      for (const pre of [KV_PACK, KV_PATH, KV_ANSWERED, KV_HANDED, 'dm-held:', 'dm-out:']) await kvDelPrefix(pre).catch(() => {})
    } else { const s = await kvGet<State>(KV).catch(() => undefined); if (s) st = { ...emptyState(), ...s } }
    for (const [pk, p] of Object.entries(st.pins)) if (!Array.isArray((p as Partial<Pin>).path)) delete st.pins[pk] // an earlier shape
    for (const path of await kvPrefix<NostrEvent[]>(KV_PATH).catch(() => [] as NostrEvent[][])) { const pk = path[0]?.tags.find((t) => t[0] === 'p')?.[1]; if (pk) pinPaths.set(pk, path) }
    await kvSet(KV_DEVICE, device.pub).catch(() => {})
    for (const p of await kvPrefix<C.Rumor>(KV_PACK).catch(() => [] as C.Rumor[])) packs.set(p.id, p)
    for (const a of await kvPrefix<{ id: string; at?: number }>(KV_ANSWERED).catch(() => [] as { id: string; at?: number }[])) {
      if (Date.now() - (a.at ?? 0) > 30 * 86400e3) { void kvDel(KV_ANSWERED + a.id).catch(() => {}); continue } // older than the 7-day request window, long since
      doneRequests.add(a.id)
    }
    for (const h of await kvPrefix<{ dev: string; at: number }>(KV_HANDED).catch(() => [] as { dev: string; at: number }[])) handedAt.set(h.dev, h.at)
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
    // read whole, page by page, after each answer (the live subscription gets at most a page per filter)
    setPagedMainFilters(() => device ? [{ kinds: [C.ROSTER], '#p': [me()] }, { kinds: [C.ANCHOR], authors: [me()] }, { kinds: [1059], '#p': [device.pub] }] as Filter[] : [])
    setBeforeFlush(beforeFlush)
    onOutbox((item) => { if (item.dm && packs.has(item.dm)) void dropPack(item.dm) }, () => {})
    // the relay answered after AUTH: what was waiting for an answer runs (my own path, held messages)
    onEose((t) => { if (t === 'main' && relayAuthed('main')) { if (pathDue) scheduleMyDevice(0); decidable() } })
    let t: ReturnType<typeof setTimeout> | null = null
    subscribe(() => { t ??= setTimeout(() => { t = null; void onStore() }, 400) })
    scheduleMyDevice(3000)
    setInterval(() => scheduleMyDevice(0), 6 * 3600e3)
  } finally { loadedResolve() }
}

const keyOf = (lb: string) => st.keys[lb]
async function addKeys(list: C.LbKey[]): Promise<string[]> {
  const fresh = list.filter((k) => !st.keys[k.lb])
  if (!fresh.length) return []
  await commit((s) => { for (const k of fresh) s.keys[k.lb] = k.secret })
  void navigator.storage?.persist?.().catch(() => false) // Safari evicts unvisited sites' storage: ask once we hold keys
  return fresh.map((k) => k.lb)
}

let keysListener: ((lbs: string[]) => void) | undefined
/** dm.ts: letterbox keys arrived: wraps naming them that were marked seen are read again. */
export const onKeysArrived = (f: (lbs: string[]) => void) => { keysListener = f }
function keysArrived(lbs: string[]) { for (const lb of lbs) missingLbs.delete(lb); keysListener?.(lbs); replayWraps(); touch() }

let decideListener: (() => void) | undefined
/** dm.ts: held messages are decided again (a complete answer arrived). */
export const onDecidable = (f: () => void) => { decideListener = f }
function decidable() { decideListener?.() }

// ---- chains ---------------------------------------------------------------------------------

let memo: { v: number; of: Map<string, C.Chain | null> } = { v: -1, of: new Map() }
let storeV = 0
subscribe(() => { storeV++ })

/** A person's valid chain from what the store holds plus the pinned path (null: none, or not loaded yet). */
export function chainOf(pk: string): C.Chain | null {
  if (memo.v !== storeV) memo = { v: storeV, of: new Map() }
  if (memo.of.has(pk)) return memo.of.get(pk)!
  const evs = all()
  const anchor = C.newestAnchor(evs.filter((e) => e.kind === C.ANCHOR && e.pubkey === pk), pk)
  const pin = st.pins[pk]
  const rosters: NostrEvent[] = evs.filter((e) => e.kind === C.ROSTER && e.tags.some((t) => t[0] === 'p' && t[1] === pk))
  const have = new Set(rosters.map((e) => e.id))
  for (const e of pinPaths.get(pk) ?? []) if (!have.has(e.id)) rosters.push(e) // the pin's rosters stay known (review caveat)
  // §3.3/§3.4 as the vectors fix it: the chain this client follows, its pin after, what happened
  const r = anchor ? C.follow(anchor, rosters, pin ?? null) : { chain: null, pin: pin ?? null, event: 'none' as const }
  memo.of.set(pk, r.chain)
  if (r.chain && r.pin && (!pin || r.event !== 'none' || JSON.stringify({ ...pin, heads: undefined }) !== JSON.stringify({ ...r.pin, heads: undefined }))) notePin(pk, r.chain, r.pin, r.event, rosters)
  return r.chain
}

/** People for whom this device holds a complete answer of the relay (after AUTH, to EOSE, on a
 *  live connection): when; and when last asked at all (a back-off when the relay doesn't answer). */
const answeredAt = new Map<string, { at: number; conn: number }>()
const triedAt = new Map<string, number>()
const KNOW = 10 * 60e3 // §6: knowledge lasts 10 minutes
const known = (pk: string) => { const a = answeredAt.get(pk); return !!a && relayAuthed('main') && a.conn === connectionOf('main') && Date.now() - a.at < KNOW }
const answeredRecently = (pk: string, ms: number) => { const a = answeredAt.get(pk); return !!a && Date.now() - a.at < ms }

/** Ask the relay for these people's anchors and rosters, always without since (§3.3); at most
 *  WAIT ms, then what is known. True when it answered completely after AUTH. */
async function fetchChains(people: string[], waitMs = 3000, totalMs?: number): Promise<boolean> {
  if (!people.length) return true
  for (const p of people) triedAt.set(p, Date.now())
  const conn = connectionOf('main')
  const got = await queryPaged('main', [{ kinds: [C.ANCHOR], authors: people, '#d': [C.ANCHOR_D] }, { kinds: [C.ROSTER], '#p': people }] as Filter[], waitMs, 100, totalMs)
  if (got === null) return false
  addEvents(got)
  if (conn < 0 || connectionOf('main') !== conn) return false // asked before AUTH, or the socket changed
  for (const p of people) answeredAt.set(p, { at: Date.now(), conn })
  decidable()
  return true
}

const inflight = new Map<string, Promise<C.Chain | null>>()
/** One person's chain, asked again when not asked in the last MIN_GAP ms (never twice at once). */
export function loadChain(pk: string, minGapMs = 30e3): Promise<C.Chain | null> {
  if (Date.now() - (triedAt.get(pk) ?? 0) < minGapMs) return Promise.resolve(chainOf(pk))
  const running = inflight.get(pk)
  if (running) return running
  const p = fetchChains([pk]).then(() => chainOf(pk)).finally(() => inflight.delete(pk))
  inflight.set(pk, p)
  return p
}

/** The pin after a read (sealedcore.follow): kept at once in memory and in kv; a fresh start or a
 *  switch tells it in each conversation with that person (the amber line); a switch of my own
 *  account: is this device in the new head? */
function notePin(pk: string, c: C.Chain, next: C.Pin, event: C.FollowEvent, rosterEvents: NostrEvent[]) {
  const head = C.headOf(c)
  const old = st.pins[pk]
  const pin: Pin = { ...next, heads: { ...(event === 'fresh_start' ? {} : old?.heads ?? {}), [head.id]: head.lb } }
  st.pins[pk] = pin
  const ids = new Set(c.rosters.map((r) => r.id))
  savePath(pk, rosterEvents.filter((e) => ids.has(e.id)))
  const fresh: Fresh | undefined = event === 'fresh_start' ? { root: c.rosters[0].id, at: c.rosters[0].created_at * 1000, shared: [] }
    : event === 'switch' ? { root: c.rosters[0].id, head: head.id, at: Date.now(), shared: [] } : undefined
  if (fresh) st.fresh[pk] = fresh
  void commit((s) => { s.pins[pk] = { ...pin, heads: { ...(event === 'fresh_start' ? {} : s.pins[pk]?.heads ?? {}), ...pin.heads } }; if (fresh) s.fresh[pk] = fresh })
  if (event === 'switch' && pk === me()) scheduleMyDevice(1000)
}

/** The person allowed DEV through the card on this device: trusted from now on (after a switch). */
function trustDevice(dev: string) {
  const p = st.pins[me()]
  if (!p?.trusted) return
  const next = { ...C.allowLocally(p, dev), heads: p.heads }
  st.pins[me()] = next
  void commit((s) => { const q = s.pins[me()]; if (q) s.pins[me()] = { ...q, trusted: [...new Set([...(q.trusted ?? []), dev])].sort() } })
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
  const missing = peopleOf(conv).filter((p) => !readyChain(p) && Date.now() - (triedAt.get(p) ?? 0) > 10 * 60e3)
  if (!missing.length) return
  const before = peopleOf(conv).filter(readyChain).length
  await fetchChains(missing)
  if (peopleOf(conv).filter(readyChain).length !== before) touch()
}

/** §4: before each send and when a sealed conversation opens, every participant's anchor and
 *  rosters asked for again (they stay live too); at most 3 s, then what is known is used. */
export async function refreshChains(conv: Conv, waitMs = 3000) {
  if (!active()) return
  await fetchChains(peopleOf(conv), waitMs, waitMs) // the whole read within the 3 s (§4), every page included
}

/** §6: how a message to CONV goes. 'sealed': remembered sealed, or every participant has a ready
 *  chain. 'old': only on knowledge (a complete answer within 10 minutes on a live connection shows
 *  someone with no ready chain). 'undecided': this device can't tell now: the message is held on
 *  the device (shown as waiting) and decided at the first complete answer. Waits for the runtime. */
export async function sendMode(conv: Conv, refresh = true): Promise<'sealed' | 'old' | 'undecided'> {
  await loaded
  if (!active()) return 'old' // demo, guest, or no device key: today's way
  if (refresh) await refreshChains(conv)
  if (sealedSet().has(conv)) return 'sealed'
  const people = peopleOf(conv)
  if (people.every(readyChain)) { markSealed(conv); return 'sealed' }
  if (people.some((p) => !readyChain(p) && known(p))) return 'old'
  return 'undecided'
}

/** A sealed rumor was accepted in CONV: the conversation is sealed from now on (§6). */
export const acceptedSealed = (conv: Conv) => { if (active()) markSealed(conv) }

const notInHead = () => { const c = chainOf(me()); return !!c && !!device && !C.headOf(c).devices.includes(device.pub) }
/** This device is in my head and holds its letterbox secret. */
export function deviceIn(): boolean {
  const c = chainOf(me())
  return !!c && !!device && C.headOf(c).devices.includes(device.pub) && !!keyOf(C.headOf(c).lb)
}

/** §5.3: "Locked on this device" (and no composer) only when this device is not in my head. */
export const lockedConv = (conv: Conv) => active() && notInHead() && peopleOf(conv).every(readyChain)
/** The banner: this device isn't in my head, or it holds a wrap whose lb is one of MY chain's
 *  letterboxes and lacks its secret (a made-up lb pins nothing). */
const missingLbs = new Set<string>()
function myLbs(): Set<string> | null {
  const c = chainOf(me())
  if (!c) return null
  const out = new Set(c.rosters.map((r) => r.lb))
  const prev = st.pins[me()]?.prev_root
  if (prev) for (const r of C.chainFromRoot(me(), prev, all().filter((e) => e.kind === C.ROSTER && e.tags.some((t) => t[0] === 'p' && t[1] === me())))?.rosters ?? []) out.add(r.lb)
  return out
}
export function showBanner(): boolean {
  if (!active() || !chainOf(me())) return false
  if (notInHead()) return true
  const mine = myLbs()!
  return [...missingLbs].some((lb) => mine.has(lb) && !keyOf(lb))
}
/** §5.5: this device is in my head but lacks the head key: "Waiting for another of your devices"
 *  in place of the composer of sealed conversations, until a sibling hands it over. */
export const inHeadNoKey = () => { const c = active() ? chainOf(me()) : null; return !!c && !!device && C.headOf(c).devices.includes(device.pub) && !keyOf(C.headOf(c).lb) }
/** Remove is offered only on a device that can change the list. */
export const canRemove = () => deviceIn()

// ---- opening (dm.ts) ------------------------------------------------------------------------

export type Opened = { ok: true; rumor: C.Rumor; seal: NostrEvent } | { ok: false; retry: boolean }

const KEEP = 30 * 86400 // §4: a wrap waiting for its author's chain is kept 30 days, then given up
/** wraps waiting for their author's chain: wrap id → (author, the head it was tried against) */
const waiting = new Map<string, { author: string; head: string }>()
const headId = (pk: string) => { const c = chainOf(pk); return c ? C.headOf(c).id : '' }
const normalAccepted = new Set<string>() // rumor ids that came sealed the normal way (never "shared")

/** A sealed wrap to me: its rumor when it opens and is its author's. retry: keep the wrap and try
 *  again (no key yet; the author's device not in a chain I have, or their chain not read yet:
 *  again at each new roster of theirs and at each start, for 30 days). Not retried: a malformed lb,
 *  an lb that is none of my chain's letterboxes, a wrap that doesn't decrypt with the right secret
 *  or whose seal signature is bad. BELFRY: the belfry's key from belfry.json (its own mail only).
 *  Never waits on the network: an author's chain is asked for, and the wrap tried again when it comes. */
export async function openDm(wrap: NostrEvent, belfry?: string): Promise<Opened> {
  if (C.badLb(wrap)) return { ok: false, retry: false }
  const lb = C.wrapLb(wrap)
  if (!lb || !active()) return { ok: false, retry: true }
  const w = waiting.get(wrap.id)
  if (w && headId(w.author) === w.head) return { ok: false, retry: nowS() - wrap.created_at < KEEP } // nothing new for that author
  const secret = keyOf(lb)
  if (!secret) {
    const mine = myLbs()
    // anyone can name an lb: not one of mine is dropped, but only once my own chain was answered
    // on this connection (a message sealed to my newest letterbox can come before its roster)
    if (mine && !mine.has(lb)) { if (known(me())) return { ok: false, retry: false }; void loadChain(me()); return { ok: false, retry: true } }
    missingLbs.add(lb)
    return { ok: false, retry: true }
  }
  let o: { seal: NostrEvent; rumor: C.Rumor }
  try { o = C.openSealed(hexToBytes(secret), wrap) } catch { return { ok: false, retry: false } }
  if (belfry && o.seal.pubkey === belfry) return C.accept(null, o.seal, o.rumor, belfry) ? { ok: true, ...o } : { ok: false, retry: false }
  // the belfry's own mail before belfry.json gave its key: tried again then (not parked as waiting)
  if (!belfry && o.rumor.kind === C.MAIL && o.rumor.pubkey === o.seal.pubkey) return { ok: false, retry: true }
  const author = chainOf(o.rumor.pubkey)
  if (!author || !C.accept(author, o.seal, o.rumor, belfry)) {
    void loadChain(o.rumor.pubkey) // asked for (backed off per author); the wrap is tried again when it comes
    waiting.set(wrap.id, { author: o.rumor.pubkey, head: headId(o.rumor.pubkey) })
    return { ok: false, retry: nowS() - wrap.created_at < KEEP }
  }
  waiting.delete(wrap.id)
  if (o.rumor.kind === C.HISTORY) { await takeHistory(o.rumor); return { ok: false, retry: false } }
  normalAccepted.add(o.rumor.id)
  if (st.shared[o.rumor.id]) await commit((s) => { delete s.shared[o.rumor.id]; s.unshared = [...new Set([...s.unshared, o.rumor.id])] }) // came sealed the normal way: no longer "shared"
  return { ok: true, ...o }
}

/** §7: a delivery report encrypted to one of my letterboxes (its lb tag); undefined without the key. */
export function decryptForLb(lb: string, peer: string, content: string): string | undefined {
  const secret = keyOf(lb)
  if (!secret) return undefined
  return nip44.v2.decrypt(content, nip44.v2.utils.getConversationKey(hexToBytes(secret), peer))
}

// ---- sealing (dm.ts) ------------------------------------------------------------------------

/** Canvas v98: a message that can never be sent sealed. */
export const TOO_LONG = 'the message is too long to lock. Shorten it and send again.'

/** Thrown when a sealed message has no head to go to yet: dm.ts holds it (never the old way). */
export class NoHead extends Error {}

/** The wraps of a sealed message: one per participant's head letterbox and one to mine, sealed by
 *  this device (no signer call). NoHead when a letterbox is unknown; an Error when too big. */
export function sealWraps(rumor: C.Rumor, to: string[]): NostrEvent[] {
  if (!device) throw new NoHead('')
  const r = C.withId(rumor)
  if (!C.fits(r)) throw new Error(TOO_LONG)
  return [...new Set([...to, me()])].map((p) => {
    const lb = lbFor(p)
    if (!lb) throw new NoHead(p) // whose device isn't ready (v98: "‹Name›'s new device isn't ready")
    return C.sealToLetterbox(device!.secret, r, p, lb)
  })
}

/** §7 out: a mail to the belfry's key sealed by this device (wrap to the belfry, no lb). */
export function sealMailOut(rumor: C.Rumor, belfry: string): NostrEvent {
  if (!device) throw new NoHead('')
  const r = C.withId(rumor)
  if (!C.fits(r)) throw new Error(TOO_LONG)
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
const liveChat = (rows: SessionRow[]) => new Set(rows.filter((s) => s.chat === true && s.client_pubkey).map((s) => s.client_pubkey!))

export class NotDone extends Error {}
type RosterArgs = { n: number; prev?: string; lb: string; devices: string[]; ready?: boolean }
const sleepMs = (ms: number) => new Promise((r) => setTimeout(r, ms))

/** §3.2: sign the roster, then the anchor naming it (nothing goes out if that fails); send both,
 *  the roster first, each waiting for the relay's OK. NotDone when it could not be. */
async function sendRosterAndAnchor(c: C.Chain | null, a: RosterArgs): Promise<NostrEvent> {
  const ev = C.makeRoster(device!.secret, { account: me(), ...a })
  const root = c ? c.rosters[0].id : ev.id
  let anchor: NostrEvent
  try { anchor = await currentSigner().sign({ kind: C.ANCHOR, created_at: nowS(), tags: C.anchorTags(root, ev.id), content: '' }) } catch { throw new NotDone('not signed') }
  try { await publishNow('main', ev) } catch (e) { throw new NotDone('roster refused: ' + (e instanceof Error ? e.message : String(e))) }
  let sent = false
  for (const wait of [0, 2000, 5000]) { await sleepMs(wait); try { await publishNow('main', anchor); sent = true; break } catch { /* again */ } }
  addEvents([ev]) // accepted by the relay: kept
  if (!sent) throw new NotDone('anchor refused')
  addEvents([anchor])
  return ev
}

/** §3.2 "done": the relay's own answer (after AUTH, to EOSE) holds roster ID on my anchored path. */
async function onAnchoredPath(id: string): Promise<boolean> {
  const conn = connectionOf('main')
  const got = await queryPaged('main', [{ kinds: [C.ANCHOR], authors: [me()], '#d': [C.ANCHOR_D] }, { kinds: [C.ROSTER], '#p': [me()] }] as Filter[])
  if (!got || conn < 0 || connectionOf('main') !== conn) throw new NotDone('no answer')
  addEvents(got)
  answeredAt.set(me(), { at: Date.now(), conn })
  const anchor = C.newestAnchor(got, me())
  if (!anchor) return false
  const c = C.validChain(anchor, got.filter((e) => e.kind === C.ROSTER))
  const ids = c?.rosters.map((r) => r.id) ?? []
  const at = ids.indexOf(id), headAt = ids.indexOf(C.anchorHead(anchor)!)
  return at >= 0 && headAt >= at
}

/** The change BUILD makes to my head, done per §3.2: read again, checked on the anchored path,
 *  redone on the new head when another branch was anchored (at most 3 times). BEFORE runs first
 *  each time (the keys of a hand-over). Null when there was nothing to do. Under the roster lock. */
async function changeMyRoster(build: (c: C.Chain, sessions: SessionRow[] | null) => RosterArgs | null, before?: (c: C.Chain, a: RosterArgs) => Promise<void>): Promise<NostrEvent | null> {
  for (let i = 0; i < 3; i++) {
    if (!(await fetchChains([me()]))) throw new NotDone('no answer')
    let c = chainOf(me())
    if (!c || !deviceIn()) throw new NotDone('not in the head')
    // a roster of mine the relay took but whose anchor it refused (an earlier try): anchor it now,
    // so the change is finished before another is built on it
    if (await anchorMyTail(c)) { c = chainOf(me()); if (!c) throw new NotDone('no chain') }
    const a = build(c, await mySessions(0))
    if (!a) return null
    if (before) await before(c, a)
    const ev = await sendRosterAndAnchor(c, a)
    await sleepMs(500)
    if (await onAnchoredPath(ev.id)) return ev // read back from the relay: done
  }
  throw new NotDone('another change won three times')
}

/** The end of the anchored path (the anchor's head when it is in the chain, else the root). */
export function anchoredEnd(c: C.Chain): C.Roster {
  return c.rosters.find((r) => r.id === c.anchored) ?? c.rosters[0]
}

/** Rosters after the anchored path that this device signed (its anchor was refused): the anchor
 *  naming the last of them, signed and sent; true when it went out and was read back. */
async function anchorMyTail(c: C.Chain): Promise<boolean> {
  const end = anchoredEnd(c)
  const tail = c.rosters.slice(c.rosters.indexOf(end) + 1)
  if (!tail.length || !tail.every((r) => r.signer === device!.pub)) return false
  const head = tail[tail.length - 1]
  let anchor: NostrEvent
  try { anchor = await currentSigner().sign({ kind: C.ANCHOR, created_at: nowS(), tags: C.anchorTags(c.rosters[0].id, head.id), content: '' }) } catch { throw new NotDone('not signed') }
  try { await publishNow('main', anchor) } catch { throw new NotDone('anchor refused') }
  addEvents([anchor])
  await sleepMs(500)
  await fetchChains([me()])
  return true
}

// this device's own path: at start, when the relay answers after AUTH, with a back-off, every 6 h
let pathDue = true
let pathTimer: ReturnType<typeof setTimeout> | null = null
let backoff = 5000
function scheduleMyDevice(ms: number) {
  if (pathTimer) clearTimeout(pathTimer)
  pathTimer = setTimeout(() => { pathTimer = null; void myDevice() }, ms)
}

async function myDevice() {
  if (!active()) return
  let ok = false
  let gone: string[] = []
  try {
    gone = await withRosterLock(async (): Promise<string[]> => {
      if (!relayAuthed('main')) return [] // only an answer given after AUTH says anything (§5.1)
      if (!(await fetchChains([me()]))) return []
      const sessions = await mySessions()
      const c = chainOf(me())
      if (!c) { await firstDevice(sessions); ok = true; return [] }
      const head = C.headOf(c)
      if (!head.devices.includes(device!.pub)) { await ask(); ok = true; return [] }
      // §5.5: in the head without the head key (a hand-over was lost): ask again, answered at once
      if (!keyOf(head.lb)) { await ask(60e3); ok = true; return [] }
      // §3.2: a roster of mine past the anchored end (its anchor refused, the app closed): anchored now
      await anchorMyTail(c).catch(() => false)
      await writeMyName()
      // §6: ready once every counted chat session is a device of the head
      if (sessions && !C.isReady(c) && C.mayCarryReady(sessions, head.devices)) {
        await changeMyRoster((c2) => { const h = C.headOf(c2); return C.isReady(c2) ? null : { n: h.n + 1, prev: h.id, lb: h.lb, devices: h.devices, ready: true } })
      }
      ok = true
      // §5.6: removal by absence needs a well-formed list that has this device's own session
      if (!sessions || !sessions.some((s) => s.client_pubkey === device!.pub)) return []
      const live = new Set(sessions.map((s) => s.client_pubkey).filter(Boolean))
      return head.devices.filter((d) => d !== device!.pub && !live.has(d))
    })
  } catch { ok = false }
  pathDue = !ok
  if (!ok) { scheduleMyDevice(backoff); backoff = Math.min(backoff * 2, 5 * 60e3); return }
  backoff = 5000
  if (!gone.length) return
  await sleepMs(Math.random() * 60e3) // outside the lock (§5.6)
  await withRosterLock(async () => {
    const sessions = await mySessions(0)
    if (!sessions || !sessions.some((s) => s.client_pubkey === device!.pub)) return
    const live = new Set(sessions.map((s) => s.client_pubkey).filter(Boolean))
    const still = gone.filter((d) => !live.has(d))
    if (still.length) await rotate(still).catch(() => { /* not in the head, or not done: next time */ })
  })
}

/** §5.1: no anchor for my account (a complete answer after AUTH said so): a letterbox, roster 1
 *  with this device and the anchor, signed, sent together and accepted, under the lock; then read
 *  again: when another device was first, ask like any new device. */
async function firstDevice(sessions: SessionRow[] | null) {
  const again = await queryPaged('main', [{ kinds: [C.ANCHOR], authors: [me()], '#d': [C.ANCHOR_D] }] as Filter[])
  if (again === null || !relayAuthed('main')) throw new NotDone('no answer')
  if (again.some((e) => C.anchorRoot(e, me()))) { addEvents(again); return ask() } // another device was first
  if (rootRefused) return // the relay refused it for good: not again on this run
  if (rootTries >= 5 && Date.now() - rootLastTry < 86400e3) return // after 5 tries, once a day
  rootTries++; rootLastTry = Date.now()
  if (!pendingRoot) { const sk = randomBytes(32); pendingRoot = { lb: getPublicKey(sk), secret: bytesToHex(sk) } } // one key for every try
  await addKeys([pendingRoot])
  const ready = !!sessions && C.mayCarryReady(sessions, [device!.pub])
  try { await sendRosterAndAnchor(null, { n: 1, lb: pendingRoot.lb, devices: [device!.pub], ready }) } catch (e) {
    if (e instanceof NotDone && /refused: (blocked|restricted|invalid|pow|error):/.test(e.message)) rootRefused = true
    throw e
  }
  await sleepMs(500)
  await fetchChains([me()])
  const c = chainOf(me())
  if (c && !C.headOf(c).devices.includes(device!.pub)) await ask()
}

let pendingRoot: C.LbKey | undefined
let rootTries = 0, rootLastTry = 0, rootRefused = false

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
/** Tests against a local relay only (VITE_TEST_RELAYS): publish a device request now. */
export const askNow = () => ask(0)
/** Tests against a local relay only (VITE_TEST_RELAYS): a roster signed with this device's key (a
 *  removed device's sibling roster, §3.3's switch case), published as is. */
export async function testSignRoster(a: { n: number; prev: string; lb: string; devices: string[] }) {
  const ev = C.makeRoster(device!.secret, { account: me(), ...a })
  await publishNow('main', ev)
  return ev.id
}
let myUsername = ''
export const setSealedUsername = (u: string) => { myUsername = u.toLowerCase() }

/** §5.2: this device isn't in my head: ask (again every 24 h while waiting). Published at once
 *  (the relay's OK), else tried again. No automatic fresh start (the lead, until Xavier decides). */
async function ask(minGapMs = 24 * 3600e3) {
  const linkSecret = linkFor(myUsername)
  if (!linkSecret && Date.now() - st.asked < minGapMs) return
  const ev = C.deviceRequest(device!.secret, me(), linkSecret)
  try { await publishNow('main', ev) } catch { throw new NotDone('request not sent') }
  addEvents([ev])
  await commit((s) => { s.asked = Date.now() })
  dropLinkSecret()
}

// requests on my other devices ------------------------------------------------------------
export interface Asking { id: string; device: string; name: string; at: number }
let asking: Asking | undefined
const doneRequests = new Set<string>()
const KV_ANSWERED = 'sealed:answered:', KV_HANDED = 'sealed:handed:'
const handedAt = new Map<string, number>() // device → my last hand-over to it (s), across restarts
function requestDone(id: string) { doneRequests.add(id); void kvSet(KV_ANSWERED + id, { id, at: Date.now() }).catch(() => {}) }
export const pendingAsk = () => asking
let linked: string | undefined // Link a phone: the device that came in by scanning my code
export const linkedName = () => linked
export const clearLinkedName = () => { linked = undefined; touch() }
const shownCodes: { secret: string; at: number }[] = [] // Link a phone: codes shown in the last 15 min (memory only)
export function newLinkCode(): string { const s = bytesToHex(randomBytes(32)); shownCodes.push({ secret: s, at: Date.now() }); return s }

/** hand-overs tried: wrap id → my head when tried (tried again when my chain changes) */
const handoverTried = new Map<string, string>()
let headsSeen = ''
let noKeyNudge = 0

async function onStore() {
  if (!active()) return
  // a head changed (mine or someone's) with sealed wraps waiting: the flush re-seals them (§9)
  const recipients = queuedRecipients()
  const heads = [me(), ...recipients].map((pk) => pk + headId(pk)).join()
  if (heads !== headsSeen) { headsSeen = heads; if (recipients.length) void flush(); void writeMyName() }
  await takeHandovers()
  // §5.5: this device just appeared in my head without the head key: ask again soon
  const mineNow = chainOf(me())
  if (mineNow && C.headOf(mineNow).devices.includes(device!.pub) && !keyOf(C.headOf(mineNow).lb) && Date.now() - noKeyNudge > 30e3) { noKeyNudge = Date.now(); scheduleMyDevice(2000) }
  // a request from a new device of mine (§5.2)
  const c = chainOf(me())
  if (!c || !deviceIn()) return
  const head = C.headOf(c)
  for (const r of all().filter((e) => e.kind === C.REQUEST && e.tags.some((t) => t[0] === 'p' && t[1] === me())).sort((a, b) => b.created_at - a.created_at)) {
    if (doneRequests.has(r.id) || asking?.id === r.id) continue
    const pin = st.pins[me()] ?? null
    const inBoth = anchoredEnd(c).devices.includes(r.pubkey) && C.headOf(c).devices.includes(r.pubkey)
    if (inBoth && (!pin?.trusted || pin.trusted.includes(r.pubkey))) {
      if (asking?.device === r.pubkey) { asking = undefined; touch() }
      // §5.5 (sealedcore.answerWithoutPrompt): a device of the head without the key asks again:
      // answered at once, no prompt, when live and its request is newer than the roster that added
      // it and than my last hand-over to it
      if (!C.validEvent(r as unknown)) { requestDone(r.id); continue }
      let rows = await mySessions(30e3)
      if (rows && !rows.some((x) => x.client_pubkey === r.pubkey && x.chat === true)) rows = await mySessions(0) // a session newer than the cache
      if (!rows) continue
      const live = [...liveChat(rows)]
      if (!live.includes(r.pubkey)) { doneRequests.add(r.id); continue } // not live now: ignored for this run only
      if (!C.answerWithoutPrompt(c, pin, r.pubkey, live, r.created_at, undefined, handedAt.get(r.pubkey) ?? null)) { requestDone(r.id); continue } // too old
      if (await handAllKeys(r.pubkey)) requestDone(r.id)
      continue
    }
    // in the head but not trusted after a switch: the person decides (the Allow card below)
    if (!C.validEvent(r as unknown)) { requestDone(r.id); continue }
    let sessions = await mySessions(30e3)
    if (sessions && !sessions.some((s) => s.client_pubkey === r.pubkey && s.chat === true)) sessions = await mySessions(0) // a session newer than the cache
    if (!sessions) continue // can't tell now: next time
    const row = sessions.find((s) => s.client_pubkey === r.pubkey && s.chat === true)
    if (!row) { doneRequests.add(r.id); continue } // not a live chat session of mine: ignored for this run (not remembered)
    const fresh = shownCodes.filter((x) => Date.now() - x.at < 15 * 60e3)
    if (fresh.some((x) => C.linkOk(x.secret, r))) {
      try { await allow(r.pubkey); requestDone(r.id); linked = row.device || row.application_name || 'Your new device' } catch { /* not done: next time */ }
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
    const prev = st.pins[me()]?.prev_root
    const chains = [chainOf(me()), prev ? C.chainFromRoot(me(), prev, mineRosters) : null].filter(Boolean) as C.Chain[]
    const got = C.acceptKeys(me(), chains, opened.seal, opened.rumor)
    if (!got.length) { if (!chains.length) void loadChain(me(), 0); continue }
    handoverTried.set(w.id, 'done')
    const added = await addKeys(got)
    if (added.length) { keysArrived(added); scheduleMyDevice(2000) /* in the head now: my name (§3.5) */ }
  }
}

/** Canvas v90: Allow on a 21st device. */
export const PENDING_OTHER = 'Not allowed yet: waiting for your other device.'
export const FULL = `You already have ${C.MAX_DEVICES} devices. Remove one in Devices, then try again.`

/** Allow: hand the whole key chain to the new device (several 8561 if needed, each accepted by the
 *  relay), then roster n+1 with it and the anchor, done per §3.2. At 20 devices: refused, nothing
 *  sent. Throws (NotDone, or a reason) when it isn't done. */
export async function allow(newDevice: string) {
  let ev: NostrEvent | null = null
  try {
    ev = await withRosterLock(() => changeMyRoster((c, sessions) => {
      const head = C.headOf(c)
      if (anchoredEnd(c).devices.includes(newDevice)) return null // already on the anchored path: done (the keys below)
      // in a roster another of my devices hasn't anchored yet: not done here, said so (the card stays)
      if (head.devices.includes(newDevice)) throw new Error(PENDING_OTHER)
      if (head.devices.length >= C.MAX_DEVICES) throw new Error(FULL)
      if (!sessions?.some((s) => s.client_pubkey === newDevice && s.chat === true)) throw new Error('That device isn’t signed in any more.')
      const devices = [...head.devices, newDevice]
      return { n: head.n + 1, prev: head.id, lb: head.lb, devices, ready: C.isReady(c) || C.mayCarryReady(sessions, devices) }
    }))
  } catch (e) { throw e instanceof NotDone ? new Error('Not allowed yet: the change didn’t go through. Try again in a moment.') : e }
  // §5.2 step 3: the keys only now the device is on the anchored path (this change, or a roster of
  // mine this change finished anchoring); lost on the way: it asks again and is answered at once (§5.5)
  void ev
  const c = chainOf(me())
  if (c && anchoredEnd(c).devices.includes(newDevice)) { trustDevice(newDevice); await handAllKeys(newDevice) }
  if (asking?.device === newDevice) { requestDone(asking.id); asking = undefined }
  touch()
}

function noteHanded(dev: string) { const at = nowS(); handedAt.set(dev, at); void kvSet(KV_HANDED + dev, { dev, at }).catch(() => {}) }

/** My whole key chain to DEVICE (several 8561 when one would pass the ceiling), each accepted by
 *  the relay. False when one wasn't (the device will ask again). */
async function handAllKeys(dev: string): Promise<boolean> {
  const keys = Object.entries(st.keys).map(([lb, secret]) => ({ lb, secret }))
  try {
    for (const chunk of C.keyChunks(device!.pub, me(), keys, nowS())) await publishNow('main', C.handKeys(device!.secret, me(), chunk, dev))
    noteHanded(dev)
    return true
  } catch { return false }
}

/** Not me: end that device's session; no roster change. Says so when it can't. */
export async function refuse(newDevice: string) {
  const sessions = await mySessions(0)
  if (!sessions) throw new Error('The belfry didn’t answer: try again in a moment.')
  const row = sessions.find((s) => s.client_pubkey === newDevice)
  if (!row?.bunker_pubkey) { if (asking?.device === newDevice) { requestDone(asking.id); asking = undefined; touch() }; throw new Error('That device isn’t signed in any more.') }
  const r = await signedFetch(`${BELFRY.api}/me/sessions/revoke`, 'POST', { bunker_pubkey: row.bunker_pubkey })
  if (!r.ok) throw new Error('The belfry did not end that session.')
  sessionsCache = null
  if (asking?.device === newDevice) { requestDone(asking.id); asking = undefined }
  touch()
}
/** The Allow card closes without an answer (it comes back next time the app opens). */
export const dismissAsk = () => { asking = undefined; touch() }

let pendingLb: C.LbKey | undefined
/** §5.6: a new letterbox; roster n+1 without the removed ones and its anchor, done per §3.2; then
 *  its key (accepted by the relay) to each device that stays AND has a live chat session AND (after
 *  a switch) is trusted on this device. When the
 *  session list can't be read, nothing at all: never a letterbox whose secret was handed to nobody.
 *  A hand-over lost on the way: that device asks again and is answered at once (§5.5). Under the lock. */
async function rotate(gone: string[]) {
  const rows = await mySessions(0)
  if (!rows) throw new NotDone('no session list')
  const ev = await changeMyRoster((c) => {
    const head = C.headOf(c)
    const left = head.devices.filter((d) => !gone.includes(d))
    if (left.length === head.devices.length) return null // done already
    if (!left.length) throw new Error('No device would be left.')
    const lbSecret = randomBytes(32)
    pendingLb = { lb: getPublicKey(lbSecret), secret: bytesToHex(lbSecret) }
    return { n: head.n + 1, prev: head.id, lb: pendingLb.lb, devices: left, ready: C.isReady(c) }
  }, async () => { await addKeys([pendingLb!]) }) // kept here first (this device's other tabs get it through kv)
  if (!ev) return
  const k = pendingLb!
  const live = liveChat((await mySessions(0)) ?? rows)
  const trusted = st.pins[me()]?.trusted // after a switch (§5.6, 52808c2): never to a device this one wouldn't answer without a prompt
  for (const d of ev.tags.filter((t) => t[0] === 'device').map((t) => t[1])) {
    if (d === device!.pub || !live.has(d) || (trusted && !trusted.includes(d))) continue
    try { await publishNow('main', C.handKeys(device!.secret, me(), [k], d)); noteHanded(d) } catch { /* it asks again (§5.5) */ }
  }
  await writeMyName()
}

/** Remove one of my devices (Me → Devices): its session ends FIRST (nothing changes when that
 *  fails), then the letterbox changes at once, done per §3.2. */
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
  if (chainOf(me())) {
    try { await withRosterLock(() => rotate([d])) } catch (e) { throw e instanceof NotDone ? new Error('Not removed yet: the change didn’t go through. Try again in a moment.') : e }
  }
  touch()
}

// ---- shared history (§8) ----------------------------------------------------------------------

let historyListener: ((rumors: C.Rumor[]) => void) | undefined
let haveRumor: (rid: string) => boolean = () => false
/** dm.ts: what to add, and which rumors it already holds (those are left as they are). */
export const onHistory = (f: (rumors: C.Rumor[]) => void, have: (rid: string) => boolean) => { historyListener = f; haveRumor = have }

async function takeHistory(h: C.Rumor) {
  const rumors = C.historyRumors(h, me()).filter((r) => !haveRumor(r.id) && !normalAccepted.has(r.id))
  if (!rumors.length) return
  await commit((s) => { for (const r of rumors) if (!s.unshared.includes(r.id)) s.shared[r.id] ??= h.pubkey })
  historyListener?.(rumors)
}
export const sharedByOf = (rid: string) => st.shared[rid]

// history packs still in the outbox: one kv entry each (re-sealed if a head changes, deleted when out)
const packs = new Map<string, C.Rumor>()
async function dropPack(id: string) { packs.delete(id); await kvDel(KV_PACK + id).catch(() => {}) }

/** Share a conversation's sealed history with PERSON's new head letterbox (any participant, by a tap). */
export async function shareHistory(person: string, rumors: C.Rumor[], conv: Conv) {
  if (!device) throw new Error('This device has no key for sealed messages.')
  const c = await loadChain(person, 0)
  if (!c) throw new Error('Their new device isn’t ready yet.')
  const { packs: list } = C.packHistory(me(), person, rumors.map((r) => C.withId(r)), nowS())
  for (const p of list) { packs.set(p.id, p); await kvSet(KV_PACK + p.id, p).catch(() => {}) }
  for (const p of list) await enqueueSigned('main', C.sealToLetterbox(device.secret, p, person, C.headOf(c).lb), p.id)
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
 *  sealed again to the head, from the rumor dm.ts keeps or the history pack kept here. Run only
 *  from the flush (sync.setBeforeFlush), so it never races the sending loop. */
async function resealQueued() {
  if (!device) return
  for (const o of sealedQueued()) {
    const w = o.signed!
    const lb = C.wrapLb(w)
    const to = w.tags.find((t) => t[0] === 'p')?.[1]
    if (!lb || !to) continue
    const want = lbFor(to)
    if (!want || want === lb) continue
    const r = packs.get(o.dm!) ?? rumorSource?.(o.dm!)
    if (!r) continue
    try {
      const nw = C.sealToLetterbox(device.secret, r, to, want)
      await putOutbox({ ...o, signed: nw, content: nw.content, tags: nw.tags, created_at: nw.created_at, status: o.status === 'failed' ? 'queued' : o.status, error: undefined })
    } catch { /* too big or broken: left as it was */ }
  }
}

/** sync.flush, before anything goes out: the heads of queued sealed wraps' recipients asked again
 *  (when not answered in the last minute; 3 s at most), then the wraps re-sealed when a head moved. */
async function beforeFlush() {
  if (!active()) return
  const to = queuedRecipients()
  if (!to.length) return
  const stale = to.filter((p) => !answeredRecently(p, 60e3) && Date.now() - (triedAt.get(p) ?? 0) > 15e3)
  if (stale.length) await fetchChains(stale)
  await resealQueued()
}
