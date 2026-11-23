# Membership, groups and vouches

Xavier, 2026-11-14: "there is a clear distinction between the membership in the belfry and just
joining a subgroup that somebody has created." Code: `services/onboarding/belfry_onboarding/`
`membership.py` (settings, requests to join the belfry) and `vouches.py` (groups that accept by
vouches); their docstrings are the reference. Both need the belfry's own register
(`REGISTER=belfry`); with Odoo the settings read as the defaults and changes answer 409
`unsupported`.

All paths are under `https://belfry.<domain>/api/v1`, signed with NIP-98.

## Words

- **Member** (of the belfry): a pass of tier `member` / `steward` (whois `member`). Admins invite
  members; any member too when the setting says so.
- **Group**: what the chat called a *pod* until 2026-11-14. A NIP-29 group with a parent channel
  (or group). The protocol keeps the old word: ids `pod-<slug>-<4 hex>`, the parent's card is a
  kind 9 with `["pod", <id>]`. Any member creates one and is its admin; its admins let people in
  (members directly, others as guests of that group: docs/guests.md). Being in a group makes
  nobody a member of the belfry. A group can also be started from a printed card, by a guest
  too, who is then the steward of that group and of no other (docs/cards.md §4).
- **Channel**: unchanged. "Groups relay" stays the internal name of the NIP-29 relay.

## Membership settings

Register param `belfry.membership`:

```json
{"invite": "admins" | "members", "accept": "admins" | "vouches", "vouches": 1..5}
```

Defaults: `admins`, `admins`, `2`.

| Request | Who | Answer |
|---|---|---|
| `GET /membership` | anyone with an active pass | `{"invite", "accept", "vouches", "may_invite_members", "admin"}` |
| `POST /membership` `{"invite"?, "accept"?, "vouches"?}` | belfry admins | the same |

`invite: "members"` lets any member make a member link (`POST /channel-invites` `{"as":
"member"}`), for the default role only (another role: 403 `not_admin`). Turning vouching on or
lowering the number accepts the requests that have enough vouches already.

## Requests to join the belfry

People ask on `pass.<domain>/apply` (name, e-mail, a few words; apply.py): register model
`belfry.application` (`state` `new` → `invited` | `declined`), a confirmation mail to them, a mail
to the admins. Accepting = the existing invite (`belfry_application_invite`: an invite mailed to
the applicant, who opens it, gives name + e-mail and gets the membership and its welcome mail).

`POST /applications`:

| Body | Who | Answer |
|---|---|---|
| `{"list": true, "state"?: "new" \| "all"}` | admins; members while `accept` = `vouches` | `{"applications": [{"id", "name", "motivation", "kind", "state", "created", "vouches", "vouched", "email"?}], "accept", "needed"}` (`email` for admins only; `all` for admins only) |
| `{"decide": <id>, "approve": bool}` | admins | `{"application"}` |
| `{"vouch": <id>, "on"?: bool}` | members while vouching | `{"application", "accepted": bool}` |

Vouches are kept on the application (`vouched_by`: member keys). Only keys that are members now
count (guests, agents and people who left don't). When the count reaches the number, the request
is accepted as an admin's would be, with `handled_by` = `vouches`. The service's round
(`followers.tick`, every 5 minutes) also settles them. Errors: 403 `not_admin`, 403 `not_here`,
403 `not_vouching`, 404 `not_found`, 409 `decided`, 422 `invalid`, 409 `unsupported`.

## Applying from the chat (verified)

Since 2026-11-19 (`applyverified.py`; needs the belfry's own register, else 409 `unsupported`).
The whole flow stays on `chat.<domain>`: the chat makes a key in the browser, the **applicant
key**, and signs every request below with it (NIP-98, `belfry.<domain>/api/v1/…`). A 6-digit
code mailed to an address is the only proof of that address (10 minutes, 5 tries, as every
e-mail code here); a session is only ever bound to the key that signed the request.

| Request | Answer |
|---|---|
| `POST /apply/start` `{"email", "motivation", "name", "username"?, "agreed": [<agreement id>…]}` | `name` is expected; `username` is the address they would like here (below, "A name and a username"): 400 `missing` when it comes without a name, 422 `username_invalid` (with `reason`), 409 `username_taken` (with `suggestions`); it is kept with the request and **not reserved** while the request waits. Older chat builds send neither: the name is then the part before the @. 202 `{"sent": true}`: a code is mailed ("Your code to apply to <community>: 123456"), the same answer whether the address is known or not. Checked as `/api/apply`: 400 `missing` (e-mail or motivation), 400 `unticked` (every agreement of the charter must be ticked), 422 `invalid` (not an address); the hidden field `website` filled in → 202 and nothing happens. 429 `too_many` (5 codes per address and 10 per key in 30 minutes, 20 per network address an hour). |
| `POST /apply/start` with `"proof"` (from `/login/verify`, same `email`, same key) | no code is mailed: 200 with the answer of `/apply/verify`. Invalid or expired proof: 422 `proof` (the chat falls back to the code). |
| `POST /apply/verify` `{"email", "code"}` | 422 `wrong_code` with `tries_left`; 410 `expired`. Right code, and the address has an account here (a member, or a guest who saved it): 200 `{"state": "signed_in", "bunker_url", "pubkey", "npub", "nip05"}`, a session on that account bound to the applicant key (as `guests.md`, "An address that has an account"); an account that holds its own key: 409 `taken` with `nip05`; 409 `key_taken`; 503 `signer`. Otherwise the application is stored (`verified`, the applicant key, the name, the username they asked for, the motivation, the ticked agreements) and the admins get the mail and push of a request to join, once: 200 `{"state": "pending"}`. A request that waits already for this address or this key: the same answer, no second request, no second mail (the key that proved the address last is its applicant key). |
| `POST /apply/status` `{}` | `{"state": "none" \| "pending" \| "declined" \| "approved"}` for the latest request of this key. `pending` also carries what they sent, shown back to the browser that holds the applicant key and to nobody else: `"name", "username", "email", "motivation"`. `declined` is the state alone (a decline has no note here). `approved` comes with `"signed_in": true, "bunker_url", "pubkey", "npub", "nip05"`: a session on the new member's key, bound to the applicant key, made on this call. Once it was handed out (or the signer knows the key already) the answer is `{"state": "approved", "signed_in": false, "nip05"}`: the chat shows Log in with the address filled in. One register lookup while the request waits: made to be asked every 5 seconds. |
| `POST /login/start` `{"email"}` | 202 `{"sent": true}` for any address that looks like one, known or not ("Your code for <community>: 123456"); 422 `invalid`; 429 `too_many` (the same limits). `email` may be the member's address here, `<username>@<domain>`: the code then goes to that member's own e-mail address, never to the belfry mailbox. An address at the belfry's domain that is an account's own e-mail address stays that; a username nobody has is treated as any unknown address (the same 202; the code is mailed to the address as typed, which no member reads). `/login/verify` takes the same words as `/login/start`. |
| `POST /login/verify` `{"email", "code"}` | 422 `wrong_code` (`tries_left`), 410 `expired`. Right code: the address has an account (also a request that was approved meanwhile) → `{"state": "signed_in", …}` as above (409 `taken` + `nip05`, 409 `key_taken`, 503 `signer`); a request for it waits → `{"state": "pending"}` and this key is its applicant key from now on (`/apply/status` answers for this browser; the earlier key gets `none`); nobody knows it → `{"state": "unknown", "proof"}`. |

`proof` is `<expiry>.<HMAC-SHA256>` over the lower-cased address, the applicant key and the
expiry (15 minutes ahead), keyed from `PASS_PAGE_SECRET`: checked without anything stored, good
for that key and that address only. A code sent for applying is not taken by `/login/verify`, and
the other way round.

**Accepted** (an admin's `decide`, or enough vouches): the person is a member at once. Their
contact with the name of the request and a free membership (the activation an accepted invite
ends in: default role; the username they asked for when it is still free and valid, else the
first free suggestion, else the usual one from their name: an approval never fails over a
username), the agreements they ticked signed for them (`charter.md`), and one
mail: the welcome mail (`register.md`). No invite, no confirmation mail, no sign-in link. State
`accepted`. A browser that still waits gets its session from `/apply/status`; anyone else opens
the mail's button and logs in with the address. **Declined**: the usual mail; `/apply/status`
says `declined`, and they may ask again.

List rows of `POST /applications` carry `"verified": true` for these requests (false for the
others) and, next to `name`, `username`: the one they asked for (after the acceptance, the one
they got; "" when they sent none). The older ways keep their invite: the form on `pass.<domain>/apply` and `POST
/api/apply` (older chat builds) store an unproven request, and accepting it mails an invite. If
the person later proves that address (`/apply/verify` or `/login/verify`), the request that waits
becomes a verified one.

## A name and a username (2026-11-19)

The username is the person's address here, `<username>@<domain>`: they log in with it, people
mention them with it, and it receives direct messages and e-mails. They pick it when they apply
(with their name), or right after the code when they sign up from a member invitation. Rules:
`usernames.py` (1 to 30 of `a-z 0-9 . _ -`, starting and ending with a letter or digit; the
reserved names; a channel's name counts as taken: it has a mailbox).

`POST /username/check` (NIP-98 by **any** key: an applicant key with no account, a guest's, a
member's), 60 calls a minute per key and per network address (429 `too_many`):

- `{"username", "name"?}` → `{"username": "<lower-cased, trimmed>", "state": "available" |
  "taken" | "invalid", "reason"?: "short" | "long" | "chars" | "reserved", "suggestions": [up to
  3]}`. The caller's own username is `available`. `suggestions` is empty for `available`.
- `{"name"}` alone → `{"suggested": "<the first suggestion, or empty>", "suggestions": [up to
  3, the suggested one first]}`.
- Suggestions, valid and free ones only, in this order: the first name (`lina` for "Lina
  Moreau"), first-last (`lina-moreau`), then `lina-2`, `lina-3`… From `name` when sent, else
  from the username itself.
- "taken" is remembered for 5 seconds. One signer lookup (its `nostr.json`) and the channel
  check per name asked.

**A member invitation** (`guests.md`): `/guest/save` and `/guest/save/verify` are unchanged (an
e-mail and its code; the name starts from the address). The chat then asks for both and sends

`POST /welcome` `{"name", "username"}`, NIP-98 by the new member's own key →
`{"name", "username", "nip05"}`. The contact gets the name, the signer the username, and the
profile (kind 0) `name`, `display_name` and `nip05`; its picture and about stay. Allowed while
the username is still a default one (a number, `member-N`, `guest-N`) or the first membership
started less than 24 hours ago; later 409 `already_set` (the profile screen: `POST /username`).
Errors: 400 `missing`, 403 `not_member` (guests, agents, no account), 403 `unverified`, 409
`own_key`, 422 `username_invalid` (`reason`), 409 `username_taken` (`suggestions`).

The welcome mail (`register.md`) of someone who came with a member link waits for this call, so
it names the address they picked; when the call never comes it leaves ten minutes after the
membership started (`welcome.tick` on the 5-minute round, so 10 to 15 minutes). One welcome
mail either way. An applicant's welcome mail leaves at the approval.

## Groups that accept by vouches

A closed group's metadata edit (kind 9002 by one of its admins) carries next to `closed` and
`prompt`:

```json
["vouches", "2"]
```

Absent or `0` (or the group `open`): its admins accept. The newest 9002 / 39000 that says
anything about access wins.

A request is a 9021 by the applicant in the group, content = their answer. A **vouch** is a
NIP-32 label in the group by one of its members:

```json
{"kind": 1985, "tags": [["h", "<group>"], ["L", "belfry/vouch"],
  ["l", "vouch", "belfry/vouch"], ["e", "<the 9021's id>"], ["p", "<applicant>"]]}
```

`["l", "withdrawn", "belfry/vouch"]` takes it back; the latest label per author and request
counts. A vouch counts when its author is in the group (its 39002), is a member of the belfry
(whois `member`: not a guest, not an agent) and is not the applicant. A new 9021 from the same
person starts again from zero.

When a waiting request (no 9000 / 9001 / 9022 for that person after it, not in the group) has
enough vouches, Belfry admits them as the groups key (the relay's owner):

```json
{"kind": 9000, "tags": [["h", "<group>"], ["p", "<applicant>", "member"], ["by", "vouches"],
  ["vouches", "<n>"], ["e", "<the 9021's id>"]]}
```

`vouches.watch` runs in the service's round over the requests of the last 30 days;
`POST /group-requests/check` `{"channel": "<group>"}` (anyone with an active pass) runs it at once
for one group → `{"admitted": ["<hex>"]}`. Both are idempotent. Admins can still accept (9000)
or decline (9001) any request.

## The chat

Requests sit at the top of the channel list (and add to the count on ☰): agents asking to join
(belfry admins; "Let it in as my agent" = `POST /agent-requests {"decide"}`), people asking to
join the belfry, and the requests of every group the person runs (or is in, when it accepts by
vouches). A new group's card in its parent opens the group's page (`#/c/<id>/group`): its
description, who's in, its admins, Follow (the bell: its announcements by e-mail) and Join /
Apply to join / Pending / You're in. Belfry settings → Membership holds the two settings.

## A guest asks to become a member (2026-11-16)

A guest (docs/guests.md) has an account, a saved e-mail and a key already, so they ask from the
app and stay that person. Code: `services/onboarding/belfry_onboarding/guestapply.py` (its
docstring is the reference). Same endpoint, NIP-98 by the guest's own key:

| Body | Answer |
|---|---|
| `{"apply": {"answer": "<≤ 2000>", "agreed"?: ["<2100 id>", …]}}` | 200 `{"application": {"id", "state", "vouches", "needed", "vouched_by": [{"pubkey", "name"}]}, "until": <unix>, "accept": "admins"\|"vouches"}` |
| `{"apply": "status"}` | the same, `application` = their latest request or `null`, plus `agreed` |

- Who: an active guest pass (not an agent's) whose contact has an e-mail. Else 403 `not_guest`
  (members, agents, unknown keys) or 409 `guest_not_saved` (a guest who hasn't joined with an
  e-mail yet). 422 `invalid` (empty or too long answer, `agreed` not a list of event ids), 409
  `unsupported` (Odoo). `status` answers anyone who has a request on record, members too.
- One open request per guest: another answers 409 `pending` with the same body (and `error`).
- `until`: when their latest guest pass ends (end of its last day, UTC; after a decline, the
  grace time below).
- States: `new` → `accepted` (now a member) | `declined`. A declined guest may ask again.
- Stored as a `belfry.application` of kind `guest` with `pubkey`, `guest_partner`, `channels`,
  `inviter` (key), `since` (first guest day), `agreed`. The admins' list rows gain `channels`,
  `inviter` (name) and, for guest rows, `pubkey` and `since`.
- The admins get a mail ("a guest asks to become a member") and a push notification of type
  `membership_request` (url `https://chat.<domain>/#/requests`).
- **Accepting** (an admin's `decide`, or enough vouches) mails no invite: the guest becomes a
  member on the same key with the default role, as when an admin makes a guest a member
  (roles.make_member: a membership pass, guest passes closed, the label `member` in their
  channels). 409 `gone` when they are no longer a guest here. Then the agreements they ticked are
  signed for them (docs/charter.md).
- **While a request is open** their guest passes don't expire. **Declined**: they stay a guest;
  a pass whose last day passed meanwhile gets 24 hours from the decline (`guest_grace_until`;
  the decline mail tells them they stay a guest).

The web form `/apply` lists the charter's agreements with a checkbox each when there is one
and takes the request only with all ticked (`agreed` on the application, own register only);
when the applicant takes the invite and their membership opens, their ticks are signed for them.

## Requests to a closed channel or group, answered through the belfry

`POST /api/v1/group-requests/decide` (NIP-98) with `{"channel", "pubkey", "action": "approve" |
"decline"}`, by an admin of that channel (on the relay's list, its creator, or an admin by role:
see docs/checkin.md) or a belfry admin. The belfry answers the person's latest unanswered
request (their 9021) as the groups key: a 9000 (`["p", <key>, "member"]`) or a 9001, with
`["by", <who decided>]` and `["e", <the 9021>]`. Answers `{"approved": true}`,
`{"declined": true}` or `{"already": true}` (approved since). Errors: 403 `not_allowed`, 409
`no_request` (nothing open: none, withdrawn, or answered), 409 `archived`, 409 `event` (an
event's requests go through `/events/<d>/decide`), 422 `invalid`, 503 `relay`. Admins the
relay knows (its list, the creator) may still publish the 9000 / 9001 themselves; clients
should use this call for everyone so an admin by role sees the same buttons.
