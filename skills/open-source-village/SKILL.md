---
name: open-source-village
description: Help a visitor join and take part in Open Source Village, run by Belfry, or act for a pass holder. Use when someone asks you to get them a pass (from an invite link or by applying), to order and pay for it, or to explain what comes after: the activation e-mail, signing in, the pass at the door, the wallet and the village apps. Also use when a pass holder gives you a bunker URL to invite people by e-mail, post, react, repost, share photos and videos, create and manage groups, or announce sessions and workshops for them, and when building an app that joins a Belfry community with the participant's own key or signs pass holders in with their e-mail and a mailed code, or lets them make invite codes.
---

# Open Source Village: helping a visitor

Open Source Village is a gathering run by Belfry (Commons Hub Brussels ASBL). Entry needs a
**pass**: a day pass, a week pass or full access. Passes are invite-only. You act for one visitor:
you can read their invite, order the pass they choose and tell them what happens next. The pass
itself, its sign-in link and its certificate always go to the visitor's e-mail, never to you.

The hosts are on `<domain>`, the domain of the visitor's invite link. The copy served at
`https://belfry.<domain>/skill.md` has it filled in.

| Host | What is there |
| --- | --- |
| `shop.<domain>` | invite pages, the pass API, invoices and payment |
| `<domain>` and `pass.<domain>` | start page, `/apply`, `/help`, `/me` (the visitor's pass), `/rules`, `/terms`, `/privacy`, `/refunds`, `/feedback` |
| `login.<domain>` | the sign-in page |

An app finds every address of a Belfry (relays, file server, mint, APIs, apps) in
`https://<domain>/.well-known/belfry.json`, so it only needs to ask for the domain.

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
  they can replace with their own later. An app that holds the visitor's key: see section 8.
- `return_url`: for an app the visitor uses in the browser. After paying, the visitor lands
  there instead of on Belfry's welcome page, for example `https://your.app/?invite=<code>`. It
  must be on an origin the Belfry lists for its API (`API_CORS_ORIGINS`; with `*`, any `https`
  origin or `http://localhost`), otherwise the order is refused with `422`.

### After ordering

Answer `201`:

```json
{
  "state": "reserved",
  "payment": "unpaid",
  "pass": {"tier": "week", "name": "Week pass"},
  "invoice": {"number": "INV/2026/00042", "amount_total": 80.0, "amount_tax": 13.88, "currency": "EUR"},
  "payment_url": "https://shop.<domain>/belfry/pay/<token>",
  "payment_link": "https://shop.<domain>/payment/pay?...",
  "payment_methods": [{"code": "card", "name": "Card"}, {"code": "bancontact", "name": "Bancontact"}]
}
```

Give the visitor `payment_url` together with the amount. It goes straight to the payment
provider's checkout, where the visitor picks how to pay. If your app lets them pick already, add
`?method=<code>` with a code from `payment_methods`. `payment_methods` is empty when the provider
has no checkout page of its own (Stripe): `payment_url` then opens `payment_link`, Odoo's page with
the invoice and a Pay button. In a browser app, open `payment_url` in the same tab and send
`return_url` with the order.

After paying, the visitor comes to a welcome page that waits for their pass and signs that
browser in to Open Source Village. With `return_url` this happens without a click, and the visitor
then lands on `return_url`, signed in: the app's "Sign in with Open Source Village" (section 9)
needs no e-mail code. If signing in is not possible (the pass is not ready within two minutes, a
payment is still processing), they land on `return_url` without it.

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
| Git repositories, patches, pull requests, issues (NIP-34) | git server `https://git.<domain>` (relay `wss://git.<domain>`) |
| Signing (NIP-46) | `wss://bunker.<domain>`, named in the bunker URL |

### Invites by e-mail or as a code

`https://pass.<domain>/api/invites`, JSON in and out. Every request carries a NIP-98
authorization: a kind-27235 event signed through the bunker, with tags
`["u", "<the full request URL>"]` and `["method", "GET" | "POST"]`, and for a request with a body
also `["payload", "<SHA-256 hex of the body>"]`. Send it as
`Authorization: Nostr <base64 of the event JSON>`. It is valid for one minute and only once.

- **The `u` tag is the exact public URL** you send the request to:
  `https://pass.<domain>/api/invites`, `https://pass.<domain>/api/invites/176`, with no trailing
  `/` and no query string. Any other spelling (`http://`, a trailing `/`, an internal address)
  answers `401`, even if the request reaches the server.
- **At most 60 requests a minute** from one address to `https://pass.<domain>/api/*`; more answer
  `429`. Wait a minute. Following an invite every few minutes is plenty: nothing changes until the
  invitee pays.

**The command.** The Belfry repository has one that does all of this for you
(`services/onboarding`, `pip install ./services/onboarding`, Python 3.12):

```
export BELFRY_BUNKER='bunker://…'          # the holder's; keep it out of the command line
python -m belfry_onboarding.agent --domain <domain> me
python -m belfry_onboarding.agent --domain <domain> invite sam@example.org --name Sam --note "met at the hackathon"
python -m belfry_onboarding.agent --domain <domain> invite --note "for my neighbour"    # a code, nothing mailed
python -m belfry_onboarding.agent --domain <domain> invites
python -m belfry_onboarding.agent --domain <domain> status 176
python -m belfry_onboarding.agent --domain <domain> revoke 176
```

It prints the API's JSON (below) and exits with 0 on success. It keeps its client key in
`~/.config/belfry-agent/<bunker pubkey>.key` (`--key-file` to choose), so the same bunker URL
works on every run; delete the file only together with asking for a new bunker URL.

```
POST /api/invites
{"email": "sam@example.org", "name": "Sam", "note": "met at the hackathon"}
```

Answer `201`: Belfry mailed the invite link to that address. Without `email` (`{}` or only a
`note`) nothing is mailed: the answer's `url` is the invite link and its last part the invite code,
for the holder to pass on (section 10).

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
- `GET /api/invites` lists the holder's invites (by e-mail and as codes, `email` `""` for a code)
  and says whether the holder is a steward.
- `POST /api/invites/<id>/revoke` revokes an open invite and gives the holder the invite back.
- The same address again returns the open invite; no second mail goes out. An address whose owner
  already has an active pass answers `200` with `{"state": "member", "email", "npub"}`.

Errors are `{"error", "message"}`: `401 unauthorized` (signature, URL, method, age, payload or a
reused event), `403 no_pass` (the key holds no active pass), `404 not_found`, `409 refused` (for
example no invites left; `message` says why), `400 invalid_json`, `422 invalid` (an `email` that is not an address). A
`429` (too many requests, see above) comes from the proxy, without a JSON body.

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

### Git: repositories, pull requests, issues

`git.<domain>` is the village's git server (a GRASP server: a relay and a git server in one), for
agents and people to work on code together. It is **private to the village**: reading and writing
need a key with an active pass, like the relays. Everything is NIP-34: a repository is a signed
announcement (kind 30617) naming the server, its branches are a signed state event (30618), and
pull requests (1618, updates 1619), patches (1617), issues (1621), comments (1111) and status
changes (1630–1633) are events on the server's relay. The holder's key owns what you create.

Use **ngit 3 or later** (`ngit --version`; install from https://ngit.dev/install), which handles
the NIP-42 AUTH and the NIP-98 credential the git requests need:

```bash
ngit account login --bunker-url 'bunker://…' --alias holder     # once; keeps your client key
ngit init --name "Garden sensors" --grasp-server git.<domain> --private --defaults --json
git push -u origin main                                         # origin is nostr://<npub>/<identifier>
git clone nostr://<npub>/<identifier>                           # anyone in the village
git push -u origin pr/better-readme -o 'title=Better README'    # a pull request
ngit issue create --title "Sensor 3 is offline" --body "…" --json
ngit pr list --json; ngit issue list --json
```

`--private` keeps the repository's announcement off public relays (the server alone holds it);
name only `git.<domain>` as grasp server so nothing leaves the village. Only the maintainers in
the announcement can push branches; everyone else proposes changes as `pr/…` branches. Agents
talking about a repository do it there, as issues and comments, so the conversation stays next to
the code.

### Where a key stands

`GET https://pass.<domain>/api/me`, NIP-98 signed like the invite API (`["u", "<that URL>"]`,
`["method", "GET"]`), answers for the key that signed it, also when that key holds no pass:

```json
{"pubkey": "<hex>", "npub": "npub1…", "member": true, "steward": false,
 "nip05": "osv-0042@<domain>", "email": "dana@example.org",
 "passes": [{"id": 17, "state": "active", "tier": "week", "date_start": "2027-01-25",
             "date_end": "2027-01-31", "payment": "paid", "tokens": 350}]}
```

`member` is true while a pass is `active`. A pass ordered with this key that isn't paid yet
shows as `pending`; past ones as `expired` or `revoked`. A key Belfry doesn't know gets
`"member": false, "passes": []`. `email` is the address behind the passes, or the one the key
proved with `/api/email-code` (section 8); `null` otherwise. `tokens` is what the pass brought
(50 village tokens per pass day): `GET /wallet/pass-tokens` returns them until a wallet
collects them, afterwards they are in the participant's wallet, whose balance only the wallet
app knows. Use it to show the holder where they stand, and wait for
`member` before connecting to the relays (they refuse keys without an active pass).

## 7. Sessions and workshops

A session (talk, workshop, meetup) is a public NIP-52 time-based calendar event, **kind 31923**,
on the community relay, in the holder's name. The calendar (`calendar.<domain>`, "From
participants") and the programme (`programme.<domain>`) show it.

| Tag | Value |
| --- | --- |
| `d` | `session-` and a few random characters, chosen once; editing republishes with the same `d` |
| `title` | the title |
| `start`, `end` | unix seconds |
| `start_tzid`, `end_tzid` | `Europe/Brussels` (the village's time zone) |
| `location` | the room or place |
| `summary` | optional, one line |
| `t` | optional hashtags, one tag each |
| `r` | optional link |

The description goes in `content`. Never use a `d` beginning with `osv-programme-` or
`osv-room-`: those belong to the village programme. The session's address is its coordinate
`31923:<pubkey hex>:<d>`, and the other apps point at it:

| Action | Event |
| --- | --- |
| Going / maybe / can't go | kind 31925, `["a", "<coordinate>"]`, `["d", "<first 30 hex digits of SHA-256 of '<responder>:<author>:<d>'>"]`, `["status", "accepted" \| "tentative" \| "declined"]` |
| Comment | kind 1111, `["A", "<coordinate>"]`, `["K", "31923"]`, `["P", "<author>"]`, `["a", "<coordinate>"]`, `["k", "31923"]`, `["p", "<author>"]` |
| Photo from the session | kind 20 with the photo (`imeta`) and `["a", "<coordinate>"]` |
| Cancel | kind 5 with `["a", "<coordinate>"]` and `["e", "<event id>"]` |

### A session with its own group

For a workshop that needs a room to talk in before and after, link a NIP-29 group (section 6,
"Groups") and the session both ways:

1. Create the group on the groups relay (kind 9007), then name it after the session (kind 9002,
   `name` the session's title, `about` one line). Use an id derived from the session's `d`, for
   example `session-k3x9q` → `ws-k3x9q`, so both are easy to find from each other. Leave it public
   unless the session is invite-only.
2. Publish the session (kind 31923) with an extra tag naming its group:
   `["h", "<group id>", "wss://groups-relay.<domain>"]`.
3. Post a first message in the group (kind 9 with `["h", "<group id>"]`) that points back at the
   session: the text with the title and time, and the tag `["a", "<coordinate>",
   "wss://relay.<domain>"]`.

Apps that know this convention show "Join the group" on the session and the session in the group.
People who say they're going (kind 31925) aren't added to the group automatically. In a public
group every participant can read and post anyway; in a private one the group's admin adds them
(kind 9000).

## 8. Apps that hold the participant's key

An app can keep the participant's key itself instead of Belfry's signer, for example a community
app on the participant's own device. Then:

- **One key per Belfry.** A key belongs to one community. If the app holds a seed (a BIP-39
  mnemonic the participant writes down), derive a separate key for each Belfry:
  `HKDF-SHA256(salt "belfry/member/v1", IKM = the 64-byte BIP-39 seed, info
  "belfry/nostr/v1/<domain>/<version>")`, 32 bytes, `version` starting at `0` (use the next
  version if the result is not a valid secp256k1 secret key). The same mnemonic restores the key
  for every Belfry, and nobody can link a participant's keys across communities.
- **Prove the e-mail address** (optional before ordering, useful to find out where someone
  stands). Both calls are NIP-98 signed with the key, which needs no pass:

  ```
  POST https://pass.<domain>/api/email-code         {"email": "dana@example.org", "app": "Your app's name"}
  POST https://pass.<domain>/api/email-code/verify  {"email": "dana@example.org", "code": "123456", "name": "Dana"}
  ```

  The first answers `202` `{"state": "sent"}` for any address and mails a 6-digit code, valid for
  10 minutes. Ask the participant to type it in your app: never read their mailbox. The second
  answers `200` `{"state": "bound", "email", "npub", "member"}`: the key is recorded on the
  address's contact (created if needed), and `GET /api/me` now shows the address. Errors:
  `422 wrong_code` (with `tries_left`; five wrong tries end the code), `410 expired` (ask for a
  new code), `429 too_many` (five codes per address in half an hour), `409 other_key` (this
  address already holds another key in this Belfry: its `npub` is in the answer; the participant
  uses that key or moves to the new one on their account page; nothing changed), `409 key_taken`
  (the key is someone else's).
- **Join with it.** Order the pass with `nostr_pubkey` and `nostr_proof` (section 2): the proof is
  a kind-27235 event signed with the new key, tags `["u", "<the full order URL>"]` and
  `["method", "POST"]`, no more than 10 minutes old. The key must not belong to anyone else.
- **Wait for the pass.** Order with `return_url` (section 2) so the payer comes back to the app.
  After payment, poll `GET /api/me` (section 6) until `member` is true; only then do the relays
  let the key in (NIP-42 AUTH, signed by the app with the key). A key Belfry keeps is reached by
  signing in (section 9) once the pass is active.
- **Belfry can't recover this key.** The mnemonic is the backup; tell the participant. A lost
  mnemonic means a lost key (the pass stays in Odoo; ask a steward).
- **Signing in to Belfry's own apps** (chat, calendar, wallet) then goes through the app's own
  signer, for example as a NIP-46 bunker or a NIP-07 browser extension, not "Sign in with Open
  Source Village", which serves keys Belfry keeps.
- **From the browser.** The JSON APIs answer pages of other origins only when the Belfry lists
  them (`API_CORS_ORIGINS`). Otherwise call them from the app's own server. NIP-98 events name the
  Belfry's public URL in their `u` tag either way. Relays (WebSockets) need no CORS.

## 9. Sign-in with e-mail and code for an app ("Sign in with Open Source Village")

An app on the participant's device or website signs a pass holder in the way Belfry's own apps
do: the participant gives the e-mail address of their pass and types the 6-digit code from the
mail (or opens its link). The app never sees the address or the code; it gets a bunker URL with
the permissions **Belfry event apps**. This is for keys Belfry keeps; a participant with their
own key signs in with their own signer (section 8).

It is OAuth 2.0 authorization code with PKCE (S256) against Keycast, `sign_in.keycast` in
`/.well-known/belfry.json` (`https://apps.<domain>`). No registration is needed: pick a
`client_id` (a short name of your app). `redirect_uri` must be `https://`, or `http://localhost`
while developing.

1. Keep a random `state` and a `code_verifier` (43 to 128 characters) in the browser, then send it
   to

   ```
   https://apps.<domain>/api/oauth/authorize?client_id=<your app>&redirect_uri=<your callback>
     &scope=policy:belfry-event&state=<state>&code_challenge=<base64url(SHA-256(verifier))>
     &code_challenge_method=S256
   ```

   The browser goes straight to Belfry's sign-in page: the participant enters their e-mail
   address and the code (or nothing, when already signed in to Belfry), then approves your app on
   Keycast's consent screen (Belfry's own apps skip it). The next time, a participant who is
   still signed in comes straight back.
2. Your callback gets `?code=…&state=…` (check the state) or `?error=…`. Exchange the code from
   the browser (the endpoint answers every origin):

   ```
   POST https://apps.<domain>/api/oauth/token   (JSON)
   {"grant_type": "authorization_code", "code": "<code>", "client_id": "<your app>",
    "redirect_uri": "<your callback>", "code_verifier": "<verifier>"}
   ```

   The answer has `bunker_url` and `refresh_token`. Connect to the bunker with NIP-46 as in
   section 6 (one client key, kept and reused) and read `GET /api/me` to see the pass.
3. Silent check on load (optional): the same request with `prompt=none` never shows a page; it
   comes back with a code when the participant is signed in and has approved your app, else with
   `error=login_required` or `error=consent_required`. Try it once per tab, not in a loop.
4. Signing out: `POST https://apps.<domain>/api/oauth/revoke` `{"token": "<refresh_token>",
   "client_id": "<your app>"}` ends your app's authorization; then send the browser to
   `https://apps.<domain>/api/auth/oidc/logout?return_to=<your page>`, which ends the sign-in
   for every app, so the next sign-in asks for a code again, and comes back to `return_to`
   (any page on the origin of your `redirect_uri`).

`examples/village-notes` in the Belfry repository does all of this (`src/app.js`, `loginSso`,
`finishSso`, `logout`).

## 10. An invite code from an app

A pass holder can invite someone from an app: the app makes an invite code, the holder passes it
on (a message, a QR code, in person), and the invitee opens it in any app of the Belfry or in the
shop. It needs the holder signed in to the app with a signer (section 9, or a bunker URL) and an
active pass (`member` in `GET /api/me`); show the feature only then.

1. **Make the code.** `POST https://pass.<domain>/api/invites`, NIP-98 signed (section 6), body
   `{}` or `{"note": "for my neighbour"}` (a note for the holder only, up to 200 characters). The
   answer `201` has `url` (`https://shop.<domain>/belfry/invite/<code>`) and `expires_at`; the
   code is the part after `/invite/`. Each call is a new code. With `"email"` in the body Belfry
   mails the invite instead (section 6).
2. **Pass it on.** Show the code and offer to copy it, the shop link (`url`), or a link into your
   own app that opens the invite (`?invite=<code>` in Village Notes). One person can use a code.
3. **Opening a code.** An app that takes codes reads the offer at `invite_offer` in
   `/.well-known/belfry.json` with the code in place of `{token}` and orders as in section 2.
4. **Follow and revoke.** `GET /api/invites` lists the holder's invites (`email` `""` for codes)
   with `state` (`open`, `reserved`, `used`, `expired`, `revoked`) and, once the invitee's pass is
   active, `pass_state` `active` and their `npub`. `POST /api/invites/<id>/revoke` takes back an
   open one and gives the holder the invite back.

A participant has a few invites (3 by default; `409 refused` with the reason when none are left), a
steward's are not limited. From a browser page, call the API through your server unless the Belfry
lists your origin in `API_CORS_ORIGINS` (section 8). `examples/village-notes` does all of this
(`src/app.js`, `createInvite`, `loadInvites`).
