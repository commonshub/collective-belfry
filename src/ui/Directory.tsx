// #/members: everyone in the belfry (Xavier, #issues 2026-11-15: "a directory of all members of the
// belfry (group agents under their owner)"), opened from Members at the top of the channel list.
// First, whatever waits for me (RequestSections: agents and people asking to join the belfry,
// requests to join my groups). #/requests opens this screen too.
import { useEffect, useMemo, useState } from 'react'
import { NAME } from '../config'
import { displayName, profiles } from '../core/model'
import { roleBadge } from '../core/roles'
import { directory, loadDirectory, loadMembership, loadRequests, mayInviteMembers } from '../core/requests'
import { groups } from '../core/model'
import { inOrder } from '../core/order'
import { InviteSheet } from './Invite'
import { PersonPlusIcon } from './icons'
import { useVersion } from '../core/store'
import { Avatar, back } from './common'
import { useCommunityName } from './community'
import { openProfile } from './Profile'
import { RequestSections } from './Requests'

export function MembersDirectory() {
  useVersion()
  const community = useCommunityName(NAME)
  const [q, setQ] = useState('')
  const [busy, setBusy] = useState(true)
  const [inviting, setInviting] = useState(false)
  useEffect(() => { void loadMembership() }, [])
  // the invite starts in # general, else the first open channel in the admins' order
  const inviteIn = groups().find((g) => g.id === 'general')?.id ?? inOrder(groups().filter((g) => !g.isEvent && !g.isPod && !g.isProposal && !g.closed))[0]?.id
  useEffect(() => { void Promise.all([loadDirectory(), loadRequests()]).finally(() => setBusy(false)) }, [])
  const ps = profiles()
  const nameOf = (pk: string, fallback: string) => ps.get(pk)?.name || fallback || displayName(pk)
  const list = useMemo(() => (directory() ?? [])
    .map((m) => ({ ...m, shown: nameOf(m.pubkey, m.name) }))
    .filter((m) => !q.trim() || m.shown.toLowerCase().includes(q.trim().toLowerCase()) || m.agents.some((a) => nameOf(a.pubkey, a.name).toLowerCase().includes(q.trim().toLowerCase())))
    .sort((a, b) => a.shown.localeCompare(b.shown)), [directory(), q, ps]) // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <div className="screen members-dir">
      <div className="topbar">
        <button className="icon-btn" aria-label="Back" onClick={() => back('c')}>‹</button>
        <h1>Members<small>{directory() ? `${directory()!.length} in ${community}` : community}</small></h1>
        {mayInviteMembers() && inviteIn && <button className="icon-btn" aria-label="Invite someone" onClick={() => setInviting(true)}><PersonPlusIcon /></button>}
      </div>
      {inviting && inviteIn && <InviteSheet id={inviteIn} preset="member" onClose={() => setInviting(false)} />}
      <div className="scroll">
        <RequestSections />
        <div className="pad-x dir-search"><input className="input" type="search" placeholder="Search members" aria-label="Search members" value={q} onChange={(e) => setQ(e.target.value)} /></div>
        {!directory() && <div className="empty">{busy ? 'Loading…' : 'The list could not be loaded.'}</div>}
        {list.map((m) => (
          <div key={m.pubkey} className="dir-member">
            <button className="dir-row" onClick={() => openProfile(m.pubkey)}>
              <Avatar pubkey={m.pubkey} size={36} />
              <span className="t"><b>{m.shown}</b>{ps.get(m.pubkey)?.nip05 && <small>{ps.get(m.pubkey)!.nip05}</small>}</span>
              {roleBadge(m.pubkey) && <span className="badge role-badge">{roleBadge(m.pubkey)}</span>}
            </button>
            {m.agents.map((a) => (
              <button key={a.pubkey} className="dir-row agent" onClick={() => openProfile(a.pubkey)}>
                <Avatar pubkey={a.pubkey} size={28} />
                <span className="t"><b>{nameOf(a.pubkey, a.name)}</b><small>🤖 {m.shown}'s agent</small></span>
              </button>
            ))}
          </div>
        ))}
      </div>
    </div>
  )
}
