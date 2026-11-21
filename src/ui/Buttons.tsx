import { useEffect, useState } from 'react'
import type { NostrEvent } from 'nostr-tools'
import { BELFRY } from '../config'
import { ANSWER_TIMEOUT, FIXED, buttonsOf, isBelfryAction, knownAction, messageButtons, press, type Button } from '../core/buttons'
import { askToJoin, reading } from '../core/guestmode'
import { useVersion } from '../core/store'
import { Btn, Sheet } from './common'

// Buttons under a message (docs/actions.md): the person's own key signs the press, the handler
// answers. All of a message's buttons lock while a press waits for its answer (at most 20 s).

/** The client's own words before a belfry: action (never the button's label alone). */
function confirmText(action: string, id = ''): { text: string; yes: string } | undefined {
  if (action === 'belfry:offer-decide') {
    // what the answer is comes from the button's id, in the app's own words
    const pct = /^offer-(\d{1,3})$/.exec(id)?.[1]
    const what = id === 'offer-no' ? 'Decline this request?' : pct ? `Give ${pct}% off?` : 'Answer this offer?'
    return { text: `${what} ${FIXED['confirm:belfry:offer-decide'].replace(/^Answer this offer\? /, '')}`, yes: id === 'offer-no' ? 'Decline' : pct ? `Give ${pct}% off` : 'Answer' }
  }
  if (action === 'belfry:request-close') return { text: FIXED['confirm:belfry:request-close'], yes: 'Close' }
  if (action === 'belfry:website-publish') return { text: FIXED['confirm:belfry:website-publish'].replace('<domain>', BELFRY.domain), yes: 'Publish' }
  return undefined
}

/** The pressed button's words while the belfry answers (canvas v101: "Closing"). */
const BUSY: Record<string, string> = { 'belfry:request-close': 'Closing' }

const nowSec = () => Math.floor(Date.now() / 1000)

export function MessageButtons({ ev, groupId, disabled = false }: { ev: NostrEvent; groupId: string; disabled?: boolean }) {
  useVersion()
  const buttons = buttonsOf(ev)
  const [asking, setAsking] = useState<Button | null>(null)
  const [sendError, setSendError] = useState('')
  const [, tick] = useState(0)
  const st = buttons.length ? messageButtons(ev, buttons) : null
  const waitingFor = st?.press && !st.mine ? nowSec() - st.press.created_at : -1
  const waiting = waitingFor >= 0 && waitingFor < ANSWER_TIMEOUT
  // wake up when the 20 s are over (the line changes to "No answer yet.")
  useEffect(() => {
    if (!waiting) return
    const t = setTimeout(() => tick((n) => n + 1), (ANSWER_TIMEOUT - waitingFor) * 1000 + 50)
    return () => clearTimeout(t)
  }, [waiting, waitingFor])
  if (!st) return null
  if (st.closed) return <div className="msg-buttons closed" role="status"><span className="mb-line ok">{st.closed_text || 'Done.'}</span></div>

  const send = async (b: Button) => {
    setSendError('')
    try {
      await press(ev, groupId, b)
    } catch {
      setSendError("Couldn't send. Check your connection and try again.")
    }
  }
  const tap = async (b: Button) => {
    if (reading()) { askToJoin(); return }
    if (isBelfryAction(b.action) || b.style === 'danger') { setAsking(b); return }
    await send(b)
  }
  const line = sendError ? { cls: 'error', text: sendError }
    : st.mine ? { cls: st.mine.status, text: st.mine.text }
    : waiting ? null
    : st.press ? { cls: 'muted', text: FIXED.timeout }
    : null
  const unknown = buttons.some((b) => !knownAction(b.action))
  const ask = asking ? (isBelfryAction(asking.action) ? confirmText(asking.action, asking.id) : { text: `${asking.label}?`, yes: asking.label }) : undefined
  return (
    <div className="msg-buttons" onPointerDown={(e) => e.stopPropagation()} onContextMenu={(e) => e.stopPropagation()}>
      <div className="mb-row">
        {buttons.map((b) => (
          <Btn key={b.id} className={'mb' + (b.style ? ' ' + b.style : '')} aria-busy={waiting || undefined}
            disabled={disabled || waiting || !knownAction(b.action)} onClick={() => tap(b)}>
            {waiting && st.press?.button === b.id && <span className="mb-spin" aria-hidden />}{waiting && st.press?.button === b.id && BUSY[b.action] || b.label}
          </Btn>
        ))}
      </div>
      {waiting && <div className="mb-line muted" role="status">Waiting for an answer…</div>}
      {unknown && <div className="mb-line muted">{FIXED.unknown_belfry_action}</div>}
      {line && <div className={'mb-line ' + line.cls} role={line.cls === 'error' ? 'alert' : 'status'}>{line.text}</div>}
      {asking && ask && (
        <Sheet title={ask.text} hideTitle onClose={() => setAsking(null)}>
          <p className="mb-ask">{ask.text}</p>
          <div className="row-btns">
            <button type="button" className="btn ghost" onClick={() => setAsking(null)}>Cancel</button>
            <Btn className={'btn' + (asking.style === 'danger' ? ' danger' : '')} onClick={async () => { const b = asking; setAsking(null); await send(b) }}>{ask.yes}</Btn>
          </div>
        </Sheet>
      )}
    </div>
  )
}
