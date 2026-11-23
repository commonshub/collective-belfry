// A printed card's page (canvas "Belfry · Group cards": cards/claim-*, confirm-code, states-*):
// what chat.<domain>/g/<id> opens. Free card: "Start this group" / "Start this topic" (signed in:
// straight in; not signed in: name and e-mail, the 6-digit code, then in). A claimed card is a
// guest link: its invite takes the usual way in (App.tsx). Dead ends say why, with the way to the
// belfry's page. Nothing from the address is shown.
import { useEffect, useMemo, useRef, useState } from 'react'
import qrcode from 'qrcode-generator'
import { BELFRY, NAME } from '../config'
import { setPendingInvite } from '../core/auth'
import {
  CardFailed, cardErrorText, cardInfo, claimSignedIn, claimStart, claimVerify, dropForm, hashtagSlug, hashtagState,
  cardDone, cardUrl, groupCard, keepForm, keptForm, markMid, retireCard, type CardForm, type GroupCard, type CardInfo, type CardKind,
} from '../core/cards'
import type { SignedIn } from '../core/guestjoin'
import { signedFetch } from '../core/sync'
import { Btn, Form } from './common'
import { CodeBoxes } from './GuestJoin'
import { useCommunityName } from './community'
import { Logo } from './Login'

const EMPTY: CardForm = { title: '', about: '', hashtag: '', name: '', email: '' }

/** The card ID. SIGNED_IN: the account's name ("Signed in as …"), or undefined for a visitor.
 *  ON_IN: a visitor's right code (the session to enter). ON_INVITE: the card's guest link is the
 *  pending invite now (App takes it from there). ON_CLOSE: leave the card page. */
export function CardScreen({ id, signedIn, onIn, onInvite, onStarted, onClose }: {
  id: string; signedIn?: string; onIn: (r: SignedIn) => void | Promise<void>; onInvite: (group: string) => void
  /** signed in: the group or topic was just made */ onStarted: (group: string) => void; onClose: () => void
}) {
  const [info, setInfo] = useState<CardInfo | null>(null)
  const [offline, setOffline] = useState(false)
  const [tries, setTries] = useState(0)
  useEffect(() => {
    let live = true
    setOffline(false)
    cardInfo(id).then((i) => { if (live) setInfo(i) }, () => { if (live) setOffline(true) })
    return () => { live = false }
  }, [id, tries])
  // a claimed card: its guest link, and the group opens half pulled down with the welcome
  const join = (group: string, invite: string) => {
    cardDone()
    dropForm()
    setPendingInvite(invite)
    markMid(group, 'joined')
    onInvite(group)
  }
  useEffect(() => { if (info?.state === 'claimed') join(info.group, info.invite) }, [info]) // eslint-disable-line react-hooks/exhaustive-deps
  // a card that can't be started any more: what was typed for it goes (the lead's review)
  useEffect(() => { if (info?.state === 'retired' || info?.state === 'archived' || info?.state === 'unknown') dropForm() }, [info])

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
  return <StartForm id={id} kind={info.kind} signedIn={signedIn} onIn={onIn} onTaken={join} onStarted={onStarted} onRetired={() => setInfo({ state: 'retired', kind: info.kind, title: '' })} />
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

function StartForm({ id, kind, signedIn, onIn, onTaken, onStarted, onRetired }: {
  id: string; kind: CardKind; signedIn?: string; onIn: (r: SignedIn) => void | Promise<void>; onTaken: (group: string, invite: string) => void; onStarted: (group: string) => void; onRetired: () => void
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
  const set = (k: keyof CardForm) => (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) => setF((o) => ({ ...o, [k]: e.target.value }))
  useEffect(() => { keepForm(id, f) }, [id, f])
  // the hashtag's live check (a topic card)
  const slug = hashtagSlug(f.hashtag)
  const asked = useRef('')
  useEffect(() => {
    if (kind !== 'topic') return
    if (slug.length < 2) { setTag(f.hashtag.trim() ? 'invalid' : ''); return }
    setTag('checking')
    asked.current = slug
    const t = setTimeout(() => { void hashtagState(slug).then((s) => { if (asked.current === slug) setTag(s) }) }, 350)
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
  const ready = !!f.title.trim() && (kind !== 'topic' || tag === 'free') && (!!signedIn || (!!f.name.trim() && /^\S+@\S+\.\S+$/.test(f.email.trim())))

  const start = async () => {
    setError('')
    if (!ready) return
    if (signedIn) {
      try {
        const group = await claimSignedIn(id, kind, f, signedFetch)
        cardDone(); dropForm()
        markMid(group, 'started')
        onStarted(group)
      } catch (e) { fail(e) }
      return
    }
    try { await claimStart(id, f.email); setCode(''); setStep('code') } catch (e) { fail(e) }
  }
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
  if (step === 'code') return (
    <div className="screen login card-page">
      <Logo />
      <h2>Check your e-mail</h2>
      <p>We sent a 6-digit code to <b className="addr">{f.email.trim()}</b>. It can take a minute.</p>
      <CodeBoxes value={code} disabled={busy} invalid={!!error} onChange={(v) => { setCode(v); setError(''); if (v.length === 6) void verify(v) }} />
      <div className="join-state" role="status" aria-live="polite">
        {busy ? <span className="hint">Starting {f.title.trim()}…</span> : error ? <span className="error" role="alert">{error}</span> : note ? <span className="hint">{note}</span> : <span className="hint">Not there? Look in your spam folder.</span>}
      </div>
      <div className="join-links">
        <Btn className="link" disabled={busy} onClick={async () => { setError(''); try { await claimStart(id, f.email); setNote('A new code is on its way to your e-mail.') } catch (e) { fail(e) } }}>Send a new code</Btn>
        <button type="button" className="link" disabled={busy} onClick={() => { setStep('form'); setError(''); setNote(''); setCode('') }}>Use another address</button>
      </div>
    </div>
  )
  return (
    <div className="screen login card-page">
      <Logo />
      <h2>Start this {word}</h2>
      <p>{kind === 'topic'
        ? 'You found a new topic card. No introductions needed: people come and go and share what they know.'
        : `You found a new group card. You start the group and look after it; everyone who scans this card after you joins it.`}</p>
      {signedIn && <p className="card-who">Signed in as <b>{signedIn}</b></p>}
      <Form className="start-form card-form" onSubmit={start}>
        {kind === 'topic' && <label className="field"><span>Hashtag</span>
          <span className="card-hash"><b aria-hidden="true">#</b><input className="input" value={f.hashtag} maxLength={31} autoCapitalize="none" spellCheck={false} required onChange={set('hashtag')} placeholder="sourdough" /></span>
          <small className={'card-tag ' + tag} role="status" aria-live="polite">{tag === 'free' ? '✓ Available' : tag === 'taken' ? 'Taken: try another one' : tag === 'invalid' ? 'Letters, digits and dashes, 2 to 30' : tag === 'checking' ? 'Checking…' : tag === 'unknown' ? <>Couldn’t check just now · <button type="button" className="link" onClick={() => setRecheck((n) => n + 1)}>Try again</button></> : ''}</small>
        </label>}
        <label className="field"><span>{kind === 'topic' ? 'Topic' : 'Title'}</span>
          <input className="input" value={f.title} maxLength={80} required onChange={set('title')} placeholder={kind === 'topic' ? 'What the topic is' : 'What you wrote on the card'} /></label>
        <label className="field"><span>{kind === 'topic' ? 'About' : 'Description'} <small>(optional)</small></span>
          <textarea className="input" rows={3} value={f.about} maxLength={500} onChange={set('about')} /></label>
        {!signedIn && <>
          <label className="field"><span>Your name</span>
            <input className="input" value={f.name} maxLength={100} autoComplete="name" required onChange={set('name')} /></label>
          <label className="field"><span>E-mail</span>
            <input className="input" type="email" inputMode="email" autoComplete="email" autoCapitalize="none" spellCheck={false} required placeholder="you@example.org" value={f.email} onChange={set('email')} /></label>
          <p className="hint small">We’ll send a 6-digit code to confirm it. Already have an account on {community}? Use its e-mail address.</p>
        </>}
        {error && <div className="error" role="alert">{error}</div>}
        <button className="btn" disabled={!ready}>Start the {word}</button>
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
