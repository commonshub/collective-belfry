// What I have read, the same on all my devices (Android SPEC.md 19.27; the rules are readcore.ts).
// One replaceable event: kind 30078, d = belfry:read, content = NIP-44 v2 to my own key, on the
// community relay (members only). Every belfry:read event of mine that arrives is merged in (the
// highest mark wins: reading never un-reads); when this device knows more than the newest one on
// the relay, it publishes the merged state, debounced, through the outbox. Nothing is published
// before the relay has answered this session (a new device must not overwrite what others marked)
// nor by a guest (the community relay is not theirs): their read state stays on the device.
import { getBackend } from '../config'
import { kvGet, kvSet } from './db'
import { reading } from './guestmode'
import { isGuest } from './invite'
import { READ_D, covers, empty, mark, merge, parse, prune, readUpTo as upTo, serialize, type ReadState, type Section } from './readcore'
import { addEvents, all, dropOutbox, outboxItems, subscribe, touch } from './store'
import { currentSigner, enqueue, isLocalOnly, me, onEose } from './sync'

const KV = 'readstate'
const MENTIONS_SEEN = 'osv-mentions-seen' // the @Mentions list's old mark (localStorage), seeded once

let local: ReadState = empty()
let remote: { at: number; id: string; state: ReadState } = { at: 0, id: '', state: empty() }
let checked = false // the community relay answered this session (EOSE)
let loaded = false // the stored state is in memory (nothing merges or publishes before)
const done = new Set<string>() // belfry:read event ids merged already
let lastPublish = 0
let timer: ReturnType<typeof setTimeout> | undefined
let saveTimer: ReturnType<typeof setTimeout> | undefined

const nowS = () => Math.floor(Date.now() / 1000)
const dTag = (e: { tags: string[][] }) => e.tags.find((t) => t[0] === 'd')?.[1]

let readyResolve!: () => void
/** Resolves once the stored state is loaded (dm.ts waits for it before reading its marks). */
export const readReady = new Promise<void>((r) => { readyResolve = r })

/** Read up to when (unix s): the key's own mark, or the floor (SPEC 19.27). */
export const readUpTo = (sec: Section | 'm', key = '') => upTo(local, sec, key, nowS())
/** The key's own mark (0 when none): whether I opened this thread before. */
export const ownMark = (sec: Section, key: string) => local[sec][key] ?? 0

/** Read SEC/KEY up to AT (never backwards). */
export function markRead(sec: Section | 'm', key: string, at: number) {
  if (!loaded) return
  const next = mark(local, sec, key, at)
  if (next === local) return
  local = next
  changed()
}

function changed() {
  touch()
  clearTimeout(saveTimer)
  saveTimer = setTimeout(() => void kvSet(KV, serialize(local)), 400)
  schedule()
}

/** Is this device's read state shared (published)? Not for guests, nor in guest mode. */
const shared = () => !reading() && !isGuest(me())

// ---- publishing (debounced: 3 s after the last change, at most one every 10 s) -------------

function schedule() {
  if (!loaded || !checked || !shared()) return
  if (covers(remote.state, local, nowS())) return
  clearTimeout(timer)
  const wait = Math.max(3000, lastPublish + 10_000 - Date.now())
  timer = setTimeout(() => void publish(), wait)
}

async function publish() {
  clearTimeout(timer)
  timer = undefined
  if (!checked || !shared() || !me()) return
  const now = nowS()
  if (covers(remote.state, local, now)) return
  const out = prune(merge(local, remote.state), now)
  let content: string
  try { content = await currentSigner().encrypt(me(), serialize(out)) } catch { schedule(); return }
  // a read state still waiting in the outbox is out of date now: this one replaces it
  for (const o of outboxItems()) if (o.kind === 30078 && o.status === 'queued' && !o.tries && o.tags.some((t) => t[0] === 'd' && t[1] === READ_D)) await dropOutbox(o.localId)
  try {
    await enqueue('main', { kind: 30078, content, tags: [['d', READ_D]] })
  } catch { return } // guest mode refused it: stays on the device
  lastPublish = Date.now()
  remote = { ...remote, state: out } // what the relay will hold; its echo merges in harmlessly
}

/** Send now what waits (the page goes to the background or closes). */
function flushNow() { if (timer) void publish() }

// ---- receiving ---------------------------------------------------------------------------

let scanning = false, rescan = false
async function scan() {
  if (!loaded) return
  if (scanning) { rescan = true; return }
  scanning = true
  try {
    const mine = me()
    if (!mine) return
    for (const e of all(mine)) {
      if (e.kind !== 30078 || e.pending || e.pubkey !== mine || done.has(e.id) || dTag(e) !== READ_D) continue
      done.add(e.id)
      let st: ReadState
      try { st = parse(await currentSigner().decrypt(mine, e.content)) } catch { continue }
      const before = serialize(local)
      local = merge(local, st)
      if (e.created_at > remote.at || (e.created_at === remote.at && e.id < remote.id)) remote = { at: e.created_at, id: e.id, state: st }
      if (serialize(local) !== before) changed()
      else schedule() // the newest on the relay may lack what this device knows
    }
  } finally {
    scanning = false
    if (rescan) { rescan = false; void scan() }
  }
}

// ---- start -----------------------------------------------------------------------------

let owner = '' // whose state is in memory (another account in the same page starts afresh)
let listening = false
export async function startReadState() {
  if (owner === me()) return
  owner = me()
  local = empty(); remote = { at: 0, id: '', state: empty() }; checked = false; loaded = false; done.clear(); lastPublish = 0
  clearTimeout(timer); timer = undefined
  // (before any await: the relay's first EOSE may come while the stored state loads)
  if (!listening) {
    listening = true
    onEose((target) => { if (target === 'main' && !checked) { checked = true; void scan().then(schedule) } })
    let t: ReturnType<typeof setTimeout> | null = null
    subscribe(() => { t ??= setTimeout(() => { t = null; void scan() }, 250) })
    document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'hidden') flushNow() })
    window.addEventListener('pagehide', flushNow)
  }
  try {
    const kept = await kvGet<string>(KV)
    if (kept) local = parse(kept)
    else { // first run: start now, with this device's old marks (SPEC 19.27 "migrating")
      local = { ...empty(), s: nowS() }
      for (const [conv, at] of Object.entries((await kvGet<Record<string, number>>('dm-read')) ?? {})) local = mark(local, 'd', conv, at)
      try { local = mark(local, 'm', '', Number(localStorage.getItem(MENTIONS_SEEN) || 0)) } catch { /* private mode */ }
      void kvSet(KV, serialize(local))
    }
  } finally {
    loaded = true
    readyResolve()
  }
  // demo mode: no relay to wait for (its "publish" lands in the local log, like another device's)
  if (isLocalOnly()) checked = true
  void scan().then(schedule)
}

// browser tests (?backend=demo): look at the state, and make "another device's" event
if (typeof window !== 'undefined' && getBackend() === 'demo') {
  const w = window as unknown as Record<string, unknown>
  w.__readState = () => JSON.parse(serialize(local))
  // two "devices" in two browser profiles: copy events from one to the other
  w.__demoExport = () => all(me()).filter((e) => !e.pending)
  w.__demoAddEvents = (list: Parameters<typeof addEvents>[0]) => addEvents(list)
  w.__readStateRemote = async (json: Record<string, unknown>) => {
    if (!isLocalOnly()) throw new Error('demo only')
    const s = currentSigner()
    const ev = await s.sign({ kind: 30078, created_at: nowS(), tags: [['d', READ_D], ['client', 'other-device']], content: await s.encrypt(me(), JSON.stringify(json)) })
    addEvents([ev])
  }
}
