import { useState } from 'react'
import { addTask, commentTask, editChannel, joinGroup, leaveGroup, setMemberLabels, taskAction } from '../core/actions'
import { channelIcon, displayName, groups, memberLabels, messages, tasklist, type Task } from '../core/model'
import { useVersion } from '../core/store'
import { me } from '../core/sync'
import { Avatar, back, go, MediaGrid, Sheet } from './common'

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
      <Tabs tab={tab} setTab={setTab} />
      <div className="scroll">
        {tab === 'about' && <About id={id} />}
        {tab === 'tasks' && <Tasks groupId={id} />}
        {tab === 'members' && <Members groupId={id} />}
        {tab === 'files' && <Files groupId={id} />}
      </div>
    </div>
  )
}

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
      <div className="card">
        <div className="hint">Members</div>
        <div>{g.members.size}</div>
      </div>
      {g.isPod && (g.members.has(me())
        ? <button className="btn ghost" style={{ marginBottom: 10 }} onClick={() => { if (confirm(`Leave ${g.name}?`)) void leaveGroup(id) }}>Leave pod</button>
        : <button className="btn" style={{ marginBottom: 10 }} onClick={() => void joinGroup(id)}>Join pod</button>)}
      <button className="btn ghost" onClick={() => { setName(g.name); setAbout(g.about); setEditing(true) }}>Edit name and description</button>
      <p className="hint" style={{ marginTop: 16 }}>{g.isPod ? 'Pod' : 'Channel'} id <code>{g.id}</code></p>
      {editing && (
        <Sheet title="Edit channel" onClose={() => setEditing(false)}>
          <form onSubmit={async (e) => { e.preventDefault(); await editChannel(id, name.trim(), about.trim()); setEditing(false) }}>
            <label className="field"><span>Name</span><input className="input" value={name} onChange={(e) => setName(e.target.value)} /></label>
            <label className="field"><span>Description</span><textarea className="input" rows={4} value={about} onChange={(e) => setAbout(e.target.value)} /></label>
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
  const members = [...new Set([...(g?.members ?? []), ...labels.keys(), me()])].filter(Boolean)
  const vocab = [...new Set([...labels.values()].flat())].sort()
  return (
    <div>
      {members.map((p) => (
        <button key={p} className="member" onClick={() => setEditing(p)}>
          <Avatar pubkey={p} />
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ fontWeight: 600 }}>{displayName(p)}</div>
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
  if (!media.length) return <div className="empty">Photos and files posted in this channel show up here.</div>
  return <div className="pad"><MediaGrid media={media} /></div>
}
