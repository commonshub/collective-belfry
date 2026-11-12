// Members' roles (Belfry roles.py, 2026-11-12). A role is a label and an optional line; every
// member has one, guests and agents none. Member and Admin are built in (Admin = the belfry's
// admins, the only role with powers); the rest are labels. One role is the default for new
// members (picked first on invites).
//   GET  /roles                      → {roles, default, members: {hex: id}, others?: {guest, agent}}
//   POST /roles {label, description?, default?}      (admins) → {role, roles, default}
//   POST /roles/<id> {label?, description?, default?} (admins) → {role, roles, default}
//   POST /roles/<id>/delete {}       (admins) → {deleted, moved: [hex], roles, default}
//   POST /member-roles {pubkey, role} (admins) → {pubkey, role, admins}
// Errors are shown in fixed words by code (ROLE_ERRORS), never the server's text.
import { BELFRY, getBackend } from '../config'
import { agentOwner, demoSetAdmins, loadAccessInfo } from './access'
import { isGuest } from './invite'
import { groups } from './model'
import { touch } from './store'
import { isLocalOnly, me, signedFetch } from './sync'

export interface Role { id: string; label: string; description: string; builtin: boolean; powers: boolean; default: boolean }
export const MEMBER = 'member'
export const ADMIN = 'admin'

const BUILTIN: Role[] = [
  { id: MEMBER, label: 'Member', description: 'Joins any open channel, invites guests, votes.', builtin: true, powers: false, default: true },
  { id: ADMIN, label: 'Admin', description: 'Everything, plus settings and roles.', builtin: true, powers: true, default: false },
]

let list: Role[] = BUILTIN.map((r) => ({ ...r }))
let byKey: Record<string, string> = {}
let others: { guest: number; agent: number } | null = null
let loaded = false

export const roles = () => list
export const rolesLoaded = () => loaded
export const defaultRole = () => list.find((r) => r.default)?.id ?? MEMBER
export const roleById = (id?: string) => list.find((r) => r.id === id)
/** A member's role id (undefined: a guest, an agent, or not known yet). */
export const roleOf = (pubkey: string): string | undefined => byKey[pubkey]
/** The label to show next to a name: nothing for the plain Member role, nor for guests. */
export const roleBadge = (pubkey: string): string => {
  const id = byKey[pubkey]
  if (!id || id === MEMBER) return ''
  return roleById(id)?.label ?? ''
}
export const holders = (id: string) => Object.keys(byKey).filter((k) => byKey[k] === id)
export const memberKeys = () => Object.keys(byKey)
export const otherCounts = () => others

export type RoleError = 'not_admin' | 'not_here' | 'unknown_role' | 'invalid' | 'taken' | 'builtin' | 'admin_default' | 'not_member' | 'guest_not_saved' | 'agent' | 'last_admin' | 'too_many' | 'unsupported' | 'offline' | 'failed'
export const ROLE_ERRORS: Record<RoleError, string> = {
  not_admin: 'Only admins of the belfry can do this.',
  not_here: 'Only people of this belfry can see its roles.',
  unknown_role: 'That role doesn’t exist any more.',
  invalid: 'A label of 1 to 40 characters; a description of up to 200.',
  taken: 'There is a role with that label already.',
  builtin: 'Member and Admin are built in: they can’t be deleted.',
  admin_default: 'Admin can’t be the default role.',
  not_member: 'Only members have a role.',
  guest_not_saved: 'This guest has no e-mail yet. Once they save their account, you can make them a member.',
  agent: 'An agent has no role.',
  last_admin: 'A belfry needs at least one admin.',
  too_many: 'A belfry has at most 50 roles.',
  unsupported: 'This belfry can’t keep roles yet.',
  offline: 'No connection to the belfry. Try again.',
  failed: 'That did not work. Try again.',
}
export class RoleFailed extends Error { constructor(public code: RoleError) { super(ROLE_ERRORS[code]) } }

async function call(path: string, method: 'GET' | 'POST', body?: unknown): Promise<Record<string, unknown>> {
  let r: Response
  try { r = await signedFetch(`${BELFRY.api}${path}`, method, body) } catch { throw new RoleFailed('offline') }
  const d = (await r.json().catch(() => ({}))) as Record<string, unknown>
  if (!r.ok) throw new RoleFailed((String(d.error) in ROLE_ERRORS ? d.error : 'failed') as RoleError)
  return d
}

const clean = (x: unknown): Role[] => (Array.isArray(x) ? x : [])
  .filter((r): r is Role => !!r && typeof r === 'object' && typeof (r as Role).id === 'string' && typeof (r as Role).label === 'string')
  .map((r) => ({ id: r.id, label: r.label, description: String(r.description ?? ''), builtin: !!r.builtin, powers: !!r.powers, default: !!r.default }))

function take(d: Record<string, unknown>) {
  const got = clean(d.roles)
  if (got.length) list = got
  if (d.members && typeof d.members === 'object') byKey = Object.fromEntries(Object.entries(d.members as Record<string, unknown>).filter(([k, v]) => /^[0-9a-f]{64}$/.test(k) && typeof v === 'string')) as Record<string, string>
  if (d.others && typeof d.others === 'object') others = d.others as { guest: number; agent: number }
  loaded = true
  touch()
}

let inflight: Promise<void> | null = null
/** The roles and who has which (members only; a guest gets the list). */
export function loadRoles(): Promise<void> {
  if (isLocalOnly()) { demoInit(); return Promise.resolve() }
  inflight ??= call('/roles', 'GET').then(take, () => { /* offline: what we had */ }).finally(() => { inflight = null })
  return inflight
}

export async function createRole(v: { label: string; description: string; default: boolean }): Promise<Role> {
  if (isLocalOnly()) return demoCreate(v)
  const d = await call('/roles', 'POST', v)
  take(d)
  return clean([d.role])[0]
}

export async function updateRole(id: string, v: { label?: string; description?: string; default?: boolean }): Promise<void> {
  if (isLocalOnly()) return demoUpdate(id, v)
  take(await call(`/roles/${encodeURIComponent(id)}`, 'POST', v))
}

/** Delete a role: who had it gets the default role. */
export async function deleteRole(id: string): Promise<string[]> {
  if (isLocalOnly()) return demoDelete(id)
  const d = await call(`/roles/${encodeURIComponent(id)}/delete`, 'POST', {})
  take(d)
  await loadRoles()
  return Array.isArray(d.moved) ? (d.moved as string[]) : []
}

/** Give PUBKEY a role (admin goes through the admins list; a saved guest becomes a member). */
export async function setMemberRole(pubkey: string, role: string): Promise<void> {
  if (isLocalOnly()) return demoSet(pubkey, role)
  await call('/member-roles', 'POST', { pubkey, role })
  await Promise.all([loadRoles(), loadAccessInfo()])
  touch()
}

// ---- demo (no belfry): the same rules, on this device ------------------------------------------
let demoReady = false
function demoInit() {
  if (demoReady) return
  demoReady = true
  byKey = { [me()]: MEMBER }
  others = { guest: 0, agent: 0 }
  loaded = true
}
const pause = () => new Promise((r) => setTimeout(r, 300))
const slug = (s: string) => s.normalize('NFKD').replace(/[^\w\s-]/g, '').toLowerCase().trim().replace(/[^a-z0-9]+/g, '-').slice(0, 32) || 'role'
function demoCheck(label?: string, description?: string, skip = '') {
  if (label !== undefined && (!label.trim() || label.trim().length > 40)) throw new RoleFailed('invalid')
  if (description !== undefined && description.length > 200) throw new RoleFailed('invalid')
  if (label !== undefined && list.some((r) => r.id !== skip && r.label.toLowerCase() === label.trim().toLowerCase())) throw new RoleFailed('taken')
}
const setDefault = (id: string) => { list = list.map((r) => ({ ...r, default: r.id === id })) }
async function demoCreate(v: { label: string; description: string; default: boolean }): Promise<Role> {
  await pause(); demoInit(); demoCheck(v.label, v.description)
  let id = slug(v.label); let n = 1
  while (list.some((r) => r.id === id) || ['guest', 'agent'].includes(id)) id = `${slug(v.label)}-${++n}`
  const role: Role = { id, label: v.label.trim(), description: v.description.trim(), builtin: false, powers: false, default: false }
  list = [...list.slice(0, -1), role, list[list.length - 1]]
  if (v.default) setDefault(id)
  touch()
  return role
}
async function demoUpdate(id: string, v: { label?: string; description?: string; default?: boolean }) {
  await pause(); demoCheck(v.label, v.description, id)
  if (v.default && id === ADMIN) throw new RoleFailed('admin_default')
  list = list.map((r) => (r.id === id ? { ...r, ...(v.label !== undefined ? { label: v.label.trim() } : {}), ...(v.description !== undefined ? { description: v.description.trim() } : {}) } : r))
  if (v.default === true) setDefault(id)
  else if (v.default === false && defaultRole() === id) setDefault(MEMBER)
  touch()
}
async function demoDelete(id: string): Promise<string[]> {
  await pause()
  if (id === MEMBER || id === ADMIN) throw new RoleFailed('builtin')
  if (defaultRole() === id) setDefault(MEMBER)
  const moved = holders(id)
  for (const k of moved) byKey[k] = defaultRole()
  list = list.filter((r) => r.id !== id)
  touch()
  return moved
}
async function demoSet(pubkey: string, role: string) {
  await pause()
  if (role !== ADMIN && byKey[pubkey] === ADMIN && holders(ADMIN).length <= 1) throw new RoleFailed('last_admin')
  byKey[pubkey] = role
  demoSetAdmins(holders(ADMIN))
  touch()
}

/** Demo only: make me an admin of the belfry (or not), and put PEOPLE in the members list. */
export function demoRoles(admin = true, people?: string[]) {
  if (getBackend() !== 'demo') return
  demoInit()
  // the people of the channels (not their guests) are the members
  const keys = people ?? [...new Set(groups().flatMap((g) => [...g.members]))].filter((k) => !isGuest(k) && !agentOwner(k))
  for (const p of keys) byKey[p] ??= MEMBER
  byKey[me()] = admin ? ADMIN : MEMBER
  demoSetAdmins(holders(ADMIN))
  touch()
}

/** Demo only: my own role (a member link was used). */
export function setDemoRole(role: string) { demoInit(); byKey[me()] = role; touch() }
