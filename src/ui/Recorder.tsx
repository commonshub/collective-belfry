// ＋ → Audio recording (Xavier, 2026-11-12): record on the device with pause and resume and the
// length so far; at the end, which event or meeting it was (not asked in an event's own chat),
// a new meeting made on the spot if needed. Saved on the device as it records (core/recorder.ts)
// and only forgotten once the outbox holds it; an unsent one is offered back in the chat.
import { useEffect, useState } from 'react'
import { postMessage } from '../core/actions'
import { calendar, group, type CalEvent } from '../core/model'
import { clock, current, discard, elapsed, filesOf, level, pause, recover, resume, start, stop, subscribe, unsent, type RecMeta } from '../core/recorder'
import { Btn, Sheet, fmtDay } from './common'
import { MeetingSheet } from './Meeting'
import { when } from './Calendar'

/** Re-render on the recorder's news and every quarter second while one runs. */
export function useRecorder() {
  const [, set] = useState(0)
  useEffect(() => {
    const off = subscribe(() => set((x) => x + 1))
    const t = setInterval(() => { if (current()) set((x) => x + 1) }, 250)
    return () => { off(); clearInterval(t) }
  }, [])
  return current()
}

/** Events and meetings a recording made now probably belongs to: today's and yesterday's, the
 *  one going on first. */
function candidates(at: number): CalEvent[] {
  const near = (e: CalEvent) => Math.abs(+e.start - at)
  return calendar()
    .filter((e) => e.isNew && e.status !== 'cancelled' && +e.start - at < 12 * 3600e3 && +(e.end ?? e.start) > at - 36 * 3600e3)
    .sort((a, b) => {
      const on = (e: CalEvent) => (+e.start <= at && at <= +(e.end ?? e.start) + 3600e3 ? 0 : 1)
      return on(a) - on(b) || near(a) - near(b)
    })
    .slice(0, 6)
}

type Step = 'ready' | 'live' | 'where' | 'sending' | 'meeting'

export function RecorderSheet({ groupId, resumeOf, onClose }: { groupId: string; /** an unsent recording to send */ resumeOf?: RecMeta; onClose: () => void }) {
  const live = useRecorder()
  const [step, setStep] = useState<Step>(resumeOf ? 'where' : live ? 'live' : 'ready')
  const [done, setDone] = useState<RecMeta | null>(resumeOf ?? null)
  const [error, setError] = useState('')
  const inEvent = groupId.startsWith('ev-')
  const go = async () => {
    setError('')
    try { await start(groupId); setStep('live') } catch (x) {
      setError(x instanceof DOMException && x.name === 'NotAllowedError' ? 'The microphone is not allowed: allow it for this site in your browser settings.' : x instanceof Error ? x.message : String(x))
    }
  }
  const finish = async () => {
    const m = await stop()
    if (!m) return
    setDone(m)
    if (inEvent) await send(m, groupId)
    else setStep('where')
  }
  const send = async (m: RecMeta, target: string) => {
    setStep('sending'); setError('')
    try {
      const files = await filesOf(m)
      if (!files.length) throw new Error('Nothing was recorded.')
      // the outbox keeps the files on the device and uploads them, retrying, until they are out
      await postMessage(target, `🎙 Recording · ${clock(m.ms)}${files.length > 1 ? ` · ${files.length} parts` : ''}`, files)
      await discard(m.id)
      onClose()
    } catch (x) { setError(x instanceof Error ? x.message : String(x)); setStep('where') }
  }
  const ms = live ? elapsed() : done?.ms ?? 0
  const lvl = level()
  const list = candidates(done?.startedAt ?? Date.now())
  // closing while it records: it goes on (the pill in the chat brings this back)
  return (
    <>
      <Sheet title="Audio recording" onClose={onClose}>
        <div className="rec">
          {(step === 'ready' || step === 'live') && (<>
            <div className={'rec-clock' + (live?.state === 'recording' ? ' on' : '')} aria-live="off">{clock(ms)}</div>
            <div className="rec-meter" aria-hidden="true">{Array.from({ length: 24 }, (_, i) => <span key={i} className={i / 24 < lvl ? 'lit' : ''} />)}</div>
            <p className="hint rec-state" role="status">{step === 'ready' ? 'Kept on this device while it records, until it is sent.' : live?.state === 'paused' ? 'Paused' : `Recording · saved on this device${live ? ` (${live.parts.reduce((a, b) => a + b, 0)} s)` : ''}`}</p>
            <div className="rec-buttons">
              {step === 'ready' && <Btn className="btn rec-start" onClick={go}>● Start recording</Btn>}
              {step === 'live' && live && (live.state === 'paused'
                ? <Btn className="btn ghost" onClick={resume}>Resume</Btn>
                : <Btn className="btn ghost" onClick={pause}>Pause</Btn>)}
              {step === 'live' && live && <Btn className="btn" onClick={finish}>■ Stop</Btn>}
            </div>
          </>)}
          {step === 'where' && done && (<>
            <p className="rec-done"><b>{clock(done.ms)}</b> recorded{done.parts.length > 1 ? ` in ${done.parts.length} parts` : ''} · kept on this device</p>
            <h3 className="rec-q">Which event or meeting was it?</h3>
            <div className="act-group">
              {list.map((e) => (
                <Btn key={e.coord} className="act" onClick={() => send(done, e.groupId)}>
                  <span>{e.meeting ? 'Meeting · ' : ''}{e.title}<small>{fmtDay(e.start)} · {when(e)}{e.location ? ' · ' + e.location : ''}</small></span>
                </Btn>
              ))}
              <button className="act" onClick={() => setStep('meeting')}><span>New meeting…<small>Make it now, the recording goes in its chat</small></span></button>
              <Btn className="act" onClick={() => send(done, done.groupId)}><span>None: post it in #{group(done.groupId)?.name ?? done.groupId}</span></Btn>
            </div>
            <button className="link rec-discard" onClick={async () => { if (confirm('Throw this recording away? It cannot be brought back.')) { await discard(done.id); onClose() } }}>Throw it away</button>
          </>)}
          {step === 'sending' && <p className="hint" role="status">Handing it to the outbox… it uploads from this device, and retries until it is out.</p>}
          {error && <p className="error" role="alert">{error}</p>}
        </div>
      </Sheet>
      {step === 'meeting' && done && (
        <MeetingSheet channel={done.groupId} onClose={() => setStep((s) => (s === 'meeting' ? 'where' : s))}
          onCreated={(d) => void send(done, 'ev-' + d)} />
      )}
    </>
  )
}

/** In a chat: the live recording (tap: back to it), or an unsent one (send it, or throw it away). */
export function RecordingBar({ groupId }: { groupId: string }) {
  const live = useRecorder()
  const [left, setLeft] = useState<RecMeta[]>([])
  const [open, setOpen] = useState<RecMeta | 'live' | null>(null)
  useEffect(() => { void recover().then(setLeft) }, [live?.id, open])
  useEffect(() => subscribe(() => { void unsent().then((l) => setLeft(l.filter((m) => m.state === 'stopped' && m.parts.some((n) => n > 0)))) }), [])
  const first = left.find((m) => m.id !== live?.id)
  return (
    <>
      {live && <button className="rec-pill" onClick={() => setOpen('live')}><span className={live.state === 'recording' ? 'dot on' : 'dot'} />{live.state === 'paused' ? 'Paused' : 'Recording'} · {clock(elapsed())}</button>}
      {!live && first && (
        <div className="rec-unsent" role="status">
          <span>🎙 An unsent recording · {clock(first.ms)} · {fmtDay(new Date(first.startedAt))}</span>
          <button className="btn small" onClick={() => setOpen(first)}>Send</button>
        </div>
      )}
      {open && <RecorderSheet groupId={open === 'live' ? live?.groupId ?? groupId : open.groupId} resumeOf={open === 'live' ? undefined : open} onClose={() => setOpen(null)} />}
    </>
  )
}
