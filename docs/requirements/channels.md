# Channels, groups, followers, summaries

Prefix `CH`. Contracts: docs/one-shape.md, docs/archive.md, docs/membership.md, docs/followers.md,
docs/push.md, docs/mail.md, docs/roles.md, docs/charter.md, docs/actions.md. Canvas: channels,
home.

## The list (Town)

- **CH-1** [apps] No "follow" for the menu: the Town list = channels pinned for everyone, then
  channels I pinned, then channels where I was mentioned, posted or reacted in the last 30 days
  (opening one doesn't count), by newest activity. Computed on the device. (owner, 2026-11-22)
  → docs/one-shape.md §1
- **CH-2** [all] The main channel (`general`, shown "Townsquare") is always first in everyone's
  list; its Settings say "Townsquare is always first in everyone's menu" instead of a disabled
  control.
- **CH-3** [apps] "Add a channel" (＋ on Channels): typing filters the known channels, "Create"
  comes last. Any member may create a channel. There is no "All channels" screen. A channel not
  in my list opens as a preview; writing or reacting brings it in. (owner, 2026-11-22)
- **CH-4** [apps] Members is a row of the list with its count and "N requests waiting for you";
  pending requests (agents, people, my groups) show at the top of the members directory. There
  is no separate Requests row. (owner, 2026-11-15)
- **CH-5** [apps] Belfry admins order channels and categories (hold and drag); the order is
  shared by everyone. → `belfry:channel-order`

## Pins

- **CH-10** [all] Pin for everyone (belfry admins) and Pin for me (anyone); a person's own pins
  are private (encrypted to themselves). Guests keep their pins on the device. (owner, 2026-11-22)
- **CH-11** [all] Pinned messages in a channel are pinned by channel or community admins; each
  person may hide one ("Got it, hide").

## Settings

- **CH-20** [all] A channel's Settings, top to bottom: Name and Description; Who can read it;
  Posts; E-mail; Pin. Nothing about notifications there. (owner, 2026-11-22)
- **CH-21** [all] Every member has the channel's Settings row with "Pin for me" and "Notify me
  of everything"; admins see the rest.
- **CH-22** [all] Who can read it: Members only (default for new channels) / Public (asks first) /
  Closed (people apply; channels and groups, never `general`). A Private group has no Public
  choice. (owner, round 6, 2026-11-18)
- **CH-23** [all] Posts: switches for Messages, Threads, Events, Proposals, all on by default, at
  least one stays on; with Messages off the composer offers only the kinds that are on.
- **CH-24** [all] E-mail: the channel's address is shown to whoever may edit the channel and
  editable only by the belfry's admins, with a live check; it is not shown in the pull-down.
  "E-mails from anyone" (shared inbox) and "Forward e-mails" sit under it.
- **CH-25** [all] Settings of different channels never conflict or override each other: every
  per-channel setting is stored under the channel's own name (one replaceable event per channel,
  never one shared by all). (owner, 2026-11-22, after one channel's inbox switch turned another
  off and mail was lost)
- **CH-26** [apps] Saving settings returns at once and rides the outbox; a final refusal shows a
  red mark by the channel's name with Try again / Discard. A switch shows Loading until the
  admin list has arrived.

## Groups

- **CH-30** [all] "There is a clear distinction between the membership in the belfry and just
  joining a subgroup that somebody has created." (owner, 2026-11-14) → docs/membership.md
- **CH-31** [all] A new group asks only its name, what it works on, and who can join: Open ("Any
  member can join"), Closed ("Members apply to join"), Private ("Only invited people"). The
  question and how requests are accepted (by stewards / by vouches) live in the group's Settings,
  for Closed only. (owner, 2026-11-22)
- **CH-32** [all] A Private group is shown to nobody who is not in it, the belfry's stewards
  included: no card, no list entry, no Apply. Everything in it is encrypted once format 3 is on
  (encryption.md). (owner, 2026-11-22 / 2026-11-23)
- **CH-33** [all] After creating a group the creator may pick people from a grid of the belfry's
  members (not guests) to add.
- **CH-34** [all] A group's roles: Steward (admin) · Member; in an event also Door (check-in).
  Admins of a group = the relay's list, the creator, and people given the role; the server acts
  for admins by role.

## Archived and deleted

- **CH-40** [all] Archive is the soft delete: out of the channel list, pickers, unread counts and
  push; read only with a banner and Unarchive; an "Archived" row at the end of the list. Never
  `general`, never events or proposals. (owner, 2026-11-16) → docs/archive.md
- **CH-41** [all] A deleted group's card says "This group has been deleted" and opens nothing. A
  group is called deleted only on the relay's complete answer, never while its own creation is
  queued or just made, never by absence before authentication.

## Followers and e-mail summaries

- **CH-50** [all] Anyone with a proven address can follow a channel, topic, group or event by
  e-mail: ASAP (its announcements), Weekly (a full summary, the default) or Monthly. "Ask whether
  you want a weekly or monthly summary by email. Default to weekly." (owner, 2026-11-23)
  → docs/followers.md
- **CH-51** [server] A summary lists up to 20 new messages (name, time, the first ~200
  characters, "Open in the chat"), then "and N more"; "N new people started following"; nothing
  sent for an empty period.
- **CH-52** [server] Only people who may read the channel get its full summary; anyone else gets
  its announcements only. Private groups get no summary and no mention digest.
- **CH-53** [all] Announcements are messages tagged as such by one of the channel's admins;
  others' tags are ignored.
- **CH-54** [all] Every mail has the frequency, the two others and unsubscribe as links to a page
  with a button, and one-click List-Unsubscribe.

## Notifications

- **CH-60** [all] Per channel: one sheet "Notify me about #x": Everything / When it's for me
  (default) / Nothing, plus "Also e-mail me its announcements". (owner, round 6, 2026-11-18)
  → docs/push.md
- **CH-61** [apps] The bell in a channel = "Notify me of everything", off by default.
  (owner, 2026-11-22)
- **CH-62** [apps] Ask for notification permission at a moment that explains it (after a first
  hello, or a "Turn on notifications" row), never at launch.
- **CH-63** [all] A notification carries no message text and no ids the gateway could read; my
  own copies and notes never notify (`silent`). Tapping opens the place at the message.

## E-mail threads (shared inbox)

- **CH-70** [all] Shared-inbox mail groups into threads; the feed shows one row per thread; the
  thread shows each mail framed. (owner, 2026-11-16)
- **CH-71** [all] Reply all is the only reply: the sender plus the mail's To and Cc (never Bcc,
  never the belfry's own addresses); replies leave from the channel's plain address.
  (owner, 2026-11-22)
- **CH-72** [all] "Answered" is set automatically when a reply is delivered; a newer incoming mail
  reopens the thread. Team-only drafts read "Internal draft". Cc'd outsiders are addresses only,
  never accounts. (owner, 2026-11-22)
- **CH-73** [all] An outsider's mail reads like a guest's message tagged "guest, by e-mail"; the
  composer switches between "Reply to ‹name›" (default, by e-mail from the channel's address) and
  "Comment" (team only). (owner, round 6, 2026-11-18)
- **CH-74** [all] A member's own mail to a channel is a normal message with a small "via e-mail".
- **CH-75** [all] Faces for e-mail correspondents come from the belfry's avatar proxy by hash, else
  initials; apps never ask Gravatar themselves. → privacy.md PRIV-23

## To-dos, proposals, charter

- **CH-80** [all] Any message can become a to-do; a to-do opens as a conversation; claim / assign
  / unassign; a due date and description are optional, shown first in its pull-down, never in
  the header. → docs/one-shape.md §3
- **CH-81** [all] A to-do in #issues is "Working on now" when claimed and "Up next" when assigned
  (to confirm: proposed to the owner 2026-11-19).
- **CH-82** [all] A proposal channel shows the proposal on top like an event: status, text, vote
  buttons, folding to one bar with Vote. (owner, round 6)
- **CH-83** [all] The charter is a list of agreements each person ticks for themselves; a changed
  agreement asks everyone again for that rule only; a new rule never blocks posting (a Home
  reminder only). (owner, 2026-11-16) → docs/charter.md

## Buttons on messages

- **CH-90** [all] A message may carry up to 4 buttons; a press is signed by the person; the
  handler answers done or an error a person can read. Buttons the belfry handles show the app's
  own fixed confirmation text. → docs/actions.md
- **CH-91** [all] The website changes only through a proposal in #website that an admin passes
  (an "Approve and publish" button); a proposal may be scoped to a path and the belfry says
  exactly what would change before approval. → docs/actions.md, skills/belfry-homepage
