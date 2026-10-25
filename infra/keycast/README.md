# Keycast: the signer

[Keycast](https://github.com/divinevideo/keycast) (Divine's version, pinned at `fe9c6460`) holds
every participant's Nostr key, encrypted at rest, and signs for the community apps over HTTPS
(HTTP RPC) or NIP-46. It also serves the participants' account page at `apps.<domain>`: the
community apps, the pass, the identity (npub, NIP-05 name), key export and self-custody. Belfry
runs it with a series of patches. Background on the choice and its API:
`docs/research/signing-server.md`, `docs/research/keycast-spike.md`.

## How participants use it

- **Accounts are created by Belfry, never by participants.** When a pass is activated, the
  onboarding service creates the account through Keycast's preload API, keyed by
  `vine_id = odoo-partner-<Odoo partner id>`. The key is derived from the custody seed (below).
- **Signing in goes through Authentik.** "Sign in with Open Source Village" runs OpenID Connect
  against Authentik. Authentik's `keycast` scope carries the claim `keycast_vine_id`; on the first
  sign-in Keycast links the Authentik user to the account with that `vine_id`, later sign-ins use
  the stored link. Only members of the Authentik group `belfry-participants` (active passes) may
  sign in, and an Authentik user without an account gets "No signing account".
- **The community apps** are OAuth clients of Keycast: `belfry-chat`, `belfry-calendar`,
  `belfry-vote`, `belfry-wallet`, `belfry-people`, `belfry-market`, `belfry-photos`. Bootstrap
  registers them with their redirect URIs; as trusted clients they skip the consent screen and can
  sign in silently (`prompt=none`) when the participant is already signed in. The token response
  carries a `bunker://` URL the app connects to over NIP-46.
- **What may be signed** is the policy `belfry-event` (`policy.sql`): a list of event kinds,
  applied by bootstrap. Encryption and decryption (NIP-44, NIP-04) are not limited by kind.
- **Logging out** of an app revokes that app's authorization and ends the Keycast and the
  Authentik session, so the silent sign-in does not log the participant straight back in.
- **Self-custody.** On the security page a participant can move their account to a key of their
  own (proved by signing a challenge with it). Keycast then deletes the key it held, and the apps
  sign in with the participant's own signer from then on.

## Custody seed and recoverable keys

With `KEYCAST_CUSTODY_SEED_FILE` set (Compose: `/run/belfry/custody-seed`), a participant's key is

```
HKDF-SHA256(salt "belfry/custody/v1", IKM = seed, info "belfry/nostr/v1/<vine_id>/<version>")
```

with the version from Odoo (`res.partner.belfry_key_version`). The seed file is read on every use:
a steward can unlock it without restarting Keycast, and while it is missing, account creation
answers 503 instead of creating a key that could not be recovered. Keys are still stored
encrypted for signing; the seed is needed only to create or recreate them. After losing Keycast's
data, `python -m belfry_onboarding restore-keys` recreates every account with the same key.
Because Belfry can recompute a derived key, self-custody always means moving to a new key.
Handling the seed: `../DEPLOY.md`, "Custody seed".

## The image and the patches

`build/Dockerfile` clones upstream at the pinned commit, applies `build/patches/*.patch` in name
order (the build fails if one does not apply) and runs the upstream build (Rust and SvelteKit,
about 15 minutes cold). `docker compose up` does not rebuild an existing image; after changing a
patch run `make build-keycast`.

| Patch | What it adds or changes |
| --- | --- |
| 0001 | sign-in through an upstream OpenID provider (`api/src/api/http/oidc.rs`): authorization code with PKCE against `OIDC_ISSUER`, ID token checked against the provider's keys, the subject linked to an existing account by `OIDC_LINK_CLAIM` (table `oidc_identities`), a server-signed session cookie; "Sign in with …" in the OAuth popup |
| 0002 | the same button on the web app's `/login` page; `OIDC_HIDE_PASSWORD_LOGIN` hides the password form |
| 0003 | fix: the token exchange failed for accounts without an e-mail address (all preloaded accounts) |
| 0004 | the Open Source Village brand instead of Divine's: names, mails, colours (dark theme), self-hosted Geist font, logos, landing page copy for accounts without passwords |
| 0005 | key export for accounts without a password: allowed within `OIDC_REAUTH_WINDOW_SECS` (300) of a fresh sign-in; "Sign in again to unlock" on the security page |
| 0006 | the e-mail address from the ID token is stored as verified (`OIDC_TRUST_EMAIL`); "Your pass" card on the dashboard from the onboarding service (`BELFRY_PASS_INFO_URL`, `PASS_INFO_TOKEN`) |
| 0007 | `BUNKER_PUBLIC_RELAYS`: the relay URL written into `bunker://` URLs and NIP-05 discovery, as clients reach it |
| 0008 | silent sign-in (`prompt=none`) and trusted clients (`KEYCAST_TRUSTED_CLIENTS`) that skip the consent screen |
| 0009 | "Community apps" cards on the dashboard (`BELFRY_COMMUNITY_APPS`) |
| 0010 | logout through Authentik (`/api/auth/oidc/logout`, `/logged-out`), app-initiated revocation (`POST /api/oauth/revoke`), pruning of abandoned authorizations of trusted clients, branded sign-in error pages |
| 0011 | `derive_secret`: per-purpose secrets derived from the participant's key, for allowed clients only (`KEYCAST_DERIVE_SECRETS`); the wallet's seed and nutzap key |
| 0012 | self-custody: take over the key or move the account to another key; Keycast reports the change to `BELFRY_KEY_CHANGED_URL`; apps get `access_denied`/`self_custody` for accounts without a held key |
| 0013 | `OIDC_LOGOUT_URL`: logout goes to Authentik's logout flow directly (the discovered end-session endpoint returns an empty page when an unfinished sign-in is pending) |
| 0014 | participant keys derived from the custody seed (above); preload with a `pubkey` recreates an account without a key |
| 0015, 0016 | layout for phones and large text: buttons wrap, the header wraps, long identity values break |
| 0017 | "Show your pass" on the pass card: the pass as a renewing QR code (`/me` of the onboarding service) |

**Changing a patch:**

```bash
git clone https://github.com/divinevideo/keycast.git && cd keycast
git checkout -b belfry fe9c6460c89e82e22c3aab4ba1dc79b7f0dd54c0
git am <repo>/infra/keycast/build/patches/*.patch
# edit, then: cargo test -p keycast_api --lib, and in web/: bun run check && bun run build
git commit ...
git format-patch -1 HEAD --start-number <next number> -o <repo>/infra/keycast/build/patches/
make build-keycast
```

Add new patches with the next number and leave the existing files untouched (`format-patch`
over the whole range rewrites every header). To change an existing patch, fold the fix into it
(`git commit --fixup`, `git rebase -i --autosquash`) and regenerate the series from that patch on.
To move to a newer upstream commit, rebase the branch, regenerate the series and update
`KEYCAST_COMMIT` in the Dockerfile.

## Configuration

Set in `docker-compose.yml` from `.env`; the secrets (`SERVER_NSEC`, `OIDC_CLIENT_SECRET`,
`KEYCAST_SERVICE_TOKEN`, `PASS_INFO_TOKEN`, `BELFRY_HOOK_TOKEN` and the database password as
`PGPASSWORD`) come from `infra/secrets/` through the entrypoint wrapper (`SECRETS_ENV`,
`../README.md`, "Secrets"). Upstream settings:

| Variable | Meaning |
| --- | --- |
| `DATABASE_URL` | Postgres. Keycast does not migrate at startup; the one-shot service `keycast-migrate` does. |
| `REDIS_URL` | required even with one instance |
| `KMS_PROVIDER=file`, `MASTER_KEY_PATH` | the file `master.key` encrypts every stored key (see below) |
| `SERVER_NSEC` | signs server-issued tokens (admin tokens, preload tokens); changing it invalidates them |
| `ALLOWED_PUBKEYS` | admins for `/api/admin/*` |
| `ALLOWED_ORIGINS` | CORS for the web app |
| `ALLOWED_TENANT_DOMAINS` | Keycast picks the tenant from the `Host` header; callers inside Compose must send the tenant's host (the onboarding service sets `KEYCAST_TENANT_DOMAIN`) |
| `BUNKER_RELAYS` | the relay the signer listens on for NIP-46 (`ws://keycast-relay:8080`) |
| `KEYCAST_SERVICE_TOKEN` | bearer for the service routes (account status) |
| `APP_URL`, `BASE_URL` | links in pages and mails |

Belfry's additions:

| Variable | Meaning |
| --- | --- |
| `OIDC_ISSUER`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`, `OIDC_REDIRECT_URL`, `OIDC_SCOPES`, `OIDC_LINK_CLAIM`, `OIDC_PROVIDER_NAME`, `OIDC_LOGOUT_URL` | sign-in through Authentik. `OIDC_ISSUER` must equal the ID token's `iss` |
| `OIDC_BACKCHANNEL_URL` | reach Authentik through the Compose network (local stack only; on a server Keycast uses the public URL, see `../DEPLOY.md`) |
| `OIDC_HIDE_PASSWORD_LOGIN`, `OIDC_TRUST_EMAIL`, `OIDC_REAUTH_WINDOW_SECS` | no password forms; trust the provider's e-mail; key export window |
| `BUNKER_PUBLIC_RELAYS` | the NIP-46 relay as clients reach it (`wss://bunker.<domain>`) |
| `KEYCAST_TRUSTED_CLIENTS`, `KEYCAST_DERIVE_SECRETS` | Belfry's apps; which client may derive which secrets |
| `KEYCAST_CUSTODY_SEED_FILE` | the custody seed |
| `BELFRY_PASS_INFO_URL`, `PASS_INFO_TOKEN`, `BELFRY_COMMUNITY_APPS`, `BELFRY_KEY_CHANGED_URL`, `BELFRY_HOOK_TOKEN` | the onboarding service for the pass card and key changes; the apps on the dashboard |
| `NIP05_DOMAIN` | the domain of the NIP-05 names shown on the dashboard |

Keycast sends no mails in Belfry (participants sign in through Authentik); its development mail
inbox is only used by bootstrap to confirm the admin account.

**Graceful shutdown.** Keycast leaves its signer ring in Redis at the end of a shutdown that takes
about a minute. Compose gives it 90 s (`stop_grace_period`); if it were killed earlier, NIP-46
requests for its keys would be dropped for 30 s.

## Files here

| File | Purpose |
| --- | --- |
| `master.key` | 32 random bytes (base64) from `gen-secrets.sh`, gitignored. The AES-256-GCM key of every stored Nostr secret: back it up. It must be mounted exactly at `/app/master.key`: Keycast ignores `MASTER_KEY_PATH` beyond checking it is set and reads a path fixed at compile time. |
| `policy.sql` | the `belfry-event` policy (allowed event kinds) and the tenant's name; bootstrap applies it on every run |
| `build/` | the image build and the patch series |

## Tokens

- `KEYCAST_ADMIN_TOKEN`: a 30-day admin token for creating accounts, written by bootstrap and
  renewed when bootstrap runs after it expired.
- `KEYCAST_SERVICE_TOKEN`: static, from `infra/secrets/keycast_service_token`.

## Checks

The smoke test signs a participant in through Authentik (`scripts/keycast-oidc-login.sh`), signs
events of every allowed kind and is refused one outside the policy, checks NIP-46, single sign-on
and logout (`scripts/silent-signin-check.sh`), key recovery from the seed and self-custody
(`scripts/self-custody-check.sh`).

Left over from Divine and unused by Belfry: the `vine_id` name, the `--color-divine-*` CSS
variable names, minor accounts and the ATProto settings (inactive without their settings).
