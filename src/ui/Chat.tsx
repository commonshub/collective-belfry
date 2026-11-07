import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { createChannel, createPod, deleteMessage, joinGroup, postMessage, react, topEmojis } from '../core/actions'
import { channelIcon, displayName, eventCoordOf, groups, messages, podOf, profiles, reactions, type Group, type Message, type Reaction } from '../core/model'
import { useVersion } from '../core/store'
import { me, retry } from '../core/sync'
import { Avatar, fmtDay, fmtTime, go, MediaGrid, Sheet, SyncDot } from './common'
import { EventCard, EventsBar, NewEventSheet } from './Calendar'

// ---- channel list (the "server sidebar", full screen on a phone) ------------

export function ChannelList({ current }: { current?: string }) {
  useVersion()
  const [creating, setCreating] = useState(false)
  const all = groups()
  const chans = all.filter((g) => !g.isEvent && !g.isPod)
  const events = all.filter((g) => g.isEvent)
  const pods = all.filter((g) => g.isPod)
  const ids = new Set(all.map((g) => g.id))
  const last = (id: string) => {
    const m = messages(id).at(-1)
    return m ? `${displayName(m.ev.pubkey)}: ${m.text || (m.media.length ? '📷 photo' : '')}` : undefined
  }
  const Row = ({ g, depth = 0 }: { g: Group; depth?: number }) => (
    <button className={'chan' + (g.id === current ? ' on' : '')} style={depth ? { paddingLeft: 16 + depth * 18 } : undefined}
      onClick={() => go('c/' + encodeURIComponent(g.id))}>
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
        <h1 style={{ paddingLeft: 8 }}>Channels</h1>
        <button className="icon-btn" aria-label="New channel" onClick={() => setCreating(true)}>＋</button>
        <MeButton />
      </div>
      <div className="scroll">
        <button className="chan" onClick={() => go('cal')}>
          <span className="hash">📅</span>
          <span className="t"><b>All events</b><small>Every channel's events</small></span>
        </button>
        <div className="section-title">Channels and pods <span>{chans.length + pods.length}</span></div>
        {chans.map((g) => <Tree key={g.id} g={g} />)}
        {orphans.map((g) => <Tree key={g.id} g={g} />)}
        {chans.length === 0 && <div className="empty">No channels yet. They appear here once the village relay answers.</div>}
        {events.length > 0 && <div className="section-title">Event chats <span>{events.length}</span></div>}
        {events.map((g) => <Row key={g.id} g={g} />)}
        <div style={{ padding: 16 }}>
          <button className="btn ghost" onClick={() => setCreating(true)}>＋ Create a channel</button>
        </div>
      </div>
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

export function ChannelView({ id }: { id: string }) {
  useVersion()
  const all = groups()
  const g = all.find((x) => x.id === id)
  const parent = g?.isPod ? all.find((x) => x.id === g.parent) : undefined
  const where = `Pod in ${channelIcon(parent)}${parent?.isPod ? ' ' : ''}${parent?.name ?? g?.parent}`
  const sub = g?.isPod ? (g.about ? `${where} · ${g.about}` : where) : g?.about
  return (
    <div className="screen">
      <div className="topbar">
        <button className="icon-btn" aria-label="Channels" onClick={() => go('channels')}>☰</button>
        <h1 className="tap" onClick={() => go(`c/${encodeURIComponent(id)}/info`)}>
          {channelIcon(g)} {g?.name ?? id}
          {sub && <small>{sub}</small>}
        </h1>
        <MeButton />
      </div>
      <EventsBar channel={id} />
      <PodsBar channel={id} />
      <Conversation id={id} events />
      {g?.isPod && !g.members.has(me()) && (
        <div className="join-bar">
          <span>You're not in this pod yet.</span>
          <button className="btn small" onClick={() => void joinGroup(id)}>Join</button>
        </div>
      )}
    </div>
  )
}

/** Under a channel's name (after its events): the pods inside it. Nothing when none. */
function PodsBar({ channel }: { channel: string }) {
  const pods = groups().filter((x) => x.isPod && x.parent === channel)
  if (!pods.length) return null
  return (
    <div className="pods-bar">
      {pods.map((p) => (
        <button key={p.id} onClick={() => go('c/' + encodeURIComponent(p.id))}>⬡ {p.name}<small>{p.members.size}</small></button>
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
        <small>Pod · {g.members.size} {g.members.size === 1 ? 'member' : 'members'}{inIt ? ' · you’re in' : ''}</small>
      </span>
      {!inIt && <button className="btn small" onClick={(e) => { e.stopPropagation(); void joinGroup(id) }}>Join</button>}
    </div>
  )
}

function NewPod({ parent, onClose }: { parent: string; onClose: () => void }) {
  const [name, setName] = useState('')
  const [about, setAbout] = useState('')
  const where = groups().find((g) => g.id === parent)
  return (
    <Sheet title="New pod" onClose={onClose}>
      <form onSubmit={async (e) => {
        e.preventDefault()
        const id = await createPod(name.trim(), about.trim(), parent)
        onClose()
        go('c/' + encodeURIComponent(id))
      }}>
        <label className="field"><span>Name</span>
          <input className="input" autoFocus placeholder="e.g. Garden crew" value={name} onChange={(e) => setName(e.target.value)} /></label>
        <label className="field"><span>What is it working on?</span>
          <textarea className="input" rows={3} value={about} onChange={(e) => setAbout(e.target.value)} /></label>
        <button className="btn" disabled={!name.trim()}>Create pod</button>
        <p className="hint" style={{ marginTop: 10 }}>A working group inside {channelIcon(where)} {where?.name ?? parent}, with its own members, chat, events, photos, tasks and pods.</p>
      </form>
    </Sheet>
  )
}

/** Top right: your avatar, which opens Me. */
export function MeButton() {
  return (
    <button className="icon-btn me-btn" aria-label="Me" onClick={() => go('me')}>
      <Avatar pubkey={me()} size={30} />
      <SyncDot />
    </button>
  )
}

export function Conversation({ id, events = false }: { id: string; events?: boolean }) {
  const [newEvent, setNewEvent] = useState(false)
  const [newPod, setNewPod] = useState(false)
  useVersion()
  const list = messages(id)
  const ps = profiles()
  const rx = reactions(id)
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
          {list.length === 0 && <div className="empty">No messages yet. Say hi 👋</div>}
          {list.map((m, i) => <Row key={m.ev.id} m={m} prev={list[i - 1]} name={displayName(m.ev.pubkey, ps)} groupId={id}
            reactions={rx.get(m.ev.id) ?? []} onAct={() => setActing(m)} />)}
        </div>
      </div>
      <Composer onSend={(text, files) => { stick.current = true; return postMessage(id, text, files) }}
        onEvent={events ? () => setNewEvent(true) : undefined} onPod={events ? () => setNewPod(true) : undefined} />
      {newPod && <NewPod parent={id} onClose={() => setNewPod(false)} />}
      {acting && <MessageActions m={acting} groupId={id} mine={rx.get(acting.ev.id) ?? []} onClose={() => setActing(null)} />}
      {newEvent && <NewEventSheet channel={id} onClose={() => setNewEvent(false)} />}
    </>
  )
}

function Row({ m, prev, name, groupId, reactions: rx, onAct }: { m: Message; prev?: Message; name: string; groupId: string; reactions: Reaction[]; onAct: () => void }) {
  const d = new Date(m.ev.created_at * 1000)
  const newDay = !prev || new Date(prev.ev.created_at * 1000).toDateString() !== d.toDateString()
  const first = newDay || prev!.ev.pubkey !== m.ev.pubkey || m.ev.created_at - prev!.ev.created_at > 300
  const p = m.ev.pending
  // press and hold (phones) or right-click: react, or delete your own
  const hold = useRef<ReturnType<typeof setTimeout> | null>(null)
  const cancel = () => { if (hold.current) clearTimeout(hold.current); hold.current = null }
  return (
    <>
      {newDay && <div className="day">{fmtDay(d)}</div>}
      <div className={'msg' + (first ? ' first' : '') + (p ? ' pending' : '')}
        onContextMenu={(e) => { if (p) return; e.preventDefault(); cancel(); onAct() }}
        onPointerDown={(e) => {
          if (p || e.pointerType === 'mouse' || (e.target as HTMLElement).closest('button, a, video')) return
          hold.current = setTimeout(() => { hold.current = null; navigator.vibrate?.(10); onAct() }, 450)
        }}
        onPointerUp={cancel} onPointerMove={cancel} onPointerCancel={cancel}>
        {first ? <Avatar pubkey={m.ev.pubkey} /> : <div className="gutter" />}
        <div className="body">
          {first && <div className="who">{name}<time>{fmtTime(m.ev.created_at)}</time></div>}
          {eventCoordOf(m.ev) ? <EventCard coord={eventCoordOf(m.ev)!} fallback={m.text} /> : podOf(m.ev) ? <PodCard id={podOf(m.ev)!} fallback={m.text} /> : m.text && <div className="text">{m.text}</div>}
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

function MessageActions({ m, groupId, mine, onClose }: { m: Message; groupId: string; mine: Reaction[]; onClose: () => void }) {
  const [more, setMore] = useState(false)
  const pick = (emoji: string) => {
    void react(groupId, m.ev, emoji, mine.find((r) => r.emoji === emoji)?.mine)
    onClose()
  }
  const isOn = (emoji: string) => mine.some((r) => r.emoji === emoji && r.mine)
  return (
    <Sheet title="React" onClose={onClose}>
      <div className="emoji-row">
        {topEmojis(6).map((e) => <button key={e} className={isOn(e) ? 'on' : ''} onClick={() => pick(e)}>{e}</button>)}
        <button aria-label="More emojis" className={more ? 'on' : ''} onClick={() => setMore(!more)}>＋</button>
      </div>
      {more && (
        <div className="emoji-grid">
          {MORE_EMOJIS.map((e) => <button key={e} className={isOn(e) ? 'on' : ''} onClick={() => pick(e)}>{e}</button>)}
        </div>
      )}
      {m.ev.pubkey === me() && (
        <button className="btn ghost" style={{ marginTop: 12 }} onClick={() => { onClose(); if (confirm('Delete this message?')) void deleteMessage(groupId, m.ev.id) }}>Delete message</button>
      )}
    </Sheet>
  )
}

// ---- composer --------------------------------------------------------------------

export function Composer({ onSend, onEvent, onPod, placeholder = 'Message' }: { onSend: (text: string, files: File[]) => Promise<unknown>; onEvent?: () => void; onPod?: () => void; placeholder?: string }) {
  const [text, setText] = useState('')
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
    setText('')
    setFiles([])
    await onSend(t, f)
  }

  return (
    <div className="composer">
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
        <button className="icon-btn" aria-label={onEvent ? 'Add photos, files, an event or a pod' : 'Add photos or files'}
          onClick={() => (onEvent ? setMenu(true) : picker.current?.click())}>＋</button>
        <input ref={picker} type="file" multiple hidden
          onChange={(e) => { setFiles([...files, ...Array.from(e.target.files ?? [])]); e.target.value = '' }} />
        <textarea ref={ta} rows={1} placeholder={placeholder} value={text} onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            // Enter sends on a keyboard; on phones the return key adds a line
            if (e.key === 'Enter' && !e.shiftKey && matchMedia('(pointer: fine)').matches) { e.preventDefault(); void send() }
          }} />
        <button className="send" aria-label="Send" disabled={!text.trim() && !files.length} onClick={() => void send()}>↑</button>
      </div>
      {menu && onEvent && (
        <Sheet title="Add" onClose={() => setMenu(false)}>
          <button className="btn ghost" onClick={() => { setMenu(false); picker.current?.click() }}>📷 Photos or files</button>
          <div style={{ height: 8 }} />
          <button className="btn ghost" onClick={() => { setMenu(false); onEvent() }}>📅 An event</button>
          {onPod && <><div style={{ height: 8 }} /><button className="btn ghost" onClick={() => { setMenu(false); onPod() }}>⬡ A pod (working group)</button></>}
        </Sheet>
      )}
    </div>
  )
}
