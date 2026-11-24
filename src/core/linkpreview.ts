// Link previews built by the SENDER (collective-belfry docs/link-previews-v2.md; Xavier: "for all").
// Readers never fetch anything: the box comes only from the message's ["preview", url, title,
// description, site] tag and its imeta "purpose preview" picture (core/previewcore.ts, the shared vectors).
// The web sender, in a channel, an open group or an unsealed DM, asks the belfry once while composing
// (GET /api/v1/link-preview?url=, NIP-98), takes the picture from the belfry's proxy, makes it small and
// uploads it to Blossom itself; in sealed or encrypted conversations the web sends no preview.
import { useEffect, useState } from 'react'
import { BELFRY } from '../config'
import { target } from './refs'
import { isLocalOnly, signedFetch, uploadImage } from './sync'
import { NO_PREVIEW, noPreview, previewLink, previewTags, readPreview, type ShownWithKeys } from './previewcore'

export { NO_PREVIEW, noPreview }
export interface LinkPreview { url: string; title: string; description: string; site: string; host: string; image?: string; /** demo only: a coloured block stands for the picture */ tint?: boolean }

/** The composer's link: the first outside https link (previewcore), not one of this belfry's own cards. */
export function outsideLink(text: string, tags: string[][] = []): string | undefined {
  const u = previewLink(text, tags, BELFRY.domain)
  return u && !target(u) ? u : undefined
}

/** What a reader shows for a message, from its tags only (null: a plain link). The demo (no relay) keeps
 *  its pictures in the page (data: URLs), so it may show those. */
export function shownFor(text: string, tags: string[][] = []): ShownWithKeys | null {
  const r = readPreview(text, tags, BELFRY.blossom)
  if (!r || r.picture || !isLocalOnly()) return r
  const im = tags.find((t) => t[0] === 'imeta' && t.includes('purpose preview'))
  const url = im?.find((x) => x.startsWith('url data:image/'))?.slice(4)
  return url ? { ...r, picture: { url, mime: 'image/jpeg', width: null, height: null, encrypted: false } } : r
}

/** The tags the sender attaches for LINK: the belfry's answer (asked while composing; waited for a few
 *  seconds at most) and, when it has a picture, that picture made small (≤ 1200 px, ≤ 300 kB JPEG) and
 *  uploaded to Blossom. Nothing usable, or offline: no tags (a plain link). */
export async function buildPreviewTags(link: string): Promise<string[][]> {
  const e = getPreview(link)
  const p = e instanceof Promise ? await Promise.race([e, new Promise<null>((r) => setTimeout(() => r(null), 6000))]) : e
  if (!p) return []
  let pic: Parameters<typeof previewTags>[1] = null
  try {
    const blob = p.image ? await (await fetch(p.image)).blob() : p.tint ? await demoPicture() : null
    if (blob && blob.size <= 5_242_880 && /^image\/(jpeg|png|webp|gif)$/.test(blob.type || 'image/jpeg')) {
      const small = await shrink(blob)
      if (small) { const up = await uploadImage(small.blob); pic = { url: up.url, mime: 'image/jpeg', width: small.w, height: small.h, x: up.x } }
    }
  } catch { /* the picture couldn't be had: the preview without it */ }
  return previewTags({ url: link, title: p.title, description: p.description, site: p.site }, pic)
}

async function shrink(b: Blob): Promise<{ blob: Blob; w: number; h: number } | null> {
  const img = await createImageBitmap(b)
  const scale = Math.min(1, 1200 / Math.max(img.width, img.height))
  const w = Math.max(1, Math.round(img.width * scale)), h = Math.max(1, Math.round(img.height * scale))
  const c = document.createElement('canvas'); c.width = w; c.height = h
  c.getContext('2d')!.drawImage(img, 0, 0, w, h)
  for (const q of [0.82, 0.7, 0.55, 0.4]) {
    const out = await new Promise<Blob | null>((r) => c.toBlob(r, 'image/jpeg', q))
    if (out && out.size <= 307_200) return { blob: out, w, h }
  }
  return null
}
async function demoPicture(): Promise<Blob> {
  const c = document.createElement('canvas'); c.width = 600; c.height = 315
  const g = c.getContext('2d')!; const gr = g.createLinearGradient(0, 0, 600, 315); gr.addColorStop(0, '#7FA88B'); gr.addColorStop(1, '#2E5E43'); g.fillStyle = gr; g.fillRect(0, 0, 600, 315)
  return new Promise((r) => c.toBlob((b) => r(b!), 'image/jpeg', 0.8))
}

// ---- the sender's ask to the belfry (once per URL per page; nothing found is remembered too) --------------------------
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
  // the page after its redirects names the site (a short link shows where it goes); the box opens what was written
  if (typeof d.url === 'string') { try { const f = new URL(d.url); if (f.protocol === 'https:') host = f.hostname.replace(/^www\./, '') } catch { /* keep the link's */ } }
  let image: string | undefined
  if (typeof d.image === 'string') { try { const i = new URL(d.image); if (i.protocol === 'https:' && i.origin === belfryOrigin) image = i.href } catch { /* not a URL */ } }
  return { url, title, description, site: str(d.site_name, 80), host, image }
}

const DEMO: Record<string, Record<string, unknown>> = {
  'https://toolshare.example/stories/street-library': { title: 'How a street tool library lends 400 drills a year', description: 'Neighbours in a small street started lending their tools from a garage. Two years later they share drills, ladders and saws with half the town, and nobody keeps a list.', site_name: 'ToolShare Journal', image: 'yes' },
  'https://repair.example/guide': { title: 'The repair café guide', description: 'How to start one in your street.', site_name: 'Repair' },
}

async function load(url: string): Promise<LinkPreview | null> {
  if (isLocalOnly()) { // (the demo with no relay: its stand-in; a demo session on a real relay asks the belfry)
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
export function usePreview(url: string | undefined, visible = true): LinkPreview | null | 'loading' {
  // asked only once it is on screen (the server: only when visible); until then the same-size box
  const known = url ? cache.get(url) : null
  const now = url && !visible ? (known === undefined ? 'loading' : known) : url ? getPreview(url) : null
  const [v, setV] = useState<LinkPreview | null | 'loading'>(now instanceof Promise ? 'loading' : (now as LinkPreview | null | 'loading'))
  useEffect(() => {
    if (!url) { setV(null); return }
    if (!visible && cache.get(url) === undefined) { setV('loading'); return }
    const e = getPreview(url)
    if (!(e instanceof Promise)) { setV(e); return }
    setV('loading')
    let on = true
    void e.then((x) => { if (on) setV(x) })
    return () => { on = false }
  }, [url, visible])
  return v
}
