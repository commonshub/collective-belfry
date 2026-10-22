# Keycast in the Belfry stack

Keycast (Divine's fork, github.com/divinevideo/keycast @ `fe9c6460`) is the Nostr signer: it holds every participant's key encrypted at rest and signs over HTTPS or NIP-46. Details and the verified API behaviour are in `docs/research/keycast-spike.md`.

## Image

`docker-compose.yml` builds `belfry-keycast:local` from the git context at that commit (Rust release build plus SvelteKit web build, about 15 minutes cold, 463 MB). If the image already exists locally, `docker compose up` does not rebuild; use `docker compose build keycast` to force it.

## Files in this directory

| File | Purpose |
| --- | --- |
| `master.key` | 32 random bytes, base64, written by `scripts/gen-secrets.sh`. Gitignored. AES-256-GCM key for every stored Nostr secret: losing it makes all participant keys unreadable, so back it up with the database. |
| `policy.sql` | The `belfry-event` policy (kinds 1, 7, 30023). Applied by `scripts/bootstrap.sh`, idempotent. |

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

## Tokens

- `KEYCAST_ADMIN_TOKEN` (`.env`, written by bootstrap): a 30-day admin UCAN from `GET /api/admin/token`, used for `POST /api/admin/preload-user`. Re-run `make bootstrap` to refresh it; it is re-issued when the stored one no longer passes `/api/admin/status`.
- `KEYCAST_SERVICE_TOKEN`: static, from `.env`.

## Things left from Divine

`vine_id` (the preload idempotency key; Belfry uses `odoo-partner-<id>`), minor accounts (inert unless `create-minor-account` is called) and the ATProto settings card (fails closed without `DIVINE_SKY_ATPROTO_CONTROL_PLANE_URL`). See the spike note, section 5.
