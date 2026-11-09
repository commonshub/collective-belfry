// The channel drawer on a phone (the Slack/Discord "push" drawer, Material's timings):
// ☰ opens it; ☰ again (same spot, in the drawer), a tap on the page beside it, a swipe to the
// left, Esc or the phone's back gesture close it, and you are where you were. Opening adds a
// history entry, so "back" closes it instead of leaving the page; picking a channel replaces that
// entry (back then goes to where you were before, not to the drawer).
import { useSyncExternalStore } from 'react'

let open = false
const subs = new Set<() => void>()
const set = (v: boolean) => { if (open !== v) { open = v; subs.forEach((f) => f()) } }
const ours = () => !!(history.state && (history.state as { drawer?: number }).drawer)

export function openDrawer() {
  if (open) return
  history.pushState({ ...(history.state ?? {}), drawer: 1 }, '')
  set(true)
}
/** Close it; with PATH, go there instead of back to where you were. */
export function closeDrawer(path?: string) {
  if (path !== undefined) {
    if (ours()) location.replace('#/' + path)
    else location.hash = '#/' + path
    set(false)
    return
  }
  if (!open) return
  if (ours()) history.back() // popstate closes it
  else set(false)
}
export const toggleDrawer = () => (open ? closeDrawer() : openDrawer())
export const useDrawer = () => useSyncExternalStore((f) => { subs.add(f); return () => subs.delete(f) }, () => open)

addEventListener('popstate', () => { if (open && !ours()) set(false) })
addEventListener('keydown', (e) => { if (e.key === 'Escape' && open) closeDrawer() })
