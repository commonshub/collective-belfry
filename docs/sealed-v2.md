# Sealed conversations, version 2: one list of devices, a copy for each

Status: **draft for review, nothing built** (2026-11-23). Replaces the chain of rosters, the
letterbox, the anchor, device requests as a way IN, key hand-overs, endorsements and §5.7 of
`docs/sealed.md`. What stays from version 1: device keys, the seal signed by the device, the
gift wrap, silent wraps, sealed history (8564), "It was me" once for all (8563), the 40 000
byte ceiling, files (kind 15), Notes to myself.

## 0. Why

Xavier, 2026-11-23, after a morning of new devices that said "Messages are locked on this
device": "This entire thing looks very complicated. It shouldn't be. We need to trust any
device that entered the 6-digit. It should be added to the list of devices of the identity in
the bunker of the belfry and from that moment on, anyone sending a private message to that
identity should encrypt it for that new device as well. And that new device can write a self
note that will be encrypted for all existing devices. When on the new device you open the
private messages there should be a message to ask your other devices for history (explaining
that previous conversations have been encrypted for the other devices, you can ask them to
re-encrypt them for your device). Then all other devices get a notification to approve that
and proceed (or remove device because not trusted)."

Version 1 made a person's own devices the authority on which devices are theirs. That cost a
chain, a shared key that had to travel, and a new device that waited for an old one. Xavier's
choices of 22 and 23 November already gave the authority back to "whoever holds a signed-in
session"; version 2 says so plainly and drops the machinery.

## 1. What it protects, and what not

- A sealed message is readable only on the devices that were on the recipient's (and the
  sender's) device list when it was sent. Not by the relay, not by the belfry's signer, not
  by an admin reading the database.
- **Not protected, said plainly.** The belfry decides what is on a person's device list: a
  device is on it because it holds a signed-in chat session (the e-mail code). So whoever can
  open a session on an account (the belfry's server, hence its admins; someone with the
  person's mailbox) gets a device on the list and reads everything sealed FROM THEN ON. They
  get nothing sealed before, unless one of the person's devices approves sharing it. Every
  device of the person shows "‹session name› was added ‹time› · Not you? Remove it".
- A removed device keeps what it already had.
- The web app's code is served by the belfry. E-mail is in clear at the mail server's edge.

## 2. Keys

One key per device: the session's client key (as today; made on the device, never leaves it).
There is no letterbox, no shared key, nothing to hand over.

## 3. The device list

A person's device list is ONE replaceable event, signed by the **account** key through the
signer, written by the belfry (the onboarding service), never by an app:

```
kind 30078, pubkey = the account, tags [["d","belfry:devices"],
  ["device", <device pubkey>, <session created_at, seconds>, <session name>], ...]   (0 to 20)
content ""
```

- It lists the account's live chat sessions (the `chat: true` rows of `/api/me/sessions`),
  oldest first, at most 20 (the 20 most recently active when there are more).
- The belfry rewrites it at the moment a chat session is bound (every sign-in path), at a
  revoke or sign-out, when a session expires, and checks hourly that it matches. A sign-in
  answers only after the list with the new device is on the relay (else the app would open
  with nothing readable).
- On the community relay. Readable by members like profiles.
- Accounts that hold their own key, and agents, have no list: they are not sealed (as today).
- Apps read `{kinds:[30078], authors:[A], "#d":["belfry:devices"]}`, always without `since`,
  after AUTH, and keep the newest by `created_at` (then lower id).

There is no pin, no chain, no "going back" rule: the newest list is the list. An app
remembers the lists it has seen only to show notices (§6).

## 4. A sealed message

Rumor as today (kind 14, 15, 1301, 7, 5; `pubkey` = the account). For every device D on the
list of every participant, the sender included (except the sending device itself, which keeps
its own copy locally):

- seal, kind 13: `pubkey` = the sending device, signed by it; content = NIP-44 with
  conv(sending device secret, D) of the rumor JSON;
- gift wrap, kind 1059: one-time key; content = NIP-44 with conv(one-time secret, D) of the
  seal; tags `["p", <D's account>]`, `["d", <D>]`, and `["silent","1"]` when D's account is
  the sender's own.

A device reads `{kinds:[1059], "#p":[my account], "#d":[my device]}`. It accepts a rumor when
the seal's signer is on the list of the rumor's `pubkey` account now, or was on a list this
device has seen for it; a seal by a device it has never seen listed is kept 30 days and
judged again when that account's list is read (a message can arrive before the list). The
belfry's own key seals mail in (§7) the same way and is accepted for kind 1301 only.

At most 20 devices a person, so a two-person message is at most 39 wraps. A conversation of
more than 8 people is not sealed in this version.

**Before each send** the app reads every participant's list again (3 s; the live
subscription keeps it fresh). A participant with no list, or an empty one: §5.

## 5. When a conversation is sealed

A conversation is sealed when every participant has a device list with at least one device.
No `ready`, no waiting for all of someone's devices: a device is on the list the moment it
signs in. Sealed is sticky per device as today (never sent the old way again), and "the old
way only on knowledge" stays: a list that cannot be read means hold, not unsealed.

Notes to myself: the conversation whose only participant is me; sealed as soon as I have a
list, which is as soon as I am signed in.

## 6. A new device

1. It signs in (e-mail code). The belfry puts it on the list. From that second it reads and
   writes everything sealed from then on. Nothing is "locked".
2. It has none of the earlier sealed messages. Messages shows, on top and in sealed
   conversations above the older part: "Earlier messages were encrypted for your other
   devices. Ask them to share them with this one." with **Ask my other devices**.
3. Asking publishes a request, kind 8562 as today (signed by the device, `["p", account]`).
   Every other device of mine shows a card (and the belfry pushes it, type `device_history`):
   "‹session name› asks for your earlier messages · signed in ‹time› · **Share** ·
   **Remove this device**".
4. **Share**: that device re-seals what it holds to the asking device (kind 8564 history
   packs as today: the rumors of kinds 14, 15, 7, 5 and 1301 of every sealed conversation it
   holds, in packs under the ceiling), then sends an 8563 naming the request so the card goes
   on my other devices. Sharing is the person's tap, never automatic. A second device may
   share too; rumors already held are skipped.
5. **Remove this device**: `POST /api/me/sessions/revoke` for that session. The belfry
   rewrites the list without it. Nothing else to do: no key changes hands, there is nothing
   to rotate.
6. The notice. A device that sees a device on my list that was not on the last list it saw
   shows "‹session name› was added ‹time› · Not you? Remove it" until "It was me" (8563, once
   for all my devices) or Remove. This is the only alarm; it needs one of my devices opened.

Messages sealed under version 1 (to a letterbox) stay readable on the devices that hold that
letterbox's secret, and are shared with a new device like any other earlier message (as
rumors, through 8564). No letterbox secret is ever sent again.

## 7. E-mail

In: the belfry seals a mail to every device on the member's list (seal signed by the belfry
key). No list: as before sealing. Out: a device-sealed 1301 to the belfry key (one wrap, to
the belfry's key, as today) is accepted when the seal's signer is on the sender's list.
Reports (7679) are sealed per device like a message from the belfry.

## 8. What the server builds

- `devices.py`: the list event (build, sign through the signer, publish), hooks at session
  bind / revoke / expiry, the hourly check, `belfry devices` CLI to print and rewrite.
- Sign-in answers after the list is published.
- `mail.py`: seal per device; accept device-sealed mail by the list.
- Push: `device_history`; the wrap filter by `#d` is the apps'.
- `belfry.json`: `"sealed": {"version": 2}` when the belfry writes device lists.

## 9. Moving from version 1

- The server starts writing device lists for everyone with a chat session. Apps that know
  version 2 use the list as soon as an account has one and ignore its chain; version 1 apps
  keep using the chain. So during the move a v1 sender seals to a letterbox that v2-only
  devices (new ones) do not hold: those messages reach them only through history sharing.
  To keep that window short: web first (it updates itself), then the TestFlight build and
  the Android release the same day, then `belfry.json` says version 2 and v2 apps stop
  reading v1 chains for sending altogether.
- A v2 app that holds letterbox secrets keeps opening v1 wraps for good.
- Nothing on the relay is deleted.

## 10. Open, to settle in review

- The belfry writes the list as the account, through the signer: a belfry on Keycast needs
  the same call (all four run the own signer today).
- A participant whose list lists 20 devices of which most are dead: the cap keeps the most
  recently active; a sender still makes up to 20 wraps for them.
- Reading old wraps: `#d` is an indexed tag on rnostr and zooid? (to verify live before
  building; `#lb` was not.)
- Group conversations between 9 and more people.
- Whether Share should offer "only this conversation".
