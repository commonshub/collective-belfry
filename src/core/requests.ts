// Requests waiting for someone to decide (Xavier, 2026-11-14): the Requests screen at the top of
// the channel list. Three kinds:
//   - agents asking to join the belfry (Belfry agentrequests.py; belfry admins only):
//       POST /agent-requests {list: true} → {requests: [{id, pubkey, name, about, created, for_me}]}
//       POST /agent-requests {decide: id, approve} → let it in as my own agent, or decline
//   - people asking to join the belfry (/apply; Belfry membership.py):
//       POST /applications {list: true} → {applications: [{id, name, motivation, created, vouches,
//         vouched, email?}], accept, needed}   (admins; members too while vouching is on)
//       POST /applications {decide: id, approve} (admins) · {vouch: id, on} (members, vouching on)
//   - people asking to join a group (NIP-29 9021 with their answer): its admins accept (9000) or
//     decline (9001); when the group accepts by vouches its members vouch (kind 1985
//     `belfry/vouch`) and Belfry admits them (vouches.py; POST /group-requests/check right after).
// And the belfry's Membership settings: GET/POST /membership → {invite, accept, vouches,
// may_invite_members, admin}.
// Names, notes and answers are the askers' own words: shown as plain text, never as HTML.
import { BELFRY, getBackend } from '../config'
import { agentOwner } from './access'
import { isAgent, isGuest } from './invite'
import { groups, vouchesFor, type Application, type Group } from './model'
import { isBelfryAdmin } from './people'
import { touch } from './store'
import { isLocalOnly, me, signedFetch } from './sync'
import { vouch } from './actions'

export interface Membership { invite: 'admins' | 'members'; accept: 'admins' | 'vouches'; vouches: number; mayInviteMembers: boolean }
export interface BelfryRequest { id: number; name: string; motivation: string; created: string; vouches: number; vouched: boolean; email?: string }
export interface AgentRequest { id: number; pubkey: string; name: string; about: string; created: string; forMe: boolean }

export type RequestError = 'not_admin' | 'not_here' | 'not_vouching' | 'not_found' | 'decided' | 'invalid' | 'unsupported' | 'limit' | 'offline' | 'failed'
export const REQUEST_ERRORS: Record<RequestError, string> = {
  not_admin: 'Only admins of the belfry can do this.',
  not_here: 'Only members of the belfry can do this.',
  not_vouching: 'Requests here are accepted by the admins.',
  not_found: 'This request is gone.',
  decided: 'Someone answered this request already.',
  invalid: 'That did not work. Try again.',
  unsupported: 'This belfry can’t keep requests yet.',
  limit: 'You can’t take more agents.',
  offline: 'No connection to the belfry. Try again.',
  failed: 'That did not work. Try again.',
}
export class RequestFailed extends Error { constructor(public code: RequestError) { super(REQUEST_ERRORS[code]) } }

const DEFAULT: Membership = { invite: 'admins', accept: 'admins', vouches: 2, mayInviteMembers: false }
let settings: Membership = { ...DEFAULT }
let settingsLoaded = false
let people: BelfryRequest[] = []
let agents: AgentRequest[] = []

export const membership = () => settings
export const membershipLoaded = () => settingsLoaded
/** May I make member links (Belfry: admins, or any member when the setting says so)? */
export const mayInviteMembers = () => isBelfryAdmin(me()) || (settings.mayInviteMembers && !isGuest(me()) && !agentOwner(me()))
export const belfryRequests = () => people
export const agentRequests = () => agents

async function call(path: string, method: 'GET' | 'POST', body?: unknown): Promise<Record<string, unknown>> {
  let r: Response
  try { r = await signedFetch(`${BELFRY.api}${path}`, method, body) } catch { throw new RequestFailed('offline') }
  const d = (await r.json().catch(() => ({}))) as Record<string, unknown>
  if (!r.ok) throw new RequestFailed((String(d.error) in REQUEST_ERRORS ? d.error : 'failed') as RequestError)
  return d
}

const str = (x: unknown) => (typeof x === 'string' ? x : '')
const takeSettings = (d: Record<string, unknown>) => {
  settings = {
    invite: d.invite === 'members' ? 'members' : 'admins',
    accept: d.accept === 'vouches' ? 'vouches' : 'admins',
    vouches: typeof d.vouches === 'number' && d.vouches >= 1 && d.vouches <= 5 ? d.vouches : 2,
    mayInviteMembers: !!d.may_invite_members,
  }
  settingsLoaded = true
  touch()
}
const cleanPeople = (x: unknown): BelfryRequest[] => (Array.isArray(x) ? x : []).filter((r) => r && typeof r.id === 'number').map((r) => ({
  id: r.id, name: str(r.name), motivation: str(r.motivation), created: str(r.created), vouches: Number(r.vouches) || 0, vouched: !!r.vouched, ...(typeof r.email === 'string' ? { email: r.email } : {}),
}))
const cleanAgents = (x: unknown): AgentRequest[] => (Array.isArray(x) ? x : []).filter((r) => r && typeof r.id === 'number' && /^[0-9a-f]{64}$/.test(r.pubkey)).map((r) => ({
  id: r.id, pubkey: r.pubkey, name: str(r.name), about: str(r.about), created: str(r.created), forMe: !!r.for_me,
}))

export async function loadMembership(): Promise<void> {
  if (isLocalOnly()) { settingsLoaded = true; touch(); return }
  try { takeSettings(await call('/membership', 'GET')) } catch { /* offline: what we had */ }
}

export async function saveMembership(patch: Partial<Pick<Membership, 'invite' | 'accept' | 'vouches'>>): Promise<void> {
  if (isLocalOnly()) {
    await new Promise((r) => setTimeout(r, 300))
    settings = { ...settings, ...patch, mayInviteMembers: settings.mayInviteMembers }
    if (settings.accept === 'vouches') people = people.filter((p) => p.vouches < settings.vouches)
    touch(); return
  }
  takeSettings(await call('/membership', 'POST', patch))
  void loadRequests()
}

let inflight: Promise<void> | null = null
/** What the belfry has waiting: people (admins, or members while vouching) and agents (admins). */
export function loadRequests(): Promise<void> {
  if (isLocalOnly()) return Promise.resolve()
  inflight ??= (async () => {
    await loadMembership()
    const admin = isBelfryAdmin(me())
    const jobs: Promise<unknown>[] = []
    if (admin || (settings.accept === 'vouches' && !isGuest(me()) && !agentOwner(me())))
      jobs.push(call('/applications', 'POST', { list: true }).then((d) => { people = cleanPeople(d.applications) }, () => { /* not allowed or offline */ }))
    else people = []
    if (admin) jobs.push(call('/agent-requests', 'POST', { list: true }).then((d) => { agents = cleanAgents(d.requests) }, () => { /* offline */ }))
    else agents = []
    await Promise.all(jobs)
    touch()
  })().finally(() => { inflight = null })
  return inflight
}

let timer = 0
/** Keep the lists fresh while the app is open (every two minutes, and when it comes back). */
export function startRequests() {
  if (timer) return
  void loadRequests()
  timer = window.setInterval(() => { if (document.visibilityState === 'visible') void loadRequests() }, 120_000)
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') void loadRequests() })
}

export async function decidePerson(id: number, approve: boolean): Promise<void> {
  if (isLocalOnly()) { await new Promise((r) => setTimeout(r, 300)); people = people.filter((p) => p.id !== id); touch(); return }
  try { await call('/applications', 'POST', { decide: id, approve }) } finally { await loadRequests() }
}

/** Vouch for (or take back) a request to join the belfry; true when it got in by it. */
export async function vouchPerson(id: number, on: boolean): Promise<boolean> {
  if (isLocalOnly()) {
    await new Promise((r) => setTimeout(r, 300))
    const p = people.find((x) => x.id === id)
    if (!p) throw new RequestFailed('not_found')
    p.vouched = on; p.vouches = Math.max(0, p.vouches + (on ? 1 : -1))
    const done = settings.accept === 'vouches' && p.vouches >= settings.vouches
    if (done) people = people.filter((x) => x.id !== id)
    touch(); return done
  }
  try { const d = await call('/applications', 'POST', { vouch: id, on }); return !!d.accepted } finally { await loadRequests() }
}

/** Let an agent in as my own agent, or decline it. */
export async function decideAgent(id: number, approve: boolean): Promise<void> {
  if (isLocalOnly()) { await new Promise((r) => setTimeout(r, 300)); agents = agents.filter((a) => a.id !== id); touch(); return }
  try { await call('/agent-requests', 'POST', { decide: id, approve }) } finally { await loadRequests() }
}

// ---- groups --------------------------------------------------------------------------------

/** Only members of the belfry count when vouching (Belfry leaves out guests and agents). */
export const counts = (pk: string) => !isGuest(pk) && !isAgent(pk)

/** Requests to group G I can act on: all of them when I'm one of its admins; while it accepts
 *  by vouches, its members (of the belfry) see them to vouch. */
export function groupRequestsFor(g: Group): Application[] {
  if (!g.closed || g.applications.length === 0) return []
  if (g.admins.has(me())) return g.applications
  if (g.vouches > 0 && g.members.has(me()) && counts(me())) return g.applications.filter((a) => a.pubkey !== me())
  return []
}
export const groupRequests = (): { g: Group; list: Application[] }[] =>
  groups().map((g) => ({ g, list: groupRequestsFor(g) })).filter((x) => x.list.length > 0)

export const groupVouches = (g: Group, a: Application) => vouchesFor(g, a, counts)

/** Vouch in a group, then ask Belfry to count at once (it also does every few minutes). */
export async function vouchInGroup(g: Group, a: Application, on: boolean): Promise<void> {
  await vouch(g.id, { id: a.ev.id, pubkey: a.pubkey }, on)
  if (getBackend() === 'demo' || isLocalOnly()) return
  setTimeout(() => { void signedFetch(`${BELFRY.api}/group-requests/check`, 'POST', { channel: g.id }).catch(() => { /* the round does it */ }) }, 2500)
}

/** How many things wait for me: the count on ☰ and next to Requests. */
export function requestCount(): number {
  const admin = isBelfryAdmin(me())
  let n = 0
  if (admin) n += agents.length
  n += people.filter((p) => admin || !p.vouched).length
  for (const { g, list } of groupRequests()) n += g.admins.has(me()) ? list.length : list.filter((a) => !groupVouches(g, a).includes(me())).length
  return n
}
/** Whether to show Requests at all: something waits, or I'm someone who decides things. */
export const showRequests = () => requestCount() > 0 || isBelfryAdmin(me()) || groups().some((g) => g.closed && g.admins.has(me()))

// ---- demo ----------------------------------------------------------------------------------

/** Demo only: a few requests to look at (window.demoRequests). */
export function demoRequests(opts: { vouching?: boolean; needed?: number; inviteMembers?: boolean } = {}) {
  if (getBackend() !== 'demo') return
  settings = { invite: opts.inviteMembers ? 'members' : 'admins', accept: opts.vouching ? 'vouches' : 'admins', vouches: opts.needed ?? 2, mayInviteMembers: !!opts.inviteMembers }
  settingsLoaded = true
  const ago = (h: number) => new Date(Date.now() - h * 3600_000).toISOString().slice(0, 19).replace('T', ' ')
  people = [
    { id: 1, name: 'Nora Lindqvist', motivation: 'I run the repair café on Thursdays and would like to bring it here. <b>not bold</b>', created: ago(5), vouches: 1, vouched: false, ...(isBelfryAdmin(me()) ? { email: 'nora@example.org' } : {}) },
    { id: 2, name: 'Omar B.', motivation: 'A friend of Ana’s.', created: ago(30), vouches: 0, vouched: false, ...(isBelfryAdmin(me()) ? { email: 'omar@example.org' } : {}) },
  ]
  agents = isBelfryAdmin(me()) ? [{ id: 7, pubkey: 'b'.repeat(64), name: 'Scout', about: 'Sorts the bug reports in #issues.', created: ago(2), forMe: true }] : []
  touch()
}
