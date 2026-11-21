# The public requests and an event's activity, as JSON

For a community's own website (the nsite on the apex), which renders three pages itself, in its
own look: the list of requests (like GitHub issues), one request's thread with its approval, and
an event's public activity log. The data is on the groups relay, which wants authentication even
for public channels, so a browser can't read it: the belfry serves it, read only, plus what a
request's author does from the web: a comment, and editing their own title, text and comments. Code: `webrequests.py` (the reading and
the permissions are `webthreads.py`'s, the HTML pages of the same channel); offers and orders:
`sales.py`, [sales.md](sales.md).

Base: `https://belfry.<domain>/api/v1`. No account, no cookie. Errors are
`{"error": "<code>", "message": "…"}`.

**Names only.** No answer here holds a key (hex or npub) or an e-mail address. A person with no
profile name is "Somebody"; a "name" that is itself a key or an address is "Somebody" too. Text
(`title`, `body`, `text`, `note`) is the plain text / Markdown the thread holds, as its author
wrote it: never HTML. The website renders it and must escape it.

**CORS.** The apex (`https://<domain>`) may call everything, with the request headers
`Authorization, Content-Type, X-Thread-Key`; a page's script may read the answer's headers
`X-Belfry-Replayed` and `Retry-After` (`Access-Control-Expose-Headers`). The website's previews (`https://preview.<domain>`)
may call the three `GET`s only, never the comment, the edit or the reaction.

**Limits.** 120 calls a minute per IP over these six endpoints (429 `too_many`). What is read
from the relay is kept 15 seconds in the service; the list and the activity say
`Cache-Control: public, max-age=15`, one request says `no-store` (the same address answers its
author with more). A comment, an edit or a decision made through the belfry shows at once.

**When the relay can't be read: 503 `relay`** (2026-11-21). The requests live on the groups
relay, an event's activity on the community relay too. When one of them does not answer
(refused, timed out, closed the question), every endpoint here answers
`503 {"error": "relay", "message": "…"}` with `Retry-After: 5`, `Cache-Control: no-store` and
the CORS headers: **never** a 200 with an empty list, a request without its comments or its
reactions, "open" for a request whose labels could not be read, or 404 `none` / `unknown`
because the channel's settings or the thread could not be read. A failed read is never kept:
the next call asks the relay again (an answer read **before** the failure may still be served
for its 15 seconds). A write (§3, §4, §4c) answers 503 `relay` too when the relay does not
take the event or what it needs can't be read; nothing is posted then. The signer failing is
not this: 503 `signer` / `unavailable`, as before; a name the register can't give is
"Somebody", as before. Code: `retries.py`; [website-accounts.md](website-accounts.md)
"Retries".

**`client_id` on the writes** (2026-11-21): `POST …/comments`, `…/edit`, `…/react` (and
`POST /offers`) take an optional `"client_id"` in the JSON body: 8–64 characters of
`A-Z a-z 0-9 -` (else 422 `invalid`). The same call with the same id by the same author within
24 hours answers the **first success again**, same status and body, with the header
`X-Belfry-Replayed: 1`, and does nothing else (no second comment, no mail, no count towards a
limit). While the first is still running: 409 `in_progress`. Only 2xx answers are kept: after
an error (a 503 `relay` too) the retry runs for real. The full rule:
[website-accounts.md](website-accounts.md) "Retries".

## 1. `GET /requests`

The threads of the **requests channel**: the channel picked in the console (Sales → requests
channel), while its web page is on and public. Only what was posted while the channel was
public (webthreads' `periods`). 404 `none` when there is no such channel, or its page is off or
members only.

Query: `state` = `open` | `approved` | `closed` | `all` (default `all`; `closed` covers declined
and closed); `q` = words looked for in the title and the body as they are now (after their author's
edits), whatever the case or the accents, at most 100 characters; `type` = one type (below:
`enquiry` | `sponsoring` | `talk` | `workshop` | `meetup` | `in_kind` | `sponsored_ticket`; only the requests
of that type, and `counts` then counts only those); `before` = a unix time (only threads made before it); `limit` = 1–100
(default 30). Anything else (an unknown type, two types): 422 `invalid`.

```json
{"slug": "requests", "channel": "Requests",
 "counts": {"open": 12, "approved": 5, "closed": 3},
 "items": [
  {"id": "<64 hex>", "number": 42,
   "title": "Kim Cook: 50% off Two-week pass in kind",
   "type": "in_kind", "kind": "in_kind", "label": "food", "days": ["2027-01-26", "2027-01-27"],
   "percent_asked": 50,
   "state": "approved", "percent_approved": 75, "approved_by": {"name": "Ada"},
   "author": {"name": "Kim Cook", "role": "guest"},
   "created_at": 1795000000, "edited_at": null, "comments": 3, "last_at": 1795003600,
   "reactions": [{"emoji": "👍", "count": 3}, {"emoji": "🎉", "count": 1}]}],
 "next": 1795000000}
```

- `channel`: the channel's name (not its id). `slug`: its page's slug.
- `counts`: over the threads that match `q`, whatever `state`, `before` and `limit`. `closed`
  counts declined and closed together.
- `items`: newest first (by `created_at`). `next`: the `before` of the next page, or `null`. A
  page can hold a few more than `limit` when several threads were made in the same second.
- `id`: the thread's root event id, an opaque identifier for URLs. `number`: a short number per
  channel ("#42"), given the first time the belfry sees the thread, in the order the threads
  were made, kept in `sales.sqlite` (table `request_numbers`): it never changes and is never
  given again, also when older threads are deleted. (Threads made in the same second are
  numbered in the order of their ids.) Every message at the top of the channel is a thread and
  takes a number, also one that is not public: public numbers can have gaps.
- `title`: the title the thread has now: the person's own (`"title"` of `POST /offers`,
  [sales.md](sales.md) §4) or the generated one, after its author's edits (§4). `edited_at`:
  when the author last edited the title or the text (unix), else `null`.
- `type` (2026-11-21): what the request is, on every item: `enquiry` | `sponsoring` | `talk` |
  `workshop` | `meetup` (plain requests: a thread that is open or closed, nothing to approve) |
  `in_kind` | `sponsored_ticket` (offers: they ask for a pass or a ticket, see `kind`). It is
  the `"type"` of `POST /offers` ([sales.md](sales.md) §4), kept on the offer and as the tag
  `["request-type", <type>]` on the thread's message. Threads from before types existed: an
  offer of kind `in_kind` is `in_kind`, of kind `sponsored` is `sponsored_ticket`; everything
  else, also a thread started in the chat, is `enquiry`. `workshop` is a type of its own
  since 2026-11-21; until then a talk's shown name was "Talk or workshop" (now "Talk"):
  threads made as `talk` stay `talk`, and an older card's line "Talk or workshop" is still
  read as its type line (never part of `body`, kept as it is by an edit).
- **A plain request** (`enquiry`, `sponsoring`, `talk`, `workshop`, `meetup`): `state` is `open` or
  `closed`, never anything else; `kind` is `other`; `label`, `ticket`, `ticket_title`,
  `event_link`, `event_title` and `approved_by` are `null`; **every percent field is JSON
  `null`** (`percent_asked`, `percent_approved`: present, never 0, never left out), open or
  closed. `days` when its author gave some.
- `kind`: for an offer (type `in_kind` or `sponsored_ticket`), what it said: `in_kind` (the default)
  or `sponsored`; else `other`. `label` never says sponsored. `label`, `days`, `percent_asked`
  come from the offer (`null` for `other`, or when the offer has none).
- `state`: `approved` / `declined` once an admin decided the offer (that wins over a label);
  else `closed` when the latest label on it says closed (NIP-32 `belfry/inbox`: a member's in
  the chat, or the organisers' Close, §4b); else `open`. `percent_approved` and `approved_by` only when approved.
- `author.role`: `organiser` (an admin of the belfry, or the belfry itself), `member`, `agent`,
  `guest` (anyone else: someone from outside, with or without an account).
- `comments`: the public replies, without the belfry's own reply that says the decision.
  `last_at`: the latest of the thread, its comments and its decision.
- `reactions`: the emoji on the thread's opening message, `[{"emoji", "count", "mine"?}]`,
  most used first, then first used; `[]` when none (§4c). `"mine": true` only in an answer to
  a signed request, on the emoji the signer used; never in an anonymous answer.

## 2. `GET /requests/<id or number>`

The item of §1, plus:

```json
{"body": "I cook for 40 people on two evenings.", "edited_at": 1795000050,
 "timeline": [
  {"type": "edited", "field": "title", "by": {"name": "Kim Cook"}, "at": 1795000050,
   "title": "Dinner for forty"},
  {"type": "comment", "id": "<64 hex>", "author": {"name": "Mia", "role": "member"},
   "at": 1795000100, "edited_at": null, "text": "Which evenings?",
   "reactions": [{"emoji": "🙏", "count": 1}]},
  {"type": "comment", "id": "<64 hex>", "author": {"name": "Kim Cook", "role": "author"},
   "at": 1795000200, "edited_at": 1795000300, "text": "Tuesday and Friday.", "mine": true},
  {"type": "edited", "field": "comment", "by": {"name": "Kim Cook"}, "at": 1795000300,
   "comment": "<64 hex>"},
  {"type": "approved", "by": {"name": "Ada"}, "percent": 75, "note": "See you there.",
   "at": 1795003600},
  {"type": "closed", "by": {"name": "Mia"}, "at": 1795090000},
  {"type": "reopened", "by": {"name": "Mia"}, "at": 1795093600}],
 "me": {"author": true, "ticket_url": "https://<domain>/tickets"}}
```

- `timeline`, oldest first. A comment's `author.role` is `author` when it is by whoever opened
  the thread, else as in §1. `approved` (with `percent`) / `declined`: the admin's decision, by
  the admin's name; `note` (optional) is the admin's public note. `closed` / `reopened`: the
  labels on the thread (a label that changes nothing is left out): a member's in the chat, by
  that member's name, or the organisers' Close / Reopen (§4b), by the admin's name. Every
  `comment` entry carries its own `reactions` (as on the request; `[]` when none).
- `title`, `body` and a comment's `text` are the current ones. `edited_at` (the opening post:
  its title or text; a comment: its text) is when its author last edited it, else `null`.
  A comment's `id` is an opaque identifier (for §4). `"mine": true` marks the comments the
  author may edit, **only** in an answer proven as the author's (see `me`); never otherwise.
- `edited` entries, one per change: `field` = `title` (with the new `title`), `body` or
  `comment` (with the `comment`'s id); `by`: who wrote the message. An edit of the title and
  the text at once gives two entries with the same `at`. Older versions are not in any answer
  (they stay on the relay: the first message and each edit).
- 404 `unknown`: no such thread, or one that is not public (asked without its author's proof).
- **`me`**, only when the request proves it comes from the thread's author:
  - a NIP-98 `Authorization` signed by the key that made the offer (`POST /offers`), or
  - the header `X-Thread-Key: <k>`: the key of the thread's own link, the one mailed to its
    author (`…#k=<k>`, or `?k=<k>` on the belfry's own page).

  The key is **never** read from the query string (query strings end up in logs). A wrong or
  missing proof is not an error: the answer just has no `me`. With the proof, the author also
  reads their thread when it is not public, and its replies.
- `me.ticket_url`: where an approved author goes on for their pass. `GET /offers/<id>` never
  gives the personal code (it is only in the private mail to the requester), so neither does
  this. It is the console's "Where people buy tickets" address when that is one page for
  everything (no `{link}` / `{id}` in it); absent otherwise, and for anyone not approved. The
  author types the code from their mail there.
- **An offer for an event ticket** (docs/sales.md §10) says more in `me`:
  `{"author": true, "offer": "f_…", "ticket": "<the ticket's d>", "event": "<the event's link>",
  "state": "open" | "accepted" | "declined", "redeemed": false, "ticket_url"?}`. `redeemed`:
  an order holds the approval (made with it, paid or still to pay; false again when that order
  ends unpaid). `ticket_url`, once accepted and when the console's "Where people buy tickets"
  template is set: the template filled for that event and ticket (`{link}`, `{id}`) with
  `offer=<offer id>` appended (`&offer=`, or `?offer=` when the address has no query). The page
  there sends `POST /checkout {"ticket", "offer", …}` with the same proof (the key, or
  `X-Thread-Key`): no code to type. Never a code in this answer.

## 3. `POST /requests/<id or number>/comments`

`{"text": "…", "client_id"?: "…"}` (`text`: 1–8000 characters; `client_id`: a retry is
answered again, see above), by the thread's author only, proven as for `me`: NIP-98 by
the offer's key (with the `payload` tag), or `X-Thread-Key`. By the thread's id or its number.
The comment is posted as the web page posts it: by the author's own account when the
thread was opened by one, else by the groups key for their address. → 201 `{"ok": true}`.

Errors: 403 `not_author` (no proof, or someone else's); 401 `unauthorized` (an `Authorization`
that is not a valid NIP-98); 404 `unknown`; 422 `invalid` (no text, or too long); 400
`invalid_json`; 429 `too_many` (the page's own limit: 20 comments an hour per author); 503
`unavailable` (the signer); 503 `relay` (the relay: nothing posted); 409 `in_progress` (the
same `client_id`, still running).

## 4. `POST /requests/<id or number>/edit`

The author changes their request, proven as for a comment (NIP-98 by the offer's key, or
`X-Thread-Key`). Exactly one of:

(Both forms take an optional `"client_id"` besides: a retry is answered again, see above.)

- `{"title"?: "…", "body"?: "…"}`, at least one: the thread's title (one line, 1–120
  characters once trimmed, control characters taken out: the rule of `POST /offers`'s `title`)
  and / or its opening text (1–8000 characters);
- `{"comment": "<comment id>", "text": "…"}` (1–8000 characters): one of the author's **own**
  comments: a reply signed by the key that signed the thread (on an older card, which the
  belfry's posting key wrote: one for the author's address). Never an organiser's or a
  member's.

→ 200 `{"ok": true, "changed": true}`; `"changed": false` when the edit changes nothing (no
event is published, and it does not count towards the limit).

Errors: 403 `not_author` (no proof, or someone else's); 403 `not_yours` (a comment that is not
the author's); 404 `unknown` (no such thread, no such comment under it); 422 `invalid`
(neither or both forms, a title or text outside its rule); 409 `archived` (the channel is
archived); 429 `too_many` (20 edits an hour per thread); 401, 400, 503 (`unavailable`,
`relay`) and 409 `in_progress` as in §3.

A closed, declined or approved request can still be edited, like an issue. An approval stays;
the organisers' console says "edited after approval" on that request's row.

**On the relay** an edit is what the chat's "Edit message" is: a kind 1010 event in the
channel, `["h", <channel>]`, `["e", <the message's id>]`, `["k", "9" | "1111"]`, the whole new
text as content, signed by the key that signed the message (the author's own account, or the
belfry's posting key for an older card); only that key's edits count, the latest wins. The
message's other tags ride along (whose thread, which offer; never the buttons). A thread's
title is its `subject` tag **and** the bold first line of its text (`**<title>**`, a blank
line, the text): an edit sets both, so the page and the chat read the same title. An edit made
in the chat (which sends no `subject`) is read by its bold first line. Members in the chat see
the new text; the first version and every edit stay on the relay.

As for everything else on the page, moving the channel is never retroactive: an edit made
while the channel was members only shows to the author (with their proof) and in the chat,
not in the public answers, which keep the last version written while it was public.

## 4b. Closing a plain request

A plain request (`enquiry`, `sponsoring`, `talk`, `workshop`, `meetup`; also a thread started in the chat)
is closed and reopened by the organisers: the belfry's admins, the people who decide offers.
There is no endpoint for the website. Two ways:

- **the console**, belfry.<domain>/sales, the requests' list: a Close or Reopen button on each
  plain request made through `POST /offers` (a form with the csrf; the page answers with a
  fixed message);
- **the card's button in the chat**: a plain request's card carries
  `["button", "close", "Close", "", "belfry:request-close"]` ([actions.md](actions.md) §4).
  The belfry answers "Closed by <name>." and the button is finished for everyone. (After a
  reopen the card's button stays finished: closing again is the console's, or a label in the
  chat.)

Either one publishes what a member's close in the chat is, signed by the belfry's groups key
for the admin: a kind 1985 in the channel, `["L", "belfry/inbox"]`,
`["l", "closed" | "open", "belfry/inbox"]`, `["e", <the thread's message>]`, `["h", <channel>]`,
`["by", <the admin's key>]` (read only on the groups key's own labels: it names who closed).
The record is those events on the relay, nothing else: the latest label is the state, and each
change is a `closed` / `reopened` entry of the timeline. Closing what is closed changes
nothing (no event). A closed request still shows, in the list (`state=closed`) and by itself;
comments, edits and reactions stay allowed. `GET /offers/<id>` says `"state": "closed"` for it.

An offer (`in_kind`, `sponsored_ticket`) is not closed this way: it is approved or declined
(`belfry:request-close` on its card answers `no_target`; the console shows no Close for it).

## 4c. Reactions: `POST /requests/<id or number>/react`

`{"emoji": "👍", "comment"?: "<comment id>", "remove"?: true, "client_id"?: "…"}` → 200
`{"reactions": [{"emoji", "count", "mine"?}]}`: the reactions of the request (or of that
comment) as they are now, for the person who asked.

- **Who**: someone logged in who may comment (`viewer.can_comment`): NIP-98 by a website
  session ([website-accounts.md](website-accounts.md)), or by the own key of an account that
  has the channel. The thread's author by `X-Thread-Key` or by the offer's key alone, with no
  account: 403 `not_allowed` (a reaction is signed as a person). Nothing that proves an
  account: 401 `no_session`.
- **`emoji`**: exactly one emoji: one emoji sequence (a flag, a skin tone, a family joined by
  zero-width joiners count as one), at most 32 UTF-8 bytes, no ASCII letter, digit or
  whitespace. `"+"` and `"-"` are taken as 👍 and 👎. Anything else: 422 `invalid`.
- **`comment`**: a comment's `id` from the timeline; one that is not under this request: 422
  `invalid`. Without it the reaction is on the request itself.
- The same emoji twice: 200, unchanged, no second event. `"remove": true`: takes the person's
  own reaction(s) with that emoji away; with none: 200, unchanged. (`remove` that is not a
  boolean: 422 `invalid`.)
- Errors: 401 `no_session`; 401 `unauthorized` (a bad NIP-98); 403 `not_allowed`; 404 `unknown`
  (no such request, or one that is not public and not theirs); 422 `invalid`; 409 `own_key`
  (someone who holds their own key: the belfry can't sign for them; they react in the chat);
  409 `archived`; 429 `too_many` (60 reactions an hour per person; a call that changes nothing
  never counts); 400 `invalid_json`; 503 `signer` / `unavailable`; 503 `relay` (the relay
  did not take it, or the request could not be read: nothing changed); 409 `in_progress`.
- CORS: the apex only, as for a comment; never a preview.

**On the relay** a reaction is what the chat's is: a kind 7 in the channel signed **as the
person** through the signer, the emoji as content, `["h", <channel>]`,
`["e", <the message or the comment>]`, `["p", <its author>]`, `["k", "9" | "1111"]`. Taking
one away is a kind 5 by the person: `["h", <channel>]`, `["e", <their reaction>]` (one per
reaction), `["k", "7"]`.

**What is counted** (`reactions` everywhere): every kind 7 in the channel on that message
(its last `e` tag), whoever made it and wherever (the chat, the apps, this endpoint), each
person once per emoji, without those their own author deleted (a kind 5 by the same key).
Content `"+"` (or empty) counts as 👍, `"-"` as 👎; content that is no emoji (text, a
`:shortcode:`, more than 32 bytes) is left out, nothing else is. Order: `count` descending,
then the emoji first used. `mine` (always `true` when present) only for the key that signed the read:
a website session's account, or an account's own key.

## 5. `GET /events/<link>/activity`

The public activity of a **public** event (`<link>`: its address or its id), newest first.
A members-only event, one seen only by its link, or none: 404 `not_found`.

Query: `type` = `tickets` | `requests` | `program` (default: everything); `before`, `limit` as
in §1.

```json
{"stats": {"villagers": 131, "open_requests": 12, "in_kind_passes": 5, "sponsored_passes": 2},
 "items": [
  {"type": "ticket", "at": 1795003700, "who": {"name": null}, "ticket": {"title": "Day pass"}},
  {"type": "request_approved", "at": 1795003600, "who": {"name": "Kim Cook"}, "percent": 75,
   "request": {"id": "<64 hex>", "number": 42, "title": "…", "label": "food"}},
  {"type": "request_comment", "at": 1795000100, "who": {"name": "Mia"}, "request": {…}},
  {"type": "request_opened", "at": 1795000000, "who": {"name": "Kim Cook"}, "request": {…}}],
 "next": null}
```

- **`ticket`**: a paid order (state `paid` or `active`) of a ticket of this event
  ([sales.md](sales.md) §10). For an event other events point to with an `a` tag (a village
  event and its day events: each day event's NIP-52 event carries
  `["a", "<kind>:<creator>:<d of the village event>"]`), also the orders of those events'
  tickets. `ticket.title`: the ticket's name. Never an amount. A tester's test order never
  shows; neither does an unpaid one, or one refunded.
- **`request_opened`, `request_comment`, `request_approved`**: the requests channel's public
  threads (§1), on **the event the requests belong to**: the event other events point to with
  an `a` tag; when no event is pointed to, every public event. `request_approved` carries
  `percent`. A declined request has no item.
- **`program`**: nothing yet (no source exists); the filter answers no items.
- **`stats`** (whatever the filter and the page): `villagers` = people with a paid ticket of
  the event or of its day events (each once); `open_requests` = public threads that are open;
  `in_kind_passes` = approved offers in kind, `sponsored_passes` = approved offers of kind
  `sponsored`. The last three are 0 on an event the requests don't belong to. An offer for an
  event **ticket** belongs to that ticket's event (and to the event its day events point to),
  wherever the other requests belong: its items and its counts show there and nowhere else.

### The privacy rule

`who.name` of a buyer or a requester is their name **only** when their order or offer carries
their consent: the boolean `public_name` of `POST /checkout` and `POST /offers` (default
`false`; 422 `invalid` when it is not a boolean; [sales.md](sales.md) §4). Without it
`who.name` is `null` (the site writes "A villager"), and a request's `title` here leaves out
the name an offer's title starts with. Never an e-mail address, never an amount.

- A comment by the thread's author follows the author's consent. A comment by a member, an
  organiser or an agent says their name (they answer in public under it); by anyone else,
  `null`.
- A thread that is not an offer (opened in the chat or on the page) has no consent: `null`.
- An order or offer that asked `erase_after_event` shows while the event is not over (its name
  under the same rule) and is left out of the items once the event is over.
- The requests page itself (§1, §2) is public by its own rule: a requester is told, before
  anything is posted, that the request and their name will be public there.

## 6. Handing the page over to the website

By default the belfry serves the channel's page itself, as HTML: `<domain>/<slug>` and
`<domain>/<slug>/<thread id>` (webthreads.py). The channel's web setting (the JSON of the
`belfry:web:<channel>` event, kind 30078) takes one more field:

```json
{"on": true, "slug": "requests", "visibility": "public", "page": "https://<domain>/requests"}
```

`page` is where the community's website shows the list. It counts only when it is `https`, on
the belfry's own domain (the apex: not a subdomain), with no port, user, query or fragment; it
is checked where the setting is read (clients write it): anything else is ignored, logged once,
and the belfry keeps serving the page. A trailing `/` is dropped. With a valid `page`:

- The belfry answers **no HTML** at `/<slug>` and under it: those requests fall through to the
  website. Two exceptions, both links people hold in a mail:
  - `GET /<slug>/confirm/<token>`, the confirmation link, stays the belfry's (it posts the
    thread) and then sends the person to their thread on the website;
  - `GET /<slug>/<64-hex thread id>`, a thread link mailed before the hand-over, answers 302 to
    the thread on the website, in the shape below (its `?k=` moved to the fragment).

  So the website must not use those two shapes of path under `/<slug>` for its own pages.
- Every link the belfry builds for the channel points to the website, on the list's own page:
  the list is `<page>`, a thread is `<page>?id=<number>` (the request's number, §1), and its
  author's own link is `<page>?id=<number>#k=<thread key>`. The page asks
  `GET /requests/<number>`. The key rides in the **fragment**, so it is never sent to a server
  or written in a log; the page reads it and sends it as `X-Thread-Key` (§2–§4). (A thread
  the belfry has no number for, in a channel that is not the requests channel, keeps its id:
  `<page>?id=<64 hex>`.) This covers the confirmation mail, the "your thread is open" mail, the hourly mail
  with members' replies, the answer to an offer, and `url` / `my_url` of `GET /offers/<id>`.
- The channel stays the sales requests channel; `GET /products` keeps `"offers": true`;
  `POST /offers` works as before.

Links stored before the hand-over (`url` / `my_url` of an older offer) keep their old shape;
they still work through the 302 above. Links made until 2026-11-19 had the shape
`<page>/thread?id=<64 hex>#k=…`: the belfry never serves those (the path is the website's), so
the website keeps answering them, or sends them to `<page>?id=…`. Taking `page` out of the
setting gives the HTML pages back; links of the `<page>?id=…` shape are then the website's to
handle. Without a `page`, links are as they were (`<domain>/<slug>/<id>`, `?k=`); that HTML
page shows an edited title or text with a small "edited" mark.

## Logged in on the website (2026-11-20)

With a website session ([website-accounts.md](website-accounts.md)) the same five endpoints
answer for the account, signed (NIP-98) by the session's key, with no `X-Thread-Key`:

- `GET /requests`: the same public list, each of the account's own requests with
  `"mine": true`; `Cache-Control: no-store`.
- `GET /requests/<id or number>`: `me` as for the author when the request is theirs (they made
  its offer, they signed the thread, or it is an older card written for their proven address);
  `"mine": true` on every comment they signed, also under someone else's request; and
  `"viewer": {"role": "admin" | "member" | "guest", "author": bool, "can_comment": bool}`.
- `POST …/comments`: besides the author, anyone whose account **has the requests channel**: a
  guest of it (everyone who logs in on the website is one), a member when members read the
  channel (it is open, or they are in it), an admin. Signed with their own key through the
  signer. 403 `not_allowed` otherwise; 409 `own_key` for someone who holds their own key; 429
  `too_many` (20 comments an hour per account); 409 `archived`.
- `POST …/edit`: the author as before; anyone else only `{"comment", "text"}` for a comment
  they signed (403 `not_author` for a title or a text, 403 `not_yours` for someone else's
  comment).

A **guest** with no profile shows under the name on their account (the one they gave when they
logged in or opened their thread; "Guest" when they gave none). A member with no profile stays
"Somebody", as before. `POST /site/profile {"name"}` sets the profile's name and the
account's, so what they wrote before reads under the new name.

## Not in this version

- `program` items.
- Tickets taken for free on an event's page (no order) are not in the activity; nor are
  membership passes bought as products (only event tickets are).
- Closing a request from the web (organisers close a plain one in the console or in the chat,
  §4b); comments by anyone who is not logged in on the website, but
  the author (members also answer in the chat); edits of anything but one's own title, text
  and comments.
- Nothing here was run against a live relay or a real website yet.

### Which ticket a request asks for (2026-11-20)

Every item (list and detail) carries `ticket` (the 30402's d tag), `ticket_title`, `event_link`
(the event's address: its slug, else its id) and `event_title`; all four are null for a
product's offer and for a plain thread.
