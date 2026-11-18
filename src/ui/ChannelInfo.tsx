import { roleBadge } from '../core/roles'
import { roleWord } from '../core/checkin'
import { lazy, Suspense, useEffect, useRef, useState } from 'react'
import { FORWARD_ERRORS, ForwardFailed, loadForward, onDemoForward, resendForward, setForward, type Forward } from '../core/mailforward'
import { extras, placeLabel } from '../core/people'
import { Linkify, Markdown } from './markdown'
import { openProfile } from './Profile'

const MembersMap = lazy(() => import('./MembersMap'))
import { setArchived, mayPickAddress, setInbox, setVisibility, setWebPage } from '../core/actions'
import { isInbox, isPublicChannel, mailLocal, publicPeriods, slugify, webPage, WEB_RESERVED, type ChannelVisibility, type Group, type WebPage } from '../core/model'
import { BELFRY, NAME } from '../config'
import { addTask, admit, assignTask, commentTask, decline, DEFAULT_PROMPT, editChannel, editPod, joinGroup, leaveGroup, setMemberLabels, taskAction } from '../core/actions'
import { ACTIVE_DAYS, allGroups, archivable, channelIcon, displayName, group, groups, guestJoins, memberLabels, messages, recentlyActive, tasklist, isWorking, workingSince, type Task } from '../core/model'
import { isGuest } from '../core/invite'
import { useVersion } from '../core/store'
import { DropBox } from './Drop'
import { InviteSheet } from './Invite'
import { canAdmin } from '../core/access'
import { me } from '../core/sync'
import { Avatar, back, devMode, fmtDay, fmtTime, go, Guard, MediaGrid, Sheet, Btn, Form, useNow, WorkRing } from './common'
import { all, type Ev } from '../core/store'
import { isAdmin } from '../core/access'
import { GroupRequest } from './Requests'
import { groupRequestsFor } from '../core/requests'
import { ArchiveIcon, GearIcon, ThreadIcon } from './icons'
import { ArchivedBar } from './GroupPage'

type Tab = 'about' | 'tasks' | 'members' | 'files' | 'log'

export function ChannelInfo({ id, initial = 'about' }: { id: string; initial?: Tab }) {
  useVersion()
  const [tab, setTab] = useState<Tab>(initial)
  const [settings, setSettings] = useState(false)
  const g = group(id)
  return (
    <div className="screen">
      <div className="topbar">
        <button className="icon-btn" aria-label="Back" onClick={() => back('c/' + id)}>‹</button>
        <h1>{channelIcon(g)} {g?.name ?? id}</h1>
        {g && canAdmin(g) && !g.archived && <button className="icon-btn" aria-label="Settings" onClick={() => setSettings(true)}><GearIcon /></button>}
      </div>
      {g?.archived && <ArchivedBar g={g} />}
      {/* archived: what there is to read (no to-dos to tick) */}
      <ChannelInfoBody id={id} tab={g?.archived && tab === 'tasks' ? 'about' : tab} setTab={setTab} tabs={g?.archived ? [['about', 'About'], ['members', 'Members'], ['files', 'Files'], ['log', 'Log']] : undefined} />
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
  const g = group(id)
  const [inviting, setInviting] = useState(false)
  const [settings, setSettings] = useState(false)
  if (!g) return null
  const active = recentlyActive(id)
  const parent = g.parent ? group(g.parent) : undefined
  // archived: its description and who was in it; nothing to invite to, join, leave or write to
  if (g.archived) return (
    <div className="pad about">
      {g.about ? <Markdown text={g.about} className="chead-about" /> : <p className="hint">No description yet.</p>}
      <div className="about-line">
        <button className="about-faces" onClick={() => go(`c/${encodeURIComponent(id)}/members`)} aria-label="See the members">
          {[...g.members].slice(0, 6).map((p) => <Avatar key={p} pubkey={p} size={24} />)}
          <span><b>{g.members.size}</b> {g.members.size === 1 ? 'member' : 'members'}</span>
        </button>
      </div>
      {g.isPod && parent && <p className="about-line">Group in <button className="link" onClick={() => go('c/' + encodeURIComponent(parent.id))}>{channelIcon(parent)} {parent.name}</button></p>}
      <p className="hint">{g.isPod ? 'Group' : 'Channel'} id <code>{g.id}</code></p>
    </div>
  )
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
        <p className="about-line">Group in <button className="link" onClick={() => go('c/' + encodeURIComponent(parent.id))}>{channelIcon(parent)} {parent.name}</button> · {g.closed ? (g.vouches > 0 ? `by request: ${g.vouches === 1 ? 'one vouch' : `${g.vouches} vouches`} or an admin lets people in` : 'by request: its admins let people in') : 'open: any member joins'} · <button className="link" onClick={() => go(`c/${encodeURIComponent(id)}/group`)}>Group page</button></p>
      )}
      {g.isPod && g.closed && !compact && <p className="hint" style={{ whiteSpace: 'pre-wrap' }}>Question: {g.prompt || '—'}</p>}
      {g.isPod && (g.members.has(me())
        ? <button className="btn ghost" style={{ marginBottom: 10 }} onClick={() => { if (confirm(`Leave ${g.name}?`)) void leaveGroup(id) }}>Leave group</button>
        : !g.closed && <Btn className="btn" style={{ marginBottom: 10 }} onClick={() => joinGroup(id)}>Join group</Btn>)}
      <p className="about-mail">✉️ {isInbox(g) ? <>Anyone can e-mail <b>{mailLocal(g)}@{BELFRY.domain}</b>: mails land here as cards, and the channel's admins answer as the channel.</> : <>Members can e-mail <b>{mailLocal(g)}@{BELFRY.domain}</b>: their mail shows as their message.</>}</p>
      {webPage(g)?.on && <p className="hint web-page-link">{webPage(g)!.visibility === 'members' ? 'Members only' : 'Public'} · web page: <a href={`https://${BELFRY.domain}/${webPage(g)!.slug}`} target="_blank" rel="noreferrer">{BELFRY.domain}/{webPage(g)!.slug}</a></p>}
      <p className="hint">{g.isPod ? 'Group' : 'Channel'} id <code>{g.id}</code></p>
      {inviting && <InviteSheet id={id} onClose={() => setInviting(false)} />}
      {settings && <ChannelSettingsSheet id={id} onClose={() => setSettings(false)} />}
    </div>
  )
}

/** Moving channel G to public or members only (its admins, #issues 2026-11-13): what it changes,
 *  worked out from the channel as it is, then a confirmation. Never retroactive (setVisibility). */
export function VisibilitySheet({ g, to, onClose, onDone }: { g: Group; to: ChannelVisibility; onClose: () => void; onDone: (page: WebPage) => void }) {
  const n = messages(g.id).length
  const w = webPage(g)
  const slug = w?.slug || slugify(g.rawName)
  const wasPublic = publicPeriods(g).length > 0
  const where = <b>{BELFRY.domain}/{slug}</b>
  const mail = <b>{mailLocal(g)}@{BELFRY.domain}</b>
  return (
    <Sheet title={to === 'public' ? `Make #${g.name} public?` : `Make #${g.name} members only?`} onClose={onClose}>
      <div className="pad vis-sheet">
        {to === 'public' ? <>
          <p>From now on, anyone on the web reads what is posted here, at {where}{isInbox(g) ? <>, and anyone can write to {mail}</> : null}.</p>
          <p>{n ? <>Its <b>{n} message{n === 1 ? '' : 's'}</b> already here stay members only: only messages posted from now on are public.</> : 'It has no messages yet.'}</p>
        </> : <>
          <p>From now on, what is posted here is for members only. People from outside see only their own thread.</p>
          <p>{wasPublic && n ? <>What was posted while it was public stays public at {where}: only new messages are members only.</> : n ? 'Its messages were never public.' : 'It has no messages yet.'}</p>
        </>}
        <div className="row" style={{ gap: 8, marginTop: 12 }}>
          <button type="button" className="btn ghost" onClick={onClose}>Cancel</button>
          <Btn className="btn" onClick={async () => onDone(await setVisibility(g.id, to))}>{to === 'public' ? 'Make it public' : 'Make it members only'}</Btn>
        </div>
      </div>
    </Sheet>
  )
}

/** A channel's settings (its admins) in a sheet. */
export function ChannelSettingsSheet({ id, onClose }: { id: string; onClose: () => void }) {
  const g = group(id)
  if (!g || g.archived) return null
  return <Sheet title={`${g.isPod ? 'Group' : 'Channel'} settings`} onClose={onClose}><ChannelSettingsForm id={id} onDone={onClose} /></Sheet>
}

/** A channel's settings (its admins): name, description, visibility, posts, mail, its web page,
 *  archive. In a sheet, or as the channel's Settings screen (ChannelMenu.tsx). */
export function ChannelSettingsForm({ id, onDone }: { id: string; onDone: () => void }) {
  const onClose = onDone
  const g = group(id)
  const [name, setName] = useState(g?.name ?? '')
  const [about, setAbout] = useState(g?.about ?? '')
  const [access, setAccess] = useState<{ closed: boolean; prompt: string; vouches?: number }>({ closed: !!g?.closed, prompt: g?.prompt ?? '', vouches: g?.vouches ?? 0 })
  const [openMail, setOpenMail] = useState(g ? isInbox(g) : false)
  const [web, setWeb] = useState<WebPage>((g && webPage(g)) ?? { on: false, slug: mayPickAddress() ? slugify(g?.name ?? '') : (g?.id ?? ''), visibility: 'members' })
  const [webError, setWebError] = useState('')
  // who can read it, one choice of three (Xavier, 2026-11-18, canvas "Settings-A/B"): members only,
  // public (the belfry:web period, never retroactive) or closed (NIP-29 closed: people ask, a
  // steward lets them in). Public and closed exclude each other. Saved with the rest.
  const vis0: Vis = g ? visOf(g) : 'members'
  const [vis, setVis] = useState<Vis>(vis0)
  const [askPublic, setAskPublic] = useState(false)
  const [archiving, setArchiving] = useState(false)
  const [archiveError, setArchiveError] = useState('')
  if (!g || g.archived) return null
  // going public: asked first, here in the app (Settings-B)
  if (askPublic) return (
      <div className="confirm-box vis-confirm" role="alertdialog" aria-label={`Make ${chanLabel(g)} public?`}>
        <b>Make {chanLabel(g)} public?</b>
        <p>From now on, anyone on the web can read what is posted here, with the names of the people who post. Earlier messages stay for members only.</p>
        <div className="row-btns two">
          <button type="button" className="btn ghost" onClick={() => setAskPublic(false)}>Keep it for members</button>
          <button type="button" className="btn" onClick={() => { setVis('public'); setAskPublic(false) }}>Make it public</button>
        </div>
      </div>
  )
  // archiving: asked here, in the app (never the browser's confirm)
  if (archiving) return (
      <div className="confirm-box" role="alertdialog" aria-label={`Archive ${g.name}`}>
        <b>Archive {g.name}?</b>
        <p>It leaves the channel list and nobody can post in it. You can unarchive it later.</p>
        {archiveError && <div className="error" role="alert">{archiveError}</div>}
        <div className="row-btns two">
          <button type="button" className="btn ghost" onClick={() => { setArchiving(false); setArchiveError('') }}>Keep it</button>
          <Btn className="btn danger" onClick={async () => { setArchiveError(''); try { await setArchived(id, true); onClose() } catch (e) { setArchiveError(e instanceof Error ? e.message : 'That did not work. Try again.') } }}>Archive</Btn>
        </div>
      </div>
  )
  return (
          <Form className="chan-settings-form" onSubmit={async (e) => {
            e.preventDefault()
            const closed = vis === 'closed'
            if (g.isPod) await editPod(id, name.trim(), about.trim(), { ...access, closed })
            else if (closed !== !!g.closed) await editPod(id, name.trim(), about.trim(), { closed, prompt: g.prompt ?? '', vouches: 0 })
            else if (name.trim() !== g.name || about.trim() !== g.about) await editChannel(id, name.trim(), about.trim())
            // public from now on, or not any more (a period opens or closes; never retroactive)
            if ((vis === 'public') !== isPublicChannel(g)) {
              const page = await setVisibility(id, vis === 'public' ? 'public' : 'members')
              web.visibility = page.visibility; web.periods = page.periods
              if (page.visibility === 'public') { web.on = true; if (!web.slug) web.slug = page.slug }
            }
            if (web.on) {
              const slug = slugify(web.slug)
              if (slug.length < 2) { setWebError('Give the page an address: two letters or more.'); return }
              if (WEB_RESERVED.has(slug)) { setWebError(`${BELFRY.domain}/${slug} is taken by ${BELFRY.domain} itself: pick another address.`); return }
              const taken = groups().find((x) => x.id !== id && webPage(x)?.on && webPage(x)?.slug === slug)
              if (taken) { setWebError(`${BELFRY.domain}/${slug} is the page of #${taken.name}: pick another address.`); return }
              web.slug = slug
            }
            if (openMail !== isInbox(g)) await setInbox(id, openMail)
            const was = webPage(group(id))
            if (was) { web.visibility = was.visibility; web.periods = was.periods; if (was.visibility === 'public') web.on = web.on || was.on }
            if (web.on !== !!was?.on || web.slug !== (was?.slug ?? web.slug) || web.visibility !== (was?.visibility ?? 'public') || (web.posts ?? '') !== (was?.posts ?? '')) await setWebPage(id, web)
            onClose()
          }}>
            <label className="field"><span>Name</span><input className="input" value={name} onChange={(e) => setName(e.target.value)} /></label>
            <label className="field"><span>Description · Markdown works, links become clickable</span><textarea className="input" rows={4} value={about} onChange={(e) => setAbout(e.target.value)} /></label>
            <div className="field who-reads"><span className="sec-title">Who can read it</span>
              <div className="act-group vis-rows" role="radiogroup" aria-label="Who can read it">
                {VIS.filter(([v]) => v !== 'closed' || g.id !== 'general').map(([v, Icon, title, line]) => (
                  <button key={v} type="button" role="radio" aria-checked={vis === v} className={'act vis-row' + (vis === v ? ' on' : '')}
                    onClick={() => { if (v === vis) return; if (v === 'public') setAskPublic(true); else setVis(v) }}>
                    <span className="cmenu-ico"><Icon /></span>
                    <span className="t"><b>{title}</b><small>{line}</small></span>
                    {vis === v && <CheckIcon />}
                  </button>
                ))}
              </div>
              <small className="hint vis-foot">A change counts from now on. Messages keep the visibility they were posted with.</small>
            </div>
            {g.isPod && vis === 'closed' && <ClosedFields prompt={access.prompt} vouches={access.vouches} set={(a) => setAccess({ ...access, ...a })} />}
            {!g.isPod && <div className="field visibility"><span>Posts</span>
              {([['', 'Anything', 'messages, threads, events, proposals'], ['thread', 'Threads only', 'every post has a title; talk goes in its thread'], ['event', 'Events only', 'the composer becomes "New event"'], ['proposal', 'Proposals only', 'to discuss and vote on'],
                ...(web.posts === 'request' ? [['request', 'Requests', 'from the web; members turn them into proposals']] : [])] as [string, string, string][]).map(([v, label, hint]) => (
                <label key={v || 'any'} className="toggle"><input type="radio" name="posts" checked={(web.posts ?? '') === v} onChange={() => setWeb({ ...web, posts: (v || undefined) as WebPage['posts'] })} /> <span><b>{label}</b> · {hint}</span></label>
              ))}
            </div>}
            <label className="switch-row">
              <span><b>E-mails from anyone</b><small>Mail to {mailLocal(g)}@{BELFRY.domain} from outside lands here as cards; admins answer as the channel. Off: only members' mail comes in.</small></span>
              <input type="checkbox" role="switch" checked={openMail} onChange={(e) => setOpenMail(e.target.checked)} />
            </label>
            <MailForwardRow id={g.id} />
            <label className="switch-row">
              <span><b>Threads from the web</b><small>A public page at {BELFRY.domain}/{slugify(web.slug) || '…'} where anyone opens a thread: it lands here as a card, and members' replies are mailed back to its author.</small></span>
              <input type="checkbox" role="switch" checked={web.on} onChange={(e) => setWeb({ ...web, on: e.target.checked })} />
            </label>
            {web.on && (
              <div className="web-page-fields">
                <label className="field"><span>Address</span>
                  <span className="row" style={{ gap: 6, alignItems: 'center' }}><span className="hint" style={{ whiteSpace: 'nowrap' }}>{BELFRY.domain}/</span>
                    <input className="input" aria-label="Page address" value={web.slug} readOnly={!mayPickAddress()} onChange={(e) => { setWebError(''); setWeb({ ...web, slug: e.target.value.toLowerCase() }) }} /></span>
                </label>
                {!mayPickAddress() && <p className="hint">The channel's own address: the belfry's admins choose plain ones.</p>}
                {webError && <p className="error">{webError}</p>}
              </div>
            )}
            <button className="btn primary save-btn" disabled={!name.trim()}>Save</button>
            {/* a softer way to delete: out of the list and read only, until someone unarchives it */}
            {archivable(id) && (
              <div className="archive-under">
                <button type="button" className="link danger archive-link" onClick={() => setArchiving(true)}><ArchiveIcon /> Archive this {g.isPod ? 'group' : 'channel'}</button>
                <small>It leaves the channel list and becomes read only. You can unarchive it later.</small>
              </div>
            )}
            <p className="hint">{g.isPod ? 'Group' : 'Channel'} id <code>{g.id}</code></p>
          </Form>
  )
}

type Vis = 'members' | 'public' | 'closed'
const visOf = (g: Group): Vis => (isPublicChannel(g) ? 'public' : g.closed ? 'closed' : 'members')
const chanLabel = (g: Group) => (g.isPod || g.isEvent || g.isProposal ? g.name : '#' + g.name)
const VisLock = () => <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><rect x="5" y="11" width="14" height="9" rx="2" /><path d="M8 11V8a4 4 0 0 1 8 0v3" /></svg>
const VisGlobe = () => <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9" /><path d="M3 12h18M12 3c3 3 3 15 0 18M12 3c-3 3-3 15 0 18" /></svg>
const VisAsk = () => <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><circle cx="9" cy="8" r="4" /><path d="M2 21v-1a6 6 0 0 1 9.5-4.9M15 18l2 2 4-4" /></svg>
const CheckIcon = () => <svg className="vis-check" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" role="img" aria-label="Selected"><path d="M5 12l5 5 9-10" /></svg>
const VIS: [Vis, () => JSX.Element, string, string][] = [
  ['members', VisLock, 'Members only', 'Everyone with a pass in this belfry'],
  ['public', VisGlobe, 'Public', 'Anyone on the web can read it. Only members write.'],
  ['closed', VisAsk, 'Closed', 'People ask to join. A steward of this channel approves.'],
]

/** A closed group's question and how requests are accepted (by its stewards, or by vouches). */
function ClosedFields({ prompt, vouches = 0, set }: { prompt: string; vouches?: number; set: (a: { prompt: string; vouches: number }) => void }) {
  return (
    <>
      <label className="field"><span>Question people answer when they ask</span>
        <textarea className="input" rows={2} placeholder={DEFAULT_PROMPT} value={prompt} onChange={(e) => set({ prompt: e.target.value, vouches })} /></label>
      <div className="field vouch-rule"><span>Requests are accepted</span>
        <div className="pick-group" role="radiogroup" aria-label="How requests are accepted">
          <button type="button" role="radio" aria-checked={!vouches} className={'pick-row' + (!vouches ? ' on' : '')} onClick={() => set({ prompt, vouches: 0 })}>
            <span className="radio" aria-hidden="true" /><span className="t"><b>By its stewards</b><small>A steward accepts or declines each request</small></span>
          </button>
          <button type="button" role="radio" aria-checked={!!vouches} className={'pick-row' + (vouches ? ' on' : '')} onClick={() => set({ prompt, vouches: vouches || 2 })}>
            <span className="radio" aria-hidden="true" /><span className="t"><b>When members vouch</b><small>Once enough of its members vouch, they’re in. Stewards can still answer.</small></span>
          </button>
        </div>
        {!!vouches && (
          <div className="seg small vouch-n" role="group" aria-label="How many vouches">
            {[1, 2, 3, 4, 5].map((n) => <button key={n} type="button" className={vouches === n ? 'on' : ''} aria-pressed={vouches === n} onClick={() => set({ prompt, vouches: n })}>{n}</button>)}
          </div>
        )}
        {!!vouches && <small className="hint">{vouches === 1 ? 'One vouch' : `${vouches} vouches`} from different members of the group (not guests, not agents).</small>}
      </div>
    </>
  )
}

/** "Forward all e-mails to" an outside address (Belfry mailforward.py): read from the belfry
 *  when the settings open, saved on its own (it waits for the outside address to confirm). */
function MailForwardRow({ id }: { id: string }) {
  const [loaded, setLoaded] = useState(false)
  const [fwd, setFwd] = useState<Forward | null>(null)
  const [on, setOn] = useState(false)
  const [address, setAddress] = useState('')
  const [error, setError] = useState('')
  const [note, setNote] = useState('')
  const save = useRef<HTMLSpanElement>(null)
  const show = (f: Forward | null) => { setFwd(f); setOn(!!f); setAddress(f?.address ?? '') }
  const fail = (e: unknown) => setError(e instanceof ForwardFailed ? e.message : FORWARD_ERRORS.failed)
  useEffect(() => {
    let live = true
    const load = () => loadForward(id).then((f) => { if (live) { show(f); setLoaded(true) } }, (e) => { if (live) { fail(e); setLoaded(true) } })
    void load()
    const off = onDemoForward(() => void load())
    return () => { live = false; off() }
  }, [id])
  const toggle = async (want: boolean) => {
    setError(''); setNote('')
    if (want || !fwd) { setOn(want); return }
    try { show(await setForward(id, null)); setNote('Forwarding is off.') } catch (e) { fail(e) }
  }
  const submit = async () => {
    setError(''); setNote('')
    try {
      const f = await setForward(id, address.trim())
      show(f)
      if (f?.state === 'pending') setNote('We sent a confirmation e-mail. Forwarding starts once it is confirmed.')
    } catch (e) { fail(e) }
  }
  const resend = async () => {
    setError(''); setNote('')
    try { show(await resendForward(id)); setNote('Sent again.') } catch (e) { fail(e) }
  }
  const dom = (fwd?.address ?? '').split('@')[1] ?? ''
  const same = !!fwd && address.trim().toLowerCase() === fwd.address.toLowerCase()
  return (
    <div className="mail-forward">
      <label className="switch-row">
        <span><b>Forward all e-mails to</b><small>E-mails still show up here. A copy goes to this address.</small></span>
        <input type="checkbox" role="switch" aria-label="Forward all e-mails" checked={on} disabled={!loaded} onChange={(e) => void toggle(e.target.checked)} />
      </label>
      {on && (
        <div className="mail-forward-fields">
          {fwd?.state === 'on' && <p className="fwd-state" role="status">Forwarding to <b>{fwd.address}</b></p>}
          {fwd?.state === 'pending' && <p className="fwd-state" role="status">Waiting for <b>{fwd.address}</b> to confirm · <Btn className="link" onClick={resend}>Send again</Btn></p>}
          <div className="row" style={{ gap: 8, alignItems: 'center' }}>
            <input className="input" type="email" inputMode="email" autoComplete="off" aria-label="Forward to address" placeholder="name@example.org" value={address}
              onChange={(e) => { setError(''); setNote(''); setAddress(e.target.value) }}
              onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); save.current?.querySelector('button')?.click() } }} />
            <span ref={save}><Btn className="btn small" disabled={!address.trim() || same} onClick={submit}>Save</Btn></span>
          </div>
          {fwd && fwd.sameDomainSkipped && dom && <p className="hint">E-mails sent from @{dom} addresses are not forwarded.</p>}
          {note && <p className="hint" role="status">{note}</p>}
          {error && <p className="error" role="alert">{error}</p>}
        </div>
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
  const [assigning, setAssigning] = useState(false)
  const mine = t.assignee === me()
  const now = useNow()
  const busy = isWorking(t, now)
  return (
    <div className={'task' + (t.done ? ' done' : '')} style={{ opacity: t.pending ? 0.6 : 1 }}>
      <button className={'check' + (t.done ? ' done' : t.assignee ? ' claimed' : '') + (busy ? ' working' : '')} aria-label={t.done ? 'Mark as not done' : 'Mark as done'}
        onClick={() => taskAction(groupId, t.id, t.done ? 'undone' : 'done')}>{t.done ? '✓' : busy ? <WorkRing since={workingSince(t) ?? now} now={now} /> : ''}</button>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div className="title" onClick={toggle}><Linkify text={t.title} /></div>
        <small onClick={toggle}>
          {t.assignee ? `${displayName(t.assignee)} ${t.assignee === me() ? 'are' : 'is'} ${busy ? 'working on it now' : 'on it'}` : 'Unassigned'}
          {t.comments.length ? ` · 💬 ${t.comments.length}` : ''}
        </small>
        {open && (
          <div style={{ marginTop: 8 }}>
            {t.comments.map((c, i) => <div key={i} style={{ fontSize: 14, margin: '4px 0' }}><b>{displayName(c.author)}</b> <Linkify text={c.text} /></div>)}
            <div className="row" style={{ margin: '8px 0', flexWrap: 'wrap' }}>
              <button className="btn small ghost" onClick={() => taskAction(groupId, t.id, mine ? 'unclaim' : 'claim')}>
                {mine ? 'Unassign me' : t.assignee ? 'Take over' : "I'll take it"}
              </button>
              {/* someone else: give it to them, or take them off it (Xavier, 2026-11-18) */}
              <button className="btn small ghost" aria-expanded={assigning} onClick={() => setAssigning(!assigning)}>Assign to…</button>
              {t.assignee && !mine && <Btn className="btn small ghost" onClick={() => assignTask(groupId, t.id, t.assignee!, false)}>Unassign {displayName(t.assignee)}</Btn>}
              {t.author === me() && <button className="btn small ghost" onClick={() => taskAction(groupId, t.id, 'delete')}>Delete</button>}
            </div>
            {assigning && (
              <div className="assign-list" role="listbox" aria-label="Assign to">
                {[me(), ...[...(group(groupId)?.members ?? [])].filter((k) => k !== me() && !isGuest(k))].slice(0, 50).map((k) => (
                  <Btn key={k} className={'pick-row' + (t.assignee === k ? ' on' : '')} role="option" aria-selected={t.assignee === k}
                    onClick={async () => { await (k === me() ? taskAction(groupId, t.id, 'claim') : assignTask(groupId, t.id, k, true)); setAssigning(false) }}>
                    <Avatar pubkey={k} size={24} /><span>{k === me() ? 'Me' : displayName(k)}</span>
                  </Btn>
                ))}
              </div>
            )}
            <Form className="row" onSubmit={async (e) => { e.preventDefault(); if (comment.trim()) { await commentTask(groupId, t.id, comment.trim()); setComment('') } }}>
              <input className="input" placeholder="Comment…" value={comment} onChange={(e) => setComment(e.target.value)} />
              <button className="btn small" disabled={!comment.trim()}>Send</button>
            </Form>
          </div>
        )}
      </div>
      {/* made from a message: its thread, where its history is written (the old pull-down row's link) */}
      {t.source && <button type="button" className="icon-btn task-thread" aria-label="Open its thread" title="Open its thread"
        onClick={() => go('c/' + encodeURIComponent(groupId) + '/thread/' + t.source)}><ThreadIcon /></button>}
    </div>
  )
}

// ---- members and their labels (NIP-32) ---------------------------------------------

export function Members({ groupId }: { groupId: string }) {
  useVersion()
  const g = group(groupId)
  const members = [...new Set([...(g?.members ?? []), ...(g?.isPod ? [] : [me()])])].filter(Boolean)
  // requests to join: its admins answer; while it accepts by vouches, its members vouch
  const reviewing = g && (g.isPod || g.closed) && !g.archived ? (canAdmin(g) ? g.applications : groupRequestsFor(g)) : []
  // guest mode: people who opened an invite link and have not joined are a number, not members
  const readingN = g ? [...g.reading].filter((p) => p !== me()).length : 0
  // guests (people or agents) and who brought them in: whose agent it is
  const invited = new Map(guestJoins(groupId).filter((j) => !j.member).map((j) => [j.pubkey, j]))
  const ex = extras()
  const mapped = members.filter((p) => ex.get(p)?.place?.lat !== undefined)
  const isMember = !allGroups().flatMap((x) => guestJoins(x.id)).some((j) => j.pubkey === me() && !j.member)
  return (
    <div>
      {reviewing.length > 0 && <div className="section-title">Asking to join <span>{reviewing.length}</span></div>}
      {reviewing.map((a) => <GroupRequest key={a.ev.id} g={g!} a={a} />)}
      {reviewing.length > 0 && <div className="section-title">Members <span>{g?.members.size}</span></div>}
      <Guard fallback={null}><Suspense fallback={<div className="members-map" />}>
        {mapped.length > 0
          ? <MembersMap pubkeys={members} onOpen={openProfile} />
          : <p className="hint pad-x">Nobody here shares their city yet. {isMember ? <button className="link" onClick={() => openProfile(me())}>Add yours</button> : null}</p>}
      </Suspense></Guard>
      {/* a row: face, name, the role here when it isn't the default (no labels: docs/checkin.md §5);
          a tap opens the profile, which shows the role here and lets this channel's admins change it */}
      {members.map((p) => {
        const role = roleWord(groupId, p) || roleBadge(p)
        const inv = invited.get(p)
        const sub = role || (inv ? `${inv.agent ? 'Agent' : inv.unsaved ? 'Guest · not saved' : 'Guest'}${inv.by ? ` · invited by ${displayName(inv.by)}` : ''}` : ex.get(p)?.place ? placeLabel(ex.get(p)!.place) : '')
        return (
          <button key={p} className="member member-row" onClick={() => openProfile(p, groupId)} aria-label={`${displayName(p)}: profile`}>
            <Avatar pubkey={p} />
            <span className="t"><b>{displayName(p)}</b>{sub && <small>{sub}</small>}</span>
          </button>
        )
      })}
      {readingN > 0 && <p className="hint pad-x guests-reading">{readingN === 1 ? '1 guest is reading' : `${readingN} guests are reading`}: they opened an invite link and have not joined yet.</p>}
    </div>
  )
}

// ---- log: everything that happened in the channel, newest first (Xavier, 2026-11-12) ----------
// One line per Nostr event in the group: who did what, an icon for the kind of thing, the time;
// a day per section, with the days as chips to jump. A tap shows more and a way to go there;
// admins in developer mode (Me) see the raw event. Heartbeats and "seen" markers are left out.

interface LogItem { ev: Ev; icon: string; text: string; detail?: string; go?: string }

const LABEL_WORDS: Record<string, string> = { steward: 'put someone on the door', none: 'changed someone’s role', 'checked-in': 'checked someone in', 'not-checked-in': 'undid a check-in', pinned: 'pinned a message', unpinned: 'unpinned a message', attended: 'checked someone in', absent: 'marked someone not there', saved: 'saved their account', member: 'became a member', cancelled: 'cancelled the event', scheduled: 'reopened the event', assigned: 'assigned a mail', done: 'closed a mail', open: 'reopened a mail' }
const TASK_WORDS: Record<string, string> = { assign: 'gave someone a todo', unassign: 'took someone off a todo', claim: 'took a todo', unclaim: 'let go of a todo', done: 'marked a todo done', undone: 'reopened a todo', idle: '', working: '', delete: 'deleted a todo' }
const short = (t: string, n = 80) => { const x = t.replace(/\s+/g, ' ').trim(); return x.length > n ? x.slice(0, n - 1) + '…' : x }

function logItem(e: Ev, groupId: string, titles: Map<string, string>): LogItem | null {
  const t = (k: string) => e.tags.find((x) => x[0] === k)?.[1]
  const at = (p: string) => `c/${encodeURIComponent(groupId)}/${p}`
  switch (e.kind) {
    case 9: {
      if (t('pod')) return { ev: e, icon: '⬡', text: 'created a group', detail: short(e.content, 200), go: at('m/' + e.id) }
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
  const g = group(groupId)
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

export function Files({ groupId, only }: { groupId: string; /** the Files screen: photos have their own */ only?: 'files' }) {
  useVersion()
  const media = messages(groupId).flatMap((m) => m.media).reverse().filter((m) => !only || !/^(image|video)\//.test(m.mime))
  return (
    <div className="pad">
      {!group(groupId)?.archived && <DropBox groupId={groupId} />}
      {media.length ? <MediaGrid media={media} /> : <div className="empty">{only ? 'Files posted in this channel show up here.' : 'Photos and files posted in this channel show up here.'}</div>}
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
  const g = group(id)
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
          {all.slice(0, n).map((p) => <button key={p} className="face" onClick={() => openProfile(p, id)} aria-label={displayName(p)}><Avatar pubkey={p} size={28} /></button>)}
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
