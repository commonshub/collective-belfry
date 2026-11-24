// Events in lists and cards, and the form that makes or edits one (Belfry events,
// work/events-design/PROTOCOL.md). The event's own screen is in Events.tsx.
import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { BELFRY, NAME, THEME } from '../config'
import { postMessage } from '../core/actions'
import { typingSlug } from '../core/cards'
import { displayName } from '../core/model'
import { useCommunityName } from './community'
import { jitsiLink, mayPickAddress, randomEnd } from '../core/actions'
import { calendar, eventByCoord, eventLink, group, type CalEvent, type Rsvp, type Visibility, nameOf } from '../core/model'
import { useVersion } from '../core/store'
import { me } from '../core/sync'
import PlaceInput from './PlaceInput'
import { allDayWord, claimSlug, isPastEvent, createEvent, editEvent, eventByLink, formOf, goingList, localTz, myState, people, rsvp, slugFree, slugOf, slugProblem, tickets, uploadCover, fetchEventDetails, utcToZoned, type EventForm } from '../core/events'
import { back, fmtDay, go, Sheet, Btn, Form, up, useFormDraft } from './common'
import { Tabs } from './ChannelInfo'
import { CalendarIcon, ImageIcon } from './icons'

/** Where an event opens in the chat: #/events/<link>; an older session by its coordinate. */
// every event by its link (its slug, else its d), never #/cal/<coord>: an address carries no key or
// id (lead, 2026-11-20); old #/cal/<coord> links still open (App.tsx moves them here)
export const eventPath = (e: CalEvent) => 'events/' + encodeURIComponent(eventLink(e))
export const openEvent = (e: CalEvent) => go(eventPath(e))

const timeIn = (d: Date, tz?: string) => {
  try { return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', ...(tz ? { timeZone: tz } : {}) }) } catch { return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) }
}
/** The time zone's short name when it is not the viewer's own ("CET", "GMT+1"). */
export function tzNote(e: CalEvent): string {
  if (e.allDay || !e.tz) return ''
  try {
    if (e.tz === Intl.DateTimeFormat().resolvedOptions().timeZone) return ''
    return new Intl.DateTimeFormat([], { timeZone: e.tz, timeZoneName: 'short' }).formatToParts(e.start).find((p) => p.type === 'timeZoneName')?.value ?? e.tz
  } catch { return e.tz }
}

export const when = (e: CalEvent) => {
  if (e.allDay) return allDayWord(e) // several days: their days (SPEC 19.65)
  const tz = tzNote(e) ? e.tz : undefined
  const note = tzNote(e)
  return (e.end ? `${timeIn(e.start, tz)} – ${timeIn(e.end, tz)}` : timeIn(e.start, tz)) + (note ? ' ' + note : '')
}

export const going = (e: CalEvent) => (e.isNew ? goingList(e).length : [...e.rsvps.values()].filter((s) => s === 'accepted').length)

// over once it has ended (events.ts eventEnd), not at the end of its day
const isPast = (e: CalEvent) => isPastEvent(e)

/** Upcoming events (not over yet: going on now included), of one channel (its cards) or of all. */
export const upcoming = (channel?: string) =>
  calendar().filter((e) => !isPast(e) && (!channel || e.channels.includes(channel)))

export const LABEL: Record<Rsvp, string> = { accepted: 'Going', tentative: 'Maybe', declined: "Can't go" }
const STATE: Record<string, string> = { ticket: 'Going', maybe: 'Maybe', cant: "Can't go", applied: 'Applied', waitlist: 'Waitlist', declined: 'Not this time', host: 'Hosting' }
export const stateLabel = (e: CalEvent) => (e.isNew ? STATE[myState(e)] : e.rsvps.get(me()) ? LABEL[e.rsvps.get(me())!] : '')

type Tab = 'upcoming' | 'mine' | 'hosting' | 'past'

/** #/events (every event the viewer can read) and #/c/<id>/events (the ones carded there). */
export function EventsScreen({ channel }: { channel?: string }) {
  useVersion()
  const [creating, setCreating] = useState(false)
  const [tab, setTab] = useState<Tab>('upcoming')
  const g = channel ? group(channel) : undefined
  const list = calendar().filter((e) => !channel || e.channels.includes(channel))
  const mine = (e: CalEvent) => { const s = myState(e); return s !== 'none' && s !== 'declined' && s !== 'cant' }
  const shown = tab === 'past' ? list.filter(isPast).reverse()
    : list.filter((e) => !isPast(e) && (tab === 'upcoming' || (tab === 'mine' ? mine(e) : e.hosts.has(me()))))
  const empty = { upcoming: 'Nothing planned yet. Make the first event.', mine: 'You’re not going to anything yet.', hosting: 'You’re not hosting anything yet.', past: 'No past events.' }[tab]
  return (
    <div className="screen">
      <div className="topbar">
        <button className="icon-btn" aria-label="Back" onClick={() => (channel ? up('c/' + encodeURIComponent(channel)) : back('c'))}>‹</button>
        <h1>Events{g && <small># {g.name}</small>}</h1>
        <button className="icon-btn" aria-label="New event" onClick={() => setCreating(true)}>＋</button>
      </div>
      <Tabs tab={tab} setTab={setTab} tabs={[['upcoming', 'Upcoming'], ['mine', 'Mine'], ['hosting', 'Hosting'], ['past', 'Past']]} />
      <div className="scroll">
        {shown.length === 0 && <div className="empty">{empty}</div>}
        <EventRows list={shown} days={tab !== 'past'} />
        <div style={{ padding: 16 }}><button className="btn ghost" onClick={() => setCreating(true)}>＋ New event</button></div>
      </div>
      {creating && <EventFormSheet onClose={() => setCreating(false)} onSaved={(link) => go('events/' + encodeURIComponent(link))} />}
    </div>
  )
}
/** The older name (other screens import it). */
export const CalendarList = EventsScreen

function EventRows({ list, days = true }: { list: CalEvent[]; days?: boolean }) {
  let lastDay = ''
  return (
    <>
      {list.map((e) => {
        const day = fmtDay(e.start)
        const header = days && day !== lastDay ? <div className="section-title">{day}</div> : null
        lastDay = day
        return <div key={e.coord}>{header}<EventRow e={e} /></div>
      })}
    </>
  )
}

function EventRow({ e }: { e: CalEvent }) {
  const s = stateLabel(e)
  return (
    <button className="ev" onClick={() => openEvent(e)} style={{ opacity: e.ev.pending ? 0.6 : 1 }}>
      {e.cover ? <img className="ev-thumb" src={e.cover} alt="" loading="lazy" /> : <DateChip d={e.start} />}
      <div className="info">
        <b>{e.status === 'cancelled' && <span className="badge cancel">Cancelled</span>}{e.meeting && <span className="badge meet">Meeting</span>} {e.title}</b>
        <small>{fmtDay(e.start)} · {when(e)}{e.location ? ' · ' + e.location : ''}{e.links.length ? ' · online' : ''}</small>
        <small>{s ? `${s} · ` : ''}{e.meeting ? `${new Set([e.creator, ...e.invited]).size} people` : `${going(e)} going`}</small>
      </div>
    </button>
  )
}

export const DateChip = ({ d }: { d: Date }) => (
  <div className="date"><small>{d.toLocaleDateString([], { month: 'short' })}</small><b>{d.getDate()}</b></div>
)

/** An event's card in a channel: from a message's `a` tag or its URL (ours, or the belfry's
 *  answer for a guest who can't read the community relay). Going / Maybe in one tap when the
 *  event has the plain RSVP. */
export function EventCard({ coord, link, fallback }: { coord?: string; link?: string; fallback: string }) {
  useVersion()
  const found = coord ? eventByCoord(coord) : undefined
  const r = !found && link ? eventByLink(link) : undefined
  const e = found ?? r?.e
  if (!e) return fallback ? <div className="text">{fallback}</div> : null
  const tks = e.isNew ? tickets(e, r?.api) : []
  const plain = !e.isNew || (tks.length === 1 && tks[0].implicit && !e.remote)
  const mine = e.isNew ? myState(e, people(e), r?.api) : undefined
  const answered = e.isNew ? mine !== 'none' && mine !== 'host' : !!e.rsvps.get(me())
  const open = e.status !== 'cancelled' && !isPast(e)
  return (
    <div className={'ev-card' + (e.cover ? ' has-cover' : '')} role="button" tabIndex={0} onClick={() => openEvent(e)} onKeyDown={(k) => { if (k.key === 'Enter') openEvent(e) }}>
      {e.cover && <img className="ev-card-cover" src={e.cover} alt="" loading="lazy" />}
      <div className="ev-card-row">
        <DateChip d={e.start} />
        <span className="info">
          <b>{e.status === 'cancelled' && <span className="badge cancel">Cancelled</span>} {e.title}</b>
          <small>{fmtDay(e.start)} · {when(e)}{e.location ? ' · ' + e.location : ''}</small>
          <small>{answered ? `${stateLabel(e)} · ` : ''}{going(e)} going</small>
        </span>
      </div>
      {open && !answered && (plain
        ? <div className="rsvp card-rsvp" onClick={(x) => x.stopPropagation()}>
            <Btn onClick={() => rsvp(e, 'accepted')}>Going</Btn>
            <Btn onClick={() => rsvp(e, 'tentative')}>Maybe</Btn>
          </div>
        : <div className="card-rsvp"><span className="line-pill ask">RSVP</span></div>)}
    </div>
  )
}

const pad = (n: number) => String(n).padStart(2, '0')
function localInput(d: Date) {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`
}
const ZONES = (() => {
  try { return (Intl as unknown as { supportedValuesOf?: (k: string) => string[] }).supportedValuesOf?.('timeZone') ?? [] } catch { return [] }
})()

const VIS: [Visibility, string, string][] = [
  ['members', 'Members', 'Only members see it'],
  ['link', 'Hidden', 'Not listed: anyone with the link'],
  ['public', 'Public', 'Listed on the website'],
]

/** "Brussels time (CET)": a time zone as people say it. */
function tzLabel(tz: string, at = new Date()): string {
  const city = (tz.split('/').pop() ?? tz).replace(/_/g, ' ')
  let abbr = ''
  try { abbr = new Intl.DateTimeFormat('en-GB', { timeZone: tz, timeZoneName: 'short' }).formatToParts(at).find((x) => x.type === 'timeZoneName')?.value ?? '' } catch { /* unknown zone */ }
  return `${city} time${abbr ? ` (${abbr})` : ''}`
}
type Where = 'in' | 'online' | 'both'
const WHERE: [Where, string][] = [['in', 'In person'], ['online', 'Online'], ['both', 'Both']]
const VIS3: [Visibility, string][] = [['members', 'Members'], ['public', 'Public'], ['link', 'Hidden']]
const hhmm = (s: string) => s.slice(11, 16)
const ymdOf = (s: string) => s.slice(0, 10)
const hostOf = (u: string) => { try { return new URL(u).hostname.replace(/^www\./, '') } catch { return '' } }

/** What changed between two versions of the form, for "Save changes?" (Event form canvas, evform-edit-confirm):
 *  its lines, the ["changed", …] words, and whether people going are told by default (date, time, place or
 *  the Join online link: the canvas's rule, EV-63). */
export function eventChanges(a: EventForm, b: EventForm): { lines: string[]; changed: string[]; tell: boolean } {
  const lines: string[] = [], changed = new Set<string>()
  if (a.allDay !== b.allDay || a.tz !== b.tz || ymdOf(a.start) !== ymdOf(b.start) || ymdOf(a.end) !== ymdOf(b.end) || hhmm(a.start) !== hhmm(b.start) || hhmm(a.end) !== hhmm(b.end)) {
    changed.add('time')
    if (ymdOf(a.start) !== ymdOf(b.start)) lines.push(`Now on ${fmtDay(new Date(ymdOf(b.start) + 'T12:00'))} (was ${fmtDay(new Date(ymdOf(a.start) + 'T12:00'))})`)
    if (!b.allDay && hhmm(a.start) !== hhmm(b.start)) lines.push(`Starts at ${hhmm(b.start)}${a.allDay ? '' : ` (was ${hhmm(a.start)})`}`)
    if (!b.allDay && hhmm(a.end) !== hhmm(b.end) && b.end) lines.push(`Ends at ${hhmm(b.end)}${a.allDay || !a.end ? '' : ` (was ${hhmm(a.end)})`}`)
    if (a.allDay !== b.allDay) lines.push(b.allDay ? 'All day now' : 'Not all day any more')
    if (a.tz !== b.tz && !b.allDay) lines.push(`In ${tzLabel(b.tz)}`)
  }
  if (a.location.trim() !== b.location.trim()) { changed.add('place'); lines.push(b.location.trim() ? `Place: ${b.location.trim()}` : 'No place any more') }
  if (a.online.trim() !== b.online.trim()) { changed.add('online'); lines.push(b.online.trim() ? 'New Join online link' : 'No Join online link any more') }
  if (a.title.trim() !== b.title.trim()) { changed.add('title'); lines.push(`Title: ${b.title.trim()}`) }
  if (a.description !== b.description) { changed.add('description'); lines.push('New description') }
  if ((a.cover ?? '') !== (b.cover ?? '')) { changed.add('cover'); lines.push(b.cover ? 'New cover' : 'No cover any more') }
  if ((a.website ?? '').trim() !== (b.website ?? '').trim()) { changed.add('website'); lines.push(b.website?.trim() ? `Event website: ${hostOf(b.website.trim())}` : 'No event website any more') }
  if (a.visibility !== b.visibility) { changed.add('visibility'); lines.push(`Who sees it: ${VIS3.find(([v]) => v === b.visibility)?.[1] ?? b.visibility}`) }
  if (a.slug !== b.slug && b.visibility === 'public') { changed.add('page'); lines.push(`Our page: ${BELFRY.domain}/events/${b.slug}`) }
  return { lines, changed: [...changed], tell: changed.has('time') || changed.has('place') || changed.has('online') }
}

/** Make an event, or edit one (EDIT): Event form canvas v15 (evform-new … evform-laptop; Xavier approved).
 *  Top to bottom: Event website (paste it and the belfry fills in the rest), cover, title, when, where (In
 *  person / Online / Both → Place, Join online), description, who sees it, (public only) our page, tickets,
 *  co-hosts, Create. Editing marks changed fields and asks before saving, with "Tell the N people going". */
export function EventFormSheet({ edit, onClose, onSaved, visibility = 'members', postedIn, going = 0, onTickets, onCohosts }: { edit?: CalEvent; onClose: () => void; onSaved?: (link: string) => void; /** made from a channel: its card goes there */ postedIn?: string; /** a new event's: #events (members) or #bookings */ visibility?: Visibility
  /** editing: how many people are going (the switch on save) */ going?: number; /** editing: the existing Tickets and Co-hosts screens */ onTickets?: () => void; onCohosts?: () => void }) {
  const community = useCommunityName(NAME)
  const start = new Date()
  start.setHours(start.getHours() + 1, 0, 0, 0)
  const end = new Date(+start + 3600_000)
  const was = useMemo(() => (edit ? formOf(edit) : null), [edit?.ev.id]) // eslint-disable-line react-hooks/exhaustive-deps
  // a new event keeps its draft on this device until it is created (one per visibility: #events, #bookings)
  const [n, setN, clearDraft] = useFormDraft<EventForm>(edit ? null : 'event:' + visibility, () => was ?? {
    title: '', description: '', location: THEME === 'belfry' ? '' : 'Commons Hub Brussels', allDay: false,
    start: localInput(start), end: localInput(end), tz: localTz(), online: '', website: '', visibility, slug: '',
  })
  const set = (patch: Partial<EventForm>) => setN((x) => ({ ...x, ...patch }))
  const [where, setWhere] = useState<Where>(() => (n.location.trim() && n.online.trim() ? 'both' : n.online.trim() && !n.location.trim() ? 'online' : 'in'))
  const [otherDay, setOtherDay] = useState(() => !!n.end && ymdOf(n.end) !== ymdOf(n.start))
  const [tzOpen, setTzOpen] = useState(false)
  const [timesEdited, setTimesEdited] = useState(!!edit)
  const [slugEdited, setSlugEdited] = useState(!!edit?.slug)
  const [slugState, setSlugState] = useState<{ checking?: boolean; free?: boolean; problem?: string; suggestion?: string; unknown?: boolean }>({})
  const [coverBusy, setCoverBusy] = useState(false)
  const [error, setError] = useState('')
  const [fetching, setFetching] = useState<'' | 'busy' | string>('') // '' · 'busy' · the site it filled in from
  const [confirm, setConfirm] = useState<{ lines: string[]; changed: string[]; tell: boolean } | null>(null)
  const [tell, setTell] = useState(false)
  const asked = useRef(0)
  // the page address: from the title until edited; only the belfry's admins pick a plain one (#issues 2026-11-15)
  const pick = mayPickAddress()
  const [slugEnd] = useState(randomEnd)
  useEffect(() => { if (!slugEdited && !edit) set({ slug: pick ? slugOf(n.title) : (slugOf(n.title).slice(0, 35) || 'event') + '-' + slugEnd }) }, [n.title]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    const sl = n.slug
    if (!sl || (edit && sl === edit.slug)) { setSlugState(sl ? { free: true } : {}); return }
    const pr = slugProblem(sl)
    if (pr) { setSlugState({ free: false, problem: pr }); return }
    const mine = ++asked.current
    setSlugState({ checking: true })
    const t = setTimeout(() => {
      slugFree(sl, edit?.d).then((r) => { if (mine === asked.current) setSlugState({ free: r.free, suggestion: r.free ? '' : r.suggestion }) },
        () => { if (mine === asked.current) setSlugState({ unknown: true }) })
    }, 350)
    return () => clearTimeout(t)
  }, [n.slug]) // eslint-disable-line react-hooks/exhaustive-deps
  // the event that already uses the address, when I can see it ("See that event ›")
  const takenBy = slugState.free === false && !slugState.problem ? calendar().find((x) => x.slug === n.slug && x.d !== edit?.d) : undefined

  // "Event website": pasted → the belfry reads the page and fills in what is still empty (never Join online)
  const fetchedFor = useRef(edit?.website ?? '')
  useEffect(() => {
    const u = (n.website ?? '').trim()
    if (!/^https:\/\/[^\s/]+\.[^\s]+/.test(u) || u === fetchedFor.current) return
    const t = setTimeout(async () => {
      fetchedFor.current = u
      setFetching('busy')
      const d = await fetchEventDetails(u)
      if (!d) { setFetching(''); return }
      const patch: Partial<EventForm> = {}
      if (d.title && !n.title.trim()) patch.title = d.title
      if (d.description && !n.description.trim()) patch.description = d.description
      if (d.place && !n.location.trim()) { patch.location = d.place; setWhere((w) => (w === 'online' ? 'both' : w)) }
      if (d.start && !timesEdited) {
        const tz = d.tz || n.tz
        patch.tz = tz; patch.allDay = d.allDay
        patch.start = d.allDay ? utcToZoned(d.start, tz).slice(0, 10) + 'T00:00' : utcToZoned(d.start, tz)
        if (d.end) { patch.end = d.allDay ? utcToZoned(d.end, tz).slice(0, 10) + 'T00:00' : utcToZoned(d.end, tz); setOtherDay(ymdOf(patch.end) !== ymdOf(patch.start)) }
      }
      set(patch)
      if (d.image && !n.cover) {
        try { const b = await (await fetch(d.image)).blob(); if (/^image\//.test(b.type)) set({ cover: await uploadCover(new File([b], 'cover.jpg', { type: b.type })) }) } catch { /* no cover from it */ }
      }
      setFetching(hostOf(d.url) || hostOf(u))
    }, 600)
    return () => clearTimeout(t)
  }, [n.website]) // eslint-disable-line react-hooks/exhaustive-deps

  const places = [...new Set(calendar().sort((a, b) => b.ev.created_at - a.ev.created_at).map((e) => e.location).filter((l): l is string => !!l?.trim()))].slice(0, 8)
  const zones = [...new Set([n.tz, localTz(), 'UTC', ...ZONES])]
  const badOnline = where !== 'in' && !!n.online.trim() && !/^https:\/\//.test(n.online.trim())
  const badSite = !!n.website?.trim() && !/^https:\/\//.test(n.website.trim())
  const publicPage = n.visibility === 'public'
  const ok = !!n.title.trim() && !!n.start && !badOnline && !badSite && (!publicPage || slugState.free !== false) && !coverBusy && fetching !== 'busy'
  // the form as it will be saved: Where decides which of place / Join online is kept
  const final = (): EventForm => ({ ...n, location: where === 'online' ? '' : n.location, online: where === 'in' ? '' : n.online, slug: slugState.free === false ? (edit?.slug ?? '') : n.slug })
  const changedChip = (k: keyof EventForm | 'time') => {
    if (!was) return null
    const a = was, b = final()
    const diff = k === 'time' ? a.start !== b.start || a.end !== b.end || a.allDay !== b.allDay || a.tz !== b.tz : (a[k] ?? '') !== (b[k] ?? '')
    return diff ? <span className="chip evf-changed">Changed</span> : null
  }
  const setStartDate = (dt: string) => { const t = n.allDay ? '00:00' : hhmm(n.start) || '10:00'; set({ start: `${dt}T${t}`, ...(otherDay ? {} : { end: `${dt}T${n.allDay ? '00:00' : hhmm(n.end) || t}` }) }); setTimesEdited(true) }
  const save = async (form: EventForm, extra: string[][] = [], line = '') => {
    setError('')
    try {
      if (edit) {
        if (form.slug && form.slug !== edit.slug) await claimSlug(edit.d, form.slug)
        await editEvent(edit, form, extra)
        if (line) await postMessage(edit.groupId, line, []) // the chat line (the server mails people going: notify=1)
        onClose()
        onSaved?.(form.slug || edit.d)
      } else {
        const d = await createEvent(form)
        clearDraft()
        onClose()
        onSaved?.(form.slug || d)
      }
    } catch (err) { setError(err instanceof Error ? err.message : String(err)) }
  }
  const field = (label: string, chip: ReactNode, child: ReactNode, cls = 'field') => <label className={cls}><span>{label}{chip}</span>{child}</label>

  if (confirm) return (
    <Sheet title="Save changes?" onClose={() => setConfirm(null)}>
      <ul className="evf-changes">{confirm.lines.map((l, i) => <li key={i}>{l}</li>)}</ul>
      {going > 0 && <label className="switch-row evf-tell">
        <span><b>Tell the {going} {going === 1 ? 'person' : 'people'} going</b><small>A line in the event’s chat and an e-mail.</small></span>
        <input type="checkbox" role="switch" aria-label={`Tell the ${going} people going`} checked={tell} onChange={(x) => setTell(x.target.checked)} />
      </label>}
      {error && <p className="error" role="alert">{error}</p>}
      <div className="row-btns two">
        <Btn className="btn" onClick={() => save(final(), tell && going > 0 ? [['notify', '1'], ...confirm.changed.map((c) => ['changed', c])] : [], tell && going > 0 ? '📅 ' + confirm.lines.join(' · ') : '')}>Save changes</Btn>
        <button type="button" className="btn ghost" onClick={() => setConfirm(null)}>Cancel</button>
      </div>
    </Sheet>
  )
  return (
    <Sheet title={edit ? 'Edit event' : 'New event'} onClose={onClose}>
      <Form className="evf" onSubmit={async (x) => {
        x.preventDefault()
        if (!ok) return
        if (edit && was) {
          const c = eventChanges(was, final())
          if (!c.lines.length) { onClose(); return }
          setTell(c.tell); setConfirm(c); return
        }
        await save(final())
      }}>
        {/* the laptop: Save top right, with its line (UI-26, evform-laptop); the phone keeps the button at the bottom */}
        <div className="evf-top">
          <p className="hint">{edit ? 'Everyone going is told when the time, the place or the Join online link changes' : postedIn ? `Its card is posted in ${postedIn}.` : 'It gets its own chat, photos and to-dos.'}</p>
          <button className="btn" disabled={!ok}>{edit ? 'Save' : 'Create the event'}</button>
        </div>
        <div className="evf-col">
          <div className="evf-card">
            {field('Event website', changedChip('website'), <input className="input" type="url" inputMode="url" placeholder="https://… (optional)" value={n.website ?? ''} onChange={(e) => set({ website: e.target.value })} />)}
            <p className="hint">The event’s own site, if it has one (e.g. a conference site). Paste it and we fill in the rest.</p>
            {fetching === 'busy' ? <p className="hint evf-fetching" role="status"><span className="spinner" aria-hidden="true" /> Fetching event details…</p>
              : fetching ? <p className="hint evf-fetched" role="status">✓ Filled in from {fetching}. Check it and change anything.</p> : null}
            {badSite && <p className="error">The address should start with https://</p>}
          </div>
          <div className={fetching === 'busy' ? 'evf-busy' : undefined} aria-busy={fetching === 'busy'}>
            <label className={'cover-pick' + (n.cover ? ' has' : '')}>
              {n.cover ? <img src={n.cover} alt="" /> : <span className="cover-empty"><span><ImageIcon /> {coverBusy ? 'Uploading…' : 'Add a cover'}</span></span>}
              {n.cover && <span className="cover-change">{coverBusy ? 'Uploading…' : 'Change cover'}</span>}
              <input type="file" accept="image/*" hidden disabled={coverBusy} onChange={async (e) => {
                const f = e.target.files?.[0]
                e.target.value = ''
                if (!f) return
                setCoverBusy(true); setError('')
                try { set({ cover: await uploadCover(f) }) } catch (err) { setError(err instanceof Error ? err.message : String(err)) } finally { setCoverBusy(false) }
              }} />
            </label>
            {field('Title', changedChip('title'), <input className="input" aria-label="Title" autoFocus={!edit} value={n.title} maxLength={200} onChange={(e) => set({ title: e.target.value })} />)}
            <div className="field"><span>When{changedChip('time')}</span>
              <div className="evf-when">
                <input className="input" type="date" aria-label="Date" value={ymdOf(n.start)} onChange={(e) => e.target.value && setStartDate(e.target.value)} />
                {!n.allDay && <div className="evf-times">
                  <input className="input" type="time" aria-label="Starts" value={hhmm(n.start)} onChange={(e) => { set({ start: `${ymdOf(n.start)}T${e.target.value}` }); setTimesEdited(true) }} />
                  <span aria-hidden="true">–</span>
                  <input className="input" type="time" aria-label="Ends" value={hhmm(n.end)} onChange={(e) => { set({ end: `${ymdOf(n.end) || ymdOf(n.start)}T${e.target.value}` }); setTimesEdited(true) }} />
                </div>}
                {otherDay && <input className="input" type="date" aria-label="Ends on" value={ymdOf(n.end)} onChange={(e) => { set({ end: `${e.target.value}T${n.allDay ? '00:00' : hhmm(n.end) || '18:00'}` }); setTimesEdited(true) }} />}
              </div>
              <p className="hint evf-tz">{!n.allDay && <><button type="button" className="link" onClick={() => setTzOpen(!tzOpen)}>{tzLabel(n.tz)}</button> · </>}
                <button type="button" className="link" onClick={() => { if (otherDay) set({ end: `${ymdOf(n.start)}T${hhmm(n.end) || '18:00'}` }); setOtherDay(!otherDay) }}>{otherDay ? 'Ends the same day' : 'Ends on another day'}</button></p>
              {tzOpen && !n.allDay && <select className="input" aria-label="Time zone" value={n.tz} onChange={(e) => { set({ tz: e.target.value }); setTzOpen(false) }}>
                {zones.map((z) => <option key={z} value={z}>{z.replace(/_/g, ' ')}</option>)}
              </select>}
            </div>
            <label className="toggle"><input type="checkbox" aria-label="All day" checked={n.allDay} onChange={(e) => { set({ allDay: e.target.checked }); setTimesEdited(true) }} /> All day</label>
            <div className="field"><span>Where</span></div>
            <div className="seg2 evf-where" role="radiogroup" aria-label="Where">
              {WHERE.map(([w, label]) => <button key={w} type="button" role="radio" aria-checked={where === w} className={where === w ? 'on' : ''} onClick={() => setWhere(w)}>{label}</button>)}
            </div>
            {where !== 'online' && field('Place', changedChip('location'), <PlaceInput placeholder="Community hall, Rue Haute 12" value={n.location} past={places} onChange={(v) => set({ location: v })} />)}
            {where !== 'in' && <div className="evf-card">
              {field('Join online', changedChip('online'), <input className="input" type="url" inputMode="url" placeholder="Paste a meeting link" value={n.online} onChange={(e) => set({ online: e.target.value })} />)}
              <p className="hint">The video call link. Only people going see it, from 1 hour before the start.</p>
              <p className="hint">or <button type="button" className="link" onClick={() => set({ online: jitsiLink(n.title || 'meeting') })}>create a Jitsi link</button></p>
              {badOnline && <p className="error">The link should start with https://</p>}
            </div>}
          </div>
        </div>
        <div className="evf-col">
          {field('Description', changedChip('description'), <textarea className="input" rows={4} value={n.description} onChange={(e) => set({ description: e.target.value })} />)}
          <div className="field"><span>Who sees it{changedChip('visibility')}</span></div>
          <div className="seg2" role="radiogroup" aria-label="Who sees it">
            {VIS3.map(([v, label]) => <button key={v} type="button" role="radio" aria-checked={n.visibility === v} className={n.visibility === v ? 'on' : ''} onClick={() => set({ visibility: v })}>{label}</button>)}
          </div>
          {publicPage && <div className="evf-card evf-page">
            <div className="field"><span>Page on {community}’s website{changedChip('slug')}</span></div>
            <p className="hint">Your community’s page for this event, with RSVP, tickets and the chat. Share this one.</p>
            <div className="url-field"><span className="pre">{BELFRY.domain}/events/</span>
              <input className="input" aria-label="Page address" value={n.slug} placeholder="open-day" maxLength={40} autoCapitalize="none" autoCorrect="off" spellCheck={false} readOnly={!pick}
                onChange={(e) => { setSlugEdited(true); set({ slug: typingSlug(e.target.value).slice(0, 40) }) }} /></div>
            <p className={'hint evf-slug' + (slugState.free === false ? ' error' : '')} role="status">
              {slugState.checking ? 'Checking…' : slugState.problem ? slugState.problem : slugState.free ? '✓ Available' : slugState.free === false ? (takenBy
                ? <>✗ Already used by <b>{takenBy.title}</b> · <button type="button" className="link" onClick={() => { onClose(); go('events/' + encodeURIComponent(eventLink(takenBy))) }}>See that event ›</button></>
                : 'Already used by another event') : slugState.unknown ? 'Couldn’t check it now: it’s checked again when you save.' : ''}
            </p>
            {slugState.free === false && slugState.suggestion && <p className="hint">{slugState.suggestion} is free · <button type="button" className="link" onClick={() => { setSlugEdited(true); set({ slug: slugState.suggestion! }) }}>Use it</button></p>}
            {!pick && <p className="hint">From the title, with 4 random characters at the end: the belfry’s admins choose plain addresses.</p>}
          </div>}
          {edit && (onTickets || onCohosts) && <div className="act-group evf-rows">
            {onTickets && <button type="button" className="act" onClick={onTickets}><span>Tickets</span><small>{tickets(edit).length ? `${tickets(edit).length} ${tickets(edit).length === 1 ? 'ticket' : 'tickets'}` : 'Free RSVP'} ›</small></button>}
            {onCohosts && <button type="button" className="act" onClick={onCohosts}><span>Co-hosts</span><small>{[...edit.hosts].filter((k) => k !== edit.creator).map((k) => displayName(k)).join(', ') || 'Invite one'} ›</small></button>}
          </div>}
          {error && <p className="error" role="alert">{error}</p>}
          <button className="btn evf-bottom" disabled={!ok}>{edit ? 'Save changes' : 'Create the event'}</button>
          <p className="hint evf-bottom" style={{ marginTop: 10 }}>{edit ? 'Everyone going is told when the time, the place or the Join online link changes.' : postedIn ? `It gets its own chat, photos and to-dos, and its card is posted in ${postedIn}.` : 'It gets its own chat, photos and to-dos.'}</p>
        </div>
      </Form>
    </Sheet>
  )
}

/** Kept for the composer's ＋ (Chat.tsx): a new event; its URL is put in the composer after. */
export function NewEventSheet({ channel, onClose, onCreated, visibility }: { channel?: string; onClose: () => void; onCreated?: (url: string) => void; visibility?: Visibility }) {
  return <EventFormSheet onClose={onClose} visibility={visibility} postedIn={onCreated && channel ? (group(channel)?.isPod ? '' : '#') + (nameOf(channel)) : undefined} onSaved={(link) => (onCreated ? onCreated(`https://${BELFRY.domain}/events/${link}`) : go('events/' + encodeURIComponent(link)))} />
}

export { CalendarIcon }
