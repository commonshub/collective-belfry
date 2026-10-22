# Issuer spike: SD-JWT VC village pass with walt.id, Credo and DIDroom

Date: 2026-10-22. Half-day spike for Phase 3 of the plan ("EUDI pass and membership certificates").
Goal: issue a test `dc+sd-jwt` village pass locally with at least one stack and compare walt.id, Credo and DIDroom against the plan's requirements.

Result in one line: walt.id Issuer2 v1.2.0 issued and walt.id Verifier2 verified a `village-pass/v1` SD-JWT VC end to end (pre-authorized code, P-256 holder binding, selective disclosure, `status.status_list` claim, optional `x5c`). Credo covers the same ground as a library but needs an app around it. DIDroom's issuer is missing most of the plan's requirements and has been idle since November 2025.

## Versions tested

| Component | Version |
| --- | --- |
| walt.id `waltid-identity` repo | tag `v1.2.0` (release 2026-10-15); configs identical to `main` @ `00917b6` (2026-10-21) |
| Docker images | `waltid/issuer-api2:1.2.0` (digest `sha256:4138ca61…c15f2`, same as `:stable`), `waltid/verifier-api2:1.2.0` |
| Holder / presenter | hand-written Node 24.15.0 scripts (WebCrypto, no wallet library) |
| Docker | 29.9.1, Compose v5.6.0 |

Credo and DIDroom were assessed from docs and source only (no code run).

## 1. walt.id: what was run

Work dir: `/tmp/claude-1000/-home-friedger--repos-github-commonshub-collective-belfry/25a6ddf2-0cae-48ea-9d4d-b384572f5352/scratchpad/spikes/issuer/` (`waltid-deploy/` compose + config, `scripts/holder.mjs`, `scripts/present.mjs`, `out/` transcripts).

### Setup

The repo's `docker-compose/` starts the whole stack behind Caddy with ports outside 7000–7999 (Postgres 5432, pgAdmin 8080). A minimal compose file was used instead: only `issuer-api2` (port 7005) and `verifier-api2` (host network, port 7004), configs copied from `docker-compose/issuer-api2/config` and `docker-compose/verifier-api2/config`, `.env` with `SERVICE_HOST=localhost`, `ISSUER_API2_PORT=7005`, `VERIFIER_API2_PORT=7004`, `VERSION_TAG=1.2.0`.

Village pass configuration, appended to the stock files (HOCON merges):

```hocon
# credential-issuer-metadata.conf
credentialConfigurations."village_pass" = {
  format = "dc+sd-jwt"
  scope = "village_pass"
  vct = "https://belfry.example/vct/village-pass/v1"   # custom VCT, published unchanged
  credential_signing_alg_values_supported = ["ES256"]
  cryptographic_binding_methods_supported = ["jwk"]
  proof_types_supported = { jwt = { proof_signing_alg_values_supported = ["ES256"] } }
  credential_metadata = {
    display = [{ name = "Open Source Village Pass", locale = "en", background_color = "#1F2A44", text_color = "#FFFFFF" }]
    claims = [
      { path = ["pass_tier"],    display = [{ name = "Pass tier", locale = "en" }] }
      { path = ["display_name"], display = [{ name = "Display name", locale = "en" }] }
      { path = ["npub"],         display = [{ name = "Nostr public key", locale = "en" }] }
    ]
  }
}

# issuer2-profiles.conf
profiles.villagePassSdJwt = {
  name = "Village Pass"
  credentialConfigurationId = "village_pass"
  issuerKey = ${defaultIssuerKey}            # stock P-256 test key (type = "jwk")
  credentialData = { "pass_tier" = "week", "display_name" = "Test Participant", "npub" = "npub1placeholder" }
  mapping = { iat = "<timestamp-seconds>", nbf = "<timestamp-seconds>", exp = "<timestamp-in-seconds:7d>" }
  selectiveDisclosure = {
    "fields" = { "display_name" = { "sd" = true }, "npub" = { "sd" = true }, "pass_tier" = { "sd" = false } }
    "decoyMode" = "NONE", "decoys" = 0
  }
}
```

```bash
docker compose up -d issuer-api2                      # ready after ~25 s
docker compose --profile verifier up -d verifier-api2
curl http://localhost:7005/.well-known/openid-credential-issuer/openid4vci   # village_pass listed, format dc+sd-jwt
curl http://localhost:7005/.well-known/jwt-vc-issuer/openid4vci
# {"issuer":"http://localhost:7005/openid4vci","jwks_uri":"http://localhost:7005/openid4vci/jwks"}
curl http://localhost:7005/openid4vci/jwks
# {"keys":[{"kty":"EC","crv":"P-256","x":"G0RINBiF-oQUD3d5DGnegQuXenI29JDaMGoMvioKRBM","y":"ed3eFGs2pEtrp7vAZ7BLcbrUtpKkYWAT2JPUQK4lN4E","kid":"9vuaJyUxRx4KmHyoZ9kjJxMs_mjpnnf-mPM9nPMG51A"}]}
```

### Issuance flow (exact requests)

1. Create the offer (management API, per participant; the Odoo webhook would call this):

```http
POST http://localhost:7005/issuer2/credential-offers
Content-Type: application/json

{"profileId":"villagePassSdJwt","authMethod":"PRE_AUTHORIZED","valueMode":"BY_VALUE",
 "runtimeOverrides":{
   "credentialData":{"pass_tier":"week","display_name":"Alice Example","npub":"npub1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq"},
   "credentialStatus":{"status_list":{"idx":412,"uri":"https://belfry.example/status/1"}}}}
```

Response (the `credentialOffer` value is the QR payload):

```json
{"offerId":"5ff16146-3527-477f-9950-eff533101f74","profileId":"villagePassSdJwt","authMethod":"PRE_AUTHORIZED","expiresAt":1792691350720,
 "credentialOffer":"openid-credential-offer://?credential_offer=%7B%22credential_issuer%22%3A%22http%3A%2F%2Flocalhost%3A7005%2Fopenid4vci%22%2C%22credential_configuration_ids%22%3A%5B%22village_pass%22%5D%2C%22grants%22%3A%7B%22urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Apre-authorized_code%22%3A%7B%22pre-authorized_code%22%3A%22VsCgghX0aSNvi8HtI68qpJH5eGfLns5WszN12JcAfmJW%22%7D%7D%7D"}
```

2. Holder: `node scripts/holder.mjs "<credentialOffer>" belfry-spike-wallet` does:

```http
GET  /.well-known/openid-credential-issuer/openid4vci
GET  /.well-known/oauth-authorization-server/openid4vci

POST /openid4vci/token
Content-Type: application/x-www-form-urlencoded
grant_type=urn:ietf:params:oauth:grant-type:pre-authorized_code&pre-authorized_code=VsCgghX0aSNvi8HtI68qpJH5eGfLns5WszN12JcAfmJW&client_id=belfry-spike-wallet
-> 200 {"token_type":"bearer","access_token":"eyJ…","expires_in":287,"refresh_token":"eyJ…"}

POST /openid4vci/nonce
-> 200 {"c_nonce":"eyJ…"}            # the nonce is itself a signed JWT (iss, aud=credential endpoint, exp 300 s)

POST /openid4vci/credential
Authorization: Bearer <access_token>
Content-Type: application/json
{"credential_configuration_id":"village_pass","proofs":{"jwt":["<proof JWT>"]}}
-> 200 {"credentials":[{"credential":"<SD-JWT VC>"}]}
```

Proof JWT: header `{"typ":"openid4vci-proof+jwt","alg":"ES256","jwk":{P-256 public key}}`, payload `{"aud":"http://localhost:7005/openid4vci","iat":1792691062,"nonce":"<c_nonce>","iss":"belfry-spike-wallet"}`.

Without `client_id` the token endpoint answers `401 {"error":"invalid_client","error_description":"Anonymous pre-authorized code access is not supported"}`; metadata advertises `pre-authorized_grant_anonymous_access_supported: false`. Anonymous access is enabled with `clientAuthenticationConfig.supportedMethods = [{type = "preauth-anonymous"}]` in `issuer-service.conf` (commented example in the stock file; not tested). The EUDI reference wallet sends `client_id` (`eudiw-abca`), so the default is fine for it.

### The issued credential (test data, decoded)

Compact form (issuer JWT `~` disclosure `~` disclosure `~`):

```
eyJhbGciOiJFUzI1NiIsImtpZCI6Ijl2dWFKeVV4Ung0S21IeW9aOWtqSnhNc19tanBubmYtbVBNOW5QTUc1MUEiLCJ0eXAiOiJkYytzZC1qd3QifQ.eyJwYXNzX3RpZXIiOiJ3ZWVrIiwic3RhdHVzIjp7InN0YXR1c19saXN0Ijp7ImlkeCI6NDEyLCJ1cmkiOiJodHRwczovL2JlbGZyeS5leGFtcGxlL3N0YXR1cy8xIn19LCJleHAiOjE3OTMyOTMyMDAsIm5iZiI6MTc5MjYyNzIwMCwiaWF0IjoxNzkyNjI3MjAwLCJfc2RfYWxnIjoic2hhLTI1NiIsImlzcyI6Imh0dHA6Ly9sb2NhbGhvc3Q6NzAwNS9vcGVuaWQ0dmNpIiwiY25mIjp7Imp3ayI6eyJrdHkiOiJFQyIsIngiOiJmQU9WbjFMOEh3RHBzcWVxdjNkNHhnMmtLcjN6U0NMZWxUYVBEMUEwZmpZIiwieSI6InhoWWJyRW5xRHgzdm1LZDlUZkQxNU53dFpKSFhKRE8yUVlHaG1GZHRzTjAiLCJjcnYiOiJQLTI1NiIsImtpZCI6IkRPUzJEbFhwV1ptRmZmbUFqenpMVXNkbUQ0RnlyUHJHQXdMdlFhNTRKdDQifX0sInZjdCI6Imh0dHBzOi8vYmVsZnJ5LmV4YW1wbGUvdmN0L3ZpbGxhZ2UtcGFzcy92MSIsIl9zZCI6WyIxS1RLQXhxcUhiYVM4clBRZTBkRkUxdzlGV0JzQ0p2NWhhUDNfbU5nc3VBIiwibEtDUEY1aUdiVHZ0eU5lR1pFdTllaXp6NXktQUlDdlkxMXZGbGZkRkZ2cyJdfQ.bkP8TctYo-79SSyo0kudxkSagob2pykW3JEw_FVWBdlf9kr7WcSUA03jwVggxA8W31dOk4BS2iruzQG9yPJI_w~WyJROXFoWk0wR0N3Ynl3cEpDWDNjbzJ3IiwiZGlzcGxheV9uYW1lIiwiQWxpY2UgRXhhbXBsZSJd~WyJnOWpiT3ZrM01Nc2I5MnRpUGZoTGR3IiwibnB1YiIsIm5wdWIxcXFxcXFxcXFxcXFxcXFxcXFxcXFxcXFxcXFxcXFxcXFxcXFxcXFxcXFxcXFxcXFxcXFxcXFxcXFxcXFxcSJd~
```

Header: `{"alg":"ES256","kid":"9vuaJyUxRx4KmHyoZ9kjJxMs_mjpnnf-mPM9nPMG51A","typ":"dc+sd-jwt"}`

Payload:

```json
{
  "iss": "http://localhost:7005/openid4vci",
  "vct": "https://belfry.example/vct/village-pass/v1",
  "iat": 1792627200, "nbf": 1792627200, "exp": 1793293200,
  "pass_tier": "week",
  "status": { "status_list": { "idx": 412, "uri": "https://belfry.example/status/1" } },
  "cnf": { "jwk": { "kty": "EC", "crv": "P-256", "x": "fAOVn1L8HwDpsqeqv3d4xg2kKr3zSCLelTaPD1A0fjY", "y": "xhYbrEnqDx3vmKd9TfD15NwtZJHXJDO2QYGhmFdtsN0", "kid": "DOS2DlXpWZmFffmAjzzLUsdmD4FyrPrGAwLvQa54Jt4" } },
  "_sd_alg": "sha-256",
  "_sd": [ "1KTKAxqqHbaS8rPQe0dFE1w9FWBsCJv5haP3_mNgsuA", "lKCPF5iGbTvtyNeGZEu9eizz5y-AICvY11vFlfdFFvs" ]
}
```

Disclosures: `["Q9qhZM0GCwbywpJCX3co2w","display_name","Alice Example"]` (digest `lKCP…FFvs`) and `["g9jbOvk3MMsb92tiPfhLdw","npub","npub1qqq…qqq"]` (digest `1KTK…suA`); both digests are in `_sd`. The holder script verified the issuer signature against `jwks_uri` (`valid: true`). This matches the plan's claim layout (`iss`, `vct`, `iat/nbf/exp`, `pass_tier` always visible, `cnf.jwk` holder binding, `status.status_list`, `_sd` for the personal claims). Holder private key (test only): `d = lQ89UNZ-1fX7OJEKZvLXG_HgLz55DOCKE8pEgqE5ZF8`.

Observation: the issuance happened at 17:44 UTC but `iat`/`nbf` are `1792627200` (2026-10-22T00:00:00Z) and `exp` is `1793293200` (2026-10-29T17:00:00Z), so the mapping functions appear to truncate `iat/nbf` to the day and `exp` to the hour. Not investigated (unverified why); harmless for a day/week pass but worth knowing for day-pass `nbf`.

### x5c variant

Same offer with `runtimeOverrides.x5Chain = ["-----BEGIN CERTIFICATE-----…"]` (the stock test leaf certificate from `issuer2-profiles.conf`, `defaultIssuerX5chain`) yields header `{"alg":"ES256","kid":"9vua…","typ":"dc+sd-jwt","x5c":["MIICgTCC…"]}` and an otherwise identical payload (`pass_tier: "day"`, `idx: 413`). Transcript: `out/holder-run-x5c.log`.

### Verification with walt.id Verifier2

```http
POST http://localhost:7004/verification-session/create
{"flow_type":"cross_device",
 "core_flow":{"dcql_query":{"credentials":[{"id":"village_pass","format":"dc+sd-jwt",
     "meta":{"vct_values":["https://belfry.example/vct/village-pass/v1"]},
     "claims":[{"path":["pass_tier"]},{"path":["npub"]}]}]},
   "policies":{"vp_policies":{"dc+sd-jwt":["dc+sd-jwt/audience-check","dc+sd-jwt/kb-jwt_signature","dc+sd-jwt/nonce-check","dc+sd-jwt/sd_hash-check"]}}},
 "redirects":{"success_redirect_uri":"https://example.com/success","error_redirect_uri":"https://example.com/not-successful"}}
-> {"sessionId":"…","bootstrapAuthorizationRequestUrl":"openid4vp://authorize?client_id=redirect_uri%3A…&request_uri=…","fullAuthorizationRequestUrl":"…"}
```

`node scripts/present.mjs out/holder-run-x5c.log http://localhost:7004/verification-session/<id>/request npub` fetches the request (`response_mode: direct_post`, `client_id: redirect_uri:…`, `nonce`, `dcql_query`), builds `<issuer JWT>~<npub disclosure>~<KB-JWT>` with `kb+jwt` (`aud`, `nonce`, `sd_hash`) and posts `vp_token={"village_pass":["…"]}&state=…`.

- x5c-signed credential: `200 {"redirect_uri":"https://example.com/success?response_code=…"}`; session `SUCCESSFUL`; all four `dc+sd-jwt/*` presentation policies and the `signature` VC policy passed; `presented_credentials` contains only the `npub` disclosure (display_name stayed hidden).
- Credential without x5c (issuer identified only by `iss` + `/.well-known/jwt-vc-issuer`): `400 "Credential policy verification failed. Failed VC policies: signature"`, reason `Could not resolve issuer key`. Cause: walt.id's key resolver only tries `.well-known/jwt-vc-issuer` when `iss` starts with `https://` (`Crypto2JwtKeyResolver.kt` line 72); the spike issuer ran on `http://localhost`. It would work behind HTTPS; not re-tested.
- The `signature` policy accepted the x5c leaf without any trust anchor configured; chain trust is a separate policy (`x5c`, `etsi-trust-list`, `allowed-issuer`). Not tested.

Teardown: `docker compose --profile verifier down -v`.

### walt.id findings beyond the flow

- Custom `vct`: any string is published unchanged in metadata and credential, but `GET /.well-known/vct/{type}` serves Type Metadata only for self-hosted VCTs of the form `<baseUrl>/<configurationId>` (`vct = "vctBaseUrl"`); for `https://belfry.example/vct/village-pass/v1` the request returns 404 and Belfry must host the VCTM document on belfry.example itself (which is what SD-JWT VC expects anyway). Type metadata content is configured per configuration in `sdJwtVcTypeMetadataConfiguration` ([MetadataService.kt](https://github.com/walt-id/waltid-identity/blob/v1.2.0/waltid-services/waltid-issuer-api2/src/main/kotlin/id/walt/issuer2/service/openid4vci/MetadataService.kt)).
- Status list: OSS Issuer2 embeds the caller-supplied `status` object verbatim ("Status allocation and updates remain the caller's responsibility", `OpenId4VciProtocolService.kt` ~line 1205). The `waltid-status-list` library has a `TokenStatusListCodec` (bit packing, zlib) but no service creates, signs, hosts or updates status list tokens in the community stack; that is the enterprise "Credential Status Service" ([docs.walt.id/enterprise-stack](https://docs.walt.id/enterprise-stack)). Verifier2 does have a `credential-status` policy for checking.
- Keys: Issuer2 accepts the legacy walt.id key wrapper (`type = jwk | tse | aws | azure | oci-rest-api | aws-rest-api | azure-rest-api`; the service links `waltid-crypto-aws` and `waltid-crypto-azure`; `tse` = HashiCorp Vault Transit, example in `issuer2-profiles.conf`) and crypto2 `StoredKey` sidecars. A PKCS#11 provider exists (`waltid-crypto2-pkcs11`, JVM, SoftHSMv2 and TPM2 tested, P-256 ECDSA) but OSS Issuer2 instantiates `CryptoRuntime(defaultSoftwareKeyProviders())` only, so PKCS#11 is not wired in without a code change (unverified by running). None of the KMS paths were exercised in the spike.
- HAIP: no explicit HAIP conformance statement in the Issuer2/Verifier2 READMEs or docs; the profiles config has `defaultHaipIssuerKey`/`defaultHaipIssuerX5chain` fields and v1.0.0 notes say "HAIP metadata is passed through". Signed issuer metadata (`signedMetadata.signingMethod` = `static-jwk` | `x509-chain` | `key-reference`), PAR, DPoP, key attestation and wallet/client attestation are implemented in OSS ([issuer-api2 README](https://github.com/walt-id/waltid-identity/blob/v1.2.0/waltid-services/waltid-issuer-api2/README.md)).
- Security note: `GET /issuer2/profiles/{id}` returns the profile including the private key `d` of a `jwk` issuer key. The management API (`/issuer2/*`) has no auth in OSS and must stay internal.
- Persistence: in-memory by default, Redis optional; a restart loses open offers/sessions.
- Licence and activity: Apache-2.0; 319 stars; releases v1.0.0 (2026-08-24), v1.1.0 (09-29), v1.1.1 (10-01), v1.2.0 (10-15); about 600 commits since 2026-07-22, the most recent 100 almost all by one author (Peter Szij) ([releases](https://github.com/walt-id/waltid-identity/releases)). The enterprise stack (tenants, KMS service, credential status service, X.509 service, DID registry) requires a licence from walt.id; pricing is not public.

## 2. Comparison

Legend: supported / partial / missing / unverified. "Tested" means exercised in this spike; everything else is from docs and source.

| Requirement | walt.id Issuer2/Verifier2 v1.2.0 (community) | Credo 0.7.2 | DIDroom microservices |
| --- | --- | --- | --- |
| HAIP stated | partial: no conformance claim; HAIP fields in profiles config, signed metadata, PAR, DPoP, key/wallet attestation present | partial: tracking issue [#2476](https://github.com/openwallet-foundation/credo-ts/issues/2476) open (DPoP nonce, signed metadata, status-list gaps); `iss` check "as required by HAIP" in 0.6.2 | missing: only "based on EUDI-ARF" marketing ([product page](https://forkbomb.solutions/solution/didroom/)) |
| `dc+sd-jwt` via OpenID4VCI, pre-authorized code | supported, tested (OpenID4VCI 1.0) | supported: `SdJwtDc = "dc+sd-jwt"`, OpenID4VCI 1.0 with draft 11–15 compat ([format enum](https://raw.githubusercontent.com/openwallet-foundation/credo-ts/main/packages/openid4vc/src/shared/models/OpenId4VciCredentialFormatProfile.ts)) | partial: `dc+sd-jwt` yes, but only `authorization_code` + PAR + PKCE, no pre-authorized grant ([AS metadata](https://raw.githubusercontent.com/ForkbombEu/DIDroom_microservices/main/public/authz_server/.well-known/oauth-authorization-server)) |
| Custom `vct` + Type Metadata | partial, tested: custom `vct` issued; VCTM served only for self-hosted VCTs | partial: `vct` free string; fetches/validates VCTM, no API to create or host it ([SdJwtVcApi.ts](https://raw.githubusercontent.com/openwallet-foundation/credo-ts/main/packages/core/src/modules/sd-jwt-vc/SdJwtVcApi.ts)) | missing: no `vct` in the issuer Zencode ([credential_4_sd_jwt.zencode](https://raw.githubusercontent.com/ForkbombEu/DIDroom_microservices/main/credential_issuer/credential_4_sd_jwt.zencode)) |
| `/.well-known/jwt-vc-issuer` | supported, tested (`/.well-known/jwt-vc-issuer/openid4vci` + `jwks_uri`); verifier resolves it only over HTTPS | missing: issuer module serves only `openid-credential-issuer` and `oauth-authorization-server`; issuer identified by DID or x5c ([OpenId4VcIssuerModule.ts](https://raw.githubusercontent.com/openwallet-foundation/credo-ts/main/packages/openid4vc/src/openid4vc-issuer/OpenId4VcIssuerModule.ts)) | missing: only `openid-credential-issuer` with inline `jwks` |
| `x5c` issuer chain | supported, tested (profile `x5Chain` or runtime override) | supported: `SdJwtVcIssuerX5c`, cert/CSR creation ([SdJwtVcOptions.ts](https://raw.githubusercontent.com/openwallet-foundation/credo-ts/main/packages/core/src/modules/sd-jwt-vc/SdJwtVcOptions.ts)) | missing: header is `alg`+`typ` only ([crypto_sd_jwt.lua](https://raw.githubusercontent.com/dyne/Zenroom/master/src/lua/crypto_sd_jwt.lua)) |
| Token Status List: embed claim | supported, tested (`credentialStatus` override → `status.status_list`) | partial: integrator adds `status` to payload (`sign` passes payload through) | missing |
| Token Status List: create/sign/host/revoke | missing in community (codec library only; enterprise Credential Status Service) | partial: `TokenStatusListModule` creates/updates/signs JWT or CWT lists (0.7.1); hosting is the integrator's ([TokenStatusListOptions.ts](https://raw.githubusercontent.com/openwallet-foundation/credo-ts/main/packages/core/src/modules/token-status-list/TokenStatusListOptions.ts)) | missing (Fork Bomb's [capture-status-list](https://github.com/ForkbombEu/capture-status-list) is a test stub) |
| External HSM/KMS key | partial: Vault Transit (`tse`), AWS, Azure, OCI key types; PKCS#11 library exists but not wired into OSS Issuer2; none tested | partial: `KeyManagementService` interface with Node, Askar and React-Native secure-element backends; no PKCS#11/AWS/GCP backend shipped ([kms interface](https://raw.githubusercontent.com/openwallet-foundation/credo-ts/main/packages/core/src/modules/kms/KeyManagementService.ts)) | missing: Zenroom keyring file (`secrets.keys`) |
| Licence / paid parts | Apache-2.0; enterprise stack (tenants, KMS service, status service, X.509 service, DID registry) licensed | Apache-2.0, nothing gated; Paradym is Animo's separate hosted product ([pricing](https://paradym.id/pricing)) | AGPL-3.0 microservices, dashboard, wallet; dashboard SaaS Free/Pro €99/mo/Enterprise ([pricing](https://didroom.com/guides/Pricing/pricing.html)) |
| Maintenance | v1.2.0 2026-10-15; ~600 commits in 3 months, dominated by one author | v0.7.2 2026-10-01; ~130–140 commits in 3 months; 5 listed maintainers ([MAINTAINERS.md](https://github.com/openwallet-foundation/credo-ts/blob/main/MAINTAINERS.md)); upstream `oid4vc-ts`/`sd-jwt-js` merged into [identity-common-ts](https://github.com/openwallet-foundation-labs/identity-common-ts) Sept 2026 | issuer repo last push 2025-11-28, no tags/releases ever; wallet v2.1.2 2026-06-08; Fork Bomb forked credo-ts in Oct 2026 ([org](https://github.com/ForkbombEu)) |
| Ready-to-run server | supported: Docker images, config files, Swagger | missing: library; integrator writes an Express app and `credentialRequestToCredentialMapper`; samples `demo-openid/`, [animo/openid4vc-playground](https://github.com/animo/openid4vc-playground) | supported: `ghcr.io/forkbombeu/didroom_microservices`, compose zip from dashboard |
| Verifier for the door | supported, tested (Verifier2, DCQL, direct_post, policies) | supported (verifier module, `direct_post.jwt` default), not tested | partial: verifier microservice, last push 2025-02 |
| EUDI reference wallet interop evidence | unverified: EUDI PID/EHIC/CoR profiles and key attestation shipped, no published test report | partial: verifier test vector from `verifier-backend.eudiw.dev` ([PR #2681](https://github.com/openwallet-foundation/credo-ts/pull/2681)); Animo marketing claims national-wallet tests | unverified |

## 3. EUDI wallet testing

Without a phone (today):

| Holder | Platform | Fit |
| --- | --- | --- |
| walt.id Wallet API v2 (`wallet-api2`, port 7006 in the compose file) | REST service, no UI | Receives `dc+sd-jwt` via `POST /wallet/{id}/credentials/receive {offerUrl}`; default `clientId` `eudiw-abca` ([docs](https://docs.walt.id/community-stack/wallet2/credential-receiving/sd-jwt-vc)). Needs Postgres (port 5432 in the stock compose). Not run in this spike |
| walt.id legacy web wallet (`identity-old` profile, wallet.demo.walt.id) | Browser | SD-JWT VC via OID4VCI stated; `dc+sd-jwt`/1.0 support unverified |
| wwWallet ([demo.wwwallet.org](https://demo.wwwallet.org), [repo](https://github.com/wwWallet/wallet-frontend)) | Browser PWA, passkey-held keys | Companion issuer is OpenID4VCI 1.0 with `dc+sd-jwt` and pre-authorized code; VCT registry config. Best browser candidate; `jwk` binding unverified |
| Sphereon web-wallet | Browser + Node agent | Alpha; OID4VCI draft 15; SD-JWT support unverified; Sphereon calls the TS SDK legacy |
| OIDF conformance suite | Hosted | Tests an issuer under HAIP, `dc+sd-jwt`; authorization code only (HAIP mandates it); your issuer must be reachable from the internet ([OIDF](https://openid.net/certification/conformance-testing-for-openid-for-verifiable-credential-issuance/)) |
| Hand-written holder (this spike) | Node | Enough for CI and regression tests of the issuer |

Phone only: EUDI reference wallet, Paradym, Lissi, Procivis One, DIDroom wallet.

To test against the EUDI reference wallet ([Android](https://github.com/eu-digital-identity-wallet/eudi-app-android-wallet-ui), latest `2026.10.43-Demo` APK on the releases page; iOS must be built with Xcode):

- Issuer reachable over HTTPS with a valid chain; cleartext is off in release builds, a debug-only CA for `10.0.2.2` (emulator host) exists for local dev ([CONFIGURATION.md](https://raw.githubusercontent.com/eu-digital-identity-wallet/eudi-app-android-wallet-ui/main/wiki/CONFIGURATION.md)).
- Add Belfry to `issuersConfig` in `WalletCoreConfigImpl.kt` of a custom build; the stock dev/demo builds only know `issuer.eudiw.dev`.
- Issuer trust: wallet-core defaults to `RequireSigned` issuer metadata with a trusted certificate chain ([eudi-lib-android-wallet-core](https://github.com/eu-digital-identity-wallet/eudi-lib-android-wallet-core)); for non-PID credentials the policy is `INFORM` (continues with a warning). A private issuer needs signed metadata + x5c and either an entry in the wallet's trusted certificates (`trustedCertificates(R.raw.…)`) or a relaxed policy in a custom build. Whether the wallet accepts an SD-JWT VC issuer identified only by `/.well-known/jwt-vc-issuer` is unverified (the JVM SD-JWT lib has `UsingX5cOrIssuerMetadata`, but which mode wallet-core uses at issuance is not documented).
- Format and flow: OpenID4VCI 1.0, `dc+sd-jwt`, pre-authorized code supported, JWT proof without key attestation accepted ([eudi-lib-jvm-openid4vci-kt v0.15.0](https://github.com/eu-digital-identity-wallet/eudi-lib-jvm-openid4vci-kt/releases)); the wallet sends attestation-based client auth or acts as a public client with `client_id`.
- Revocation: wallet-core resolves Token Status Lists (JWT and CWT); status-list trust policy `INFORM` in reference flavours.
- No EU test bed accepts private issuers; the OIDF conformance suite and Credimi (SaaS, AGPL, StepCI + Android emulator pipelines) are the available harnesses.

## 4. Verdict

| Requirement | walt.id | Credo | DIDroom |
| --- | --- | --- | --- |
| HAIP | partial | partial | missing |
| `dc+sd-jwt` + pre-authorized code | supported (tested) | supported | partial |
| Custom `vct` + Type Metadata | partial (tested) | partial | missing |
| `/.well-known/jwt-vc-issuer` | supported (tested) | missing | missing |
| `x5c` | supported (tested) | supported | missing |
| Status list claim | supported (tested) | partial | missing |
| Status list management | missing (enterprise) | partial | missing |
| HSM/KMS | partial | partial | missing |
| Licence | Apache-2.0 | Apache-2.0 | AGPL-3.0 |
| Maintenance | active | active | stalled |
| Runs out of the box | supported | missing | supported |
| EUDI wallet evidence | unverified | partial | unverified |

Recommendation: start Phase 3 on walt.id Issuer2 + Verifier2 (community). It is the only candidate that produced a plan-conformant credential in half a day without writing issuer code, it already publishes `jwt-vc-issuer`, signs with `x5c`, embeds the status claim and verifies presentations with DCQL; Belfry still has to build a small status-list service (sign and host `application/statuslist+jwt`, flip bits from Odoo), host the VCTM and keep the management API private, and should put the issuer key in Vault Transit or a cloud KMS until the PKCS#11 question is settled. Credo is the fallback if walt.id's single-maintainer bus factor or enterprise boundaries become a problem: same formats, status-list module, cleaner KMS interface, but Belfry would own an Express service and host `jwt-vc-issuer` itself. DIDroom does not fit as the issuer (no pre-authorized flow, no `vct`/status/x5c, idle repo); Credimi may still be useful for conformance testing, and Fork Bomb's recent credo-ts fork is worth asking about.

## 5. Contradictions with the plan

1. The plan treats `/.well-known/jwt-vc-issuer` as the primary key publication and `x5c` as optional. In practice `x5c` is the path every stack supports (walt.id, Credo, EUDI reference wallet expects a trusted chain + signed metadata), and walt.id's verifier only resolves `jwt-vc-issuer` over HTTPS. Make `x5c` (own CA, leaf per issuer key) the primary and keep `jwt-vc-issuer` as the second publication.
2. "Revoking membership flips one bit" assumes a status-list service. None of the three community stacks hosts one; walt.id sells it as enterprise, Credo offers a library module. Budget a small Belfry service (one JWT per list, zlib bitstring, `Cache-Control`, re-signed on change).
3. "Claim QR after payment" is the pre-authorized code flow; HAIP (and the OIDF conformance suite) mandate the authorization code flow. Wallets accept pre-authorized code, so the flow works, but a strict HAIP certification will not cover it.
4. "Fork Bomb can supply most of Phase 3 off the shelf" does not hold for the issuer (see table); only the testing side (Credimi) remains interesting.
5. The plan's claims JSON shows `_sd` names; actual `_sd` entries are digests and `display_name`/`npub` live in disclosures (cosmetic, the plan's intent is right).
6. Type Metadata "at that URL": true for the spec, but walt.id will not serve it for a belfry.example `vct`; Belfry hosts `https://belfry.example/vct/village-pass/v1` itself (JSON with `vct`, `name`, `display`, `claims`, optionally `vct#integrity`).

## 6. Open questions

- Does OSS Issuer2's `/openid4vci/jwks` list every profile issuer key, or only the token key? The spike used one key for both, so key rotation with "old keys stay in the JWKS" is unverified.
- PKCS#11 in OSS Issuer2: confirm with walt.id whether a `managed` crypto2 key with the PKCS#11 provider can be configured without code changes, or whether Vault Transit (`tse`) is the intended community path.
- Does the EUDI reference wallet accept an SD-JWT VC whose issuer is identified only by `iss` + `jwt-vc-issuer` (no `x5c`)? Needs a phone test behind HTTPS with a custom wallet build.
- Timestamp truncation (`iat/nbf` to the day, `exp` to the hour) in walt.id's mapping functions: intended privacy feature or side effect? Affects day-pass validity windows.
- walt.id bus factor: recent commits are almost all one author; ask about the maintainer team and support options before committing.
- Fork Bomb: is DIDroom issuance being re-based on their credo-ts fork, and what exactly does Credimi test (OIDF HAIP suite, EWC, national wallets)?
- Anonymous pre-authorized access (`preauth-anonymous`) versus requiring `client_id`: decide which wallets Belfry wants to support at the event and configure accordingly.
