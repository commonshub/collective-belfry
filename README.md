# Open Source Village app (mobile web, offline first)

Sign in by e-mail + 6-digit code → not in yet? become the first admin, use an invite code or ask
the admins with a note → channels (Discord-style), calendar with RSVPs, and a chat/tasks/files
space for every event. Admins answer requests, manage members and make invite codes in the app.

- `npm run dev` · `npm run build` (static files in `dist/`, served from any sub-path)
- Real Belfry by default ([BELFRY-API.md](BELFRY-API.md)); `?backend=demo` keeps everything on
  this device (tests only).

## How it works
- `src/core/` has no UI code, so the native apps can reuse it: `auth` (sign-in drivers, signer:
  NIP-46 bunker or local demo key), `db` (IndexedDB), `store` (in-memory mirror),
  `sync` (outbox, relays with NIP-42 auth, Blossom uploads), `model` (derived views), `actions`,
  `access` (who may use the village, as Nostr events: requests, grants, invites, admins).
- Offline first: every action is written to the IndexedDB outbox and shown at once; the flush
  loop signs, uploads attachments and publishes when online, in order. Everything received is
  cached, so the app opens and reads offline. The service worker caches the app shell.
- Nostr kinds: channels are NIP-29 groups (9007 create, 9002 edit, 39000/39002 from the relay,
  9 messages with NIP-92 `imeta` attachments); member labels NIP-32 (1985, namespace
  `osv/member-label`, latest per member wins); tasks follow tasklist.sh (2100–2103, list id
  derived from the group id); calendar NIP-52 (31922 all-day, 31923 timed, 31925 RSVP), each
  event gets a group `ev-<d>`.
