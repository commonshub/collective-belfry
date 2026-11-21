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
- Still ahead: the signer's own items (section 11, "For the Rust round"), a rehearsal on a copy
  of a real belfry, the switch of one belfry, then what Keycast had that Belfry does not have
  yet (section 10).

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

**The signer's network.** The signer shares its internal network with the onboarding service
(the API's caller) and the bunker relay (which it dials), so the relay can open the API's port.
The API token, which only the onboarding service holds, is what keeps it out. Giving the signer
two networks would not change that: containers that share any network reach every port of each
other. A real split needs the API to listen on one interface only (section 11).

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

**More limits, in the service today and pending in the signer** (decided in the review): an
outside connection never signs app data named `belfry:…` (kind 30078 with such a `d` tag: the
sealed conversations' anchor `belfry:sealed`, the read state, the channel order, a channel's
inbox and web settings), never a label in a `belfry/…` namespace (kind 1985: a pin, a
proposal's status, a ticket checked in, a vouch, a button's press and answer), and decrypts
(`nip44_decrypt`, `nip04_decrypt`) only when its connection carries a consent flag, as remote
apps' connections do. Kind 5 stays allowed. `remoteapps._forbidden` and `/api/nostr` apply
them now; the signer does not yet, so an outside app that holds a bunker URL can still get
those signed over NIP-46 until section 11's item 6 is done. The vectors are in
`services/signer/vectors/limits-next.json`, a separate file on purpose: `limits.rs`'s test
reads `limits.json` and would fail on cases it does not implement.

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
- **Order** (as `migrate-to-own-signer.sh` does it, section 6b): backup; import and verify
  while the belfry runs, as a rehearsal; then stop Keycast and the service; import once more
  and verify (every public key against Keycast's and the register's `keycast_pubkey`, every
  session); `SIGNER=belfry` and apply (the proxy sends `nostr.json` and the apps' sign-in to
  the service); check one session of each kind (web chat, iPhone, Android) signs without
  signing in again. Roll back = the old settings and Keycast up again; what was made on the
  signer in between does not go back (new accounts there are derived keys and come out the
  same in Keycast; new sessions cannot be carried back).
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

**Before the day**: the belfry runs this code with `SIGNER=keycast` (`belfry update`, `belfry
apply`: so the switch has nothing left to build except the signer); `PASS_SIGNIN=belfry` (a
belfry that signs in through Authentik cannot move: section 10); backups are set up
(`BACKUP_RECIPIENTS`, `BACKUP_TARGET`); the custody seed is unlocked.

| | Command | What it does | What is down |
| --- | --- | --- | --- |
| 1 | `migrate-to-own-signer.sh check` | Read-only. The code's commit, the settings, free disk (stops under 1 GB; a signer built on the box needs about 3 GB for 15 to 30 minutes), the master key, the seed; that Keycast's database, Authentik and the register answer; then the plan: accounts by kind (`member`, `guest`, `agent`, `own_key`, `no_vine_id`), usernames, suspended, sessions by state (`served`, `revoked`, `expired`, `never_connected`, `unverified`), how many live ones are the belfry's own apps' and how many outside apps', and every problem | nothing |
| 2 | `… backup` | A backup from the last hour, or `backup.sh run` now. Without a successful one nothing else runs (import wants one from the last 24 hours, switch and cleanup one from the last hour) | as every night: the services with a data volume are paused for the copy |
| 3 | `… import` | Makes `signer_token` and `signer_master_key` if missing and checks the latter **is** Keycast's `master.key`; starts the signer next to Keycast with its bunker off (`docker-compose.signer-import.yml`: it answers no NIP-46 request, so no request gets two answers); imports keys with the public key each must open to, members' own keys, usernames, suspended state, sessions (per account, 400 rows or 256 kB a call: the signer's body limit is 2 MB), and Authentik's admins into the register's flag | nothing; the belfry runs on Keycast |
| 4 | `… verify` | For every account: the signer's key = Keycast's = the register's; custody, username, state; every served session is live in the signer with its bunker key and client key, and nothing else is; every person of the register is in the signer; the admins. A report file. Exit 1 on any difference | nothing |
| 5 | `… switch` | A fresh backup; asks for `switch <domain>`; **stops Keycast and the service** (nothing writes to Keycast's database from here); a last import; the verify that decides (not clean: Keycast and the service start again, nothing was switched); `BELFRY_SIGNER=belfry BELFRY_SIGNER_MIGRATED=1 belfry config`; `belfry apply`; then waits for the signer, asks the service, and verifies the keys once more | **the belfry**, from the stop to the end of `belfry apply` |
| 6 | `… rollback` | Asks for `rollback <domain>`; stops the signer and removes its container (not its volume); `BELFRY_SIGNER=keycast`; `belfry apply` brings Keycast, Authentik and Redis back on their untouched data | the belfry, for one `belfry apply` |
| 7 | `… cleanup` | Only at a terminal, only with the two clean reports (the one the switch was decided on and one from after it), only seven days after the switch (`--before-seven-days` overrides), after a fresh backup, after the typed sentence `delete keycast and authentik of <domain>`: empties Keycast's and Authentik's databases (they stay, empty: Postgres' health check looks for `keycast`), removes Authentik's two volumes and the five containers. Keeps `infra/keycast/master.key`, the seed, the backups | nothing |

`… status` shows where a belfry is. Options: `--exclude <pubkey>` (leave one Keycast account
out on purpose: an account without a `vine_id` stops check, import and verify until it is named),
`--accept-lost-sessions N`, `--accept-missing-stewards N` (below), `--yes` (no question before
switch or rollback; never for cleanup).

**How writes between import and switch are kept out.** They are not kept out: the belfry runs
normally between steps 3 and 5, people join and sign in, and the rehearsal's verify goes stale
(it then says `account_not_imported` or `session_missing`: run import again). What decides is
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
- `unverified` sessions in the counts (connected, but `verified_client_pubkey` is not the
  connected key): §6 reads Keycast as not serving them, so they are left out. **If that number
  is not zero on a real belfry, stop and read Keycast's `validate_client` again**: if they are
  served after all, those members would be signed out.
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
Keycast and Authentik come back as they were, because nothing wrote to them. What the signer
gained since the switch does not go back: sessions opened since end (those devices sign in
again), usernames and admins changed since are as they were before, people who joined since
get the same key from Keycast when it is a derived one (the same seed), and nobody else has
any other. The signer's volume is kept, so a second attempt starts from it (`existing` rows).

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
   and nothing sets it. The sign-in page needs the question (drawn first), or outside apps
   never read private messages over `/api/nostr`.

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
| **A session's `redirect_origin`** | Always empty (`clients/signer.py:349`, `appsignin.py:459`): the signer keeps no origin. `discovery.chat_session` (`discovery.py:323`) tells a chat session by its name and accepts an empty origin | section 11, item 9 |
| **Consent to decrypt, profile and contacts for outside apps** | Refused (`remoteapps.py` `_forbidden`, `allowed`; `appsignin.py` `rpc`) with no way for a member to allow them | section 9, questions 2 and 9 |

## 11. For the Rust round

Nothing in `services/signer` changed in phase 3's first part. What the signer needs next, from
the review and from writing the migration:

1. **A failed `renew` keeps `subscribed` true and waits 300 s** (`bunker.rs:473-483`: `renew(…)
   || subscribed`, then `renew_at = now + RENEW`), so new connections are not listened for
   until then; and **`wait_subscribed` returns at once while disconnected**
   (`signer.rs:370-375`), so the API answers "made" for a connection nobody listens on.
2. **`ensure_account` checks, then upserts** (`signer.rs:463-500`, `store.rs:386-393`: `ON
   CONFLICT (vine_id) DO UPDATE SET pubkey = …`): two requests at once can both pass the check,
   and the second write replaces the first row. Insert-or-fail in one statement, then read.
3. **The rate-limit bucket is chosen before `event.verify()`** (`bunker.rs:213-218`): a forged
   event with the client's public key spends the client's allowance.
4. **Unclaimed connections migrated from the PR's schema never expire** (no `expires_at`).
5. **The body limit and `/import/connections`**: 5,000 rows a call are allowed
   (`api.rs:465`), axum's default body limit is 2 MB. The migration sends 400 rows or 256 kB a
   call, so it is not hit today; raise the limit for that route or lower the row limit.
6. **More limits for outside connections** (decided; the service applies them already,
   section 2): decrypt (`nip44_decrypt`, `nip04_decrypt`) only when the connection carries a
   `decrypt` consent flag (`POST /accounts/{id}/connections {…, decrypt: true}`, kept on the
   row, as `remoteapps.py` keeps it); never a signature for kind 30078 with a `d` tag starting
   `belfry:` nor for kind 1985 with an `L` tag, or an `l` tag's mark, that is `belfry` or starts
   `belfry/` (trimmed, any case); kind 5 stays allowed. Vectors:
   `services/signer/vectors/limits-next.json` (`cases` for `sign_event`, `methods` for the
   decrypt flag). When `limits.rs` passes them: move the cases into `limits.json`, delete the
   file, point `test_more_limits_the_signer_does_not_have_yet` at the one file, and have
   `clients/signer.py` `create_bunker` and `/import/connections` carry the flag.
7. **Reading for the migration's verify**: `GET /accounts/{id}/connections` lists live
   connections only, so revoked rows that were imported cannot be compared (the report counts
   them as `not_verifiable`); an `?all=true` would close that. And there is no list or count of
   accounts, so verify cannot see an account the signer has that Keycast did not, and the
   start check samples 40 members instead of comparing all.
8. **A mode that makes no accounts**: the guard against new keys is in the service
   (`expect_pubkey` on every creation, section 6). A signer switch (`SIGNER_NO_NEW_ACCOUNTS=1`,
   or refusing `PUT` without `expect_pubkey` / `own_pubkey` once it holds accounts) would hold
   even if a caller forgot.
9. **A connection's origin**: keep the address a session was opened from (`origin` on
   creation, in the list), so `redirect_origin` is not always empty and the migration can
   carry Keycast's.
10. **The API on one interface**: the bunker relay shares the signer's network and reaches
    the API's port (section 1d). A second listener address, or a Unix socket for the API,
    would let Compose keep the relay out.
11. **A sign-in without a connection**: the service no longer ties an app's HTTP sign-in to a
    NIP-46 connection (section 1c), and `/sign` takes a `connection` id whose limits it applies
    (README, "API"). If HTTP sign-ins should be limited by the signer too, it needs a kind of
    connection with no bunker key and no deadline for them to name.
