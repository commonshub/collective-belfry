---
name: open-source-village
description: Help a visitor join and take part in Open Source Village, run by Belfry, or act for a pass holder. Use when someone asks you to get them a pass (from an invite link or by applying), to order and pay for it, or to explain what comes after: the activation e-mail, signing in, the pass at the door, the wallet and the village apps. Also use when a pass holder gives you a bunker URL to invite people by e-mail, post, react, repost, share photos and videos, or create and manage groups for them.
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

## 6. Acting for a pass holder (agents)

A participant or steward can let you act for them. You then have **their rights, nothing more**,
and everything you do appears under their name. There is no API key: every request is a Nostr
event signed with the holder's key, which the relays, the file server and the invite API check.

### Getting the signer

Ask the holder for a **bunker URL** (NIP-46 remote signing; `bunker://<pubkey>?relay=…&secret=…`):

- They sign in at `https://apps.<domain>` (their account page), choose **Connect to Nostr App**,
  give it a name (yours) and the permissions **Belfry event apps**, and copy the `bunker://` URL.
- A holder who uses their own Nostr key gives you a bunker URL from their own signer instead.

Treat the bunker URL like a password: don't log it, post it or pass it on. Its secret binds to the
first client key that connects, so generate one client key, keep it, and reuse it for every
session; a new client key needs a new bunker URL. The holder can end your access at any time
(**Revoke Access** on their account page), and a pass that ends stops it everywhere.

Any Nostr library with NIP-46 works (for example `nostr-tools` in JavaScript, or `nak` on the
command line). Belfry's own reference agent is
`services/onboarding/belfry_onboarding/agentcheck.py` in the Belfry repository: it does every
step below through a bunker.

### Rules

- **Do what the holder asked.** Posts, reactions, invites and groups speak in the holder's name.
  When unsure (a public post, an invite, adding or removing someone), ask first.
- **Invites are the holder's to give.** A participant has a few invites (3 by default); a
  steward's are not limited. Someone already in the village is not invited again.
- **Photos and videos are public by URL**, also when you post them in a private group. Don't
  upload anything the holder would not show to anyone with the link.

### Where things are

Every relay needs NIP-42 AUTH: answer the relay's `AUTH` challenge with a kind-22242 event
(tags `["relay", "<the relay URL>"]`, `["challenge", "<challenge>"]`) signed through the bunker.
Only keys with an active pass get in.

| What | Where |
| --- | --- |
| Posts, replies, reactions, reposts for the whole village, profiles | community relay `wss://relay.<domain>` |
| Groups (rooms): NIP-29 | groups relay `wss://groups-relay.<domain>` (the app: `groups.<domain>`) |
| Photos and videos | Blossom `https://blossom.<domain>` |
| Invites by e-mail | `https://pass.<domain>/api/invites` |
| Signing (NIP-46) | `wss://bunker.<domain>`, named in the bunker URL |

### Invites by e-mail

`https://pass.<domain>/api/invites`, JSON in and out. Every request carries a NIP-98
authorization: a kind-27235 event signed through the bunker, with tags
`["u", "<the full request URL>"]` and `["method", "GET" | "POST"]`, and for a request with a body
also `["payload", "<SHA-256 hex of the body>"]`. Send it as
`Authorization: Nostr <base64 of the event JSON>`. It is valid for one minute and only once.

```
POST /api/invites
{"email": "sam@example.org", "name": "Sam", "note": "met at the hackathon"}
```

Answer `201`: Belfry mailed the invite link to that address.

```json
{"id": 176, "state": "open", "email": "sam@example.org", "name": "Sam", "note": "met at the hackathon",
 "url": "https://shop.<domain>/belfry/invite/…", "expires_at": "2026-11-13 12:00:00",
 "pass_state": false, "npub": false}
```

- The invitee chooses a pass and pays on the page, themselves. Their npub exists only once the
  pass is active. Until then `npub` is `false`.
- `GET /api/invites/<id>` follows one invite: `state` is `open`, `reserved` (they ordered and the
  payment is outstanding), `used`, `expired` or `revoked`; `pass_state` becomes `active` and `npub`
  is their key (`npub1…`) once they have paid.
- `GET /api/invites` lists the holder's invites by e-mail and says whether the holder is a steward.
- `POST /api/invites/<id>/revoke` revokes an open invite and gives the holder the invite back.
- The same address again returns the open invite; no second mail goes out. An address whose owner
  already has an active pass answers `200` with `{"state": "member", "email", "npub"}`.

Errors are `{"error", "message"}`: `401 unauthorized` (signature, URL, method, age, payload or a
reused event), `403 no_pass` (the key holds no active pass), `404 not_found`, `409 refused` (for
example no invites left; `message` says why), `400 invalid_json`, `422 invalid` (no e-mail).

### Posts, replies, reactions, reposts

Publish to the community relay, signed through the bunker:

| Action | Event |
| --- | --- |
| Post | kind 1, the text in `content` |
| Reply | kind 1 with `["e", "<root id>", "<relay>", "root"]`, plus `["e", "<parent id>", "<relay>", "reply"]` when answering a reply, and `["p", "<author pubkey>"]` |
| Reaction | kind 7, `content` `+` or an emoji, tags `["e", "<id>"]`, `["p", "<author>"]`, `["k", "<kind of the event>"]` |
| Repost | kind 6 for a kind-1 post (`content` the reposted event's JSON, tags `["e", "<id>", "<relay>"]`, `["p", "<author>"]`); kind 16 with `["k", "<kind>"]` for other kinds |
| Delete own event | kind 5 with `["e", "<id>"]` |

### Photos and videos

1. Upload: `PUT https://blossom.<domain>/upload` with the file as the body, its `Content-Type`,
   and `Authorization: Nostr <base64 event>`: a kind-24242 event with `content` "Upload <name>",
   tags `["t", "upload"]`, `["x", "<SHA-256 hex of the file>"]`,
   `["expiration", "<unix time, a few minutes ahead>"]`. The answer is
   `{"url", "sha256", "size", "type"}`.
2. Post: put the `url` in the text and add
   `["imeta", "url <url>", "m <type>", "x <sha256>", "size <size>"]` to the event.

Up to 25 MB per file. Images are kept for a year, videos and audio for three months.

### Groups (rooms)

Groups live on the groups relay. Every event about a group carries `["h", "<group id>"]`; choose
the id yourself (lowercase letters, digits and `-`, for example `garden-team`). The holder who
creates a group administers it; only a group's admins may change it or its members.

| Action | Event |
| --- | --- |
| Create | kind 9007 with `["h", id]` |
| Name, description, access | kind 9002 with `["h", id]`, `["name", …]`, `["about", …]`, optional `["picture", <url>]`, and for a **private** group `["private"]` and `["closed"]` (add `["hidden"]` to hide it from non-members). Without these tags the group is **public**: everyone in the village can read it and post in it. |
| Add someone | kind 9000 with `["h", id]`, `["p", "<their pubkey hex>"]` |
| Remove someone | kind 9001 with `["h", id]`, `["p", "<pubkey>"]` |
| Message | kind 9 with `["h", id]`, the text in `content` (photos as above) |
| Reply to a message | kind 9 with `["h", id]`, `["q", "<message id>", "<relay>", "<author>"]`, `["p", "<author>"]` |
| Reaction, repost | as on the community relay (kind 7, kind 16 with `["k", "9"]`), plus `["h", id]` |

The relay keeps each group's state and signs it: read kind 39000 (name and flags), 39001 (admins)
and 39002 (members) with the filter `{"#d": [id]}`.

To turn an npub into the hex pubkey that `p` tags need, decode it (bech32, NIP-19). The invite API
gives you the invitee's npub once their pass is active, so you can then add them to a group.

