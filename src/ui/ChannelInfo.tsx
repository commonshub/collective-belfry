import { useState } from 'react'
import { setInbox } from '../core/actions'
import { isInbox } from '../core/model'
import { BELFRY } from '../config'
import { addTask, admit, commentTask, decline, editChannel, editPod, joinGroup, leaveGroup, setMemberLabels, taskAction } from '../core/actions'
import { ACTIVE_DAYS, channelIcon, displayName, groups, guestJoins, memberLabels, messages, recentlyActive, tasklist, type Task } from '../core/model'
import { useVersion } from '../core/store'
import { DropBox } from './Drop'
import { InviteSheet } from './Invite'
import { canAdmin } from '../core/access'
import { me } from '../core/sync'
import { Avatar, back, fmtTime, go, MediaGrid, Sheet } from './common'
import { AccessFields } from './Chat'

type Tab = 'about' | 'tasks' | 'members' | 'files'

export function ChannelInfo({ id, initial = 'about' }: { id: string; initial?: Tab }) {
  useVersion()
  const [tab, setTab] = useState<Tab>(initial)
  const g = groups().find((x) => x.id === id)
  return (
    <div className="screen">
      <div className="topbar">
        <button className="icon-btn" aria-label="Back" onClick={() => back('c/' + id)}>‹</button>
        <h1>{channelIcon(g)} {g?.name ?? id}</h1>
      </div>
      <ChannelInfoBody id={id} tab={tab} setTab={setTab} />
    </div>
  )
}

/** The tabs and their content (also the pulled-down channel header's full view). */
export function ChannelInfoBody({ id, tab, setTab }: { id: string; tab: Tab; setTab: (t: Tab) => void }) {
  return (
    <>
      <Tabs tab={tab} setTab={setTab} />
      <div className="scroll">
        {tab === 'about' && <About id={id} />}
        {tab === 'tasks' && <Tasks groupId={id} />}
        {tab === 'members' && <Members groupId={id} />}
        {tab === 'files' && <Files groupId={id} />}
      </div>
    </>
  )
}
export type InfoTab = Tab

export function Tabs<T extends string>({ tab, setTab, tabs }: { tab: T; setTab: (t: T) => void; tabs?: [T, string][] }) {
  const list = tabs ?? ([['about', 'About'], ['tasks', 'Tasks'], ['members', 'Members'], ['files', 'Files']] as [T, string][])
  return (
    <div className="seg">
      {list.map(([k, label]) => <button key={k} className={tab === k ? 'on' : ''} onClick={() => setTab(k)}>{label}</button>)}
    </div>
  )
}

function About({ id }: { id: string }) {
  const g = groups().find((x) => x.id === id)
  const [editing, setEditing] = useState(false)
  const [name, setName] = useState(g?.name ?? '')
  const [about, setAbout] = useState(g?.about ?? '')
  const [access, setAccess] = useState({ closed: !!g?.closed, prompt: g?.prompt ?? '' })
  const [openMail, setOpenMail] = useState(false)
  const [inviting, setInviting] = useState(false)
  if (!g) return null
  return (
    <div className="pad">
      <div className="card">
        <div className="hint" style={{ marginBottom: 4 }}>Description</div>
        <div style={{ whiteSpace: 'pre-wrap' }}>{g.about || <span className="hint">No description yet.</span>}</div>
      </div>
      {g.isPod && (
        <div className="card">
          <div className="hint">Pod in</div>
          <button className="link" onClick={() => go('c/' + encodeURIComponent(g.parent ?? 'general'))}>
            {channelIcon(groups().find((x) => x.id === g.parent))} {groups().find((x) => x.id === g.parent)?.name ?? g.parent}
          </button>
        </div>
      )}
      <ActiveCard id={id} />
      <div className="card">
        <div className="hint">Members</div>
        <div>{g.members.size}</div>
      </div>
      {g.isPod && (
        <div className="card">
          <div className="hint">Who can join</div>
          <div>{g.closed ? 'Closed: apply, admins admit' : 'Open: any member joins'}</div>
          {g.closed && <div className="hint" style={{ marginTop: 6, whiteSpace: 'pre-wrap' }}>Question: {g.prompt || '—'}</div>}
        </div>
      )}
      {g.isPod && (g.members.has(me())
        ? <button className="btn ghost" style={{ marginBottom: 10 }} onClick={() => { if (confirm(`Leave ${g.name}?`)) void leaveGroup(id) }}>Leave pod</button>
        : !g.closed && <button className="btn" style={{ marginBottom: 10 }} onClick={() => void joinGroup(id)}>Join pod</button>)}
      <div className="card">
        <div className="hint">E-mail</div>
        <div>{isInbox(g) ? <>Anyone can e-mail <b>{g.id}@{BELFRY.domain}</b>: mails land here as cards, and the channel's admins answer as the channel.</> : <>Members can e-mail <b>{g.id}@{BELFRY.domain}</b>: their mail shows as their message.</>}</div>
      </div>
      <div className="row" style={{ gap: 8 }}>
        <button className="btn" style={{ flex: 1 }} onClick={() => setInviting(true)}>Invite</button>
        {canAdmin(g) && <button className="btn ghost" style={{ flex: 1 }} onClick={() => { setName(g.name); setAbout(g.about); setAccess({ closed: g.closed, prompt: g.prompt ?? '' }); setOpenMail(isInbox(g)); setEditing(true) }}>Settings</button>}
      </div>
      {inviting && <InviteSheet id={id} onClose={() => setInviting(false)} />}
      <p className="hint" style={{ marginTop: 16 }}>{g.isPod ? 'Pod' : 'Channel'} id <code>{g.id}</code></p>
      {editing && (
        <Sheet title={`${g.isPod ? 'Pod' : 'Channel'} settings`} onClose={() => setEditing(false)}>
          <form onSubmit={async (e) => {
            e.preventDefault()
            if (name.trim() !== g.name || about.trim() !== g.about || g.isPod)
              await (g.isPod ? editPod(id, name.trim(), about.trim(), access) : editChannel(id, name.trim(), about.trim()))
            if (openMail !== isInbox(g)) await setInbox(id, openMail)
            setEditing(false)
          }}>
            <label className="field"><span>Name</span><input className="input" value={name} onChange={(e) => setName(e.target.value)} /></label>
            <label className="field"><span>Description</span><textarea className="input" rows={4} value={about} onChange={(e) => setAbout(e.target.value)} /></label>
            {g.isPod && <AccessFields closed={access.closed} prompt={access.prompt} set={setAccess} />}
            <label className="switch-row">
              <span><b>E-mails from anyone</b><small>Mail to {g.id}@{BELFRY.domain} from outside lands here as cards; admins answer as the channel. Off: only members' mail comes in.</small></span>
              <input type="checkbox" role="switch" checked={openMail} onChange={(e) => setOpenMail(e.target.checked)} />
            </label>
            <button className="btn" disabled={!name.trim()}>Save</button>
          </form>
        </Sheet>
      )}
    </div>
  )
}

// ---- tasks (tasklist.sh protocol) ---------------------------------------------------

export function Tasks({ groupId }: { groupId: string }) {
  useVersion()
  const { tasks } = tasklist(groupId)
  const [title, setTitle] = useState('')
  const [open, setOpen] = useState<string | null>(null)
  const todo = tasks.filter((t) => !t.done)
  const done = tasks.filter((t) => t.done)
  return (
    <div>
      <form className="row pad" onSubmit={async (e) => { e.preventDefault(); if (title.trim()) { await addTask(groupId, title.trim()); setTitle('') } }}>
        <input className="input" placeholder="Add a task…" value={title} onChange={(e) => setTitle(e.target.value)} />
        <button className="btn small" disabled={!title.trim()}>Add</button>
      </form>
      {tasks.length === 0 && <div className="empty">No tasks yet.</div>}
      {[...todo, ...done].map((t) => (
        <TaskRow key={t.id} t={t} groupId={groupId} open={open === t.id} toggle={() => setOpen(open === t.id ? null : t.id)} />
      ))}
    </div>
  )
}

function TaskRow({ t, groupId, open, toggle }: { t: Task; groupId: string; open: boolean; toggle: () => void }) {
  const [comment, setComment] = useState('')
  const mine = t.assignee === me()
  return (
    <div className={'task' + (t.done ? ' done' : '')} style={{ opacity: t.pending ? 0.6 : 1 }}>
      <button className={'check' + (t.done ? ' done' : t.assignee ? ' claimed' : '')} aria-label={t.done ? 'Mark as not done' : 'Mark as done'}
        onClick={() => taskAction(groupId, t.id, t.done ? 'undone' : 'done')}>{t.done ? '✓' : ''}</button>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div className="title" onClick={toggle}>{t.title}</div>
        <small onClick={toggle}>
          {t.assignee ? `${displayName(t.assignee)} ${t.assignee === me() ? 'are' : 'is'} on it` : 'Unassigned'}
          {t.comments.length ? ` · 💬 ${t.comments.length}` : ''}
        </small>
        {open && (
          <div style={{ marginTop: 8 }}>
            {t.comments.map((c, i) => <div key={i} style={{ fontSize: 14, margin: '4px 0' }}><b>{displayName(c.author)}</b> {c.text}</div>)}
            <div className="row" style={{ margin: '8px 0', flexWrap: 'wrap' }}>
              <button className="btn small ghost" onClick={() => taskAction(groupId, t.id, mine ? 'unclaim' : 'claim')}>
                {mine ? 'Unassign me' : t.assignee ? 'Take over' : "I'll take it"}
              </button>
              {t.author === me() && <button className="btn small ghost" onClick={() => taskAction(groupId, t.id, 'delete')}>Delete</button>}
            </div>
            <form className="row" onSubmit={async (e) => { e.preventDefault(); if (comment.trim()) { await commentTask(groupId, t.id, comment.trim()); setComment('') } }}>
              <input className="input" placeholder="Comment…" value={comment} onChange={(e) => setComment(e.target.value)} />
              <button className="btn small" disabled={!comment.trim()}>Send</button>
            </form>
          </div>
        )}
      </div>
    </div>
  )
}

// ---- members and their labels (NIP-32) ---------------------------------------------

export function Members({ groupId }: { groupId: string }) {
  useVersion()
  const g = groups().find((x) => x.id === groupId)
  const labels = memberLabels(groupId)
  const [editing, setEditing] = useState<string | null>(null)
  const members = [...new Set([...(g?.members ?? []), ...labels.keys(), ...(g?.isPod ? [] : [me()])])].filter(Boolean)
  const vocab = [...new Set([...labels.values()].flat())].sort()
  const reviewing = g?.isPod && canAdmin(g) ? g.applications : []
  // guests (people or agents) and who brought them in: whose agent it is
  const invited = new Map(guestJoins(groupId).map((j) => [j.pubkey, j]))
  return (
    <div>
      {reviewing.length > 0 && <div className="section-title">Asking to join <span>{reviewing.length}</span></div>}
      {reviewing.map((a) => (
        <div key={a.pubkey} className="appl">
          <div className="who"><Avatar pubkey={a.pubkey} size={28} />{displayName(a.pubkey)}<span className="hint" style={{ fontWeight: 400, fontSize: 12 }}>{fmtTime(a.at)}</span></div>
          {g?.prompt && <div className="hint" style={{ fontSize: 13, marginTop: 6 }}>{g.prompt}</div>}
          <blockquote>{a.answer || '(no answer)'}</blockquote>
          <div className="row">
            <button className="btn small" onClick={() => void admit(groupId, a.pubkey)}>Admit</button>
            <button className="btn small ghost" onClick={() => { if (confirm(`Decline ${displayName(a.pubkey)}?`)) void decline(groupId, a.pubkey) }}>Decline</button>
          </div>
        </div>
      ))}
      {reviewing.length > 0 && <div className="section-title">Members <span>{g?.members.size}</span></div>}
      {members.map((p) => (
        <button key={p} className="member" onClick={() => setEditing(p)}>
          <Avatar pubkey={p} />
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ fontWeight: 600 }}>{displayName(p)}</div>
            {invited.get(p) && (
              <div className="hint invited-by">
                <span className="chip">{invited.get(p)!.agent ? 'agent' : invited.get(p)!.unsaved ? 'guest · not saved' : 'guest'}</span>
                {invited.get(p)!.by ? <>invited by <b>{displayName(invited.get(p)!.by!)}</b></> : null}
              </div>
            )}
            <div>{(labels.get(p) ?? []).map((l) => <span key={l} className="chip on">{l}</span>)}
              {!(labels.get(p) ?? []).length && <span className="hint" style={{ fontSize: 13 }}>Add a label</span>}</div>
          </div>
        </button>
      ))}
      {editing && <LabelEditor groupId={groupId} pubkey={editing} current={labels.get(editing) ?? []} vocab={vocab} onClose={() => setEditing(null)} />}
    </div>
  )
}

const SUGGESTED = ['organiser', 'speaker', 'volunteer', 'maintainer', 'host', 'newcomer']

function LabelEditor({ groupId, pubkey, current, vocab, onClose }: {
  groupId: string; pubkey: string; current: string[]; vocab: string[]; onClose: () => void
}) {
  const [sel, setSel] = useState(current)
  const [extra, setExtra] = useState('')
  const options = [...new Set([...vocab, ...SUGGESTED, ...sel])]
  const flip = (l: string) => setSel(sel.includes(l) ? sel.filter((x) => x !== l) : [...sel, l])
  return (
    <Sheet title={`Labels for ${displayName(pubkey)}`} onClose={onClose}>
      <div style={{ marginBottom: 14 }}>
        {options.map((l) => <button key={l} className={'chip' + (sel.includes(l) ? ' on' : '')} onClick={() => flip(l)}>{l}</button>)}
      </div>
      <form className="row" style={{ marginBottom: 14 }} onSubmit={(e) => {
        e.preventDefault()
        const l = extra.trim().toLowerCase().slice(0, 32)
        if (l && !sel.includes(l)) setSel([...sel, l])
        setExtra('')
      }}>
        <input className="input" placeholder="New label" value={extra} onChange={(e) => setExtra(e.target.value)} />
        <button className="btn small ghost">Add</button>
      </form>
      <button className="btn" onClick={async () => { await setMemberLabels(groupId, pubkey, sel); onClose() }}>Save labels</button>
    </Sheet>
  )
}

// ---- files: every photo and file posted in the channel --------------------------------

export function Files({ groupId }: { groupId: string }) {
  useVersion()
  const media = messages(groupId).flatMap((m) => m.media).reverse()
  return (
    <div className="pad">
      <DropBox groupId={groupId} />
      {media.length ? <MediaGrid media={media} /> : <div className="empty">Photos and files posted in this channel show up here.</div>}
    </div>
  )
}

/** Who opened or wrote in this channel in the last 30 days: how many people a message reaches. */
function ActiveCard({ id }: { id: string }) {
  const active = recentlyActive(id)
  const ago = (at: number) => {
    const d = Math.floor((Date.now() / 1000 - at) / 86400)
    return d < 1 ? 'today' : d === 1 ? 'yesterday' : `${d} days ago`
  }
  return (
    <div className="card">
      <div className="hint" style={{ marginBottom: 6 }}>Active in the last {ACTIVE_DAYS} days · {active.length}</div>
      {active.length === 0 && <div className="hint">Nobody opened this channel yet.</div>}
      {active.map((a) => (
        <div key={a.pubkey} className="row" style={{ gap: 10, padding: '6px 0' }}>
          <Avatar pubkey={a.pubkey} size={28} />
          <span style={{ flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{displayName(a.pubkey)}</span>
          <span className="hint" style={{ fontSize: 13 }}>{ago(a.at)}</span>
        </div>
      ))}
      <div className="hint" style={{ fontSize: 13, marginTop: 6 }}>Opening a channel tells its members you saw it (at most every few hours).</div>
    </div>
  )
}
