# Link previews built by the sender (v2)

Status: **contract** (2026-11-24, Xavier: "Good idea. In fact for all"; "build the sender built link
previews on android"). Replaces the reader-side fetch of `docs/link-previews.md`: from now on
**readers never fetch anything for a preview**, neither the outside page nor the belfry.

## In one paragraph

When someone writes a message with an outside `https://` link, the SENDER's app builds the preview
once while composing (picture, site, title, description), shows it above the field with ✕, and
attaches it to the message it sends. Recipients display what arrived. In sealed or encrypted
conversations the picture is encrypted like any photo there, so neither the belfry nor anyone else
learns the link from a preview.

## Which link

The first `https://` link in the text that is not the belfry's own (its domain and every subdomain:
those have their own in-app cards, docs/one-shape.md) and not a file already attached. One
preview per message.

## How the sender gets it

- **Phones (Android, iPhone):** fetch the page on the device: https only, port 443, no
  credentials in the URL, refuse IP literals, single-label names and private / loopback /
  link-local addresses after DNS resolution; at most 3 redirects, each checked the same way;
  5 seconds; `text/html` only; the first 512 kB. Read `og:title`, `og:description`, `og:image`,
  `og:site_name`, then `twitter:*`, then `<title>` and `<meta name="description">`. Strip tags,
  collapse whitespace; title ≤ 200 characters, description ≤ 300. The picture: same fetch rules,
  png / jpeg / webp / gif by their first bytes, ≤ 5 MB downloaded, then re-encoded on the device
  to JPEG or WebP, longest side ≤ 1200 px, ≤ 300 kB.
- **Web:** a browser may not read other sites. In a channel, an open group or an unsealed DM the
  web sender asks the belfry once (`GET /api/v1/link-preview?url=`, as in v1) and then uploads the
  picture it got to Blossom itself like a phone does. In a sealed or encrypted conversation the
  web sends no preview.
- Nothing usable (no title and no description) → no preview, the link stays plain.

## On the wire

One tag on the message (kind 9, 1111, 14; inside the rumor when the message is sealed or
encrypted):

```
["preview", <url as written in the text>, <title>, <description>, <site name>]
```

Empty strings for missing title / description / site (never all three empty). Plus, when there
is a picture, one `imeta` entry for it marked as the preview's:

```
["imeta", "url <blossom url>", "m image/jpeg", "dim 1200x630", "x <sha256 of what Blossom stores>",
          "purpose preview"]
```

In a sealed (version 1) or encrypted (format 3) conversation, and in a private group, the picture
is AES-256-GCM encrypted on the device before upload exactly like a file there
(vectors/dm-files.json, docs/encrypted.md §23): the `imeta` entry also carries `ox`,
`encryption-algorithm aes-gcm`, `decryption-key`, `decryption-nonce`, and `m` is the original type.

`["preview", "none"]` (✕ in the composer) means: no preview for this message. A message that
carries neither tag shows a plain link.

## Showing it

- Readers build the box only from the tag and the imeta entry. Never fetch the page; never fetch
  the belfry's preview endpoint.
- The picture only from the belfry's own Blossom (`https://<domain>` / its Blossom host; an image
  URL elsewhere is ignored). Decrypt when the entry carries a key; check `x` (and `ox`).
- Messages sent before v2 (no tag) show a plain link.
- The tag is part of the message: an edit (kind 1010) may carry a new tag; the latest wins.

## The server

`GET /api/v1/link-preview` stays, for web senders only (channels, open groups, unsealed DMs). The
picture proxy stays for the web sender to download from; readers no longer use it.

## Vectors

`vectors/link-previews.json` (written by belfry-android): which link counts, the tag's shape,
parsing og / twitter / title fallbacks from sample HTML, the imeta entry plain and encrypted.
