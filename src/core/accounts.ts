// Several accounts on this browser (board home/accounts; the lead's limits for the web, 2026-11-18):
// - on this belfry: real switches. Each account has its OWN IndexedDB (its session and so its own
//   client key, its events, its outbox, its blobs) and its own copy of this app's localStorage
//   keys; only one is live at a time. Switching saves the live keys of the one leaving, puts back
//   those of the one coming, and reloads the page: nothing of one account is ever in memory or on
//   screen for another, and an outbox item queued by A can only be sent by A's session.
// - other belfries: rows that open https://chat.<domain> (its domain, name and picture only).
// Only one account is connected at a time: no background connection, no dots for the others.
import { openDB } from 'idb'
import { BELFRY } from '../config'

export interface Account {
  id: string
  /** its IndexedDB: the first account keeps the original name, so nothing moves for it */
  db: string
  pubkey?: string
  name?: string
  picture?: string
  email?: string
  /** its role or "Guest", as the row's second line */
  label?: string
}
export interface OtherBelfry { domain: string; name?: string; picture?: string }

const REG = 'belfry-accounts'
const ACTIVE = 'belfry-account'
const OTHERS = 'belfry-other-belfries'
const SNAP = 'belfry-accounts-ls:' // + id: the keys of an account that isn't live
const LEGACY_DB = 'osv-village'
/** Kept for the whole device, never swapped: which backend, the belfry's look and name, dev mode. */
// (belfry-enc-format: the belfry's encryption format belongs to the belfry, not to an account)
const DEVICE = new Set([REG, ACTIVE, OTHERS, 'osv-backend', 'osv-branding', 'belfry-community-name', 'osv-dev-mode', 'belfry-enc-format'])
const mine = (k: string) => /^(belfry|osv)[-:]/.test(k) && !DEVICE.has(k) && !k.startsWith(SNAP)

const read = <T,>(k: string, d: T): T => { try { return JSON.parse(localStorage.getItem(k) ?? 'null') ?? d } catch { return d } }
const write = (k: string, v: unknown) => { try { localStorage.setItem(k, JSON.stringify(v)) } catch { /* private mode */ } }

export const accounts = (): Account[] => read<Account[]>(REG, [])
export const otherBelfries = (): OtherBelfry[] => read<OtherBelfry[]>(OTHERS, [])
export const activeId = (): string => { try { return localStorage.getItem(ACTIVE) ?? 'a0' } catch { return 'a0' } }
export const active = (): Account => accounts().find((a) => a.id === activeId()) ?? { id: activeId(), db: activeId() === 'a0' ? LEGACY_DB : `${LEGACY_DB}-${activeId()}` }
/** The IndexedDB of the live account (db.ts opens this one). */
export const dbName = () => active().db

/** The live account, once signed in: who it is, for its row. Returns another account of this
 *  browser that is the same person (signed in twice), if any. */
export function noteAccount(patch: Omit<Account, 'id' | 'db'>): Account | undefined {
  const cur = active()
  const clean = Object.fromEntries(Object.entries(patch).filter(([, v]) => v !== undefined && v !== ''))
  const list = accounts()
  const i = list.findIndex((a) => a.id === cur.id)
  const next: Account = { ...cur, ...clean }
  if (i >= 0) list[i] = next; else list.push(next)
  write(REG, list)
  setActive(cur.id)
  return list.find((a) => a.id !== cur.id && !!a.pubkey && a.pubkey === next.pubkey)
}

function snapshot(id: string) {
  const out: Record<string, string> = {}
  try { for (const k of Object.keys(localStorage)) if (mine(k)) out[k] = localStorage.getItem(k) ?? '' } catch { /* private mode */ }
  write(SNAP + id, out)
}
function clearLive() {
  for (const store of [localStorage, sessionStorage]) {
    try { for (const k of Object.keys(store)) if (mine(k)) store.removeItem(k) } catch { /* private mode */ }
  }
}
function restore(id: string) {
  const snap = read<Record<string, string>>(SNAP + id, {})
  try { for (const [k, v] of Object.entries(snap)) localStorage.setItem(k, v); localStorage.removeItem(SNAP + id) } catch { /* private mode */ }
}
function setActive(id: string) { try { localStorage.setItem(ACTIVE, id) } catch { /* private mode */ } }

// another tab of this app on the old account would write into the new one's keys: it reloads too
const chan = typeof BroadcastChannel !== 'undefined' ? new BroadcastChannel('belfry-accounts') : null
if (chan) chan.onmessage = () => location.reload()
const reload = () => { chan?.postMessage({ active: activeId() }); location.hash = ''; location.reload() }

/** Switch to another account of this belfry: its keys back, a full reload. */
export function switchTo(id: string) {
  if (id === activeId() || !accounts().some((a) => a.id === id)) return
  snapshot(activeId())
  clearLive()
  restore(id)
  setActive(id)
  reload()
}

/** A new, empty account slot (its own IndexedDB): the reload shows this belfry's sign-in. No
 *  pending invite, guest note or draft of the one before comes along. */
export function addAccount() {
  const cur = active()
  if (!accounts().some((a) => a.id === cur.id)) write(REG, [...accounts(), cur])
  snapshot(cur.id)
  clearLive()
  const id = 'a' + Date.now().toString(36)
  write(REG, [...accounts(), { id, db: `${LEGACY_DB}-${id}` }])
  setActive(id)
  reload()
}

const dropDb = (name: string) => new Promise<void>((r) => { try { const q = indexedDB.deleteDatabase(name); q.onsuccess = q.onerror = q.onblocked = () => r() } catch { r() } })

/** After signing the live account out (its session revoked by the caller): its database and keys
 *  go; another account of this browser opens if there is one. Returns whether one did. */
export async function dropActive(): Promise<boolean> {
  const cur = active()
  const rest = accounts().filter((a) => a.id !== cur.id)
  clearLive()
  try { localStorage.removeItem(SNAP + cur.id) } catch { /* private mode */ }
  write(REG, rest)
  await dropDb(cur.db)
  const next = rest.find((a) => a.pubkey) ?? rest[0]
  if (next) { restore(next.id); setActive(next.id); chan?.postMessage({ active: next.id }); return true }
  try { localStorage.removeItem(ACTIVE) } catch { /* private mode */ }
  return false
}

/** What account A's own database keeps under KEY (its session, for "Forget this device"). */
export async function readOf<T>(a: Account, key: string): Promise<T | undefined> {
  try {
    const d = await openDB(a.db, 1, { upgrade(x) { for (const n of ['events', 'outbox', 'blobs', 'kv']) x.createObjectStore(n, n === 'events' ? { keyPath: 'id' } : n === 'outbox' ? { keyPath: 'localId' } : undefined) } })
    try { return (await d.get('kv', key)) as T | undefined } finally { d.close() }
  } catch { return undefined }
}
/** Every account of this browser (the live one too, even before it was noted). */
export const allAccounts = (): Account[] => { const l = accounts(); return l.some((a) => a.id === activeId()) ? l : [active(), ...l] }

/** "Forget this device": every account, every belfry row, every key (the caller ends each
 *  account's session first). */
export async function forgetAll() {
  const dbs = new Set([LEGACY_DB, ...accounts().map((a) => a.db)])
  for (const store of [localStorage, sessionStorage]) {
    try { for (const k of Object.keys(store)) if (/^(belfry|osv)[-:]/.test(k) && k !== 'osv-backend') store.removeItem(k) } catch { /* private mode */ }
  }
  for (const d of dbs) await dropDb(d)
  chan?.postMessage({ active: 'gone' })
}

/** Leave the live account slot for account ID and throw the slot away: one nobody signed in to
 *  (Add an account, then back), or a second slot of the same person (signed in twice). */
export function discardActiveFor(id: string) {
  const cur = active()
  if (cur.id === id || !accounts().some((a) => a.id === id)) return
  write(REG, accounts().filter((a) => a.id !== cur.id))
  void dropDb(cur.db)
  clearLive()
  restore(id)
  setActive(id)
  reload()
}

// ---- other belfries ---------------------------------------------------------------------------

/** "belfry.example.org", "https://chat.example.org/…" → "example.org"; null when it isn't a domain. */
export function domainOf(input: string): string | null {
  let s = input.trim().toLowerCase().replace(/^https?:\/\//, '').replace(/[/?#].*$/, '').replace(/\.$/, '')
  s = s.replace(/^(chat|belfry)\./, '')
  if (s.length > 253 || !/^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$/.test(s)) return null
  return s
}

/** Another belfry by its address: listed only when its /.well-known/belfry.json answers. */
export async function addOtherBelfry(input: string): Promise<OtherBelfry> {
  const domain = domainOf(input)
  if (!domain) throw new Error('That isn’t a belfry’s address, like example.org.')
  if (domain === BELFRY.domain) throw new Error('That is this belfry: add another account here instead.')
  let j: Record<string, unknown>
  try {
    const r = await fetch(`https://${domain}/.well-known/belfry.json`, { signal: AbortSignal.timeout(8000), credentials: 'omit' })
    if (!r.ok) throw new Error()
    j = await r.json()
  } catch { throw new Error(`No belfry answers at ${domain}.`) }
  const str = (v: unknown, max: number) => (typeof v === 'string' ? v.slice(0, max) : undefined)
  if (j.domain !== domain) throw new Error(`No belfry answers at ${domain}.`)
  const pic = str(j.picture ?? j.logo, 500)
  const b: OtherBelfry = { domain, name: str(j.name, 80), picture: pic && /^https:\/\//.test(pic) ? pic : undefined }
  write(OTHERS, [...otherBelfries().filter((x) => x.domain !== domain), b])
  return b
}
export const removeOtherBelfry = (domain: string) => write(OTHERS, otherBelfries().filter((x) => x.domain !== domain))
