# Village notes

A small app on a Belfry, built from the visitor skill
(`https://pass.<domain>/skills/open-source-village.md`):

- **Invite**: paste an invite link or code (or open `/?invite=<token>`). The app reads the offer,
  orders the chosen pass and shows the payment link. By default it creates the participant's key
  itself (12 words, HKDF per the skill's section 8) and sends `nostr_pubkey` + `nostr_proof` with
  the order. Once the payment is in and `/api/me` says `member`, you're signed in. If you untick
  that box, Belfry keeps the key and you sign in later with a bunker URL.
- **Sign in**: a `bunker://` URL from the account page (Connect to Nostr App, permissions
  "Belfry event apps"), or a NIP-07 browser extension.
- **Tokens**: the pass's tokens that no wallet has collected yet (`/wallet/pass-tokens`), plus
  nutzaps to the key that are still unspent at the mint. What the wallet app already holds stays
  there, because its seed is derived through Keycast for the wallet only.
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
