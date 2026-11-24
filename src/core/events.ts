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
import { BELFRY, getBackend, TZ } from '../config'
import { calendar, eventByLinkLocal, EVENT_D, EVENT_STATUS_NS, eventUrl, group, groups, type CalEvent, type Rsvp, type Visibility } from './model'
import { all, addEvents, touch, type Ev } from './store'
import { currentSigner, enqueue, isLocalOnly, me, newId, signedFetch, uploadBlob } from './sync'
import { isGuest } from './invite'
import { isAdmin } from './access'
import { editGroupMeta, relayAdmin, setGroupRole } from './checkin'

const tag = (e: Ev, k: string) => e.tags.find((t) => t[0] === k)?.[1]

// ---- ids, URLs ---------------------------------------------------------------------------

const ALPHA = 'abcdefghijklmnopqrstuvwxyz0123456789'
/** A new event's id: 6 random characters (no year since 2026-11-18). Never recomputed. */
export function newEventD(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(6))
  return [...bytes].map((b) => ALPHA[b % 36]).join('')
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

export interface ApiTicket { d: string; id: string; title: string; summary: string; quantity: number | null; left: number | null; closed: boolean; prompt: string; members: boolean; position: number; implicit: boolean
  price?: { amount: string; currency: string; vat?: string | null; vat_included?: boolean; until?: number | null } | null; prices?: { amount: string; currency: string; from?: number | null; until?: number | null }[]; on_sale?: boolean
  /** docs/sales.md §10: this viewer can buy it through the community's checkout now, and where */ can_pay?: boolean; checkout_url?: string | null }
export interface ApiViewer {
  host: boolean; ticket: string | null; applied: boolean; waitlist: boolean; declined: boolean; rsvp: Rsvp | null; member: boolean
  /** check-in (docs/checkin.md): my ticket's code and the URL its QR holds, when I hold one */
  ticket_code?: string | null; ticket_url?: string | null; checked_in?: number | null
  roles?: ('host' | 'cohost' | 'steward')[]; may_check_in?: boolean
}
export interface ApiEvent {
  d: string; coord: string; kind: number; group: string; link: string; url: string; title: string; description: string
  all_day: boolean; start: string | null; end: string | null; tz: string; location: string; online: boolean; links: string[]
  /** the event's own site (["website", url]) */ website?: string | null
  cover: string | null; visibility: Visibility; status: 'scheduled' | 'cancelled'; status_reason: string
  hosts: { pubkey: string; name: string }[]; tickets: ApiTicket[]; going: number; community: string
  me: ApiViewer | null
  /** the same block as `me` (docs/checkin.md) */
  viewer?: ApiViewer | null
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
// demo mode only (tests): what the belfry would answer for an event, and how old the answer is
if (typeof window !== 'undefined' && getBackend() === 'demo') (window as unknown as { __demoRemote: object }).__demoRemote = {
  set: (link: string, ev: ApiEvent) => { remote.set(link, { at: Date.now(), ev }); touch() },
  age: (link: string) => remote.get(link)?.at,
}
/** Ask the belfry again (back from the checkout in another tab: "Show my ticket" without a reload). */
export function refreshEvent(link: string) {
  const got = remote.get(link)
  if (got) got.at = 0
  touch()
}

export async function fetchEvent(link: string, signed = true): Promise<ApiEvent> {
  const url = `${BELFRY.api}/events/${encodeURIComponent(link)}`
  const r = signed && me() ? await signedFetch(url) : await fetch(url)
  const d = await r.json().catch(() => ({}))
  if (r.status === 403) throw new Error('members_only')
  if (!r.ok) throw new Error(typeof d.error === 'string' ? d.error : 'not_found')
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
    allDay, start: parse(a.start) ?? new Date(), end: parse(a.end), tz: a.tz || undefined, groupId: a.group, links: a.links, website: typeof a.website === 'string' && /^https:\/\//.test(a.website) ? a.website : undefined,
    channels: [], rsvps: new Map(a.me?.rsvp ? [[me(), a.me.rsvp]] : []), rsvpEvents: new Map(), isNew: true, creator, meeting: false, invited: [],
    hosts: new Set(a.hosts.map((h) => h.pubkey)), cover: a.cover ?? undefined, visibility: a.visibility, slug: a.link !== a.d ? a.link : undefined,
    status: a.status, statusReason: a.status_reason || undefined, remote: true,
  }
}

/** The belfry's public events (GET /api/v1/events?visibility=public, no auth; docs/events.md):
 *  what a guest's Calendar lists next to their channels' events. Cached five minutes. */
let publicList: { at: number; list: CalEvent[] } | null = null
let publicAsking = false
export function publicEvents(): CalEvent[] {
  if (!isLocalOnly() && !publicAsking && (!publicList || Date.now() - publicList.at > 300_000)) {
    publicAsking = true
    const from = Math.floor(Date.now() / 1000) - 86400
    void fetch(`${BELFRY.api}/events?visibility=public&from=${from}`)
      .then((r) => (r.ok ? r.json() : { events: [] }))
      .then((d: { events?: unknown }) => {
        const list = (Array.isArray(d.events) ? d.events : []).filter((a): a is ApiEvent => !!a && typeof a === 'object' && typeof (a as ApiEvent).coord === 'string' && typeof (a as ApiEvent).d === 'string')
        publicList = { at: Date.now(), list: list.map(fromApi) }
      }, () => { publicList = { at: Date.now(), list: publicList?.list ?? [] } })
      .finally(() => { publicAsking = false; touch() })
  }
  return publicList?.list ?? []
}

/** An event by its URL's last part: ours when the relay gave it to us, else the belfry's answer. */
export function eventByLink(link: string): { e?: CalEvent; api?: ApiEvent; error?: string } {
  const e = eventByLinkLocal(link)
  if (e) return { e }
  const api = remoteEvent(link)
  return api ? { e: fromApi(api), api } : { error: remoteError(link) ?? (isLocalOnly() ? 'not_found' : undefined) }
}

// ---- tickets and the people of an event ---------------------------------------------------

export * from './prices'
import { isMoney, PRICE_AMOUNT, PRICE_LABEL, pricesOf, VAT_RATE, type Price, type PriceRow } from './prices'
export interface Ticket { d: string; id: string; title: string; summary: string; quantity?: number; closed: boolean; prompt: string; members: boolean; position: number; implicit: boolean; ev?: Ev
  /** the price now (none: free, or not on sale when `priced`) */ price?: Price;
  /** sales.md §10: bought through the community's checkout, at this address (safeCheckout) */ checkout?: string; /** places left as the belfry counts them (orders holding one too) */ left?: number | null; /** it has a price at all */ priced?: boolean; prices?: PriceRow[]; /** false: priced, but no price is on now */ onSale?: boolean }

/** People ask for this ticket (an application, a price) instead of taking it in one tap. */
export const asks = (t: { closed: boolean; price?: unknown; priced?: boolean }) => t.closed || !!t.price || !!t.priced

export const freeTicket = (e: Pick<CalEvent, 'd'>): Ticket => ({ d: `${e.d}:free`, id: 'free', title: 'RSVP', summary: '', closed: false, prompt: '', members: false, position: 0, implicit: true })

/** A ticket as the belfry describes it (GET /events/<link>), in the app's shape. */
/** A checkout address the app may open (sales.md §10, the server's own rule when the template is
 *  saved): https, on this belfry's domain or a subdomain, no port, no user info, no fragment. */
export function safeCheckout(u?: string | null): string | undefined {
  if (!u || u.length > 600) return undefined
  try {
    const x = new URL(u)
    const d = BELFRY.domain.toLowerCase(), h = x.hostname.toLowerCase()
    if (x.protocol !== 'https:' || x.port || x.username || x.password || x.hash) return undefined
    return h === d || h.endsWith('.' + d) ? x.href : undefined
  } catch { return undefined }
}
/** The sales side of the belfry's answer on tickets (by d): left, and the checkout when the viewer can buy. */
export function withSales(tks: Ticket[], api?: ApiEvent): Ticket[] {
  if (!api) return tks
  const by = new Map(api.tickets.map((t) => [t.d, t]))
  return tks.map((t) => {
    const a = by.get(t.d)
    if (!a) return t
    return { ...t, left: a.left, checkout: a.can_pay ? safeCheckout(a.checkout_url) : undefined }
  })
}
export const apiTicket = (t: ApiTicket): Ticket & { left: number | null } => ({
  ...t, quantity: t.quantity ?? undefined, checkout: t.can_pay ? safeCheckout(t.checkout_url) : undefined,
  price: t.price ? { amount: t.price.amount, currency: t.price.currency, vat: t.price.vat ?? undefined, vatIncluded: !!t.price.vat_included, until: t.price.until ?? undefined } : undefined,
  prices: (t.prices ?? []).map((r) => ({ amount: r.amount, currency: r.currency, from: r.from ?? undefined, until: r.until ?? undefined })),
  priced: !!t.price || (t.prices?.length ?? 0) > 0, onSale: t.on_sale ?? true,
})
export function tickets(e: CalEvent, api?: ApiEvent): Ticket[] {
  if (api || e.remote) {
    const a = api ?? remoteEvent(e.d)
    if (a) return a.tickets.map(apiTicket)
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
      prompt: tag(x, 'prompt') ?? '', members: x.tags.some((t) => t[0] === 'members'), position: Number(tag(x, 'position')) || 0, implicit: false, ev: x, ...pricesOf(x.tags),
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
export const placesLeft = (t: Ticket, p: People) => (typeof t.left === 'number' ? t.left : t.left === null ? undefined : t.quantity === undefined ? undefined : Math.max(0, t.quantity - taken(p, t.d)))

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
  /** the event's own outside site ("Event website"), https */
  website?: string
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
// ---- when an event is over (lead 2026-11-20, a member in #website: "Upcoming events show events of
// the current day that have already passed"): an event is upcoming until its END, then past. No end:
// start + 1 h. All day: until the end of its last day (its end date, as the chat writes it: the last
// day) in its time zone, the viewer's when it has none. Going on now = still upcoming.
export const DEFAULT_LENGTH = 3600e3
const ymd = (d: Date) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
/** SPEC 19.65: a whole-day event over several days shows its days ("Thu 12 Nov → Sat 14 Nov"),
 *  never "All day"; one whole day still says "All day". The end date is the last day. */
export const shortDay = (d: Date) => d.toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short' }).replace(',', '')
export function allDaySpan(e: Pick<CalEvent, 'allDay' | 'start' | 'end'>): [string, string] | null {
  if (!e.allDay || !e.end || ymd(e.end) <= ymd(e.start)) return null
  return [shortDay(e.start), shortDay(e.end)]
}
export const allDayWord = (e: Pick<CalEvent, 'allDay' | 'start' | 'end'>) => { const s = allDaySpan(e); return s ? `${s[0]} → ${s[1]}` : 'All day' }
/** When E is over (ms). */
export function eventEnd(e: Pick<CalEvent, 'allDay' | 'start' | 'end' | 'tz'>): number {
  if (e.allDay) {
    const last = e.end && +e.end > +e.start ? e.end : e.start // a date at the viewer's midnight (model.ts parse)
    const next = new Date(last.getFullYear(), last.getMonth(), last.getDate() + 1)
    return e.tz ? zonedToUtc(ymd(next) + 'T00:00', e.tz) * 1000 : +next
  }
  return e.end && +e.end > +e.start ? +e.end : +e.start + DEFAULT_LENGTH
}
/** When E starts (ms): an all-day event at the start of its first day in its time zone. */
export const eventStart = (e: Pick<CalEvent, 'allDay' | 'start' | 'tz'>): number => (e.allDay && e.tz ? zonedToUtc(ymd(e.start) + 'T00:00', e.tz) * 1000 : +e.start)
export const isPastEvent = (e: Pick<CalEvent, 'allDay' | 'start' | 'end' | 'tz'>, now = Date.now()) => eventEnd(e) <= now
export const isOnNow = (e: Pick<CalEvent, 'allDay' | 'start' | 'end' | 'tz'>, now = Date.now()) => eventStart(e) <= now && now < eventEnd(e)

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
  if (n.website?.trim() && /^https:\/\//.test(n.website.trim())) tags.push(['website', n.website.trim()])
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
  // the belfry also adopts the event's own `slug` tag when it loads it (Belfry ba5024b); claiming
  // right away can reach it before the event does (404), so try again a few times
  if (n.slug) void claimSoon(d, n.slug)
  return d
}

/** A host's new version (rule B: any host's newest counts; the coordinate stays the creator's). */
export async function editEvent(e: CalEvent, n: EventForm, extra: string[][] = [], uninvite: string[] = []) {
  // a meeting's own tags stay (core/meetings.ts): what it is, where it was made, who is invited
  // (UNINVITE: people removed, SPEC 19.63)
  const keep = e.ev.tags.filter((t) => (t[0] === 't' && t[1] === 'meeting') || t[0] === 'channel' || (t[0] === 'p' && t[3] === 'invited' && !uninvite.includes(t[1])))
  const v = versionTags(e.d, n, [...keep.filter((k) => !extra.some((x) => x.join() === k.join())), ...extra, ...(e.status === 'cancelled' ? [['status', 'cancelled']] : [])])
  await enqueue('main', { kind: v.kind, content: n.description, tags: v.tags })
  const g = group(e.groupId)
  if (g && (g.name !== n.title.trim() || (n.cover && g.picture !== n.cover)))
    await editGroupMeta(e.groupId, [['name', n.title.trim()], ['about', g.about], ...(n.cover ? [['picture', n.cover]] : [])], { name: n.title.trim(), ...(n.cover ? { picture: n.cover } : {}) })
}

export function formOf(e: CalEvent): EventForm {
  const tz = e.tz || localTz()
  return {
    title: e.title, description: e.description, allDay: e.allDay,
    start: e.allDay ? tag(e.ev, 'start') ?? '' : utcToZoned(e.start, tz),
    end: e.end ? (e.allDay ? tag(e.ev, 'end') ?? '' : utcToZoned(e.end, tz)) : '',
    tz, location: e.location ?? '', online: e.links[0] ?? '', website: e.website ?? '', visibility: e.visibility, slug: e.slug ?? '', cover: e.cover,
  }
}

// ---- "Event website": paste it and the belfry fills in the rest (docs/event-details.md) ---------
export interface EventDetails { url: string; title: string; start?: Date; end?: Date; allDay: boolean; tz?: string; place: string; image?: string; description: string }
const DETAILS_DEMO: Record<string, Record<string, unknown>> = {
  'https://riverside-openday.example': { title: 'Riverside Open Day 2027', starts_at: '2027-01-28T09:00:00Z', ends_at: '2027-01-28T16:00:00Z', all_day: false, timezone: 'Europe/Brussels', place: { name: 'Community hall', address: 'Rue Haute 12' }, description: 'Meet the groups of Riverside Commons: workshops, the repair café and soup at noon.' },
}
/** The event behind an outside URL, as the belfry reads it (members and their agents; null: nothing
 *  usable, not allowed, or offline). Its picture comes through the belfry's proxy. Never a call link. */
export async function fetchEventDetails(url: string): Promise<EventDetails | null> {
  let d: Record<string, unknown>
  if (isLocalOnly()) {
    await new Promise((r) => setTimeout(r, 900))
    const k = url.replace(/\/+$/, '')
    if (!DETAILS_DEMO[k]) return null
    d = { url: k, ...DETAILS_DEMO[k] }
  } else {
    try {
      const r = await signedFetch(`${BELFRY.api}/event-details?url=${encodeURIComponent(url)}`)
      if (!r.ok) return null
      d = (await r.json().catch(() => ({}))) as Record<string, unknown>
    } catch { return null }
  }
  const s = (v: unknown, n: number) => (typeof v === 'string' ? v.trim().slice(0, n) : '')
  const date = (v: unknown) => { if (typeof v !== 'string') return undefined; const t = new Date(v); return isNaN(+t) ? undefined : t }
  const p = d.place && typeof d.place === 'object' ? d.place as Record<string, unknown> : null
  const place = p ? [s(p.name, 200), s(p.address, 300)].filter(Boolean).join(', ') : ''
  let image: string | undefined
  try { if (typeof d.image === 'string') { const i = new URL(d.image); if (i.protocol === 'https:' && i.origin === new URL(BELFRY.api).origin) image = i.href } } catch { /* not the proxy */ }
  const out: EventDetails = { url: s(d.url, 2000) || url, title: s(d.title, 200), start: date(d.starts_at), end: date(d.ends_at), allDay: d.all_day === true,
    tz: typeof d.timezone === 'string' && /^[A-Za-z_]+(\/[A-Za-z0-9_+-]+)*$/.test(d.timezone) ? d.timezone : undefined, place, image, description: s(d.description, 5000) }
  return out.title || out.start || out.place || out.description ? out : null
}

/** Is that custom URL free? (the belfry keeps them: unique per belfry) */
export async function slugFree(name: string, d = ''): Promise<{ free: boolean; suggestion: string; problem?: string }> {
  const p = slugProblem(name)
  if (isLocalOnly()) {
    const used = (n: string) => calendar().some((x) => x.slug === n && x.d !== d)
    let next = name
    for (let i = 2; used(next) && i < 50; i++) next = `${name.slice(0, 36)}-${i}` // (as the belfry does: the next free one)
    return { free: !p && !used(name), suggestion: next, problem: p }
  }
  const r = await signedFetch(`${BELFRY.api}/events/slug?name=${encodeURIComponent(name)}${d ? '&event=' + encodeURIComponent(d) : ''}`)
  const j = await r.json().catch(() => ({}))
  if (!r.ok) throw new Error('Could not check the URL.')
  return j
}
async function claimSoon(d: string, slug: string) {
  for (const wait of [0, 2000, 5000, 15000]) {
    if (wait) await new Promise((r) => setTimeout(r, wait))
    try { await claimSlug(d, slug); return } catch (x) { if (!(x instanceof SlugError) || x.code !== 'not_found') return }
  }
}
class SlugError extends Error { constructor(m: string, readonly code?: string) { super(m) } }
export async function claimSlug(d: string, slug: string) {
  if (isLocalOnly()) return
  const r = await signedFetch(`${BELFRY.api}/events/${d}/slug`, 'PUT', { slug })
  const j = await r.json().catch(() => ({}))
  if (!r.ok) throw new SlugError('That URL could not be set.', j.error)
}

/** Cancel (with a line to the guests) or reopen: a label in the group and a new version. */
export async function setStatus(e: CalEvent, status: 'cancelled' | 'scheduled', reason = '') {
  await enqueue('groups', { kind: 1985, content: reason.slice(0, 500), tags: [['h', e.groupId], ['L', EVENT_STATUS_NS], ['l', status, EVENT_STATUS_NS], ['a', e.coord]] })
  const tags = e.ev.tags.filter((t) => t[0] !== 'status')
  await enqueue('main', { kind: e.ev.kind, content: e.ev.content, tags: status === 'cancelled' ? [...tags, ['status', 'cancelled']] : tags })
}

/** A price row of the sheet: an amount, and optional dates (YYYY-MM-DD, this device's time zone). */
export interface FormPrice { amount: string; from: string; until: string }
export interface TicketForm { id?: string; title: string; summary: string; quantity: string; closed: boolean; prompt: string; members: boolean; position: number
  /** empty amounts: free */ prices: FormPrice[]; /** EUR, HOUR, or the host's label */ currency: string; /** money only; empty: no VAT */ vat: string; vatIncluded: boolean }
const dayStart = (s: string) => (s ? Math.floor(new Date(s + 'T00:00:00').getTime() / 1000) : undefined)
export const dayInput = (t?: number) => { if (!t) return ''; const d = new Date(t * 1000); const z = (n: number) => String(n).padStart(2, '0'); return `${d.getFullYear()}-${z(d.getMonth() + 1)}-${z(d.getDate())}` }
/** The form's price tags, or why they can't be written (the sheet shows it). Free: no tags. */
export function formPriceTags(t: Pick<TicketForm, 'prices' | 'currency' | 'vat' | 'vatIncluded'>): { tags: string[][]; error?: string } {
  const currency = t.currency.trim()
  const rows = t.prices.map((r) => ({ ...r, amount: r.amount.trim().replace(',', '.') })).filter((r) => r.amount)
  if (!rows.length) return { tags: [] }
  if (rows.some((r) => !PRICE_AMOUNT.test(r.amount) || !(Number(r.amount) > 0))) return { tags: [], error: 'A price like 15 or 7.50' }
  if (!PRICE_LABEL.test(currency)) return { tags: [], error: 'Say what it is paid in' }
  const plain = rows.filter((r) => !r.from && !r.until)
  if (plain.length > 1) return { tags: [], error: 'Only one price without dates' }
  if (rows.some((r) => r.from && r.until && r.until <= r.from)) return { tags: [], error: 'A price ends after it starts' }
  const tags: string[][] = []
  for (const r of rows.filter((x) => x.from || x.until)) tags.push(['price_at', String(Number(r.amount)), currency, String(dayStart(r.from) ?? ''), String(dayStart(r.until) ?? '')])
  if (plain[0]) tags.push(['price', String(Number(plain[0].amount)), currency])
  const rate = t.vat.trim().replace(',', '.')
  if (isMoney(currency) && rate) {
    if (!VAT_RATE.test(rate)) return { tags: [], error: 'A VAT rate like 21 or 5.5' }
    if (Number(rate) > 0) tags.push(t.vatIncluded ? ['vat', String(Number(rate)), 'included'] : ['vat', String(Number(rate))])
  }
  return { tags }
}
/** Save a ticket type (NIP-99 30402, with its `price` when it has one). */
export async function saveTicket(e: CalEvent, t: TicketForm) {
  const id = t.id || newId(3)
  const tags: string[][] = [['d', `${e.d}:${id}`], ['a', e.coord, BELFRY.mainRelay], ['h', e.groupId], ['title', t.title.trim() || 'Ticket'], ['t', 'ticket'], ['position', String(t.position)]]
  if (t.summary.trim()) tags.push(['summary', t.summary.trim()])
  if (/^\d+$/.test(t.quantity.trim())) tags.push(['quantity', t.quantity.trim()])
  if (t.closed) tags.push(['closed'], ['prompt', t.prompt.trim() || 'Why would you like to come?'])
  if (t.members) tags.push(['members'])
  const money = formPriceTags(t)
  if (money.error) throw new Error(money.error)
  tags.push(...money.tags)
  const price = money.tags.length > 0
  await enqueue('main', { kind: 30402, content: t.summary.trim(), tags })
  // any ticket that takes an application closes the event's group (NIP-29 `closed`)
  const closing = t.closed || price || tickets(e).some((x) => asks(x) && x.id !== id)
  const g = group(e.groupId)
  if (g && g.closed !== closing) await editGroupMeta(e.groupId, [['name', g.rawName], ['about', g.about], closing ? ['closed'] : ['open']], { closed: closing })
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
  if (!r.ok) throw new Error('That did not work. Try again.')
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
  if (!r.ok) throw new Error('Your application did not go through. Try again.')
  addEvents([request])
  remote.delete(e.d); touch()
}
export const withdraw = (e: CalEvent) => enqueue('groups', { kind: 9022, tags: [['h', e.groupId]] })

/** A host approves an application (the ticket: a 9000 with `ticket`) or declines it. A guest
 *  co-host can't moderate on the relay: the belfry does it for them. */
export async function decide(e: CalEvent, pubkey: string, action: 'approve' | 'decline', ticketD?: string, app?: Ev) {
  // a guest co-host, or a co-host by role (zooid refuses their 9000 / 9001): through the belfry
  if (amGuest() || (!isLocalOnly() && !relayAdmin(e.groupId))) {
    const r = await signedFetch(`${BELFRY.api}/events/${e.d}/decide`, 'POST', { pubkey, action, ticket: ticketD })
    if (!r.ok) throw new Error('That did not work.')
    return
  }
  if (action === 'decline') return void (await enqueue('groups', { kind: 9001, tags: [['h', e.groupId], ['p', pubkey]] }))
  await enqueue('groups', { kind: 9000, tags: [['h', e.groupId], ['p', pubkey, 'member'], ['ticket', ticketD || `${e.d}:free`], ['paid', tickets(e).find((x) => x.d === ticketD)?.priced ? 'paid' : 'free'], ['a', e.coord], ...(app ? [['e', app.id]] : [])] })
}

/** Make a member a co-host: an admin of the event's group. */
export const addCoHost = async (e: CalEvent, pubkey: string) => {
  if (isLocalOnly() || relayAdmin(e.groupId)) return enqueue('groups', { kind: 9000, tags: [['h', e.groupId], ['p', pubkey, 'admin']] })
  await setGroupRole(e.groupId, pubkey, 'admin') // a co-host by role: through the belfry
}

/** A co-host invitation link (a channel invite with role host, guests.py). */
export async function coHostLink(e: CalEvent): Promise<string> {
  if (isLocalOnly()) return `https://chat.${BELFRY.domain}/join/demo-cohost-${e.d}`
  const r = await signedFetch(`${BELFRY.api}/channel-invites`, 'POST', { channel: e.groupId, role: 'host', people: 1, link_days: 1 })
  const j = await r.json().catch(() => ({}))
  if (!r.ok) throw new Error('The link could not be made.')
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
/** A ticket's QR opened in the chat (#/events/<link>/t/<code>): the event's link, else ''. */
export function ticketLinkFromUrl(): string {
  const m = /^#\/events\/([a-z0-9][a-z0-9-]{1,38}[a-z0-9])\/t\/[a-z2-7]{16}\/?$/.exec(location.hash)
  return m ? m[1] : ''
}
/** Forget the cached answer for an event (after a check-in or a role change). */
export const forgetRemote = (link: string) => { remote.delete(link); touch() }

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
  const t = tickets(e).find((x) => asks(x)) ?? tickets(e)[0]
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
