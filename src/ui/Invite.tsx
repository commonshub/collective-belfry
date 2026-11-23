// Invite people to a channel (boards inviting/sheet-external v45-v48, -edit, -guest, Xavier
// 19 Nov). People already here come first, by name. Under "Or invite an external person": the QR
// and the link, then "Invite as" (radio rows, only the roles this person may give: Member when a
// steward or GET /membership may_invite_members, Steward for stewards, Agent for whoever may add
// an agent; one role: no rows), then one line that always says what the current link does, with
// Edit. Edit unfolds People, Works for, Stays and Instead (a guest of this channel only) in place;
// each opens its list. Changing anything makes a new link (the QR greys meanwhile). Someone who
// may not invite members gets the guest link (board -guest): People and Works for only. Agent
// swaps the QR for the line to paste to the agent (inviting/add-agent); its link works once.
// Belfry's POST /channel-invites (guests.py, roles.py): never offer what the server refuses.
import { LinkPair } from './LinkPair'
import { useEffect, useMemo, useRef, useState } from 'react'
import qrcode from 'qrcode-generator'
import { channelIcon, displayName, followsIt, group, profiles, nameOf } from '../core/model'
import { inChannel, inviteMember, isAgent, isGuest, LinkFailed, makeInviteLink, myAgents, type GuestDays, type JoinsAs, type LinkOpts, type Membership, type MyAgents } from '../core/invite'
import { agentOwner } from '../core/access'
import { isBelfryAdmin } from '../core/people'
import { ADMIN, defaultRole, loadRoles, roleById, roles } from '../core/roles'
import { loadMembership, mayInviteMembers } from '../core/requests'
import { me } from '../core/sync'
import { useVersion } from '../core/store'
import { Avatar, BackHead, Btn, Sheet } from './common'
import { useCommunityName } from './community'
import { ChevronRightIcon } from './icons'
import { NAME } from '../config'

const DAYS_WORD: Record<GuestDays, string> = { 1: 'a day', 7: 'a week', 30: 'a month' }
const LINK_WORD: Record<1 | 7 | 30, string> = { 1: '24 hours', 7: 'a week', 30: 'a month' }
const MEMBERSHIP_WORD: Record<Membership, string> = { month: 'One month', year: 'One year', permanent: 'Permanent' }
const others = <T extends string>(all: readonly T[], on: T) => 'or ' + all.filter((x) => x !== on).map((x) => x.toLowerCase()).join(', ')

/** The line under the role rows: what the current link does (board wording). */
export function inviteLine(who: JoinsAs, people: 1 | 50, linkDays: 1 | 7 | 30, label: string, guestOnly = false): string {
  const p = people === 1 ? 'one person' : 'up to 50 people'
  const works = linkDays === 1 ? '' : ` They work for ${LINK_WORD[linkDays]}.`
  if (who.as === 'agent') return `This link works once, for your agent, for ${LINK_WORD[linkDays]}.`
  if (who.as === 'guest') return guestOnly
    ? `This QR code and link let ${p} join ${label} as a guest for ${DAYS_WORD[who.days]}.${works}`
    : `This QR code and link make ${p} ${people === 1 ? 'a guest' : 'guests'} of ${label} for ${DAYS_WORD[who.days]}.${works}`
  return `This QR code and link work for ${p} for ${LINK_WORD[linkDays]}.`
}

type View = 'main' | 'people' | 'works' | 'stays' | 'instead'
const ROLE_LINE: Record<string, string> = { member: 'Joins open channels, invites guests, votes.', admin: 'Runs the belfry: settings, roles, the charter.' }

export function InviteSheet({ id, onClose, preset }: { id: string; onClose: () => void; /** 'member': an invitation to the belfry itself (the members screen) */ preset?: 'member' }) {
  useVersion()
  const g = group(id)
  const community = useCommunityName(NAME)
  const admin = isBelfryAdmin(me())
  const members = mayInviteMembers() // a steward, or a member when Membership settings allow it
  useEffect(() => { void loadMembership() }, [])
  useEffect(() => { if (members) void loadRoles() }, [members])
  const [view, setView] = useState<View>('main')
  const toBelfry = preset === 'member'
  const [outside, setOutside] = useState(toBelfry)
  const [mine, setMine] = useState<MyAgents | null>(null)
  useEffect(() => { myAgents().then(setMine).catch(() => setMine(null)) }, [])
  const room = mine ? mine.limit - mine.agents.length : 0
  // the roles this person may give: the default role (Member); stewards also Steward and the
  // belfry's other roles; Agent for whoever may add one (and has room)
  const roleIds: string[] = !members ? [] : admin ? [defaultRole(), ADMIN, ...roles().filter((r) => r.id !== defaultRole() && r.id !== ADMIN).map((r) => r.id)] : [defaultRole()]
  const choices = members && mine && mine.limit > 0 && room > 0 ? [...roleIds, 'agent'] : roleIds
  const guestOnly = !members
  const [choice, setChoice] = useState(defaultRole())
  const role = choices.includes(choice) ? choice : choices[0] ?? defaultRole()
  const [instead, setInstead] = useState<0 | GuestDays>(0)
  const [membership, setMembership] = useState<Membership>('permanent')
  const [link, setLink] = useState<LinkOpts>({ people: 1, linkDays: 1 })
  const [editing, setEditing] = useState(false)
  const who: JoinsAs = guestOnly ? { as: 'guest', days: 1 }
    : instead && role !== 'agent' ? { as: 'guest', days: instead }
    : role === 'agent' ? { as: 'agent' }
    : { as: 'member', role, membership }
  // a steward's or an agent's link is for one person
  const onlyOne = who.as === 'agent' || (who.as === 'member' && who.role === ADMIN)
  const people = onlyOne ? 1 : link.people
  const [url, setUrl] = useState('')
  const [agentUrl, setAgentUrl] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const asked = useRef(0)
  const label = `${channelIcon(g)}${g?.isPod || g?.isEvent || g?.isProposal ? ' ' : ''}${nameOf(id)}`
  const fetchLink = async () => {
    const n = ++asked.current // a newer choice wins
    setBusy(true); setError(''); setUrl(''); setAgentUrl('')
    try {
      const got = await makeInviteLink(id, who, { ...link, people })
      if (n !== asked.current) return
      if (who.as === 'agent' && !/^https:\/\/[^\s<>"]+$/.test(got.agentUrl)) throw new LinkFailed('failed')
      setUrl(got.url); setAgentUrl(got.agentUrl)
    } catch (e) {
      if (n !== asked.current) return
      setError(e instanceof LinkFailed ? e.message : 'The link could not be made. Try again.')
    } finally { if (n === asked.current) setBusy(false) }
  }
  const key = JSON.stringify([who, link.linkDays, people])
  // no link is made before the part for someone from outside is open
  useEffect(() => { if (outside) void fetchLink() }, [key, outside]) // eslint-disable-line react-hooks/exhaustive-deps
  const pending = !url
  const agentLine = who.as === 'agent' && agentUrl ? `Join ${community} as my agent: read ${agentUrl} and follow it.` : ''
  const qr = useMemo(() => {
    const q = qrcode(0, 'M')
    q.addData(url || `https://chat.example.org/join/${'0'.repeat(32)}`) // the last shape stays, faded, while it loads
    q.make()
    return 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(q.createSvgTag({ cellSize: 6, margin: 2, scalable: true }))
  }, [url])
  const text = who.as === 'agent' ? agentLine : url
  const roleLabel = (r: string) => (r === 'agent' ? 'Agent' : roleById(r)?.label ?? 'Member')
  const roleLine = (r: string) => (r === 'agent' ? 'Your agent, a member while you are one.' : ROLE_LINE[r] ?? roleById(r)?.description ?? '')
  const back = () => setView('main')

  // the lists behind the Edit rows (inviting/sheet-link, inviting/sheet-member)
  const list = <T extends string | number>(title: string, opts: { v: T; l: string; off?: boolean }[], on: T, pick: (v: T) => void) => (
    <Sheet title={title} hideTitle onClose={onClose}>
      <BackHead title={title} onBack={back} />
      <div className="pick-group" role="radiogroup" aria-label={title} style={{ marginTop: 8 }}>
        {opts.map((o) => (
          <button key={String(o.v)} type="button" role="radio" aria-checked={o.v === on} disabled={o.off} className={'pick-row' + (o.v === on ? ' on' : '')} onClick={() => { pick(o.v); back() }}>
            <span className="radio" aria-hidden="true" /><span className="t"><b>{o.l}</b></span>
          </button>
        ))}
      </div>
      {title === 'People' && onlyOne && <p className="hint" style={{ fontSize: 13 }}>{who.as === 'agent' ? 'A link for your agent works once.' : `A link that makes someone ${roleLabel(ADMIN)} works for one person.`}</p>}
    </Sheet>
  )
  if (view === 'people') return list('People', [{ v: 1, l: 'One person' }, { v: 50, l: 'Up to 50 people', off: onlyOne }], people, (v) => setLink({ ...link, people: v as 1 | 50 }))
  if (view === 'works') return list('Works for', [{ v: 1, l: '24 hours' }, { v: 7, l: 'A week' }, { v: 30, l: 'A month' }], link.linkDays, (v) => setLink({ ...link, linkDays: v as 1 | 7 | 30 }))
  if (view === 'stays') return list('Stays', [{ v: 'permanent', l: 'Permanent' }, { v: 'year', l: 'One year' }, { v: 'month', l: 'One month' }], membership, (v) => setMembership(v as Membership))
  if (view === 'instead') return list('Instead', [{ v: 0, l: `No: invite as ${roleLabel(role)}` }, { v: 1, l: `Guest of ${label} only, for a day` }, { v: 7, l: `Guest of ${label} only, for a week` }, { v: 30, l: `Guest of ${label} only, for a month` }], instead, (v) => setInstead(v as 0 | GuestDays))

  const PEOPLE = ['One person', 'Up to 50'] as const
  const WORKS = ['24 hours', 'A week', 'A month'] as const
  const STAYS = ['Permanent', 'One month', 'One year'] as const
  const pv = people === 1 ? PEOPLE[0] : PEOPLE[1]
  const wv = link.linkDays === 1 ? WORKS[0] : link.linkDays === 7 ? WORKS[1] : WORKS[2]
  const sv = MEMBERSHIP_WORD[membership] as (typeof STAYS)[number]
  return (
    <Sheet title={`Invite to ${toBelfry ? community : label}`} onClose={onClose}>
      {!toBelfry && <InviteMember id={id} />}
      {!toBelfry && <button type="button" className="pick-row invite-outside" aria-expanded={outside} onClick={() => setOutside(!outside)}>
        <span className="t"><b>Or invite an external person</b><small>A link or a QR code for someone who is not in {community} yet</small></span><ChevronRightIcon />
      </button>}
      {outside && <>
      {who.as === 'agent'
        ? <div className="agent-prompt invite-agent" aria-label="What to paste to your agent" aria-busy={pending}>{agentLine || (busy ? 'Making the link…' : error || '—')}</div>
        : <>
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
          <div className="drop-url"><code>{pending ? (busy ? 'Making the link…' : '—') : url}</code></div>
        </>}
      {who.as === 'agent' && pending && !busy && error && <Btn className="btn small" onClick={() => fetchLink()}>Retry</Btn>}
      {choices.length > 1 && <>
        <div className="pick-head">Invite as</div>
        <div className="pick-group invite-as" role="radiogroup" aria-label="Invite as">
          {choices.map((r) => (
            <button key={r} type="button" role="radio" aria-checked={r === role} className={'pick-row' + (r === role ? ' on' : '')} onClick={() => { setChoice(r); if (r === 'agent') setInstead(0) }}>
              <span className="radio" aria-hidden="true" /><span className="t"><b>{roleLabel(r)}</b><small>{roleLine(r)}</small></span>
            </button>
          ))}
        </div>
      </>}
      <p className="invite-line">{inviteLine(who, people, link.linkDays, label, guestOnly)} <button type="button" className="link" aria-expanded={editing} onClick={() => setEditing(!editing)}>{editing ? 'Done' : 'Edit'}</button></p>
      {guestOnly && <p className="hint invite-only-stewards">Only {roleLabel(ADMIN).toLowerCase()}s invite new members here.</p>}
      {editing && (
        <div className="pick-group invite-rows invite-edit">
          {who.as !== 'agent' && <button type="button" className="pick-row edit-people" disabled={onlyOne} onClick={() => setView('people')}>
            <span className="k">People</span><span className="t"><b>{pv}</b><small>{onlyOne ? 'one person only' : others(PEOPLE, pv)}</small></span><ChevronRightIcon />
          </button>}
          <button type="button" className="pick-row edit-works" onClick={() => setView('works')}>
            <span className="k">Works for</span><span className="t"><b>{wv}</b><small>{others(WORKS, wv)}</small></span><ChevronRightIcon />
          </button>
          {who.as === 'member' && <button type="button" className="pick-row edit-stays" onClick={() => setView('stays')}>
            <span className="k">Stays</span><span className="t"><b>{sv}</b><small>{others(STAYS, sv)}</small></span><ChevronRightIcon />
          </button>}
          {!guestOnly && role !== 'agent' && <button type="button" className="pick-row edit-instead" onClick={() => setView('instead')}>
            <span className="k">Instead</span><span className="t"><b>{instead ? `Guest of ${label} only, for ${DAYS_WORD[instead]}` : `Guest of ${label} only`}</b><small>{instead ? `or invite as ${roleLabel(role)}` : 'for a day, a week, a month'}</small></span><ChevronRightIcon />
          </button>}
        </div>
      )}
      <LinkPair text={text} disabled={!text} copyLabel={who.as === 'agent' ? 'Copy' : 'Copy link'} share={who.as === 'agent' ? { text } : { title: label, url }} />
      </>}
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
  const kind = (pk: string) => { const o = agentOwner(pk); return o ? (o === me() ? 'Your agent · ' : `${displayName(o)}'s agent · `) : isGuest(pk) ? (isAgent(pk) ? 'Agent, a guest · ' : 'Guest · ') : '' }
  return (
    <div className="invite-member">
      <label className="field" style={{ marginBottom: 6 }}><span>Invite a community member:</span>
        <input className="input" placeholder="Invite a community member" value={q} autoCapitalize="none" autoCorrect="off" onChange={(e) => { setQ(e.target.value); setError('') }} /></label>
      {list.map((pk) => {
        const here = inChannel(g, pk)
        return (
          <div key={pk} className="invite-row">
            <Avatar pubkey={pk} size={28} />
            <span className="t">{displayName(pk)}<small>{kind(pk)}{here ? (g && followsIt(g) && !isGuest(pk) ? 'Already following' : g?.closed || isGuest(pk) ? 'Already in' : 'Already a member') : 'Not in this channel'}</small></span>
            {done.has(pk) || (here && isGuest(pk))
              ? <span className="hint">{done.get(pk) ?? ''}</span>
              : <Btn className="btn small" onClick={async () => {
                  try { const s = await inviteMember(id, pk); setDone((m) => new Map(m).set(pk, SAID[s])) } catch (e) { setError(e instanceof Error ? e.message : String(e)) }
                }}>{isGuest(pk) ? 'Add as a guest' : here ? 'Send the link' : 'Invite'}</Btn>}
          </div>
        )
      })}
      {words && list.length === 0 && <p className="hint">Nobody here by that name. For someone new, invite an external person below.</p>}
      {error && <div className="error">{error}</div>}
    </div>
  )
}
