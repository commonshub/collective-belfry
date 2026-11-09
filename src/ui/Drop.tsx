import { useMemo, useState } from 'react'
import qrcode from 'qrcode-generator'
import { createDropLink, dropUrl, endDropLink } from '../core/actions'
import { dropLinks, group, type DropLink } from '../core/model'
import { me } from '../core/sync'
import { useVersion } from '../core/store'
import { Sheet, Btn } from './common'

// Drop box links (Files tab): anyone with the link or its QR code uploads photos and files to
// this channel, without an account; they may add their name, or sign in to post as themselves.
// The link is a NIP-78 event in the group with a NIP-40 expiry; Belfry serves the upload page.

const DAYS: [number, string][] = [[1, '1 day'], [7, '1 week'], [30, '1 month']]
const until = (ts: number) => new Date(ts * 1000).toLocaleString([], { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })

export function DropBox({ groupId }: { groupId: string }) {
  useVersion()
  const links = dropLinks(groupId)
  const [creating, setCreating] = useState(false)
  const [sharing, setSharing] = useState<string | null>(null)
  return (
    <div className="drop-box">
      <div className="row" style={{ justifyContent: 'space-between', gap: 12 }}>
        <div style={{ minWidth: 0 }}>
          <div style={{ fontWeight: 600 }}>📥 Drop box</div>
          <div className="hint" style={{ fontSize: 13 }}>A link or QR code anyone can use to upload here</div>
        </div>
        <button className="btn small" onClick={() => setCreating(true)}>New link</button>
      </div>
      {links.map((l) => <LinkRow key={l.token} l={l} onShare={() => setSharing(l.token)} />)}
      {creating && <NewDropSheet groupId={groupId} onClose={() => setCreating(false)} onMade={(t) => { setCreating(false); setSharing(t) }} />}
      {sharing && <ShareSheet groupId={groupId} token={sharing} onClose={() => setSharing(null)} />}
    </div>
  )
}

function LinkRow({ l, onShare }: { l: DropLink; onShare: () => void }) {
  const mine = l.author === me()
  return (
    <div className="drop-link">
      <button className="link-main" onClick={onShare}>
        <b>Until {until(l.expires)}</b>
        <small>{l.login ? 'Signed-in members only' : 'Anyone with the link'}{l.ev.pending ? ' · sending…' : ''}</small>
      </button>
      <button className="btn ghost small" onClick={onShare}>Share</button>
      {mine && <button className="btn ghost small" onClick={() => { if (confirm('End this drop link? It stops working at once.')) void endDropLink(l.groupId, l.ev, l.token) }}>End</button>}
    </div>
  )
}

function NewDropSheet({ groupId, onClose, onMade }: { groupId: string; onClose: () => void; onMade: (token: string) => void }) {
  const [days, setDays] = useState(7)
  const [login, setLogin] = useState(false)
  const [busy, setBusy] = useState(false)
  const g = group(groupId)
  return (
    <Sheet title="New drop link" onClose={onClose}>
      <p style={{ margin: '0 0 12px' }}>People with the link upload photos and files to <b>{g?.name ?? groupId}</b>. They can add their name afterwards, or sign in to post as themselves.</p>
      <div className="field"><span>Works for</span>
        <div className="chips">{DAYS.map(([d, label]) => <button key={d} type="button" className={'chip' + (d === days ? ' on' : '')} onClick={() => setDays(d)}>{label}</button>)}</div>
      </div>
      <div className="choice">
        <button type="button" className={!login ? 'on' : ''} onClick={() => setLogin(false)}><b>Anyone</b><small>No account needed</small></button>
        <button type="button" className={login ? 'on' : ''} onClick={() => setLogin(true)}><b>Members</b><small>Must sign in to upload</small></button>
      </div>
      <Btn className="btn" disabled={busy} onClick={async () => { setBusy(true); try { onMade(await createDropLink(groupId, days, login)) } finally { setBusy(false) } }}>Create link</Btn>
    </Sheet>
  )
}

function ShareSheet({ groupId, token, onClose }: { groupId: string; token: string; onClose: () => void }) {
  useVersion()
  const url = dropUrl(token)
  const l = dropLinks(groupId).find((x) => x.token === token)
  const [copied, setCopied] = useState(false)
  const qr = useMemo(() => {
    const q = qrcode(0, 'M')
    q.addData(url)
    q.make()
    return 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(q.createSvgTag({ cellSize: 6, margin: 2, scalable: true }))
  }, [url])
  const copy = async () => {
    try { await navigator.clipboard.writeText(url) } catch {
      const t = document.createElement('textarea'); t.value = url; document.body.appendChild(t); t.select(); document.execCommand('copy'); t.remove()
    }
    setCopied(true); setTimeout(() => setCopied(false), 1500)
  }
  return (
    <Sheet title="Drop link" onClose={onClose}>
      <img className="drop-qr" src={qr} alt="QR code of the drop link" />
      <div className="drop-url"><code>{url}</code></div>
      <div className="row" style={{ gap: 8, marginTop: 10 }}>
        <Btn className="btn" style={{ flex: 1 }} onClick={() => copy()}>{copied ? 'Copied' : 'Copy link'}</Btn>
        {'share' in navigator && <Btn className="btn ghost" style={{ flex: 1 }} onClick={() => navigator.share({ title: 'Drop box', url }).catch(() => {})}>Share…</Btn>}
      </div>
      {l && <p className="hint" style={{ fontSize: 13, marginTop: 12 }}>Works until {until(l.expires)} · {l.login ? 'signed-in members only' : 'anyone with the link'}. End it any time from the Files tab.</p>}
    </Sheet>
  )
}
