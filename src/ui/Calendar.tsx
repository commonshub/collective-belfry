import { useState } from 'react'
import { Markdown } from './markdown'
import { THEME } from '../config'
import { createEvent, deleteEvent, jitsiLink, meetingLabel, openEventChat, rsvp, type NewEvent } from '../core/actions'
import { calendar, displayName, groups, type CalEvent, type Rsvp } from '../core/model'
import { useVersion } from '../core/store'
import { me } from '../core/sync'
import { Avatar, back, fmtDay, go, Sheet } from './common'
import { Conversation } from './Chat'
import { TopicsBar } from './Topics'
import { Files, Tabs, Tasks } from './ChannelInfo'
import { FollowButton } from './Follow'

const when = (e: CalEvent) => {
  if (e.allDay) return 'All day'
  const t = (d: Date) => d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  return e.end ? `${t(e.start)} – ${t(e.end)}` : t(e.start)
}

const going = (e: CalEvent) => [...e.rsvps.values()].filter((s) => s === 'accepted').length

type Filter = 'going' | 'maybe' | 'all'

const endOf = (e: CalEvent) => +(e.end ?? e.start)
const startOfToday = () => +new Date(new Date().toDateString())

/** Upcoming events (today onwards), of one channel or of all. */
export const upcoming = (channel?: string) =>
  calendar().filter((e) => endOf(e) >= startOfToday() && (!channel || e.channel === channel))

export function CalendarList({ channel }: { channel?: string }) {
  useVersion()
  const [creating, setCreating] = useState(false)
  const [filter, setFilter] = useState<Filter>('all')
  const g = channel ? groups().find((x) => x.id === channel) : undefined
  const mine = (e: CalEvent) => e.rsvps.get(me())
  const soon = upcoming(channel).filter((e) =>
    filter === 'going' ? mine(e) === 'accepted' : filter === 'maybe' ? mine(e) === 'tentative' : true)
  const past = filter === 'all'
    ? calendar().filter((e) => endOf(e) < startOfToday() && (!channel || e.channel === channel)).reverse()
    : []
  const empty = { going: "You're not going to anything yet.", maybe: 'No maybes.', all: 'Nothing planned yet. Post the first event.' }[filter]
  return (
    <div className="screen">
      <div className="topbar">
        <button className="icon-btn" aria-label="Back" onClick={() => back(channel ? 'c/' + encodeURIComponent(channel) : 'c')}>‹</button>
        <h1>Events{g && <small># {g.name}</small>}</h1>
        <button className="icon-btn" aria-label="New event" onClick={() => setCreating(true)}>＋</button>
      </div>
      <Tabs tab={filter} setTab={setFilter} tabs={[['going', 'Going'], ['maybe', 'Maybe'], ['all', 'All']]} />
      <div className="scroll">
        {soon.length === 0 && <div className="empty">{empty}</div>}
        <EventRows list={soon} />
        {past.length > 0 && <><div className="section-title">Past</div><EventRows list={past} days={false} /></>}
        <div style={{ padding: 16 }}><button className="btn ghost" onClick={() => setCreating(true)}>＋ Post an event</button></div>
      </div>
      {creating && <NewEventSheet channel={channel} onClose={() => setCreating(false)} />}
    </div>
  )
}

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
  const mine = e.rsvps.get(me())
  return (
    <button className="ev" onClick={() => go('cal/' + encodeURIComponent(e.coord))} style={{ opacity: e.ev.pending ? 0.6 : 1 }}>
      <DateChip d={e.start} />
      <div className="info">
        <b>{e.title}</b>
        <small>{when(e)}{e.location ? ' · ' + e.location : ''}{e.links.length ? ' · 🎥 online' : ''}</small>
        <small>{going(e)} going{mine ? ` · you: ${LABEL[mine]}` : ''}</small>
      </div>
    </button>
  )
}

const DateChip = ({ d }: { d: Date }) => (
  <div className="date"><small>{d.toLocaleDateString([], { month: 'short' })}</small><b>{d.getDate()}</b></div>
)

/** Under a channel's name: its next event, and a link to the others. Nothing when none. */
export function EventsBar({ channel }: { channel: string }) {
  useVersion()
  const list = upcoming(channel)
  if (!list.length) return null
  const e = list[0]
  return (
    <div className="events-bar">
      <button className="next" onClick={() => go('cal/' + encodeURIComponent(e.coord))}>
        <span aria-hidden="true">📅</span>
        <span className="t"><b>{e.title}</b><small>{fmtDay(e.start)} · {when(e)}</small></span>
      </button>
      <button className="more" onClick={() => go(`c/${encodeURIComponent(channel)}/events`)}>
        {list.length > 1 ? `+${list.length - 1} more` : 'All events'}
      </button>
    </div>
  )
}

/** The post an event leaves in its channel: a card that opens the event. */
export function EventCard({ coord, fallback }: { coord: string; fallback: string }) {
  const e = calendar().find((x) => x.coord === coord)
  if (!e) return <div className="text">{fallback}</div>
  const mine = e.rsvps.get(me())
  return (
    <button className="ev-card" onClick={() => go('cal/' + encodeURIComponent(e.coord))}>
      <DateChip d={e.start} />
      <span className="info">
        <b>{e.title}</b>
        <small>{fmtDay(e.start)} · {when(e)}{e.location ? ' · ' + e.location : ''}</small>
        <small>{going(e)} going{mine ? ` · you: ${LABEL[mine]}` : ''}</small>
      </span>
    </button>
  )
}

const LABEL: Record<Rsvp, string> = { accepted: 'Going', tentative: 'Maybe', declined: "Can't go" }

function localInput(d: Date) {
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`
}

export function NewEventSheet({ channel, onClose }: { channel?: string; onClose: () => void }) {
  const start = new Date()
  start.setHours(start.getHours() + 1, 0, 0, 0)
  const end = new Date(+start + 3600_000)
  const [n, setN] = useState<NewEvent>({ title: '', description: '', location: THEME === 'belfry' ? '' : 'Commons Hub Brussels', allDay: false, start: localInput(start), end: localInput(end) })
  const set = (patch: Partial<NewEvent>) => setN({ ...n, ...patch })
  // places already used for events in this channel (most recent first), then elsewhere
  const where = channel ?? 'general'
  const byRecent = [...calendar()].sort((a, b) => b.ev.created_at - a.ev.created_at)
  const places = [...new Set([
    ...byRecent.filter((e) => e.channel === where).map((e) => e.location),
    ...byRecent.map((e) => e.location),
  ].filter((l): l is string => !!l?.trim()))].slice(0, 8)
  return (
    <Sheet title="Post an event" onClose={onClose}>
      <form onSubmit={async (e) => {
        e.preventDefault()
        const coord = await createEvent(n, channel ?? 'general')
        onClose()
        go('cal/' + encodeURIComponent(coord))
      }}>
        <label className="field"><span>Title</span>
          <input className="input" autoFocus placeholder="e.g. Nix workshop" value={n.title} onChange={(e) => set({ title: e.target.value })} /></label>
        <label className="toggle"><input type="checkbox" checked={n.allDay} onChange={(e) => set({ allDay: e.target.checked })} /> All day</label>
        <label className="field"><span>Starts</span>
          <input className="input" type={n.allDay ? 'date' : 'datetime-local'} value={n.allDay ? n.start.slice(0, 10) : n.start} onChange={(e) => set({ start: e.target.value })} /></label>
        <label className="field"><span>Ends</span>
          <input className="input" type={n.allDay ? 'date' : 'datetime-local'} value={n.allDay ? n.end.slice(0, 10) : n.end} onChange={(e) => set({ end: e.target.value })} /></label>
        <label className="field"><span>Where</span>
          <input className="input" placeholder="A place, or leave empty if online only" value={n.location} onChange={(e) => set({ location: e.target.value })} /></label>
        {places.length > 0 && (
          <div className="chips" style={{ margin: '-6px 0 14px' }}>
            {places.map((pl) => <button type="button" key={pl} className={'chip' + (n.location === pl ? ' on' : '')} onClick={() => set({ location: n.location === pl ? '' : pl })}>📍 {pl}</button>)}
          </div>
        )}
        <label className="field"><span>Online</span>
          <input className="input" type="url" inputMode="url" placeholder="Paste a Google Meet, Zoom or other link" value={n.online ?? ''} onChange={(e) => set({ online: e.target.value })} /></label>
        <div className="chips" style={{ margin: '-6px 0 14px' }}>
          <button type="button" className="chip" onClick={() => set({ online: jitsiLink(n.title || 'meeting') })}>🎥 New Jitsi link</button>
          <a className="chip" href="https://meet.google.com/new" target="_blank" rel="noreferrer">🎥 New Google Meet ↗</a>
        </div>
        {n.online && !/^https:\/\//.test(n.online.trim()) && <p className="error" style={{ marginTop: -8 }}>The link should start with https://</p>}
        <label className="field"><span>Description</span>
          <textarea className="input" rows={3} placeholder="Markdown: **bold**, *italic*, - lists, [links](https://…)" value={n.description} onChange={(e) => set({ description: e.target.value })} /></label>
        <button className="btn" disabled={!n.title.trim() || !n.start || (!!n.online?.trim() && !/^https:\/\//.test(n.online.trim()))}>Post event</button>
        <p className="hint" style={{ marginTop: 10 }}>Posted in #{groups().find((g) => g.id === (channel ?? 'general'))?.name ?? 'general'}. The event gets its own chat, task list and files.</p>
      </form>
    </Sheet>
  )
}

type EvTab = 'chat' | 'people' | 'tasks' | 'files'

export function EventPage({ coord }: { coord: string }) {
  useVersion()
  const [tab, setTab] = useState<EvTab>('chat')
  const [topic, setTopic] = useState<string | undefined>()
  const [open, setOpen] = useState<boolean | null>(null) // null: details shown until you answer
  const e = calendar().find((x) => x.coord === coord)
  if (!e) return (
    <div className="screen">
      <div className="topbar"><button className="icon-btn" aria-label="Back" onClick={() => back('c')}>‹</button><h1>Event</h1></div>
      <div className="empty">This event is not here (yet). It may still be syncing.</div>
    </div>
  )
  const mine = e.rsvps.get(me())
  return (
    <div className="screen">
      <div className="topbar">
        <button className="icon-btn" aria-label="Back" onClick={() => back('c/' + encodeURIComponent(e.channel))}>‹</button>
        <h1>{e.title}<small>{fmtDay(e.start)} · {when(e)}</small></h1>
        {groups().some((g) => g.id === e.groupId) && <FollowButton channel={e.groupId} />}
      </div>
      <div style={{ padding: '10px 12px 0' }}>
        {mine && !(open ?? false) ? (
          <button className="card ev-status" style={{ marginBottom: 8 }} aria-expanded={false} onClick={() => setOpen(true)}>
            <span><b>{mine === 'accepted' ? '✓ ' : ''}{LABEL[mine]}</b> · {going(e)} going</span>
            <span className="hint">Details ▾</span>
          </button>
        ) : (
          <div className="card" style={{ marginBottom: 8 }}>
            {mine && <button className="ev-status" aria-expanded onClick={() => setOpen(false)}>
              <span><b>{mine === 'accepted' ? '✓ ' : ''}{LABEL[mine]}</b> · {going(e)} going</span>
              <span className="hint">Hide ▴</span>
            </button>}
            {e.location && <div style={{ marginTop: mine ? 8 : 0 }}>📍 {e.location}</div>}
            {e.links.map((l) => (
              <a key={l} className="btn small" style={{ display: 'inline-flex', marginTop: 8, marginRight: 8, textDecoration: 'none' }} href={l} target="_blank" rel="noreferrer">🎥 Join on {meetingLabel(l)}</a>
            ))}
            {e.description && <Markdown className="hint" text={e.description} style={{ marginTop: 6, maxHeight: 160, overflowY: 'auto' }} />}
            <div className="hint" style={{ marginTop: 6, fontSize: 13 }}>Posted by {displayName(e.ev.pubkey)}{mine ? '' : ` · ${going(e)} going`}</div>
            <div className="rsvp" style={{ marginTop: 10 }}>
              {(['accepted', 'tentative', 'declined'] as Rsvp[]).map((s) => (
                <button key={s} className={mine === s ? 'on' : ''} onClick={() => { rsvp(e, s); setOpen(null) }}>{LABEL[s]}</button>
              ))}
            </div>
          </div>
        )}
      </div>
      <Tabs tab={tab} setTab={setTab} tabs={[['chat', 'Chat'], ['people', 'People'], ['tasks', 'Tasks'], ['files', 'Files']]} />
      {tab === 'chat' && (groups().some((g) => g.id === e.groupId) ? <><TopicsBar groupId={e.groupId} active={topic} onOpen={setTopic} /><Conversation id={e.groupId} topic={topic} onTopic={setTopic} /></> : (
        <div className="scroll"><div className="empty">
          <p>This session has no chat yet.</p>
          <button className="btn small" onClick={() => openEventChat(e)}>Open a chat for it</button>
        </div></div>
      ))}
      {tab !== 'chat' && (
        <div className="scroll">
          {tab === 'people' && <People e={e} />}
          {tab === 'tasks' && <Tasks groupId={e.groupId} />}
          {tab === 'files' && <Files groupId={e.groupId} />}
        </div>
      )}
    </div>
  )
}

function People({ e }: { e: CalEvent }) {
  const rows = [...e.rsvps.entries()].sort((a, b) => (a[1] === 'accepted' ? -1 : 1) - (b[1] === 'accepted' ? -1 : 1))
  return (
    <div>
      {rows.length === 0 && <div className="empty">No answers yet.</div>}
      {rows.map(([p, s]) => (
        <div key={p} className="member">
          <Avatar pubkey={p} />
          <div style={{ flex: 1 }}>{displayName(p)}</div>
          <span className={'badge' + (s === 'accepted' ? ' lime' : '')}>{LABEL[s]}</span>
        </div>
      ))}
      {e.ev.pubkey === me() && (
        <div className="pad">
          <button className="btn ghost" onClick={async () => { if (confirm('Delete this event?')) { await deleteEvent(e); go('c/' + encodeURIComponent(e.channel)) } }}>Delete event</button>
        </div>
      )}
    </div>
  )
}
