import { openDB, type IDBPDatabase } from 'idb'
import type { NostrEvent } from 'nostr-tools'

// Everything the app knows lives in IndexedDB first. The UI reads from memory
// (loaded from here at start), writes go here before they go anywhere else.

export type Target = 'groups' | 'main'

export interface Attachment {
  blobKey: string // key in the `blobs` store
  name: string
  mime: string
  size: number
  width?: number
  height?: number
}

export interface OutboxItem {
  localId: string
  target: Target
  kind: number
  content: string
  tags: string[][]
  created_at: number
  attachments: Attachment[]
  status: 'queued' | 'failed'
  error?: string
  tries: number
  /** An event signed already (a gift wrap, by its one-time key): published as is. */
  signed?: NostrEvent
  /** The direct message or e-mail it carries (dm.ts), told when it is out. */
  dm?: string
}

type Schema = {
  events: { key: string; value: NostrEvent }
  outbox: { key: string; value: OutboxItem }
  blobs: { key: string; value: Blob }
  kv: { key: string; value: unknown }
}

let dbp: Promise<IDBPDatabase<Schema>> | null = null

export function db() {
  if (!dbp) {
    dbp = openDB<Schema>('osv-village', 1, {
      upgrade(d) {
        d.createObjectStore('events', { keyPath: 'id' })
        d.createObjectStore('outbox', { keyPath: 'localId' })
        d.createObjectStore('blobs')
        d.createObjectStore('kv')
      },
    })
  }
  return dbp
}

export async function kvGet<T>(key: string): Promise<T | undefined> {
  return (await (await db()).get('kv', key)) as T | undefined
}

export async function kvSet(key: string, value: unknown) {
  await (await db()).put('kv', value, key)
}

export async function kvDel(key: string) {
  await (await db()).delete('kv', key)
}

export async function wipe() {
  const d = await db()
  await Promise.all((['events', 'outbox', 'blobs', 'kv'] as const).map((s) => d.clear(s)))
}
