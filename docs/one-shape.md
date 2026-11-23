# One shape and Navigation v2: what the apps and the server agree on

Design: the canvas "Belfry app", sections **Navigation v2** and **One shape** (Xavier's comments
of 2026-11-22). This page is the wire. Everything not listed here is drawn by the apps from
events that already exist.

## 1. Navigation

- **Bottom bar (phones)**: Today · Calendar · Messages · Groups · Town. No ＋ in the bar. Laptop
  and tablet keep the left column.
- **No "follow".** The Town list is: channels pinned for everyone, channels I pinned, then
  channels where **I was mentioned, posted or reacted in the last 30 days** (opening one does
  not count; creating one counts as posting in it, from its kind 9007), ordered by the channel's newest message, thread reply or reaction. Computed on the device from the events it holds.
- **Pinned for everyone**: `"pinned": ["<group id>", …]` in the content of the existing kind
  30078 `d=belfry:channel-order` event (belfry admins write it; a writer keeps every other
  key). Unknown ids are skipped. The main channel (`general`) is always first in everyone's
  list, whether listed or not, so a menu is never empty.
- **Pinned for me**: one kind 30078 `d=belfry:pins` per person on the community relay, content
  NIP-44 v2 encrypted to self: `{"v":1,"c":["<group id>", …]}`. Newest wins; sent through the
  outbox. Guests keep it on the device only. Same pattern as `belfry:read`.
- **Notify me of everything** (the bell, off by default) = push level `all` for that channel
  (`docs/push.md`); off = `default`. Every member has the channel's Settings row, with "Pin
  for me" and "Notify me of everything"; admins also see "Pin for everyone" and the rest.
- **Add a channel** (the ＋ on Channels): typing filters the channels the device knows; "Create"
  comes last. Any member may create (as today). There is no "All channels" screen.
- A channel not in my list opens as a preview; writing or reacting brings it into the list.

## 2. One header for everything

Two lines at most: the title, then one row of one-colour icons with counts (each left out at
zero): the lead item in the accent colour (open e-mails, an event's date, "From <sender>"),
people, pinned, to-dos, files, photos, comments. No avatars and no message count in the row.
When the row does not fit, items leave in this order: the owner, files, to-dos, photos, comments,
upcoming events, pinned; the lead item and people stay. Every item has a spoken label ("3 open to-dos").

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
  version, and neither is Make a to-do (both need sealed rules first). Copy link is not offered
  there either: a direct message has no address. The menu there is reactions, Reply, Copy text.
- **Activity lines**: an action about an item (to-do made or done, RSVP, meeting, vote) shows as
  one line with a hook to open a small preview of the item with its quick action. Open or
  closed is remembered per item on the device only. A preview that needs me starts open.
- **Hosts and invitations** (2026-11-22): Host, for display, is the event's or meeting's maker
  only; co-hosts by role read "Co-host"; belfry admins who are in the chat's 39001 only because
  they are belfry admins are not shown as hosts. Any event with a `["p", <me>, "", "invited"]`
  tag for me shows under Calendar · Invited until I answer, not only meetings (the wire, the
  chat line `["invited", "event"]` and what the belfry sends: docs/checkin.md "Inviting people
  to an event").
- **To-do description**: `["description", "<Markdown, at most 2000 characters>"]` on the kind
  2100, optional; the title stays where it is today. Changed by the author's kind 1010 edit,
  like the due date. A to-do without one shows the text of the message it was made from (its
  `q` tag) when that says more than the title.
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

## Joining the belfry: a line in the main channel (2026-11-22)

Xavier: "when someone joins the belfry there is a message in town square to make sure everyone
can see who entered the room." When a membership opens (a new member, an accepted application, a
member link, a guest who becomes a member, a member's own agent), the belfry publishes once per
person, as the groups key (`joined.py`):

```
kind 9000
["h", "general"], ["p", <pubkey>, "member"], ["joined", "belfry"], ["name", <name>]
["agent", "1"]            for an agent
```

Apps draw it as an activity line in the main channel: "‹name› joined ‹the belfry's name›" (an
agent: "‹name›, ‹owner›'s agent, joined"), the face and name opening the profile. The name comes
from the profile when there is one, else from the tag. A `9000` in `general` without the
`joined` tag draws no such line (older ones, and guests': theirs carry `guest` and `invited-by`
and keep their own line). A person removed since (`9001` later) who never wrote keeps no line.
Not for guests of one channel, reading guests, or a buyer whose address is not proven yet.
People who joined before this date have no line.

## Groups: open, closed, private (2026-11-22)

The New group form asks only the name, what it is working on, and who can join:

| Choice | In the form | On the group's metadata (9007 / 9002) |
|---|---|---|
| Open | "Any member can join" | `open`, `public` |
| Closed | "Members apply to join" | `closed`, `public` (+ `prompt`, `vouches` from Settings) |
| Private | "Only invited people" | `closed`, `private` |

Private: no Apply button, no card or list entry for someone who is not in it, nobody asks to
join (a `9021` for a private group is not shown and not answered); an admin adds people (`9000`,
or `POST /group-roles` role `member` for an admin by role). `POST /group-meta` takes
`"private": true|false` next to `closed`. The question people answer and how requests are
accepted (by its stewards / when members vouch) are in the group's Settings, shown for Closed
only; a new Closed group starts with no question and "by its stewards".

A group's Settings keep two separate things: who can join (the three choices) and who can read
its page (Members only / Public, as before); a Private group has no Public choice. A Private
group is not shown to anyone who is not in it, the belfry's stewards included.

After the form, the creator picks people from a grid of the belfry's members (the directory;
not guests) and adds them: one `9000` per person, `["p", <pubkey>, "member"]`.

## A channel's Settings (2026-11-22, Xavier's order)

Top to bottom: Name and Description; Who can read it; Posts; E-mail; Pin. Nothing about
notifications (they have their own row in the pull-down). The pull-down shows no e-mail address
line: the address lives in Settings → E-mail only.

**Posts: which kinds this channel takes.** One row per kind, each with a switch and one line of
explanation, all on by default; at least one stays on:

| Row | Line | id |
|---|---|---|
| Messages | "Plain messages, pictures and files" | `message` |
| Threads | "Each post starts its own thread" | `thread` |
| Events | "Dates people can answer" | `event` |
| Proposals | "Texts to discuss and decide" | `proposal` |

On the wire, in the channel's `belfry:web:<channel>` setting next to the old `posts`:
`"accepts": ["message", "thread", "event", "proposal"]` (any non-empty subset; absent = all).
A client that writes `accepts` also writes `posts` for older apps: `"anything"` when all four are
on or when the set is none of the single cases; `"thread"`, `"event"` or `"proposal"` when that
one alone is on. An old `posts` value that is none of these four (`"request"`: the requests
page) is kept as it is while all four kinds are on, so that page keeps working; it follows the
rule above as soon as one kind is off. A client that reads prefers `accepts`; without it, `posts: anything` = all four
and a single value = that kind alone. With `message` off the composer's plain send is replaced
by the kinds that are on.

**E-mail.** The channel's address (`POST /api/v1/mail-alias`, docs/mail.md "A channel's
address"): shown to everyone who may edit the channel, editable by the belfry's admins only,
with a live check (available / taken / reserved / invalid) before Save; automatic when nobody set
one. Under it "E-mails from anyone" and "Forward e-mails", as before.

**Pin.** "Pin for me", and for the belfry's admins "Pin for everyone". The main channel shows one
line instead, not a disabled control: "Townsquare is always first in everyone's menu."

## The Messages list: faces, not envelopes (2026-11-22)

Every row is a face, a name in bold, then one line (the subject for an e-mail thread, else the
last message), never an envelope icon. For an outside e-mail correspondent the face is their
picture from `https://belfry.<domain>/api/v1/avatar/<sha256 of the trimmed, lowercased address>`
when that answers 200, else a disc with their initials in a colour picked from the hash.
Initials: the first letters of the first and the last word of the name ("Ana Maria Silva" →
"AS", "Lina" → "L"); with no name, the first letter of the address. Apps cache by hash for 30 days (a 404 for 7) and never contact
Gravatar themselves. A small envelope may mark the row's corner so e-mail stays recognisable.

## Faces for e-mail correspondents

Someone who writes by e-mail has no profile. Apps show their Gravatar picture when there is one, else initials, and never ask Gravatar themselves (that would tell Gravatar who reads whose mail, and from where): the belfry asks, by hash, and keeps the answer.

- Hash = SHA-256, 64 lowercase hex, of the address trimmed and lowercased (`" Marie@Example.org "` → the hash of `marie@example.org`).
- `GET https://belfry.<domain>/api/v1/avatar/<hash>`: no sign-in (it is loaded as a picture); CORS like the rest of the API.
- `200` = the picture (JPEG, PNG, WebP or GIF, 160 px, at most 200 kB), `Cache-Control: public, max-age=2592000, immutable`.
- `404` = draw initials. `max-age=604800` when Gravatar has no picture; `max-age=600` when Gravatar did not answer or the belfry has faces turned off. `429` when one address of the network asks more than 600 times a minute.
- Apps cache by hash (the HTTP cache does it; honour the `max-age` of a 404 too) and never call Gravatar or any other service with the address or its hash.

On the server (mailfaces.py): pictures are kept 30 days and "no picture" 7 days in the folder `faces/` next to the register, at most 5000 entries; a failure is remembered 10 minutes; one request to Gravatar per hash at a time. `AVATARS=0` in `infra/.env` turns it off: always `404`, nothing is asked.

## Notes to myself

Xavier, 2026-11-22 / 23: "add as a default pinned thread in messages a message to yourself for
personal notes. And explain it's encrypted only for your own devices. No one can read them."
"…pinned at the top of messages on all platforms with the lock at the top showing that
everything is e2e encrypted to all your devices."

- **The row.** The first row of Messages on every platform, always, also when empty: "Notes
  to myself" (the person's own face with a small lock, or the notes icon the board shows), then
  the last note or "Only you can read these". It cannot be unpinned, archived or deleted, never
  counts as unread, never notifies. For everyone who has Messages (members, guests, agents).
- **The conversation** is the ordinary direct conversation whose only participant is me: no
  new kind. A note = a kind 14 rumor, `pubkey` = my account, exactly one `p` tag = my account;
  files = kind 15 the same way; replies (e root), reactions (7), deletions (5) and edits as in
  any direct conversation. No typing events. Conversation id = the DM id of {me}.
- **Sealed** (docs/sealed.md) whenever I have a chain and this device is in its head: wrapped
  to my own head letterbox, seal signed by this device, no signer call. Unlike other
  conversations this does NOT wait for my chain to be `ready` (the only reader is me; a device
  of mine outside the head shows the usual waiting banner until it is added). A device makes
  my chain when I have none, exactly as it does for any first sealed conversation.
  On an account that can seal, a note is never sent the old way because the chain is not
  there yet: with a definite "no chain" answer the device makes it now (first-device path)
  and holds the note, shown as sending, until the chain is anchored; then it is sealed.
- **The lock.** When sealed, the header shows the lock and the screen starts with one fixed
  line: "Encrypted for your own devices. Nobody else can read your notes, not the belfry's
  admins either." Tapping the lock opens the Encryption screen, as in any sealed conversation.
  While there is no note yet, one more line under it, sealed or not: "Write anything you want
  to keep: ideas, lists, pictures, files."
- **Not sealed** (an agent, an account that holds its own key, or a device that cannot seal):
  the notes are NIP-17 wraps to my own account through the signer, as old direct messages;
  no lock, and the line reads "Private to your account. Not end-to-end encrypted on this
  account." Never the "nobody else" sentence there. Old unsealed notes stay readable and are
  marked like any old message once the conversation is sealed.
- **Reading.** A device shows every note any of my devices wrote (the letterbox history it
  can open); a device added later reads what its keys open (5.5 hands it the chain's keys).
  Read state: none needed.
- Not in the first version: to-dos and pins inside Notes, sharing a message into Notes from
  the hold menu ("Save to Notes": next).
