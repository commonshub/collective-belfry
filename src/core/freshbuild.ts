// A new build shipped while this page stays open (a phone's Safari tab can live for days): the page
// checks the server's index on coming back to it and every 15 min, and when it names another entry
// script, reloads on the next return to the tab, never while something is typed or a sheet is
// open (Xavier's Android, 2026-11-22: his iPhone tab ran the build before the one that adds devices).

const ENTRY = /\/assets\/index-[A-Za-z0-9_-]+\.js/
const KEY = 'belfry:freshbuild'

function current(): string | null {
  for (const s of document.querySelectorAll<HTMLScriptElement>('script[type=module][src]')) {
    const m = ENTRY.exec(new URL(s.src, location.href).pathname)
    if (m) return m[0]
  }
  return null
}

let pending: string | null = null
let hiddenAt = 0
let checking = false

async function check() {
  if (checking || pending || !navigator.onLine) return
  const mine = current()
  if (!mine) return
  checking = true
  try {
    const r = await fetch(import.meta.env.BASE_URL, { cache: 'no-store', credentials: 'same-origin' })
    if (!r.ok || !(r.headers.get('content-type') || '').includes('text/html')) return
    const m = ENTRY.exec(await r.text())
    if (m && m[0] !== mine) pending = m[0]
  } catch { /* offline: next time */ } finally { checking = false }
}

function busy(): boolean {
  if (document.querySelector('dialog[open], .sheet')) return true
  return [...document.querySelectorAll<HTMLInputElement | HTMLTextAreaElement>('textarea, input:not([type]), input[type=text], input[type=search], input[type=email]')].some((e) => e.value.trim() !== '')
}

function maybeReload() {
  if (!pending || busy()) return
  try { if (sessionStorage.getItem(KEY) === pending) return; sessionStorage.setItem(KEY, pending) } catch { /* no storage: reload anyway, once per page */ }
  location.reload()
}

export function watchBuilds() {
  if (!import.meta.env.PROD || current() === null) return
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') { hiddenAt = Date.now(); return }
    // back after a while: a reload now loses nothing the person is looking at
    if (pending && Date.now() - hiddenAt > 30e3) maybeReload()
    else void check().then(() => { if (pending && Date.now() - hiddenAt > 30e3) maybeReload() })
  })
  setInterval(() => { void check() }, 15 * 60e3)
  setTimeout(() => { void check() }, 60e3)
}
