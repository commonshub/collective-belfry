# Guests of a channel

Someone without a membership, in one channel, for 30 days. The code is
`services/onboarding/belfry_onboarding/guests.py` (its docstring is the reference) and the
register's `join_guest` / `save_guest_email`. Guests need the belfry's own register
(`REGISTER=belfry`). All paths below are under `https://belfry.<domain>/api/v1`; requests marked
NIP-98 carry `Authorization: Nostr <base64 kind 27235>` with `u`, `method` and `payload` tags.

## The link

`POST /channel-invites` (NIP-98, a member who has the channel) `{"channel", "people"?: 1|50,
"link_days"?: 1|7|30}` → `{"url": "https://chat.<domain>/join/<token>", …}`.

`GET /channel-invites/<token>` (no auth) → `{"community", "channel", "channel_name", "about",
"inviter", "state": "open"|"used"|"expired"|"revoked", "days", "role"}`; 404 `not_found`.

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

Guests who never join end with their pass (30 days), like every guest.
