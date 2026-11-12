// Meetings (Xavier, 2026-11-12): a members-only event made in a few taps from a channel's ＋ menu.
// A topic, the people (picked from the most active members of the channel or its parent), a date
// and time, a length, a place (the last one by default). Under the hood it is a Belfry event
// (core/events.ts), so it shows in upcoming events, has its own chat, RSVPs and to-dos:
//   - the event (NIP-52) carries ['t', 'meeting'], ['channel', <where it was made>] and one
//     ['p', <pubkey>, '', 'invited'] per person invited; visibility members;
//   - its chat (ev-<d>) is closed, the invited people are put in it (kind 9000, no ticket);
//   - attendance: a host marks someone there or not (a NIP-32 label in the chat, namespace
//     belfry/attendance, `l` attended | absent, `p` the person, `a` the event), for people who
//     don't use their phone. Their own RSVP still counts as "going".
import { calendar, group, recentlyActive, profiles, displayName, type CalEvent } from './model'
import { all } from './store'
import { enqueue, me } from './sync'
import { createEvent, localTz, zonedToUtc, utcToZoned } from './events'

export const ATTENDANCE_NS = 'belfry/attendance'
const tag = (e: { tags: string[][] }, k: string) => e.tags.find((t) => t[0] === k)?.[1]

export interface MeetingForm { topic: string; people: string[]; start: string /* YYYY-MM-DDTHH:mm, local */; minutes: number; place: string; online: string }
export const LENGTHS = [30, 45, 60, 90, 120]

/** Make a meeting from channel CHANNEL; returns the event's id (d). */
export async function createMeeting(channel: string, m: MeetingForm): Promise<string> {
  const tz = localTz()
  const end = utcToZoned(new Date((zonedToUtc(m.start, tz) + m.minutes * 60) * 1000), tz)
  const people = [...new Set(m.people)].filter((p) => p !== me())
  const d = await createEvent(
    { title: m.topic.trim(), description: '', allDay: false, start: m.start, end, tz, location: m.place.trim(), online: m.online.trim(), visibility: 'members', slug: '', closed: true },
    [['t', 'meeting'], ['channel', channel], ...people.map((p) => ['p', p, '', 'invited'])],
  )
  for (const p of people) await enqueue('groups', { kind: 9000, tags: [['h', 'ev-' + d], ['p', p, 'member']] })
  return d
}

/** Who the hosts marked as there (true) or not (false), latest label per person. */
export function attendance(e: CalEvent): Map<string, boolean> {
  const out = new Map<string, { at: number; on: boolean }>()
  for (const x of all(me())) {
    if (x.kind !== 1985 || tag(x, 'h') !== e.groupId || tag(x, 'L') !== ATTENDANCE_NS || !e.hosts.has(x.pubkey)) continue
    const p = tag(x, 'p'); const l = x.tags.find((t) => t[0] === 'l')?.[1]
    if (!p || (l !== 'attended' && l !== 'absent')) continue
    const had = out.get(p)
    if (!had || had.at <= x.created_at) out.set(p, { at: x.created_at, on: l === 'attended' })
  }
  return new Map([...out].map(([k, v]) => [k, v.on]))
}

/** A host marks PUBKEY as there (ON) or not. */
export const checkIn = (e: CalEvent, pubkey: string, on: boolean) =>
  enqueue('groups', { kind: 1985, content: '', tags: [['h', e.groupId], ['L', ATTENDANCE_NS], ['l', on ? 'attended' : 'absent', ATTENDANCE_NS], ['p', pubkey], ['a', e.coord]] })

/** People to suggest first: the most recently active in CHANNEL, then in its parent, then its
 *  members; me left out. */
export function suggestions(channel: string): string[] {
  const g = group(channel)
  const parent = g?.parent ? group(g.parent) : undefined
  const keys = [
    ...recentlyActive(channel).sort((a, b) => b.at - a.at).map((a) => a.pubkey),
    ...(parent ? recentlyActive(parent.id).sort((a, b) => b.at - a.at).map((a) => a.pubkey) : []),
    ...(g?.members ?? []), ...(parent?.members ?? []),
  ]
  return [...new Set(keys)].filter((k) => k !== me() && /^[0-9a-f]{64}$/.test(k))
}

/** Anyone whose name matches Q (for the autocomplete), suggestions first. */
export function findPeople(channel: string, q: string, limit = 8): string[] {
  const s = q.trim().toLowerCase()
  const first = suggestions(channel)
  if (!s) return first.slice(0, limit)
  const pool = [...new Set([...first, ...profiles().keys()])].filter((k) => k !== me())
  return pool.filter((k) => displayName(k).toLowerCase().includes(s) || (profiles().get(k)?.name ?? '').toLowerCase().includes(s)).slice(0, limit)
}

/** The place of my last meeting (then of my last event), for the default. */
export function lastPlace(): string {
  const mine = calendar().filter((e) => e.creator === me() && e.location?.trim()).sort((a, b) => b.ev.created_at - a.ev.created_at)
  return (mine.find((e) => e.meeting) ?? mine[0])?.location ?? ''
}
