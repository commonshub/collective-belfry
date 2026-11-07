import { sha256 } from '@noble/hashes/sha256'
import { bytesToHex } from '@noble/hashes/utils'
import { nip19 } from 'nostr-tools'
import { LABEL_NS } from '../config'
import { all, type Ev } from './store'
import { me } from './sync'

// Derived views over the event log. Pure functions of all(); cheap enough to
// recompute per render for a village-sized dataset.

const tag = (e: Ev, k: string) => e.tags.find((t) => t[0] === k)?.[1]
const byTime = (a: Ev, b: Ev) => a.created_at - b.created_at || (a.id < b.id ? -1 : 1)

function deleted(list: Ev[]) {
  const out = new Set<string>()
  const authors = new Map(list.map((e) => [e.id, e.pubkey]))
  for (const e of list) {
    if (e.kind !== 5) continue
    for (const t of e.tags) {
      if (t[0] === 'e' && authors.get(t[1]) === e.pubkey) out.add(t[1])
      if (t[0] === 'a' && t[1]?.split(':')[1] === e.pubkey) out.add(t[1])
    }
  }
  return out
}

// ---- profiles ---------------------------------------------------------------

export interface Profile { name?: string; picture?: string; nip05?: string }

export function profiles(): Map<string, Profile> {
  const out = new Map<string, Profile & { at: number }>()
  for (const e of all(me())) {
    if (e.kind !== 0) continue
    const prev = out.get(e.pubkey)
    if (prev && prev.at >= e.created_at) continue
    try {
      const p = JSON.parse(e.content)
      out.set(e.pubkey, { name: p.display_name || p.name, picture: p.picture, nip05: p.nip05, at: e.created_at })
    } catch { /* bad profile json */ }
  }
  return out
}

export function displayName(pubkey: string, ps = profiles()) {
  if (pubkey === me()) return 'You'
  const p = ps.get(pubkey)
  return p?.name || p?.nip05?.split('@')[0] || nip19.npubEncode(pubkey).slice(0, 12) + '…'
}

// ---- groups (NIP-29) --------------------------------------------------------

export interface Group {
  id: string
  name: string
  about: string
  picture?: string
  createdAt: number
  lastActivity: number
  members: Set<string>
  isEvent: boolean
  /** A working group: a channel with a parent (where it was posted), members, events, sub-pods. */
  isPod: boolean
  /** For a pod: the channel or pod it lives in ('general' when unknown). */
  parent?: string
  pending: boolean
}

export type ChannelType = 'channel' | 'event' | 'pod'
export const channelType = (g?: Group): ChannelType => (g?.isEvent ? 'event' : g?.isPod ? 'pod' : 'channel')
export const channelIcon = (g?: Group) => (g?.isEvent ? '📅' : g?.isPod ? '⬡' : '#')
export const isPodId = (id: string) => id.startsWith('pod-')
/** The pod a chat post announces (its `pod` tag), if any. */
export const podOf = (e: Ev) => {
  const v = tag(e, 'pod')
  return v && isPodId(v) ? v : undefined
}

export function groups(): Group[] {
  const list = all(me())
  const map = new Map<string, Group & { metaAt: Record<string, number> }>()
  const get = (id: string, at: number) => {
    let g = map.get(id)
    if (!g) {
      g = { id, name: id, about: '', createdAt: at, lastActivity: at, members: new Set(), isEvent: id.startsWith('ws-') || id.startsWith('ev-'), isPod: isPodId(id), pending: false, metaAt: {} }
      map.set(id, g)
    }
    return g
  }
  for (const e of [...list].sort(byTime)) {
    const id = e.kind >= 39000 && e.kind <= 39003 ? tag(e, 'd') : tag(e, 'h')
    if (!id) continue
    const g = get(id, e.created_at)
    if (e.kind === 9007) {
      g.createdAt = Math.min(g.createdAt, e.created_at)
      g.pending ||= !!e.pending
    }
    if (e.kind === 9007 || e.kind === 9002 || e.kind === 39000) {
      for (const k of ['name', 'about', 'picture'] as const) {
        const v = tag(e, k)
        if (v === undefined || (g.metaAt[k] ?? 0) > e.created_at) continue
        g[k] = v
        g.metaAt[k] = e.created_at
      }
    }
    if (e.kind === 39002) g.members = new Set(e.tags.filter((t) => t[0] === 'p').map((t) => t[1]))
    if (e.kind === 9) {
      g.lastActivity = Math.max(g.lastActivity, e.created_at)
      g.members.add(e.pubkey)
    }
    // join / leave (NIP-29 9021 / 9022): counted here too, whether or not the relay folds them in
    if (e.kind === 9021) g.members.add(e.pubkey)
    if (e.kind === 9022) g.members.delete(e.pubkey)
  }
  // a pod lives where it was announced: the first post carrying its `pod` tag
  for (const e of [...list].sort(byTime)) {
    const pod = e.kind === 9 ? podOf(e) : undefined
    const h = tag(e, 'h')
    const g = pod ? map.get(pod) : undefined
    if (g && h && h !== pod && !g.parent) g.parent = h
  }
  for (const g of map.values()) if (g.isPod && !g.parent) g.parent = 'general'
  const gone = new Set([...map.values()].filter((g) => list.some((e) => e.kind === 9008 && tag(e, 'h') === g.id)).map((g) => g.id))
  return [...map.values()]
    .filter((g) => !gone.has(g.id))
    .sort((a, b) => (a.id === 'general' ? -1 : b.id === 'general' ? 1 : b.lastActivity - a.lastActivity))
}

export const group = (id: string) => groups().find((g) => g.id === id)

// ---- messages -----------------------------------------------------------------

export interface Media { url: string; mime: string; name?: string; w?: number; h?: number }
export interface Message { ev: Ev; text: string; media: Media[] }

export function parseMedia(e: Ev): Message {
  const media: Media[] = []
  for (const t of e.tags) {
    if (t[0] !== 'imeta') continue
    const f = Object.fromEntries(t.slice(1).map((p) => [p.slice(0, p.indexOf(' ')), p.slice(p.indexOf(' ') + 1)]))
    if (!f.url || !/^(https?:|idb:)/.test(f.url)) continue
    const [w, h] = (f.dim ?? '').split('x').map(Number)
    media.push({ url: f.url, mime: f.m ?? '', name: f.alt, w: w || undefined, h: h || undefined })
  }
  let text = e.content
  for (const m of media) text = text.split(m.url).join('')
  return { ev: e, text: text.trim(), media }
}

export function messages(groupId: string): Message[] {
  const list = all(me())
  const del = deleted(list)
  return list
    .filter((e) => e.kind === 9 && tag(e, 'h') === groupId && !del.has(e.id))
    .sort(byTime)
    .map(parseMedia)
}

// ---- reactions (NIP-25, kind 7 in the group: e = the message, content = the emoji) -----

export interface Reaction { emoji: string; who: Set<string>; mine?: Ev }

/** Per message id: its reactions, most given first; `mine` is your reaction event (to undo it). */
export function reactions(groupId: string): Map<string, Reaction[]> {
  const list = all(me())
  const del = deleted(list)
  const byMsg = new Map<string, Map<string, Reaction>>()
  for (const e of list.filter((x) => x.kind === 7 && tag(x, 'h') === groupId && !del.has(x.id)).sort(byTime)) {
    const target = e.tags.filter((t) => t[0] === 'e').at(-1)?.[1]
    const emoji = (e.content || '+').trim() === '+' ? '👍' : e.content.trim()
    if (!target || !emoji || emoji.length > 16) continue
    const m = byMsg.get(target) ?? new Map<string, Reaction>()
    const r = m.get(emoji) ?? { emoji, who: new Set<string>() }
    r.who.add(e.pubkey)
    if (e.pubkey === me()) r.mine = e
    m.set(emoji, r)
    byMsg.set(target, m)
  }
  return new Map([...byMsg].map(([id, m]) => [id, [...m.values()].sort((a, b) => b.who.size - a.who.size)]))
}

// ---- member labels (NIP-32, one kind 1985 per member, latest wins) ------------

export function memberLabels(groupId: string): Map<string, string[]> {
  const latest = new Map<string, Ev>()
  for (const e of all(me())) {
    if (e.kind !== 1985 || tag(e, 'h') !== groupId || tag(e, 'L') !== LABEL_NS) continue
    const p = tag(e, 'p')
    if (!p) continue
    const prev = latest.get(p)
    if (!prev || byTime(prev, e) < 0) latest.set(p, e)
  }
  const out = new Map<string, string[]>()
  for (const [p, e] of latest) out.set(p, e.tags.filter((t) => t[0] === 'l' && t[2] === LABEL_NS).map((t) => t[1]))
  return out
}

// ---- tasklist (tasklist.sh protocol, kinds 2100-2103) --------------------------

export const listIdFor = (groupId: string) => bytesToHex(sha256('osv-village:' + groupId)).slice(0, 16)

export interface Task {
  id: string
  title: string
  author: string
  assignee?: string
  done: boolean
  pending: boolean
  comments: { author: string; text: string; at: number }[]
}

export function tasklist(groupId: string): { name?: string; tasks: Task[] } {
  const listId = listIdFor(groupId)
  const evs = all(me()).filter((e) => e.kind >= 2100 && e.kind <= 2103 && tag(e, 't') === listId).sort(byTime)
  const tasks = new Map<string, Task>()
  let name: string | undefined
  for (const e of evs) {
    if (e.kind === 2100) {
      const title = e.content.trim().slice(0, 300)
      if (title) tasks.set(e.id, { id: e.id, title, author: e.pubkey, done: false, pending: !!e.pending, comments: [] })
    } else if (e.kind === 2102) {
      name = e.content.slice(0, 48)
    } else {
      const t = tasks.get(tag(e, 'e') ?? '')
      if (!t) continue
      if (e.kind === 2103) t.comments.push({ author: e.pubkey, text: e.content.slice(0, 2000), at: e.created_at })
      const a = tag(e, 'action')
      if (a === 'claim') t.assignee = e.pubkey
      if (a === 'unclaim' && t.assignee === e.pubkey) t.assignee = undefined
      if (a === 'done') t.done = true
      if (a === 'undone') t.done = false
      if (a === 'delete' && e.pubkey === t.author) tasks.delete(t.id)
    }
  }
  return { name, tasks: [...tasks.values()] }
}

// ---- calendar (NIP-52) ----------------------------------------------------------

export type Rsvp = 'accepted' | 'tentative' | 'declined'

export interface CalEvent {
  coord: string // kind:pubkey:d
  d: string
  ev: Ev
  title: string
  description: string
  location?: string
  allDay: boolean
  start: Date
  end?: Date
  groupId: string
  /** The channel it was posted in (a sub-channel of it): where the post linking to it is. */
  channel: string
  rsvps: Map<string, Rsvp>
}

export const isEventKind = (k: number) => k === 31922 || k === 31923

/** The event a chat post links to (its `a` tag), if any. */
export const eventCoordOf = (e: Ev) => {
  const a = tag(e, 'a')
  return a && isEventKind(Number(a.split(':')[0])) ? a : undefined
}

export function calendar(): CalEvent[] {
  const list = all(me())
  const del = deleted(list)
  const latest = new Map<string, Ev>()
  for (const e of list) {
    if (e.kind !== 31922 && e.kind !== 31923) continue
    const d = tag(e, 'd')
    if (!d) continue
    const coord = `${e.kind}:${e.pubkey}:${d}`
    const prev = latest.get(coord)
    if (!prev || byTime(prev, e) < 0) latest.set(coord, e)
  }
  const rsvps = new Map<string, Map<string, { s: Rsvp; at: number }>>()
  for (const e of list) {
    if (e.kind !== 31925) continue
    const a = tag(e, 'a')
    const s = tag(e, 'status') as Rsvp | undefined
    if (!a || !s) continue
    const m = rsvps.get(a) ?? new Map()
    const prev = m.get(e.pubkey)
    if (!prev || prev.at <= e.created_at) m.set(e.pubkey, { s, at: e.created_at })
    rsvps.set(a, m)
  }
  // where each event was posted: the first post linking to it outside the event's own chat
  const posted = new Map<string, string>()
  for (const e of [...list].sort(byTime)) {
    if (e.kind !== 9 || del.has(e.id)) continue
    const a = eventCoordOf(e)
    const h = tag(e, 'h')
    if (a && h && !h.startsWith('ws-') && !posted.has(a)) posted.set(a, h)
  }
  const out: CalEvent[] = []
  for (const [coord, e] of latest) {
    if (del.has(coord)) continue
    const d = tag(e, 'd')!
    const allDay = e.kind === 31922
    const parse = (v?: string) => (!v ? undefined : allDay ? new Date(v + 'T00:00:00') : new Date(Number(v) * 1000))
    const start = parse(tag(e, 'start'))
    if (!start || isNaN(+start)) continue
    out.push({
      coord, d, ev: e,
      title: tag(e, 'title') || tag(e, 'name') || 'Untitled',
      description: e.content,
      location: tag(e, 'location'),
      allDay, start, end: parse(tag(e, 'end')),
      groupId: tag(e, 'h') || 'ws-' + d.replace(/^session-/, ''),
      channel: posted.get(coord) ?? 'general',
      rsvps: new Map([...(rsvps.get(coord) ?? new Map()).entries()].map(([p, v]) => [p, v.s])),
    })
  }
  return out.sort((a, b) => +a.start - +b.start)
}
