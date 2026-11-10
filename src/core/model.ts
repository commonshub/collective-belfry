import { sha256 } from '@noble/hashes/sha256'
import { bytesToHex } from '@noble/hashes/utils'
import { nip19 } from 'nostr-tools'
import { BELFRY, LABEL_NS } from '../config'
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
    // kind 9005: a moderator removed it (the relay only takes those from moderators)
    if (e.kind === 9005) for (const t of e.tags) if (t[0] === 'e' && t[1]) out.add(t[1])
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

/** One result per state of the event log. Views ask for profiles() and groups() many times per
 *  render (every avatar, every name): computed once, until the log changes (all() then returns
 *  a new list). Callers only read the result. */
function perLog<T>(compute: (list: Ev[]) => T): () => T {
  let key: Ev[] | undefined
  let val: T
  return () => {
    const list = all(me())
    if (list !== key) { val = compute(list); key = list }
    return val
  }
}

export const profiles: () => Map<string, Profile> = perLog((list) => {
  const out = new Map<string, Profile & { at: number }>()
  const guests: [string, string][] = []
  for (const e of list) {
    // a guest's name, from the event that put them in a channel (guests.py), until a profile
    if (e.kind === 9000 && tag(e, 'name')) { const p = tag(e, 'p'); if (p) guests.push([p, tag(e, 'name')!.slice(0, 100)]) }
    if (e.kind !== 0) continue
    const prev = out.get(e.pubkey)
    if (prev && prev.at >= e.created_at) continue
    try {
      const p = JSON.parse(e.content)
      out.set(e.pubkey, { name: p.display_name || p.name, picture: p.picture, nip05: p.nip05, at: e.created_at })
    } catch { /* bad profile json */ }
  }
  for (const [pk, name] of guests) if (!out.get(pk)?.name) out.set(pk, { ...out.get(pk), name, at: out.get(pk)?.at ?? 0 })
  return out
})

/** The name other people know someone by (never "You"): for e-mail From lines and signatures. */
export function realName(pubkey: string, ps = profiles()) {
  const p = ps.get(pubkey)
  return p?.name || p?.nip05?.split('@')[0] || ''
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
  /** A proposal: a channel holding one NIP-23 text (versioned), a status and a vote. */
  isProposal: boolean
  /** For a pod or proposal: the channel or pod it lives in ('general' when unknown). */
  parent?: string
  /** NIP-29 `closed`: joining takes an application (9021) that an admin admits (9000). */
  closed: boolean
  /** The question applicants answer (our `prompt` tag on the group's 9002). */
  prompt?: string
  admins: Set<string>
  /** Open applications to a closed group: the latest 9021 of someone not admitted nor declined. */
  applications: Application[]
  pending: boolean
}

export interface Application { pubkey: string; answer: string; at: number; ev: Ev }

export type ChannelType = 'channel' | 'event' | 'pod' | 'proposal'
export const channelType = (g?: Group): ChannelType => (g?.isEvent ? 'event' : g?.isPod ? 'pod' : g?.isProposal ? 'proposal' : 'channel')
/** A pod's shape has as many sides as it has members: a triangle up to 3, a square for 4, … the
 *  more people, the closer to a circle (PodShape in icons.tsx draws it; this is the text form). */
export const podSides = (members: number) => Math.max(3, Math.min(members, 12))
const POD_GLYPH: Record<number, string> = { 3: '△', 4: '□', 5: '⬠', 6: '⬡' }
export const channelIcon = (g?: Group) => (g?.isEvent ? '📅' : g?.isPod ? (POD_GLYPH[podSides(g.members.size)] ?? '○') : g?.isProposal ? '📝' : '#')
// ---- channel references: a channel picked after "#" in the composer is written as
// nostr:naddr1… (NIP-19/27: kind 39000, the group's metadata, by the relay's key, d = group id),
// shown as a "#name" chip that opens the channel. A "#word" typed by hand stays a topic.
export const NADDR = /nostr:(naddr1[02-9ac-hj-np-z]{20,400})/g
const metaAuthors: () => Map<string, string> = perLog((list) => {
  const out = new Map<string, string>()
  for (const e of list) if (e.kind === 39000) { const d = e.tags.find((t) => t[0] === 'd')?.[1]; if (d) out.set(d, e.pubkey) }
  return out
})
/** "nostr:naddr1…" for a channel, or undefined when its metadata hasn't been seen. */
export function channelRef(id: string): string | undefined {
  const pubkey = metaAuthors().get(id)
  if (!pubkey) return undefined
  return 'nostr:' + nip19.naddrEncode({ kind: 39000, pubkey, identifier: id, relays: [BELFRY.groupsRelay] })
}
/** The group id an naddr points to, if it is a NIP-29 group's metadata. */
export function channelOfNaddr(naddr: string): string | undefined {
  try { const d = nip19.decode(naddr); return d.type === 'naddr' && d.data.kind === 39000 ? d.data.identifier : undefined } catch { return undefined }
}
/** Text for one-line previews: mentions as "@…", channel references as "#name". */
export function plainRefs(text: string, npub = '@…'): string {
  return text.replace(/nostr:npub1[02-9ac-hj-np-z]+/g, npub).replace(NADDR, (all, a: string) => {
    const id = channelOfNaddr(a)
    return id ? '#' + (groups().find((g) => g.id === id)?.name ?? id) : all
  })
}
export const isPodId = (id: string) => id.startsWith('pod-')
export const isProposalId = (id: string) => id.startsWith('prop-')
/** The pod a chat post announces (its `pod` tag), if any. */
export const podOf = (e: Ev) => {
  const v = tag(e, 'pod')
  return v && isPodId(v) ? v : undefined
}
/** The proposal a chat post announces (its `proposal` tag), if any. */
export const proposalOf = (e: Ev) => {
  const v = tag(e, 'proposal')
  return v && isProposalId(v) ? v : undefined
}

export const groups: () => Group[] = perLog((list) => {
  const map = new Map<string, Group & { metaAt: Record<string, number> }>()
  const applied = new Map<string, Ev>() // group id + pubkey → latest 9021
  const decided = new Map<string, number>() // group id + pubkey → latest 9000 / 9001
  const get = (id: string, at: number) => {
    let g = map.get(id)
    if (!g) {
      g = { id, name: id, about: '', createdAt: at, lastActivity: at, members: new Set(), isEvent: id.startsWith('ws-') || id.startsWith('ev-'), isPod: isPodId(id), isProposal: isProposalId(id), closed: false, admins: new Set(), applications: [], pending: false, metaAt: {} }
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
    if (e.kind === 9007 || e.kind === 9002 || e.kind === 39000) {
      const access = e.tags.some((t) => t[0] === 'closed') ? true : e.tags.some((t) => t[0] === 'open') ? false : undefined
      if (access !== undefined && (g.metaAt.closed ?? 0) <= e.created_at) { g.closed = access; g.metaAt.closed = e.created_at }
      const prompt = tag(e, 'prompt')
      if (prompt !== undefined && (g.metaAt.prompt ?? 0) <= e.created_at) { g.prompt = prompt; g.metaAt.prompt = e.created_at }
    }
    if (e.kind === 9007) g.admins.add(e.pubkey) // its creator administers it (Belfry's relay patch)
    if (e.kind === 39001) for (const t of e.tags) if (t[0] === 'p') g.admins.add(t[1])
    if (e.kind === 39002) g.members = new Set(e.tags.filter((t) => t[0] === 'p').map((t) => t[1]))
    // admitted (9000 put-user) or removed / declined (9001 remove-user) by an admin
    if (e.kind === 9000 || e.kind === 9001) {
      const p = tag(e, 'p')
      if (p) { if (e.kind === 9000) g.members.add(p); else g.members.delete(p); decided.set(id + p, e.created_at) }
    }
    if (e.kind === 9) {
      g.lastActivity = Math.max(g.lastActivity, e.created_at)
      g.members.add(e.pubkey)
    }
    // join / leave (NIP-29 9021 / 9022): counted here too, whether or not the relay folds them in
    if (e.kind === 9021) {
      if (!g.closed) g.members.add(e.pubkey)
      else applied.set(id + e.pubkey, e)
    }
    if (e.kind === 9022) g.members.delete(e.pubkey)
  }
  // a pod lives where it was announced: the first post carrying its `pod` tag
  for (const e of [...list].sort(byTime)) {
    const pod = e.kind === 9 ? podOf(e) ?? proposalOf(e) : undefined
    const h = tag(e, 'h')
    const g = pod ? map.get(pod) : undefined
    if (g && h && h !== pod && !g.parent) g.parent = h
  }
  for (const g of map.values()) if ((g.isPod || g.isProposal) && !g.parent) g.parent = 'general'
  for (const [k, e] of applied) {
    const g = map.get(k.slice(0, -64))
    if (!g || g.members.has(e.pubkey) || (decided.get(k) ?? 0) >= e.created_at) continue
    g.applications.push({ pubkey: e.pubkey, answer: e.content, at: e.created_at, ev: e })
  }
  const gone = new Set([...map.values()].filter((g) => list.some((e) => e.kind === 9008 && tag(e, 'h') === g.id)).map((g) => g.id))
  return [...map.values()]
    .filter((g) => !gone.has(g.id))
    .sort((a, b) => (a.id === 'general' ? -1 : b.id === 'general' ? 1 : b.lastActivity - a.lastActivity))
})

/** An announcement: a message tagged `t announcement` (NIP-24) by one of its channel's admins.
 *  Any member can tag one; only the admins' count (here and in Belfry's e-mails to followers). */
export const ANNOUNCE = 'announcement'
export const isAnnouncement = (m: Message, g?: Group) => m.topics.includes(ANNOUNCE) && !!g?.admins.has(m.ev.pubkey)

/** A drop box link: anyone with it uploads to this channel (NIP-78 kind 30078, `d` drop:<token>,
 *  NIP-40 `expiration`, `login` when uploaders must be signed in). Belfry serves the page. */
export interface DropLink { token: string; groupId: string; expires: number; login: boolean; author: string; ev: Ev }
export function dropLinks(groupId: string): DropLink[] {
  const list = all(me())
  const del = deleted(list)
  const now = Date.now() / 1000
  const out: DropLink[] = []
  for (const e of list) {
    const d = tag(e, 'd') ?? ''
    if (e.kind !== 30078 || !d.startsWith('drop:') || tag(e, 'h') !== groupId) continue
    if (del.has(e.id) || del.has(`30078:${e.pubkey}:${d}`)) continue
    const expires = Number(tag(e, 'expiration') ?? 0)
    if (!(expires > now)) continue
    out.push({ token: d.slice(5), groupId, expires, login: e.tags.some((t) => t[0] === 'login'), author: e.pubkey, ev: e })
  }
  return out.sort((a, b) => b.ev.created_at - a.ev.created_at)
}

export const group = (id: string) => groups().find((g) => g.id === id)

// ---- messages -----------------------------------------------------------------

export interface Media { url: string; mime: string; name?: string; w?: number; h?: number }
export interface Message { ev: Ev; text: string; media: Media[]; topics: string[]; /** when its author last edited it (kind 1010) */ edited?: number }

// ---- topics: hashtags (NIP-24 `t` tags, lowercase) and a channel's pinned ones -------------

export const HASHTAG = /(^|[^\p{L}\p{N}_&/])#([\p{L}\p{N}_][\p{L}\p{N}_-]{0,48})/gu
export const normTopic = (t: string) => t.toLowerCase().replace(/^#/, '')
export const hashtags = (text: string) => [...new Set([...text.matchAll(HASHTAG)].map((m) => normTopic(m[2])))]
export const TOPIC_NS = '#t'

/** A channel's pinned topics: the latest NIP-32 label event in the group with namespace `#t`
 *  (its `l` tags are the topics, in order). Any member may pin or unpin. */
export function topics(groupId: string): string[] {
  let latest: Ev | undefined
  for (const e of all(me())) {
    if (e.kind !== 1985 || tag(e, 'h') !== groupId || tag(e, 'L') !== TOPIC_NS) continue
    if (!latest || byTime(latest, e) < 0) latest = e
  }
  return latest ? latest.tags.filter((t) => t[0] === 'l' && t[2] === TOPIC_NS).map((t) => normTopic(t[1])) : []
}

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
  return { ev: e, text: text.trim(), media, topics: [...new Set(e.tags.filter((t) => t[0] === 't' && t[1]).map((t) => normTopic(t[1])))] }
}

// ---- edits: kind 1010 in the group, `e` = the message; content and tags are the new version.
// Only the author's own edit counts, the latest wins. The message keeps its id (reactions,
// threads and links hang off it); other clients simply show the first version.

export const EDIT_KIND = 1010

function edits(list: Ev[], del: Set<string>): Map<string, Ev> {
  const out = new Map<string, Ev>()
  const authors = new Map(list.map((e) => [e.id, e.pubkey]))
  for (const e of list) {
    if (e.kind !== EDIT_KIND || del.has(e.id)) continue
    const id = tag(e, 'e')
    if (!id || authors.get(id) !== e.pubkey) continue // only its author edits a message
    const had = out.get(id)
    if (!had || had.created_at < e.created_at || (had.created_at === e.created_at && had.id < e.id)) out.set(id, e)
  }
  return out
}

function parsed(list: Ev[], keep: (e: Ev) => boolean): Message[] {
  const del = deleted(list)
  const ed = edits(list, del)
  return list.filter((e) => keep(e) && !del.has(e.id)).sort(byTime).map((e) => {
    const x = ed.get(e.id)
    return x && x.pubkey === e.pubkey ? { ...parseMedia(x), ev: e, edited: x.created_at } : parseMedia(e)
  })
}

export function messages(groupId: string): Message[] {
  return parsed(all(me()), (e) => e.kind === 9 && tag(e, 'h') === groupId)
}

// ---- threads (NIP-22: kind 1111 in the group, `E` = the message it hangs off) -----------
// A thread is a side conversation about one message, like Discord's. It exists once someone
// replies; replies never show in the channel itself.

export const rootOf = (e: Ev) => e.tags.find((t) => t[0] === 'E')?.[1]

export function threadReplies(groupId: string, rootId: string): Message[] {
  return parsed(all(me()), (e) => e.kind === 1111 && tag(e, 'h') === groupId && rootOf(e) === rootId)
}

export interface ThreadInfo { count: number; last: number; who: string[] }

/** Per message id in GROUP: how many replies its thread has, the latest time, who wrote. */
export function threadsOf(groupId: string): Map<string, ThreadInfo> {
  const list = all(me())
  const del = deleted(list)
  const out = new Map<string, ThreadInfo>()
  for (const e of list) {
    if (e.kind !== 1111 || tag(e, 'h') !== groupId || del.has(e.id)) continue
    const root = rootOf(e)
    if (!root) continue
    const t = out.get(root) ?? { count: 0, last: 0, who: [] }
    t.count++
    t.last = Math.max(t.last, e.created_at)
    if (!t.who.includes(e.pubkey)) t.who.push(e.pubkey)
    out.set(root, t)
  }
  return out
}

// ---- shared inboxes: mail from anyone to <channel>@<domain> lands as e-mail cards ---------
// The setting is the group admins' latest NIP-78 event (d = belfry:inbox); a card's state and
// who has it are NIP-32 labels (namespace belfry/inbox) on the card, latest wins.

export const INBOX_D = 'belfry:inbox'
export const INBOX_NS = 'belfry/inbox'

export function isInbox(g: Group | undefined): boolean {
  if (!g) return false
  const own = all(me()).filter((e) => e.kind === 30078 && tag(e, 'd') === INBOX_D && tag(e, 'h') === g.id && g.admins.has(e.pubkey)).sort(byTime)
  try { return !!own.length && !!JSON.parse(own.at(-1)!.content || '{}').on } catch { return false }
}

export type CardStatus = 'open' | 'waiting' | 'closed'
export interface CardState { status: CardStatus; assignee?: string; answered: number }

/** Per e-mail card in GROUP: open / waiting (answered) / closed, and who has it. SENDER = the key
 *  that posts cards and answers (only its "sent" events count as answers). */
export function inboxStates(groupId: string, sender?: string): Map<string, CardState> {
  const list = all(me())
  const del = deleted(list)
  const out = new Map<string, CardState>()
  const get = (id: string) => out.get(id) ?? out.set(id, { status: 'open', answered: 0 }).get(id)!
  for (const e of list.filter((x) => tag(x, 'h') === groupId && !del.has(x.id)).sort(byTime)) {
    const card = e.tags.find((t) => t[0] === 'e')?.[1]
    if (!card) continue
    if (e.kind === 1985 && e.tags.some((t) => t[0] === 'L' && t[1] === INBOX_NS)) {
      const l = e.tags.find((t) => t[0] === 'l' && t[2] === INBOX_NS)?.[1]
      if (l === 'open' || l === 'closed') get(card).status = l
      if (l === 'assigned') get(card).assignee = e.tags.find((t) => t[0] === 'p')?.[1] || undefined
    } else if (e.kind === 9 && sender && e.pubkey === sender && e.tags.some((t) => t[0] === 'client' && t[1] === 'belfry-mail-out')) {
      const c = get(card)
      c.answered++
      if (c.status === 'open') c.status = 'waiting'
    }
  }
  return out
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
  /** The message it was made from ("Turn into a to-do"), its `q` tag. */
  source?: string
}

export function tasklist(groupId: string): { name?: string; tasks: Task[] } {
  const listId = listIdFor(groupId)
  const evs = all(me()).filter((e) => e.kind >= 2100 && e.kind <= 2103 && tag(e, 't') === listId).sort(byTime)
  const tasks = new Map<string, Task>()
  let name: string | undefined
  for (const e of evs) {
    if (e.kind === 2100) {
      const title = e.content.trim().slice(0, 300)
      if (title) tasks.set(e.id, { id: e.id, title, author: e.pubkey, done: false, pending: !!e.pending, comments: [], source: tag(e, 'q') })
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

// ---- pinned messages: a NIP-32 label in the group (namespace belfry/pin, `l` pinned | unpinned,
// `e` = the message), by someone who may administer the channel (MAY says who). The latest label
// per message wins. A pinned message is the channel's standing notice (its PSA).

export const PIN_NS = 'belfry/pin'

export function pinned(groupId: string, may: (pubkey: string) => boolean): Message[] {
  const state = new Map<string, { on: boolean; at: number }>()
  for (const e of all(me())) {
    if (e.kind !== 1985 || tag(e, 'h') !== groupId || tag(e, 'L') !== PIN_NS || !may(e.pubkey)) continue
    const id = tag(e, 'e')
    const l = e.tags.find((t) => t[0] === 'l')?.[1]
    if (!id || (l !== 'pinned' && l !== 'unpinned')) continue
    const had = state.get(id)
    if (!had || had.at <= e.created_at) state.set(id, { on: l === 'pinned', at: e.created_at })
  }
  if (!state.size) return []
  return messages(groupId).filter((m) => state.get(m.ev.id)?.on).sort((a, b) => state.get(b.ev.id)!.at - state.get(a.ev.id)!.at)
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
  /** Online meeting links (NIP-52 `r` tags: video calls, documents). */
  links: string[]
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
      links: e.tags.filter((t) => t[0] === 'r' && /^https:\/\//.test(t[1] ?? '')).map((t) => t[1]),
      allDay, start, end: parse(tag(e, 'end')),
      groupId: tag(e, 'h') || 'ws-' + d.replace(/^session-/, ''),
      channel: posted.get(coord) ?? 'general',
      rsvps: new Map([...(rsvps.get(coord) ?? new Map()).entries()].map(([p, v]) => [p, v.s])),
    })
  }
  return out.sort((a, b) => +a.start - +b.start)
}

// ---- proposals ----------------------------------------------------------------------------
// The text: a NIP-23 long-form event (kind 30023, `t` proposal, `h` its chat, `version` N), on
// the community relay; only its author edits it. The status: the latest NIP-32 label (namespace
// below) in its chat by the author or an admin. The vote: a NIP-88 poll (1068) in its chat,
// answered with 1018 responses (latest per member counts).

export type ProposalStatus = 'draft' | 'feedback' | 'voting' | 'passed' | 'rejected' | 'closed'
export const PROPOSAL_NS = 'belfry/proposal-status'
export const STATUS_LABEL: Record<ProposalStatus, string> = {
  draft: 'Draft', feedback: 'Open for feedback', voting: 'Open for votes', passed: 'Passed', rejected: 'Rejected', closed: 'Closed',
}
export const STATUSES = Object.keys(STATUS_LABEL) as ProposalStatus[]
export const VOTE_OPTIONS = [['yes', 'Yes'], ['no', 'No'], ['abstain', 'Abstain']] as const

export interface Proposal {
  coord: string
  d: string
  ev: Ev
  author: string
  groupId: string
  title: string
  summary: string
  text: string
  version: number
  status: ProposalStatus
  /** The latest poll on it, with its version and each member's latest answer. */
  poll?: { ev: Ev; version: number; votes: Map<string, string> }
}

export function proposals(): Proposal[] {
  const list = all(me())
  const del = deleted(list)
  const latest = new Map<string, Ev>()
  for (const e of list) {
    if (e.kind !== 30023 || !e.tags.some((t) => t[0] === 't' && t[1] === 'proposal')) continue
    const coord = `30023:${e.pubkey}:${tag(e, 'd')}`
    const prev = latest.get(coord)
    if (!prev || byTime(prev, e) < 0) latest.set(coord, e)
  }
  const gs = new Map(groups().map((g) => [g.id, g]))
  const out: Proposal[] = []
  for (const [coord, e] of latest) {
    if (del.has(coord)) continue
    const groupId = tag(e, 'h') ?? ''
    const g = gs.get(groupId)
    const parent = g?.parent ? gs.get(g.parent) : undefined
    const may = (pk: string) => pk === e.pubkey || !!g?.admins.has(pk) || !!parent?.admins.has(pk)
    let status = (tag(e, 'status') as ProposalStatus) || 'draft'
    let statusAt = 0
    let poll: Proposal['poll']
    for (const x of list) {
      if (tag(x, 'h') !== groupId) continue
      if (x.kind === 1985 && tag(x, 'L') === PROPOSAL_NS && may(x.pubkey) && x.created_at >= statusAt) {
        const l = x.tags.find((t) => t[0] === 'l' && t[2] === PROPOSAL_NS)?.[1] as ProposalStatus | undefined
        if (l && l in STATUS_LABEL) { status = l; statusAt = x.created_at }
      }
      if (x.kind === 1068 && tag(x, 'a') === coord && (!poll || byTime(poll.ev, x) < 0))
        poll = { ev: x, version: Number(tag(x, 'version')) || 1, votes: new Map() }
    }
    if (poll) {
      const at = new Map<string, number>()
      for (const x of list) {
        if (x.kind !== 1018 || tag(x, 'e') !== poll.ev.id) continue
        if ((at.get(x.pubkey) ?? -1) > x.created_at) continue
        const r = tag(x, 'response')
        if (r) { poll.votes.set(x.pubkey, r); at.set(x.pubkey, x.created_at) }
      }
    }
    out.push({
      coord, d: tag(e, 'd')!, ev: e, author: e.pubkey, groupId,
      title: tag(e, 'title') || 'Untitled proposal', summary: tag(e, 'summary') ?? '', text: e.content,
      version: Number(tag(e, 'version')) || 1, status, poll,
    })
  }
  return out
}

export const proposalFor = (groupId: string) => proposals().find((p) => p.groupId === groupId)

/** Who may change a proposal's status: its author and the admins of its chat or its parent. */
export function canDecide(p: Proposal) {
  const gs = groups()
  const g = gs.find((x) => x.id === p.groupId)
  const parent = gs.find((x) => x.id === g?.parent)
  const k = me()
  return k === p.author || !!g?.admins.has(k) || !!parent?.admins.has(k)
}

// ---- who sees a channel (NIP-78 kind 30078, `d` seen:<channel>, in the group) ----------------
// Opening a channel publishes (at most every few hours) one replaceable event per member and
// channel; with the people who wrote there, it says how many people a message will reach.
export const ACTIVE_DAYS = 30
export const seenD = (groupId: string) => `seen:${groupId}`

/** The people who opened or wrote in this channel in the last 30 days, the most recent first. */
export function recentlyActive(groupId: string): { pubkey: string; at: number }[] {
  const since = Date.now() / 1000 - ACTIVE_DAYS * 86400
  const g = group(groupId)
  const last = new Map<string, number>()
  for (const e of all(me())) {
    if (e.created_at < since || tag(e, 'h') !== groupId) continue
    const seen = e.kind === 30078 && tag(e, 'd') === seenD(groupId)
    if (!seen && e.kind !== 9 && e.kind !== 1111 && e.kind !== 7) continue
    if ((last.get(e.pubkey) ?? 0) < e.created_at) last.set(e.pubkey, e.created_at)
  }
  // a channel with a member list: only its members (not the keys that post e-mail or drop-box cards)
  const known = g && g.members.size ? new Set([...g.members, ...g.admins]) : undefined
  return [...last].filter(([pk]) => !known || known.has(pk)).map(([pubkey, at]) => ({ pubkey, at })).sort((a, b) => b.at - a.at)
}

/** When I last told this channel I opened it (0: never, or more than 30 days ago). */
export function mySeen(groupId: string): number {
  let at = 0
  for (const e of all(me())) if (e.kind === 30078 && e.pubkey === me() && tag(e, 'd') === seenD(groupId) && tag(e, 'h') === groupId) at = Math.max(at, e.created_at)
  return at
}

/** Who joined a channel as its guest (kind 9000 from Belfry with `invited-by`, guests.py). */
export interface GuestJoin { id: string; pubkey: string; by?: string; at: number; agent: boolean; unsaved: boolean; /** a member since (an admin made them one): no guest any more */ member: boolean }
export function guestJoins(groupId: string): GuestJoin[] {
  const out: GuestJoin[] = []
  const list = all(me())
  // Belfry labels a guest "saved" (kind 1985, belfry/guest) once they proved an e-mail
  const saved = new Set(list.filter((e) => e.kind === 1985 && tag(e, 'h') === groupId && tag(e, 'L') === 'belfry/guest'
    && e.tags.some((t) => t[0] === 'l' && t[1] === 'saved')).map((e) => tag(e, 'p')))
  // … and "member" once an admin made them a member (admins.py): the join line stays, the guest tag goes
  const member = new Set(list.filter((e) => e.kind === 1985 && tag(e, 'h') === groupId && tag(e, 'L') === 'belfry/guest'
    && e.tags.some((t) => t[0] === 'l' && t[1] === 'member')).map((e) => tag(e, 'p')))
  for (const e of list) {
    if (e.kind !== 9000 || tag(e, 'h') !== groupId || !tag(e, 'invited-by')) continue
    const p = tag(e, 'p')
    if (p) out.push({ id: e.id, pubkey: p, by: tag(e, 'invited-by'), at: e.created_at, agent: tag(e, 'agent') === '1', unsaved: tag(e, 'unsaved') === '1' && !saved.has(p) && !member.has(p), member: member.has(p) })
  }
  // someone who was taken out again without ever writing here (a double sign-up that was
  // cleaned up, an invite never used) leaves no line
  const gone = new Map<string, number>()
  const wrote = new Set<string>()
  for (const e of list) {
    if (tag(e, 'h') !== groupId) continue
    if (e.kind === 9001) { const p = tag(e, 'p'); if (p) gone.set(p, Math.max(gone.get(p) ?? 0, e.created_at)) }
    else if (e.kind === 9 || e.kind === 1111) wrote.add(e.pubkey)
  }
  return out.filter((j) => wrote.has(j.pubkey) || (gone.get(j.pubkey) ?? 0) < j.at).sort((a, b) => a.at - b.at)
}
