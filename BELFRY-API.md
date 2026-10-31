# What the village app needs from Belfry

The app runs today in **demo** mode (everything on the phone). To run against Belfry
(`?backend=belfry`) it needs the endpoints below. Everything else it uses already exists.

## Already there (found by reading Belfry's own apps, 2026-10-31)

| What | Where | Used for |
|---|---|---|
| NIP-29 groups relay (zooid), NIP-42 auth, members = active passes | `wss://groups-relay.belfry.opensourcevillage.org` | channels, messages (kind 9), tasks (2100–2103), member labels (1985) |
| Main relay (rnostr), NIP-42 | `wss://relay.belfry.opensourcevillage.org` | profiles (0), calendar (31922/31923), RSVPs (31925) |
| Blossom, BUD-02 upload with kind 24242 auth | `https://blossom.belfry.opensourcevillage.org/upload` | photos and files |
| NIP-46 bunker holding members' keys | `wss://bunker.belfry.opensourcevillage.org` | signing |
| Browser OAuth (PKCE) returning `{ bunker_url, refresh_token }` | `apps.belfry…/api/oauth/authorize`, `/api/oauth/token`, client ids `belfry-chat`, `belfry-calendar`, scope `policy:belfry-event` | how Belfry's apps sign in today |

## Missing: in-app e-mail + code sign-in

The OAuth flow sends the user to Belfry's web page, which can't give the app its own
e-mail and 6-digit screens (and is awkward in a native app). Proposed, under
`https://apps.belfry.opensourcevillage.org`, CORS open to the app's origins:

```
POST /api/app/login/start   { email, client_id: "osv-village" }              → 204
     Sends the same e-mail as today: link + 6-digit code, 10 minutes. Always 204 (no account enumeration).

POST /api/app/login/verify  { email, code, client_id, client_pubkey }        → 200
     { token, membership, bunker_url? }
     token:       opaque app token (long-lived, revocable), for the calls below
     membership:  { status: "active" | "pending" | "revoked", pass?: "Two weeks · 25 Jan – 5 Feb 2027", member?: "OSV-0042" }
     bunker_url:  bunker://<signer>?relay=wss://bunker.belfry…&secret=…  only when status is active;
                  authorised for client_pubkey with the same policy as policy:belfry-event
     400 { error: "That code is wrong or expired." }   (rate-limit per e-mail and per IP)

GET  /api/app/membership    Authorization: Bearer <token>                    → { membership }

POST /api/app/invite        Authorization: Bearer <token>  { code }          → { membership, bunker_url? }
     Redeems an invite code for this e-mail. If the invite needs payment first, return
     { membership: { status: "pending" }, pay_url } and the app opens pay_url.

POST /api/app/logout        Authorization: Bearer <token>                    → 204 (revokes token + bunker grant)
```

"Pending" means: signed in with an e-mail that has no paid pass (yet). Today Belfry only
creates accounts once a pass is paid, so this needs a lightweight "e-mail known, no pass"
state, or `verify` returns 403 `{ status: "none" }` and the app shows the invite screen.

## Signing policy

The bunker policy for this client must allow kinds:
`9, 5, 9007, 9002, 1985, 2100, 2101, 2102, 2103, 31922, 31923, 31925, 22242 (relay auth), 24242 (Blossom auth)`.

## Questions

- Can participants create groups (kind 9007) on the groups relay? The Groups app says "anyone can open one".
- Does zooid accept the tasklist kinds (2100–2103) and labels (1985) with an `h` tag?
- Should a calendar event's chat be a NIP-29 group `ev-<d>` (what the app does) or something Belfry creates?
- Push notifications: does Belfry plan a push bridge (Flotilla's `VITE_PUSH_SERVER` is empty)?
