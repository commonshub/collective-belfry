// The charter (iOS SPEC 19.32, the owner's canvas "Charter", 2026-11-16): a short list of
// agreements every person ticks for themselves. It reuses the to-do list of one NIP-29 group so
// no new kind is needed.
//  - Where: the group `charter` on the groups relay, made by the steward who writes the first
//    agreement (9007, then 9002 name "Charter", open). Never listed as a channel or a group.
//  - An agreement = kind 2100 in that group, content = its title (≤ 300), tags = the to-do list's
//    ["t", listIdFor("charter")], ["h","charter"], ["each","1"], ["order","<int>"], optional
//    ["about","<one line ≤ 200>"]. Only a belfry admin's 2100 with each = 1 counts.
//  - A change = 2101 ["e",id],["action","edit"] by a belfry admin: content = the full title, the
//    full about / order tags (latest wins; a missing about empties it; a missing order keeps the
//    place). A changed title or explanation makes that edit's time the agreement's text time.
//    Removal = 2101 action delete by a belfry admin.
//  - A person's agreement = their own 2101 done (undone takes it back), latest per author wins;
//    agreed when it is done and not older than the text time; a done before it is stale: that
//    person is asked again, for that rule only (the owner's decision). A new or changed rule never
//    stops anyone from posting: Home reminds them.
//  - Order: order ascending (none last), then the 2100's created_at, then id.
import { BELFRY } from '../config'
import { belfryAdminKeys } from './access'
import { group, listIdFor } from './model'
import { isBelfryAdmin } from './people'
import { CHARTER_ID } from './places'
import { all, touch, type Ev } from './store'
import { enqueue, isLocalOnly, me } from './sync'

export interface Agreement {
  id: string
  title: string
  about: string
  order: number
  author: string
  addedAt: number
  /** When its text (title or explanation) last changed: its 2100, or a steward's edit. */
  textAt: number
  /** People whose latest tick is done and not older than textAt → when. */
  agreed: Map<string, number>
  /** People who had agreed before the text changed (they are asked again). */
  stale: Set<string>
  pending: boolean
}

const tag = (e: Ev, k: string) => e.tags.find((t) => t[0] === k)?.[1]
const byTime = (a: Ev, b: Ev) => a.created_at - b.created_at || (a.id < b.id ? -1 : 1)
const units = (s: string, n: number) => s.slice(0, n)
const NONE = Number.MAX_SAFE_INTEGER

/** The charter's agreements from EVENTS, ADMINS = the belfry's admins' keys (pure; tested against
 *  the iOS vector vectors/charter.json). */
export function parseCharter(events: Ev[], admins: (pk: string) => boolean): Agreement[] {
  const listId = listIdFor(CHARTER_ID)
  const evs = events.filter((e) => (e.kind === 2100 || e.kind === 2101) && tag(e, 't') === listId && tag(e, 'h') === CHARTER_ID).sort(byTime)
  const rules = new Map<string, Agreement>()
  const ticks = new Map<string, Map<string, { on: boolean; at: number }>>()
  for (const e of evs) {
    if (e.kind === 2100) {
      if (tag(e, 'each') !== '1' || !admins(e.pubkey) || rules.has(e.id)) continue
      const title = units(e.content.trim(), 300)
      if (!title) continue
      const o = Number.parseInt(tag(e, 'order') ?? '', 10)
      rules.set(e.id, { id: e.id, title, about: units((tag(e, 'about') ?? '').trim(), 200), order: Number.isFinite(o) ? o : NONE, author: e.pubkey, addedAt: e.created_at, textAt: e.created_at, agreed: new Map(), stale: new Set(), pending: !!e.pending })
      continue
    }
    const r = rules.get(tag(e, 'e') ?? '')
    if (!r) continue
    const a = tag(e, 'action') ?? ''
    if (a === 'edit') {
      if (!admins(e.pubkey)) continue
      const title = units(e.content.trim(), 300)
      const about = units((tag(e, 'about') ?? '').trim(), 200)
      if (title && (title !== r.title || about !== r.about)) { r.title = title; r.about = about; r.textAt = e.created_at }
      const o = Number.parseInt(tag(e, 'order') ?? '', 10)
      if (Number.isFinite(o)) r.order = o
    } else if (a === 'delete') {
      if (admins(e.pubkey)) { rules.delete(r.id); ticks.delete(r.id) }
    } else if (a === 'done' || a === 'undone') {
      let m = ticks.get(r.id)
      if (!m) ticks.set(r.id, (m = new Map()))
      m.set(e.pubkey, { on: a === 'done', at: e.created_at })
    }
  }
  const out = [...rules.values()]
  for (const r of out) for (const [pk, t] of ticks.get(r.id) ?? []) if (t.on) { if (t.at >= r.textAt) r.agreed.set(pk, t.at); else r.stale.add(pk) }
  return out.sort((a, b) => a.order - b.order || a.addedAt - b.addedAt || (a.id < b.id ? -1 : 1))
}

let cacheList: Ev[] | undefined, cacheAdmins = '', cache: Agreement[] = []
/** The agreements on this device, in the stewards' order. */
export function charter(): Agreement[] {
  const evs = all(me()) // a new array whenever the log changes
  const admins = belfryAdminKeys().join(',')
  if (evs !== cacheList || admins !== cacheAdmins) { cache = parseCharter(evs, isBelfryAdmin); cacheList = evs; cacheAdmins = admins }
  return cache
}
/** The charter's group exists on this device (a steward made it). */
export const charterExists = () => !!group(CHARTER_ID)?.known || charter().length > 0
/** The rules I haven't agreed to (or agreed to before their text changed). */
export const toAgree = (pk = me()) => charter().filter((r) => !r.agreed.has(pk))
/** When I agreed to the last rule I ticked, while I have agreed to all of them. */
export function agreedAt(pk = me()): number | undefined {
  const list = charter()
  if (!list.length || list.some((r) => !r.agreed.has(pk))) return undefined
  return Math.max(...list.map((r) => r.agreed.get(pk)!))
}
export const isStale = (r: Agreement, pk = me()) => r.stale.has(pk) && !r.agreed.has(pk)

/** The charter's to-do list id: sha256hex("osv-village:charter")[:16] = d93ecde52eff27c7. */
export const listIdForCharter = () => listIdFor(CHARTER_ID)
const tags = () => [['t', listIdFor(CHARTER_ID)], ['h', CHARTER_ID], ['each', '1']]
const aboutTag = (about: string) => { const a = units(about.trim(), 200); return a ? [['about', a]] : [] }

/** A steward writes an agreement; the first one makes the charter's group. */
export async function addAgreement(title: string, about: string) {
  if (!charterExists()) {
    await enqueue('groups', { kind: 9007, tags: [['h', CHARTER_ID]] })
    await enqueue('groups', { kind: 9002, tags: [['h', CHARTER_ID], ['name', 'Charter'], ['about', 'The agreements everyone here ticks.'], ['open']] })
  }
  const order = Math.max(0, ...charter().map((r) => (r.order === NONE ? 0 : r.order + 1)))
  await enqueue('groups', { kind: 2100, content: units(title.trim(), 300), tags: [...tags(), ...aboutTag(about), ['order', String(order)]] })
}
/** A steward's change: the full text, explanation and place (latest wins). */
export const editAgreement = (r: Agreement, title: string, about: string, order = r.order) =>
  enqueue('groups', { kind: 2101, content: units(title.trim(), 300), tags: [['e', r.id], ['action', 'edit'], ...tags(), ...aboutTag(about), ...(order === NONE ? [] : [['order', String(order)]])] })
export const removeAgreement = (r: Agreement) => enqueue('groups', { kind: 2101, content: '', tags: [['e', r.id], ['action', 'delete'], ...tags()] })
/** My own tick (or taking it back). */
export const agree = (r: Agreement, on: boolean) => enqueue('groups', { kind: 2101, content: '', tags: [['e', r.id], ['action', on ? 'done' : 'undone'], ...tags()] })
/** Move R up or down one place: every rule gets its place written once (0, 1, 2…). */
export async function moveAgreement(r: Agreement, by: -1 | 1) {
  const list = charter()
  const i = list.findIndex((x) => x.id === r.id)
  const j = i + by
  if (i < 0 || j < 0 || j >= list.length) return
  const next = [...list]
  ;[next[i], next[j]] = [next[j], next[i]]
  for (let k = 0; k < next.length; k++) if (next[k].order !== k) await editAgreement(next[k], next[k].title, next[k].about, k)
}

/** Whether the belfry says it has a charter (belfry.json `charter`), for people who can't read
 *  the group (guests): undefined when the belfry doesn't say. */
let remoteCharter: { agreements: RemoteAgreement[] } | null = null
export interface RemoteAgreement { id: string; title: string; about: string; order: number }
/** Guests and applicants read the charter from the belfry (GET /api/v1/charter: it reads the group
 *  for them). Demo: the device's own. */
export async function loadRemoteCharter(): Promise<RemoteAgreement[]> {
  if (isLocalOnly()) { remoteCharter = { agreements: charter().map((r) => ({ id: r.id, title: r.title, about: r.about, order: r.order })) }; touch(); return remoteCharter.agreements }
  try {
    const r = await fetch(`${BELFRY.api}/charter`)
    if (!r.ok) return remoteCharter?.agreements ?? []
    const d = (await r.json()) as { agreements?: unknown }
    const list = (Array.isArray(d.agreements) ? d.agreements : []).filter((a): a is RemoteAgreement => !!a && typeof a === 'object' && typeof (a as RemoteAgreement).id === 'string' && typeof (a as RemoteAgreement).title === 'string')
      .map((a) => ({ id: a.id, title: String(a.title).slice(0, 300), about: String(a.about ?? '').slice(0, 200), order: Number(a.order) || 0 }))
    remoteCharter = { agreements: list }
    touch()
    return list
  } catch { return remoteCharter?.agreements ?? [] }
}
export const remoteAgreements = () => remoteCharter?.agreements
