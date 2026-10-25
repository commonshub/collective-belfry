# The Belfry stack

One Docker Compose project (`belfry`) with every service Belfry runs. Locally each service is
published on its own port; a public deployment adds a reverse proxy in front and publishes nothing
else (`DEPLOY.md`).

## Run

```
make up           # first run: writes infra/.env, infra/secrets/ and the keys, builds the signer
                  # (about 15 minutes); starts the core services
make bootstrap    # creates the Odoo database, users, API tokens, the signer's admin and policy,
                  # applies Authentik's blueprints; writes the tokens into infra/secrets/
make up-app       # builds and starts the onboarding service and the community apps
make smoke        # end-to-end test (see "Checks")
make logs         # follow the logs
make down         # stop, keep the data
```

Without `make`: `infra/scripts/gen-secrets.sh`, then
`docker compose -f infra/docker-compose.yml up -d`, `infra/scripts/bootstrap.sh`,
`docker compose -f infra/docker-compose.yml --profile app up -d --build`.

The onboarding service and the apps are in the Compose profile `app`, because they need the
tokens bootstrap writes. Restart them after bootstrap changed a token (`make up-app`).

Host requirements: Docker with Compose v2, `curl`, `jq`, `python3`, `openssl`; about 6 GB of RAM
while running (Odoo 2 GB, Authentik about 1 GB, the walt.id services 1 GB each, the rest small),
more while images build.

## Services

| Service | Local URL | What it is |
| --- | --- | --- |
| `odoo` | http://localhost:8069 | registry and billing; `admin` / `ODOO_ADMIN_PASSWORD` |
| `authentik-server`, `authentik-worker` | http://localhost:9000 | login; admin `akadmin` / `AUTHENTIK_BOOTSTRAP_PASSWORD` at `/if/flow/default-authentication-flow/?next=/if/admin/` |
| `keycast` | http://localhost:3000 | the signer and the participants' account page (`keycast/README.md`) |
| `keycast-relay` | ws://localhost:3080 | the relay the signer answers NIP-46 requests on |
| `relay` | ws://localhost:3090 | the community relay, participants only (`relay/README.md`) |
| `blossom-gate` → `blossom` | http://localhost:3100 | file uploads for participants (`blossom/README.md`) |
| `mint` | http://localhost:3338 | the village token mint (`mint/README.md`) |
| `issuer`, `verifier` | http://localhost:7005, http://localhost:7004 | walt.id Issuer2 and Verifier2 for the pass certificate (`issuer/README.md`); their management APIs have no authentication |
| `onboarding` | http://localhost:8100 | the onboarding service (`../services/onboarding/README.md`); also serves `brand/` at `/brand` |
| `calendar` | http://localhost:3200 | community calendar (`apps/calendar/README.md`) |
| `chat` | http://localhost:3300 | community chat (`apps/chat/README.md`) |
| `wallet` | http://localhost:3400 | village token wallet (`apps/wallet/README.md`) |
| `vote` | http://localhost:3500 | anonymous polls (`../services/vote/README.md`) |
| `village` | http://localhost:3600, :3700, :3800 | directory, marketplace, photos (`../services/village/README.md`) |
| `mailpit` | http://localhost:8025 | catches every mail the local stack sends (not used in production) |
| `postgres`, `redis` | internal | databases of Odoo, Authentik and Keycast; Keycast's cache. `docker compose exec postgres psql -U postgres` |

One-shot helpers: `keycast-migrate` (database migrations before Keycast starts), `relay-init`
(seeds the relay's configuration), `perms-init` (hands the data volumes to the non-root users).

Networks: `data` (internal) connects Postgres and Redis with their clients only; `edge` connects
the proxy with the browser-facing services; `default` is everything else.

## This directory

| Path | What |
| --- | --- |
| `docker-compose.yml` | the stack; settings come from `.env`, secrets from `secrets/` |
| `docker-compose.prod.yml` | the public overlay: proxy, no published ports, restart policies (`DEPLOY.md`) |
| `.env.example` | every setting with a comment (no secrets); `gen-secrets.sh` turns it into `.env` |
| `authentik/` | blueprints (flows, providers, sources, branding) and the mail template |
| `keycast/` | the signer's build (upstream plus Belfry's patch series) and its policy |
| `apps/` | builds of the chat, calendar and wallet: upstream apps at a pinned commit plus Belfry's patches |
| `issuer/`, `verifier/` | walt.id configuration and the issuer's keys |
| `relay/`, `blossom/`, `mint/` | configuration of the community relay, the file server and the mint |
| `odoo/` | Odoo's configuration template |
| `postgres/` | creates the databases and roles on first start |
| `proxy/` | the public reverse proxy (Caddy with rate limiting) |
| `hetzner/` | creating a VM on Hetzner Cloud |
| `scripts/` | setup, bootstrap, checks, backups, the custody seed (below) |
| `secrets/` | every password, token and key, one file each (below); gitignored |
| `secrets-env/` | `with-secrets.sh`, the entrypoint wrapper for images that read secrets from variables only |

## Secrets

Settings (URLs, names, ports) are in `infra/.env`. Every secret is a file of its own in
`infra/secrets/`, named after its variable in lower case (`ODOO_API_KEY` is
`secrets/odoo_api_key`). Neither is in git, and no secret goes into git encrypted either.

How a container gets a secret:

- Compose mounts the files a service needs, and only those, at `/run/secrets/<name>` (the
  `secrets:` lists in `docker-compose.yml`). No secret is an environment variable of a container,
  so `docker inspect`, `docker compose config` and crash reports show none.
- The files are mode 640 with group 10001. `perms-init` sets that at every start (also for the
  rendered issuer and verifier configs with their keys), and every container that reads a secret
  carries group 10001 (`group_add`). Other users on the host cannot read them.
- Belfry's own services read the files themselves (the onboarding service through pydantic's
  `secrets_dir`, the Node services for `PASS_INFO_TOKEN`). Authentik reads `file://` values and
  its blueprints use `!File`. Images that know only environment variables (Odoo, the signer, the
  mint, authentik's first-start bootstrap) start through `/belfry/with-secrets`
  (`secrets-env/with-secrets.sh`): it exports the files named in `SECRETS_ENV` for that process
  only. A command run with `docker compose exec` in such a container needs the wrapper too:
  `$C exec odoo /belfry/with-secrets odoo shell -d odoo`.
- The host scripts (bootstrap, smoke, the issuer PKI) load the files with `scripts/secrets.sh`
  and pass tokens to `curl` on standard input or as header files, not as arguments, which every
  user could read in `ps`.

`scripts/gen-secrets.sh` writes, once:

- `infra/.env` from `.env.example`. Run again, it keeps existing values and only appends settings
  that `.env.example` gained;
- `infra/secrets/<name>` with a random value for every generated secret (the signer's server key
  and admin key come from `scripts/nostr_keys.py`; the admin's public key is a setting in `.env`),
  and empty files for the ones written later (below), so Compose can mount them. A secret that
  exists is kept; delete its file to generate it anew. A secret still found in `.env` (an older
  setup) is moved into its file and removed from `.env`;
- `infra/keycast/master.key`, which encrypts every key the signer stores (back it up);
- `infra/odoo/odoo.conf` from `odoo/odoo.conf.tmpl`, with a PBKDF2 hash of the Odoo master
  password (the password itself is only in `secrets/odoo_master_password`);
- the issuer's CA, signing, bridge and verifier keys (`scripts/gen-issuer-pki.sh`,
  `issuer/README.md`);
- locally, the custody seed participant keys derive from (`secrets/custody/custody-seed`; on a
  server it is unlocked into memory instead, see `DEPLOY.md`).

An empty variable in `.env` must not carry an inline comment (`VAR=   # note`): Compose would
read the comment as the value.

Written later, into `infra/secrets/`:

| File | What | Written by | Lifetime |
| --- | --- | --- | --- |
| `odoo_api_key` | API key of the Odoo service user `onboarding@belfry.local` | bootstrap | permanent |
| `keycast_admin_token` | the signer's admin token, used to create participant accounts | bootstrap | 30 days; run bootstrap again |
| `authentik_token` | Authentik's API token | bootstrap | permanent |
| `stripe_webhook_secret` | signing secret of the Stripe dispute webhook | bootstrap, with Stripe keys | until the webhook is deleted |
| `smtp_password`, `stripe_secret_key`, `mollie_api_key` | provider credentials | `scripts/put-secret.sh` | yours |

Restart the onboarding service after bootstrap wrote a token (`$C up -d`).

## What bootstrap does

Every step checks before it changes anything, so bootstrap can run any number of times (a run on
a set-up stack takes seconds). Run it again after changing public URLs, SMTP settings or anything
in `authentik/` or `keycast/policy.sql`.

1. Waits for Odoo, Authentik, Keycast, the issuer and the verifier.
2. Creates the Odoo database with the module `belfry_pass`, or upgrades the module when its
   version changed.
3. Sets Odoo's admin password, creates the service user with the rights it needs and its API key,
   the outgoing mail server, company name, currency (on a fresh database) and logo, and the
   configuration the module reads (onboarding URL, webhook token, public URLs).
4. Signer: registers its admin, fetches the admin token, applies the policy `belfry-event` (the
   Nostr event kinds the apps may sign, `keycast/policy.sql`) and registers Belfry's apps as
   OAuth clients with their redirect URIs.
5. Authentik: applies and checks the blueprints: the pass sign-in source, the signer's OIDC
   provider, the participants' sign-in link and magic-link flows (and writes the SMTP settings
   into them), the branding, and the applications for the steward pages, the pass page and the
   issuer.

## Images built here

| Image | Built from |
| --- | --- |
| `belfry-keycast:local` | upstream Keycast at a pinned commit plus `keycast/build/patches/` (about 15 minutes cold; `make build-keycast` rebuilds it, `up` never does on its own once it exists) |
| `belfry-chat:local`, `belfry-calendar:local`, `belfry-wallet:local` | upstream apps at a pinned commit plus `apps/<app>/patches/`, with the deployment's URLs compiled in: rebuild after changing public URLs |
| `belfry-onboarding:local`, `belfry-vote:local`, `belfry-village:local` | `services/*` |
| the proxy | `proxy/Dockerfile`: Caddy with the rate-limit module (public deployment only) |

## Checks

| Check | What it covers |
| --- | --- |
| `make smoke` (`scripts/smoke.sh`) | the whole path against the running stack: invite, emulated card payment, activation exactly once, Authentik user, signer account, relay access, profile, activation mail, sign-in link, magic link, the signer's sign-in and single sign-on, policy kinds, NIP-46, the apps' presence, tokens, the pass certificate into a scripted wallet, door check, sign-in with the pass, the pass on the phone, key recovery, self-custody, revocation, webhook replays. On a server: `DEPLOY.md` |
| `make test-onboarding`, `make test-odoo` | unit tests of the onboarding service and the Odoo module |
| `npm test` in `services/vote`, `services/village` | unit tests of the polls and the village apps |
| `scripts/*-check.mjs` | browser checks in headless Chromium (chat, calendar, wallet, vote, village, photos); each script's header says what it needs |
| `scripts/silent-signin-check.sh`, `self-custody-check.sh`, `keycast-oidc-login.sh` | single sign-on, self-custody and the signer's sign-in, driven with curl (used by the smoke test) |

`SMOKE_KEEP_PASS=1 SAVE_AK_JAR=<file> make smoke` leaves the test participant active and saves
its Authentik session for the browser checks.

## Notes

- **Odoo** reads its database credentials from `PGHOST`/`PGUSER`/`PGPASSWORD`, so
  `docker compose exec odoo odoo shell -d odoo` works without arguments. The service user talks
  JSON-RPC (`/jsonrpc`), which Odoo 19 marks as deprecated in favour of `/json/2/<model>/<method>`.
- **Authentik** applies the blueprints in `authentik/blueprints/` (mounted at
  `/blueprints/custom`) on its own when a file changes; bootstrap re-applies the ones that depend
  on secrets. Participants have no passwords: the login page asks for an e-mail address and mails
  a one-time link (valid 10 minutes, at most five per account in 30 minutes), or offers the pass
  (`../docs/pass-sign-in.md`). The link must be opened in the browser that started the sign-in.
- **Branding**: every participant-facing page carries the Open Source Village look from `brand/`
  (`../brand/README.md`).
