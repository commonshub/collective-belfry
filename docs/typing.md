# Typing indicators

"Lina is typing…" under a conversation while someone writes in it: in direct messages, and in
every channel, group, event chat, proposal and thread. Asked by Xavier on 2026-11-21. Nothing on
the server: the apps send short-lived (ephemeral) Nostr events to the relays they already use.

## 1. The event

There is no NIP for typing indicators (checked 2026-11-21: nothing merged, no open PR). NIP-16 /
NIP-01 name typing indicators as a use of ephemeral events (kinds 20000–29999: relays forward them
to open subscriptions and don't keep them). Belfry uses **kind 20100**, unassigned in the NIPs'
list, with empty content.

| Tag | When | Meaning |
|---|---|---|
| `["h", "<group id>"]` | channels, groups, events, proposals | where (NIP-29) |
| `["e", "<root id>"]` | in a thread | the thread's root message; absent = the conversation itself |
| `["p", "<pubkey>"]` | direct messages, inside the seal only | every participant but the author (as a NIP-17 kind 14) |
| `["status", "stop"]` | optional | "I stopped" (the field was cleared without sending) |

### Channels, groups, event chats, proposals, threads

The event is signed by the person (through the signer, like a message) and sent to the groups
relay, with `h`, and with `e` inside a thread. Readers subscribe while the conversation is open:

    {"kinds": [20100], "#h": ["<group id>"], "since": <now - 5>}

The thread screen shows only events whose `e` is its root; the conversation shows only events
without `e`.

### Direct messages

Who talks to whom stays private, as for messages: the kind 20100 event is a rumor (unsigned),
tagged `p` for every other participant, sealed (kind 13, signed by the person, NIP-44 to the
recipient) and wrapped in a **kind 21059 ephemeral gift wrap** (NIP-59 §4) to each other
participant on the community relay. No copy to oneself. Unlike kind 1059, the wrap's `created_at`
is the real time (not randomised), since readers ask with `since` and drop old ones; the timing
of a live signal can't be hidden anyway.

    {"kinds": [21059], "#p": ["<my pubkey>"], "since": <now - 5>}

The conversation is the set of the rumor's `p` tags plus its author, as for kind 14. A sealed
conversation (docs/sealed.md, not shipped) would seal with the device key and wrap to the
letterboxes, as its messages do; until then, sealed conversations send no typing events.

## 2. When to send

- On a keystroke that leaves the field non-empty, if nothing was sent for this place in the last
  **4 seconds**. A person typing for a minute sends about 15 events; one who stops sends nothing
  more.
- Never for a draft restored on open, an edit of an older message, or a field holding only
  spaces.
- `["status", "stop"]` when the field is emptied without sending, only if a typing event went out
  in the last 6 seconds. Sending the message needs no stop: the message clears it.
- Not in archived channels, not from reading guests (no name yet), not when the person turned it
  off (§4).
- Best effort: never through the outbox, never retried, dropped when the socket is down or the
  signer doesn't answer within 3 s. A typing event never delays a message.

## 3. How to show it

- A person shows as typing in a place from a typing event until the first of: **6 seconds** after
  their latest one, their `stop`, or a message (kind 9, 1111, 14 or 15) from them landing there.
- Ignore an event whose `created_at` is more than 10 s in the past or 10 s in the future. Relays may
  replay ephemeral events: rnostr (the community relay) keeps them up to 5 minutes and serves them
  to new subscriptions (checked on belfry.sh 2026-11-21); zooid (the groups relay) neither stores
  them nor replays them.
- Ignore one's own events (from any device of the same account), and events from people who are
  muted, removed (9001) or not in the group.
- Text, under the last message, above the composer, in the secondary text colour, with three dots
  that animate (still when the system asks for reduced motion):
  - one: "Lina is typing…"
  - two: "Lina and Tom are typing…"
  - three or more: "Several people are typing…"
  Names as everywhere else (names, never ids; "Somebody" when there's no profile).
- The conversations list and the channel list may show "typing…" in place of the last message's
  preview for a DM; not for channels in v1.
- Never a notification, a sound or an unread count.

## 4. The setting

Me → Privacy (or the app's equivalent): **"Show others when I'm typing"**, on by default. It
belongs to the device (like the theme), not the account: off means this device sends nothing;
the person still sees others typing.

## 5. Costs and limits

Each typing event is one signer call in a channel (sign), and two per other participant in a DM
(sign the seal, encrypt it). With one 4 s gap that is at most one signer call every 4 s per person
typing in a channel. The relays' rate limits (rnostr `[rate_limiter]`, zooid) apply as to messages;
if a relay refuses one (rate limited), stop sending typing events to it for 30 s.

## 6. Relay checks (belfry.sh, 2026-11-21)

| Relay | Event | Accepted | Forwarded live | Kept |
|---|---|---|---|---|
| groups (zooid) | 20100 with `h` | yes ("handled internally") | yes | no |
| community (rnostr) | 21059 with `p` | yes | yes | up to 5 min, then deleted |
| community (rnostr) | 20100 with `p` | yes | yes | up to 5 min (not used: DMs go wrapped) |

Not checked: a member's (not the groups key's) 20100 in a closed group, a guest's.
