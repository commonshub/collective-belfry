// Sealed conversations, the core (collective-belfry docs/sealed.md, version 1): roster chains,
// sealing and opening, handing keys over, the link proof, shared history, "ready". Pure
// functions over events and keys: no storage, no network, no browser APIs (the vectors run it in
// Node: tools/vectors/check.mjs against vectors/sealed.json). sealed.ts / dm.ts do the rest.
//
// Never log or throw a secret, a letterbox key or a rumor's text: errors here say what failed only.
import { schnorr } from '@noble/curves/secp256k1'
import { sha256 } from '@noble/hashes/sha256'
import { hmac } from '@noble/hashes/hmac'
import { bytesToHex, hexToBytes, randomBytes } from '@noble/hashes/utils'
import { getEventHash, getPublicKey, nip44, verifyEvent, type NostrEvent } from 'nostr-tools'

export const ROSTER = 8560, KEYS = 8561, REQUEST = 8562, HISTORY = 8564, SEAL = 13, WRAP = 1059, MAIL = 1301, ANCHOR = 30078
export const ANCHOR_D = 'belfry:sealed'
export const MAX_DEVICES = 20
export const MAX_RUMOR = 40_000 // bytes of a sealed rumor's JSON (§4, §8)
export const MAX_HISTORY_RUMORS = 200
const BACKDATE = 2 * 86400 // as dm.ts: seal and wrap dated up to two days back

const HEX64 = /^[0-9a-f]{64}$/
export const isKey = (v: unknown): v is string => typeof v === 'string' && HEX64.test(v)
/** The public key of a secret (hex). */
export const getPub = (secret: Uint8Array) => getPublicKey(secret)
const N = /^[1-9][0-9]{0,8}$/

const values = (e: { tags?: unknown }, name: string): string[] =>
  Array.isArray(e.tags) ? (e.tags as unknown[]).filter((t): t is string[] => Array.isArray(t) && t.length > 1 && t[0] === name && typeof t[1] === 'string').map((t) => t[1]) : []
/** The value of tag NAME when the event has exactly one. */
const one = (e: { tags?: unknown }, name: string) => { const v = values(e, name); return v.length === 1 ? v[0] : undefined }

/** A signed event whose id is its hash and whose signature verifies. */
export function validEvent(e: unknown): e is NostrEvent {
  if (!e || typeof e !== 'object') return false
  const x = e as NostrEvent
  if (!isKey(x.pubkey) || !isKey(x.id) || typeof x.sig !== 'string' || !Number.isInteger(x.created_at) || !Number.isInteger(x.kind) || typeof x.content !== 'string' || !Array.isArray(x.tags)) return false
  try { return getEventHash(x) === x.id && verifyEvent({ ...x }) } catch { return false }
}

// ---- the roster chain (§3) ---------------------------------------------------------------

export interface Roster { id: string; signer: string; n: number; prev?: string; lb: string; devices: string[]; ready: boolean; created_at: number }

export interface Chain {
  account: string
  rosters: Roster[] // root first
  /** device → the first roster that contains it ("Added from": its signer, nothing for a root's own) */
  added: Map<string, Roster>
  /** device no longer in the head → the roster that dropped it (the latest, if it came back and left again) */
  dropped: Map<string, Roster>
}
export const headOf = (c: Chain) => c.rosters[c.rosters.length - 1]
/** Ready ever (§6): once a roster of the chain had the tag, later ones don't unset it. */
export const isReady = (c: Chain) => c.rosters.some((r) => r.ready)
export const allDevices = (c: Chain) => new Set(c.added.keys())
/** §4: DEVICE may have sealed a rumor dated AT. */
export function hasDevice(c: Chain, device: string, at: number): boolean {
  if (headOf(c).devices.includes(device)) return true
  const gone = c.dropped.get(device)
  return !!gone && at <= gone.created_at
}

/** EVENT as a roster of ACCOUNT, or null: kind 8560, signed, one p (the account), one n, one lb,
 *  at most one prev, 1 to 20 devices, none listed twice. Says nothing of its place in a chain. */
export function parseRoster(e: unknown, account: string): Roster | null {
  if (!validEvent(e) || e.kind !== ROSTER) return null
  const p = values(e, 'p')
  if (p.length !== 1 || p[0] !== account) return null
  const n = one(e, 'n'), lb = one(e, 'lb'), prevs = values(e, 'prev')
  if (!n || !N.test(n) || !isKey(lb) || prevs.length > 1 || (prevs[0] !== undefined && !isKey(prevs[0]))) return null
  const devices = values(e, 'device')
  if (devices.length < 1 || devices.length > MAX_DEVICES || new Set(devices).size !== devices.length || !devices.every(isKey)) return null
  return { id: e.id, signer: e.pubkey, n: Number(n), prev: prevs[0], lb, devices, ready: e.tags.some((t) => t[0] === 'ready'), created_at: e.created_at }
}

/** The root an anchor names (§3.2), or undefined when it isn't a signed anchor (of ACCOUNT). */
export function anchorRoot(a: unknown, account?: string): string | undefined {
  if (!validEvent(a) || a.kind !== ANCHOR || (account !== undefined && a.pubkey !== account)) return undefined
  const d = values(a, 'd')
  if (d.length !== 1 || d[0] !== ANCHOR_D) return undefined
  const root = one(a, 'root')
  return isKey(root) ? root : undefined
}

/** The account's current anchor among EVENTS: the newest, the lower id on a tie. */
export function newestAnchor(events: NostrEvent[], account: string): NostrEvent | undefined {
  return events.filter((e) => anchorRoot(e, account)).sort((a, b) => b.created_at - a.created_at || (a.id < b.id ? -1 : 1))[0]
}

function follows(prev: Roster, earlierLbs: Set<string>, r: Roster): boolean {
  if (r.n !== prev.n + 1 || !prev.devices.includes(r.signer)) return false
  if (prev.devices.some((d) => !r.devices.includes(d))) return !earlierLbs.has(r.lb) // a device left: a letterbox the chain never had
  return r.lb === prev.lb
}

/** The head an anchor names (§3.2): its one head tag, else its root. */
export function anchorHead(a: unknown): string | undefined {
  const root = anchorRoot(a)
  if (!root) return undefined
  const h = one(a as NostrEvent, 'head')
  return isKey(h) ? h : root
}

/** The valid chain of the anchor's account (§3.3), or null (no valid anchor, or no valid root).
 *  PINNED: the head this device last accepted for the account; an anchor naming one of its
 *  ancestors is not followed back (the pinned head is kept). */
export function validChain(anchor: unknown, rosterEvents: unknown[], pinned?: string): Chain | null {
  const rootId = anchorRoot(anchor)
  if (!rootId) return null
  return chainFromRoot((anchor as NostrEvent).pubkey, rootId, rosterEvents, anchorHead(anchor), pinned)
}

/** The valid chain from ROOT_ID: the anchored path root → HEAD_ID (when it is a valid descent
 *  of the root, else the root alone), then valid steps after its end with the lower id winning
 *  for now. A roster off that path is ignored whatever its id. PINNED as in validChain. Without
 *  HEAD_ID (a root pinned before a fresh start, §5.5): the root, then lower-id-wins. */
export function chainFromRoot(account: string, rootId: string, rosterEvents: unknown[], headId?: string, pinned?: string): Chain | null {
  const parsed = new Map<string, Roster>()
  for (const e of rosterEvents ?? []) { const r = parseRoster(e, account); if (r) parsed.set(r.id, r) }
  const root = parsed.get(rootId)
  if (!root || root.n !== 1 || root.prev !== undefined || !root.devices.includes(root.signer)) return null
  // a valid descent root → id, checked forward (each step follows the one before)
  const pathTo = (id: string | undefined): Roster[] | null => {
    if (!id) return null
    const back: Roster[] = []
    const seen = new Set<string>()
    for (let r = parsed.get(id); r; r = r.prev ? parsed.get(r.prev) : undefined) {
      if (seen.has(r.id)) return null
      seen.add(r.id); back.push(r)
      if (r.id === rootId) break
    }
    if (back[back.length - 1]?.id !== rootId) return null
    const path = back.reverse()
    const lbs = new Set([root.lb])
    for (let i = 1; i < path.length; i++) { if (!follows(path[i - 1], lbs, path[i])) return null; lbs.add(path[i].lb) }
    return path
  }
  let path = pathTo(headId) ?? [root]
  // no going back: when the anchored head is an ancestor of the pinned one, the pinned path stays
  const pin = pathTo(pinned)
  if (pin && pin.some((r) => r.id === path[path.length - 1].id) && pin.length > path.length) path = pin
  const children = new Map<string, Roster[]>()
  for (const r of parsed.values()) if (r.prev) (children.get(r.prev) ?? children.set(r.prev, []).get(r.prev)!).push(r)
  const rosters = [root]
  const lbs = new Set([root.lb])
  const added = new Map(root.devices.map((d) => [d, root] as const))
  const dropped = new Map<string, Roster>()
  const step = (head: Roster, next: Roster) => {
    for (const d of head.devices) if (!next.devices.includes(d)) dropped.set(d, next)
    for (const d of next.devices) { if (!added.has(d)) added.set(d, next); dropped.delete(d) }
    rosters.push(next)
    lbs.add(next.lb)
  }
  for (const r of path.slice(1)) step(rosters[rosters.length - 1], r)
  for (;;) { // after the anchored path: rosters published but not anchored yet
    const head = rosters[rosters.length - 1]
    const valid = (children.get(head.id) ?? []).filter((r) => follows(head, lbs, r))
    if (!valid.length) break
    step(head, valid.reduce((a, b) => (b.id.toLowerCase() < a.id.toLowerCase() ? b : a))) // for now: the lower id; the next anchor settles it
  }
  return { account, rosters, added, dropped }
}

/** §3.4 notices, one roster step at a time: who appeared / disappeared, dated by the roster. */
export function rosterSteps(c: Chain): { at: number; added: string[]; removed: string[] }[] {
  return c.rosters.slice(1).map((r, i) => {
    const before = c.rosters[i]
    return { at: r.created_at, added: r.devices.filter((d) => !before.devices.includes(d)), removed: before.devices.filter((d) => !r.devices.includes(d)) }
  })
}

// ---- rumors and wraps (§4) -------------------------------------------------------------------

export interface Rumor { pubkey: string; created_at: number; kind: number; tags: string[][]; content: string; id: string }

/** RUMOR with its id computed (an id or sig it came with is ignored); throws when it isn't an event. */
export function withId(r: unknown): Rumor {
  if (!r || typeof r !== 'object') throw new Error('the rumor is not an event')
  const x = r as Record<string, unknown>
  if (!isKey(x.pubkey)) throw new Error('the rumor has no author')
  if (!Number.isInteger(x.created_at) || !Number.isInteger(x.kind) || typeof x.content !== 'string' || !Array.isArray(x.tags)
    || !(x.tags as unknown[]).every((t) => Array.isArray(t) && t.every((v) => typeof v === 'string'))) throw new Error('the rumor is not an event')
  const kept: Record<string, unknown> = {}
  for (const [k, v] of Object.entries(x)) if (k !== 'id' && k !== 'sig') kept[k] = v
  return { ...kept, id: getEventHash(kept as unknown as NostrEvent) } as Rumor
}

/** A rumor's fields in the order sealed.md §4 serialises them: id, pubkey, created_at, kind, tags, content. */
const ordered = (r: Rumor) => ({ id: r.id, pubkey: r.pubkey, created_at: r.created_at, kind: r.kind, tags: r.tags, content: r.content })
/** The rumor as it goes into a seal: compact JSON, keys in that order, UTF-8 and '/' not escaped. */
export const rumorJson = (r: Rumor) => JSON.stringify(ordered(r))
const utf8 = (s: string) => new TextEncoder().encode(s).length
export const rumorBytes = (r: Rumor) => utf8(rumorJson(r))
/** §4: a sealed rumor's JSON is at most 40 000 bytes. */
export const fits = (r: Rumor) => rumorBytes(r) <= MAX_RUMOR

/** What a wrap otherwise draws at random (the vectors fix them). */
export interface Fixed { sealAt?: number; wrapAt?: number; oneTime?: Uint8Array; sealNonce?: Uint8Array; wrapNonce?: Uint8Array; aux?: Uint8Array }

/** Signed with SECRET: pubkey and id set, keys in the order kind, created_at, tags, content, pubkey, id, sig. */
export function signWith(secret: Uint8Array, t: { kind: number; created_at: number; tags: string[][]; content: string }, aux?: Uint8Array): NostrEvent {
  const e = { kind: t.kind, created_at: t.created_at, tags: t.tags, content: t.content, pubkey: getPublicKey(secret) } as NostrEvent
  e.id = getEventHash(e)
  e.sig = bytesToHex(schnorr.sign(e.id, secret, aux ?? randomBytes(32)))
  return e
}

const backdated = (now: number) => now - Math.floor(Math.random() * BACKDATE)
const conv = (secret: Uint8Array, pub: string) => nip44.v2.utils.getConversationKey(secret, pub)

/** RUMOR sealed by SIGNER and gift-wrapped, both encrypted to the key TO. */
function wrapTo(signer: Uint8Array, rumor: Rumor, to: string, tags: string[][], f: Fixed = {}, now = Math.floor(Date.now() / 1000)): NostrEvent {
  const r = withId(rumor)
  if (!fits(r)) throw new Error('the message is too long to seal')
  const seal = signWith(signer, { kind: SEAL, created_at: f.sealAt ?? backdated(now), tags: [], content: nip44.v2.encrypt(rumorJson(r), conv(signer, to), f.sealNonce) }, f.aux)
  const once = f.oneTime ?? randomBytes(32)
  return signWith(once, { kind: WRAP, created_at: f.wrapAt ?? backdated(now), tags, content: nip44.v2.encrypt(JSON.stringify(seal), conv(once, to), f.wrapNonce) }, f.aux)
}

/** RUMOR sealed by SIGNER and gift-wrapped to the key TO with TAGS (§7: a mail out to the
 *  belfry's key, ["p", belfry], no lb). */
export const sealToKey = (signer: Uint8Array, rumor: Rumor, to: string, tags: string[][], f?: Fixed) => wrapTo(signer, rumor, to, tags, f)

/** §4: RUMOR sealed by a device key (the belfry's for its mail) to the letterbox LB of ACCOUNT. */
export function sealToLetterbox(signer: Uint8Array, rumor: Rumor, account: string, lb: string, f?: Fixed): NostrEvent {
  if (!isKey(account) || !isKey(lb)) throw new Error('account and letterbox are 64 hex characters')
  return wrapTo(signer, rumor, lb, [['p', account], ['lb', lb]], f)
}

/** (seal, rumor as sent) of a gift wrap to SECRET's key; throws when it doesn't open. */
export function openWrap(secret: Uint8Array, wrap: unknown): { seal: NostrEvent; rumor: unknown } {
  if (!validEvent(wrap) || wrap.kind !== WRAP) throw new Error('not a valid gift wrap')
  let seal: NostrEvent
  try { seal = JSON.parse(nip44.v2.decrypt(wrap.content, conv(secret, wrap.pubkey))) } catch { throw new Error('the wrap does not open with this key') }
  if (!validEvent(seal) || seal.kind !== SEAL) throw new Error('the seal is not signed')
  let rumor: unknown
  try { rumor = JSON.parse(nip44.v2.decrypt(seal.content, conv(secret, seal.pubkey))) } catch { throw new Error('the seal does not open with this key') }
  if (!rumor || typeof rumor !== 'object' || Array.isArray(rumor)) throw new Error('the rumor is not an event')
  return { seal, rumor }
}

/** The letterbox a sealed wrap names (its one lb tag), or undefined for an old-style wrap. */
export const wrapLb = (wrap: NostrEvent) => { const lb = one(wrap, 'lb'); return isKey(lb) ? lb : undefined }

/** (seal, rumor with its computed id) of a sealed wrap opened with the letterbox secret its lb names. */
export function openSealed(lbSecret: Uint8Array, wrap: NostrEvent): { seal: NostrEvent; rumor: Rumor } {
  const lb = wrapLb(wrap)
  if (!lb) throw new Error('not a sealed wrap')
  if (lb !== getPublicKey(lbSecret)) throw new Error('another letterbox')
  const { seal, rumor } = openWrap(lbSecret, wrap)
  return { seal, rumor: withId(rumor) }
}

/** §4: whether an opened sealed rumor is its author's. AUTHOR: the valid chain of rumor.pubkey
 *  (null: none or not loaded yet: keep the wrap, retry). BELFRY: its own seal on its own mail. */
export function accept(author: Chain | null, seal: NostrEvent, rumor: Rumor, belfry = ''): boolean {
  if (belfry && seal.pubkey === belfry) return rumor.pubkey === belfry && rumor.kind === MAIL
  if (!author || author.account !== rumor.pubkey || !Number.isInteger(rumor.created_at)) return false
  return hasDevice(author, seal.pubkey, rumor.created_at)
}

// ---- handing keys over (§5.5) ------------------------------------------------------------

export interface LbKey { lb: string; secret: string }

export function keysRumor(giver: string, account: string, keys: LbKey[], at: number): Rumor {
  return withId({ kind: KEYS, pubkey: giver, created_at: at, tags: [['p', account]], content: JSON.stringify({ v: 1, account, keys }) })
}

/** KEYS split so that each 8561 rumor stays within the 40 000-byte ceiling (§5.5). */
export function keyChunks(giver: string, account: string, keys: LbKey[], at: number): LbKey[][] {
  const out: LbKey[][] = []
  let cur: LbKey[] = []
  for (const k of keys) {
    if (cur.length && !fits(keysRumor(giver, account, [...cur, k], at))) { out.push(cur); cur = [] }
    cur.push(k)
  }
  if (cur.length) out.push(cur)
  return out
}

/** The giver's key chain gift-wrapped to a device: ["p", <receiving device>], no lb. */
export function handKeys(giver: Uint8Array, account: string, keys: LbKey[], toDevice: string, f?: Fixed & { rumorAt?: number }): NostrEvent {
  const r = keysRumor(getPublicKey(giver), account, keys, f?.rumorAt ?? Math.floor(Date.now() / 1000))
  return wrapTo(giver, r, toDevice, [['p', toDevice]], f)
}

/** (seal, rumor) of a hand-over to this device; throws when it isn't one. */
export function openKeys(deviceSecret: Uint8Array, wrap: NostrEvent): { seal: NostrEvent; rumor: Rumor } {
  const p = values(wrap, 'p')
  if (p.length !== 1 || p[0] !== getPublicKey(deviceSecret)) throw new Error('not for this device')
  const { seal, rumor } = openWrap(deviceSecret, wrap)
  const r = withId(rumor)
  if (r.kind !== KEYS || r.pubkey !== seal.pubkey) throw new Error('not a hand-over by its giver')
  return { seal, rumor: r }
}

/** The letterbox keys of a hand-over this device takes ([] for none): the giver must be a device
 *  of any roster of CHAINS (mine, and after a fresh start the one pinned before); each secret its lb's. */
export function acceptKeys(account: string, chains: Chain[], seal: NostrEvent, rumor: Rumor): LbKey[] {
  const giver = seal.pubkey
  if (!chains.some((c) => c.account === account && allDevices(c).has(giver))) return []
  if (rumor.kind !== KEYS || rumor.pubkey !== giver) return []
  const p = values(rumor, 'p')
  if (p.length !== 1 || p[0] !== account) return []
  let body: { v?: unknown; account?: unknown; keys?: unknown }
  try { body = JSON.parse(rumor.content) } catch { return [] }
  if (!body || typeof body !== 'object' || body.v !== 1 || body.account !== account) return []
  const out: LbKey[] = []
  for (const k of Array.isArray(body.keys) ? body.keys : []) {
    if (!k || typeof k !== 'object' || !isKey(k.lb) || !isKey(k.secret)) continue
    let good = false
    try { good = getPublicKey(hexToBytes(k.secret)) === k.lb } catch { good = false }
    if (good && !out.some((o) => o.lb === k.lb)) out.push({ lb: k.lb, secret: k.secret })
  }
  return out
}

// ---- requests and the link proof (§5.2) -----------------------------------------------------

/** hex HMAC-SHA256 keyed with the link secret's 32 bytes over the device's 32 public-key bytes. */
export function linkProof(linkSecret: string, device: string): string {
  if (!isKey(linkSecret) || !isKey(device)) throw new Error('64 hex characters each')
  return bytesToHex(hmac(sha256, hexToBytes(linkSecret), hexToBytes(device)))
}

/** Whether a device request proves it came from the link with LINK_SECRET. */
export function linkOk(linkSecret: string, request: unknown): boolean {
  if (!validEvent(request) || request.kind !== REQUEST) return false
  const given = one(request, 'link')
  if (!given) return false
  const want = linkProof(linkSecret, request.pubkey)
  let diff = given.length ^ want.length
  for (let i = 0; i < Math.min(given.length, want.length); i++) diff |= given.charCodeAt(i) ^ want.charCodeAt(i)
  return diff === 0
}

export function deviceRequest(device: Uint8Array, account: string, linkSecret?: string, now = Math.floor(Date.now() / 1000)): NostrEvent {
  const tags = [['p', account], ...(linkSecret ? [['link', linkProof(linkSecret, getPublicKey(device))]] : [])]
  return signWith(device, { kind: REQUEST, created_at: now, tags, content: '' })
}

/** The anchor's tags (§3.2): root, and head = the newest roster (absent: the root). */
export const anchorTags = (root: string, head?: string) => [['d', ANCHOR_D], ['root', root], ...(head && head !== root ? [['head', head]] : [['head', root]])]

/** A roster signed by DEVICE (§3.1). */
export function makeRoster(device: Uint8Array, a: { account: string; n: number; prev?: string; lb: string; devices: string[]; ready?: boolean }, now = Math.floor(Date.now() / 1000)): NostrEvent {
  if (a.devices.length < 1 || a.devices.length > MAX_DEVICES || new Set(a.devices).size !== a.devices.length) throw new Error('1 to 20 different devices')
  const tags = [['p', a.account], ['n', String(a.n)], ...(a.prev ? [['prev', a.prev]] : []), ['lb', a.lb], ...a.devices.map((d) => ['device', d]), ...(a.ready ? [['ready']] : [])]
  return signWith(device, { kind: ROSTER, created_at: now, tags, content: '' })
}

// ---- shared history (§8) -------------------------------------------------------------------

export function historyRumor(sharer: string, person: string, rumors: Rumor[], at: number): Rumor {
  return withId({ kind: HISTORY, pubkey: sharer, created_at: at, tags: [['p', person]], content: JSON.stringify({ v: 1, rumors: rumors.map(ordered) }) })
}

/** RUMORS (oldest first) as kind 8564 rumors, each filled until one more would pass 200 rumors or
 *  its own JSON 40 000 bytes; a rumor too large for an 8564 alone is left out. */
export function packHistory(sharer: string, person: string, rumors: Rumor[], at: number): { packs: Rumor[]; leftOut: string[] } {
  const packs: Rumor[] = [], leftOut: string[] = []
  let cur: Rumor[] = []
  const close = () => { if (cur.length) packs.push(historyRumor(sharer, person, cur, at)); cur = [] }
  for (const r of rumors) {
    if (!fits(historyRumor(sharer, person, [r], at))) { leftOut.push(r.id); continue }
    if (cur.length >= MAX_HISTORY_RUMORS || !fits(historyRumor(sharer, person, [...cur, r], at))) close()
    cur.push(r)
  }
  close()
  return { packs, leftOut }
}

/** The rumors of an opened 8564 the receiver adds, one by one: those whose conversation both the
 *  sharer and the RECEIVER are in (author or a p tag), with computed ids; HAVE: ids already here. */
export function historyRumors(h: Rumor, receiver: string, have: Set<string> = new Set()): Rumor[] {
  if (h.kind !== HISTORY) return []
  const hp = values(h, 'p')
  if (hp.length !== 1 || hp[0] !== receiver) return [] // the 8564's p must be the receiver
  let body: { v?: unknown; rumors?: unknown }
  try { body = JSON.parse(h.content) } catch { return [] }
  if (!body || body.v !== 1 || !Array.isArray(body.rumors)) return []
  const out: Rumor[] = []
  for (const x of body.rumors) {
    let r: Rumor
    try { r = withId(x) } catch { continue }
    if (r.kind !== 14 && r.kind !== 7 && r.kind !== 5) continue // never an e-mail (§8)
    const inIt = (pk: string) => r.pubkey === pk || values(r, 'p').includes(pk)
    if (!inIt(h.pubkey) || !inIt(receiver)) continue
    if (!have.has(r.id) && !out.some((o) => o.id === r.id)) out.push(r)
  }
  return out
}

// ---- ready (§6) ------------------------------------------------------------------------------

export interface SessionRow { client_pubkey?: string | null; chat?: boolean; last_activity?: string | null; created_at?: string | null; device?: string; application_name?: string }
const DAY = 86400
/** Counted: a chat session whose last activity (its creation when never active) is within the
 *  last 30 days, strictly (the lead, 20 Nov). */
export function counted(s: SessionRow, now: number): boolean {
  const t = s.last_activity || s.created_at
  return s.chat === true && !!t && now - Date.parse(t) / 1000 < 30 * DAY
}
/** A roster may carry ready when every counted session's client key is one of its devices. */
export const mayCarryReady = (sessions: SessionRow[], devices: string[], now = Math.floor(Date.now() / 1000)) =>
  sessions.filter((s) => counted(s, now)).every((s) => !!s.client_pubkey && devices.includes(s.client_pubkey))
