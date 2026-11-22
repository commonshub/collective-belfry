# One shape and Navigation v2: what the apps and the server agree on

Design: the canvas "Belfry app", sections **Navigation v2** and **One shape** (Xavier's comments
of 2026-11-22). This page is the wire. Everything not listed here is drawn by the apps from
events that already exist.

## 1. Navigation

- **Bottom bar (phones)**: Today · Calendar · Messages · Groups · Town. No ＋ in the bar. Laptop
  and tablet keep the left column.
- **No "follow".** The Town list is: channels pinned for everyone, channels I pinned, then
  channels where **I was mentioned, posted or reacted in the last 30 days** (opening one does
  not count), ordered by the channel's newest message, thread reply or reaction. Computed on the device from the events it holds.
- **Pinned for everyone**: `"pinned": ["<group id>", …]` in the content of the existing kind
  30078 `d=belfry:channel-order` event (belfry admins write it; a writer keeps every other
  key). Unknown ids are skipped.
- **Pinned for me**: one kind 30078 `d=belfry:pins` per person on the community relay, content
  NIP-44 v2 encrypted to self: `{"v":1,"c":["<group id>", …]}`. Newest wins; sent through the
  outbox. Guests keep it on the device only. Same pattern as `belfry:read`.
- **Notify me of everything** (the bell, off by default) = push level `all` for that channel
  (`docs/push.md`); off = `default`.
- **Add a channel** (the ＋ on Channels): typing filters the channels the device knows; "Create"
  comes last. Any member may create (as today). There is no "All channels" screen.
- A channel not in my list opens as a preview; writing or reacting brings it into the list.

## 2. One header for everything

Two lines at most: the title, then one row of one-colour icons with counts (each left out at
zero): the lead item in the accent colour (open e-mails, an event's date, "From <sender>"),
people, pinned, to-dos, files, photos, comments. No avatars and no message count in the row.
When the row does not fit, items leave in this order: the owner, files, to-dos, photos, comments,
pinned; the lead item and people stay. Every item has a spoken label ("3 open to-dos").

Pulled down, the thing itself is on top (the e-mails, the to-do, the event, the channel's
description and pins) and the conversation about it below. A message thread hides nothing: its
first message stays on top. A thread's title is the beginning of its first message (thread
names: not in this version).

Events, to-dos, proposals and e-mail threads have three states: tiny (the header), mid (the
default while it needs me) and full.

## 3. Messages

- **Hold menu, everywhere**: reactions, Reply, Make a to-do, Copy text, Copy link, Pin.
- **Reply** starts or opens the message's thread: kind 1111 in a group (as today). In a direct
  message: a kind 14 with `["e", "<root rumor id>", "", "root"]`, wrapped like any other; apps
  show such messages under their root, older apps show them in line.
- **Pin** in a group: the existing `belfry/pin` label. Not offered in direct messages in this
  version, and neither is Make a to-do (both need sealed rules first).
- **Activity lines**: an action about an item (to-do made or done, RSVP, meeting, vote) shows as
  one line with a hook to open a small preview of the item with its quick action. Open or
  closed is remembered per item on the device only. A preview that needs me starts open.
- **To-do due date**: `["due", "<unix seconds>"]` on the kind 2100; changed by the author's
  kind 1010 edit, which keeps every other tag. Shown first in the pull-down, never in the header.

## 4. E-mail threads (shared inboxes)

- An inbox card (kind 9 by the groups key, `client belfry-mail`) now carries who the mail was
  for: `["to", address, name]` and `["cc", address, name]`, one tag a person, at most 20 in all,
  never Bcc. The sent card (`belfry-mail-out`) carries the same tags from the mail that left.
  Cards from before 2026-11-22 have neither: Reply all then answers the sender only.
- **Reply all is the only reply.** The kind 1301 has one `rcpt-to` per person: the sender
  (`mail-from` of the latest received e-mail of the thread) plus its `to` and `cc`, without any
  address at the belfry's own domain or one of its subdomains, lowercased, without duplicates. Headers: `To` = the sender, `Cc` =
  the others, `In-Reply-To` and `References` as before. The bridge takes at most 20 recipients.
- **Answered is automatic.** After the delivery report says delivered, the app that sent the
  reply labels the thread's first card `answered` (the existing `belfry/inbox` label). A thread
  whose newest received e-mail is newer than its newest `answered` or `closed` label is **open**
  again: computed by every reader, no event.
- **Internal draft**: a kind 1111 on the thread's first card with `["mail-draft", "1"]`, by a
  channel admin; shown to the team as "Internal draft" (older apps show a team comment). Its
  author edits it with kind 1010. Sending it = the kind 1301 with `["draft", "<draft id>"]`; the
  sent card carries the same tag and apps then hide the draft. The author may also delete it.
- Cc'd outsiders are addresses only: no account, no guest.
