# belfry-signer

Belfry's signer: the service that holds the members' Nostr keys, written to replace Keycast
(Divine's fork with 23 Belfry patches). On the `belfry-sh` line it is **not in use yet**: the
onboarding service runs against Keycast unless `SIGNER=belfry` is set, and no migration of a
live Keycast has been run (`docs/own-signer.md` has the plan and what a migration must carry
over; the signer's side of it is `POST /import` and `POST /import/connections`, below).
First written by Friedger for OSV (PR #7). Rust, about 8,300 lines with tests, on
[rust-nostr](https://github.com/rust-nostr/nostr) (keys, events, NIP-04, NIP-44 and the relay
client) and RustCrypto's AES-GCM and HKDF.

It does three things:

- **holds the keys**: derived from the custody seed, sealed at rest with the master key;
- **signs for the onboarding service** over an internal API (profiles at activation, the apps'
  HTTP RPC, the wallet's derived secrets, key export, the move to an own key);
- **answers NIP-46** as a bunker on the bunker relay, one key per app connection.

Everything else (sign-in, which app may do what, sessions pages, the `nostr.json` answer,
admins) is the onboarding service's. The signer knows accounts, their usernames and their
connections, not people.

## Keys

An account is keyed by its `vine_id`: `odoo-partner-<register partner id>` for everyone the
onboarding service creates today (members, guests and agents alike; the id is letters, digits,
`-` and `_`, up to 64). Its key is

```
HKDF-SHA256(salt "belfry/custody/v1", IKM = custody seed, info "belfry/nostr/v1/<vine_id>/<version>")
```

(an output outside the curve order, about 2^-128, moves on to `info …/<version>#1`, `#2`, …),
with the version from Odoo (`res.partner.belfry_key_version`). This is Keycast patch 0014's
formula, byte for byte (the test vector is in `src/keys.rs`), so every npub stayed the same.

The seed file (`CUSTODY_SEED_FILE`, 64 hex characters) is read only when an account is created
or restored. A created key is sealed with the master key (`SIGNER_MASTER_KEY_FILE`: 32 random
bytes, base64; AES-256-GCM, stored as `nonce || ciphertext`) in the SQLite file. So signing goes on
while the seed is locked (after a reboot it is in memory only, `infra/DEPLOY.md`), and a new
account waits (503 `seed_locked`) instead of getting a key that could not be recovered.

The master key is Keycast's (`FileKeyManager`: 32 random bytes, base64), on purpose: a
migration hands Keycast's sealed keys to the signer as they are (`POST /import`), including
keys made before the custody seed existed, which the seed cannot give back. The signer marks
those `imported` and the ones the seed reproduces `derived`.

What the signer writes itself is bound to its row: `0x02 || nonce || ciphertext`, with
`account:<vine_id>` (or `connection:<id>` for a bunker key) as AES-GCM associated data, so a
blob copied onto another row does not open; and after opening, the key is compared with the
public key the row names. Keycast's format (`nonce || ciphertext`, no associated data) is
opened only by the import and by a one-time upgrade at start (a database of the signer's first
version), never on the signing path.

Per-purpose secrets (the village wallet's seed and nutzap key) are

```
HKDF-SHA256(salt "keycast/derive/v1", IKM = the member's 32-byte secret key, info = purpose)
```

again Keycast's (patch 0011; the salt keeps its name so wallets keep their tokens).

A member who holds their own key has an account with `custody = own`: the signer keeps no key
and refuses to sign (409 `own_key`). Moving there is a call of its own
(`POST /accounts/{vine_id}/own-key`): it puts the held key away (sealed, in `archived_keys`:
never dropped; the answer's `archived_keys` counts them) and revokes every connection;
because Belfry can recompute a derived key, self-custody always means a new key (the held key
is refused).

Creating an account never changes one that exists. `PUT /accounts/{vine_id}` with
`expect_pubkey` (or `own_pubkey`) for an account that has another key answers 409 `mismatch`
and writes nothing, as does an import that names another key: never a silent new key. An
account is made by one insert-or-nothing in a transaction (`Store::create_account`), so two
first calls at once for one id end with one row, the first one's: the second is answered as for
any account that exists (its own key against another one: 409 `mismatch`).

**No new accounts.** While `no_new_accounts` is on (`POST /settings {"no_new_accounts": true}`,
kept in the database; or `SIGNER_NO_NEW_ACCOUNTS=1`, which the API cannot lift),
`PUT /accounts/{vine_id}` for an account the signer does not have answers 409
`no_new_accounts` and writes nothing, unless the caller names the key: `own_pubkey` (no key is
made) or `expect_pubkey` (the account is made only if it comes out with that key). `/import`
goes on. A migration turns it on before it imports and lifts it when the belfry runs on the
signer, so that a caller that forgot to say which key it expects can never mint one in between.

## Usernames

An account may have a username (the NIP-05 name `<username>@<domain>` and the mailbox): lowercase,
unique whatever the case, 1 to 63 of `a-z 0-9 . _ -` starting and ending with a letter or digit
(the onboarding service's own rules are stricter). `PUT /accounts/{vine_id}/username` sets it
(409 `username_taken`), `GET /names/{username}` finds the account. A new account can be given
one when it is made; an existing account keeps its own.

## Bunker connections

Every app sign-in is a connection: a key of its own (the `bunker://<key>` in its URL), the
application's name, when it was made and last used, how often, and the purposes it may derive.
A connection belongs to **one client key**:

- made **for a client key** (`client_pubkey`, what Belfry's own sign-in does: the chat's session
  key, which is also the device key of sealed conversations): bound from the start. No other key
  can use it. No secret is kept, so the URL is not a credential; the client may just start
  sending requests, and `connect` from it (with any secret or none, as often as it likes: apps
  connect again whenever they start) answers `ack`. It does not expire.
- made **without one**: the URL's secret binds the first client key that sends `connect` with
  it. Only the secret's SHA-256 is stored (128 random bits; compared in constant time) and it is
  dropped at that moment: the secret is spent, and `connect` again from the bound client still
  answers `ack`. Unclaimed, the connection stops existing after `SIGNER_UNCLAIMED_SECONDS`
  (10 minutes). No connection waits for ever: one found unclaimed and without a deadline at
  start (the signer's first version made them so) is given ten minutes from that start.

A connection also carries where it was opened from (`origin`, the app's registered address:
Keycast's `redirect_origin`; "" when the caller gave none) and a `kind`:

- `nip46` (the default): an app that talks to the bunker, as above;
- `http`: an app's sign-in that is used through the API (`sign`, `encrypt`, … with
  `connection`), as the wallet's is. It has a bunker URL like any other and an app may connect
  to it within the ten minutes; when nobody does, **the connection stays** (it is listed, used
  over the API and revoked like any other) and only its secret ends: the bunker stops
  listening for it and nobody can take it any more.

**One client key, one identity** (Keycast patches 0022 and 0023): a client key named for or
bound to a connection of one account is refused for every other account, when a connection is
made for it (409 `client_key_taken`, nothing created) and on a first `connect` (`unauthorized:
Client key is linked to another account`, even with the right secret). Revoked and expired
connections keep counting: a key is never freed, also not by the move to an own key.

Revoking a connection (by its id or its bunker key, an app's, or all) ends both its NIP-46
bunker and its use over the API. `keep` on creation revokes all but an app's newest connections.

NIP-46 requests are kind-24133 events p-tagged to a connection's key, NIP-44 v2 encrypted (or
NIP-04 for old clients; the answer mirrors the request). Methods: `connect`, `get_public_key`,
`sign_event`, `nip44_encrypt`, `nip44_decrypt`, `nip04_encrypt`, `nip04_decrypt`, `ping`,
`get_relays`, `switch_relays` (`null`), and Belfry's `derive_secret` for the connection's
purposes. An answer is `{"id", "result"}`, or `{"id", "result": "", "error"}`. A suspended
account gets nothing signed, over the API and NIP-46 alike. `nostrconnect://` (the app's own
URL) is not answered here, as with Keycast: outside apps go through the onboarding service's
remote apps.

The bunker loop:

- asks the relay only for requests to the bunker keys of live connections (`#p`, 500 keys a
  filter), and subscribes again whenever the set changes (the new subscription first, then the
  old one is closed). With more than 5,000 live connections it reads every kind-24133 event
  instead;
- **says when it is not listening.** A subscription that fails (no relay took it) means "not
  subscribed" from that moment: `/health` says `disconnected`, the next 5-second tick tries
  again, then after 5, 10, 20, 40 and 60 seconds, and a change of the set is always tried at
  once. Creating a connection over the API waits (at most 1.5 s) until the subscription covers
  it, so a client that connects the moment it has its URL is heard; when it does not get
  covered, the connection is taken back and the answer is 503 `bunker_unavailable` (the caller
  asks again), never a URL nobody listens on. An `http` connection is answered all the same,
  and with the bunker off (`BUNKER_RELAYS` empty) nothing waits;
- drops a request for a bunker key it does not have on the lookup alone, before its signature
  is checked or anything is decrypted;
- takes a request only when its signature is right, its `created_at` is within ten minutes of
  the signer's clock either way, and its event id was not taken before (ids are remembered
  for 21 minutes, longer than a request stays fresh; up to 200,000, in memory: a restart
  forgets them). Every subscription asks for the last minute too, so a request sent during a
  reconnect is answered where the relay kept it (most relays do not keep kind 24133): the
  minute is well inside the ten;
- answers 16 requests at a time, each connection within its rate limit: the connection's own
  client 600 at once and 20 a second after that, every other sender together 20 and 1 a second
  (a stranger's flood cannot use up the client's share). Whose allowance a request counts
  against is decided **after** its signature is checked: an event that only names the
  client's key as its author spends nothing. Over the limit a request is dropped;
- is supervised: when the relay client's stream ends or the loop panics it starts again after
  1 s, 2 s, … 60 s, with an error in the log. A panic in one request's handler is logged and
  the loop goes on.

### Scope: the belfry's own apps and outside apps

A connection has a `scope`, given when it is made and never changed: `own` (the default: the
chat, the phone apps, everything Belfry signs in itself) or `outside` (an app Belfry does not
run). An `own` connection may sign any event and use NIP-04/44 for its account, as with
Keycast's "full access". An `outside` one is refused `sign_event` (the NIP-46 error
`not allowed for this app`; 403 `forbidden` over the API's `sign` with `connection`) for:

- NIP-98 (27235) without a `u` tag, with a `u` that has no host, or for one of the belfry's
  own hosts: its API hosts, its domain and every name under it (the API acts on that key);
- NIP-42 AUTH (22242) without a `relay` tag or for one of the belfry's own relays (members
  only);
- NIP-46 (24133), NIP-29 moderation and joins (9000 to 9009, 9021, 9022);
- Blossom authorization (24242) without a `server` tag or for the belfry's own storage;
- the profile (0) and the contact list (3);
- app data under the belfry's names: kind 30078 with a `d` tag that starts `belfry:` (trimmed,
  any case): the sealed conversations' anchor, the read state, the channel order, …;
- a label in one of the belfry's namespaces: kind 1985 with an `L` tag, or the mark of an `l`
  tag, that is `belfry` or starts `belfry/` (trimmed, any case): a pin, a proposal's status,
  a ticket checked in, a vouch, a button's press and answer;
- a template that is not an object with a whole-number `kind`.

Kind 5 (a deletion) stays allowed.

**Decrypting needs the member's consent.** An `outside` connection always encrypts
(`nip44_encrypt`, `nip04_encrypt`) and decrypts (`nip44_decrypt`, `nip04_decrypt`) only when
its `decrypt` flag is set: given when the connection is made (`decrypt: true`), carried by the
session import, or set later with `POST /accounts/{vine_id}/connections/consent`. Without it
the answer is the same refusal (`not allowed for this app`; 403 `forbidden` over the API's
`decrypt` with `connection`). An `own` connection decrypts without it.

The rules are the ones of `remoteapps.py` (`_forbidden` with no perms, `allowed`, `_host`,
`own_hosts`), written again in `src/limits.rs`; both pass `vectors/limits.json` (73 `cases`
for `sign_event`, 7 `methods` for the consent). The belfry's own
names come from `SIGNER_OWN_DOMAIN` (which adds the domain, `belfry.`, `pass.`, `apps.`,
`chat.` as API hosts, `relay.`, `groups-relay.`, `bunker.` as relays and `blossom.` as
storage) and the `SIGNER_OWN_*_HOSTS` lists. Without a domain the rules that need no name
still hold, and the signer says so in the log at start.

Hosts are read as Python's `urlsplit(...).hostname` reads them (lower case, no userinfo, no
port, no trailing dot). Where the two could differ the signer reads no host, which refuses: a
bracketed host that is not an IPv6 address, and a host with a character that NFKC turns into
`/ ? # @ :` (a fixed list of 19 characters, not a normalization).

There is no limit on the `created_at` of an event that is **signed**, for either scope
(outboxes and sealed conversations sign older events, so a time window would break them). The
ten-minute window above is on the NIP-46 **request** that asks for it.

## Carrying Keycast's sessions over

`POST /import` brings the keys (above, "Keys"). `POST /import/connections` brings one
account's sessions, so nobody is signed out: each keeps its bunker key and its client key.

Keycast did not store a session's bunker key. It derived it (`core/src/bunker_key.rs` at
`fe9c646`):

```
HKDF-SHA256(no salt, IKM = the account's 32-byte secret key,
            info "keycast-bunker-nip46-v1-" + oauth_authorizations.secret_hash)
```

with the stored `secret_hash` text (a bcrypt hash) as it is; an output outside the curve order
moves on to `info … + "-retry1"`, `-retry2`, …. The signer holds the account's key, derives
the bunker key again, checks its public key against the row's `bunker_pubkey` (the check is
also the proof of the formula for that row) and seals it bound to the new connection row.
Vectors of the formula are in `src/keys.rs`.

Body: `{"vine_id", "connections": [{"bunker_pubkey", "keycast_secret_hash", "client_pubkey",
"app", "label"?, "scope"?, "decrypt"?, "origin"?, "created_at", "last_used_at"?, "uses"?,
"revoked_at"?}]}` (times in seconds; `origin` is Keycast's `redirect_origin`). At most 1,000
rows and 1 MB a call: a larger body is 413 `too_large`, in the API's own shape (every other
route keeps the 2 MB default). The migration sends 400 rows or 256 kB a call. One account a
call, one transaction, all or nothing:

- a row comes in **bound to its client key**, with no secret and no deadline: the app goes on
  sending requests, and `connect` from it answers `ack`. `client_pubkey` is the client Keycast
  served: `connected_client_pubkey` where it equals `verified_client_pubkey`;
- a live row **without a client key** is refused (422): Keycast's secret is bcrypt, the
  signer's SHA-256, so there is nothing to carry. The caller leaves those out;
- a row with `revoked_at` comes in **revoked** (its bunker key is derived, checked and sealed
  like the others; it never answers), so its client key stays linked to the account;
- a bunker key the formula does not give: that row is **skipped** (its index in `skipped`,
  listed by `GET /import/skipped` until an import carries it); the account's other rows go in.
  Every other fault of a row is 422 `invalid`, the message starting with `connections[<index>]:`;
- a client key linked to another account: 409 `client_key_taken`;
- a bunker key the account already has a connection for: left exactly as it is and counted
  as `existing` (importing twice is safe), **unless no new accounts are made** (the
  migration's window, where only the import writes connections): then it is brought to what
  the row says (name, scope, origin, revoked or live, client key; use counts never go down)
  and counted as `updated`; a row that gives up its client key (another account keeps it) is
  `released` and frees it for that account. One that another account has: 409 `mismatch`;
- an account whose member holds their own key: 409 `own_key`; no such account: 404;
- a suspended account's sessions are carried (they answer when it is active again).

The answer is `{imported, existing, revoked, updated, released, skipped}`: live rows written,
rows already there, revoked rows written, rows brought up to date, client keys released, and
the indexes of rows left out. Before answering, the signer waits (1.5 s at most) for the bunker's relay
subscription to cover the new bunker keys (the rows are written either way: a bunker that is
not listening shows in `/health`). `GET /accounts/{vine_id}/connections?all=true` reads them
back, the revoked ones too. Keycast's `expires_at` is not carried (an expired
session is not a live one: leave it out or send it as revoked), nor is `handle_expires_at`
(Keycast's signer does not look at it).

## API

Only the onboarding service calls it, with the bearer token (`SIGNER_TOKEN_FILE`), on a network
the bunker relay is not on. In Compose the signer has two internal networks: `signer` (with the
bunker relay, which it dials) and `signer-api` (with the onboarding service). `SIGNER_LISTEN`
is the signer's alias on the second (`signer-api:8200`): the name is looked up once at start and
the API listens on that one address, so on the relay's network nothing listens on the port (a
name that does not resolve stops the start). `/health` needs no token and says nothing about
who is here: the seed's state and the bunker's.

| Method and path | Body | Answer |
| --- | --- | --- |
| `GET /health` | | `{status: ok\|degraded, seed, bunker: {state: off\|connected\|disconnected, last_event_at, restarts, filtered}}` (no token; nothing about accounts) |
| `GET /settings` | | `{no_new_accounts, by_environment}` |
| `POST /settings` | `{no_new_accounts: true\|false}` | the same; 422 when the environment closes and the body opens |
| `GET /accounts` | query `status=active\|suspended`, `after=<vine_id>`, `limit` (1 to 1000, default 500) | `{accounts: [the account, …], next, total, suspended}` in the order of the ids; `next` is the id to ask `after` while there is more; `total` and `suspended` count all accounts |
| `PUT /accounts/{vine_id}` | `{key_version?, own_pubkey?, expect_pubkey?, username?}` | the account; creates it if missing, never changes its key. `own_pubkey`: a new account without a key. `expect_pubkey` / `own_pubkey` against another key: 409 `mismatch`. `username` for a new account: 409 `username_taken`. An unknown account while no accounts are made and no key is named: 409 `no_new_accounts` |
| `GET /accounts/{vine_id}` | | `{vine_id, pubkey, custody, key_version, status, username}` |
| `POST /accounts/{vine_id}/status` | `{status: active\|suspended}` | |
| `POST /pubkeys/{pubkey}/status` | `{status}` | `{updated}`; 404 when no account has that key |
| `POST /accounts/{vine_id}/own-key` | `{pubkey}` | the account, moved to the member's own key; its connections are revoked |
| `PUT /accounts/{vine_id}/username` | `{username}` | the account; 409 `username_taken` |
| `GET /names/{username}` | | `{name, pubkey, vine_id}` or 404 |
| `POST /accounts/{vine_id}/sign` | `{event, connection?}` | `{event}` signed (a `pubkey` in the template must be the account's); through an `outside` connection within its limits, else 403 `forbidden` |
| `POST /accounts/{vine_id}/encrypt`, `/decrypt` | `{scheme: nip44\|nip04, peer, text, connection?}` | `{result}`; `decrypt` through an `outside` connection without consent: 403 `forbidden` |
| `POST /accounts/{vine_id}/derive` | `{purpose, connection?}` | `{secret}` (64 hex); through a connection only its purposes |
| `POST /accounts/{vine_id}/export` | | `{nsec}` (the onboarding service asks for a fresh sign-in first) |
| `POST /accounts/{vine_id}/connections` | `{app, label?, client_pubkey?, derive?, keep?, scope?, decrypt?, origin?, kind?}` | `{id, bunker_url, bunker_pubkey, pubkey, app, scope, kind, decrypt, origin, created_at, client_pubkey?, expires_at?}`; 409 `client_key_taken`; 503 `bunker_unavailable` (not made: ask again). `scope` is `own` (default) or `outside`, `kind` `nip46` (default) or `http`, `decrypt` true or false (default false), `origin` up to 200 characters; anything else 422 |
| `GET /accounts/{vine_id}/connections` | query `all=true` | the live connections: `{id, app, label, bunker_pubkey, client_pubkey, derive, created_at, last_used_at, uses, expires_at, scope, decrypt, origin, kind, revoked_at}` (times in seconds; never a secret). With `all=true` every connection the account ever had: `revoked_at` is set on the revoked ones |
| `POST /accounts/{vine_id}/connections/revoke` | `{id}` or `{bunker_pubkey}` or `{app}` or `{all: true}` | `{revoked}` |
| `POST /accounts/{vine_id}/connections/consent` | `{id}` or `{bunker_pubkey}`, and `decrypt: true\|false` | `{decrypt}`; 404 when the account has no such live connection |
| `POST /import` | `{accounts: [{vine_id, pubkey, sealed}]}` | per account `derived`, `imported`, `exists` (same key) or `error` (`mismatch` when the account exists with another key) |
| `POST /import/connections` | `{vine_id, connections: [{bunker_pubkey, keycast_secret_hash, client_pubkey, app, label?, scope?, decrypt?, origin?, created_at, last_used_at?, uses?, revoked_at?}]}` | `{imported, existing, revoked, updated, released, skipped}`; all or nothing (but for skipped rows); 422 `invalid` naming the row, 409 `client_key_taken` / `own_key` / `mismatch`, 404, 413 `too_large` ("Carrying Keycast's sessions over") |
| `GET /import/skipped` | | `{skipped: [{vine_id, bunker_pubkey, at}]}`: rows imports left out because their bunker key does not derive |

`connection` (the id behind an app's access token) must be live and the account's, else 403
`forbidden`. Errors are `{"error", "message"}`: 401 `unauthorized`, 404 `not_found`, 503
`seed_locked` / `bunker_unavailable`, 409 `own_key` / `mismatch` / `client_key_taken` /
`username_taken` / `no_new_accounts`, 403 `suspended` / `forbidden`, 413 `too_large`, 422
`invalid` (a field of the wrong type in a typed body, such as `decrypt: "yes"`, is refused by
the body's parser: 422 with a line of text).

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `SIGNER_LISTEN` | `0.0.0.0:8200` | the API. A host name is looked up once at start and the API listens on that address only (Compose: `signer-api:8200`, "API") |
| `SIGNER_NO_NEW_ACCOUNTS` | | `1`: no accounts are made unless the caller names the key ("Keys"); `POST /settings` cannot lift it |
| `SIGNER_TOKEN_FILE` | `/run/secrets/signer_token` | the API's bearer token (16 characters at least) |
| `SIGNER_MASTER_KEY_FILE` | `/run/secrets/signer_master_key` | seals the keys at rest |
| `SIGNER_DB` | `/data/signer.sqlite3` | accounts and connections (volume `signer-data`; in the backups) |
| `CUSTODY_SEED_FILE` | unset | the custody seed; unset or missing: no new accounts |
| `BUNKER_RELAYS` | | relays the bunker listens on, comma separated (`ws://bunker-relay:8080`); empty: no bunker |
| `BUNKER_PUBLIC_RELAYS` | | the same relays as clients reach them, written into bunker URLs (`wss://bunker.<domain>`) |
| `SIGNER_UNCLAIMED_SECONDS` | `600` | how long a connection made without a client key waits for its first `connect` |
| `SIGNER_OWN_DOMAIN` | | the belfry's domain: what a connection of an outside app never signs for ("Scope"); empty: a warning at start |
| `SIGNER_OWN_API_HOSTS`, `SIGNER_OWN_RELAY_HOSTS`, `SIGNER_OWN_BLOSSOM_HOSTS` | | more of the belfry's own hosts or URLs (the internal relay name, a storage host elsewhere), separated by commas or spaces |
| `RUST_LOG` | `info` | log filter |

In Compose the container runs as the deploying user (`HOST_UID`, the owner of the custody seed
in `/run/belfry`), read-only, without capabilities, 128 MB.

`belfry-signer derive <vine_id> [<version>]` prints the public key the seed gives (a check
without the service; the smoke test uses it). `belfry-signer healthcheck` is the container's
health check.

## Recovery

- **Lost the signer's data** (volume): restore it from a backup. Without one,
  `python -m belfry_onboarding restore-keys` recreates the derived keys from the register with
  the seed unlocked (same npubs), but every connection is gone (everyone is signed out, and a
  chat session's client key is its device key for sealed conversations), usernames must be set
  again, and keys from before the seed are lost. The backups must carry `signer-data` together
  with `signer_master_key`.
- **Lost the master key**: the sealed keys are unreadable; derived keys come back with
  `restore-keys` into a fresh `signer-data` (move the old volume away first); imported ones are
  lost. Back the master key up (it is in `infra/secrets/`, so in the backups).
- **Lost the custody seed**: nothing breaks until a new account is needed; the seed is kept as
  `custody-seed.age` under the stewards' YubiKeys (`infra/DEPLOY.md`, "Custody seed").

## Develop

```
cargo test        # 61 tests (one needs a relay): key derivation vectors, sealing, store, API, bunker, limits
cargo clippy --all-targets -- -D warnings
# the bunker loop against a real relay (one that passes kind 24133 on, e.g. strfry):
BUNKER_TEST_RELAY=ws://127.0.0.1:7791 cargo test -- --ignored
```

Set `CARGO_TARGET_DIR` outside a small `/tmp`. The image:
`docker compose -f infra/docker-compose.yml -f infra/docker-compose.signer.yml build signer`
(`Dockerfile`, Rust 1.93 and Debian slim; the second file is the only place the signer is in Compose).

## Security

The signer is the most sensitive code Belfry runs: it holds every managed key. It is kept small
(no web pages, no sessions, no policy beyond the connection's purposes and its scope), on an internal network
without internet, behind a token only the onboarding service has. What it does not change:
whoever controls the onboarding service can sign as any managed member, and whoever has the
unlocked seed has every derived key. Only members moving to their own keys (a NIP-07 extension or
a phone signer) reduce that. Have it reviewed before it holds real participants' keys.
