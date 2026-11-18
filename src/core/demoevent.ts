// Demo only (window.demoEvent): an event with tickets, a host, a co-host, a steward, attendees,
// check-ins and photos, as Belfry and the relay would have published them (docs/checkin.md).
// AS picks who I am in it: the host, a co-host, a steward, an attendee with a ticket, or someone
// with no place yet. TODAY puts it on today. PLAIN: no ticket types (the plain RSVP).
import { generateSecretKey, getPublicKey } from 'nostr-tools'
import { sha256 } from '@noble/hashes/sha256'
import { BELFRY } from '../config'
import { db } from './db'
import { knownGroupsKey } from './dm'
import { addEvents, touch } from './store'
import { profiles } from './model'
import { me } from './sync'

const hex = (n: number) => [...crypto.getRandomValues(new Uint8Array(n))].map((b) => b.toString(16).padStart(2, '0')).join('')

async function photo(hue: number, label: string): Promise<string> {
  const c = document.createElement('canvas'); c.width = 480; c.height = 360
  const x = c.getContext('2d')!
  const g = x.createLinearGradient(0, 0, 480, 360)
  g.addColorStop(0, `hsl(${hue} 55% 62%)`); g.addColorStop(1, `hsl(${(hue + 50) % 360} 60% 38%)`)
  x.fillStyle = g; x.fillRect(0, 0, 480, 360)
  x.fillStyle = 'rgba(255,255,255,.85)'; x.font = 'bold 34px sans-serif'; x.fillText(label, 24, 330)
  const blob = await new Promise<Blob>((r) => c.toBlob((b) => r(b!), 'image/png'))
  const key = 'demo-photo-' + hex(6)
  await (await db()).put('blobs', blob, key)
  return 'idb:' + key
}

export type DemoAs = 'host' | 'cohost' | 'steward' | 'attendee' | 'outsider'
export async function demoEvent(opts: { as?: DemoAs; today?: boolean; plain?: boolean; d?: string; slug?: string; title?: string; photos?: boolean; /** the same people every time (tests) */ seed?: string } = {}): Promise<string> {
  const { as = 'host', today = true, plain = false, slug = 'garden-day', title = 'Garden day', photos = true } = opts
  const d = opts.d ?? hex(3)
  const group = 'ev-' + d
  const now = Math.floor(Date.now() / 1000)
  const gk = knownGroupsKey() ?? 'f'.repeat(64)
  const relay = hex(32)
  const person = (name: string) => { const pk = getPublicKey(opts.seed ? sha256(new TextEncoder().encode(opts.seed + ':' + name)) : generateSecretKey()); return { pk, name } }
  const ana = person('Ana Duarte'), ben = person('Ben Okafor'), cleo = person('Cleo Martin')
  const crowd = ['Dan Weiss', 'Eve Laurent', 'Finn Byrne', 'Gus Peeters', 'Hana Sato', 'Ivo Janssens'].map(person)
  const creator = as === 'host' ? me() : ana.pk
  const cohost = as === 'cohost' ? me() : ben.pk
  const steward = as === 'steward' ? me() : cleo.pk
  const people = [ana, ben, cleo, ...crowd].filter((p) => p.pk !== creator || as !== 'host')
  const start = new Date()
  if (today) start.setHours(Math.min(start.getHours(), 14), 0, 0, 0)
  else { start.setDate(start.getDate() + 5); start.setHours(10, 0, 0, 0) }
  const end = new Date(+start + 6 * 3600e3)
  const cover = photos ? await photo(130, '') : ''
  const holders = [...crowd.map((p) => p.pk), steward, ...(as === 'attendee' ? [me()] : [])]
  const ticketD = plain ? `${d}:free` : `${d}:std`
  const evs: Record<string, unknown>[] = [
    ...people.map((p) => ({ id: hex(32), pubkey: p.pk, sig: hex(64), kind: 0, created_at: now - 3000, tags: [], content: JSON.stringify({ name: p.name }) })),
    { id: hex(32), pubkey: creator, sig: hex(64), kind: 9007, created_at: now - 2900, tags: [['h', group]], content: '' },
    { id: hex(32), pubkey: creator, sig: hex(64), kind: 9002, created_at: now - 2900, tags: [['h', group], ['name', title], ['about', 'The event’s chat'], ['open']], content: '' },
    { id: hex(32), pubkey: relay, sig: hex(64), kind: 39000, created_at: now - 2890, tags: [['d', group], ['name', title]], content: '' },
    { id: hex(32), pubkey: relay, sig: hex(64), kind: 39001, created_at: now - 2890, tags: [['d', group], ['p', creator]], content: '' },
    { id: hex(32), pubkey: creator, sig: hex(64), kind: 31923, created_at: now - 2880, content: 'We plant **beans**, share lunch and fix the shed.\n\nBring gloves and a cup.',
      tags: [['d', d], ['title', title], ['start', String(Math.floor(+start / 1000))], ['end', String(Math.floor(+end / 1000))], ['start_tzid', 'Europe/Brussels'],
        ['location', 'The community garden, Rue Haute 12'], ['h', group, BELFRY.groupsRelay], ['visibility', 'public'], ['slug', slug], ...(cover ? [['image', cover]] : [])] },
    // the co-host: put in with the admin role by the groups key (POST /group-roles)
    { id: hex(32), pubkey: gk, sig: hex(64), kind: 9000, created_at: now - 2800, tags: [['h', group], ['p', cohost, 'admin'], ['role', 'admin'], ['by', creator]], content: '' },
  ]
  if (!plain) {
    evs.push(
      { id: hex(32), pubkey: creator, sig: hex(64), kind: 30402, created_at: now - 2870, content: 'A place for the day', tags: [['d', `${d}:std`], ['a', `31923:${creator}:${d}`], ['h', group], ['title', 'Standard'], ['summary', 'A place for the day'], ['quantity', '40'], ['t', 'ticket'], ['position', '1']] },
      { id: hex(32), pubkey: creator, sig: hex(64), kind: 30402, created_at: now - 2870, content: '', tags: [['d', `${d}:sup`], ['a', `31923:${creator}:${d}`], ['h', group], ['title', 'Supporter'], ['t', 'ticket'], ['position', '2']] },
    )
  }
  holders.forEach((pk, i) => {
    const t = plain ? ticketD : i % 3 === 2 ? `${d}:sup` : `${d}:std`
    if (!plain) evs.push({ id: hex(32), pubkey: gk, sig: hex(64), kind: 9000, created_at: now - 2700 + i, tags: [['h', group], ['p', pk, 'member'], ['ticket', t], ['paid', 'free'], ['a', `31923:${creator}:${d}`]], content: '' })
    else evs.push({ id: hex(32), pubkey: gk, sig: hex(64), kind: 9000, created_at: now - 2700 + i, tags: [['h', group], ['p', pk, 'member']], content: '' })
    // their RSVP: going, one maybe
    evs.push({ id: hex(32), pubkey: pk, sig: hex(64), kind: 31925, created_at: now - 2600 + i, content: '',
      tags: [['d', hex(15)], ['a', `31923:${creator}:${d}`], ['status', i === 4 ? 'tentative' : 'accepted'], ['p', creator], ...(plain ? [] : [['ticket', t]]), ...(pk === me() ? [] : [['h', group]])] })
  })
  evs.push({ id: hex(32), pubkey: gk, sig: hex(64), kind: 9000, created_at: now - 2650, tags: [['h', group], ['p', ana.pk === creator ? ben.pk : ana.pk, 'member']], content: '' })
  // the steward (a label by the groups key) and two people checked in
  evs.push({ id: hex(32), pubkey: gk, sig: hex(64), kind: 1985, created_at: now - 2500, tags: [['L', 'belfry/group-role'], ['l', 'steward', 'belfry/group-role'], ['p', steward], ['h', group], ['by', creator]], content: '' })
  for (const [i, p] of crowd.slice(0, 2).entries())
    evs.push({ id: hex(32), pubkey: gk, sig: hex(64), kind: 1985, created_at: now - 1800 + i * 60, tags: [['L', 'belfry/ticket'], ['l', 'checked-in', 'belfry/ticket'], ['p', p.pk], ['h', group], ['by', steward]], content: '' })
  // a few messages and photos in its chat
  evs.push(
    { id: hex(32), pubkey: creator, sig: hex(64), kind: 9, created_at: now - 1500, tags: [['h', group]], content: 'Gate opens at 10. Coffee is on us ☕' },
    { id: hex(32), pubkey: crowd[0].pk, sig: hex(64), kind: 9, created_at: now - 1400, tags: [['h', group]], content: 'Can I bring my kids?' },
    { id: hex(32), pubkey: cohost, sig: hex(64), kind: 9, created_at: now - 1300, tags: [['h', group]], content: 'Of course! There is a sandpit.' },
  )
  if (photos) {
    for (const [i, hue] of [20, 200, 90, 300, 45].entries()) {
      const url = await photo(hue, ['Beans', 'The shed', 'Lunch', 'Seeds', 'Team'][i])
      evs.push({ id: hex(32), pubkey: [crowd[1], crowd[2], ana, ben, crowd[3]][i].pk, sig: hex(64), kind: 9, created_at: now - 1200 + i * 30, tags: [['h', group], ['imeta', `url ${url}`, 'm image/png', 'dim 480x360']], content: '' })
    }
  }
  // a name on my ticket (demo profiles start without one)
  if (!profiles().get(me())?.name) evs.push({ id: hex(32), pubkey: me(), sig: hex(64), kind: 0, created_at: now - 3000, tags: [], content: JSON.stringify({ name: 'Sam Rivera' }) })
  const members = [...new Set([creator, cohost, ...holders, ana.pk, ben.pk])]
  evs.push({ id: hex(32), pubkey: relay, sig: hex(64), kind: 39002, created_at: now - 1000, tags: [['d', group], ...members.map((p) => ['p', p])], content: '' })
  addEvents(evs as unknown as Parameters<typeof addEvents>[0])
  touch()
  Object.assign(window, { demoEventPeople: { ana: ana.pk, ben: ben.pk, cleo: cleo.pk, crowd: crowd.map((p) => p.pk), creator, cohost, steward } })
  return slug
}
