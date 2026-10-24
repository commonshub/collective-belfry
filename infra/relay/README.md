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
