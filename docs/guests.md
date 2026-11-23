# Guests of a channel

Someone without a membership, in one channel, for a day, a week or a month. The code is
`services/onboarding/belfry_onboarding/guests.py` (its docstring is the reference) and the
register's `join_guest` / `save_guest_email`. Guests need the belfry's own register
(`REGISTER=belfry`). All paths below are under `https://belfry.<domain>/api/v1`; requests marked
NIP-98 carry `Authorization: Nostr <base64 kind 27235>` with `u`, `method` and `payload` tags.

## The link

`POST /channel-invites` (NIP-98, a member who has the channel) `{"channel", "people"?: 1|50,
"link_days"?: 1|7|30, "guest_days"?: 1|7|30}` → `{"url": "https://chat.<domain>/join/<token>",
"days", "as": "guest", …}`. `link_days` is how long the link can be opened; `guest_days` how long
the guest stays (absent: 30, as older clients expect; the chat sends it, a day by default).

A belfry admin can make the link a **member link** instead (`"as": "member", "role",
"membership"`): see docs/roles.md. Opening and joining work as below; joining makes them a member
with that role.

A **printed card** (docs/cards.md) stands for a guest link of the group started from it: the same
link and the same endpoints, with no end date and no number of people (`guest_unlimited`); a
guest's own access still lasts 30 days, and the link makes at most 100 new guest accounts a day
(then `410 closed`, as a used-up link). The visitor who started the group is a guest too, and the
steward of that one group ("A guest as steward" there).

`GET /channel-invites/<token>` (no auth) → `{"community", "channel", "channel_name", "about",
"inviter", "state": "open"|"used"|"expired"|"revoked", "days", "role", "as": "guest"|"member"}`
(member links add `member_role`, `member_role_label`, `membership`; a printed card's link adds
`"card": "group" | "topic"`, and so does the answer of `…/join`: docs/cards.md §3); 404
`not_found`.

## Opening it: guest mode (since 2026-11-12)

`POST /channel-invites/<token>/join` (NIP-98). Who signs decides:

| Signed by | Body | Result |
|---|---|---|
| the key of an account here | anything | that account gets the channel: `{"channel", "new": false, "guest", "already"?}` |
| a new key, the one that will hold the session | `{}` | a nameless guest in **guest mode**: `{"channel", "new": true, "guest": true, "reading": true, "bunker_url", "pubkey"}` |
| a new key | `{"name": "Ana"}` | a guest by that name at once (older clients): the same answer with `"reading": false` |

Errors (fixed codes): 410 `closed` (used, expired, revoked), 410 `for_agent`, 409 `session_taken`
(the signing key is another account's session: make a new key), 409 `unsupported`, 503 `failed`.
`name_needed` is no longer answered here. The request is idempotent per signing key: the same key
gets the same guest again, a new session, and the link is counted once.

In the channel (groups relay), signed by the groups key:

- guest mode: kind 9000 `["h", channel] ["p", guest, "member"] ["guest", <last day>]
  ["invited-by", inviter] ["unsaved", "1"] ["reading", "1"]`, **no `name`**. Clients show no join
  line for an event with `reading`, and leave that key out of member lists, counts, "active"
  numbers and mention pickers until a later 9000 without `reading` (or the label `saved`).
- by name (older clients): as before, `["name", …] ["unsaved", "1"]`.

Guest mode is read-only **in the client**: where the composer would be it says "Guest mode" with a
Join button, and nothing that writes is sent. The relay itself treats such a key like any guest
of the group and would store what it signs.

## Joining: a name, an e-mail, the code mailed to it

Both requests are NIP-98, signed with the guest's identity (through the session).

1. `POST /guest/save` `{"email", "name"}` → 202 `{"sent": true}`: a 6-digit code goes to the
   address by e-mail. 422 `invalid` (not an address), 422 `name_needed` (guest mode needs the
   name), 409 `not_guest`, 429 `too_many`. Send it again for a new code.
2. `POST /guest/save/verify` `{"email", "code", "name"}` → 200 `{"saved": true, "joined": bool}`.
   422 `wrong_code` (message says how many tries are left), 410 `expired`, 422 `name_needed`,
   409 `not_guest`, 409 `taken`.

`joined: true` (they were in guest mode): name and address are on their contact, and the channel
gets a second kind 9000 for the same key with `["name", <name>]` and neither `reading` nor
`unsaved` (the join line, at the time they joined), then kind 1985 `["L", "belfry/guest"]
["l", "saved", "belfry/guest"] ["p", guest]`. A guest who joined by name sends the e-mail only
(`name` optional; when sent it replaces the contact's name) and gets `joined: false` and the
label alone, as before.

409 `taken`: the address is another account's here. Body `{"error": "taken", "message",
"moved": bool}`. For a guest in guest mode with `moved: true`, the belfry has given that account
the channel (one more guest pass for a guest; a 9000 for a member) and closed the anonymous guest
(9001 for its key, its signer account suspended): the client drops the guest session and signs in
with the address; the channel is there. `moved: false`: nothing changed (the account has no key).

Guests who never join end with their pass, like every guest.

A member link (docs/roles.md) adds to these answers: the join answer has `"as": "member"` and
`"email_needed": true` until they joined; `/guest/save/verify` answers `"member": true, "role"`
once the membership opened. An agent's key can't use a member link (403 `agent`).

**A member link asks for an e-mail only** (since 2026-11-19): in guest mode after a member link,
`/guest/save` and `/guest/save/verify` take `{"email"}` / `{"email", "code"}` without `name` and
never answer `name_needed`. The name is then the part of the address before the @, its words
(split on `.` `_` `-`) capitalised, at most 100 characters (`jane.doe@…` → "Jane Doe"); they
change it in their profile. A name that is sent is used as before. Guest links (not member
links) keep `name_needed`.

## An address that has an account: the right code signs you in

(2026-11-19; canvas onboarding/guest-code.) A guest who gives an address that is another
account's here is not sent to sign in a second time. The device makes a **new session key** (its
guest session key is bound to the guest account: one key, one identity) and sends its public key
with the code:

    POST /api/v1/guest/save/verify      (NIP-98 by the guest, as before)
    {"email", "code", "name"?, "session_key": "<64 hex: the new key's public key>",
     "client": "belfry-ios" | "belfry-android" | "belfry-web"}

- The code is right and the address is another account's →
  `200 {"signed_in": true, "bunker_url", "pubkey", "npub", "nip05", "moved", "channel"}`:
  a session on **that account's** signer, bound to `session_key` when it is made (the address
  alone opens nothing), named after `client` ("Belfry for iPhone", "Belfry for Android", else
  "Chat"). `pubkey` / `nip05` are the account's. `moved` as before (a guest in guest mode: the
  account got the channel and the anonymous guest pass is closed). `channel` = the channel the
  guest was in, to open next (null when none). The device drops its guest session and key,
  stores the new session like any sign-in, and says "Signed in as <address>". No second code.
- Without `session_key` (older apps), or when that account can't be signed in by the belfry
  (it holds its own key): `409 taken` with `moved` and `nip05`, as before.
- `session_key` that is not 64 hex, or is the guest's own key: `422 invalid`, checked before the
  code is spent. `409 key_taken`: that key is linked to another account (make a new one and ask
  for a new code). `503 signer`: try again (the code is spent: ask for a new one).
- Whether the address has an account is still said only after the code is proven: `/guest/save`
  answers 202 either way, and the mail reads the same.
- The proof is the same as the e-mail-code sign-in's (a 6-digit code mailed to the address, the
  same tries), so this opens nothing a sign-in would not.
