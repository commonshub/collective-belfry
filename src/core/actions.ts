import { LABEL_NS, TZ } from '../config'
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

export const editChannel = (id: string, name: string, about: string) =>
  enqueue('groups', { kind: 9002, tags: [['h', id], ['name', name], ['about', about]] })

export const postMessage = (groupId: string, text: string, files: File[]) =>
  enqueue('groups', { kind: 9, content: text, tags: [['h', groupId]] }, files)

export const deleteMessage = (groupId: string, id: string) =>
  enqueue('groups', { kind: 5, tags: [['h', groupId], ['e', id]] })

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

// NIP-52 calendar event on the main relay, plus a NIP-29 group for its chat.
export async function createEvent(n: NewEvent) {
  const d = newId(6)
  const groupId = 'ev-' + d
  const ts = (v: string) => String(Math.floor(new Date(v).getTime() / 1000))
  const tags: string[][] = [['d', d], ['title', n.title], ['h', groupId]]
  if (n.allDay) {
    tags.push(['start', n.start.slice(0, 10)])
    if (n.end) tags.push(['end', n.end.slice(0, 10)])
  } else {
    tags.push(['start', ts(n.start)], ['start_tzid', TZ])
    if (n.end) tags.push(['end', ts(n.end)], ['end_tzid', TZ])
  }
  if (n.location) tags.push(['location', n.location])
  await enqueue('main', { kind: n.allDay ? 31922 : 31923, content: n.description, tags })
  await createChannel(n.title, n.description || 'Chat for this event', groupId)
  return groupId
}

export const rsvp = (e: CalEvent, status: Rsvp) =>
  enqueue('main', {
    kind: 31925,
    tags: [['d', 'rsvp-' + e.coord.split(':').slice(1).join('-')], ['a', e.coord], ...(e.ev.pending ? [] : [['e', e.ev.id]]), ['status', status], ['p', e.ev.pubkey]],
  })

export const deleteEvent = (e: CalEvent) =>
  enqueue('main', { kind: 5, tags: [['a', e.coord], ['e', e.ev.id]] })

export const isMine = (pubkey: string) => pubkey === me()
