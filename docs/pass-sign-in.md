# Signing in with the village pass

Participants can sign in to Open Source Village by presenting their pass from an EUDI wallet
instead of using the e-mail link or code. The login page (`pass.<domain>/login`) offers **Sign in
with your pass**; the onboarding service asks the wallet for the pass through walt.id Verifier2
(OpenID4VP) and, once the pass is verified, signs the browser in to Belfry's session, the same
session the e-mail sign-in gives. Code: `services/onboarding/belfry_onboarding/passlogin.py`,
routes `/login/pass*`.

The same service is also a small OpenID provider for one client, walt.id Issuer2, whose
authorization-code credential offers need a sign-in (below, "Issuer2's sign-in").

Until 2026-11-19 both went through Authentik: the pass sign-in was an OpenID4VP-to-OIDC bridge
(`bridge.py`, `/bridge/*`) behind Authentik's OAuth source `pass-bridge`, and Issuer2 signed
participants in at Authentik's application `belfry-issuer`. Authentik is gone; the pass sign-in
is direct and Issuer2's sign-in is the onboarding service's own (`oidc.py`, `/oidc/*`).

## The flow

```
participant's browser          onboarding service (pass.<domain>)        Verifier2        wallet
        |-- GET /login/pass?next=… ------->|-- create session ---------------->|               |
        |<-- page with QR code, cookie ----|                                    |               |
        |                                  |                                    |<-- presents --|
        |-- polls GET /login/pass/poll/<sid> ->|-- session result ------------->|               |
        |<-- {status: done, redirect} -----|                                    |               |
        |-- GET /login/pass/done/<sid> --->|  (checks the cookie, asks Odoo)    |               |
        |<-- session cookie, redirect to next                                   |               |
```

1. On the login page the participant picks **Sign in with your pass**. `GET /login/pass` opens a
   Verifier2 session that asks for the village pass (`member_number`, `npub`, `pass_tier`) and
   shows its request as a QR code (and as a link for a wallet on the same phone). A cookie
   (`belfry_pass_login`) binds the attempt to this browser.
2. The wallet presents the pass to Verifier2. Verifier2 applies the policies `signature`,
   `expiration`, `not-before`, `allowed-issuer` and `credential-status` (the revocation status
   list, failing closed).
3. The page polls `/login/pass/poll/<sid>`. Once Verifier2 reports success, the onboarding
   service additionally checks that the credential was signed by Belfry's own certificate (the
   first `x5c` entry must be Belfry's issuer certificate; Verifier2 alone would accept any issuer
   certificate the credential carries) and that a member number was disclosed.
4. The page goes to `/login/pass/done/<sid>`. Only the browser that started the attempt (its
   cookie) can finish it, once. The service finds the Odoo contact with that member number and
   asks Odoo whether it may sign in (`belfry.login.code.belfry_may_sign_in`: an active pass, or a
   steward). Then it sets the session cookie and redirects to `next`.

The pass sign-in never creates anything: a pass whose member number has no contact in Odoo, or
whose holder has no active pass any more, cannot sign in. From the session on, everything works
as after the e-mail sign-in: the account page, the apps' "Sign in with Open Source Village"
(`/api/oauth/authorize`), the pass on the phone, the steward pages.

## Issuer2's sign-in

Wallets that do not take pre-authorized codes (DIDroom, HAIP wallets) get an authorization-code
offer from the claim page (`/claim/<token>/offer?signin=1`, with `issuer_state`). Issuer2 then
sends the browser to its external login, which is the onboarding service
(`infra/issuer/config/authentication-service.conf`, rendered from `.tmpl` by
`gen-issuer-pki.sh`):

| Endpoint | What |
| --- | --- |
| `GET /oidc/.well-known/openid-configuration`, `/oidc/jwks` | discovery and the ID tokens' public key |
| `GET /oidc/authorize` | needs Belfry's session (else the login page first, e-mail or pass), checks the client and that `redirect_uri` is Issuer2's, issues a code |
| `POST /oidc/token` | client secret (basic or post) and PKCE (S256); an ES256 ID token signed with `bridge.jwk.json` |
| `GET /oidc/userinfo` | the same claims |

The claims are `sub` and `member_number` (the member number), `name`, `email` and `npub`. Issuer2
takes the credential's data from the offer (`issuer_state`), not from these claims: the sign-in
only proves that the wallet's holder is a participant. Requests without `issuer_state` never get
here; the onboarding service refuses them in front of Issuer2.

## Configuration

| Where | Setting |
| --- | --- |
| onboarding service | `VERIFIER_URL`, `ISSUER_X5C_PATH` (the pinned issuer certificate), `SESSION_SECRET` (the session cookie); for Issuer2's sign-in `ISSUER_OIDC_CLIENT_ID` (`belfry-issuer`), `ISSUER_OIDC_CLIENT_SECRET` (shared with Issuer2's config), `BRIDGE_JWK_PATH` (signs the ID tokens; the file kept the bridge's name) |
| Issuer2 | `authentication-service.conf`: authorize URL `<ONBOARDING_PUBLIC_URL>/oidc/authorize`, token URL `http://onboarding:8100/oidc/token` over the Compose network |
| proxy | `/login/pass*` and `/oidc/*` are public on `pass.<domain>` (rate limited); Verifier2 publishes only the request and response endpoints of a session |

## Limits

- Sign-ins in progress and Issuer2's codes and tokens are kept in memory: one instance of the
  onboarding service; a restart only drops sign-ins that were under way.
- Issuer2 reads the ID token without verifying its signature; `/oidc/jwks` publishes the key
  anyway.
- Real phone wallets need HTTPS with public certificates for the onboarding service and the
  verifier; locally only the scripted test wallet works
  (`python -m belfry_onboarding.testwallet`).

## Checks

The smoke test signs a participant in with the pass from the test wallet (`infra/scripts/smoke.sh`,
step 10: QR page, presentation, poll, another browser refused, the session opens `/me`) and runs
Issuer2's sign-in with the participant's session (step 8b: the hop through `/oidc/authorize`, a
credential through the sign-in offer). Background on the original design choice:
`docs/research/authentik-openid4vp.md` (written for Authentik; it proposed patching the SUNET
verifier, which turned out not to verify presentations on its OIDC path, see
`docs/research/sunet-verifier-spike.md`).
