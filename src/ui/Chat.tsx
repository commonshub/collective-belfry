import { nip19 } from 'nostr-tools'
import { FollowButton } from './Follow'
import { address, unreadTotal } from '../core/dm'
import type React from 'react'
import { Fragment, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { addTask, createChannel, createPod, DEFAULT_PROMPT, deleteMessage, editMessage, joinGroup, pinMessage, taskAction, postMessage, postThreadReply, react, topEmojis } from '../core/actions'
import { ANNOUNCE, channelIcon, group, guestJoins, type GuestJoin, isAnnouncement, displayName, eventCoordOf, groups, hashtags, messages, podOf, proposalOf, profiles, reactions, threadReplies, threadsOf, inboxStates, pinned, tasklist, type Task, type CardState, type Group, type Message, type Reaction, type ThreadInfo } from '../core/model'
import { all, outboxItems, useVersion } from '../core/store'
import { me, retry, signedFetch } from '../core/sync'
import { Avatar, fmtDay, fmtTime, go, MediaGrid, Sheet, SyncDot, Btn, Form, up } from './common'
import { EventCard, NewEventSheet } from './Calendar'
import { RichText } from './Topics'
import { NewProposalSheet, ProposalCard } from './Proposals'
import { cardOf, FullMail, InboxCardView, mailOf, mailText, SentCardView, sentOf, ViaMail } from './MailPost'
import { knownGroupsKey } from '../core/dm'
import { BELFRY, NAME, THEME } from '../config'
import { AtIcon, CalendarIcon, DocIcon, HexIcon, ImageIcon, MegaphoneIcon, CopyIcon, GearIcon, GlobeIcon, LinkIcon, MailIcon, MenuIcon, MoreIcon, PencilIcon, PushPinIcon, PodShape, TodoIcon, ReplyIcon, SearchIcon, SmilePlusIcon, ThreadIcon, TrashIcon } from './icons'
import { closeDrawer, toggleDrawer } from './drawer'
import { ChannelHeader } from './ChannelHeader'
import { canAdmin, isAdmin } from '../core/access'
import { guestInfo, setGuestInfo } from '../core/auth'
import { InviteSheet } from './Invite'
import { mayPin } from './ChannelLine'
import { inChannel, inviteMember, isGuest } from '../core/invite'
import { openProfile } from './Profile'

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
      <span className="t"><b>{p?.name ?? displayName(me())}</b><small>{address()}</small></span>
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
        {chans.length === 0 && <div className="empty">No channels yet. They appear here once the relay answers.</div>}
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
      <Form onSubmit={async (e) => {
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
      </Form>
    </Sheet>
  )
}

// ---- a channel's messages ------------------------------------------------------

export function ChannelView({ id, topic, focus }: { id: string; topic?: string; focus?: string }) {
  const openTopic = (t?: string) => go(`c/${encodeURIComponent(id)}` + (t ? `/t/${encodeURIComponent(t)}` : ''))
  useVersion()
  const all = groups()
  const g = all.find((x) => x.id === id)
  return (
    <div className="screen">
      <ChannelHeader id={id} topic={topic} onTopic={openTopic}
        left={<MenuButton />}
        right={<><button className="icon-btn" aria-label={`Search ${g?.name ?? id}`} onClick={() => go(`search/${encodeURIComponent(id)}`)}><SearchIcon /></button><FollowButton channel={id} /></>} />
      <Conversation id={id} events topic={topic} onTopic={openTopic} focus={focus} readOnly={!!g?.isPod && g.closed && !g.members.has(me())} />
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

export function Conversation({ id, events = false, readOnly = false, topic, onTopic, focus }: { id: string; events?: boolean; readOnly?: boolean; topic?: string; onTopic?: (t?: string) => void; focus?: string }) {
  const [newEvent, setNewEvent] = useState(false)
  const [newPod, setNewPod] = useState(false)
  const [newProp, setNewProp] = useState(false)
  const [newTodo, setNewTodo] = useState(false)
  useVersion()
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
  const openThread = (m: Message) => go(`c/${encodeURIComponent(id)}/thread/${m.ev.id}`)
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
            : <div className="empty-start">
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
                <ThreadActivity root={it.root} last={it.last} onOpen={() => toggle(key)} open={openThreads.has(key)} />
                {openThreads.has(key) && <InlineThread groupId={id} root={it.root} focus={replyTo === key} onHide={() => toggle(key)} onOpen={() => openThread(it.root)} onAct={(r) => { setPicking(false); setActingReply(r) }} />}
              </Fragment>
            }
            const m = it.m
            const prevItem = items[i - 1]
            const key = m.ev.id
            return <Fragment key={key}>
              <Row m={m} prev={prevItem?.t === 'msg' && !openThreads.has(prevItem.m.ev.id) ? prevItem.m : undefined} groupId={id}
                name={!ps.get(m.ev.pubkey)?.name && m.ev.tags.some((t) => t[0] === 'client' && t[1] === 'belfry-drop') ? 'Drop box' : displayName(m.ev.pubkey, ps)}
                reactions={rx.get(m.ev.id) ?? []} onAct={(emoji) => { setPicking(!!emoji); setActing(m) }} topic={topic} onTopic={onTopic} announce={isAnnouncement(m, g)}
                thread={th.get(m.ev.id)} onThread={() => toggle(key)} onOpenThread={() => openThread(m)} threadOpen={openThreads.has(key)} inbox={inbox} todo={todoOf.get(m.ev.id)} />
              {openThreads.has(key) && <InlineThread groupId={id} root={m} focus={replyTo === key} onHide={() => toggle(key)} onOpen={() => openThread(m)} onAct={(r) => { setPicking(false); setActingReply(r) }} />}
            </Fragment>
          })}
        </div>
      </div>
      <SaveAccountBar />
      {!readOnly && (topic !== ANNOUNCE || admin) && <Composer groupId={id} placeholder={topic && topic !== ANNOUNCE ? `Message #${topic}` : undefined} canAnnounce={admin} announcing={topic === ANNOUNCE}
        onSend={(text, files, announce) => { stick.current = true; return postMessage(id, text, files, topic === ANNOUNCE ? undefined : topic, announce) }}
        onEvent={events ? () => setNewEvent(true) : undefined} onPod={events ? () => setNewPod(true) : undefined} onProposal={events ? () => setNewProp(true) : undefined} onTodo={events ? () => setNewTodo(true) : undefined} />}
      {newTodo && <NewTodoSheet groupId={id} onClose={() => setNewTodo(false)} />}
      {newProp && <NewProposalSheet parent={id} onClose={() => setNewProp(false)} />}
      {newPod && <NewPod parent={id} onClose={() => setNewPod(false)} />}
      {acting && <MessageActions emojiOnly={picking} m={acting} groupId={id} mine={rx.get(acting.ev.id) ?? []} onClose={() => setActing(null)}
        thread={th.get(acting.ev.id)} onReply={() => { toggle(acting.ev.id, true); setReplyTo(acting.ev.id) }}
        onThread={th.get(acting.ev.id)?.count ? () => openThread(acting) : undefined} onEdit={() => setEditing(acting)} />}
      {actingReply && <MessageActions m={actingReply} groupId={id} mine={rx.get(actingReply.ev.id) ?? []} onClose={() => setActingReply(null)} onEdit={() => setEditing(actingReply)} />}
      {editing && <EditSheet m={editing} groupId={id} onClose={() => setEditing(null)} />}
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
  const [editing, setEditing] = useState<Message | null>(null)
  const [picking, setPicking] = useState(false)
  const scroller = useRef<HTMLDivElement>(null)
  useLayoutEffect(() => { const el = scroller.current; if (el) el.scrollTop = el.scrollHeight }, [replies.length])
  const nameOf = (m: Message) => displayName(m.ev.pubkey, ps)
  const back = () => up(`c/${encodeURIComponent(groupId)}`)
  const gkey = knownGroupsKey()
  const inbox = { states: inboxStates(groupId, gkey), key: gkey, canSend: canAdmin(g), name: g?.name ?? groupId }
  const card = root ? cardOf(root.ev, gkey) : null
  return (
    <div className="screen">
      <div className="topbar">
        <button className="icon-btn" aria-label={`Back to ${g?.name ?? groupId}`} onClick={back}>‹</button>
        <h1>Thread<small>{channelIcon(g)} {g?.name ?? groupId}</small></h1>
      </div>
      {card && <div className="thread-private">Team only. {card.name || card.from} never sees this thread.</div>}
      <div className="scroll" ref={scroller}>
        <div className="messages">
          {root ? <Row m={root} name={nameOf(root)} groupId={groupId} reactions={rx.get(root.ev.id) ?? []} onAct={(emoji) => { setPicking(!!emoji); setActing(root) }} announce={isAnnouncement(root, g)} inbox={inbox} onThread={() => {}} thread={{ count: replies.length, last: 0, who: [] }} />
            : <div className="empty">This message is not here (yet, or any more).</div>}
          <div className="thread-sep"><i />{replies.length ? `${replies.length} ${replies.length === 1 ? 'reply' : 'replies'}` : 'No replies yet'}<i /></div>
          {replies.map((m, i) => <Row key={m.ev.id} m={m} prev={replies[i - 1]} head={i === 0} name={nameOf(m)} groupId={groupId}
            reactions={rx.get(m.ev.id) ?? []} onAct={(emoji) => { setPicking(!!emoji); setActing(m) }} announce={false} />)}
        </div>
      </div>
      {root && <Composer groupId={groupId} placeholder="Reply in the thread" onSend={(text, files) => postThreadReply(groupId, root.ev, text, files)} />}
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

function Row({ m, prev, name, groupId, reactions: rx, onAct, topic, onTopic, announce, thread, onThread, onOpenThread, threadOpen = false, head, inbox, todo }: { todo?: Task; onOpenThread?: () => void; inbox?: { states: Map<string, CardState>; key?: string; canSend: boolean; name: string }; head?: boolean; m: Message; prev?: Message; name: string; groupId: string; reactions: Reaction[]; onAct: (emoji?: boolean) => void; topic?: string; onTopic?: (t?: string) => void; announce: boolean; thread?: ThreadInfo; onThread?: () => void; threadOpen?: boolean }) {
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
  const isCard = !!(eventCoordOf(m.ev) || podOf(m.ev) || proposalOf(m.ev)) // a card always says who made it
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
      <div id={'m-' + m.ev.id} className={'msg' + (first || announce || mail || isCard ? ' first' : '') + (p ? ' pending' : '') + (announce ? ' announce' : '')}
        {...hold}>
        {first || announce || mail || isCard ? <button className="face" onClick={() => openProfile(m.ev.pubkey)} aria-label={`${name}: profile`}><Avatar pubkey={m.ev.pubkey} /></button> : <div className="gutter" />}
        <div className="body">
          {(first || announce || mail || isCard) && <div className="who"><button className="who-name" onClick={() => openProfile(m.ev.pubkey)}>{name}</button><time>{fmtTime(m.ev.created_at)}</time>{m.edited && <span className="edited" title={`Edited ${fmtTime(m.edited)}`}>edited</span>}{mail && <ViaMail />}{announce && <span className="badge announce-badge">📣 Announcement</span>}</div>}
          {mail?.subject && <div className="mail-subject">{mail.subject}</div>}
          {(eventCoordOf(m.ev) || podOf(m.ev) || proposalOf(m.ev)) && <div className="created-line">Created {eventCoordOf(m.ev) ? 'an event' : podOf(m.ev) ? 'a pod' : 'a proposal'}</div>}
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
              {!p && <button className="react-add" aria-label="Add a reaction" title="Add a reaction" onClick={() => onAct(true)}><SmilePlusIcon /></button>}
            </div>
          )}
          {!first && !announce && !mail && !isCard && m.edited && <span className="edited below">edited</span>}
          {todo && (
            <button className={'todo-chip' + (todo.done ? ' done' : '')} onClick={() => void taskAction(groupId, todo.id, todo.done ? 'undone' : 'done')}
              aria-label={todo.done ? 'Done. Mark as not done' : 'To do. Mark as done'}>
              <TodoIcon />{todo.done ? 'Done' : todo.assignee ? `To do · ${displayName(todo.assignee)}` : 'To do'}
            </button>
          )}
          {thread && thread.count > 0 && onThread && (
            <div className="thread-chips">
              <button className={'thread-chip' + (threadOpen ? ' open' : '')} onClick={onThread} aria-expanded={threadOpen}>
                <span className="faces">{thread.who.slice(0, 3).map((pk) => <Avatar key={pk} pubkey={pk} size={18} />)}</span>
                {threadOpen ? 'Hide replies' : `${thread.count} ${thread.count === 1 ? 'reply' : 'replies'}`}<time>{fmtTime(thread.last)}</time>
              </button>
              {onOpenThread && <button className="thread-chip open-thread" onClick={onOpenThread}><ThreadIcon />Open thread</button>}
            </div>
          )}
          {p && p.status === 'failed' && (
            <div className="state failed">Not sent: {p.error} · <button className="link" onClick={() => retry(p.localId)}>Retry</button></div>
          )}
          {p && p.status === 'queued' && <div className="state">{p.tries ? 'Waiting for connection…' : 'Sending…'}</div>}
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

function MessageActions({ m, groupId, mine, onClose, thread, onThread, onReply, onEdit, emojiOnly = false }: { m: Message; groupId: string; mine: Reaction[]; onClose: () => void; thread?: ThreadInfo; onThread?: () => void; onReply?: () => void; onEdit?: () => void; emojiOnly?: boolean }) {
  // your own plain message or reply can be edited (not a card, an event, a pod, a proposal or an e-mail)
  const canEdit = !!onEdit && m.ev.pubkey === me() && !m.ev.pending && !!m.text && !eventCoordOf(m.ev) && !podOf(m.ev) && !proposalOf(m.ev) && !mailOf(m.ev) && !cardOf(m.ev, knownGroupsKey()) && !sentOf(m.ev, knownGroupsKey())
  // long press / right-click / "More": one line of emojis, then what you can do (Discord's menu).
  // The add-reaction button opens the emojis alone.
  const [more, setMore] = useState(emojiOnly)
  const [confirming, setConfirming] = useState(false) // "Delete message" asks first, in the same sheet
  const g = group(groupId)
  const isPinned = canAdmin(g) && pinned(groupId, mayPin(g)).some((x) => x.ev.id === m.ev.id)
  const isTodo = tasklist(groupId).tasks.some((t) => t.source === m.ev.id)
  const todoTitle = m.text.replace(/nostr:npub1[02-9ac-hj-np-z]+/g, '').replace(/\s+/g, ' ').trim().slice(0, 300)
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
        <span>{(m.text || (m.media.length ? (m.media.length === 1 ? 'A photo or file' : `${m.media.length} photos or files`) : '')).replace(/nostr:npub1[02-9ac-hj-np-z]+/g, '@…').slice(0, 240)}</span>
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
            {todoTitle && !isTodo && !m.ev.pending && <Item icon={<TodoIcon />} label={copied === 'todo' ? 'Added to the to-dos' : 'Turn into a to-do'} onClick={() => { if (copied === 'todo') return; setCopied('todo'); void addTask(groupId, todoTitle, m.ev.id); setTimeout(onClose, 700) }} />}
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

export function Composer({ onSend, onEvent, onPod, onProposal, onTodo, groupId, placeholder = 'Message', canAnnounce = false, announcing = false, autoFocus = false, compact = false }: { onSend: (text: string, files: File[], announce: boolean) => Promise<unknown>; onEvent?: () => void; onPod?: () => void; onProposal?: () => void; onTodo?: () => void; /** the channel written in: mentions of people who are not in it are pointed out */ groupId?: string; placeholder?: string; canAnnounce?: boolean; announcing?: boolean; autoFocus?: boolean; compact?: boolean }) {
  const [text, setText] = useState('')
  const [ann, setAnn] = useState(announcing)
  useEffect(() => setAnn(announcing), [announcing])
  const [menu, setMenu] = useState(false)
  const [files, setFiles] = useState<File[]>([])
  const [error, setError] = useState('')
  const ta = useRef<HTMLTextAreaElement>(null)
  const picker = useRef<HTMLInputElement>(null)
  useEffect(() => { if (autoFocus) ta.current?.focus({ preventScroll: false }) }, [autoFocus])
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
    picked.current.clear()
    return out
  }

  const send = async () => {
    if (!text.trim() && !files.length) return
    const mentioned = new Map(picked.current)
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
            : isGuest(pk) ? null
            : <Btn className="btn small" onClick={async () => {
                try { const s = await inviteMember(groupId!, pk); setInvited((m) => new Map(m).set(pk, s === 'asked' ? 'Link sent · they apply' : 'Invited')) }
                catch { setInvited((m) => new Map(m).set(pk, 'Could not invite. Try from the channel\'s Invite.')) }
              }}>Invite</Btn>}
        </div>
      ))}
      <div className="bar">
        <button className="icon-btn" aria-label={onEvent ? 'Add photos, files, an event, a proposal or a pod' : 'Add photos or files'}
          onClick={() => (onEvent || canAnnounce ? setMenu(true) : picker.current?.click())}>＋</button>
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
        <textarea ref={ta} rows={1} placeholder={canAnnounce && ann ? 'Announcement' : placeholder} value={text}
          onChange={(e) => { setText(e.target.value); setCaret(e.target.selectionStart ?? e.target.value.length) }}
          onSelect={(e) => setCaret((e.target as HTMLTextAreaElement).selectionStart ?? 0)}
          onKeyDown={(e) => {
            if (people.length && (e.key === 'Enter' || e.key === 'Tab')) { e.preventDefault(); pick(people[0]); return }
            // Enter sends on a keyboard; on phones the return key adds a line
            if (e.key === 'Enter' && !e.shiftKey && matchMedia('(pointer: fine)').matches) { e.preventDefault(); void send() }
          }} />
        <button className="send" aria-label="Send" disabled={!text.trim() && !files.length} onClick={() => void send()}>↑</button>
      </div>
      {menu && (onEvent || canAnnounce) && (
        <Sheet title="Add to the conversation" hideTitle onClose={() => setMenu(false)}>
          <div className="act-group">
            <button className="act" onClick={() => { setMenu(false); picker.current?.click() }}><ImageIcon /><span>Photos or files</span></button>
          </div>
          <div className="act-group">
            {canAnnounce && <button className="act" onClick={() => { setMenu(false); setAnn(true); ta.current?.focus() }}><MegaphoneIcon /><span>Announcement<small>Stands out; followers get it by e-mail</small></span></button>}
            {onTodo && <button className="act" onClick={() => { setMenu(false); onTodo() }}><TodoIcon /><span>To-do<small>Something to get done, on the channel's list</small></span></button>}
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
  const [title, setTitle] = useState('')
  return (
    <Sheet title="New to-do" onClose={onClose}>
      <Form onSubmit={async (e) => { e.preventDefault(); if (!title.trim()) return; await addTask(groupId, title.trim()); onClose() }}>
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
      <span><b>{displayName(j.pubkey)}</b> joined as {j.agent ? 'an agent' : 'a guest'}{j.by ? <>, invited by <b>{displayName(j.by)}</b></> : null}</span>
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
      <Composer groupId={groupId} placeholder={replies.length ? 'Reply in the thread' : 'Reply (starts a thread)'} autoFocus={focus} compact
        onSend={(text, files) => postThreadReply(groupId, root.ev, text, files)} />
      <div className="thread-foot">
        <button className="link thread-hide" onClick={onHide}>Hide</button>
        <button className="link thread-hide" onClick={onOpen}>Open thread</button>
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

/** An older thread, back in the feed where its latest reply came. */
function ThreadActivity({ root, last, open, onOpen }: { root: Message; last: Message; open: boolean; onOpen: () => void }) {
  const snip = (root.text || (root.media.length ? 'a photo' : '')).replace(/nostr:npub1[02-9ac-hj-np-z]+/g, '@…').slice(0, 80)
  return (
    <button className={'thread-activity' + (open ? ' open' : '')} onClick={onOpen} aria-expanded={open}>
      <span className="curve" aria-hidden="true" />
      <span className="t">
        <small>{displayName(last.ev.pubkey)} replied to {displayName(root.ev.pubkey)}: “{snip}{root.text.length > 80 ? '…' : ''}”</small>
        <span className="reply">{(last.text || 'a photo').replace(/nostr:npub1[02-9ac-hj-np-z]+/g, '@…').slice(0, 140)}</span>
      </span>
      <time>{fmtTime(last.ev.created_at)}</time>
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

/** A guest who joined with a name only, once they did something (a message, a reply, a
 *  reaction): a slim line above the composer to save the account with an e-mail and its code. */
function SaveAccountBar() {
  useVersion()
  const [g, setG] = useState(guestInfo())
  const [step, setStep] = useState<'line' | 'email' | 'code' | 'done'>('line')
  const [email, setEmail] = useState('')
  const [code, setCode] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [hidden, setHidden] = useState(() => { try { return sessionStorage.getItem('belfry-save-hidden') === '1' } catch { return false } })
  const acted = all(me()).some((e) => e.pubkey === me() && (e.kind === 9 || e.kind === 1111 || e.kind === 7)) || outboxItems().some((o) => [9, 1111, 7].includes(o.kind))
  if (!g || (g.saved && step !== 'done') || !acted || hidden) return null
  const post = async (path: string, body: unknown) => {
    setBusy(true); setError('')
    try {
      const r = await signedFetch(`${BELFRY.api}${path}`, 'POST', body)
      const d = await r.json().catch(() => ({}))
      if (!r.ok) throw new Error(d.message || 'That did not work. Try again.')
      return true
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); return false } finally { setBusy(false) }
  }
  if (step === 'done') return <div className="save-bar done" role="status">Saved. Sign in with {email} on any device.<button className="link" onClick={() => setG(null)}>OK</button></div>
  return (
    <div className="save-bar" role="region" aria-label="Save your account">
      {step === 'line' && <>
        <span>Save your account: add your e-mail to come back from any device.</span>
        <button className="btn small" onClick={() => setStep('email')}>Save</button>
        <button className="link close" aria-label="Not now" onClick={() => { setHidden(true); try { sessionStorage.setItem('belfry-save-hidden', '1') } catch { /* private mode */ } }}>✕</button>
      </>}
      {step === 'email' && <Form onSubmit={async (e) => { e.preventDefault(); if (await post('/guest/save', { email: email.trim() })) setStep('code') }}>
        <input className="input" type="email" inputMode="email" autoComplete="email" autoFocus placeholder="you@example.org" value={email} onChange={(e) => setEmail(e.target.value)} />
        <button className="btn small" disabled={busy || !email.includes('@')}>{busy ? '…' : 'Send code'}</button>
      </Form>}
      {step === 'code' && <Form onSubmit={async (e) => {
        e.preventDefault()
        if (await post('/guest/save/verify', { email: email.trim(), code: code.trim() })) { setGuestInfo({ ...g, saved: true }); setStep('done') }
      }}>
        <input className="input" inputMode="numeric" autoComplete="one-time-code" autoFocus placeholder="6-digit code" maxLength={6} value={code} onChange={(e) => setCode(e.target.value.replace(/\D/g, ''))} />
        <button className="btn small" disabled={busy || code.length !== 6}>{busy ? '…' : 'Confirm'}</button>
      </Form>}
      {error && <div className="error">{error}</div>}
    </div>
  )
}
