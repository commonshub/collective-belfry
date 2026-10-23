# Keycast in the Belfry stack

Keycast (Divine's fork, github.com/divinevideo/keycast @ `fe9c6460`) is the Nostr signer: it holds every participant's key encrypted at rest and signs over HTTPS or NIP-46. Details and the verified API behaviour are in `docs/research/keycast-spike.md`.

## Image

`docker-compose.yml` builds `belfry-keycast:local` from `build/Dockerfile`: it clones upstream at that commit, applies the Belfry patch series in `build/patches/`, then runs the upstream build (Rust release build plus SvelteKit web build, about 15 minutes cold, 463 MB). If the image already exists locally, `docker compose up` does not rebuild; use `docker compose build keycast` to force it after changing a patch.

## The Belfry fork

The fork is a patch series on top of the pinned upstream commit, kept in this monorepo (decision 6) instead of a GitHub fork. Each file in `build/patches/` is one `git format-patch` commit; the Dockerfile applies them in name order and the build fails if one no longer applies.

| Patch | What it changes |
| --- | --- |
| `0001-feat-auth-sign-in-through-an-upstream-OpenID-Provide.patch` | New `api/src/api/http/oidc.rs`: `GET /api/auth/oidc/start`, `/callback`, `/config`. Authorization code + PKCE against `OIDC_ISSUER`; the ID token is verified against the provider's JWKS (RS256/ES256, `iss`, `aud`, `exp`, `nonce`). The subject is mapped to an existing account (never created) and a server-signed UCAN session cookie is set, then the browser returns to `/api/oauth/authorize` where consent continues. New table `oidc_identities` (migration `20261023120000`), repository `core/src/repositories/oidc_identity.rs`. The OAuth popup shows "Sign in with Belfry". |
| `0002-feat-web-Sign-in-with-.-on-the-login-page-when-OIDC-.patch` | The web app's `/login` page: the same button, auto-continue after the callback, password form hidden with `OIDC_HIDE_PASSWORD_LOGIN`. |
| `0003-fix-oauth-token-exchange-for-accounts-without-an-e-m.patch` | Upstream bug that only shows with this login: `/api/oauth/token` read `users.email` as non-null and answered 503 for preloaded (unclaimed) accounts, which have no e-mail. |

**Who may sign in.** The Authentik application is bound to the group `belfry-participants`, which the onboarding service keeps equal to the Authentik users of active passes (added at activation and reactivation, removed when the last active pass closes, full sync at start and in `reconcile`, `python -m belfry_onboarding sync-participants`). A refund or expiry therefore blocks the Keycast login at Authentik, in addition to the Keycast suspension that blocks signing.

**Account mapping.** Keycast accounts are only created by the onboarding service (`preload-user` with `vine_id = odoo-partner-<id>`). Authentik's `keycast` scope (blueprint `authentik/blueprints/belfry-keycast-provider.yaml`) puts the same value into the claim `keycast_vine_id`. On the first login Keycast links the Authentik subject to the account with that `vine_id`; later logins use the stored link. An Authentik user without an Odoo partner, or one whose account does not exist yet, gets "No signing account". This replaces the claim link and the Keycast password: a preloaded account never needs claiming.

**Configuration** (set in `docker-compose.yml`): `OIDC_ISSUER` is Authentik's public URL plus `/application/o/keycast/` (it must equal the ID token's `iss`). `OIDC_BACKCHANNEL_URL=http://authentik-server:9000` sends discovery, token and JWKS calls through the Compose network while keeping the public `Host` header, so Authentik still issues tokens for the public issuer. `OIDC_CLIENT_SECRET` is `KEYCAST_OIDC_CLIENT_SECRET` from `.env`, shared with the blueprint. `KEYCAST_OIDC_ONLY=true` (default) hides the password forms; the password API stays, because `bootstrap.sh` registers the Keycast admin through it. The module header in `oidc.rs` lists every variable.

**Changing a patch.**

```bash
git clone https://github.com/divinevideo/keycast.git && cd keycast
git checkout -b belfry fe9c6460c89e82e22c3aab4ba1dc79b7f0dd54c0
git am <belfry>/infra/keycast/build/patches/*.patch
# edit, then: cargo test -p keycast_api --lib oidc && cargo clippy -p keycast_api --lib
git commit ...   # or git commit --amend / rebase -i to fold into an existing patch
rm <belfry>/infra/keycast/build/patches/*.patch
git format-patch fe9c6460..belfry -o <belfry>/infra/keycast/build/patches
docker compose -f <belfry>/infra/docker-compose.yml build keycast
```

To move to a newer upstream commit, rebase the branch onto it, regenerate the patches and update `KEYCAST_COMMIT` in the Dockerfile.

**Check it end to end:** `scripts/keycast-oidc-login.sh <authentik username> <password> <pubkey hex>` drives the whole browser flow with curl (popup, Authentik login over the flow executor API, callback, consent, token, a signed kind-1 event); `make smoke` runs it for a fresh participant (step 6c).

**Not done yet in the fork:** removing the Divine UI parts and branding, and key export for OIDC-only accounts (export needs a password, spike §6.5). Participants get their Authentik login through the account-setup link Odoo mails at activation (`authentik/blueprints/belfry-account-setup-flow.yaml`, since 2026-10-23); the pass sign-in arrives with the Phase 3 bridge.

## Files in this directory

| File | Purpose |
| --- | --- |
| `master.key` | 32 random bytes, base64, written by `scripts/gen-secrets.sh`. Gitignored. AES-256-GCM key for every stored Nostr secret: losing it makes all participant keys unreadable, so back it up with the database. |
| `build/Dockerfile`, `build/patches/` | Image build: upstream at the pinned commit plus the Belfry patch series (see "The Belfry fork"). |
| `policy.sql` | The `belfry-event` policy (kinds 0, 1, 7, 22242, 30023: profile, note, reaction, relay AUTH, long-form). Applied by `scripts/bootstrap.sh` on every run, idempotent, updates the kinds of an existing permission. |

## The master.key path gotcha

`MASTER_KEY_PATH` is only checked for presence. `core/src/encryption/file_key_manager.rs::load_key` opens `<CARGO_MANIFEST_DIR>/../master.key`, a path baked in at compile time, which resolves to `/app/master.key` inside the image. Mount the key exactly there (`./keycast/master.key:/app/master.key:ro`); any other path is silently ignored and Keycast fails to decrypt keys.

## Environment (set in docker-compose.yml from `.env`)

| Variable | Meaning |
| --- | --- |
| `DATABASE_URL` | `postgres://keycast:<KEYCAST_DB_PASSWORD>@postgres:5432/keycast`. Migrations are not run at startup; the one-shot `keycast-migrate` service runs `/app/keycast --migrate` first. |
| `REDIS_URL` | Required even single-instance (cluster hashring). |
| `KMS_PROVIDER=file`, `MASTER_KEY_PATH` | File-based KMS, see above. |
| `SERVER_NSEC` | Hex secret that signs server-issued UCANs (admin sessions, preload tokens). Rotating it invalidates every admin token. |
| `ALLOWED_PUBKEYS` | Full-admin whitelist for `/api/admin/*`; the admin registers with this key. |
| `ALLOWED_ORIGINS` | CORS for the auth endpoints; the web app's origin. |
| `ALLOWED_TENANT_DOMAINS` | Tenant = `Host` header without port. Unknown hosts get 400 "Invalid domain". Everything (browser, bootstrap, onboarding service) must present the same host, `localhost` locally; the onboarding service reaches `http://keycast:3000` and must set `Host: localhost`. |
| `BUNKER_RELAYS` | `ws://keycast-relay:8080`; the relay the signer listens on for NIP-46. Host port 3080. |
| `APP_URL`, `BASE_URL` | Claim and e-mail links. |
| `NODE_ENV=development`, `ENABLE_DEV_EMAIL_INBOX=true` | No SendGrid; captured mails are readable at `GET /api/dev/emails` (bootstrap uses it to verify the admin). Production needs `SENDGRID_API_KEY` or `DISABLE_EMAILS=true`. |
| `KEYCAST_SERVICE_TOKEN` | Bearer for the service-token routes (`PUT /api/admin/users/<pubkey>/status`, minor accounts). |
| `OIDC_*` | Belfry fork: login through Authentik, see "The Belfry fork". |

## Tokens

- `KEYCAST_ADMIN_TOKEN` (`.env`, written by bootstrap): a 30-day admin UCAN from `GET /api/admin/token`, used for `POST /api/admin/preload-user`. Re-run `make bootstrap` to refresh it; it is re-issued when the stored one no longer passes `/api/admin/status`.
- `KEYCAST_SERVICE_TOKEN`: static, from `.env`.

## Things left from Divine

`vine_id` (the preload idempotency key; Belfry uses `odoo-partner-<id>`), minor accounts (inert unless `create-minor-account` is called) and the ATProto settings card (fails closed without `DIVINE_SKY_ATPROTO_CONTROL_PLANE_URL`). See the spike note, section 5.
