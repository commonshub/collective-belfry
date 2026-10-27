# Pass certificates: issuer, verifier, status list

Every pass is an SD-JWT VC (`dc+sd-jwt`, `vct` `<public URL>/vct/village-pass/v1`) issued by walt.id **Issuer2 1.2.0** over OpenID4VCI with a pre-authorized code, signed with Belfry's P-256 key and an `x5c` chain (own CA + leaf). walt.id **Verifier2 1.2.0** checks presentations (OpenID4VP, DCQL, `direct_post`) for the door page and the pass bridge, both part of the onboarding service. Why walt.id: `docs/research/issuer-spike.md`.

## Keys and certificates

`scripts/gen-issuer-pki.sh` (run by `gen-secrets.sh`) writes, once:

| File (`infra/issuer/keys/`, gitignored) | Purpose |
| --- | --- |
| `ca.key.pem`, `ca.crt.pem` | Belfry Issuer CA, 10 years, P-256 |
| `issuer.key.pem`, `issuer.crt.pem` | the pass signer, 3 years, SAN `issuer.belfry.local` (`ISSUER_SAN`) |
| `issuer.jwk.json` | the same key as private JWK (`kid` = RFC 7638 thumbprint); signs the status list in the onboarding service |
| `issuer.x5c.json` | `[leaf, CA]` as base64 DER: the `x5c` header of credentials and status lists |
| `bridge.jwk.json` | signs the pass bridge's ID tokens |
| `verifier.key.pem`, `verifier.crt.pem`, `verifier.jwk.json`, `verifier.x5c.json` | Verifier2's request-signing key and certificate, SAN `verifier.belfry.local` (`VERIFIER_SAN`), rendered into `infra/verifier/config/verifier-service.conf` |
| `ci-token.jwk.json` | Issuer2's own access-token and nonce key |

and renders two config files with that material into `infra/issuer/config/` (also gitignored): `issuer2-profiles.conf` (profile `villagePass`) and `issuer-service.conf` (`ciTokenKey`). The committed files in `config/` are the rest of the Issuer2 configuration; `credential-issuer-metadata.conf` declares the `village_pass` configuration. **Rotating the issuer key invalidates every pass certificate signed with it**, unless the old certificate stays in the accepted list of the door and the bridge (below) until the last pass signed with it has expired.

The leaf is what matters to verifiers: Verifier2's `signature` policy takes the key from whatever `x5c` leaf the credential carries, so on its own it would accept a credential signed by anyone. The door and the bridge therefore **pin Belfry's leaf**: after a successful session they read the presented credential's `x5c[0]` and accept only Belfry's own certificate (`ISSUER_X5C_PATH`; a rotation appends the old leaf to the accepted list). Verifier2 accepts a status list only when the list's `x5c` leaf is the credential's leaf (or the list's `iss` equals the credential's `iss`). The CA is not published for wallets; wallets that insist on a trust anchor for the issuer do not accept the pass.

## What is where

| Piece | Where | Notes |
| --- | --- | --- |
| Credential offer | onboarding `GET /claim/<token>` (QR page), `GET /claim/<token>/offer` (JSON) | A fresh offer per page load, `OFFER_EXPIRY_SECONDS` (600); Issuer2 keeps offers in memory only. The token is `belfry.pass.claim_token`, in the activation mail (`belfry.public_url`). |
| Claims | `credentials.pass_credential_data` | `pass_tier` always visible; `display_name`, `member_number`, `npub` selectively disclosable; `nbf` = start of `date_start` (or now, if earlier), `exp` = end of `date_end`; explicit timestamps because Issuer2 rounds its generated ones |
| Status | `status.status_list = {uri: STATUS_LIST_URL, idx: pass id}`; `GET /status/1` serves `application/statuslist+jwt` (1 bit per pass, 1 = revoked or expired, derived from Odoo on every request), `ttl`/`max-age` = `STATUS_LIST_TTL` | the revocation delay at the door; 5 s locally, 300 s is a sensible production value |
| Type Metadata | `GET /vct/village-pass/v1` | the `vct` URL; Issuer2 only serves VCTM for its own `vct`s |
| Door | `GET /door` (QR page), `POST /door/sessions`, `GET /door/sessions/<id>` | asks for `pass_tier` only; policies `signature`, `expiration`, `not-before`, `allowed-issuer`, `credential-status` (fail closed) |
| Pass bridge | `GET /bridge/.well-known/openid-configuration`, `/bridge/authorize`, `/bridge/poll/<sid>`, `/bridge/token`, `/bridge/userinfo`, `/bridge/jwks` | sign-in with the pass for Authentik's `pass-bridge` source (`docs/pass-sign-in.md`) |
| Test wallet | `python -m belfry_onboarding.testwallet receive|present|metadata|show` | a scripted holder (fresh P-256 key, JWT proof, KB-JWT); `--map` rewrites hosts for use inside Compose |

## EUDI reference wallet: signed metadata and the registrar's certificates

The EUDI reference wallet (`eudi-app-android-wallet-ui`, wallet-core) reads issuer metadata only signed (`requireSignedMetadata()`), refuses a signed OpenID4VP request whose certificate it does not trust (`enforceIfPresent`), and, when the user turns on "registration check", wants a registration certificate from issuers and verifiers. Its trust anchors are the EU test trusted lists at `trustedlist.serviceproviders.eudiw.dev`; for credentials other than a PID an untrusted issuer is only a warning (`INFORM`).

- **Signed metadata, always.** The onboarding service's `/issuer-metadata` (the proxy routes both metadata paths of `issuer.<domain>` there) answers `Accept: application/jwt` with the metadata as a JWT (`typ` `openidvci-issuer-metadata+jwt`, ES256, `sub` = `iss` = the credential issuer, `iat`, `exp` one day, `x5c`), OpenID4VCI 1.0 §12.2.2; other clients still get JSON (`Vary: Accept`). Code: `belfry_onboarding/eudi.py`.
- **With the registrar's certificates.** Register Belfry at the EUDI relying-party registrar (`registry.serviceproviders.eudiw.dev`, or `itb.registry.serviceproviders.eudiw.dev` with the Launchpad guide `launchpad2026.md` of `eudi-srv-web-relyingparty-registration-py`): entitlements `Non_Q_EAA_Provider` and `Service_Provider`, provided attestation `dc+sd-jwt` with `vct_values` = the pass's `vct`, an intended use asking for `pass_tier` (and, for the pass sign-in, `member_number` and `npub`). That gives a WRPAC (`wrpac.p12`, access certificate with key, under a CA on the test list `WRPACProviders`) and a WRPRC (`wrprc.jwt`, registration certificate). Then:

  ```bash
  infra/scripts/eudi-registry-certs.sh wrpac.p12 wrprc.jwt [wrprc-issuer.jwt]
  docker compose up -d --force-recreate perms-init verifier onboarding
  ```

  The script writes `infra/issuer/keys/eudi/` (`wrpac.jwk.json`, `wrpac.x5c.json` without the self-signed root, the key and leaf as PEM, the registration certificates), shows the certificate's subject and the registration certificate's `sub`, entitlements and attestations (the wallet binds the two by the organisation identifier), and re-runs `gen-issuer-pki.sh`. From then on the metadata is signed with the WRPAC and carries the WRPRC as `issuer_info` (`[{format: registration_cert, data}]`, ETSI TS 119 472-3); Verifier2 signs the door's and the bridge's request objects with the WRPAC under the client id `x509_hash:<SHA-256 of the leaf>` (the WRPAC names no DNS host, so `x509_san_dns` cannot match), and the onboarding service adds the WRPRC to every session as `verifier_info`. `wrprc-issuer.jwt` is only needed when the issuer has its own registration certificate. Delete `keys/eudi/*`, run `gen-issuer-pki.sh` and recreate the same containers to go back to Belfry's own certificates.
- **What stays Belfry's.** The passes themselves are still signed with Belfry's issuer key and CA, and the door and the bridge still pin that leaf. The status list too.
- **Check.** `python -m belfry_onboarding.testwallet metadata <credential issuer>` fetches and verifies the signed metadata like the EUDI library (typ, signature with the `x5c` leaf, `sub`); `present` prints the request's `client_id` and `verifier_info`. Smoke steps 8 and 9 check both, for the registrar's certificate when `keys/eudi/` has one, else for Belfry's.
- **Test only.** The registrar's CAs are on the EU *test* lists; national production wallets trust their member states' lists, not these.

## URLs

Wallets talk to `ISSUER_PUBLIC_URL` (`credential_issuer` = `<that>/openid4vci`) and `VERIFIER_PUBLIC_URL`; browsers to `ONBOARDING_PUBLIC_URL`; the verifier fetches the status list at `STATUS_LIST_URL` (inside Compose the service name, in production the public URL). The onboarding service reaches the management APIs through the Compose network (`http://issuer:7005/issuer2/*`, `http://verifier:7004/verification-session/*`), which have **no authentication**: never publish `/issuer2/` or `/verification-session/create` on the internet.

## Known limits

- Phones: `http://localhost` works for the test wallet only. Real wallets need HTTPS with a public chain for the issuer, verifier and onboarding URLs. The EUDI reference wallet also needs signed issuer metadata and, to trust Belfry, the registrar's certificates (section above); it also authenticates itself with a wallet attestation and DPoP, which have not been tried against Issuer2.
- HAIP mandates the authorization code flow; the claim QR is the pre-authorized code flow (accepted by wallets, not by a strict conformance run).
- The verifier's requests are signed (`x509_san_dns:verifier.belfry.local`, request object JWT with the verifier's x5c; `x509_hash` with the registrar's access certificate, section above). walt.id refuses `localhost` as a DNS name, so locally the client id does not match the response URI's host; the test wallet does not check that, real wallets do. In production `VERIFIER_SAN` is the verifier's public hostname. Encrypted responses (`direct_post.jwt`) are not enabled.
- `exp` of a day pass is the end of its day (UTC), not the event's closing hour.
