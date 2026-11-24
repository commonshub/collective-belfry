# Printed cards

Prefix `CARD`. Pre-printed cards with a QR code that start a group or a topic: the first person
to scan a card starts it and looks after it, everyone after joins. Asked by the owner on
2026-11-21 (for belfry.brussels). Contract: docs/cards.md. Canvas: Cards (boards `cards/*`,
`onb-*`), https://claude.ai/artifact/RxoRzu7fk2SpbTGp2AWkBR.

## The topic's hashtag

- **CARD-1** [apps] On the create-topic flow, the hashtag field cleans as the person types:
  white space becomes a dash, and punctuation and other special characters can't be entered.
  "On create topic flow for the hashtag input text it should automatically turn white spaces
  into dashes and prevent special chars like punctuation and co." (owner, 2026-11-24)
  The result must satisfy the server's rule: 2–30 of `a–z`, `0–9`, `-`, lower case, no dash at
  either end (capitals are lowered; several dashes in a row collapse to one; to confirm: the
  server does not clean, it only refuses). → docs/cards.md §2.2, §2.4
- **CARD-2** [apps] The field checks the hashtag live (free / taken / invalid, with the reason)
  before the form can be sent; taken names include channels, usernames, mail aliases and the
  reserved names (`general`, `charter`, `website`, `hello`, `pod-`…). → GET /cards/hashtag/<name>
- **CARD-3** [apps] The start-topic form order: Topic → suggested hashtag → Description.
  (owner, Cards canvas v41; to confirm: "topic start form order" was still listed as pending
  with the owner on 2026-11-24.) A topic without a title is named by its hashtag.
- **CARD-4** [apps] No Summary row on the topic start form (owner, Cards v80, 2026-11-24). The
  server still accepts `summary` and defaults the starter's follow to weekly. (to confirm which
  wins: the form no longer asks, so the starter follows weekly.)

## The card

- **CARD-10** [all] A card's QR holds `https://chat.<domain>/g/<id>`; the id is 6 characters
  without look-alikes (no 0 O 1 I L) and is printed nowhere as text. (owner, Cards v29,
  2026-11-23) Card lookups are rate-limited because short ids can be guessed.
- **CARD-11** [all] Two kinds: a group card (upright) "Scan to join this group", a topic card
  (on its side) "Scan to contribute to this topic · No account required". (owner, 2026-11-23)
- **CARD-12** [all] An unused card leaves nothing on the relay: no group exists until someone
  claims it.
- **CARD-13** [server] Printed cards are managed on belfry.<domain> (the console, "Printed
  cards"), never in the chat. "The cards admin should be on belfry.domain, not on chat."
  (owner, 2026-11-23)
- **CARD-14** [server] "Generate PDF" makes a real PDF on the server (A4; 1, 2 or 4 cards a page;
  cut lines; nothing within 8 mm of the edge; QR with its quiet zone), laid out as the boards
  `cards/print`, `cards/print-sheet`, `cards/print-sheet-channels`. The browser's own printing is
  not relied on. (owner, 2026-11-23)

## Claiming

- **CARD-20** [all] The first scan of an unclaimed card claims it: title (or hashtag) and
  description, and the claimer becomes the group's steward. A signed-in person claims at once.
- **CARD-21** [all] A visitor claims with name + e-mail and proves the address with a 6-digit
  code, or with the Confirm button in the same mail; the page that waits moves on by itself once
  either is done. The session always belongs to the browser that scanned, never to the one
  where the mail's button was clicked. "If possible, instead of sending a confirm link, it should
  send a 6 digit code. But if a confirm link is necessary it should indeed sign you in within
  10mn." (owner, 2026-11-23; the button was added on 2026-11-24, boards onb-code, onb-mail)
- **CARD-22** [all] Nothing is created before the address is proven; the first proven claim wins
  and the loser's group is never made.
- **CARD-23** [all] Starting a topic, like a group, asks for an e-mail address and a code "for
  now" (owner, 2026-11-23). Starting without an e-mail is wanted later.
- **CARD-24** [all] A visitor who claims becomes a guest steward of that group only: no belfry
  rights, no guest links of their own (the card is their link). Their pass of that group lasts
  while the card is theirs. (owner approved the guest steward 2026-11-23; to confirm: he said
  the 30-day rule need not change, the server keeps the pass open while the card is claimed.)
- **CARD-25** [all] Who created it: the belfry writes the group for the person and every event
  says whose it is (`by`); apps show the group and its first posts as the person's, never the
  belfry's. (owner, 2026-11-23)
- **CARD-26** [all] A new topic posts a card in Townsquare (`# New topic: #hashtag · about`); a
  new group posts "New group: ‹title›". The first line in the group: "‹name› started this group
  from a printed card". The hashtag must be visible on the topic ("hashtag nowhere to be seen",
  owner, 2026-11-23).

## After the claim

- **CARD-30** [all] Every later scan previews the group and joins it through the card's guest
  link, which has no end date and no limit on people (at most 100 new guest accounts per card a
  day, then "This invite is used").
- **CARD-31** [all] Guests who reach a topic by its card may post there (text and pictures)
  without giving an e-mail address. (owner, 2026-11-23) Not in groups.
- **CARD-32** [all] On a channel or topic, the people in it are **followers**, not members; never
  "member" or "Join" about following a topic. Belfry stewards are admins of every channel
  without following it. (owner, 2026-11-23)
- **CARD-33** [apps] The group's stewards see the card in its settings (QR, how many joined by
  scan, Retire). A retired card says so and joins nobody; an archived group's card says
  archived.
- **CARD-34** [all] Someone who starts or joins a topic by its card can follow it by e-mail
  (weekly by default, or monthly). → channels.md CH-50…
