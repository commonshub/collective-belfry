import { generateSecretKey, getPublicKey } from 'nostr-tools'
import { useEffect, useState } from 'react'
import { closeDrawer, useDrawer } from './drawer'
import { Phone } from './Phone'
import { SearchView } from './Search'
import { belfrySignOut, endSession, forgetDevice, clearSession, demoSession, finishSignIn, openAfterSignIn, pendingInvite, setOpenAfterSignIn, setPendingInvite, startSignIn, takeInviteFromUrl, loadSession, makeSigner, saveSession, setGuestInfo, type Session } from '../core/auth'
import { closeJoin, openingNow, reading, setOpening, useGuestMode } from '../core/guestmode'
import { GuestJoinHost, OpeningScreen } from './GuestJoin'
import { accessState, isAdmin, loadAccessInfo, myGrant, onAccessInfo } from '../core/access'
import { wipe } from '../core/db'
import { groups, profiles } from '../core/model'
import { addEvents, loadAll, outboxItems, status, useVersion } from '../core/store'
import { isLocalOnly, joinWithAccount, me, setAvatar, start, stop } from '../core/sync'
import { BELFRY, NAME, PLACE, TEST_RELAYS, THEME, getBackend } from '../config'
import { Admin } from './Admin'
import { BrandingScreen } from './Branding'
import { SettingsScreen } from './Settings'
import { RequestsScreen } from './Requests'
import { GroupPage } from './GroupPage'
import { demoRequests, startRequests } from '../core/requests'
import { demoRoles, loadRoles } from '../core/roles'
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
import { Avatar, back, devMode, go, setDevMode, useRoute, useWide, WIDE3 } from './common'
import { openProfile, ProfileView } from './Profile'
import { dropCode, OpenInvite, MembershipScreen, WelcomeScreen } from './Login'
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
      // a guest link and nobody signed in: the loading screen from here until its channel is drawn
      if (!s && pendingInvite()) setOpening({})
      setPhase(s ? { p: 'in', s } : { p: 'out' })
    })()
  }, [])
  useGuestMode()

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

  // An address proven in guest mode had an account already (GuestJoin.tsx): this guest session
  // goes, and the person signs in; the channel opens after.
  const signInInstead = async (channel: string, moved: boolean) => {
    const s = phase.p === 'in' ? phase.s : undefined
    if (s?.backend === 'demo') { setGuestInfo(null); closeJoin(); await clearSession(); await wipe(); location.hash = ''; return location.reload() }
    if (s && !moved) await endSession(s) // (moved: the belfry closed the guest account itself)
    stop()
    await clearSession()
    await wipe()
    forgetDevice()
    setOpenAfterSignIn(channel)
    await startSignIn()
    await new Promise(() => { /* the page is leaving: the button stays busy */ })
  }

  // The loading screen of a guest link stays mounted from the link to the channel (the same
  // element over whatever comes under it), so nothing blinks in between.
  const op = openingNow()
  const veil = op ? <OpeningScreen name={op.name} /> : null
  if (phase.p === 'loading') return veil
  if (phase.p === 'out' && pendingInvite() && !signInFirst && !phase.error)
    return <>{veil}<OpenInvite token={pendingInvite()} backend={backend} onSignIn={() => setSignInFirst(true)}
      onJoined={async (s) => { await wipe(); await enter(s) }} /></>
  // an event's URL opened in the chat by someone with no account: the event, RSVP or apply
  if (phase.p === 'out' && eventLinkFromUrl() && !signInFirst && !phase.error)
    return <EventJoinScreen link={eventLinkFromUrl()} backend={backend} onSignIn={() => { setPendingEvent(eventLinkFromUrl()); setSignInFirst(true) }}
      onJoined={async (s) => { await wipe(); await enter(s) }}
      onDemo={(name) => void enter(demoSession(`${name.toLowerCase().replace(/[^a-z0-9]+/g, '.') || 'guest'}@example.org`))} />
  if (phase.p === 'out') return <WelcomeScreen invited={!!pendingInvite()} backend={backend} error={phase.error} onDemo={(email) => enter(demoSession(email))} />
  return <>{veil}<Village key={phase.s.bunkerUrl ?? phase.s.email} session={phase.s} onSignOut={signOut} onSignIn={signInInstead} /></>
}

function Village({ session, onSignOut, onSignIn }: { session: Session; onSignOut: () => void; onSignIn: (channel: string, moved: boolean) => Promise<void> }) {
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
        if (session.backend === 'demo') Object.assign(window, { demoMail, demoMailPost, demoInboxMail, demoGuestJoin, demoGuestReading, demoPeople, demoApply, demoGuest, demoRoles, demoAgentPost, demoRequests, demoGroup })
        setReady(true)
        // the village's read-only state (admins, whether it needs a first admin)
        if (session.backend === 'belfry') void loadAccessInfo()
        // members' roles (roles.ts): shown next to names; not asked in guest mode
        if (THEME === 'belfry' && !reading()) void loadRoles()
        // requests waiting for me (Requests.tsx) and the Membership settings
        if (THEME === 'belfry' && !reading()) startRequests()
      } catch (e) {
        setOpening(null)
        setError(e instanceof Error ? e.message : String(e))
      }
    })()
    return () => { cancelled = true; stop() }
  }, [session.bunkerUrl, session.demoSecret])
  // A guest link just used: the loading screen stays until the channel can be drawn in full (the
  // relay let this key in and sent what it has, or said no), at most 10 s more.
  useGuestMode()
  const opening = !!openingNow()
  useEffect(() => {
    if (!opening || !ready) return
    if (isLocalOnly() || status.access !== 'unknown') return setOpening(null)
    const t = setTimeout(() => setOpening(null), 10_000)
    return () => clearTimeout(t)
  }, [opening, ready, status.access])

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
    // signed in after guest mode said "you have an account": the channel they were reading
    const ch = openAfterSignIn()
    if (ch) { setOpenAfterSignIn(''); go('c/' + encodeURIComponent(ch)) }
  }, [ready])
  useEffect(() => { if (status.access === 'ok' && pendingApplication()) setPendingApplication(null) }, [status.access])
  const toast = <>{joined ? <div className="toast" role="status"><span>{joined}</span><button onClick={() => setJoined('')}>OK</button></div> : null}<GuestJoinHost onSignIn={onSignIn} /></>
  if (opening) return null // under the guest link's loading screen: nothing else is drawn
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
  else if (section === 'settings' && THEME === 'belfry') view = <SettingsScreen section={a} id={b} />
  else if (section === 'requests') view = <RequestsScreen />
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
    } else view = b === 'group' ? <GroupPage key={id} id={id} /> : b === 'info' ? <ChannelInfo id={id} initial={c === 'members' ? 'members' : 'about'} /> : b && Object.hasOwn(INFO_PAGES, b) ? <ChannelInfo key={b} id={id} initial={INFO_PAGES[b]} /> : b === 'events' ? <EventsScreen channel={id} /> : b === 'thread' && c ? <ThreadView groupId={id} rootId={c} replying={d === 'reply'} /> : <ChannelView id={id} topic={b === 't' ? c : undefined} focus={b === 'm' ? c : undefined} />
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

/** c/<id>/members, /photos, /files, /todos, /log: a channel's full lists (Xavier, 2026-11-12). */
const INFO_PAGES: Record<string, 'members' | 'files' | 'tasks' | 'log' | 'about'> = { members: 'members', images: 'files', photos: 'files', files: 'files', todos: 'tasks', log: 'log', about: 'about' }

const EVENT_KEY = 'belfry-event-open'
const pendingEvent = () => { try { return localStorage.getItem(EVENT_KEY) ?? '' } catch { return '' } }
const setPendingEvent = (link: string) => { try { if (link) localStorage.setItem(EVENT_KEY, link); else localStorage.removeItem(EVENT_KEY) } catch { /* private mode */ } }

/** Admins: show the raw Nostr events (a channel's Log links to them). */
function DevModeRow() {
  const [on, setOn] = useState(devMode())
  return (
    <label className="card switch-row" style={{ width: '100%' }}>
      <span><b>Developer mode</b><small>A channel's Log links each item to its Nostr event</small></span>
      <input type="checkbox" role="switch" checked={on} onChange={(e) => { setDevMode(e.target.checked); setOn(e.target.checked) }} />
    </label>
  )
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
          <button className="card row" style={{ width: '100%', textAlign: 'left', justifyContent: 'space-between' }} onClick={() => go('settings')}>
            <div>
              <div style={{ fontWeight: 600 }}>Settings of the belfry</div>
              <div className="hint" style={{ fontSize: 14 }}>Branding, roles, members, e-mail and more</div>
            </div>
            <span className="hint" aria-hidden="true">›</span>
          </button>
        )}
        {(isAdmin() || (THEME === 'belfry' && isBelfryAdmin(me()))) && <DevModeRow />}
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

/** Demo only: an agent joins CHANNEL as a guest (9000 with `agent`) and says TEXT there. */
function demoAgentPost(channel: string, name: string, text: string) {
  const hex = (n: number) => [...crypto.getRandomValues(new Uint8Array(n))].map((b) => b.toString(16).padStart(2, '0')).join('')
  const pk = hex(32), now = Math.floor(Date.now() / 1000)
  addEvents([
    { id: hex(32), pubkey: hex(32), sig: hex(64), kind: 9000, content: '', created_at: now - 2, tags: [['h', channel], ['p', pk, 'member'], ['invited-by', me()], ['name', name], ['guest', ''], ['agent', '1']] },
    { id: hex(32), pubkey: pk, sig: hex(64), kind: 0, content: JSON.stringify({ name }), created_at: now - 2, tags: [] },
    { id: hex(32), pubkey: pk, sig: hex(64), kind: 9, content: text, created_at: now - 1, tags: [['h', channel]] },
  ])
  return pk
}

/** Demo only: someone opens a guest link of CHANNEL (guest mode: a 9000 tagged `reading`, no
 *  name) → their key; with NAME, that key then joins (a second 9000 with the name, then `saved`). */
function demoGuestReading(channel: string, pubkey?: string, name?: string) {
  const hex = (n: number) => [...crypto.getRandomValues(new Uint8Array(n))].map((b) => b.toString(16).padStart(2, '0')).join('')
  const pk = pubkey ?? hex(32)
  const now = Math.floor(Date.now() / 1000)
  const base = { pubkey: hex(32), sig: hex(64), content: '' }
  const members = [...(groups().find((g) => g.id === channel)?.members ?? []), ...(groups().find((g) => g.id === channel)?.reading ?? []), pk]
  addEvents(name
    ? [{ ...base, id: hex(32), kind: 9000, created_at: now + 1, tags: [['h', channel], ['p', pk, 'member'], ['guest', ''], ['invited-by', me()], ['name', name]] },
       { ...base, id: hex(32), kind: 1985, created_at: now + 2, tags: [['h', channel], ['L', 'belfry/guest'], ['l', 'saved', 'belfry/guest'], ['p', pk]] }]
    : [{ ...base, id: hex(32), kind: 9000, created_at: now - 1, tags: [['h', channel], ['p', pk, 'member'], ['guest', ''], ['invited-by', me()], ['unsaved', '1'], ['reading', '1']] },
       // what the relay then says: its member list has their key
       { ...base, id: hex(32), kind: 39002, created_at: now, tags: [['d', channel], ...[...new Set(members)].map((p) => ['p', p])] }])
  return pk
}

/** Demo only: a group in PARENT made by someone else (Ana) or by me (ADMIN), announced there with
 *  its card; MEMBER puts me in it; APPLICANTS people ask to join (with an answer); VOUCHED of
 *  them got a vouch from Ben (a member). Returns the group id. */
function demoGroup(opts: { name?: string; parent?: string; closed?: boolean; vouches?: number; admin?: boolean; member?: boolean; applicants?: number; vouched?: number } = {}) {
  const hex = (n: number) => [...crypto.getRandomValues(new Uint8Array(n))].map((b) => b.toString(16).padStart(2, '0')).join('')
  const now = Math.floor(Date.now() / 1000)
  const { name = 'Garden club', parent = 'general', closed = true, vouches = 0, admin = false, member = false, applicants = 0, vouched = 0 } = opts
  const id = 'pod-' + name.toLowerCase().replace(/[^a-z0-9]+/g, '-') + '-' + hex(2)
  const person = (n: string) => { const pk = getPublicKey(generateSecretKey()); return { pk, ev: { id: hex(32), pubkey: pk, sig: hex(64), kind: 0, created_at: now - 900, tags: [], content: JSON.stringify({ name: n }) } } }
  const ana = person('Ana'), ben = person('Ben')
  const owner = admin ? me() : ana.pk
  const members = [owner, ben.pk, ...(member && !admin ? [me()] : [])]
  const evs: Record<string, unknown>[] = [ana.ev, ben.ev,
    { id: hex(32), pubkey: owner, sig: hex(64), kind: 9007, created_at: now - 800, tags: [['h', id]], content: '' },
    { id: hex(32), pubkey: owner, sig: hex(64), kind: 9002, created_at: now - 800, tags: [['h', id], ['name', name], ['about', 'We grow vegetables on the roof. **Wednesdays** at 6.'], closed ? ['closed'] : ['open'], ...(closed ? [['prompt', 'What would you like to grow?']] : []), ...(vouches ? [['vouches', String(vouches)]] : [])], content: '' },
    { id: hex(32), pubkey: hex(32), sig: hex(64), kind: 39001, created_at: now - 790, tags: [['d', id], ['p', owner, 'admin']], content: '' },
    { id: hex(32), pubkey: hex(32), sig: hex(64), kind: 39002, created_at: now - 790, tags: [['d', id], ...members.map((p) => ['p', p])], content: '' },
    { id: hex(32), pubkey: owner, sig: hex(64), kind: 9, created_at: now - 780, tags: [['h', parent], ['pod', id]], content: `⬡ New group: ${name}` },
  ]
  for (let i = 0; i < applicants; i++) {
    const a = person(['Nora', 'Omar', 'Pia'][i % 3])
    const req = { id: hex(32), pubkey: a.pk, sig: hex(64), kind: 9021, created_at: now - 600 + i, tags: [['h', id]], content: i === 0 ? 'Tomatoes, and <i>basil</i>.' : 'Herbs.' }
    evs.push(a.ev, req)
    if (i < vouched) evs.push({ id: hex(32), pubkey: ben.pk, sig: hex(64), kind: 1985, created_at: now - 500, tags: [['h', id], ['L', 'belfry/vouch'], ['l', 'vouch', 'belfry/vouch'], ['e', req.id], ['p', a.pk]], content: '' })
  }
  addEvents(evs as unknown as Parameters<typeof addEvents>[0])
  return id
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
