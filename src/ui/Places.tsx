// One belfry, five places (the owner's canvas, boards iOS-Home / iOS-CalGroups / iOS-Town /
// Join-Guest / Join-Apply; iOS SPEC 19.31). On a phone a bottom bar: Home · Calendar · Create (＋)
// · Groups · Town; guests: Home · Calendar · Town. The bar shows only at a place's root: Home,
// Calendar, Groups, and a town channel (the Square and the channels of the Town drawer). Chats of
// groups, events, threads, direct messages and profiles open over it, without the bar. On a laptop
// the same places sit at the top of the left column (App.tsx).
//   Home     = what needs me today on this belfry; calm when nothing does
//   Calendar = My calendar · Invited · Discover (guests: Public events · Going); no buttons on rows
//   Groups   = My groups · Active · All
//   Town     = the town's channels, with the ☰ drawer
//   Create   = one sheet: Message, Event, Meeting, To-do, Group, Proposal into a chosen channel
import { AgreementAbout } from './Charter'
import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { NAME, THEME } from '../config'
import { agentOwner } from '../core/access'
import { applyStatus, demoApplyStatus, loadApplyStatus, sendApplication, ApplyFailed, APPLY_ERRORS, type ApplyStatus } from '../core/apply'
import { guestInfo } from '../core/auth'
import { useBranding } from '../core/branding'
import { charter, charterExists, loadRemoteCharter, remoteAgreements, toAgree, isStale } from '../core/charter'
import { threads, unreadTotal, convName } from '../core/dm'
import { myState, publicEvents } from '../core/events'
import { askToJoin, reading } from '../core/guestmode'
import { isGuest } from '../core/invite'
import { allGroups, archivedGroups, calendar, channelIcon, displayName, realName, group, groups, guestJoins, messages, tasklist, isWorking, workingSince, type CalEvent, type Group, type Task } from '../core/model'
import { CHARTER_ID, MEMBERS } from '../core/places'
import { agentRequests, belfryRequests, groupRequests, requestCount, startRequests } from '../core/requests'
import { useVersion } from '../core/store'
import { me } from '../core/sync'
import { channelNewest, channelUnread } from '../core/unread'
import { joinGroup, taskAction } from '../core/actions'
import { isBelfryAdmin } from '../core/people'
import { inOrder } from '../core/order'
import { Avatar, Btn, Form, fmtTime, go, Sheet, back, useRoute, useNow, WorkRing } from './common'
import { useCommunityName } from './community'
import { CalendarIcon, ChevronDownIcon, ChevronRightIcon, DocIcon, GateIcon, HexIcon, HomeIcon, MessageIcon, PeopleIcon, PlusIcon, SearchIcon, TodoIcon, TownIcon, AtIcon, ArchiveIcon, PodShape, ClockIcon, CheckCircleIcon } from './icons'
import { mentions, isNew as isNewMention, preview } from './Mentions'
import { eventPath, when } from './Calendar'
import { Arms } from './Login'
import { MenuButton } from './Chat'

// ---- which place, and whether the bar shows ------------------------------------------------

export type Place = 'home' | 'calendar' | 'groups' | 'town'

/** Places are the Belfry look's (the OSV look keeps its drawer); not while only reading a guest
 *  link (guest mode: the channel and Join, nothing else). */
export const placesOn = () => THEME === 'belfry' && !reading()

/** I am here as a guest (of one or more channels), not a member nor a member's agent. */
export const amGuest = () => !agentOwner(me()) && (isGuest(me()) || (!!guestInfo() && !guestInfo()!.member && !isBelfryAdmin(me())))

/** A channel of the town (the drawer's): not a group, an event's chat or a proposal. */
export function isTownChannel(id: string): boolean {
  if (id === CHARTER_ID) return false
  const g = group(id)
  if (g) return !g.isPod && !g.isEvent && !g.isProposal
  return !/^(pod-|ev-|ws-|prop-)/.test(id)
}

/** The place a route is the root of (the bar shows there), else undefined. */
export function placeOf(route: string[]): Place | undefined {
  const [section = '', a, b] = route
  if (section === '' || section === 'home') return 'home'
  if (section === 'calendar') return 'calendar'
  if (section === 'groups') return 'groups'
  if (section === 'town') return 'town'
  if (section === 'c' && a && !b && isTownChannel(a)) return 'town'
  return undefined
}

const LAST_TOWN = 'belfry-last-town'
/** Where Town opens: the town channel I was last in (unless gone or archived), else the Square. */
export function townPath(): string {
  let id = ''
  try { id = sessionStorage.getItem(LAST_TOWN) ?? '' } catch { /* private mode */ }
  const g = id ? group(id) : undefined
  return 'c/' + encodeURIComponent(g && !g.archived && isTownChannel(id) ? id : 'general')
}
addEventListener('hashchange', () => {
  const m = /^#\/c\/([^/]+)$/.exec(location.hash)
  if (m && isTownChannel(decodeURIComponent(m[1]))) try { sessionStorage.setItem(LAST_TOWN, decodeURIComponent(m[1])) } catch { /* ignore */ }
})

// ---- Create: one sheet; each row opens its form in the chosen channel -----------------------

export type CreateKind = 'message' | 'event' | 'meeting' | 'todo' | 'group' | 'proposal'
let pending: { channel: string; kind: CreateKind } | null = null
/** The form Create asked for, taken once by the channel it opened (Chat.tsx Conversation). */
export function takeCreate(channel: string): CreateKind | undefined {
  if (!pending || pending.channel !== channel) return undefined
  const k = pending.kind
  pending = null
  return k
}

const CREATE_ROWS: { k: CreateKind; title: string; line: (where: string) => string; icon: () => JSX.Element; cls: string }[] = [
  { k: 'message', title: 'Message', line: (w) => `Post in ${w}`, icon: MessageIcon, cls: 'c-msg' },
  { k: 'event', title: 'Event', line: () => 'A date, a place, who’s coming', icon: CalendarIcon, cls: 'c-ev' },
  { k: 'meeting', title: 'Meeting', line: () => 'In a room, online, or both', icon: ClockIcon, cls: 'c-meet' },
  { k: 'todo', title: 'To-do', line: () => 'Something someone can take on', icon: TodoIcon, cls: 'c-todo' },
  { k: 'group', title: 'Group', line: () => 'A smaller circle inside the channel', icon: HexIcon, cls: 'c-group' },
  { k: 'proposal', title: 'Proposal', line: () => 'Something to decide together', icon: DocIcon, cls: 'c-prop' },
]

/** Create (＋): WHERE = the channel on screen, else the Square; FIRST puts one row on top
 *  (Calendar: Event; Groups: Group). */
export function CreateSheet({ onClose, where, first }: { onClose: () => void; where?: string; first?: CreateKind }) {
  useVersion()
  const choices = [...inOrder(groups().filter((g) => isTownChannel(g.id))), ...groups().filter((g) => g.isPod && g.members.has(me()))]
  const start = where && choices.some((g) => g.id === where) ? where : choices.some((g) => g.id === 'general') ? 'general' : choices[0]?.id ?? 'general'
  const [ch, setCh] = useState(start)
  const g = group(ch)
  const label = g ? `${g.isPod ? '' : '# '}${g.name}` : '# ' + ch
  const rows = first ? [...CREATE_ROWS.filter((r) => r.k === first), ...CREATE_ROWS.filter((r) => r.k !== first)] : CREATE_ROWS
  return (
    <Sheet title="Create" onClose={onClose}>
      <label className="create-where">
        <span>Where</span>
        <select aria-label="Where" value={ch} onChange={(e) => setCh(e.target.value)}>
          {choices.map((c) => <option key={c.id} value={c.id}>{c.isPod ? c.name : '# ' + c.name}</option>)}
        </select>
        <ChevronDownIcon />
      </label>
      <div className="act-group create-rows">
        {rows.map((r) => (
          <button key={r.k} className={'act-row ' + r.cls} onClick={() => { pending = { channel: ch, kind: r.k }; onClose(); setTimeout(() => go('c/' + encodeURIComponent(ch)), 0) }}>
            <span className="create-ico"><r.icon /></span>
            <span className="t"><b>{r.title}</b><small>{r.line(label)}</small></span>
          </button>
        ))}
      </div>
    </Sheet>
  )
}

// ---- the bottom bar ----------------------------------------------------------------------

/** A text field has the focus, or the on-screen keyboard is up (visualViewport): the bar steps
 *  aside so it never sits on top of the keyboard, and the composer keeps the room. */
function useTyping(): boolean {
  const [typing, setTyping] = useState(false)
  useEffect(() => {
    const field = (el: EventTarget | null) => el instanceof HTMLElement && (el.tagName === 'TEXTAREA' || (el.tagName === 'INPUT' && !/^(checkbox|radio|button|submit|file|range|color)$/.test((el as HTMLInputElement).type)) || el.isContentEditable)
    const vv = window.visualViewport
    const check = () => setTyping(field(document.activeElement) || (!!vv && window.innerHeight - vv.height > 120))
    const off = () => setTimeout(check, 0) // focus moving to another field: no blink
    document.addEventListener('focusin', check)
    document.addEventListener('focusout', off)
    vv?.addEventListener('resize', check)
    return () => { document.removeEventListener('focusin', check); document.removeEventListener('focusout', off); vv?.removeEventListener('resize', check) }
  }, [])
  return typing
}

export function TabBar({ place }: { place?: Place }) {
  useVersion()
  const typing = useTyping()
  const route = useRoute()
  const [creating, setCreating] = useState(false)
  const guest = amGuest()
  const homeN = homeCount()
  const here = route[0] === 'c' ? route[1] : undefined
  const first: CreateKind | undefined = place === 'calendar' ? 'event' : place === 'groups' ? 'group' : undefined
  const Tab = ({ p, label, icon: I, to, n }: { p: Place; label: string; icon: () => JSX.Element; to: string; n?: number }) => (
    <button className={'tab' + (place === p ? ' on' : '')} aria-current={place === p ? 'page' : undefined} onClick={() => { if (place !== p) go(to) }}>
      <span className="tab-ico"><I />{n ? <span className="tab-n" aria-hidden="true">{n > 99 ? '99+' : n}</span> : null}</span>
      <span>{label}</span>
    </button>
  )
  return (
    <nav className={'tabbar' + (guest ? ' guest' : '')} aria-label="Places" hidden={typing && !creating}>
      <Tab p="home" label="Home" icon={HomeIcon} to="home" n={homeN} />
      <Tab p="calendar" label="Calendar" icon={CalendarIcon} to="calendar" />
      {!guest && <button className="tab create" aria-label="Create" onClick={() => setCreating(true)}><span className="create-btn"><PlusIcon /></span></button>}
      {!guest && <Tab p="groups" label="Groups" icon={PeopleIcon} to="groups" />}
      <Tab p="town" label="Town" icon={TownIcon} to={townPath()} />
      {creating && <CreateSheet onClose={() => setCreating(false)} where={place === 'town' ? here : undefined} first={first} />}
    </nav>
  )
}

/** On a laptop: the same places at the top of the left column. */
export function SidePlaces({ place }: { place?: Place }) {
  useVersion()
  const [creating, setCreating] = useState(false)
  const guest = amGuest()
  const homeN = homeCount()
  const route = useRoute()
  const here = route[0] === 'c' ? route[1] : undefined
  const Row = ({ p, label, icon: I, to, n }: { p: Place; label: string; icon: () => JSX.Element; to: string; n?: number }) => (
    <button className={'chan side-place' + (place === p ? ' on' : '')} aria-current={place === p ? 'page' : undefined} onClick={() => go(to)}>
      <span className="hash"><I /></span><span className="t"><b>{label}</b></span>
      {n ? <span className="badge lime">{n > 99 ? '99+' : n}</span> : null}
    </button>
  )
  return (
    <div className="side-places" role="navigation" aria-label="Places">
      {/* Home is "Today" in the channel list (the owner, 2026-11-17), its first row */}
      <Row p="home" label="Today" icon={HomeIcon} to="home" n={homeN} />
      <Row p="calendar" label="Calendar" icon={CalendarIcon} to="calendar" />
      {!guest && <Row p="groups" label="Groups" icon={PeopleIcon} to="groups" />}
      <Row p="town" label="Town" icon={TownIcon} to={townPath()} />
      <button className={'chan side-place' + (route[0] === 'dm' ? ' on' : '')} onClick={() => go('dm')}>
        <span className="hash"><MessageIcon /></span><span className="t"><b>Messages</b></span>
        {unreadTotal() ? <span className="badge lime">{unreadTotal()}</span> : null}
      </button>
      <button className={'chan side-place' + (route[0] === 'mentions' ? ' on' : '')} onClick={() => go('mentions')}>
        <span className="hash"><AtIcon /></span><span className="t"><b>Mentions</b></span>
        {mentionsNew() ? <span className="badge lime">{mentionsNew()}</span> : null}
      </button>
      {/* Create is the + at the top of the list (TownList's header) */}
    </div>
  )
}

// ---- what needs me ------------------------------------------------------------------------

const DAY = 86400e3
/** A time short enough for a row: "18:30", or "2:00 PM" where the locale says so. */
const hm = (d: Date) => d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
const startOfDay = (d = new Date()) => new Date(d.toDateString())
const sameDay = (a: Date, b: Date) => a.toDateString() === b.toDateString()

/** To-dos on me (taken by me, not done), the open ones nobody is on yet, and how many are open
 *  in my channels and groups. */
type MyTodo = { g: Group; title: string; id: string; t: Task }
function myTodos(): { mine: MyTodo[]; unclaimed: MyTodo[]; open: number } {
  const mine: MyTodo[] = [], unclaimed: MyTodo[] = []
  let open = 0
  for (const g of groups()) {
    if (!g.members.has(me()) && !isTownChannel(g.id)) continue
    for (const t of tasklist(g.id).tasks) {
      if (t.done) continue
      open++
      if (t.assignee === me()) mine.push({ g, title: t.title, id: t.id, t })
      else if (!t.assignee) unclaimed.push({ g, title: t.title, id: t.id, t })
    }
  }
  unclaimed.sort((a, b) => b.t.at - a.t.at)
  return { mine, unclaimed, open }
}

/** My part in an event, in words (no buttons on rows: the event screen holds the actions). */
function stateWords(e: CalEvent): string | undefined {
  const s = myState(e)
  const today = sameDay(e.start, new Date())
  if (s === 'host' || e.hosts.has(me())) return today ? 'you host · check-in today' : 'you host'
  if (s === 'ticket') return 'going'
  if (s === 'maybe') return 'maybe'
  if (s === 'applied') return 'applied'
  if (s === 'waitlist') return 'on the waitlist'
  if (e.meeting && e.invited.includes(me()) && s === 'none') return 'invited · not answered yet'
  return undefined
}
const isMine = (e: CalEvent) => { const s = myState(e); return s === 'host' || s === 'ticket' || s === 'maybe' || s === 'applied' || s === 'waitlist' || e.hosts.has(me()) }
const isInvited = (e: CalEvent) => e.meeting && e.invited.includes(me()) && myState(e) === 'none' && !e.hosts.has(me())
const upcomingEvents = () => calendar().filter((e) => e.status !== 'cancelled' && +(e.end ?? e.start) >= +startOfDay())

/** The events of the next 7 days I'm part of or invited to. */
function comingUp(): CalEvent[] {
  const end = +startOfDay() + 8 * DAY
  return upcomingEvents().filter((e) => +e.start < end && (isMine(e) || isInvited(e))).sort((a, b) => +a.start - +b.start)
}

/** Channels and groups with something unread, newest first. */
function recentActivity(): Group[] {
  return groups().filter((g) => !g.isEvent && g.id !== CHARTER_ID && channelUnread(g.id) > 0).sort((a, b) => channelNewest(b.id) - channelNewest(a.id))
}

/** The count on Home's tab: things waiting for me (requests, an agreement, my to-dos, mentions, messages). */
function homeCount(): number {
  if (amGuest()) return 0
  return requestCount() + (charter().length ? toAgree().length : 0) + myTodos().mine.length + mentionsNew() + unreadTotal()
}
const mentionsNew = () => mentions(99).filter((e) => isNewMention(e)).length

function shortWhen(ts: number): string {
  const d = new Date(ts * 1000)
  const now = new Date()
  if (sameDay(d, now)) return fmtTime(ts)
  if (+now - +d < 6 * DAY) return d.toLocaleDateString([], { weekday: 'short' })
  return d.toLocaleDateString([], { day: 'numeric', month: 'short' })
}
const lastLine = (id: string) => {
  const m = messages(id).at(-1)
  return m ? { text: `${m.ev.pubkey === me() ? 'You' : displayName(m.ev.pubkey)}: ${preview(m.text) || (m.media.length ? 'Photo' : '')}`, at: m.ev.created_at } : undefined
}

/** The belfry's name at the top of a place (with its logo); on Home and Calendar, a button.
 *  Home has ☰ there instead (LEFT), the same as a channel's: the drawer holds the belfry's name. */
function PlaceHead({ title, sub, right, left }: { title: string; sub?: string; right?: ReactNode; left?: ReactNode }) {
  const community = useCommunityName(NAME)
  const brand = useBranding()
  return (
    <div className="place-head">
      <div className="place-top">
        {left ?? <span className="place-belfry">{brand.logo ? <img className="brand-logo" src={brand.logo} alt="" /> : <Arms size={20} />}<span>{community}</span></span>}
        {right}
      </div>
      <h1 className="place-title">{title}</h1>
      {sub && <div className="place-sub">{sub}</div>}
    </div>
  )
}

/** Home's title: the date, "Tuesday, 17 November" (the owner, 2026-11-17: not "Today"). */
export const homeTitle = (d = new Date()) => d.toLocaleDateString([], { weekday: 'long', day: 'numeric', month: 'long' })

function DayChip({ d }: { d: Date }) {
  const today = sameDay(d, new Date())
  return (
    <div className={'day-chip' + (today ? ' today' : '')}>
      <small>{today ? 'Today' : d.toLocaleDateString([], { weekday: 'short' })}</small>
      <b>{d.getDate()}</b>
    </div>
  )
}

function EventLine({ e }: { e: CalEvent }) {
  const s = stateWords(e)
  const what = e.meeting ? (e.links.length && !e.location ? 'online meeting' : 'meeting') : e.location
  return (
    <button className="home-row" onClick={() => go(eventPath(e))}>
      <DayChip d={e.start} />
      <span className="t"><b>{e.title}</b><small>{[e.allDay ? 'All day' : hm(e.start), what, s].filter(Boolean).join(' · ')}</small></span>
      <ChevronRightIcon />
    </button>
  )
}

function ActivityRow({ g }: { g: Group }) {
  const n = channelUnread(g.id)
  const last = lastLine(g.id)
  return (
    <button className="home-row activity" onClick={() => go('c/' + encodeURIComponent(g.id))}>
      <span className="row-ico">{g.isPod ? <PodShape members={g.members.size} size={20} /> : channelIcon(g)}</span>
      <span className="t"><b>{g.name}</b><small>{last?.text ?? g.about}</small></span>
      <span className="row-end">{last && <small>{shortWhen(last.at)}</small>}{n > 0 && <span className="badge unread-n">{n > 99 ? '99+' : n}</span>}</span>
    </button>
  )
}

/** #/home (and the app's start): what needs me today. One layout, always (the owner,
 *  2026-11-17: no "all caught up"): the date, the To-dos / Mentions / Messages tiles (a shortcut
 *  even at zero), what waits for me when something does, then what's coming. Guests get their
 *  own (GuestHome). */
export function HomeScreen() {
  useVersion()
  useEffect(() => { startRequests() }, [])
  if (amGuest()) return <GuestHome />
  const asks = requestCount()
  const askNames = [...belfryRequests().map((p) => p.name), ...(isBelfryAdmin(me()) ? agentRequests().map((a) => `${a.name}, an agent`) : []), ...groupRequests().flatMap(({ list }) => list.map((a) => displayName(a.pubkey)))]
  const rules = charter()
  const fresh = rules.length ? toAgree() : []
  const todos = myTodos()
  const men = mentions(99).filter((e) => isNewMention(e))
  const menWhere = [...new Set(men.map((e) => displayName(e.pubkey)))].slice(0, 2).concat([...new Set(men.map((e) => group(e.tags.find((t) => t[0] === 'h')?.[1] ?? '')?.name).filter(Boolean) as string[])].slice(0, 1))
  const dms = threads().filter((t) => t.unread)
  const dmCount = unreadTotal()
  const dmNames = dms.slice(0, 2).map((t) => convName(t.conv).split(/[ @]/)[0])
  const soon = comingUp()
  const next = soon.length ? undefined : upcomingEvents().filter((e) => isMine(e) && +e.start > Date.now()).sort((a, b) => +a.start - +b.start)[0]
  const act = recentActivity()
  const search = <button className="icon-btn round" aria-label="Search" onClick={() => go('search')}><SearchIcon /></button>
  return (
    <div className="screen place home">
      <div className="scroll">
        <PlaceHead title={homeTitle()} left={<MenuButton />} right={search} />
        <div className="home-tiles">
          <button className={'tile' + (todos.mine.length ? '' : ' zero')} onClick={() => go('todos')}><span className="tile-ico c-todo"><TodoIcon /></span><b>{todos.mine.length || ''}</b><span>To-dos</span><small>{todos.mine.length ? `on you · ${todos.open} open` : todos.open ? `${todos.open} open` : 'Nothing new'}</small></button>
          <button className={'tile' + (men.length ? '' : ' zero')} onClick={() => go('mentions')}><span className="tile-ico c-ev"><AtIcon /></span><b>{men.length || ''}</b><span>Mentions</span><small>{men.length ? menWhere.join(', ') : 'Nothing new'}</small></button>
          <button className={'tile' + (dmCount ? '' : ' zero')} onClick={() => go('dm')}><span className="tile-ico c-msg"><MessageIcon /></span><b>{dmCount || ''}</b><span>Messages</span><small>{dmCount ? dmNames.join(', ') + (dms.length > 2 ? ` +${dms.length - 2}` : '') : 'Nothing new'}</small></button>
        </div>
        {asks > 0 && (
          <div className="home-card">
            <button className="home-row" onClick={() => go('members')}>
              <span className="row-ico"><PeopleIcon /></span>
              <span className="t"><b>{asks} asking to join</b><small>{askNames.slice(0, 3).join(' · ') || 'Requests waiting for you'}</small></span>
              <span className="badge lime">{asks}</span><ChevronRightIcon />
            </button>
          </div>
        )}
        {fresh.length > 0 && (
          <div className="home-card">
            <button className="home-row" onClick={() => go('charter')}>
              <span className="row-ico"><DocIcon /></span>
              <span className="t"><b>{fresh.length === 1 ? '1 new agreement to read' : `${fresh.length} new agreements to read`}</b><small>Charter: {fresh[0].title}</small>{fresh.length === 1 && fresh[0].about && <AgreementAbout text={fresh[0].about} more={false} />}</span>
              <ChevronRightIcon />
            </button>
          </div>
        )}
        {act.length > 0 && <>
          <div className="place-section"><h3>Recent activity</h3><button className="link" onClick={() => go(townPath())}>Town</button></div>
          <div className="home-card">{act.slice(0, 8).map((g) => <ActivityRow key={g.id} g={g} />)}</div>
        </>}
        {soon.length > 0 && <>
          <div className="place-section"><h3>Coming up</h3><button className="link" onClick={() => go('calendar')}>Calendar</button></div>
          <div className="home-card">{soon.slice(0, 5).map((e) => <EventLine key={e.coord} e={e} />)}</div>
        </>}
        {next && <>
          <div className="place-section"><h3>Next on your calendar</h3><button className="link" onClick={() => go('calendar')}>Calendar</button></div>
          <div className="home-card"><EventLine e={next} /></div>
        </>}
      </div>
    </div>
  )
}

// ---- a guest's Home ---------------------------------------------------------------------

const untilWords = (until?: number) => until ? new Date(until * 1000).toLocaleDateString([], { weekday: 'short', day: 'numeric', month: 'long' }) : ''

/** Channels I'm in as a guest. */
export const myGuestChannels = () => allGroups().filter((g) => !g.archived && (guestJoins(g.id).some((j) => j.pubkey === me() && !j.member) || guestInfo()?.channel === g.id) && !g.isEvent && g.id !== CHARTER_ID)
/** Open to everyone, read-only for guests: the charter and #issues / #requests, each only if it exists. */
const OPEN_IDS = ['issues', 'requests']

function GuestHome() {
  useVersion()
  const community = useCommunityName(NAME)
  useEffect(() => { void loadApplyStatus(); void loadRemoteCharter() }, [])
  const st = applyStatus()
  const name = realName(me()) || 'guest'
  const mine = myGuestChannels()
  const rules = remoteAgreements() ?? (charter().length ? charter() : [])
  const open = OPEN_IDS.map((id) => group(id)).filter((g): g is Group => !!g && !mine.some((m) => m.id === g.id))
  return (
    <div className="screen place home guest-home">
      <div className="scroll">
        <PlaceHead title={homeTitle()} sub={`Welcome, ${name} · ${st?.until ? `guest until ${untilWords(st.until)}` : 'guest'}`} left={<MenuButton />} />
        <ApplyCard st={st} community={community} />
        {mine.length > 0 && <>
          <div className="place-section"><h3>You were invited to</h3></div>
          <div className="home-card">{mine.map((g) => <ActivityRow key={g.id} g={g} />)}</div>
        </>}
        {threads().length > 0 && (
          <div className="home-card"><button className="home-row" onClick={() => go('dm')}><span className="row-ico"><MessageIcon /></span><span className="t"><b>Messages</b><small>{unreadTotal() ? `${unreadTotal()} unread` : 'Direct messages and e-mail'}</small></span><ChevronRightIcon /></button></div>
        )}
        {(rules.length > 0 || open.length > 0) && <>
          <div className="place-section"><h3>Open to everyone</h3></div>
          <div className="home-card">
            {rules.length > 0 && (
              <button className="home-row" onClick={() => go('charter')}>
                <span className="row-ico"><DocIcon /></span>
                <span className="t"><b>Charter</b><small>How we do things here · {rules.length === 1 ? '1 rule' : `${rules.length} rules`}</small></span>
                <ChevronRightIcon />
              </button>
            )}
            {open.map((g) => <ActivityRow key={g.id} g={g} />)}
          </div>
        </>}
      </div>
    </div>
  )
}

/** The guest's one big action, then (once sent) the request's state in plain words. */
function ApplyCard({ st, community }: { st: ApplyStatus | null; community: string }) {
  const app = st?.application
  if (app && app.state === 'declined') return (
    <div className="home-card apply-card"><div className="pad-card"><b>Your request to become a member</b><p className="hint">Not this time. You stay a guest{st?.until ? ` until ${untilWords(st.until)}` : ''}.</p></div></div>
  )
  if (app && (app.state === 'new' || app.state === 'pending')) {
    const need = app.needed || 0
    return (
      <div className="home-card apply-card">
        <div className="pad-card">
          <div className="hint">Your request to become a member</div>
          {st?.accept === 'vouches' && need > 0
            ? <><b className="apply-count">{Math.min(app.vouches, need)} of {need} vouches</b>
                <div className="progress" role="progressbar" aria-valuemin={0} aria-valuemax={need} aria-valuenow={Math.min(app.vouches, need)}><span style={{ width: `${Math.min(100, (100 * app.vouches) / need)}%` }} /></div>
                {app.vouchedBy.map((v) => <div key={v.pubkey} className="vouched"><Avatar pubkey={v.pubkey} size={24} /> {v.name || displayName(v.pubkey)} vouched for you</div>)}
                <p className="hint">Any member can vouch; a steward can also say yes. You stay a guest meanwhile, and your guest access doesn't end while the request is open.</p></>
            : <><b className="apply-count">Waiting for a steward</b><p className="hint">A steward answers your request. You stay a guest meanwhile, and your guest access doesn't end while the request is open.</p></>}
        </div>
      </div>
    )
  }
  return (
    <div className="home-card apply-card">
      <div className="pad-card">
        <b>Become a member of {community}</b>
        <p className="hint">Members see the whole town: the Townsquare, its events and groups.{st?.accept === 'vouches' ? ' Vouches from members or a steward’s yes make you one.' : ' A steward’s yes makes you one.'}</p>
        <button className="btn" onClick={() => go('apply')}>Apply to become a member</button>
      </div>
    </div>
  )
}

/** #/apply: the guest's request (board Join-Apply 1): the belfry's question, the saved e-mail,
 *  the charter ticked one by one (when there is one); Send waits for all of them. */
export function ApplyScreen() {
  useVersion()
  const community = useCommunityName(NAME)
  const [answer, setAnswer] = useState(() => { try { return localStorage.getItem('belfry-apply-draft') ?? '' } catch { return '' } })
  const [ticked, setTicked] = useState<Set<string>>(new Set())
  const [showRules, setShowRules] = useState(false)
  const [error, setError] = useState('')
  useEffect(() => { void loadRemoteCharter(); void loadApplyStatus() }, [])
  useEffect(() => { try { localStorage.setItem('belfry-apply-draft', answer) } catch { /* private mode */ } }, [answer])
  const rules = remoteAgreements() ?? []
  const saved = !!guestInfo()?.saved || allGroups().some((g) => guestJoins(g.id).some((j) => j.pubkey === me() && !j.unsaved))
  const st = applyStatus()
  const sent = !!st?.application && st.application.state !== 'declined'
  const allTicked = rules.every((r) => ticked.has(r.id))
  return (
    <div className="screen">
      <div className="topbar">
        <button className="icon-btn" aria-label="Back" onClick={() => back('home')}>‹</button>
        <h1>Become a member</h1>
      </div>
      <div className="scroll pad">
        {sent ? <><p>Your request is with {community}’s members and stewards.</p><button className="btn" onClick={() => go('home')}>Back to Home</button></> : (
          <Form onSubmit={async (e) => {
            e.preventDefault()
            setError('')
            try { await sendApplication(answer, [...ticked]); try { localStorage.removeItem('belfry-apply-draft') } catch { /* */ } go('home') } catch (x) { setError(x instanceof ApplyFailed ? x.message : APPLY_ERRORS.failed) }
          }}>
            <p className="hint apply-intro"><Arms size={14} /> {community} asks one question.{st?.accept === 'vouches' ? ' Members vouching, or a steward’s yes, makes you a member.' : ' A steward’s yes makes you a member.'}</p>
            <label className="field"><span>What brings you to {community}?</span>
              <textarea className="input" rows={5} maxLength={2000} value={answer} onChange={(e) => setAnswer(e.target.value)} /></label>
            <div className="card row" style={{ justifyContent: 'space-between' }}>
              <span><span className="hint" style={{ fontSize: 13 }}>E-mail</span><br />{saved ? 'Saved on your account' : 'Not saved yet'}</span>
              {saved ? <span className="saved-mark">✓ Saved</span> : <button type="button" className="btn small" onClick={() => askToJoin()}>Save it</button>}
            </div>
            {rules.length > 0 && (
              <button type="button" className="card row charter-pick" style={{ width: '100%', textAlign: 'left', justifyContent: 'space-between' }} onClick={() => setShowRules(true)}>
                <span><DocIcon /> <b>Our charter</b><br /><small className="hint">{ticked.size} of {rules.length} agreements ticked</small></span>
                {allTicked ? <span className="saved-mark">✓</span> : <ChevronRightIcon />}
              </button>
            )}
            <p className="hint">Your name and answer are shown to members; your e-mail only to stewards.</p>
            {error && <div className="error" role="alert">{error}</div>}
            <button className="btn" disabled={!answer.trim() || !saved || !allTicked}>Send request</button>
          </Form>
        )}
      </div>
      {showRules && (
        <Sheet title="Our charter" onClose={() => setShowRules(false)}>
          <p className="hint">Tick each one you agree to.</p>
          <div className="charter-list">
            {rules.map((r) => (
              <button key={r.id} type="button" className={'rule' + (ticked.has(r.id) ? ' on' : '')} role="checkbox" aria-checked={ticked.has(r.id)}
                onClick={() => setTicked((s) => { const n = new Set(s); if (n.has(r.id)) n.delete(r.id); else n.add(r.id); return n })}>
                <CheckCircleIcon on={ticked.has(r.id)} />
                <span className="t"><b>{r.title}</b>{r.about && <AgreementAbout text={r.about} />}</span>
              </button>
            ))}
          </div>
          <button className="btn" disabled={!allTicked} onClick={() => setShowRules(false)}>{allTicked ? 'Done' : `${ticked.size} of ${rules.length} agreed`}</button>
        </Sheet>
      )}
    </div>
  )
}

// ---- Calendar ----------------------------------------------------------------------------

type CalTab = 'mine' | 'invited' | 'discover' | 'public' | 'going'
const weekStart = (d: Date) => { const x = startOfDay(d); const wd = (x.getDay() + 6) % 7; x.setDate(x.getDate() - wd); return x }

function hue(id: string) { let h = 0; for (const c of id) h = (h * 31 + c.charCodeAt(0)) % 360; return h }

function CalRow({ e }: { e: CalEvent }) {
  const s = stateWords(e)
  const ch = e.channels.map((c) => group(c)?.name).filter(Boolean)[0]
  const where = e.meeting && e.links.length && !e.location ? 'Online meeting' : e.location
  return (
    <button className="cal-row" onClick={() => go(eventPath(e))}>
      <span className="cal-time"><b>{e.allDay ? 'All day' : hm(e.start)}</b>{!e.allDay && e.end && <small>{hm(e.end)}</small>}</span>
      <span className="cal-bar" style={{ background: `hsl(${hue(e.channels[0] ?? e.groupId)} 55% 45%)` }} />
      <span className="t"><b>{e.status === 'cancelled' && <span className="badge cancel">Cancelled</span>} {e.title}</b><small>{[where, ch, s ?? (e.visibility === 'public' ? 'open to all' : '')].filter(Boolean).join(' · ')}</small></span>
      <ChevronRightIcon />
    </button>
  )
}

/** #/calendar: My calendar (hosting, going, maybe, applied) · Invited · Discover; a guest's:
 *  Public events · Going. A week strip on top; tap a day to see only that day. */
export function CalendarPlace() {
  useVersion()
  const guest = amGuest()
  const [tab, setTab] = useState<CalTab>(guest ? 'public' : 'mine')
  const [week, setWeek] = useState(() => weekStart(new Date()))
  const [day, setDay] = useState<Date | null>(null)
  const [creating, setCreating] = useState(false)
  const pub = guest ? publicEvents() : []
  const all = useMemo(() => {
    const local = upcomingEvents()
    const byCoord = new Map(local.map((e) => [e.coord, e]))
    for (const e of pub) if (!byCoord.has(e.coord) && e.status !== 'cancelled' && +(e.end ?? e.start) >= +startOfDay()) byCoord.set(e.coord, e)
    return [...byCoord.values()].sort((a, b) => +a.start - +b.start)
  }, [calendar(), pub]) // eslint-disable-line react-hooks/exhaustive-deps
  const invited = all.filter(isInvited)
  const mineChannels = new Set(myGuestChannels().map((g) => g.id))
  const list = all.filter((e) => tab === 'mine' ? isMine(e) : tab === 'invited' ? isInvited(e) : tab === 'discover' ? !isMine(e) && !isInvited(e)
    : tab === 'going' ? isMine(e) : e.visibility === 'public' || e.channels.some((c) => mineChannels.has(c)) || mineChannels.has(e.groupId))
  const shown = day ? list.filter((e) => sameDay(e.start, day)) : list
  const days = Array.from({ length: 7 }, (_, i) => new Date(+week + i * DAY + 3600e3))
  const dot = (d: Date) => list.some((e) => sameDay(e.start, d)) ? (all.some((e) => sameDay(e.start, d) && isInvited(e)) ? 'ring' : 'dot') : ''
  // sections: Today, This week, Next week, Later (or the picked day)
  const today = startOfDay()
  const wkEnd = +weekStart(new Date()) + 7 * DAY
  const sections: [string, CalEvent[]][] = day ? [[day.toLocaleDateString([], { weekday: 'long', day: 'numeric', month: 'long' }), shown]] : [
    ['Today', shown.filter((e) => sameDay(e.start, new Date()) || (+e.start < +today && +(e.end ?? e.start) >= +today))],
    ['This week', shown.filter((e) => +e.start >= +today + DAY && +e.start < wkEnd)],
    ['Next week', shown.filter((e) => +e.start >= wkEnd && +e.start < wkEnd + 7 * DAY)],
    ['Later', shown.filter((e) => +e.start >= wkEnd + 7 * DAY)],
  ]
  const empty = { mine: 'Nothing on your calendar yet.', invited: 'No invitations waiting.', discover: 'No other events coming up.', public: 'No public events coming up.', going: 'You’re not going to anything yet.' }[tab]
  return (
    <div className="screen place calendar">
      <div className="scroll">
        <PlaceHead title="Calendar" right={!guest ? <button className="icon-btn round" aria-label="New event" onClick={() => setCreating(true)}><PlusIcon /></button> : undefined} />
        <div className="seg" role="tablist">
          {(guest ? [['public', 'Public events'], ['going', 'Going']] as [CalTab, string][] : [['mine', 'My calendar'], ['invited', 'Invited'], ['discover', 'Discover']] as [CalTab, string][]).map(([k, l]) => (
            <button key={k} role="tab" aria-selected={tab === k} className={tab === k ? 'on' : ''} onClick={() => { setTab(k); setDay(null) }}>{l}{k === 'invited' && invited.length > 0 && <span className="seg-n">{invited.length}</span>}</button>
          ))}
        </div>
        <div className="week">
          <div className="week-head">
            <b>{days[3].toLocaleDateString([], { month: 'long', year: 'numeric' })}</b>
            <span>
              <button className="icon-btn" aria-label="Previous week" onClick={() => { setWeek(new Date(+week - 7 * DAY)); setDay(null) }}>‹</button>
              <button className="icon-btn" aria-label="Next week" onClick={() => { setWeek(new Date(+week + 7 * DAY)); setDay(null) }}>›</button>
            </span>
          </div>
          <div className="week-days">
            {days.map((d) => (
              <button key={+d} className={'wd' + (sameDay(d, new Date()) ? ' today' : '') + (day && sameDay(d, day) ? ' on' : '')} aria-pressed={!!day && sameDay(d, day)} onClick={() => setDay(day && sameDay(d, day) ? null : d)}>
                <small>{d.toLocaleDateString([], { weekday: 'short' })}</small><b>{d.getDate()}</b><i className={dot(d)} aria-hidden="true" />
              </button>
            ))}
          </div>
        </div>
        {shown.length === 0 && <div className="empty">{day ? 'Nothing that day.' : empty}</div>}
        {sections.filter(([, l]) => l.length).map(([title, l]) => (
          <div key={title}>
            <div className="place-section"><h3>{title}</h3>{title === 'Today' && <span className="hint">{new Date().toLocaleDateString([], { weekday: 'long', day: 'numeric' })}</span>}</div>
            <div className="home-card">{l.map((e) => <CalRow key={e.coord} e={e} />)}</div>
          </div>
        ))}
        <div className="place-more"><button className="link" onClick={() => go('events')}>All events, past ones too</button></div>
      </div>
      {creating && <CreateSheet onClose={() => setCreating(false)} first="event" />}
    </div>
  )
}

// ---- Groups ------------------------------------------------------------------------------

type GroupTab = 'mine' | 'active' | 'all'

function GroupRow({ g }: { g: Group }) {
  const mine = g.members.has(me()) || g.admins.has(me())
  const n = mine ? channelUnread(g.id) : 0
  const last = lastLine(g.id)
  const parent = g.parent ? group(g.parent)?.name ?? g.parent : undefined
  const applied = g.applications.some((a) => a.pubkey === me())
  const sub = [parent, `${g.members.size} ${g.members.size === 1 ? 'member' : 'members'}`, !mine && g.closed ? (applied ? 'request sent' : 'by application') : ''].filter(Boolean).join(' · ')
  return (
    <div className="group-row">
      <button className="home-row" onClick={() => go('c/' + encodeURIComponent(g.id) + (mine ? '' : '/group'))}>
        <span className="row-ico round"><PodShape members={g.members.size} size={20} /></span>
        <span className="t"><b>{g.name}</b>{last && <small>{last.text}</small>}<small className="faint">{sub}</small></span>
        <span className="row-end">{last && <small>{shortWhen(last.at)}</small>}{n > 0 && <span className="badge unread-n">{n > 99 ? '99+' : n}</span>}</span>
      </button>
      {!mine && !g.closed && <Btn className="btn small join-btn" onClick={() => joinGroup(g.id)}>Join</Btn>}
    </div>
  )
}

/** #/groups: My groups (default) · Active (mine, a message in the last 30 days) · All (every
 *  group; Join / "by application" on the ones I'm not in). Newest activity first. */
export function GroupsScreen() {
  useVersion()
  const [tab, setTab] = useState<GroupTab>('mine')
  const [creating, setCreating] = useState(false)
  const pods = groups().filter((g) => g.isPod)
  const mine = pods.filter((g) => g.members.has(me()) || g.admins.has(me()))
  const since = Date.now() / 1000 - 30 * 86400
  const recent = (g: Group) => lastLine(g.id)?.at ?? g.lastActivity
  const list = (tab === 'mine' ? mine : tab === 'active' ? mine.filter((g) => recent(g) >= since) : pods).sort((a, b) => recent(b) - recent(a))
  const arch = archivedGroups().filter((g) => g.isPod).length
  const empty = { mine: 'You’re not in any group yet. All shows every group here.', active: 'None of your groups had a message in the last 30 days.', all: 'No groups yet. Make the first one with ＋.' }[tab]
  return (
    <div className="screen place groups">
      <div className="scroll">
        <PlaceHead title="Groups" right={<button className="icon-btn round" aria-label="New group" onClick={() => setCreating(true)}><PlusIcon /></button>} />
        <div className="seg" role="tablist">
          {([['mine', 'My groups'], ['active', 'Active'], ['all', 'All']] as [GroupTab, string][]).map(([k, l]) => (
            <button key={k} role="tab" aria-selected={tab === k} className={tab === k ? 'on' : ''} onClick={() => setTab(k)}>{l}</button>
          ))}
        </div>
        {list.length === 0 ? <div className="empty">{empty}</div> : <div className="home-card">{list.map((g) => <GroupRow key={g.id} g={g} />)}</div>}
        {arch > 0 && (
          <div className="home-card">
            <button className="home-row" onClick={() => go('archived')}><span className="row-ico"><ArchiveIcon /></span><span className="t"><b>Archived</b></span><span className="row-end"><small>{arch}</small></span><ChevronRightIcon /></button>
          </div>
        )}
      </div>
      {creating && <CreateSheet onClose={() => setCreating(false)} first="group" />}
    </div>
  )
}

// ---- #/todos: the to-dos on me, then the open ones nobody is on yet (board todos/my-todos) ------

export function TodosScreen() {
  useVersion()
  const now = useNow()
  const { mine, unclaimed } = myTodos()
  const row = ({ g, t }: MyTodo) => {
    const busy = isWorking(t, now)
    const where = g.isPod ? g.name : '#' + g.name
    const line = [where, busy ? `working on it · ${Math.max(1, Math.round((now - (workingSince(t) ?? now)) / 60))} min` : '', t.comments.length ? `${t.comments.length} ${t.comments.length === 1 ? 'comment' : 'comments'}` : ''].filter(Boolean).join(' · ')
    return (
      <div key={t.id} className="home-row todo-row">
        <Btn className={'check' + (t.assignee ? ' claimed' : '') + (busy ? ' working' : '')} aria-label={`Mark “${t.title}” as done`} onClick={() => taskAction(g.id, t.id, 'done')}>
          {busy ? <WorkRing since={workingSince(t) ?? now} now={now} /> : ''}
        </Btn>
        <button className="todo-open" onClick={() => go(`c/${encodeURIComponent(g.id)}/todo/${t.id}`)}>
          <span className="t"><b>{t.title}</b><small>{line}</small></span><ChevronRightIcon />
        </button>
      </div>
    )
  }
  return (
    <div className="screen">
      <div className="topbar"><button className="icon-btn" aria-label="Back" onClick={() => back('home')}>‹</button><h1>To-dos</h1></div>
      <div className="scroll todos-screen">
        {mine.length === 0 && unclaimed.length === 0 && <div className="empty">Nothing to do. To-dos from your channels and groups show up here.</div>}
        {mine.length > 0 && <>
          <div className="place-section"><h3>On you</h3></div>
          <div className="home-card">{mine.map(row)}</div>
        </>}
        {unclaimed.length > 0 && <>
          <div className="place-section"><h3>Open · nobody on it yet</h3></div>
          <div className="home-card">{unclaimed.map(row)}</div>
        </>}
        {(mine.length > 0 || unclaimed.length > 0) && <p className="hint todos-foot">Open to-dos in the channels and groups you are in. Tap the ring to tick one off; tap the row to open it.</p>}
      </div>
    </div>
  )
}

/** Members (and their requests) in the drawer and its screen. */
export const membersName = () => MEMBERS

// demo hooks (window.demoApplyStatus)
export { demoApplyStatus }
