# Plan: follow-ups from the user test (2026-10-26)

Draft. Nothing here is built yet. Split between the two sessions: see "Who builds what".

## A. Fixes to the sign-up journey (small, first)

### A1. The invite link breaks after "Continue to payment"

Cause (`odoo/addons/belfry_pass/controllers/invite.py`, `_checkout`): submitting the form marks
the invite `used` at once, before payment. A refresh, the back button or a failed card attempt
then shows "This invite link has already been used".

Fix: the invite is used only when the pass is paid.
- Submit sets the invite to a new state `reserved` (the pending pass and order on it).
- Opening a `reserved` invite shows "You started signing up as <name>": **Continue to payment**
  (the invoice's payment link) and **Start over** (cancels the pending order and pass, back to
  the form). No personal data is shown beyond the first name, since anyone with the link sees it.
- "Start over" is offered only while the order is unpaid (anyone with the link can press it; the
  link is the credential). Paid but not yet active (a few seconds after the provider's webhook):
  "Payment received, setting up your pass" instead.
- Activation sets `used`. A reserved invite does not expire while its order is open. A daily job
  cancels orders unpaid after `INVITE_RESERVE_DAYS` (default 7) and reopens the invite, but first
  asks the provider about the order's transactions: Mollie can report a bank transfer or a late
  SEPA payment as paid after a week, so a pending or paid transaction keeps the reservation.
- Participants' invite slots (`belfry_friends.py`) count reserved invites as taken.

### A2. "Join with my own Nostr key" is intimidating

Move it into a closed "For Nostr users (advanced)" section at the bottom of the form. The text
says: skip this if the word Nostr is new to you; you get an account either way and can bring your
own key later. Nothing changes in the logic.

### A3. The paid invoice says "Proforma Invoice"

Cause: Odoo 19 shows a proforma PDF until the invoice's legal PDF is generated, which happens
only when the invoice is sent (Send & Print). Belfry posts the invoice but never sends it.

Fix: when the payment is registered (activation), Odoo generates the invoice's legal PDF without
any of `account.move.send`'s own mail (generate only), and the activation mail carries it as an
attachment. One mail per payment; refunds and re-sends of the sign-in link do not mail invoices
again. The portal and the download then show "Invoice".

### A4. No VAT on the invoice

The pass products were created without taxes (VAT was left open, §3 known gaps). Needs a
decision before building (see Questions): rate (21% standard for admission to events in Belgium,
6% for some cultural events), and whether the prices are VAT-included (participants pay
€50/€100/€200) or VAT is added on top. Then: the tax on the three products, `price_include`
accordingly, the company VAT number on the invoice, a VAT field for company buyers on the invite
form. Place of supply for event admission is where the event happens, so Belgian VAT applies to
foreign companies too (no reverse charge).

### A5. "How do I continue after paying?"

Today Odoo shows the invoice after payment and the next step is in a mail.

Fix: after payment Odoo sends the browser to a Belfry welcome page,
`pass.<domain>/welcome/<claim token>`:
- "Payment received, your pass is being set up" while activation runs (polls, a few seconds),
- then **Open the apps**, which signs this browser in directly with a second, short-lived
  one-time link minted for the welcome page (the mail's link stays unused, so it still works on
  another device; the smoke test checks that a used link is refused), and below it "Add the pass
  to a wallet" and "Show your pass". The claim token is known only to the payer's browser and the
  mail.
- Getting there: Odoo 19 lands on `/payment/status` and then on the portal invoice, so the
  override is the payment status page's landing URL for transactions of a Belfry pass invoice,
  not only the portal page.
- The mail still arrives, for another device or later.

### A6. From the mail to the apps takes too many clicks

Today: mail link → Authentik's "Continue" (kept: it stops mail scanners from using the link) →
Authentik's user portal → the participant has to find `apps.<domain>` → "Sign in with Open Source
Village" → an app.

Fix:
- The sign-in flow ends at the apps page instead of Authentik's portal, at Keycast's sign-in start
  (`apps.<domain>/api/auth/oidc/start?return_to=/`), which with an Authentik session signs in
  without a page. The same for the magic link from the login page when no app asked for it.
  How, to verify on authentik 2026.8 first: the brand's `default_application` redirects only
  external users, and participants are created as internal users (the onboarding service sets no
  `type`). Either make participants external (also hides the user portal, which they do not need;
  check the magic-link and pass-bridge flows for external users), or end the sign-in-link and
  magic-link flows with a redirect stage that applies only when no app is waiting (no `next`).
- The mail has one button, "Open Open Source Village", instead of three numbered steps; the
  wallet and the tokens move to the apps page and the welcome page.
- Result: mail → Continue → the apps page, signed in. Two clicks.

## B. Application form

Public page `<domain>/apply` (onboarding service, next to the feedback form): name, e-mail,
motivation, "I want to volunteer" checkbox, privacy note; honeypot and rate limit like feedback.
- Odoo `belfry.application` (name, e-mail, motivation, kind visitor/volunteer, state new /
  invited / declined, steward, notes). Confirmation mail to the applicant; a mail to the stewards.
- Steward pages: an "Applications" section. **Invite** creates an invite with the applicant's
  e-mail as recipient and mails the invite link (visitor) or the volunteer invite (B→C).
  **Decline** sends a short friendly mail (text in code). Duplicates by e-mail are flagged.
- The start page says: ask someone in the community for an invite, or apply here.

## C. Volunteers

A volunteer pass: tier `volunteer`, two weeks (dates set per volunteer by the steward, default the
event's two weeks), price 0, earned by signing up for at least `VOLUNTEER_MIN_SHIFTS` (3) shifts.

Shifts are kept in Odoo, not on the public relay: a public NIP-52 event is readable by every
participant, and shifts need capacity and a count that decides a pass, which a relay cannot
enforce.
- Odoo `belfry.shift` (title, task, start, end, place, capacity, notes, lead) and
  `belfry.shift.signup` (shift, partner, state). Sign-up refused when full or overlapping.
- Volunteer journey: steward approves a volunteer application → the volunteer gets an invite to
  the volunteer page → signs in by e-mail (Authentik group `belfry-volunteers`) → picks shifts at
  `pass.<domain>/shifts` → at the third shift the volunteer pass activates through the usual
  activation (no invoice; marked "granted" by the shift rule, like a steward's mark paid) → the
  normal welcome and apps follow.
- Leaving shifts: allowed until `VOLUNTEER_CHANGE_HOURS` before the shift; dropping below the
  minimum after activation alerts the stewards (no automatic revoke).
- Who sees what: volunteers and stewards see all shifts with who is signed up; visitors never.
  In the programme app, active volunteers and stewards get a "Shifts" tab once the volunteer pass
  is active (no Nostr key before that; until then `pass.<domain>/shifts` carries it). It reads the
  village server's new `/api/shifts`, which checks NIP-98 like `/api/participants` and asks the
  onboarding service with `PASS_INFO_TOKEN`; onboarding answers for the pass's partner with the
  volunteer or steward role from Odoo. One auth path, no CSP change. A volunteer's shifts appear in
  "Yours" and in the .ics export. Stewards manage shifts (create, copy a day, move people) on the steward pages.
- Optional later: mirror shifts as events in a closed "Volunteers" group (D), where the relay
  enforces membership on reads.

## D. Groups app like Flotilla

Flotilla (Coracle's author) is built on NIP-29 groups, which need a relay that runs the groups.
rnostr, the community relay, has no NIP-29 (why Flotilla was ruled out in §3o).
- Add a second relay for groups at `groups.<domain>`: zooid (Coracle's NIP-29 relay, the one
  Flotilla is made for) or khatru/relay29. Access: NIP-42 AUTH, members = active passes, synced from
  Odoo like rnostr's whitelist; group admins = stewards plus whoever creates a group.
- Flotilla at `spaces.<domain>`/`groups.<domain>` as a static build like the chat: patches for
  "Sign in with Open Source Village" (shared with the chat's Coracle patch), the app bar, palette,
  the village relay as the one space.
- Starter groups made by bootstrap: Announcements (stewards post), Volunteers (volunteers and
  stewards, closed), Help desk.
- Spike first (one day): does zooid run behind Caddy with NIP-42 and a member list we can sync,
  does Flotilla sign through Keycast (NIP-29 kinds 9, 10, 11, 12, 9000–9022, 39000–39003 in the
  `belfry-event` policy), does Flotilla's push/notification server need to be hosted.

## Order

1. A1, A2, A3, A5, A6 (one or two days, deploy, re-test with the testers).
2. A4 after the VAT decision.
3. B (applications), then C (volunteers), which builds on B.
4. D spike, then D.

## Who builds what

Agreed with the other session (collective-belfry-fc) on 2026-10-26:
- This session: A (journey fixes), B (applications), C's Odoo models, shift sign-up page and
  steward pages, and the onboarding API for shifts.
- The other session: D (groups spike, then the groups relay and Flotilla; zooid and Flotilla share
  Coracle's welshman code, so the chat's SSO patch should port), and C's "Shifts" tab in the
  programme app with the village server's `/api/shifts`.
- The D spike checks zooid's access config (whitelist or roles, rendered from active passes like
  rnostr's), AUTH behind Caddy, the kinds Flotilla signs (added to `belfry-event` only after the
  spike), and whether Flotilla needs its push or notification backend.
- Neither session starts before the user says go.

## Questions for the user

1. VAT: 21% or 6%, prices including VAT or VAT on top?
2. Volunteers: do they pay nothing, and are the two weeks the same for everyone?
3. Applications: who sees them (all stewards), and does an approved visitor still pay?
4. Groups: who may create groups, everyone or stewards only?
