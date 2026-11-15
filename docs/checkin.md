# Event screen, tickets, check-in, roles in a group

Built 2026-11-15 (server: `services/onboarding/belfry_onboarding/checkin.py`, tests
`tests/test_checkin.py`; web chat: `src/ui/EventScreen.tsx`, `src/ui/CheckIn.tsx`,
`src/core/checkin.ts`). The events themselves (NIP-52 event, its NIP-29 group `ev-<d>`, ticket
types as kind 30402, a ticket = a 9000 with a `ticket` tag) are described in `events.py`'s module
docstring and in the events protocol note (work/events-design/PROTOCOL.md in the belfry agent's
workspace).

Xavier's request (2026-11-15): opening an event shows a header with the event and my RSVP, half
pulled down, that can be pulled further down (who organises, who comes, the description) or up
(the chat); a switch between Chat, Photos and People; "Get tickets" or "Show my ticket"; the
ticket's QR opens the event with a check-in card for the event's host, co-hosts and stewards; on
the day those see "Check people in" (scan, or pick from the list). People lists show no tag icon;
tapping a person opens their profile, where a group's admins set the person's role in that group.

All calls are NIP-98 signed, under `https://belfry.<domain>/api/v1/`. Errors are
`{"error": "<code>", "message": …}`; clients show a fixed text per code, never the message.

## 1. Roles in a group (a channel, a group, an event)

| Role | What it is | UI word |
| --- | --- | --- |
| `admin` | the relay's 39001 for the group, plus everyone put in with the admin role (below) | Admin; in an event the creator is "Host", the others "Co-host" |
| `steward` | events only: may check people in and see the attendee list | Steward |
| `member` | everyone else in the group | Member; in an event "Attendee" |

**Admins by role.** zooid (the groups relay) has no per-group admins: its 39001 lists the relay
owner, the keys with the `moderategroups` method (the community's admins, patch 0003) and the
group's creator, and it refuses moderation events from anyone else. A per-group admin is
therefore a Belfry convention on top of the relay's events: the latest 9000/9001 for that key in
the group is a 9000 whose `p` tag carries `admin` (`["p", <key>, "admin"]`, as co-host links
always did). A 9000 tagged `["role", "member"]`, or a 9001, ends it. A ticket's 9000 (no `role`
tag) does not. The server (`checkin.role_admins`, used by `events.hosts_of` and
`mail.group_admins`) and the chat (`model.ts`) read it the same way. What such an admin does on
the relay goes through the belfry, signed by the groups key (decisions on applications,
check-in, roles); moderation events they sign themselves (a 9000, a 9002 metadata edit) are
refused by the relay. A relay patch would lift that; not done (see "Open" below).

**Stewards.** Kind 1985 by the groups key in the group: `["L","belfry/group-role"]`,
`["l","steward","belfry/group-role"]` (or `"none"` to clear), `["p", key]`, `["h", group]`,
`["by", caller]`. The latest per `p` wins; labels by any other key count for nothing.

`POST /group-roles {"channel": "<group id>", "pubkey": "<hex>", "role": "admin"|"steward"|"member"}`
by an admin of the group or a belfry admin → `{"role": "<now>"}`.

- admin: a 9000 `["h", g] ["p", key, "admin"] ["role", "admin"] ["by", caller]`; a steward label
  is cleared;
- member: when they were an admin by role, a 9000 `["p", key, "member"] ["role", "member"]`; a
  steward label is cleared;
- steward: (demoted first when an admin) the steward label.

Errors: 403 `not_allowed`; 409 `not_in_group` (not in the group's 39002 / latest 9000, holds no
ticket and has no RSVP for the event); 409 `last_admin`; 409 `guest_not_saved` (an admin needs an
account with an e-mail); 409 `agent`; 409 `events_only` (steward outside `ev-` / `ws-`); **409
`always_admin`** (added: the group's creator, the community's admins and the relay's own keys are
admins of the group by the relay itself and can't be made member or steward); 422 `invalid`;
503 `relay`.

## 2. Tickets and their QR

Who holds a ticket: a 9000 with a `ticket` tag (not taken back by a later 9001); on an event that
has only the implicit free ticket (no ticket types), also every key whose own RSVP says going
(`accepted`), with ticket `<d>:free` (decision: so a free event can be checked in too).

Ticket code: `base32(HMAC-SHA256(secret, "belfry-ticket:<d>:<pubkey>")[:10])`, lowercase, 16
characters; the secret is `PASS_PAGE_SECRET`, else `HOOK_TOKEN`. The same every time; another
event's code never matches.

`GET /events/<link>`: the viewer block (`me`, also sent as `viewer`) gains

- `ticket_code` (mine, or null), `ticket_url` = `https://chat.<domain>/events/<link>/t/<code>`
  (what the QR holds);
- `checked_in`: unix time or null;
- `roles`: subset of `["host", "cohost", "steward"]` (the community's admins, who administer
  every group, are not listed as co-hosts);
- `may_check_in`: host, co-host, steward, any other admin of the group, or a belfry admin.

Caddy (chat host): `/events/<link>/t/<code>` → 302 `/#/events/<link>/t/<code>`; the phone apps
take it as a universal / app link (`/events/*` is already in apple-app-site-association).

## 3. Check-in

- `POST /events/<d>/attendees {}` → `{"people": [{"pubkey", "name", "ticket", "ticket_title",
  "rsvp", "checked_in", "role"}], "going": n, "checked_in": n}`: the ticket holders, sorted by
  name; `role` is `host` / `cohost` / `steward` / `member`; `rsvp` is their own RSVP, `accepted`
  when they gave none.
- `POST /events/<d>/checkin {"code"}` or `{"pubkey"}`, optional `"undo": true` →
  `{"person": {…}, "already": bool}`. `already` = it was in that state (then `checked_in` is the
  time of the first check-in). Errors: 404 `no_ticket` (also for another event's code or a code
  of the wrong shape), 403 `not_allowed`, 422 `invalid`, 503 `relay`.
- Recorded as kind 1985 by the groups key in the group: `["L","belfry/ticket"]`,
  `["l","checked-in"|"not-checked-in","belfry/ticket"]`, `["p", key]`, `["h","ev-<d>"]`,
  `["by", caller]`. The latest per `p` wins; only the groups key's labels count.
- The server does not restrict the time. Clients show "Check people in" on the event header from
  00:00 local on the first day to the end of the last day, and always in the People tab's menu
  for those who may.

## 4. The web chat

See the chat's section in this file's history and `src/ui/EventScreen.tsx`. In short: the event
screen has three levels (bar / half / full), a Chat · Photos · People switch, "Get tickets" /
"Show my ticket" (full-screen QR), "Check people in" (camera with BarcodeDetector or a jsQR
fallback, a result card for 2 s, the list with search, undo with confirmation). Profiles opened
from a group show "In <group>" with the role picker for that group's admins; the belfry-wide
"Role" section replaced the "Admin of the belfry" switch.

## Open

- A zooid patch (0005) letting per-group admins moderate on the relay and listing them in 39001
  would make clients that only read 39001 see co-hosts too. Not done: a relay rebuild on every box.
