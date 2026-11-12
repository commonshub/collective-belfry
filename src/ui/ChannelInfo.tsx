import { lazy, Suspense, useState } from 'react'
import { extras, placeLabel } from '../core/people'
import { Markdown } from './markdown'
import { openProfile } from './Profile'

const MembersMap = lazy(() => import('./MembersMap'))
import { setInbox, setWebPage } from '../core/actions'
import { isInbox, mailLocal, slugify, webPage, WEB_RESERVED, type WebPage } from '../core/model'
import { BELFRY } from '../config'
import { addTask, admit, commentTask, decline, editChannel, editPod, joinGroup, leaveGroup, setMemberLabels, taskAction } from '../core/actions'
import { ACTIVE_DAYS, channelIcon, displayName, groups, guestJoins, memberLabels, messages, recentlyActive, tasklist, isWorking, workingSince, type Task } from '../core/model'
import { useVersion } from '../core/store'
import { DropBox } from './Drop'
import { InviteSheet } from './Invite'
import { canAdmin } from '../core/access'
import { me } from '../core/sync'
import { Avatar, back, fmtTime, go, MediaGrid, Sheet, Btn, Form, useNow, WorkRing } from './common'
import { AccessFields, stepGroups, stepText } from './Chat'

type Tab = 'about' | 'tasks' | 'members' | 'files' | 'log'

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
export function ChannelInfoBody({ id, tab, setTab, tabs }: { id: string; tab: Tab; setTab: (t: Tab) => void; tabs?: [Tab, string][] }) {
  return (
    <>
      <Tabs tab={tab} setTab={setTab} tabs={tabs} />
      <div className="scroll">
        <Summary id={id} tab={tab} open={setTab} />
        {tab === 'about' && <About id={id} />}
        {tab === 'tasks' && <Tasks groupId={id} />}
        {tab === 'members' && <Members groupId={id} />}
        {tab === 'files' && <Files groupId={id} />}
        {tab === 'log' && <Log groupId={id} />}
      </div>
    </>
  )
}
export type InfoTab = Tab

export function Tabs<T extends string>({ tab, setTab, tabs }: { tab: T; setTab: (t: T) => void; tabs?: [T, string][] }) {
  const list = tabs ?? ([['about', 'About'], ['tasks', 'To-dos'], ['members', 'Members'], ['files', 'Files'], ['log', 'Log']] as [T, string][])
  return (
    <div className="seg">
      {list.map(([k, label]) => <button key={k} className={tab === k ? 'on' : ''} onClick={() => setTab(k)}>{label}</button>)}
    </div>
  )
}

/** COMPACT: under the pulled-down header, which shows the members and photos itself. */
export function About({ id, compact = false }: { id: string; compact?: boolean }) {
  const g = groups().find((x) => x.id === id)
  const [editing, setEditing] = useState(false)
  const [name, setName] = useState(g?.name ?? '')
  const [about, setAbout] = useState(g?.about ?? '')
  const [access, setAccess] = useState({ closed: !!g?.closed, prompt: g?.prompt ?? '' })
  const [openMail, setOpenMail] = useState(false)
  const [web, setWeb] = useState<WebPage>({ on: false, slug: '', visibility: 'public' })
  const [webError, setWebError] = useState('')
  const [inviting, setInviting] = useState(false)
  if (!g) return null
  return (
    <div className="pad">
      {g.isPod && !compact && (
        <div className="card">
          <div className="hint">Pod in</div>
          <button className="link" onClick={() => go('c/' + encodeURIComponent(g.parent ?? 'general'))}>
            {channelIcon(groups().find((x) => x.id === g.parent))} {groups().find((x) => x.id === g.parent)?.name ?? g.parent}
          </button>
        </div>
      )}
      {compact && g.about.trim().includes('\n') && <div className="card"><div className="hint">Description</div><Markdown text={g.about} /></div>}
      {!compact && <ActiveCard id={id} />}
      {!compact && <div className="card">
        <div className="hint">Members</div>
        <div>{g.members.size}</div>
      </div>}
      {g.isPod && (
        <div className="card">
          <div className="hint">Who can join</div>
          <div>{g.closed ? 'Closed: apply, admins admit' : 'Open: any member joins'}</div>
          {g.closed && <div className="hint" style={{ marginTop: 6, whiteSpace: 'pre-wrap' }}>Question: {g.prompt || '—'}</div>}
        </div>
      )}
      {g.isPod && (g.members.has(me())
        ? <button className="btn ghost" style={{ marginBottom: 10 }} onClick={() => { if (confirm(`Leave ${g.name}?`)) void leaveGroup(id) }}>Leave pod</button>
        : !g.closed && <Btn className="btn" style={{ marginBottom: 10 }} onClick={() => joinGroup(id)}>Join pod</Btn>)}
      <div className="card">
        <div className="hint">E-mail</div>
        <div>{isInbox(g) ? <>Anyone can e-mail <b>{mailLocal(g)}@{BELFRY.domain}</b>: mails land here as cards, and the channel's admins answer as the channel.</> : <>Members can e-mail <b>{mailLocal(g)}@{BELFRY.domain}</b>: their mail shows as their message.</>}</div>
      </div>
      <div className="row" style={{ gap: 8 }}>
        <button className="btn" style={{ flex: 1 }} onClick={() => setInviting(true)}>Invite</button>
        {canAdmin(g) && <button className="btn ghost" style={{ flex: 1 }} onClick={() => { setName(g.name); setAbout(g.about); setAccess({ closed: g.closed, prompt: g.prompt ?? '' }); setOpenMail(isInbox(g)); setWeb(webPage(g) ?? { on: false, slug: slugify(g.name), visibility: 'public' }); setWebError(''); setEditing(true) }}>Settings</button>}
      </div>
      {inviting && <InviteSheet id={id} onClose={() => setInviting(false)} />}
      {webPage(g)?.on && <p className="hint web-page-link" style={{ marginTop: 16 }}>{webPage(g)!.visibility === 'members' ? 'Members only' : 'Public'} · web page: <a href={`https://${BELFRY.domain}/${webPage(g)!.slug}`} target="_blank" rel="noreferrer">{BELFRY.domain}/{webPage(g)!.slug}</a></p>}
      <p className="hint" style={{ marginTop: 16 }}>{g.isPod ? 'Pod' : 'Channel'} id <code>{g.id}</code></p>
      {editing && (
        <Sheet title={`${g.isPod ? 'Pod' : 'Channel'} settings`} onClose={() => setEditing(false)}>
          <Form onSubmit={async (e) => {
            e.preventDefault()
            if (name.trim() !== g.name || about.trim() !== g.about || g.isPod)
              await (g.isPod ? editPod(id, name.trim(), about.trim(), access) : editChannel(id, name.trim(), about.trim()))
            if (web.on) {
              const slug = slugify(web.slug)
              if (slug.length < 2) { setWebError('Give the page an address: two letters or more.'); return }
              if (WEB_RESERVED.has(slug)) { setWebError(`${BELFRY.domain}/${slug} is taken by ${BELFRY.domain} itself: pick another address.`); return }
              const taken = groups().find((x) => x.id !== id && webPage(x)?.on && webPage(x)?.slug === slug)
              if (taken) { setWebError(`${BELFRY.domain}/${slug} is the page of #${taken.name}: pick another address.`); return }
              web.slug = slug
            }
            if (openMail !== isInbox(g)) await setInbox(id, openMail)
            const was = webPage(g)
            if (web.on !== !!was?.on || web.slug !== (was?.slug ?? web.slug) || web.visibility !== (was?.visibility ?? 'public')) await setWebPage(id, web)
            setEditing(false)
          }}>
            <label className="field"><span>Name</span><input className="input" value={name} onChange={(e) => setName(e.target.value)} /></label>
            <label className="field"><span>Description · Markdown works, links become clickable</span><textarea className="input" rows={4} value={about} onChange={(e) => setAbout(e.target.value)} /></label>
            {g.isPod && <AccessFields closed={access.closed} prompt={access.prompt} set={setAccess} />}
            {!g.isPod && <div className="field visibility"><span>Visibility</span>
              <label className="toggle"><input type="radio" name="visibility" checked={web.visibility === 'public'} onChange={() => setWeb({ ...web, visibility: 'public' })} /> <span><b>Public</b> · anyone reads its threads on the web</span></label>
              <label className="toggle"><input type="radio" name="visibility" checked={web.visibility === 'members'} onChange={() => setWeb({ ...web, visibility: 'members' })} /> <span><b>Members only</b> · people from outside see only their own thread</span></label>
            </div>}
            <label className="switch-row">
              <span><b>E-mails from anyone</b><small>Mail to {mailLocal(g)}@{BELFRY.domain} from outside lands here as cards; admins answer as the channel. Off: only members' mail comes in.</small></span>
              <input type="checkbox" role="switch" checked={openMail} onChange={(e) => setOpenMail(e.target.checked)} />
            </label>
            <label className="switch-row">
              <span><b>Threads from the web</b><small>A public page at {BELFRY.domain}/{slugify(web.slug) || '…'} where anyone opens a thread: it lands here as a card, and members' replies are mailed back to its author.</small></span>
              <input type="checkbox" role="switch" checked={web.on} onChange={(e) => setWeb({ ...web, on: e.target.checked })} />
            </label>
            {web.on && (
              <div className="web-page-fields">
                <label className="field"><span>Address</span>
                  <span className="row" style={{ gap: 6, alignItems: 'center' }}><span className="hint" style={{ whiteSpace: 'nowrap' }}>{BELFRY.domain}/</span>
                    <input className="input" aria-label="Page address" value={web.slug} onChange={(e) => { setWebError(''); setWeb({ ...web, slug: e.target.value.toLowerCase() }) }} /></span>
                </label>
                {webError && <p className="error">{webError}</p>}
              </div>
            )}
            <button className="btn" disabled={!name.trim()}>Save</button>
          </Form>
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
      <Form className="row pad" onSubmit={async (e) => { e.preventDefault(); if (title.trim()) { await addTask(groupId, title.trim()); setTitle('') } }}>
        <input className="input" placeholder="Add a to-do…" value={title} onChange={(e) => setTitle(e.target.value)} />
        <button className="btn small" disabled={!title.trim()}>Add</button>
      </Form>
      {tasks.length === 0 && <div className="empty">No to-dos yet.</div>}
      {[...todo, ...done].map((t) => (
        <TaskRow key={t.id} t={t} groupId={groupId} open={open === t.id} toggle={() => setOpen(open === t.id ? null : t.id)} />
      ))}
    </div>
  )
}

function TaskRow({ t, groupId, open, toggle }: { t: Task; groupId: string; open: boolean; toggle: () => void }) {
  const [comment, setComment] = useState('')
  const mine = t.assignee === me()
  const now = useNow()
  const busy = isWorking(t, now)
  return (
    <div className={'task' + (t.done ? ' done' : '')} style={{ opacity: t.pending ? 0.6 : 1 }}>
      <button className={'check' + (t.done ? ' done' : t.assignee ? ' claimed' : '') + (busy ? ' working' : '')} aria-label={t.done ? 'Mark as not done' : 'Mark as done'}
        onClick={() => taskAction(groupId, t.id, t.done ? 'undone' : 'done')}>{t.done ? '✓' : busy ? <WorkRing since={workingSince(t) ?? now} now={now} /> : ''}</button>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div className="title" onClick={toggle}>{t.title}</div>
        <small onClick={toggle}>
          {t.assignee ? `${displayName(t.assignee)} ${t.assignee === me() ? 'are' : 'is'} ${busy ? 'working on it now' : 'on it'}` : 'Unassigned'}
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
            <Form className="row" onSubmit={async (e) => { e.preventDefault(); if (comment.trim()) { await commentTask(groupId, t.id, comment.trim()); setComment('') } }}>
              <input className="input" placeholder="Comment…" value={comment} onChange={(e) => setComment(e.target.value)} />
              <button className="btn small" disabled={!comment.trim()}>Send</button>
            </Form>
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
  const invited = new Map(guestJoins(groupId).filter((j) => !j.member).map((j) => [j.pubkey, j]))
  const ex = extras()
  const mapped = members.filter((p) => ex.get(p)?.place?.lat !== undefined)
  const isMember = !groups().flatMap((x) => guestJoins(x.id)).some((j) => j.pubkey === me() && !j.member)
  return (
    <div>
      {reviewing.length > 0 && <div className="section-title">Asking to join <span>{reviewing.length}</span></div>}
      {reviewing.map((a) => (
        <div key={a.pubkey} className="appl">
          <div className="who"><Avatar pubkey={a.pubkey} size={28} />{displayName(a.pubkey)}<span className="hint" style={{ fontWeight: 400, fontSize: 12 }}>{fmtTime(a.at)}</span></div>
          {g?.prompt && <div className="hint" style={{ fontSize: 13, marginTop: 6 }}>{g.prompt}</div>}
          <blockquote>{a.answer || '(no answer)'}</blockquote>
          <div className="row">
            <Btn className="btn small" onClick={() => admit(groupId, a.pubkey)}>Admit</Btn>
            <button className="btn small ghost" onClick={() => { if (confirm(`Decline ${displayName(a.pubkey)}?`)) void decline(groupId, a.pubkey) }}>Decline</button>
          </div>
        </div>
      ))}
      {reviewing.length > 0 && <div className="section-title">Members <span>{g?.members.size}</span></div>}
      <Suspense fallback={<div className="members-map" />}>
        {mapped.length > 0
          ? <MembersMap pubkeys={members} onOpen={openProfile} />
          : <p className="hint pad-x">Nobody here shares their city yet. {isMember ? <button className="link" onClick={() => openProfile(me())}>Add yours</button> : null}</p>}
      </Suspense>
      {members.map((p) => (
        <div key={p} className="member">
          <button className="member-open" onClick={() => openProfile(p)} aria-label={`${displayName(p)}: profile`}><Avatar pubkey={p} /></button>
          <div style={{ flex: 1, minWidth: 0 }}>
            <button className="member-name" onClick={() => openProfile(p)}>{displayName(p)}</button>
            {ex.get(p)?.place && <div className="hint member-place">{placeLabel(ex.get(p)!.place)}</div>}
            {invited.get(p) && (
              <div className="hint invited-by">
                <span className="chip">{invited.get(p)!.agent ? 'agent' : invited.get(p)!.unsaved ? 'guest · not saved' : 'guest'}</span>
                {invited.get(p)!.by ? <>invited by <b>{displayName(invited.get(p)!.by!)}</b></> : null}
              </div>
            )}
            <button className="member-labels" onClick={() => setEditing(p)} aria-label={`Labels for ${displayName(p)} in this channel`}>
              {(labels.get(p) ?? []).map((l) => <span key={l} className="chip on">{l}</span>)}
              {!(labels.get(p) ?? []).length && <span className="hint" style={{ fontSize: 13 }}>Add a label</span>}</button>
          </div>
        </div>
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
      <Form className="row" style={{ marginBottom: 14 }} onSubmit={(e) => {
        e.preventDefault()
        const l = extra.trim().toLowerCase().slice(0, 32)
        if (l && !sel.includes(l)) setSel([...sel, l])
        setExtra('')
      }}>
        <input className="input" placeholder="New label" value={extra} onChange={(e) => setExtra(e.target.value)} />
        <button className="btn small ghost">Add</button>
      </Form>
      <Btn className="btn" onClick={async () => { await setMemberLabels(groupId, pubkey, sel); onClose() }}>Save labels</Btn>
    </Sheet>
  )
}

// ---- log: what happened to the channel's to-dos, newest first ------------------------------

export function Log({ groupId }: { groupId: string }) {
  useVersion()
  const steps = stepGroups(tasklist(groupId).tasks).sort((a, b) => b.at - a.at).slice(0, 200)
  if (!steps.length) return <div className="empty">Nothing happened to this channel's to-dos yet.</div>
  return (
    <div className="pad log-list">
      {steps.map((s, i) => (
        <div key={s.t.id + i} className="log-row">
          <Avatar pubkey={s.who} size={24} />
          <span><b>{displayName(s.who)}</b> {stepText(s.what)}: “{s.t.title}”</span>
          <time className="hint">{fmtTime(s.at)}</time>
        </div>
      ))}
    </div>
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

// ---- the top of each tab of the channel's page: a few lines that sum it up ----------------------

function Summary({ id, tab, open }: { id: string; tab: Tab; open: (t: Tab) => void }) {
  useVersion()
  const g = groups().find((x) => x.id === id)
  if (!g) return null
  const media = messages(id).flatMap((m) => m.media).reverse()
  const photos = media.filter((m) => m.mime.startsWith('image/'))
  const files = media.length - photos.length
  const active = recentlyActive(id)
  const n = 14
  if (tab === 'about') return (
    <div className="summary pad-x">
      {g.about ? <Markdown text={g.about} className="chead-about" /> : <p className="hint chead-about">No description yet.</p>}
      {active.length > 0 && (
        <button className="chead-people" onClick={() => open('members')} aria-label={`${active.length} active this month: see the members`}>
          {active.slice(0, n).map((a) => <Avatar key={a.pubkey} pubkey={a.pubkey} size={28} />)}
          {active.length > n && <span className="more">+{active.length - n}</span>}
          <span className="hint" style={{ fontSize: 13, marginLeft: 8 }}>{active.length} active this month</span>
        </button>
      )}
    </div>
  )
  if (tab === 'members') {
    const all = [...g.members]
    return (
      <div className="summary pad-x">
        <div className="chead-people">
          {all.slice(0, n).map((p) => <button key={p} className="face" onClick={() => openProfile(p)} aria-label={displayName(p)}><Avatar pubkey={p} size={28} /></button>)}
          {all.length > n && <span className="more">+{all.length - n}</span>}
          <span className="hint" style={{ fontSize: 13, marginLeft: 8 }}>{all.length} {all.length === 1 ? 'member' : 'members'}</span>
        </div>
      </div>
    )
  }
  if (tab === 'files') return (
    <div className="summary pad-x">
      {media.length === 0 ? <p className="hint">No photos or files yet.</p> : (
        <div className="chead-media">
          {photos.slice(0, 12).map((m) => <span key={m.url} className="thumb"><img src={m.url} alt={m.name || 'Photo'} loading="lazy" /></span>)}
          <span className="chip">{photos.length} {photos.length === 1 ? 'photo' : 'photos'}{files ? ` · ${files} ${files === 1 ? 'file' : 'files'}` : ''}</span>
        </div>
      )}
    </div>
  )
  const { tasks } = tasklist(id)
  const todo = tasks.filter((t) => !t.done)
  return (
    <div className="summary pad-x">
      <p className="hint" style={{ margin: 0 }}>{todo.length} open · {tasks.length - todo.length} done</p>
    </div>
  )
}
