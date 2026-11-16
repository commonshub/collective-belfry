// A guest asks to become a member from inside the app (board Join-Apply; Belfry membership.py,
// docs/membership.md "A guest applies"). NIP-98 by the guest's own key:
//   POST /applications {"apply": {"answer", "agreed"?: [<2100 id>]}} → {application, until, accept}
//   POST /applications {"apply": "status"}                          → the same (application: null)
// application = {id, state: new|invited|declined|accepted, vouches, needed, vouched_by: [{pubkey, name}]}
// until = the end of the guest's latest guest pass (unix); it doesn't run out while a request is open.
// Errors are shown in fixed words by code, never the server's text.
import { BELFRY } from '../config'
import { touch } from './store'
import { isLocalOnly, signedFetch } from './sync'

export interface MyApplication { id: number; state: string; vouches: number; needed: number; vouchedBy: { pubkey: string; name: string }[] }
export interface ApplyStatus { application: MyApplication | null; until?: number; accept: 'admins' | 'vouches' }

export type ApplyError = 'guest_not_saved' | 'not_guest' | 'pending' | 'invalid' | 'unsupported' | 'offline' | 'failed'
export const APPLY_ERRORS: Record<ApplyError, string> = {
  guest_not_saved: 'Save your e-mail first: the stewards need a way to reach you.',
  not_guest: 'Only guests apply from here.',
  pending: 'Your request is waiting already.',
  invalid: 'Write a few words (up to 2000 characters).',
  unsupported: 'This belfry can’t take a guest’s request from the app yet. Ask the person who invited you, or a steward.',
  offline: 'No connection to the belfry. Try again.',
  failed: 'That did not work. Try again.',
}
export class ApplyFailed extends Error { constructor(public code: ApplyError) { super(APPLY_ERRORS[code]) } }

let status: ApplyStatus | null = null
let loaded = false
export const applyStatus = () => status
export const applyLoaded = () => loaded

function take(d: Record<string, unknown>): ApplyStatus {
  const a = d.application as Record<string, unknown> | null | undefined
  const app: MyApplication | null = a && typeof a === 'object' ? {
    id: Number(a.id) || 0, state: String(a.state ?? 'new'), vouches: Number(a.vouches) || 0, needed: Number(a.needed) || 0,
    vouchedBy: (Array.isArray(a.vouched_by) ? a.vouched_by : []).filter((v): v is { pubkey: string; name: string } => !!v && typeof v === 'object' && typeof (v as { pubkey?: unknown }).pubkey === 'string')
      .map((v) => ({ pubkey: v.pubkey, name: String(v.name ?? '') })),
  } : null
  return { application: app, until: typeof d.until === 'number' ? d.until : undefined, accept: d.accept === 'vouches' ? 'vouches' : 'admins' }
}

async function call(body: unknown): Promise<ApplyStatus> {
  let r: Response
  try { r = await signedFetch(`${BELFRY.api}/applications`, 'POST', body) } catch { throw new ApplyFailed('offline') }
  const d = (await r.json().catch(() => ({}))) as Record<string, unknown>
  if (r.status === 409 && d.error === 'pending') return take(d)
  if (r.status === 404 || r.status === 405) throw new ApplyFailed('unsupported') // an older belfry: no such call
  if (!r.ok) throw new ApplyFailed((String(d.error) in APPLY_ERRORS ? d.error : 'failed') as ApplyError)
  return take(d)
}

// demo: what the belfry would answer (window.demoApplyStatus changes it)
let demo: ApplyStatus = { application: null, until: Math.floor(Date.now() / 1000) + 7 * 86400, accept: 'vouches' }

/** The guest's request (if any) and how long they stay. */
export async function loadApplyStatus(): Promise<ApplyStatus | null> {
  try { status = isLocalOnly() ? demo : await call({ apply: 'status' }) } catch { /* keeps what it had */ }
  loaded = true
  touch()
  return status
}

/** Send the request: the answer, and the agreements ticked on the way. */
export async function sendApplication(answer: string, agreed: string[]): Promise<ApplyStatus> {
  if (isLocalOnly()) {
    await new Promise((r) => setTimeout(r, 250))
    demo = { ...demo, application: { id: 1, state: 'new', vouches: 0, needed: demo.accept === 'vouches' ? 2 : 0, vouchedBy: [] } }
    status = demo
  } else status = await call({ apply: { answer: answer.trim().slice(0, 2000), agreed } })
  loaded = true
  touch()
  return status
}

/** Demo only: the state of my request as the belfry would tell it. */
export function demoApplyStatus(patch: Partial<ApplyStatus> & { vouchedBy?: string[]; state?: string }) {
  const app = demo.application ?? { id: 1, state: 'new', vouches: 0, needed: 2, vouchedBy: [] }
  const vb = patch.vouchedBy?.map((name, i) => ({ pubkey: String(i).repeat(64).slice(0, 64), name }))
  demo = { ...demo, ...('until' in patch ? { until: patch.until } : {}), ...(patch.accept ? { accept: patch.accept } : {}),
    application: patch.application === null ? null : { ...app, ...(patch.state ? { state: patch.state } : {}), ...(vb ? { vouchedBy: vb, vouches: vb.length } : {}) } }
  void loadApplyStatus()
}
