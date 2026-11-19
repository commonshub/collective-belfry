// "Your name" and "Pick a username:" (boards onboarding/apply-name, username-states,
// member-link-name; canvas v54, Xavier 19 Nov). The username starts as what the belfry suggests
// for the name (its first name: lina) until the person edits it; it is checked as they type
// (≥ 300 ms after the last key): "<u>@<domain> is available" / "Checking…" / "Taken. Try [x]"
// (the belfry's suggestions, one tap) / the rules, only when not allowed. The client never
// invents a username. Under it: what the address is for.
import { useEffect, useRef, useState } from 'react'
import { BELFRY } from '../config'
import { checkUsername, type Check } from '../core/direct'
import { CheckIcon } from './icons'

export interface NameState { name: string; username: string; ok: boolean }

export function NameFields({ value, onChange, via, autoFocus }: { value: NameState; onChange: (v: NameState) => void; via: 'applicant' | 'session'; autoFocus?: boolean }) {
  const [edited, setEdited] = useState(false) // the person typed a username: no more suggestions over it
  const [check, setCheck] = useState<Check | null>(null)
  const [checking, setChecking] = useState(false)
  const askedName = useRef(0)
  const asked = useRef(0)
  const set = (patch: Partial<NameState>) => onChange({ ...value, ...patch })
  // the name typed: the belfry's suggestion fills the username (until it is edited)
  useEffect(() => {
    if (edited || !value.name.trim()) return
    const n = ++askedName.current
    const t = setTimeout(() => {
      void checkUsername({ name: value.name.trim() }, via).then((r) => {
        if (n !== askedName.current || !r?.suggested) return
        if (r.suggested !== value.username) onChange({ ...value, username: r.suggested, ok: false })
      }).catch(() => {})
    }, 300)
    return () => clearTimeout(t)
  }, [value.name]) // eslint-disable-line react-hooks/exhaustive-deps
  // the username: checked 300 ms after the last change
  useEffect(() => {
    const u = value.username.trim()
    if (!u) { setCheck(null); setChecking(false); if (value.ok) set({ ok: false }); return }
    setChecking(true)
    const n = ++asked.current
    const t = setTimeout(() => {
      void checkUsername({ username: u, ...(value.name.trim() ? { name: value.name.trim() } : {}) }, via).then((r) => {
        if (n !== asked.current) return
        setChecking(false)
        if (!r?.check) return // 429: keep the last state
        setCheck(r.check)
        const ok = r.check.state === 'available' && r.check.username === u
        if (ok !== value.ok) onChange({ ...value, ok })
      }).catch(() => { if (n === asked.current) setChecking(false) })
    }, 300)
    return () => clearTimeout(t)
  }, [value.username]) // eslint-disable-line react-hooks/exhaustive-deps
  const u = value.username.trim() || 'you'
  const state = checking ? 'checking' : check?.state
  return (
    <div className="name-fields">
      <label className="field"><span>Your name</span>
        <input className="input" value={value.name} maxLength={100} autoComplete="name" required autoFocus={autoFocus}
          onChange={(e) => set({ name: e.target.value })} /></label>
      <label className="field"><span>Pick a username:</span>
        <span className={'username-input' + (state === 'taken' || state === 'invalid' ? ' bad' : state === 'available' ? ' good' : '')}>
          <input className="input" value={value.username} maxLength={30} autoCapitalize="none" autoCorrect="off" spellCheck={false} aria-describedby="username-state"
            onChange={(e) => { setEdited(true); onChange({ ...value, username: e.target.value.toLowerCase().replace(/\s+/g, ''), ok: false }) }} />
          <span className="at" aria-hidden="true">@{BELFRY.domain}</span>
        </span></label>
      <div id="username-state" className="username-state" role="status" aria-live="polite">
        {state === 'checking' ? <span className="hint">Checking…</span>
          : state === 'available' ? <span className="ok"><CheckIcon /> {check!.username}@{BELFRY.domain} is available</span>
          : state === 'taken' ? <span className="error">Taken.{check!.suggestions.length > 0 && <> Try {check!.suggestions.map((x, i) => <span key={x}>{i > 0 && ' or '}<button type="button" className="chip-btn" onClick={() => { setEdited(true); onChange({ ...value, username: x, ok: false }) }}>{x}</button></span>)}</>}</span>
          : state === 'invalid' && check!.reason === 'reserved' ? <span className="error">That name is kept for the belfry.{check!.suggestions.length > 0 && <> Try {check!.suggestions.map((x, i) => <span key={x}>{i > 0 && ' or '}<button type="button" className="chip-btn" onClick={() => { setEdited(true); onChange({ ...value, username: x, ok: false }) }}>{x}</button></span>)}</>}</span>
          : state === 'invalid' ? <span className="error">Use lowercase letters, digits and dashes, 3 to 30 characters.</span>
          : null}
      </div>
      <p className="hint small username-about">This is your address here: <b>{u}@{BELFRY.domain}</b>. You log in with it, people mention you as @{u}, and it receives your direct messages and e-mails.</p>
    </div>
  )
}

/** Set the username from outside (the belfry said it was taken meanwhile): its suggestions show. */
export const takenNow = (v: NameState): NameState => ({ ...v, ok: false })
