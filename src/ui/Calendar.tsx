import { useState } from 'react'
import { createEvent, deleteEvent, openEventChat, rsvp, type NewEvent } from '../core/actions'
import { calendar, displayName, groups, type CalEvent, type Rsvp } from '../core/model'
import { useVersion } from '../core/store'
import { me } from '../core/sync'
import { Avatar, back, fmtDay, go, Sheet, StatusPill } from './common'
import { Conversation } from './Chat'
import { Files, Tabs, Tasks } from './ChannelInfo'

const when = (e: CalEvent) => {
  if (e.allDay) return 'All day'
  const t = (d: Date) => d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  return e.end ? `${t(e.start)} – ${t(e.end)}` : t(e.start)
}

const going = (e: CalEvent) => [...e.rsvps.values()].filter((s) => s === 'accepted').length

export function CalendarList() {
  useVersion()
  const [creating, setCreating] = useState(false)
  const [past, setPast] = useState(false)
  const startOfToday = new Date(new Date().toDateString())
  const all = calendar()
  const list = all.filter((e) => (past ? +(e.end ?? e.start) < +startOfToday : +(e.end ?? e.start) >= +startOfToday))
  if (past) list.reverse()
  let lastDay = ''
  return (
    <div className="screen">
      <div className="topbar">
        <h1 style={{ paddingLeft: 8 }}>Calendar</h1>
        <StatusPill />
        <button className="icon-btn" aria-label="New event" onClick={() => setCreating(true)}>＋</button>
      </div>
      <Tabs tab={past ? 'past' : 'upcoming'} setTab={(t) => setPast(t === 'past')} tabs={[['upcoming', 'Upcoming'], ['past', 'Past']]} />
      <div className="scroll">
        {list.length === 0 && <div className="empty">{past ? 'No past events.' : 'Nothing planned yet. Post the first event.'}</div>}
        {list.map((e) => {
          const day = fmtDay(e.start)
          const header = day !== lastDay ? <div className="section-title">{day}</div> : null
          lastDay = day
          const mine = e.rsvps.get(me())
          return (
            <div key={e.coord}>
              {header}
              <button className="ev" onClick={() => go('cal/' + encodeURIComponent(e.coord))} style={{ opacity: e.ev.pending ? 0.6 : 1 }}>
                <div className="date"><small>{e.start.toLocaleDateString([], { month: 'short' })}</small><b>{e.start.getDate()}</b></div>
                <div className="info">
                  <b>{e.title}</b>
                  <small>{when(e)}{e.location ? ' · ' + e.location : ''}</small>
                  <small>{going(e)} going{mine ? ` · you: ${LABEL[mine]}` : ''}</small>
                </div>
              </button>
            </div>
          )
        })}
        <div style={{ padding: 16 }}><button className="btn ghost" onClick={() => setCreating(true)}>＋ Post an event</button></div>
      </div>
      {creating && <NewEventSheet onClose={() => setCreating(false)} />}
    </div>
  )
}

const LABEL: Record<Rsvp, string> = { accepted: 'Going', tentative: 'Maybe', declined: "Can't go" }

function localInput(d: Date) {
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`
}

function NewEventSheet({ onClose }: { onClose: () => void }) {
  const start = new Date()
  start.setHours(start.getHours() + 1, 0, 0, 0)
  const end = new Date(+start + 3600_000)
  const [n, setN] = useState<NewEvent>({ title: '', description: '', location: 'Commons Hub Brussels', allDay: false, start: localInput(start), end: localInput(end) })
  const set = (patch: Partial<NewEvent>) => setN({ ...n, ...patch })
  return (
    <Sheet title="Post an event" onClose={onClose}>
      <form onSubmit={async (e) => {
        e.preventDefault()
        await createEvent(n)
        onClose()
      }}>
        <label className="field"><span>Title</span>
          <input className="input" autoFocus placeholder="e.g. Nix workshop" value={n.title} onChange={(e) => set({ title: e.target.value })} /></label>
        <label className="toggle"><input type="checkbox" checked={n.allDay} onChange={(e) => set({ allDay: e.target.checked })} /> All day</label>
        <label className="field"><span>Starts</span>
          <input className="input" type={n.allDay ? 'date' : 'datetime-local'} value={n.allDay ? n.start.slice(0, 10) : n.start} onChange={(e) => set({ start: e.target.value })} /></label>
        <label className="field"><span>Ends</span>
          <input className="input" type={n.allDay ? 'date' : 'datetime-local'} value={n.allDay ? n.end.slice(0, 10) : n.end} onChange={(e) => set({ end: e.target.value })} /></label>
        <label className="field"><span>Where</span>
          <input className="input" value={n.location} onChange={(e) => set({ location: e.target.value })} /></label>
        <label className="field"><span>Description</span>
          <textarea className="input" rows={3} value={n.description} onChange={(e) => set({ description: e.target.value })} /></label>
        <button className="btn" disabled={!n.title.trim() || !n.start}>Post event</button>
        <p className="hint" style={{ marginTop: 10 }}>The event gets its own chat, task list and files.</p>
      </form>
    </Sheet>
  )
}

type EvTab = 'chat' | 'people' | 'tasks' | 'files'

export function EventPage({ coord }: { coord: string }) {
  useVersion()
  const [tab, setTab] = useState<EvTab>('chat')
  const e = calendar().find((x) => x.coord === coord)
  if (!e) return (
    <div className="screen">
      <div className="topbar"><button className="icon-btn" onClick={() => back('cal')}>‹</button><h1>Event</h1></div>
      <div className="empty">This event is not here (yet). It may still be syncing.</div>
    </div>
  )
  const mine = e.rsvps.get(me())
  return (
    <div className="screen">
      <div className="topbar">
        <button className="icon-btn" aria-label="Back" onClick={() => back('cal')}>‹</button>
        <h1>{e.title}<small>{fmtDay(e.start)} · {when(e)}</small></h1>
        <StatusPill />
      </div>
      <div style={{ padding: '10px 12px 0' }}>
        <div className="card" style={{ marginBottom: 8 }}>
          {e.location && <div>📍 {e.location}</div>}
          {e.description && <div className="hint" style={{ whiteSpace: 'pre-wrap', marginTop: 6, maxHeight: 96, overflowY: 'auto' }}>{e.description}</div>}
          <div className="hint" style={{ marginTop: 6, fontSize: 13 }}>Posted by {displayName(e.ev.pubkey)} · {going(e)} going</div>
          <div className="rsvp" style={{ marginTop: 10 }}>
            {(['accepted', 'tentative', 'declined'] as Rsvp[]).map((s) => (
              <button key={s} className={mine === s ? 'on' : ''} onClick={() => rsvp(e, s)}>{LABEL[s]}</button>
            ))}
          </div>
        </div>
      </div>
      <Tabs tab={tab} setTab={setTab} tabs={[['chat', 'Chat'], ['people', 'People'], ['tasks', 'Tasks'], ['files', 'Files']]} />
      {tab === 'chat' && (groups().some((g) => g.id === e.groupId) ? <Conversation id={e.groupId} /> : (
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
          <button className="btn ghost" onClick={async () => { if (confirm('Delete this event?')) { await deleteEvent(e); go('cal') } }}>Delete event</button>
        </div>
      )}
    </div>
  )
}
