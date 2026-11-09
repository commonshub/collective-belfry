// What members say about themselves beyond their Nostr profile (kind 0): tags and where they are.
// Kept in ONE replaceable event per member on the community relay, which only members can read
// (guests and the public can't): kind 30078 (NIP-78), d = "belfry:profile", content JSON
//   { "tags": ["facilitation", "solar"], "share": "city" | "country" | "none",
//     "place": { "id": "gn:2800866", "name": "Brussels", "region": "…", "country": "BE", "lat": 50.85, "lon": 4.35 } }
// "country" keeps only { country }; "none" keeps no place. A place is always a city's point
// (from the belfry's list, or the nearest city of one a member added), never the phone's GPS.
import { BELFRY } from '../config'
import { loadAccessInfo, accessInfo, belfryAdminKeys } from './access'
import { all, touch, type Ev } from './store'
import { enqueue, isLocalOnly, me, signedFetch } from './sync'

export const PROFILE_D = 'belfry:profile'

export interface Place { id?: string; name?: string; region?: string; country: string; lat?: number; lon?: number; near?: string }
export type Share = 'city' | 'country' | 'none'
export interface Extras { tags: string[]; share: Share; place?: Place; at: number }

const clean = (t: string) => t.trim().toLowerCase().replace(/^#/, '').replace(/\s+/g, '-').replace(/[^\p{L}\p{N}_-]/gu, '').slice(0, 32)

function parse(e: Ev): Extras | null {
  try {
    const c = JSON.parse(e.content) as Partial<Extras>
    const tags = Array.isArray(c.tags) ? [...new Set(c.tags.filter((t) => typeof t === 'string').map(clean).filter(Boolean))].slice(0, 30) : []
    const share: Share = c.share === 'country' || c.share === 'none' ? c.share : 'city'
    let place: Place | undefined
    const p = c.place
    if (share !== 'none' && p && typeof p.country === 'string' && /^[A-Z]{2}$/.test(p.country)) {
      place = share === 'country'
        ? { country: p.country }
        : {
            country: p.country,
            id: typeof p.id === 'string' ? p.id.slice(0, 40) : undefined,
            name: typeof p.name === 'string' ? p.name.slice(0, 80) : undefined,
            region: typeof p.region === 'string' ? p.region.slice(0, 80) : undefined,
            lat: typeof p.lat === 'number' && Math.abs(p.lat) <= 90 ? Math.round(p.lat * 100) / 100 : undefined,
            lon: typeof p.lon === 'number' && Math.abs(p.lon) <= 180 ? Math.round(p.lon * 100) / 100 : undefined,
          }
    }
    return { tags, share, place, at: e.created_at }
  } catch { return null }
}

/** Every member's tags and place, newest event per member. */
export function extras(): Map<string, Extras> {
  const out = new Map<string, Extras>()
  for (const e of all(me())) {
    if (e.kind !== 30078 || !e.tags.some((t) => t[0] === 'd' && t[1] === PROFILE_D)) continue
    const prev = out.get(e.pubkey)
    if (prev && prev.at >= e.created_at) continue
    const x = parse(e)
    if (x) out.set(e.pubkey, x)
  }
  return out
}

/** Tags any member uses, most used first: the suggestions when you add yours. */
export function allTags(): { tag: string; count: number }[] {
  const n = new Map<string, number>()
  for (const x of extras().values()) for (const t of x.tags) n.set(t, (n.get(t) ?? 0) + 1)
  return [...n].map(([tag, count]) => ({ tag, count })).sort((a, b) => b.count - a.count || a.tag.localeCompare(b.tag))
}

export const cleanTag = clean

export async function saveExtras(x: { tags: string[]; share: Share; place?: Place }) {
  const place = x.share === 'none' || !x.place ? undefined : x.share === 'country' ? { country: x.place.country } : x.place
  const content = JSON.stringify({ tags: [...new Set(x.tags.map(clean).filter(Boolean))].slice(0, 30), share: x.share, ...(place ? { place } : {}) })
  await enqueue('main', { kind: 30078, content, tags: [['d', PROFILE_D]] })
}

/** Name and about in the kind-0 profile, the rest of it kept. */
export async function saveProfile(p: { name: string; about: string }) {
  const mine = all(me()).filter((e) => e.kind === 0 && e.pubkey === me()).sort((a, b) => b.created_at - a.created_at)[0]
  let content: Record<string, unknown> = {}
  try { content = mine ? JSON.parse(mine.content) : {} } catch { content = {} }
  const name = p.name.trim().slice(0, 100)
  await enqueue('main', { kind: 0, content: JSON.stringify({ ...content, name, display_name: name, about: p.about.trim().slice(0, 2000) }), tags: [] })
}

/** About text from someone's kind 0. */
export function aboutOf(pubkey: string): string {
  const e = all(me()).filter((x) => x.kind === 0 && x.pubkey === pubkey).sort((a, b) => b.created_at - a.created_at)[0]
  try { const c = e ? JSON.parse(e.content) : {}; return typeof c.about === 'string' ? c.about : '' } catch { return '' }
}

// ---- places: the belfry's list of cities (GeoNames) plus the ones members added ----------------

const DEMO_PLACES: Place[] = [
  { id: 'gn:2800866', name: 'Brussels', region: 'Brussels Capital', country: 'BE', lat: 50.85, lon: 4.35 },
  { id: 'gn:2643743', name: 'London', region: 'England', country: 'GB', lat: 51.51, lon: -0.13 },
  { id: 'gn:2759794', name: 'Amsterdam', region: 'North Holland', country: 'NL', lat: 52.37, lon: 4.89 },
  { id: 'gn:2179537', name: 'Wellington', region: 'Wellington', country: 'NZ', lat: -41.29, lon: 174.78 },
  { id: 'gn:3117735', name: 'Madrid', region: 'Madrid', country: 'ES', lat: 40.42, lon: -3.7 },
  { id: 'gn:2950159', name: 'Berlin', region: 'Berlin', country: 'DE', lat: 52.52, lon: 13.41 },
]
const fold = (s: string) => s.normalize('NFD').replace(/\p{M}/gu, '').toLowerCase()

export async function searchPlaces(q: string, signal?: AbortSignal): Promise<{ places: Place[]; attribution?: string }> {
  if (q.trim().length < 2) return { places: [] }
  if (isLocalOnly()) return { places: DEMO_PLACES.filter((p) => fold(p.name!).startsWith(fold(q.trim()))) }
  const res = await fetch(`${BELFRY.api}/places?q=${encodeURIComponent(q.trim())}&limit=8`, { signal })
  if (!res.ok) throw new Error('The list of places is not available right now.')
  return (await res.json()) as { places: Place[]; attribution?: string }
}

/** A place the list doesn't have: its name, its country, and the nearest listed city (whose
 *  point it uses on the map). Saved on the belfry so the next member finds it. */
export async function addPlace(name: string, country: string, near: Place): Promise<Place> {
  if (isLocalOnly()) return { id: 'b:demo', name, country, lat: near.lat, lon: near.lon, near: near.name }
  const res = await signedFetch(`${BELFRY.api}/places`, 'POST', { name, country, near: near.id })
  const body = (await res.json().catch(() => ({}))) as { place?: Place; error?: string }
  if (!res.ok || !body.place) throw new Error(body.error === 'bad_country' ? 'That country code is not known.' : 'Could not add the place. Try again.')
  return body.place
}

// ---- belfry admins -------------------------------------------------------------------------

export const isBelfryAdmin = (pubkey: string) => belfryAdminKeys().includes(pubkey) || !!accessInfo()?.admins.includes(pubkey)

export async function setBelfryAdmin(pubkey: string, admin: boolean) {
  const res = await signedFetch(`${BELFRY.api}/admins`, 'POST', { pubkey, admin })
  const body = (await res.json().catch(() => ({}))) as { error?: string }
  if (!res.ok) {
    throw new Error(body.error === 'not_member' ? 'Only members can be admins (not guests or agents).'
      : body.error === 'last_admin' ? 'A belfry needs at least one admin.'
      : body.error === 'not_admin' ? 'Only admins of the belfry can do this.'
      : 'Could not change it. Try again.')
  }
  await loadAccessInfo()
  touch()
}

// ---- countries (ISO 3166-1 alpha-2), named in the reader's language ---------------------------

const CODES = 'AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ BR BS BT BV BW BY BZ CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU ID IE IL IM IN IO IQ IR IS IT JE JM JO JP KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS MT MU MV MW MX MY MZ NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM PN PR PS PT PW PY QA RE RO RS RU RW SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ TC TD TF TG TH TJ TK TL TM TN TO TR TT TV TW TZ UA UG UM US UY UZ VA VC VE VG VI VN VU WF WS XK YE YT ZA ZM ZW'.split(' ')
let names: Intl.DisplayNames | null = null
export function countryName(code: string) {
  try { names ??= new Intl.DisplayNames([navigator.language, 'en'], { type: 'region' }); return names.of(code) ?? code } catch { return code }
}
export const countries = () => CODES.map((code) => ({ code, name: countryName(code) })).sort((a, b) => a.name.localeCompare(b.name))

export function placeLabel(p?: Place) {
  if (!p) return ''
  return p.name ? `${p.name}, ${countryName(p.country)}` : countryName(p.country)
}
