# The app on Belfry

Sign-in follows the Belfry skill (`commonshub/collective-belfry`, `skills/open-source-village/SKILL.md`):

- **Section 9, "Sign in with Open Source Village"**: OAuth code + PKCE against Keycast
  (`https://apps.belfry.opensourcevillage.org`), client id `osv-village-app`, redirect back to
  the app's own page. Belfry's sign-in page asks for the e-mail address and the mailed 6-digit
  code; the token response's `bunker_url` signs (and NIP-44 encrypts) for the person's key.
  Sign-out revokes the token and ends the Keycast session.
- **Access, section 11** (Belfry PR #2): no API calls and no CORS. The only HTTP read is
  `https://belfry.opensourcevillage.org/.well-known/belfry.json` (`access`: the service key,
  `admins`, `needs_admin`). Whether a key is in is what the relays say (a member's REQ is
  answered, anyone else's is `CLOSED restricted:`).
  - Not in yet: become the first admin (only while `needs_admin`), use an invite code or ask with
    a note. All three are a kind 8537 rumor, gift-wrapped (NIP-59) to the service key on the
    groups relay. An invite code typed before signing in (or `?invite=<code>`) is kept and sent
    after sign-in. The app asks the relays again every few seconds until the key is in.
  - Admins (`#/admin`): requests (Belfry's 8538 receipts; the notes come from the DMs Belfry sent
    the admin, unwrapped with the bunker), members (8534 grants, newest grant or revoke per key)
    and invite codes (8536 with the code's SHA-256; the code itself stays on the device that
    made it). Grants, declines and withdrawals are signed by the admin. `#/admin/requests/<id>`
    is the link in Belfry's DM (`ACCESS_APP_URL`).
- **Sessions** (section 7): kind 31923 `d=session-<x>` on the community relay with
  `["h", "ws-<x>", <groups relay>]`, a NIP-29 group `ws-<x>`, a first message in it with the
  session's `a` tag. RSVP `d` = first 30 hex of SHA-256 of `<me>:<author>:<d>`.

## Still needed from Belfry

1. PR #2 (access as Nostr events) merged and deployed. Until then the live `belfry.json` has no
   `access` and the app shows "Apply to join" to keys that are not in.
2. PR #1 (the signer signs any kind) for tasks (2100–2103) and the access kinds, and the groups
   relay accepting the task kinds with an `h` tag.

## Testing against a local relay

A test build can point elsewhere: `VITE_GROUPS_RELAY`, `VITE_MAIN_RELAY`, `VITE_DISCOVERY`.
`~/agents/osv/.shots/access-flow.cjs` drives three browsers (local keys injected as the session)
through first admin, request + approval, and invite code, against zooid with Belfry's patch and
Belfry's access service.
