import { Relay } from 'nostr-tools/relay'

// The one connection manager (the lead, from Xavier, 2026-11-21: DMs stuck on "sending…" on iOS).
// Every WebSocket the app opens is a Sock here: the groups relay, the community relay and the
// signer's relay (NIP-46). Two users of the same URL share one socket. The manager:
// - checks every socket at once when the app comes back to the foreground or online: no answer in
//   5 s → dropped and reconnected;
// - while the app is visible, asks a socket that has been quiet for 25 s whether it still answers;
// - before a send, checks a socket that has been quiet for over 10 s (fresh());
// - a timeout on a socket (publish, a signer call) drops it (drop());
// - reconnects after 1 s, 2 s, 4 s, then backs off up to 30 s.

export const CHECK_MS = 5_000 // an answer to a check within this, or the socket is dead
export const KEEPALIVE_MS = 25_000 // quiet this long while visible: checked
export const BEFORE_SEND_MS = 10_000 // quiet this long before a send: checked first

/** 1 s, 2 s, 4 s, 8 s, 16 s, then 30 s (transient retries and reconnects). */
export const backoffMs = (step: number) => Math.min(30_000, 1000 * 2 ** Math.max(0, step))

const norm = (u: string) => u.trim().replace(/\/+$/, '').toLowerCase()

export interface SockHooks {
  /** a new socket is open (subscribe again here) */
  open?: (r: Relay) => void
  /** the socket is gone (closed, dropped, or failed to open) */
  down?: () => void
  /** the relay asks for NIP-42 AUTH on this socket */
  auth?: (r: Relay) => void
}

export class Sock {
  readonly url: string
  relay?: Relay
  lastRx = 0
  private hooks = new Set<SockHooks>()
  private opening?: Promise<Relay>
  private checking?: Promise<boolean>
  private attempts = 0
  private timer?: ReturnType<typeof setTimeout>
  /** reconnect by itself after a drop (the relays always; the signer's once it was used) */
  wanted = false
  /** how many times this socket was dropped as dead, and the last reason (Diagnostics) */
  drops = 0
  lastDrop = ''

  constructor(url: string) { this.url = url }

  listen(h: SockHooks) { this.hooks.add(h); return () => { this.hooks.delete(h) } }
  get connected() { return !!this.relay?.connected }
  quietFor() { return Date.now() - this.lastRx }

  /** The open socket, opened if needed (at most one opening at a time). */
  get(): Promise<Relay> {
    if (this.relay?.connected) return Promise.resolve(this.relay)
    return (this.opening ??= this.open().finally(() => { this.opening = undefined }))
  }

  private async open(): Promise<Relay> {
    const r = new Relay(this.url)
    // every frame from the relay counts as a sign of life
    const onmessage = r._onmessage.bind(r)
    r._onmessage = (ev: MessageEvent) => { this.lastRx = Date.now(); onmessage(ev) }
    r._onauth = () => { if (this.relay === r) this.hooks.forEach((h) => h.auth?.(r)) }
    r.onclose = () => { if (this.relay === r) this.gone() }
    try {
      await r.connect()
    } catch (e) {
      this.hooks.forEach((h) => h.down?.())
      this.later()
      throw e
    }
    this.relay = r
    this.lastRx = Date.now()
    this.attempts = 0
    clearTimeout(this.timer)
    this.hooks.forEach((h) => h.open?.(r))
    return r
  }

  private gone() {
    this.relay = undefined
    this.hooks.forEach((h) => h.down?.())
    this.later()
  }

  /** Reconnect after the next back-off step, when wanted and online. */
  private later() {
    if (!this.wanted) return
    clearTimeout(this.timer)
    this.timer = setTimeout(() => { if (this.wanted && navigator.onLine) void this.get().catch(() => {}) }, backoffMs(this.attempts++))
  }

  /** Close this socket (dead, or a timeout on it) and reconnect at once when wanted. */
  drop(why = '') {
    const r = this.relay
    if (!r) return
    if (why) { console.warn('dropping the socket', this.url, why); this.drops++; this.lastDrop = why }
    this.relay = undefined
    r.onclose = null
    try { r.close() } catch { /* already closed */ }
    this.hooks.forEach((h) => h.down?.())
    this.attempts = 0
    if (this.wanted && navigator.onLine) void this.get().catch(() => {})
  }

  /** Does the socket still answer? A tiny REQ that must end (EOSE or CLOSED) within CHECK_MS;
   *  no answer: dropped (and reconnected). True when it answered. */
  check(): Promise<boolean> {
    const r = this.relay
    if (!r?.connected) return Promise.resolve(false)
    return (this.checking ??= new Promise<boolean>((resolve) => {
      let over = false
      const end = (ok: boolean) => {
        if (over) return
        over = true
        clearTimeout(t)
        try { s.close() } catch { /* closed */ }
        if (!ok && this.relay === r) this.drop('no answer to a check')
        resolve(ok)
      }
      const t = setTimeout(() => end(false), CHECK_MS)
      const s = r.subscribe([{ kinds: [39000], limit: 1 }], { oneose: () => end(true), onevent: () => {}, onclose: () => end(true), eoseTimeout: CHECK_MS + 1000 })
    }).finally(() => { this.checking = undefined }))
  }

  /** Before a send: the socket, checked first when it has been quiet for over BEFORE_SEND_MS. */
  async fresh(): Promise<Relay> {
    if (this.relay?.connected && this.quietFor() > BEFORE_SEND_MS) await this.check()
    return this.get()
  }

  stop() {
    this.wanted = false
    clearTimeout(this.timer)
    const r = this.relay
    this.relay = undefined
    if (r) { r.onclose = null; try { r.close() } catch { /* closed */ } }
  }
}

const socks = new Map<string, Sock>()

/** The one socket for URL (shared by everyone who uses that URL). */
export function sock(url: string): Sock {
  const k = norm(url)
  let s = socks.get(k)
  if (!s) { s = new Sock(url); socks.set(k, s) }
  return s
}

export const allSocks = () => [...socks.values()]

/** PROMISE, or "timeout: WHAT" after MS. */
export function within<T>(promise: Promise<T>, ms: number, what: string): Promise<T> {
  let t: ReturnType<typeof setTimeout>
  promise.catch(() => {}) // after a timeout it may still fail: nobody waits for it any more
  return Promise.race([promise, new Promise<T>((_, rej) => { t = setTimeout(() => rej(new Error(`timeout: ${what}`)), ms) })])
    .finally(() => clearTimeout(t))
}

// ---- foreground, online, keep-alive ---------------------------------------------------

const wakeListeners = new Set<() => void>()
/** Called when the app comes back (foreground, online), after the sockets were checked. */
export function onWake(f: () => void) { wakeListeners.add(f) }

/** Check every open socket at once (dead ones are dropped and reconnect); reopen wanted ones. */
export async function checkAll() {
  await Promise.all(allSocks().map((s) => (s.connected ? s.check() : s.wanted && navigator.onLine ? s.get().then(() => true, () => false) : Promise.resolve(false))))
}

let watching = false
export function watchConnections() {
  if (watching || typeof window === 'undefined') return
  watching = true
  const wake = () => { void checkAll(); wakeListeners.forEach((f) => f()) }
  window.addEventListener('online', wake)
  window.addEventListener('offline', () => {
    // the sockets are dead even if they don't know it yet: drop them, reconnect when back
    for (const s of allSocks()) { const w = s.wanted; s.wanted = false; s.drop(); s.wanted = w }
  })
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') wake() })
  window.addEventListener('pageshow', wake)
  // while visible: a socket quiet for 25 s is asked whether it still answers
  setInterval(() => {
    if (document.visibilityState !== 'visible' || !navigator.onLine) return
    for (const s of allSocks()) if (s.connected && s.quietFor() > KEEPALIVE_MS) void s.check()
  }, 5_000)
}

// ---- the send trace (About Belfry › Diagnostics) ----------------------------------------

/** One send, timed step by step: no content, no keys. */
export interface Trace { at: number; what: string; steps: [string, number][]; ms: number; result: string; tries: number }
const TRACE_KEY = 'belfry-send-trace'
let traces: Trace[] | null = null
export function sendTraces(): Trace[] {
  if (!traces) { try { traces = JSON.parse(localStorage.getItem(TRACE_KEY) || '[]') as Trace[] } catch { traces = [] } }
  return traces
}
export function addTrace(t: Trace) {
  traces = [t, ...sendTraces()].slice(0, 20)
  try { localStorage.setItem(TRACE_KEY, JSON.stringify(traces)) } catch { /* private mode */ }
}
