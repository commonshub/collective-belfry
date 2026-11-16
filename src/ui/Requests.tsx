// What waits for someone to decide (Xavier, 2026-11-14), shown at the top of the members
// directory (2026-11-15: "instead of requests top left it should be members directory and pending
// requests should show up at the top of it"). One list, by where the request goes:
//   - Agents asking to join (the belfry's admins): "Let it in as my agent" / Decline;
//   - People asking to join the belfry (admins: Accept / Decline; every member vouches);
//   - each group I'm an admin of (or a member of, when it accepts by vouches): who asks, their
//     answer to its question, "1 of 2 vouches", Accept / Decline / Vouch.
// Everything the askers wrote is shown as plain text.
import { relayAdmin } from '../core/checkin'
import { useState } from 'react'
import { nip19 } from 'nostr-tools'
import { NAME } from '../config'
import { canAdmin } from '../core/access'
import { decline as declineInGroup, admit } from '../core/actions'
import { displayName, group, type Application, type Group } from '../core/model'
import { isBelfryAdmin } from '../core/people'
import { agentRequests, belfryRequests, decideAgent, decidePerson, groupRequests, groupVouches, membership, RequestFailed, vouchInGroup, vouchPerson, type AgentRequest, type BelfryRequest } from '../core/requests'
import { useVersion } from '../core/store'
import { me } from '../core/sync'
import { Avatar, Btn, fmtTime, go } from './common'
import { useCommunityName } from './community'
import { BotIcon, InboxIcon, PodShape, VouchIcon } from './icons'

const when = (s: string) => {
  const t = Date.parse(s.replace(' ', 'T') + (s.endsWith('Z') ? '' : 'Z'))
  return Number.isFinite(t) ? fmtTime(Math.floor(t / 1000)) : ''
}
const vouchLine = (n: number, of: number) => `${n} of ${of} ${of === 1 ? 'vouch' : 'vouches'}`

/** What waits for someone to decide, by where the request goes; nothing when nothing waits.
 *  Shown at the top of the members directory (#/members; #/requests opens the same screen). */
export function RequestSections() {
  useVersion()
  const community = useCommunityName(NAME)
  const admin = isBelfryAdmin(me())
  const agents = admin ? agentRequests() : []
  const people = belfryRequests()
  const groups = groupRequests()
  if (agents.length === 0 && people.length === 0 && groups.length === 0) return null
  return (
    <div className="requests">
      {agents.length > 0 && <>
        <div className="section-title"><span className="lbl"><BotIcon /> Agents asking to join</span><span>{agents.length}</span></div>
        {agents.map((a) => <AgentRow key={a.id} a={a} />)}
      </>}
      {people.length > 0 && <>
        <div className="section-title"><span className="lbl"><InboxIcon /> Asking to join {community}</span><span>{people.length}</span></div>
        <p className="hint pad-x req-rule">{membership().accept === 'vouches'
          ? `${membership().vouches === 1 ? 'One vouch' : `${membership().vouches} vouches`} from members let someone in${admin ? '; admins can still accept or decline' : ''}.`
          : admin ? 'You decide; members vouch for the people they know.' : 'Vouch for the people you know: the admins decide, and they see who vouched.'}</p>
        {people.map((p) => <PersonRow key={p.id} p={p} admin={admin} />)}
      </>}
      {groups.map(({ g, list }) => (
        <div key={g.id} className="req-group">
          <button type="button" className="section-title link-title" onClick={() => go(`c/${encodeURIComponent(g.id)}/group`)}>
            <span className="lbl"><PodShape members={g.members.size} size={16} /> {g.name}</span><span>{list.length}</span>
          </button>
          {g.vouches > 0 && <p className="hint pad-x req-rule">{g.vouches === 1 ? 'One vouch' : `${g.vouches} vouches`} from its members let someone in.</p>}
          {list.map((a) => <GroupRequest key={a.ev.id} g={g} a={a} />)}
        </div>
      ))}
    </div>
  )
}

function Fail({ error }: { error: string }) { return error ? <div className="error" role="alert">{error}</div> : null }
const fixed = (e: unknown) => (e instanceof RequestFailed ? e.message : 'That did not work. Try again.')

function AgentRow({ a }: { a: AgentRequest }) {
  const [error, setError] = useState('')
  const act = (approve: boolean) => decideAgent(a.id, approve).catch((e) => setError(fixed(e)))
  return (
    <div className="appl req-row">
      <div className="who"><span className="req-ic"><BotIcon /></span><span className="t">{a.name || 'An agent'}<small>{nip19.npubEncode(a.pubkey).slice(0, 16)}… · {when(a.created)}{a.forMe ? ' · asked for you' : ''}</small></span></div>
      {a.about && <blockquote>{a.about}</blockquote>}
      <Fail error={error} />
      <div className="row">
        <Btn className="btn small" onClick={() => act(true)}>Let it in as my agent</Btn>
        <Btn className="btn small ghost" onClick={() => act(false)}>Decline</Btn>
      </div>
    </div>
  )
}

export function PersonRow({ p, admin }: { p: BelfryRequest; admin: boolean }) {
  const [error, setError] = useState('')
  const [done, setDone] = useState('')
  const m = membership()
  const vouching = m.accept === 'vouches'
  return (
    <div className="appl req-row">
      <div className="who"><span className="req-ic initial" aria-hidden="true">{(p.name.trim()[0] ?? '?').toUpperCase()}</span>
        <span className="t">{p.name || 'Someone'}<small>{admin && p.email ? `${p.email} · ` : ''}{when(p.created)}</small>
          {p.guest && <small className="guest-line">Guest in {p.guest.channels.map((c) => group(c)?.name ?? c).join(', ') || 'a channel'}{p.guest.since ? ` since ${when(p.guest.since)}` : ''}{p.guest.inviter ? `, invited by ${p.guest.inviter}` : ''}</small>}</span></div>
      {p.motivation && <blockquote>{p.motivation}</blockquote>}
      {/* anyone vouches (#issues 2026-11-15); while the admins decide, vouches tell them who stands for whom */}
      <div className="hint vouch-line"><VouchIcon /> {vouching ? vouchLine(p.vouches, m.vouches) : `${p.vouches} ${p.vouches === 1 ? 'vouch' : 'vouches'} · the admins decide`}{p.vouched ? ' · you vouched' : ''}</div>
      <Fail error={error} />
      {done ? <div className="hint">{done}</div> : (
        <div className="row">
          {admin && <Btn className="btn small" onClick={() => decidePerson(p.id, true).catch((e) => setError(fixed(e)))}>Accept</Btn>}
          {admin && <Btn className="btn small ghost" onClick={() => decidePerson(p.id, false).catch((e) => setError(fixed(e)))}>Decline</Btn>}
          {(
            <Btn className={'btn small' + (p.vouched ? ' ghost' : '')} aria-pressed={p.vouched}
              onClick={() => vouchPerson(p.id, !p.vouched).then((inNow) => { if (inNow) setDone(p.guest ? 'Accepted: they are a member now.' : 'Accepted: they get an invite by e-mail.') }, (e) => setError(fixed(e)))}>
              {p.vouched ? 'Take back my vouch' : 'Vouch'}</Btn>
          )}
        </div>
      )}
    </div>
  )
}

/** One request to join group G: who, when, the question and their answer, vouches; what I can do. */
export function GroupRequest({ g, a }: { g: Group; a: Application }) {
  useVersion()
  const [error, setError] = useState('')
  // its admins, and the belfry's; an admin by role can't admit on the relay (docs/checkin.md): they vouch
  const admin = canAdmin(g) && relayAdmin(g.id)
  const vouchers = groupVouches(g, a)
  const mine = vouchers.includes(me())
  const canVouch = g.vouches > 0 && g.members.has(me()) && a.pubkey !== me()
  const run = (f: () => Promise<unknown>) => f().catch((e) => setError(e instanceof Error ? 'That did not work. Try again.' : String(e)))
  return (
    <div className="appl req-row">
      <div className="who"><Avatar pubkey={a.pubkey} size={28} /><span className="t">{displayName(a.pubkey)}<small>{fmtTime(a.at)}</small></span></div>
      {g.prompt && <div className="hint req-q">{g.prompt}</div>}
      <blockquote>{a.answer || '(no answer)'}</blockquote>
      {g.vouches > 0 && (
        <div className="hint vouch-line"><VouchIcon /> {vouchLine(vouchers.length, g.vouches)}
          {vouchers.length > 0 && <> · {vouchers.map((v) => displayName(v)).join(', ')}</>}</div>
      )}
      <Fail error={error} />
      <div className="row">
        {admin && <Btn className="btn small" onClick={() => run(() => admit(g.id, a.pubkey))}>Accept</Btn>}
        {admin && <Btn className="btn small ghost" onClick={() => run(() => declineInGroup(g.id, a.pubkey))}>Decline</Btn>}
        {canVouch && <Btn className={'btn small' + (mine || admin ? ' ghost' : '')} aria-pressed={mine} onClick={() => run(() => vouchInGroup(g, a, !mine))}>{mine ? 'Take back my vouch' : 'Vouch'}</Btn>}
      </div>
    </div>
  )
}
