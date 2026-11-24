// A channel's notifications, one choice of three (Xavier, 2026-11-18, canvas "Notif-C"):
//   Everything       → push level "all"
//   When it's for me → "default" (mentions, replies, announcements, pinned messages; a channel the
//                      phone set to "mentions" shows here too)
//   Nothing          → "off"
// The levels are the belfry's push settings (docs/push.md §3: POST /api/v1/notifications, NIP-98;
// {} reads, {"set": {"channels": {<group id>: level}}} merges one channel). They are kept per person
// by the belfry and apply to every phone where they use the Belfry app; the web has no push of its
// own. Demo mode keeps them on this device.

import { useEffect, useState } from 'react'
import { BELFRY } from '../config'
import { isLocalOnly, signedFetch } from './sync'

export type Level = 'all' | 'default' | 'mentions' | 'off'
export type Choice = 'all' | 'mine' | 'off'

interface State {
  /** the belfry keeps no push settings (with Odoo: 409 unsupported) */
  unsupported: boolean
  channels: Record<string, Level>
  /** phones registered for push (only their count matters here) */
  devices: number
}

const KEY = 'belfry-notify-demo'
let cache: State | null = null
let loading: Promise<void> | null = null
let failed = ''
const subs = new Set<() => void>()
const changed = () => subs.forEach((f) => f())

export const choiceOf = (l: Level | undefined): Choice => (l === 'all' ? 'all' : l === 'off' ? 'off' : 'mine')
const levelOf = (c: Choice): Level => (c === 'all' ? 'all' : c === 'off' ? 'off' : 'default')

async function load() {
  failed = ''
  try {
    if (isLocalOnly()) {
      let saved: Record<string, Level> = {}
      try { saved = JSON.parse(localStorage.getItem(KEY) || '{}') } catch { /* nothing kept */ }
      cache = { unsupported: false, channels: saved, devices: 0 }
      return
    }
    const r = await signedFetch(`${BELFRY.api}/notifications`, 'POST', {})
    const d = await r.json().catch(() => ({}))
    if (r.status === 409 && d.error === 'unsupported') { cache = { unsupported: true, channels: {}, devices: 0 }; return }
    if (!r.ok) throw new Error(`The belfry answered ${r.status}.`)
    let devices = 0
    try {
      const dv = await signedFetch(`${BELFRY.api}/push/devices`, 'POST', {})
      if (dv.ok) devices = ((await dv.json()).devices ?? []).length
    } catch { /* unknown: say nothing about phones */ devices = -1 }
    cache = { unsupported: false, channels: d.channels ?? {}, devices }
  } catch (e) {
    failed = e instanceof Error ? e.message : String(e)
  } finally {
    loading = null
    changed()
  }
}

/** The settings (null while loading) and why they could not be read. */
export function useNotify(): { state: State | null; failed: string } {
  const [, bump] = useState(0)
  useEffect(() => {
    const f = () => bump((n) => n + 1)
    subs.add(f)
    if (!cache && !loading) loading = load()
    return () => { subs.delete(f) }
  }, [])
  return { state: cache, failed }
}

/** Set CHANNEL to CHOICE: shown at once, put back if the belfry refuses. */
export async function setChoice(channel: string, choice: Choice) {
  if (!cache) return
  const before = cache.channels[channel]
  const level = levelOf(choice)
  const apply = (l: Level | undefined) => {
    const next = { ...cache!.channels }
    if (!l || l === 'default') delete next[channel]; else next[channel] = l
    cache = { ...cache!, channels: next }
    changed()
  }
  apply(level)
  if (isLocalOnly()) {
    try { localStorage.setItem(KEY, JSON.stringify(cache.channels)) } catch { /* this visit only */ }
    return
  }
  try {
    const r = await signedFetch(`${BELFRY.api}/notifications`, 'POST', { set: { channels: { [channel]: level } } })
    const d = await r.json().catch(() => ({}))
    if (!r.ok) throw new Error('Not saved. Try again.')
    cache = { ...cache!, channels: d.channels ?? cache!.channels }
    changed()
  } catch (e) {
    apply(before)
    throw e instanceof Error ? e : new Error(String(e))
  }
}
