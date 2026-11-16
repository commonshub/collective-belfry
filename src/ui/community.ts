// The community's name (belfry.json "name"), asked once and kept on the device, so every place
// shows the same name from the first paint; FALLBACK only until it was ever known.
import { useEffect, useState } from 'react'
import { BELFRY, THEME } from '../config'

const KEY = 'belfry-community-name'
let communityName = (() => { try { return localStorage.getItem(KEY) ?? '' } catch { return '' } })()
let asked = false
const listeners = new Set<() => void>()
export function useCommunityName(fallback: string) {
  const [, bump] = useState(0)
  useEffect(() => {
    const l = () => bump((n) => n + 1)
    listeners.add(l)
    if (!asked && THEME === 'belfry') {
      asked = true
      fetch(`https://${BELFRY.domain}/.well-known/belfry.json`).then((r) => r.json()).then((j) => {
        if (typeof j?.name !== 'string' || !j.name.trim()) return
        communityName = j.name.trim().slice(0, 60)
        try { localStorage.setItem(KEY, communityName) } catch { /* private mode */ }
        listeners.forEach((f) => f()) // every component showing it, not only the one that asked
      }).catch(() => {})
    }
    return () => { listeners.delete(l) }
  }, [])
  return communityName || fallback
}
