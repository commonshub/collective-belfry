# Git server: ngit-grasp

`git.<domain>` is [ngit-grasp](https://gitworkshop.dev/danconwaydev.com/ngit-grasp) (MIT, Rust),
the reference [GRASP](https://gitworkshop.dev/danconwaydev.com/grasp) server: a Nostr relay and a
Git Smart HTTP server in one binary. Repositories, pull requests, patches, issues and comments are
NIP-34 events; pushes are authorised by the maintainers' signed state events, not by accounts. It
is where agents of pass holders (and the holders themselves) work on code together and talk about
it (`skills/open-source-village/SKILL.md`, section 6, "Git"). It replaces the archived ngit-relay.

| Piece | Where |
| --- | --- |
| Image | `Dockerfile`: clones ngit-grasp at `NGIT_GRASP_COMMIT` (v3.1.0) from relay.ngit.dev, applies `patches/`, builds the release binary; Debian slim runtime as user 10001, upstream's entrypoint. |
| Storage | volume `git-data` (`/data`): the relay's LMDB database and the bare repositories. In the nightly backup (paused while copied). |
| Key | `secrets/git_relay_nsec` (gen-secrets, hex): the relay owner (NIP-11 `pubkey`, `_@git.<domain>`), read as the `relay_owner_nsec` credential (`CREDENTIALS_DIRECTORY=/run/secrets`). |
| Members | volume `git-members`: `members.txt`, written by the onboarding service (`git_members.py`), read-only here. |
| Compose | service `git` (`--profile app`), host port 3170 locally, none in production; Caddy `git.<domain>` → `git:7334` (WebSocket and HTTP on one host). `GIT_DOMAIN` / `GIT_PUBLIC_URL` (`localhost:3170` locally, `git.<domain>` from `prod-env.sh`). |

**Who gets in.** Private mode (GRASP-08, `NGIT_PRIVATE_MODE=true`): every relay session must
answer NIP-42 AUTH and every git request carry a repository-scoped NIP-98 credential from a
member, otherwise an empty 401; only NIP-11, the landing page and the NIP-05 of the relay owner
are public. Members are the pubkeys of active passes, never edited by hand: the onboarding
service rewrites `members.txt` (atomic rename) whenever it syncs the community relay's whitelist
(activation, revoke, expiry, start, `sync-relay`, `reconcile`). Upstream reads its members only
from the environment at start; Belfry's patch `0001-…` adds `NGIT_PRIVATE_MEMBERS_FILE`, re-read
on the five-second membership pass, so a new pass gets in within seconds and a revoked one has
its live sessions closed. A file with a malformed line is ignored (previous members kept). Within
the members, GRASP-01 decides writes: only maintainers listed in an announcement push its
branches; anyone may push `pr/…` refs for a pull request event they signed.

**Landing page.** Behind the proxy, a browser asking for a page on `git.<domain>` (the root or a
repository's address) gets Belfry's page from the onboarding service (`content/git.md`, branded
like the other pages: what the server is, that it is for pass holders, how to start with ngit,
the agent guide) instead of ngit-grasp's own pages, which point to gitworkshop.dev. NIP-11,
WebSocket and git requests still reach ngit-grasp. Locally (port 3170, no proxy) ngit-grasp's page
shows.

**Signing.** Participants' keys sign through Keycast like for the other apps; policy
`belfry-event` allows any kind (NIP-34 kinds, 22242 for AUTH, 27235 for NIP-98). Agents use ngit
3 with a bunker URL (`ngit account login --bunker-url …`).

**Off.** GRASP-06 (unauthenticated PR pushes, incompatible with private mode), GRASP-03 mailbox
discovery on public relays (`NGIT_SYNC_PLUS_ENABLED=false`), archive mode, Blossom retention. A
repository whose announcement also names public relays or other servers syncs with them like any
GRASP server would (that is the maintainer's choice; the skill says to name only `git.<domain>`
and use `--private`).

**Changing the patch.** Clone ngit-grasp, check out `NGIT_GRASP_COMMIT`, `git am patches/*.patch`,
change, commit, `git format-patch <commit>..HEAD -o patches/`. Tests:
`CARGO_TARGET_DIR=~/.cache/ngit-grasp-target cargo test --lib` (the patch adds
`members_file_reads_npubs_and_hex_and_fails_on_garbage`).
