import { useEffect, useState } from 'react'
import { BellIcon } from './icons'
import { BELFRY, NAME } from '../config'
import { isLocalOnly, signedFetch } from '../core/sync'
import { channelIcon, group } from '../core/model'
import { Sheet } from './common'

// Follow a channel's announcements by e-mail, like following an account: as soon as possible
// (a few minutes after each one) or monthly. Belfry keeps the follows (GET/POST
// /api/me/follows, NIP-98) and mails the member's own address; demo mode keeps them here.

export type Frequency = 'asap' | 'monthly'
interface Follows { email: string; map: Map<string, Frequency> }

const KEY = 'belfry-follows-demo'
let cache: Follows | null = null
let loading: Promise<void> | null = null
let failed = ''
const subs = new Set<() => void>()
const changed = () => subs.forEach((f) => f())
const url = () => `${BELFRY.pass}/api/me/follows`

async function load() {
  failed = ''
  try {
    if (isLocalOnly()) {
      let saved: Record<string, Frequency> = {}
      try { saved = JSON.parse(localStorage.getItem(KEY) || '{}') } catch { /* nothing kept */ }
      cache = { email: 'demo@example.org', map: new Map(Object.entries(saved)) }
      return
    }
    const r = await signedFetch(url())
    const d = await r.json().catch(() => ({}))
    if (!r.ok) throw new Error(d.message || `The belfry answered ${r.status}.`)
    const map = new Map<string, Frequency>()
    for (const f of d.follows ?? []) if (f.state === 'active') map.set(f.channel, f.frequency)
    cache = { email: d.email ?? '', map }
  } catch (e) {
    failed = e instanceof Error ? e.message : String(e)
  } finally {
    loading = null
    changed()
  }
}

function useFollows() {
  const [, bump] = useState(0)
  useEffect(() => {
    const f = () => bump((n) => n + 1)
    subs.add(f)
    if (!cache && !loading) loading = load()
    return () => { subs.delete(f) }
  }, [])
  return cache
}

async function setFollow(channel: string, choice: Frequency | 'stop') {
  if (isLocalOnly()) {
    if (choice === 'stop') cache!.map.delete(channel)
    else cache!.map.set(channel, choice)
    try { localStorage.setItem(KEY, JSON.stringify(Object.fromEntries(cache!.map))) } catch { /* this visit only */ }
    changed()
    return
  }
  const r = await signedFetch(url(), 'POST', { channel, frequency: choice })
  const d = await r.json().catch(() => ({}))
  if (!r.ok) throw new Error(d.message || 'Not saved.')
  if (d.state === 'active') cache!.map.set(channel, d.frequency)
  else cache!.map.delete(channel)
  changed()
}

/** The bell in a channel's (or event's) top bar: filled when you follow its announcements. */
export function FollowButton({ channel }: { channel: string }) {
  const f = useFollows()
  const [open, setOpen] = useState(false)
  const on = !!f?.map.has(channel)
  return (
    <>
      <button className={'icon-btn follow-btn' + (on ? ' on' : '')} aria-label={on ? 'Following its announcements' : 'Follow its announcements'}
        aria-pressed={on} onClick={() => setOpen(true)}><BellIcon on={on} /></button>
      {open && <FollowSheet channel={channel} onClose={() => setOpen(false)} />}
    </>
  )
}

const OPTIONS: [Frequency | 'stop', string, string][] = [
  ['asap', 'As soon as possible', 'A few minutes after each announcement'],
  ['monthly', 'Monthly', 'On the 1st, when there was something new'],
  ['stop', 'Not following', 'No e-mails'],
]

function FollowSheet({ channel, onClose }: { channel: string; onClose: () => void }) {
  const f = useFollows()
  const g = group(channel)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const current: Frequency | 'stop' = f?.map.get(channel) ?? 'stop'
  const label = g ? `${channelIcon(g)}${g.isPod || g.isEvent || g.isProposal ? ' ' : ''}${g.name}` : `#${channel}`
  const pick = async (c: Frequency | 'stop') => {
    if (c === current) return onClose()
    setBusy(true); setError('')
    try { await setFollow(channel, c); onClose() } catch (e) { setError(e instanceof Error ? e.message : String(e)) } finally { setBusy(false) }
  }
  return (
    <Sheet title="Announcements" onClose={onClose}>
      <p style={{ margin: '0 0 12px' }}>Get the announcements of <b>{label}</b> by e-mail{f?.email ? <> at <b>{f.email}</b></> : null}.
        Only its admins post announcements.</p>
      {!f && !failed && <p className="hint">Loading…</p>}
      {failed && <p className="error">{failed}</p>}
      {f && (
        <div className="choice" style={{ gridTemplateColumns: '1fr' }}>
          {OPTIONS.map(([c, title, hint]) => (
            <button key={c} type="button" disabled={busy} className={c === current ? 'on' : ''} onClick={() => void pick(c)}>
              <b>{title}</b><small>{hint}</small>
            </button>
          ))}
        </div>
      )}
      {error && <p className="error">{error}</p>}
      <p className="hint" style={{ fontSize: 13, marginTop: 12 }}>{NAME} sends these mails; every one has links to change or stop.</p>
    </Sheet>
  )
}
