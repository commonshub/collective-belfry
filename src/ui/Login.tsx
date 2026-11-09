import { useEffect, useState } from 'react'
import { INVITE_CLOSED, inviteInfo, joinAsNew, setPendingInvite, startSignIn, type InviteInfo, type Session } from '../core/auth'
import { accessInfo, lastRequest, loadAccessInfo, normalizeCode, onAccessInfo, requestAccess } from '../core/access'
import { recheckAccess } from '../core/sync'
import { BELFRY, NAME, THEME, type Backend } from '../config'

// The arms: a tower over water on a shield (the Belfry mark).
export const Arms = ({ size = 18 }: { size?: number }) => (
  <svg width={size} height={Math.round(size * 70 / 64)} viewBox="0 0 64 70" aria-hidden="true">
    <path d="M8 6a2 2 0 0 1 2-2h44a2 2 0 0 1 2 2v24c0 17-11 29-24 35C19 59 8 47 8 30Z" fill="currentColor" />
    <path d="M22 43V24h-2v-6h4v3h3v-3h4v3h2v-3h4v3h3v-3h4v6h-2v19Z" fill="var(--accent)" />
    
  </svg>
)

const Logo = () =>
  THEME === 'belfry' ? (
    <div className="logo app-logo">
      <span className="app-icon"><svg width="22" height="22" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 5h16v11H9l-5 4Z" fill="currentColor" /></svg></span>
      <span><b>Chat</b><small>{NAME}</small></span>
    </div>
  ) : (
    <img className="logo" src="./icons/osv-logo-horizontal-white.svg" alt="Open Source Village" />
  )

// An invite code typed before signing in (or from ?invite=) waits here until there is a key
// to send it with.
const CODE_KEY = 'osv-invite-code'
export function savedCode() {
  const q = new URLSearchParams(location.search).get('invite')
  try {
    if (q) localStorage.setItem(CODE_KEY, normalizeCode(q))
    return localStorage.getItem(CODE_KEY) ?? ''
  } catch { return q ?? '' }
}
const saveCode = (code: string) => { try { localStorage.setItem(CODE_KEY, normalizeCode(code)) } catch { /* private mode */ } }
export const dropCode = () => { try { localStorage.removeItem(CODE_KEY) } catch { /* private mode */ } }

/** Opened from a guest link, not signed in: who invites to what, a name, and you're in. */
export function JoinScreen({ token, backend, onJoined, onSignIn, onDemo }: { token: string; backend: Backend; onJoined: (s: Session) => void; onSignIn: () => void; onDemo: (name: string) => void }) {
  const [info, setInfo] = useState<InviteInfo | null>(null)
  const [error, setError] = useState('')
  const [name, setName] = useState('')
  const [busy, setBusy] = useState(false)
  useEffect(() => {
    if (backend === 'demo') return setInfo({ community: NAME, channel: 'general', channel_name: 'general', about: 'Where everyone talks.', inviter: 'Ana', state: 'open', days: 30 })
    inviteInfo(token).then(setInfo, (e) => setError(e instanceof Error ? e.message : String(e)))
  }, [token])
  const forget = () => { setPendingInvite(''); location.reload() }
  const closed = info && info.state !== 'open' ? INVITE_CLOSED[info.state] ?? INVITE_CLOSED.used : ''
  const join = async (e: React.FormEvent) => {
    e.preventDefault()
    const n = name.trim().replace(/\s+/g, ' ')
    if (!n || busy) return
    if (backend === 'demo') return onDemo(n)
    setBusy(true); setError('')
    try { onJoined(await joinAsNew(token, n)) } catch (x) {
      const m = x instanceof Error ? x.message : String(x)
      setError(/fetch|network|load/i.test(m) ? 'No connection to the belfry. Try again.' : m)
      setBusy(false)
    }
  }
  if (!info || closed)
    return (
      <div className="screen login">
        <Logo />
        {closed || error ? <><h2>This invite can’t be used</h2><p role="alert">{closed || error}</p></> : <p role="status">Opening the invite…</p>}
        {(closed || error) && <>
          <button className="btn" onClick={() => { if (closed) setPendingInvite(''); onSignIn() }}>I have an account: sign in</button>
          <div style={{ height: 12 }} />
          <button className="link" style={{ alignSelf: 'center' }} onClick={forget}>Close</button>
        </>}
      </div>
    )
  return (
    <form className="screen login" onSubmit={join}>
      <Logo />
      <h2>{info.inviter || 'A member'} invited you to # {info.channel_name}</h2>
      {info.about && <p style={{ marginBottom: 12 }}>{info.about}</p>}
      {error && <div className="error" role="alert">{error}</div>}
      <label className="field"><span>Your name</span>
        <input className="input" value={name} maxLength={100} autoComplete="name" autoFocus onChange={(e) => setName(e.target.value)} /></label>
      <button className="btn" disabled={busy || !name.trim()}>{busy ? 'Joining…' : `Join # ${info.channel_name}`}</button>
      <p className="hint" style={{ fontSize: 13, margin: '12px 0 0' }}>You’ll be a guest of {info.community} for {info.days} days: you see and post in this channel only. No e-mail needed now.</p>
      <div style={{ height: 28 }} />
      <button type="button" className="link" style={{ alignSelf: 'center' }} onClick={onSignIn}>Already have an account? Sign in</button>
    </form>
  )
}

export function WelcomeScreen({ backend, error, onDemo, invited }: { backend: Backend; error?: string; onDemo: (email: string) => void; invited?: boolean }) {
  const [busy, setBusy] = useState(false)
  const [invite, setInvite] = useState(!!savedCode())
  const [code, setCode] = useState(savedCode())
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
  const signIn = () => { setBusy(true); void startSignIn() }
  return (
    <div className="screen login">
      <Logo />
      {THEME === 'belfry' ? (
        <>
          <h2>Talk with your community</h2>
          <p>Channels, events and files for the people of {NAME}. You sign in on {BELFRY.domain}: no password, a 6-digit code by e-mail.</p>
        </>
      ) : (
        <>
          <h2>Welcome to the village</h2>
          <p>Sign in with your e-mail address. We'll mail you a 6-digit code. No password, ever.</p>
        </>
      )}
      {invited && <div className="card" role="status" style={{ marginBottom: 16 }}>After signing in, your invite opens its channel.</div>}
      {error && <div className="error">{error}</div>}
      {invite ? (
        <form onSubmit={(e) => { e.preventDefault(); saveCode(code); signIn() }}>
          <label className="field">
            <span>Invite code</span>
            <input className="input" autoCapitalize="characters" autoComplete="off" spellCheck={false}
              placeholder="OSV-XXXX-XXXX" value={code} onChange={(e) => setCode(e.target.value)} />
          </label>
          <button className="btn" disabled={busy || normalizeCode(code).length < 6}>{busy ? 'Opening…' : 'Sign in and use my code'}</button>
        </form>
      ) : (
        <>
          <button className="btn" disabled={busy} onClick={signIn}>{busy ? 'Opening…' : THEME === 'belfry' ? <><Arms /> Sign in with {BELFRY.domain}</> : 'Sign in with e-mail'}</button>
          <div style={{ height: 28 }} />
          <button className="btn ghost" onClick={() => setInvite(true)}>I have an invite code</button>
        </>
      )}
      <p className="hint" style={{ marginTop: 18, fontSize: 14 }}>
        No invite? Sign in all the same and ask to join: {THEME === 'belfry' ? 'the admins' : "the village's admins"} get your note.
      </p>
    </div>
  )
}

type Sent = 'code' | 'admin' | 'request'

/** Signed in, but the relays don't let this key in (yet): become the first admin, use an
 *  invite code or ask the admins. */
export function MembershipScreen({ session, reason, onSignOut }: {
  session: Session; reason?: string; onSignOut: () => void
}) {
  const [, setTick] = useState(0)
  const [sent, setSent] = useState<Sent | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [code, setCode] = useState(savedCode())
  const [note, setNote] = useState('')
  const [name, setName] = useState('')
  const [loaded, setLoaded] = useState(false)

  useEffect(() => {
    void loadAccessInfo().then(() => setLoaded(true))
    void lastRequest().then((r) => { if (r && Date.now() - r.at < 7 * 86400_000) setSent(r.kind) })
    return onAccessInfo(() => setTick((t) => t + 1))
  }, [])

  // after sending: ask the relays again, often at first
  useEffect(() => {
    if (!sent) return
    let n = 0
    let timer: ReturnType<typeof setTimeout>
    const again = () => {
      recheckAccess()
      if (sent === 'admin') void loadAccessInfo()
      timer = setTimeout(again, ++n < 20 ? 3000 : 30000)
    }
    timer = setTimeout(again, 1500)
    return () => clearTimeout(timer)
  }, [sent])

  const send = async (kind: Sent, input: Parameters<typeof requestAccess>[0]) => {
    setBusy(true)
    setError('')
    try {
      await requestAccess(input)
      if (kind === 'code') dropCode()
      setSent(kind)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  // an invite code typed before signing in goes out by itself
  useEffect(() => {
    const c = savedCode()
    if (c && accessInfo()) void send('code', { claim: c })
  }, [accessInfo()?.service])

  const a = accessInfo()
  const who = session.email ? <> as <b style={{ color: 'var(--text)' }}>{session.email}</b></> : ''
  return (
    <div className="screen login" style={{ justifyContent: 'flex-start', overflowY: 'auto' }}>
      <Logo />
      {sent ? (
        <>
          <div style={{ marginBottom: 12 }}><span className="badge orange">{sent === 'request' ? 'Request sent' : 'Checking…'}</span></div>
          <h2>{sent === 'admin' ? 'Setting you up as admin' : sent === 'code' ? 'Checking your code' : 'Waiting for an admin'}</h2>
          <p>
            {sent === 'request'
              ? "The village's admins got your note. You're in as soon as one of them says yes; this page opens by itself then."
              : 'This takes a few seconds. If nothing happens, the code may be used up: ask the person who gave it to you, or ask the admins below.'}
          </p>
          <button className="btn" onClick={() => { recheckAccess(); void loadAccessInfo() }}>Check again</button>
          <div style={{ height: 10 }} />
          <button className="btn ghost" onClick={() => setSent(null)}>{sent === 'request' ? 'Send another request' : 'Ask the admins instead'}</button>
        </>
      ) : loaded && !a ? (
        <>
          <div style={{ marginBottom: 12 }}><span className="badge orange">Not in yet</span></div>
          <h2>Almost in</h2>
          <p>You're signed in{who}, but this key isn't a member of the village yet, and the village doesn't take requests from this app yet.</p>
          {reason && <p className="hint" style={{ fontSize: 13 }}>The village said: {reason}</p>}
          <a className="btn" style={{ textDecoration: 'none', display: 'grid', placeItems: 'center' }} href={`https://${BELFRY.domain}/apply`} target="_blank" rel="noreferrer">Apply to join</a>
          <div style={{ height: 10 }} />
          <button className="btn ghost" onClick={() => { recheckAccess(); void loadAccessInfo() }}>Check again</button>
        </>
      ) : a?.needs_admin ? (
        <>
          <div style={{ marginBottom: 12 }}><span className="badge lime">New village</span></div>
          <h2>No admin yet</h2>
          <p>You're signed in{who}. This village has no admin yet: the first person to claim it becomes its admin and lets the others in.</p>
          {error && <div className="error">{error}</div>}
          <button className="btn" disabled={busy} onClick={() => send('admin', { admin: true, name })}>{busy ? 'Sending…' : 'Become the admin of this village'}</button>
          <p className="hint" style={{ fontSize: 13, marginTop: 10 }}>Whoever runs the server can change admins later.</p>
        </>
      ) : (
        <>
          <div style={{ marginBottom: 12 }}><span className="badge orange">Not in yet</span></div>
          <h2>Almost in</h2>
          <p>You're signed in{who}, but this key isn't a member of the village yet.</p>
          {reason && <p className="hint" style={{ fontSize: 13 }}>The village said: {reason}</p>}
          {error && <div className="error">{error}</div>}
          <form onSubmit={(e) => { e.preventDefault(); void send('code', { claim: code }) }}>
            <label className="field"><span>Invite code</span>
              <input className="input" autoCapitalize="characters" autoComplete="off" spellCheck={false}
                placeholder="OSV-XXXX-XXXX" value={code} onChange={(e) => setCode(e.target.value)} /></label>
            <button className="btn" disabled={busy || normalizeCode(code).length < 6}>Use my code</button>
          </form>
          <div className="section-title" style={{ margin: '24px 0 10px' }}>Or ask to join</div>
          <form onSubmit={(e) => { e.preventDefault(); void send('request', { note, name }) }}>
            <label className="field"><span>Your name</span>
              <input className="input" autoComplete="name" value={name} onChange={(e) => setName(e.target.value)} /></label>
            <label className="field"><span>A note for the admins</span>
              <textarea className="input" rows={3} placeholder="Who you are, why you'd like to join" value={note} onChange={(e) => setNote(e.target.value)} /></label>
            <button className="btn ghost" disabled={busy || !note.trim()}>{busy ? 'Sending…' : 'Request access'}</button>
          </form>
        </>
      )}
      <div className="row" style={{ justifyContent: 'flex-end', marginTop: 18 }}>
        <button type="button" className="link" onClick={onSignOut}>Sign out</button>
      </div>
    </div>
  )
}
