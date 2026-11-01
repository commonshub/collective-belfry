import { useEffect, useState } from 'react'
import { nip19 } from 'nostr-tools'
import { belfrySignOut, clearSession, demoSession, fetchMembership, finishSignIn, loadSession, makeSigner, saveSession, type Session } from '../core/auth'
import { wipe } from '../core/db'
import { groups, profiles } from '../core/model'
import { loadAll, outboxItems, status, useVersion } from '../core/store'
import { me, start, stop } from '../core/sync'
import { BELFRY, getBackend } from '../config'
import { CalendarList, EventPage } from './Calendar'
import { ChannelInfo } from './ChannelInfo'
import { ChannelList, ChannelView } from './Chat'
import { Avatar, go, StatusPill, useRoute } from './common'
import { MembershipScreen, WelcomeScreen } from './Login'

type Phase = { p: 'loading' } | { p: 'out'; error?: string } | { p: 'in'; s: Session }

export function App() {
  const [phase, setPhase] = useState<Phase>({ p: 'loading' })
  const backend = getBackend()

  useEffect(() => {
    void (async () => {
      // back from the village sign-in page?
      try {
        const fresh = await finishSignIn()
        if (fresh) {
          await wipe() // a new account: don't show the previous one's cache
          await saveSession(fresh)
          return setPhase({ p: 'in', s: fresh })
        }
      } catch (e) {
        return setPhase({ p: 'out', error: e instanceof Error ? e.message : String(e) })
      }
      // Offline first: a stored session opens straight into the app, from cache.
      let s = await loadSession()
      if (s && s.backend !== backend) {
        // switching demo <-> belfry: never mix the two event logs
        await clearSession()
        await wipe()
        s = undefined
      }
      setPhase(s ? { p: 'in', s } : { p: 'out' })
    })()
  }, [])

  const enter = async (s: Session) => {
    await saveSession(s)
    setPhase({ p: 'in', s })
  }

  const signOut = async () => {
    if (outboxItems().length && !confirm(`${outboxItems().length} messages are not sent yet and will be lost. Sign out anyway?`)) return
    const s = phase.p === 'in' ? phase.s : undefined
    stop()
    await clearSession()
    await wipe()
    location.hash = ''
    if (s?.backend === 'belfry') return belfrySignOut(s)
    location.reload()
  }

  if (phase.p === 'loading') return null
  if (phase.p === 'out') return <WelcomeScreen backend={backend} error={phase.error} onDemo={(email) => enter(demoSession(email))} />
  return <Village key={phase.s.bunkerUrl ?? phase.s.email} session={phase.s} onSignOut={signOut} onSession={enter} />
}

function Village({ session, onSignOut, onSession }: { session: Session; onSignOut: () => void; onSession: (s: Session) => void }) {
  const [ready, setReady] = useState(false)
  const [error, setError] = useState('')
  useEffect(() => {
    let cancelled = false
    void (async () => {
      try {
        await loadAll()
        const signer = await makeSigner(session)
        if (cancelled) return
        await start(signer, { localOnly: session.backend === 'demo' })
        setReady(true)
        // where the key stands; quietly, the cached answer is fine offline
        if (session.backend === 'belfry' && navigator.onLine) {
          const m = await fetchMembership(signer)
          if (m && !cancelled && (m.status !== session.membership.status || m.pass !== session.membership.pass)) onSession({ ...session, membership: m })
        }
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e))
      }
    })()
    return () => { cancelled = true; stop() }
  }, [session.bunkerUrl, session.demoSecret])

  const route = useRoute()
  useVersion()
  if (error)
    return (
      <div className="screen login">
        <h2>Can't reach your village key</h2>
        <p>{error}</p>
        <button className="btn" onClick={() => location.reload()}>Try again</button>
        <div style={{ height: 12 }} />
        <button className="btn ghost" onClick={onSignOut}>Sign out</button>
      </div>
    )
  if (!ready) return <div className="screen login"><p>Opening the village…</p></div>
  if (session.membership.status === 'pending' || status.access === 'denied')
    return <MembershipScreen session={session} reason={status.accessReason} onRetry={() => location.reload()} onSignOut={onSignOut} />

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
  const p = profiles().get(me())
  return (
    <div className="screen">
      <div className="topbar"><h1 style={{ paddingLeft: 8 }}>Me</h1><StatusPill /></div>
      <div className="scroll pad">
        <div className="card row" style={{ gap: 12 }}>
          <Avatar pubkey={me()} size={48} />
          <div style={{ minWidth: 0 }}>
            <div style={{ fontWeight: 650 }}>{p?.name ?? session.email ?? 'Villager'}</div>
            <div className="hint" style={{ fontSize: 14, overflowWrap: 'anywhere' }}>{m.nip05 ?? p?.nip05 ?? ''}</div>
          </div>
        </div>
        <div className="card">
          <div className="row" style={{ justifyContent: 'space-between' }}>
            <div>
              <div className="hint">Pass</div>
              <div style={{ fontWeight: 600 }}>{m.pass ?? 'Open Source Village'}</div>
            </div>
            <span className="badge lime">{status.access === 'ok' || m.status === 'active' ? 'Active' : 'Checking…'}</span>
          </div>
          <a className="btn ghost" style={{ marginTop: 12, textDecoration: 'none' }} href={`${BELFRY.pass}/me`} target="_blank" rel="noreferrer">Show my pass at the door</a>
        </div>
        <div className="card">
          <div className="hint">Your village key (Nostr)</div>
          <div style={{ fontFamily: 'ui-monospace, monospace', fontSize: 13, wordBreak: 'break-all' }}>{npub}</div>
          <div className="hint" style={{ fontSize: 13, marginTop: 6 }}>
            {session.backend === 'demo' ? 'Demo key, kept on this device only.' : 'The village keeps this key safe for you; this app asks it to sign.'}
          </div>
        </div>
        <button className="btn ghost" onClick={onSignOut}>Sign out</button>
      </div>
    </div>
  )
}
