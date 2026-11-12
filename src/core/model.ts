import { sha256 } from '@noble/hashes/sha256'
import { bytesToHex } from '@noble/hashes/utils'
import { nip19 } from 'nostr-tools'
import { BELFRY, LABEL_NS, getBackend } from '../config'
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
/** A public channel (its page on the web is on, visibility public) shows a globe, not a hash. */
export const isPublicChannel = (g?: Group) => { const w = webPage(g); return !!w?.on && w.visibility === 'public' }
export const channelIcon = (g?: Group) => (g?.isEvent ? '📅' : g?.isPod ? (POD_GLYPH[podSides(g.members.size)] ?? '○') : g?.isProposal ? '📝' : isPublicChannel(g) ? '🌐' : '#')
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
      // a co-host (events): put in with the admin role; the relay's 39001 says the same
      if (p && e.kind === 9000 && e.tags.some((t) => t[0] === 'p' && t[1] === p && t[2] === 'admin')) g.admins.add(p)
    }
    if (e.kind === 9) {
      g.lastActivity = Math.max(g.lastActivity, e.created_at)
      g.members.add(e.pubkey)
    }
    // join / leave (NIP-29 9021 / 9022): counted here too, whether or not the relay folds them in
    if (e.kind === 9021) {
      if (!g.closed && !e.tags.some((t) => t[0] === 'waitlist' || t[0] === 'ticket')) g.members.add(e.pubkey)
      else applied.set(id + e.pubkey, e)
    }
    if (e.kind === 9022) applied.delete(id + e.pubkey)
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

// ---- a channel's public page on the web (<domain>/<slug>, Belfry's webthreads.py) ---------
// The admins' latest NIP-78 event, d = belfry:web:<channel>, t = belfry-web: anyone opens a thread
// there (it lands here as an e-mail card). The channel's visibility (Xavier, 2026-11-11: one
// general setting, public or members only) rides in the same event: "public" lists every thread
// on the page, "members" shows someone from outside only their own. Older events said "list".
export const WEB_D = 'belfry:web:'
export type ChannelVisibility = 'public' | 'members'
// "posts": a channel that takes one type of post only (#proposals, #events: Xavier, 2026-11-11)
export type PostType = 'proposal' | 'event' | 'request'
export interface WebPage { on: boolean; slug: string; visibility: ChannelVisibility; posts?: PostType }
// paths of the belfry's own: a channel can't take them (webthreads.RESERVED)
export const WEB_RESERVED = new Set(('api admin apply apps bridge calendar chat check claim console connect door drop events feedback follow git groups hooks internal ' +
  'market me mentions people photos programme shop signin skills static steward theme tokens vote wallet welcome invite brand ' +
  'changelog monitor privacy terms refunds rules help').split(' '))
export const slugify = (s: string) => s.toLowerCase().normalize('NFKD').replace(/[\u0300-\u036f]/g, '').replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 40)

/** The left part of a channel's e-mail address (Xavier, 2026-11-12): its plain slug, bugs@ rather
 *  than bugs-c3e5@, when no other channel answers to it; the server resolves it the same way
 *  (mail.channel_by_slug) and keeps taking the full id. */
export function mailLocal(g: Group): string {
  const plain = (x: Group) => [x.id.replace(/-[0-9a-z]{3,8}$/, ''), slugify(x.name)]
  const want = slugify(g.name) || g.id.replace(/-[0-9a-z]{3,8}$/, '')
  if (!want || want === g.id) return g.id
  return groups().filter((x) => plain(x).includes(want)).length === 1 ? want : g.id
}

export function webPage(g: Group | undefined): WebPage | undefined {
  if (!g) return undefined
  const evs = all(me())
  // the relay's own key (it signs the 39000) set up the default channels (#proposals, #events)
  const relayKey = evs.find((e) => e.kind === 39000 && tag(e, 'd') === g.id)?.pubkey
  const own = evs.filter((e) => e.kind === 30078 && tag(e, 'd') === WEB_D + g.id && tag(e, 'h') === g.id && (g.admins.has(e.pubkey) || e.pubkey === relayKey)).sort(byTime)
  try {
    const c = own.length ? JSON.parse(own.at(-1)!.content || '{}') : null
    return c ? { on: !!c.on, slug: String(c.slug || ''), visibility: c.visibility === 'members' || (!c.visibility && c.list === 'own') ? 'members' : 'public', ...(c.posts === 'proposal' || c.posts === 'event' || c.posts === 'request' ? { posts: c.posts } : {}) } : undefined
  } catch { return undefined }
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
  /** When its assignee last said "working on it now" (a 2101 `working` heartbeat), unix seconds. */
  beat?: number
  /** When it was made (unix seconds). */
  at: number
  /** What happened to it, oldest first: shown as lines in the thread of the message it came from.
   *  Heartbeats count once per working stretch (a gap over BEAT_FRESH starts a new one). */
  history: TaskStep[]
}

export interface TaskStep { at: number; who: string; what: 'created' | 'claim' | 'unclaim' | 'working' | 'idle' | 'done' | 'undone' | 'comment'; text?: string }

/** A to-do is being worked on right now: its assignee's heartbeat is under 5 minutes old (agents
 *  send one every 2 minutes while busy, `idle` when they stop). One to-do per person at a time. */
export const BEAT_FRESH = 300
// browser tests only (?backend=demo): read a channel's to-dos
if (typeof window !== 'undefined' && getBackend() === 'demo') (window as unknown as { __demoTasks: typeof tasklist }).__demoTasks = (g) => tasklist(g)
export const isWorking = (t: Task, now = Date.now() / 1000) => !t.done && !!t.beat && now - t.beat < BEAT_FRESH
/** When the current working stretch began (its first heartbeat), for the ring that fills with the minutes. */
export const workingSince = (t: Task) => [...t.history].reverse().find((h) => h.what === 'working' && h.who === t.assignee)?.at ?? t.beat

export function tasklist(groupId: string): { name?: string; tasks: Task[] } {
  const listId = listIdFor(groupId)
  const evs = all(me()).filter((e) => e.kind >= 2100 && e.kind <= 2103 && tag(e, 't') === listId).sort(byTime)
  const tasks = new Map<string, Task>()
  let name: string | undefined
  const beats = new Map<string, { task: string; at: number }>() // per person: their latest heartbeat
  for (const e of evs) {
    if (e.kind === 2100) {
      const title = e.content.trim().slice(0, 300)
      if (title) tasks.set(e.id, { id: e.id, title, author: e.pubkey, done: false, pending: !!e.pending, comments: [], source: tag(e, 'q'), at: e.created_at, history: [{ at: e.created_at, who: e.pubkey, what: 'created' }] })
    } else if (e.kind === 2102) {
      name = e.content.slice(0, 48)
    } else {
      const t = tasks.get(tag(e, 'e') ?? '')
      if (!t) continue
      if (e.kind === 2103) { t.comments.push({ author: e.pubkey, text: e.content.slice(0, 2000), at: e.created_at }); t.history.push({ at: e.created_at, who: e.pubkey, what: 'comment', text: e.content.slice(0, 2000) }) }
      const a = tag(e, 'action')
      if (a === 'working') {
        // one line per working stretch: not after another beat of the same person within BEAT_FRESH
        const lastBeat = [...t.history].reverse().find((h) => h.who === e.pubkey && (h.what === 'working' || h.what === 'idle' || h.what === 'done'))
        const prevAt = (t as Task & { _beat?: number })._beat
        if (!(lastBeat?.what === 'working' && prevAt && e.created_at - prevAt < BEAT_FRESH)) t.history.push({ at: e.created_at, who: e.pubkey, what: 'working' });
        (t as Task & { _beat?: number })._beat = e.created_at
      } else if (a === 'claim' || a === 'unclaim' || a === 'idle' || a === 'done' || a === 'undone') t.history.push({ at: e.created_at, who: e.pubkey, what: a })
      if (a === 'claim') t.assignee = e.pubkey
      if (a === 'unclaim' && t.assignee === e.pubkey) t.assignee = undefined
      if (a === 'done') t.done = true
      if (a === 'undone') t.done = false
      if (a === 'delete' && e.pubkey === t.author) tasks.delete(t.id)
      if (a === 'working') beats.set(e.pubkey, { task: t.id, at: e.created_at })
      if (a === 'idle' && beats.get(e.pubkey)?.task === t.id) beats.delete(e.pubkey)
    }
  }
  for (const [p, b] of beats) { const t = tasks.get(b.task); if (t && t.assignee === p) t.beat = b.at }
  for (const t of tasks.values()) delete (t as Task & { _beat?: number })._beat
  return { name, tasks: [...tasks.values()] }
}

/** The badge: done / total among the latest 20 to-dos (by when they were made). */
export function taskTally(tasks: Task[]): { done: number; total: number } {
  const recent = [...tasks].sort((a, b) => b.at - a.at).slice(0, 20)
  return { done: recent.filter((t) => t.done).length, total: recent.length }
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
// An event (Belfry events, PROTOCOL.md): a NIP-52 event on the community relay with `d` =
// <year>-<6 of [a-z0-9]>, its own NIP-29 group ev-<d> (its chat; the hosts are its admins), and
// cards in any channels: messages that carry its URL (https://<domain>/events/<link>) or its
// `a` tag. Co-hosts edit it (rule B): among the versions with that `d` signed by a host, the
// newest wins; its coordinate stays the creator's. Events made before (session-<x> ↔ ws-<x>)
// keep working: one author, the group named by the `h` tag.

export type Rsvp = 'accepted' | 'tentative' | 'declined'
export type Visibility = 'members' | 'link' | 'public'

export interface CalEvent {
  coord: string // kind:creator:d
  d: string
  ev: Ev
  title: string
  description: string
  location?: string
  allDay: boolean
  start: Date
  end?: Date
  /** The time zone it was set in (NIP-52 start_tzid). */
  tz?: string
  groupId: string
  /** Online meeting links (NIP-52 `r` tags: video calls, documents). */
  links: string[]
  /** The channels its card was sent in (not its own chat); may be empty. */
  channels: string[]
  rsvps: Map<string, Rsvp>
  /** Each person's RSVP event (for "Still going?"). */
  rsvpEvents: Map<string, Ev>
  /** A Belfry event (ev-<d> group, public page), not an older session. */
  isNew: boolean
  creator: string
  hosts: Set<string>
  cover?: string
  visibility: Visibility
  slug?: string
  status: 'scheduled' | 'cancelled'
  statusReason?: string
  /** Every version seen, oldest first (for "Still going?" after a change). */
  versions: Ev[]
  /** Read from the belfry's API (a guest: the community relay is not theirs). */
  remote?: boolean
  /** A meeting (Xavier, 2026-11-12): a members-only event made from a channel's ＋ menu with a
   *  topic, the people invited, a time, a length and a place. Its `t` tag is `meeting`. */
  meeting: boolean
  /** The people invited to a meeting: its `p` tags marked `invited`. */
  invited: string[]
  /** The channel it was made from (`channel` tag), for meetings. */
  from?: string
}

export const isEventKind = (k: number) => k === 31922 || k === 31923
export const EVENT_D = /^\d{4}-[a-z0-9]{6}$/
export const EVENT_STATUS_NS = 'belfry/event-status'
export const eventLink = (e: Pick<CalEvent, 'd' | 'slug'>) => e.slug || e.d
export const eventUrl = (e: Pick<CalEvent, 'd' | 'slug'>) => `https://${BELFRY.domain}/events/${eventLink(e)}`
const escapeRe = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
/** https://<own domain>/events/<link> in a text: the links, at most three, in order. */
export function eventLinksIn(text: string): string[] {
  const re = new RegExp(`https://${escapeRe(BELFRY.domain)}/events/([a-z0-9][a-z0-9-]{1,38}[a-z0-9])(?![a-z0-9-])`, 'g')
  return [...new Set([...(text || '').matchAll(re)].map((m) => m[1]))].slice(0, 3)
}

/** The event a chat post links to (its `a` tag), if any. */
export const eventCoordOf = (e: Ev) => {
  const a = tag(e, 'a')
  return a && isEventKind(Number(a.split(':')[0])) ? a : undefined
}
/** Every event coordinate a message's `a` tags name (up to three). */
export const eventCoordsOf = (e: Ev) =>
  [...new Set(e.tags.filter((t) => t[0] === 'a' && isEventKind(Number((t[1] ?? '').split(':')[0]))).map((t) => t[1]))].slice(0, 3)

export const calendar: () => CalEvent[] = perLog((list) => {
  const del = deleted(list)
  const gs = new Map(groups().map((g) => [g.id, g]))
  const creators = new Map<string, string>() // group id → its creator (first 9007)
  for (const e of [...list].sort(byTime)) if (e.kind === 9007) { const h = tag(e, 'h'); if (h && !creators.has(h)) creators.set(h, e.pubkey) }
  // versions: by d for Belfry events (any host may write one), by coordinate for the older ones
  const byKey = new Map<string, Ev[]>()
  for (const e of list) {
    if (e.kind !== 31922 && e.kind !== 31923) continue
    const d = tag(e, 'd')
    if (!d) continue
    const key = EVENT_D.test(d) ? 'd:' + d : `${e.kind}:${e.pubkey}:${d}`
    byKey.set(key, [...(byKey.get(key) ?? []), e])
  }
  const rsvps = new Map<string, Map<string, { s: Rsvp; at: number; ev: Ev }>>()
  for (const e of list) {
    if (e.kind !== 31925) continue
    const a = tag(e, 'a')
    const s = tag(e, 'status') as Rsvp | undefined
    if (!a || !s) continue
    const m = rsvps.get(a) ?? new Map()
    const prev = m.get(e.pubkey)
    if (!prev || prev.at <= e.created_at) m.set(e.pubkey, { s, at: e.created_at, ev: e })
    rsvps.set(a, m)
  }
  const statusLabels = new Map<string, Ev[]>()
  for (const e of list) if (e.kind === 1985 && tag(e, 'L') === EVENT_STATUS_NS) { const h = tag(e, 'h'); if (h) statusLabels.set(h, [...(statusLabels.get(h) ?? []), e]) }
  const out: CalEvent[] = []
  for (const [key, versions] of byKey) {
    const sorted = [...versions].sort(byTime)
    let e = sorted.at(-1)!
    let creator = e.pubkey
    let hosts = new Set([e.pubkey])
    const d = tag(e, 'd')!
    const isNew = key.startsWith('d:')
    const groupId = isNew ? 'ev-' + d : tag(e, 'h') || 'ws-' + d.replace(/^session-/, '')
    if (isNew) {
      creator = creators.get(groupId) ?? sorted[0].pubkey
      hosts = new Set([creator, ...(gs.get(groupId)?.admins ?? [])])
      const mine = sorted.filter((v) => hosts.has(v.pubkey))
      if (!mine.length) continue
      e = mine.at(-1)!
    }
    const kind = isNew ? (sorted.find((v) => v.pubkey === creator)?.kind ?? e.kind) : e.kind
    const coord = `${kind}:${creator}:${d}`
    if (del.has(coord)) continue
    const allDay = e.kind === 31922
    const parse = (v?: string) => (!v ? undefined : allDay ? new Date(v + 'T00:00:00') : new Date(Number(v) * 1000))
    const start = parse(tag(e, 'start'))
    if (!start || isNaN(+start)) continue
    let status: CalEvent['status'] = tag(e, 'status') === 'cancelled' ? 'cancelled' : 'scheduled'
    let statusReason: string | undefined
    const labels = (statusLabels.get(groupId) ?? []).filter((x) => hosts.has(x.pubkey)).sort(byTime)
    const last = labels.at(-1)
    if (last && (last.created_at >= e.created_at || status === 'scheduled')) {
      status = last.tags.some((t) => t[0] === 'l' && t[1] === 'cancelled') ? 'cancelled' : 'scheduled'
    }
    // the line to the guests: the latest cancellation label's
    if (status === 'cancelled') statusReason = labels.filter((x) => x.tags.some((t) => t[0] === 'l' && t[1] === 'cancelled')).at(-1)?.content || undefined
    const vis = tag(e, 'visibility')
    const m = rsvps.get(coord) ?? new Map()
    out.push({
      coord, d, ev: e, versions: sorted.filter((v) => hosts.has(v.pubkey)),
      title: tag(e, 'title') || tag(e, 'name') || 'Untitled',
      description: e.content,
      location: tag(e, 'location'),
      links: e.tags.filter((t) => t[0] === 'r' && /^https:\/\//.test(t[1] ?? '')).map((t) => t[1]),
      allDay, start, end: parse(tag(e, 'end')), tz: tag(e, 'start_tzid'),
      groupId, channels: [], isNew, creator, hosts,
      cover: tag(e, 'image') || undefined,
      visibility: vis === 'public' || vis === 'link' ? vis : 'members',
      slug: tag(e, 'slug') || undefined,
      status, statusReason,
      meeting: e.tags.some((t) => t[0] === 't' && t[1] === 'meeting'),
      invited: [...new Set(e.tags.filter((t) => t[0] === 'p' && t[3] === 'invited' && /^[0-9a-f]{64}$/.test(t[1] ?? '')).map((t) => t[1]))],
      from: tag(e, 'channel') || undefined,
      rsvps: new Map([...m.entries()].map(([p, v]) => [p, v.s])),
      rsvpEvents: new Map([...m.entries()].map(([p, v]) => [p, v.ev])),
    })
  }
  // the channels showing each event's card: messages with its `a` tag or its URL, not deleted
  const byLink = new Map<string, CalEvent>()
  for (const x of out) { byLink.set(x.d, x); if (x.slug) byLink.set(x.slug, x) }
  const byCoord = new Map(out.map((x) => [x.coord, x]))
  for (const e of list) {
    if (e.kind !== 9 || del.has(e.id)) continue
    const h = tag(e, 'h')
    if (!h) continue
    const found = new Set<CalEvent>()
    for (const c of eventCoordsOf(e)) { const x = byCoord.get(c); if (x) found.add(x) }
    for (const l of eventLinksIn(e.content)) { const x = byLink.get(l); if (x) found.add(x) }
    for (const x of found) if (x.groupId !== h && !x.channels.includes(h)) x.channels.push(h)
  }
  return out.sort((a, b) => +a.start - +b.start)
})

export const eventByCoord = (coord: string) => calendar().find((x) => x.coord === coord)
export const eventByLinkLocal = (link: string) => calendar().find((x) => x.d === link || x.slug === link)
export const eventOfGroup = (groupId: string) => calendar().find((x) => x.groupId === groupId)

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
