import { useRef, useState, type ClipboardEvent, type KeyboardEvent } from 'react'
import { driver, type Session } from '../core/auth'
import type { Backend } from '../config'

const Logo = () => <img className="logo" src="./icons/osv-logo-horizontal-white.svg" alt="Open Source Village" />

function useBusy() {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const run = async (fn: () => Promise<void>) => {
    setBusy(true)
    setError('')
    try {
      await fn()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }
  return { busy, error, run }
}

export function EmailScreen({ backend, initial, onSent }: { backend: Backend; initial: string; onSent: (email: string) => void }) {
  const [email, setEmail] = useState(initial)
  const { busy, error, run } = useBusy()
  const valid = /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email.trim())
  return (
    <form
      className="screen login"
      onSubmit={(e) => {
        e.preventDefault()
        if (valid) run(async () => { await driver(backend).requestCode(email.trim()); onSent(email.trim()) })
      }}
    >
      <Logo />
      <h2>Welcome to the village</h2>
      <p>Sign in with the e-mail address you used for your pass or your invite. No password, ever.</p>
      <label className="field">
        <span>E-mail</span>
        <input className="input" type="email" inputMode="email" autoComplete="email" autoCapitalize="off"
          autoFocus placeholder="you@example.org" value={email} onChange={(e) => setEmail(e.target.value)} />
      </label>
      {error && <div className="error">{error}</div>}
      <button className="btn" disabled={!valid || busy}>{busy ? 'Sending…' : 'Send me a code'}</button>
      {backend === 'demo' && <div className="demo-note">Demo mode: nothing leaves this phone. Any 6-digit code works.</div>}
    </form>
  )
}

export function CodeScreen({ backend, email, onBack, onVerified }: {
  backend: Backend; email: string; onBack: () => void; onVerified: (s: Session) => void
}) {
  const [digits, setDigits] = useState(['', '', '', '', '', ''])
  const refs = useRef<(HTMLInputElement | null)[]>([])
  const { busy, error, run } = useBusy()

  const submit = (d: string[]) => {
    const code = d.join('')
    if (code.length === 6) run(async () => onVerified(await driver(backend).verifyCode(email, code)))
  }
  const fill = (start: number, text: string) => {
    const only = text.replace(/\D/g, '').slice(0, 6 - start).split('')
    if (!only.length) return
    const next = [...digits]
    only.forEach((c, i) => (next[start + i] = c))
    setDigits(next)
    refs.current[Math.min(start + only.length, 5)]?.focus()
    submit(next)
  }
  const onKey = (i: number, e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Backspace' && !digits[i] && i > 0) {
      const next = [...digits]
      next[i - 1] = ''
      setDigits(next)
      refs.current[i - 1]?.focus()
    }
  }
  return (
    <form className="screen login" onSubmit={(e) => { e.preventDefault(); submit(digits) }}>
      <Logo />
      <h2>Check your e-mail</h2>
      <p>We sent a 6-digit code to <b style={{ color: 'var(--text)' }}>{email}</b>. It's valid for 10 minutes.</p>
      <div className="code">
        {digits.map((d, i) => (
          <input key={i} ref={(el) => { refs.current[i] = el }} value={d} inputMode="numeric" pattern="[0-9]*"
            autoComplete={i === 0 ? 'one-time-code' : 'off'} autoFocus={i === 0} aria-label={`Digit ${i + 1}`}
            onChange={(e) => {
              const v = e.target.value
              if (!v) { const next = [...digits]; next[i] = ''; setDigits(next); return }
              fill(i, v)
            }}
            onPaste={(e: ClipboardEvent<HTMLInputElement>) => { e.preventDefault(); fill(0, e.clipboardData.getData('text')) }}
            onKeyDown={(e) => onKey(i, e)} />
        ))}
      </div>
      {error && <div className="error">{error}</div>}
      <button className="btn" disabled={busy || digits.join('').length < 6}>{busy ? 'Checking…' : 'Sign in'}</button>
      <div className="row" style={{ justifyContent: 'space-between', marginTop: 18 }}>
        <button type="button" className="link" onClick={onBack}>Use another e-mail</button>
        <button type="button" className="link" disabled={busy}
          onClick={() => run(() => driver(backend).requestCode(email))}>Send a new code</button>
      </div>
    </form>
  )
}

export function MembershipScreen({ session, onUpdate, onSignOut }: {
  session: Session; onUpdate: (s: Session) => void; onSignOut: () => void
}) {
  const [code, setCode] = useState('')
  const { busy, error, run } = useBusy()
  const m = session.membership
  return (
    <form
      className="screen login"
      onSubmit={(e) => {
        e.preventDefault()
        run(async () => onUpdate(await driver(session.backend).redeemInvite(session, code.trim())))
      }}
    >
      <Logo />
      <div style={{ marginBottom: 12 }}>
        <span className={'badge ' + (m.status === 'revoked' ? '' : 'orange')}>
          {m.status === 'revoked' ? 'Pass revoked' : 'Membership pending'}
        </span>
      </div>
      <h2>{m.status === 'revoked' ? 'Your pass is no longer valid' : 'Almost in'}</h2>
      <p>
        You're signed in as <b style={{ color: 'var(--text)' }}>{session.email}</b>, but there's no active pass on this
        address yet. Got an invite code from a villager or a steward? Enter it here.
      </p>
      <label className="field">
        <span>Invite code</span>
        <input className="input" autoCapitalize="characters" autoComplete="off" placeholder="OSV-XXXX-XXXX"
          value={code} onChange={(e) => setCode(e.target.value)} />
      </label>
      {error && <div className="error">{error}</div>}
      <button className="btn" disabled={busy || code.trim().length < 4}>{busy ? 'Checking…' : 'Use invite'}</button>
      <div className="row" style={{ justifyContent: 'space-between', marginTop: 18 }}>
        <a className="link" href="https://belfry.opensourcevillage.org/apply" target="_blank" rel="noreferrer">No invite? Apply</a>
        <button type="button" className="link" onClick={onSignOut}>Sign out</button>
      </div>
      {session.backend === 'demo' && <div className="demo-note">Demo mode: any invite code of 4+ characters works.</div>}
    </form>
  )
}
