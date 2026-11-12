// A new meeting in a few taps (Xavier, 2026-11-12): the composer's ＋ → Meeting. Topic, people
// (faces of the channel's most active members first, then a search by name), when, how long,
// where (the last place by default). Members only. core/meetings.ts says how it is stored.
import { useState } from 'react'
import { jitsiLink } from '../core/actions'
import { calendar, displayName } from '../core/model'
import { useVersion } from '../core/store'
import { createMeeting, findPeople, lastPlace, LENGTHS, type MeetingForm, type Named } from '../core/meetings'
import { Avatar, Form, Sheet, useFormDraft } from './common'

const pad = (n: number) => String(n).padStart(2, '0')
const localInput = (d: Date) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`
export const lengthLabel = (m: number) => (m < 60 ? `${m} min` : `${Math.floor(m / 60)} h${m % 60 ? ' ' + (m % 60) : ''}`)

const EMAIL = /^\S+@\S+\.\S+$/

/** Who comes (Xavier, 2026-11-12: "just select / unselect people by tapping on their avatar and
 *  add an extra + to add someone"): one grid of faces (the channel's most active members first,
 *  then its parent's; the ones picked are ticked), then +. The + asks for a name or an e-mail,
 *  shows the people here who match it, and then asks for whichever of the two is missing
 *  (optional); someone not here gets an account (core/meetings.ts). */
function PeoplePicker({ channel, people, named, onChange }: { channel: string; people: string[]; named: Named[]; onChange: (people: string[], named: Named[]) => void }) {
  const [step, setStep] = useState<'' | 'who' | 'more'>('')
  const [who, setWho] = useState('')
  const [more, setMore] = useState('')
  const faces = [...new Set([...people, ...findPeople(channel, '', 12)])]
  const matches = who.trim() && !EMAIL.test(who.trim()) ? findPeople(channel, who, 6).filter((k) => !people.includes(k)) : []
  const toggle = (k: string) => onChange(people.includes(k) ? people.filter((x) => x !== k) : [...people, k], named)
  const isEmail = EMAIL.test(who.trim())
  const reset = () => { setStep(''); setWho(''); setMore('') }
  const add = () => {
    const a = who.trim(), b = more.trim()
    const n: Named = isEmail ? { name: b || a.split('@')[0], email: a } : { name: a, ...(EMAIL.test(b) ? { email: b } : {}) }
    onChange(people, [...named, n])
    reset()
  }
  return (
    <div className="meet-people-pick">
      <div className="meet-faces" role="group" aria-label="People">
        {faces.map((k) => (
          <button type="button" key={k} className={'meet-face' + (people.includes(k) ? ' on' : '')} aria-pressed={people.includes(k)} onClick={() => toggle(k)} aria-label={displayName(k)}>
            <span className="ring"><Avatar pubkey={k} size={44} />{people.includes(k) && <b className="tick" aria-hidden="true">✓</b>}</span><small>{displayName(k)}</small>
          </button>
        ))}
        {named.map((n, i) => (
          <button type="button" key={'n' + i} className="meet-face on" aria-pressed="true" onClick={() => onChange(people, named.filter((_, j) => j !== i))} aria-label={`${n.name}: remove`}>
            <span className="ring"><span className="meet-initial big" aria-hidden="true">{n.name.trim()[0]?.toUpperCase()}</span><b className="tick" aria-hidden="true">✓</b></span><small>{n.name}</small>
          </button>
        ))}
        {!step && (
          <button type="button" className="meet-face add" onClick={() => setStep('who')} aria-label="Add someone">
            <span className="ring"><span className="meet-plus" aria-hidden="true">+</span></span><small>Add</small>
          </button>
        )}
      </div>
      {step === 'who' && (
        <div className="meet-add-step">
          <div className="row">
            <input className="input" autoFocus placeholder="Name or e-mail" value={who} maxLength={254} aria-label="Name or e-mail"
              onChange={(e) => setWho(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); if (who.trim()) setStep('more') } if (e.key === 'Escape') { e.preventDefault(); reset() } }} />
            <button type="button" className="btn small" disabled={!who.trim()} onClick={() => setStep('more')}>OK</button>
            <button type="button" className="btn small ghost" onClick={reset} aria-label="Cancel">✕</button>
          </div>
          {matches.length > 0 && (
            <div className="meet-matches">
              <span className="hint">Already here:</span>
              {matches.map((k) => (
                <button type="button" key={k} className="meet-person" onClick={() => { toggle(k); reset() }}><Avatar pubkey={k} size={24} /><span>{displayName(k)}</span></button>
              ))}
            </div>
          )}
        </div>
      )}
      {step === 'more' && (
        <div className="meet-add-step">
          <p className="hint">{isEmail ? <>Adding <b>{who.trim()}</b>. Their name?</> : <>Adding <b>{who.trim()}</b>, who isn't here yet: an account is made for them. Their e-mail?</>}</p>
          <div className="row">
            <input className="input" autoFocus type={isEmail ? 'text' : 'email'} placeholder={isEmail ? 'Name (optional)' : 'E-mail (optional)'} value={more} maxLength={isEmail ? 100 : 254}
              aria-label={isEmail ? 'Their name (optional)' : 'Their e-mail (optional)'}
              onChange={(e) => setMore(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); if (isEmail || !more.trim() || EMAIL.test(more.trim())) add() } }} />
            <button type="button" className="btn small" disabled={!isEmail && !!more.trim() && !EMAIL.test(more.trim())} onClick={add}>Add</button>
          </div>
        </div>
      )}
    </div>
  )
}

export function MeetingSheet({ channel, onClose, onCreated }: { channel: string; onClose: () => void; onCreated?: (d: string) => void }) {
  useVersion()
  const [m, setM, clearDraft] = useFormDraft<MeetingForm>('meeting:' + channel, () => {
    const s = new Date()
    s.setHours(s.getHours() + 1, 0, 0, 0)
    return { topic: '', people: [], start: localInput(s), minutes: 60, place: lastPlace(), online: '' }
  })
  const set = (patch: Partial<MeetingForm>) => setM((x) => ({ ...x, ...patch }))
  const [error, setError] = useState('')
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
        <PeoplePicker channel={channel} people={m.people} named={m.named ?? []} onChange={(people, named) => set({ people, named })} />

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
        <p className="hint" style={{ marginTop: 10 }}>Members only. {m.people.length ? `${m.people.length === 1 ? displayName(m.people[0]) + ' sees' : m.people.length + ' people see'} it in their upcoming events and get its chat.` : 'The people you invite see it in their upcoming events and get its chat.'}{(m.named ?? []).length ? ` ${(m.named ?? []).length === 1 ? (m.named ?? [])[0].name + ' gets' : (m.named ?? []).length + ' people get'} an account; without an e-mail, it stays yours to hand over.` : ''}</p>
      </Form>
    </Sheet>
  )
}
