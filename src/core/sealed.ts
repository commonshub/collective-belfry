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
import type { Conv } from './dm'
import { touch } from './store'
import { me } from './sync'

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
  /** on this (older) device: a new device of mine waits for Allow */
  asking?: { id: string; name: string; kind: DeviceKind; at: number }
  /** Link a phone or tablet: the device that just came in by scanning my code */
  linked?: { name: string; kind: DeviceKind }
}

const empty = (): State => ({ mine: [], people: new Map(), sealed: new Set(), lines: new Map(), lockedHere: new Set(), sealedAt: new Map() })
let state: State = empty()

export const isSealed = (conv: Conv) => state.sealed.has(conv)
export const myDevices = () => state.mine
export const hasDevices = () => state.mine.length > 0
export const theirDevices = (pk: string): TheirDevices | undefined => state.people.get(pk)
export const deviceLines = (conv: Conv) => state.lines.get(conv) ?? []
export const lockedHere = (conv: Conv) => state.lockedHere.has(conv)
export const anyLockedHere = () => state.lockedHere.size > 0
/** When the conversation was sealed (ms; 0 when unknown). */
export const sealedAt = (conv: Conv) => state.sealedAt.get(conv) ?? 0
/** A device not yet allowed reads NO sealed message, old or new (canvas v84): only what came
 *  before the conversation was sealed reads as usual. */
export const lockedMsg = (conv: Conv, at: number) => state.lockedHere.has(conv) && at * 1000 >= sealedAt(conv)
export const asking = () => state.asking
export const linkedByScan = () => state.linked
export const clearLinked = () => { state.linked = undefined; touch() }

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

/** Allow on an older device: the new one reads everything (its banner goes). */
export async function allowAsking() {
  ready(); const a = state.asking; if (!a) return
  await later()
  if (state.asking?.id !== a.id) return // another device answered first
  state.mine.push({ id: a.id, name: a.name, kind: a.kind, added: a.at, from: state.mine.find((d) => d.here)?.name, active: Date.now() })
  state.asking = undefined
  touch()
}

/** Not me: that device is removed and signed out. */
export async function refuseAsking() {
  ready(); if (!state.asking) return
  await later()
  state.asking = undefined
  touch()
}

/** Remove one of my devices (never this one: that is Sign out). */
export async function removeDevice(id: string) {
  ready()
  const d = state.mine.find((x) => x.id === id)
  if (!d || d.here) return
  await later()
  state.mine = state.mine.filter((x) => x.id !== id)
  for (const conv of state.sealed) push(conv, { what: 'removed', who: me(), device: d.name, kind: d.kind })
  touch()
}

/** Share a conversation's history with someone's new device (any member, by this deliberate tap). */
export async function shareHistory(conv: Conv, lineId: string) {
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
  asking?: { name: string; kind: DeviceKind; at: number }
  linked?: { name: string; kind: DeviceKind }
}

/** Demo mode only (dm.ts demoSealedPeople fills it with the boards' people and conversations). */
export function setDemoSealed(d: DemoSealed) {
  demo = true
  state = {
    mine: d.mine, people: new Map(Object.entries(d.people)), sealed: new Set(d.sealed), lockedHere: new Set(d.lockedHere ?? []), sealedAt: new Map(Object.entries(d.sealedAt ?? {})),
    lines: new Map(Object.entries(d.lines ?? {}).map(([c, ls]) => [c, ls.map((l) => ({ id: crypto.randomUUID(), ...l }))])),
    asking: d.asking && { id: crypto.randomUUID(), ...d.asking }, linked: d.linked,
  }
  touch()
}

/** Demo: unlock this new device (as if another device allowed it). */
export function demoUnlockHere() { state.lockedHere.clear(); touch() }
/** Demo: a new device of mine asks (the prompt on this device). */
export function demoAsk(name: string, kind: DeviceKind = 'computer') { demo = true; state.asking = { id: crypto.randomUUID(), name, kind, at: Date.now() - 120e3 }; touch() }
/** Demo: a device came in by scanning my Link code. */
export function demoLinked(name: string, kind: DeviceKind = 'tablet') { demo = true; state.linked = { name, kind }; touch() }
export const resetSealed = () => { state = empty(); demo = false; touch() }
