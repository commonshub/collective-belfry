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

### Inviting a member to co-host (2026-11-21)

A host invites someone already in the community to co-host without sending a link
(`cohost.py`). Co-host **links** (`POST /channel-invites {"channel": "ev-<d>", "role": "host"}`,
guests.py) stay for people who aren't members yet: a guest is told to use one.

All NIP-98, on belfry.<domain>/api/v1, the belfry's own register only (with Odoo 409
`unsupported`). CORS: the chat's host (and approved apps) only, never the apex (published
websites) nor preview.<domain> (`discovery._chat_only`).

- `POST /events/<d>/cohost-invites {"pubkey": "<hex>"}` by a host: the creator, any admin of
  `ev-<d>` (relay list or by role) or a belfry admin. A steward can't. → `{"invite": {"id",
  "by": {"name", "slug", "picture"}, "at", "state": "pending"}}`; while that invitation is
  pending, asking again answers the same one with `"already": true` (and sends no new push). A
  declined, accepted-then-removed or lapsed invitation is replaced by a new one (new `id`).
  The target needs an active pass that is not a guest's or an agent's. Errors: 403
  `not_allowed`, 404 `unknown` (no such event), 409 `already_host` (the creator or a co-host
  already, or yourself), 409 `not_member` (a guest: `"guest": true`, send a co-host link; an
  agent; nobody known), 409 `event_over`, 409 `cancelled`, 409 `archived`, 422 `invalid`.
- Push type `cohost_invite` to the invitee's phones (docs/push.md §4): title "<Inviter> invited
  you to co-host", body = the event's title, `channel` = `ev-<d>`, `url` = the event in the chat.
  Not sent when the person set `ev-<d>` to `off` in their notification settings. No e-mail.
- `POST /events/<d>/cohost-invites/answer {"accept": true | false}` by the invitee. Accept →
  `{"state": "accepted", "host": true}` after the same 9000 a co-host link publishes (by the
  groups key: `["h", "ev-<d>"] ["p", <key>, "admin"] ["invited-by", <inviter>]`,
  `guests._make_host`). Decline → `{"state": "declined"}`, nothing on the relay. Errors: 404
  `no_invite` (nothing pending), 409 `event_over`; on accept also 409 `cancelled`, `archived`,
  `not_member`, 503 `relay`. "Ignore" is the app's own: nothing is sent.
- `POST /events/<d>/cohost-invites/withdraw {"pubkey"}` by a host → `{"withdrawn": true}` (the
  invitation is deleted); 404 `no_invite`.
- `GET /events/<link>`, in `me`: `cohost_invite` = `{"id", "by", "at"}` while the viewer has a
  pending invitation, else null; for hosts also `cohost_invites` = `[{"pubkey", "person": {"name",
  "slug", "picture"}, "by", "state": "pending" | "declined" | "accepted", "at"}]` (`at` = when
  answered, else when sent; `pubkey` is for withdrawing, never to be shown).

Storage: register table `cohost_invites`, one row per (event `d`, key). A pending invitation
lapses when the event is over or 30 days after it was sent: it reads as gone (not listed, not
answerable) and may be sent again.

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
with `cancelled=1`, soonest first, at most 100; 503 `relay` (with `Retry-After: 5`) when a relay
can't be read, here and for `GET /api/v1/events/<link>`: never an empty list or a "no such
event" for what could not be read (docs/website-accounts.md "Retries"). The same query as the apex `/events` page (events of the last 400 days). CORS as
for every `/api/*` (the chat's origin is allowed). Cached 60 s by clients.

## Open

- A zooid patch (0005) letting per-group admins moderate on the relay and listing them in 39001
  would make clients that only read 39001 see co-hosts too. Not done: a relay rebuild on every box.

## Ticket prices

A ticket type (kind 30402) may carry NIP-99's price tag: `["price", "<amount>", "<currency>"]`.
The amount is a decimal string (dot, at most two decimals, above zero); the currency is an ISO
4217 code (`EUR`), `HOUR` for hours, or the host's own label (1–12 letters, digits, spaces).
No tag, or an amount of zero, means free. A tag that can't be read still counts as priced.

A priced ticket is never self-served. `POST /events/<d>/join` answers 409 `payment`; the person
asks through the application path (`/apply`, or a member's own 9021 with the `ticket` tag), and a
host admitting them (their own 9000, or `/decide`) is the host saying the price is settled: that
9000 carries `["paid", "paid"]`. `GET /events/<link>` gives each ticket `price` (`{amount,
currency}` or null) and `needs_payment`. The public page shows the price instead of "Free".

Belfry takes the money for a ticket only when the belfry's admins turned on "Sell event tickets
through the checkout" (docs/sales.md §10; off by default) and the price is in the sales currency
(`EUR`): the ticket is then bought with `POST /api/v1/checkout {"ticket": "<the ticket's d
tag>", …}`, by card or by bank transfer (against an invoice when the belfry has an Odoo
connected, else with Belfry's own receipt: docs/sales.md §11), and the paid order admits the buyer with the same
9000 (`["paid", "paid"]`, `["by", "checkout"]`, by the groups key; `["paid", "offer"]` when an
approved request covered all of the price, docs/sales.md §10). Otherwise (the switch off,
a price in hours or in an own label, a `closed` ticket) the person and the host settle it
between them as above. Each ticket in `GET /events/<link>` says which it is:

- `left`: places left (null when unlimited), counting the checkout's unpaid orders that hold one;
- `can_pay`: this viewer could buy it through the checkout now (without a viewer: anyone could);
- `checkout_url`: the page where it is bought, when the admins set one and `can_pay` is true
  (else null);
- `needs_payment`: unchanged (the ticket has a price).

`/join` on a priced ticket still answers 409 `payment`, with `"checkout": true` in the body
when `can_pay` is true for the caller. On the public page a ticket that can be bought links to
its `checkout_url`. A tester's ticket (a test order; its 9000 carries `["test", "1"]`) takes no
place.

### VAT and prices for a while

- `["vat", "<rate>"]` or `["vat", "<rate>", "included"]`: the VAT on a price in money (an ISO
  currency; ignored for hours and own labels). Without `included` the price is before VAT and is
  shown as "€50 + 21% VAT"; with it, "€50 incl. 21% VAT". No `vat` tag = no VAT stated.
- `["price_at", "<amount>", "<currency>", "<from>", "<until>"]`, any number of them: a price for
  a while. `from` / `until` are unix seconds, either may be empty. The first `price_at` (in tag
  order) whose time it is gives the current price; otherwise the plain `price` tag does. A ticket
  with `price_at` tags only, none of them current, is not on sale: `/join` and `/apply` answer
  409 `not_on_sale`. An unreadable `price_at` makes the ticket priced with no readable amount.
- `GET /events/<link>`: a ticket's `price` is `{amount, currency, vat, vat_included, until}` (the
  current one; `until` = when it ends or the next one starts, or null), `prices` lists every row
  (`{amount, currency, from, until}`), `on_sale` says whether it can be asked for now.

## Tickets that need another ticket (2026-11-20)

A ticket type (kind 30402) may carry **one** tag

```
["requires", "<event>", "<ticket>"?]
```

- `<event>`: the required event, as its `a` coordinate (`31922:<pubkey>:<d>` or
  `31923:<pubkey>:<d>`) or as its d alone. A coordinate must be the event's real one (its kind
  and its creator), else the tag can't be read.
- `<ticket>` (optional): one ticket type of that event, as its d (`<event d>:<id>`) or its id.
  Without it any ticket of the event will do.

**Meaning.** Only a key that holds that ticket **now** may take this one: admitted to the
required event with a ticket (the group's 9000 with the `ticket` tag) and not taken out since
(a later 9001). An RSVP alone is not a ticket. An admission made by a tester's test order
(`["test", "1"]`) counts only while its holder still is a tester. The check is made when the
ticket is handed out, not afterwards: someone who loses the required ticket later keeps this one.

**Unreadable = nobody.** A tag with a value that is neither a coordinate nor a d, a coordinate
that is not the event's, a ticket that is not one of that event's (or was deleted), an event
that can't be found, or more than one `requires` tag on the ticket: nobody qualifies.

**Where it is enforced** (409 `requires`, with `event_link`, `event_title` and, when the tag
names a ticket, `ticket_title`, so a page can say which ticket is needed; all three are absent
or null for an unreadable tag):

- `POST /events/<d>/join` (a free ticket taken directly), for the caller's account. A key
  nobody knows holds nothing: it is refused before any guest is made.
- `POST /events/<d>/apply`, also for the waitlist.
- `POST /checkout {"ticket"}` (docs/sales.md §10), for the account the order is on: the
  signed-in account, or the tester a test order is for. A browser key with no account is
  refused: the buyer logs in first (docs/website-accounts.md).
- `POST /offers {"ticket"}` (a request for the ticket in kind or sponsored), for the same
  account; and again at the checkout that uses the approval.

Not enforced: a host admitting someone themselves (their own 9000 on the relay, or `/decide`):
that is the host's word, as for a price. Places count as for any ticket.

**`GET /events/<link>`.** Each ticket gains `requires`: `null`, or `{"event_link",
"event_title", "ticket"?, "ticket_title"?}` (`event_link`: the required event's address, its
slug or its id; `ticket`: the required ticket's d; `{"event_link": null, "event_title": null}`
for an unreadable tag). When the request is signed (NIP-98, also by a website session, which
reads as its account) each ticket also gains `eligible`: `true` when it needs nothing or the
viewer holds what it needs. `can_pay` is false for a viewer who is not eligible.

## The website's event page

The event's conversation, photos, links and participants for the website
(`GET|POST /events/<link>/chat`, `/photos`, `/links`, `/participants`): docs/website-accounts.md §6b.

When the community's published website has the file `/events/_.html`, `https://<domain>/events/<link>`
answers with that file (status 200) instead of the belfry's own page, for an event that exists
and is public or by link. The belfry puts the event's link-preview tags (`og:title`,
`og:description`, `og:image`, canonical, the calendar link, `noindex` for a by-link event) in
the file's `<head>`; the page reads the link from the address and asks
`GET /api/v1/events/<link>` (signed by a website session when there is one,
docs/website-accounts.md). Still the belfry's: `/events` (the list), `/events/<link>.ics`,
`/events/<link>/cover`, the redirect from an event's id or old name to its address, an unknown
event (404), a members-only event (the note that sends to the chat). Removing the file from the
website gives the belfry's page back. On `preview.<domain>` a proposed site shows its own
`/events/_.html` through the website's general rule (docs/website-accounts.md), without the tags.

`me` / `viewer` of `GET /events/<link>` also carries `ticket_title`: the title of the ticket
the viewer holds (`"Ticket"` when that ticket is gone, `null` without one).
