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
