import { useSyncExternalStore } from 'react'
// Unread counts, from my read state (readstate.ts, SPEC 19.27): a channel's top-level messages
// (kind 9) by others after its mark; a thread's replies (kind 1111) by others after its mark, when
// I am in it (I wrote the message or a reply, or opened it before). Direct messages: dm.ts.
import { deleted, rootOf } from './model'
import { ownMark, readUpTo } from './readstate'
import { all, type Ev } from './store'
import { me } from './sync'

const tag = (e: Ev, k: string) => e.tags.find((t) => t[0] === k)?.[1]

interface Feed { others: number[]; newest: number }
interface Thread { others: number[]; newest: number; mine: boolean }
let cache: { list: Ev[] | null; chans: Map<string, Feed>; threads: Map<string, Thread> } = { list: null, chans: new Map(), threads: new Map() }

function index() {
  const mine = me()
  const list = all(mine)
  if (cache.list === list) return cache
  const del = deleted(list)
  const chans = new Map<string, Feed>()
  const threads = new Map<string, Thread>()
  const authors = new Map<string, string>()
  for (const e of list) {
    if (del.has(e.id)) continue
    if (e.kind === 9) {
      const h = tag(e, 'h')
      if (!h) continue
      authors.set(e.id, e.pubkey)
      const f = chans.get(h) ?? chans.set(h, { others: [], newest: 0 }).get(h)!
      f.newest = Math.max(f.newest, e.created_at)
      if (e.pubkey !== mine) f.others.push(e.created_at)
    } else if (e.kind === 1111) {
      const root = rootOf(e)
      if (!root || !tag(e, 'h')) continue
      const t = threads.get(root) ?? threads.set(root, { others: [], newest: 0, mine: false }).get(root)!
      t.newest = Math.max(t.newest, e.created_at)
      if (e.pubkey === mine) t.mine = true
      else t.others.push(e.created_at)
    }
  }
  for (const [root, t] of threads) if (authors.get(root) === mine) t.mine = true
  cache = { list, chans, threads }
  return cache
}

/** How many messages in this channel came after what I read. */
export function channelUnread(groupId: string): number {
  const f = index().chans.get(groupId)
  if (!f) return 0
  const at = readUpTo('c', groupId)
  return f.others.reduce((n, x) => n + (x > at ? 1 : 0), 0)
}

/** The time of the channel's newest message (what "read" goes up to when it is on screen). */
export const channelNewest = (groupId: string) => index().chans.get(groupId)?.newest ?? 0

/** New replies in a thread I am in (0 when I am not in it). */
export function threadUnread(rootId: string): number {
  const t = index().threads.get(rootId)
  if (!t) return 0
  const at = readUpTo('t', rootId)
  const opened = ownMark('t', rootId) > 0 && ownMark('t', rootId) >= at
  if (!t.mine && !opened) return 0
  return t.others.reduce((n, x) => n + (x > at ? 1 : 0), 0)
}

export const threadNewest = (rootId: string) => index().threads.get(rootId)?.newest ?? 0

/** Whether the page is on screen (a hidden tab reads nothing). */
export function useVisible(): boolean {
  return useSyncExternalStore((cb) => { document.addEventListener('visibilitychange', cb); return () => document.removeEventListener('visibilitychange', cb) }, () => document.visibilityState === 'visible')
}
