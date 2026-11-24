# iPhone app

Prefix `IOS`. Only what is specific to iPhone; everything in the cross-platform files applies.
Repo: opencollective/belfry-ios (SwiftUI, `BelfryCore` package). Its own CLAUDE.md holds the
engineering rules (layout rules that prevent hangs, offline-first, demo mode).

## Native feel

- **IOS-1** [iphone] Native first: system navigation (push), system controls, SF Symbols, Dynamic
  Type, haptics, swipe actions where iOS users expect them; the neutral look the owner asked for
  (no bronze accent). "Compare with iMessage or WhatsApp… they don't feel native"
  (owner, 2026-11-15).
- **IOS-2** [iphone] Menus are the system's own: the composer's ＋ is a Menu of Labels, a held
  message opens a context menu (quick reactions, then the message menu). Sheets are List / Form
  sections with native rows, no hand-made pills.
- **IOS-3** [iphone] Drawer rows and list rows open screens, never sheets; profiles push. The one
  sheet the owner chose is the channel notifications sheet. Sheets are for short tasks started
  from a screen (invite, new event, edit profile). (owner, 2026-11-15)
- **IOS-4** [iphone] Buttons that start work are busy buttons (locked with progress); copying
  gives a check mark and the success haptic; the keyboard goes down before any sheet opens.
- **IOS-5** [iphone] Text fields ≥ 16 pt (body font). No tap gesture over a whole row with text
  (it eats link taps); links are underlined in the tint.
- **IOS-6** [iphone] Views never freeze the main thread: the layout rules in the repo's CLAUDE.md
  (fixed frames on the event screen, no view state written from geometry callbacks, views in
  animated containers stay in the tree) are requirements; the typing UI test guards them.
- **IOS-7** [iphone] A ticket's QR turns the screen brightness up.

## Links and notifications

- **IOS-10** [iphone] Universal links open the app for `/join/<token>`, `/events/<link>`,
  `/g/<id>` and `/login/<username>` on every belfry's chat host (each host must be in the
  associated-domains entitlement and serve apple-app-site-association).
- **IOS-11** [iphone] Notifications come through the push gateway; a Notification Service Extension
  decrypts the payload with the device's key in the shared App Group; ask permission at a moment
  that explains it, never at launch.

## Releasing

- **IOS-20** [iphone] A green `main` uploads to TestFlight for the internal group only. Not
  authorized: App Store submission, external testers, external beta review, anything in App Store
  Connect beyond what CI does. (owner, 2026-11-18; whether "release iPhone" ever means external
  review is unanswered: to confirm before doing it.)
- **IOS-21** [iphone] The Apple team is shared with other apps: touch only `sh.belfry.*`.
- **IOS-22** [iphone] No Mac on the team's machine: CI is the only build; a run does only what the
  change touched; the full suite only for a new minor release; no full screenshot suite (PROC-21).
- **IOS-23** [iphone] The export-compliance answer (the app implements NIP-44 itself) is the
  owner's call. (to confirm)
- **IOS-24** [iphone] The marketing version follows Android's number.
