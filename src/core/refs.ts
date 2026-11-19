// References to a channel, group, event or proposal of this belfry in a message (SPEC 19.44;
// boards channels/link-preview-end + -inline, to-do c92a9f45, 2026-11-19; vectors/references.json,
// ported step for step from belfry-android tools/vectors/references.mjs parts()).
// A reference: this belfry's chat link https://chat.<domain>/#/c/<id> (also
// https://<domain>/chat/#/c/<id>; never with /m/<event>), its event pages
// https://chat.<domain>/events/<link> and https://<domain>/events/<slug-or-d> (not /t/<code>), or
// nostr:naddr1… of kind 39000 (group id), 31922 / 31923 (event coord), 30023 (proposal coord).
// The last one, when the trimmed text ends with it, comes out of the text as a box; the others
// are names inside the text. Anything else stays as written.
import { nip19 } from 'nostr-tools'
import { BELFRY } from '../config'
import { calendar, deletedGroup, eventOfGroup, group, isPodId, isProposalId, proposals, type CalEvent, type Group, type Proposal } from './model'

export type Ref = { type: 'group'; id: string } | { type: 'event'; link?: string; coord?: string } | { type: 'proposal'; coord: string }
export type Part = { text: string } | { ref: Ref } | { box: Ref }

const URL = /https?:\/\/[^\s<>()]+[^\s<>().,;:!?'"]/g // = markdown.tsx URL_RE
const NADDR = /nostr:(naddr1[02-9ac-hj-np-z]{20,400})/g
const esc = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
const res = new Map<string, { channel: RegExp; event: RegExp }>()
function patterns(domain: string) {
  let p = res.get(domain)
  if (!p) {
    const D = esc(domain)
    p = {
      channel: new RegExp(`^https://(?:chat\\.${D}|${D}/chat)/#/c/([^/?#]{1,120})/?$`),
      event: new RegExp(`^https://(?:chat\\.${D}/(?:#/)?events|${D}/events)/([A-Za-z0-9_-]{1,80})/?$`),
    }
    res.set(domain, p)
  }
  return p
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
  let m = p.channel.exec(token)
  if (m) { try { return { type: 'group', id: decodeURIComponent(m[1]) } } catch { return null } }
  m = p.event.exec(token)
  if (m) return { type: 'event', link: m[1] }
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

export type RefKind = 'channel' | 'group' | 'event' | 'proposal'
export interface Resolved {
  kind: RefKind
  state: 'ok' | 'loading' | 'locked' | 'deleted'
  group?: Group
  event?: CalEvent
  proposal?: Proposal
  /** where a tap goes (a hash route, no '#/') */
  route?: string
}

const kindOfId = (id: string): RefKind => (isProposalId(id) ? 'proposal' : isPodId(id) ? 'group' : id.startsWith('ev-') || id.startsWith('ws-') ? 'event' : 'channel')
const eventRoute = (e: CalEvent) => 'events/' + encodeURIComponent(e.slug || e.d)

/** What a reference points at, as far as this device knows; ME = my key (membership). */
export function resolveRef(r: Ref, me: string): Resolved {
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
  if (g.closed && !g.members.has(me) && !g.admins.has(me)) return { kind, state: 'locked', group: g }
  const e = g.isEvent ? eventOfGroup(g.id) : undefined
  return { kind, state: 'ok', group: g, event: e, proposal: p, route: e?.isNew ? eventRoute(e) : 'c/' + encodeURIComponent(g.id) }
}

/** The kind's word while a name is not known (SPEC 19.44; "#channel" as channel-refs.json). */
export const kindWord = (k: RefKind) => (k === 'channel' ? '#channel' : k === 'group' ? '#Group' : k === 'event' ? 'Event' : 'Proposal')
/** Grey words for something I can't open, inside the text. */
export const lockedWords = (k: RefKind) => (k === 'channel' ? 'a members-only channel' : k === 'group' ? 'a closed group' : k === 'event' ? 'a members-only event' : 'a members-only proposal')
export const deletedWords = (k: RefKind) => `a deleted ${k}`

/** The coordinates of the events and proposals TEXT names, for `a` tags on send (SPEC 19.44). */
export function refCoords(text: string): string[] {
  const out = new Set<string>()
  for (const p of parts(text)) {
    const r = 'ref' in p ? p.ref : 'box' in p ? p.box : undefined
    if (r && (r.type === 'event' || r.type === 'proposal') && r.coord) out.add(r.coord)
  }
  return [...out]
}
