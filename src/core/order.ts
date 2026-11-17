// The order of the channels in the list, set by the belfry's admins by dragging them (#issues,
// 2026-11-13): their latest NIP-78 event on the community relay, d = belfry:channel-order, content
// {"order": [channel ids]}. Channels it doesn't name keep their place after the named ones.
//
// Categories (the owner, 2026-11-17; same format as the iPhone app's SPEC 19.34): the same event
// gains `categories`:
//   {"order": [ids, flattened, for older clients],
//    "categories": [{"id": "life", "name": "Community life", "channels": [ids]}, …]}
//  - built-in ids: `life` (the default: any channel no category names lands there, after the ones
//    it names) and `building` (default channels `proposals`, `website`). "The town" (the Square,
//    Members, the Charter) is never in the list.
//  - custom ids: a short slug + 4 random characters. A built-in without `name` has its default
//    title. A built-in left out of the list still exists with its defaults (`life` first,
//    `building` last; `building` only shows when it has channels).
//  - a channel named twice counts in the first category; unknown ids are ignored (and kept when an
//    admin rewrites the list: they may be channels this device doesn't see).
//  - without `categories`: exactly the old behaviour (life, then building, both in `order`).
import { all } from './store'
import { enqueue, me } from './sync'
import { isBelfryAdmin } from './people'
import { isAdmin } from './access'

export const ORDER_D = 'belfry:channel-order'
export const LIFE = 'life'
export const BUILDING = 'building'
export const BUILTIN_NAME: Record<string, string> = { [LIFE]: 'Community life', [BUILDING]: 'Building together' }
export const BUILDING_DEFAULT = ['proposals', 'website']

export type RawCategory = { id: string; name?: string; channels: string[] }
type Content = { order: string[]; categories?: RawCategory[] }

const strings = (x: unknown, max = 1000): string[] => (Array.isArray(x) ? x.filter((s: unknown): s is string => typeof s === 'string').slice(0, max) : [])

/** The latest order event's content, as far as it reads. */
export function orderContent(): Content {
  const own = all(me()).filter((e) => e.kind === 30078 && e.tags.some((t) => t[0] === 'd' && t[1] === ORDER_D) && (isBelfryAdmin(e.pubkey) || (e.pubkey === me() && isAdmin())))
  const last = own.sort((a, b) => a.created_at - b.created_at).at(-1)
  try {
    const c = JSON.parse(last?.content || '{}')
    const out: Content = { order: strings(c?.order) }
    if (Array.isArray(c?.categories)) {
      const seen = new Set<string>()
      out.categories = (c.categories as unknown[]).flatMap((x) => {
        const r = x as { id?: unknown; name?: unknown; channels?: unknown }
        if (!r || typeof r.id !== 'string' || !r.id.trim() || seen.has(r.id)) return []
        seen.add(r.id)
        return [{ id: r.id, ...(typeof r.name === 'string' && r.name.trim() ? { name: r.name.trim().slice(0, 60) } : {}), channels: strings(r.channels) }]
      }).slice(0, 50)
    }
    return out
  } catch { return { order: [] } }
}

export const channelOrder = (): string[] => orderContent().order

/** LIST in the admins' order (a stable sort: the ones it doesn't name stay as they were, after). */
export function inOrder<T extends { id: string }>(list: T[], order = channelOrder()): T[] {
  const at = new Map(order.map((id, i) => [id, i]))
  return list.map((x, i) => ({ x, i })).sort((a, b) => (at.get(a.x.id) ?? 1e6 + a.i) - (at.get(b.x.id) ?? 1e6 + b.i)).map((y) => y.x)
}

export type Category<T> = {
  id: string
  name: string
  builtin: boolean
  /** the channels this device knows, in order */
  items: T[]
  /** every id the event names for it (unknown ones too): kept when the list is rewritten */
  named: string[]
  /** in the event's list (a built-in left out is implied) */
  listed: boolean
}

/** CHANNELS (the town's, without the Square) in their categories. */
export function categorize<T extends { id: string }>(channels: T[], content = orderContent()): Category<T>[] {
  const byId = new Map(channels.map((c) => [c.id, c]))
  const order = content.order
  if (!content.categories) {
    // the old behaviour: life, then building, each in `order`
    const life = inOrder(channels.filter((c) => !BUILDING_DEFAULT.includes(c.id)), order)
    const building = inOrder(channels.filter((c) => BUILDING_DEFAULT.includes(c.id)), order)
    return [
      { id: LIFE, name: BUILTIN_NAME[LIFE], builtin: true, items: life, named: [], listed: false },
      { id: BUILDING, name: BUILTIN_NAME[BUILDING], builtin: true, items: building, named: [], listed: false },
    ]
  }
  const raw = [...content.categories]
  if (!raw.some((c) => c.id === LIFE)) raw.unshift({ id: LIFE, channels: [] })
  const buildingListed = raw.some((c) => c.id === BUILDING)
  const cats: Category<T>[] = raw.map((c) => ({
    id: c.id,
    name: c.name || BUILTIN_NAME[c.id] || c.id,
    builtin: !!BUILTIN_NAME[c.id],
    items: [],
    named: c.channels,
    listed: c.id !== LIFE || content.categories!.some((x) => x.id === LIFE),
  }))
  const taken = new Set<string>()
  for (const c of cats) for (const id of c.named) {
    if (taken.has(id)) continue
    const ch = byId.get(id)
    if (!ch) continue
    taken.add(id)
    c.items.push(ch)
  }
  if (!buildingListed) {
    const b: Category<T> = { id: BUILDING, name: BUILTIN_NAME[BUILDING], builtin: true, items: [], named: [], listed: false }
    for (const id of BUILDING_DEFAULT) { const ch = byId.get(id); if (ch && !taken.has(id)) { taken.add(id); b.items.push(ch) } }
    cats.push(b)
  }
  const life = cats.find((c) => c.id === LIFE)!
  life.items.push(...inOrder(channels.filter((c) => !taken.has(c.id)), order))
  return cats
}

/** Shown in the list: non-admins see categories with channels; admins see every category (empty
 *  ones with their +), except `building` left out of the list and empty. */
export const shownCategories = <T,>(cats: Category<T>[], admin: boolean) =>
  cats.filter((c) => c.items.length > 0 || (admin && (c.listed || c.id === LIFE)))

/** The event content for these categories (what the admins see, in order). Ids this device
 *  doesn't know stay in their category, after the known ones. */
export function layoutContent<T extends { id: string }>(cats: { id: string; name: string; items: T[]; named?: string[] }[]): Content {
  const known = new Set(cats.flatMap((c) => c.items.map((x) => x.id)))
  const seen = new Set<string>()
  const categories = cats.map((c) => {
    const channels = [...c.items.map((x) => x.id), ...(c.named ?? []).filter((id) => !known.has(id))].filter((id) => (seen.has(id) ? false : (seen.add(id), true)))
    const name = c.name.trim()
    return { id: c.id, ...(name && name !== BUILTIN_NAME[c.id] ? { name } : !BUILTIN_NAME[c.id] ? { name: name || c.id } : {}), channels }
  })
  return { order: categories.flatMap((c) => c.channels), categories }
}

/** A new category's id: a short slug of its name + 4 random characters. */
export function categoryId(name: string): string {
  const s = name.normalize('NFKD').replace(/[̀-ͯ]/g, '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 16) || 'category'
  const r = Array.from(crypto.getRandomValues(new Uint8Array(4)), (b) => 'abcdefghijklmnopqrstuvwxyz0123456789'[b % 36]).join('')
  return `${s}-${r}`
}

export const mayOrder = () => isBelfryAdmin(me()) || isAdmin()
export const setChannelOrder = (order: string[]) => {
  // a client that only reorders (the OSV look's list) keeps the categories as they were
  const c = orderContent()
  return enqueue('main', { kind: 30078, content: JSON.stringify(c.categories ? { order, categories: c.categories } : { order }), tags: [['d', ORDER_D]] })
}
export const setLayout = (content: Content) => enqueue('main', { kind: 30078, content: JSON.stringify(content), tags: [['d', ORDER_D]] })
