---
name: belfry-app
description: Let people open a session in your app (web, mobile, bot or agent) with their own belfry account, name@community.org, without a password or a key of their own. A web app sends them to the belfry's sign-in page (OAuth code + PKCE) and gets a NIP-46 connection back; other apps ask by Nostr DM or HTTPS with a session key. Only apps an admin of that belfry approved get a session.
---

# Sign in with a belfry address

Every member of a belfry has an address like `xavier@belfry.sh`: a Nostr NIP-05 name whose key
the belfry's signer (Keycast) holds for them. Your app never sees that key. The person opens a
**session** in your app with their own account: a NIP-46 connection on which their key signs what
your app asks it to.

## 0. Your app needs an admin's approval

A belfry opens sessions only for apps its admins approved. An app is known by its **address**: the
return address of its sign-in (`redirect_uri`, without query or fragment), or the `app` address it
gives over DM/HTTPS. Until it is approved, a member who signs in sends the admins a request
instead of getting a session, and an admin who signs in is offered to approve it. Requests,
approvals and withdrawals are Nostr events on the community relay, signed by the member or admin
(kinds 8550, 8551, 8552; tags `["r", <app address>]`, `["client", <name>]`, `["p", <belfry key>]`),
so anyone can follow them on `belfry.<domain>/monitor`. Admins answer at `belfry.<domain>/apps`.
Never show a name your app gives itself as proof of anything: the belfry shows your address.

## 1. Web apps: the sign-in page (OAuth 2.0 code + PKCE)

Send the person to the belfry's sign-in page (`sign_in.connect.authorize` in belfry.json):

```
GET https://belfry.<domain>/connect/authorize?client_id=<your app>&redirect_uri=<your page>
    &state=<random>&code_challenge=<S256 of verifier>&code_challenge_method=S256[&prompt=none]
```

They type their e-mail address and a 6-digit code (or tap "Continue" if their browser is already
signed in to the belfry). Back at `redirect_uri?code=…&state=…`, exchange the code:

```
POST https://belfry.<domain>/connect/token
{"grant_type": "authorization_code", "code", "client_id", "redirect_uri", "code_verifier"}
→ {"bunker_url": "bunker://…", "pubkey": "<the person's key>", "npub", "token_type": "bunker"}
```

Then connect to the bunker URL with a fresh client key (step 4). `prompt=none` signs in silently
when the browser is known and the app approved, else returns `error=login_required`. Sign out:
forget the bunker URL, and `GET https://belfry.<domain>/connect/logout?return_to=<your page>` to
end the belfry sign-in too. The rest of this skill is for apps that can't redirect (bots, agents,
native apps).

## 1. Find the belfry

From the address's domain: `GET https://<domain>/.well-known/belfry.json`. You need
`sign_in.sessions`:

```json
"sessions": {
  "pubkey": "<belfry key, hex>", "npub": "npub1…",
  "relays": ["wss://bunker.<domain>"],
  "api": "https://belfry.<domain>/api/v1/session"
}
```

(`https://<domain>/.well-known/nostr.json?name=<name>` tells you whether the name exists.)

## 2. Make a session key

A fresh key pair for this sign-in on this device. Keep its secret where your app keeps secrets
(the browser's storage, the phone's keychain). It is not the person's key and is worth nothing
without the session the belfry opens for it.

## 3. Ask, by DM (NIP-17)

From the session key, send the belfry's key a private message (kind 14, sealed and gift-wrapped,
kind 1059; `nip17.wrapEvent` in nostr-tools) on the `relays` above. Any text naming the address
works; give your app's address as JSON (admins approve apps by address):

```
{"nip05": "xavier@belfry.sh", "app": "https://your.app/"}
```

Listen on the same relays for gift wraps to your session key. The belfry answers:

- `["belfry", "code_sent"]`: "A code is on its way to x•••@example.org…". Ask the person for the
  6-digit code from that mail (10 minutes, five tries).
- `["belfry", "error"], ["error", "<code>"]`: `unknown` (no such member), `app_address` (no
  https address for your app), `too_many`, `wrong_code`, `expired`, `own_key` (the member holds
  their own key: use their own signer), `app_not_approved` (after the right code: your app isn't
  approved yet; a request went to the admins).

Send the code back in the same conversation (a message with the six digits). The belfry answers
`["belfry", "open"]`, `["nip05", "xavier@belfry.sh"]` and `["bunker", "bunker://…"]`.

## 3b. Or ask over HTTPS

The same two steps, each request NIP-98 signed with the session key (`Authorization: Nostr …`,
with a `payload` tag for the body):

```
POST <api>          {"nip05": "xavier@belfry.sh", "app": "https://your.app/"}  → {"state": "code_sent", "to": "x•••@…"}
POST <api>/verify   {"code": "123456"}                                → {"state": "open", "bunker_url", "npub", "nip05"}
```

Errors are `{"error", "message"}` with the codes above.

## 4. Use the session

Connect to the `bunker://` URL with your session key as the NIP-46 client (nostr-tools
`BunkerSigner.fromBunker(sessionSecret, parseBunkerInput(url))`, then `connect()`). The URL's
secret binds your session key on first use; nobody else can use it. From then on
`get_public_key` returns the person's key and `sign_event` signs with it: what your app publishes
is theirs, under `xavier@belfry.sh`.

Store the bunker URL with the session key. Sign out = forget both; the person can also end the
session from their account on the belfry.

## Rules

- Never ask for or handle the person's own key or a password.
- One session key per device and sign-in; don't reuse it across people.
- The members-only relays and Blossom server of the belfry accept events signed by the person's
  key (through the session), not by the session key itself.
