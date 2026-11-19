// Guest mode's own pieces: the one loading screen from an invite link to its channel, the bar
// that stands where the composer would be, and the Join sheet (name and e-mail, then the code
// that was sent by e-mail).
import { useEffect, useRef, useState } from 'react'
import { BELFRY, NAME } from '../config'
import { coHostToken, guestInfo, setCoHostToken } from '../core/auth'
import { JoinFailed, joinErrorText, nameDone, sendJoinCode, verifyJoinCode } from '../core/guestjoin'
import { DirectFailed, directErrorText, welcomeName } from '../core/direct'
import { NameFields, Offers, type NameState } from './UsernameField'
import { switchToLogin, switchToSignedIn } from '../core/signedin'
import { askToJoin, closeJoin, joinAsked, reading, useGuestMode } from '../core/guestmode'
import { channelIcon, group, nameOf } from '../core/model'
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

/** A belfry member link (board onboarding/member-link, Xavier 19 Nov): no channel behind it.
 *  "You're invited to <community>", the e-mail, Send me a code → the six boxes → in the chat. No
 *  name (the belfry takes it from the address), no Sign up / log in choice, no profile step; an
 *  address that has an account already signs in with it (guest-code). */
export function MemberInviteScreen() {
  useGuestMode()
  const community = useCommunityName(NAME)
  const member = guestInfo()?.member
  const [email, setEmail] = useState('')
  const [error, setError] = useState('')
  if (!member) return null
  const stay = member.membership === 'permanent' || !member.membership ? '' : member.membership === 'year' ? ' for a year' : ' for a month'
  return (
    <div className="screen login member-invite">
      <h2>You’re invited to {community}</h2>
      <p>You’re invited as {asWho(member.label)}{stay}. Enter your e-mail: we send a 6-digit code, and you’re in.</p>
      <Form className="join-sheet start-form" onSubmit={async () => {
        setError('')
        try { await sendJoinCode(email); handOff.email = email.trim(); askToJoin() } catch (e) { setError(joinErrorText(e instanceof JoinFailed ? e : new JoinFailed('failed'))) }
      }}>
        <label className="field"><span>E-mail</span>
          <input className="input" type="email" inputMode="email" autoComplete="email" autoCapitalize="none" spellCheck={false} required autoFocus
            placeholder="you@example.org" value={email} onChange={(e) => setEmail(e.target.value)} /></label>
        {error && <div className="error" role="alert">{error}</div>}
        <button className="btn" disabled={!/^\S+@\S+\.\S+$/.test(email.trim())}>Send me a code</button>
        <Btn className="link" onClick={() => switchToLogin()}>I’m already a member · Log in</Btn>
      </Form>
    </div>
  )
}

/** The member link screen sent the code already: the Join sheet opens on the six boxes. */
const handOff: { email: string } = { email: '' }

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
        : <span><b>Guest mode</b> · You’re reading {g ? channelIcon(g) : '#'} {nameOf(groupId)} as a guest.</span>}
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

/** What the six boxes hold after a change: the digits, at most six; a whole code pasted over a
 *  full row replaces it; one more digit typed into a full row changes nothing. */
export function nextCode(old: string, raw: string): string {
  const d = raw.replace(/\D/g, '')
  if (d.length <= 6) return d
  const added = d.startsWith(old) ? d.slice(old.length) : d
  return (added.length >= 6 ? added : d).slice(0, 6)
}

/** Six boxes for the code: one real field under them (paste, autofill and backspace just work). */
export function CodeBoxes({ value, onChange, disabled, invalid }: { value: string; onChange: (v: string) => void; disabled?: boolean; invalid?: boolean }) {
  const ref = useRef<HTMLInputElement>(null)
  useEffect(() => { ref.current?.focus() }, [])
  return (
    <label className={'code-boxes' + (invalid ? ' bad' : '')}>
      <span className="sr-only">The 6-digit code from the e-mail</span>
      {/* no maxLength: it would cut a pasted "123 456" or "Code: 123456" before the digits are
          picked, and refuse a new code pasted over a full row (#issues 2026-11-13) */}
      <input ref={ref} inputMode="numeric" pattern="[0-9]*" autoComplete="one-time-code" disabled={disabled}
        value={value} onChange={(e) => onChange(nextCode(value, e.target.value))} />
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
  const [step, setStep] = useState<Step>(() => (handOff.email ? 'code' : 'who'))
  const [name, setName] = useState('')
  const [email, setEmail] = useState(handOff.email)
  const [code, setCode] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [note, setNote] = useState('')
  const [moved, setMoved] = useState(false)
  const sentTo = useRef(handOff.email) // what the person typed, as it was when the code went out
  useEffect(() => { handOff.email = '' }, [])
  // a member link: no name (the belfry takes it from the address); a guest link asks for one
  const askName = !saveOnly && !info?.member
  const fail = (e: unknown) => {
    const f = e instanceof JoinFailed ? e : new JoinFailed('failed')
    if (f.code === 'taken') { setMoved(f.moved); setStep('taken'); setError(''); return }
    setError(joinErrorText(f))
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
  const verify = async (c: string, again = false) => {
    if (busy) return
    setBusy(true); setError(''); setNote('')
    try {
      const r = await verifyJoinCode(sentTo.current, c, askName ? name : undefined, again)
      // the address has an account: the right code signs in as it (no second code)
      if (r) { await switchToSignedIn(r); return }
      // a co-host link waiting for a proven e-mail: now it makes them a host
      const cohost = coHostToken()
      if (cohost) { try { await joinWithAccount(cohost); setCoHostToken('') } catch { /* the link again later */ } }
      touch()
      onSaved?.(sentTo.current)
      onClose()
    } catch (e) {
      // a wrong or expired code is cleared; a connection that failed keeps the digits for Try again
      const f = e instanceof JoinFailed ? e.code : 'failed'
      if (f === 'wrong_code' || f === 'expired' || f === 'key_taken' || f === 'signer') setCode('')
      else retryable.current = c
      fail(e)
    } finally { setBusy(false) }
  }
  // the code that could not be checked (no connection): again when the page is back in front
  const retryable = useRef('')
  useEffect(() => {
    const back = () => { if (document.visibilityState === 'visible' && retryable.current && !busy) { const c = retryable.current; retryable.current = ''; void verify(c, true) } }
    document.addEventListener('visibilitychange', back)
    addEventListener('online', back)
    return () => { document.removeEventListener('visibilitychange', back); removeEventListener('online', back) }
  })
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
          {!busy && error && code.length === 6 && <Btn className="btn" onClick={() => { retryable.current = ''; return verify(code, true) }}>Try again</Btn>}
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

/** After a member link's code (board onboarding/member-link-name, canvas v54): how people will
 *  know them here. Name + username (POST /welcome through the new member's session), then the
 *  chat. Shown again on the next open while due (core/guestjoin nameNext: < 24 h, not set yet);
 *  the belfry's already_set (or a guest's 403) ends it. */
export function MemberNameScreen({ email, onDone }: { email: string; onDone: () => void }) {
  const community = useCommunityName(NAME)
  const [who, setWho] = useState<NameState>({ name: '', username: '', ok: false })
  const [error, setError] = useState('')
  const [sug, setSug] = useState<string[]>([])
  return (
    <div className="screen login start member-name">
      <Form className="start-form" onSubmit={async () => {
        setError('')
        if (!who.name.trim() || !who.ok) return
        try {
          await welcomeName(who.name, who.username.trim())
          nameDone(); touch(); onDone()
        } catch (e) {
          const f = e instanceof DirectFailed ? e : new DirectFailed('failed')
          if (f.code === 'username_taken' || f.code === 'username_invalid') { setSug(f.suggestions); setWho((w) => ({ ...w, ok: false })) }
          setError(directErrorText(f))
        }
      }}>
        <h2>Welcome to {community}</h2>
        <p className="hint">{email ? <><b className="addr">{email}</b> is confirmed. </> : null}One last thing: how people will know you here.</p>
        <NameFields value={who} onChange={(v) => { setWho(v); if (v.username !== who.username) setSug([]) }} via="session" autoFocus />
        {sug.length > 0 && <p className="username-state"><span className="error">Try <Offers list={sug} pick={(x) => { setWho({ ...who, username: x, ok: false }); setSug([]) }} /></span></p>}
        {error && <div className="error" role="alert">{error}</div>}
        <button className="btn" disabled={!who.name.trim() || !who.ok}>Go to the chat</button>
      </Form>
    </div>
  )
}
