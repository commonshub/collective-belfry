# The app on Belfry

Sign-in follows the Belfry skill (`commonshub/collective-belfry`, `skills/open-source-village/SKILL.md`):

- **Section 9, "Sign in with Open Source Village"**: OAuth code + PKCE against Keycast
  (`https://apps.belfry.opensourcevillage.org`), client id `osv-village-app`, redirect back to
  the app's own page. Belfry's sign-in page asks for the pass's e-mail and the mailed 6-digit
  code; the token response's `bunker_url` signs for the participant's key (policy
  `belfry-event`). Sign-out revokes the token and ends the Keycast session.
- **Membership**: `GET https://pass.…/api/me` (NIP-98). Belfry answers browsers only from
  origins in `API_CORS_ORIGINS`; until `https://xavierdamman.com` (and later the app's own
  origin) is listed, the app falls back to the relays, which refuse keys without an active pass,
  and shows "Membership pending" then.
- **Invite codes** open the shop's invite page (`https://shop.…/belfry/invite/<code>`), which
  shows the passes and takes the payment; the participant then signs in.
- **Sessions** (section 7): kind 31923 `d=session-<x>` on the community relay with
  `["h", "ws-<x>", <groups relay>]`, a NIP-29 group `ws-<x>`, a first message in it with the
  session's `a` tag. RSVP `d` = first 30 hex of SHA-256 of `<me>:<author>:<d>`.

## Still needed from Belfry

1. `API_CORS_ORIGINS` to include the app's origin (for `/api/me` and later `/api/invites`).
2. Kinds **2100–2103** (tasklist.sh) in the `belfry-event` policy (`infra/keycast/policy.sql`),
   and the groups relay accepting them with an `h` tag. Until then channel tasks can't be signed.
