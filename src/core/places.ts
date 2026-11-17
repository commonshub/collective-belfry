// The town's places (iOS SPEC 19.31, the owner's canvas "One belfry, five places", 2026-11-16).
// Display only: ids, mail addresses, links and wire values never change.
//  - On-brand names of the default channels, only while the group's own name is still its
//    default (empty, its id, or "#" + id, any case): general → Square, events → Fairground,
//    proposals → Assembly, website → Scriptorium. A renamed channel keeps its name.
//  - The members' directory with its requests is "Members" (it was "Gates" until 2026-11-17).
//  - Roles read Steward (the built-in admin) · Member · Guest; the per-event / per-group check-in
//    role (wire value `steward`) reads "Door".
import { THEME } from '../config'

export const PLACE_NAMES: Record<string, string> = { general: 'Square', events: 'Fairground', proposals: 'Assembly', website: 'Scriptorium' }
export const MEMBERS = 'Members'
export const STEWARD = 'Steward'
export const DOOR = 'Door'
/** The `charter` group (19.32): never listed as a channel or a group. */
export const CHARTER_ID = 'charter'

/** On-brand names are on in the Belfry look (the OSV look keeps its own words). */
export const placeNamesOn = () => THEME === 'belfry'

/** The name to show instead of NAME, or undefined to keep it. */
export function placeName(id: string, name: string): string | undefined {
  if (!placeNamesOn()) return undefined
  const n = PLACE_NAMES[id]
  if (!n) return undefined
  const raw = name.trim().toLowerCase()
  return raw === '' || raw === id || raw === '#' + id ? n : undefined
}

/** What to write for a name typed or kept in a form: never "Square" on the wire for #general. */
export const wireName = (id: string, name: string) => (PLACE_NAMES[id] && PLACE_NAMES[id] === name.trim() ? id : name)

/** The drawer's fixed section, The town (the Square, Members, the Charter); the other channels
 *  sit in categories (core/order.ts: Community life, Building together, and the admins' own). */
export type Section = 'town' | 'life' | 'building'
export const SECTION_TITLE: Record<Section, string> = { town: 'The town', life: 'Community life', building: 'Building together' }
export const sectionOf = (id: string): Section => (id === 'general' ? 'town' : id === 'proposals' || id === 'website' ? 'building' : 'life')
