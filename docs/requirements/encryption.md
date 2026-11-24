# End-to-end encryption

Prefix `ENC`. The current design is **format 3, one key per conversation**: docs/encrypted.md
(read §16–§28: where a later section and an earlier one differ, the later counts). Version 1
(docs/sealed.md, a letterbox key per person) stays readable; docs/sealed-v2.md was never built
and is withdrawn. Canvas: "Encrypted conversations" https://claude.ai/artifact/JrubHFFVtgRHFCGp1ePTqq.
Teaching page: the "How encryption works" explainer (private preview).

## What it protects, said plainly

- **ENC-1** [all] The belfry, its admins and anyone reading its disks cannot read the content of
  private messages, Notes and private groups. (owner, 2026-11-20: DMs must not be readable by the
  belfry's admins)
- **ENC-2** [all] Apps and pages say plainly what it does not protect: the belfry issues the
  device lists, so a dishonest belfry or someone holding a person's mailbox can add a device and
  read NEW messages from then on (every device of that person shows the addition, with Remove);
  the belfry sees who writes to whom, when, how much; a removed device keeps what it fetched.

## Keys and devices

- **ENC-10** [all] Every device (browser, app) makes its own key at sign-in; that key never leaves
  the device. The identity key stays in the belfry's signer and plays no part in encryption.
  → privacy.md PRIV-30
- **ENC-11** [all] "We need to trust any device that entered the 6-digit": a device that signed in
  with the code is on the person's device list at once and reads and writes NEW messages at
  once; nobody has to approve it first. (owner, 2026-11-23)
- **ENC-12** [all] The belfry publishes each person's device list: device public keys only.
  "The details of the devices of correspondants should never be shared for privacy": others see
  a person and a device count; only my own devices have names (from my own sessions).
  (owner, 2026-11-23)
- **ENC-13** [all] For earlier messages a new device asks; my other devices show ONE card: the
  addition, with "Share earlier messages" (the button) or "Remove this device". No hand-over by
  the other person's devices in v1. (owner, 2026-11-23)
- **ENC-14** [all] A device's first screen after sign-in lists the devices already on the account
  with the date each was added and Remove ("These devices can read your messages").
- **ENC-15** [all] At most 20 devices per person (a 21st is not listed and says "Remove one to read
  encrypted messages here"; nothing is evicted); at most 100 devices per encrypted conversation.
  Idle is never removal: a device leaves the list only when its session is revoked, signed out
  or expires.
- **ENC-16** [all] A member's own agent may read their encrypted conversations only through a
  setting on the agent's profile, off by default ("Give access to my private messages and private
  groups"), and only "from that moment on". (owner, 2026-11-23)

## Conversations and keys

- **ENC-20** [all] The sender makes a conversation key (a key pair; the public half names it),
  stores a copy of the secret for every listed device of every person, and each message names
  its key. A new key when a device list or the people changed, or after 30 days. Two keys made
  at once are fine. (owner's model, 2026-11-23)
- **ENC-21** [all] A device sends only with keys made by its own account's devices (private groups:
  the contract's exception), never with a key the belfry made for e-mail.
- **ENC-22** [all] Private groups are encrypted too, everything in them except what the relay or
  the belfry must read in clear (membership, metadata, roles); a joiner reads only from joining.
  (owner, 2026-11-23) → encrypted.md §16.9
- **ENC-23** [all] Mail into a private conversation or private group still works: the belfry
  encrypts it as a sender and forgets the clear text; the app says it came by e-mail.
  (owner, 2026-11-23)
- **ENC-24** [all] Buttons keep working in private groups; the belfry never learns file keys.

## Never decide by absence, never fall back

- **ENC-30** [all] Never decide by absence: no list known for a person = Held, whatever the wait;
  "which devices does this account have" is always one of three answers (a list / a service key /
  no list), never two. (Android review, 2026-11-23) → encrypted.md §17.1, §20
- **ENC-31** [all] Once encrypted, never clear: a conversation a device has ever seen encrypted
  never sends unencrypted; what can't be encrypted is held on the device with its text kept and
  words that say why. An unencrypted event in an encrypted conversation is marked "Not
  encrypted" and never acts on anything.
- **ENC-32** [all] A device out of the list never sends into an encrypted conversation. Format 3:
  the composer stays; what it sends is held with "This device isn't set up for encrypted messages
  yet · Try again" (docs/encrypted.md §3); no version-1 lock UI on a format-3 belfry. Version 1:
  the composer was replaced by the locked line. (lead, 2026-11-22 / 2026-11-24)
- **ENC-33** [apps] The states, with words, never a blank or a spinner without end: Opening ·
  Can't be read on this device (Ask for earlier messages) · Held (Try again; text kept) · Not
  encrypted (no lock). A locked view names the device that can unlock it and when it was asked.
- **ENC-34** [all] Every device checks its own account's lists and shows a card for every device
  added (also one added and removed while offline); two different lists of the same number show a
  warning that doesn't go away by itself.
- **ENC-35** [all] No backdating: key and message events carry the time of sending; the relay
  refuses one more than 5 minutes off.

## What is never done in encrypted conversations

- **ENC-40** [apps] Link previews in sealed (v1), format-3 conversations and private groups: only
  built by the sender's phone (it fetches the page itself) and attached inside the message with
  the picture AES-GCM encrypted; readers never fetch; the web sends none there (a browser can't
  fetch the page). → docs/link-previews-v2.md (lead's ruling, 2026-11-24)
- **ENC-41** [all] No typing indicators in encrypted conversations, version 1 and format 3
  (docs/encrypted.md §6).
- **ENC-42** [all] Events, tickets, check-in and proposals exist only outside private groups in
  this version; announcements by e-mail and mention digests don't exist for a private group.

## Rollout

- **ENC-50** [team] Order set by the owner: a canvas of every screen first, then the web proven
  working, then Android, then iOS, "asking his confirmation between each step". (owner,
  2026-11-23)
- **ENC-51** [all] Switched per belfry (`belfry.json` `encrypted.format` 1 | 3), belfry.sh first
  (the trial, 2026-11-24); apps read both formats before any switch and write the one the belfry
  names. One way: a device that has seen format 3 for a belfry never writes format 1 there again.
- **ENC-52** [server] A switch needs first: the relay rule (key and message kinds served only to
  the account in `p`), device lists on, and the phones warned (they go quiet for new private
  messages there until they build format 3; the owner accepted this for belfry.sh).
- **ENC-53** [all] Version-1 messages stay readable where the device holds the letterbox secret;
  after the switch no app writes v1 rosters, anchors, requests or hand-overs; the §5.7 self-join
  code is removed.

## Guests

- **ENC-60** [all] Guests stay out of encrypted conversations for now: a conversation with a guest
  is not encrypted (one that already was holds: "this encrypted conversation now includes a
  guest"), guests can't post in private groups ("Guests can't take part in private groups yet"),
  a guest's session doesn't run encryption. (owner, 2026-11-23) → encrypted.md §25

## Withdrawn (do not bring back)

- **ENC-90** Withdrawn on 2026-11-23: §5.7 "a device adds itself" of version 1 (flag never turned
  on).
- **ENC-91** Withdrawn on 2026-11-23: sealed v2 (a copy per device, belfry-signed lists).
- **ENC-92** Withdrawn on 2026-11-23: the "strict rule" (belfry refusing messages under a key made
  before a device was removed): an honest belfry already blocks a removed device, a dishonest one
  would ignore it.
- **ENC-93** Withdrawn: idle removal of devices (§3 "used in the last 30 days").
