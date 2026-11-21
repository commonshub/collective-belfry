# Belfry's own signer and sign-in

Belfry runs two pieces of other people's software in its most sensitive place: **Keycast**
(Divine's signer, with a series of 23 Belfry patches that must be rebased on every upstream
change and takes 20 to 30 minutes to build on a small box) and **Authentik** (the login, which
the `belfry` look no longer shows to anyone but still runs and still gets a user per member).
The plan is to replace both with Belfry's own code, in phases, without signing anybody out and
without changing anybody's key.

- Phase 1 (this document, branch `own-signer`): the signer exists, does what the product needs,
  and the onboarding service can run against either signer. **Nothing in production changes.**
- Phase 2: move what still depends on Keycast's web side, write and rehearse the migration,
  switch one belfry.
- Phase 3: Authentik out.

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

**Open, for phase 2: signing policy.** Every connection may sign any event for its account.
A limit on `created_at` was considered and not added: outboxes and sealed conversations sign
events that are not from now. Limits for connections of outside apps are still to be designed.

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
POST /accounts/{vine_id}/connections          {app, client_pubkey?, label?, derive?, keep?}
GET  /accounts/{vine_id}/connections
POST /accounts/{vine_id}/connections/revoke   {id | bunker_pubkey | app | all: true}
POST /import                                  {accounts: [{vine_id, pubkey, sealed}]}
```

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
  URL and connects later finds the session gone). Before a switch: pass the app's client key
  through `/connect` too, or raise `SIGNER_UNCLAIMED_SECONDS` for the time being. An agent "acting through a
  bunker URL" handed over by hand would have to connect within ten minutes.
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
- **No web side at all**: no OAuth, no account pages, no `nostr.json`, no OIDC. See section 5.
- **`nostrconnect://`** is not answered (Keycast's flow was unusable for outside apps too);
  outside apps stay on `remoteapps.py`, which signs through `sign_event` / `nip44_*` of
  whichever client is in use.
- **`sign_event` fills in a missing `pubkey`**; Keycast refused the event.
- **Wallet secrets** (`derive_secret`, patch 0011): the same formula and vectors, but offered
  per connection (`derive` on creation), not by a trusted-clients setting.

## 5. Still to move

Everything outside `clients/keycast.py` that talks to Keycast or leans on its web side. Line
numbers are of this branch. **Risk**: what breaks for members if the switch is flipped without
it.

Moved in phase 1 (only active with `SIGNER=belfry`): NIP-05 for usernames
(`belfry_onboarding/mail.py`, `member_name`), the status page's signer check
(`belfry_onboarding/console.py`, `checks`).

| # | What | Where | Risk |
| --- | --- | --- | --- |
| 1 | **The apps' sign-in through Keycast's OAuth** (`/api/oauth/authorize`, `/token`, `/revoke`, `/api/auth/oidc/logout`): the OSV app suite and every app that uses "Sign in with …" instead of Belfry's `/connect` | `services/village/web/shared/sso.js:42,70,99,110`, `services/vote/web/src/sso.js:43,71,100,111`, `infra/apps/{calendar,chat,wallet,groups}` patches, `infra/docker-compose.yml:113` (`KEYCAST_TRUSTED_CLIENTS`) | **High** where `OSV_APPS=1` or extras are added: those apps cannot sign in at all. None on a belfry with only the built-in chat |
| 2 | **Self-custody**: Keycast's page and API to move to an own key, and its callback | Keycast patch 0012; `belfry_onboarding/app.py:531` (`/hooks/keycast/key-changed`), `core.py:535` (`key_changed`), `infra/docker-compose.yml:125`, Caddy `infra/proxy/Caddyfile:148` | **High**: without a page that calls `POST /accounts/{id}/own-key` and then `core.key_changed`, nobody can move to their own key, and a move would not reach the pass and the relay whitelist |
| 3 | **`nostr.json` in the proxy**: `<domain>/.well-known/nostr.json` goes to Keycast except `_smtp` and `_groups` | `infra/proxy/Caddyfile:263-281` | **High**: every `name@domain` stops resolving (sign-in by name in the apps, mail delivery by username, NIP-05 marks) until the proxy sends all names to the onboarding service |
| 4 | `apps.<domain>`: everything that is not Belfry's apps list falls through to Keycast (its dashboard, security page, key export, "Your pass" card, OAuth pages) | `infra/proxy/Caddyfile:222-261`; `belfry_onboarding/settings.py:159` (`APPS_PUBLIC_URL`) used in `claim.py:54`, `discovery.py:121` (`belfry.json` `"keycast"`), `pages.py:170`, `remoteapps.py:261`, `apps.py:499,595`, `signin.py:88` | Medium: dead links and a 502 host; `belfry.json`'s `keycast` field is read by clients (not verified which) |
| 5 | **Key export** (patch 0005) and **the wallet's secrets over OAuth** (patch 0011, `KEYCAST_DERIVE_SECRETS`) | Keycast web; `infra/docker-compose.yml:116`; `infra/apps/wallet/patches/0002…`, `0007…` | Medium: the signer has `/export` and `/derive`, nothing calls them; the wallet (an extra) cannot derive its seed |
| 6 | Keycast signs members in **through Authentik** (OIDC), and the pass page reads the member from Authentik's `keycast` scope | `infra/authentik/blueprints/belfry-keycast-provider.yaml`; `belfry_onboarding/mypass.py:191,235` (`keycast_vine_id`); `core.py:357-390` (`sync_participants`: the group that may use Keycast); `core.py:962` | Medium: with `PASS_SIGNIN=authentik` (OSV look) `/me` cannot tell who signed in. Belongs to phase 3 |
| 7 | `core._ensure_keycast_user` and `restore_keys` assume Keycast's preload semantics | `belfry_onboarding/core.py:456`, `core.py:571`; callers `guests.py:790`, `events.py:1201`, `ticketsales.py:552`, `meetingguests.py:62`, `webthreads.py:810`, `core.py:796,955` | Low: they go through the client and pass in both modes (the client turns the signer's 409 `mismatch` into "the account as it is"). `restore-keys` cannot bring back sessions or usernames; with the signer the backup matters more (section 6) |
| 8 | The installer, the `belfry` command and the secrets | `infra/scripts/gen-secrets.sh:16,50-71` (no `signer_token`), `infra/scripts/belfry:89-101,118,444,482` (prebuilt images, compose files, the health URL through Authentik's Keycast provider), `infra/scripts/bootstrap.sh` (about 40 lines on Keycast: admin key, tenant, OIDC client), `install.sh`, `infra/docker-compose.noodoo.yml:12,26`, `infra/docker-compose.images.yml:11-15`, `.github/workflows/images.yml`, `Makefile` | Medium: a new install cannot come up on the signer; nothing breaks on existing boxes |
| 9 | **Backups and the restore drill** name Keycast's tables and files | `infra/scripts/backup.sh:77,346,348` | **High after a switch**: the `signer-data` volume would not be in the backups, and it then holds every session and username |
| 10 | Smoke and check scripts drive Keycast's API and pages | `infra/scripts/smoke.sh` (about 35 places), `keycast-oidc-login.sh`, `self-custody-check.sh`, `silent-signin-check.sh`, `chat-login-check.mjs`, `calendar-check.mjs`, `groups-check.mjs`, `wallet-own-signer-check.mjs`, `custody-seed.sh:56` | Low for members; no end-to-end check exists for the signer |
| 11 | The onboarding service waits for Keycast and holds its tokens even with `SIGNER=belfry` | `belfry_onboarding/settings.py:58-59` (`KEYCAST_ADMIN_TOKEN` and `KEYCAST_SERVICE_TOKEN` are required), `infra/docker-compose.yml:614,623` | Low: Keycast has to keep running (or the settings made optional) |
| 12 | Names: `services.keycast`, `keycast_pubkey` on passes (register and Odoo), `KEYCAST_*` in docs, "Signer (Keycast)" in tests | throughout; `belfry_onboarding/register.py`, the Odoo module | None: a rename, last |
| 13 | `apply` restarts services whose mounted files changed; the signer's secrets are not in its list | `infra/scripts/belfry` (`restart_changed_mounts`) | Low |

The three riskiest: **3** (every `name@domain` dead the moment Keycast stops answering),
**9** (a switched belfry whose sessions are not in the backup) and **1** (the OSV app suite and
extras cannot sign in; also the reason OSV-look belfries cannot switch before phase 3).

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
| Every live session | `oauth_authorizations`: `user_pubkey`, `client_id` (the name shown, e.g. `Chat · Firefox on Linux`), `redirect_origin`, `bunker_public_key`, `secret_hash`, `client_pubkey`, `connected_client_pubkey`, `connected_at`, `created_at`, `last_activity`, `activity_count`, `revoked_at`, `expires_at`, `handle_expires_at`; `verified_client_pubkey` is used by patches 0022/0023 (its migration was not read) | **needs an import that does not exist yet** (below) |
| The record of client keys of revoked sessions | the same table, rows with `revoked_at` | also to import, as revoked rows, or the rule "one client key, one identity" forgets them |

**The bunker key of a session is not stored in Keycast.** It is derived
(`core/src/bunker_key.rs`): `HKDF-SHA256(no salt, IKM = the account's 32-byte secret key,
info = "keycast-bunker-nip46-v1-" + secret_hash)`, where the callers pass the **stored**
`secret_hash` (`api/src/api/http/auth.rs:1766`, `signer/src/signer_daemon.rs:1982`); a result
outside the curve order retries with `…-retry<n>`. So a session can be carried over exactly:
the signer, which holds the account's key, derives the bunker key from `secret_hash`, checks it
against `bunker_public_key`, and seals it for the new connection row. That check is also the
test of the formula: no fixed vector of it exists in Keycast's tests. To build in phase 2:
`POST /import/connections` (`vine_id`, `bunker_pubkey`, `keycast_secret_hash`, client key,
name, times, uses, revoked), all or nothing per account.

Three things the session import must settle:

- **Sessions never connected** (no `connected_client_pubkey`, no `client_pubkey`): Keycast's
  `secret_hash` is bcrypt, the signer's is SHA-256, so the secret cannot be carried. Drop them
  (nobody is signed in on them); count them in the report.
- **Members with their own key** have no sessions in Keycast: nothing to carry.
- **Expiry**: whether Keycast's signer stops answering a session at `expires_at` or
  `handle_expires_at` (30 days after creation) is **not verified**. The signer has no expiry
  for a bound connection; if Keycast enforces one, the migration decides whether to keep it.

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
- **The master key** is shared (`infra/keycast/master.key` is the signer's `signer_master_key`
  in `infra/docker-compose.signer.yml`); the custody seed is the same file.
- **After the switch** the `signer-data` volume is the only place sessions and usernames live:
  it goes into `backup.sh` before the switch, not after (item 9 above).

## 7. How to try it

```
# Rust: tests, lints (no cargo on the machine: through Docker)
docker run --rm -v "$PWD/services/signer":/src -w /src rust:1-slim-bookworm \
  sh -c 'rustup component add clippy && cargo clippy --all-targets -- -D warnings && cargo test'
# the bunker loop against a real relay (strfry on 7791)
docker run --rm --network host -e BUNKER_TEST_RELAY=ws://127.0.0.1:7791 … cargo test -- --ignored

# Python: the suite against Keycast's fake (default) or the signer's
cd services/onboarding && python -m pytest tests -q
BELFRY_TEST_SIGNER=belfry python -m pytest tests -q
# the contract against a running belfry-signer
SIGNER_LIVE_URL=http://127.0.0.1:8291 SIGNER_LIVE_TOKEN=… python -m pytest tests/test_signer_contract.py -q
```

## 8. Not verified

- The signer has not run in a container built from its `Dockerfile`, in the Compose override,
  behind the bunker relay Belfry ships (`relay_builder`), or with a real app (nostr-tools, NDK,
  the phone apps). The NIP-46 answers were checked by the signer's own tests and one run
  against strfry with the rust-nostr client.
- The full Python suite was run against Keycast's fake; against the signer's fake the files
  that use the signer most were run, not all of them (the numbers are in the commit message).
- Keycast's behaviour where the fakes guess: preload of an existing account with another
  `pubkey`, the text form of session times, whether a suspended account's name still resolves
  in `nostr.json`, session expiry.
- No migration code exists; section 6 is a requirement list from reading the schema and the
  patches, not a tested procedure.
