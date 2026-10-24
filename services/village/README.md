# Village: the directory, the marketplace and the photos

Three community apps of Open Source Village:

- **Directory** at `people.<domain>`: who is here. Participants opt in; nobody is listed by default.
- **Marketplace** at `market.<domain>`: things, food, help and skills offered between participants,
  priced in village tokens and paid through the wallet.
- **Photos** at `photos.<domain>`: pictures of the village shared between participants, with likes,
  comments and tips in village tokens.

All are browser apps that read and write the community relay with the participant's own signer
("Sign in with Open Source Village" through Keycast, silent once per tab, or a browser extension or
bunker URL after self-custody). One Node process serves them (ports 8080, 8081 and 8082) and
answers one question: which keys hold an active pass.

## How it works

- **Participants.** `GET /api/participants` (NIP-98, the caller must hold an active pass) returns
  the keys of all active passes, from the onboarding service (`GET /admin/active-passes`,
  `PASS_INFO_TOKEN`). The relay keeps the events of people whose pass ended; the apps show only
  active ones. Nobody outside learns who is attending.
- **Directory consent.** A kind-30078 event (NIP-78 app data) with `d` = `osv-directory` and content
  `{"listed": true}`; the newest one per author counts. "Your entry" edits it together with the
  kind-0 profile (name, about, picture link, website; other kind-0 fields are kept). The profile is
  the same one the chat and the other apps show. What the directory shows: the kind-0 fields and
  the NIP-05 name, nothing from Odoo (no e-mail, tier or pass dates).
- **Offers.** NIP-99 classifieds, kind 30402: `title`, `summary`, `price` `["price", "<n>", "OSV"]`
  (0 = free), `t` one of things, food, services, skills, rides, other, `location`, `image` URLs,
  `status` active or sold ("gone"), the description as content. Photos are shrunk to 1600 px JPEG in
  the browser and uploaded to Belfry's Blossom server (kind-24242 auth, active pass required by the
  gate). Deleting publishes a kind-5 with the offer's `a` coordinate.
- **Paying.** "Pay N tokens" opens the wallet (`wallet.<domain>/?pay=<npub>&amount=N&memo=…&ref=<a
  coordinate>&return=<offer URL>`, wallet patch 0003): the send dialog is filled in, the wallet sends
  a NIP-61 nutzap whose `a` tag names the offer, and returns to the offer. The tokens never leave the
  wallet. The offer page shows the buyer "You paid N tokens" and the seller every payment, from the
  kind-9321 events with that `a` tag. Handing over the thing is up to the two (a "Message the
  seller" link opens the chat). There is no escrow and no refund: tokens have no money value.
- **Links.** "Message" opens the person in the chat (Coracle, `/people/<npub>`); "Send tokens" opens
  the wallet's pay request without an amount reference.

- **Photos.** NIP-68 pictures, kind 20: up to 8 images as `imeta` tags (`url`, `m`, `x` (SHA-256),
  `dim`, `alt` written by the participant) plus `m` and `x` tags, `title`, hashtags as `t`,
  `location`, the caption as content. Images are shrunk in the browser to 2048 px JPEG (which drops
  EXIF and GPS) and uploaded to Blossom like the marketplace's. The feed shows posts of active passes
  only, newest first, with the most used hashtags as filters (`#/t/<tag>`) and one person's posts
  (`#/u/<npub>`). Likes are NIP-25 reactions (kind 7, `k` 20; the newest reaction per person counts,
  `-` takes a like back), comments NIP-22 (kind 1111, root `E`/`K`/`P`), both from active passes
  only. Deleting publishes a kind 5 with the post's `e` and `k` 20.
- **Tips.** "Tip the photographer" offers 1, 5 or 10 tokens: the wallet's pay request with the
  post's id as `ref` (wallet patch 0004 puts it in an `e` tag of the nutzap). The post shows the
  tipper "You tipped N tokens" and the photographer each tip; only nutzaps to the photographer count.

- **Reports.** A "Report" link on a person in the directory, an offer and a photo or comment opens
  a short form (reason: spam, harassment or hate, illegal content, scam or fraud, other; an
  optional note). `POST /api/report` takes it NIP-98 signed with the body's hash; the reporter is
  the signer and must hold an active pass. The server passes it to the onboarding service, which
  stores it in Odoo and mails the stewards; stewards handle it on the steward pages (hide the
  content, dismiss, revoke the author's pass). `GET /api/hidden` lists what stewards hid; every
  app filters it out (lists, pages, comments, "What's new"), except for the author, who sees a
  notice instead. The content itself stays on the relay.

Kinds signed: 0, 5, 7, 20, 1111, 22242, 24242, 27235, 30078, 30402 (all in the `belfry-event`
policy). Keycast clients `belfry-people`, `belfry-market` and `belfry-photos`, registered by
bootstrap with `<URL>/oauth/callback` and trusted (`KEYCAST_TRUSTED_CLIENTS`).

## What's new

`people.<domain>/#/activity` (linked from the directory as "What's new"): the last 14 days of
readable events from participants with an active pass, newest first, grouped by day, filterable
by app, each linking into its app. Shown: chat notes (not replies) and articles, photos and photo
comments, offers (and offers marked as gone), public calendar events, directory opt-ins. Left out
on purpose: private messages and private calendar entries (encrypted), likes, profile edits,
opting out of the directory, and token payments (who paid whom stays in the wallets and on the
offer pages); deleted events are dropped. The rules are `activityFromEvent` in `web/shared/model.js`.

## Build and run

Node 24, no framework, like `services/vote` (whose NIP-98 check and Keycast sign-in are copied
here): `server/` (node:http), `web/shared` (sign-in, signer, relay with NIP-42 AUTH, the event
formats in `model.js`), `web/people`, `web/market`, `web/photos` (vanilla JS, Vite;
`APP=people|market|photos vite build -c web/vite.config.js`). Compose service `village` (profile
app), host ports 3600, 3700 and 3800 locally, proxy hosts `people.`, `market.` and `photos.<domain>`,
`PEOPLE_PUBLIC_URL`, `MARKET_PUBLIC_URL`, `PHOTOS_PUBLIC_URL`.

| Build arg | Meaning |
| --- | --- |
| `VITE_KEYCAST_URL`, `VITE_APPS_URL` | Keycast (sign-in, the app bar's home) |
| `VITE_RELAY_URL` | the community relay (`COMMUNITY_RELAY_URL`) |
| `VITE_BLOSSOM_URL` | Blossom for listing photos and the photos app (`BLOSSOM_PUBLIC_URL`) |
| `VITE_CHAT_URL`, `VITE_WALLET_URL`, `VITE_PEOPLE_URL` | links between the apps |
| `VITE_COMMUNITY_APPS`, `VITE_BRAND_URL` | the app bar and the brand files |

Checks: `npm test` (the participants endpoint, the event formats), smoke step 6p (both apps served,
the list refused without auth and for a key without a pass, given to the participant), and in a
browser `infra/scripts/village-check.mjs` with two participants: directory opt-in and search, an
offer with a photo, paying it through the wallet, the payment shown to buyer and seller. Photos: tests in `test/photos.test.js`, smoke step 6q, and
`infra/scripts/photos-check.mjs` with two participants: upload with alt text, share, feed and
hashtag, like, comment, a tip through the wallet, the photographer sees all three, delete.

## Open

No moderation or reporting of offers or profiles yet (participants can delete their own); no
notifications (a seller learns of a payment in the wallet or on the offer page); the directory has
no fields beyond kind 0 (interests, languages); pictures are links in the directory but uploads in
the marketplace. Photos: animated GIFs become still JPEGs; no albums; Blossom keeps the files of
deleted posts until its retention removes them.
