# belfry-signer

Belfry's signer: the only service that holds the members' Nostr keys. It replaced Keycast
(Divine's fork with 21 Belfry patches) on 2026-11-19. Rust, about 2,300 lines with tests, on
[rust-nostr](https://github.com/rust-nostr/nostr) (keys, events, NIP-04, NIP-44 and the relay
client) and RustCrypto's AES-GCM and HKDF.

It does three things:

- **holds the keys**: derived from the custody seed, sealed at rest with the master key;
- **signs for the onboarding service** over an internal API (profiles at activation, the apps'
  HTTP RPC, the wallet's derived secrets, key export, the move to an own key);
- **answers NIP-46** as a bunker on the bunker relay, one key per app connection.

Everything else (sign-in, which app may do what, the account page, NIP-05 names, stewards) is the
onboarding service's (`../onboarding/belfry_onboarding/connect.py`, `account.py`, `login.py`).
The signer knows accounts and connections, not people.

## Keys

A member's account is keyed by `vine_id = odoo-partner-<Odoo partner id>`. Its key is

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

The master key and the sealed format are Keycast's (`FileKeyManager`), on purpose: the move
copied Keycast's sealed keys into the signer unchanged (`POST /import`), including keys made
before the custody seed existed, which the seed cannot give back. The signer marks those
`imported` and the ones the seed reproduces `derived`.

Per-purpose secrets (the village wallet's seed and nutzap key) are

```
HKDF-SHA256(salt "keycast/derive/v1", IKM = the member's 32-byte secret key, info = purpose)
```

again Keycast's (patch 0011; the salt keeps its name so wallets keep their tokens).

A member who holds their own key has an account with `custody = own`: the signer keeps no key
and refuses to sign (409 `own_key`). Moving there (`own_pubkey`) drops the held key and revokes
every connection; because Belfry can recompute a derived key, self-custody always means a new key.

## Bunker connections

Every app sign-in, and every "Connect a Nostr app" on the account page, is a connection: a key of
its own (the `bunker://<key>` in its URL), a secret, the app's name and the purposes it may
derive. The secret binds the connection to the first client key that presents it in `connect`;
later requests must come from that client. Revoking a connection (the app's logout, the account
page) ends both its NIP-46 bunker and its HTTP RPC token. Belfry's own apps keep their five newest
connections per member (`keep`), so silent sign-ins on new devices do not pile up.

NIP-46 requests are kind-24133 events p-tagged to a connection's key, NIP-44 v2 encrypted (or
NIP-04 for old clients; the answer mirrors the request). Methods: `connect`, `get_public_key`,
`sign_event`, `nip44_encrypt`, `nip44_decrypt`, `nip04_encrypt`, `nip04_decrypt`, `ping`,
`get_relays`, and Belfry's `derive_secret` for the connection's purposes. A suspended account
(pass closed) gets nothing signed, over the API and NIP-46 alike.

## API

Only the onboarding service calls it: the Compose network `signer` (internal, no internet: the
onboarding service, the bunker relay and the signer), bearer token `SIGNER_TOKEN`.

| Method and path | Body | Answer |
| --- | --- | --- |
| `GET /health` | | `{status, seed, accounts}` (no token) |
| `PUT /accounts/{vine_id}` | `{key_version?, own_pubkey?, expect_pubkey?}` | the account; creates it if missing (idempotent). `own_pubkey`: keyless (moves an existing account). `expect_pubkey`: 409 `mismatch` unless the seed gives that key (restore) |
| `GET /accounts/{vine_id}` | | `{vine_id, pubkey, custody, key_version, status}` |
| `POST /accounts/{vine_id}/status` | `{status: active\|suspended}` | |
| `POST /accounts/{vine_id}/sign` | `{event, connection?}` | `{event}` signed (a `pubkey` in the template must be the account's) |
| `POST /accounts/{vine_id}/encrypt`, `/decrypt` | `{scheme: nip44\|nip04, peer, text, connection?}` | `{result}` |
| `POST /accounts/{vine_id}/derive` | `{purpose, connection?}` | `{secret}` (64 hex); through a connection only its purposes |
| `POST /accounts/{vine_id}/export` | | `{nsec}` (the onboarding service asks for a fresh sign-in first) |
| `POST /accounts/{vine_id}/connections` | `{app, label?, derive?, keep?}` | `{id, bunker_url, bunker_pubkey, pubkey}`; the URL's secret is shown only here |
| `GET /accounts/{vine_id}/connections` | | the live connections (no secrets) |
| `POST /accounts/{vine_id}/connections/revoke` | `{id?}` or `{app?}` or `{}` (all) | `{revoked}` |
| `POST /import` | `{accounts: [{vine_id, pubkey, sealed}]}` | per account `derived`, `imported`, `exists` or `error` |

`connection` (the id behind an app's access token) must be live and the account's, else 403
`forbidden`. Errors are `{"error", "message"}`: 401 `unauthorized`, 404 `not_found`, 503
`seed_locked`, 409 `own_key` / `mismatch`, 403 `suspended` / `forbidden`, 422 `invalid`.

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
| `RUST_LOG` | `info` | log filter |

In Compose the container runs as the deploying user (`HOST_UID`, the owner of the custody seed
in `/run/belfry`), read-only, without capabilities, 128 MB.

`belfry-signer derive <vine_id> [<version>]` prints the public key the seed gives (a check
without the service; the smoke test uses it). `belfry-signer healthcheck` is the container's
health check.

## Recovery

- **Lost the signer's data** (volume): `python -m belfry_onboarding restore-keys` recreates every
  account from Odoo with the seed unlocked (same npubs); keys from before the seed come back only
  from a backup of `signer-data` with `signer_master_key`.
- **Lost the master key**: the sealed keys are unreadable; derived keys come back with
  `restore-keys` into a fresh `signer-data` (move the old volume away first); imported ones are
  lost. Back the master key up (it is in `infra/secrets/`, so in the backups).
- **Lost the custody seed**: nothing breaks until a new account is needed; the seed is kept as
  `custody-seed.age` under the stewards' YubiKeys (`infra/DEPLOY.md`, "Custody seed").

## Develop

```
cargo test        # 12 tests: key derivation vectors, sealing, store, API, bunker (NIP-44 and NIP-04)
cargo clippy --all-targets
```

Set `CARGO_TARGET_DIR` outside a small `/tmp`. The image: `make build-signer`
(`Dockerfile`, Rust 1.93 and Debian slim).

## Security

The signer is the most sensitive code Belfry runs: it holds every managed key. It is kept small
(no web pages, no sessions, no policy beyond the connection's purposes), on an internal network
without internet, behind a token only the onboarding service has. What it does not change:
whoever controls the onboarding service can sign as any managed member, and whoever has the
unlocked seed has every derived key. Only members moving to their own keys (a NIP-07 extension or
a phone signer) reduce that. Have it reviewed before it holds real participants' keys.
