// "Forward all e-mails to" an outside address (Belfry mailforward.py, docs/mail.md "Forwarding").
// The address is the belfry's, never in an event: read and set through the API, nothing cached.
//   POST /mail-forward {channel}                      → {forward}
//   POST /mail-forward {channel, address: "x@y"|null} → {forward}  (set: pending until confirmed)
//   POST /mail-forward {channel, resend: true}        → {forward}
// forward = {address, state: "pending"|"on", set_by, since, same_domain_skipped} | null
// Errors are shown in fixed words by code (FORWARD_ERRORS), never the server's text.
import { BELFRY } from '../config'
import { isLocalOnly, me, signedFetch } from './sync'

export interface Forward { address: string; state: 'pending' | 'on'; setBy: string; since: number; sameDomainSkipped: boolean }

export type ForwardError = 'not_allowed' | 'unknown_channel' | 'invalid' | 'own_domain' | 'archived' | 'not_pending' | 'too_many' | 'no_mail' | 'unsupported' | 'offline' | 'failed'
export const FORWARD_ERRORS: Record<ForwardError, string> = {
  not_allowed: 'Only the channel’s admins can change this.',
  unknown_channel: 'This channel doesn’t exist on the belfry.',
  invalid: 'That is not an e-mail address.',
  own_domain: `Forward to an address outside ${BELFRY.domain}.`,
  archived: 'This channel is archived. Unarchive it first.',
  not_pending: 'Nothing is waiting for a confirmation.',
  too_many: 'Three confirmation e-mails were sent this hour. Try again later.',
  no_mail: 'This belfry can’t send e-mail yet: set up its mail provider first.',
  unsupported: 'This belfry can’t forward e-mails yet.',
  offline: 'No connection to the belfry. Try again.',
  failed: 'That did not work. Try again.',
}
export class ForwardFailed extends Error { constructor(public code: ForwardError) { super(FORWARD_ERRORS[code]) } }

function take(d: Record<string, unknown>): Forward | null {
  const f = d.forward as Record<string, unknown> | null | undefined
  if (!f || typeof f !== 'object' || typeof f.address !== 'string') return null
  return { address: f.address, state: f.state === 'on' ? 'on' : 'pending', setBy: String(f.set_by ?? ''), since: Number(f.since ?? 0), sameDomainSkipped: !!f.same_domain_skipped }
}

async function call(body: Record<string, unknown>): Promise<Forward | null> {
  if (isLocalOnly()) return demo(body)
  let r: Response
  try { r = await signedFetch(`${BELFRY.api}/mail-forward`, 'POST', body) } catch { throw new ForwardFailed('offline') }
  const d = (await r.json().catch(() => ({}))) as Record<string, unknown>
  if (!r.ok) throw new ForwardFailed((String(d.error) in FORWARD_ERRORS ? d.error : 'failed') as ForwardError)
  return take(d)
}

export const loadForward = (channel: string) => call({ channel })
export const setForward = (channel: string, address: string | null) => call({ channel, address })
export const resendForward = (channel: string) => call({ channel, resend: true })

// ---- demo (no belfry): the same states on this device; the test confirms with
// window.demoMailForward.confirm(channel) --------------------------------------------------------
const PUBLIC = new Set('gmail.com googlemail.com outlook.com hotmail.com live.com msn.com yahoo.com ymail.com icloud.com me.com mac.com proton.me protonmail.com protonmail.ch pm.me gmx.com gmx.net gmx.de aol.com fastmail.com fastmail.fm mail.ru yandex.com yandex.ru zoho.com'.split(' '))
const demoState = new Map<string, Forward>()
const listeners = new Set<() => void>()
export const onDemoForward = (f: () => void) => { listeners.add(f); return () => { listeners.delete(f) } }

function demo(body: Record<string, unknown>): Forward | null {
  const channel = String(body.channel ?? '')
  const cur = demoState.get(channel) ?? null
  if ('address' in body) {
    const a = body.address
    if (a === null || a === '') { demoState.delete(channel); return null }
    const address = String(a).trim()
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(address)) throw new ForwardFailed('invalid')
    const dom = address.split('@')[1].toLowerCase()
    if (dom === BELFRY.domain || dom.endsWith('.' + BELFRY.domain)) throw new ForwardFailed('own_domain')
    if (cur && cur.state === 'on' && cur.address.toLowerCase() === address.toLowerCase()) return cur
    const f: Forward = { address, state: 'pending', setBy: me(), since: Math.floor(Date.now() / 1000), sameDomainSkipped: !PUBLIC.has(dom) }
    demoState.set(channel, f)
    return f
  }
  if (body.resend) { if (!cur || cur.state !== 'pending') throw new ForwardFailed('not_pending'); return cur }
  return cur
}

if (typeof window !== 'undefined') {
  ;(window as unknown as { demoMailForward: unknown }).demoMailForward = {
    confirm(channel: string) {
      const f = demoState.get(channel)
      if (f) { demoState.set(channel, { ...f, state: 'on' }); listeners.forEach((l) => l()) }
      return !!f
    },
  }
}
