// A channel's header, simplified (the owner, 2026-11-17, after the iPhone app's Square):
//  - under the name, one short subtitle: 🔒 (members only) or 🌐 (public) · "6 active" · photos ·
//    groups (each pair only when there are some). No other words.
//  - the line under the header only when it must catch the eye (ChannelLine.SummaryLine).
//  - pulled down: the description (and who reads it, its address), a pinned message not hidden yet,
//    then a menu: Pinned, Events, To decide, To-dos, Photos, Files, Groups, Topics, Members,
//    Notifications, Settings. Each row opens its own screen (c/<id>/<page>), which slides in from
//    the right on a phone; ‹ goes back up (never pushes). Members holds Invite. Settings: those
//    who may edit the channel. The others show when they hold at least one thing.
// "Channel info" is gone; its parts live here (old links c/<id>/info… land on these screens).
import { replaceHash } from '../core/nav'
import { isVisual } from '../core/pictures'
import { useState, type ReactNode } from 'react'
import { canAdmin } from '../core/access'
import { groupRole } from '../core/checkin'
import { reading } from '../core/guestmode'
import { isGuest } from '../core/invite'
import { todoParts, todoPartsText, channelIcon, displayName, dropLinks, group, isPublicChannel, messages, recentlyActive, topics, ANNOUNCE, isAnnouncement, type Group, followsIt } from '../core/model'
import { leaveGroup } from '../core/actions'
import { useVersion } from '../core/store'
import { me } from '../core/sync'
import { Avatar, Btn, fmtDay, go, MediaGrid, up, useWide } from './common'
import { channelFacts, hiddenPins, hidePin, mayPin } from './ChannelLine'
import { visibilityText } from './ChannelHeader'
import { ChannelSettingsForm, Files, Log, Members, Tasks } from './ChannelInfo'
import { NotifyOptions, NotifySheet, useNotifyRow } from './Follow'
import { InviteSheet } from './Invite'
import { Markdown } from './markdown'
import { RichText, TopicsBar } from './Topics'
import { BellIcon, CalendarIcon, ChevronRightIcon, DocIcon, GearIcon, GlobeIcon, ImageIcon, LinkIcon, PeopleIcon, PersonPlusIcon, PlusIcon, PodShape, PushPinIcon, TodoIcon, ClockIcon } from './icons'
import { stateLabel } from './Calendar'

const LockIcon = () => <svg className="ico" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><rect x="5" y="11" width="14" height="10" rx="2" /><path d="M8 11V7a4 4 0 0 1 8 0v4" /></svg>
const HashIcon = () => <svg className="ico" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" aria-hidden="true"><path d="M5 9h15M4 15h15M10 3 8 21M16 3l-2 18" /></svg>

/** May edit the channel: its admins, admins by role, the belfry's admins. */
export const mayEdit = (g?: Group) => !!g && (canAdmin(g) || groupRole(g.id, me()) === 'admin')

/** The pages a channel's menu opens (c/<id>/<page>). */
export const CHANNEL_PAGES = new Set(['pinned', 'proposals', 'todos', 'photos', 'files', 'groups', 'topics', 'members', 'notifications', 'settings', 'log'])

/** What each row counts. */
export function menuCounts(id: string) {
  const f = channelFacts(id)
  const media = [...new Map(messages(id).flatMap((m) => m.media).map((x) => [x.url, x])).values()]
  const files = media.filter((m) => !isVisual(m)).length + dropLinks(id).length
  const g = f.g
  const tops = topics(id).filter((t) => t !== ANNOUNCE).length + (messages(id).some((m) => isAnnouncement(m, g)) ? 1 : 0)
  const visual = media.filter(isVisual).length
  return { f, files, topics: tops, photos: visual, active: recentlyActive(id).length }
}

/** Under the channel's name: 🔒/🌐 · N active · photos · groups. */
export function ChannelSubtitle({ g }: { g: Group }) {
  useVersion()
  const c = menuCounts(g.id)
  const vis = g.isPod || g.isProposal ? null : isPublicChannel(g) ? <span className="sub-vis" aria-label="Public"><GlobeIcon /></span> : <span className="sub-vis" aria-label="Members only"><LockIcon /></span>
  return (
    <small className="chan-sub">
      {vis}
      <span className="sub-active">{c.active} active</span>
      {c.photos > 0 && <span className="sub-count" aria-label={`${c.photos} photos`}><ImageIcon />{c.photos}</span>}
      {c.f.pods.length > 0 && <span className="sub-count" aria-label={`${c.f.pods.length} groups`}><PodShape members={6} size={13} />{c.f.pods.length}</span>}
    </small>
  )
}

type Row = { page: string; icon: ReactNode; title: string; n?: number | string; hint?: string; to?: string }

/** The header pulled down: description, who reads it, a pinned message, then the menu. */
export function ChannelMenu({ id, onPick }: { id: string; onPick: () => void }) {
  useVersion()
  const [, redraw] = useState(0)
  const [notify, setNotify] = useState(false)
  const notifyRow = useNotifyRow(id)
  const c = menuCounts(id)
  const f = c.f
  const g = f.g
  if (!g) return null
  const parent = g.parent ? group(g.parent) : undefined
  const hidden = hiddenPins()
  const pin = f.pins.find((m) => !hidden.includes(m.ev.id))
  const guestish = reading() || isGuest(me())
  const openTodos = f.todos.length
  const rows: Row[] = [
    { page: 'pinned', icon: <PushPinIcon />, title: 'Pinned', n: f.pins.length },
    { page: 'events', icon: <CalendarIcon />, title: 'Events', n: f.events.length, hint: f.events.find((e) => !stateLabel(e) && e.status !== 'cancelled') ? 'Not answered yet' : undefined },
    { page: 'proposals', icon: <DocIcon />, title: 'To decide', n: f.proposals.length, hint: f.proposals.some(f.needsVote) ? 'Your vote' : undefined },
    { page: 'todos', icon: <TodoIcon />, title: 'To-dos', n: f.allTasks.length ? openTodos : 0, hint: todoPartsText(todoParts(f.allTasks)) || undefined }, // board channels/header-todos
    { page: 'photos', icon: <ImageIcon />, title: 'Photos', n: c.photos },
    { page: 'files', icon: <LinkIcon />, title: 'Files', n: c.files },
    { page: 'groups', icon: <PodShape members={6} size={20} />, title: 'Groups', n: f.pods.length },
    { page: 'topics', icon: <HashIcon />, title: 'Topics', n: c.topics },
  ].filter((r) => (r.n as number) > 0 || (r.page === 'todos' && f.allTasks.length > 0))
  const asking = g.isPod && canAdmin(g) ? g.applications.length : 0
  // a channel's people are its followers (Xavier, 23 Nov); a group keeps its members
  rows.push({ page: 'members', icon: <PeopleIcon />, title: followsIt(g) ? 'Followers' : 'Members', n: g.members.size, hint: asking ? `${asking} asking to join` : undefined })
  if (!guestish) rows.push({ page: 'notifications', icon: <BellIcon on={notifyRow.on} />, title: 'Notifications', n: notifyRow.value })
  // the activity log: a row like the others (Xavier, 23 Nov: "in its own menu as 'Activity log'")
  if (f.allTasks.length + messages(id).length > 0) rows.push({ page: 'log', icon: <ClockIcon />, title: 'Activity log' })
  if (mayEdit(g) && !g.archived) rows.push({ page: 'settings', icon: <GearIcon />, title: 'Settings' })
  // Notifications is a sheet over the channel (three choices, applied on tap); the others are screens
  const open = (r: Row) => { if (r.page === 'notifications') { setNotify(true); return } onPick(); go(`c/${encodeURIComponent(id)}/${r.page}`) }
  return (
    <div className="pull cmenu">
      {g.about && <Markdown className="pull-about" text={g.about} />}
      {!g.isPod && !g.isProposal && !g.isEvent && (() => { const v = visibilityText(g); return (
        <div className="pull-vis"><b>{v.title}</b>{v.lines.map((l) => <span key={l}>{l}</span>)}</div>
      ) })()}
      {(g.isPod || g.isProposal) && parent && (
        <button className="link pull-parent" onClick={() => { onPick(); go('c/' + encodeURIComponent(parent.id)) }}>{g.isProposal ? 'Proposal' : 'Group'} in {parent.isPod ? '' : '# '}{parent.name}{g.isPod ? (g.inviteOnly ? ' · private' : g.closed ? ' · by request' : ' · open to members') : ''}</button>
      )}
      {g.isPod && g.closed && g.prompt && <p className="hint pull-prompt">Question for people who ask to join: {g.prompt}</p>}
      {pin && (
        <div className="pull-pin first">
          <PushPinIcon />
          <div>
            <RichText text={pin.text} groupId={id} />
            <small>{displayName(pin.ev.pubkey)} · {fmtDay(new Date(pin.ev.created_at * 1000))} · <button className="link" onClick={() => { onPick(); go(`c/${encodeURIComponent(id)}/m/${pin.ev.id}`) }}>Show in the channel</button>
              {' · '}<button className="link" onClick={() => { hidePin(pin.ev.id); redraw((x) => x + 1) }}>Got it, hide</button></small>
          </div>
        </div>
      )}
      <nav className="act-group cmenu-rows" aria-label={`${g.name}: more`}>
        {rows.map((r) => (
          <button key={r.page} type="button" className={'act cmenu-row row-' + r.page} onClick={() => open(r)}>
            <span className="cmenu-ico">{r.icon}</span>
            <span className="t"><b>{r.title}</b>{r.hint && <small>{r.hint}</small>}</span>
            {r.n !== undefined && r.n !== '' && <span className="v">{r.n}</span>}
            <ChevronRightIcon />
          </button>
        ))}
      </nav>
      <div className="cmenu-foot">
        {g.isPod && g.members.has(me()) && <LeaveLink g={g} />}
      </div>
      {notify && <NotifySheet channel={id} onClose={() => setNotify(false)} />}
    </div>
  )
}

/** Leave a group: asked in the app. */
function LeaveLink({ g }: { g: Group }) {
  const [asking, setAsking] = useState(false)
  if (!asking) return <button className="link danger" onClick={() => setAsking(true)}>Leave {g.name}…</button>
  return (
    <span className="leave-ask">Leave {g.name}? <Btn className="link danger" onClick={async () => { await leaveGroup(g.id); setAsking(false) }}>Leave</Btn> · <button className="link" onClick={() => setAsking(false)}>Stay</button></span>
  )
}

const TITLES: Record<string, string> = { pinned: 'Pinned', proposals: 'To decide', todos: 'To-dos', photos: 'Photos', files: 'Files', groups: 'Groups', topics: 'Topics', members: 'Members', notifications: 'Notifications', settings: 'Settings', log: 'Activity log' }

/** c/<id>/<page>: one of the menu's screens. */
export function ChannelPage({ id, page }: { id: string; page: string }) {
  useVersion()
  const wide = useWide()
  const g = group(id)
  const [inviting, setInviting] = useState(false)
  const parent = 'c/' + encodeURIComponent(id)
  const name = g ? `${channelIcon(g)}${g.isPod || g.isProposal ? ' ' : ''}${g.name}` : id
  const mayInvite = !!g && !g.archived && !reading()
  const f = channelFacts(id)
  let body: ReactNode = null
  if (!g) body = <div className="empty">This channel isn’t on this device.</div>
  else if (page === 'pinned') body = <Pinned id={id} />
  else if (page === 'proposals') body = (
    <div className="act-group page-rows">
      {f.proposals.length === 0 && <div className="empty">Nothing to decide right now.</div>}
      {f.proposals.map((p) => (
        <button key={p.groupId} className="act page-row" onClick={() => go('c/' + encodeURIComponent(p.groupId))}>
          <DocIcon /><span className="t"><b>{p.title}</b><small>{p.status[0].toUpperCase() + p.status.slice(1)} · version {p.version}</small></span>
          {f.needsVote(p) ? <span className="line-pill ask">Vote</span> : p.poll?.votes.get(me()) && p.status === 'voting' ? <span className="line-pill">Voted</span> : null}
          <ChevronRightIcon />
        </button>
      ))}
    </div>
  )
  else if (page === 'todos') body = <Tasks groupId={id} />
  else if (page === 'photos') {
    const visual = [...new Map(messages(id).flatMap((m) => m.media).map((x) => [x.url, x])).values()].filter(isVisual).reverse()
    body = <div className="pad">{visual.length ? <MediaGrid media={visual} gallery /> : <div className="empty">Photos posted here show up here.</div>}</div>
  }
  else if (page === 'files') body = <Files groupId={id} only="files" />
  else if (page === 'groups') body = (
    <div className="act-group page-rows">
      {f.pods.length === 0 && <div className="empty">No groups in this channel yet.</div>}
      {f.pods.map((p) => (
        <button key={p.id} className="act page-row" onClick={() => go(p.members.has(me()) ? 'c/' + encodeURIComponent(p.id) : `c/${encodeURIComponent(p.id)}/group`)}>
          <PodShape members={p.members.size} size={20} /><span className="t"><b>{p.name}</b><small>{p.members.size} {p.members.size === 1 ? 'member' : 'members'} · {p.inviteOnly ? 'private' : p.closed ? 'by request' : 'open'}{p.members.has(me()) ? ' · you’re in' : ''}</small></span><ChevronRightIcon />
        </button>
      ))}
    </div>
  )
  else if (page === 'topics') body = <div className="pad pull-topics"><TopicsBar groupId={id} onOpen={(t) => replaceHash('#/' + parent + (t ? '/t/' + encodeURIComponent(t) : ''))} /></div>
  else if (page === 'members') body = <><ActiveLine id={id} /><Members groupId={id} /></>
  else if (page === 'notifications') body = <div className="pad"><NotifyOptions channel={id} /></div>
  else if (page === 'settings') body = mayEdit(g) && !g.archived ? <div className="pad"><ChannelSettingsForm key={id} id={id} onDone={() => up(parent)} /></div> : <div className="empty">Only the channel’s admins change its settings.</div>
  else if (page === 'log') body = <Log groupId={id} />
  return (
    <div className={'screen chan-page' + (wide ? '' : ' slide-in')}>
      <div className="topbar">
        <button className="icon-btn" aria-label="Back" onClick={() => up(parent)}>‹</button>
        <h1>{page === 'members' && followsIt(g) ? 'Followers' : TITLES[page] ?? page}<small>{name}</small></h1>
        {/* "add one" is an icon top right, like Groups' + (Xavier, 2026-11-22; canvas v185) */}
        {page === 'members' && mayInvite && <button className="icon-btn round invite-btn" aria-label="Invite someone" title="Invite someone" onClick={() => setInviting(true)}><PlusIcon /></button>}
      </div>
      <div className="scroll">{body}</div>
      {inviting && <InviteSheet id={id} onClose={() => setInviting(false)} />}
    </div>
  )
}

/** Who opened or wrote here in the last 30 days, on top of Members. */
function ActiveLine({ id }: { id: string }) {
  const active = recentlyActive(id)
  if (!active.length) return null
  return (
    <div className="chead-people pad-x active-line" aria-label={`${active.length} active this month`}>
      {active.slice(0, 12).map((a) => <Avatar key={a.pubkey} pubkey={a.pubkey} size={28} />)}
      {active.length > 12 && <span className="more">+{active.length - 12}</span>}
      <span className="hint" style={{ fontSize: 13, marginLeft: 8 }}>{active.length} active in the last 30 days</span>
    </div>
  )
}

function Pinned({ id }: { id: string }) {
  const [, redraw] = useState(0)
  const g = group(id)
  const pins = channelFacts(id).pins
  const hidden = hiddenPins()
  void mayPin(g)
  if (!pins.length) return <div className="empty">Nothing pinned.</div>
  return (
    <div className="pad">
      {pins.map((m) => (
        <div key={m.ev.id} className="pull-pin">
          <PushPinIcon />
          <div>
            <RichText text={m.text} groupId={id} />
            <small>{displayName(m.ev.pubkey)} · {fmtDay(new Date(m.ev.created_at * 1000))} · <button className="link" onClick={() => go(`c/${encodeURIComponent(id)}/m/${m.ev.id}`)}>Show in the channel</button>
              {!hidden.includes(m.ev.id) && <> · <button className="link" onClick={() => { hidePin(m.ev.id); redraw((x) => x + 1) }}>Got it, hide</button></>}</small>
          </div>
        </div>
      ))}
    </div>
  )
}

