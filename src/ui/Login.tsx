import { useState } from 'react'
import { startSignIn, type Session } from '../core/auth'
import { BELFRY, type Backend } from '../config'

const Logo = () => <img className="logo" src="./icons/osv-logo-horizontal-white.svg" alt="Open Source Village" />

// An invite code is the last part of https://shop.<domain>/belfry/invite/<code>; people
// paste either. The shop's invite page shows the passes and takes the payment.
export function inviteUrl(input: string) {
  const code = input.trim().split('/invite/').pop()!.replace(/[/?#].*$/, '')
  return `${BELFRY.shop}/belfry/invite/${encodeURIComponent(code)}`
}

function InviteForm() {
  const [code, setCode] = useState('')
  return (
    <form onSubmit={(e) => { e.preventDefault(); location.assign(inviteUrl(code)) }}>
      <label className="field">
        <span>Invite code or link</span>
        <input className="input" autoCapitalize="off" autoComplete="off" spellCheck={false}
          placeholder="Paste the code from your invite" value={code} onChange={(e) => setCode(e.target.value)} />
      </label>
      <button className="btn ghost" disabled={code.trim().length < 6}>Choose my pass</button>
    </form>
  )
}

export function WelcomeScreen({ backend, error, onDemo }: { backend: Backend; error?: string; onDemo: (email: string) => void }) {
  const [busy, setBusy] = useState(false)
  const [invite, setInvite] = useState(false)
  const [email, setEmail] = useState('')
  if (backend === 'demo')
    return (
      <form className="screen login" onSubmit={(e) => { e.preventDefault(); if (email.includes('@')) onDemo(email.trim()) }}>
        <Logo />
        <h2>Demo mode</h2>
        <p>Everything stays on this device. <a href="?backend=belfry">Use the real village</a>.</p>
        <label className="field"><span>E-mail</span>
          <input className="input" type="email" value={email} onChange={(e) => setEmail(e.target.value)} /></label>
        <button className="btn">Enter</button>
      </form>
    )
  return (
    <div className="screen login">
      <Logo />
      <h2>Welcome to the village</h2>
      <p>Sign in with the e-mail address of your pass. We'll mail you a 6-digit code. No password, ever.</p>
      {error && <div className="error">{error}</div>}
      <button className="btn" disabled={busy} onClick={() => { setBusy(true); void startSignIn() }}>
        {busy ? 'Opening…' : 'Sign in with e-mail'}
      </button>
      <div style={{ height: 28 }} />
      {invite ? <InviteForm /> : (
        <button className="btn ghost" onClick={() => setInvite(true)}>I have an invite code</button>
      )}
      <p className="hint" style={{ marginTop: 18, fontSize: 14 }}>
        No pass and no invite? <a href="https://belfry.opensourcevillage.org/apply" target="_blank" rel="noreferrer">Apply to join</a>.
      </p>
    </div>
  )
}

export function MembershipScreen({ session, reason, onRetry, onSignOut }: {
  session: Session; reason?: string; onRetry: () => void; onSignOut: () => void
}) {
  return (
    <div className="screen login">
      <Logo />
      <div style={{ marginBottom: 12 }}><span className="badge orange">Membership pending</span></div>
      <h2>Almost in</h2>
      <p>
        You're signed in{session.membership.nip05 ? <> as <b style={{ color: 'var(--text)' }}>{session.membership.nip05}</b></> : ''},
        but there's no active pass on your account. If you just paid, it can take a minute.
        Got an invite code from a villager or a steward? Use it here.
      </p>
      {reason && <p className="hint" style={{ fontSize: 13 }}>The village said: {reason}</p>}
      <InviteForm />
      <div style={{ height: 12 }} />
      <button className="btn" onClick={onRetry}>Check again</button>
      <div className="row" style={{ justifyContent: 'space-between', marginTop: 18 }}>
        <a className="link" href="https://belfry.opensourcevillage.org/apply" target="_blank" rel="noreferrer">No invite? Apply</a>
        <button type="button" className="link" onClick={onSignOut}>Sign out</button>
      </div>
    </div>
  )
}
