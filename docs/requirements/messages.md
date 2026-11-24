# Messages: direct messages, notes, files, typing, link previews, sending

Prefix `MSG`. Contracts: docs/one-shape.md (Messages list, Notes, replies), docs/typing.md,
docs/link-previews-v2.md, docs/encrypted.md, docs/mail.md, the apps' SPEC (connection manager,
DM files: vectors/dm-files.json). Canvas: Messages https://claude.ai/artifact/R8UHkCSh7gQDxbyC5LEqSZ.
Encryption rules are in encryption.md.

## The Messages list

- **MSG-1** [apps] Every row is a face, the name in bold and one line (an e-mail thread's subject,
  else the last message); never an envelope icon (a small envelope may mark the corner).
  (owner, 2026-11-22)
- **MSG-2** [apps] No lock and no "Not encrypted" in the list; the lock shows only at the top of
  a conversation. (owner, 2026-11-23)
- **MSG-3** [apps] The Messages header shows my own address (username@domain).
  (owner, 2026-11-23)
- **MSG-4** [apps] A DM header shows the other person's face. A DM conversation has the same
  shape as any channel. → ui-rules.md UI-40

## Notes to myself

- **MSG-10** [apps] "Notes to myself" is the first row of Messages on every platform, always, also
  empty; it can't be unpinned, archived or deleted, never counts as unread, never notifies.
  "add as a default pinned thread in messages a message to yourself for personal notes. And
  explain it's encrypted only for your own devices. No one can read them." (owner, 2026-11-22/23)
- **MSG-11** [all] Notes are the direct conversation whose only participant is me; no typing
  events there. A note on a device that is not yet able to encrypt is kept on the device and
  sent once it can, never sent in clear on an account that can encrypt.
- **MSG-12** [apps] The fixed line when encrypted: "Encrypted for your own devices. Nobody else
  can read your notes, not the belfry's admins either." Where it is not end-to-end encrypted (an
  agent, an own-key account): "Private to your account. Not end-to-end encrypted on this
  account." — never the "nobody else" sentence there. Notes never show "Locked".

## Replies, reactions, menus

- **MSG-20** [all] Reply in a DM starts a thread under the root message (kind 14 with an `e` root);
  older apps show it in line. → docs/one-shape.md §3
- **MSG-21** [all] Reactions work in DMs and on thread replies. → vectors dm-reactions.json,
  reply-reactions.json
- **MSG-22** [apps] The DM hold menu has reactions, Reply, Copy text only: no Pin, no Make a
  to-do, no Copy link in this version (a DM has no address).

## Files and pictures

- **MSG-30** [all] Pictures, files and audio recordings in DMs; one file per message event, a
  caption and several files grouped; up to 25 MB (guests 5 MB); a video is sent as a file.
  (owner, 2026-11-21) → vectors/dm-files.json
- **MSG-31** [all] Files in private conversations are encrypted on the device before upload; the
  belfry stores only ciphertext.
- **MSG-32** [all] Pictures show inline, never as a file link, when their type is an image
  (jpeg, png, gif, webp, heic/heif, or untyped with a picture extension). "Images should be
  displayed inline instead of linked as a file" (owner, 2026-11-22). Mailed pictures too.
- **MSG-33** [apps] An encrypted file opens in the app only as image, video, audio or PDF;
  anything else downloads.

## Typing

- **MSG-40** [all] "Lina is typing…" in DMs and in every channel, group, event chat, proposal and
  thread. (owner, 2026-11-21) → docs/typing.md
- **MSG-41** [apps] Sent at most every 4 s, shown for 6 s, best effort (never queued, never
  retried, never delays a message); never a notification, sound or unread count.
- **MSG-42** [apps] A device setting "Show others when I'm typing", on by default.
- **MSG-43** [apps] No typing events in Notes or in end-to-end encrypted conversations (v1 sealed
  and format 3, docs/encrypted.md §6), not in archived channels, not from
  reading guests.

## Link previews

- **MSG-50** [all] Link previews are built by the SENDER: the sender's app makes the preview while
  composing (picture, site, title, description), shows it above the field with ✕, and attaches it
  to the message. "Good idea. In fact for all" (owner, 2026-11-24). → docs/link-previews-v2.md
- **MSG-51** [all] Readers never fetch anything for a preview: not the outside page, not the
  belfry's preview endpoint. A message without a preview shows a plain link. The picture is only
  taken from the belfry's own Blossom.
- **MSG-52** [all] One preview per message: the first outside https link, never the belfry's
  own domain or subdomains (those get in-app cards, UI-35).
- **MSG-53** [all] In encrypted conversations a phone may attach a preview whose picture is
  encrypted like any photo there; the web sends none there (it can only fetch through the
  belfry, which would learn the link). → webapp.md WEB-10. Changed on 2026-11-24: v1 previews
  were fetched by readers through the belfry, which leaked links from sealed DMs for about an
  hour.
- **MSG-54** [apps] Sender-side fetching follows the safety rules (https, 443, no private
  addresses, 3 redirects, 5 s, 512 kB, images re-encoded small). → link-previews-v2.md

## Sending

- **MSG-60** [apps] One connection manager per app owns every socket: foreground check, keep-alive
  about every 30 s, a timeout drops the socket, quick retries. (owner, 2026-11-21, after a DM
  stuck on "sending…")
- **MSG-61** [apps] Direct messages never wait behind other queued items; the outbox has one lane
  per conversation, group and relay.
- **MSG-62** [apps] Send states are shown ("Still sending…", failed with Try again); a per-send
  timing trace sits in Diagnostics, with no secrets in it.
- **MSG-63** [apps] A permanent refusal (upload 4xx other than 401/408/429) fails that message;
  it never stalls the queue.
- **MSG-64** [apps] Catch up when the tab or app comes back to the foreground (a fresh
  subscription from the last event). (owner, 2026-11-23)
- **MSG-65** [apps] Metadata and the charter are always read without `since`; after a
  late authentication the subscription is sent again; "complete" and "absent" only count from
  an answer received after authentication. (lead, 2026-11-19) → iOS SPEC 19.42

## E-mail as messages

- **MSG-70** [all] Mail to username@domain lands in the member's Messages; members answer from the
  chat; mail threads group by In-Reply-To / References, else subject. → docs/mail.md
- **MSG-71** [all] A member's own mail into a channel is posted as the member only when it passes
  DKIM or SPF aligned with its From.
