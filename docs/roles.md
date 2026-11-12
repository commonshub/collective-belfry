# Roles and member links

A member of a belfry has exactly one **role**: a label and an optional line of description that
people see on profiles and in member lists. Guests and agents have none. The code is
`services/onboarding/belfry_onboarding/roles.py` (its docstring is the reference); member links
live in `guests.py`. Both need the belfry's own register (`REGISTER=belfry`); with Odoo the read
answers the two built-ins and every change is 409 `unsupported`.

All paths are under `https://belfry.<domain>/api/v1` and signed with NIP-98
(`Authorization: Nostr <base64 kind 27235>`, tags `u`, `method`, `payload` for a body).

## The roles

- `member` (built in): what a member is unless an admin says otherwise. Can be renamed and
  described, not deleted.
- `admin` (built in): the belfry's admins. It is the same list as before (the stewards group,
  `belfry.json` `admins`, `POST /admins`): giving or taking it goes through `admins.change`, so
  the last admin can't be removed (`last_admin`). Can be renamed and described, not deleted, never
  the default. The only role with powers (`"powers": true`).
- custom roles: labels with no powers yet.
- One role can be the **default for new members** (`member` unless an admin picks another): the
  invite sheet starts on it, and a membership that opens gets it when the person has no role
  yet. Members who have no stored role stay `member`; changing the default never relabels them.

Stored in the register param `belfry.roles` (`{"roles": [{"id", "label", "description"}],
"default"}`) and, per person, `res.partner.belfry_role` (survives a new pass).

### Read

`GET /roles` (anyone with an active pass) →

```json
{"roles": [{"id": "member", "label": "Member", "description": "…", "builtin": true,
            "powers": false, "default": true},
           {"id": "host", "label": "Host", "description": "…", "builtin": false,
            "powers": false, "default": false},
           {"id": "admin", "label": "Admin", "description": "…", "builtin": true,
            "powers": true, "default": false}],
 "default": "member",
 "members": {"<hex>": "host", "<hex>": "member", "…": "…"},
 "others": {"guest": 3, "agent": 1}}
```

Order: `member`, the custom roles as they were added, `admin`. `members` has every member's key
(not guests', not agents'); a guest gets `{}`. `others` only for admins. `POST /whois` also
answers `"roles": {"<hex>": "<id>"}` for the members among the keys asked, next to `people`
(whose values stay `member` / `agent` / `guest`).

### Change (belfry admins only)

| Request | Answer |
|---|---|
| `POST /roles` `{"label", "description"?, "default"?: bool}` | 201 `{"role", "roles", "default"}`; the id is a slug of the label (`host`, `host-2`) |
| `POST /roles/<id>` `{"label"?, "description"?, "default"?: bool}` | `{"role", "roles", "default"}`; `default: false` on the default puts `member` back |
| `POST /roles/<id>/delete` `{}` | `{"deleted", "moved": ["<hex>"], "roles", "default"}`: who had it gets the default role |
| `POST /member-roles` `{"pubkey", "role"}` | `{"pubkey", "role", "admins"}` |

`/member-roles` on a member sets the role (`admin` through `admins.change`; another role on an
admin first takes the admin role away, refused for the last one). On a guest who saved their
account (an e-mail on it), it makes them a member with that role, permanently: the same key, a
membership instead of their guest passes, still in their channels. Unsaved guest: 400
`guest_not_saved`; an agent: 400 `agent`.

Errors (`{"error", "message"}`; clients show fixed words per code, never the message): 403
`not_admin`, 403 `not_here`, 404 `unknown_role`, 422 `invalid` (label 1–40 characters,
description up to 200), 409 `taken` (same label), 400 `builtin`, 422 `admin_default`, 400
`not_member`, 400 `guest_not_saved`, 400 `agent`, 400 `last_admin`, 409 `too_many` (50), 409
`unsupported`.

## Member links

`POST /channel-invites` (docs/guests.md) takes, from a belfry admin only:

```json
{"channel": "general", "as": "member", "role": "host", "membership": "year",
 "people": 1, "link_days": 1}
```

- `role`: a role id (default: the default role). On a member link `role` is the member's role;
  on a guest link it still means a co-host (`"host"`, events).
- `membership`: `month` (30 days), `year` (365 days) or `permanent` (default).
- an `admin` link is for one person whatever `people` says.
- 403 `not_admin` for anyone else; 404 `unknown_role`; 422 `invalid`.

Answer: the usual fields plus `"as": "member"`, `"member_role"`, `"membership"` (`"days": 0`).
Guest links answer `"as": "guest"` and now take `"guest_days"` 1 | 7 | 30 (absent: 30).

`GET /channel-invites/<token>` adds `"as": "guest" | "member"` and, for a member link,
`"member_role"`, `"member_role_label"`, `"membership"`.

### Opening one

`POST /channel-invites/<token>/join` (NIP-98), as for guest links:

| Signed by | Result |
|---|---|
| a new key | guest mode, reading the channel: the usual answer plus `"as": "member", "email_needed": true` |
| a guest with a saved e-mail | a member at once: `{"channel", "guest": false, "member": true, "role", "as": "member"}` |
| a guest without an e-mail | the channel as a guest, `"email_needed": true`: saving the e-mail makes them a member |
| a member | the channel; **their role does not change** |
| an agent's key | 403 `agent` |

Joining from guest mode is the guest flow (`/guest/save` → `/guest/save/verify`); the verify
answer then says `{"saved": true, "joined": true, "member": true, "role": "<id>"}`. The member's
pass is of tier `member`, ends after the membership (or 2099-12-31), and the guest pass closes
(kind 1985 `belfry/guest` `member` in the channel; no 9001: they stay in it). If the membership
can't be opened the answer has `"member": false` and they stay a saved guest.

Memberships with an end close like guest passes: the service's hourly round (`core.expire`)
closes every active pass whose last day has passed, whatever its tier; closing a membership
takes the person (and their agents) out of their channels.
