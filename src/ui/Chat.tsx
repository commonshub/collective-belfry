import { Files, Members, Tabs, Tasks } from './ChannelInfo'
import { FollowButton } from './Follow'
import { address, unreadTotal } from '../core/dm'
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { createChannel, createPod, DEFAULT_PROMPT, deleteMessage, joinGroup, postMessage, postThreadReply, react, topEmojis } from '../core/actions'
import { ANNOUNCE, channelIcon, group, isAnnouncement, displayName, eventCoordOf, groups, hashtags, messages, podOf, proposalFor, proposalOf, profiles, reactions, threadReplies, threadsOf, inboxStates, type CardState, type Group, type Message, type Reaction, type ThreadInfo } from '../core/model'
import { useVersion } from '../core/store'
import { me, retry } from '../core/sync'
import { Avatar, fmtDay, fmtTime, go, MediaGrid, Sheet, SyncDot } from './common'
import { EventCard, EventsBar, NewEventSheet } from './Calendar'
import { RichText, TopicsBar } from './Topics'
import { NewProposalSheet, ProposalCard, ProposalHeader } from './Proposals'
import { cardOf, FullMail, InboxCardView, mailOf, mailText, SentCardView, sentOf, ViaMail } from './MailPost'
import { knownGroupsKey } from '../core/dm'
import { BELFRY, NAME, THEME } from '../config'
import { BellIcon, CalendarIcon, GearIcon, MailIcon, MenuIcon } from './icons'
import { closeDrawer, toggleDrawer } from './drawer'

// ---- channel list (the "server sidebar", full screen on a phone) ------------

/** The community's name from the belfry (/.well-known/belfry.json), else the domain. */
let communityName = ''
let asked = false
function useCommunityName(fallback: string) {
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
      <span className="t"><b>{p?.name ?? displayName(me())}</b><small>{address() ?? p?.nip05 ?? ''}</small></span>
      <span className="hint"><GearIcon /></span>
    </button>
  )
}

export function ChannelList({ current, side = false, section, drawer = false }: { current?: string; side?: boolean; section?: string; drawer?: boolean }) {
  useVersion()
  const title = useCommunityName(side ? NAME : 'Channels')
  const [creating, setCreating] = useState(false)
  const all = groups()
  const chans = all.filter((g) => !g.isEvent && !g.isPod && !g.isProposal)
  const events = all.filter((g) => g.isEvent)
  const pods = all.filter((g) => g.isPod || g.isProposal)
  const ids = new Set(all.map((g) => g.id))
  const last = (id: string) => {
    const m = messages(id).at(-1)
    return m ? `${displayName(m.ev.pubkey)}: ${m.text || (m.media.length ? 'Photo' : '')}` : undefined
  }
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
        <h1 style={{ paddingLeft: drawer ? 0 : 8 }}>{side ? title : 'Channels'}</h1>
        <button className="icon-btn" aria-label="New channel" onClick={() => setCreating(true)}>＋</button>
        {!side && <MeButton />}
      </div>
      <div className="scroll">
        <button className={'chan' + (section === 'dm' ? ' on' : '')} onClick={() => closeDrawer('dm')}>
          <span className="hash"><MailIcon unread={unreadTotal() > 0} /></span>
          <span className="t"><b>Messages</b><small>Direct messages and e-mail{address() ? ` · ${address()}` : ''}</small></span>
          {unreadTotal() > 0 && <span className="badge lime">{unreadTotal()}</span>}
        </button>
        <button className={'chan' + (section === 'cal' ? ' on' : '')} onClick={() => closeDrawer('cal')}>
          <span className="hash"><CalendarIcon /></span>
          <span className="t"><b>All events</b><small>Every channel's events</small></span>
        </button>
        <div className="section-title">Channels <span>{chans.length + pods.length}</span></div>
        {chans.map((g) => <Tree key={g.id} g={g} />)}
        {orphans.map((g) => <Tree key={g.id} g={g} />)}
        {chans.length === 0 && <div className="empty">No channels yet. They appear here once the village relay answers.</div>}
        {events.length > 0 && <div className="section-title">Event chats <span>{events.length}</span></div>}
        {events.map((g) => <Row key={g.id} g={g} />)}
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
  const [name, setName] = useState('')
  const [about, setAbout] = useState('')
  return (
    <Sheet title="New channel" onClose={onClose}>
      <form onSubmit={async (e) => {
        e.preventDefault()
        const id = await createChannel(name.trim(), about.trim())
        onClose()
        go('c/' + id)
      }}>
        <label className="field"><span>Name</span>
          <input className="input" autoFocus placeholder="e.g. rust-hackers" value={name} onChange={(e) => setName(e.target.value)} /></label>
        <label className="field"><span>Description</span>
          <textarea className="input" rows={3} placeholder="What is this channel about?" value={about} onChange={(e) => setAbout(e.target.value)} /></label>
        <button className="btn" disabled={!name.trim()}>Create channel</button>
      </form>
    </Sheet>
  )
}

// ---- a channel's messages ------------------------------------------------------

export function ChannelView({ id, topic }: { id: string; topic?: string }) {
  const openTopic = (t?: string) => go(`c/${encodeURIComponent(id)}` + (t ? `/t/${encodeURIComponent(t)}` : ''))
  useVersion()
  const all = groups()
  const g = all.find((x) => x.id === id)
  const parent = g?.isPod || g?.isProposal ? all.find((x) => x.id === g.parent) : undefined
  const where = `${g?.isProposal ? 'Proposal' : 'Pod'} in ${channelIcon(parent)}${parent?.isPod ? ' ' : ''}${parent?.name ?? g?.parent}`
  const sub = g?.isPod || g?.isProposal ? (g.about ? `${where} · ${g.about}` : where) : g?.about
  const [podTab, setPodTab] = useState<'chat' | 'tasks' | 'members' | 'files'>('chat')
  useEffect(() => setPodTab('chat'), [id])
  return (
    <div className="screen">
      <div className="topbar">
        <button className="icon-btn menu-btn" aria-label="Channels" aria-expanded="false" onClick={toggleDrawer}><MenuIcon /></button>
        <h1 className="tap" onClick={() => go(`c/${encodeURIComponent(id)}/info`)}>
          {channelIcon(g)} {g?.name ?? id}
          {sub && <small>{sub}</small>}
        </h1>
        <FollowButton channel={id} />
        <MeButton />
      </div>
      <EventsBar channel={id} />
      <PodsBar channel={id} />
      {g?.isProposal && <ProposalHeader groupId={id} />}
      {g?.isPod && g.admins.has(me()) && g.applications.length > 0 && (
        <button className="join-bar" style={{ borderTop: 0, borderBottom: '1px solid var(--line)', width: '100%' }} onClick={() => go(`c/${encodeURIComponent(id)}/info/members`)}>
          <span>{g.applications.length} {g.applications.length === 1 ? 'person asks' : 'people ask'} to join</span>
          <span className="badge orange">Review</span>
        </button>
      )}
      {g?.isPod && <Tabs tab={podTab} setTab={setPodTab} tabs={[['chat', 'Chat'], ['tasks', 'Tasks'], ['members', 'Members'], ['files', 'Files']]} />}
      {podTab === 'chat' && <>
        <TopicsBar groupId={id} active={topic} onOpen={openTopic} />
        <Conversation id={id} events topic={topic} onTopic={openTopic} readOnly={!!g?.isPod && g.closed && !g.members.has(me())} />
      </>}
      {podTab !== 'chat' && (
        <div className="scroll">
          {podTab === 'tasks' && <Tasks groupId={id} />}
          {podTab === 'members' && <Members groupId={id} />}
          {podTab === 'files' && <Files groupId={id} />}
        </div>
      )}
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
        <button className="btn small" onClick={() => void joinGroup(g.id)}>Join</button>
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
          <form onSubmit={async (e) => { e.preventDefault(); await joinGroup(g.id, answer.trim()); setAsking(false) }}>
            <p className="apply-q">{g.prompt || DEFAULT_PROMPT}</p>
            <textarea className="input" rows={4} autoFocus value={answer} onChange={(e) => setAnswer(e.target.value)} />
            <div style={{ height: 12 }} />
            <button className="btn" disabled={!answer.trim()}>Send my application</button>
            <p className="hint" style={{ marginTop: 10 }}>The pod’s admins see your answer and admit you, or not.</p>
          </form>
        </Sheet>
      )}
    </>
  )
}

/** Under a channel's name (after its events): the pods inside it. Nothing when none. */
function PodsBar({ channel }: { channel: string }) {
  const subs = groups().filter((x) => (x.isPod || x.isProposal) && x.parent === channel)
  const props = new Map(subs.filter((x) => x.isProposal).map((x) => [x.id, proposalFor(x.id)]))
  // pods, then proposals still in play (passed, rejected and closed ones stay in the channel list)
  const shown = [...subs.filter((x) => x.isPod), ...subs.filter((x) => x.isProposal && !['passed', 'rejected', 'closed'].includes(props.get(x.id)?.status ?? 'draft'))]
  if (!shown.length) return null
  return (
    <div className="pods-bar">
      {shown.map((p) => (
        <button key={p.id} onClick={() => go('c/' + encodeURIComponent(p.id))}>
          {channelIcon(p)} {p.name}
          {p.isPod ? <small>{p.members.size}</small> : props.get(p.id) && <small>v{props.get(p.id)!.version}</small>}
        </button>
      ))}
    </div>
  )
}

/** The post a pod leaves in its parent: a card to open (or join) it. */
function PodCard({ id, fallback }: { id: string; fallback: string }) {
  const g = groups().find((x) => x.id === id)
  if (!g) return <div className="text">{fallback}</div>
  const inIt = g.members.has(me())
  return (
    <div className="ev-card pod-card" role="button" tabIndex={0} onClick={() => go('c/' + encodeURIComponent(id))}>
      <div className="date pod-ic">⬡</div>
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
      <form onSubmit={async (e) => {
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
      </form>
    </Sheet>
  )
}

/** Top right: your avatar (opens Me), with the number of unread messages on it. */
export function MeButton() {
  const n = unreadTotal()
  return (
    <button className="icon-btn me-btn" aria-label={n ? `Me · ${n} unread messages` : 'Me'} onClick={() => go('me')}>
      <Avatar pubkey={me()} size={30} />
      <SyncDot />
      {n > 0 && <span className="count" aria-hidden="true">{n > 99 ? '99+' : n}</span>}
    </button>
  )
}

export function Conversation({ id, events = false, readOnly = false, topic, onTopic }: { id: string; events?: boolean; readOnly?: boolean; topic?: string; onTopic?: (t?: string) => void }) {
  const [newEvent, setNewEvent] = useState(false)
  const [newPod, setNewPod] = useState(false)
  const [newProp, setNewProp] = useState(false)
  useVersion()
  const g = group(id)
  const admin = !!g?.admins.has(me())
  const list = messages(id).filter((m) => !topic || (topic === ANNOUNCE ? isAnnouncement(m, g) : m.topics.includes(topic)))
  const ps = profiles()
  const rx = reactions(id)
  const th = threadsOf(id)
  const gkey = knownGroupsKey()
  const inbox = { states: inboxStates(id, gkey), key: gkey, canSend: admin, name: g?.name ?? id }
  const openThread = (m: Message) => go(`c/${encodeURIComponent(id)}/thread/${m.ev.id}`)
  const [acting, setActing] = useState<Message | null>(null)
  const scroller = useRef<HTMLDivElement>(null)
  const stick = useRef(true)

  useLayoutEffect(() => {
    const el = scroller.current
    if (el && stick.current) el.scrollTop = el.scrollHeight
  })

  return (
    <>
      <div className="scroll" ref={scroller}
        onScroll={(e) => { const el = e.currentTarget; stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80 }}>
        <div className="messages">
          {list.length === 0 && <div className="empty">{topic === ANNOUNCE ? 'No announcements yet. Only the admins post them.' : topic ? `Nothing in #${topic} yet. What you send here carries it.` : 'No messages yet. Say hi.'}</div>}
          {list.map((m, i) => <Row key={m.ev.id} m={m} prev={list[i - 1]} groupId={id}
            name={!ps.get(m.ev.pubkey)?.name && m.ev.tags.some((t) => t[0] === 'client' && t[1] === 'belfry-drop') ? 'Drop box' : displayName(m.ev.pubkey, ps)}
            reactions={rx.get(m.ev.id) ?? []} onAct={() => setActing(m)} topic={topic} onTopic={onTopic} announce={isAnnouncement(m, g)}
            thread={th.get(m.ev.id)} onThread={() => openThread(m)} inbox={inbox} />)}
        </div>
      </div>
      {!readOnly && (topic !== ANNOUNCE || admin) && <Composer placeholder={topic && topic !== ANNOUNCE ? `Message #${topic}` : undefined} canAnnounce={admin} announcing={topic === ANNOUNCE}
        onSend={(text, files, announce) => { stick.current = true; return postMessage(id, text, files, topic === ANNOUNCE ? undefined : topic, announce) }}
        onEvent={events ? () => setNewEvent(true) : undefined} onPod={events ? () => setNewPod(true) : undefined} onProposal={events ? () => setNewProp(true) : undefined} />}
      {newProp && <NewProposalSheet parent={id} onClose={() => setNewProp(false)} />}
      {newPod && <NewPod parent={id} onClose={() => setNewPod(false)} />}
      {acting && <MessageActions m={acting} groupId={id} mine={rx.get(acting.ev.id) ?? []} onClose={() => setActing(null)}
        thread={th.get(acting.ev.id)} onThread={() => openThread(acting)} />}
      {newEvent && <NewEventSheet channel={id} onClose={() => setNewEvent(false)} />}
    </>
  )
}

/** A thread: the message it hangs off, then the replies (#/c/<id>/thread/<message id>). */
export function ThreadView({ groupId, rootId }: { groupId: string; rootId: string }) {
  useVersion()
  const g = group(groupId)
  const ps = profiles()
  const rx = reactions(groupId)
  const root = messages(groupId).find((m) => m.ev.id === rootId)
  const replies = threadReplies(groupId, rootId)
  const [acting, setActing] = useState<Message | null>(null)
  const scroller = useRef<HTMLDivElement>(null)
  useLayoutEffect(() => { const el = scroller.current; if (el) el.scrollTop = el.scrollHeight }, [replies.length])
  const nameOf = (m: Message) => displayName(m.ev.pubkey, ps)
  const back = () => go(`c/${encodeURIComponent(groupId)}`)
  const gkey = knownGroupsKey()
  const inbox = { states: inboxStates(groupId, gkey), key: gkey, canSend: !!g?.admins.has(me()), name: g?.name ?? groupId }
  const card = root ? cardOf(root.ev, gkey) : null
  return (
    <div className="screen">
      <div className="topbar">
        <button className="icon-btn" aria-label={`Back to ${g?.name ?? groupId}`} onClick={back}>‹</button>
        <h1>Thread<small>{channelIcon(g)} {g?.name ?? groupId}</small></h1>
        <MeButton />
      </div>
      {card && <div className="thread-private">Team only. {card.name || card.from} never sees this thread.</div>}
      <div className="scroll" ref={scroller}>
        <div className="messages">
          {root ? <Row m={root} name={nameOf(root)} groupId={groupId} reactions={rx.get(root.ev.id) ?? []} onAct={() => setActing(root)} announce={isAnnouncement(root, g)} inbox={inbox} onThread={() => {}} thread={{ count: replies.length, last: 0, who: [] }} />
            : <div className="empty">This message is not here (yet, or any more).</div>}
          <div className="thread-sep"><i />{replies.length ? `${replies.length} ${replies.length === 1 ? 'reply' : 'replies'}` : 'No replies yet'}<i /></div>
          {replies.map((m, i) => <Row key={m.ev.id} m={m} prev={replies[i - 1]} head={i === 0} name={nameOf(m)} groupId={groupId}
            reactions={rx.get(m.ev.id) ?? []} onAct={() => setActing(m)} announce={false} />)}
        </div>
      </div>
      {root && <Composer placeholder="Reply in the thread" onSend={(text, files) => postThreadReply(groupId, root.ev, text, files)} />}
      {acting && <MessageActions m={acting} groupId={groupId} mine={rx.get(acting.ev.id) ?? []} onClose={() => setActing(null)} />}
    </div>
  )
}

function Row({ m, prev, name, groupId, reactions: rx, onAct, topic, onTopic, announce, thread, onThread, head, inbox }: { inbox?: { states: Map<string, CardState>; key?: string; canSend: boolean; name: string }; head?: boolean; m: Message; prev?: Message; name: string; groupId: string; reactions: Reaction[]; onAct: () => void; topic?: string; onTopic?: (t?: string) => void; announce: boolean; thread?: ThreadInfo; onThread?: () => void }) {
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
  // press and hold (phones) or right-click: react, or delete your own
  const hold = useRef<ReturnType<typeof setTimeout> | null>(null)
  const cancel = () => { if (hold.current) clearTimeout(hold.current); hold.current = null }
  if (card || sent) return (
    <>
      {newDay && <div className="day">{fmtDay(d)}</div>}
      <div className="msg first card-row">
        {card ? <InboxCardView m={m} card={card} groupId={groupId} channelName={inbox!.name} state={inbox!.states.get(m.ev.id)} canSend={inbox!.canSend}
          thread={thread} onThread={onThread ?? (() => {})} />
          : <SentCardView m={m} sent={sent!} groupId={groupId} />}
      </div>
    </>
  )
  return (
    <>
      {newDay && <div className="day">{fmtDay(d)}</div>}
      <div className={'msg' + (first || announce || mail ? ' first' : '') + (p ? ' pending' : '') + (announce ? ' announce' : '')}
        onContextMenu={(e) => { if (p) return; e.preventDefault(); cancel(); onAct() }}
        onPointerDown={(e) => {
          if (p || e.pointerType === 'mouse' || (e.target as HTMLElement).closest('button, a, video')) return
          hold.current = setTimeout(() => { hold.current = null; navigator.vibrate?.(10); onAct() }, 450)
        }}
        onPointerUp={cancel} onPointerMove={cancel} onPointerCancel={cancel}>
        {first || announce || mail ? <Avatar pubkey={m.ev.pubkey} /> : <div className="gutter" />}
        <div className="body">
          {(first || announce || mail) && <div className="who">{name}<time>{fmtTime(m.ev.created_at)}</time>{mail && <ViaMail />}{announce && <span className="badge announce-badge">📣 Announcement</span>}</div>}
          {mail?.subject && <div className="mail-subject">{mail.subject}</div>}
          {eventCoordOf(m.ev) ? <EventCard coord={eventCoordOf(m.ev)!} fallback={m.text} /> : podOf(m.ev) ? <PodCard id={podOf(m.ev)!} fallback={m.text} /> : proposalOf(m.ev) ? <ProposalCard id={proposalOf(m.ev)!} fallback={m.text} /> : m.text && <RichText text={mail ? mailText(m.text, mail) : m.text} groupId={groupId} onOpen={onTopic} />}
          {mail?.original && <button className="link full-mail-btn" onClick={() => setFullMail(true)}>Full e-mail</button>}
          {fullMail && mail && <FullMail mail={mail} onClose={() => setFullMail(false)} />}
          {extra.length > 0 && <div className="msg-topics">{extra.map((t) => <button key={t} onClick={() => onTopic?.(t)}>#{t}</button>)}</div>}
          <MediaGrid media={m.media} />
          {rx.length > 0 && (
            <div className="reactions">
              {rx.map((r) => (
                <button key={r.emoji} className={r.mine ? 'on' : ''} title={[...r.who].map((k) => displayName(k)).join(', ')}
                  onClick={() => void react(groupId, m.ev, r.emoji, r.mine)}>
                  <span>{r.emoji}</span> {r.who.size}
                </button>
              ))}
            </div>
          )}
          {thread && thread.count > 0 && onThread && (
            <button className="thread-chip" onClick={onThread}>
              <span className="faces">{thread.who.slice(0, 3).map((pk) => <Avatar key={pk} pubkey={pk} size={18} />)}</span>
              {thread.count} {thread.count === 1 ? 'reply' : 'replies'}<time>{fmtTime(thread.last)}</time>
            </button>
          )}
          {p && p.status === 'failed' && (
            <div className="state failed">Not sent: {p.error} · <button className="link" onClick={() => retry(p.localId)}>Retry</button></div>
          )}
          {p && p.status === 'queued' && <div className="state">{p.tries ? 'Waiting for connection…' : 'Sending…'}</div>}
        </div>
        {!p && <button className="react-btn" aria-label="React" onClick={onAct}>☺</button>}
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

function MessageActions({ m, groupId, mine, onClose, thread, onThread }: { m: Message; groupId: string; mine: Reaction[]; onClose: () => void; thread?: ThreadInfo; onThread?: () => void }) {
  const [more, setMore] = useState(false)
  const pick = (emoji: string) => {
    void react(groupId, m.ev, emoji, mine.find((r) => r.emoji === emoji)?.mine)
    onClose()
  }
  const isOn = (emoji: string) => mine.some((r) => r.emoji === emoji && r.mine)
  return (
    <Sheet title="Message" onClose={onClose}>
      <div className="emoji-row">
        {topEmojis(6).map((e) => <button key={e} className={isOn(e) ? 'on' : ''} onClick={() => pick(e)}>{e}</button>)}
        <button aria-label="More emojis" className={more ? 'on' : ''} onClick={() => setMore(!more)}>＋</button>
      </div>
      {more && (
        <div className="emoji-grid">
          {MORE_EMOJIS.map((e) => <button key={e} className={isOn(e) ? 'on' : ''} onClick={() => pick(e)}>{e}</button>)}
        </div>
      )}
      {onThread && (
        <button className="btn ghost" style={{ marginTop: 12 }} onClick={() => { onClose(); onThread() }}>
          {thread?.count ? `Open the thread (${thread.count})` : 'Start a thread'}
        </button>
      )}
      {m.ev.pubkey === me() && (
        <button className="btn ghost" style={{ marginTop: 12 }} onClick={() => { onClose(); if (confirm('Delete this message?')) void deleteMessage(groupId, m.ev.id) }}>Delete message</button>
      )}
    </Sheet>
  )
}

// ---- composer --------------------------------------------------------------------

export function Composer({ onSend, onEvent, onPod, onProposal, placeholder = 'Message', canAnnounce = false, announcing = false }: { onSend: (text: string, files: File[], announce: boolean) => Promise<unknown>; onEvent?: () => void; onPod?: () => void; onProposal?: () => void; placeholder?: string; canAnnounce?: boolean; announcing?: boolean }) {
  const [text, setText] = useState('')
  const [ann, setAnn] = useState(announcing)
  useEffect(() => setAnn(announcing), [announcing])
  const [menu, setMenu] = useState(false)
  const [files, setFiles] = useState<File[]>([])
  const ta = useRef<HTMLTextAreaElement>(null)
  const picker = useRef<HTMLInputElement>(null)
  const previews = useMemo(() => files.map((f) => (f.type.startsWith('image/') ? URL.createObjectURL(f) : '')), [files])
  useEffect(() => () => previews.forEach((u) => u && URL.revokeObjectURL(u)), [previews])

  useLayoutEffect(() => {
    const el = ta.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = Math.min(el.scrollHeight, 140) + 'px'
  }, [text])

  const send = async () => {
    if (!text.trim() && !files.length) return
    const t = text.trim()
    const f = files
    const a = canAnnounce && ann
    setText('')
    setFiles([])
    if (!announcing) setAnn(false)
    await onSend(t, f, a)
  }

  return (
    <div className="composer">
      {canAnnounce && ann && (
        <div className="announce-mode">
          <span>📣 Announcement: sent to the channel and e-mailed to its followers</span>
          {!announcing && <button aria-label="Not an announcement" onClick={() => setAnn(false)}>✕</button>}
        </div>
      )}
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
      <div className="bar">
        <button className="icon-btn" aria-label={onEvent ? 'Add photos, files, an event, a proposal or a pod' : 'Add photos or files'}
          onClick={() => (onEvent || canAnnounce ? setMenu(true) : picker.current?.click())}>＋</button>
        <input ref={picker} type="file" multiple hidden
          onChange={(e) => { setFiles([...files, ...Array.from(e.target.files ?? [])]); e.target.value = '' }} />
        <textarea ref={ta} rows={1} placeholder={canAnnounce && ann ? 'Announcement' : placeholder} value={text} onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            // Enter sends on a keyboard; on phones the return key adds a line
            if (e.key === 'Enter' && !e.shiftKey && matchMedia('(pointer: fine)').matches) { e.preventDefault(); void send() }
          }} />
        <button className="send" aria-label="Send" disabled={!text.trim() && !files.length} onClick={() => void send()}>↑</button>
      </div>
      {menu && (onEvent || canAnnounce) && (
        <Sheet title="Add" onClose={() => setMenu(false)}>
          <button className="btn ghost" onClick={() => { setMenu(false); picker.current?.click() }}>📷 Photos or files</button>
          <div style={{ height: 8 }} />
          {canAnnounce && <><button className="btn ghost" onClick={() => { setMenu(false); setAnn(true); ta.current?.focus() }}>📣 An announcement</button><div style={{ height: 8 }} /></>}
          {onEvent && <button className="btn ghost" onClick={() => { setMenu(false); onEvent() }}>📅 An event</button>}
          {onProposal && <><div style={{ height: 8 }} /><button className="btn ghost" onClick={() => { setMenu(false); onProposal() }}>📝 A proposal</button></>}
          {onPod && <><div style={{ height: 8 }} /><button className="btn ghost" onClick={() => { setMenu(false); onPod() }}>⬡ A pod (working group)</button></>}
        </Sheet>
      )}
    </div>
  )
}
