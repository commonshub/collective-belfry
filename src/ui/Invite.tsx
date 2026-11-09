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
  // guest on and its link not here yet (loading, or failed): the last QR stays, faded, so the
  // sheet keeps its height; a spinner, or an error mark with Retry, sits on top of it
  const pending = guest && !guestUrl
  const url = guest && guestUrl ? guestUrl : memberUrl
  const qr = useMemo(() => {
    const q = qrcode(0, 'M')
    q.addData(url)
    q.make()
    return 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(q.createSvgTag({ cellSize: 6, margin: 2, scalable: true }))
  }, [url])
  const fetchGuest = async () => {
    setBusy(true); setError('')
    try {
      const r = await signedFetch(`${BELFRY.api}/channel-invites`, 'POST', { channel: id })
      const d = await r.json().catch(() => ({}))
      if (!r.ok) throw new Error(d.message || 'The guest link could not be made.')
      setGuestUrl(String(d.url))
    } catch (e) {
      const m = e instanceof Error ? e.message : String(e)
      setError(/fetch|network|load/i.test(m) ? 'No connection to the belfry.' : m)
    } finally { setBusy(false) }
  }
  const pickGuest = (on: boolean) => {
    setGuest(on); setCopied(false)
    if (on && !guestUrl && !busy) void fetchGuest()
    if (!on) setError('')
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
          <input type="checkbox" role="switch" checked={guest} onChange={(e) => pickGuest(e.target.checked)} />
        </label>
      )}
      <div className={'qr-box' + (pending ? ' waiting' : '')}>
        <img className="drop-qr" src={qr} alt={pending ? '' : `QR code of the ${guest ? 'guest' : 'channel'} link`} />
        {pending && busy && <span className="qr-over" role="status" aria-label="Making the guest link"><span className="spinner" /></span>}
        {pending && !busy && error && (
          <span className="qr-over error" role="alert">
            <svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" aria-hidden="true"><circle cx="12" cy="12" r="9.5" /><path d="M12 7.5v5.5M12 16.5v.01" /></svg>
            <span className="qr-err">{error}</span>
            <button className="btn small" onClick={() => void fetchGuest()}>Retry</button>
          </span>
        )}
      </div>
      <div className="drop-url"><code>{pending ? (busy ? 'Making the guest link…' : '—') : url}</code></div>
      <div className="row" style={{ gap: 8, marginTop: 10 }}>
        <button className="btn" style={{ flex: 1 }} disabled={pending} onClick={() => void copy()}>{copied ? 'Copied' : 'Copy link'}</button>
        {'share' in navigator && <button className="btn ghost" style={{ flex: 1 }} disabled={pending} onClick={() => void navigator.share({ title: label, url }).catch(() => {})}>Share…</button>}
      </div>
          <p className="hint" style={{ fontSize: 13, marginTop: 12 }}>
            {guest
              ? 'One person uses this link: they give their name and e-mail, and join for 30 days. Everyone here sees who invited them.'
              : `Members open ${label} with this link. For someone who isn't a member yet, turn on guest access above.`}
          </p>
    </Sheet>
  )
}
