# The public requests and an event's activity, as JSON

For a community's own website (the nsite on the apex), which renders three pages itself, in its
own look: the list of requests (like GitHub issues), one request's thread with its approval, and
an event's public activity log. The data is on the groups relay, which wants authentication even
for public channels, so a browser can't read it: the belfry serves it, read only, plus the one
thing a request's author does from the web, a comment. Code: `webrequests.py` (the reading and
the permissions are `webthreads.py`'s, the HTML pages of the same channel); offers and orders:
`sales.py`, [sales.md](sales.md).

Base: `https://belfry.<domain>/api/v1`. No account, no cookie. Errors are
`{"error": "<code>", "message": "…"}`.

**Names only.** No answer here holds a key (hex or npub) or an e-mail address. A person with no
profile name is "Somebody"; a "name" that is itself a key or an address is "Somebody" too. Text
(`title`, `body`, `text`, `note`) is the plain text / Markdown the thread holds, as its author
wrote it: never HTML. The website renders it and must escape it.

**CORS.** The apex (`https://<domain>`) may call everything, with the request headers
`Authorization, Content-Type, X-Thread-Key`. The website's previews (`https://preview.<domain>`)
may call the three `GET`s only, never the comment.

**Limits.** 120 calls a minute per IP over these four endpoints (429 `too_many`). What is read
from the relay is kept 15 seconds in the service; the list and the activity say
`Cache-Control: public, max-age=15`, one request says `no-store` (the same address answers its
author with more). A comment or a decision made through the belfry shows at once.

## 1. `GET /requests`

The threads of the **requests channel**: the channel picked in the console (Sales → requests
channel), while its web page is on and public. Only what was posted while the channel was
public (webthreads' `periods`). 404 `none` when there is no such channel, or its page is off or
members only.

Query: `state` = `open` | `approved` | `closed` | `all` (default `all`; `closed` covers declined
and closed); `q` = words looked for in the title and the body, whatever the case or the accents,
at most 100 characters; `before` = a unix time (only threads made before it); `limit` = 1–100
(default 30). Anything else: 422 `invalid`.

```json
{"slug": "requests", "channel": "Requests",
 "counts": {"open": 12, "approved": 5, "closed": 3},
 "items": [
  {"id": "<64 hex>", "number": 42,
   "title": "Kim Cook: 50% off Two-week pass in kind",
   "kind": "in_kind", "label": "food", "days": ["2027-01-26", "2027-01-27"],
   "percent_asked": 50,
   "state": "approved", "percent_approved": 75, "approved_by": {"name": "Ada"},
   "author": {"name": "Kim Cook", "role": "guest"},
   "created_at": 1795000000, "comments": 3, "last_at": 1795003600}],
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
- `kind`: for a thread started by `POST /offers`, what the offer said: `in_kind` (the default)
  or `sponsored`; else `other`. `label` never says sponsored. `label`, `days`, `percent_asked`
  come from the offer (`null` for `other`, or when the offer has none).
- `state`: `approved` / `declined` once an admin decided the offer (that wins over a label);
  else `closed` when the members' latest label on it in the chat says closed (NIP-32
  `belfry/inbox`); else `open`. `percent_approved` and `approved_by` only when approved.
- `author.role`: `organiser` (an admin of the belfry, or the belfry itself), `member`, `agent`,
  `guest` (anyone else: someone from outside, with or without an account).
- `comments`: the public replies, without the belfry's own reply that says the decision.
  `last_at`: the latest of the thread, its comments and its decision.

## 2. `GET /requests/<id or number>`

The item of §1, plus:

```json
{"body": "I cook for 40 people on two evenings.",
 "timeline": [
  {"type": "comment", "author": {"name": "Mia", "role": "member"}, "at": 1795000100,
   "text": "Which evenings?"},
  {"type": "comment", "author": {"name": "Kim Cook", "role": "author"}, "at": 1795000200,
   "text": "Tuesday and Friday."},
  {"type": "approved", "by": {"name": "Ada"}, "percent": 75, "note": "See you there.",
   "at": 1795003600},
  {"type": "closed", "by": {"name": "Mia"}, "at": 1795090000},
  {"type": "reopened", "by": {"name": "Mia"}, "at": 1795093600}],
 "me": {"author": true, "ticket_url": "https://<domain>/tickets"}}
```

- `timeline`, oldest first. A comment's `author.role` is `author` when it is by whoever opened
  the thread, else as in §1. `approved` (with `percent`) / `declined`: the admin's decision, by
  the admin's name; `note` (optional) is the admin's public note. `closed` / `reopened`: the
  members' labels in the chat (a label that changes nothing is left out).
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

## 3. `POST /requests/<id>/comments`

`{"text": "…"}` (1–8000 characters), by the thread's author only, proven as for `me`: NIP-98 by
the offer's key (with the `payload` tag), or `X-Thread-Key`. By the thread's **id** (not its
number). The comment is posted as the web page posts it: by the author's own account when the
thread was opened by one, else by the groups key for their address. → 201 `{"ok": true}`.

Errors: 403 `not_author` (no proof, or someone else's); 401 `unauthorized` (an `Authorization`
that is not a valid NIP-98); 404 `unknown`; 422 `invalid` (no text, or too long); 400
`invalid_json`; 429 `too_many` (the page's own limit: 20 comments an hour per author); 503
`unavailable` (the signer).

## 4. `GET /events/<link>/activity`

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

## 5. Handing the page over to the website

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
    the thread on the website (its `?k=` moved to the fragment).

  So the website must not use those two shapes of path under `/<slug>` for its own pages.
- Every link the belfry builds for the channel points to the website: the list is `<page>`, a
  thread is `<page>/thread?id=<id>`, and its author's own link is
  `<page>/thread?id=<id>#k=<thread key>`. The key rides in the **fragment**, so it is never
  sent to a server or written in a log; the page reads it and sends it as `X-Thread-Key`
  (§2, §3). This covers the confirmation mail, the "your thread is open" mail, the hourly mail
  with members' replies, the answer to an offer, and `url` / `my_url` of `GET /offers/<id>`.
- The channel stays the sales requests channel; `GET /products` keeps `"offers": true`;
  `POST /offers` works as before.

Links stored before the hand-over (`url` / `my_url` of an older offer) keep their old shape;
they still work through the 302 above. Taking `page` out of the setting gives the HTML pages
back; links of the `<page>/thread?id=…` shape are then the website's to handle.

## Not in this version

- `program` items.
- Tickets taken for free on an event's page (no order) are not in the activity; nor are
  membership passes bought as products (only event tickets are).
- Editing or closing a request from the web; comments by anyone but the author (members answer
  in the chat).
- Nothing here was run against a live relay or a real website yet.
