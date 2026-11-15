// #/me/signin: how I get into my account (#issues 2026-11-15): my e-mail (a code), and passkeys.
import { useEffect, useState } from 'react'
import { BELFRY } from '../config'
import { addPasskey, methods, passkeysWork, removePasskey, renamePasskey, type Methods, type Passkey } from '../core/passkeys'
import { address } from '../core/dm'
import { back, Btn, Sheet } from './common'

export function SignInMethods() {
  const [m, setM] = useState<Methods | null>(null)
  const [error, setError] = useState('')
  const [naming, setNaming] = useState<Passkey | null>(null)
  useEffect(() => { methods().then(setM, (e) => setError(e.message)) }, [])
  const run = async (f: () => Promise<Methods>) => { setError(''); try { setM(await f()) } catch (e) { setError(e instanceof Error ? e.message : String(e)) } }
  const only = !!m && m.passkeys.length === 1 && !m.email
  return (
    <div className="screen signin-methods">
      <div className="topbar">
        <button className="icon-btn" aria-label="Back" onClick={() => back('me')}>‹</button>
        <h1>Sign-in methods<small>How you get into your account</small></h1>
      </div>
      <div className="scroll pad">
        <div className="sm-row">
          <span className="ic" aria-hidden="true">✉️</span>
          <span className="t"><b>E-mail code</b><small>{m?.email ? `A 6-digit code to your e-mail${address() ? ` (${address()})` : ''}` : 'No e-mail on your account'}</small></span>
        </div>
        <div className="section-title" style={{ padding: '16px 0 6px' }}>Passkeys <span>{m?.passkeys.length ?? ''}</span></div>
        <p className="hint">A passkey signs you in with your fingerprint, face or screen lock, without an e-mail code. One passkey works on every {BELFRY.domain} app.</p>
        {m?.passkeys.map((p) => (
          <div key={p.id} className="sm-row">
            <span className="ic" aria-hidden="true">🔑</span>
            <span className="t"><b>{p.label}</b><small>Added {p.created.slice(0, 10)}{p.last_used ? ` · last used ${p.last_used.slice(0, 10)}` : ''}</small></span>
            <button className="link" onClick={() => setNaming(p)}>Rename</button>
            <Btn className="link danger" disabled={only} title={only ? 'Your only way in' : undefined} onClick={() => run(() => removePasskey(p.id))}>Remove</Btn>
          </div>
        ))}
        {error && <p className="error" role="alert">{error}</p>}
        {passkeysWork()
          ? <Btn className="btn" style={{ marginTop: 12 }} onClick={() => run(() => addPasskey(deviceName()))}>Add a passkey</Btn>
          : <p className="hint">This browser can't make passkeys.</p>}
        {m && m.passkeys.length > 0 && <p className="hint" style={{ marginTop: 12 }}>To sign in with it, choose “Sign in with a passkey” on the sign-in page. Removing one asks you to confirm with a passkey first.</p>}
      </div>
      {naming && <RenameSheet p={naming} onClose={() => setNaming(null)} onSave={(label) => run(() => renamePasskey(naming.id, label)).then(() => setNaming(null))} />}
    </div>
  )
}

function RenameSheet({ p, onClose, onSave }: { p: Passkey; onClose: () => void; onSave: (label: string) => Promise<void> }) {
  const [label, setLabel] = useState(p.label)
  return (
    <Sheet title="Rename the passkey" onClose={onClose}>
      <label className="field"><span>Name</span><input className="input" maxLength={60} value={label} autoFocus onChange={(e) => setLabel(e.target.value)} /></label>
      <Btn className="btn" disabled={!label.trim()} onClick={() => onSave(label.trim())}>Save</Btn>
    </Sheet>
  )
}

/** A name for a new passkey: the kind of device it was made on. */
function deviceName(): string {
  const ua = navigator.userAgent
  return /iPhone/.test(ua) ? 'iPhone' : /iPad/.test(ua) ? 'iPad' : /Android/.test(ua) ? 'Android phone' : /Mac/.test(ua) ? 'Mac' : /Windows/.test(ua) ? 'Windows' : /Linux/.test(ua) ? 'Linux' : 'Passkey'
}
