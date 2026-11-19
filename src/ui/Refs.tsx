// A reference to a channel, group, event or proposal (core/refs.ts): its name inside the text
// (tappable; grey words when I can't open it), a preview box when it ends the message. Loading:
// a grey bar, then after ~5 s the kind's word (GroupName's shared rule).
import { useEffect, useState } from 'react'
import { useVersion } from '../core/store'
import { me } from '../core/sync'
import { channelIcon, group, proposalFor, STATUS_LABEL } from '../core/model'
import { deletedWords, kindWord, lockedWords, resolveRef, type Ref, type RefKind } from '../core/refs'
import { EventCard } from './Calendar'
import { go } from './common'
import { useCommunityName } from './community'
import { NAME } from '../config'

const START = Date.now()
const WAIT = 5000
function useWaited() {
  const [, tick] = useState(0)
  const waited = Date.now() - START > WAIT
  useEffect(() => {
    if (waited) return
    const t = setTimeout(() => tick((x) => x + 1), WAIT - (Date.now() - START) + 50)
    return () => clearTimeout(t)
  }, [waited])
  return waited
}

const mark = (k: RefKind) => (k === 'channel' || k === 'group' ? '#' : k === 'event' ? '📅 ' : '☑️ ')

/** Inside the text: the name, tappable. */
export function RefName({ r }: { r: Ref }) {
  useVersion()
  const waited = useWaited()
  const x = resolveRef(r, me())
  if (x.state === 'deleted') return <span className="ref-off">{deletedWords(x.kind)}</span>
  if (x.state === 'locked') return <span className="ref-off">{lockedWords(x.kind)}</span>
  if (x.state === 'loading' || (x.group?.unnamed && !x.event && !x.proposal)) {
    if (!waited) return <span className="name-skel ref-skel" role="img" aria-label="Loading" />
    return <span className="ref-off">{kindWord(x.kind)}</span>
  }
  const name = x.event?.title || x.proposal?.title || x.group!.name
  const icon = x.kind === 'channel' || x.kind === 'group' ? channelIcon(x.group) : mark(x.kind)
  return (
    <button type="button" className="mention channel-ref" onClick={(e) => { e.stopPropagation(); go(x.route!) }}>
      {icon === '#' || icon === '🌐' ? '#' : icon + (x.kind === 'group' ? ' ' : '')}{name}
    </button>
  )
}

const KIND_LABEL: Record<RefKind, string> = { channel: 'Channel', group: 'Group', event: 'Event', proposal: 'Proposal' }
const count = (n: number) => `${n} ${n === 1 ? 'member' : 'members'}`

/** At the end of a message: a box that opens it. */
export function RefPreview({ r }: { r: Ref }) {
  useVersion()
  const waited = useWaited()
  const community = useCommunityName(NAME)
  const x = resolveRef(r, me())
  if (x.kind === 'event' && x.event && x.state === 'ok') return <div className="ev-cards ref-pv-ev"><EventCard coord={x.event.coord} fallback="" /></div>
  if (x.state === 'locked' || x.state === 'deleted') {
    const what = x.state === 'deleted' ? deletedWords(x.kind) : lockedWords(x.kind)
    return (
      <div className="ref-pv off" role="note">
        <span className="ib" aria-hidden="true">🔒</span>
        <span className="rt"><span className="t1">{what[0].toUpperCase() + what.slice(1)}</span>
          {x.state === 'locked' && <span className="t2">{x.kind === 'channel' ? `Only members of ${community} can open it` : 'Only its members can open it'}</span>}</span>
      </div>
    )
  }
  if (x.state === 'loading' || x.group?.unnamed) {
    return (
      <div className="ref-pv" role="note" aria-label={waited ? kindWord(x.kind) : 'Loading'}>
        <span className="ib" aria-hidden="true">{mark(x.kind).trim()}</span>
        <span className="rt">{waited ? <span className="t1">{kindWord(x.kind)}</span> : <><span className="name-skel" /><span className="name-skel short" /></>}</span>
      </div>
    )
  }
  const g = x.group
  const p = x.proposal ?? (x.kind === 'proposal' && g ? proposalFor(g.id) : undefined)
  const name = x.event?.title || p?.title || g?.name || ''
  const parent = p && group(p.groupId)?.parent
  const sub = x.kind === 'proposal'
    ? ['Proposal' + (parent && group(parent) ? ` in #${group(parent)!.name}` : ''), p ? STATUS_LABEL[p.status] : ''].filter(Boolean).join(' · ')
    : [KIND_LABEL[x.kind], count(g!.members.size), g!.closed ? 'closed' : ''].filter(Boolean).join(' · ')
  const votes = p?.poll ? [...p.poll.votes.values()] : []
  const about = p?.summary || g?.about
  return (
    <button type="button" className="ref-pv" aria-label={`Open the ${x.kind} ${name}`} onClick={(e) => { e.stopPropagation(); go(x.route!) }}>
      <span className="ib" aria-hidden="true">{x.kind === 'proposal' ? '☑️' : channelIcon(g)}</span>
      <span className="rt">
        <span className="t1">{name}</span>
        <span className="t2">{sub}</span>
        {about && <span className="d">{about}</span>}
        {votes.length > 0 && <span className="votes">{['yes', 'no', 'abstain'].map((v) => { const n = votes.filter((x) => x === v).length; return n ? <span key={v}>{n} {v === 'yes' ? 'for' : v === 'no' ? 'against' : 'abstain'}</span> : null })}</span>}
      </span>
    </button>
  )
}
