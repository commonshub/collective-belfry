// Bringing a member into a channel by name (the invite sheet, or after mentioning someone who is
// not in it): they get a direct message with the channel's link; in a closed pod an admin also
// admits them, so the link opens straight away. Guests of other channels are not members: they
// need a guest link (the invite sheet's switch), not this.
import { BELFRY } from '../config'
import { canAdmin } from './access'
import { admit } from './actions'
import { sendDm } from './dm'
import { group, groups, guestJoins, profiles, type Group } from './model'
import { me } from './sync'

/** Someone who is in this belfry only as a channel's guest (a person or an agent). */
export const isGuest = (pubkey: string) => groups().some((g) => guestJoins(g.id).some((j) => j.pubkey === pubkey && !j.member))

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
export async function inviteMember(groupId: string, pubkey: string): Promise<'admitted' | 'asked' | 'sent'> {
  const g = group(groupId)
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
