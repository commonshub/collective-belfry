---
name: belfry-steward
description: Help a steward of a Belfry community (such as Open Source Village) run it, or build an app that does. Use when a steward asks you to invite people, follow who joined, check someone's membership, open and manage groups and their members, announce a workshop with its own group, or explain how passes, keys, tokens and the steward pages fit together. Also use when building an app on top of a Belfry, to find its relays, APIs and conventions.
---

# Running a Belfry community: the steward's skill

A **Belfry** runs access and identity for one community: who holds a pass (Odoo), their Nostr
keys (Keycast, or the participant's own), sign-in (Authentik), the pass in EUDI wallets, the
community relays, files and village tokens. Stewards run the community. This skill covers what a
steward, or an agent or app acting for one, can do and where.

The hosts are on `<domain>`. Everything below is also listed, with this Belfry's exact
addresses, at `https://<domain>/.well-known/belfry.json`. Read it first when you build an app.
The participant-side skill (passes, ordering, posting, groups) is at `https://pass.<domain>/skill.md`.

## How the pieces fit

- **Membership and identity are separate.** Membership is a pass in Odoo, which is the single
  source of truth. Identity is a Nostr key. Revoking a pass never touches the key, and the key
  stays the participant's.
- **One key per Belfry.** A participant's key belongs to one community: Belfry derives managed keys
  from its own custody seed, and an app that holds a member's seed derives a separate key for each
  Belfry. Don't reuse one Belfry's key in another.
- **Access follows the pass.** The community relay, the groups relay and the file server let in
  only keys with an active pass. They follow Odoo within seconds of a payment, a revocation or
  an expiry. There is nothing to add or remove by hand.
- **Stewards** are the members of the Authentik group `belfry-stewards`. Each gets a free pass.
  A steward's invites are unlimited; a participant has a few (3 by default).

## Rules

- **Act only on what the steward asked.** Invites, removals and revocations affect real people. When
  unsure, ask first. Prefer the smallest action: remove someone from a group before asking for
  their pass to be revoked.
- **E-mail addresses and pass details are personal data.** Use them for the task and don't
  copy them elsewhere. Odoo has them; you don't need your own list.
- **Never ask for anyone's nsec, sign-in code or bunker secret** except the steward's own bunker
  URL, which they give you on purpose. Treat it like a password.
- **Nothing here moves money.** Payments run through Odoo's payment providers, and refunds through
  the stewards in Odoo.

## Acting for a steward: the signer

As for any pass holder (participant skill, section 6): the steward signs in at
`https://apps.<domain>`, chooses **Connect to Nostr App** with the permissions **Belfry event
apps**, and gives you the `bunker://` URL. Every request below is a Nostr event signed through it.
You then have the steward's rights, nothing more. The steward can end your access at any time
(**Revoke Access**).

## What you can do through the API

### Check someone's membership: `GET https://pass.<domain>/api/me`

NIP-98 signed (kind 27235, tags `["u", "https://pass.<domain>/api/me"]`, `["method", "GET"]`,
sent as `Authorization: Nostr <base64 of the event>`). It answers for **the signing key**, so it
tells an app where its own key stands, not someone else's:

```json
{"pubkey": "<hex>", "npub": "npub1…", "member": true, "steward": true,
 "nip05": "osv-0042@<domain>", "email": "dana@example.org",
 "passes": [{"id": 17, "state": "active", "tier": "full", "date_start": "2027-01-25",
             "date_end": "2027-01-31", "payment": "paid"}]}
```

- `member` is true while one pass is `active`. `passes` also lists a pass ordered with this key
  and not yet paid (`pending`), and past ones (`expired`, `revoked`).
- A key without any pass gets `member: false` and `passes: []`, not an error.
- To see whether *someone else* joined, use your invites (below): an invite shows the
  invitee's npub once their pass is active.

### Invite people by e-mail: `https://pass.<domain>/api/invites`

As in the participant skill (section 6, "Invites by e-mail"). For a steward:
- `GET /api/invites` says `"steward": true`, and creating invites is not limited.
- `POST /api/invites` `{"email", "name"?, "note"?}` mails the invite link. The invitee chooses
  a pass and pays on the page. Their npub appears on `GET /api/invites/<id>` once the pass is
  active, and then you can add them to groups.
- An address whose owner holds an active pass already answers `{"state": "member", "npub"}`:
  nobody is invited twice.

### Groups (rooms) and their members

NIP-29 on `wss://groups-relay.<domain>`, exactly as in the participant skill (section 6,
"Groups"): create (kind 9007), name and access (9002, `private`/`closed`/`hidden`), add (9000)
and remove (9001) members, messages (kind 9). The creator administers a group. Stewards are
**not** automatically admins of groups others opened: ask the group's admin, or report the
content (below).

Only people with an active pass can be members. To add someone, you need their pubkey: from
their invite (`npub`, decoded to hex) or from their profile.

### A workshop or session, with its own group

A session is a public NIP-52 calendar event, and its group is an ordinary NIP-29 group. The
participant skill, section 7, has the steps: the event's tags, the group's id, and the two
links that join them (`h` on the event, the event's coordinate in the group). The calendar and
the programme app show the session; the groups app shows the room.

### Village tokens

Each pass brings tokens (50 per pass day) from the mint (`https://mint.<domain>`, unit `osv`),
which are minted when the pass activates. Participants pass them on as nutzaps from the wallet
(`wallet.<domain>`). There is no API to issue extra tokens (rewards, prizes) yet.

## What needs the steward pages

`https://pass.<domain>/steward`: the steward signs in with Authentik. It opens only from the
community's admin network (ZeroTier). These are pages for the steward, not an API, so don't
script them. Tell the steward what to do there:

| Task | Where |
| --- | --- |
| Invite links without an e-mail (to copy and hand out), revoke them | Invites |
| Mark a pending pass paid (cash, bank transfer), send a new sign-in link, revoke a pass | Passes |
| Hide or keep reported content | Reports |
| Accept or decline an application, accept someone as a volunteer | Applications |
| Shifts and volunteers | Shifts, Volunteers |
| Feedback from participants | Feedback |

## What needs a server operator

Commands on the Belfry server (`docker compose exec onboarding python -m belfry_onboarding …`):
`add-steward <e-mail> [name]` (makes someone a steward and gives them their free pass),
`steward-passes`, `login-link <pass id>`, `reconcile`, `sync-relay`, `restore-keys`. Making
someone a steward or taking it away, and the list of stewards, are in Authentik (Directory →
Groups → `belfry-stewards`).

## Not there yet

These are planned, not available, so don't promise them:
- a steward API over NIP-98 for what the steward pages do (mark paid, revoke, applications);
- issuing extra village tokens;
- stewards as moderators of every group.
