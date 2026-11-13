import { useEffect, useState } from 'react'
import { BELFRY } from '../config'
import { applyBranding, branding, fetchBranding, onColor, saveBranding, type Branding } from '../core/branding'
import { uploadBlob } from '../core/sync'
import { NAME } from '../config'
import { back, Form, up } from './common'

const HEX = /^#[0-9a-f]{6}$/i

/** #/me/branding: the belfry's admins set its logo, cover image and two colours (branding.ts).
 *  The page shows them as they are picked; Save publishes them for everyone. */
export function BrandingScreen({ parent, embedded = false }: { /** opened from the settings: ‹ goes there */ parent?: string; /** inside the settings' panel (laptop): no top bar of its own */ embedded?: boolean } = {}) {
  const [b, setB] = useState<Branding>(branding())
  const [busy, setBusy] = useState<'' | 'logo' | 'cover'>('')
  const [error, setError] = useState('')
  const [saved, setSaved] = useState(false)
  // a live preview of the colours while picking; leaving without saving puts the saved ones back
  // the homepage's line is not in belfry.json: ask the belfry (once), unless it is being typed
  useEffect(() => { void fetchBranding().then((got) => { if (got) setB((x) => (x.description ? x : { ...x, description: got.description })) }) }, [])
  useEffect(() => { applyBranding({ ...b, primary: HEX.test(b.primary) ? b.primary : '', secondary: HEX.test(b.secondary) ? b.secondary : '' }) }, [b])
  useEffect(() => () => applyBranding(), [])
  const set = (k: keyof Branding, v: string) => { setSaved(false); setB((x) => ({ ...x, [k]: v })) }
  const pick = async (k: 'logo' | 'cover', f?: File) => {
    if (!f) return
    setBusy(k); setError('')
    try { set(k, await uploadBlob(f, k === 'logo' ? 512 : 1600)) } catch (x) { setError(x instanceof Error ? x.message : String(x)) } finally { setBusy('') }
  }
  const Color = ({ k, label, hint }: { k: 'primary' | 'secondary'; label: string; hint: string }) => (
    <div>
      <div style={{ fontWeight: 600 }}>{label}</div>
      <div className="hint" style={{ fontSize: 13 }}>{hint}</div>
      <div className="swatch">
        <input type="color" aria-label={label} value={HEX.test(b[k]) ? b[k] : '#2563eb'} onChange={(e) => set(k, e.target.value)} />
        <input className="input" aria-label={label + ' (hex)'} placeholder="#2563eb" value={b[k]} onChange={(e) => set(k, e.target.value.trim())} />
        {b[k] && <button type="button" className="link" onClick={() => set(k, '')}>Clear</button>}
      </div>
    </div>
  )
  const bad = (['primary', 'secondary'] as const).find((k) => b[k] && !HEX.test(b[k]))
  return (
    <div className={embedded ? 'settings-embed' : 'screen'}>
      {embedded ? <h2 className="panel-title">Branding</h2> : (
      <div className="topbar">
        <button className="icon-btn" aria-label="Back" onClick={() => (parent ? up(parent) : back('me'))}>‹</button>
        <h1>Branding<small>{NAME}</small></h1>
      </div>)}
      <div className="scroll pad brand-edit">
        <Form onSubmit={async (e) => {
          e.preventDefault()
          if (bad) { setError(`The ${bad} colour: #rrggbb, like #2563eb.`); return }
          setError('')
          try { await saveBranding(b); setSaved(true) } catch (x) { setError(x instanceof Error ? x.message : String(x)) }
        }}>
          <p className="hint" style={{ fontSize: 14, marginTop: 0 }}>
            Shown in the chat, in the mails and in any Nostr app: they are kept in the profile of the belfry's own Nostr account.
          </p>
          <label className="field"><span>Description · the line under {NAME} on its homepage, {BELFRY.domain}</span>
            <textarea className="input" rows={3} maxLength={500} placeholder="Empty: the default line" value={b.description} onChange={(e) => { setSaved(false); setB({ ...b, description: e.target.value }) }} /></label>
          <div style={{ fontWeight: 600 }}>Logo</div>
          <div className="brand-pick">
            {b.logo ? <img className="prev" src={b.logo} alt="The logo" /> : <span className="prev" aria-hidden="true" />}
            <label className="btn ghost" style={{ width: 'auto' }}>
              {busy === 'logo' ? 'Uploading…' : b.logo ? 'Change' : 'Upload'}
              <input type="file" accept="image/*" hidden disabled={!!busy} onChange={(e) => { const f = e.target.files?.[0]; e.target.value = ''; void pick('logo', f) }} />
            </label>
            {b.logo && <button type="button" className="link" onClick={() => set('logo', '')}>Remove</button>}
          </div>
          <div style={{ fontWeight: 600 }}>Cover image</div>
          <div className="brand-pick">
            {b.cover ? <img className="prev wide" src={b.cover} alt="The cover" /> : <span className="prev wide" aria-hidden="true" />}
            <label className="btn ghost" style={{ width: 'auto' }}>
              {busy === 'cover' ? 'Uploading…' : b.cover ? 'Change' : 'Upload'}
              <input type="file" accept="image/*" hidden disabled={!!busy} onChange={(e) => { const f = e.target.files?.[0]; e.target.value = ''; void pick('cover', f) }} />
            </label>
            {b.cover && <button type="button" className="link" onClick={() => set('cover', '')}>Remove</button>}
          </div>
          <Color k="primary" label="Primary colour" hint="Buttons, links in mails, the bar next to mentions" />
          <Color k="secondary" label="Secondary colour" hint="Badges and highlights" />
          <div className="row" style={{ gap: 8, margin: '0 0 16px' }} aria-label="Preview">
            {HEX.test(b.primary) && <span className="preview-btn" style={{ background: b.primary, color: onColor(b.primary) }}>Primary</span>}
            {HEX.test(b.secondary) && <span className="preview-btn" style={{ background: b.secondary, color: onColor(b.secondary) }}>Secondary</span>}
          </div>
          {error && <p className="error">{error}</p>}
          {saved && <p className="hint brand-saved">Saved. Everyone sees it the next time their app checks in.</p>}
          <button className="btn" type="submit" disabled={!!busy}>Save</button>
        </Form>
      </div>
    </div>
  )
}
