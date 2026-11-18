// A group's name in a list row or a header (the lead's shared rule for web, iOS and Android,
// 2026-11-18): while its name hasn't arrived, a grey bar where the name will be (aria-label
// "Loading"), never any part of its id; if it is still unknown ~5 s after start while online, the
// kind's word ("Channel", "Group", "Event", "Proposal"). Inside sentences and titles the model's
// neutral word is used instead ("this channel", model.ts).
import { useEffect, useState } from 'react'
import type { Group } from '../core/model'

const START = Date.now()
const WAIT = 5000
const KIND = (g: Group) => (g.isEvent ? 'Event' : g.isProposal ? 'Proposal' : g.isPod ? 'Group' : 'Channel')

export function GroupName({ g }: { g: Group }) {
  const [, tick] = useState(0)
  const unknown = !!g.unnamed
  useEffect(() => {
    if (!unknown) return
    const left = WAIT - (Date.now() - START)
    const t = setTimeout(() => tick((x) => x + 1), Math.max(0, left) + 50)
    const on = () => tick((x) => x + 1)
    addEventListener('online', on)
    return () => { clearTimeout(t); removeEventListener('online', on) }
  }, [unknown])
  if (!unknown) return <>{g.name}</>
  if (Date.now() - START > WAIT && navigator.onLine) return <>{KIND(g)}</>
  return <span className="name-skel" role="img" aria-label="Loading" />
}
