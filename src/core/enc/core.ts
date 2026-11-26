// Encrypted conversations, format 3: one key per conversation (collective-belfry docs/encrypted.md,
// with its §16 readings). The pure rules: device lists (8570), conversation keys and their
// envelopes (8571), messages (8572), key packs (8573), asks and answers (8574, 8575), with §17
// and §18 (Android's two reviews). No storage,
// no network, no browser APIs: the vectors run this in Node (tools/vectors/check.mjs against
// vectors/encrypted.json); the runtime (enc/*.ts) does the rest.
//
// Never log or throw a secret or a rumor's text: errors here say what failed only.
import { sha256 } from '@noble/hashes/sha256'
import { bytesToHex, hexToBytes, randomBytes } from '@noble/hashes/utils'
import { getPublicKey, nip44, type NostrEvent } from 'nostr-tools'
import { isKey, rumorJson, signWith, validEvent, withId, SEAL, SILENT, type Rumor } from '../sealedcore'

export { isKey, withId, type Rumor }
export const LIST = 8570, KEY = 8571, MSG = 8572, PACK = 8573, ASK = 8574, ANSWER = 8575
export const MAX_LIST = 20 // devices on one person's list (readers aside)
export const MAX_PEOPLE_DEVICES = 100 // a people conversation over this is not encrypted (no lock)
export const MAX_GROUP_DEVICES = 1000 // a private group over this refuses to send
export const KEY_AGE = 30 * 86400
export const GRACE = 600 // §6: a dropped device's messages up to 10 minutes after the list that dropped it
export const MAX_ENVELOPE = 60_000 // §16.1: bytes of content in one envelope event
export const MAX_PACK = 40_000 // §7.5: bytes of the key list in one pack
export const MAX_RUMOR = 40_000
export const FUTURE = 900 // §17.8: a key dated more than 15 minutes ahead is refused
export const SKEW = 300 // §18.2: a rumor is never newer than its outer event by more than 5 minutes

const N = /^[1-9][0-9]{0,8}$/
const utf8 = (s: string) => new TextEncoder().encode(s).length
const tagValues = (e: { tags: string[][] }, name: string) => e.tags.filter((t) => t[0] === name && typeof t[1] === 'string').map((t) => t[1])
const oneTag = (e: { tags: string[][] }, name: string) => { const v = tagValues(e, name); return v.length === 1 ? v[0] : undefined }
const conv = (secret: Uint8Array, pub: string) => nip44.v2.utils.getConversationKey(secret, pub)
const now0 = () => Math.floor(Date.now() / 1000)

/** What would otherwise be random (the vectors fix it). */
export interface Fixed { at?: number; sealAt?: number; oneTime?: Uint8Array; nonce?: (i: number) => Uint8Array; aux?: Uint8Array }
const nonceOf = (f: Fixed | undefined, i: number) => f?.nonce?.(i)

// ---- §3 device lists ---------------------------------------------------------------------------

/** §20 "three answers, never two": an account's devices are its list (a Chain), SERVICE (exactly the
 *  belfry key or the groups key: its own device), or undefined (no list: Held). Never null. */
export const SERVICE = 'service' as const
export type Devices = Chain | typeof SERVICE | undefined

export interface DeviceList { id: string; account: string; n: number; prev?: string; devices: string[]; readers: string[]; created_at: number }

/** E as a device list of the belfry BELFRY, or null. Devices: 0 to 20 keys, each once; readers
 *  (["device", key, "reader"]) apart, not counted in the 20; an unknown marker skips that entry; §17.1 the account's own key as ["device", account, "self"]
 *  (an agent, an own-key member) is one of its devices. */
export function parseList(e: unknown, belfry: string): DeviceList | null {
  if (!validEvent(e) || e.kind !== LIST || e.pubkey !== belfry || e.content !== '') return null
  const account = oneTag(e, 'p'), n = oneTag(e, 'n'), prevs = tagValues(e, 'prev')
  if (!isKey(account) || !n || !N.test(n)) return null
  const num = Number(n)
  if (num === 1 ? prevs.length !== 0 : prevs.length !== 1 || !isKey(prevs[0])) return null
  const devices: string[] = [], readers: string[] = []
  for (const t of e.tags) {
    if (t[0] !== 'device') continue
    if (!isKey(t[1]) || t.length > 3 || (t[2] === 'self' && t[1] !== account)) return null
    if (t.length === 3 && t[2] !== 'reader' && t[2] !== 'self') continue // a marker this app doesn't know: that entry is skipped, the list stays
    const into = t[2] === 'reader' ? readers : devices
    if (devices.includes(t[1]) || readers.includes(t[1])) return null
    into.push(t[1])
  }
  if (devices.length > MAX_LIST) return null
  // §18.5: a self entry only alone on its list (readers allowed)
  if (e.tags.some((t) => t[0] === 'device' && t[2] === 'self') && devices.length !== 1) return null
  return { id: e.id, account, n: num, ...(num > 1 ? { prev: prevs[0] } : {}), devices, readers, created_at: e.created_at }
}

export interface Chain {
  account: string
  /** one list per n seen, ascending (§17.7: the current list is the highest n, a missing older one doesn't block) */
  lists: DeviceList[]
  /** some n below the highest is missing: an old seal may be judged once it comes */
  gaps: boolean
  /** §17.7: two different lists with the same n, or a prev that doesn't name the list before: warn, for good */
  fork: boolean
  /** a list event from the belfry for this account with a higher n than every valid one, that isn't
   *  valid (self with devices, more than 20…): the newest word can't be read (hold, §18.5) */
  invalidAbove: boolean
}

/** ACCOUNT's lists among EVENTS (several for one n: a fork, the lower id kept). Null when there is none. */
export function chainOf(events: unknown[], belfry: string, account: string): Chain | null {
  const byN = new Map<number, DeviceList[]>()
  let badTop = 0
  for (const e of events) {
    const l = parseList(e, belfry)
    if (!l) { // not valid: remember its n when it is the belfry's signed word about this account (N4: signature checked)
      if (validEvent(e) && e.kind === LIST && e.pubkey === belfry && e.tags.some((t) => t[0] === 'p' && t[1] === account)) {
        const n = Number(e.tags.find((t) => t[0] === 'n')?.[1]); if (Number.isInteger(n) && n > badTop) badTop = n
      }
      continue
    }
    if (l.account !== account) continue
    const at = byN.get(l.n) ?? []
    if (!at.some((x) => x.id === l.id)) byN.set(l.n, [...at, l])
  }
  if (!byN.size) return null // (an account whose only lists are invalid has no list: Held)
  let fork = false
  const lists = [...byN.keys()].sort((a, b) => a - b).map((n) => {
    const c = byN.get(n)!.sort((a, b) => (a.id < b.id ? -1 : 1))
    if (c.length > 1) fork = true
    return c[0]
  })
  lists.forEach((l, i) => { const before = lists[i - 1]; if (before && before.n === l.n - 1 && l.prev !== before.id) fork = true })
  const top = lists[lists.length - 1].n
  return { account, lists, gaps: lists.length < top, fork, invalidAbove: badTop > top }
}

/** §17.7 what changed on each list: the devices it added and removed against the list before (the
 *  first list known adds nothing: "none at all for the first list an app ever sees"). */
export function changes(c: Chain): { list: string; n: number; added: string[]; removed: string[]; readersAdded: string[] }[] {
  return c.lists.slice(1).map((l, i) => {
    const b = c.lists[i]
    return { list: l.id, n: l.n, added: l.devices.filter((d) => !b.devices.includes(d)), removed: b.devices.filter((d) => !l.devices.includes(d)), readersAdded: l.readers.filter((d) => !b.readers.includes(d)) }
  })
}

/** §17.6: OLDER was on a list of the account before DEVICE first was. */
export function listedBefore(c: Chain, older: string, device: string): boolean {
  const first = (d: string) => c.lists.findIndex((l) => l.devices.includes(d))
  const a = first(older), b = first(device)
  return a >= 0 && b >= 0 && a < b // §20: each device's first list ever; a device never listed has no "before"
}
/** The list right after index I is the next n (no list missing in between). */
const nextKnown = (c: Chain, i: number) => { const nx = c.lists[i + 1]; return nx && nx.n === c.lists[i].n + 1 ? nx : undefined
}

export const current = (c: Chain) => c.lists[c.lists.length - 1]
export const listById = (c: Chain, id: string) => c.lists.find((l) => l.id === id)
/** Every key that receives copies for this account now: devices and readers. */
export const receivers = (l: DeviceList) => [...l.devices, ...l.readers]

/** §6: DEVICE may sign for the account at AT (the outer event's date, §18.2): on the list that was
 *  current at AT, or on the one before it when AT is not later than GRACE after the list that dropped
 *  it (a device dropped and listed again isn't accepted for the gap). Readers never sign as the
 *  account. Undefined: can't tell yet: a list around AT is missing, or (§20, the review's P2.3) the
 *  device is on no list known here and AT isn't before the newest list: its list may be behind. */
export function signsAt(c: Chain, device: string, at: number): boolean | undefined {
  const v = listedAt(c, device, at)
  if (v !== false) return v
  // (a key on a list as a reader is known: it never signs as the account, so no)
  if (!c.lists.some((l) => l.devices.includes(device) || l.readers.includes(device)) && at >= current(c).created_at) return undefined
  return false
}

/** §16.2 for a group: DEVICE was on the account's list that was current at AT (with the grace;
 *  §21: the first list that has it counts from its date − 5 minutes).
 *  Undefined: the list current at AT may be missing. */
export function listedAt(c: Chain, device: string, at: number): boolean | undefined {
  // §21: a slow clock dates a new device's first messages (and its first group key) before the list
  // that added it: that list counts from its date − SKEW
  // (Android: at the start of every run of lists that have it, a device listed again included)
  const start = c.lists.find((l, i) => l.devices.includes(device) && !c.lists[i - 1]?.devices.includes(device) && at < l.created_at && l.created_at <= at + SKEW)
  if (start) at = start.created_at
  let i = -1
  c.lists.forEach((l, j) => { if (l.created_at <= at) i = j })
  if (i < 0) return c.lists[0].n === 1 ? false : undefined
  const after = c.lists[i + 1]
  if (after && after.n !== c.lists[i].n + 1) return undefined // a list between i and the next may be the one current at AT
  if (c.lists[i].devices.includes(device)) return true
  const prev = c.lists[i - 1]
  if (!prev || prev.n !== c.lists[i].n - 1) return c.lists[i].n === 1 ? false : undefined
  return prev.devices.includes(device) && at <= c.lists[i].created_at + GRACE
}

// ---- §5 names --------------------------------------------------------------------------------

/** §5: the key alias of K for ACCOUNT (people conversations): sha256("belfry:key:" + K + ":" + account). */
export const alias = (K: string, account: string) => bytesToHex(sha256(new TextEncoder().encode(`belfry:key:${K}:${account}`)))
/** §16.1: the lists hash of a group key: sha256 of the sorted lines "account:list id", joined by "\n". */
export const listsHash = (lists: { account: string; id: string }[]) => bytesToHex(sha256(new TextEncoder().encode(lists.map((l) => `${l.account}:${l.id}`).sort().join('\n'))))
/** A people conversation's id: its accounts, sorted, once each. */
export const peopleId = (accounts: Iterable<string>) => [...new Set(accounts)].sort().join(',')
/** The accounts of a rumor's people conversation: its p tags and its author. */
export const peopleOf = (r: { pubkey: string; tags: string[][] }) => peopleId([r.pubkey, ...tagValues(r, 'p').filter(isKey)])

// ---- §5 conversation keys --------------------------------------------------------------------

/** A new conversation key: secret and public half (x-only hex). */
export function newKey(secret: Uint8Array = randomBytes(32)): { k: string; K: string } {
  return { k: bytesToHex(secret), K: getPublicKey(secret) }
}

/** §5 the key rumor of a people conversation, made by the device MAKER for PEOPLE (account → its list id; an account without a list: no list tag). */
export function keyRumor(maker: string, k: string, people: { account: string; list?: string }[], at = now0()): Rumor {
  const K = getPublicKey(hexToBytes(k))
  return withId({ kind: KEY, pubkey: maker, created_at: at, content: k,
    tags: [['K', K], ...people.map((p) => ['p', p.account]), ...people.filter((p) => p.list).map((p) => ['list', p.account, p.list!])] })
}

export type Verdict = 'ok' | 'wait' | 'no'

/** §5 + §16.2 + §17 + §20: may RECEIVER (an account) take this people key rumor? DEVICES: what an
 *  account's devices are (a chain, SERVICE, or undefined: no list, so wait). */
export function acceptKeyRumor(r: Rumor, receiver: string, devices: (account: string) => Devices, now = now0()): Verdict {
  if (r.kind !== KEY || !isKey(r.content) || r.created_at > now + FUTURE) return 'no'
  const K = oneTag(r, 'K')
  let pub: string
  try { pub = getPublicKey(hexToBytes(r.content)) } catch { return 'no' }
  if (!K || K !== pub) return 'no'
  const people = tagValues(r, 'p')
  if (!people.includes(receiver) || people.some((p) => !isKey(p))) return 'no'
  let wait = false
  for (const a of people) {
    const c = devices(a)
    if (c === SERVICE) { if (r.pubkey === a) return 'ok'; continue } // the belfry or the groups key: its own device
    const named = r.tags.find((t) => t[0] === 'list' && t[1] === a)?.[2]
    if (!named) continue
    if (c === undefined) { wait = true; continue }
    const l = listById(c, named)
    if (!l) { wait = true; continue } // the named list isn't loaded yet
    if (l.devices.includes(r.pubkey)) return 'ok' // a self account's key is on its own list (§20)
  }
  return wait ? 'wait' : 'no'
}

/** §5 people envelopes: for each person, kind 8571 by a one-time key, ["p", account], ["d", alias],
 *  content {device: nip44(one-time → device, seal by MAKER of the key rumor)}. Split at MAX_ENVELOPE. */
export function peopleEnvelopes(maker: Uint8Array, r: Rumor, people: { account: string; devices: string[] }[], f?: Fixed): NostrEvent[] {
  const K = oneTag(r, 'K')!
  const now = f?.at ?? now0()
  const out: NostrEvent[] = []
  let n = 0
  for (const p of people) {
    const seals = new Map(p.devices.map((d) => [d, JSON.stringify(signWith(maker, { kind: SEAL, created_at: f?.sealAt ?? now, tags: [], content: nip44.v2.encrypt(rumorJson(r), conv(maker, d), nonceOf(f, n++)) }, f?.aux))]))
    const onces: Uint8Array[] = []
    const onceOf = (i: number) => (onces[i] ??= f?.oneTime ?? randomBytes(32))
    for (const [i, content] of split(p.devices, (i, d) => nip44.v2.encrypt(seals.get(d)!, conv(onceOf(i), d), nonceOf(f, n++))).entries())
      out.push(signWith(onceOf(i), { kind: KEY, created_at: now, tags: [['p', p.account], ['d', alias(K, p.account)]], content: JSON.stringify(content) }, f?.aux))
  }
  return out
}

/** DEVICES' entries in chunks whose JSON stays under MAX_ENVELOPE bytes; ENTRY(chunk, device) encrypts
 *  one entry for the chunk it lands in (each entry is encrypted once, or twice when it opens a new chunk). */
function split(devices: string[], entry: (chunk: number, device: string) => string): Record<string, string>[] {
  const out: Record<string, string>[] = [{}]
  let size = 2
  for (const d of devices) {
    let v = entry(out.length - 1, d)
    const add = d.length + v.length + 6
    if (size + add > MAX_ENVELOPE && Object.keys(out[out.length - 1]).length) { out.push({}); size = 2; v = entry(out.length - 1, d) }
    out[out.length - 1][d] = v
    size += d.length + v.length + 6
  }
  return out
}

/** My entry of a people envelope: (seal, key rumor with its id), or null when there is none for this device. Throws on a bad entry. */
export function openPeopleEnvelope(device: Uint8Array, env: unknown): { seal: NostrEvent; rumor: Rumor } | null {
  if (!validEvent(env) || env.kind !== KEY) throw new Error('not an envelope')
  const me = getPublicKey(device)
  let map: Record<string, unknown>
  try { map = JSON.parse(env.content) } catch { throw new Error('not an envelope') }
  const entry = map?.[me]
  if (typeof entry !== 'string') return null
  let seal: NostrEvent
  try { seal = JSON.parse(nip44.v2.decrypt(entry, conv(device, env.pubkey))) } catch { throw new Error('the entry does not open') }
  if (!validEvent(seal) || seal.kind !== SEAL) throw new Error('the seal is not signed')
  let rumor: Rumor
  try { rumor = withId(JSON.parse(nip44.v2.decrypt(seal.content, conv(device, seal.pubkey)))) } catch { throw new Error('the seal does not open') }
  if (rumor.pubkey !== seal.pubkey) throw new Error('the key rumor is not the seal signer’s')
  return { seal, rumor }
}

/** §16.1 group envelopes, unsigned (the maker's account signs them through the signer): tags h,
 *  d = K, maker, lists; content {device: nip44(maker device → device, {"k","K","h"})}, split. */
export function groupEnvelopes(maker: Uint8Array, k: string, group: string, devices: string[], lists: string, f?: Fixed): { kind: number; created_at: number; tags: string[][]; content: string }[] {
  const K = getPublicKey(hexToBytes(k))
  // §18.4: every entry names the whole set of devices that have one
  const body = JSON.stringify({ k, K, h: group, set: deviceSet(devices) })
  let n = 0
  const tags = [['h', group], ['d', K], ['maker', getPublicKey(maker)], ['lists', lists]]
  return split(devices, (_, d) => nip44.v2.encrypt(body, conv(maker, d), nonceOf(f, n++))).map((c) => ({ kind: KEY, created_at: f?.at ?? now0(), tags, content: JSON.stringify(c) }))
}

/** §18.4: the hash of a set of device keys (sorted, joined by "\n"). */
export const deviceSet = (devices: Iterable<string>) => bytesToHex(sha256(new TextEncoder().encode([...new Set(devices)].sort().join('\n'))))
/** The device keys that have an entry, over every envelope event of one key (a group's d). */
export function entryKeys(envs: unknown[]): string[] {
  const out = new Set<string>()
  for (const e of envs) { if (!validEvent(e) || e.kind !== KEY) continue; try { const m = JSON.parse(e.content); if (m && typeof m === 'object') for (const k of Object.keys(m)) if (isKey(k)) out.add(k) } catch { /* not an envelope */ } }
  return [...out].sort()
}

export interface GroupKey { k: string; K: string; group: string; maker: string; account: string; at: number; /** §18.4 the hash of the devices with an entry */ set: string }
/** My entry of a group envelope found in GROUP: the key, or null when there is none for this device. Throws on a bad one.
 *  §17.15: the entry, opened with the maker device's key, is what authenticates; the account's signature proves nothing. */
export function openGroupEnvelope(device: Uint8Array, env: unknown, group: string): GroupKey | null {
  if (!validEvent(env) || env.kind !== KEY) throw new Error('not an envelope')
  const h = oneTag(env, 'h'), d = oneTag(env, 'd'), maker = oneTag(env, 'maker')
  if (h !== group || !isKey(d) || !isKey(maker)) throw new Error('not this group’s envelope')
  let map: Record<string, unknown>
  try { map = JSON.parse(env.content) } catch { throw new Error('not an envelope') }
  const entry = map?.[getPublicKey(device)]
  if (typeof entry !== 'string') return null
  let x: { k?: unknown; K?: unknown; h?: unknown; set?: unknown }
  try { x = JSON.parse(nip44.v2.decrypt(entry, conv(device, maker))) } catch { throw new Error('the entry does not open') }
  if (!isKey(x.k) || x.K !== d || x.h !== group || !isKey(x.set) || getPublicKey(hexToBytes(x.k)) !== d) throw new Error('the entry is not this key')
  return { k: x.k, K: d, group, maker, account: env.pubkey, at: env.created_at, set: x.set }
}
/** §16.2: the maker was a device of the envelope's account at its date. */
export function acceptGroupKey(key: GroupKey, devices: Devices, now = now0()): Verdict {
  if (key.at > now + FUTURE) return 'no'
  if (devices === SERVICE) return key.maker === key.account ? 'ok' : 'no'
  if (devices === undefined) return 'wait'
  const v = listedAt(devices, key.maker, key.at)
  return v === undefined ? 'wait' : v ? 'ok' : 'no'
}

// ---- §6 messages -----------------------------------------------------------------------------

const sealFor = (device: Uint8Array, r: Rumor, K: string, f?: Fixed, i = 0) => {
  if (utf8(rumorJson(r)) > MAX_RUMOR) throw new Error('the message is too long to encrypt')
  return signWith(device, { kind: SEAL, created_at: f?.sealAt ?? f?.at ?? now0(), tags: [], content: nip44.v2.encrypt(rumorJson(r), conv(device, K), nonceOf(f, i)) }, f?.aux)
}

/** §6 people: one 8572 per person (the sender's own copy silent), each under its own one-time key. */
export function peopleMessage(device: Uint8Array, rumor: Rumor, K: string, people: string[], me: string, f?: Fixed): NostrEvent[] {
  const r = withId(rumor)
  const seal = JSON.stringify(sealFor(device, r, K, f, 0))
  return people.map((a, i) => {
    const once = f?.oneTime ?? randomBytes(32)
    return signWith(once, { kind: MSG, created_at: f?.at ?? now0(), tags: [['p', a], ['d', alias(K, a)], ...(a === me ? [SILENT] : [])], content: nip44.v2.encrypt(seal, conv(once, K), nonceOf(f, 100 + i)) }, f?.aux)
  })
}

/** §6 group: one 8572 by the sender's account (unsigned: the signer signs it), tags h and d = K (and p per mention). */
export function groupMessage(device: Uint8Array, rumor: Rumor, K: string, group: string, f?: Fixed, mentions: string[] = []) {
  const r = withId(rumor)
  // §17.14: the people it mentions, outside, so the belfry can push to them
  return { kind: MSG, created_at: f?.at ?? now0(), tags: [['h', group], ['d', K], ...[...new Set(mentions)].filter(isKey).map((p) => ['p', p])], content: JSON.stringify(sealFor(device, r, K, f, 0)) }
}

/** The key a message names: its d (people: an alias, group: K). */
export const named = (e: { tags: string[][] }) => oneTag(e, 'd')

function openSeal(k: string, sealJson: string): { seal: NostrEvent; rumor: Rumor } {
  let seal: NostrEvent
  try { seal = JSON.parse(sealJson) } catch { throw new Error('the seal is not an event') }
  if (!validEvent(seal) || seal.kind !== SEAL) throw new Error('the seal is not signed')
  let rumor: Rumor
  try { rumor = withId(JSON.parse(nip44.v2.decrypt(seal.content, conv(hexToBytes(k), seal.pubkey)))) } catch { throw new Error('the seal does not open with this key') }
  return { seal, rumor }
}
/** (seal, rumor) of a people 8572 with the conversation secret k. Throws when it doesn't open. */
export function openPeopleMessage(k: string, msg: unknown): { seal: NostrEvent; rumor: Rumor } {
  if (!validEvent(msg) || msg.kind !== MSG) throw new Error('not a message')
  let sealJson: string
  try { sealJson = nip44.v2.decrypt(msg.content, conv(hexToBytes(k), msg.pubkey)) } catch { throw new Error('the message does not open with this key') }
  return openSeal(k, sealJson)
}
/** (seal, rumor) of a group 8572 with k. */
export function openGroupMessage(k: string, msg: unknown): { seal: NostrEvent; rumor: Rumor } {
  if (!validEvent(msg) || msg.kind !== MSG) throw new Error('not a message')
  return openSeal(k, msg.content)
}

/** §6 + §18.2 accept, people: the seal's signer signs for rumor.pubkey at the OUTER event's date
 *  (the relay vouches for it), the rumor is not newer than the outer + 5 minutes, and the rumor's
 *  people are the key's. */
export function acceptPeopleMessage(outer: NostrEvent, seal: NostrEvent, rumor: Rumor, keyPeople: string, chain: Devices): Verdict {
  if (peopleOf(rumor) !== keyPeople) return 'no'
  return signer(outer, seal, rumor, chain)
}
/** §6 accept, group: the outer event is the rumor's author, the rumor's h is the key's group, the signer signs for the author. */
export function acceptGroupMessage(outer: NostrEvent, seal: NostrEvent, rumor: Rumor, group: string, chain: Devices): Verdict {
  if (outer.pubkey !== rumor.pubkey || oneTag(rumor, 'h') !== group || oneTag(outer, 'h') !== group) return 'no'
  return signer(outer, seal, rumor, chain)
}
function signer(outer: NostrEvent, seal: NostrEvent, rumor: Rumor, chain: Devices): Verdict {
  if (rumor.created_at > outer.created_at + SKEW) return 'no'
  if (chain === SERVICE) return seal.pubkey === rumor.pubkey ? 'ok' : 'no'
  if (chain === undefined) return 'wait' // no list: kept and judged again, never the account key (§20)
  // the account's own key is one of its devices only when a list says so (self): no list can be "behind" for it
  if (seal.pubkey === rumor.pubkey && !chain.lists.some((l) => l.devices.includes(seal.pubkey))) return 'no'
  const v = signsAt(chain, seal.pubkey, outer.created_at)
  return v === undefined ? 'wait' : v ? 'ok' : 'no'
}

// ---- §5 picking the key to send with ---------------------------------------------------------

export interface HeldKey {
  K: string; k: string; conv: string
  /** §17.8: made by a device of my own account */ mine: boolean
  /** made by the belfry key or the groups key (e-mail, buttons): never sent with (§18.7) */ service?: boolean
  /** people: the lists it was made for (peopleLists); group: the hash of its entry set (deviceSet) */ lists: string
  group?: boolean
  at: number
}
/** The lists a key is made for, as one string (people: "account:listid" lines sorted). */
export const peopleLists = (lists: { account: string; id: string }[]) => lists.map((l) => `${l.account}:${l.id}`).sort().join('\n')
/** §5 + §17.8 + §18.7: the key to send with in CONV: the newest one younger than 30 days and not
 *  future-dated, made for exactly LISTS (people: made by my own account's devices for these lists;
 *  group: any member's key whose authenticated entry set is exactly the device set, LISTS =
 *  deviceSet of it). Never a key the belfry or the groups key made. Undefined: make one. */
export const pickKey = (held: HeldKey[], conv: string, lists: string, now = now0()) =>
  held.filter((h) => h.conv === conv && h.lists === lists && !h.service && (h.mine || h.group) && now - h.at < KEY_AGE && h.at <= now + FUTURE)
    .sort((a, b) => b.at - a.at || (a.K < b.K ? -1 : 1))[0]

// ---- §7 a new device -------------------------------------------------------------------------

/** A key in a pack: a people key rumor as received, a group key, or a version-1 letterbox secret (§16.3). */
export type PackKey = { kind: 'people'; rumor: Rumor } | { kind: 'group'; k: string; K: string; group: string } | { kind: 'v1'; secret: string; lb: string }
const packRumor = (sharer: string, account: string, p: PackKey, at: number): unknown =>
  p.kind === 'people' ? { kind: KEY, pubkey: p.rumor.pubkey, created_at: p.rumor.created_at, tags: p.rumor.tags, content: p.rumor.content }
  : p.kind === 'group' ? { kind: KEY, pubkey: sharer, created_at: at, tags: [['K', p.K], ['h', p.group]], content: p.k }
  : { kind: KEY, pubkey: sharer, created_at: at, tags: [['v1', p.lb], ['p', account]], content: p.secret }

/** §7.5 packs: 8573 by a one-time key, ["p", account], ["d", new device]; a seal by SHARER holding a
 *  JSON list of key rumors, at most MAX_PACK bytes each. */
export function packs(sharer: Uint8Array, account: string, to: string, keys: PackKey[], f?: Fixed): NostrEvent[] {
  const at = f?.at ?? now0()
  const me = getPublicKey(sharer)
  const groups: unknown[][] = [[]]
  for (const k of keys) {
    const r = packRumor(me, account, k, at)
    const last = groups[groups.length - 1]
    if (last.length && utf8(JSON.stringify([...last, r])) > MAX_PACK) groups.push([r]); else last.push(r)
  }
  return groups.filter((g, i) => g.length || i === 0).map((g, i) => {
    const seal = signWith(sharer, { kind: SEAL, created_at: f?.sealAt ?? at, tags: [], content: nip44.v2.encrypt(JSON.stringify(g), conv(sharer, to), nonceOf(f, 2 * i)) }, f?.aux)
    const once = f?.oneTime ?? randomBytes(32)
    return signWith(once, { kind: PACK, created_at: at, tags: [['p', account], ['d', to]], content: nip44.v2.encrypt(JSON.stringify(seal), conv(once, to), nonceOf(f, 2 * i + 1)) }, f?.aux)
  })
}

/** §16.3 + §17.6: the keys of a pack to this DEVICE of ACCOUNT, when its seal is signed by another device on
 *  the account's current list that was listed before this one. Keys that don't check out are left out; none when the pack doesn't. */
export function openPack(device: Uint8Array, account: string, pack: unknown, chain: Chain | undefined): { from: string; keys: PackKey[] } | null {
  if (!validEvent(pack) || pack.kind !== PACK) return null
  const me = getPublicKey(device)
  if (oneTag(pack, 'd') !== me || oneTag(pack, 'p') !== account) return null
  let seal: NostrEvent, list: unknown
  try {
    seal = JSON.parse(nip44.v2.decrypt(pack.content, conv(device, pack.pubkey)))
    if (!validEvent(seal) || seal.kind !== SEAL) return null
    list = JSON.parse(nip44.v2.decrypt(seal.content, conv(device, seal.pubkey)))
  } catch { return null }
  // §16.3 + §17.6: sealed by another device on my current list, listed before me (a reader never shares)
  // §18.3: when the lists needed to decide "listed before" are not all loaded (gaps), refused
  if (!chain || chain.gaps || seal.pubkey === me || !current(chain).devices.includes(seal.pubkey) || !listedBefore(chain, seal.pubkey, me) || !Array.isArray(list)) return null
  const keys: PackKey[] = []
  for (const x of list) {
    let r: Rumor
    try { r = withId(x) } catch { continue }
    if (r.kind !== KEY || !isKey(r.content)) continue
    let pub: string
    try { pub = getPublicKey(hexToBytes(r.content)) } catch { continue }
    const v1 = oneTag(r, 'v1'), h = oneTag(r, 'h'), K = oneTag(r, 'K')
    if (v1 !== undefined) { if (v1 === pub && oneTag(r, 'p') === account) keys.push({ kind: 'v1', secret: r.content, lb: v1 }); continue }
    if (K !== pub) continue
    if (h !== undefined) { if (h) keys.push({ kind: 'group', k: r.content, K, group: h }); continue }
    if (tagValues(r, 'p').includes(account)) keys.push({ kind: 'people', rumor: r })
  }
  return { from: seal.pubkey, keys }
}

/** §7.3 the ask, signed by the device. */
export const ask = (device: Uint8Array, account: string, at = now0(), aux?: Uint8Array) =>
  signWith(device, { kind: ASK, created_at: at, tags: [['p', account]], content: '' }, aux)
/** §7.7 the answer, signed by the answering device: to an ask (e) or to the card of a device that never asked. */
export const answer = (device: Uint8Array, account: string, to: { ask: string } | { device: string }, what: 'shared' | 'removed', at = now0(), aux?: Uint8Array) =>
  signWith(device, { kind: ANSWER, created_at: at, tags: [['p', account], 'ask' in to ? ['e', to.ask] : ['device', to.device], ['answer', what]], content: '' }, aux)

/** An ask or answer of ACCOUNT signed by a device on its current list: who asked / what was answered for whom. */
export function readAsk(e: unknown, account: string, chain: Chain | undefined): { device: string; id: string; at: number } | null {
  if (!validEvent(e) || e.kind !== ASK || oneTag(e, 'p') !== account || !chain || !current(chain).devices.includes(e.pubkey)) return null
  return { device: e.pubkey, id: e.id, at: e.created_at }
}
/** ABOUT: the device the answer is for (an e answer: the asking device; runtime resolves it). §17.6: only
 *  a device listed before it counts. */
export function readAnswer(e: unknown, account: string, chain: Chain | undefined, about?: string): { by: string; ask?: string; device?: string; answer: 'shared' | 'removed'; at: number } | null {
  // §20: only from a device on the current list (a since-removed device doesn't count)
  if (!validEvent(e) || e.kind !== ANSWER || oneTag(e, 'p') !== account || !chain || !current(chain).devices.includes(e.pubkey)) return null
  const dev = oneTag(e, 'device') ?? about
  if (!dev || chain.gaps || !listedBefore(chain, e.pubkey, dev)) return null // an e answer needs its asking device
  const a = oneTag(e, 'answer'), ask = oneTag(e, 'e'), device = oneTag(e, 'device')
  if ((a !== 'shared' && a !== 'removed') || (!ask === !device) || (ask && !isKey(ask)) || (device && !isKey(device))) return null
  return { by: e.pubkey, ...(ask ? { ask } : {}), ...(device ? { device } : {}), answer: a, at: e.created_at }
}

// ---- §17.2, §17.12, §13 small rules --------------------------------------------------------

/** §17.12: KEYS (K → the conversation it was first declared for) with a declaration added: the first stays. */
export function declare(book: Map<string, string>, K: string, conv: string): boolean {
  if (book.has(K)) return false
  book.set(K, conv)
  return true
}
/** §17.2: an unencrypted event in a conversation whose first key is dated FIRST_KEY_AT: shown
 *  marked "Not encrypted" and never acting when dated after it (before: old history). */
export const clearAfterKey = (firstKeyAt: number | undefined, at: number) => firstKeyAt !== undefined && at > firstKeyAt
/** §4 + §17.2: a people conversation of N devices: encrypted (≤ 100), or over: not encrypted when it
 *  never was, held when it was. A private group: refused over 1000. */
export const peopleLimit = (n: number, everEncrypted: boolean): 'encrypt' | 'clear' | 'held' => (n <= MAX_PEOPLE_DEVICES ? 'encrypt' : everEncrypted ? 'held' : 'clear')
export const groupLimit = (n: number): 'encrypt' | 'refuse' => (n <= MAX_GROUP_DEVICES ? 'encrypt' : 'refuse')

// ---- §16.9: what stays in clear in a private group (the relay or the belfry reads it) ----------
export function clearInGroup(e: { kind: number; pubkey: string; tags: string[][] }, groupsKey: string): boolean {
  if ([9000, 9001, 9002, 9005, 9007, 9008, 9021, 9022, 39000, 39001, 39002, 39003].includes(e.kind)) return true
  if (e.kind === 1985) return (!!groupsKey && e.pubkey === groupsKey) || e.tags.some((t) => t[0] === 'L' && t[1] === 'belfry/vouch')
  if (e.kind === 30078) { const d = e.tags.find((t) => t[0] === 'd')?.[1] ?? ''; return d.startsWith('belfry:inbox:') || d.startsWith('seen:') }
  return false
}

// ---- §17.12: never block the search ------------------------------------------------------------
/** The first envelope of a group key that opens AND is accepted. A decoy (same d = K, an unlisted maker)
 *  never hides a real one after it: an opened envelope that is refused is skipped, and the search goes on.
 *  'wait': none accepted yet, one waits on a list (its account is returned to be fetched). */
export function pickGroupEnvelope<T>(envs: T[], open: (e: T) => GroupKey | null, accept: (k: GroupKey) => 'ok' | 'wait' | 'no'):
  { key: GroupKey } | { wait: string } | null {
  let waiting = ''
  for (const env of envs) {
    let k: GroupKey | null = null
    try { k = open(env) } catch { k = null } // a bad envelope: skipped
    if (!k) continue
    const v = accept(k)
    if (v === 'ok') return { key: k }
    if (v === 'wait' && !waiting) waiting = k.account
  }
  return waiting ? { wait: waiting } : null
}
