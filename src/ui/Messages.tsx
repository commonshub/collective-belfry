import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { MailIcon } from './icons'
import { address, attachmentsOf, checkReports, convFor, convName, isMail, isNewMail, mailAddress, markRead, peers, queueState, report, retryDm, sendDm, thread, threadName, threads, type Conv, type Dm } from '../core/dm'
import { displayName, profiles } from '../core/model'
import { useVersion } from '../core/store'
import { me } from '../core/sync'
import { BELFRY } from '../config'
import { Avatar, back, fmtDay, fmtTime, go, Sheet } from './common'

// Direct messages and e-mail in one place (#/dm, #/dm/<conv>): members by NIP-17, anyone else
// by e-mail through the belfry. Your address here is name@domain; mail to it lands here too.

const enc = (c: Conv) => encodeURIComponent(c)

function Who({ conv, size = 40 }: { conv: Conv; size?: number }) {
  if (!isMail(conv)) return <Avatar pubkey={peers(conv)[0]} size={size} />
  return <span className="mail-avatar" style={{ width: size, height: size }} aria-hidden="true"><MailIcon /></span>
}

export function MessagesList() {
  useVersion()
  const [creating, setCreating] = useState(false)
  const list = threads()
  return (
    <div className="screen">
      <div className="topbar">
        <button className="icon-btn" aria-label="Back" onClick={() => back('channels')}>‹</button>
        <h1>Messages{address() && <small>Your address: {address()}</small>}</h1>
        <button className="icon-btn" aria-label="New message" onClick={() => setCreating(true)}>＋</button>
      </div>
      <div className="scroll">
        {list.length === 0 && (
          <div className="empty">
            No messages yet.<br />
            Members can write to you here, and anyone can e-mail you at <b>{address() || `you@${BELFRY.domain}`}</b>.
          </div>
        )}
        {list.map((t) => (
          <button key={t.conv} className="chan dm-row" onClick={() => go('dm/' + enc(t.conv))}>
            <Who conv={t.conv} size={36} />
            <span className="t">
              <b>{isMail(t.conv) ? threadName(t.conv) : convName(t.conv)}</b>
              <small>{isMail(t.conv) ? `${convName(t.conv)} · ` : ''}{t.last.mine ? 'You: ' : ''}{t.last.text.split('\n')[0]}</small>
            </span>
            <span className="dm-meta">
              <time>{fmtTime(t.last.at)}</time>
              {t.unread > 0 && <span className="badge lime">{t.unread}</span>}
            </span>
          </button>
        ))}
        <div style={{ padding: 16 }}>
          <button className="btn ghost" onClick={() => setCreating(true)}>＋ New message</button>
        </div>
      </div>
      {creating && <NewMessage onClose={() => setCreating(false)} />}
    </div>
  )
}

function NewMessage({ onClose }: { onClose: () => void }) {
  const [to, setTo] = useState('')
  const [error, setError] = useState('')
  const people = useMemo(() => [...profiles().entries()].filter(([pk, p]) => pk !== me() && (p.name || p.nip05))
    .map(([pubkey, p]) => ({ pubkey, name: p.name ?? '', nip05: p.nip05 })), [])
  const q = to.trim().toLowerCase()
  const matches = q ? people.filter((p) => p.name.toLowerCase().includes(q) || p.nip05?.toLowerCase().includes(q)).slice(0, 8) : people.slice(0, 8)
  const open = (c: Conv) => { onClose(); go('dm/' + enc(c)) }
  return (
    <Sheet title="New message" onClose={onClose}>
      <form onSubmit={async (e) => {
        e.preventDefault()
        const c = await convFor(to, people)
        if (c) open(c)
        else setError('Type a member’s name, or an e-mail address.')
      }}>
        <label className="field"><span>To</span>
          <input className="input" autoFocus placeholder="A member, or an e-mail address" value={to}
            onChange={(e) => { setTo(e.target.value); setError('') }} autoCapitalize="off" autoCorrect="off" /></label>
        {error && <div className="error">{error}</div>}
        <div className="dm-picks">
          {matches.map((p) => (
            <button type="button" key={p.pubkey} className="chan" onClick={() => open('n:' + p.pubkey)}>
              <Avatar pubkey={p.pubkey} size={32} />
              <span className="t"><b>{p.name || p.nip05}</b><small>{p.nip05}</small></span>
            </button>
          ))}
        </div>
        <button className="btn" disabled={!q}>{/@/.test(q) && !q.endsWith('@' + BELFRY.domain) ? 'Write an e-mail' : 'Write'}</button>
      </form>
    </Sheet>
  )
}

export function MessageThread({ conv }: { conv: Conv }) {
  useVersion()
  const list = thread(conv)
  const mail = isMail(conv)
  const scroller = useRef<HTMLDivElement>(null)
  useEffect(() => { markRead(conv) }, [conv, list.length])
  useEffect(() => { if (mail) void checkReports() }, [conv])
  useLayoutEffect(() => { const el = scroller.current; if (el) el.scrollTop = el.scrollHeight }, [list.length])
  const addr = mail ? mailAddress(conv) : ''
  const who = mail ? convName(conv) : ''
  const sub = mail ? (who !== addr ? `${who} · ${addr}` : `E-mail · ${addr}`) : peers(conv).map((p) => profiles().get(p)?.nip05).filter(Boolean).join(', ')
  const title = mail ? (isNewMail(conv) ? `New e-mail to ${who}` : threadName(conv)) : convName(conv)
  let day = ''
  return (
    <div className="screen">
      <div className="topbar">
        <button className="icon-btn" aria-label="Messages" onClick={() => go('dm')}>‹</button>
        <Who conv={conv} size={30} />
        <h1>{title}{sub && <small>{sub}</small>}</h1>
        {mail && !isNewMail(conv) && <button className="icon-btn" aria-label={`New e-mail to ${who}`} title="New e-mail (a new thread)" onClick={() => go('dm/' + enc('m:' + addr))}>＋</button>}
      </div>
      <div className="scroll" ref={scroller}>
        <div className="messages dm-list">
          {list.length === 0 && <div className="empty">{mail ? `A new e-mail to ${addr}: its subject names the thread. It goes out from ${address() || 'your address here'}; answers land in the thread.` : 'No messages yet. Only you two can read them.'}</div>}
          {list.map((d) => {
            const dd = fmtDay(new Date(d.at * 1000))
            const sep = dd !== day ? (day = dd) : ''
            return <div key={d.rid}>{sep && <div className="day">{sep}</div>}<Bubble d={d} /></div>
          })}
        </div>
      </div>
      <DmComposer conv={conv} needsSubject={isNewMail(conv)} />
    </div>
  )
}

function Bubble({ d }: { d: Dm }) {
  const r = d.mine ? report(d.emailId) : undefined
  return (
    <div className={'dm' + (d.mine ? ' mine' : '') + (d.pending ? ' pending' : '')}>
      {!d.mine && d.kind !== 1301 && peers(d.conv).length > 1 && <div className="dm-who">{displayName(d.from)}</div>}
      {d.kind === 1301 && d.subject && <div className="dm-subject">{d.subject}</div>}
      <div className="dm-text">{d.text || (d.attachments?.length ? '' : '(empty)')}</div>
      {d.attachments && d.attachments.length > 0 && <Attachments d={d} />}
      <div className="dm-foot">
        <time>{fmtTime(d.at)}</time>
        {d.kind === 1301 && !d.mine && !d.verified && <span className="dm-warn" title="The sender's provider did not prove this address (no DKIM or SPF)">unverified sender</span>}
        {d.mine && <DmState d={d} r={r} />}
      </div>
    </div>
  )
}

/** Where my message stands: saved here and waiting for the connection, out, delivered, refused. */
function DmState({ d, r }: { d: Dm; r?: ReturnType<typeof report> }) {
  const q = queueState(d)
  if (d.failed) return <span className="dm-state failed">Not sent: {d.failed} · <button className="link" onClick={() => void retryDm(d)}>Retry</button></span>
  if (d.pending || q.waiting)
    return <span className="dm-state queued" title={q.error}>{q.error || !navigator.onLine ? 'Saved · waiting for the connection' : 'Sending…'}</span>
  if (d.kind !== 1301) return null
  const label = r?.status === 'delivered' ? 'Delivered' : r?.status === 'failed' ? `Not delivered: ${r.details}` : r?.status === 'delayed' ? 'Delayed, retrying' : r?.details || 'Sent'
  return <span className={'dm-state ' + (r?.status ?? 'queued')}>{label}</span>
}

function Attachments({ d }: { d: Dm }) {
  const [busy, setBusy] = useState(-1)
  const open = async (i: number) => {
    setBusy(i)
    try {
      const a = (await attachmentsOf(d))[i]
      if (!a) return
      const url = URL.createObjectURL(new Blob([a.data as BlobPart], { type: a.type }))
      const link = Object.assign(document.createElement('a'), { href: url, download: a.name })
      link.click()
      setTimeout(() => URL.revokeObjectURL(url), 60_000)
    } finally { setBusy(-1) }
  }
  return (
    <div className="dm-files">
      {d.attachments!.map((a, i) => (
        <button key={i} className="dm-file" onClick={() => void open(i)}>📎 {a.name} <small>{busy === i ? '…' : kb(a.size)}</small></button>
      ))}
    </div>
  )
}

const kb = (n: number) => (n > 1e6 ? `${(n / 1e6).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1e3))} kB`)

function DmComposer({ conv, needsSubject }: { conv: Conv; needsSubject: boolean }) {
  const [text, setText] = useState('')
  const [subject, setSubject] = useState('')
  const [error, setError] = useState('')
  const ta = useRef<HTMLTextAreaElement>(null)
  useLayoutEffect(() => {
    const el = ta.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = Math.min(el.scrollHeight, 160) + 'px'
  }, [text])
  const send = async () => {
    const t = text.trim()
    if (!t) return
    setText(''); setError('')
    if (needsSubject && !subject.trim()) { setText(t); setError('Give it a subject: it names the thread.'); return }
    try {
      const target = await sendDm(conv, t, needsSubject ? subject : undefined)
      setSubject('')
      if (target && target !== conv) go('dm/' + enc(target)) // a new e-mail: its thread
    } catch (e) {
      setText(t); setError(e instanceof Error ? e.message : String(e))
    }
  }
  return (
    <div className="composer">
      {error && <div className="error" style={{ margin: '0 4px 6px' }}>{error}</div>}
      {needsSubject && <input className="input dm-subject-input" placeholder="Subject" value={subject} onChange={(e) => setSubject(e.target.value)} />}
      <div className="bar">
        <textarea ref={ta} rows={1} placeholder={isMail(conv) ? 'Write an e-mail' : 'Message'} value={text} onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey && matchMedia('(pointer: fine)').matches) { e.preventDefault(); void send() } }} />
        <button className="send" aria-label="Send" disabled={!text.trim()} onClick={() => void send()}>↑</button>
      </div>
    </div>
  )
}
