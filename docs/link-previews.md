# Link previews

Contract for the apps (web chat, iPhone, Android). Server: `services/onboarding/belfry_onboarding/linkpreview.py`.

**Superseded on 2026-11-24 by docs/link-previews-v2.md (previews built by the sender; readers never fetch). The endpoint below stays for web senders only.**

A message with an outside `https://` link shows a preview box under it: the page's picture,
title, description and site name (Open Graph). **The apps never fetch an outside page or picture
themselves**: that would tell the site who reads the message, from which address. They ask the
belfry, which fetches once a day per page and serves the picture from its own address.

## When to ask

- For each `https://` link in a message that is not the belfry's own (its domain and every
  subdomain: those have their own in-app previews, docs/one-shape.md) and not a Blossom file
  already shown as an attachment. At most the first 3 links of a message.
- When the message is on screen, not for the whole history. Keep the answer on the device
  (a day for a preview, an hour for `none`); the belfry caches too.
- Not in encrypted conversations (DMs, Notes, private groups) unless the person turned previews
  on there: asking tells the belfry which link was in the message. (v1: never in them.)

## The API

`GET https://belfry.<domain>/api/v1/link-preview?url=<the link, URL-encoded>`

- NIP-98 (`u` = the whole URL with its query, `method` GET) by a key with an active pass
  (member, agent, guest) or a website session (docs/website-accounts.md). CORS: `chat.<domain>`
  and the apex (`https://<domain>`), like the other read endpoints.
- 200:

```json
{
  "url": "https://example.org/after-redirects",
  "title": "Bread & Butter",
  "description": "Fresh bread every morning",
  "site_name": "The Bakery",
  "image": "https://belfry.<domain>/api/v1/link-preview/image/<64 hex>",
  "image_width": 1200,
  "image_height": 630
}
```

  `title`, `description`, `site_name`, `image` may each be `null`; `image_width` /
  `image_height` only when the page states them (use them to reserve the box's height). Text is
  plain (entities decoded, tags stripped, spaces collapsed): title ≤ 200, description ≤ 300,
  site name ≤ 100 characters. Render it as text, never as HTML.
- 404 `{"error": "none"}`: nothing to show (no title and no description, not an HTML page, the
  site did not answer, a redirect led somewhere not allowed). Show the plain link.
- 422 `{"error": "invalid"}`: an address the belfry never fetches (below). Show the plain link.
- 401 unsigned, 403 `no_pass`, 429 `too_many` (60 a minute per caller), 503 `busy`.

`GET https://belfry.<domain>/api/v1/link-preview/image/<hash>`: the picture, no sign-in (load it
as an image). PNG, JPEG, WebP or GIF, ≤ 2 MB, `Cache-Control: public, max-age=604800`,
`Cross-Origin-Resource-Policy: cross-origin`. 404 when there is none (draw the box without a
picture). `<hash>` is an HMAC of the picture's address with the belfry's secret: it opens only
pictures a preview named, so this is no open proxy. Pictures are kept on disk next to the
register (`link-previews/`) for 7 days.

Both live under `belfry.<domain>/api/*`, which Caddy limits to 60 requests a minute per address:
ask for previews a few at a time, and treat 429 as "later".

## What the belfry fetches, and what it never does

- `https://` on port 443 only; no credentials in the address; no IP literals, single-label or
  private names (`.local`, `.internal`, …); nothing under the belfry's own domain.
- The name is looked up first; every address it has must be public (no private, loopback,
  link-local, multicast, reserved or CGNAT range, no IPv4-mapped private address) and none of
  the belfry's own addresses. The connection goes to the address checked (no DNS rebinding);
  the certificate is checked for the name.
- At most 3 redirects, each checked the same way. 5 s per request; at most 4 fetches at once.
- Only `text/html` (or XHTML), the first 512 kB. `og:title`, `og:description`, `og:site_name`,
  `og:image` (+ `:width`, `:height`), then `twitter:*`, then `<title>` and
  `<meta name="description">`. A picture address over `http://` is dropped.
- User-Agent `Mozilla/5.0 (compatible; Belfry link preview; +https://belfry.sh)`, no cookies,
  nothing about who asked.
