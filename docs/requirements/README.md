# Belfry requirements

The rules Belfry must follow, written for every agent and person who builds a part of it: the
server, the web chat, the iPhone app, the Android app, the canvas. Asked by the owner on
2026-11-24: "Keep track of all the requirements like this one that are platform independent to
feed all the agents in a docs/requirements/*.md."

A **requirement** says WHAT the product does or must never do, and WHY when it isn't obvious. It
is short and testable. It is not the wire format: the contracts in `docs/*.md` (and the apps'
`SPEC.md` + `vectors/`) say HOW, and a requirement points to them. When a requirement and a
contract disagree, the newer decision of the owner wins; tell the lead (`belfry`) so both get
fixed.

## How to read a line

```
- **ONB-13** [all] A member link asks only the e-mail address and the code, then the name and username screen. (owner, 2026-11-19) → docs/roles.md
```

- **ID**: chapter prefix + number. IDs are never reused; a withdrawn rule keeps its ID with
  "Withdrawn on <date>: …" so nobody brings it back by accident.
- **[binds]**: who must follow it: `all` (every app and the server), `apps` (web, iPhone,
  Android), `server`, `web`, `iphone`, `android`, `canvas`, `team` (process).
- **Source**: the owner's words in quotes when short, with the date. "(owner, date)" without
  quotes = his decision as recorded by the lead. "(lead, date)" = the lead's ruling, which the
  owner may override.
- **→** the contract (`docs/<x>.md` §), the canvas board id (`<area>/<name>`), or SPEC section.
- "(to confirm)" = the source is unclear or the owner has not answered yet: ask before relying on
  it.
- "Changed on <date>: was …" is kept only where the old rule could come back as a regression.

## How to add a requirement

1. Put it in the chapter where an agent would look first; one line per rule (two or three short
   lines when needed). If it binds only one platform, it goes in that platform's file.
2. Next free ID of that chapter. Quote the owner and date; add the pointer.
3. Keep each file under about 250 lines; split a chapter in two when it grows past that and add
   the new file to the index below.
4. Never write secrets, keys, setup links, full e-mail addresses, or names of community members
   (say "the owner", "a member", "a guest").
5. Commit on its own ("Requirements: …") and tell the lead.

## Index

Cross-platform (bind every app):

| File | Prefix | What |
|---|---|---|
| [ui-rules.md](ui-rules.md) | UI | buttons, menus, navigation, words, names not ids, the one shape, canvas first |
| [onboarding.md](onboarding.md) | ONB | sign-in, applying, member and guest links, guest mode, usernames, Link your phone, agents |
| [cards.md](cards.md) | CARD | printed cards: claiming, topics vs groups, hashtags, stewards, followers |
| [channels.md](channels.md) | CH | channel list, header, settings, pins, archive, groups, followers, summaries, e-mail threads, notifications |
| [events.md](events.md) | EV | events, invitations, tickets, prices, check-in, public pages |
| [messages.md](messages.md) | MSG | direct messages, Notes to myself, files, typing, link previews, sending |
| [encryption.md](encryption.md) | ENC | end-to-end encryption (format 3): device lists, conversation keys, held states, rollout |
| [privacy.md](privacy.md) | PRIV | what never leaves, never shows, never gets fetched or rendered |
| [nips.md](nips.md) | N | Nostr kinds, NIPs and Belfry's own conventions, one line each |
| [server.md](server.md) | SRV | the server and deploys |
| [process.md](process.md) | PROC | how the team works |

Platform-specific (only what differs from the cross-platform files):

| File | Prefix |
|---|---|
| [webapp.md](webapp.md) | WEB |
| [iphone.md](iphone.md) | IOS |
| [android.md](android.md) | AND |

## Where the details live

- Contracts: `docs/one-shape.md`, `encrypted.md`, `cards.md`, `guests.md`, `membership.md`,
  `roles.md`, `charter.md`, `checkin.md`, `followers.md`, `push.md`, `typing.md`, `actions.md`,
  `mail.md`, `link-previews-v2.md`, `website-accounts.md`, `requests.md`, `sales.md`,
  `archive.md`, `agents.md`, `remote-apps.md`, `own-signer.md`, `setup.md`, `sealed.md`.
- Shared app specs: `SPEC.md` and `vectors/` in opencollective/belfry-android and
  opencollective/belfry-ios; the web chat's `vectors/` (branch `chat-source`).
- Screens: the canvas index (claude.ai artifact TVzTJ2JqUfpTvPUJLFMr62), one canvas per area;
  board ids `<area>/<name>`.
- Team roles and day-to-day rules: `~/agents/belfry/TEAM.md` on the team's machine.
