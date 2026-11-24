// A printed card's page (canvas "Belfry · Group cards": cards/claim-*, confirm-code, states-*):
// what chat.<domain>/g/<id> opens. Free card: "Start this group" / "Start this topic" (signed in:
// straight in; not signed in: name and e-mail, the 6-digit code, then in). A claimed card is a
// guest link: its invite takes the usual way in (App.tsx). Dead ends say why, with the way to the
// belfry's page. Nothing from the address is shown.
import { useEffect, useMemo, useRef, useState } from 'react'
import qrcode from 'qrcode-generator'
import { BELFRY, NAME } from '../config'
import { loadSession, setPendingInvite } from '../core/auth'
import {
  CardFailed, cardErrorText, cardInfo, claimConfirm, claimPoll, claimSignedIn, claimStart, claimVerify, takeConfirm, dropForm, hashtagSlug, hashtagState, typingSlug,
  cardDone, cardUrl, setCardTopic, groupCard, keepForm, keptForm, markMid, retireCard, type CardForm, type GroupCard, type CardInfo, type CardKind,
} from '../core/cards'
import type { SignedIn } from '../core/guestjoin'
import { signedFetch } from '../core/sync'
import { Btn, Form, useNow } from './common'
import { CodeBoxes } from './GuestJoin'
import { useCommunityName } from './community'
import { Logo } from './Login'

const EMPTY: CardForm = { title: '', about: '', hashtag: '', name: '', email: '' }

/** The card ID. SIGNED_IN: the account's name ("Signed in as …"), or undefined for a visitor.
 *  ON_IN: a visitor's right code (the session to enter). ON_INVITE: the card's guest link is the
 *  pending invite now (App takes it from there). ON_CLOSE: leave the card page. */
export function CardScreen({ id, signedIn, signedEmail, onSignOut, onIn, onInvite, onStarted, onClose }: {
  id: string; signedIn?: string; /** the account box (cards/claim-signed-in): its e-mail and Log out */ signedEmail?: string; onSignOut?: () => void; onIn: (r: SignedIn) => void | Promise<void>; onInvite: (group: string) => void
  /** signed in: the group or topic was just made */ onStarted: (group: string) => void; onClose: () => void
}) {
  const [info, setInfo] = useState<CardInfo | null>(null)
  const [offline, setOffline] = useState(false)
  const [tries, setTries] = useState(0)
  // opened by the e-mail's Confirm button: the claim is made now; this browser signs in when it started it
  const [confirm] = useState(() => takeConfirm(id))
  const [confirmed, setConfirmed] = useState<'' | 'asking' | 'elsewhere' | 'failed'>(confirm ? 'asking' : '')
  const [confirmError, setConfirmError] = useState('')
  useEffect(() => {
    if (!confirm) return
    let live = true
    claimConfirm(id, confirm).then(async (r) => {
      if (!live) return
      if (r.signed) { cardDone(); dropForm(); markMid(r.group, 'started'); await onIn(r.signed) } else { cardDone(); dropForm(); setConfirmed('elsewhere') }
    }, (e) => { if (live) { setConfirmError(cardErrorText(e instanceof CardFailed ? e : new CardFailed('failed'), 'group')); setConfirmed('failed') } })
    return () => { live = false }
  }, []) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    let live = true
    setOffline(false)
    cardInfo(id).then((i) => { if (live) setInfo(i) }, () => { if (live) setOffline(true) })
    return () => { live = false }
  }, [id, tries])
  // a claimed card: its guest link, and the group opens half pulled down with the welcome
  const join = (group: string, invite: string) => {
    // a topic card: its guest may post there before giving an e-mail (the lead's approved list)
    setCardTopic(info && info.state !== 'unknown' && info.kind === 'topic' ? group : '')
    cardDone()
    dropForm()
    setPendingInvite(invite)
    markMid(group, 'joined')
    onInvite(group)
  }
  useEffect(() => { if (info?.state === 'claimed') join(info.group, info.invite) }, [info]) // eslint-disable-line react-hooks/exhaustive-deps
  // a card that can't be started any more: what was typed for it goes (the lead's review)
  useEffect(() => { if (info?.state === 'retired' || info?.state === 'archived' || info?.state === 'unknown') dropForm() }, [info])

  if (confirmed === 'asking') return <div className="screen login card-page"><Logo /><div className="spinner" aria-hidden="true" /><p role="status">Confirming…</p></div>
  if (confirmed === 'elsewhere') return (
    <Dead title="Confirmed" onClose={onClose}>It’s created. Go back to the page where you scanned the card: it opens there by itself.</Dead>
  )
  if (confirmed === 'failed') return <Dead title="This link didn’t work" onClose={onClose}>{confirmError || 'It may have expired.'} Scan the card again to start over.</Dead>
  if (offline) return (
    <Dead title="No connection" onClose={onClose} again={() => setTries((n) => n + 1)}>
      This page needs the internet to open the card. What you typed stays here.
    </Dead>
  )
  if (!info || info.state === 'claimed') return (
    <div className="opening" role="status" aria-live="polite"><div className="spinner" aria-hidden="true" /><p>Opening the card…</p></div>
  )
  if (info.state === 'unknown') return <Dead title="This card isn’t known here" onClose={onClose}>Check that you scanned a card of {BELFRY.domain}.</Dead>
  if (info.state === 'retired') return (
    <Dead title={`${info.title || `This ${info.kind}`} is closed to new people`} onClose={onClose}>
      The card was retired. People already in the {info.kind} carry on there.
    </Dead>
  )
  if (info.state === 'archived') return <Dead title={`${info.title || `This ${info.kind}`} has ended`} onClose={onClose}>The {info.kind} was archived.</Dead>
  return <StartForm id={id} kind={info.kind} signedIn={signedIn} signedEmail={signedEmail} onSignOut={onSignOut} onIn={onIn} onTaken={join} onStarted={onStarted} onRetired={() => setInfo({ state: 'retired', kind: info.kind, title: '' })} />
}

/** A card that can't go the usual way: why, and the belfry's page (states-closed, -archived, -offline). */
function Dead({ title, children, again, onClose }: { title: string; children: React.ReactNode; again?: () => void; onClose: () => void }) {
  const community = useCommunityName(NAME)
  return (
    <div className="screen login card-page card-dead">
      <Logo />
      <h2>{title}</h2>
      <p role="alert">{children}</p>
      <div className="login-ctas">
        {again && <button className="btn" onClick={again}>Try again</button>}
        <a className={again ? 'btn ghost' : 'btn'} href={`https://${BELFRY.domain}`} onClick={() => cardDone()}>Visit {community}</a>
        <button className="link" onClick={() => { cardDone(); onClose() }}>Close</button>
      </div>
    </div>
  )
}

type Step = 'form' | 'code' | 'taken'

function StartForm({ id, kind, signedIn, signedEmail, onSignOut, onIn, onTaken, onStarted, onRetired }: {
  id: string; kind: CardKind; signedIn?: string; signedEmail?: string; onSignOut?: () => void; onIn: (r: SignedIn) => void | Promise<void>; onTaken: (group: string, invite: string) => void; onStarted: (group: string) => void; onRetired: () => void
}) {
  const community = useCommunityName(NAME)
  const [f, setF] = useState<CardForm>(() => keptForm(id) ?? EMPTY)
  const [step, setStep] = useState<Step>('form')
  const [error, setError] = useState('')
  const [code, setCode] = useState('')
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState('')
  const [tag, setTag] = useState<'' | 'free' | 'taken' | 'invalid' | 'unknown' | 'checking'>('')
  const [recheck, setRecheck] = useState(0)
  const [taken, setTaken] = useState<{ group: string; invite: string } | null>(null)
  const [sentAt, setSentAt] = useState(0) // "Send a new code (in 24 s)"
  const now = useNow(1000)
  const wait = Math.max(0, 30 - Math.floor(now - sentAt)) // (useNow: seconds)
  const set = (k: keyof CardForm) => (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) => setF((o) => ({ ...o, [k]: e.target.value }))
  useEffect(() => { keepForm(id, f) }, [id, f])
  // a topic: the hashtag is asked directly, it is the channel's name (Cards v87, onb-topic-form)
  // the hashtag's live check (a topic card)
  const slug = hashtagSlug(f.hashtag)
  const asked = useRef('')
  useEffect(() => {
    if (kind !== 'topic') return
    if (slug.length < 2) { setTag(f.hashtag.trim() ? 'invalid' : ''); return }
    setTag('checking')
    asked.current = slug
    const t = setTimeout(() => { void hashtagState(slug).then(async (s) => {
      if (asked.current !== slug) return
      setTag(s)
    }) }, 350)
    return () => clearTimeout(t)
  }, [slug, kind, recheck]) // eslint-disable-line react-hooks/exhaustive-deps

  const word = kind === 'topic' ? 'topic' : 'group'
  const fail = (e: unknown) => {
    const x = e instanceof CardFailed ? e : new CardFailed('failed')
    if (x.code === 'retired') { dropForm(); onRetired(); return }
    if (x.code === 'claimed' && x.extra.group && x.extra.invite) { dropForm(); setTaken({ group: x.extra.group, invite: x.extra.invite }); setStep('taken'); return }
    if (x.code === 'hashtag_taken') { setTag('taken'); setStep('form') }
    setError(cardErrorText(x, kind))
  }
  const ready = (kind === 'topic' ? tag === 'free' : !!f.title.trim()) && (!!signedIn || (!!f.name.trim() && /^\S+@\S+\.\S+$/.test(f.email.trim())))

  const start = async () => {
    setError('')
    if (!ready) return
    if (kind === 'topic') f.title = slug // (a topic's name is its hashtag; no words: Cards v87)
    if (signedIn) {
      try {
        const group = await claimSignedIn(id, kind, f, signedFetch)
        cardDone(); dropForm()
        markMid(group, 'started')
        onStarted(group)
      } catch (e) { fail(e) }
      return
    }
    try { await claimStart(id, kind, f); setCode(''); setSentAt(Date.now() / 1000); setStep('code') } catch (e) { fail(e) }
  }
  // the e-mail's Confirm button, clicked anywhere: this page moves on by itself (claim-poll every 3 s)
  useEffect(() => {
    if (step !== 'code' || signedIn) return
    let live = true, running = false
    const tick = async () => {
      if (running || busy) return
      running = true
      try {
        const r = await claimPoll(id, f.email)
        if (r && live) { cardDone(); dropForm(); markMid(r.group, 'started'); await onIn(r.signed) }
      } catch (e) {
        if (!live || !(e instanceof CardFailed) || e.code !== 'expired') return // (anything else: asked again)
        // 410 also when the Confirm button was opened in another tab of this browser: that tab holds the
        // session now (shared storage): come in with it instead of saying it expired (the lead)
        if (await loadSession().catch(() => undefined)) { live = false; location.reload(); return }
        setError(cardErrorText(e, kind))
      }
      finally { running = false }
    }
    const t = setInterval(() => { void tick() }, 3000)
    return () => { live = false; clearInterval(t) }
  }, [step, id]) // eslint-disable-line react-hooks/exhaustive-deps
  const verify = async (c: string) => {
    if (busy) return
    setBusy(true); setError(''); setNote('')
    try {
      const { signed, group } = await claimVerify(id, kind, f, c)
      cardDone(); dropForm()
      markMid(group, 'started')
      await onIn(signed)
    } catch (e) {
      const x = e instanceof CardFailed ? e.code : 'failed'
      if (x === 'wrong_code' || x === 'expired' || x === 'key_taken' || x === 'signer') setCode('')
      fail(e)
    } finally { setBusy(false) }
  }

  if (step === 'taken' && taken) return (
    <div className="screen login card-page">
      <Logo />
      <h2>Someone just started this {word}</h2>
      <p>Your text wasn’t used; you can join theirs.</p>
      <Btn className="btn" onClick={() => onTaken(taken.group, taken.invite)}>{kind === 'topic' ? 'Open the topic' : 'Join'}</Btn>
    </div>
  )
  const what = kind === 'topic' ? '#' + slug : f.title.trim()
  if (step === 'code') return (
    <div className="screen login card-page">
      <Logo />
      <h2>Check your e-mail</h2>
      <p>To create <b>{what}</b>, enter the 6-digit code we sent to <b className="addr">{f.email.trim()}</b>.</p>
      <CodeBoxes value={code} disabled={busy} invalid={!!error} onChange={(v) => { setCode(v); setError(''); if (v.length === 6) void verify(v) }} />
      <p className="hint">or click <b>Confirm</b> in the e-mail. This page moves on by itself once you have.</p>
      <div className="join-state" role="status" aria-live="polite">
        {busy ? <span className="hint">Creating {what}…</span> : error ? <span className="error" role="alert">{error}</span> : note ? <span className="hint">{note}</span> : null}
      </div>
      <div className="join-links">
        <Btn className="link" disabled={busy || wait > 0} onClick={async () => { setError(''); try { await claimStart(id, kind, f); setSentAt(Date.now() / 1000); setNote('A new code is on its way to your e-mail.') } catch (e) { fail(e) } }}>{wait > 0 ? `Send a new code (in ${wait} s)` : 'Send a new code'}</Btn>
        <button type="button" className="link" disabled={busy} onClick={() => { setStep('form'); setError(''); setNote(''); setCode('') }}>Use another address</button>
      </div>
      <p className="hint small">Not there? Look in your spam folder.</p>
    </div>
  )
  const me1 = (signedIn || '?').trim()[0]?.toUpperCase() ?? '?'
  return (
    <div className="screen login card-page">
      <Logo />
      <span className="card-chip">Card {id}{kind === 'topic' ? ' · topic' : ''}</span>
      <h2>Start a {word}</h2>
      <p>Nobody has scanned this card yet. The {word} starts with you.</p>
      <Form className="start-form card-form" onSubmit={start}>
        {kind === 'topic' ? <label className="field"><span>Hashtag</span>
          <span className="card-hash"><b aria-hidden="true">#</b><input className="input" value={f.hashtag} maxLength={31} autoCapitalize="none" spellCheck={false} required autoFocus onChange={(e) => { const v = typingSlug(e.target.value); setF((o) => ({ ...o, hashtag: v })) }} onBlur={() => setF((o) => ({ ...o, hashtag: o.hashtag.replace(/-+$/, '') }))} placeholder="tool-library" /></span>
          <small className={'card-tag ' + tag} role="status" aria-live="polite">{tag === 'free' ? '✓ Available · this is the channel’s name' : tag === 'taken' ? 'Taken: try another one' : tag === 'invalid' ? 'Letters, digits and dashes, 2 to 30' : tag === 'checking' ? 'Checking…' : tag === 'unknown' ? <>Couldn’t check just now · <button type="button" className="link" onClick={() => setRecheck((n) => n + 1)}>Try again</button></> : ''}</small>
        </label>
        : <label className="field"><span>Group name</span>
          <input className="input" value={f.title} maxLength={80} required autoFocus onChange={set('title')} />
          <small className="hint">What you wrote on the card</small></label>}
        <label className="field"><span>Description</span>
          <textarea className="input" rows={3} value={f.about} maxLength={500} onChange={set('about')} /></label>
        {!signedIn && <>
          <label className="field"><span>Your name</span>
            <input className="input" value={f.name} maxLength={100} autoComplete="name" required onChange={set('name')} /></label>
          <label className="field"><span>Your e-mail</span>
            <input className="input" type="email" inputMode="email" autoComplete="email" autoCapitalize="none" spellCheck={false} required placeholder="you@example.org" value={f.email} onChange={set('email')} /></label>
        </>}
        {error && <div className="error" role="alert">{error}</div>}
        <button className="btn" disabled={!ready}>Create the {word}</button>
        {signedIn && <div className="card-account" role="group" aria-label="Signed in">
          <span className="card-av" aria-hidden="true">{me1}</span>
          <span className="rt"><b>{signedIn}</b>{signedEmail && signedEmail !== signedIn && <small>{signedEmail}</small>}</span>
          {onSignOut && <button type="button" className="link" onClick={onSignOut}>Log out</button>}
        </div>}
        <p className="hint small">{signedIn
          ? (kind === 'topic' ? 'You’ll look after this topic.' : <>You’ll be the group’s first steward.<br />People who scan the same QR code again will be invited to join this group so that you can remain connected after this event without having to share your email address publicly.</>)
          : `You’ll look after this ${word}. We’ll send a 6-digit code to confirm your e-mail.`}</p>
      </Form>
    </div>
  )
}

/** Group settings, for its stewards (canvas cards/steward): the card this group was started from,
 *  its QR to print again, and Retire. Nothing when the group has no card. */
export function CardSettings({ group: gid, word }: { group: string; word: string }) {
  const [card, setCard] = useState<GroupCard | null>(null)
  const [qr, setQr] = useState(false)
  const [asking, setAsking] = useState(false)
  const [error, setError] = useState('')
  const [retired, setRetired] = useState(false)
  useEffect(() => { let live = true; groupCard(gid, signedFetch).then((c) => { if (live) setCard(c) }, () => {}); return () => { live = false } }, [gid])
  const img = useMemo(() => {
    if (!card) return ''
    const q = qrcode(0, 'M'); q.addData(cardUrl(card.id)); q.make()
    return 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(q.createSvgTag({ cellSize: 6, margin: 2, scalable: true }))
  }, [card])
  if (!card) return null
  if (retired) return <div className="card-settings"><span className="sec-title">The card</span><p className="hint">Card {card.id} is retired: its QR says the {word} is closed to new people. The belfry’s admins can reset it.</p></div>
  return (
    <div className="card-settings">
      <span className="sec-title">The card</span>
      <p><b>Card {card.id}</b><br /><small className="hint">{card.startedBy ? `Started by ${card.startedBy} · ` : ''}{card.joinedByScan === 1 ? '1 person' : `${card.joinedByScan} people`} joined by scanning it</small></p>
      <button type="button" className="link" onClick={() => setQr((x) => !x)}>{qr ? 'Hide the card’s QR' : 'Show the card’s QR'}</button>
      {qr && <div className="card-qr"><img src={img} alt={`The QR of card ${card.id}`} width={220} height={220} /><button type="button" className="btn ghost" onClick={() => print()}>Print</button></div>}
      {asking ? (
        <div className="confirm-box" role="alertdialog" aria-label="Retire this card">
          <b>Retire this card?</b>
          <p>Its QR will say the {word} is closed to new people. People already in it carry on here.</p>
          {error && <div className="error" role="alert">{error}</div>}
          <div className="row-btns two">
            <button type="button" className="btn ghost" onClick={() => { setAsking(false); setError('') }}>Keep it</button>
            <Btn className="btn danger" onClick={async () => { setError(''); try { await retireCard(card.id, signedFetch); setRetired(true) } catch (e) { setError(e instanceof CardFailed ? cardErrorText(e) : 'That did not work. Try again.') } }}>Retire</Btn>
          </div>
        </div>
      ) : <button type="button" className="link danger" onClick={() => setAsking(true)}>Retire this card</button>}
      <small className="hint">Only the {word}’s stewards and the belfry’s admins see this.</small>
    </div>
  )
}

/** "Summary by e-mail": Weekly (the default) or Monthly (cards v45: claim-channel, follow-channel-form). */
export function SummaryChoice({ value, onChange }: { value: 'weekly' | 'monthly'; onChange: (v: 'weekly' | 'monthly') => void }) {
  const opts: ['weekly' | 'monthly', string][] = [['weekly', 'Weekly'], ['monthly', 'Monthly']]
  return (
    <div className="field">
      <span id="sum-l">Summary by e-mail</span>
      <div className="seg2" role="radiogroup" aria-labelledby="sum-l">
        {opts.map(([v, label]) => <button key={v} type="button" role="radio" aria-checked={value === v} className={value === v ? 'on' : ''} onClick={() => onChange(v)}>{label}</button>)}
      </div>
      <small className="hint">What was said, once a {value === 'weekly' ? 'week' : 'month'}. Change it any time.</small>
    </div>
  )
}
