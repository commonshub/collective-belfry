// Names never known yet are asked for (to-do bbd452d5, 2026-11-19): each key a screen wants a name
// for and the profiles don't have is batched, and its kind 0 asked on the community relay and the
// groups relay. While asked: "loading" (a grey bar where a name stands alone, PersonName); after:
// "Somebody" / "An agent" (model.ts fallbackName), never a shortened npub.
import { addEvents, touch } from './store'
import { isLocalOnly, queryOnce, queryAuthed } from './sync'
import { onMissingName, profiles } from './model'

const asked = new Map<string, number>() // pubkey → when its kind 0 was last asked (ms)
const waiting = new Set<string>() // asked, no answer yet
let queue = new Set<string>()
let timer: ReturnType<typeof setTimeout> | null = null
const AGAIN = 10 * 60_000 // a key with no profile is asked again after ten minutes

function want(pubkey: string) {
  if (!/^[0-9a-f]{64}$/.test(pubkey)) return
  const at = asked.get(pubkey)
  if (at && Date.now() - at < AGAIN) return
  asked.set(pubkey, Date.now())
  waiting.add(pubkey)
  queue.add(pubkey)
  timer ??= setTimeout(flush, 150)
}

async function flush() {
  timer = null
  const keys = [...queue]
  queue = new Set()
  if (!keys.length) return
  if (isLocalOnly()) { setTimeout(() => { keys.forEach((k) => waiting.delete(k)); touch() }, 400); return } // demo mode: no relays
  for (let i = 0; i < keys.length; i += 100) {
    const authors = keys.slice(i, i + 100)
    const found = await Promise.all((['main', 'groups'] as const).map((t) => queryAuthed(t, { kinds: [0], authors }, 6000).catch(() => [])))
    addEvents(found.flat())
    authors.forEach((k) => waiting.delete(k))
    touch()
  }
}

/** Is a name for PUBKEY still being asked for? (a grey bar meanwhile) */
export const nameLoading = (pubkey: string) => waiting.has(pubkey) && !profiles().get(pubkey)?.name

let started = false
export function startNames() {
  if (started) return
  started = true
  onMissingName(want)
}
