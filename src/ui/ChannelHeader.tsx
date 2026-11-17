// A channel's header (the owner, 2026-11-17): the name, and under it one short subtitle (🔒/🌐 ·
// "6 active" · photos · groups: ChannelMenu.ChannelSubtitle). The line under the header only when
// something must catch the eye (ChannelLine.SummaryLine). Pulled down (drag the header or the
// handle, tap the name or the handle, Esc to close): the description and a menu whose rows open
// their own screens (ChannelMenu). Opening a channel records that you saw it (markSeen), so the
// count says how many people a message will actually reach.
import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { markSeen } from '../core/actions'
import { channelIcon, group, isInbox, isPublicChannel, mailLocal, publicPeriods, webPage, type Group } from '../core/model'
import { BELFRY } from '../config'
import { useVersion } from '../core/store'
import { me } from '../core/sync'
import { SummaryLine } from './ChannelLine'
import { ChannelMenu, ChannelSubtitle } from './ChannelMenu'
import { PodShape } from './icons'

export function ChannelHeader({ id, left, right, topic, onTopic, initialOpen = false }: {
  id: string; left: ReactNode; right: ReactNode; topic?: string; onTopic: (t?: string) => void
  /** an old link to the channel's info: open the menu */
  initialOpen?: boolean
}) {
  useVersion()
  const g = group(id)
  const [open, setOpen] = useState(initialOpen)
  const [drag, setDrag] = useState<number | null>(null) // px moved while dragging
  const menuRef = useRef<HTMLDivElement>(null)
  const rootRef = useRef<HTMLDivElement>(null)
  const [menuH, setMenuH] = useState(0)
  const [roomH, setRoomH] = useState(600)

  useEffect(() => { setOpen(initialOpen) }, [id, initialOpen])
  useEffect(() => {
    const ch = group(id)
    if (ch && !(ch.isPod && ch.closed && !ch.members.has(me()))) markSeen(id)
  }, [id])
  useEffect(() => {
    const k = (e: KeyboardEvent) => { if (e.key === 'Escape' && open) setOpen(false) }
    addEventListener('keydown', k)
    return () => removeEventListener('keydown', k)
  }, [open])
  // measure: the menu's own height, and the room under the top bar
  useLayoutEffect(() => {
    const m = () => {
      if (menuRef.current) setMenuH(menuRef.current.scrollHeight)
      const screen = rootRef.current?.closest('.screen') as HTMLElement | null
      if (screen && rootRef.current) setRoomH(screen.clientHeight - rootRef.current.offsetHeight)
    }
    m()
    addEventListener('resize', m)
    return () => removeEventListener('resize', m)
  })

  const full = Math.min(menuH, roomH)
  const base = open ? full : 0
  const height = drag === null ? base : Math.max(0, Math.min(full, base + drag))

  // pulling: pointer events on the header and the handle; vertical drags only (a tap stays a tap)
  const start = useRef<{ y: number; x: number; dragging: boolean; moved: boolean } | null>(null)
  const onDown = (e: React.PointerEvent) => {
    if (e.pointerType === 'mouse' && e.button !== 0) return
    start.current = { y: e.clientY, x: e.clientX, dragging: false, moved: false }
  }
  const onMove = (e: React.PointerEvent) => {
    const s = start.current
    if (!s) return
    const dy = e.clientY - s.y, dx = e.clientX - s.x
    if (!s.dragging) {
      if (Math.abs(dy) < 8 || Math.abs(dy) < Math.abs(dx)) return
      s.dragging = true
      ;(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId)
    }
    s.moved = true
    setDrag(dy)
  }
  const onUp = () => {
    const s = start.current
    start.current = null
    if (!s?.dragging || drag === null) { setDrag(null); return }
    const h = Math.max(0, Math.min(full, base + drag))
    setDrag(null)
    setOpen(h + (drag > 0 ? 40 : -40) > full / 2) // the nearest end, leaning the way the finger went
  }
  const swallowClick = (e: React.MouseEvent) => { if (start.current?.moved) { e.stopPropagation(); e.preventDefault() } }
  const pull = { onPointerDown: onDown, onPointerMove: onMove, onPointerUp: onUp, onPointerCancel: onUp, onClickCapture: swallowClick }
  const toggle = () => setOpen((o) => !o)

  return (
    <div ref={rootRef} className={'chead lvl' + (open ? 1 : 0) + (drag !== null ? ' dragging' : '')}>
      <div className="topbar chead-bar" {...pull}>
        {left}
        <h1 className="tap" onClick={toggle} aria-expanded={open}>
          {g?.isPod ? <PodShape members={g.members.size} size={18} /> : channelIcon(g)} {g?.name ?? id}
          {g && !g.isEvent && <ChannelSubtitle g={g} />}
        </h1>
        {right}
      </div>
      <SummaryLine id={id} open={open} onToggle={toggle} topic={topic} onTopic={onTopic} pull={pull} />
      <div className="chead-panel" style={{ height }} aria-hidden={height === 0}>
        <div className="chead-more-wrap" style={{ maxHeight: roomH }}>
          <div ref={menuRef} className="chead-peek"><ChannelMenu id={id} onPick={() => setOpen(false)} /></div>
        </div>
      </div>
      <button className="chead-handle" {...pull} onClick={toggle} style={{ top: height ? `calc(100% + ${height - 22}px)` : 'calc(100% - 4px)' }}
        aria-label={open ? 'Close the channel menu' : 'Open the channel menu'}>
        <span />
      </button>
    </div>
  )
}

const parentOf = (g: Group) => { const p = g.parent ? group(g.parent) : undefined; return p ? (p.isPod ? p.name : '# ' + p.name) : 'the community' }

/** Under a channel's name, always (#issues 2026-11-13, 2026-11-15): who reads it, and where people
 *  reach it from outside when that is on. Public: just the addresses (the globe is in the name
 *  already, and an address says what it is). */
export function visibilityLine(g: Group): string {
  const w = webPage(g)
  const mail = isInbox(g) ? `${mailLocal(g)}@${BELFRY.domain}` : ''
  const page = w?.on ? `${BELFRY.domain}/${w.slug}` : ''
  if (isPublicChannel(g)) return [page, mail].filter(Boolean).join(' · ')
  return 'Members only' + ([page, mail].some(Boolean) ? ' · ' + [page, mail].filter(Boolean).join(' · ') : '')
}

/** The pull-down's words on who reads what and who can write (#issues 2026-11-15): whether every
 *  message is public and where, or members only, and whether people from outside can post. */
export function visibilityText(g: Group): { title: string; lines: string[] } {
  const w = webPage(g)
  const page = w?.on ? `${BELFRY.domain}/${w.slug}` : ''
  const mail = `${mailLocal(g)}@${BELFRY.domain}`
  const since = publicPeriods(g).at(-1)?.[0]
  const lines: string[] = []
  if (isPublicChannel(g)) {
    lines.push(`Everything posted here${since ? ` since ${new Date(since * 1000).toLocaleDateString(undefined, { day: 'numeric', month: 'long', year: 'numeric' })}` : ''} is public: anyone can read it at ${page}.`)
    lines.push(w?.posts === 'proposal' || w?.posts === 'event' ? 'Members post here; people from outside read.' : `People from outside can open a thread there${isInbox(g) ? ` or write to ${mail}` : ''}; members answer here.`)
    return { title: '🌐 Public', lines }
  }
  lines.push('Only members read what is posted here.')
  if (page) lines.push(`People from outside can open a thread at ${page}: they see only their own.`)
  lines.push(isInbox(g) ? `Anyone can write to ${mail}: their mail lands here.` : `Members can write to ${mail}.`)
  if (publicPeriods(g).length) lines.push(`What was posted while it was public stays readable at ${page || BELFRY.domain}.`)
  return { title: '🔒 Members only', lines }
}
