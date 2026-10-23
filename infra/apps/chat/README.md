# Community chat: Coracle

`chat.<domain>` is [Coracle](https://github.com/coracle-social/coracle) (MIT, Svelte), chosen for
its NIP-17 private messages (one to one and small groups, gift-wrapped, NIP-44) on top of the usual
notes, reactions and profiles. It is a static build, pinned to one upstream commit, with Belfry's
relays and the Open Source Village look compiled in; nothing of Belfry's runs behind it. The
browser talks to the community relay (`relay.<domain>`, NIP-42 AUTH with the pass whitelist), the
signer's relay (`bunker.<domain>`, NIP-46) and Blossom (`blossom.<domain>`) directly.

| Piece | Where |
| --- | --- |
| Image | `Dockerfile`: clones upstream at `CORACLE_COMMIT`, copies the brand logo and wordmark, appends `osv.css` (dark text on the lime accent), renders `.env` with `make-env.sh` from the build args, runs upstream's `build:web`, serves `dist/` with upstream's `nginx.conf`. |
| Build args | `CHAT_PUBLIC_URL`, `COMMUNITY_RELAY_URL`, `BUNKER_PUBLIC_RELAYS`, `BLOSSOM_PUBLIC_URL`, `KEYCAST_APP_URL` from `.env` (local defaults: `http://localhost:3300`, `ws://localhost:3090`, `ws://localhost:3080`, `http://localhost:3100`; `prod-env.sh` sets the `https`/`wss` hosts). A change needs `docker compose build chat`. |
| Settings compiled in | Default, indexer and search relays = the community relay; signer relays = the bunker relay; Blossom = Belfry's; no default follows, onboarding lists, zaps, market or DVM relays; name "Open Source Village"; the OSV palette as both themes. |
| Compose | service `chat` (`--profile app`), host port 3300 locally, none in production; Caddy `chat.<domain>` → `chat:80`. |

**Signing in.** Patch `patches/0001-…` (applied by the Dockerfile) adds single sign-on through
Keycast (`src/belfry/sso.ts`): OAuth authorization code + PKCE, client `belfry-chat`, redirect
`<CHAT_PUBLIC_URL>/oauth/callback`; the token response's `bunker://` URL is connected like the bunker
screen does. On load without a session the app redirects once per tab with `prompt=none`
(sessionStorage flag): a participant with a Keycast or Authentik session comes back signed in
without a click (Keycast patch 0008; Belfry's apps skip the consent screen). Otherwise the login
screen opens with "Sign in with Open Source Village" first, which goes straight to Authentik's
sign-in page (magic link or pass). Build arg `KEYCAST_APP_URL` (empty turns both off). After
logging out, a new tab signs in silently again while the Authentik session lasts (14 days).
Participants can still paste a `bunker://` URL from the apps dashboard (`apps.<domain>`,
"Connect to Nostr App" → copy the bunker URL) into Coracle's "Login with bunker" screen. Keycast's
`belfry-event` policy (`infra/keycast/policy.sql`) lists the kinds Coracle signs; NIP-44 for the
messages is not kind-gated. The NIP-05 "handle" login of other clients does not apply: Keycast
authorizes per app and needs the bunker URL's secret.

**Checks.** `make smoke` step 6j fetches the app and checks the compiled-in relay URLs;
`infra/scripts/chat-login-check.mjs` drives a headless browser through the bunker login, and with
`-` as the bunker URL through the single sign-on: with `COOKIE_JAR` (an Authentik session,
`SAVE_AK_JAR=<file> make smoke`) the app must sign in silently, without it the login screen must
offer the button (see the script's header). Smoke step 6l checks the Keycast side with curl.
