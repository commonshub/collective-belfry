// Encrypted conversations, format 3, on this device (docs/encrypted.md with §16–§20): the device
// lists of the people I talk to, the conversation keys this device holds, opening 8572s for dm.ts
// and encrypting what it sends. People conversations (private messages, group messages, Notes);
// private groups come later. Writing format 3 only when belfry.json says so (access.ts); reading
// always. Every signed-in chat session runs it, guests too (the lead's ruling: they are devices).
//
// Keys live in this account's own database (kv 'enc-keys'), never localStorage, never logged.
import { hexToBytes } from '@noble/hashes/utils'
import { getPublicKey, type Filter, type NostrEvent } from 'nostr-tools'
import { BELFRY } from '../../config'
import { belfryGroupsKey, belfryNostrKey, encryptedFormat, loadAccessInfo, onAccessInfo } from '../access'
import { kvGet, kvSet } from '../db'
import { addEvents, all, latestCreatedAt, outboxItems, subscribe } from '../store'
import { connectionOf, me, onOutbox, queryPaged, setExtraMainFilters, setPagedMainFilters } from '../sync'
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
function noteConv(conv: string, at: number) { const was = encConvs.get(conv); if (was === undefined || at < was) encConvs.set(conv, at) }
function holdKey(r: KeyRec) {
  const had = keys.get(r.K)
  if (had) { // §17.12: the first valid declaration of a K fixes its people; only "unconfirmed" may clear
    if (had.unconfirmed && !r.unconfirmed) delete had.unconfirmed
    return false
  }
  keys.set(r.K, r)
  byAlias.set(E.alias(r.K, me()), r.K)
  if (!r.group) noteConv(r.conv, r.at)
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

export async function startEnc(clientSecret: string) {
  if (device || !/^[0-9a-f]{64}$/.test(clientSecret)) return
  const secret = hexToBytes(clientSecret)
  device = { secret, pub: getPublicKey(secret) }
  startedAt = Date.now()
  for (const r of (await kvGet<KeyRec[]>(KV_KEYS).catch(() => undefined)) ?? []) if (r && E.isKey(r.K) && E.isKey(r.k)) holdKey(r)
  const convs = (await kvGet<[string, number][]>(KV_CONVS).catch(() => undefined)) ?? []
  for (const [c, at] of Array.isArray(convs) ? convs : []) if (typeof c === 'string' && typeof at === 'number') noteConv(c, at)
  setExtraMainFilters(filters, 'enc')
  // belfry.json: the belfry key (who signs lists) and the format; its filters again once known
  onAccessInfo(() => setExtraMainFilters(filters, 'enc'))
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
    if (holdKey({ K, k: r.content, conv: E.peopleId(people), people, mine, service: r.pubkey === belfry() || r.pubkey === belfryGroupsKey(), lists, at: r.created_at })) changed = true
  }
  if (changed) { await saveKeys(); keyListeners.forEach((l) => l()) }
}
const keyListeners = new Set<() => void>()
/** Called when new keys arrive (dm.ts reads the messages waiting for them again). */
export const onEncKeys = (l: () => void) => { keyListeners.add(l) }

export type Opened = { ok: true; rumor: NostrEvent; conv: string } | { ok: false; retry: boolean; state?: 'opening' | 'unreadable' }
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
        dRead.set(d, { at: Date.now(), found: got.length > 0 })
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
    return { ok: false, retry: true, state: 'opening' }
  }
  if (v === 'no') return { ok: false, retry: false }
  noteConv(key.conv, key.at)
  return { ok: true, rumor: o.rumor as unknown as NostrEvent, conv: key.conv }
}

/** Is this people conversation encrypted (§18.6), by its accounts; its first key's date. */
export const isEncConv = (accounts: string[]) => encConvs.has(E.peopleId(accounts))
export const encFirstAt = (accounts: string[]) => encConvs.get(E.peopleId(accounts))

// ---- sending ---------------------------------------------------------------------------------

export class Held extends Error {
  constructor(public reason: 'no_list' | 'not_listed' | 'no_device' | 'too_many' | 'full' | 'fork' | 'mail' | 'format', public who?: string) { super('held: ' + reason) }
}
/** Words for a held message (state-held; §17.4, §17.5). */
export function heldText(h: Held, name: (pk: string) => string): string {
  if (h.reason === 'no_device' && h.who) return `${name(h.who)} isn't signed in on any device · It will be sent when they are`
  if (h.reason === 'not_listed') return 'This device isn’t set up for encrypted messages yet'
  if (h.reason === 'full') return 'You have 20 devices signed in · Remove one to send encrypted messages here'
  if (h.reason === 'too_many') return 'Too many devices to encrypt for everyone here'
  if (h.reason === 'mail') return 'E-mail from the chat waits for this belfry’s update'
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
  if (!device) throw new Held('not_listed')
  const m = me()
  const all = [...new Set([m, ...people])].sort()
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
  // a key whose envelopes never went out and are too old for the relay now (±5 min, §18.1): a new one
  if (key?.unconfirmed?.some((env) => Math.floor(Date.now() / 1000) - env.created_at > 240)) {
    const queuedIds = new Set(outboxItems().map((o) => o.signed?.id).filter(Boolean))
    if (key.unconfirmed.some((env) => !queuedIds.has(env.id))) { keys.delete(key.K); byAlias.delete(E.alias(key.K, m)); key = undefined; await saveKeys() }
  }
  if (!key) {
    const { k, K } = E.newKey()
    const r = E.keyRumor(device.pub, k, per.map((p) => ({ account: p.account, ...(p.list ? { list: p.list } : {}) })))
    const envs = E.peopleEnvelopes(device.secret, r, per.filter((p) => p.devices.length))
    const rec: KeyRec = { K, k, conv, people: all, mine: true, lists, at: r.created_at, unconfirmed: envs }
    holdKey(rec)
    key = rec
    await saveKeys()
  }
  // its envelopes not confirmed out yet (a refused send, a closed tab): again, unless still queued
  const queued = new Set(outboxItems().map((o) => o.signed?.id).filter(Boolean))
  for (const env of key.unconfirmed ?? []) if (!queued.has(env.id)) out.push(env)
  const to = all.filter((a) => !missing.includes(a))
  out.push(...E.peopleMessage(device.secret, rumor, key.K, to, m))
  noteConv(conv, key.at)
  return { events: out, missing }
}

// tests against a local relay (TEST_RELAYS): public facts only, never a secret
export const encTest = () => ({ device: encDevice(), keys: encKeyCount(), listed: listedHere(), on: encOn(), convs: [...encConvs.keys()], relay: BELFRY.mainRelay })
