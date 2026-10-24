// The electorate: active passes and their Nostr keys, from the onboarding service
// (GET /admin/active-passes, bearer PASS_INFO_TOKEN; Odoo is the source of truth).
const TTL_MS = 30_000

export function onboardingPasses({ baseUrl, token, fetchImpl = fetch }) {
  let cache = null
  let cachedAt = 0
  return {
    /** Map pubkey (hex) -> pass id of every active pass. */
    async active() {
      if (cache && Date.now() - cachedAt < TTL_MS) return cache
      const res = await fetchImpl(`${baseUrl.replace(/\/$/, "")}/admin/active-passes`, {
        headers: { Authorization: `Bearer ${token}` },
        signal: AbortSignal.timeout(5000),
      })
      if (!res.ok) throw new Error(`onboarding answered ${res.status}`)
      const { passes } = await res.json()
      cache = new Map(passes.map(p => [p.pubkey, p.pass_id]))
      cachedAt = Date.now()
      return cache
    },
  }
}
