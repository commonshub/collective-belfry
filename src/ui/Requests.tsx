// #/requests: what waits for someone to decide (Xavier, 2026-11-14). Opened from Requests at the
// top of the channel list (its count is also on ☰). One list, by where the request goes:
//   - Agents asking to join (the belfry's admins): "Let it in as my agent" / Decline;
//   - People asking to join the belfry (admins: Accept / Decline; while the belfry accepts by
//     vouches, every member sees them and vouches);
//   - each group I'm an admin of (or a member of, when it accepts by vouches): who asks, their
//     answer to its question, "1 of 2 vouches", Accept / Decline / Vouch.
// Everything the askers wrote is shown as plain text.
import { useEffect, useState } from 'react'
import { nip19 } from 'nostr-tools'
import { NAME } from '../config'
import { canAdmin } from '../core/access'
import { decline as declineInGroup, admit } from '../core/actions'
import { displayName, type Application, type Group } from '../core/model'
import { isBelfryAdmin } from '../core/people'
import { agentRequests, belfryRequests, decideAgent, decidePerson, groupRequests, groupVouches, loadRequests, membership, RequestFailed, vouchInGroup, vouchPerson, type AgentRequest, type BelfryRequest } from '../core/requests'
import { useVersion } from '../core/store'
import { me } from '../core/sync'
import { Avatar, back, Btn, fmtTime, go } from './common'
import { useCommunityName } from './community'
import { BotIcon, InboxIcon, PodShape, VouchIcon } from './icons'

const when = (s: string) => {
  const t = Date.parse(s.replace(' ', 'T') + (s.endsWith('Z') ? '' : 'Z'))
  return Number.isFinite(t) ? fmtTime(Math.floor(t / 1000)) : ''
}
const vouchLine = (n: number, of: number) => `${n} of ${of} ${of === 1 ? 'vouch' : 'vouches'}`

export function RequestsScreen() {
  useVersion()
  const community = useCommunityName(NAME)
  const [busy, setBusy] = useState(true)
  useEffect(() => { void loadRequests().finally(() => setBusy(false)) }, [])
  const admin = isBelfryAdmin(me())
  const agents = agentRequests()
  const people = belfryRequests()
  const groups = groupRequests()
  const nothing = agents.length === 0 && people.length === 0 && groups.length === 0
  return (
    <div className="screen requests">
      <div className="topbar">
        <button className="icon-btn" aria-label="Back" onClick={() => back('c')}>‹</button>
        <h1>Requests<small>Who asks to join, and where</small></h1>
      </div>
      <div className="scroll">
        {nothing && <div className="empty">{busy ? 'Loading…' : 'Nothing is waiting for you.'}</div>}
        {admin && agents.length > 0 && <>
          <div className="section-title"><span className="lbl"><BotIcon /> Agents asking to join</span><span>{agents.length}</span></div>
          {agents.map((a) => <AgentRow key={a.id} a={a} />)}
        </>}
        {people.length > 0 && <>
          <div className="section-title"><span className="lbl"><InboxIcon /> Asking to join {community}</span><span>{people.length}</span></div>
          {membership().accept === 'vouches' && <p className="hint pad-x req-rule">{membership().vouches === 1 ? 'One vouch' : `${membership().vouches} vouches`} from members let someone in{admin ? '; admins can still accept or decline' : ''}.</p>}
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

function PersonRow({ p, admin }: { p: BelfryRequest; admin: boolean }) {
  const [error, setError] = useState('')
  const [done, setDone] = useState('')
  const m = membership()
  const vouching = m.accept === 'vouches'
  return (
    <div className="appl req-row">
      <div className="who"><span className="req-ic initial" aria-hidden="true">{(p.name.trim()[0] ?? '?').toUpperCase()}</span>
        <span className="t">{p.name || 'Someone'}<small>{admin && p.email ? `${p.email} · ` : ''}{when(p.created)}</small></span></div>
      {p.motivation && <blockquote>{p.motivation}</blockquote>}
      {vouching && <div className="hint vouch-line"><VouchIcon /> {vouchLine(p.vouches, m.vouches)}{p.vouched ? ' · you vouched' : ''}</div>}
      <Fail error={error} />
      {done ? <div className="hint">{done}</div> : (
        <div className="row">
          {admin && <Btn className="btn small" onClick={() => decidePerson(p.id, true).catch((e) => setError(fixed(e)))}>Accept</Btn>}
          {admin && <Btn className="btn small ghost" onClick={() => decidePerson(p.id, false).catch((e) => setError(fixed(e)))}>Decline</Btn>}
          {vouching && !admin && (
            <Btn className={'btn small' + (p.vouched ? ' ghost' : '')} aria-pressed={p.vouched}
              onClick={() => vouchPerson(p.id, !p.vouched).then((inNow) => { if (inNow) setDone('Accepted: they get an invite by e-mail.') }, (e) => setError(fixed(e)))}>
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
  const admin = canAdmin(g) // its admins, and the belfry's
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
