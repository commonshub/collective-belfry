// Read state, the pure part (no imports): what was read up to when, per channel, thread and
// direct-message conversation, merged across a person's devices. The contract is the Android
// repo's SPEC.md 19.27 and vectors/readstate.json (the iOS app follows the same); readstate.ts
// does the I/O (the encrypted kind 30078 `d` = belfry:read on the community relay).
//
// JSON: {"v":1,"s":<unix s>,"m":<unix s>,"c":{"<group id>":<unix s>},"t":{"<thread root id>":<unix s>},
//        "d":{"<dm conversation key>":<unix s>}} — every number is "read up to and including".
// Merge: every number is the highest of the two (reading never un-reads). Unknown top-level
// fields are kept (the relay's copy first, then the local one).

export const READ_D = 'belfry:read'
export const DAY = 86400
/** Marks older than this count as read anyway, and are pruned from the event. */
export const KEEP_DAYS = 90
/** The event's entries weigh at most this many units (UTF-8 bytes of the key + 16 each). */
export const MAX_WEIGHT = 40000
export const MAX_KEY = 200
export type Section = 'c' | 't' | 'd'
export const SECTIONS: Section[] = ['c', 't', 'd']

export interface ReadState {
  v: 1
  /** Start: channels, threads and mentions with no mark of their own count as read up to here. */
  s: number
  /** @Mentions list seen up to. */
  m: number
  c: Record<string, number>
  t: Record<string, number>
  d: Record<string, number>
  /** Fields a newer client wrote, kept as they are. */
  extra: Record<string, unknown>
}

export const empty = (): ReadState => ({ v: 1, s: 0, m: 0, c: {}, t: {}, d: {}, extra: {} })

const num = (x: unknown): number => (typeof x === 'number' && Number.isFinite(x) && x > 0 ? Math.floor(x) : 0)
const utf8 = (s: string) => new TextEncoder().encode(s).length

/** Read the decrypted content. Anything unreadable is dropped, never thrown. */
export function parse(json: string | unknown): ReadState {
  let o: unknown = json
  if (typeof json === 'string') { try { o = JSON.parse(json) } catch { return empty() } }
  const r = empty()
  if (!o || typeof o !== 'object' || Array.isArray(o)) return r
  for (const [k, v] of Object.entries(o as Record<string, unknown>)) {
    if (k === 'v') continue
    if (k === 's' || k === 'm') { r[k] = num(v); continue }
    if (k === 'c' || k === 't' || k === 'd') {
      if (v && typeof v === 'object' && !Array.isArray(v))
        for (const [key, at] of Object.entries(v as Record<string, unknown>)) {
          const n = num(at)
          if (n && key && key.length <= MAX_KEY) r[k][key] = n
        }
      continue
    }
    r.extra[k] = v
  }
  return r
}

/** What goes in the event (before encryption): keys sorted, so equal states give equal text. */
export function serialize(r: ReadState): string {
  const sorted = (m: Record<string, number>) => Object.fromEntries(Object.keys(m).sort().map((k) => [k, m[k]]))
  const out: Record<string, unknown> = {}
  for (const k of Object.keys(r.extra).sort()) if (!['v', 's', 'm', 'c', 't', 'd'].includes(k)) out[k] = r.extra[k]
  return JSON.stringify({ ...out, v: 1, s: r.s, m: r.m, c: sorted(r.c), t: sorted(r.t), d: sorted(r.d) })
}

/** LOCAL and REMOTE together: every mark is the highest of the two; unknown fields REMOTE's first. */
export function merge(local: ReadState, remote: ReadState): ReadState {
  const r: ReadState = { v: 1, s: Math.max(local.s, remote.s), m: Math.max(local.m, remote.m), c: {}, t: {}, d: {}, extra: { ...local.extra, ...remote.extra } }
  for (const sec of SECTIONS) {
    r[sec] = { ...local[sec] }
    for (const [k, at] of Object.entries(remote[sec])) r[sec][k] = Math.max(r[sec][k] ?? 0, at)
  }
  return r
}

/** Read KEY of SECTION up to AT (never backwards). Returns the same object when nothing changes. */
export function mark(r: ReadState, sec: Section | 'm', key: string, at: number): ReadState {
  at = num(at)
  if (sec === 'm') return at > r.m ? { ...r, m: at } : r
  if (!at || !key || key.length > MAX_KEY || (r[sec][key] ?? 0) >= at) return r
  return { ...r, [sec]: { ...r[sec], [key]: at } }
}

/** Below this everything counts as read: KEEP_DAYS ago, and for channels / threads / mentions
 *  also the start `s` (direct messages had their own marks before: `s` does not apply to them). */
export function floor(r: ReadState, sec: Section | 'm', now: number): number {
  const old = now - KEEP_DAYS * DAY
  return sec === 'd' ? old : Math.max(old, r.s)
}

/** Read up to when: the key's own mark or the floor, whichever is later. */
export function readUpTo(r: ReadState, sec: Section | 'm', key: string, now: number): number {
  const own = sec === 'm' ? r.m : (r[sec][key] ?? 0)
  return Math.max(own, floor(r, sec, now))
}

/** Before publishing: drop marks at or under their floor, then the oldest channel and thread marks
 *  while the entries weigh more than MAX_WEIGHT; `s` rises to the newest mark dropped that way, so
 *  nothing dropped turns unread. Direct-message marks go last (`s` is no floor for them), only if
 *  they alone still weigh too much. Ties in age: section c before t, then key (code points). */
export function prune(r: ReadState, now: number): ReadState {
  const out: ReadState = { ...r, c: {}, t: {}, d: {}, extra: { ...r.extra } }
  type E = { sec: Section; key: string; at: number }
  const list: E[] = []
  for (const sec of SECTIONS)
    for (const [key, at] of Object.entries(r[sec])) if (at > floor(r, sec, now)) list.push({ sec, key, at })
  const order: Record<Section, number> = { c: 0, t: 1, d: 2 }
  // channels and threads first (oldest first), then direct messages (oldest first)
  list.sort((a, b) => (a.sec === 'd' ? 1 : 0) - (b.sec === 'd' ? 1 : 0) || a.at - b.at || order[a.sec] - order[b.sec] || (a.key < b.key ? -1 : a.key > b.key ? 1 : 0))
  let weight = list.reduce((n, e) => n + utf8(e.key) + 16, 0)
  let i = 0
  while (weight > MAX_WEIGHT && i < list.length) {
    const e = list[i++]
    weight -= utf8(e.key) + 16
    if (e.sec !== 'd') out.s = Math.max(out.s, e.at)
  }
  for (const e of list.slice(i)) if (e.at > floor(out, e.sec, now)) out[e.sec][e.key] = e.at
  return out
}

/** Does REMOTE (the newest event on the relay) already say everything LOCAL says? If not, LOCAL
 *  (merged with it and pruned) has to be published. Marks under their floor don't count. */
export function covers(remote: ReadState, local: ReadState, now: number): boolean {
  if (local.s > remote.s || local.m > Math.max(remote.m, floor(remote, 'm', now))) return false
  for (const sec of SECTIONS)
    for (const [k, at] of Object.entries(local[sec]))
      if (at > floor(local, sec, now) && at > readUpTo(remote, sec, k, now)) return false
  return true
}
