// The channel drawer on a phone: ☰ opens it from the left; ☰ again (same spot) or the shade closes
// it and you are where you were. Picking a channel closes it too.
import { useSyncExternalStore } from 'react'

let open = false
const subs = new Set<() => void>()
const set = (v: boolean) => { if (open !== v) { open = v; subs.forEach((f) => f()) } }
export const toggleDrawer = () => set(!open)
export const closeDrawer = () => set(false)
export const useDrawer = () => useSyncExternalStore((f) => { subs.add(f); return () => subs.delete(f) }, () => open)
