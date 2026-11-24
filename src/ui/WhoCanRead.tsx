// Who can read this (Encrypted canvas: encrypted/who-can-read, -group-who, -email-who, -who-agent;
// v19: a trash icon on each of my own devices and my agent). Format 3 (docs/encrypted.md): my own
// devices by name (from my /api/me/sessions), this device marked; everyone else only as a person
// with a count of devices, never which (Xavier, 23 Nov). The header lock opens it.
import { useEffect, useState } from 'react'
import { BELFRY } from '../config'
import { answerFor, devicesOf, encDevice, groupCantCheck } from '../core/enc/runtime'
import { displayName, group, realName } from '../core/model'
import { mySessions, type SessionRow } from '../core/sealedrt'
import { useVersion } from '../core/store'
import { me, signedFetch } from '../core/sync'
import { isMail, mailAddress, convName, peers, type Conv } from '../core/dm'
import { Avatar, Btn, fmtDay, Sheet, up } from './common'
import { SealIcon } from './Sealed'
import { MailIcon, TrashIcon } from './icons'

const encConv = (c: Conv) => encodeURIComponent(c)
const devicesWord = (n: number) => (n === 1 ? '1 device' : `${n} devices`)
const nameOf = (r?: SessionRow) => (r?.device || r?.application_name || '').trim() || 'A device'

type Removing = { kind: 'device'; key: string; name: string; bunker?: string } | { kind: 'agent'; key: string; name: string }

export function WhoCanRead({ conv }: { conv: Conv }) {
  useVersion()
  const [rows, setRows] = useState<SessionRow[] | null>(null)
  const [removing, setRemoving] = useState<Removing | null>(null)
  const [done, setDone] = useState('')
  useEffect(() => { void mySessions().then(setRows) }, [])
  const mail = isMail(conv)
  const others = peers(conv).filter((p) => p !== me()).sort((a, b) => displayName(a).localeCompare(displayName(b)))
  const mine = devicesOf(me())
  const here = encDevice()
  const byKey = new Map((rows ?? []).filter((r) => r.client_pubkey).map((r) => [r.client_pubkey!, r]))
  const myRows = (mine?.devices ?? []).slice().sort((a, b) => (a === here ? -1 : b === here ? 1 : (mine?.since?.get(a) ?? 0) - (mine?.since?.get(b) ?? 0)))
  const firstNames = others.map((p) => (realName(p) || displayName(p)).split(' ')[0])
  const lead = mail ? 'On the belfry, only your devices can read this conversation.'
    : others.length === 0 ? 'Only your devices can read your notes.' // (Notes: encrypted/notes-self-who)
    : others.length === 1 ? `Only ${firstNames[0]}’s devices and yours can read this conversation.`
    : 'Only the devices of the people here can read this conversation.'
  return (
    <div className="screen seal-screen who-can-read">
      <div className="topbar"><button className="icon-btn" aria-label="Back to the conversation" onClick={() => up('dm/' + encConv(conv))}>‹</button><h1>Who can read this</h1></div>
      <div className="scroll pad">
        <div className="seal-hero">
          <span className="seal-shield"><SealIcon size={28} /></span>
          <p className="who-lead">{lead}</p>
        </div>
        {(others.length > 0 || mail) && <>
          <div className="place-section"><h3>Others</h3></div>
          <div className="home-card">
            {mail
              ? <div className="home-row seal-person"><span className="mail-avatar" style={{ width: 32, height: 32 }} aria-hidden="true"><MailIcon /></span><span className="t"><b>{convName(conv) || mailAddress(conv)}</b><small>By e-mail · not on this belfry</small></span></div>
              : others.map((p) => {
                const d = devicesOf(p)
                return (
                  <div key={p} className="home-row seal-person">
                    <Avatar pubkey={p} size={32} />
                    <span className="t"><b>{displayName(p)}</b></span>
                    <span className="seal-count">{d ? devicesWord(d.devices.length + d.readers.length) : '…'}</span>
                  </div>
                )
              })}
          </div>
          {!mail && <p className="seal-only">You see how many devices others have, never which.</p>}
        </>}
        <div className="place-section"><h3>{realName(me()) || 'You'} <span className="seal-you">(you)</span> <span className="seal-count">{mine ? devicesWord(mine.devices.length + mine.readers.length) : ''}</span></h3></div>
        <div className="home-card">
          {myRows.map((k) => {
            const r = byKey.get(k), name = nameOf(r), at = mine?.since?.get(k)
            return (
              <div key={k} className="home-row seal-dev">
                <span className="t"><b>{name}{k === here && <> <span className="seal-here">This device</span></>}</b>{at ? <small>Added {fmtDay(new Date(at * 1000))}</small> : null}</span>
                {k !== here && <button type="button" className="icon-btn danger who-trash" aria-label={`Remove ${name}`} onClick={() => setRemoving({ kind: 'device', key: k, name, bunker: (r as { bunker_pubkey?: string } | undefined)?.bunker_pubkey })}><TrashIcon /></button>}
              </div>
            )
          })}
          {(mine?.readers ?? []).map((k) => (
            <div key={k} className="home-row seal-dev">
              <span className="t"><b>{displayName(k)}</b><small>Your agent{mine?.since?.get(k) ? ` · access given ${fmtDay(new Date(mine!.since!.get(k)! * 1000))}` : ''}</small></span>
              <button type="button" className="icon-btn danger who-trash" aria-label={`Remove ${displayName(k)}’s access`} onClick={() => setRemoving({ kind: 'agent', key: k, name: displayName(k) })}><TrashIcon /></button>
            </div>
          ))}
          {!mine && <div className="home-row"><span className="t"><small>This device isn’t set up for encrypted messages yet.</small></span></div>}
        </div>
        {done && <p className="hint" role="status">{done}</p>}
        <div className="seal-facts">
          {mail ? <>
            <h3>E-mail</h3>
            <p>E-mails travel the usual way: they reach the belfry’s mail server unencrypted and are encrypted as soon as they arrive. Your replies leave the same way. Once here, the belfry can’t read them. {convName(conv) || mailAddress(conv)}’s mail provider keeps its own copy.</p>
          </> : <>
            <h3>How it works</h3>
            <p>Each message is encrypted on the device that sends it. Only the devices above can read it. The belfry stores and delivers your messages but can’t read them, and neither can its admins. It does see who writes to whom, and when.</p>
            <p>Each device has its own key, made when it signs in, that never leaves it. When a device is added or removed, the next message uses a new lock, so a removed device can’t read anything new.</p>
          </>}
        </div>
      </div>
      {removing && <RemoveSheet r={removing} onClose={() => setRemoving(null)} onDone={(t) => { setRemoving(null); setDone(t); void mySessions(0).then(setRows) }} />}
    </div>
  )
}

/** Remove one of my devices or my agent's access: asks first (encrypted/card-remove's words). */
export function RemoveSheet({ r, onClose, onDone }: { r: Removing; onClose: () => void; onDone: (said: string) => void }) {
  const [error, setError] = useState('')
  return (
    <Sheet title={r.kind === 'agent' ? `Remove ${r.name}’s access?` : `Remove ${r.name}?`} onClose={onClose}>
      <p>{r.kind === 'agent'
        ? `${r.name} won’t read your private messages and private groups any more. What it read before stays with it.`
        : 'It will be signed out at once and can’t read new messages. If you didn’t add it, someone used a code sent to your e-mail: change your e-mail password.'}</p>
      {error && <p className="error" role="alert">{error}</p>}
      <div className="row-btns two">
        <button type="button" className="btn ghost" onClick={onClose}>Cancel</button>
        <Btn className="btn danger" onClick={async () => {
          setError('')
          try {
            const res = r.kind === 'agent'
              ? await signedFetch(`${BELFRY.api}/agents`, 'POST', { pubkey: r.key, reads: false })
              : await signedFetch(`${BELFRY.api}/me/sessions/revoke`, 'POST', { ...(r.bunker ? { bunker_pubkey: r.bunker } : { client_pubkey: r.key }), by: encDevice() })
            if (!res.ok) throw new Error(res.status === 404 ? 'not yet' : 'failed')
            onDone(r.kind === 'agent' ? `${r.name} can’t read your private messages any more.` : `${r.name} was removed.`)
          } catch (e) {
            setError(e instanceof Error && e.message === 'not yet' ? 'This belfry can’t do that yet.' : 'That did not work. Try again.')
          }
        }}>Remove</Btn>
      </div>
    </Sheet>
  )
}

/** Me › Devices in format 3 (encrypted/devices, device, devices-agent): every device on my list,
 *  this one marked, when it was added and last active, New for one added in the last day; my agent
 *  when it may read. A tap opens one device: Added, Last active, Earlier messages, Remove. */
export function EncDevicesScreen() {
  useVersion()
  const [rows, setRows] = useState<SessionRow[] | null>(null)
  const [open, setOpen] = useState<string | null>(null)
  const [removing, setRemoving] = useState<Removing | null>(null)
  const [done, setDone] = useState('')
  useEffect(() => { void mySessions(0).then(setRows) }, [])
  const mine = devicesOf(me())
  const here = encDevice()
  const byKey = new Map((rows ?? []).filter((r) => r.client_pubkey).map((r) => [r.client_pubkey!, r]))
  const day = (v?: string | number | null) => { const t = typeof v === 'number' ? (v > 1e12 ? v : v * 1000) : v ? Date.parse(v) : NaN; return Number.isFinite(t) ? fmtDay(new Date(t)) : '' }
  const list = (mine?.devices ?? []).slice().sort((a, b) => (a === here ? -1 : b === here ? 1 : (mine?.since?.get(b) ?? 0) - (mine?.since?.get(a) ?? 0)))
  const now = Date.now() / 1000
  if (open) {
    const r = byKey.get(open), name = nameOf(r), at = mine?.since?.get(open), a = answerFor(open)
    return (
      <div className="screen seal-screen enc-devices">
        <div className="topbar"><button className="icon-btn" aria-label="Back to my devices" onClick={() => setOpen(null)}>‹</button><h1>{name}</h1></div>
        <div className="scroll pad">
          <div className="home-card">
            <div className="home-row"><span className="t"><b>Added</b></span><span className="seal-count">{at ? fmtDay(new Date(at * 1000)) : '—'}</span></div>
            <div className="home-row"><span className="t"><b>Last active</b></span><span className="seal-count">{day(r?.last_activity) || '—'}</span></div>
            {open !== here && <div className="home-row"><span className="t"><b>Earlier messages</b></span><span className="seal-count">{a?.answer === 'shared' ? 'Shared' : 'Not shared'}</span></div>}
          </div>
          {open !== here
            ? <button className="link danger enc-remove" onClick={() => setRemoving({ kind: 'device', key: open, name, bunker: (r as { bunker_pubkey?: string } | undefined)?.bunker_pubkey })}>Remove this device</button>
            : <p className="hint">This device. Signing out is in Settings.</p>}
        </div>
        {removing && <RemoveSheet r={removing} onClose={() => setRemoving(null)} onDone={(t) => { setRemoving(null); setOpen(null); setDone(t); void mySessions(0).then(setRows) }} />}
      </div>
    )
  }
  return (
    <div className="screen seal-screen enc-devices">
      <div className="topbar"><button className="icon-btn" aria-label="Back" onClick={() => up('me')}>‹</button><h1>Devices</h1></div>
      <div className="scroll pad">
        <p className="hint">These devices can read your encrypted messages.</p>
        <div className="home-card">
          {list.map((k) => {
            const r = byKey.get(k), name = nameOf(r), at = mine?.since?.get(k)
            return (
              <div key={k} className="home-row seal-dev">
                <button type="button" className="t row-btn" onClick={() => setOpen(k)}>
                  <b>{name}{k === here && <> <span className="seal-here">This device</span></>}{k !== here && at && now - at < 86400 && <> <span className="seal-new">New</span></>}</b>
                  <small>{[at ? `Added ${fmtDay(new Date(at * 1000))}` : '', day(r?.last_activity) ? `last active ${day(r?.last_activity)}` : ''].filter(Boolean).join(' · ')}</small>
                </button>
                {k !== here && <button type="button" className="icon-btn danger who-trash" aria-label={`Remove ${name}`} onClick={() => setRemoving({ kind: 'device', key: k, name, bunker: (r as { bunker_pubkey?: string } | undefined)?.bunker_pubkey })}><TrashIcon /></button>}
              </div>
            )
          })}
          {(mine?.readers ?? []).map((k) => (
            <div key={k} className="home-row seal-dev">
              <span className="t"><b>{displayName(k)}</b><small>Your agent{mine?.since?.get(k) ? ` · access given ${fmtDay(new Date(mine!.since!.get(k)! * 1000))}` : ''}</small></span>
              <button type="button" className="icon-btn danger who-trash" aria-label={`Remove ${displayName(k)}’s access`} onClick={() => setRemoving({ kind: 'agent', key: k, name: displayName(k) })}><TrashIcon /></button>
            </div>
          ))}
          {!mine && <div className="home-row"><span className="t"><small>This device isn’t set up for encrypted messages yet.</small></span></div>}
        </div>
        {done && <p className="hint" role="status">{done}</p>}
      </div>
      {removing && <RemoveSheet r={removing} onClose={() => setRemoving(null)} onDone={(t) => { setRemoving(null); setDone(t); void mySessions(0).then(setRows) }} />}
    </div>
  )
}

/** On my agent's profile (encrypted/agent-access, -confirm, -on): "Give access to my private messages
 *  and private groups", off by default; turning it on asks first; off = the same as its trash icon. */
export function AgentAccess({ agent }: { agent: string }) {
  useVersion()
  const on = !!devicesOf(me())?.readers.includes(agent)
  const [asking, setAsking] = useState(false)
  const [removing, setRemoving] = useState(false)
  const [error, setError] = useState('')
  const [pending, setPending] = useState(false)
  const name = displayName(agent)
  return (
    <div className="agent-access">
      <label className="switch-row">
        <span><b>Give access to my private messages and private groups</b><small>{on ? `${name} is now one of your devices: it reads them like one.` : pending ? 'Waiting for the belfry…' : 'Off: it doesn’t read them.'}</small></span>
        <input type="checkbox" role="switch" checked={on || pending} onChange={(e) => { if (e.target.checked) setAsking(true); else setRemoving(true) }} />
      </label>
      {error && <p className="error" role="alert">{error}</p>}
      {asking && (
        <Sheet title={`Give ${name} access?`} onClose={() => setAsking(false)}>
          <p>It will read your private messages and private groups from now on, like one of your devices. You can turn this off at any time.</p>
          <div className="row-btns two">
            <button type="button" className="btn ghost" onClick={() => setAsking(false)}>Cancel</button>
            <Btn className="btn" onClick={async () => {
              setError('')
              try {
                const r = await signedFetch(`${BELFRY.api}/agents`, 'POST', { pubkey: agent, reads: true })
                if (!r.ok) throw new Error(r.status === 404 ? 'This belfry can’t do that yet.' : 'That did not work. Try again.')
                setPending(true); setAsking(false)
              } catch (e) { setError(e instanceof Error ? e.message : 'That did not work. Try again.'); setAsking(false) }
            }}>Give access</Btn>
          </div>
        </Sheet>
      )}
      {removing && <RemoveSheet r={{ kind: 'agent', key: agent, name }} onClose={() => setRemoving(false)} onDone={() => { setRemoving(false); setPending(false) }} />}
    </div>
  )
}

/** Who can read this, in a private group (encrypted/group-who, group-who-cant-check): my devices,
 *  then each other member with a count only; when the entries seen don't add up, "Can't check". */
export function GroupWho({ id }: { id: string }) {
  useVersion()
  const [rows, setRows] = useState<SessionRow[] | null>(null)
  const [removing, setRemoving] = useState<Removing | null>(null)
  const [done, setDone] = useState('')
  useEffect(() => { void mySessions().then(setRows) }, [])
  const g = group(id)
  const people = [...(g?.members ?? [])]
  const others = people.filter((p) => p !== me()).sort((a, b) => displayName(a).localeCompare(displayName(b)))
  const mine = devicesOf(me())
  const here = encDevice()
  const byKey = new Map((rows ?? []).filter((r) => r.client_pubkey).map((r) => [r.client_pubkey!, r]))
  const cant = groupCantCheck(id)
  const name = g?.name ?? ''
  return (
    <div className="screen seal-screen who-can-read">
      <div className="topbar"><button className="icon-btn" aria-label="Back to the group" onClick={() => up('c/' + encodeURIComponent(id))}>‹</button><h1>Who can read this</h1></div>
      <div className="scroll pad">
        <div className="seal-hero">
          <span className="seal-shield"><SealIcon size={28} /></span>
          {cant
            ? <><b>Can’t check who can read this</b><p className="who-lead">This device couldn’t confirm which devices can read {name}. It keeps trying.</p></>
            : <p className="who-lead">Only the devices of these {people.length} people can read {name}. Someone who joins later can’t read what was said before.</p>}
        </div>
        <div className="place-section"><h3>{realName(me()) || 'You'} <span className="seal-you">(you)</span> <span className="seal-count">{mine ? devicesWord(mine.devices.length + mine.readers.length) : ''}</span></h3></div>
        <div className="home-card">
          {(mine?.devices ?? []).map((k) => {
            const r = byKey.get(k), nm = nameOf(r), at = mine?.since?.get(k)
            return (
              <div key={k} className="home-row seal-dev">
                <span className="t"><b>{nm}{k === here && <> <span className="seal-here">This device</span></>}</b>{at ? <small>Added {fmtDay(new Date(at * 1000))}</small> : null}</span>
                {k !== here && <button type="button" className="icon-btn danger who-trash" aria-label={`Remove ${nm}`} onClick={() => setRemoving({ kind: 'device', key: k, name: nm, bunker: (r as { bunker_pubkey?: string } | undefined)?.bunker_pubkey })}><TrashIcon /></button>}
              </div>
            )
          })}
          {(mine?.readers ?? []).map((k) => (
            <div key={k} className="home-row seal-dev">
              <span className="t"><b>{displayName(k)}</b><small>Your agent</small></span>
              <button type="button" className="icon-btn danger who-trash" aria-label={`Remove ${displayName(k)}’s access`} onClick={() => setRemoving({ kind: 'agent', key: k, name: displayName(k) })}><TrashIcon /></button>
            </div>
          ))}
        </div>
        {!cant && others.length > 0 && <>
          <div className="place-section"><h3>Others</h3></div>
          <div className="home-card">
            {others.filter((p) => (devicesOf(p)?.devices.length ?? 0) + (devicesOf(p)?.readers.length ?? 0) > 0).map((p) => { const d = devicesOf(p); return (
              <div key={p} className="home-row seal-person"><Avatar pubkey={p} size={32} /><span className="t"><b>{displayName(p)}</b></span><span className="seal-count">{d ? devicesWord(d.devices.length + d.readers.length) : '…'}</span></div>
            ) })}
          </div>
          <p className="seal-only">You see how many devices others have, never which.</p>
          {/* §23: a member signed in nowhere gets nothing until they are */}
          {others.some((p) => !((devicesOf(p)?.devices.length ?? 0) + (devicesOf(p)?.readers.length ?? 0))) && <>
            <div className="place-section"><h3>Not signed in on any device</h3></div>
            <div className="home-card">{others.filter((p) => !((devicesOf(p)?.devices.length ?? 0) + (devicesOf(p)?.readers.length ?? 0))).map((p) => <div key={p} className="home-row seal-person"><Avatar pubkey={p} size={32} /><span className="t"><b>{displayName(p)}</b><small>Gets new messages once signed in</small></span></div>)}</div>
          </>}
        </>}
        {done && <p className="hint" role="status">{done}</p>}
        <div className="seal-facts">
          <h3>How it works</h3>
          <p>Each message is encrypted on the device that sends it. Only the devices above can read it. The belfry stores and delivers your messages but can’t read them, and neither can its admins. It does see who writes to whom, and when.</p>
          <p>Each device has its own key, made when it signs in, that never leaves it. When a device is added or removed, the next message uses a new lock, so a removed device can’t read anything new.</p>
        </div>
      </div>
      {removing && <RemoveSheet r={removing} onClose={() => setRemoving(null)} onDone={(t) => { setRemoving(null); setDone(t); void mySessions(0).then(setRows) }} />}
    </div>
  )
}
