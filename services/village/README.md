# Village: the directory, the marketplace, the photos and the programme

Four community apps of Open Source Village:

- **Directory** at `people.<domain>`: who is here. Participants opt in; nobody is listed by default.
- **Marketplace** at `market.<domain>`: things, food, help and skills offered between participants,
  priced in village tokens and paid through the wallet.
- **Photos** at `photos.<domain>`: pictures of the village shared between participants, with likes,
  comments and tips in village tokens.
- **Programme** at `programme.<domain>`: what is on, as a list for the phone (see "Programme" below).

All are browser apps that read and write the community relay with the participant's own signer
("Sign in with Open Source Village" through Keycast, silent once per tab, or a browser extension or
bunker URL after self-custody). One Node process serves them (ports 8080, 8081, 8082 and 8083) and
answers one question: which keys hold an active pass.

## How it works

- **Participants.** `GET /api/participants` (NIP-98, the caller must hold an active pass) returns
  the keys of all active passes, from the onboarding service (`GET /admin/active-passes`,
  `PASS_INFO_TOKEN`). The relay keeps the events of people whose pass ended; the apps show only
  active ones. Nobody outside learns who is attending.
- **Directory consent.** A kind-30078 event (NIP-78 app data) with `d` = `osv-directory` and content
  `{"listed": true}`; the newest one per author counts. "Your entry" edits it together with the
  kind-0 profile (name, about, picture link, website; other kind-0 fields are kept). Without a
  picture, every app shows the default avatar drawn from the key (`web/shared/avatar.js`, the
  same picture the onboarding service serves as PNG and puts in the starter profile). The profile is
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

## Programme

`programme.<domain>`: the village programme and the room bookings (the public NIP-52 events of
Belfry's programme key, `PROGRAMME_PUBKEY`, published by the onboarding service's
`sync-programme` job) and participants' own public events (kind 31923, announced here or as public events in the calendar app),
one day at a time. The day opens on today, or the next day with sessions; on today the list is
split into "On now", "Later today" and "Earlier today". Filters by track: the programme, each
room, "From participants" (the choice is kept per browser). Times are shown in `VITE_EVENT_TZ`
(Europe/Brussels), whatever the phone's zone. "Your sessions" lists what the participant is going
to, might go to or announced, with "Add all to my calendar" (one .ics file).

Made for the phone: a tab bar at the bottom (Programme, Search, Yours, Announce, and Shifts for
volunteers and stewards), the day chips stay
under the app bar while the list scrolls, a swipe goes to the next or previous day, today's list
redraws every minute ("On now"), and "Starting soon" names the participant's sessions within half an
hour. Search finds sessions where every typed word starts a word of the title, description, place
or room (case and accents ignored, links left out; "ch" finds "Chess", not "lunch"), upcoming
first; the field stays under the app bar while the results scroll. While a field has focus (the
phone's keyboard is up) the tab bar steps aside, and `interactive-widget=resizes-content` makes
the page end at the keyboard instead of panning the app bar and the field out of view. Every relay
read pages past the relay's cap of 300 events per request (`queryAll` in `shared/nostr.js`;
deletions are read with `#k` 31923). The programme the phone last loaded is kept in `localStorage` (per
participant, cleared on sign-out) and drawn at once, then refreshed from the relay; when the relay
does not answer, the kept copy stays with a note. A service worker (`web/programme/public/sw.js`,
the programme app only) caches the page, its assets, the app bar and the brand files, so the app
opens without a network and shows the kept copy (the CSP's `connect-src` names `pass.<domain>` for
its brand fetches). Calendar files carry a reminder 15 minutes
before each session (`VALARM`); that is the reminder, since the apps send no notifications.

"Announce a session" (`#/new`) publishes a public kind 31923 in the participant's name: title,
day, from and until in the village's time (whatever the phone's zone; an end before the start
runs past midnight), place (the rooms are suggested), description, a link; `d` = `session-…`, never
the programme key's `osv-programme-`/`osv-room-` prefixes, which `sync-programme` manages. The host
edits it (`#/edit/<naddr>`, same coordinate, so RSVPs, comments and photos stay; tags the form does
not show, such as a summary, an image, hashtags or invitees from the calendar app, are kept) or
cancels it (kind 5 with `a` and `e`). The calendar app shows participants' public events as "From
participants" (calendar patch 0006), with the same filters as here: it reads this app's
`/api/participants` (NIP-98) and `/api/hidden`, which answer the calendar's origin
(`PROGRAMME_CORS_ORIGINS`, CORS).

A session is addressed by its coordinate `31923:<pubkey>:<d>` (links: `#/s/<naddr>`), and the
other apps point at it:

| What | Event | Where it shows |
| --- | --- | --- |
| Going / Maybe / Can't go | kind 31925, `a` = the coordinate, `status`; one replaceable answer per participant, with Calendar by Form*'s `d` (the first 30 hex digits of SHA-256 of `<responder>:<author>:<d>`), so an answer in either app replaces the other's | counts on the list, the people going on the session page, "Your sessions" |
| Comments | NIP-22 kind 1111 with `A` = the coordinate, `K` 31923 | the session page; "What's new" |
| Photos | kind 20 with an `a` tag: the photos app's `#/new?session=<coordinate>` ("Share a photo from this session") | the session page; "At <session>" on the photo |
| Thanks | a nutzap through the wallet with the coordinate as `ref` (an `a` tag), participants' sessions only | "Thank the host" |
| Polls | a poll in the polls app (`services/vote`) with the session's coordinate: "Start a poll" opens `<vote>/#/new?session=…`; anonymous like every poll | the session page (the polls app's public list, `VITE_VOTE_URL`, CSP `connect-src`); "About <session>" in the poll |
| Add to my calendar | an .ics file made in the browser, with a reminder 15 minutes before | the phone's own calendar |

Sessions of participants and comments can be reported (`session`, `comment`); the programme's
own events cannot. No new kinds in the policy (31923, 31925, 1111, 20 and 9321 were in).
Keycast client `belfry-programme`.

**Shifts** (`#/shifts`). Volunteers earn their pass with shifts, which live in Odoo, never on the
relay (docs/NEXT_STEPS.md §3zh). This app's server answers `GET /api/shifts` and
`POST /api/shifts/<id>/signup|leave` (NIP-98, programme port only): it finds the signer's partner
in the active passes (`partner_id` of `/admin/active-passes`) and asks the onboarding service's
`/admin/shifts` for that partner, so the browser never names a partner. The role decides: an
active volunteer sees every shift with who signed up and signs up or leaves (until
`change_hours` before it; a refusal shows the onboarding service's message), a steward sees the
same without buttons, anyone else gets no role and no tab. The tab appears once the server has
named a role (remembered per participant on the device; asked once per page load). The
participant's shifts are in "Yours" and in its calendar file. Pending volunteers use their
personal shift page on `pass.<domain>/shifts/<secret>` instead (they have no Nostr key yet).

## What's new

`people.<domain>/#/activity` (linked from the directory as "What's new"): the last 14 days of
readable events from participants with an active pass, newest first, grouped by day, filterable
by app, each linking into its app. Shown: chat notes (not replies) and articles, photos and photo
comments, comments on programme sessions, offers (and offers marked as gone), public calendar
events (they open in the programme), directory opt-ins, and the polls app's notes (a poll opened,
its result; kind 1 of `POLLS_PUBKEY`, build argument `VITE_POLLS_PUBKEY`, shown as "Polls"). Left out
on purpose: private messages and private calendar entries (encrypted), likes, profile edits,
opting out of the directory, and token payments (who paid whom stays in the wallets and on the
offer pages); deleted events are dropped. The rules are `activityFromEvent` in `web/shared/model.js`.

## Build and run

Node 24, no framework, like `services/vote` (whose NIP-98 check and Keycast sign-in are copied
here): `server/` (node:http), `web/shared` (sign-in, signer, relay with NIP-42 AUTH, the event
formats in `model.js`), `web/people`, `web/market`, `web/photos`, `web/programme` (vanilla JS,
Vite; `APP=people|market|photos|programme vite build -c web/vite.config.js`). Compose service
`village` (profile app), host ports 3600, 3700, 3800 and 3900 locally, proxy hosts `people.`,
`market.`, `photos.` and `programme.<domain>`, `PEOPLE_PUBLIC_URL`, `MARKET_PUBLIC_URL`,
`PHOTOS_PUBLIC_URL`, `PROGRAMME_PUBLIC_URL`.

| Build arg | Meaning |
| --- | --- |
| `VITE_KEYCAST_URL`, `VITE_APPS_URL` | Keycast (sign-in, the app bar's home) |
| `VITE_RELAY_URL` | the community relay (`COMMUNITY_RELAY_URL`) |
| `VITE_BLOSSOM_URL` | Blossom for listing photos and the photos app (`BLOSSOM_PUBLIC_URL`) |
| `VITE_CHAT_URL`, `VITE_WALLET_URL`, `VITE_PEOPLE_URL`, `VITE_PHOTOS_URL`, `VITE_PROGRAMME_URL` | links between the apps |
| `VITE_PROGRAMME_PUBKEY`, `VITE_EVENT_TZ` | the programme key (`PROGRAMME_PUBKEY`) and the event's time zone |
| `VITE_COMMUNITY_APPS`, `VITE_BRAND_URL` | the app bar and the brand files |

Checks: `npm test` (the participants endpoint, the event formats), smoke step 6p (both apps served,
the list refused without auth and for a key without a pass, given to the participant), and in a
browser `infra/scripts/village-check.mjs` with two participants: directory opt-in and search, an
offer with a photo, paying it through the wallet, the payment shown to buyer and seller. Photos: tests in `test/photos.test.js`, smoke step 6q, and
`infra/scripts/photos-check.mjs` with two participants: upload with alt text, share, feed and
hashtag, like, comment, a tip through the wallet, the photographer sees all three, delete.
Programme: `test/programme.test.js`, smoke step 6t, and `infra/scripts/programme-check.mjs` at
390 px: the day list without sideways scrolling, "Going", a comment, the .ics link, "Your
sessions", a photo shared from the session and shown there, a second participant seeing it, and
a session announced, seen by the second participant (also in the calendar app), edited and
cancelled, search, "Add all", a poll started from the session and listed there, the tab bar, the
kept copy on a reload and the page opening offline. Shifts: `test/shifts.test.js`,
`infra/scripts/shifts-check.mjs` at 390 px (a steward sees the board without buttons; a volunteer,
with the browser answering `/api/shifts`, signs up, gets a refusal and finds the shifts in "Yours").

## Open

No moderation or reporting of offers or profiles yet (participants can delete their own); no
notifications (a seller learns of a payment in the wallet or on the offer page); the directory has
no fields beyond kind 0 (interests, languages); pictures are links in the directory but uploads in
the marketplace. Photos: animated GIFs become still JPEGs; no albums; Blossom keeps the files of
deleted posts until its retention removes them. Programme: no push notifications (the reminder is the
phone calendar's, from the .ics file); offline, the day lists, search and "Yours" work from the kept
copy, but a session's page (answers, comments, photos, polls) needs the network.
