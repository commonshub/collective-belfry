// The right code for an address that has an account (board onboarding/guest-code v41, docs/
// guests.md "An address that has an account", vectors/guest-signin.json): the belfry made a
// session on that account for a new key of this device. The guest session and its key go (ended
// in the signer unless the belfry closed the guest itself), the new one is stored like any
// sign-in, and the channel opens with "Signed in as <address>" for 3 s. No second address, no
// second code. Used by both Join sheets (guest mode, and a named guest saving an e-mail).
import { clearSession, endSession, forgetDevice, loadSession, saveSession, setGuestInfo } from './auth'
import { wipe } from './db'
import type { SignedIn } from './guestjoin'
import { closeJoin } from './guestmode'
import { stop } from './sync'

/** sessionStorage: the address to say once after the reload (not a belfry- key: forgetDevice keeps it) */
export const SIGNED_IN_AS = 'signed-in-as'

export async function switchToSignedIn(r: SignedIn) {
  const s = await loadSession()
  if (s?.backend === 'belfry' && !r.moved) await endSession(s)
  stop()
  setGuestInfo(null)
  closeJoin()
  await clearSession()
  await wipe()
  forgetDevice()
  await saveSession(r.session)
  try { sessionStorage.setItem(SIGNED_IN_AS, r.address) } catch { /* private mode */ }
  location.hash = r.open ? `#/c/${encodeURIComponent(r.open)}` : '#/home'
  location.reload()
  await new Promise(() => { /* the page is leaving: the boxes stay locked */ })
}

/** The address to say, once (read and forgotten). */
export function takeSignedInAs(): string {
  try { const a = sessionStorage.getItem(SIGNED_IN_AS) ?? ''; sessionStorage.removeItem(SIGNED_IN_AS); return a } catch { return '' }
}
