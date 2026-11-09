// Invite people to a channel: its link (members open it directly) and QR code; any member can
// make it a guest link instead (for one person and a day, or up to 50 and a month): whoever
// uses it joins this channel only, for 30 days, without a membership (Belfry's /api/channel-invites, guests.py). The chat then shows
// "<name> joined as a guest, invited by <you>".
import { useMemo, useRef, useState } from 'react'
import qrcode from 'qrcode-generator'
import { BELFRY } from '../config'
import { channelIcon, group } from '../core/model'
import { signedFetch } from '../core/sync'
import { Sheet, Btn } from './common'

export function InviteSheet({ id, onClose }: { id: string; onClose: () => void }) {
  const g = group(id)
  // every member may bring a guest (Belfry checks they hold a membership, not a guest pass)
  const admin = true
  const [guest, setGuest] = useState(false)
  const [guestUrl, setGuestUrl] = useState('')
  const [agentUrl, setAgentUrl] = useState('')
  const [people, setPeople] = useState<1 | 50>(1) // one person unless asked
  const [linkDays, setLinkDays] = useState<1 | 7 | 30>(1) // a day unless asked
  const asked = useRef(0)
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
  const fetchGuest = async (n: 1 | 50 = people, days: 1 | 7 | 30 = linkDays) => {
    const mine = ++asked.current // a newer choice wins
    setBusy(true); setError(''); setGuestUrl(''); setAgentUrl('')
    try {
      const r = await signedFetch(`${BELFRY.api}/channel-invites`, 'POST', { channel: id, people: n, link_days: days })
      const d = await r.json().catch(() => ({}))
      if (!r.ok) throw new Error(d.message || 'The guest link could not be made.')
      if (mine !== asked.current) return
      setGuestUrl(String(d.url)); setAgentUrl(String(d.agent_url ?? ''))
    } catch (e) {
      if (mine !== asked.current) return
      const m = e instanceof Error ? e.message : String(e)
      setError(/fetch|network|load/i.test(m) ? 'No connection to the belfry.' : m)
    } finally { if (mine === asked.current) setBusy(false) }
  }
  const pickGuest = (on: boolean) => {
    setGuest(on); setCopied(false)
    if (on && !guestUrl && !busy) void fetchGuest()
    if (!on) setError('')
  }
  const pickPeople = (n: 1 | 50) => { if (n !== people) { setPeople(n); void fetchGuest(n, linkDays) } }
  const pickDays = (d: 1 | 7 | 30) => { if (d !== linkDays) { setLinkDays(d); void fetchGuest(people, d) } }
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
          <span><b>Guest access for 30 days</b><small>For a person or an AI agent who isn't a member: this channel only, nothing else.</small></span>
          <input type="checkbox" role="switch" checked={guest} onChange={(e) => pickGuest(e.target.checked)} />
        </label>
      )}
      {guest && (
        <div className="invite-opts">
          <div role="group" aria-label="How many people"><span>How many</span>
            <div className="seg small">{([[1, 'One person'], [50, 'Up to 50']] as const).map(([n, l]) => <button key={n} type="button" className={people === n ? 'on' : ''} aria-pressed={people === n} onClick={() => pickPeople(n)}>{l}</button>)}</div></div>
          <div role="group" aria-label="How long the link works"><span>Link works for</span>
            <div className="seg small">{([[1, '1 day'], [7, '1 week'], [30, '1 month']] as const).map(([n, l]) => <button key={n} type="button" className={linkDays === n ? 'on' : ''} aria-pressed={linkDays === n} onClick={() => pickDays(n)}>{l}</button>)}</div></div>
        </div>
      )}
      <div className={'qr-box' + (pending ? ' waiting' : '')}>
        <img className="drop-qr" src={qr} alt={pending ? '' : `QR code of the ${guest ? 'guest' : 'channel'} link`} />
        {pending && busy && <span className="qr-over" role="status" aria-label="Making the guest link"><span className="spinner" /></span>}
        {pending && !busy && error && (
          <span className="qr-over error" role="alert">
            <svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" aria-hidden="true"><circle cx="12" cy="12" r="9.5" /><path d="M12 7.5v5.5M12 16.5v.01" /></svg>
            <span className="qr-err">{error}</span>
            <Btn className="btn small" onClick={() => fetchGuest()}>Retry</Btn>
          </span>
        )}
      </div>
      <div className="drop-url"><code>{pending ? (busy ? 'Making the guest link…' : '—') : url}</code></div>
      <div className="row" style={{ gap: 8, marginTop: 10 }}>
        <Btn className="btn" style={{ flex: 1 }} disabled={pending} onClick={() => copy()}>{copied ? 'Copied' : 'Copy link'}</Btn>
        {'share' in navigator && <Btn className="btn ghost" style={{ flex: 1 }} disabled={pending} onClick={() => navigator.share({ title: label, url }).catch(() => {})}>Share…</Btn>}
      </div>
          <p className="hint" style={{ fontSize: 13, marginTop: 12 }}>
            {guest
              ? <>{people === 1 ? 'For one person' : 'For up to 50 people'}, within {linkDays === 1 ? 'a day' : linkDays === 7 ? 'a week' : 'a month'}. They give a name and are in; someone who has an account joins with it. Everyone here sees who invited them.{agentUrl && <> An AI agent reads <a href={agentUrl} target="_blank" rel="noreferrer">its instructions</a> instead.</>}</>
              : `Members open ${label} with this link. For someone who isn't a member yet, turn on guest access above.`}
          </p>
    </Sheet>
  )
}
