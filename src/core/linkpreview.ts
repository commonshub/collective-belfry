// Previews of outside pages (Channels canvas v10, channels/og-*; approved by Xavier, 23 Nov).
// The belfry fetches the page (GET /api/v1/link-preview?url=, NIP-98 with this session); the app
// never contacts the outside site, and shows a picture only when the belfry serves it (its proxy).
// A message tagged ["preview","none"] gets no preview (the sender's ✕ before sending: the lead's tag,
// the same on iOS and Android). Only the first outside link of a message gets one.
import { useEffect, useState } from 'react'
import { BELFRY, getBackend } from '../config'
import { target } from './refs'
import { isLocalOnly, signedFetch } from './sync'

export interface LinkPreview { url: string; title: string; description: string; site: string; host: string; image?: string; /** demo only: a coloured block stands for the picture */ tint?: boolean }

export const NO_PREVIEW: string[] = ['preview', 'none']
export const noPreview = (tags?: string[][]) => !!tags?.some((t) => t[0] === 'preview' && t[1] === 'none')

const URL_RE = /https:\/\/[^\s<>()]+[^\s<>().,;:!?'"]/g // https only (= refs.ts URL, without http)
const MEDIA = /\.(jpe?g|png|gif|webp|avif|heic|svg|mp4|mov|webm|m4a|mp3|ogg|wav|pdf)(\?|#|$)/i

/** The first link to an outside page in TEXT: https, not something in this belfry (its own cards),
 *  not a file (pictures and files show as themselves), not one of SKIP (event links with their cards). */
export function outsideLink(text: string, tags?: string[][], skip: string[] = []): string | undefined {
  if (!text || noPreview(tags)) return undefined
  const files = new Set((tags ?? []).filter((t) => t[0] === 'imeta').flatMap((t) => t.filter((x) => x.startsWith('url ')).map((x) => x.slice(4))))
  for (const m of text.matchAll(URL_RE)) {
    const u = m[0]
    if (files.has(u) || skip.includes(u) || MEDIA.test(u) || target(u)) continue
    let host: string
    try { host = new URL(u).hostname } catch { continue }
    if (host === BELFRY.domain || host.endsWith('.' + BELFRY.domain)) continue // the belfry's own pages: not "outside"
    return u
  }
  return undefined
}

// ---- fetching (once per URL per page; nothing found is remembered too) --------------------------
type Entry = LinkPreview | null | Promise<LinkPreview | null>
const cache = new Map<string, Entry>()
const MAX = 300
const belfryOrigin = (() => { try { return new URL(BELFRY.api).origin } catch { return '' } })()
const str = (v: unknown, n: number) => (typeof v === 'string' ? v.replace(/\s+/g, ' ').trim().slice(0, n) : '')

/** The belfry's answer, kept only when it has a title or a description; a picture only from the belfry. */
export function previewFrom(url: string, d: Record<string, unknown>): LinkPreview | null {
  const title = str(d.title, 200), description = str(d.description, 400)
  if (!title && !description) return null
  let host = ''
  try { host = new URL(url).hostname.replace(/^www\./, '') } catch { return null }
  let image: string | undefined
  if (typeof d.image === 'string') { try { const i = new URL(d.image); if (i.protocol === 'https:' && i.origin === belfryOrigin) image = i.href } catch { /* not a URL */ } }
  return { url, title, description, site: str(d.site_name, 80), host, image }
}

const DEMO: Record<string, Record<string, unknown>> = {
  'https://toolshare.example/stories/street-library': { title: 'How a street tool library lends 400 drills a year', description: 'Neighbours in a small street started lending their tools from a garage. Two years later they share drills, ladders and saws with half the town, and nobody keeps a list.', site_name: 'ToolShare Journal', image: 'yes' },
  'https://repair.example/guide': { title: 'The repair café guide', description: 'How to start one in your street.', site_name: 'Repair' },
}

async function load(url: string): Promise<LinkPreview | null> {
  if (getBackend() === 'demo' || isLocalOnly()) {
    await new Promise((r) => setTimeout(r, 400))
    const d = DEMO[url]
    if (!d) return null
    const p = previewFrom(url, { ...d, image: undefined })
    return p && d.image ? { ...p, tint: true } : p
  }
  try {
    const r = await signedFetch(`${BELFRY.api}/link-preview?url=${encodeURIComponent(url)}`)
    if (!r.ok) return null // 404: nothing found; anything else: just the link too
    return previewFrom(url, (await r.json().catch(() => ({}))) as Record<string, unknown>)
  } catch { return null }
}

export function getPreview(url: string): Entry {
  let e = cache.get(url)
  if (e === undefined) {
    if (cache.size > MAX) cache.delete(cache.keys().next().value!)
    const p = load(url).then((v) => { cache.set(url, v); return v })
    cache.set(url, p)
    e = p
  }
  return e
}

/** 'loading' until the belfry answers, then the preview or null (just the link). */
export function usePreview(url: string | undefined): LinkPreview | null | 'loading' {
  const now = url ? getPreview(url) : null
  const [v, setV] = useState<LinkPreview | null | 'loading'>(now instanceof Promise ? 'loading' : now)
  useEffect(() => {
    if (!url) { setV(null); return }
    const e = getPreview(url)
    if (!(e instanceof Promise)) { setV(e); return }
    setV('loading')
    let on = true
    void e.then((x) => { if (on) setV(x) })
    return () => { on = false }
  }, [url])
  return v
}
