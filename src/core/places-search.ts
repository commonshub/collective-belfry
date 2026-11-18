// "Where" suggestions for an event (Xavier, 2026-11-18): places this belfry used before come
// first, then OpenStreetMap through Photon (photon.komoot.io: made for search-as-you-type, unlike
// Nominatim, whose policy forbids autocomplete). Only what the person types is sent, after a pause.
export type PlaceHit = { label: string; sub?: string; past?: boolean }

const cache = new Map<string, PlaceHit[]>()

type PhotonProps = { name?: string; street?: string; housenumber?: string; postcode?: string; city?: string; state?: string; country?: string; type?: string }

/** One line for a Photon result: "Commons Hub Brussels, Galerie Bortier, 1000 Brussels, Belgium". */
export function photonLabel(p: PhotonProps): PlaceHit | null {
  const street = [p.street, p.housenumber].filter(Boolean).join(' ')
  const town = [p.postcode, p.city].filter(Boolean).join(' ')
  const name = p.name || street || p.city
  if (!name) return null
  const rest = [street !== name ? street : '', town !== name ? town : '', p.country].filter(Boolean)
  return { label: [name, ...rest].join(', '), sub: rest.join(', ') || undefined }
}

export async function searchPlaces(q: string, signal?: AbortSignal): Promise<PlaceHit[]> {
  const key = q.trim().toLowerCase()
  if (key.length < 3) return []
  const hit = cache.get(key)
  if (hit) return hit
  const lang = (navigator.language || 'en').slice(0, 2)
  const r = await fetch(`https://photon.komoot.io/api/?q=${encodeURIComponent(q.trim())}&limit=5&lang=${['en', 'de', 'fr'].includes(lang) ? lang : 'en'}`, { signal })
  if (!r.ok) return []
  const j = await r.json() as { features?: { properties: PhotonProps }[] }
  const out: PlaceHit[] = []
  for (const f of j.features ?? []) {
    const h = photonLabel(f.properties)
    if (h && !out.some((x) => x.label === h.label)) out.push(h)
  }
  cache.set(key, out)
  return out
}

/** Past places matching what is typed (all of them while it is empty), newest first. */
export const pastMatches = (past: string[], q: string) => {
  const k = q.trim().toLowerCase()
  return past.filter((p) => !k || (p.toLowerCase().includes(k) && p.toLowerCase() !== k)).slice(0, 5)
}
