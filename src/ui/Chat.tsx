import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { createChannel, deleteMessage, postMessage } from '../core/actions'
import { displayName, groups, messages, profiles, type Message } from '../core/model'
import { useVersion } from '../core/store'
import { me, retry } from '../core/sync'
import { Avatar, fmtDay, fmtTime, go, MediaGrid, Sheet, StatusPill } from './common'

// ---- channel list (the "server sidebar", full screen on a phone) ------------

export function ChannelList({ current }: { current?: string }) {
  useVersion()
  const [creating, setCreating] = useState(false)
  const all = groups()
  const chans = all.filter((g) => !g.isEvent)
  const events = all.filter((g) => g.isEvent)
  const last = (id: string) => {
    const m = messages(id).at(-1)
    return m ? `${displayName(m.ev.pubkey)}: ${m.text || (m.media.length ? '📷 photo' : '')}` : undefined
  }
  const Row = ({ id, name, about, ev }: { id: string; name: string; about: string; ev?: boolean }) => (
    <button className={'chan' + (id === current ? ' on' : '')} onClick={() => go('c/' + encodeURIComponent(id))}>
      <span className="hash">{ev ? '📅' : '#'}</span>
      <span className="t"><b>{name}</b><small>{last(id) ?? about}</small></span>
    </button>
  )
  return (
    <div className="screen">
      <div className="topbar">
        <h1 style={{ paddingLeft: 8 }}>Channels</h1>
        <StatusPill />
        <button className="icon-btn" aria-label="New channel" onClick={() => setCreating(true)}>＋</button>
      </div>
      <div className="scroll">
        <div className="section-title">Channels <span>{chans.length}</span></div>
        {chans.map((g) => <Row key={g.id} id={g.id} name={g.name} about={g.about} />)}
        {chans.length === 0 && <div className="empty">No channels yet. They appear here once the village relay answers.</div>}
        {events.length > 0 && <div className="section-title">Event chats <span>{events.length}</span></div>}
        {events.map((g) => <Row key={g.id} id={g.id} name={g.name} about={g.about} ev />)}
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
  const g = groups().find((x) => x.id === id)
  return (
    <div className="screen">
      <div className="topbar">
        <button className="icon-btn" aria-label="Channels" onClick={() => go('channels')}>☰</button>
        <h1 onClick={() => go(`c/${encodeURIComponent(id)}/info`)}>
          {g?.isEvent ? '📅 ' : '# '}{g?.name ?? id}
          {g?.about && <small>{g.about}</small>}
        </h1>
        <StatusPill />
        <button className="icon-btn" aria-label="Channel info, tasks and members" onClick={() => go(`c/${encodeURIComponent(id)}/info`)}>ⓘ</button>
      </div>
      <Conversation id={id} />
    </div>
  )
}

export function Conversation({ id }: { id: string }) {
  useVersion()
  const list = messages(id)
  const ps = profiles()
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
          {list.map((m, i) => <Row key={m.ev.id} m={m} prev={list[i - 1]} name={displayName(m.ev.pubkey, ps)} groupId={id} />)}
        </div>
      </div>
      <Composer onSend={(text, files) => { stick.current = true; return postMessage(id, text, files) }} />
    </>
  )
}

function Row({ m, prev, name, groupId }: { m: Message; prev?: Message; name: string; groupId: string }) {
  const d = new Date(m.ev.created_at * 1000)
  const newDay = !prev || new Date(prev.ev.created_at * 1000).toDateString() !== d.toDateString()
  const first = newDay || prev!.ev.pubkey !== m.ev.pubkey || m.ev.created_at - prev!.ev.created_at > 300
  const p = m.ev.pending
  const mine = m.ev.pubkey === me()
  return (
    <>
      {newDay && <div className="day">{fmtDay(d)}</div>}
      <div className={'msg' + (first ? ' first' : '') + (p ? ' pending' : '')}
        onContextMenu={(e) => {
          if (!mine || p) return
          e.preventDefault()
          if (confirm('Delete this message?')) void deleteMessage(groupId, m.ev.id)
        }}>
        {first ? <Avatar pubkey={m.ev.pubkey} /> : <div className="gutter" />}
        <div className="body">
          {first && <div className="who">{name}<time>{fmtTime(m.ev.created_at)}</time></div>}
          {m.text && <div className="text">{m.text}</div>}
          <MediaGrid media={m.media} />
          {p && p.status === 'failed' && (
            <div className="state failed">Not sent: {p.error} · <button className="link" onClick={() => retry(p.localId)}>Retry</button></div>
          )}
          {p && p.status === 'queued' && <div className="state">{p.tries ? 'Waiting for connection…' : 'Sending…'}</div>}
        </div>
      </div>
    </>
  )
}

// ---- composer --------------------------------------------------------------------

export function Composer({ onSend, placeholder = 'Message' }: { onSend: (text: string, files: File[]) => Promise<unknown>; placeholder?: string }) {
  const [text, setText] = useState('')
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
        <button className="icon-btn" aria-label="Add photos or files" onClick={() => picker.current?.click()}>＋</button>
        <input ref={picker} type="file" multiple hidden
          onChange={(e) => { setFiles([...files, ...Array.from(e.target.files ?? [])]); e.target.value = '' }} />
        <textarea ref={ta} rows={1} placeholder={placeholder} value={text} onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            // Enter sends on a keyboard; on phones the return key adds a line
            if (e.key === 'Enter' && !e.shiftKey && matchMedia('(pointer: fine)').matches) { e.preventDefault(); void send() }
          }} />
        <button className="send" aria-label="Send" disabled={!text.trim() && !files.length} onClick={() => void send()}>↑</button>
      </div>
    </div>
  )
}
