// The belfry's branding: logo, cover image, primary and secondary colour (Xavier, 2026-11-11).
// Admins set them (Branding screen, POST {api}/branding); Belfry keeps them in the kind-0 profile
// of the belfry's own key and lists them in belfry.json "branding", which loadAccessInfo reads.
// Applied at once from the last copy on this device (localStorage), so the colours don't flash.
import { BELFRY, THEME, getBackend } from '../config'
import { useEffect, useState } from 'react'
import { signedFetch } from './sync'

// description: the line under the community's name on its homepage (the belfry profile's about,
// plain text, at most 500; "" = the default line)
export interface Branding { logo: string; cover: string; primary: string; secondary: string; description: string }
const EMPTY: Branding = { logo: '', cover: '', primary: '', secondary: '', description: '' }
const KEY = 'osv-branding'
const HEX = /^#[0-9a-f]{6}$/i
// https only (data:image/… is the demo's uploads, on this device)
const URL_OK = (u: unknown): u is string => typeof u === 'string' && (/^https:\/\/[^\s"<>]+$/.test(u) || /^data:image\/(png|jpeg|webp|gif);base64,[A-Za-z0-9+/=]+$/.test(u))

let brand: Branding = read()
const listeners = new Set<() => void>()

function read(): Branding {
  try { return clean(JSON.parse(localStorage.getItem(KEY) || '{}')) } catch { return { ...EMPTY } }
}

function clean(b: Partial<Record<keyof Branding, unknown>> | undefined): Branding {
  return {
    logo: URL_OK(b?.logo) ? b!.logo as string : '',
    cover: URL_OK(b?.cover) ? b!.cover as string : '',
    primary: typeof b?.primary === 'string' && HEX.test(b.primary) ? b.primary.toLowerCase() : '',
    secondary: typeof b?.secondary === 'string' && HEX.test(b.secondary) ? b.secondary.toLowerCase() : '',
    description: typeof b?.description === 'string' ? b.description.slice(0, 500) : '',
  }
}

export const branding = () => brand
export function onBranding(l: () => void) { listeners.add(l); return () => { listeners.delete(l) } }
/** The branding, re-rendering when it changes. */
export function useBranding(): Branding {
  const [, setTick] = useState(0)
  useEffect(() => onBranding(() => setTick((t) => t + 1)), [])
  return brand
}

/** belfry.json "branding" (absent: none set). */
export function setBranding(raw: unknown, save = true) {
  brand = clean(raw as Partial<Branding>)
  if (save) try { localStorage.setItem(KEY, JSON.stringify(brand)) } catch { /* private mode */ }
  applyBranding()
  listeners.forEach((l) => l())
}

/** Text on a colour: white or near-black, whichever reads better. */
export function onColor(hex: string): string {
  const n = parseInt(hex.slice(1), 16)
  const lin = (c: number) => { c /= 255; return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4 }
  const L = 0.2126 * lin(n >> 16) + 0.7152 * lin((n >> 8) & 255) + 0.0722 * lin(n & 255)
  return L > 0.4 ? '#09090b' : '#ffffff'
}

/** The colours as CSS variables (styles.css [data-brand]), the logo as the tab's icon. */
export function applyBranding(b: Branding = brand) {
  if (THEME !== 'belfry' || typeof document === 'undefined') return
  const root = document.documentElement
  if (b.primary) {
    root.dataset.brand = ''
    root.style.setProperty('--brand', b.primary)
    root.style.setProperty('--on-brand', onColor(b.primary))
  } else {
    delete root.dataset.brand
    root.style.removeProperty('--brand'); root.style.removeProperty('--on-brand')
  }
  if (b.secondary) {
    root.style.setProperty('--brand-2', b.secondary)
    root.style.setProperty('--on-brand-2', onColor(b.secondary))
  } else { root.style.removeProperty('--brand-2'); root.style.removeProperty('--on-brand-2') }
  if (b.logo) {
    let icon = document.querySelector<HTMLLinkElement>('link[rel=icon][data-brand]')
    if (!icon) { icon = Object.assign(document.createElement('link'), { rel: 'icon' }); icon.dataset.brand = ''; document.head.append(icon) }
    icon.href = b.logo
  } else document.querySelector('link[rel=icon][data-brand]')?.remove()
}

/** An admin changes it: any of the four ("" clears one). */
export async function saveBranding(change: Partial<Branding>): Promise<Branding> {
  if (getBackend() === 'demo') { setBranding({ ...brand, ...change }); return brand } // tests: this device only
  const res = await signedFetch(`${BELFRY.api}/branding`, 'POST', change)
  const body = (await res.json().catch(() => ({}))) as Partial<Branding> & { message?: string }
  if (!res.ok) throw new Error(body.message || 'The branding could not be saved just now.')
  setBranding(body)
  return brand
}

/** The branding as the belfry has it now (GET, anyone): belfry.json may not carry the description. */
export async function fetchBranding(): Promise<Branding | null> {
  if (getBackend() === 'demo') return brand
  try {
    const res = await fetch(`${BELFRY.api}/branding`)
    return res.ok ? clean(await res.json()) : null
  } catch { return null }
}
