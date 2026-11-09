// Belfry endpoints. Everything Belfry-specific lives here so the native apps
// can share the same values. A test build may point the relays and the discovery
// document elsewhere (VITE_GROUPS_RELAY, VITE_MAIN_RELAY, VITE_DISCOVERY).
const env = (import.meta as unknown as { env: Record<string, string | undefined> }).env

// The belfry this app belongs to: VITE_BELFRY_DOMAIN at build time, else the host it is served
// from: <domain>/<app> (a belfry's own apps, the Belfry look) or apps.<domain>/<app> (before
// they moved to the community's domain), else Open Source Village.
const host = typeof location === 'undefined' ? '' : location.hostname
const servedByBelfry = env.VITE_THEME === 'belfry' && host.includes('.') && !/^[\d.]+$/.test(host) && host !== 'localhost'
const DOMAIN =
  env.VITE_BELFRY_DOMAIN ||
  (host.startsWith('apps.') ? host.slice(5) : servedByBelfry ? host : 'belfry.opensourcevillage.org')

// "osv" (the village look, default) or "belfry" (the Belfry look: Clean, the arms mark).
export const THEME: 'osv' | 'belfry' = env.VITE_THEME === 'belfry' ? 'belfry' : 'osv'
export const NAME = THEME === 'belfry' ? DOMAIN : 'Open Source Village'

export const BELFRY = {
  domain: DOMAIN,
  apps: `https://apps.${DOMAIN}`, // Keycast: sign-in, bunker
  pass: `https://pass.${DOMAIN}`, // the pass at the door (/me)
  shop: `https://shop.${DOMAIN}`, // invite pages and payment
  groupsRelay: env.VITE_GROUPS_RELAY || `wss://groups-relay.${DOMAIN}`, // NIP-29 (zooid)
  mainRelay: env.VITE_MAIN_RELAY || `wss://relay.${DOMAIN}`, // profiles, calendar (rnostr)
  blossom: `https://blossom.${DOMAIN}`,
  bunkerRelay: `wss://bunker.${DOMAIN}`,
}

// Where "Sign in with <domain>" happens (OAuth code + PKCE). The Belfry look uses the belfry's
// own sign-in page (belfry.<domain>/connect: approved apps only); the village look keeps the
// signer's (Keycast) until its belfry has that page too. VITE_SIGNIN=connect|keycast overrides.
const CONNECT = (env.VITE_SIGNIN || (THEME === 'belfry' ? 'connect' : 'keycast')) === 'connect'
export const SIGNIN = CONNECT
  ? {
      authorize: `https://belfry.${DOMAIN}/connect/authorize`,
      token: `https://belfry.${DOMAIN}/connect/token`,
      revoke: `https://belfry.${DOMAIN}/connect/revoke`,
      logout: `https://belfry.${DOMAIN}/connect/logout`,
    }
  : {
      authorize: `${BELFRY.apps}/api/oauth/authorize`,
      token: `${BELFRY.apps}/api/oauth/token`,
      revoke: `${BELFRY.apps}/api/oauth/revoke`,
      logout: `${BELFRY.apps}/api/auth/oidc/logout`,
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

// Access as Nostr events (Belfry skill, section 11): grant, revoke, invite, request (a gift-wrapped
// rumor, never published as such), receipt. The read-only state is in belfry.json.
export const ACCESS = { grant: 8534, revoke: 8535, invite: 8536, request: 8537, receipt: 8538 }
export const ACCESS_KINDS = [ACCESS.grant, ACCESS.revoke, ACCESS.invite, ACCESS.receipt]
export const DISCOVERY = env.VITE_DISCOVERY || `https://${BELFRY.domain}/.well-known/belfry.json`

export const CLIENT_TAG = ['client', 'osv-village']
export const LABEL_NS = 'osv/member-label'
export const TZ = 'Europe/Brussels'
