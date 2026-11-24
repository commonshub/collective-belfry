# Events, tickets, check-in

Prefix `EV`. Contracts: docs/checkin.md (roles, tickets, prices, check-in, invitations, the
website's event page), docs/sales.md §10 (tickets through the checkout), docs/requests.md
(activity), events.py docstring, work/events-design/PROTOCOL.md. Canvas: events.

## What an event is

- **EV-1** [all] Belfry is its own event tool (a Luma alternative): an event is a NIP-52 event
  with its own chat (a group), photos and people; there is no separate events app.
  (owner, 2026-11-10)
- **EV-2** [all] Every event has a short address: its slug when one is set, else its 6-character
  id. Links and pages use the slug; ids never show as text. → ui-rules.md UI-33
- **EV-3** [all] An event screen has the header at three levels (opens at half): Chat · Photos ·
  People. (owner, 2026-11-15)
- **EV-4** [apps] Calendar lists upcoming events; "Invited" lists any event with an invitation for
  me until I answer.
- **EV-5** [all] Past means now ≥ the end; an event without an end lasts one hour; an all-day
  event ends at the start of the day after its last day, in the event's time zone.
  (lead, 2026-11-20, after a member's report)
- **EV-6** [all] Events, meetings, proposals and tickets can't be made inside a private group in
  this version ("Not available in private groups yet").

## Creating and editing an event (Event form canvas, draft)

- **EV-60** [apps] Three distinct links on an event, named the same everywhere (form, event screen,
  mails): "Join online" (the call link), "Event website" (the event's own outside site), and the
  page on the community's website (`<domain>/events/<slug>`). "we need to make it clearer the
  difference between the online url … versus the official website of the event, versus the custom
  url on the public website" (owner, 2026-11-24). → Event form canvas Vo1xJ8QzZQ4PPqWbD2UmkF
- **EV-61** [apps, server] The form asks the Event website first; pasting it fills in the rest
  (cover, title, dates, place, description) from that page, fetched and cached by a belfry service,
  with "Fetching event details…" while it works; the person checks and changes anything. The Join
  online link is never taken from a site. "ask for event website at the top and automatically fetch
  details with a new service on the belfry to fetch and cache event details based on their URL by
  scraping them. It should show a loading indicator" (owner, Event form v6, 2026-11-24; boards approved v16)
- **EV-63** [apps] Saving changes to an event: the switch "Tell the N people going" (a line in the
  event's chat + an e-mail) starts ON when the date, time, place or Join online link changed, OFF
  for any other change (title, description, cover, Event website, tickets, co-hosts); the host can
  always flip it. "turn off by default unless there is a change of date time or location or online
  url" (owner, Event form canvas evform-edit-confirm, 2026-11-24)
- **EV-64** [apps, server] The Join online link is for people going only; the app shows it from 1
  hour before the start, and the server e-mails it to them 1 hour before (lead's ruling on the
  canvas's open point, 2026-11-24, to confirm with the owner).
- **EV-62** [apps] The description comes after the links that follow from Where (owner, Event form
  v6, 2026-11-24).

## Hosts and invitations

- **EV-10** [all] "Host" = the event's or meeting's maker only; co-hosts by role read "Co-host";
  belfry admins are never shown as hosts because they are admins. (owner, 2026-11-22)
- **EV-11** [all] Co-hosts are invited and answer the invitation; a host may remove someone
  before the start. A meeting's ✕ (remove) is only for its maker or an admin.
- **EV-12** [all] People invited to an event are told once (push following their Mentions switch,
  and one mail); the chat shows one system line for the invitation. → docs/checkin.md
  "Inviting people to an event"
- **EV-13** [apps] After an RSVP only the answer shows on the card; tap for details. RSVP never
  counts twice.

## Tickets and prices

- **EV-20** [all] A ticket is shown as a QR ("Show my ticket"), kept on the device; the phone
  turns the brightness up. Hosts don't see "Show my ticket" for their own event.
  (owner, 2026-11-15 / 2026-11-24)
- **EV-21** [all] A priced ticket is never self-served: joining refuses it; it is bought through
  the belfry's checkout (when an admin turned "Sell event tickets through the checkout" on) or
  settled by a host admitting the person. Belfry handles no money of its own.
  (lead, 2026-11-18 / 2026-11-19) → docs/checkin.md "Ticket prices", docs/sales.md §10
- **EV-22** [all] Prices may be dated (early-bird rows; the first current one wins), with or
  without VAT included; a ticket with only past/future dated prices is "not on sale".
- **EV-23** [all] A ticket may require another ticket (a day RSVP needs the full ticket); the
  refusal says so. → docs/checkin.md "Tickets that need another ticket"
- **EV-24** [apps] The Buy button opens the checkout of the belfry's own domain only, in the
  browser.
- **EV-25** [server] Test purchases: an order is a test exactly when its e-mail is a tester's
  proven address; a test never creates an account, never takes places or codes, and its
  admission is marked. → docs/sales.md §9

## Check-in

- **EV-30** [all] Check-in is done by hosts, co-hosts and people with the Door role; it opens at
  the event's start (to confirm: the owner was asked "at the start vs 15 minutes before").
- **EV-31** [all] Scanning a ticket shows a card with a Check in button; nothing is checked in
  automatically. Undo is possible.

## Public pages

- **EV-40** [all] A public event has a public page `https://<domain>/events/<slug>` (with .ics,
  cover and link previews); a members-only event's page says so and leads to sign-in.
- **EV-41** [all] The community's own website may render the event page itself and use the
  belfry's API for the chat, photos, links and participants; a person is shown by name, picture
  and role only, never a key. (owner via osv, 2026-11-20) → docs/website-accounts.md §6b
- **EV-42** [all] An event's public activity shows a person's name only with their consent
  (`public_name`); never amounts, never test orders. → docs/requests.md §5
- **EV-43** [all] "The entire flow needs to happen on the website with the ability to chat or
  upload pictures from the website" (owner via osv, 2026-11-19): RSVP, tickets, sign-up by
  e-mail, chat and photos on the event's page. (canvas drafts events/public-page; partly built:
  to confirm what is still open.)

## Promises

- **EV-50** [server] A checkout that offers "erase my data after the event" must have the
  export-and-erase run built before that event ends (OSV 2027: before 5 Feb 2027). (lead,
  2026-11-18; to confirm whether built)
