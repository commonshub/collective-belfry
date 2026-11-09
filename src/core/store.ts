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

// All events, with queued ones shown as if they were already published.
let cache: { v: number; list: Ev[] } = { v: -1, list: [] }
export function all(me?: string): Ev[] {
  if (cache.v === version) return cache.list
  const list: Ev[] = [...events.values()]
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
  cache = { v: version, list }
  return list
}

export function latestCreatedAt(filter: (e: NostrEvent) => boolean) {
  let max = 0
  for (const e of events.values()) if (filter(e) && e.created_at > max) max = e.created_at
  return max
}
