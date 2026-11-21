// Typing indicators, the wire and the rules (collective-belfry docs/typing.md, 7750419; asked by
// Xavier 2026-11-21). Pure: the signer and the clock come in, so the tests run it in node.
//
// - Channels, groups, event chats, proposals, threads: kind 20100 signed by the person, h (and e =
//   the thread's root inside a thread), to the groups relay.
// - Direct messages: a kind 20100 rumor with p for every other participant, sealed (kind 13) and
//   wrapped in a kind 21059 ephemeral gift wrap to each other participant on the community
//   relay, with the real created_at. No copy to oneself.
// - At most one every 4 s per place while typing; "stop" when the field is emptied within 6 s of
//   one; shown for 6 s after the latest; anything more than 10 s off is ignored (relays replay
//   ephemeral events: rnostr up to 5 min).

import { finalizeEvent, generateSecretKey, getEventHash, nip44, verifyEvent, type EventTemplate, type NostrEvent } from 'nostr-tools'

export const TYPING_KIND = 20100
export const EPHEMERAL_WRAP = 21059
export const SEND_GAP_MS = 4_000
export const SHOW_MS = 6_000
export const SKEW_S = 10
export const SIGNER_MS = 3_000
export const REFUSED_PAUSE_MS = 30_000
/** messages that end someone's typing where they land */
export const MESSAGE_KINDS = [9, 1111, 14, 15]

/** Where someone types: a group conversation (h), one of its threads (h + root), or a DM (the
 *  other participants). */
export type Place = { h: string; root?: string } | { peers: string[] }

/** One key per place. A DM's is its participants but me, sorted: dm.ts's conv ("n:a,b"). */
export function placeKey(p: Place): string {
  if ('h' in p) return p.root ? `g:${p.h}:${p.root}` : `g:${p.h}`
  return 'n:' + [...new Set(p.peers)].sort().join(',')
}

export interface Signer {
  pubkey: string
  sign(t: EventTemplate): Promise<NostrEvent>
  encrypt(peer: string, text: string): Promise<string>
  decrypt(peer: string, payload: string): Promise<string>
}

const sec = (ms: number) => Math.floor(ms / 1000)

/** The template of a group typing event (to sign). */
export function groupTyping(p: { h: string; root?: string }, nowMs: number, stop = false): EventTemplate {
  const tags = [['h', p.h]]
  if (p.root) tags.push(['e', p.root])
  if (stop) tags.push(['status', 'stop'])
  return { kind: TYPING_KIND, created_at: sec(nowMs), tags, content: '' }
}

/** A DM typing event: one ephemeral wrap per other participant (2 signer calls each: the seal's
 *  encryption and its signature). */
export async function dmTyping(s: Signer, peers: string[], nowMs: number, stop = false): Promise<NostrEvent[]> {
  const others = [...new Set(peers)].filter((p) => p !== s.pubkey)
  const at = sec(nowMs)
  const tags = others.map((p) => ['p', p])
  if (stop) tags.push(['status', 'stop'])
  const rumor = { kind: TYPING_KIND, created_at: at, tags, content: '', pubkey: s.pubkey } as NostrEvent
  rumor.id = getEventHash(rumor)
  return Promise.all(others.map(async (to) => {
    const seal = await s.sign({ kind: 13, created_at: at, tags: [], content: await s.encrypt(to, JSON.stringify(rumor)) })
    const one = generateSecretKey()
    return finalizeEvent({ kind: EPHEMERAL_WRAP, created_at: at, tags: [['p', to]], content: nip44.v2.encrypt(JSON.stringify(seal), nip44.v2.utils.getConversationKey(one, to)) }, one)
  }))
}

/** What a typing event says, once read. */
export interface Seen { from: string; key: string; at: number; stop: boolean }

const fresh = (at: number, nowMs: number) => Math.abs(at - nowMs / 1000) <= SKEW_S

/** A group typing event → what it says; null when it's no typing event, old (a replay) or from
 *  the future, or mine. The signature is the relay's job and nostr-tools' (verified on receipt). */
export function readGroupTyping(e: NostrEvent, me: string, nowMs: number): Seen | null {
  if (e.kind !== TYPING_KIND || e.pubkey === me || !fresh(e.created_at, nowMs)) return null
  const h = e.tags.find((t) => t[0] === 'h')?.[1]
  if (!h) return null
  const root = e.tags.find((t) => t[0] === 'e')?.[1]
  return { from: e.pubkey, key: placeKey({ h, root }), at: e.created_at, stop: e.tags.some((t) => t[0] === 'status' && t[1] === 'stop') }
}

/** An ephemeral wrap → what its rumor says; null as above, also when the wrap is old (no
 *  decryption at all then: a replay costs no signer call), the seal isn't signed by the rumor's
 *  author, or it isn't for me. */
export async function readDmTyping(w: NostrEvent, s: Signer, nowMs: number): Promise<Seen | null> {
  if (w.kind !== EPHEMERAL_WRAP || !fresh(w.created_at, nowMs) || !w.tags.some((t) => t[0] === 'p' && t[1] === s.pubkey)) return null
  let rumor: NostrEvent
  try {
    const seal = JSON.parse(await s.decrypt(w.pubkey, w.content)) as NostrEvent
    if (seal.kind !== 13 || !verifyEvent(seal)) return null
    rumor = JSON.parse(await s.decrypt(seal.pubkey, seal.content)) as NostrEvent
    if (rumor.pubkey !== seal.pubkey) return null // no one types in someone else's name
  } catch { return null }
  if (rumor.kind !== TYPING_KIND || rumor.pubkey === s.pubkey || !fresh(rumor.created_at, nowMs)) return null
  const ps = rumor.tags.filter((t) => t[0] === 'p').map((t) => t[1])
  if (!ps.includes(s.pubkey)) return null
  return { from: rumor.pubkey, key: placeKey({ peers: [rumor.pubkey, ...ps].filter((p) => p !== s.pubkey) }), at: rumor.created_at, stop: rumor.tags.some((t) => t[0] === 'status' && t[1] === 'stop') }
}

/** Who is typing where: until SHOW_MS after their latest event, their stop, or their message
 *  (LASTMSG: when their latest message in that place was written, in seconds). Seconds only: a
 *  message of the typing event's own second ends it only if it arrived after the dots showed
 *  (BASE: their latest message when the dots first showed). */
type Entry = { ms: number; at: number; base?: number }
export class Typists {
  private at = new Map<string, Map<string, Entry>>() // key → person → when seen, its created_at
  see(s: Seen, nowMs: number) {
    const m = this.at.get(s.key) ?? new Map<string, Entry>()
    if (s.stop) m.delete(s.from)
    else if ((m.get(s.from)?.at ?? 0) <= s.at) m.set(s.from, { ms: nowMs, at: s.at })
    this.at.set(s.key, m)
  }
  who(key: string, nowMs: number, lastMsg: (pubkey: string) => number = () => 0): string[] {
    const m = this.at.get(key)
    if (!m) return []
    for (const [p, t] of m) {
      const last = lastMsg(p)
      t.base ??= last
      if (nowMs - t.ms >= SHOW_MS || last > t.at || (last === t.at && last !== t.base)) m.delete(p)
    }
    return [...m.keys()]
  }
  /** the next moment someone's indicator ends (to redraw then), or null */
  nextEnd(nowMs: number): number | null {
    let next: number | null = null
    for (const m of this.at.values()) for (const t of m.values()) if (t.ms + SHOW_MS > nowMs) next = Math.min(next ?? Infinity, t.ms + SHOW_MS)
    return next
  }
}

/** When to send (§2): typing at most once every 4 s per place; a stop only within 6 s of one. */
export class Throttle {
  private last = new Map<string, number>()
  typed(key: string, text: string, nowMs: number): boolean {
    if (!text.trim()) return false
    const l = this.last.get(key)
    if (l !== undefined && nowMs - l < SEND_GAP_MS) return false
    this.last.set(key, nowMs)
    return true
  }
  /** the field was emptied without sending */
  cleared(key: string, nowMs: number): boolean {
    const l = this.last.get(key)
    this.last.delete(key)
    return l !== undefined && nowMs - l < SHOW_MS
  }
  /** the message went: the next keystroke starts again */
  sent(key: string) { this.last.delete(key) }
}

/** "Lina is typing…" · "Lina and Tom are typing…" · "Several people are typing…" */
export function typingLine(names: string[]): string {
  if (names.length === 0) return ''
  if (names.length === 1) return `${names[0]} is typing…`
  if (names.length === 2) return `${names[0]} and ${names[1]} are typing…`
  return 'Several people are typing…'
}
