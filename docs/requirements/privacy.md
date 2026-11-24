# Privacy and security rules

Prefix `PRIV`. Rules about what never shows, never leaves, never gets fetched. Bind every part.

## What pages render

- **PRIV-1** [all] Never render text that comes from the URL. "Never ever render directly
  something from the url, remember that." (owner, 2026-11-05) Redirects carry only enumerated
  keys mapped to fixed text; unknown keys render nothing. Messages after a post ride in a signed
  cookie, not the query string.
- **PRIV-2** [apps] A link gives the app only its domain and token / id; server errors are mapped
  to fixed words by code (UI-18).
- **PRIV-3** [all] Anything people wrote is escaped in HTML mails and pages and never put in a
  URL.

## Personal data

- **PRIV-10** [all] No personal data of community members on any website, screenshot, canvas
  board, test fixture or log; use the apps' demo mode and demo people. (owner, 2026-11-01)
- **PRIV-11** [all] Never print full e-mail addresses in logs; mail logs mask them; a logged
  subject never contains a sign-in code when no mail provider is set (to confirm: flagged
  2026-11-20 as an open risk).
- **PRIV-12** [all] Profile visibility: listed (name, picture, headline public; the rest for
  villagers) | private | public. New accounts start listed; profiles that existed before
  2026-11-21 were made private once (to confirm: the owner was asked to confirm both defaults).
  → docs/website-accounts.md §2b
- **PRIV-13** [all] An event's public activity shows names only with consent; no amounts.
- **PRIV-14** [all] Others' device details are never shown (a count only). → ENC-12
- **PRIV-15** [all] Cc'd outsiders of an e-mail are addresses only: no account, no guest.

## Fetching

- **PRIV-20** [apps] Readers never fetch outside pages or pictures for a message (it would tell the
  site who reads it, from where); previews are built by the sender. → MSG-50
- **PRIV-21** [server] Whatever the belfry fetches from outside (the web sender's preview, faces)
  is SSRF-guarded: https only, public addresses only after DNS, few redirects, size and time
  caps.
- **PRIV-22** [apps] Pictures in messages are loaded only from the belfry's own Blossom.
- **PRIV-23** [apps] Apps never contact Gravatar or any other service with an e-mail address or
  its hash; the belfry asks by hash and caches (`/api/v1/avatar/<sha256>`).

## Keys and secrets

- **PRIV-30** [all] "A private key should never leave the box/machine it has been created in."
  (owner, 2026-11-23) No key export, no key import from elsewhere, no "move to my own key". The
  one place a key travels is the encrypted backup of the signer's volume.
- **PRIV-31** [all] Never print secrets: check them by length or comparison; secret files stay in
  `.secrets/` / `~/.secrets` (700/600), never pasted, logged or committed. Setup links and codes
  are handed only to the owner.
- **PRIV-32** [all] The community's website never holds a signer session (no `bunker_url`); the
  server signs the allowed things for a website session.
- **PRIV-33** [all] Sign-out revokes the session at the belfry; Forget this device revokes every
  account's session on it and clears local data.
- **PRIV-34** [all] One client key, one identity, on every path (signer connect, app approval):
  a key linked to one account is never accepted for another. (owner, 2026-11-10)
- **PRIV-35** [server] Third-party Nostr apps signing with a member's key (remote apps) may never
  ask the belfry's own hosts or relays to authenticate, moderate groups, or sign Belfry's private
  settings and labels; decrypting needs a per-app consent. Members only, Android only for now.
  (owner, 2026-11-20) → docs/remote-apps.md
- **PRIV-36** [server] The relay surface for unknown keys stays minimal: only keys vouched for by
  an existing identity or a new sign-up get on the whitelist. (owner, 2026-11-21)

## Admin actions

- **PRIV-40** [server] Admin changes (who gave or took admin, when, from which client) must leave a
  kept record. (lead, 2026-11-22: no audit trail existed when two people became admins
  unexpectedly; to build.)
- **PRIV-41** [server] A first-come setup that a stranger claims is undone with `belfry
  setup-reset`; the owner keeps first come, first served (ONB-60).
