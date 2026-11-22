import { Fragment, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type React from 'react'
import { openProfile } from './Profile'
import { MailIcon } from './icons'
import { Markdown } from './markdown'
import { RefText } from './Topics'
import { RefPreview } from './Refs'
import { endRef } from '../core/refs'
import { cancelHeld, heldFailure, heldFor, heldSince, isHeld, address, attachmentsOf, dmReactions, reactDm, baseSubject, checkReports, convFor, LookupFailed, convName, isMail, isNewMail, mailAddress, markRead, peers, queueState, report, retryDm, sendDm, thread, threadName, threads, type Conv, type Dm } from '../core/dm'
import { previewOf, sendDmFiles } from '../core/dm'
import { MAX_FILE, MAX_FILE_GUEST, bubbles, tooLarge } from '../core/dmfiles'
import { isGuest } from '../core/invite'
import { AttachSheet, DmRecordSheet, FilesBody } from './DmFiles'
import { EmojiSheet, ReactionChips } from './Reactions'
import { displayName, plainRefs, profiles, type Media } from '../core/model'
import { isPicture, pictureType } from '../core/pictures'
import { useVersion } from '../core/store'
import { me } from '../core/sync'
import { useVisible } from '../core/unread'
import { BELFRY } from '../config'
import { refreshChains } from '../core/sealedrt'
import { LOCKED_LINE, outOfHead, waitingForSibling, notSealed, anyLockedHere, deviceLines, isSealed, lockedHere, lockedMsg, sealedAt, sharedBy, type DeviceLine } from '../core/sealed'
import { DeviceLineRow, LockButton, LockedBanner, LockedBlock, LockedHereText, NotSealedMark, ShareHistorySheet, SharedByLine } from './Sealed'
import { Avatar, up, fmtDay, fmtTime, go, Sheet, Form, PersonName, useNow, Pictures, PictureRow } from './common'
import { SendingLine } from './Sending'
import { TypingDots, TypingLine } from './Typing'
import { cleared, messageSent, onTyping, typed, typingIn, type Place } from '../core/typing'

// Direct messages and e-mail in one place (#/dm, #/dm/<conv>): members by NIP-17, anyone else
// by e-mail through the belfry. Your address here is name@domain; mail to it lands here too.

const enc = (c: Conv) => encodeURIComponent(c)

function Who({ conv, size = 40 }: { conv: Conv; size?: number }) {
  if (!isMail(conv)) return <Avatar pubkey={peers(conv)[0]} size={size} />
  return <span className="mail-avatar" style={{ width: size, height: size }} aria-hidden="true"><MailIcon /></span>
}

/** Someone else types in CONV now (the list's "typing…", canvas messages/typing-list). */
function typingNow(conv: Conv): boolean {
  if (isMail(conv) || isSealed(conv)) return false // none in a sealed conversation for now (docs/typing.md)
  const p = peers(conv).filter((k) => k !== me())
  return p.length > 0 && typingIn({ peers: p }, (pk) => thread(conv).reduce((m, d) => (d.from === pk && d.at > m ? d.at : m), 0)).length > 0
}

export function MessagesList() {
  useVersion()
  const [, tick] = useState(0)
  useEffect(() => onTyping(() => tick((n) => n + 1)), [])
  const [creating, setCreating] = useState(false)
  const list = threads()
  return (
    <div className="screen">
      <div className="topbar">
        <button className="icon-btn" aria-label="Back" onClick={() => up('c')}>‹</button>
        <h1>Messages{address() && <small>{address()}</small>}</h1>
        <button className="icon-btn" aria-label="New message" onClick={() => setCreating(true)}>＋</button>
      </div>
      <div className="scroll">
        {anyLockedHere() && <LockedBanner />}
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
              {lockedHere(t.conv) ? <small><LockedHereText /></small>
                : typingNow(t.conv) ? <small className="typing-row"><TypingDots />typing…</small>
                : <small>{isMail(t.conv) ? `${convName(t.conv)} · ` : ''}{t.last.mine ? 'You: ' : ''}{plainRefs(previewOf(t.last)).split('\n')[0]}</small>}
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
      <Form onSubmit={async (e) => {
        e.preventDefault()
        let c: Conv | null
        try { c = await convFor(to, people) } catch (err) { setError(err instanceof LookupFailed ? err.message : 'Something went wrong. Try again.'); return }
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
      </Form>
    </Sheet>
  )
}

export function MessageThread({ conv }: { conv: Conv }) {
  useVersion()
  // boards messages/encryption-locked-thread (v84): a device not yet allowed reads no sealed
  // message; what came before the conversation was sealed reads as usual, then the amber block
  const locked = lockedHere(conv)
  const list = thread(conv).filter((d) => !lockedMsg(conv, d.at))
  const mail = isMail(conv)
  const scroller = useRef<HTMLDivElement>(null)
  const visible = useVisible()
  useEffect(() => { if (visible) markRead(conv) }, [conv, list.length, visible])
  useEffect(() => { if (mail) void checkReports() }, [conv])
  useEffect(() => { void refreshChains(conv) }, [conv]) // §4: a sealed conversation opens with fresh heads
  useLayoutEffect(() => { const el = scroller.current; if (el) el.scrollTop = el.scrollHeight }, [list.length])
  const addr = mail ? mailAddress(conv) : ''
  const who = mail ? convName(conv) : ''
  const sub = mail ? (who !== addr ? `${who} · ${addr}` : `E-mail · ${addr}`) : peers(conv).map((p) => profiles().get(p)?.nip05).filter(Boolean).join(', ')
  const title = mail ? (isNewMail(conv) ? `New e-mail to ${who}` : threadName(conv)) : convName(conv)
  const sealed = isSealed(conv)
  // the device lines between the messages, by time (core/sealed.ts; none unless sealed)
  const lines = sealed && !locked ? deviceLines(conv) : []
  // pictures sent together and their caption are one bubble (dmfiles.ts bubbles)
  const bs = bubbles(list.map((d) => ({ id: d.rid, kind: d.kind, pubkey: d.from, created_at: d.at, group: d.group, d })))
    .map((b) => ({ d: b.files[0]?.d ?? b.caption!.d, files: b.files.map((f) => f.d), caption: b.caption?.d, t: b.at * 1000 }))
  const rows: ({ d: Dm; files: Dm[]; caption?: Dm } | { l: DeviceLine })[] = [...bs, ...lines.map((l) => ({ l, t: l.at }))].sort((a, b) => a.t - b.t)
  const [sharing, setSharing] = useState<DeviceLine | null>(null)
  const onShare = (l: DeviceLine) => setSharing(l)
  let day = ''
  let shareRun: string | undefined
  return (
    <div className="screen">
      <div className="topbar">
        <button className="icon-btn" aria-label="Back to Messages" onClick={() => up('dm')}>‹</button>
        {!mail && peers(conv).length === 1
          ? <PersonHead pubkey={peers(conv)[0]} sealed={sealed ? conv : undefined} />
          : <><Who conv={conv} size={30} /><h1><span className="seal-title">{title}{sealed && <LockButton conv={conv} />}</span>{sub && <small>{sub}</small>}</h1></>}
        {mail && !isNewMail(conv) && <button className="icon-btn" aria-label={`New e-mail to ${who}`} title="New e-mail (a new thread)" onClick={() => go('dm/' + enc('m:' + addr))}>＋</button>}
      </div>
      <div className="scroll" ref={scroller}>
        <div className="messages dm-list">
          {list.length === 0 && !locked && <div className="empty">{mail ? `A new e-mail to ${addr}: its subject names the thread. It goes out from ${address() || 'your address here'}; answers land in the thread.` : 'No messages yet. Only you two can read them.'}</div>}
          {rows.map((r) => {
            const t = 'd' in r ? r.d.at * 1000 : r.l.at
            const dd = fmtDay(new Date(t))
            const sep = dd !== day ? (day = dd) : ''
            if ('l' in r) { shareRun = undefined; return <div key={r.l.id} className="dm-item seal-item">{sep && <div className="day">{sep}</div>}<DeviceLineRow conv={conv} l={r.l} onShare={onShare} /></div> }
            const d = r.d
            const body = r.files.length ? <FilesItem files={r.files} caption={r.caption} /> : <DmItem d={d} head={mail ? title : ''} />
            // §8: a shared history sits in its sharer's block (a left rule), one line above it
            const by = sharedBy(d.rid)
            const opens = !!by && by !== shareRun
            shareRun = by
            const item = <div key={d.rid} className={'dm-item' + (d.mine ? ' mine' : '') + (by ? ' seal-shared' : '')}>{sep && <div className="day">{sep}</div>}{body}</div>
            return opens ? <Fragment key={d.rid}><div className="dm-item seal-item"><SharedByLine who={by} /></div>{item}</Fragment> : item
          })}
          {locked && <LockedBlock since={sealedAt(conv) || Date.now()} />}
        </div>
      </div>
      {locked || outOfHead(conv) ? <div className="seal-nocompose">{LOCKED_LINE}</div>
        : waitingForSibling(conv) ? <div className="seal-nocompose seal-waiting" role="status"><span className="spinner" aria-hidden="true" /> Waiting for another of your devices…</div>
        : <DmComposer conv={conv} needsSubject={isNewMail(conv)} />}
      {sharing && <ShareHistorySheet conv={conv} l={sharing} onClose={() => setSharing(null)} />}
    </div>
  )
}

// The thread's header already names its subject: a bubble shows one only when it differs
// (Xavier, 2026-11-11: "Don't repeat the subject of emails in each message").
const sameSubject = (a?: string, b?: string) => baseSubject(a).toLowerCase().replace(/\s+/g, ' ') === baseSubject(b).toLowerCase().replace(/\s+/g, ' ')

/** Board home/conversation (2026-11-19): the other person's face and name at the top, a tap
 *  pushes their profile. */
function PersonHead({ pubkey, sealed }: { pubkey: string; sealed?: Conv }) {
  const name = displayName(pubkey)
  const head = (
    <button className="dm-person" aria-label={`Open ${name}’s profile`} onClick={() => openProfile(pubkey)}>
      <Avatar pubkey={pubkey} size={34} />
      <span className="n"><PersonName pubkey={pubkey} /> <span className="chev" aria-hidden="true">›</span></span>
    </button>
  )
  // board messages/encryption-thread: the small lock by the name opens the Encryption screen
  return sealed ? <span className="dm-person-wrap">{head}<LockButton conv={sealed} /></span> : head
}

/** A message and, under it on its side, its reactions (the same chips as in channels). A hold or
 *  a right-click on the bubble opens the reactions row; e-mail has none. */
function DmItem({ d, head }: { d: Dm; head: string }) {
  const [picking, setPicking] = useState(false)
  const can = d.kind !== 1301 && !d.pending && !d.failed
  const t = useRef<ReturnType<typeof setTimeout> | null>(null)
  const clear = () => { if (t.current) clearTimeout(t.current); t.current = null }
  const hold = can ? {
    onPointerDown: (e: React.PointerEvent) => { if (e.pointerType === 'mouse') return; clear(); t.current = setTimeout(() => setPicking(true), 500) },
    onPointerUp: clear, onPointerLeave: clear, onPointerCancel: clear, onPointerMove: (e: React.PointerEvent) => { if (Math.abs(e.movementY) > 4) clear() },
    onContextMenu: (e: React.MouseEvent) => { if ((e.target as HTMLElement).closest('a')) return; e.preventDefault(); clear(); setPicking(true) },
  } : {}
  return (
    <>
      <Bubble d={d} head={head} hold={hold} />
      {d.kind !== 1301 && <DmReactions d={d} picking={picking} setPicking={setPicking} />}
    </>
  )
}

/** Files sent together (one group, canvas v104 messages/attach-thread): the pictures and files,
 *  the caption under them, the time and how the sending goes; reactions on the first file. */
function FilesItem({ files, caption }: { files: Dm[]; caption?: Dm }) {
  const [picking, setPicking] = useState(false)
  const all = caption ? [...files, caption] : files
  const d = files[0]
  const state = all.find((x) => x.failed) ?? all.find((x) => x.pending || queueState(x).waiting) ?? d
  return (
    <>
      <div className={'dm dm-files-bubble' + (d.mine ? ' mine' : '') + (state.pending ? ' pending' : '')}
        onContextMenu={(e) => { if (d.pending || d.failed || (e.target as HTMLElement).closest('a, button, audio')) return; e.preventDefault(); setPicking(true) }}>
        {!d.mine && peers(d.conv).length > 1 && <div className="dm-who">{displayName(d.from)}</div>}
        <FilesBody files={files} />
        {caption?.text && <RefText text={caption.text} className="dm-text" />}
        <div className="dm-foot">
          <time>{fmtTime(d.at)}</time>
          {d.mine && (state.failed
            ? <span className="dm-state failed">Not sent: {state.failed} · <button className="link" onClick={() => { for (const x of all) if (x.failed) void retryDm(x) }}>Retry</button></span>
            : <DmState d={state} />)}
        </div>
      </div>
      <DmReactions d={d} picking={picking} setPicking={setPicking} />
    </>
  )
}

function Bubble({ d, head, hold }: { d: Dm; head: string; hold: React.HTMLAttributes<HTMLDivElement> }) {
  const r = d.mine ? report(d.emailId) : undefined
  const end = d.kind === 1301 ? undefined : endRef(d.text) // a reference at the end: its preview box
  return (
    <div className={'dm' + (d.mine ? ' mine' : '') + (d.pending ? ' pending' : '')} {...hold}>
      {!d.mine && d.kind !== 1301 && peers(d.conv).length > 1 && <div className="dm-who">{displayName(d.from)}</div>}
      {d.kind === 1301 && d.subject && !sameSubject(d.subject, head) && <div className="dm-subject">{d.subject}</div>}
      {d.kind === 1301 ? (d.text ? <Markdown text={d.text} className="dm-text" /> : <div className="dm-text">{d.attachments?.length ? '' : '(empty)'}</div>)
        : end ? <>{end.rest && <RefText text={end.rest} className="dm-text" />}<RefPreview r={end.ref} /></>
        : d.text ? <RefText text={d.text} className="dm-text" /> : <div className="dm-text">(empty)</div>}
      {d.attachments && d.attachments.length > 0 && <Attachments d={d} />}
      <div className="dm-foot">
        <time>{fmtTime(d.at)}</time>
        {notSealed(d.conv, d) && <NotSealedMark />}
        {d.kind === 1301 && !d.mine && !d.verified && <span className="dm-warn" title="The sender's provider did not prove this address (no DKIM or SPF)">unverified sender</span>}
        {d.mine && <DmState d={d} r={r} />}
      </div>
    </div>
  )
}

// reactions (#issues 2026-11-15): e-mail has none, a message still in the outbox not yet
function DmReactions({ d, picking, setPicking }: { d: Dm; picking: boolean; setPicking: (on: boolean) => void }) {
  const rx = dmReactions(d.rid)
  const off = !!d.pending || !!d.failed
  return (
    <>
      <ReactionChips chips={rx.map((r) => ({ emoji: r.emoji, who: r.who, mine: !!r.mine }))} disabled={off}
        onToggle={(emoji) => { const r = rx.find((x) => x.emoji === emoji); void reactDm(d, emoji, r?.mine) }}
        onAdd={off ? undefined : () => setPicking(true)} />
      {picking && <EmojiSheet onClose={() => setPicking(false)} isOn={(e) => rx.some((r) => r.emoji === e && r.mine)}
        onPick={(e) => { const r = rx.find((x) => x.emoji === e); void reactDm(d, e, r?.mine) }} />}
    </>
  )
}

/** A message held on this device until it can be decided sealed or not (docs/sealed.md §6): after
 *  a minute it says why; Cancel takes it back into the composer. */
function HeldState({ d }: { d: Dm }) {
  const now = useNow(15_000)
  const long = now * 1000 - heldSince(d.rid) > 60_000 // useNow: seconds
  const [sent, setSent] = useState(false)
  // canvas v98 messages/encryption-held: one grey line under the faded bubble, never a technical reason
  const failed = heldFailure(d.rid), who = heldFor(d.rid)
  const line = failed ? `Not sent: ${failed}`
    : who ? `Not sent yet: ${displayName(who)}’s new device isn’t ready.`
    : long ? 'Not sent yet: this device can’t reach the belfry to check how to lock it.'
    : 'Saved · waiting for the connection'
  if (sent) return <span className="dm-state">Already sent</span>
  return (
    <span className="dm-state queued">
      {line}{' · '}
      <button className="link" onClick={async () => {
        const text = await cancelHeld(d.rid)
        if (text === null) { setSent(true); setTimeout(() => setSent(false), 2000); return } // the send won: "Already sent" for two seconds
        window.dispatchEvent(new CustomEvent('belfry-compose', { detail: { conv: d.conv, text } }))
      }}>Cancel</button>
    </span>
  )
}

/** Where my message stands: saved here and waiting for the connection, out, delivered, refused. */
function DmState({ d, r }: { d: Dm; r?: ReturnType<typeof report> }) {
  const q = queueState(d)
  if (d.failed) return <span className="dm-state failed">Not sent: {d.failed} · <button className="link" onClick={() => void retryDm(d)}>Retry</button></span>
  // out of my head: what was typed or queued before stays here, never sent, with the locked line's words
  if ((d.pending || q.waiting) && outOfHead(d.conv)) return <span className="dm-state queued">{LOCKED_LINE}</span>
  if (d.pending || q.waiting)
    return isHeld(d.rid) ? <HeldState d={d} />
      : <span title={q.error}><SendingLine className="dm-state queued" since={q.since} step={q.step} onRetry={() => void retryDm(d)} /></span>
  if (d.kind !== 1301) return null
  const label = r?.status === 'delivered' ? 'Delivered' : r?.status === 'failed' ? `Not delivered: ${r.details}` : r?.status === 'delayed' ? 'Delayed, retrying' : r?.details || 'Sent'
  return <span className={'dm-state ' + (r?.status ?? 'queued')}>{label}</span>
}

/** An e-mail's files (canvas v179 messages/email-pictures): its pictures in the message, after the
 *  text and in the e-mail's order (pictures put inline in an HTML mail too), read from the MIME on
 *  this device (the wrap, or the large copy decrypted from the file server, as Full e-mail does);
 *  then the other files as rows that save them. A picture that can't be read: its file row. */
function Attachments({ d }: { d: Dm }) {
  const [busy, setBusy] = useState(-1)
  const list = d.attachments!
  const picIdx = list.flatMap((a, i) => (isPicture(a.type, a.name) ? [i] : []))
  const [urls, setUrls] = useState<Record<number, string> | null>(null)
  const [unread, setUnread] = useState(false)
  useEffect(() => {
    if (!picIdx.length) return
    let on = true
    const made: string[] = []
    attachmentsOf(d).then((parts) => {
      if (!on) return
      const got: Record<number, string> = {}
      for (const i of picIdx) {
        const a = parts[i]
        if (!a?.data.length) continue
        const u = URL.createObjectURL(new Blob([a.data as BlobPart], { type: pictureType(a.type, a.name) }))
        made.push(u); got[i] = u
      }
      setUrls(got)
    }, () => on && setUnread(true))
    return () => { on = false; made.forEach((u) => URL.revokeObjectURL(u)) }
  }, [d.rid]) // eslint-disable-line react-hooks/exhaustive-deps
  const open = async (i: number) => {
    setBusy(i)
    try {
      const a = (await attachmentsOf(d))[i]
      if (!a) return
      const url = URL.createObjectURL(new Blob([a.data as BlobPart], { type: a.type }))
      const link = Object.assign(document.createElement('a'), { href: url, download: a.name })
      link.click()
      setTimeout(() => URL.revokeObjectURL(url), 60_000)
    } catch { /* the row stays; tapped again, tried again */ } finally { setBusy(-1) }
  }
  const asMedia = (i: number, url: string): Media => ({ url, mime: pictureType(list[i].type, list[i].name), name: list[i].name, size: list[i].size })
  const shown = urls ? picIdx.filter((i) => urls[i]) : []
  const missing = unread ? picIdx : urls ? picIdx.filter((i) => !urls[i]) : []
  const at = (m: Media) => shown.find((i) => urls![i] === m.url) ?? -1
  return (
    <div className="dm-files">
      {picIdx.length > 0 && !urls && !unread && <div className="dm-pic one" style={{ aspectRatio: '4 / 3' }} aria-label="Opening the pictures"><span className="dm-pic-wait" aria-hidden="true" /></div>}
      <Pictures pics={shown.map((i) => asMedia(i, urls![i]))} onSave={(m) => { const i = at(m); if (i >= 0) void open(i) }} />
      {missing.map((i) => <PictureRow key={i} m={asMedia(i, '')} busy={busy === i} onSave={() => void open(i)} />)}
      {list.map((a, i) => picIdx.includes(i) ? null : (
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
  const [files, setFiles] = useState<File[]>([])
  const [menu, setMenu] = useState(false)
  const [recording, setRecording] = useState(false)
  const [busy, setBusy] = useState(false)
  const ta = useRef<HTMLTextAreaElement>(null)
  // a held message cancelled: its text back here
  useEffect(() => {
    const on = (e: Event) => { const d = (e as CustomEvent<{ conv: string; text: string }>).detail; if (d.conv === conv && d.text) { setText(d.text); ta.current?.focus() } }
    window.addEventListener('belfry-compose', on)
    return () => window.removeEventListener('belfry-compose', on)
  }, [conv])
  const canFiles = !isMail(conv)
  // typing indicators (docs/typing.md): Nostr conversations with someone else, not e-mail
  // none in a sealed conversation for now (docs/typing.md): nothing sent, nothing shown
  const tpeers = useMemo<Place | null>(() => { const p = peers(conv).filter((k) => k !== me()); return p.length ? { peers: p } : null }, [conv])
  const tplace = isSealed(conv) ? null : tpeers
  const previews = useMemo(() => files.map((f) => (f.type.startsWith('image/') ? URL.createObjectURL(f) : '')), [files])
  useEffect(() => () => previews.forEach((u) => u && URL.revokeObjectURL(u)), [previews])
  useLayoutEffect(() => {
    const el = ta.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = Math.min(el.scrollHeight, 160) + 'px'
  }, [text])
  const add = (more: File[]) => {
    const max = isGuest(me()) ? MAX_FILE_GUEST : MAX_FILE
    if (more.some((f) => f.size > max)) { setError(tooLarge(max)); more = more.filter((f) => f.size <= max) }
    else setError('')
    if (more.length) setFiles((now) => [...now, ...more])
  }
  const send = async () => {
    const t = text.trim()
    if (busy || (!t && !files.length)) return
    setError('')
    if (tplace) messageSent(tplace)
    if (files.length) {
      // pictures and files, the text as their caption (canvas v104)
      setBusy(true)
      const sending = files
      setFiles([]); setText('')
      try { await sendDmFiles(conv, sending.map((file) => ({ file })), t) } catch (e) { setFiles(sending); setText(t); setError(e instanceof Error ? e.message : String(e)) } finally { setBusy(false) }
      return
    }
    setText('')
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
    <>
    {tplace && <TypingLine place={tplace} />}
    <div className="composer" onDragOver={(e) => { if (canFiles && e.dataTransfer.types.includes('Files')) e.preventDefault() }}
      onDrop={(e) => { if (!canFiles || !e.dataTransfer.files.length) return; e.preventDefault(); add([...e.dataTransfer.files]) }}>
      {error && <div className="error" style={{ margin: '0 4px 6px' }}>{error}</div>}
      {needsSubject && <input className="input dm-subject-input" placeholder="Subject" value={subject} onChange={(e) => setSubject(e.target.value)} />}
      {files.length > 0 && (
        <div className="comp-files">
          {files.map((f, i) => (
            <span key={i} className="comp-file">
              {previews[i] ? <img src={previews[i]} alt="" /> : <span className="dm-filebadge">{(f.name.split('.').pop() || 'file').slice(0, 4).toUpperCase()}</span>}
              <button type="button" aria-label={`Remove ${f.name}`} onClick={() => setFiles(files.filter((_, j) => j !== i))}>×</button>
            </span>
          ))}
        </div>
      )}
      <div className="bar">
        {canFiles && <button type="button" className="plus" aria-label="Add a picture, a file or a recording" onClick={() => setMenu(true)}>＋</button>}
        <textarea ref={ta} rows={1} placeholder={isMail(conv) ? (needsSubject ? 'Write an e-mail' : 'Reply by e-mail') : files.length ? 'Add a caption' : 'Message'} value={text}
          onChange={(e) => { const v = e.target.value; setText(v); if (tplace) { if (v.trim()) typed(tplace, v); else cleared(tplace) } }}
          onPaste={(e) => { const pasted = canFiles ? [...e.clipboardData.files] : []; if (!pasted.length) return; if (!e.clipboardData.getData('text/plain')) e.preventDefault(); add(pasted) }}
          onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey && matchMedia('(pointer: fine)').matches) { e.preventDefault(); void send() } }} />
        <button className="send" aria-label="Send" aria-busy={busy || undefined} disabled={busy || (!text.trim() && !files.length)} onClick={() => void send()}>↑</button>
      </div>
      {menu && <AttachSheet onFiles={add} onRecord={() => setRecording(true)} onClose={() => setMenu(false)} />}
      {recording && <DmRecordSheet conv={conv} onClose={() => setRecording(false)} onSend={(file, seconds) => sendDmFiles(conv, [{ file, duration: seconds }])} />}
    </div>
    </>
  )
}
