# Group cards

Pre-printed QR codes for groups that don't exist yet (Xavier, 2026-11-21, for belfry.brussels):
someone writes a topic on an A6 card and pins it on a board; the first person to scan it starts
the group and becomes its steward; everyone after joins it. Design: canvas section "Group cards"
(boards cards/*). Status: design, not built.

## 1. The card

- An admin makes cards in the console (belfry.<domain>/cards, 1–200 at a time) and prints them
  (A6, four per A4).
- A card = `https://chat.<domain>/g/<token>`: 22 random characters `[A-Za-z0-9_-]` (128 bits),
  plus a number for people ("Card 17"). The token is the only secret. It is stored hashed
  (SHA-256) in the register, and logs only show the card number.
- Register table `group_cards`: number, token hash, state (`unclaimed` | `claimed` | `retired`),
  group id, made_by, made_at, claimed_by (contact), claimed_at.
- No group exists until a claim: unused cards leave nothing on the relay.

## 2. API (belfry.<domain>/api/v1)

| | Who | What |
|---|---|---|
| `POST /cards` `{count}` | belfry admins | makes cards → `{cards: [{number, url}]}` (the only time the URLs are shown; the print page renders them) |
| `GET /cards` | belfry admins | the list: number, state, group name, claimed by (name), made at |
| `POST /cards/<number>/reset` | belfry admins | claimed → unclaimed (the group stays, without the card) |
| `GET /cards/<token>` | anyone | `{state, group?: {name, about, steward: {name}, people, open, recent?: [...]}, me?: {member_of_group}}`: `recent` only for a public group; 404 `unknown` for a wrong token, the same for every wrong token |
| `POST /cards/<token>/claim` `{name, about}` | NIP-98 by a signed-in key (member or saved guest) | creates the group, makes the caller its steward → `{group}`; 409 `claimed` (+ the group's preview) when someone was first |
| `POST /cards/<token>/start` `{email, name, title, about}` | NIP-98 by a new key the browser made | visitor claim: mails a code + confirm link; nothing is created before the proof |
| `POST /cards/<token>/join` | NIP-98 by a signed-in key | joins the group (9000 by the groups key, as guest links do) |
| `POST /cards/<token>/join-start` `{email, name}` | NIP-98 by a new key | visitor join: mails a code + confirm link |
| `POST /cards/<token>/verify` `{email, code}` | the same key | proof by code → `{signed_in, bunker_url, …, group}` (the claim or join happens now) |
| `GET /cards/confirm/<proof>` | anyone with the mail | proof by link: marks the pending claim or join proven, shows a fixed page "Confirmed. Go back to the page where you scanned the card." It signs nobody in where it is opened. |
| `POST /cards/<token>/status` | the pending key | `pending` \| `done {signed_in, bunker_url, …, group}`: the scanning tab polls this (every 3 s, for 30 min) |
| `POST /cards/<token>/retire` | the group's stewards, belfry admins | the card stops starting or joining: `GET` answers `retired` + the group's name |

A visitor who proves an address that already has an account gets that account (guests.md's rule:
same proof as the e-mail sign-in, a session bound to the new key).

## 3. Rules

- The first proven claim wins: `claim` and `verify` take the card under one lock. A second
  claimer gets 409 `claimed` with the preview and joins instead (one tap, already proven).
- The group: id `card-<number>-<4 random>`, created by the groups key (9007), name/about by
  9002, open (anyone with the card joins), in a "Topics" category of the channel list
  (categories in belfry:channel-order; the server adds the category when it's missing).
  Members see it like any open group.
- The claimer becomes its steward (`group-roles` admin). A visitor becomes a guest of that
  group and its steward. Steward of this group only: no belfry rights, no other groups. This is
  new; until now a guest could never be a steward.
- Joiners: members join as members; visitors become guests of that group (guest mode's rules:
  name + proven e-mail; no time limit while the card is live).
- Title 1–80 characters, about ≤ 500, one name per account as everywhere.
- Limits: 20 code mails / hour / IP; 10 claims / hour / IP; a card with 3 wrong codes on one
  key asks for a new code.
- Archiving the group retires its card. Deleting the group resets it to unclaimed (admins only).

## 4. Open

- Do cards expire if never claimed (proposal: no)?
- Should a public group show recent messages in the preview (proposal: yes, last 3, names only)?
