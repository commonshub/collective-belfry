# Nostr: what Belfry uses and what it invents

Prefix `N` (not NIP numbers: those are in the Standard column). One line each: the kind or convention, what Belfry uses it for, and where the
contract is. Rule for everyone:

- **N-0** [all] A wire format (kind, tag, endpoint) is never invented by one platform: it comes
  from the lead or the shared SPEC; a platform that needs one asks the lead first. When one
  platform defined it first, the others copy it exactly and port its vectors. (owner, 2026-11-18)
- **N-00** [all] Prefer an existing NIP; when none fits, use an unassigned kind or a NIP-78 /
  NIP-32 convention under a `belfry` name, and write it in a contract before shipping.

## Relays and signing

| Id | Standard | Belfry's use | Contract |
|---|---|---|---|
| N-1 | NIP-01, kind 0 | profiles (published at activation); kind 9 chat messages in groups | |
| N-2 | NIP-05 | username@domain identities; `_smtp@` (mail bridge = belfry key), `_groups@` (groups key) in nostr.json | docs/mail.md |
| N-3 | NIP-11 | relay info carries the community's name | |
| N-4 | NIP-42 AUTH | both relays; community relay (rnostr, Belfry's build) whitelists active passes; "complete"/"absent" only count after AUTH | iOS SPEC 19.42 |
| N-5 | NIP-46 | the signer: sessions per device (client key bound at creation); kind 24133; also the remote signer for third-party apps (`nostrconnect://`) | docs/own-signer.md, docs/remote-apps.md |
| N-6 | NIP-98 (kind 27235) | auth for every API call; short-lived token exchange planned | |
| N-7 | NIP-86 | relay management (admins of every group) | |
| N-8 | NIP-29 groups | channels, groups, events' and proposals' chats on zooid (patched: guests, moderategroups, applicants): 9000 9001 9002 9005 9007 9008 9021 9022, 39000–39003, `h` tag | docs/membership.md, docs/archive.md |

## Messages and content

| Id | Standard | Belfry's use | Contract |
|---|---|---|---|
| N-10 | NIP-17 / NIP-59 / NIP-44 v2 | DMs: kind 14 text, 15 files, seals 13, gift wraps 1059; `["silent","1"]` on wraps only for oneself (never pushed); NIP-04 read only | docs/sealed.md §4 |
| N-11 | NIP-22 kind 1111 | threads in groups | docs/one-shape.md |
| N-12 | NIP-25 / NIP-09 | reactions (7, with `k` of the target), deletions (5); 9005 in groups | |
| N-13 | kind 1010 (Belfry) | message edits: only the author's latest counts; keeps imeta / topic tags | |
| N-14 | NIP-92 imeta + Blossom | files and pictures; `dim` for sizes; encrypted files AES-256-GCM with `ox`, `decryption-key`, `decryption-nonce` | vectors/dm-files.json, encrypted.md §23 |
| N-15 | NIP-24 `t` tags | topics; `t announcement` by a channel admin = announcement | docs/followers.md |
| N-16 | NIP-23 kind 30023 | proposals (`version`), website proposals (`path` tags) | docs/actions.md |
| N-17 | NIP-88 | polls / votes 1068 / 1018 per proposal version | |
| N-18 | NIP-52 | events (31923, `d` = 6 chars or slug), group `ev-<d>`; `["p", key, "", "invited"]`; 9 with `["invited","event"]` | docs/checkin.md |
| N-19 | NIP-99 kind 30402 | ticket types; `price`, `price_at`, `vat`, `requires` tags | docs/checkin.md |
| N-20 | NIP-40 | expiration (drop-box links) | |
| N-21 | kinds 2100–2103 | to-dos (tasklist): claim / assign / unassign, `due`, `description`, `q` = source message; charter = to-dos of group `charter` with `["each","1"]` | docs/charter.md, Android SPEC 19.40 |
| N-22 | nsite (kind 35128) | the community's website; proposals publish it | skills/belfry-homepage |
| N-23 | Nostr Mail kind 1301 | e-mail as RFC 2822 in gift wraps; 7679 delivery reports (no NIP) | docs/mail.md |

## Belfry's own tags

| Id | Tag | Use | Contract |
|---|---|---|---|
| N-30 | `["by", key]` | the person the belfry wrote an event for (cards, roles, decisions) | docs/cards.md |
| N-31 | `["joined","belfry"]` on a 9000 in `general` | the join line | docs/one-shape.md |
| N-32 | `["button", id, label, style, action]` | up to 4 buttons on a 9 / 1111 | docs/actions.md |
| N-33 | `["preview", url, title, description, site]`, `["preview","none"]`, imeta `purpose preview` | sender-built link previews | docs/link-previews-v2.md |
| N-34 | `["to"\|"cc", address, name]`, `["mail-draft","1"]`, `["draft", id]` | e-mail threads | docs/one-shape.md §4 |
| N-35 | `["archived","1"\|"0"]`, `["private"]`, `["vouches","N"]`, `prompt` on 9002 | group state; every 9002 keeps all settings | docs/archive.md |
| N-36 | `["guest", date]`, `["unsaved","1"]`, `["reading","1"]`, `["invited-by", key]`, `["agent","1"]`, `["card","1"]` on 9000s | guests, agents, cards | docs/guests.md |

## NIP-32 labels (kind 1985), `L` spaces

- **N-40** `belfry/pin` (pinned / unpinned), `belfry/inbox` (open / answered / closed, take),
  `belfry/proposal-status`, `belfry/event-status`, `belfry/guest` (saved, member),
  `belfry/vouch`, `belfry/ticket` (checked-in), `belfry/group-role` (steward),
  `belfry/action` (a press), `belfry/action-result` (ok / error). Labels the belfry trusts are
  signed by the groups key or checked against admin lists. A label space inside an encrypted
  message counts only for pin, inbox, event-status, proposal-status. → encrypted.md §26

## NIP-78 settings (kind 30078), `d` values

- **N-50** `belfry:channel-order` (order, categories, `pinned` for everyone; belfry admins),
  `belfry:pins` (my pins, NIP-44 to self), `belfry:read` (read state across devices, NIP-44 to
  self), `belfry:profile` (tags, place, headline, links: writers keep unknown keys),
  `belfry:inbox:<channel>` (one per channel, never a shared d: CH-25), `belfry:web:<channel>`
  (public page, `accepts`, `posts`), `seen:<channel>` (presence, every 6 h at most),
  `drop:<token>` (drop-box links), `belfry:device:reads` (device says it reads format 3).
- **N-51** Version-1 sealed only (still read): `belfry:sealed` (anchor),
  `belfry:device:<device pubkey>` (device names).

## Private kinds (Belfry ranges)

| Id | Kinds | What | State |
|---|---|---|---|
| N-60 | 8534–8538 | app access requests (old) | legacy |
| N-61 | 8550 / 8551 / 8552 | app approval: request / approve / withdraw | built |
| N-62 | 8560–8564 | v1 sealed: roster, hand-over, device request, "It was me" (8563), shared history | read only after format 3 |
| N-63 | 8570 | device list, signed by the belfry key, regular, counter `n` + `prev`, pubkeys only | encrypted.md §3 |
| N-64 | 8571 / 8572 / 8573 | key envelope / encrypted message / key pack; served only to the account in `p` (relay rule) | encrypted.md §5–§7, §18.1 |
| N-65 | 8574 / 8575 | ask for earlier messages / answer | encrypted.md §7 |
| N-66 | 20100 (groups, `h`) and 21059 ephemeral wrap (DMs) | typing indicators; never stored by zooid; rnostr replays up to 5 min | docs/typing.md |

- **N-70** [all] Kinds 8571–8573 are refused by the relay when dated more than 5 minutes from
  its clock; apps never backdate them.
