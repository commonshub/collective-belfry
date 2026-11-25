import { roleOf } from './myrole'
import { clearInGroup } from './enc/core'
import { encryptedMedia } from './dmfiles'
import { sha256 } from '@noble/hashes/sha256'
import { bytesToHex } from '@noble/hashes/utils'
import { nip19 } from 'nostr-tools'
import { BELFRY, LABEL_NS, getBackend } from '../config'
import { all, type Ev } from './store'
import { me, setWriteGuard, groupWrapReady, setPrivateVerdict } from './sync'
import { plainName, plainOf } from './refs'
import { agentOwner, belfryAdminKeys, belfryGroupsKey, isAdmin as isAdminNow, encryptedFormat, encSeen } from './access'
import { CHARTER_ID, placeName } from './places'

// Derived views over the event log. Pure functions of all(); cheap enough to
// recompute per render for a village-sized dataset.

const tag = (e: Ev, k: string) => e.tags.find((t) => t[0] === k)?.[1]
const byTime = (a: Ev, b: Ev) => a.created_at - b.created_at || (a.id < b.id ? -1 : 1)

export function deleted(list: Ev[]) {
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
  const out = new Map<string, Profile & { at: number; id?: string }>()
  const guests: [string, string][] = []
  for (const e of list) {
    // a guest's name, from the event that put them in a channel (guests.py), until a profile
    if (e.kind === 9000 && tag(e, 'name')) { const p = tag(e, 'p'); if (p) guests.push([p, tag(e, 'name')!.slice(0, 100)]) }
    if (e.kind !== 0) continue
    const prev = out.get(e.pubkey)
    // the newest wins; two in the same second: my queued one, else the lowest id (NIP-01, as relays
    // keep it), so every device and every view agrees on one profile
    if (prev && (prev.at > e.created_at || (prev.at === e.created_at && !e.pending && (prev.id ?? '') <= e.id))) continue
    try {
      const p = JSON.parse(e.content)
      out.set(e.pubkey, { name: p.display_name || p.name, picture: p.picture, nip05: p.nip05, at: e.created_at, id: e.pending ? '' : e.id })
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

/** Keys a 9000 marks as an agent (`agent` 1): an agent with no name reads "An agent". */
const agentKeys: () => Set<string> = perLog((list) => new Set(list.filter((e) => e.kind === 9000 && tag(e, 'agent') === '1').map((e) => tag(e, 'p')).filter((p): p is string => !!p)))
export const looksLikeAgent = (pubkey: string) => !!agentOwner(pubkey) || agentKeys().has(pubkey)

// Never a shortened npub (to-do bbd452d5, 2026-11-19): a name not known is asked for (names.ts:
// kind 0 on both relays) and reads "Somebody" for a person, "An agent" for an agent meanwhile;
// screens that show a name on its own show a grey bar while it is being asked (PersonName).
let missing: (pubkey: string) => void = () => {}
export const onMissingName = (f: (pubkey: string) => void) => { missing = f }
export const FALLBACK_PERSON = 'Somebody'
export const FALLBACK_AGENT = 'An agent'
export const fallbackName = (pubkey: string) => (looksLikeAgent(pubkey) ? FALLBACK_AGENT : FALLBACK_PERSON)

export function displayName(pubkey: string, ps = profiles()) {
  if (pubkey === me()) return 'You'
  const p = ps.get(pubkey)
  const n = p?.name || p?.nip05?.split('@')[0]
  if (n) return n
  // a reading guest (a 9000 tagged reading, never named): "Guest" until they save (cards/contribute-save)
  if (allGroups().some((g) => g.reading.has(pubkey))) return 'Guest'
  missing(pubkey)
  return fallbackName(pubkey)
}

// ---- groups (NIP-29) --------------------------------------------------------

export interface Group {
  id: string
  /** The name to show: on-brand for the default places while their own name is the default (places.ts). */
  name: string
  /** The group's own name as the relay has it (what an edit writes back). */
  rawName: string
  /** no name known yet: NAME is a neutral word ("this channel"), never something to save */
  unnamed?: boolean
  about: string
  picture?: string
  createdAt: number
  lastActivity: number
  members: Set<string>
  /** guests who only opened a link so far (guest mode, guests.py): reading, not counted as members */
  reading: Set<string>
  isEvent: boolean
  /** A group (a working group; ids `pod-…`, the protocol's old word): a channel with a parent
   *  (where it was posted), its own members, events and sub-groups. */
  isPod: boolean
  /** A proposal: a channel holding one NIP-23 text (versioned), a status and a vote. */
  isProposal: boolean
  /** For a pod or proposal: the channel or pod it lives in ('general' when unknown). */
  parent?: string
  /** NIP-29 `closed`: joining takes an application (9021) that an admin admits (9000). */
  closed: boolean
  /** The question applicants answer (our `prompt` tag on the group's 9002). */
  prompt?: string
  /** How many members vouching admits an applicant (our `vouches` tag on the 9002, 1–5;
   *  0: its admins accept). Belfry's vouches.py does the admitting. */
  vouches: number
  /** Private: `closed` + `private` on the 9007 / 9002 (docs/one-shape.md, 2026-11-22): only the
   *  people its admins add get in. No Apply, no card, not listed to others; a 9021 isn't shown. */
  inviteOnly: boolean
  admins: Set<string>
  /** The server's own sets (Belfry's mail.group_admins, webthreads.py): the p keys of the relay's
   *  latest 39001 (undefined until one arrived) and the admins by role. ADMINS adds the 9007
   *  creator on top, for the screens before the 39001 arrives; settings the server reads count
   *  only these. */
  adminList?: string[]
  roleAdmins: Set<string>
  /** Open applications to a closed group: the latest 9021 of someone not admitted nor declined. */
  applications: Application[]
  pending: boolean
  /** Archived (our `archived` tag on the group's 9002: "1", and "0" to unarchive; the latest 9002
   *  that says anything about it wins): out of every list, read only, until it is unarchived.
   *  Never for `general`, events or proposals. The relay enforces none of it. */
  archived: boolean
  /** Its own metadata is here (a 9007, 9002 or 39000): the group exists, or did. */
  known: boolean
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
/** Text for one-line previews: mentions as "@…", each reference to something of this belfry (a
 *  channel, event, to-do, message, person, drop box…) as its name, never its address (refs.ts). */
export function plainRefs(text: string, npub = '@…'): string {
  return plainOf(text, (r) => plainName(r, me()), BELFRY.domain, npub)
}
export const isPodId = (id: string) => id.startsWith('pod-')
export const isProposalId = (id: string) => id.startsWith('prop-')
/** The pod a chat post announces (its `pod` tag), if any. */
// ---- who really made it: the cards server signs for the claimer (Xavier, 23 Nov) --------------
// The groups key signs a card's group or topic (9007, 9002, the "New group" post in general, the
// first line) and names the person in ["by", <pubkey>]. Honoured only on events whose author is
// exactly the groups key (belfry.json groups_key). Groups claimed before the server wrote `by`: the
// ["p", …] of the group's kind 9 tagged ["card", "1"] by the groups key.
let claimers: { list: Ev[]; map: Map<string, string> } = { list: [], map: new Map() }
function claimerOf(group: string): string | undefined {
  const list = all(me())
  if (claimers.list !== list) {
    const gk = belfryGroupsKey(), map = new Map<string, string>()
    if (gk) for (const e of list) if (e.kind === 9 && e.pubkey === gk && e.tags.some((t) => t[0] === 'card' && t[1] === '1')) {
      const h = e.tags.find((t) => t[0] === 'h')?.[1], p = e.tags.find((t) => t[0] === 'p' && /^[0-9a-f]{64}$/.test(t[1] ?? ''))?.[1]
      if (h && p && !map.has(h)) map.set(h, p)
    }
    claimers = { list, map }
  }
  return claimers.map.get(group)
}
/** E's real author: the person behind an event the groups key signed for them (["by"], or the
 *  card's claimer); anyone else's events are their own. */
export function authorOf(e: { pubkey: string; kind: number; tags: string[][] }): string {
  const gk = belfryGroupsKey()
  if (!gk || e.pubkey !== gk) return e.pubkey
  const by = e.tags.find((t) => t[0] === 'by' && /^[0-9a-f]{64}$/.test(t[1] ?? ''))?.[1]
  if (by) return by
  const group = e.tags.find((t) => t[0] === 'pod')?.[1] ?? e.tags.find((t) => t[0] === 'h')?.[1]
  return (group && claimerOf(group)) || e.pubkey
}

/** The topic a town-square post announces (the cards server: ["t","new-topic"], ["channel", <hashtag>]). */
export const newTopicOf = (e: Ev) => e.kind === 9 && e.tags.some((t) => t[0] === 't' && t[1] === 'new-topic') ? tag(e, 'channel') : undefined
export const podOf = (e: Ev) => {
  const v = tag(e, 'pod')
  return v && isPodId(v) ? v : undefined
}
/** The proposal a chat post announces (its `proposal` tag), if any. */
export const proposalOf = (e: Ev) => {
  const v = tag(e, 'proposal')
  return v && isProposalId(v) ? v : undefined
}

/** Can this group be archived at all? Channels and groups; not `general`, events or proposals. */
export const archivable = (id: string) => id !== 'general' && !id.startsWith('ws-') && !id.startsWith('ev-') && !isProposalId(id)

interface GroupSets { live: Group[]; archived: Group[]; every: Group[]; byId: Map<string, Group>; gone: Map<string, Group> }
const groupSets: () => GroupSets = perLog((list) => {
  const map = new Map<string, Group & { metaAt: Record<string, number> }>()
  const applied = new Map<string, Ev>() // group id + pubkey → latest 9021
  const decided = new Map<string, number>() // group id + pubkey → latest 9000 / 9001
  const made = new Map<string, string>() // group id → its creator (first 9007)
  const listed = new Map<string, string[]>() // group id → the relay's latest 39001
  const byRole = new Map<string, boolean>() // group id + pubkey → admin by role
  const get = (id: string, at: number) => {
    let g = map.get(id)
    if (!g) {
      g = { id, name: id, rawName: id, about: '', createdAt: at, lastActivity: at, members: new Set(), reading: new Set(), isEvent: id.startsWith('ws-') || id.startsWith('ev-'), isPod: isPodId(id), isProposal: isProposalId(id), closed: false, vouches: 0, inviteOnly: false, admins: new Set(), roleAdmins: new Set(), applications: [], pending: false, archived: false, known: false, metaAt: {} }
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
      g.known = true
      // archived or not: the latest of them that says so ("1" / "0"); an edit without the tag changes nothing
      const arch = tag(e, 'archived')
      if (arch !== undefined && archivable(id) && (g.metaAt.archived ?? 0) <= e.created_at) { g.archived = arch === '1'; g.metaAt.archived = e.created_at }
      const access = e.tags.some((t) => t[0] === 'closed') ? true : e.tags.some((t) => t[0] === 'open') ? false : undefined
      if (access !== undefined && (g.metaAt.closed ?? 0) <= e.created_at) { g.closed = access; g.inviteOnly = access && e.tags.some((t) => t[0] === 'private'); g.metaAt.closed = e.created_at }
      const prompt = tag(e, 'prompt')
      if (prompt !== undefined && (g.metaAt.prompt ?? 0) <= e.created_at) { g.prompt = prompt; g.metaAt.prompt = e.created_at }
      // the vouches rule goes with the access tags: an edit that says open / closed without it means "admins accept"
      if ((access !== undefined || tag(e, 'vouches') !== undefined) && (g.metaAt.vouches ?? 0) <= e.created_at) {
        const n = Number(tag(e, 'vouches') ?? 0)
        g.vouches = access === false ? 0 : Number.isInteger(n) ? Math.max(0, Math.min(n, 5)) : 0
        g.metaAt.vouches = e.created_at
      }
    }
    if (e.kind === 9007) made.set(id, made.get(id) ?? authorOf(e)) // its creator administers it (Belfry's relay patch); a card's claimer (authorOf)
    if (e.kind === 39001) listed.set(id, e.tags.filter((t) => t[0] === 'p' && t[1]).map((t) => t[1]))
    if (e.kind === 39002) g.members = new Set(e.tags.filter((t) => t[0] === 'p').map((t) => t[1]))
    // admitted (9000 put-user) or removed / declined (9001 remove-user) by an admin
    if (e.kind === 9000 || e.kind === 9001) {
      const p = tag(e, 'p')
      if (p) { if (e.kind === 9000) g.members.add(p); else g.members.delete(p); decided.set(id + p, e.created_at) }
      // guest mode (guests.py): a 9000 tagged `reading` is someone who opened a guest link and
      // has not joined; a later 9000 without it (they joined), a `saved` / `member` label or a
      // 9001 ends that
      if (p && e.kind === 9000 && tag(e, 'reading') === '1') g.reading.add(p)
      else if (p) g.reading.delete(p)
      // an admin by role (co-hosts, POST /group-roles; docs/checkin.md): put in with ["p", k, "admin"];
      // a 9000 tagged ["role", "member"] or a 9001 ends it (a ticket's 9000 does not). The relay's
      // 39001 never lists them: zooid has no per-group admins.
      if (p && e.kind === 9000 && e.tags.some((t) => t[0] === 'p' && t[1] === p && t[2] === 'admin')) byRole.set(id + p, true)
      else if (p && (e.kind === 9001 || tag(e, 'role') === 'member')) byRole.set(id + p, false)
    }
    if (e.kind === 1985 && tag(e, 'L') === 'belfry/guest' && e.tags.some((t) => t[0] === 'l' && (t[1] === 'saved' || t[1] === 'member'))) {
      const p = tag(e, 'p')
      if (p) g.reading.delete(p)
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
  for (const g of map.values()) {
    const c = made.get(g.id)
    if (c) g.admins.add(c)
    for (const k of listed.get(g.id) ?? []) g.admins.add(k)
    if (listed.has(g.id)) g.adminList = listed.get(g.id)
  }
  for (const [k, on] of byRole) if (on) { const g = map.get(k.slice(0, -64)); g?.admins.add(k.slice(-64)); g?.roleAdmins.add(k.slice(-64)) }
  for (const [k, e] of applied) {
    const g = map.get(k.slice(0, -64))
    if (!g || g.inviteOnly || g.members.has(e.pubkey) || (decided.get(k) ?? 0) >= e.created_at) continue
    g.applications.push({ pubkey: e.pubkey, answer: e.content, at: e.created_at, ev: e })
  }
  // … and until then they are in no member list or count (the relay's 39002 has their key)
  const self = me()
  for (const g of map.values()) for (const p of g.reading) if (p !== self) g.members.delete(p)
  // deleted: a 9008 for it (the relay then drops everything of the group but that 9008), unless the
  // group was made again since (a 9007 after it: the relay takes one for an id that is free again)
  const goneAt = new Map<string, number>(), madeAt = new Map<string, number>()
  for (const e of list) {
    const h = e.kind === 9008 || e.kind === 9007 ? tag(e, 'h') : undefined
    if (!h) continue
    const m = e.kind === 9008 ? goneAt : madeAt
    m.set(h, Math.max(m.get(h) ?? 0, e.created_at))
  }
  const gone = new Map<string, Group>()
  for (const [id, at] of goneAt) { const g = map.get(id); if (g && !((madeAt.get(id) ?? 0) > at)) gone.set(id, g) }
  // the on-brand names of the default places (display only; rawName is what the relay has)
  for (const g of map.values()) { g.rawName = g.name; g.name = placeName(g.id, g.name) ?? g.name }
  // never an id as a name (Xavier, 2026-11-18: "Don't put the ugly slug here. Just use the name of
  // the event"): a group whose metadata hasn't come yet takes its event's or proposal's title, else
  // a neutral word; it re-renders with its own name as soon as the 39000 / 9002 arrives
  const titles = new Map<string, { title: string; at: number }>()
  for (const e of list) {
    if (e.kind !== 31922 && e.kind !== 31923 && !(e.kind === 30023 && e.tags.some((t) => t[0] === 't' && t[1] === 'proposal'))) continue
    const title = tag(e, 'title')?.trim()
    if (!title) continue
    const d = tag(e, 'd') ?? ''
    for (const gid of [tag(e, 'h'), e.kind === 30023 ? undefined : 'ev-' + d, e.kind === 30023 ? undefined : 'ws-' + d.replace(/^session-/, '')]) {
      if (gid && (titles.get(gid)?.at ?? -1) <= e.created_at) titles.set(gid, { title, at: e.created_at })
    }
  }
  for (const g of map.values()) {
    if (g.metaAt.name !== undefined || g.name !== g.id) continue // named (its id can be its name: "alpha")
    const t = titles.get(g.id)?.title.slice(0, 120)
    g.name = t ?? (g.isEvent ? 'this event' : g.isProposal ? 'this proposal' : g.isPod ? 'this group' : 'this channel')
    if (!t) g.unnamed = true
  }
  const kept = [...map.values()].filter((g) => !gone.has(g.id))
  // the charter (19.32) is a group of the relay, never a channel or a group in any list
  const every = kept.filter((g) => g.id !== CHARTER_ID)
    .sort((a, b) => (a.id === 'general' ? -1 : b.id === 'general' ? 1 : b.lastActivity - a.lastActivity))
  return { every, live: every.filter((g) => !g.archived), archived: every.filter((g) => g.archived), byId: new Map(kept.map((g) => [g.id, g])), gone }
})

/** The groups people work in: every channel, group, event chat and proposal that is neither
 *  deleted nor archived. Every list is made of these (the channel list, pickers, counts). */
export const groups = (): Group[] => groupSets().live
/** The archived ones (readable from their card, a link or the Archived screen; never listed). */
export const archivedGroups = (): Group[] => groupSets().archived
/** Live and archived together: for looking things up (who is a guest where, an event's channel). */
export const allGroups = (): Group[] => groupSets().every
/** A deleted group, as this device still remembers it (its name for the card). */
export const deletedGroup = (id: string): Group | undefined => groupSets().gone.get(id)

/** Vouches for requests to join a group (NIP-32 kind 1985, `L belfry/vouch`, `l vouch|withdrawn`,
 *  `e` = the 9021, `p` = the applicant, `h` = the group): request id → author → latest value. */
export const VOUCH_NS = 'belfry/vouch'
const vouchLabels: () => Map<string, Map<string, { v: string; at: number }>> = perLog((list) => {
  const out = new Map<string, Map<string, { v: string; at: number }>>()
  for (const e of list) {
    if (e.kind !== 1985 || tag(e, 'L') !== VOUCH_NS) continue
    const req = tag(e, 'e'), v = tag(e, 'l')
    if (!req || !v) continue
    const m = out.get(req) ?? new Map()
    const was = m.get(e.pubkey)
    if (!was || was.at <= e.created_at) m.set(e.pubkey, { v, at: e.created_at })
    out.set(req, m)
  }
  return out
})
/** Who vouches for application A in group G now: members of G, never the applicant. (Belfry also
 *  leaves out guests and agents when it counts: isCounted filters those where the chat knows.) */
export function vouchesFor(g: Group, a: Application, isCounted: (pk: string) => boolean = () => true): string[] {
  const m = vouchLabels().get(a.ev.id)
  if (!m) return []
  return [...m].filter(([pk, x]) => x.v === 'vouch' && pk !== a.pubkey && g.members.has(pk) && isCounted(pk)).map(([pk]) => pk)
}

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

/** A group by id, archived or not (never a deleted one). */
export const group = (id: string): Group | undefined => groupSets().byId.get(id)

// nothing is posted or changed in an archived group (the relay would take it: this is the app's
// own rule, whatever screen asks): only unarchiving it, leaving it, or deleting it
// the review's P1.2 (sync.ts): a group write on format 3 is encrypted when its group is private here
// (its metadata, or a key of it already seen: enc/runtime adds that), and waits while the group isn't known
// (re-read P1-a) a key of the group seen on this device: encrypted whatever the format says; until
// enc/runtime has loaded what it saw (setEncryptedHere), a device that ever saw format 3 holds
let encryptedHere: ((h: string) => boolean) | undefined
export const setEncryptedHere = (f: (h: string) => boolean) => { encryptedHere = f }
const privateMeta = (h: string) => { const g = group(h); return !!(g?.isPod && g.closed && g.inviteOnly) }
/** Whether writing in group H is encrypted ('enc'), must wait ('hold'), or is plain (undefined). */
export function groupWriteMode(h: string): 'enc' | 'hold' | undefined {
  if (!h) return undefined
  const f = encryptedFormat(), seen = encSeen()
  if (!encryptedHere) return f === 3 || seen ? 'hold' : undefined // runtime not loaded yet
  if (encryptedHere(h)) return 'enc'
  if (f === 3) return privateMeta(h) ? 'enc' : group(h)?.known ? undefined : 'hold'
  if (f === undefined && seen) return privateMeta(h) || !group(h)?.known ? 'hold' : undefined
  return undefined
}
/** A private group, encrypted or waiting to be: P1.4's rule, the guest rule, no typing. */
export const privateNow = (h: string) => groupWriteMode(h) === 'enc' || (groupWriteMode(h) === 'hold' && privateMeta(h))
/** The review's P1.4 (a ruling): events, meetings, proposals and tickets can't be made from a private group yet. */
export const noExtrasHere = (h: string) => privateNow(h)
export const NO_EXTRAS = 'Not available in private groups yet.'
setPrivateVerdict((h, t) => {
  if (!h || clearInGroup({ ...t, pubkey: me() }, belfryGroupsKey())) return undefined
  return groupWriteMode(h)
})
setWriteGuard((t) => {
  const of = (k: string) => t.tags.find((x) => x[0] === k)?.[1]
  const h = of('h')
  // a private group on format 3 (docs/encrypted.md): nothing goes there unencrypted. A guest's
  // session can't take part yet (the lead, 2026-11-23); a session where encryption doesn't run can't post
  const g0 = h ? group(h) : undefined
  if (h && privateNow(h) && ![9021, 9022].includes(t.kind) && !(t.kind === 30078 && (of('d') ?? '').startsWith('seen:'))) {
    if (allGroups().some((x) => guestJoins(x.id).some((j) => j.pubkey === me() && !j.member))) return 'Guests can’t take part in private groups yet.'
    if (!groupWrapReady()) return 'This device can’t post in private groups yet.'
  }
  if (!h || !group(h)?.archived) return undefined
  if (t.kind === 9002 && t.tags.some((x) => x[0] === 'archived' && x[1] === '0')) return undefined
  if (t.kind === 9022 || t.kind === 9008) return undefined
  if (t.kind === 30078 && (of('d') ?? '').startsWith('seen:')) return undefined
  return 'This group is archived: nothing can be posted or changed in it.'
})

// ---- messages -----------------------------------------------------------------

export interface Media { url: string; mime: string; name?: string; w?: number; h?: number; size?: number }
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
    if (t[0] !== 'imeta' || t.includes('purpose preview')) continue // (a link preview's picture: shown in its box, docs/link-previews-v2.md)
    const f = Object.fromEntries(t.slice(1).map((p) => [p.slice(0, p.indexOf(' ')), p.slice(p.indexOf(' ') + 1)]))
    if (!f.url || !/^(https?:|idb:)/.test(f.url)) continue
    const [w, h] = (f.dim ?? '').split('x').map(Number)
    media.push({ url: f.url, mime: f.m ?? '', name: f.alt, w: w || undefined, h: h || undefined, size: Number(f.size) || undefined })
    // §23: encrypted (a private group): what opens it, for the viewer (dmfiles.encryptedMedia)
    if (f['decryption-key']) encryptedMedia.set(f.url, [['encryption-algorithm', f['encryption-algorithm'] ?? ''], ['decryption-key', f['decryption-key']], ['decryption-nonce', f['decryption-nonce'] ?? ''], ['x', f.x ?? ''], ['ox', f.ox ?? ''], ['file-type', f.m ?? '']])
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
// The setting is the group admins' latest NIP-78 event, d = belfry:inbox:<channel> and h = the
// channel (docs/mail.md, belfry-sh 73a414f). Until 2026-11-21 the d was belfry:inbox alone: a 30078
// is addressable by kind + author + d, so one admin's setting for a channel replaced theirs for
// every other (OSV's #hello lost its inbox when #sponsors got one). Read: the new d, or the old one
// whose h is the channel; written: the new d only. A card's state and who has it are NIP-32
// labels (namespace belfry/inbox) on the card, latest wins.

export const INBOX_D = 'belfry:inbox'
export const inboxD = (groupId: string) => `${INBOX_D}:${groupId}`
export const INBOX_NS = 'belfry/inbox'

/** Is the inbox setting decided here: the channel's 39001 (who its admins are) arrived. */
export const inboxKnown = (g: Group | undefined) => !!g?.adminList

export function isInbox(g: Group | undefined): boolean {
  if (!g) return false
  // A 30078 is replaced by the author's newer one with the same d: the store keeps every event it
  // ever saw, so take the newest per author + d first, as the relay does. Otherwise an old
  // belfry:inbox for this channel, cached before a newer one for another channel replaced it on
  // the relay, still counted (OSV's #hello showed on, 2026-11-22). Then: the new d counts; the old
  // d only when one of its h tags is this channel; admins only; the latest wins.
  // admins = the server's set (mail.group_admins): the channel's latest 39001 and admins by role,
  // not the 9007 creator. The groups key is in every 39001: its per-channel copies (the lead's
  // migration, OSV and belfry.sh 2026-11-22) count like any admin's. No 39001 yet: not known
  // (inboxKnown), read as off.
  if (!g.adminList) return false
  const admins = new Set([...g.adminList, ...g.roleAdmins])
  const latest = new Map<string, Ev>()
  for (const e of all(me())) {
    if (e.kind !== 30078 || !admins.has(e.pubkey)) continue
    const d = tag(e, 'd')
    if (d !== inboxD(g.id) && d !== INBOX_D) continue
    const k = e.pubkey + ' ' + d
    const was = latest.get(k)
    if (!was || byTime(was, e) < 0) latest.set(k, e)
  }
  const own = [...latest.values()].filter((e) => tag(e, 'd') === inboxD(g.id) || e.tags.some((t) => t[0] === 'h' && t[1] === g.id)).sort(byTime)
  try { return !!own.length && !!JSON.parse(own.at(-1)!.content || '{}').on } catch { return false }
}

// ---- a channel's public page on the web (<domain>/<slug>, Belfry's webthreads.py) ---------
// The admins' latest NIP-78 event, d = belfry:web:<channel>, t = belfry-web: anyone opens a thread
// there (it lands here as an e-mail card). The channel's visibility (Xavier, 2026-11-11: one
// general setting, public or members only) rides in the same event: "public" lists every thread
// on the page, "members" shows someone from outside only their own. Older events said "list".
export const WEB_D = 'belfry:web:'
export type ChannelVisibility = 'public' | 'members'
// "posts": a channel that takes one type of post only (#proposals, #events: Xavier, 2026-11-11);
// its admins pick it in the channel's settings (#issues, 2026-11-13). "thread": every post has a
// title and talk goes in its thread (a forum, GitHub-issues style); "request": the web's #requests.
export const POST_TYPES = ['thread', 'event', 'proposal', 'request'] as const
export type PostType = typeof POST_TYPES[number]
// "periods": when it was public, [from, to | null] in seconds (#issues, 2026-11-13: moving a channel
// never changes who could read what was posted before; the page shows only what was posted while
// public). A setting from before periods that says public counts as public since ever.
export type Period = [number, number | null]
export interface WebPage { on: boolean; slug: string; visibility: ChannelVisibility; posts?: PostType; periods?: Period[] }

/** When channel G was public (its page on and public), oldest first. */
export function publicPeriods(g: Group | undefined): Period[] {
  const w = webPage(g)
  if (!w) return []
  if (w.periods) return w.periods
  return w.on && w.visibility === 'public' ? [[0, null]] : []
}
/** Was a message of G posted at AT (seconds) public on the web? */
export const publicAt = (g: Group | undefined, at: number) => publicPeriods(g).some(([a, b]) => at >= a && (b === null || at < b))
// paths of the belfry's own: a channel can't take them (webthreads.RESERVED)
export const WEB_RESERVED = new Set(('api admin apply apps bridge calendar chat check claim console connect door drop events feedback follow git groups hooks internal ' +
  'market me mentions people photos programme shop signin skills static steward theme tokens vote wallet welcome invite brand ' +
  'changelog monitor privacy terms refunds rules help').split(' '))
export const slugify = (s: string) => s.toLowerCase().normalize('NFKD').replace(/[\u0300-\u036f]/g, '').replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 40)

/** The left part of a channel's e-mail address (Xavier, 2026-11-12): its plain slug, bugs@ rather
 *  than bugs-c3e5@, when no other channel answers to it; the server resolves it the same way
 *  (mail.channel_by_slug) and keeps taking the full id. */
export function mailLocal(g: Group): string {
  const plain = (x: Group) => [x.id.replace(/-[0-9a-z]{3,8}$/, ''), slugify(x.rawName)]
  const want = slugify(g.rawName) || g.id.replace(/-[0-9a-z]{3,8}$/, '')
  if (!want || want === g.id) return g.id
  if (!madeByAdmin(g)) return g.id // a plain alias for an admin's channel only (#issues 2026-11-15; mail.channel_by_slug)
  return allGroups().filter((x) => plain(x).includes(want)).length === 1 ? want : g.id
}

/** Did a belfry admin (or the belfry's own key) make G? Not known (no 9007 here): yes, as before. */
function madeByAdmin(g: Group): boolean {
  const evs = all(me())
  const make = evs.filter((e) => e.kind === 9007 && tag(e, 'h') === g.id).sort(byTime)[0]
  if (!make) return true
  const relayKey = evs.find((e) => e.kind === 39000 && tag(e, 'd') === g.id)?.pubkey
  return make.pubkey === relayKey || belfryAdminKeys().includes(make.pubkey) || (make.pubkey === me() && isAdminNow())
}

/** The groups key (belfry.json groups_key); before belfry.json answered (or in the demo), the
 *  key that signed the channel's 39000, which is the groups key. */
function groupsKeyOf(groupId: string, evs: Ev[]): string | undefined {
  return belfryGroupsKey() || evs.find((e) => e.kind === 39000 && tag(e, 'd') === groupId)?.pubkey
}

export function webPage(g: Group | undefined): WebPage | undefined {
  if (!g) return undefined
  const evs = all(me())
  // who counts, as Belfry's webthreads.py: the channel's latest 39001, the belfry's admins
  // (belfry.json admins) and the groups key (it set up the default channels: #proposals, #events)
  const gk = groupsKeyOf(g.id, evs)
  const ok = (pk: string) => !!g.adminList?.includes(pk) || belfryAdminKeys().includes(pk) || pk === gk
  const own = evs.filter((e) => e.kind === 30078 && tag(e, 'd') === WEB_D + g.id && tag(e, 'h') === g.id && ok(e.pubkey)).sort(byTime)
  try {
    const c = own.length ? JSON.parse(own.at(-1)!.content || '{}') : null
    return c ? { on: !!c.on, slug: String(c.slug || ''), visibility: c.visibility === 'members' || (!c.visibility && c.list === 'own') ? 'members' : 'public', ...((POST_TYPES as readonly string[]).includes(c.posts) ? { posts: c.posts as PostType } : {}), ...(Array.isArray(c.periods) ? { periods: cleanPeriods(c.periods) } : {}) } : undefined
  } catch { return undefined }
}

function cleanPeriods(raw: unknown[]): Period[] {
  return raw.filter((p): p is [number, number | null] => Array.isArray(p) && Number.isFinite(p[0]) && (p[1] === null || Number.isFinite(p[1])))
    .map(([a, b]) => [Math.floor(a), b === null ? null : Math.floor(b)] as Period).sort((x, y) => x[0] - y[0]).slice(-100)
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

export interface Reaction { emoji: string; who: Set<string>; mine?: Ev; /** pubkey → when (seconds) */ at: Map<string, number> }

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
    const r = m.get(emoji) ?? { emoji, who: new Set<string>(), at: new Map<string, number>() }
    r.who.add(e.pubkey)
    r.at.set(e.pubkey, e.created_at)
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

export interface TaskStep { at: number; who: string; what: 'created' | 'claim' | 'unclaim' | 'assign' | 'unassign' | 'working' | 'idle' | 'done' | 'undone' | 'comment'; text?: string }

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
      // giving it to someone, taking someone off it (SPEC 19.40, Android 2026-11-18): ["p", <key>]
      const p = tag(e, 'p')
      if ((a === 'assign' || a === 'unassign') && p && /^[0-9a-f]{64}$/.test(p)) {
        t.history.push({ at: e.created_at, who: e.pubkey, what: a, text: p })
        if (a === 'assign') t.assignee = p
        else if (t.assignee === p) t.assignee = undefined
      }
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
  /** Online meeting links (NIP-52 `r` tags: video calls, documents): "Join online". */
  links: string[]
  /** The event's own outside site (["website", url]: Event form canvas, the lead's tag, 24 Nov). */
  website?: string
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
/** An event's id: 6 of a–z, 0–9 (since 2026-11-18, Belfry ba5024b); older ones have the year before them. */
export const EVENT_D = /^(\d{4}-)?[a-z0-9]{6}$/
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
  const gs = new Map(allGroups().map((g) => [g.id, g]))
  const creators = new Map<string, string>() // group id → its creator (first 9007)
  for (const e of [...list].sort(byTime)) if (e.kind === 9007) { const h = tag(e, 'h'); if (h && !creators.has(h)) creators.set(h, authorOf(e)) }
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
      website: e.tags.find((t) => t[0] === 'website' && /^https:\/\//.test(t[1] ?? ''))?.[1],
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
/** A group's name to show, never its id (a group not loaded yet: a neutral word). */
export const nameOf = (id: string): string => group(id)?.name
  ?? (id.startsWith('ev-') || id.startsWith('ws-') ? 'this event' : isProposalId(id) ? 'this proposal' : isPodId(id) ? 'this group' : 'this channel')

/** A channel reference's name (a "#…" in a text): its name, else the kind's word, never its id
 *  (vectors/channel-refs.json: "Unknown: #channel"). */
export const refName = (id: string): string => {
  const g = group(id)
  if (g && !g.unnamed) return g.name
  return id.startsWith('ev-') || id.startsWith('ws-') ? 'event' : isProposalId(id) ? 'proposal' : isPodId(id) ? 'group' : 'channel'
}

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
  /** A new version of the website (an nsite ref): live once a belfry admin marks it Passed. */
  website?: string
  /** Its scope (its ["path", …] tags): only these parts of the site; empty = the whole site. */
  paths: string[]
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
  const gs = new Map(allGroups().map((g) => [g.id, g]))
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
      version: Number(tag(e, 'version')) || 1, status, poll, website: tag(e, 'website') || undefined,
      paths: e.tags.filter((t) => t[0] === 'path' && t[1]).map((t) => t[1]),
    })
  }
  return out
}

export const proposalFor = (groupId: string) => proposals().find((p) => p.groupId === groupId)

/** Who may change a proposal's status: its author and the admins of its chat or its parent. */
export function canDecide(p: Proposal) {
  const gs = allGroups()
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
  return [...last].filter(([pk]) => (!known || known.has(pk)) && !g?.reading.has(pk)).map(([pubkey, at]) => ({ pubkey, at })).sort((a, b) => b.at - a.at)
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
    if (tag(e, 'reading') === '1') continue // guest mode: opened the link, not joined: no line, no guest chip
    const p = tag(e, 'p')
    // a guest's join carries ["guest", <last day>] (docs/guests.md); an account that took the channel through a
    // link or a card has none: a member's join, never "Guest in …" (Xavier on belfry.brussels, 25 Nov)
    const asGuest = e.tags.some((t) => t[0] === 'guest')
    if (p) out.push({ id: e.id, pubkey: p, by: tag(e, 'invited-by'), at: e.created_at, agent: tag(e, 'agent') === '1', unsaved: tag(e, 'unsaved') === '1' && !saved.has(p) && !member.has(p), member: tag(e, 'agent') === '1' ? member.has(p) : (() => { const r = roleOf(p); return r ? r !== 'guest' : member.has(p) || !asGuest })() }) // (an agent's join: its own label, whose agent it is)
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

// ---- a channel's to-dos in parts (board channels/header-todos, to-do 8ec8c3b5, 2026-11-19):
// "6 to-dos" / "2 unclaimed, 1 up next, 2 in progress, 1 closed today". Unclaimed: open, nobody's;
// up next: assigned, not started; in progress: claimed (or working) and not put down; closed today:
// done since the viewer's midnight. The total is the sum of the parts; a part at 0 is left out.
/** "Up next" is DRAFT until Xavier confirms assign = up next / claim = working now: until then an
 *  assigned, not started to-do counts as unclaimed. */
export const UP_NEXT = false
export interface TodoParts { total: number; unclaimed: number; upNext: number; inProgress: number; closedToday: number }
export function todoParts(tasks: Task[], now = Date.now(), upNext = UP_NEXT): TodoParts {
  const midnight = new Date(now); midnight.setHours(0, 0, 0, 0)
  const since = Math.floor(+midnight / 1000)
  const p: TodoParts = { total: 0, unclaimed: 0, upNext: 0, inProgress: 0, closedToday: 0 }
  for (const t of tasks) {
    if (t.done) {
      const at = [...t.history].reverse().find((s) => s.what === 'done')?.at ?? 0
      if (at >= since) p.closedToday++
      continue
    }
    const last = [...t.history].reverse().find((s) => ['claim', 'unclaim', 'assign', 'unassign', 'idle', 'working'].includes(s.what))?.what
    const started = last === 'claim' || last === 'working'
    if (t.assignee && started) p.inProgress++
    else if (t.assignee && upNext) p.upNext++
    else p.unclaimed++
  }
  p.total = p.unclaimed + p.upNext + p.inProgress + p.closedToday
  return p
}
export const todoPartsText = (p: TodoParts) => [
  [p.unclaimed, 'unclaimed'], [p.upNext, 'up next'], [p.inProgress, 'in progress'], [p.closedToday, 'closed today'],
].filter(([n]) => (n as number) > 0).map(([n, w]) => `${n} ${w}`).join(', ')
export const todoTotalText = (p: TodoParts) => `${p.total} ${p.total === 1 ? 'to-do' : 'to-dos'}`

/** A link to a thread reply (…/#/c/<id>/m/<reply>, board channels/message-link): the thread's root. */
export function replyRoot(groupId: string, id: string): string | undefined {
  const e = all(me()).find((x) => x.id === id)
  if (!e || e.kind !== 1111 || tag(e, 'h') !== groupId) return undefined
  return e.tags.find((t) => t[0] === 'E')?.[1]
}
/** Was message ID of GROUP deleted (by its author, or removed by a moderator)? */
export const deletedMessage = (_groupId: string, id: string) => deleted(all(me())).has(id)

/** A private group I'm not in (nor run): not listed to me, no Join or Apply (the lead, 2026-11-22). */
/** Xavier, 2026-11-23 (the lead): in a channel (a topic) its people are followers, not members;
 *  groups, events and proposals keep members. The belfry's own members stay members everywhere. */
export const followsIt = (g?: { isPod: boolean; isEvent: boolean; isProposal: boolean }) => !!g && !g.isPod && !g.isEvent && !g.isProposal
/** "12 followers" in a channel, "8 members" elsewhere. */
export const peopleCount = (g: { isPod: boolean; isEvent: boolean; isProposal: boolean }, n: number) => followsIt(g) ? `${n} ${n === 1 ? 'follower' : 'followers'}` : `${n} ${n === 1 ? 'member' : 'members'}`
export const hiddenFromMe = (g: Group | undefined): boolean => !!g && g.isPod && g.inviteOnly && !g.members.has(me()) && !g.admins.has(me())

/** Someone joined the belfry (docs/one-shape.md "Joining the belfry", 2026-11-22): the groups key's
 *  9000 in the main channel with ["joined", "belfry"] (+ ["name"], ["agent", "1"]), once per person.
 *  A 9000 without `joined` draws nothing here (guests keep their own line, guestJoins). Someone
 *  removed since (a later 9001) who never wrote there keeps no line. */
/** A name to show as text: no control or direction characters, trimmed, 100 at most. */
export const plainLabel = (t: string) => t.replace(/[\u0000-\u001f\u007f\u200e\u200f\u202a-\u202e\u2066-\u2069]/g, '').trim().slice(0, 100)
export interface BelfryJoin { id: string; pubkey: string; name: string; agent: boolean; at: number }
export function belfryJoins(groupId: string): BelfryJoin[] {
  if (groupId !== 'general') return []
  const byPk = new Map<string, BelfryJoin>()
  const gone = new Map<string, number>()
  const wrote = new Set<string>()
  for (const e of all(me())) {
    if (tag(e, 'h') !== groupId) continue
    if (e.kind === 9000 && tag(e, 'joined') === 'belfry') {
      const p = tag(e, 'p')
      if (!p || !/^[0-9a-f]{64}$/.test(p)) continue
      const was = byPk.get(p)
      if (was && was.at <= e.created_at) continue // once per person: the first one
      // the name tag is shown as text only: no control or direction characters, 100 at most
      const name = plainLabel(tag(e, 'name') ?? '')
      byPk.set(p, { id: e.id, pubkey: p, name, agent: tag(e, 'agent') === '1', at: e.created_at })
    } else if (e.kind === 9001) { const p = tag(e, 'p'); if (p) gone.set(p, Math.max(gone.get(p) ?? 0, e.created_at)) }
    else if (e.kind === 9 || e.kind === 1111) wrote.add(e.pubkey)
  }
  return [...byPk.values()].filter((j) => wrote.has(j.pubkey) || (gone.get(j.pubkey) ?? 0) < j.at).sort((a, b) => a.at - b.at)
}
