# The Belfry stack

One Docker Compose project (`belfry`) with every service Belfry runs. Locally each service is
published on its own port; a public deployment adds a reverse proxy in front and publishes nothing
else (`DEPLOY.md`).

Since 2026-11-19 Belfry signs people in and holds their Nostr keys with its own code: the
onboarding service is the sign-in (login page, account page, the apps' "Sign in with Open Source
Village") and `signer` (`../services/signer`, Rust) holds the keys. Keycast and Authentik are gone;
a deployment from before moves with `scripts/migrate-from-keycast.sh` ("Moving from Keycast and
Authentik" below).

## Run

```
make up           # first run: writes infra/.env, infra/secrets/ and the keys, builds the signer
                  # (a few minutes); starts the core services
make bootstrap    # creates the Odoo database, users and API token, checks the signer;
                  # writes the token into infra/secrets/
make up-app       # builds and starts the onboarding service and the community apps
make smoke        # end-to-end test (see "Checks")
make logs         # follow the logs
make down         # stop, keep the data
```

Without `make`: `infra/scripts/gen-secrets.sh`, then
`docker compose -f infra/docker-compose.yml up -d`, `infra/scripts/bootstrap.sh`,
`docker compose -f infra/docker-compose.yml --profile app up -d --build`.

The onboarding service and the apps are in the Compose profile `app`, because they need the
token bootstrap writes. Restart them after bootstrap changed a token (`make up-app`).

Host requirements: Docker with Compose v2, `curl`, `jq`, `python3`, `openssl`; about 5 GB of RAM
while running (Odoo 2 GB, the walt.id services 1 GB each, the rest small), more while images
build.

## Services

| Service | Local URL | What it is |
| --- | --- | --- |
| `odoo` | http://localhost:8069 | registry and billing; `admin` / `ODOO_ADMIN_PASSWORD` |
| `onboarding` | http://localhost:8100 | the onboarding service (`../services/onboarding/README.md`): the sign-in (`/login`), the account page (`/account`), the apps' sign-in (`/api/oauth/*`, `/api/nostr`), NIP-05 names, the steward pages, claim pages and door; also serves `brand/` at `/brand` |
| `signer` | internal only | Belfry's signer (`../services/signer/README.md`): the members' keys, an internal API for the onboarding service and a NIP-46 bunker; its own network, no internet |
| `bunker-relay` | ws://localhost:3080 | the relay the signer answers NIP-46 requests on (`wss://bunker.<domain>`) |
| `relay` | ws://localhost:3090 | the community relay, participants only (`relay/README.md`) |
| `blossom-gate` → `blossom` | http://localhost:3100 | file uploads for participants (`blossom/README.md`) |
| `mint` | http://localhost:3338 | the village token mint (`mint/README.md`) |
| `issuer`, `verifier` | http://localhost:7005, http://localhost:7004 | walt.id Issuer2 and Verifier2 for the pass certificate (`issuer/README.md`); their management APIs have no authentication |
| `calendar` | http://localhost:3200 | community calendar (`apps/calendar/README.md`) |
| `chat` | http://localhost:3300 | community chat (`apps/chat/README.md`) |
| `wallet` | http://localhost:3400 | village token wallet (`apps/wallet/README.md`) |
| `vote` | http://localhost:3500 | anonymous polls (`../services/vote/README.md`) |
| `village` | http://localhost:3600, :3700, :3800, :3900 | directory, marketplace, photos, programme (`../services/village/README.md`) |
| `groups` | http://localhost:3150 | groups app, Flotilla (`apps/groups/README.md`) |
| `groups-relay` | ws://localhost:3190 | the groups relay, zooid with NIP-29, participants only (`groups-relay/README.md`) |
| `git` | http://localhost:3170 | the git server, ngit-grasp, participants only (`git/README.md`) |
| `mailpit` | http://localhost:8025 | catches every mail the local stack sends (not used in production) |
| `postgres` | internal | databases of Odoo and the groups relay. `docker compose exec postgres psql -U postgres` |

One-shot helpers: `relay-init` (seeds the relay's configuration), `perms-init` (hands the data
volumes to the non-root users, the signer's to the deploying user).

Networks: `data` (internal) connects Postgres with its clients only; `signer` (internal)
connects the signer with the onboarding service and the bunker relay only; `edge` connects the
proxy with the browser-facing services; `default` is everything else.

## How the services connect

What each part is for, the pass tiers and the main flows (paid, sign-in, apps, door, closed) are
in the plan's Architecture section (`../docs/identity-membership-plan.md`). This is the wiring
inside Compose: who calls whom, over the internal network.

| From | To | What for |
| --- | --- | --- |
| `odoo` | `onboarding` `/hooks/odoo/*` | invoice paid, refund, chargeback (webhooks with `HOOK_TOKEN`) |
| `onboarding` | `odoo` (JSON-RPC) | reads and writes passes, invoices, sign-in links and codes, stewards; Odoo then mails them |
| `onboarding` | `signer` (internal API, `SIGNER_TOKEN`) | creates accounts (the key derived from the custody seed), suspends and reactivates them, signs (profiles, the apps' HTTP RPC), derives the wallet's secrets, makes and revokes bunker connections, exports a key, moves an account to the member's own key |
| `signer` | `bunker-relay` | NIP-46: listens for requests to its connections' keys and answers them |
| `onboarding` | `relay` | renders the whitelist into the shared volume `community-relay-config` (rnostr reloads it); publishes profiles and relay lists |
| `onboarding` | `groups-relay` (NIP-98 member API) | member list and profile import |
| `onboarding` | `git` | writes `members.txt` into the shared volume `git-members` |
| `onboarding` | `mint` | mints the village tokens of a pass |
| `onboarding` | `issuer`, `verifier` (management APIs, no authentication) | credential offers for the claim page; verification sessions for the door and the pass sign-in |
| `verifier` | `onboarding` `/status/1` | the status list |
| `issuer` | `onboarding` `/oidc/token` | Issuer2's sign-in for authorization-code offers: the onboarding service is its OpenID provider (`issuer/config/authentication-service.conf`) |
| `blossom-gate` | `onboarding` `/blossom/auth`, then `blossom` | checks every upload for an active pass |
| `vote`, `village` | `onboarding` `/admin/active-passes` (`PASS_INFO_TOKEN`) | who has an active pass; `vote` also posts polls on `relay` |

From outside, browsers and wallets reach the services through the proxy, one hostname each
(`DEPLOY.md` lists them). The apps are static pages: from the browser they talk to the
onboarding service (`pass.<domain>`: sign-in, tokens, the wallet's HTTP RPC), the bunker relay
(NIP-46), the relays, `blossom-gate` and `mint`. `login.<domain>` and `apps.<domain>`, the
addresses of Authentik and Keycast, redirect to the login and account pages on `pass.<domain>`.

### Signing in

- **Members** sign in on `pass.<domain>/login` with their e-mail address: Odoo mails a one-time
  link and a 6-digit code (`belfry.login.code`, five mails per address in half an hour), or they
  show the pass from a wallet (OpenID4VP through `verifier`). The activation mail's link and the
  page after paying sign in once by themselves. The session is a signed cookie on `pass.<domain>`
  (`SESSION_SECRET`); only members with an active pass, and stewards, may sign in.
- **Apps** sign in with the same OAuth shape Keycast had (`/api/oauth/authorize` with PKCE,
  `/api/oauth/token`, `/api/oauth/revoke`, `/api/auth/oidc/logout`), so the app builds only point
  at `pass.<domain>` instead. Belfry's own apps (`OAUTH_CLIENTS` in `docker-compose.yml`) skip the
  consent screen and may sign in silently; other apps see it. The token response carries a
  `bunker://` URL for NIP-46 and an access token for the HTTP RPC (`/api/nostr`, the wallet).
  Which app may derive which secrets from the member's key is `DERIVE_SECRETS`.
- **Stewards** are Odoo contacts marked `belfry_steward` (`python -m belfry_onboarding
  add-steward <e-mail>`); they sign in like members and then open `/steward`.

## This directory

| Path | What |
| --- | --- |
| `docker-compose.yml` | the stack; settings come from `.env`, secrets from `secrets/` |
| `docker-compose.prod.yml` | the public overlay: proxy, no published ports, restart policies (`DEPLOY.md`) |
| `.env.example` | every setting with a comment (no secrets); `gen-secrets.sh` turns it into `.env` |
| `apps/` | builds of the chat, groups, calendar and wallet: upstream apps at a pinned commit plus Belfry's patches |
| `issuer/`, `verifier/` | walt.id configuration and the issuer's keys |
| `relay/`, `blossom/`, `mint/` | configuration of the community relay, the file server and the mint |
| `groups-relay/`, `git/` | builds of the groups relay (zooid) and the git server (ngit-grasp), upstream plus Belfry's patches |
| `odoo/` | Odoo's configuration template |
| `postgres/` | creates the databases and roles on first start |
| `proxy/` | the public reverse proxy (Caddy with rate limiting) |
| `hetzner/` | creating a VM on Hetzner Cloud |
| `scripts/` | setup, bootstrap, checks, backups, the custody seed, the move from Keycast (below) |
| `secrets/` | every password, token and key, one file each (below); gitignored |
| `secrets-env/` | `with-secrets.sh`, the entrypoint wrapper for images that read secrets from variables only |

The signer's code is `../services/signer`, not here.

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
  `secrets_dir`, the signer its token and master key, the Node services `PASS_INFO_TOKEN`).
  Images that know only environment variables (Odoo, the mint) start through
  `/belfry/with-secrets` (`secrets-env/with-secrets.sh`): it exports the files named in
  `SECRETS_ENV` for that process only. A command run with `docker compose exec` in such a
  container needs the wrapper too: `$C exec odoo /belfry/with-secrets odoo shell -d odoo`.
- The host scripts (bootstrap, smoke, the issuer PKI) load the files with `scripts/secrets.sh`
  and pass tokens to `curl` on standard input or as header files, not as arguments, which every
  user could read in `ps`.

`scripts/gen-secrets.sh` writes, once:

- `infra/.env` from `.env.example`. Run again, it keeps existing values and only appends settings
  that `.env.example` gained;
- `infra/secrets/<name>` with a random value for every generated secret, and empty files for the
  ones written later (below), so Compose can mount them. A secret that exists is kept; delete its
  file to generate it anew. A secret still found in `.env` (an older setup) is moved into its
  file and removed from `.env`;
- `secrets/signer_master_key`, the key that seals every member key the signer stores (32 bytes,
  base64, Keycast's `master.key` format; back it up). On a deployment from before 2026-11-19 it
  is taken over from `infra/keycast/master.key`, so the keys moved out of Keycast open with it;
- `infra/odoo/odoo.conf` from `odoo/odoo.conf.tmpl`, with a PBKDF2 hash of the Odoo master
  password (the password itself is only in `secrets/odoo_master_password`);
- the issuer's CA, signing, ID-token and verifier keys, and Issuer2's sign-in configuration
  (`scripts/gen-issuer-pki.sh`, `issuer/README.md`);
- locally, the custody seed participant keys derive from (`secrets/custody/custody-seed`; on a
  server it is unlocked into memory instead, see `DEPLOY.md`).

The signer's own secrets: `signer_token` (the onboarding service at its API) and
`signer_master_key`. The sign-in's: `session_secret` (sessions and the apps' tokens),
`steward_session_secret`, `pass_page_secret`, `issuer_oidc_client_secret` (Issuer2's sign-in).

An empty variable in `.env` must not carry an inline comment (`VAR=   # note`): Compose would
read the comment as the value.

Written later, into `infra/secrets/`:

| File | What | Written by | Lifetime |
| --- | --- | --- | --- |
| `odoo_api_key` | API key of the Odoo service user `onboarding@belfry.local` | bootstrap | permanent |
| `stripe_webhook_secret` | signing secret of the Stripe dispute webhook | bootstrap, with Stripe keys | until the webhook is deleted |
| `smtp_password`, `stripe_secret_key`, `mollie_api_key` | provider credentials | `scripts/put-secret.sh` | yours |

Restart the onboarding service after bootstrap wrote a token (`$C up -d`).

## What bootstrap does

Every step checks before it changes anything, so bootstrap can run any number of times (a run on
a set-up stack takes seconds). Run it again after changing public URLs or SMTP settings.

1. Waits for Odoo, the issuer and the verifier.
2. Creates the Odoo database with the module `belfry_pass`, or upgrades the module when its
   version changed.
3. Sets Odoo's admin password, creates the service user with the rights it needs and its API key,
   the outgoing mail server (Odoo mails the sign-in links and codes), company name, currency (on
   a fresh database) and logo, and the configuration the module reads (onboarding URL, webhook
   token, public URLs, the login and account pages).
4. Checks that the signer is up and whether the custody seed is unlocked.

The sign-in needs no setup: the apps' OAuth clients are `OAUTH_CLIENTS` in `docker-compose.yml`,
and the stewards are Odoo contacts (`add-steward`).

## Images built here

| Image | Built from |
| --- | --- |
| `belfry-signer:local` | `../services/signer` (Rust, a few minutes cold; `make build-signer`, `make up` rebuilds it) |
| `belfry-chat:local`, `belfry-groups:local`, `belfry-calendar:local`, `belfry-wallet:local` | upstream apps at a pinned commit plus `apps/<app>/patches/`, with the deployment's URLs compiled in: rebuild after changing public URLs |
| `belfry-onboarding:local`, `belfry-vote:local`, `belfry-village:local` | `services/*` |
| `belfry-groups-relay:local`, `belfry-git:local` | zooid and ngit-grasp at a pinned commit plus `groups-relay/patches/`, `git/patches/` |
| the proxy | `proxy/Dockerfile`: Caddy with the rate-limit module (public deployment only) |

The apps' patches still name their build arguments after Keycast (`VITE_KEYCAST_URL`,
`BELFRY_KEYCAST_URL`, `KEYCAST_APP_URL`); Compose sets them to the onboarding service's public
URL, which serves the same sign-in paths.

## Checks

| Check | What it covers |
| --- | --- |
| `make smoke` (`scripts/smoke.sh`) | the whole path against the running stack: invite, emulated card payment, activation exactly once, the signer account, relay access, profile, activation mail, sign-in link, account page, the apps' sign-in (own apps silently, outside apps with consent), the wallet's derived secrets (byte for byte Keycast's), e-mail link and code, logout, NIP-46 through the bunker relay, the apps' presence, an agent with a bunker URL, the git server, stewards, tokens, the pass certificate into a scripted wallet, Issuer2's sign-in, door check, sign-in with the pass, the pass on the phone, key recovery from the seed, self-custody, revocation, webhook replays. On a server: `DEPLOY.md` |
| `make test-onboarding`, `make test-odoo`, `make test-signer` | unit tests of the onboarding service, the Odoo module and the signer |
| `npm test` in `services/vote`, `services/village` | unit tests of the polls and the village apps |
| `scripts/*-check.mjs` | browser checks in headless Chromium (chat, calendar, wallet, vote, village, photos); each script's header says what it needs |

`SMOKE_KEEP_PASS=1 SAVE_SESSION_JAR=<file> make smoke` leaves the test participant active and
saves its session (a curl cookie jar for `pass.<domain>`) for the browser checks.

## Moving from Keycast and Authentik

A deployment from before 2026-11-19 moves once, every npub staying the same:

```
git pull                                  # this version
infra/scripts/gen-secrets.sh              # signer_master_key from infra/keycast/master.key, new secrets,
                                          # Issuer2's sign-in re-rendered
make up && make bootstrap && make up-app  # the signer; the Odoo module renames keycast_pubkey to nostr_pubkey
infra/scripts/migrate-from-keycast.sh     # keys out of Keycast, accounts from Odoo, stewards out of Authentik
make smoke
infra/scripts/migrate-from-keycast.sh --remove            # the old containers and volumes
infra/scripts/migrate-from-keycast.sh --drop-databases    # after a backup: the keycast and authentik databases
```

The script copies every key Keycast holds into the signer as it is (sealed with the same master
key), so keys from before the custody seed, which the seed cannot give back, are kept too; they
live in the signer's data (`signer-data`, in the backups). Existing bunker connections end:
Belfry's apps sign in again by themselves, outside apps are connected again from the account
page. Rebuild the apps, which have the sign-in URL compiled in:
`docker compose -f infra/docker-compose.yml --profile app build chat calendar groups wallet vote village`,
then `make up-app`.

## Notes

- **Odoo** reads its database credentials from `PGHOST`/`PGUSER`/`PGPASSWORD`, so
  `docker compose exec odoo odoo shell -d odoo` works without arguments. The service user talks
  JSON-RPC (`/jsonrpc`), which Odoo 19 marks as deprecated in favour of `/json/2/<model>/<method>`.
- **Sign-in mails** come from Odoo (template `mail_template_login_code`); the link signs in any
  browser that opens it, once, within ten minutes; the code is typed in the browser that asked.
- **The signer** reads the custody seed only to create or recreate accounts; the keys it holds
  are sealed with `signer_master_key`, so signing goes on while the seed is locked (after a
  reboot) and only new accounts wait.
- **Branding**: every participant-facing page carries the Open Source Village look from `brand/`
  (`../brand/README.md`).
