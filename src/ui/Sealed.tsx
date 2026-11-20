// Sealed conversations, the screens (canvas "Messages · encryption", approved 20 Nov). Data:
// core/sealed.ts (demo only until the lead's docs/sealed.md). Never a key, npub or id as text;
// only my own devices are named.
import { useState } from 'react'
import { convName, isMail, mailAddress, peers, type Conv } from '../core/dm'
import { displayName, realName } from '../core/model'
import { useVersion } from '../core/store'
import { me } from '../core/sync'
import { replaceHash } from '../core/nav'
import { dayMonth, devicesWord, isSealed, myDevices, recentChange, theirDevices, whenWord, type DeviceKind, type DeviceLine, type MyDevice } from '../core/sealed'
import { MailIcon } from './icons'
import { Avatar, go, up } from './common'

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
