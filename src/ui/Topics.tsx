import { useRef, useState, type ReactNode } from 'react'
import { setTopics } from '../core/actions'
import { ANNOUNCE, group, HASHTAG, isAnnouncement, messages, normTopic, topics } from '../core/model'
import { useVersion } from '../core/store'
import { Sheet } from './common'

// Topics: a channel's pinned hashtags. Hold a #hashtag (or right-click it) to pin it; tap a
// topic to open it: the chat shows its messages and what you send carries it (a `t` tag),
// without writing it in the text.

/** Press-and-hold on touch, right-click on a mouse. */
export function useHold(onHold: () => void, ms = 450) {
  const t = useRef<ReturnType<typeof setTimeout> | null>(null)
  const held = useRef(false)
  const cancel = () => { if (t.current) clearTimeout(t.current); t.current = null }
  return {
    held,
    handlers: {
      onPointerDown: (e: React.PointerEvent) => {
        e.stopPropagation()
        held.current = false
        if (e.pointerType === 'mouse') return
        t.current = setTimeout(() => { t.current = null; held.current = true; navigator.vibrate?.(10); onHold() }, ms)
      },
      onPointerUp: cancel, onPointerMove: cancel, onPointerCancel: cancel,
      onContextMenu: (e: React.MouseEvent) => { e.preventDefault(); e.stopPropagation(); cancel(); held.current = true; onHold() },
    },
  }
}

export function TopicMenu({ groupId, topic, onOpen, onClose }: { groupId: string; topic: string; onOpen?: (t: string) => void; onClose: () => void }) {
  const pinned = topics(groupId)
  const isPinned = pinned.includes(topic)
  return (
    <Sheet title={'#' + topic} onClose={onClose}>
      {onOpen && <><button className="btn" onClick={() => { onClose(); onOpen(topic) }}>Open #{topic}</button><div style={{ height: 8 }} /></>}
      <button className="btn ghost" onClick={() => { onClose(); void setTopics(groupId, isPinned ? pinned.filter((x) => x !== topic) : [...pinned, topic]) }}>
        {isPinned ? `Unpin #${topic}` : `Pin #${topic} to this channel`}
      </button>
      <p className="hint" style={{ marginTop: 10 }}>Pinned topics show under the channel’s name, for everyone.</p>
    </Sheet>
  )
}

function Tag({ name, groupId, onOpen }: { name: string; groupId: string; onOpen?: (t: string) => void }) {
  const [menu, setMenu] = useState(false)
  const t = normTopic(name)
  const hold = useHold(() => setMenu(true))
  return (
    <>
      <span className="hashtag" role="button" tabIndex={0} {...hold.handlers}
        onClick={(e) => { e.stopPropagation(); if (hold.held.current) return; if (onOpen) onOpen(t); else setMenu(true) }}>#{name}</span>
      {menu && <TopicMenu groupId={groupId} topic={t} onOpen={onOpen} onClose={() => setMenu(false)} />}
    </>
  )
}

/** Message text with its #hashtags tappable (open) and holdable (pin). */
export function RichText({ text, groupId, onOpen }: { text: string; groupId: string; onOpen?: (t: string) => void }) {
  const out: ReactNode[] = []
  let last = 0
  for (const m of text.matchAll(HASHTAG)) {
    const at = m.index! + m[1].length
    if (at > last) out.push(text.slice(last, at))
    out.push(<Tag key={at} name={m[2]} groupId={groupId} onOpen={onOpen} />)
    last = at + 1 + m[2].length
  }
  if (last < text.length) out.push(text.slice(last))
  return <div className="text">{out}</div>
}

/** The pinned topics, a scrolling line; the open one highlighted (shown even if not pinned). */
export function TopicsBar({ groupId, active, onOpen }: { groupId: string; active?: string; onOpen: (t?: string) => void }) {
  useVersion()
  const list = topics(groupId).filter((t) => t !== ANNOUNCE)
  const all = active && active !== ANNOUNCE && !list.includes(active) ? [active, ...list] : list
  const g = group(groupId)
  const announced = active === ANNOUNCE || messages(groupId).some((m) => isAnnouncement(m, g))
  const [menu, setMenu] = useState<string | null>(null)
  if (!all.length && !announced) return null
  return (
    <div className="topics-bar">
      {announced && <button className={active === ANNOUNCE ? 'on' : ''} onClick={() => onOpen(active === ANNOUNCE ? undefined : ANNOUNCE)}>📣 Announcements{active === ANNOUNCE && <span aria-hidden="true"> ✕</span>}</button>}
      {all.map((t) => <TopicChip key={t} t={t} on={t === active} onTap={() => onOpen(t === active ? undefined : t)} onHold={() => setMenu(t)} />)}
      {menu && <TopicMenu groupId={groupId} topic={menu} onClose={() => setMenu(null)} />}
    </div>
  )
}

function TopicChip({ t, on, onTap, onHold }: { t: string; on: boolean; onTap: () => void; onHold: () => void }) {
  const hold = useHold(onHold)
  return (
    <button className={on ? 'on' : ''} {...hold.handlers} onClick={() => { if (!hold.held.current) onTap() }}>
      #{t}{on && <span aria-hidden="true"> ✕</span>}
    </button>
  )
}
