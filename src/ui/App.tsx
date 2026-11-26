import { applyRole, refreshMyRole } from '../core/myrole'
import { loadRelaySelf, relaySelf } from '../core/relayself'
import { startNames } from '../core/names'
import { demoTyping, demoTypingSent, setShowMyTyping, showMyTyping, watchDms } from '../core/typing'
import { replaceHash } from '../core/nav'
import { demoButtons } from '../core/buttons'
import { generateSecretKey, getPublicKey } from 'nostr-tools'
import { useEffect, useState } from 'react'
import { closeDrawer, useDrawer } from './drawer'
import { Phone } from './Phone'
import { SearchView } from './Search'
import { belfrySignOut, endSession, revokeWith, forgetDevice, clearSession, demoSession, finishSignIn, openAfterSignIn, pendingInvite, setOpenAfterSignIn, setPendingInvite, startSignIn, takeInviteFromUrl, loadSession, makeSigner, signerCalls, signerCallsIn, saveSession, setGuestInfo, guestInfo, type Session } from '../core/auth'
import { closeJoin, openingNow, reading, setOpening, useGuestMode } from '../core/guestmode'
import { GuestJoinHost, MemberInviteScreen, MemberNameScreen, OpeningScreen } from './GuestJoin'
import { nameNext } from '../core/guestjoin'
import { SIGNED_IN_AS, startAt, takeSignedInAs } from '../core/signedin'
import { releaseApplicantKey } from '../core/direct'
import type { SignedIn } from '../core/guestjoin'
import { CheckIcon } from './icons'
import { accessState, demoSetAgents, isAdmin, loadAccessInfo, myGrant, onAccessInfo } from '../core/access'
import { wipe } from '../core/db'
import { accounts, activeId, allAccounts, discardActiveFor, dropActive, forgetAll, noteAccount, readOf } from '../core/accounts'
import { roleBadge } from '../core/roles'
import { replyRoot, archivedGroups, deletedGroup, displayName, group, groups, profiles, nameOf } from '../core/model'
import { createChannel, createPod } from '../core/actions'
import { addEvents, hasEvent, loadAll, outboxItems, status, useVersion } from '../core/store'
import { enqueue, isLocalOnly, joinWithAccount, me, setAvatar, start, stop } from '../core/sync'
import { BELFRY, NAME, PLACE, TEST_RELAYS, THEME, getBackend } from '../config'
import { Admin } from './Admin'
import { AboutBelfry, AccountsScreen, Diagnostics } from './Accounts'
import { BrandingScreen } from './Branding'
import { SettingsScreen } from './Settings'
import { MembersDirectory } from './Directory'
import { SignInMethods } from './SignInMethods'
import { ArchivedScreen, DeletedGroup, GroupPage } from './GroupPage'
import { useGroupState } from '../core/groupstate'
import { demoRequests, startRequests } from '../core/requests'
import { demoRoles, loadRoles } from '../core/roles'
import { demoEvent } from '../core/demoevent'
import { demoSealed } from '../core/demosealed'
import { AllowPrompt, DevicesScreen, EncryptionScreen, FreshElsewherePrompt, OnlyDeviceAlert, ShowHowScreen } from './Sealed'
import { addedNotices as sealedNotices, askNow, testSignRoster, chainOf, deviceIn, devicePub, expectSealed, freshStart, keyCount, isSealedConv, readyChain, setSealedUsername, startSealed, takeLinkSecret } from '../core/sealedrt'
import { demoAdded, demoAsk, demoFreshElsewhere, demoLinked, demoUnlockHere, onlyDevice } from '../core/sealed'
import { demoCheckedIn, demoCode, demoSteward } from '../core/checkin'
import { isBelfryAdmin } from '../core/people'
import { EventsScreen, eventPath } from './Calendar'
import { EventJoinScreen, EventScreen, EventWaitScreen } from './Events'
import { demoApply, demoGuest, eventLinkFromUrl, pendingApplication, setPendingApplication, ticketLinkFromUrl } from '../core/events'
import { eventByCoord, eventOfGroup } from '../core/model'
import { ChannelInfo, TodoScreen } from './ChannelInfo'
import { Connections } from './Connections'
import { ChannelList, ChannelView, ThreadView } from './Chat'
import { orderContent } from '../core/order'
import { ChannelPage, CHANNEL_PAGES } from './ChannelMenu'
import { MessagesList, MessageThread } from './Messages'
import { demoMail, groupsKey, lastChannelMail, setMyAddress, startDms } from '../core/dm'
import { startReadState } from '../core/readstate'
import { demoInboxChannel, demoInboxMail, demoMailPost } from './MailPost'
import { Avatar, back, Btn, devMode, fmtDay, fmtTime, go, setDevMode, useRoute, useWide, WIDE3 } from './common'
import { openProfile, ProfileView } from './Profile'
import { dropCode, OpenInvite, MembershipScreen, WelcomeScreen } from './Login'
import { MentionsScreen } from './Mentions'
import { ApplyScreen, CalendarPlace, GroupsScreen, HomeScreen, SidePlaces, TabBar, TodosScreen, demoApplyStatus, placeOf, placesOn, townPath } from './Places'
import { CardScreen } from './Card'
import { GroupWho } from './WhoCanRead'
import { cardNow, hashtagSlug, setDemoMake, takeCardFromUrl } from '../core/cards'
import { setEncryptedHere } from '../core/model'
import { encProbe, encTest, encWhy, onRemoved, removedNow, startEnc } from '../core/enc/runtime'
import { testFormat3 } from '../core/access'
import { CharterScreen, WelcomeFlow, demoCharter, welcomeDue } from './Charter'

// demo: a card's claim makes its group or topic here, as the belfry's groups key would
// (as the cards server does, Belfry 7bfa781: a topic's name is its hashtag, about "<topic> · <description>",
// and it is announced in general with ["t","new-topic"], ["channel", <hashtag>])
if (getBackend() === 'demo') setDemoMake(async (kind, f) => {
  if (kind !== 'topic') return createPod(f.title.trim(), f.about.trim(), 'general', { closed: false, prompt: '' })
  const tag = hashtagSlug(f.hashtag), about = f.about.trim() // (Cards v87: the hashtag is the name, the description the about)
  const id = await createChannel(tag, about, tag)
  await enqueue('groups', { kind: 9, tags: [['h', 'general'], ['t', 'new-topic'], ['channel', tag]], content: `# New topic: #${tag} · ${about}\n${location.origin}${location.pathname}#/c/${tag}` })
  return id
})

type Phase = { p: 'loading' } | { p: 'out'; error?: string } | { p: 'in'; s: Session }

/** encrypted/removed-device: one full screen; this device's messages and keys are already gone. */
function RemovedScreen({ r }: { r: { at: number; by?: string } }) {
  const when = r.at ? `${fmtDay(new Date(r.at * 1000)).toLowerCase()} at ${fmtTime(r.at)}` : ''
  return (
    <div className="screen login removed-device" role="alert">
      <h2>This device was removed</h2>
      <p>{r.by ? `${r.by} removed it` : 'Another of your devices removed it'}{when ? ` ${when}` : ''}. Your messages were deleted from this device. They’re still on your other devices.</p>
      <button className="btn" onClick={() => { location.hash = ''; location.reload() }}>Sign in again</button>
    </div>
  )
}

export function App() {
  const [phase, setPhase] = useState<Phase>({ p: 'loading' })
  // another of my devices removed this one (§16.6): its data goes at once, then one screen
  const [removedHere, setRemovedHere] = useState(removedNow())
  useEffect(() => onRemoved(() => void (async () => {
    stop(); await clearSession(); await wipe(); forgetDevice()
    setRemovedHere(removedNow())
  })()), [])
  const backend = getBackend()
  const [signInFirst, setSignInFirst] = useState(false)
  const [, setCardTick] = useState(0)

  useEffect(() => {
    void (async () => {
      takeCardFromUrl() // a printed card (#/g/<id>): kept until it is dealt with
      takeInviteFromUrl() // a guest link (#/join/<token>): kept until it is used
      takeLinkSecret() // Link a phone (sealed.md §5.2): #l=<secret>, kept until this device has asked
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

  // signed in from the start screen (Log in, or this browser's application accepted): in the
  // chat at once, no reload; "Signed in as <address>" for 3 s
  const directIn = async (r: SignedIn) => {
    await wipe() // a new account: nothing of another one's cache
    try { sessionStorage.setItem(SIGNED_IN_AS, r.address) } catch { /* private mode */ }
    if (location.pathname === '/apply') history.replaceState(history.state, '', '/' + location.search + location.hash)
    if (!pendingInvite()) replaceHash(r.open ? `#/c/${encodeURIComponent(r.open)}` : '#/c/general')
    await enter(r.session)
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
    // this account only: its database goes; another account of this browser opens if there is one
    const another = await dropActive()
    location.hash = ''
    if (s?.backend === 'belfry' && !another) return belfrySignOut(s)
    location.reload()
  }

  // An address proven in guest mode had an account that holds its own key (409 taken): this
  // guest session goes, and the person signs in; the channel opens after.
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
  if (removedHere) return <RemovedScreen r={removedHere} />
  if (phase.p === 'loading') return veil
  // a printed card opened by someone who isn't signed in: start its group, or its guest link
  if (phase.p === 'out' && cardNow() && !signInFirst && !phase.error)
    return <CardScreen id={cardNow()} onIn={directIn} onStarted={() => {}} onClose={() => setCardTick((n) => n + 1)}
      onInvite={() => { setOpening({}); setCardTick((n) => n + 1) }} />
  if (phase.p === 'out' && pendingInvite() && !signInFirst && !phase.error)
    return <>{veil}<OpenInvite token={pendingInvite()} backend={backend} onSignIn={() => setSignInFirst(true)}
      onJoined={async (s) => { await wipe(); await enter(s) }} /></>
  // a ticket's QR opened by someone who isn't signed in: the event's public page
  if (phase.p === 'out' && ticketLinkFromUrl()) {
    location.replace(`https://${BELFRY.domain}/events/${encodeURIComponent(ticketLinkFromUrl())}`)
    return null
  }
  // an event's URL opened in the chat by someone with no account: the event, RSVP or apply
  if (phase.p === 'out' && eventLinkFromUrl() && !signInFirst && !phase.error)
    return <EventJoinScreen link={eventLinkFromUrl()} backend={backend} onSignIn={() => { setPendingEvent(eventLinkFromUrl()); startAt('login'); setSignInFirst(true) }}
      onApply={() => { setPendingEvent(eventLinkFromUrl()); startAt('apply'); setSignInFirst(true) }}
      onJoined={async (s) => { await wipe(); await enter(s) }}
      onDemo={(name) => void enter(demoSession(`${name.toLowerCase().replace(/[^a-z0-9]+/g, '.') || 'guest'}@example.org`))} />
  if (phase.p === 'out') return <WelcomeScreen invited={!!pendingInvite()} backend={backend} error={phase.error} onDemo={(email) => enter(demoSession(email))} onIn={directIn} />
  return <>{veil}<Village key={phase.s.bunkerUrl ?? phase.s.email} session={phase.s} onSignOut={signOut} onSignIn={signInInstead} /></>
}

function Village({ session, onSignOut, onSignIn }: { session: Session; onSignOut: () => void; onSignIn: (channel: string, moved: boolean) => Promise<void> }) {
  const [ready, setReady] = useState(false)
  const [error, setError] = useState('')
  const [, setTick] = useState(0)
  useEffect(() => onAccessInfo(() => setTick((t) => t + 1)), [])
  // the groups relay's own key (metadata pinned to it): once the sync has started (before that it counts as local),
  // and again later if still unknown
  useEffect(() => {
    const t1 = setTimeout(() => void loadRelaySelf(), 1200)
    const t2 = setTimeout(() => { if (!relaySelf()) void loadRelaySelf() }, 10_000)
    return () => { clearTimeout(t1); clearTimeout(t2) }
  }, [session])
  // my role from the belfry (whois), at start and back in the app: never the guest view for a member (myrole.ts)
  useEffect(() => {
    const ask = () => { if (document.visibilityState === 'visible') void refreshMyRole() }
    const t = setTimeout(ask, 1500)
    document.addEventListener('visibilitychange', ask)
    return () => { clearTimeout(t); document.removeEventListener('visibilitychange', ask) }
  }, [session])
  useEffect(() => {
    let cancelled = false
    void (async () => {
      try {
        await loadAll()
        const signer = await makeSigner(session)
        if (cancelled) return
        await start(signer, { localOnly: session.backend === 'demo' && !TEST_RELAYS })
        // signed in from the start screen: the applicant key leaves its slot once the saved
        // session has signed (a dead tab before this is still let in by /apply/status)
        void signer.sign({ kind: 27235, content: '', created_at: Math.floor(Date.now() / 1000), tags: [['u', 'belfry:session-check']] }).then(() => releaseApplicantKey(session.clientSecret), () => {})
        // direct messages and e-mail (dm.ts); my address is my name@domain
        setMyAddress(session.membership.nip05 ?? profiles().get(signer.pubkey)?.nip05 ?? (session.backend === 'demo' ? `${(session.email ?? 'me').split('@')[0]}@${BELFRY.domain}` : undefined))
        void startReadState() // what I read, shared across my devices (before startDms: it reads the marks)
        // sealed conversations (docs/sealed.md): this sign-in's device key is the session's client
        // key; members and admins only, never a guest session (§5.1). Sends wait until it has loaded.
        const sealedOn = (session.backend === 'belfry' || TEST_RELAYS) && !!session.clientSecret && !guestInfo()
        // encrypted conversations, format 3 (docs/encrypted.md): every chat session is a device, a
        // guest's too (the lead's ruling, the review's P1.1); read always, written when belfry.json says 3
        // (never for a guest's session: guests aren't on the community relay's whitelist; the lead, 2026-11-23)
        if ((session.backend === 'belfry' || TEST_RELAYS) && session.clientSecret && !guestInfo()) {
          void startEnc(session.clientSecret)
          if (TEST_RELAYS) Object.assign(window, { __enc: { state: encTest, format3: testFormat3, why: encWhy, probe: encProbe, relaySelf } })
        } else setEncryptedHere(() => false) // no runtime here (a guest, no device key): no key ever seen, nothing waits for it
        if (sealedOn) expectSealed()
        startDms()
        if (sealedOn && session.clientSecret) {
          setSealedUsername((session.membership.nip05 ?? profiles().get(signer.pubkey)?.nip05 ?? '').split('@')[0])
          void startSealed(session.clientSecret)
          // tests against a local relay only (sealedwire.cjs): public facts, never a secret
          if (TEST_RELAYS) Object.assign(window, { __sealed: { device: devicePub, inHead: deviceIn, ready: readyChain, sealed: isSealedConv, keys: keyCount, askNow, signRoster: testSignRoster, signerIn: signerCallsIn, signerCalls: () => signerCalls.map((c) => c.method), has: hasEvent, root: (pk: string) => chainOf(pk)?.rosters[0].id ?? '', fresh: (pk: string) => !!freshStart(pk), notices: () => sealedNotices().map((n) => n.device) } })
        }
        watchDms() // others typing to me (typing.ts): one live subscription on the community relay
        startNames() // names never known: asked for, never a shortened npub
        void groupsKey()
        if (session.backend === 'demo') Object.assign(window, { demoRole: applyRole, demoButtons, demoMail, demoMailPost, demoInboxMail, demoInboxChannel, lastChannelMail, demoGuestJoin, demoGuestReading, demoPeople, demoApply, demoGuest, demoRoles, demoAgentPost, demoRequests, demoGroup, demoEvent, demoCheckedIn, demoSteward, demoCode, demoApplyStatus, demoCharter, demoSetAgents, demoSealed, demoAsk, demoAdded, demoLinked, demoUnlockHere, demoFreshElsewhere, __demoTyping: demoTyping, __demoTypingSent: () => demoTypingSent, __demoMe: () => me(), __demoOrder: () => orderContent() })
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
  // this account's row in Accounts (who it is, its role); the same person signed in twice in two
  // slots: this new slot goes (its session ended) and the older one opens
  useVersion()
  const meP = ready ? profiles().get(me()) : undefined
  const badge = ready ? roleBadge(me()) : ''
  useEffect(() => {
    if (!ready) return
    const dup = noteAccount({ pubkey: me(), email: session.email, name: meP?.name, picture: meP?.picture, label: guestInfo() ? 'Guest' : badge || 'Member' })
    if (dup) void (async () => { if (session.backend === 'belfry') await endSession(session); discardActiveFor(dup.id) })()
  }, [ready, meP?.name, meP?.picture, badge]) // eslint-disable-line react-hooks/exhaustive-deps
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
  const [card, setCard] = useState(cardNow)
  // a card's address opened while the app runs (a link in a message, the address bar)
  useEffect(() => { if (route[0] === 'g' && route[1]) { const c = takeCardFromUrl(); if (c) setCard(c) } }, [route[0], route[1]])
  const wide = useWide()
  const three = useWide(WIDE3)
  const drawer = useDrawer()
  useEffect(() => { if (wide) closeDrawer() }, [wide])
  useVersion()
  // the channel or group the address names (c/<id>/…), and whether it is still there: a deleted
  // one shows that, whatever the link was to (groupstate.ts asks the relay when nothing is known)
  const chanId = route[1] ?? lastChannel() ?? groups()[0]?.id ?? 'general'
  const chanState = useGroupState((route[0] ?? 'c') === 'c' && route[1] ? chanId : undefined)
  // an event's chat has the event's own address: its slug when it has one (Xavier, 2026-11-18:
  // "Always use the custom slug if one is set as a general rule"), never #/c/ev-<d>
  // (older events too, lead 2026-11-20); an old #/cal/<coord> address moves to the event's link
  const evRoute = route[0] === 'c' && route[1] && /^(ev|ws)-/.test(route[1]) && (!route[2] || route[2] === 'm') ? eventOfGroup(route[1])
    : route[0] === 'cal' && route[1] ? eventByCoord(route[1]) : undefined
  useEffect(() => {
    if (evRoute) replaceHash('#/' + eventPath(evRoute) + (route[0] === 'c' && route[2] === 'm' && route[3] ? '/m/' + route[3] : ''))
  }, [evRoute?.coord, evRoute?.slug, route[0], route[2], route[3]]) // eslint-disable-line react-hooks/exhaustive-deps
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
      ({ channel, already }) => { go('c/' + encodeURIComponent(channel)); setJoined(already ? '' : `You joined # ${nameOf(channel)}`) },
      (e) => setJoined(e instanceof Error ? e.message : String(e)),
    )
  }, [ready, route.join('/')])
  useEffect(() => { if (!joined) return; const t = setTimeout(() => setJoined(''), 6000); return () => clearTimeout(t) }, [joined])
  // just signed in by a guest's code: "Signed in as <address>" for 3 s (said once)
  const [signedAs, setSignedAs] = useState(takeSignedInAs)
  // read on each render: a member link sets it while the app runs (the demo doesn't reload)
  const [, nameTick] = useState(0)
  const askName = session.backend === 'demo' || session.bunkerUrl ? nameNext() : null
  useEffect(() => { if (!signedAs) return; const t = setTimeout(() => setSignedAs(''), 3000); return () => clearTimeout(t) }, [signedAs])
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
  const toast = <>{joined ? <div className="toast" role="status"><span>{joined}</span><button onClick={() => setJoined('')}>OK</button></div> : null}{signedAs ? <div className="toast signed-as" role="status"><CheckIcon /><span>Signed in as {signedAs}</span></div> : null}<GuestJoinHost onSignIn={onSignIn} /></>
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

  // a member link, not used yet: log in or sign up, no channel behind it (#issues 2026-11-18)
  if (reading() && guestInfo()?.member) return <>{toast}<MemberInviteScreen /></>

  // the places (Places.tsx): the app starts on Home; #/town opens the town channel I was in
  const places = placesOn()
  if (places && route[0] === 'town') { replaceHash('#/' + townPath()); return null }
  // chat.<domain>/login/<username> opened while signed in: nothing to do, stay where I was
  if (route[0] === 'login') { replaceHash('#/home'); return null }
  // a member link just used: their name and username first (board onboarding/member-link-name)
  if (places && askName && !reading()) return <MemberNameScreen email={askName.email} onDone={() => nameTick((x) => x + 1)} />
  if (places && welcomeDue() && route[0] !== 'welcome' && !card) { replaceHash('#/welcome'); return null }
  const [section = places ? 'home' : 'c', a, b, c, d] = route
  let view
  let pane // a large screen: an open thread in a third column, its channel stays in the middle
  if (card) view = <CardScreen id={card} signedIn={profiles().get(me())?.name || session.email || displayName(me())} signedEmail={session.email || profiles().get(me())?.nip05 || undefined} onSignOut={onSignOut} onIn={() => {}}
    onInvite={(g) => { setCard(''); go('c/' + encodeURIComponent(g)) }} onStarted={(g) => { setCard(''); go('c/' + encodeURIComponent(g)) }} onClose={() => setCard('')} />
  else if (places && section === 'home') view = <HomeScreen />
  else if (places && section === 'calendar') view = <CalendarPlace />
  else if (places && section === 'groups') view = <GroupsScreen />
  else if (places && section === 'todos') view = <TodosScreen />
  else if (places && section === 'apply') view = <ApplyScreen />
  else if (section === 'charter') view = <CharterScreen sub={a} id={b} />
  else if (section === 'welcome') view = <WelcomeFlow />
  else if (section === 'channels' && !wide) view = <ChannelList />
  else if (section === 'dm' && a === 'unlock') view = <ShowHowScreen />
  else if (section === 'dm' && a && b === 'lock') view = <EncryptionScreen conv={a} />
  else if (section === 'dm' && a) view = <MessageThread conv={a} />
  else if (section === 'dm') view = <MessagesList />
  else if (section === 'mentions') view = <MentionsScreen />
  else if (section === 'events' && a) view = <EventScreen key={a} link={a} ticket={b === 't' ? c : undefined} />
  else if (section === 'events' || section === 'cal' && !a) view = <EventsScreen />
  else if (section === 'cal' && a) view = <EventScreen key={a} coord={a} />
  else if (section === 'me' && a === 'connections') view = <Connections session={session} />
  else if (section === 'me' && a === 'devices') view = <DevicesScreen />
  else if (section === 'me' && a === 'signin') view = <SignInMethods />
  else if (section === 'me' && a === 'privacy') view = <Privacy />
  else if (section === 'me' && a === 'branding' && THEME === 'belfry') view = <BrandingScreen />
  else if (section === 'me') view = <Me session={session} onSignOut={onSignOut} />
  else if (section === 'accounts') view = <AccountsScreen link={a === 'link'} />
  else if (section === 'about' && a === 'diagnostics') view = <Diagnostics />
  else if (section === 'about') view = <AboutBelfry />
  else if (section === 'settings' && THEME === 'belfry') view = <SettingsScreen section={a} id={b} />
  else if (section === 'archived') view = <ArchivedScreen />
  else if (section === 'requests') view = <MembersDirectory />
  else if (section === 'members') view = <MembersDirectory />
  else if (section === 'admin') view = <Admin sub={a} id={b} />
  else if (section === 'search') view = <SearchView channel={a} />
  else if (section === 'u') view = <ProfileView key={a + (b === 'in' ? c : '')} id={a} inGroup={b === 'in' ? c : undefined} />
  else if (chanState === 'deleted' && !eventOfGroup(chanId)) view = <DeletedGroup /> // whatever the link was to: its chat, page, a thread, a message
  else {
    const id = chanId
    const evOf = eventOfGroup(id) // an event's chat opens as the event
    if (group(id)?.archived && b && ARCHIVED_READS.has(b)) view = <ChannelInfo key={b} id={id} initial={b === 'info' ? (c === 'members' ? 'members' : 'about') : INFO_PAGES[b] === 'tasks' ? 'about' : INFO_PAGES[b]} />
    else if (b === 'todo' && c) view = <TodoScreen key={c} groupId={id} taskId={c} />
    else if (group(id)?.archived && b && b !== 'm' && b !== 't' && b !== 'thread' && b !== 'group') view = <ChannelView id={id} /> // its to-dos, its events: the channel itself, read only
    else if (evOf && (!b || b === 'm' || (b === 'thread' && wide && three && c))) {
      view = <EventScreen key={id} coord={evOf.coord} activeThread={b === 'thread' ? c : undefined} />
      if (b === 'thread' && c) pane = <ThreadView key={c} groupId={id} rootId={c} pane replying={d === 'reply'} />
    } else if (wide && three && b === 'thread' && c) {
      view = <ChannelView id={id} activeThread={c} />
      pane = <ThreadView key={c} groupId={id} rootId={c} pane replying={d === 'reply'} />
    } else view = b === 'group' ? <GroupPage key={id} id={id} />
      // the header's menu (ChannelMenu.tsx); old links to the channel's info land on its screens
      : (b === 'info' || b === 'about') && c !== 'members' ? <ChannelView id={id} menu />
      // the first open from a card: half pulled down (cards canvas; not linked from anywhere yet)
      : b === 'mid' ? <ChannelView id={id} mid />
      // a private group's lock (encrypted/group-who)
      : b === 'who' ? <GroupWho id={id} />
      : b === 'info' ? <ChannelPage key="members" id={id} page="members" />
      : b && OLD_PAGES[b] ? <ChannelPage key={OLD_PAGES[b]} id={id} page={OLD_PAGES[b]} />
      : b && CHANNEL_PAGES.has(b) ? <ChannelPage key={b} id={id} page={b} />
      : b === 'events' ? <EventsScreen channel={id} /> : b === 'thread' && c ? <ThreadView groupId={id} rootId={c} replying={d === 'reply'} />
      // a link to a thread reply: the thread, that reply centred and flashed (board channels/message-link)
      : b === 'm' && c && replyRoot(id, c) ? <ThreadView key={c} groupId={id} rootId={replyRoot(id, c)!} focus={c} />
      : <ChannelView id={id} topic={b === 't' ? c : undefined} focus={b === 'm' ? c : undefined} />
  }
  // phone: no tab bar: channels in a drawer behind ☰ (☰ again closes it), Me behind your avatar (top right), events under each
  // channel's name. Laptop: the channel list stays on the left, you at its bottom (like WhatsApp).
  const current = section === 'c' ? chanId : section === 'events' ? a : undefined
  const place = places ? placeOf(route) : undefined
  if (!wide) return <><Phone current={current} section={section} open={drawer} bar={place ? <TabBar place={place} /> : undefined}>{view}</Phone>{toast}<AllowPrompt /><FreshElsewherePrompt email={session.email} /></>
  return (
    <div className="layout wide">
      <aside className="side" aria-label="Channels"><ChannelList side current={current} section={section} top={places ? <SidePlaces place={place} /> : undefined} /></aside>
      <main className="main screen">{view}</main>
      {pane && <aside className="thread-pane" aria-label="Thread">{pane}</aside>}
      {toast}
      <AllowPrompt />
      <FreshElsewherePrompt email={session.email} />
    </div>
  )
}

/** c/<id>/members, /photos, /files, /todos, /log: a channel's full lists (Xavier, 2026-11-12). */
/** What of an archived group's pages stays open: reading. */
const ARCHIVED_READS = new Set(['info', 'about', 'members', 'images', 'photos', 'files', 'log'])
const OLD_PAGES: Record<string, string> = { images: 'photos' }
const INFO_PAGES: Record<string, 'members' | 'files' | 'tasks' | 'log' | 'about'> = { members: 'members', images: 'files', photos: 'files', files: 'files', todos: 'tasks', log: 'log', about: 'about' }

const EVENT_KEY = 'belfry-event-open'
const pendingEvent = () => { try { return localStorage.getItem(EVENT_KEY) ?? '' } catch { return '' } }
const setPendingEvent = (link: string) => { try { if (link) localStorage.setItem(EVENT_KEY, link); else localStorage.removeItem(EVENT_KEY) } catch { /* private mode */ } }

/** Me → Privacy (canvas v110 messages/typing-setting): this browser's own settings. */
function Privacy() {
  const [on, setOn] = useState(showMyTyping())
  return (
    <div className="screen privacy">
      <div className="topbar">
        <button className="icon-btn" aria-label="Back" onClick={() => back('me')}>‹</button>
        <h1>Privacy</h1>
      </div>
      <div className="scroll" style={{ padding: 16 }}>
        <h3 className="privacy-sec">This browser</h3>
        <label className="card switch-row" style={{ width: '100%' }}>
          <span><b>Show others when I'm typing</b></span>
          <input type="checkbox" role="switch" checked={on} onChange={(e) => { setShowMyTyping(e.target.checked); setOn(e.target.checked) }} />
        </label>
        <p className="hint">When this is off, this browser doesn't tell anyone you're typing. You still see when others are.</p>
      </div>
    </div>
  )
}

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
  const id = m ? decodeURIComponent(m[1]) : undefined
  // the app opens where you were, unless that was deleted or archived since
  return id && (deletedGroup(id) || group(id)?.archived) ? undefined : id
}
addEventListener('hashchange', () => {
  if (location.hash.startsWith('#/c/')) try { sessionStorage.setItem('osv-last-channel', location.hash) } catch { /* ignore */ }
})

function Me({ session, onSignOut }: { session: Session; onSignOut: () => void | Promise<void> }) {
  // canvas v117: this browser is the only device for sealed conversations: asked first
  const [onlyAsk, setOnlyAsk] = useState<'sign out' | 'forget' | null>(null)
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
        {!guestInfo() && <button className="card row" style={{ width: '100%', textAlign: 'left', justifyContent: 'space-between' }} onClick={() => go('me/signin')}>
          <div>
            <div style={{ fontWeight: 600 }}>Sign-in methods</div>
            <div className="hint" style={{ fontSize: 14 }}>E-mail code and passkeys</div>
          </div>
          <span className="hint" aria-hidden="true">›</span>
        </button>}
        {/* canvas v88: always (every member has a list of where they're signed in) */}
        {<button className="card row me-devices" style={{ width: '100%', textAlign: 'left', justifyContent: 'space-between' }} onClick={() => go('me/devices')}>
          <div>
            <div style={{ fontWeight: 600 }}>Devices</div>
            <div className="hint" style={{ fontSize: 14 }}>Where you’re signed in</div>
          </div>
          <span className="hint" aria-hidden="true">›</span>
        </button>}
        <button className="card row" style={{ width: '100%', textAlign: 'left', justifyContent: 'space-between' }} onClick={() => go('me/privacy')}>
          <div>
            <div style={{ fontWeight: 600 }}>Privacy</div>
            <div className="hint" style={{ fontSize: 14 }}>What this browser tells others</div>
          </div>
          <span className="hint" aria-hidden="true">›</span>
        </button>
        <button className="card row" style={{ width: '100%', textAlign: 'left', justifyContent: 'space-between' }} onClick={() => go('me/connections')}>
          <div>
            <div style={{ fontWeight: 600 }}>Connections</div>
            <div className="hint" style={{ fontSize: 14 }}>Your key and the apps signed in as you</div>
          </div>
          <span className="hint" aria-hidden="true">›</span>
        </button>
        <button className="link sign-out" onClick={() => (onlyDevice() ? setOnlyAsk('sign out') : void onSignOut())}>Sign out{accounts().filter((a) => a.pubkey).length > 1 ? ' of this account' : ''}</button>
        <Btn className="link sign-out forget-device" onClick={async () => {
          if (onlyDevice()) { setOnlyAsk('forget'); return }
          if (!confirm('Sign out of every account on this browser and forget them all?')) return
          await forgetDeviceNow(session)
        }}>Forget this device</Btn>
        {onlyAsk && <OnlyDeviceAlert verb={onlyAsk} onClose={() => setOnlyAsk(null)} onGo={async () => { if (onlyAsk === 'forget') await forgetDeviceNow(session); else await onSignOut() }} />}
      </div>
    </div>
  )
}

/** Forget this device: every account's session ends in the signer, each signed with its own key,
 *  then everything goes (the letterbox keys too). */
async function forgetDeviceNow(session: Session) {
  stop()
  for (const a of allAccounts()) {
    const s = a.id === activeId() ? session : await readOf<Session>(a, 'session')
    if (s) await revokeWith(s)
  }
  await forgetAll()
  location.hash = ''
  location.reload()
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
  const members = [...(group(channel)?.members ?? []), ...(group(channel)?.reading ?? []), pk]
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
function demoGroup(opts: { name?: string; parent?: string; closed?: boolean; private?: boolean; vouches?: number; admin?: boolean; member?: boolean; applicants?: number; vouched?: number; about?: string } = {}) {
  const hex = (n: number) => [...crypto.getRandomValues(new Uint8Array(n))].map((b) => b.toString(16).padStart(2, '0')).join('')
  const now = Math.floor(Date.now() / 1000)
  const { name = 'Garden club', parent = 'general', closed = true, private: inviteOnly = false, vouches = 0, admin = false, member = false, applicants = 0, vouched = 0 } = opts
  const id = 'pod-' + name.toLowerCase().replace(/[^a-z0-9]+/g, '-') + '-' + hex(2)
  const person = (n: string) => { const pk = getPublicKey(generateSecretKey()); return { pk, ev: { id: hex(32), pubkey: pk, sig: hex(64), kind: 0, created_at: now - 900, tags: [], content: JSON.stringify({ name: n }) } } }
  const ana = person('Ana'), ben = person('Ben')
  const owner = admin ? me() : ana.pk
  const members = [owner, ben.pk, ...(member && !admin ? [me()] : [])]
  const evs: Record<string, unknown>[] = [ana.ev, ben.ev,
    { id: hex(32), pubkey: owner, sig: hex(64), kind: 9007, created_at: now - 800, tags: [['h', id]], content: '' },
    { id: hex(32), pubkey: owner, sig: hex(64), kind: 9002, created_at: now - 800, tags: [['h', id], ['name', name], ['about', opts.about ?? 'We grow vegetables on the roof. **Wednesdays** at 6.'], closed ? ['closed'] : ['open'], closed && inviteOnly ? ['private'] : ['public'], ...(closed ? [['prompt', 'What would you like to grow?']] : []), ...(vouches ? [['vouches', String(vouches)]] : [])], content: '' },
    { id: hex(32), pubkey: hex(32), sig: hex(64), kind: 39001, created_at: now - 790, tags: [['d', id], ['p', owner, 'admin']], content: '' },
    { id: hex(32), pubkey: hex(32), sig: hex(64), kind: 39002, created_at: now - 790, tags: [['d', id], ...members.map((p) => ['p', p])], content: '' },
  ]
  // a Private group posts no announcement (docs/one-shape.md)
  if (!(closed && inviteOnly)) evs.push({ id: hex(32), pubkey: owner, sig: hex(64), kind: 9, created_at: now - 780, tags: [['h', parent], ['pod', id]], content: `⬡ New group: ${name}` })
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
