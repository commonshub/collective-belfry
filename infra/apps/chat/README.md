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
| App bar and home screen | `brand/osv-bar.js` is copied to `public/` and `add-app-bar.mjs` adds its tag to `index.html` (`KEYCAST_APP_URL` as home, `COMMUNITY_APPS` as links); `osv.css` points Coracle's `--safe-area-inset-top` at the bar's height (every fixed top part reads it) and puts the bar at z-index 3 (above the nav, below modals). The app's icon is `brand/apps/chat-maskable-512.png` as `public/images/logo.png`; favicons and PWA icons are generated from it with `pwa-assets.config.js` (no padding, no white background); the manifest says "Chat" with the bar's dark green. |
| Build args | `CHAT_PUBLIC_URL`, `COMMUNITY_RELAY_URL`, `BUNKER_PUBLIC_RELAYS`, `BLOSSOM_PUBLIC_URL`, `KEYCAST_APP_URL` (Keycast's name kept; Compose sets it to `ONBOARDING_PUBLIC_URL`) from `.env` (local defaults: `http://localhost:3300`, `ws://localhost:3090`, `ws://localhost:3080`, `http://localhost:3100`, `http://localhost:8100`; `prod-env.sh` sets the `https`/`wss` hosts). A change needs `docker compose build chat`. |
| Settings compiled in | Default, indexer and search relays = the community relay; signer relays = the bunker relay; Blossom = Belfry's; no default follows, onboarding lists, zaps, market or DVM relays; name "Open Source Village"; the OSV palette as both themes. |
| Compose | service `chat` (`--profile app`), host port 3300 locally, none in production; Caddy `chat.<domain>` → `chat:80`. |

**Signing in.** Patch `patches/0001-…` (applied by the Dockerfile) adds single sign-on through
Belfry's sign-in (`src/belfry/sso.ts`): OAuth authorization code + PKCE at the onboarding
service (`pass.<domain>/api/oauth/*`), client `belfry-chat`, redirect
`<CHAT_PUBLIC_URL>/oauth/callback`; the token response's `bunker://` URL is connected like the
bunker screen does. On load without a session the app redirects once per tab with `prompt=none`
(sessionStorage flag): a participant signed in on `pass.<domain>` comes back signed in without a
click (Belfry's own apps skip the consent screen). Otherwise the login screen opens with "Sign in
with Open Source Village" first, which goes to Belfry's login page (e-mail link and code, or the
pass). Build arg `KEYCAST_APP_URL` (empty turns both off): the patch kept Keycast's name, Compose
sets it to `ONBOARDING_PUBLIC_URL`, which serves the same paths Keycast did. Patch `0002-…`:
Logout revokes the app's connection (`/api/oauth/revoke` with the token response's refresh
token) and goes through `/api/auth/oidc/logout`, which ends the Belfry session and returns to
`/notes` signed out; without that, the silent check would sign the participant straight back in.
Patch `0003-…` puts "Log out" in the mobile menu (upstream hides it under the account name).
Participants can still paste a `bunker://` URL from the account page (`pass.<domain>/account`,
"Connect a Nostr app" → copy the bunker URL) into Coracle's "Login with bunker" screen. The
signer signs any kind for the participant's connections; NIP-44 for the messages works the same
way. The NIP-05 "handle" login of other clients does not apply: the signer authorizes per
connection and needs the bunker URL's secret.

**Checks.** `make smoke` step 6j fetches the app and checks the compiled-in relay URLs;
`infra/scripts/chat-login-check.mjs` drives a headless browser through the bunker login, and with
`-` as the bunker URL through the single sign-on: with `COOKIE_JAR` (the participant's Belfry
session, `SAVE_SESSION_JAR=<file> make smoke`) the app must sign in silently, without it the login
screen must offer the button, and with `LOGOUT=1` as well the logout must end in the signed-out
app with the Belfry session gone (see the script's header). Smoke steps 6d, 6l and 6m check the
sign-in side with curl.
