// A channel's header (the owner, 2026-11-17): the name, and under it one short subtitle (🔒/🌐 ·
// "6 active" · photos · groups: ChannelMenu.ChannelSubtitle). The line under the header only when
// something must catch the eye (ChannelLine.SummaryLine). Pulled down (drag the header or the
// handle, tap the name or the handle, Esc to close): the description and a menu whose rows open
// their own screens (ChannelMenu). Opening a channel records that you saw it (markSeen), so the
// count says how many people a message will actually reach.
import { GroupName } from './GroupName'
import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { markSeen } from '../core/actions'
import { channelIcon, displayName, group, isInbox, isPublicChannel, mailLocal, publicPeriods, webPage, type Group, nameOf } from '../core/model'
import { BELFRY } from '../config'
import { dropOutbox, outboxItems, useVersion } from '../core/store'
import { me, retry } from '../core/sync'
import { Btn, go, Sheet } from './common'
import { SummaryLine } from './ChannelLine'
import { ChannelMenu, ChannelSubtitle } from './ChannelMenu'
import { Markdown } from './markdown'
import { ChevronDownIcon, ChevronUpIcon, LockIcon, PodShape } from './icons'
import { encOn, isPrivateGroup } from '../core/enc/runtime'

export function ChannelHeader({ id, left, right, topic, onTopic, initialOpen = false, initialMid = false, welcome }: {
  id: string; left: ReactNode; right: ReactNode; topic?: string; onTopic: (t?: string) => void
  /** an old link to the channel's info: open the menu */
  initialOpen?: boolean
  /** half pulled down (cards canvas, claim-done / join-visitor): the description, who looks after
   *  it and how many, ⌃ ⌄; the chat stays under it. Touching the chat or the composer folds it. */
  initialMid?: boolean
  /** the first open after a printed card: who is welcomed (cards canvas claim-done) */
  welcome?: 'started' | 'joined'
}) {
  useVersion()
  const g = group(id)
  const [open, setOpen] = useState(initialOpen)
  const [mid, setMid] = useState(initialMid && !initialOpen)
  const [drag, setDrag] = useState<number | null>(null) // px moved while dragging
  const menuRef = useRef<HTMLDivElement>(null)
  const rootRef = useRef<HTMLDivElement>(null)
  const [menuH, setMenuH] = useState(0)
  const [roomH, setRoomH] = useState(600)

  useEffect(() => { setOpen(initialOpen) }, [id, initialOpen])
  useEffect(() => { setMid(initialMid && !initialOpen) }, [id, initialMid, initialOpen])
  // mid folds up as soon as the person goes for the chat or the composer (outside this header)
  useEffect(() => {
    if (!mid) return
    const screen = rootRef.current?.closest('.screen')
    if (!screen) return
    const fold = (e: Event) => { if (!rootRef.current?.contains(e.target as Node)) setMid(false) }
    screen.addEventListener('pointerdown', fold)
    screen.addEventListener('focusin', fold)
    return () => { screen.removeEventListener('pointerdown', fold); screen.removeEventListener('focusin', fold) }
  }, [mid])
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
  const toggle = () => { setMid(false); setOpen((o) => !o) }

  return (
    <div ref={rootRef} className={'chead lvl' + (open ? 1 : 0) + (mid && !open ? ' mid' : '') + (drag !== null ? ' dragging' : '')}>
      <div className="topbar chead-bar" {...pull}>
        {left}
        <h1 className="tap" onClick={toggle} aria-expanded={open}>
          {/* a public channel's globe is in the subtitle: one globe, not two (#issues 2026-11-15) */}
          {g?.isPod ? <PodShape members={g.members.size} size={18} /> : g && !g.isEvent && !g.isProposal && isPublicChannel(g) ? '#' : channelIcon(g)} {g ? <GroupName g={g} /> : nameOf(id)}
          {g && !g.isEvent && <ChannelSubtitle g={g} />}
        </h1>
        {/* encrypted/group: a private group's lock opens Who can read this */}
        {isPrivateGroup(id) && encOn() && <button className="seal-lock" aria-label="End-to-end encrypted: see who can read it" title="End-to-end encrypted" onClick={() => go(`c/${encodeURIComponent(id)}/who`)}><LockIcon /></button>}
        <SaveFailedIcon id={id} />
        {right}
      </div>
      {mid && !open && g && <GroupMid g={g} welcome={welcome} onFold={() => setMid(false)} onMore={() => { setMid(false); setOpen(true) }} />}
      <SummaryLine id={id} open={open} onToggle={toggle} topic={topic} onTopic={onTopic} pull={pull} />
      <div className="chead-panel" style={{ height }} aria-hidden={height === 0}>
        <div className="chead-more-wrap" style={{ maxHeight: roomH }}>
          <div ref={menuRef} className="chead-peek"><ChannelMenu id={id} onPick={() => setOpen(false)} /></div>
        </div>
      </div>
      {!(mid && !open) && <button className="chead-handle" {...pull} onClick={toggle} style={{ top: height ? `calc(100% + ${height - 22}px)` : 'calc(100% - 4px)' }}
        aria-label={open ? 'Close the channel menu' : 'Open the channel menu'}>
        <span />
      </button>}
    </div>
  )
}

/** "Steward: Lou · 8 people" (a group) or "Started by Dev · 23 following" (a channel): one line. */
export function midFacts(g: Group, mine = me()): string {
  const keeper = [...g.admins][0]
  const who = keeper ? (keeper === mine ? 'you' : displayName(keeper)) : ''
  const n = g.isPod ? g.members.size : g.reading.size || g.members.size
  const count = g.isPod ? (n === 1 ? '1 person' : `${n} people`) : `${n} following`
  return [who && (g.isPod ? 'Steward: ' : 'Started by ') + who, count].filter(Boolean).join(' · ')
}

/** The mid part: two lines of description (More opens the whole menu), the facts, ⌃ folds, ⌄ opens. */
function GroupMid({ g, welcome, onFold, onMore }: { g: Group; welcome?: 'started' | 'joined'; onFold: () => void; onMore: () => void }) {
  // More only when the two-line clamp really cut the description (Xavier, Cards v73)
  const about = useRef<HTMLDivElement>(null)
  const [cut, setCut] = useState(false)
  useLayoutEffect(() => {
    const el = about.current?.firstElementChild as HTMLElement | null
    if (!el) { setCut(false); return }
    const measure = () => setCut(el.scrollHeight > el.clientHeight + 1)
    measure()
    const ro = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(measure) : null
    ro?.observe(el)
    return () => ro?.disconnect()
  }, [g.about])
  return (
    <div className="gmid">
      {g.about && <div ref={about}><Markdown className="gmid-about" text={g.about} /></div>}
      {g.about && cut && <button className="link gmid-more" onClick={onMore}>More</button>}
      <div className="gmid-facts">{midFacts(g)}</div>
      {welcome && <div className="gmid-welcome" role="status"><b>Welcome to {g.isPod ? g.name : '# ' + g.name}!</b> {g.isPod
        ? (welcome === 'started' ? 'You started it. Say hi and share something to get it going: a photo, a tip, a question.' : 'Say hi and share something: a photo, a tip, a question.')
        : (welcome === 'started' ? 'You started it. Share something to get it going: a link, a photo, a question.' : 'No introductions needed: share what you know.')}</div>}
      <div className="gmid-handle">
        <button className="icon-btn" aria-label="Fold up" onClick={onFold}><ChevronUpIcon /></button>
        <span className="gmid-bar" aria-hidden="true" />
        <button className="icon-btn" aria-label="Open more" onClick={onMore}><ChevronDownIcon /></button>
      </div>
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

/** The channel's settings that the relay or the belfry refused for good (a 9002 in the outbox,
 *  failed): a mark next to its name; tapped, what failed, Try again or Discard (the relay's
 *  version comes back). */
export function SaveFailedIcon({ id }: { id: string }) {
  useVersion()
  const [open, setOpen] = useState(false)
  const failed = outboxItems().filter((o) => o.status === 'failed' && o.kind === 9002 && o.tags.some((t) => t[0] === 'h' && t[1] === id))
  if (!failed.length) return null
  const why = (failed[failed.length - 1].error ?? '').replace(/^(error|blocked|invalid|restricted):\s*/, '').trim()
  return (
    // (the header around it is dragged and tapped: nothing of this reaches it, the sheet neither)
    <span className="save-failed-wrap" onPointerDown={(e) => e.stopPropagation()} onClick={(e) => e.stopPropagation()}>
      <button type="button" className="icon-btn save-failed" aria-label="The channel's settings weren't saved" onClick={() => setOpen(true)}>!</button>
      {open && (
        <Sheet title="Not saved" onClose={() => setOpen(false)}>
          <p className="save-failed-why">The new settings of this channel weren't saved{why ? `: ${why.replace(/[.!]$/, '')}` : ''}.</p>
          <div className="row-btns">
            <Btn className="btn ghost" onClick={async () => { for (const o of failed) await dropOutbox(o.localId); setOpen(false) }}>Discard</Btn>
            <Btn className="btn" onClick={async () => { for (const o of failed) await retry(o.localId); setOpen(false) }}>Try again</Btn>
          </div>
        </Sheet>
      )}
    </span>
  )
}
