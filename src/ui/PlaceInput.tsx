// The "Where" field of an event: a text field with a list under it, past places first, then
// OpenStreetMap places (src/core/places-search.ts). Up/down and Enter pick; Escape closes.
import { useEffect, useRef, useState } from 'react'
import { pastMatches, searchPlaces, type PlaceHit } from '../core/places-search'

export default function PlaceInput({ value, onChange, past, placeholder }: { value: string; onChange: (v: string) => void; past: string[]; placeholder?: string }) {
  const [open, setOpen] = useState(false)
  const [osm, setOsm] = useState<PlaceHit[]>([])
  const [busy, setBusy] = useState(false)
  const [at, setAt] = useState(-1)
  const typed = useRef(false)
  useEffect(() => {
    if (!typed.current || value.trim().length < 3) { setOsm([]); setBusy(false); return }
    const ctl = new AbortController()
    setBusy(true)
    const t = setTimeout(() => {
      searchPlaces(value, ctl.signal).then((r) => { setOsm(r); setBusy(false) }, () => setBusy(false))
    }, 400)
    return () => { clearTimeout(t); ctl.abort() }
  }, [value])
  const mine = pastMatches(past, value).map((label): PlaceHit => ({ label, past: true }))
  const list = [...mine, ...osm.filter((o) => !mine.some((m) => m.label === o.label) && o.label !== value)]
  const show = open && list.length > 0
  const pick = (h: PlaceHit) => { typed.current = false; onChange(h.label); setOpen(false); setAt(-1) }
  return (
    <div className="place-input">
      <input className="input" placeholder={placeholder} value={value} autoComplete="off" role="combobox" aria-expanded={show} aria-autocomplete="list"
        onFocus={() => setOpen(true)} onBlur={() => setTimeout(() => setOpen(false), 150)}
        onChange={(e) => { typed.current = true; onChange(e.target.value); setOpen(true); setAt(-1) }}
        onKeyDown={(e) => {
          if (!show) return
          if (e.key === 'ArrowDown') { e.preventDefault(); setAt((at + 1) % list.length) }
          else if (e.key === 'ArrowUp') { e.preventDefault(); setAt((at - 1 + list.length) % list.length) }
          else if (e.key === 'Enter' && at >= 0) { e.preventDefault(); pick(list[at]) }
          else if (e.key === 'Escape') setOpen(false)
        }} />
      {show && (
        <ul className="place-list" role="listbox">
          {list.map((h, i) => (
            <li key={(h.past ? 'p:' : 'o:') + h.label} role="option" aria-selected={i === at} className={(i === at ? 'on' : '') + (h.past ? ' past' : '')}
              onMouseDown={(e) => { e.preventDefault(); pick(h) }}>
              <span aria-hidden>{h.past ? '🕘' : '📍'}</span>
              <span className="t"><b>{h.past ? h.label : h.label.split(', ')[0]}</b>{h.past ? <small>Used before</small> : h.sub && <small>{h.sub}</small>}</span>
            </li>
          ))}
          {osm.length > 0 && <li className="place-credit" aria-hidden>Places from OpenStreetMap</li>}
        </ul>
      )}
      {open && busy && list.length === 0 && <div className="place-list place-credit">Looking…</div>}
    </div>
  )
}
