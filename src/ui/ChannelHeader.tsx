// A channel's header, pulled down like a sheet (a handle under it says so):
//   closed  the name and how many people were active in the last 30 days;
//   peek    the description, the avatars of those people, the latest photos, the number of files;
//   full    the whole screen: every member with their name, the files, settings for its admins.
// Drag the header or the handle down or up (it follows the finger), tap the name or the handle,
// or Esc. Opening a channel records that you saw it (markSeen), so the count says how many people
// a message will actually reach.
import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { markSeen } from '../core/actions'
import { channelIcon, group, type Group } from '../core/model'
import { useVersion } from '../core/store'
import { me } from '../core/sync'
import { ChannelInfoBody, type InfoTab } from './ChannelInfo'
import { PullDown, SummaryLine } from './ChannelLine'
import { PodShape } from './icons'

type Level = 0 | 1 | 2

export function ChannelHeader({ id, left, right, topic, onTopic, title, line, peek }: {
  id: string; left: ReactNode; right: ReactNode; topic?: string; onTopic: (t?: string) => void
  /** Another header's own title, line and peek (an event's: Events.tsx); the pull works the same. */
  title?: ReactNode
  line?: (p: { open: boolean; onToggle: () => void; pull: object }) => ReactNode
  peek?: (p: { close: () => void; more: (t: InfoTab) => void }) => ReactNode
}) {
  useVersion()
  const g = group(id)
  const [level, setLevel] = useState<Level>(0)
  const [tab, setTab] = useState<InfoTab>('about')
  const [drag, setDrag] = useState<number | null>(null) // px moved while dragging
  const peekRef = useRef<HTMLDivElement>(null)
  const rootRef = useRef<HTMLDivElement>(null)
  const [peekH, setPeekH] = useState(0)
  const [fullH, setFullH] = useState(600)

  useEffect(() => { setLevel(0); setTab('about') }, [id])
  useEffect(() => {
    const ch = group(id)
    if (ch && !(ch.isPod && ch.closed && !ch.members.has(me()))) markSeen(id)
  }, [id])
  useEffect(() => {
    const k = (e: KeyboardEvent) => { if (e.key === 'Escape' && level) setLevel(0) }
    addEventListener('keydown', k)
    return () => removeEventListener('keydown', k)
  }, [level])
  // measure: the peek's own height, and the room under the top bar
  useLayoutEffect(() => {
    const m = () => {
      if (peekRef.current) setPeekH(peekRef.current.scrollHeight)
      const screen = rootRef.current?.closest('.screen') as HTMLElement | null
      const bar = rootRef.current?.querySelector('.topbar') as HTMLElement | null
      if (screen && bar && rootRef.current) setFullH(screen.clientHeight - rootRef.current.offsetHeight)
    }
    m()
    addEventListener('resize', m)
    return () => removeEventListener('resize', m)
  })

  const base = level === 0 ? 0 : level === 1 ? peekH : fullH
  const height = drag === null ? base : Math.max(0, Math.min(fullH, base + drag))

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
    const h = Math.max(0, Math.min(fullH, base + drag))
    // settle on the nearest level, leaning the way the finger went
    const stops: [Level, number][] = [[0, 0], [1, peekH], [2, fullH]]
    const lean = drag > 0 ? 40 : -40
    const next = stops.reduce((best, cur) => (Math.abs(cur[1] - (h + lean)) < Math.abs(best[1] - (h + lean)) ? cur : best))[0]
    setDrag(null)
    setLevel(next)
  }
  const swallowClick = (e: React.MouseEvent) => { if (start.current?.moved) { e.stopPropagation(); e.preventDefault() } }
  const pull = { onPointerDown: onDown, onPointerMove: onMove, onPointerUp: onUp, onPointerCancel: onUp, onClickCapture: swallowClick }
  const cycle = () => setLevel((l) => (l === 0 ? 1 : l === 1 ? 2 : 0))
  const fullRef = useRef<HTMLDivElement>(null)
  const moreRef = useRef<HTMLDivElement>(null)
  // "All to-dos", "Members…": the full level, scrolled to that part (to-dos are in the summary itself)
  const openMore = (t: InfoTab) => {
    setTab(t); setLevel(2)
    requestAnimationFrame(() => requestAnimationFrame(() => {
      const box = fullRef.current
      const at = t === 'tasks' ? box?.querySelector('.pull-todos') : moreRef.current
      if (box && at) box.scrollTo({ top: (at as HTMLElement).offsetTop - 8, behavior: 'smooth' })
    }))
  }

  return (
    <div ref={rootRef} className={'chead lvl' + level + (drag !== null ? ' dragging' : '')}>
      <div className="topbar chead-bar" {...pull}>
        {left}
        <h1 className="tap" onClick={() => setLevel(level ? 0 : 1)} aria-expanded={level > 0}>
          {title ?? <>{g?.isPod ? <PodShape members={g.members.size} size={18} /> : channelIcon(g)} {g?.name ?? id}
          {(g?.isPod || g?.isProposal) && <small>{g.isProposal ? 'Proposal' : 'Pod'} in {parentOf(g)}{g.isPod ? (g.closed ? ' · by application' : ' · open') : ''}</small>}</>}
        </h1>
        {right}
      </div>
      {line ? line({ open: level > 0, onToggle: () => setLevel(level ? 0 : 1), pull }) : <SummaryLine id={id} open={level > 0} onToggle={() => setLevel(level ? 0 : 1)} topic={topic} onTopic={onTopic} pull={pull} />}
      <div className="chead-panel" style={{ height }} aria-hidden={height === 0}>
        {/* one layout at every level: the full one is the same summary, scrollable, showing more
            (every to-do, then members, files and settings under it), so nothing jumps */}
        <div ref={fullRef} className={level === 2 ? 'chead-more-wrap' : undefined} style={level === 2 ? { height: fullH } : undefined}>
          <div ref={peekRef} className="chead-peek">{peek ? peek({ close: () => setLevel(0), more: openMore }) : <PullDown id={id} topic={topic} full={level === 2} onTopic={(t) => { setLevel(0); onTopic(t) }} more={openMore} />}</div>
          {level === 2 && g && <div ref={moreRef} className="chead-more"><ChannelInfoBody id={id} tab={tab === 'tasks' ? 'about' : tab} setTab={setTab} tabs={[['about', 'About'], ['members', 'Members'], ['files', 'Files']]} /></div>}
        </div>
      </div>
      <button className="chead-handle" {...pull} onClick={cycle} style={{ top: height ? `calc(100% + ${height - 22}px)` : 'calc(100% - 4px)' }}
        aria-label={level === 0 ? 'Show the channel details' : level === 1 ? 'Show everything about the channel' : 'Close the channel details'}>
        <span />
      </button>
    </div>
  )
}

const parentOf = (g: Group) => { const p = g.parent ? group(g.parent) : undefined; return p ? (p.isPod ? p.name : '# ' + p.name) : 'the community' }
