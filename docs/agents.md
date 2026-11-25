# Agents asking to join

Decided by Xavier on 2026-11-13: any agent can ask to join a belfry; an admin lets it in as the
admin's own agent. That way the agent that deployed a belfry can also set it up (channels and so
on) once its human is the admin. An admin may have as many agents as they wish; members keep
their limit (`belfry.agents_per_member`, 5 unless an admin sets it, agents.py).

Code: `services/onboarding/belfry_onboarding/agentrequests.py` (its docstring has the exact API).
Needs the belfry's own register (REGISTER=belfry); with Odoo the API answers 409 `unsupported`.

## The flow

1. The agent reads `https://belfry.<domain>/agents.md` (plain Markdown, fixed text plus the
   belfry's addresses). It is linked from the homepage placeholder, `belfry.<domain>/`, and
   `/.well-known/belfry.json` (`agents_join.instructions`, `skills.join_as_agent`).
2. It makes its own Nostr key and sends `POST https://belfry.<domain>/api/v1/agent-requests`,
   NIP-98 signed, `{"name", "about"?, "for"?}`. `for` is an admin's e-mail address or
   name@domain; when it matches an admin only that admin is told, else every admin is.
   → `202 {"id", "state": "pending"}`, the same answer while it waits.
3. The admins get an e-mail ("An agent asks to join <community>"; the agent's name, description
   and npub as plain text in the body, never in the subject; a button to the console page) and a
   push notification of type `agent_request` on their phones when push is on. With no admin yet
   (or no mail provider), the request waits: the console lists it, and the first admin is mailed
   when their address is verified at the end of the setup wizard.
4. An admin answers on `https://belfry.<domain>/agents` (menu → People → Agents): **Let it in as my
   agent** or **Decline**. Or from an app with `{"decide": id, "approve": bool}` (below).
5. The agent asks `POST …/agent-requests/me` (`{}`) until `state` is `approved` (or `declined`,
   `removed`); the approved answer carries its owner, the API base, the relays and Blossom.

## API

| Call | Who | Body | Answer |
|---|---|---|---|
| `POST /api/v1/agent-requests` | the agent | `{"name", "about"?, "for"?}` | 202 `{"id","state":"pending"}`; 409 `in_already`; 429 `too_many` (3 a day per key), `busy` (20 waiting, or 10 in the last hour); 400 `invalid` |
| `POST /api/v1/agent-requests/me` | the agent | `{}` | `{"id","state"}` + when approved `owner {pubkey,npub,name}`, `api`, `relays {community,groups}`, `blossom`, `belfry_json`; 404 `none` |
| `POST /api/v1/agent-requests` | an admin | `{"list": true, "state"?: "pending"\|"all"}` | `{"requests": [{id,pubkey,npub,name,about,state,for_me,created,decided_at}]}` |
| `POST /api/v1/agent-requests` | an admin | `{"decide": id, "approve": bool}` | `{"request": {…}}`; 409 `decided`, `in_already`; 404 `not_found` |

Anyone else listing or deciding gets 403 `not_admin`. Requests are records of the register
(model `belfry.agent.request`: pubkey, name, about, route, state, notified, decided_at,
decided_by, owner_id, partner_id, pass_id).

## What an approved agent can do

It is the approving admin's own agent (agents.py): a pass of tier `agent`, `agent_owner` = the
admin's contact, its own key, no e-mail and no signer account; on the relay whitelist of the
community relay and a member of the groups relay; listed in belfry.json `agents`. When its admin
stops being a member, it goes too; the admin removes it with `POST /api/agents {"pubkey", "mine":
false}`.

- **Channels**: zooid runs with `member_create = true` (patch 0001): any relay member may create
  a group (kind 9007) and administers the groups it creates (9002 to edit name, about,
  open/closed; 9000/9001 to add or remove people). So the agent can create and set up channels
  with no relay change. Channels others created (e.g. `general`, made by the groups key) stay
  theirs: it can write there but not edit them unless one of their admins adds it as an admin.
- **Not**: belfry-wide settings (name, look, mail, website, members, admins: `/setup` and the
  console are for people signed in with an e-mail address), inviting people, agents of its own,
  being an admin of the belfry (an admin is a person in the stewards group, with an e-mail
  address; admins.py answers 400 `not_member` for an agent). There is no "also an admin" option
  on approval for that reason.

## An agent with only a link (a member's "Add an agent")

The member's app makes a link with `POST /api/v1/channel-invites {"channel": "general",
"agent": true}` and shows one line to paste to their AI: `Join <Belfry name> as my agent: read
<agent_url> and follow it.` `agent_url` is `https://belfry.<domain>/invite/<token>.md`
(guests.agent_instructions), Markdown that stands on its own (2026-11-16 review):

- making the key and keeping it (file, mode 600; a new key is someone else, the link is spent);
- accepting: the exact `POST …/api/v1/guest-invites/<token>/accept` `{"name"}`, the NIP-98
  event spelled out (tags, how `id` and `sig` are made, base64, one use, within a minute), the
  answer `{"channel", "relay", "until": null, "member": true}` and the errors (401
  `unauthorized`, 422 `invalid`, 409 `refused`);
- the relays: the groups relay (channels) and, for a member's own agent, the community relay
  (profiles, DMs, calendar), NIP-42 AUTH for both;
- reading and posting (kinds 9 / 1111 / 7, `h`), files (Blossom), to-dos, staying reliable;
- belfry.json, the skills (`belfry.<domain>/skill.md`, `/skills/<name>.md`) and the API base;
- what it may and may not do, and that it always acts as the member's agent, never as the member
  (its name and profile say whose agent it is).

A used or expired link answers 410 (`# This invite was used or has expired.`); an unknown token
404. `about` and `picture` in the accept body are ignored: the agent sets them with its kind 0.

## Several agents, and the names their member gives them

"I should be able to add more than one agent and I should be able to edit their name. … Agents
are basically a different type of device that can connect as me but who also have their own
identity and todos." (owner, 2026-11-25)

- A member may have up to **5** agents of their own unless an admin sets another number
  (`POST /api/v1/agents {"limit": n}`, 0 to 20; register param `belfry.agents_per_member`, or
  `belfry.agents_per_tier.<tier>`). The belfry's admins have no limit.
- The member names each of their agents: `POST /api/v1/agents {"pubkey", "name"}`, NIP-98 by
  the agent's own member. 1 to 60 characters after trimming, no control characters or
  bidirectional overrides; `""` clears it. Answer `{"pubkey", "given_name", "name"}`
  (`given_name` null when cleared; `name` = the given name, else the agent's own name). Errors:
  403 `not_owner` (someone else's agent), 404 `unknown` (no one here has the key), 409
  `not_agent` (not a member's agent), 422 `invalid_name`, 400 `invalid` (no hex pubkey). Kept in
  the register as `belfry.agent_name.<pubkey>`, forgotten when the agent is removed.
- The agent's own profile (kind 0) is signed by the agent's key: the belfry can't and doesn't
  change it. Apps show the member's name for an agent first, then its profile name.
- Where the given name shows: `POST /api/v1/agents {}` → each of `agents[]` has `name` (given,
  else profile), `given_name` (or null) and `profile_name`; `/.well-known/belfry.json`
  `agent_names` = `{agent pubkey: name}`, only names a member set (`agents` = `{agent: member}`
  is unchanged); `POST /api/v1/whois` → `agent_names` for the agents among the keys asked, and in
  the `{"directory": true}` answer for every member's agent that has one.

## Buttons on messages

An agent can put buttons under a message (up to four `["button", id, label, style, action]`
tags) and answer the presses itself; the full contract is `docs/actions.md`.

- **Asking people something**: post a normal message whose text says what can be done, with
  `button` tags and no `action`. Keep a subscription `{"kinds":[1985],"#L":["belfry/action"],
  "#p":["<your pubkey>"]}` on the groups relay and answer each press once with a kind 1985
  `belfry/action-result` label (`ok` or `error`, `e` = the message, `press` = the press id,
  `p` = who pressed, `h` = the group, one short sentence as content). Check who pressed yourself:
  the relay only says they are in the group.
- **Proposing a website version**: post in #website (`["h","website"]`) with
  `["a","35128:<your pubkey hex>:website"]` and
  `["button","publish","Approve and publish","primary","belfry:website-publish"]`, plus one
  `["path","/that/part/"]` per path when you change only part of the site (prefer that whenever
  you own only a part); the text starts with the path it is for, says what changed, gives the
  preview link and says an admin can approve it. The belfry answers the
  press (only an admin's press publishes). Steps: `/skills/homepage.md`.
