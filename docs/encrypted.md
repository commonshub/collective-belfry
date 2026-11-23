# Encrypted conversations: one key per conversation

Status: **contract, not built** (2026-11-23). Replaces `docs/sealed.md` (version 1, a letterbox
key per person) for writing; version 1 stays readable. `docs/sealed-v2.md` (a copy per device)
was never built and is withdrawn. Teaching page for people: the "How encryption works" explainer.
Screens: the canvas "Encrypted conversations".

Build order set by Xavier: web, then Android, then iOS, each after his confirmation.

## 1. In one page

- Every device (a browser, an installed app) makes its own key when it signs in. That key never
  leaves the device. It is the session's client key of today: nothing new is generated.
- The belfry publishes, for every person, the list of their devices: the devices that entered
  the 6-digit code and are still signed in. The list holds public keys and nothing else.
- A conversation is a set of people: a private message, a group message, Notes to myself (one
  person), or a **private group** (a group only invited people see).
- The device that sends makes a **conversation key**: a key pair. Its public half is the key's
  name; its private half is the shared secret. The secret is encrypted once for every device of
  every person in the conversation and stored at the belfry. A device fetches its copy the
  first time it meets a message that names the key.
- Every message is encrypted for the conversation key's public half and says which key it used.
- The next sender makes a new key when anybody's device list has changed, or when the key is
  30 days old. Two senders making a key at the same moment is fine: both keys are stored, every
  message names its own.
- A new device reads and writes new messages at once. For earlier messages it asks; one of the
  person's other devices answers once, **Share earlier messages** or **Remove this device**.
- Nothing is ever sent unencrypted in a conversation that is encrypted. What cannot be
  encrypted is held on the device with its text kept.

What it protects: the belfry, its admins and anyone reading its disks cannot read the content
of these conversations.

What it does not protect, said plainly:
- The belfry issues the device lists. A dishonest belfry, or someone who controls a person's
  mailbox, can add a device and read **new** messages from then on, never earlier ones. Every
  device of that person shows the addition, with Remove.
- The belfry sees who writes to whom, when, and how much.
- Whoever holds an unlocked device reads what that device can read.
- A device that was removed keeps what it already fetched.

## 2. Keys and names

| Key | Made by | Held by | Used for |
|---|---|---|---|
| Device key | the device, at sign-in | that device only | signing what the device sends; receiving its copies of conversation keys |
| Conversation key (pair) | the sending device | every device of the conversation | encrypting messages; the public half names the key |
| Identity key | the belfry | the belfry's signer | public posts, relay sign-in, profile. No part in encryption |

`K` = a conversation key's public half (32-byte x-only hex). `k` = its secret.

An account with **no device list** (an agent with its own key, an own-key member, the belfry's
own key and its groups key) is its own single device: its device key is its account key.

## 3. Device lists, kind 8570

Written by the belfry, signed with the **belfry key** (the key in `belfry.json`). A regular
event, never replaced, so the history stays:

```
kind 8570, pubkey = belfry
tags: ["p", <account>], ["n", "<counter, from 1>"], ["prev", <id of list n-1>] (absent for n = 1),
      ["device", <device pubkey>], … (0 to 20, sorted),
      ["device", <agent pubkey>, "reader"] (see §11)
content: ""
```

- Devices = the account's live **chat** sessions (the `chat` flag of `/api/me/sessions`) used in
  the last 30 days, newest 20.
- No names, no device types, no times: Xavier's rule, "the details of the devices of
  correspondents should never be shared". An app shows other people as a person and a count.
  A person's own device names come from their own `/api/me/sessions`.
- The belfry writes a new list at once when a session is bound or revoked, and checks every
  10 minutes for sessions that passed 30 days.
- The **current list** of an account = the valid 8570 with the highest `n` (`prev` must chain;
  a gap means "not loaded yet", ask again). Its event id is the **list id**.
- Reading: `{kinds:[8570], authors:[belfry], "#p":[accounts…]}` on the community relay, kept on
  the live subscription.
- Built (server, `devices.py`): a list is written after every session bound to a client key or
  ended, in whatever part of the belfry it happens (the signer client is wrapped), and by a check
  of every account every 10 minutes (sessions bound by their first `connect`, expiries, anything
  missed); at most one list every 2 seconds. The counter and the latest list per account are kept
  in `devices.sqlite` next to the register; before writing, the relay's latest list wins when it
  is further on. Switch: `DEVICE_LISTS` (§12).
- A device that does not find itself on its own account's current list within 60 s of signing
  in tells the person ("This device isn't set up for encrypted messages yet · Try again") and
  holds what it would send.

## 4. Conversations

- **People conversation**: the set of accounts in the rumor's `p` tags plus its author (NIP-17).
  One person only = Notes to myself.
- **Private group**: a NIP-29 group whose metadata is private (`docs/one-shape.md`, Private).
  Its people = the group's member list (kind 39002) on the groups relay.
- The **device set** of a conversation = the union of the current lists of its people.
- A people conversation is encrypted with this version when its device set has 1 to 100 devices
  and the belfry's switch (§12) is on. Over 100 it is not encrypted: it simply has no lock.
- A private group is always encrypted, up to 1000 devices. Its envelope may be several events
  with the same `d` (at most 200 entries each); a device looks for its entry in all of them.
  Over 1000 the group refuses to send and tells its stewards (it never falls back).

## 5. Conversation key, kind 8571

The sending device picks the key to use, at the moment of sending, after refreshing the lists
on the current connection (wait at most 3 s, then use what is known):

- the newest key it holds for this conversation that was **made for exactly the current list
  ids** of all its people and is less than 30 days old;
- none: it makes a new pair and publishes its envelopes **before** the first message that uses it.

**Key rumor** (the thing each device receives; never published in clear):

```json
{"kind": 8571, "pubkey": <maker device>, "created_at": <now>, "content": <k hex>,
 "tags": [["K", <K>], ["p", <account>]…, ["list", <account>, <list id>]…, ["h", <group id>]?]}
```

A receiving device accepts a key rumor only when: `K` is the public half of `content`; the
maker device is on a list of one of the `p` accounts (or is such an account's key, §2); the
receiver's account is among the `p` tags; and, for a group, the `h` tag is the group the
envelope was found in. A key is only ever used for the conversation its `p` tags (or `h`) say.

**Envelopes.** One envelope per person (people conversations) or one for the whole group:

People conversation, on the community relay:
```
kind 8571, pubkey = a one-time key E, created_at backdated as NIP-59
tags: ["p", <account>], ["d", <alias>]
content: JSON {<device pubkey>: nip44(conv(E secret, device pub), <seal JSON>), …}
```
where the seal is a kind 13 signed by the **maker device**, content =
nip44(conv(maker secret, device pub), key rumor JSON), and
`alias = sha256("belfry:key:" + K + ":" + account)` in hex. The alias keeps other members of the
relay from seeing which people share a key.

Private group, on the groups relay:
```
kind 8571, pubkey = the maker's account (signed through the signer, like every group post)
tags: ["h", <group id>], ["d", <K>]
content: JSON {<device pubkey>: nip44(conv(maker device secret, device pub), key rumor JSON), …},
         plus "maker": <maker device pubkey>
```

Fetching: a device that meets a message naming a key it does not hold asks
`{kinds:[8571], "#p":[me], "#d":[alias]}` (people) or `{kinds:[8571], "#h":[group], "#d":[K]}`
(group), takes its own entry, and keeps `k` in the device's store. No entry for this device =
"can't be read on this device" (§9). Nothing is pushed to devices.

Conflicts need no rule: two devices that each make a key both publish; every message names
its key; every device can fetch both.

## 6. Messages, kind 8572

The rumor is unchanged: the real event, unsigned, `pubkey` = the account. In a people
conversation kinds 14, 15, 7, 5, 1301 as today. In a private group **every** event of the
group: messages, replies, reactions, edits, deletions, files, to-dos, events, polls, pins,
labels (9, 1111, 7, 1010, 5, 2100–2103, 31923, 1068, 1018, 1985, …).

Seal, kind 13: signed by the sending **device**, content =
nip44(conv(device secret, K), rumor JSON). Every holder of `k` opens it with conv(k, device pub).

People conversation, one copy per person (the sender included), community relay:
```
kind 8572, pubkey = a one-time key, created_at backdated as NIP-59
tags: ["p", <account>], ["d", <alias of K for that account>], ["silent","1"] on the sender's own copy
content: nip44(conv(one-time secret, K), seal JSON)
```

Private group, one event, groups relay:
```
kind 8572, pubkey = the sender's account (through the signer), created_at = now
tags: ["h", <group id>], ["d", <K>]
content: the seal JSON
```

Opening: find `k` by the `d` tag (fetch the envelope if needed); open; verify the seal's
signature; compute the rumor's id. Accept only when
- the seal's signer is a device of `rumor.pubkey`: on its current list, or on an earlier list
  when the rumor's `created_at` is not later than 10 minutes after the list that dropped it
  (or it is that account's own key, §2);
- the rumor's people equal the key's `p` tags (people) / its `h` equals the key's (group), and
  in a group the outer event's `pubkey` equals `rumor.pubkey`.

The author's lists may not be loaded yet: keep the event and retry; never mark it seen on that
failure. Bad signature or a copy that does not open with the right `k`: drop.

In a private group the relay no longer orders or replaces anything by kind: the app applies
"newest per author and `d`", edits and deletions itself, on the opened rumors.

Reading: `{kinds:[8572, 8571], "#p":[me]}` and, per private group, `{kinds:[8572, 8571], "#h":[group]}`.

No typing indicators in encrypted conversations in this version.

## 7. A new device

1. It signs in (6-digit code). The belfry writes the account's new list. From the next message
   on, every sender's key includes it. It reads and writes at once.
2. Earlier messages name keys it has no copy of. It shows "Earlier messages are on your other
   devices · Ask for them".
3. **Ask**, kind 8574, signed by the device: `["p", account]`, content "". One per device,
   again after 24 h.
4. Every other device of the account that sees a new device on the list shows one card:
   "‹its own name for the device› was added · ‹time›", Share earlier messages (the button) and
   Remove this device. There is no "Not now": the card stays until one device answers.
5. **Share**: that device collects every conversation key it can open (its own store, plus the
   envelopes on the relays) and every version-1 letterbox secret it holds, and sends them in
   **key packs**, kind 8573: one-time author, `["p", account]`, `["d", <new device pubkey>]`,
   content = nip44(one-time → new device) of a seal signed by the sharing device, holding a
   JSON list of key rumors (at most 40 000 bytes a pack). The new device accepts a pack only
   from a device on its own account's list.
6. **Remove**: revokes that session (`POST /me/sessions/revoke`); the belfry writes a new list;
   the removed device is signed out and deletes its messages and keys the next time it is online.
7. **Answer**, kind 8575, signed by the answering device: `["p", account]`, `["e", <ask id>]`
   or `["device", <pubkey>]`, `["answer", "shared" | "removed"]`. The card disappears on the
   other devices. Answered once for all devices and all conversations.

Other people are not asked and cannot share: a new member of a private group, or a person who
lost every device, reads from the moment they are on the lists.

## 8. Private groups

- A person who joins reads from the first key made after they joined; nothing earlier.
- A person who leaves or is removed gets no later key.
- Membership is read from the groups relay (39002). The sender refreshes it with the lists
  before sending.
- E-mail into the group, buttons and agents: §10, §11.
- To verify before building: the groups relay accepts kinds 8571 and 8572 with an `h` tag from
  members, serves them by `#h` and `#d`, and hides a private group's events from non-members.

## 9. States a device shows

| State | When | What the person can do |
|---|---|---|
| Opening | the key is being fetched, or offline | wait |
| Can't be read on this device | the key's envelope has no copy for this device | Ask for earlier messages (§7) |
| Held | lists or membership can't be read, or this device is not on its list | Try again; the text is kept |
| Not encrypted | over 100 devices (people conversation), or messages from before | nothing is said: the conversation has no lock |

## 10. The belfry as a sender

- **E-mail in.** The mail server is a sender whose device is the belfry key (§2): it makes a
  conversation key for the conversation (or reuses the public half of the one it made while
  the lists are unchanged; it never keeps a secret), publishes the envelopes and the message
  (rumor kind 1301, or the group's inbox card), and forgets the clear text. The line in the
  app: the mail came through the belfry in clear and is stored encrypted.
- **E-mail out, reports (7679):** the sending device addresses the belfry key as one more
  person of that one message, as version 1 did with the bridge.
- **Buttons the belfry answers** (`belfry:` actions) in a private group: the press is sent a
  second time as a people-conversation message to the groups key, carrying the button and the
  message it sits on; the answer comes back into the group from the groups key as a sender.
  Buttons answered by a person's own app (RSVP, to-do) need nothing: they are group events.
- **Push:** the belfry announces a kind 8572 as it does a 1059 today (per `p`, or per member
  of the group by its notification level), without content. `["silent","1"]` as today.

## 11. A member's own agent

An agent has its own key and is its own device (§2) in conversations it is a person of.

A member may also let **their own** agent read their encrypted conversations: a setting on the
agent's profile, off by default, "Give access to my private messages and private groups".
`POST /api/v1/agents {pubkey, reads: true|false}` by the owner. On: the belfry lists the
agent's key on the owner's device list as `["device", <agent>, "reader"]`. A reader receives
key copies like a device; a seal signed by a reader is valid only for the agent's own account,
never as the owner. The owner sees it among their own devices (removing it there turns the
access off); others see the count. It reads from the moment access is given: no Share card,
no earlier messages (Xavier, 2026-11-23: "from that moment on").

## 11b. What other people see of a device change

Nothing in the conversation. "Who can read this" shows each other person with a device count
only, never expandable. (A line "Lina added a device" in the conversation is drawn as an
alternative and not chosen.) The lock shows only at the top of a conversation, never in the
Messages list. An e-mail shows "By e-mail"; the header lock explains.

## 12. Moving from version 1

- Nobody signs in again; nothing is re-encrypted.
- `belfry.json` → `"encrypted": {"format": 1 | 3}`. Apps that know this document read both
  formats always, and write the one the belfry names. **The switch** is the setting
  `ENCRYPTED_FORMAT` (`1`, the default, or `3`; any other value stops the service at start).
  The device lists (§3) have their own switch, `DEVICE_LISTS` (off by default): they are
  turned on first, and format 3 only once every account has a list and the relay rule of §18.1
  is live. Each such device says so once with a
  kind 30078 `d = belfry:device:reads` signed by the **device** key, tag `["v","3"]`; the
  console shows how many live devices have not said it, and whose.
- Switched per belfry, belfry.sh first. One way: after the switch, devices added since are
  outside every version-1 chain.
- Version-1 messages stay readable where the letterbox secret is held; §7's Share hands the
  secrets on.
- After the switch no app writes rosters, anchors, device requests or hand-overs. The §5.7
  self-join code is removed.

## 13. Limits and numbers

| | |
|---|---|
| Devices per person | 20 |
| Devices per encrypted conversation | 100 |
| Key age | 30 days |
| Sealed rumor | 40 000 bytes (larger content goes to Blossom encrypted, as today) |
| Community relay | 600 events a minute per address (was 60) |
| Grace for a dropped device's last messages | 10 minutes |

## 14. Kinds

| Kind | What | Signed by |
|---|---|---|
| 8570 | device list | the belfry |
| 8571 | conversation key envelope | one-time key (people) / the maker's account (group) |
| 8572 | encrypted message | one-time key (people) / the sender's account (group) |
| 8573 | key pack for a new device | one-time key |
| 8574 | ask for earlier messages | the device |
| 8575 | answer to an ask | the device |

## 15. Decided, and why

- **No "strict rule" at the belfry.** It was proposed that the belfry refuse a message made
  with a key from before a device was removed. An honest belfry already keeps a removed device
  from reading anything (its session is gone); a dishonest one would not apply the rule. So
  the rule adds nothing and is left out. What narrows the gap instead: a device encrypts at
  the moment of sending, after refreshing the lists.
- **Messages are copied per person, not per device**, each under a different one-time key and
  key alias, so members of the relay who are not in a conversation cannot tell who is in it.
- **Lists are regular events with a counter**, so a reader can check that a sender's device was
  listed when it wrote.

## 16. Readings fixed after the web's first read (2026-11-23)

Where this section and an earlier one differ, this one counts.

1. **Group envelope, small entries.** In a private group the envelope is
   `tags: ["h", group], ["d", K], ["maker", <maker device>], ["lists", <sha256 hex of the sorted
   lines "account:list id" of every member>]`, `content` = JSON `{<device pubkey>: nip44(conv(maker
   device secret, device pub), JSON {"k": <k hex>, "K": <K>, "h": <group>})}`. No per-person list
   tags, no key rumor copied per entry. An envelope event holds at most 60 000 bytes of content;
   more entries go in further events with the same `d`. The `lists` hash is for the maker's own
   "made for the current lists" check and for debugging; receivers do not verify it.
2. **The maker of a key** must be a device of one of the conversation's people: for a people
   conversation, on the list named for its account in the key rumor; for a group, on its
   account's list that was current at the envelope's `created_at`, with §6's 10-minute grace. A key
   stays valid for every message made with it, also after its maker left the lists; each message
   is still checked by §6 on its own signer.
3. **Key packs.** A version-1 letterbox secret travels as a rumor `{kind 8571, content: <secret
   hex>, tags: [["v1", <letterbox pubkey>], ["p", account]]}`. Packs hold the person's group keys
   too (their own new device has no entry in older group envelopes; "a joiner sees nothing
   earlier" is about other people). A pack is accepted when its seal is signed by a device on
   the receiver's own account's current list; no open ask is needed (Share can be tapped before
   the new device asks). A pack never replaces a key the device already holds.
4. **Lists are kept for ever** on the community relay (regular kind, never replaced or pruned by
   the belfry). A reader that must judge an old key or seal loads the account's whole chain.
5. **Device = session.** A device's public key is the session's `client_pubkey` in
   `/api/me/sessions`.
6. **A removed device learns it** from `GET /api/v1/sessions/removed`, NIP-98 signed by the
   **device** key itself: `{removed: false}` or `{removed: true, at, by_name?}` (`by_name` = the
   removing device's name when the belfry knows it; `POST /me/sessions/revoke` takes an optional
   `by` = the caller's device pubkey). Kept 30 days. As built: `at` in Unix seconds; `by_name`
   = the name the signer keeps for the caller's own session (as `/api/me/sessions` gives it,
   with the device); a device that left its list without a revoke the belfry saw (an expiry)
   is answered `removed: true` with no `by_name`; a key the belfry never saw as a session = 404.
   Rate: 30 a minute per address.
7. **Writing.** Device keys and one-time keys write 8571–8575 and the device-signed 30078 on an
   authenticated connection, as one-time 1059s do today.
8. **Events, check-in, tickets in a private group** exist only inside the group, encrypted: no
   public page, no calendar outside its members' apps, no tickets, no check-in.
9. **In clear in a private group**, because the relay or the belfry must read them: 9000, 9001,
   9002, 9005, 9007, 9008, 9021, 9022, the relay's 39000–39003, labels signed by the groups key
   (roles, guest), vouch labels (1985 `belfry/vouch`), the 30078 settings the belfry reads
   (`belfry:inbox:<channel>`), the `seen:<channel>` marker. Everything else is encrypted. A
   private group has no public web page (`belfry:web` is ignored for it).
10. **The belfry writes into a private group** only two things: incoming e-mail and answers to
   buttons (§10). Announcements by e-mail and mention digests do not exist for a private group:
   their rows are hidden there. A member's agent is in a private group only as a person of it,
   or as a reader (§11).
11. **A returning device** (idle more than 30 days, still signed in) is listed again within
   10 seconds of its first signer request.
12. **Asking again:** an 8574 is sent again after 24 hours, and when another device of the
    account appears on the list. An 8575 with `["device", pubkey]` and no `e` answers the card
    for a device that never asked.
13. **Catch-up** for 8571–8573 goes back two days before the last seen event (they are backdated).

## 17. Changes after Android's adversarial review (2026-11-23)

Review: `/home/xdamman/agents/belfry-android/handover-2026-11-22/ENCRYPTED-REVIEW.md`. Where this
section and an earlier one differ, this one counts.

1. **Never decide by absence.** Every account has an 8570, also an agent, an own-key member:
   the belfry lists the account's own key as `["device", <account key>, "self"]`. Only the
   belfry key and the groups key (both named in `belfry.json`) are their own device without a
   list. No list known for a person = **Held**, whatever the wait; never the account key.
2. **Once encrypted, never clear.** A people conversation this device has ever seen encrypted
   never sends unencrypted: over 100 devices it is Held, with words. Only a conversation that
   starts over 100 devices has no lock. After a conversation's first key, an unencrypted event
   in it is shown marked "Not encrypted" (people) or ignored (private group, outside §16.9's
   clear list), and never acts on anything (no reaction, edit, deletion, tick or vote counts).
3. **Idle is not removal.** A device leaves its list only when its session is revoked, signed
   out or expires. §3's "used in the last 30 days" and §16.11 are withdrawn.
4. **Twenty devices.** A twenty-first device is not listed: it shows "You have 20 devices
   signed in · Remove one to read encrypted messages here" and holds. Nothing is evicted.
5. **A person with no device** (signed out everywhere, or never signed in to the chat): a
   message for them alone is Held on the sender's device ("‹name› isn't signed in on any
   device · It will be sent when they are") and goes out when the sender's app next sees a
   device on their list. In a conversation with others it is sent for the others, and the
   sender sees "‹name› won't get this". (Wording and boards: for Xavier.)
6. **Only older devices answer.** An answer (8575), a key pack (8573) and the card's Share count
   only from a device that was on the account's list **before** the device they are about.
   A reader never sends packs or answers.
7. **Every device checks its own account's lists.** It compares each list with the one before
   and shows the card for every device added, also one added and removed while it was offline
   ("‹device› was added and removed while you were away"). Two different lists with the same
   `n`, or an envelope for my account holding an entry for a key that was never on my lists, show
   a warning that does not go away by itself ("Your belfry gave out two different lists of your
   devices"). The current list is the highest `n` seen, never an earlier one; a missing older
   list does not block (`prev` serves to detect forks, not to gate reading).
8. **A device sends only with keys made by its own account's devices.** It opens messages under
   any valid key, but never adopts another person's key for sending. So a conversation has one
   current key per person who writes in it. A member cannot make others use a key that leaves a
   device out, and nobody ever sends with a key the belfry made for e-mail. A new key is made
   when a device list changed, when the people or the group's members changed, or after 30 days.
   A key dated more than 15 minutes in the future is refused.
9. **A reader is announced like a device.** When a `reader` entry appears on my list every device
   of mine shows "‹agent› can now read your private messages · Remove" (no Share). A seal, pack or
   answer signed by a reader is refused for the owner's account. Others still see only a count.
10. **Private-group membership is the belfry's word.** Said in §1's limits: the belfry (through
    the groups relay) can add a member, who then reads from the next key. The app draws
    "‹name› can read from here" from the difference between one key's people and the next, not
    from the relay's join events alone.
11. **No backdating.** 8571, 8572 and 8573 carry `created_at` = now (the belfry sees arrival times
    anyway). §16.13 is withdrawn: ordinary catch-up applies. A seal from a device no longer listed
    is accepted only when the rumor's date is before that device left plus 10 minutes.
12. **A key is declared once.** The first valid declaration of a `K` a device sees fixes its people
    (or group); a later one with other tags is ignored. Envelopes that do not open under an alias
    are skipped, never blocking the search for one that does.
13. **One way, kept by the device.** A device that has once seen `encrypted.format` 3 for a belfry
    never writes format 1 there again. 8574, 8575 and the `belfry:device:reads` event count only
    from keys on a list.
14. **"When it's for me" in a private group.** The outer 8572 may carry `["p", <account>]` for each
    person mentioned, so the belfry can push to them; nothing else of the content is exposed.
15. **Said plainly in §1.** On the web the belfry serves the code that holds the device key. The
    alias keeps other members from matching copies; it hides nothing from the belfry. In a
    private group the account's signature on the outer event proves nothing: what authenticates
    a key is the entry opened with the maker device's key, and a message, its device seal.
16. **Not taken:** a separate key for signing lists (same box, same trust); "Remove" as the
    card's main button (Xavier approved Share as the button); showing correspondents "and her
    agent" (Xavier: others' device details are never shown).

## 18. Changes after Android's second read (2026-11-23)

Where this section and an earlier one differ, this one counts.

1. **A relay rule, required before any belfry is switched to format 3.** The community relay
   serves kinds 8571, 8572 and 8573 only to a connection authenticated as the account in the
   event's `p` tag, and refuses to store one whose `created_at` is more than 5 minutes from its
   own clock. (rnostr has no such rule today: any member's connection can read anything, which is
   also true of today's 1059 wraps. It is a small relay extension, built and shipped with the
   server part.) Until that rule is live nothing is switched. §15's sentence about members not
   being able to tell who is in a conversation holds only with this rule; the alias stays as a
   second layer.
2. **Dates.** A device that is no longer listed is judged by the **outer** event's `created_at`
   (which the relay vouches for by 18.1), not by the rumor's. A rumor may be older than its outer
   event, never newer by more than 5 minutes. In a private group the outer event is dated by the
   groups relay's own rule in the same way (to verify with the other group probes).
3. **A device's first screen after sign-in** lists every device already on the account's list,
   with the date each was added and Remove: "These devices can read your messages". It is how a
   person who signs in after an intruder did learns of the intruder (by 17.6 they are the later
   device and would get no card). When the lists needed to decide "listed before" cannot be
   loaded, an answer or a pack is refused, not accepted.
4. **A group key's entries are the authenticated list of who can read.** Each entry's plaintext
   is `{"k", "K", "h", "set": <sha256 hex of the sorted device keys that have an entry>}`. A
   receiver gathers every envelope event of that `d`; when the keys it sees do not hash to
   `set` it keeps asking and shows "Can't check who can read this" in Who can read this.
   "‹name› can read from here" is drawn from the entry keys, mapped to people through the lists.
5. **`self` entries.** A `self` entry is valid only on a list with no other device entry (readers
   allowed). A list holding both is refused by everyone (Held), and the owner's devices show
   the warning that does not go away. A member whose key is in the belfry's signer never has
   a `self` entry; their devices warn if one appears.
6. **Whether a conversation is encrypted** is decided from the conversation, not from the
   device's memory: a key envelope for my account (even without an entry for this device) or an
   8572 under its alias means encrypted. Limit, said plainly: an unencrypted event dated before
   the first key looks like old history to someone who joins later.
7. **Adopting keys, by transport.** People conversation: 17.8 as written (own account's keys
   only). Private group: a device may send with the newest valid key made by another member's
   device when its authenticated entry set (18.4) is exactly the device set the sender computes
   from the current lists and members, the key is under 30 days old and not future-dated;
   otherwise it makes its own. Never a key made by the belfry key or the groups key. (So a list
   change in a large group costs one envelope, not one per writer.)
8. **Mentions** are taken from the opened rumor only; the outer `p` tags of 17.14 serve the
   belfry's push and nothing else.

## 19. Vectors

`vectors/encrypted.json` in the web chat's repository (written by belfry-webapp, generated by
`tools/vectors/gen-encrypted.mjs` with fixed keys; sha256 3d05a274…051e at its first version) is
the shared test file for every platform and for the server's list writer.

A **group key inside a key pack** (§16.3) is the rumor `{kind 8571, pubkey: <sharing device>,
content: <k hex>, tags: [["K", K], ["h", <group>]]}`. A key received this way opens messages; it
is never adopted for sending (it carries no authenticated entry set).

## 20. After Android's last read of the vectors (2026-11-23)

- **Three answers, never two.** "Which devices does this account have" answers one of: a list;
  *service key* (exactly the belfry key or the groups key of `belfry.json`: its own device);
  *no list* (Held). Code must not use one value for the last two: a seal signed with a member's
  identity key is never accepted because that member's list is missing. A vector pins it.
- **Limit, said plainly:** a group key's `set` proves which devices have an entry, not that each
  entry opens. A member who makes a key can still give one device a broken entry; that device
  shows "Can't be read on this device" until the next key. It cannot be hidden from that device,
  and it gains the member nothing but a nuisance.
- An account with a `self` entry can make people keys like any device. An answer or pack from a
  device that has since been removed does not count. "Listed before" compares the first list
  each device ever appeared on.
- A seal signed with an account's identity key while that account's list is missing is kept and
  judged again when the list arrives ("wait"); it is never accepted on the identity key.
- A `device` tag with a third element the reader does not know (`reader` and `self` are the
  known ones) is skipped as an entry; the rest of the list counts. (An older app then neither
  encrypts for that entry nor accepts its seals.)
- Vectors: sha256 c15479be…1f71 (135 checks) after this section.

## 21. Dates around a listing (2026-11-23, Android)

- A seal's signer is judged against **the list current at the outer event's `created_at`**
  (this replaces §6's "on its current list, or on an earlier list when …"): a device on the
  current list is refused for a message dated in a gap when it was off.
- **Grace at the start of a listing**: a device also counts when the first list that has it is
  dated no later than the outer date + 5 minutes (clocks may be off by the relay's 5 minutes).
  The same for a group key's maker. Vector: listed at 2000, outer 1800 → ok; outer 1600 → no.
