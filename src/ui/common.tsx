import { Drawer } from 'vaul'
import { useEffect, useId, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
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
export const back = (fallback: string) => (history.length > 1 ? history.back() : go(fallback))

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

export function MediaItem({ m }: { m: Media }) {
  const src = useMediaUrl(m.url)
  if (!src) return <div className="file">…</div>
  if (m.mime.startsWith('image/'))
    return (
      <a href={src} target="_blank" rel="noreferrer">
        <img src={src} alt={m.name ?? ''} loading="lazy" style={m.w && m.h ? { aspectRatio: `${m.w}/${m.h}` } : undefined} />
      </a>
    )
  if (m.mime.startsWith('video/')) return <video src={src} controls playsInline preload="metadata" />
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
  return (
    <>
      {visual.length > 0 && (
        <div className={'media' + (visual.length === 1 ? ' one' : '')}>
          {visual.map((m) => <MediaItem key={m.url} m={m} />)}
        </div>
      )}
      {files.map((m) => <div key={m.url} style={{ marginTop: 6 }}><MediaItem m={m} /></div>)}
    </>
  )
}

// ---- sheet ---------------------------------------------------------------------

/** A bottom drawer (Vaul, what shadcn/ui's Drawer is built on): slide it down, tap outside or
 *  press Escape to close it. */
export function Sheet({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  const wide = useWide()
  if (wide) return <Modal title={title} onClose={onClose}>{children}</Modal>
  return (
    <Drawer.Root open onOpenChange={(open) => { if (!open) onClose() }}>
      <Drawer.Portal>
        <Drawer.Overlay className="sheet-bg" />
        <Drawer.Content className="sheet" aria-describedby={undefined}>
          <Drawer.Handle className="grab" />
          <Drawer.Title asChild><h3>{title}</h3></Drawer.Title>
          {children}
        </Drawer.Content>
      </Drawer.Portal>
    </Drawer.Root>
  )
}

/** On a laptop the same content opens as a dialog in the middle (native <dialog>: focus kept
 *  inside, Escape closes, the page behind is inert); a click on the backdrop closes it too. */
function Modal({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
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
        <h3 id={titleId}>{title}</h3>
        <button type="button" className="modal-close" aria-label="Close" onClick={() => closeRef.current()}><CloseIcon /></button>
      </div>
      {children}
    </dialog>,
    document.body,
  )
}

/** True on a laptop-wide window: the channel list stays beside the conversation, sheets are dialogs. */
export function useWide() {
  const q = '(min-width: 900px)'
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
