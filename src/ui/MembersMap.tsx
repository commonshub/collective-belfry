// Where a channel's members are, on an OpenStreetMap map: one dot per city with how many are
// there; tap it for their names. Only members who share their city (people.ts) are on it, at the
// city's point. Names go in as text, never as HTML.
import { useEffect, useRef } from 'react'
import L from 'leaflet'
import 'leaflet/dist/leaflet.css'
import { displayName } from '../core/model'
import { extras, placeLabel, type Place } from '../core/people'

export default function MembersMap({ pubkeys, onOpen }: { pubkeys: string[]; onOpen: (pubkey: string) => void }) {
  const box = useRef<HTMLDivElement>(null)
  const map = useRef<L.Map | null>(null)
  const layer = useRef<L.LayerGroup | null>(null)
  const openRef = useRef(onOpen)
  openRef.current = onOpen

  const x = extras()
  const spots = new Map<string, { place: Place; who: string[] }>()
  for (const pk of pubkeys) {
    const p = x.get(pk)?.place
    if (p?.lat === undefined || p.lon === undefined) continue
    const k = `${p.lat},${p.lon}`
    const s = spots.get(k) ?? { place: p, who: [] }
    s.who.push(pk)
    spots.set(k, s)
  }
  const key = [...spots].map(([k, s]) => k + ':' + s.who.join(',')).join('|')

  useEffect(() => {
    if (!box.current || map.current) return
    map.current = L.map(box.current, { zoomControl: true, attributionControl: true, worldCopyJump: true, scrollWheelZoom: false })
    L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 12, attribution: '© <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noreferrer">OpenStreetMap</a> contributors',
    }).addTo(map.current)
    layer.current = L.layerGroup().addTo(map.current)
    return () => { map.current?.remove(); map.current = null }
  }, [])

  useEffect(() => {
    const m = map.current, lg = layer.current
    if (!m || !lg) return
    lg.clearLayers()
    const pts: L.LatLngExpression[] = []
    for (const s of spots.values()) {
      const at: L.LatLngExpression = [s.place.lat!, s.place.lon!]
      pts.push(at)
      // the html is only a number we count ourselves
      const icon = L.divIcon({ className: 'map-dot', html: String(Number(s.who.length)), iconSize: [30, 30] })
      const mk = L.marker(at, { icon, title: `${placeLabel(s.place)}: ${s.who.length} ${s.who.length === 1 ? 'member' : 'members'}`, keyboard: true }).addTo(lg)
      const pop = document.createElement('div')
      pop.className = 'map-pop'
      const h = document.createElement('b'); h.textContent = placeLabel(s.place); pop.append(h)
      for (const pk of s.who) {
        const b = document.createElement('button')
        b.type = 'button'; b.className = 'link'; b.textContent = displayName(pk)
        b.onclick = () => openRef.current(pk)
        pop.append(b)
      }
      mk.bindPopup(pop)
    }
    if (pts.length === 1) m.setView(pts[0], 6)
    else if (pts.length) m.fitBounds(L.latLngBounds(pts), { padding: [28, 28], maxZoom: 7 })
    else m.setView([30, 10], 1)
  }, [key]) // eslint-disable-line react-hooks/exhaustive-deps

  return <div ref={box} className="members-map" role="region" aria-label="Map of where members are" />
}
