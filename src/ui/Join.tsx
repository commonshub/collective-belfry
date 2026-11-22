// The chat's start screen when nobody is signed in (Xavier 19 Nov, via the lead; boards
// onboarding/apply-* follow): the community's logo and name, "Log in" and, smaller, "Apply to
// join". Everything stays on chat.<domain> (core/direct.ts):
//   Log in   → e-mail → six boxes → in the chat. An address with no account: the apply screen,
//              the address carried over and proven (no second code).
//   Apply    → why + the charter's agreements (no name) → e-mail → six boxes → Application pending.
//   Pending  → asked every 5 s and when the tab comes back: approved = in the chat, no reload;
//              declined = said so. Opening the chat later in this browser asks first.
// One address and one code per device: never asked twice.
import { useEffect, useRef, useState } from 'react'
import { BELFRY, NAME } from '../config'
import { startSignIn } from '../core/auth'
import { loadRemoteCharter, type RemoteAgreement } from '../core/charter'
import { applyOld, applyStart, applyStatus, applyVerify, appliedEmail, DirectFailed, directErrorText, refusalOf, hasApplicantKey, loginStart, loginVerify, takePollTurn, type Verified } from '../core/direct'
import type { SignedIn } from '../core/guestjoin'
import type { Pending } from '../core/direct'
import { NameFields, Offers, type NameState } from './UsernameField'
import { takeStartAt } from '../core/signedin'
import { AgreementAbout } from './Charter'
import { Btn, Form } from './common'
import { useCommunityName } from './community'
import { CodeBoxes } from './GuestJoin'
import { CheckCircleIcon } from './icons'
import { Logo } from './Login'

type Step =
  | { s: 'checking' } | { s: 'start' }
  | { s: 'login-email' } | { s: 'login-code' } | { s: 'unknown'; proof: string }
  | { s: 'apply'; proof?: string } | { s: 'apply-email' } | { s: 'apply-code' }
  | { s: 'pending' } | { s: 'declined' } | { s: 'sent' } | { s: 'approved'; address: string } | { s: 'taken' }

// chat.<domain>/login/<username>: read as the page loads (before the router touches the hash);
// only a username by the rule is kept, never the raw text
const loginUser = (hash: string) => {
  const m = /^#\/login\/([a-z0-9](?:[a-z0-9._-]{0,28}[a-z0-9])?)\/?$/.exec(hash.toLowerCase())
  return m ? m[1] : ''
}
// also chat.<domain>/login/<username> served as the page itself (the #l= of sealed.md §5.2 survives that, not a redirect)
const LOGIN_LINK = loginUser(location.hash) || loginUser('#' + location.pathname)
let loginLinkTaken = false
function takeLoginLink(): string {
  if (loginLinkTaken) return ''
  loginLinkTaken = true
  if (/^#\/login\//.test(location.hash) || /^\/login\//.test(location.pathname)) history.replaceState(history.state, '', (/^\/login\//.test(location.pathname) ? '/' : location.pathname) + location.search)
  return LOGIN_LINK
}

const POLL = 5000 // every 5 s for the first 30 minutes of an open tab, then every minute
const SLOW = 60_000
const FAST_FOR = 30 * 60_000

export function StartScreen({ onIn, invited, error: given }: { onIn: (r: SignedIn) => Promise<void> | void; invited?: boolean; error?: string }) {
  const community = useCommunityName(NAME)
  const atApply = location.pathname === '/apply'
  // chat.<domain>/login/<username> (the proxy makes it #/login/<username>; "Link your phone"): Log
  // in with <username>@<domain> filled in. Only a username by the rule is used, never the raw text.
  const [linked] = useState(() => { const u = takeLoginLink(); return u ? `${u}@${BELFRY.domain}` : '' })
  const [step, setStep] = useState<Step>(() => linked ? { s: 'login-email' } : hasApplicantKey() ? { s: 'checking' } : ((at) => at === 'login' ? { s: 'login-email' } : at === 'apply' || atApply ? { s: 'apply' } : { s: 'start' })(takeStartAt()))
  const [email, setEmail] = useState(linked || appliedEmail())
  const [code, setCode] = useState('')
  const [why, setWhy] = useState('')
  const [who, setWho] = useState<NameState>({ name: '', username: '', ok: false })
  const [takenSug, setTakenSug] = useState<string[]>([]) // the belfry said the username went meanwhile
  const [takenAt, setTakenAt] = useState('') // 409 taken: the account's address, said on its screen
  const [pend, setPend] = useState<Pending | null>(null) // what /apply/status says of the request
  const [note2, setNote2] = useState('') // a declined request's note
  const named = { name: who.name.trim(), username: who.username.trim() }
  const [ticked, setTicked] = useState<Set<string>>(new Set())
  const [rules, setRules] = useState<RemoteAgreement[] | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [note, setNote] = useState('')
  const sentTo = useRef('')
  // "Send a new code (in N s)": 30 s after a code went out
  const [sentAt, setSentAt] = useState(0)
  const [, tick] = useState(0)
  const wait = Math.max(0, 30 - Math.floor((Date.now() - sentAt) / 1000))
  useEffect(() => { if (!wait) return; const t = setTimeout(() => tick((x) => x + 1), 1000); return () => clearTimeout(t) })
  const go = (s: Step) => { setError(''); setNote(''); setStep(s) }
  const fail = (e: unknown) => {
    const f = e instanceof DirectFailed ? e : new DirectFailed('failed')
    const r = refusalOf(f) // the same on iOS and Android (vectors/way-in.json)
    if (r.screen === 'sign_in_instead') { setTakenAt(r.nip05 ?? ''); return go({ s: 'taken' }) }
    // the username went meanwhile: back to it, with the belfry's suggestions
    if (f.code === 'username_taken' || f.code === 'username_invalid') {
      setTakenSug(f.suggestions); setWho((w) => ({ ...w, ok: false }))
      setStep((st) => ({ s: 'apply', proof: st.s === 'apply' ? st.proof : undefined }))
      setError(directErrorText(f)); return
    }
    // a belfry without its own register: the old ways (its sign-in page; the unsigned application)
    if (r.screen === 'older') {
      if (step.s === 'login-email') { void startSignIn(); return }
      void applyOld(email, why, [...ticked]).then(() => go({ s: 'sent' }), (x) => setError(directErrorText(x instanceof DirectFailed ? x : new DirectFailed('failed'))))
      return
    }
    if (r.screen !== 'stay') { setError(directErrorText(new DirectFailed('failed'))); return } // without_proof: handled where the proof is sent
    if (r.clears_code) setCode('')
    setError(r.message)
  }
  const landed = async (v: Verified, from: 'login' | 'apply') => {
    if (v.state === 'signed_in') { await onIn(v.signed); return }
    if (v.state === 'pending') return go({ s: 'pending' })
    if (from === 'login') return go({ s: 'unknown', proof: v.proof })
  }

  // opened again in this browser: where is my application?
  const check = async (quiet = false) => {
    try {
      const st = await applyStatus()
      if (st.state === 'approved') {
        if (st.signed) { await onIn(st.signed); return }
        return go({ s: 'approved', address: st.nip05 || appliedEmail() })
      }
      if (st.state === 'pending') { if (st.pending) setPend(st.pending); return step.s === 'pending' ? undefined : go({ s: 'pending' }) }
      if (st.state === 'declined') { setNote2(st.note ?? ''); return go({ s: 'declined' }) }
      if (!quiet) go(atApply ? { s: 'apply' } : { s: 'start' })
    } catch { if (!quiet) go(atApply ? { s: 'apply' } : { s: 'start' }) }
  }
  useEffect(() => { if (step.s === 'checking') void check() }, []) // eslint-disable-line react-hooks/exhaustive-deps
  // pending, tab open: every 5 s and when the tab comes back
  useEffect(() => {
    if (step.s !== 'pending') return
    // one tab of this browser asks at a time (takePollTurn); the others still ask when shown
    const since = Date.now()
    let t: ReturnType<typeof setTimeout>
    const next = () => { t = setTimeout(() => { if (document.visibilityState === 'visible' && takePollTurn()) void check(true); next() }, Date.now() - since < FAST_FOR ? POLL : SLOW) }
    next()
    const back = () => { if (document.visibilityState === 'visible') void check(true) }
    document.addEventListener('visibilitychange', back)
    addEventListener('focus', back)
    return () => { clearTimeout(t); document.removeEventListener('visibilitychange', back); removeEventListener('focus', back) }
  }, [step.s]) // eslint-disable-line react-hooks/exhaustive-deps
  // the charter, for the apply screen
  useEffect(() => { if (step.s === 'apply' && !rules) void loadRemoteCharter(true).then((r) => setRules([...r].sort((a, b) => a.order - b.order))).catch(() => setRules([])) }, [step.s]) // eslint-disable-line react-hooks/exhaustive-deps
  // the address bar follows Apply (chat.<domain>/apply), never pushing on a ‹
  useEffect(() => {
    const want = step.s === 'apply' || step.s === 'apply-email' || step.s === 'apply-code' ? '/apply' : '/'
    if (location.pathname !== want && (want === '/apply' || location.pathname === '/apply')) history.replaceState(history.state, '', want + location.search + location.hash)
  }, [step.s])

  // the same link opened in this tab later (only the hash changes)
  useEffect(() => {
    const on = () => {
      if (!/^#\/login\//.test(location.hash)) return
      const u = loginUser(location.hash)
      history.replaceState(history.state, '', location.pathname + location.search)
      if (u) { setEmail(`${u}@${BELFRY.domain}`); setCode(''); go({ s: 'login-email' }) }
    }
    addEventListener('hashchange', on)
    return () => removeEventListener('hashchange', on)
  }, []) // eslint-disable-line react-hooks/exhaustive-deps
  const emailOk = /^\S+@\S+\.\S+$/.test(email.trim())
  const codeStep = async (c: string, from: 'login' | 'apply') => {
    if (busy) return
    setBusy(true); setError(''); setNote('')
    try { await landed(from === 'login' ? await loginVerify(sentTo.current, c) : await applyVerify(sentTo.current, c), from) } catch (e) { fail(e) } finally { setBusy(false) }
  }
  const resend = async (from: 'login' | 'apply') => {
    setError(''); setNote('')
    try {
      if (from === 'login') await loginStart(sentTo.current)
      else await applyStart(sentTo.current, why, [...ticked], undefined, named)
      setCode(''); setSentAt(Date.now()); setNote('A new code is on its way to your e-mail.')
    } catch (e) { fail(e) }
  }

  const head = <><Logo /><h2>{community}</h2></>
  const err = (error || given) ? <div className="error" role="alert">{error || given}</div> : null

  if (step.s === 'checking') return <div className="screen login start" role="status" aria-live="polite">{head}<div className="spinner" aria-label="Opening" /></div>

  if (step.s === 'start')
    return (
      <div className="screen login start">
        {head}
        <p className="hint start-line">Channels, events and files for its members. No password: a 6-digit code by e-mail.</p>
        {invited && <div className="card" role="status" style={{ marginBottom: 16 }}>After logging in, your invite opens its channel.</div>}
        {err}
        <div className="login-ctas">
          <button className="btn" onClick={() => go({ s: 'login-email' })}>Log in</button>
        </div>
        <button className="link start-apply" onClick={() => go({ s: 'apply' })}>Apply to join</button>
      </div>
    )

  if (step.s === 'login-email' || step.s === 'apply-email') {
    const apply = step.s === 'apply-email'
    return (
      <div className="screen login start">
        {head}
        <Form className="join-sheet start-form" onSubmit={async () => {
          setError('')
          try {
            sentTo.current = email.trim()
            if (apply) await applyStart(sentTo.current, why, [...ticked], undefined, named)
            else await loginStart(sentTo.current)
            setCode(''); setSentAt(Date.now()); go({ s: apply ? 'apply-code' : 'login-code' })
          } catch (e) { fail(e) }
        }}>
          <h3>{apply ? 'Your e-mail' : 'Log in'}</h3>
          <p className="hint">{apply ? 'We send a 6-digit code to check it’s yours. The stewards’ answer comes here too.' : 'We send a 6-digit code to your e-mail. No password.'}</p>
          <label className="field"><span>E-mail</span>
            <input className="input" type="email" inputMode="email" autoComplete="email" autoCapitalize="none" spellCheck={false} required autoFocus
              placeholder="you@example.org" value={email} onChange={(e) => setEmail(e.target.value)} /></label>
          {err}
          <button className="btn" disabled={!emailOk}>Send me a code</button>
          <button type="button" className="link" onClick={() => go(apply ? { s: 'apply' } : { s: 'start' })}>‹ Back</button>
        </Form>
      </div>
    )
  }

  if (step.s === 'login-code' || step.s === 'apply-code') {
    const from = step.s === 'login-code' ? 'login' : 'apply'
    return (
      <div className="screen login start">
        {head}
        <div className="join-sheet start-form">
          <h3>Check your e-mail</h3>
          <p>We sent a 6-digit code <b>by e-mail</b> to <b className="addr">{sentTo.current}</b>. It can take a minute.</p>
          <CodeBoxes value={code} disabled={busy} invalid={!!error} onChange={(v) => { setCode(v); setError(''); if (v.length === 6) void codeStep(v, from) }} />
          <div className="join-state" role="status" aria-live="polite">
            {busy ? <span className="hint">Checking the code…</span> : error ? <span className="error" role="alert">{error}</span> : note ? <span className="hint">{note}</span> : <span className="hint">Not there? Look in your spam folder.</span>}
          </div>
          <div className="join-links">
            <Btn className="link" disabled={busy || wait > 0} onClick={() => resend(from)}>{wait > 0 ? `Send a new code (in ${wait} s)` : 'Send a new code'}</Btn>
            <button type="button" className="link" disabled={busy} onClick={() => { setCode(''); go({ s: from === 'login' ? 'login-email' : 'apply-email' }) }}>Use another address</button>
          </div>
        </div>
      </div>
    )
  }

  if (step.s === 'unknown')
    return (
      <div className="screen login start">
        {head}
        <h3>No account here yet</h3>
        <p className="hint"><b className="addr">{email.trim()}</b> is confirmed, but it isn’t a member of {community}.</p>
        <button className="btn" onClick={() => go({ s: 'apply', proof: step.proof })}>Apply to join</button>
        <button type="button" className="link start-apply" onClick={() => { setCode(''); go({ s: 'login-email' }) }}>Use another address</button>
      </div>
    )

  if (step.s === 'apply') {
    const list = rules ?? []
    const all = list.every((r) => ticked.has(r.id))
    const proof = step.proof
    return (
      <div className="screen login start" style={{ justifyContent: 'flex-start', overflowY: 'auto' }}>
        {head}
        <Form className="apply-form" onSubmit={async () => {
          setError('')
          // Enter can submit past the disabled button: the same conditions here
          if (!named.name || !who.ok || !why.trim() || rules === null || !(rules ?? []).every((r) => ticked.has(r.id))) return
          if (!proof) return go({ s: 'apply-email' })
          // the address was just proven by Log in: no second code
          try {
            sentTo.current = email.trim()
            const v = await applyStart(sentTo.current, why, [...ticked], proof, named)
            if (v !== 'sent') return landed(v, 'apply') // pending, or signed in: the address got an account meanwhile
            setCode(''); go({ s: 'apply-code' })
          } catch (e) {
            if (e instanceof DirectFailed && e.code === 'proof') { // stale: a code after all
              try { await applyStart(sentTo.current, why, [...ticked], undefined, named); setCode(''); go({ s: 'apply-code' }); setNote(directErrorText(e)) } catch (x) { fail(x) }
              return
            }
            fail(e)
          }
        }}>
          <h3>Apply to join {community}</h3>
          <NameFields value={who} onChange={(v) => { setWho(v); if (v.username !== who.username) setTakenSug([]) }} via="applicant" autoFocus />
          {takenSug.length > 0 && <p className="username-state"><span className="error">Try <Offers list={takenSug} pick={(x) => { setWho({ ...who, username: x, ok: false }); setTakenSug([]) }} /></span></p>}
          {proof && <p className="hint start-unknown">With <b className="addr">{email.trim()}</b>: the stewards answer there by e-mail.</p>}
          <label className="field"><span>Why would you like to join?</span>
            <textarea className="input" rows={4} required maxLength={4000} value={why} onChange={(e) => setWhy(e.target.value)} /></label>
          {rules === null && <div className="spinner" aria-label="Loading the charter" />}
          {list.length > 0 && <>
            <div className="field"><span>Our charter: tick each agreement</span></div>
            <div className="charter-list" style={{ marginTop: -6 }}>
              {list.map((r) => (
                <button type="button" key={r.id} className={'rule' + (ticked.has(r.id) ? ' on' : '')} role="checkbox" aria-checked={ticked.has(r.id)}
                  onClick={() => setTicked((s) => { const n = new Set(s); if (n.has(r.id)) n.delete(r.id); else n.add(r.id); return n })}>
                  <span className="tick"><CheckCircleIcon on={ticked.has(r.id)} /></span>
                  <span className="t"><b>{r.title}</b>{r.about && <AgreementAbout text={r.about} />}</span>
                </button>
              ))}
            </div>
          </>}
          {err}
          <button className="btn" disabled={!named.name || !who.ok || !why.trim() || rules === null || !all}>{all ? (proof ? 'Apply' : 'Continue') : `Tick each agreement (${ticked.size} of ${list.length})`}</button>
          {!proof && <button type="button" className="link" onClick={() => go({ s: 'login-email' })}>I’m already a member · Log in</button>}
        </Form>
      </div>
    )
  }

  if (step.s === 'pending') {
    // what the belfry says of the request, else what was typed here
    const p: Pending = pend ?? { name: named.name, username: named.username, email: email.trim(), motivation: why.trim() }
    return (
      <div className="screen login start" role="status" aria-live="polite">
        {head}
        <h3>Your request is pending</h3>
        {(p.name || p.email) && <div className="pending-card">
          <span><span className="nm">{p.name}</span>{p.username && <> <span className="hint">@{p.username}</span></>}</span>
          {p.email && <small className="addr">{p.email}</small>}
        </div>}
        {p.motivation && <><div className="field" style={{ width: '100%', maxWidth: 420 }}><span>Your message</span></div><blockquote className="start-why">{p.motivation}</blockquote></>}
        <p className="hint small">Once approved, you will be logged in to {location.host} with this browser. To log in in another browser or device, just enter your email{p.email ? <> <b className="addr">{p.email}</b></> : null} to receive a new 6-digit code.</p>
      </div>
    )
  }

  if (step.s === 'sent')
    return (
      <div className="screen login start" role="status">
        {head}
        <h3>Thank you</h3>
        <p className="hint">Your request is in. The stewards’ answer comes by e-mail.</p>
      </div>
    )

  if (step.s === 'declined')
    return (
      <div className="screen login start" role="status">
        {head}
        <h3>Not this time</h3>
        <p className="hint">The stewards of {community} did not accept your request this time.{note2 ? '' : ' Their e-mail may say more.'}</p>
        {note2 && <blockquote className="start-why declined-note">{note2}</blockquote>}
        <button className="link start-apply" onClick={() => go({ s: 'start' })}>I have an account: log in</button>
      </div>
    )

  if (step.s === 'approved')
    return (
      <div className="screen login start">
        {head}
        <h3>You’re in</h3>
        <p className="hint">Your application was accepted. Log in{step.address ? <> with <b className="addr">{step.address}</b></> : null}.</p>
        <button className="btn" onClick={() => { if (step.address.includes('@')) setEmail(appliedEmail() || step.address); go({ s: 'login-email' }) }}>Log in</button>
      </div>
    )

  // taken: an account that signs in with its own key (the belfry's own page)
  return (
    <div className="screen login start">
      {head}
      <h3>This address signs in with its own key</h3>
      <p className="hint">Its account{takenAt ? <> (<b className="addr">{takenAt}</b>)</> : null} holds its own key, so it logs in with your signer.</p>
      <Btn className="btn" onClick={() => startSignIn()}>Log in with my key</Btn>
      <button type="button" className="link" onClick={() => go({ s: 'start' })}>‹ Back</button>
    </div>
  )
}
