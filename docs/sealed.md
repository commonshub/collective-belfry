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
  an admin can still make a *new* device appear for someone, which everybody sees as a notice
  and which cannot read earlier messages.
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
["d", "belfry:sealed"], ["root", <id of the roster with n = 1>]
```

It stops another member from publishing a roster "for" someone. Only a session of the
account (or the server) can make it.

### 3.3 Valid chain

Read `{kinds:[30078], authors:[A], "#d":["belfry:sealed"]}` and `{kinds:[8560], "#p":[A]}`,
always without `since`.

- Root: the event named by the anchor, `n = 1`, no `prev`, its signer is one of its devices.
- Roster n: `prev` is the id of a valid roster n−1; its signer is a device of roster n−1;
  at least one device; `p` = A; when any device of n−1 is missing, `lb` differs from the
  `lb` of every earlier roster of the chain; when none is missing, `lb` equals the previous one.
- Also invalid: not exactly one `p`, one `n` and one `lb`, more than one `prev`; `n` that is not the previous `n` + 1, a device listed twice, more than 20 devices.
- Two valid rosters with the same `prev` (ids compared as lower-case hex strings): the lower id wins; the other and its descendants are ignored.
- Head: the last roster of the valid chain. Its `lb` is where to seal for A. Its devices are A's devices.
- Anything else is ignored silently (wrong signature, unknown prev, signer not in the previous roster).

### 3.4 Pinning and resets

A client remembers, per person, the root it first accepted (and the head it last saw).
When the anchor later names a root that is not the pinned one: the person started fresh.
Accept the new chain, pin it, and show in each conversation with them, once:
"‹Name› signed in on a new device that can't read earlier messages yet" (amber, opens Share history, §8).
The same when it is my own account and this device is not in the new chain: this device is out (§5.4).

Notices for ordinary changes, one roster step at a time (compare the devices of each valid
roster with the one before it, dated by that roster; two changes while away give two lines): "‹Name› added a device" for each device
that appears, "‹Name› removed a device" for each that disappears. Never a device's name or kind
for someone else. Date = the roster's `created_at`. Don't show notices for changes older than
the newest message already shown at first sync of a conversation; the Encryption screen's grey
line covers the last 30 days.

"Added from ‹device›" on my own list: the signer of the first roster that contains the device
(nothing for a device that added itself, i.e. a root).

### 3.5 My device names, kind 30078 `d = belfry:devices`

Signed by the account, replaceable, content = NIP-44 with conv(letterbox secret, letterbox pub)
of the head letterbox, tag `["lb", <that letterbox pub>]`:

```json
{"v":1,"devices":{"<device pubkey>":{"name":"Safari on Mac","kind":"computer","added":1790000000}}}
```

`kind` is phone | tablet | computer. Each device writes its own entry (its name = the `device` of its own row in `/api/me/sessions`, found by `client_pubkey`, so it matches what the Allow prompt showed; the platform's own name for itself when that is empty)
when it joins, keeps unknown fields and other devices' entries, drops entries of devices no longer
in any roster after 90 days. Rewritten under the new letterbox after a key change. Other people
never receive it.

## 4. Messages

Everything is NIP-17 / NIP-59 as today, with three differences.

Rumor: unchanged (`kind 14`, `1301`, `7`, `5`; `pubkey` = the account; unsigned; id computed).

Seal, kind 13:
- `pubkey` = the sending **device**, signed with the device key, `tags: []`, `created_at` backdated as today;
- `content` = NIP-44 with conv(device secret, recipient's letterbox pub) of the rumor JSON.

Gift wrap, kind 1059:
- signed by a one-time key, `created_at` backdated as today;
- `content` = NIP-44 with conv(one-time secret, recipient's letterbox pub) of the seal JSON;
- tags `["p", <recipient account>]` (unchanged: push, filters and the relay rely on it) and
  `["lb", <recipient's letterbox pub>]`.

A wrap with an `lb` tag is sealed; one without is the old kind and goes to the signer as before.

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
(§7) is the exception: its seal is signed by the belfry's key.

Reading filter: unchanged `{kinds:[1059], "#p":[me]}`; a device that just received keys (§5)
asks once from 0.

Reactions, taking a reaction back, e-mail threads, conversation ids, read state: unchanged
(read state stays the `belfry:read` event of today in this version).

## 5. Devices joining and leaving

### 5.1 The first device

No anchor for my account and I am capable: make a letterbox, publish roster n = 1 with this
device, then the anchor, then `belfry:devices`. If an anchor appeared meanwhile (two devices at
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
2. With a valid `link` for a code this device showed in the last 15 minutes: allow at once.
   Otherwise ask: Allow / Not me.
3. Allow: send the keys (5.5) to the new device, then publish roster n+1 = same `lb`, devices + the new one.
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
wrap whose `lb` it has no secret for.

### 5.4 Starting fresh

When this device is not in the head and the user says they have no other device ("Show how" →
"I don't have another device"), or when no device of the head has a live session in
`/api/me/sessions`: make a new letterbox, publish a new root (n = 1, this device) and replace the
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

`keys` = the giver's whole chain. The receiver reads `{kinds:[1059], "#p":[my device pubkey]}`,
accepts when the seal's signer is a device of any roster of its own account's chains (current
or, after a fresh start, the previous pinned one), checks each secret against its `lb`, merges,
and reads its wraps again from 0.

### 5.6 A device leaves

- Removed from the list by the user, or its session is gone from `/api/me/sessions` (checked at
  start and every 6 h by each device; wait a random 0–60 s, re-read the head, skip if done):
  make a new letterbox, send keys (5.5) with the new secret to each remaining device, publish
  roster n+1 with the new `lb` and without the device, rewrite `belfry:devices`.
  A user's removal also revokes the session.
- Signing out: the device revokes its session as today and wipes its keys. The others do the above.

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
set of conversation ids, not published), and re-checks at each send until the conversation is
sealed. If a participant of a sealed conversation later has no ready chain (a fresh start not
finished, an anchor that disappeared), seal to the last valid head this device knew for them;
never fall back. Before a conversation is sealed: exactly today's behaviour, no lock. An account with no chain (an agent, a member holding their own key, an old
app) keeps the conversation unsealed.

The lock shows on a sealed conversation, always.

## 7. E-mail

In: for `<username>@`, when the member's chain is ready the belfry seals the 1301 rumor to the
head letterbox: wrap as §4 with `["lb", …]`; the seal is signed by the belfry's key as today.
Otherwise as today. The plain text is not kept or logged. When the member's chain cannot be read
(the relay does not answer), the mail is refused for now (451) and the sending server tries again:
it is never delivered unsealed because of a failure.

Out: unchanged wrap to the belfry's key. A member with a ready chain always seals with the
device (content conv(device secret, belfry key), no signer call); without one, today's path.
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

At most 200 rumors per wrap (in practice the size decides), and the 8564 rumor itself (its whole JSON, as UTF-8 bytes, the shared rumors escaped inside its content) at most 40 000 bytes, whichever comes first (NIP-44 carries
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
