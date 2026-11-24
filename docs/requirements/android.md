# Android app

Prefix `AND`. Only what is specific to Android; everything in the cross-platform files applies.
Repo: opencollective/belfry-android (Kotlin, Jetpack Compose, `core` + `app`). Its own CLAUDE.md
holds the engineering rules (demo mode, outbox, screenshot tests).

## Native feel

- **AND-1** [android] Material 3: NavigationBar, ModalBottomSheet, top app bars, FAB only where
  Material would; predictive back; the system share sheet; edge-to-edge; dynamic type sizes. Adapt
  the canvas to Android, don't copy the iPhone.
- **AND-2** [android] Edge-to-edge means insets everywhere: every bar (the app's update bar
  included) sits below the status bar and the camera cutout, never under them. (owner, on a Pixel
  with 0.8.3, 2026-11-24)
- **AND-3** [android] Tablets keep the left column (no bottom bar); e-ink screens work: no state
  shown only by a subtle grey or by animation (the owner tests on an e-ink tablet).
- **AND-4** [android] Count badges never clip (never inside an IconButton) and grow with the font
  size.
- **AND-5** [android] Notification permission is asked from Me → Notifications or a channel's bell,
  never at launch. Notification channels: Direct messages, Mentions and replies, Announcements,
  Pinned, All messages.
- **AND-6** [android] Push uses Firebase Cloud Messaging (no Analytics); a device without Google
  Play services keeps working with push off. Scanning (invitations, cards, tickets) works without
  Play services.

## Updates and releases

- **AND-10** [android] A release (tag `v<x.y.z>`) only on the owner's own word, each time: the
  in-app updater offers it to every installed phone and belfry.sh/android follows the newest
  release. (owner, from 2026-11-10)
- **AND-11** [android] The app updates itself from the project's GitHub releases (release builds
  only), with a setting to turn it off; it shows only the version number, never a release's
  text. The Update button installs the update (it must not open the app's system settings).
- **AND-12** [android] Only the signed release APK opens App Links (`/join/`, `/events/`, `/g/`,
  `/login/` on the chat hosts, verified by assetlinks.json); debug builds have another
  certificate.
- **AND-13** [android] The signing key stays in GitHub secrets and its local copy; never printed,
  never moved.
- **AND-14** [android] Never run Gradle on the team's machine: CI is the only build; no full
  screenshot suite (PROC-21); filtered screenshot runs by hand when a reviewer needs them.

## Accounts

- **AND-20** [android] Several accounts on one phone (one live session at a time, a switcher at
  the drawer's bottom); two identities on one belfry both get push.
- **AND-21** [android] Sign in to third-party Nostr apps by scanning their `nostrconnect://` QR,
  next to "Link a phone or tablet" (Android only for now; owner, 2026-11-20). → privacy.md PRIV-35
