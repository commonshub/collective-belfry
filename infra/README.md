# Belfry local stack

One Docker Compose project (`belfry`) with everything slice 1 needs: Postgres, Odoo 19, Authentik, Keycast (signer, Belfry fork with Authentik login) with its NIP-46 relay and Redis, the community relay (rnostr, whitelist from Odoo) and Belfry's onboarding service. Contract: `docs/slice-1-onboarding.md`.

## Run

```
make up           # generates infra/.env on first run, starts everything except the onboarding service
make bootstrap    # database, users, tokens, policy; writes tokens into infra/.env
make up-app       # builds and starts the onboarding service (needs services/onboarding and the tokens)
make smoke        # invite -> pay -> accounts created exactly once
make down         # stop, keep volumes
```

Without `make`: `infra/scripts/gen-secrets.sh`, `docker compose -f infra/docker-compose.yml up -d`, `infra/scripts/bootstrap.sh`, `docker compose -f infra/docker-compose.yml --profile app up -d`.

`make up` starts the stack without the `app` profile, so it works before `services/onboarding` exists; the onboarding service needs the tokens bootstrap writes anyway. `make up-app` builds only that service (the Keycast image is never rebuilt implicitly, see below).

Requirements on the host: Docker with Compose v2, `curl`, `jq`, `python3`, `openssl`. About 5 GB of RAM for the stack (Odoo is capped at 2 GB, Postgres at 512 MB; Authentik server + worker take about 1 GB; Keycast about 100 MB; the community relay is capped at 256 MB), more while building images.

| Service | URL on the host | Login |
| --- | --- | --- |
| Odoo | http://localhost:8069 | `admin` / `ODOO_ADMIN_PASSWORD` |
| Authentik | http://localhost:9000 | `akadmin` / `AUTHENTIK_BOOTSTRAP_PASSWORD` |
| Keycast | http://localhost:3000 | `KEYCAST_ADMIN_EMAIL` / `KEYCAST_ADMIN_PASSWORD` |
| Keycast relay (NIP-46) | ws://localhost:3080 | |
| Community relay (rnostr) | ws://localhost:3090 | NIP-42 as a whitelisted pubkey, see `relay/README.md` |
| Onboarding service | http://localhost:8100 (`/health`) | |

Postgres and Redis are internal (`docker compose exec postgres psql -U postgres`).

## Secrets and tokens

`scripts/gen-secrets.sh` writes `infra/.env` from `.env.example` (every variable has a comment there) with random passwords, the Keycast server key and admin Nostr keypair (`scripts/nostr_keys.py`, pure Python), and `infra/keycast/master.key`. Existing files are kept, and variables added to `.env.example` since are appended to an existing `.env` (generated ones with fresh values); `--force` regenerates `.env` with a backup. An empty variable must not carry an inline comment (`VAR=   # note`): Compose reads the comment as the value, so comments for empty variables sit on the line above. Everything is gitignored.

`scripts/bootstrap.sh` fills the three values the services need and replaces them in `.env` in place:

| Variable | What | Lifetime |
| --- | --- | --- |
| `ODOO_API_KEY` | API key of the Odoo user `onboarding@belfry.local` (JSON-RPC password) | persistent |
| `KEYCAST_ADMIN_TOKEN` | admin UCAN from `GET /api/admin/token` | 30 days, re-run bootstrap |
| `AUTHENTIK_TOKEN` | the akadmin API token created by Authentik's bootstrap blueprint (`AUTHENTIK_BOOTSTRAP_TOKEN`) | non-expiring |

Restart the onboarding service after bootstrap so it reads the new values (`make up-app`).

## What bootstrap does

Idempotent; each step checks before it changes anything (a second run takes two seconds).

1. Waits for `/web/health` (Odoo), `/-/health/ready/` (Authentik) and `/health` (Keycast).
2. Creates the Odoo database `odoo` with `docker compose exec odoo odoo -d odoo -i base,belfry_pass --stop-after-init --without-demo` (the database must not exist beforehand: an empty database that matches the dbfilter makes Odoo answer 500 on every request). If `odoo/addons/belfry_pass` is missing, it installs the standard modules the contract lists instead and prints a notice; a later run installs `belfry_pass` once the directory exists. The full CLI output goes to `infra/.bootstrap-odoo.log`.
3. Sets the admin password, creates the user `onboarding@belfry.local` (Settings, Sales manager, Accounting manager and `belfry_pass.group_admin` when present) and generates an API key for it through `odoo shell` (`res.users.apikeys._generate` in a sudo env; the public `generate()` RPC method needs an existing key of the same user, so it cannot create the first one). Stores `belfry.onboarding_url` and `belfry.hook_token`.
4. Keycast: registers the admin with the key from `ALLOWED_PUBKEYS`, confirms the e-mail through the dev inbox (`GET /api/dev/emails`), logs in, fetches the admin token, applies `keycast/policy.sql` (idempotent, also updates the allowed kinds: 0, 1, 7, 22242, 30023) and checks `GET /api/policies` lists `belfry-event`.
5. Authentik: waits until the bootstrap token works, then until the blueprint `belfry-pass-bridge-source` reports `successful`, and checks that the source `pass-bridge` and the group `belfry-participants` exist. Then waits for `belfry-keycast-provider` (the OIDC provider and application Keycast logs in with, bound to `belfry-participants`), re-applying it once if it ran before the group existed, and checks the issuer `/application/o/keycast/`. Finally waits for `belfry-account-setup-flow` and checks that the default brand's recovery flow is `belfry-account-setup` (the onboarding service mints the participants' login setup links through it).

Step 2 also upgrades `belfry_pass` (`odoo -u`) when the manifest version differs from the installed one, and step 3 stores `belfry.apps_url` (= `KEYCAST_APP_URL`) for the activation mail.

## Keycast image

`belfry-keycast:local` is built from `keycast/build/Dockerfile`, which clones divinevideo/keycast at the pinned commit in a first stage, applies the Belfry patch series `keycast/build/patches/` (Authentik login, see `keycast/README.md`, "The Belfry fork") and then runs the upstream Dockerfile stages (a plain git build context fails on upstream's dangling `divine-funnelcake` submodule entry). The build takes about 15 minutes cold and is only run explicitly: `make build-keycast`. `docker compose up` uses the existing image. See `keycast/README.md` for the environment and the `master.key` path gotcha.

## Odoo notes

- Config: `odoo/odoo.conf` (addons path `/mnt/extra-addons` = `../odoo/addons`, `dbfilter ^odoo$`, `proxy_mode`, single process, memory limits). Database credentials reach Odoo as `PGHOST/PGUSER/PGPASSWORD`, so `docker compose exec odoo odoo -d odoo ...` and `odoo shell -d odoo` work without extra arguments.
- RPC: `/jsonrpc` with `execute_kw` works but Odoo 19 logs it as deprecated (removal planned for 22). The replacement is `POST /json/2/<model>/<method>` with `Authorization: Bearer <api key>` and the kwargs as JSON body, which the same API key already works for.
- `make test-odoo` drops `test_belfry`, then runs `odoo -d test_belfry -i belfry_pass --test-enable --test-tags /belfry_pass --stop-after-init` in a throwaway container (host port 8169) against the stack's Postgres.

## Authentik notes

- Blueprints in `authentik/blueprints/` are bind-mounted at `/blueprints/custom`; the worker applies them within seconds of a change. Status: `GET /api/v3/managed/blueprints/?search=belfry`.
- The pass-bridge source uses placeholder `authorization_url` / `access_token_url` / `profile_url` under `https://bridge.belfry.example`. `oidc_well_known_url` cannot be used until the bridge exists: authentik fetches it on save and rejects the source (400, name resolution error).
- Pre-linking a user to the source (`POST /api/v3/sources/user_connections/oauth/`): `source` is the source UUID, not the slug, and `user` is read-only in the serializer, so the request fails (400 for a slug, 500 IntegrityError on `user_id` for a UUID). The onboarding service therefore treats the pre-link as best effort (warning in its log) and the participant links the pass once from user settings; `smoke.sh` reports the missing link as a note. Revisit when the Phase 3 bridge exists (alternative: `user_matching_mode: username_link` with the bridge sending `preferred_username` = member number).
- Participant login (2026-10-23): users are created without a password. `belfry-account-setup-flow.yaml` defines a recovery flow `belfry-account-setup` (prompt password + repeat with a 12-character zxcvbn policy, user write, user login with a 14-day "remember me") and makes it the default brand's recovery flow (model `authentik_brands.brand`, not `authentik_core`). The onboarding service calls `POST /api/v3/core/users/<pk>/recovery/` (`token_duration` `days=30`), which plans that flow for the user and returns a one-time link built from the request host, so the service swaps the origin for `AUTHENTIK_PUBLIC_URL`. A second call for the same user replaces the token. Verified live: the link runs prompt → login → redirect and the session belongs to the participant; a consumed link ends in "No user found and can't create new user" and cannot set a password; the stale link's password does not log in. A direct visit to the flow without a token shows the prompt and fails the same way. Self-service "forgot password" is not wired (it needs authentik's SMTP settings); stewards re-send a link from the pass form.

## Smoke test

`scripts/smoke.sh` creates an invite over JSON-RPC, drives `/belfry/invite/<token>` with curl, emulates a Stripe payment (a `payment.transaction` in state `done` with a `provider_reference`, then a manual payment on the invoice, which fires the automation rule), waits for the pass to become `active`, checks the Authentik user and source link and the Keycast NIP-05 name, the relay whitelist and kind-0 profile, reads the login setup link from the pass and the queued activation mail, runs the account-setup flow with curl (password chosen, session is the participant's; the consumed link is refused), then signs in to Keycast through Authentik with that password (`scripts/keycast-oidc-login.sh`: OAuth popup, Authentik flow executor, callback, consent, token, a signed kind-1 event), asks for a new link through `/admin/passes/<id>/login-link` (second mail), and replays the webhook three times. Form field names and the emulated-payment approach are documented in its header.
