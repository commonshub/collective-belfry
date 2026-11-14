// #/c/<id>/group: a group's page (Xavier, 2026-11-14). The card a new group leaves in its parent
// channel opens it: what the group is, who is in it and who runs it, Follow (its announcements by
// e-mail, the bell), and Join (open) / Apply to join (closed) / Pending / You're in. A group is
// not membership of the belfry: whoever created it is its admin and lets people in.
import { useState } from 'react'
import { DEFAULT_PROMPT, joinGroup } from '../core/actions'
import { channelIcon, displayName, group } from '../core/model'
import { groupRequestsFor, groupVouches } from '../core/requests'
import { useVersion } from '../core/store'
import { me } from '../core/sync'
import { Avatar, back, Btn, Form, go, Sheet } from './common'
import { FollowButton } from './Follow'
import { PodShape, VouchIcon } from './icons'
import { Markdown } from './markdown'
import { openProfile } from './Profile'
import { GroupRequest } from './Requests'

export function GroupPage({ id }: { id: string }) {
  useVersion()
  const g = group(id)
  const [asking, setAsking] = useState(false)
  const [answer, setAnswer] = useState('')
  const parent = g?.parent ? group(g.parent) : undefined
  const open = () => go('c/' + encodeURIComponent(id))
  if (!g) {
    return (
      <div className="screen">
        <div className="topbar"><button className="icon-btn" aria-label="Back" onClick={() => back('c')}>‹</button><h1>Group</h1></div>
        <div className="empty">This group isn’t here (yet). It shows once the relay sends it.</div>
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
        <FollowButton channel={id} />
      </div>
      <div className="scroll pad">
        {g.about ? <Markdown text={g.about} className="chead-about" /> : <p className="hint">No description yet.</p>}
        <div className="gp-action">
          {inIt ? <>
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
        <p className="hint gp-follow">The bell above: get its announcements by e-mail, whether you’re in or not.</p>

        <div className="section-title">Who’s in <span>{members.length}</span></div>
        {members.length === 0 && <p className="hint">Nobody yet.</p>}
        {members.map((p) => (
          <button key={p} type="button" className="member member-open-row" onClick={() => openProfile(p)}>
            <Avatar pubkey={p} size={32} /><span className="t">{displayName(p)}{g.admins.has(p) && <span className="role-tag">Admin</span>}</span>
          </button>
        ))}
        {admins.some((a) => !g.members.has(a)) && <p className="hint">Also run by {admins.filter((a) => !g.members.has(a)).map((a) => displayName(a)).join(', ')}.</p>}

        {waiting.length > 0 && <>
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
