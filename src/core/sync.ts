import { Relay, type Subscription } from 'nostr-tools/relay'
import type { EventTemplate, Filter, NostrEvent, VerifiedEvent } from 'nostr-tools'
import { sha256 } from '@noble/hashes/sha256'
import { bytesToHex } from '@noble/hashes/utils'
import { ACCESS_KINDS, BELFRY, CLIENT_TAG, NAME } from '../config'
import type { Signer } from './auth'
import { db, type Attachment, type OutboxItem, type Target } from './db'
import { addEvents, all, dropOutbox, latestCreatedAt, outboxItems, putOutbox, setRelay, setStatus, status } from './store'

// Offline first: every write lands in the outbox (IndexedDB) and shows in the UI
// at once. A flush loop signs, uploads attachments and publishes when it can.

export const KINDS_GROUPS = [0, 39000, 39001, 39002, 9, 1111, 7, 9007, 9002, 9008, 9000, 9001, 9005, 9021, 9022, 1068, 1018, 5, 1985, 30078, 2100, 2101, 2102, 2103, ...ACCESS_KINDS]
export const KINDS_MAIN = [0, 31922, 31923, 31925, 30023, 5]

const URLS: Record<Target, string> = { groups: BELFRY.groupsRelay, main: BELFRY.mainRelay }

let signer: Signer | null = null
let localOnly = true
const relays: Partial<Record<Target, Relay>> = {}
let lastCreatedAt = 0

export function me() {
  return signer?.pubkey ?? ''
}

export function currentSigner() {
  if (!signer) throw new Error('Not signed in.')
  return signer
}

// created_at must grow per client so quick sequences (claim → done) fold in order
export function now() {
  lastCreatedAt = Math.max(lastCreatedAt + 1, Math.floor(Date.now() / 1000))
  return lastCreatedAt
}

export function newId(bytes = 8) {
  return bytesToHex(crypto.getRandomValues(new Uint8Array(bytes)))
}

export async function enqueue(
  target: Target,
  t: { kind: number; content?: string; tags: string[][] },
  files: File[] = []
) {
  const d = await db()
  const attachments: Attachment[] = []
  for (const f of files) {
    const blobKey = newId()
    await d.put('blobs', f, blobKey)
    attachments.push({ blobKey, name: f.name, mime: f.type || 'application/octet-stream', size: f.size, ...(await dims(f)) })
  }
  const item: OutboxItem = {
    localId: 'local-' + newId(),
    target,
    kind: t.kind,
    content: t.content ?? '',
    tags: [...t.tags, CLIENT_TAG],
    created_at: now(),
    attachments,
    status: 'queued',
    tries: 0,
  }
  await putOutbox(item)
  void flush()
  return item.localId
}

async function dims(f: File): Promise<{ width?: number; height?: number }> {
  if (!f.type.startsWith('image/')) return {}
  try {
    const b = await createImageBitmap(f)
    const r = { width: b.width, height: b.height }
    b.close()
    return r
  } catch {
    return {}
  }
}

// ---- flushing the outbox ----------------------------------------------------

let flushing = false
let again = false // something was queued while a flush ran: run once more right after
// Everything you send is saved on this device first (the outbox, IndexedDB), then published
// when a relay answers, retried on every wake-up (online, back in the foreground, the 15 s
// watchdog) until the relay says yes or refuses for good. Order is kept per relay: a message
// waiting for one relay never holds back another relay's.
const sentListeners = new Set<(item: OutboxItem, ev: NostrEvent) => void>()
const failListeners = new Set<(item: OutboxItem) => void>()
/** Called when an outbox item is out (accepted by its relay), or refused for good. */
export function onOutbox(sent: (item: OutboxItem, ev: NostrEvent) => void, failed: (item: OutboxItem) => void) {
  sentListeners.add(sent)
  failListeners.add(failed)
}

export async function flush() {
  if (!signer) return
  if (flushing) { again = true; return }
  flushing = true
  again = false
  const stalled = new Set<Target>()
  try {
    for (const item of outboxItems()) {
      if (item.status === 'failed' || stalled.has(item.target)) continue
      if (!localOnly && !navigator.onLine) break
      try {
        const ev = await send(item)
        if (!item.signed) addEvents([ev])
        await dropOutbox(item.localId)
        sentListeners.forEach((f) => f(item, ev))
      } catch (e) {
        const msg = e instanceof Error ? e.message : String(e)
        const permanent = /^(blocked|invalid|restricted|pow|duplicate|error):/.test(msg) || /not allowed|denied|unauthori[sz]ed/i.test(msg)
        if (msg.startsWith('restricted') && item.target === 'groups') noteRefusal(msg)
        if (/^duplicate:/.test(msg)) { // the relay has it already: out
          await dropOutbox(item.localId)
          sentListeners.forEach((f) => f(item, item.signed ?? ({} as NostrEvent)))
          continue
        }
        const next = { ...item, tries: item.tries + 1, status: permanent ? 'failed' as const : 'queued' as const, error: msg }
        await putOutbox(next)
        if (permanent) failListeners.forEach((f) => f(next))
        else stalled.add(item.target) // keep this relay's order: retry this one before its later items
      }
    }
  } finally {
    flushing = false
  }
  if (again && !stalled.size) void flush()
}

/** Save an event that is signed already (gift wraps) and publish it when the relay answers. */
export async function enqueueSigned(target: Target, ev: NostrEvent, dm?: string) {
  const item: OutboxItem = {
    localId: 'local-' + newId(), target, kind: ev.kind, content: ev.content, tags: ev.tags, created_at: ev.created_at,
    attachments: [], status: 'queued', tries: 0, signed: ev, dm,
  }
  await putOutbox(item)
  void flush()
  return item.localId
}

export async function retry(localId: string) {
  const item = outboxItems().find((o) => o.localId === localId)
  if (item) await putOutbox({ ...item, status: 'queued', error: undefined })
  void flush()
}

async function send(item: OutboxItem): Promise<NostrEvent> {
  if (item.signed) {
    if (localOnly) return item.signed
    await publish(item.target, item.signed)
    return item.signed
  }
  const d = await db()
  let content = item.content
  const tags = [...item.tags]
  for (const a of item.attachments) {
    const blob = await d.get('blobs', a.blobKey)
    if (!blob) continue
    const url = localOnly ? 'idb:' + a.blobKey : await upload(blob, a)
    const x = localOnly ? '' : url.split('/').pop()!.split('.')[0]
    tags.push(['imeta', `url ${url}`, `m ${a.mime}`, `size ${a.size}`, ...(x ? [`x ${x}`] : []),
      ...(a.width ? [`dim ${a.width}x${a.height}`] : []), `alt ${a.name}`])
    content = content ? content + '\n' + url : url
  }
  const tpl: EventTemplate = { kind: item.kind, content, tags, created_at: item.created_at }
  const ev = await signer!.sign(tpl)
  if (localOnly) {
    synthesizeRelayEvents(ev)
    return ev
  }
  await publish(item.target, ev)
  // the blob stays cached: the uploaded URL is served from it until the next reload
  return ev
}

// ---- Blossom (BUD-02) ---------------------------------------------------------

/** A new profile picture: resized on the device, uploaded to Blossom (or kept as a small data
 *  URL in demo mode), then the kind-0 profile is republished with it, the rest kept. */
export async function setAvatar(file: File) {
  if (!signer) throw new Error('Not signed in.')
  const small = await resize(file, localOnly ? 160 : 512)
  const url = localOnly
    ? await new Promise<string>((res) => { const r = new FileReader(); r.onload = () => res(String(r.result)); r.readAsDataURL(small) })
    : await upload(small, { blobKey: '', name: 'avatar.jpg', mime: 'image/jpeg', size: small.size })
  const mine = all(signer.pubkey).filter((e) => e.kind === 0 && e.pubkey === signer!.pubkey).sort((a, b) => b.created_at - a.created_at)[0]
  let content: Record<string, unknown> = {}
  try { content = mine ? JSON.parse(mine.content) : {} } catch { content = {} }
  await enqueue('main', { kind: 0, content: JSON.stringify({ ...content, picture: url }), tags: [] })
}

async function resize(file: File, max: number): Promise<Blob> {
  const img = await createImageBitmap(file)
  const scale = Math.min(1, max / Math.max(img.width, img.height))
  const side = (n: number) => Math.max(1, Math.round(n * scale))
  const canvas = document.createElement('canvas')
  canvas.width = side(img.width)
  canvas.height = side(img.height)
  canvas.getContext('2d')!.drawImage(img, 0, 0, canvas.width, canvas.height)
  return new Promise((res, rej) => canvas.toBlob((b) => (b ? res(b) : rej(new Error('Could not read the image.'))), 'image/jpeg', 0.85))
}

/** fetch() with a NIP-98 Authorization signed by the member's key (through the session). */
export async function signedFetch(url: string, method = 'GET', body?: unknown): Promise<Response> {
  if (!signer) throw new Error('Not signed in.')
  const text = body === undefined ? '' : JSON.stringify(body)
  const tags = [['u', url], ['method', method]]
  if (text) tags.push(['payload', bytesToHex(sha256(new TextEncoder().encode(text)))])
  const auth = await signer.sign({ kind: 27235, content: '', tags, created_at: Math.floor(Date.now() / 1000) })
  return fetch(url, {
    method,
    headers: { Authorization: 'Nostr ' + btoa(JSON.stringify(auth)), ...(text ? { 'Content-Type': 'application/json' } : {}) },
    body: text || undefined,
  })
}

async function upload(blob: Blob, a: Attachment): Promise<string> {
  const buf = new Uint8Array(await blob.arrayBuffer())
  const x = bytesToHex(sha256(buf))
  const auth = await signer!.sign({
    kind: 24242,
    content: 'Upload ' + a.name,
    created_at: Math.floor(Date.now() / 1000),
    tags: [['t', 'upload'], ['x', x], ['expiration', String(Math.floor(Date.now() / 1000) + 600)]],
  })
  const res = await fetch(BELFRY.blossom + '/upload', {
    method: 'PUT',
    headers: { Authorization: 'Nostr ' + btoa(JSON.stringify(auth)), 'Content-Type': a.mime },
    body: blob,
  })
  if (!res.ok) throw new Error(`upload failed (${res.status} ${res.headers.get('x-reason') ?? ''})`)
  const desc = (await res.json()) as { url: string }
  return desc.url
}

// ---- relays ---------------------------------------------------------------------

async function relay(target: Target): Promise<Relay> {
  const existing = relays[target]
  if (existing?.connected) return existing
  const url = URLS[target]
  const r = new Relay(url)
  r._onauth = () => {
    setRelay(url, 'auth')
    r.auth((t) => signer!.sign(t) as Promise<VerifiedEvent>)
      .then(() => { setRelay(url, 'up'); resubscribe(target) })
      .catch((e) => { setRelay(url, 'down'); noteRefusal(String(e?.message ?? e)) })
  }
  r.onclose = () => {
    if (relays[target] && relays[target] !== r) return // an older socket
    setRelay(url, 'down')
    delete relays[target]
    setTimeout(() => void connect(target), backoff(target))
  }
  await r.connect()
  relays[target] = r
  attempts[target] = 0
  setRelay(url, 'up')
  return r
}

const attempts: Record<Target, number> = { groups: 0, main: 0 }
function backoff(t: Target) {
  attempts[t]++
  return Math.min(30_000, 800 * 2 ** attempts[t])
}

/** PROMISE, or "timeout: …" after MS: a connection that drops mid-way never answers, and the
 *  outbox must then retry rather than wait forever. */
function within<T>(promise: Promise<T>, ms: number, what: string): Promise<T> {
  let t: ReturnType<typeof setTimeout>
  promise.catch(() => {}) // after a timeout it may still fail: nobody waits for it any more
  return Promise.race([promise, new Promise<T>((_, rej) => { t = setTimeout(() => rej(new Error(`timeout: ${what}`)), ms) })])
    .finally(() => clearTimeout(t))
}

async function publish(target: Target, ev: NostrEvent) {
  const r = await within(relay(target), 15000, 'no connection to the relay')
  try {
    await within(r.publish(ev), 15000, 'the relay did not answer')
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e)
    if (!msg.startsWith('auth-required')) throw e
    await within(r.auth((t) => signer!.sign(t) as Promise<VerifiedEvent>), 15000, 'sign-in to the relay')
    await within(r.publish(ev), 15000, 'the relay did not answer')
  }
}

/** Publish at once, outside the outbox (gift wraps: they must not wait behind refused events). */
export async function publishNow(target: Target, ev: NostrEvent) {
  if (localOnly) throw new Error('Not in demo mode.')
  await publish(target, ev)
}

/** One-off query up to EOSE. */
export async function queryOnce(target: Target, filter: Filter, timeoutMs = 8000): Promise<NostrEvent[]> {
  const r = await relay(target)
  return new Promise((resolve) => {
    const out: NostrEvent[] = []
    const done = () => { clearTimeout(timer); sub.close(); resolve(out) }
    const timer = setTimeout(done, timeoutMs)
    const sub = r.subscribe([filter], { onevent: (e) => out.push(e), oneose: done, onclose: () => resolve(out) })
  })
}

/** Ask the relays again whether this key is in (after a request, an invite code or an approval). */
export function recheckAccess() {
  for (const t of ['groups', 'main'] as Target[]) {
    if (relays[t]?.connected) resubscribe(t)
    else void connect(t)
  }
}

const subs: Partial<Record<Target, Subscription>> = {}

function filters(target: Target): Filter[] {
  const kinds = target === 'groups' ? KINDS_GROUPS : KINDS_MAIN
  // overlap by an hour: relays may deliver late events with older timestamps
  const since = Math.max(0, latestCreatedAt((e) => kinds.includes(e.kind)) - 3600)
  if (target === 'groups' || !signer) return [{ kinds, since, limit: 2000 }]
  // direct messages and e-mail (dm.ts) on the community relay, my DM relay (10050): gift wraps
  // are back-dated up to two days (NIP-59), so they overlap by three
  const p = signer.pubkey
  const wrapsSince = Math.max(0, latestCreatedAt((e) => e.kind === 1059) - 3 * 86400)
  const oldSince = Math.max(0, latestCreatedAt((e) => e.kind === 4) - 3600)
  return [
    { kinds, since, limit: 2000 },
    { kinds: [1059], '#p': [p], since: wrapsSince, limit: 1000 },
    { kinds: [4], '#p': [p], since: oldSince, limit: 500 },
    { kinds: [4], authors: [p], since: oldSince, limit: 500 },
  ]
}

function resubscribe(target: Target) {
  const r = relays[target]
  if (!r) return
  subs[target]?.close()
  subs[target] = r.subscribe(filters(target), {
    onevent: (e) => addEvents([e]),
    oneose: () => {
      if (target !== 'groups') return
      if (status.access !== 'ok') setStatus({ access: 'ok', accessReason: '' })
      void firstChannel()
    },
    onclose: (reason) => {
      if (reason.startsWith('auth-required')) return // _onauth resubscribes
      // only the groups relay decides whether you're in: a channel's guest is in there, while
      // the community relay (members only) turns them away
      if (target === 'groups') noteRefusal(reason)
    },
  })
}

function noteRefusal(reason: string) {
  if (/^(restricted|blocked)|pass|member/i.test(reason)) setStatus({ access: 'denied', accessReason: reason })
}

const connecting: Partial<Record<Target, Promise<void>>> = {}
function connect(target: Target): Promise<void> {
  if (localOnly || !signer) return Promise.resolve()
  if (relays[target]?.connected) return Promise.resolve()
  // offline: nothing to try; the 'online' event (or the watchdog) calls again
  if (!navigator.onLine) { setRelay(URLS[target], 'down'); return Promise.resolve() }
  return (connecting[target] ??= (async () => {
    try {
      await relay(target)
      resubscribe(target) // catches up from the last event seen (since = latest − 1 h)
      void flush()
    } catch {
      setRelay(URLS[target], 'down')
      setTimeout(() => void connect(target), backoff(target))
    } finally {
      delete connecting[target]
    }
  })())
}

// Going offline and back online syncs by itself: the browser's online/offline events, the app
// coming back to the foreground, and a watchdog every 15 s (the online flag can lie, and a
// socket can die silently while a phone sleeps) all reconnect, catch up and send the outbox.
let watching = false
function watchNetwork() {
  if (watching) return
  watching = true
  const wake = () => {
    if (!signer) return
    attempts.groups = attempts.main = 0
    void connect('groups')
    void connect('main')
    void flush()
  }
  window.addEventListener('online', () => { setStatus({ online: true }); wake() })
  window.addEventListener('offline', () => {
    setStatus({ online: false })
    // the sockets are dead even if they don't know it yet: drop them, reconnect when back
    for (const t of ['groups', 'main'] as Target[]) {
      const r = relays[t]
      delete relays[t]
      if (r) { r.onclose = null; try { r.close() } catch { /* already closed */ } }
      setRelay(URLS[t], 'down')
    }
  })
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') wake() })
  setInterval(() => {
    if (!signer) return
    if (status.online !== navigator.onLine) setStatus({ online: navigator.onLine })
    if (!localOnly && navigator.onLine) for (const t of ['groups', 'main'] as Target[]) if (!relays[t]?.connected) void connect(t)
    if (outboxItems().some((o) => o.status !== 'failed')) void flush()
  }, 15_000)
}

export async function start(s: Signer, opts: { localOnly: boolean }) {
  signer = s
  localOnly = opts.localOnly
  lastCreatedAt = Math.max(lastCreatedAt, latestCreatedAt((e) => e.pubkey === s.pubkey))
  watchNetwork()
  if (localOnly) {
    if (!latestCreatedAt((e) => e.kind === 39000)) {
      await enqueue('groups', { kind: 9007, tags: [['h', 'general']] })
      await enqueue('groups', { kind: 9002, tags: [['h', 'general'], ['name', 'general'], ['about', 'Everything about the village. Say hi 👋']] })
    }
  } else {
    void connect('groups')
    void connect('main')
  }
  void flush()
}

// A new belfry's groups relay has no channel yet: the first member in opens #general (any member
// may create a group; its creator administers it). Once per page load, after the relay's EOSE.
let firstChannelTried = false
async function firstChannel() {
  if (firstChannelTried || latestCreatedAt((e) => e.kind === 39000)) return
  firstChannelTried = true
  await enqueue('groups', { kind: 9007, tags: [['h', 'general']] })
  await enqueue('groups', { kind: 9002, tags: [['h', 'general'], ['name', 'general'], ['about', `Everything about ${NAME}. Say hi 👋`]] })
  void flush()
}

export function stop() {
  for (const t of ['groups', 'main'] as Target[]) {
    subs[t]?.close()
    relays[t]?.close()
    delete relays[t]
  }
  signer = null
}

// In local-only (demo) mode there is no relay to turn 9007/9002 into 39000
// metadata and member lists, so do what the relay would do.
function synthesizeRelayEvents(ev: NostrEvent) {
  const h = ev.tags.find((t) => t[0] === 'h')?.[1]
  if (!h || (ev.kind !== 9007 && ev.kind !== 9002)) return
  const base = { pubkey: ev.pubkey, created_at: ev.created_at, sig: '', content: '' }
  const meta = ev.tags.filter((t) => ['name', 'about', 'picture'].includes(t[0]))
  if (ev.kind === 9002 || meta.length)
    addEvents([{ ...base, id: 'meta-' + ev.id, kind: 39000, tags: [['d', h], ...meta] }])
  if (ev.kind === 9007)
    addEvents([{ ...base, id: 'members-' + ev.id, kind: 39002, tags: [['d', h], ['p', ev.pubkey]] }])
}

export const isLocalOnly = () => localOnly
export const connectionStatus = () => status
