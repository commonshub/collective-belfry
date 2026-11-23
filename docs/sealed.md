# Sealed conversations

Direct messages (and e-mails to a person) that only the people's own devices can read.
Contract for the web chat, the iPhone app, the Android app and the server. Version 1,
2026-11-20. Vectors: `docs/sealed-vectors.json` (copied to each app's `vectors/sealed.json`).

## 1. Why, and what it protects

A member's identity key lives in the signer, on the belfry's server. Until now a direct
message was a NIP-17 gift wrap to that key, and the signer decrypted it for the app: the
server saw every message, and an admin could read all of them, past and future.

A sealed message is encrypted to a **letterbox key** whose private half exists only on the
person's devices. The signer is never asked: sealing, opening and signing happen on the device.

- Protected: everything stored (relay, backups, database). The server and admins cannot read it.
- Not protected: a device that is compromised; the web app's code is served by the belfry;
  an admin can still make a *new* device appear for someone. Since 2026-11-22 (§5.2 step 2,
  Xavier's choice of no tap over a prompt) the person's own devices add it and hand it the
  keys by themselves, so such a device reads what the person can read; the person sees
  "‹device› was added · Not you? Remove it" on every device (§5.2 step 2a). Before that
  date a new device had to be allowed by hand.
  Since 2026-11-23 (§5.7, Xavier: a signed-in device must work at once), where the belfry
  turns it on, such a device adds ITSELF: it reads everything sealed from then on without any
  of the person's devices being online; the same notice shows on every device.
- E-mail: sealed on arrival and in the thread; in clear at the moment it enters or leaves the
  belfry's mail server, and at the other side's provider.
- A removed or stolen device can no longer read, but it can still write messages dated before
  its removal, which are accepted as old messages of the person (§4). Accepted in this version.
- No forward secrecy between key changes (a letterbox key changes when a device is removed).

Channels and NIP-29 groups are not covered by this version.

## 2. Keys

| Key | What | Where its secret is |
|---|---|---|
| account | the person's Nostr identity (`rumor.pubkey`, `p` tags) | the signer (unchanged) |
| device | the session key of this sign-in (the NIP-46 client key, `clientSecret`) | this device only |
| letterbox | a secp256k1 key pair per person; a new one each time a device is removed | every device of the person's current roster |

All public keys are 32-byte x-only hex. All encryption is NIP-44 v2. "conv(a_secret, b_pub)"
is NIP-44's conversation key.

A device keeps every letterbox secret it was ever given (the **key chain**), each with its public key.

## 3. Roster: which devices are a person's

### 3.1 Roster event, kind 8560

Signed by a **device key**, published on the community relay (the relay accepts any author
on a member's authenticated connection).

```
kind 8560, pubkey = the signing device, created_at = now, content ""
["p", <account>]
["n", "<sequence, from 1>"]
["prev", <id of roster n-1>]          absent when n = 1
["lb", <letterbox public key>]
["device", <device pubkey>]            1 to 20 of them, the signer's own included when it stays
["ready"]                              optional, see §6
```

### 3.2 Anchor, kind 30078 `d = belfry:sealed`

Signed by the **account** (through the signer), replaceable:

```
kind 30078, pubkey = account, content ""
["d", "belfry:sealed"], ["root", <id of the roster with n = 1>], ["head", <id of the newest roster>]
```

It stops another member from publishing a roster "for" someone, and a removed device from
taking the chain back: only a live session of the account (or the server) can make it, and a
removed device's session is revoked. Every device that publishes a roster publishes the anchor
again together with it: the roster is signed locally, the anchor naming it is signed by the signer,
and only then are both sent: the roster first, the anchor the moment the relay accepted the roster
(an anchor naming a roster the relay refused would un-anchor the standing head). The anchor is a
replaceable event: its `created_at` is max(now, the standing anchor's `created_at` + 1), never
the bare clock — a relay keeps the lower id of two anchors in one second and discards an older
one, so a device whose clock is behind another's would otherwise never land a change. When the anchor cannot be signed,
neither is sent and the change is tried again later. A change counts as done only when the relay
accepted both events; the device then reads its chain again from the relay and checks its roster is
on the anchored path. When it is not (another branch was anchored, an event was refused), it does
the change again on the new head, and until then shows the action as not done. A device that finds a roster of its own past the anchored end (the
anchor was refused, the app closed in between) anchors it at its next start and at each upkeep, with `head` = that roster (and the same `root`). `head` absent = the root.

### 3.3 Valid chain

Read `{kinds:[30078], authors:[A], "#d":["belfry:sealed"]}` and `{kinds:[8560], "#p":[A]}`,
always without `since`.

- Root: the event named by the anchor, `n = 1`, no `prev`, its signer is one of its devices.
- Roster n: `prev` is the id of a valid roster n−1; its signer is a device of roster n−1;
  at least one device; `p` = A; when any device of n−1 is missing, `lb` differs from the
  `lb` of every earlier roster of the chain; when none is missing, `lb` equals the previous one.
- Also invalid: not exactly one `p`, one `n` and one `lb`, more than one `prev` (a tag given twice makes the event invalid; the same for the anchor's `d` and `root`, a wrap's `lb`, and the `p` of an 8561 or 8564; an anchor with more than one `head`, or a malformed one, stays valid with `head` read as absent, as the vectors check); `n` that is not the previous `n` + 1, a device listed twice, more than 20 devices.
- The anchored path: the rosters from the root to the anchor's `head`, when `head` is a valid
  roster descending from the root; otherwise the root alone. A roster that is not on that path
  and does not descend from its end is ignored, whatever its id: a device dropped by the path
  cannot fork it.
- After the end of the anchored path (rosters published but not anchored yet): each step must be
  valid as above; when two valid rosters have the same `prev` there, the lower id wins for now
  (ids compared as lower-case hex strings) and the next anchor settles it. A device always
  anchors its own roster at once, whatever the ids; when the anchor then names another branch
  (another of my devices was a moment later), it re-reads the chain and does its change again.
- Head: the last roster reached. Its `lb` is where to seal for A. Its devices are A's devices.
- No going back: a client that has pinned head H for a person ignores an anchor whose `head`
  is an ancestor of H (it keeps H). An anchor naming another branch of the same root is followed, but
  when the pinned head had already been seen anchored it is a SWITCH: the client treats it like a
  fresh start (§3.4: the amber notice in each conversation with that person), because the account
  key that signs anchors is held by the server. After a switch, a device of the new head that was
  not in the head this client held before is never answered without a prompt (§5.5): it goes
  through Allow. The devices of the head held before the switch are the trusted ones; a device
  becomes trusted when the person allows it on this device, or when a later roster of the followed
  chain, signed by a trusted device, ADDS it (a roster that merely keeps it vouches for nothing:
  devices publish rosters on their own, for `ready` or a gone session). A device the person removed
  comes back untrusted. (Two of a person's devices anchoring different rosters within seconds can cause
  a false notice; rare, accepted.)
- Anything else is ignored silently (wrong signature, unknown prev, signer not in the previous roster).

### 3.4 Pinning and resets

A client remembers, per person, the root it first accepted and the head it last accepted (with
enough of its path to recognise it, and which heads it saw anchored). This memory is durable: it
survives restarts and is wiped only with the account. A pin a restart forgets protects nothing.
When the anchor later names a root that is not the pinned one: the person started fresh.
Accept the new chain, pin it, and show in each conversation with them, once:
"‹Name› signed in on a new device that can't read earlier messages yet" (amber, opens Share history, §8).
The same when it is my own account and this device is not in the new chain: this device is out (§5.4).

Notices for ordinary changes, one roster step at a time (compare the devices of each valid
roster with the one before it, dated by that roster; two changes while away give two lines): "‹Name› added a device" for each device
that appears, "‹Name› removed a device" for each that disappears. Never a device's name or kind
for someone else. Date = the roster's `created_at`. Show them only for roster steps dated at or after the moment this
device first saw the conversation sealed; the Encryption screen's grey
line covers the last 30 days.

"Added from ‹device›" on my own list: the signer of the first roster that contains the device
(nothing for a device that added itself, i.e. a root).

### 3.5 My device names, kind 30078 `d = belfry:device:<device pubkey>`

One event per device, signed by the account, replaceable, written only by that device (so two
devices never overwrite each other). Content = NIP-44 with conv(letterbox secret, letterbox pub)
of the head letterbox at the time of writing, tag `["lb", <that letterbox pub>]`:

```json
{"v":1,"name":"Safari on Mac","kind":"computer","added":1790000000}
```

`kind` is phone | tablet | computer. The name is the `device` of the device's own row in
`/api/me/sessions` (found by `client_pubkey`), so it matches what the Allow prompt showed; the
platform's own name for itself when that is empty. Written when the device joins the head and
again after each letterbox change. Read with `{kinds:[30078], authors:[me]}` and the `d` prefix;
an entry under an older letterbox is opened with the key chain. Entries of devices that are in no
roster are ignored. Other people never receive the names.

## 4. Messages

Everything is NIP-17 / NIP-59 as today, with three differences.

Rumor: unchanged (`kind 14`, `15`, `1301`, `7`, `5`; `pubkey` = the account; unsigned; id computed). Kind 15 is a file (vectors/dm-files.json in belfry-android: AES-256-GCM on the device, ciphertext on Blossom, key and nonce in the rumor's tags): in a sealed conversation it is sealed by the device exactly like kind 14, decided before the upload, held on the device while the conversation is undecided, and never sent the old way into a sealed conversation (added 2026-11-21, as built on web, iOS and Android).

Seal, kind 13:
- `pubkey` = the sending **device**, signed with the device key, `tags: []`, `created_at` backdated as today;
- `content` = NIP-44 with conv(device secret, recipient's letterbox pub) of the rumor JSON.

Gift wrap, kind 1059:
- signed by a one-time key, `created_at` backdated as today;
- `content` = NIP-44 with conv(one-time secret, recipient's letterbox pub) of the seal JSON;
- tags `["p", <recipient account>]` (unchanged: push, filters and the relay rely on it) and
  `["lb", <recipient's letterbox pub>]`.

A wrap with an `lb` tag is sealed; one without is the old kind and goes to the signer as before.

**Silent wraps** (2026-11-23). A wrap that addresses only its own sender carries one more tag,
`["silent", "1"]`: the copy to myself of a message I send (sealed or old-style), a note to
myself, an 8563, the keys and history wraps to my own devices. The belfry sends no push for a
wrap that carries it (it cannot see who sent a wrap, so without the tag every device of mine
would say "New message" for my own notes and copies). Readers ignore the tag, and every other
wrap tag they do not know; only the exact tag `["silent", "1"]` counts (another value is
read as absent: the wrap stays valid, a push goes out). It is a request, not a proof: a sender who puts it
on a wrap to someone else only keeps their own message from being announced; it arrives all
the same. Given up, said plainly: the relay can tell which wraps to an account are that
account's own (it already sees them published on the sender's authenticated connection next
to the copy for the other side).

Before each send, and when a sealed conversation is opened, ask the relay again for every
participant's anchor and rosters (keep them on the live subscription too; wait at most 3 s, then
use what is known): sealing to a letterbox its owner has left lets the removed device read.

One wrap per participant's head letterbox, and one to my own head letterbox (the self-copy),
through the outbox to the community relay as today. No signer call.

Opening (the rumor's id is always computed by the opener; an `id` or `sig` field inside is ignored): find the letterbox secret by the `lb` tag (no secret: the message is locked on this
device, §5.3; keep the wrap). Open wrap, verify the seal's signature, open the seal with
conv(letterbox secret, seal.pubkey). Accept the rumor only when `seal.pubkey` is a device of
`rumor.pubkey`'s valid chain — in the head, or in an earlier roster when the rumor's `created_at`
is not after the `created_at` of the roster that dropped it. The chain may not be loaded yet:
keep the wrap and retry when it is (on each new roster of that author and at each start);
never mark it seen on that failure, and give up only after 30 days. A wrap that does not
decrypt with the right letterbox secret, or whose seal signature is bad, is dropped at once. A mail from the belfry
(§7) is the only exception: a rumor of kind 1301 whose `pubkey` and whose seal's signer are both the
belfry's key (the key in belfry.json, not a name looked up at the time). Any other kind sealed by
that key is refused.

Reading filter: unchanged `{kinds:[1059], "#p":[me]}`; a device that just received keys (§5)
asks once from 0.

Reactions, taking a reaction back, e-mail threads, conversation ids, read state: unchanged
(read state stays the `belfry:read` event of today in this version).

## 5. Devices joining and leaving

### 5.1 The first device

Members and admins only (never a guest session). No anchor for my account — known from an answer the relay gave AFTER authentication, complete to EOSE (a CLOSED is not an answer; the REQ itself must have been sent after authentication); never from a timeout or a pre-AUTH answer — and I am capable: make a letterbox, publish roster n = 1 with this
device, then the anchor, then its device name (§3.5). If an anchor appeared meanwhile (two devices at
once), drop mine and go to 5.2.

### 5.2 A new device asks

Device request, kind 8562, signed by the new device:

```
kind 8562, pubkey = new device, content ""
["p", <account>], ["link", <hex HMAC-SHA256(key = the 32 bytes of the link secret, message = the 32 device pubkey bytes)>]   link optional
```

Published at sign-in when a chain exists and this device is not in its head; again every 24 h
while waiting. The device shows the banner "Messages are locked on this device".
Until it is allowed it reads no sealed message at all, old or new (it has no letterbox secret).

An existing device (in the head), on seeing a request `{kinds:[8562], "#p":[me]}`:
1. `GET /api/me/sessions`: the request's pubkey must be the `client_pubkey` of a live session with `"chat": true`;
   otherwise ignore it. The prompt's device name comes from that session.
2. Allow at once, without a prompt (Xavier, 2026-11-22: "If I'm connecting a new device to my
   account it should automatically consider it as a sealed device"), when ALL of these hold;
   otherwise ask: Allow / Not me.
   - the sessions list of step 1 was read for this request (never a cached copy), and the
     session is live and `"chat": true`;
   - the request's `created_at` is not before that session's `created_at` minus 120 s, and
     not more than 30 days old;
   - this device is in the head it holds (pinned), on the anchored end, and trusted (§3.4);
   - the chain never had the asking device before (no roster of it, at any point, listed it):
     a device that was removed and whose session outlived the removal asks, it is never put
     back by itself (found by Android's tests, 2026-11-22);
   - fewer than 3 devices were added to my head without a prompt in the last 24 hours
     (counted from the rosters' own times, whoever added them). A fourth asks.
   A valid `link` (a code this device showed in the last 15 minutes) allows at once too and
   does not count toward the 3.
   Two devices of mine may both see the request: each first reads the chain again; a roster
   that already has the new device means there is nothing to add, only the keys to hand over
   (5.5, which is idempotent per device).
2a. The notice. Every device of mine that sees a roster add a device WITHOUT this device
   having shown a prompt or a link code for it shows, on top of Messages and in Devices:
   "‹session name› was added ‹time› · Not you? Remove it". It stays, across restarts, until
   the person taps "It was me" or Remove, or the device leaves the head. Remove = 5.6
   (revoke the session first, then the roster without it and a new letterbox). The device
   that did the adding shows it as well. One notice per added device.
2b. Answered once. "It was me" on one device clears the notice on all of them (Xavier,
   2026-11-22: "make sure we only need to do that once, in case I have more than 2 devices").
   The device that was tapped sends a sealed rumor (§4 form, to my own head letterbox, seal
   signed by this device), not through the outbox's e-mail path and never shown as a message:
   ```
   kind 8563, pubkey = the confirming device, tags [["p", <account>], ["device", <added device>], ["e", <id of the roster that added it>]], content ""
   ```
   A device clears its notice for <added device> when it accepts an 8563 whose seal signer
   (a) is in my valid chain in the roster BEFORE the one named by `e` (so the added device
   can never confirm itself, nor can two devices added together confirm each other), (b) is
   in the head now, and (c) names the roster that really added that device. Anything else is
   ignored without a word. A device that is added later reads the 8563s from its letterbox
   like any sealed rumor and never shows a notice already answered. An 8563 is never part of
   shared history (8564). Remove needs nothing new: the roster without the device clears the
   notice everywhere. A device that does not know kind 8563 keeps its own notice until
   tapped (the behaviour before this rule).
   Readings fixed with the first build (Android, vectors `confirm` in belfry-android
   vectors/sealed.json): the rumor's `pubkey` is the seal's signer (never the account);
   content empty; `p`, `device` and `e` exactly once; `e` is never the root; the notice is
   per (device, the roster that LAST added it): a device removed and added again gets a new
   notice; an 8563 whose roster this device does not have yet is kept and judged again when
   the chain moves, up to 30 days, clearing nothing meanwhile; a device whose word would not
   count clears its own notice and sends nothing.
   What this gives up, said plainly (§1): whoever can open a chat session on my account (the
   belfry's server, so its admins; or someone with my mailbox) gets my sealed messages the
   next time one of my devices is online, and I learn it from the notice, not before.
3. Allow: when the roster already has 20 devices, refuse and say so. Else publish roster n+1 = same `lb`, devices + the new one, with its anchor; only when that
   change is done (§3.2: accepted and on the anchored path) send the keys (5.5) to the new device.
   Never the keys first: a device that ends in no roster must not hold them.
4. Not me: `POST /api/me/sessions/revoke` for that session. No roster change.

"Link a phone or tablet": the QR becomes `https://chat.<domain>/login/<username>#l=<64 hex>`
(a fresh random secret each time the screen opens; kept in memory 15 minutes; the fragment
never reaches a server). A device that signs in from that link puts the `link` tag in its request.

### 5.3 Locked

A conversation whose newest messages this device can't open shows "Locked on this device" in
the list; the Messages screen shows the banner. Old-style messages (no `lb`) stay readable as today.
A wrap that can't be opened says only that it is for me, not which conversation: so the list
marks "Locked on this device" the conversations this device already knows whose participants
all have a ready chain, while this device is not in my head; a conversation that exists only as
unopened wraps is not listed. The banner shows when this device is not in my head or holds a
wrap whose `lb` is a letterbox of MY OWN chain (any roster of it) that it has no secret for. A
wrap naming any other `lb` is dropped: anyone can write one — but only once my own chain is
current on this connection (my anchor and rosters answered after authentication, to EOSE); until
then it is kept, never marked seen: a message sealed to my newest letterbox can arrive before the
roster that announces it. "My chain's letterboxes" are those of VALID rosters (the current chain
and, after a fresh start, the previously pinned one), never of any event that merely names me. Only "this device is not in my head"
hides messages or the composer.

### 5.4 Starting fresh

When this device is not in the head and the user says they have no other device ("Show how" →
"I don't have another device"), or when no device of the head has a live session in
`/api/me/sessions` — the head being known from an answer the relay gave after authentication,
complete to EOSE, on this connection, for my anchor and my rosters, never from what the device had
stored: make a new letterbox, publish a new root (n = 1, this device) and replace the
anchor. Everyone sees the amber notice (§3.4). Devices of the old chain that come back see
they are out: they publish a request like any new device, and once allowed they send their
old key chain (5.5), which unlocks the earlier messages again.

### 5.5 Handing keys over

A gift wrap to the **device**: rumor kind 8561, `pubkey` = the giving device; seal signed by the
giving device, content conv(giving device secret, receiving device pub); wrap `["p", <receiving
device pubkey>]`, content conv(one-time secret, receiving device pub). Rumor:

```
kind 8561, tags [["p", <account>]], content =
{"v":1,"account":"<hex>","keys":[{"lb":"<pub>","secret":"<hex>"}, ...]}
```

`keys` = the giver's whole chain, in several 8561 wraps when one would pass the 40 000-byte ceiling. The receiver reads `{kinds:[1059], "#p":[my device pubkey]}`,
accepts when the seal's signer is a device of any roster of its own account's chains (current
or, after a fresh start, the previous pinned one), checks each secret against its `lb`, merges,
and reads its wraps again from 0.

A device that IS in the head but lacks the head letterbox's secret (a hand-over was lost) publishes
its 8562 as well; a device of the head that holds the secret answers it with the keys at once,
without a prompt (the device is already allowed), after checking its live chat session. "In the
head" means in the head THIS device holds (its pinned head, never an older roster an anchor points
back to), and the request must be newer than the roster that added the device and than the last
hand-over this device made to it (remembered across restarts). While it waits, the device shows
"Waiting for another of your devices" in place of the composer of sealed conversations.

### 5.6 A device leaves

- Removed from the list by the user, or its session is gone from `/api/me/sessions` (a well-formed
  answer that lists this device's own session; an empty or odd answer removes nobody; checked at
  start and every 6 h by each device; wait a random 0–60 s, re-read the head, skip if done):
  make a new letterbox, send keys (5.5) with the new secret to each remaining device, publish
  roster n+1 with the new `lb` and without the device (when the session list cannot be read, do
  nothing: never publish a new letterbox whose secret was handed to nobody); each device rewrites its name (§3.5) when it sees the new head.
  A user's removal revokes the session FIRST and changes nothing when that fails (a removed device
  with a live session could still have an anchor signed); keys go only to devices that stay AND have
  a live session AND, after a switch (§3.3), are trusted on this device: a new letterbox's secret is
  never handed to a device this device would not answer without a prompt.
- Signing out: the device revokes its session as today and wipes its keys. The others do the above.

### 5.7 A device that signed in adds itself

Xavier, 2026-11-23, after a first note on a new iPhone ended in "Locked on this device": "that
situation should not happen. If I'm properly logged in … I expect to be on a trusted device
and I should have access to my new notes and on other devices with older notes I would get a
message to share old notes with me." So a device no longer waits for another device to let it
in. It lets itself in for everything from now on; what came before arrives when one of the
older devices is next online.

**Where it is allowed.** Only when `belfry.json` says `"sealed": {"self_join": true}` (a server
setting, `SEALED_SELF_JOIN`), read fresh in that pass. Reading a join is not behind the flag:
every app and the server accept one from the day they know this section. The flag exists
because an app that does not know the rule ignores the roster and stays on the old head.

**When.** A device with a live chat session (its row in a fresh `/api/me/sessions`, `chat`
true) that is not in my head, and whose head it has from a complete authenticated answer of
this connection, does this at once, with no prompt on any device:

1. make a new letterbox key pair;
2. sign roster n+1 **itself**: `prev` = the head, `devices` = the head's devices in their
   order, then this device, last; `lb` = the new letterbox; `ready` by the usual rule;
3. have the account sign the anchor with `head` = that roster (the signer does this: it is
   what proves the session; the roster alone proves nothing);
4. publish both; the change is done as in §3.2 (both accepted, the roster on the anchored
   path when read back), else undo locally and try again;
5. hand the new letterbox's secret to every other device of the roster (5.5: one 8561 wrap
   per device key, silent), and keep handing it to a device that asks (8562) and is in the head;
6. publish its 8562 as before: a device of the head that holds the older letterboxes answers
   it without a prompt (5.5, "a device that IS in the head but lacks a secret"), which is how
   the earlier messages arrive. Until then the device shows, above them, "Earlier messages
   arrive when one of your other devices is next open: ‹names›" and nothing is locked: it
   reads and writes everything sealed from its join on.

No chain yet → 5.1 as before. More than 20 devices → it asks as in 5.2 (someone must remove
one). A device that was in the chain before and was removed never adds itself again: it asks
(5.2), and the person answers on another device. Two devices joining at once: the anchor
names one; the other reads the chain again and joins after it.

**Validity (§3.3, added).** A roster is a valid step after `prev` also when: `n` = prev's + 1;
its signer is NOT in prev's devices; its devices are exactly prev's, same order, plus the
signer last; its `lb` is a letterbox the chain never had; **and it lies on the anchored
path**. Not anchored, it is nothing: anyone can sign one. (Server: `sealed._joins`.) The
device counts as added by itself (`added` → its own roster).

**Readings fixed before the first build** (raised by the web, 2026-11-23). (1) A client
accepts a join only where it sees it on the anchored path (root → the anchor's head). Never
in the provisional tail after the anchored end, and never in a pinned part the client had not
itself seen anchored. A join the client DID pin as anchored stays in its pin if the anchor
later points back ("no going back" holds for it like any roster). (2) After a switch (§3.3)
a joiner is NOT trusted by the join alone: the device keeps the Allow card for it and hands
it nothing until the person allows. (3) "Newer than the roster that added it": for a device
that added itself, that roster is its own join, and a request dated at or after it counts
(`>=`, not `>`); the joiner dates its 8562 at or after its roster; a refusal is remembered
per request, never against a later request of the same device.

**Tightened after Android's review** (2026-11-23, before anything was built; these replace
whatever above says otherwise).

- *Validity, more.* A join's `created_at` is not before its prev's. Its signer is in NO
  earlier roster of the path (a device the chain ever had asks, 5.2; a removed device with a
  live session cannot walk back in). **A device that added itself changes nothing until it is
  endorsed**: a normal roster (one that adds, removes, or only restates) signed by a device
  that joined is invalid, on the anchored path and in the provisional tail alike, while no
  roster after its join carries `["endorse", <that device>]` signed by a device that was in
  the roster BEFORE that join. So a joiner reads and writes, and can neither remove my
  devices nor add others. (Server: `sealed._unendorsed`, tests.)
- *"It was me" endorses.* Besides the 8563, the device that is tapped publishes roster n+1 =
  same devices, same `lb`, with one `["endorse", <device>]` tag per joiner it confirms (and
  its anchor). Only that tag endorses: the rosters devices publish by themselves (`ready`,
  a session gone, another device let in) never do, so the limits lift only when the person
  taps. From then on the joiner is a device like the others, and on a device that saw a
  switch it is trusted when the endorsing device is trusted there. Remove needs no
  endorsement (it is signed by an older device). A joiner that leaves the head is no longer
  waiting for anything.
- *From Android's first build of reading* (2026-11-23). Allow on the card endorses too: it
  is the person's own tap on an older device, so that device publishes the endorsement roster
  as "It was me" does. Any device IN the head that lacks the head letterbox's secret, after a
  join or after a removal's rotation alike, is "a letterbox behind" (composer kept, Remove,
  notices, answering, writing; only reading newer messages waits); the replaced-composer
  waiting state is for a device OUTSIDE the head only. The waiting line cannot be per
  message (a wrap does not say its conversation): it is a count on top of Messages and one
  line above the composer of sealed conversations while any wrap waits.
- *The server's guard for mail.* Before sealing a mail to a head that holds an unendorsed
  joiner, the belfry checks that joiner against the account's live chat sessions; not one of
  them, or the list cannot be read: 451, the sender tries again (the signer anchors what a
  session asks, so a join could name a key nobody honest holds; the person's own devices
  remove it when they next look).
- *Known limits, accepted.* Until the joiner's hand-over lands, my other devices cannot read
  what is sealed after its join; if it dies before handing the secret on, that lasts until it
  is back or removed. People I write with see nothing of a join: the notice on my own devices
  is the only alarm, and needs one of them opened. A join that races an older device's roster
  can look like a switch. Joins never prune: at 20 devices a new one asks. A joiner with a
  slow clock dates its roster at its prev's time.
- *More readings* (Android's vectors, 2026-11-23). A stored pin whose path holds a join past
  its anchored head is cut back to before that join and followed from there (`moved`).
  "Fewer than 3 let in within 24 h", for a joiner's older keys: three OTHER devices, the
  asking joiner not counted. A join that lost the anchor: the device joins again with a new
  letterbox; nothing checks that the lost one is never used.
- *An older device that lacks the new secret* is "in the head, a letterbox behind": it keeps
  everything (Remove, the notice, 8563, session checks, answering requests, writing: it
  seals to the head letterbox by its public key); only reading the newer messages waits
  ("Waiting for ‹session name›"). Never the locked / waiting state of a device outside the
  head. So a joiner that never hands the secret on can always be removed.
- *The joiner on first sight.* A device that follows a join reads `/api/me/sessions` fresh:
  a joiner that is not a live chat session there is removed at once (5.6), with the notice
  saying so. Notices and any per-day count use the time THIS device first saw the join, never
  the roster's own date.
- *Older keys.* A joiner is handed earlier letterboxes without a prompt only under exactly
  step 2's conditions (fresh session row, request not older than the session, pinned and
  trusted, never in the chain before, fewer than 3 devices let in within 24 h, joins
  counted); otherwise the Allow card. Nobody hands a joiner its whole chain because the
  joiner sent it an 8561: the "hand back" of 5.4 is for a fresh start only. A giver gives
  what it holds.
- *8563.* A confirmation signed by a device whose own added-notice is unanswered on this
  device does not count yet (kept, judged again).
- *Arrivals out of order.* A hand-over from a joiner that arrives before its roster is kept
  and tried again. A device that first follows a join reads its wraps again from 0.
- *Races and leftovers.* A join that lost the anchor to another roster is discarded and its
  letterbox never reused; if the winning roster already holds the device, it asks for keys.
  A device left out by a fresh start made elsewhere asks (5.2), it does not join. Remove on
  a joiner also offers to remove the devices added since, while unanswered.
- *Old apps.* An app that does not know this section rejects every join: a pinned device
  keeps its old head and treats wraps to the join's letterbox as not for it. So the flag
  goes on for a belfry only when its people's apps read joins (web live, the TestFlight
  build, the Android release), and the first build that reads joins re-reads from 0.

**What the other devices do.** They follow the anchored head as for any roster, take the new
secret from the joiner's 8561 (accepted: the giver is a device of the chain), answer its 8562
without a prompt, and show the 2a notice, once for all (2b): "‹session name› was added
‹time› · Not you? Remove it". Remove = 5.6 (revoke the session first; then a roster without
it and a new letterbox, which the joiner never gets). A device of the head that does not have
the new secret yet (the joiner went offline before handing it) shows "Waiting for ‹session
name›" on newer sealed messages and asks (8562); any head device holding it answers.

**What this gives up, said plainly.** Before, an outsider with a session on my account (the
belfry's server, so its admins; someone with my mailbox) got my sealed messages only once one
of my devices came online and added theirs (step 2), and only if fewer than 3 were added that
day. Now they read everything sealed from the moment they join, with none of my devices
needed, and the 3-a-day limit no longer stands in their way; they can write as me in sealed
conversations; and a joiner who never hands the new secret on keeps my own devices from
reading new messages until I remove it. They cannot remove my devices or add more. What stays:
they never get anything sealed before they joined unless a device of mine hands it over, and
every device of mine says who was added and offers Remove. §1's "Not protected" says the same.

## 6. When a conversation is sealed

A device adds `["ready"]` to the roster it publishes when every counted session of the account
is a device of that roster. Counted = a session of `/api/me/sessions` with `"chat": true` (the
belfry's own chat clients: the web chat and the phone apps; other apps and remote apps never
count) and last active less than 30 × 86 400 s ago (`last_activity`, or `created_at` when the session was never active). The device key is the key that list gives
as `client_pubkey`, for every way of signing in. Then: a device that sees this become true publishes
roster n+1 with `ready` (same devices, same `lb`). Once a chain has had `ready`, it stays ready
for everybody (later rosters without the tag don't unset it).

A conversation is sealed when every participant, me included, has a ready chain. From then on
it is never sent the old way again: each device remembers its sealed conversations (a local
set of conversation ids, not published; a conversation also enters the set the moment this device
accepts a sealed rumor in it), and re-checks at each send until the conversation is
sealed. If a participant of a sealed conversation later has no ready chain (a fresh start not
finished, an anchor that disappeared), seal to the last valid head this device knew for them;
never fall back. If there is no head to seal to at all, the message waits in the outbox or the send is refused
with the text kept in the composer; it is never sent the old way. And a message goes the old way
only on knowledge, never on silence: the device must hold, for some participant, a complete
answer from the relay (after authentication, to EOSE, on a live connection, within the last
10 minutes) showing no ready chain. When it cannot tell (offline, before authentication, no answer),
the message is neither sealed nor sent the old way: either the send is refused with the text kept,
or the message is held on the device undecided (shown as waiting) and decided at the first
complete answer. An old-style message (no `lb`) that arrives in a sealed conversation dated
after this device first saw it sealed is shown with a "not sealed" mark when it is a message (kind 14); an old-style reaction or
take-back (7, 5) there is ignored. An old-style rumor never replaces a message the device holds
(its id is computed by the opener on the old path too). Before a conversation is sealed: exactly today's behaviour, no lock. An account with no chain (an agent, a member holding their own key, an old
app) keeps the conversation unsealed.

The lock shows on a sealed conversation, always.

## 7. E-mail

In: for `<username>@`, when the member's chain is ready the belfry seals the 1301 rumor to the
head letterbox: wrap as §4 with `["lb", …]`; the seal is signed by the belfry's key as today.
Otherwise as today. The plain text is not kept or logged. When the member's chain cannot be read
(the relay does not answer), the mail is refused for now (451) and the sending server tries again:
it is never delivered unsealed because of a failure.

Out: unchanged wrap to the belfry's key. A member with a ready chain always seals with the
device, replies sent as a channel's address included, (content conv(device secret, belfry key), no signer call); without one, today's path.
The belfry accepts a seal signed by a device of the sender's
valid chain (`rumor.pubkey` = the member) instead of the account key; the belfry checks the
chain (the device must be in its head) and that the device is the `client_pubkey` of one of the member's live chat sessions. The
delivery report (7679) is encrypted to the sender's head letterbox when they have a ready chain,
with `["lb", …]`.

## 8. Sharing history with someone who started fresh

From the amber notice, by a deliberate tap, any participant of the conversation: a gift wrap
(§4 form, to the person's new head letterbox) of rumor kind 8564, `pubkey` = the sharer:

```
kind 8564, tags [["p", <the person>]]
content = {"v":1,"rumors":[ <rumor objects of the conversation, oldest first> ]}
```

Each rumor keeps its own `pubkey`, tags and id, so the receiver computes its conversation as usual.

Only kinds 14, 7 and 5 are shared and taken (never an e-mail, and in v1 never a file, kind 15: a person who started fresh gets the conversation's text again, not its pictures and files). The 8564's `p` must be the receiver. A rumor the receiver already holds is left as it is, and one that later arrives sealed in the normal way loses its "shared" mark. Only the conversation's sealed messages are shared (older ones are still opened by the person's
signer). At most 200 rumors per wrap (in practice the size decides), and the 8564 rumor itself (its whole JSON, as UTF-8 bytes, the shared rumors escaped inside its content) at most 40 000 bytes, whichever comes first (NIP-44 carries
65 535 bytes, and the wrap holds the seal's base64 of the padded rumor); several wraps for more.
A single rumor larger than that is left out. The same ceiling holds for any sealed rumor (§4):
a sender never seals a rumor whose JSON (the exact string that is encrypted, as UTF-8 bytes) is over 40 000 bytes.
Sealed rumors are serialised compactly (no spaces), keys in the order `id`, `pubkey`, `created_at`,
`kind`, `tags`, `content`, non-ASCII characters and `/` not escaped; the ceiling is the sender's
business only, an opener never refuses a rumor for its size. The receiver takes the rumors one by one: a rumor is added when the sharer and the receiver are
both participants of its conversation (author or `p`) and the receiver doesn't have it yet, marked as shared
by that person (they are not proven to be authentic). The mark is kept on the receiving
device only.

## 9. Errors and edge rules

- Never fall back to the signer for a sealed conversation.
- A send while my chain's head changed: re-seal to the new head on the next outbox attempt.
- A wrap whose `lb` is not the recipient's head is still opened when the secret is in the chain.
- Kinds 8560, 8562 and the anchor are public to members: they show how many devices someone
  has and when they changed, nothing else.
- The relay ignores a `#lb` filter: read wraps by `#p` only and match `lb` on the device.
- A queued wrap is sealed again when the head it was sealed to has changed before it is sent
  (my own head for the self-copy, the recipient's for theirs).
- Limits: 20 devices per roster; key chain of any length.
