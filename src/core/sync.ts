import { Relay, type Subscription } from 'nostr-tools/relay'
import type { EventTemplate, Filter, NostrEvent, VerifiedEvent } from 'nostr-tools'
import { sha256 } from '@noble/hashes/sha256'
import { bytesToHex } from '@noble/hashes/utils'
import { ACCESS_KINDS, BELFRY, CLIENT_TAG, PLACE, getBackend } from '../config'
import type { Signer } from './auth'
import { GUEST_MODE, askToJoin, reading } from './guestmode'
import { db, type Attachment, type OutboxItem, type Target } from './db'
import { addEvents, all, dropOutbox, hasEvent, latestCreatedAt, outboxItems, putOutbox, setRelay, setStatus, status } from './store'

// Offline first: every write lands in the outbox (IndexedDB) and shows in the UI
// at once. A flush loop signs, uploads attachments and publishes when it can.

export const KINDS_GROUPS = [0, 39000, 39001, 39002, 9, 1111, 1010, 7, 9007, 9002, 9008, 9000, 9001, 9005, 9021, 9022, 1068, 1018, 5, 1985, 30078, 2100, 2101, 2102, 2103, 31925, ...ACCESS_KINDS]
export const KINDS_MAIN = [0, 31922, 31923, 31925, 30023, 30402, 5]

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

/** A rule every event to the groups relay passes first (model.ts: archived groups take nothing):
 *  it returns why not, or nothing. */
let writeGuard: ((t: { kind: number; tags: string[][] }) => string | undefined) | undefined
export function setWriteGuard(f: typeof writeGuard) { writeGuard = f }

export async function enqueue(
  target: Target,
  t: { kind: number; content?: string; tags: string[][]; created_at?: number },
  files: File[] = []
) {
  // guest mode: nothing is sent before joining; the Join sheet opens instead
  if (reading()) { askToJoin(); throw new Error(GUEST_MODE) }
  const refused = target === 'groups' ? writeGuard?.(t) : undefined
  if (refused) throw new Error(refused)
  return put(target, t, files)
}

/** Like enqueue, but sent as a call to the belfry (POST BELFRY.api + PATH with BODY), which
 *  publishes the change itself; T is how it shows meanwhile. */
export async function enqueueApi(target: Target, t: { kind: number; content?: string; tags: string[][] }, path: string, body: unknown) {
  if (reading()) { askToJoin(); throw new Error(GUEST_MODE) }
  const refused = target === 'groups' ? writeGuard?.(t) : undefined
  if (refused) throw new Error(refused)
  return put(target, t, [], { path, body })
}

/** Save and send, whoever asks (the app's own first events; everything else goes through enqueue). */
async function put(
  target: Target,
  t: { kind: number; content?: string; tags: string[][]; created_at?: number },
  files: File[] = [],
  http?: OutboxItem['http']
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
    created_at: Math.max(now() - demoShift, t.created_at ?? 0),
    attachments,
    status: 'queued',
    tries: 0,
    ...(http ? { http } : {}),
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

let beforeFlushHook: (() => Promise<void>) | undefined
/** sealedrt.ts: sealed wraps are checked against fresh heads before they go out. */
export function setBeforeFlush(f: () => Promise<void>) { beforeFlushHook = f }

export async function flush() {
  if (!signer) return
  if (localOnly && demoHold) return // tests: a relay that takes its time
  if (flushing) { again = true; return }
  flushing = true
  again = false
  const stalled = new Set<Target>()
  try {
    if (beforeFlushHook && !localOnly && navigator.onLine) await beforeFlushHook().catch(() => { /* heads unknown: what is queued goes as it is */ })
    for (const item of outboxItems()) {
      if (item.status === 'failed' || stalled.has(item.target)) continue
      if (!localOnly && !navigator.onLine) break
      try {
        const ev = await send(item)
        retryStep = 0
        if (!item.signed && !item.http) addEvents([ev])
        await dropOutbox(item.localId)
        sentListeners.forEach((f) => f(item, ev))
      } catch (e) {
        const msg = e instanceof Error ? e.message : String(e)
        // a file the storage refuses (too big, a type it does not take, not allowed) will be refused
        // again: fail this one message instead of holding back every later one to that relay.
        // 401, 408 and 429 can pass on a later try.
        const refusedUpload = /^upload failed \((4\d\d)/.exec(msg)
        const permanent = /^(blocked|invalid|restricted|pow|duplicate|error):/.test(msg) || /not allowed|denied|unauthori[sz]ed/i.test(msg)
          || (!!refusedUpload && !['401', '408', '429'].includes(refusedUpload[1]))
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
  else if (stalled.size) retrySoon()
}

// A send that failed for now (a dropped socket, a signer that was reconnecting) is tried again
// within seconds, not at the next 15 s watchdog tick: 1.5 s, 3 s, 6 s, then every 12 s.
let retryTimer: ReturnType<typeof setTimeout> | undefined
let retryStep = 0
function retrySoon() {
  if (retryTimer) return
  const ms = Math.min(12_000, 1500 * 2 ** retryStep++)
  retryTimer = setTimeout(() => {
    retryTimer = undefined
    if (!outboxItems().some((o) => o.status === 'queued' && o.tries > 0)) retryStep = 0
    void flush()
  }, ms)
}

/** Save an event that is signed already (gift wraps) and publish it when the relay answers. */
// browser tests only (?backend=demo, nothing leaves the device): publish raw events, e.g. a to-do heartbeat
if (typeof window !== 'undefined' && getBackend() === 'demo') (window as unknown as { __demoEnqueue: typeof enqueue; __demoAt: (secs: number) => void }).__demoEnqueue = (...a) => enqueue(...a)
// and pretend the clock is earlier for the next events (to test what a long stretch looks like)
if (typeof window !== 'undefined' && getBackend() === 'demo') (window as unknown as { __demoAt: (secs: number) => void }).__demoAt = (secs) => { demoShift = secs }
let demoShift = 0
// and hold the outbox (a slow relay: what is queued stays queued until released)
let demoHold = false
if (typeof window !== 'undefined' && getBackend() === 'demo') (window as unknown as { __demoHold: (on: boolean) => void }).__demoHold = (on) => { demoHold = on; if (!on) void flush() }

export async function enqueueSigned(target: Target, ev: NostrEvent, dm?: string) {
  if (reading()) { askToJoin(); throw new Error(GUEST_MODE) }
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

/** Why the belfry refused a call, in a sentence (its `error` codes, Belfry checkin.py). */
let apiErrors: Record<string, string> = {}
export function setApiErrors(m: Record<string, string>) { apiErrors = m }
// demo only (tests): the next API calls from the outbox fail with this code ('' = they pass)
let demoApiFail = ''
if (typeof window !== 'undefined' && getBackend() === 'demo') (window as unknown as { __demoApiFail: (code: string) => void }).__demoApiFail = (code) => { demoApiFail = code }

/** An outbox call to the belfry. Refused for good (4xx but 401, 408, 429): "error: <sentence>";
 *  anything else (offline, 5xx, a signer that was away) is tried again. */
async function sendApi(item: OutboxItem): Promise<NostrEvent> {
  const http = item.http!
  if (localOnly) {
    if (demoApiFail) throw new Error('error: ' + (apiErrors[demoApiFail] ?? apiErrors.failed ?? 'That did not work.'))
    // what the belfry would publish: the event, as the groups key would sign it
    const ev = await signer!.sign({ kind: item.kind, content: item.content, tags: item.tags, created_at: item.created_at })
    addEvents([ev])
    synthesizeRelayEvents(ev)
    return {} as NostrEvent
  }
  let r: Response
  try { r = await signedFetch(`${BELFRY.api}${http.path}`, 'POST', http.body) } catch (e) {
    throw new Error('timeout: no connection to the belfry (' + (e instanceof Error ? e.message : String(e)) + ')')
  }
  if (r.ok) return {} as NostrEvent
  const d = (await r.json().catch(() => ({}))) as { error?: string }
  if (r.status >= 400 && r.status < 500 && ![401, 408, 429].includes(r.status)) {
    const code = r.status === 404 && !d.error ? 'missing' : String(d.error ?? 'failed')
    throw new Error('error: ' + (apiErrors[code] ?? (code === 'missing' ? 'This belfry can’t do this yet.' : apiErrors.failed ?? 'That did not work.')))
  }
  throw new Error(`timeout: the belfry answered ${r.status}`)
}

async function send(item: OutboxItem): Promise<NostrEvent> {
  if (item.http) return sendApi(item)
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
  const { content, after } = myProfile()
  await enqueue('main', { kind: 0, content: JSON.stringify({ ...content, picture: url }), tags: [], created_at: after })
}

/** My newest profile (kind 0), queued ones included, and the second a new one must come after:
 *  never the same second as the last, or two quick changes (a picture, then the name) tie and
 *  one of them is lost (relays keep the lowest id of a tie). */
export function myProfile(): { content: Record<string, unknown>; after: number } {
  const pk = signer?.pubkey ?? ''
  const mine = all(pk).filter((e) => e.kind === 0 && e.pubkey === pk).sort((a, b) => b.created_at - a.created_at)[0]
  let content: Record<string, unknown> = {}
  try { content = mine ? JSON.parse(mine.content) : {} } catch { content = {} }
  return { content, after: mine ? mine.created_at + 1 : 0 }
}

/** A picture (an event's cover) on Blossom, resized on the device; a data URL in demo mode. */
export async function uploadBlob(file: File, max = 1600): Promise<string> {
  if (!signer) throw new Error('Not signed in.')
  const small = file.type.startsWith('image/') ? await resize(file, localOnly ? 640 : max) : file
  if (localOnly) return new Promise<string>((res) => { const r = new FileReader(); r.onload = () => res(String(r.result)); r.readAsDataURL(small) })
  return upload(small, { blobKey: '', name: file.name || 'cover.jpg', mime: small.type || 'image/jpeg', size: small.size })
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
  // guest mode: only joining itself (and ending the session, using a link) is asked of the belfry
  if (method !== 'GET' && reading() && !/\/(guest\/save(\/verify)?|me\/sessions\/revoke|channel-invites\/[^/]+\/join)$/.test(url)) { askToJoin(); throw new Error(GUEST_MODE) }
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

/** Someone signed in uses a guest link: their account gets the channel (no second account). */
export async function joinWithAccount(token: string): Promise<{ channel: string; already: boolean }> {
  const res = await signedFetch(`${BELFRY.api}/channel-invites/${token}/join`, 'POST', {})
  const body = (await res.json().catch(() => ({}))) as { channel?: string; already?: boolean; message?: string; error?: string }
  if (!res.ok || !body.channel) throw new Error(body.error === 'name_needed' ? 'This account can\u2019t use invites.' : body.message || 'The invite could not be used just now.')
  return { channel: body.channel, already: !!body.already }
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

const connGen: Partial<Record<Target, number>> = {}
/** Which socket to TARGET this is (a new one each reconnect): sealedrt's knowledge is per socket. */
export const connectionOf = (target: Target) => (relayAuthed(target) ? connGen[target] ?? 0 : -1)

async function relay(target: Target): Promise<Relay> {
  const existing = relays[target]
  if (existing?.connected) return existing
  const url = URLS[target]
  const r = new Relay(url)
  authed[target] = false // a new socket: not signed in until it says so
  connGen[target] = (connGen[target] ?? 0) + 1 // answers given on an earlier socket say nothing about this one
  r._onauth = () => {
    setRelay(url, 'auth')
    r.auth((t) => signer!.sign(t) as Promise<VerifiedEvent>)
      .then(() => { setRelay(url, 'up'); authed[target] = true; replay[target] = !replayed[target]; resubscribe(target) })
      .catch((e) => { setRelay(url, 'down'); noteRefusal(String(e?.message ?? e)) })
  }
  r.onclose = () => {
    if (relays[target] && relays[target] !== r) return // an older socket
    authed[target] = false
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
    try {
      await within(r.publish(ev), 15000, 'the relay did not answer')
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e)
      if (!msg.startsWith('auth-required')) throw e
      await within(r.auth((t) => signer!.sign(t) as Promise<VerifiedEvent>), 15000, 'sign-in to the relay')
      // signed in only now: the live subscription had the public part only
      authed[target] = true; replay[target] = !replayed[target]; resubscribe(target)
      await within(r.publish(ev), 15000, 'the relay did not answer')
    }
  } catch (e) {
    // no answer on a socket that says it is open: it is dead (a proxy restart can leave it so).
    // Drop it; onclose reconnects at once and the outbox tries again on the new one.
    if (/timed out|timeout: the relay/.test(e instanceof Error ? e.message : String(e)) && relays[target] === r) {
      // (close() by us skips the socket's onclose: reconnect and resubscribe here)
      r.onclose = null
      try { r.close() } catch { /* already closed */ }
      delete relays[target]
      setRelay(URLS[target], 'down')
      attempts[target] = 0
      void connect(target)
    }
    throw e
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

/** What the relay has for FILTERS, once it said that was all (EOSE). `null` when it never said
 *  so (no connection, a timeout, the subscription closed): then nothing is known, and an empty
 *  list would be a guess. */
export async function queryComplete(target: Target, filters: Filter[], timeoutMs = 8000): Promise<NostrEvent[] | null> {
  if (localOnly || !signer) return null
  let r: Relay
  try { r = await within(relay(target), timeoutMs, 'no connection to the relay') } catch { return null }
  return new Promise((resolve) => {
    const out: NostrEvent[] = []
    let over = false
    const end = (v: NostrEvent[] | null) => {
      if (over) return
      over = true
      clearTimeout(timer)
      setTimeout(() => { try { sub.close() } catch { /* closed */ } }, 0)
      resolve(v)
    }
    const timer = setTimeout(() => end(null), timeoutMs)
    const sub = r.subscribe(filters, { onevent: (e) => out.push(e), oneose: () => end(out), onclose: () => end(null) })
  })
}

/** The relay gives at most PAGE events per filter in one REQ (rnostr max_limit): every page of
 *  FILTERS, newest first, stepping back with `until` (the oldest created_at seen, inclusive, ids
 *  de-duplicated), stopping only on a page that brings nothing new (a short page proves nothing).
 *  null when any page went unanswered: then nothing is known. */
export const PAGE = 300
export async function queryPaged(target: Target, filters: Filter[], timeoutMs = 8000, maxPages = 100): Promise<NostrEvent[] | null> {
  const out = new Map<string, NostrEvent>()
  for (const f of filters) {
    let until: number | undefined
    for (let i = 0; i < maxPages; i++) {
      const page = await queryComplete(target, [{ ...f, limit: PAGE, ...(until !== undefined ? { until } : {}) }], timeoutMs)
      if (page === null) return null
      const fresh = page.filter((e) => !out.has(e.id))
      if (!fresh.length) break
      for (const e of fresh) out.set(e.id, e)
      until = Math.min(...page.map((e) => e.created_at))
    }
  }
  return [...out.values()]
}

/** Ask the relays again whether this key is in (after a request, an invite code or an approval). */
export function recheckAccess() {
  for (const t of ['groups', 'main'] as Target[]) {
    if (relays[t]?.connected) resubscribe(t)
    else void connect(t)
  }
}

const subs: Partial<Record<Target, Subscription>> = {}
const eoseListeners = new Set<(target: Target) => void>()
/** Called each time a relay has sent what it stored for the live subscription (EOSE). */
export function onEose(f: (target: Target) => void) { eoseListeners.add(f) }

// Once signed in to a relay (NIP-42), the next subscription replays from the start: what came
// before the sign-in was only what anyone may read, and its newer timestamps must not become the
// `since` that hides the members-only events (Xavier, 2026-11-18: channels missing, the charter's
// older agreement missing).
// Only once per page load (the lead, 2026-11-19, from an iOS bug): resent from 0 on every
// reconnect, live events queued behind a 2000-event replay (the 15 s watchdog makes reconnects
// frequent on a phone). Later connections still resend after AUTH, from the mark.
const replay: Partial<Record<Target, boolean>> = {}
const replayed: Partial<Record<Target, boolean>> = {} // a replay's EOSE came, signed in, this page load
// groups known at this page's first answer: one learned later gets its own history from 0
const knownGroups = new Set<string>()
let groupsAnswered = false
function learnGroup(e: NostrEvent) {
  const d = e.tags.find((t) => t[0] === 'd')?.[1]
  if (!d || knownGroups.has(d)) return
  knownGroups.add(d)
  if (!groupsAnswered) return
  void queryOnce('groups', { kinds: KINDS_GROUPS, '#h': [d], limit: 2000 }, 10000).then(addEvents).catch(() => {})
}
// signed in to that relay (NIP-42) on its current connection: only then does an empty answer mean
// "it has nothing" (groupstate.ts), not "you may not see it yet"
const authed: Partial<Record<Target, boolean>> = {}
export const relayAuthed = (target: Target) => !!authed[target] && !!relays[target]?.connected
// The catch-up mark: up to when this device has everything the relay would show it signed in. It
// moves only at an EOSE received after AUTH (an answer given before the sign-in is the public part
// only, and its newer events must not hide the rest at the next reconnect or start). Per account
// (a belfry- key: accounts.ts keeps one copy per account).
const MARK = (t: Target) => 'belfry-sync-mark:' + t
const syncMark = (t: Target): number => { try { return Number(localStorage.getItem(MARK(t)) || 0) } catch { return 0 } }
function moveMark(t: Target) {
  const kinds = t === 'groups' ? KINDS_GROUPS : KINDS_MAIN
  const at = latestCreatedAt((e) => kinds.includes(e.kind)) // relay events only (store.ts: not the outbox)
  if (at > syncMark(t)) try { localStorage.setItem(MARK(t), String(at)) } catch { /* private mode */ }
}

// sealed conversations (sealedrt.ts): its own filters on the community relay (rosters, anchors,
// device requests, wraps to this device's key), set once it knows the device
let extraMain: () => Filter[] = () => []
export function setExtraMainFilters(f: () => Filter[]) { extraMain = f; resubscribe('main') }
/** Read the gift wraps again from the start once (a device that just received keys, §4). */
export function replayWraps() { wrapsFromZero = true; resubscribe('main') }
let wrapsFromZero = false
let lastWrapsSince = 0
let extraPaged: () => Filter[] = () => []
/** sealedrt.ts: its filters that must be read whole (rosters, anchors, wraps to this device). */
export function setPagedMainFilters(f: () => Filter[]) { extraPaged = f }
/** After an EOSE on the community relay: the gift wraps since the catch-up mark (or from 0 after
 *  replayWraps) and the sealed filters, read page by page (a REQ gives at most PAGE per filter). */
let paging = false
async function pageMain() {
  if (paging || !signer || localOnly) return
  paging = true
  try {
    const p = signer.pubkey
    const got = await queryPaged('main', [{ kinds: [1059], '#p': [p], since: lastWrapsSince }, ...extraPaged()])
    if (got) addEvents(got)
  } finally { paging = false }
}

function filters(target: Target): Filter[] {
  const kinds = target === 'groups' ? KINDS_GROUPS : KINDS_MAIN
  // from the catch-up mark (moved only at an EOSE after the relay signed us in), overlapping by
  // an hour: relays may deliver late events with older timestamps. No mark yet: from the start.
  const m = syncMark(target)
  const since = replay[target] || !m ? 0 : Math.max(0, m - 3600)
  // every group's metadata and the charter, always whole: a device that saw less before (a guest,
  // a sign-in after the first answer) still gets every channel and every agreement
  if (target === 'groups') return [
    { kinds, since, limit: 2000 },
    { kinds: [39000, 39001, 39002, 39003], limit: 1000 },
    { kinds: [2100, 2101, 2102, 2103], '#h': ['charter'], limit: 2000 },
  ]
  if (!signer) return [{ kinds, since, limit: 2000 }]
  // direct messages and e-mail (dm.ts) on the community relay, my DM relay (10050): gift wraps
  // are back-dated up to two days (NIP-59), so they overlap by three
  const p = signer.pubkey
  const wrapsSince = Math.max(0, latestCreatedAt((e) => e.kind === 1059 && e.tags.some((t) => t[0] === 'p' && t[1] === p)) - 3 * 86400)
  lastWrapsSince = wrapsFromZero ? 0 : wrapsSince
  wrapsFromZero = false
  const oldSince = Math.max(0, latestCreatedAt((e) => e.kind === 4) - 3600)
  // members' tags and places (people.ts): one replaceable event each, members-only relay
  const extrasSince = Math.max(0, latestCreatedAt((e) => e.kind === 30078 && e.tags.some((t) => t[0] === 'd' && t[1] === 'belfry:profile')) - 3600)
  return [
    { kinds, since, limit: 2000 },
    { kinds: [30078], '#d': ['belfry:profile', 'belfry:channel-order'], since: extrasSince, limit: 2000 },
    // my read state (readstate.ts): one replaceable event, always the exact d
    { kinds: [30078], authors: [p], '#d': ['belfry:read'], limit: 5 },
    { kinds: [1059], '#p': [p], since: wrapsSince, limit: PAGE }, // more than a page: pageWraps
    { kinds: [4], '#p': [p], since: oldSince, limit: 500 },
    { kinds: [4], authors: [p], since: oldSince, limit: 500 },
    ...extraMain(),
  ]
}

function resubscribe(target: Target) {
  const r = relays[target]
  if (!r) return
  subs[target]?.close()
  const fs = filters(target)
  const wasReplay = !!replay[target]
  replay[target] = false
  subs[target] = r.subscribe(fs, {
    alreadyHaveEvent: hasEvent, // known events skipped before the signature check
    onevent: (e) => { if (target === 'groups' && e.kind === 39000) learnGroup(e); addEvents([e]) },
    oneose: () => {
      if (relayAuthed(target)) { moveMark(target); if (wasReplay) replayed[target] = true }
      if (target === 'groups' && !groupsAnswered && relayAuthed(target)) {
        for (const e of all()) if (e.kind === 39000) { const d = e.tags.find((t) => t[0] === 'd')?.[1]; if (d) knownGroups.add(d) }
        groupsAnswered = true
      }
      eoseListeners.forEach((f) => f(target))
      if (target === 'main' && relayAuthed('main')) void pageMain()
      if (target !== 'groups') return
      if (status.access !== 'ok') setStatus({ access: 'ok', accessReason: '' })
      void firstChannel()
    },
    onclose: (reason) => {
      if (reason.startsWith('auth-required')) return // _onauth resubscribes
      // only the groups relay decides whether you're in: a channel's guest is in there, while
      // the community relay (members only) turns them away
      if (target === 'groups') noteRefusal(reason)
      // the relay ended the live subscription for another reason (or we closed it to replace it):
      // without a new one nothing arrives until a reload, so open it again while the socket lives
      if (subs[target] === sub && relays[target] === r && r.connected && status.access !== 'denied') {
        setTimeout(() => { if (subs[target] === sub && relays[target] === r) resubscribe(target) }, 2000)
      }
    },
  })
  const sub = subs[target]
}

/** Is the socket really alive? A phone that slept keeps a WebSocket that says "open" but is dead:
 *  nothing arrives and nothing says so. Ask the relay something tiny; no answer in 6 s means dead:
 *  drop it, and onclose reconnects and catches up (since = the latest event seen − 1 h). */
const probing: Partial<Record<Target, boolean>> = {}
function probe(target: Target) {
  const r = relays[target]
  if (!r?.connected || probing[target]) return
  probing[target] = true
  let answered = false
  const end = () => { answered = true; try { s.close() } catch { /* closed */ } }
  const s = r.subscribe([{ kinds: [39000], limit: 1 }], { oneose: end, onevent: () => {}, onclose: () => { answered = true } })
  setTimeout(() => {
    probing[target] = false
    if (answered || relays[target] !== r) return
    console.warn('relay did not answer, reconnecting', URLS[target])
    try { r.close() } catch { /* already closed */ }
    if (relays[target] === r) { delete relays[target]; setRelay(URLS[target], 'down'); void connect(target) }
  }, 6000)
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
let ticks = 0
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
  // back in the foreground: the socket may have died while asleep (probe), reconnect if so
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') { wake(); probe('groups'); probe('main') } })
  window.addEventListener('pageshow', () => { probe('groups'); probe('main') })
  setInterval(() => {
    if (!signer) return
    if (status.online !== navigator.onLine) setStatus({ online: navigator.onLine })
    if (!localOnly && navigator.onLine) for (const t of ['groups', 'main'] as Target[]) if (!relays[t]?.connected) void connect(t)
    // every minute, check the open sockets still answer (a silent dead socket = no live updates)
    if (!localOnly && navigator.onLine && document.visibilityState === 'visible' && ++ticks % 4 === 0) { probe('groups'); probe('main') }
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
      await put('groups', { kind: 9007, tags: [['h', 'general']] })
      await put('groups', { kind: 9002, tags: [['h', 'general'], ['name', 'general'], ['about', `Everything about ${PLACE}. Say hi 👋`]] })
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
  // only once signed in to the relay: before, its answer can't show the members-only channels
  if (firstChannelTried || reading() || !relayAuthed('groups') || latestCreatedAt((e) => e.kind === 39000)) return
  firstChannelTried = true
  await enqueue('groups', { kind: 9007, tags: [['h', 'general']] })
  await enqueue('groups', { kind: 9002, tags: [['h', 'general'], ['name', 'general'], ['about', `Everything about ${PLACE}. Say hi 👋`]] })
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
