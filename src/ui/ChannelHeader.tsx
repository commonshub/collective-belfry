// A channel's header, pulled down like a sheet (a handle under it says so):
//   closed  the name and how many people were active in the last 30 days;
//   peek    the description, the avatars of those people, the latest photos, the number of files;
//   full    the whole screen: every member with their name, the files, settings for its admins.
// Drag the header or the handle down or up (it follows the finger), tap the name or the handle,
// or Esc. Opening a channel records that you saw it (markSeen), so the count says how many people
// a message will actually reach.
import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { markSeen } from '../core/actions'
import { channelIcon, group, messages, recentlyActive, type Group } from '../core/model'
import { useVersion } from '../core/store'
import { me } from '../core/sync'
import { Avatar, go } from './common'
import { ChannelInfoBody, type InfoTab } from './ChannelInfo'

type Level = 0 | 1 | 2

export function ChannelHeader({ id, left, right }: { id: string; left: ReactNode; right: ReactNode }) {
  useVersion()
  const g = group(id)
  const active = recentlyActive(id)
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
      if (screen && bar) setFullH(screen.clientHeight - bar.offsetHeight)
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

  const n = active.length
  return (
    <div ref={rootRef} className={'chead lvl' + level + (drag !== null ? ' dragging' : '')}>
      <div className="topbar chead-bar" {...pull}>
        {left}
        <h1 className="tap" onClick={() => setLevel(level ? 0 : 1)} aria-expanded={level > 0}>
          {channelIcon(g)} {g?.name ?? id}
          <small>{n ? `${n} active ${n === 1 ? 'member' : 'members'} this month` : 'Nobody active this month yet'}</small>
        </h1>
        {right}
      </div>
      <div className="chead-panel" style={{ height }} aria-hidden={height === 0}>
        {(level === 2 || (drag !== null && height > peekH + 8)) && g
          ? <div className="chead-full" style={{ height: fullH }}><ChannelInfoBody id={id} tab={tab} setTab={setTab} /></div>
          : <div ref={peekRef} className="chead-peek"><Peek g={g} id={id} active={active} open={(t) => { setTab(t); setLevel(2) }} /></div>}
      </div>
      <button className="chead-handle" {...pull} onClick={cycle} style={{ top: height ? `calc(100% + ${height - 22}px)` : 'calc(100% - 4px)' }}
        aria-label={level === 0 ? 'Show the channel details' : level === 1 ? 'Show everything about the channel' : 'Close the channel details'}>
        <span />
      </button>
    </div>
  )
}

function Peek({ g, id, active, open }: { g?: Group; id: string; active: { pubkey: string }[]; open: (t: InfoTab) => void }) {
  const media = messages(id).flatMap((m) => m.media).reverse()
  const photos = media.filter((m) => m.mime.startsWith('image/')).slice(0, 12)
  const files = media.length - media.filter((m) => m.mime.startsWith('image/')).length
  const parent = g?.parent ? group(g.parent) : undefined
  return (
    <div className="pad-x">
      {(g?.isPod || g?.isProposal) && parent && (
        <button className="link chead-parent" onClick={() => go('c/' + encodeURIComponent(parent.id))}>
          {g.isProposal ? 'Proposal' : 'Pod'} in {channelIcon(parent)} {parent.name}
        </button>
      )}
      <p className="chead-about">{g?.about || <span className="hint">No description yet.</span>}</p>
      {active.length > 0 && (
        <button className="chead-people" onClick={() => open('members')} aria-label={`${active.length} active this month: see the members`}>
          {active.slice(0, 14).map((a) => <Avatar key={a.pubkey} pubkey={a.pubkey} size={32} />)}
          {active.length > 14 && <span className="more">+{active.length - 14}</span>}
        </button>
      )}
      {(photos.length > 0 || files > 0) && (
        <div className="chead-media">
          {photos.map((m) => (
            <button key={m.url} className="thumb" onClick={() => open('files')} aria-label={m.name || 'Photo'}>
              <img src={m.url} alt="" loading="lazy" />
            </button>
          ))}
          {files > 0 && <button className="chip" onClick={() => open('files')}>{files} {files === 1 ? 'file' : 'files'}</button>}
        </div>
      )}
    </div>
  )
}
