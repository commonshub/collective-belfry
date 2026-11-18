import { useEffect, useState } from 'react'
import { BellIcon } from './icons'
import { BELFRY, NAME } from '../config'
import { isLocalOnly, signedFetch } from '../core/sync'
import { channelIcon, group } from '../core/model'
import { Sheet } from './common'
import { choiceOf, setChoice, useNotify, type Choice } from '../core/notify'

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
const url = () => `${BELFRY.api}/me/follows`

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
  return <Sheet title="Announcements" onClose={onClose}><FollowOptions channel={channel} onDone={onClose} /></Sheet>
}

/** The follow choices for CHANNEL (the bell's sheet; a channel's Notifications screen). */
export function FollowOptions({ channel, onDone }: { channel: string; onDone?: () => void }) {
  const f = useFollows()
  const g = group(channel)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const current: Frequency | 'stop' = f?.map.get(channel) ?? 'stop'
  const label = g ? `${channelIcon(g)}${g.isPod || g.isEvent || g.isProposal ? ' ' : ''}${g.name}` : `#${channel}`
  const pick = async (c: Frequency | 'stop') => {
    if (c === current) return onDone?.()
    setBusy(true); setError('')
    try { await setFollow(channel, c); onDone?.() } catch (e) { setError(e instanceof Error ? e.message : String(e)) } finally { setBusy(false) }
  }
  return (
    <>
      <p style={{ margin: '0 0 12px' }}>Get the announcements of <b>{label}</b> by e-mail{f?.email ? <> at <b>{f.email}</b></> : null}.
        Only its admins post announcements.</p>
      {!f && !failed && <p className="hint">Loading…</p>}
      {failed && <p className="error">{failed}</p>}
      {f && (
        <div className="choice follow-choice" role="radiogroup" aria-label="Announcements by e-mail" style={{ gridTemplateColumns: '1fr' }}>
          {OPTIONS.map(([c, title, hint]) => (
            <button key={c} type="button" role="radio" aria-checked={c === current} disabled={busy} className={c === current ? 'on' : ''} onClick={() => void pick(c)}>
              <b>{title}</b><small>{hint}</small>
            </button>
          ))}
        </div>
      )}
      {error && <p className="error">{error}</p>}
      <p className="hint" style={{ fontSize: 13, marginTop: 12 }}>{NAME} sends these mails; every one has links to change or stop.</p>
    </>
  )
}

/** Following CHANNEL: "As soon as possible", "Monthly", or "" (for a row's value). */
export function useFollowing(channel: string): string {
  const f = useFollows()
  const c = f?.map.get(channel)
  return c === 'asap' ? 'As soon as possible' : c === 'monthly' ? 'Monthly' : f ? 'Off' : ''
}

// ---- a channel's notifications: one sheet (Xavier, 2026-11-18, canvas "Notif-C") --------------
// Three large choices, applied on tap (no Save), then the e-mail of its announcements. The three
// are the belfry's push levels for this channel (core/notify.ts), the switch is the e-mail follow
// above (asap; a monthly follow shows as on and stays monthly).

const RingIcon = () => <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M6 17v-6a6 6 0 0 1 12 0v6l2 2H4l2-2zM10 21h4M2 9a10 10 0 0 1 3-6M22 9a10 10 0 0 0-3-6" /></svg>
const AtIcon = () => <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="4" /><path d="M16 12v1.5a2.5 2.5 0 0 0 5 0V12a9 9 0 1 0-4 7.5" /></svg>
const MuteIcon = () => <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M6 17v-6a6 6 0 0 1 10-4.5M18 11v6l2 2H8M10 21h4M3 3l18 18" /></svg>

const CHOICES: [Choice, () => JSX.Element, string, string][] = [
  ['all', RingIcon, 'Everything', 'Every new message'],
  ['mine', AtIcon, 'When it’s for me', 'Mentions, replies to me, announcements'],
  ['off', MuteIcon, 'Nothing', 'I’ll look when I open the channel'],
]

/** "Notify me about #x": the pull-down's Notifications row. */
export function NotifySheet({ channel, onClose }: { channel: string; onClose: () => void }) {
  return <Sheet title={`Notify me about ${channelLabel(channel)}`} onClose={onClose}><NotifyOptions channel={channel} /></Sheet>
}

const channelLabel = (channel: string) => { const g = group(channel); return g ? `${channelIcon(g)}${g.isPod || g.isEvent || g.isProposal ? ' ' : ''}${g.name}` : `#${channel}` }

/** The three choices and the e-mail switch for CHANNEL. */
export function NotifyOptions({ channel }: { channel: string }) {
  const n = useNotify()
  const f = useFollows()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const current = n.state ? choiceOf(n.state.channels[channel]) : null
  const pick = async (c: Choice) => {
    if (c === current || busy) return
    setBusy(true); setError('')
    try { await setChoice(channel, c) } catch (e) { setError(e instanceof Error ? e.message : String(e)) } finally { setBusy(false) }
  }
  const follow = f?.map.get(channel)
  const [mailBusy, setMailBusy] = useState(false)
  const toggleMail = async (on: boolean) => {
    setMailBusy(true); setError('')
    try { await setFollow(channel, on ? 'asap' : 'stop') } catch (e) { setError(e instanceof Error ? e.message : String(e)) } finally { setMailBusy(false) }
  }
  return (
    <div className="notify">
      {!n.state && !n.failed && <p className="hint">Loading…</p>}
      {n.failed && <p className="error" role="alert">{n.failed}</p>}
      {n.state && !n.state.unsupported && (
        <div className="notify-opts" role="radiogroup" aria-label={`Notify me about ${channelLabel(channel)}`}>
          {CHOICES.map(([c, Icon, title, hint]) => (
            <button key={c} type="button" role="radio" aria-checked={c === current}
              className={'notify-opt' + (c === current ? ' on' : '')} disabled={busy} onClick={() => void pick(c)}>
              <span className="no-ico"><Icon /></span>
              <span className="t"><b>{title}{c === 'mine' && <span className="no-default"> (the default here)</span>}</b><small>{hint}</small></span>
            </button>
          ))}
        </div>
      )}
      {n.state && !n.state.unsupported && n.state.devices === 0 && <p className="hint notify-phone">These choices are for the Belfry app on your phone.</p>}
      <label className="switch-row notify-mail">
        <span><b>Also e-mail me its announcements</b><small>What admins mark as an announcement{follow === 'monthly' ? ' · once a month' : ''}</small></span>
        <input type="checkbox" role="switch" aria-label="E-mail me its announcements" checked={!!follow} disabled={!f || mailBusy} onChange={(e) => void toggleMail(e.target.checked)} />
      </label>
      {failed && <p className="error">{failed}</p>}
      {error && <p className="error" role="alert">{error}</p>}
    </div>
  )
}

/** For the menu's row: what is chosen ("Everything", "For me", "Nothing"; "" while unknown) and
 *  whether anything reaches me (the bell drawn filled). */
export function useNotifyRow(channel: string): { value: string; on: boolean } {
  const n = useNotify()
  const f = useFollows()
  const mail = !!f?.map.has(channel)
  if (!n.state || n.state.unsupported) return { value: mail ? 'E-mail' : n.state ? 'Off' : '', on: mail }
  const c = choiceOf(n.state.channels[channel])
  return { value: c === 'all' ? 'Everything' : c === 'off' ? 'Nothing' : 'For me', on: c !== 'off' || mail }
}
