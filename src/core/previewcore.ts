// Link previews built by the sender (collective-belfry docs/link-previews-v2.md; vectors/link-previews.json,
// written by belfry-android): which link of a text gets a preview, the tags a message carries, and what a
// reader shows from them. Pure: no browser, no network (the vector check bundles it as is).

export const TITLE_MAX = 200, DESCRIPTION_MAX = 300, SITE_MAX = 100
export const NO_PREVIEW: string[] = ['preview', 'none']
export const PURPOSE = 'purpose preview'
export const noPreview = (tags?: string[][]) => !!tags?.some((t) => t[0] === 'preview' && t[1] === 'none')

const URL_RE = /https?:\/\/[^\s<>()]+[^\s<>().,;:!?'"]/g
const MEDIA = /\.(jpe?g|png|gif|webp|avif|heic|svg|mp4|mov|webm|m4a|mp3|ogg|wav|pdf)(\?|#|$)/i
const hostOf = (u: string) => { try { return new URL(u).hostname.toLowerCase() } catch { return '' } }
/** A name under the community's domain (the domain itself and every subdomain), case aside. */
const ours = (host: string, domain: string) => { const d = domain.toLowerCase(); return host === d || host.endsWith('.' + d) }

/** The first outside https link of TEXT (the composer's rule): not the belfry's own (its domain and every
 *  name under it), not the file server, not a file (attached, or by its ending), no credentials; none when
 *  the message says ["preview","none"]. FILES: the file server's origin. */
export function previewLink(text: string, tags: string[][], domain: string, files = ''): string | null {
  if (!text || noPreview(tags)) return null
  const attached = new Set(tags.filter((t) => t[0] === 'imeta').flatMap((t) => t.filter((x) => x.startsWith('url ')).map((x) => x.slice(4))))
  const filesHost = files ? hostOf(files) : ''
  for (const m of text.matchAll(URL_RE)) {
    const u = m[0]
    if (!u.startsWith('https://')) continue
    let url: URL
    try { url = new URL(u) } catch { continue }
    if (url.username || url.password) continue
    const host = url.hostname.toLowerCase()
    if (!host.includes('.') || ours(host, domain) || (filesHost && host === filesHost)) continue
    if (attached.has(u) || MEDIA.test(url.pathname)) continue
    return u
  }
  return null
}

/** Cut to N Unicode code points, then the trailing spaces go. */
const cut = (s: string, n: number) => [...s].slice(0, n).join('').replace(/\s+$/u, '')
const clean = (s: unknown, n: number) => (typeof s === 'string' ? cut(s.replace(/\s+/g, ' ').trim(), n) : '')

export interface PreviewText { url: string; title: string; description: string; site: string }
export interface PreviewPicture { url: string; mime: string; width?: number; height?: number; x: string; ox?: string; key?: string; nonce?: string }

/** The tags a message carries for its preview (and its picture, when one was uploaded). */
export function previewTags(p: PreviewText, pic?: PreviewPicture | null): string[][] {
  const out: string[][] = [['preview', p.url, clean(p.title, TITLE_MAX), clean(p.description, DESCRIPTION_MAX), clean(p.site, SITE_MAX)]]
  if (pic) out.push(['imeta', `url ${pic.url}`, `m ${pic.mime}`, ...(pic.width && pic.height ? [`dim ${pic.width}x${pic.height}`] : []), `x ${pic.x}`,
    ...(pic.key ? [...(pic.ox ? [`ox ${pic.ox}`] : []), 'encryption-algorithm aes-gcm', `decryption-key ${pic.key}`, `decryption-nonce ${pic.nonce}`] : []), PURPOSE])
  return out
}

const IMAGE = new Set(['image/jpeg', 'image/png', 'image/webp', 'image/gif'])
export interface Shown { url: string; title: string; description: string; site: string; site_line: string; picture: null | { url: string; mime: string; width: number | null; height: number | null; encrypted: boolean } }
export interface ShownWithKeys extends Shown { keys?: { x: string; ox?: string; key?: string; nonce?: string } }

/** What a reader shows for a message (or null: a plain link). Only from the tags: the first preview tag
 *  whose https link the text holds, with a title or a description; its picture only from the belfry's
 *  own Blossom (exactly that origin), an image type, with x (and key + nonce + aes-gcm when encrypted). */
export function readPreview(text: string, tags: string[][], blossom: string): ShownWithKeys | null {
  const t = tags.find((x) => x[0] === 'preview')
  if (!t || t[1] === 'none' || typeof t[1] !== 'string' || !t[1].startsWith('https://') || !text.includes(t[1])) return null
  const title = clean(t[2], TITLE_MAX), description = clean(t[3], DESCRIPTION_MAX), site = clean(t[4], SITE_MAX)
  if (!title && !description) return null
  const host = hostOf(t[1]).replace(/^www\./, '')
  const site_line = [...new Set([site, host].filter(Boolean))].join(' · ')
  let picture: Shown['picture'] = null, keys: ShownWithKeys['keys']
  const im = tags.find((x) => x[0] === 'imeta' && x.includes(PURPOSE))
  if (im) {
    const f: Record<string, string> = {}
    for (const p of im.slice(1)) { const i = p.indexOf(' '); if (i > 0) f[p.slice(0, i)] = p.slice(i + 1) }
    let origin = ''
    try { const u = new URL(f.url ?? ''); origin = u.protocol === 'https:' ? u.origin : '' } catch { /* not a URL */ }
    let want = ''
    try { want = new URL(blossom).origin } catch { /* none */ }
    const enc = !!(f['decryption-key'] || f['decryption-nonce'] || f['encryption-algorithm'])
    const encOk = !enc || (f['encryption-algorithm'] === 'aes-gcm' && /^[0-9a-f]{64}$/.test(f['decryption-key'] ?? '') && /^[0-9a-f]{24}$/.test(f['decryption-nonce'] ?? ''))
    if (origin && origin === want && IMAGE.has(f.m ?? '') && /^[0-9a-f]{64}$/.test(f.x ?? '') && encOk) {
      const [w, h] = (f.dim ?? '').split('x').map(Number)
      picture = { url: f.url, mime: f.m, width: w > 0 && h > 0 ? w : null, height: w > 0 && h > 0 ? h : null, encrypted: enc }
      keys = { x: f.x, ...(f.ox ? { ox: f.ox } : {}), ...(enc ? { key: f['decryption-key'], nonce: f['decryption-nonce'] } : {}) }
    }
  }
  return { url: t[1], title, description, site, site_line, picture, ...(keys ? { keys } : {}) }
}
