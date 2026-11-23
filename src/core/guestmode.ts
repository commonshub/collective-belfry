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

// ---- a topic reached through a card (Xavier, 23 Nov; the lead's approved list) -----------------
// Its reading guest may post a plain message (text, links, photos) in that topic only, before giving
// an e-mail. The topic is the one the card screen recorded (core/cards.ts 'belfry-card-topic').
const cardTopicNow = (): string => { try { const v = localStorage.getItem('belfry-card-topic') ?? ''; return /^[A-Za-z0-9_-]{1,120}$/.test(v) ? v : '' } catch { return '' } }
/** Reading in GROUP, which this browser reached through a topic card: the join answer said
 *  "card": "topic" (Belfry c986136), or else what the card screen recorded. */
export const guestTopic = (group: string) => { const g = guestInfo(); return reading() && !!group && g?.channel === group && (g.card ? g.card === 'topic' : cardTopicNow() === group) }
/** The approved list, exactly: kind 9 to the groups relay whose tags are [["h", T]] plus imeta (its
 *  photos, images only), T being that topic. Nothing else a reading guest writes goes. */
export function guestTopicPost(target: string, t: { kind: number; tags: string[][] }, files: { type: string }[] = []): boolean {
  if (target !== 'groups' || t.kind !== 9) return false
  const h = t.tags.filter((x) => x[0] === 'h')
  if (h.length !== 1 || !guestTopic(h[0][1])) return false
  return t.tags.every((x) => x[0] === 'h' || x[0] === 'imeta') && files.every((f) => f.type.startsWith('image/'))
}
