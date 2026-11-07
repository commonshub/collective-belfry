import { sha256 } from '@noble/hashes/sha256'
import { bytesToHex } from '@noble/hashes/utils'
import { BELFRY, LABEL_NS, TZ } from '../config'
import { listIdFor, type CalEvent, type Rsvp } from './model'
import { enqueue, me, newId } from './sync'

// Every user action is one or more nostr events queued in the outbox.

export function slug(name: string) {
  const s = name.toLowerCase().normalize('NFKD').replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 32)
  return s || 'channel-' + newId(3)
}

export async function createChannel(name: string, about: string, id = slug(name) + '-' + newId(2)) {
  await enqueue('groups', { kind: 9007, tags: [['h', id]] })
  await enqueue('groups', { kind: 9002, tags: [['h', id], ['name', name], ['about', about]] })
  return id
}

/** A pod (working group): its own group `pod-…`, announced in its parent (channel or pod),
 *  which is what links the two. Pods nest: a pod can hold pods. */
export async function createPod(name: string, about: string, parent = 'general') {
  const id = 'pod-' + slug(name).slice(0, 24) + '-' + newId(2)
  await createChannel(name, about, id)
  await enqueue('groups', {
    kind: 9,
    content: `⬡ New pod: ${name}${about ? ' · ' + about : ''}`,
    tags: [['h', parent], ['pod', id]],
  })
  return id
}

export const joinGroup = (id: string) => enqueue('groups', { kind: 9021, tags: [['h', id]] })
export const leaveGroup = (id: string) => enqueue('groups', { kind: 9022, tags: [['h', id]] })

export const editChannel = (id: string, name: string, about: string) =>
  enqueue('groups', { kind: 9002, tags: [['h', id], ['name', name], ['about', about]] })

export const postMessage = (groupId: string, text: string, files: File[]) =>
  enqueue('groups', { kind: 9, content: text, tags: [['h', groupId]] }, files)

export const deleteMessage = (groupId: string, id: string) =>
  enqueue('groups', { kind: 5, tags: [['h', groupId], ['e', id]] })

// ---- reactions: one tap adds, a second removes; the emojis you use most come first ------

const USAGE = 'belfry-emoji-usage'
export const DEFAULT_EMOJIS = ['👍', '❤️', '😂', '🎉', '🙏', '👀']

function usage(): Record<string, number> {
  try { return JSON.parse(localStorage.getItem(USAGE) ?? '{}') } catch { return {} }
}

/** Your most used emojis, topped up with the defaults. */
export function topEmojis(n = 6): string[] {
  const used = Object.entries(usage()).sort((a, b) => b[1] - a[1]).map(([e]) => e)
  return [...new Set([...used, ...DEFAULT_EMOJIS])].slice(0, n)
}

export async function react(groupId: string, msg: { id: string; pubkey: string }, emoji: string, mine?: { id: string; pending?: unknown }) {
  if (mine) {
    if (mine.pending) return // not sent yet: nothing to take back on the relay
    return enqueue('groups', { kind: 5, tags: [['h', groupId], ['e', mine.id], ['k', '7']] })
  }
  try {
    const u = usage()
    u[emoji] = (u[emoji] ?? 0) + 1
    localStorage.setItem(USAGE, JSON.stringify(u))
  } catch { /* private mode: no memory, defaults stay */ }
  return enqueue('groups', { kind: 7, content: emoji, tags: [['h', groupId], ['e', msg.id], ['p', msg.pubkey], ['k', '9']] })
}

export const setMemberLabels = (groupId: string, pubkey: string, labels: string[]) =>
  enqueue('groups', {
    kind: 1985,
    tags: [['h', groupId], ['L', LABEL_NS], ...labels.map((l) => ['l', l, LABEL_NS]), ['p', pubkey]],
  })

// tasklist.sh events also carry the group's h tag so the NIP-29 relay accepts them
const taskTags = (groupId: string) => [['t', listIdFor(groupId)], ['h', groupId]]

export const addTask = (groupId: string, title: string) =>
  enqueue('groups', { kind: 2100, content: title.slice(0, 300), tags: taskTags(groupId) })

export const taskAction = (groupId: string, taskId: string, action: 'claim' | 'unclaim' | 'done' | 'undone' | 'delete') =>
  enqueue('groups', { kind: 2101, tags: [['e', taskId], ['action', action], ...taskTags(groupId)] })

export const commentTask = (groupId: string, taskId: string, text: string) =>
  enqueue('groups', { kind: 2103, content: text.slice(0, 2000), tags: [['e', taskId], ...taskTags(groupId)] })

export interface NewEvent {
  title: string
  description: string
  location: string
  allDay: boolean
  start: string // datetime-local or date value
  end: string
}

// An event is a sub-channel of the channel it is posted in: a NIP-52 event on the community
// relay, linked both ways to its own NIP-29 group (session-<x> ↔ ws-<x>), and a post in the
// parent channel that links to it (`a` tag), shown there as the event's card.
export async function createEvent(n: NewEvent, channel = 'general') {
  const x = newId(4)
  const d = 'session-' + x
  const groupId = 'ws-' + x
  const ts = (v: string) => String(Math.floor(new Date(v).getTime() / 1000))
  const tags: string[][] = [['d', d], ['title', n.title], ['h', groupId, BELFRY.groupsRelay]]
  if (n.allDay) {
    tags.push(['start', n.start.slice(0, 10)])
    if (n.end) tags.push(['end', n.end.slice(0, 10)])
  } else {
    tags.push(['start', ts(n.start)], ['start_tzid', TZ])
    if (n.end) tags.push(['end', ts(n.end)], ['end_tzid', TZ])
  }
  if (n.location) tags.push(['location', n.location])
  const kind = n.allDay ? 31922 : 31923
  await createChannel(n.title, n.description.split('\n')[0] || 'Chat for this event', groupId)
  await enqueue('main', { kind, content: n.description, tags })
  const when = n.allDay ? n.start.slice(0, 10) : new Date(n.start).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' })
  await enqueue('groups', {
    kind: 9,
    content: `📅 ${n.title} · ${when}${n.location ? ' · ' + n.location : ''}`,
    tags: [['h', channel], ['a', `${kind}:${me()}:${d}`, BELFRY.mainRelay]],
  })
  return `${kind}:${me()}:${d}`
}

/** A chat for an event that has none (e.g. the village programme): same ws- convention. */
export const openEventChat = (e: CalEvent) => createChannel(e.title, 'Chat about this session', e.groupId)

export const rsvp = (e: CalEvent, status: Rsvp) => {
  const [, author, d] = e.coord.split(':')
  return enqueue('main', {
    kind: 31925,
    tags: [
      ['d', bytesToHex(sha256(`${me()}:${author}:${d}`)).slice(0, 30)],
      ['a', e.coord], ...(e.ev.pending ? [] : [['e', e.ev.id]]), ['status', status], ['p', author],
    ],
  })
}

export const deleteEvent = (e: CalEvent) =>
  enqueue('main', { kind: 5, tags: [['a', e.coord], ['e', e.ev.id]] })

export const isMine = (pubkey: string) => pubkey === me()
