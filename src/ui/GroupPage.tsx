// #/c/<id>/group: a group's page (Xavier, 2026-11-14). The card a new group leaves in its parent
// channel opens it: what the group is, who is in it and who runs it, Follow (its announcements by
// e-mail, the bell), and Join (open) / Apply to join (closed) / Pending / You're in. A group is
// not membership of the belfry: whoever created it is its admin and lets people in.
import { GroupName } from './GroupName'
import { useState } from 'react'
import { DEFAULT_PROMPT, joinGroup, setArchived } from '../core/actions'
import { canAdmin } from '../core/access'
import { useGroupState } from '../core/groupstate'
import { archivedGroups, channelIcon, displayName, group, messages, type Group } from '../core/model'
import { groupRequestsFor, groupVouches } from '../core/requests'
import { useVersion } from '../core/store'
import { me } from '../core/sync'
import { Avatar, back, Btn, Form, go, Sheet } from './common'
import { FollowButton } from './Follow'
import { ChevronRightIcon, PodShape, VouchIcon } from './icons'
import { Markdown } from './markdown'
import { openProfile } from './Profile'
import { GroupRequest } from './Requests'

/** Any link to a deleted channel or group lands here: it says so, with a way back. Nothing else
 *  (no composer, no pull-down, no settings). Fixed words only: nothing from the address. */
export function DeletedGroup() {
  return (
    <div className="screen group-deleted">
      <div className="topbar"><button className="icon-btn" aria-label="Back" onClick={() => back('c')}>‹</button><h1>Group</h1></div>
      <div className="empty">
        <b>This group has been deleted</b>
        <p><button className="btn ghost" onClick={() => back('c')}>Back</button></p>
      </div>
    </div>
  )
}

/** Under an archived group's header: "Archived · read only", and Unarchive for who may. */
export function ArchivedBar({ g }: { g: Group }) {
  const [error, setError] = useState('')
  return (
    <div className="archived-bar" role="status">
      <span><b>Archived</b> · read only</span>
      {canAdmin(g) && <Btn className="btn small" onClick={async () => { setError(''); try { await setArchived(g.id, false) } catch (e) { setError(e instanceof Error ? e.message : 'That did not work. Try again.') } }}>Unarchive</Btn>}
      {error && <span className="error" role="alert">{error}</span>}
    </div>
  )
}

/** "Nov 15" (with the year when another one). */
const shortDay = (at: number) => { const d = new Date(at * 1000); return d.toLocaleDateString([], { month: 'short', day: 'numeric', ...(d.getFullYear() !== new Date().getFullYear() ? { year: 'numeric' } : {}) }) }

/** #/archived: the archived channels and groups (the row at the bottom of the channel list). A
 *  tap opens one, read only; Unarchive is there. */
export function ArchivedScreen() {
  useVersion()
  const list = archivedGroups()
  return (
    <div className="screen">
      <div className="topbar"><button className="icon-btn" aria-label="Back" onClick={() => back('c')}>‹</button><h1>Archived</h1></div>
      <div className="scroll">
        {list.length === 0 && <div className="empty">Nothing is archived.</div>}
        {list.map((g) => {
          const parent = g.parent ? group(g.parent) : undefined
          const last = messages(g.id).at(-1)?.ev.created_at
          const kind = g.isPod ? `Group${parent ? ` in ${parent.isPod ? '' : '#'}${parent.name}` : ''}` : 'Channel'
          return (
            <button key={g.id} className="chan archived-row" onClick={() => go('c/' + encodeURIComponent(g.id))}>
              <span className="hash">{g.isPod ? <PodShape members={g.members.size} size={18} /> : channelIcon(g)}</span>
              <span className="t"><b><GroupName g={g} /></b><small>{kind}{last ? ` · last message ${shortDay(last)}` : ''}</small></span>
              <ChevronRightIcon />
            </button>
          )
        })}
        {list.length > 0 && <p className="hint pad-x archived-foot">Read only. Nobody can post in an archived channel or group; its stewards can unarchive it.</p>}
      </div>
    </div>
  )
}

export function GroupPage({ id }: { id: string }) {
  useVersion()
  const state = useGroupState(id)
  const g = group(id)
  const [asking, setAsking] = useState(false)
  const [answer, setAnswer] = useState('')
  const parent = g?.parent ? group(g.parent) : undefined
  const open = () => go('c/' + encodeURIComponent(id))
  if (state === 'deleted') return <DeletedGroup />
  if (!g) {
    // nothing of it on this device and the relay has not answered (first load, offline): no guess
    return (
      <div className="screen">
        <div className="topbar"><button className="icon-btn" aria-label="Back" onClick={() => back('c')}>‹</button><h1>Group</h1></div>
        <div className="empty">This group isn’t on this device.</div>
      </div>
    )
  }
  const inIt = g.members.has(me())
  const mine = g.applications.find((a) => a.pubkey === me())
  const members = [...g.members]
  const admins = [...g.admins]
  const waiting = groupRequestsFor(g)
  return (
    <div className="screen group-page">
      <div className="topbar">
        <button className="icon-btn" aria-label="Back" onClick={() => back(parent ? 'c/' + encodeURIComponent(parent.id) : 'c')}>‹</button>
        <h1><span className="gp-title"><PodShape members={g.members.size} size={18} /> {g.name}</span>
          <small>Group{parent ? ` in ${parent.isPod ? '' : '# '}${parent.name}` : ''} · {g.closed ? 'by request' : 'open to members'}</small></h1>
        {!g.archived && <FollowButton channel={id} />}
      </div>
      {g.archived && <ArchivedBar g={g} />}
      <div className="scroll pad">
        {g.about ? <Markdown text={g.about} className="chead-about" /> : <p className="hint">No description yet.</p>}
        <div className="gp-action">
          {g.archived ? <button className="btn" onClick={open}>Read {g.name}</button> : inIt ? <>
            <span className="badge lime">You’re in</span>
            <button className="btn" onClick={open}>Open {g.name}</button>
          </> : mine ? <>
            <span className="badge">Pending</span>
            <span className="hint">Request sent{g.vouches > 0 ? ` · ${groupVouches(g, mine).length} of ${g.vouches} ${g.vouches === 1 ? 'vouch' : 'vouches'}` : ''}. {g.vouches > 0 ? 'Its members vouch, or its admins answer.' : 'Its admins will answer.'}</span>
          </> : g.closed ? <>
            <button className="btn" onClick={() => setAsking(true)}>Apply to join</button>
            <button className="btn ghost" onClick={open}>Have a look</button>
          </> : <>
            <Btn className="btn" onClick={async () => { await joinGroup(id); open() }}>Join</Btn>
            <button className="btn ghost" onClick={open}>Have a look</button>
          </>}
        </div>
        {!g.archived && <p className="hint gp-follow">The bell above: get its announcements by e-mail, whether you’re in or not.</p>}

        <div className="section-title">Who’s in <span>{members.length}</span></div>
        {members.length === 0 && <p className="hint">Nobody yet.</p>}
        {members.map((p) => (
          <button key={p} type="button" className="member member-open-row" onClick={() => openProfile(p, id)}>
            <Avatar pubkey={p} size={32} /><span className="t">{displayName(p)}{g.admins.has(p) && <span className="role-tag">Admin</span>}</span>
          </button>
        ))}
        {admins.some((a) => !g.members.has(a)) && <p className="hint">Also run by {admins.filter((a) => !g.members.has(a)).map((a) => displayName(a)).join(', ')}.</p>}

        {waiting.length > 0 && !g.archived && <>
          <div className="section-title"><span className="lbl"><VouchIcon /> Asking to join</span><span>{waiting.length}</span></div>
          {waiting.map((a) => <GroupRequest key={a.ev.id} g={g} a={a} />)}
        </>}
        <p className="hint">Group id <code>{g.id}</code>{parent ? <> · in {channelIcon(parent)} {parent.name}</> : null}</p>
      </div>
      {asking && (
        <Sheet title={`Apply to ${g.name}`} onClose={() => setAsking(false)}>
          <Form onSubmit={async (e) => { e.preventDefault(); await joinGroup(id, answer.trim()); setAsking(false) }}>
            <p className="apply-q">{g.prompt || DEFAULT_PROMPT}</p>
            <textarea className="input" rows={4} autoFocus value={answer} onChange={(e) => setAnswer(e.target.value)} />
            <div style={{ height: 12 }} />
            <button className="btn" disabled={!answer.trim()}>Send my request</button>
            <p className="hint" style={{ marginTop: 10 }}>{g.vouches > 0 ? `${g.vouches === 1 ? 'One member' : `${g.vouches} members`} vouching for you, or an admin, let you in.` : 'The group’s admins see your answer and let you in, or not.'}</p>
          </Form>
        </Sheet>
      )}
    </div>
  )
}
