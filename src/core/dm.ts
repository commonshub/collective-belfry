import { finalizeEvent, generateSecretKey, getEventHash, getPublicKey, nip44, verifyEvent, type NostrEvent } from 'nostr-tools'
import { sha256 } from '@noble/hashes/sha256'
import { bytesToHex, hexToBytes } from '@noble/hashes/utils'
import { BELFRY } from '../config'
import { kvGet, kvSet } from './db'
import { addressOf, buildMail, parseMail, type Part } from './mime'
import { displayName } from './model'
import { addEvents, all, subscribe, touch } from './store'
import { currentSigner, isLocalOnly, me, publishNow, queryOnce } from './sync'

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
  kind: 14 | 4 | 1301
  text: string
  subject?: string
  fromName?: string
  emailId?: string
  messageId?: string
  references?: string
  verified?: boolean // e-mail: the bridge saw DKIM or SPF aligned with From
  attachments?: { name: string; type: string; size: number }[]
  raw?: string // e-mail MIME when it fitted in the wrap (attachments are read from it)
  large?: { url: string; key: string; nonce: string }
  pending?: boolean
}

export interface Report { status: 'delivered' | 'failed' | 'delayed' | 'queued'; details: string }

const items = new Map<string, Dm>() // by rid
const seen = new Set<string>() // event ids already read (gift wraps, kind 4)
const reports = new Map<string, Report>() // by email-id
let read: Record<string, number> = {}
let myAddress = ''
let bridgeKey: string | undefined
let loaded = false

const DEMO_BRIDGE = bytesToHex(sha256('osv-demo-bridge')) // demo mode only: the "belfry" that mails

export function setMyAddress(a?: string) { if (a) myAddress = a.toLowerCase() }
export const address = () => myAddress

export async function bridge(): Promise<string | undefined> {
  if (isLocalOnly()) return getPublicKey(hexToBytes(DEMO_BRIDGE))
  if (bridgeKey) return bridgeKey
  bridgeKey = await kvGet<string>('dm-bridge')
  try {
    const r = await fetch(`https://${BELFRY.domain}/.well-known/nostr.json?name=_smtp`)
    const k = ((await r.json()) as { names?: Record<string, string> }).names?._smtp
    if (k && /^[0-9a-f]{64}$/.test(k)) { bridgeKey = k; void kvSet('dm-bridge', k) }
  } catch { /* offline: the stored one */ }
  return bridgeKey
}

// ---- reading -------------------------------------------------------------------

async function load() {
  if (loaded) return
  loaded = true
  for (const d of (await kvGet<Dm[]>('dm-items')) ?? []) items.set(d.rid, d)
  for (const id of (await kvGet<string[]>('dm-seen')) ?? []) seen.add(id)
  for (const [k, v] of Object.entries((await kvGet<Record<string, Report>>('dm-reports')) ?? {})) reports.set(k, v)
  read = (await kvGet<Record<string, number>>('dm-read')) ?? {}
}

let saveTimer: ReturnType<typeof setTimeout> | null = null
function save() {
  touch()
  saveTimer ??= setTimeout(() => {
    saveTimer = null
    void kvSet('dm-items', [...items.values()].filter((d) => !d.pending))
    void kvSet('dm-seen', [...seen])
    void kvSet('dm-reports', Object.fromEntries(reports))
  }, 300)
}

const tag = (e: { tags: string[][] }, n: string) => e.tags.find((t) => t[0] === n)?.[1]
const tagsOf = (e: { tags: string[][] }, n: string) => e.tags.filter((t) => t[0] === n).map((t) => t[1])

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
  if (rumor.kind === 14) return { ...base, from: rumor.pubkey, kind: 14, text: rumor.content }
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
  }
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
    const todo = all().filter((e) => !e.pending && !seen.has(e.id) && (
      (e.kind === 1059 && e.tags.some((t) => t[0] === 'p' && t[1] === self)) || e.kind === 4))
      .sort((a, b) => b.created_at - a.created_at)
    for (const e of todo) {
      try {
        let d: Dm | null = null
        if (e.kind === 1059) {
          const rumor = await unwrap(e)
          if (rumor) d = await fromRumor(rumor)
        } else if (e.pubkey === self || e.tags.some((t) => t[0] === 'p' && t[1] === self)) {
          const peer = e.pubkey === self ? tag(e, 'p') ?? self : e.pubkey
          const s = currentSigner()
          const text = s?.decrypt04 ? await s.decrypt04(peer, e.content) : '(an old-style message this app cannot read)'
          d = { rid: e.id, conv: 'n:' + peer, from: e.pubkey, mine: e.pubkey === self, at: e.created_at, kind: 4, text }
        }
        if (d) { const old = items.get(d.rid); items.set(d.rid, { ...d, mine: d.mine || !!old?.mine }) }
        seen.add(e.id)
      } catch (x) {
        if (String(x).includes('timeout')) break // the signer is away: next time
        seen.add(e.id) // not for me, or broken
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
  let t: ReturnType<typeof setTimeout> | null = null
  subscribe(() => { t ??= setTimeout(() => { t = null; void process() }, 200) })
  void load().then(process)
}

// ---- the views' data -------------------------------------------------------------

export interface Thread { conv: Conv; last: Dm; unread: number; count: number }

export function threads(): Thread[] {
  const by = new Map<Conv, Dm[]>()
  for (const d of items.values()) (by.get(d.conv) ?? by.set(d.conv, []).get(d.conv)!).push(d)
  return [...by.entries()].map(([conv, list]) => {
    list.sort((a, b) => a.at - b.at)
    const last = list[list.length - 1]
    return { conv, last, count: list.length, unread: list.filter((d) => !d.mine && d.at > (read[conv] ?? 0)).length }
  }).sort((a, b) => b.last.at - a.last.at)
}

export const unreadTotal = () => threads().reduce((n, t) => n + (t.unread ? 1 : 0), 0)
export const thread = (conv: Conv) => [...items.values()].filter((d) => d.conv === conv).sort((a, b) => a.at - b.at)
export const report = (emailId?: string) => (emailId ? reports.get(emailId) : undefined)
export const isMail = (conv: Conv) => conv.startsWith('m:')
export const peers = (conv: Conv) => (conv.startsWith('n:') ? conv.slice(2).split(',') : [])

export function convName(conv: Conv): string {
  if (isMail(conv)) {
    const named = thread(conv).find((d) => !d.mine && d.fromName)?.fromName
    return named ? `${named}` : conv.slice(2)
  }
  return peers(conv).map((p) => displayName(p)).join(', ')
}

export function markRead(conv: Conv) {
  const last = thread(conv).at(-1)?.at ?? 0
  if ((read[conv] ?? 0) >= last) return
  read = { ...read, [conv]: last }
  void kvSet('dm-read', read)
  touch()
}

// ---- sending ---------------------------------------------------------------------

const backdate = () => Math.floor(Date.now() / 1000) - Math.floor(Math.random() * 2 * 86400)

async function giftWrap(rumor: NostrEvent, to: string): Promise<NostrEvent> {
  const s = currentSigner()!
  const seal = await s.sign({ kind: 13, created_at: backdate(), tags: [], content: await s.encrypt(to, JSON.stringify(rumor)) })
  const one = generateSecretKey()
  return finalizeEvent({
    kind: 1059, created_at: backdate(), tags: [['p', to]],
    content: nip44.v2.encrypt(JSON.stringify(seal), nip44.v2.utils.getConversationKey(one, to)),
  }, one)
}

async function deliver(rumor: NostrEvent, to: string[]) {
  const wraps = await Promise.all([...new Set([...to, me()])].map((p) => giftWrap(rumor, p)))
  if (isLocalOnly()) { addEvents(wraps.filter((w) => w.tags[0][1] === me())); return }
  const results = await Promise.allSettled(wraps.map((w) => publishNow('main', w)))
  const failed = results.filter((r, i) => r.status === 'rejected' && wraps[i].tags[0][1] !== me())
  if (failed.length) throw new Error((failed[0] as PromiseRejectedResult).reason?.message ?? 'The relay refused the message.')
}

function rumorOf(kind: number, tags: string[][], content: string): NostrEvent {
  const r = { kind, pubkey: me(), created_at: Math.floor(Date.now() / 1000), tags, content } as NostrEvent
  r.id = getEventHash(r)
  return r
}

/** Send TEXT in CONV: a NIP-17 message, or an e-mail through the belfry. */
export async function sendDm(conv: Conv, text: string, subject?: string) {
  await load()
  if (!isMail(conv)) {
    const to = peers(conv)
    const rumor = rumorOf(14, to.filter((p) => p !== me()).map((p) => ['p', p]), text)
    items.set(rumor.id, { rid: rumor.id, conv, from: me(), mine: true, at: rumor.created_at, kind: 14, text, pending: true })
    save()
    try {
      await deliver(rumor, to)
      const d = items.get(rumor.id)
      if (d) d.pending = false
    } catch (e) {
      items.delete(rumor.id) // the composer gets the text back
      throw /curve|hex/i.test(String(e)) ? new Error('This is not a valid Nostr key.') : e
    } finally { save() }
    return
  }
  const b = await bridge()
  if (!b) throw new Error('This belfry does not send e-mail yet.')
  if (!myAddress) throw new Error('You have no address here yet (name@domain).')
  const to = conv.slice(2)
  const before = thread(conv).filter((d) => !d.mine).at(-1)
  const subj = subject?.trim() || (before?.subject ? (/^re:/i.test(before.subject) ? before.subject : `Re: ${before.subject}`) : '(no subject)')
  const { raw } = buildMail({
    fromName: displayName(me()), from: myAddress, to, subject: subj, text,
    inReplyTo: before?.messageId, references: before?.references, domain: BELFRY.domain,
  })
  const emailId = crypto.randomUUID()
  const rumor = rumorOf(1301, [['email-id', emailId], ['mail-from', myAddress], ['rcpt-to', to], ['subject', subj]], raw)
  items.set(rumor.id, { rid: rumor.id, conv, from: me(), mine: true, at: rumor.created_at, kind: 1301, text, subject: subj, emailId, raw, pending: true })
  reports.set(emailId, { status: 'queued', details: 'Sending…' })
  save()
  try {
    await deliver(rumor, [b])
    if (isLocalOnly()) reports.set(emailId, { status: 'failed', details: 'Demo mode sends no e-mail.' })
  } catch (e) {
    reports.set(emailId, { status: 'failed', details: e instanceof Error ? e.message : String(e) })
  } finally {
    const d = items.get(rumor.id)
    if (d) d.pending = false
    save()
  }
  if (!isLocalOnly()) for (const wait of [3000, 10000, 30000]) setTimeout(() => void checkReports(), wait)
}

/** The bridge's delivery reports (kind 7679) for my e-mails that have none yet. */
export async function checkReports() {
  const b = await bridge()
  const s = currentSigner()
  const open = [...items.values()].filter((d) => d.mine && d.emailId && ['queued', undefined].includes(reports.get(d.emailId)?.status)).map((d) => d.emailId!)
  if (!b || !s || !open.length || isLocalOnly()) return
  for (const e of await queryOnce('main', { kinds: [7679], authors: [b], '#r': open.slice(-50) })) {
    try {
      const body = JSON.parse(await s.decrypt(tag(e, 'ephemeral-pubkey')!, e.content)) as { status: Report['status']; details?: string; smtp_response?: string }
      reports.set(tag(e, 'r')!, { status: body.status, details: body.details || body.smtp_response || '' })
    } catch { /* not mine */ }
  }
  save()
}

/** Where a new conversation goes: a member (name, name@this-domain, npub) or an e-mail address. */
export async function convFor(input: string, candidates: { pubkey: string; name: string; nip05?: string }[]): Promise<Conv | null> {
  const v = input.trim().toLowerCase()
  const hit = candidates.find((c) => c.nip05?.toLowerCase() === v || c.name.toLowerCase() === v)
  if (hit) return 'n:' + hit.pubkey
  if (/^[0-9a-f]{64}$/.test(v)) return 'n:' + v
  const m = /^([^@\s]+)@([^@\s]+\.[^@\s]+)$/.exec(v)
  if (!m) return null
  if (m[2] === BELFRY.domain) {
    try {
      const r = await fetch(`https://${m[2]}/.well-known/nostr.json?name=${encodeURIComponent(m[1])}`)
      const pk = ((await r.json()) as { names?: Record<string, string> }).names?.[m[1]]
      if (pk) return 'n:' + pk
    } catch { /* fall back to e-mail */ }
  }
  return 'm:' + v
}

/** Demo mode only (tests): an e-mail arrives from SENDER as the belfry would deliver it. */
export async function demoMail(sender: string, subject: string, text: string, verified = true) {
  if (!isLocalOnly()) return
  const sk = hexToBytes(DEMO_BRIDGE)
  const raw = `From: ${sender}\r\nTo: ${myAddress || 'me@demo'}\r\nSubject: ${subject}\r\nMessage-ID: <${crypto.randomUUID()}@example.org>\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n${text}\r\n`
  const rumor = { kind: 1301, pubkey: getPublicKey(sk), created_at: Math.floor(Date.now() / 1000), content: raw,
    tags: [['email-id', crypto.randomUUID()], ['mail-from', addressOf(sender).address], ['p', me()], ['subject', subject], ['belfry-auth', verified ? 'pass' : 'none']] } as NostrEvent
  rumor.id = getEventHash(rumor)
  const seal = finalizeEvent({ kind: 13, created_at: backdate(), tags: [], content: nip44.v2.encrypt(JSON.stringify(rumor), nip44.v2.utils.getConversationKey(sk, me())) }, sk)
  const one = generateSecretKey()
  addEvents([finalizeEvent({ kind: 1059, created_at: backdate(), tags: [['p', me()]], content: nip44.v2.encrypt(JSON.stringify(seal), nip44.v2.utils.getConversationKey(one, me())) }, one)])
}
