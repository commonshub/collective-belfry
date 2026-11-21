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
- Phase 3, first part (same branch): the fixes of an independent review (section 1d), the
  guards that keep a signer from ever making a new key for somebody who has one (section 6,
  "The guards"), and **the migration as code** (section 6b, "Moving a belfry":
  `infra/scripts/migrate-to-own-signer.sh` and `migration.py`). Written and unit-tested;
  **never run against a real Keycast database, signer, Authentik or Docker**.
- The Rust round (same branch, section 11): the signer's own items from the review and from
  writing the migration, done, with the Python side wired to them.
- The migration's review (same branch, section 12): agents, the import as a sync, custody and
  scope checked, the apply lock, rollback carrying sign-outs back, blocking counts, and the
  default-mode changes listed and two of them undone.
- Still ahead: a rehearsal on a copy of a real belfry, the switch of one belfry, then what
  Keycast had that Belfry does not have yet (section 10).

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
(`stewards.own_mode`). The service refuses to start with `PASS_SIGNIN=authentik` next to it.
With `SIGNER=keycast` it is exactly as tolerant as before this branch: `AUTHENTIK_TOKEN`,
`KEYCAST_ADMIN_TOKEN` and `KEYCAST_SERVICE_TOKEN` must be set and may be empty
(`stewards.check_settings`; they were required fields without a default, and the four production
servers get this code with the default long before any migration). With `SIGNER=belfry` none
of the three is needed. `services.authentik` is then an object
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

**A sign-in of an app is not a NIP-46 connection** (since the review). The token exchange opens
a connection in the signer and hands the app its bunker URL, but the wallet never connects to
it: it signs over `/api/nostr` only, and the signer drops a connection nobody connected to
within ten minutes. So the table (`appsignin.sqlite`) alone decides whether a token works; the
member's list of sessions (`/api/me/sessions`, the console's Devices) is the signer's list plus
the sign-ins whose connection the signer no longer has (`appsignin.with_grants`, same id: the
bunker key), and signing a session out ends the sign-in with it (`appsignin.session_revoked`,
called by `discovery.py`, `console.revoke_device` and `sales.py`). An outside app's sign-in
does not decrypt (`nip44_decrypt`, `nip04_decrypt`) unless its row says the member allowed it
(`grants.decrypt`; nothing sets it yet: the sign-in page has no such question).

Since the Rust round the token exchange makes that connection of kind `http` in the signer
(`connect.redeem(..., kind="http")`): an app may still connect to it within ten minutes, and
one that never does keeps it (only its secret ends), so the grant's session stays in the
signer's list with the app's origin. `with_grants` keeps its fallback for sign-ins made before
connections had a kind. The consent flag is passed to the signer when the connection is made
(`create_bunker(..., decrypt=...)`, False until the page asks) and can be changed later
(`SignerClient.set_consent`, `POST …/connections/consent`).

**A silent sign-in (`prompt=none`)**, decided: an address in the registry (served by the belfry,
approved by an admin, or the phone apps' `sh.belfry.app://connect`) whose visitor is not signed
in is redirected to that registered address with `error=login_required`; that is not an open
redirect. Any other address gets a page (403) and no redirect. The same in both modes
(`connect.py`, `authorize`; tests in `test_connect.py` and `test_own_mode.py`).

### 1d. The review's findings (phase 3, first part)

| | Finding | What changed | Where |
| --- | --- | --- | --- |
| 1 | The name@domain sign-in gave an approved outside app an `own` session | It passes the app's scope, as `/connect` does | `sessions.py:283` (`connect.scope_of`); test `test_signing_in_by_name_gives_an_outside_app_an_outside_session` |
| 2 | An app that signs over HTTP only lost its sign-in after ten minutes | The sign-in stands on the table, not on the connection (above) | `appsignin.py` `check_live`, `with_grants`, `session_revoked`; test `test_an_app_that_only_signs_over_http_keeps_its_sign_in` |
| 3 | The first-run setup could be claimed again; a signer started empty would mint keys | Section 6, "The guards" | `signerguard.py`, `setup.has_admin`, `clients/signer.py` `preload_user`, `infra/scripts/belfry` `ever_had_people` |
| 4 | A chosen username like `50` blocked member 50's account for ever | Nobody chooses a name accounts start with (`usernames.problem`: `reserved`; also the first admin's); a taken member number no longer stops an account: it is made as `member-<id>` (`core.preload_account`, both signers) | `usernames.py:87`, `core.py` `preload_account`, `community.py` `member_token`, `setup.py` `create_first_admin` |
| 5 | `prompt=none` for an app nobody approved | Decided as above; the two skills say so | `connect.py` `authorize`, `skills/belfry-app/SKILL.md`, `skills/open-source-village/SKILL.md` |
| 6 | Lows | `mail.py` uses `own_mode`; `redeem` checks `client_pubkey` before it spends the code; `belfry.json` `sign_in.login` names the pass page's sign-in on a belfry without Authentik (the field stays: whether the phone apps decode it strictly was not checked); `signer_master_key` is written 600 (the signer runs as the file's owner, `HOST_UID`, like Keycast: no group read needed); `check_settings` as above; the compose comment about the signer's network is corrected (below) | `mail.py`, `connect.py` `redeem`, `discovery.py`, `gen-secrets.sh`, `stewards.py`, `docker-compose.signer.yml` |

**A suspended member's name still resolves in `nostr.json`**, as with Keycast: its
`find_pubkey_by_username` (`core/src/repositories/user.rs:762` at `fe9c646`) has no status in
its WHERE, and the signer's `GET /names/{username}` has none either. Kept the same so a move
changes nothing; whether a closed membership should keep its address is a product question,
not a migration one (section 9).

**The signer's network** (since the Rust round). Two internal networks: `signer` (the signer
and the bunker relay it dials) and `signer-api` (the signer and the onboarding service). The
API listens on one interface only: `SIGNER_LISTEN=signer-api:8200`, the signer's alias on the
second network, looked up once at start; on the relay's network nothing listens on 8200. The
onboarding service calls `http://signer-api:8200`. The API token stays the second lock.

**Which scope each session gets** (every caller of `create_bunker`):

| Caller | Session | Scope | Why |
| --- | --- | --- | --- |
| `connect.py:271` (`/connect/token`, and `/api/oauth/token` through it) | an app's sign-in in the browser | `connect.scope_of`: `own` for an app the belfry serves (installed, the chat, the suite when it runs here, the phone apps' address), else `outside` | an admin's approval lets an app sign people in; it does not make it the belfry's |
| `sessions.py:283` (`POST /api/v1/session`, and by DM) | name@domain and a code | the same function | the phone apps (`sh.belfry.app://connect`) are `own`; any other app is what it is |
| `setup.py:449` | the first admin's browser on `/setup` | `own` | the console's own page |
| `guests.py:792`, `events.py:1203` | a guest who joined from a link, in the chat | `own` | the chat signs moderation, AUTH and NIP-98 to the belfry for them |
| `guests.py:1183` | a guest's code signs an existing account in (`client`: web, iOS, Android) | `own` | the belfry's own chat clients only (`NATIVE_CLIENTS`, else "Chat") |
| `sales.py:2486`, `ticketsales.py:617` | the browser that bought a pass or a ticket | `own` | the website's checkout then asks the belfry's API with NIP-98 as the buyer, which an `outside` session may not sign |
| `applyverified.py`, `sitesessions.py`, `remoteapps.py` | none | | they return a bunker URL another of these made, a site session (no bunker), or sign for a remote app inside the service with its own limits |

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

**More limits, in the service and in the signer** (decided in the review; in the signer since
the Rust round): an outside connection never signs app data named `belfry:…` (kind 30078 with
such a `d` tag: the sealed conversations' anchor `belfry:sealed`, the read state, the channel
order, a channel's inbox and web settings), never a label in a `belfry/…` namespace (kind
1985: a pin, a proposal's status, a ticket checked in, a vouch, a button's press and answer),
and decrypts (`nip44_decrypt`, `nip04_decrypt`) only when its connection carries a consent
flag, as remote apps' connections do. Kind 5 stays allowed. One vector file for both sides:
`services/signer/vectors/limits.json` (73 `cases`, 7 `methods`; `limits-next.json` is gone).

Decision to confirm: kinds 0 and 3 are refused to an approved outside app too (the rule of
remote apps "only when the app said so" has no way to say so at `/connect`). An outside Nostr
client that edits the profile would need a perms field in the approval.

## 3. The signer's API

Internal network only (`signer-api`), bearer token (`signer_token`). The full table is in
`services/signer/README.md`; in short:

```
GET  /health                                  {status, seed, bunker}           (no token)
GET  /settings | POST /settings               {no_new_accounts}                (SIGNER_NO_NEW_ACCOUNTS)
GET  /accounts?status=&after=&limit=          -> {accounts, next, total, suspended}
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
POST /accounts/{vine_id}/connections          {app, client_pubkey?, label?, derive?, keep?, scope?,
                                               decrypt?, origin?, kind?: nip46|http}
GET  /accounts/{vine_id}/connections[?all=true]   (all: revoked rows too, with revoked_at)
POST /accounts/{vine_id}/connections/revoke   {id | bunker_pubkey | app | all: true}
POST /accounts/{vine_id}/connections/consent  {id | bunker_pubkey, decrypt}
POST /import                                  {accounts: [{vine_id, pubkey, sealed}]}
POST /import/connections                      {vine_id, connections: [{bunker_pubkey,
                                               keycast_secret_hash, client_pubkey, app,
                                               origin?, decrypt?, …}]}  (1 MB, 1000 rows)
```

`scope` is `own` (default) or `outside`. New environment: `SIGNER_OWN_DOMAIN` and
`SIGNER_OWN_API_HOSTS`, `SIGNER_OWN_RELAY_HOSTS`, `SIGNER_OWN_BLOSSOM_HOSTS` (what an outside
connection never signs for; the compose file sets the domain and the relays' internal names).

Errors are `{"error", "message"}`: 401 `unauthorized`, 404 `not_found`, 503 `seed_locked` /
`bunker_unavailable` (a NIP-46 connection is not made while the bunker does not listen: ask
again), 409 `own_key` / `mismatch` / `client_key_taken` / `username_taken` / `no_new_accounts`,
403 `suspended` / `forbidden`, 413 `too_large`, 422 `invalid`.

## 4. Differences from Keycast

What a caller of the Python client could notice is listed at the top of `clients/signer.py`.
The ones that matter for the product:

- **An unclaimed connection lives 10 minutes**, not 30 days. Every sign-in Belfry does itself
  names the client key (`sessions.py:283`, `guests.py:792,1183`, `events.py:1203`,
  `ticketsales.py:617`, `sales.py:2486`), so this meets the two made without: `setup.py:449`
  (the first admin's session, connected within seconds) and **`connect.py:271`, the `/connect`
  sign-in of approved apps** (OAuth code + PKCE: the app gets the bunker URL from the token
  exchange and has to send its first `connect` within ten minutes of it; an app that keeps the
  URL and connects later finds the session gone). Since phase 2: `setup.py` binds the first
  admin's session to the browser's key, and an app may send `client_pubkey` with its token
  exchange (`/connect/token`, `/api/oauth/token`) to get a session bound from the start. An
  app that does not has ten minutes **from the answer that carries the URL** (the session is
  made in that request, never earlier), and signs in again if it missed them: nothing is
  re-issued by itself. The suite's apps connect at once after the exchange. An agent "acting
  through a bunker URL" handed over by hand would have to connect within ten minutes. An app
  that never connects and signs over `/api/nostr` (the wallet) loses the connection and keeps
  its sign-in (section 1c).
- **An account is read before it is asked for**, and made only with the key the register has
  for the person (section 6, "The guards"); Keycast's preload made whatever it made.
- **A client key is never freed.** In Keycast the move to an own key deletes the account's
  authorizations and with them the record of its client keys; here revoked connections stay.
- **Statuses**: `active` and `suspended` only; `banned` is kept as `suspended`. The reason is
  not stored.
- **Session times** are seconds in the signer; the client writes them as ISO 8601 with `Z` and
  no fraction (`2026-11-05T00:00:00Z`). Keycast's exact text form (fraction, offset) is **not
  verified**, and the phone apps decode `/api/me/sessions` strictly: check one real answer of
  each before a switch.
- **`redirect_origin`** is the `origin` a connection was made with: the app's scheme and host
  (`apps.origin_of`; `sh.belfry.app://connect` for the phone apps), passed by `connect.py`,
  `sessions.py` and `appsignin.py`; carried from Keycast by the migration. Sessions made
  without one (guests, buyers, setup) have "". `discovery.chat_session` takes the chat's name
  and an origin that is "", the chat's address or the phone apps'.
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
| An account whose member holds their own key | a `users` row with no `personal_keys` row (patch 0012 deletes it; patch 0014's preload creates it without one) | `PUT /accounts/{id}` with `own_pubkey`; an account the signer already holds a key for (the member took their own key in Keycast after the first import) is moved with `POST …/own-key`, and the held key is archived sealed (`archived_keys`), never dropped |
| An agent | **no Keycast row at all**: `register._agent_pass` and `accept_agent_guest` write the agent's own key into the pass (`keycast_pubkey` = `own_pubkey`) and make no signer account (belfry.sh has six or more). A Keycast row for such a partner (a "phantom": something asked for the partner's token and Keycast minted a derived key) may exist | nothing: counted as `own_key_no_account`, verify checks the signer has no account for them or a keyless one with that key. A phantom is left behind (`phantom` in the counts); one somebody signs in on stops the plan (`phantom_with_sessions`) |
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

### The guards: never a new key for somebody who has one

The worst thing a move can do is not to lose a session but to give a member another key: a
signer asked for an account it does not have makes one, and for a key from before the custody
seed (or imported) the key it makes is a different one. `signerguard.py` closes that:

- **Per account.** The own signer's client reads an account before it asks for one. When it
  must be made and the register already has a key for the person (their newest pass with a
  key), the signer is asked with `expect_pubkey`: it writes nothing when the key it would make
  differs, and the client raises `KeyNotHeld` with an error line that names the migration. A
  key the seed gives again is made again (a lost volume, `restore-keys`); a person the
  register has no key for is made as always. A register that cannot be read is an error, not
  "no key". (`clients/signer.py` `preload_user`, `signerguard.register_key` / `install`.)
- **At start.** With `SIGNER=belfry` the service looks up to 40 members up in the signer (the
  oldest and the newest). It does not start when the signer holds one with another key, or
  holds none of them: the log says which and why. `SIGNER_RECOVERING=1` lets a start with none
  through, for one case only: the signer's volume is lost and the keys are being derived again
  from the seed; the per-account guard still applies. (`signerguard.check_start`, `app.py`.)
- **Between import and switch** the running service still has `SIGNER=keycast`: it never
  talks to the signer, so it cannot make anything there. Only the migration's one-off
  container does, and it creates accounts with their public key named.
- **The first-run setup** is open only when all of this holds: there is a setup folder, no
  `first-admin.json`, nobody is an admin (and the admins' home answers), and **the register
  has no people**: no pass that is active and none that was ever given a key
  (`signerguard.has_people`; an invitation nobody took is not a person; a register that does
  not answer counts as "has people"). A belfry with members and no admin on record is one
  whose record is missing (the file lost in a restore, the admins not yet copied from
  Authentik), not a new one. `belfry setup-reset` is the deliberate way to open it: it now
  also opens such a belfry (it says so and asks), by writing `"open": true` into `reset.json`,
  which the next first admin clears. A new, empty belfry is claimable as before.
  (`setup.has_admin`, `reopened`, `reset`.)
- **`belfry config`** refuses to change `SIGNER` without `BELFRY_SIGNER_MIGRATED=1` whenever
  the belfry has, or ever had, people: the running service says (`python -m belfry_onboarding
  has-people`), a `first-admin*.json` (also one a reset archived) or `reset.json` says, and a
  stack that is down but has data volumes counts as yes. (`infra/scripts/belfry`
  `ever_had_people`.)

### What the migration reads

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
- **`scope`**, from `redirect_origin` as the patched Keycast at `fe9c646` writes it (read in
  the source): `""` from `POST /api/user/bunker/create` without an origin (`auth.rs:1812`),
  which is **every session the onboarding service makes** (`clients/keycast.create_bunker`
  never sends one: the chat's e-mail sign-in, the phone apps' native sign-in, guests, tickets,
  site sessions, the setup page) → `own`; the OAuth code flow stores scheme, host and port of
  the redirect URI (`oauth.rs` `extract_origin`; custom schemes are refused there, so the phone
  apps never come this way) → `own` when the host is the belfry's domain or under it (the chat
  on `chat.<domain>`, earlier on the apex, the suite), `outside` when it is an outside app an
  admin approved (`apps.state`); nostr-login stores `nostrconnect://<client>` (`oauth.rs:5071`)
  and the OIDC path the authorize request's origin or the app origin (`oidc.rs`
  `redirect_origin_for`). **Anything not known is carried `own` and counted as "scope guessed"**
  (in every phase's counts, listed in the verify report): a chat session carried as outside
  would lock its member out of their sealed history, which is worse than an outside app keeping
  own scope. Verify compares the scope; the next import corrects it.
- **Agents** (their own key on the pass, no Keycast row): nothing to carry, not a problem.
  `community.member_token` returns None for them in both modes (it used to make Keycast mint a
  derived key for the agent's partner).
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
- **Order** (as `migrate-to-own-signer.sh` does it, section 6b): backup; import and verify
  while the belfry runs, as a rehearsal; then stop Keycast and the service; import once more
  and verify (every public key against Keycast's and the register's `keycast_pubkey`, every
  session); `SIGNER=belfry` and apply (the proxy sends `nostr.json` and the apps' sign-in to
  the service); check one session of each kind (web chat, iPhone, Android) signs without
  signing in again. Roll back = the old settings and Keycast up again; what was made on the
  signer in between does not go back (new accounts there are derived keys and come out the
  same in Keycast; new sessions cannot be carried back; sign-outs and suspensions are written
  back into Keycast by `rollback` before it starts).
- **The master key** is shared: `gen-secrets.sh` copies `infra/keycast/master.key` to
  `infra/secrets/signer_master_key` (and says so when the two differ); the custody seed is the
  same file.
- **The switch itself**: `BELFRY_SIGNER=belfry BELFRY_SIGNER_MIGRATED=1 belfry config`, then
  `belfry apply` (which stops and removes Keycast's, Authentik's and Redis's containers and
  leaves their data); `migrate-to-own-signer.sh switch` does both. `belfry config` refuses the
  switch on a belfry that has, or ever had, people without `BELFRY_SIGNER_MIGRATED=1`. Web
  sessions of `belfry.<domain>` and the pass page are the service's own cookies and survive; **Authentik sessions end** (nobody uses them on a
  belfry with `PASS_SIGNIN=belfry`; an OSV-look belfry, `PASS_SIGNIN=authentik`, cannot switch:
  section 5, entry 6).
- **Apps signed in through Keycast's OAuth** (the suite) hold Keycast refresh tokens, which
  mean nothing to Belfry: their NIP-46 session is carried (they keep signing), their next
  token refresh fails and they sign in again. The wallet, which signs through `/api/nostr`
  with its access token, asks for a new sign-in at once.
- **After the switch** the `signer-data` volume is the only place sessions and usernames live:
  it goes into `backup.sh` before the switch, not after (item 9 above).

## 6b. Moving a belfry

`infra/scripts/migrate-to-own-signer.sh <phase>`, as the owner of the checkout or root. Every
phase can be run again; each prints counts; none prints a key, a secret or an e-mail address
(public keys are shortened, addresses masked). The Python side is `python -m belfry_onboarding
signer-migrate plan|import|verify` (`migration.py`), fed Keycast's rows through a pipe: they are
never written to disk. Reports go to `infra/data/setup/migration/` (counts, vine ids, shortened
public keys, app names).

**Run `switch`, `rollback` and `cleanup` under tmux or screen.** A dropped SSH session in the
middle of `belfry apply` leaves a half-applied stack; the script refuses those three phases
outside tmux/screen unless `--no-tmux` is given. They also hold the lock `belfry apply` takes
(`/run/lock/belfry-apply.lock`, flock) from before anything stops to the end, and refuse to
start when it is not free within two minutes (`BELFRY_MIGRATE_LOCK_WAIT`); the `belfry apply`
they run is told the lock is held (`BELFRY_APPLY_LOCKED=1`).

**Before the day**: the belfry runs this code with `SIGNER=keycast` (`belfry update`, `belfry
apply`: so the switch has nothing left to build except the signer); `PASS_SIGNIN=belfry` (a
belfry that signs in through Authentik cannot move: section 10); backups are set up
(`BACKUP_RECIPIENTS`, `BACKUP_TARGET`); the custody seed is unlocked.

| | Command | What it does | What is down |
| --- | --- | --- | --- |
| 1 | `migrate-to-own-signer.sh check` | Read-only. The code's commit, the settings, free disk (stops under 1 GB; a signer built on the box needs about 3 GB for 15 to 30 minutes), the master key, the seed; that Keycast's database, Authentik and the register answer; then the plan: accounts by kind (`member`, `guest`, `agent`, `own_key`, `no_vine_id`), usernames, suspended, sessions by state (`served`, `revoked`, `expired`, `never_connected`, `unverified`), how many live ones are the belfry's own apps' and how many outside apps', and every problem | nothing |
| 2 | `… backup` | A backup from the last hour, or `backup.sh run` now. Without a successful one nothing else runs (import wants one from the last 24 hours, switch and cleanup one from the last hour). Its age is read from the newest backup file's own name (`belfry-<host>-<UTC stamp>.tar.age`), never from the environment; a time in the future is no backup. Path and checksum go into the state | as every night: the services with a data volume are paused for the copy |
| 3 | `… import` | Makes `signer_token` and `signer_master_key` if missing and checks the latter **is** Keycast's `master.key`; starts the signer next to Keycast with its bunker off (`docker-compose.signer-import.yml`: it answers no NIP-46 request, so no request gets two answers); **closes the signer's "no new accounts" switch** (from now on it makes no account a caller does not name the key of); imports keys with the public key each must open to, members' own keys, usernames, suspended state, sessions (per account, 400 rows or 256 kB a call: the signer's body limit is 2 MB), and Authentik's admins into the register's flag. **A sync**: run again, it brings the signer to Keycast's current state (below). A row whose bunker key does not derive is skipped and listed; the rest of the account goes in | nothing; the belfry runs on Keycast |
| 4 | `… verify` | For every account: the signer's key = Keycast's = the register's; custody in Keycast, in the signer and in the register's `belfry_key_custody`; username, state; every served session is live in the signer with its bunker key, client key and scope, and nothing else is; agents with their own key have no signer account or a keyless one; no Keycast rows while the register has people is a failure, and the account counts come from Keycast and from the signer's own list; unverified sessions and skipped rows block unless accepted by exact number; every revoked or expired row is there **as revoked** with its client key (`?all=true`); no account in the signer that Keycast did not have (`GET /accounts`); every person of the register is in the signer; the admins. A report file. Exit 1 on any difference | nothing |
| 5 | `… switch` | Under tmux; takes the apply lock; a fresh backup; asks for `switch <domain>`; **stops Keycast and the service** (nothing writes to Keycast's database from here); a last import (the sync: whatever changed since the rehearsal); the verify that decides (not clean: Keycast and the service start again, nothing was switched); `BELFRY_SIGNER=belfry BELFRY_SIGNER_MIGRATED=1 belfry config`; `belfry apply`; then waits for the signer, asks the service, verifies the keys once more and, when that is clean, **opens new accounts again** (`signer-migrate open`; `… open` by hand if the switch stopped before) | **the belfry**, from the stop to the end of `belfry apply` |
| 6 | `… rollback` | Under tmux; asks for `rollback <domain>`; takes the apply lock; stops the service; **writes into Keycast what was signed out or suspended on the signer since the switch** (`signer-migrate carry-back`: `oauth_authorizations.revoked_at` for sessions Keycast still has live and the signer revoked, `users.status = 'suspended'` for accounts Keycast has active and the signer suspended; one transaction; never run against a real Keycast database); stops the signer and removes its container (not its volume); `BELFRY_SIGNER=keycast`; `belfry apply` brings Keycast, Authentik and Redis back | the belfry, for one `belfry apply` |
| 7 | `… cleanup` | Under tmux, only at a terminal, only with the two clean reports of **this box** (the one the switch was decided on and one from after it), only seven days after the switch (`--before-seven-days` overrides), after a fresh backup, and when accounts hold `imported` keys (random, from before the seed: only signer-data has them once Keycast is emptied) only when that backup is from after the switch and its own record (`<backup>.json`, written by `backup.sh`: volume file counts and the file's checksum) says it carries signer-data and the checksum matches (path and checksum go into the state), after the typed sentence `delete keycast and authentik of <domain>`. Stops at the first failed deletion (and records nothing): empties Keycast's and Authentik's databases (they stay, empty: Postgres' health check looks for `keycast`), removes Authentik's two volumes and the five containers. Keeps `infra/keycast/master.key`, the seed, the backups | nothing |

`… status` shows where a belfry is. Options: `--exclude <pubkey>` (leave one Keycast account
out on purpose: an account without a `vine_id` stops check, import and verify until it is named),
`--accept-lost-sessions N`, `--accept-missing-stewards N`, `--accept-unverified N`,
`--accept-skipped-rows N` (below; each must match the count exactly), `--yes` (no question
before switch or rollback; never for cleanup), `--no-tmux`.

**Reports are bound to the box.** `state.json` and the reports in `infra/data/setup/migration/`
travel in backups. Each report carries `box`: the domain, the first 16 hex of the SHA-256 of
the signer's master key file (never the key), and the system identifier of the Postgres
cluster Keycast's database lives in. `import` records it in the state; every later phase
refuses a state from another box, and switch and cleanup refuse reports from another box.

**How writes between import and switch are kept out.** They are not kept out: the belfry runs
normally between steps 3 and 5, people join, sign in and out, take their own key, and the
rehearsal's verify goes stale (it then says `account_not_imported`, `session_missing`,
`session_unexpected`: run import again). **Import is a sync.** It closes the signer's "no new
accounts" switch first, and while that is closed nothing but the import writes connections, so
the signer takes the import's rows as the truth for rows it has: a session signed out in Keycast
is revoked, a row whose client key went to another account (the shared-key owner changed, it is
ranked by `last_used_at`) releases it, and the account that keeps it gets it in the same run
(sessions go in two passes: rows without a client key first); scope, origin and names are
corrected; a row Keycast serves again comes back live. A live session the signer has that
Keycast no longer has at all (deleted, as patch 0012 does when a member takes their key) is
revoked. An account Keycast now shows with the member's own key (or a new one) is moved to it,
the held key archived sealed (`archived_keys`), and listed as `custody_moved`. A held key that
differs from Keycast's is an error, never an overwrite. Outside that window (accounts open:
after the switch) the signer never changes a row it has from an import. What decides is
the verify inside `switch`, made after Keycast (its API and its signer daemon, one container)
and the onboarding service are stopped: with those two down nothing writes to Keycast's
database, the last import reads its final state, and the report carries a digest of the rows
it compared. The alternative, comparing a report's time with the database's last write, was
dropped: Keycast writes a use counter on every signature, so the database is never quiet while
it runs.

**How long it is down.** From the stop to the end of `belfry apply`: the last import and verify
are four short container runs (a minute or two for some hundred accounts and some thousand
sessions: an estimate, never measured), then one `belfry apply` that recreates the signer, the
service and the proxy and removes five containers. Three to ten minutes on the boxes we run;
one `apply` has taken thirty minutes on the smallest. Down in that time: signing (the chat and
the phone apps cannot post or authenticate to the relays), every sign-in, the service's pages
and API (console, invitations, checkout, the website it serves, incoming mail is deferred),
uploads (the file server asks the service). The relays themselves keep running; what incoming
mail does while the service is away (refused for the moment, or deferred by the sender) was
not checked.

**What the operator sees** when it goes well: in `check`, five lines of counts ending in
`problems: 0` and `check: nothing stands in the way`; in `import`, `keys: N derived from the
seed, N imported as they are, N there already, N members' own`, the usernames, the sessions
(`live written`, `revoked written`, `there already`) and the admins; in `verify`, `verified: N
accounts, N keys equal in Keycast, the signer and the register, …` and `verify: CLEAN` with the
report's path; in `switch`, the same twice more around `belfry apply`, and how long it took.
When it does not: every problem and every mismatch is one line of JSON naming the account by its
vine id (`odoo-partner-<id>`), and the phase exits 1.

**What can stop it**, and what to do:

- `no_vine_id`: an account made on Keycast's own pages. Decide per account; `--exclude` leaves
  it behind on purpose.
- `username_not_carried`: a username outside the signer's `a-z 0-9 . _ -` (1 to 63). Rename it
  in Keycast first.
- `register_has_another_key`, `member_not_in_keycast`: the register and Keycast disagree about
  a person. Never go on; find out why.
- `session_lost`: two accounts in use have a live session with the same client key (possible
  for sessions older than Keycast patch 0023). One account keeps the key: an active one before
  a suspended one, a served session before a revoked one, then the one used last; the other
  device signs in again. Accept it by number, `--accept-lost-sessions N`, or sign that device
  out first. A suspended account's session never costs an active account its key (the eight
  guests one phone made on Enspiral: seven closed ones do not take the key from the eighth).
- `unverified_sessions` (connected, but `verified_client_pubkey` is not the connected key): §6
  reads Keycast as not serving them, so they are left out, and verify fails unless
  `--accept-unverified N` names their exact number. **Before accepting, read Keycast's
  `validate_client` again**: if they are served after all, those members would be signed out.
- `session_skipped`: a session whose bunker key the formula does not give was left out (the
  account and its other sessions went in). Blocking unless `--accept-skipped-rows N` matches;
  that device signs in again. More than a handful means the formula is wrong for this
  Keycast: stop.
- `register_custody`: Keycast, the signer or the register's `belfry_key_custody` disagree about
  who holds a key. Never go on.
- `phantom_with_sessions`: Keycast holds a key for an agent that has its own, and somebody signs
  in on it. Find out who before going on.
- `keycast_empty`: the query read no Keycast rows while the register has people (wrong tenant,
  an empty copy).
- `own_key_account`: the signer has an account with a held key, or another key, for an agent
  with its own key.
- `session_scope`: a live session's scope in the signer differs from what the origin says: run
  import again.
- `steward_without_person`: an admin in Authentik with no person in the register. Nothing to
  carry; accept by number, `--accept-missing-stewards N`, or give them a membership first.

**What members notice.**

- **The web chat and the phone apps stay signed in.** Their sessions are carried with their
  bunker keys and client keys, so sealed conversations keep their devices. They cannot send for
  the minutes of the switch.
- **Apps of the suite** (calendar, vote, groups, village; only where they run) keep signing
  over NIP-46 and sign in again at their next token refresh; **the wallet** signs in again at
  once (section 6).
- **A sign-in link or bunker URL that was handed out and never connected to** no longer works:
  ask for a new one. (`never_connected` in the counts.)
- **Outside apps an admin approved** keep their session with the limits of an outside app
  (section 2): no profile or contact list, nothing for the belfry's own hosts, relays, labels
  or app data.
- **`apps.<domain>`**: Keycast's pages are gone (dashboard, security, key export, move to an
  own key: section 10). The address leads to `belfry.<domain>`.
- Members who hold their own key, remote apps (`remoteapps.py`), passes in wallets, mail,
  push: nothing changes. `nostr.json` answers the same names.
- Admins are who they were. A session of Authentik (`login.<domain>`) ends; nobody uses one
  on a belfry with `PASS_SIGNIN=belfry`.

**Rolling back.** Before the switch there is nothing to roll back: stop the import's signer
(`BELFRY_SIGNER_IMPORT=1 belfry compose stop signer`) or leave it. After it, `… rollback`:
Keycast and Authentik come back as they were, except for what rollback writes into Keycast
first: **sign-outs and suspensions made on the signer since the switch** (a session Keycast
still had live that the signer revoked is revoked in Keycast; an account active in Keycast and
suspended on the signer is suspended), so a rollback does not sign anybody back in nor lift a
suspension. What does not go back: sessions opened since end (those devices sign in again),
reactivations (they stay suspended in Keycast), usernames and admins changed since are as they
were before, people who joined since get the same key from Keycast when it is a derived one
(the same seed), and nobody else has any other. The signer's volume is kept, so a second
attempt starts from it (`existing` rows).

### 6c. Recovering a lost signer volume

On a belfry that runs on its signer, `signer-data` is the only place sessions, usernames and
`imported` keys live. If the volume is lost:

1. **Restore it from the backup** (`backup.sh restore`): nothing else brings back sessions,
   usernames or keys made before the custody seed.
2. Only when there is no backup of it: the service refuses to start on a signer that holds none
   of its members (`signerguard.check_start`). `BELFRY_SIGNER_RECOVERING=1 belfry config` then
   `belfry apply` lets it start (the variable reaches the service through
   `docker-compose.signer.yml`); every account is made again **only with the register's key**
   (`expect_pubkey`), so derived keys come back and nothing else is ever made. Members with an
   `imported` key cannot be recovered this way (`KeyNotHeld` in the log names them); every
   device signs in again; usernames are given again from names.
3. `BELFRY_SIGNER_RECOVERING=0 belfry config` and `belfry apply` as soon as the service runs.

**Never run.** No part of the script has run against Docker, Postgres, a real signer or
Authentik: the SQL (column names and types are from upstream's migrations), `compose run` with
the import override, the image pull or build, `backup.sh` from inside it, `belfry config` and
`apply` in sequence, the health waits. The Python side is unit-tested against rows shaped like
§6's and a stand-in for the signer that applies its rules. The first run belongs on a copy of a
real belfry restored from a backup (`backup.sh drill` makes one), not on a belfry.

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

What was run in phase 3's first part: the full Python suite in both modes (default 1231
passed, 1 skipped; `BELFRY_TEST_SIGNER=belfry` 1208 passed, 23 skipped, and 1 failed:
`test_agents.py::test_a_guest_agent_becomes_its_members_own_and_leaves_with_them`, a known
flake that passed three times alone in that mode), `bash -n` on `migrate-to-own-signer.sh` and `belfry` (shellcheck is not installed on the
machine), and `docker compose config` on the merged files with `docker-compose.signer-import.yml`
(the signer comes up with `BUNKER_RELAYS` empty next to Keycast, Authentik and Redis; the
onboarding service keeps `SIGNER` unset and gains the signer's network and token; with the
prebuilt images the signer's image is the tagged one) and with `docker-compose.signer.yml`. No
Docker container, no cargo, no Rust change: the disk was nearly full again (4 GB free). So the
signer does **not** yet do what section 11 lists, and `limits-next.json` is tested in Python
only.

What was run in the Rust round: `cargo clippy --all-targets -D warnings` clean and `cargo test`
60 passed, 1 ignored; the ignored relay test passed on its own against a local strfry; `docker
compose config` of the merged files with `docker-compose.signer.yml` and with
`docker-compose.signer-import.yml` (who is on which network, `SIGNER_LISTEN`, `SIGNER_URL`); the
network split itself with three stand-in containers (`caddy:2` with `nc`) on two internal
networks: the alias resolves only on its own network, the onboarding stand-in is answered, the
relay stand-in is refused and cannot resolve the alias. Python: focused files only (the full
suite was not run in this round): see the commit message of 29eaa90. Not run: the signer's
image, the signer in a real stack (the bind to `signer-api:8200` inside Compose, the healthcheck
against that address), the migration script end to end.

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
7. **A suspended member's address** keeps resolving in `nostr.json` (section 1d), as with
   Keycast. Should a closed membership lose its NIP-05 name? Then both signers' answers change
   together, after the move.
8. **Usernames that look like member numbers** are now refused to people who choose one
   (`50`, `member-12`, `guest-12`, and any `xx-NN`: two to six letters, a dash, two or more
   digits, the old `osv-0007` shape). That last pattern also refuses `bob-42`. Narrow it to the
   belfry's own member-number prefix?
9. **Consent to decrypt for an outside app**: the flag exists on a sign-in (`grants.decrypt`)
   and on the signer's connection (`decrypt`, set at creation or by `…/connections/consent`),
   and nothing sets it. The sign-in page needs the question (drawn first), or outside apps
   never read private messages, over `/api/nostr` or over NIP-46.

## 10. Not available yet in belfry mode

For the next rounds. Each is absent, not broken: nothing on a belfry with its own signer
offers it.

| What | Where it stands | Where to pick it up |
| --- | --- | --- |
| **Move to an own key** (self-custody) | The signer can (`POST /accounts/{id}/own-key`: `services/signer/src/api.rs:205`, route `:549`) and the service knows what follows (`core.key_changed`, `core.py:603`; Keycast's hook `app.py:552` stays routed and unused). No page and no endpoint join them: a member cannot do it | a page with a fresh e-mail code and a proof of the new key (canvas first), calling the signer and then `key_changed` |
| **Key export** | The signer can (`POST /accounts/{id}/export`: `api.rs:335`, `signer.rs:730`); nothing in the service calls it | the same page; `clients/signer.py` has no `export` method on purpose until then |
| **EUDI-wallet sign-in** (sign in with the pass) | Lived in Authentik through the bridge (`bridge.py:110` `start_authorization`, an OIDC provider for Authentik's source). Nothing calls it without Authentik | its own step in `passlogin.py` (the bridge's verifier side is reusable), if wanted (section 9, question 4) |
| **`nostrconnect://`** to the signer | Not answered by the signer (`services/signer/README.md:112`). Members' other Nostr apps go through the service's own remote signer (`remoteapps.py:4`, `:176`), which works in both modes | nothing to do unless the signer should answer it itself |
| **The issuer's own login** (walt.id Issuer2's authorization-code flow) | Points at Authentik (`infra/issuer/config/authentication-service.conf.tmpl:12-13`; the shim `app.py:767`, `:777`). Passes are issued with a pre-authorized code, which does not use it; whether Issuer2 starts without Authentik is not verified | start a stack and see; then drop the config or point it at the bridge |
| **`smoke.sh` and the `*-check` scripts** | Drive Keycast's and Authentik's pages (`infra/scripts/smoke.sh:29-30`, `:380`, `:387`, `:486`; `silent-signin-check.sh`, `self-custody-check.sh`, `chat-login-check.mjs`, `wallet-check.mjs`, …). `signer-smoke.py` is the only check of this mode | a `smoke.sh` branch for `SIGNER=belfry` (e-mail and code sign-in, `/api/oauth/*` of `appsignin.py`) |
| **Consent to decrypt, profile and contacts for outside apps** | Refused (`remoteapps.py` `_forbidden`, `allowed`; `appsignin.py` `rpc`) with no way for a member to allow them | section 9, questions 2 and 9 |

## 11. The Rust round

What the review and the migration asked of the signer, and where each now stands (signer
4ff0e25, network 8cda7ef, Python 29eaa90):

| | Item | What was done | Where |
| --- | --- | --- | --- |
| 1 | A failed `renew` kept `subscribed` and waited 300 s; `wait_subscribed` returned at once while disconnected | A round that fails means "not subscribed" at once (`/health` says `disconnected`), the next 5-second tick tries again, then 5, 10, 20, 40, 60 s; a change is always tried at once. `wait_subscribed` is bounded and returns `BunkerUnavailable` unless the subscription covers the change (`Ok` only with the bunker off). The API takes the new connection back and answers 503 `bunker_unavailable` (an `http` connection is answered all the same; the session import writes its rows either way) | `bunker.rs` `Renewal`, `session`; `signer.rs` `wait_subscribed`; `api.rs` `create_connection` |
| 2 | `ensure_account` check, then upsert | `Store::create_account`: one transaction, `INSERT … ON CONFLICT DO NOTHING`, then read: two first calls end with the first one's row; the second is answered as for an existing account (409 `mismatch` when its key differs). `import_sealed` too. `put_account` is left for the move to an own key | `store.rs` `create_account`; `signer.rs` `ensure_account`, `import_sealed`; test `two_first_calls_at_once_never_replace_a_key` (16 concurrent requests, 8 threads) |
| 3 | Rate-limit bucket before `verify()`; no freshness or replay check | Order: lookup, signature, freshness (`created_at` within `FRESH` = 600 s either way), event id taken once (`Seen`, ids kept `SEEN_FOR` = 1260 s, 200,000 at most), then the rate limit of the proven sender. The relay's look-back (60 s) is well inside the window, and the de-duplication moved from the loop into `respond`, after the signature, so a forgery never marks an id | `bunker.rs` `respond`, `Seen`, `FRESH` |
| 4 | Unclaimed connections migrated from PR #7's schema never expired | At every start, an unclaimed live connection without a deadline gets ten minutes from that start | `store.rs` `migrate` |
| 5 | `/import/connections` body limit | 1 MB and 1000 rows for that route (`DefaultBodyLimit`), 413 `too_large` in the API's shape; other routes keep axum's 2 MB | `api.rs` `MAX_IMPORT_BODY`, `import_connections` |
| 6 | More limits for outside connections | 30078 `belfry:…` and 1985 `belfry`/`belfry/…` refused; decrypt only with the connection's `decrypt` flag (column, set at creation, by `POST …/connections/consent`, carried by the import; API and NIP-46 alike). One vector file; `create_bunker(..., decrypt=False)` and `connect.redeem` carry it (False until a page asks) | `limits.rs` `forbidden`, `method_refused`; `bunker.rs` `handle`; `api.rs` `crypt`, `consent`; `vectors/limits.json`; `clients/signer.py` |
| 7 | Verify could not read revoked rows nor list accounts | `GET …/connections?all=true` (with `revoked_at`) and `GET /accounts` (paged, by status, with totals). `migration.verify` checks every revoked row and that the signer holds no account Keycast did not have; an older signer shows as `not_verifiable`, never as checked | `api.rs` `list_connections`, `list_accounts`; `migration.py` `verify`, `SignerAdmin` |
| 8 | A mode that makes no accounts | `no_new_accounts` (stored, `POST /settings`; or `SIGNER_NO_NEW_ACCOUNTS=1`, which the API cannot lift): an unknown account is 409 `no_new_accounts` unless the caller names its key (`own_pubkey` or `expect_pubkey`); `/import` goes on. `signer-migrate import` closes it, `switch` opens it after the clean verify after the switch (`open` by hand); the client raises `NoNewAccounts`; the start check says when it is closed | `signer.rs` `accounts_closed`; `api.rs` `post_settings`; `migration.py` `cli`; `migrate-to-own-signer.sh` `cmd_switch`, `cmd_open`; `signerguard.py` `check_start` |
| 9 | A connection's origin | `origin` column, given at creation and by the import, listed; the client gives it as `redirect_origin` | `store.rs`; `clients/signer.py`; `apps.origin_of`; `connect.py`, `sessions.py`, `appsignin.py`, `migration.connection_row`; `discovery.chat_session` |
| 10 | The API on one interface | `SIGNER_LISTEN=signer-api:8200` on a network the relay is not on (section 1d) | `main.rs`; `infra/docker-compose.signer.yml`, `docker-compose.signer-import.yml` |
| 11 | A connection kind for HTTP sign-ins | `kind: http`: stays when nobody connects; at its deadline only its secret ends (the bunker stops listening for it, nobody can bind it). The apps' token exchange uses it; `with_grants` keeps its fallback | `store.rs` `LIVE`, `LISTENING`, `expire_unclaimed`; `appsignin.py` `exchange` |

Decided differently from the list, and why:

- **Item 1, what the API does when the bunker is not listening**: it takes the connection back
  (revokes it) before answering 503, rather than leaving a row nobody will ever use. With the
  bunker off (`BUNKER_RELAYS` empty, the import override) nothing waits and nothing fails: no
  listener was promised.
- **Item 3, the window**: ten minutes either way, as asked; wider than the 60 s look-back, so
  what a renewed subscription brings back is never stale. The memory of ids is in the process:
  a restart forgets them, and a request replayed after a restart is still refused once it is
  ten minutes old (at most ten minutes of exposure after a restart, to an answer only the
  client can read).
- **Item 6, kind 0 and 3**: unchanged (still refused to outside apps; section 9, question 2).
- **Item 8, in the service**: the Python guard (`expect_pubkey`) stays; the switch is the
  second lock and is set by the migration only. No belfry on Keycast is affected.
- **Item 11, Python**: the workaround was not removed, only made the exception: a grant's
  connection is now in the signer's list, and `with_grants` covers sign-ins made before.

Left:

- **Seen across restarts** (item 3): in memory only, see above.
- **The healthcheck** (`belfry-signer healthcheck`) dials `SIGNER_LISTEN` with `0.0.0.0`
  replaced by `127.0.0.1`; with `signer-api:8200` it dials the name, which resolves inside the
  container to the signer's own address on that network: should work, **not run**.
- **The consent page** (section 9, question 9) and a member's Devices list showing it.
- **`GET /accounts` for the start check**: `signerguard.check_start` still samples 40 members;
  it could compare all now.

## 12. The migration's review

An independent review of the migration (script, `migration.py`, `signerguard.py`, the signer's
import) found these; all done on `own-signer` (signer e565ea7, the rest 4f956ab and after):

| | Finding | What was done | Where |
| --- | --- | --- | --- |
| 1 | Agents block every phase: their own key is on the pass and Keycast has no account (`member_not_in_keycast`, `member_not_in_signer`); after the switch `member_token` for an agent raised `KeyNotHeld` | A known class: `signerguard.members` says `own` / `own_pass`; the plan counts them (`own_key_no_account`), imports nothing, verify checks the signer has no account or a keyless one with that key (`own_key_account`). A Keycast account minted for such a partner (what `member_token` did with Keycast: it asked for the partner's token and Keycast made a **derived key that signed as somebody else**) is a `phantom`: left behind, a problem only when somebody signs in on it. `community.member_token` returns None for own-key people in both modes (its callers handle None as for self-custody). The test world's agent is now what the register makes | `signerguard.py` `members`, `own_key_person`; `migration.py` `plan`, `verify`; `community.py` `member_token`; tests `test_agents_with_their_own_key_…`, `test_the_signer_is_never_asked_for_an_agents_account` |
| 2 | A sign-out, a key change or a shared key changing hands between import and switch aborted the switch with no remedy | Import is a sync (section 6b, "How writes … are kept out"): the signer brings existing rows to the import's state while new accounts are closed (only the migration writes then; un-revoking is allowed only there), rows Keycast deleted are revoked, keys released and handed over in two passes, own-key moves done with the held key archived (`archived_keys`, never dropped) | `store.rs` `import_connections(…, sync, skipped)`, `put_account`; `signer.rs`; `migration.py` `run_import`; tests `an_import_run_again_in_the_migrations_window_…`, `a_held_key_replaced_by_an_own_key_is_archived_sealed`, `test_running_import_again_follows_keycast_…`, `test_a_session_signed_out_in_keycast_…` |
| 3 | Custody never checked against the register | Plan problem and verify mismatch `register_custody` (Keycast / signer vs `belfry_key_custody`); after the switch the signer's custody is compared. Cleanup wants, when `imported` keys exist (`signer_custody` in the report), a backup from after the switch whose own record says it carries signer-data, checksum checked; path and sha in the state | `migration.py` `plan`, `verify`; `migrate-to-own-signer.sh` `cmd_cleanup`, `backup_holds_signer`; `backup.sh` (the `<name>.json` record) |
| 4 | Scope never verified nor corrected; a chat session could be carried as outside | `scope_of` from `redirect_origin` as Keycast writes it (section 6, "scope"; verified in the patched source: `""` from `create_bunker`, https origin from the OAuth flow, `nostrconnect://…` from nostr-login); unknown → own, counted and listed as guessed; verify compares; the sync corrects | `migration.py` `scope_of`, `outside_origins`; test `test_scope_comes_from_keycasts_origin` |
| 5 | The switch did not hold the apply lock; a dropped SSH session could leave a half-applied stack | `take_apply_lock` (flock on the apply lock, refused after 120 s) in switch, rollback, cleanup, with `BELFRY_APPLY_LOCKED=1` for the inner apply; `need_tmux` (`--no-tmux` to override) | `migrate-to-own-signer.sh` |
| 6 | `SIGNER_RECOVERING` not wired | Through `docker-compose.signer.yml` and `BELFRY_SIGNER_RECOVERING` in `belfry config`; procedure in section 6c | `docker-compose.signer.yml`, `infra/scripts/belfry` |
| 7 | Rollback undid sign-outs and suspensions | `signer-migrate carry-back` lists sessions Keycast has live that the signer revoked and accounts Keycast has active that the signer suspended; the script writes them into Keycast in one transaction before Keycast starts (two `UPDATE`s against the schema at `fe9c646`; never run). The printed text says what is not carried | `migration.py` `carry_back`; `migrate-to-own-signer.sh` `keycast_carry_back`, `cmd_rollback` |
| 8 | Keys-only verify could pass with no Keycast rows; `account_count` compared a count with itself | `keycast_empty` in every mode; `account_count` compares Keycast's carried accounts with the signer's own list (and with what lookups found) | `migration.py` `verify` |
| 9 | `unverified` sessions only counted | Blocking unless `--accept-unverified N` matches exactly | `migration.py` `verify`; `__main__.py` |
| 10 | One underivable row failed a whole account | The signer skips that row and lists it (`GET /import/skipped`); verify blocks unless `--accept-skipped-rows N` matches | `signer.rs` `import_connections`; `api.rs` `import_skipped`; `migration.py` |
| 11 | Smaller | Cleanup stops at the first failed deletion; a backup's age only from the newest backup file's name, never from `BELFRY_BACKUP_STAMP`, never from the future; the signer's messages shortened (`shorten_keys`); reports and state bound to the box (domain, master key fingerprint, Postgres system identifier); a failing app-sign-in store no longer stops a sign-out | script; `migration.py` `SignerAdmin._error`; `discovery.py` sessions revoke |
| 12a | `usernames.problem` refused "lina-10", "max-42" | `DEFAULT` = digits, `[a-z]{2,6}-\d{4}` (Odoo's sequence pads to four; belfry.sh's old numbers are `OSV-0001`), `member-N`, `guest-N`; the same narrower set is what `give_default` and `suggestions` skip (they skipped "lina-10" too) | `usernames.py`; test `test_nobody_chooses_a_name_accounts_start_with` |
| 12b | A `reset.json` from before kept a belfry closed | A file without `"open"` counts as open, as it did; the next first admin writes `"open": false` | `setup.py` `reopened`, `_close_reopening` |
| 12c | Default-mode changes not listed | Below; one more undone: `gen-secrets.sh` copied Keycast's master key to `signer_master_key` on every `belfry update`; now only when the signer is wanted (`SIGNER=belfry`, `--own-signer`, the migration's import, `belfry config` switching) | `gen-secrets.sh`, `infra/scripts/belfry` `cmd_config`, `migrate-to-own-signer.sh` |

**What Keycast stores in `redirect_origin`**, per path (patched source at `fe9c646`, read in
the scratchpad copy; nothing seen in a real database):

- the onboarding service's sessions (`create_bunker`: chat e-mail sign-in, phone apps' native
  sign-in through `POST /api/v1/session`, guests, tickets, site sessions, setup): `""`,
  verified (`auth.rs:1812`; the client sends no `origin`);
- the chat's and the suite's OAuth (`apps.<domain>/api/oauth`): scheme, host and port of the
  redirect URI (`oauth.rs` `extract_origin`), verified; custom schemes are refused there, so
  `sh.belfry.app://connect` never appears;
- nostr-login: `nostrconnect://<client pubkey>`, verified (`oauth.rs:5071`); which app it was
  is not in the row: guessed own;
- the OIDC start (`oidc.rs:899`): the authorize request's redirect origin, else the app origin:
  verified in code, not which rows exist.

**Decided differently**: un-revoking a row is allowed in the sync (the reviewer asked only for
revoking): in the migration's window every connection row came from an import, so Keycast's row
is the truth both ways; outside it the signer never changes an existing row. A phantom
account is not carried (the signer would hold a key the register says nobody holds). Cleanup's
"verified backup" is the backup's own record (`<name>.json`: volume file counts and checksum),
not a restore drill.

**Not verified**: everything against a real Keycast database, signer, Postgres or Docker (the
two rollback `UPDATE`s, `pg_control_system()`, the lock under `runuser`, tmux detection through
`runuser`); whether belfry.sh's agents ever got a phantom account; the sidecar in a real
`backup.sh run`.

### Changes in default mode (`SIGNER=keycast`)

What this branch changes on a belfry that stays on Keycast, each with its reason:

- `/setup` stays closed on a belfry that has people and no admin on record until `belfry
  setup-reset` (which now opens it on purpose): a lost `first-admin.json` must not hand a
  belfry with members to the first comer (section 6, "The guards"). A `reset.json` from before
  keeps its old meaning.
- Usernames that accounts are made with (digits, `xx-0001`, `member-N`, `guest-N`) cannot be
  chosen, also by the first admin: a chosen "50" blocked member 50's account for ever.
- A taken member-number username no longer stops an activation: the account is made as
  `member-<id>` (then `member-<id>0NNNN`), and gets its name later (`core.preload_account`);
  whether Keycast's 409 body carries the word "username" as this expects was not checked.
- `community.member_token` returns None for people whose pass names their own key (agents):
  Keycast minted a derived key for the agent's partner that signed as somebody else.
- The first admin's setup session is bound to the browser's key when it is made (Keycast patch
  0022), and `/connect/token` binds an app's session to the `client_pubkey` it sends (409
  `key_taken`, 400 for a malformed key, before the code is spent): one client key, one identity
  from the first second.
- `/connect/authorize?prompt=none` for an app no admin approved answers a 403 page instead of
  redirecting with `error=login_required` (section 1d, item 5; the skills say so).
- Remote apps (nostrconnect) may no longer sign 30078 `belfry:…` or 1985 `belfry`/`belfry/…`
  events: the same limits as an outside app on the signer.
- A failure of the app-sign-in store never stops a session's sign-out (it does nothing with
  Keycast anyway).
- `chat_session` counts a session whose origin is a phone app's address as a chat session: no
  effect with Keycast, which never stores such an origin.
- `belfry config` refuses to change `SIGNER` on a belfry that has, or had, people without
  `BELFRY_SIGNER_MIGRATED=1`; accepts `BELFRY_SIGNER_RECOVERING` (no effect with Keycast).
- `backup.sh` writes `<backup>.json` next to each backup (volume file counts, checksum; no
  content), removes it with its backup, pauses `signer` too when it runs, and carries
  `signer-data` when it exists.
- `gen-secrets.sh` adds `SIGNER_TOKEN` to `.env` (an unused random value with Keycast) and
  `SIGNER=keycast` from `.env.example`; it no longer copies Keycast's master key unless the
  signer is wanted.
- The service refuses to start on a `SIGNER` value other than `keycast`, `belfry` or empty (a
  typo used to mean Keycast).
- `console.set_admin` no longer fails early when the stewards group is missing (`add_steward`
  checks); `_set_participant` skips an empty Authentik id.
- `belfry`: local edits under `services/signer` make it build every image on the box (as any
  local change does); new CLI commands (`import-stewards`, `signer-migrate`, `has-people`) exist
  in every mode.
