import { useSyncExternalStore } from 'react'
import type { NostrEvent } from 'nostr-tools'
import { db, type OutboxItem } from './db'

// In-memory mirror of IndexedDB, plus optimistic copies of queued outbox items.
// Every change bumps `version`; views derive what they need from `all()`.

export type Ev = NostrEvent & { pending?: OutboxItem }

const events = new Map<string, Ev>()
const outbox = new Map<string, OutboxItem>()
let version = 0
const listeners = new Set<() => void>()

export const status = {
  online: navigator.onLine,
  relays: {} as Record<string, 'up' | 'down' | 'auth'>,
  // the relays only let in keys with an active pass: 'denied' means no pass (yet)
  access: 'unknown' as 'unknown' | 'ok' | 'denied',
  accessReason: '',
}

function bump() {
  version++
  listeners.forEach((l) => l())
}

/** Something outside the event log changed (decrypted messages): views read again. */
export function touch() { bump() }

export function subscribe(l: () => void) {
  listeners.add(l)
  return () => listeners.delete(l)
}

export function useVersion() {
  return useSyncExternalStore(subscribe, () => version)
}

export async function loadAll() {
  const d = await db()
  for (const e of await d.getAll('events')) events.set(e.id, e)
  for (const o of await d.getAll('outbox')) outbox.set(o.localId, o)
  bump()
}

const writeQueue: NostrEvent[] = []
let writeTimer: ReturnType<typeof setTimeout> | null = null

/** Already held (a relay's copy of it is skipped before its signature is checked: sync.ts). */
export const hasEvent = (id: string) => events.has(id)
/** An event held already (checked when it came in). */
export const heldEvent = (id: string): Ev | undefined => events.get(id)

export function addEvents(list: NostrEvent[]) {
  let changed = false
  for (const e of list) {
    if (events.has(e.id)) continue
    events.set(e.id, e)
    writeQueue.push(e)
    changed = true
  }
  if (!changed) return
  bump()
  // batch IndexedDB writes: relays can deliver hundreds of events at once
  writeTimer ??= setTimeout(async () => {
    writeTimer = null
    const batch = writeQueue.splice(0)
    const tx = (await db()).transaction('events', 'readwrite')
    await Promise.all([...batch.map((e) => tx.store.put(e)), tx.done])
  }, 150)
}

export async function putOutbox(o: OutboxItem) {
  outbox.set(o.localId, o)
  await (await db()).put('outbox', o)
  bump()
}

export async function dropOutbox(localId: string) {
  outbox.delete(localId)
  await (await db()).delete('outbox', localId)
  bump()
}

export const outboxItems = () => [...outbox.values()].sort((a, b) => a.created_at - b.created_at)

export function setStatus(patch: Partial<typeof status>) {
  Object.assign(status, patch)
  bump()
}

export function setRelay(url: string, s: 'up' | 'down' | 'auth') {
  status.relays = { ...status.relays, [url]: s }
  bump()
}

// A replaceable or addressable event is replaced on the relay by its author's newer one (kind +
// author, + d for 30000–39999), but this store keeps every copy it ever saw: a reader filtering by
// h first found a superseded copy (OSV's #hello inbox, 2026-11-22). So only the newest copy of
// each address is shown, as the relay keeps it (newest created_at, then the lowest id): kinds 0, 3,
// 10000–19999 and the 30078 settings. Not 30023 (a proposal's versions are its history) nor other
// 3xxxx whose readers already take the newest; not the sealed anchor (d = belfry:sealed, read by
// sealedcore.newestAnchor, which skips an anchor that doesn't parse). Queued events are added
// after: they are newer than what they replace and their readers take the newest.
const SEALED_ANCHOR_D = 'belfry:sealed'
function addressOf(e: NostrEvent): string | undefined {
  const k = e.kind
  if (k === 0 || k === 3 || (k >= 10000 && k < 20000)) return `${k}:${e.pubkey}`
  if (k !== 30078) return undefined
  const d = e.tags.find((t) => t[0] === 'd')?.[1] ?? ''
  return d === SEALED_ANCHOR_D ? undefined : `${k}:${e.pubkey}:${d}`
}
const newer = (a: NostrEvent, b: NostrEvent) => a.created_at > b.created_at || (a.created_at === b.created_at && a.id < b.id)
export function current<T extends NostrEvent>(list: T[]): T[] {
  const best = new Map<string, T>()
  for (const e of list) {
    const a = addressOf(e)
    if (!a) continue
    const b = best.get(a)
    if (!b || newer(e, b)) best.set(a, e)
  }
  return list.filter((e) => { const a = addressOf(e); return !a || best.get(a) === e })
}

// All events, with queued ones shown as if they were already published.
// Keyed by ME too: queued events carry my key, so a call without it (or with another key) must
// not hand the next caller a list where my own queued events (a new profile…) belong to nobody.
let cache: { v: number; me: string; list: Ev[] } = { v: -1, me: '', list: [] }
export function all(me?: string): Ev[] {
  if (cache.v === version && cache.me === (me ?? '')) return cache.list
  const list: Ev[] = current([...events.values()])
  for (const o of outbox.values()) {
    if (o.signed) continue // a gift wrap on its way: its message shows from dm.ts, not as an event
    list.push({
      id: o.localId,
      pubkey: me ?? '',
      kind: o.kind,
      content: o.content,
      tags: o.tags,
      created_at: o.created_at,
      sig: '',
      pending: o,
    })
  }
  cache = { v: version, me: me ?? '', list }
  return list
}

export function latestCreatedAt(filter: (e: NostrEvent) => boolean) {
  let max = 0
  for (const e of events.values()) if (filter(e) && e.created_at > max) max = e.created_at
  return max
}
