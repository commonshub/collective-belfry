// An event's own screen (#/events/<link>): the channel header's pattern (one line, pulled down
// for the rest), the event's chat under it, RSVP or apply in one tap, the guest list for hosts.
// And the screen someone with no account sees when they open an event's URL in the chat.
import { LinkPair, copyText } from './LinkPair'
import { isPictureMedia } from '../core/pictures'
import { replaceHash } from '../core/nav'
import { Fragment, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import qrcode from 'qrcode-generator'
import { BELFRY, NAME, type Backend } from '../config'
import { calendar, displayName, eventByCoord, eventUrl, group, messages, profiles, type CalEvent, type Media, type Rsvp } from '../core/model'
import { useVersion } from '../core/store'
import { isLocalOnly, me } from '../core/sync'
import { reading } from '../core/guestmode'
import { checkedLive, demoCode, eventRoles, mayCheckIn, onTheDay, roleWord } from '../core/checkin'
import { postMessage } from '../core/actions'
import { CheckInScreen, TicketCard, TicketScreen } from './CheckIn'
import { addCoHost, askVersions, eventEnd, eventStart, isPastEvent, apiTicket, refreshEvent, withSales, applyFor, asks, dayInput, formPriceTags, pricesOf, isMoney, priceLabel, priceLine, type FormPrice, type Price, coHostLink, decide, eventByLink, eventLinkOf, goingList, isHost, localTz, myState, people, placesLeft, remoteEvent, rsvp, saveTicket, deleteTicket, setStatus, stillGoing, takeTicket, tickets, withdraw, fetchEvent, setPendingApplication, type ApiEvent, type People, type Ticket, type TicketForm } from '../core/events'
import { deleteEvent } from '../core/actions'
import { isGuest } from '../core/invite'
import { finalizeEvent, generateSecretKey } from 'nostr-tools'
import { sha256 } from '@noble/hashes/sha256'
import { bytesToHex, hexToBytes } from '@noble/hashes/utils'
import { makeSigner, type Session } from '../core/auth'
import { kvDel } from '../core/db'
import { Avatar, back, fmtDay, go, Lightbox, Sheet, Btn, Form, useMediaUrl, useNow } from './common'
import { Conversation, useCommunityName } from './Chat'
import { FollowButton } from './Follow'
import { Markdown } from './markdown'
import { openProfile } from './Profile'
import { CalendarIcon, ExternalIcon, CloseIcon, CopyIcon, QrIcon, DocIcon, GlobeIcon, LinkIcon, MoreIcon, PencilIcon, PeopleIcon, TrashIcon, ChevronDownIcon, ChevronUpIcon, ChevronRightIcon, ImageIcon, MessageIcon, PersonPlusIcon, PinIcon, ScanIcon, ShareIcon, TicketIcon } from './icons'
import { EventFormSheet, DateChip, when, LABEL } from './Calendar'
import { addByName, attendance, checkIn, handOver, mayRemove, removeFromMeeting, removedFromMeeting } from '../core/meetings'
import { isAdmin } from '../core/access'
import { guestJoins } from '../core/model'
import { Logo } from './Login'

const day = (d: Date) => (d.toDateString() === new Date().toDateString() ? 'Today' : fmtDay(d))
const at = (e: CalEvent) => (e.allDay ? '' : ' ' + when(e).split(' – ')[0])
const ended = (e: CalEvent) => isPastEvent(e)
/** The button that takes a ticket: "Ask for a ticket · €15", "Apply" or "RSVP". */
const pickWord = (t: { closed: boolean; price?: Price | null; priced?: boolean; onSale?: boolean }) => t.priced && t.onSale === false ? 'Not on sale now' : t.price ? `Ask for a ticket · ${priceLabel(t.price)}` : t.closed ? 'Apply' : 'RSVP'
/** A ticket's prices back in the sheet: its rows, currency and VAT (from its tags). */
const formOfPrices = (t: Ticket) => {
  const vat = t.ev?.tags.find((x) => x[0] === 'vat')
  const rows = (t.prices ?? []).map((r) => ({ amount: r.amount, from: dayInput(r.from), until: dayInput(r.until) }))
  return { prices: rows.length ? rows : [{ amount: '', from: '', until: '' }], currency: t.prices?.[0]?.currency ?? 'EUR', vat: vat ? vat[1] : t.prices?.length ? '' : '21', vatIncluded: vat?.[2] === 'included' }
}
/** What a ticket is: its price now ("€50 + 21% VAT · until 1 January 2027"), and/or how it is taken. */
const ticketSub = (t: Ticket, open = 'Free') => { const p = priceLine(t); return p ? p + (t.closed ? ' · By application' : '') : t.closed ? 'By application' : open }
const STATE_LINE: Record<string, string> = { ticket: 'You’re going', maybe: 'Maybe', cant: 'Can’t go', applied: 'Application sent: the hosts will answer', waitlist: 'On the waitlist: the hosts will tell you', declined: 'The hosts could not give you a place' }

/** The event by its URL's last part, or (older ones) by coordinate. */
export function EventScreen({ link, coord, activeThread, ticket }: { link?: string; coord?: string; activeThread?: string; ticket?: string }) {
  useVersion()
  const found = coord ? { e: eventByCoord(coord) } : eventByLink(link ?? '')
  const e = found.e
  if (e) askVersions([e.d]) // every version of it, any author (rule B; once per page load)
  if (!e) {
    const err = 'error' in found ? found.error : undefined
    return (
      <div className="screen">
        <div className="topbar"><button className="icon-btn" aria-label="Back" onClick={() => back('events')}>‹</button><h1>Event</h1></div>
        <div className="empty">{err === 'members_only' ? 'This event is for members.' : err ? 'There is no such event here.' : 'Opening the event…'}</div>
      </div>
    )
  }
  return <EventView e={e} api={'api' in found ? found.api : undefined} activeThread={activeThread} ticketCode={ticket && /^[a-z2-7]{16}$/.test(ticket) ? ticket : undefined} />
}

type Level = 0 | 1 | 2
type ETab = 'chat' | 'photos' | 'people'
type ESheet = '' | 'menu' | 'people' | 'edit' | 'tickets' | 'guests' | 'qr' | 'cohost' | 'cancel' | 'take' | 'delete' | 'ticket' | 'checkin'
const TAB_KEY = 'belfry-event-tab'

/** The event screen (docs/checkin.md §4): one header at three levels — 0 a bar (title, date, my
 *  status), 1 half (opens here: cover, when, where, my RSVP or ticket, who comes), 2 full (also the
 *  hosts, the lists, the description, the URL) — over Chat · Photos · People. Pull the header (or
 *  its handle) down or up; on a laptop the chevron cycles half ↔ full; scrolling the content folds
 *  it to the bar. */
function EventView({ e, api: given, activeThread, ticketCode }: { e: CalEvent; api?: ApiEvent; activeThread?: string; ticketCode?: string }) {
  useVersion()
  const community = useCommunityName(NAME)
  const [level, setLevel] = useState<Level>(1)
  const [tab, setTabState] = useState<ETab>(() => { try { const t = sessionStorage.getItem(TAB_KEY + e.d); return t === 'photos' || t === 'people' ? t : 'chat' } catch { return 'chat' } })
  const setTab = (t: ETab) => { setTabState(t); try { sessionStorage.setItem(TAB_KEY + e.d, t) } catch { /* private mode */ } }
  const [sheet, setSheet] = useState<ESheet>('')
  const [ticketFor, setTicketFor] = useState<Ticket | null>(null)
  const [card, setCard] = useState(ticketCode ?? '')
  useEffect(() => { if (ticketCode) setCard(ticketCode) }, [ticketCode])
  const link = eventLinkOf(e)
  // what the belfry says about me (ticket code, roles): asked for local events too (cached a minute)
  const api = given ?? remoteEvent(link) // demo mode: never asked (remoteEvent), only what a test feeds it
  const v = api?.me ?? api?.viewer ?? undefined
  const p = people(e)
  const tks = withSales(tickets(e, given), api) // left and Buy as the belfry says (sales.md §10)
  const st = myState(e, p, given)
  // back from the checkout (another tab): ask the belfry again, so "Show my ticket" comes without a reload
  useEffect(() => {
    const again = () => { if (document.visibilityState === 'visible') refreshEvent(link) }
    addEventListener('focus', again); document.addEventListener('visibilitychange', again)
    return () => { removeEventListener('focus', again); document.removeEventListener('visibilitychange', again) }
  }, [link])
  const host = isHost(e)
  const [copied, setCopied] = useState(false)
  const nowS = useNow(30_000)
  const g = group(e.groupId)
  const plain = tks.length === 1 && tks[0].implicit
  // Chat · Photos · People once I'm in: in the group, holding a ticket, or (the plain RSVP) having
  // answered; before that the header and the description only (docs/checkin.md §4)
  const inside = !!g && (g.members.has(me()) || g.admins.has(me()) || p.holders.has(me()) || host || (plain && !g.closed && !!e.rsvps.get(me())))
  const readOnly = !!g && g.closed && !g.members.has(me()) && !g.admins.has(me()) && !p.holders.has(me()) && !host
  const open = e.status !== 'cancelled' && !ended(e)
  const asking = host ? p.applications.length : 0
  const going = goingList(e, p)
  const maybe = [...e.rsvps].filter(([, s]) => s === 'tentative').map(([k]) => k)
  // my ticket: the belfry's code; in demo mode one made here
  const holds = p.holders.has(me()) || (plain && e.rsvps.get(me()) === 'accepted') || !!v?.ticket
  const code = v?.ticket_code ?? (isLocalOnly() && holds ? demoCode(e.d, me()) : undefined)
  const ticketUrl = v?.ticket_url ?? (code ? `https://chat.${BELFRY.domain}/events/${encodeURIComponent(link)}/t/${code}` : '')
  const myTicket = tks.find((t) => t.d === (p.holders.get(me()) ?? v?.ticket ?? ''))
  const checkedMine = checkedLive(e.groupId, me()) ?? v?.checked_in ?? null
  const may = mayCheckIn(e, v?.may_check_in)
  const holders = useMemo(() => {
    const m = new Map(p.holders)
    if (plain) for (const [k, s] of e.rsvps) if (s === 'accepted' && !m.has(k)) m.set(k, tks[0].d)
    return m
  }, [p.holders, e.rsvps, plain]) // eslint-disable-line react-hooks/exhaustive-deps
  const titles = new Map(tks.map((t) => [t.d, t.title]))
  const pick = (t: Ticket) => { setTicketFor(t); setSheet('take') }
  const take = async () => {
    if (plain && !e.remote && !isGuest(me())) return rsvp(e, 'accepted')
    if (tks.length === 1) return pick(tks[0])
    setSheet('take'); setTicketFor(null)
  }
  // a ticket's QR opened here: the code leaves the address at once (never shown, never kept)
  useEffect(() => { if (ticketCode) replaceHash('#/events/' + encodeURIComponent(link)) }, [ticketCode])

  // ---- pulling the header ----
  const start = useRef<{ y: number; x: number; dragging: boolean } | null>(null)
  const [drag, setDrag] = useState(0)
  const onDown = (x: React.PointerEvent) => {
    if (x.pointerType === 'mouse' && x.button !== 0) return
    // fully open, the details scroll: a drag inside them is a scroll, never a pull (Xavier, 25 Nov: About was unreadable);
    // the handle still pulls
    if (level === 2 && (x.target as Element).closest?.('.evh-body') && !(x.target as Element).closest?.('.evh-handle')) { start.current = null; return }
    start.current = { y: x.clientY, x: x.clientX, dragging: false }
  }
  const onMove = (x: React.PointerEvent) => {
    const s = start.current
    if (!s) return
    const dy = x.clientY - s.y, dx = x.clientX - s.x
    if (!s.dragging) {
      if (Math.abs(dy) < 10 || Math.abs(dy) < Math.abs(dx)) return
      s.dragging = true
      ;(x.currentTarget as HTMLElement).setPointerCapture(x.pointerId)
    }
    setDrag(dy)
  }
  const onUp = () => {
    const s = start.current
    start.current = null
    if (s?.dragging) {
      if (drag > 40) setLevel((l) => (Math.min(2, l + 1) as Level))
      else if (drag < -40) setLevel((l) => (Math.max(0, l - 1) as Level))
    }
    setDrag(0)
  }
  const swallow = (x: React.MouseEvent) => { if (start.current?.dragging) { x.stopPropagation(); x.preventDefault() } }
  const pull = { onPointerDown: onDown, onPointerMove: onMove, onPointerUp: onUp, onPointerCancel: onUp, onClickCapture: swallow }
  const fold = () => { if (level === 1) setLevel(0) }
  useEffect(() => {
    const k = (x: KeyboardEvent) => { if (x.key === 'Escape' && level === 2 && !sheet) setLevel(1) }
    addEventListener('keydown', k)
    return () => removeEventListener('keydown', k)
  }, [level, sheet])

  const statusWord = e.status === 'cancelled' ? 'Cancelled' : st === 'ticket' ? 'Going' : st === 'maybe' ? 'Maybe' : st === 'cant' ? 'Can’t go' : st === 'applied' ? 'Applied' : st === 'waitlist' ? 'Waitlist' : st === 'declined' ? 'No place' : host ? 'Hosting' : ''
  const tz = e.tz && e.tz !== localTz() && !e.allDay ? ` (${e.tz.split('/').pop()?.replace(/_/g, ' ')})` : ''
  const mapsUrl = e.location ? `https://www.openstreetmap.org/search?query=${encodeURIComponent(e.location)}` : ''

  const control = (
    <div className="ev-control">
      {e.status === 'cancelled' ? <p className="ev-state bad"><b>Cancelled</b>{e.statusReason ? ' · ' + e.statusReason : ''}</p>
        : stillGoing(e) ? <div className="ev-still"><span>The date or the place changed. Still going?</span><Btn className="btn small" onClick={() => rsvp(e, 'accepted', p.holders.get(me()))}>Yes</Btn><Btn className="btn small ghost" onClick={() => rsvp(e, 'declined', p.holders.get(me()))}>No</Btn></div>
        : st === 'applied' || st === 'waitlist' || st === 'declined' ? <RsvpRow e={e} p={p} tks={tks} st={st} onPick={pick} onWithdraw={() => withdraw(e)} />
        : !plain && !p.holders.has(me()) && !v?.ticket ? (open ? <Btn className="btn ev-main" onClick={take}><TicketIcon /> Get tickets</Btn> : null)
        : !plain ? <>
            {code && !e.meeting && <button className="btn ev-main" onClick={() => setSheet('ticket')}><TicketIcon /> Show my ticket</button>}
            {open && <RsvpRow e={e} p={p} tks={tks} st={st} onPick={pick} onWithdraw={() => withdraw(e)} />}
          </>
        : <>
            {open && <RsvpRow e={e} p={p} tks={tks} st={st} onPick={pick} onWithdraw={() => withdraw(e)} />}
            {code && !e.meeting && <button className="btn small ghost ev-ticket-link" onClick={() => setSheet('ticket')}><TicketIcon /> Show my ticket</button>}
          </>}
      {asking > 0 && <button className="btn small ghost ev-asking" onClick={() => setSheet('guests')}><PeopleIcon /> {asking} {asking === 1 ? 'person asks' : 'people ask'} to come · Review</button>}
      {may && !e.meeting && onTheDay(e) && e.status !== 'cancelled' && <button className="btn ev-checkin" onClick={() => setSheet('checkin')}><ScanIcon /> Check people in</button>}
    </div>
  )
  const faces = (keys: string[]) => (
    <span className="ev-faces">{keys.slice(0, 5).map((k) => <Avatar key={k} pubkey={k} size={24} />)}</span>
  )
  const hostKeys = [e.creator, ...[...e.hosts].filter((k) => k !== e.creator && eventRoles(e, k)[0] === 'cohost')]
  // the hosts line (canvas events/chat-*, 26 Nov): "Hosted by you and Ana Silva", you first; three names at most
  // ("you, Ana Silva and Ben Okafor"), else the first and "N others" ("you and 3 others"); no "You're hosting" line
  const hostOrder = hostKeys.includes(me()) ? [me(), ...hostKeys.filter((k) => k !== me())] : hostKeys
  const nameOfHost = (k: string) => (k === me() ? 'you' : api?.hosts.find((x) => x.pubkey === k)?.name || displayName(k))
  const hostLine = hostOrder.length <= 3
    ? hostOrder.map((k, i) => <Fragment key={k}>{i === 0 ? '' : i === hostOrder.length - 1 ? ' and ' : ', '}<b>{nameOfHost(k)}</b></Fragment>)
    : <><b>{nameOfHost(hostOrder[0])}</b> and <b>{hostOrder.length - 1} others</b></>
  // the empty chat (events/chat-host-empty, -going-empty, -not-going): one line and at most one button, by state
  const othersGoing = going.filter((k) => !hostKeys.includes(k)).length
  const emptyChat = host && othersGoing === 0
    ? <EventEmpty line="Nobody is going yet. Invite people: they'll talk here." button="Invite people" icon={<PersonPlusIcon />} onClick={() => setSheet('qr')} />
    : host || holds
      ? <EventEmpty line={`${going.length || 1} ${(going.length || 1) === 1 ? 'person is' : 'people are'} going. Nobody has written yet.`} button="Say hello" icon={<MessageIcon />} hello />
      : <EventEmpty line={`Join the event to start chatting with ${hostKeys.length === 1 ? 'the host' : 'the hosts'} and other participants.`} />
  // Join online (evview-mid, -join-now, -not-going): for people going, from 1 hour before (the lead, canvas v16)
  const online = e.links.length > 0 || !!api?.online
  const startMs = eventStart(e), endMs = eventEnd(e), nowMs = nowS * 1000
  const joinNow = holds && nowMs >= startMs - 3600_000 && nowMs < endMs
  const mins = Math.round(Math.abs(nowMs - startMs) / 60_000)
  const joinWord = !holds ? 'For people going: answer Going to get the link' : nowMs >= endMs ? 'The event has ended' : !joinNow ? 'The link appears here 1 hour before'
    : nowMs < startMs ? `Starts in ${mins} min` : `Started ${mins < 60 ? `${mins} min` : `${Math.round(mins / 60)} h`} ago`
  const PersonRow = ({ k, sub }: { k: string; sub?: string }) => (
    <button className="ev-person" onClick={() => openProfile(k, e.groupId)}><Avatar pubkey={k} size={32} /><span className="t"><b>{api?.hosts.find((x) => x.pubkey === k)?.name || displayName(k)}</b>{sub && <small>{sub}</small>}</span></button>
  )

  const header = (
    <div className={'evh lvl' + level + (drag ? ' dragging' : '')} {...pull} style={drag ? { transform: `translateY(${Math.max(-30, Math.min(30, drag / 3))}px)` } : undefined}>
      {level === 0 ? (
        <button className="evh-bar" onClick={() => setLevel(1)} aria-expanded={false} aria-label="Show the event">
          <span className="evh-date"><CalendarIcon /> {day(e.start)}{at(e)}</span>
          {statusWord && <span className={'chip evh-chip' + (st === 'ticket' ? ' on' : '')}>{statusWord}</span>}
          <ChevronDownIcon />
        </button>
      ) : (
        <div className={'evh-body' + (level === 2 ? ' full' : '')}>
          {e.cover && <Cover url={e.cover} />}
          <div className="evh-main">
            <p className="evh-when"><b>{fmtDay(e.start)}</b>{e.allDay ? '' : ' · ' + when(e)}{tz}{e.end && e.end.toDateString() !== e.start.toDateString() ? ` → ${fmtDay(e.end)}` : ''}</p>
            {e.location && <a className="evh-where" href={mapsUrl} target="_blank" rel="noreferrer"><PinIcon /> {e.location}{online ? ' · and online' : ''}</a>}
            {!e.location && online && <p className="evh-where"><LinkIcon /> Online</p>}
            {/* Event form canvas (evview-*): hosts and people going in one line under the place; fully open, › → People */}
            <button className="evh-going" onClick={() => (level === 2 ? setSheet('people') : setLevel(2))} aria-label={level === 2 ? 'See everyone going' : undefined}>
              {faces([...hostOrder, ...going.filter((k) => !hostKeys.includes(k))])}
              <span>Hosted by {hostLine}{going.length ? <span className="nowrap"> · {going.length} going</span> : ''}</span>{level === 2 && <span className="evh-more" aria-hidden="true">›</span>}
            </button>
            {control}
            <div className="evr-rows">
              {online && <div className="evr"><span className="evr-t"><b>Join online</b><small>{joinWord}</small></span>
                {joinNow && e.links[0] && <a className="btn small evr-join" href={e.links[0]} target="_blank" rel="noopener noreferrer">Join</a>}</div>}
              {e.website && <div className="evr"><span className="evr-t"><b>Event website</b><small>{e.website.replace(/^https:\/\/(www\.)?/, '').replace(/\/$/, '')}</small></span>
                <a className="icon-btn evr-ic" href={e.website} target="_blank" rel="noopener noreferrer" aria-label="Open the event website"><ExternalIcon /></a></div>}
              <div className="evr"><span className="evr-t"><b>Our page</b><small>{eventUrl(e).replace(/^https:\/\//, '')}</small></span>
                <button className="icon-btn evr-ic" aria-label={copied ? 'Copied' : 'Copy the link'} onClick={() => { void navigator.clipboard?.writeText(eventUrl(e)); setCopied(true); setTimeout(() => setCopied(false), 1500) }}>{copied ? <span className="evr-copied">Copied</span> : <CopyIcon />}</button>
                <button className="icon-btn evr-ic" aria-label="Show the link as a QR code" onClick={() => setSheet('qr')}><QrIcon /></button></div>
            </div>
            {!inside && e.description && level === 1 && <Markdown className="ev-about evh-teaser" text={e.description} />}
            {level === 2 && <>
              {!plain && <section><h3>Tickets</h3>{tks.map((t) => <TicketRow key={t.d} t={t} p={p} onPick={open && st === 'none' ? () => pick(t) : undefined} />)}</section>}
              {e.description && <section><h3>About</h3><Markdown className="ev-about" text={e.description} /></section>}
              {host && e.isNew && <h3 className="evh-host-h">For hosts</h3>}
              {host && e.isNew && (
                <section className="act-group evh-host">
                  <button className="act" onClick={() => setSheet('edit')}><PencilIcon /><span>Edit the event</span></button>
                  <button className="act" onClick={() => setSheet('cohost')}><PersonPlusIcon /><span>Invite a co-host</span></button>
                  <button className="act" onClick={() => setSheet('guests')}><PeopleIcon /><span>Guest list{asking ? ` · ${asking} asking` : ''}</span></button>
                </section>
              )}
            </>}
          </div>
        </div>
      )}
      {level > 0 && (
        <button className="evh-handle" onClick={() => setLevel(level === 1 ? 2 : 1)} aria-label={level === 1 ? 'Show everything about the event' : 'Show less'}>
          <span className="grip" />{level === 1 ? <ChevronDownIcon /> : <ChevronUpIcon />}
        </button>
      )}
    </div>
  )

  return (
    <div className="screen ev-screen">
      <div className="topbar">
        <button className="icon-btn" aria-label="Back" onClick={() => back('events')}>‹</button>
        <h1 className="tap" onClick={() => setLevel(level === 0 ? 1 : 0)}>{e.title}<small>{e.meeting ? 'Meeting · members only' : 'Event · ' + community}</small></h1>
        {g && <FollowButton channel={e.groupId} />}
        <button className="icon-btn" aria-label="More" onClick={() => setSheet('menu')}><MoreIcon /></button>
      </div>
      {card && may && <TicketCard e={e} code={card} holders={holders} titles={titles} onDone={() => setCard('')} />}
      {header}
      {level < 2 && (inside && g ? <>
        <div className="seg ev-tabs" role="tablist" aria-label="The event">
          {([['chat', 'Chat'], ['photos', 'Photos'], ['people', 'People']] as [ETab, string][]).map(([k, l]) =>
            <button key={k} role="tab" aria-selected={tab === k} className={tab === k ? 'on' : ''} onClick={() => setTab(k)}>{l}</button>)}
        </div>
        <div className="ev-content" onWheel={fold} onTouchMove={fold}>
          {tab === 'chat' && <Conversation id={e.groupId} readOnly={readOnly || e.remote === true && !p.holders.has(me())} activeThread={activeThread} empty={emptyChat} placeholder={e.meeting ? undefined : 'Message the event'} />}
          {tab === 'photos' && <EventPhotos groupId={e.groupId} readOnly={readOnly || reading()} />}
          {tab === 'people' && e.meeting && <div className="scroll ev-people pad-x"><MeetingPeople e={e} host={host} /></div>}
          {tab === 'people' && !e.meeting && <EventPeople e={e} p={p} holders={holders} titles={titles} may={may} plain={plain} onCheckIn={() => setSheet('checkin')} />}
        </div>
      </> : <div className="scroll"><div className="empty ev-closed">
          {st === 'applied' || st === 'waitlist' ? 'The event’s chat opens once the hosts give you a place.' : e.status === 'cancelled' ? 'This event is cancelled.' : 'The chat, photos and people open once you have a place.'}
        </div></div>)}
      {sheet === 'menu' && <EventMenu e={e} host={host} may={may} onClose={() => setSheet('')} open={(s) => setSheet(s)} />}
      {sheet === 'people' && <PeopleSheet e={e} hostKeys={hostKeys} going={going} maybe={maybe} onInvite={() => setSheet('qr')} onClose={() => setSheet('')} />}
      {sheet === 'edit' && <EventFormSheet edit={e} going={going.length} onTickets={() => setSheet('tickets')} onCohosts={() => setSheet('cohost')} onClose={() => setSheet('')} onSaved={(l) => { if (l !== eventLinkOf(e)) replaceHash('#/events/' + encodeURIComponent(l)) }} />}
      {sheet === 'tickets' && <TicketsSheet e={e} onClose={() => setSheet('')} />}
      {sheet === 'guests' && <GuestList e={e} onClose={() => setSheet('')} />}
      {sheet === 'qr' && <QrSheet url={eventUrl(e)} title="Our page" onClose={() => setSheet('')} />}
      {sheet === 'cohost' && <CoHostSheet e={e} onClose={() => setSheet('')} />}
      {sheet === 'cancel' && <CancelSheet e={e} onClose={() => setSheet('')} />}
      {sheet === 'delete' && <DeleteSheet e={e} onClose={() => setSheet('')} />}
      {sheet === 'take' && <TakeSheet e={e} tks={tks} p={p} t={ticketFor} onPick={setTicketFor} onClose={() => { setSheet(''); setTicketFor(null) }} />}
      {sheet === 'ticket' && ticketUrl && <TicketScreen e={e} url={ticketUrl} ticketTitle={myTicket && !myTicket.implicit ? myTicket.title : ''} checkedIn={checkedMine} onClose={() => setSheet('')} />}
      {sheet === 'checkin' && <CheckInScreen e={e} holders={holders} titles={titles} onClose={() => setSheet('')} />}
    </div>
  )
}

/** An event's chat with no messages yet: one line and at most one button. Say hello only puts the cursor in
 *  the composer (nothing is sent for you); the button then goes (events/chat-say-hello). */
function EventEmpty({ line, button, icon, onClick, hello = false }: { line: string; button?: string; icon?: ReactNode; onClick?: () => void; hello?: boolean }) {
  const [said, setSaid] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  const sayHello = () => { setSaid(true); ref.current?.closest('.ev-content')?.querySelector<HTMLTextAreaElement>('.composer textarea')?.focus() }
  return (
    <div className="ev-empty" ref={ref}>
      <p>{line}</p>
      {button && !said && <button className="btn ev-empty-cta" onClick={hello ? sayHello : onClick}>{icon}{button}</button>}
    </div>
  )
}

function Cover({ url }: { url: string }) {
  const src = useMediaUrl(url)
  return src ? <img className="evh-cover" src={src} alt="" draggable={false} /> : null
}


/** The event's photos, newest first; "Add photos" for everyone in the group. */
function EventPhotos({ groupId, readOnly }: { groupId: string; readOnly: boolean }) {
  useVersion()
  const images = messages(groupId).flatMap((m) => m.media).filter(isPictureMedia).reverse()
  const [open, setOpen] = useState<number | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const picker = useRef<HTMLInputElement>(null)
  const add = async (files: File[]) => {
    if (!files.length) return
    setBusy(true); setError('')
    try { await postMessage(groupId, '', files) } catch { setError('The photos could not be added. Try again.') } finally { setBusy(false) }
  }
  return (
    <div className="scroll ev-photos">
      {!readOnly && (
        <div className="pad-x ev-photos-add">
          <button className="btn ghost" disabled={busy} aria-busy={busy} onClick={() => picker.current?.click()}><ImageIcon /> {busy ? 'Adding…' : 'Add photos'}</button>
          <input ref={picker} type="file" accept="image/*" multiple hidden onChange={(x) => { const f = [...(x.target.files ?? [])]; x.target.value = ''; void add(f) }} />
          {error && <p className="error" role="alert">{error}</p>}
        </div>
      )}
      {images.length ? (
        <div className="photo-grid">
          {images.map((m, i) => <PhotoThumb key={m.url + i} m={m} onOpen={() => setOpen(i)} />)}
        </div>
      ) : <div className="empty">{readOnly ? 'No photos yet.' : 'Add the first photos.'}</div>}
      {open !== null && <Lightbox images={images} start={open} onClose={() => setOpen(null)} />}
    </div>
  )
}
function PhotoThumb({ m, onOpen }: { m: Media; onOpen: () => void }) {
  const src = useMediaUrl(m.url)
  return <button className="photo-thumb" onClick={onOpen} aria-label="Open the photo">{src && <img src={src} alt="" loading="lazy" />}</button>
}

/** SPEC 19.63: a meeting's people by status, never colour alone (a mark and a word each). */
type MStatus = 'in' | 'yes' | 'maybe' | 'no' | 'invited'
const STATUS_WORD: Record<MStatus, string> = { in: 'Checked in', yes: 'Confirmed', maybe: 'Maybe', no: 'Can’t come', invited: 'Invited' }
const STATUS_COUNT: Record<MStatus, string> = { in: 'checked in', yes: 'confirmed', maybe: 'maybe', no: 'can’t come', invited: 'invited' }
const STATUS_MARK: Record<MStatus, string> = { in: '✔', yes: '✓', maybe: '?', no: '✕', invited: '🕓' }
function meetingStatus(e: CalEvent, k: string, there: Map<string, boolean>): MStatus {
  if (there.get(k)) return 'in'
  const r = e.rsvps.get(k)
  if (r === 'accepted' || (k === e.creator && !r)) return 'yes'
  return r === 'tentative' ? 'maybe' : r === 'declined' ? 'no' : 'invited'
}
const StatusMark = ({ s }: { s: MStatus }) => <span className={'meet-mark ' + s} aria-hidden="true">{STATUS_MARK[s]} </span>

/** Hosts first (Host / Co-host / Door under the name), then Going, then Maybe. Tap → the
 *  profile; the message button opens a conversation. A check for who is in, for those who check in. */
function EventPeople({ e, p, holders, titles, may, plain, onCheckIn }: { e: CalEvent; p: People; holders: Map<string, string>; titles: Map<string, string>; may: boolean; plain: boolean; onCheckIn: () => void }) {
  useVersion()
  const going = goingList(e, p)
  const maybe = [...e.rsvps].filter(([, s]) => s === 'tentative').map(([k]) => k)
  const g = group(e.groupId)
  const staff = [e.creator, ...[...new Set([...(g?.admins ?? []), ...going, ...maybe, ...(g?.members ?? [])])].filter((k) => k !== e.creator && eventRoles(e, k).length > 0)]
  const rest = (keys: string[]) => keys.filter((k) => !staff.includes(k))
  const canDm = !reading()
  const Row = ({ k, sub }: { k: string; sub?: string }) => {
    const checked = may && holders.has(k) ? checkedLive(e.groupId, k) : undefined
    return (
      <div className="member ev-member">
        <button className="member-open" onClick={() => openProfile(k, e.groupId)} aria-label={`${displayName(k)}: profile`}><Avatar pubkey={k} /></button>
        <button className="member-name ev-member-name" onClick={() => openProfile(k, e.groupId)}>
          <span>{displayName(k)}{checked ? <span className="in-mark" title="Checked in" aria-label="checked in"> ✓</span> : null}</span>
          {sub && <small>{sub}</small>}
        </button>
        {canDm && k !== me() && <button className="icon-btn" aria-label={`Message ${displayName(k)}`} onClick={() => go('dm/' + encodeURIComponent('n:' + k))}><MessageIcon /></button>}
      </div>
    )
  }
  const ticketOf = (k: string) => (plain ? '' : titles.get(holders.get(k) ?? '') ?? '')
  return (
    <div className="scroll ev-people">
      {may && <button className="pull-row ev-checkin-row" onClick={onCheckIn}><ScanIcon /><span className="t">Check people in<small>Scan tickets or pick from the list</small></span><ChevronRightIcon /></button>}
      <div className="section-title">Hosts</div>
      {staff.map((k) => <Row key={k} k={k} sub={roleWord(e.groupId, k, e) || undefined} />)}
      <div className="section-title">Going <span>{rest(going).length}</span></div>
      {rest(going).length ? rest(going).map((k) => <Row key={k} k={k} sub={ticketOf(k) || undefined} />) : <p className="hint pad-x">Nobody yet.</p>}
      {rest(maybe).length > 0 && <><div className="section-title">Maybe <span>{rest(maybe).length}</span></div>{rest(maybe).map((k) => <Row key={k} k={k} />)}</>}
    </div>
  )
}

/** A meeting's people: the host and everyone invited, what they answered, and whether they were
 *  there; a host marks it for each (for people who don't use their phone). */
function MeetingPeople({ e, host }: { e: CalEvent; host: boolean }) {
  useVersion()
  const there = attendance(e)
  const gone = removedFromMeeting(e) // SPEC 19.63: removed by the maker or a belfry admin
  const keys = [...new Set([e.creator, ...e.invited, ...[...e.rsvps.keys()]])].filter((k) => !gone.has(k))
  // name-only accounts I made: mine to hand over (the belfry checks it too)
  const held = new Set(guestJoins(e.groupId).filter((j) => j.unsaved && j.by === me()).map((j) => j.pubkey))
  const [adding, setAdding] = useState({ name: '', email: '' })
  const [giving, setGiving] = useState<{ k: string; email: string } | null>(null)
  const [removing, setRemoving] = useState<string | null>(null)
  const [err, setErr] = useState('')
  // SPEC 19.63: check-in opens AT the start; before it, the maker (or a belfry admin) may remove
  const started = Date.now() >= +e.start
  const remover = mayRemove(e, isAdmin())
  const st = (k: string) => meetingStatus(e, k, there)
  const counts = (['in', 'yes', 'maybe', 'no', 'invited'] as const).map((s) => [s, keys.filter((k) => st(k) === s).length] as const).filter(([, n]) => n > 0)
  return (
    <section className="meet-people">
      <h3>People <span>{counts.map(([s, n]) => `${n} ${STATUS_COUNT[s]}`).join(' · ')}</span></h3>
      {keys.map((k) => (
        <div key={k} className={'meet-row' + (st(k) === 'in' ? ' there' : '')}>
          <button className="face" onClick={() => openProfile(k)} aria-label={`${displayName(k)}: profile`}><Avatar pubkey={k} size={32} /></button>
          <span className="t">{displayName(k)}<small><StatusMark s={st(k)} />{[roleWord(e.groupId, k, e), STATUS_WORD[st(k)]].filter(Boolean).join(' · ')}{held.has(k) ? ' · account held by you' : ''}</small></span>
          {host && held.has(k) && <button className="btn small ghost" onClick={() => setGiving({ k, email: '' })}>Hand over</button>}
          {host && started && <Btn className={'btn small' + (there.get(k) ? '' : ' ghost')} aria-pressed={!!there.get(k)} onClick={() => checkIn(e, k, !there.get(k))}>{there.get(k) ? '✓ Checked in' : 'Check in'}</Btn>}
          {!started && remover && k !== e.creator && <button className="icon-btn meet-remove" aria-label={`Remove ${displayName(k)}`} onClick={() => setRemoving(k)}><CloseIcon /></button>}
        </div>
      ))}
      {removing && (
        <Sheet title={`Remove ${displayName(removing)}?`} onClose={() => setRemoving(null)}>
          <p className="hint">They are no longer invited to {e.title}.</p>
          <Btn className="btn danger" onClick={async () => { setErr(''); try { await removeFromMeeting(e, removing); setRemoving(null) } catch (y) { setErr(y instanceof Error ? y.message : String(y)) } }}>Remove</Btn>
          <button className="btn ghost" onClick={() => setRemoving(null)}>Cancel</button>
        </Sheet>
      )}
      {giving && (
        <Form className="row meet-give" onSubmit={async (x) => { x.preventDefault(); setErr(''); try { await handOver(e.groupId, giving.k, giving.email); setGiving(null) } catch (y) { setErr(y instanceof Error ? y.message : String(y)) } }}>
          <input className="input" type="email" autoFocus placeholder={`${displayName(giving.k)}'s e-mail`} value={giving.email} onChange={(x) => setGiving({ ...giving, email: x.target.value })} aria-label="Their e-mail" />
          <button className="btn small" disabled={!/^\S+@\S+\.\S+$/.test(giving.email.trim())}>Give it</button>
        </Form>
      )}
      {host && (
        <Form className="row meet-add" onSubmit={async (x) => { x.preventDefault(); setErr(''); try { await addByName(e, { name: adding.name, ...(adding.email.trim() ? { email: adding.email.trim() } : {}) }); setAdding({ name: '', email: '' }) } catch (y) { setErr(y instanceof Error ? y.message : String(y)) } }}>
          <input className="input" placeholder="Add someone by name" value={adding.name} maxLength={100} onChange={(x) => setAdding({ ...adding, name: x.target.value })} aria-label="Their name" />
          <input className="input" type="email" placeholder="E-mail (optional)" value={adding.email} onChange={(x) => setAdding({ ...adding, email: x.target.value })} aria-label="Their e-mail (optional)" />
          <button className="btn small ghost" disabled={!adding.name.trim()}>Add</button>
        </Form>
      )}
      {err && <p className="error" role="alert">{err}</p>}
    </section>
  )
}

/** Going / Maybe / Can't go (the plain RSVP), or the ticket you hold, or your application. */
function RsvpRow({ e, p, tks, st, onPick, onWithdraw }: { e: CalEvent; p: People; tks: Ticket[]; st: string; onPick: (t: Ticket) => void; onWithdraw: () => unknown }) {
  const held = p.holders.get(me())
  const plain = tks.length === 1 && tks[0].implicit && !e.remote && !isGuest(me())
  if (st === 'applied' || st === 'waitlist')
    return <div className="row" style={{ gap: 8, justifyContent: 'space-between' }}><span>{st === 'applied' && tks.find((t) => t.d === p.applications.find((a) => a.pubkey === me())?.ticket)?.priced ? 'Waiting for the host to confirm your payment' : STATE_LINE[st]}</span><Btn className="btn small ghost" onClick={onWithdraw}>Withdraw</Btn></div>
  if (st === 'declined') return <p className="hint">{STATE_LINE.declined}.</p>
  if (!plain && !held && st === 'none')
    return <div className="rsvp">{tks.length === 1 && tks[0].checkout ? <BuyLink href={tks[0].checkout} className="on" /> : tks.length === 1 ? <button className="on" onClick={() => onPick(tks[0])}>{pickWord(tks[0])}</button> : <button className="on" onClick={() => onPick(tks[0])}>Pick a ticket</button>}</div>
  const mine = e.rsvps.get(me()) ?? (held ? 'accepted' : undefined)
  return (
    <div className="rsvp">
      {(['accepted', 'tentative', 'declined'] as Rsvp[]).map((s) => (
        <Btn key={s} className={mine === s ? 'on' : ''} aria-pressed={mine === s} onClick={() => rsvp(e, s, held)}>{LABEL[s]}</Btn>
      ))}
    </div>
  )
}

/** "Buy" (sales.md §10, board events/get-tickets): the community's checkout page, in the browser. */
function BuyLink({ href, className = 'btn small' }: { href: string; className?: string }) {
  return <a className={className + ' buy'} href={href} target="_blank" rel="noopener noreferrer" aria-label="Buy, opens the ticket page in the browser" onClick={(x) => x.stopPropagation()}>Buy <ExternalIcon /></a>
}

function TicketRow({ t, p, onPick }: { t: Ticket; p: People; onPick?: () => void }) {
  const left = placesLeft(t, p)
  const full = left === 0
  return (
    <div className="ticket-row">
      <div className="t"><b>{t.title}</b>
        <small>{ticketSub(t)}{t.members ? ' · Members only' : ''}{left !== undefined ? ` · ${left} left` : ''}</small>
        {t.summary && <small>{t.summary}</small>}
      </div>
      {onPick && (t.checkout && !full ? <BuyLink href={t.checkout} />
        : <button className="btn small" disabled={t.priced && t.onSale === false} onClick={onPick}>{full ? 'Waitlist' : t.priced && t.onSale === false ? 'Not on sale' : t.price ? 'Ask' : pickWord(t)}</button>)}
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
  const needs = asks(t) || waitlist
  if (t.members && !member)
    return <Sheet title={t.title} onClose={onClose}><p>This ticket is for members of {NAME}.</p><button className="btn" onClick={onClose}>OK</button></Sheet>
  return (
    <Sheet title={waitlist ? `Waitlist: ${t.title}` : t.closed && !t.priced ? `Apply: ${t.title}` : t.title} onClose={onClose}>
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
        {t.price && !waitlist && <p className="ticket-price"><b>{priceLine(t)}</b> · you pay the hosts; once they confirm your payment, the place is yours.</p>}
        {needs && <textarea className="input" rows={4} autoFocus value={answer} maxLength={2000} placeholder={waitlist || !t.closed ? 'A word for the hosts (optional)' : 'Your answer'} onChange={(x) => setAnswer(x.target.value)} />}
        {!needs && <p>{t.summary || 'A free place.'}{left !== undefined ? ` ${left} left.` : ''}</p>}
        {error && <p className="error" role="alert">{error}</p>}
        <div style={{ height: 12 }} />
        <button className="btn" disabled={t.closed && !waitlist && !answer.trim()}>{waitlist ? 'Join the waitlist' : t.price ? `Ask for a ticket · ${priceLabel(t.price)}` : t.closed ? 'Send my application' : 'RSVP'}</button>
      </Form>
    </Sheet>
  )
}

function EventMenu({ e, host, may, onClose, open }: { e: CalEvent; host: boolean; may: boolean; onClose: () => void; open: (s: 'edit' | 'tickets' | 'guests' | 'qr' | 'cohost' | 'cancel' | 'delete' | 'checkin') => void }) {
  const [copied, setCopied] = useState(false)
  const Item = ({ icon, label, onClick, danger = false }: { icon: ReactNode; label: string; onClick: () => void; danger?: boolean }) => (
    <button className={'act' + (danger ? ' danger' : '')} onClick={onClick}>{icon}<span>{label}</span></button>
  )
  return (
    <Sheet title="Event" hideTitle onClose={onClose}>
      {/* events/chat-more-menu: the share actions live here, not in the chat */}
      <div className="act-group">
        <Item icon={<PersonPlusIcon />} label="Invite people" onClick={() => open('qr')} />
        {host && e.isNew && <Item icon={<PeopleIcon />} label="Invite a co-host" onClick={() => open('cohost')} />}
      </div>
      <div className="act-group">
        <Item icon={<LinkIcon />} label={copied ? 'Copied' : 'Copy the link'} onClick={async () => { await copyText(eventUrl(e)); setCopied(true); setTimeout(onClose, 600) }} />
        <Item icon={<QrIcon />} label="QR code" onClick={() => open('qr')} />
        {e.isNew && e.visibility !== 'members' && <Item icon={<GlobeIcon />} label="Open the public page" onClick={() => { window.open(eventUrl(e), '_blank', 'noopener'); onClose() }} />}
      </div>
      {may && !e.meeting && <div className="act-group"><Item icon={<ScanIcon />} label="Check people in" onClick={() => open('checkin')} /></div>}
      {host && e.isNew && (
        <div className="act-group">
          <Item icon={<PencilIcon />} label="Edit the event" onClick={() => open('edit')} />
          <Item icon={<DocIcon />} label="Tickets" onClick={() => open('tickets')} />
          <Item icon={<PeopleIcon />} label="Guest list" onClick={() => open('guests')} />
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

function CopyButton({ text, label }: { text: string; label: string }) {
  const [done, setDone] = useState(false)
  return <button className="btn small ghost" onClick={async () => { await copyText(text); setDone(true); setTimeout(() => setDone(false), 1500) }}>{done ? 'Copied' : label}</button>
}

/** A QR code is a button: it opens full width on white with the URL under it. */
export function QrSheet({ url, title, onClose }: { url: string; title: string; onClose: () => void }) {
  const [copied, setCopied] = useState(false)
  const svg = useMemo(() => { const q = qrcode(0, 'M'); q.addData(url); q.make(); return 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(q.createSvgTag({ cellSize: 8, margin: 2, scalable: true })) }, [url])
  return (
    <Sheet title={title} onClose={onClose}>
      <div className="qr-full"><img src={svg} alt={`QR code for ${url}`} /></div>
      {/* Xavier (25 Nov): the address on one line, scrolling sideways, a small copy icon beside it */}
      <div className="qr-url">
        <code tabIndex={0} aria-label={url}>{url.replace(/^https:\/\//, '')}</code>
        <button className="icon-btn qr-copy" aria-label={copied ? 'Copied' : 'Copy the link'} onClick={() => { void navigator.clipboard?.writeText(url); setCopied(true); setTimeout(() => setCopied(false), 1500) }}>{copied ? <span className="evr-copied">Copied</span> : <CopyIcon />}</button>
      </div>
      <p className="hint">Show it to someone: their camera opens the page.</p>
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
  const money = editing ? formPriceTags(editing) : { tags: [] as string[][] }
  const shown = editing && !money.error ? priceLine(pricesOf(money.tags)) : ''
  if (editing) return (
    <Sheet title={editing.id ? 'Edit the ticket' : 'New ticket'} onClose={onClose}>
      <Form onSubmit={async (x) => { x.preventDefault(); await saveTicket(e, editing); setEditing(null) }}>
        <label className="field"><span>Name</span><input className="input" autoFocus value={editing.title} maxLength={120} placeholder="e.g. Standard, Speaker, Volunteer" onChange={(x) => setEditing({ ...editing, title: x.target.value })} /></label>
        <label className="field"><span>Summary</span><input className="input" value={editing.summary} maxLength={300} onChange={(x) => setEditing({ ...editing, summary: x.target.value })} /></label>
        <label className="field"><span>How many places</span><input className="input" inputMode="numeric" placeholder="No limit" value={editing.quantity} onChange={(x) => setEditing({ ...editing, quantity: x.target.value.replace(/\D/g, '') })} /></label>
        {/* prices (Xavier, 2026-11-18): an amount, optionally for a while (from / until), in Euros
            (with VAT: 21% on top by default), Hours or a label of the host's own */}
        <div className="field price-head"><span>Price</span>
          <select className="input" aria-label="Currency" value={['EUR', 'HOUR'].includes(editing.currency) ? editing.currency : 'other'} onChange={(x) => setEditing({ ...editing, currency: x.target.value === 'other' ? '' : x.target.value })}>
            <option value="EUR">Euros</option><option value="HOUR">Hours</option><option value="other">Other…</option>
          </select>
        </div>
        {!['EUR', 'HOUR'].includes(editing.currency) && <label className="field"><span>Paid in</span><input className="input" autoFocus maxLength={12} placeholder="e.g. tokens" value={editing.currency} onChange={(x) => setEditing({ ...editing, currency: x.target.value.replace(/[^\p{L}\p{N} ]/gu, '') })} /></label>}
        <div className="price-rows">
          {editing.prices.map((r, i) => {
            const put = (patch: Partial<FormPrice>) => setEditing({ ...editing, prices: editing.prices.map((x, j) => (j === i ? { ...x, ...patch } : x)) })
            return (
              <div key={i} className="price-row">
                <input className="input" inputMode="decimal" aria-label={i ? `Price ${i + 1}` : 'Price'} placeholder="Free" value={r.amount} onChange={(x) => put({ amount: x.target.value.replace(/[^\d.,]/g, '') })} />
                <label className="price-date"><span>From</span><input className="input" type="date" aria-label={`From (price ${i + 1})`} value={r.from} onChange={(x) => put({ from: x.target.value })} /></label>
                <label className="price-date"><span>Until</span><input className="input" type="date" aria-label={`Until (price ${i + 1})`} value={r.until} onChange={(x) => put({ until: x.target.value })} /></label>
                {editing.prices.length > 1 && <button type="button" className="icon-btn" aria-label={`Remove price ${i + 1}`} onClick={() => setEditing({ ...editing, prices: editing.prices.filter((_, j) => j !== i) })}><TrashIcon /></button>}
              </div>
            )
          })}
          <button type="button" className="link price-add" onClick={() => setEditing({ ...editing, prices: [...editing.prices, { amount: '', from: '', until: '' }] })}>＋ Add a price (e.g. early bird until a date)</button>
        </div>
        {isMoney(editing.currency) && money.tags.length > 0 && (
          <div className="vat-row">
            <label className="vat-rate"><input className="input" inputMode="decimal" aria-label="VAT rate" placeholder="0" value={editing.vat} onChange={(x) => setEditing({ ...editing, vat: x.target.value.replace(/[^\d.,]/g, '') })} /><span>% VAT</span></label>
            <label className="toggle"><input type="checkbox" checked={editing.vatIncluded} onChange={(x) => setEditing({ ...editing, vatIncluded: x.target.checked })} /> Price includes VAT</label>
          </div>
        )}
        {money.error && <p className="error" style={{ marginTop: -4 }}>{money.error}</p>}
        {!money.error && money.tags.length > 0 && <p className="hint" style={{ marginTop: -4 }}>{shown ? <><b>{shown}</b> · </> : null}People ask for it; they pay you, and you mark them paid to give them their place.{editing.prices.some((r) => r.amount && (r.from || r.until)) && !editing.prices.some((r) => r.amount && !r.from && !r.until) ? ' Outside its dates it is not on sale.' : ''}</p>}
        <div className="choice" style={{ marginTop: 4 }}>
          <button type="button" className={!editing.closed ? 'on' : ''} onClick={() => setEditing({ ...editing, closed: false })}><b>Open</b><small>{money.tags.length ? 'Anyone can ask for it' : 'One tap and you’re in'}</small></button>
          <button type="button" className={editing.closed ? 'on' : ''} onClick={() => setEditing({ ...editing, closed: true })}><b>Needs an application</b><small>Hosts approve</small></button>
        </div>
        {editing.closed && <label className="field"><span>The question applicants answer</span><textarea className="input" rows={2} value={editing.prompt} placeholder="Why would you like to come?" onChange={(x) => setEditing({ ...editing, prompt: x.target.value })} /></label>}
        <label className="toggle"><input type="checkbox" checked={editing.members} onChange={(x) => setEditing({ ...editing, members: x.target.checked })} /> Members only (shown to everyone, taken by members)</label>
        <button className="btn" disabled={!editing.title.trim() || !!money.error}>Save the ticket</button>
      </Form>
    </Sheet>
  )
  return (
    <Sheet title="Tickets" onClose={onClose}>
      {list.length === 0 && <p className="hint">No tickets: anyone who may see the event RSVPs in one tap, without a limit. Add a ticket for a limit, an application or a members-only place.</p>}
      {list.map((t) => (
        <div key={t.d} className="ticket-row">
          <div className="t"><b>{t.title}</b><small>{ticketSub(t, 'Open')}{t.members ? ' · Members only' : ''}{t.quantity !== undefined ? ` · ${t.quantity} places` : ''}</small></div>
          <button className="icon-btn" aria-label={`Edit ${t.title}`} onClick={() => setEditing({ id: t.id, title: t.title, summary: t.summary, quantity: t.quantity === undefined ? '' : String(t.quantity), closed: t.closed, prompt: t.prompt, members: t.members, position: t.position, ...formOfPrices(t) })}><PencilIcon /></button>
          <Btn className="icon-btn" aria-label={`Delete ${t.title}`} onClick={() => deleteTicket(e, t)}><TrashIcon /></Btn>
        </div>
      ))}
      <div style={{ height: 8 }} />
      <button className="btn ghost" onClick={() => setEditing({ title: '', summary: '', quantity: '', closed: false, prompt: '', members: false, position: list.length + 1, prices: [{ amount: '', from: '', until: '' }], currency: 'EUR', vat: '21', vatIncluded: false })}>＋ Add a ticket</button>
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
  const titleOf = (d?: string) => { const t = tks.find((x) => x.d === d); return t ? t.title + (t.price ? ' · ' + priceLabel(t.price) : '') : '' }
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
      <Btn className="btn small" onClick={() => decide(e, a.pubkey, 'approve', a.ticket, a.ev)}>{tks.find((t) => t.d === a.ticket)?.priced && !a.waitlist ? 'Mark paid and admit' : 'Approve'}</Btn>
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
  return calendar().filter((e) => e.isNew && +(e.end ?? e.start) >= since && (() => { const s = myState(e); return s === 'host' || s === 'ticket' || s === 'maybe' || s === 'applied' || s === 'waitlist' || e.hosts.has(me()) || (e.meeting && e.invited.includes(me())) })())
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
  if (!r.ok) throw new Error('That did not work just now. Try again.')
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
    { d: '2026-demo01:sup', id: 'sup', title: 'Supporter', summary: '', quantity: 20, left: 12, closed: false, prompt: '', members: false, position: 3, implicit: false,
      price: { amount: '50', currency: 'EUR', vat: '21', vat_included: false }, can_pay: true, checkout_url: `https://${BELFRY.domain}/tickets?event=garden-day&ticket=2026-demo01%3Asup` },
  ],
}

export function EventJoinScreen({ link, backend, onJoined, onSignIn, onApply, onDemo }: { link: string; backend: Backend; onJoined: (s: Session) => void; onSignIn: () => void; /** members only: the start screen's application */ onApply?: () => void; onDemo: (name: string) => void }) {
  const community = useCommunityName(NAME)
  const [ev, setEv] = useState<ApiEvent | null>(null)
  const [error, setError] = useState('')
  const [t, setT] = useState<ReturnType<typeof apiTicket> | null>(null)
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
        {error ? <><h2>{error === 'members' ? 'This event is for members' : 'There is no such event here'}</h2><button className="btn" onClick={onSignIn}>I have an account: sign in</button>{error === 'members' && onApply && <button className="link start-apply" onClick={onApply}>Apply to join {community}</button>}</> : <p role="status">Opening the event…</p>}
      </div>
    )
  const start = ev.start ? new Date(ev.start) : undefined
  const open = ev.status !== 'cancelled' && !(ev.end && new Date(ev.end) < new Date())
  const left = (x: { left: number | null }) => x.left
  const needs = !!t && (asks(t) || left(t) === 0)
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
      replaceHash(`#/events/${encodeURIComponent(link)}`)
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
          {ev.tickets.map(apiTicket).map((x) => x.checkout && x.left !== 0 && !x.members ? (
            <div key={x.d} className="ticket-row">
              <div className="t"><b>{x.title}</b><small>{ticketSub(x)}{x.left !== null ? ` · ${x.left} left` : ''}</small></div>
              <BuyLink href={x.checkout} />
            </div>
          ) : (
            <button key={x.d} className="ticket-row" onClick={() => x.members ? onSignIn() : setT(x)}>
              <div className="t"><b>{x.implicit ? 'RSVP' : x.title}</b><small>{ticketSub(x)}{x.members ? ' · Members only: sign in' : ''}{x.left !== null ? ` · ${x.left} left` : ''}</small></div>
              <span className="btn small">{x.left === 0 ? 'Waitlist' : x.priced && x.onSale === false ? 'Not on sale' : x.price ? 'Ask' : pickWord(x)}</span>
            </button>
          ))}
        </div>
      )}
      {t && (
        <Form onSubmit={submit}>
          <h3 style={{ margin: '8px 0' }}>{t.implicit ? 'RSVP' : t.title}</h3>
          {t.closed && left(t) !== 0 && <p className="apply-q">{t.prompt || 'Why would you like to come?'}</p>}
          {t.price && left(t) !== 0 && <p className="ticket-price"><b>{priceLine(t)}</b> · you pay the hosts; once they confirm your payment, the place is yours.</p>}
          {needs && <textarea className="input" rows={3} value={answer} maxLength={2000} placeholder={left(t) === 0 || !t.closed ? 'A word for the hosts (optional)' : 'Your answer'} onChange={(x) => setAnswer(x.target.value)} />}
          <label className="field"><span>Your name</span><input className="input" value={name} maxLength={100} autoComplete="name" autoFocus onChange={(x) => setName(x.target.value)} /></label>
          <label className="field"><span>E-mail{needs ? '' : ' (optional)'}</span><input className="input" type="email" inputMode="email" autoComplete="email" value={email} onChange={(x) => setEmail(x.target.value)} /></label>
          {error && <div className="error" role="alert">{error}</div>}
          <button className="btn" disabled={!name.trim() || (needs && !email.includes('@')) || (t.closed && left(t) !== 0 && !answer.trim())}>{left(t) === 0 ? 'Join the waitlist' : t.price ? `Ask for a ticket · ${priceLabel(t.price)}` : t.closed ? 'Send my application' : 'RSVP'}</button>
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

/** Everyone, like a channel's or group's members (Event form canvas, evview-people): search, Hosts (Host,
 *  Co-host), Going · N (See all), Maybe · N; a name opens the profile; + invites (shares our page). */
function PeopleSheet({ e, hostKeys, going, maybe, onInvite, onClose }: { e: CalEvent; hostKeys: string[]; going: string[]; maybe: string[]; onInvite: () => void; onClose: () => void }) {
  const [q, setQ] = useState('')
  const [all, setAll] = useState(false)
  const everyone = [...new Set([...hostKeys, ...going, ...maybe])]
  const hit = (k: string) => !q.trim() || displayName(k).toLowerCase().includes(q.trim().toLowerCase())
  const row = (k: string, sub?: string) => <button key={k} className="ev-person" onClick={() => openProfile(k, e.groupId)}><Avatar pubkey={k} size={32} /><span className="t"><b>{displayName(k)}</b>{sub && <small>{sub}</small>}</span></button>
  const g = going.filter((k) => !hostKeys.includes(k) && hit(k))
  return (
    <Sheet title="People" onClose={onClose}>
      <div className="ev-people-top"><small className="hint">{e.title}</small><button className="icon-btn" aria-label="Invite someone" onClick={onInvite}><PersonPlusIcon /></button></div>
      <input className="input" type="search" placeholder={`Search ${everyone.length} ${everyone.length === 1 ? 'person' : 'people'}`} value={q} onChange={(x) => setQ(x.target.value)} />
      <section><h3>Hosts · {hostKeys.length}</h3>{hostKeys.filter(hit).map((k) => row(k, k === e.creator ? 'Host' : 'Co-host'))}</section>
      {going.length > 0 && <section><h3>Going · {going.length}</h3>{(all || q ? g : g.slice(0, 5)).map((k) => row(k))}
        {!all && !q && g.length > 5 && <button className="link" onClick={() => setAll(true)}>See all {going.length}</button>}</section>}
      {maybe.length > 0 && <section><h3>Maybe · {maybe.length}</h3>{maybe.filter(hit).map((k) => row(k))}</section>}
    </Sheet>
  )
}
