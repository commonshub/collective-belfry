// Encrypted conversations, format 3, on this device (docs/encrypted.md with §16–§19): the device
// lists of the people I talk to, the conversation keys this device holds, opening 8572s for dm.ts
// and encrypting what it sends. People conversations (private messages, group messages, Notes);
// private groups come later. Nothing here runs unless belfry.json says format 3 (access.ts) or a
// test switches it on.
//
// Keys live in this account's own database (kv 'enc-keys'), never localStorage, never logged.
import { hexToBytes } from '@noble/hashes/utils'
import { getPublicKey, type Filter, type NostrEvent } from 'nostr-tools'
import { BELFRY } from '../../config'
import { belfryGroupsKey, belfryNostrKey, encryptedFormat, loadAccessInfo, onAccessInfo } from '../access'
import { kvGet, kvSet } from '../db'
import { addEvents, all, latestCreatedAt, subscribe } from '../store'
import { me, queryPaged, setExtraMainFilters, setPagedMainFilters } from '../sync'
import * as E from './core'

let device: { secret: Uint8Array; pub: string } | null = null
let startedAt = 0
/** The device key of this session (public), once started. */
export const encDevice = () => device?.pub ?? ''
export const encOn = () => !!device && encryptedFormat() === 3

// ---- keys this device holds ------------------------------------------------------------------

interface KeyRec extends E.HeldKey { /** the people of the conversation (people keys) */ people?: string[]; group?: boolean }
const keys = new Map<string, KeyRec>() // K → key
const byAlias = new Map<string, string>() // alias(K, me) → K
/** Conversations (peopleId) known to be encrypted here (§18.6: from envelopes and messages seen). */
const encConvs = new Set<string>()
const KV_KEYS = 'enc-keys', KV_CONVS = 'enc-convs'
async function saveKeys() { await kvSet(KV_KEYS, [...keys.values()]).catch(() => {}); await kvSet(KV_CONVS, [...encConvs]).catch(() => {}) }
function holdKey(r: KeyRec) {
  if (keys.has(r.K)) return false // §17.12: the first valid declaration of a K fixes its people
  keys.set(r.K, r)
  byAlias.set(E.alias(r.K, me()), r.K)
  if (!r.group) encConvs.add(r.conv)
  return true
}
/** How many conversation keys this device holds (tests, Diagnostics). */
export const encKeyCount = () => keys.size

// ---- device lists --------------------------------------------------------------------------

let partners: string[] = []
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
  void queryPaged('main', [{ kinds: [E.LIST], authors: [belfry()], '#p': ask }], 5000).then((got) => { if (got?.length) addEvents(got) })
}
/** The people I talk to (dm.ts): their lists are kept on the live subscription. */
export function setEncPartners(ps: string[]) {
  const next = [...new Set(ps)].sort()
  if (next.join() === partners.join()) return
  partners = next
  if (device) setExtraMainFilters(filters, 'enc')
}
const belfry = () => belfryNostrKey()
/** §17.1 + §20: ACCOUNT's devices: its chain; SERVICE only for the belfry key and the groups key
 *  (their own device); undefined when no list is known (Held). */
export function chainFor(account: string): E.Devices {
  if (account && (account === belfry() || account === belfryGroupsKey())) return E.SERVICE
  if (!belfry()) return undefined
  return E.chainOf(all().filter((e) => e.kind === E.LIST), belfry(), account) ?? undefined
}
/** This device on its own account's current list. */
export function listedHere(): 'yes' | 'not_yet' | 'held' {
  const c = chainFor(me())
  if (c && c !== E.SERVICE && device && E.current(c).devices.includes(device.pub)) return 'yes'
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
  for (const c of (await kvGet<string[]>(KV_CONVS).catch(() => undefined)) ?? []) if (typeof c === 'string') encConvs.add(c)
  setExtraMainFilters(filters, 'enc')
  // belfry.json: the belfry key (who signs lists) and the format; its filters again once known
  onAccessInfo(() => setExtraMainFilters(filters, 'enc'))
  void loadAccessInfo()
  setPagedMainFilters(() => (device && belfry() ? [{ kinds: [E.LIST], authors: [belfry()], '#p': [...new Set([me(), ...partners])] }] : []), 'enc')
  let t: ReturnType<typeof setTimeout> | null = null
  subscribe(() => { t ??= setTimeout(() => { t = null; void takeEnvelopes() }, 150) })
}

// ---- receiving -------------------------------------------------------------------------------

/** Envelopes for my account: my entries opened and judged; held when accepted (§5, §16.2). */
const envSeen = new Set<string>()
const noEntry = new Set<string>() // aliases whose envelopes have no entry for this device
async function takeEnvelopes() {
  if (!device) return
  const m = me()
  let changed = false
  for (const e of all()) {
    if (e.kind !== E.KEY || envSeen.has(e.id) || !e.tags.some((t) => t[0] === 'p' && t[1] === m)) continue
    const d = E.named(e)
    let o: ReturnType<typeof E.openPeopleEnvelope> = null
    try { o = E.openPeopleEnvelope(device.secret, e) } catch { envSeen.add(e.id); continue } // a bad entry: skipped, never blocking (§17.12)
    if (!o) { if (d) noEntry.add(d); envSeen.add(e.id); markConvByAlias(d); continue }
    const v = E.acceptKeyRumor(o.rumor, m, chainFor)
    if (v === 'wait') { want(o.rumor.tags.filter((t) => t[0] === 'p').map((t) => t[1])); continue } // judged again when lists come
    envSeen.add(e.id)
    if (v !== 'ok') continue
    const K = o.rumor.tags.find((t) => t[0] === 'K')![1]
    const people = o.rumor.tags.filter((t) => t[0] === 'p').map((t) => t[1])
    const mc = chainFor(m)
    const mine = !!mc && mc !== E.SERVICE && mc.lists.some((l) => l.devices.includes(o!.rumor.pubkey))
    const lists = E.peopleLists(o.rumor.tags.filter((t) => t[0] === 'list').map((t) => ({ account: t[1], id: t[2] })))
    if (holdKey({ K, k: o.rumor.content, conv: E.peopleId(people), people, mine, service: o.rumor.pubkey === belfry() || o.rumor.pubkey === belfryGroupsKey(), lists, at: o.rumor.created_at })) changed = true
  }
  if (changed) { await saveKeys(); keyListeners.forEach((l) => l()) }
}
const markConvByAlias = (_d?: string) => { /* an envelope for me without my entry: its conversation is encrypted (§18.6); which one isn't known without a key */ }
const keyListeners = new Set<() => void>()
/** Called when new keys arrive (dm.ts reads the messages waiting for them again). */
export const onEncKeys = (l: () => void) => { keyListeners.add(l) }

export type Opened = { ok: true; rumor: NostrEvent; conv: string } | { ok: false; retry: boolean; state?: 'opening' | 'unreadable' }
const asked = new Map<string, number>()
/** An 8572 for me: its rumor when it opens and is accepted; retry when its key or the author's
 *  lists aren't here yet (never marked seen on that, §6). */
export async function openEnc(e: NostrEvent): Promise<Opened> {
  if (!device) return { ok: false, retry: true, state: 'opening' }
  const d = E.named(e)
  if (!d) return { ok: false, retry: false }
  let K = byAlias.get(d)
  if (!K) {
    await takeEnvelopes()
    K = byAlias.get(d)
  }
  if (!K) {
    if (noEntry.has(d)) return { ok: false, retry: true, state: 'unreadable' } // kept: a pack may bring it (§7)
    // not here yet: ask the relay for its envelopes once a minute
    if (Date.now() - (asked.get(d) ?? 0) > 60_000) {
      asked.set(d, Date.now())
      void queryPaged('main', [{ kinds: [E.KEY], '#p': [me()], '#d': [d] }], 5000).then((got) => { if (got?.length) addEvents(got) })
    }
    return { ok: false, retry: true, state: 'opening' }
  }
  const key = keys.get(K)!
  let o: { seal: NostrEvent; rumor: E.Rumor }
  try { o = E.openPeopleMessage(key.k, e) } catch { return { ok: false, retry: false } } // a copy that doesn't open with the right k: dropped
  const v = E.acceptPeopleMessage(e, o.seal, o.rumor, key.conv, chainFor(o.rumor.pubkey))
  if (v === 'wait') { want([o.rumor.pubkey]); return { ok: false, retry: true, state: 'opening' } }
  if (v === 'no') return { ok: false, retry: false }
  encConvs.add(key.conv)
  return { ok: true, rumor: o.rumor as unknown as NostrEvent, conv: key.conv }
}

/** Is this people conversation encrypted (§18.6), by its accounts. */
export const isEncConv = (accounts: string[]) => encConvs.has(E.peopleId(accounts))

// ---- sending ---------------------------------------------------------------------------------

export class Held extends Error {
  constructor(public reason: 'no_list' | 'not_listed' | 'no_device' | 'too_many' | 'full', public who?: string) { super('held: ' + reason) }
}
/** Words for a held message (state-held; §17.4, §17.5). */
export function heldText(h: Held, name: (pk: string) => string): string {
  if (h.reason === 'no_device' && h.who) return `${name(h.who)} isn't signed in on any device · It will be sent when they are`
  if (h.reason === 'not_listed') return 'This device isn’t set up for encrypted messages yet'
  if (h.reason === 'too_many') return 'Too many devices to encrypt for everyone here'
  return 'Not sent: couldn’t check who can read this conversation'
}

/** §4 + §5: refresh the lists of PEOPLE on this connection (at most 3 s). */
async function refreshLists(people: string[]) {
  if (!belfry()) return
  const got = await queryPaged('main', [{ kinds: [E.LIST], authors: [belfry()], '#p': people }], 3000, 20, 3000).catch(() => null)
  if (got?.length) addEvents(got)
}

/** The events that carry RUMOR to PEOPLE (accounts, me included) encrypted: the key's envelopes
 *  first when a new key is made, then one copy per person. Null: not encrypted (a conversation
 *  that starts over 100 devices: no lock). Throws Held when it can't be encrypted now. */
export async function encryptFor(rumor: E.Rumor, people: string[], skip: string[] = []): Promise<{ events: NostrEvent[]; missing: string[] } | null> {
  if (!device) throw new Held('not_listed')
  const m = me()
  const all = [...new Set([m, ...people])].sort()
  await refreshLists(all)
  if (listedHere() !== 'yes') throw new Held('not_listed')
  const per: { account: string; list?: string; devices: string[] }[] = []
  for (const a of all) {
    const c = chainFor(a)
    if (c === undefined) throw new Held('no_list', a) // §17.1: never decided by absence
    per.push(c === E.SERVICE ? { account: a, devices: [a] } : { account: a, list: E.current(c).id, devices: E.receivers(E.current(c)) })
  }
  // §17.5: a person with no device: alone, held; with others, sent for the others
  const missing = per.filter((p) => p.account !== m && !p.devices.length).map((p) => p.account)
  const others = per.filter((p) => p.account !== m)
  if (others.length && missing.length === others.length) throw new Held('no_device', missing[0])
  const total = per.reduce((n, p) => n + p.devices.length, 0)
  const conv = E.peopleId(all)
  if (total > E.MAX_PEOPLE_DEVICES) { if (encConvs.has(conv)) throw new Held('too_many'); return null } // §17.2
  const lists = E.peopleLists(per.map((p) => ({ account: p.account, id: p.list ?? '' })))
  const out: NostrEvent[] = []
  let key = E.pickKey([...keys.values()], conv, lists)
  if (!key) {
    const { k, K } = E.newKey()
    const r = E.keyRumor(device.pub, k, per.map((p) => ({ account: p.account, ...(p.list ? { list: p.list } : {}) })))
    out.push(...E.peopleEnvelopes(device.secret, r, per.filter((p) => p.devices.length)))
    const rec: KeyRec = { K, k, conv, people: all, mine: true, lists, at: r.created_at }
    holdKey(rec)
    key = rec
    await saveKeys()
  }
  const to = all.filter((a) => !missing.includes(a) && !skip.includes(a))
  out.push(...E.peopleMessage(device.secret, rumor, key.K, to, m))
  encConvs.add(conv)
  return { events: out, missing }
}

// tests against a local relay (TEST_RELAYS): public facts only, never a secret
export const encTest = () => ({ device: encDevice(), keys: encKeyCount(), listed: listedHere(), on: encOn(), convs: [...encConvs], relay: BELFRY.mainRelay })
