// A new meeting in a few taps (Xavier, 2026-11-12): the composer's ＋ → Meeting. Topic, people
// (faces of the channel's most active members first, then a search by name), when, how long,
// where (the last place by default). Members only. core/meetings.ts says how it is stored.
import { useState } from 'react'
import { jitsiLink } from '../core/actions'
import { calendar, displayName } from '../core/model'
import { useVersion } from '../core/store'
import { createMeeting, findPeople, lastPlace, LENGTHS, type MeetingForm } from '../core/meetings'
import { Avatar, Form, Sheet, useFormDraft } from './common'

const pad = (n: number) => String(n).padStart(2, '0')
const localInput = (d: Date) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`
export const lengthLabel = (m: number) => (m < 60 ? `${m} min` : `${Math.floor(m / 60)} h${m % 60 ? ' ' + (m % 60) : ''}`)

export function MeetingSheet({ channel, onClose, onCreated }: { channel: string; onClose: () => void; onCreated?: (d: string) => void }) {
  useVersion()
  const [m, setM, clearDraft] = useFormDraft<MeetingForm>('meeting:' + channel, () => {
    const s = new Date()
    s.setHours(s.getHours() + 1, 0, 0, 0)
    return { topic: '', people: [], start: localInput(s), minutes: 60, place: lastPlace(), online: '' }
  })
  const set = (patch: Partial<MeetingForm>) => setM((x) => ({ ...x, ...patch }))
  const [q, setQ] = useState('')
  const [error, setError] = useState('')
  const found = findPeople(channel, q, q ? 8 : 12).filter((k) => !m.people.includes(k))
  const toggle = (k: string) => set({ people: m.people.includes(k) ? m.people.filter((x) => x !== k) : [...m.people, k] })
  const places = [...new Set(calendar().filter((e) => e.meeting).sort((a, b) => b.ev.created_at - a.ev.created_at).map((e) => e.location).filter((l): l is string => !!l?.trim()))].slice(0, 5)
  const ok = !!m.topic.trim() && !!m.start
  return (
    <Sheet title="New meeting" onClose={onClose}>
      <Form className="meeting-form" onSubmit={async (e) => {
        e.preventDefault()
        if (!ok) return
        setError('')
        try {
          const d = await createMeeting(channel, m)
          clearDraft()
          onClose()
          onCreated?.(d)
        } catch (x) { setError(x instanceof Error ? x.message : String(x)) }
      }}>
        <label className="field"><span>Topic</span>
          <input className="input" autoFocus placeholder="e.g. Plan the garden day" value={m.topic} maxLength={200} onChange={(e) => set({ topic: e.target.value })} /></label>

        <div className="field"><span>People</span></div>
        {m.people.length > 0 && (
          <div className="meet-picked">
            {m.people.map((k) => (
              <button type="button" key={k} className="meet-person on" onClick={() => toggle(k)} aria-label={`Remove ${displayName(k)}`}>
                <Avatar pubkey={k} size={24} /><span>{displayName(k)}</span><b aria-hidden="true">×</b>
              </button>
            ))}
          </div>
        )}
        <input className="input meet-search" placeholder="Add people: type a name" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Find people" />
        <div className="meet-faces" role="list">
          {found.map((k) => (
            <button type="button" key={k} role="listitem" className="meet-face" onClick={() => { toggle(k); setQ('') }} aria-label={`Invite ${displayName(k)}`}>
              <Avatar pubkey={k} size={40} /><small>{displayName(k)}</small>
            </button>
          ))}
          {!found.length && <p className="hint">{q ? 'Nobody by that name.' : 'Nobody else here yet.'}</p>}
        </div>

        <label className="field"><span>When</span>
          <input className="input" type="datetime-local" value={m.start} onChange={(e) => set({ start: e.target.value })} /></label>
        <div className="field"><span>How long</span></div>
        <div className="chips meet-lengths">
          {LENGTHS.map((n) => <button type="button" key={n} className={'chip' + (m.minutes === n ? ' on' : '')} aria-pressed={m.minutes === n} onClick={() => set({ minutes: n })}>{lengthLabel(n)}</button>)}
        </div>

        <label className="field"><span>Where</span>
          <input className="input" placeholder="A room, an address" value={m.place} onChange={(e) => set({ place: e.target.value })} /></label>
        <div className="chips" style={{ margin: '-6px 0 14px' }}>
          {places.filter((p) => p !== m.place).map((p) => <button type="button" key={p} className="chip" onClick={() => set({ place: p })}>📍 {p}</button>)}
          <button type="button" className={'chip' + (m.online ? ' on' : '')} onClick={() => set({ online: m.online ? '' : jitsiLink(m.topic || 'meeting') })}>🎥 {m.online ? 'Video link added' : 'Add a video link'}</button>
        </div>

        {error && <p className="error" role="alert">{error}</p>}
        <button className="btn" disabled={!ok}>Create the meeting</button>
        <p className="hint" style={{ marginTop: 10 }}>Members only. {m.people.length ? `${m.people.length === 1 ? displayName(m.people[0]) + ' sees' : m.people.length + ' people see'} it in their upcoming events and get its chat.` : 'The people you invite see it in their upcoming events and get its chat.'}</p>
      </Form>
    </Sheet>
  )
}
