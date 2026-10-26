# Groups: Flotilla

`groups.<domain>` is [Flotilla](https://gitea.coracle.social/coracle/flotilla) (MIT, SvelteKit),
a Discord-like Nostr client built on NIP-29 groups, in platform mode on Belfry's groups relay
(zooid, `infra/groups-relay`): the village is the one space, its rooms are groups that any
participant can open. It is a static build, pinned to one upstream commit, served by nginx;
nothing of Belfry's runs behind it. The browser talks to the groups relay, the community relay
(profiles, relay lists, private messages), the signer's relay (`bunker.<domain>`, NIP-46) and
Blossom directly.

| Piece | Where |
| --- | --- |
| Image | `Dockerfile`: clones upstream at `FLOTILLA_COMMIT`, applies `patches/`, copies the groups icon (`brand/apps/groups-maskable-512.png`) as the platform logo, appends `osv.css`, adds the app bar to `src/app.html`, renders `.env` with `make-env.sh`, runs upstream's `scripts/build/web.sh` and fails the build if a third-party host is left in it; nginx (`nginx.conf`) serves `build/` with the SPA fallback. |
| Build args | `GROUPS_PUBLIC_URL`, `GROUPS_RELAY_URL`, `COMMUNITY_RELAY_URL`, `BUNKER_PUBLIC_RELAYS`, `BLOSSOM_PUBLIC_URL`, `KEYCAST_APP_URL`, `SITE_URL`, `COMMUNITY_APPS`. A change needs `docker compose build groups`. |
| Settings compiled in | Platform relay = the groups relay; default, indexer and messaging relays = the community relay; signer relays = the bunker relay; Blossom = Belfry's; no default follows or spaces, no push server or bridge, no link-preview proxy, no thumbnails, no Pomade e-mail signers, no hosting backend; the village palette (`osv.css`, Flotilla's `nature` tokens set to the OSV colours in light and dark). |
| Compose | service `groups` (`--profile app`), host port 3150 locally, none in production; Caddy `groups.<domain>` → `groups:80`. Keycast client `belfry-groups` (bootstrap, trusted). |

**Patches.**

- `0001-…` no third-party services: the Plausible script and its CSP entry go; the dufflepud
  proxy comes from `VITE_DUFFLEPUD_URL` (empty: no link previews, no read-marker sync, NIP-05
  fetched directly); an empty push bridge no longer stops the app; with
  `VITE_MEMBERS_CREATE_ROOMS=true` every member of the space sees "Create room" (the relay
  decides, see the groups relay's patch).
- `0002-…` "Sign in with Open Source Village" (`src/belfry/sso.ts`, as in the chat): OAuth code +
  PKCE against Keycast, the token response's bunker URL over NIP-46, a silent `prompt=none` try
  once per tab before the app starts, the login dialog when it does not work out; logout revokes
  the app's authorization and ends the sign-in everywhere (Keycast patch 0010).
- `0003-…` the village space joins itself (no "Join Space" dialog; it stays as the fallback when
  access fails), no notifications toggle without a push server, the landing page offers the
  village sign-in first and "No pass yet? Ask for an invite" (`SITE_URL`) instead of creating an
  account.
- `0004-…` AUTH is always answered on the build's own relays: Flotilla answers only relays from
  the user's lists, which a new device cannot read from the AUTH-only community relay.

**Checks.** Smoke step 6u (app, bundle, relay NIP-11, the participant on the member list, the
group kinds signed; step 11: a revoked pass leaves the relay). `infra/scripts/groups-check.mjs`
in Chromium (`WIDTH=390` for a phone): silent sign-in with the participant's Authentik session
(`SAVE_AK_JAR` from the smoke test), the space with its members, a room created by the
participant who then administers it ("Create invite"), a message posted and read back after a
reload.

**Not done.** Push notifications (Flotilla's push server and bridge are not run); voice and
video rooms (no LiveKit); stewards are not admins of every room. Locally (plain `ws://localhost`
relays) Flotilla does not read profiles from the community relay, so members show as npubs; the
public `wss://` deployment is where names are checked.
