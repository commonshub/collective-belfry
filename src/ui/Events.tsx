// An event's own screen (#/events/<link>): the channel header's pattern (one line, pulled down
// for the rest), the event's chat under it, RSVP or apply in one tap, the guest list for hosts.
// And the screen someone with no account sees when they open an event's URL in the chat.
import { useEffect, useMemo, useState, type ReactNode } from 'react'
import qrcode from 'qrcode-generator'
import { BELFRY, NAME, type Backend } from '../config'
import { calendar, displayName, eventByCoord, eventUrl, group, profiles, type CalEvent, type Rsvp } from '../core/model'
import { useVersion } from '../core/store'
import { me } from '../core/sync'
import { addCoHost, applyFor, coHostLink, decide, eventByLink, eventLinkOf, goingList, isHost, myState, people, placesLeft, rsvp, saveTicket, deleteTicket, setStatus, stillGoing, takeTicket, tickets, withdraw, fetchEvent, setPendingApplication, type ApiEvent, type People, type Ticket, type TicketForm } from '../core/events'
import { deleteEvent } from '../core/actions'
import { isGuest } from '../core/invite'
import { finalizeEvent, generateSecretKey } from 'nostr-tools'
import { sha256 } from '@noble/hashes/sha256'
import { bytesToHex, hexToBytes } from '@noble/hashes/utils'
import { makeSigner, type Session } from '../core/auth'
import { kvDel } from '../core/db'
import { Avatar, back, fmtDay, go, Sheet, Btn, Form } from './common'
import { ChannelHeader } from './ChannelHeader'
import { Conversation, useCommunityName } from './Chat'
import { FollowButton } from './Follow'
import { Markdown } from './markdown'
import { openProfile } from './Profile'
import { CalendarIcon, CloseIcon, CopyIcon, DocIcon, GlobeIcon, LinkIcon, MoreIcon, PencilIcon, PeopleIcon, TrashIcon, ChevronDownIcon, ChevronUpIcon } from './icons'
import { EventFormSheet, DateChip, when, LABEL } from './Calendar'
import { Logo } from './Login'

const day = (d: Date) => (d.toDateString() === new Date().toDateString() ? 'Today' : fmtDay(d))
const at = (e: CalEvent) => (e.allDay ? '' : ' ' + when(e).split(' – ')[0])
const ended = (e: CalEvent) => +(e.end ?? e.start) < Date.now()
const STATE_LINE: Record<string, string> = { ticket: 'You’re going', maybe: 'Maybe', cant: 'Can’t go', applied: 'Application sent: the hosts will answer', waitlist: 'On the waitlist: the hosts will tell you', declined: 'The hosts could not give you a place' }

/** The event by its URL's last part, or (older ones) by coordinate. */
export function EventScreen({ link, coord, activeThread }: { link?: string; coord?: string; activeThread?: string }) {
  useVersion()
  const found = coord ? { e: eventByCoord(coord) } : eventByLink(link ?? '')
  const e = found.e
  if (!e) {
    const err = 'error' in found ? found.error : undefined
    return (
      <div className="screen">
        <div className="topbar"><button className="icon-btn" aria-label="Back" onClick={() => back('events')}>‹</button><h1>Event</h1></div>
        <div className="empty">{err === 'members_only' ? 'This event is for members.' : err ? 'There is no such event here.' : 'Opening the event…'}</div>
      </div>
    )
  }
  return <EventView e={e} api={'api' in found ? found.api : undefined} activeThread={activeThread} />
}

function EventView({ e, api, activeThread }: { e: CalEvent; api?: ApiEvent; activeThread?: string }) {
  const community = useCommunityName(NAME)
  const [sheet, setSheet] = useState<'' | 'menu' | 'edit' | 'tickets' | 'guests' | 'qr' | 'cohost' | 'cancel' | 'take' | 'delete'>('')
  const [ticketFor, setTicketFor] = useState<Ticket | null>(null)
  const p = people(e)
  const tks = tickets(e, api)
  const st = myState(e, p, api)
  const host = isHost(e)
  const g = group(e.groupId)
  const inside = !!g && (!g.closed || g.members.has(me()) || g.admins.has(me()) || p.holders.has(me()))
  const readOnly = !!g && g.closed && !g.members.has(me()) && !g.admins.has(me()) && !p.holders.has(me()) && !host
  const plain = tks.length === 1 && tks[0].implicit
  const open = e.status !== 'cancelled' && !ended(e)
  const asking = host ? p.applications.length : 0
  const pick = (t: Ticket) => { setTicketFor(t); setSheet('take') }
  const take = async () => {
    if (plain && !e.remote && !isGuest(me())) return rsvp(e, 'accepted')
    if (tks.length === 1) return pick(tks[0])
    setSheet('take'); setTicketFor(null)
  }
  const line = ({ open: isOpen, onToggle, pull }: { open: boolean; onToggle: () => void; pull: object }) => {
    let text: ReactNode = <><b>{day(e.start)}{at(e)}</b>{e.location ? ' · ' + e.location : e.links.length ? ' · online' : ''}</>
    let pill: ReactNode = null
    if (e.status === 'cancelled') text = <><b>Cancelled</b>{e.statusReason ? ' · ' + e.statusReason : ''}</>
    else if (stillGoing(e)) {
      text = <span title="The date or the place changed"><b>Changed</b> · still going?</span>
      pill = <span className="line-pills"><Btn className="line-pill ask" onClick={() => rsvp(e, 'accepted', p.holders.get(me()))}>Yes</Btn><Btn className="line-pill" onClick={() => rsvp(e, 'declined', p.holders.get(me()))}>No</Btn></span>
    } else if (asking) {
      text = <><b>{asking} {asking === 1 ? 'person asks' : 'people ask'} to come</b> · {goingList(e, p).length} going</>
      pill = <button className="line-pill ask" onClick={() => setSheet('guests')}>Review</button>
    } else if (STATE_LINE[st]) {
      text = <><b>{STATE_LINE[st]}</b> · {day(e.start)}{at(e)}</>
    } else if (open && st === 'none') pill = <Btn className="line-pill ask" onClick={take}>RSVP</Btn>
    return (
      <div className={'chan-line' + (isOpen ? ' open' : '')} {...pull}>
        <button className="line-main" onClick={onToggle} aria-expanded={isOpen}>
          <span className="line-ico"><CalendarIcon /></span>
          <span className="line-text">{text}</span>
        </button>
        {pill}
        <button className="line-more" onClick={onToggle} aria-label={isOpen ? 'Close the event’s details' : 'Open the event’s details'}>
          <span className="line-count" title="going"><PeopleIcon />{goingList(e, p).length}</span>
          <span className="line-chev">{isOpen ? <ChevronUpIcon /> : <ChevronDownIcon />}</span>
        </button>
      </div>
    )
  }
  const peek = ({ more }: { close: () => void; more: (t: 'about' | 'tasks' | 'members' | 'files') => void }) => (
    <div className="pull ev-pull">
      {e.cover && <img className="ev-cover" src={e.cover} alt="" />}
      <section>
        <p className="ev-when"><b>{fmtDay(e.start)}</b> · {when(e)}{e.end && e.end.toDateString() !== e.start.toDateString() ? ` → ${fmtDay(e.end)}` : ''}</p>
        {e.location && <p className="ev-where">📍 {e.location}</p>}
        {e.links.map((l) => <a key={l} className="btn small" style={{ display: 'inline-flex', marginRight: 8, textDecoration: 'none' }} href={l} target="_blank" rel="noreferrer">🎥 Join online</a>)}
        {!e.links.length && api?.online && <p className="hint">Online: the link shows once you have a place.</p>}
      </section>
      {open && <section><h3>Your RSVP</h3><RsvpRow e={e} p={p} tks={tks} st={st} onPick={pick} onWithdraw={() => withdraw(e)} /></section>}
      <section>
        <h3>Hosts</h3>
        <div className="ev-hosts">{[...e.hosts].map((h) => <button key={h} className="ev-host" onClick={() => openProfile(h)}><Avatar pubkey={h} size={24} /><span>{api?.hosts.find((x) => x.pubkey === h)?.name || displayName(h)}</span></button>)}</div>
      </section>
      {!plain && (
        <section>
          <h3>Tickets</h3>
          {tks.map((t) => <TicketRow key={t.d} t={t} p={p} onPick={open && st === 'none' ? () => pick(t) : undefined} />)}
        </section>
      )}
      {e.description && <section><Markdown className="ev-about" text={e.description} /></section>}
      <section className="ev-url">
        <code>{eventUrl(e)}</code>
        <CopyButton text={eventUrl(e)} label="Copy URL" />
        <button className="btn small ghost" onClick={() => setSheet('qr')}>QR code</button>
      </section>
      {host && <button className="pull-row" onClick={() => setSheet('guests')}><PeopleIcon /><span className="t">Guest list<small>{goingList(e, p).length} going{asking ? ` · ${asking} asking` : ''}</small></span></button>}
      {g && <button className="pull-row pull-people" onClick={() => more('members')}><PeopleIcon /><span className="t">The event’s chat<small>Members, files, to-dos</small></span><ChevronDownIcon /></button>}
    </div>
  )
  return (
    <div className="screen">
      <ChannelHeader id={e.groupId} onTopic={() => {}}
        title={<>{e.title}<small>Event · {community}</small></>}
        left={<button className="icon-btn" aria-label="Back" onClick={() => back('events')}>‹</button>}
        right={<>{g && <FollowButton channel={e.groupId} />}<button className="icon-btn" aria-label="More" onClick={() => setSheet('menu')}><MoreIcon /></button></>}
        line={line} peek={peek} />
      {inside && g ? <Conversation id={e.groupId} readOnly={readOnly || e.remote === true && !p.holders.has(me())} activeThread={activeThread} />
        : <div className="scroll"><div className="empty ev-closed">
            {st === 'applied' || st === 'waitlist' ? 'The event’s chat opens once the hosts give you a place.' : e.status === 'cancelled' ? 'This event is cancelled.' : 'The event’s chat opens once you have a place.'}
            {open && st === 'none' && <><div style={{ height: 12 }} /><Btn className="btn" onClick={take}>RSVP</Btn></>}
          </div></div>}
      {open && st === 'none' && inside && <div className="join-bar"><span>{plain ? 'Coming?' : 'Get a place to join in.'}</span><Btn className="btn small" onClick={take}>RSVP</Btn></div>}
      {sheet === 'menu' && <EventMenu e={e} host={host} onClose={() => setSheet('')} open={(s) => setSheet(s)} />}
      {sheet === 'edit' && <EventFormSheet edit={e} onClose={() => setSheet('')} onSaved={(l) => { if (l !== eventLinkOf(e)) location.replace('#/events/' + encodeURIComponent(l)) }} />}
      {sheet === 'tickets' && <TicketsSheet e={e} onClose={() => setSheet('')} />}
      {sheet === 'guests' && <GuestList e={e} onClose={() => setSheet('')} />}
      {sheet === 'qr' && <QrSheet url={eventUrl(e)} title="URL of the event" onClose={() => setSheet('')} />}
      {sheet === 'cohost' && <CoHostSheet e={e} onClose={() => setSheet('')} />}
      {sheet === 'cancel' && <CancelSheet e={e} onClose={() => setSheet('')} />}
      {sheet === 'delete' && <DeleteSheet e={e} onClose={() => setSheet('')} />}
      {sheet === 'take' && <TakeSheet e={e} tks={tks} p={p} t={ticketFor} onPick={setTicketFor} onClose={() => { setSheet(''); setTicketFor(null) }} />}
    </div>
  )
}

/** Going / Maybe / Can't go (the plain RSVP), or the ticket you hold, or your application. */
function RsvpRow({ e, p, tks, st, onPick, onWithdraw }: { e: CalEvent; p: People; tks: Ticket[]; st: string; onPick: (t: Ticket) => void; onWithdraw: () => unknown }) {
  const held = p.holders.get(me())
  const plain = tks.length === 1 && tks[0].implicit && !e.remote && !isGuest(me())
  if (st === 'applied' || st === 'waitlist')
    return <div className="row" style={{ gap: 8, justifyContent: 'space-between' }}><span>{STATE_LINE[st]}</span><Btn className="btn small ghost" onClick={onWithdraw}>Withdraw</Btn></div>
  if (st === 'declined') return <p className="hint">{STATE_LINE.declined}.</p>
  if (!plain && !held && st === 'none')
    return <div className="rsvp">{tks.length === 1 ? <button className="on" onClick={() => onPick(tks[0])}>{tks[0].closed ? 'Apply' : 'RSVP'}</button> : <button className="on" onClick={() => onPick(tks[0])}>Pick a ticket</button>}</div>
  const mine = e.rsvps.get(me()) ?? (held ? 'accepted' : undefined)
  return (
    <div className="rsvp">
      {(['accepted', 'tentative', 'declined'] as Rsvp[]).map((s) => (
        <Btn key={s} className={mine === s ? 'on' : ''} aria-pressed={mine === s} onClick={() => rsvp(e, s, held)}>{LABEL[s]}</Btn>
      ))}
    </div>
  )
}

function TicketRow({ t, p, onPick }: { t: Ticket; p: People; onPick?: () => void }) {
  const left = placesLeft(t, p)
  const full = left === 0
  return (
    <div className="ticket-row">
      <div className="t"><b>{t.title}</b>
        <small>{t.closed ? 'By application' : 'Free'}{t.members ? ' · Members only' : ''}{left !== undefined ? ` · ${left} left` : ''}</small>
        {t.summary && <small>{t.summary}</small>}
      </div>
      {onPick && <button className="btn small" onClick={onPick}>{full ? 'Waitlist' : t.closed ? 'Apply' : 'RSVP'}</button>}
    </div>
  )
}

/** Take a ticket in one tap, or answer its question, or join its waitlist. */
function TakeSheet({ e, tks, p, t, onPick, onClose }: { e: CalEvent; tks: Ticket[]; p: People; t: Ticket | null; onPick: (t: Ticket) => void; onClose: () => void }) {
  const [answer, setAnswer] = useState('')
  const [error, setError] = useState('')
  const member = !isGuest(me()) && !e.remote
  if (!t)
    return (
      <Sheet title="Pick a ticket" onClose={onClose}>
        {tks.map((x) => <TicketRow key={x.d} t={x} p={p} onPick={() => onPick(x)} />)}
      </Sheet>
    )
  const left = placesLeft(t, p)
  const waitlist = left === 0
  const needs = t.closed || waitlist
  if (t.members && !member)
    return <Sheet title={t.title} onClose={onClose}><p>This ticket is for members of {NAME}.</p><button className="btn" onClick={onClose}>OK</button></Sheet>
  return (
    <Sheet title={waitlist ? `Waitlist: ${t.title}` : t.closed ? `Apply: ${t.title}` : t.title} onClose={onClose}>
      <Form onSubmit={async (x) => {
        x.preventDefault(); setError('')
        try {
          if (needs) await applyFor(e, t, answer.trim(), waitlist)
          else await takeTicket(e, t)
          onClose()
        } catch (err) { setError(err instanceof Error ? err.message : String(err)) }
      }}>
        {t.closed && !waitlist && <p className="apply-q">{t.prompt || 'Why would you like to come?'}</p>}
        {waitlist && <p className="hint">It is full. Leave a word for the hosts: they may make an exception.</p>}
        {needs && <textarea className="input" rows={4} autoFocus value={answer} maxLength={2000} placeholder={waitlist ? 'Optional' : 'Your answer'} onChange={(x) => setAnswer(x.target.value)} />}
        {!needs && <p>{t.summary || 'A free place.'}{left !== undefined ? ` ${left} left.` : ''}</p>}
        {error && <p className="error" role="alert">{error}</p>}
        <div style={{ height: 12 }} />
        <button className="btn" disabled={t.closed && !waitlist && !answer.trim()}>{waitlist ? 'Join the waitlist' : t.closed ? 'Send my application' : 'RSVP'}</button>
      </Form>
    </Sheet>
  )
}

function EventMenu({ e, host, onClose, open }: { e: CalEvent; host: boolean; onClose: () => void; open: (s: 'edit' | 'tickets' | 'guests' | 'qr' | 'cohost' | 'cancel' | 'delete') => void }) {
  const [copied, setCopied] = useState(false)
  const Item = ({ icon, label, onClick, danger = false }: { icon: ReactNode; label: string; onClick: () => void; danger?: boolean }) => (
    <button className={'act' + (danger ? ' danger' : '')} onClick={onClick}>{icon}<span>{label}</span></button>
  )
  return (
    <Sheet title="Event" hideTitle onClose={onClose}>
      <div className="act-group">
        <Item icon={<CopyIcon />} label={copied ? 'Copied' : 'Copy URL'} onClick={async () => { await copyText(eventUrl(e)); setCopied(true); setTimeout(onClose, 600) }} />
        <Item icon={<GlobeIcon />} label="QR code" onClick={() => open('qr')} />
        {e.isNew && e.visibility !== 'members' && <Item icon={<LinkIcon />} label="Open the public page" onClick={() => { window.open(eventUrl(e), '_blank', 'noopener'); onClose() }} />}
      </div>
      {host && e.isNew && (
        <div className="act-group">
          <Item icon={<PencilIcon />} label="Edit the event" onClick={() => open('edit')} />
          <Item icon={<DocIcon />} label="Tickets" onClick={() => open('tickets')} />
          <Item icon={<PeopleIcon />} label="Guest list" onClick={() => open('guests')} />
          <Item icon={<LinkIcon />} label="Co-host invitation link" onClick={() => open('cohost')} />
        </div>
      )}
      {host && e.isNew && (
        <div className="act-group">
          {e.status === 'cancelled'
            ? <Item icon={<CalendarIcon />} label="Reopen the event" onClick={() => { onClose(); void setStatus(e, 'scheduled') }} />
            : <Item danger icon={<CloseIcon />} label="Cancel the event" onClick={() => open('cancel')} />}
          {e.creator === me() && <Item danger icon={<TrashIcon />} label="Delete the event" onClick={() => open('delete')} />}
        </div>
      )}
    </Sheet>
  )
}

async function copyText(text: string) {
  try { await navigator.clipboard.writeText(text) } catch {
    const t = document.createElement('textarea'); t.value = text; document.body.appendChild(t); t.select(); document.execCommand('copy'); t.remove()
  }
}
function CopyButton({ text, label }: { text: string; label: string }) {
  const [done, setDone] = useState(false)
  return <button className="btn small ghost" onClick={async () => { await copyText(text); setDone(true); setTimeout(() => setDone(false), 1500) }}>{done ? 'Copied' : label}</button>
}

/** A QR code is a button: it opens full width on white with the URL under it. */
export function QrSheet({ url, title, onClose }: { url: string; title: string; onClose: () => void }) {
  const svg = useMemo(() => { const q = qrcode(0, 'M'); q.addData(url); q.make(); return 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(q.createSvgTag({ cellSize: 8, margin: 2, scalable: true })) }, [url])
  return (
    <Sheet title={title} onClose={onClose}>
      <div className="qr-full"><img src={svg} alt={`QR code for ${url}`} /><code>{url}</code></div>
      <CopyButton text={url} label="Copy" />
    </Sheet>
  )
}

function CoHostSheet({ e, onClose }: { e: CalEvent; onClose: () => void }) {
  const [url, setUrl] = useState('')
  const [error, setError] = useState('')
  const [qr, setQr] = useState(false)
  useEffect(() => { coHostLink(e).then(setUrl, (x) => setError(x instanceof Error ? x.message : String(x))) }, [e.d])
  if (qr && url) return <QrSheet url={url} title="Co-host invitation link" onClose={onClose} />
  return (
    <Sheet title="Co-host invitation link" onClose={onClose}>
      <p className="hint">For one person, for a day. Whoever opens it becomes a host: they edit the event and answer applications. Someone new saves an e-mail first.</p>
      {error ? <p className="error">{error}</p> : url ? <><code className="ev-link">{url}</code><div className="row" style={{ gap: 8, marginTop: 10 }}><CopyButton text={url} label="Copy link" /><button className="btn small ghost" onClick={() => setQr(true)}>QR code</button></div></> : <p role="status">Making the link…</p>}
    </Sheet>
  )
}

function CancelSheet({ e, onClose }: { e: CalEvent; onClose: () => void }) {
  const [reason, setReason] = useState('')
  return (
    <Sheet title="Cancel the event?" onClose={onClose}>
      <Form onSubmit={async (x) => { x.preventDefault(); await setStatus(e, 'cancelled', reason.trim()); onClose() }}>
        <p className="hint">Everyone with a place or an application is told by e-mail when they gave one. You can reopen it later.</p>
        <label className="field"><span>A line for the guests (optional)</span><textarea className="input" rows={3} value={reason} maxLength={500} onChange={(x) => setReason(x.target.value)} /></label>
        <div className="row-btns"><button type="button" className="btn ghost" onClick={onClose}>Keep it</button><button className="btn danger">Cancel the event</button></div>
      </Form>
    </Sheet>
  )
}

function DeleteSheet({ e, onClose }: { e: CalEvent; onClose: () => void }) {
  return (
    <Sheet title="Delete this event?" onClose={onClose}>
      <p className="hint">It disappears for everyone; its chat stays. To tell the guests, cancel it instead.</p>
      <div className="row-btns"><button type="button" className="btn ghost" onClick={onClose}>Keep it</button><Btn className="btn danger" onClick={async () => { await deleteEvent(e); onClose(); go('events') }}>Delete</Btn></div>
    </Sheet>
  )
}

/** Ticket types: no price in v1. None = the plain RSVP. */
function TicketsSheet({ e, onClose }: { e: CalEvent; onClose: () => void }) {
  useVersion()
  const list = tickets(e).filter((t) => !t.implicit)
  const [editing, setEditing] = useState<TicketForm | null>(null)
  if (editing) return (
    <Sheet title={editing.id ? 'Edit the ticket' : 'New ticket'} onClose={onClose}>
      <Form onSubmit={async (x) => { x.preventDefault(); await saveTicket(e, editing); setEditing(null) }}>
        <label className="field"><span>Name</span><input className="input" autoFocus value={editing.title} maxLength={120} placeholder="e.g. Standard, Speaker, Volunteer" onChange={(x) => setEditing({ ...editing, title: x.target.value })} /></label>
        <label className="field"><span>Summary</span><input className="input" value={editing.summary} maxLength={300} onChange={(x) => setEditing({ ...editing, summary: x.target.value })} /></label>
        <label className="field"><span>How many places</span><input className="input" inputMode="numeric" placeholder="No limit" value={editing.quantity} onChange={(x) => setEditing({ ...editing, quantity: x.target.value.replace(/\D/g, '') })} /></label>
        <div className="choice" style={{ marginTop: 4 }}>
          <button type="button" className={!editing.closed ? 'on' : ''} onClick={() => setEditing({ ...editing, closed: false })}><b>Open</b><small>One tap and you’re in</small></button>
          <button type="button" className={editing.closed ? 'on' : ''} onClick={() => setEditing({ ...editing, closed: true })}><b>Needs an application</b><small>Hosts approve</small></button>
        </div>
        {editing.closed && <label className="field"><span>The question applicants answer</span><textarea className="input" rows={2} value={editing.prompt} placeholder="Why would you like to come?" onChange={(x) => setEditing({ ...editing, prompt: x.target.value })} /></label>}
        <label className="toggle"><input type="checkbox" checked={editing.members} onChange={(x) => setEditing({ ...editing, members: x.target.checked })} /> Members only (shown to everyone, taken by members)</label>
        <button className="btn" disabled={!editing.title.trim()}>Save the ticket</button>
      </Form>
    </Sheet>
  )
  return (
    <Sheet title="Tickets" onClose={onClose}>
      {list.length === 0 && <p className="hint">No tickets: anyone who may see the event RSVPs in one tap, without a limit. Add a ticket for a limit, an application or a members-only place.</p>}
      {list.map((t) => (
        <div key={t.d} className="ticket-row">
          <div className="t"><b>{t.title}</b><small>{t.closed ? 'By application' : 'Open'}{t.members ? ' · Members only' : ''}{t.quantity !== undefined ? ` · ${t.quantity} places` : ''}</small></div>
          <button className="icon-btn" aria-label={`Edit ${t.title}`} onClick={() => setEditing({ id: t.id, title: t.title, summary: t.summary, quantity: t.quantity === undefined ? '' : String(t.quantity), closed: t.closed, prompt: t.prompt, members: t.members, position: t.position })}><PencilIcon /></button>
          <Btn className="icon-btn" aria-label={`Delete ${t.title}`} onClick={() => deleteTicket(e, t)}><TrashIcon /></Btn>
        </div>
      ))}
      <div style={{ height: 8 }} />
      <button className="btn ghost" onClick={() => setEditing({ title: '', summary: '', quantity: '', closed: false, prompt: '', members: false, position: list.length + 1 })}>＋ Add a ticket</button>
    </Sheet>
  )
}

type GTab = 'going' | 'asking' | 'waitlist' | 'other'
/** Hosts: who is going, who asks, the waitlist, the rest. Approve, decline, make a co-host. */
function GuestList({ e, onClose }: { e: CalEvent; onClose: () => void }) {
  useVersion()
  const p = people(e)
  const [tab, setTab] = useState<GTab>(p.applications.some((a) => !a.waitlist) ? 'asking' : 'going')
  const going = goingList(e, p)
  const asking = p.applications.filter((a) => !a.waitlist)
  const waiting = p.applications.filter((a) => a.waitlist)
  const other = [...e.rsvps].filter(([k, s]) => s !== 'accepted' && !going.includes(k)).map(([k]) => k)
  const tks = tickets(e)
  const titleOf = (d?: string) => tks.find((t) => t.d === d)?.title ?? ''
  const name = (k: string) => profiles().get(k)?.name ?? displayName(k)
  const Person = ({ k, sub, children }: { k: string; sub?: string; children?: ReactNode }) => (
    <div className="member guest-row">
      <button className="face" onClick={() => openProfile(k)} aria-label={`${name(k)}: profile`}><Avatar pubkey={k} /></button>
      <div style={{ flex: 1, minWidth: 0 }}><div>{name(k)}{e.hosts.has(k) && <span className="badge">Host</span>}{isGuest(k) && <span className="badge">Guest</span>}</div>{sub && <small className="hint">{sub}</small>}</div>
      {children}
    </div>
  )
  const Decide = ({ a }: { a: (typeof p.applications)[number] }) => (
    <span className="row" style={{ gap: 6 }}>
      <Btn className="btn small" onClick={() => decide(e, a.pubkey, 'approve', a.ticket, a.ev)}>Approve</Btn>
      <Btn className="btn small ghost" onClick={() => decide(e, a.pubkey, 'decline')}>Decline</Btn>
    </span>
  )
  return (
    <Sheet title="Guest list" onClose={onClose}>
      <div className="seg">
        {([['going', `Going ${going.length}`], ['asking', `Asking ${asking.length}`], ['waitlist', `Waitlist ${waiting.length}`], ['other', 'Others']] as [GTab, string][]).map(([k, l]) =>
          <button key={k} className={tab === k ? 'on' : ''} onClick={() => setTab(k)}>{l}</button>)}
      </div>
      <div className="guest-list">
        {tab === 'going' && (going.length ? going.map((k) => (
          <Person key={k} k={k} sub={titleOf(p.holders.get(k))}>
            {!e.hosts.has(k) && !isGuest(k) && !e.remote && <Btn className="btn small ghost" onClick={() => addCoHost(e, k)}>Make co-host</Btn>}
          </Person>)) : <p className="hint">Nobody yet.</p>)}
        {tab === 'asking' && (asking.length ? asking.map((a) => <Person key={a.pubkey} k={a.pubkey} sub={`${titleOf(a.ticket)}${a.answer ? ' · “' + a.answer.slice(0, 200) + '”' : ''}`}><Decide a={a} /></Person>) : <p className="hint">No applications waiting.</p>)}
        {tab === 'waitlist' && (waiting.length ? waiting.map((a) => <Person key={a.pubkey} k={a.pubkey} sub={`${titleOf(a.ticket)}${a.answer ? ' · “' + a.answer.slice(0, 200) + '”' : ''}`}><Decide a={a} /></Person>) : <p className="hint">Nobody on the waitlist.</p>)}
        {tab === 'other' && (<>
          {other.map((k) => <Person key={k} k={k} sub={LABEL[e.rsvps.get(k)!]} />)}
          {[...p.declined].map((k) => <Person key={k} k={k} sub="Declined" />)}
          {!other.length && !p.declined.size && <p className="hint">Nobody.</p>}
        </>)}
      </div>
    </Sheet>
  )
}

/** In the drawer: the events I host or have a place at, upcoming and up to 30 days after. */
export function yourEvents(): CalEvent[] {
  const since = Date.now() - 30 * 86400e3
  return calendar().filter((e) => e.isNew && +(e.end ?? e.start) >= since && (() => { const s = myState(e); return s === 'host' || s === 'ticket' || s === 'maybe' || s === 'applied' || s === 'waitlist' || e.hosts.has(me()) })())
}

// ---- someone with no account opens an event's URL in the chat -----------------------------

const VISIT_KEY = 'belfry-event-key'
function keyFor(link: string): Uint8Array {
  try { const k = JSON.parse(localStorage.getItem(VISIT_KEY) ?? 'null'); if (k?.link === link && /^[0-9a-f]{64}$/.test(k.sk)) return hexToBytes(k.sk) } catch { /* none */ }
  const sk = generateSecretKey()
  try { localStorage.setItem(VISIT_KEY, JSON.stringify({ link, sk: bytesToHex(sk) })) } catch { /* private mode */ }
  return sk
}
async function signedWith(sign: (t: { kind: number; content: string; created_at: number; tags: string[][] }) => Promise<unknown>, url: string, body: unknown) {
  const text = JSON.stringify(body)
  const auth = await sign({ kind: 27235, content: '', created_at: Math.floor(Date.now() / 1000), tags: [['u', url], ['method', 'POST'], ['payload', bytesToHex(sha256(new TextEncoder().encode(text)))]] })
  const r = await fetch(url, { method: 'POST', headers: { Authorization: 'Nostr ' + btoa(JSON.stringify(auth)), 'Content-Type': 'application/json' }, body: text })
  const j = await r.json().catch(() => ({}))
  if (!r.ok) throw new Error(j.message || 'That did not work just now. Try again.')
  return j
}

const DEMO: ApiEvent = {
  d: '2026-demo01', coord: '31923:' + 'a'.repeat(64) + ':2026-demo01', kind: 31923, group: 'ev-2026-demo01', link: 'garden-day', url: '', title: 'Garden day',
  description: 'We plant **beans** and share lunch.', all_day: false, start: new Date(Date.now() + 5 * 86400e3).toISOString(), end: new Date(Date.now() + 5 * 86400e3 + 3 * 3600e3).toISOString(),
  tz: 'Europe/Brussels', location: 'The community garden', online: false, links: [], cover: null, visibility: 'public', status: 'scheduled', status_reason: '',
  hosts: [{ pubkey: 'a'.repeat(64), name: 'Ana' }], going: 12, community: NAME, me: null,
  tickets: [
    { d: '2026-demo01:std', id: 'std', title: 'Standard', summary: 'A place for the day', quantity: 30, left: 18, closed: false, prompt: '', members: false, position: 1, implicit: false },
    { d: '2026-demo01:sp', id: 'sp', title: 'Workshop host', summary: '', quantity: null, left: null, closed: true, prompt: 'What would you show?', members: false, position: 2, implicit: false },
  ],
}

export function EventJoinScreen({ link, backend, onJoined, onSignIn, onDemo }: { link: string; backend: Backend; onJoined: (s: Session) => void; onSignIn: () => void; onDemo: (name: string) => void }) {
  const [ev, setEv] = useState<ApiEvent | null>(null)
  const [error, setError] = useState('')
  const [t, setT] = useState<ApiEvent['tickets'][number] | null>(null)
  const [name, setName] = useState('')
  const [email, setEmail] = useState('')
  const [answer, setAnswer] = useState('')
  const [sent, setSent] = useState<Session | null>(null)
  useEffect(() => {
    if (backend === 'demo') return setEv(DEMO)
    fetchEvent(link, false).then(setEv, (x) => setError(x instanceof Error && x.message === 'members_only' ? 'members' : 'missing'))
  }, [link])
  if (sent)
    return (
      <div className="screen login">
        <Logo />
        <h2>Application sent</h2>
        <p>The hosts of {ev?.title} will answer. You’ll get an e-mail at {email}.</p>
        <button className="btn" onClick={() => onJoined(sent)}>OK</button>
      </div>
    )
  if (!ev)
    return (
      <div className="screen login">
        <Logo />
        {error ? <><h2>{error === 'members' ? 'This event is for members' : 'There is no such event here'}</h2><button className="btn" onClick={onSignIn}>I have an account: sign in</button></> : <p role="status">Opening the event…</p>}
      </div>
    )
  const start = ev.start ? new Date(ev.start) : undefined
  const open = ev.status !== 'cancelled' && !(ev.end && new Date(ev.end) < new Date())
  const left = (x: ApiEvent['tickets'][number]) => x.left
  const needs = !!t && (t.closed || left(t) === 0)
  const submit = async (x: React.FormEvent) => {
    x.preventDefault()
    if (!t) return
    const n = name.trim().replace(/\s+/g, ' ')
    if (backend === 'demo') return onDemo(n || 'Guest')
    setError('')
    const sk = keyFor(link)
    const local = (tpl: Parameters<typeof finalizeEvent>[0]) => Promise.resolve(finalizeEvent(tpl, sk))
    try {
      const url = `${BELFRY.api}/events/${ev.d}/${needs ? 'apply' : 'join'}`
      const j = (await signedWith(local, url, { ticket: t.d, name: n, ...(email.trim() ? { email: email.trim() } : {}) })) as { bunker_url?: string; needs_request?: boolean; waitlist?: boolean }
      if (!j.bunker_url) throw new Error('Joining did not work just now. Try again.')
      await kvDel('pubkey')
      const session: Session = { backend: 'belfry', bunkerUrl: j.bunker_url, clientSecret: bytesToHex(sk), membership: { status: 'unknown' } }
      try { localStorage.removeItem(VISIT_KEY) } catch { /* private mode */ }
      history.replaceState(null, '', `${location.pathname}${location.search}#/events/${encodeURIComponent(link)}`)
      if (!needs) return onJoined(session)
      // the identity is made: it signs its own join request, which the belfry hands in
      const signer = await makeSigner(session)
      const waitlist = left(t) === 0
      const request = await signer.sign({ kind: 9021, content: answer.trim().slice(0, 2000), created_at: Math.floor(Date.now() / 1000), tags: [['h', ev.group], ['ticket', t.d], ...(waitlist ? [['waitlist']] : [])] })
      await signedWith((tpl) => signer.sign(tpl), url, { ticket: t.d, request, email: email.trim() })
      setPendingApplication({ link, title: ev.title })
      setSent(session)
    } catch (err) {
      const m = err instanceof Error ? err.message : String(err)
      setError(/fetch|network|load/i.test(m) ? 'No connection to the belfry. Try again.' : m)
    }
  }
  return (
    <div className="screen login ev-join">
      <Logo />
      {ev.cover && <img className="ev-cover" src={ev.cover} alt="" />}
      <h2>{ev.title}</h2>
      {start && <p className="ev-when"><b>{fmtDay(start)}</b>{ev.all_day ? '' : ' · ' + start.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}{ev.location ? ' · ' + ev.location : ev.online ? ' · online' : ''}</p>}
      <p className="hint">Hosted by {ev.hosts.map((h) => h.name || 'a member').join(', ')} · {ev.going} going</p>
      {ev.status === 'cancelled' && <p className="error">This event is cancelled.</p>}
      {open && !t && (
        <div className="ev-tickets">
          {ev.tickets.map((x) => (
            <button key={x.d} className="ticket-row" onClick={() => x.members ? onSignIn() : setT(x)}>
              <div className="t"><b>{x.implicit ? 'RSVP' : x.title}</b><small>{x.closed ? 'By application' : 'Free'}{x.members ? ' · Members only: sign in' : ''}{x.left !== null ? ` · ${x.left} left` : ''}</small></div>
              <span className="btn small">{x.left === 0 ? 'Waitlist' : x.closed ? 'Apply' : 'RSVP'}</span>
            </button>
          ))}
        </div>
      )}
      {t && (
        <Form onSubmit={submit}>
          <h3 style={{ margin: '8px 0' }}>{t.implicit ? 'RSVP' : t.title}</h3>
          {t.closed && left(t) !== 0 && <p className="apply-q">{t.prompt || 'Why would you like to come?'}</p>}
          {needs && <textarea className="input" rows={3} value={answer} maxLength={2000} placeholder={left(t) === 0 ? 'A word for the hosts (optional)' : 'Your answer'} onChange={(x) => setAnswer(x.target.value)} />}
          <label className="field"><span>Your name</span><input className="input" value={name} maxLength={100} autoComplete="name" autoFocus onChange={(x) => setName(x.target.value)} /></label>
          <label className="field"><span>E-mail{needs ? '' : ' (optional)'}</span><input className="input" type="email" inputMode="email" autoComplete="email" value={email} onChange={(x) => setEmail(x.target.value)} /></label>
          {error && <div className="error" role="alert">{error}</div>}
          <button className="btn" disabled={!name.trim() || (needs && !email.includes('@')) || (t.closed && left(t) !== 0 && !answer.trim())}>{left(t) === 0 ? 'Join the waitlist' : t.closed ? 'Send my application' : 'RSVP'}</button>
          <p className="hint" style={{ fontSize: 13, margin: '10px 0 0' }}>{needs ? 'The hosts answer by e-mail.' : 'You join the event’s chat as a guest, until 30 days after it.'}</p>
          <button type="button" className="link" onClick={() => setT(null)}>Other tickets</button>
        </Form>
      )}
      {ev.description && !t && <Markdown className="ev-about" text={ev.description} />}
      <div style={{ height: 20 }} />
      <button type="button" className="link" style={{ alignSelf: 'center' }} onClick={onSignIn}>Already have an account? Sign in</button>
    </div>
  )
}

/** An application sent before having any channel: no "no access" screen, the state instead. */
export function EventWaitScreen({ title, onSignOut }: { title: string; onSignOut: () => void }) {
  return (
    <div className="screen login">
      <Logo />
      <h2>Your application is with the hosts</h2>
      <p>The hosts of {title} will answer by e-mail. Once they give you a place, the event’s chat opens here.</p>
      <button className="btn" onClick={() => location.reload()}>Check again</button>
      <div style={{ height: 12 }} />
      <button className="link" onClick={onSignOut}>Sign out</button>
    </div>
  )
}

export { DateChip }
