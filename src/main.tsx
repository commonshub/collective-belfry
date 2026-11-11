import { createRoot } from 'react-dom/client'
import { App } from './ui/App'
import './styles.css'
import { NAME, THEME } from './config'
import { applyBranding } from './core/branding'

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

createRoot(document.getElementById('root')!).render(<App />)

if ('serviceWorker' in navigator && import.meta.env.PROD) {
  navigator.serviceWorker.register('./sw.js').catch(() => { /* offline shell is a bonus */ })
}
