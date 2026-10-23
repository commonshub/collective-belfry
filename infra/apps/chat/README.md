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
| Build args | `CHAT_PUBLIC_URL`, `COMMUNITY_RELAY_URL`, `BUNKER_PUBLIC_RELAYS`, `BLOSSOM_PUBLIC_URL` from `.env` (local defaults: `http://localhost:3300`, `ws://localhost:3090`, `ws://localhost:3080`, `http://localhost:3100`; `prod-env.sh` sets the `https`/`wss` hosts). A change needs `docker compose build chat`. |
| Settings compiled in | Default, indexer and search relays = the community relay; signer relays = the bunker relay; Blossom = Belfry's; no default follows, onboarding lists, zaps, market or DVM relays; name "Open Source Village"; the OSV palette as both themes. |
| Compose | service `chat` (`--profile app`), host port 3300 locally, none in production; Caddy `chat.<domain>` → `chat:80`. |

**Signing in.** Participants paste a `bunker://` URL from the apps dashboard (`apps.<domain>`,
"Connect to Nostr App" → copy the bunker URL) into Coracle's "Login with bunker" screen. Keycast's
`belfry-event` policy (`infra/keycast/policy.sql`) lists the kinds Coracle signs; NIP-44 for the
messages is not kind-gated. The NIP-05 "handle" login of other clients does not apply: Keycast
authorizes per app and needs the bunker URL's secret.

**Checks.** `make smoke` step 6j fetches the app and checks the compiled-in relay URLs;
`infra/scripts/chat-login-check.mjs` drives a headless browser through the bunker login (see the
script's header).
