// Belfry events (work/events-design/PROTOCOL.md): tickets, who holds one, applications, the
// actions of hosts and guests, and what the belfry's API says to those who can't read the
// community relay (a channel's guests, people with no account).
//
//   ticket type   NIP-99 kind 30402, `d` = <event d>:<id>, `a` = the event, `h` = ev-<d>; no
//                 `price` in v1. None defined: the implicit free open ticket <d>:free.
//   holding one   kind 9000 in ev-<d> with `ticket` (a later 9001 takes it back)
//   application   kind 9021 in ev-<d>, `ticket`, content = the answer; `waitlist` when full
//   RSVP          NIP-52 31925 (`ticket` when one is held); a guest's goes on the groups relay
//   status        NIP-32 label belfry/event-status (cancelled | scheduled) by a host
import { nip19 } from 'nostr-tools'
import { sha256 } from '@noble/hashes/sha256'
import { bytesToHex } from '@noble/hashes/utils'
import { BELFRY, TZ } from '../config'
import { calendar, eventByLinkLocal, EVENT_D, EVENT_STATUS_NS, eventUrl, group, groups, type CalEvent, type Rsvp, type Visibility } from './model'
import { all, addEvents, touch, type Ev } from './store'
import { currentSigner, enqueue, isLocalOnly, me, newId, signedFetch, uploadBlob } from './sync'
import { isGuest } from './invite'
import { isAdmin } from './access'

const tag = (e: Ev, k: string) => e.tags.find((t) => t[0] === k)?.[1]

// ---- ids, URLs ---------------------------------------------------------------------------

const ALPHA = 'abcdefghijklmnopqrstuvwxyz0123456789'
/** A new event's id: the year it is made in (UTC) and 6 random characters. Never recomputed. */
export function newEventD(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(6))
  return `${new Date().getUTCFullYear()}-${[...bytes].map((b) => ALPHA[b % 36]).join('')}`
}
export const SLUG = /^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$/
const RESERVED = new Set(['new', 'slug', 'feed', 'past', 'mine'])
export function slugProblem(s: string): string | undefined {
  if (!SLUG.test(s)) return '3 to 40 of a–z, 0–9 and -'
  if (EVENT_D.test(s)) return 'That is shaped like an event’s own id'
  if (RESERVED.has(s)) return 'That word is kept'
  return undefined
}
export const slugOf = (title: string) => title.toLowerCase().normalize('NFKD').replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 40).replace(/-+$/, '')

// ---- what the belfry's API answers (GET /api/v1/events/<link>) ---------------------------

export interface ApiTicket { d: string; id: string; title: string; summary: string; quantity: number | null; left: number | null; closed: boolean; prompt: string; members: boolean; position: number; implicit: boolean }
export interface ApiEvent {
  d: string; coord: string; kind: number; group: string; link: string; url: string; title: string; description: string
  all_day: boolean; start: string | null; end: string | null; tz: string; location: string; online: boolean; links: string[]
  cover: string | null; visibility: Visibility; status: 'scheduled' | 'cancelled'; status_reason: string
  hosts: { pubkey: string; name: string }[]; tickets: ApiTicket[]; going: number; community: string
  me: { host: boolean; ticket: string | null; applied: boolean; waitlist: boolean; declined: boolean; rsvp: Rsvp | null; member: boolean } | null
}

const remote = new Map<string, { at: number; ev?: ApiEvent; error?: string }>()
const asking = new Set<string>()

/** The event as the belfry tells it (cached a minute); undefined while asking. */
export function remoteEvent(link: string, signed = true): ApiEvent | undefined {
  const got = remote.get(link)
  if (got && Date.now() - got.at < 60_000) return got.ev
  if (!asking.has(link) && !isLocalOnly()) {
    asking.add(link)
    void fetchEvent(link, signed).then((ev) => remote.set(link, { at: Date.now(), ev }), (e) => remote.set(link, { at: Date.now(), error: String(e?.message ?? e) }))
      .finally(() => { asking.delete(link); touch() })
  }
  return got?.ev
}
export const remoteError = (link: string) => remote.get(link)?.error

export async function fetchEvent(link: string, signed = true): Promise<ApiEvent> {
  const url = `${BELFRY.api}/events/${encodeURIComponent(link)}`
  const r = signed && me() ? await signedFetch(url) : await fetch(url)
  const d = await r.json().catch(() => ({}))
  if (r.status === 403) throw new Error('members_only')
  if (!r.ok) throw new Error(d.message || 'not_found')
  return d as ApiEvent
}

/** An event read from the API, in the chat's own shape (for guests, and cards they get). */
export function fromApi(a: ApiEvent): CalEvent {
  const [kind, creator] = a.coord.split(':')
  const allDay = Number(kind) === 31922
  const parse = (v: string | null) => (!v ? undefined : allDay ? new Date(v.slice(0, 10) + 'T00:00:00') : new Date(v))
  const fake: Ev = { id: 'api-' + a.d, pubkey: creator, kind: Number(kind), created_at: 0, content: a.description, tags: [['d', a.d]], sig: '' }
  return {
    coord: a.coord, d: a.d, ev: fake, versions: [fake], title: a.title, description: a.description, location: a.location || undefined,
    allDay, start: parse(a.start) ?? new Date(), end: parse(a.end), tz: a.tz || undefined, groupId: a.group, links: a.links,
    channels: [], rsvps: new Map(a.me?.rsvp ? [[me(), a.me.rsvp]] : []), rsvpEvents: new Map(), isNew: true, creator, meeting: false, invited: [],
    hosts: new Set(a.hosts.map((h) => h.pubkey)), cover: a.cover ?? undefined, visibility: a.visibility, slug: a.link !== a.d ? a.link : undefined,
    status: a.status, statusReason: a.status_reason || undefined, remote: true,
  }
}

/** An event by its URL's last part: ours when the relay gave it to us, else the belfry's answer. */
export function eventByLink(link: string): { e?: CalEvent; api?: ApiEvent; error?: string } {
  const e = eventByLinkLocal(link)
  if (e) return { e }
  const api = remoteEvent(link)
  return api ? { e: fromApi(api), api } : { error: remoteError(link) ?? (isLocalOnly() ? 'not_found' : undefined) }
}

// ---- tickets and the people of an event ---------------------------------------------------

export interface Ticket { d: string; id: string; title: string; summary: string; quantity?: number; closed: boolean; prompt: string; members: boolean; position: number; implicit: boolean; ev?: Ev }

export const freeTicket = (e: Pick<CalEvent, 'd'>): Ticket => ({ d: `${e.d}:free`, id: 'free', title: 'RSVP', summary: '', closed: false, prompt: '', members: false, position: 0, implicit: true })

export function tickets(e: CalEvent, api?: ApiEvent): Ticket[] {
  if (api || e.remote) {
    const a = api ?? remoteEvent(e.d)
    if (a) return a.tickets.map((t) => ({ ...t, quantity: t.quantity ?? undefined }))
  }
  const list = all(me())
  const latest = new Map<string, Ev>()
  for (const x of list) {
    if (x.kind !== 30402 || tag(x, 'a') !== e.coord || !e.hosts.has(x.pubkey)) continue
    const d = tag(x, 'd') ?? ''
    if (!d.startsWith(e.d + ':')) continue
    const had = latest.get(d)
    if (!had || had.created_at < x.created_at) latest.set(d, x)
  }
  const gone = (d: string, at: number) => list.some((x) => x.kind === 5 && e.hosts.has(x.pubkey) && x.created_at >= at && x.tags.some((t) => t[0] === 'a' && (t[1] ?? '').endsWith(':' + d)))
  const out: Ticket[] = []
  for (const [d, x] of latest) {
    if (gone(d, x.created_at)) continue
    const q = Number(tag(x, 'quantity'))
    out.push({
      d, id: d.slice(e.d.length + 1), title: tag(x, 'title') || 'Ticket', summary: tag(x, 'summary') ?? '',
      quantity: tag(x, 'quantity') !== undefined && q >= 0 ? q : undefined, closed: x.tags.some((t) => t[0] === 'closed'),
      prompt: tag(x, 'prompt') ?? '', members: x.tags.some((t) => t[0] === 'members'), position: Number(tag(x, 'position')) || 0, implicit: false, ev: x,
    })
  }
  return out.length ? out.sort((a, b) => a.position - b.position || a.title.localeCompare(b.title)) : [freeTicket(e)]
}

export interface Application { pubkey: string; answer: string; ticket?: string; waitlist: boolean; at: number; ev: Ev }
export interface People {
  holders: Map<string, string> // pubkey → ticket d
  applications: Application[]
  declined: Set<string>
}

/** Who holds a ticket (latest 9000 with `ticket`, not taken back), who applied, who was declined. */
export function people(e: CalEvent): People {
  const list = all(me()).filter((x) => tag(x, 'h') === e.groupId && (x.kind === 9000 || x.kind === 9001 || x.kind === 9021 || x.kind === 9022)).sort((a, b) => a.created_at - b.created_at)
  const put = new Map<string, Ev>()
  const out = new Map<string, number>()
  const decided = new Map<string, number>()
  const applied = new Map<string, Ev>()
  const left = new Map<string, number>()
  for (const x of list) {
    const p = tag(x, 'p')
    if (x.kind === 9000 && p) { decided.set(p, x.created_at); if (tag(x, 'ticket')) put.set(p, x) }
    if (x.kind === 9001 && p) { decided.set(p, x.created_at); out.set(p, x.created_at) }
    if (x.kind === 9021) applied.set(x.pubkey, x)
    if (x.kind === 9022) left.set(x.pubkey, x.created_at)
  }
  const holders = new Map<string, string>()
  for (const [p, x] of put) if (x.created_at > (out.get(p) ?? 0)) holders.set(p, tag(x, 'ticket')!)
  const applications: Application[] = []
  for (const [p, x] of applied) {
    if (holders.has(p) || (decided.get(p) ?? 0) >= x.created_at || (left.get(p) ?? 0) >= x.created_at) continue
    applications.push({ pubkey: p, answer: x.content, ticket: tag(x, 'ticket'), waitlist: x.tags.some((t) => t[0] === 'waitlist'), at: x.created_at, ev: x })
  }
  const declined = new Set([...out].filter(([p, at]) => !holders.has(p) && at >= (decided.get(p) ?? 0)).map(([p]) => p))
  return { holders, applications: applications.sort((a, b) => a.at - b.at), declined }
}

export const taken = (p: People, ticketD: string) => [...p.holders.values()].filter((t) => t === ticketD).length
export const placesLeft = (t: Ticket, p: People) => (t.quantity === undefined ? undefined : Math.max(0, t.quantity - taken(p, t.d)))

/** Going: ticket holders who did not say otherwise, and members' RSVPs on the free ticket. */
export function goingList(e: CalEvent, p = people(e)): string[] {
  const keys = new Set([...p.holders.keys(), ...[...e.rsvps].filter(([, s]) => s === 'accepted').map(([k]) => k)])
  return [...keys].filter((k) => (e.rsvps.get(k) ?? 'accepted') === 'accepted')
}

export type MyState = 'host' | 'ticket' | 'maybe' | 'cant' | 'applied' | 'waitlist' | 'declined' | 'none'
/** Where I stand, first match (PROTOCOL.md §2). */
export function myState(e: CalEvent, p = people(e), api?: ApiEvent): MyState {
  const k = me()
  const a = api?.me
  const rsvp = e.rsvps.get(k) ?? a?.rsvp ?? undefined
  if (p.declined.has(k) || a?.declined) return 'declined'
  if (p.holders.has(k) || a?.ticket) return rsvp === 'tentative' ? 'maybe' : rsvp === 'declined' ? 'cant' : 'ticket'
  const app = p.applications.find((x) => x.pubkey === k)
  if (app || a?.applied) return app?.waitlist || a?.waitlist ? 'waitlist' : 'applied'
  if (rsvp === 'accepted') return 'ticket'
  if (rsvp === 'tentative') return 'maybe'
  if (rsvp === 'declined') return 'cant'
  if (e.hosts.has(k)) return 'host'
  return 'none'
}
export const isHost = (e: CalEvent) => e.hosts.has(me()) || (isAdmin() && !e.remote)

/** "Still going?": my RSVP or ticket predates a change of the date or the place. */
export function stillGoing(e: CalEvent): boolean {
  const mine = e.rsvpEvents.get(me())
  const s = e.rsvps.get(me())
  if (!mine || s === 'declined') return false
  const said = tag(mine, 'e')
  const base = e.versions.find((v) => v.id === said) ?? e.versions.filter((v) => v.created_at <= mine.created_at).at(-1)
  if (!base) return false
  const key = (v: Ev) => [tag(v, 'start'), tag(v, 'end'), tag(v, 'location')].join('|')
  return key(base) !== key(e.ev)
}

// ---- making and changing an event ----------------------------------------------------------

export interface EventForm {
  title: string
  description: string
  allDay: boolean
  start: string // datetime-local / date, in TZ
  end: string
  tz: string
  location: string
  online: string
  visibility: Visibility
  slug: string
  cover?: string // a Blossom URL (uploaded before)
  closed?: boolean
}

/** The UTC timestamp of a wall-clock time LOCAL (YYYY-MM-DDTHH:mm) in time zone TZ. */
export function zonedToUtc(local: string, tz: string): number {
  const [d, t = '00:00'] = local.split('T')
  const [y, mo, da] = d.split('-').map(Number)
  const [h, mi] = t.split(':').map(Number)
  const guess = Date.UTC(y, mo - 1, da, h, mi)
  const offset = (at: number) => {
    const parts = Object.fromEntries(new Intl.DateTimeFormat('en-US', { timeZone: tz, hourCycle: 'h23', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' })
      .formatToParts(new Date(at)).map((p) => [p.type, p.value]))
    return Date.UTC(+parts.year, +parts.month - 1, +parts.day, +parts.hour, +parts.minute, +parts.second) - at
  }
  try {
    let ts = guess - offset(guess)
    ts = guess - offset(ts) // across a DST change
    return Math.floor(ts / 1000)
  } catch { return Math.floor(new Date(local).getTime() / 1000) }
}
/** The wall-clock time of DATE in TZ, as an input's value. */
export function utcToZoned(date: Date, tz: string): string {
  try {
    const p = Object.fromEntries(new Intl.DateTimeFormat('en-US', { timeZone: tz, hourCycle: 'h23', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }).formatToParts(date).map((x) => [x.type, x.value]))
    return `${p.year}-${p.month}-${p.day}T${p.hour}:${p.minute}`
  } catch {
    const z = (n: number) => String(n).padStart(2, '0')
    return `${date.getFullYear()}-${z(date.getMonth() + 1)}-${z(date.getDate())}T${z(date.getHours())}:${z(date.getMinutes())}`
  }
}
export const localTz = () => { try { return Intl.DateTimeFormat().resolvedOptions().timeZone || TZ } catch { return TZ } }

function versionTags(d: string, n: EventForm, extra: string[][] = []): { kind: number; tags: string[][] } {
  const tags: string[][] = [['d', d], ['title', n.title.trim()], ['h', 'ev-' + d, BELFRY.groupsRelay], ['visibility', n.visibility]]
  if (n.allDay) {
    tags.push(['start', n.start.slice(0, 10)])
    if (n.end) tags.push(['end', n.end.slice(0, 10)])
  } else {
    tags.push(['start', String(zonedToUtc(n.start, n.tz))], ['start_tzid', n.tz])
    if (n.end) tags.push(['end', String(zonedToUtc(n.end, n.tz))], ['end_tzid', n.tz])
  }
  if (n.location.trim()) tags.push(['location', n.location.trim()])
  if (n.online.trim() && /^https:\/\//.test(n.online.trim())) tags.push(['r', n.online.trim()])
  if (n.cover) tags.push(['image', n.cover])
  if (n.slug) tags.push(['slug', n.slug])
  return { kind: n.allDay ? 31922 : 31923, tags: [...tags, ...extra] }
}

/** A new event: its group (ev-<d>, me as creator and admin), then the NIP-52 event. No post in
 *  any channel: sending its URL in one shows its card there. Returns its id. */
export async function createEvent(n: EventForm, extra: string[][] = []): Promise<string> {
  const d = newEventD()
  const groupId = 'ev-' + d
  await enqueue('groups', { kind: 9007, tags: [['h', groupId]] })
  await enqueue('groups', { kind: 9002, tags: [['h', groupId], ['name', n.title.trim()], ['about', n.description.split('\n')[0].slice(0, 200) || 'The event’s chat'], ...(n.cover ? [['picture', n.cover]] : []), n.closed ? ['closed'] : ['open']] })
  const v = versionTags(d, n, extra)
  await enqueue('main', { kind: v.kind, content: n.description, tags: v.tags })
  if (n.slug) await claimSlug(d, n.slug).catch(() => undefined)
  return d
}

/** A host's new version (rule B: any host's newest counts; the coordinate stays the creator's). */
export async function editEvent(e: CalEvent, n: EventForm, extra: string[][] = []) {
  // a meeting's own tags stay (core/meetings.ts): what it is, where it was made, who is invited
  const keep = e.ev.tags.filter((t) => (t[0] === 't' && t[1] === 'meeting') || t[0] === 'channel' || (t[0] === 'p' && t[3] === 'invited'))
  const v = versionTags(e.d, n, [...keep.filter((k) => !extra.some((x) => x.join() === k.join())), ...extra, ...(e.status === 'cancelled' ? [['status', 'cancelled']] : [])])
  await enqueue('main', { kind: v.kind, content: n.description, tags: v.tags })
  const g = group(e.groupId)
  if (g && (g.name !== n.title.trim() || (n.cover && g.picture !== n.cover)))
    await enqueue('groups', { kind: 9002, tags: [['h', e.groupId], ['name', n.title.trim()], ['about', g.about], ...(n.cover ? [['picture', n.cover]] : [])] })
}

export function formOf(e: CalEvent): EventForm {
  const tz = e.tz || localTz()
  return {
    title: e.title, description: e.description, allDay: e.allDay,
    start: e.allDay ? tag(e.ev, 'start') ?? '' : utcToZoned(e.start, tz),
    end: e.end ? (e.allDay ? tag(e.ev, 'end') ?? '' : utcToZoned(e.end, tz)) : '',
    tz, location: e.location ?? '', online: e.links[0] ?? '', visibility: e.visibility, slug: e.slug ?? '', cover: e.cover,
  }
}

/** Is that custom URL free? (the belfry keeps them: unique per belfry) */
export async function slugFree(name: string, d = ''): Promise<{ free: boolean; suggestion: string; problem?: string }> {
  const p = slugProblem(name)
  if (isLocalOnly()) {
    const other = calendar().find((x) => x.slug === name && x.d !== d)
    return { free: !p && !other, suggestion: name, problem: p }
  }
  const r = await signedFetch(`${BELFRY.api}/events/slug?name=${encodeURIComponent(name)}${d ? '&event=' + encodeURIComponent(d) : ''}`)
  const j = await r.json().catch(() => ({}))
  if (!r.ok) throw new Error(j.message || 'Could not check the URL.')
  return j
}
export async function claimSlug(d: string, slug: string) {
  if (isLocalOnly()) return
  const r = await signedFetch(`${BELFRY.api}/events/${d}/slug`, 'PUT', { slug })
  const j = await r.json().catch(() => ({}))
  if (!r.ok) throw new Error(j.message || 'That URL could not be set.')
}

/** Cancel (with a line to the guests) or reopen: a label in the group and a new version. */
export async function setStatus(e: CalEvent, status: 'cancelled' | 'scheduled', reason = '') {
  await enqueue('groups', { kind: 1985, content: reason.slice(0, 500), tags: [['h', e.groupId], ['L', EVENT_STATUS_NS], ['l', status, EVENT_STATUS_NS], ['a', e.coord]] })
  const tags = e.ev.tags.filter((t) => t[0] !== 'status')
  await enqueue('main', { kind: e.ev.kind, content: e.ev.content, tags: status === 'cancelled' ? [...tags, ['status', 'cancelled']] : tags })
}

export interface TicketForm { id?: string; title: string; summary: string; quantity: string; closed: boolean; prompt: string; members: boolean; position: number }
/** Save a ticket type (NIP-99 30402; no `price` in v1: free). */
export async function saveTicket(e: CalEvent, t: TicketForm) {
  const id = t.id || newId(3)
  const tags: string[][] = [['d', `${e.d}:${id}`], ['a', e.coord, BELFRY.mainRelay], ['h', e.groupId], ['title', t.title.trim() || 'Ticket'], ['t', 'ticket'], ['position', String(t.position)]]
  if (t.summary.trim()) tags.push(['summary', t.summary.trim()])
  if (/^\d+$/.test(t.quantity.trim())) tags.push(['quantity', t.quantity.trim()])
  if (t.closed) tags.push(['closed'], ['prompt', t.prompt.trim() || 'Why would you like to come?'])
  if (t.members) tags.push(['members'])
  await enqueue('main', { kind: 30402, content: t.summary.trim(), tags })
  // any ticket that takes an application closes the event's group (NIP-29 `closed`)
  const closing = t.closed || tickets(e).some((x) => x.closed && x.id !== id)
  const g = group(e.groupId)
  if (g && g.closed !== closing) await enqueue('groups', { kind: 9002, tags: [['h', e.groupId], ['name', g.name], ['about', g.about], closing ? ['closed'] : ['open']] })
}
export const deleteTicket = (e: CalEvent, t: Ticket) => t.ev && enqueue('main', { kind: 5, tags: [['a', `30402:${t.ev.pubkey}:${t.d}`], ['e', t.ev.id]] })

// ---- coming: RSVP, a ticket, an application -----------------------------------------------

const amGuest = () => isGuest(me())

/** RSVP: a member's goes on the community relay (NIP-52); a guest's in the event's group. */
export function rsvp(e: CalEvent, status: Rsvp, ticketD?: string) {
  const [, author, d] = e.coord.split(':')
  const tags = [['d', bytesToHex(sha256(`${me()}:${author}:${d}`)).slice(0, 30)], ['a', e.coord], ...(e.ev.pending || e.remote ? [] : [['e', e.ev.id]]), ['status', status], ['p', author], ...(ticketD ? [['ticket', ticketD]] : [])]
  if (amGuest() || e.remote) return enqueue('groups', { kind: 31925, tags: [...tags, ['h', e.groupId]] })
  return enqueue('main', { kind: 31925, tags })
}

interface JoinAnswer { ticket?: string; pubkey?: string; bunker_url?: string; already?: boolean; message?: string; error?: string }

/** Take an open ticket. A member on the implicit free ticket simply RSVPs (as before); otherwise
 *  the belfry checks the places and issues it (a 9000 by the groups key). */
export async function takeTicket(e: CalEvent, t: Ticket): Promise<void> {
  if (t.implicit && !amGuest() && !e.remote) { await rsvp(e, 'accepted'); return }
  if (isLocalOnly()) { demoIssue(e, me(), t.d); await rsvp(e, 'accepted', t.d); return }
  const r = await signedFetch(`${BELFRY.api}/events/${e.d}/join`, 'POST', { ticket: t.d })
  const j = (await r.json().catch(() => ({}))) as JoinAnswer
  if (!r.ok) throw new Error(j.message || 'That did not work. Try again.')
  remote.delete(e.d); remote.delete(eventLinkOf(e)); touch()
}

/** Apply (or join the waitlist): a member sends the 9021 to the group; a guest signs it and the
 *  belfry hands it in (the relay takes it from people who are not members, zooid patch 0004). */
export async function applyFor(e: CalEvent, t: Ticket, answer: string, waitlist: boolean) {
  const tags = [['h', e.groupId], ['ticket', t.d], ...(waitlist ? [['waitlist']] : [])]
  if (!amGuest() && !e.remote) { await enqueue('groups', { kind: 9021, content: answer.slice(0, 2000), tags }); return }
  const request = await currentSigner().sign({ kind: 9021, content: answer.slice(0, 2000), tags, created_at: Math.floor(Date.now() / 1000) })
  const r = await signedFetch(`${BELFRY.api}/events/${e.d}/apply`, 'POST', { ticket: t.d, request })
  const j = await r.json().catch(() => ({}))
  if (!r.ok) throw new Error(j.message || 'Your application did not go through. Try again.')
  addEvents([request])
  remote.delete(e.d); touch()
}
export const withdraw = (e: CalEvent) => enqueue('groups', { kind: 9022, tags: [['h', e.groupId]] })

/** A host approves an application (the ticket: a 9000 with `ticket`) or declines it. A guest
 *  co-host can't moderate on the relay: the belfry does it for them. */
export async function decide(e: CalEvent, pubkey: string, action: 'approve' | 'decline', ticketD?: string, app?: Ev) {
  if (amGuest()) {
    const r = await signedFetch(`${BELFRY.api}/events/${e.d}/decide`, 'POST', { pubkey, action, ticket: ticketD })
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).message || 'That did not work.')
    return
  }
  if (action === 'decline') return void (await enqueue('groups', { kind: 9001, tags: [['h', e.groupId], ['p', pubkey]] }))
  await enqueue('groups', { kind: 9000, tags: [['h', e.groupId], ['p', pubkey, 'member'], ['ticket', ticketD || `${e.d}:free`], ['paid', 'free'], ['a', e.coord], ...(app ? [['e', app.id]] : [])] })
}

/** Make a member a co-host: an admin of the event's group. */
export const addCoHost = (e: CalEvent, pubkey: string) =>
  enqueue('groups', { kind: 9000, tags: [['h', e.groupId], ['p', pubkey, 'admin']] })

/** A co-host invitation link (a channel invite with role host, guests.py). */
export async function coHostLink(e: CalEvent): Promise<string> {
  if (isLocalOnly()) return `https://chat.${BELFRY.domain}/join/demo-cohost-${e.d}`
  const r = await signedFetch(`${BELFRY.api}/channel-invites`, 'POST', { channel: e.groupId, role: 'host', people: 1, link_days: 1 })
  const j = await r.json().catch(() => ({}))
  if (!r.ok) throw new Error(j.message || 'The link could not be made.')
  return String(j.url)
}

export const eventLinkOf = (e: CalEvent) => e.slug || e.d

/** Upload a cover to Blossom (a data URL in demo mode). */
export const uploadCover = (f: File) => uploadBlob(f)

// ---- someone with no account (the join screen, signed out) -------------------------------

const PENDING = 'belfry-event-applied'
/** An application sent before having any channel: the app says so instead of "no access". */
export const pendingApplication = (): { link: string; title: string } | null => { try { return JSON.parse(localStorage.getItem(PENDING) ?? 'null') } catch { return null } }
export const setPendingApplication = (v: { link: string; title: string } | null) => { try { if (v) localStorage.setItem(PENDING, JSON.stringify(v)); else localStorage.removeItem(PENDING) } catch { /* private mode */ } }

/** The event's link from the address (#/events/<link>), when the chat opens on one. */
export function eventLinkFromUrl(): string {
  const m = /^#\/events\/([a-z0-9][a-z0-9-]{1,38}[a-z0-9])$/.exec(location.hash)
  return m ? m[1] : ''
}

// ---- demo mode: what the belfry would publish ---------------------------------------------

function hex(n: number) { return [...crypto.getRandomValues(new Uint8Array(n))].map((b) => b.toString(16).padStart(2, '0')).join('') }
/** Demo only: the groups key issues a ticket (what POST /events/<d>/join does). */
export function demoIssue(e: CalEvent, pubkey: string, ticketD: string, name?: string) {
  addEvents([{ id: hex(32), pubkey: 'f'.repeat(64), sig: hex(64), kind: 9000, created_at: Math.floor(Date.now() / 1000), content: '',
    tags: [['h', e.groupId], ['p', pubkey, 'member'], ['ticket', ticketD], ['paid', 'free'], ...(name ? [['name', name], ['invited-by', e.creator]] : [])] }])
}
/** Demo only: someone applies (their own 9021) with a profile name. */
export function demoApply(link: string, name: string, answer: string, waitlist = false) {
  const e = eventByLinkLocal(link)
  if (!e) return
  const pk = hex(32)
  const now = Math.floor(Date.now() / 1000)
  const t = tickets(e).find((x) => x.closed) ?? tickets(e)[0]
  addEvents([
    { id: hex(32), pubkey: pk, sig: hex(64), kind: 0, created_at: now, tags: [], content: JSON.stringify({ name }) },
    { id: hex(32), pubkey: pk, sig: hex(64), kind: 9021, created_at: now, content: answer, tags: [['h', e.groupId], ['ticket', t.d], ...(waitlist ? [['waitlist']] : [])] },
  ])
  return pk
}
/** Demo only: a guest with a name takes the free ticket. */
export function demoGuest(link: string, name: string) {
  const e = eventByLinkLocal(link)
  if (!e) return
  const pk = hex(32)
  demoIssue(e, pk, tickets(e)[0].d, name)
  return pk
}

export const npubOf = (pk: string) => nip19.npubEncode(pk)
export { eventUrl, groups }
