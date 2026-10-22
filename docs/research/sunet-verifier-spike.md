# Spike: SUNET vc verifier as the OpenID4VP-to-OIDC bridge (2026-10-22)

Checks the recommendation in `authentik-openid4vp.md` (SUNET verifier as an OIDC source for Authentik) against the real code.

- Repo: https://github.com/SUNET/vc, commit `2725c3418880b5ca4a66eaba051bbbcff4dfbf3a` (2026-10-22, "Merge pull request #844"). Paths below are relative to the repo root.
- Runtime test: public image `docker.sunet.se/iam_vc/verifier:latest`, built 2026-06-08 (no OCI labels; last commit before that time is `974fdd8e`). It is about four months behind HEAD, so runtime observations marked **(June image)** may differ from HEAD. Go is not installed here and a HEAD build needs Rust plus native ZK libraries (`dockerfiles/worker`), so HEAD was not run.
- Method: three parallel read-only code audits plus my own re-read of every load-bearing line cited below; the verifier was run with Docker (verifier + MongoDB, port 8800) and probed with curl. Stack torn down afterwards.

## Verdict in one paragraph

The verifier is an OpenID4VP 1.0 implementation with a real OIDC provider in front of it, and the request side (signed request object, `x509_san_dns`, DCQL by `vct` with claim paths, `direct_post.jwt`, QR + polling, deep link, template-based claim mapping to `sub`/`npub`) does what the plan needs. The response side does not: **the OIDC login path (`POST /verification/oidc-direct_post`) verifies neither the SD-JWT issuer signature nor the KB-JWT, and does no status check.** The code says so itself, and the June image minted a signed ID token from a hand-written, unsigned token. Even on the verifying path (`/verification/direct_post`, used by the standalone UI), the default trust evaluator accepts any issuer, there is no pinned-issuer-key mode, revocation is off by default and fail-open when on, and `exp`/`nbf` of the credential are not checked. Using it as-is would let anyone log in as any member. It is usable only with upstream or local patches to the OIDC path; see "Gap verdict".

## 1. Component, maturity, licence

- Verifier binary: `cmd/verifier/main.go`; service code `internal/verifier/{apiv1,httpserver,db,cache,notify,middleware}`; shared libs `pkg/openid4vp`, `pkg/sdjwtvc`, `pkg/trust`, `pkg/revocation`, `pkg/tokenstatuslist`. Docs: `docs/verifier/README.md`, `docs/OIDC_OP.md`, `docs/DIGITAL_CREDENTIALS_API.md`.
- Licence: BSD-2-Clause (`LICENSE.md`).
- Activity: 3561 commits since 2023-04; 370 commits touching `internal/verifier` or `cmd/verifier` since 2026-04; last verifier commit 2026-10-22. Two main authors in 2026 (Magnus Svensson 1078, Leif Johansson 1057 commits), plus a handful of smaller contributors. Tags `v0.7.21`–`v0.7.23` (Oct 2026). Project origin: DC4EU (README).
- Tests: 509 `_test.go` files repo-wide (526 non-test Go files); 42 test files under `internal/verifier`; `pkg/sdjwtvc`, `pkg/openid4vp`, `pkg/revocation` have substantial unit tests. CI runs `make test` (`.github/workflows/test.yaml`). No HAIP or EUDI external test vectors found (Singapore vectors in `pkg/keyresolver` are W3C/did:web).
- README claims vs code: `docs/verifier/README.md` lists "Credential verification (signature, status)", "Refresh tokens", "client_secret_basic", "private_key_jwt". Of these, refresh tokens are a TODO (`internal/verifier/apiv1/handler_oidc.go:423-426`), `client_secret_basic` is not implemented on `/token` (only form fields are bound, `handler_oidc.go:278-286`; confirmed at runtime: Basic auth returned `invalid_client`, `client_secret_post` worked), and signature verification does not apply to the OIDC path (section 4).
- Runtime deps: MongoDB (or Postgres/MariaDB via `common.sql.backend`) for the client registry; sessions in memory unless HA mode (`pkg/cache/factory.go`). Images are distroless; verifier image bundles native ZK libraries.

## 2. HAIP: request objects, client id, response modes

- OpenID4VP version: 1.0. No `client_id_scheme` request parameter; the prefix is inside `client_id` (`pkg/model/config.go:1120-1147`). `dcql_query` only; `presentation_definition` is commented out (`pkg/openid4vp/request_object.go:45-47`). `expected_origins`, `transaction_data`, `verifier_info` present (`request_object.go:56-90`). Code comments cite "OpenID4VP 1.0" sections (e.g. `pkg/openid4vp/dcql.go:57,66`, `handler_oidc.go:676`).
- Request object: served at `GET /verification/request-object/:session_id` as `application/oauth-authz-req+jwt` (`internal/verifier/httpserver/endpoints_openid4vp.go:42`), built by `CreateRequestObject` (`internal/verifier/apiv1/handler_openid4vp.go:20-135`), header `typ: oauth-authz-req+jwt` (`pkg/openid4vp/authorization_request_methods.go:59-61`), `x5c` from `verifier.key_config.chain_path`, `alg` from the key type (`pkg/jose/jwt.go:88-100`). **Runtime (June image):** header `{"alg":"ES256","kid":"vc_dev_signing_ec","typ":"oauth-authz-req+jwt","x5c":[2 certs]}`, payload `aud: https://self-issued.me/v2`, `response_type: vp_token`, `nonce`, `state = session id`, `response_uri = <public_url>/verification/oidc-direct_post`.
- Client id prefix: `verifier.client_id_scheme` default `x509_san_dns`, also `x509_hash` and `did` (`pkg/model/config.go:901`). Value is `x509_san_dns:<host of public_url>`; runtime showed `x509_san_dns:localhost:8800`, i.e. the port is included when `public_url` has one, which is not a DNS name (set a real host without port in production). There is no startup check that the certificate SAN matches the host (`config.go:1096-1110` only checks `x509_hash` material).
- `direct_post.jwt`: supported in HEAD. Mode is chosen by `verifier.inbound.openid4vp.response_mode` (`direct_post` | `direct_post.jwt`, `config.go:1461`), otherwise derived from `digital_credentials` (`pkg/model/openid4vp_response_mode.go:40-67`). When encryption is required, an ephemeral P-256 key goes into `client_metadata.jwks` with `encrypted_response_enc_values_supported: [A256GCM, A128GCM]` (`handler_openid4vp.go:87-116`, `ephemeral_keys.go:43`), and the response is decrypted with ECDH-ES (`handler_oidc.go:722`). **Runtime (June image):** `inbound.openid4vp.response_mode` was ignored (key did not exist yet); with `digital_credentials.enable: true` the request object said `response_mode: direct_post.jwt` but `client_metadata` was empty (no encryption key), so the June release's encrypted mode is not usable. HEAD behaviour is unverified at runtime.
- Plain `direct_post` is the default and works (runtime).

## 3. DCQL

- `pkg/openid4vp/dcql.go`: `CredentialQuery{id, format, meta.vct_values, claims[].path, claim_sets, trusted_authorities, require_cryptographic_holder_binding}`; `FormatSDJWTVC = "dc+sd-jwt"` (`dcql.go:22-47,108,214,438`).
- How an OIDC login becomes a query (`internal/verifier/apiv1/client.go:386-435`): first a presentation-request template whose `oidc_scopes` match (YAML files in `verifier.inbound.openid4vp.presentation_requests_dir`, struct `pkg/configuration/presentation_requests.go:23-48`), else a fallback built from `common.credential_metadata[scope]` that sets only `format` and `meta.vct_values` and **requests no specific claims** ("It does NOT enumerate individual claims", `client.go:754-756`).
- Custom type: scopes are arbitrary map keys; no hard-coded credential types. Needs a VCTM file (`vctm_file_path`) per type, and `supported_credentials` entries whose scope is used by at least one static client (startup validation `supported_credential_scope_unused`, hit at runtime).
- **Runtime (June image), village-pass test:** with `common.credential_metadata.village_pass` pointing at a minimal VCTM for `https://belfry.example/vct/village-pass/v1` and a template requesting `member_number`, `npub`, `pass_tier`, scope `openid village_pass` produced exactly: `{"credentials":[{"id":"village_pass","format":"dc+sd-jwt","meta":{"vct_values":["https://belfry.example/vct/village-pass/v1"]},"claims":[{"path":["member_number"]},{"path":["npub"]},{"path":["pass_tier"]}]}]}`. Without a template the June image enumerated every VCTM claim (29 paths for PID); HEAD omits `claims` instead.
- Not found: a check that the presented credential's `vct` matches `vct_values` on the SD-JWT path (only the format is compared, `internal/verifier/apiv1/handlers_verification.go:273-282`).

## 4. Verification (the critical part)

Two different response endpoints exist (`internal/verifier/httpserver/verification_routes.go:31-47`):

| Endpoint | Used by | What it verifies |
| --- | --- | --- |
| `POST /verification/oidc-direct_post` | the OIDC login flow (`response_uri` in the request object, `handler_openid4vp.go:33`) | W3C VC 2.0 documents only. SD-JWT and mdoc: nothing. `handler_oidc.go:829`: "This flow verifies nothing for any format, which is a gap of its own"; `pkg/openid4vp/claims_extractor.go:565-567`: "ProcessDirectPost performs no signature verification for any format". Claims are read with `sdjwtvc.Token.Parse` (no signature check) and mapped straight into the session (`handler_oidc.go:845-861`). |
| `POST /verification/direct_post` | standalone verifier UI; DC API branch of the authorize page | SD-JWT: `VPTokenValidator` (KB-JWT, nonce, aud, sd_hash, transaction data) then `JWTTrustVerifier.EvaluateIssuerTrust` (signature + trust), optional revocation (`handlers_verification.go:296-317,728-748`). |

**Runtime proof (June image):** `POST /verification/oidc-direct_post` with `vp_token={"village_pass":["eyJhbGciOiJFUzI1NiJ9.<payload>.c2ln~"]}` (fake signature `sig`, no KB-JWT, payload `member_number: OSV-0042, npub: npub1test, pass_tier: week`) returned `code_issued`; `/token` then returned an ES256-signed ID token with `sub`, `npub: npub1test`, `pass_tier: week`. Log: "VP processed successfully".

Details on the verifying path, for when the OIDC path is fixed or rerouted:

- Issuer key resolution (`pkg/trust/jwt_verifier.go:199-292`), first match wins: `x5c` header (leaf key used, chain **not** validated locally), `jwk` header (embedded key, accepted), issuer `did:` (resolved only through the external go-trust PDP), `kid` + `.well-known/jwt-vc-issuer` (`pkg/trust/jwks_resolver.go:195-240`, also falls back to `openid-credential-issuer` / `openid-configuration`; issuer in metadata must equal `iss`; 5-minute cache; plain `http.Client`). Trust decision: `verifier.trust.pdp_url` set → AuthZEN PDP (go-trust); unset → `AllowAllEvaluator`, logged at startup as "Trust evaluation is DISABLED - no pdp_url configured. All credential issuers will be trusted." (`internal/verifier/apiv1/client.go:186-194`, `pkg/trust/gotrust.go:28-33`). **No config for a pinned issuer key**; `WithSDJWTStaticKey` exists in `pkg/sdjwtvc/openid4vp_handler.go:29-70` but is not wired into the verifier. did:webvh: not found.
- KB-JWT (`pkg/openid4vp/validator.go:171-254`): mandatory, `cnf.jwk` key (EC/RSA/Ed25519), `typ kb+jwt`, `iat` within 10 min (+30 s skew), `sd_hash`; `nonce` and `aud` checked only when the validator is given them, which the verifier does (`handlers_verification.go:300-301`). Three KB implementations exist with different strictness (`pkg/sdjwtvc/keybinding_verify.go`, `pkg/sdjwtvc/verification.go:627-695`); only `validator.go` is on the verifier path.
- Token Status List (`pkg/revocation/status_list.go`): reads `status.status_list.{uri,idx}`, fetches `statuslist+jwt` or `+cwt`, verifies the list's signature via the list's own `iss`/`kid` through the JWKS resolver (`status_list.go:312-323`); does not check that the list issuer equals the credential issuer. **Off by default** (`verifier.revocation.enabled`), and when on, `fail_open` defaults to `true` (`pkg/model/config.go:1066-1086`); explicit revoked/suspended is always rejected (`handlers_verification.go:734-745`). A credential with no `status` claim passes.
- Not checked on the verifying path: credential `exp`/`nbf`/`iat` (`jwt_verifier.go:102` uses `WithoutClaimsValidation()`), `typ` header, VCTM/type metadata (never fetched by the verifier), `vct` vs query. Unmatched disclosures are dropped silently (`pkg/sdjwtvc/utils.go:33-140`).
- Recent commits show active hardening of the verifying path (2026-10-13/14 "verify holder binding instead of checking that a proof is present", 2026-10-15 "normalising on the verification path was a bypass"), but no commit or issue reference addresses the OIDC path gap.

## 5. OIDC provider side

- Routes (runtime and `internal/verifier/httpserver/service.go:122-210`): `/.well-known/openid-configuration`, `/.well-known/oauth-authorization-server`, `/.well-known/openid-federation`, `/.well-known/did.json`, `/jwks`, `/authorize`, `/token`, `/userinfo`, `/op/par`, `/register` (+ GET/PUT/DELETE `/register/:id`), `/qr/:session`, `/poll/:session`, the `/verification/*` routes above, `/ui/*`, `/health`, `/swagger`.
- Discovery (runtime): `response_types_supported: [code, id_token, token id_token]` (only `code` is actually allowed for clients), `grant_types_supported: [authorization_code]`, `code_challenge_methods_supported: [S256]`, `token_endpoint_auth_methods_supported: [client_secret_basic, client_secret_post, none]`, `id_token_signing_alg_values_supported: [RS256, ES256]` (hard-coded; actual key ES256), `subject_types_supported: [public, pairwise]`, fixed `claims_supported` list.
- Clients: static clients in config (`verifier.inbound.openid4vp.clients`, public only in the shipped example) or dynamic registration (RFC 7591, open by default, `dynamic_registration_auth` modes static/jwt available). Runtime registration returned a confidential client with bcrypt-stored secret.
- PKCE: S256 only; required for public clients, and for registered clients by OP policy `require_pkce` default true (`internal/verifier/apiv1/pkce_policy.go:44-60`, `handler_oidc.go:131`).
- Claim mapping: template `claim_mappings` (VP claim path → OIDC claim name, `"*": "*"` passthrough minus `_sd`, `_sd_alg`, `cnf`, `status`, `vct`, `docType`), applied by `MapClaimsToOIDC` (`pkg/openid4vp/claims_extractor.go:289-327,360-375`). Mapped claims land flat in the ID token, except reserved `iss, sub, aud, exp, iat, nonce` (`handler_oidc.go:452-460`). Not per client; per scope set.
- `sub`: `session.WalletID` is set from whatever mapped claim is named `sub` (`handler_oidc.go:859-861`), then **hashed**: `base64url(sha256(walletID + salt))`, or with `clientID` for `pairwise` (`client.go:262-277`). **Runtime:** mapping `member_number: "sub"` gave `sub = kZyr4t…` (hash of `OSV-0042` + salt), stable for a given salt; `npub` and `pass_tier` came through verbatim. So `sub = member_number` literally is not possible without a code change; `sub = H(member_number, salt)` is, and is stable as long as the salt and `subject_type` never change. If no claim maps to `sub`, every user gets the same `sub` (hash of salt alone) — observed in the first run (`sub = CsBc7D…` with `foo: bar`). Map `member_number` to a second name too if Authentik needs the raw value.
- Refresh tokens: not implemented. Access token/userinfo: gated on `enable_userinfo`; the June image returned no access token with my config.

## 6. Device modes

- Cross-device: `/authorize` renders `internal/verifier/staticembed/authorize_enhanced.html` with a QR (`/qr/:session`, PNG, encodes `openid4vp://cb?client_id=…&request_uri=<public_url>/verification/request-object/<session>`, `handler_oidc.go:1067,1242-1252`); the page polls `/poll/:session` every 2 s; after the wallet posts, `/poll` returns `{"status":"code_issued","redirect_uri":"<rp redirect>?code=…&state=…"}` and the page navigates there (observed at runtime). Code comment: "TODO(masv): no polling, use WebSocket or Server-Sent Events instead" (`service.go:199`).
- Same-device: deep link hard-coded to `openid4vp://cb?…` (`handler_oidc.go:1251`); `digital_credentials.deep_link_scheme` only reaches the JS polyfill. `supported_wallets` config adds per-wallet web links. `haip://`, `eudi-openid4vp://`: not found.
- Digital Credentials API: off by default (`digital_credentials.enable`); JS uses `@sirosfoundation/dc-api` with protocols `openid4vp-v1-signed/-multisigned/-unsigned` (`internal/webvendor/dc-api.js:13-21`). On the OIDC authorize page the DC API branch posts to `/verification/direct_post`, not `oidc-direct_post`, and no code path was found that sets the OIDC session code from there (audit finding, not tested): DC API through the OIDC login looks unfinished.

## 7. Running it

Ran in about 25 minutes: `make pki` script (plain openssl) for dev keys, public image + `mongo:7.0`, TLS off, `public_url http://localhost:8800`. Started cleanly; `/health`, discovery, JWKS, `/register`, `/authorize`, `/qr`, `/poll`, request object, `/verification/oidc-direct_post`, `/token` all answered as described above. Config pitfalls hit: static clients defined under `verifier.inbound.openid4vp.clients` in HEAD's `config.yaml` were not recognised by the June image (`invalid_client`; dynamic registration worked instead); every `supported_credentials` scope must be used by a static client or startup panics; `inbound.openid4vp.response_mode` is HEAD-only.

## Gap verdict

| Need (plan) | Status | Evidence | Fallback cost if unfixed |
| --- | --- | --- | --- |
| OpenID4VP 1.0 signed request object | supported | §2, runtime | — |
| `x509_san_dns` client id with `x5c` | supported (no SAN/host check) | §2 | — |
| `direct_post.jwt` encrypted response | supported in HEAD; broken in June image | §2 | run HEAD, verify at runtime |
| DCQL by `vct`, specific claims | supported via templates | §3, runtime | — |
| Custom `village-pass` type in config | supported (VCTM file + template) | §3, runtime | — |
| Issuer signature on OIDC login path | **missing** | §4, runtime | patch `ProcessDirectPost` to run the same checks as `/verification/direct_post` (~1–3 days, Go), or upstream PR |
| Issuer key pinned to Belfry's key | **missing** (allow-all default, or external PDP) | §4 | run go-trust PDP (extra service), or add static-key resolver (small patch) |
| KB-JWT with nonce + aud | supported on verifying path only | §4 | same patch as above |
| Token Status List, fail closed | partial (off by default, fail-open default, list issuer unchecked) | §4 | config `revocation.enabled: true, fail_open: false`; patch for issuer check |
| `exp`/`nbf` of credential | **missing** on verifier path | §4 | small patch |
| OIDC: discovery, `/authorize`, `/token`, `/jwks` | supported | §5, runtime | — |
| PKCE S256, confidential client | supported (`client_secret_post` only; Basic not implemented) | §5, runtime | Authentik must send secret in body (check Authentik's OAuth source supports it; unverified) |
| `sub = member_number`, `npub` extra claim | partial: `sub = H(member_number+salt)`, `npub` ok | §5, runtime | acceptable if salt is treated as immutable; else small patch |
| QR + polling | supported | §6, runtime | — |
| `openid4vp://` same-device | supported (scheme hard-coded) | §6 | — |
| Digital Credentials API | partial/unfinished on OIDC page | §6 | optional; skip |

**Fallbacks (READMEs fetched 2026-10-22, not tested):**

- `eudi-srv-verifier-endpoint` (Kotlin/Spring, Apache-2.0): OpenID4VP 1.0 with `haip` profile, `direct_post` and `direct_post.jwt` (default jwt), client id prefixes `pre-registered`, `x509_san_dns`, `x509_hash`, DCQL, SD-JWT VC + mdoc, status check on by default, no OIDC endpoints, Verifier API has no access control, README: "We strongly recommend to not put this version of the software into production use". Cost: a thin OIDC layer (`/authorize` rendering QR + polling its Verifier API, `/token`, `/jwks`, discovery) plus a reverse proxy for its unauthenticated admin API; roughly 1–2 weeks. Issuer trust configuration not checked here.
- `polaris-oid4vp` 1.0.0 (Python, Apache-2.0, released 2026-10-11, single author, "pre-pilot", "no independent security review exists"): OpenID4VP 1.0 + HAIP library, `direct_post.jwt` with ECDH-ES/A128GCM|A256GCM, KB-JWT (sd_hash, signature, nonce, audience, freshness), opt-in Token Status List resolver, `x509_hash`; library with a test-harness HTTP listener, no OIDC. Cost: the same thin OIDC layer on top, about 1–2 weeks; smaller surface than SUNET but a one-person project.

Comparison: patching SUNET's OIDC path (reuse `handlers_verification.go` logic in `ProcessDirectPost`, add a static issuer key, enable revocation fail-closed, add `exp`/`nbf`) is probably the smallest change, but it means carrying a fork of a fast-moving 50k-line Go repo until upstream accepts it. The thin-OIDC-over-polaris route is more code to write but all of it is ours and small.

## Contradictions with the plan

- `authentik-openid4vp.md` says "checks: issuer signature pinned to Belfry's key; KB-JWT …; Token Status List, fail closed" and lists SUNET as "status check" capable with "HAIP support unconfirmed". HAIP request-side support is now confirmed; the checks are not available on the OIDC path and there is no pinned-key mode anywhere. Effort estimate "1–2 weeks for bridge + Authentik config" assumed a working verifier; add the patch work or switch fallback.
- Plan: `sub = member_number`. Achievable only as a salted hash of it (or with a patch).
- Plan: confidential client with Authentik. Works, but `client_secret_post` only.
- Plan's happy path item 6 ("signs in by presenting the same pass") also relies on the verifier checking `vct`, which the SD-JWT path does not do; a PID or any other SD-JWT would pass a `village_pass` query once signatures are verified unless the template's claim set fails to match. Needs a check or a patch.

## Open questions

- Will SUNET accept a PR that routes `oidc-direct_post` through the verifying path? No issue found; ask on the repo (maintainers are responsive by commit cadence, unverified).
- Does Authentik's OAuth source send the client secret in the POST body (`client_secret_post`)? Not verified in this spike.
- go-trust PDP: is running it (for issuer trust) lighter than patching in a static key? Not evaluated.
- HEAD runtime behaviour (encrypted response, template loading, client config under `inbound.openid4vp.clients`) is untested because HEAD was not built; the public image is four months old. Building HEAD needs the `dockerfiles/worker` chain with Rust and native ZK libs.
- EUDI wallets' acceptance of `x509_san_dns` certs from a private CA for the request object, and of `openid4vp://cb` links, is unknown; test with the national/sandbox wallets planned for Phase 3.

## Artefacts

- Work area (scratch, not in repo): `/tmp/claude-1000/-home-friedger--repos-github-commonshub-collective-belfry/25a6ddf2-0cae-48ea-9d4d-b384572f5352/scratchpad/spikes/sunet-vc` (clone) and `.../spikes/run` (compose file, config, test VCTM and template, captured request objects and tokens).
- Docker: containers, network and volume removed; the pulled image `docker.sunet.se/iam_vc/verifier:latest` and `mongo:7.0` remain in the local image cache.
