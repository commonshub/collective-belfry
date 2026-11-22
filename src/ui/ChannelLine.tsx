// What the channel header knows (channelFacts), and the one line under it, which shows only when
// something must catch the eye (SummaryLine). The pulled-down menu is in ChannelMenu.tsx.
import { TodoField, todoOver } from './TodoField'
import { isPictureMedia } from '../core/pictures'
import { useState, type ReactNode } from 'react'
import { addTask, taskAction } from '../core/actions'
import { canAdmin, belfryAdminKeys, accessInfo } from '../core/access'
import { todoParts, todoPartsText, todoTotalText, ANNOUNCE, displayName, plainRefs, group, groups, pinned, proposalFor, tasklist, isWorking, workingSince, taskTally, messages, type Task, type Group, type Media, type Message, type Proposal, type ProposalStatus , hiddenFromMe } from '../core/model'
import { all, touch, useVersion } from '../core/store'
import { me } from '../core/sync'
import { Avatar, Btn, Form, fmtDay, go, Lightbox, useMediaUrl, useNow, WorkRing } from './common'
import { openProfile } from './Profile'
import { going, openEvent, stateLabel, upcoming, when } from './Calendar'
import { TopicsBar } from './Topics'
import { CalendarIcon, ChevronDownIcon, ChevronRightIcon, ChevronUpIcon, DocIcon, ImageIcon, PeopleIcon, PushPinIcon as PinIcon, PodShape, TodoIcon } from './icons'
import { RichText } from './Topics'
import { Markdown } from './markdown'
import { visibilityText } from './ChannelHeader'

const HIDDEN = 'belfry-pins-hidden'
export const hiddenPins = (): string[] => { try { return JSON.parse(localStorage.getItem(HIDDEN) ?? '[]') } catch { return [] } }
export const hidePin = (id: string) => { try { localStorage.setItem(HIDDEN, JSON.stringify([...new Set([...hiddenPins(), id])].slice(-200))) } catch { /* private mode */ } touch() }

/** Who may pin in GROUP: its admins and the community's. */
export const mayPin = (g?: Group) => (pubkey: string) => !!g && (g.admins.has(pubkey) || belfryAdminKeys().includes(pubkey) || !!accessInfo()?.admins.includes(pubkey))

const OPEN_STATUS: ProposalStatus[] = ['draft', 'feedback', 'voting']
const plain = (m: Message) => plainRefs(m.text || (m.media.length ? 'A photo or file' : '')).replace(/[*_`>]/g, '').replace(/(^|\s)#(?=\s|$)/g, '$1').split('\n')[0]
const at = (e: { allDay: boolean; start: Date }) => (e.allDay ? '' : ' ' + e.start.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }))
const day = (d: Date) => (d.toDateString() === new Date().toDateString() ? 'Today' : fmtDay(d))

export function channelFacts(id: string) {
  const g = group(id)
  const subs = groups().filter((x) => x.parent === id)
  const pods = subs.filter((x) => x.isPod && !hiddenFromMe(x))
  const proposals = subs.filter((x) => x.isProposal).map((x) => proposalFor(x.id)).filter((p): p is Proposal => !!p && OPEN_STATUS.includes(p.status))
  const events = upcoming(id)
  const pins = pinned(id, mayPin(g))
  const allTasks = tasklist(id).tasks
  const todos = allTasks.filter((t) => !t.done)
  const tally = taskTally(allTasks)
  const members = g ? [...g.members] : []
  const media = [...new Map(messages(id).flatMap((m) => m.media).map((x) => [x.url, x])).values()].reverse()
  const photos = media.filter(isPictureMedia)
  const files = media.length - photos.length - media.filter((m) => m.mime.startsWith('video/')).length
  const own = g?.isProposal ? proposalFor(id) : undefined
  const needsVote = (p: Proposal) => p.status === 'voting' && !!p.poll && p.poll.version === p.version && !p.poll.votes.get(me())
  return { g, pods, proposals, events, pins, todos, allTasks, tally, members, photos, files, own, needsVote }
}

/** The one line. `topic`: the feed is filtered on it, which the line says first of all. */
/** The line under the header, only when something must catch the eye (the owner, 2026-11-17):
 *  a topic filter in force (with Show all), a vote I haven't cast, people asking to join a group I
 *  run, an event I haven't answered within 7 days, a pinned message I haven't hidden, to-dos on
 *  me, to-dos nobody took. Otherwise nothing: the counts live in the subtitle (ChannelMenu.tsx). */
export function SummaryLine({ id, open, onToggle, topic, onTopic, pull }: { id: string; open: boolean; onToggle: () => void; topic?: string; onTopic: (t?: string) => void; pull?: object }) {
  useVersion()
  const f = channelFacts(id)
  const g = f.g
  const hidden = hiddenPins()
  const pin = f.pins.find((m) => !hidden.includes(m.ev.id))
  const unanswered = f.events.find((e) => !stateLabel(e) && e.status !== 'cancelled')
  const toVote = f.proposals.find(f.needsVote)
  const parts = todoParts(f.allTasks)

  let icon: ReactNode = null
  let text: ReactNode = null
  let pill: ReactNode = null
  const say = (i: ReactNode, t: ReactNode, p: ReactNode = null) => { icon = i; text = t; pill = p }
  const ask = (label: string, onClick: () => void) => <button className="line-pill ask" onClick={onClick}>{label}</button>

  if (topic) {
    say(<span className="line-hash">#</span>, <>Showing <b>{topic === ANNOUNCE ? 'announcements' : '#' + topic}</b> only</>, ask('Show all', () => onTopic(undefined)))
  } else if (g?.isProposal && f.own && f.needsVote(f.own)) {
    say(<DocIcon />, <><b>Voting</b> · version {f.own.version}</>, ask('Vote', onToggle))
  } else if (g?.isPod && canAdmin(g) && g.applications.length > 0) {
    const k = g.applications.length
    say(<PodShape members={g.members.size} size={18} />, <b>{k} {k === 1 ? 'person asks' : 'people ask'} to join</b>, ask('Review', () => go(`c/${encodeURIComponent(id)}/members`)))
  } else if (toVote) {
    say(<DocIcon />, <b>{toVote.title}</b>, ask('Vote', () => go('c/' + encodeURIComponent(toVote.groupId))))
  } else if (unanswered && +unanswered.start - Date.now() < 7 * 86400e3) {
    say(<CalendarIcon />, <><b>{day(unanswered.start)}{at(unanswered)}</b> · {unanswered.title}</>, ask('RSVP', () => openEvent(unanswered)))
  } else if (pin) {
    say(<PinIcon />, plain(pin))
  }
  // the channel's to-dos in parts (board channels/header-todos): under any line above (a vote first)
  const todoLine = !topic && parts.total > 0 && (
    <button className="chan-line todo-line" onClick={() => go(`c/${encodeURIComponent(id)}/todos`)}
      aria-label={`Open the to-dos: ${todoTotalText(parts)}, ${todoPartsText(parts)}`}>
      <span className="line-ico"><TodoIcon /></span>
      <span className="line-text"><b>{todoTotalText(parts)}</b><span className="todo-parts">{todoPartsText(parts)}</span></span>
      <span className="line-chev" aria-hidden="true"><ChevronRightIcon /></span>
    </button>
  )
  if (!text) return todoLine || null
  return (
    <>
    <div className={'chan-line' + (open ? ' open' : '')} {...pull}>
      <button className="line-main" onClick={onToggle} aria-expanded={open}>
        <span className="line-ico">{icon}</span>
        <span className="line-text">{text}</span>
      </button>
      {pill}
      <button className="line-more" onClick={onToggle} aria-label={open ? 'Close the channel menu' : 'Open the channel menu'}>
        <span className="line-chev">{open ? <ChevronUpIcon /> : <ChevronDownIcon />}</span>
      </button>
    </div>
    {todoLine}
    </>
  )
}

/** The latest photos: one row in the small pull-down, a mosaic when it is pulled all the way. */
function Photos({ photos, full, all }: { photos: Media[]; full: boolean; all: () => void }) {
  const [open, setOpen] = useState<number | null>(null)
  const shown = photos.slice(0, full ? 12 : 24) // the row scrolls sideways
  return (
    <section className="pull-photos">
      <h3>Latest photos <span>{photos.length}</span></h3>
      <div className={full ? 'photo-mosaic' : 'photo-row'}>
        {shown.map((m, i) => <Thumb key={m.url} m={m} onOpen={() => setOpen(i)} />)}
      </div>
      {photos.length > shown.length && <button className="link pull-all" onClick={all}>All {photos.length} photos</button>}
      {open !== null && <Lightbox images={photos} start={open} onClose={() => setOpen(null)} />}
    </section>
  )
}

function Thumb({ m, onOpen }: { m: Media; onOpen: () => void }) {
  const src = useMediaUrl(m.url)
  return <button className="thumb" onClick={onOpen} aria-label={m.name || 'Photo'}>{src ? <img src={src} alt="" loading="lazy" /> : null}</button>
}

/** The latest 20 to-dos: the open ones first, then the done ones; newest first in each. */
const latestTasks = (tasks: Task[]) => {
  const recent = [...tasks].sort((a, b) => b.at - a.at).slice(0, 20)
  return [...recent.filter((t) => !t.done), ...recent.filter((t) => t.done)]
}

/** The thread a message belongs to: itself, or the root its reply hangs off. */
function threadOfMessage(groupId: string, id: string): string | undefined {
  const m = messages(groupId).find((x) => x.ev.id === id)
  if (m) return m.ev.kind === 1111 ? (m.ev.tags.find((t) => t[0] === 'E')?.[1] ?? id) : id
  const r = all(me()).find((e) => e.id === id)
  return r ? (r.kind === 1111 ? r.tags.find((t) => t[0] === 'E')?.[1] : r.id) : undefined
}

function NewTodo({ groupId }: { groupId: string }) {
  const [title, setTitle] = useState('')
  return (
    <Form className="row pull-newtodo todo-add" onSubmit={async (e) => { e.preventDefault(); if (title.trim() && !todoOver(title)) { await addTask(groupId, title.trim()); setTitle('') } }}>
      <TodoField label="Add a to-do" placeholder="Add a to-do…" value={title} onChange={setTitle} />
      <Btn className="btn small" disabled={!title.trim() || todoOver(title) > 0}>Add</Btn>
    </Form>
  )
}

/** "4 min", "1 h 20" */
const mins = (secs: number) => { const m = Math.max(1, Math.round(secs / 60)); return m < 60 ? `${m} min` : `${Math.floor(m / 60)} h${m % 60 ? ' ' + String(m % 60).padStart(2, '0') : ''}` }
