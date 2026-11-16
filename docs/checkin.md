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

- removed (`"role": "removed"`): a 9001 by the groups key `["h", g] ["p", key] ["by", caller]`
  (for an event this also takes the ticket back, as a host's removal does), a steward label
  cleared. `always_admin` for the creator, the community's admins and the relay's keys;
  `last_admin` when the person is the last admin. Answer `{"role": "removed"}`. Admins by role
  can't publish a 9001 on the relay themselves: clients use this call (and the direct 9001 only
  when the belfry answers 404 without an error code, i.e. it predates this call).

Errors: 403 `not_allowed`; 409 `not_in_group` (not in the group's 39002 / latest 9000, holds no
ticket and has no RSVP for the event); 409 `last_admin`; 409 `guest_not_saved` (an admin needs an
account with an e-mail); 409 `agent`; 409 `events_only` (steward outside `ev-` / `ws-`); **409
`always_admin`** (added: the group's creator, the community's admins and the relay's own keys are
admins of the group by the relay itself and can't be made member or steward); 422 `invalid`;
503 `relay`.

### A group's name and description: `POST /group-meta`

zooid also refuses a 9002 (edit metadata) from an admin by role (checked with a Go test against
the pinned zooid). And each 9002 *replaces* the whole metadata (its 39000), so a 9002 with only a
name drops `closed`, `prompt`, `vouches`, `picture`.

`POST /group-meta {"channel", "name"?, "about"?, "picture"?, "closed"?, "prompt"?, "vouches"?}` by
an admin of the group (any kind, including by role) or a belfry admin. The server takes the
group's current metadata (the relay's latest 39000, without `d`), replaces only the fields sent
(`name` 1–100 characters, `about` up to 2000, `picture` an https:// URL or "" to remove,
`closed` true/false → `closed` / `open`, `prompt` "" to remove, `vouches` 0–5, 0 removes) and
publishes it as a 9002 by the groups key. Answer `{"channel", "name", "about", "closed",
"archived"}`.
Errors: 403 `not_allowed`, 404 `not_found` (no metadata), 422 `invalid`, 503 `relay`.

Since 2026-11-16 it also takes `"archived": true | false` (docs/archive.md): an archived group
refuses every other change here, and `/group-roles`, with 409 `archived`; `general` is never
archived (409 `always_open`).

Clients publish their own 9002 when they can (the relay's 39001, the creator, a community admin)
and use this call otherwise: the web chat's channel / group settings, an event's edit (name,
cover) and its tickets (closing the group when a ticket needs an application).

Also through the belfry for admins by role on the web: approving / declining an event's
applications (`/events/<d>/decide`, existing) and "Make co-host" (`/group-roles`). Not covered:
accepting someone into a closed group (pod) by an admin by role: the web hides Accept / Decline
for them (they can still vouch); the relay-level admins accept.

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
- `"peek": true` (with a code or a pubkey): who it is and whether they are in
  (`already` = checked in), nothing recorded. For the card a ticket URL opens.
- Recorded as kind 1985 by the groups key in the group: `["L","belfry/ticket"]`,
  `["l","checked-in"|"not-checked-in","belfry/ticket"]`, `["p", key]`, `["h","ev-<d>"]`,
  `["by", caller]`. The latest per `p` wins; only the groups key's labels count.
- The server does not restrict the time. Clients show "Check people in" on the event header from
  00:00 local on the first day to the end of the last day, and always in the People tab's menu
  for those who may.

## 4. The web chat (as built: village-app branch `checkin`)

Files: `src/ui/Events.tsx` (the event screen), `src/ui/CheckIn.tsx` (ticket, check-in, ticket
card), `src/core/checkin.ts` (roles, labels, API calls, error words), `src/core/scan.ts` (QR from
the camera), `src/core/demoevent.ts` (demo data). Browser test: `site/.shots/chat/checkin.cjs` in
the belfry agent's workspace (58 checks, including a fake camera showing a ticket's QR).

- **The event screen.** One header at three levels: bar (date + my status chip), half (where it
  opens: cover, when — the time zone only when not mine —, where → OpenStreetMap / the online link,
  my control, "N people ask to come · Review" for hosts, "Check people in" for host / co-host /
  steward / admins from 00:00 on the first day to the end of the last, "N going · M maybe" with
  faces) and full (also Hosted by, Going, Maybe, tickets, the description, URL + Share + QR, and
  for hosts Edit / Invite a co-host / Guest list). Drag the header or its handle down / up; tap
  the title to fold to the bar or open half; on ≥900 px the handle is a chevron (half ↔ full) and
  a wheel or touch scroll in the content folds the header to the bar.
- **My control.** No ticket types: Going / Maybe / Can't go (and "Show my ticket" as a small
  button once going: the implicit ticket has a code too). Ticket types: "Get tickets" until I
  hold one, then "Show my ticket" + the RSVP row. Applied / waitlist / declined: the state and
  Withdraw.
- **Chat · Photos · People** show once I'm in (in the group, holding a ticket, a host, or having
  answered the plain RSVP); before that the header and the start of the description. Photos: the
  group's images newest first (3 columns on a phone), tap → the viewer, "Add photos" (a kind 9 with
  the images, as the composer sends them). People: Hosts (Host / Co-host / Steward), Going (ticket
  type as the second line), Maybe; a ✓ for who is checked in, only for those who check in; a
  message button per row; tap → the profile opened from the event.
- **Show my ticket.** Full screen on white, QR of `ticket_url`, my name (none rather than "You"),
  the ticket type, the event and its time, "Checked in at HH:MM" once done; screen kept on with
  the Wake Lock API where the browser allows it (no web API raises the brightness).
- **Check people in.** Camera on top (45 % of the height; a 132 px note when there is no camera);
  a frame is read every 250 ms; a text that is a URL ending `/t/<16 base32>` sends only the code;
  vibrate, result card for 2 s (green / amber / red), the same text ignored for 3 s; then
  "N of M checked in", a search field, the ticket holders (name, ticket type, "in at", a check);
  tap → check in (locks), tap a checked one → "Undo check-in?". The list follows the relay's
  labels live, so every checker's phone updates.
- **A ticket URL** (`chat.<domain>/events/<link>/t/<code>`): for those who may check in, a card
  (asked with `peek`) shows the person, their ticket and whether they are in, with a "Check in"
  button (locks while it runs; then green with Undo). Nothing happens by itself: a link can be
  opened by accident or by a link preview. The check-in screen's camera still checks in at once; the code leaves the address at once
  (`location.replace`), never shows on screen. Signed in without the right: the event only. Not
  signed in: `https://<domain>/events/<link>`.
- **Profiles.** Opened from a group (`#/u/<npub>/in/<group>`): "In <group>" with the role there,
  a radio list for that group's admins and the belfry's (Member / Admin, or Attendee / Co-host /
  Steward for an event; the creator is "Host", read only) and "Remove from <group>" (a 9001,
  confirmed, through `/group-roles` "removed"). The belfry-wide "Role in <community>" now shows to members too (read only); admins
  get the picker and "Edit roles…" (→ Belfry settings → Roles).
- **People lists.** Channel members have no label chips any more: face, name, the role (or guest /
  place) as the second line; tap → the profile with that channel. The Members screen has an
  "Invite someone" button (person-plus) for whoever may invite members (`mayInviteMembers`),
  opening the invite sheet on "Joins as: Member" in #general (else the first open channel in the
  admins' order).

## Public events for guests

`GET https://belfry.<domain>/api/v1/events?visibility=public&from=<unix>&cancelled=1` (no auth;
2026-11-16) → `{"events": [...]}`, each exactly what `GET /api/v1/events/<link>` answers an
anonymous viewer (`me` null). Only `visibility` public (another value: 422 `invalid`), start or
end at or after `from` (default now − 1 day; not a number: 422 `invalid`), cancelled ones only
with `cancelled=1`, soonest first, at most 100; 503 `unavailable` when the community relay
can't be read. The same query as the apex `/events` page (events of the last 400 days). CORS as
for every `/api/*` (the chat's origin is allowed). Cached 60 s by clients.

## Open

- A zooid patch (0005) letting per-group admins moderate on the relay and listing them in 39001
  would make clients that only read 39001 see co-hosts too. Not done: a relay rebuild on every box.
