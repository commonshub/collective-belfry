# Server and deploys

Prefix `SRV`. The belfry server (`commonshub/collective-belfry`, branches `belfry-sh` and
`belfry-setup`) and the four boxes. Runbook: docs/runbook.md; signer: docs/own-signer.md.

## What a belfry is

- **SRV-1** [server] A belfry runs on infrastructure the community runs itself: login by e-mail
  code, an identity per member held by the belfry's signer, a members-only relay, a groups relay,
  file storage (Blossom), mail in and out, the chat and the console. Odoo is an optional add-on
  (`REGISTER=belfry` keeps the register in SQLite). (owner, 2026-11-08)
- **SRV-2** [server] Belfry's own signer is the default for new installs (`SIGNER=belfry`);
  Keycast and Authentik are gone from all four boxes; their data is kept until cleanup (not
  before 2026-11-29, after a fresh backup and a second clean verify), then their code is removed.
  (owner, 2026-11-23)
- **SRV-3** [server] No community's look is in the code: one base theme; any other look is a
  theme folder. (lead, 2026-11-09)
- **SRV-4** [server] An open first-run setup is first come, first served. → onboarding.md ONB-60

## Deploying

- **SRV-10** [server] Every release goes to ALL FOUR boxes: opensourcevillage.org, belfry.sh,
  enspiral.eu, belfry.brussels. "Also update belfry.brussels, let's keep them on all in sync for
  now." (owner, 2026-11-23) → TEAM.md "Deploying". Changed on 2026-11-23: was three boxes, before
  that OSV only. Security fixes always go to all four.
- **SRV-11** [server] Deploy to ONE box first and check its health (status page, chat page serves
  the new build, onboarding answers), then the others; a loop never continues past a failed
  apply. (lead, after the 2026-11-23 outage)
- **SRV-12** [server] A new setting must accept its value as TEXT from the environment, as the
  compose file passes it ("1", not 1); instantiate the settings from the real env before
  deploying. (lead, 2026-11-23: a typed setting stopped onboarding on all four boxes)
- **SRV-13** [server] Pushes to `belfry-sh` / `belfry-setup` are fast-forward only, built on top
  of origin (others push too); history is never rewritten. GitHub `main` is synced from ngit:
  merges there need the owner's go.
- **SRV-14** [server] On a box: update = fetch + `reset --hard origin/belfry-sh` as user belfry,
  then `belfry apply`. Applies queue behind a lock; never start a second one; a box can take
  many minutes and is not hung.
- **SRV-15** [server] Never touch `infra/apps/chat` (the source of another image); the shipped chat
  build lives in `infra/shipped-apps/chat` with its commit in `.build`.
- **SRV-16** [server] Caddyfile changes are tried in a local `caddy:2` container and validated on
  a box (`caddy validate`) before pushing; a Caddyfile-only change re-tags the current proxy
  image instead of rebuilding it.
- **SRV-17** [server] A change that restarts the relay or changes its image follows the per-box
  order of the runbook (the relay image comes from `RELAY_IMAGE` in each box's `.env`; set the
  new image BEFORE an apply renders a template it alone understands).
- **SRV-18** [server] After a deploy, tell the platform agents and the owner what went out and
  what was not tried by a real person.

## Disk, memory, images

- **SRV-20** [server] No image build on a box or on the team machine with less than 2.5 GB of free
  disk; prune old tags first, but keep the tree-tagged images of the current code (pruning them
  forces a long signer / proxy rebuild) and the rollback images until cleanup.
- **SRV-21** [server] Heavy builds (Rust signer, relay) are made once on one box and copied to
  the others (`docker save | load`), never rebuilt on each.
- **SRV-22** [server] The team machine is low on memory: lean builds, one test file per process
  when memory is short; never Gradle or Xcode-sized builds there.

## Backups and data

- **SRV-30** [server] Every box has a nightly encrypted backup (register, signer volume, relay
  data); each first backup is decrypt-checked. The decryption identities stay off the boxes.
  Backups are still on the box itself: moving them off-box is open (the owner gives the
  destination).
- **SRV-31** [server] Migrations never delete the old data: they keep it next to the new one
  with a written rollback (relay export, old signer data, Odoo dump).
- **SRV-32** [server] A promise made in the product (e.g. "erase my data after the event") is a
  server requirement with a deadline. → events.md EV-50

## API rules

- **SRV-40** [server] Every endpoint answers JSON errors with a fixed `error` code (apps show their
  own words), and CORS, also on 5xx; a relay that can't be read is `503 relay` with Retry-After,
  never an empty or wrong 200, and never cached.
- **SRV-41** [server] Endpoints that create things are idempotent (per signing key, or with a
  `client_id`); a repeat answers the first result.
- **SRV-42** [server] NIP-98 for every signed call; endpoints that hand out sessions answer only
  the chat and approved apps, never the community's website or its previews.
- **SRV-43** [server] Rate limits on anything that sends mail or looks up short ids (codes: 20 mails
  an hour per address of the network; cards 30 a minute).
- **SRV-44** [server] Mail never delivered in clear to an encrypted inbox: answer 451 (the sender
  retries) when the device list can't be read. Every refused mail is logged with the reason,
  without the address.
- **SRV-45** [server] A mail health check runs from the host every 5 minutes on IPv4 and IPv6, and
  `belfry mailcheck` at the end of every apply; admins are alerted after two failed rounds.
- **SRV-46** [server] The belfry fetches outside content only with the SSRF guard. → privacy.md
  PRIV-21

## The signer and sessions

- **SRV-50** [server] A session's client key is bound at creation; one client key = one identity
  on every path; revoking a session frees nothing silently. → privacy.md PRIV-34
- **SRV-51** [server] A device's key goes on the relay whitelist when the server binds it and
  comes off at revoke. (owner, 2026-11-21)
- **SRV-52** [server] The bunker relay allows a page's first burst of signer messages (100/s per
  connection). (lead, 2026-11-23, after the blinking join screen)
- **SRV-53** [server] Device lists (kind 8570) are written by the belfry at every session bind and
  revoke; they are never pruned. → encryption.md
