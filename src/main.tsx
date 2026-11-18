import { createRoot } from 'react-dom/client'
import { App } from './ui/App'
import './styles.css'
import { NAME, THEME } from './config'
import { applyBranding } from './core/branding'
import { Guard, reloadOnce } from './ui/common'

if (THEME === 'belfry') {
  document.documentElement.dataset.theme = 'belfry'
  document.title = `Chat · ${NAME}`
  document.querySelector('meta[name=theme-color]')?.setAttribute('content', '#ffffff')
  applyBranding() // the admins' colours and logo, from the last copy on this device
}

// iPhones zoom the page in when a text field under 16px gets the focus, and the page then
// scrolls sideways. Our fields are 16px; this covers any that slip through. iOS keeps pinch
// zoom regardless of maximum-scale; other systems are left alone.
if (/iP(hone|ad|od)/.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1)) {
  const v = document.querySelector('meta[name=viewport]')
  if (v && !/maximum-scale/.test(v.getAttribute('content') ?? '')) v.setAttribute('content', v.getAttribute('content') + ', maximum-scale=1')
}

// The on-screen keyboard. iOS Safari doesn't shrink the page for it (interactive-widget is
// ignored there): it pans the page under a smaller visual viewport, so a composer at the bottom
// ends up behind the keyboard or the page slides off the top (Xavier, 2026-11-16, an e-mail
// reply). While the keyboard is up, the app is pinned to exactly the visible part (styles.css,
// html.kb #root) and the page is kept from panning. Browsers that resize the page themselves
// (Android Chrome with resizes-content) never see a gap, so nothing changes there.
const vv = window.visualViewport
if (vv) {
  const root = document.documentElement
  let frame = 0
  const fit = () => {
    frame = 0
    const gap = window.innerHeight - vv.height
    const up = gap > 120 && !!document.activeElement?.matches?.('textarea, input, [contenteditable]')
    root.classList.toggle('kb', up)
    if (up) {
      root.style.setProperty('--app-h', `${Math.round(vv.height)}px`)
      root.style.setProperty('--app-top', `${Math.round(vv.offsetTop)}px`)
      // sheets and dialogs are fixed to the layout viewport: their bottom goes up to the keyboard's
      // top (styles.css, html.kb .sheet), so a field in them stays on screen (Xavier, 2026-11-18)
      root.style.setProperty('--kb-bottom', `${Math.max(0, Math.round(window.innerHeight - vv.height - vv.offsetTop))}px`)
      keepFocusedInSheet()
    } else {
      root.style.removeProperty('--app-h')
      root.style.removeProperty('--app-top')
      root.style.removeProperty('--kb-bottom')
    }
  }
  // a field inside a sheet: once the keyboard has settled, scrolled into the sheet's own view
  // (its scroll box), never by panning the page
  let settle = 0
  const keepFocusedInSheet = () => {
    clearTimeout(settle)
    settle = window.setTimeout(() => {
      const el = document.activeElement as HTMLElement | null
      if (el?.closest?.('.sheet, dialog.modal') && el.matches('textarea, input, [contenteditable]')) el.scrollIntoView({ block: 'nearest' })
      if (window.scrollY) window.scrollTo(0, 0)
    }, 120)
  }
  const later = () => { if (!frame) frame = requestAnimationFrame(fit) }
  vv.addEventListener('resize', later)
  vv.addEventListener('scroll', later)
  document.addEventListener('focusin', later)
  document.addEventListener('focusout', () => setTimeout(later, 50))
}

// A second tap on the same button within 600 ms never reaches the app (double sends); the first
// one is answered at once: the button shows it was pressed (styles.css, [data-tapped]).
const tapped = new WeakMap<Element, number>()
document.addEventListener('click', (e) => {
  const b = (e.target as Element | null)?.closest?.('button.btn, button.act, button.send, button[type=submit], form button:not([type=button])')
  if (!b) return
  const now = performance.now()
  if (now - (tapped.get(b) ?? -1e9) < 600) { e.stopPropagation(); e.preventDefault(); return }
  tapped.set(b, now)
  b.setAttribute('data-tapped', '')
  setTimeout(() => b.removeAttribute('data-tapped'), 600)
}, true)

// A new version shipped while this page was open: its old code chunks are gone from the server
// (a lazy part, the members map, then failed to load and blanked the app: Xavier, 2026-11-12).
// Load the new version instead, once.
addEventListener('vite:preloadError', (e) => { e.preventDefault(); reloadOnce() })

createRoot(document.getElementById('root')!).render(<Guard whole><App /></Guard>)

if ('serviceWorker' in navigator && import.meta.env.PROD) {
  navigator.serviceWorker.register('./sw.js').catch(() => { /* offline shell is a bonus */ })
}
