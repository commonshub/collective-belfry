import { useEffect, useState } from 'react'
import { nip19 } from 'nostr-tools'
import { BELFRY, NAME } from '../config'
import type { Session } from '../core/auth'
import { me, signedFetch } from '../core/sync'
import { back } from './common'

// Your key and every session open on it (Belfry: GET /api/me/sessions, NIP-98), each one
// an app or device that can sign as you. End any of them here.

interface Conn {
  application_name: string
  device?: string
  redirect_origin: string
  bunker_pubkey: string
  created_at: string
  last_activity: string | null
}

export const shortNpub = (npub: string) => (npub.length > 20 ? `${npub.slice(0, 10)}…${npub.slice(-6)}` : npub)

export function CopyKey({ value, label }: { value: string; label?: string }) {
  const [done, setDone] = useState(false)
  const copy = async () => {
    try { await navigator.clipboard.writeText(value) } catch {
      const t = document.createElement('textarea'); t.value = value; document.body.appendChild(t); t.select(); document.execCommand('copy'); t.remove()
    }
    setDone(true)
    setTimeout(() => setDone(false), 1500)
  }
  return (
    <button className="key-copy" onClick={copy} aria-label={`Copy ${label ?? 'key'}`}>
      <code>{shortNpub(value)}</code>
      <span className="hint">{done ? 'Copied' : 'Copy'}</span>
    </button>
  )
}

const day = (s: string | null) => (s ? new Date(s).toLocaleDateString([], { day: 'numeric', month: 'short', year: 'numeric' }) : '')

export function Connections({ session }: { session: Session }) {
  const npub = me() ? nip19.npubEncode(me()) : ''
  const mine = (session.bunkerUrl ?? '').replace(/^bunker:\/\//, '').split('?')[0]
  const [rows, setRows] = useState<Conn[] | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState('')
  const url = `${BELFRY.api}/me/sessions`

  const load = async () => {
    setError('')
    try {
      const r = await signedFetch(url)
      const d = await r.json().catch(() => ({}))
      if (!r.ok) throw new Error(d.message || `The belfry answered ${r.status}.`)
      setRows(d.sessions ?? [])
    } catch (e) { setError(e instanceof Error ? e.message : String(e)) }
  }
  useEffect(() => { if (session.backend === 'belfry') void load() }, [])

  const end = async (c: Conn) => {
    if (!confirm(`End the session of ${c.application_name}? It will no longer sign as you.`)) return
    setBusy(c.bunker_pubkey)
    try {
      const r = await signedFetch(url + '/revoke', 'POST', { bunker_pubkey: c.bunker_pubkey })
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).message || 'Not ended.')
      await load()
    } catch (e) { setError(e instanceof Error ? e.message : String(e)) } finally { setBusy('') }
  }

  return (
    <div className="screen">
      <div className="topbar">
        <button className="icon-btn" aria-label="Back" onClick={() => back('me')}>‹</button>
        <h1>Connections</h1>
      </div>
      <div className="scroll pad">
        <div className="card">
          <div className="hint" style={{ marginBottom: 6 }}>Your key</div>
          <CopyKey value={npub} label="your key" />
          <div className="hint" style={{ fontSize: 13, marginTop: 8 }}>
            {session.backend === 'demo' ? 'Demo key, kept on this device only.' : `${NAME} keeps this key for you. The sessions below sign with it.`}
          </div>
        </div>
        <div className="section-title" style={{ margin: '18px 4px 8px' }}>Sessions</div>
        {session.backend === 'demo' && <p className="hint">Demo mode has no sessions.</p>}
        {error && <p className="error">{error}</p>}
        {rows === null && session.backend === 'belfry' && !error && <p className="hint">Loading…</p>}
        {rows?.length === 0 && <p className="hint">No sessions.</p>}
        {rows?.map((c) => {
          const here = c.bunker_pubkey === mine
          return (
            <div key={c.bunker_pubkey} className="card row" style={{ justifyContent: 'space-between', gap: 12 }}>
              <div style={{ minWidth: 0 }}>
                <div style={{ fontWeight: 600, overflowWrap: 'anywhere' }}>{c.application_name}{here && <span className="badge" style={{ marginLeft: 8 }}>This app</span>}</div>
                {c.device && <div style={{ fontSize: 14 }}>{c.device}</div>}
                <div className="hint" style={{ fontSize: 13 }}>
                  Since {day(c.created_at)}{c.last_activity ? ` · last used ${day(c.last_activity)}` : ''}
                </div>
              </div>
              {!here && <button className="btn ghost small" disabled={busy === c.bunker_pubkey} onClick={() => end(c)}>End</button>}
            </div>
          )
        })}
      </div>
    </div>
  )
}
