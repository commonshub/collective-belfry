// Sealed conversations (canvas "Messages · encryption", approved 20 Nov): which devices can read
// a conversation, the device lines in it, a new device's locked history, the Allow prompt on my
// other devices, sharing history with someone's new device, removing one of my devices.
//
// SCREENS ONLY for now: the lead's contract (collective-belfry docs/sealed.md + vectors) is not
// here yet, so nothing in this file reads or writes keys, tags or the relay. Outside demo mode
// nothing is sealed: no lock, no device lines, no banner, no prompt (the app looks as before).
// In demo mode, window.demoSealed() fills it with the boards' people (Sam Rivera as me).
//
// Rules from the boards: never a key, npub or id as text; only MY devices are named, other
// people's are a count (and a "changed in the last 30 days" line); channels never have a lock.
import { thread, type Conv } from './dm'
import * as R from './sealedrt'
import * as SC from './sealedcore'
import { touch } from './store'
import { me } from './sync'
import { isGuest } from './invite'

export type DeviceKind = 'phone' | 'tablet' | 'computer'

/** One of my devices (the only ones shown by name). */
export interface MyDevice {
  id: string
  name: string // "iPhone", "Safari on Mac"
  kind: DeviceKind
  added: number // ms
  from?: string // the device that let it in ("from iPhone")
  active?: number // ms, last seen
  here?: boolean // this device
}

/** Someone else: how many devices, and their last change if it is within 30 days. */
export interface TheirDevices { count: number; change?: { what: 'added' | 'removed'; at: number } }

/** A line in a conversation, in the style of a channel's join line. */
export interface DeviceLine {
  id: string
  at: number // ms
  who: string // a pubkey, or me()
  what: 'added' | 'removed' | 'new-locked'
  device?: string // my own lines only: its name
  kind?: DeviceKind // mine: for the icon; new-locked: "A new phone"
  shared?: boolean // new-locked: history was shared (the line turns neutral)
}

interface State {
  mine: MyDevice[]
  people: Map<string, TheirDevices>
  sealed: Set<Conv>
  lines: Map<Conv, DeviceLine[]>
  /** this device is new: the conversations whose earlier messages it can't read yet */
  lockedHere: Set<Conv>
  /** when each conversation was sealed (ms): messages from before read as usual */
  sealedAt: Map<Conv, number>
  /** demo: messages that came unsealed after sealing (rumor ids) */
  unsealed: Set<string>
  /** §8: messages received in a shared history, by rumor id → who shared them (this device only) */
  shared: Map<string, string>
  /** on this (older) device: a new device of mine waits for Allow */
  asking?: { id: string; name: string; kind: DeviceKind; at: number }
  /** demo: the account has 20 devices already (canvas v90) */
  full?: boolean
  /** Link a phone or tablet: the device that just came in by scanning my code */
  linked?: { name: string; kind: DeviceKind }
  /** §5.2 2a: devices of mine added without this device asking (demo) */
  notices: AddedNotice[]
}

/** §5.2 2a (boards messages/encryption-device-added, -devices-added): a device added to my head
 *  without this device having shown a prompt or a link code for it. */
export interface AddedNotice { id: string; name: string; kind: DeviceKind; at: number }

const empty = (): State => ({ mine: [], people: new Map(), sealed: new Set(), lines: new Map(), lockedHere: new Set(), sealedAt: new Map(), shared: new Map(), unsealed: new Set(), notices: [] })
let state: State = empty()

// Demo mode: the boards' people (setDemoSealed). On a belfry: the real chains (sealedrt.ts).
const real = () => !demo && R.active()
const DAY_MS = 864e5
const kindOf = (name: string): DeviceKind => (/iphone|android|pixel|galaxy|phone|mobile/i.test(name) ? (/ipad|tablet/i.test(name) ? 'tablet' : 'phone') : /ipad|tablet/i.test(name) ? 'tablet' : 'computer')
const sessionName = (pk: string) => { const n = R.deviceNames()[pk]?.name; if (typeof n === 'string' && n) return n; const r = R.sessionsNow()?.find((x) => x.client_pubkey === pk); return r ? (r.device || r.application_name || '') : '' }
const participantsOf = (conv: Conv) => (conv.startsWith('n:') ? conv.slice(2).split(',') : [])

function realMine(): MyDevice[] {
  const c = R.chainOf(me())
  const rows = R.sessionsNow() ?? []
  // no chain yet: where I'm signed in is still my list (the chat sessions, canvas v88)
  if (!c) return rows.filter((r) => r.chat === true && r.client_pubkey).map((r) => {
    const name = r.device || r.application_name || 'A device'
    return { id: r.client_pubkey!, name, kind: kindOf(name), added: r.created_at ? (SC.whenOf(r.created_at) * 1000 || 0) : 0, active: r.last_activity ? (SC.whenOf(r.last_activity) * 1000 || undefined) : undefined, here: r.client_pubkey === R.devicePub() }
  })
  return SC.headOf(c).devices.map((d) => {
    const at = c.added.get(d)
    const by = at && at.signer !== d ? sessionName(at.signer) || undefined : undefined
    const row = rows.find((x) => x.client_pubkey === d)
    const named = R.deviceNames()[d]
    const name = (typeof named?.name === 'string' && named.name) || sessionName(d) || 'A device'
    return { id: d, name, kind: kindOf(name), added: (at?.created_at ?? 0) * 1000, from: by, active: row?.last_activity ? (SC.whenOf(row.last_activity) * 1000 || undefined) : undefined, here: d === R.devicePub() }
  })
}
function realTheirs(pk: string): TheirDevices | undefined {
  const c = R.chainOf(pk)
  if (!c) return undefined
  const steps = SC.rosterSteps(c).filter((x) => x.added.length || x.removed.length)
  const last = steps[steps.length - 1]
  const change = last && Date.now() - last.at * 1000 < 30 * DAY_MS ? { what: (last.removed.length ? 'removed' : 'added') as 'added' | 'removed', at: last.at * 1000 } : undefined
  return { count: SC.headOf(c).devices.length, change }
}
/** §3.4: one line per device that appeared or left, one roster step at a time, from the newest
 *  message of the conversation at its first sync on; mine name the device. A fresh start: amber. */
function realLines(conv: Conv): DeviceLine[] {
  // roster steps dated at or after the moment this device first saw the conversation sealed
  const from = R.firstSeenOf(conv) ?? Date.now()
  const out: DeviceLine[] = []
  for (const pk of [...new Set([...participantsOf(conv), me()])]) {
    const c = R.chainOf(pk)
    if (!c) continue
    for (const st of SC.rosterSteps(c)) {
      const at = st.at * 1000
      if (at < from) continue
      for (const d of st.added) out.push({ id: `${pk}:${d}:+${st.at}`, at, who: pk, what: 'added', ...(pk === me() ? { device: sessionName(d) || undefined, kind: kindOf(sessionName(d)) } : {}) })
      for (const d of st.removed) out.push({ id: `${pk}:${d}:-${st.at}`, at, who: pk, what: 'removed', ...(pk === me() ? { device: sessionName(d) || undefined, kind: kindOf(sessionName(d)) } : {}) })
    }
    const f = pk === me() ? undefined : R.freshStart(pk)
    if (f) out.push({ id: `${pk}:fresh:${f.head ?? f.root}`, at: f.at, who: pk, what: 'new-locked', shared: f.shared.includes(conv) }) // a fresh start or a switch (§3.3); their device's kind isn't known (v88)
  }
  return out.sort((a, b) => a.at - b.at)
}

// demo: Notes to myself is sealed as soon as I have devices (window.demoSealed), like the real rule
export const isSealed = (conv: Conv) => (real() ? R.isSealedConv(conv) : state.sealed.has(conv) || (conv === 'n:' + me() && state.mine.length > 0))
export const myDevices = () => (real() ? realMine() : state.mine)
export const hasDevices = () => myDevices().length > 0
/** canvas v117: this browser is my only device for sealed conversations (never for a guest; not
 *  before a letterbox exists: then there is nothing to lose) */
export const onlyDevice = () => !isGuest(me()) && (real() ? R.onlyDeviceHere() : state.mine.length === 1 && !!state.mine[0].here)
export const theirDevices = (pk: string): TheirDevices | undefined => (real() ? realTheirs(pk) : state.people.get(pk))
export const deviceLines = (conv: Conv) => (real() ? realLines(conv) : state.lines.get(conv) ?? [])
/** §5.5: in my head without the head key: the composer of a sealed conversation waits. */
export const waitingForSibling = (conv: Conv) => real() && R.inHeadNoKey() && R.isSealedConv(conv)
/** A sealed conversation while this device is out of my head (real only): no composer, nothing sent. */
export const outOfHead = (conv: Conv) => real() && R.outOfHead(conv)
/** board messages/encryption-locked-thread: the grey line in place of the composer (and the mark
 *  of a message held because of it) */
export const LOCKED_LINE = 'You can write here once this device is allowed.'
export const lockedHere = (conv: Conv) => (real() ? R.lockedConv(conv) : state.lockedHere.has(conv))
export const anyLockedHere = () => (real() ? R.showBanner() : state.lockedHere.size > 0)
export const sharedBy = (rid: string) => (real() ? R.sharedByOf(rid) : state.shared.get(rid))
/** When the conversation was sealed (ms; 0 when unknown). On a belfry: the latest of its people's
 *  first ready roster. */
export function sealedAt(conv: Conv): number {
  if (!real()) return state.sealedAt.get(conv) ?? 0
  let at = 0
  for (const pk of [...new Set([...participantsOf(conv), me()])]) { const r = R.chainOf(pk)?.rosters.find((x) => x.ready); if (r) at = Math.max(at, r.created_at * 1000) }
  return at
}
/** A device not yet allowed reads NO sealed message, old or new (canvas v84): only what came
 *  before the conversation was sealed reads as usual. On a belfry the locked ones never opened. */
export const lockedMsg = (conv: Conv, at: number) => (real() ? false : state.lockedHere.has(conv) && at * 1000 >= sealedAt(conv))
export function asking() {
  if (!real()) return state.asking
  const a = R.pendingAsk()
  return a && { id: a.id, name: a.name, kind: kindOf(a.name), at: a.signedIn ?? a.at }
}
export const linkedByScan = () => (real() ? (R.linkedName() ? { name: R.linkedName()!, kind: kindOf(R.linkedName()!) } : undefined) : state.linked)
/** Board messages/encryption-thread (v89): a message that came the old way in a sealed
 *  conversation, dated after this device first saw it sealed. */
export function notSealed(conv: Conv, d: { rid: string; at: number; sealed?: boolean; pending?: boolean; failed?: string }): boolean {
  if (!real()) return state.unsealed.has(d.rid)
  if (d.sealed || d.pending || d.failed || !R.isSealedConv(conv)) return false
  const first = R.firstSeenOf(conv)
  return first !== undefined && d.at * 1000 >= first
}
/** Remove is offered only on a device that can change the list (demo: always). */
/** §5.4: Start fresh is offered when this device isn't in my head (it can never read what is sealed
 *  to my devices without another device's Allow). */
export const canStartFresh = () => (real() ? R.active() && !!R.chainOf(me()) && !R.deviceIn() && !R.inHeadNoKey() : state.lockedHere.size > 0)
export const FRESH_NOT_DONE = 'Not done: this device can’t reach the belfry just now. Try again in a moment.'
/** Start fresh on this device (canvas v84 messages/encryption-start-fresh-confirm). */
export async function startFresh() {
  if (!real()) { state.lockedHere.clear(); touch(); return }
  try { await R.startFresh() } catch { throw new Error(FRESH_NOT_DONE) }
}
export const canRemoveDevices = () => (real() ? R.canRemove() : true)
/** The Allow card closes without an answer (it comes back next time). */
export const dismissAsking = () => { if (real()) R.dismissAsk(); else { state.asking = undefined; touch() } }
export const clearLinked = () => { if (real()) R.clearLinkedName(); state.linked = undefined; touch() }

const DAY = 864e5
/** A change counts for 30 days. */
export const recentChange = (t?: TheirDevices) => (t?.change && Date.now() - t.change.at < 30 * DAY ? t.change : undefined)

/** "today", "yesterday", "on 2 Oct" */
export function whenWord(at: number, now = Date.now()): string {
  const d0 = new Date(now); d0.setHours(0, 0, 0, 0)
  if (at >= +d0) return 'today'
  if (at >= +d0 - DAY) return 'yesterday'
  return 'on ' + dayMonth(at)
}
export const dayMonth = (at: number) => new Date(at).toLocaleDateString('en-GB', { day: 'numeric', month: 'short', ...(new Date(at).getFullYear() !== new Date().getFullYear() ? { year: 'numeric' } : {}) })

/** "just now", "12 min ago", "today at 9:40", else whenWord */
export function addedWhen(at: number, now = Date.now()): string {
  const m = (now - at) / 6e4
  if (m < 2) return 'just now'
  if (m < 60) return `${Math.round(m)} min ago`
  const d0 = new Date(now); d0.setHours(0, 0, 0, 0)
  if (at >= +d0) return 'today at ' + new Date(at).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
  return whenWord(at, now)
}

/** "active now", "active 3 days ago" */
export function activeWord(at?: number, now = Date.now()): string {
  if (!at) return ''
  const m = (now - at) / 6e4
  if (m < 5) return 'active now'
  if (m < 60) return `active ${Math.round(m)} min ago`
  if (m < 60 * 24) return `active ${Math.round(m / 60)} h ago`
  const d = Math.round(m / 60 / 24)
  return d === 1 ? 'active yesterday' : `active ${d} days ago`
}

export const kindWord = (k: DeviceKind) => (k === 'computer' ? 'computer' : k)
export const devicesWord = (n: number) => `${n} device${n === 1 ? '' : 's'}`

// ---- actions (demo: a short wait, then the state changes; the wire comes with docs/sealed.md) --

const later = (ms = 700) => new Promise<void>((r) => setTimeout(r, ms))
const ready = () => { if (!demo) throw new Error('Not available yet.') }
// Share history sends only the conversation's sealed messages
const rumorsOf = (conv: Conv) => thread(conv).filter((d) => d.kind === 14 && d.sealed && !d.pending && !d.failed)
  .map((d) => {
    const author = d.mine ? me() : d.from
    // kept since this version; older ones: the p tags rebuilt (everyone in it but the author)
    const tags = d.tags ?? [...new Set([...participantsOf(conv), me()])].filter((p) => p !== author).map((p) => ['p', p])
    return { pubkey: author, created_at: d.at, kind: 14, tags, content: d.text, id: '' }
  })

/** Allow on an older device: the new one reads everything (its banner goes). */
export async function allowAsking() {
  if (real()) { const a = R.pendingAsk(); if (a) await R.allowTapped(a.device); return }
  ready()
  if (state.full) { await later(300); throw new Error(R.FULL) } const a = state.asking; if (!a) return
  await later()
  if (state.asking?.id !== a.id) return // another device answered first
  state.mine.push({ id: a.id, name: a.name, kind: a.kind, added: a.at, from: state.mine.find((d) => d.here)?.name, active: Date.now() })
  state.asking = undefined
  touch()
}

/** Not me: that device is removed and signed out. */
export async function refuseAsking() {
  if (real()) { const a = R.pendingAsk(); if (a) await R.refuse(a.device); return }
  ready(); if (!state.asking) return
  await later()
  state.asking = undefined
  touch()
}

/** Remove one of my devices (never this one: that is Sign out). */
export async function removeDevice(id: string) {
  if (real()) return R.removeMyDevice(id)
  ready()
  const d = state.mine.find((x) => x.id === id)
  if (!d || d.here) return
  await later()
  state.mine = state.mine.filter((x) => x.id !== id)
  for (const conv of state.sealed) push(conv, { what: 'removed', who: me(), device: d.name, kind: d.kind })
  touch()
}

/** §5.2 2a: the notices to show (newest first), until "It was me" or Remove, or the device left. */
export const addedNotices = (): AddedNotice[] => (real()
  ? R.addedNotices().map((n) => { const name = sessionName(n.device) || 'A device'; return { id: n.device, name, kind: kindOf(name), at: n.at } })
  : state.notices.filter((n) => state.mine.some((d) => d.id === n.id)))
/** "It was me": the notice goes (on every tab, across restarts). */
export async function itWasMe(id: string, tell = true) {
  if (real()) return R.answerNotice(id, tell)
  state.notices = state.notices.filter((n) => n.id !== id); touch()
}
/** "Not you? Remove it": the device is removed (its session ends first, §5.6), then the notice goes. */
export async function removeAdded(id: string) {
  await removeDevice(id)
  await itWasMe(id, false) // the roster without it clears it everywhere (§5.2 2b)
}
/** Demo: NAME was added to my devices without a prompt, AGO ms ago. */
export function demoAdded(name: string, kind: DeviceKind = 'phone', ago = 0) {
  demo = true
  const id = crypto.randomUUID(), at = Date.now() - ago
  state.mine.push({ id, name, kind, added: at, from: state.mine.find((d) => d.here)?.name })
  state.notices.push({ id, name, kind, at })
  touch()
}

/** Share a conversation's history with someone's new device (any member, by this deliberate tap). */
export async function shareHistory(conv: Conv, lineId: string) {
  if (real()) { const who = lineId.split(':')[0]; return R.shareHistory(who, rumorsOf(conv) as unknown as SC.Rumor[], conv) }
  ready()
  await later()
  const l = deviceLines(conv).find((x) => x.id === lineId)
  if (l) l.shared = true
  touch()
}

function push(conv: Conv, l: Omit<DeviceLine, 'id' | 'at'> & { at?: number }) {
  const list = state.lines.get(conv) ?? state.lines.set(conv, []).get(conv)!
  list.push({ id: crypto.randomUUID(), at: Date.now(), ...l })
}

// ---- demo ------------------------------------------------------------------------------

let demo = false

export interface DemoSealed {
  mine: MyDevice[]
  people: Record<string, TheirDevices>
  sealed: Conv[]
  lines?: Record<Conv, Omit<DeviceLine, 'id'>[]>
  lockedHere?: Conv[]
  sealedAt?: Record<Conv, number>
  shared?: Record<string, string>
  unsealed?: string[]
  asking?: { name: string; kind: DeviceKind; at: number }
  linked?: { name: string; kind: DeviceKind }
}

/** Demo mode only (dm.ts demoSealedPeople fills it with the boards' people and conversations). */
export function setDemoSealed(d: DemoSealed) {
  demo = true
  state = {
    mine: d.mine, people: new Map(Object.entries(d.people)), sealed: new Set(d.sealed), lockedHere: new Set(d.lockedHere ?? []), sealedAt: new Map(Object.entries(d.sealedAt ?? {})), shared: new Map(Object.entries(d.shared ?? {})), unsealed: new Set(d.unsealed ?? []),
    lines: new Map(Object.entries(d.lines ?? {}).map(([c, ls]) => [c, ls.map((l) => ({ id: crypto.randomUUID(), ...l }))])),
    asking: d.asking && { id: crypto.randomUUID(), ...d.asking }, linked: d.linked, notices: [],
  }
  touch()
}

/** Demo: unlock this new device (as if another device allowed it). */
export function demoUnlockHere() { state.lockedHere.clear(); touch() }
/** Demo: a new device of mine asks (the prompt on this device). */
export function demoAsk(name: string, kind: DeviceKind = 'computer', full = false) { demo = true; state.full = full; state.asking = { id: crypto.randomUUID(), name, kind, at: Date.now() - 120e3 }; touch() }
/** Demo: a device came in by scanning my Link code. */
export function demoLinked(name: string, kind: DeviceKind = 'tablet') { demo = true; state.linked = { name, kind }; touch() }
// canvas v122 messages/encryption-fresh-elsewhere: my sealed conversations were started fresh on
// another device while this one held my keys
let demoElse: { name: string; at: number } | undefined
let demoElseFails = false
/** The fresh start made elsewhere, until answered: the device's name ('' when the sessions don't
 *  name it) and when (ms). */
export function freshElsewhere(): { name: string; at: number } | undefined {
  if (real()) { const e = R.freshElsewhere(); return e ? { name: sessionName(e.device), at: e.at } : undefined }
  return demoElse
}
export async function answerElsewhere() { if (real()) return R.answerElsewhere(); demoElse = undefined; touch() }
/** Every other session ends; the names of those signed out. */
export async function signOutOthers(): Promise<string[]> {
  if (real()) return R.signOutOthers()
  await later()
  if (demoElseFails) { demoElseFails = false; throw new Error(R.OTHERS_NOT_OUT) } // demo: fails once
  return state.mine.filter((d) => !d.here).map((d) => d.name)
}
export function demoFreshElsewhere(name = 'Chrome on Windows', failing = false) { demo = true; demoElseFails = failing; demoElse = { name, at: Date.now() - 60e3 }; touch() }
export const resetSealed = () => { state = empty(); demo = false; touch() }
