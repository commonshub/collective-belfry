// Bringing a member into a channel by name (the invite sheet, or after mentioning someone who is
// not in it): they get a direct message with the channel's link; in a closed pod an admin also
// admits them, so the link opens straight away. Guests of other channels are not members: they
// need a guest link (the invite sheet's switch), not this.
import { BELFRY } from '../config'
import { agentOwner, canAdmin, loadAccessInfo } from './access'
import { admit } from './actions'
import { sendDm } from './dm'
import { group, groups, guestJoins, profiles, type Group } from './model'
import { isLocalOnly, me, signedFetch } from './sync'
import { touch } from './store'

/** Someone who is in this belfry only as a channel's guest (a person or an agent). */
export const isGuest = (pubkey: string) => !agentOwner(pubkey) && groups().some((g) => guestJoins(g.id).some((j) => j.pubkey === pubkey && !j.member))
/** An agent (a channel's guest, or a member's own), not a person. */
export const isAgent = (pubkey: string) => !!agentOwner(pubkey) || groups().some((g) => guestJoins(g.id).some((j) => j.pubkey === pubkey && j.agent))

/** Whether PUBKEY can read CHANNEL: an open channel is every member's (and its own guests');
 *  a closed one (a pod by application) only its members' and admins'. */
export function inChannel(g: Group | undefined, pubkey: string): boolean {
  if (!g) return true
  if (g.members.has(pubkey) || g.admins.has(pubkey)) return true
  if (g.closed) return false
  return !isGuest(pubkey)
}

export const channelUrl = (id: string) => `https://chat.${BELFRY.domain}/#/c/${encodeURIComponent(id)}`

/** Invite a member: 'admitted' when they are in at once, 'asked' when they still have to apply
 *  (a closed pod I do not administer), 'sent' for an open channel. */
export async function inviteMember(groupId: string, pubkey: string): Promise<'admitted' | 'asked' | 'sent' | 'guest'> {
  const g = group(groupId)
  if (isGuest(pubkey)) { // a guest of another channel (a person or an agent): a guest of this one too
    const res = await signedFetch(`${BELFRY.api}/channel-guests`, 'POST', { channel: groupId, pubkey })
    const body = (await res.json().catch(() => ({}))) as { message?: string; member?: boolean }
    if (!res.ok) throw new Error(body.message || 'Could not add them. Try again.')
    if (!body.member) return 'guest'
  }
  const name = g ? (g.isPod || g.isEvent || g.isProposal ? g.name : '# ' + g.name) : groupId
  let state: 'admitted' | 'asked' | 'sent' = 'sent'
  if (g?.closed && !g.members.has(pubkey)) {
    if (canAdmin(g)) { await admit(groupId, pubkey); state = 'admitted' } else state = 'asked'
  }
  const from = profiles().get(me())?.name || 'A member' // my own name as others read it (never "You")
  const text = state === 'asked'
    ? `${from} thinks you should join ${name}. It takes an application: ${channelUrl(groupId)}`
    : `${from} invited you to ${name}: ${channelUrl(groupId)}`
  await sendDm(('n:' + pubkey) as Parameters<typeof sendDm>[0], text)
  return state
}

// ── a member's own agents (Belfry's agents.py) ──────────────────────────────────────────────
export interface MyAgents { agents: { pubkey: string; name: string; since: string }[]; limit: number }

async function agentsCall(body: Record<string, unknown>): Promise<MyAgents> {
  const res = await signedFetch(`${BELFRY.api}/agents`, 'POST', body)
  const out = (await res.json().catch(() => ({}))) as MyAgents & { message?: string }
  if (!res.ok) throw new Error(out.message || 'Could not do that. Try again.')
  return { agents: Array.isArray(out.agents) ? out.agents : [], limit: Number(out.limit) || 0 }
}
/** My agents and how many I can have. */
export const myAgents = () => agentsCall({})
/** Make a guest agent my own (it stays a member for as long as I am one), or remove my agent. */
export async function setMyAgent(pubkey: string, mine: boolean): Promise<MyAgents> {
  const out = await agentsCall({ pubkey, mine })
  await loadAccessInfo()
  touch()
  return out
}

// ── the invite sheet's link (Belfry guests.py, roles.py) ────────────────────────────────────
// Guest of the channel for a day, a week or a month; or (the belfry's admins only) a member of
// the belfry with a role, for a month, a year or for good. The link itself: how many people
// (one, up to 50) and how long it can be opened (a day, a week, a month).
export type GuestDays = 1 | 7 | 30
export type Membership = 'month' | 'year' | 'permanent'
export type JoinsAs = { as: 'guest'; days: GuestDays } | { as: 'member'; role: string; membership: Membership } | { as: 'agent' }
export interface LinkOpts { people: 1 | 50; linkDays: 1 | 7 | 30 }

export type LinkError = 'not_admin' | 'not_allowed' | 'guest' | 'agent' | 'unknown_role' | 'limit' | 'unsupported' | 'offline' | 'failed'
export const LINK_ERRORS: Record<LinkError, string> = {
  not_admin: 'Only admins of the belfry invite members.',
  not_allowed: 'You can’t invite people to this channel.',
  guest: 'Guests can’t invite.',
  agent: 'Agents can’t invite.',
  unknown_role: 'That role doesn’t exist any more. Pick another.',
  limit: 'You have your agent already.',
  unsupported: 'This belfry does not take guests.',
  offline: 'No connection to the belfry.',
  failed: 'The link could not be made. Try again.',
}
export class LinkFailed extends Error { constructor(public code: LinkError) { super(LINK_ERRORS[code]) } }

/** A link for CHANNEL: ``{url, agentUrl}``. */
export async function makeInviteLink(channel: string, who: JoinsAs, link: LinkOpts): Promise<{ url: string; agentUrl: string }> {
  const body: Record<string, unknown> = { channel, people: who.as === 'agent' ? 1 : link.people, link_days: link.linkDays }
  if (who.as === 'guest') body.guest_days = who.days
  else if (who.as === 'member') Object.assign(body, { as: 'member', role: who.role, membership: who.membership })
  else body.agent = true
  if (isLocalOnly()) { // demo: what the belfry would answer
    await new Promise((r) => setTimeout(r, 250))
    const token = [...crypto.getRandomValues(new Uint8Array(24))].map((b) => b.toString(16).padStart(2, '0')).join('')
    return { url: `https://chat.${BELFRY.domain}/join/${token}`, agentUrl: `https://belfry.${BELFRY.domain}/invite/${token}.md` }
  }
  let r: Response
  try { r = await signedFetch(`${BELFRY.api}/channel-invites`, 'POST', body) } catch { throw new LinkFailed('offline') }
  const d = (await r.json().catch(() => ({}))) as { url?: string; agent_url?: string; error?: string }
  if (!r.ok || !d.url) throw new LinkFailed((d.error && d.error in LINK_ERRORS ? d.error : 'failed') as LinkError)
  return { url: String(d.url), agentUrl: String(d.agent_url ?? '') }
}
