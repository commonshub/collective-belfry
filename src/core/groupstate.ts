// Is a group there, archived, or deleted? (Xavier, 2026-11-16)
//
//   deleted   a kind 9008 for it is known (the relay drops everything of a deleted group but its
//             9008), or the groups relay answered a question about this very group to the end
//             (EOSE) with nothing: no 9007, 9002, 39000. That second case is also a group whose
//             creation never reached the relay while its card did.
//   unknown   neither the group's own metadata nor that answer is here yet (first load, offline):
//             nothing is said on a guess.
//   archived  the latest 9002 that says so carries ["archived", "1"] (model.ts).
//
// The live subscription can't answer "is there nothing": it is one filter with a limit over every
// kind, so an old group's metadata may simply not be in it. Hence the question per group.
import { useEffect } from 'react'
import { guestInfo } from './auth'
import { reading } from './guestmode'
import { archivable, deletedGroup, group } from './model'
import { addEvents, touch, useVersion } from './store'
import { isGuest } from './invite'
import { isLocalOnly, me, queryComplete, relayAuthed } from './sync'

export type GroupState = 'live' | 'archived' | 'deleted' | 'unknown'

// what the relay said it does not have: id → when it said so (kept on this device, so a deleted
// group's card still says so offline; the group's own events, if they ever come, win over it)
// (v2, 2026-11-18: notes written before that may come from an answer given before the relay had
// signed us in, which shows nothing members-only: they are dropped)
const KEY = 'belfry-groups-absent-v2'
try { localStorage.removeItem('belfry-groups-absent') } catch { /* private mode */ }
const absent = new Map<string, number>()
try { for (const [id, at] of Object.entries(JSON.parse(localStorage.getItem(KEY) ?? '{}') as Record<string, number>)) absent.set(id, Number(at) || 0) } catch { /* nothing kept */ }
const keep = () => { try { localStorage.setItem(KEY, JSON.stringify(Object.fromEntries([...absent].slice(-200)))) } catch { /* private mode */ } }

/** The relay shows a channel's guest only their own channels: its "nothing" proves nothing. */
const partialView = () => reading() || !!guestInfo() || isGuest(me())

export function groupState(id: string): GroupState {
  if (deletedGroup(id)) return 'deleted'
  const g = group(id)
  if (g?.known) return g.archived ? 'archived' : 'live'
  // events and proposals have their own cancel / status; #general is made by the first member in
  if (!archivable(id)) return g ? 'live' : 'unknown'
  // demo mode: this device's log is all there is
  if (isLocalOnly()) return g ? 'live' : 'deleted'
  if (absent.has(id) && !partialView()) return 'deleted'
  return g ? 'live' : 'unknown'
}

const asked = new Map<string, number>() // id → when the relay was last asked
const tries = new Map<string, number>() // id → questions in a row it did not answer
const AGAIN_ABSENT = 5 * 60_000, TRIES = 6

/** Ask the groups relay about this group, unless it was asked a moment ago. No answer (not
 *  connected or signed in to it yet, a timeout): asked again a few times, further and further
 *  apart, then left unknown until a screen asks again. */
export function checkGroup(id: string): void {
  if (isLocalOnly() || partialView() || !archivable(id) || !/^[a-z0-9_-]{1,64}$/.test(id)) return
  const now = Date.now()
  if (asked.has(id) && now - asked.get(id)! < (absent.has(id) ? AGAIN_ABSENT : 10_000)) return
  asked.set(id, now)
  const signedIn = relayAuthed('groups')
  void queryComplete('groups', [{ kinds: [39000], '#d': [id] }, { kinds: [9007, 9002, 9008], '#h': [id] }]).then((found) => {
    if (found === null) { // no answer: still unknown
      const n = (tries.get(id) ?? 0) + 1
      tries.set(id, n)
      asked.delete(id)
      if (n < TRIES) setTimeout(() => { if (!group(id)?.known && !deletedGroup(id)) checkGroup(id) }, 1500 * 2 ** (n - 1))
      else tries.delete(id)
      return
    }
    tries.delete(id)
    if (found.length) {
      addEvents(found) // its metadata (older than what the live subscription reached), or its 9008
      if (absent.delete(id)) keep()
    } else if (signedIn && relayAuthed('groups')) {
      // nothing, from a relay that had signed us in before and after the question: it isn't there
      absent.set(id, now)
      keep()
    } else {
      // asked before the sign-in: the answer proves nothing; ask again in a moment
      asked.delete(id)
      setTimeout(() => { if (!group(id)?.known && !deletedGroup(id)) checkGroup(id) }, 2000)
    }
    touch()
  })
}

/** A group's state, asking the relay when this device has nothing of the group's own. */
export function useGroupState(id?: string): GroupState {
  useVersion()
  const state = id ? groupState(id) : 'live'
  const known = !!(id && group(id)?.known)
  useEffect(() => {
    // nothing of the group's own here (even with messages of it: its metadata may be older than
    // what the live subscription reached): ask
    if (id && !known && !deletedGroup(id)) checkGroup(id)
  }, [id, state, known])
  return state
}
