# Groups relay: zooid

`groups-relay.<domain>` is [zooid](https://gitea.coracle.social/coracle/zooid) (MIT, Go, a
[Khatru](https://gitworkshop.dev/fiatjaf.com/nostrlib) relay with NIP-29 groups), the relay
behind the groups app (Flotilla, `infra/apps/groups`). The community relay (rnostr) has no NIP-29,
which is why Flotilla was ruled out for the chat (docs/NEXT_STEPS.md §3o); this relay sits next to
it rather than replacing it.

| Piece | Where |
| --- | --- |
| Image | `Dockerfile`: clones zooid at `ZOOID_COMMIT` (0.3.0), applies `patches/`, builds the binary; Alpine runtime with `entrypoint.sh`, which renders the one relay config (`/app/config/groups.toml`, on tmpfs) from the environment and the secret files and starts zooid. |
| Storage | Postgres, role and database `zooid` (`postgres/init.sh` on a fresh volume, `bootstrap.sh` on an older one; password `secrets/zooid_db_password`). Backed up with the rest of Postgres. |
| Keys | `secrets/groups_relay_nsec`: the relay's own key (signs the group state events 39000–39002). `secrets/groups_api_nsec`: the onboarding service's key; its pubkey `GROUPS_API_PUBKEY` (gen-secrets) is the relay's owner and the only key on zooid's `API_WHITELIST`. |
| Compose | service `groups-relay` (`--profile app`), host port 3190 locally, none in production; Caddy `groups-relay.<domain>` → `groups-relay:3334`. `GROUPS_RELAY_URL` (`ws://localhost:3190` locally, `wss://groups-relay.<domain>` from `prod-env.sh`) is the Host zooid serves the relay on. |

**Who gets in.** `policy.public_join = false` and `public_read = false`: reading and writing need
NIP-42 AUTH as a member. Members are the pubkeys of active passes, never edited by hand: the
onboarding service (`groups_members.py`) puts them on zooid's member API whenever it syncs the
community relay's whitelist (activation, revoke, expiry, start, `sync-relay`, `reconcile`), and
removes those whose pass closed. A removal neither bans the key nor deletes what it wrote. The
API answers only on the internal host name `groups-relay:3334` (`API_HOST`), which the proxy never
forwards (Caddy passes the public Host on), and only to NIP-98 requests signed by the groups API
key. The same sync copies the members' signed kind-0 profiles from the community relay through
zooid's import API (the groups key is on the community relay's whitelist for that), so group
apps show names and pictures.

**Groups.** Any member may open a group, and its creator administers it: Belfry's patch
`0001-…` adds `[groups] member_create = true` (stock zooid leaves groups to the relay owner). With
it, a relay member may send kind 9007; the author of a group's create event may send that group's
moderation events (9000–9010), is listed in its kind 39001 with those kinds as roles (Flotilla
reads them as the admin's rights) and joins it as a member. Stewards are not yet admins of every
group; the relay owner (the groups API key) is.

**Off.** Blossom (files go to Belfry's Blossom), push (NIP-9a; no push server is run), LiveKit
calls, paid membership, invite claims.

**Changing the patch.** Clone zooid at `ZOOID_COMMIT`, `git am patches/*.patch`, change, commit,
`git format-patch <commit>..HEAD -o patches/`. Its tests need Postgres:
`TEST_DATABASE_URL=postgres://… go test ./zooid/` (the patch adds
`TestGroupStore_CheckWrite_MemberCreate`).
