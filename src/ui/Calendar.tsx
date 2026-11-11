// Events in lists and cards, and the form that makes or edits one (Belfry events,
// work/events-design/PROTOCOL.md). The event's own screen is in Events.tsx.
import { useEffect, useRef, useState } from 'react'
import { BELFRY, THEME } from '../config'
import { jitsiLink } from '../core/actions'
import { calendar, eventByCoord, eventLink, groups, type CalEvent, type Rsvp, type Visibility } from '../core/model'
import { useVersion } from '../core/store'
import { me } from '../core/sync'
import { claimSlug, createEvent, editEvent, eventByLink, formOf, goingList, localTz, myState, people, rsvp, slugFree, slugOf, slugProblem, tickets, uploadCover, type EventForm } from '../core/events'
import { back, fmtDay, go, Sheet, Btn, Form, up, useFormDraft } from './common'
import { Tabs } from './ChannelInfo'
import { CalendarIcon, ImageIcon } from './icons'

/** Where an event opens in the chat: #/events/<link>; an older session by its coordinate. */
export const eventPath = (e: CalEvent) => (e.isNew ? 'events/' + encodeURIComponent(eventLink(e)) : 'cal/' + encodeURIComponent(e.coord))
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
  if (e.allDay) return 'All day'
  const tz = tzNote(e) ? e.tz : undefined
  const note = tzNote(e)
  return (e.end ? `${timeIn(e.start, tz)} – ${timeIn(e.end, tz)}` : timeIn(e.start, tz)) + (note ? ' ' + note : '')
}

export const going = (e: CalEvent) => (e.isNew ? goingList(e).length : [...e.rsvps.values()].filter((s) => s === 'accepted').length)

const endOf = (e: CalEvent) => +(e.end ?? e.start)
const startOfToday = () => +new Date(new Date().toDateString())
const isPast = (e: CalEvent) => endOf(e) < startOfToday()

/** Upcoming events (today onwards), of one channel (its cards) or of all. */
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
  const g = channel ? groups().find((x) => x.id === channel) : undefined
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
        <b>{e.status === 'cancelled' && <span className="badge cancel">Cancelled</span>} {e.title}</b>
        <small>{fmtDay(e.start)} · {when(e)}{e.location ? ' · ' + e.location : ''}{e.links.length ? ' · online' : ''}</small>
        <small>{s ? `${s} · ` : ''}{going(e)} going</small>
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
  ['link', 'Link', 'Anyone with the URL'],
  ['public', 'Public', 'Listed on the website'],
]

/** Make an event, or edit one (EDIT): title, Markdown description, cover, when (all day or
 *  times in a time zone), where, online link, who sees it, its custom URL. */
export function EventFormSheet({ edit, onClose, onSaved, visibility = 'members' }: { edit?: CalEvent; onClose: () => void; onSaved?: (link: string) => void; /** a new event's: #events public, #bookings members */ visibility?: Visibility }) {
  const start = new Date()
  start.setHours(start.getHours() + 1, 0, 0, 0)
  const end = new Date(+start + 3600_000)
  // a new event keeps its draft on this device until it is created (one per visibility: #events, #bookings)
  const [n, setN, clearDraft] = useFormDraft<EventForm>(edit ? null : 'event:' + visibility, () => edit ? formOf(edit) : {
    title: '', description: '', location: THEME === 'belfry' ? '' : 'Commons Hub Brussels', allDay: false,
    start: localInput(start), end: localInput(end), tz: localTz(), online: '', visibility, slug: '',
  })
  const set = (patch: Partial<EventForm>) => setN((x) => ({ ...x, ...patch }))
  const [slugEdited, setSlugEdited] = useState(!!edit?.slug)
  const [slugState, setSlugState] = useState<{ checking?: boolean; free?: boolean; msg?: string }>({})
  const [coverBusy, setCoverBusy] = useState(false)
  const [error, setError] = useState('')
  const asked = useRef(0)
  // the custom URL: from the title until edited; checked as it is typed
  useEffect(() => { if (!slugEdited && !edit) set({ slug: slugOf(n.title) }) }, [n.title]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    const s = n.slug
    if (!s || (edit && s === edit.slug)) { setSlugState({}); return }
    const p = slugProblem(s)
    if (p) { setSlugState({ free: false, msg: p }); return }
    const mine = ++asked.current
    setSlugState({ checking: true })
    const t = setTimeout(() => {
      slugFree(s, edit?.d).then((r) => { if (mine === asked.current) setSlugState({ free: r.free, msg: r.free ? 'Free' : `Taken. Try ${r.suggestion}` }) },
        () => { if (mine === asked.current) setSlugState({ msg: 'Could not check it now; it is checked again when you save.' }) })
    }, 350)
    return () => clearTimeout(t)
  }, [n.slug]) // eslint-disable-line react-hooks/exhaustive-deps
  const places = [...new Set(calendar().sort((a, b) => b.ev.created_at - a.ev.created_at).map((e) => e.location).filter((l): l is string => !!l?.trim()))].slice(0, 8)
  const zones = [...new Set([n.tz, localTz(), 'UTC', ...ZONES])]
  const badOnline = !!n.online.trim() && !/^https:\/\//.test(n.online.trim())
  const ok = !!n.title.trim() && !!n.start && !badOnline && slugState.free !== false && !coverBusy
  return (
    <Sheet title={edit ? 'Edit the event' : 'New event'} onClose={onClose}>
      <Form onSubmit={async (x) => {
        x.preventDefault()
        if (!ok) return
        setError('')
        try {
          const form = { ...n, slug: slugState.free === false ? '' : n.slug }
          if (edit) {
            if (form.slug && form.slug !== edit.slug) await claimSlug(edit.d, form.slug)
            await editEvent(edit, form)
            onClose()
            onSaved?.(form.slug || edit.d)
          } else {
            const d = await createEvent(form)
            clearDraft()
            onClose()
            onSaved?.(form.slug || d)
          }
        } catch (err) { setError(err instanceof Error ? err.message : String(err)) }
      }}>
        <label className="field"><span>Title</span>
          <input className="input" autoFocus={!edit} placeholder="e.g. Garden day" value={n.title} maxLength={200} onChange={(e) => set({ title: e.target.value })} /></label>
        <div className="field"><span>Cover</span></div>
        <label className={'cover-pick' + (n.cover ? ' has' : '')}>
          {n.cover ? <img src={n.cover} alt="" /> : <span><ImageIcon /> {coverBusy ? 'Uploading…' : 'Add a cover picture'}</span>}
          <input type="file" accept="image/*" hidden disabled={coverBusy} onChange={async (e) => {
            const f = e.target.files?.[0]
            e.target.value = ''
            if (!f) return
            setCoverBusy(true); setError('')
            try { set({ cover: await uploadCover(f) }) } catch (err) { setError(err instanceof Error ? err.message : String(err)) } finally { setCoverBusy(false) }
          }} />
        </label>
        {n.cover && <button type="button" className="link" style={{ margin: '-6px 0 12px' }} onClick={() => set({ cover: undefined })}>Remove the cover</button>}
        <label className="toggle"><input type="checkbox" checked={n.allDay} onChange={(e) => set({ allDay: e.target.checked })} /> All day</label>
        <label className="field"><span>Starts</span>
          <input className="input" type={n.allDay ? 'date' : 'datetime-local'} value={n.allDay ? n.start.slice(0, 10) : n.start} onChange={(e) => set({ start: e.target.value })} /></label>
        <label className="field"><span>Ends</span>
          <input className="input" type={n.allDay ? 'date' : 'datetime-local'} value={n.allDay ? n.end.slice(0, 10) : n.end} onChange={(e) => set({ end: e.target.value })} /></label>
        {!n.allDay && (
          <label className="field"><span>Time zone</span>
            <select className="input" value={n.tz} onChange={(e) => set({ tz: e.target.value })}>
              {zones.map((z) => <option key={z} value={z}>{z.replace(/_/g, ' ')}</option>)}
            </select></label>
        )}
        <label className="field"><span>Where</span>
          <input className="input" placeholder="A place, or leave empty if online only" value={n.location} onChange={(e) => set({ location: e.target.value })} /></label>
        {places.length > 0 && (
          <div className="chips" style={{ margin: '-6px 0 14px' }}>
            {places.map((pl) => <button type="button" key={pl} className={'chip' + (n.location === pl ? ' on' : '')} onClick={() => set({ location: n.location === pl ? '' : pl })}>📍 {pl}</button>)}
          </div>
        )}
        <label className="field"><span>Online</span>
          <input className="input" type="url" inputMode="url" placeholder="A Jitsi, Google Meet or Zoom link" value={n.online} onChange={(e) => set({ online: e.target.value })} /></label>
        <div className="chips" style={{ margin: '-6px 0 14px' }}>
          <button type="button" className="chip" onClick={() => set({ online: jitsiLink(n.title || 'meeting') })}>🎥 New Jitsi link</button>
        </div>
        {badOnline && <p className="error" style={{ marginTop: -8 }}>The link should start with https://</p>}
        <label className="field"><span>Description</span>
          <textarea className="input" rows={4} placeholder="Markdown: **bold**, *italic*, - lists, [links](https://…)" value={n.description} onChange={(e) => set({ description: e.target.value })} /></label>
        <div className="field"><span>Who sees it</span></div>
        <div className="choice three" style={{ marginTop: -8 }}>
          {VIS.map(([v, label, hint]) => <button type="button" key={v} className={n.visibility === v ? 'on' : ''} aria-pressed={n.visibility === v} onClick={() => set({ visibility: v })}><b>{label}</b><small>{hint}</small></button>)}
        </div>
        <label className="field"><span>URL</span>
          <div className="url-field"><span className="pre">/events/</span>
            <input className="input" value={n.slug} placeholder={edit?.d ?? 'garden-day'} maxLength={40} autoCapitalize="none" autoCorrect="off" spellCheck={false}
              onChange={(e) => { setSlugEdited(true); set({ slug: e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, '') }) }} /></div></label>
        {slugState.msg && <p className={'hint' + (slugState.free === false ? ' error' : '')} role="status" style={{ marginTop: -8 }}>{slugState.checking ? 'Checking…' : slugState.msg}</p>}
        {error && <p className="error" role="alert">{error}</p>}
        <button className="btn" disabled={!ok}>{edit ? 'Save' : 'Create the event'}</button>
        <p className="hint" style={{ marginTop: 10 }}>{edit ? 'Everyone who RSVPed sees the change.' : 'It gets its own chat, photos and to-dos. Send its URL in a channel to show it there.'}</p>
      </Form>
    </Sheet>
  )
}

/** Kept for the composer's ＋ (Chat.tsx): a new event; its URL is put in the composer after. */
export function NewEventSheet({ onClose, onCreated, visibility }: { channel?: string; onClose: () => void; onCreated?: (url: string) => void; visibility?: Visibility }) {
  return <EventFormSheet onClose={onClose} visibility={visibility} onSaved={(link) => (onCreated ? onCreated(`https://${BELFRY.domain}/events/${link}`) : go('events/' + encodeURIComponent(link)))} />
}

export { CalendarIcon }
