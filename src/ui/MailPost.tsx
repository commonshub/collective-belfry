import { useEffect, useState } from 'react'
import { finalizeEvent, generateSecretKey, type NostrEvent } from 'nostr-tools'
import { addEvents } from '../core/store'
import { isLocalOnly } from '../core/sync'
import { fetchLarge } from '../core/dm'
import { parseMail, type Mail } from '../core/mime'
import { Sheet } from './common'

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
