import { lazy, Suspense, useState } from 'react'
import { extras, placeLabel } from '../core/people'
import { Markdown } from './markdown'
import { openProfile } from './Profile'

const MembersMap = lazy(() => import('./MembersMap'))
import { setInbox, setWebPage } from '../core/actions'
import { isInbox, mailLocal, slugify, webPage, WEB_RESERVED, type WebPage } from '../core/model'
import { BELFRY, NAME } from '../config'
import { addTask, admit, commentTask, decline, editChannel, editPod, joinGroup, leaveGroup, setMemberLabels, taskAction } from '../core/actions'
import { ACTIVE_DAYS, channelIcon, displayName, groups, guestJoins, memberLabels, messages, recentlyActive, tasklist, isWorking, workingSince, type Task } from '../core/model'
import { useVersion } from '../core/store'
import { DropBox } from './Drop'
import { InviteSheet } from './Invite'
import { canAdmin } from '../core/access'
import { me } from '../core/sync'
import { Avatar, back, devMode, fmtDay, fmtTime, go, Guard, MediaGrid, Sheet, Btn, Form, useNow, WorkRing } from './common'
import { all, type Ev } from '../core/store'
import { isAdmin } from '../core/access'
import { AccessFields } from './Chat'
import { GearIcon } from './icons'

type Tab = 'about' | 'tasks' | 'members' | 'files' | 'log'

export function ChannelInfo({ id, initial = 'about' }: { id: string; initial?: Tab }) {
  useVersion()
  const [tab, setTab] = useState<Tab>(initial)
  const [settings, setSettings] = useState(false)
  const g = groups().find((x) => x.id === id)
  return (
    <div className="screen">
      <div className="topbar">
        <button className="icon-btn" aria-label="Back" onClick={() => back('c/' + id)}>‹</button>
        <h1>{channelIcon(g)} {g?.name ?? id}</h1>
        {g && canAdmin(g) && <button className="icon-btn" aria-label="Settings" onClick={() => setSettings(true)}><GearIcon /></button>}
      </div>
      <ChannelInfoBody id={id} tab={tab} setTab={setTab} />
      {settings && <ChannelSettingsSheet id={id} onClose={() => setSettings(false)} />}
    </div>
  )
}

/** The tabs and their content (also the pulled-down channel header's full view). */
export function ChannelInfoBody({ id, tab, setTab, tabs }: { id: string; tab: Tab; setTab: (t: Tab) => void; tabs?: [Tab, string][] }) {
  return (
    <>
      <Tabs tab={tab} setTab={setTab} tabs={tabs} />
      <div className="scroll">
        {tab !== 'about' && tab !== 'log' && <Summary id={id} tab={tab} open={setTab} />}
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
  const list = tabs ?? ([['about', 'About'], ['members', 'Members'], ['tasks', 'Todos'], ['files', 'Files'], ['log', 'Log']] as [T, string][])
  return (
    <div className="seg">
      {list.map(([k, label]) => <button key={k} className={tab === k ? 'on' : ''} onClick={() => setTab(k)}>{label}</button>)}
    </div>
  )
}

/** About a channel (Xavier, 2026-11-12: no boxes): its description, "2/12 active members" with
 *  an invite link, the e-mail line, its web page. Settings: the gear in the header
 *  (ChannelSettingsSheet). COMPACT: under the pulled-down header, which shows the description,
 *  members and photos itself. */
export function About({ id, compact = false }: { id: string; compact?: boolean }) {
  useVersion()
  const g = groups().find((x) => x.id === id)
  const [inviting, setInviting] = useState(false)
  const [settings, setSettings] = useState(false)
  if (!g) return null
  const active = recentlyActive(id)
  const parent = groups().find((x) => x.id === g.parent)
  return (
    <div className="pad about">
      {!compact && (g.about ? <Markdown text={g.about} className="chead-about" /> : <p className="hint">No description yet.</p>)}
      {compact && g.about.trim().includes('\n') && <Markdown text={g.about} className="chead-about" />}
      {!compact && (
        <div className="about-line">
          <button className="about-faces" onClick={() => go(`c/${encodeURIComponent(id)}/members`)} aria-label="See the members">
            {active.slice(0, 6).map((a) => <Avatar key={a.pubkey} pubkey={a.pubkey} size={24} />)}
            <span><b>{active.length}/{g.members.size}</b> active members</span>
          </button>
          <span className="sep">·</span>
          <button className="link" onClick={() => setInviting(true)}>Invite</button>
        </div>
      )}
      {compact && <p className="about-line"><button className="link" onClick={() => setInviting(true)}>Invite people</button>{canAdmin(g) && <> · <button className="link" onClick={() => setSettings(true)}>Settings</button></>}</p>}
      {g.isPod && !compact && parent && (
        <p className="about-line">Pod in <button className="link" onClick={() => go('c/' + encodeURIComponent(parent.id))}>{channelIcon(parent)} {parent.name}</button> · {g.closed ? 'closed: apply, admins admit' : 'open: any member joins'}</p>
      )}
      {g.isPod && g.closed && !compact && <p className="hint" style={{ whiteSpace: 'pre-wrap' }}>Question: {g.prompt || '—'}</p>}
      {g.isPod && (g.members.has(me())
        ? <button className="btn ghost" style={{ marginBottom: 10 }} onClick={() => { if (confirm(`Leave ${g.name}?`)) void leaveGroup(id) }}>Leave pod</button>
        : !g.closed && <Btn className="btn" style={{ marginBottom: 10 }} onClick={() => joinGroup(id)}>Join pod</Btn>)}
      <p className="about-mail">✉️ {isInbox(g) ? <>Anyone can e-mail <b>{mailLocal(g)}@{BELFRY.domain}</b>: mails land here as cards, and the channel's admins answer as the channel.</> : <>Members can e-mail <b>{mailLocal(g)}@{BELFRY.domain}</b>: their mail shows as their message.</>}</p>
      {webPage(g)?.on && <p className="hint web-page-link">{webPage(g)!.visibility === 'members' ? 'Members only' : 'Public'} · web page: <a href={`https://${BELFRY.domain}/${webPage(g)!.slug}`} target="_blank" rel="noreferrer">{BELFRY.domain}/{webPage(g)!.slug}</a></p>}
      <p className="hint">{g.isPod ? 'Pod' : 'Channel'} id <code>{g.id}</code></p>
      {inviting && <InviteSheet id={id} onClose={() => setInviting(false)} />}
      {settings && <ChannelSettingsSheet id={id} onClose={() => setSettings(false)} />}
    </div>
  )
}

/** A channel's settings (its admins): name, description, visibility, mail, its web page. */
export function ChannelSettingsSheet({ id, onClose }: { id: string; onClose: () => void }) {
  const g = groups().find((x) => x.id === id)
  const [name, setName] = useState(g?.name ?? '')
  const [about, setAbout] = useState(g?.about ?? '')
  const [access, setAccess] = useState({ closed: !!g?.closed, prompt: g?.prompt ?? '' })
  const [openMail, setOpenMail] = useState(g ? isInbox(g) : false)
  const [web, setWeb] = useState<WebPage>((g && webPage(g)) ?? { on: false, slug: slugify(g?.name ?? ''), visibility: 'public' })
  const [webError, setWebError] = useState('')
  if (!g) return null
  return (
    <Sheet title={`${g.isPod ? 'Pod' : 'Channel'} settings`} onClose={onClose}>
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
            onClose()
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
        <input className="input" placeholder="Add a todo…" value={title} onChange={(e) => setTitle(e.target.value)} />
        <button className="btn small" disabled={!title.trim()}>Add</button>
      </Form>
      {tasks.length === 0 && <div className="empty">No todos yet.</div>}
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
      <Guard fallback={null}><Suspense fallback={<div className="members-map" />}>
        {mapped.length > 0
          ? <MembersMap pubkeys={members} onOpen={openProfile} />
          : <p className="hint pad-x">Nobody here shares their city yet. {isMember ? <button className="link" onClick={() => openProfile(me())}>Add yours</button> : null}</p>}
      </Suspense></Guard>
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

// ---- log: everything that happened in the channel, newest first (Xavier, 2026-11-12) ----------
// One line per Nostr event in the group: who did what, an icon for the kind of thing, the time;
// a day per section, with the days as chips to jump. A tap shows more and a way to go there;
// admins in developer mode (Me) see the raw event. Heartbeats and "seen" markers are left out.

interface LogItem { ev: Ev; icon: string; text: string; detail?: string; go?: string }

const LABEL_WORDS: Record<string, string> = { pinned: 'pinned a message', unpinned: 'unpinned a message', attended: 'checked someone in', absent: 'marked someone not there', saved: 'saved their account', member: 'became a member', cancelled: 'cancelled the event', scheduled: 'reopened the event', assigned: 'assigned a mail', done: 'closed a mail', open: 'reopened a mail' }
const TASK_WORDS: Record<string, string> = { claim: 'took a todo', unclaim: 'let go of a todo', done: 'marked a todo done', undone: 'reopened a todo', idle: '', working: '', delete: 'deleted a todo' }
const short = (t: string, n = 80) => { const x = t.replace(/\s+/g, ' ').trim(); return x.length > n ? x.slice(0, n - 1) + '…' : x }

function logItem(e: Ev, groupId: string, titles: Map<string, string>): LogItem | null {
  const t = (k: string) => e.tags.find((x) => x[0] === k)?.[1]
  const at = (p: string) => `c/${encodeURIComponent(groupId)}/${p}`
  switch (e.kind) {
    case 9: {
      if (t('pod')) return { ev: e, icon: '⬡', text: 'created a pod', detail: short(e.content, 200), go: at('m/' + e.id) }
      if (t('proposal')) return { ev: e, icon: '📄', text: 'made a proposal', detail: short(e.content, 200), go: at('m/' + e.id) }
      const media = e.tags.filter((x) => x[0] === 'imeta').map((x) => x.find((v) => v.startsWith('m '))?.slice(2) ?? '')
      if (media.some((m) => m.startsWith('audio/'))) return { ev: e, icon: '🎙', text: 'posted a recording', detail: short(e.content, 200), go: at('m/' + e.id) }
      if (media.some((m) => m.startsWith('image/'))) return { ev: e, icon: '🖼', text: media.length > 1 ? `posted ${media.length} photos` : 'posted a photo', detail: short(e.content, 200), go: at('m/' + e.id) }
      if (media.length) return { ev: e, icon: '📎', text: 'posted a file', detail: short(e.content, 200), go: at('m/' + e.id) }
      if (t('mail-from')) return { ev: e, icon: '✉️', text: 'an e-mail came in', detail: short(t('subject') || e.content, 200), go: at('m/' + e.id) }
      return { ev: e, icon: '💬', text: 'posted a message', detail: short(e.content, 200), go: at('m/' + e.id) }
    }
    case 1111: return { ev: e, icon: '↩️', text: 'replied in a thread', detail: short(e.content, 200), go: at('thread/' + (t('E') ?? e.id)) }
    case 1010: return { ev: e, icon: '✏️', text: 'edited a message', detail: short(e.content, 200), go: t('e') ? at('m/' + t('e')) : undefined }
    case 7: return { ev: e, icon: e.content && e.content.length <= 4 && e.content !== '+' ? e.content : '👍', text: 'reacted', go: t('e') ? at('m/' + t('e')) : undefined }
    case 5: case 9005: return { ev: e, icon: '🗑', text: t('k') === '7' ? 'took back a reaction' : 'deleted something' }
    case 2100: return { ev: e, icon: '☑️', text: 'made a todo', detail: short(t('title') || e.content, 200), go: at('todos') }
    case 2101: { const w = TASK_WORDS[t('action') ?? ''] ; if (!w) return null; const title = titles.get(t('e') ?? ''); return { ev: e, icon: t('action') === 'done' ? '✅' : '☑️', text: w, detail: title, go: at('todos') } }
    case 2103: return { ev: e, icon: '💬', text: 'commented on a todo', detail: short(e.content, 200), go: at('todos') }
    case 9000: return { ev: e, icon: '👋', text: e.tags.some((x) => x[0] === 'p' && x[2] === 'admin') ? 'made someone an admin' : 'added someone', detail: t('p') ? displayName(t('p')!) : undefined, go: at('members') }
    case 9001: return { ev: e, icon: '🚪', text: 'removed someone', detail: t('p') ? displayName(t('p')!) : undefined, go: at('members') }
    case 9021: return { ev: e, icon: '🙋', text: 'asked to join', detail: short(e.content, 200) }
    case 9022: return { ev: e, icon: '🚪', text: 'left' }
    case 9002: return { ev: e, icon: '⚙️', text: 'changed the channel', detail: [t('name'), t('about')].filter(Boolean).map((x) => short(x!, 120)).join(' · ') }
    case 9007: return { ev: e, icon: '✨', text: 'created the channel' }
    case 1985: {
      const l = e.tags.find((x) => x[0] === 'l')?.[1] ?? ''
      if (t('L') === '#t') return { ev: e, icon: '#', text: 'changed the pinned topics' }
      const who = t('p') ? displayName(t('p')!) : undefined
      return { ev: e, icon: l === 'pinned' || l === 'unpinned' ? '📌' : l === 'attended' ? '✅' : '🏷', text: LABEL_WORDS[l] ?? `labelled something ${l}`, detail: who, go: t('e') ? at('m/' + t('e')) : undefined }
    }
    case 30078: {
      const d = t('d') ?? ''
      if (d.startsWith('seen:')) return null
      return { ev: e, icon: '⚙️', text: 'changed a setting', detail: d }
    }
    case 31922: case 31923: return { ev: e, icon: e.tags.some((x) => x[0] === 't' && x[1] === 'meeting') ? '👥' : '📅', text: e.tags.some((x) => x[0] === 't' && x[1] === 'meeting') ? 'set the meeting' : 'set the event', detail: t('title') }
    case 31925: return { ev: e, icon: '📅', text: `answered ${t('status') === 'accepted' ? 'going' : t('status') === 'declined' ? "can't go" : 'maybe'}` }
    default: return { ev: e, icon: '•', text: `kind ${e.kind}` }
  }
}

export function Log({ groupId }: { groupId: string }) {
  useVersion()
  const [open, setOpen] = useState<string | null>(null)
  const [raw, setRaw] = useState<Ev | null>(null)
  const g = groups().find((x) => x.id === groupId)
  const dev = devMode() && ((g && canAdmin(g)) || isAdmin())
  const evs = all(me()).filter((e) => e.tags.some((x) => x[0] === 'h' && x[1] === groupId))
  const titles = new Map(tasklist(groupId).tasks.map((t) => [t.id, t.title]))
  // the relay signs joins and channel changes itself: say so instead of showing its key
  const relayKey = all(me()).find((e) => e.kind === 39000 && e.tags.some((x) => x[0] === 'd' && x[1] === groupId))?.pubkey
  const who = (pk: string) => (pk !== me() && pk === relayKey ? NAME : displayName(pk))
  const items = evs.sort((a, b) => b.created_at - a.created_at).map((e) => logItem(e, groupId, titles)).filter((x): x is LogItem => !!x).slice(0, 1000)
  if (!items.length) return <div className="empty">Nothing happened here yet.</div>
  const dayOf = (s: number) => new Date(s * 1000).toDateString()
  const days: [string, LogItem[]][] = []
  for (const it of items) { const d = dayOf(it.ev.created_at); const last = days.at(-1); if (last && last[0] === d) last[1].push(it); else days.push([d, [it]]) }
  const label = (d: string) => (d === new Date().toDateString() ? 'Today' : d === new Date(Date.now() - 86400e3).toDateString() ? 'Yesterday' : fmtDay(new Date(d)))
  return (
    <div className="log">
      <div className="log-days" role="navigation" aria-label="Days">
        {days.map(([d, l]) => <button key={d} className="chip" onClick={() => document.getElementById('log-' + d.replace(/\s/g, '-'))?.scrollIntoView({ behavior: 'smooth', block: 'start' })}>{label(d)} <small>{l.length}</small></button>)}
      </div>
      {days.map(([d, list]) => (
        <section key={d} id={'log-' + d.replace(/\s/g, '-')} className="log-day">
          <h3 className="log-date">{label(d)}</h3>
          {list.map((it) => (
            <div key={it.ev.id} className={'log-row' + (open === it.ev.id ? ' open' : '')}>
              <button className="log-main" onClick={() => setOpen(open === it.ev.id ? null : it.ev.id)} aria-expanded={open === it.ev.id}>
                <span className="log-ico" aria-hidden="true">{it.icon}</span>
                <span className="log-text"><b>{who(it.ev.pubkey)}</b> {it.text}{it.detail && open !== it.ev.id ? <small>{short(it.detail, 60)}</small> : null}</span>
                <time className="hint">{fmtTime(it.ev.created_at)}</time>
              </button>
              {open === it.ev.id && (
                <div className="log-more">
                  {it.detail && <p>{it.detail}</p>}
                  <p className="hint">{new Date(it.ev.created_at * 1000).toLocaleString()}</p>
                  <div className="row" style={{ gap: 8 }}>
                    {it.go && <button className="btn small" onClick={() => go(it.go!)}>Go there</button>}
                    {dev && <button className="btn small ghost" onClick={() => setRaw(it.ev)}>Inspect the Nostr event</button>}
                  </div>
                </div>
              )}
            </div>
          ))}
        </section>
      ))}
      {raw && (
        <Sheet title={`Nostr event · kind ${raw.kind}`} onClose={() => setRaw(null)}>
          <pre className="raw-event">{JSON.stringify({ id: raw.id, pubkey: raw.pubkey, created_at: raw.created_at, kind: raw.kind, tags: raw.tags, content: raw.content, sig: (raw as { sig?: string }).sig }, null, 2)}</pre>
          <button className="btn small ghost" onClick={() => void navigator.clipboard?.writeText(JSON.stringify(raw))}>Copy</button>
        </Sheet>
      )}
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
