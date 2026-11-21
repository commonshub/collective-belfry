import { useEffect, useRef, useState } from 'react'
import { CheckIcon, CopyIcon, ShareIcon } from './icons'

// Copy link · Share… (canvas v113 events/event-full-address, -copied; the invite sheets): side by
// side, equal widths, 50 px tall. Copy link reads "Copied" with a check for 2 s. Share… opens the
// browser's share sheet where there is one, else it copies.

export async function copyText(text: string) {
  try { await navigator.clipboard.writeText(text) } catch {
    const t = document.createElement('textarea'); t.value = text; document.body.appendChild(t); t.select(); document.execCommand('copy'); t.remove()
  }
}

export function LinkPair({ text, share, copyLabel = 'Copy link', disabled = false }: { text: string; share: ShareData; copyLabel?: string; disabled?: boolean }) {
  const [copied, setCopied] = useState(false)
  const timer = useRef<ReturnType<typeof setTimeout>>()
  useEffect(() => () => clearTimeout(timer.current), [])
  useEffect(() => setCopied(false), [text])
  const copy = async () => {
    await copyText(text)
    setCopied(true)
    clearTimeout(timer.current)
    timer.current = setTimeout(() => setCopied(false), 2000)
  }
  const doShare = async () => {
    const nav = navigator as Navigator & { share?: (d: ShareData) => Promise<void> }
    if (nav.share) { try { await nav.share(share) } catch { /* cancelled */ } return }
    await copy()
  }
  return (
    <div className="link-pair">
      <button type="button" className={'btn ghost' + (copied ? ' ok' : '')} disabled={disabled} onClick={() => void copy()} aria-live="polite">
        {copied ? <><CheckIcon /> Copied</> : <><CopyIcon /> {copyLabel}</>}
      </button>
      <button type="button" className="btn ghost" disabled={disabled} onClick={() => void doShare()}><ShareIcon /> Share…</button>
    </div>
  )
}
