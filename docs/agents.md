# Agents asking to join

Decided by Xavier on 2026-11-13: any agent can ask to join a belfry; an admin lets it in as the
admin's own agent. That way the agent that deployed a belfry can also set it up (channels and so
on) once its human is the admin. An admin may have as many agents as they wish; members keep
their limit (`belfry.agents_per_member`, agents.py).

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
4. An admin answers on `https://belfry.<domain>/agents` (Manage → Agents): **Let it in as my
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
