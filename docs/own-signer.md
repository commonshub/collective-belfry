# Belfry's own signer and sign-in

Belfry runs two pieces of other people's software in its most sensitive place: **Keycast**
(Divine's signer, with a series of 23 Belfry patches that must be rebased on every upstream
change and takes 20 to 30 minutes to build on a small box) and **Authentik** (the login, which
the `belfry` look no longer shows to anyone but still runs and still gets a user per member).
The plan is to replace both with Belfry's own code, in phases, without signing anybody out and
without changing anybody's key.

- Phase 1 (branch `own-signer`): the signer exists, does what the product needs, and the
  onboarding service can run against either signer.
- Phase 2 (same branch, section 1b): with `SIGNER=belfry` a belfry needs **neither Keycast nor
  Authentik nor Redis**. A new belfry can be installed that way; an existing one cannot switch
  yet. **Nothing in production changes**: the default is still `SIGNER=keycast`, unchanged.
- Phase 3: the migration of a live belfry (section 6: the script, a rehearsal, the switch of
  one belfry), then the pages Keycast had that Belfry does not have yet (section 5).

The signer is `services/signer` (Rust), first written by Friedger for OSV in PR #7. Its README
is the reference for keys, connections, the API and the bunker; this document is the plan.

## 1. What phase 1 delivers

1. **The signer** with what `belfry-sh` relies on in Keycast, and the findings of the review of
   PR #7 fixed (section 2).
2. **`clients/signer.py`**: `SignerClient`, with the methods, arguments, return shapes and
   exceptions of `KeycastClient`. `app.build_signer` picks one by the setting `SIGNER`
   (`keycast`, the default, or `belfry`); the rest of the service keeps calling
   `services.keycast.…` and does not change.
3. **Tests against both**: `tests/fakes.py` has `FakeSigner` next to `FakeKeycast` with the
   same books; `BELFRY_TEST_SIGNER=belfry` runs the whole suite against the new signer's
   client and fake; `tests/test_signer_contract.py` runs one set of assertions against both
   clients, and against a real `belfry-signer` when `SIGNER_LIVE_URL` and `SIGNER_LIVE_TOKEN`
   are set (that run is what keeps the fake honest).
4. **Compose**: `infra/docker-compose.signer.yml`, an override nobody includes yet. The base
   file is untouched: no belfry starts the signer.
5. Two small moves off Keycast's web side, active only with `SIGNER=belfry`: the onboarding
   service answers NIP-05 for usernames (`mail.py`, `member_name`), and the status page checks
   the signer's `/health` (`console.py`, `checks`).

What phase 1 does **not** do: migrate anything, remove Keycast or Authentik, change the proxy,
the installer or the `belfry` command, or touch what an app sees.

## 1b. What phase 2 delivers

**The switch is one setting.** `SIGNER=belfry` means: Belfry's own signer, no Authentik
(`stewards.own_mode`). The service refuses to start with `PASS_SIGNIN=authentik` next to it, and
with `SIGNER=keycast` when a token of Authentik or Keycast is missing (`stewards.check_settings`;
those three settings are no longer required otherwise). `services.authentik` is then an object
that raises on any use, so a forgotten caller fails in the tests, not silently in production.
It works with both registers; with `REGISTER=odoo` the Odoo module must be upgraded first (the
new field `res.partner.belfry_steward`; **not run against a real Odoo**).

| | What | Where |
| --- | --- | --- |
| B1 | **Admins live in the register**: `res.partner.belfry_steward`. One set of functions for both homes: `stewards.emails`, `is_steward`, `remove`, `mark`; `steward.add_steward` makes the membership and sets the flag. The callers (console, setup, reports, feedback, shifts, `/api/me`, invites) call these and nothing else. An Authentik id kept from before a migration is never read in this mode | `belfry_onboarding/stewards.py`, `steward.py` `add_steward`, `console.py` `admin_emails` / `set_admin`, `discovery.py`, `holder_invites.py` |
| B2 | **No Authentik user** is made, updated or looked up: `core._ensure_authentik_user`, `_set_participant`, `sync_participants`, `_authentik_user_for` do nothing in this mode; a pass keeps `authentik_user_id` empty | `core.py` |
| B3 | **One-time import** of Authentik's stewards group into the register, matched by e-mail; an admin with no person here is reported, not created. `python -m belfry_onboarding import-stewards` (needs `AUTHENTIK_URL`, `AUTHENTIK_TOKEN`; safe to run again) | `stewards.import_from_authentik`, `__main__.py` |
| B4 | **The steward pages** (`/steward`: reports, feedback, shifts, invites) sign in with the pass page's own sign-in (e-mail and code): `/steward/login` takes that session when its person is an admin | `steward.py` `own_login`, `passlogin.TARGETS` |
| B5 | **First run and reset** work without Authentik: the first admin is a person with the flag; their first session is bound to the browser's key when it is made; `has_admin` reads the register | `setup.py`, tests `test_setup.py`, `test_opensetup.py` in belfry mode |
| B6 | **Keycast's sign-in for apps, answered by Belfry**: `/api/oauth/authorize`, `/token`, `/revoke`, `/api/auth/oidc/logout`, `/api/nostr` on `apps.<domain>` (section 1c) | `belfry_onboarding/appsignin.py` |
| B7 | **Signing limits for outside apps**, one rule set in two places held to the same vectors: the signer for NIP-46 connections of scope `outside` (`limits.rs`), the service for `/api/nostr` and for remote apps (`remoteapps.allowed`). Vectors: `services/signer/vectors/limits.json` | `services/signer/src/limits.rs`, `bunker.rs`, `remoteapps.py`, `connect.scope_of` |
| B8 | **Bunkers bound where the client key is known**: the first admin's session; an app may send `client_pubkey` with its token exchange (`/connect/token` and `/api/oauth/token`). Otherwise the ten-minute window starts when the app receives the URL (section 4) | `setup.py`, `connect.redeem` |
| B9 | **`POST /import/connections`** in the signer: Keycast's sessions with their bunker keys (section 6) | `services/signer/src/signer.rs` `import_connections`, `keys.rs`, README "Carrying Keycast's sessions over" |
| B10 | **The stack**: compose files, the proxy, the `belfry` command, the installer, secrets, backups, the image workflow (section 5, "moved in phase 2") | `infra/docker-compose.signer.yml`, `signer-prod.yml`, `images-signer.yml`, `infra/proxy/Caddyfile`, `infra/scripts/{belfry,bootstrap.sh,gen-secrets.sh,backup.sh,signer-smoke.py}`, `install.sh`, `.github/workflows/images.yml` |
| B11 | **Tests in both modes**: `tests/test_own_mode.py` forces a belfry on its own signer in every run; `BELFRY_TEST_SIGNER=belfry` runs the whole suite as one (the tests about Authentik itself are skipped there, marked `authentik_only`; the Authentik fake is replaced by handles over the register's flag) | `tests/conftest.py`, `tests/fakes.py` `RegisterStewards` |

### 1c. Keycast's sign-in for apps (`appsignin.py`)

The apps of the Open Source Village suite and any app written against a Keycast belfry keep
their five addresses on `apps.<domain>`. What happens behind them is Belfry's own:

- `GET /api/oauth/authorize` is a redirect to `belfry.<domain>/connect/authorize` with the same
  query: one sign-in (connect.py), one registry of apps (apps.py). The suite's own apps
  (`calendar.<domain>`, `wallet.<domain>`, … when they run here: `OSV_APPS=1` or `EXTRA_APPS`)
  count as the belfry's own in this mode (`apps.own_app`, `appsignin.suite_name`), as Keycast's
  trusted clients did; any other app needs an admin's approval.
- `POST /api/oauth/token`: `authorization_code` (the code of `/connect`: once, five minutes,
  PKCE S256, same client and return address) gives `bunker_url`, `access_token`,
  `refresh_token`, `expires_in`, `pubkey`; `refresh_token` gives new tokens.
- `POST /api/nostr` with the access token: `get_public_key`, `sign_event`, `nip44_*`,
  `nip04_*`, and `derive_secret` for the wallet the belfry serves (`cashu-wallet-v1`,
  `cashu-nutzap-v1`) and nobody else.
- `POST /api/oauth/revoke`, `GET /api/auth/oidc/logout?return_to=`.

The findings of the review of PR #7's first version, and what answers each:

| Finding | Here |
| --- | --- |
| An unregistered `client_id` could be redirected to | An address is redirected to only when it is in the registry; a silent sign-in (`prompt=none`) for anything else is an error page (this was also open in `/connect` and is closed there); the logout returns only to an approved address |
| PKCE optional | S256 required at `/connect/authorize` (400 otherwise) |
| Codes reusable or long-lived | One use, five minutes, in memory (`connect.CODES`) |
| Refresh tokens never expired | 30 days; replaced at every use; one used twice ends the whole sign-in, in the signer too |
| Tokens and sessions not revocable | Tokens are random and kept as HMACs in `appsignin.sqlite` next to the register; every use checks the row, the app's approval, the member's pass and key, and that the session is still in the signer's list. A browser session of `belfry.<domain>` carries an id (`sid`) in this mode and a logout writes it down (`ended`) |
| Consent page showed text from the URL | The pages are connect.py's: an app is named by its address or the name an admin gave it |
| No CSRF protection | The sign-in's POSTs carry the flow's token and are refused from another origin (connect.py); the token endpoints read no cookie |
| One signing key for every token | No signatures: a key per kind of token for the HMACs, HKDF of `PASS_PAGE_SECRET` with the labels `token/access` and `token/refresh` |

Not Keycast's: the logout is a GET (the apps navigate to it), so another site can sign a
browser out of `belfry.<domain>` (a nuisance, nothing more). Sessions of a belfry with Keycast
keep their cookies as they were (no `sid`).

## 2. The signer: what was added and fixed

| | What | Where |
| --- | --- | --- |
| A1 | A connection can be made for a client key: bound from the start, no secret kept. One client key, one identity: refused for another account at creation (409 `client_key_taken`) and on a first `connect`; a first connect records the key; revoking frees nothing (Keycast patches 0022, 0023) | `signer.rs` `create_connection`, `store.rs` `add_connection` / `bind_client` / `client_of_another`, `bunker.rs` `handle` |
| A2 | Connections carry the application's name, client key, created and last-used times and a use counter; list them, revoke one by its bunker key | `store.rs` `ConnectionRow`, `revoke_by_bunker`; `api.rs` `list_connections`, `revoke_connections` |
| A3 | Usernames: one per account, unique whatever the case, set (409 `username_taken`), read with the account, name to account | `store.rs` `set_username`, `account_by_username`; `signer.rs` `valid_username`; `api.rs` `set_username`, `name` |
| A4 | No user tokens: the API's one caller names the account in the path. The Python client's "token" is `acct:<vine_id>` | `clients/signer.py` `_account` |
| A5 | NIP-46: `connect`, `get_public_key`, `sign_event`, `nip44_*`, `nip04_*`, `ping`, plus `get_relays` and `switch_relays`; answers are `{"id","result"}` or `{"id","result":"","error"}`; a NIP-04 request gets a NIP-04 answer | `bunker.rs` `handle`, `respond` |
| A6 | Connection secrets are stored as SHA-256 only, compared in constant time, and dropped once a client is bound; an unclaimed connection ends after 10 minutes; `connect` again from the bound client always answers `ack` | `signer.rs` `secret_hash`, `store.rs` `LIVE`, `expire_unclaimed`, `bunker.rs` `handle` |
| A7 | The bunker loop is supervised (back-off 1 s to 60 s, an error in the log); `/health` reports the bunker and no longer counts accounts; requests are answered 16 at a time within a rate limit per connection; unknown bunker keys are dropped before any crypto; the relay is asked for the live bunker keys only (`#p`), again when the set changes, and the API waits until the subscription covers a new connection; look-back of 60 s and one answer per event | `bunker.rs` `supervise`, `session`, `Subscription::renew`, `filters_for`, `Seen`, `respond`; `signer.rs` `Limiter`, `BunkerState`, `wait_subscribed`; `api.rs` `health` |
| A8 | Sealed keys are bound to their row (the account id or connection id is AES-GCM associated data) and, once open, compared with the row's public key. Keycast's format opens only on import and in a one-time upgrade at start | `vault.rs`, `signer.rs` `keys_of`, `bunker_keys`, `upgrade_seals`, `import_sealed` |
| A9 | Creating an account never changes one: an expected or own public key that differs is 409 `mismatch`, nothing written. An import that names another key for an existing account is an error. The move to an own key is a call of its own | `signer.rs` `ensure_account`, `import_sealed`, `move_to_own_key`; `api.rs` `own_key` |

Decisions taken in A6: a connection made for a client key keeps no secret at all (the secret in
its URL is there because apps expect the parameter); for the other kind the secret is spent at
the first `connect`. In both cases the client key is the credential afterwards, so an app that
reconnects and sends `connect` again keeps working.

**Signing policy (phase 2).** A connection has a scope, given when it is made: `own` (the
belfry's own apps: the chat, the phone apps, the suite; they sign anything for their account)
or `outside` (every other app, approved or not). An outside connection is never given a
signature for: NIP-98 (27235) whose `u` is the belfry's domain or any name under it, or has no
`u`; NIP-42 AUTH (22242) for one of the belfry's relays, or without a relay; kind 24133;
NIP-29 moderation and joins (9000 to 9009, 9021, 9022); Blossom auth (24242) for the belfry's
storage or for no server; kinds 0 and 3. That is `remoteapps._forbidden` with no perms; the
signer's copy (`limits.rs`) and the service's are held to the same 50 vectors. The limit is in
the signer because that is where a NIP-46 request arrives; the service applies the same
function to `/api/nostr`. Chosen against a facade-only limit: an app holding a bunker URL never
passes the service. A limit on `created_at` was considered and not added: outboxes and sealed
conversations sign events that are not from now.

Decision to confirm: kinds 0 and 3 are refused to an approved outside app too (the rule of
remote apps "only when the app said so" has no way to say so at `/connect`). An outside Nostr
client that edits the profile would need a perms field in the approval.

## 3. The signer's API

Internal network only, bearer token (`signer_token`). The full table is in
`services/signer/README.md`; in short:

```
GET  /health                                  {status, seed, bunker}           (no token)
PUT  /accounts/{vine_id}                      {key_version?, own_pubkey?, expect_pubkey?, username?}
GET  /accounts/{vine_id}                      -> {vine_id, pubkey, custody, key_version, status, username}
POST /accounts/{vine_id}/status               {status: active|suspended}
POST /pubkeys/{pubkey}/status                 {status}
POST /accounts/{vine_id}/own-key              {pubkey}
PUT  /accounts/{vine_id}/username             {username}
GET  /names/{username}                        -> {name, pubkey, vine_id}
POST /accounts/{vine_id}/sign                 {event}                          -> {event}
POST /accounts/{vine_id}/encrypt | /decrypt   {scheme: nip44|nip04, peer, text} -> {result}
POST /accounts/{vine_id}/derive               {purpose}                        -> {secret}
POST /accounts/{vine_id}/export                                                -> {nsec}
POST /accounts/{vine_id}/connections          {app, client_pubkey?, label?, derive?, keep?, scope?}
GET  /accounts/{vine_id}/connections
POST /accounts/{vine_id}/connections/revoke   {id | bunker_pubkey | app | all: true}
POST /import                                  {accounts: [{vine_id, pubkey, sealed}]}
POST /import/connections                      {vine_id, connections: [{bunker_pubkey,
                                               keycast_secret_hash, client_pubkey, app, …}]}
```

`scope` is `own` (default) or `outside`. New environment: `SIGNER_OWN_DOMAIN` and
`SIGNER_OWN_API_HOSTS`, `SIGNER_OWN_RELAY_HOSTS`, `SIGNER_OWN_BLOSSOM_HOSTS` (what an outside
connection never signs for; the compose file sets the domain and the relays' internal names).

Errors are `{"error", "message"}`: 401 `unauthorized`, 404 `not_found`, 503 `seed_locked`,
409 `own_key` / `mismatch` / `client_key_taken` / `username_taken`, 403 `suspended` /
`forbidden`, 422 `invalid`.

## 4. Differences from Keycast

What a caller of the Python client could notice is listed at the top of `clients/signer.py`.
The ones that matter for the product:

- **An unclaimed connection lives 10 minutes**, not 30 days. Every sign-in Belfry does itself
  names the client key (`sessions.py:279`, `guests.py:792,1183`, `events.py:1203`,
  `ticketsales.py:617`, `sales.py:2483`), so this meets the two made without: `setup.py:399`
  (the first admin's session, connected within seconds) and **`connect.py:247`, the `/connect`
  sign-in of approved apps** (OAuth code + PKCE: the app gets the bunker URL from the token
  exchange and has to send its first `connect` within ten minutes of it; an app that keeps the
  URL and connects later finds the session gone). Since phase 2: `setup.py` binds the first
  admin's session to the browser's key, and an app may send `client_pubkey` with its token
  exchange (`/connect/token`, `/api/oauth/token`) to get a session bound from the start. An
  app that does not has ten minutes **from the answer that carries the URL** (the session is
  made in that request, never earlier), and signs in again if it missed them: nothing is
  re-issued by itself. The suite's apps connect at once after the exchange. An agent "acting
  through a bunker URL" handed over by hand would have to connect within ten minutes.
- **A client key is never freed.** In Keycast the move to an own key deletes the account's
  authorizations and with them the record of its client keys; here revoked connections stay.
- **Statuses**: `active` and `suspended` only; `banned` is kept as `suspended`. The reason is
  not stored.
- **Session times** are seconds in the signer; the client writes them as ISO 8601 with `Z` and
  no fraction (`2026-11-05T00:00:00Z`). Keycast's exact text form (fraction, offset) is **not
  verified**, and the phone apps decode `/api/me/sessions` strictly: check one real answer of
  each before a switch.
- **No `redirect_origin`**: always empty. `discovery.chat_session` tells a chat session by its
  name and accepts an empty origin, so sealed conversations count the same sessions.
- **No web side in the signer**: the service answers OAuth for apps and `nostr.json` (phase 2);
  there are no account pages and no OIDC. See section 5.
- **`nostrconnect://`** is not answered (Keycast's flow was unusable for outside apps too);
  outside apps stay on `remoteapps.py`, which signs through `sign_event` / `nip44_*` of
  whichever client is in use.
- **`sign_event` fills in a missing `pubkey`**; Keycast refused the event.
- **Wallet secrets** (`derive_secret`, patch 0011): the same formula and vectors, but offered
  per connection (`derive` on creation), not by a trusted-clients setting.

## 5. Still to move

The 13 entries of phase 1's list, and where each stands. Everything "moved" is active only
with `SIGNER=belfry`; with `SIGNER=keycast` nothing changed.

| # | What | State after phase 2 |
| --- | --- | --- |
| 1 | The apps' sign-in through Keycast's OAuth | **Moved**: `appsignin.py` (section 1c); the proxy sends `apps.<domain>/api/oauth/*`, `/api/auth/oidc/logout`, `/api/nostr` to the service. Not run with the real suite apps (their builds bake `KEYCAST_URL` = `https://apps.<domain>`, which stays right) |
| 2 | **Self-custody**: the page and API to move to an own key | **Open.** The signer has `POST /accounts/{id}/own-key` and the service `core.key_changed`; no page or endpoint joins them. Nobody on a belfry with its own signer can move to their own key. A visible page: drawn first (canvas), then built, with a fresh e-mail code and a proof of the new key |
| 3 | `nostr.json` in the proxy | **Moved**: every name goes to the service (`mail.py`), `_smtp` and `_groups` as before |
| 4 | `apps.<domain>`: Keycast's dashboard, security page, "Your pass" card | **Moved as far as there is something to move**: those pages are gone; the address leads to `belfry.<domain>`. `belfry.json` keeps its `keycast` field (the apps' sign-in still answers there) |
| 5 | **Key export**, and the wallet's secrets over OAuth | Wallet secrets: **moved** (`derive_secret` on `/api/nostr`, the wallet only). Key export: **open**, like 2 (the signer has `/export`; a page with a fresh e-mail code is missing) |
| 6 | Keycast signs members in through Authentik; the pass page reads Authentik's `keycast` scope | **Gone with Authentik**: `PASS_SIGNIN=belfry` is the only sign-in in this mode. The OSV look's wallet sign-in on Authentik's page (the pass bridge, `bridge.py`) has no counterpart: a belfry that wants "sign in with the pass in an EUDI wallet" stays on Keycast and Authentik for now |
| 7 | `_ensure_keycast_user`, `restore_keys` assume Keycast's preload | Unchanged: they pass in both modes through the client. `restore-keys` cannot bring back sessions or usernames: with the signer the backup does (9) |
| 8 | The installer, the `belfry` command, the secrets | **Moved**: `gen-secrets.sh` (`signer_token`; `signer_master_key` = Keycast's `master.key`), `belfry` (compose files by `SIGNER`, the signer's image in Keycast's place, `BELFRY_SIGNER` in `config`, `status`), `bootstrap.sh` (skips Keycast and Authentik), `install.sh --own-signer`, `docker-compose.signer.yml` + `signer-prod.yml` + `images-signer.yml`, the image workflow. Keycast's and Authentik's secrets are still generated (the base compose file names them): removed with the files, last |
| 9 | Backups and the restore drill | **Moved**: `signer-data` is in `VOLUMES` (the signer paused while it is copied), `infra/secrets/signer_master_key` in the files; the drill fails when the volume is in a backup without its master key. Not run on a box |
| 10 | Smoke and check scripts | **Partly**: `infra/scripts/signer-smoke.py` is the signer's end-to-end check (section 7). `smoke.sh` and the `*-check` scripts still drive Keycast's and Authentik's pages and do not run against this mode |
| 11 | The service needs Keycast's and Authentik's tokens | **Moved**: optional, required only with `SIGNER=keycast` |
| 12 | Names (`services.keycast`, `keycast_pubkey`, `KEYCAST_*`, the relay's service name `keycast-relay`) | Open: a rename, last. The relay keeps its name and `bunker.<domain>` on purpose until then |
| 13 | `apply` restarts services whose mounted files changed | **Covered**: `restart_changed_mounts` reads the merged compose configuration, so the signer and its two secrets are in it once `docker-compose.signer.yml` is |

Also still Authentik's or Keycast's, found in phase 2:

- **The issuer's own login** (walt.id Issuer2's authorization-code flow, `infra/issuer/config/
  authentication-service.conf.tmpl`, `/issuer-shim`) points at Authentik. Passes are issued
  from the claim page with a pre-authorized code, which does not use it; whether Issuer2 starts
  and issues with Authentik absent is **not verified**.
- **Postgres** stays (the groups relay's database; Odoo's with Odoo). `postgres/init.sh` still
  makes the empty roles and databases `authentik` and `keycast` on a new volume, and the
  health check still looks for `keycast`: harmless, removed with the rest.
- **`/hooks/keycast/key-changed`** stays routed and unused.

## 6. Migration requirements

A belfry that moves from a live Keycast to the signer must come out with **nobody signed out
and no key changed**. A chat session's client key is also the device key of sealed
conversations (`docs/sealed.md` §2): a member whose sessions are lost has to sign in again with
a new key and is locked out of their sealed threads until another device lets the new one in.
So the migration carries, for every account:

| What | Where it is in Keycast (schema at `fe9c646`, the pinned commit, read from upstream's `database/migrations`; patches add no tables) | Into the signer |
| --- | --- | --- |
| The account and its id | `users.pubkey`, `users.vine_id` (unique per tenant). Every account Belfry makes has `vine_id = odoo-partner-<partner id>`: members, guests (username `guest-<n>`), agents, testers | `accounts.vine_id`, `pubkey` |
| An account without a `vine_id` | possible in principle (someone who registered on Keycast's own pages before they were closed): **not verified** on any box. The migration must list them and stop, not skip them | to decide per account |
| The key | `personal_keys.encrypted_secret_key` by `user_pubkey`: `nonce \|\| AES-256-GCM` under `infra/keycast/master.key` | `POST /import` (opens Keycast's format under the same master key, checks the key against the public key, seals it again bound to its row). `derived` when the custody seed gives the same key at version 0 to 8, else `imported`: **random keys** (made before a seed existed, or by a Keycast without `KEYCAST_CUSTODY_SEED_FILE`) exist only in this blob and in the backups |
| An account whose member holds their own key | a `users` row with no `personal_keys` row (patch 0012 deletes it; patch 0014's preload creates it without one) | `PUT /accounts/{id}` with `own_pubkey` |
| The key version | not in Keycast: `res.partner.belfry_key_version` in the register | found by the import (the version whose derived key matches) |
| The username | `users.username` (lowercase, unique per tenant since migration `20260417120000`) | `PUT /accounts/{id}/username`. The signer accepts 1 to 63 of `a-z 0-9 . _ -`: a name outside that (**not verified** that none exists) must be reported, not dropped |
| Suspended state | `users.status` (`active`, `suspended`, `banned`; migration `20260518120000`) | `POST /accounts/{id}/status`; `banned` becomes `suspended` |
| Every live session | `oauth_authorizations`: `user_pubkey`, `client_id` (the name shown, e.g. `Chat · Firefox on Linux`), `redirect_origin`, `bunker_public_key`, `secret_hash`, `client_pubkey`, `connected_client_pubkey`, `connected_at`, `created_at`, `last_activity`, `activity_count`, `revoked_at`, `expires_at`, `handle_expires_at`, `verified_client_pubkey` | `POST /import/connections` (below) |
| The record of client keys of revoked sessions | the same table, rows with `revoked_at` | also to import, as revoked rows, or the rule "one client key, one identity" forgets them |

**The bunker key of a session is not stored in Keycast.** It is derived
(`core/src/bunker_key.rs`): `HKDF-SHA256(no salt, IKM = the account's 32-byte secret key,
info = "keycast-bunker-nip46-v1-" + secret_hash)`, where the callers pass the **stored**
`secret_hash` (`api/src/api/http/auth.rs:1766`, `signer/src/signer_daemon.rs:1982`); a result
outside the curve order retries with `…-retry<n>`. So a session can be carried over exactly:
the signer, which holds the account's key, derives the bunker key from `secret_hash`, checks it
against `bunker_public_key`, and seals it for the new connection row. That check is also the
test of the formula for every row: no fixed vector of it exists in Keycast's tests (the
signer's own vectors, `keys.rs`, were computed from the formula with another implementation).
The formula and its callers were read again in phase 2 at `fe9c646` (`core/src/bunker_key.rs:
38-63`; the retry info is the same string with `-retry1`, `-retry2`, … appended).

**Built in phase 2: `POST /import/connections`** (`services/signer/README.md`, "Carrying
Keycast's sessions over"): one account a call, one transaction, all or nothing, safe to repeat;
a bunker key the formula does not give is refused with the row's index. It was tested with
rows made from the formula, **never with a row of a real Keycast database**.

### What the migration script reads (not written yet)

Schema read from upstream's `database/migrations` at `fe9c646`; Belfry's patches add no table
and no column (0022 and 0023 only write `client_pubkey`, `connected_client_pubkey` and
`verified_client_pubkey`). One tenant per belfry: `tenants.domain = KEYCAST_TENANT_DOMAIN`.
`psql -U keycast -d keycast` in the `postgres` container.

```sql
-- 1. accounts, keys, usernames, states (one row per account)
SELECT u.pubkey,                 -- char(64)
       u.vine_id,                -- text; 'odoo-partner-<id>' for every account Belfry made
       u.username,               -- text, lower case
       u.status,                 -- text: active | suspended | banned
       encode(k.encrypted_secret_key, 'hex') AS sealed   -- bytea; NULL: the member's own key
FROM users u
JOIN tenants t ON t.id = u.tenant_id AND t.domain = :'tenant'
LEFT JOIN personal_keys k ON k.user_pubkey = u.pubkey AND k.tenant_id = u.tenant_id
ORDER BY u.pubkey;

-- 2. sessions (every row, revoked ones too)
SELECT a.user_pubkey,                                   -- char(64) → the account
       a.bunker_public_key,                             -- char(64) → bunker_pubkey
       a.secret_hash,                                   -- text (bcrypt) → keycast_secret_hash, as it is
       a.client_id,                                     -- text → app (the name shown)
       a.redirect_origin,                               -- text (not carried; tells own apps from outside ones)
       a.client_pubkey,                                 -- char(64): the declared client
       a.connected_client_pubkey,                       -- text
       a.verified_client_pubkey,                        -- text: NULL or = connected_client_pubkey (a trigger)
       extract(epoch FROM a.created_at)::bigint    AS created_at,
       extract(epoch FROM a.last_activity)::bigint AS last_used_at,
       a.activity_count                            AS uses,
       extract(epoch FROM a.revoked_at)::bigint    AS revoked_at,
       extract(epoch FROM a.expires_at)::bigint    AS expires_at
FROM oauth_authorizations a
JOIN tenants t ON t.id = a.tenant_id AND t.domain = :'tenant'
ORDER BY a.user_pubkey, a.created_at;
```

How the rows go in:

- **Accounts**: `sealed` not NULL → `POST /import {accounts: [{vine_id, pubkey, sealed}]}`
  (hex, as selected); NULL → `PUT /accounts/{vine_id}` with `own_pubkey`. Then the username
  (`PUT …/username`) and, for `suspended` and `banned`, `POST …/status {status: suspended}`.
  A row with no `vine_id`, and a username outside the signer's `a-z 0-9 . _ -` (1 to 63): list
  and stop.
- **A session Keycast served** is one where `connected_client_pubkey = verified_client_pubkey`,
  `revoked_at IS NULL` and `expires_at` is NULL or in the future (`signer_daemon.rs`,
  `binding_state` and `validate_client`). It goes in with `client_pubkey :=
  connected_client_pubkey`.
- **Expiry is enforced by Keycast** at `expires_at` (`effective_status`); `handle_expires_at`
  is never read by its signer. `create_bunker` leaves `expires_at` NULL, so Belfry's sessions
  have none. A row that has expired is not a live session: send it as revoked (its client key
  stays linked) with `revoked_at := expires_at`.
- **Revoked rows** go in as revoked (`revoked_at`), with `client_pubkey` when they have one.
- **Sessions never connected** (no `connected_client_pubkey`): Keycast's `secret_hash` is
  bcrypt, the signer's SHA-256, so the secret cannot be carried; a live row without a client
  key is refused by the import. Leave them out and count them (nobody is signed in on them).
  One made with patch 0022 (bound at creation: all three client columns equal) is a served
  session and goes in.
- **`scope`**: `own` for the sessions of the belfry's own apps, `outside` for the rest. Tell
  them by `client_id` (`apps.chat_names`, the phone apps' names, the suite's client ids
  `belfry-*`) and `redirect_origin`; when in doubt `outside` (the app can sign in again).
- **`app`** must be 1 to 64 characters: a longer or empty `client_id` is cut or replaced by
  the script, and said in its report.
- **Members with their own key** have no sessions in Keycast: nothing to carry.
- **Admins**: `python -m belfry_onboarding import-stewards` while Authentik still answers
  (section 1b, B3); its report names admins without a person in the register.
- **Authentik ids** on people and passes (`belfry_authentik_user_id`, `authentik_user_id`)
  stay as they are: nothing reads them in the new mode.

And around the data:

- **The relay**: the signer must listen on the relay the sessions' URLs name
  (`wss://bunker.<domain>`, `keycast-relay`). Both signers on one relay at once is fine (they
  answer different bunker keys) only while each account's sessions are answered by one of them:
  Keycast must be stopped, or its signer daemon at least, before the signer answers imported
  sessions, or a request gets two answers. Whether `relay_builder` accepts the signer's
  subscription (`#p` with hundreds of keys, several filters) is **not verified**; the loop was
  run against strfry only.
- **Order**: backup; stop Keycast's writes; import accounts, usernames, statuses; import
  sessions; compare counts and every public key with the register (`keycast_pubkey` of each
  pass); switch the proxy's `nostr.json` and `SIGNER=belfry`; start; check one session of each
  kind (web chat, iPhone, Android) signs without signing in again. Roll back = the old
  settings and Keycast up again, which works as long as nothing was created on the signer in
  between (new accounts there are derived keys and can be preloaded into Keycast; new sessions
  cannot be carried back).
- **The master key** is shared: `gen-secrets.sh` copies `infra/keycast/master.key` to
  `infra/secrets/signer_master_key` (and says so when the two differ); the custody seed is the
  same file.
- **The switch itself**: `BELFRY_SIGNER=belfry BELFRY_SIGNER_MIGRATED=1 belfry config`, then
  `belfry apply` (which stops and removes Keycast's, Authentik's and Redis's containers and
  leaves their data). `belfry config` refuses the switch on a belfry with a first admin
  without `BELFRY_SIGNER_MIGRATED=1`. Web sessions of `belfry.<domain>` and the pass page are
  the service's own cookies and survive; **Authentik sessions end** (nobody uses them on a
  belfry with `PASS_SIGNIN=belfry`; an OSV-look belfry, `PASS_SIGNIN=authentik`, cannot switch:
  section 5, entry 6).
- **Apps signed in through Keycast's OAuth** (the suite) hold Keycast refresh tokens, which
  mean nothing to Belfry: their NIP-46 session is carried (they keep signing), their next
  token refresh fails and they sign in again. The wallet, which signs through `/api/nostr`
  with its access token, asks for a new sign-in at once.
- **After the switch** the `signer-data` volume is the only place sessions and usernames live:
  it goes into `backup.sh` before the switch, not after (item 9 above).

## 7. How to try it

```
# Rust: tests, lints (no cargo on the machine: through Docker)
docker run --rm -v "$PWD/services/signer":/src -w /src rust:1-slim-bookworm \
  sh -c 'rustup component add clippy && cargo clippy --all-targets -- -D warnings && cargo test'
# the bunker loop against a real relay (strfry on 7791)
docker run --rm --network host -e BUNKER_TEST_RELAY=ws://127.0.0.1:7791 … cargo test -- --ignored

# Python: the suite as a belfry with Keycast and Authentik (default), or on its own signer
cd services/onboarding && python -m pytest tests -q
BELFRY_TEST_SIGNER=belfry python -m pytest tests -q
# the contract against a running belfry-signer
SIGNER_LIVE_URL=http://127.0.0.1:8291 SIGNER_LIVE_TOKEN=… python -m pytest tests/test_signer_contract.py -q
```

**The signer with a relay, end to end** (no containers needed beyond the Rust one that builds
it; this is the smoke that was run in phase 2):

```
D=$(mktemp -d); mkdir -p $D/relay/db $D/data
printf 'db = "%s/relay/db/"\nrelay {\n bind = "127.0.0.1"\n port = 7793\n nofiles = 0\n}\n' $D > $D/relay.conf
strfry --config=$D/relay.conf relay &                      # any relay that passes kind 24133 on
(umask 077; openssl rand -hex 32 > $D/token; openssl rand -hex 32 > $D/seed
 openssl rand 32 | base64 | tr -d '\n' > $D/master)
SIGNER_LISTEN=127.0.0.1:8293 SIGNER_DB=$D/data/signer.sqlite3 SIGNER_TOKEN_FILE=$D/token \
  SIGNER_MASTER_KEY_FILE=$D/master CUSTODY_SEED_FILE=$D/seed SIGNER_OWN_DOMAIN=belfry.test \
  BUNKER_RELAYS=ws://127.0.0.1:7793 BUNKER_PUBLIC_RELAYS=ws://127.0.0.1:7793 belfry-signer &
cd services/onboarding && PYTHONPATH=. python ../../infra/scripts/signer-smoke.py $D
```

It makes an account, a username, a connection of an outside app (connect with the secret,
`get_public_key`, a signed note whose signature verifies, four refusals, a second key with the
spent secret refused) and one of the belfry's own bound at creation (signs moderation, NIP-44
both ways), then lists, revokes and derives.

**A whole belfry on its own signer** (a new one; never an existing one, section 6):

```
infra/scripts/gen-secrets.sh                # also writes signer_token and signer_master_key
echo SIGNER=belfry >> infra/.env            # and PASS_SIGNIN=belfry, REGISTER=belfry
docker compose -f infra/docker-compose.yml -f infra/docker-compose.noodoo.yml \
  -f infra/docker-compose.signer.yml up -d  # no keycast, authentik-*, redis
SIGNER=belfry infra/scripts/bootstrap.sh
docker compose -f infra/docker-compose.yml -f infra/docker-compose.noodoo.yml \
  -f infra/docker-compose.signer.yml --profile app up -d
# on a server: install.sh --domain … --own-signer, or BELFRY_SIGNER=belfry belfry config; belfry apply
```

## 8. Not verified

What was run in phase 2: the signer's tests and lints (clippy clean, 44 passed, 1 ignored: the relay test); the full Python suite in
both modes (default 1184 passed, 1 skipped; `BELFRY_TEST_SIGNER=belfry` 1162 passed, 23 skipped); the contract tests against a running signer (34 passed, 1 skipped); the smoke of
section 7 against a real relay (strfry), 16 checks; the merged compose configuration in both
modes (`docker compose config`: the services, what the service and the proxy wait for, the
image names); the Caddyfile in a local `caddy:2.11.7`, validated and routed against stand-in
back ends in both modes (the rate-limit directive taken out: that module is not in the stock
image).

Not run, and why it matters:

- **No stack was started.** The machine had 0.7 GB of disk left: no image could be built or
  pulled. So unrun: the signer's own Dockerfile image, `docker-compose.signer.yml` as a running
  stack, the onboarding service against the signer over the Compose network, `bootstrap.sh`
  and `belfry apply` in this mode, `gen-secrets.sh`, `backup.sh` with the signer's volume, the
  first-run setup in a browser, Issuer2 and the mint without Authentik.
- **The bunker relay Belfry ships** (`relay_builder`): whether it accepts the signer's
  subscription (`#p` with hundreds of keys) is still unknown; strfry does.
- **Real apps**: the suite (calendar, vote, wallet, groups, village) against `appsignin.py`,
  nostr-tools / NDK / the phone apps against the signer's bunker. The shapes were written from
  the apps' sources and Keycast's patches, not from a recorded exchange.
- **A real Keycast database**: the session import, the SQL of section 6 (column names and
  types are from upstream's migrations), the accounts without `vine_id`, usernames outside the
  signer's alphabet.
- **A real Odoo** with `belfry_steward` (`REGISTER=odoo`), and the module's upgrade.
- Keycast's behaviour where the fakes guess: preload of an existing account with another
  `pubkey`, the text form of session times (the phone apps decode `/api/me/sessions`
  strictly), whether a suspended account's name still resolves in `nostr.json`.
- `limits.rs`'s host parsing was held to Python 3.12's `urlsplit` through the vectors only.
- Limits kept in memory (codes, flows) assume one process, as the rest of the service does.

## 9. Open questions

1. **Self-custody and key export** (section 5, entries 2 and 5): pages a member sees, so they
   are drawn first. Until they exist a belfry on its own signer cannot offer either; decide
   whether that blocks the first switch.
2. **Kinds 0 and 3 for approved outside apps** (section 2): refused today, by the remote-apps
   rule. Keep, or add perms to an approval?
3. **The logout is a GET** (Keycast's shape, section 1c): accept, or have the suite's apps
   post?
4. **EUDI-wallet sign-in** lived in Authentik (the pass bridge). Is it wanted on a belfry
   without Authentik? Then it needs its own page in `passlogin.py`.
5. **When does a new install default to `SIGNER=belfry`?** Today `install.sh` needs
   `--own-signer`. Proposed: after one belfry has run on it and the migration is rehearsed.
6. **The first belfry to migrate**: one with `PASS_SIGNIN=belfry`, `OSV_APPS=0` and few
   members (belfry.sh or belfry.brussels); never the OSV-look setup first.
