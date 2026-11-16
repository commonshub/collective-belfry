// Roles in a group, ticket codes and checking people in (Belfry checkin.py, docs/checkin.md,
// Xavier 2026-11-15).
//
//   admin     the relay's 39001, the creator, and anyone put in with ["p", k, "admin"] (model.ts
//             folds them into Group.admins). In an event: the creator is the Host, the others
//             Co-hosts (the belfry's admins and the relay's keys administer every group: not
//             shown as co-hosts).
//   steward   events only: 1985 L belfry/group-role, l steward | none, p, h, by the groups key.
//   member    everyone else (an event's "Attendee").
//   checked in  1985 L belfry/ticket, l checked-in | not-checked-in, p, h, by the groups key.
//
//   POST /group-roles {channel, pubkey, role}            → {role}
//   POST /events/<d>/attendees {}                         → {people, going, checked_in}
//   POST /events/<d>/checkin {code | pubkey, undo?}       → {person, already}
// Errors are shown in fixed words by code, never the server's text.
import { sha256 } from '@noble/hashes/sha256'
import { BELFRY } from '../config'
import { belfryAdminKeys, isAdmin } from './access'
import { knownGroupsKey } from './dm'
import { displayName, group, profiles, type CalEvent } from './model'
import { addEvents, all, touch, type Ev } from './store'
import { enqueue, isLocalOnly, me, signedFetch } from './sync'

export const ROLE_NS = 'belfry/group-role'
export const TICKET_NS = 'belfry/ticket'
export type GroupRole = 'admin' | 'steward' | 'member'
export type EventRole = 'host' | 'cohost' | 'steward'

const tag = (e: Ev, k: string) => e.tags.find((t) => t[0] === k)?.[1]
const isEventGroup = (id: string) => id.startsWith('ev-') || id.startsWith('ws-')

/** The groups key: only its labels count (demo: the demo groups key). */
const signerKey = () => knownGroupsKey()

/** group id → pubkey → the latest label of NS by the groups key ({v, at}). */
const labels = (ns: string) => {
  let key: Ev[] | undefined, keyGk: string | undefined, val = new Map<string, Map<string, { v: string; at: number }>>()
  return () => {
    const list = all(me()), gk = signerKey()
    if (list === key && gk === keyGk) return val
    key = list; keyGk = gk; val = compute(list, gk)
    return val
  }
  function compute(list: Ev[], gk?: string) {
  const out = new Map<string, Map<string, { v: string; at: number }>>()
  for (const e of [...list].sort((a, b) => a.created_at - b.created_at)) {
    if (e.kind !== 1985 || tag(e, 'L') !== ns || !gk || e.pubkey !== gk) continue
    const h = tag(e, 'h'), p = tag(e, 'p')
    const v = e.tags.find((t) => t[0] === 'l' && t[2] === ns)?.[1]
    if (!h || !p || !v) continue
    const m = out.get(h) ?? new Map()
    m.set(p, { v, at: e.created_at })
    out.set(h, m)
  }
  return out
  }
}
const roleLabels = labels(ROLE_NS)
const ticketLabels = labels(TICKET_NS)

export const isSteward = (groupId: string, pk: string) => isEventGroup(groupId) && roleLabels().get(groupId)?.get(pk)?.v === 'steward'
/** When PK was checked in (live from the relay), null when the latest label says not, undefined
 *  when there is no label. */
export function checkedLive(groupId: string, pk: string): number | null | undefined {
  const l = ticketLabels().get(groupId)?.get(pk)
  return l ? (l.v === 'checked-in' ? l.at : null) : undefined
}

/** Keys that administer every group (not shown as an event's co-hosts). */
function everywhere(groupId: string): Set<string> {
  const relay = all(me()).find((e) => e.kind === 39000 && tag(e, 'd') === groupId)?.pubkey
  return new Set([...belfryAdminKeys(), ...(relay ? [relay] : []), ...(signerKey() ? [signerKey()!] : [])])
}

export function groupRole(groupId: string, pk: string): GroupRole {
  const g = group(groupId)
  if (g?.admins.has(pk)) return 'admin'
  return isSteward(groupId, pk) ? 'steward' : 'member'
}

/** Host / co-host / steward of an event, for PK. */
export function eventRoles(e: Pick<CalEvent, 'creator' | 'groupId'>, pk: string): EventRole[] {
  if (pk === e.creator) return ['host']
  const g = group(e.groupId)
  if (g?.admins.has(pk) && !everywhere(e.groupId).has(pk)) return ['cohost']
  return isSteward(e.groupId, pk) ? ['steward'] : []
}

/** May I check people in (the server checks again)? */
export function mayCheckIn(e: Pick<CalEvent, 'creator' | 'groupId'>, apiSays?: boolean): boolean {
  if (apiSays) return true
  const k = me()
  return eventRoles(e, k).length > 0 || !!group(e.groupId)?.admins.has(k) || isAdmin()
}

/** "Check people in" shows on the header from 00:00 on the first day to the end of the last. */
export function onTheDay(e: Pick<CalEvent, 'start' | 'end'>, now = new Date()): boolean {
  const from = new Date(e.start); from.setHours(0, 0, 0, 0)
  const to = new Date(e.end ?? e.start); to.setHours(23, 59, 59, 999)
  return +now >= +from && +now <= +to
}

/** The word for a role in a group's people lists ('' for the default one). */
export function roleWord(groupId: string, pk: string, e?: Pick<CalEvent, 'creator' | 'groupId'>): string {
  if (e) { const r = eventRoles(e, pk)[0]; return r === 'host' ? 'Host' : r === 'cohost' ? 'Co-host' : r === 'steward' ? 'Steward' : '' }
  const g = group(groupId)
  return g?.admins.has(pk) && !everywhere(groupId).has(pk) ? 'Admin' : ''
}

// ---- errors ----------------------------------------------------------------------------------

export type CheckError = 'archived' | 'always_open' | 'not_allowed' | 'not_in_group' | 'last_admin' | 'guest_not_saved' | 'agent' | 'events_only' | 'always_admin' | 'no_ticket' | 'not_found' | 'invalid' | 'relay' | 'offline' | 'failed'
export const CHECK_ERRORS: Record<CheckError, string> = {
  archived: 'This group is archived. Unarchive it first.',
  always_open: 'This channel can’t be archived.',
  not_allowed: 'Only the admins can change this.',
  not_in_group: 'This person isn’t in the group.',
  last_admin: 'A group keeps at least one admin.',
  guest_not_saved: 'This guest has no e-mail yet. Once they save their account, they can be an admin.',
  agent: 'An agent can’t be an admin.',
  events_only: 'Stewards are for events.',
  always_admin: 'The host and the belfry’s admins stay admins.',
  no_ticket: 'Not a ticket for this event',
  not_found: 'This event isn’t there any more.',
  invalid: 'That did not work. Try again.',
  relay: 'The event could not be reached. Try again.',
  offline: 'No connection to the belfry. Try again.',
  failed: 'That did not work. Try again.',
}
export class CheckFailed extends Error { constructor(public code: CheckError) { super(CHECK_ERRORS[code]) } }

async function call(path: string, body: unknown): Promise<Record<string, unknown>> {
  let r: Response
  try { r = await signedFetch(`${BELFRY.api}${path}`, 'POST', body) } catch { throw new CheckFailed('offline') }
  const d = (await r.json().catch(() => ({}))) as Record<string, unknown>
  if (!r.ok) throw new CheckFailed((String(d.error) in CHECK_ERRORS ? d.error : 'failed') as CheckError)
  return d
}

// ---- changing a role -------------------------------------------------------------------------

export async function setGroupRole(groupId: string, pubkey: string, role: GroupRole): Promise<GroupRole> {
  if (isLocalOnly()) return demoSetRole(groupId, pubkey, role)
  const d = await call('/group-roles', { channel: groupId, pubkey, role })
  return (d.role as GroupRole) ?? role
}

// ---- tickets ---------------------------------------------------------------------------------

export interface Attendee { pubkey: string; name: string; ticket: string; ticket_title: string; rsvp: string; checked_in: number | null; role: string }

/** A scanned text: a URL whose path ends /t/<16 base32 characters> → the code (else undefined). */
export function codeFromScan(text: string): string | undefined {
  let path = ''
  try { path = new URL(text.trim()).pathname + new URL(text.trim()).hash } catch { return undefined }
  const m = /\/t\/([a-z2-7]{16})\/?$/i.exec(path)
  return m ? m[1].toLowerCase() : undefined
}

const B32 = 'abcdefghijklmnopqrstuvwxyz234567'
/** Demo mode only: a code made on this device (the belfry makes the real ones with its secret). */
export function demoCode(d: string, pk: string): string {
  const h = sha256(new TextEncoder().encode(`demo-ticket:${d}:${pk}`))
  let bits = 0, v = 0, out = ''
  for (const b of h.slice(0, 10)) { v = (v << 8) | b; bits += 8; while (bits >= 5) { out += B32[(v >>> (bits - 5)) & 31]; bits -= 5 } }
  return out
}

export async function attendees(e: CalEvent, holders: Map<string, string>, titles: Map<string, string>): Promise<{ people: Attendee[]; going: number; checked_in: number }> {
  if (!isLocalOnly()) {
    const d = await call(`/events/${e.d}/attendees`, {})
    return { people: (Array.isArray(d.people) ? d.people : []) as Attendee[], going: Number(d.going) || 0, checked_in: Number(d.checked_in) || 0 }
  }
  const ps = profiles()
  const people = [...holders].map(([pk, t]): Attendee => ({
    pubkey: pk, name: ps.get(pk)?.name || displayName(pk), ticket: t, ticket_title: titles.get(t) ?? '',
    rsvp: e.rsvps.get(pk) ?? 'accepted', checked_in: checkedLive(e.groupId, pk) ?? null, role: eventRoles(e, pk)[0] ?? 'member',
  })).sort((a, b) => a.name.localeCompare(b.name))
  return { people, going: people.filter((p) => p.rsvp === 'accepted').length, checked_in: people.filter((p) => p.checked_in).length }
}

/** Check in (or UNDO); PEEK: only who it is and whether they are in, nothing recorded. */
export async function checkIn(e: CalEvent, who: { code: string } | { pubkey: string }, undo: boolean, holders: Map<string, string>, peek = false): Promise<{ person: Attendee; already: boolean }> {
  if (!isLocalOnly()) {
    const d = await call(`/events/${e.d}/checkin`, { ...who, ...(undo ? { undo: true } : {}), ...(peek ? { peek: true } : {}) })
    return { person: d.person as Attendee, already: !!d.already }
  }
  const pk = 'pubkey' in who ? who.pubkey : [...holders.keys()].find((k) => demoCode(e.d, k) === who.code)
  if (!pk || !holders.has(pk)) throw new CheckFailed('no_ticket')
  const before = checkedLive(e.groupId, pk) ?? null
  if (peek) return { person: { pubkey: pk, name: profiles().get(pk)?.name || displayName(pk), ticket: holders.get(pk)!, ticket_title: '', rsvp: e.rsvps.get(pk) ?? 'accepted', checked_in: before, role: eventRoles(e, pk)[0] ?? 'member' }, already: before !== null }
  const already = undo ? before === null : before !== null
  if (!already) demoLabel(TICKET_NS, undo ? 'not-checked-in' : 'checked-in', pk, e.groupId)
  const at = already ? before : undo ? null : Math.floor(Date.now() / 1000)
  return { person: { pubkey: pk, name: profiles().get(pk)?.name || displayName(pk), ticket: holders.get(pk)!, ticket_title: '', rsvp: e.rsvps.get(pk) ?? 'accepted', checked_in: at, role: eventRoles(e, pk)[0] ?? 'member' }, already }
}

// ---- demo mode: what the belfry would publish, as the groups key -----------------------------

function hex(n: number) { return [...crypto.getRandomValues(new Uint8Array(n))].map((b) => b.toString(16).padStart(2, '0')).join('') }
function demoLabel(ns: string, v: string, pk: string, groupId: string, at = Math.floor(Date.now() / 1000)) {
  addEvents([{ id: hex(32), pubkey: signerKey() ?? 'f'.repeat(64), sig: hex(64), kind: 1985, created_at: at, content: '',
    tags: [['L', ns], ['l', v, ns], ['p', pk], ['h', groupId], ['by', me()]] }])
  touch()
}
function demoSetRole(groupId: string, pk: string, role: GroupRole): GroupRole {
  const g = group(groupId)
  const admin = !!g?.admins.has(pk)
  if (!g?.admins.has(me()) && !isAdmin()) throw new CheckFailed('not_allowed')
  if (role === 'steward' && !isEventGroup(groupId)) throw new CheckFailed('events_only')
  const now = Math.floor(Date.now() / 1000)
  const put = (r: 'admin' | 'member') => addEvents([{ id: hex(32), pubkey: signerKey() ?? 'f'.repeat(64), sig: hex(64), kind: 9000, created_at: now, content: '',
    tags: [['h', groupId], ['p', pk, r], ['role', r], ['by', me()]] }])
  const creator = all(me()).filter((e) => e.kind === 9007 && tag(e, 'h') === groupId).sort((a, b) => a.created_at - b.created_at)[0]?.pubkey
  if (role === 'admin') { if (!admin) put('admin') }
  else if (admin) { if (pk === creator || everywhere(groupId).has(pk)) throw new CheckFailed('always_admin'); put('member') }
  if (role === 'steward' && !isSteward(groupId, pk)) demoLabel(ROLE_NS, 'steward', pk, groupId, now)
  if (role !== 'steward' && isSteward(groupId, pk)) demoLabel(ROLE_NS, 'none', pk, groupId, now)
  touch()
  return role
}
/** Demo only: someone checked in a while ago (by the groups key), for the browser tests. */
export const demoCheckedIn = (groupId: string, pk: string, minutesAgo = 20) => demoLabel(TICKET_NS, 'checked-in', pk, groupId, Math.floor(Date.now() / 1000) - minutesAgo * 60)
export const demoSteward = (groupId: string, pk: string) => demoLabel(ROLE_NS, 'steward', pk, groupId)

// ---- what a co-host / admin by role can't publish on the relay: the belfry does it ----------
// zooid takes moderation events (9000, 9001, 9002) only from the relay owner, the community's
// admins and the group's creator. An admin by role goes through POST /group-roles ("removed") and
// POST /group-meta; an older belfry without them (404) gets the direct event, as before.

/** Whether PK can publish a group's moderation events itself (the relay's 39001, its creator, a
 *  community admin). */
export function relayAdmin(groupId: string, pk = me()): boolean {
  if (pk === me() && isAdmin()) return true
  const evs = all(me())
  const list = evs.filter((e) => e.kind === 39001 && tag(e, 'd') === groupId).sort((a, b) => b.created_at - a.created_at)[0]
  if (list?.tags.some((t) => t[0] === 'p' && t[1] === pk)) return true
  const made = evs.filter((e) => e.kind === 9007 && tag(e, 'h') === groupId).sort((a, b) => a.created_at - b.created_at)[0]
  return made?.pubkey === pk
}

async function viaBelfry(path: string, body: unknown): Promise<'done' | 'missing'> {
  let r: Response
  try { r = await signedFetch(`${BELFRY.api}${path}`, 'POST', body) } catch { throw new CheckFailed('offline') }
  const d = (await r.json().catch(() => ({}))) as Record<string, unknown>
  if (r.status === 404 && !d.error) return 'missing' // a belfry from before /group-roles removed, /group-meta
  if (!r.ok) throw new CheckFailed((String(d.error) in CHECK_ERRORS ? d.error : 'failed') as CheckError)
  return 'done'
}

/** Take PK out of a group (an event: their ticket too): through the belfry, else a direct 9001. */
export async function removeFromGroup(groupId: string, pk: string): Promise<void> {
  const direct = () => enqueue('groups', { kind: 9001, tags: [['h', groupId], ['p', pk]] })
  if (isLocalOnly()) { await direct(); return }
  if ((await viaBelfry('/group-roles', { channel: groupId, pubkey: pk, role: 'removed' })) === 'missing') await direct()
}

/** Archive (ON) or unarchive a group: TAGS (all of its settings, with ["archived", "1" | "0"]) as
 *  a 9002 when I can publish one, else through the belfry (POST /group-meta {"archived"}), which
 *  keeps the rest itself. A belfry from before archiving answers without the field: say so. */
export async function archiveGroupMeta(groupId: string, tags: string[][], on: boolean): Promise<void> {
  const direct = () => enqueue('groups', { kind: 9002, tags: [['h', groupId], ...tags] })
  if (isLocalOnly() || relayAdmin(groupId)) { await direct(); return }
  let r: Response
  try { r = await signedFetch(`${BELFRY.api}/group-meta`, 'POST', { channel: groupId, archived: on }) } catch { throw new CheckFailed('offline') }
  const d = (await r.json().catch(() => ({}))) as Record<string, unknown>
  if (r.status === 404 && !d.error) { await direct(); return }
  if (!r.ok) throw new CheckFailed((String(d.error) in CHECK_ERRORS ? d.error : 'failed') as CheckError)
  if (d.archived !== on) throw new CheckFailed('failed')
}

/** A group's new metadata: TAGS as a 9002 when I can publish one, else FIELDS through the belfry
 *  (which keeps every other tag of the current metadata). */
export async function editGroupMeta(groupId: string, tags: string[][], fields: { name?: string; about?: string; picture?: string; closed?: boolean; prompt?: string; vouches?: number }): Promise<void> {
  const direct = () => enqueue('groups', { kind: 9002, tags: [['h', groupId], ...tags] })
  if (isLocalOnly() || relayAdmin(groupId)) { await direct(); return }
  if ((await viaBelfry('/group-meta', { channel: groupId, ...fields })) === 'missing') await direct()
}
