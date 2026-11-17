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

## 2. Settings (console, admins only): belfry.<domain>/sales

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
  enter at Stripe (`https://belfry.<domain>/hooks/stripe`) and the events to select.
- **Products**: id (slug), name, description, pass tier, role, valid from / until (the pass's
  dates), prices `[{until?: date, amount}]` in order (the first whose `until` hasn't passed
  applies; amount = price excluding VAT for organisations, including VAT for people — the same
  number, as belfry_pass does), VAT rate, how many exist (optional), on sale yes / no.
- **Codes**: make codes (percent 1–100, product or any, uses, expires, note; N at once), see
  who used them, revoke.
- **Offers** (in kind) and **Orders**: lists with their state; an order can be cancelled or
  marked paid by hand (reason required).

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
             "zip": "…", "city": "…", "country": "BE", "invoice_email": "…"},
 "return_url": "https://<domain>/tickets/thanks"}
```

→ 201

```json
{"order": "o_…", "state": "confirm" | "pending" | "invoiced" | "active",
 "total": {"net": 165.29, "vat": 34.71, "gross": 200.0, "percent_off": 0},
 "invoice": {"number": "INV/2026/0042", "communication": "+++123/4567/89012+++", "url": "…"},
 "card_url": "https://checkout.stripe.com/…",
 "pay_url": "https://belfry.<domain>/pay/o_…",
 "hold_until": "2026-11-24T16:00:00Z"}
```

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
- `pay: card` → `card_url` (a Stripe Checkout session made from the posted invoice: invoice
  number in `client_reference_id` and metadata). `pay: invoice` → the invoice is mailed and
  `pay_url` shows the transfer details. Both are returned when both are set up, so the buyer can
  change their mind.
- An organisation's VAT number is checked on VIES before the invoice is made (`invalid_vat`;
  VIES down → accepted and flagged on the order for the admins).
- The invoice's contact in Odoo is never found by an e-mail address (the buyer's isn't
  proven): a person gets the Odoo contact Belfry stored on their account, else a new one; an
  organisation is matched by its VAT number only when VIES said it is valid, else a new one.
- Errors: `unknown_product`, `not_on_sale`, `sold_out`, `unknown_code`, `code_used`,
  `invalid_email`, `invalid_billing`, `invalid_vat`, `cannot_sell` (Odoo not set),
  `email_mismatch` (409: an unverified account ordering for another address), `too_many`
  (429), `accounting` (Odoo refused).
- Limits (`too_many`): 5 open orders per key; 3 open orders per address and 20 orders an hour
  per address; 10 checkouts an hour per IP; for a product with limited places, 2 unpaid
  orders per IP; 10 code checks an hour per key. At most 5 sales mails a day go to one
  address (the others wait). The IP is kept only as a keyed hash.
- Asking again with the same key, product and e-mail while an order is open (or became active
  in the last 24 hours: a retried request) returns that order, with 200.
- `unknown_code` is 404, `code_used` and the other refusals 409, `accounting` 502 (the order is
  cancelled and its code's use given back). Odoo not answering is no error: the order is kept
  `pending` and the tick makes the invoice later.

`GET /checkout/<order>` — NIP-98 by the key that made it → the same shape with the current
state (`confirm` →) `pending` → `invoiced` → `paid` → `active`, or `cancelled` / `expired`
(an ended order that still gets paid comes back to `active`) and, once active,
`bunker_url` when the key isn't signed in yet. Asking also retries a step that failed and makes
a new card session when Stripe's (24 hours) expired. Another key gets 404.

`GET /billing` — NIP-98, a verified account only → the billing details saved at its last order.

`POST /offers` — NIP-98 (any key). `{"product", "name", "email", "offer": "…", "percent": 50–100}`
→ 201 `{"offer": "f_…", "state": "open"}`. 3 a day per key and per IP, 3 open per key. The belfry's admins see it in the console and decide
(percent may differ from the asked one, or no); the answer is mailed with a personal code.
`GET /offers/<id>` by the same key → its state. Admins: `POST /offers/<id>/decide
{"percent": 0|50–100, "note"}`.

`POST /codes` — NIP-98, admins. `{"percent", "product"?, "uses": 1, "count": 1, "expires"?,
"note"?}` → the codes. A sponsor paying for N passes is an order by hand in Odoo and N codes
made here (no sponsor checkout in this version).

## 5. Paying

- **Hold**: an unpaid order holds its place for **7 days** (`hold_until`), then expires: the
  invoice gets a credit note, the code's use is given back. One reminder is mailed 2 days before;
  it and `/pay/<order>` say until when the place is held.
- **Paid after it ended** (a card payment that comes late, a transfer reconciled after day 7):
  the order **reopens**: the pass opens, a new invoice is made when the first was credited (the
  old number and its credit note stay in the order's history), the payment is registered on
  the new one, and the order is flagged `paid_late` (and `oversold` when no place was left:
  they paid, they get the pass, the admins see it). An admin can do the same by hand: "Reopen
  and mark paid" (reason required) on an expired or cancelled order. Only the order's current
  invoice counts for closing a pass on a credit note.
- **Card**: `checkout.session.completed` (and `checkout.session.async_payment_succeeded`) at
  `https://belfry.<domain>/hooks/stripe` (signature checked with the console's webhook secret,
  deduplicated on the event id; amount, currency and invoice number must match the order, else
  it is flagged and nothing opens) → pass active → the payment registered on the Odoo invoice
  in the card journal (after the pass: Odoo being down never keeps a paid pass shut; the tick
  retries the booking). Stripe refusing to make a session leaves the transfer (flag on the
  order). A payment that arrives after the order ended reopens it (above).
- **Transfer**: Belfry reads the open orders' invoices from Odoo every 15 minutes (in one call;
  when that fails, one by one, flagging the one that can't be read); when Odoo's reconciliation
  says paid → pass active. An admin can also mark an order paid by hand (reason required); an
  order paid before it had an invoice still gets one, with the payment on it.
- A step that fails (Odoo or Stripe down) is retried every 15 minutes, 30 times at most, then
  the order is flagged `stuck` for the admins. Mails are marked sent only once the mail
  provider took them, and retried (8 times at most).
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
  one. It works without bank details too.
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
- The browser key binding (Keycast patch 0022) and an EPC QR read by real banking apps.

