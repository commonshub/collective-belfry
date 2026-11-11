# Push notifications (Belfry for iPhone and Android)

Status: built on the belfry side and the gateway (branch `push`, 2026-11-11); the apps are being
built against it. Changes made while building are marked *(built)*. This is the contract between the belfry, the push gateway and the
two native apps. The web chat has no push yet (Web Push could reuse the same settings later).

## 1. Why a gateway

Every community runs its own belfry, but the phone apps are one app (`sh.belfry.app`) published
by Open Collective. Apple (APNs) and Google (FCM) only accept pushes signed with the publisher's
credentials, so a belfry cannot push to a phone by itself. As in Matrix (Sygnal) or Mastodon:

```
belfry (any domain) --HTTPS, NIP-98 by the belfry key--> push gateway (push.belfry.sh)
push gateway --APNs / FCM--> phone
```

The gateway holds the APNs key and the FCM service account. It never sees what a notification
says: the belfry encrypts it to a key the device made (§4). The gateway sees a device token, the
belfry's key, a size and a time.

## 2. Push gateway (`services/push-gateway`, only on the publisher's belfry)

Enabled with `PUSH_GATEWAY=1` (belfry.sh only), served at `https://push.<domain>`.
Secrets *(built: the names have no extension, like every other secret)*: `infra/secrets/apns_key`
(the `.p8`) and `infra/secrets/fcm_service_account` (the JSON); settings `APNS_KEY_ID`,
`APNS_TEAM_ID` (and `APNS_TOPIC`, default `sh.belfry.app`). `belfry config` takes them
(`services/push-gateway/README.md`).

`POST /v1/send`, `Authorization: Nostr <NIP-98 event>` signed by a belfry's own key (the
`belfry_nsec`; its npub is in that belfry's `/.well-known/belfry.json`):

```json
{"platform": "apns" | "fcm", "token": "<device token>", "env": "production" | "sandbox",
 "payload": "<base64 NIP-44 v2 ciphertext, ≤ 3000 bytes>", "collapse": "<≤ 64 chars>",
 "urgent": true}
```

- APNs (topic `sh.belfry.app`, push type alert, `mutable-content: 1`): `aps.alert` =
  `{"title": "Belfry", "body": "New notification"}` (shown only if the extension fails),
  `aps.sound = "default"`, `aps.thread-id = collapse`, custom key `b` = payload. The app's
  Notification Service Extension decrypts and replaces title and body.
- FCM HTTP v1: data-only message, `android.priority = high` when urgent, `collapse_key`,
  `data.b` = payload. The app's messaging service decrypts and posts the notification itself.
- Answers: `200 {"ok": true}`, `410 {"error": "gone"}` when APNs/FCM say the token is dead (the
  belfry deletes that device), `429 {"error": "rate"}`, `400 invalid`, `401 unauthorized`;
  *(built)* `503 {"error": "not_configured"}` when that platform's credentials are missing (the
  gateway starts without them) and `502 {"error": "upstream"}` when APNs/FCM fail otherwise.
- *(built)* `env` defaults to `production`, `urgent` to true. APNs: `apns-priority` 10 when
  urgent, else 5; `apns-collapse-id` = collapse; expiry one day. FCM: `android.priority`
  `high` / `normal`, `ttl` one day. Dead tokens: APNs 410, or 400 `BadDeviceToken` /
  `DeviceTokenNotForTopic`; FCM `UNREGISTERED`, `SENDER_ID_MISMATCH`, 404, or 400 "not a valid
  registration token".
- Limits: 120 sends per hour per token, 10 000 per hour per belfry key (sliding hour, in memory;
  refused requests don't count). No list of allowed belfries: tokens are only known to the
  belfry the phone registered with. A NIP-98 event is accepted once.
- Logs: no payloads; tokens only as a hash prefix.

Other belfries use `PUSH_GATEWAY_URL` (default `https://push.belfry.sh`); empty = no push.

## 3. Devices and settings on each belfry (`services/onboarding`, `push.py`)

Every call is NIP-98 by the person (member, guest or agent with an active pass). *(built)* Push
needs the belfry's own register (`REGISTER=belfry`, every new belfry): with Odoo both endpoints
answer `409 {"error": "unsupported"}` and nothing is watched. NIP-98 events are accepted once,
so two identical calls in the same second need different `created_at`s.

`POST /api/v1/push/devices`
- `{"add": {"platform": "apns"|"fcm", "token": "…", "env": "production"|"sandbox",
  "key": "<base64 32 random bytes>", "app": "belfry-ios"|"belfry-android", "name": "…"}}` →
  `{"id": "<device id>"}`. The same token again replaces the old row (a new key, a new owner).
- `{"remove": "<token>"}` → `{"ok": true}` (the app calls this on sign-out, before revoking).
- `{}` → `{"devices": [{"id", "app", "name", "since"}]}` (the caller's).
- A device goes away when its pass closes, when the gateway answers 410, or after 60 days with
  no call from it (the apps re-register on every start).

`POST /api/v1/notifications`
- `{}` → the caller's settings; `{"set": {…}}` merges and returns them. Shape and defaults:

```json
{"dms": true, "mentions": true, "announcements": true, "pins": true,
 "channels": {"<group id>": "all" | "default" | "mentions" | "off"}}
```

Per-channel levels (a channel not listed is `default`):

| level | notifies |
|---|---|
| `all` | every message and thread reply, plus what `default` notifies |
| `default` | mentions of me, replies to me, announcements, pinned messages (each subject to its switch above) |
| `mentions` | mentions of me and replies to me only |
| `off` | nothing from this channel |

*(built)* The `mentions` switch covers mentions and replies. At level `all`, a mention, reply or
announcement whose switch is off still comes, as `message`.

`dms: false` turns off direct messages and e-mail threads. Settings are stored in the register
(param `belfry.push.<partner id>`); e-mail follows (`followers.py`, the bell's e-mail part) and the
mention e-mail digest (`mentions.py`) stay separate.

## 4. Payload

The device makes a random 32-byte key `K` and registers it. The belfry encrypts with NIP-44 v2
using `K` directly as the conversation key (no ECDH), so both apps reuse their NIP-44 code.
Plaintext (JSON, at most 2 KB before encryption; `body` cut to 180 characters):

```json
{"v": 1, "domain": "belfry.sh", "type": "dm" | "mention" | "reply" | "announcement" | "pin" | "message",
 "title": "#general", "body": "Ada: are you coming tonight?",
 "channel": "<group id>", "event": "<event id>", "from": "<author pubkey>",
 "url": "https://chat.belfry.sh/#/c/<group id>/m/<event id>"}
```

- dm: `title` = the belfry's name, `body` = "New message". The belfry cannot read gift wraps
  (sender and text are hidden), so no `from`, `channel` or `event`; `url` =
  `https://chat.<domain>/#/dm`.
- pin: `title` = "📌 #channel", `body` = the pinned message's author and text. *(built)* `event`
  and `from` are the pinned message's id and author.
- *(built)* A thread reply's `url` opens its thread: `https://chat.<domain>/#/c/<group id>/thread/<root id>`
  (`event` is the reply's own id). `title` is the channel's label as the e-mails write it
  (`#name`, or a pod's / event's own name). A message without text shows "📷 Photo" (or "…").
- announcement: `title` = "📣 #channel".
- The apps never trust `url` blindly: they check its host is `chat.<domain>` of the belfry they
  are signed in to, and open the channel / message by id; otherwise they open the app's home.
- `collapse` = `dm` for DMs, else the group id. *(built)* `urgent` is true for dm, mention, reply
  and announcement, false for pin and message.
- *(built)* NIP-17 senders also gift-wrap a copy to themselves, which the belfry cannot tell
  apart: a phone may get "New message" for a DM its owner just sent. Apps may stay quiet for a
  DM notification that arrives within a few seconds of sending one themselves.

## 5. What triggers a notification (the belfry's watcher)

A live subscription (reconnecting with backoff, 1 s to 5 min). *(built)* The groups relay is read
as the groups API key (the relay's owner: it sees closed pods too, as followers.py and
mentions.py do); the community relay as the belfry key:

- groups relay: kinds 9, 1111 (thread replies), 1985 with `L belfry/pin`, for every group;
- community relay: kind 1059 (gift wraps; NIP-17 DMs and Nostr Mail) with a `p` of a person
  who has a device.

For each new event (seen once; older than 10 minutes on arrival = skipped, so a reconnect
doesn't replay history). *(built)* What a relay sends before EOSE on the first connection after a
start is history (marked seen, never sent: a restart replays nothing); after a reconnect it is
caught up within the 10 minutes. Gift wraps carry a randomised `created_at` (NIP-59), so they
count by arrival: only those that come in after EOSE notify.

1. Recipients: people with a device and an active pass who are **members of the group** (their
   key in its 39002 member list, or community-wide admins for any group), minus the author.
   DMs: the `p` of the gift wrap. Guests only for their own channels.
2. Type per recipient, the first that applies: mention (a `p` tag = them or `nostr:npub…` of
   them in the content; *(built)* on a 1111 the `p`/`P` naming the message it answers is the
   NIP-22 reply marker, not a mention) → reply (a 1111 or a kind 9 whose `e`/`E`/`q` points at a message of
   theirs, or a 1111 in a thread they wrote in) → announcement (`t announcement` by a group admin,
   the rule of followers.py) → pin (`l pinned` by a group or community admin; the pinned
   message's text) → message (only at level `all`).
3. Settings: the channel's level and the type's switch decide. An edit (1010), a reaction (7)
   or a deletion never notifies.
4. Send to each of that person's devices through the gateway, at most one per event per device.

Private by design: the gateway sees no content; Apple and Google see ciphertext.

## 6. Apps

- Ask for permission at a moment that explains it (first time a DM or channel opens, or a
  "Turn on notifications" row in Me), never at launch. Register (`add`) on every start once
  allowed; `remove` on sign-out.
- Settings: Me → Notifications (the four switches, the list of my channels with their level,
  devices) and, per channel, the bell sheet: Push level (All / Default / Mentions only / Off)
  next to the existing e-mail follow.
- iOS: Notification Service Extension `sh.belfry.app.notifications`, App Group
  `group.sh.belfry.app` (the device key `K` in a shared keychain access group), APNs entitlement.
- Android: Firebase Cloud Messaging (`firebase-messaging`, no Analytics), notification channels
  Direct messages / Mentions and replies / Announcements / Pinned / All messages, Android 13+
  `POST_NOTIFICATIONS`. Builds without `google-services.json` keep working with push off.
- Tapping a notification opens the channel at the message (or the conversation list for a DM).
