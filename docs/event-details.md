# Event details from an event's website

Contract for the apps (web chat, iPhone, Android). Server:
`services/onboarding/belfry_onboarding/eventdetails.py`. Requirement EV-61
(docs/requirements/events.md).

The event form asks for the **Event website** first. When the host pastes it, the app asks the
belfry for the page's details, shows **"Fetching event details…"** while it waits, then fills in
the empty fields: cover, title, start / end (all day, time zone), place (name + address) and
description. The host checks and changes them. Fields the host already typed are not replaced.

**The Join online link is never taken from a site.** When the page says the event is online the
answer only sets `online: true`; the call's address is never in the answer (it is also taken out
of the description).

The apps never fetch the site themselves (that would tell it who is making an event, from which
address): the belfry fetches it, keeps the answer, and serves the cover from its own address.

## The API

`GET https://belfry.<domain>/api/v1/event-details?url=<the website, URL-encoded>`

- NIP-98 (`u` = the whole URL with its query, `method` GET) by a key that may create events: an
  active pass that is not a guest's (members, and members' own agents). Guests, reading guests
  and website sessions get 403. CORS like the other API reads (`chat.<domain>`, the apex).
- 200:

```json
{
  "url": "https://example.org/event (after redirects)",
  "title": "Open Source Village 2027",
  "starts_at": "2027-01-25T10:00:00+01:00",
  "ends_at": "2027-02-05T18:00:00+01:00",
  "all_day": false,
  "timezone": "Europe/Brussels",
  "place": {"name": "Commons Hub", "address": "Rue de la Loi 1, 1000 Brussels, BE"},
  "online": false,
  "image": "https://belfry.<domain>/api/v1/link-preview/image/<64 hex>",
  "description": "Two weeks of building together.\nBring a laptop.",
  "organizer": "Commons Hub",
  "source": "jsonld"
}
```

- Every field but `url`, `title`, `all_day`, `online` and `source` may be `null`. In `place`,
  `name` or `address` may be `null` (never both: then `place` is `null`).
- **Times.** `all_day: true`: `starts_at` / `ends_at` are dates (`2027-01-25`), and `ends_at`
  is the **last day** (inclusive; a calendar file's exclusive DTEND is turned into it). Otherwise
  ISO 8601 date-times. With an offset (`+01:00`, `+00:00` for UTC): an exact moment. **Without
  an offset** (`2027-01-25T10:00:00`): the site gave its local time and no zone; take it as the
  wall-clock time in the zone the host picks (default: `timezone` if given, else the belfry's /
  device's zone). `timezone` is an IANA name when the site gave one (a calendar file's TZID or
  X-WR-TIMEZONE, schema.org `eventSchedule.scheduleTimezone`), else `null`; an offset alone never
  becomes a zone name. `ends_at` is `null` when the site gave none (or gave one all-day and the
  other not).
- `description`: plain text, tags stripped, paragraphs kept as `\n` / `\n\n`, list items as
  `• `, at most 4000 characters (cut at a word with `…`). Render as text, never HTML.
- `title` ≤ 200, place name / address ≤ 300, organizer ≤ 200 characters, one line each. A place
  or organiser that is only a web address is dropped.
- `image`: the belfry's picture proxy (the same as link previews, docs/link-previews.md: no
  sign-in, PNG / JPEG / WebP / GIF ≤ 2 MB, 404 when it can't be had). Never the site's address.
  To use it as the cover, the app downloads it and uploads it to Blossom like a picture the host
  chose.
- `source`: where the start came from (else the title): `jsonld`, `microdata`, `opengraph` or
  `ics`. For the app's information only.
- 404 `{"error": "none"}`: nothing usable (no title, not an HTML page, the site did not answer
  or answered an error, a redirect led somewhere not allowed). Say "No details found on this
  page" and leave the form as it is.
- 422 `{"error": "invalid"}`: an address the belfry never fetches (not `https`, a port other than
  443, credentials, an IP address, a private or single-label name, the belfry's own domain and
  its subdomains).
- 401 unsigned, 403 `not_member`, 429 `too_many` (60 a minute per caller), 503 `busy` (try once
  more after a few seconds).

The answer usually takes under a second when kept, up to about 10 s (20 s when a calendar file is
fetched too) when not: show the loading line all along and let the host keep typing.

## What the belfry reads

The page: `text/html` or XHTML, 5 s per request, at most 3 redirects (each checked again), the
first **1 MB** (link previews read 512 kB; JSON-LD often sits at the end of big pages). Each
field comes from the first of these that has it:

1. **schema.org JSON-LD** (`<script type="application/ld+json">`): the first `Event` (or a
   subtype: `MusicEvent`, `Festival`, `SocialEvent`, `EducationEvent`, … with or without a
   `schema:` / URL prefix), found at the top level, in arrays, in `@graph` or `mainEntity`;
   one with a `startDate` first. `name`, `startDate`, `endDate`, `location` (a `Place` with
   `name` and a `PostalAddress` or text address; a `VirtualLocation` or a web address only sets
   `online`), `eventAttendanceMode` (Online / Mixed → `online`), `image` (text, list or
   `ImageObject`), `description`, `organizer`.
2. **Microdata**: the first element with `itemscope itemtype=…/Event` (or a subtype): `name`,
   `startDate`, `endDate` (from `content` / `datetime`), `description`, `image`, and a nested
   `location` Place (`name`, `address`). RDFa is not read.
3. **Open Graph / Twitter**: `og:title` / `twitter:title` / `<title>`, `og:description` /
   `twitter:description` / `description`, `og:image` / `twitter:image`, and start / end from
   `event:start_time`, `event:end_time` (and `og:event:*`, `startDate` / `endDate` meta).
4. **A calendar file**, only when nothing above gave a start: a `<link rel="alternate"
   type="text/calendar">` or an `<a href="….ics">` **on the same host**, fetched with the same
   checks (256 kB). Its first VEVENT: SUMMARY, DTSTART / DTEND (date, UTC, or TZID; a TZID that
   is not an IANA name is read as local time), LOCATION (a web address → `online`; else "name,
   address" split at the first comma), DESCRIPTION, ORGANIZER's CN.

Kept by address (the one asked and the final one) for 6 hours; a page with nothing usable for
30 minutes. At most 4 sites fetched at once in all; the same address asked twice at once is
fetched once.

## Known limits

- A page that builds its content with JavaScript and has no JSON-LD or meta tags in the HTML gives
  only what its `<title>` says.
- An online address written in the description by the site in another form than the one in its
  `VirtualLocation` (or an online address in an Open Graph description) is not recognised.
- Sites that refuse unknown clients (bot walls) answer 404 `none`.
