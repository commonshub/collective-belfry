import { useEffect, useState } from 'react'

// A message on its way (the lead, from Xavier, 2026-11-21): "Sending…", after 5 s "Still sending…"
// with the step it is at, after 30 s "Not sent yet · Retry" (it keeps trying by itself too).

/** The step in people's words, the same on the three apps (the lead, 2026-11-21): connecting,
 *  encrypting, uploading x of y MB, or waiting for the server (never "signer" or "relay"). */
export function peopleStep(step?: string): string | undefined {
  if (!step) return undefined
  if (step.startsWith('uploading') || step === 'encrypting') return step
  if (/^(connecting|checking the connection|signing in|waiting for the connection)$/.test(step)) return 'connecting'
  return 'waiting for the server' // signing, publishing, calling the belfry, waiting to try again
}

export const STILL_MS = 5_000
export const NOT_YET_MS = 30_000

/** "Sending…" / "Still sending… · <step>" / "Not sent yet · Retry" for a send started at SINCE (ms). */
export function SendingLine({ since, step, onRetry, className }: { since: number; step?: string; onRetry: () => void; className: string }) {
  const [, tick] = useState(0)
  const age = Date.now() - since
  useEffect(() => {
    if (age >= NOT_YET_MS) return
    const t = setTimeout(() => tick((n) => n + 1), (age < STILL_MS ? STILL_MS : NOT_YET_MS) - age + 50)
    return () => clearTimeout(t)
  }, [age])
  if (!navigator.onLine) return <span className={className}>Waiting for the connection…</span>
  if (age < STILL_MS) return <span className={className}>Sending…</span>
  const said = peopleStep(step)
  if (age < NOT_YET_MS) return <span className={className}>Still sending…{said ? ` · ${said}` : ''}</span>
  return <span className={className}>Not sent yet · <button type="button" className="link" onClick={onRetry}>Retry</button></span>
}
