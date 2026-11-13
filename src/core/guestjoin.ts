// Joining from guest mode (Belfry guests.py): a name and an e-mail, then the 6-digit code the
// belfry mails to that address. Both requests are signed with the guest's identity (NIP-98).
//   POST /guest/save         {"email", "name"}           → 202; a code goes out by e-mail
//   POST /guest/save/verify  {"email", "code", "name"}   → {"saved": true, "joined"}
// The right code puts name and address on the account; the channel gets the guest's 9000 with
// the name (the join line). 409 `taken`: the address is another account's here; for a guest who
// was only reading, that account got the channel ("moved") and this one is closed: sign in.
import { BELFRY } from '../config'
import { guestInfo, setGuestInfo } from './auth'
import { guestChanged } from './guestmode'
import { addEvents, all } from './store'
import { isLocalOnly, me, signedFetch } from './sync'
import { demoRoles, setDemoRole } from './roles'

export type JoinError = 'invalid' | 'name_needed' | 'too_many' | 'wrong_code' | 'expired' | 'taken' | 'not_guest' | 'offline' | 'failed'
export class JoinFailed extends Error {
  /** retried: the request went out more than once (an answer may have been lost on the way back) */
  constructor(public code: JoinError, public moved = false, public retried = false) { super(code) }
}

/** What each answer means, in fixed words (never the server's text, never the address bar's). */
export const JOIN_ERRORS: Record<JoinError, string> = {
  invalid: 'That doesn’t look like an e-mail address.',
  name_needed: 'Your name is needed to join.',
  too_many: 'Too many codes for now. Try again in half an hour.',
  wrong_code: 'That code is not right. Check the e-mail and try again.',
  expired: 'That code has expired. Send a new one.',
  taken: 'This address already has an account here.',
  not_guest: 'This guest access has ended. Ask for a new invite.',
  offline: 'No connection. Try again.',
  failed: 'That did not work. Try again.',
}
const KNOWN = new Set(Object.keys(JOIN_ERRORS))

const wait = (ms: number) => new Promise((r) => setTimeout(r, ms))
const clean = (name: string) => name.trim().replace(/\s+/g, ' ').slice(0, 100)

async function post(path: string, body: unknown): Promise<Record<string, unknown>> {
  let r: Response | undefined
  let tries = 0
  // coming back from the mail app with the code, the phone may have dropped the connection (or
  // the signer's) while the page slept: try again for a few seconds before saying so (#issues
  // 2026-11-13, "Can't enter the 6-digit after join")
  for (let i = 0; ; i++) {
    try { tries++; r = await signedFetch(`${BELFRY.api}${path}`, 'POST', body); break } catch (e) {
      if (i >= 3) throw new JoinFailed(/timeout/i.test(String((e as Error)?.message ?? e)) ? 'failed' : 'offline')
      await wait(1000 * (i + 1))
    }
  }
  const d = (await r.json().catch(() => ({}))) as Record<string, unknown>
  if (!r.ok) throw new JoinFailed(KNOWN.has(String(d.error)) ? (d.error as JoinError) : 'failed', d.moved === true, tries > 1)
  return d
}


/** Ask for the code: it goes by e-mail to EMAIL. NAME is sent when the guest has none yet. */
export async function sendJoinCode(email: string, name?: string) {
  if (isLocalOnly()) { await wait(400); return } // demo: no mail; the code is 123456
  await post('/guest/save', { email: email.trim(), ...(name ? { name: clean(name) } : {}) })
}

/** The code from the e-mail: the account is saved under that address (and name). */
export async function verifyJoinCode(email: string, code: string, name?: string, again = false): Promise<void> {
  const g = guestInfo()
  if (isLocalOnly()) { // demo: what the belfry would do
    await wait(400)
    if (email.trim().toLowerCase() === 'taken@example.org') throw new JoinFailed('taken', true)
    if (code !== '123456') throw new JoinFailed('wrong_code')
    const hex = (n: number) => [...crypto.getRandomValues(new Uint8Array(n))].map((b) => b.toString(16).padStart(2, '0')).join('')
    if (g?.reading && name) addEvents([{ id: hex(32), pubkey: hex(32), sig: hex(64), kind: 9000, content: '', created_at: Math.floor(Date.now() / 1000),
      tags: [['h', g.channel], ['p', me(), 'member'], ['guest', ''], ['invited-by', hex(32)], ['name', clean(name)]] }])
  } else {
    let d: Record<string, unknown> = {}
    const started = Math.floor(Date.now() / 1000) - 5 // only what the belfry does from now on
    try {
      d = await post('/guest/save/verify', { email: email.trim(), code, ...(name ? { name: clean(name) } : {}) })
    } catch (e) {
      // the first try may have gone through and only its answer got lost: the code is used up
      // then, and the retry hears expired / wrong / not a guest. What the belfry did shows on the
      // relay (the saved label, or the join with the name): look before saying so.
      if (!(e instanceof JoinFailed && (e.retried || again) && ['expired', 'wrong_code', 'not_guest'].includes(e.code) && g && await savedOnRelay(g.channel, started))) throw e
    }
    if (d.member === true) return becameMember(g?.channel)
  }
  if (g?.member && isLocalOnly()) return becameMember(g.channel, g.member.role)
  if (g) setGuestInfo({ channel: g.channel, saved: true })
  guestChanged()
}

/** Did the belfry save this guest in CHANNEL (its label, or its join with a name)? Waits a few
 *  seconds for the relay to say so. */
async function savedOnRelay(channel: string, since: number): Promise<boolean> {
  for (let i = 0; i < 6; i++) {
    const mine = all(me()).filter((e) => e.created_at >= since && e.tags.some((t) => t[0] === 'h' && t[1] === channel) && e.tags.some((t) => t[0] === 'p' && t[1] === me()))
    if (mine.some((e) => (e.kind === 1985 && e.tags.some((t) => t[0] === 'L' && t[1] === 'belfry/guest') && e.tags.some((t) => t[0] === 'l' && (t[1] === 'saved' || t[1] === 'member')))
      || (e.kind === 9000 && e.tags.some((t) => t[0] === 'name') && !e.tags.some((t) => t[0] === 'reading')))) return true
    await wait(1000)
  }
  return false
}

/** A member link was used (roles.py): no guest any more. The session is the same key, now a
 *  member's; the app opens again so the relays and the belfry see it as one. */
function becameMember(channel?: string, demoRole?: string) {
  setGuestInfo(null)
  guestChanged()
  if (isLocalOnly()) { demoRoles(demoRole === 'admin', []); if (demoRole) setDemoRole(demoRole); return }
  location.replace(`${location.pathname}${location.search}#/c/${encodeURIComponent(channel ?? 'general')}`)
  location.reload()
}
