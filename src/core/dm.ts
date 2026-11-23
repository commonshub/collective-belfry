import { finalizeEvent, generateSecretKey, getEventHash, getPublicKey, nip44, verifyEvent, type NostrEvent } from 'nostr-tools'
import { sha256 } from '@noble/hashes/sha256'
import { bytesToHex, hexToBytes } from '@noble/hashes/utils'
import { BELFRY, TEST_RELAYS } from '../config'
import { kvDel, kvGet, kvPrefix, kvSet } from './db'
import { FILE_KIND, MAX_FILE, MAX_FILE_GUEST, UNREADABLE, encryptFile, fileRefOf, fileTags, openFile, tooLarge, type FileRef } from './dmfiles'
import { isGuest } from './invite'
import { addressOf, buildMail, parseMail, type Part } from './mime'
import { displayName, profiles, realName } from './model'
import { addEvents, all, outboxItems, putOutbox, subscribe, touch } from './store'
import { markRead as markReadState, ownMark, readReady, readUpTo } from './readstate'
import { currentSigner, enqueueSigned, flush, isLocalOnly, me, onOutbox, queryOnce, retryNow, sendStep } from './sync'
import { NoHead, TOO_LONG, acceptedSealed, openReportHere, firstSeenOf, isSealedConv, onDecidable, onHistory, onKeysArrived, openDm, sealMailOut, sealWraps, sendMode, setPartners, setRumorSource } from './sealedrt'
import { belfryNostrKey, onAccessInfo } from './access'
import type { Rumor } from './sealedcore'

// Direct messages: NIP-17 (kind 14 in NIP-59 gift wraps), old NIP-04 (kind 4, read only) and
// e-mail as Nostr Mail (kind 1301 in gift wraps, nogringo/nostr-mail). The belfry is the e-mail
// bridge (_smtp@<domain>): mail to <username>@<domain> arrives as a 1301 from the belfry key
// with `mail-from`; an answer is a 1301 with `mail-from` (my address) and `rcpt-to`, wrapped to
// the bridge, which reports back with a kind 7679. Every message is also wrapped to myself, so
// what I sent shows on my other devices. All of it lives on the community relay (my 10050).
//
// Decrypted messages are kept on this device only (IndexedDB, wiped at sign-out).

export type Conv = string // "n:<pubkey>[,<pubkey>…]" (Nostr) or "m:<address>" (e-mail)

export interface Dm {
  rid: string // the rumor's id (the same in every copy)
  conv: Conv
  from: string // pubkey, or the sender's address for e-mail
  mine: boolean
  at: number
  kind: 14 | 4 | 1301 | 15
  text: string
  /** the rumor's tags (kind 14): a shared history (§8) sends the rumor again as it was */
  tags?: string[][]
  /** it came, or went, sealed (docs/sealed.md): only these are shared with a new device (§8) */
  sealed?: boolean
  /** my e-mail's rumor tags, while it may still be sealed again (§9) */
  mailTags?: string[][]
  /** a kind 15: its tags (what opens its file, dmfiles.ts); FILE undefined when they are unusable */
  fileTags?: string[][]
  file?: FileRef
  /** pictures sent together and their caption share one group (one bubble) */
  group?: string
  subject?: string
  fromName?: string
  emailId?: string
  messageId?: string
  references?: string
  verified?: boolean // e-mail: the bridge saw DKIM or SPF aligned with From
  attachments?: { name: string; type: string; size: number }[]
  raw?: string // e-mail MIME when it fitted in the wrap (attachments are read from it)
  large?: { url: string; key: string; nonce: string }
  /** Saved here, its gift wraps in the outbox (sync.ts) until the relay takes them. */
  pending?: boolean
  /** The relay refused it for good: why (Retry puts it back in the outbox). */
  failed?: string
}

export interface Report { status: 'delivered' | 'failed' | 'delayed' | 'queued'; details: string }

const items = new Map<string, Dm>() // by rid
const seen = new Set<string>() // event ids already read (gift wraps, kind 4)
// sealed wraps already read (or given up), kept apart: an older version's tab, which hands them to
// the signer and fails, writes only 'dm-seen' and can't hide them from this one
const seenSealed = new Set<string>()
const isSealedWrap = (e: { kind: number; tags: string[][] }) => e.kind === 1059 && e.tags.some((t) => t[0] === 'lb')
const wasSeen = (e: { id: string; kind: number; tags: string[][] }) => (isSealedWrap(e) ? seenSealed.has(e.id) : seen.has(e.id))
const markSeen = (e: { id: string; kind: number; tags: string[][] }) => (isSealedWrap(e) ? seenSealed : seen).add(e.id)
// reactions to direct messages (NIP-17: a kind 7 rumor in its own gift wraps, `e` = the message's
// rumor id; taking one back is a kind 5 rumor naming it). By reaction rumor id.
export interface DmReaction { rid: string; target: string; from: string; emoji: string; at: number; pending?: boolean }
const dmReacts = new Map<string, DmReaction>()
const reports = new Map<string, Report>() // by email-id
let myAddress = ''
let bridgeKey: string | undefined
let loaded = false

const DEMO_BRIDGE = bytesToHex(sha256('osv-demo-bridge')) // demo mode only: the "belfry" that mails

export function setMyAddress(a?: string) { if (a) myAddress = a.toLowerCase() }
/** My address here (name@domain): what my profile says now, so a new sign-in (profile not loaded
 *  yet when the app starts) and a username change show without a reload; else what was known
 *  when the app started. */
export const address = () => profiles().get(me())?.nip05?.toLowerCase() || myAddress

export async function bridge(): Promise<string | undefined> {
  if (isLocalOnly() || TEST_RELAYS) return getPublicKey(hexToBytes(DEMO_BRIDGE))
  if (bridgeKey) return bridgeKey
  bridgeKey = await kvGet<string>('dm-bridge')
  try {
    const r = await fetch(`https://${BELFRY.domain}/.well-known/nostr.json?name=_smtp`)
    const k = ((await r.json()) as { names?: Record<string, string> }).names?._smtp
    if (k && /^[0-9a-f]{64}$/.test(k)) { bridgeKey = k; void kvSet('dm-bridge', k) }
  } catch { /* offline: the stored one */ }
  return bridgeKey
}

let groupsKeyCache: string | undefined
export const DEMO_GROUPS = bytesToHex(sha256('osv-demo-groups')) // demo mode only: the key that posts e-mail cards

/** The key that posts e-mail cards in shared inboxes (`_groups@<domain>`): only its cards show as e-mail. */
export async function groupsKey(): Promise<string | undefined> {
  if (isLocalOnly()) return (groupsKeyCache = getPublicKey(hexToBytes(DEMO_GROUPS)))
  if (groupsKeyCache) return groupsKeyCache
  groupsKeyCache = await kvGet<string>('dm-groups-key')
  try {
    const r = await fetch(`https://${BELFRY.domain}/.well-known/nostr.json?name=_groups`)
    const k = ((await r.json()) as { names?: Record<string, string> }).names?._groups
    if (k && /^[0-9a-f]{64}$/.test(k)) { groupsKeyCache = k; void kvSet('dm-groups-key', k) }
  } catch { /* offline: the stored one */ }
  touch()
  return groupsKeyCache
}
export const knownGroupsKey = () => groupsKeyCache

const channelSends = new Set<string>() // email-ids sent as a shared inbox, still without a report

/** Answer an e-mail card as the shared inbox CHANNEL (only its admins can: the bridge checks).
 *  Resolves with the email-id; `report(id)` then follows its delivery. */
export async function sendAsChannel(m: { channel: string; /** the channel's address before the @: its plain alias when it has one (model.mailLocal), else its id */ local: string; channelName: string; to: string; subject: string; text: string; inReplyTo?: string; /** the thread's Message-IDs before In-Reply-To, oldest first */ references?: string; cardId: string }): Promise<string> {
  const b = await bridge()
  if (!b) throw new Error('This belfry does not send e-mail yet.')
  // envelope (mail-from) and From header alike: the bridge checks they match (mail.outgoing)
  const from = `${m.local || m.channel}@${BELFRY.domain}`
  const { raw, messageId } = buildMail({ fromName: channelSignature(m.channelName), from, to: m.to, subject: m.subject, text: m.text, inReplyTo: m.inReplyTo, references: m.references, domain: BELFRY.domain })
  const emailId = crypto.randomUUID()
  const rumor = rumorOf(1301, [['email-id', emailId], ['mail-from', from], ['rcpt-to', m.to], ['subject', m.subject], ['e', m.cardId]], raw)
  lastRaw = raw
  if (isLocalOnly()) { reports.set(emailId, { status: 'queued', details: 'Sending…' }); channelSends.add(emailId); touch(); demoSent({ ...m, messageId }, emailId); return emailId }
  await deliver(rumor, [b], false, 'm:' + m.to.toLowerCase()) // throws before anything shows as sending
  reports.set(emailId, { status: 'queued', details: 'Sending…' })
  channelSends.add(emailId)
  touch()
  for (const wait of [2000, 5000, 10000, 30000]) setTimeout(() => void checkReports(), wait)
  return emailId
}

/** How an answer from a shared inbox is signed: "<my name> for <channel>". */
export const channelSignature = (channelName: string) => (realName(me()) ? `${realName(me())} for ${channelName}` : channelName)

/** Demo mode and tests: the RFC 2822 text of the last answer sent as a channel. */
let lastRaw = ''
export const lastChannelMail = () => lastRaw

function demoSent(m: { channel: string; to: string; subject: string; text: string; cardId: string; messageId: string }, emailId: string) {
  const sk = hexToBytes(DEMO_GROUPS)
  setTimeout(() => {
    addEvents([finalizeEvent({ kind: 9, created_at: Math.floor(Date.now() / 1000), content: m.text,
      tags: [['h', m.channel], ['subject', m.subject], ['mail-to', m.to], ['sent-by', me()], ['message-id', m.messageId], ['e', m.cardId, '', 'reply'], ['client', 'belfry-mail-out']] }, sk)])
    reports.set(emailId, { status: 'delivered', details: '' })
    channelSends.delete(emailId)
    touch()
  }, 800)
}

// ---- reading -------------------------------------------------------------------

async function load() {
  if (loaded) return
  loaded = true
  for (const d of (await kvGet<Dm[]>('dm-items')) ?? []) items.set(d.rid, d)
  for (const id of (await kvGet<string[]>('dm-seen')) ?? []) seen.add(id)
  for (const id of (await kvGet<string[]>('dm-seen-sealed')) ?? []) seenSealed.add(id)
  for (const r of (await kvGet<DmReaction[]>('dm-reactions')) ?? []) dmReacts.set(r.rid, r)
  for (const [k, v] of Object.entries((await kvGet<Record<string, Report>>('dm-reports')) ?? {})) reports.set(k, v)
  for (const h of await kvPrefix<Held>(KV_HELD).catch(() => [] as Held[])) held.set(h.rumor.id, h)
  for (const r of await kvPrefix<Rumor>(KV_OUT).catch(() => [] as Rumor[])) outRumors.set(r.id, r)
  await readReady // read marks: readstate.ts (shared across my devices; seeded from the old 'dm-read')
  // mail stored before threads: one conversation per address → one per subject
  const waiting = new Set([...outboxItems().map((o) => o.dm).filter(Boolean), ...held.keys()])
  for (const d of items.values()) if (d.pending && !waiting.has(d.rid)) d.pending = false
  // sent by an older version, which kept no copy: stuck at "Sending…" with nothing left to send
  for (const d of items.values()) {
    const r = d.emailId ? reports.get(d.emailId) : undefined
    if (d.mine && d.kind === 1301 && r?.status === 'queued' && r.details === 'Sending…' && !waiting.has(d.rid))
      reports.set(d.emailId!, { status: 'failed', details: 'it never left this device (an older version of the app). Send it again.' })
  }
  for (const d of items.values()) {
    if (!isNewMail(d.conv)) continue
    const conv = mailThreadOf(mailAddress(d.conv), d.subject)
    if (ownMark('d', d.conv)) markReadState('d', conv, ownMark('d', d.conv))
    d.conv = conv
  }
}

let saveTimer: ReturnType<typeof setTimeout> | null = null
function save() {
  touch()
  saveTimer ??= setTimeout(() => {
    saveTimer = null
    void kvSet('dm-items', [...items.values()]) // unsent ones too: their wraps wait in the outbox
    void kvSet('dm-seen', [...seen])
    void kvSet('dm-seen-sealed', [...seenSealed])
    void kvSet('dm-reports', Object.fromEntries(reports))
    void kvSet('dm-reactions', [...dmReacts.values()])
  }, 300)
}

const tag = (e: { tags: string[][] }, n: string) => e.tags.find((t) => t[0] === n)?.[1]
const tagsOf = (e: { tags: string[][] }, n: string) => e.tags.filter((t) => t[0] === n).map((t) => t[1])

// ---- e-mail threads ---------------------------------------------------------------
// Every e-mail thread is a conversation of its own, named after its subject (like a thread in a
// channel): "m:<address>|<subject key>". "m:<address>" alone is a new e-mail to that address,
// which becomes a thread once sent. A mail arriving joins the thread its In-Reply-To or
// References name, else the one with the same subject (Re:, Fwd: … aside), else starts one.
const PREFIX = /^\s*((re|fwd?|aw|sv|tr|wg|antw|rif|r)\s*(\[\d+\])?\s*:\s*)+/i
export const baseSubject = (s?: string) => (s ?? '').replace(PREFIX, '').trim()
const subjectKey = (s?: string) => encodeURIComponent(baseSubject(s).toLowerCase().replace(/\s+/g, ' ')) || '-'
export const mailAddress = (conv: Conv) => conv.slice(2).split('|')[0]
export const mailThreadOf = (address: string, subject?: string): Conv => `m:${address.toLowerCase()}|${subjectKey(subject)}`
export const isNewMail = (conv: Conv) => conv.startsWith('m:') && !conv.includes('|')
export const msgIds = (v?: string) => (v ?? '').match(/<[^<>\s]+>/g) ?? []

/** The thread an e-mail belongs to: the one its In-Reply-To/References point at, else its subject's. */
function mailConv(address: string, subject: string | undefined, refs: string[]): Conv {
  for (const id of refs.reverse()) {
    for (const d of items.values()) if (d.messageId === id && d.conv.startsWith('m:') && mailAddress(d.conv) === address) return d.conv
  }
  return mailThreadOf(address, subject)
}

function convOf(rumor: NostrEvent, bridgePk?: string): Conv {
  const self = me()
  if (rumor.kind === 1301) {
    const from = tag(rumor, 'mail-from')
    if (rumor.pubkey === self && tag(rumor, 'rcpt-to')) return 'm:' + tag(rumor, 'rcpt-to')!.toLowerCase()
    if (from && rumor.pubkey === bridgePk) return 'm:' + from.toLowerCase()
  }
  const people = new Set([rumor.pubkey, ...tagsOf(rumor, 'p')])
  people.delete(self)
  return 'n:' + ([...people].sort().join(',') || self)
}

async function fromRumor(rumor: NostrEvent): Promise<Dm | null> {
  const b = await bridge()
  const base = { rid: rumor.id || getEventHash(rumor), conv: convOf(rumor, b), mine: rumor.pubkey === me(), at: rumor.created_at }
  if (rumor.kind === 14) return { ...base, from: rumor.pubkey, kind: 14, text: rumor.content, tags: rumor.tags, ...(groupOf(rumor) ? { group: groupOf(rumor) } : {}) }
  if (rumor.kind === FILE_KIND) return { ...base, from: rumor.pubkey, kind: 15, text: '', tags: rumor.tags, fileTags: rumor.tags, file: fileRefOf(rumor) ?? undefined, ...(groupOf(rumor) ? { group: groupOf(rumor) } : {}) }
  if (rumor.kind !== 1301) return null
  const bridged = rumor.pubkey === b && !!tag(rumor, 'mail-from')
  const d: Dm = {
    ...base, kind: 1301, text: '',
    from: bridged ? tag(rumor, 'mail-from')!.toLowerCase() : rumor.pubkey,
    emailId: tag(rumor, 'email-id'),
    subject: tag(rumor, 'subject'),
    verified: bridged ? tag(rumor, 'belfry-auth') === 'pass' : true,
  }
  let raw = rumor.content
  if (!raw && tag(rumor, 'x')) {
    d.large = { url: tag(rumor, 'url') ?? `${BELFRY.blossom}/${tag(rumor, 'x')}`, key: tag(rumor, 'decryption-key') ?? '', nonce: tag(rumor, 'decryption-nonce') ?? '' }
    try { raw = await fetchLarge(d.large) } catch { d.text = '(This e-mail is stored on the file server and could not be fetched.)' }
  } else d.raw = raw
  if (raw) {
    const m = parseMail(raw)
    d.subject = m.subject || d.subject
    d.text = m.text
    d.fromName = addressOf(m.from).name || undefined
    d.messageId = m.headers.get('message-id')
    d.references = m.headers.get('references')
    d.attachments = m.attachments.map((a) => ({ name: a.name, type: a.type, size: a.data.length }))
    if (d.conv.startsWith('m:')) d.conv = mailConv(mailAddress(d.conv), d.subject, [...msgIds(d.references), ...msgIds(m.headers.get('in-reply-to'))])
  } else if (d.conv.startsWith('m:')) d.conv = mailThreadOf(mailAddress(d.conv), d.subject)
  return d
}

const b64 = (s: string) => Uint8Array.from(atob(s), (c) => c.charCodeAt(0))
export async function fetchLarge(l: { url: string; key: string; nonce: string }): Promise<string> {
  const blob = new Uint8Array(await (await fetch(l.url)).arrayBuffer())
  const key = await crypto.subtle.importKey('raw', b64(l.key), 'AES-GCM', false, ['decrypt'])
  return new TextDecoder().decode(await crypto.subtle.decrypt({ name: 'AES-GCM', iv: b64(l.nonce) }, key, blob))
}

/** The attachments of an e-mail, with their bytes (read from the message again). */
export async function attachmentsOf(d: Dm): Promise<Part[]> {
  const raw = d.raw ?? (d.large ? await fetchLarge(d.large) : '')
  return raw ? parseMail(raw).attachments : []
}

async function unwrap(wrap: NostrEvent): Promise<NostrEvent | null> {
  const s = currentSigner()
  if (!s) return null
  const seal = JSON.parse(await s.decrypt(wrap.pubkey, wrap.content)) as NostrEvent
  if (seal.kind !== 13 || !verifyEvent(seal)) return null
  const rumor = JSON.parse(await s.decrypt(seal.pubkey, seal.content)) as NostrEvent
  return rumor.pubkey === seal.pubkey ? rumor : null // no one writes in someone else's name
}

let busy = false
async function process() {
  if (busy) return
  busy = true
  try {
    await load()
    const self = me()
    const todo = all(self).filter((e) => !e.pending && !wasSeen(e) && (
      (e.kind === 1059 && e.tags.some((t) => t[0] === 'p' && t[1] === self)) || e.kind === 4))
      .sort((a, b) => b.created_at - a.created_at)
    for (const e of todo) {
      try {
        let d: Dm | null = null
        if (e.kind === 1059 && e.tags.some((t) => t[0] === 'lb')) {
          // sealed (docs/sealed.md §4): opened with my letterbox key, never by the signer; no key
          // or chain yet: keep the wrap and try again later (not marked seen)
          // the belfry's own mail: its key from belfry.json (demo and tests: the demo bridge)
          const o = await openDm(e, isLocalOnly() || TEST_RELAYS ? await bridge() : belfryNostrKey() || undefined)
          if (!o.ok) { if (!o.retry) markSeen(e); continue }
          const rumor = o.rumor as unknown as NostrEvent
          if (rumor.kind === 7 || rumor.kind === 5) reactionRumor(rumor)
          else { d = await fromRumor(rumor); if (d) { d.sealed = true; acceptedSealed(d.conv) } }
        } else if (e.kind === 1059) {
          const rumor = await unwrap(e)
          if (rumor) {
            rumor.id = getEventHash(rumor) // computed by the opener on the old path too (§6)
            // in a sealed conversation, after this device first saw it sealed: an old-style
            // reaction or take-back is ignored, an old-style message never replaces one held
            const c = convOf(rumor, await bridge())
            const after = isSealedConv(c) && rumor.created_at * 1000 >= (firstSeenOf(c) ?? Infinity)
            if (rumor.kind === 7 || rumor.kind === 5) { if (!after) reactionRumor(rumor) }
            else { d = await fromRumor(rumor); if (d && items.has(d.rid) && after) d = null }
          }
        } else if (e.pubkey === self || e.tags.some((t) => t[0] === 'p' && t[1] === self)) {
          const peer = e.pubkey === self ? tag(e, 'p') ?? self : e.pubkey
          const s = currentSigner()
          const text = s?.decrypt04 ? await s.decrypt04(peer, e.content) : '(an old-style message this app cannot read)'
          d = { rid: e.id, conv: 'n:' + peer, from: e.pubkey, mine: e.pubkey === self, at: e.created_at, kind: 4, text }
        }
        if (d) { const old = items.get(d.rid); items.set(d.rid, { ...d, mine: d.mine || !!old?.mine, sealed: d.sealed || old?.sealed }) }
        markSeen(e)
      } catch (x) {
        if (String(x).includes('timeout')) break // the signer is away: next time
        markSeen(e) // not for me, or broken
      }
      save()
    }
  } finally {
    busy = false
  }
}

let started = false
export function startDms() {
  if (started) return
  started = true
  watchOutbox()
  // §9: a queued sealed wrap is sealed again to a new head from the rumor kept here until it is out
  setRumorSource((rid) => outRumors.get(rid))
  onOutbox((item) => { if (item.dm) setTimeout(() => outSent(item.dm!), 0) }, () => {})
  // letterbox keys arrived: wraps naming them that were marked seen are read again
  onKeysArrived((lbs) => {
    const set = new Set(lbs)
    let n = 0
    for (const e of all()) if (isSealedWrap(e) && seenSealed.has(e.id) && e.tags.some((t) => t[0] === 'lb' && set.has(t[1]))) { seenSealed.delete(e.id); n++ }
    if (n) { save(); void process() }
  })
  // a complete answer arrived: held messages are decided (§6)
  onDecidable(() => void sendHeld())
  onAccessInfo(() => void process()) // belfry.json's key arrived: the belfry's sealed mail opens
  setInterval(() => { if (held.size && navigator.onLine) void sendHeld(true) }, 15_000) // a silent relay: asked again
  // §8: a history shared with this device: its messages, marked (sealedrt keeps who shared them);
  // its reactions are not applied (not proven to be authentic)
  onHistory((rumors) => void (async () => {
    for (const r of rumors) {
      if (items.has(r.id) || r.kind !== 14) continue
      const d = await fromRumor(r as unknown as NostrEvent)
      if (d) items.set(d.rid, { ...d, sealed: true })
    }
    save(); touch()
  })(), (rid) => items.has(rid))
  let t: ReturnType<typeof setTimeout> | null = null
  subscribe(() => { t ??= setTimeout(() => { t = null; void process() }, 200) })
  void load().then(process)
}

// ---- the views' data -------------------------------------------------------------

export interface Thread { conv: Conv; last: Dm; unread: number; count: number }

let partnersSent = ''
function notePartners(list: Thread[]) {
  const ps = list.flatMap((t) => peers(t.conv))
  const key = ps.join()
  if (key !== partnersSent) { partnersSent = key; setPartners(ps) }
}

/** Notes to myself (docs/one-shape.md): the direct conversation whose only participant is me
 *  (kind 14 / 15, one p = my account). Pinned first in Messages, never unread. */
export const notesConv = (): Conv => 'n:' + me()
export const isNotes = (conv: Conv) => conv === notesConv()

export function threads(): Thread[] {
  const by = new Map<Conv, Dm[]>()
  for (const d of items.values()) (by.get(d.conv) ?? by.set(d.conv, []).get(d.conv)!).push(d)
  const out = [...by.entries()].map(([conv, list]) => {
    list.sort((a, b) => a.at - b.at)
    const last = list[list.length - 1]
    return { conv, last, count: list.length, unread: isNotes(conv) ? 0 : list.filter((d) => !d.mine && d.at > readUpTo('d', conv)).length }
  }).sort((a, b) => b.last.at - a.last.at)
  notePartners(out)
  return out
}

export const unreadTotal = () => threads().reduce((n, t) => n + (t.unread ? 1 : 0), 0)
export const thread = (conv: Conv) => [...items.values()].filter((d) => d.conv === conv).sort((a, b) => a.at - b.at)
export const report = (emailId?: string) => (emailId ? reports.get(emailId) : undefined)
export const isMail = (conv: Conv) => conv.startsWith('m:')
/** A mail thread's name: its subject (without Re:/Fwd:). */
export const threadName = (conv: Conv) => baseSubject(thread(conv).find((d) => d.subject)?.subject) || '(no subject)'
export const peers = (conv: Conv) => (conv.startsWith('n:') ? conv.slice(2).split(',') : [])

export function convName(conv: Conv): string {
  if (isMail(conv)) {
    const a = mailAddress(conv)
    const named = [...items.values()].find((d) => !d.mine && d.fromName && d.conv.startsWith('m:') && mailAddress(d.conv) === a)?.fromName
    return named ? `${named}` : a
  }
  return peers(conv).map((p) => displayName(p)).join(', ')
}

/** The conversation is on screen: read up to its newest message or e-mail (reactions are not
 *  items: they never move the mark). Shared with my other devices (readstate.ts). */
export function markRead(conv: Conv) {
  const last = thread(conv).at(-1)?.at ?? 0
  if (last) markReadState('d', conv, last)
}

// ---- sending ---------------------------------------------------------------------

const backdate = () => Math.floor(Date.now() / 1000) - Math.floor(Math.random() * 2 * 86400)

async function giftWrap(rumor: NostrEvent, to: string): Promise<NostrEvent> {
  const s = currentSigner()!
  const seal = await s.sign({ kind: 13, created_at: backdate(), tags: [], content: await s.encrypt(to, JSON.stringify(rumor)) })
  const one = generateSecretKey()
  return finalizeEvent({
    kind: 1059, created_at: backdate(), tags: [['p', to], ...(to === me() ? [['silent', '1']] : [])], // my own copy: silent (sealed.md §4)
    content: nip44.v2.encrypt(JSON.stringify(seal), nip44.v2.utils.getConversationKey(one, to)),
  }, one)
}

/** Wrap RUMOR for TO (and me) and put the wraps in the outbox: saved on this device, published
 *  when the relay answers, retried until it does (sync.ts). Never waits for the network. */
async function deliver(rumor: NostrEvent, to: string[], self = true, conv?: Conv, upload?: Blob) {
  // §6: sealed, or the old way only on knowledge; when this device can't tell (offline, before
  // the relay's answer), or a sealed conversation has no head yet, the message is held here,
  // shown as waiting, and decided at the first complete answer. CONV 'm:': my own chain decides.
  // A file (UPLOAD: its ciphertext) goes the same way: sealed by the device in a sealed
  // conversation, held with its bytes when undecided, never the old way into a sealed one.
  const mode = conv ? await sendMode(conv) : 'old'
  if (mode === 'undecided') return hold({ rumor: rumor as unknown as Rumor, to, self, conv: conv!, upload })
  dmStep.set(rumor.id, 'encrypting')
  try {
    let wraps: NostrEvent[]
    if (mode === 'sealed') {
      try { wraps = sealedWrapsOf(rumor as unknown as Rumor, to, self, conv!) } catch (e) { if (e instanceof Error && e.message === TOO_LONG) throw new Error('Not sent: ' + TOO_LONG); if (e instanceof NoHead) return hold({ rumor: rumor as unknown as Rumor, to, self, conv: conv!, who: e.message || undefined, upload }); throw e }
    } else wraps = await Promise.all([...new Set([...to, ...(self ? [me()] : [])])].map((p) => giftWrap(rumor, p)))
    if (isLocalOnly()) { addEvents(wraps.filter((w) => w.tags[0][1] === me())) }
    await enqueueWraps(wraps, rumor.id, to, upload)
  } finally {
    dmStep.delete(rumor.id)
  }
}
/** the wraps in the outbox, in the conversation's queue (its order kept, sync.ts); a file's upload
 *  is a step of its first wrap, the others follow it */
async function enqueueWraps(wraps: NostrEvent[], rid: string, to: string[], upload?: Blob) {
  const q = [...new Set([...to, me()])].sort().join(',')
  for (const [i, w] of wraps.entries()) await enqueueSigned('main', w, rid, q, i === 0 ? upload : undefined)
}
/** a message being signed and wrapped, before its wraps are in the outbox */
const dmStep = new Map<string, string>()

function sealedWrapsOf(r: Rumor, to: string[], self: boolean, conv: Conv): NostrEvent[] {
  const wraps = isMail(conv) ? [sealMailOut(r, to[0]), ...(self ? sealWraps(r, []) : [])] : sealWraps(r, to)
  const it = items.get(r.id); if (it) it.sealed = true
  keepOut(r)
  return wraps
}

// held: decided neither sealed nor old yet (§6), one kv entry each (this account's own database,
// like the messages), sent at the first complete answer
interface Held { rumor: Rumor; to: string[]; self: boolean; conv: Conv; failed?: string; /** whose new device isn't ready (a locked conversation with nobody to lock it to) */ who?: string; /** a file's ciphertext, uploaded once it goes */ upload?: Blob }
const KV_HELD = 'dm-held:', KV_OUT = 'dm-out:'
const held = new Map<string, Held>()
/** Held on this device until it can be decided (shown as waiting). */
export const isHeld = (rid: string) => held.has(rid)
function hold(h: Held) {
  held.set(h.rumor.id, h)
  void kvSet(KV_HELD + h.rumor.id, h).catch(() => {})
  const it = items.get(h.rumor.id); if (it) it.pending = true
  save()
}
let sending = false, sendAgain = false
/** REFRESH: ask the relay first (the periodic try); after a complete answer it is known already. */
async function sendHeld(refresh = false) {
  if (sending) { sendAgain = true; return }
  sending = true
  try {
    let fresh = refresh
    do {
      sendAgain = false
      for (const h of [...held.values()]) {
        if (h.failed) continue // failed for good: stays held (Cancel gives the text back)
        if ((await sendMode(h.conv, fresh)) === 'undecided') continue
        await withHeldLock(async () => {
          // another tab of this device may have sent or cancelled it meanwhile
          if (!(await kvGet<Held>(KV_HELD + h.rumor.id).catch(() => undefined))) { held.delete(h.rumor.id); return }
          const mode = await sendMode(h.conv, false) // decided again inside the lock
          if (mode === 'undecided') return
          let wraps: NostrEvent[]
          try {
            wraps = mode === 'sealed' ? sealedWrapsOf(h.rumor, h.to, h.self, h.conv)
              : await Promise.all([...new Set([...h.to, ...(h.self ? [me()] : [])])].map((p) => giftWrap(h.rumor as unknown as NostrEvent, p)))
          } catch (e) {
            if (e instanceof NoHead) { if (h.who !== (e.message || undefined)) { h.who = e.message || undefined; await kvSet(KV_HELD + h.rumor.id, h).catch(() => {}) } return }
            if (!(e instanceof Error) || e.message !== TOO_LONG) return // a passing failure (the signer away): tried again later
            h.failed = TOO_LONG // can never go: stays held, said why (never a technical reason), Cancel still there
            await kvSet(KV_HELD + h.rumor.id, h).catch(() => {})
            return
          }
          await enqueueWraps(wraps, h.rumor.id, h.to, h.upload) // queued first …
          held.delete(h.rumor.id); await kvDel(KV_HELD + h.rumor.id).catch(() => {}) // … then no longer held
          const r = dmReacts.get(h.rumor.id); if (r) r.pending = false
        })
        save()
      }
      fresh = false
    } while (sendAgain)
  } finally { sending = false }
  void flush()
}
const withHeldLock = <T,>(f: () => Promise<T>): Promise<T> =>
  (navigator.locks?.request ? navigator.locks.request('belfry-held-' + me(), f) as Promise<T> : f())

/** A held message's Cancel: it goes, and its text comes back (the composer takes it). */
export async function cancelHeld(rid: string): Promise<string | null> {
  // under the same lock as sending: either it is cancelled (text back) or it went already (null)
  return withHeldLock(async () => {
    const h = held.get(rid)
    if (!h || !(await kvGet<Held>(KV_HELD + rid).catch(() => undefined))) { held.delete(rid); save(); return null }
    held.delete(rid); await kvDel(KV_HELD + rid).catch(() => {})
    const it = items.get(rid)
    items.delete(rid); save()
    return it?.text ?? h.rumor.content
  })
}
/** Why a held message failed for good (it stays held until Cancel), or undefined. */
export const heldFailure = (rid: string) => held.get(rid)?.failed
/** Whose new device a held message waits for, if that is why (v98). */
export const heldFor = (rid: string) => held.get(rid)?.who
/** When it was held (ms; for "why" after a minute). */
export const heldSince = (rid: string) => (held.has(rid) ? held.get(rid)!.rumor.created_at * 1000 : 0)

// §9: the rumor of a sealed message, kept here (one kv entry each, this account's own database)
// until all of its wraps are out, so a queued wrap can be sealed again to a new head; never in the outbox
const outRumors = new Map<string, Rumor>()
function keepOut(r: Rumor) {
  outRumors.set(r.id, { id: r.id, pubkey: r.pubkey, created_at: r.created_at, kind: r.kind, tags: r.tags, content: r.content })
  void kvSet(KV_OUT + r.id, outRumors.get(r.id)).catch(() => {})
}
function outSent(rid: string) {
  if (!outRumors.has(rid) || outboxItems().some((o) => o.dm === rid)) return
  outRumors.delete(rid); void kvDel(KV_OUT + rid).catch(() => {})
}

/** Where a message stands in the outbox: waiting (and why, after a failed try); since when (ms)
 *  and the step it is at (the UI's "Still sending… · <step>"). */
export function queueState(d: Dm): { waiting: boolean; error?: string; since: number; step?: string } {
  const mine = outboxItems().filter((o) => o.dm === d.rid)
  const err = mine.find((o) => o.error)?.error
  const since = Math.min(d.at * 1000, ...mine.map((o) => o.queuedAt ?? o.created_at * 1000))
  const step = dmStep.get(d.rid) ?? mine.map((o) => sendStep(o.localId)).find(Boolean) ?? (err ? 'waiting to try again' : undefined)
  return { waiting: mine.length > 0, error: err, since, step }
}

/** Put a refused message back in the outbox. */
export async function retryDm(d: Dm) {
  const queued = outboxItems().filter((x) => x.dm === d.rid)
  if (!queued.length) return
  for (const o of queued) await putOutbox({ ...o, status: 'queued', error: undefined })
  const it = items.get(d.rid)
  if (it) { it.failed = undefined; it.pending = true }
  if (d.emailId) reports.set(d.emailId, { status: 'queued', details: 'Waiting for the connection…' })
  save()
  retryNow()
}

let watching = false
function watchOutbox() {
  if (watching) return
  watching = true
  onOutbox(
    (item) => {
      if (!item.dm) return
      const d = items.get(item.dm)
      if (!d) return
      // out once no wrap for someone else waits any more (my own copy may follow later)
      const left = outboxItems().filter((o) => o.dm === item.dm && o.tags.some((t) => t[0] === 'p' && t[1] !== me()))
      if (left.length) return
      d.pending = false
      d.failed = undefined
      if (d.emailId && d.kind === 1301) {
        reports.set(d.emailId, { status: 'queued', details: 'Sent: waiting for the delivery report…' })
        if (!isLocalOnly()) for (const wait of [3000, 10000, 30000, 90000]) setTimeout(() => void checkReports(), wait)
        else reports.set(d.emailId, { status: 'failed', details: 'Demo mode sends no e-mail.' })
      }
      save()
    },
    (item) => {
      if (!item.dm) return
      const d = items.get(item.dm)
      if (!d || !item.tags.some((t) => t[0] === 'p' && t[1] !== me())) return
      d.failed = item.error || 'The relay refused it.'
      if (d.emailId) reports.set(d.emailId, { status: 'failed', details: d.failed })
      save()
    },
  )
}

function reactionRumor(r: NostrEvent) {
  const id = r.id || getEventHash(r)
  if (r.kind === 7) {
    const target = r.tags.filter((t) => t[0] === 'e').at(-1)?.[1]
    const emoji = (r.content || '+').trim() === '+' ? '👍' : r.content.trim()
    if (target && emoji && emoji.length <= 16) dmReacts.set(id, { rid: id, target, from: r.pubkey, emoji, at: r.created_at })
  } else {
    // kind 5: only its author takes a reaction back
    for (const t of r.tags) if (t[0] === 'e' && dmReacts.get(t[1])?.from === r.pubkey) dmReacts.delete(t[1])
  }
}

/** The reactions to direct message RID, most given first (who → when). */
export function dmReactions(rid: string): { emoji: string; who: Map<string, number>; mine?: DmReaction }[] {
  const by = new Map<string, { emoji: string; who: Map<string, number>; mine?: DmReaction }>()
  for (const r of [...dmReacts.values()].filter((x) => x.target === rid).sort((a, b) => a.at - b.at)) {
    const c = by.get(r.emoji) ?? { emoji: r.emoji, who: new Map<string, number>() }
    c.who.set(r.from, r.at)
    if (r.from === me()) c.mine = r
    by.set(r.emoji, c)
  }
  return [...by.values()].sort((a, b) => b.who.size - a.who.size)
}

/** React to D with EMOJI, or take my reaction back (MINE): sealed to the same people. */
export async function reactDm(d: Dm, emoji: string, mine?: DmReaction) {
  await load()
  const to = peers(d.conv)
  if (mine) {
    const rumor = rumorOf(5, [['e', mine.rid], ['k', '7'], ...to.filter((p) => p !== me()).map((p) => ['p', p])], '')
    dmReacts.delete(mine.rid); save()
    await deliver(rumor, to, true, d.conv)
    return
  }
  const rumor = rumorOf(7, [['e', d.rid, '', d.from], ['p', d.from], ['k', String(d.kind)], ...to.filter((p) => p !== me() && p !== d.from).map((p) => ['p', p])], emoji)
  dmReacts.set(rumor.id, { rid: rumor.id, target: d.rid, from: me(), emoji, at: rumor.created_at, pending: !isLocalOnly() })
  save()
  try { await deliver(rumor, to, true, d.conv) } catch (e) { dmReacts.delete(rumor.id); save(); throw e }
  const r = dmReacts.get(rumor.id); if (r && !held.has(rumor.id)) r.pending = false
  save()
}

function rumorOf(kind: number, tags: string[][], content: string, at = Math.floor(Date.now() / 1000)): NostrEvent {
  const r = { kind, pubkey: me(), created_at: at, tags, content } as NostrEvent
  r.id = getEventHash(r)
  return r
}

const groupOf = (r: NostrEvent) => { const g = tag(r, 'group'); return g && /^[0-9a-f]{16}$/.test(g) ? g : undefined }

// ---- files, pictures and recordings (kind 15, dmfiles.ts) ----------------------------------

let lastAt = 0 // several files sent together: created_at strictly increasing (the outbox rule)
const nextAt = () => (lastAt = Math.max(lastAt + 1, Math.floor(Date.now() / 1000)))
/** what this device sent, shown from the file itself while it goes (and until a reload) */
const localPlain = new Map<string, string>()
/** files opened on this device: rid → blob: URL of the decrypted file (memory only) */
const opened = new Map<string, Promise<string>>()

/** Send FILES in CONV, each its own kind 15 (encrypted here, the ciphertext uploaded as a step of
 *  the outbox item); CAPTION, when there is one, a kind 14 right after with the same group. */
export async function sendDmFiles(conv: Conv, files: { file: File; duration?: number }[], caption = '') {
  await load()
  if (isMail(conv)) throw new Error('Files can’t be sent in an e-mail thread yet.')
  const max = isGuest(me()) ? MAX_FILE_GUEST : MAX_FILE
  if (files.some((f) => f.file.size > max)) throw new Error(tooLarge(max))
  const to = peers(conv)
  const others = to.filter((p) => p !== me())
  const group = files.length > 1 || caption.trim() ? bytesToHex(crypto.getRandomValues(new Uint8Array(8))) : undefined
  for (const { file, duration } of files) {
    const plain = new Uint8Array(await file.arrayBuffer())
    const sealed = await encryptFile(plain)
    const mime = file.type || 'application/octet-stream'
    const dim = mime.startsWith('image/') ? await dimsOf(file) : undefined
    // a file's name goes with it (inside the sealed rumor); pictures and recordings have none
    const name = mime.startsWith('image/') || mime.startsWith('audio/') ? undefined : file.name || undefined
    const tags = fileTags(others, sealed, { mime, size: plain.length, name, dim, duration, group })
    const rumor = rumorOf(FILE_KIND, tags, `${BELFRY.blossom.replace(/\/+$/, '')}/${sealed.x}`, nextAt())
    localPlain.set(rumor.id, URL.createObjectURL(file))
    items.set(rumor.id, { rid: rumor.id, conv, from: me(), mine: true, at: rumor.created_at, kind: 15, text: '', fileTags: tags, file: fileRefOf(rumor) ?? undefined, ...(group ? { group } : {}), pending: true })
    save()
    if (isLocalOnly()) await kvSet('dmfile:' + sealed.x, bytesToHex(sealed.cipher)) // demo: the "storage"
    try {
      await deliver(rumor, to, true, conv, new Blob([sealed.cipher as BlobPart], { type: 'application/octet-stream' }))
    } catch (e) {
      items.delete(rumor.id)
      save()
      throw e
    }
    if (isLocalOnly()) { const d = items.get(rumor.id); if (d) d.pending = false }
  }
  if (caption.trim()) {
    const rumor = rumorOf(14, [...others.map((p) => ['p', p]), ['group', group!]], caption.trim(), nextAt())
    items.set(rumor.id, { rid: rumor.id, conv, from: me(), mine: true, at: rumor.created_at, kind: 14, text: caption.trim(), tags: rumor.tags, group, pending: true })
    save()
    await deliver(rumor, to, true, conv)
    if (isLocalOnly()) { const d = items.get(rumor.id); if (d) d.pending = false }
  }
  save()
}

async function dimsOf(f: File): Promise<string | undefined> {
  try { const b = await createImageBitmap(f); const d = `${b.width}x${b.height}`; b.close(); return d } catch { return undefined }
}

/** A kind 15's file as a blob: URL of its decrypted bytes (never a link to the ciphertext):
 *  downloaded, x checked, decrypted, ox checked. Throws UNREADABLE when anything is wrong. */
export function fileUrl(d: Dm): Promise<string> {
  const mine = localPlain.get(d.rid)
  if (mine) return Promise.resolve(mine)
  let p = opened.get(d.rid)
  if (!p) {
    p = (async () => {
      if (!d.file || !d.fileTags) throw new Error(UNREADABLE)
      let bytes: Uint8Array | undefined
      if (isLocalOnly()) { const h = await kvGet<string>('dmfile:' + d.file.x); bytes = h ? hexToBytes(h) : undefined }
      else {
        for (const u of [d.file.url, `${BELFRY.blossom.replace(/\/+$/, '')}/${d.file.x}`]) {
          try { const r = await fetch(u); if (r.ok) { bytes = new Uint8Array(await r.arrayBuffer()); break } } catch { /* the next */ }
        }
      }
      if (!bytes) throw new Error(UNREADABLE)
      const o = await openFile(d.fileTags, bytes)
      if (!o.ok) throw new Error(UNREADABLE)
      return URL.createObjectURL(new Blob([o.plain as BlobPart], { type: o.mime }))
    })()
    p.catch(() => opened.delete(d.rid)) // a failed download is tried again next time it shows
    opened.set(d.rid, p)
  }
  return p
}

/** One line for the list of conversations. */
export const previewOf = (d: Dm) => (d.kind === 15 ? (d.file ? d.file.name : 'File') : d.text)

/** Send TEXT in CONV: a NIP-17 message, or an e-mail through the belfry. */
export async function sendDm(conv: Conv, text: string, subject?: string) {
  await load()
  if (!isMail(conv)) {
    const to = peers(conv)
    const rumor = rumorOf(14, to.filter((p) => p !== me()).map((p) => ['p', p]), text)
    items.set(rumor.id, { rid: rumor.id, conv, from: me(), mine: true, at: rumor.created_at, kind: 14, text, tags: rumor.tags, pending: true })
    save()
    try {
      await deliver(rumor, to, true, conv) // saved; the outbox sends it when it can
    } catch (e) {
      items.delete(rumor.id) // the composer gets the text back
      save()
      throw /curve|hex/i.test(String(e)) ? new Error('This is not a valid Nostr key.') : e
    }
    if (isLocalOnly()) { const d = items.get(rumor.id); if (d) d.pending = false }
    save()
    return
  }
  const b = await bridge()
  if (!b) throw new Error('This belfry does not send e-mail yet.')
  const myAddress = address()
  if (!myAddress) throw new Error('You have no address here yet (name@domain).')
  const to = mailAddress(conv)
  const fresh = isNewMail(conv)
  // in a thread: answer its last mail (theirs or mine), Re: its subject; new: a thread of its own
  const before = fresh ? undefined : thread(conv).filter((d) => d.kind === 1301).at(-1)
  const base = fresh ? (subject?.trim() || '(no subject)') : baseSubject(before?.subject) || '(no subject)'
  const subj = fresh || !before ? base : `Re: ${base}`
  const { raw, messageId } = buildMail({
    fromName: realName(me()) || myAddress.split('@')[0], from: myAddress, to, subject: subj, text,
    inReplyTo: before?.messageId, references: before?.references, domain: BELFRY.domain,
  })
  const target = fresh ? mailThreadOf(to, subj) : conv
  const emailId = crypto.randomUUID()
  const rumor = rumorOf(1301, [['email-id', emailId], ['mail-from', myAddress], ['rcpt-to', to], ['subject', subj]], raw)
  items.set(rumor.id, { rid: rumor.id, conv: target, from: me(), mine: true, at: rumor.created_at, kind: 1301, text, subject: subj, emailId, raw, mailTags: rumor.tags, messageId, references: before ? [before.references, before.messageId].filter(Boolean).join(' ') : undefined, pending: true })
  reports.set(emailId, { status: 'queued', details: 'Waiting for the connection…' })
  save()
  try {
    await deliver(rumor, [b], true, target) // saved; the outbox sends it when it can, the report follows
  } catch (e) {
    reports.set(emailId, { status: 'failed', details: e instanceof Error ? e.message : String(e) })
    const d = items.get(rumor.id)
    if (d) { d.pending = false; d.failed = String(e) }
  }
  if (isLocalOnly()) {
    reports.set(emailId, { status: 'failed', details: 'Demo mode sends no e-mail.' })
    const d = items.get(rumor.id)
    if (d) d.pending = false
  }
  save()
  return target
}

/** The bridge's delivery reports (kind 7679) for my e-mails that have none yet. */
export async function checkReports() {
  const b = await bridge()
  const s = currentSigner()
  const open = [...[...items.values()].filter((d) => d.mine && d.emailId && ['queued', undefined].includes(reports.get(d.emailId)?.status)).map((d) => d.emailId!), ...channelSends]
  if (!b || !s || !open.length || isLocalOnly()) return
  for (const e of await queryOnce('main', { kinds: [7679], authors: [b], '#r': open.slice(-50) })) {
    try {
      // §7 (sealedcore.openReport): with an lb, opened with that letterbox's secret only; without,
      // the old kind, by the signer as before; an lb this device lacks: tried again next time
      const r = openReportHere(e, b)
      if (r.state === 'locked' || r.state === 'error') continue
      const raw = r.state === 'opened' ? r.body : JSON.parse(await s.decrypt(tag(e, 'ephemeral-pubkey')!, e.content))
      const body = raw as { status: Report['status']; details?: string; smtp_response?: string }
      reports.set(tag(e, 'r')!, { status: body.status, details: body.details || body.smtp_response || '' })
      if (body.status !== 'queued') channelSends.delete(tag(e, 'r')!)
    } catch { /* not mine */ }
  }
  save()
}

/** convFor: the member lookup of this belfry's domain didn't answer (nothing opened, the input kept). */
export class LookupFailed extends Error {}

/** Where a new conversation goes: a member (name, name@this-domain, npub) or an e-mail address. */
export async function convFor(input: string, candidates: { pubkey: string; name: string; nip05?: string }[]): Promise<Conv | null> {
  const v = input.trim().toLowerCase()
  const hit = candidates.find((c) => c.nip05?.toLowerCase() === v || c.name.toLowerCase() === v)
  if (hit) return 'n:' + hit.pubkey
  if (/^[0-9a-f]{64}$/.test(v)) return 'n:' + v
  const m = /^([^@\s]+)@([^@\s]+\.[^@\s]+)$/.exec(v)
  if (!m) return null
  if (m[2] === BELFRY.domain) {
    // a member of this belfry is never written to by e-mail because the lookup failed: only a
    // definite answer without that name goes on as an e-mail (an alias of the domain)
    let names: Record<string, unknown> | undefined
    try {
      const r = await fetch(`https://${m[2]}/.well-known/nostr.json?name=${encodeURIComponent(m[1])}`)
      if (!r.ok) throw new Error(String(r.status))
      const j = (await r.json()) as { names?: unknown }
      if (!j || typeof j !== 'object' || (j.names !== undefined && (typeof j.names !== 'object' || j.names === null))) throw new Error('bad answer')
      names = (j.names ?? {}) as Record<string, unknown>
    } catch { throw new LookupFailed(`${BELFRY.domain} didn’t answer, so we can’t tell if ${m[1]} is a member. Try again in a moment.`) }
    const pk = names[m[1]]
    if (typeof pk === 'string' && /^[0-9a-f]{64}$/.test(pk)) return 'n:' + pk
  }
  return 'm:' + v
}

/** Demo mode only (tests): an e-mail arrives from SENDER as the belfry would deliver it. */
/** BODY: a whole MIME body with its own Content-Type header line(s) instead of plain TEXT (files). */
export async function demoMail(sender: string, subject: string, text: string, verified = true, body?: string) {
  if (!isLocalOnly()) return
  const sk = hexToBytes(DEMO_BRIDGE)
  const raw = body ? `From: ${sender}\r\nTo: ${address() || 'me@demo'}\r\nSubject: ${subject}\r\nMessage-ID: <${crypto.randomUUID()}@example.org>\r\nMIME-Version: 1.0\r\n${body}` : `From: ${sender}\r\nTo: ${address() || 'me@demo'}\r\nSubject: ${subject}\r\nMessage-ID: <${crypto.randomUUID()}@example.org>\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n${text}\r\n`
  const rumor = { kind: 1301, pubkey: getPublicKey(sk), created_at: Math.floor(Date.now() / 1000), content: raw,
    tags: [['email-id', crypto.randomUUID()], ['mail-from', addressOf(sender).address], ['p', me()], ['subject', subject], ['belfry-auth', verified ? 'pass' : 'none']] } as NostrEvent
  rumor.id = getEventHash(rumor)
  const seal = finalizeEvent({ kind: 13, created_at: backdate(), tags: [], content: nip44.v2.encrypt(JSON.stringify(rumor), nip44.v2.utils.getConversationKey(sk, me())) }, sk)
  const one = generateSecretKey()
  addEvents([finalizeEvent({ kind: 1059, created_at: backdate(), tags: [['p', me()]], content: nip44.v2.encrypt(JSON.stringify(seal), nip44.v2.utils.getConversationKey(one, me())) }, one)])
}

/** Demo mode only (tests, the encryption boards): a kind 14 message at AT (seconds) in the
 *  conversation of me and PEOPLE, from SK (a demo person's key) or from me (no SK), as my relay
 *  would hold its gift wrap to me. */
export async function demoDm(people: string[], text: string, at: number, sk?: Uint8Array) {
  if (!isLocalOnly()) return
  const from = sk ? getPublicKey(sk) : me()
  const rumor = { kind: 14, pubkey: from, created_at: at, content: text, tags: [...new Set([...people, me()])].filter((p) => p !== from).map((p) => ['p', p]) } as unknown as NostrEvent
  rumor.id = getEventHash(rumor)
  const seal = sk
    ? finalizeEvent({ kind: 13, created_at: at, tags: [], content: nip44.v2.encrypt(JSON.stringify(rumor), nip44.v2.utils.getConversationKey(sk, me())) }, sk)
    : await currentSigner()!.sign({ kind: 13, created_at: at, tags: [], content: await currentSigner()!.encrypt(me(), JSON.stringify(rumor)) })
  const one = generateSecretKey()
  addEvents([finalizeEvent({ kind: 1059, created_at: at, tags: [['p', me()]], content: nip44.v2.encrypt(JSON.stringify(seal), nip44.v2.utils.getConversationKey(one, me())) }, one)])
  return rumor.id
}
