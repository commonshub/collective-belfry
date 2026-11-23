# Sealed conversations, version 2: one list of devices, a copy for each

Status: **contract, agreed 2026-11-23 after review by Android, iPhone and web; being built.**
Replaces the chain of rosters, the letterbox, the anchor, device requests as a way in, key
hand-overs, endorsements and §5.7 of `docs/sealed.md`. Kept from version 1: device keys, the
seal signed by the device, the gift wrap, silent wraps, history packs (8564), "It was me"
(8563), the 40 000 byte ceiling, files (kind 15), Notes to myself, reading v1 wraps.

Changes to this file after today go through the lead and are announced to the three apps
with the section that changed. Vectors: `vectors/sealed.json`, section `v2`, in belfry-android.

## 0. Why

Xavier, 2026-11-23, after new devices kept saying "Messages are locked on this device": "This
entire thing looks very complicated. It shouldn't be. We need to trust any device that
entered the 6-digit. It should be added to the list of devices of the identity in the bunker
of the belfry and from that moment on, anyone sending a private message to that identity
should encrypt it for that new device as well. And that new device can write a self note that
will be encrypted for all existing devices. When on the new device you open the private
messages there should be a message to ask your other devices for history (explaining that
previous conversations have been encrypted for the other devices, you can ask them to
re-encrypt them for your device). Then all other devices get a notification to approve that
and proceed (or remove device because not trusted)."

## 1. What it protects, and what not

- A sealed message is readable only on the devices that were on its participants' device
  lists when it was sent. Not by the relay, not by the signer, not by reading the database.
- **Not protected, said plainly.** The belfry writes the device lists. A device is on a list
  because it holds a signed-in chat session (the e-mail code). So whoever can open a session
  on an account (the belfry's server, hence its admins; someone with the person's mailbox)
  reads everything sealed FROM THEN ON. They get nothing sealed before unless one of the
  person's devices shares it. The person's devices show "‹session name› was added ‹time› ·
  Not you? Remove it"; §3.3 makes that notice hard for the belfry to suppress.
- The relay sees, for every message, how many devices each participant has.
- A removed device keeps what it already had.
- The web app's code is served by the belfry. E-mail is in clear at the mail server's edge.

## 2. Keys

One key per device: the session's client key (made on the device, never leaves it). No
letterbox, no shared key, nothing to hand over.

## 3. The device list

### 3.1 The event

One replaceable event per account, signed by the **belfry's own key** (`belfry.json`
`nostr.pubkey`), written by the onboarding service and by nothing else:

```
kind 30078, pubkey = the belfry key,
tags [["d","belfry:devices:<account hex>"], ["p", <account>], ["device", <device pubkey>], ...]
content ""
```

- Signed by the belfry, not the account: no session of the account, and no other app
  connected to it, can write or forge one (they sign as the account, never as the belfry).
- `device` tags: the account's live chat sessions (`chat: true` rows of the sessions list)
  that were active in the last 30 days, oldest session first, no names, no dates. At most
  20: when there are more, the 20 most recently active. 0 devices = an empty list event.
- `created_at` = max(now, the standing list's + 1). Readers ignore a list dated more than
  10 minutes ahead of their clock.
- Rewritten when a chat session is bound (every sign-in path), revoked, signed out or
  expired, and checked every 10 minutes against the sessions (idle ones leave then).
- Every account with a chat session has one: members, guests. Agents and accounts that hold
  their own key have none and are not sealed.
- On the community relay. Apps read `{kinds:[30078], authors:[belfry key],
  "#d":["belfry:devices:<A>"]}`, without `since`, after AUTH; the newest wins (`created_at`,
  then lower id). Never from another author.

A signed-in device that does not find itself on its account's list (the cap, or the list
not written yet) says so in one line with Retry, and seals nothing meanwhile: "Getting your
messages ready…", then after 15 s "This device isn't on your device list yet." It is never
"locked".

### 3.2 What an app remembers

Per account: the lists it has seen (event id, `created_at`, devices, when this app first
saw it), for good. That memory serves accepting (§4.2), notices (§6.4) and §3.3.

### 3.3 The list a message was sealed to

Every sealed rumor carries, for each participant, the list it was sealed to:

```
["list", <account>, <id of that account's list event>]
```

inside the rumor, so under the seal: the belfry cannot change it. A device that opens a
message whose `list` tag for ITS OWN account names a list it has never seen reads that list
by id (`{ids:[…]}`); if the list has a device this app did not know as mine, it shows the
added-device notice for it, marked "seen by ‹sender›, not shown to this device before". This
is the check that does not depend on what the belfry serves this device: a list shown only
to my correspondents is found out by the first message they send.

## 4. A sealed message

### 4.1 Sending

Rumor as today (kind 14, 15, 1301, 7, 5; `pubkey` = the account), plus the `list` tags. For
every device D on the list of every participant, **the sending device itself included**:

- seal, kind 13: `pubkey` = the sending device, signed by it, `tags: []`; content = NIP-44
  with conv(sending device secret, D) of the rumor JSON;
- gift wrap, kind 1059: one-time key; content = NIP-44 with conv(one-time secret, D) of the
  seal; tags `["p", <D's account>]`, `["d", <D>]`, and `["silent","1"]` when D's account is
  the sender's own.

Before each send the app reads every participant's list again (the live subscription keeps
them fresh; wait at most 3 s). The outbox keeps the RUMOR and builds the wraps when it
sends: a message still queued when a list changes gains the new devices and loses the
removed ones. A message is "sent" when every wrap was accepted. At most 10 wraps a second.

### 4.2 Receiving

A device reads `{kinds:[1059], "#p":[my account], "#d":[my device]}` (and, for version 1
wraps, the old `{kinds:[1059], "#p":[my account]}` read stays). It accepts a rumor when the
seal's signer S and the rumor's `pubkey` account A satisfy one of:

- S is on A's newest list;
- S was on a list of A this app has seen, and the rumor is not dated after this app first
  saw a list of A without S (a removed device cannot go on writing as A);
- A is the belfry's key and the rumor is kind 1301 or a report (§7).

Otherwise the wrap is kept 30 days and judged again whenever A's list is read (a message
can arrive before the list that names its device). Same rumor id twice = once.

### 4.3 Cost

Two people with 3 devices each: 6 wraps a message. The cap makes it at most 40. The
community relay's limit goes from 60 to 600 events a minute per address. Typing is not sent
in sealed conversations (as today). Reactions and take-backs are wrapped like messages.

## 5. When a conversation is sealed

- Sealed when every participant has a list with at least one device and the participants'
  devices total 100 or fewer. Over 100: not sealed, and the conversation says so in one line
  ("Too many devices to encrypt for everyone here"); never a silent downgrade.
- No `ready`, no waiting: a device is on the list when it signs in.
- Sticky per device, as today: once sealed, never sent the old way again.
- **Hold, never unseal.** A list that cannot be read = hold the message (shown as sending).
  Once a non-empty list was seen for an account, a missing or empty one also means hold,
  never the old way.
- Notes to myself: sealed as soon as I have a list, which is as soon as I am signed in.

## 6. A new device

1. It signs in. The belfry puts it on the list. It reads and writes everything sealed from
   then on. Nothing is locked.
2. It has none of the earlier sealed messages. On top of Messages, and above the older part
   of sealed conversations: "Earlier messages were encrypted for your other devices. Ask
   them to share them with this one. · **Ask my other devices**". The line stays until the
   person asks or dismisses it.
3. **Asking**: kind 8562, signed by the device, `["p", account]`, content "". One request
   per device; again only after 24 h or when another of my devices appears on the list. A
   request from a device not on the list is ignored. The belfry pushes it to my other
   devices (type `device_history`).
4. **The card, on every other device of mine** (it is also the added-device notice for that
   device, one card not two): "‹session name› was added ‹time› and asks for your earlier
   messages: ‹N› conversations since ‹date› · **Share** · **Not you? Remove this device**".
   Without a request the card reads "‹session name› was added ‹time› · It was me · Not you?
   Remove it".
5. **Share**: that device seals what it holds to the asking device as history packs (kind
   8564 as today: the rumors of kinds 14, 15, 7, 5, 1301 of its sealed conversations, v1 and
   v2 alike, under the ceiling per pack), then sends the 8563 (§6.5) so the card goes on my
   other devices. The person's tap, never automatic. Share counts as "It was me".
   The asking device takes packs only while it has an open request, only from a device on
   my list, never replaces a rumor it already holds, and marks each conversation "Earlier
   messages here were shared by ‹device› on ‹date›".
6. **Remove**: `POST /api/me/sessions/revoke` for that session. The belfry rewrites the list
   without it. Nothing to rotate.

### 6.4 The notice

A device shows the card for a device D of my list when D was not on the last list it saw,
or §3.3 reveals it. Never for devices whose session is older than this device's own, and
none at all for the first list an app ever sees (the day of the update, a fresh install).
In a sealed conversation the other side sees one grey line: "‹Name› added a device".

### 6.5 "It was me", once for all

```
kind 8563, pubkey = the confirming device,
tags [["p", account], ["device", <the added device>], ["list", <id of the list that has it>]]
```

sealed to my own devices like a note (silent). It clears the card for that device on a
device of mine when its signer is on my list, is not the named device, has a session older
than the named device's (sessions list), and has no open card of its own here. Share sends
the same event with `["request", <id of the 8562>]`.

## 7. E-mail

In: the belfry seals a mail to every device on the member's list (seal signed by the belfry
key, `list` tag included). No list: as before sealing. A list that cannot be read: 451.
Out: one device-sealed 1301 to the belfry's key, as today; accepted when the seal's signer
is on the sender's list. Reports (7679): one per device of the sender.

## 8. Version 1, and moving

- The belfry writes lists for everyone from the day it runs this code; `belfry.json` says
  `"sealed": {"version": 1, "lists": true}` until every released app sends version 2, then
  `{"version": 2}`.
- **While the belfry says version 1**, a v2 app sends BOTH: the per-device wraps, and the
  version 1 wrap to the letterbox of anyone who has a ready chain (so apps that only know
  version 1 keep reading; a v2 reader sees the same rumor twice and keeps one). It stops
  making, following or changing chains: no rosters, anchors, requests-to-join, hand-overs.
- **When the belfry says version 2**, nothing is sent the version 1 way.
- A v2 app keeps opening version 1 wraps with the letterbox secrets it holds, for good, and
  shares them like any earlier message. No letterbox secret is ever sent again.
- The first v2 build reads its `#d` wraps from 0.
- An app older than v2 cannot open a per-device wrap (it takes it for an old NIP-17 wrap,
  asks the signer, fails): harmless while both are sent; after the flip it reads nothing
  new. So the flip waits for the web to be live, the TestFlight build, and an Android
  release that has been out for some days.
- Queued version 1 wraps not yet sent when an app updates are rebuilt from their rumor.

## 9. What the server builds

- `devices.py`: the list (build from sessions, sign as the belfry key, publish with
  retries), hooks at session bind / revoke / sign-out / expiry, the 10-minute check, CLI
  `python -m belfry_onboarding devices [--write]`.
- `mail.py`: seal per device with the `list` tag; accept device-sealed mail by the list;
  reports per device. The version 1 paths stay while `version` is 1.
- Push: one "dm" push per account per 5 seconds however many wraps arrive (never per wrap);
  `device_history` for an 8562 of a listed device.
- The relay's limit: 600 events a minute per address.
- `belfry.json` `sealed`.
- `docs/sealed.md` gets a line on top pointing here.

## 10. Later, not in this version

- A younger session revoking an older one asks for a fresh e-mail code.
- Share "only this conversation".
- One envelope per account with a key slot per device, in place of a wrap per device.
- Sealing for agents and own-key accounts.

## Appendix: readings

Fixed with the first vectors (belfry-android `vectors/sealed.json`, section `v2`, key
`readings`; 2026-11-23). Where a section above leaves a choice, these decide.

- R1. A list has exactly one `d` and one `p`, and `p` is the account `d` names; else it is
  not a list. R2. Its content is ""; else not a list.
- R3. A `device` tag that is not 64 lower-case hex is skipped, the others count. R4. A device
  given twice counts once, at its first place. R5. More than 20 devices: not a list (the
  reader keeps the one it had).
- R6. A kind 1059 with a `d` tag is a version 2 wrap only with exactly one `d`, exactly one
  `p` and no `lb`; else invalid, dropped. R7. `d` is this device but `p` is not its account:
  invalid.
- R8. The belfry's key is accepted only as both the seal's signer and the rumor's `pubkey`,
  with kind 1301; anything else under that key is dropped.
- R9. A version 2 rumor without a `["list", <the reader's account>, <id>]` tag is dropped.
  Not so for rumors carried inside a history pack (8564): the pack's rule decides (§6),
  since version 1 rumors have no such tag.
- R10. "First saw a list of A without S" = when this app first saw the list that LAST
  dropped S. A list from before S was ever listed is not a removal. A rumor dated in the gap
  between two stays of S is accepted.
- R11. Over 100 devices: in a conversation sealed here before, hold with the one line, never
  the old way; in one never sealed here, not sealed, said in the line.
- R12. The `list` tags are the rumor's last tags, one per participant, in the order of the
  accounts' hex; readers do not depend on the order.
- R13. Bounds are inclusive (a rumor of the same second is accepted; a list exactly 10
  minutes ahead is taken).
