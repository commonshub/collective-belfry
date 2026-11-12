// Audio recordings (Xavier, 2026-11-12: "it would be terrible to lose the recording before it's
// properly uploaded. Always save locally first?"). Yes:
//   - MediaRecorder hands over a chunk every second; each one is written to IndexedDB at once
//     (store `blobs`, key rec:<id>:<part>:<n>), with the recording's state in `kv` (rec:<id>);
//     a reload, a crash or a dead battery loses a second or two at most;
//   - a new part starts every hour (a Blossom upload takes 25 MB; speech at 32 kbit/s is
//     ~14 MB an hour), each part a file that plays on its own;
//   - sending hands the parts to the outbox (core/sync.ts), which keeps them in IndexedDB too and
//     retries the upload until it is published; only then are the recording's chunks deleted;
//   - a recording that was not sent (stopped, or cut by a reload) is offered back in the chat.
// One recording at a time, kept here while its sheet is closed (the chat shows a pill).
import { db, kvDel, kvGet, kvSet } from './db'

export interface RecMeta {
  id: string
  groupId: string
  mime: string
  startedAt: number // ms
  /** chunks written per part */
  parts: number[]
  /** recorded time, ms (pauses not counted) */
  ms: number
  state: 'recording' | 'paused' | 'stopped'
  at: number
}

const LIST = 'rec:list'
const PART_MS = 60 * 60_000
const BITRATE = 32_000

type Listener = () => void
const listeners = new Set<Listener>()
const tell = () => { for (const l of listeners) l() }
export const subscribe = (l: Listener) => { listeners.add(l); return () => { listeners.delete(l) } }

/** The kind of audio this browser records (Chrome/Firefox: WebM Opus; Safari: MP4 AAC). */
export function audioMime(): string {
  const M = (globalThis as { MediaRecorder?: typeof MediaRecorder }).MediaRecorder
  if (!M) return ''
  for (const t of ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4;codecs=mp4a.40.2', 'audio/mp4', 'audio/ogg;codecs=opus'])
    if (M.isTypeSupported?.(t)) return t
  return ''
}
export const canRecord = () => !!navigator.mediaDevices?.getUserMedia && !!audioMime()

async function ids(): Promise<string[]> { return (await kvGet<string[]>(LIST)) ?? [] }
async function saveMeta(m: RecMeta) {
  m.at = Date.now()
  await kvSet('rec:' + m.id, m)
  const l = await ids()
  if (!l.includes(m.id)) await kvSet(LIST, [...l, m.id])
}

/** Recordings on this device not sent yet, newest first (a live one included). */
export async function unsent(): Promise<RecMeta[]> {
  const out: RecMeta[] = []
  for (const id of await ids()) { const m = await kvGet<RecMeta>('rec:' + id); if (m) out.push(m) }
  return out.sort((a, b) => b.startedAt - a.startedAt)
}

const ext = (mime: string) => (mime.includes('mp4') ? 'm4a' : mime.includes('ogg') ? 'ogg' : 'webm')
const stamp = (t: number) => { const d = new Date(t); const p = (n: number) => String(n).padStart(2, '0'); return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}` }

/** The recording's parts as files, from what is on the device. */
export async function filesOf(m: RecMeta): Promise<File[]> {
  const d = await db()
  const out: File[] = []
  const type = m.mime.split(';')[0]
  for (let p = 0; p < m.parts.length; p++) {
    const chunks: Blob[] = []
    for (let n = 0; n < m.parts[p]; n++) { const b = await d.get('blobs', `rec:${m.id}:${p}:${n}`); if (b) chunks.push(b) }
    if (!chunks.length) continue
    out.push(new File(chunks, `recording-${stamp(m.startedAt)}${m.parts.length > 1 ? `-part${p + 1}` : ''}.${ext(m.mime)}`, { type }))
  }
  return out
}

/** Forget a recording on this device (sent, or thrown away). */
export async function discard(id: string) {
  const m = await kvGet<RecMeta>('rec:' + id)
  const d = await db()
  if (m) for (let p = 0; p < m.parts.length; p++) for (let n = 0; n < m.parts[p]; n++) await d.delete('blobs', `rec:${id}:${p}:${n}`)
  await kvDel('rec:' + id)
  await kvSet(LIST, (await ids()).filter((x) => x !== id))
  if (live?.meta.id === id) live = null
  tell()
}

// ---- the one live recording -----------------------------------------------------------

interface Live {
  meta: RecMeta
  stream: MediaStream
  rec: MediaRecorder
  analyser?: AnalyserNode
  ctx?: AudioContext
  since: number // when the current stretch of recording began (ms), 0 while paused
  partBegan: number // recorded time (ms) when the current part began
  writes: Promise<void>
  wake?: { release: () => Promise<void> }
  timer?: ReturnType<typeof setInterval>
}
let live: Live | null = null
export const current = () => live?.meta ?? null
/** Recorded time now, ms. */
export const elapsed = () => (live ? live.meta.ms + (live.since ? Date.now() - live.since : 0) : 0)
/** The input level now, 0..1 (for the meter). */
export function level(): number {
  const a = live?.analyser
  if (!a || !live?.since) return 0
  const buf = new Uint8Array(a.fftSize)
  a.getByteTimeDomainData(buf)
  let peak = 0
  for (const v of buf) peak = Math.max(peak, Math.abs(v - 128))
  return Math.min(1, peak / 100)
}

function newRecorder(l: Live, part: number) {
  const rec = new MediaRecorder(l.stream, { mimeType: l.meta.mime, audioBitsPerSecond: BITRATE })
  if (l.meta.parts.length <= part) l.meta.parts.push(0)
  rec.ondataavailable = (ev) => {
    if (!ev.data || !ev.data.size) return
    const n = l.meta.parts[part]++
    const key = `rec:${l.meta.id}:${part}:${n}`
    // written in order, one after the other; the state goes with each chunk
    l.writes = l.writes.then(async () => {
      await (await db()).put('blobs', ev.data, key)
      await saveMeta({ ...l.meta, ms: elapsed() })
    }).catch((x) => console.error('recording: a chunk could not be saved', x))
    tell()
  }
  rec.start(1000)
  return rec
}

async function keepAwake(l: Live) {
  try { l.wake = await (navigator as unknown as { wakeLock?: { request: (t: string) => Promise<{ release: () => Promise<void> }> } }).wakeLock?.request('screen') } catch { /* not allowed: fine */ }
}

export async function start(groupId: string): Promise<RecMeta> {
  if (live) return live.meta
  const mime = audioMime()
  if (!mime) throw new Error('This browser cannot record audio.')
  const stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } })
  const meta: RecMeta = { id: crypto.randomUUID(), groupId, mime, startedAt: Date.now(), parts: [], ms: 0, state: 'recording', at: Date.now() }
  const l: Live = { meta, stream, rec: undefined as unknown as MediaRecorder, since: Date.now(), partBegan: 0, writes: Promise.resolve() }
  try {
    const AC = window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext
    l.ctx = new AC()
    l.analyser = l.ctx.createAnalyser()
    l.analyser.fftSize = 512
    l.ctx.createMediaStreamSource(stream).connect(l.analyser)
  } catch { /* no meter */ }
  live = l
  await saveMeta(meta)
  l.rec = newRecorder(l, 0)
  void keepAwake(l)
  // a new part every hour; the screen lock may have dropped the wake lock: ask again on return
  l.timer = setInterval(() => {
    if (live !== l || !l.since) return
    if (elapsed() - l.partBegan >= PART_MS) {
      l.partBegan = elapsed()
      const old = l.rec
      old.onstop = () => { if (live === l) l.rec = newRecorder(l, l.meta.parts.length) }
      old.stop()
    }
    tell()
  }, 1000)
  document.addEventListener('visibilitychange', () => { if (live === l && document.visibilityState === 'visible' && l.since) void keepAwake(l) })
  tell()
  return meta
}

export async function pause() {
  const l = live
  if (!l || !l.since) return
  l.meta.ms = elapsed(); l.since = 0; l.meta.state = 'paused'
  try { l.rec.requestData(); l.rec.pause() } catch { /* stopped */ }
  await saveMeta({ ...l.meta })
  tell()
}

export async function resume() {
  const l = live
  if (!l || l.since) return
  l.since = Date.now(); l.meta.state = 'recording'
  try { l.rec.resume() } catch { /* stopped */ }
  await saveMeta({ ...l.meta })
  tell()
}

/** Stop: the last chunk is written before this returns. */
export async function stop(): Promise<RecMeta | null> {
  const l = live
  if (!l) return null
  l.meta.ms = elapsed(); l.since = 0
  await new Promise<void>((res) => {
    if (l.rec.state === 'inactive') return res()
    l.rec.onstop = () => res()
    try { l.rec.stop() } catch { res() }
  })
  await l.writes
  l.stream.getTracks().forEach((t) => t.stop())
  if (l.timer) clearInterval(l.timer)
  try { await l.ctx?.close() } catch { /* closed */ }
  try { await l.wake?.release() } catch { /* released */ }
  l.meta.state = 'stopped'
  await saveMeta({ ...l.meta })
  live = null
  tell()
  return l.meta
}

/** A recording cut short (the page reloaded while it ran) counts as stopped. */
export async function recover(): Promise<RecMeta[]> {
  const list = await unsent()
  for (const m of list) if (m.state !== 'stopped' && live?.meta.id !== m.id) { m.state = 'stopped'; await saveMeta(m) }
  return list.filter((m) => live?.meta.id !== m.id && m.parts.some((n) => n > 0))
}

export const clock = (ms: number) => {
  const s = Math.floor(ms / 1000), h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60
  const p = (n: number) => String(n).padStart(2, '0')
  return h ? `${h}:${p(m)}:${p(x)}` : `${m}:${p(x)}`
}
