import { BELFRY } from '../config'
import { useState } from 'react'
import { createProposal, reviseProposal, setProposalStatus, vote, type NewProposal } from '../core/actions'
import { canDecide, displayName, groups, proposalFor, STATUS_LABEL, STATUSES, VOTE_OPTIONS, type Proposal, type ProposalStatus } from '../core/model'
import { useVersion } from '../core/store'
import { me } from '../core/sync'
import { go, Sheet, Form, useFormDraft } from './common'
import { Markdown } from './markdown'

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
  const where = groups().find((g) => g.id === parent)
  return (
    <Sheet title="New proposal" onClose={onClose}>
      <Form onSubmit={async (e) => { e.preventDefault(); const id = await createProposal(n, parent); clearDraft(); onMade?.(id, n.title); onClose(); go('c/' + encodeURIComponent(id)) }}>
        <Fields n={n} set={set} />
        <div className="choice">
          <button type="button" className={n.status === 'draft' ? 'on' : ''} onClick={() => set({ status: 'draft' })}><b>Draft</b><small>Still writing it</small></button>
          <button type="button" className={n.status === 'feedback' ? 'on' : ''} onClick={() => set({ status: 'feedback' })}><b>Open for feedback</b><small>Ask for comments</small></button>
        </div>
        <button className="btn" disabled={!n.title.trim()}>Post proposal</button>
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
      <Form onSubmit={async (e) => { e.preventDefault(); await reviseProposal(p, n, note); onClose() }}>
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
      <span className="hint">{p.status === 'passed' ? `Passed: it is (or is about to be) live at ${BELFRY.domain}.` : `A new version of ${BELFRY.domain}: it goes live once a belfry admin marks this proposal Passed.`}</span>
    </div>
  )
}
