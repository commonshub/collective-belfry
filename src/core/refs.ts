// References to a channel, group, event or proposal of this belfry in a message (SPEC 19.44;
// boards channels/link-preview-end + -inline, to-do c92a9f45, 2026-11-19; vectors/references.json,
// ported step for step from belfry-android tools/vectors/references.mjs parts()).
// A reference: any address of this belfry's chat, https://chat.<domain>/#/… or
// https://<domain>/chat/#/… (segments percent-encoded or not; #lead 2026-11-20, Xavier: "links to
// things in the belfry never show as a raw link"):
//   #/c/<id> and its pages (/group, /info, /about, /members, /photos, /files, /todos, /log, /events,
//   /t/<topic>) = the channel or group; #/c/<id>/todo/<task> = a to-do; #/c/<id>/m/<event> and
//   #/c/<id>/thread/<event> = a message; #/cal/<31922|31923 coord> (older event addresses) and
//   #/events/<link> = an event, #/events/<link>/m/<event> = a message in its chat; #/u/<npub> = a
//   person. Also the event pages https://chat.<domain>/events/<link> and
//   https://<domain>/events/<slug-or-d> (never /t/<code>: a ticket stays a link), the drop box
//   links https://<domain>/drop/<token>, and nostr:naddr1… of kind 39000 (group id), 31922 / 31923
//   (event coord), 30023 (proposal coord).
// The last one, when the trimmed text ends with it, comes out of the text as a box; the others
// are names inside the text. Anything else stays as written.
import { nip19 } from 'nostr-tools'
import { BELFRY } from '../config'
import { calendar, deletedGroup, displayName, eventByLinkLocal, eventOfGroup, group, isPodId, isProposalId, proposals, tasklist, type CalEvent, type Group, type Proposal, type Task } from './model'
import { all, type Ev } from './store'
const tag = (e: Ev, k: string) => e.tags.find((t) => t[0] === k)?.[1]

export type Ref = { type: 'group'; id: string } | { type: 'event'; link?: string; coord?: string } | { type: 'proposal'; coord: string }
  | { type: 'todo'; group: string; task: string } | { type: 'message'; id: string; group?: string; link?: string }
  | { type: 'person'; pubkey: string } | { type: 'drop'; token: string }
export type Part = { text: string } | { ref: Ref } | { box: Ref }

const URL = /https?:\/\/[^\s<>()]+[^\s<>().,;:!?'"]/g // = markdown.tsx URL_RE
const NADDR = /nostr:(naddr1[02-9ac-hj-np-z]{20,400})/g
const esc = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
const res = new Map<string, { hash: RegExp; event: RegExp; drop: RegExp }>()
function patterns(domain: string) {
  let p = res.get(domain)
  if (!p) {
    const D = esc(domain)
    p = {
      hash: new RegExp(`^https://(?:chat\\.${D}|${D}/chat)/#/([^?#\\s]{1,600})$`),
      event: new RegExp(`^https://(?:chat\\.${D}|${D})/events/([A-Za-z0-9_-]{1,80})/?$`),
      drop: new RegExp(`^https://${D}/drop/([A-Za-z0-9_-]{8,100})/?$`),
    }
    res.set(domain, p)
  }
  return p
}

const HEX = /^[0-9a-f]{64}$/
const LINK = /^[A-Za-z0-9_-]{1,80}$/
const COORD = /^3192[23]:[0-9a-f]{64}:[^\s]{1,200}$/
/** A channel's pages that show the channel itself (App.tsx). */
const GROUP_PAGES = new Set(['group', 'info', 'about', 'members', 'images', 'photos', 'files', 'todos', 'log', 'events'])

/** A chat route (after "#/") as a reference, or null (the app's places, a ticket, …). */
function routeRef(path: string): Ref | null {
  const raw = path.replace(/\/+$/, '').split('/')
  let seg: string[]
  try { seg = raw.map((x) => decodeURIComponent(x)) } catch { return null }
  if (seg.some((x) => !x || x.length > 200)) return null
  const [a, b, c, d, ...more] = seg
  if (a === 'c' && b && b.length <= 120) {
    if (!c) return { type: 'group', id: b }
    if (c === 'todo' && d && !more.length && /^[A-Za-z0-9_-]{1,100}$/.test(d)) return { type: 'todo', group: b, task: d }
    if ((c === 'm' || c === 'thread') && d && HEX.test(d) && (!more.length || (more.length === 1 && more[0] === 'reply'))) return { type: 'message', group: b, id: d }
    if (GROUP_PAGES.has(c) && (!d || (c === 'info' && d === 'members' && !more.length))) return { type: 'group', id: b }
    if (c === 't' && d && !more.length) return { type: 'group', id: b }
    return null
  }
  if (a === 'cal' && b && !c && COORD.test(b)) return { type: 'event', coord: b }
  if (a === 'events' && b && LINK.test(b)) {
    if (!c) return { type: 'event', link: b }
    if (c === 'm' && d && HEX.test(d) && !more.length) return { type: 'message', link: b, id: d }
    return null // a ticket (/t/<code>) stays a link
  }
  if (a === 'u' && b && !c) {
    if (HEX.test(b)) return { type: 'person', pubkey: b }
    try { const x = nip19.decode(b); if (x.type === 'npub') return { type: 'person', pubkey: x.data as string } } catch { /* not a key */ }
  }
  return null
}

/** What a token (a URL or nostr:naddr1…) points to, or null when it isn't a reference. */
export function target(token: string, domain = BELFRY.domain): Ref | null {
  if (token.startsWith('nostr:')) {
    let d: ReturnType<typeof nip19.decode>
    try { d = nip19.decode(token.slice(6)) } catch { return null }
    if (d.type !== 'naddr') return null
    const { kind, pubkey, identifier } = d.data
    if (kind === 39000) return { type: 'group', id: identifier }
    if (kind === 31922 || kind === 31923) return { type: 'event', coord: `${kind}:${pubkey}:${identifier}` }
    if (kind === 30023) return { type: 'proposal', coord: `${kind}:${pubkey}:${identifier}` }
    return null
  }
  const p = patterns(domain)
  let m = p.hash.exec(token)
  if (m) return routeRef(m[1])
  m = p.event.exec(token)
  if (m) return { type: 'event', link: m[1] }
  m = p.drop.exec(token)
  if (m) return { type: 'drop', token: m[1] }
  return null
}

/** TEXT as parts: {text}, {ref} (inside the text: a name), {box} (the last one, at the very end). */
export function parts(text: string, domain = BELFRY.domain): Part[] {
  const found: { start: number; end: number; t: Ref }[] = []
  for (const re of [NADDR, URL]) for (const m of text.matchAll(re)) {
    const t = target(m[0], domain); if (t) found.push({ start: m.index!, end: m.index! + m[0].length, t })
  }
  found.sort((a, b) => a.start - b.start)
  const out: Part[] = []
  const push = (s: string) => { if (!s) return; const l = out.at(-1); if (l && 'text' in l) l.text += s; else out.push({ text: s }) }
  const trimmedEnd = text.replace(/\s+$/u, '').length
  let last = 0
  found.forEach((f, i) => {
    if (f.start < last) return
    push(text.slice(last, f.start))
    if (i === found.length - 1 && f.end === trimmedEnd) {
      const l = out.at(-1)
      if (l && 'text' in l) { l.text = l.text.replace(/\s+$/u, ''); if (!l.text) out.pop() }
      out.push({ box: f.t })
    } else out.push({ ref: f.t })
    last = f.end
  })
  if (!out.length || !('box' in out.at(-1)!)) push(text.slice(last))
  return out
}

/** The text before the box and the box, when the message ends with a reference. */
export function endRef(text: string): { rest: string; ref: Ref } | undefined {
  const p = parts(text)
  const b = p.at(-1)
  if (!b || !('box' in b)) return undefined
  // the rest as written: the text up to where the box's token starts, trimmed at the end
  const t = text.replace(/\s+$/u, '')
  let start = t.length
  for (const re of [NADDR, URL]) for (const m of t.matchAll(re)) if (m.index! + m[0].length === t.length) start = Math.min(start, m.index!)
  return { rest: t.slice(0, start).replace(/\s+$/u, ''), ref: b.box }
}

export type RefKind = 'channel' | 'group' | 'event' | 'proposal' | 'todo' | 'message' | 'person' | 'drop'
export interface Resolved {
  kind: RefKind
  state: 'ok' | 'loading' | 'locked' | 'deleted'
  group?: Group
  event?: CalEvent
  proposal?: Proposal
  task?: Task
  /** a message: its event */
  msg?: Ev
  pubkey?: string
  /** a drop box: until when it takes files (unix seconds) */
  until?: number
  /** where a tap goes (a hash route, no '#/') */
  route?: string
  /** a page outside the chat (a drop box) */
  href?: string
}

let evIndex: { n: number; first?: Ev; map: Map<string, Ev> } = { n: -1, map: new Map() }
function eventById(id: string): Ev | undefined {
  const list = all()
  if (list.length !== evIndex.n || list[0] !== evIndex.first) evIndex = { n: list.length, first: list[0], map: new Map(list.map((e) => [e.id, e])) }
  return evIndex.map.get(id)
}
const shut = (g: Group, me: string) => g.closed && !g.members.has(me) && !g.admins.has(me)

const kindOfId = (id: string): RefKind => (isProposalId(id) ? 'proposal' : isPodId(id) ? 'group' : id.startsWith('ev-') || id.startsWith('ws-') ? 'event' : 'channel')
const eventRoute = (e: CalEvent) => 'events/' + encodeURIComponent(e.slug || e.d)

/** What a reference points at, as far as this device knows; ME = my key (membership). */
export function resolveRef(r: Ref, me: string): Resolved {
  if (r.type === 'person') return { kind: 'person', state: 'ok', pubkey: r.pubkey, route: 'u/' + nip19.npubEncode(r.pubkey) }
  if (r.type === 'drop') {
    const e = all().find((x) => x.kind === 30078 && tag(x, 'd') === 'drop:' + r.token)
    if (!e) return { kind: 'drop', state: 'loading', href: 'https://' + BELFRY.domain + '/drop/' + r.token }
    const g = group(tag(e, 'h') ?? '')
    const until = Number(tag(e, 'expiration')) || undefined
    const ended = (until && until * 1000 < Date.now()) || all().some((x) => x.kind === 5 && x.tags.some((t) => t[0] === 'e' && t[1] === e.id))
    return { kind: 'drop', state: ended ? 'deleted' : 'ok', group: g, until, href: 'https://' + BELFRY.domain + '/drop/' + r.token }
  }
  if (r.type === 'todo') {
    const g = group(r.group)
    if (!g) return { kind: 'todo', state: deletedGroup(r.group) ? 'deleted' : 'loading' }
    if (shut(g, me)) return { kind: 'todo', state: 'locked', group: g }
    const t = tasklist(r.group).tasks.find((x) => x.id === r.task)
    return t ? { kind: 'todo', state: 'ok', group: g, task: t, route: `c/${encodeURIComponent(g.id)}/todo/${encodeURIComponent(t.id)}` } : { kind: 'todo', state: 'loading', group: g }
  }
  if (r.type === 'message') {
    const ev = r.link ? eventByLinkLocal(r.link) : undefined
    const gid = r.group ?? ev?.groupId
    const g = gid ? group(gid) : undefined
    if (gid && !g && deletedGroup(gid)) return { kind: 'message', state: 'deleted' }
    if (g && shut(g, me)) return { kind: 'message', state: 'locked', group: g }
    const m = eventById(r.id)
    if (!m || !g) return { kind: 'message', state: 'loading', group: g }
    if (all().some((x) => x.kind === 5 && x.pubkey === m.pubkey && x.tags.some((t) => t[0] === 'e' && t[1] === m.id))) return { kind: 'message', state: 'deleted', group: g }
    const e = eventOfGroup(g.id)
    return { kind: 'message', state: 'ok', group: g, msg: m, event: e, route: (e || ev ? eventRoute((e ?? ev)!) : 'c/' + encodeURIComponent(g.id)) + '/m/' + m.id }
  }
  if (r.type === 'event') {
    const e = (r.coord && calendar().find((x) => x.coord === r.coord)) || (r.link ? calendar().find((x) => x.d === r.link || x.slug === r.link) : undefined)
    return e ? { kind: 'event', state: 'ok', event: e, route: eventRoute(e) } : { kind: 'event', state: 'loading' }
  }
  let id: string
  let p: Proposal | undefined
  if (r.type === 'proposal') {
    p = proposals().find((x) => x.coord === r.coord)
    if (!p) return { kind: 'proposal', state: 'loading' }
    id = p.groupId
  } else id = r.id
  const g = group(id)
  if (!g) {
    if (p) return { kind: 'proposal', state: 'ok', proposal: p, route: 'c/' + encodeURIComponent(id) }
    return { kind: kindOfId(id), state: deletedGroup(id) ? 'deleted' : 'loading' }
  }
  const kind: RefKind = g.isEvent ? 'event' : g.isProposal || p ? 'proposal' : g.isPod ? 'group' : 'channel'
  // closed and I am not in it: no name, nothing to open (a guest, someone outside the group)
  if (shut(g, me)) return { kind, state: 'locked', group: g }
  const e = g.isEvent ? eventOfGroup(g.id) : undefined
  return { kind, state: 'ok', group: g, event: e, proposal: p, route: e ? eventRoute(e) : 'c/' + encodeURIComponent(g.id) }
}

/** The kind's word while a name is not known (SPEC 19.44; "#channel" as channel-refs.json). */
const WORD: Record<RefKind, string> = { channel: '#channel', group: '#Group', event: 'Event', proposal: 'Proposal', todo: 'To-do', message: 'Message', person: 'Someone', drop: 'Drop box' }
export const kindWord = (k: RefKind) => WORD[k]
/** Grey words for something I can't open, inside the text. */
const LOCKED: Record<RefKind, string> = { channel: 'a members-only channel', group: 'a closed group', event: 'a members-only event', proposal: 'a members-only proposal', todo: 'a to-do in a closed channel', message: 'a message in a closed channel', person: 'someone', drop: 'a drop box' }
export const lockedWords = (k: RefKind) => LOCKED[k]
export const deletedWords = (k: RefKind) => (k === 'drop' ? 'an ended drop box' : k === 'todo' ? 'a deleted to-do' : `a deleted ${k}`)

/** The coordinates of the events and proposals TEXT names, for `a` tags on send (SPEC 19.44). */
export function refCoords(text: string): string[] {
  const out = new Set<string>()
  for (const p of parts(text)) {
    const r = 'ref' in p ? p.ref : 'box' in p ? p.box : undefined
    if (r && (r.type === 'event' || r.type === 'proposal') && r.coord) out.add(r.coord)
  }
  return [...out]
}

// ---- plain text (one-line previews: the channel list, notifications, the pinned line, reply
// quotes, search results, the DM list; lead 2026-11-20): every reference reads as the thing's
// name, never its address; a thing not known on this device reads as a generic word.

/** The key a name is known by (vectors/references.json `known`). */
export function refKey(r: Ref): string {
  switch (r.type) {
    case 'group': return 'group:' + r.id
    case 'event': return 'event:' + (r.coord ?? r.link)
    case 'proposal': return 'proposal:' + r.coord
    case 'todo': return `todo:${r.group}/${r.task}`
    case 'message': return 'message:' + r.id
    case 'person': return 'person:' + r.pubkey
    case 'drop': return 'drop:' + r.token
  }
}

/** The word for a thing not known (yet) on this device: never its address. A channel keeps
 *  "#channel" (vectors/channel-refs.json, SPEC 19.44). */
export function genericWord(r: Ref): string {
  if (r.type === 'group') {
    const k = kindOfId(r.id)
    return k === 'event' ? 'an event' : k === 'proposal' ? 'a proposal' : k === 'group' ? '#group' : '#channel'
  }
  return { event: 'an event', proposal: 'a proposal', todo: 'a to-do', message: 'a message', person: 'someone', drop: 'a drop box' }[r.type]
}

/** TEXT as one line of plain text: mentions as NPUB, each reference as NAME(r) (else its generic word). */
export function plainOf(text: string, name: (r: Ref) => string | undefined, domain = BELFRY.domain, npub = '@…'): string {
  let out = ''
  for (const p of parts(text, domain)) {
    if ('text' in p) out += p.text.replace(/nostr:npub1[02-9ac-hj-np-z]+/g, npub)
    else {
      const r = 'ref' in p ? p.ref : p.box
      if ('box' in p && out && !/\s$/.test(out)) out += ' '
      out += name(r) ?? genericWord(r)
    }
  }
  return out
}

/** A reference's name in plain text, as far as this device knows (else undefined). */
export function plainName(r: Ref, me: string): string | undefined {
  const x = resolveRef(r, me)
  if (x.state === 'locked') return lockedWords(x.kind)
  if (x.state === 'deleted') return deletedWords(x.kind)
  if (x.state !== 'ok') return undefined
  switch (x.kind) {
    case 'person': return '@' + displayName(x.pubkey!)
    case 'todo': return (x.task!.done ? '☑ ' : '☐ ') + x.task!.title
    case 'message': return 'a message from ' + displayName(x.msg!.pubkey)
    case 'drop': return x.group && !x.group.unnamed ? 'the drop box of #' + x.group.name : undefined
    case 'event': return x.event?.title || (x.group && !x.group.unnamed ? x.group.name : undefined)
    case 'proposal': return x.proposal?.title || (x.group && !x.group.unnamed ? x.group.name : undefined)
    default: return x.group && !x.group.unnamed ? '#' + x.group.name : undefined
  }
}
