import { nip19 } from 'nostr-tools'
import { FollowButton } from './Follow'
import { address, unreadTotal } from '../core/dm'
import { useBranding } from '../core/branding'
import type React from 'react'
import { Fragment, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { addTask, createChannel, createPod, DEFAULT_PROMPT, deleteMessage, editMessage, joinGroup, pinMessage, taskAction, postMessage, postThreadReply, react, topEmojis } from '../core/actions'
import { ANNOUNCE, NADDR, webPage, isPublicChannel, calendar, eventLink, eventLinksIn, eventCoordsOf, eventByCoord, eventByLinkLocal, channelIcon, channelRef, plainRefs, group, guestJoins, type GuestJoin, isAnnouncement, displayName, eventCoordOf, groups, hashtags, messages, podOf, proposalOf, profiles, reactions, threadReplies, threadsOf, inboxStates, pinned, tasklist, isWorking, workingSince, type Task, type TaskStep, type CardState, type Group, type Message, type Reaction, type ThreadInfo } from '../core/model'
import { all, outboxItems, status, useVersion } from '../core/store'
import { joinWithAccount, me, retry, signedFetch } from '../core/sync'
import { Avatar, fmtDay, fmtTime, go, MediaGrid, Sheet, SyncDot, Btn, Form, up, useFormDraft, useNow, WorkRing } from './common'
import { EventCard, NewEventSheet } from './Calendar'
import { MeetingSheet } from './Meeting'
import { RecorderSheet, RecordingBar } from './Recorder'
import { canRecord } from '../core/recorder'
import { yourEvents } from './Events'
import { eventByLink as eventByLinkAny } from '../core/events'
import { RichText } from './Topics'
import { newMentions, preview } from './Mentions'
import { NewProposalSheet, ProposalCard } from './Proposals'
import { cardOf, FullMail, InboxCardView, mailOf, mailText, SentCardView, sentOf, ViaMail } from './MailPost'
import { knownGroupsKey } from '../core/dm'
import { BELFRY, NAME, THEME } from '../config'

/** The groups relay is connected: a send that is retried says so, not "waiting for connection". */
const relaysUp = () => status.relays[BELFRY.groupsRelay] === 'up'
import { AtIcon, MicIcon, CalendarIcon, ChevronRightIcon, CloseIcon, DocIcon, HexIcon, ImageIcon, MegaphoneIcon, CopyIcon, GearIcon, GlobeIcon, LinkIcon, MailIcon, MenuIcon, MoreIcon, PencilIcon, PeopleIcon, PushPinIcon, PodShape, TodoIcon, ReplyIcon, SearchIcon, SmilePlusIcon, ThreadIcon, TrashIcon } from './icons'
import { closeDrawer, toggleDrawer } from './drawer'
import { ChannelHeader } from './ChannelHeader'
import { agentOwner, canAdmin, isAdmin } from '../core/access'
import { coHostToken, guestInfo } from '../core/auth'
import { askToJoin, reading, useGuestMode } from '../core/guestmode'
import { GuestBar, JoinSheet } from './GuestJoin'
import { InviteSheet } from './Invite'
import { mayPin } from './ChannelLine'
import { inChannel, inviteMember, isGuest } from '../core/invite'
import { openProfile } from './Profile'

/** The `a` tags of the events a message's text links to (https://<domain>/events/<link>): the
 *  ones this device knows, or the belfry told it about. Receivers resolve the rest themselves. */
function eventTags(text: string): string[][] {
  return eventLinksIn(text).map((l) => eventByLinkAny(l).e?.coord).filter((c): c is string => !!c).map((c) => ['a', c, BELFRY.mainRelay])
}

// ---- channel list (the "server sidebar", full screen on a phone) ------------

/** The community's name from the belfry (/.well-known/belfry.json), else the domain. */
let communityName = ''
let asked = false
export function useCommunityName(fallback: string) {
  const [, bump] = useState(0)
  useEffect(() => {
    if (asked || THEME !== 'belfry') return
    asked = true
    fetch(`https://${BELFRY.domain}/.well-known/belfry.json`).then((r) => r.json()).then((j) => {
      if (typeof j?.name === 'string' && j.name.trim()) { communityName = j.name.trim().slice(0, 60); bump((n) => n + 1) }
    }).catch(() => {})
  }, [])
  return communityName || fallback
}

/** On a laptop: who is signed in, bottom left (opens Me). */
function SideUser() {
  useVersion()
  const p = profiles().get(me())
  return (
    <button className="side-user" onClick={() => closeDrawer('me')} aria-label="Me: your profile and settings">
      <span className="me-btn"><Avatar pubkey={me()} size={36} /><SyncDot /></span>
      <span className="t"><b>{p?.name ?? displayName(me())}</b><small>{address()}</small></span>
      <span className="hint"><GearIcon /></span>
    </button>
  )
}

export function ChannelList({ current, side = false, section, drawer = false }: { current?: string; side?: boolean; section?: string; drawer?: boolean }) {
  useVersion()
  const title = useCommunityName(side ? NAME : 'Channels')
  const brand = useBranding()
  const [creating, setCreating] = useState(false)
  const all = groups()
  const chans = all.filter((g) => !g.isEvent && !g.isPod && !g.isProposal)
  const mineEvents = yourEvents()
  const pods = all.filter((g) => g.isPod || g.isProposal)
  const ids = new Set(all.map((g) => g.id))
  const last = (id: string) => {
    const m = messages(id).at(-1)
    return m ? `${displayName(m.ev.pubkey)}: ${preview(m.text) || (m.media.length ? 'Photo' : '')}` : undefined
  }
  // public channels first, then the members' (Xavier, 2026-11-11); pods and proposals stay under theirs
  const pub = chans.filter((g) => isPublicChannel(g))
  const members = chans.filter((g) => !isPublicChannel(g))
  const next = calendar().filter((e) => e.status !== 'cancelled' && +e.start > Date.now()).sort((a, b) => +a.start - +b.start)[0]
  const fresh = newMentions()
  const Row = ({ g, depth = 0 }: { g: Group; depth?: number }) => (
    <button className={'chan' + (g.id === current ? ' on' : '')} style={depth ? { paddingLeft: 16 + depth * 18 } : undefined}
      onClick={() => closeDrawer('c/' + encodeURIComponent(g.id))}>
      <span className="hash">{channelIcon(g)}</span>
      <span className="t"><b>{g.name}</b><small>{last(g.id) ?? g.about}</small></span>
    </button>
  )
  // pods nest under the channel or pod they were posted in: a tree, as deep as it goes
  const Tree = ({ g, depth = 0, seen = new Set<string>() }: { g: Group; depth?: number; seen?: Set<string> }) => (
    <>
      <Row g={g} depth={depth} />
      {pods.filter((x) => x.parent === g.id && !seen.has(x.id)).map((x) =>
        <Tree key={x.id} g={x} depth={depth + 1} seen={new Set([...seen, g.id])} />)}
    </>
  )
  const orphans = pods.filter((x) => !x.parent || !ids.has(x.parent))
  return (
    <div className="screen">
      <div className="topbar">
        {drawer && <button className="icon-btn menu-btn drawer-close" aria-label="Close the channels" aria-expanded="true" onClick={() => closeDrawer()}><MenuIcon /></button>}
        <h1 style={{ paddingLeft: drawer ? 0 : 8 }}>{side && brand.logo && <img className="brand-logo" src={brand.logo} alt="" />}{side ? title : 'Channels'}</h1>
        <button className="icon-btn" aria-label="New channel" onClick={() => setCreating(true)}>＋</button>

      </div>
      <div className="scroll">
        {side && brand.cover && <img className="brand-cover" src={brand.cover} alt="" />}
        <button className={'chan' + (section === 'events' && !current ? ' on' : '')} onClick={() => closeDrawer('events')}>
          <span className="hash"><CalendarIcon /></span>
          <span className="t"><b>Upcoming events</b><small>{next ? `${fmtDay(next.start)} · ${next.title}` : 'Upcoming, yours, the ones you host'}</small></span>
        </button>
        <button className={'chan' + (section === 'mentions' ? ' on' : '')} onClick={() => closeDrawer('mentions')}>
          <span className="hash"><AtIcon /></span>
          <span className="t"><b>@Mentions</b><small>Where people named you, in every channel</small></span>
          {fresh > 0 && <span className="badge lime">{fresh}</span>}
        </button>
        <button className={'chan' + (section === 'dm' ? ' on' : '')} onClick={() => closeDrawer('dm')}>
          <span className="hash"><MailIcon unread={unreadTotal() > 0} /></span>
          <span className="t"><b>Direct messages</b><small>And e-mail{address() ? ` · ${address()}` : ''}</small></span>
          {unreadTotal() > 0 && <span className="badge lime">{unreadTotal()}</span>}
        </button>
        {pub.length > 0 && <div className="section-title">Public channels <span>{pub.length}</span></div>}
        {pub.map((g) => <Tree key={g.id} g={g} />)}
        {(members.length > 0 || orphans.length > 0) && <div className="section-title">Members only <span>{members.length + orphans.length}</span></div>}
        {members.map((g) => <Tree key={g.id} g={g} />)}
        {orphans.map((g) => <Tree key={g.id} g={g} />)}
        {chans.length === 0 && <div className="empty">No channels yet. They appear here once the relay answers.</div>}
        {mineEvents.length > 0 && <div className="section-title">Your events <span>{mineEvents.length}</span></div>}
        {mineEvents.map((e) => (
          <button key={e.coord} className={'chan' + (e.groupId === current || (section === 'events' && current === eventLink(e)) ? ' on' : '')} onClick={() => closeDrawer('events/' + encodeURIComponent(eventLink(e)))}>
            <span className="hash"><CalendarIcon /></span>
            <span className="t"><b>{e.title}</b><small>{fmtDay(e.start)}{e.status === 'cancelled' ? ' · cancelled' : last(e.groupId) ? ' · ' + last(e.groupId) : ''}</small></span>
          </button>
        ))}
        <div style={{ padding: 16 }}>
          <button className="btn ghost" onClick={() => setCreating(true)}>＋ Create a channel</button>
        </div>
      </div>
      {side && <SideUser />}
      {creating && <NewChannel onClose={() => setCreating(false)} />}
    </div>
  )
}

function NewChannel({ onClose }: { onClose: () => void }) {
  const [d, setD, clearDraft] = useFormDraft('channel', () => ({ name: '', about: '' }))
  const { name, about } = d
  const setName = (v: string) => setD((x) => ({ ...x, name: v }))
  const setAbout = (v: string) => setD((x) => ({ ...x, about: v }))
  return (
    <Sheet title="New channel" onClose={onClose}>
      <Form onSubmit={async (e) => {
        e.preventDefault()
        const id = await createChannel(name.trim(), about.trim())
        clearDraft()
        onClose()
        go('c/' + id)
      }}>
        <label className="field"><span>Name</span>
          <input className="input" autoFocus placeholder="e.g. rust-hackers" value={name} onChange={(e) => setName(e.target.value)} /></label>
        <label className="field"><span>Description</span>
          <textarea className="input" rows={3} placeholder="What is this channel about?" value={about} onChange={(e) => setAbout(e.target.value)} /></label>
        <button className="btn" disabled={!name.trim()}>Create channel</button>
      </Form>
    </Sheet>
  )
}

// ---- a channel's messages ------------------------------------------------------

export function ChannelView({ id, topic, focus, activeThread }: { id: string; topic?: string; focus?: string; /** a large screen: the thread open in the third column */ activeThread?: string }) {
  const openTopic = (t?: string) => go(`c/${encodeURIComponent(id)}` + (t ? `/t/${encodeURIComponent(t)}` : ''))
  useVersion()
  const all = groups()
  const g = all.find((x) => x.id === id)
  return (
    <div className="screen">
      <ChannelHeader id={id} topic={topic} onTopic={openTopic}
        left={<MenuButton />}
        right={<><button className="icon-btn" aria-label={`Search ${g?.name ?? id}`} onClick={() => go(`search/${encodeURIComponent(id)}`)}><SearchIcon /></button><FollowButton channel={id} /></>} />
      <Conversation id={id} events topic={topic} onTopic={openTopic} focus={focus} activeThread={activeThread} readOnly={!!g?.isPod && g.closed && !g.members.has(me())} />
      {g?.isPod && !g.members.has(me()) && <JoinBar g={g} />}
    </div>
  )
}

/** Not in this pod: join it (open), or apply by answering its question (closed). */
function JoinBar({ g }: { g: Group }) {
  const [asking, setAsking] = useState(false)
  const [answer, setAnswer] = useState('')
  const applied = groups().find((x) => x.id === g.id)?.applications.some((a) => a.pubkey === me())
  if (!g.closed)
    return (
      <div className="join-bar">
        <span>You're not in this pod yet.</span>
        <Btn className="btn small" onClick={() => joinGroup(g.id)}>Join</Btn>
      </div>
    )
  return (
    <>
      <div className="join-bar">
        <span>{applied ? 'Application sent. The pod’s admins will answer.' : 'This pod is closed: apply to join.'}</span>
        {!applied && <button className="btn small" onClick={() => setAsking(true)}>Apply</button>}
      </div>
      {asking && (
        <Sheet title={`Apply to ${g.name}`} onClose={() => setAsking(false)}>
          <Form onSubmit={async (e) => { e.preventDefault(); await joinGroup(g.id, answer.trim()); setAsking(false) }}>
            <p className="apply-q">{g.prompt || DEFAULT_PROMPT}</p>
            <textarea className="input" rows={4} autoFocus value={answer} onChange={(e) => setAnswer(e.target.value)} />
            <div style={{ height: 12 }} />
            <button className="btn" disabled={!answer.trim()}>Send my application</button>
            <p className="hint" style={{ marginTop: 10 }}>The pod’s admins see your answer and admit you, or not.</p>
          </Form>
        </Sheet>
      )}
    </>
  )
}

/** The post a pod leaves in its parent: a card to open (or join) it. */
function PodCard({ id, fallback }: { id: string; fallback: string }) {
  const g = groups().find((x) => x.id === id)
  if (!g) return <div className="text">{fallback}</div>
  const inIt = g.members.has(me())
  return (
    <div className="ev-card pod-card" role="button" tabIndex={0} onClick={() => go('c/' + encodeURIComponent(id))}>
      <div className="date pod-ic"><PodShape members={g.members.size} size={26} /></div>
      <span className="info">
        <b>{g.name}</b>
        {g.about && <small>{g.about}</small>}
        <small>{g.closed ? 'Closed pod' : 'Pod'} · {g.members.size} {g.members.size === 1 ? 'member' : 'members'}{inIt ? ' · you’re in' : ''}</small>
      </span>
      {!inIt && !g.closed && <button className="btn small" onClick={(e) => { e.stopPropagation(); void joinGroup(id) }}>Join</button>}
      {!inIt && g.closed && <span className="badge">Closed</span>}
    </div>
  )
}

export function AccessFields({ closed, prompt, set }: { closed: boolean; prompt: string; set: (a: { closed: boolean; prompt: string }) => void }) {
  return (
    <>
      <div className="field"><span>Who can join</span></div>
      <div className="choice" style={{ marginTop: -8 }}>
        <button type="button" className={!closed ? 'on' : ''} onClick={() => set({ closed: false, prompt })}><b>Open</b><small>Any member joins</small></button>
        <button type="button" className={closed ? 'on' : ''} onClick={() => set({ closed: true, prompt })}><b>Closed</b><small>Apply, admins admit</small></button>
      </div>
      {closed && (
        <label className="field"><span>Question applicants answer</span>
          <textarea className="input" rows={2} placeholder={DEFAULT_PROMPT} value={prompt} onChange={(e) => set({ closed, prompt: e.target.value })} /></label>
      )}
    </>
  )
}

function NewPod({ parent, onClose }: { parent: string; onClose: () => void }) {
  const [name, setName] = useState('')
  const [about, setAbout] = useState('')
  const [access, setAccess] = useState({ closed: false, prompt: '' })
  const where = groups().find((g) => g.id === parent)
  return (
    <Sheet title="New pod" onClose={onClose}>
      <Form onSubmit={async (e) => {
        e.preventDefault()
        const id = await createPod(name.trim(), about.trim(), parent, access)
        onClose()
        go('c/' + encodeURIComponent(id))
      }}>
        <label className="field"><span>Name</span>
          <input className="input" autoFocus placeholder="e.g. Garden crew" value={name} onChange={(e) => setName(e.target.value)} /></label>
        <label className="field"><span>What is it working on?</span>
          <textarea className="input" rows={3} value={about} onChange={(e) => setAbout(e.target.value)} /></label>
        <AccessFields closed={access.closed} prompt={access.prompt} set={setAccess} />
        <button className="btn" disabled={!name.trim()}>Create pod</button>
        <p className="hint" style={{ marginTop: 10 }}>A working group inside {channelIcon(where)} {where?.name ?? parent}, with its own members, chat, events, photos, tasks and pods.</p>
      </Form>
    </Sheet>
  )
}

/** ☰: the channels drawer; the number of unread messages on it (like Discord's). */
export function MenuButton() {
  const n = unreadTotal()
  return (
    <button className="icon-btn menu-btn" aria-label={n ? `Channels · ${n} unread messages` : 'Channels'} aria-expanded="false" onClick={toggleDrawer}>
      <MenuIcon />
      {n > 0 && <span className="count" aria-hidden="true">{n > 99 ? '99+' : n}</span>}
    </button>
  )
}

export function Conversation({ id, events = false, readOnly = false, topic, onTopic, focus, activeThread }: { id: string; events?: boolean; readOnly?: boolean; topic?: string; onTopic?: (t?: string) => void; focus?: string; activeThread?: string }) {
  const [newEvent, setNewEvent] = useState(false)
  const [newMeeting, setNewMeeting] = useState(false)
  const [recording, setRecording] = useState(false)
  const [newPod, setNewPod] = useState(false)
  const [newProp, setNewProp] = useState(false)
  // #proposals, #events: one type of post only (their page's "posts"), no free text at the top level
  const page = webPage(group(id))
  const only = page?.posts === 'proposal' || page?.posts === 'event' ? page.posts : undefined
  const isPublic = isPublicChannel(group(id))
  // a request a member turns into a proposal (#requests → #proposals)
  const [proposeFrom, setProposeFrom] = useState<Message | null>(null)
  const [newTodo, setNewTodo] = useState(false)
  const [prefill, setPrefill] = useState('') // a new event's URL, put in the composer to send
  useVersion()
  // guest mode: reading only; the composer's place says so, with Join (GuestJoin.tsx)
  useGuestMode()
  const guestReading = reading()
  if (guestReading) readOnly = true
  const g = group(id)
  const admin = canAdmin(g)
  const list = messages(id).filter((m) => !topic || (topic === ANNOUNCE ? isAnnouncement(m, g) : m.topics.includes(topic)))
  const ps = profiles()
  const rx = reactions(id)
  const th = threadsOf(id)
  // a message someone turned into a to-do carries a mark (and its state)
  const todoOf = new Map(tasklist(id).tasks.filter((t) => t.source).map((t) => [t.source!, t]))
  const joins = topic ? [] : guestJoins(id)
  // threads unfold in place (no screen of their own): keyed by root id, or "act:<root>" for a
  // thread that resurfaced lower in the feed because someone replied to an older message
  const [openThreads, setOpenThreads] = useState<Set<string>>(new Set())
  const [replyTo, setReplyTo] = useState<string | null>(null)
  useEffect(() => { setOpenThreads(new Set()); setReplyTo(null) }, [id])
  const toggle = (key: string, open?: boolean) => setOpenThreads((s) => {
    const n = new Set(s)
    const on = open ?? !n.has(key)
    if (on) n.add(key); else n.delete(key)
    return n
  })
  type Item = { t: 'msg'; m: Message; at: number } | { t: 'join'; j: GuestJoin; at: number } | { t: 'act'; root: Message; last: Message; at: number }
  const items: Item[] = []
  for (const m of list) items.push({ t: 'msg', m, at: m.ev.created_at })
  for (const j of joins) items.push({ t: 'join', j, at: j.at })
  if (!topic) {
    // an old thread resurfaces where its latest reply came, once other messages followed it
    for (const [rootId, info] of th) {
      const root = list.find((m) => m.ev.id === rootId)
      if (!root) continue
      const later = list.some((m) => m.ev.created_at > root.ev.created_at && m.ev.created_at < info.last)
      if (!later) continue
      const last = threadReplies(id, rootId).at(-1)
      if (last) items.push({ t: 'act', root, last, at: info.last })
    }
  }
  items.sort((a, b) => a.at - b.at || (a.t === 'msg' ? -1 : 1))
  const gkey = knownGroupsKey()
  const inbox = { states: inboxStates(id, gkey), key: gkey, canSend: admin, name: g?.name ?? id }
  // with a thread already in the third column, another one replaces it (no history entry per thread)
  // Reply opens the thread's own view (or the third column), with its reply box focused: no second box in the channel (Xavier, #bugs 2026-11-10)
  const openThread = (m: Message, reply = false) => { const to = `c/${encodeURIComponent(id)}/thread/${m.ev.id}` + (reply ? '/reply' : ''); if (activeThread) location.replace('#/' + to); else go(to) }
  const [acting, setActing] = useState<Message | null>(null)
  const [actingReply, setActingReply] = useState<Message | null>(null) // a reply in an unfolded thread
  const [editing, setEditing] = useState<Message | null>(null)
  const [picking, setPicking] = useState(false)
  const [inviting, setInviting] = useState(false)
  const scroller = useRef<HTMLDivElement>(null)
  const stick = useRef(!focus)
  const focused = useRef('')

  useLayoutEffect(() => {
    const el = scroller.current
    // opened on one message (a search result): bring it to the middle and flash it, once
    if (el && focus && focused.current !== focus) {
      const m = el.querySelector<HTMLElement>(`[id="m-${CSS.escape(focus)}"]`)
      if (m) {
        focused.current = focus
        stick.current = false
        m.scrollIntoView({ block: 'center' })
        m.classList.add('flash')
        setTimeout(() => m.classList.remove('flash'), 2200)
        return
      }
    }
    if (el && stick.current) el.scrollTop = el.scrollHeight
  })

  return (
    <>
      <div className="scroll" ref={scroller}
        onScroll={(e) => { const el = e.currentTarget; stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80 }}>
        <div className="messages">
          {list.length === 0 && (topic
            ? <div className="empty">{topic === ANNOUNCE ? 'No announcements yet. Only the admins post them.' : `Nothing in #${topic} yet. What you send here carries it.`}</div>
            : guestReading ? <div className="empty">No messages here yet.</div> : <div className="empty-start">
                <b>{g ? `${channelIcon(g)}${g.isPod || g.isEvent || g.isProposal ? ' ' : ''}${g.name}` : id} is new</b>
                <p>No messages yet. Bring the people who belong here, then say hi.</p>
                <button className="btn" onClick={() => setInviting(true)}>Invite people</button>
              </div>)}
          {inviting && <InviteSheet id={id} onClose={() => setInviting(false)} />}
          {items.map((it, i) => {
            if (it.t === 'join') return <JoinLine key={'j' + it.j.id} j={it.j} />
            if (it.t === 'act') {
              const key = 'act:' + it.root.ev.id
              return <Fragment key={key}>
                <ThreadActivity root={it.root} last={it.last} onOpen={() => openThread(it.root)} open={activeThread === it.root.ev.id} />
              </Fragment>
            }
            const m = it.m
            const prevItem = items[i - 1]
            const key = m.ev.id
            return <Fragment key={key}>
              <Row m={m} prev={prevItem?.t === 'msg' && !openThreads.has(prevItem.m.ev.id) ? prevItem.m : undefined} groupId={id}
                name={!ps.get(m.ev.pubkey)?.name && m.ev.tags.some((t) => t[0] === 'client' && t[1] === 'belfry-drop') ? 'Drop box' : displayName(m.ev.pubkey, ps)}
                reactions={rx.get(m.ev.id) ?? []} onAct={(emoji) => { if (reading()) return askToJoin(); setPicking(!!emoji); setActing(m) }} topic={topic} onTopic={onTopic} announce={isAnnouncement(m, g)}
                thread={th.get(m.ev.id)} onThread={() => openThread(m)} onOpenThread={() => openThread(m)} threadOpen={false} inbox={inbox} todo={todoOf.get(m.ev.id)} active={activeThread === m.ev.id} />
            </Fragment>
          })}
        </div>
      </div>
      {guestReading ? <GuestBar groupId={id} /> : <SaveAccountBar />}
      {!readOnly && only && (
        <div className="only-bar">
          <span className="hint">{only === 'proposal'
            ? (isPublic ? 'Public proposals: anyone on the web sees them.' : 'Members only: proposals to discuss and vote on.')
            : (isPublic ? <>Public: anyone on the web sees these events. Internal? <a href="#/c/bookings">#bookings</a></> : 'Members only: internal bookings.')}</span>
          <button className="btn" onClick={() => (only === 'proposal' ? setNewProp(true) : setNewEvent(true))}>{only === 'proposal' ? 'New proposal' : 'New event'}</button>
        </div>
      )}
      {!readOnly && !only && isPublic && <div className="purpose-line">🌐 Public: anyone on the web sees this channel{page?.posts === 'request' ? '. Members can turn a request into a proposal.' : '.'}</div>}
      {!readOnly && <RecordingBar groupId={id} />}
      {!readOnly && !only && (topic !== ANNOUNCE || admin) && <Composer groupId={id} onRecord={canRecord() ? () => setRecording(true) : undefined} draft={'c:' + id} placeholder={topic && topic !== ANNOUNCE ? `Message #${topic}` : undefined} canAnnounce={admin} announcing={topic === ANNOUNCE}
        onSend={(text, files, announce) => { stick.current = true; return postMessage(id, text, files, topic === ANNOUNCE ? undefined : topic, announce, eventTags(text)) }} prefill={prefill}
        onEvent={events ? () => setNewEvent(true) : undefined} onMeeting={events && !guestInfo() ? () => setNewMeeting(true) : undefined} onPod={events ? () => setNewPod(true) : undefined} onProposal={events ? () => setNewProp(true) : undefined} onTodo={events ? () => setNewTodo(true) : undefined} />}
      {recording && <RecorderSheet groupId={id} onClose={() => setRecording(false)} />}
      {newMeeting && <MeetingSheet channel={id} onClose={() => setNewMeeting(false)} onCreated={(d) => setPrefill(`https://${BELFRY.domain}/events/${d} `)} />}
      {newTodo && <NewTodoSheet groupId={id} onClose={() => setNewTodo(false)} />}
      {newProp && <NewProposalSheet parent={id} onClose={() => setNewProp(false)} />}
      {newPod && <NewPod parent={id} onClose={() => setNewPod(false)} />}
      {acting && <MessageActions onPropose={page?.posts === 'request' && !guestInfo() && acting.ev.kind === 9 ? () => setProposeFrom(acting) : undefined} emojiOnly={picking} m={acting} groupId={id} mine={rx.get(acting.ev.id) ?? []} onClose={() => setActing(null)}
        thread={th.get(acting.ev.id)} onReply={() => openThread(acting, true)}
        onThread={th.get(acting.ev.id)?.count ? () => openThread(acting) : undefined} onEdit={() => setEditing(acting)} />}
      {actingReply && <MessageActions m={actingReply} groupId={id} mine={rx.get(actingReply.ev.id) ?? []} onClose={() => setActingReply(null)} onEdit={() => setEditing(actingReply)} />}
      {editing && <EditSheet m={editing} groupId={id} onClose={() => setEditing(null)} />}
      {proposeFrom && <NewProposalSheet parent="proposals" onClose={() => setProposeFrom(null)}
        from={{ title: (proposeFrom.ev.tags.find((t) => t[0] === 'subject')?.[1] ?? plainRefs(proposeFrom.text, '').split('\n')[0].replace(/\*\*/g, '')).slice(0, 140), text: proposeFrom.text.replace(/^\*\*[^\n]*\*\*\n+/, ''), by: displayName(proposeFrom.ev.pubkey), link: `https://${BELFRY.domain}/${page?.slug ?? id}/${proposeFrom.ev.id}` }}
        onMade={(_pid, title) => void postThreadReply(id, proposeFrom.ev, `Became a proposal: “${title}”. Members discuss it and vote on it in #proposals.`, [])} />}
      {newEvent && <NewEventSheet channel={id} visibility={only === 'event' ? (isPublic ? 'public' : 'members') : undefined} onClose={() => setNewEvent(false)} onCreated={(url) => { if (only === 'event') void postMessage(id, url, [], undefined, false, eventTags(url)); else setPrefill(url + ' ') }} />}
    </>
  )
}

/** A thread: the message it hangs off, then the replies (#/c/<id>/thread/<message id>). */
export function ThreadView({ groupId, rootId, pane = false, replying = false }: { groupId: string; rootId: string; /** shown as the third column beside its channel */ pane?: boolean; /** opened from Reply: the reply box gets the focus */ replying?: boolean }) {
  useVersion()
  const g = group(groupId)
  const ps = profiles()
  const rx = reactions(groupId)
  const root = messages(groupId).find((m) => m.ev.id === rootId)
  const replies = threadReplies(groupId, rootId)
  // to-dos made from this thread's messages: each step (made, claimed, working, done…) is a line in it
  const ids = new Set([rootId, ...replies.map((m) => m.ev.id)])
  const steps = stepGroups(tasklist(groupId).tasks.filter((t) => t.source && ids.has(t.source)))
  type Flow = { at: number; m?: Message; i?: number; g?: StepGroup }
  const flow: Flow[] = [
    ...replies.map((m, i): Flow => ({ at: m.ev.created_at, m, i })),
    ...steps.map((g): Flow => ({ at: g.at, g })),
  ].sort((a, b) => a.at - b.at || (a.m ? -1 : 0) - (b.m ? -1 : 0))
  const [acting, setActing] = useState<Message | null>(null)
  const [editing, setEditing] = useState<Message | null>(null)
  const [picking, setPicking] = useState(false)
  const scroller = useRef<HTMLDivElement>(null)
  useLayoutEffect(() => { const el = scroller.current; if (el) el.scrollTop = el.scrollHeight }, [flow.length])
  const nameOf = (m: Message) => displayName(m.ev.pubkey, ps)
  const back = () => up(`c/${encodeURIComponent(groupId)}`)
  const gkey = knownGroupsKey()
  const inbox = { states: inboxStates(groupId, gkey), key: gkey, canSend: canAdmin(g), name: g?.name ?? groupId }
  const card = root ? cardOf(root.ev, gkey) : null
  return (
    <div className="screen">
      <div className="topbar">
        {!pane && <button className="icon-btn" aria-label={`Back to ${g?.name ?? groupId}`} onClick={back}>‹</button>}
        <h1>Thread<small>{channelIcon(g)} {g?.name ?? groupId}</small></h1>
        {pane && <button className="icon-btn" aria-label="Close the thread" onClick={back}><CloseIcon /></button>}
      </div>
      {card && <div className="thread-private">Team only. {card.name || card.from} never sees this thread.</div>}
      <div className="scroll" ref={scroller}>
        <div className="messages">
          {root ? <Row m={root} name={nameOf(root)} groupId={groupId} reactions={rx.get(root.ev.id) ?? []} onAct={(emoji) => { if (reading()) return askToJoin(); setPicking(!!emoji); setActing(root) }} announce={isAnnouncement(root, g)} inbox={inbox} onThread={() => {}} thread={{ count: replies.length, last: 0, who: [] }} />
            : <div className="empty">This message is not here (yet, or any more).</div>}
          <div className="thread-sep"><i />{replies.length ? `${replies.length} ${replies.length === 1 ? 'reply' : 'replies'}` : 'No replies yet'}<i /></div>
          {flow.map((x) => x.m
            ? <Row key={x.m.ev.id} m={x.m} prev={replies[x.i! - 1]} head={x.i === 0} name={nameOf(x.m)} groupId={groupId}
              reactions={rx.get(x.m.ev.id) ?? []} onAct={(emoji) => { if (reading()) return askToJoin(); setPicking(!!emoji); setActing(x.m!) }} announce={false} />
            : <TodoStepLine key={x.g!.t.id + x.g!.at + x.g!.what.join()} g={x.g!} />)}
        </div>
      </div>
      {root && reading() && <GuestBar groupId={groupId} />}
      {root && !reading() && <Composer groupId={groupId} draft={'t:' + rootId} autoFocus={replying} placeholder={replies.length ? 'Reply in the thread' : 'Reply (starts a thread)'} onSend={(text, files) => postThreadReply(groupId, root.ev, text, files)} />}
      {acting && <MessageActions emojiOnly={picking} m={acting} groupId={groupId} mine={rx.get(acting.ev.id) ?? []} onClose={() => setActing(null)} onEdit={() => setEditing(acting)} />}
      {editing && <EditSheet m={editing} groupId={groupId} onClose={() => setEditing(null)} />}
    </div>
  )
}

/** Press and hold (phones) or right-click opens a message's menu. A finger may wobble a little;
 *  moving further is a scroll. Not on buttons, links or videos inside the message. */
function useHold(open: () => void, off = false) {
  const t = useRef<ReturnType<typeof setTimeout> | null>(null)
  const at = useRef({ x: 0, y: 0 })
  const cancel = () => { if (t.current) clearTimeout(t.current); t.current = null }
  useEffect(() => cancel, [])
  return {
    onContextMenu: (e: React.MouseEvent) => { if (off) return; e.preventDefault(); cancel(); open() },
    onPointerDown: (e: React.PointerEvent) => {
      if (off || e.pointerType === 'mouse' || (e.target as HTMLElement).closest('button, a, video, textarea, input')) return
      at.current = { x: e.clientX, y: e.clientY }
      cancel()
      t.current = setTimeout(() => { t.current = null; navigator.vibrate?.(10); open() }, 450)
    },
    onPointerMove: (e: React.PointerEvent) => { if (t.current && Math.hypot(e.clientX - at.current.x, e.clientY - at.current.y) > 10) cancel() },
    onPointerUp: cancel, onPointerCancel: cancel, onPointerLeave: cancel,
  }
}

function Row({ m, prev, name, groupId, reactions: rx, onAct, topic, onTopic, announce, thread, onThread, onOpenThread, threadOpen = false, head, inbox, active = false }: { active?: boolean; todo?: Task; onOpenThread?: () => void; inbox?: { states: Map<string, CardState>; key?: string; canSend: boolean; name: string }; head?: boolean; m: Message; prev?: Message; name: string; groupId: string; reactions: Reaction[]; onAct: (emoji?: boolean) => void; topic?: string; onTopic?: (t?: string) => void; announce: boolean; thread?: ThreadInfo; onThread?: () => void; threadOpen?: boolean }) {
  // topics it carries that its text doesn't show (sent inside an open topic), except the open one
  const inText = new Set(hashtags(m.text))
  const extra = m.topics.filter((t) => !inText.has(t) && t !== topic && t !== ANNOUNCE)
  const d = new Date(m.ev.created_at * 1000)
  const newDay = !head && (!prev || new Date(prev.ev.created_at * 1000).toDateString() !== d.toDateString())
  const first = head || newDay || prev!.ev.pubkey !== m.ev.pubkey || m.ev.created_at - prev!.ev.created_at > 300
  const p = m.ev.pending
  const card = inbox ? cardOf(m.ev, inbox.key) : null
  const sent = inbox && !card ? sentOf(m.ev, inbox.key) : null
  const mail = card || sent ? null : mailOf(m.ev)
  const [fullMail, setFullMail] = useState(false)
  // events: a message carrying an event's URL (or its `a` tag) shows its card under the text, at
  // most three; the older automatic post ("📅 …", the `a` tag only) is the card alone
  const links = eventLinksIn(m.text)
  const legacy = !links.length && m.text.startsWith('📅') ? eventCoordOf(m.ev) : undefined
  const cards: { key: string; coord?: string; link?: string }[] = []
  if (!legacy) {
    const seen = new Set<string>()
    for (const c of eventCoordsOf(m.ev)) {
      const d = c.split(':')[2]
      if (seen.has(d)) continue
      seen.add(d)
      cards.push(eventByCoord(c) ? { key: c, coord: c } : { key: c, link: d })
    }
    for (const l of links) {
      const d = eventByLinkLocal(l)?.d ?? l
      if (seen.has(d)) continue
      seen.add(d)
      cards.push({ key: l, link: l })
    }
  }
  const isCard = !!(legacy || podOf(m.ev) || proposalOf(m.ev)) // a card always says who made it
  // press and hold (phones) or right-click: react, reply, edit or delete your own
  const hold = useHold(() => onAct(), !!p)
  if (card || sent) return (
    <>
      {newDay && <div className="day" role="separator"><span>{fmtDay(d)}</span></div>}
      <div className="msg first card-row" id={'m-' + m.ev.id}>
        {card ? <InboxCardView m={m} card={card} groupId={groupId} channelName={inbox!.name} state={inbox!.states.get(m.ev.id)} canSend={inbox!.canSend}
          thread={thread} onThread={onThread ?? (() => {})} />
          : <SentCardView m={m} sent={sent!} groupId={groupId} />}
      </div>
    </>
  )
  return (
    <>
      {newDay && <div className="day" role="separator"><span>{fmtDay(d)}</span></div>}
      <div id={'m-' + m.ev.id} className={'msg' + (first || announce || mail || isCard ? ' first' : '') + (p ? ' pending' : '') + (announce ? ' announce' : '') + (active ? ' thread-active' : '')}
        {...hold}>
        {first || announce || mail || isCard ? <button className="face" onClick={() => openProfile(m.ev.pubkey)} aria-label={`${name}: profile`}><Avatar pubkey={m.ev.pubkey} /></button> : <div className="gutter" />}
        <div className="body">
          {(first || announce || mail || isCard) && <div className="who"><button className="who-name" onClick={() => openProfile(m.ev.pubkey)}>{name}</button><time>{fmtTime(m.ev.created_at)}</time>{m.edited && <span className="edited" title={`Edited ${fmtTime(m.edited)}`}>edited</span>}{mail && <ViaMail />}{announce && <span className="badge announce-badge">📣 Announcement</span>}</div>}
          {mail?.subject && <div className="mail-subject">{mail.subject}</div>}
          {(legacy || podOf(m.ev) || proposalOf(m.ev)) && <div className="created-line">Created {legacy ? 'an event' : podOf(m.ev) ? 'a pod' : 'a proposal'}</div>}
          {legacy ? <EventCard coord={legacy} fallback={m.text} /> : podOf(m.ev) ? <PodCard id={podOf(m.ev)!} fallback={m.text} /> : proposalOf(m.ev) ? <ProposalCard id={proposalOf(m.ev)!} fallback={m.text} /> : m.text && <RichText text={mail ? mailText(m.text, mail) : m.text} groupId={groupId} onOpen={onTopic} />}
          {cards.length > 0 && <div className="ev-cards">{cards.slice(0, 3).map((c) => <EventCard key={c.key} coord={c.coord} link={c.link} fallback="" />)}</div>}
          {mail?.original && <button className="link full-mail-btn" onClick={() => setFullMail(true)}>Full e-mail</button>}
          {fullMail && mail && <FullMail mail={mail} onClose={() => setFullMail(false)} />}
          {extra.length > 0 && <div className="msg-topics">{extra.map((t) => <button key={t} onClick={() => onTopic?.(t)}>#{t}</button>)}</div>}
          <MediaGrid media={m.media} />
          {/* no to-do mark here any more (Xavier, 2026-11-12): the ⏳ / ☑️ reactions say it */}
          {rx.length > 0 && (
            <div className="reactions">
              {rx.map((r) => (
                <button key={r.emoji} className={r.mine ? 'on' : ''} title={[...r.who].map((k) => displayName(k)).join(', ')}
                  onClick={() => void react(groupId, m.ev, r.emoji, r.mine)}>
                  <span>{r.emoji}</span> {r.who.size}
                </button>
              ))}
              {!p && rx.length > 0 && <button className="react-add" aria-label="Add a reaction" title="Add a reaction" onClick={() => onAct(true)}><SmilePlusIcon /></button>}
            </div>
          )}
          {!first && !announce && !mail && !isCard && m.edited && <span className="edited below">edited</span>}
          {thread && thread.count > 0 && thread.last > 0 && (onOpenThread || onThread) && (
            <ThreadLine groupId={groupId} rootId={m.ev.id} thread={thread} open={threadOpen} onClick={onThread ?? onOpenThread!} />
          )}
          {p && p.status === 'failed' && (
            <div className="state failed">Not sent: {p.error} · <button className="link" onClick={() => retry(p.localId)}>Retry</button></div>
          )}
          {p && p.status === 'queued' && <div className="state">{p.tries && !relaysUp() ? 'Waiting for connection…' : p.tries ? 'Sending again…' : 'Sending…'}</div>}
        </div>
        {!p && (
          <div className="msg-tools" role="toolbar" aria-label="Message actions">
            <button aria-label="Add a reaction" title="Add a reaction" onClick={() => onAct(true)}><SmilePlusIcon /></button>
            <button aria-label="More" title="More" onClick={() => onAct()}><MoreIcon /></button>
          </div>
        )}
      </div>
    </>
  )
}

const MORE_EMOJIS = [
  '👍', '❤️', '😂', '🎉', '🙏', '👀', '🔥', '👏', '😍', '🤔', '😮', '😢',
  '😅', '🙌', '💪', '✅', '❌', '💯', '🚀', '✨', '🌱', '☕', '🍕', '🍻',
  '😊', '😎', '🤗', '🥳', '😴', '🤯', '😬', '🙈', '👋', '🤝', '👌', '✌️',
  '💡', '📌', '📅', '⏰', '🎶', '📷', '🌞', '🌧️', '❄️', '🌈', '🐝', '🌻',
]

function MessageActions({ m, groupId, mine, onClose, thread, onThread, onReply, onEdit, onPropose, emojiOnly = false }: { m: Message; groupId: string; mine: Reaction[]; onClose: () => void; thread?: ThreadInfo; onThread?: () => void; onReply?: () => void; onEdit?: () => void; /** #requests: a member turns it into a proposal */ onPropose?: () => void; emojiOnly?: boolean }) {
  // your own plain message or reply can be edited (not a card, an event, a pod, a proposal or an e-mail)
  const canEdit = !!onEdit && m.ev.pubkey === me() && !m.ev.pending && !!m.text && !(m.text.startsWith('📅') && eventCoordOf(m.ev)) && !podOf(m.ev) && !proposalOf(m.ev) && !mailOf(m.ev) && !cardOf(m.ev, knownGroupsKey()) && !sentOf(m.ev, knownGroupsKey())
  // long press / right-click / "More": one line of emojis, then what you can do (Discord's menu).
  // The add-reaction button opens the emojis alone.
  const [more, setMore] = useState(emojiOnly)
  const [confirming, setConfirming] = useState(false) // "Delete message" asks first, in the same sheet
  const g = group(groupId)
  const isPinned = canAdmin(g) && pinned(groupId, mayPin(g)).some((x) => x.ev.id === m.ev.id)
  const isTodo = tasklist(groupId).tasks.some((t) => t.source === m.ev.id)
  const todoTitle = plainRefs(m.text, '').replace(/\s+/g, ' ').trim().slice(0, 300)
  const [copied, setCopied] = useState('')
  const pick = (emoji: string) => {
    void react(groupId, m.ev, emoji, mine.find((r) => r.emoji === emoji)?.mine)
    onClose()
  }
  const isOn = (emoji: string) => mine.some((r) => r.emoji === emoji && r.mine)
  const copy = async (what: string, text: string) => {
    try { await navigator.clipboard.writeText(text); setCopied(what); setTimeout(onClose, 600) } catch { setCopied('') }
  }
  const link = `${location.origin}${location.pathname}#/c/${encodeURIComponent(groupId)}/m/${m.ev.id}`
  const Item = ({ icon, label, onClick, danger = false }: { icon: ReactNode; label: string; onClick: () => void; danger?: boolean }) => (
    <button className={'act' + (danger ? ' danger' : '')} onClick={onClick}>{icon}<span>{label}</span></button>
  )
  if (confirming) return (
    <Sheet title="Delete this message?" onClose={onClose}>
      <blockquote className="delete-preview">
        <b>{displayName(m.ev.pubkey)}</b>
        <span>{plainRefs(m.text || (m.media.length ? (m.media.length === 1 ? 'A photo or file' : `${m.media.length} photos or files`) : '')).slice(0, 240)}</span>
      </blockquote>
      <p className="hint">It disappears for everyone, with its reactions. This can't be undone.</p>
      <div className="row-btns">
        <button type="button" className="btn ghost" onClick={onClose}>Cancel</button>
        <Btn className="btn danger" onClick={async () => { await deleteMessage(groupId, m.ev.id); onClose() }}>Delete</Btn>
      </div>
    </Sheet>
  )
  return (
    <Sheet title={emojiOnly ? 'Add a reaction' : 'Message'} hideTitle={!emojiOnly} onClose={onClose}>
      <div className="emoji-row">
        {topEmojis(5).map((e) => <button key={e} className={isOn(e) ? 'on' : ''} aria-label={`React with ${e}`} onClick={() => pick(e)}>{e}</button>)}
        <button aria-label="More emojis" aria-expanded={more} className={'more' + (more ? ' on' : '')} onClick={() => setMore(!more)}><SmilePlusIcon /></button>
      </div>
      {more && (
        <div className="emoji-grid">
          {MORE_EMOJIS.map((e) => <button key={e} className={isOn(e) ? 'on' : ''} aria-label={`React with ${e}`} onClick={() => pick(e)}>{e}</button>)}
        </div>
      )}
      {!emojiOnly && (
        <>
          <div className="act-group">
            {canEdit && <Item icon={<PencilIcon />} label="Edit message" onClick={() => { onClose(); onEdit!() }} />}
            {onReply && <Item icon={<ReplyIcon />} label={thread?.count ? `Reply in the thread (${thread.count})` : 'Reply'} onClick={() => { onClose(); onReply() }} />}
            {onThread && <Item icon={<ThreadIcon />} label={thread?.count ? `Open the thread (${thread.count})` : 'Start a thread'} onClick={() => { onClose(); onThread() }} />}
            {m.ev.pubkey !== me() && <Item icon={<AtIcon />} label={`Message ${displayName(m.ev.pubkey)}`} onClick={() => { onClose(); go('dm/' + encodeURIComponent('n:' + m.ev.pubkey)) }} />}
          </div>
          <div className="act-group">
            {m.text && <Item icon={<CopyIcon />} label={copied === 'text' ? 'Copied' : 'Copy text'} onClick={() => void copy('text', m.text)} />}
            <Item icon={<LinkIcon />} label={copied === 'link' ? 'Copied' : 'Copy link'} onClick={() => void copy('link', link)} />
            {onPropose && !m.ev.pending && <Item icon={<DocIcon />} label="Turn into a proposal" onClick={() => { onClose(); onPropose() }} />}
            {todoTitle && !isTodo && !m.ev.pending && <Item icon={<TodoIcon />} label={copied === 'todo' ? 'Added to the todos' : 'Turn into a todo'} onClick={() => { if (copied === 'todo') return; setCopied('todo'); void addTask(groupId, todoTitle, m.ev.id); setTimeout(onClose, 700) }} />}
            {canAdmin(g) && !m.ev.pending && m.ev.kind === 9 && <Item icon={<PushPinIcon />} label={isPinned ? 'Unpin from the channel' : 'Pin to the channel'} onClick={() => { onClose(); void pinMessage(groupId, m.ev.id, !isPinned) }} />}
            {isAdmin() && nsiteRef(m.text) && <Item icon={<GlobeIcon />} label="Make this the website" onClick={() => { onClose(); void makeWebsite(m) }} />}
          </div>
          {m.ev.pubkey === me() && (
            <div className="act-group">
              <Item danger icon={<TrashIcon />} label="Delete message" onClick={() => setConfirming(true)} />
            </div>
          )}
        </>
      )}
    </Sheet>
  )
}

// ---- composer --------------------------------------------------------------------

export function Composer({ onSend, onEvent, onMeeting, onRecord, onPod, onProposal, onTodo, groupId, placeholder = 'Message', canAnnounce = false, announcing = false, autoFocus = false, compact = false, prefill, draft }: { /** where its unsent text is kept on this device (a channel, a thread): switching away and back brings it back */ draft?: string; prefill?: string; onSend: (text: string, files: File[], announce: boolean) => Promise<unknown>; onEvent?: () => void; /** a members-only meeting (Meeting.tsx) */ onMeeting?: () => void; /** an audio recording (Recorder.tsx) */ onRecord?: () => void; onPod?: () => void; onProposal?: () => void; onTodo?: () => void; /** the channel written in: mentions of people who are not in it are pointed out */ groupId?: string; placeholder?: string; canAnnounce?: boolean; announcing?: boolean; autoFocus?: boolean; compact?: boolean }) {
  const [text, setText] = useState('')
  const [ann, setAnn] = useState(announcing)
  useEffect(() => setAnn(announcing), [announcing])
  const [menu, setMenu] = useState(false)
  const [files, setFiles] = useState<File[]>([])
  const [error, setError] = useState('')
  const ta = useRef<HTMLTextAreaElement>(null)
  const picker = useRef<HTMLInputElement>(null)
  // focus now, and again once a closing menu has handed the focus back to where it was opened
  useEffect(() => {
    if (!autoFocus) return
    ta.current?.focus({ preventScroll: false })
    // a closing sheet keeps the page inert for a moment, then drops the focus on <body>: take it back
    let n = 0
    const t = setInterval(() => {
      if (++n > 15) clearInterval(t)
      else if (document.activeElement === document.body) ta.current?.focus({ preventScroll: true })
    }, 100)
    return () => clearInterval(t)
  }, [autoFocus])
  useEffect(() => { if (prefill) { setText((t) => (t ? t + ' ' : '') + prefill); requestAnimationFrame(() => ta.current?.focus()) } }, [prefill])
  const previews = useMemo(() => files.map((f) => (f.type.startsWith('image/') ? URL.createObjectURL(f) : '')), [files])
  useEffect(() => () => previews.forEach((u) => u && URL.revokeObjectURL(u)), [previews])

  useLayoutEffect(() => {
    const el = ta.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = Math.min(el.scrollHeight, 140) + 'px'
  }, [text])

  // @mentions: "@" and a few letters suggest people; a pick writes "@Name", sent as nostr:npub…
  const picked = useRef(new Map<string, string>()) // name → pubkey
  const [caret, setCaret] = useState(0)
  const query = /(^|\s)@([\p{L}\p{N}_.-]{0,30})$/u.exec(text.slice(0, caret))
  const people = useMemo(() => {
    if (!query) return []
    const q = query[2].toLowerCase()
    return [...profiles().entries()]
      .filter(([pk, p]) => pk !== me() && p.name && p.name.toLowerCase().split(/\s+/).some((w) => w.startsWith(q)))
      .slice(0, 6).map(([pubkey, p]) => ({ pubkey, name: p.name! }))
  }, [query?.[2]]) // eslint-disable-line react-hooks/exhaustive-deps
  // #channels: "#" and a few letters suggest channels; a pick writes "#Name", sent as nostr:naddr…
  // (a "#word" typed without picking stays a topic)
  const pickedChans = useRef(new Map<string, string>()) // "#Name" → nostr:naddr…
  // the draft: saved to the conversation the text belongs to (owner), loaded when switching to another
  const owner = useRef<string | undefined>(undefined)
  useEffect(() => { if (owner.current) writeDraft(owner.current, text, picked.current, pickedChans.current) }, [text, draft])
  useEffect(() => {
    owner.current = draft
    if (!draft) return
    const d = readDraft(draft)
    picked.current = new Map(d?.picked ?? [])
    pickedChans.current = new Map(d?.chans ?? [])
    setText(d?.text ?? '')
  }, [draft])
  const chanQuery = query ? null : /(^|[^\p{L}\p{N}_&/])#([\p{L}\p{N}_-]{0,30})$/u.exec(text.slice(0, caret))
  const chans = useMemo(() => {
    if (!chanQuery) return []
    const q = chanQuery[2].toLowerCase()
    return groups()
      .filter((g) => !g.isEvent && (g.id.toLowerCase().startsWith(q) || g.name.toLowerCase().split(/[\s_-]+/).some((w) => w.startsWith(q))))
      .map((g) => ({ g, ref: channelRef(g.id) }))
      .filter((x): x is { g: Group; ref: string } => !!x.ref)
      .sort((a, b) => b.g.lastActivity - a.g.lastActivity)
      .slice(0, 6)
  }, [chanQuery?.[2]]) // eslint-disable-line react-hooks/exhaustive-deps
  const pickChannel = ({ g, ref }: { g: Group; ref: string }) => {
    const start = caret - chanQuery![2].length - 1
    const label = '#' + g.name
    const next = text.slice(0, start) + label + ' ' + text.slice(caret)
    pickedChans.current.set(label, ref)
    setText(next)
    const at = start + label.length + 1
    setCaret(at)
    requestAnimationFrame(() => { ta.current?.focus(); ta.current?.setSelectionRange(at, at) })
  }
  const pick = (who: { pubkey: string; name: string }) => {
    const start = caret - query![2].length - 1
    const next = text.slice(0, start) + '@' + who.name + ' ' + text.slice(caret)
    picked.current.set(who.name, who.pubkey)
    setText(next)
    const at = start + who.name.length + 2
    setCaret(at)
    requestAnimationFrame(() => { ta.current?.focus(); ta.current?.setSelectionRange(at, at) })
  }
  // people mentioned in the draft who cannot see this channel: said so before sending, with a
  // way to bring them in (a mention alone neither notifies them nor shows them the message)
  const here = groupId ? group(groupId) : undefined
  const outside = here ? [...picked.current].filter(([name, pk]) => text.includes('@' + name) && !inChannel(here, pk)) : []
  const [invited, setInvited] = useState<Map<string, string>>(new Map())
  const withMentions = (t: string) => {
    let out = t
    for (const [name, pk] of [...picked.current].sort((a, b) => b[0].length - a[0].length))
      out = out.split('@' + name).join('nostr:' + nip19.npubEncode(pk))
    for (const [label, ref] of [...pickedChans.current].sort((a, b) => b[0].length - a[0].length))
      out = out.split(label).join(ref)
    picked.current.clear()
    pickedChans.current.clear()
    return out
  }

  const send = async () => {
    if (!text.trim() && !files.length) return
    const mentioned = new Map(picked.current)
    const chansPicked = new Map(pickedChans.current)
    const t = withMentions(text.trim())
    const f = files
    const a = canAnnounce && ann
    setText('')
    setFiles([])
    setError('')
    if (!announcing) setAnn(false)
    try { await onSend(t, f, a) } catch (e) {
      // not saved on this device (storage full or refused): the text and files come back
      setText((now) => now || text)
      for (const [name, pk] of mentioned) picked.current.set(name, pk)
      for (const [label, ref] of chansPicked) pickedChans.current.set(label, ref)
      setFiles((now) => (now.length ? now : f))
      if (a) setAnn(true)
      setError(`Not saved: ${(e instanceof Error ? e.message : String(e)).replace(/\.$/, "")}. Try again.`)
    }
  }

  return (
    <div className={'composer' + (compact ? ' compact' : '')}>
      {canAnnounce && ann && (
        <div className="announce-mode">
          <span>📣 Announcement: sent to the channel and e-mailed to its followers</span>
          {!announcing && <button aria-label="Not an announcement" onClick={() => setAnn(false)}>✕</button>}
        </div>
      )}
      {error && <div className="error" role="alert" style={{ margin: '0 4px 6px' }}>{error}</div>}
      {files.length > 0 && (
        <div className="previews">
          {files.map((f, i) => (
            <div className="p" key={i}>
              {previews[i] ? <img src={previews[i]} alt="" /> : <span>📎 {f.name}</span>}
              <button aria-label="Remove" onClick={() => setFiles(files.filter((_, j) => j !== i))}>✕</button>
            </div>
          ))}
        </div>
      )}
      {outside.map(([name, pk]) => (
        <div key={pk} className="outside-note" role="status">
          <span><b>{name}</b> is not in this channel{isGuest(pk) ? ' (a guest of another one)' : ''}: they won't be told and can't read this.</span>
          {invited.has(pk) ? <span className="hint">{invited.get(pk)}</span>
            : <Btn className="btn small" onClick={async () => {
                try { const s = await inviteMember(groupId!, pk); setInvited((m) => new Map(m).set(pk, s === 'asked' ? 'Link sent · they apply' : s === 'guest' ? 'Added as a guest' : 'Invited')) }
                catch { setInvited((m) => new Map(m).set(pk, 'Could not invite. Try from the channel\'s Invite.')) }
              }}>{isGuest(pk) ? 'Add as a guest' : 'Invite'}</Btn>}
        </div>
      ))}
      <div className="bar">
        <button className="icon-btn" aria-label={onEvent ? 'Add photos, files, an event, a proposal or a pod' : 'Add photos or files'}
          onClick={() => (onEvent || canAnnounce || onRecord ? setMenu(true) : picker.current?.click())}>＋</button>
        <input ref={picker} type="file" multiple hidden
          onChange={(e) => { setFiles([...files, ...Array.from(e.target.files ?? [])]); e.target.value = '' }} />
        {people.length > 0 && (
          <div className="mention-pop" role="listbox" aria-label="Mention someone">
            {people.map((p) => (
              <button key={p.pubkey} role="option" onMouseDown={(e) => e.preventDefault()} onClick={() => pick(p)}>
                <Avatar pubkey={p.pubkey} size={24} /><span>{p.name}</span>
                {here && !inChannel(here, p.pubkey) && <small className="not-here">not in this channel</small>}
              </button>
            ))}
          </div>
        )}
        {chans.length > 0 && (
          <div className="mention-pop" role="listbox" aria-label="Link a channel">
            {chans.map((c) => (
              <button key={c.g.id} role="option" onMouseDown={(e) => e.preventDefault()} onClick={() => pickChannel(c)}>
                <span className="chan-glyph" aria-hidden="true">{channelIcon(c.g)}</span><span>{c.g.name}</span>
              </button>
            ))}
          </div>
        )}
        <textarea ref={ta} rows={1} placeholder={canAnnounce && ann ? 'Announcement' : placeholder} value={text}
          onChange={(e) => { setText(e.target.value); setCaret(e.target.selectionStart ?? e.target.value.length) }}
          onSelect={(e) => setCaret((e.target as HTMLTextAreaElement).selectionStart ?? 0)}
          onKeyDown={(e) => {
            if (people.length && (e.key === 'Enter' || e.key === 'Tab')) { e.preventDefault(); pick(people[0]); return }
            if (chans.length && (e.key === 'Enter' || e.key === 'Tab')) { e.preventDefault(); pickChannel(chans[0]); return }
            // Enter sends on a keyboard; on phones the return key adds a line
            if (e.key === 'Enter' && !e.shiftKey && matchMedia('(pointer: fine)').matches) { e.preventDefault(); void send() }
          }} />
        <button className="send" aria-label="Send" disabled={!text.trim() && !files.length} onClick={() => void send()}>↑</button>
      </div>
      {menu && (onEvent || canAnnounce || onRecord) && (
        <Sheet title="Add to the conversation" hideTitle onClose={() => setMenu(false)}>
          <div className="act-group">
            <button className="act" onClick={() => { setMenu(false); picker.current?.click() }}><ImageIcon /><span>Photos or files</span></button>
            {onRecord && <button className="act" onClick={() => { setMenu(false); onRecord() }}><MicIcon /><span>Audio recording<small>Kept on this device until it is sent</small></span></button>}
          </div>
          <div className="act-group">
            {canAnnounce && <button className="act" onClick={() => { setMenu(false); setAnn(true); ta.current?.focus() }}><MegaphoneIcon /><span>Announcement<small>Stands out; followers get it by e-mail</small></span></button>}
            {onTodo && <button className="act" onClick={() => { setMenu(false); onTodo() }}><TodoIcon /><span>Todo<small>Something to get done, on the channel's list</small></span></button>}
            {onMeeting && <button className="act" onClick={() => { setMenu(false); onMeeting() }}><PeopleIcon /><span>Meeting<small>Members only: a topic, people, a time</small></span></button>}
            {onEvent && <button className="act" onClick={() => { setMenu(false); onEvent() }}><CalendarIcon /><span>Event</span></button>}
            {onProposal && <button className="act" onClick={() => { setMenu(false); onProposal() }}><DocIcon /><span>Proposal<small>A text to discuss and vote on</small></span></button>}
            {onPod && <button className="act" onClick={() => { setMenu(false); onPod() }}><HexIcon /><span>Pod<small>A working group with its own channel</small></span></button>}
          </div>
        </Sheet>
      )}
    </div>
  )
}

/** A new to-do for the channel (the ＋ menu). */
function NewTodoSheet({ groupId, onClose }: { groupId: string; onClose: () => void }) {
  const [d, setD, clearDraft] = useFormDraft('todo:' + groupId, () => ({ title: '' }))
  const title = d.title
  const setTitle = (t: string) => setD(() => ({ title: t }))
  return (
    <Sheet title="New todo" onClose={onClose}>
      <Form onSubmit={async (e) => { e.preventDefault(); if (!title.trim()) return; await addTask(groupId, title.trim()); clearDraft(); onClose() }}>
        <label className="field"><span>What needs doing?</span>
          <input className="input" autoFocus maxLength={300} placeholder="e.g. Book the room for Friday" value={title} onChange={(e) => setTitle(e.target.value)} /></label>
        <button className="btn" disabled={!title.trim()}>Add to the list</button>
        <p className="hint" style={{ marginTop: 10 }}>Everyone in the channel sees it under the channel's name. Anyone can take it or tick it off.</p>
      </Form>
    </Sheet>
  )
}

/** "<name> joined as a guest, invited by <inviter>" (guests.py), a line in the feed. */
function JoinLine({ j }: { j: GuestJoin }) {
  return (
    <div className="join-line" role="note">
      <span className="arrow" aria-hidden="true">→</span>
      <span><b>{displayName(j.pubkey)}</b> joined as {j.agent && j.member && j.by ? <><b>{displayName(agentOwner(j.pubkey) ?? j.by)}</b>'s agent</> : j.agent ? 'an agent' : 'a guest'}{j.by && !(j.agent && j.member) ? <>, invited by <b>{displayName(j.by)}</b></> : null}</span>
      <time>{fmtTime(j.at)}</time>
    </div>
  )
}

/** A thread unfolded under its message: the latest replies, earlier ones on demand, a reply box. */
function InlineThread({ groupId, root, focus, onHide, onOpen, onAct }: { groupId: string; root: Message; focus: boolean; onHide: () => void; onOpen: () => void; onAct: (m: Message) => void }) {
  useVersion()
  const replies = threadReplies(groupId, root.ev.id)
  const [all, setAll] = useState(false)
  const shown = all ? replies : replies.slice(-3)
  const hidden = replies.length - shown.length
  return (
    <div className="inline-thread" role="region" aria-label="Thread">
      {hidden > 0 && <button className="link thread-more" onClick={() => setAll(true)}>Show {hidden} earlier {hidden === 1 ? 'reply' : 'replies'}</button>}
      {shown.map((r) => <InlineReply key={r.ev.id} r={r} groupId={groupId} onAct={() => onAct(r)} />)}
      {!reading() && <Composer groupId={groupId} draft={'t:' + root.ev.id} placeholder={replies.length ? 'Reply in the thread' : 'Reply (starts a thread)'} autoFocus={focus} compact
        onSend={(text, files) => postThreadReply(groupId, root.ev, text, files)} />}
      <div className="thread-foot">
        <button className="link thread-hide" onClick={onHide}>Hide</button>
        <button className="link thread-hide thread-open" onClick={onOpen}>Open thread<ChevronRightIcon /></button>
      </div>
    </div>
  )
}

function InlineReply({ r, groupId, onAct }: { r: Message; groupId: string; onAct: () => void }) {
  const hold = useHold(onAct, !!r.ev.pending)
  return (
    <div id={'m-' + r.ev.id} className={'thread-reply' + (r.ev.pending ? ' pending' : '')} {...hold}>
      <Avatar pubkey={r.ev.pubkey} size={24} />
      <div className="body">
        <div className="who">{displayName(r.ev.pubkey)}<time>{fmtTime(r.ev.created_at)}</time>{r.edited && <span className="edited">edited</span>}</div>
        {r.text && <RichText text={r.text} groupId={groupId} />}
        <MediaGrid media={r.media} />
      </div>
    </div>
  )
}

/** Edit your own message: its text, with mentions shown as @Name. Saved as an edit event. */
function EditSheet({ m, groupId, onClose }: { m: Message; groupId: string; onClose: () => void }) {
  const names = useMemo(() => {
    const out = new Map<string, string>() // "@Name" → nostr:npub…
    for (const x of m.text.matchAll(/nostr:(npub1[02-9ac-hj-np-z]{58})/g)) {
      try { const d = nip19.decode(x[1]); if (d.type === 'npub') out.set('@' + displayName(d.data), x[0]) } catch { /* not a key */ }
    }
    for (const x of m.text.matchAll(NADDR)) { const label = plainRefs(x[0]); if (label !== x[0]) out.set(label, x[0]) }
    return out
  }, [m.text])
  const shown = useMemo(() => { let t = m.text; for (const [name, ref] of names) t = t.split(ref).join(name); return t }, [m.text, names])
  const [text, setText] = useState(shown)
  const ta = useRef<HTMLTextAreaElement>(null)
  useEffect(() => { const el = ta.current; if (el) { el.focus(); el.setSelectionRange(el.value.length, el.value.length) } }, [])
  const save = async () => {
    let t = text.trim()
    for (const [name, ref] of [...names].sort((a, b) => b[0].length - a[0].length)) t = t.split(name).join(ref)
    if (t && t !== m.text) await editMessage(groupId, m, t)
    onClose()
  }
  return (
    <Sheet title="Edit message" onClose={onClose}>
      <Form onSubmit={async (e) => { e.preventDefault(); await save() }}>
        <textarea ref={ta} className="input edit-text" rows={4} value={text} onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); e.currentTarget.form?.requestSubmit() } }} />
        <div className="row-btns">
          <button type="button" className="btn ghost" onClick={onClose}>Cancel</button>
          <button className="btn" disabled={!text.trim() || text.trim() === shown}>Save</button>
        </div>
      </Form>
    </Sheet>
  )
}

/** An older thread, back in the feed where its latest reply came: the message it answers as one
 *  grey quote line, then the reply (proposal A). */
function ThreadActivity({ root, last, open, onOpen }: { root: Message; last: Message; open: boolean; onOpen: () => void }) {
  const snip = plainRefs(root.text || (root.media.length ? 'a photo' : ''), '').replace(/\s+/g, ' ').trim().slice(0, 80)
  return (
    <button className={'thread-activity' + (open ? ' open' : '')} onClick={onOpen} aria-expanded={open}>
      <Avatar pubkey={last.ev.pubkey} size={28} />
      <span className="t">
        <span className="quote">↳ {displayName(root.ev.pubkey)}: {snip}{root.text.length > 80 ? '…' : ''}</span>
        <span className="who"><b>{displayName(last.ev.pubkey)}</b><time>{fmtTime(last.ev.created_at)}</time></span>
        <span className="reply">{replyText(last).slice(0, 200) || 'a photo'}</span>
      </span>
    </button>
  )
}

/** The nsite a message shares (nsite:<npub>[/<name>], a preview or gateway link), as text. */
export function nsiteRef(text: string): string | null {
  const m = /nsite:npub1[02-9ac-hj-np-z]{58}(?:\/[a-z0-9-]{1,13})?|https:\/\/preview\.[^/\s]+\/p\/npub1[02-9ac-hj-np-z]{58}(?:@[a-z0-9-]{1,13})?|https:\/\/npub1[02-9ac-hj-np-z]{58}\.[a-z0-9.-]+/.exec(text || '')
  return m ? m[0] : null
}

/** An admin makes the version a message shares the website (Belfry copies it, website.py). */
async function makeWebsite(m: Message) {
  const ref = nsiteRef(m.text)
  if (!ref || !confirm(`Publish this version as ${BELFRY.domain}? The belfry copies it; the author's preview stays theirs.`)) return
  try {
    const r = await signedFetch(`${BELFRY.api}/website/publish`, 'POST', { ref, message: m.ev.id })
    const d = await r.json().catch(() => ({}))
    if (!r.ok) throw new Error(d.message || 'Not published.')
    alert(`Published: ${d.files} files are now https://${BELFRY.domain}. A note went to #website.`)
  } catch (e) {
    alert(e instanceof Error ? e.message : String(e))
  }
}

/** A guest who joined with a name only (an event's guest, an older link), once they did
 *  something (a message, a reply, a reaction): a slim line above the composer to save the account.
 *  Save opens the same sheet as joining from guest mode: the e-mail, then the code sent to it. */
function SaveAccountBar() {
  useVersion()
  useGuestMode()
  const g = guestInfo()
  const [open, setOpen] = useState(false)
  const [savedAs, setSavedAs] = useState('')
  const [hidden, setHidden] = useState(() => { try { return sessionStorage.getItem('belfry-save-hidden') === '1' } catch { return false } })
  const acted = all(me()).some((e) => e.pubkey === me() && (e.kind === 9 || e.kind === 1111 || e.kind === 7)) || outboxItems().some((o) => [9, 1111, 7].includes(o.kind))
  const cohost = coHostToken()
  if (savedAs) return <div className="save-bar done" role="status">Saved. Sign in with {savedAs} on any device.<button className="link" onClick={() => setSavedAs('')}>OK</button></div>
  if (!g || g.saved || g.reading || (!acted && !cohost) || (hidden && !cohost)) return null
  return (
    <div className="save-bar" role="region" aria-label="Save your account">
      <span>{cohost ? 'You were invited as a co-host: save your account with your e-mail to become one.' : 'Save your account: add your e-mail to come back from any device.'}</span>
      <button className="btn small" onClick={() => setOpen(true)}>Save</button>
      <button className="link close" aria-label="Not now" onClick={() => { setHidden(true); try { sessionStorage.setItem('belfry-save-hidden', '1') } catch { /* private mode */ } }}>✕</button>
      {open && <JoinSheet saveOnly onClose={() => setOpen(false)} onSaved={setSavedAs} />}
    </div>
  )
}

/** Under a message with replies: one line, the repliers' faces, how many, when, and the start of
 *  the last reply. It opens the thread (Xavier, 2026-11-11: no more replies unfolded inline). */
function ThreadLine({ groupId, rootId, thread, open, onClick }: { groupId: string; rootId: string; thread: ThreadInfo; open: boolean; onClick: () => void }) {
  const last = threadReplies(groupId, rootId).at(-1)
  const said = last ? replyText(last).slice(0, 120) || (last.media.length ? 'a photo' : '') : ''
  return (
    <button className={'thread-line' + (open ? ' open' : '')} onClick={onClick} aria-expanded={open}>
      <span className="faces">{thread.who.slice(0, 3).map((pk) => <Avatar key={pk} pubkey={pk} size={18} />)}</span>
      <span className="n">{open ? 'Hide replies' : `${thread.count} ${thread.count === 1 ? 'reply' : 'replies'}`}</span>
      {!open && <time>{fmtTime(thread.last)}</time>}
      {!open && last && said && <span className="last"><b>{displayName(last.ev.pubkey)}</b>: {said}</span>}
      {!open && <span className="caret" aria-hidden="true"><ChevronRightIcon /></span>}
    </button>
  )
}

/** A reply's text without the "@someone" it starts with (the thread says whom it answers). */
export function replyText(m: Message): string {
  return plainRefs(m.text.replace(/^(\s*(nostr:npub1[02-9ac-hj-np-z]+|@\S+)[,:]?\s+)+/, ''), '').replace(/\s+/g, ' ').trim()
}

// Only a to-do's status shows in its thread (Xavier, 2026-11-11): comments are replies already,
// and "stopped working" or "let go" is noise.
const STEP_WORDS: Partial<Record<TaskStep['what'], string>> = {
  created: 'made a todo', claim: 'took it', working: 'started working on it', done: 'marked it done', undone: 'reopened it',
}
export interface StepGroup { t: Task; who: string; at: number; what: TaskStep['what'][] }

/** The status steps of these to-dos, one line per to-do, person and moment: steps by the same
 *  person within two minutes of each other merge ("made a to-do and took it"). A second
 *  "started working" after the first is dropped. */
export function stepGroups(tasks: Task[]): StepGroup[] {
  const out: StepGroup[] = []
  for (const t of tasks) {
    let started = false
    let cur: StepGroup | null = null
    for (const s of [...t.history].sort((a, b) => a.at - b.at)) {
      if (!STEP_WORDS[s.what]) continue
      if (s.what === 'working') { if (started) continue; started = true }
      if (s.what === 'done' || s.what === 'undone') started = false
      if (cur && cur.who === s.who && s.at - cur.at <= 120 && !cur.what.includes(s.what)) { cur.what.push(s.what); continue }
      cur = { t, who: s.who, at: s.at, what: [s.what] }
      out.push(cur)
    }
  }
  return out
}

/** "made a to-do, took it and started working on it" */
export function stepText(what: TaskStep['what'][]): string {
  const w = what.map((x) => STEP_WORDS[x]!)
  return w.length > 1 ? w.slice(0, -1).join(', ') + ' and ' + w.at(-1) : w[0]
}

/** A to-do's status, as one line in the thread: who, what, which to-do, when. */
function TodoStepLine({ g }: { g: StepGroup }) {
  const d = new Date(g.at * 1000)
  const last = g.what.at(-1)!
  return (
    <div className={'todo-step ' + last}>
      <span className="ico-wrap"><TodoIcon /></span>
      <span className="what"><b>{displayName(g.who)}</b> {stepText(g.what)}: “{g.t.title}”</span>
      <time dateTime={d.toISOString()} title={d.toLocaleString()}>{d.toDateString() === new Date().toDateString() ? '' : fmtDay(d) + ' '}{fmtTime(g.at)}</time>
    </div>
  )
}

// ---- drafts: unsent text per conversation, on this device only (localStorage) ----------
const DRAFT = 'osv-draft:'
interface Draft { text: string; picked: [string, string][]; chans: [string, string][]; at: number }
function readDraft(key: string): Draft | null {
  try { const d = JSON.parse(localStorage.getItem(DRAFT + key) || 'null') as Draft | null; return d && Date.now() - d.at < 30 * 86400e3 ? d : null } catch { return null }
}
function writeDraft(key: string, text: string, picked: Map<string, string>, chans: Map<string, string>) {
  try {
    if (!text.trim()) localStorage.removeItem(DRAFT + key)
    else localStorage.setItem(DRAFT + key, JSON.stringify({ text: text.slice(0, 20000), picked: [...picked], chans: [...chans], at: Date.now() }))
  } catch { /* private mode or full: no draft */ }
}
