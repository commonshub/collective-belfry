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
  nobody a member of the belfry.
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
