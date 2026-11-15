// Tickets and checking people in (docs/checkin.md, Xavier 2026-11-15).
//   TicketScreen   "Show my ticket": the QR full screen on white, my name, the ticket, the event.
//   CheckInScreen  for the host, co-hosts and stewards: the camera on top (a scan checks in and
//                  shows a card for 2 s; scanning goes on), then "12 of 40 checked in", a search
//                  and the list (tap → check in; a checked one asks before undoing).
//   TicketCard     a ticket's QR opened by someone who checks in: the result, on the event.
import { useEffect, useMemo, useRef, useState } from 'react'
import qrcode from 'qrcode-generator'
import { displayName, profiles, type CalEvent } from '../core/model'
import { attendees, checkedLive, checkIn, CheckFailed, CHECK_ERRORS, codeFromScan, type Attendee, type CheckError } from '../core/checkin'
import { useVersion } from '../core/store'
import { me } from '../core/sync'
import { Avatar, Btn } from './common'
import { CloseIcon } from './icons'
import { when } from './Calendar'

const hhmm = (ts: number) => new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
const errorOf = (x: unknown): CheckError => (x instanceof CheckFailed ? x.code : 'failed')

/** Keep the screen on while a ticket or the scanner shows (where the browser allows it). */
function useWakeLock() {
  useEffect(() => {
    let lock: { release: () => Promise<void> } | undefined
    const nav = navigator as Navigator & { wakeLock?: { request: (t: 'screen') => Promise<{ release: () => Promise<void> }> } }
    nav.wakeLock?.request('screen').then((l) => { lock = l }, () => { /* not allowed: fine */ })
    return () => { void lock?.release().catch(() => undefined) }
  }, [])
}

export function TicketScreen({ e, url, ticketTitle, checkedIn, onClose }: { e: CalEvent; url: string; ticketTitle: string; checkedIn?: number | null; onClose: () => void }) {
  useWakeLock()
  useEffect(() => {
    const k = (x: KeyboardEvent) => { if (x.key === 'Escape') onClose() }
    addEventListener('keydown', k)
    return () => removeEventListener('keydown', k)
  }, [onClose])
  const svg = useMemo(() => { const q = qrcode(0, 'M'); q.addData(url); q.make(); return 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(q.createSvgTag({ cellSize: 8, margin: 2, scalable: true })) }, [url])
  const name = profiles().get(me())?.name || '' // never "You" on a ticket: the door needs the name
  return (
    <div className="ticket-screen" role="dialog" aria-modal="true" aria-label="My ticket">
      <button className="icon-btn ticket-close" aria-label="Close" onClick={onClose}><CloseIcon /></button>
      <div className="ticket-card">
        <img className="ticket-qr" src={svg} alt="The ticket's QR code" />
        {name && <div className="ticket-name">{name}</div>}
        {ticketTitle && <div className="ticket-type">{ticketTitle}</div>}
        <div className="ticket-event"><b>{e.title}</b><span>{when(e)}</span></div>
        {checkedIn ? <div className="ticket-in">✓ Checked in at {hhmm(checkedIn)}</div> : <p className="ticket-hint">Show this at the door.</p>}
      </div>
    </div>
  )
}

type Result = { kind: 'ok' | 'already' | 'bad'; text: string; at: number }
function resultOf(r: { person: Attendee; already: boolean }): Result {
  const name = r.person.name || displayName(r.person.pubkey)
  if (r.already) return { kind: 'already', text: r.person.checked_in ? `${name}: already checked in at ${hhmm(r.person.checked_in)}` : `${name}: already checked in`, at: Date.now() }
  return { kind: 'ok', text: [name, r.person.ticket_title, 'checked in'].filter(Boolean).join(' · '), at: Date.now() }
}
const badResult = (code: CheckError): Result => ({ kind: 'bad', text: code === 'no_ticket' ? CHECK_ERRORS.no_ticket : CHECK_ERRORS[code], at: Date.now() })

function ResultCard({ r }: { r: Result }) {
  return <div className={'scan-result ' + r.kind} role="status">{r.kind === 'ok' ? '✓ ' : r.kind === 'already' ? '• ' : '✕ '}{r.text}</div>
}

/** A ticket's QR opened by someone who may check in: who it is, their ticket and whether they are
 *  in, and a "Check in" button (a link can be opened by accident or by a link preview: nothing
 *  happens by itself). Then the green state, with Undo. */
export function TicketCard({ e, code, holders, titles, onDone }: { e: CalEvent; code: string; holders: Map<string, string>; titles: Map<string, string>; onDone: () => void }) {
  const [person, setPerson] = useState<Attendee | null>(null)
  const [state, setState] = useState<'looking' | 'out' | 'in' | 'just' | 'bad'>('looking')
  const [error, setError] = useState<CheckError | ''>('')
  useEffect(() => {
    let gone = false
    checkIn(e, { code }, false, holders, true).then(
      (x) => { if (!gone) { setPerson(x.person); setState(x.person.checked_in ? 'in' : 'out') } },
      (x) => { if (!gone) { setError(errorOf(x)); setState('bad') } })
    return () => { gone = true }
  }, [code])
  const name = person ? person.name || displayName(person.pubkey) : ''
  const ticket = person ? person.ticket_title || titles.get(person.ticket) || '' : ''
  const kind = state === 'just' ? 'ok' : state === 'in' ? 'already' : state === 'bad' ? 'bad' : ''
  const line = state === 'looking' ? 'Checking the ticket…'
    : state === 'bad' ? (error === 'no_ticket' ? CHECK_ERRORS.no_ticket : CHECK_ERRORS[error || 'failed'])
    : [name, ticket, state === 'just' ? 'checked in' : state === 'in' ? (person?.checked_in ? `already checked in at ${hhmm(person.checked_in)}` : 'already checked in') : 'not checked in yet'].filter(Boolean).join(' · ')
  return (
    <div className={'ticket-check card ' + kind} role="status">
      {person && <Avatar pubkey={person.pubkey} size={32} />}
      {person && state !== 'bad'
        ? <div className="t two"><b>{name}</b><small>{[ticket, line.split(' · ').at(-1)].filter(Boolean).join(' · ')}</small></div>
        : <div className="t">{line}</div>}
      <div className="row" style={{ gap: 8 }}>
        {person && state === 'out' && <Btn className="btn small" onClick={async () => {
          try { const x = await checkIn(e, { pubkey: person.pubkey }, false, holders); setPerson({ ...person, checked_in: x.person.checked_in }); setState(x.already ? 'in' : 'just') } catch (y) { setError(errorOf(y)); setState('bad') }
        }}>Check in</Btn>}
        {person && state === 'just' && <Btn className="btn small ghost" onClick={async () => {
          try { await checkIn(e, { pubkey: person.pubkey }, true, holders); setPerson({ ...person, checked_in: null }); setState('out') } catch (y) { setError(errorOf(y)); setState('bad') }
        }}>Undo</Btn>}
        <button className={'btn small' + (state === 'out' ? ' ghost' : '')} onClick={onDone}>{state === 'out' ? 'Not now' : 'OK'}</button>
      </div>
    </div>
  )
}

export function CheckInScreen({ e, holders, titles, onClose }: { e: CalEvent; holders: Map<string, string>; titles: Map<string, string>; onClose: () => void }) {
  useVersion()
  useWakeLock()
  const [list, setList] = useState<Attendee[] | null>(null)
  const [loadError, setLoadError] = useState<CheckError | ''>('')
  const [local, setLocal] = useState<Map<string, number | null>>(new Map()) // answers from my own check-ins
  const [q, setQ] = useState('')
  const [result, setResult] = useState<Result | null>(null)
  const [confirm, setConfirm] = useState('')
  const [camera, setCamera] = useState<'starting' | 'on' | 'off'>('starting')
  const video = useRef<HTMLVideoElement>(null)
  const last = useRef<{ code: string; at: number }>({ code: '', at: 0 })

  const load = () => attendees(e, holders, titles).then((x) => { setList(x.people); setLoadError('') }, (x) => setLoadError(errorOf(x)))
  useEffect(() => { void load() }, [e.d])
  useEffect(() => {
    const k = (x: KeyboardEvent) => { if (x.key === 'Escape') onClose() }
    addEventListener('keydown', k)
    return () => removeEventListener('keydown', k)
  }, [onClose])
  useEffect(() => { if (!result) return; const t = setTimeout(() => setResult(null), 2000); return () => clearTimeout(t) }, [result])

  const took = (x: { person: Attendee; already: boolean }) => {
    setLocal((m) => new Map(m).set(x.person.pubkey, x.person.checked_in))
    setList((l) => (l && !l.some((p) => p.pubkey === x.person.pubkey) ? [...l, x.person] : l))
  }
  // the camera: every 250 ms a frame is read; a ticket's URL checks in at once
  useEffect(() => {
    let stream: MediaStream | undefined
    let timer: ReturnType<typeof setTimeout> | undefined
    let stopped = false
    void (async () => {
      try {
        if (!navigator.mediaDevices?.getUserMedia) throw new Error('no camera')
        stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: 'environment' }, audio: false })
        if (stopped) return
        const v = video.current
        if (!v) return
        v.srcObject = stream
        await v.play().catch(() => undefined)
        setCamera('on')
        const { qrReader } = await import('../core/scan')
        const read = await qrReader(v)
        const tick = async () => {
          if (stopped) return
          const text = await read()
          const code = text ? codeFromScan(text) : undefined
          const now = Date.now()
          if (text && !(code === last.current.code && now - last.current.at < 3000) && !(text === last.current.code && now - last.current.at < 3000)) {
            last.current = { code: code ?? text, at: now }
            try { navigator.vibrate?.(60) } catch { /* no vibration */ }
            if (!code) setResult(badResult('no_ticket'))
            else {
              try { const x = await checkIn(e, { code }, false, holders); took(x); setResult(resultOf(x)) } catch (y) { setResult(badResult(errorOf(y))) }
            }
          }
          timer = setTimeout(tick, 250)
        }
        void tick()
      } catch { if (!stopped) setCamera('off') }
    })()
    return () => { stopped = true; clearTimeout(timer); stream?.getTracks().forEach((t) => t.stop()) }
  }, [e.d])

  const stateOf = (p: Attendee): number | null => {
    const live = checkedLive(e.groupId, p.pubkey)
    if (local.has(p.pubkey)) {
      const mine = local.get(p.pubkey) ?? null
      return live !== undefined && (live ?? 0) > (mine ?? 0) ? live : mine
    }
    return live !== undefined ? live : p.checked_in
  }
  const shown = (list ?? []).filter((p) => !q.trim() || (p.name || displayName(p.pubkey)).toLowerCase().includes(q.trim().toLowerCase()))
  const total = list?.length ?? 0
  const inside = (list ?? []).filter((p) => stateOf(p)).length
  return (
    <div className="checkin-screen" role="dialog" aria-modal="true" aria-label="Check people in">
      <div className="topbar">
        <button className="icon-btn" aria-label="Close" onClick={onClose}>‹</button>
        <h1>Check people in<small>{e.title}</small></h1>
      </div>
      <div className={'scan-box ' + camera}>
        <video ref={video} muted playsInline aria-label="Camera" />
        {camera === 'starting' && <p className="scan-note">Starting the camera…</p>}
        {camera === 'off' && <p className="scan-note">The camera isn’t available. Pick people from the list.</p>}
        {camera === 'on' && !result && <span className="scan-frame" aria-hidden="true" />}
        {result && <ResultCard r={result} />}
      </div>
      <div className="checkin-count"><b>{inside} of {total}</b> checked in</div>
      <div className="pad-x"><input className="input" type="search" placeholder="Search people" aria-label="Search people" value={q} onChange={(x) => setQ(x.target.value)} /></div>
      <div className="scroll checkin-list">
        {!list && <div className="empty">{loadError ? CHECK_ERRORS[loadError] : 'Loading…'}</div>}
        {list && !total && <div className="empty">Nobody has a ticket yet.</div>}
        {shown.map((p) => {
          const at = stateOf(p)
          const name = p.name || displayName(p.pubkey)
          if (confirm === p.pubkey)
            return (
              <div key={p.pubkey} className="checkin-row confirm">
                <span className="t"><b>Undo check-in?</b><small>{name}</small></span>
                <Btn className="btn small danger" onClick={async () => { try { took(await checkIn(e, { pubkey: p.pubkey }, true, holders)); setConfirm('') } catch (y) { setResult(badResult(errorOf(y))); setConfirm('') } }}>Undo</Btn>
                <button className="btn small ghost" onClick={() => setConfirm('')}>Keep</button>
              </div>
            )
          return (
            <Btn key={p.pubkey} className={'checkin-row' + (at ? ' in' : '')} aria-pressed={!!at}
              onClick={async () => {
                if (at) { setConfirm(p.pubkey); return }
                try { const x = await checkIn(e, { pubkey: p.pubkey }, false, holders); took(x); setResult(resultOf(x)) } catch (y) { setResult(badResult(errorOf(y))) }
              }}>
              <Avatar pubkey={p.pubkey} size={36} />
              <span className="t"><b>{name}</b><small>{[p.ticket_title, p.rsvp === 'tentative' ? 'Maybe' : p.rsvp === 'declined' ? 'Can’t go' : '', at ? `in at ${hhmm(at)}` : ''].filter(Boolean).join(' · ')}</small></span>
              <span className={'check-mark' + (at ? ' on' : '')} aria-hidden="true">{at ? '✓' : ''}</span>
            </Btn>
          )
        })}
      </div>
    </div>
  )
}
