// A new device and earlier messages (Encrypted canvas: encrypted/new-list, new-thread, new-asked,
// new-arrived, transition, lost-all): one line at the bottom of Messages and above the history of
// an encrypted conversation. It can't know which conversation a message it can't open belongs to
// (the alias hides it: the lead's ruling), so it speaks generally.
import { useEffect, useState } from 'react'
import { addedDevices, answerDevice, firstScreenDevices, firstScreenOk, goneOk, listWarnings, askForEarlier, askedNow, olderDevices, sharedByNow, shareWith, unresolvedCount, type AddedDevice } from '../core/enc/runtime'
import { displayName } from '../core/model'
import { RemoveSheet } from './WhoCanRead'
import { missingLetterboxes, mySessions, type SessionRow } from '../core/sealedrt'
import { useVersion } from '../core/store'
import { Btn, fmtDay, fmtTime, go } from './common'

const LOST_OK = 'belfry-enc-lost-ok'
const listOf = (names: string[], word: 'and' | 'or') => names.length <= 1 ? names.join('') : `${names.slice(0, -1).join(', ')} ${word} ${names.at(-1)}`

export function EarlierLine({ where }: { where: 'list' | 'thread' }) {
  useVersion()
  const [rows, setRows] = useState<SessionRow[] | null>(null)
  const [lostOk, setLostOk] = useState(() => { try { return localStorage.getItem(LOST_OK) === '1' } catch { return false } })
  useEffect(() => { void mySessions().then(setRows) }, [])
  const missing = unresolvedCount() + missingLetterboxes()
  const shared = sharedByNow()
  const name = (k: string) => (rows ?? []).find((r) => r.client_pubkey === k)?.device || (rows ?? []).find((r) => r.client_pubkey === k)?.application_name || ''
  if (!missing) return shared && where === 'thread' ? <div className="enc-line quiet" role="note">Earlier messages shared by {name(shared.device) || 'another of your devices'}</div> : null
  const older = olderDevices()
  if (!older.length) { // §8 lost-all: none of the devices that could read them is signed in any more
    if (lostOk) return null
    return (
      <div className="enc-line" role="note">
        <b>Earlier messages can’t be recovered</b>
        <span>None of the devices that could read them is signed in any more. New messages work as usual.</span>
        {where === 'list' && <button className="link" onClick={() => { setLostOk(true); try { localStorage.setItem(LOST_OK, '1') } catch { /* private mode */ } }}>OK</button>}
      </div>
    )
  }
  // names from my sessions, once each; none known: "your other devices"
  const names = [...new Set(older.map(name).filter(Boolean))]
  const unnamed = names.length < older.length
  const asked = askedNow()
  if (asked) return (
    <div className="enc-line" role="status">
      <b>Asked your other devices</b>
      <span>Earlier messages arrive here when {names.length && !unnamed ? listOf(names, 'or') : 'one of your other devices'} is next open.</span>
      <button className="link" onClick={() => go('me/devices')}>Your devices ›</button>
    </div>
  )
  return (
    <div className="enc-line" role="note">
      <b>Earlier messages are on your other devices</b>
      {where === 'list' && <span>{names.length && !unnamed ? `${listOf(names, 'and')} can` : 'Your other devices can'} share them with this one.</span>}
      <Btn className="link" onClick={async () => { await askForEarlier() }}>Ask for them</Btn>
    </div>
  )
}

/** On my other devices (encrypted/card, card-sharing, card-shared, card-remove; §17.9 for a reader):
 *  one card per added device, answered once for every conversation and every device. No "Not now". */
export function AddedCards() {
  useVersion()
  const [rows, setRows] = useState<SessionRow[] | null>(null)
  useEffect(() => { void mySessions(0).then(setRows) }, [])
  const [said, setSaid] = useState<string[]>([]) // confirmations stay after their card went (card-shared, card-removed)
  const list = addedDevices()
  if (!list.length && !said.length && !listWarnings().fork && !listWarnings().strange && !firstScreenDevices().length) return null
  const name = (k: string) => (rows ?? []).find((r) => r.client_pubkey === k)?.device || (rows ?? []).find((r) => r.client_pubkey === k)?.application_name || 'A new device'
  const byKey = (k: string) => (rows ?? []).find((r) => r.client_pubkey === k)
  const warn = listWarnings()
  const first = firstScreenDevices()
  return <>
    {warn.fork && <div className="enc-card warn" role="alert"><b>Your belfry gave out two different lists of your devices</b><p>Something is wrong on the belfry’s side. Tell its admins; check Me › Devices.</p></div>}
    {warn.strange > 0 && <div className="enc-card warn" role="alert"><b>A device that was never on your list got your messages</b><p>Someone may be reading what is sent to you. Tell the belfry’s admins; check Me › Devices.</p></div>}
    {first.length > 0 && <div className="enc-card" role="region" aria-label="These devices can read your messages">
      <b>These devices can read your messages</b>
      {first.map((d) => <p key={d}>{name(d)}{byKey(d)?.created_at ? ` · added ${fmtDay(new Date(String(byKey(d)!.created_at)))}` : ''}</p>)}
      <p>Not yours? Remove it in Me › Devices.</p>
      <div className="row-btns two"><button className="btn ghost" onClick={() => go('me/devices')}>Me › Devices</button><button className="btn" onClick={() => void firstScreenOk()}>OK</button></div>
    </div>}
    {said.map((t, i) => <div key={i} className="enc-card done" role="status">{t}<button className="link" onClick={() => setSaid(said.filter((_, j) => j !== i))}>OK</button></div>)}
    {list.map((a) => <AddedCard key={a.device} a={a} name={a.reader ? displayName(a.device) : name(a.device)} onSaid={(t) => setSaid((x) => [...x, t])} bunker={(rows ?? []).find((r) => r.client_pubkey === a.device) as { bunker_pubkey?: string } | undefined} />)}
  </>
}
function AddedCard({ a, name, bunker, onSaid }: { a: AddedDevice; name: string; bunker?: { bunker_pubkey?: string }; onSaid: (t: string) => void }) {
  const [busy, setBusy] = useState<{ done: number; total: number } | null>(null)
  const [removing, setRemoving] = useState(false)
  const [error, setError] = useState('')
  const when = new Date(a.at * 1000)
  const at = fmtDay(when) + ' ' + fmtTime(a.at)
  return (
    <div className="enc-card" role="region" aria-label={`${name} was added`}>
      {a.gone ? <>
        <b>{name} was added and removed while you were away</b>
        <p>If you didn’t add it, someone used a code sent to your e-mail: change your e-mail password.</p>
        <button className="btn" onClick={() => goneOk(a.device)}>OK</button>
      </> : a.reader ? <>
        <b>{name} can now read your private messages</b>
        <button className="btn" onClick={async () => { await answerDevice(a, 'shared'); onSaid(`${name} reads your private messages from now on.`) }}>OK</button>
        <button className="link danger" onClick={() => setRemoving(true)}>Remove</button>
      </> : <>
        <b>{name} was added · {at}</b>
        <p>Share your earlier messages with it? If this wasn’t you, remove it.</p>
        <button className="btn" disabled={!!busy} onClick={async () => {
          setError(''); setBusy({ done: 0, total: 0 })
          try { await shareWith(a, (d, t) => setBusy({ done: d, total: t })); onSaid(`Earlier messages shared with ${name}`) } catch { setError('That did not work. Try again.') } finally { setBusy(null) }
        }}>{busy ? (busy.total ? `Sharing… ${busy.done} of ${busy.total}` : 'Sharing…') : 'Share earlier messages'}</button>
        {error && <p className="error" role="alert">{error}</p>}
        <button className="link danger" disabled={!!busy} onClick={() => setRemoving(true)}>Remove this device</button>
      </>}
      {removing && <RemoveSheet r={a.reader ? { kind: 'agent', key: a.device, name } : { kind: 'device', key: a.device, name, bunker: bunker?.bunker_pubkey }} onClose={() => setRemoving(false)}
        onDone={async (said) => { setRemoving(false); await answerDevice(a, 'removed'); onSaid(said) }} />}
    </div>
  )
}
