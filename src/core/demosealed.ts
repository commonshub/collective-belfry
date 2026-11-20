// Demo only (window.demoSealed): the people and conversations of the "Messages · encryption"
// boards, so the screens can be seen and tested without the wire (docs/sealed.md is not here yet).
// AS 'old': this device has read everything (the thread with Lina's device lines, a group of 5,
// an e-mail thread). AS 'new': this device just signed in, earlier messages are locked here.
import { getPublicKey } from 'nostr-tools'
import { BELFRY } from '../config'
import { sha256 } from '@noble/hashes/sha256'
import { demoDm, demoMail, mailThreadOf } from './dm'
import { profiles } from './model'
import { setDemoSealed, type DemoSealed, type DeviceKind } from './sealed'
import { addEvents, touch } from './store'
import { isLocalOnly, me } from './sync'

const hex = (n: number) => [...crypto.getRandomValues(new Uint8Array(n))].map((b) => b.toString(16).padStart(2, '0')).join('')
const keyOf = (name: string) => sha256(new TextEncoder().encode('demo-sealed:' + name))
const DAY = 864e5

/** today (or DAYS ago) at H:M, in ms */
const at = (days: number, h: number, m: number) => { const d = new Date(); d.setDate(d.getDate() - days); d.setHours(h, m, 0, 0); return Math.min(+d, Date.now() - 60e3) }
const s = (ms: number) => Math.floor(ms / 1000)

export async function demoSealed(opts: { as?: 'old' | 'new' | 'received'; ask?: string; askKind?: DeviceKind; linked?: string } = {}) {
  if (!isLocalOnly()) return null
  const as = opts.as ?? 'old'
  const names = ['Lina Moreau', 'Ana Silva', 'Ben Okafor', 'Emma Lind', 'Dev Patel', 'Lou (neighbour)']
  const pk = Object.fromEntries(names.map((n) => [n.split(' ')[0].toLowerCase(), getPublicKey(keyOf(n))])) as Record<'lina' | 'ana' | 'ben' | 'emma' | 'dev' | 'lou', string>
  const sk = (first: string) => keyOf(names.find((n) => n.toLowerCase().startsWith(first))!)
  const now = s(Date.now())
  const evs = names.map((n) => ({ id: hex(32), pubkey: getPublicKey(keyOf(n)), sig: hex(64), kind: 0, created_at: now - 9000, tags: [], content: JSON.stringify({ name: n }) }))
  // me: Sam Rivera, with a username here (Link your phone needs one)
  const mine = profiles().get(me())
  if (!mine?.name || !mine?.nip05) evs.push({ id: hex(32), pubkey: me(), sig: hex(64), kind: 0, created_at: now, tags: [], content: JSON.stringify({ ...mine, name: mine?.name || 'Sam Rivera', nip05: mine?.nip05 || 'sam@' + BELFRY.domain }) })
  addEvents(evs as unknown as Parameters<typeof addEvents>[0])

  const lina = 'n:' + pk.lina, ana = 'n:' + pk.ana, dev = 'n:' + pk.dev
  const five = 'n:' + [pk.ana, pk.ben, pk.emma, pk.lina].sort().join(',')
  const two = 'n:' + [pk.emma, pk.ben].sort().join(',')
  const mailConv = mailThreadOf('events@townhall.example.org', 'Street party permit')

  if (as === 'old') {
    await demoDm([pk.lina], 'Can you bring the long ladder on Saturday?', s(at(1, 18, 2)), sk('lina'))
    await demoDm([pk.lina], 'Yes, I’ll drop it at the hall at 8:30.', s(at(1, 18, 10)))
    await demoDm([pk.lina], 'Thanks! The key is under the blue pot.', s(at(0, 9, 14)), sk('lina'))
    await demoDm([pk.lina], 'Writing from my new phone 👋', s(at(0, 9, 40)), sk('lina'))
    await demoDm([pk.ana, pk.ben, pk.emma, pk.lina], 'Who has the keys to the shed this weekend?', s(at(0, 8, 30)), sk('ana'))
    await demoDm([pk.ana, pk.ben, pk.emma, pk.lina], 'I do, until Sunday.', s(at(0, 8, 41)), sk('ben'))
    await demoMail('Town Hall <events@townhall.example.org>', 'Street party permit', 'Hello, your request for Mill Lane on 14 June is approved. Please put the signs out by 8:00.')
  } else if (as === 'received') {
    // canvas v86 messages/encryption-shared-history: I started fresh, Lina shared our history
    await demoDm([pk.lina], 'Can you water the tomatoes while I’m away?', s(at(17, 18, 10)))
    await demoDm([pk.lina], 'Of course, leave the key with Ana.', s(at(17, 18, 12)), sk('lina'))
    await demoDm([pk.lina], 'Lost my old phone, writing from the new one', s(at(0, 9, 40)))
  } else {
    // before Lina's conversation was sealed (reads as usual), then sealed ones (locked here)
    await demoDm([pk.lina], 'Can you water the tomatoes while I’m away?', s(at(17, 18, 10)), sk('lina'))
    await demoDm([pk.lina], 'Of course, leave the key with Ana.', s(at(17, 18, 12)))
    await demoDm([pk.lina], 'Welcome on the new phone!', s(Date.now() - 60e3), sk('lina'))
    await demoDm([pk.lou], 'See you at the market', s(Date.now() - 3600e3), sk('lou'))
    await demoDm([pk.ana], 'See you at the repair café.', s(Date.now() - 2 * 3600e3), sk('ana'))
    await demoDm([pk.emma, pk.ben], 'Here is the plan for the garden.', s(at(3, 10, 0)), sk('emma'))
    await demoDm([pk.dev], 'Thanks for the drill.', s(Date.now() - 2 * DAY), sk('dev'))
  }

  const phone = { id: 'd-iphone', name: 'iPhone', kind: 'phone' as const, added: new Date(new Date().getFullYear(), 7, 12).getTime(), active: Date.now() }
  const mac = { id: 'd-mac', name: 'Safari on Mac', kind: 'computer' as const, added: new Date(new Date().getFullYear(), 9, 2).getTime(), from: 'iPhone', active: Date.now() - 3 * DAY }
  const d: DemoSealed = {
    mine: as === 'old' ? [{ ...phone, here: true }, mac] : [phone, { ...mac, here: true, added: Date.now() - 5 * 60e3, from: undefined, active: Date.now() }],
    people: {
      [pk.lina]: { count: 2, change: { what: 'removed', at: at(0, 8, 2) } },
      [pk.ana]: { count: 3, change: { what: 'added', at: at(0, 7, 50) } },
      [pk.ben]: { count: 1 },
      [pk.emma]: { count: 2, change: { what: 'removed', at: Date.now() - 18 * DAY } },
      [pk.dev]: { count: 1 },
    },
    sealed: [lina, ana, dev, five, two, mailConv],
    lines: as === 'old' ? {
      [lina]: [
        { at: at(1, 18, 30), who: pk.lina, what: 'added' },
        { at: at(0, 8, 2), who: pk.lina, what: 'removed' },
        { at: at(0, 8, 5), who: me(), what: 'added', device: 'Safari on Mac', kind: 'computer' },
        { at: at(0, 9, 38), who: pk.lina, what: 'new-locked', kind: 'phone' },
      ],
      [five]: [{ at: at(0, 7, 50), who: pk.ana, what: 'added' }],
    } : {},
    lockedHere: as === 'new' ? [lina, ana, two, dev] : [],
    sealedAt: { [lina]: at(16, 0, 0), [ana]: at(40, 0, 0), [two]: at(40, 0, 0), [dev]: at(40, 0, 0), [five]: at(40, 0, 0), [mailConv]: at(40, 0, 0) },
    ...(opts.ask ? { asking: { name: opts.ask, kind: opts.askKind ?? 'computer', at: Date.now() - 120e3 } } : {}),
    ...(opts.linked ? { linked: { name: opts.linked, kind: 'tablet' as const } } : {}),
  }
  if (as === 'received') {
    const { thread } = await import('./dm')
    await new Promise((r) => setTimeout(r, 600)) // dm.ts reads the wraps
    d.shared = Object.fromEntries(thread(lina).filter((m) => m.at * 1000 < at(1, 0, 0)).map((m) => [m.rid, pk.lina]))
  }
  setDemoSealed(d)
  touch()
  return { conv: { lina, ana, dev, five, two, mail: mailConv }, pk }
}
