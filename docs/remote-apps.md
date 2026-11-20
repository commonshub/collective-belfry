# Remote apps: signing in to another Nostr app with a Belfry account

A member opens a third-party Nostr app (for example gitworkshop.dev → "Remote Signer"). The
app shows a QR code of a `nostrconnect://` address (NIP-46, started by the client): the app's
own key, a few public relays, a secret. The member scans it with the Belfry phone app, which
calls the API below. From then on **the belfry's onboarding service is that app's remote
signer**, on the app's relays, with the member's key, which the signer (Keycast) holds.

Keycast is not changed. Its own NIP-46 signer listens on the belfry's bunker relay only and
never sends the acknowledgement a `nostrconnect://` app waits for, so it cannot serve these
apps. The onboarding service does the NIP-46 part and asks Keycast for each operation over
`POST /api/nostr` with the member's token (`clients/keycast.py`: `sign_event`,
`nip44_encrypt`, `nip44_decrypt`, `nip04_encrypt`, `nip04_decrypt`).

Code: `services/onboarding/belfry_onboarding/remoteapps.py`; rows in the own register
(`register.py`, table `remote_apps`); tests `tests/test_remote_apps.py`.

## Who may

Every call is NIP-98, signed by **the account's own key** (what the phone app's session signs
with), on `https://belfry.<domain>/api/v1/remote-apps…` (the same routes answer at
`/api/remote-apps…` on the pass host).

| Caller | Answer |
| --- | --- |
| a member or an admin (an active pass that is not a guest's or an agent's) | served |
| a guest | 403 `guest` |
| an agent | 403 `agent` |
| a key that is nobody's account here, a website session key included | 403 `no_pass` |
| a member who holds their own key (preview and connect only) | 409 `own_key` |
| a belfry on Odoo's register | 409 `unsupported` |
| the signer not answering (preview and connect) | 503 `signer` |

## The API

### `POST /remote-apps/preview` `{"uri": "nostrconnect://…"}`

Nothing is stored, no relay is contacted.

```json
{"name": "Git Workshop", "url": "https://gitworkshop.dev", "host": "gitworkshop.dev",
 "image": "https://gitworkshop.dev/icon.png",
 "asks": {"sign": true, "decrypt": false}, "known": false}
```

- `asks.decrypt` is true when the URI's `perms` name `nip44_decrypt` or `nip04_decrypt`, or
  when it has no `perms` at all (the app may then ask anything).
- `known`: this client key is connected to this account already.

How the URI is read (`parse_uri`). Anything else is 422 `invalid_uri` with a `reason`:

| Part | Rule | `reason` |
| --- | --- | --- |
| scheme | `nostrconnect://`, the whole URI at most 4096 characters | `bad_scheme` |
| client key | 64 hex characters that are a public key | `bad_key` |
| `relay` | at least one | `no_relay` |
| `relay` | each `wss://host[:port][/path]`: no `ws://`, no IP literal, no `localhost`, no single-label name (the belfry's internal services), no `.local` / `.internal` / `.lan` / `.home` / `.corp` / `.arpa` / `.localhost`, no user, query or fragment; at most 8 after duplicates are dropped | `bad_relay` |
| `secret` | 1 to 128 characters | `no_secret` |

`name` (at most 60 characters, one line), `url` (https, a public host name, at most 200;
`host` is its host name) and `image` (https, at most 300) are null when they do not fit: never
an error. `perms` is a comma list of `method` or `method:param`; entries that do not read that
way are dropped (what is not understood is not granted). An empty `perms` counts as none.

### `POST /remote-apps` `{"uri": "…", "decrypt": true|false}` → 201 `{"app": {…}}`

1. The service starts listening for the app on the URI's relays (kind 24133 is ephemeral:
   relays do not keep it, so listening comes first).
2. It sends the NIP-46 acknowledgement: a kind 24133 event **signed as the member** (the
   remote-signer pubkey is the member's own pubkey), `p` = the client key, content =
   `{"id": <random>, "result": "<the URI's secret>"}` NIP-44-encrypted to the client key,
   to every relay of the URI.
3. When at least one relay accepts it within 8 seconds the connection is stored and the
   answer is 201. Otherwise nothing is stored: 502 `unreachable`.

`decrypt` is the member's answer to "may this app read my private messages?". It is kept as
true only when the app could ask for it at all (`asks.decrypt`); default false.

Errors: those of preview; 422 `invalid` (`decrypt` not a boolean); 409 `key_taken` (this
client key is, or once was, connected to **another** account: revoked connections count);
409 `limit` (10 live apps per account); 429 `too_many` (10 connects an hour per account,
counted in memory); 502 `unreachable`; 503 `signer`; 503 `relays_full` (see Limits).

Connecting the same client key again for the same account replaces the old row (new `id`).

The secret is used for the acknowledgement and then forgotten: never stored, never logged.

### `GET /remote-apps` → `{"apps": [ … ]}`

The live connections, the newest first:

```json
{"id": "…", "name": "Git Workshop", "url": "https://gitworkshop.dev",
 "host": "gitworkshop.dev", "image": null, "decrypt": false,
 "created": 1795000000, "used": 1795000300, "expires": 1797592300}
```

No keys and no relays. `used` is null until the app's first request and is written at most
once a minute. `expires` = `used` (or `created`) + 30 days.

### `POST /remote-apps/<id>/disconnect` → `{"ok": true}`

Idempotent for the owner; 404 `not_found` for someone else's or an unknown id. The next
request of the app is ignored at once.

### What else ends a connection

- 30 days without a request.
- The account's last member pass closes (`core._close_pass` → `remoteapps.closed`): all its
  connections are revoked. Someone who is left with only a guest pass loses them too.
- The row stays in the register (revoked) so the client key can never become another
  account's.

`GET /api/me/sessions` is unchanged: these connections are **not** listed there (the phone
apps decode that list strictly; a new kind of row could break released builds). Use
`GET /remote-apps`.

## The signer loop (`Hub`)

Started with the service (`remoteapps.running`, own register only), rebuilt from the register
at start and every 30 seconds, and at once after a connect or disconnect.

- One websocket per distinct relay over all live connections, with one subscription:
  `{"kinds": [24133], "#p": [members with a connection on that relay], "since": now - 10}`.
  A change sends a new REQ on the same socket. A relay that is down or refuses is tried again
  with backoff (1 s to 5 min) and logged once per relay per hour (its host name only).
- Before connecting, the relay's name is looked up; an address that is not a public one
  (private, loopback, link-local…) is refused and the socket goes to the address checked.
- A relay that asks for NIP-42 before it serves or accepts (CLOSED / OK `auth-required`) gets
  an AUTH signed as up to five of the members listened for there, once per connection.
- A request counts only when: kind 24133; exactly one `p` tag, a member's key; its author is
  the client key of a live connection of that member; it came from one of that connection's
  relays; id and signature are right; `created_at` within 2 minutes; not seen before (the last
  2000 event ids). The connection is read again from the register for every request, so a
  disconnect or a closed pass takes effect immediately.
- Content is decrypted with the member's key: NIP-04 when it carries `?iv=`, else NIP-44. The
  answer uses the same scheme, is `{"id", "result"}` or `{"id", "error"}`, kind 24133 signed
  as the member with `p` = the client key, and goes to the relay the request came from, only.
- The member's signer token is asked per member at most every 5 minutes. A signer failure
  answers `The signer isn't answering` when an answer can still be encrypted, and never stops
  the loop.

| Method | Result |
| --- | --- |
| `connect` | the secret the request carries in `params[1]`, else `"ack"` |
| `ping` | `"pong"` |
| `get_public_key` | the member's pubkey |
| `get_relays` | `"{}"` |
| `sign_event` (`params[0]` = the event as JSON) | the signed event as JSON, after the rules below; a template with someone else's `pubkey` is `Invalid params` |
| `nip44_encrypt`, `nip04_encrypt` (`[peer pubkey, text]`) | the ciphertext |
| `nip44_decrypt`, `nip04_decrypt` | the text; `Could not decrypt` when the signer cannot |
| anything else | error `Unsupported method` |

## What an app may ask (`allowed`, one pure function)

1. `connect`, `ping`, `get_public_key`, `get_relays`: always.
2. If the URI had `perms`, the request must be covered: `sign_event` alone covers every kind,
   `sign_event:<kind>` that kind, a method's name that method. Perms that named nothing
   readable cover nothing.
3. `nip44_decrypt` / `nip04_decrypt`: only when the connection's `decrypt` is true (and the
   perms, if any, name it).
4. **Always refused**, whatever the perms say:
   - kind 27235 (NIP-98) whose `u` tag is the belfry's domain **or any host under it**, or one
     of the hosts in its settings, or that has no readable `u` host. (Stricter than "belfry.,
     pass., apps., chat. and the apex": the groups relay's management API also takes NIP-98.)
   - kind 22242 (NIP-42 AUTH) whose `relay` tag is the community relay, the groups relay or
     the bunker relay (from the settings, plus `relay.`, `groups-relay.`, `bunker.<domain>`),
     or that has no `relay` tag. Other relays, other subdomains included, are allowed.
   - kind 24133.
   - kinds 9000–9009, 9021 and 9022 (NIP-29 moderation and joins).
   - kind 24242 (Blossom) whose `server` tag is the belfry's Blossom host (`BLOSSOM_PUBLIC_URL`,
     `BLOSSOM_HOSTS`, `blossom.<domain>`) or that has no `server` tag.
   - kinds 0 and 3 unless the perms name `sign_event:0` / `sign_event:3` explicitly.
5. A refusal answers `{"id", "error": "Not allowed"}`.

Why: with rule 4 an outside app can post as the member on public relays, but cannot call the
belfry's API as them, cannot authenticate to the members-only relays, cannot moderate or join
groups, and cannot upload to or delete from the belfry's storage.

## Limits

| What | Limit |
| --- | --- |
| live apps per account | 10 (409 `limit`) |
| connects per account | 10 an hour (429 `too_many`; in memory, reset by a restart) |
| relays per URI | 8 |
| distinct relays over all connections | 30. A new connection uses those of its relays already in use plus as many new ones as fit; when none remains: 503 `relays_full` |
| requests per connection | 120 a minute, then error `Too many requests`; beyond 240 a minute requests are dropped unread (the signer is not asked to decrypt them) |
| idle | 30 days |

## Failure modes

- **The onboarding service restarts**: the app's requests during the restart are lost (kind
  24133 is not stored by relays); the app waits or times out and asks again. Connections are
  rebuilt from the register at start.
- **A public relay refuses kind 24133**, wants payment, or rate-limits: the acknowledgement
  needs one relay of the URI to accept; later answers are sent without waiting for the
  relay's OK, so a refusal shows as the app timing out.
- **The signer is down**: connect is 503 `signer`; requests get an error when one can be
  encrypted, else nothing.
- **The member moves to their own key**: the signer has no token for them any more; requests
  fail and the connection lapses after 30 days (it is not revoked at the move).
- **The rate counters, the seen ids and the token cache are in memory**: one onboarding
  process is assumed.

## Not verified

- Nothing ran against a real Keycast: that `POST /api/nostr` takes `nip44_encrypt`,
  `nip44_decrypt`, `nip04_encrypt`, `nip04_decrypt` with params `[pubkey, text]` and answers
  `{"result": "<string>"}`, that the member's token from `preload-user` may call them, that it
  stays valid for 5 minutes, and that the signer's own policy lets every kind an app asks for
  through, are assumptions (the fake signer in the tests implements them with real NIP-44 v2
  and NIP-04).
- Nothing ran against a real relay or a real app (gitworkshop.dev or any other): whether such
  apps accept the member's own pubkey as the remote-signer pubkey, whether they send `connect`
  after the acknowledgement, and which relays accept kind 24133 from an unknown key.
- The public-address check of relay names was tested with a faked lookup only.
- The phone apps do not have the screens yet.
