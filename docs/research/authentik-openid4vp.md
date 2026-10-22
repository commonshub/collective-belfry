# OpenID4VP sign-in for Authentik (2026-10-22)

Goal: participants sign in to Authentik by presenting their `village-pass` SD-JWT VC from an EUDI wallet. Findings from a research pass on 2026-10-22; claims not individually re-verified.

> Verified later the same day: the Authentik side holds (`authentik-spike.md`), but the SUNET verifier's OIDC login path performs no credential verification and `sub` can only be a salted hash (`sunet-verifier-spike.md`). Item 3 of the recommended design below is therefore not available without patching the verifier, and item 4's `sub = member_number` needs the hash mapping or a patch. "Enrollment: deny" means `enrollment_flow: null`; the passkey condition on linking cannot be enforced.

## Status in Authentik

- No native OpenID4VP / SD-JWT VC support, nothing in progress.
- Only related request: https://github.com/goauthentik/authentik/issues/2344 ("eID webauth support", open since 2022). On 2026-10-14 the author of `polaris-oid4vp` (Apache-2.0 Python OpenID4VP 1.0 + HAIP verifier, https://pypi.org/project/polaris-oid4vp/) offered to build an Authentik source; no maintainer reply yet.
- No stage plugin system (https://github.com/goauthentik/authentik/issues/1541): a custom stage means maintaining an Authentik fork.
- Redirect stage can't resume the original flow. Source stage (pause flow, go to a source, resume) is Enterprise-only: https://docs.goauthentik.io/add-secure-apps/flows-stages/stages/source/

## Options

| Approach | Verdict |
| --- | --- |
| Custom flow stage | Fork of Authentik + web UI component; high ongoing effort |
| External verifier exposed as OIDC, added as an Authentik OAuth source | Recommended; config-only in Authentik |
| Browser Digital Credentials API | Extra for same-device inside the verifier; Chrome 141+ with OpenID4VP, Safari mdoc only |

## Verifier candidates

| Candidate | OIDC built in | Notes | Licence |
| --- | --- | --- | --- |
| SUNET vc verifier (Go) https://pkg.go.dev/github.com/SUNET/vc/docs/verifier | Yes | dc+sd-jwt, DCQL, status check, DC API; HAIP support unconfirmed — test first | BSD-2 |
| EUDI reference verifier https://github.com/eu-digital-identity-wallet/eudi-srv-verifier-endpoint | No (add thin OIDC layer) | VP 1.0, DCQL, HAIP, direct_post.jwt; README says not for production | Apache-2.0 |
| walt.id OIDC Bridge | Yes | Enterprise Stack only | Commercial |
| Keycloak 26.8 OID4VP IdP | Yes (as broker) | Experimental | Apache-2.0 |
| acapy-vc-authn-oidc | Yes | AnonCreds/DIDComm, not OpenID4VP | Apache-2.0 |

## Recommended design

1. Bridge: SUNET vc verifier; fallback EUDI verifier endpoint or `polaris-oid4vp` wrapped in OIDC (`/authorize` shows QR/deep link, `/token`, `/jwks`, `.well-known`).
2. Request: DCQL query on `vct = https://belfry.example/vct/village-pass/v1`, disclose only `member_number` (+ `npub` if needed); HAIP, `direct_post.jwt`, `x509_san_dns` client ID.
3. Checks: issuer signature pinned to Belfry's key; KB-JWT against `cnf` with nonce and audience; Token Status List, fail closed.
4. ID token: `sub = member_number` (immutable, never reassigned); `npub` as extra claim.
5. Authentik: OAuth source of type OpenID Connect at the bridge's well-known URL, PKCE S256, confidential client.
   - User matching: users link the pass once while signed in with a passkey; then mode `identifier`.
   - Enrollment flow: deny, so an unknown pass can't create an account.
   - Add the source to the identification stage's "Selected Sources".
6. Device modes: QR + polling (cross-device), `openid4vp://` deep link (same-device), optional DC API.
7. Later: second DCQL query for PID prefill; requires relying-party registration per country.

Estimated effort: 1–2 weeks for bridge + Authentik config.
