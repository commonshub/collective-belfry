# Website accounts

People use the community's own website (the nsite on `https://<domain>`, and its previews on
`https://preview.<domain>`) signed in: someone the belfry doesn't know as a guest of the public
requests channel, members and admins as themselves. Built 2026-11-20; code: `sitesessions.py`
(sessions, `/site/*`), `applyverified.py` (the log-in), `webrequests.py` / `webthreads.py`
(requests and comments), `sales.py` / `ticketsales.py` (offers, checkout), `events.py` (tickets),
`sitenotify.py` (which e-mails someone gets).

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
 "picture": "https://…" , "about": "…", "tags": ["solar", "bread-making"],
 "email": "kim@example.org", "role": "guest",
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
- `tags`: the words they put on their profile, `[]` when none. For a **member or an admin**
  it is the list the chat shows and edits: the `tags` of their kind 30078 `d` =
  `belfry:profile` event on the community relay, read as the chat reads it (lowercased, at
  most 30, each at most 32 characters; the chat allows letters of any alphabet and `_`). For
  a **guest** it is kept on their contact in the register (`belfry_tags`): guests are not on
  the members' relay, so they have no `belfry:profile` event. **The chat reads a guest's tags
  from `POST /api/v1/whois`** (below), and they follow the guest into membership.

**A guest's tags in the chat (2026-11-20).** `POST https://belfry.<domain>/api/v1/whois`
(NIP-98, anyone holding an active pass, guests too; `{"pubkeys": ["<hex>", …]}`, at most 500)
answers, next to `people`, `roles` and `testers`:

```json
{"people": {"<kim>": "guest", "<bob>": "member"},
 "tags": {"<kim>": ["solar", "bread-making"]}}
```

`tags` has one entry per asked key that is a **guest with tags** on their contact (cleaned as
the chat reads tags, 30 at most). A guest without tags, a member, an agent and an unknown key
are not in it: a member's tags are read from their own `belfry:profile` event, as before.
`{"directory": true}` lists members and their agents only, so it carries no `tags`.

**When a guest becomes a member** (an admin accepts their request, a member link, an admin
role: `guests.became_member`), the contact's tags are carried into a `belfry:profile` event of
theirs on the community relay, written through the signer the way `POST /site/profile` writes
a member's: added after the tags an event of theirs already has (30 at most), the rest of that
event kept; the contact's list is then emptied. Best effort, never in the way of the
membership: someone who holds their own key gets no event (the belfry can't sign for them),
and when the relay or the signer doesn't answer the tags stay on the contact, where nothing
shows them for a member (they set them again in the chat or on the website).
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

`{"name"?: "…", "about"?: "…" | null, "picture"?: "https://…" | null, "tags"?: ["…"]}`, at
least one, nothing else. `name`: 1–80 characters, one line, not a key or an e-mail address.
`about`: at most 1000 characters. `picture`: an `https://` address, at most 500 characters.
`null` or `""` takes `about` / `picture` out.

`tags`: the whole list (`[]` empties it), at most 30 (the chat's own limit), each 1–30 characters of `a-z`, `0-9`
and `-`. The server trims and lowercases them and drops doubles; a `#`, a space inside, or
any other character is 422 `invalid` (nothing is saved). One exception: a tag the account
**already has** (written in the chat, whose rule is wider: `café`, `solar_power`) may be sent
back as `GET /site/me` gave it, so the list can be edited without losing it.

- **Member, admin:** the server reads their latest `belfry:profile` event (kind 30078) on the
  community relay and publishes it again **through the signer** with only `tags` in its
  content changed: `share`, `place`, any other field and the event's own tags are kept. No
  event yet: one with `{"tags": […]}` and the `d` tag. When that relay can't be read: 503
  `unavailable`, nothing written. The chat shows the new list at its next sync.
- **Guest:** saved on the contact (see `tags` above).

The server publishes the account's kind 0 **through the signer**, the current one with these
fields changed and everything else kept (`nip05`, fields this API doesn't know; `display_name`
follows `name` when the profile has one): on the groups relay (where the website's names are
read from, and the only relay a guest is on) and, for a member, on the community relay. A
`name` also becomes the name on the contact. → `{"ok": true, "changed": true}`; `"changed":
false` when nothing changes (no event). `changed` is true when the tags or the profile changed.

Errors: 422 `invalid`; 409 `own_key` (they hold their own key: the belfry can't sign for
them; also for `tags` alone, guest or member); 429 `too_many` (10 changes an hour per account); 503 `signer` / `unavailable`; 401
`no_session`.

The site treats the name "Guest" as "ask for a name": after `{"name"}` what they wrote before
reads under the new name (docs/requests.md: names come from the profile; a guest with no
profile shows under the account's name).


### A picture: straight to Blossom

There is no upload endpoint in this API. The website sends the file to the community's
Blossom server itself (`PUT https://blossom.<domain>/upload`, BUD-02, with a kind 24242
authorization **signed by the site session's key**, `x` = the file's SHA-256), then saves the
address it got with `POST /site/profile {"picture": "https://blossom.<domain>/<sha256>.jpg"}`.

The gate in front of Blossom (blossom.py) lets write: a key with an active pass, and a live
site session, as its account. How much:

| who (the account) | one file | in all |
|---|---|---|
| a member, an admin, an agent | 25 MB (the server's limit) | not counted |
| a guest who holds a ticket of an event (not cancelled; a tester's test ticket too) | 25 MB | 1 GB |
| any other guest | 5 MB | 50 MB |

The count is per account (its own key in the chat and all its site sessions together) and of
everything it ever sent: deleting a file gives nothing back. The same file sent again (same
`x`) is not counted twice. A guest's upload must carry a `Content-Length` (a browser's `fetch`
with a Blob does). "Holds a ticket" is read again every two minutes.

Refusals of the gate come with `X-Reason` (words), `X-Belfry-Code` and a JSON body
`{"error", "code"}`, readable from any origin:

- 401: the authorization is not right (no code).
- 403 `no_pass`: the key is no account's and no live session's (logged out, ended).
- 411 `length`: no `Content-Length`.
- 413 `too_large`: `file too big: 5 MB at most for a guest`.
- 413 `quota`: `over quota: 50 MB in all for a guest, 48 MB used` (or `1024 MB in all for a
  ticket holder, …`).

Blossom itself still answers 413 over 25 MB and 415 for a type it doesn't keep. The website
should send a picture it has redrawn on a canvas (small, and without the camera's EXIF data,
which can hold the place a photo was taken): nothing on the way re-encodes it.

## 3. Which e-mails they get

### `GET /site/notifications`

```json
{"items": [
  {"id": "replies",  "value": "asap",  "default": "asap",  "choices": ["asap", "daily", "weekly", "off"]},
  {"id": "mentions", "value": "daily", "default": "daily", "choices": ["daily", "weekly", "off"]},
  {"id": "requests", "value": "off",   "default": "off",   "choices": ["asap", "daily", "off"]},
  {"id": "events",   "value": "asap",  "default": "asap",  "choices": ["asap", "off"]}
]}
```

**An item is there only when the belfry really sends that mail to this account.** The site
shows what it gets, in this order, and nothing else. An account with no proven address gets
`{"items": []}`. 401 `no_session`. `no-store`.

### `POST /site/notifications`

`{"<id>": "<choice>", …}`: one or more of the ids the account has. → the same shape, with the
new values. 422 `invalid` for an empty body, an id the account doesn't have (also one that
exists for others) or a choice the id doesn't take: nothing is changed then. 503
`unavailable` (`events`, when the relay or the register can't be reached; nothing changed).
401 `no_session`.

### What each one is

| id | members, admins | guests | what is sent, when |
|---|---|---|---|
| `replies` | yes | yes | Answers in a thread they **opened from the web** (a request), and in a thread they **commented on from the website** (what came after their first comment). Their own comments never. `asap`: gathered, at most one mail an hour (`webthreads.notify`). `daily`: one mail at the morning's digest hour (`DIGEST_HOUR`, `DIGEST_TZ`). `weekly`: that mail on Mondays. `off`: none. Listed when some channel of the belfry has a page on the web. |
| `mentions` | yes | yes | The digest of the chat messages that name them (`mentions.py`): `daily` at the digest hour, `weekly` on Mondays, `off`. There is no `asap`: the belfry has no immediate mention mail (the phone apps' push is another thing). A guest who came in from the web starts at `off` (so `value` is `off` while `default` says `daily`); members are at `daily` unless they changed it. |
| `requests` | when they can read the requests channel | guests of the requests channel | New **public** requests in the requests channel, never their own. `asap`: gathered, at most one mail an hour. `daily`: one mail at the digest hour. `off` (the default): none. Not listed when the belfry has no public requests channel or the account doesn't have it. |
| `events` | yes | yes | The **announcements** of the events they hold a ticket for (a host's message flagged as an announcement in the event's chat; `followers.py`), a few minutes after each. Not every message of the event's chat: the belfry mails no such thing. `off`: none. |

Not there, because the belfry sends no such mail: `messages` (direct messages are not mailed
to anyone) and `digest` (there is no digest of the activity). They appear when they exist.

**Always sent, whatever `events` says:** the ticket itself ("You're going"), "Cancelled",
the reminder the day before, and an answer to an application.

**`default` is what the belfry does for someone who never chose.** `events` is `asap`
because a ticket has always made its holder follow the event's announcements; the switch
did not change that.

### One store per setting

- `mentions` is the choice the mention digest's own footer page changes
  (`belfry.<domain>/mentions/<token>`): both read and write the same value.
- `events` is the account's follows of the events' groups (`ev-<d>`; what the chat's bell
  "Also e-mail me its announcements" and each mail's footer change, one event at a time).
  `off` stops all of them and keeps a new ticket from following; `asap` follows the events
  they hold a ticket for now. `value` says the account's switch: an event unfollowed alone,
  in the chat or from a mail, stays unfollowed and the switch still reads `asap`.
- `replies` and `requests` are register params (`belfry.notify.<contact>.<id>`), read by the
  senders. Nothing is stored for someone who never chose.
- Changing `replies` loses nothing: going to `daily` / `weekly`, the first digest starts
  where the hourly mails stopped; going back to `asap`, the next hourly mail carries what
  was waiting for the next digest. A daily mail looks back two days at most, a weekly one
  eight. Going to `requests: daily` starts from that moment.

### The mails

Each mail about replies or new requests ends with "Change or stop these e-mails": the
account's own page `https://belfry.<domain>/notifications/<token>` (also in
`List-Unsubscribe`), which shows the same items with a button per choice. Opening the link
changes nothing (mail scanners open links); a button does. The token is an HMAC of the
contact, and the page is the only thing it opens. The mention digest and the announcement
mails keep their own footer links, as before.

Mails go only to the account's own proven address, of an account with an active pass; one
mail per person per round, at most 25 requests listed.

**For members who never touch a setting nothing changed**: thread authors get the hourly
mail as before (now with the link to the page), the mention digest and the event follows are
as they were, and a reply written **in the chat** subscribes nobody to anything: only a
comment made from the website (a site session) makes its writer a recipient of `replies`.
The author of an older card (a thread posted for an address, before accounts) gets the
hourly mail as before; when an account has that address now, its `replies` choice counts.

## 4. Requests and comments (docs/requests.md)

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

## 5. Offers and the checkout (docs/sales.md)

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

## 6. Tickets

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

## 7. CORS

No cookies anywhere; nothing is ever answered with `Access-Control-Allow-Credentials`.

- The apex `https://<domain>`: every `/api/v1/*` path, as before.
- `https://preview.<domain>` (anyone's draft site): what it could call before (the sales
  endpoints, reading events and requests), plus `login/start`, `login/verify`, `site/me`,
  `site/profile`, `site/logout`, `site/notifications`. Still **not**: a comment or an edit on a request,
  `events/<d>/join`.

A preview is another origin: its browser key is its own, and a session made there is not the
real site's.

## 8. What a site session can and cannot do

Can, for its own account only: read `/site/me`; change name, about, picture, tags; choose
which e-mails it gets; comment on public
requests and edit its own comments, title and text; open a request (an offer); order through
the checkout and read its own orders; check a code; take a free open ticket; read events as
the account; log out.

Cannot: get a signer session or a `bunker_url`; read or post in the chat; send or read
messages or mail; invite; administer anything (an admin's site session has no admin rights
anywhere); change the account's e-mail address or username; apply for a ticket; reach any
endpoint not named in this document as the account.

## 9. Known limits, not verified

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
- A preview's session can also change which e-mails the account gets (it can already change
  the profile).
- Someone who gets a reply both as a mention (the reply's `p` tag names the thread's author)
  and as `replies` reads it twice when both are on: the mention digest and the thread mail
  are separate mails, as before.
- `replies` covers the threads opened from the web and the comments made from the website.
  Threads and replies written in the chat are not mailed to their writers.
- A guest's tags reach the chat only through `POST /whois` (`tags`): an app that doesn't
  read it shows none. Tags that could not be carried over when a guest became a member (relay
  or signer down, own key) are not retried.
- Not run: a member's `belfry:profile` event written by the server and read back by the
  chat on a live relay (the test uses an event as the chat writes it); any of these mails
  through a real mail provider.
- Nothing here ran against a live signer, relay, Stripe or Odoo.
