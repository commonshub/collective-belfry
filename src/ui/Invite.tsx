// Invite people to a channel (Xavier, 2026-11-12, canvas "Invite-C": the link first). The QR
// and the link are there at once, for the safest choice: a guest of this channel for a day. One
// row, "Joins as", changes who they come in as: a guest for a day, a week or a month; the
// member's own agent; or (the belfry's admins only) a member of the belfry with a role, for a
// month, a year or for good. A second row, "Link", is the link itself: for one person or up to
// 50, and how long it can be opened. Belfry's /api/channel-invites (guests.py, roles.py). Under
// it: someone who is here already, by name.
import { useEffect, useMemo, useRef, useState } from 'react'
import qrcode from 'qrcode-generator'
import { channelIcon, displayName, group, profiles } from '../core/model'
import { inChannel, inviteMember, isAgent, isGuest, LinkFailed, makeInviteLink, myAgents, type GuestDays, type JoinsAs, type LinkOpts, type Membership, type MyAgents } from '../core/invite'
import { agentOwner } from '../core/access'
import { isBelfryAdmin } from '../core/people'
import { defaultRole, loadRoles, roleById } from '../core/roles'
import { loadMembership, mayInviteMembers } from '../core/requests'
import { me } from '../core/sync'
import { useVersion } from '../core/store'
import { Avatar, BackHead, Btn, go, Sheet } from './common'
import { useCommunityName } from './community'
import { ChevronRightIcon, PencilIcon } from './icons'
import { RoleList } from './RolePicker'
import { NAME } from '../config'

const DAYS_WORD: Record<GuestDays, string> = { 1: 'a day', 7: 'a week', 30: 'a month' }
const MEMBERSHIP_WORD: Record<Membership, string> = { month: 'one month', year: 'one year', permanent: 'permanent' }

/** What "Joins as" says: a name and a line (the canvas's wording). */
export function joinsAsLine(who: JoinsAs, channel: string, community: string): [string, string] {
  if (who.as === 'guest') return [`Guest for ${DAYS_WORD[who.days]}`, `${channel} only, for ${DAYS_WORD[who.days]}`]
  if (who.as === 'agent') return ['My own agent', 'Your AI agent: a member for as long as you are one']
  const label = roleById(who.role)?.label ?? 'Member'
  return [`${label} · ${MEMBERSHIP_WORD[who.membership]}`, `A member of ${community}, starting in ${channel}`]
}
export function linkLine(link: LinkOpts): string {
  return `${link.people === 1 ? 'One person' : 'Up to 50 people'}, the link works for ${DAYS_WORD[link.linkDays]}`
}

type View = 'main' | 'joins' | 'link'

export function InviteSheet({ id, onClose }: { id: string; onClose: () => void }) {
  useVersion()
  const g = group(id)
  const community = useCommunityName(NAME)
  // members of the belfry: its admins invite them, and any member when its Membership settings
  // say so (then with the default role only; Belfry refuses the rest: 403 not_admin)
  const admin = isBelfryAdmin(me())
  const members = mayInviteMembers()
  useEffect(() => { void loadMembership() }, [])
  useEffect(() => { if (members) void loadRoles() }, [members])
  const [view, setView] = useState<View>('main')
  const [who, setWho] = useState<JoinsAs>({ as: 'guest', days: 1 })
  const [link, setLink] = useState<LinkOpts>({ people: 1, linkDays: 1 })
  const [membership, setMembership] = useState<Membership>('permanent')
  // a member's own agent: the link is for one agent, which stays a member for as long as I am one
  const [mine, setMine] = useState<MyAgents | null>(null)
  useEffect(() => { myAgents().then(setMine).catch(() => setMine(null)) }, [])
  const room = mine ? mine.limit - mine.agents.length : 0
  const [url, setUrl] = useState('')
  const [agentUrl, setAgentUrl] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [copied, setCopied] = useState(false)
  const asked = useRef(0)
  const label = `${channelIcon(g)}${g?.isPod || g?.isEvent || g?.isProposal ? ' ' : ''}${g?.name ?? id}`
  // an admin link is for one person, whatever "Link" says
  const people = who.as === 'agent' || (who.as === 'member' && who.role === 'admin') ? 1 : link.people
  const fetchLink = async () => {
    const n = ++asked.current // a newer choice wins
    setBusy(true); setError(''); setUrl(''); setAgentUrl('')
    try {
      const got = await makeInviteLink(id, who, { ...link, people })
      if (n !== asked.current) return
      setUrl(got.url); setAgentUrl(got.agentUrl)
    } catch (e) {
      if (n !== asked.current) return
      setError(e instanceof LinkFailed ? e.message : 'The link could not be made. Try again.')
    } finally { if (n === asked.current) setBusy(false) }
  }
  const key = JSON.stringify([who, link.linkDays, people])
  useEffect(() => { setCopied(false); void fetchLink() }, [key]) // eslint-disable-line react-hooks/exhaustive-deps
  const pending = !url
  const qr = useMemo(() => {
    const q = qrcode(0, 'M')
    q.addData(url || `https://chat.example.org/join/${'0'.repeat(32)}`) // the last shape stays, faded, while it loads
    q.make()
    return 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(q.createSvgTag({ cellSize: 6, margin: 2, scalable: true }))
  }, [url])
  const copy = async () => {
    try { await navigator.clipboard.writeText(url) } catch {
      const t = document.createElement('textarea'); t.value = url; document.body.appendChild(t); t.select(); document.execCommand('copy'); t.remove()
    }
    setCopied(true); setTimeout(() => setCopied(false), 1500)
  }
  const [asName, asDesc] = joinsAsLine(who, label, community)
  const pick = (w: JoinsAs) => { setWho(w); setView('main') }
  const isOn = (w: JoinsAs) => JSON.stringify(w) === JSON.stringify(who)

  if (view === 'joins') {
    const memberRole = who.as === 'member' ? who.role : undefined
    return (
      <Sheet title="Joins as" hideTitle onClose={onClose}>
        <BackHead title="Joins as" onBack={() => setView('main')} />
        <div className="pick-head">{g?.isPod ? `Into ${label} only, as a guest` : `Guest of ${label}`}</div>
        <div className="pick-group" role="radiogroup" aria-label={`Guest of ${label}`}>
          {([1, 7, 30] as const).map((d) => (
            <button key={d} type="button" role="radio" aria-checked={isOn({ as: 'guest', days: d })} className={'pick-row' + (isOn({ as: 'guest', days: d }) ? ' on' : '')} onClick={() => pick({ as: 'guest', days: d })}>
              <span className="radio" aria-hidden="true" /><span className="t"><b>For {DAYS_WORD[d]}</b></span>
            </button>
          ))}
        </div>
        {mine && mine.limit > 0 && (
          <div className="pick-group" role="radiogroup" aria-label="An agent" style={{ marginTop: 12 }}>
            <button type="button" role="radio" aria-checked={who.as === 'agent'} disabled={room <= 0 && who.as !== 'agent'} className={'pick-row' + (who.as === 'agent' ? ' on' : '')} onClick={() => pick({ as: 'agent' })}>
              <span className="radio" aria-hidden="true" />
              <span className="t"><b>My own agent</b><small>{room > 0 || who.as === 'agent'
                ? 'For your AI agent: it joins as a member and stays for as long as you are one. Everyone sees it is yours.'
                : `You have ${mine.limit === 1 ? 'your agent' : `your ${mine.limit} agents`} already (${mine.agents.map((a) => a.name || 'Agent').join(', ')}). Remove one from its profile to bring another.`}</small></span>
            </button>
          </div>
        )}
        {members && <>
          <div className="pick-head">Member of {community}</div>
          <div className="invite-opts" role="group" aria-label="Length of membership">
            <div><span>Membership</span>
              <div className="seg small">{(['month', 'year', 'permanent'] as const).map((m) => <button key={m} type="button" className={membership === m ? 'on' : ''} aria-pressed={membership === m}
                onClick={() => { setMembership(m); if (who.as === 'member') setWho({ ...who, membership: m }) }}>{m === 'month' ? 'One month' : m === 'year' ? 'One year' : 'Permanent'}</button>)}</div></div>
          </div>
          {admin
            ? <RoleList value={memberRole} onPick={(r) => pick({ as: 'member', role: r, membership })} />
            : <div className="pick-group" role="radiogroup" aria-label="Member">
                <button type="button" role="radio" aria-checked={who.as === 'member'} className={'pick-row' + (who.as === 'member' ? ' on' : '')} onClick={() => pick({ as: 'member', role: defaultRole(), membership })}>
                  <span className="radio" aria-hidden="true" /><span className="t"><b>{roleById(defaultRole())?.label ?? 'Member'}</b><small>A member of {community}, not only of {label}</small></span>
                </button>
              </div>}
          {admin && <button type="button" className="pick-row edit-roles" onClick={() => { onClose(); go('settings/roles') }}><PencilIcon /><span>Edit roles…</span></button>}
        </>}
        {!members && g?.isPod && <p className="hint" style={{ fontSize: 13, marginTop: 12 }}>This brings people into {label} only. Only admins of {community} make someone a member of {community}.</p>}
      </Sheet>
    )
  }
  if (view === 'link') {
    return (
      <Sheet title="Link" hideTitle onClose={onClose}>
        <BackHead title="Link" onBack={() => setView('main')} />
        <div className="pick-head">How many people</div>
        <div className="pick-group" role="radiogroup" aria-label="How many people">
          {([[1, 'One person'], [50, 'Up to 50 people']] as const).map(([n, l]) => (
            <button key={n} type="button" role="radio" aria-checked={link.people === n} disabled={people === 1 && n === 50 && who.as !== 'guest'} className={'pick-row' + (link.people === n ? ' on' : '')} onClick={() => { setLink({ ...link, people: n }); setView('main') }}>
              <span className="radio" aria-hidden="true" /><span className="t"><b>{l}</b></span>
            </button>
          ))}
        </div>
        <div className="pick-head">The link can be opened for</div>
        <div className="pick-group" role="radiogroup" aria-label="How long the link works">
          {([1, 7, 30] as const).map((d) => (
            <button key={d} type="button" role="radio" aria-checked={link.linkDays === d} className={'pick-row' + (link.linkDays === d ? ' on' : '')} onClick={() => { setLink({ ...link, linkDays: d }); setView('main') }}>
              <span className="radio" aria-hidden="true" /><span className="t"><b>{d === 1 ? '1 day' : d === 7 ? '1 week' : '1 month'}</b></span>
            </button>
          ))}
        </div>
        <p className="hint" style={{ fontSize: 13 }}>How long they stay is chosen under “Joins as”.</p>
      </Sheet>
    )
  }
  return (
    <Sheet title={`Invite to ${label}`} onClose={onClose}>
      <div className={'qr-box' + (pending ? ' waiting' : '')}>
        <img className="drop-qr" src={qr} alt={pending ? '' : 'QR code of the invite link'} />
        {pending && busy && <span className="qr-over" role="status" aria-label="Making the link"><span className="spinner" /></span>}
        {pending && !busy && error && (
          <span className="qr-over error" role="alert">
            <svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" aria-hidden="true"><circle cx="12" cy="12" r="9.5" /><path d="M12 7.5v5.5M12 16.5v.01" /></svg>
            <span className="qr-err">{error}</span>
            <Btn className="btn small" onClick={() => fetchLink()}>Retry</Btn>
          </span>
        )}
      </div>
      <div className="pick-group invite-rows">
        <button type="button" className="pick-row joins-as" onClick={() => setView('joins')}>
          <span className="k">Joins as</span><span className="t"><b>{asName}</b><small>{asDesc}</small></span><ChevronRightIcon />
        </button>
        <button type="button" className="pick-row link-row" onClick={() => setView('link')}>
          <span className="k">Link</span><span className="t"><b>{linkLine({ ...link, people })}</b></span><ChevronRightIcon />
        </button>
      </div>
      <div className="drop-url"><code>{pending ? (busy ? 'Making the link…' : '—') : url}</code></div>
      <div className="row" style={{ gap: 8, marginTop: 10 }}>
        <Btn className="btn ghost" style={{ flex: 1 }} disabled={pending} onClick={() => copy()}>{copied ? 'Copied' : 'Copy link'}</Btn>
        {'share' in navigator && <Btn className="btn" style={{ flex: 1 }} disabled={pending} onClick={() => navigator.share({ title: label, url }).catch(() => {})}>Share…</Btn>}
      </div>
      <p className="hint" style={{ fontSize: 13, marginTop: 12 }}>
        {who.as === 'agent'
          ? <>Give this link to your agent, within {DAYS_WORD[link.linkDays]}: it reads the instructions there and joins with its own key. It works once.</>
          : who.as === 'member'
          ? <>They read {label} at once and join {community} as {roleById(who.role)?.label ?? 'Member'} with their name and e-mail address. Someone who is a member already just gets the channel.</>
          : <>They read {label} at once and join with a name and an e-mail address; someone who has an account joins with it. Everyone here sees who invited them.{agentUrl && <> An AI agent reads <a href={agentUrl} target="_blank" rel="noreferrer">its instructions</a> instead.</>}</>}
      </p>
      <InviteMember id={id} />
    </Sheet>
  )
}

/** Pick a member by name: they get the channel's link as a direct message (and are let in at
 *  once when it is a group by request that I run). */
function InviteMember({ id }: { id: string }) {
  const g = group(id)
  const [q, setQ] = useState('')
  const [done, setDone] = useState<Map<string, string>>(new Map())
  const [error, setError] = useState('')
  const words = q.trim().toLowerCase()
  const list = useMemo(() => {
    if (!words) return []
    return [...profiles().entries()]
      .filter(([pk, p]) => pk !== me() && !!p.name && p.name.toLowerCase().split(/\s+/).some((w) => w.startsWith(words)))
      .slice(0, 5).map(([pubkey]) => pubkey)
  }, [words]) // eslint-disable-line react-hooks/exhaustive-deps
  const SAID: Record<string, string> = { admitted: 'Added · link sent', asked: 'Link sent · they apply', sent: 'Link sent', guest: 'Added as a guest' }
  // what someone is, when not a plain member: a member's agent, or a guest of another channel
  const kind = (pk: string) => { const o = agentOwner(pk); return o ? `${displayName(o)}'s agent · ` : isGuest(pk) ? (isAgent(pk) ? 'Agent, a guest · ' : 'Guest · ') : '' }
  return (
    <div className="invite-member">
      <div className="invite-or"><span>or someone who is here already</span></div>
      <label className="field" style={{ marginBottom: 6 }}><span>Invite by name</span>
        <input className="input" placeholder="Type a name" value={q} autoCapitalize="none" autoCorrect="off" onChange={(e) => { setQ(e.target.value); setError('') }} /></label>
      {list.map((pk) => {
        const here = inChannel(g, pk)
        return (
          <div key={pk} className="invite-row">
            <Avatar pubkey={pk} size={28} />
            <span className="t">{displayName(pk)}<small>{kind(pk)}{here ? (g?.closed || isGuest(pk) ? 'Already in' : 'Can already open it') : 'Not in this channel'}</small></span>
            {done.has(pk) || (here && isGuest(pk))
              ? <span className="hint">{done.get(pk) ?? ''}</span>
              : <Btn className="btn small" onClick={async () => {
                  try { const s = await inviteMember(id, pk); setDone((m) => new Map(m).set(pk, SAID[s])) } catch (e) { setError(e instanceof Error ? e.message : String(e)) }
                }}>{isGuest(pk) ? 'Add as a guest' : here ? 'Send the link' : 'Invite'}</Btn>}
          </div>
        )
      })}
      {words && list.length === 0 && <p className="hint">Nobody here by that name. For someone new, share the link above.</p>}
      {error && <div className="error">{error}</div>}
    </div>
  )
}
