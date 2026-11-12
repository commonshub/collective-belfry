// Guest mode's own pieces: the one loading screen from an invite link to its channel, the bar
// that stands where the composer would be, and the Join sheet (name and e-mail, then the code
// that was sent by e-mail).
import { useEffect, useRef, useState } from 'react'
import { BELFRY, NAME } from '../config'
import { coHostToken, guestInfo, setCoHostToken } from '../core/auth'
import { JOIN_ERRORS, JoinFailed, sendJoinCode, verifyJoinCode } from '../core/guestjoin'
import { askToJoin, closeJoin, joinAsked, reading, useGuestMode } from '../core/guestmode'
import { channelIcon, group } from '../core/model'
import { touch } from '../core/store'
import { joinWithAccount } from '../core/sync'
import { Btn, Form, Sheet } from './common'
import { useCommunityName } from './community'

/** "as a member", "as Host": the plain role reads as a member. */
const asWho = (label: string) => (label.toLowerCase() === 'member' ? 'a member' : label)

/** From the link to the channel: one screen, nothing else in between (App.tsx). */
export function OpeningScreen({ name }: { name?: string }) {
  return (
    <div className="opening" role="status" aria-live="polite">
      <div className="spinner" aria-hidden="true" />
      <p>{name ? <>Opening <b># {name}</b>…</> : 'Opening…'}</p>
    </div>
  )
}

/** Where the composer would be: you are reading; Join opens the sheet. */
export function GuestBar({ groupId }: { groupId: string }) {
  useGuestMode()
  const g = group(groupId)
  const community = useCommunityName(NAME)
  if (!reading()) return null
  const member = guestInfo()?.member
  return (
    <div className="guest-bar" role="region" aria-label="Guest mode">
      {member
        ? <span><b>You’re invited</b> to join {community} as {asWho(member.label)}.</span>
        : <span><b>Guest mode</b> · You’re reading {g ? channelIcon(g) : '#'} {g?.name ?? groupId} as a guest.</span>}
      <button className="btn small" onClick={askToJoin}>Join</button>
    </div>
  )
}

/** Mounted once (App.tsx): the Join sheet, opened by the bar or by anything that writes. */
export function GuestJoinHost({ onSignIn }: { onSignIn: (channel: string, moved: boolean) => Promise<void> | void }) {
  useGuestMode()
  if (!joinAsked() || !guestInfo()) return null
  return <JoinSheet onClose={closeJoin} onSignIn={onSignIn} />
}

/** Six boxes for the code: one real field under them (paste, autofill and backspace just work). */
function CodeBoxes({ value, onChange, disabled, invalid }: { value: string; onChange: (v: string) => void; disabled?: boolean; invalid?: boolean }) {
  const ref = useRef<HTMLInputElement>(null)
  useEffect(() => { ref.current?.focus() }, [])
  return (
    <label className={'code-boxes' + (invalid ? ' bad' : '')}>
      <span className="sr-only">The 6-digit code from the e-mail</span>
      <input ref={ref} inputMode="numeric" pattern="[0-9]*" autoComplete="one-time-code" maxLength={6} disabled={disabled}
        value={value} onChange={(e) => onChange(e.target.value.replace(/\D/g, '').slice(0, 6))} />
      <span className="cells" aria-hidden="true">
        {[0, 1, 2, 3, 4, 5].map((i) => <i key={i} className={i === Math.min(value.length, 5) ? 'on' : ''}>{value[i] ?? ''}</i>)}
      </span>
    </label>
  )
}

type Step = 'who' | 'code' | 'taken'

/** Join (guest mode: name + e-mail) or save the account (a guest with a name: e-mail only), then
 *  the code step, which says where the code went. */
export function JoinSheet({ onClose, onSignIn, saveOnly = false, onSaved }: { onClose: () => void; onSignIn?: (channel: string, moved: boolean) => Promise<void> | void; /** a guest who has a name already: the e-mail alone */ saveOnly?: boolean; onSaved?: (email: string) => void }) {
  const info = guestInfo()
  const g = group(info?.channel ?? '')
  const label = `${g ? channelIcon(g) : '#'} ${g?.name ?? info?.channel ?? ''}`
  const [step, setStep] = useState<Step>('who')
  const [name, setName] = useState('')
  const [email, setEmail] = useState('')
  const [code, setCode] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [note, setNote] = useState('')
  const [moved, setMoved] = useState(false)
  const sentTo = useRef('') // what the person typed, as it was when the code went out
  const askName = !saveOnly
  const fail = (e: unknown) => {
    const f = e instanceof JoinFailed ? e : new JoinFailed('failed')
    if (f.code === 'taken') { setMoved(f.moved); setStep('taken'); setError(''); return }
    setError(JOIN_ERRORS[f.code])
  }
  const send = async () => {
    setError(''); setNote('')
    try {
      await sendJoinCode(email, askName ? name : undefined)
      sentTo.current = email.trim()
      setCode(''); setStep('code')
      return true
    } catch (e) { fail(e); return false }
  }
  const verify = async (c: string) => {
    if (busy) return
    setBusy(true); setError(''); setNote('')
    try {
      await verifyJoinCode(sentTo.current, c, askName ? name : undefined)
      // a co-host link waiting for a proven e-mail: now it makes them a host
      const cohost = coHostToken()
      if (cohost) { try { await joinWithAccount(cohost); setCoHostToken('') } catch { /* the link again later */ } }
      touch()
      onSaved?.(sentTo.current)
      onClose()
    } catch (e) { setCode(''); fail(e) } finally { setBusy(false) }
  }
  const community = useCommunityName(NAME)
  const member = saveOnly ? undefined : info?.member
  const title = step === 'code' ? 'Check your e-mail' : step === 'taken' ? 'You have an account here' : saveOnly ? 'Save your account' : member ? `Join ${community} as ${member.label}` : `Join ${label}`
  return (
    <Sheet title={title} onClose={() => { if (!busy) onClose() }}>
      {step === 'who' && (
        <Form className="join-sheet" onSubmit={send}>
          <p className="hint">{saveOnly
            ? 'Add your e-mail address to come back from any device. We’ll send a 6-digit code to it.'
            : member
            ? `Your name and e-mail address, and you are a member of ${community}${member.membership === 'permanent' ? '' : member.membership === 'year' ? ' for a year' : ' for a month'}. We’ll send a 6-digit code to your e-mail to confirm it.`
            : 'Your name and e-mail address, and you can write here. We’ll send a 6-digit code to your e-mail to confirm it.'}</p>
          {askName && <label className="field"><span>Your name</span>
            <input className="input" value={name} maxLength={100} autoComplete="name" autoFocus required onChange={(e) => setName(e.target.value)} /></label>}
          <label className="field"><span>E-mail</span>
            <input className="input" type="email" inputMode="email" autoComplete="email" autoCapitalize="none" spellCheck={false} required autoFocus={!askName}
              placeholder="you@example.org" value={email} onChange={(e) => setEmail(e.target.value)} /></label>
          {error && <div className="error" role="alert">{error}</div>}
          <button className="btn" disabled={(askName && !name.trim()) || !/^\S+@\S+\.\S+$/.test(email.trim())}>Continue</button>
          {!saveOnly && <p className="hint small">Already have an account on {BELFRY.domain}? Use its e-mail address.</p>}
        </Form>
      )}
      {step === 'code' && (
        <div className="join-sheet">
          <p>We sent a 6-digit code <b>by e-mail</b> to <b className="addr">{sentTo.current}</b>. It can take a minute.</p>
          <CodeBoxes value={code} disabled={busy} invalid={!!error} onChange={(v) => { setCode(v); setError(''); if (v.length === 6) void verify(v) }} />
          <div className="join-state" role="status" aria-live="polite">
            {busy ? <span className="hint">Checking the code…</span> : error ? <span className="error" role="alert">{error}</span> : note ? <span className="hint">{note}</span> : <span className="hint">Not there? Look in your spam folder.</span>}
          </div>
          <div className="join-links">
            <Btn className="link" disabled={busy} onClick={async () => { if (await send()) setNote('A new code is on its way to your e-mail.') }}>Send a new code</Btn>
            <button type="button" className="link" disabled={busy} onClick={() => { setStep('who'); setError(''); setNote(''); setCode('') }}>Use another address</button>
          </div>
        </div>
      )}
      {step === 'taken' && (
        <div className="join-sheet">
          <p><b className="addr">{sentTo.current}</b> already has an account on {BELFRY.domain}. {moved ? <>Sign in with it: {label} is waiting there.</> : 'Sign in with it instead.'}</p>
          <Btn className="btn" onClick={async () => { await onSignIn?.(info?.channel ?? '', moved) }}>Sign in instead</Btn>
          {!moved && <><div style={{ height: 10 }} /><button type="button" className="btn ghost" onClick={() => { setStep('who'); setError('') }}>Use another address</button></>}
        </div>
      )}
    </Sheet>
  )
}
