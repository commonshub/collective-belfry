// Guest mode (Belfry guests.py, 2026-11-12): someone who opened a guest link reads its channel at
// once, with no name. Nothing they do is sent until they join (a name, an e-mail and the code
// mailed to it): the composer's place says "Guest mode" with a Join button, and every write
// (sync.ts enqueue / signedFetch) opens the same Join sheet instead.
// The relay does not enforce this: it takes a write from such a key like from any guest.
import { useSyncExternalStore } from 'react'
import { guestInfo } from './auth'

const listeners = new Set<() => void>()
let v = 0
const bump = () => { v++; listeners.forEach((l) => l()) }
const subscribe = (l: () => void) => { listeners.add(l); return () => { listeners.delete(l) } }
/** Re-render when guest mode, the Join sheet or the opening screen change. */
export const useGuestMode = () => useSyncExternalStore(subscribe, () => v)

/** This device is a guest who only opened a link so far: reading, nothing is sent. */
export const reading = () => guestInfo()?.reading === true
/** Guest mode changed (joined): views read it again. */
export const guestChanged = () => bump()

export const GUEST_MODE = 'Join to do this: you are reading as a guest.'

// ---- the Join sheet ---------------------------------------------------------------------------
let asking = false
export const joinAsked = () => asking
/** Something that writes was tapped in guest mode: open the Join sheet. */
export function askToJoin() { if (!asking) { asking = true; bump() } }
export function closeJoin() { if (asking) { asking = false; bump() } }

// ---- one loading screen from the link to the channel -------------------------------------------
// App shows it from the moment the link is read until the channel can be drawn in full (session
// made, key reached, the relay's stored events in): nothing else is rendered in between.
export interface Opening { channel?: string; name?: string }
let opening: Opening | null = null
export const openingNow = () => opening
export function setOpening(o: Opening | null) {
  if (!o && !opening) return
  opening = o
  bump()
}
