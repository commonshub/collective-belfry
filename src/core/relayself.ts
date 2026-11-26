// The groups relay's own key (NIP-11 `self`): the only signer of group metadata (39000–39003) this app
// believes (the lead, 26 Nov: the member list decides who gets a private group's key; it must not rest on the
// relay never storing a member-published 39002). NIP-11 `pubkey` is the operator, not this key. Read once per
// relay URL and kept; unreadable or no `self`: metadata unknown, and private-group writes hold.
import { BELFRY } from '../config'
import { kvGet, kvSet } from './db'
import { isLocalOnly } from './sync'
import { touch } from './store'

let self: string | undefined
let loaded = false
const KEY = () => 'belfry-groups-self:' + BELFRY.groupsRelay

/** The groups relay's own key, once known (cached), else undefined. */
export const relaySelf = () => self
/** Metadata is pinned to the relay's key here (the demo, with no relay, needs none). */
export const metadataPinned = () => isLocalOnly() || !!self

/** Read it (the cached one first, then the relay's NIP-11 document). */
export async function loadRelaySelf(): Promise<void> {
  if (isLocalOnly()) return
  if (!loaded) { loaded = true; const c = await kvGet<string>(KEY()).catch(() => undefined); if (typeof c === 'string' && /^[0-9a-f]{64}$/.test(c)) { self = c; touch() } }
  try {
    const url = BELFRY.groupsRelay.replace(/^ws(s?):\/\//, 'http$1://')
    const r = await fetch(url, { headers: { Accept: 'application/nostr+json' } })
    if (!r.ok) return
    const d = (await r.json().catch(() => null)) as { self?: unknown } | null
    if (typeof d?.self === 'string' && /^[0-9a-f]{64}$/.test(d.self) && d.self !== self) { self = d.self; await kvSet(KEY(), self).catch(() => {}); touch() }
  } catch { /* offline: the cached one, or unknown */ }
}
