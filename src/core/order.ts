// The order of the channels in the list, set by the belfry's admins by dragging them (#issues,
// 2026-11-13): their latest NIP-78 event on the community relay, d = belfry:channel-order, content
// {"order": [channel ids]}. Channels it doesn't name keep their place after the named ones.
import { all } from './store'
import { enqueue, me } from './sync'
import { isBelfryAdmin } from './people'
import { isAdmin } from './access'

export const ORDER_D = 'belfry:channel-order'

export function channelOrder(): string[] {
  const own = all(me()).filter((e) => e.kind === 30078 && e.tags.some((t) => t[0] === 'd' && t[1] === ORDER_D) && (isBelfryAdmin(e.pubkey) || (e.pubkey === me() && isAdmin())))
  const last = own.sort((a, b) => a.created_at - b.created_at).at(-1)
  try { const o = JSON.parse(last?.content || '{}').order; return Array.isArray(o) ? o.filter((x: unknown): x is string => typeof x === 'string').slice(0, 1000) : [] } catch { return [] }
}

/** LIST in the admins' order (a stable sort: the ones it doesn't name stay as they were, after). */
export function inOrder<T extends { id: string }>(list: T[], order = channelOrder()): T[] {
  const at = new Map(order.map((id, i) => [id, i]))
  return list.map((x, i) => ({ x, i })).sort((a, b) => (at.get(a.x.id) ?? 1e6 + a.i) - (at.get(b.x.id) ?? 1e6 + b.i)).map((y) => y.x)
}

export const mayOrder = () => isBelfryAdmin(me()) || isAdmin()
export const setChannelOrder = (order: string[]) =>
  enqueue('main', { kind: 30078, content: JSON.stringify({ order }), tags: [['d', ORDER_D]] })
