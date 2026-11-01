// Belfry endpoints. Everything Belfry-specific lives here so the native apps
// can share the same values.
export const BELFRY = {
  domain: 'belfry.opensourcevillage.org',
  apps: 'https://apps.belfry.opensourcevillage.org', // Keycast: sign-in, bunker
  pass: 'https://pass.belfry.opensourcevillage.org', // /api/me, /api/invites
  shop: 'https://shop.belfry.opensourcevillage.org', // invite pages and payment
  groupsRelay: 'wss://groups-relay.belfry.opensourcevillage.org', // NIP-29 (zooid)
  mainRelay: 'wss://relay.belfry.opensourcevillage.org', // profiles, calendar (rnostr)
  blossom: 'https://blossom.belfry.opensourcevillage.org',
  bunkerRelay: 'wss://bunker.belfry.opensourcevillage.org',
}

// "belfry" is the real thing. "demo" keeps everything on this device and is only
// for automated tests: ?backend=demo (remembered until ?backend=belfry).
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
  return 'belfry'
}

export const CLIENT_TAG = ['client', 'osv-village']
export const LABEL_NS = 'osv/member-label'
export const TZ = 'Europe/Brussels'
