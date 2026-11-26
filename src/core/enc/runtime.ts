// Encrypted conversations, format 3, on this device (docs/encrypted.md with §16–§20): the device
// lists of the people I talk to, the conversation keys this device holds, opening 8572s for dm.ts
// and encrypting what it sends. People conversations (private messages, group messages, Notes);
// private groups come later. Writing format 3 only when belfry.json says so (access.ts); reading
// always. Every signed-in chat session runs it, guests too (the lead's ruling: they are devices).
//
// Keys live in this account's own database (kv 'enc-keys'), never localStorage, never logged.
import { hexToBytes } from '@noble/hashes/utils'
import { finalizeEvent, getPublicKey, nip19, type Filter, type NostrEvent } from 'nostr-tools'
import { BELFRY } from '../../config'
import { belfryGroupsKey, belfryNostrKey, encryptedFormat, loadAccessInfo, onAccessInfo } from '../access'
import { kvGet, kvSet } from '../db'
import { addEvents, all, latestCreatedAt, outboxItems, recheck, setRefuse, subscribe } from '../store'
import { allGroups, group as groupOf, setEncryptedHere } from '../model'
import { connectionOf, enqueueSigned, me, onOutbox, queryPaged, setEncryptsFiles, setExtraGroupsFilters, setExtraMainFilters, setGroupWrap, setPagedMainFilters } from '../sync'
import * as E from './core'

let device: { secret: Uint8Array; pub: string } | null = null
let startedAt = 0
/** The device key of this session (public), once started. */
export const encDevice = () => device?.pub ?? ''
/** Format 3 is what this belfry writes (known: belfry.json or this device's memory of it). */
export const writesFormat3 = () => encryptedFormat() === 3
/** Started and writing format 3. */
export const encOn = () => !!device && writesFormat3()

// ---- keys this device holds ------------------------------------------------------------------

interface KeyRec extends E.HeldKey {
  /** the people of the conversation (people keys) */ people?: string[]
  /** a key this device made whose envelopes the outbox hasn't confirmed yet: sent again before
   *  the next message that uses it (the review's P2.1) */ unconfirmed?: NostrEvent[]
  /** what rebuilds those envelopes for the SAME key with fresh dates (N1): its rumor and recipients */
  made?: { rumor: E.Rumor; per: { account: string; devices: string[] }[] }
  /** a people key's rumor as received (what a key pack hands on, §7.5) */ rumor?: E.Rumor
  /** a group key's entry set as its maker claims it (adopted only once the entries add up: P1.3) */ claimed?: string
  /** a group key I made whose envelopes aren't confirmed out (P2.2) */ unconfirmedGroup?: { kind: number; created_at: number; tags: string[][]; content: string }[]
}
const keys = new Map<string, KeyRec>() // K → key
const byAlias = new Map<string, string>() // alias(K, me) → K
/** Conversations (peopleId) known to be encrypted here, with the date of their first key (§17.2, §18.6). */
const encConvs = new Map<string, number>()
const KV_KEYS = 'enc-keys', KV_CONVS = 'enc-convs'
const lockOr = <T,>(f: () => Promise<T>): Promise<T> => (navigator.locks?.request ? navigator.locks.request('belfry-enc-' + me(), f) as Promise<T> : f())
/** Saved merged with what another tab of this device saved (never last-writer-wins). */
async function saveKeys() {
  await lockOr(async () => {
    for (let i = 0; i < 2; i++) {
      try {
        for (const r of (await kvGet<KeyRec[]>(KV_KEYS)) ?? []) if (r && E.isKey(r.K) && E.isKey(r.k)) holdKey(r)
        const convs = (await kvGet<[string, number][]>(KV_CONVS)) ?? []
        for (const [c, at] of Array.isArray(convs) ? convs : []) if (typeof c === 'string' && typeof at === 'number') noteConv(c, at)
        await kvSet(KV_KEYS, [...keys.values()])
        await kvSet(KV_CONVS, [...encConvs.entries()])
        return
      } catch { await new Promise((r) => setTimeout(r, 1000)) } // once more, then kept in memory
    }
  })
}
function noteConv(conv: string, at: number) {
  const was = encConvs.get(conv)
  if (was === undefined || at < was) { encConvs.set(conv, at); if (conv.startsWith('g:')) queueMicrotask(() => recheck()) } // a group's first key: clear events after it go (§17.2)
}
function holdKey(r: KeyRec) {
  const had = keys.get(r.K)
  if (had) { // §17.12: the first valid declaration of a K fixes its people; only "unconfirmed" may clear
    if (had.unconfirmed && !r.unconfirmed) delete had.unconfirmed
    return false
  }
  keys.set(r.K, r)
  byAlias.set(E.alias(r.K, me()), r.K)
  noteConv(r.conv, r.at) // (a group's too, as 'g:<id>': its first key's date, §17.2)
  return true
}
/** How many conversation keys this device holds (tests, Diagnostics). */
export const encKeyCount = () => keys.size

// ---- device lists --------------------------------------------------------------------------

let partners: string[] = []
/** The people I talk to (dm.ts): their lists are kept on the live subscription. */
export function setEncPartners(ps: string[]) {
  const next = [...new Set(ps)].sort()
  if (next.join() === partners.join()) return
  partners = next
  if (device) setExtraMainFilters(filters, 'enc')
}
/** People whose lists something here waits for (a key or message from someone not yet a partner). */
const wanted = new Set<string>()
const wantAsked = new Map<string, number>()
function want(accounts: string[]) {
  const fresh = accounts.filter((a) => E.isKey(a) && !wanted.has(a) && !partners.includes(a))
  for (const a of fresh) wanted.add(a)
  const ask = accounts.filter((a) => E.isKey(a) && Date.now() - (wantAsked.get(a) ?? 0) > 30_000)
  if (!ask.length || !belfry()) return
  for (const a of ask) wantAsked.set(a, Date.now())
  if (fresh.length && device) setExtraMainFilters(filters, 'enc')
  void readLists(ask, 5000)
}
const belfry = () => belfryNostrKey()

// the review's P2.5: what is known of a list counts only after one complete read of it on this connection
const listsRead = new Map<string, number>() // account → the connection it was last read whole on
const readOnThisConnection = (a: string) => listsRead.get(a) === connectionOf('main')
/** Read ACCOUNTS' lists whole; true when the read completed (they count as known on this connection). */
async function readLists(accounts: string[], ms: number): Promise<boolean> {
  if (!belfry() || !accounts.length) return false
  const conn = connectionOf('main')
  const got = await queryPaged('main', [{ kinds: [E.LIST], authors: [belfry()], '#p': accounts }], ms, 20, ms).catch(() => null)
  if (!got) return false
  addEvents(got)
  for (const a of accounts) listsRead.set(a, conn)
  return true
}

// chains are verified once per change of the store, per account (the review's P3)
let chainCache: { list: unknown; map: Map<string, E.Chain | undefined> } = { list: null, map: new Map() }
/** §17.1 + §20: ACCOUNT's devices: its chain; SERVICE only for the belfry key and the groups key
 *  (their own device); undefined when no list is known (Held). */
export function chainFor(account: string): E.Devices {
  if (account && (account === belfry() || account === belfryGroupsKey())) return E.SERVICE
  if (!belfry()) return undefined
  const list = all(me())
  if (chainCache.list !== list) chainCache = { list, map: new Map() }
  if (!chainCache.map.has(account)) chainCache.map.set(account, E.chainOf(list.filter((e) => e.kind === E.LIST), belfry(), account) ?? undefined)
  return chainCache.map.get(account)
}
const chainOnly = (a: string): E.Chain | undefined => { const c = chainFor(a); return c && c !== E.SERVICE ? c : undefined }

let chatSessions = 0
/** How many chat sessions my /api/me/sessions lists (the ruling: 'full' vs 'not yet'). */
export const setChatSessions = (n: number) => { chatSessions = n }
/** This device on its own account's current list; 'full': twenty devices and not listed (§17.4). */
export function listedHere(): 'yes' | 'not_yet' | 'held' | 'full' {
  const c = chainOnly(me())
  if (c && device && E.current(c).devices.includes(device.pub)) return 'yes'
  if (c && E.current(c).devices.length >= E.MAX_LIST && chatSessions >= E.MAX_LIST) return 'full'
  return Date.now() - startedAt < 60_000 ? 'not_yet' : 'held' // §3: "isn't set up for encrypted messages yet" after 60 s
}

function filters(): Filter[] {
  if (!device || !belfry()) return []
  const m = me()
  const since = Math.max(0, latestCreatedAt((e) => e.kind >= E.KEY && e.kind <= E.PACK) - 3600) // §17.11: ordinary catch-up
  return [
    { kinds: [E.LIST], authors: [belfry()], '#p': [...new Set([m, ...partners, ...wanted])] },
    { kinds: [E.KEY, E.MSG, E.PACK], '#p': [m], since },
    { kinds: [E.ASK, E.ANSWER], '#p': [m] },
  ]
}

// ---- start -----------------------------------------------------------------------------------

let saidReads = false
const KV_READS = 'enc-said-reads'
async function sayReads() {
  if (!device || (await kvGet<string>(KV_READS).catch(() => undefined)) === device.pub) return
  const ev = finalizeEvent({ kind: 30078, created_at: Math.floor(Date.now() / 1000), tags: [['d', 'belfry:device:reads'], ['v', '3']], content: '' }, device.secret)
  await enqueueSigned('main', ev)
  await kvSet(KV_READS, device.pub).catch(() => {})
}
let loading: Promise<void> | null = null
/** Resolved once this device's keys and encrypted conversations are loaded (at once when the runtime doesn't run). */
export const whenEncLoaded = () => loading ?? Promise.resolve()
// the re-read's P1-4: old-style wraps wait until a complete read of my envelopes and messages on this connection
let caughtUpOn = -2, catchingUp = false, lastTry = 0
export const encCaughtUp = () => caughtUpOn === connectionOf('main')
/** Start that read once per connection (a failed one again after 10 s); dm.ts processes again
 *  when it completed (onEncKeys). */
export function ensureEncCaughtUp() {
  if (!device || catchingUp || encCaughtUp() || Date.now() - lastTry < 10_000) return
  catchingUp = true
  lastTry = Date.now()
  const conn = connectionOf('main')
  void queryPaged('main', [{ kinds: [E.KEY, E.MSG], '#p': [me()] }], 8000, 1000, 60000).then(async (got) => {
    catchingUp = false
    if (!got) { setTimeout(() => keyListeners.forEach((l) => l()), 10_500); return } // tried again at the next process
    addEvents(got)
    await takeEnvelopes()
    caughtUpOn = conn
    keyListeners.forEach((l) => l())
  }, () => { catchingUp = false })
}

export function startEnc(clientSecret: string): Promise<void> {
  if (device || !/^[0-9a-f]{64}$/.test(clientSecret)) return whenEncLoaded()
  loading = startEncNow(clientSecret)
  return loading
}
async function startEncNow(clientSecret: string) {
  const secret = hexToBytes(clientSecret)
  device = { secret, pub: getPublicKey(secret) }
  startedAt = Date.now()
  for (const r of (await kvGet<KeyRec[]>(KV_KEYS).catch(() => undefined)) ?? []) if (r && E.isKey(r.K) && E.isKey(r.k)) holdKey(r)
  const convs = (await kvGet<[string, number][]>(KV_CONVS).catch(() => undefined)) ?? []
  for (const [c, at] of Array.isArray(convs) ? convs : []) if (typeof c === 'string' && typeof at === 'number') noteConv(c, at)
  asked = (await kvGet<Asked>(KV_ASKED).catch(() => undefined)) ?? null
  firstN = (await kvGet<number>(KV_FIRST).catch(() => undefined)) ?? 0
  firstScreenSeen = !!(await kvGet<boolean>(KV_FIRST_SCREEN).catch(() => undefined)) || firstN > 0 // (an earlier build already saw its first list)
  for (const [d, a] of (await kvGet<[string, { by: string; answer: 'shared' | 'removed' }][]>(KV_ANSWERED).catch(() => undefined)) ?? []) answeredHere.set(d, a)
  sharedBy = (await kvGet<{ device: string; at: number }>(KV_SHARED).catch(() => undefined)) ?? null
  setExtraMainFilters(filters, 'enc')
  // belfry.json: the belfry key (who signs lists) and the format; its filters again once known
  onAccessInfo(() => { setExtraMainFilters(filters, 'enc'); void checkRemoved() })
  void loadAccessInfo()
  // read whole after each EOSE: the lists, and keys, messages and packs for me (the review's P2.6)
  setPagedMainFilters(() => {
    if (!device || !belfry()) return []
    const since = Math.max(0, latestCreatedAt((e) => e.kind >= E.KEY && e.kind <= E.PACK) - 3600)
    return [{ kinds: [E.LIST], authors: [belfry()], '#p': [...new Set([me(), ...partners])] }, { kinds: [E.KEY, E.MSG, E.PACK], '#p': [me()], since }]
  }, 'enc')
  // my new key's envelopes: confirmed once the outbox says they are out (P2.1)
  onOutbox((_item, ev) => {
    if (ev.kind !== E.KEY) return
    for (const k of keys.values()) if (k.unconfirmed?.some((x) => x.id === ev.id)) {
      k.unconfirmed = k.unconfirmed.filter((x) => x.id !== ev.id)
      if (!k.unconfirmed.length) { delete k.unconfirmed; void saveKeys() }
    }
  }, () => {})
  let t: ReturnType<typeof setTimeout> | null = null
  subscribe(() => { t ??= setTimeout(() => { t = null; void takeEnvelopes() }, 150) })
  // part 10: private groups: their 8571/8572 on the groups relay, opened into the store; sends wrapped
  setExtraGroupsFilters(() => { const ids = myPrivateGroups(); return device && ids.length ? [{ kinds: [E.KEY, E.MSG], '#h': ids }] : [] }, 'enc')
  setGroupWrap((tpl) => wrapForGroup(tpl))
  setEncryptedHere((h) => encConvs.has('g:' + h))
  setEncryptsFiles((h, kind) => groupEncrypts(h) && !clearInGroup({ kind, pubkey: me(), tags: [] }))
  setRefuse(refuseClear)
  let gt: ReturnType<typeof setTimeout> | null = null
  let groupIds = ''
  subscribe(() => { gt ??= setTimeout(() => {
    gt = null
    const ids = myPrivateGroups().sort().join(); if (ids !== groupIds) { groupIds = ids; setExtraGroupsFilters(() => { const l = myPrivateGroups(); return device && l.length ? [{ kinds: [E.KEY, E.MSG], '#h': l }] : [] }, 'enc') }
    void openGroupMessages()
  }, 200) })
  // removed by another device? at start, every 5 minutes, and back in the foreground (§16.6)
  void checkRemoved()
  setInterval(() => void checkRemoved(), 5 * 60e3)
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') void checkRemoved() })
}

// ---- receiving -------------------------------------------------------------------------------

/** Envelopes for my account: my entries opened and judged; held when accepted (§5, §16.2). */
const envSeen = new Set<string>()
async function takeEnvelopes() {
  if (!device) return
  const m = me()
  let changed = false
  for (const e of all(m)) {
    if (e.kind !== E.KEY || envSeen.has(e.id) || !e.tags.some((t) => t[0] === 'p' && t[1] === m)) continue
    let o: ReturnType<typeof E.openPeopleEnvelope> = null
    try { o = E.openPeopleEnvelope(device.secret, e) } catch { envSeen.add(e.id); continue } // a bad entry: skipped, never blocking (§17.12)
    checkEntries(e) // P2.5: an entry for a key never on my lists → the warning that stays
    if (!o) { envSeen.add(e.id); continue } // no entry for this device here ("can't be read" only after a complete #d read)
    const v = E.acceptKeyRumor(o.rumor, m, chainFor)
    if (v === 'wait') { want(o.rumor.tags.filter((t) => t[0] === 'p').map((t) => t[1])); continue } // judged again when lists come
    envSeen.add(e.id)
    if (v !== 'ok') continue
    const r = o.rumor
    const K = r.tags.find((t) => t[0] === 'K')![1]
    const people = r.tags.filter((t) => t[0] === 'p').map((t) => t[1])
    // mine: made by a device on the list the key names for MY account (the review's P3)
    const named = r.tags.find((t) => t[0] === 'list' && t[1] === m)?.[2]
    const mc = chainOnly(m)
    const mine = !!named && !!mc && !!E.listById(mc, named)?.devices.includes(r.pubkey)
    const lists = E.peopleLists(r.tags.filter((t) => t[0] === 'list').map((t) => ({ account: t[1], id: t[2] })))
    if (holdKey({ K, k: r.content, conv: E.peopleId(people), people, mine, service: r.pubkey === belfry() || r.pubkey === belfryGroupsKey(), lists, at: r.created_at, rumor: r })) changed = true
  }
  // §12 (part 9): once, this device says it reads format 3: a 30078 d=belfry:device:reads, ["v","3"],
  // signed by the device (the console counts the live devices that haven't); sent once it is on my list
  if (!saidReads) { const c = chainOnly(m); if (c && E.current(c).devices.includes(device.pub)) { saidReads = true; void sayReads() } }
  // §6.4: the first list of mine this device sees brings no card
  if (!firstN) { const c = chainOnly(m); if (c && E.current(c).devices.includes(device.pub)) { firstN = E.current(c).n; await kvSet(KV_FIRST, firstN).catch(() => {}) } }
  if (await takePacks()) changed = true
  if (changed) { await saveKeys(); keyListeners.forEach((l) => l()) }
}
const keyListeners = new Set<() => void>()
/** Called when new keys arrive (dm.ts reads the messages waiting for them again). */
export const onEncKeys = (l: () => void) => { keyListeners.add(l) }

export type Opened = { ok: true; rumor: NostrEvent; conv: string } | { ok: false; retry: boolean; state?: 'opening' | 'unreadable'; /** its conversation when its key is here (the placeholder's place) */ conv?: string }
const dRead = new Map<string, { at: number; found: boolean }>() // alias → the last complete #d read
/** An 8572 for me: its rumor when it opens and is accepted; retry when its key or the author's
 *  lists aren't here yet (never marked seen on that, §6). */
export async function openEnc(e: NostrEvent): Promise<Opened> {
  if (!device) return { ok: false, retry: true, state: 'opening' }
  const d = E.named(e)
  if (!d) return { ok: false, retry: false }
  let K = byAlias.get(d)
  if (!K) { await takeEnvelopes(); K = byAlias.get(d) }
  if (!K) {
    // its envelopes, asked by #d once a minute whatever came live (a forged empty one never stops it)
    const r = dRead.get(d)
    if (!r || Date.now() - r.at > 60_000) {
      dRead.set(d, { at: Date.now(), found: r?.found ?? false })
      void queryPaged('main', [{ kinds: [E.KEY], '#p': [me()], '#d': [d] }], 5000).then((got) => {
        if (!got) return
        addEvents(got)
        // N5: "unreadable" only when envelopes were found and none has an entry for this device (an
        // entry whose key waits for lists is "opening", not unreadable)
        const mineIn = got.some((env) => { try { return !!E.openPeopleEnvelope(device!.secret, env) } catch { return false } })
        dRead.set(d, { at: Date.now(), found: got.length > 0 && !mineIn })
      })
    }
    // "can't be read on this device" only after a complete #d read found envelopes, none with my entry
    return { ok: false, retry: true, state: r?.found ? 'unreadable' : 'opening' }
  }
  const key = keys.get(K)!
  let o: { seal: NostrEvent; rumor: E.Rumor }
  try { o = E.openPeopleMessage(key.k, e) } catch { return { ok: false, retry: false } } // a copy that doesn't open with the right k: dropped
  const v = E.acceptPeopleMessage(e, o.seal, o.rumor, key.conv, chainFor(o.rumor.pubkey))
  if (v === 'wait') {
    // P2.3: the author's list may be behind: dropped only once a complete read of it on this
    // connection, with no gap, still has the signer on no list
    const c = chainOnly(o.rumor.pubkey)
    if (readOnThisConnection(o.rumor.pubkey) && c && !c.gaps && !c.lists.some((l) => l.devices.includes(o.seal.pubkey))) return { ok: false, retry: false }
    want([o.rumor.pubkey])
    return { ok: false, retry: true, state: 'opening', conv: key.conv }
  }
  if (v === 'no') return { ok: false, retry: false }
  noteConv(key.conv, key.at)
  return { ok: true, rumor: o.rumor as unknown as NostrEvent, conv: key.conv }
}

/** Is this people conversation encrypted (§18.6), by its accounts; its first key's date. */
export const isEncConv = (accounts: string[]) => encConvs.has(E.peopleId(accounts))
export const encFirstAt = (accounts: string[]) => encConvs.get(E.peopleId(accounts))

// ---- sending ---------------------------------------------------------------------------------

/** A guest among these accounts (a channel's guest, or this session's own guest account). */
let guestTest: (pk: string) => boolean = () => false
/** dm.ts: how to tell a guest (core/invite isGuest, auth guestInfo for my own session). */
export const setGuestTest = (f: (pk: string) => boolean) => { guestTest = f }
const guestIn = (accounts: string[]) => accounts.some((a) => guestTest(a))

export class Held extends Error {
  constructor(public reason: 'no_list' | 'not_listed' | 'no_device' | 'too_many' | 'full' | 'fork' | 'mail' | 'format' | 'guest', public who?: string) { super('held: ' + reason) }
}
/** Words for a held message (state-held; §17.4, §17.5). */
export function heldText(h: Held, name: (pk: string) => string): string {
  if (h.reason === 'no_device' && h.who) return `${name(h.who)} isn't signed in on any device · It will be sent when they are`
  if (h.reason === 'not_listed') return 'This device isn’t set up for encrypted messages yet'
  if (h.reason === 'full') return 'You have 20 devices signed in · Remove one to send encrypted messages here'
  if (h.reason === 'too_many') return 'Too many devices to encrypt for everyone here'
  if (h.reason === 'mail') return 'E-mail from the chat waits for this belfry’s update'
  if (h.reason === 'guest') return 'Not sent: this encrypted conversation now includes a guest, and guests can’t read encrypted messages yet'
  return 'Not sent: couldn’t check who can read this conversation'
}

/** §17.2 + §18.6 (the review's P1.3): a conversation over 100 devices goes unencrypted only when it
 *  was never encrypted, known from a complete read of every envelope and message for me on this
 *  connection with every alias resolved; otherwise Held. */
async function neverEncrypted(conv: string): Promise<boolean> {
  if (encConvs.has(conv)) return false
  const got = await queryPaged('main', [{ kinds: [E.KEY, E.MSG], '#p': [me()] }], 8000, 100, 15000).catch(() => null)
  if (!got) return false
  addEvents(got)
  await takeEnvelopes()
  if (got.some((e) => { const d = E.named(e); return !!d && !byAlias.has(d) })) return false // an alias I can't resolve may be this conversation
  return !encConvs.has(conv)
}

/** The events that carry RUMOR to PEOPLE (accounts, me included) encrypted: the key's envelopes
 *  first when a new key is made (or its unconfirmed ones again), then one copy per person. Null: not
 *  encrypted (a conversation that starts over 100 devices: no lock). Throws Held when it can't be
 *  encrypted now. MISSING: people with no device, who won't get it (§17.5). */
export async function encryptFor(rumor: E.Rumor, people: string[]): Promise<{ events: NostrEvent[]; missing: string[] } | null> {
  const m = me()
  const all = [...new Set([m, ...people])].sort()
  // guests first (a guest's session runs no runtime: the lead): unencrypted, or held when encrypted here before
  if (guestIn(all)) { if (await neverEncrypted(E.peopleId(all))) return null; throw new Held('guest') } // (re-read P1-b: never encrypted, from a complete read)
  if (!device) throw new Held('not_listed')
  if (E.peopleOf(rumor) !== E.peopleId(all)) throw new Error('the message is not for these people') // a programming error, never shown
  if (new TextEncoder().encode(JSON.stringify(E.withId(rumor))).length > E.MAX_RUMOR) throw new Error('the message is too long to encrypt') // before any key (P2.1)
  // P2.5: lists count only after one complete read for these accounts on this connection
  // (an account with no list known is read again at every try: its list may have come since)
  const toRead = all.filter((a) => chainFor(a) !== E.SERVICE && (!readOnThisConnection(a) || chainFor(a) === undefined))
  if (toRead.length && !(await readLists(toRead, 3000))) throw new Held('no_list')
  const here = listedHere()
  if (here === 'full') throw new Held('full')
  if (here !== 'yes') throw new Held('not_listed')
  const per: { account: string; list?: string; devices: string[] }[] = []
  for (const a of all) {
    const c = chainFor(a)
    if (c === undefined) throw new Held('no_list', a) // §17.1: never decided by absence
    if (c !== E.SERVICE && (c.fork || c.invalidAbove)) throw new Held('fork', a) // P2.7, §17.7, §18.5
    per.push(c === E.SERVICE ? { account: a, devices: [a] } : { account: a, list: E.current(c).id, devices: E.receivers(E.current(c)) })
  }
  // §17.5: a person with no device: alone, held; with others, sent for the others
  const missing = per.filter((p) => p.account !== m && !p.devices.length).map((p) => p.account)
  const others = per.filter((p) => p.account !== m)
  if (others.length && missing.length === others.length) throw new Held('no_device', missing[0])
  const total = per.reduce((n, p) => n + p.devices.length, 0)
  const conv = E.peopleId(all)
  if (total > E.MAX_PEOPLE_DEVICES) { if (await neverEncrypted(conv)) return null; throw new Held('too_many') }
  // the lists a key is made for: accounts with a list only (a service key has none: P3)
  const lists = E.peopleLists(per.filter((p) => p.list).map((p) => ({ account: p.account, id: p.list! })))
  const out: NostrEvent[] = []
  let key = E.pickKey([...keys.values()], conv, lists) as KeyRec | undefined
  if (!key) {
    const { k, K } = E.newKey()
    const r = E.keyRumor(device.pub, k, per.map((p) => ({ account: p.account, ...(p.list ? { list: p.list } : {}) })))
    const envs = E.peopleEnvelopes(device.secret, r, per.filter((p) => p.devices.length))
    const made = { rumor: r, per: per.filter((p) => p.devices.length).map((p) => ({ account: p.account, devices: p.devices })) }
    const rec: KeyRec = { K, k, conv, people: all, mine: true, lists, at: r.created_at, unconfirmed: envs, made }
    holdKey(rec)
    key = rec
    await saveKeys()
  }
  // its envelopes not confirmed out yet: still live in the outbox (status queued) → nothing to do;
  // refused or lost (a failed item, a closed tab) → the SAME key's envelopes rebuilt with fresh dates,
  // never a dropped key (messages under it may be out already: N1)
  const live = new Set(outboxItems().filter((o) => o.status === 'queued').map((o) => o.signed?.id).filter(Boolean))
  if (key.unconfirmed?.some((env) => !live.has(env.id))) {
    if (key.made) {
      const fresh = E.peopleEnvelopes(device.secret, key.made.rumor, key.made.per)
      key.unconfirmed = fresh
      await saveKeys()
      out.push(...fresh)
    } else out.push(...key.unconfirmed) // (an earlier build's record: sent as it was)
  }
  const to = all.filter((a) => !missing.includes(a))
  out.push(...E.peopleMessage(device.secret, rumor, key.K, to, m))
  noteConv(conv, key.at)
  return { events: out, missing }
}

/** The third read's N1: one of my key's envelopes (ENV_ID) was refused or dropped: that key's
 *  envelopes rebuilt now with fresh dates (same K, from `made`), to be queued on their own, so the
 *  messages already sent under it stay readable whether or not anyone writes again. */
export async function repairKey(envId: string): Promise<NostrEvent[]> {
  if (!device) return []
  for (const k of keys.values()) {
    if (!k.unconfirmed?.some((x) => x.id === envId) || !k.made) continue
    const fresh = E.peopleEnvelopes(device.secret, k.made.rumor, k.made.per)
    k.unconfirmed = fresh
    await saveKeys()
    return fresh
  }
  return []
}

// ---- §7 a new device: earlier messages (part 6) --------------------------------------------

/** How many messages for me this device can't open: format-3 copies whose key it has no copy of. */
export function unresolvedCount(): number {
  const m = me()
  let n = 0
  for (const e of all(m)) if (e.kind === E.MSG && e.tags.some((t) => t[0] === 'p' && t[1] === m)) { const d = E.named(e); if (d && !byAlias.has(d)) n++ }
  return n
}
/** My devices listed before this one (they hold what this device misses; §17.6). */
export function olderDevices(): string[] {
  const c = chainOnly(me())
  if (!c || !device) return []
  return E.current(c).devices.filter((d) => d !== device!.pub && E.listedBefore(c, d, device!.pub))
}
interface Asked { at: number; devices: string[]; id: string }
let asked: Asked | null = null
let sharedBy: { device: string; at: number } | null = null
const KV_ASKED = 'enc-asked', KV_SHARED = 'enc-shared-by'
export const askedNow = () => asked
export const sharedByNow = () => sharedBy
/** §7.3: ask my other devices for earlier messages (an 8574 signed by this device). Again only after
 *  24 hours, or when another device of mine appeared on the list since (§16.12). */
export async function askForEarlier(): Promise<boolean> {
  if (!device) return false
  const now = Math.floor(Date.now() / 1000)
  const mineNow = chainOnly(me()) ? E.current(chainOnly(me())!).devices : []
  if (asked && now - asked.at < 86400 && mineNow.every((d) => asked!.devices.includes(d))) return false
  const ev = E.ask(device.secret, me(), now)
  await enqueueSigned('main', ev)
  asked = { at: now, devices: mineNow, id: ev.id }
  await kvSet(KV_ASKED, asked).catch(() => {})
  keyListeners.forEach((l) => l())
  return true
}
const v1Listeners = new Set<(list: { lb: string; secret: string }[]) => void>()
/** dm.ts → sealedrt: version-1 letterbox secrets that came in a key pack (§16.3). */
export const onV1Secrets = (l: (list: { lb: string; secret: string }[]) => void) => { v1Listeners.add(l) }
const packSeen = new Set<string>()
/** §7.5 + §16.3 + §19: key packs to this device from an older device of mine: people and group keys
 *  kept to read with (never adopted for sending: the ruling), version-1 secrets handed to sealedrt. */
async function takePacks(): Promise<boolean> {
  if (!device) return false
  const m = me()
  let changed = false
  for (const e of all(m)) {
    if (e.kind !== E.PACK || packSeen.has(e.id) || !e.tags.some((t) => t[0] === 'd' && t[1] === device!.pub)) continue
    const c = chainOnly(m)
    if (!c || c.gaps) continue // judged once my lists are whole (§18.3)
    packSeen.add(e.id)
    const o = E.openPack(device.secret, m, e, c)
    if (!o) continue
    const v1: { lb: string; secret: string }[] = []
    for (const k of o.keys) {
      if (k.kind === 'people') {
        const people = k.rumor.tags.filter((t) => t[0] === 'p').map((t) => t[1])
        const K = k.rumor.tags.find((t) => t[0] === 'K')![1]
        if (holdKey({ K, k: k.rumor.content, rumor: k.rumor, conv: E.peopleId(people), people, mine: false, lists: E.peopleLists(k.rumor.tags.filter((t) => t[0] === 'list').map((t) => ({ account: t[1], id: t[2] }))), at: k.rumor.created_at })) changed = true
      } else if (k.kind === 'group') {
        if (holdKey({ K: k.K, k: k.k, conv: 'g:' + k.group, group: true, mine: false, lists: '', at: e.created_at })) changed = true
      } else v1.push({ lb: k.lb, secret: k.secret })
    }
    if (v1.length) v1Listeners.forEach((l) => l(v1))
    sharedBy = { device: o.from, at: e.created_at }
    await kvSet(KV_SHARED, sharedBy).catch(() => {})
    changed = true
  }
  return changed
}

// ---- §7 on my other devices: the card, Share, Remove (part 7) -------------------------------

let v1Provider: () => { lb: string; secret: string }[] = () => []
/** dm.ts: where this device's version-1 letterbox secrets come from (sealedrt), for Share. */
export const setV1Provider = (f: typeof v1Provider) => { v1Provider = f }
const KV_FIRST = 'enc-first-n', KV_ANSWERED = 'enc-answered'
let firstN = 0 // the n of the first list of mine this device saw: no card for what that list already had (§6.4)
const answeredHere = new Map<string, { by: string; answer: 'shared' | 'removed' }>() // device → an answer seen
/** A device of mine that needs the card: added after the first list this device saw, listed after
 *  this device (only older devices answer, §17.6), not answered by any device yet. Readers: their own card (§17.9). */
export interface AddedDevice { device: string; at: number; reader: boolean; ask?: string; /** §17.7: added and removed again while this device was away */ gone?: boolean }
export function addedDevices(): AddedDevice[] {
  const c = chainOnly(me())
  if (!c || !device || !firstN) return []
  const cur = E.current(c)
  if (!cur.devices.includes(device.pub)) return []
  const asks = new Map<string, string>()
  for (const e of all(me())) { const a = E.readAsk(e, me(), c); if (a) asks.set(a.device, a.id) }
  for (const e of all(me())) {
    if (e.kind !== E.ANSWER) continue
    const askId = e.tags.find((t) => t[0] === 'e')?.[1]
    const about = askId ? [...asks.entries()].find(([, id]) => id === askId)?.[0] : undefined
    const a = E.readAnswer(e, me(), c, about)
    const d = a?.device ?? about
    if (a && d && !answeredHere.has(d)) answeredHere.set(d, { by: a.by, answer: a.answer })
  }
  const out: AddedDevice[] = []
  for (const ch of E.changes(c)) {
    if (ch.n <= firstN) continue
    const l = c.lists.find((x) => x.id === ch.list)!
    for (const d of ch.added) if (d !== device.pub && cur.devices.includes(d) && E.listedBefore(c, device.pub, d) && !answeredHere.has(d)) out.push({ device: d, at: l.created_at, reader: false, ask: asks.get(d) })
    for (const d of ch.readersAdded) if (cur.readers.includes(d) && !answeredHere.has(d)) out.push({ device: d, at: l.created_at, reader: true })
    // §17.7: added and removed while this device was away (not on the current list any more)
    for (const d of ch.added) if (d !== device.pub && !cur.devices.includes(d) && !answeredHere.has(d) && !goneSeen.has(d)) out.push({ device: d, at: l.created_at, reader: false, gone: true })
  }
  return out
}
const goneSeen = new Set<string>()
/** The "added and removed while you were away" card read (OK). */
export const goneOk = (d: string) => { goneSeen.add(d); keyListeners.forEach((l) => l()) }

/** §17.7 + the review's P2.5: warnings that don't go by themselves: my lists forked, or an envelope for
 *  my account holds an entry for a key that was never on my lists. */
const strangeEntries = new Set<string>()
export function listWarnings(): { fork: boolean; strange: number } {
  const c = chainOnly(me())
  return { fork: !!c && (c.fork || c.invalidAbove), strange: strangeEntries.size }
}
function checkEntries(env: NostrEvent) {
  const c = chainOnly(me())
  if (!c || c.gaps) return
  let map: Record<string, unknown>
  try { map = JSON.parse(env.content) } catch { return }
  const ever = new Set(c.lists.flatMap((l) => [...l.devices, ...l.readers]))
  for (const k of Object.keys(map)) if (E.isKey(k) && !ever.has(k)) strangeEntries.add(k)
}

/** §18.3: on a device's first list, the devices already on it ("These devices can read your messages"). */
let firstScreenSeen = false
const KV_FIRST_SCREEN = 'enc-first-screen'
export function firstScreenDevices(): string[] {
  if (firstScreenSeen || !device) return []
  const c = chainOnly(me())
  if (!c || !E.current(c).devices.includes(device.pub)) return []
  return E.current(c).devices.filter((d) => d !== device!.pub)
}
export async function firstScreenOk() { firstScreenSeen = true; await kvSet(KV_FIRST_SCREEN, true).catch(() => {}); keyListeners.forEach((l) => l()) }

/** How many devices my current list has (agent readers aside): Notes' "Encrypted for your N devices". */
export function myListDevices(): number { const c = chainOnly(me()); return c ? E.current(c).devices.length : 0 }

/** An answer seen for DEVICE (by another device of mine, or this one). */
export const answerFor = (d: string) => answeredHere.get(d)

/** §7.5 Share: every key this device can open (its store, after a read of the envelopes on the relay)
 *  and its version-1 secrets, in key packs to DEVICE; then the answer (8575). PROGRESS: packs sent. */
export async function shareWith(to: AddedDevice, progress: (done: number, total: number) => void): Promise<void> {
  if (!device) throw new Error('not set up')
  const got = await queryPaged('main', [{ kinds: [E.KEY], '#p': [me()] }], 8000, 1000, 60000).catch(() => null)
  if (got) { addEvents(got); await takeEnvelopes() }
  const list: E.PackKey[] = []
  for (const k of keys.values()) {
    if (k.group) list.push({ kind: 'group', k: k.k, K: k.K, group: k.conv.replace(/^g:/, '') })
    else if (k.people?.includes(me()) || k.conv.split(',').includes(me())) list.push({ kind: 'people', rumor: k.rumor ?? k.made?.rumor ?? E.keyRumor(device.pub, k.k, (k.people ?? k.conv.split(',')).map((a) => ({ account: a })), k.at) })
  }
  for (const v of v1Provider()) list.push({ kind: 'v1', secret: v.secret, lb: v.lb })
  const packs = E.packs(device.secret, me(), to.device, list)
  for (const [i, p] of packs.entries()) { await enqueueSigned('main', p); progress(i + 1, packs.length) }
  await answerDevice(to, 'shared')
}
/** §7.7 the answer, for every device of mine at once. */
export async function answerDevice(to: AddedDevice, what: 'shared' | 'removed') {
  if (!device) return
  await enqueueSigned('main', E.answer(device.secret, me(), to.ask ? { ask: to.ask } : { device: to.device }, what))
  answeredHere.set(to.device, { by: device.pub, answer: what })
  await kvSet(KV_ANSWERED, [...answeredHere.entries()]).catch(() => {})
  keyListeners.forEach((l) => l())
}

// ---- part 10: private groups (§4, §5, §6, §8, §16.1, §16.9, §18.4, §18.7, §22) -----------------

/** A private group (docs/one-shape.md: closed + private): always encrypted, up to 1000 devices. */
export const isPrivateGroup = (h?: string) => { const g = h ? groupOf(h) : undefined; return !!g && g.isPod && g.closed && g.inviteOnly }
/** P1.2: private here: its metadata says so, or a key of it was seen (once encrypted, never clear). */
export const privateHere = (h: string) => isPrivateGroup(h) || encConvs.has('g:' + h)
/** P2-1 + re-read P1-a: encrypted when a key of it was seen (any format), or private on format 3. */
export const groupEncrypts = (h: string) => encConvs.has('g:' + h) || (writesFormat3() && isPrivateGroup(h))
const myPrivateGroups = () => allGroups().filter((g) => g.isPod && g.closed && g.inviteOnly && g.members.has(me())).map((g) => g.id)
/** §16.9: what stays in clear in a private group (core.clearInGroup with this belfry's groups key). */
export const clearInGroup = (e: { kind: number; pubkey: string; tags: string[][] }) => E.clearInGroup(e, belfryGroupsKey())
const hOf = (e: { tags: string[][] }) => e.tags.find((t) => t[0] === 'h')?.[1]
/** The date of a private group's first key known here (§17.2: clear events after it never count). */
const groupFirstAt = (h: string) => encConvs.get('g:' + h)
const opened = new Set<string>() // 8572s opened into the store (their outer ids)

/** §17.2 (store.setRefuse): a clear event in a private group after its first key, outside §16.9's list. */
function refuseClear(e: NostrEvent): boolean {
  if (!writesFormat3()) return false // P2-1: format 1: never (the apps write clear there)
  if (e.kind === E.KEY || e.kind === E.MSG || !('sig' in e) || !e.sig) return false // (opened rumors carry no signature)
  const h = hOf(e)
  if (!h || !privateHere(h) || clearInGroup(e)) return false // §26: private OR ever encrypted here
  const f = groupFirstAt(h)
  return f !== undefined && e.created_at >= f // (the key's own second too: a same-second clear event never counts)
}

/** The group key named by a group 8572/8571 (d = K): held, or opened from the group's envelopes
 *  in the store (all events of that d: the entry set is checked against `set`, §18.4). */
const cantCheck = new Set<string>() // groups whose newest key's entry set doesn't add up here
function groupKeyFor(h: string, K: string): KeyRec | undefined | 'wait' {
  const envs = all(me()).filter((e) => e.kind === E.KEY && hOf(e) === h && e.tags.some((t) => t[0] === 'd' && t[1] === K))
  const held = keys.get(K)
  if (held) {
    if (held.conv !== 'g:' + h) return undefined // the review's P1.5: a key opens only its own group's messages
    // P1.3 / P2.3: more events of this d arrived: the entries judged again against the claimed set
    if (held.group && held.claimed && envCount.get(K) !== envs.length) judgeSet(h, held, envs)
    return held
  }
  if (!device) return undefined
  // §17.12 (the iOS review): keep looking until an envelope is ACCEPTED, never stop at the first that opens
  const dev = device
  const pick = E.pickGroupEnvelope(envs, (env) => E.openGroupEnvelope(dev.secret, env, h), (k) => E.acceptGroupKey(k, chainFor(k.account)))
  if (!pick) return undefined
  if ('wait' in pick) { want([pick.wait]); return 'wait' }
  const got = pick.key
  const svc = got.account === belfryGroupsKey() || got.account === belfry()
  const mc = chainOnly(me())
  const rec: KeyRec = { K, k: got.k, conv: 'g:' + h, group: true, mine: !!mc && mc.lists.some((l) => l.devices.includes(got!.maker)), service: svc, lists: '', claimed: got.set, at: got.at }
  judgeSet(h, rec, envs)
  holdKey(rec)
  void saveKeys()
  return rec
}
/** P1.3: a group key's `lists` (what pickKey adopts by) is its claimed set only while every entry seen
 *  adds up to it; otherwise '' (never adopted) and the group shows "Can't check". */
const envCount = new Map<string, number>()
function judgeSet(h: string, rec: KeyRec, envs: unknown[]) {
  envCount.set(rec.K, envs.length)
  if (rec.mine && !envs.length) return // (my own key: its envelopes may not be back from the relay yet)
  const match = E.deviceSet(E.entryKeys(envs)) === rec.claimed
  rec.lists = match ? rec.claimed! : ''
  if (match) cantCheck.delete(h); else cantCheck.add(h)
}
/** "Can't check who can read this" (group-who-cant-check): the entries seen don't add up to the set. */
export const groupCantCheck = (h: string) => cantCheck.has(h)

function privileged(r: E.Rumor): boolean {
  const Ls = r.kind === 1985 ? r.tags.filter((t) => t[0] === 'L').map((t) => t[1]) : []
  if (Ls.length && Ls.every((x) => x === 'belfry/action-result') && (r.pubkey === belfryGroupsKey() || r.pubkey === belfry())) return false // a service key's answer to a press (§24a)
  if (clearInGroup(r)) return true
  if (r.kind !== 1985) return false
  const L = r.tags.filter((t) => t[0] === 'L').map((t) => t[1])
  if (!L.some((x) => x?.startsWith('belfry/'))) return false
  if (L.every((x) => x === 'belfry/action')) return false
  // the ruling: pins, inbox, event and proposal status come through (the model's own admin / author checks apply)
  if (L.every((x) => ['belfry/pin', 'belfry/inbox', 'belfry/event-status', 'belfry/proposal-status'].includes(x))) return false
  if (L.every((x) => x === 'belfry/action-result') && (r.pubkey === belfryGroupsKey() || r.pubkey === belfry())) return false
  return true
}
/** Open the private groups' 8572s into the store: their rumors are the group's events (§6). */
async function openGroupMessages() {
  if (!device) return
  const add: NostrEvent[] = []
  for (const e of all(me())) {
    if (e.kind !== E.MSG || opened.has(e.id) || !('sig' in e) || !e.sig) continue
    const h = hOf(e), K = E.named(e)
    if (!h || !K || !privateHere(h)) continue // §26: private OR ever encrypted (a group's metadata changed later doesn't stop it)
    const key = groupKeyFor(h, K)
    if (key === 'wait' || !key) continue // its key not here (no entry for this device: from when it joined, §8) or lists coming
    let o: { seal: NostrEvent; rumor: E.Rumor }
    try { o = E.openGroupMessage(key.k, e) } catch { opened.add(e.id); continue }
    const v = E.acceptGroupMessage(e, o.seal, o.rumor, h, chainFor(o.rumor.pubkey))
    if (v === 'wait') { want([o.rumor.pubkey]); continue }
    opened.add(e.id)
    if (v !== 'ok') continue
    // P1.1: what the relay or the belfry decides never comes from inside an 8572: its clear-list kinds
    // (metadata, members, roles…) and 1985s in belfry/* label spaces are dropped; a press (belfry/action)
    // and the groups key's own answers (belfry/action-result) are what buttons are made of
    if (privileged(o.rumor)) continue
    noteConv('g:' + h, key.at)
    add.push({ ...o.rumor, sig: '' } as NostrEvent) // into the store as the group's event (mentions from the rumor only, §18.8)
  }
  if (add.length) addEvents(add)
}

/** For a press sent at once (buttons.ts): the group's encryption, outside the outbox. */
export const encryptGroupEvent = (tpl: { kind: number; content: string; tags: string[][]; created_at: number }) => wrapForGroup(tpl)

/** §5 + §16.1 + §18.7 sending in a private group: the members' devices (39002 × their lists, read
 *  whole on this connection), the newest key whose entry set is exactly that device set (mine, or
 *  another member's), else a new key and its envelopes (signed by my account through the signer). */
const membersReadOn = new Map<string, number>() // group → the groups-relay connection its 39002 was read on
async function wrapForGroup(tpl: { kind: number; content: string; tags: string[][]; created_at: number }) {
  const h = hOf(tpl)
  if (!h || !groupEncrypts(h) || clearInGroup({ ...tpl, pubkey: me() }) || tpl.kind === E.KEY || tpl.kind === E.MSG) return null
  if (!device) throw new Error('timeout: this device isn’t set up for encrypted messages yet')
  // P2.4: the member list from a 39002 read on this connection, with me in it (never a stale or empty cache)
  if (membersReadOn.get(h) !== connectionOf('groups')) {
    const got = await queryPaged('groups', [{ kinds: [39002], '#d': [h] }], 5000).catch(() => null)
    if (!got) throw new Error('timeout: couldn’t check who can read this group')
    addEvents(got)
    membersReadOn.set(h, connectionOf('groups'))
  }
  const g = groupOf(h)
  if (!g || !g.members.has(me())) throw new Error('timeout: couldn’t check who can read this group')
  const people = [...new Set([...g.members, me()])].sort()
  const toRead = people.filter((a) => chainFor(a) !== E.SERVICE && (!readOnThisConnection(a) || chainFor(a) === undefined))
  if (toRead.length && !(await readLists(toRead, 5000))) throw new Error('timeout: couldn’t check who can read this group')
  // §23 (end): a member with no list at all after that read: read once more, then skipped for this message only
  const none = people.filter((a) => chainFor(a) === undefined)
  if (none.length && !(await readLists(none, 5000))) throw new Error('timeout: couldn’t check who can read this group') // (a failed second read: retried, P3)
  if (listedHere() !== 'yes') throw new Error('timeout: this device isn’t set up for encrypted messages yet')
  const devices: string[] = [], lines: { account: string; id: string }[] = []
  for (const a of people) {
    const c = chainFor(a)
    if (c === undefined) continue // no list found after two reads: skipped for this message (§23); an empty list: no devices, no entry
    if (c === E.SERVICE) { devices.push(a); continue }
    if (guestTest(a)) continue // a guest member gets no entry for now (the lead: guests can't take part in private groups yet)
    if (c.fork || c.invalidAbove) throw new Error('timeout: couldn’t check who can read this group')
    devices.push(...E.receivers(E.current(c))); lines.push({ account: a, id: E.current(c).id })
  }
  if (devices.length > E.MAX_GROUP_DEVICES) throw new Error('error: Too many devices in this group to encrypt for everyone.') // §4: it never falls back
  const set = E.deviceSet(devices)
  let key = E.pickKey([...keys.values()], 'g:' + h, set) as KeyRec | undefined
  // re-read P1-3: another's key judged again right before use (its envelope events counted now); adopted
  // only while every entry seen adds up to what it claims
  if (key && key.claimed && !key.unconfirmedGroup) {
    const envs = all(me()).filter((e) => e.kind === E.KEY && hOf(e) === h && e.tags.some((t) => t[0] === 'd' && t[1] === key!.K))
    judgeSet(h, key, envs)
    if (!key.mine && key.lists !== set) key = undefined
  }
  const before: { kind: number; created_at: number; tags: string[][]; content: string }[] = []
  if (!key) {
    const { k, K } = E.newKey()
    const envs = E.groupEnvelopes(device.secret, k, h, devices, E.listsHash(lines))
    key = { K, k, conv: 'g:' + h, group: true, mine: true, lists: set, claimed: set, at: Math.floor(Date.now() / 1000), unconfirmedGroup: envs }
    holdKey(key)
    await saveKeys()
  }
  // P2.2: its envelopes until confirmed out (kept with the key, across reloads), sent again with fresh dates
  if (key.unconfirmedGroup?.length) before.push(...key.unconfirmedGroup.map((x) => ({ ...x, created_at: Math.floor(Date.now() / 1000) })))
  const rumor = E.withId({ ...tpl, pubkey: me() })
  // §17.14: outer p only for the people the text mentions (nostr:npub…), for the belfry's push (P3)
  const mentions: string[] = []
  for (const m of tpl.content.matchAll(/nostr:(npub1[02-9ac-hj-np-z]{58})/g)) { try { const d = nip19.decode(m[1]); if (d.type === 'npub') mentions.push(d.data) } catch { /* not an npub */ } }
  const K = key.K, rec = key
  return { before, beforeOut: () => { if (rec.unconfirmedGroup) { delete rec.unconfirmedGroup; void saveKeys() } }, event: E.groupMessage(device.secret, rumor, K, h, undefined, [...new Set(mentions)]) }
}

// ---- for the screens (Who can read this, Me › Devices) --------------------------------------

/** ACCOUNT's current list as the screens show it: its devices and readers (undefined: no list known;
 *  the service keys: themselves). Others are only ever shown as a count. */
export function devicesOf(account: string): { devices: string[]; readers: string[]; since?: Map<string, number> } | undefined {
  const c = chainFor(account)
  if (c === undefined) return undefined
  if (c === E.SERVICE) return { devices: [account], readers: [] }
  const cur = E.current(c)
  // when each one first appeared on a list (Added …), for my own rows
  const since = new Map<string, number>()
  for (const l of c.lists) for (const d of [...l.devices, ...l.readers]) if (!since.has(d)) since.set(d, l.created_at)
  return { devices: cur.devices, readers: cur.readers, since }
}
// ---- encrypted/removed-device: this device was removed (§16.6) ----------------------------------

export interface Removed { at: number; by?: string }
let removed: Removed | null = null
const removedListeners = new Set<() => void>()
export const removedNow = () => removed
export const onRemoved = (l: () => void) => { removedListeners.add(l) }
/** GET /api/v1/sessions/removed, NIP-98 signed by this DEVICE's key (a revoked session can't sign
 *  through the signer any more): {removed: false} | {removed: true, at, by_name?} | 404. */
export async function checkRemoved(): Promise<void> {
  if (!device || removed) return
  const url = `${BELFRY.api}/sessions/removed`
  const auth = finalizeEvent({ kind: 27235, content: '', created_at: Math.floor(Date.now() / 1000), tags: [['u', url], ['method', 'GET']] }, device.secret)
  let d: { removed?: unknown; at?: unknown; by_name?: unknown }
  try {
    const r = await fetch(url, { headers: { Authorization: 'Nostr ' + btoa(JSON.stringify(auth)) } })
    if (!r.ok) return
    d = await r.json()
  } catch { return }
  if (d.removed !== true) return
  // the ruling: wipe only when this device is also gone from my current list, read on this connection
  if (!(await readLists([me()], 5000))) { setTimeout(() => void checkRemoved(), 10_000); return } // (lists not readable yet: again soon)
  const mine = chainOnly(me())
  if (!mine || E.current(mine).devices.includes(device.pub)) return
  const at = typeof d.at === 'number' ? d.at : typeof d.at === 'string' ? Math.floor(Date.parse(d.at) / 1000) || 0 : 0
  removed = { at, ...(typeof d.by_name === 'string' && d.by_name.trim() ? { by: d.by_name.trim().slice(0, 80) } : {}) }
  removedListeners.forEach((l) => l())
}

/** encrypted/signout-only: this device is alone on my list and holds keys (its earlier messages go with it). */
export function encOnlyDevice(): boolean {
  const c = chainOnly(me())
  return !!device && !!c && E.current(c).devices.length === 1 && E.current(c).devices[0] === device.pub && keys.size > 0
}
/** Is CONV (n:…) a format-3 conversation here (keys or messages seen for it, §18.6). */
export const encConvHere = (conv: string) => conv.startsWith('n:') && isEncConv([...conv.slice(2).split(','), me()])

// tests against a local relay (TEST_RELAYS): public facts only, never a secret
export const encProbe = async (h: string) => { const got = await queryPaged('groups', [{ kinds: [39002], '#d': [h] }], 5000).catch((e) => String(e)); return { got: Array.isArray(got) ? got.length : got, conn: connectionOf('groups'), members: [...(groupOf(h)?.members ?? [])].length, mine: !!groupOf(h)?.members.has(me()) } }
export const encWhy = (e: NostrEvent) => { const h = hOf(e) ?? ''; return { private: isPrivateGroup(h), first: groupFirstAt(h) ?? null, clear: clearInGroup(e), refused: refuseClear(e), sig: !!e.sig } }
export const encTest = () => ({ device: encDevice(), keys: encKeyCount(), listed: listedHere(), on: encOn(), convs: [...encConvs.keys()], relay: BELFRY.mainRelay })
