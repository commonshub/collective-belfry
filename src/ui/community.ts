// The community's name (belfry.json "name"), asked once; FALLBACK until it is known.
import { useEffect, useState } from 'react'
import { BELFRY, THEME } from '../config'

let communityName = ''
let asked = false
export function useCommunityName(fallback: string) {
  const [, bump] = useState(0)
  useEffect(() => {
    if (asked || THEME !== 'belfry') return
    asked = true
    fetch(`https://${BELFRY.domain}/.well-known/belfry.json`).then((r) => r.json()).then((j) => {
      if (typeof j?.name === 'string' && j.name.trim()) { communityName = j.name.trim().slice(0, 60); bump((n) => n + 1) }
    }).catch(() => {})
  }, [])
  return communityName || fallback
}
