import { openDB, type IDBPDatabase } from 'idb'
import type { NostrEvent } from 'nostr-tools'
import { dbName } from './accounts'

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
  /** Not an event to publish but a call to the belfry's API (POST BELFRY.api + path, NIP-98
   *  signed when it is sent). Its kind and tags still show in the app while it waits (a 9002:
   *  the channel's new name at once); what the belfry publishes comes back from the relay. */
  http?: { path: string; body: unknown }
  /** When it was first saved (ms): the UI's "Still sending…" after 5 s, "Not sent yet" after 30 s. */
  queuedAt?: number
  /** The conversation a direct message belongs to: its order is kept within it (sync.ts). */
  conv?: string
  /** An encrypted file to upload before this item is published (a kind 15's ciphertext, in
   *  'blobs' under BLOBKEY): the upload is a step of the item; done once the storage took it. */
  upload?: { blobKey: string; size: number; done?: boolean }
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
    dbp = openDB<Schema>(dbName(), 1, { // the live account's own database (accounts.ts)
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
