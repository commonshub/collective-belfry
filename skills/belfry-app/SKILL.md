---
name: belfry-app
description: Let people sign in to your app (web, mobile, bot or agent) with their belfry address, name@community.org, without a password or a key of their own. The app makes a session key, asks the community's belfry by Nostr DM (or HTTPS), the person types the code they got by e-mail, and the app gets a NIP-46 connection that signs with the person's own key.
---

# Sign in with a belfry address

Every member of a belfry has an address like `xavier@belfry.sh`: a Nostr NIP-05 name whose key
the belfry's signer (Keycast) holds for them. Your app never sees that key. It gets a **session**:
a NIP-46 connection on which the person's key signs what your app asks it to.

## 1. Find the belfry

From the address's domain: `GET https://<domain>/.well-known/belfry.json`. You need
`sign_in.sessions`:

```json
"sessions": {
  "pubkey": "<belfry key, hex>", "npub": "npub1…",
  "relays": ["wss://bunker.<domain>"],
  "api": "https://pass.<domain>/api/session"
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
works; add a `client` tag with your app's name, which the e-mail shows:

```
This newly created npub claims to be xavier@belfry.sh, please verify.
tags: [["client", "Your app"]]
```

Listen on the same relays for gift wraps to your session key. The belfry answers:

- `["belfry", "code_sent"]`: "A code is on its way to x•••@example.org…". Ask the person for the
  6-digit code from that mail (10 minutes, five tries).
- `["belfry", "error"], ["error", "<code>"]`: `unknown` (no such member), `too_many`,
  `wrong_code`, `expired`, `own_key` (the member holds their own key: use their own signer).

Send the code back in the same conversation (a message with the six digits). The belfry answers
`["belfry", "open"]`, `["nip05", "xavier@belfry.sh"]` and `["bunker", "bunker://…"]`.

## 3b. Or ask over HTTPS

The same two steps, each request NIP-98 signed with the session key (`Authorization: Nostr …`,
with a `payload` tag for the body):

```
POST <api>          {"nip05": "xavier@belfry.sh", "app": "Your app"}  → {"state": "code_sent", "to": "x•••@…"}
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
