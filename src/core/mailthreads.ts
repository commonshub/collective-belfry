// E-mail threads in a shared inbox (Xavier, 2026-11-16): every e-mail from outside starts or
// continues a thread, so the channel's feed shows ONE row per conversation, not a card per mail.
//
// Nothing new on the wire: the bridge still posts each e-mail as a kind 9 by the groups key
// (`client belfry-mail`, `mail-from`, `message-id`, `in-reply-to`, `references` since 2026-11-16)
// and what went out as a kind 9 `client belfry-mail-out` with `e` = the e-mail it answers. They
// are joined here, on every device, the way the Messages list joins e-mail threads (dm.ts):
// - an e-mail naming (In-Reply-To / References) one already in a thread joins that thread;
// - else the same sender with the same subject (Re:, Fwd: … aside) joins theirs;
// - else it starts one, and is its root (the id the thread's route, labels and NIP-22 replies use);
// - what went out joins the thread of the e-mail it answers.
// The team's talk is ordinary NIP-22 replies (kind 1111) under any e-mail of the thread (older
// clients hung them under the e-mail they were on).

import { all, type Ev } from './store'
import { me } from './sync'
import { deleted, INBOX_NS, messages, threadsOf, type CardState, type Message } from './model'
import { baseSubject, msgIds } from './dm'

const tag = (e: Ev, k: string) => e.tags.find((t) => t[0] === k)?.[1]

export interface MailItem {
  m: Message
  dir: 'in' | 'out'
  /** in: the sender's address; out: the address it went to */
  address: string
  /** in: the sender's name as their mail gave it */
  name: string
  subject: string
  messageId?: string
  /** in: DKIM or SPF aligned with the From domain */
  verified: boolean
  /** out: the member who sent it */
  by?: string
  /** the whole e-mail on Blossom (in only) */
  original?: { url: string; key: string; nonce: string }
}

export interface MailThread {
  /** the first e-mail's event id: the thread's id everywhere (route, labels, replies) */
  id: string
  mails: MailItem[]
  /** every event id of its e-mails */
  ids: Set<string>
  address: string
  name: string
  subject: string
  /** the latest e-mail received (what a reply answers) */
  lastIn?: MailItem
  /** latest activity: an e-mail either way or a team reply */
  last: number
  replies: number
  /** the team members who replied in it */
  who: string[]
  state: CardState
  /** every Message-ID in it, oldest first (References of an answer) */
  refs: string[]
}

/** An e-mail from outside, if KEY (the groups key) posted it. */
export function mailIn(m: Message, key?: string): MailItem | null {
  const e = m.ev
  if (!key || e.pubkey !== key || !tag(e, 'mail-from') || !e.tags.some((t) => t[0] === 'client' && t[1] === 'belfry-mail')) return null
  const o = e.tags.find((t) => t[0] === 'mail-original' && t[1]?.startsWith('https://') && t[2] && t[3])
  return {
    m, dir: 'in', address: (tag(e, 'mail-from') ?? '').toLowerCase(), name: tag(e, 'mail-name') ?? '', subject: tag(e, 'subject') ?? '',
    messageId: tag(e, 'message-id') || undefined, verified: tag(e, 'belfry-auth') === 'pass',
    original: o ? { url: o[1], key: o[2], nonce: o[3] } : undefined,
  }
}

/** What went out as the channel (only the bridge posts these, with the groups key). */
export function mailOut(m: Message, key?: string): MailItem | null {
  const e = m.ev
  if (!key || e.pubkey !== key || !e.tags.some((t) => t[0] === 'client' && t[1] === 'belfry-mail-out')) return null
  return { m, dir: 'out', address: tag(e, 'mail-to') ?? '', name: '', subject: tag(e, 'subject') ?? '', messageId: tag(e, 'message-id') || undefined, verified: true, by: tag(e, 'sent-by') || undefined }
}

const subjectKey = (address: string, s: string) => address.toLowerCase() + '|' + baseSubject(s).toLowerCase().replace(/\s+/g, ' ')

/** GROUP's e-mail threads (KEY = the groups key), oldest first, and which thread each e-mail is in. */
export function mailThreads(groupId: string, key?: string): { list: MailThread[]; of: Map<string, MailThread> } {
  const list: MailThread[] = []
  const of = new Map<string, MailThread>()
  if (!key) return { list, of }
  const byMsgId = new Map<string, MailThread>()
  const bySubject = new Map<string, MailThread>()
  const add = (t: MailThread, it: MailItem) => {
    t.mails.push(it)
    t.ids.add(it.m.ev.id)
    of.set(it.m.ev.id, t)
    if (it.messageId) { byMsgId.set(it.messageId, t); if (!t.refs.includes(it.messageId)) t.refs.push(it.messageId) }
    if (it.dir === 'in') { t.lastIn = it; bySubject.set(subjectKey(it.address, it.subject), t); if (!t.name && it.name) t.name = it.name }
    t.last = Math.max(t.last, it.m.ev.created_at)
  }
  const start = (it: MailItem): MailThread => {
    const t: MailThread = { id: it.m.ev.id, mails: [], ids: new Set(), address: it.address, name: it.name, subject: baseSubject(it.subject) || it.subject, last: 0, replies: 0, who: [], state: { status: 'open', answered: 0 }, refs: [] }
    list.push(t)
    return t
  }
  for (const m of messages(groupId)) { // oldest first
    const got = mailIn(m, key) ?? mailOut(m, key)
    if (!got) continue
    let t: MailThread | undefined
    if (got.dir === 'in') {
      const refs = [...msgIds(tag(m.ev, 'references')), ...msgIds(tag(m.ev, 'in-reply-to'))]
      for (const r of refs.reverse()) { t = byMsgId.get(r); if (t) break }
      t ??= bySubject.get(subjectKey(got.address, got.subject))
    } else {
      const answered = m.ev.tags.find((x) => x[0] === 'e')?.[1]
      t = answered ? of.get(answered) : undefined
    }
    add(t ?? start(got), got)
  }
  if (!list.length) return { list, of }
  // the team's replies, under any of the thread's e-mails
  const th = threadsOf(groupId)
  for (const t of list) for (const id of t.ids) {
    const r = th.get(id)
    if (!r) continue
    t.replies += r.count
    t.last = Math.max(t.last, r.last)
    for (const w of r.who) if (!t.who.includes(w)) t.who.push(w)
  }
  // state: Open → Answered once something went out → Open again when they write back; Closed
  // and who has it are NIP-32 labels (belfry/inbox) on any of its e-mails, latest wins
  const evs = all(me())
  const del = deleted(evs)
  const labels = evs.filter((e) => e.kind === 1985 && tag(e, 'h') === groupId && !del.has(e.id) && e.tags.some((x) => x[0] === 'L' && x[1] === INBOX_NS))
  for (const t of list) {
    type Step = { at: number; id: string; label?: Ev; mail?: MailItem }
    const steps: Step[] = [
      ...t.mails.map((mail): Step => ({ at: mail.m.ev.created_at, id: mail.m.ev.id, mail })),
      ...labels.filter((l) => t.ids.has(tag(l, 'e') ?? '')).map((label): Step => ({ at: label.created_at, id: label.id, label })),
    ].sort((a, b) => a.at - b.at || (a.id < b.id ? -1 : 1))
    const s: CardState = { status: 'open', answered: 0 }
    for (const x of steps) {
      if (x.label) {
        const l = x.label.tags.find((y) => y[0] === 'l' && y[2] === INBOX_NS)?.[1]
        if (l === 'open' || l === 'closed') s.status = l
        if (l === 'assigned') s.assignee = x.label.tags.find((y) => y[0] === 'p')?.[1] || undefined
      } else if (x.mail!.dir === 'out') {
        s.answered++
        if (s.status === 'open') s.status = 'waiting'
      } else if (x.mail!.m.ev.id !== t.id) s.status = 'open' // they wrote back
    }
    t.state = s
  }
  return { list, of }
}
