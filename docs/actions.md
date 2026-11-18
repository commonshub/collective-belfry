# Buttons on messages

A message can carry buttons. Someone taps one, their own key signs "I pressed this", and whoever
handles the button answers with what happened: done, or an error a person can read. The first use
is approving a new version of the website from the message that proposes it.

There is no accepted NIP for this (2026-11). The one draft, NIP-C1 (nostr-protocol/nips PR 2295),
replaces the message text with JSON, so a client that doesn't know it shows raw JSON. Here the
text stays an ordinary message and the buttons are tags, so every older client still shows
something sensible. The button fields (`id`, `label`, `style`) are NIP-C1's. Presses and answers
are NIP-32 labels (kind 1985), which the groups relay and all clients already carry.

## 1. The message

Any kind 9 or kind 1111 event in a group may carry up to **4** button tags:

```
["button", "<id>", "<label>", "<style>", "<action>"]
```

| Field | Rule |
|---|---|
| `id` | `[a-z0-9-]{1,32}`, unique in the message |
| `label` | 1–40 characters after trimming; shown as is, never as Markdown |
| `style` | `""` (plain), `"primary"` or `"danger"`; anything else reads as plain; may be left out |
| `action` | optional. Absent or empty: the **message's author** handles the press. `belfry:<name>`: the **belfry** handles it (section 4) |

A tag that breaks a rule is ignored; a fifth button and a repeated `id` are ignored. The content
must make sense without the buttons: say in words what can be done and where (a link), because
older clients show only the text.

Buttons show under the message text, in tag order. An edit (kind 1010) never changes them.

## 2. The press

Tapping a button publishes, signed by the person who tapped, on the groups relay:

```
kind 1985
tags  ["L", "belfry/action"]
      ["l", "<button id>", "belfry/action"]
      ["e", "<message id>"]
      ["p", "<message author>"]
      ["h", "<group id>"]
content ""
```

A press is sent at once, not through the offline queue: when it can't be sent the button says so
and unlocks. Someone who can only read (a reading guest) gets the Join sheet instead.

Before a `belfry:` action the client asks for confirmation with **its own fixed text for that
action** (section 4), never the button's label alone: the label is written by whoever posted the
message. A button of style `danger` handled by its author asks "<label>?" with Cancel.

## 3. The answer

The handler answers each press once, on the groups relay:

```
kind 1985
tags  ["L", "belfry/action-result"]
      ["l", "ok" | "error", "belfry/action-result"]
      ["e", "<message id>"]
      ["press", "<press event id>"]
      ["p", "<the person who pressed>"]
      ["h", "<group id>"]
      ["code", "<machine code>"]        only on an error
      ["closed", "1"]                   optional, only on ok
content  one short sentence for people, plain text
```

An answer counts only when it is signed by the button's handler:

- no `action`: the author of the message;
- `belfry:<name>`: the belfry's key or its groups key: `nostr.pubkey` and `groups_key.pubkey`
  in `/.well-known/belfry.json`. The belfry signs its answers with the groups key.

Answers from anyone else are ignored.

What the client shows:

- after the tap the button is locked with a spinner (all buttons of that message lock);
- `ok`: the content as a line under the buttons, for the person who pressed;
- `error`: the content as an error line under the buttons, for the person who pressed, and the
  buttons unlock. With no content, a fixed sentence for the code; an unknown code reads
  "That didn't work.";
- `ok` with `closed`: the buttons are finished **for everyone**: they are replaced by the
  content of the latest closing answer (for example "Published by Xavier"). This state comes from
  the relay, so it shows on every device and after a restart;
- no answer after 20 seconds: "No answer yet." and the buttons unlock; an answer that arrives
  later still shows.

Codes: `not_allowed`, `unknown_action`, `no_target` (the message doesn't say what to act on),
`gone` (the thing no longer exists), `expired`, `failed`.

## 4. Actions the belfry handles

The belfry keeps a subscription on presses and handles those whose button names a `belfry:`
action, whoever wrote the message. Anyone may put such a button on a message; the right to do the
thing is checked on **the person who pressed**. A press older than 10 minutes when the belfry
sees it gets `expired`; one it already answered is skipped. More than 20 presses an hour from one
key are answered `failed` ("Too many tries. Wait a little.").

A client that doesn't know the name of a `belfry:` action shows the button disabled with
"Update the app to use this."

| Action | Who | What | Confirmation the client shows |
|---|---|---|---|
| `belfry:website-publish` | an admin of the belfry | Publishes the site the message points to as the website. The site is the message's first `["a", "35128:<pubkey>:<name>"]` tag, else the first preview link in its text. The message's own `["path", "<prefix>"]` tags (a folder `/ios/screenshots/` or one file `/android/index.html`, at most 10) limit the publish to those paths: the rest of the live website is kept as it is (skills/belfry-homepage). Answers `ok` + `closed`, "Published by <name>." or, with paths, "Published by <name>: only /ios/screenshots/." The usual "Website updated" line follows in #website. | "Publish this version as the website? Everyone will see it at https://<domain>." · Publish / Cancel |
| `belfry:offer-decide` | an admin of the belfry | Answers an offer in kind (docs/sales.md): the button's id says how much (`offer-50`, `offer-75`, `offer-100`) or no (`offer-no`); the offer is the message's `["offer", id]` tag, and the message must be that offer's own card. Posts the public reply in the thread and mails the requester their personal code. Answers `ok` + `closed`, "Approved by <name>: 75% off." / "Declined by <name>." | "Answer this offer? The answer is public in the thread; the code goes privately to them." · Answer / Cancel |

Errors (website): `not_allowed` "Only admins of this belfry can publish the website.", `no_target` "This
message doesn't point to a version of the website.", `failed` "Couldn't publish: <reason in a few
words>." (also when its path tags aren't valid: nothing is published then) Errors (offers): `not_allowed` "Only admins of this belfry decide offers.", `no_target`
"This message is not an offer here.", `gone` "This offer has an answer already."

New actions are added to this table first.

## 5. For agents

To ask people something with buttons, post a normal message with `button` tags and no `action`,
keep a subscription `{"kinds":[1985],"#L":["belfry/action"],"#p":["<your pubkey>"]}` on the
groups relay, and answer each press as in section 3. Check yourself who pressed (`pubkey` of the
press): the relay only tells you they are in the group.

To let an admin publish a website version from the chat, post in #website:

```
kind 9
tags  ["h", "website"]
      ["a", "35128:<your pubkey hex>:website"]
      ["path", "/ios/screenshots/"]     (optional, one per path: only these change)
      ["button", "publish", "Approve and publish", "primary", "belfry:website-publish"]
content  which path it is for, what changed, the preview link, and that an admin can approve it
```

## 6. Test vectors

`docs/actions-vectors.json`: which buttons a list of tags yields, which answer wins, and what a
press looks like. The three apps and the server run the same file.
