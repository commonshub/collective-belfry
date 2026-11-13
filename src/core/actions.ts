import { nip19 } from 'nostr-tools'
import { sha256 } from '@noble/hashes/sha256'
import { bytesToHex } from '@noble/hashes/utils'
import { BELFRY, LABEL_NS, TZ } from '../config'
import { EDIT_KIND, PIN_NS, hashtags, WEB_D, type WebPage, type ChannelVisibility, type Period, group, webPage, publicPeriods, slugify, INBOX_D, INBOX_NS, listIdFor, normTopic, PROPOSAL_NS, TOPIC_NS, VOTE_OPTIONS, type CalEvent, type Proposal, type ProposalStatus, type Rsvp, ANNOUNCE, mySeen, seenD } from './model'
import { enqueue, me, newId } from './sync'
import { reading } from './guestmode'

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

export interface PodAccess { closed: boolean; prompt: string }
export const DEFAULT_PROMPT = 'Why do you want to join, and what would you bring?'

/** NIP-29 metadata tags for a pod's access: `open` or `closed`, and our `prompt` (the question). */
const accessTags = (a: PodAccess) => [a.closed ? ['closed'] : ['open'], ...(a.closed ? [['prompt', a.prompt.trim() || DEFAULT_PROMPT]] : [])]

/** A pod (working group): its own group `pod-…`, announced in its parent (channel or pod),
 *  which is what links the two. Pods nest: a pod can hold pods. */
export async function createPod(name: string, about: string, parent = 'general', access: PodAccess = { closed: false, prompt: '' }) {
  const id = 'pod-' + slug(name).slice(0, 24) + '-' + newId(2)
  await enqueue('groups', { kind: 9007, tags: [['h', id]] })
  await enqueue('groups', { kind: 9002, tags: [['h', id], ['name', name], ['about', about], ...accessTags(access)] })
  await enqueue('groups', {
    kind: 9,
    content: `⬡ New pod: ${name}${about ? ' · ' + about : ''}`,
    tags: [['h', parent], ['pod', id]],
  })
  return id
}

export const editPod = (id: string, name: string, about: string, access: PodAccess) =>
  enqueue('groups', { kind: 9002, tags: [['h', id], ['name', name], ['about', about], ...accessTags(access)] })

/** NIP-29 join request: in an open group it lets you in; in a closed one it is your application,
 *  with your answer to the pod's question as its content. */
export const joinGroup = (id: string, answer = '') => enqueue('groups', { kind: 9021, content: answer.slice(0, 2000), tags: [['h', id]] })
export const leaveGroup = (id: string) => enqueue('groups', { kind: 9022, tags: [['h', id]] })
/** An admin admits an applicant (9000 put-user) or declines / removes them (9001 remove-user). */
export const admit = (id: string, pubkey: string) => enqueue('groups', { kind: 9000, tags: [['h', id], ['p', pubkey, 'member']] })
export const decline = (id: string, pubkey: string) => enqueue('groups', { kind: 9001, tags: [['h', id], ['p', pubkey]] })

export const editChannel = (id: string, name: string, about: string) =>
  enqueue('groups', { kind: 9002, tags: [['h', id], ['name', name], ['about', about]] })

/** A chat message. Its hashtags become `t` tags (NIP-24); `topic` (the open topic) is added as a
 *  `t` tag too, without being written in the text, and so is `announcement` for an admin's
 *  announcement (mailed to the channel's followers). */
/** The people a text mentions (NIP-27 nostr:npub…), as p tags: they get the mention by e-mail. */
export function mentionTags(text: string, skip: string[] = []): string[][] {
  const keys = new Set<string>()
  for (const m of text.matchAll(/nostr:(npub1[02-9ac-hj-np-z]{58})/g)) {
    try { const d = nip19.decode(m[1]); if (d.type === 'npub' && !skip.includes(d.data)) keys.add(d.data) } catch { /* not an npub */ }
  }
  return [...keys].map((k) => ['p', k])
}

export const postMessage = (groupId: string, text: string, files: File[], topic?: string, announce = false, extra: string[][] = []) =>
  enqueue('groups', {
    kind: 9, content: text,
    tags: [['h', groupId], ...[...new Set([...(topic ? [normTopic(topic)] : []), ...(announce ? [ANNOUNCE] : []), ...hashtags(text)])].map((t) => ['t', t]), ...mentionTags(text), ...extra],
  }, files)

/** A reply in the thread of message ROOT (NIP-22 comment, kept in the group by its `h` tag). */
export const postThreadReply = (groupId: string, root: { id: string; pubkey: string; kind: number }, text: string, files: File[]) =>
  enqueue('groups', {
    kind: 1111, content: text,
    tags: [['h', groupId], ['E', root.id, '', root.pubkey], ['K', String(root.kind)], ['P', root.pubkey],
      ['e', root.id, '', root.pubkey], ['k', String(root.kind)], ['p', root.pubkey], ...hashtags(text).map((t) => ['t', t]), ...mentionTags(text, [root.pubkey])],
  }, files)

/** Turn a channel's shared inbox on or off (admins: the bridge ignores anyone else's setting). */
export const setWebPage = (groupId: string, page: WebPage) =>
  enqueue('groups', { kind: 30078, content: JSON.stringify(page), tags: [['d', WEB_D + groupId], ['h', groupId], ['t', 'belfry-web']] })
/** Make a channel public or members only (its admins; #issues 2026-11-13). Never retroactive: going
 *  public opens a period from now (and turns its page on), going members only closes it, so what
 *  was posted before keeps the visibility it had. */
export async function setVisibility(groupId: string, visibility: ChannelVisibility) {
  const g = group(groupId)
  const w: WebPage = webPage(g) ?? { on: false, slug: slugify(g?.name ?? groupId), visibility: 'members' }
  const now = Math.floor(Date.now() / 1000)
  const periods = publicPeriods(g).map((p) => [...p] as Period)
  const open = periods.at(-1)?.[1] === null
  if (visibility === 'public' && !open) periods.push([now, null])
  if (visibility === 'members' && open) periods.at(-1)![1] = now
  const page: WebPage = { ...w, visibility, periods, on: visibility === 'public' ? true : w.on, slug: w.slug || slugify(g?.name ?? groupId) }
  await setWebPage(groupId, page)
  return page
}
export const setInbox = (groupId: string, on: boolean) =>
  enqueue('groups', { kind: 30078, content: JSON.stringify({ on }), tags: [['d', INBOX_D], ['h', groupId]] })

/** Close or reopen an e-mail card; ASSIGNEE '' = nobody. */
export const setCardStatus = (groupId: string, card: string, status: 'open' | 'closed') =>
  enqueue('groups', { kind: 1985, tags: [['h', groupId], ['L', INBOX_NS], ['l', status, INBOX_NS], ['e', card]] })
export const assignCard = (groupId: string, card: string, assignee: string) =>
  enqueue('groups', { kind: 1985, tags: [['h', groupId], ['L', INBOX_NS], ['l', 'assigned', INBOX_NS], ['e', card], ...(assignee ? [['p', assignee]] : [])] })

/** Pin / unpin a channel's topics: one NIP-32 label event with the whole list (latest wins). */
export const setTopics = (groupId: string, list: string[]) =>
  enqueue('groups', { kind: 1985, tags: [['h', groupId], ['L', TOPIC_NS], ...[...new Set(list.map(normTopic))].map((t) => ['l', t, TOPIC_NS])] })

/** Edit your own message or thread reply: a kind 1010 event in the group that names it (`e`) and
 *  carries the new text. Photos, the topic it was sent in and an announcement mark stay. */
export function editMessage(groupId: string, m: { ev: { id: string; kind: number; tags: string[][] }; text: string }, text: string) {
  const typed = new Set(hashtags(m.text))
  const kept = m.ev.tags.filter((t) => t[0] === 'imeta' || (t[0] === 't' && t[1] && !typed.has(normTopic(t[1]))))
  const urls = kept.filter((t) => t[0] === 'imeta').map((t) => t.find((p) => p.startsWith('url '))?.slice(4)).filter(Boolean)
  const topics = new Set([...kept.filter((t) => t[0] === 't').map((t) => normTopic(t[1])), ...hashtags(text)])
  return enqueue('groups', {
    kind: EDIT_KIND, content: [text, ...urls].filter(Boolean).join('\n'),
    tags: [['h', groupId], ['e', m.ev.id], ['k', String(m.ev.kind)], ...kept.filter((t) => t[0] === 'imeta'), ...[...topics].map((t) => ['t', t]), ...mentionTags(text)],
  })
}

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

/** A to-do in the channel's list; FROM = the message it was made from (shown under it). */
export const addTask = (groupId: string, title: string, from?: string) =>
  enqueue('groups', { kind: 2100, content: title.slice(0, 300), tags: [...taskTags(groupId), ...(from ? [['q', from]] : [])] })

/** Pin a message as the channel's notice, or unpin it (channel admins; model.pinned). */
export const pinMessage = (groupId: string, id: string, on: boolean) =>
  enqueue('groups', { kind: 1985, tags: [['h', groupId], ['L', PIN_NS], ['l', on ? 'pinned' : 'unpinned', PIN_NS], ['e', id]] })

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
  /** An online meeting (Google Meet, Zoom, Jitsi…): a NIP-52 `r` link. */
  online?: string
}

/** A video call link anyone can open straight away, no account to create it: Jitsi Meet. */
export const jitsiLink = (title: string) => `https://meet.jit.si/${slug(title).replace(/^channel-.*/, 'meeting')}-${newId(5)}`

export function meetingLabel(url: string) {
  const h = (() => { try { return new URL(url).hostname } catch { return '' } })()
  if (/meet\.google\.com$/.test(h)) return 'Google Meet'
  if (/(^|\.)zoom\.us$/.test(h)) return 'Zoom'
  if (/jit\.si$|jitsi/.test(h)) return 'Jitsi'
  if (/teams\.(microsoft|live)\.com$/.test(h)) return 'Teams'
  return h || 'Online'
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
  if (n.online && /^https:\/\//.test(n.online.trim())) tags.push(['r', n.online.trim()])
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

// ---- proposals ------------------------------------------------------------------------------

// website: a new version of the website (an nsite ref, #issues 2026-11-13): it goes live once a
// belfry admin marks the proposal Passed (website.py watches the card in #website)
export interface NewProposal { title: string; summary: string; text: string; status: ProposalStatus; website?: string }

const proposalTags = (d: string, groupId: string, n: NewProposal, version: number, status: ProposalStatus) => [
  ['d', d], ['title', n.title], ['summary', n.summary], ['t', 'proposal'], ['h', groupId, BELFRY.groupsRelay],
  ['version', String(version)], ['status', status], ['published_at', String(Math.floor(Date.now() / 1000))],
  ...(n.website ? [['t', 'website'], ['website', n.website]] : []),
]

/** A proposal: its own chat `prop-…` (for feedback), its text (NIP-23, version 1) and a card in
 *  the parent channel or pod. */
export async function createProposal(n: NewProposal, parent = 'general') {
  const x = newId(3)
  const groupId = 'prop-' + slug(n.title).slice(0, 24) + '-' + x
  const d = 'proposal-' + x
  await createChannel(n.title, n.summary || 'Proposal', groupId)
  await enqueue('main', { kind: 30023, content: n.text, tags: proposalTags(d, groupId, n, 1, n.status) })
  const coord = `30023:${me()}:${d}`
  await enqueue('groups', {
    kind: 9,
    content: `📝 Proposal: ${n.title}${n.summary ? ' · ' + n.summary : ''}`,
    tags: [['h', parent], ['proposal', groupId], ['a', coord, BELFRY.mainRelay], ...(n.website ? [['website', n.website]] : [])],
  })
  if (n.status === 'voting') await openVote(groupId, coord, n.title, 1)
  return groupId
}

/** A new version: the text replaced (same d, version + 1) and a note in its chat saying what changed. */
export async function reviseProposal(p: Proposal, n: Omit<NewProposal, 'status'>, note: string) {
  const version = p.version + 1
  await enqueue('main', { kind: 30023, content: n.text, tags: proposalTags(p.d, p.groupId, { ...n, status: p.status, website: p.website }, version, p.status) })
  await enqueue('groups', {
    kind: 9,
    content: `📝 Version ${version}${note.trim() ? ': ' + note.trim() : ''}`,
    tags: [['h', p.groupId], ['a', p.coord, BELFRY.mainRelay], ['version', String(version)]],
  })
  // a vote is on one version: a new version under vote opens a new poll
  if (p.status === 'voting') await openVote(p.groupId, p.coord, n.title, version)
}

const openVote = (groupId: string, coord: string, title: string, version: number) =>
  enqueue('groups', {
    kind: 1068,
    content: `${title} (version ${version}): do you approve?`,
    tags: [['h', groupId], ['a', coord], ['version', String(version)], ['polltype', 'singlechoice'], ...VOTE_OPTIONS.map(([id, label]) => ['option', id, label])],
  })

export async function setProposalStatus(p: Proposal, status: ProposalStatus) {
  await enqueue('groups', { kind: 1985, tags: [['h', p.groupId], ['L', PROPOSAL_NS], ['l', status, PROPOSAL_NS], ['a', p.coord]] })
  if (status === 'voting' && (!p.poll || p.poll.version !== p.version)) await openVote(p.groupId, p.coord, p.title, p.version)
}

export const vote = (p: Proposal, option: string) =>
  p.poll && enqueue('groups', { kind: 1018, tags: [['h', p.groupId], ['e', p.poll.ev.id], ['response', option]] })


// ---- drop box links: upload to a channel without an account --------------------------------

/** A new drop box link for a channel: a NIP-78 event in the group with a NIP-40 expiry. The
 *  token in its `d` tag is the link's only credential (https://<domain>/drop/<token>). */
export async function createDropLink(groupId: string, days: number, login: boolean) {
  const bytes = crypto.getRandomValues(new Uint8Array(16))
  const token = btoa(String.fromCharCode(...bytes)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
  const expires = Math.floor(Date.now() / 1000) + Math.round(days * 86400)
  await enqueue('groups', {
    kind: 30078, content: '',
    tags: [['d', `drop:${token}`], ['h', groupId], ['expiration', String(expires)], ...(login ? [['login']] : []),
      ['alt', 'Drop box link: upload photos and files to this channel']],
  })
  return token
}

export const dropUrl = (token: string) => `https://${BELFRY.domain}/drop/${token}`

/** End a drop box link (kind 5 by its author: the event and its address). */
export const endDropLink = (groupId: string, ev: { id: string; pubkey: string }, token: string) =>
  enqueue('groups', { kind: 5, tags: [['h', groupId], ['e', ev.id], ['a', `30078:${ev.pubkey}:drop:${token}`]] })

/** I opened this channel: tell it, at most every 6 hours (one replaceable event per channel). */
const seenSent = new Map<string, number>()
export function markSeen(groupId: string) {
  if (reading()) return // guest mode: reading leaves no trace
  const now = Date.now() / 1000
  const last = Math.max(mySeen(groupId), seenSent.get(groupId) ?? 0)
  if (now - last < 6 * 3600) return
  seenSent.set(groupId, now)
  void enqueue('groups', { kind: 30078, content: '', tags: [['d', seenD(groupId)], ['h', groupId]] })
}
