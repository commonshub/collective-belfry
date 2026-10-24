# Village: the directory and the marketplace

Two community apps of Open Source Village, built 2026-10-24 on the user's choice of extras
("Photos, Directory, Marketplace with tokens"; photos are the other session's):

- **Directory** at `people.<domain>`: who is here. Participants opt in; nobody is listed by default.
- **Marketplace** at `market.<domain>`: things, food, help and skills offered between participants,
  priced in village tokens and paid through the wallet.

Both are browser apps that read and write the community relay with the participant's own signer
("Sign in with Open Source Village" through Keycast, silent once per tab, or a browser extension or
bunker URL after self-custody). One Node process serves both (ports 8080 and 8081) and answers one
question: which keys hold an active pass.

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

Kinds signed: 0, 5, 22242, 24242, 27235, 30078, 30402 (all in the `belfry-event` policy). Keycast
clients `belfry-people` and `belfry-market`, registered by bootstrap with `<URL>/oauth/callback` and
trusted (`KEYCAST_TRUSTED_CLIENTS`).

## Build and run

Node 24, no framework, like `services/vote` (whose NIP-98 check and Keycast sign-in are copied
here): `server/` (node:http), `web/shared` (sign-in, signer, relay with NIP-42 AUTH, the event
formats in `model.js`), `web/people`, `web/market` (vanilla JS, Vite; `APP=people|market vite build -c
web/vite.config.js`). Compose service `village` (profile app), host ports 3600 and 3700 locally,
proxy hosts `people.` and `market.<domain>`, `PEOPLE_PUBLIC_URL`, `MARKET_PUBLIC_URL`.

| Build arg | Meaning |
| --- | --- |
| `VITE_KEYCAST_URL`, `VITE_APPS_URL` | Keycast (sign-in, the app bar's home) |
| `VITE_RELAY_URL` | the community relay (`COMMUNITY_RELAY_URL`) |
| `VITE_BLOSSOM_URL` | Blossom for photos (`BLOSSOM_PUBLIC_URL`) |
| `VITE_CHAT_URL`, `VITE_WALLET_URL`, `VITE_PEOPLE_URL` | links between the apps |
| `VITE_COMMUNITY_APPS`, `VITE_BRAND_URL` | the app bar and the brand files |

Checks: `npm test` (the participants endpoint, the event formats), smoke step 6p (both apps served,
the list refused without auth and for a key without a pass, given to the participant), and in a
browser `infra/scripts/village-check.mjs` with two participants: directory opt-in and search, an
offer with a photo, paying it through the wallet, the payment shown to buyer and seller.

## Open

No moderation or reporting of offers or profiles yet (participants can delete their own); no
notifications (a seller learns of a payment in the wallet or on the offer page); the directory has
no fields beyond kind 0 (interests, languages); pictures are links in the directory but uploads in
the marketplace.
