// Typing indicators on the relays (docs/typing.md; the rules are in typingcore.ts). Best effort
// throughout: never through the outbox, never retried, nothing sent when the socket is down or
// the signer doesn't answer within 3 s, so a typing event never delays a message.
import type { NostrEvent } from 'nostr-tools'
import type { Subscription } from 'nostr-tools/relay'
import { within } from './conn'
import type { Target } from './db'
import { group } from './model'
import { currentSigner, isLocalOnly, me, sockFor } from './sync'
import {
  EPHEMERAL_WRAP, REFUSED_PAUSE_MS, SIGNER_MS, TYPING_KIND, Throttle, Typists, dmTyping, groupTyping, placeKey,
  readDmTyping, readGroupTyping, type Place, type Seen,
} from './typingcore'

export { placeKey, typingLine, type Place } from './typingcore'

// ---- the setting (§4): this device's, on by default ----------------------------------------

const OFF_KEY = 'belfry-typing-off'
export function showMyTyping(): boolean {
  try { return localStorage.getItem(OFF_KEY) !== '1' } catch { return true }
}
export function setShowMyTyping(on: boolean) {
  try { if (on) localStorage.removeItem(OFF_KEY); else localStorage.setItem(OFF_KEY, '1') } catch { /* private mode: stays on */ }
}

// ---- sending --------------------------------------------------------------------------------

const throttle = new Throttle()
const pausedUntil: Partial<Record<Target, number>> = {}
const targetOf = (p: Place): Target => ('h' in p ? 'groups' : 'main')
/** what went out, for the tests and Diagnostics: no content (there is none), no keys */
export const typingStats = { sent: 0, skipped: 0, refused: 0, signerCalls: 0 }

/** demo mode: what would have gone out (the browser tests read it) */
export const demoTypingSent: { key: string; stop: boolean; at: number }[] = []

async function emit(place: Place, stop: boolean) {
  if (isLocalOnly()) { demoTypingSent.push({ key: placeKey(place), stop, at: Date.now() }); return }
  const target = targetOf(place)
  const so = sockFor(target)
  if (Date.now() < (pausedUntil[target] ?? 0) || !so.connected || !so.relay) { typingStats.skipped++; return }
  const r = so.relay
  let evs: NostrEvent[]
  try {
    const s = currentSigner()
    if ('h' in place) {
      typingStats.signerCalls++
      evs = [await within(s.sign(groupTyping(place, Date.now(), stop)), SIGNER_MS, 'the signer did not answer')]
    } else {
      typingStats.signerCalls += 2 * place.peers.filter((p) => p !== s.pubkey).length
      evs = await within(dmTyping(s, place.peers, Date.now(), stop), SIGNER_MS, 'the signer did not answer')
    }
  } catch { typingStats.skipped++; return }
  for (const ev of evs) {
    try {
      await within(r.publish(ev), 5_000, 'the relay did not answer')
      typingStats.sent++
    } catch (e) {
      // a refusal for the rate: nothing more to that relay for 30 s (§5); anything else: dropped
      if (/rate.?limit/i.test(e instanceof Error ? e.message : String(e))) { pausedUntil[target] = Date.now() + REFUSED_PAUSE_MS; typingStats.refused++; return }
    }
  }
}

/** The composer of PLACE now holds TEXT after a keystroke (not a restored draft, not an edit of
 *  an older message: the caller doesn't call then). ALLOWED: false in an archived channel, for a
 *  reading guest. */
export function typed(place: Place, text: string, allowed = true) {
  if (!allowed || !showMyTyping() || !me()) return
  if (throttle.typed(placeKey(place), text, Date.now())) void emit(place, false)
}
/** The field of PLACE was emptied without sending: "stop", when a typing event went out lately. */
export function cleared(place: Place, allowed = true) {
  if (throttle.cleared(placeKey(place), Date.now()) && allowed && showMyTyping() && me()) void emit(place, true)
}
/** The message of PLACE went (the message itself ends the indicator: no stop). */
export function messageSent(place: Place) { throttle.sent(placeKey(place)) }

// ---- receiving ------------------------------------------------------------------------------

const typists = new Typists()
const listeners = new Set<() => void>()
let redraw: ReturnType<typeof setTimeout> | undefined
function changed() {
  listeners.forEach((f) => f())
  clearTimeout(redraw)
  const next = typists.nextEnd(Date.now())
  if (next !== null) redraw = setTimeout(changed, Math.max(50, next - Date.now() + 20))
}
export function onTyping(f: () => void) { listeners.add(f); return () => { listeners.delete(f) } }

function seen(s: Seen | null) {
  if (!s) return
  typists.see(s, Date.now())
  changed()
}

/** Who types in PLACE now, by pubkey. LASTMSG: when each one's latest message there was
 *  written (seconds): a message ends their indicator. */
export function typingIn(place: Place, lastMsg?: (pubkey: string) => number): string[] {
  const who = typists.who(placeKey(place), Date.now(), lastMsg)
  if (!('h' in place)) return who
  // not in the group (or removed from it): ignored
  const g = group(place.h)
  return g && g.members.size ? who.filter((p) => g.members.has(p) || g.admins.has(p)) : who
}

/** A subscription that is opened again on every new socket, and after the relay asked for AUTH. */
function live(target: Target, filter: () => Record<string, unknown>, onevent: (e: NostrEvent) => void): () => void {
  const so = sockFor(target)
  let sub: Subscription | undefined
  let over = false
  let authTries = 0
  const open = () => {
    const r = so.relay
    if (over || !r?.connected) return
    sub?.close()
    const mine = (sub = r.subscribe([filter() as never], {
      onevent,
      onclose: (reason) => {
        if (over || sub !== mine) return
        sub = undefined
        // the groups relay signs us in first: ask again once it did
        if (reason.startsWith('auth-required') && authTries++ < 5) setTimeout(open, 1500)
      },
    }))
  }
  const off = so.listen({ open: () => { authTries = 0; open() } })
  open()
  return () => { over = true; off(); sub?.close() }
}

const groupSubs = new Map<string, { n: number; stop: () => void }>()
/** While a group conversation (or one of its threads) is open: its typing events. */
export function watchGroup(h: string): () => void {
  if (isLocalOnly()) return () => {}
  let w = groupSubs.get(h)
  if (!w) {
    const stop = live('groups', () => ({ kinds: [TYPING_KIND], '#h': [h], since: Math.floor(Date.now() / 1000) - 5 }), (e) => seen(readGroupTyping(e, me(), Date.now())))
    w = { n: 0, stop }
    groupSubs.set(h, w)
  }
  w.n++
  let done = false
  return () => {
    if (done) return
    done = true
    if (--w!.n === 0) { w!.stop(); groupSubs.delete(h) }
  }
}

// direct messages: one subscription for every conversation (the list may show "typing…" too).
// Each wrap costs two decryptions: one at a time, at most 20 waiting (the rest are dropped).
let dmStop: (() => void) | undefined
let dmSelf = ''
let dmQueue: NostrEvent[] = []
let dmBusy = false
async function readDms() {
  if (dmBusy) return
  dmBusy = true
  try {
    for (let w = dmQueue.shift(); w; w = dmQueue.shift()) {
      try { seen(await within(readDmTyping(w, currentSigner(), Date.now()), SIGNER_MS * 2, 'the signer did not answer')) } catch { /* the signer is away: dropped */ }
    }
  } finally { dmBusy = false }
}
export function watchDms(): () => void {
  if (dmStop && dmSelf !== me()) dmStop() // another account now
  if (isLocalOnly() || !me() || dmStop) return () => {}
  const self = (dmSelf = me())
  const stop = live('main', () => ({ kinds: [EPHEMERAL_WRAP], '#p': [self], since: Math.floor(Date.now() / 1000) - 5 }), (w) => {
    if (dmQueue.length >= 20) return
    dmQueue.push(w)
    void readDms()
  })
  dmStop = () => { stop(); dmStop = undefined; dmQueue = [] }
  return dmStop
}

/** demo mode: FROM types in PLACE (a received event, as the relay would bring it). */
export function demoTyping(from: string, place: Place, stop = false) {
  seen({ from, key: placeKey(place), at: Math.floor(Date.now() / 1000), stop })
}
