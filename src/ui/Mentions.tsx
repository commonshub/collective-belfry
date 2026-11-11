import { nip19 } from 'nostr-tools'
import { displayName, group, plainRefs } from '../core/model'
import { all, useVersion, type Ev } from '../core/store'
import { me } from '../core/sync'
import { Avatar, fmtDay, fmtTime, go, up } from './common'
import { closeDrawer } from './drawer'

// #/mentions (Xavier, 2026-11-11): the latest messages that name you, from every channel; a tap
// opens the message where it is (the channel at that message, or its thread).
const SEEN = 'osv-mentions-seen'
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

function seenAt(): number { try { return Number(localStorage.getItem(SEEN) || 0) } catch { return 0 } }
/** How many mentions came since I last opened the list (the badge in the channel list). */
export const newMentions = () => { const at = seenAt(); return mentions(99).filter((e) => e.created_at > at).length }

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
  const at = seenAt()
  // opening the list marks them seen (next time)
  try { localStorage.setItem(SEEN, String(Math.floor(Date.now() / 1000))) } catch { /* private mode */ }
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
            <button key={e.id} className={'mention' + (e.created_at > at ? ' new' : '')} onClick={() => { closeDrawer(); go(where(e)) }}>
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
