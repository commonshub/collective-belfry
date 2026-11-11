// Under a channel's name there is exactly one line. It says the most useful thing about the
// channel right now, picked in this order:
//   1. what needs you: a vote you have not cast, an event you have not answered, people asking
//      to join a pod you administer;
//   2. a pinned message (its notice);
//   3. the next event;
//   4. an open proposal;
//   5. how many people were active this month.
// Everything else shows as small counts on the right (events, proposals, pods, to-dos). Pulling
// the line down (ChannelHeader) shows the same things in full: PullDown below.
import { useState, type ReactNode } from 'react'
import { addTask, taskAction } from '../core/actions'
import { canAdmin, belfryAdminKeys, accessInfo } from '../core/access'
import { ANNOUNCE, displayName, plainRefs, group, groups, pinned, proposalFor, recentlyActive, tasklist, isWorking, workingSince, taskTally, messages, type Task, type Group, type Message, type Proposal, type ProposalStatus } from '../core/model'
import { all, useVersion } from '../core/store'
import { me } from '../core/sync'
import { Btn, Form, fmtDay, go, useNow, WorkRing } from './common'
import { going, openEvent, stateLabel, upcoming, when } from './Calendar'
import { ProposalHeader } from './Proposals'
import { TopicsBar } from './Topics'
import { CalendarIcon, ChevronDownIcon, ChevronUpIcon, DocIcon, PeopleIcon, PushPinIcon as PinIcon, PodShape, TodoIcon } from './icons'
import { RichText } from './Topics'

const HIDDEN = 'belfry-pins-hidden'
const hiddenPins = (): string[] => { try { return JSON.parse(localStorage.getItem(HIDDEN) ?? '[]') } catch { return [] } }
const hidePin = (id: string) => { try { localStorage.setItem(HIDDEN, JSON.stringify([...new Set([...hiddenPins(), id])].slice(-200))) } catch { /* private mode */ } }

/** Who may pin in GROUP: its admins and the community's. */
export const mayPin = (g?: Group) => (pubkey: string) => !!g && (g.admins.has(pubkey) || belfryAdminKeys().includes(pubkey) || !!accessInfo()?.admins.includes(pubkey))

const OPEN_STATUS: ProposalStatus[] = ['draft', 'feedback', 'voting']
const plain = (m: Message) => plainRefs(m.text || (m.media.length ? 'A photo or file' : '')).replace(/[*_`>]/g, '').replace(/(^|\s)#(?=\s|$)/g, '$1').split('\n')[0]
const at = (e: { allDay: boolean; start: Date }) => (e.allDay ? '' : ' ' + e.start.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }))
const day = (d: Date) => (d.toDateString() === new Date().toDateString() ? 'Today' : fmtDay(d))

export function channelFacts(id: string) {
  const g = group(id)
  const subs = groups().filter((x) => x.parent === id)
  const pods = subs.filter((x) => x.isPod)
  const proposals = subs.filter((x) => x.isProposal).map((x) => proposalFor(x.id)).filter((p): p is Proposal => !!p && OPEN_STATUS.includes(p.status))
  const events = upcoming(id)
  const pins = pinned(id, mayPin(g))
  const allTasks = tasklist(id).tasks
  const todos = allTasks.filter((t) => !t.done)
  const tally = taskTally(allTasks)
  const active = recentlyActive(id)
  const own = g?.isProposal ? proposalFor(id) : undefined
  const needsVote = (p: Proposal) => p.status === 'voting' && !!p.poll && p.poll.version === p.version && !p.poll.votes.get(me())
  return { g, pods, proposals, events, pins, todos, allTasks, tally, active, own, needsVote }
}

/** The one line. `topic`: the feed is filtered on it, which the line says first of all. */
export function SummaryLine({ id, open, onToggle, topic, onTopic, pull }: { id: string; open: boolean; onToggle: () => void; topic?: string; onTopic: (t?: string) => void; pull?: object }) {
  useVersion()
  const [, redraw] = useState(0)
  const f = channelFacts(id)
  const g = f.g
  const hidden = hiddenPins()
  const pin = f.pins.find((m) => !hidden.includes(m.ev.id))
  const next = f.events[0]
  const unanswered = f.events.find((e) => !stateLabel(e) && e.status !== 'cancelled')
  const toVote = f.proposals.find(f.needsVote)
  const n = f.active.length
  const people = n ? `${n} ${n === 1 ? 'person' : 'people'} active this month` : 'Nobody active this month yet'

  let icon: ReactNode = <PeopleIcon />
  let text: ReactNode = people
  let pill: ReactNode = null
  let quiet = true
  let shown: 'event' | 'proposal' | 'pin' | '' = ''
  const say = (i: ReactNode, t: ReactNode, p: ReactNode = null, s: typeof shown = '') => { icon = i; text = t; pill = p; quiet = false; shown = s }
  const ask = (label: string, onClick: () => void) => <button className="line-pill ask" onClick={onClick}>{label}</button>
  const mineIs = (label: string) => <span className="line-pill">{label}</span>

  if (topic) {
    say(<span className="line-hash">#</span>, <>Showing <b>{topic === ANNOUNCE ? 'announcements' : '#' + topic}</b> only</>, ask('Show all', () => onTopic(undefined)))
  } else if (g?.isProposal) {
    const p = f.own
    if (p) {
      const votes = [...(p.poll?.votes.values() ?? [])]
      const count = (v: string) => votes.filter((x) => x === v).length
      const voting = p.status === 'voting' && !!p.poll && p.poll.version === p.version
      const mine = p.poll?.votes.get(me())
      say(<DocIcon />, voting ? <><b>Voting</b> · {count('yes')} yes · {count('no')} no</> : <><b>{p.status[0].toUpperCase() + p.status.slice(1)}</b> · version {p.version} · by {displayName(p.author)}</>,
        voting ? (mine ? mineIs(mine[0].toUpperCase() + mine.slice(1)) : ask('Vote', onToggle)) : null)
    }
  } else if (g?.isPod && canAdmin(g) && g.applications.length > 0) {
    const k = g.applications.length
    say(<PodShape members={g.members.size} size={18} />, <><b>{k} {k === 1 ? 'person asks' : 'people ask'} to join</b> · {g.members.size} {g.members.size === 1 ? 'member' : 'members'}</>, ask('Review', () => go(`c/${encodeURIComponent(id)}/info/members`)))
  } else if (toVote) {
    say(<DocIcon />, <b>{toVote.title}</b>, ask('Vote', () => go('c/' + encodeURIComponent(toVote.groupId))), 'proposal')
  } else if (unanswered && +unanswered.start - Date.now() < 7 * 86400e3) {
    say(<CalendarIcon />, <><b>{day(unanswered.start)}{at(unanswered)}</b> · {unanswered.title}</>, ask('RSVP', () => openEvent(unanswered)), 'event')
  } else if (pin) {
    say(<PinIcon />, plain(pin), null, 'pin')
  } else if (next) {
    const mine = stateLabel(next)
    say(<CalendarIcon />, <><b>{day(next.start)}{at(next)}</b> · {next.title}</>, mine ? mineIs(mine) : ask('RSVP', () => openEvent(next)), 'event')
  } else if (f.proposals[0]) {
    say(<DocIcon />, <><b>{f.proposals[0].title}</b> · {f.proposals[0].status}</>, null, 'proposal')
  } else if (g?.isPod) {
    say(<PodShape members={g.members.size} size={18} />, <>{g.members.size} {g.members.size === 1 ? 'member' : 'members'} · {g.closed ? 'by application' : 'open to join'}</>)
    quiet = true
  }
  void redraw
  const shownNow = shown as string

  const counts: [string, ReactNode, number, string][] = [
    ['events', <CalendarIcon />, f.events.length - (shownNow === 'event' ? 1 : 0), 'more events'],
    ['proposals', <DocIcon />, f.proposals.length - (shownNow === 'proposal' ? 1 : 0), 'more open proposals'],
    ['pods', <PodShape members={6} size={13} />, f.pods.length, 'pods'],
    ['todos', <TodoIcon />, f.tally.total, 'to-dos'],
  ]
  return (
    <div className={'chan-line' + (quiet ? ' quiet' : '') + (open ? ' open' : '')} {...pull}>
      <button className="line-main" onClick={onToggle} aria-expanded={open}>
        <span className="line-ico">{icon}</span>
        <span className="line-text">{text}</span>
      </button>
      {pill}
      <button className="line-more" onClick={onToggle} aria-label={open ? 'Close the channel summary' : 'Open the channel summary'}>
        {!topic && counts.filter((c) => c[2] > 0).map(([k, ico, num, label]) => k === 'todos'
          ? <span key={k} className="line-count" title={`${f.tally.done} of the latest ${f.tally.total} to-dos done`}>{ico}{f.tally.done}/{f.tally.total}</span>
          : <span key={k} className="line-count" title={`${num} ${label}`}>{ico}{(k === 'events' && shownNow === 'event') || (k === 'proposals' && shownNow === 'proposal') ? '+' : ''}{num}</span>)}
        <span className="line-chev">{open ? <ChevronUpIcon /> : <ChevronDownIcon />}</span>
      </button>
    </div>
  )
}

/** The line pulled down: everything it summarises, in the same order. */
export function PullDown({ id, topic, onTopic, more, full = false }: { id: string; topic?: string; onTopic: (t?: string) => void; more: (tab: 'about' | 'tasks' | 'members' | 'files') => void; /** the header pulled all the way: the same summary, showing more */ full?: boolean }) {
  useVersion()
  const now = useNow()
  const [, redraw] = useState(0)
  const f = channelFacts(id)
  const g = f.g
  if (!g) return null
  const parent = g.parent ? group(g.parent) : undefined
  const hidden = hiddenPins()
  const n = f.active.length
  return (
    <div className="pull">
      {(g.isPod || g.isProposal) && parent && (
        <button className="link pull-parent" onClick={() => go('c/' + encodeURIComponent(parent.id))}>{g.isProposal ? 'Proposal' : 'Pod'} in {parent.isPod ? '' : '# '}{parent.name}</button>
      )}
      {g.isProposal && <ProposalHeader groupId={id} />}
      {g.isPod && canAdmin(g) && g.applications.length > 0 && (
        <section>
          <h3>Asking to join <span>{g.applications.length}</span></h3>
          <button className="pull-row" onClick={() => go(`c/${encodeURIComponent(id)}/info/members`)}>
            <span className="t">{g.applications.slice(0, 3).map((a) => displayName(a.pubkey)).join(', ')}{g.applications.length > 3 ? '…' : ''}</span>
            <span className="line-pill ask">Review</span>
          </button>
        </section>
      )}
      {f.pins.length > 0 && (
        <section>
          <h3>Pinned</h3>
          {f.pins.map((m) => (
            <div key={m.ev.id} className="pull-pin">
              <PinIcon />
              <div>
                <RichText text={m.text} groupId={id} />
                <small>{displayName(m.ev.pubkey)} · {fmtDay(new Date(m.ev.created_at * 1000))} · <button className="link" onClick={() => go(`c/${encodeURIComponent(id)}/m/${m.ev.id}`)}>Show in the channel</button>
                  {!hidden.includes(m.ev.id) && <> · <button className="link" onClick={() => { hidePin(m.ev.id); redraw((x) => x + 1) }}>Got it, hide</button></>}</small>
              </div>
            </div>
          ))}
        </section>
      )}
      {f.events.length > 0 && (
        <section>
          <h3>Coming up</h3>
          {f.events.slice(0, 3).map((e) => {
            const mine = stateLabel(e)
            return (
              <button key={e.coord} className="pull-row" onClick={() => openEvent(e)}>
                <b className="when">{day(e.start)}<small>{when(e)}</small></b>
                <span className="t">{e.title}<small>{e.status === 'cancelled' ? 'Cancelled' : `${going(e)} going`}</small></span>
                <span className={'line-pill' + (mine ? '' : ' ask')}>{mine || 'RSVP'}</span>
              </button>
            )
          })}
          {f.events.length > 3 && <button className="link pull-all" onClick={() => go(`c/${encodeURIComponent(id)}/events`)}>All {f.events.length} events</button>}
        </section>
      )}
      {f.proposals.length > 0 && (
        <section>
          <h3>To decide</h3>
          {f.proposals.map((p) => (
            <button key={p.groupId} className="pull-row" onClick={() => go('c/' + encodeURIComponent(p.groupId))}>
              <span className="t">{p.title}<small>{p.status[0].toUpperCase() + p.status.slice(1)} · version {p.version}</small></span>
              {f.needsVote(p) ? <span className="line-pill ask">Vote</span> : p.poll?.votes.get(me()) && p.status === 'voting' ? <span className="line-pill">Voted</span> : null}
            </button>
          ))}
        </section>
      )}
      {f.allTasks.length > 0 && (
        <section className="pull-todos">
          <h3>To do <span>{f.tally.done}/{f.tally.total}</span></h3>
          {(full ? latestTasks(f.allTasks) : f.todos.slice(0, 4)).map((t) => { const busy = isWorking(t, now); return (
            <div key={t.id} className={'pull-row todo' + (t.done ? ' done' : '')} style={{ opacity: t.pending ? 0.6 : 1 }}>
              <button className={'check' + (t.done ? ' done' : t.assignee ? ' claimed' : '') + (busy ? ' working' : '')} aria-label={t.done ? `Reopen "${t.title}"` : `Mark "${t.title}" as done`}
                onClick={() => void taskAction(id, t.id, t.done ? 'undone' : 'done')}>{t.done ? '✓' : busy ? <WorkRing since={workingSince(t) ?? now} now={now} /> : ''}</button>
              <button className="t" onClick={() => { const th = t.source && threadOfMessage(id, t.source); if (th) go(`c/${encodeURIComponent(id)}/thread/${th}`); else if (!full) more('tasks') }}>
                <span className="tt">{t.title}</span><small>{t.done ? 'Done' : t.assignee ? (t.assignee === me() ? (busy ? `You are working on it · ${mins(now - (workingSince(t) ?? now))}` : 'You are on it') : busy ? `${displayName(t.assignee)} is working on it · ${mins(now - (workingSince(t) ?? now))}` : `${displayName(t.assignee)} is on it`) : 'Nobody on it yet'}{t.source ? ' · thread' : ''}</small>
              </button>
            </div>
          ) })}
          {!full && f.allTasks.length > Math.min(4, f.todos.length) && <button className="link pull-all" onClick={() => more('tasks')}>{f.todos.length ? 'All to-dos' : 'Nothing left to do · show the done ones'}</button>}
          {full && <NewTodo groupId={id} />}
        </section>
      )}
      {f.pods.length > 0 && (
        <section>
          <h3>Pods</h3>
          <div className="pull-pods">
            {f.pods.map((p) => (
              <button key={p.id} onClick={() => go('c/' + encodeURIComponent(p.id))}>
                <PodShape members={p.members.size} size={18} /><span>{p.name}</span><small>{p.members.size}</small>
              </button>
            ))}
          </div>
        </section>
      )}
      <section className="pull-topics">
        <TopicsBar groupId={id} active={topic} onOpen={onTopic} titled />
      </section>
      {!full && <button className="pull-row pull-people" onClick={() => more('members')}>
        <PeopleIcon />
        <span className="t">{n ? `${n} ${n === 1 ? 'person' : 'people'} active this month` : 'Nobody active this month yet'}<small>Members, files, settings</small></span>
        <ChevronDownIcon />
      </button>}
      {g.about && !full && <p className="pull-about">{g.about.split('\n')[0].slice(0, 200)}</p>}
    </div>
  )
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
  const r = all().find((e) => e.id === id)
  return r ? (r.kind === 1111 ? r.tags.find((t) => t[0] === 'E')?.[1] : r.id) : undefined
}

function NewTodo({ groupId }: { groupId: string }) {
  const [title, setTitle] = useState('')
  return (
    <Form className="row pull-newtodo" onSubmit={async (e) => { e.preventDefault(); if (title.trim()) { await addTask(groupId, title.trim()); setTitle('') } }}>
      <input className="input" placeholder="Add a to-do…" value={title} onChange={(e) => setTitle(e.target.value)} />
      <Btn className="btn small" disabled={!title.trim()}>Add</Btn>
    </Form>
  )
}

/** "4 min", "1 h 20" */
const mins = (secs: number) => { const m = Math.max(1, Math.round(secs / 60)); return m < 60 ? `${m} min` : `${Math.floor(m / 60)} h${m % 60 ? ' ' + String(m % 60).padStart(2, '0') : ''}` }
