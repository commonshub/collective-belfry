import { generateSecretKey, getPublicKey } from 'nostr-tools'
import { useEffect, useState } from 'react'
import { closeDrawer, useDrawer } from './drawer'
import { Phone } from './Phone'
import { SearchView } from './Search'
import { belfrySignOut, endSession, forgetDevice, clearSession, demoSession, finishSignIn, pendingInvite, setPendingInvite, takeInviteFromUrl, loadSession, makeSigner, saveSession, setGuestInfo, type Session } from '../core/auth'
import { accessState, isAdmin, loadAccessInfo, myGrant, onAccessInfo } from '../core/access'
import { wipe } from '../core/db'
import { groups, profiles } from '../core/model'
import { addEvents, loadAll, outboxItems, status, useVersion } from '../core/store'
import { joinWithAccount, me, setAvatar, start, stop } from '../core/sync'
import { BELFRY, NAME, PLACE, TEST_RELAYS, THEME, getBackend } from '../config'
import { Admin } from './Admin'
import { BrandingScreen } from './Branding'
import { isBelfryAdmin } from '../core/people'
import { EventsScreen } from './Calendar'
import { EventJoinScreen, EventScreen, EventWaitScreen } from './Events'
import { demoApply, demoGuest, eventLinkFromUrl, pendingApplication, setPendingApplication } from '../core/events'
import { eventOfGroup } from '../core/model'
import { ChannelInfo } from './ChannelInfo'
import { Connections } from './Connections'
import { ChannelList, ChannelView, ThreadView } from './Chat'
import { MessagesList, MessageThread } from './Messages'
import { demoMail, groupsKey, setMyAddress, startDms } from '../core/dm'
import { demoInboxMail, demoMailPost } from './MailPost'
import { Avatar, back, go, useRoute, useWide, WIDE3 } from './common'
import { openProfile, ProfileView } from './Profile'
import { dropCode, JoinScreen, MembershipScreen, WelcomeScreen } from './Login'
import { MentionsScreen } from './Mentions'

type Phase = { p: 'loading' } | { p: 'out'; error?: string } | { p: 'in'; s: Session }

export function App() {
  const [phase, setPhase] = useState<Phase>({ p: 'loading' })
  const backend = getBackend()
  const [signInFirst, setSignInFirst] = useState(false)

  useEffect(() => {
    void (async () => {
      takeInviteFromUrl() // a guest link (#/join/<token>): kept until it is used
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
    // this device's session ends in the signer too (its key can sign nothing any more), while
    // the session can still sign the request; then nothing of it stays on the device, and the
    // next sign-in starts from a new key
    if (s?.backend === 'belfry') await endSession(s)
    setGuestInfo(null)
    stop()
    await clearSession()
    await wipe()
    forgetDevice()
    location.hash = ''
    if (s?.backend === 'belfry') return belfrySignOut(s)
    location.reload()
  }

  if (phase.p === 'loading') return null
  if (phase.p === 'out' && pendingInvite() && !signInFirst && !phase.error)
    return <JoinScreen token={pendingInvite()} backend={backend} onSignIn={() => setSignInFirst(true)}
      onJoined={async (s) => { await wipe(); await enter(s) }}
      onDemo={(name) => { setPendingInvite(''); void enter(demoSession(`${name.toLowerCase().replace(/[^a-z0-9]+/g, '.') || 'guest'}@example.org`)) }} />
  // an event's URL opened in the chat by someone with no account: the event, RSVP or apply
  if (phase.p === 'out' && eventLinkFromUrl() && !signInFirst && !phase.error)
    return <EventJoinScreen link={eventLinkFromUrl()} backend={backend} onSignIn={() => { setPendingEvent(eventLinkFromUrl()); setSignInFirst(true) }}
      onJoined={async (s) => { await wipe(); await enter(s) }}
      onDemo={(name) => void enter(demoSession(`${name.toLowerCase().replace(/[^a-z0-9]+/g, '.') || 'guest'}@example.org`))} />
  if (phase.p === 'out') return <WelcomeScreen invited={!!pendingInvite()} backend={backend} error={phase.error} onDemo={(email) => enter(demoSession(email))} />
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
        await start(signer, { localOnly: session.backend === 'demo' && !TEST_RELAYS })
        // direct messages and e-mail (dm.ts); my address is my name@domain
        setMyAddress(session.membership.nip05 ?? profiles().get(signer.pubkey)?.nip05 ?? (session.backend === 'demo' ? `${(session.email ?? 'me').split('@')[0]}@${BELFRY.domain}` : undefined))
        startDms()
        void groupsKey()
        if (session.backend === 'demo') Object.assign(window, { demoMail, demoMailPost, demoInboxMail, demoGuestJoin, demoPeople, demoApply, demoGuest })
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
  const wide = useWide()
  const three = useWide(WIDE3)
  const drawer = useDrawer()
  useEffect(() => { if (wide) closeDrawer() }, [wide])
  useVersion()
  useEffect(() => { if (status.access === 'ok') dropCode() }, [status.access]) // in: a saved code is spent
  // a guest link opened while signed in (or kept while signing in): this account gets the channel
  const [joined, setJoined] = useState('')
  useEffect(() => {
    if (!ready) return
    const token = takeInviteFromUrl() || pendingInvite()
    if (!token) return
    setPendingInvite('')
    if (session.backend === 'demo') { setJoined('You joined # general'); go('c/general'); return }
    joinWithAccount(token).then(
      ({ channel, already }) => { go('c/' + encodeURIComponent(channel)); setJoined(already ? '' : `You joined # ${groups().find((x) => x.id === channel)?.name ?? channel}`) },
      (e) => setJoined(e instanceof Error ? e.message : String(e)),
    )
  }, [ready, route.join('/')])
  useEffect(() => { if (!joined) return; const t = setTimeout(() => setJoined(''), 6000); return () => clearTimeout(t) }, [joined])
  // signed in from an event's page: back to it; an application that got a place: done waiting
  useEffect(() => {
    if (!ready) return
    const link = pendingEvent()
    if (link) { setPendingEvent(''); go('events/' + encodeURIComponent(link)) }
  }, [ready])
  useEffect(() => { if (status.access === 'ok' && pendingApplication()) setPendingApplication(null) }, [status.access])
  const toast = joined ? <div className="toast" role="status"><span>{joined}</span><button onClick={() => setJoined('')}>OK</button></div> : null
  if (error)
    return (
      <div className="screen login">
        <h2>{THEME === 'belfry' ? "Can't reach your key" : "Can't reach your village key"}</h2>
        <p>{error}</p>
        <button className="btn" onClick={() => location.reload()}>Try again</button>
        <div style={{ height: 12 }} />
        <button className="btn ghost" onClick={onSignOut}>Sign out</button>
      </div>
    )
  if (!ready) return <div className="screen login"><p>Opening {PLACE}…</p></div>
  // someone who applied to an event before having anything here: the relay doesn't know them yet
  if (status.access === 'denied' && pendingApplication())
    return <EventWaitScreen title={pendingApplication()!.title} onSignOut={onSignOut} />
  if (status.access === 'denied')
    return <MembershipScreen session={session} reason={status.accessReason} onSignOut={onSignOut} />

  const [section = 'c', a, b, c, d] = route
  let view
  let pane // a large screen: an open thread in a third column, its channel stays in the middle
  if (section === 'channels' && !wide) view = <ChannelList />
  else if (section === 'dm' && a) view = <MessageThread conv={a} />
  else if (section === 'dm') view = <MessagesList />
  else if (section === 'mentions') view = <MentionsScreen />
  else if (section === 'events' && a) view = <EventScreen key={a} link={a} />
  else if (section === 'events' || section === 'cal' && !a) view = <EventsScreen />
  else if (section === 'cal' && a) view = <EventScreen key={a} coord={a} />
  else if (section === 'me' && a === 'connections') view = <Connections session={session} />
  else if (section === 'me' && a === 'branding' && THEME === 'belfry') view = <BrandingScreen />
  else if (section === 'me') view = <Me session={session} onSignOut={onSignOut} />
  else if (section === 'admin') view = <Admin sub={a} id={b} />
  else if (section === 'search') view = <SearchView channel={a} />
  else if (section === 'u') view = <ProfileView id={a} />
  else {
    const id = a ?? lastChannel() ?? groups()[0]?.id ?? 'general'
    const evOf = eventOfGroup(id) // an event's chat opens as the event
    if (evOf && (!b || b === 'm' || (b === 'thread' && wide && three && c))) {
      view = <EventScreen key={id} coord={evOf.coord} activeThread={b === 'thread' ? c : undefined} />
      if (b === 'thread' && c) pane = <ThreadView key={c} groupId={id} rootId={c} pane replying={d === 'reply'} />
    } else if (wide && three && b === 'thread' && c) {
      view = <ChannelView id={id} activeThread={c} />
      pane = <ThreadView key={c} groupId={id} rootId={c} pane replying={d === 'reply'} />
    } else view = b === 'info' ? <ChannelInfo id={id} initial={c === 'members' ? 'members' : 'about'} /> : b === 'events' ? <EventsScreen channel={id} /> : b === 'thread' && c ? <ThreadView groupId={id} rootId={c} replying={d === 'reply'} /> : <ChannelView id={id} topic={b === 't' ? c : undefined} focus={b === 'm' ? c : undefined} />
  }
  // phone: no tab bar: channels in a drawer behind ☰ (☰ again closes it), Me behind your avatar (top right), events under each
  // channel's name. Laptop: the channel list stays on the left, you at its bottom (like WhatsApp).
  const current = section === 'c' ? (a ?? lastChannel() ?? groups()[0]?.id ?? 'general') : section === 'events' ? a : undefined
  if (!wide) return <><Phone current={current} section={section} open={drawer}>{view}</Phone>{toast}</>
  return (
    <div className="layout wide">
      <aside className="side" aria-label="Channels"><ChannelList side current={current} section={section} /></aside>
      <main className="main screen">{view}</main>
      {pane && <aside className="thread-pane" aria-label="Thread">{pane}</aside>}
      {toast}
    </div>
  )
}

const EVENT_KEY = 'belfry-event-open'
const pendingEvent = () => { try { return localStorage.getItem(EVENT_KEY) ?? '' } catch { return '' } }
const setPendingEvent = (link: string) => { try { if (link) localStorage.setItem(EVENT_KEY, link); else localStorage.removeItem(EVENT_KEY) } catch { /* private mode */ } }

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
          <div style={{ minWidth: 0, flex: 1 }}>
            <div style={{ fontWeight: 650 }}>{p?.name ?? session.email ?? (THEME === 'belfry' ? 'Member' : 'Villager')}</div>
            <div className="hint" style={{ fontSize: 14, overflowWrap: 'anywhere' }}>{m.nip05 ?? p?.nip05 ?? ''}</div>
            {avatarError && <div className="error" style={{ margin: '4px 0 0' }}>{avatarError}</div>}
          </div>
          <button className="btn small ghost" onClick={() => openProfile(me())}>Your profile</button>
        </div>
        {THEME !== 'belfry' && (
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
        )}
        {isAdmin() && (
          <button className="card row" style={{ width: '100%', textAlign: 'left', justifyContent: 'space-between' }} onClick={() => go('admin')}>
            <div>
              <div style={{ fontWeight: 600 }}>Admin</div>
              <div className="hint" style={{ fontSize: 14 }}>Requests, members and invite codes</div>
            </div>
            {waiting > 0 && <span className="badge orange">{waiting} waiting</span>}
          </button>
        )}
        {THEME === 'belfry' && isBelfryAdmin(me()) && (
          <button className="card row" style={{ width: '100%', textAlign: 'left', justifyContent: 'space-between' }} onClick={() => go('me/branding')}>
            <div>
              <div style={{ fontWeight: 600 }}>Branding</div>
              <div className="hint" style={{ fontSize: 14 }}>Logo, cover image and colours of {NAME}</div>
            </div>
            <span className="hint" aria-hidden="true">›</span>
          </button>
        )}
        <button className="card row" style={{ width: '100%', textAlign: 'left', justifyContent: 'space-between' }} onClick={() => go('me/connections')}>
          <div>
            <div style={{ fontWeight: 600 }}>Connections</div>
            <div className="hint" style={{ fontSize: 14 }}>Your key and the apps signed in as you</div>
          </div>
          <span className="hint" aria-hidden="true">›</span>
        </button>
        <button className="link sign-out" onClick={onSignOut}>Sign out</button>
      </div>
    </div>
  )
}

/** Demo only: someone joins CHANNEL as a guest invited by you (what Belfry publishes, guests.py). */
function demoGuestJoin(channel: string, name: string) {
  const hex = (n: number) => [...crypto.getRandomValues(new Uint8Array(n))].map((b) => b.toString(16).padStart(2, '0')).join('')
  addEvents([{ id: hex(32), pubkey: hex(32), sig: hex(64), kind: 9000, content: '', created_at: Math.floor(Date.now() / 1000),
    tags: [['h', channel], ['p', hex(32), 'member'], ['invited-by', me()], ['name', name], ['guest', '']] }])
}

/** Demo only: three members of CHANNEL with a profile, tags and a city (people.ts). */
function demoPeople(channel = 'general') {
  const hex = (n: number) => [...crypto.getRandomValues(new Uint8Array(n))].map((b) => b.toString(16).padStart(2, '0')).join('')
  const now = Math.floor(Date.now() / 1000)
  const people = [
    { name: 'Ana', tags: ['facilitation', 'retreats'], place: { id: 'gn:2800866', name: 'Brussels', country: 'BE', lat: 50.85, lon: 4.35 } },
    { name: 'Ben', tags: ['finance', 'facilitation'], place: { id: 'gn:2800866', name: 'Brussels', country: 'BE', lat: 50.85, lon: 4.35 } },
    { name: 'Rua', tags: ['software'], place: { id: 'gn:2179537', name: 'Wellington', country: 'NZ', lat: -41.29, lon: 174.78 } },
  ]
  const evs = people.flatMap((p) => {
    const pk = getPublicKey(generateSecretKey()) // a real key: a direct message to them can be encrypted
    return [
      { id: hex(32), pubkey: pk, sig: hex(64), kind: 0, created_at: now, tags: [], content: JSON.stringify({ name: p.name, about: `Hi, I'm ${p.name}.` }) },
      { id: hex(32), pubkey: pk, sig: hex(64), kind: 30078, created_at: now, tags: [['d', 'belfry:profile']], content: JSON.stringify({ tags: p.tags, share: 'city', place: p.place }) },
      { id: hex(32), pubkey: hex(32), sig: hex(64), kind: 9000, created_at: now, content: '', tags: [['h', channel], ['p', pk, 'member']] },
      { id: hex(32), pubkey: pk, sig: hex(64), kind: 9, created_at: now, content: `Hello from ${p.place.name}`, tags: [['h', channel]] },
    ]
  })
  // an agent I invited as a guest of this channel (its own key)
  const bot = getPublicKey(generateSecretKey())
  evs.push(
    { id: hex(32), pubkey: bot, sig: hex(64), kind: 0, created_at: now, tags: [], content: JSON.stringify({ name: 'Scout', about: 'An agent that keeps notes.' }) },
    { id: hex(32), pubkey: hex(32), sig: hex(64), kind: 9000, created_at: now, content: '', tags: [['h', channel], ['p', bot, 'member'], ['invited-by', me()], ['name', 'Scout'], ['agent', '1']] },
    { id: hex(32), pubkey: bot, sig: hex(64), kind: 9, created_at: now, content: 'Scout here, taking notes.', tags: [['h', channel]] },
  )
  addEvents(evs)
}
