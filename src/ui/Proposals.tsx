import { BELFRY } from '../config'
import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { createProposal, markSeen, reviseProposal, setProposalStatus, vote, type NewProposal } from '../core/actions'
import { canDecide, displayName, group, messages, proposalFor, STATUS_LABEL, STATUSES, VOTE_OPTIONS, type Proposal, type ProposalStatus } from '../core/model'
import { all, useVersion } from '../core/store'
import { me } from '../core/sync'
import { Avatar, Btn, fmtDay, go, Sheet, Form, useFormDraft } from './common'
import { Markdown } from './markdown'
import { parsePaths, rootFolder, scopeWords, siteFiles, websiteTitle } from '../core/website'
import { Conversation } from './Chat'
import { ChannelMenu } from './ChannelMenu'
import { SaveFailedIcon } from './ChannelHeader'
import { openProfile } from './Profile'
import { ChevronDownIcon, ChevronUpIcon, GlobeIcon, MoreIcon, SearchIcon } from './icons'

// A proposal is a channel template: its chat is where feedback goes; above it, the text (with its
// version), its status and, while open for votes, the vote.

export const StatusBadge = ({ s }: { s: ProposalStatus }) => <span className={'badge st-' + s}>{STATUS_LABEL[s]}</span>

function tally(p: Proposal) {
  const out: Record<string, number> = {}
  for (const v of p.poll?.votes.values() ?? []) out[v] = (out[v] ?? 0) + 1
  return out
}

/** The post a proposal leaves in its parent. */
export function ProposalCard({ id, fallback }: { id: string; fallback: string }) {
  const p = proposalFor(id)
  if (!p) return <div className="text">{fallback}</div>
  const t = tally(p)
  return (
    <div className="ev-card pod-card" role="button" tabIndex={0} onClick={() => go('c/' + encodeURIComponent(id))}>
      <div className="date pod-ic">📝</div>
      <span className="info">
        <b>{p.title}</b>
        {p.summary && <small>{p.summary}</small>}
        <small><StatusBadge s={p.status} /> v{p.version}{p.poll && p.status === 'voting' ? ` · ${t.yes ?? 0} yes · ${t.no ?? 0} no` : ''}</small>
      </span>
    </div>
  )
}

/** Above a proposal's chat: the text, version, status, vote. Folds to one line. */
export function ProposalHeader({ groupId }: { groupId: string }) {
  useVersion()
  const p = proposalFor(groupId)
  const [open, setOpen] = useState(true)
  const [revising, setRevising] = useState(false)
  const [status, setStatus] = useState(false)
  if (!p) return <div className="prop-head"><span className="hint">The proposal's text is on its way (syncing).</span></div>
  const t = tally(p)
  const mine = p.poll?.votes.get(me())
  const voting = p.status === 'voting' && p.poll && p.poll.version === p.version
  return (
    <div className="prop-head">
      <button className="prop-line" onClick={() => setOpen(!open)}>
        <StatusBadge s={p.status} />
        <span className="hint">Version {p.version} · by {displayName(p.author)}</span>
        <span className="hint" aria-hidden="true">{open ? '▴' : '▾'}</span>
      </button>
      {open && (
        <>
          {p.text ? <Markdown className="prop-text" text={p.text} /> : <p className="hint">No text yet.</p>}
          {voting && (
            <div className="rsvp" style={{ marginTop: 10 }}>
              {VOTE_OPTIONS.map(([id, label]) => (
                <button key={id} className={mine === id ? 'on' : ''} disabled={!!p.poll?.ev.pending} onClick={() => void vote(p, id)}>
                  {label} <small>{t[id] ?? 0}</small>
                </button>
              ))}
            </div>
          )}
          {!voting && p.poll && (p.status === 'passed' || p.status === 'rejected' || p.status === 'closed') && (
            <p className="hint" style={{ marginTop: 8 }}>Vote on version {p.poll.version}: {t.yes ?? 0} yes · {t.no ?? 0} no · {t.abstain ?? 0} abstain</p>
          )}
          <div className="row" style={{ gap: 8, marginTop: 10, flexWrap: 'wrap' }}>
            {p.author === me() && <button className="btn small ghost" onClick={() => setRevising(true)}>New version</button>}
            {canDecide(p) && <button className="btn small ghost" onClick={() => setStatus(true)}>Change status</button>}
          </div>
        </>
      )}
      {revising && <ReviseSheet p={p} onClose={() => setRevising(false)} />}
      {status && (
        <Sheet title="Status" onClose={() => setStatus(false)}>
          {STATUSES.map((s) => (
            <button key={s} className={'btn ' + (s === p.status ? '' : 'ghost')} style={{ marginBottom: 8 }}
              onClick={() => { setStatus(false); if (s !== p.status) void setProposalStatus(p, s) }}>{STATUS_LABEL[s]}</button>
          ))}
          <p className="hint">Opening the votes starts a vote on version {p.version}.</p>
        </Sheet>
      )}
    </div>
  )
}

function Fields({ n, set }: { n: Omit<NewProposal, 'status'>; set: (patch: Partial<NewProposal>) => void }) {
  return (
    <>
      <label className="field"><span>Title</span>
        <input className="input" autoFocus placeholder="What do you propose?" value={n.title} onChange={(e) => set({ title: e.target.value })} /></label>
      <label className="field"><span>In one line</span>
        <input className="input" value={n.summary} onChange={(e) => set({ summary: e.target.value })} /></label>
      <label className="field"><span>The proposal</span>
        <textarea className="input" rows={6} placeholder="Markdown: **bold**, - lists, [links](https://…)" value={n.text} onChange={(e) => set({ text: e.target.value })} /></label>
    </>
  )
}

/** FROM: a request (#requests) a member turns into a proposal: its words to start from, a link
 *  back in the text, and ONMADE to say so in the request's thread. */
export function NewProposalSheet({ parent, onClose, from, onMade, website }: { parent: string; onClose: () => void; from?: { title: string; text: string; by: string; link: string }; onMade?: (id: string, title: string) => void
  /** a new version of the website (#issues 2026-11-13): its nsite ref, and who made it */
  website?: { ref: string; by: string } }) {
  const [n, setN, clearDraft] = useFormDraft<NewProposal>(from || website ? null : 'proposal:' + parent, () => website
    ? { title: 'A new version of the website', summary: `By ${website.by}`, text: `Preview it: ${previewLink(website.ref)}\n\nWhat changes:\n- `, status: 'feedback', website: website.ref }
    : { title: from?.title ?? '', summary: '', text: from ? `${from.text}\n\n_From a request by ${from.by}: ${from.link}_` : '', status: 'feedback' })
  const set = (patch: Partial<NewProposal>) => setN((x) => ({ ...x, ...patch }))
  const where = group(parent)
  // a website version: "Only this part of the site", prefilled with the one folder all its files
  // are in (read from its manifest) until the person types there
  const [scope, setScope] = useState('')
  const touched = useRef(false)
  useEffect(() => {
    if (!website) return
    let live = true
    void siteFiles(website.ref).then((f) => { const r = f && rootFolder(f); if (live && r && !touched.current) setScope(r) })
    return () => { live = false }
  }, [website?.ref])
  const parsed = website ? parsePaths(scope) : { paths: [] }
  const paths = 'paths' in parsed ? parsed.paths : null
  const final = (): NewProposal => website && paths ? { ...n, title: websiteTitle(n.title, paths), paths } : n
  return (
    <Sheet title="New proposal" onClose={onClose}>
      <Form onSubmit={async (e) => { e.preventDefault(); if (!paths) return; const m = final(); const id = await createProposal(m, parent); clearDraft(); onMade?.(id, m.title); onClose(); go('c/' + encodeURIComponent(id)) }}>
        <Fields n={n} set={set} />
        {website && (
          <label className="field prop-scope-field"><span>Only this part of the site <small className="hint">(optional)</small></span>
            <input className="input" placeholder="/ios/screenshots/" autoCapitalize="off" autoCorrect="off" spellCheck={false} value={scope}
              onChange={(e) => { touched.current = true; setScope(e.target.value) }} aria-invalid={!paths} />
            {paths
              ? <small className="hint">{paths.length ? `Publishing it changes only ${scopeWords(paths)}; the rest of the website stays as it is.` : 'Empty: this version replaces the whole website.'} Posted as “{websiteTitle(n.title, paths)}”.</small>
              : <small className="error">{'error' in parsed ? parsed.error : ''}</small>}
          </label>
        )}
        <div className="choice">
          <button type="button" className={n.status === 'draft' ? 'on' : ''} onClick={() => set({ status: 'draft' })}><b>Draft</b><small>Still writing it</small></button>
          <button type="button" className={n.status === 'feedback' ? 'on' : ''} onClick={() => set({ status: 'feedback' })}><b>Open for feedback</b><small>Ask for comments</small></button>
        </div>
        <button className="btn" disabled={!n.title.trim() || !paths}>Post proposal</button>
        <p className="hint" style={{ marginTop: 10 }}>Posted in {where?.name ?? parent}. It gets its own chat for feedback; you can publish new versions.{n.website ? ` Once an admin marks it Passed, this version goes live at ${BELFRY.domain}.` : ''}</p>
      </Form>
    </Sheet>
  )
}

function ReviseSheet({ p, onClose }: { p: Proposal; onClose: () => void }) {
  const [n, setN] = useState({ title: p.title, summary: p.summary, text: p.text })
  const [note, setNote] = useState('')
  return (
    <Sheet title={`Version ${p.version + 1}`} onClose={onClose}>
      <Form onSubmit={async (e) => { e.preventDefault(); await reviseProposal(p, p.website ? { ...n, title: websiteTitle(n.title, p.paths) } : n, note); onClose() }}>
        <Fields n={n} set={(patch) => setN({ ...n, ...patch })} />
        <label className="field"><span>What changed</span>
          <input className="input" placeholder="e.g. Budget lowered after feedback" value={note} onChange={(e) => setNote(e.target.value)} /></label>
        <button className="btn" disabled={!n.title.trim()}>Publish version {p.version + 1}</button>
        {p.status === 'voting' && <p className="hint" style={{ marginTop: 10 }}>The vote restarts on the new version.</p>}
      </Form>
    </Sheet>
  )
}

/** Where to look at an nsite version: its preview on this belfry (nsite:<npub>[/<name>] → preview.<domain>/p/<npub>[@<name>]). */
export function previewLink(ref: string): string {
  const m = /^nsite:(npub1[02-9ac-hj-np-z]{58})(?:\/([a-z0-9-]{1,13}))?$/.exec(ref)
  return m ? `https://preview.${BELFRY.domain}/p/${m[1]}${m[2] ? '@' + m[2] : ''}` : ref
}

/** Under a website proposal's header, always (#issues 2026-11-13): look at the version, and
 *  how it goes live. */
export function SiteBar({ groupId }: { groupId: string }) {
  useVersion()
  const p = proposalFor(groupId)
  if (!p?.website) return null
  return (
    <div className="prop-site">
      <a className="btn small" href={previewLink(p.website)} target="_blank" rel="noreferrer">Preview this version</a>
      <span className="hint">{p.status === 'passed' ? `Passed: it is (or is about to be) live at ${BELFRY.domain}${p.paths.length ? ` (only ${scopeWords(p.paths)})` : ''}.` : `A new version of ${p.paths.length ? `${scopeWords(p.paths)} on ` : ''}${BELFRY.domain}: it goes live once a belfry admin marks this proposal Passed.`}</span>
    </div>
  )
}

// ---- the proposal at the top of its chat (Xavier, 2026-11-18, canvas "Proposal-A/B") -----------
// Like an event's screen: one header at three levels — 0 a bar (title, status, tally, Vote),
// 1 half (opens here: status, by whom and which version, the title, the start of the text, the
// vote with its counts and who has voted), 2 full (the whole text, then New version / Change
// status for those who may) — over Chat · Versions · Votes (only the tabs with something in them).
// Pull the header or its handle; scrolling or typing in the chat folds it to the bar.

type PLevel = 0 | 1 | 2
type PTab = 'chat' | 'versions' | 'votes'

/** The options of the proposal's poll (its NIP-88 `option` tags), else yes / no / abstain. */
export function pollOptions(p: Proposal): [string, string][] {
  const tags = p.poll?.ev.tags.filter((t) => t[0] === 'option' && t[1]).map((t) => [t[1], t[2] || t[1]] as [string, string]) ?? []
  return tags.length ? tags : VOTE_OPTIONS.map(([id, label]) => [id, label] as [string, string])
}

/** Who voted and who could have: the voters, of the chat's members and the voters themselves. */
export function voteCounts(p: Proposal) {
  const g = group(p.groupId)
  const voters = p.poll?.votes.size ?? 0
  const people = new Set([...(g?.members ?? []), ...(p.poll?.votes.keys() ?? [])]).size
  return { voters, people }
}

/** The versions it had: its first (when its card was posted), then each "📝 Version N" note in its
 *  chat (reviseProposal), with the text when this device still holds that version's event. */
export function proposalVersions(p: Proposal): { version: number; at: number; by: string; note: string; text?: string }[] {
  const texts = new Map<number, string>()
  for (const e of all(me())) if (e.kind === 30023 && `30023:${e.pubkey}:${e.tags.find((t) => t[0] === 'd')?.[1]}` === p.coord) texts.set(Number(e.tags.find((t) => t[0] === 'version')?.[1]) || 1, e.content)
  const card = all(me()).find((e) => e.kind === 9 && e.tags.some((t) => t[0] === 'proposal' && t[1] === p.groupId))
  const out: { version: number; at: number; by: string; note: string; text?: string }[] = [{ version: 1, at: card?.created_at ?? 0, by: p.author, note: '', text: texts.get(1) }]
  for (const m of messages(p.groupId)) {
    const v = Number(m.ev.tags.find((t) => t[0] === 'version')?.[1])
    if (!v || v < 2 || !m.ev.tags.some((t) => t[0] === 'a' && t[1] === p.coord) || out.some((x) => x.version === v)) continue
    out.push({ version: v, at: m.ev.created_at, by: m.ev.pubkey, note: m.text.replace(/^📝 Version \d+:?\s*/, ''), text: texts.get(v) })
  }
  return out.sort((a, b) => b.version - a.version)
}

const VOTE_ICON: Record<string, string> = { yes: '✓', no: '✗', abstain: '–' }

/** The folded bar's tally, longest first: "12 yes · 2 no · 1 abstain", then "✓12 ✗2 –1" (an
 *  option without an icon of its own shows its first letter), then dropping options from the end
 *  (abstain goes first). The bar shows the first that fits; the last one may still be cut. */
export function tallyForms(opts: [string, string][], count: Record<string, number>): string[] {
  const full = opts.map(([o, label]) => `${count[o] ?? 0} ${label.toLowerCase()}`).join(' · ')
  const short = opts.map(([o, label]) => `${VOTE_ICON[o] ?? label.trim().charAt(0).toUpperCase()}${count[o] ?? 0}`)
  const out = [full]
  for (let n = short.length; n >= 1; n--) out.push(short.slice(0, n).join(' '))
  return out
}

/** Shows the first of `forms` that fits its box (re-measured when the box changes width). */
function FitText({ forms, className, title }: { forms: string[]; className?: string; title?: string }) {
  const ref = useRef<HTMLSpanElement>(null)
  const [i, setI] = useState(0)
  const key = forms.join('\n')
  useEffect(() => setI(0), [key])
  useLayoutEffect(() => {
    const el = ref.current
    if (el && i < forms.length - 1 && el.scrollWidth > el.clientWidth + 1) setI(i + 1)
  })
  useEffect(() => {
    const el = ref.current
    if (!el || typeof ResizeObserver === 'undefined') return
    let w = el.clientWidth
    const ro = new ResizeObserver(() => { if (el.clientWidth !== w) { w = el.clientWidth; setI(0) } })
    ro.observe(el)
    return () => ro.disconnect()
  }, [])
  return <span ref={ref} className={className} title={title}>{forms[Math.min(i, forms.length - 1)]}</span>
}

/** prop-<id>: the proposal on top, its chat under it. */
export function ProposalView({ id, left, topic, onTopic, focus, activeThread }: { id: string; left: ReactNode; topic?: string; onTopic: (t?: string) => void; focus?: string; activeThread?: string }) {
  useVersion()
  const p = proposalFor(id)
  const g = group(id)
  const [level, setLevel] = useState<PLevel>(1)
  const [tab, setTab] = useState<PTab>('chat')
  const [menu, setMenu] = useState(false)
  const [revising, setRevising] = useState(false)
  const [status, setStatus] = useState(false)
  useEffect(() => { markSeen(id) }, [id])
  useEffect(() => { setLevel(1); setTab('chat') }, [id])
  useEffect(() => {
    const k = (x: KeyboardEvent) => { if (x.key === 'Escape' && level === 2 && !menu && !revising && !status) setLevel(1) }
    addEventListener('keydown', k)
    return () => removeEventListener('keydown', k)
  }, [level, menu, revising, status])

  // pulling the header (as the event's: vertical drags only, a tap stays a tap)
  const start = useRef<{ y: number; x: number; dragging: boolean } | null>(null)
  const [drag, setDrag] = useState(0)
  const pull = {
    onPointerDown: (x: React.PointerEvent) => { if (x.pointerType === 'mouse' && x.button !== 0) return; if ((x.target as HTMLElement).closest('textarea, input')) return; start.current = { y: x.clientY, x: x.clientX, dragging: false } },
    onPointerMove: (x: React.PointerEvent) => {
      const st = start.current
      if (!st) return
      const dy = x.clientY - st.y, dx = x.clientX - st.x
      if (!st.dragging) { if (Math.abs(dy) < 10 || Math.abs(dy) < Math.abs(dx)) return; st.dragging = true; (x.currentTarget as HTMLElement).setPointerCapture(x.pointerId) }
      setDrag(dy)
    },
    onPointerUp: () => { const st = start.current; start.current = null; if (st?.dragging) { if (drag > 40) setLevel((l) => Math.min(2, l + 1) as PLevel); else if (drag < -40) setLevel((l) => Math.max(0, l - 1) as PLevel) } setDrag(0) },
    onPointerCancel: () => { start.current = null; setDrag(0) },
    onClickCapture: (x: React.MouseEvent) => { if (start.current?.dragging) { x.stopPropagation(); x.preventDefault() } },
  }
  const fold = () => { if (level === 1) setLevel(0) }

  const parent = g?.parent ? group(g.parent) : undefined
  const where = parent ? (parent.isPod ? parent.name : '#' + parent.name) : ''
  const right = (
    <>
      <SaveFailedIcon id={id} />
      <button className="icon-btn" aria-label={`Search ${g?.name ?? id}`} onClick={() => go(`search/${encodeURIComponent(id)}`)}><SearchIcon /></button>
      <button className="icon-btn" aria-label="More about this proposal" onClick={() => setMenu(true)}><MoreIcon /></button>
    </>
  )
  if (!p) return (
    <div className="screen prop-screen">
      <div className="topbar">{left}<h1>{g?.name ?? 'Proposal'}<small>Proposal{where ? ' in ' + where : ''}</small></h1>{right}</div>
      <div className="prop-wait hint">The proposal’s text is on its way (syncing).</div>
      <Conversation id={id} events topic={topic} onTopic={onTopic} focus={focus} activeThread={activeThread} />
      {menu && <Sheet title={g?.name ?? 'Proposal'} onClose={() => setMenu(false)}><ChannelMenu id={id} onPick={() => setMenu(false)} /></Sheet>}
    </div>
  )

  const opts = pollOptions(p)
  const mine = p.poll?.votes.get(me())
  const voting = p.status === 'voting' && !!p.poll && p.poll.version === p.version
  const count: Record<string, number> = {}
  for (const v of p.poll?.votes.values() ?? []) count[v] = (count[v] ?? 0) + 1
  const tallyText = p.poll ? opts.map(([o, label]) => `${count[o] ?? 0} ${label.toLowerCase()}`).join(' · ') : ''
  const { voters, people } = voteCounts(p)
  const mineLabel = opts.find(([o]) => o === mine)?.[1]
  const versions = proposalVersions(p)
  const tabs: [PTab, string][] = [['chat', 'Chat'], ...(versions.length > 1 ? [['versions', 'Versions'] as [PTab, string]] : []), ...(p.poll && voters > 0 ? [['votes', 'Votes'] as [PTab, string]] : [])]
  const shownTab = tabs.some(([k]) => k === tab) ? tab : 'chat'

  const voteRow = voting && (
    <div className="prop-votes" role="group" aria-label="Your vote">
      {opts.map(([o, label]) => (
        <Btn key={o} className={'prop-v' + (mine === o ? ' on' : '')} aria-pressed={mine === o} disabled={!!p.poll?.ev.pending} onClick={() => vote(p, o) ?? undefined}>
          {label}<span>{count[o] ?? 0}</span>
        </Btn>
      ))}
    </div>
  )
  const voteLine = p.poll && (voting
    ? <p className="hint prop-voted">{mineLabel ? `You voted ${mineLabel.toLowerCase()}` : 'You haven’t voted yet'} · {voters} of {people} {people === 1 ? 'member has' : 'members have'}</p>
    : <p className="hint prop-voted">Vote on version {p.poll.version}: {tallyText}</p>)

  const header = level > 0 && (
    <div className={'evh prop-head2 lvl' + level + (drag ? ' dragging' : '')} {...pull} style={drag ? { transform: `translateY(${Math.max(-30, Math.min(30, drag / 3))}px)` } : undefined}>
      <div className={'evh-body' + (level === 2 ? ' full' : '')}>
        <div className="evh-main prop-main">
          <p className="prop-meta"><StatusBadge s={p.status} /><span className="hint">Proposal by <button className="link who" onClick={() => openProfile(p.author)}>{displayName(p.author)}</button> · version {p.version}</span></p>
          <h2 className="prop-title">{p.title}</h2>
          {p.website && p.paths.length > 0 && <p className="prop-scope"><GlobeIcon /> Changes only {scopeWords(p.paths)}</p>}
          {p.text ? <Markdown className={'prop-text' + (level === 1 ? ' teaser' : '')} text={p.text} /> : <p className="hint">No text yet.</p>}
          {level === 1 && p.text && <button className="link prop-more" onClick={() => setLevel(2)}>Read the full proposal</button>}
          {voteRow}
          {voteLine}
          {level === 2 && (canDecide(p) || p.author === me()) && (
            <div className="row prop-acts">
              {p.author === me() && <button className="btn small ghost" onClick={() => setRevising(true)}>New version</button>}
              {canDecide(p) && <button className="btn small ghost" onClick={() => setStatus(true)}>Change status</button>}
            </div>
          )}
        </div>
      </div>
      <button className="evh-handle" onClick={() => setLevel(level === 1 ? 2 : 1)} aria-label={level === 1 ? 'Read the full proposal' : 'Show less'}>
        <span className="grip" />{level === 1 ? <ChevronDownIcon /> : <ChevronUpIcon />}
      </button>
    </div>
  )

  return (
    <div className="screen prop-screen">
      <div className="topbar prop-top">
        {left}
        {level === 0 ? (
          <button className="prop-bar" onClick={() => setLevel(1)} aria-label={`Show the proposal: ${p.title}, ${STATUS_LABEL[p.status] ?? p.status}${tallyText ? ', ' + tallyText : ''}`}>
            <b>{p.title}</b>
            <small><StatusBadge s={p.status} />{p.poll && <FitText className="prop-tally" forms={tallyForms(opts, count)} title={tallyText} />}</small>
          </button>
        ) : (
          <h1 className="tap" onClick={() => setLevel(0)}>Proposal<small>{where ? 'In ' + where : 'Proposal'}</small></h1>
        )}
        {level === 0 && voting && (mine
          ? <button className="chip on prop-mine" onClick={() => setLevel(1)}>Voted {mineLabel?.toLowerCase()}</button>
          : <button className="btn small prop-vote-btn" onClick={() => setLevel(1)}>Vote</button>)}
        {right}
      </div>
      {header}
      <SiteBar groupId={id} />
      {level < 2 && <>
        {tabs.length > 1 && (
          <div className="seg ev-tabs prop-tabs" role="tablist" aria-label="The proposal">
            {tabs.map(([k, l]) => <button key={k} role="tab" aria-selected={shownTab === k} className={shownTab === k ? 'on' : ''} onClick={() => setTab(k)}>{l}</button>)}
          </div>
        )}
        <div className="ev-content" onWheel={fold} onTouchMove={fold} onFocusCapture={(x) => { if ((x.target as HTMLElement).matches('textarea, input')) fold() }} onInputCapture={fold}>
          {shownTab === 'chat' && <Conversation id={id} events topic={topic} onTopic={onTopic} focus={focus} activeThread={activeThread} />}
          {shownTab === 'versions' && <Versions p={p} onRead={() => setLevel(2)} />}
          {shownTab === 'votes' && <Votes p={p} opts={opts} />}
        </div>
      </>}
      {menu && <Sheet title={p.title} onClose={() => setMenu(false)}><ChannelMenu id={id} onPick={() => setMenu(false)} /></Sheet>}
      {revising && <ReviseSheet p={p} onClose={() => setRevising(false)} />}
      {status && (
        <Sheet title="Status" onClose={() => setStatus(false)}>
          {STATUSES.map((st) => (
            <button key={st} className={'btn ' + (st === p.status ? '' : 'ghost')} style={{ marginBottom: 8 }}
              onClick={() => { setStatus(false); if (st !== p.status) void setProposalStatus(p, st) }}>{STATUS_LABEL[st]}</button>
          ))}
          <p className="hint">Opening the votes starts a vote on version {p.version}.</p>
        </Sheet>
      )}
    </div>
  )
}

function Versions({ p, onRead }: { p: Proposal; onRead: () => void }) {
  const [open, setOpen] = useState<number | null>(null)
  return (
    <div className="scroll pad prop-versions">
      {proposalVersions(p).map((v) => (
        <div key={v.version} className="prop-version">
          <div className="pv-head">
            <b>Version {v.version}</b>
            <span className="hint">{v.at ? fmtDay(new Date(v.at * 1000)) : ''}{v.version === p.version ? ' · current' : ''}</span>
          </div>
          {v.note && <p className="pv-note">{v.note}</p>}
          {v.version === p.version
            ? <button className="link" onClick={onRead}>Read it</button>
            : v.text ? (open === v.version ? <Markdown className="prop-text" text={v.text} /> : <button className="link" onClick={() => setOpen(v.version)}>Read it</button>) : null}
        </div>
      ))}
    </div>
  )
}

function Votes({ p, opts }: { p: Proposal; opts: [string, string][] }) {
  const by = new Map<string, string[]>()
  for (const [pk, o] of p.poll?.votes ?? []) by.set(o, [...(by.get(o) ?? []), pk])
  return (
    <div className="scroll pad prop-votes-list">
      <p className="hint">Vote on version {p.poll?.version}. The latest answer of each person counts.</p>
      {opts.map(([o, label]) => (
        <section key={o}>
          <h3>{label} · {by.get(o)?.length ?? 0}</h3>
          {(by.get(o) ?? []).map((pk) => (
            <button key={pk} className="ev-person" onClick={() => openProfile(pk, p.groupId)}><Avatar pubkey={pk} size={32} /><span className="t"><b>{displayName(pk)}</b></span></button>
          ))}
        </section>
      ))}
    </div>
  )
}
