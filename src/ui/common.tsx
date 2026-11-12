import { Drawer } from 'vaul'
import { Component, useEffect, useId, useRef, useState, type ButtonHTMLAttributes, type FormEvent, type FormHTMLAttributes, type MouseEvent, type ReactNode } from 'react'
import { createPortal } from 'react-dom'

/** Busy on it now: a ring that fills clockwise over the hour (one minute = 6°), "1h", "2h"… in it
 *  once past an hour. SINCE and NOW in unix seconds. */
export function WorkRing({ since, now, size = 24 }: { since: number; now: number; size?: number }) {
  const min = Math.max(0, (now - since) / 60)
  const r = size / 2 - 2, c = 2 * Math.PI * r
  const frac = min >= 60 ? 1 : Math.max(0.04, min / 60)
  const label = min >= 60 ? `${Math.floor(min / 60)}h` : ''
  return (
    <svg className="work-ring" width={size} height={size} viewBox={`0 0 ${size} ${size}`} role="img" aria-label={`Working on it for ${min < 60 ? Math.round(min) + ' min' : label}`}>
      <circle cx={size / 2} cy={size / 2} r={r} className="track" />
      <circle cx={size / 2} cy={size / 2} r={r} className="arc" strokeDasharray={`${frac * c} ${c}`} transform={`rotate(-90 ${size / 2} ${size / 2})`} />
      {label && <text x="50%" y="50%" dominantBaseline="central" textAnchor="middle" style={{ fontSize: size * 0.38 }}>{label}</text>}
    </svg>
  )
}

/** The time now (unix seconds), refreshed every MS: for states that expire, like a to-do heartbeat. */
export function useNow(ms = 30_000) {
  const [n, setN] = useState(() => Date.now() / 1000)
  useEffect(() => { const i = setInterval(() => setN(Date.now() / 1000), ms); return () => clearInterval(i) }, [ms])
  return n
}
import { CloseIcon } from './icons'
import { db } from '../core/db'
import { displayName, profiles, type Media } from '../core/model'
import { outboxItems, status, useVersion } from '../core/store'
import { isLocalOnly } from '../core/sync'
import { BELFRY } from '../config'

// ---- hash router: #/c/general, #/c/general/info, #/cal, #/cal/<d>, #/me, #/channels

export function useRoute(): string[] {
  const [h, setH] = useState(location.hash)
  useEffect(() => {
    const on = () => setH(location.hash)
    addEventListener('hashchange', on)
    return () => removeEventListener('hashchange', on)
  }, [])
  return h.replace(/^#\/?/, '').split('/').filter(Boolean).map(decodeURIComponent)
}

export const go = (path: string) => { location.hash = '#/' + path }

// Where we are in this tab's history, and which screen each entry of our own holds. "Back"
// buttons use it: a screen's ‹ goes one step back when that step is the screen it came from,
// and otherwise replaces itself with its parent. Never a push: pushing a parent and then going
// "back" from it would bounce between the two screens for ever.
let cur = typeof history.state?.i === 'number' ? (history.state.i as number) : 0
const trail: (string | undefined)[] = []
let replacing = false
const mark = () => {
  const i = history.state?.i
  if (typeof i === 'number') cur = i // back or forward to an entry we numbered
  else {
    if (!replacing) cur += 1 // a new entry right after the one we were on
    trail.length = cur // what was ahead is gone
    try { history.replaceState({ ...(history.state ?? {}), i: cur }, '') } catch { /* sandboxed */ }
  }
  replacing = false
  trail[cur] = location.hash
}
if (typeof addEventListener === 'function') { mark(); addEventListener('hashchange', mark) }
const first = (h: string | undefined) => (h ?? '').replace(/^#\//, '').split('/')[0]
const replaceWith = (path: string) => { replacing = true; location.replace('#/' + path); }

/** ‹ on a screen with no fixed parent (a profile, Me, an event): back to where you were, or
 *  FALLBACK when this is where the tab started. */
export const back = (fallback: string) => (cur > 0 && trail[cur - 1] !== undefined ? history.back() : replaceWith(fallback))

/** ‹ on a screen under PARENT (a conversation under Messages, a thread under its channel): one
 *  step back when that is the parent, else the parent takes this screen's place. A one-word
 *  parent ("c", "dm") matches any screen of that section. */
export function up(parent: string) {
  const prev = trail[cur - 1]
  const same = prev !== undefined && cur > 0 && (parent.includes('/') ? prev === '#/' + parent : first(prev) === parent && (parent !== 'dm' || prev === '#/dm'))
  if (same) history.back(); else replaceWith(parent)
}

// ---- avatars -----------------------------------------------------------------

const COLORS = ['#d6ff52', '#f89b0e', '#7ee0a1', '#5ec8e5', '#c792ea', '#ff9a8a', '#f2d06b']

export function Avatar({ pubkey, size = 36 }: { pubkey: string; size?: number }) {
  const p = profiles().get(pubkey)
  const name = displayName(pubkey)
  const color = COLORS[parseInt(pubkey.slice(0, 6) || '0', 16) % COLORS.length]
  return (
    <div className="avatar" style={{ background: color, width: size, height: size, fontSize: size * 0.42 }}>
      {p?.picture ? <img src={p.picture} alt="" loading="lazy" /> : (name[0] ?? '?').toUpperCase()}
    </div>
  )
}

// ---- media: idb: URLs are files that only exist on this device (demo / not yet uploaded)

const objectUrls = new Map<string, string>()

export function useMediaUrl(url: string) {
  const [src, setSrc] = useState(() => (url.startsWith('idb:') ? objectUrls.get(url) : url))
  useEffect(() => {
    if (!url.startsWith('idb:') || objectUrls.has(url)) return
    let live = true
    db().then((d) => d.get('blobs', url.slice(4))).then((b) => {
      if (!b || !live) return
      const u = URL.createObjectURL(b)
      objectUrls.set(url, u)
      setSrc(u)
    })
    return () => { live = false }
  }, [url])
  return src
}

export function MediaItem({ m, onOpen }: { m: Media; /** images: open them in the lightbox instead of a new tab */ onOpen?: () => void }) {
  const src = useMediaUrl(m.url)
  if (!src) return <div className="file">…</div>
  if (m.mime.startsWith('image/'))
    return (
      <a href={src} target="_blank" rel="noreferrer" onClick={(e) => { if (!onOpen || e.metaKey || e.ctrlKey) return; e.preventDefault(); e.stopPropagation(); onOpen() }}>
        <img src={src} alt={m.name ?? ''} loading="lazy" style={m.w && m.h ? { aspectRatio: `${m.w}/${m.h}` } : undefined} />
      </a>
    )
  if (m.mime.startsWith('video/')) return <video src={src} controls playsInline preload="metadata" />
  if (m.mime.startsWith('audio/')) return <audio className="audio" src={src} controls preload="metadata" />
  return (
    <a className="file" href={src} target="_blank" rel="noreferrer" download={m.name}>
      📎 <span>{m.name || m.url.split('/').pop()}</span>
    </a>
  )
}

export function MediaGrid({ media }: { media: Media[] }) {
  if (!media.length) return null
  const visual = media.filter((m) => /^(image|video)\//.test(m.mime))
  const files = media.filter((m) => !/^(image|video)\//.test(m.mime))
  const images = visual.filter((m) => m.mime.startsWith('image/'))
  const [open, setOpen] = useState<number | null>(null)
  return (
    <>
      {visual.length > 0 && (
        <div className={'media' + (visual.length === 1 ? ' one' : '')}>
          {visual.map((m) => <MediaItem key={m.url} m={m} onOpen={m.mime.startsWith('image/') ? () => setOpen(images.indexOf(m)) : undefined} />)}
        </div>
      )}
      {open !== null && <Lightbox images={images} start={open} onClose={() => setOpen(null)} />}
      {files.map((m) => <div key={m.url} style={{ marginTop: 6 }}><MediaItem m={m} /></div>)}
    </>
  )
}

// ---- lightbox -------------------------------------------------------------------

/** A message's images, as big as the screen allows. Swipe sideways for the others: the pictures sit
 *  side by side on one strip that follows the finger and glides to the next one on release (a short
 *  flick is enough), with the neighbours already loaded (Xavier, 2026-11-12: it wasn't smooth).
 *  Swipe down, Esc, ✕ or a tap beside the picture closes it; ←/→ and the side buttons on a laptop.
 *  No history entry (‹ never pushes). */
export function Lightbox({ images, start, onClose }: { images: Media[]; start: number; onClose: () => void }) {
  const n = images.length
  const [i, setI] = useState(Math.min(Math.max(start, 0), n - 1))
  const [drag, setDrag] = useState<{ dx: number; dy: number } | null>(null)
  const [glide, setGlide] = useState(false)
  const at = useRef<{ x: number; y: number; t: number; axis: '' | 'x' | 'y'; moved: boolean } | null>(null)
  const box = useRef<HTMLDivElement>(null)
  const swiped = useRef(false) // the click after a swipe is not a tap
  const go = (d: number) => { setGlide(true); setI((x) => Math.min(n - 1, Math.max(0, x + d))) }
  useEffect(() => {
    const k = (e: KeyboardEvent) => {
      if (e.key === 'Escape') { e.stopPropagation(); onClose() }
      else if (e.key === 'ArrowRight') go(1)
      else if (e.key === 'ArrowLeft') go(-1)
    }
    addEventListener('keydown', k, true)
    const was = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => { removeEventListener('keydown', k, true); document.body.style.overflow = was }
  }, [n]) // eslint-disable-line react-hooks/exhaustive-deps
  const down = (e: React.PointerEvent) => {
    if (e.pointerType === 'mouse' && e.button !== 0) return
    at.current = { x: e.clientX, y: e.clientY, t: performance.now(), axis: '', moved: false }
  }
  const move = (e: React.PointerEvent) => {
    const s = at.current
    if (!s) return
    let dx = e.clientX - s.x
    const dy = e.clientY - s.y
    if (!s.axis) {
      if (Math.hypot(dx, dy) < 6) return
      s.axis = Math.abs(dx) >= Math.abs(dy) ? 'x' : 'y'
      s.moved = true
      ;(e.currentTarget as HTMLElement).setPointerCapture?.(e.pointerId)
      setGlide(false)
    }
    if (s.axis === 'x') {
      if ((i === 0 && dx > 0) || (i === n - 1 && dx < 0)) dx /= 3 // the ends give a little, then stop
      setDrag({ dx, dy: 0 })
    } else setDrag({ dx: 0, dy: Math.max(0, dy) })
  }
  const up = (e: React.PointerEvent) => {
    const s = at.current
    at.current = null
    if (!s?.moved) return
    swiped.current = true
    setTimeout(() => { swiped.current = false }, 0)
    const dx = e.clientX - s.x, dy = e.clientY - s.y
    const speed = Math.abs(s.axis === 'x' ? dx : dy) / Math.max(1, performance.now() - s.t) // px per ms
    const w = box.current?.clientWidth ?? innerWidth
    setGlide(true)
    setDrag(null)
    if (s.axis === 'x' && (Math.abs(dx) > w * 0.2 || (speed > 0.35 && Math.abs(dx) > 20))) go(dx < 0 ? 1 : -1)
    else if (s.axis === 'y' && (dy > 140 || (speed > 0.5 && dy > 40))) onClose()
  }
  const cancel = () => { at.current = null; setGlide(true); setDrag(null) }
  const dx = drag?.dx ?? 0, dy = drag?.dy ?? 0
  return createPortal(
    <div ref={box} className="lightbox" role="dialog" aria-modal="true" aria-label={n > 1 ? `Image ${i + 1} of ${n}` : 'Image'}
      style={{ background: `rgba(0, 0, 0, ${0.92 * (1 - Math.min(dy / 400, 0.6))})` }}
      onClickCapture={(e) => { if (swiped.current) { e.stopPropagation(); e.preventDefault() } }}
      onClick={(e) => { const c = (e.target as HTMLElement).classList; if (e.target === e.currentTarget || c.contains('lb-slide') || c.contains('lb-track')) onClose() }}
      onPointerDown={down} onPointerMove={move} onPointerUp={up} onPointerCancel={cancel}>
      <div className={'lb-track' + (glide ? ' glide' : '')} onTransitionEnd={() => setGlide(false)}
        style={{ transform: `translate3d(calc(${-i * 100}% + ${dx}px), ${dy}px, 0)${dy ? ` scale(${1 - Math.min(dy / 1600, 0.15)})` : ''}` }}>
        {images.map((m, k) => (
          <div key={m.url + k} className="lb-slide" aria-hidden={k !== i}>{Math.abs(k - i) <= 1 ? <LightboxImage m={m} /> : null}</div>
        ))}
      </div>
      <button className="lb-close icon-btn" aria-label="Close" onClick={onClose}><CloseIcon /></button>
      {n > 1 && <>
        {i > 0 && <button className="lb-nav prev" aria-label="Previous image" onClick={() => go(-1)}>‹</button>}
        {i < n - 1 && <button className="lb-nav next" aria-label="Next image" onClick={() => go(1)}>›</button>}
        <div className="lb-count">{i + 1} / {n}</div>
      </>}
    </div>,
    document.body,
  )
}

function LightboxImage({ m }: { m: Media }) {
  const src = useMediaUrl(m.url)
  if (!src) return <div className="lb-loading">…</div>
  return <img src={src} alt={m.name ?? ''} draggable={false} />
}

// ---- sheet ---------------------------------------------------------------------

/** A bottom drawer (Vaul, what shadcn/ui's Drawer is built on): slide it down, tap outside or
 *  press Escape to close it. */
/** A form's fields kept on this device while it is being written (Xavier, 2026-11-11: like a
 *  message's draft): closing the sheet and opening it again brings them back; `clear` after
 *  sending. KEY null: no draft (editing something that exists). Drafts older than 30 days go. */
export function useFormDraft<T extends object>(key: string | null, initial: () => T): [T, (f: (x: T) => T) => void, () => void] {
  const store = key ? 'osv-form:' + key : ''
  const [v, setV] = useState<T>(() => {
    if (store) try {
      const d = JSON.parse(localStorage.getItem(store) || 'null') as { at: number; v: T } | null
      if (d && Date.now() - d.at < 30 * 86400e3) return { ...initial(), ...d.v }
    } catch { /* none */ }
    return initial()
  })
  const update = (f: (x: T) => T) => setV((x) => {
    const n = f(x)
    if (store) try { localStorage.setItem(store, JSON.stringify({ at: Date.now(), v: n })) } catch { /* full or private */ }
    return n
  })
  const clear = () => { if (store) try { localStorage.removeItem(store) } catch { /* private */ } }
  return [v, update, clear]
}

export function Sheet({ title, onClose, children, hideTitle = false }: { title: string; onClose: () => void; children: ReactNode; hideTitle?: boolean }) {
  const wide = useWide()
  if (wide) return <Modal title={title} hideTitle={hideTitle} onClose={onClose}>{children}</Modal>
  return (
    <Drawer.Root open onOpenChange={(open) => { if (!open) onClose() }}>
      <Drawer.Portal>
        <Drawer.Overlay className="sheet-bg" />
        <Drawer.Content className="sheet" aria-describedby={undefined}>
          <Drawer.Handle className="grab" />
          {/* the drawer itself takes drags (to close it): a long form scrolls in its own box */}
          <div className="sheet-scroll">
            <Drawer.Title asChild><h3 className={hideTitle ? 'sr-only' : undefined}>{title}</h3></Drawer.Title>
            {children}
          </div>
        </Drawer.Content>
      </Drawer.Portal>
    </Drawer.Root>
  )
}

/** A sheet's second screen: ‹ and its title on one line (‹ is this sheet's own step, no history). */
export function BackHead({ title, onBack }: { title: string; onBack: () => void }) {
  return <div className="sheet-back-head"><button type="button" className="icon-btn" aria-label="Back" onClick={onBack}>‹</button><h3 aria-hidden="true">{title}</h3></div>
}

/** On a laptop the same content opens as a dialog in the middle (native <dialog>: focus kept
 *  inside, Escape closes, the page behind is inert); a click on the backdrop closes it too. */
function Modal({ title, onClose, children, hideTitle = false }: { title: string; onClose: () => void; children: ReactNode; hideTitle?: boolean }) {
  const ref = useRef<HTMLDialogElement>(null)
  const closeRef = useRef(onClose)
  closeRef.current = onClose
  useEffect(() => {
    const d = ref.current
    if (!d) return
    if (!d.open) d.showModal()
    const onCancel = (e: Event) => { e.preventDefault(); closeRef.current() }
    d.addEventListener('cancel', onCancel)
    return () => { d.removeEventListener('cancel', onCancel); if (d.open) d.close() }
  }, [])
  const titleId = useId()
  return createPortal(
    <dialog ref={ref} className="modal" aria-labelledby={titleId}
      onMouseDown={(e) => { if (e.target === ref.current) closeRef.current() }}>
      <div className="modal-head">
        <h3 id={titleId} className={hideTitle ? 'sr-only' : undefined}>{title}</h3>
        <button type="button" className="modal-close" aria-label="Close" onClick={() => closeRef.current()}><CloseIcon /></button>
      </div>
      {children}
    </dialog>,
    document.body,
  )
}

/** True on a laptop-wide window: the channel list stays beside the conversation, sheets are dialogs. */
/** ≥ 900 px: channels stay on the left. useWide(WIDE3): ≥ 1200 px, an open thread gets a third column. */
export const WIDE3 = '(min-width: 1200px)'
export function useWide(q = '(min-width: 900px)') {
  const [wide, setWide] = useState(() => typeof matchMedia === 'function' && matchMedia(q).matches)
  useEffect(() => {
    const m = matchMedia(q)
    const on = () => setWide(m.matches)
    m.addEventListener('change', on)
    return () => m.removeEventListener('change', on)
  }, [])
  return wide
}

// ---- sync status -----------------------------------------------------------------

/** The sync state as a dot (on your avatar, top right): green synced, orange sending, grey offline / demo. */
export function syncState(): { cls: 'ok' | 'busy' | 'off'; label: string } {
  const queued = outboxItems().length
  if (isLocalOnly()) return { cls: 'off', label: 'Demo: kept on this device' }
  const up = status.relays[BELFRY.groupsRelay] === 'up'
  if (!status.online || !up) return { cls: 'off', label: `Offline${queued ? `, ${queued} to send` : ''}` }
  if (queued) return { cls: 'busy', label: `Sending ${queued}` }
  return { cls: 'ok', label: 'Synced' }
}

export function SyncDot() {
  useVersion()
  const s = syncState()
  return <span className={'sync-dot ' + s.cls} title={s.label} aria-label={s.label} role="status" />
}

export function fmtTime(ts: number) {
  return new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

export function fmtDay(d: Date) {
  const today = new Date()
  const y = new Date(today)
  y.setDate(today.getDate() - 1)
  if (d.toDateString() === today.toDateString()) return 'Today'
  if (d.toDateString() === y.toDateString()) return 'Yesterday'
  return d.toLocaleDateString([], d.getFullYear() === today.getFullYear()
    ? { weekday: 'long', day: 'numeric', month: 'long' }
    : { day: 'numeric', month: 'long', year: 'numeric' })
}


// ---- buttons that lock while their work runs -------------------------------------------------
// A tap is answered at once (the button dims and shows a spinner) and can't be sent twice: Form
// and Btn stay busy until the promise their handler returns settles. main.tsx also swallows a
// second tap on the same button within 600 ms, for the handlers that return nothing.

export function useBusy(): [boolean, (fn: () => unknown) => Promise<void>] {
  const [busy, setBusy] = useState(false)
  const lock = useRef(false)
  const alive = useRef(true)
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])
  const run = async (fn: () => unknown) => {
    if (lock.current) return
    lock.current = true
    let done = false
    // only show it when the work isn't instant (no flicker on local actions)
    const t = setTimeout(() => { if (!done && alive.current) setBusy(true) }, 0)
    try { await fn() } finally {
      done = true; clearTimeout(t); lock.current = false
      if (alive.current) setBusy(false)
    }
  }
  return [busy, run]
}

type FormProps = Omit<FormHTMLAttributes<HTMLFormElement>, 'onSubmit'> & { onSubmit?: (e: FormEvent<HTMLFormElement>) => unknown }
export function Form({ onSubmit, children, ...rest }: FormProps) {
  const [busy, run] = useBusy()
  return (
    <form {...rest} aria-busy={busy || undefined} {...(busy ? { inert: '' } : {})}
      onSubmit={(e) => { e.preventDefault(); void run(() => onSubmit?.(e)) }}>
      {children}
    </form>
  )
}

type BtnProps = Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'onClick'> & { onClick?: (e: MouseEvent<HTMLButtonElement>) => unknown }
export function Btn({ onClick, disabled, children, ...rest }: BtnProps) {
  const [busy, run] = useBusy()
  return <button type="button" {...rest} disabled={disabled || busy} aria-busy={busy || undefined} onClick={(e) => void run(() => onClick?.(e))}>{children}</button>
}

// ---- errors: never a white screen ----------------------------------------------------------

const RELOADED = 'osv-reloaded-at'
/** Load the page again for a newer version, at most once a minute (no loops). */
export function reloadOnce(): boolean {
  try {
    const at = Number(sessionStorage.getItem(RELOADED) || 0)
    if (Date.now() - at < 60_000) return false
    sessionStorage.setItem(RELOADED, String(Date.now()))
  } catch { /* private mode: reload anyway */ }
  location.reload()
  return true
}
const isChunkError = (e: unknown) => /dynamically imported module|Importing a module script failed|module script|Failed to fetch dynamically|error loading dynamically|ChunkLoadError|MIME type/i.test(String((e as Error)?.message ?? e))

/** Catches what a part of the screen throws: a code chunk from an older version reloads the page
 *  once; anything else shows a line (the whole app: with a Reload button) instead of blanking. */
export class Guard extends Component<{ children: ReactNode; whole?: boolean; fallback?: ReactNode }, { error: unknown }> {
  state = { error: null as unknown }
  static getDerivedStateFromError(error: unknown) { return { error } }
  componentDidCatch(error: unknown) {
    console.error('caught:', error)
    if (isChunkError(error)) reloadOnce()
  }
  render() {
    if (!this.state.error) return this.props.children
    if (!this.props.whole) return this.props.fallback ?? <p className="hint pad-x">This part could not be shown. <button className="link" onClick={() => location.reload()}>Reload</button></p>
    return (
      <div className="screen"><div className="empty" role="alert">
        <p><b>Something went wrong.</b></p>
        <p className="hint">{isChunkError(this.state.error) ? 'A new version is out.' : String((this.state.error as Error)?.message ?? this.state.error).slice(0, 200)}</p>
        <button className="btn" onClick={() => location.reload()}>Reload</button>
      </div></div>
    )
  }
}

// ---- dev mode: admins see the raw Nostr events (Me → Developer mode), on this device only ----
const DEV = 'osv-dev-mode'
export const devMode = () => { try { return localStorage.getItem(DEV) === '1' } catch { return false } }
export const setDevMode = (on: boolean) => { try { if (on) localStorage.setItem(DEV, '1'); else localStorage.removeItem(DEV) } catch { /* private mode */ } }
