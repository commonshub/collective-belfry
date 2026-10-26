# Open Source Village calendar

[Calendar by Form*](https://github.com/formstr-hq/nostr-calendar) (MIT, NIP-52 calendar events,
private events as NIP-59 gift wraps), built at a pinned commit with Belfry's patch series and
served as a static site at `calendar.<domain>` (locally http://localhost:3200).

## How participants use it

1. Open the calendar. A participant who is signed in to Open Source Village (Authentik, or
   Keycast within 24 h) is signed in without a click: on load the app tries Keycast's OAuth with
   `prompt=none` once per tab and connects with the bunker URL the token response carries.
2. Otherwise the dialog offers **Sign in with Open Source Village** (the same OAuth, with the
   e-mail link or the pass at Authentik when needed) and, below it, the manual way: on the apps
   page (`apps.<domain>`) **Connect to Nostr App**, copy the bunker URL, paste it under
   **Connect with Remote Signer (NIP-46)**. One bunker per app: Keycast binds the first client
   that connects with its secret.
3. Create a calendar first ("Create Calendar"), then events. Events are private by default
   (gift-wrapped to the invited participants). A public event is visible to anyone who opens
   its link (the app shows other people's public events only through links and invitations).
4. The sidebar's **Open Source Village** group (patch 0004) shows the village programme and one
   calendar per room of Commons Hub Brussels, each with its own colour and checkbox. They are the
   public events of the programme key (`PROGRAMME_PUBKEY`, build argument
   `VITE_PROGRAMME_PUBKEY`), published by the onboarding service's `sync-programme` job from the
   website's `/schedule.json` and the rooms' public Google calendars. Read-only here; participants
   can open, share and RSVP to them. Patch 0006 adds **From participants**: every participant's
   public event (kind 31923 on the community relay) of a participant with an active pass, without
   what stewards hid after a report and without deleted ones (kind 5 by their author). The list of
   active participants and the hidden list come from the programme app (`VITE_PROGRAMME_URL`:
   `/api/participants` signed with NIP-98, `/api/hidden`; CORS for the calendar's origin, CSP
   `connect-src`); until they are known the calendar stays empty. The signed-in participant's own
   events are left out only when they are in one of their calendars already (the app shows those);
   a session announced in the programme app shows up here. The relay answers at most 300 events
   per filter and a subscription cannot page, so the programme, the room bookings and the
   deletions have a filter each, and participants' events are asked for by author, 100 per
   subscription; a failed participants request is retried after 30 seconds.

Reading and writing go to the community relay only (NIP-42 AUTH as a participant with an active
pass); the app signs through Keycast, so the `belfry-event` policy (`infra/keycast/policy.sql`)
must allow its kinds: 0, 3, 5, 13, 1059, 10002, 22242, 30078, 31923, 31925, 31926, 31927, 32069,
32123, 32678, 32680.

## Build

`infra/apps/calendar/Dockerfile` clones the app at `CALENDAR_COMMIT`, applies `patches/`, copies
the OSV logo and mark from `brand/` and builds with pnpm 10. The URLs are build arguments, baked
into the bundle (rebuild after changing them): `VITE_TEST_RELAY` (`COMMUNITY_RELAY_URL`, the
app's single-relay switch), `VITE_SIGNER_RELAYS` (`BUNKER_PUBLIC_RELAYS`), `VITE_BRAND_URL`
(`<ONBOARDING_PUBLIC_URL>/brand`, fonts and favicon), `VITE_APPS_URL` (`KEYCAST_APP_URL`),
`VITE_ABOUT_URL`, `VITE_KEYCAST_URL` (`KEYCAST_APP_URL`; empty disables "Sign in with Open
Source Village"). Keycast must know the client `belfry-calendar` with the redirect URI
`<CALENDAR_PUBLIC_URL>/oauth/callback` and trust it (no consent screen; Keycast patch 0008,
bootstrap). `VITE_MAIL_API_BASE_URL` is empty, so the app never calls Formstr's mail API.

Patch `0001-...patch`:

- OSV palette, lime accent and dark mode as defaults, Geist and favicon from `/brand`, OSV
  logo, titles and manifest.
- Sign in with Open Source Village (`src/belfry/sso.ts`): authorization code + PKCE against
  Keycast, silent attempt on load (`prompt=none`, once per tab), callback `/oauth/callback`
  exchanges the code and logs in with the bunker URL; any error opens the dialog.
- Sign-in dialog: Sign in with Open Source Village first, then the remote signer with the
  apps-page hint; "Existing Key" and "Create
  Account" only with `VITE_LOCAL_KEYS=true` (participants have a managed key in Keycast).
- Sidebar: Apps and About point to the deployment; Formstr's privacy policy and contact form are
  hidden.
- `@formstr/local-relay` raised from 0.6.0 to 0.6.5: 0.6.0 never answers NIP-42 AUTH challenges,
  and the community relay refuses every read and write without AUTH.

Patch `0002-...patch`: the Open Source Village app bar (`brand/osv-bar.js`, see
`brand/README.md`) in `index.html` with `VITE_APPS_URL` as its home and `VITE_COMMUNITY_APPS` as
its links; `html.osv-bar` sets `--safe-area-top`, which every top offset of the app reads, to the
bar's height, and the desktop sidebar pads by it; the sidebar's Apps entry opens the apps page in
the same tab; the PWA manifest uses the bar's dark green and the calendar's own icons from
`brand/apps/` (copied by the Dockerfile), with a separate maskable icon and `apple-touch-icon.png`.

Patch `0003-...patch`: logout ends the sign-in everywhere. The token exchange keeps
the refresh token (localStorage); after the app's own logout `endSsoSession()` revokes it at
Keycast (`POST /api/oauth/revoke`), sets the silent-try flag and goes through Keycast's
`/api/auth/oidc/logout?return_to=<origin>/` (Keycast patch 0010), which ends the Keycast and
Authentik sessions and comes back to the signed-out calendar. Check: `calendar-check.mjs` with
`SSO=1 LOGOUT=1`.

Patch `0005-...patch`: the phone layout. Upstream shows "Calendar by" plus a small square logo;
patch 0001's wide OSV wordmark pushed the top bar (and with it the page) past the edge of a phone
(451 px on a 412 px screen), so phones get the square mark, the date label stays on one line, and
a phone opens on the day view (seven week columns cannot be read there). Desktop is unchanged.
For a list of what is on, with RSVPs, comments and photos per session, participants use the
programme app (`programme.<domain>`, `services/village`).

Patch `0006-...patch`: participants' public sessions as the "From participants" calendar (see 4.).

To change the patch: clone the app, `git checkout <CALENDAR_COMMIT>`, `git apply` the patch,
commit, edit, `git commit --amend` (or a new commit), `git format-patch <CALENDAR_COMMIT>.. -o infra/apps/calendar/patches`. Run
Prettier on changed files: the build runs the app's lint and typecheck.

## Check it

`make smoke` step 6k checks the page and the bundle (title, community relay, no upstream
relays). The full browser check drives headless Chromium through the bunker sign-in, creates a
calendar and an event and reads them back from a fresh browser profile:

    node infra/scripts/calendar-check.mjs http://localhost:3200/ <file with a bunker URL> /tmp/cal

(a bunker URL from "Connect to Nostr App" on the apps page, used once: Keycast binds it to the
first client; `CALENDAR=<name> CREATE=1 TITLE=<title>` also creates them).
