// Sealed conversations, the screens (canvas "Messages · encryption", approved 20 Nov). Data:
// core/sealed.ts (demo only until the lead's docs/sealed.md). Never a key, npub or id as text;
// only my own devices are named.
import { useEffect, useRef, useState } from 'react'
import { convName, isMail, mailAddress, peers, type Conv } from '../core/dm'
import { displayName, realName } from '../core/model'
import { touch, useVersion } from '../core/store'
import { FULL, active as sealedOn, mySessions } from '../core/sealedrt'
import { me } from '../core/sync'
import { replaceHash } from '../core/nav'
import { canRemoveDevices, dismissAsking, sealedAt, activeWord, allowAsking, asking, refuseAsking, removeDevice, shareHistory, anyLockedHere, dayMonth, devicesWord, isSealed, myDevices, recentChange, theirDevices, whenWord, type DeviceKind, type DeviceLine, type MyDevice } from '../core/sealed'
import { MailIcon } from './icons'
import { Avatar, Sheet, SheetOrPopover, go, up, type Anchor } from './common'

export const encConv = (c: Conv) => encodeURIComponent(c)

const Ico = ({ children, size = 14 }: { children: React.ReactNode; size?: number }) => (
  <svg className="ico" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2.2}
    strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false">{children}</svg>
)
export const SealIcon = ({ size = 14 }: { size?: number }) => <Ico size={size}><rect x="5" y="10.5" width="14" height="10" rx="2" /><path d="M8 10.5V8a4 4 0 0 1 8 0v2.5" /></Ico>
export const DeviceIcon = ({ kind, size = 14 }: { kind?: DeviceKind; size?: number }) =>
  kind === 'computer' ? <Ico size={size}><rect x="4" y="5" width="16" height="11" rx="1.5" /><path d="M2 19h20" /></Ico>
    : kind === 'tablet' ? <Ico size={size}><rect x="5" y="2.5" width="14" height="19" rx="2.5" /><path d="M11 18.5h2" /></Ico>
      : <Ico size={size}><rect x="7" y="2.5" width="10" height="19" rx="2.5" /><path d="M11 18.5h2" /></Ico>
const Chev = () => <Ico size={10}><path d="M9 5l7 7-7 7" /></Ico>

/** The small lock by the name: opens the Encryption screen (the face and name still open the profile). */
export function LockButton({ conv }: { conv: Conv }) {
  return (
    <button className="seal-lock" aria-label="End-to-end encrypted: see who can read it" title="End-to-end encrypted" onClick={() => go('dm/' + encConv(conv) + '/lock')}>
      <SealIcon size={13} />
    </button>
  )
}

/** A device line, in the style of a channel's join line. Mine name the device; other people's
 *  never do. The amber one (a new device that can't read earlier messages yet) opens the Share
 *  history sheet; the others the Encryption screen. Once shared, it reads "<name> added a device". */
export function DeviceLineRow({ conv, l, onShare }: { conv: Conv; l: DeviceLine; onShare: (l: DeviceLine) => void }) {
  const mine = l.who === me()
  const first = mine ? 'You' : displayName(l.who).split(' ')[0]
  const amber = l.what === 'new-locked' && !l.shared && !mine
  const what = l.what === 'removed' ? 'removed a device' : 'added a device'
  return (
    <button className={'seal-line' + (amber ? ' warn' : '')} onClick={() => (amber ? onShare(l) : go('dm/' + encConv(conv) + '/lock'))}>
      {mine ? <DeviceIcon kind={l.kind} /> : <SealIcon />}
      <span>{amber
        ? <><b>{first} signed in on a new device</b> that can’t read earlier messages yet</>
        : <><b>{first} {what}</b>{mine && l.device ? <> · {l.device}</> : null}</>}</span>
      <Chev />
    </button>
  )
}

const EyeOff = () => <Ico size={18}><path d="M3 3l18 18M10.6 6.1A9.8 9.8 0 0 1 12 6c5 0 8.5 4 9.5 6-.4.9-1.3 2.2-2.6 3.4M6.6 7.6C4.6 8.9 3.2 10.7 2.5 12c1 2 4.5 6 9.5 6 1.6 0 3-.4 4.3-1" /></Ico>
const KeyIco = () => <Ico size={18}><circle cx="8" cy="15" r="4" /><path d="M11 12l9-9M16 7l3 3" /></Ico>
const Fold = ({ open }: { open: boolean }) => <Ico size={14}><path d={open ? 'M6 15l6-6 6 6' : 'M6 9l6 6 6-6'} /></Ico>

/** "Added 2 Oct · from iPhone" */
const addedLine = (d: MyDevice) => `Added ${dayMonth(d.added)}${d.from ? ` · from ${d.from}` : ''}`

export function MyDeviceRows({ list, extra }: { list: MyDevice[]; extra?: (d: MyDevice) => React.ReactNode }) {
  return <>{list.map((d) => (
    <div key={d.id} className="home-row seal-dev">
      <span className="row-ico"><DeviceIcon kind={d.kind} size={22} /></span>
      <span className="t"><b>{d.name}{d.here && <> </>}{d.here && <span className="seal-here">This device</span>}</b><small>{extra ? extra(d) : addedLine(d)}</small></span>
    </div>
  ))}</>
}

/** Boards messages/encryption-devices (+ -dark, -group, -email): who can read this conversation.
 *  My devices in full; each other person one row with a count (never which devices). */
export function EncryptionScreen({ conv }: { conv: Conv }) {
  useVersion()
  const [open, setOpen] = useState(true)
  if (!isSealed(conv)) { replaceHash('#/dm/' + encConv(conv)); return null }
  const mail = isMail(conv)
  const others = peers(conv).filter((p) => p !== me()).sort((a, b) => displayName(a).localeCompare(displayName(b)))
  const group = others.length > 1
  const mine = myDevices()
  const counts = others.map((p) => theirDevices(p)?.count ?? 0)
  const total = mine.length + counts.reduce((a, b) => a + b, 0)
  return (
    <div className="screen seal-screen">
      <div className="topbar"><button className="icon-btn" aria-label="Back to the conversation" onClick={() => up('dm/' + encConv(conv))}>‹</button><h1>Encryption</h1></div>
      <div className="scroll pad">
        <div className="seal-hero">
          <span className="seal-shield"><SealIcon size={28} /></span>
          <h2>Only these devices can read this conversation</h2>
          {group && <span className="seal-sum">{others.length + 1} people · {devicesWord(total)}</span>}
        </div>
        {group ? <>
          <div className="place-section"><h3>You</h3></div>
          <div className="home-card">
            <button className="home-row seal-me" aria-expanded={open} onClick={() => setOpen(!open)}>
              <Avatar pubkey={me()} size={32} />
              <span className="t"><b>{realName(me()) || 'You'} <span className="seal-you">(you)</span></b></span>
              <span className="seal-count">{devicesWord(mine.length)}</span><Fold open={open} />
            </button>
            {open && <div className="seal-sub"><MyDeviceRows list={mine} /></div>}
          </div>
        </> : <>
          <div className="seal-pp"><Avatar pubkey={me()} size={28} /><span>{realName(me()) || 'You'} <span className="seal-you">(you)</span></span></div>
          <div className="home-card"><MyDeviceRows list={mine} /></div>
        </>}
        <div className="place-section"><h3>Others</h3></div>
        <div className="home-card">
          {mail
            ? <div className="home-row seal-person"><span className="mail-avatar" style={{ width: 32, height: 32 }} aria-hidden="true"><MailIcon /></span>
              <span className="t"><b>{convName(conv) !== mailAddress(conv) ? convName(conv) : mailAddress(conv)} · by e-mail</b><small>not on this belfry</small></span></div>
            : others.map((p) => {
              const t = theirDevices(p); const c = recentChange(t)
              return (
                <div key={p} className="home-row seal-person">
                  <Avatar pubkey={p} size={32} />
                  <span className="t"><b>{displayName(p)}</b>{c && <small>{c.what === 'added' ? 'Added' : 'Removed'} a device {whenWord(c.at)}</small>}</span>
                  {t && <span className="seal-count">{devicesWord(t.count)}</span>}
                </div>
              )
            })}
        </div>
        {!mail && <p className="seal-only">You see only how many devices others have, not which.</p>}
        <div className="seal-facts">
          <p><EyeOff /><span>The server and the belfry’s admins can’t read it.</span></p>
          <p><KeyIco /><span>{group ? 'When a device is removed or someone leaves, the lock changes: they can’t read anything new.' : 'When a device is removed, the lock changes: that device can’t read anything new.'}</span></p>
          {mail && <p><span className="seal-mail-ico"><MailIcon /></span><span>E-mails are locked as soon as they arrive. They pass through the belfry’s mail server, and the other side’s mail provider keeps its own copy.</span></p>}
        </div>
      </div>
    </div>
  )
}

/** Board messages/encryption-new-device: on a new device, at the top of Messages (and in place of
 *  a locked conversation's earlier messages). Goes when unlocked. */
export function LockedBanner() {
  return (
    <div className="seal-banner" role="status">
      <SealIcon size={20} />
      <span className="t"><b>Messages are locked on this device</b>Open Belfry on another device of yours to unlock them.<br />
        <button className="seal-how" onClick={() => go('dm/unlock')}>Show how</button></span>
    </div>
  )
}

/** Board messages/encryption-locked-thread: in a conversation, in place of its sealed messages. */
export function LockedBlock({ since }: { since: number }) {
  return (
    <div className="seal-banner" role="status">
      <SealIcon size={20} />
      <span className="t"><b>Locked on this device</b>Messages from {dayMonth(since)} on can be read once another device of yours allows this one.<br />
        <button className="seal-how" onClick={() => go('dm/unlock')}>Show how</button></span>
    </div>
  )
}

export const LockedHereText = () => <span className="seal-locked-t"><SealIcon />Locked on this device</span>

/** "Show how" (a pushed page): the three steps. */
export function ShowHowScreen() {
  useVersion()
  return (
    <div className="screen seal-screen">
      <div className="topbar"><button className="icon-btn" aria-label="Back to Messages" onClick={() => up('dm')}>‹</button><h1>Unlock with another device</h1></div>
      <div className="scroll pad">
        <ol className="seal-steps">
          <li>Open Belfry on a phone, tablet or computer where you’re already signed in.</li>
          <li>It asks whether to give this device access: tap Allow.</li>
          <li>Your conversations open here.</li>
        </ol>
        <p className="seal-only">No other device? The people you write to can share a conversation’s history with this device.</p>
        {!anyLockedHere() && <p className="seal-done">Your conversations are unlocked on this device.</p>}
      </div>
    </div>
  )
}

/** Two buttons that both lock while either works (Allow / Not me, Share history / Not now). */
function usePair() {
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const run = (which: string, f: () => Promise<unknown>) => async () => {
    if (busy) return
    setBusy(which); setError('')
    try { await f() } catch (e) { setError(e instanceof Error ? e.message : String(e)) } finally { setBusy('') }
  }
  return { busy, error, run }
}
const Spin = () => <span className="spinner" aria-hidden="true" />

/** Board messages/encryption-allow: on my other devices, a card over whatever page is open. The
 *  first device to answer wins (the others drop it); ignored, it comes back next time. */
export function AllowPrompt() {
  useVersion()
  const a = asking()
  const ref = useRef<HTMLDialogElement>(null)
  const { busy, error, run } = usePair()
  useEffect(() => { const d = ref.current; if (d && !d.open) d.showModal() }, [a?.id])
  if (!a) return null
  // canvas v90: a 21st device: said in the card, with Devices / OK; the new device stays locked
  if (error === FULL) return (
    <dialog ref={ref} className="seal-alert" role="alertdialog" aria-labelledby="seal-allow-t" onCancel={(e) => e.preventDefault()}>
      <span className="seal-dvbig"><DeviceIcon kind={a.kind} size={30} /></span>
      <h2 id="seal-allow-t">{FULL}</h2>
      <button className="btn" onClick={() => { dismissAsking(); go('me/devices') }}>Devices</button>
      <button className="btn ghost" onClick={dismissAsking}>OK</button>
    </dialog>
  )
  return (
    <dialog ref={ref} className="seal-alert" role="alertdialog" aria-labelledby="seal-allow-t" onCancel={(e) => e.preventDefault()}>
      <span className="seal-dvbig"><DeviceIcon kind={a.kind} size={30} /></span>
      <h2 id="seal-allow-t">Give your new device “{a.name}” access to your conversations?</h2>
      <p>It will read all your messages, including earlier ones.</p>
      <span className="seal-meta">Signed in as {realName(me()) || 'you'} · {ago(a.at)}</span>
      {error && <div className="error">{error}</div>}
      <button className="btn" disabled={!!busy} aria-busy={busy === 'allow' || undefined} onClick={run('allow', allowAsking)}>{busy === 'allow' ? <><Spin /> Allowing…</> : 'Allow'}</button>
      <button className="btn seal-red" disabled={!!busy} aria-busy={busy === 'no' || undefined} onClick={run('no', refuseAsking)}>{busy === 'no' ? <><Spin /> Removing…</> : 'Not me'}</button>
      <span className="seal-note">Not me removes that device and signs it out.</span>
      {error && <button className="link seal-close" onClick={dismissAsking}>Close</button>}
    </dialog>
  )
}
const ago = (at: number) => { const m = Math.round((Date.now() - at) / 6e4); return m < 1 ? 'just now' : m < 60 ? `${m} min ago` : m < 1440 ? `${Math.round(m / 60)} h ago` : dayMonth(at) }

/** Board messages/encryption-link-qr: inside Link your phone, once the new device is in. */
export function LinkedByScan({ name, onDone }: { name: string; onDone: () => void }) {
  return (
    <div className="seal-linked">
      <span className="seal-ok"><Ico size={32}><path d="M5 12.5l4.5 4.5L19 7.5" /></Ico></span>
      <h2>{name} is linked</h2>
      <p>It can read your conversations, including earlier ones.</p>
      <p className="g">Scanning your code was the confirmation: nothing more to allow.</p>
      <button className="btn" onClick={onDone}>Done</button>
    </div>
  )
}

/** Board messages/encryption-share-history: from the amber line. Any member may share, by this tap. */
export function ShareHistorySheet({ conv, l, onClose }: { conv: Conv; l: DeviceLine; onClose: () => void }) {
  const { busy, error, run } = usePair()
  const first = displayName(l.who).split(' ')[0]
  const group = peers(conv).filter((p) => p !== me()).length > 1
  const kind = l.kind ?? 'device' // outside the demo: "A new device" (canvas v88)
  const t = new Date(l.at)
  return (
    <Sheet title={`Share ${group ? 'this group’s' : 'this conversation’s'} history with ${first}’s new device?`} onClose={() => { if (!busy) onClose() }}>
      <div className="seal-share">
        <p>{first} started over on a new device and can’t read your earlier messages. Only do this if you’re sure it’s {first}: ask in person or by phone if you’re not.</p>
        <p className="seal-meta"><DeviceIcon kind={l.kind} /> A new {kind} · signed in {whenWord(l.at)} {t.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}</p>
        {error && <div className="error">{error}</div>}
        <button className="btn" disabled={!!busy} aria-busy={busy === 'share' || undefined} onClick={run('share', async () => { await shareHistory(conv, l.id); onClose() })}>{busy === 'share' ? <><Spin /> Sharing…</> : 'Share history'}</button>
        <button className="btn ghost" disabled={!!busy} onClick={onClose}>Not now</button>
      </div>
    </Sheet>
  )
}

/** Me → Devices (board messages/encryption-remove-device): my devices; Remove asks first, then
 *  the row shows progress and no other row can be removed. This device: Sign out instead. */
export function DevicesScreen() {
  useVersion()
  useEffect(() => { if (sealedOn()) void mySessions(0).then(() => touch()) }, [])
  const [asked, setAsked] = useState<MyDevice | null>(null)
  const [removing, setRemoving] = useState('')
  const [error, setError] = useState('')
  const remove = async (d: MyDevice) => {
    setAsked(null); setRemoving(d.id); setError('')
    try { await removeDevice(d.id) } catch (e) { setError(e instanceof Error ? e.message : String(e)) } finally { setRemoving('') }
  }
  return (
    <div className="screen seal-screen">
      <div className="topbar"><button className="icon-btn" aria-label="Back to Me" onClick={() => up('me')}>‹</button><h1>Devices</h1></div>
      <div className="scroll pad">
        <div className="place-section"><h3>Signed in as {realName(me()) || 'you'}</h3></div>
        <div className="home-card">
          {myDevices().map((d) => (
            <div key={d.id} className="home-row seal-dev">
              <span className="row-ico"><DeviceIcon kind={d.kind} size={22} /></span>
              <span className="t"><b>{d.name}{d.here && <> <span className="seal-here">This device</span></>}</b>
                <small>{[addedLine(d), activeWord(d.active)].filter(Boolean).join(' · ')}</small></span>
              {!d.here && canRemoveDevices() && (removing === d.id
                ? <span className="seal-removing"><Spin /> Removing…</span>
                : <button className="btn small ghost seal-remove" disabled={!!removing} onClick={() => setAsked(d)}>Remove</button>)}
            </div>
          ))}
        </div>
        {error && <div className="error" style={{ margin: '8px 16px' }}>{error}</div>}
        <p className="seal-only">This device can’t be removed here: use Sign out.</p>
      </div>
      {asked && (
        <Sheet title={`Remove ${asked.name}?`} onClose={() => setAsked(null)}>
          <div className="seal-share">
            <p>It will be signed out and stop reading new messages, in all your conversations. What it already shows stays on it.</p>
            <button className="btn seal-red" onClick={() => void remove(asked)}>Remove</button>
            <button className="btn ghost" onClick={() => setAsked(null)}>Cancel</button>
          </div>
        </Sheet>
      )}
    </div>
  )
}

const ShareIco = () => <Ico><path d="M12 15V3M7 8l5-5 5 5" /><path d="M5 12v7a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-7" /></Ico>

/** Board messages/encryption-shared-history (+ -why): on the device that received a shared
 *  history, one grey line above each sharer's block; a tap explains (a popover on a laptop). */
export function SharedByLine({ who }: { who: string }) {
  const [anchor, setAnchor] = useState<Anchor | null | undefined>(undefined)
  const first = displayName(who).split(' ')[0]
  return (
    <>
      <button className="seal-line seal-shby" aria-haspopup="dialog" onClick={(e) => { const el = e.currentTarget; setAnchor({ rect: () => el.getBoundingClientRect(), el }) }}>
        <ShareIco /><span>Shared by {first}</span><Chev />
      </button>
      {anchor !== undefined && (
        <SheetOrPopover title={`Shared by ${first}`} anchor={anchor} onClose={() => setAnchor(undefined)}>
          <div className="seal-why">
            <p>{first} sent you these earlier messages. They come from {first}’s device, so the app can’t check them the way it checks new ones.</p>
            <button className="btn" onClick={() => setAnchor(undefined)}>OK</button>
          </div>
        </SheetOrPopover>
      )}
    </>
  )
}

const OpenLock = () => <Ico size={12}><rect x="5" y="10.5" width="14" height="10" rx="2" /><path d="M8 10.5V8a4 4 0 0 1 7.5-2" /></Ico>

/** Board messages/encryption-thread (v89): under a message that came the old way after the
 *  conversation was sealed; a tap explains (a popover on a laptop, a sheet on a phone). */
export function NotSealedMark() {
  const [anchor, setAnchor] = useState<Anchor | null | undefined>(undefined)
  return (
    <>
      <button className="seal-notsealed" aria-haspopup="dialog" onClick={(e) => { const el = e.currentTarget; setAnchor({ rect: () => el.getBoundingClientRect(), el }) }}>
        <OpenLock />Not sealed
      </button>
      {anchor !== undefined && (
        <SheetOrPopover title="Not sealed" anchor={anchor} onClose={() => setAnchor(undefined)}>
          <div className="seal-why">
            <p>This message was not sent with the lock. It may come from an old app version.</p>
            <button className="btn" onClick={() => setAnchor(undefined)}>OK</button>
          </div>
        </SheetOrPopover>
      )}
    </>
  )
}
