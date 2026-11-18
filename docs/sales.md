# Selling passes

A belfry on its own register can sell passes: products with dated prices, codes that take a
percentage off, a checkout that pays by card (Stripe) or by bank transfer against an invoice, and
invoices kept in an Odoo the community already has (Odoo Online works: only standard records over
its JSON-2 API, no module to install). Decided by Xavier on 2026-11-17 for Open Source Village
2027; first user: opensourcevillage.org.

Status: **built** (2026-11-17, `sales.py`, `salesodoo.py`, `salesstripe.py`, `salespay.py`,
`salesconsole.py`; tests `tests/test_sales*.py`). Tested against fakes only: the real Odoo
Online, Stripe and VIES were never called (see §8). Nothing takes money until an admin has
entered the Odoo connection, the bank details and the Stripe key in the console.

## 1. Pieces

| Piece | Where | Needs |
|---|---|---|
| Products, prices, codes, orders | Belfry's register | nothing |
| Invoices, VAT, payments, credit notes | the community's Odoo (JSON-2) | Odoo connection |
| Card payments | Stripe Checkout, the community's own account | Stripe key + webhook secret |
| Bank transfers | `/pay` page + Odoo's bank reconciliation | bank details, Odoo connection |

A product with a price can only be sold when the Odoo connection is set (invoice numbers and VAT
live there). Free products and codes worth 100% work with nothing set.

## 2. Settings (console, admins only): belfry.<domain>/sales and belfry.<domain>/plugins

Since 2026-11-18 the **connections** (Odoo with its picks, Stripe) are on the **Plugins** page
(`plugins.py`; Stripe, Odoo and E-mail, each its own page), each with a **Production** and a
**Test** section (§9). `/sales` keeps the bank, products, codes, offers and orders, and links to
Plugins. What was saved before keeps working unchanged as Production (same params and secret
names; no migration).

Secrets are typed into the console, never sent in chat or shown again (only "set" / "not set"
and the last 4 characters); an admin can clear one.

Where they are kept: the non-secret settings in the register (params `belfry.sales.settings`
and `belfry.sales.products`), the secrets (Odoo API key, Stripe key, webhook secret) in
`sales-secrets.json` next to the register (mode 600, written atomically by the service; never
in an answer, a log line, an error text or `register-import`). Both live in the `register-data`
volume, so they are in the (encrypted) backups. Where the console left a field empty, the
server's `ACCOUNTING_*` / `STRIPE_WEBHOOK_SECRET` settings are used instead. The Status page and
`belfry status` say only whether paid passes can be sold and which of Odoo / bank / Stripe are
set.

- **Odoo**: URL, database, API key → "Check" (reads the companies the key sees; nothing is
  saved when it fails). Then pick: company, the product each Belfry product is invoiced as, the
  VAT tax with the price including it (people) and the one on top (organisations), the journal
  for card payments and the one for payments marked by hand (empty: not booked, the order says
  so), and whether Odoo also mails its invoice PDF. The lists come from Odoo.
- **Bank**: account holder, IBAN (checked mod 97), BIC.
- **Stripe**: restricted secret key, webhook signing secret. The page shows the webhook URL to
  enter at Stripe (`https://belfry.<domain>/hooks/stripe`, and `…/hooks/stripe/test` for the
  Test section) and the events to select. Production refuses a test key (`sk_test_` /
  `rk_test_`), Test refuses a live one (`sk_live_` / `rk_live_`), with a fixed message.
- **Products**: id (slug), name, description, pass tier, role, valid from / until (the pass's
  dates), prices `[{until?: date, amount}]` in order (the first whose `until` hasn't passed
  applies; amount = price excluding VAT for organisations, including VAT for people — the same
  number, as belfry_pass does), VAT rate, how many exist (optional), on sale yes / no.
- **Codes**: make codes (percent 1–100, product or any, uses, expires, note; N at once), see
  who used them, revoke.
- **Offers** (in kind) and **Orders**: lists with their state; an order can be cancelled or
  marked paid by hand (reason required, the admin's name recorded).
- **Waiting for a transfer**, at the top of the orders: every order in state `invoiced` that
  isn't paid, the one whose hold ends soonest first, with the buyer's name, the amount, the
  invoice number and communication, `hold_until` and "N days left" (whole days, rounded up),
  and the same "Mark paid" action (reason required) right there.
- An order whose buyer asked `erase_after_event` says so in its state column; once its pass is
  active and the request is on the contact, "Clear the erase request" (reason required) clears
  it on the contact (`belfry_erase_after_event_cleared` keeps who, why and when).

## 3. Who is buying: a key made in the browser

Like Luma, the buyer types an e-mail and isn't asked to confirm it during checkout. The page
makes a key in the browser and signs every call with it (NIP-98). There are no unsigned writes.

- The browser key is a **session key**, not the person's identity: when the pass becomes
  active, Belfry creates the account in the signer as for every member and binds that key as its
  first session (the mechanism guest links use: `create_bunker(client_pubkey=…)`). The order's
  state answer then carries `bunker_url`, so the page that paid is signed in.
- The key stays in that browser's storage. Losing it loses nothing: the e-mail gets in.
- The account's e-mail is **unverified** until its owner proves it: by the "Confirm your
  e-mail" link in the receipt, or by the first sign-in with an e-mail code (which is the same
  proof). An unverified account has **one** address: a later order signed by that account must
  use it (else 409 `email_mismatch`). A confirmation link is bound to its contact and its
  address, is only ever mailed to that address, and proves nothing else. Its page shows what was
  bought, for how much and when, with "Yes, this was me" and "This wasn't me" (which ends the
  account's sessions, drops the address and flags the order for the admins). Until then:
  - it holds the pass, shows its ticket, and uses the chat from the browser that paid;
  - it is not found by e-mail anywhere (invites, "already a member", member lookups), has no
    username of its choice and no mailbox, cannot invite, cannot be made admin;
  - mails to the address are only the receipt, the invoice and the confirmation.
- If the address already belongs to an account, the order is still taken under a new unverified
  contact (the checkout must not reveal who is a member). On confirmation the pass moves to the
  existing account and the temporary one is closed, as a saved guest's does (guests.md).
- A member or guest who is signed in signs with their own session: the pass goes on their
  account, nothing unverified.
- **A free product without a code** makes no account from nothing: the order waits in state
  `confirm` (48 hours) for the link mailed to the address; confirming it opens the pass on an
  account whose address is proven (or on the account that already has that address). No
  browser session is bound in that case (`bunker_url` stays empty: whoever clicks the mail may
  not hold the browser); the buyer signs in with an e-mail code. A 100% code still opens the
  pass at once: the code is the gate.
- An order nobody pays leaves no account: the contact is made only when the pass opens.
- Settled while building: the unproven address is kept in `belfry_pending_email`, never in
  `email`, so every lookup by e-mail misses it by construction; the receipt's link shows a
  button and only its POST confirms (mail scanners open links); the sign-in by e-mail code
  (connect.py, pass.<domain>/me/login) is the one place that looks pending addresses up. Until
  then the account keeps its member number as username, and the sign-in by name@domain
  (sessions.py) answers `no_email`. A signed-in guest without an address gets the typed one as
  pending on their own contact.

## 4. API (https://belfry.<domain>/api/v1, CORS open to the apex)

CORS (discovery.py, `api_cors` middleware; not Caddy): the apex `https://<domain>` (and
`chat.<domain>`, `API_CORS_ORIGINS`, approved apps) may call every `/api/*` path. The website's
previews, `https://preview.<domain>`, show anyone's draft site, so they get CORS **only** on the
buyer's sales endpoints: `GET /products`, `POST /codes/check`, `POST /checkout`,
`GET /checkout/<order>`, `POST /checkout/<order>/pay`, `GET /billing`, `POST /offers`,
`GET /offers/<id>` (and their preflights). Same methods and headers as the apex
(`GET, POST, PUT`; `Authorization, Content-Type`), never `Access-Control-Allow-Credentials`:
these calls are signed per request (NIP-98) by the page's own key, no cookie is involved.
`POST /codes` and `POST /offers/<id>/decide` (admins) and every other endpoint refuse the
preview's preflight (403) and answer without CORS headers.

Unknown fields in a request body are ignored (`/checkout`, `/offers` and the others read only
the fields named here); a body that isn't a JSON object is 400 `invalid_json`.

`GET /products` — no auth.

```json
{"currency": "EUR", "can_sell": true, "products": [
  {"id": "village-2027", "name": "Two-week pass", "description": "…", "tier": "participant",
   "valid_from": "2027-06-01", "valid_until": "2027-06-14",
   "price": {"amount": 200.0, "vat_rate": 21, "until": "2026-12-31"},
   "next_price": {"amount": 300.0, "from": "2027-01-01"}, "left": 180}]}
```

`POST /codes/check` — NIP-98. `{"product", "code"}` → `{"percent": 50, "left": 3}` or 404
`unknown_code`. 10 an hour per key.

`POST /checkout` — NIP-98 (any key).

```json
{"product": "village-2027", "name": "Ada", "email": "ada@example.org", "code": "…",
 "pay": "card" | "invoice",
 "billing": {"kind": "person" | "organisation", "name": "…", "vat": "BE0…", "street": "…",
             "street2": "…", "zip": "…", "city": "…", "region": "…", "country": "BE",
             "invoice_email": "…"},
 "return_url": "https://<domain>/tickets/thanks",
 "erase_after_event": false}
```

→ 201

```json
{"order": "o_…", "state": "confirm" | "pending" | "invoiced" | "active",
 "total": {"net": 165.29, "vat": 34.71, "gross": 200.0, "percent_off": 0},
 "invoice": {"number": "INV/2026/0042", "communication": "+++123/4567/89012+++", "url": "…"},
 "card_url": "https://checkout.stripe.com/…",
 "pay_url": "https://belfry.<domain>/pay/o_…",
 "hold_until": "2026-11-24T16:00:00Z",
 "erase_after_event": false}
```

- `erase_after_event` (optional, default `false`): the buyer ticked "Send me all my data and
  erase it all from your server as soon as the event is over". It must be a JSON boolean when
  present (`"true"`, `1`, `null`… → 422 `invalid`). Kept on the order; when the pass opens it is
  copied onto the person's contact: `belfry_erase_after_event: true`,
  `belfry_erase_after_event_order` (the order id) and `belfry_erase_after_event_at` (when it was
  asked). Once true it stays true: a later order without it never clears it, only an admin
  does (console). Asking again for the same open order with `true` adds it; `false` never takes
  it back. A code that answers an offer in kind that asked it carries it to the order. When the
  pass moves to the existing account of the same address, the request goes along. The answer
  (and `GET /checkout/<order>`) always carries it. **Only recorded: the run that exports the
  person's data to them and erases it after the event is not built yet.**

- `return_url` must be `https://` with the hostname exactly the belfry's domain or a subdomain
  of it, no port, user info, query, fragment, backslash, whitespace or control characters;
  Belfry rebuilds it from its parts. Anything else is ignored and Belfry's own thanks page
  (`belfry.<domain>/sales/thanks`) is used. Belfry adds only `?order=<id>`.
- `invoice.url` is Belfry's own page of the order, `https://belfry.<domain>/pay/<order>`
  (number, amount, until when the place is held, how to pay). Never a link into Odoo. The
  invoice PDF is fetched from Odoo by Belfry and attached to Belfry's own mail to the order's
  address (or `invoice_email`); Odoo itself sends nothing.
- `pay` defaults to `card`. The answer also carries `product`, and `bunker_url` once active (to
  the key that made the order).
- Odoo's invoice is the truth for the amounts: after the invoice is posted, `total` is Odoo's
  (rounding, a fiscal position); when it differs from Belfry's own sum the order is flagged
  for the admins.
- Total 0 with a code (100%), or a free product bought by a signed-in member with a proven
  address: state `active` at once, no invoice, no payment. A free product without a code from
  anyone else: state `confirm` until the mailed link is confirmed (see §3).
- **An invoice is made only once it is needed** (Xavier, 2026-11-17: "otherwise we will create
  too many invoices that will never be paid"):
  - `pay: card` → no invoice at checkout: `invoice` is `null` until paid. `card_url` is a
    Stripe Checkout session made from the order (Belfry's own total in cents, the order id in
    `client_reference_id` and the metadata, the product's name as the line). Once paid: the
    pass opens, then the invoice is made and posted in Odoo, the payment registered on it, and
    the receipt carries its PDF. When Odoo's total differs from what was paid, the order is
    flagged `odoo_total`; the buyer is never asked again. An unpaid card order that ends has no
    invoice, so no credit note.
  - `pay: invoice` → the invoice is made at checkout and mailed with its PDF and the link to
    `/pay/<order>` (transfer details).
  - `pay_url` (`/pay/<order>`) is returned whenever bank details are set. For a card order
    without an invoice it shows "Pay by transfer instead: get the invoice", which makes the
    invoice then.
- **Changing how to pay** an open order: `POST /checkout/<order>/pay {"pay": "invoice" |
  "card"}` (NIP-98, the order's key) → the order's answer. card → invoice makes the invoice
  then (a new card session follows for the invoice's amount). invoice → card makes a card
  session for the existing invoice's amount; the payment goes on that invoice. Asking
  `POST /checkout` again for the same open order with another `pay` does the same. Errors:
  409 `not_open`, `no_transfer` (no bank details), `no_card` (no Stripe key).
- **Billing** (all fields optional unless said; whitespace collapsed; too long →
  422 `invalid_billing`): `name` ≤ 120, `street` ≤ 200, `street2` ≤ 200, `zip` ≤ 20 (optional
  for every country, people and organisations alike: no country rule), `city` ≤ 100,
  `region` ≤ 100 (free text: a state, province, county…), `country` = two letters (any ISO
  3166 alpha-2 code; not checked against a list), `invoice_email` an address ≤ 254. An
  organisation needs `name`, `country` and `city` (422 `invalid_billing`), as before.
  `GET /billing` returns these same fields, `street2` and `region` included.
- **`vat`** — what it means depends on the billing country:
  - an organisation in an EU country (AT BE BG CY CZ DE DK EE ES FI FR GR HR HU IE IT LT LU
    LV MT NL PL PT RO SE SI SK), or in GB with a Northern Ireland number (`XI…`): an EU VAT
    number, as before: spaces, dots and dashes dropped, upper case, two letters then 2–14
    letters or digits (else 422 `invalid_vat`), then checked on VIES before the invoice is made
    (VIES says no → 422 `invalid_vat`; VIES down → accepted and flagged `vies_down` for the
    admins). Greece's numbers start with EL (billing country GR).
  - an organisation anywhere else: its own tax id (EIN, ABN, GSTIN, UID, RFC…) kept as typed
    (whitespace collapsed): 2–32 characters, letters, digits, spaces and `. - /`, starting and
    ending with a letter or digit (else 422 `invalid_vat`, "That tax id is not valid."). Never
    sent to VIES, never `invalid_vat` for its format otherwise, and never used to find an
    existing Odoo contact.
  - a person: not used on the invoice; the old shape check still applies when one is sent.
- The invoice's contact in Odoo is never found by an e-mail address (the buyer's isn't
  proven): a person gets the Odoo contact Belfry stored on their account, else a new one; an
  organisation is matched by its VAT number only when VIES said it is valid, else a new one.
- A new contact gets `street`, `street2`, `zip`, `city`, `country_id` (by code) and, for
  `region`, the `res.country.state` of that country whose code or name is the region (any
  case) as `state_id`; no such state (or the search fails) → the region is appended to the city
  line ("Springfield, Kerala"). A missing state never fails the order.
- An organisation's number goes into the contact's `vat`. Odoo's VAT check (base_vat) may
  refuse a number it doesn't know for that country: on a refusal (Odoo 4xx) the contact is
  made with the number in `company_registry` instead, and if that is refused too, without it.
  The order records where it went (`tax_id_on`: `vat` | `company_registry` | `note`); in the
  last two cases the invoice's note (`narration`) says "Tax ID: <number>" ("VAT: …" for an EU
  number), so it prints on the invoice either way. Odoo down (no answer) is not a refusal: the
  tick retries as for every step.
- **The tax on the invoice line** is one of the two picked in the console, never chosen by
  country: a person (Belgian or not) gets "VAT for people (price includes it)" (`tax_included`),
  every organisation — Belgian, EU with a valid VIES number, or outside the EU — gets "VAT for
  organisations (on top)" (`tax_excluded`). Belfry's own total uses the product's VAT rate the
  same way. Belfry applies no reverse charge and no exemption for any buyer and sets no fiscal
  position (admission to an event is taxed where it takes place). Whether the community's Odoo
  maps the tax through an automatic fiscal position (e.g. "EU private / intra-community /
  non-EU" on the contact's country) is Odoo's setting: if it did, Odoo's total would differ and
  the order is flagged `odoo_total` (§8).
- Errors: `unknown_product`, `not_on_sale`, `sold_out`, `unknown_code`, `code_used`,
  `invalid_email`, `invalid_billing`, `invalid_vat`, `cannot_sell` (Odoo not set),
  `email_mismatch` (409: an unverified account ordering for another address), `too_many`
  (429), `accounting` (Odoo refused), `test_not_set_up` (409: a tester's paid order while the
  test environment can't sell; §9).
- Limits (`too_many`): 5 open orders per key; 5 unpaid orders per address, counting only the
  orders whose cancel link was mailed to that address (the refusal tells the address's owner to
  cancel the ones that aren't theirs with the link in the mails sent to it; a card order that
  sent no mail yet counts only toward the IP limits) and 20 orders an hour per address; 30 orders made an hour per IP, and 60 refused
  checkouts an hour per IP (wrong code, invalid VAT…) counted apart; for a product with
  limited places, 10 unpaid orders per IP; 10 code checks an hour per key. An IPv6 address
  counts as its /64; an IP is kept only as a keyed hash. At most 5 sales mails a day go to one
  address (the others wait).
- Every mail about an unpaid order (the invoice, the reminder, a free order's confirmation)
  carries "If you didn't order this, cancel it here": a page that shows the order and cancels
  it with one button (flag `not_me`). Paid orders can't be cancelled that way.
- Asking again with the same key, product and e-mail while an order is open (or became active
  in the last 24 hours: a retried request) returns that order, with 200.
- `unknown_code` is 404, `code_used` and the other refusals 409, `accounting` 502 (a
  `pay: invoice` order whose invoice Odoo refused: cancelled, its code's use given back). Odoo
  not answering is no error: the order is kept `pending` and the tick makes the invoice later.

`GET /checkout/<order>` — NIP-98 by the key that made it → the same shape with the current
state (`confirm` →) `pending` → `invoiced` → `paid` → `active`, or `cancelled` / `expired`
(an ended order that still gets paid comes back to `active`) and, once active,
`bunker_url` when the key isn't signed in yet. Asking may also retry a waiting step (at most
once every 10 seconds per order; a buyer's polling never counts as a failure) and makes a new
card session when Stripe's (24 hours) expired. Another key gets 404.

`GET /billing` — NIP-98, a verified account only → the billing details saved at its last order.

**Offers in kind are public requests** (Xavier, 2026-11-17: "be clear that those requests are
public (show a link to /requests which should look like github issues)"). They live in the
belfry's **requests channel**: a channel whose web page is on and public (webthreads.py: the
"issues" template at `<domain>/<slug>`), picked in the console (a hint says to make a channel
`requests` with the slug `requests` when there is none). No requests channel → `POST /offers`
answers 409 `no_requests_channel` and `GET /products` says `"offers": false`.

`POST /offers` — NIP-98 (any key). `{"product", "name", "email", "offer": "…", "percent",
"erase_after_event"?}` (`percent` 50–100, 50 when left out; the website's form shows 50% by
default; `erase_after_event` as for `/checkout`: a JSON boolean or 422 `invalid`, kept on the
offer, carried to the order made with the offer's code) → 201
`{"offer": "f_…", "state": "confirm", "product", "erase_after_event"}`. It goes through the requests page's own path for a thread
from outside: the confirmation mail must be answered (the address must be theirs) before
anything is public; the page's limits apply, and 3 a day per key and per IP, 3 open per key.
The form and the confirmation mail say in plain words that the request and the name will be
public; the e-mail address is never shown on the public page (members can read it in the chat when the card falls back to the groups key). Once the link is pressed: a thread titled
"<name>: <percent>% off <product> in kind" with the offer as its text, tagged `["offer", id]`,
`["product", id]`, `["percent", n]`, `["p", <the requester's key>]`, and four buttons for the
belfry's admins (docs/actions.md, `belfry:offer-decide`: 50%, 75%, 100%, Decline). Members
answer in the thread in the chat; the requester comments with their link; replies are mailed
back to them (webthreads.py).

`GET /offers/<id>` by the same key → `{"offer", "state": "confirm" | "open" | "accepted" |
"declined", "product", "erase_after_event", "url"?, "my_url"?, "percent"?}`: `url` is the public thread, `my_url` the
requester's own link (to comment), only in this answer and in their mail.

Deciding: the belfry's admins only, in the console (any percent 50–100 or no, with a note) or
with the card's buttons in the chat. A decision posts a public reply in the thread ("Approved:
75% off." or "Not this time.", with the note); the personal code goes **only** in the private
mail to the requester. That code is bound to the requester's key **and** address: the checkout
takes it from that key, or from an account proven with that address; one use, that product.
Admins over the API: `POST /offers/<id>/decide {"percent": 0|50–100, "note"}`.

`POST /codes` — NIP-98, admins. `{"percent", "product"?, "uses": 1, "count": 1, "expires"?,
"note"?}` → the codes. A sponsor paying for N passes is an order by hand in Odoo and N codes
made here (no sponsor checkout in this version).

## 5. Paying

- **Hold**: an unpaid order holds its place for **7 days** (`hold_until`), then expires: its
  invoice, if it has one, gets a credit note; the code's use is given back. One reminder is mailed 2 days before;
  it and `/pay/<order>` say until when the place is held.
- **Paid after it ended** (a card payment that comes late, a transfer reconciled after day 7):
  the order **reopens**: the pass opens, a new invoice is made when the first was credited (the
  old number and its credit note stay in the order's history; whether it was credited is read
  from Odoo, never only from Belfry's own record) or when it never had one (a card order),
  the payment is registered on it, and the order is flagged `paid_late` (and `oversold` when no place was left:
  they paid, they get the pass, the admins see it). An admin can do the same by hand: "Reopen
  and mark paid" (reason required) on an expired or cancelled order. Only the order's current
  invoice counts for closing a pass on a credit note.
- **Card**: `checkout.session.completed` (and `checkout.session.async_payment_succeeded`) at
  `https://belfry.<domain>/hooks/stripe` (signature checked with the console's webhook secret,
  deduplicated on the event id; amount, currency and reference (the order id, or the invoice's
  number when the order had one) must match the order, else it is flagged and nothing opens) →
  pass active → the invoice made (when the order had none) and the payment registered on it in
  the card journal (after the pass: Odoo being down never keeps a paid pass shut; the tick
  retries the books, and the receipt goes without the PDF if the invoice is an hour late).
  Each card session's amount is kept: when the total changed to Odoo's (a transfer invoice made
  later) a fresh session is made, and an older one still paid for its own amount is accepted and
  flagged `paid_old_amount`. The payment booked in Odoo is the amount Stripe took. A completed
  session for an order already paid (by another session, or by a transfer Odoo saw first) is
  flagged `paid_twice` ("Paid twice: refund one in Stripe") and changes nothing else; every
  payment, by any route, expires the order's other open session. A session without a reference
  is never accepted. Card orders get the same one reminder 2 days before the hold ends. Stripe refusing to make a session leaves the transfer (flag on the
  order). A payment that arrives after the order ended reopens it (above).
- **Transfer**: Belfry reads the open orders' invoices from Odoo every 15 minutes (in one call;
  when that fails, one by one, flagging the one that can't be read); when Odoo's reconciliation
  says paid → pass active. An admin can also mark an order paid by hand (reason required); an
  order paid before it had an invoice still gets one, with the payment on it.
- A step that fails (Odoo, Stripe or the signer down) is retried every 15 minutes; only the
  tick counts the failures, and a success resets the count. After 30: a paid order never gives
  up opening its pass (it goes on hourly, flagged `slow`); any other step stops, flagged
  `stuck`. The console's "Try again" (on every flagged order) starts it over. Mails are marked
  sent only once the mail provider took them (claimed first, so the tick and a request never
  both send one); retried each tick, hourly after 8 failures, given up after 3 days; with no
  mail provider yet, kept and tried hourly until one is set (3 days at most).
- **Active** = `core.activate_free_pass`-style activation with the product's tier, role and
  dates, the receipt mail (with the confirm-your-e-mail link when unverified) and the usual
  welcome.
- **Refunds**: by hand in Stripe and Odoo. When the invoice gets a full credit note
  (`payment_state` `reversed`), Belfry closes the pass at its next read.
- The pass is made with the product's tier, its dates (`valid_from`, else today; `valid_until`,
  else no end) and its role, when that role exists.

## 6. The /pay page: belfry.<domain>/pay

Transfer details for the belfry's bank account: account holder, IBAN, BIC, amount and
communication, each with a copy button, and an EPC (SEPA) QR code for banking apps (a Belgian
structured communication `+++…+++` goes in the QR's structured field, anything else in the
message field, exactly as given).

- `/pay/<order>`: an order's own page (the API's `invoice.url`): its invoice number, amount and
  until when the place is held; the bank details when they are set, the card link when there is
  one. It works without bank details too. A card order without an invoice shows "Pay by
  transfer instead: get the invoice" (a POST form carrying the page's token).
- `/pay?amount=12.50&communication=…`: a free-standing request. `amount` must be a number;
  `communication` at most 140 characters of letters, digits, spaces and `+ / - . , : ( )`;
  anything else → the page without them. A `description` is shown **only** when the link is
  signed by the belfry (`&sig=`, made with `belfry pay-link` or `POST /api/v1/pay-links` by an
  admin): Belfry never shows free text taken from an unsigned URL.
- No bank details set → `/pay?…` is 404.
- `POST /api/v1/pay-links {"amount"?, "communication"?, "description"?}` (admins) → 201
  `{"url"}`; `belfry pay-link --amount … --communication … --description …` on the server.
- Paid or ended orders show no bank details on `/pay/<order>`.

## 7. Not in this version

The export-and-erase run behind `erase_after_event` (the flag is recorded on the order and the
contact; nothing exports or erases anything yet).

Sponsor checkout (N passes in one order), partial payments, automatic refunds, other currencies
than the one in settings, Mollie, selling on a belfry that keeps its register in Odoo (it sells
through Odoo as before), the phone apps (they open the website).

## 8. Not tested against the real services

Everything above runs against fakes in the tests. Unproven until tried live:

- Odoo Online over JSON-2: the invoice PDF (`invoice_pdf_report_id`, made by
  `account.move.send.wizard` with `sending_methods: []`, read from `ir.attachment.datas`), the
  `account.payment.register` and `account.move.reversal` wizards with these contexts,
  `payment_reference` filled in at posting (needs the journal's communication standard set to
  Belgian for `+++…+++`), the company scoping of the pick-lists, `price_include` on taxes.
- Stripe: the session form fields, `Idempotency-Key`, expiring a session, the restricted key's
  permissions, the events' exact shape.
- VIES's REST answer for a number and when a member state is down.
- Odoo's refusal of a non-conforming `vat` (base_vat) coming back as a 4xx over JSON-2 (the
  fallback to `company_registry` / the note relies on it), `company_registry` existing on
  `res.partner` in that Odoo version, the note printing on the invoice PDF, `=ilike` on
  `res.country.state`, and whether an automatic fiscal position remaps the explicit
  `tax_ids` Belfry sends for a foreign contact.
- The browser key binding (Keycast patch 0022) and an EPC QR read by real banking apps.

## 9. Testing with testers: the test environment (2026-11-18)

Xavier: "for Stripe and Odoo, have two sections for test environment and production environment.
And there should be a role "tester" that should make use of the stripe test and odoo test."

**Environments.** Each order has `env`: `live` or `test`, fixed when it is made and stored on
the order (orders made before have none: live). Everything afterwards uses that order's
environment only: the Stripe key and session, the webhook, the Odoo invoice, the 15-minute tick
(each environment's open invoices read from its own Odoo), credit notes, reopening, mark paid,
the invoice PDF, mails.

| | Production (`live`) | Test (`test`) |
|---|---|---|
| Odoo URL, database, company, products, taxes, journals | param `belfry.sales.settings` (as before), `ACCOUNTING_*` fallback | param `belfry.sales.settings.test`, no fallback |
| Odoo API key | secret `odoo_api_key` (as before) | secret `test_odoo_api_key` |
| Stripe key, webhook secret | `stripe_key`, `stripe_webhook_secret` (as before), `STRIPE_*` fallback | `test_stripe_key`, `test_stripe_webhook_secret` |
| Webhook | `https://belfry.<domain>/hooks/stripe` | `https://belfry.<domain>/hooks/stripe/test` |

Shared: the currency, the bank details, the invoice reference prefix, the requests channel.
Secrets stay in `sales-secrets.json` (mode 600); the console shows set / not set and the last 4.
The test Odoo must be another URL + database than production's ("another database").

**Which orders are test orders.** An order is `test` exactly when the checkout's e-mail address
(lower-cased, trimmed) is the **proven** address (`res.partner.email`, never a pending one) of a
**tester** (docs/roles.md) who holds an active pass. Nothing in the request body or URL can ask
for test mode. A normal buyer is never affected by test settings.

**What a test order does and doesn't do** (test cards are public, so knowing a tester's address
must give nobody anything):

- It never creates a contact, never binds the signing browser key as a session, never returns
  `bunker_url`. The order's `partner_id` is always the tester's; `account` is the tester's only
  when the signing key is already an active session of the tester's account (then it behaves as
  any signed-in member's order), else `null`.
- On activation the pass opens on the tester's own account only, marked `belfry_test: true`,
  and only while that person still is a tester with that address (else the order stops,
  flagged `tester_gone`, and nothing opens). The product's role is not written (a tester's role
  never changes). Receipts and invoices go to the tester's address; every sales mail of a test
  order starts with "[TEST]" and a fixed line.
- A free product without a code waits for the confirmation link mailed to the tester's address,
  as for anyone; a 100% code opens the pass on the tester's account at once.
- The test Odoo never gets (or stores on the account) the tester's production Odoo contact
  (`belfry_odoo_partner`): each test invoice gets its own test contact.
- No places: test orders don't count in `left` / `sold_out` for real buyers, and a code's use by
  a test order goes in `test_used`, never `used` (a code still has to be valid for it).
- Paid orders need the test environment to be able to sell (test Odoo connection + picks, and
  the test Stripe key or bank details): else **409 `test_not_set_up`**, never a fall back to
  production.
- `pay: invoice` / transfer: the invoice is made in the test Odoo; "Mark paid" works as usual;
  the `/pay/<order>` page shows "Test order: no real money."

**Webhooks.** `/hooks/stripe/test` checks the test signing secret and takes only events with
`livemode: false`; `/hooks/stripe` takes `livemode: true` (and test-mode events only while
production's own key is a test key, as a belfry that tried Stripe before environments existed
had). An event for an order of the other environment answers `wrong_env` and changes nothing;
the wrong mode answers `wrong_mode`. Test events are deduplicated apart (`test:<id>`).

**API.** Every order answer (`POST /checkout`, `GET /checkout/<order>`, …) carries `"test":
true | false`: the buying page shows a banner when true. New error: 409 `test_not_set_up`.
Testers: 409 `tester` on `POST /admins` and `POST /member-roles`; `POST /whois` answers
`"testers"`.

**Console.** `/sales` lists test orders apart ("Test orders", a TEST mark) and products show
"N test" next to the places taken. `/plugins` says what the test environment still misses and how
many testers there are.

Not tested against the real Stripe test mode or a real test Odoo database (fakes only, as §8).
