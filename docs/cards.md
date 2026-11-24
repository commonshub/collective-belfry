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

- `title`: 1–80 characters (spaces collapsed). **Optional on a topic card** (the new form asks
  for the hashtag and a description only): without one the topic's name is its hashtag and its
  description is `about` alone; with one (older chats) the description is `<title> · <about>`;
- `about`: up to 500 characters, may be empty;
- `hashtag`: topic cards only, and required there: 2–30 of `a–z`, `0–9` and `-`, not starting or
  ending with a dash, already lower case (the server does not clean it). Ignored on a group card.

Who may: a member, or a guest who has saved their account (a name and a proven e-mail address).

`200 {"group": "<id>"}`. The server, as the groups key:

1. makes the group: `9007` then `9002`, with the tags the chat's own "New group" writes
   (`["open"]`, `["public"]`, `name`, `about`). Its id is `pod-<slug of the title, 24 at
   most>-<4 random characters>`. A **topic** is a channel as the chat's "New channel" makes one
   (`9007` `[["h", id]]`, `9002` with `name` and `about`, no access tags) whose id and name are
   the hashtag. A topic is announced in `general` as `# New topic: #<hashtag> · <about>` (with an
   older chat's title: `# New topic: #<hashtag> · <title> · <about>`), with its chat link;
2. puts the person in with the admin role: one `9000` with `["p", <key>, "admin"]`,
   `["role", "admin"]`, `["by", <key>]`, `["name", <their name>]`, and for a guest
   `["guest", "2099-12-31"]` (their pass of this group has no end, §4; the shape `/group-roles` and co-host links write);
3. for a group, posts in `general` exactly what the chat posts: kind 9,
   `⬡ New group: <title> · <about>` (no ` · ` without an about), tags
   `[["h", "general"], ["pod", <id>], ["by", <key>]]`. A topic is not announced: it is a channel in the list;
4. posts the first line in the group: kind 9, `<name> started this group from a printed card`
   (`… this topic …` for a topic), tags `["h", <id>]`, `["p", <key>]`, `["card", "1"]`. Never the
   card's id;
5. makes the card's guest link (§3) and marks the card claimed.

**Who created it.** The relay only takes these events from the groups key, so the belfry writes
them for the person, and each says whose it is: `["by", <the claimer's public key>]` on the
`9007`, the `9002`, the "⬡ New group" post in `general` and the first line in the group (which
keeps its `["p", <key>]` and `["card", "1"]`), as on the `9000`s the belfry writes for someone
(`/group-roles`, `/group-requests/decide`). Apps show the group as created, and those posts as
written, by that person: never by the belfry.

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

### 2.3 A visitor claims: e-mail, then the code or the mail's Confirm button

The requests are NIP-98 signed by a key the browser just made (the same kind of key `/apply/*`
and `/login/*` take): the **claim-start key**. That key becomes the session's key, whichever way
the address is proven (Cards canvas, boards onb-code and onb-mail).

`POST /cards/<id>/claim-start` `{"email", "name", "title"?, "about"?, "hashtag"?, "summary"?,
"client"?}` → `202 {"sent": true}`. The form is checked as `claim-verify` checks it (below; a
topic's hashtag must be free: `409 hashtag_taken`). Nothing is **created**: the form is kept in
the register with the pending claim (table `card_claims`, one per card and key; a new
claim-start replaces the one before, and that one's link stops working), for 15 minutes. Then
one mail with a 6-digit code (15 minutes, 5 tries, only the newest works; purpose `card`, tied
to this card and this key) and a Confirm button:

- subject `Confirm your group: <title>`, for a topic `Confirm your topic: #<hashtag>`;
- "Hi <name>, to create the group <title> (the topic #<hashtag>), enter this code on the page
  where you scanned the card: 427 913. Or confirm with one click: [Confirm]. The code and the
  button work for 15 minutes. Didn't scan a card? Ignore this e-mail: nothing happens." Text and
  HTML parts; the person's own words are escaped in the HTML and never put in the URL;
- the button: `https://chat.<domain>/g/<id>?confirm=<token>`, the token 32 random bytes
  (base64url, 43 characters), single use, stored hashed, bound to the card, the address and the
  claim-start key.

The same answer whether the address has an account or not. Errors: `404 not_found`,
`409 claimed` + `group`, `invite`, `409 retired`, `409 hashtag_taken`, `422 invalid` + `field`,
`429 too_many` (the limits of `/apply/start` and `/login/start`: 20 code mails an hour per client
address, shared with them, and the register's per address and per key).

Older chats send `{"email"}` alone (none of `name`, `title`, `about`, `hashtag`): the mail then
has the code only (subject `<code> is your code for <community>`), nothing is kept, and the form
comes with `claim-verify`.

**The page then waits** ("…or click Confirm in the e-mail. This page moves on by itself once you
have"): it polls `claim-poll` and takes the code.

`POST /cards/<id>/claim-verify` `{"email", "code", "name"?, "title"?, "about"?, "hashtag"?,
"summary"?, "client"?}`: with any of `name`, `title`, `about`, `hashtag` in the body the whole
form is read from it (older chats; it wins over the kept one): `name` is the **person's** name
(1–100); `title`, `about`, `hashtag` as in §2.2. Without them the form kept by `claim-start` is
used (`email` may then be left out too). `client` as elsewhere (`belfry-web` | `belfry-ios` |
`belfry-android`, names the session).

The form, the card's state and (for a topic) the hashtag are checked **before** the code is
spent. Then, on the right code, under the card's lock:

- the address has an account here → a session on that account bound to the signing key (the
  same proof and the same code as the e-mail sign-in; docs/guests.md "An address that has an
  account"). `name` is ignored: the account keeps its name;
- else → a new guest account with that name and address (saved from the start), whose first
  session is the signing key;
- then the claim of §2.2 by that account.

`200 {"signed_in": true, "bunker_url", "pubkey", "npub", "nip05"?, "group"}` (+ `summary` for a
topic).

| Error | |
|---|---|
| `400 wrong_code` + `tries_left` | (status 400 here; `/login/verify` and `/guest/save/verify` use 422) |
| `410 expired` | no live code for this address, key and card; also when the mail's button started the group already |
| `409 claimed` + `group`, `invite` | someone was first. Seen before the code: the code is not spent. Seen after (a race): the code is spent, nobody is signed in, no account is made; join through `invite` |
| `409 retired` | |
| `409 hashtag_taken` | before the code is spent |
| `409 own_key` | the address's account signs with its own key: the belfry has no session to hand out |
| `409 agent` | the address is an agent's |
| `409 key_taken` | the signing key is linked to another account: make a new key, ask for a new code |
| `422 invalid` + `field` (`name` \| `email` \| `title` \| `about` \| `hashtag`) | |
| `503 signer`, `503 relay` | the code is spent: ask for a new one |

`POST /cards/<id>/claim-confirm` `{"token"}`, NIP-98 by whatever key the browser that opened the
link has (the chat page at `/g/<id>?confirm=<token>` sends it; 20 a minute per client address).
A valid token proves the address: the claim happens exactly as `claim-verify`'s would (the
account that has the address or a new guest, the group, the steward, the post in `general`, a
topic followed with `summary`, weekly by default), and the **session is bound to the
claim-start key** (the page that waits), never to the clicking key. The code of that address and
key stops working.

`200 {"confirmed": true, "group", "same_device"}`: `same_device` is true when the clicking key
is the claim-start key, and only then the answer also carries the signed-in body (`signed_in`,
`bunker_url`, `pubkey`, `npub`, `nip05`?, `summary`?): that browser has it, and `claim-poll`
then answers `410`. Otherwise the session waits for the claim-start page.

| Error | |
|---|---|
| `410 expired` | unknown, expired or used token; a newer claim-start replaced it; the code was quicker (one claim only) |
| `409 claimed` + `group`, `invite` | someone else was first: nothing is made |
| `409 hashtag_taken`, `409 retired`, `409 own_key`, `409 agent`, `409 key_taken` | as for `claim-verify` |
| `404 not_found` | no such card |
| `429 too_many` | |
| `503 signer`, `503 relay` | nothing is used: the same link can be opened again |

`POST /cards/<id>/claim-poll` `{}`, NIP-98 by the claim-start key, 30 a minute per key:

- `202 {"pending": true}` while the claim waits (15 minutes from `claim-start`);
- `200` with the `claim-verify` body (`signed_in`, `bunker_url`, `pubkey`, `npub`, `nip05`?,
  `group`, `summary`?) once the button was clicked on another device: handed out **once**, then
  forgotten (kept at most 15 minutes after the claim ran out);
- `410 expired`: the claim ran out, the session was handed out already, it was confirmed on
  this same device, or the code started the group;
- `404 not_found`: no claim of this key for this card (or no such card); `429 too_many`.

One lock per card; the first proven claim wins, by code or by button. The loser's group is
never made, and a visitor who lost gets no account. When the relay refuses after a new guest was
made, their pass is closed and their signer account suspended; the card stays unclaimed.

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

`GET /channel-invites/<token>` and the answer of `POST /channel-invites/<token>/join` carry one
more field for such a link: `"card": "group" | "topic"` (absent on every other invite). The chat
uses it to let a guest who gave no e-mail address post in a topic they reached by a card; the
server changes nothing else for that.

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

For the belfry's admins ("Printed cards" in the menu, under Community). The page and the sheets
follow the canvas boards `cards/print` (+ dark), `cards/print-sheet` and
`cards/print-sheet-channels`: their layout, words and sizes.

- **Make cards**
  - Groups or Topics, picked from two tiles that each show the printed card: Groups ("People who
    get to know each other. They join and say hi." · Account required · Portrait), Topics
    ("People come and go, share what they know and follow by e-mail." · No account needed to
    post · Landscape). Field `kind` = `group` | `topic`.
  - Cards per page: 1 (A4), 2 (A5) or 4 (A6). Field `per_page` = `1` | `2` | `4`.
  - Pages: 1 to 50 (`pages`), with the total next to it ("= 8 cards · 4 A6 on each A4").
  - Batch name (`batch`, up to 60 characters): "Batch N" unless typed, N = every batch this
    belfry made so far, plus one.
  - **Generate PDF** (`POST /cards/make`, csrf): makes `pages × per_page` cards and answers
    `303` to the batch's PDF, `GET /cards/batch/<batch>.pdf` (admins only; `application/pdf`,
    `Content-Disposition: inline; filename="<batch name>.pdf"`), which opens in a new tab.
    `<batch>` is the batch's number (the second it was made).
- **Cards made · N**: Card (its id) · For (Group / Topic) · Batch · State (Not scanned yet /
  Claimed / Retired) · Name (a group's name; a topic's `#hashtag`) · Started by (the person's
  name, with "(guest steward)" for a guest) · **Reset** on a used card (`POST
  /cards/<id>/reset`, csrf, a confirmation): unclaimed again, its link revoked, the group it
  started left exactly as it is. A batch's name opens its PDF again (not on the board: there is
  no other way to print a batch a second time).
- **The PDF** (`cardspdf.py`, drawn with fpdf2; a browser's own printing is not relied on:
  Safari ignores `@page`). The board's cards, black on white, in Inter (static TTFs made from
  the theme's own variable fonts, in `belfry_onboarding/fonts/`, OFL).
  - A group card (upright): the belfry's logo and name; "**Group** · people you get to know.
    Join and say hi."; a box "Group name:" with two lines to write on; a box "Description:" with
    four; the QR code (bottom left) with "Scan to join this group".
  - A topic card (on its side): logo and name; "**Topic** · come and go, share what you know.";
    a box "#" with a line; a box "About:" with three; on the right the QR code, "Scan to
    contribute to this topic" and "No account required".
  - The page: A4. Nothing is drawn within 8 mm of a paper edge. The cells are equal, each as
    large as that printable area allows at the card's own proportions, and the block of cells is
    centred on the page. Dashed cut lines run between cells from one printable edge to the
    other; none at the paper's edge.

    | Cards per page | Group cards (upright) | Topic cards (on their side) |
    |---|---|---|
    | 4 | upright A4, 2 × 2, each 97 × 137.2 mm | A4 on its side, 2 × 2, each 137.2 × 97 mm |
    | 2 | A4 on its side, side by side, each 137.2 × 194 mm | upright A4, one above the other, each 194 × 137.2 mm |
    | 1 | upright A4, 194 × 274.4 mm | A4 on its side, 274.4 × 194 mm |

    (The PDF page itself is upright or on its side as the table says: with two cards a page the
    other way round would make them no larger than four a page.)
  - The QR code is drawn as vector squares (error correction Q) with four modules of white kept
    clear on every side; it is made a little smaller than on the board when that takes it. The
    card's id and address are in the QR code only.
  - The logo: the one an admin uploaded (branding) when it can be fetched and drawn, else the
    theme folder's own `logo.svg` / `.png` / `.webp`, else the Arms in the belfry's colours
    (black without any). A long name is set smaller, then cut with "…": it never leaves the card.
  - Cards made before `per_page` existed are four to a sheet.

What the cards promise and what v1 does: "Scan to contribute to this topic · No account
required" is true for a topic that exists (someone who scans it reads it as a guest at once, and
the chat lets them post there without an e-mail: `card` in §3). **Starting** a topic, like a
group, still asks for an e-mail address and a code (§2.3).

Following a topic by e-mail (docs/followers.md): `claim` and `claim-verify` take `summary`
(`weekly`, the default, or `monthly`) and the person who starts a topic follows it at that
frequency; a visitor who joins a topic with the card's guest link follows it with `summary` on
`/guest/save/verify`. A visitor's `summary` rides with `claim-start` (kept with the form) or
`claim-verify`.

## 6. The proxy

`chat.<domain>/g/<id>` is answered by the chat's page itself (the page rule that serves the
app's routes), not cached. The phone apps open it too: `/g/*` is in apple-app-site-association; assetlinks.json covers the whole chat host already (Android)
yet. `belfry.<domain>/cards/*` is in the console's form limit; the API has the limit of
`/api/*` (60 a minute per address) on top of §2's own.

## 7. Not in v1

- Posting from a card without an e-mail address.
- A preview of the group's recent messages before joining.
- Deleting unused cards, moving a card to another group.

