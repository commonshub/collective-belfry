// The preview of an outside page under a message (Channels canvas v10: channels/og-message,
// og-loading, og-no-image, og-no-preview, og-several, og-dm, og-thread, og-dark, og-laptop) and
// above the composer before sending (og-composer, with ✕).
import { useEffect, useState } from 'react'
import { usePreview, type LinkPreview } from '../core/linkpreview'
import type { ShownWithKeys } from '../core/previewcore'
import { openFile, sha256hex } from '../core/dmfiles'
const openOut = (href: string) => window.open(href, '_blank', 'noopener')

/** Under a message: built only from its tags (docs/link-previews-v2.md): nothing fetched but its picture,
 *  from the belfry's own Blossom, checked against x and opened when encrypted. */
export function OgCard({ shown }: { shown: ShownWithKeys }) {
  const pic = usePicture(shown)
  const hasPic = !!shown.picture
  return (
    <a className={'og' + (hasPic ? '' : ' noimg')} href={shown.url} target="_blank" rel="noopener noreferrer nofollow"
      onClick={(e) => { e.preventDefault(); e.stopPropagation(); openOut(shown.url) }}>
      {hasPic && (pic ? <img className="og-img" src={pic} alt="Picture from the page" loading="lazy" referrerPolicy="no-referrer" /> : <span className="og-img" role="img" aria-label="Picture from the page" />)}
      <span className="og-tb">
        <span className="og-site">{shown.site_line}</span>
        {shown.title && <span className="og-ti">{shown.title}</span>}
        {shown.description && <span className="og-ds">{shown.description}</span>}
      </span>
    </a>
  )
}

const pictures = new Map<string, string>() // url → object URL (checked, opened)
function usePicture(s: ShownWithKeys): string | undefined {
  const p = s.picture
  const [src, setSrc] = useState(() => (p ? pictures.get(p.url) ?? (p.url.startsWith('data:image/') ? p.url : undefined) : undefined))
  useEffect(() => {
    if (!p || src || !s.keys) return
    let live = true
    void (async () => {
      const bytes = new Uint8Array(await (await fetch(p.url)).arrayBuffer())
      let plain = bytes
      if (p.encrypted) {
        const o = await openFile([['encryption-algorithm', 'aes-gcm'], ['decryption-key', s.keys!.key ?? ''], ['decryption-nonce', s.keys!.nonce ?? ''], ['x', s.keys!.x], ['ox', s.keys!.ox ?? '']], bytes)
        if (!o.ok) return
        plain = o.plain
      } else if ((await sha256hex(bytes)) !== s.keys!.x) return // not what the sender uploaded: no picture
      const u = URL.createObjectURL(new Blob([plain as BlobPart], { type: p.mime }))
      pictures.set(p.url, u)
      if (live) setSrc(u)
    })().catch(() => {})
    return () => { live = false }
  }, [p?.url]) // eslint-disable-line react-hooks/exhaustive-deps
  return src
}

/** Above the field before sending: the preview the message will get, ✕ = send the link without it. */
export function ComposerPreview({ url, onDrop }: { url: string; onDrop: () => void }) {
  const p = usePreview(url)
  if (p === null || p === 'loading') return null
  return (
    <div className="og-pre">
      {p.image ? <img className="og-th" src={p.image} alt="Picture from the page" referrerPolicy="no-referrer" /> : p.tint ? <span className="og-th tint" role="img" aria-label="Picture from the page" /> : null}
      <span className="og-pre-t"><span className="t1">{p.title || p.host}</span><span className="t2">{p.site || p.host}</span></span>
      <button type="button" className="og-x" aria-label="Don’t show a preview" onClick={onDrop}>
        <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18" /></svg>
      </button>
    </div>
  )
}
export type { LinkPreview }
