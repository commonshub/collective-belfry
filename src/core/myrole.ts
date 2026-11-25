// What I am on this belfry, from the belfry itself (POST /api/v1/whois for my key: member · guest ·
// agent), never from the event history alone (a member who once came in through a card or a guest
// link keeps a guest join in that channel's history: Xavier on belfry.brussels, 25 Nov, saw the guest
// Today, the Apply card and the guest bar while he is a steward). Asked at start and after a sign-in;
// the last answer is kept per key so the first paint is right too. A member's stale guest-link info
// (localStorage) goes once whois says member.
import { guestInfo, setGuestInfo } from './auth'
import { touch } from './store'
import { isLocalOnly, me, signedFetch } from './sync'
import { BELFRY } from '../config'

export type Role = 'member' | 'guest' | 'agent'
const KEY = (pk: string) => 'belfry-role:' + pk
let role: { pk: string; role: Role } | null = null

/** My role on this belfry as it last answered, or undefined (not asked yet, offline the first time). */
export function myRole(): Role | undefined {
  const pk = me()
  if (!pk) return undefined
  if (role?.pk === pk) return role.role
  try { const v = localStorage.getItem(KEY(pk)); if (v === 'member' || v === 'guest' || v === 'agent') { role = { pk, role: v }; return v } } catch { /* private mode */ }
  return undefined
}

/** Ask the belfry again (start, sign-in, back to the app). Nothing changes when it can't answer. */
export async function refreshMyRole(): Promise<void> {
  const pk = me()
  if (!pk || isLocalOnly()) return
  let v: unknown
  try {
    const r = await signedFetch(`${BELFRY.api}/whois`, 'POST', { pubkeys: [pk] })
    if (!r.ok) return
    const d = (await r.json().catch(() => null)) as { people?: Record<string, unknown> } | null
    v = d?.people?.[pk]
  } catch { return }
  if (v !== 'member' && v !== 'guest' && v !== 'agent') return
  applyRole(v)
}

/** The belfry's answer for my key: kept, and a member's leftover guest-link info dropped (the demo's
 *  window.demoRole drives this too). */
export function applyRole(v: Role) {
  const pk = me()
  if (!pk) return
  let changed = role?.pk !== pk || role.role !== v
  role = { pk, role: v }
  try { localStorage.setItem(KEY(pk), v) } catch { /* private mode */ }
  if (v === 'member' && guestInfo()) { setGuestInfo(null); changed = true } // (a guest link's info from before: not mine any more)
  if (changed) touch()
}
