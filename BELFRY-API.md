# What the chat asks of a belfry

Hosts are derived from the domain (`src/config.ts`). With the Belfry look:

| What | Where |
|---|---|
| Sign-in (OAuth code + PKCE, client id `osv-village-app`; the token answer carries a `bunker_url`) | `belfry.<domain>/connect/{authorize,token,revoke,logout}` |
| Signing (NIP-46) | `wss://bunker.<domain>` |
| Channels (NIP-29) | `wss://groups-relay.<domain>` |
| Profiles, calendar, proposals, gift wraps | `wss://relay.<domain>` |
| Files (BUD-02 upload, kind 24242 auth) | `blossom.<domain>/upload` |
| Discovery: community name, admins' keys | `<domain>/.well-known/belfry.json` |
| Bridge keys: `_smtp` (e-mail), `_groups` (e-mail cards); member lookup | `<domain>/.well-known/nostr.json?name=…` |
| API, NIP-98 signed | `belfry.<domain>/api/v1` |

API calls the chat makes (all NIP-98 except the two marked public):

- `GET /channel-invites/<token>` (public), `POST /channel-invites/<token>/join`,
  `POST /channel-invites` (make a guest link)
- `POST /guest/save`, `POST /guest/save/verify` (a guest adds an e-mail)
- `GET /me/sessions`, `POST /me/sessions/revoke`
- `GET|POST /me/follows` (announcements by e-mail)
- `POST /username`, `POST /admins`
- `GET /places?q=` (public), `POST /places`
- `POST /website/publish`

The Open Source Village look keeps the earlier endpoints: sign-in on Keycast
(`apps.<domain>/api/oauth/*`) and the API at `pass.<domain>/api`.

Whether a key is in is what the groups relay says: a member's (or a channel guest's)
subscription is answered, anyone else's is closed with `restricted:`.

## Not served by a belfry today: access as Nostr events

`src/core/access.ts`, `src/ui/Admin.tsx` and the "invite code" and "ask to join" parts of
`src/ui/Login.tsx` implement access as Nostr events (kinds 8534 to 8538, a service key in
`belfry.json` under `access`; collective-belfry PR #2, not in the code belfries run). No belfry publishes
`access` in `belfry.json`, so the chat only reads `admins` from it; a key that is not in sees
"Apply to join" (`https://<domain>/apply`). Membership is handled by the belfry's own register
and invite pages instead.

## Event shapes

The native apps' `SPEC.md` (belfry-ios) is the written reference for the kinds and tags; it is
derived from `src/core/model.ts` and `src/core/actions.ts`.
