# belfry-signer

Belfry's signer: the service that holds the members' Nostr keys, written to replace Keycast
(Divine's fork with 23 Belfry patches). On the `belfry-sh` line it is **not in use yet**: the
onboarding service runs against Keycast unless `SIGNER=belfry` is set, and nothing migrates a
live Keycast yet (`docs/own-signer.md` has the plan and what a migration must carry over).
First written by Friedger for OSV (PR #7). Rust, about 4,000 lines with tests, on
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
(`POST /accounts/{vine_id}/own-key`): it drops the held key and revokes every connection;
because Belfry can recompute a derived key, self-custody always means a new key (the held key
is refused).

Creating an account never changes one that exists. `PUT /accounts/{vine_id}` with
`expect_pubkey` (or `own_pubkey`) for an account that has another key answers 409 `mismatch`
and writes nothing, as does an import that names another key: never a silent new key.

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
  (10 minutes).

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
  old one is closed). Creating a connection over the API waits (at most 1.5 s) until the
  subscription covers it, so a client that connects the moment it has its URL is heard. With
  more than 5,000 live connections it reads every kind-24133 event instead;
- drops a request for a bunker key it does not have on the lookup alone, before its signature
  is checked or anything is decrypted;
- answers 16 requests at a time, each connection within its rate limit: the connection's own
  client 600 at once and 20 a second after that, every other sender together 20 and 1 a second
  (a stranger's flood cannot use up the client's share). Over the limit a request is dropped;
- answers an event once (the last 50,000 event ids are remembered); every subscription asks
  for the last minute too, so a request sent during a reconnect is answered where the relay
  kept it (most relays do not keep kind 24133);
- is supervised: when the relay client's stream ends or the loop panics it starts again after
  1 s, 2 s, … 60 s, with an error in the log. A panic in one request's handler is logged and
  the loop goes on.

**Open (phase 2): signing policy.** Every connection may sign any event and use NIP-04/44 for
its account, as with Keycast's "full access". There is no limit on kinds or on `created_at`
(outboxes and sealed conversations sign older events, so a time window would break them).
Limits for connections of outside apps are still to be designed.

## API

Only the onboarding service calls it: the Compose network `signer` (internal, no internet: the
onboarding service, the bunker relay and the signer), bearer token `SIGNER_TOKEN`.

| Method and path | Body | Answer |
| --- | --- | --- |
| `GET /health` | | `{status: ok\|degraded, seed, bunker: {state: off\|connected\|disconnected, last_event_at, restarts, filtered}}` (no token; nothing about accounts) |
| `PUT /accounts/{vine_id}` | `{key_version?, own_pubkey?, expect_pubkey?, username?}` | the account; creates it if missing, never changes its key. `own_pubkey`: a new account without a key. `expect_pubkey` / `own_pubkey` against another key: 409 `mismatch`. `username` for a new account: 409 `username_taken` |
| `GET /accounts/{vine_id}` | | `{vine_id, pubkey, custody, key_version, status, username}` |
| `POST /accounts/{vine_id}/status` | `{status: active\|suspended}` | |
| `POST /pubkeys/{pubkey}/status` | `{status}` | `{updated}`; 404 when no account has that key |
| `POST /accounts/{vine_id}/own-key` | `{pubkey}` | the account, moved to the member's own key; its connections are revoked |
| `PUT /accounts/{vine_id}/username` | `{username}` | the account; 409 `username_taken` |
| `GET /names/{username}` | | `{name, pubkey, vine_id}` or 404 |
| `POST /accounts/{vine_id}/sign` | `{event, connection?}` | `{event}` signed (a `pubkey` in the template must be the account's) |
| `POST /accounts/{vine_id}/encrypt`, `/decrypt` | `{scheme: nip44\|nip04, peer, text, connection?}` | `{result}` |
| `POST /accounts/{vine_id}/derive` | `{purpose, connection?}` | `{secret}` (64 hex); through a connection only its purposes |
| `POST /accounts/{vine_id}/export` | | `{nsec}` (the onboarding service asks for a fresh sign-in first) |
| `POST /accounts/{vine_id}/connections` | `{app, label?, client_pubkey?, derive?, keep?}` | `{id, bunker_url, bunker_pubkey, pubkey, app, created_at, client_pubkey?, expires_at?}`; 409 `client_key_taken` |
| `GET /accounts/{vine_id}/connections` | | the live connections: `{id, app, label, bunker_pubkey, client_pubkey, derive, created_at, last_used_at, uses, expires_at}` (times in seconds; never a secret) |
| `POST /accounts/{vine_id}/connections/revoke` | `{id}` or `{bunker_pubkey}` or `{app}` or `{}` (all) | `{revoked}` |
| `POST /import` | `{accounts: [{vine_id, pubkey, sealed}]}` | per account `derived`, `imported`, `exists` (same key) or `error` (`mismatch` when the account exists with another key) |

`connection` (the id behind an app's access token) must be live and the account's, else 403
`forbidden`. Errors are `{"error", "message"}`: 401 `unauthorized`, 404 `not_found`, 503
`seed_locked`, 409 `own_key` / `mismatch` / `client_key_taken` / `username_taken`, 403
`suspended` / `forbidden`, 422 `invalid`.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `SIGNER_LISTEN` | `0.0.0.0:8200` | the API |
| `SIGNER_TOKEN_FILE` | `/run/secrets/signer_token` | the API's bearer token (16 characters at least) |
| `SIGNER_MASTER_KEY_FILE` | `/run/secrets/signer_master_key` | seals the keys at rest |
| `SIGNER_DB` | `/data/signer.sqlite3` | accounts and connections (volume `signer-data`; in the backups) |
| `CUSTODY_SEED_FILE` | unset | the custody seed; unset or missing: no new accounts |
| `BUNKER_RELAYS` | | relays the bunker listens on, comma separated (`ws://bunker-relay:8080`); empty: no bunker |
| `BUNKER_PUBLIC_RELAYS` | | the same relays as clients reach them, written into bunker URLs (`wss://bunker.<domain>`) |
| `SIGNER_UNCLAIMED_SECONDS` | `600` | how long a connection made without a client key waits for its first `connect` |
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
cargo test        # 35 tests: key derivation vectors, sealing, store, API, bunker
cargo clippy --all-targets -- -D warnings
# the bunker loop against a real relay (one that passes kind 24133 on, e.g. strfry):
BUNKER_TEST_RELAY=ws://127.0.0.1:7791 cargo test -- --ignored
```

Set `CARGO_TARGET_DIR` outside a small `/tmp`. The image:
`docker compose --profile own-signer build signer` (`Dockerfile`, Rust 1.93 and Debian slim).

## Security

The signer is the most sensitive code Belfry runs: it holds every managed key. It is kept small
(no web pages, no sessions, no policy beyond the connection's purposes), on an internal network
without internet, behind a token only the onboarding service has. What it does not change:
whoever controls the onboarding service can sign as any managed member, and whoever has the
unlocked seed has every derived key. Only members moving to their own keys (a NIP-07 extension or
a phone signer) reduce that. Have it reviewed before it holds real participants' keys.
