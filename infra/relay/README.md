# Community relay (rnostr)

The relay the community apps talk to. Participants only: reading and writing need a NIP-42 AUTH as a pubkey on the whitelist, and the whitelist is derived from Odoo (`belfry.pass` in state `active`), never edited by hand. The Keycast NIP-46 relay (`keycast-relay`) is a different service and stays open.

| | |
| --- | --- |
| Image | `rnostr/rnostr:latest` (tested with 0.4.9), LMDB storage, NIP-42 auth extension, config hot reload |
| Host port | 3090 (`ws://localhost:3090`); inside Compose `ws://relay:8080` |
| Config | `rnostr.toml` here is the **template**. `relay-init` copies it into the `community-relay-config` volume on first start; afterwards the onboarding service rewrites `/relay-config/rnostr.toml` from the template whenever the set of active passes changes. |
| Data | volume `community-relay-data` (`/rnostr/data`) |

## Whitelist sync

`services/onboarding` (`belfry_onboarding/relay_whitelist.py`) renders the template: every line that ends in `# belfry:whitelist` gets `pubkey_whitelist = ["<hex>", ...]` with the `keycast_pubkey` of every active pass plus `RELAY_EXTRA_PUBKEYS` (comma separated, for Belfry's own announcement or steward keys). It runs

- at activation, with the new pubkey included before the kind-0 profile is published,
- after a revoke, expiry or reactivation,
- on `python -m belfry_onboarding sync-relay` and `POST /admin/sync-relay?token=` (cron or after editing the template),
- at service start.

rnostr runs with `--watch` and only reacts to data-modification events on the config path (not to a rename), so the file is rewritten in place. A half-written file fails to parse, rnostr keeps the previous config and reloads again on the final write. The auth extension re-reads its section on every reload (`Extensions::call_setting`).

An empty whitelist closes the relay to everyone, which is the intended default before the first pass is active.

## Checks

```
docker compose -f infra/docker-compose.yml exec relay cat /rnostr/config/rnostr.toml | grep whitelist
docker compose -f infra/docker-compose.yml logs relay | grep -i reload
# events stored for a pubkey (reads the LMDB directly; works while the relay runs)
docker compose -f infra/docker-compose.yml exec relay rnostr export /rnostr/data/events -f '{"authors":["<hex>"]}'
```

`infra/scripts/smoke.sh` checks that the pubkey of the freshly activated pass is in the rendered config and that the participant's kind-0 profile was stored.

## Belfry's own image: private kinds (`build/`)

The encrypted conversations (`docs/encrypted.md`, kinds 8571 keys, 8572 messages, 8573 key packs)
need a relay rule the official image does not have (§18.1): such an event reaches only a connection
authenticated as one of its `p` pubkeys. `build/` makes that image:

- `Dockerfile` clones rnostr at a pinned commit (`ARG RNOSTR_COMMIT`, main 2f21979 of 2026-11-23:
  the first with the `outgoing` extension hook, PR #51, which sees both stored results and live
  broadcasts), applies `patches/*.patch` in order, copies the committed `Cargo.lock` and builds
  with `--locked --no-default-features` (extensions: metrics, rate_limiter, count; no NIP-50
  search, no NIP-77 negentropy, neither of which this relay uses). Same layout as the official
  image: user `rnostr`, `/rnostr/config`, `/rnostr/data`, `rnostr relay --watch -c ./config/rnostr.toml`.
- `patches/0001-private-kinds.patch`: the extension `extensions/src/private_kinds.rs`, registered
  right after `Auth` (it reads the authenticated pubkey Auth keeps in the session), with its tests.
- `patches/0002-optional-search-negentropy.patch`: makes search and negentropy features of the
  binary so it builds without them.

The rule, on when the `[private_kinds]` section is present (`kinds`, default 8571–8573; `max_skew`,
default 300 s):

1. An EVENT message of these kinds, stored or live, goes only to a connection AUTHed as a pubkey
   in one of the event's `p` tags. No `p` tag, or no AUTH: nobody.
2. A REQ with a filter whose `kinds` are all private and whose `#p` is not exactly the AUTHed
   pubkey is answered `CLOSED` (`restricted:`, or `auth-required:` without AUTH). Filters that
   can also match other kinds (`{}`, `ids`, `kinds: [1, 8572]`) are served with the private
   events left out. COUNT and NEG-OPEN that could match a private kind need the same own form.
3. Writing one is refused (`OK false "invalid: …"`) when `created_at` is more than `max_skew`
   seconds from the relay's clock, either way.

Build (on a box with disk; never on a small dev machine), tests first:

```
docker build --target test infra/relay/build                       # unit + websocket tests
docker build -t belfry-relay:private-2f21979-1 infra/relay/build
```

To move to a newer rnostr: change `RNOSTR_COMMIT`, check the patches still apply, regenerate
`Cargo.lock` (`cargo generate-lockfile` in the patched tree) and bump the tag's last number.

### Moving a belfry from `rnostr/rnostr:latest` (v0.4.9) to this image

Checked on a copy of belfry.brussels' relay data on 2026-11-23:

- **The database must be exported and imported once.** rnostr main bumped its LMDB schema
  (`DB_VERSION` 3 → 4, upstream 43beebd, addressable events are now keyed on the first `d` tag
  anywhere instead of only `tags[0]`). The new image refuses the old database at start (exit 1,
  "VersionMismatch") and prints the steps. With the relay stopped:

  ```
  rnostr export data/events > events.jsonl      # the old or the new image can export it
  mv data/events data/old_events
  rnostr import data/events events.jsonl        # with the new image
  ```

  On the Brussels copy: 14 events out, 14 in, same ids, relay up.
- Unchanged `rnostr.toml` files load (the stricter config checking of upstream fc0153f accepts
  ours); the only log line is a warning that the `[search]` section has no extension (this build
  has no search). The official v0.4.9 image starts with the `[private_kinds]` section and ignores it.
- Refusal prefixes changed (upstream af30694): a connection AUTHed as a key **not** on the
  whitelist now gets `restricted: pubkey not in whitelist` (v0.4.9 said `auth-required: …`);
  unauthenticated ones still get `auth-required: NIP-42 auth required`. Clients that retry on
  `auth-required` stop retrying for a non-member.
- Also new upstream: a whitelist reload closes open subscriptions of keys taken off it
  (fe154d6); NIP-11 reports `restricted_writes`; in-place whitelist rewrites still reload
  (checked: a key added by rewriting the file could read within seconds).
