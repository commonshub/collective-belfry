import { useEffect, useState } from 'react'
import { nip19 } from 'nostr-tools'
import { clearSession, driver, loadSession, makeSigner, saveSession, type Session } from '../core/auth'
import { kvGet, kvSet, wipe } from '../core/db'
import { groups } from '../core/model'
import { loadAll, outboxItems, useVersion } from '../core/store'
import { me, start, stop } from '../core/sync'
import { getBackend } from '../config'
import { CalendarList, EventPage } from './Calendar'
import { ChannelInfo } from './ChannelInfo'
import { ChannelList, ChannelView } from './Chat'
import { go, StatusPill, useRoute } from './common'
import { CodeScreen, EmailScreen, MembershipScreen } from './Login'

type Phase = { p: 'loading' } | { p: 'email' } | { p: 'code'; email: string } | { p: 'in'; s: Session } | { p: 'error'; msg: string }

export function App() {
  const [phase, setPhase] = useState<Phase>({ p: 'loading' })
  const [lastEmail, setLastEmail] = useState('')
  const backend = getBackend()

  // Offline first: a stored session opens straight into the app, from cache.
  useEffect(() => {
    void (async () => {
      setLastEmail((await kvGet<string>('lastEmail')) ?? '')
      let s = await loadSession()
      if (s && s.backend !== backend) {
        // switching demo <-> belfry: never mix the two event logs
        await clearSession()
        await wipe()
        s = undefined
      }
      setPhase(s ? { p: 'in', s } : { p: 'email' })
    })()
  }, [])

  const enter = async (s: Session) => {
    await saveSession(s)
    await kvSet('lastEmail', s.email)
    setPhase({ p: 'in', s })
  }

  const signOut = async () => {
    if (outboxItems().length && !confirm(`${outboxItems().length} messages are not sent yet and will be lost. Sign out anyway?`)) return
    stop()
    await clearSession()
    await wipe()
    location.hash = ''
    location.reload()
  }

  if (phase.p === 'loading') return null
  if (phase.p === 'error') return <div className="screen login"><h2>Something went wrong</h2><p>{phase.msg}</p><button className="btn" onClick={signOut}>Sign out</button></div>
  if (phase.p === 'email')
    return <EmailScreen backend={backend} initial={lastEmail} onSent={(email) => setPhase({ p: 'code', email })} />
  if (phase.p === 'code')
    return <CodeScreen backend={backend} email={phase.email} onBack={() => setPhase({ p: 'email' })} onVerified={enter} />
  if (phase.s.membership.status !== 'active')
    return <MembershipScreen session={phase.s} onUpdate={enter} onSignOut={signOut} />
  return <Village session={phase.s} onSignOut={signOut} onError={(msg) => setPhase({ p: 'error', msg })} onSession={enter} />
}

function Village({ session, onSignOut, onError, onSession }: {
  session: Session; onSignOut: () => void; onError: (m: string) => void; onSession: (s: Session) => void
}) {
  const [ready, setReady] = useState(false)
  useEffect(() => {
    let cancelled = false
    void (async () => {
      try {
        await loadAll()
        const signer = await makeSigner(session)
        if (cancelled) return
        await start(signer, { localOnly: session.backend === 'demo' })
        setReady(true)
        // membership can change (revoked, renewed): check quietly when online
        if (session.backend === 'belfry' && navigator.onLine) {
          driver('belfry').refreshMembership(session)
            .then((m) => m.status !== session.membership.status && onSession({ ...session, membership: m }))
            .catch(() => { /* offline or Belfry down: keep the cached status */ })
        }
      } catch (e) {
        onError(e instanceof Error ? e.message : String(e))
      }
    })()
    return () => { cancelled = true }
  }, [session])

  const route = useRoute()
  useVersion()
  if (!ready) return null

  const [section = 'c', a, b] = route
  let view
  if (section === 'channels') view = <ChannelList />
  else if (section === 'cal' && a) view = <EventPage coord={a} />
  else if (section === 'cal') view = <CalendarList />
  else if (section === 'me') view = <Me session={session} onSignOut={onSignOut} />
  else {
    const id = a ?? groups()[0]?.id ?? 'general'
    view = b === 'info' ? <ChannelInfo id={id} /> : <ChannelView id={id} />
  }
  const tab = section === 'cal' ? 'cal' : section === 'me' ? 'me' : 'chat'
  // the composer sits above the tabs; hide tabs while inside a conversation's sub-pages
  return (
    <div className="screen">
      <div style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}>{view}</div>
      <nav className="tabs">
        <button className={tab === 'chat' ? 'on' : ''} onClick={() => go(section === 'c' ? 'channels' : 'c/' + (lastChannel() ?? ''))}><span>💬</span>Chat</button>
        <button className={tab === 'cal' ? 'on' : ''} onClick={() => go('cal')}><span>📅</span>Calendar</button>
        <button className={tab === 'me' ? 'on' : ''} onClick={() => go('me')}><span>🙂</span>Me</button>
      </nav>
    </div>
  )
}

function lastChannel() {
  const m = /#\/c\/([^/]+)/.exec(sessionStorage.getItem('osv-last-channel') ?? '')
  return m?.[1]
}
addEventListener('hashchange', () => {
  if (location.hash.startsWith('#/c/')) try { sessionStorage.setItem('osv-last-channel', location.hash) } catch { /* ignore */ }
})

function Me({ session, onSignOut }: { session: Session; onSignOut: () => void }) {
  const m = session.membership
  const npub = me() ? nip19.npubEncode(me()) : ''
  return (
    <div className="screen">
      <div className="topbar"><h1 style={{ paddingLeft: 8 }}>Me</h1><StatusPill /></div>
      <div className="scroll pad">
        <div className="card">
          <div className="hint">Signed in as</div>
          <div style={{ fontWeight: 600 }}>{session.email}</div>
        </div>
        <div className="card">
          <div className="row" style={{ justifyContent: 'space-between' }}>
            <div>
              <div className="hint">Pass</div>
              <div style={{ fontWeight: 600 }}>{m.pass ?? 'Open Source Village 2027'}</div>
              {m.member && <div className="hint">{m.member}</div>}
            </div>
            <span className="badge lime">Active</span>
          </div>
          <a className="btn ghost" style={{ marginTop: 12, textDecoration: 'none' }} href="https://pass.belfry.opensourcevillage.org/me" target="_blank" rel="noreferrer">Show my pass at the door</a>
        </div>
        <div className="card">
          <div className="hint">Your village key (Nostr)</div>
          <div style={{ fontFamily: 'ui-monospace, monospace', fontSize: 13, wordBreak: 'break-all' }}>{npub}</div>
          <div className="hint" style={{ fontSize: 13, marginTop: 6 }}>
            {session.backend === 'demo' ? 'Demo key, kept on this phone only.' : 'Belfry keeps this key safe for you; this app asks it to sign.'}
          </div>
        </div>
        <button className="btn ghost" onClick={onSignOut}>Sign out</button>
        <p className="hint" style={{ marginTop: 16, fontSize: 13 }}>
          Backend: {session.backend}. {session.backend === 'demo' ? <a href="?backend=belfry">Switch to Belfry</a> : <a href="?backend=demo">Switch to demo</a>} (signs you out).
        </p>
      </div>
    </div>
  )
}
