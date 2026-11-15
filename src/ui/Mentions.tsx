import { useEffect, useRef } from 'react'
import { nip19 } from 'nostr-tools'
import { displayName, group, plainRefs } from '../core/model'
import { all, useVersion, type Ev } from '../core/store'
import { me } from '../core/sync'
import { markRead, readUpTo } from '../core/readstate'
import { Avatar, fmtDay, fmtTime, go, up } from './common'
import { closeDrawer } from './drawer'

// #/mentions (Xavier, 2026-11-11): the latest messages that name you, from every channel; a tap
// opens the message where it is (the channel at that message, or its thread).
const tag = (e: Ev, k: string) => e.tags.find((t) => t[0] === k)?.[1]

/** Messages and thread replies that tag me (p) or name my npub, newest first. */
export function mentions(limit = 100): Ev[] {
  const mine = me()
  if (!mine) return []
  let npub = ''
  try { npub = 'nostr:' + nip19.npubEncode(mine) } catch { /* not a key yet */ }
  const seen = new Set<string>()
  return all(mine)
    .filter((e) => (e.kind === 9 || e.kind === 1111) && e.pubkey !== mine && !!tag(e, 'h') && !seen.has(e.id) && seen.add(e.id) &&
      (e.tags.some((t) => t[0] === 'p' && t[1] === mine) || (!!npub && (e.content || '').includes(npub))))
    .sort((a, b) => b.created_at - a.created_at)
    .slice(0, limit)
}

/** A mention is new until I saw the list after it, or read its channel (a message) or its thread
 *  (a reply) past it: readstate.ts, the same on all my devices (SPEC 19.27). */
export function isNew(e: Ev, seen = readUpTo('m')): boolean {
  const where = e.kind === 1111 ? readUpTo('t', tag(e, 'E') || tag(e, 'e') || '') : readUpTo('c', tag(e, 'h') || '')
  return e.created_at > Math.max(seen, where)
}
/** How many mentions are new (the badge in the channel list). */
export const newMentions = () => mentions(99).filter(isNew).length

/** Text for a one-line preview: nostr:npub… as @Name, channel addresses as #name. */
export function preview(text: string): string {
  return plainRefs(text.replace(/nostr:(npub1[02-9ac-hj-np-z]+)/g, (all, npub: string) => {
    try { const d = nip19.decode(npub); return d.type === 'npub' ? '@' + displayName(d.data as string) : all } catch { return all }
  })).replace(/\*\*/g, '').replace(/\s+/g, ' ').trim()
}

function where(e: Ev): string {
  const h = tag(e, 'h')!
  if (e.kind === 1111) return `c/${encodeURIComponent(h)}/thread/${tag(e, 'E') || tag(e, 'e')}`
  return `c/${encodeURIComponent(h)}/m/${e.id}`
}

export function MentionsScreen() {
  useVersion()
  const list = mentions()
  const seenBefore = useRef(readUpTo('m')) // as it was when the list opened
  const fresh = new Set(list.filter((e) => isNew(e, seenBefore.current)).map((e) => e.id))
  // on screen: seen up to the newest (they stay marked new until next time)
  const newest = list[0]?.created_at ?? 0
  useEffect(() => { if (newest) markRead('m', '', newest) }, [newest])
  return (
    <div className="screen">
      <div className="topbar">
        <button className="icon-btn" aria-label="Back" onClick={() => up('c')}>‹</button>
        <h1>@Mentions<small>Where people named you</small></h1>
      </div>
      <div className="scroll mentions-list">
        {list.length === 0 && <div className="empty">No mentions yet. When someone writes @you in a channel, it shows here.</div>}
        {list.map((e) => {
          const g = group(tag(e, 'h')!)
          const d = new Date(e.created_at * 1000)
          return (
            <button key={e.id} className={'mention' + (fresh.has(e.id) ? ' new' : '')} onClick={() => { closeDrawer(); go(where(e)) }}>
              <Avatar pubkey={e.pubkey} size={36} />
              <span className="t">
                <span className="h"><b>{displayName(e.pubkey)}</b> in {g?.name ?? tag(e, 'h')}{e.kind === 1111 ? ' · thread' : ''}</span>
                <span className="x">{preview(e.content || '') || 'a photo'}</span>
              </span>
              <time dateTime={d.toISOString()}>{d.toDateString() === new Date().toDateString() ? fmtTime(e.created_at) : fmtDay(d)}</time>
            </button>
          )
        })}
      </div>
    </div>
  )
}
