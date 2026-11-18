---
name: belfry-sales
description: Build a ticket or pass checkout on a community's website against a Belfry that sells passes (products, codes, card or bank transfer, invoices in the community's Odoo), or help a Belfry admin set up selling. Use when asked to add "buy a pass" / "get tickets" to a site, to make discount codes or pay links, or to explain how an order becomes a pass.
---

# Selling passes on a Belfry

The full contract is `docs/sales.md` in the Belfry repository. This is the short version for
an agent building the website's checkout, or helping an admin.

## Where things are

- API: `https://belfry.<domain>/api/v1/…` (CORS open to `https://<domain>`).
- Admin settings: `https://belfry.<domain>/sales` (admins only: bank, products, codes, offers,
  orders) and `https://belfry.<domain>/plugins` (the Stripe and Odoo connections, each with a
  Production and a Test section, and the e-mail provider). Secrets are typed there, never in chat.
- Trying it out: add a **Tester** (Members → +, Tester; a new person). Whatever a tester's address
  buys goes through the Test sections (Stripe test mode, a test Odoo database): test cards, no
  real money, no place taken. Order answers carry `"test": true`; show a banner then.
- Bank transfer page: `https://belfry.<domain>/pay/<order>`.
- `https://<domain>/.well-known/belfry.json` lists the hosts.

## The checkout, from the website

1. `GET /products` (no auth): `{currency, can_sell, products: [{id, name, description, tier,
   valid_from, valid_until, price: {amount, vat_rate, until}, next_price, left}]}`. Show the
   price; `left` null = no limit. `can_sell: false` means only free products (or 100% codes)
   work.
2. In the browser, make a key (secp256k1, e.g. `nostr-tools` `generateSecretKey`) and keep it
   in `localStorage`. Sign every call with it: NIP-98 (kind 27235, tags `u` = the full URL,
   `method`, and `payload` = SHA-256 of the body for a POST), header
   `Authorization: Nostr <base64 of the event>`. One event per request (replays are refused).
   If the visitor is signed in to the community already, sign with their session instead: the
   pass goes on their account.
3. Optional: `POST /codes/check {"product", "code"}` → `{percent, left}` (404 `unknown_code`,
   409 `code_used`; 10 an hour per key).
4. `POST /checkout {"product", "name", "email", "code"?, "pay": "card"|"invoice",
   "billing": {"kind": "person"|"organisation", "name", "vat", "street", "zip", "city",
   "country", "invoice_email"}, "return_url"}` → 201 with `order`, `state`, `total` (`net`,
   `vat`, `gross`, `percent_off`), `invoice` (`number`, `communication`, `url`), `card_url`,
   `pay_url`, `hold_until`. Send the buyer to `card_url` to pay by card; or show `pay_url` /
   the communication for a transfer. `return_url` must be on the community's own domain;
   Belfry adds `?order=<id>`.
   A card order has no invoice until it is paid (`invoice: null`); to switch, `POST
   /checkout/<order>/pay {"pay": "invoice" | "card"}` with the same key (card → invoice makes
   the invoice then). Offer forms: in kind (`POST /offers`) defaults to 50%.
   State `confirm` (a free product without a code): tell the buyer to open the link mailed to
   their address; the pass opens then (no `bunker_url`: they sign in with an e-mail code).
   `email_mismatch`: this browser's account waits for another address to be confirmed.
5. On the return page, poll `GET /checkout/<order>` (signed by the same key) every few seconds
   until `state` is `active`; its `bunker_url` (NIP-46) signs the browser in to the
   community's apps. States: `pending` → `invoiced` → `paid` → `active`, or `cancelled` /
   `expired` (unpaid after 7 days).

Errors are `{"error", "message"}`: `unknown_product`, `not_on_sale`, `sold_out`,
`unknown_code`, `code_used`, `invalid_email`, `invalid_billing`, `invalid_vat`, `cannot_sell`,
`email_mismatch`, `too_many` (limits per key, address and IP), `accounting`. `invoice.url` is
Belfry's own page of the order; the PDF comes by mail. Show the message, keep the form filled in.

Never tell a buyer whether an address is already a member: Belfry doesn't either.

## In kind (public requests)

Offers in kind are public, like GitHub issues, on the belfry's requests page
(`https://<domain>/requests` or whichever channel the admins picked; `GET /products` says
`"offers": true` when there is one). Say so plainly on the form: the request and the name are
public; the e-mail address is never shown on the public page.

`POST /offers {"product", "name", "email", "offer", "percent"}` (percent 50–100, default 50)
→ `{offer, state: "confirm"}`; the requester confirms the mailed link, then the request is public.
`GET /offers/<id>` by the same key → `state`, `url` (the public thread), `my_url` (theirs, to
comment). The admins answer in the thread; the personal code comes only by mail and works only
for that key or that address.

## For admins

- Codes: `POST /codes {"percent", "product"?, "uses", "count", "expires"?, "note"?}` (admins'
  key), or the console. A sponsor paying for N passes: an invoice by hand in Odoo and N codes.
- Pay links with a description: `belfry pay-link --amount 12.50 --communication "…"
  --description "…"` on the server, or `POST /pay-links` (admins). An unsigned `/pay?…` link
  shows only an amount and a communication, never a description.
- A buyer's address is unproven until they click the receipt's link or sign in with an e-mail
  code; until then they hold the pass but can't invite, have no mailbox and aren't found by
  e-mail.
