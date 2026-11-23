# Printed cards

Pre-printed A6 cards with a QR code (Xavier, 2026-11-21, for belfry.brussels; built 2026-11-23).
Someone pins a card on a board; the first person to scan it starts a group (or a topic) and
looks after it; everyone who scans it afterwards joins that group. Design: the canvas
"Belfry · Group cards" (boards `cards/*`).

Code: `services/onboarding/belfry_onboarding/cards.py` (the API), `cardsconsole.py` (the admins'
page and the print view), table `group_cards` in the belfry's own register. Tests:
`tests/test_cards.py`.

Needs the belfry's own register (`REGISTER=belfry`): with Odoo every endpoint answers
`409 {"error": "unsupported"}`.

## 1. The card

- The QR code holds `https://chat.<domain>/g/<id>`. The proxy serves the chat's page there with
  no redirect (as for `/login/<name>`); the chat reads the id from the path.
- `<id>`: 6 characters of `ABCDEFGHJKMNPQRSTUVWXYZ23456789` (capitals and digits without O, I,
  L, 0, 1). Lower case is accepted in an address and read as upper case. The id is only in the QR
  code: it is not printed as text. It is no secret once the card is claimed (it is then a way
  into a group, like any guest link).
- Two kinds: `group` (printed upright) starts a group, `topic` (printed on its side) starts a
  topic: a channel whose id is a hashtag the person chooses.
- No group exists until someone claims the card: unused cards leave nothing on the relay.

Register table `group_cards`: `id`, `kind` (`group` | `topic`), `batch` (its name), `state`
(`unclaimed` | `claimed` | `retired`), `group_id`, `invite` (the token of the card's guest link),
`title` (the name the group started with), `made_by` (contact), `made_at`, `claimed_by`
(contact), `claimed_key` (their public key), `claimed_at`. The table is added to an existing
register when the service starts.

## 2. API

All under `https://belfry.<domain>/api/v1` (the app's own paths are `/api/…`), JSON. Errors are
`{"error": "<code>", "message": "…", …}`; apps show their own words for a code, never
`message`. CORS as for the guest endpoints: the chat (`https://chat.<domain>`) and approved
apps; **not** the community's website on the apex nor its previews (`cards.CHAT_ONLY`, as for
co-host invitations: starting a group hands out a session).

"NIP-98" = `Authorization: Nostr <base64 kind 27235 event>` signed for the request's own URL,
method and body, as everywhere.

### 2.1 What a card is: `GET /cards/<id>`

No authentication. 30 a minute per client address, then `429 too_many`.

| Answer | When |
|---|---|
| `404 not_found` | no such card, and anything that is not an id (the same answer) |
| `{"kind", "state": "unclaimed"}` | nobody scanned it yet |
| `{"kind", "state": "claimed", "group", "title", "invite"}` | `group`: the group's id; `title`: its name as it is today; `invite`: the token of its guest link (§3) |
| `{"kind", "state": "archived", "title"}` | the group it started is archived (docs/archive.md): no `invite` |
| `{"kind", "state": "retired", "title"}` | the card was retired (§2.6): no `invite`; `title` is `""` for a card retired before anyone claimed it |

`kind` is `group` or `topic`. `title` falls back to the name the group started with when the
relay can't be read; "archived" is read from the relay too (a relay that does not answer reads
as not archived: joining refuses an archived group anyway).

### 2.2 A signed-in account claims: `POST /cards/<id>/claim`

NIP-98 by the account's key. Body `{"title", "about"?, "hashtag"?}`:

- `title`: 1–80 characters (spaces collapsed);
- `about`: up to 500 characters, may be empty;
- `hashtag`: topic cards only, and required there: 2–30 of `a–z`, `0–9` and `-`, not starting or
  ending with a dash, already lower case (the server does not clean it). Ignored on a group card.

Who may: a member, or a guest who has saved their account (a name and a proven e-mail address).

`200 {"group": "<id>"}`. The server, as the groups key:

1. makes the group: `9007` then `9002`, with the tags the chat's own "New group" writes
   (`["open"]`, `["public"]`, `name`, `about`). Its id is `pod-<slug of the title, 24 at
   most>-<4 random characters>`. A **topic** is a channel as the chat's "New channel" makes one
   (`9007` `[["h", id]]`, `9002` with `name` and `about`, no access tags) whose id is the hashtag;
2. puts the person in with the admin role: one `9000` with `["p", <key>, "admin"]`,
   `["role", "admin"]`, `["by", <key>]`, `["name", <their name>]`, and for a guest
   `["guest", "2099-12-31"]` (their pass of this group has no end, §4; the shape `/group-roles` and co-host links write);
3. for a group, posts in `general` exactly what the chat posts: kind 9,
   `⬡ New group: <title> · <about>` (no ` · ` without an about), tags
   `[["h", "general"], ["pod", <id>]]`. A topic is not announced: it is a channel in the list;
4. posts the first line in the group: kind 9, `<name> started this group from a printed card`
   (`… this topic …` for a topic), tags `["h", <id>]`, `["p", <key>]`, `["card", "1"]`. Never the
   card's id;
5. makes the card's guest link (§3) and marks the card claimed.

Steps 3 and 4 are best effort (a relay that refuses them does not undo the group). A `9002` or
`9000` that fails deletes the group again (`9008`) and answers `503 relay`.

| Error | |
|---|---|
| `401 unauthorized` | the signature |
| `403 no_pass` | the key is nobody's here |
| `403 agent` | an agent's key |
| `403 guest_not_saved` | a guest who only reads, or who gave a name and no e-mail address |
| `403 unverified` | a buyer whose address is not proven yet |
| `404 not_found` | no such card |
| `409 claimed` + `group`, `invite` | someone was first: join theirs |
| `409 retired` | the card was retired |
| `409 hashtag_taken` | topic cards: see §2.4 |
| `422 invalid` + `field` (`title` \| `about` \| `hashtag`) | the form |
| `503 relay`, `503 signer` | try again |

### 2.3 A visitor claims: e-mail, code, then the group

Both requests are NIP-98 signed by a key the browser just made (the same kind of key
`/apply/*` and `/login/*` take). That key becomes the session's key.

`POST /cards/<id>/claim-start` `{"email"}` → `202 {"sent": true}`: mails a 6-digit code (10
minutes, 5 tries, only the newest works; purpose `card`, tied to this card and this key) and
stores nothing else. The same answer whether the address has an account or not. Errors:
`404 not_found`, `409 claimed` + `group`, `invite`, `409 retired`, `422 invalid` +
`field: "email"`, `429 too_many` (the limits of `/apply/start` and `/login/start`: 20 code mails
an hour per client address, shared with them, and the register's per address and per key).

`POST /cards/<id>/claim-verify` `{"email", "code", "name", "title", "about"?, "hashtag"?,
"client"?}`: `name` is the **person's** name (1–100); `title`, `about`, `hashtag` as in §2.2;
`client` as elsewhere (`belfry-web` | `belfry-ios` | `belfry-android`, names the session).

The form, the card's state and (for a topic) the hashtag are checked **before** the code is
spent. Then, on the right code, under the card's lock:

- the address has an account here → a session on that account bound to the signing key (the
  same proof and the same code as the e-mail sign-in; docs/guests.md "An address that has an
  account"). `name` is ignored: the account keeps its name;
- else → a new guest account with that name and address (saved from the start), whose first
  session is the signing key;
- then the claim of §2.2 by that account.

`200 {"signed_in": true, "bunker_url", "pubkey", "npub", "nip05"?, "group"}`.

| Error | |
|---|---|
| `400 wrong_code` + `tries_left` | (status 400 here; `/login/verify` and `/guest/save/verify` use 422) |
| `410 expired` | no live code for this address, key and card |
| `409 claimed` + `group`, `invite` | someone was first. Seen before the code: the code is not spent. Seen after (a race): the code is spent, nobody is signed in, no account is made; join through `invite` |
| `409 retired` | |
| `409 hashtag_taken` | before the code is spent |
| `409 own_key` | the address's account signs with its own key: the belfry has no session to hand out |
| `409 agent` | the address is an agent's |
| `409 key_taken` | the signing key is linked to another account: make a new key, ask for a new code |
| `422 invalid` + `field` (`name` \| `email` \| `title` \| `about` \| `hashtag`) | |
| `503 signer`, `503 relay` | the code is spent: ask for a new one |

One lock per card; the first proven claim wins. The loser's group is never made, and a visitor
who lost gets no account. When the relay refuses after a new guest was made, their pass is
closed and their signer account suspended; the card stays unclaimed.

### 2.4 A topic's hashtag: `GET /cards/hashtag/<name>`

No authentication, 30 a minute per client address. `{"state": "free" | "taken" | "invalid"}`.

- `invalid`: not 2–30 of `a–z`, `0–9`, `-` (or a dash at either end, capitals, anything else);
- `taken`: a channel or group has that id (a deleted one too), the name reaches a channel by
  mail (a chosen alias, the plain slug of its name), it is someone's username or page, or it is
  a name nobody gets: `general`, `charter`, `website`, anything starting with `pod-`, `ev-`,
  `ws-`, `prop-`, the mail system's names (`postmaster`, `abuse`, `admin`, …, and `hello`,
  `info`, `support`, `help`, which only a belfry admin gives to a channel), and the names
  accounts start with (`guest-12`, `member-7`, digits);
- a relay or signer that does not answer is `503` (`relay` / `signer`), never "free": the
  lookups behind a channel's mail address are asked one by one here, because
  `mail.channel_id` itself answers "no channel" when its slug lookup fails (right for mail).
  The list of channels behind the slug lookup is cached for a minute.

The claim checks again (`409 hashtag_taken`).

### 2.5 The group's card: `GET /groups/<group>/card`

NIP-98 by one of the group's stewards (its admins: the relay's list, its creator, by role) or a
belfry admin. `200 {"id", "kind", "started_by": {"name", "pubkey"}, "joined_by_scan"}`:
`joined_by_scan` = how many times the card's link was used (people in guest mode who only opened
it count, as for any guest link). `403 not_allowed` for anyone else; `404 not_found` when the
group has no card in use (none, retired, or reset).

### 2.6 Retiring a card: `POST /cards/<id>/retire`

NIP-98 by one of its group's stewards or a belfry admin (a card nobody claimed: a belfry admin).
`200 {"state": "retired"}`; again on a retired card: the same. The card's link is revoked:
joining with it answers `410`. The group and the people in it stay. `403 not_allowed`,
`404 not_found`.

Archiving the group (`POST /group-meta` `archived`) needs nothing here: `GET /cards/<id>`
answers `archived` and joining answers `409 archived`; unarchived, the card works again.

## 3. The card's guest link

A claimed card stands for one guest link of its group (`invite` in §2.1): an ordinary channel
invite of the register (docs/guests.md), used through the existing endpoints
(`GET /channel-invites/<token>`, `POST /channel-invites/<token>/join`, `/guest/save`,
`/guest/save/verify`), with two differences: it has **no end date** and **no number of people**
(`guest_unlimited` on the invite; `expires_at` empty). Its inviter is the person who started the
group. A guest's own access keeps the normal rule: 30 days from the day they joined.

Because the link never fills up, it makes at most **100 new guest accounts per card in any 24
hours** (`register.CARD_NEW_GUESTS_PER_DAY`; counted from the passes the link made, so a restart
keeps the count). Over that, `POST /channel-invites/<token>/join` by a key nobody knows answers
what a used-up link answers: `410 {"error": "closed"}` ("This invite is used."), and the log says
so once an hour per card. Never counted and never refused: someone joining with an account they
have (a member, a guest of something else), and the same device asking again. `GET
/channel-invites/<token>` keeps saying `open`. Agents that take the link with their own key
(`/guest-invites/<token>/accept`) are not counted.

## 4. A guest as steward

New rule (approved 2026-11-23): **a guest may be the steward of the group they started from a
card, and of that group only.** A visitor who claims a card becomes a guest of the new group
(a guest pass, 30 days) and is put in with the admin role by the groups key. From then on every
existing check that asks "is this key an admin of this group" (`/group-meta`, `/group-roles`,
`/group-requests/decide`, `/groups/<id>/card`, `/cards/<id>/retire`) lets them in for that
group, and for no other. They get nothing belfry-wide: no admin rights, not the community
relay, and guests still make no guest links (`403 guest` on `/channel-invites`): the card is
their link.

Nothing was relaxed in `/group-roles` for this: the server writes the role itself at the claim,
and only for an account with a proven address. (`/group-roles` already let a group's admin give
the admin role to a guest with a saved address; it still refuses one without, `guest_not_saved`,
and agents.)

They keep their group: the guest pass of **that group** has no end (`date_end` 2099-12-31,
`guest_card` = the card's id on the pass) for as long as the card is claimed by them. Retiring
or resetting the card puts the usual end back, 30 days from that moment; then it expires like
any guest pass (a 9001 takes them out). Their other guest passes are untouched.

## 5. The console: `belfry.<domain>/cards`

For the belfry's admins ("Printed cards" in the menu, under Community).

- **Make cards**: a kind, a name for the batch, 1–40 cards.
- **Batches**: each with its cards, their state and the *name* of the group each one started.
- **Reset** on a used card (`POST /cards/<id>/reset`, csrf, a confirmation): unclaimed again,
  its link revoked, the group it started left exactly as it is (it just has no card any more).
- **Print** (`/cards/print/<batch>`): sheets of four A6 cards on A4. Group cards are 105 × 148 mm
  on an upright sheet, topic cards 148 × 105 mm on a sheet on its side (`@page` sets the size
  and no margins; print at 100 %). Each card: the community's name and logo, one fixed line
  ("Scan to start a group here" / "Scan to start a topic here"), the QR code (error correction
  Q, quiet zone 4) and `chat.<domain>/g/`. Black on white.

## 6. The proxy

`chat.<domain>/g/<id>` is answered by the chat's page itself (the page rule that serves the
app's routes), not cached. No universal-link / App Link entry: the phone apps do not know cards
yet. `belfry.<domain>/cards/*` is in the console's form limit; the API has the limit of
`/api/*` (60 a minute per address) on top of §2's own.

## 7. Not in v1

- A weekly digest of a topic by e-mail.
- Posting from a card without an e-mail address.
- A PDF export of a batch (the browser's Print makes one).
- A preview of the group's recent messages before joining.
- Deleting unused cards, moving a card to another group.
- The phone apps (no `/g/*` in apple-app-site-association or assetlinks).
