# Website accounts

People use the community's own website (the nsite on `https://<domain>`, and its previews on
`https://preview.<domain>`) signed in: someone the belfry doesn't know as a guest of the public
requests channel, members and admins as themselves. Built 2026-11-20; code: `sitesessions.py`
(sessions, `/site/*`), `applyverified.py` (the log-in), `webrequests.py` / `webthreads.py`
(requests and comments), `sales.py` / `ticketsales.py` (offers, checkout), `events.py` (tickets).

**The security limit.** The website never holds a signer (bunker) session. It makes a key in
the browser; proving an e-mail address with a 6-digit code registers that key as a **site
session** of the address's account: one row in the belfry's register (key → account). The
signer knows nothing of that key. A request signed with it (NIP-98) is answered for the account
only by the endpoints listed here; where something must be signed as the person (a comment, a
request's thread, their profile), the **server** signs it through the signer, as it already
signs an author's comments and edits (docs/requests.md). Every other endpoint of the belfry
sees a key nobody knows: no pass, no member, no admin. No answer to a site session ever
carries a `bunker_url`.

Base: `https://belfry.<domain>/api/v1`. Every call is signed with NIP-98 by the browser key
(kind 27235, `u` = the full address with its query, `method`, and for a body the `payload` tag
= its SHA-256); an authorization is accepted once. Errors are `{"error": "<code>", "message":
"…"}`. Needs the belfry's own register (`REGISTER=belfry`): 409 `unsupported` otherwise.

## 1. Logging in

### `POST /login/start`

`{"email": "kim@example.org", "site": true}` → 202 `{"sent": true}`, always (it never says
whether the address is known). `site` changes nothing here. `email` may be
`<username>@<domain>`: the code goes to that member's own address. Errors: 422 `invalid` (not
an address), 429 `too_many` (20 codes an hour per IP, and the register's limits per address and
per key), 401 `unauthorized`, 400 `invalid_json`.

### `POST /login/verify`

`{"email": "kim@example.org", "code": "123456", "site": true, "name"?: "Kim Cook"}`

- **The address has an account** (a member, an admin, a guest who saved that address, a buyer
  whose address this code now proves): the key becomes a site session of it.

  ```json
  {"state": "signed_in", "site": true, "pubkey": "<64 hex>", "npub": "npub1…",
   "nip05": "kim@<domain>", "name": "Kim Cook", "role": "guest"}
  ```

  `pubkey` / `npub`: the **account's** key (never the browser's). `nip05`: the account's
  address in the signer, or `null`. `role`: `admin` | `member` | `guest`. `name`: the name on
  the account ("Guest" when it has none); the `name` sent is ignored for an existing account.
  A guest of something else (an event, another channel) also becomes a guest of the requests
  channel.
- **Nobody has the address:** the identity a requester from the web gets
  (`webthreads.web_identity`): a contact with this proven address, an account in the signer,
  a guest pass of the **requests channel** (90 days), and the 9000 that puts them in it. Same
  answer plus `"new": true`. Its name is `name` when given, else **"Guest"**; never anything
  taken from the address.
- **An application for the address waits:** `{"state": "pending"}`, as without `site` (the key
  becomes its applicant key; nothing else).
- **Nobody has the address and the belfry has no public requests channel** (none picked in
  Sales, or its page is off or members only): `{"state": "unknown", "proof": "…"}`, as without
  `site`. No identity is made.

Errors, all as the chat's log-in names them: 422 `wrong_code` (with `tries_left`), 410
`expired`, 429 `too_many`; plus 422 `invalid` (`name`: not 1–80 characters, or a key or an
e-mail address) and 409 `key_taken` (the signing key is an account's own key: make a new one),
both answered **before** the code is tried (they cost no try); 409 `agent` (the address is an
agent's); 503 `signer`.

Without `"site": true` the endpoint is exactly what it was (docs/membership.md, the chat's
vectors): a signer session bound to the key, `bunker_url` in the answer.

### The session

- Key → account, when it was made, when it was last used, a label from the User-Agent
  ("Firefox on macOS", else "Browser"). Table `site_sessions` in the register.
- **Idle expiry: 30 days** without a signed call. "Last used" is written at most once a minute.
- **At most 10 per account**: the least recently used go.
- A key is one account's: logging in with another address on the same key moves it.
- A **guest's pass on the requests channel is renewed** to 90 days from today at each log-in and
  each use, so the identity does not lapse while the website is used.
- It ends at `POST /site/logout`, and for every session of an account when the account has
  **no active pass left** (checked when the pass closes, and again at each use).
- A guest who becomes a member keeps their sessions (same account, same key): `role` is then
  `member`. A member who is removed loses them with their last pass; logging in again makes
  them a guest of the requests channel, like anyone.

**Not a session** (never logged in, logged out, expired, account gone): every `/site/*` call
answers **401 `no_session`**; the site forgets the key and logs in again. On the other
endpoints below such a key is a browser key like any other, as before.

### `POST /site/logout`

`{}` → `{"ok": true}`. 401 `no_session`.

## 2. The account

### `GET /site/me`

```json
{"name": "Kim Cook", "username": "guest-12", "nip05": "guest-12@<domain>",
 "picture": "https://…" , "about": "…", "email": "kim@example.org", "role": "guest",
 "pubkey": "<64 hex>", "npub": "npub1…",
 "tickets": [{"event_link": "village-2027", "event_title": "The village",
   "start": "2027-01-25", "end": "2027-02-05", "ticket": "<event d>:pass",
   "ticket_title": "Village pass", "ticket_url": "https://chat.<domain>/events/<link>/t/<code>",
   "checked_in": null, "test": false, "cancelled": false}],
 "requests": [{"id": "<64 hex>", "number": 42, "title": "Dinner for forty", "state": "open",
   "created_at": 1795000000, "url": "https://<domain>/requests?id=42"}],
 "comments": [{"request": {"number": 41, "title": "…"}, "id": "<64 hex>", "at": 1795000100,
   "text": "Which evenings?"}],
 "sessions": [{"label": "Firefox on macOS", "created": 1795000000, "last_used": 1795000500,
   "current": true}]}
```

- `name`, `picture`, `about`: the account's profile (kind 0; the community relay's for a
  member, the groups relay's for a guest), else the name on the account, else "Guest";
  `picture` / `about` `null` when not set. `username` / `nip05`: `null` when the signer has none.
- `email`: their own proven address, in full. This is the only answer that carries one.
- `tickets`: the events whose group has the account admitted with a ticket now (at most 30,
  by start). `ticket`: the ticket's d. `ticket_url`: the ticket's own link (its QR,
  docs/checkin.md §2). `checked_in`: unix time or `null`. `test`: a tester's test ticket.
  `start` / `end`: a date (an all-day event) or an ISO time in UTC. Events that can't be read
  are left out.
- `requests`: the account's own requests in the requests channel, newest first: they made the
  offer, they signed the thread, or it is an older card written for their address. `state`:
  `open` | `approved` | `declined` | `closed`. `url`: the request's public address.
- `comments`: the comments they signed, newest first (at most 100), as they read now.
- `sessions`: most recently used first.

401 `no_session`.

### `POST /site/profile`

`{"name"?: "…", "about"?: "…" | null, "picture"?: "https://…" | null}`, at least one, nothing
else. `name`: 1–80 characters, one line, not a key or an e-mail address. `about`: at most 1000
characters. `picture`: an `https://` address, at most 500 characters. `null` or `""` takes
`about` / `picture` out.

The server publishes the account's kind 0 **through the signer**, the current one with these
fields changed and everything else kept (`nip05`, fields this API doesn't know; `display_name`
follows `name` when the profile has one): on the groups relay (where the website's names are
read from, and the only relay a guest is on) and, for a member, on the community relay. A
`name` also becomes the name on the contact. → `{"ok": true, "changed": true}`; `"changed":
false` when nothing changes (no event).

Errors: 422 `invalid`; 409 `own_key` (they hold their own key: the belfry can't sign for
them); 429 `too_many` (10 changes an hour per account); 503 `signer` / `unavailable`; 401
`no_session`.

The site treats the name "Guest" as "ask for a name": after `{"name"}` what they wrote before
reads under the new name (docs/requests.md: names come from the profile; a guest with no
profile shows under the account's name).

## 3. Requests and comments (docs/requests.md)

Signed by a site session, with no `X-Thread-Key`:

- `GET /requests`: the public list; the account's own requests carry `"mine": true`; `no-store`.
- `GET /requests/<id or number>`: when the request is theirs, `me` as for the author
  (`"me": {"author": true, …}`); `"mine": true` on each comment they signed (also under
  someone else's request); and `"viewer": {"role", "author", "can_comment"}`. Someone else's
  request has no `me`.
- `POST /requests/<id or number>/comments` `{"text"}` (1–8000 characters) → 201
  `{"ok": true}`. The author, or anyone whose account **has the requests channel**: a guest
  of it, a member when members read it (the channel is open, or they are in it), an admin. The
  kind 1111 is signed with **their own key** through the signer (an older card's author
  comments the way their card's own link does, so it reads as the author's). Errors: 403
  `not_allowed`; 404 `unknown` (also a request that is not public and not theirs); 422
  `invalid`; 409 `own_key`; 409 `archived`; 429 `too_many` (20 an hour per account); 503.
- `POST /requests/<id or number>/edit`: their own request: `{"title"?, "body"?}` or
  `{"comment", "text"}` as its author. Someone else's request: only `{"comment", "text"}` for a
  comment they signed (403 `not_author` for anything else, 403 `not_yours` for a comment that
  is not theirs). 20 edits an hour per thread for its author, and per thread and commenter.

Unsigned, or signed by the offer's key, or with `X-Thread-Key`: everything is as it was.

## 4. Offers and the checkout (docs/sales.md)

`POST /offers`, `GET /offers/<id>`, `POST /checkout`, `GET /checkout/<order>`,
`POST /checkout/<order>/pay` and `POST /codes/check`, signed by a site session, act **as the
account** (its own key is the order's and the offer's key, so every session of the account
sees them):

- **The address is the account's proven one.** The body needs no `email`; one that names
  another address: 409 `email_mismatch`. `name` defaults to the account's. 409 `not_verified`
  when the account has no proven address.
- **`POST /offers`** → 201 with today's fields and `"state": "open"`, `url` and `my_url`:
  no `confirm` state and no confirmation mail; the thread is posted at once, **by the
  person's own key** (as their confirmation would have posted it). The "Your thread: …" mail
  with their own link is still sent. More errors: 409 `own_key`, 503 `unavailable`, and the
  thread's limits as 429 `too_many`.
- **`POST /checkout`** → today's order shape. The order, the pass or the ticket goes on the
  account: no new account, no `confirm` state (a free product opens at once), and **never a
  `bunker_url`**, in this answer or in `GET /checkout/<order>`.
- `POST /checkout {"offer": "<id>"}` also takes the approval of a request made earlier with
  the same proven address (from a browser key, before they logged in).

Not for a site session (it is a key nobody knows there): `POST /codes`,
`POST /offers/<id>/decide` and everything else an admin does, also for an admin's session;
`GET /billing`.

## 5. Tickets

- `GET /events/<link>` signed by a site session reads as the account (`me` / `viewer`: its
  ticket, `ticket_url`, `checked_in`; a members-only event for a member), and each ticket
  carries `requires` and `eligible` (docs/checkin.md "Tickets that need another ticket").
- `POST /events/<d>/join` signed by a site session: the **account** takes the open ticket →
  `{"ticket", "pubkey": <the account's key>, "new": false}` (or `{"already": true, …}`).
  Never a new guest, never a `bunker_url`. 409 `requires` (with `event_link`, `event_title`,
  `ticket_title`) when the ticket needs one the account doesn't hold; the other errors as
  before (409 `payment`, `needs_application`, `full`, 403 `members_only`, …).
- `POST /events/<d>/apply` signed by a site session: 409 `use_chat`. An application is the
  person's own signed join request; the website holds no key that signs as them.

## 6. CORS

No cookies anywhere; nothing is ever answered with `Access-Control-Allow-Credentials`.

- The apex `https://<domain>`: every `/api/v1/*` path, as before.
- `https://preview.<domain>` (anyone's draft site): what it could call before (the sales
  endpoints, reading events and requests), plus `login/start`, `login/verify`, `site/me`,
  `site/profile`, `site/logout`. Still **not**: a comment or an edit on a request,
  `events/<d>/join`.

A preview is another origin: its browser key is its own, and a session made there is not the
real site's.

## 7. What a site session can and cannot do

Can, for its own account only: read `/site/me`; change name, about, picture; comment on public
requests and edit its own comments, title and text; open a request (an offer); order through
the checkout and read its own orders; check a code; take a free open ticket; read events as
the account; log out.

Cannot: get a signer session or a `bunker_url`; read or post in the chat; send or read
messages or mail; invite; administer anything (an admin's site session has no admin rights
anywhere); change the account's e-mail address or username; apply for a ticket; reach any
endpoint not named in this document as the account.

## 8. Known limits, not verified

- **The belfry cannot tell the website from another client.** Any key that proves an address
  with a code through `login/verify` **without** `site`, `POST /session`, a guest link, or a
  paid order from a browser key still gets a signer session, as before this change. The
  guarantee is about site sessions, not about the website's code: a website that wants a
  bunker can ask for one the old way. Refusing those endpoints to the apex and preview origins
  would close it for browsers; not done (the chat's origin history and the event page rely on
  them).
- A preview is untrusted content and can now show a log-in form: a code typed into a malicious
  draft gives that draft a site session (comments excluded, profile and orders included).
- Whether the groups relay (zooid) stores a kind 0 handed in for a guest, and a member-signed
  1111 from someone who has the channel, was not run against a live relay.
- A profile's `name` on the contact also changes what the belfry calls a member in its mails
  and lists.
- Nothing here ran against a live signer, relay, Stripe or Odoo.
