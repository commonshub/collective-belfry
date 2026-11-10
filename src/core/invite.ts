// Bringing a member into a channel by name (the invite sheet, or after mentioning someone who is
// not in it): they get a direct message with the channel's link; in a closed pod an admin also
// admits them, so the link opens straight away. Guests of other channels are not members: they
// need a guest link (the invite sheet's switch), not this.
import { BELFRY } from '../config'
import { agentOwner, canAdmin, loadAccessInfo } from './access'
import { admit } from './actions'
import { sendDm } from './dm'
import { group, groups, guestJoins, profiles, type Group } from './model'
import { me, signedFetch } from './sync'
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
