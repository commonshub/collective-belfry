import { useEffect, useState } from 'react'
import { nip19 } from 'nostr-tools'
import { belfrySignOut, clearSession, demoSession, finishSignIn, loadSession, makeSigner, saveSession, type Session } from '../core/auth'
import { accessState, isAdmin, loadAccessInfo, myGrant, onAccessInfo } from '../core/access'
import { wipe } from '../core/db'
import { groups, profiles } from '../core/model'
import { loadAll, outboxItems, status, useVersion } from '../core/store'
import { me, setAvatar, start, stop } from '../core/sync'
import { BELFRY, NAME, getBackend } from '../config'
import { Admin } from './Admin'
import { CalendarList, EventPage } from './Calendar'
import { ChannelInfo } from './ChannelInfo'
import { Connections } from './Connections'
import { ChannelList, ChannelView } from './Chat'
import { MessagesList, MessageThread } from './Messages'
import { demoMail, setMyAddress, startDms } from '../core/dm'
import { Avatar, back, go, useRoute } from './common'
import { dropCode, MembershipScreen, WelcomeScreen } from './Login'

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
  return <Village key={phase.s.bunkerUrl ?? phase.s.email} session={phase.s} onSignOut={signOut} />
}

function Village({ session, onSignOut }: { session: Session; onSignOut: () => void }) {
  const [ready, setReady] = useState(false)
  const [error, setError] = useState('')
  const [, setTick] = useState(0)
  useEffect(() => onAccessInfo(() => setTick((t) => t + 1)), [])
  useEffect(() => {
    let cancelled = false
    void (async () => {
      try {
        await loadAll()
        const signer = await makeSigner(session)
        if (cancelled) return
        await start(signer, { localOnly: session.backend === 'demo' })
        // direct messages and e-mail (dm.ts); my address is my name@domain
        setMyAddress(session.membership.nip05 ?? profiles().get(signer.pubkey)?.nip05 ?? (session.backend === 'demo' ? `${(session.email ?? 'me').split('@')[0]}@${BELFRY.domain}` : undefined))
        startDms()
        if (session.backend === 'demo') (window as unknown as { demoMail: typeof demoMail }).demoMail = demoMail
        setReady(true)
        // the village's read-only state (admins, whether it needs a first admin)
        if (session.backend === 'belfry') void loadAccessInfo()
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e))
      }
    })()
    return () => { cancelled = true; stop() }
  }, [session.bunkerUrl, session.demoSecret])

  const route = useRoute()
  useVersion()
  useEffect(() => { if (status.access === 'ok') dropCode() }, [status.access]) // in: a saved code is spent
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
  if (status.access === 'denied')
    return <MembershipScreen session={session} reason={status.accessReason} onSignOut={onSignOut} />

  const [section = 'c', a, b, c] = route
  let view
  if (section === 'channels') view = <ChannelList />
  else if (section === 'dm' && a) view = <MessageThread conv={a} />
  else if (section === 'dm') view = <MessagesList />
  else if (section === 'cal' && a) view = <EventPage coord={a} />
  else if (section === 'cal') view = <CalendarList />
  else if (section === 'me' && a === 'connections') view = <Connections session={session} />
  else if (section === 'me') view = <Me session={session} onSignOut={onSignOut} />
  else if (section === 'admin') view = <Admin sub={a} id={b} />
  else {
    const id = a ?? lastChannel() ?? groups()[0]?.id ?? 'general'
    view = b === 'info' ? <ChannelInfo id={id} initial={c === 'members' ? 'members' : 'about'} /> : b === 'events' ? <CalendarList channel={id} /> : <ChannelView id={id} topic={b === 't' ? c : undefined} />
  }
  // no tab bar: channels behind ☰, Me behind your avatar (top right), events under each channel's name
  return <div className="screen">{view}</div>
}

function lastChannel() {
  const m = /#\/c\/([^/]+)/.exec(sessionStorage.getItem('osv-last-channel') ?? '')
  return m ? decodeURIComponent(m[1]) : undefined
}
addEventListener('hashchange', () => {
  if (location.hash.startsWith('#/c/')) try { sessionStorage.setItem('osv-last-channel', location.hash) } catch { /* ignore */ }
})

function Me({ session, onSignOut }: { session: Session; onSignOut: () => void }) {
  const m = session.membership
  const g = myGrant()
  const waiting = isAdmin() ? accessState().pending.length : 0
  const p = profiles().get(me())
  const [avatarBusy, setAvatarBusy] = useState(false)
  const [avatarError, setAvatarError] = useState('')
  return (
    <div className="screen">
      <div className="topbar">
        <button className="icon-btn" aria-label="Back" onClick={() => back('c')}>‹</button>
        <h1>Me</h1>
      </div>
      <div className="scroll pad">
        <div className="card row" style={{ gap: 12 }}>
          <label className="avatar-edit" aria-label="Change your picture">
            <Avatar pubkey={me()} size={56} />
            <span className="avatar-badge" aria-hidden="true">{avatarBusy ? '…' : '✎'}</span>
            <input type="file" accept="image/*" hidden disabled={avatarBusy} onChange={async (e) => {
              const f = e.target.files?.[0]
              e.target.value = ''
              if (!f) return
              setAvatarBusy(true); setAvatarError('')
              try { await setAvatar(f) } catch (x) { setAvatarError(x instanceof Error ? x.message : String(x)) } finally { setAvatarBusy(false) }
            }} />
          </label>
          <div style={{ minWidth: 0 }}>
            <div style={{ fontWeight: 650 }}>{p?.name ?? session.email ?? 'Villager'}</div>
            <div className="hint" style={{ fontSize: 14, overflowWrap: 'anywhere' }}>{m.nip05 ?? p?.nip05 ?? ''}</div>
            {avatarError && <div className="error" style={{ margin: '4px 0 0' }}>{avatarError}</div>}
          </div>
        </div>
        <div className="card">
          <div className="row" style={{ justifyContent: 'space-between' }}>
            <div>
              <div className="hint">{g ? 'Access' : 'Pass'}</div>
              <div style={{ fontWeight: 600 }}>
                {g ? `${g.role}${g.until ? ' · until ' + new Date(g.until * 1000).toLocaleDateString([], { day: 'numeric', month: 'short', year: 'numeric' }) : ''}` : m.pass ?? NAME}
              </div>
            </div>
            <span className="badge lime">{status.access === 'ok' || m.status === 'active' ? 'Active' : 'Checking…'}</span>
          </div>
          {!g && <a className="btn ghost" style={{ marginTop: 12, textDecoration: 'none' }} href={`${BELFRY.pass}/me`} target="_blank" rel="noreferrer">Show my pass at the door</a>}
        </div>
        {isAdmin() && (
          <button className="card row" style={{ width: '100%', textAlign: 'left', justifyContent: 'space-between' }} onClick={() => go('admin')}>
            <div>
              <div style={{ fontWeight: 600 }}>Admin</div>
              <div className="hint" style={{ fontSize: 14 }}>Requests, members and invite codes</div>
            </div>
            {waiting > 0 && <span className="badge orange">{waiting} waiting</span>}
          </button>
        )}
        <button className="card row" style={{ width: '100%', textAlign: 'left', justifyContent: 'space-between' }} onClick={() => go('me/connections')}>
          <div>
            <div style={{ fontWeight: 600 }}>Connections</div>
            <div className="hint" style={{ fontSize: 14 }}>Your key and the apps signed in as you</div>
          </div>
          <span className="hint" aria-hidden="true">›</span>
        </button>
        <button className="btn ghost" onClick={onSignOut}>Sign out</button>
      </div>
    </div>
  )
}
