import { Relay, type Subscription } from 'nostr-tools/relay'
import type { EventTemplate, Filter, NostrEvent, VerifiedEvent } from 'nostr-tools'
import { sha256 } from '@noble/hashes/sha256'
import { bytesToHex } from '@noble/hashes/utils'
import { BELFRY, CLIENT_TAG } from '../config'
import type { Signer } from './auth'
import { db, type Attachment, type OutboxItem, type Target } from './db'
import { addEvents, dropOutbox, latestCreatedAt, outboxItems, putOutbox, setRelay, setStatus, status } from './store'

// Offline first: every write lands in the outbox (IndexedDB) and shows in the UI
// at once. A flush loop signs, uploads attachments and publishes when it can.

export const KINDS_GROUPS = [39000, 39001, 39002, 9, 9007, 9002, 9008, 5, 1985, 2100, 2101, 2102, 2103]
export const KINDS_MAIN = [0, 31922, 31923, 31925, 5]

const URLS: Record<Target, string> = { groups: BELFRY.groupsRelay, main: BELFRY.mainRelay }

let signer: Signer | null = null
let localOnly = true
const relays: Partial<Record<Target, Relay>> = {}
let lastCreatedAt = 0

export function me() {
  return signer?.pubkey ?? ''
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
export async function flush() {
  if (!signer || flushing) return
  flushing = true
  try {
    for (const item of outboxItems()) {
      if (item.status === 'failed') continue
      if (!localOnly && !navigator.onLine) break
      try {
        const ev = await send(item)
        addEvents([ev])
        await dropOutbox(item.localId)
      } catch (e) {
        const msg = e instanceof Error ? e.message : String(e)
        const permanent = /^(blocked|invalid|restricted|pow|duplicate|error):/.test(msg) || /not allowed|denied|unauthori[sz]ed/i.test(msg)
        if (msg.startsWith('restricted')) noteRefusal(msg)
        await putOutbox({ ...item, tries: item.tries + 1, status: permanent ? 'failed' : 'queued', error: msg })
        if (!permanent) break // keep the order: retry this one before later items
      }
    }
  } finally {
    flushing = false
  }
}

export async function retry(localId: string) {
  const item = outboxItems().find((o) => o.localId === localId)
  if (item) await putOutbox({ ...item, status: 'queued', error: undefined })
  void flush()
}

async function send(item: OutboxItem): Promise<NostrEvent> {
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

async function publish(target: Target, ev: NostrEvent) {
  const r = await relay(target)
  try {
    await r.publish(ev)
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e)
    if (!msg.startsWith('auth-required')) throw e
    await r.auth((t) => signer!.sign(t) as Promise<VerifiedEvent>)
    await r.publish(ev)
  }
}

const subs: Partial<Record<Target, Subscription>> = {}

function filters(target: Target): Filter[] {
  const kinds = target === 'groups' ? KINDS_GROUPS : KINDS_MAIN
  // overlap by an hour: relays may deliver late events with older timestamps
  const since = Math.max(0, latestCreatedAt((e) => kinds.includes(e.kind)) - 3600)
  return [{ kinds, since, limit: 2000 }]
}

function resubscribe(target: Target) {
  const r = relays[target]
  if (!r) return
  subs[target]?.close()
  subs[target] = r.subscribe(filters(target), {
    onevent: (e) => addEvents([e]),
    oneose: () => { if (target === 'groups' && status.access !== 'ok') setStatus({ access: 'ok', accessReason: '' }) },
    onclose: (reason) => {
      if (reason.startsWith('auth-required')) return // _onauth resubscribes
      noteRefusal(reason)
    },
  })
}

function noteRefusal(reason: string) {
  if (/^(restricted|blocked)|pass|member/i.test(reason)) setStatus({ access: 'denied', accessReason: reason })
}

async function connect(target: Target) {
  if (localOnly || !signer) return
  try {
    await relay(target)
    resubscribe(target)
    void flush()
  } catch {
    setRelay(URLS[target], 'down')
    setTimeout(() => void connect(target), backoff(target))
  }
}

export async function start(s: Signer, opts: { localOnly: boolean }) {
  signer = s
  localOnly = opts.localOnly
  lastCreatedAt = Math.max(lastCreatedAt, latestCreatedAt((e) => e.pubkey === s.pubkey))
  window.addEventListener('online', () => {
    setStatus({ online: true })
    void connect('groups')
    void connect('main')
  })
  window.addEventListener('offline', () => setStatus({ online: false }))
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
