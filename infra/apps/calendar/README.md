# Open Source Village calendar

[Calendar by Form*](https://github.com/formstr-hq/nostr-calendar) (MIT, NIP-52 calendar events,
private events as NIP-59 gift wraps), built at a pinned commit with Belfry's patch series and
served as a static site at `calendar.<domain>` (locally http://localhost:3200).

## How participants use it

1. On the apps page (`apps.<domain>`, Keycast) choose **Connect to Nostr App** and copy the
   bunker URL. One bunker per app: Keycast binds the first app that connects with its secret.
2. On the calendar, **Connect with Remote Signer (NIP-46)**, paste the URL, **Connect**.
3. Create a calendar first ("Create Calendar"), then events. Events are private by default
   (gift-wrapped to the invited participants); switch an event to public to show it to every
   participant on the community relay.

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
`VITE_ABOUT_URL`. `VITE_MAIL_API_BASE_URL` is empty, so the app never calls Formstr's mail API.

The patch (one commit, `0001-...patch`):

- OSV palette, lime accent and dark mode as defaults, Geist and favicon from `/brand`, OSV
  logo, titles and manifest.
- Sign-in dialog: the remote signer first, with the apps-page hint; "Existing Key" and "Create
  Account" only with `VITE_LOCAL_KEYS=true` (participants have a managed key in Keycast).
- Sidebar: Apps and About point to the deployment; Formstr's privacy policy and contact form are
  hidden.
- `@formstr/local-relay` 0.6.0 -> 0.6.5. 0.6.0 never answers NIP-42 AUTH challenges, so on an
  AUTH-only relay every read and write was refused (`auth-required`); found in the browser test.

To change the patch: clone the app, `git checkout <CALENDAR_COMMIT>`, `git apply` the patch,
commit, edit, `git commit --amend`, `git format-patch -1 -o infra/apps/calendar/patches`. Run
Prettier on changed files: the build runs the app's lint and typecheck.

## Check it

`make smoke` step 6k checks the page and the bundle (title, community relay, no upstream
relays). The full browser check drives headless Chromium through the bunker sign-in, creates a
calendar and an event and reads them back from a fresh browser profile:

    node infra/scripts/calendar-check.mjs http://localhost:3200/ <file with a bunker URL> /tmp/cal

(a bunker URL from "Connect to Nostr App" on the apps page, used once: Keycast binds it to the
first client; `CALENDAR=<name> CREATE=1 TITLE=<title>` also creates them).
