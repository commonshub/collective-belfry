# Deleted and archived groups

Xavier, 2026-11-16: a deleted group's card says so and opens nothing, a link to it says so; and a
group can be **archived**, a softer way to delete: out of the channel list, read only, until
someone unarchives it.

"Group" here is anything with a NIP-29 group id that people open from the channel list or a card:
channels and groups (ids `pod-…`). Events (`ev-…`, `ws-…`) and proposals (`prop-…`) keep their own
cancel and status and are never archived; `general` neither.

## Deleted

What zooid does with a kind 9008 (delete-group; read in its source at the pinned commit,
`zooid/groups.go` `DeleteGroup`, called from `OnEventSaved`): it deletes every event of the group,
its 39000–39003 and pins (by `d`) and everything tagged `h` = the group, **except kind 9008
itself**. The 9008 stays and is served to any relay member (`CanRead`: with no metadata left, only
a 9008 of that group is readable). Any later write there is refused ("group not found"), and a new
9007 for the same id is accepted: the id is free again, with the old 9008 still stored.

So a client knows a group is deleted when:

- it has a 9008 for it and no 9007 newer than that 9008 (the group made again), or
- the groups relay answered a question about this very group to the end (EOSE) with nothing:
  `{"kinds":[39000],"#d":[id]}` and `{"kinds":[9007,9002,9008],"#h":[id]}`. That is also what a
  group looks like whose creation never reached the relay while its card did (belfry.sh,
  `pod-canal-41ec`: a group deleted with a 9008 would have left that 9008).

The live subscription can't answer the second question (one filter with a limit over every kind:
an old group's metadata may simply not be in it), hence the question per group. Until it is
answered (first load, offline) the state is unknown and nothing says "deleted". A channel's guest
never concludes it: the relay shows a guest only their own channels.

The card a group left in its parent stays after the delete; it is how people learn it is gone.

## Archived

State: the tag `["archived", "1"]` on the group's kind 9002 (edit metadata); `["archived", "0"]`
unarchives. Read like `prompt` and `vouches`: of the group's 9007 / 9002 events and the relay's
39000 (a copy of the last 9002 with the same time), **the latest one that carries the tag wins**.
A 9002 without the tag (a client that doesn't know it) changes nothing.

zooid replaces a group's whole metadata with each 9002, so every 9002 carries all of the group's
settings: `name`, `about`, `picture`, `open` / `closed`, `prompt`, `vouches` (the chat's
`metaTags`, the belfry's `/group-meta`).

Who may: whoever may edit the group (its admins, admins by role included, and the belfry's
admins). The relay's own admins (39001, the creator, belfry admins) publish the 9002 themselves;
an admin by role, whose 9002 zooid refuses, goes through the belfry.

The relay enforces none of it (zooid has no such state and is not patched): an old client can
still post in an archived group. Clients show those messages; the belfry sends no notification
for them.

### API

`POST /api/v1/group-meta` (NIP-98, docs/checkin.md) takes an optional boolean `archived` and
answers with it:

```
{"channel": "pod-garden-1a2b", "archived": true}
→ 200 {"channel", "name", "about", "closed", "archived": true}
```

- The 9002 is signed by the groups key and keeps every other tag of the group's metadata.
- While the group is archived, anything but `{"channel", "archived": false}` is
  `409 {"error": "archived"}`: another field, `archived: true` again, or nothing at all.
- `general`: `409 always_open`. An event's or a proposal's group: `422 invalid`.
- `archived` that is not a boolean: `422 invalid`. Not an admin: `403 not_allowed`.

Refused with `409 archived` for an archived group:

- `POST /group-roles` (any role change, `removed` too),
- `POST /channel-invites` (no new guest, member or co-host links),
- `POST /channel-guests`,
- `POST /channel-invites/<token>/join` (a link made before it was archived brings nobody in).

When the relay can't be asked, nothing is refused as archived (the calls fail on the relay
anyway).

Push (docs/push.md): no notification for a message in an archived group; the watcher's view of a
group is at most a minute old.

Not covered: mail to an archived channel's address, drop-box uploads and threads from the web
still land there (the relay takes them); followers of its announcements keep their follow.

### In the chat

- Out of the channel list, the drawer, every picker (invite, `#` references), requests and counts.
- Readable from its card, a link, search. Under a plain header (no pull-down): "Archived · read
  only", with Unarchive for those who may. No composer, reactions, replies, edits, to-dos, pins,
  invite, settings or bell; the outbox refuses any event to an archived group except the 9002
  that unarchives it, a 9022 (leave) and a 9008.
- Its card in the parent: its name, "Archived", greyed, opens it read only.
- Archive: at the bottom of the group's settings, with a confirmation in the app. Not offered for
  `general`, events and proposals.
- A row "Archived" at the very bottom of the channel list when there is at least one, opening a
  screen that lists them (`#/archived`).
