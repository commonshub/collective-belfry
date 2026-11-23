// The preview of an outside page under a message (Channels canvas v10: channels/og-message,
// og-loading, og-no-image, og-no-preview, og-several, og-dm, og-thread, og-dark, og-laptop) and
// above the composer before sending (og-composer, with ✕).
import { useEffect, useRef, useState } from 'react'
import { usePreview, type LinkPreview } from '../core/linkpreview'
const openOut = (href: string) => window.open(href, '_blank', 'noopener')

/** Under a message: loading → a grey box of the same size; nothing found → nothing (just the link). */
export function OgCard({ url }: { url: string }) {
  const box = useRef<HTMLSpanElement>(null)
  const [seen, setSeen] = useState(typeof IntersectionObserver === 'undefined')
  useEffect(() => {
    if (seen || !box.current) return
    const io = new IntersectionObserver((es) => { if (es.some((e) => e.isIntersecting)) { setSeen(true); io.disconnect() } }, { rootMargin: '200px' })
    io.observe(box.current)
    return () => io.disconnect()
  })
  const p = usePreview(url, seen)
  if (p === null) return null
  if (p === 'loading') return (
    <span ref={box} className="og load" role="status" aria-label="Loading the preview">
      <span className="og-img" />
      <span className="og-tb"><span className="bar" style={{ width: '40%' }} /><span className="bar big" style={{ width: '90%' }} /><span className="bar" style={{ width: '75%' }} /></span>
    </span>
  )
  const pic = !!(p.image || p.tint)
  return (
    <a className={'og' + (pic ? '' : ' noimg')} href={p.url} target="_blank" rel="noopener noreferrer nofollow"
      onClick={(e) => { e.preventDefault(); e.stopPropagation(); openOut(p.url) }}>
      {p.image ? <img className="og-img" src={p.image} alt="Picture from the page" loading="lazy" referrerPolicy="no-referrer" />
        : p.tint ? <span className="og-img tint" role="img" aria-label="Picture from the page" /> : null}
      <span className="og-tb">
        <span className="og-site">{[p.site, p.host].filter(Boolean).filter((x, i, a) => a.indexOf(x) === i).join(' · ')}</span>
        {p.title && <span className="og-ti">{p.title}</span>}
        {p.description && <span className="og-ds">{p.description}</span>}
      </span>
    </a>
  )
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
