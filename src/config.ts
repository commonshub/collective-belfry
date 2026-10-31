// Belfry endpoints. Everything Belfry-specific lives here so the native apps
// can share the same values.
export const BELFRY = {
  apps: 'https://apps.belfry.opensourcevillage.org',
  groupsRelay: 'wss://groups-relay.belfry.opensourcevillage.org', // NIP-29 (zooid)
  mainRelay: 'wss://relay.belfry.opensourcevillage.org', // profiles, calendar (rnostr)
  blossom: 'https://blossom.belfry.opensourcevillage.org',
  bunkerRelay: 'wss://bunker.belfry.opensourcevillage.org',
}

// "demo" runs everything on this device (any 6-digit code, any invite code),
// "belfry" talks to Belfry. Switch with ?backend=belfry|demo (remembered).
export type Backend = 'demo' | 'belfry'

export function getBackend(): Backend {
  const q = new URLSearchParams(location.search).get('backend')
  if (q === 'demo' || q === 'belfry') {
    try { localStorage.setItem('osv-backend', q) } catch { /* private mode */ }
    return q
  }
  try {
    const v = localStorage.getItem('osv-backend')
    if (v === 'demo' || v === 'belfry') return v
  } catch { /* private mode */ }
  return (import.meta.env.VITE_BACKEND as Backend) || 'demo'
}

export const CLIENT_TAG = ['client', 'osv-village']
export const LABEL_NS = 'osv/member-label'
export const TZ = 'Europe/Brussels'
