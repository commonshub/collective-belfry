# The Belfry stack

One Docker Compose project (`belfry`) with every service Belfry runs. Locally each service is
published on its own port; a public deployment adds a reverse proxy in front and publishes nothing
else (`DEPLOY.md`).

## Run

```
make up           # first run: writes infra/.env and the keys, builds the patched signer
                  # (about 15 minutes); starts the core services
make bootstrap    # creates the Odoo database, users, API tokens, the signer's admin and policy,
                  # applies Authentik's blueprints; writes the tokens into infra/.env
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
| `docker-compose.yml` | the stack; every setting comes from `.env` |
| `docker-compose.prod.yml` | the public overlay: proxy, no published ports, restart policies (`DEPLOY.md`) |
| `.env.example` | every variable with a comment; `gen-secrets.sh` turns it into `.env` |
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
| `secrets/` | files that must not be in `.env` (SMTP password, the local custody seed); gitignored |

## Secrets and tokens

`scripts/gen-secrets.sh` writes, once:

- `infra/.env` from `.env.example`, with random passwords and keys (the signer's server key and
  admin key pair come from `scripts/nostr_keys.py`). Run again, it keeps existing values and only
  appends variables that `.env.example` gained;
- `infra/keycast/master.key`, which encrypts every key the signer stores (back it up);
- `infra/odoo/odoo.conf` from `odoo/odoo.conf.tmpl` (with the Odoo master password);
- the issuer's CA, signing, bridge and verifier keys (`scripts/gen-issuer-pki.sh`,
  `issuer/README.md`);
- locally, the custody seed participant keys derive from (`secrets/custody/custody-seed`; on a
  server it is unlocked into memory instead, see `DEPLOY.md`).

All of these are gitignored. An empty variable in `.env` must not carry an inline comment
(`VAR=   # note`): Compose would read the comment as the value.

`scripts/bootstrap.sh` creates three tokens and writes them into `.env`:

| Variable | What | Lifetime |
| --- | --- | --- |
| `ODOO_API_KEY` | API key of the Odoo service user `onboarding@belfry.local` | permanent |
| `KEYCAST_ADMIN_TOKEN` | the signer's admin token, used to create participant accounts | 30 days; run bootstrap again |
| `AUTHENTIK_TOKEN` | Authentik's API token | permanent |

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
