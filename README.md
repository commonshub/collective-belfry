# Belfry chat (mobile web, offline first)

The chat every belfry ships with, served at `https://chat.<domain>`. It started as Open Source
Village's "village app" and still builds in two looks:

- `VITE_THEME=belfry npx vite build`: the Belfry look. The domain is read from the host it is
  served from (`chat.<domain>`), the community's name from `/.well-known/belfry.json`.
- `npx vite build` (no theme): the Open Source Village look, on `belfry.opensourcevillage.org`.

What is in it: channels (NIP-29 groups) with one summary line under the name and a pull-down
(pinned message, events, proposals, to-dos, pods, topics); inline threads; reactions; edits;
@mentions; Markdown; events with RSVPs; pods (working groups, open or by application);
proposals with versions and votes; to-dos; files and drop-box links; direct messages and
e-mail (Messages); shared inboxes (e-mail to `<channel>@<domain>`); announcements and
following; guest links (`/join/<token>`); member profiles with tags, a city and a map; search.

- `npm run dev` · `npx tsc --noEmit -p .` · `VITE_THEME=belfry npx vite build` (static files in
  `dist/`, served from any path).
- `?backend=demo` keeps everything on this device (tests only); `?backend=belfry` goes back.
- Shipping: the built `dist/` is copied into collective-belfry `infra/shipped-apps/chat`
  (its `.build` file names the commit); `belfry apply` installs it on a belfry.
- Browser tests: `~/agents/belfry/site/.shots/chat/*.cjs` (headless Chromium, demo mode)
  against `python3 -m http.server 4391` in `dist/`.
- The server side is in [BELFRY-API.md](BELFRY-API.md). The native apps' `SPEC.md`
  (belfry-ios, belfry-android) are derived from this code.

## How it works
- `src/core/` has no UI code: `auth` (sign-in, guest join, the signer: NIP-46 bunker or a local
  demo key), `db` (IndexedDB), `store` (in-memory mirror of the event log), `sync` (outbox,
  relays with NIP-42 auth, Blossom uploads, NIP-98 requests), `model` (views derived from the
  log), `actions` (what a user does, as events), `dm` (NIP-17 messages, Nostr Mail), `mime`,
  `people` (profile extras, places), `access` (see BELFRY-API.md: not served by a belfry today).
- Offline first: every action is written to the IndexedDB outbox and shown at once; the flush
  loop signs, uploads attachments and publishes when online, in order per relay, and retries on
  `online`, on coming back to the foreground and every 15 s. Everything received is cached, so
  the app opens and reads offline. The service worker caches the app shell.
- Two relays: the groups relay (`groups-relay.<domain>`, NIP-29: channels and everything in
  them) and the community relay (`relay.<domain>`: profiles, calendar, proposals' texts, gift
  wraps).
- Identifiers that still say "osv" are stored or published values and must not change without
  a migration: IndexedDB `osv-village`, client tag `osv-village`, OAuth client id
  `osv-village-app`, label namespace `osv/member-label`, to-do list id `sha256("osv-village:" +
  group)`, localStorage keys `osv-*`.
