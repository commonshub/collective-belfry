# Village notes

A small app on a Belfry, built from the visitor skill
(`https://pass.<domain>/skills/open-source-village.md`):

- **Invite**: paste an invite link or code (or open `/?invite=<token>`). The app reads the offer,
  orders the chosen pass and shows the payment link. By default it creates the participant's key
  itself (12 words, HKDF per the skill's section 8) and sends `nostr_pubkey` + `nostr_proof` with
  the order. Once the payment is in and `/api/me` says `member`, you're signed in. If you untick
  that box, Belfry keeps the key and you sign in later with a bunker URL.
- **Sign in**: "Sign in with Open Source Village" (OAuth code + PKCE against Keycast, client
  `village-notes`, back at `/oauth/callback`): the Belfry's sign-in page asks for the pass's
  e-mail address and mails a 6-digit code and a link; after Keycast's consent screen the token
  response's `bunker://` URL ("Belfry event apps") signs the app in. Or a `bunker://` URL from the
  account page (Connect to Nostr App, permissions "Belfry event apps"), or a NIP-07 browser
  extension.
- **Tokens**: the pass's tokens that no wallet has collected yet (`/wallet/pass-tokens`), plus
  nutzaps to the key that are still unspent at the mint, plus what the pass brought and the wallet
  already collected (`tokens` in `/api/me`). The wallet's own balance stays there, because its
  seed is derived through Keycast for the wallet only.
- **Sign out**: after "Sign in with Open Source Village" it revokes the app's authorization and
  ends the sign-in everywhere (Keycast's logout), so the next sign-in asks for the code again.
- **Invites**: a pass holder makes an invite code to pass on (or has Belfry mail an invite to an
  address), copies the code or a link (`/?invite=<code>` into this app, or the shop's), follows
  their invites and revokes open ones (`/api/invites`, NIP-98; skill section 10).
- **Notes**: the latest kind-1 notes on the community relay (NIP-42 AUTH), and a box to post one.

```
npm install
BELFRY_DOMAIN=belfry.opensourcevillage.org PORT=8080 npm start
```

`server.mjs` serves the page and passes the Belfry's JSON APIs through `/proxy`, because they
refuse other origins unless the Belfry lists them in `API_CORS_ORIGINS`. Relays and the mint are
reached directly from the browser.

The session (bunker URL, or the 12 words for a key held by this app) is kept in the browser's
localStorage. That's fine for a demo but not for a shared computer.
