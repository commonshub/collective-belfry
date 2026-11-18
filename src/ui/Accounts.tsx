// #/accounts (board home/accounts; the web's limits from the lead, 2026-11-18): opened from the
// person row at the bottom of the Town drawer. "On this browser": this belfry's accounts (a tap
// switches: a full reload into that account) and other belfries (a tap opens their chat, where
// their own sign-in lives). Then Add an account, Scan an invite, and Me.
import { useEffect, useRef, useState } from 'react'
import { BELFRY, NAME } from '../config'
import { accounts, activeId, addAccount, addOtherBelfry, otherBelfries, removeOtherBelfry, switchTo, type Account } from '../core/accounts'
import { useVersion } from '../core/store'
import { me } from '../core/sync'
import { Avatar, back, Btn, Form, go, Sheet } from './common'
import { useCommunityName } from './community'
import { CheckIcon, ChevronRightIcon, CloseIcon, PeopleIcon } from './icons'

/** A face from what the account row kept (its picture, else its first letter): the other
 *  accounts' profiles are in their own databases, not in this one. */
function Face({ a }: { a: { name?: string; picture?: string; domain?: string; email?: string } }) {
  const [broken, setBroken] = useState(false)
  const letter = (a.name || a.email || a.domain || '?').trim().charAt(0).toUpperCase()
  return a.picture && !broken
    ? <img className="acct-face" src={a.picture} alt="" onError={() => setBroken(true)} />
    : <span className="acct-face letter" aria-hidden="true">{letter}</span>
}

export function AccountsScreen() {
  useVersion()
  const community = useCommunityName(NAME)
  const [adding, setAdding] = useState(false)
  const [scanning, setScanning] = useState(false)
  const [switching, setSwitching] = useState('')
  const [, setTick] = useState(0)
  const cur = activeId()
  const mine = accounts().filter((a) => a.pubkey)
  const list: Account[] = mine.some((a) => a.id === cur) ? mine : [{ id: cur, db: '', pubkey: me() }, ...mine]
  const others = otherBelfries()
  return (
    <div className="screen accounts-screen">
      <div className="topbar"><button className="icon-btn" aria-label="Back" onClick={() => back('c')}>‹</button><h1>Accounts</h1></div>
      <div className="scroll pad">
        <div className="place-section"><h3>On this browser</h3></div>
        <div className="home-card">
          {list.map((a) => a.id === cur ? (
            <div key={a.id} className="home-row acct-row on" aria-current="true">
              <Avatar pubkey={me()} size={36} />
              <span className="t"><b>{a.name || 'You'}</b><small>{community} · {a.label || 'Member'}</small></span>
              <CheckIcon />
            </div>
          ) : (
            <Btn key={a.id} className="home-row acct-row" disabled={!!switching} aria-label={`Switch to ${a.name || a.email || 'this account'}`}
              onClick={() => { setSwitching(a.id); switchTo(a.id); return new Promise<void>(() => { /* the page reloads */ }) }}>
              <Face a={a} />
              <span className="t"><b>{a.name || a.email || 'Account'}</b><small>{community} · {a.label || 'Member'}</small></span>
              {switching === a.id ? <span className="spinner" aria-label="Switching" /> : <ChevronRightIcon />}
            </Btn>
          ))}
          {others.map((b) => (
            <div key={b.domain} className="home-row acct-row other">
              <a className="acct-open" href={`https://chat.${b.domain}/`} rel="noopener">
                <Face a={b} />
                <span className="t"><b>{b.name || b.domain}</b><small>{b.domain} · opens its chat</small></span>
              </a>
              <button className="icon-btn acct-forget" aria-label={`Remove ${b.domain} from this list`} onClick={() => { removeOtherBelfry(b.domain); setTick((t) => t + 1) }}><CloseIcon /></button>
            </div>
          ))}
        </div>
        <p className="hint acct-foot">Each account has its own messages and notifications. One is open at a time.</p>
        <div className="home-card" style={{ marginTop: 18 }}>
          <button className="home-row" onClick={() => setAdding(true)}><span className="row-ico"><PeopleIcon /></span><span className="t"><b>Add an account</b><small>Another belfry, or another you here</small></span><ChevronRightIcon /></button>
          <button className="home-row" onClick={() => setScanning(true)}><span className="row-ico"><QrIcon /></span><span className="t"><b>Scan an invite</b></span><ChevronRightIcon /></button>
        </div>
        <div className="home-card" style={{ marginTop: 18 }}>
          <button className="home-row" onClick={() => go('me')}><Avatar pubkey={me()} size={28} /><span className="t"><b>Me</b><small>Profile, sign-in, connections, sign out</small></span><ChevronRightIcon /></button>
        </div>
      </div>
      {adding && <AddAccountSheet onClose={() => setAdding(false)} onAdded={() => { setAdding(false); setTick((t) => t + 1) }} />}
      {scanning && <ScanInviteSheet onClose={() => setScanning(false)} />}
    </div>
  )
}

const QrIcon = () => <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><rect x="4" y="4" width="6" height="6" rx="1" /><rect x="14" y="4" width="6" height="6" rx="1" /><rect x="4" y="14" width="6" height="6" rx="1" /><path d="M14 14h2v2h-2zM18 18h2v2h-2zM14 18h2M18 14h2" /></svg>

function AddAccountSheet({ onClose, onAdded }: { onClose: () => void; onAdded: () => void }) {
  const community = useCommunityName(NAME)
  const [other, setOther] = useState(false)
  const [address, setAddress] = useState('')
  const [error, setError] = useState('')
  if (!other) return (
    <Sheet title="Add an account" onClose={onClose}>
      <button className="pick-row" onClick={() => addAccount()}>
        <span className="t"><b>Here, with another e-mail</b><small>Another account on {community}. You sign in with its e-mail; this one stays on this browser.</small></span><ChevronRightIcon />
      </button>
      <button className="pick-row" onClick={() => setOther(true)}>
        <span className="t"><b>Another belfry</b><small>Its address, like example.org. It opens in its own chat, with its own sign-in.</small></span><ChevronRightIcon />
      </button>
    </Sheet>
  )
  return (
    <Sheet title="Another belfry" onClose={onClose}>
      <Form onSubmit={async (e) => { e.preventDefault(); setError(''); try { await addOtherBelfry(address); onAdded() } catch (x) { setError(x instanceof Error ? x.message : String(x)) } }}>
        <label className="field"><span>Its address</span>
          <input className="input" autoFocus inputMode="url" autoCapitalize="off" autoCorrect="off" spellCheck={false} placeholder="example.org" value={address} onChange={(e) => { setAddress(e.target.value); setError('') }} /></label>
        {error && <p className="error">{error}</p>}
        <button className="btn" disabled={!address.trim()}>Add</button>
        <p className="hint" style={{ marginTop: 10 }}>It shows in Accounts; a tap opens its chat. Nothing of {BELFRY.domain} goes there.</p>
      </Form>
    </Sheet>
  )
}

/** The link a scanned invite QR holds, if it is one: a belfry chat's join link (https only). */
export function inviteFromScan(text: string): string | null {
  let u: URL
  try { u = new URL(text.trim()) } catch { return null }
  if (u.protocol !== 'https:' || !/^chat\.[a-z0-9.-]+$/i.test(u.hostname)) return null
  if (!/^#\/join\/[A-Za-z0-9_-]{8,200}$/.test(u.hash)) return null
  return `https://${u.hostname.toLowerCase()}/${u.hash}`
}

function ScanInviteSheet({ onClose }: { onClose: () => void }) {
  const video = useRef<HTMLVideoElement>(null)
  const [state, setState] = useState<'starting' | 'on' | 'off' | 'bad'>('starting')
  useEffect(() => {
    let stopped = false, timer: ReturnType<typeof setTimeout> | undefined, stream: MediaStream | undefined
    void (async () => {
      try {
        if (!navigator.mediaDevices?.getUserMedia) throw new Error('no camera')
        stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: 'environment' }, audio: false })
        if (stopped || !video.current) return
        video.current.srcObject = stream
        await video.current.play().catch(() => undefined)
        setState('on')
        const { qrReader } = await import('../core/scan')
        const read = await qrReader(video.current)
        const tick = async () => {
          if (stopped) return
          const text = await read()
          const link = text ? inviteFromScan(text) : null
          if (link) {
            stopped = true
            if (new URL(link).origin === location.origin) { location.hash = new URL(link).hash; onClose() } else location.assign(link)
            return
          }
          if (text) setState('bad')
          timer = setTimeout(tick, 250)
        }
        void tick()
      } catch { if (!stopped) setState('off') }
    })()
    return () => { stopped = true; clearTimeout(timer); stream?.getTracks().forEach((t) => t.stop()) }
  }, [onClose])
  return (
    <Sheet title="Scan an invite" onClose={onClose}>
      <div className={'scan-box ' + (state === 'bad' ? 'on' : state)}>
        <video ref={video} playsInline muted />
        {state === 'starting' && <p className="scan-note">Starting the camera…</p>}
        {state === 'off' && <p className="scan-note">The camera isn’t available. Open the invite link instead.</p>}
        {(state === 'on' || state === 'bad') && <span className="scan-frame" aria-hidden="true" />}
      </div>
      {state === 'bad' && <p className="hint" role="status">That code isn’t a belfry invite.</p>}
    </Sheet>
  )
}
