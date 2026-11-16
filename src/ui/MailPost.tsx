import { useEffect, useRef, useState, type ReactNode } from 'react'
import { finalizeEvent, generateSecretKey, type NostrEvent } from 'nostr-tools'
import { addEvents, all } from '../core/store'
import { isLocalOnly, me } from '../core/sync'
import { baseSubject, channelSignature, DEMO_GROUPS, fetchLarge, report, sendAsChannel } from '../core/dm'
import { hexToBytes } from '@noble/hashes/utils'
import { assignCard, createChannel, setCardStatus, setInbox } from '../core/actions'
import { displayName, plainRefs, type CardState, type Group } from '../core/model'
import { BELFRY } from '../config'
import { parseMail, type Mail } from '../core/mime'
import { mailIn, mailOut, type MailItem, type MailThread } from '../core/mailthreads'
import { fmtTime, MediaGrid, Sheet, Btn } from './common'

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

/** On the time line of a member's message that came by e-mail: small, like the time (Xavier,
 *  2026-11-16). The whole e-mail is in the message's menu. */
export function ViaMail() {
  return <span className="via-mail" title="Sent by e-mail">via e-mail</span>
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

/** Demo mode only (tests): a member's e-mail lands in CHANNEL, as the bridge would post it.
 *  ROOT: it answered something in the channel, so the bridge put it in that thread (kind 1111). */
export function demoMailPost(channel: string, name: string, subject: string, text: string, root?: string) {
  if (!isLocalOnly()) return
  const sk = generateSecretKey()
  const under = root ? all(me()).find((e) => e.id === root) : undefined
  addEvents([
    finalizeEvent({ kind: 0, created_at: Math.floor(Date.now() / 1000) - 60, tags: [], content: JSON.stringify({ name }) }, sk),
    finalizeEvent({
      kind: under ? 1111 : 9, created_at: Math.floor(Date.now() / 1000), content: `**${subject}**\n\n${text}`,
      tags: [['h', channel], ...(under ? [['E', under.id, '', under.pubkey], ['K', String(under.kind)], ['P', under.pubkey], ['e', under.id, '', under.pubkey], ['k', String(under.kind)], ['p', under.pubkey]] : []),
        ['subject', subject], ['mail-original', 'https://blossom.invalid/demo', 'AAAA', 'AAAA'], ['message-id', `<${crypto.randomUUID()}@example.org>`], ['client', 'belfry-mail']],
    }, sk),
  ])
}

/** Demo mode only (tests): a channel NAME of my own with its shared inbox on; its id. */
export async function demoInboxChannel(name: string): Promise<string | undefined> {
  if (!isLocalOnly()) return
  const id = await createChannel(name, '')
  await setInbox(id, true)
  return id
}

// ---- shared inboxes: e-mail threads (core/mailthreads.ts) ---------------------------------
// One row per e-mail thread in the channel; in the thread, each e-mail in its own frame
// (received / sent), the team's replies between them, and one composer that says who gets it.

/** Still used by the message menu (no editing an e-mail): an e-mail from outside, as before. */
export function cardOf(ev: NostrEvent, key?: string) {
  return mailIn({ ev, text: ev.content, media: [], topics: [] }, key)
}
export function sentOf(ev: NostrEvent, key?: string) {
  return mailOut({ ev, text: ev.content, media: [], topics: [] }, key)
}

const Envelope = ({ size = 14 }: { size?: number }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><rect x="3" y="5" width="18" height="14" rx="2" /><path d="m3 7 9 6 9-6" /></svg>
)
const STATUS: Record<CardState['status'], string> = { open: 'Open', waiting: 'Answered', closed: 'Closed' }
const UNDO_SECONDS = 8
const who = (t: { name: string; address: string }) => t.name || t.address
const first = (t: { name: string; address: string }) => t.name.split(' ')[0] || t.address
const snippet = (it: MailItem) => plainRefs(mailText(it.m.text, { subject: it.subject }) || (it.m.media.length ? 'a photo or file' : ''), '').replace(/\s+/g, ' ').trim().slice(0, 140)

/** In the channel's feed: one line per e-mail thread, where its latest activity is. */
export function MailRow({ t, open, unread, onOpen }: { t: MailThread; open: boolean; unread: number; onOpen: () => void }) {
  const latest = t.mails.at(-1)!
  const n = t.mails.length
  return (
    <button className={'mail-row' + (open ? ' open' : '')} id={'m-' + t.id} onClick={onOpen} aria-label={`E-mail from ${who(t)}: ${t.subject || '(no subject)'}`}>
      <span className="mr-ico"><Envelope size={18} /></span>
      <span className="mr-main">
        <span className="mr-top">
          <b className="mr-from">{who(t)}</b>
          <span className={'mc-pill ' + t.state.status}>{STATUS[t.state.status]}</span>
          <time>{fmtTime(t.last)}</time>
        </span>
        <span className="mr-subject">{t.subject || '(no subject)'}</span>
        <span className="mr-snip">{latest.dir === 'out' ? 'Sent: ' : ''}{snippet(latest)}</span>
        <span className="mr-meta">
          {n} {n === 1 ? 'e-mail' : 'e-mails'}{t.replies ? ` · ${t.replies} ${t.replies === 1 ? 'reply' : 'replies'} in the team` : ''}
          {t.state.assignee ? ` · ${t.state.assignee === me() ? 'you have it' : displayName(t.state.assignee) + ' has it'}` : ''}
          {unread > 0 && <span className="badge lime new-n">{unread} new</span>}
        </span>
      </span>
    </button>
  )
}

/** In the thread: one e-mail, framed (received or sent). */
export function MailFrame({ it, address }: { it: MailItem; /** the channel's own address (sent ones) */ address: string }) {
  const [full, setFull] = useState(false)
  const t = it.m.ev.created_at
  return (
    <div className={'mail-frame ' + it.dir} id={'m-' + it.m.ev.id}>
      <div className="mf-head">
        <Envelope />
        <span>{it.dir === 'in' ? 'E-mail received' : `E-mail sent by ${it.by ? displayName(it.by) : 'a member'}`}</span>
        <time>{fmtTime(t)}</time>
      </div>
      <div className="mf-body">
        <div className="mf-addr">
          {it.dir === 'in' ? <>From {it.name ? `${it.name} <${it.address}>` : it.address}</> : <>From {address} · to {it.address}</>}
          {it.dir === 'in' && !it.verified && <span className="dm-warn" title="The sender's provider did not prove this address (no DKIM or SPF)">unverified sender</span>}
        </div>
        {it.subject && <b className="mf-subject">{it.subject}</b>}
        <p className="mf-text">{mailText(it.m.text, { subject: it.subject }) || '(empty)'}</p>
        <MediaGrid media={it.m.media} />
        {it.original && <button className="link full-mail-btn" onClick={() => setFull(true)}>Full e-mail</button>}
      </div>
      {full && it.original && <FullMail mail={{ subject: it.subject, original: it.original }} onClose={() => setFull(false)} />}
    </div>
  )
}

/** Under the thread's title: who wrote, its state, who has it. */
export function MailThreadHead({ t, groupId, canAct }: { t: MailThread; groupId: string; canAct: boolean }) {
  const s = t.state
  return (
    <div className="mail-head">
      <div className="mh-from"><Envelope /> {t.name ? <>{t.name} <span className="muted">&lt;{t.address}&gt;</span></> : t.address}
        {t.lastIn && !t.lastIn.verified && <span className="dm-warn">unverified sender</span>}</div>
      <div className="mh-acts">
        <span className={'mc-pill ' + s.status}>{STATUS[s.status]}</span>
        {canAct ? <Btn className="mc-who" onClick={() => assignCard(groupId, t.id, s.assignee === me() ? '' : me())}
          title={s.assignee === me() ? 'Tap to let it go' : 'Tap to take it'}>
          {s.assignee ? (s.assignee === me() ? 'You have it' : `${displayName(s.assignee)} has it`) : 'Nobody yet · take it'}
        </Btn> : <span className="mc-who static">{s.assignee ? `${displayName(s.assignee)} has it` : 'Nobody has it yet'}</span>}
        {canAct && <Btn className="btn ghost small mh-close" onClick={() => setCardStatus(groupId, t.id, s.status === 'closed' ? 'open' : 'closed')}>{s.status === 'closed' ? 'Reopen' : 'Close'}</Btn>}
      </div>
      <div className="mh-note">Team only: {who(t)} sees only what is sent by e-mail.</div>
    </div>
  )
}

/** Rejected by a send that was undone: the composer takes the text back without an error. */
export const UNDONE = 'undone'

/** The thread's composer, for an e-mail thread: which way it goes (the thread, or by e-mail as
 *  the channel), the line that says so above the text, the 8-second Undo and the report. */
export function useMailReply({ t, g, groupId, local, canMail }: { t: MailThread; g?: Group; groupId: string; local: string; canMail: boolean }) {
  const [mode, setMode] = useState<'thread' | 'mail'>('thread')
  const [countdown, setCountdown] = useState(0)
  const [sentId, setSentId] = useState('')
  const timer = useRef<ReturnType<typeof setInterval> | null>(null)
  const pending = useRef<{ go: () => void; undo: () => void } | null>(null)
  const to = t.lastIn
  // Re: the e-mail it answers (the latest one received), as mail programs do
  const subject = `Re: ${baseSubject(to?.subject) || t.subject || '(no subject)'}`
  const address = `${local}@${BELFRY.domain}`
  const r = report(sentId)
  const mail = mode === 'mail' && canMail && !!to
  // leaving the thread during the countdown sends at once: nobody should believe it went when it didn't
  useEffect(() => () => { if (timer.current) { clearInterval(timer.current); pending.current?.go() } }, [])
  useEffect(() => { if (r?.status === 'delivered') { const x = setTimeout(() => setSentId(''), 4000); return () => clearTimeout(x) } }, [r?.status])
  useEffect(() => { if (!canMail) setMode('thread') }, [canMail])

  /** Send TEXT by e-mail after the countdown; resolves once it left this device. */
  const send = (text: string) => new Promise<void>((resolve, reject) => {
    const refs = to?.messageId ? t.refs.filter((x) => x !== to.messageId) : t.refs
    const go = () => sendAsChannel({ channel: groupId, local, channelName: g?.name ?? groupId, to: to!.address, subject, text, inReplyTo: to!.messageId, references: refs.slice(-10).join(' ') || undefined, cardId: to!.m.ev.id })
    let left = UNDO_SECONDS
    setCountdown(left)
    const stop = () => { if (timer.current) clearInterval(timer.current); timer.current = null; pending.current = null; setCountdown(0) }
    pending.current = { go: () => void go(), undo: () => { stop(); reject(new Error(UNDONE)) } }
    timer.current = setInterval(() => {
      left--
      setCountdown(left)
      if (left > 0) return
      stop()
      go().then((id) => { setSentId(id); setMode('thread'); resolve() }, reject)
    }, 1000)
  })

  const line: ReactNode = mail ? (
    <div className="send-to mail" role="status">
      <span className="st-what"><Envelope /><span>This will be sent to <b>{who(to!)}</b> by e-mail</span></span>
      <button type="button" className="link st-switch" onClick={() => setMode('thread')}>Only send in this thread instead</button>
      <small className="st-from">From {address} · {subject}</small>
    </div>
  ) : (
    <div className="send-to" role="status">
      <span className="st-what">Message will be sent in this thread</span>
      {canMail && to && <button type="button" className="link st-switch" onClick={() => setMode('mail')}>Reply by e-mail instead</button>}
    </div>
  )
  const toast: ReactNode = countdown > 0
    ? <div className="toast" role="status"><span>Sending to {first(to!)} in {countdown} s</span><button onClick={() => pending.current?.undo()}>Undo</button></div>
    : sentId ? (
      <div className="toast" role="status">
        <span>{r?.status === 'delivered' ? `E-mail sent to ${first(to!)}` : r?.status === 'failed' ? `Not delivered: ${r.details}` : r?.status === 'delayed' ? 'Delayed, the provider retries' : 'Sending…'}</span>
        {r?.status !== 'queued' && <button onClick={() => setSentId('')}>OK</button>}
      </div>
    ) : null
  return { mail, line, toast, send, sending: countdown > 0, placeholder: mail ? `E-mail to ${first(to!)}…` : 'Reply in the thread', signature: channelSignature(g?.name ?? groupId) }
}

/** Demo mode only (tests): an e-mail from outside lands in the shared inbox CHANNEL. IN_REPLY_TO:
 *  the Message-ID it answers (a follow-up). Resolves with its own Message-ID. */
export function demoInboxMail(channel: string, from: string, name: string, subject: string, text: string, inReplyTo?: string): string | undefined {
  if (!isLocalOnly()) return
  const mid = `<${crypto.randomUUID()}@example.org>`
  addEvents([finalizeEvent({
    kind: 9, created_at: Math.floor(Date.now() / 1000), content: `**${subject}**\n\n${text}`,
    tags: [['h', channel], ['subject', subject], ['mail-original', 'https://blossom.invalid/demo', 'AAAA', 'AAAA'], ['mail-from', from], ['mail-name', name],
      ['message-id', mid], ...(inReplyTo ? [['in-reply-to', inReplyTo], ['references', inReplyTo]] : []), ['belfry-auth', 'pass'], ['client', 'belfry-mail']],
  }, hexToBytes(DEMO_GROUPS))])
  return mid
}
