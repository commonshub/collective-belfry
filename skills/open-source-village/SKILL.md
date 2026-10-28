---
name: open-source-village
description: Help a visitor join and take part in Open Source Village, run by Belfry. Use when someone asks you to get them a pass (from an invite link or by applying), to order and pay for it, or to explain what comes after: the activation e-mail, signing in, the pass at the door, the wallet and the village apps.
---

# Open Source Village: helping a visitor

Open Source Village is a gathering run by Belfry (Commons Hub Brussels ASBL). Entry needs a
**pass**: a day pass, a week pass or full access. Passes are invite-only. You act for one visitor:
you can read their invite, order the pass they choose and tell them what happens next. The pass
itself, its sign-in link and its certificate always go to the visitor's e-mail, never to you.

The hosts are on `<domain>`, the domain of the visitor's invite link. The copy served at
`https://pass.<domain>/skill.md` has it filled in.

| Host | What is there |
| --- | --- |
| `shop.<domain>` | invite pages, the pass API, invoices and payment |
| `<domain>` and `pass.<domain>` | start page, `/apply`, `/help`, `/me` (the visitor's pass), `/rules`, `/terms`, `/privacy`, `/refunds`, `/feedback` |
| `login.<domain>` | the sign-in page |

## Rules

- **Ask before you spend.** Show the visitor the tier, its dates or duration and the price, with
  VAT as below, and get a clear yes before you order. An order holds the invite until it is paid
  or released.
- **The visitor's details only.** Use the visitor's own name and e-mail address. The pass, the
  sign-in link and the claim link are sent there. Never use your own address or one you made up.
- **Payment is the visitor's.** The order returns Odoo's payment link, a hosted page for card,
  Bancontact, iDEAL and the like. Give it to the visitor to pay. Pay it yourself only if the
  visitor told you to and you have a payment method they gave you for this.
- **The invite link is a credential.** Anyone who has it can use it. Don't post it, log it in
  shared places or send it to anyone but the visitor.
- **Sign-in links and codes are the visitor's.** There are no passwords. Sign-in works with
  one-time links and 6-digit codes sent by e-mail, or with the pass in a wallet. Don't ask for
  them, and don't use them for the visitor. Never ask for a Nostr private key (nsec) either.
- **Don't guess.** For anything not covered here (refunds, accessibility, the programme, the
  venue), point the visitor to `/help` or to a steward instead of inventing an answer. The terms,
  privacy and refund pages are still drafts: say so if the visitor relies on them.

## 1. Getting in

- **With an invite link** (`https://shop.<domain>/belfry/invite/<token>`, from a steward or from
  a participant): go to step 2. The page's HTML header points to the API:
  `<link rel="alternate" type="application/json" href="/belfry/api/invites/<token>">`.
- **Without one**: the visitor applies at `https://<domain>/apply`. Fill in name, e-mail, a few
  sentences of motivation in the visitor's own words, and optionally "I would like to volunteer".
  The form posts as `application/x-www-form-urlencoded` with the fields `name`, `email`,
  `motivation` and `kind=volunteer` (optional). Leave the hidden field `website` empty. The
  stewards answer by e-mail, either with an invite or a friendly no; there is no way to speed
  that up.
- **Volunteers** earn a two-week pass with three shifts instead of paying. If a steward accepts
  them as a volunteer, they get a link to their personal shift page by e-mail; there is no API
  for that.

## 2. Ordering a pass with the pass API

Base: `https://shop.<domain>/belfry/api/invites/<token>`. JSON in and out. The token in the URL
is the only credential.

### Read the offer

```
GET /belfry/api/invites/<token>
```

```json
{
  "state": "open",
  "expires_at": "2026-11-10T09:00:00Z",
  "currency": "EUR",
  "early_bird_until": "2026-11-30",
  "vat_rate": 21.0,
  "passes": [
    {"tier": "day",  "product_id": 12, "name": "Day pass",    "list_price": 50.0,  "price": 40.0,  "company_price": 60.0},
    {"tier": "week", "product_id": 13, "name": "Week pass",   "list_price": 100.0, "price": 80.0,  "company_price": 120.0},
    {"tier": "full", "product_id": 14, "name": "Full access", "list_price": 200.0, "price": 160.0, "company_price": 240.0}
  ],
  "order_fields": {"required": ["tier or product_id", "name", "email"],
                   "optional": ["company_name", "vat", "promo_code", "nostr_pubkey", "nostr_proof"]}
}
```

The figures above are only an example; always quote what the API returns.

- `price` is for individuals, VAT included. `company_price` is for organisations (an order with a
  `company_name`), with VAT added on top. `vat_rate` is a percentage.
- When `price` is lower than `list_price`, an early-bird price applies until `early_bird_until`.
- If an order is already waiting, the answer is `"state": "reserved"`, as under "After ordering".

### Order

```
POST /belfry/api/invites/<token>/order
Content-Type: application/json

{"tier": "week", "name": "Alex Example", "email": "alex@example.org"}
```

Optional fields:
- `company_name` and `vat`: the invoice goes to the organisation, which pays the company price
  plus VAT.
- `promo_code`: if the visitor has one. A code that doesn't apply is refused and nothing is
  created.
- `nostr_pubkey` and `nostr_proof`: only for visitors who already use Nostr and want to join with
  their own key. `nostr_pubkey` is 64 hex characters. `nostr_proof` is a Nostr event (kind 27235,
  the JSON object or its text) with tags `["u", "<the full order URL>"]` and
  `["method", "POST"]`, signed with that key in the visitor's own signer no more than 10 minutes
  before the order. Without these fields Belfry creates and keeps a key for the visitor, which
  they can replace with their own later.

### After ordering

Answer `201`:

```json
{
  "state": "reserved",
  "payment": "unpaid",
  "pass": {"tier": "week", "name": "Week pass"},
  "invoice": {"number": "INV/2026/00042", "amount_total": 80.0, "amount_tax": 13.88, "currency": "EUR"},
  "payment_url": "https://shop.<domain>/payment/pay?..."
}
```

Give the visitor `payment_url` together with the amount. After paying they land on a welcome
page that waits for their pass and then signs that browser in.

To follow up, call `GET` on the base URL again:
- `payment` is `unpaid` (a fresh `payment_url` is included), `processing` (a bank transfer or
  similar is under way) or `paid` (the pass is being activated; `payment_url` is `null`).
- An unpaid order is released by itself after 7 days.

To change the tier or the details before paying, release the order and order again:

```
POST /belfry/api/invites/<token>/start-over
```

This credits the unpaid invoice and returns the open offer. Once a payment is under way or done,
it is refused.

### Errors

Errors come as `{"error": "<code>", "message": "<text for the visitor>"}`:

| Status | `error` | What to do |
| --- | --- | --- |
| 404 | `not_found` | The link is wrong. Ask the visitor to copy it again. |
| 410 | `used`, `expired`, `revoked` | The invite can't be used. Ask whoever sent it for a new one, or apply. |
| 409 | `reserved` | An order already waits. The body carries it, `payment_url` included. |
| 409 | `payment_under_way` | It can't be released. Wait for the payment to finish. |
| 400 | `invalid_json` | Send a JSON object. |
| 422 | `invalid` | Fix what `message` says (name, e-mail, pass, promo code, key proof) and send again. |

## 3. After payment: what the visitor does

Tell the visitor to expect the following. You can't do these steps for them.

1. **Activation e-mail** ("Your Open Source Village pass is active", with the invoice as a PDF).
   Its sign-in link works once, within 7 days, on the device they want to use. If it doesn't
   arrive: check the spam folder, then ask a steward for a new one.
2. **Signing in later**: in any app, choose "Sign in with Open Source Village" and enter the
   e-mail address. A link and a 6-digit code arrive, both valid for 10 minutes. Open the link in
   the same browser, or type the code where the e-mail isn't. Once signed in, the apps sign them
   in by themselves.
3. **The pass at the door**, in one of two ways:
   - On the phone, no app needed: `https://pass.<domain>/me` shows a QR code that renews every
     30 seconds. Keep the page open at the door.
   - In an EUDI wallet: the claim link in the activation e-mail shows a QR code to scan with the
     wallet. Wallets that work are Talao (Android, Play Store) and the EU reference wallet.
     Altme's Play Store version (3.4.1) can't take the pass yet. The claim QR is valid for
     10 minutes; reload the page for a new one. With the pass in the wallet, the visitor can also
     sign in by showing it on the sign-in page.
4. **Village tokens**: 50 per pass day, with no money value, for the marketplace and for tips.
   The link in the activation e-mail puts them into the wallet at `wallet.<domain>`.
5. **Inviting friends**: with an active pass, `/me` → "Create an invite link". Each link works
   once and expires after two weeks.

## 4. The apps

All of them use the same sign-in. The bar at the top of each app leads to the others.

| App | Host | What it is for |
| --- | --- | --- |
| Chat | `chat.<domain>` | private messages with other participants |
| Groups | `groups.<domain>` | rooms for teams, topics and projects; anyone can open one |
| Calendar | `calendar.<domain>` | the village's events and one's own calendars |
| Programme | `programme.<domain>` | what is on now and next, rooms, sessions, volunteer shifts |
| Vote | `vote.<domain>` | anonymous polls, one vote per pass |
| Wallet | `wallet.<domain>` | village tokens |
| Directory | `people.<domain>` | participants who chose to be listed (nobody by default) |
| Market | `market.<domain>` | offers between participants, paid in village tokens |
| Photos | `photos.<domain>` | pictures of the village |
| Help, feedback | `pass.<domain>/help`, `/feedback` | everything above; problems and ideas |

The apps run on Nostr. Each participant has a Nostr key and a village name such as
`osv-0042@<domain>`. Belfry keeps the key for them unless they move to a key of their own on
their account page. The pass and the key are separate: a refunded or expired pass ends access,
but the key stays the participant's.

## 5. When things go wrong

- Refunds: the policy is at `/refunds` (still a draft). Refunds go through Belfry, not through
  you. A refunded or disputed payment ends the pass at once.
- Someone breaks the rules (`/rules`): use the Report button in the app, or tell a steward.
- Something doesn't work: `/feedback`, or a steward.
