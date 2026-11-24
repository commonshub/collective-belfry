# Web chat (chat.<domain>)

Prefix `WEB`. Only what is specific to the web; everything in the cross-platform files applies.
Source: `commonshub/collective-belfry` branch `chat-source`, shipped inside every belfry.

## Layout and navigation

- **WEB-1** [web] Phone layout first (the bottom bar, UI-21); at ≥ 900 px the channel list stays
  on the left and the places sit at the top of that column; sheets become native `<dialog>`
  modals on laptop.
- **WEB-2** [web] History: a ‹ never pushes; in-app redirects keep the history state (never a raw
  `location.replace` that drops it), and back never lands on an entry with the same address.
  (lead, 2026-11-19, after back loops)
- **WEB-3** [web] Sheets and the composer follow the iOS keyboard in Safari (the app is pinned to
  the visual viewport while the keyboard is up); every field ≥ 16 px, and the page is never
  zoomable sideways on iOS.
- **WEB-4** [web] Buttons and forms lock while their handler's promise runs; a second tap within
  600 ms is swallowed. Server pages load the shared busy script.

## Loading and updates

- **WEB-5** [web] The page never comes up blank after a ship: the service worker fetches the page
  network-first and never stores HTML for /assets; ships keep the previous build's assets (pruned
  after 14 days); a missing asset is a 404, never the page; a failed script reloads once, then
  shows a Reload screen after 12 s. (owner reported blank pages, 2026-11-19)
- **WEB-6** [web] Several accounts of one belfry in one browser, each with its own store; other
  belfries appear as links. Forget this device revokes every account's session.

## Link previews

- **WEB-10** [web] A browser can't read other sites, so the web sender asks the belfry for a
  preview (`GET /api/v1/link-preview`) only in channels, open groups and unencrypted DMs, then
  uploads the picture to Blossom itself. In a sealed or encrypted conversation the web sends no
  preview, neither in the bubble nor in the composer. (lead, 2026-11-24, after a leak from sealed
  DMs) → docs/link-previews-v2.md
- **WEB-11** [web] As a reader the web never asks the belfry or any site for a preview.

## Encryption on the web

- **WEB-20** [web] The web is the first platform for each step of encryption and must be proven
  working before Android starts (ENC-50).
- **WEB-21** [web] Said plainly to people: on the web the belfry serves the code that holds the
  device key; device keys live in the browser's storage, which Safari may evict (the device then
  signs in again as a new device).

## Shipping

- **WEB-30** [web] A chat build ships as a commit "Ships chat <sha>: <what changed>" in
  `infra/shipped-apps/chat` on `belfry-sh` and `belfry-setup` (fast-forward), then applied on the
  four boxes, one first (SRV-10, SRV-11); check that `https://chat.<domain>/` serves the new
  `index-*.js`.
- **WEB-31** [web] Before every commit and ship: fetch and rebase on `origin/chat-source` (others
  ship too); after shipping, tell the lead and belfry-agent in one message.
- **WEB-32** [web] Run the browser tests of the area you touched and of every screen it opens
  (including belfry-agent's meetings / channel settings / invite tests before changing those).
- **WEB-33** [web] The web has no push notifications yet.
