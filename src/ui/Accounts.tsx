import { active as sealedActive, lastAutoPass, lastStorePass, newLinkCode } from '../core/sealedrt'
import { clearLinked, linkedByScan } from '../core/sealed'
import { LinkedByScan } from './Sealed'
// #/accounts (board home/accounts; the web's limits from the lead, 2026-11-18): opened from the
// person row at the bottom of the Town drawer. "On this browser": this belfry's accounts (a tap
// switches: a full reload into that account) and other belfries (a tap opens their chat, where
// their own sign-in lives). Then Add an account, Scan an invite, and Me.
import { useEffect, useMemo, useRef, useState } from 'react'
import qrcode from 'qrcode-generator'
import { profiles } from '../core/model'
import { isGuest } from '../core/invite'
import { BELFRY, NAME } from '../config'
import { accounts, activeId, addAccount, addOtherBelfry, otherBelfries, removeOtherBelfry, switchTo, type Account } from '../core/accounts'
import { useVersion } from '../core/store'
import { signerCalls, signerCallsIn } from '../core/auth'
import { me } from '../core/sync'
import { allSocks, sendTraces } from '../core/conn'
import { Avatar, back, Btn, Form, go, Sheet } from './common'
import { useCommunityName } from './community'
import { CheckIcon, ChevronRightIcon, CloseIcon, GlobeIcon, PeopleIcon } from './icons'
import { Arms } from './Login'

/** A face from what the account row kept (its picture, else its first letter): the other
 *  accounts' profiles are in their own databases, not in this one. */
function Face({ a }: { a: { name?: string; picture?: string; domain?: string; email?: string } }) {
  const [broken, setBroken] = useState(false)
  const letter = (a.name || a.email || a.domain || '?').trim().charAt(0).toUpperCase()
  return a.picture && !broken
    ? <img className="acct-face" src={a.picture} alt="" onError={() => setBroken(true)} />
    : <span className="acct-face letter" aria-hidden="true">{letter}</span>
}

/** LINK: opened from the only-device question (canvas v117): Link your phone at once. */
export function AccountsScreen({ link = false }: { link?: boolean } = {}) {
  useVersion()
  const community = useCommunityName(NAME)
  const [adding, setAdding] = useState(false)
  const [scanning, setScanning] = useState(false)
  const [linking, setLinking] = useState(link && !!myUsername())
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
            <div key={a.id}>
              <div className="home-row acct-row on" aria-current="true">
                <Avatar pubkey={me()} size={36} />
                <span className="t"><b>{a.name || 'You'}</b><small>{community} · {a.label || 'Member'}</small></span>
                <CheckIcon />
              </div>
              {/* board home/accounts-web (canvas v55): open this account in the Belfry app */}
              {myUsername() && <button className="home-row link-phone-row" onClick={() => setLinking(true)}>
                <span className="row-ico"><PhoneIcon /></span><span className="t"><b>Link your phone</b><small>Open this account in the Belfry app</small></span><ChevronRightIcon />
              </button>}
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
          <button className="home-row" onClick={() => go('about')}><span className="row-ico about-mark"><Arms /></span><span className="t"><b>About Belfry</b><small>Version {__CHAT_VERSION__.split(' · ')[0]}</small></span><ChevronRightIcon /></button>
        </div>
      </div>
      {adding && <AddAccountSheet onClose={() => setAdding(false)} onAdded={() => { setAdding(false); setTick((t) => t + 1) }} />}
      {scanning && <ScanInviteSheet onClose={() => setScanning(false)} />}
      {linking && <LinkPhoneSheet onClose={() => setLinking(false)} />}
    </div>
  )
}

const PhoneIcon = () => <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><rect x="7" y="3" width="10" height="18" rx="2.5" /><path d="M11 18h2" /></svg>

/** My username on this belfry: the name part of my nip05 when it is name@<this domain> (none for
 *  a guest, or an address elsewhere: then no "Link your phone"). */
export function myUsername(): string {
  if (isGuest(me())) return ''
  const n = (profiles().get(me())?.nip05 ?? '').toLowerCase()
  const [u, d] = n.split('@')
  return d === BELFRY.domain.toLowerCase() && /^[a-z0-9][a-z0-9._-]{0,29}$/.test(u ?? '') ? u : ''
}

/** Link your phone (boards home/link-phone, -laptop): a QR of https://chat.<domain>/login/<username>,
 *  nothing else in it (no secret, no token); the app (or a browser) logs in with a code by e-mail. */
function LinkPhoneSheet({ onClose }: { onClose: () => void }) {
  useVersion()
  const community = useCommunityName(NAME)
  const u = myUsername()
  // sealed.md §5.2: a fresh one-time secret each time this opens (memory only, 15 min): the new
  // device proves it scanned this code, so no Allow prompt follows
  const code = useMemo(() => (sealedActive() ? newLinkCode() : ''), [])
  const url = `https://chat.${BELFRY.domain}/login/${u}` + (code ? `#l=${code}` : '')
  const name = profiles().get(me())?.name || u
  const qr = useMemo(() => {
    const q = qrcode(0, 'M'); q.addData(url); q.make()
    return 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(q.createSvgTag({ cellSize: 6, margin: 2, scalable: true }))
  }, [url])
  const linked = linkedByScan()
  // board messages/encryption-link-qr: the new device scanned this code, so no Allow follows
  if (linked) return <Sheet title="Link your phone" onClose={() => { clearLinked(); onClose() }}><LinkedByScan name={linked.name} onDone={() => { clearLinked(); onClose() }} /></Sheet>
  return (
    <Sheet title="Link your phone" onClose={onClose}>
      <div className="link-phone">
        <p>Scan this with the Belfry app to open {community} on your phone as {name} (@{u}).</p>
        <div className="qr-box"><img className="drop-qr" src={qr} alt={`QR code of https://chat.${BELFRY.domain}/login/${u}`} /></div>
        <p className="hint">Don’t have the app yet? <a href="https://belfry.sh/android" target="_blank" rel="noopener noreferrer">Android</a> · <a href="https://belfry.sh/iphone" target="_blank" rel="noopener noreferrer">iPhone</a></p>
        <p className="hint small">For your security, we’ll e-mail you a 6-digit code to confirm on your phone.</p>
      </div>
    </Sheet>
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

// ---- #/about: About Belfry (board home/about; the web shows its version only, no updates row) ----

const HOME = 'belfry.sh' // the belfry of Belfry

/** About Belfry: the mark and the chat's version; belfry.sh; the belfry of Belfry (join it, switch
 *  to it when another account of this browser is there, or "you're in it" when this one is). */
export function AboutBelfry() {
  useVersion()
  const here = BELFRY.domain === HOME
  const elsewhere = !here && otherBelfries().some((b) => b.domain === HOME)
  return (
    <div className="screen about-screen">
      <div className="topbar"><button className="icon-btn" aria-label="Back to Accounts" onClick={() => back('accounts')}>‹</button><h1>About Belfry</h1></div>
      <div className="scroll pad">
        <div className="about-head">
          <span className="about-icon"><Arms /></span>
          <h2>Belfry for the web</h2>
          <p className="hint">Version {__CHAT_VERSION__}</p>
        </div>
        <div className="place-section"><h3>Belfry</h3></div>
        <div className="home-card">
          <a className="home-row" href={`https://${HOME}`} target="_blank" rel="noopener"><span className="row-ico"><GlobeIcon /></span><span className="t"><b>{HOME}</b><small>The project’s website</small></span><ChevronRightIcon /></a>
          {here
            ? <button className="home-row" onClick={() => go('c/general')}><span className="row-ico"><PeopleIcon /></span><span className="t"><b>You’re in it with this account</b><small>The belfry of Belfry: open its Townsquare</small></span><ChevronRightIcon /></button>
            : elsewhere
              ? <a className="home-row" href={`https://chat.${HOME}/`}><span className="row-ico"><PeopleIcon /></span><span className="t"><b>Switch to the belfry of Belfry</b><small>Another account of this browser is in {HOME}</small></span><ChevronRightIcon /></a>
              : <a className="home-row" href={`https://chat.${HOME}/apply`}><span className="row-ico"><PeopleIcon /></span><span className="t"><b>Join the belfry of Belfry</b><small>Apply to join {HOME}</small></span><ChevronRightIcon /></a>}
        </div>
        <div className="home-card">
          <button className="home-row" onClick={() => go('about/diagnostics')}><span className="t"><b>Diagnostics</b><small>How the last messages were sent</small></span><ChevronRightIcon /></button>
        </div>
        <p className="hint about-foot">Belfry is open source software a community runs on its own server.</p>
      </div>
    </div>
  )
}

// ---- #/about/diagnostics: the connections and the last 20 sends, timed (no content) ----

/** About Belfry › Diagnostics: each socket (up, quiet for how long) and the last 20 sends, step by
 *  step with their times. No message content, no keys: what someone can show when sending is slow. */
export function Diagnostics() {
  useVersion()
  const [, tick] = useState(0)
  useEffect(() => { const t = setInterval(() => tick((n) => n + 1), 2000); return () => clearInterval(t) }, [])
  const traces = sendTraces()
  const auto = lastAutoPass(), pass = lastStorePass()
  const build = [...document.querySelectorAll<HTMLScriptElement>('script[type=module][src]')].map((x) => /index-([A-Za-z0-9_-]+)\.js/.exec(x.src)?.[1]).find(Boolean)
  const secs = (ms: number) => (ms / 1000).toFixed(1) + ' s'
  return (
    <div className="screen about-screen">
      <div className="topbar"><button className="icon-btn" aria-label="Back to About Belfry" onClick={() => back('about')}>‹</button><h1>Diagnostics</h1></div>
      <div className="scroll pad diag">
        <div className="place-section"><h3>Connections</h3></div>
        <ul className="diag-list">
          {allSocks().map((s) => (
            <li key={s.url}><b>{s.url.replace(/^wss?:\/\//, '').replace(/\?.*$/, '')}</b><small>{s.connected ? `Connected · last heard ${secs(s.quietFor())} ago` : 'Not connected'}{s.drops ? ` · dropped ${s.drops}× (last: ${s.lastDrop})` : ''}</small></li>
          ))}
        </ul>
        <div className="place-section"><h3>New devices</h3></div>
        <ul className="diag-list">
          <li className="diag-auto"><b>Last automatic add</b><small>{auto ? `${new Date(auto.at).toLocaleString()} · ${auto.name}: ${auto.result}` : 'No new device has asked in this browser.'}</small></li>
          <li><b>Signer requests</b><small>{signerCallsIn(1000)} in the first second · {signerCallsIn(10_000)} in the first 10 s · {signerCalls.length} since the page opened</small></li>
          <li><b>Last check</b><small>{pass ? new Date(pass).toLocaleTimeString() : 'Not yet'}{build ? ` · build ${build}` : ''}</small></li>
        </ul>
        <div className="place-section"><h3>Last sends</h3></div>
        {!traces.length && <p className="hint">Nothing sent from this browser yet.</p>}
        <ul className="diag-list">
          {traces.map((t, i) => (
            <li key={i}>
              <b>{t.what}</b>
              <small>{new Date(t.at).toLocaleTimeString()} · {t.result === 'sent' ? 'sent' : 'not sent: ' + t.result} in {secs(t.ms)}{t.tries > 1 ? ` · try ${t.tries}` : ''}</small>
              <small className="diag-steps">{t.steps.map(([n, ms]) => `${n} ${secs(ms)}`).join(' → ') || '—'}</small>
            </li>
          ))}
        </ul>
      </div>
    </div>
  )
}
