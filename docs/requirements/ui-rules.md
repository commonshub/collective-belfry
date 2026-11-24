# UI rules (every app)

Prefix `UI`. Bind the web chat, the iPhone app, the Android app, server-rendered pages (console,
invite pages, print views) and the canvas, unless the line says otherwise.

## Design process

- **UI-1** [all] Every change to what people see is drawn on the canvas first; it is built only
  after the owner picks (bug fixes, or when he says "build it directly", are the exceptions).
  "By default, when I ask you, you always make changes first in the canvas to iterate faster."
  (owner, 2026-11-18) → TEAM.md
- **UI-2** [all] When a board exists, the build follows it as closely as possible: layout,
  wording, sizes, order. This holds for server pages and print views too. "Please follow as close
  as possible the canvas when there is one (remember that)" (owner, 2026-11-23, after the printed
  cards page did not match its boards).
- **UI-3** [apps] Each platform adapts a board with its own native elements (an iOS sheet, a
  Material bottom sheet, a web `<dialog>`); the canvas says what and in what order, the platform
  says how. Wire formats are never invented per platform. (owner, 2026-11-18) → TEAM.md
- **UI-4** [all] Discord is the template to learn from for the chat; iMessage and WhatsApp are
  the yardstick for how native the phone apps feel ("sloppy" = non-native). (owner, 2026-11-09,
  2026-11-15)
- **UI-5** [canvas] Boards use only the demo world's people and content (Ana, Tom, Lina…), never
  a real member's name, face or address.

## Buttons, menus, forms

- **UI-10** [all] Every button and form locks the moment it is tapped and shows feedback (dim,
  spinner) until its work settles; a double tap never sends twice. Server endpoints that create
  things are idempotent anyway. (owner, 2026-11-09, after one guest's eight taps made eight
  accounts)
- **UI-11** [all] Menus (message actions, the composer's add menu, sheets of choices) are
  grouped list rows with a line icon on the left, Discord-style; never stacked full-width
  bordered buttons with emoji. (owner, 2026-11-09)
- **UI-12** [all] Text fields are at least 16 px / pt / sp (iOS zooms the page on smaller fields
  and the screen scrolls sideways). (lead, 2026-11-10)
- **UI-13** [all] No screen scrolls sideways.
- **UI-14** [all] Short labels that don't wrap ("Comment", not "Comment for the team").
- **UI-15** [all] On a list screen the "add one" action is an icon at the top right (Members,
  Groups…), not a row or a big button in the list. (owner, 2026-11-22) → canvas v185
- **UI-16** [apps] Copying gives feedback (a check mark; on phones the success haptic).
- **UI-17** [apps] Confirmation inside the app, never the browser's `confirm()`; a dangerous
  choice that is one tap away (making someone a steward, deleting) asks first. (lead, 2026-11-10,
  2026-11-22)
- **UI-18** [apps] A server error is shown with the app's own words chosen by the `error` code;
  never the server's `message` text. → every contract's error tables

- **UI-26** [apps] A form on a phone has ONE main button, at the bottom (Create / Save changes),
  never repeated in the header. On a laptop, where a form has no bottom bar, Save sits top right.
  "remove, just at the bottom enough" (owner, Event form canvas, 2026-11-24)

## Navigation

- **UI-20** [apps] A ‹ back button never pushes a screen: it goes back one step when that step is
  ours, else replaces the screen with its parent. (lead, 2026-11-10, after back loops)
- **UI-21** [apps] Phones: a bottom bar Today · Calendar · Messages · Groups · Town; laptop and
  tablet keep the left column. (owner, 2026-11-22) → docs/one-shape.md §1. Changed on 2026-11-22:
  was Home · Calendar · Create (+) · Groups · Town. (to confirm on iPhone: TEAM.md and the
  canvas agent's notes still name the older bar.)
- **UI-22** [apps] Tapping Town always opens the main channel (`general`), never the last channel
  opened; a second tap scrolls to the newest. (owner, 2026-11-20)
- **UI-23** [apps] Profiles push as their own screen; drawer rows open screens, never sheets.
  (owner, 2026-11-15) → iphone.md for the one exception
- **UI-24** [apps] Wording in the chat: "group" (never "pod"), "Steward" (an admin), "Door" (the
  check-in role of an event), "To-do", "Members" (the directory row). The main channel shows as
  "Townsquare"; these names are display only, never written back to ids or mail aliases.
  (owner, 2026-11-14 to 2026-11-17)

## Names, not ids

- **UI-30** [all] Never show an npub, a hex key, a group id, an event id or a token as text,
  anywhere (pages, mails, apps, boards). "Also don't ever display a npub." (owner, 2026-11-18)
- **UI-31** [all] A person without a profile reads "Somebody" (an agent "An agent"); several
  unresolved people fold into "and one other" / "and N others", with keys at most in a hover
  title. Changed on 2026-11-19: was "Unknown" and a shortened npub.
- **UI-32** [all] A channel, group or event whose name has not loaded shows a neutral placeholder
  (skeleton bar), never its id; an unknown channel reference reads "#channel".
- **UI-33** [all] "Always use the custom slug if one is set as a general rule": every event link
  uses its slug; never "ev-…" in text ("Just use the name of the event"). (owner, 2026-11-18)
- **UI-34** [all] The belfry's own service keys (the belfry key, the groups key) never appear as
  a person (a host, an author). What they write for someone carries a `by` tag and shows as that
  person's. (owner, 2026-11-19, 2026-11-23) → docs/cards.md §2.2
- **UI-35** [apps] An in-app link (a channel, event, to-do, message, person, drop box) is never
  shown as a raw link: it is a preview box that opens the thing and offers its direct action.
  (owner, 2026-11-20) → vectors/references.json

## One shape for conversations

- **UI-40** [all] Everything is a channel behind the scenes: channels, groups, events, DM
  conversations, e-mail threads, to-dos, proposals and message threads share one shape: an
  expandable header, the feed, the composer. Only the header's content and quick actions differ.
  "It's important to try to stay consistent both for the way it works and looks / behave."
  (owner, 2026-11-22) → docs/one-shape.md §2
- **UI-41** [apps] The header is two lines at most: the title, then one row of one-colour icons
  with counts (zero = left out); no avatars and no message count in it. Items leave in the fixed
  order of the contract when the row is short. Each item has a spoken label. → docs/one-shape.md §2
- **UI-42** [apps] Pulled down, the thing itself is on top (the e-mails, the to-do, the event,
  the channel's description and pins) and the conversation below. Each row of the pull-down
  opens a screen; nothing unfolds as a list in place.
- **UI-43** [apps] Events, to-dos, proposals and e-mail threads have three states: tiny (the
  header), mid (the default while it needs me) and full.
- **UI-44** [apps] One hold menu on every message: reactions, Reply, Make a to-do, Copy text,
  Copy link, Pin (direct messages: reactions, Reply, Copy text only). → docs/one-shape.md §3
- **UI-45** [apps] Actions about an item (to-do made or done, RSVP, vote, meeting) show as one
  activity line in the feed with a hook that opens a small preview with its quick action.

## Look

- **UI-50** [apps] Light and dark mode. A belfry's brand colour is used as the accent only where
  it reads (4.5:1 on the page and on raised surfaces, per mode); apps never draw text or icons
  in the raw primary colour. (owner, 2026-11-21) → GET /api/v1/branding `colors`
- **UI-51** [apps] Nothing conveys state only by a subtle grey or by animation (the owner tests
  on an e-ink Android tablet); motion respects the system's reduced-motion setting.
- **UI-52** [apps] Pictures keep their size while loading (dimensions from `imeta dim`), so the
  feed never jumps.

## Offline first

- **UI-60** [apps] Everything already seen opens at once from the device's store; channels and
  conversations open instantly and never wait for the network. (owner, 2026-11-15)
- **UI-61** [apps] Every write goes through the outbox: it survives going offline and a restart,
  keeps its order per conversation, and a final refusal shows next to the item with Try again /
  Discard. Never a write that only lives in memory. (lead, 2026-11-09) → messages.md MSG-60…
