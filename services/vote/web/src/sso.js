// "Sign in with Open Source Village" through Keycast's OAuth (authorization code + PKCE), as in
// the chat and the calendar: the token response's bunker URL connects the app over NIP-46.
// prompt=none once per tab on load; logout revokes the authorization and ends the Keycast and
// Authentik sessions (Keycast's /api/auth/oidc/logout), or the silent check would sign back in.
const KEYCAST_URL = (import.meta.env.VITE_KEYCAST_URL || "").replace(/\/$/, "")
const CLIENT_ID = import.meta.env.VITE_OAUTH_CLIENT_ID || "belfry-vote"
const SCOPE = "policy:belfry-event"
const PENDING_KEY = "belfry-sso-pending"
const TRIED_KEY = "belfry-sso-silent-tried"
const REFRESH_KEY = "belfry-sso-refresh-token"

export const SSO_CALLBACK_PATH = "/oauth/callback"
export const ssoEnabled = Boolean(KEYCAST_URL)

const b64url = bytes =>
  btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "")
const randomString = () => b64url(crypto.getRandomValues(new Uint8Array(32)))
const redirectUri = () => `${location.origin}${SSO_CALLBACK_PATH}`

function store(kind) {
  try {
    return kind === "session" ? sessionStorage : localStorage
  } catch {
    return null
  }
}

export async function startSso(silent) {
  if (!KEYCAST_URL) return
  const verifier = randomString()
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier))
  const pending = { state: randomString(), verifier, returnTo: location.hash || "#/" }
  store("session")?.setItem(PENDING_KEY, JSON.stringify(pending))
  const params = new URLSearchParams({
    client_id: CLIENT_ID,
    redirect_uri: redirectUri(),
    scope: SCOPE,
    code_challenge: b64url(new Uint8Array(digest)),
    code_challenge_method: "S256",
    state: pending.state,
  })
  if (silent) params.set("prompt", "none")
  location.assign(`${KEYCAST_URL}/api/oauth/authorize?${params}`)
}

/** True once per tab: try the silent sign-in now. */
export function shouldTrySilentSso() {
  const s = store("session")
  if (!ssoEnabled || !s || s.getItem(TRIED_KEY)) return false
  s.setItem(TRIED_KEY, "1")
  return true
}

/** On SSO_CALLBACK_PATH: check the state and exchange the code for the bunker URL. */
export async function finishSso(search) {
  const params = new URLSearchParams(search)
  const s = store("session")
  const raw = s?.getItem(PENDING_KEY)
  s?.removeItem(PENDING_KEY)
  const pending = raw ? JSON.parse(raw) : null
  const returnTo = pending?.returnTo || "#/"
  if (!pending || params.get("state") !== pending.state) return { ok: false, error: "state_mismatch", returnTo }
  if (params.get("error")) return { ok: false, error: params.get("error"), returnTo }
  const code = params.get("code")
  if (!code) return { ok: false, error: "no_code", returnTo }
  try {
    const res = await fetch(`${KEYCAST_URL}/api/oauth/token`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        grant_type: "authorization_code",
        code,
        client_id: CLIENT_ID,
        redirect_uri: redirectUri(),
        code_verifier: pending.verifier,
      }),
    })
    const body = await res.json().catch(() => ({}))
    if (!res.ok || typeof body.bunker_url !== "string") {
      return { ok: false, error: body.error || `token_${res.status}`, returnTo }
    }
    if (typeof body.refresh_token === "string") store("local")?.setItem(REFRESH_KEY, body.refresh_token)
    return { ok: true, bunkerUrl: body.bunker_url, returnTo }
  } catch {
    return { ok: false, error: "token_request_failed", returnTo }
  }
}

/** After the app's own logout: revoke and end the single sign-on session (navigates away). */
export async function endSsoSession() {
  if (!KEYCAST_URL) return
  const refreshToken = store("local")?.getItem(REFRESH_KEY)
  store("local")?.removeItem(REFRESH_KEY)
  if (refreshToken) {
    try {
      await fetch(`${KEYCAST_URL}/api/oauth/revoke`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token: refreshToken, client_id: CLIENT_ID }),
        signal: AbortSignal.timeout(3000),
      })
    } catch (e) {
      console.warn("Revoking the Open Source Village authorization failed", e)
    }
  }
  store("session")?.setItem(TRIED_KEY, "1")
  location.assign(`${KEYCAST_URL}/api/auth/oidc/logout?${new URLSearchParams({ return_to: `${location.origin}/` })}`)
}
