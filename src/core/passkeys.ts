// Passkeys (Belfry passkeys.py, #issues 2026-11-15): add one, and sign in with it on the belfry's
// sign-in page instead of the e-mail code. POST {api}/passkeys, NIP-98 by my key.
import { BELFRY } from '../config'
import { isLocalOnly, signedFetch } from './sync'

export interface Passkey { id: number; label: string; created: string; last_used: string }
export interface Methods { passkeys: Passkey[]; email: boolean }

export const passkeysWork = () => typeof window !== 'undefined' && !!window.PublicKeyCredential && !!navigator.credentials

const MESSAGES: Record<string, string> = {
  limit: 'You have 10 passkeys already: remove one first.', taken: 'This passkey is added already.',
  invalid: 'This passkey could not be added.', expired: 'That took too long. Try again.',
  last: 'This is your only way in: add an e-mail or another passkey first.', not_yours: 'Confirm with one of your own passkeys.',
  unknown: 'This passkey is not known here.', refused: 'This passkey could not be checked.', not_member: 'Only members add passkeys.',
  unsupported: 'Passkeys need a newer belfry.',
}
export class PasskeyFailed extends Error {}

let demo: Methods = { passkeys: [], email: true }
async function call(body: unknown): Promise<Record<string, unknown>> {
  let r: Response
  try { r = await signedFetch(`${BELFRY.api}/passkeys`, 'POST', body) } catch { throw new PasskeyFailed('No connection. Try again.') }
  const d = (await r.json().catch(() => ({}))) as Record<string, unknown>
  if (!r.ok) throw new PasskeyFailed(MESSAGES[String(d.error)] ?? 'That did not work. Try again.')
  return d
}
const clean = (d: Record<string, unknown>): Methods => ({
  email: !!d.email,
  passkeys: (Array.isArray(d.passkeys) ? d.passkeys : []).map((p: Record<string, unknown>) => ({ id: Number(p.id), label: String(p.label ?? 'Passkey').slice(0, 60), created: String(p.created ?? ''), last_used: String(p.last_used ?? '') })),
})

export async function methods(): Promise<Methods> {
  if (isLocalOnly()) return demo
  return clean(await call({}))
}

const u8 = (s: string) => Uint8Array.from(atob(s.replace(/-/g, '+').replace(/_/g, '/') + '==='.slice((s.length + 3) % 4)), (c) => c.charCodeAt(0))
const b64 = (b: ArrayBuffer) => btoa(String.fromCharCode(...new Uint8Array(b))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
const dialogError = (e: unknown) => new PasskeyFailed(e instanceof DOMException && e.name === 'NotAllowedError' ? 'The passkey was not made.' : e instanceof Error ? e.message : String(e))

/** Make a passkey on this device (the system's dialog) and add it to my account. */
export async function addPasskey(label: string): Promise<Methods> {
  if (isLocalOnly()) { demo = { ...demo, passkeys: [...demo.passkeys, { id: Date.now(), label: label || 'Passkey', created: new Date().toISOString().slice(0, 19).replace('T', ' '), last_used: '' }] }; return demo }
  const o = await call({ options: 'add' }) as Record<string, any> // eslint-disable-line @typescript-eslint/no-explicit-any
  o.challenge = u8(o.challenge)
  o.user = { ...o.user, id: u8(o.user.id) }
  o.excludeCredentials = (o.excludeCredentials ?? []).map((c: { id: string }) => ({ ...c, id: u8(c.id) }))
  let c: PublicKeyCredential
  try { c = (await navigator.credentials.create({ publicKey: o as PublicKeyCredentialCreationOptions })) as PublicKeyCredential } catch (e) { throw dialogError(e) }
  const r = c.response as AuthenticatorAttestationResponse
  return clean(await call({ add: { id: c.id, rawId: b64(c.rawId), type: c.type, response: { clientDataJSON: b64(r.clientDataJSON), attestationObject: b64(r.attestationObject), transports: r.getTransports?.() ?? [] }, clientExtensionResults: {}, authenticatorAttachment: c.authenticatorAttachment ?? null }, label }))
}

/** Remove passkey ID: one of my passkeys confirms it first (a fresh proof). */
export async function removePasskey(id: number): Promise<Methods> {
  if (isLocalOnly()) { demo = { ...demo, passkeys: demo.passkeys.filter((p) => p.id !== id) }; return demo }
  const o = await call({ options: 'confirm' }) as Record<string, any> // eslint-disable-line @typescript-eslint/no-explicit-any
  o.challenge = u8(o.challenge)
  o.allowCredentials = (o.allowCredentials ?? []).map((c: { id: string }) => ({ ...c, id: u8(c.id) }))
  let c: PublicKeyCredential
  try { c = (await navigator.credentials.get({ publicKey: o as PublicKeyCredentialRequestOptions })) as PublicKeyCredential } catch (e) { throw dialogError(e) }
  const r = c.response as AuthenticatorAssertionResponse
  return clean(await call({ remove: id, confirm: { id: c.id, rawId: b64(c.rawId), type: c.type, response: { clientDataJSON: b64(r.clientDataJSON), authenticatorData: b64(r.authenticatorData), signature: b64(r.signature), userHandle: r.userHandle ? b64(r.userHandle) : null }, clientExtensionResults: {} } }))
}

export async function renamePasskey(id: number, label: string): Promise<Methods> {
  if (isLocalOnly()) { demo = { ...demo, passkeys: demo.passkeys.map((p) => (p.id === id ? { ...p, label } : p)) }; return demo }
  return clean(await call({ rename: id, label }))
}
