import { useEffect, useState } from 'react'
import { all } from '../core/store'
import { me } from '../core/sync'
import { thread, type Conv } from '../core/dm'
import { displayName } from '../core/model'
import { onTyping, typingIn, watchGroup, type Place } from '../core/typing'
import { MESSAGE_KINDS } from '../core/typingcore'

// Typing indicators (canvas v110 messages/typing-*; docs/typing.md): one line under the last
// message, above the composer, 22 px, secondary colour, names semibold, three dots in turn. The
// line keeps its height when nobody types, so the feed never moves.

/** When each person's latest message in PLACE was written (seconds): a message ends their dots. */
function lastMsgIn(place: Place): (pk: string) => number {
  if ('peers' in place) {
    const conv: Conv = 'n:' + [...place.peers].sort().join(',')
    return (pk) => thread(conv).reduce((m, d) => (d.from === pk && d.at > m ? d.at : m), 0)
  }
  return (pk) => {
    let m = 0
    for (const e of all(me())) {
      if (e.pubkey !== pk || e.created_at <= m || !MESSAGE_KINDS.includes(e.kind) || !e.tags.some((t) => t[0] === 'h' && t[1] === place.h)) continue
      const root = e.tags.find((t) => t[0] === 'E')?.[1]
      if ((root ?? undefined) === place.root) m = e.created_at
    }
    return m
  }
}

/** Who types in PLACE (pubkeys), kept current; a group place is watched while shown. */
export function useTyping(place: Place | null): string[] {
  const [, tick] = useState(0)
  const h = place && 'h' in place ? place.h : ''
  useEffect(() => (h ? watchGroup(h) : undefined), [h])
  useEffect(() => onTyping(() => tick((n) => n + 1)), [])
  return place ? typingIn(place, lastMsgIn(place)) : []
}

export function TypingDots() {
  return <span className="typing-dots" aria-hidden="true"><i /><i /><i /></span>
}

/** "Lina Moreau is typing…" and so on (messages/typing-texts), names semibold. */
export function TypingText({ who }: { who: string[] }) {
  const n = who.map((pk) => displayName(pk))
  if (n.length === 1) return <span><b>{n[0]}</b> is typing…</span>
  if (n.length === 2) return <span><b>{n[0]}</b> and <b>{n[1]}</b> are typing…</span>
  return <span>Several people are typing…</span>
}

/** The line above a composer. */
export function TypingLine({ place }: { place: Place | null }) {
  const who = useTyping(place)
  return (
    <div className="typing-line" role="status" aria-live="polite">
      {who.length > 0 && <><TypingDots /><TypingText who={who} /></>}
    </div>
  )
}
