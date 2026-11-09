// Invite people to a channel: its link (members open it directly) and QR code; any member can
// make it a guest link instead: whoever uses it joins this channel only, for 30 days, without
// a membership (Belfry's /api/channel-invites, guests.py). The chat then shows
// "<name> joined as a guest, invited by <you>".
import { useMemo, useState } from 'react'
import qrcode from 'qrcode-generator'
import { BELFRY } from '../config'
import { channelIcon, group } from '../core/model'
import { signedFetch } from '../core/sync'
import { Sheet } from './common'

export function InviteSheet({ id, onClose }: { id: string; onClose: () => void }) {
  const g = group(id)
  // every member may bring a guest (Belfry checks they hold a membership, not a guest pass)
  const admin = true
  const [guest, setGuest] = useState(false)
  const [guestUrl, setGuestUrl] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [copied, setCopied] = useState(false)
  const memberUrl = `${location.origin}${location.pathname}#/c/${encodeURIComponent(id)}`
  const url = guest ? guestUrl : memberUrl
  const qr = useMemo(() => {
    if (!url) return ''
    const q = qrcode(0, 'M')
    q.addData(url)
    q.make()
    return 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(q.createSvgTag({ cellSize: 6, margin: 2, scalable: true }))
  }, [url])
  const pickGuest = async (on: boolean) => {
    setGuest(on); setError(''); setCopied(false)
    if (!on || guestUrl) return
    setBusy(true)
    try {
      const r = await signedFetch(`${BELFRY.pass}/api/channel-invites`, 'POST', { channel: id })
      const d = await r.json().catch(() => ({}))
      if (!r.ok) throw new Error(d.message || 'No guest link: try again.')
      setGuestUrl(String(d.url))
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e)); setGuest(false)
    } finally { setBusy(false) }
  }
  const copy = async () => {
    try { await navigator.clipboard.writeText(url) } catch {
      const t = document.createElement('textarea'); t.value = url; document.body.appendChild(t); t.select(); document.execCommand('copy'); t.remove()
    }
    setCopied(true); setTimeout(() => setCopied(false), 1500)
  }
  const label = `${channelIcon(g)}${g?.isPod || g?.isEvent || g?.isProposal ? ' ' : ''}${g?.name ?? id}`
  return (
    <Sheet title={`Invite to ${label}`} onClose={onClose}>
      {admin && (
        <label className="switch-row">
          <span><b>Guest access for 30 days</b><small>For someone who isn't a member: this channel only, nothing else.</small></span>
          <input type="checkbox" role="switch" checked={guest} disabled={busy} onChange={(e) => void pickGuest(e.target.checked)} />
        </label>
      )}
      {busy && <p className="hint">Making a guest link…</p>}
      {error && <p className="error">{error}</p>}
      {url && !busy && (
        <>
          <img className="drop-qr" src={qr} alt={`QR code of the ${guest ? 'guest' : 'channel'} link`} />
          <div className="drop-url"><code>{url}</code></div>
          <div className="row" style={{ gap: 8, marginTop: 10 }}>
            <button className="btn" style={{ flex: 1 }} onClick={() => void copy()}>{copied ? 'Copied' : 'Copy link'}</button>
            {'share' in navigator && <button className="btn ghost" style={{ flex: 1 }} onClick={() => void navigator.share({ title: label, url }).catch(() => {})}>Share…</button>}
          </div>
          <p className="hint" style={{ fontSize: 13, marginTop: 12 }}>
            {guest
              ? 'One person uses this link: they give their name and e-mail, and join for 30 days. Everyone here sees who invited them.'
              : `Members open ${label} with this link. For someone who isn't a member yet, turn on guest access above.`}
          </p>
        </>
      )}
    </Sheet>
  )
}
