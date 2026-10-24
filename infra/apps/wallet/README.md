# Village token wallet

The Open Source Village wallet at `wallet.<domain>`: [cashu.me](https://github.com/cashubtc/cashu.me)
(MIT, Vue/Quasar PWA) at commit `002a0a36` plus six Belfry patches, built with the village mint
(`infra/mint/`) and served by Caddy. Decisions of 2026-10-24: tokens without monetary value, 50 per
pass day at activation; cashu.me at `wallet.<domain>` plus nutzaps; the wallet derives from the
participant's Nostr key.

| Patch | What it does |
| --- | --- |
| `0001-…` | With `BELFRY_MINT_URL` set: skips cashu.me's onboarding (seed phrase, mint choice; backup stays in the settings), adds the village mint and makes it active with the unit `osv`, shows that unit as "50 tokens", hides Lightning and on-chain for it (the mint refuses both), village theme (lime on green greys) by default. |
| `0002-…` | "Sign in with Open Source Village" (Keycast OAuth + PKCE, client `belfry-wallet`, silent `prompt=none` once per tab, never on a `#token=` link) and the account: the wallet seed and the nutzap key derive from the participant's Nostr key (Keycast `derive_secret`, patch 0011: purposes `cashu-wallet-v1`, first 16 bytes as BIP39 entropy, and `cashu-nutzap-v1`, normalised to an even Y). Tokens received before signing in are swapped onto the derived seed; a fresh device restores from the mint (NUT-09). Kind 10019 on the community relay announces the relay, the mint and the nutzap key; incoming kind 9321 nutzaps are redeemed automatically; "Send to a participant" (npub or NIP-05 name) locks tokens to the recipient's 10019 key and publishes a nutzap. Signing (10019, 9321, NIP-42 AUTH) goes over Keycast's HTTP RPC with the token response's access token (24 h, refreshed with the 30-day refresh token). Sign out revokes the authorization and ends the single sign-on (Keycast patch 0010). No bitcoin price, no mint audits, the community relay as the only Nostr relay. |
| `0003-…` | Pay requests from the other village apps: `/?pay=<npub>&amount=<n>&memo=…&ref=<kind:pubkey:d>&return=<url>` opens "Send to a participant" filled in once the account is ready (the query survives the silent sign-in); the nutzap carries `ref` as an `a` tag, so the marketplace sees what was paid for; after sending, the wallet goes back to `return` if it is on the same deployment (`*.<parent of the wallet's host>`). |
| `0004-…` | A pay request's `ref` may also be an event id (64 hex): the nutzap names it in an `e` tag (NIP-61), so the photos app sees tips for a picture. |
| `0005-…` | Receive and Send fit narrow phones (and Android's larger text): below 400px they drop upstream's 140px minimum and margins and leave room for the scan button, instead of running off both screen edges. |
| `0006-…` | Geist, self-hosted from `brand/` (the image build copies it to `/belfry/` and `brand.mjs` links it), instead of Inter from Google Fonts, which the deployment's CSP blocks. |

`brand.mjs` (run by the Dockerfile) sets the PWA name "Open Source Village wallet"/"Wallet", the
colours, the home-screen icons (`brand/apps/wallet-*`, from `brand/make-app-icons.py`) and the app bar
(`brand/osv-bar.js`).

| Build arg | Default (local) | Meaning |
| --- | --- | --- |
| `BELFRY_MINT_URL` | `http://localhost:3338` | the village mint as wallets reach it (`MINT_PUBLIC_URL`) |
| `BELFRY_KEYCAST_URL` | `http://localhost:3000` | Keycast (`KEYCAST_APP_URL`) |
| `BELFRY_COMMUNITY_RELAY` | `ws://localhost:3090` | the community relay (`COMMUNITY_RELAY_URL`) |
| `APPS_URL`, `COMMUNITY_APPS` | | the app bar's home link and app list (`x-community-apps`) |

Keycast side: `belfry-wallet` is a trusted, registered client (`KEYCAST_TRUSTED_CLIENTS`, bootstrap
registers `<WALLET_PUBLIC_URL>/oauth/callback`) and the only one allowed to derive
(`KEYCAST_DERIVE_SECRETS=belfry-wallet:cashu-wallet-v1|cashu-nutzap-v1`). The `belfry-event` policy
allows kinds 9321 and 10019.

**Bring your own key.** A participant who takes custody of their Nostr key keeps the wallet: the
derivation is HKDF-SHA256 with salt `keycast/derive/v1` and the purpose as info, for example in
Python `hmac.new(hmac.new(b"keycast/derive/v1", sk, "sha256").digest(), b"cashu-wallet-v1\x01",
"sha256").digest()`. Without signing in, the wallet works like plain cashu.me with its own seed.

**Changing the patches.** Clone cashu.me, check out the commit, `git am patches/*.patch`, change,
commit, `git format-patch 002a0a3..HEAD -o patches/`. Check: `npm ci && BELFRY_MINT_URL=…
npx quasar build -m pwa` (the build runs ESLint with errors on).

Checks: smoke step 6o (the wallet client signs in silently; `derive_secret` gives it a stable seed
secret and a separate nutzap secret, refuses other purposes, and refuses the chat client), step 8c
(the token bundle a wallet redeems); in a browser, `infra/scripts/wallet-check.mjs`.
