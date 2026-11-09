// The phone layout: the page, and the channel drawer underneath it on the left (drawer.ts).
// Opening slides the page to the right (250 ms, decelerating) and the drawer in from a little
// to the left; closing reverses it (200 ms, accelerating). The drawer follows a finger dragging it
// shut, and closes past a third of its width or on a quick flick.
import { useEffect, useRef, type ReactNode } from 'react'
import { ChannelList } from './Chat'
import { closeDrawer } from './drawer'

export function Phone({ children, open, current, section }: { children: ReactNode; open: boolean; current?: string; section?: string }) {
  const root = useRef<HTMLDivElement>(null)
  const drawerRef = useRef<HTMLElement>(null)
  const mainRef = useRef<HTMLDivElement>(null)
  // only one side takes taps and focus (React 18 has no inert prop)
  useEffect(() => {
    if (drawerRef.current) drawerRef.current.inert = !open
    if (mainRef.current) mainRef.current.inert = open
  }, [open])
  // focus: into the drawer when it opens, back to ☰ when it closes
  const was = useRef(open)
  useEffect(() => {
    if (open && !was.current) drawerRef.current?.querySelector<HTMLElement>('.drawer-close')?.focus({ preventScroll: true })
    if (!open && was.current) root.current?.querySelector<HTMLElement>('.phone-main .menu-btn')?.focus({ preventScroll: true })
    was.current = open
  }, [open])
  // drag to close
  useEffect(() => {
    const el = root.current
    if (!el || !open) return
    let x0 = 0, y0 = 0, t0 = 0, dx = 0, dragging = false, decided = false
    const width = () => drawerRef.current?.offsetWidth ?? 300
    const setX = (v: number) => el.style.setProperty('--drag', `${v}px`)
    const start = (e: TouchEvent) => { const t = e.touches[0]; x0 = t.clientX; y0 = t.clientY; t0 = e.timeStamp; dx = 0; dragging = false; decided = false }
    const move = (e: TouchEvent) => {
      const t = e.touches[0]
      const mx = t.clientX - x0, my = t.clientY - y0
      if (!decided) {
        if (Math.abs(mx) < 8 && Math.abs(my) < 8) return
        decided = true
        dragging = mx < 0 && Math.abs(mx) > Math.abs(my) // a horizontal drag to the left; else it's a scroll
        if (dragging) el.classList.add('dragging')
      }
      if (!dragging) return
      e.preventDefault()
      dx = Math.min(0, Math.max(-width(), mx))
      setX(dx)
    }
    const end = (e: TouchEvent) => {
      if (!dragging) return
      el.classList.remove('dragging')
      el.style.removeProperty('--drag')
      const v = dx / Math.max(1, e.timeStamp - t0) // px per ms
      if (dx < -width() / 3 || v < -0.5) closeDrawer()
    }
    el.addEventListener('touchstart', start, { passive: true })
    el.addEventListener('touchmove', move, { passive: false })
    el.addEventListener('touchend', end)
    el.addEventListener('touchcancel', end)
    return () => {
      el.removeEventListener('touchstart', start)
      el.removeEventListener('touchmove', move)
      el.removeEventListener('touchend', end)
      el.removeEventListener('touchcancel', end)
    }
  }, [open])
  return (
    <div ref={root} className={'phone' + (open ? ' drawer-open' : '')}>
      <aside ref={drawerRef} className="drawer" aria-label="Channels" aria-hidden={!open}>
        <ChannelList side drawer current={current} section={section} />
      </aside>
      <div ref={mainRef} className="phone-main">
        {children}
      </div>
      <div className="drawer-shade" onClick={() => closeDrawer()} aria-hidden="true" />
    </div>
  )
}
