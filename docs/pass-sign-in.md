# Signing in with the village pass (OpenID4VP to OIDC bridge)

Participants can sign in to Open Source Village by presenting their pass from an EUDI wallet
instead of using an e-mail link. Authentik, the login server, does not speak OpenID4VP (the
protocol wallets use to present credentials), so Belfry puts a small bridge in between: towards
Authentik it is an ordinary OpenID Connect provider, towards the wallet it is a verifier.

The bridge is part of the onboarding service (`services/onboarding/belfry_onboarding/bridge.py`,
routes under `/bridge/*`) and uses walt.id Verifier2 to check presentations.

## The flow

```
participant's browser        Authentik             bridge (/bridge)          Verifier2        wallet
        |  "Village pass" ------->|                        |                       |               |
        |<---- redirect ----------|  authorize (PKCE) ---->|                       |               |
        |------------------ GET /bridge/authorize -------->|-- create session ---->|               |
        |<------------- page with QR code -----------------|                       |               |
        |                                                  |                       |<-- presents ---|
        |-- polls GET /bridge/poll/<sid> ----------------->|-- session result ---->|               |
        |<------------- redirect with code ----------------|                       |               |
        |-------- code -------->|-- POST /bridge/token --->|  (client secret, PKCE)                |
        |                       |-- GET /bridge/userinfo ->|                                       |
        |<--- signed in --------|                          |                                       |
```

1. On the login page the participant picks **Village pass (EUDI wallet)**. Authentik's OAuth
   source `pass-bridge` sends the browser to `https://pass.<domain>/bridge/authorize` with PKCE.
2. The bridge opens a Verifier2 session that asks for the village pass (`member_number`, `npub`,
   `pass_tier`) and shows its request as a QR code (and as a link for a wallet on the same phone).
3. The wallet presents the pass to Verifier2. Verifier2 applies the policies `signature`,
   `expiration`, `not-before`, `allowed-issuer` and `credential-status` (the revocation status
   list, failing closed).
4. The page polls the bridge. Once Verifier2 reports success, the bridge additionally checks that
   the credential was signed by Belfry's own certificate (the first `x5c` entry must be Belfry's
   issuer certificate; Verifier2 alone would accept any issuer certificate the credential
   carries) and that a member number was disclosed. Then it redirects back to Authentik with a
   one-time code.
5. Authentik exchanges the code at `/bridge/token` (client secret and PKCE verifier) and reads
   `/bridge/userinfo` through the internal Compose network.

## What Authentik receives

| Claim | Value |
| --- | --- |
| `sub` | the member number, e.g. `OSV-0042` |
| `preferred_username` | the member number in lower case, which is the participant's Authentik username |
| `npub` | the participant's Nostr public key |
| `pass_tier` | `day`, `week` or `full` |

The source matches users by username (`username_link`): the first pass sign-in links the source to
the existing account with that username, later sign-ins use the stored link. The bridge never
creates accounts; a pass whose holder has no account cannot sign in. The source's property
mapping copies `sub` and `npub` into user attributes and never touches name or e-mail.

## Configuration

| Where | Setting |
| --- | --- |
| Authentik blueprint `infra/authentik/blueprints/belfry-pass-bridge-source.yaml` | the source `pass-bridge`, its property mapping, and the source button on the login page. Public authorize URL from `BELFRY_BRIDGE_PUBLIC_URL`, token and userinfo URLs from `BELFRY_BRIDGE_INTERNAL_URL` |
| onboarding service | `BRIDGE_CLIENT_ID` (`belfry-authentik`), `BRIDGE_CLIENT_SECRET` (shared with the blueprint), `BRIDGE_JWK_PATH` (signs the ID tokens), `VERIFIER_URL`, `ISSUER_X5C_PATH` (the pinned issuer certificate) |
| proxy | `/bridge/*` is public on `pass.<domain>` (rate limited); Verifier2 publishes only the request and response endpoints of a session |

## Limits

- Logins in progress, codes and tokens are kept in memory: one instance of the onboarding
  service; a restart only drops sign-ins that were under way.
- Authentik reads the user from `/userinfo` and does not verify the ID token's signature;
  `/bridge/jwks` publishes the key anyway.
- Real phone wallets need HTTPS with public certificates for the onboarding service and the
  verifier; locally only the scripted test wallet works
  (`python -m belfry_onboarding.testwallet`).

## Checks

The smoke test signs a participant in this way with the test wallet (`infra/scripts/smoke.sh`,
step 10). Background on the design choice: `docs/research/authentik-openid4vp.md` (written before
the bridge was built; it proposed patching the SUNET verifier, which turned out not to verify
presentations on its OIDC path, see `docs/research/sunet-verifier-spike.md`).
