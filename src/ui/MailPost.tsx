import { useEffect, useRef, useState } from 'react'
import { finalizeEvent, generateSecretKey, type NostrEvent } from 'nostr-tools'
import { addEvents } from '../core/store'
import { isLocalOnly, me } from '../core/sync'
import { DEMO_GROUPS, fetchLarge, report, sendAsChannel } from '../core/dm'
import { hexToBytes } from '@noble/hashes/utils'
import { assignCard, setCardStatus } from '../core/actions'
import { displayName, realName, type CardState, type Message, type ThreadInfo } from '../core/model'
import { BELFRY } from '../config'
import { parseMail, type Mail } from '../core/mime'
import { fmtTime, MediaGrid, Sheet } from './common'

// A channel message a member sent by e-mail (posted by the belfry's mail bridge as them):
// it shows as their message, with a "via e-mail" tag; the whole e-mail stays one tap away.

export interface MailPost { subject: string; original?: { url: string; key: string; nonce: string } }

export function mailOf(ev: NostrEvent): MailPost | null {
  if (!ev.tags.some((t) => t[0] === 'client' && t[1] === 'belfry-mail')) return null
  const o = ev.tags.find((t) => t[0] === 'mail-original' && t[1]?.startsWith('https://') && t[2] && t[3])
  return {
    subject: ev.tags.find((t) => t[0] === 'subject')?.[1] ?? '',
    original: o ? { url: o[1], key: o[2], nonce: o[3] } : undefined,
  }
}

/** The text without the "**Subject**" line the bridge puts first (for other Nostr clients). */
export function mailText(text: string, mail: MailPost): string {
  const head = `**${mail.subject}**`
  return mail.subject && text.startsWith(head) ? text.slice(head.length).trimStart() : text
}

export function ViaMail() {
  return (
    <span className="via-mail" title="Sent by e-mail">
      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><rect x="3" y="5" width="18" height="14" rx="2" /><path d="m3 7 9 6 9-6" /></svg>
      via e-mail
    </span>
  )
}

export function FullMail({ mail, onClose }: { mail: MailPost; onClose: () => void }) {
  const [got, setGot] = useState<Mail | null>(null)
  const [error, setError] = useState('')
  useEffect(() => {
    if (!mail.original) return
    fetchLarge(mail.original).then((raw) => setGot(parseMail(raw)), () => setError('The e-mail could not be opened.'))
  }, [mail])
  const h = got?.headers
  return (
    <Sheet title={mail.subject || 'E-mail'} onClose={onClose}>
      {error && <div className="error">{error}</div>}
      {!got && !error && <div className="empty">Opening…</div>}
      {got && (
        <div className="full-mail">
          <dl>
            <dt>From</dt><dd>{got.from}</dd>
            {h?.get('to') && <><dt>To</dt><dd>{h.get('to')}</dd></>}
            {h?.get('date') && <><dt>Date</dt><dd>{h.get('date')}</dd></>}
          </dl>
          <pre>{got.full}</pre>
          {got.attachments.length > 0 && <p className="muted">{got.attachments.length} attachment(s), shown in the message.</p>}
        </div>
      )}
    </Sheet>
  )
}

/** Demo mode only (tests): a member's e-mail lands in CHANNEL, as the bridge would post it. */
export function demoMailPost(channel: string, name: string, subject: string, text: string) {
  if (!isLocalOnly()) return
  const sk = generateSecretKey()
  addEvents([
    finalizeEvent({ kind: 0, created_at: Math.floor(Date.now() / 1000) - 60, tags: [], content: JSON.stringify({ name }) }, sk),
    finalizeEvent({
      kind: 9, created_at: Math.floor(Date.now() / 1000), content: `**${subject}**\n\n${text}`,
      tags: [['h', channel], ['subject', subject], ['mail-original', 'https://blossom.invalid/demo', 'AAAA', 'AAAA'], ['client', 'belfry-mail']],
    }, sk),
  ])
}

// ---- shared inboxes: e-mail cards (posted by the groups key) and what went out ----------

export interface InboxCard { from: string; name: string; subject: string; messageId?: string; verified: boolean; post: MailPost }

const tagv = (ev: NostrEvent, k: string) => ev.tags.find((t) => t[0] === k)?.[1]

/** An e-mail from outside to a shared inbox, if KEY (the groups key) posted it. */
export function cardOf(ev: NostrEvent, key?: string): InboxCard | null {
  if (!key || ev.pubkey !== key || !tagv(ev, 'mail-from')) return null
  const post = mailOf(ev)
  if (!post) return null
  return { from: tagv(ev, 'mail-from')!, name: tagv(ev, 'mail-name') ?? '', subject: post.subject, messageId: tagv(ev, 'message-id'), verified: tagv(ev, 'belfry-auth') === 'pass', post }
}

/** An answer that went out as the channel (only the bridge posts these, with the groups key). */
export function sentOf(ev: NostrEvent, key?: string): { to: string; subject: string; by: string } | null {
  if (!key || ev.pubkey !== key || !ev.tags.some((t) => t[0] === 'client' && t[1] === 'belfry-mail-out')) return null
  return { to: tagv(ev, 'mail-to') ?? '', subject: tagv(ev, 'subject') ?? '', by: tagv(ev, 'sent-by') ?? '' }
}

const Envelope = () => (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><rect x="3" y="5" width="18" height="14" rx="2" /><path d="m3 7 9 6 9-6" /></svg>
)
const STATUS: Record<CardState['status'], string> = { open: 'Open', waiting: 'Answered', closed: 'Closed' }
const UNDO_SECONDS = 8

export function InboxCardView({ m, card, groupId, channelName, state, canSend, thread, onThread }: {
  m: Message; card: InboxCard; groupId: string; channelName: string; state?: CardState; canSend: boolean; thread?: ThreadInfo; onThread: () => void
}) {
  const [full, setFull] = useState(false)
  const [writing, setWriting] = useState(false)
  const [draft, setDraft] = useState('')
  const [countdown, setCountdown] = useState(0)
  const [sentId, setSentId] = useState('')
  const [error, setError] = useState('')
  const timer = useRef<ReturnType<typeof setInterval> | null>(null)
  const pending = useRef<(() => void) | null>(null)
  const status = state?.status ?? 'open'
  const who = card.name ? `${card.name} <${card.from}>` : card.from
  const first = card.name.split(' ')[0] || card.from
  const subject = /^re:/i.test(card.subject) ? card.subject : `Re: ${card.subject || '(no subject)'}`
  const r = report(sentId)

  // leaving the screen during the countdown sends at once: nobody should believe it went when it didn't
  useEffect(() => () => { if (timer.current) { clearInterval(timer.current); pending.current?.() } }, [])
  useEffect(() => { if (r?.status === 'delivered') { const t = setTimeout(() => setSentId(''), 4000); return () => clearTimeout(t) } }, [r?.status])

  const send = (text: string) => {
    setWriting(false); setError('')
    setCountdown(UNDO_SECONDS)
    let left = UNDO_SECONDS
    const go = () => sendAsChannel({ channel: groupId, channelName, to: card.from, subject, text, inReplyTo: card.messageId, cardId: m.ev.id })
    pending.current = () => void go()
    timer.current = setInterval(() => {
      left--
      setCountdown(left)
      if (left > 0) return
      clearInterval(timer.current!); timer.current = null; pending.current = null
      go().then((id) => { setSentId(id); setDraft('') }, (e) => { setError(e instanceof Error ? e.message : String(e)); setWriting(true) })
    }, 1000)
  }
  const undo = () => { if (timer.current) clearInterval(timer.current); timer.current = null; pending.current = null; setCountdown(0); setWriting(true) }

  return (
    <div className="mailcard">
      <div className="mc-head"><Envelope />E-mail received · {fmtTime(m.ev.created_at)}</div>
      <div className="mc-body">
        <div className="mc-from">{who}{!card.verified && <span className="dm-warn" title="The sender's provider did not prove this address (no DKIM or SPF)">unverified sender</span>}</div>
        {card.subject && <b className="mc-subject">{card.subject}</b>}
        <p className="mc-text">{mailText(m.text, card.post) || '(empty)'}</p>
        <MediaGrid media={m.media} />
        {card.post.original && <button className="link full-mail-btn" onClick={() => setFull(true)}>Full e-mail</button>}
      </div>
      <div className="mc-foot">
        <span className={'mc-pill ' + status}>{STATUS[status]}</span>
        <button className="mc-who" onClick={() => void assignCard(groupId, m.ev.id, state?.assignee === me() ? '' : me())}
          title={state?.assignee === me() ? 'Tap to let it go' : 'Tap to take it'}>
          {state?.assignee ? displayName(state.assignee) : 'Nobody yet · take it'}
        </button>
        <button className="thread-chip" onClick={onThread}>{thread?.count ? `Thread · ${thread.count}` : 'Start a thread'}</button>
      </div>
      <div className="mc-acts">
        {canSend && <button className="mc-reply" onClick={() => setWriting(true)}>Reply by e-mail</button>}
        <button className="btn ghost mc-sec" onClick={() => void setCardStatus(groupId, m.ev.id, status === 'closed' ? 'open' : 'closed')}>{status === 'closed' ? 'Reopen' : 'Close'}</button>
      </div>
      {full && <FullMail mail={card.post} onClose={() => setFull(false)} />}
      {writing && (
        <Sheet title={`E-mail to ${first}`} onClose={() => setWriting(false)}>
          <div className="mail-sheet">
            <dl>
              <dt>To</dt><dd>{who}</dd>
              <dt>From</dt><dd>{groupId}@{BELFRY.domain}<small>Signed “{realName(me()) ? `${realName(me())} for ${channelName}` : channelName}”</small></dd>
              <dt>Subject</dt><dd>{subject}</dd>
            </dl>
            <textarea className="input" rows={8} autoFocus value={draft} onChange={(e) => setDraft(e.target.value)} placeholder={`Write to ${first}…`} />
            {error && <div className="error">{error}</div>}
            <div className="mail-warn">{first} will receive exactly this. The thread stays inside the belfry.</div>
            <button className="btn mail-send" disabled={!draft.trim()} onClick={() => send(draft.trim())}>Send e-mail</button>
          </div>
        </Sheet>
      )}
      {countdown > 0 && <div className="toast" role="status"><span>Sending to {first} in {countdown} s</span><button onClick={undo}>Undo</button></div>}
      {!countdown && sentId && (
        <div className="toast" role="status">
          <span>{r?.status === 'delivered' ? `E-mail sent to ${first}` : r?.status === 'failed' ? `Not delivered: ${r.details}` : r?.status === 'delayed' ? 'Delayed, the provider retries' : 'Sending…'}</span>
          {r?.status !== 'queued' && <button onClick={() => setSentId('')}>OK</button>}
        </div>
      )}
    </div>
  )
}

export function SentCardView({ m, sent, groupId }: { m: Message; sent: { to: string; subject: string; by: string }; groupId: string }) {
  return (
    <div className="mailcard out">
      <div className="mc-head out"><Envelope />E-mail sent by {sent.by ? displayName(sent.by) : 'a member'} as {groupId}@ · {fmtTime(m.ev.created_at)}</div>
      <div className="mc-body">
        <div className="mc-from">To {sent.to}</div>
        {sent.subject && <b className="mc-subject">{sent.subject}</b>}
        <p className="mc-text">{m.text}</p>
      </div>
    </div>
  )
}

/** Demo mode only (tests): an e-mail from outside lands in the shared inbox CHANNEL. */
export function demoInboxMail(channel: string, from: string, name: string, subject: string, text: string) {
  if (!isLocalOnly()) return
  addEvents([finalizeEvent({
    kind: 9, created_at: Math.floor(Date.now() / 1000), content: `**${subject}**\n\n${text}`,
    tags: [['h', channel], ['subject', subject], ['mail-original', 'https://blossom.invalid/demo', 'AAAA', 'AAAA'], ['mail-from', from], ['mail-name', name],
      ['message-id', `<${crypto.randomUUID()}@example.org>`], ['belfry-auth', 'pass'], ['client', 'belfry-mail']],
  }, hexToBytes(DEMO_GROUPS))])
}
