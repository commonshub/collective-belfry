# Keycast spike (2026-10-22)

Half-day check of the plan's Phase 2 claims against the real software: github.com/divinevideo/keycast at commit `fe9c6460c89e82e22c3aab4ba1dc79b7f0dd54c0` (2026-10-19, "fix(redis): reconnect when the Redis primary changes"). Everything below was run locally with Docker unless marked *unverified*.

## 1. Running Keycast with `KMS_PROVIDER=file`

What was run: the repo's own `Dockerfile` (Rust 1.93 release build of the unified `keycast` binary plus the SvelteKit web build; ~15 min cold, 463 MB image), then a small compose stack: Postgres 16, Redis 7, a Nostr relay (`ghcr.io/verse-pbc/relay_builder:latest`, the image the repo's `docker-compose.deps.yml` uses for e2e), a one-shot migration container and Keycast itself.

```bash
docker build -t belfry-keycast:spike .            # from the clone root
docker compose -f compose.yml up -d                 # project name belfry-keycast
curl http://localhost:3000/health                   # {"status":"ok","service":"keycast"}
docker compose -f compose.yml down -v               # teardown
```

Compose essentials (full file was in the scratchpad; this is what matters):

| Service | Notes |
| --- | --- |
| `migrate` | same image, `entrypoint: ["/app/keycast", "--migrate"]`, `DATABASE_URL` only. The entrypoint script only knows `unified`/`keycast`/`web`, so the entrypoint must be overridden. Migrations are not run automatically at startup. |
| `keycast` | `command: unified`, port `3000:3000`, volume `./master.key:/app/master.key:ro` |
| `relay` | `IDLE_TIMEOUT=0`, port `3080:8080` |

Environment that was needed (startup validation is in `keycast/src/main.rs::validate_environment`):

```
DATABASE_URL=postgres://postgres:password@postgres:5432/keycast
REDIS_URL=redis://redis:6379          # required even single-instance (cluster hashring)
KMS_PROVIDER=file
MASTER_KEY_PATH=/app/master.key       # only checked for presence, see note
SERVER_NSEC=<64 hex>                  # signs server-issued UCANs (admin/preload sessions)
ALLOWED_PUBKEYS=<admin pubkey hex>    # full-admin whitelist for /api/admin/*
ALLOWED_ORIGINS=http://localhost:3000
ALLOWED_TENANT_DOMAINS=localhost      # tenant = Host header minus port; unknown hosts are rejected
BUNKER_RELAYS=ws://relay:8080         # relays the signer listens on for NIP-46
APP_URL=http://localhost:3000         # used in claim URLs and injected into the web app
BASE_URL=http://localhost:3000        # used in e-mail links
WEB_BUILD_DIR=/app/web
NODE_ENV=development                  # production requires SENDGRID_API_KEY or DISABLE_EMAILS=true
ENABLE_DEV_EMAIL_INBOX=true           # exposes GET /api/dev/emails (captured mails), non-production only
FROM_EMAIL=noreply@belfry.test  FROM_NAME=Belfry
KEYCAST_SERVICE_TOKEN=<random>        # bearer for the service-token admin routes (create-minor-account etc.)
PORT=3000
```

`master.key` is 32 random bytes, base64 (`openssl rand 32 | base64`, as in `scripts/generate_key.sh`).

Notes:
- `MASTER_KEY_PATH` is not actually read. `core/src/encryption/file_key_manager.rs::load_key` opens `<CARGO_MANIFEST_DIR>/../master.key`, a path baked in at compile time, which in the Docker image resolves to `/app/master.key`. The env var is only checked for presence. Mount the key exactly at `/app/master.key`.
- Keys at rest: AES-256-GCM with the master key; `personal_keys.encrypted_secret_key` was 60 bytes per user (12-byte nonce + 32 bytes + 16-byte tag).
- Startup logs `Can't authenticate to relay ... signer not configured` because the relay offered NIP-42 AUTH; signing over NIP-46 still worked because `relay_builder` does not require AUTH. Whether Keycast can AUTH to a relay that requires it is *unverified*; relevant if Belfry's whitelisted relay enforces NIP-42.
- Admin bootstrap (no admin exists after migration): generate a Nostr keypair, put its pubkey in `ALLOWED_PUBKEYS`, register with that key, verify the e-mail from the dev inbox, log in, fetch the admin token.

```bash
curl -X POST localhost:3000/api/auth/register -H 'Content-Type: application/json' -H 'Origin: http://localhost:3000' \
  -d '{"email":"admin@belfry.test","password":"<pw>","nsec":"<admin secret hex>"}'
TOKEN=$(curl -s localhost:3000/api/dev/emails | jq -r '.[0].verification_url' | sed 's/.*token=//')
curl -X POST localhost:3000/api/auth/verify-email -H 'Content-Type: application/json' -d "{\"token\":\"$TOKEN\"}"
curl -c cookies.txt -X POST localhost:3000/api/auth/login -H 'Content-Type: application/json' -H 'Origin: http://localhost:3000' \
  -d '{"email":"admin@belfry.test","password":"<pw>"}'            # sets keycast_session cookie (a UCAN)
SESSION=$(grep keycast_session cookies.txt | awk '{print $7}')
curl localhost:3000/api/admin/status -H "Authorization: Bearer $SESSION"   # {"is_admin":true,"role":"full"}
curl localhost:3000/api/admin/token  -H "Authorization: Bearer $SESSION"   # 30-day admin UCAN
```

Note the GET link in the verification mail (`/verify-email?token=…`) only 303-redirects to a web page; the API call that verifies is `POST /api/auth/verify-email`. Login is refused (`EMAIL_NOT_VERIFIED`) until then.

## 2. Admin API: preload-user and claim

Handler: `api/src/api/http/admin.rs::preload_user` (line 224); repository: `core/src/repositories/user.rs::create_preloaded_user` (line 2677); routes in `api/src/api/http/routes.rs` lines 282–323.

Request and response that worked (`$ADMIN` = token from `/api/admin/token`):

```bash
curl -X POST localhost:3000/api/admin/preload-user -H "Authorization: Bearer $ADMIN" -H 'Content-Type: application/json' \
  -d '{"vine_id":"odoo-partner-1001","username":"alice","display_name":"Alice Example"}'
# 200 {"pubkey":"98a8736c44c69e2c384b832fc36d765888bf9f6a16d6008c579e684fa674402d","token":"<UCAN, 30 days>"}
```

Observed behaviour:

| Case | Result |
| --- | --- |
| Same `vine_id` again (different username) | 200, same pubkey, fresh token; username in the replay is ignored. Idempotent on `vine_id`. |
| Different `vine_id`, same username | 409 `User with username alice already exists` |
| `vine_id` missing | 422 (serde: missing field) — required |
| `vine_id: ""` | 200, user created with the empty string stored; a second preload with `""` returned the first user's pubkey. No non-empty check. |
| `vine_id: "res.partner/42 ünïcode"`, `username: "Carol Mixed Case!"` | 200, both stored verbatim. No format validation on either field on this endpoint. |
| No token | 401 |

- `vine_id` is `users.vine_id TEXT` (migration `0004_preloaded_accounts.sql`) with a partial unique index on `(tenant_id, vine_id) WHERE vine_id IS NOT NULL`. Lookup is `SELECT pubkey FROM users WHERE vine_id = $1 AND tenant_id = $2`. No regex, no length limit. It can be repurposed as the Odoo id as-is (e.g. `res.partner:1001` or the invoice id); Belfry should add a non-empty check and decide which Odoo id is the idempotency key. Username uniqueness is `lower(username)` per tenant, and the username is immediately a NIP-05 name: `GET /.well-known/nostr.json?name=alice` returned the pubkey, also for unclaimed accounts.
- The Nostr key is generated at preload time (`Keys::generate()`, encrypted with the KMS, inserted into `personal_keys` in the same transaction), so the pubkey (npub) is available in the response for the Odoo record and the pass certificate before any claim.
- The returned `token` is a server-signed UCAN with no policy: it signed kind 1 and kind 4 via `POST /api/nostr`. It is meant for Vine's import tooling to post on behalf of users. Belfry's webhook does not need it and should not store it.
- `display_name` lands in `users.display_name`. The `social` policy includes kind 0, so a kind-0 profile event is not created automatically (*unverified* whether any Keycast path publishes one).

Claim flow (`api/src/api/http/claim.rs`), run end to end:

```bash
curl -X POST localhost:3000/api/admin/claim-tokens -H "Authorization: Bearer $ADMIN" -H 'Content-Type: application/json' -d '{"vine_id":"odoo-partner-1001"}'
# 200 {"claim_url":"http://localhost:3000/api/claim?token=5EVT…","expires_at":"2026-11-05T17:43:59Z"}   (14 days)
curl "localhost:3000/api/claim?token=$CT"                       # HTML form: email, password, password_confirmation
curl -X POST localhost:3000/api/claim -d "token=$CT&email=alice@belfry.test&password=…&password_confirmation=…"
# 200 "Check Your Email"; credentials are staged, not applied; confirmation mail sent
curl "localhost:3000/api/claim/confirm?token=$CONF"             # 200 HTML with a "Confirm and Claim Account" button, no side effect
curl -X POST localhost:3000/api/claim/confirm -d "token=$CONF"  # 200 "Account Claimed!", Set-Cookie: keycast_session=…
```

After confirmation `users.email` is set, `email_verified=true`, `vine_id` kept; `POST /api/admin/claim-tokens` for that user now returns 409 `User has already claimed their account`; password login works. `claim-tokens/batch` (up to a limit, sends the mail itself), `claim-tokens/invalidate` and `GET /api/admin/claim-tokens?pubkey=` also exist. Regenerating a claim token invalidates prior ones.

`provisioning_operation_id` is **not** on `preload-user`. It belongs to `POST /api/admin/create-minor-account` (`api/src/api/http/service_provisioning.rs`, documented in `docs/SERVICE_ACCOUNT_PROVISIONING.md`), which authenticates with `KEYCAST_SERVICE_TOKEN` (not an admin UCAN), validates the username (1–64 chars, lowercase alnum/`-`/`_`), and sets `users.verified_minor = true`. Verified:

```bash
H='Authorization: Bearer <KEYCAST_SERVICE_TOKEN>'
curl -X POST localhost:3000/api/admin/create-minor-account -H "$H" -H 'Content-Type: application/json' \
  -d '{"provisioning_operation_id":"eef66301-1326-48ce-9680-758f825e2782","username":"erin","display_name":"Erin"}'
# 201 {"pubkey":"b192…","claim_url":"…/api/claim?token=…","expires_at":"…","account_state":"unclaimed","replayed":false}
# exact replay            -> 200, same pubkey and claim_url, "replayed":true
# same id, other username -> 409 {"code":"provisioning_operation_conflict","retryable":false}
# no operation id         -> 201, username-based (transitional path)
# wrong token             -> 401
```

The operation record (`service_provisioning_operations`) stores a fingerprint of tenant+username+display_name and survives claim and deletion, so a webhook retry after the account was claimed returns `account_state: "claimed"` instead of creating a second user. This is the retry semantics the plan wants, but it is tied to minor accounts (`verified_minor` turns on the DM containment gate in `core/src/verified_minor_dm.rs`). For Belfry: either rely on `preload-user`'s `vine_id` idempotency (keyed on an Odoo id, not the Stripe event), or port the operation-id logic to `preload-user` (same file, ~150 lines of reusable helpers).

## 3. Policies and signing (HTTP RPC and NIP-46)

Policies are global rows in `policies` (slug) joined to `permissions` (identifier, JSON config) via `policy_permissions`; seeds in `database/migrations/0001_initial_schema.sql` (`social`, `readonly`, `wallet`, `full`). The only permission type that gates signing by kind is `allowed_kinds` (`core/src/custom_permissions/allowed_kinds.rs`; a permission's `identifier` must start with `allowed_kinds`). There is no API for creating global policies (`GET /api/policies` is read-only; `POST /api/teams/:id/policies` creates team policies, which OAuth scopes do not resolve), so the policy was inserted by SQL:

```sql
INSERT INTO permissions (identifier, config, created_at, updated_at)
  VALUES ('allowed_kinds_belfry_event', '{"allowed_kinds": [1, 7, 30023]}', NOW(), NOW());
INSERT INTO policies (name, team_id, slug, display_name, description, created_at, updated_at)
  VALUES ('Belfry Event Apps', NULL, 'belfry-event', 'Belfry event apps', 'Notes, reactions and long-form posts only', NOW(), NOW());
INSERT INTO policy_permissions (policy_id, permission_id, created_at, updated_at)
  SELECT p.id, perm.id, NOW(), NOW() FROM policies p, permissions perm
  WHERE p.slug = 'belfry-event' AND perm.identifier = 'allowed_kinds_belfry_event';
```

`GET /api/policies` then listed `belfry-event` with "Post notes, React to posts, Write long-form articles".

OAuth 2.0 + PKCE as an app (`client_id` does not need registration; registering one only restricts redirect URIs):

```bash
# 1. user login inside the OAuth popup (sets the session cookie)
curl -c c.txt -X POST localhost:3000/api/oauth/login -H 'Content-Type: application/json' -H 'Origin: http://localhost:3000' \
  -d '{"email":"alice@belfry.test","password":"…","client_id":"belfry-chat","redirect_uri":"http://localhost:3099/callback","scope":"policy:belfry-event"}'
# 2. consent (GET /api/oauth/authorize?… renders the "Choose Account"/consent HTML; the approve call is JSON)
curl -b c.txt -X POST localhost:3000/api/oauth/authorize -H 'Content-Type: application/json' \
  -d '{"client_id":"belfry-chat","redirect_uri":"http://localhost:3099/callback","scope":"policy:belfry-event","state":"xyz","approved":true,"code_challenge":"<S256>","code_challenge_method":"S256"}'
# 200 {"code":"pQRn…","redirect_uri":"…","state":"xyz"}
# 3. token
curl -X POST localhost:3000/api/oauth/token -H 'Content-Type: application/json' \
  -d '{"grant_type":"authorization_code","code":"pQRn…","client_id":"belfry-chat","redirect_uri":"http://localhost:3099/callback","code_verifier":"<verifier>"}'
# 200 {"bunker_url":"bunker://c1a9…?relay=ws%3A%2F%2Frelay%3A8080&secret=…","access_token":"<UCAN>","token_type":"Bearer",
#      "expires_in":86400,"scope":"policy:belfry-event","policy":{…},"authorization_handle":"…","refresh_token":"…"}
```

Signing with the access token (`POST /api/nostr`, event must include `pubkey`):

| kind | HTTP RPC | NIP-46 (bunker URL over the local relay, nostr-tools 2.10 `BunkerSigner`) |
| --- | --- | --- |
| 1, 7, 30023 | 200, signed event | signed event |
| 0, 4, 3 | 403 `{"error":"Operation denied by policy"}` | error `Permission denied: Blocked by 'allowed_kinds' policy` |
| `nip44_encrypt` | 200 (allowed_kinds does not gate encryption) | not tested |

Both transports gave identical results. Enforcement points: HTTP in `api/src/handlers/http_rpc_handler.rs::validate_sign` (permissions loaded in `api/src/api/http/nostr_rpc.rs` ~line 1040 and cached per authorization); NIP-46 in `signer/src/signer_daemon.rs::validate_permissions_for_sign` (line 749, loads the authorization's policy from the DB on every request; empty permission list means allow all). The NIP-46 script printed a client-side error on `connect` (nostr-tools parsing the connect ack) but all subsequent calls worked; the signer logs show the denials as `nip46.denial_response`.

Hooks for the plan's "pass-tier and expiry checks":
- `oauth_authorizations` has `policy_id`, `expires_at`, `revoked_at`; `authorizations` (NIP-46) similar. Policies are per app and kind, with no per-user attributes, so tier/expiry would not live in a policy.
- `users.status` (`active|suspended|banned`) is checked before signing on both transports (`check_user_active`), and there is a service-token route `PUT /api/admin/users/:pubkey/status` (`admin::set_user_status_admin`). A nightly job or the refund webhook can suspend an account; this needs no Keycast change. *Unverified*: whether a suspension takes effect for an HTTP RPC handler that is already cached (the cache is keyed on the UCAN expiry).
- Per-tier policies (`policy:belfry-day`, `policy:belfry-full`) are possible but the app, not Belfry, chooses the scope, so they do not enforce tiers.

Also verified: `POST /api/user/export-key` with the account password returns the `nsec` (`auth::export_key`, password is required); `GET /api/user/sessions` lists the app authorizations with activity counts; `POST /api/user/sessions/revoke` exists.

## 4. Login and an upstream OIDC (Authentik) login

Where login lives:
- API: `api/src/api/http/auth.rs` — `register` (line 1130, email+password, optional `nsec` import), `login` (1266; JSON email/password, or NIP-98 `Authorization: Nostr …` which is admin-only, see `nostr_auth_login` 1030), `verify_email`/`verify_email_get`, password reset, `change_password`, `export_key`. `api/src/api/http/oauth.rs` — `oauth_login` (3628) and `oauth_register` for the OAuth popup, whose HTML (email/password and "Create account" forms, ~lines 2100–2200) is inlined in Rust. `api/src/api/http/headless.rs` — JSON-only register/login/PIN flow for native apps. Routes in `routes.rs` lines 95–131 and 274–278.
- Web: `web/src/routes/login/+page.svelte`, `register/+page.svelte`, `web/src/lib/utils/auth.ts` (`signin`: password or NIP-07 admin login), `web/src/lib/keycast_api.svelte.ts`.
- Tables: `users` (`email`, `password_hash`, `email_verified`, `status`, `username`, `vine_id`), `personal_keys`, `email_verification_tokens`, `password_reset_tokens`, `auth_events`.
- Sessions: a UCAN in the `keycast_session` cookie or a bearer. Two issuers are accepted by `api/src/ucan_auth/validation.rs::validate_ucan_token` (line 78): the user's own key (`generate_ucan_token`, requires decrypting the user key at login) or the server key (`generate_server_signed_ucan`, `auth.rs` line 306; used for NIP-98 admin sessions, preloaded users and the claim confirmation). Required facts: `tenant_id`, `redirect_origin`; optional `email`, `admin_role`, `account_status`.
- No OIDC anywhere: `/.well-known/openid-configuration` (`openid_configuration.rs`) states Keycast "is primarily an OAuth 2.0 + Nostr Bunker (NIP-46) provider rather than an OpenID Provider: it does not issue ID Tokens". So the alternative "Authentik federates to Keycast" is not available either without work.

Feasibility: yes. A session can be minted after an external login without a password, because the server-signed UCAN path already exists and `authorize_get` only needs the cookie. Sketch:

1. `api/src/api/http/oidc.rs` (new): `GET /api/auth/oidc/start` (build Authentik authorize URL with PKCE and state, remember the pending OAuth request), `GET /api/auth/oidc/callback` (exchange code, verify `id_token` against Authentik's JWKS, read `sub` and `email`). Use the `openidconnect` crate or `oauth2` + `jsonwebtoken`. Config: `OIDC_ISSUER`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`, `OIDC_REDIRECT_URL`.
2. Account mapping in `core/src/repositories/user.rs`: find by a new `users.external_subject` (migration with a unique index per tenant), falling back to `email` or `vine_id` for preloaded accounts (the Odoo webhook knows both); create user + key with `create_preloaded_user`-style code if absent, set `email`/`email_verified`. This replaces the claim step.
3. Mint `generate_server_signed_ucan(pubkey, tenant, email, redirect_origin, None, server_keys, true, None, status)`, set the `keycast_session` cookie, redirect back to `/api/oauth/authorize?...` so consent continues.
4. UI: add a "Sign in with Belfry" button to the OAuth popup HTML in `oauth.rs` and to `web/src/routes/login/+page.svelte`; optionally a flag to hide the password forms. `routes.rs` to mount the routes.
5. Follow-ups: `export_key` and `change_email` assume a password; add an alternative confirmation for OIDC-only accounts.

Rough effort: 3–5 developer days including tests, plus Authentik app configuration. The main uncertainties are the `TenantExtractor` (Host-based) and `redirect_origin` fact conventions, both small.

## 5. Divine-specific parts

| Part | Files | Disable by config? |
| --- | --- | --- |
| `vine_id` | `api/src/api/http/admin.rs` (preload, claim-tokens, batch, invalidate, user-lookup), `core/src/repositories/user.rs` 2672–2735, `database/migrations/0004_preloaded_accounts.sql`, `core/examples/migrate-vine-users.rs` (also built by the Dockerfile), `k8s/migrations/migrate-vine-users-*.yaml`, `docs/vine-import-guide.md`, `web/src/routes/admin/+page.svelte` | No flag, but it is just a free-text external id. Keep it and treat it as the Odoo id (optionally rename in a later migration). |
| Minor accounts | `api/src/api/http/service_provisioning.rs`, `core/src/verified_minor_dm.rs` (734 lines, pinned official pubkeys), `admin.rs::set_verified_minor_admin`/`clear_verified_minor_admin`, gates in `api/src/api/http/nostr_rpc.rs` (~730) and `signer/src/signer_daemon.rs` (~800, ~2295), `database/migrations/20260527120000_add_verified_minor.sql`, `web/src/routes/support-admin/+page.svelte` | Inert unless `verified_minor=true`, which only `create-minor-account` and `PUT /api/admin/users/:pubkey/verified-minor` set, both behind `KEYCAST_SERVICE_TOKEN`. Leaving the token unset makes those routes fail closed (500 "not configured"). Can stay as dead code. |
| ATProto / Bluesky provisioning | `api/src/api/http/atproto.rs`, `atproto_oauth.rs`, `atproto_oauth_metadata.rs`, `atproto_oauth_replay.rs`, `api/src/atproto_provisioning.rs`, `core/src/repositories/atproto_oauth_session.rs`, migrations `0009_add_atproto_link_state.sql` and `20260328*`, `web/src/lib/atproto_status.svelte.ts`, `web/src/lib/components/AtprotoSettingsCard.svelte`, `web/src/routes/settings/security/+page.svelte` (card is always rendered) | Yes on the API: with `DIVINE_SKY_ATPROTO_CONTROL_PLANE_URL` unset the server logs a warning and the endpoints "fail closed" (`main.rs::warn_atproto_control_plane_config`). No on the UI: the settings card must be removed from `settings/security/+page.svelte`. |
| Divine name server (username claims) | `api/src/divine_names.rs`, called from `preload_user` and profile updates | Off unless `DIVINE_NAME_SERVER_URL` or `ENABLE_DIVINE_NAMES=true` (`is_enabled`, line 159). |
| Branding | `api/src/brand.rs`, `web/src/lib/brand.ts`, tenant seed `database/migrations/20260423100000_update_tenant_seed_branding.sql`, seeded Divine OAuth clients `20260426192000_seed_divine_invite_admin_client.sql`, `20260710150000_seed_divine_crossposter_client.sql`, e-mail templates in `api/src/email_service.rs` | Partly: `FROM_NAME=Belfry` did not change the subject "Verify your Divine email address" or "…claim your Divine account"; brand strings come from the tenant row / `brand.rs`. Needs a tenant branding change. |
| Other Divine services | `api/src/api/http/ap.rs` (ActivityPub RSA keys for a gateway), `email_marketing.rs` (HubSpot-style consent sync), `og_diviner.rs`, `headless.rs` (mobile), groups/FROST modules | All behind service tokens or unused; can stay. |

## 6. Contradictions with the plan and the research note

1. **The claim step is missing from the happy path.** `preload-user` gives the npub immediately (step 4 holds), but the participant cannot sign in to the Nostr apps until the account is claimed (claim link, e-mail + password, confirmation e-mail: two mails and a form) or until the Authentik OIDC login (section 4) binds the preloaded account on first sign-in. Either add "claim your account" to step 5 next to the OpenID4VCI QR, or make the OIDC login the claim. The second option avoids a second password and matches "one login".
2. **`provisioning_operation_id` is not on `preload-user`** (contrary to `docs/research/signing-server.md`). It exists only on `create-minor-account`, which marks the account as a protected minor. `preload-user` is idempotent on `vine_id`, so the webhook must key it on an Odoo id (partner or invoice), and the Stripe-event idempotency stays in Belfry's own handler. The research note should be corrected.
3. **"Pass-tier and expiry checks in policies"** does not fit the model: policies are per-app kind lists with no user attributes. Use `users.status` (`PUT /api/admin/users/:pubkey/status`, service token) from the refund webhook and the nightly expiry job instead, and keep policies per app.
4. **"Connect its login to Authentik"** is new code (section 4); nothing in Keycast speaks OIDC in either direction.
5. **Key export needs a password**, which OIDC-only accounts will not have; the export flow has to be adapted before "managed, exportable" holds for full-access members.
6. **NIP-05** is served by Keycast itself for every username (`/.well-known/nostr.json`), including unclaimed accounts. If Keycast runs on Belfry's domain this covers the plan's NIP-05 item; otherwise the names file must be generated elsewhere.
7. **Global policies need SQL** (or a migration); there is no admin API or UI for them. Fine for a handful of app policies, but worth knowing for operations.
8. Branding and e-mail copy still say "Divine" with the standard env vars.

## Open questions

- Which Odoo id becomes `vine_id`: partner id (one Nostr key per person across upgrades) or invoice/pass id (new key per purchase)? The plan's "upgrade = new certificate" suggests the partner id.
- Does Belfry's relay require NIP-42 AUTH? If so, confirm Keycast's bunker connection can authenticate or exempt it (*unverified*).
- Does suspending a user (`users.status`) take effect immediately on the HTTP RPC cache (*unverified*)?
- Keep the claim flow as a fallback for participants without an Authentik account, or go OIDC-only?
- Do we want a kind-0 profile published at preload (display name) so directory entries exist before first login? Nothing in Keycast does this.
- Whether to strip the ATProto, minor-account and marketing-consent modules or carry them as dead code; stripping touches the schema (several migrations) and the web settings page.
