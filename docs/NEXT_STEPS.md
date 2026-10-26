# Next steps (handoff from 2026-10-22, updated 2026-10-23)

Start here in the next session. Steps 0–3 are done, Phase 2 has started (§3b relay, §3c Keycast login, §3d participant login, §3h Keycast fork patches, Blossom and policy kinds), Phase 3 is built (§3e), hardened once (§3f) and **deployed on a public test VM** (§3g preparation, §3i the deployment itself: belfry.tofauti.net, smoke 79 green over TLS). What is left before the event is in §4. The plan's happy path (invite → pay → accounts → pass in the wallet → door and app sign-in with the pass) runs end to end against the local stack with a scripted wallet; continue with §4.

## 0. Sync with the live plan — done

The live doc was at rev 33 at the start of the session and now carries a "Verification results (2026-10-22)" block under Open decisions. The snapshot was re-exported from that version; its rev is in the snapshot header.

## 1. Verify the plan — done

Results are in `docs/research/`. Read the verdict paragraph at the top of each note before building on that area.

### Consistency review (`plan-consistency-review.md`)

- [x] Phase numbers consistent.
- [x] Login row vs. Keycast decision: edit the architecture table once decision 2 below is taken.
- [x] Happy path step 4 matches preload-user; the claim step is missing from steps 5–6 (confirmed by the Keycast spike).
- [x] Eager vs. lazy key creation: the design already assumes eager; close the decision that way (recommendation) or drop the npub claim from Phase 3.
- [x] `sub`/DID tables agree; add "Phase 4 backfill" to the re-issue column.

### Technical spikes

- [x] **Keycast** (`keycast-spike.md`): runs with `KMS_PROVIDER=file`; `vine_id` required but unvalidated, usable as Odoo id, idempotent per tenant; key and NIP-05 exist at preload; claim flow works; policies refuse disallowed kinds on HTTP RPC and NIP-46; status endpoint is the right lever for expiry/refund; Authentik login via server-signed UCAN sessions, 3–5 days. `provisioning_operation_id` is only on create-minor-account (signing-server.md corrected).
- [x] **SUNET vc verifier** (`sunet-verifier-spike.md`): request side fine; OIDC login path verifies nothing (no issuer signature, KB-JWT or status). Not usable unpatched. `sub` is a salted hash only.
- [x] **Authentik** (`authentik-spike.md`): design holds on 2026.8.5; blueprint in the note; webhook can pre-create the source link (`POST /api/v3/sources/user_connections/oauth/`, code-read only); npub needs a `user_write` stage.
- [x] **Issuer stack** (`issuer-spike.md`): walt.id Issuer2/Verifier2 v1.2.0 issued and verified a village-pass SD-JWT VC; no status-list service in community; x5c should be primary; Credo fallback; DIDroom not an issuer option. Phone wallets not tested.
- [x] **Odoo + Stripe** (`odoo-stripe-spike.md`): Odoo 19 Community does pricing, early-bird, +50% and promo codes with standard modules; Odoo's Stripe provider keys on PaymentIntent/invoice, not event id; chargebacks never reach Odoo; invite links need a custom module.

## 2. Decisions — taken 2026-10-22 (recorded in the doc's Decisions list)

1. Event date: late January 2027. Some national EUDI wallets may be usable; sandbox and open-source wallets are the baseline.
2. Login for Nostr apps: Keycast trusts Authentik via OIDC (Keycast fork mints a server-signed UCAN session after the Authentik callback).
3. Issuer stack: **still open**. walt.id recommended; the user checks with the DIDroom maintainer first (questions added to the doc's Fork Bomb list).
4. Hosting: one VM, Docker Compose, for every service.
5. Language for Belfry's own code: Python.
6. Repo layout: monorepo here (`infra/`, `services/<name>/`, `odoo/addons/<module>/`, `docs/`).
7. OpenID4VP bridge: patch the SUNET verifier's OIDC path; offer upstream, carry a fork meanwhile.
8. Payment flow: Odoo payment link + Odoo's Stripe provider (keyed on invoice + PaymentIntent); Belfry's own Stripe endpoint only for disputes (keyed on event id).
9. Odoo 19 Community, fresh in Compose; the +50% company surcharge compounds with early-bird prices.
10. Nostr keys: created eagerly at payment via preload-user.

Still open (not gating the build): Belfry as required witness, directory defaults, PID at checkout, Cashu.

## 3. First build slice: Phase 1 end to end, locally — built 2026-10-22

Contract: `docs/slice-1-onboarding.md`. Verified with `make up && make bootstrap && make up-app && make smoke` (17 checks green), `make test-odoo` (41 tests) and `make test-onboarding` (22 tests).

- [x] Repo layout: `infra/`, `odoo/addons/belfry_pass/`, `services/onboarding/`, `docs/`.
- [x] Compose stack: Postgres, Odoo 19, Authentik 2026.8.5, Keycast (fe9c6460) + relay + Redis, onboarding service; `bootstrap.sh` creates the Odoo DB, API key, Keycast admin and policy, applies the Authentik blueprint.
- [x] Odoo: pass products with early-bird (40/80/160 until `belfry.early_bird_until`), company pricelist +50% compounding, promo code `OSV10`, steward invite links (`/belfry/invite/<token>`), `belfry.pass`, automation webhooks on invoice paid and on refund.
- [x] Onboarding service: `activate_pass` idempotent on invoice + PaymentIntent (Authentik user, Keycast preload, npub written back), refund and dispute revocation, `reconcile` and `expire` jobs, Stripe dispute endpoint keyed on event id.
- [x] Tests: replayed webhook, refund, expiry, duplicate Stripe event, signature.

Known gaps to carry into the next steps:

1. Authentik refuses API-created source links (`user` read-only on `UserOAuthSourceConnection` in 2026.8.5), so participants link the pass once from user settings. Decide between that and `username_link` matching when the Phase 3 bridge exists.
2. The Keycast image is built from the upstream Dockerfile via `infra/keycast/build/`; the git build context fails on a dangling submodule. The Belfry fork exists since 2026-10-23 as a patch series (§3c); branding is still Divine's.
3. Stripe is emulated locally (done transaction + manual payment). Real Stripe test mode needs a public URL for Odoo's webhook and `stripe listen` for the dispute endpoint.
4. No enrollment or password link is sent to the Authentik user yet (Phase 2 login work).
5. VAT, credit notes on refund and the day-pass "which day" question are still open (see the Odoo spike note).

## 3b. Phase 2 start: community relay with a whitelist from Odoo — built 2026-10-23

Decision (recorded in the live doc): the community relay is **rnostr** (0.4.9, `rnostr/rnostr:latest`). It has NIP-42 with a `pubkey_whitelist` for REQ and EVENT keyed on the authenticated pubkey, and hot-reloads its config file. Reading and writing both need AUTH as a whitelisted pubkey; the list is derived from `belfry.pass` (state `active`) and never edited by hand. The Keycast NIP-46 relay stays a separate, open service.

- [x] `infra/relay/rnostr.toml` template (lines marked `# belfry:whitelist`), `relay-init` seeds the config volume, `relay` on host port 3090; `infra/relay/README.md`.
- [x] Onboarding: `relay_whitelist.py` renders the template in place (rnostr `--watch` ignores renames); synced at start, at activation (new pubkey included before the kind-0 profile is published), after revoke/expire/reactivate, via `python -m belfry_onboarding sync-relay` and `POST /admin/sync-relay`. `RELAY_EXTRA_PUBKEYS` for Belfry's own keys.
- [x] Kind-0 profile (name, display name, NIP-05 `member@NIP05_DOMAIN`) signed through Keycast with the preload UCAN, published with NIP-42 AUTH (commit f410206, now exercised in the smoke test).
- [x] Keycast policy `belfry-event` now allows kinds 0, 1, 7, 22242, 30023; bootstrap applies `policy.sql` on every run (it updates the kinds).
- [x] Bug found and fixed in the live check: Keycast's status endpoint needs a `reason` for `suspended`/`banned`, so slice 1 never actually suspended a signer account on refund or expiry. `_close_pass` now syncs the whitelist before calling Keycast.
- [x] Verified live: unauthenticated REQ → `CLOSED auth-required`; activation adds the pubkey and rnostr reloads; refund/steward revoke removes it and suspends the Keycast user with the reason; reactivation restores it. `make smoke` 23 checks, `make test-onboarding` 35 tests.

Open from this slice: whether a Keycast suspension takes effect on an already cached HTTP-RPC authorization (spike: unverified); the kind list for the Phase 2 apps (calendar, marketplace, chat kinds are not in the policy yet); `RELAY_EXTRA_PUBKEYS` is empty, so no Belfry announcement key exists yet; the profile is not re-published when the partner's name changes.

## 3c. Phase 2: Keycast fork with the Authentik login — built 2026-10-23

Decision 2 implemented. The fork is a `git format-patch` series in `infra/keycast/build/patches/` on top of the pinned upstream commit, applied by the image build (monorepo, decision 6; no GitHub fork). Details and the workflow for changing a patch: `infra/keycast/README.md`, "The Belfry fork". Decision recorded in the live doc.

- [x] Patch 0001, `api/src/api/http/oidc.rs`: `GET /api/auth/oidc/start|callback|config`, authorization code + PKCE against `OIDC_ISSUER`, ID token verified against the JWKS (RS256/ES256, `iss`, `aud`, `exp`, `nonce`; alg confusion refused), signed short-lived flow cookie, same-origin `return_to` only. The subject is linked on first login to the account whose `vine_id` equals the claim `keycast_vine_id` (`odoo-partner-<id>`), table `oidc_identities`; accounts are never created. A server-signed UCAN session cookie is set and the browser returns to `/api/oauth/authorize`, so consent continues as before. The OAuth popup shows "Sign in with Belfry"; `KEYCAST_OIDC_ONLY=true` hides the password and registration forms (the password API stays: bootstrap registers the admin through it). 9 unit tests.
- [x] Patch 0002: the web app's `/login` page offers the same login.
- [x] Patch 0003: upstream bug found in the live check. `/api/oauth/token` answered 503 for preloaded accounts because it read the NULL e-mail as non-null; upstream never hit it because unclaimed accounts could not log in.
- [x] Authentik blueprint `belfry-keycast-provider.yaml`: OIDC provider + application `keycast`, scope `keycast` with the link claim, bound to `belfry-participants`. authentik 2026.8 leaves `grant_types` empty for blueprint-created providers and then refuses every authorize request, so the blueprint sets it.
- [x] Onboarding service: the Authentik user joins `belfry-participants` at activation and reactivation and leaves it when its last active pass closes (checked per Authentik user, since two partners with one e-mail share one user); full sync at start, in `reconcile` and via `python -m belfry_onboarding sync-participants`. Slice 1 never added anyone to that group.
- [x] Found on the way: `.gitignore`'s `build/` rule had kept `infra/keycast/build/Dockerfile` out of git since slice 1 (a fresh clone could not build Keycast); Compose read `STRIPE_WEBHOOK_SECRET` and `RELAY_EXTRA_PUBKEYS` as their inline comments because the values were empty (comments moved above); `gen-secrets.sh` now appends variables new in `.env.example` to an existing `.env`.
- [x] Verified live: `make smoke` 25 checks, including step 6c (`infra/scripts/keycast-oidc-login.sh`: popup, Authentik login over the flow executor API, callback, consent, token, kind-1 signed with the participant's key). Revoked participant: Authentik refuses the authorize request. Reactivated: login works again, Keycast skips consent for the already approved app. Authentik user in the group without an Odoo partner: Keycast answers 403 "No account". `make test-onboarding` 42 tests.

Open from this slice:

1. ~~Participants still have no way to get an Authentik login~~ — done in §3d (account-setup link mailed at activation, 2026-10-23). Pass sign-in still needs the Phase 3 bridge.
2. Key export needs the account password, which OIDC-only accounts do not have (spike §6.5); "managed, exportable" for full access does not hold yet.
3. Divine UI and branding are unchanged (logo, "Divine" e-mail copy, ATProto settings card).
4. Keycast's e-mail stays empty for preloaded accounts; the UCAN carries an empty `email` fact. Nothing in Belfry's flow needs it so far.

## 3d. Phase 2: participants get their Authentik login — built 2026-10-23

Closes §3c open item 1. Decision (recorded in the live doc, rev 45): at activation the onboarding service mints a one-time Authentik account-setup link and Odoo mails it with the pass details; stewards can re-send it from the pass form.

- [x] Blueprint `infra/authentik/blueprints/belfry-account-setup-flow.yaml`: recovery flow `belfry-account-setup` (prompt password + repeat with a 12-character zxcvbn policy, user write, user login with a 14-day "remember me"), set as the default brand's recovery flow (model `authentik_brands.brand`). Deliberately without identification or e-mail stage: the link alone drives it, a direct visit ends in "no pending user".
- [x] Onboarding service: `create_recovery_link` (`POST /api/v3/core/users/<pk>/recovery/`, `token_duration days=LOGIN_LINK_DAYS`, default 30); the link's origin is rewritten to the new `AUTHENTIK_PUBLIC_URL` (authentik builds it from the request host, `authentik-server:9000`). Written on the pass (`login_setup_url`, `login_setup_expires_at`) in the activation write; strict like the group step. `send_login_link`, `POST /admin/passes/<id>/login-link`, CLI `login-link <pass id>`. 49 tests.
- [x] Odoo `belfry_pass` 19.0.1.1.0: the three fields, mail template `mail_template_pass_activated` (pass, dates, member number, link and expiry, apps URL from the new `belfry.apps_url`, npub) sent by `belfry.pass.write` whenever the link is written on an active pass, button "Send new login link" (calls the service), 44 tests. Bootstrap upgrades the module when the manifest version changes and restarts Odoo (the running server does not pick up new fields from a CLI upgrade), sets `belfry.apps_url`, waits for the blueprint and checks the brand.
- [x] Verified live: `make smoke` 34 checks. The mailed link runs prompt → login → redirect and the session is the participant's; a consumed link is refused ("No user found and can't create new user") and its password does not log in; the chosen password signs in to Keycast through Authentik (step 6d); the re-send endpoint replaces the link and queues a second mail. The throwaway `set_password` in the smoke test is gone.

Open from this slice: no outgoing mail server locally (mails stay `outgoing`; production needs Odoo's SMTP); self-service "forgot password" needs authentik's SMTP settings and an identification + e-mail stage (the example blueprint in the image shows the shape); passkeys are only offered from Authentik user settings, not in the setup flow; the participant lands on Authentik's user portal after setup, not on the apps (the flow executor refuses external `next` URLs; a brand `default_application` could change that).

## 3e. Phase 3: pass certificates, door check and pass sign-in — built 2026-10-23

Built on the user's "continue until the happy path works". Decisions taken while building (recorded in the live doc, rev 46):

- Issuer stack: **walt.id Issuer2 + Verifier2 1.2.0** (community), as the spike recommended; decision 3 is closed provisionally, the DIDroom check can still swap Credimi in for testing. Belfry's issuer key and x5c chain come from an own CA (`scripts/gen-issuer-pki.sh`); keys live in files until an HSM/KMS is chosen.
- Pass sign-in bridge: **Belfry's own Python bridge over Verifier2** (`services/onboarding/belfry_onboarding/bridge.py`, a few hundred lines: OIDC authorize/token/userinfo/jwks, PKCE, client secret) instead of patching the SUNET verifier (decision 7). Reasons: the SUNET OIDC path verifies nothing and would need a Go fork whose HEAD does not build here (sunet-verifier-spike.md); Verifier2 was already running for the door; Belfry's code is Python (decision 5). The SUNET patch can still be offered upstream later; nothing depends on it.
- Authentik matching for the pass source: `username_link` (the bridge sends `preferred_username` = member number lowercased = the Authentik username), so the first pass login links the source without the participant doing anything; `identifier` matching from then on. The property mapping keeps `username` (needed for the match) and nulls `email`/`name`.
- Status list: one Token Status List (`/status/1`, 1 bit per pass, bit = pass id), derived from Odoo on every request, signed with the issuer key and the same x5c leaf as the credentials, because Verifier2 trusts a status list only from the credential's own signer. `STATUS_LIST_TTL` is the revocation delay at the door (5 s locally, 300 s suggested for production).
- Claim offers are minted when the claim page is opened (Issuer2 keeps offers in memory only); the mail carries the claim link, not the QR.

Pieces (details: `infra/issuer/README.md`, onboarding README):

- [x] Compose: `issuer` (7005) and `verifier` (7004); `infra/issuer/config/` with the `village_pass` configuration and the rendered profile; `infra/verifier/config/`; new env `ISSUER_PUBLIC_URL`, `VERIFIER_PUBLIC_URL`, `ONBOARDING_PUBLIC_URL`, `STATUS_LIST_URL`, `STATUS_LIST_TTL`; bootstrap waits for both and sets `belfry.public_url`.
- [x] Onboarding: `claim_token` and `credential_status_index` at activation; `GET /claim/<token>` (QR page) and `/offer`; `GET /status/1`; `GET /vct/village-pass/v1`; `/door` + sessions; `/bridge/*`; `POST /admin/passes/<id>/revoke`; test wallet `python -m belfry_onboarding.testwallet` (OpenID4VCI receive, OpenID4VP present with KB-JWT). 64 tests.
- [x] Odoo `belfry_pass` 19.0.1.2.0: `claim_token` (admins), `claim_url`, status index, "Revoke pass" button, claim link in the activation mail, `belfry.public_url`. 46 tests.
- [x] Authentik blueprint: the pass-bridge source points at the bridge (public authorize URL, internal token/userinfo), `username_link`.
- [x] Verified live: `make smoke` 60 checks. Issuer2 issued a `dc+sd-jwt` with Belfry's key, two-certificate x5c, explicit validity (no rounding), status claim and three disclosable claims; the door session verified it with `signature`, `expiration`, `not-before`, `allowed-issuer` and `credential-status`; Authentik signed the participant in by pass alone through the bridge (link created, no password); a steward revoke flipped the status bit and the door refused the certificate after the TTL. Found on the way: Issuer2's JWKS lists the profile key as well as the token key (open question in the spike, answered); the verifier caches the status list for min(max-age, ttl, exp).

Open from this slice (not blocking the local happy path):

1. Real wallets: everything is `http://localhost`. Phones need HTTPS with public certificates for issuer, verifier and onboarding URLs; the EUDI reference wallet additionally wants signed issuer metadata and a trusted issuer certificate (issuer-spike.md §3). A public test deployment is the next step for this.
2. Trust in Belfry's CA is not configured on the verifier (the `signature` policy takes the x5c leaf key; `allowed-issuer` pins the `iss`); add an `x5c`/trust-list policy and publish the CA. `/.well-known/jwt-vc-issuer` is served by Issuer2 but only resolved over HTTPS by walt.id.
3. The verifier's request is unsigned (`redirect_uri` client id); HAIP wants `x509_san_dns` and `direct_post.jwt`. The claim flow is the pre-authorized code (HAIP wants the authorization code flow).
4. Bridge state is in memory (one instance); Authentik's OIDC source does not verify the ID token signature (it uses userinfo), so `/bridge/jwks` is unused for now.
5. The management APIs of Issuer2 and Verifier2 are unauthenticated: the production reverse proxy must block `/issuer2/*` and `/verification-session/create|info`.
6. Issuer key rotation (old keys in the JWKS until their credentials expire) and HSM/KMS for the key: not done.
7. Day-pass `exp` is the end of the day (UTC); the plan's `role` claim and the `sub` = did:webvh variant is not in the credential (DIDs dropped 2026-10-24).

## 3f. Hardening: Odoo mail and privileges, issuer pinning, signed verifier requests — built 2026-10-23

- [x] Odoo sends mail: `mailpit` in Compose (SMTP sink, UI http://localhost:8025); bootstrap creates the outgoing mail server `belfry` from `SMTP_*`, sets the company name and sender (`COMPANY_NAME`, `MAIL_FROM`). The smoke test sends the queue and reads the delivered activation mail back from Mailpit. Production: point `SMTP_*` at a real relay.
- [x] Odoo master password: `odoo/odoo.conf` is rendered from `odoo.conf.tmpl` with `ODOO_MASTER_PASSWORD` (`admin_passwd`); `list_db` stays off. The rendered file is gitignored (it had been committed before; untracked now). Note: `/web/database/manager` still answers 200 with `list_db = False`; the master password is what guards create/drop/backup there, so block the path at the reverse proxy in production as well.
- [x] Least privilege for the service user: internal user + contact management + `belfry_pass.group_admin` only (was Settings, Sales manager, Accounting manager). The module grants `group_admin` read on `account.move`, `account.move.line` and `payment.transaction` (ACL rows, module 19.0.1.3.0). The smoke test's own Odoo calls run as `admin`.
- [x] Issuer pinning: Verifier2's `signature` policy trusts any x5c leaf, so the door and the bridge now accept only credentials whose `x5c[0]` is Belfry's own leaf (`credentials.issued_by_belfry`; a rotation appends old leaves). Closes §3e open item 2 for the verifier side; publishing the CA for wallets stays open.
- [x] Signed verifier requests: `gen-issuer-pki.sh` adds a verifier key and certificate (SAN `VERIFIER_SAN`, default `verifier.belfry.local`, walt.id refuses `localhost`) and renders `verifier/config/verifier-service.conf` with `clientId: x509_san_dns:...`, `key` and `x5c`. The request object is a signed JWT; the test wallet reads it. Encrypted responses (`direct_post.jwt`) are not enabled.
- [x] Verified live: `make smoke` 61 checks with the restricted service user (activation, revoke, login links, claim, door, bridge all through it), the activation mail delivered over SMTP to Mailpit with the setup and claim links, signed request objects accepted by the test wallet, revoked and foreign-issuer passes refused. `make test-onboarding` 67 tests, `make test-odoo` 46 tests.

Still open: encrypted responses and HAIP's authorization-code flow; CA publication; issuer key in a KMS and rotation; the management APIs stay unauthenticated behind the network.

## 3g. Deployment preparation: reverse proxy, public URLs, guide — built 2026-10-23

The public deployment itself needs a VM and a domain; everything up to that point is prepared and checked locally.

- [x] `infra/docker-compose.prod.yml`: Caddy (`caddy:2`, ports 80/443, Let's Encrypt) in front of everything, every other `ports:` reset, `restart: unless-stopped`. `infra/proxy/Caddyfile`: `odoo.`, `login.`, `apps.`, `relay.`, `bunker.`, `issuer.`, `verifier.`, `pass.<domain>` and the apex (NIP-05 `/.well-known/nostr.json` → Keycast with the tenant host, everything else → `odoo.`). Denied: `/issuer2/*`, Verifier2 session management (only `/verification-session/<id>/request|response` pass), onboarding `/admin/*`, `/hooks/odoo/*` and API docs, Keycast `/api/admin/*`, Odoo `/web/database/*`. HSTS and the usual headers.
- [x] `scripts/prod-env.sh <domain> <acme e-mail>` sets `BELFRY_DOMAIN`, `ACME_EMAIL` and every public URL, SAN and tenant variable in `.env`. New `ODOO_PUBLIC_URL`: bootstrap sets and freezes `web.base.url` (invite links and payment links in mails were built from the request host before).
- [x] `infra/DEPLOY.md`: steps, DNS records, Stripe, what the proxy does, checks after the first start, backups.
- [x] Checked locally: Caddy with its internal CA on the stack's network; 21 routing checks (allowed and denied paths per host, NIP-05 on the apex) pass; the merged overlay validates.

Open: the deployment run itself (and with it: Stripe test mode, a real wallet, publishing the CA); Caddy rate limiting for the public claim and bridge pages; the HTTP → HTTPS redirect is Caddy's default and untested here.

## 3h. Phase 2: Keycast fork patches, Blossom, policy kinds — built 2026-10-23

- [x] Keycast patch 0004: Belfry branding instead of Divine (API strings and e-mails, web brand, title, manifest, SVG logo and favicon, landing copy, navy theme; Divine icons, ATProto card and demo page removed). Patch 0005: key export for OIDC-only accounts (`auth_time` fact on sessions, export within `OIDC_REAUTH_WINDOW_SECS` of a fresh sign-in, `has_password` in the account status, "Sign in with Belfry to unlock" on the security page). Both compile (`cargo check -p keycast_api`), the API unit tests pass (oidc 9, reauth 1). **The image was not rebuilt** (per request); `make build-keycast` applies the series, then the smoke test's step 6c–6d exercise the login and a manual check of `/settings/security` the export. The seed tenant is renamed to Belfry by `policy.sql`.
- [x] Blossom (`infra/blossom/`, hzrd149/blossom-server pinned by digest): internal server with `requireAuth`, 25 MB, MIME allowlist and retention, mirror/media/list/dashboard off; `blossom-gate` (Caddy) in front of it forwards every PUT/DELETE to the onboarding service's `GET /blossom/auth`, which validates the kind-24242 event (Schnorr via `coincurve`, verb, expiry ≤ 24 h, `server` tag) and requires an active pass; denied paths are not forwarded at all. Smoke step 6f: upload with a Keycast-signed token works, no auth 401, wrong verb 403, mirror 404, after revocation 403 (and Keycast refuses to sign). Production route `blossom.<domain>` in the proxy.
- [x] Policy kinds, decided 2026-10-23: calendar is NIP-52 (31922, 31923, 31924, 31925), photos NIP-68 (20) with Blossom (24242), proposals both NIP-88 polls (1068, 1018) and NIP-23 long-form (30023) with NIP-22 comments (1111), chat is NIP-17 (13 seals, 1059 gift wraps, 10050 DM relay list; NIP-44 encryption and decryption through the signer, which Keycast's `allowed_kinds` policy never gates). Base kinds 0, 1, 5, 7, 10002, 22242, 27235. In `infra/keycast/policy.sql`; the smoke test signs one event of each app kind, gets a refusal for a kind outside the list (9735) and round-trips `nip44_encrypt`/`nip44_decrypt`. Marketplace and links stay open. The relay has no kind filter (whitelist only).

**Policy kinds: background (decision above).** Keycast authorizes signing per OAuth app through a policy (`policy:belfry-event` scope) whose `allowed_kinds` permission lists event kinds; other permission types are `content_filter`, `encrypt_to_self`, `decrypt_only`, `full_access`. NIP-44/NIP-04 encryption and decryption are separate RPC methods and are not gated by kinds. To finish the policy, decide:

1. **Which apps and protocols.** For each of the plan's apps one kind set: profile 0 and relay lists 10002; notes 1, reactions 7, deletions 5; directory = profiles plus follow lists 3; calendar (NIP-52) 31922, 31923, 31924, 31925; proposals: NIP-88 polls 1068 and 1018, or long-form 30023 with 1 for discussion; marketplace: NIP-15 (30017, 30018, 4) or NIP-99 classifieds 30402; photos: NIP-68 pictures 20 plus Blossom 24242; links: NIP-B0 bookmarks 39701 or NIP-51 lists 10003/30001; chat: NIP-29 groups (9, 10, 11, 12 plus 39000–39003 by the relay) or NIP-17 DMs (14, 13 seals, 1059 gift wraps, 10050), which also needs `nip44_encrypt`/`nip44_decrypt`. Without the decision the policy stays at the current set and clients get `kind not allowed` for anything else.
2. **One policy or one per app.** Keycast policies are global (`policies` table, no team); one `belfry-event` scope for all apps is simplest, a policy per app (chat may encrypt, the door app may only read) is tighter. The relay also needs its kind allowlist (rnostr `kinds` filter, currently open to whitelisted pubkeys).
3. **Encryption.** NIP-17 chat means the signer decrypts for the app on every message; decide whether a managed key may do that (`decrypt_only`/`encrypt_to_self` permissions exist) and whether the chat should rather be NIP-29 public groups on the community relay.
4. **Where the list lives.** `infra/keycast/policy.sql` (applied by bootstrap, idempotent) is the single source; the relay's `rnostr.toml` should carry the same kinds.

## 3i. Public test deployment on Hetzner — live 2026-10-23

`belfry.tofauti.net` (Hetzner cx33, `infra/hetzner/`, deployed together with a second Claude session that owns the VM provisioning, secrets and SMTP relay). Bootstrap and the full smoke test run against the public hostnames (`ODOO_URL`, `AK_URL`, `KC_URL`, `ONB_URL`, `BLOSSOM_URL`, `WALLET_MAP=""`, `ONB_ADMIN_EXEC=1`): 79 checks green, including claim, door, pass sign-in, Blossom and revocation over TLS.

Found and fixed on the way: bootstrap's verifier probe and Keycast admin calls hit paths the proxy denies (now a lenient probe and `docker compose exec` inside the Keycast container); the Authentik seed brand was not flagged default on the fresh install (blueprint pins it); Keycast's backchannel to Authentik produced an `http://` issuer, so the overlay unsets `OIDC_BACKCHANNEL_URL` and Keycast uses the public HTTPS issuer; the proxy also denies Keycast's dev inbox. The activation mail reaches the OVH relay; the smoke address's fake domain is refused (expected), a real recipient is part of the real-user test.

Wallet findings from the real-user test (2026-10-23): walt.id's web wallet (wallet.demo.walt.id, its older wallet API) speaks OpenID4VCI draft 13, Issuer2 1.2.0 only 1.0. Bridged without touching Issuer2: the proxy rewrites the appended well-known paths, the onboarding service serves the issuer metadata with `authorization_servers`, adds `c_nonce` to token responses and translates `{format, vct, proof}` credential requests (`/issuer-shim/*`, `clients/issuer.py`); Issuer2 allows pre-authorized codes without `client_id` (`preauth-anonymous`); the type metadata is also at `/.well-known/vct/vct/village-pass/v1`. The DIDroom wallet (Android) ignores pre-authorized offers and starts an authorization-code flow; with the pre-authorized offer it had no `issuer_state`, and Issuer2 forwarded it to walt.id's demo Keycloak (a leftover default). Built the same day: the claim page has a sign-in variant (`?signin=1`, an authorization-code offer whose `issuer_state` carries the participant's pass data); Issuer2's external login is Authentik (blueprint `belfry-issuer-app.yaml`, participants only, `ISSUER_OIDC_CLIENT_SECRET`, `issuer/config/authentication-service.conf.tmpl` rendered by `gen-issuer-pki.sh`); Issuer2's token call to Authentik goes through the onboarding service (`/issuer-shim/ak-token`; its Java client sends `Upgrade: h2c` over plain HTTP and Authentik and uvicorn's httptools then lose the form body, so the service runs uvicorn with `--http h11`); PAR and authorize without `issuer_state` are refused (Issuer2 would otherwise issue the profile's placeholder data). Smoke step 8b runs the flow with the test wallet (`signin-start`/`signin-finish`) and the participant's Authentik session. Not yet tried with the DIDroom app itself.

Open from the other session (2026-10-23): the VM's Odoo database was created in USD with the default country (invoices show "$" and "United States"; bootstrap sets EUR only on a fresh database, so the VM needs a recreated database or a one-off change before the event); NIP-46 bunker URLs handed out by Keycast carry `BUNKER_RELAYS=ws://keycast-relay:8080`, unreachable from phones, although Caddy serves `wss://bunker.<domain>` (needs a public relay URL for handed-out bunker URLs while the signer keeps the internal one; not built).

New setups name Odoo's host `shop.<domain>` instead of `odoo.` (decided 2026-10-23; `ODOO_HOST_PREFIX`, default `shop`, set by `prod-env.sh`); belfry.tofauti.net keeps `odoo.` through `ODOO_HOST_PREFIX=odoo` in its `.env`.

Open there: a real wallet against the public issuer and door, the HTTP→HTTPS redirect is Caddy's default, Stripe test mode on the public Odoo, publishing the CA for wallets.

## 3j. Participant login by magic link, no passwords — built 2026-10-23

Decision (user, 2026-10-23, before the real-user test): participants sign in by e-mail magic link only and never get a password; the activation mail carries a one-time sign-in link instead of the set-a-password link; akadmin keeps a password on authentik's default flow. Supersedes the password part of §3d.

- [x] Blueprint `belfry-magic-link-flow.yaml`: flow `belfry-magic-link` as the default brand's authentication flow (login page and every OIDC authorize, Keycast included): identification by e-mail with the pass source on the same page, e-mail stage (one-time link, 10 minutes, template `infra/authentik/templates/belfry/magic_link.html` mounted at `/templates`), user login (14-day remember me). Unknown addresses: "Failed to authenticate.", no mail (no mail to arbitrary addresses; the page does tell whether an address has an account). authentik 2026.8 asks for a "Continue" click before it uses an e-mail token, so mail scanners cannot consume links.
- [x] `belfry-account-setup-flow.yaml`: the recovery flow behind the activation link is now only a user login; the password prompt, policy and user write are removed by the blueprint (`state: absent`). `LOGIN_LINK_DAYS` default 30 → 7.
- [x] bootstrap writes the SMTP settings (`SMTP_*`, `secrets/smtp_password`) into the e-mail stage over the API and checks the brand's authentication flow; Odoo `belfry_pass` 19.0.1.4.0: activation mail text (sign in once, later by e-mail at `belfry.login_url`), template `noupdate` off with a pre-migration that releases the old record.
- [x] Tests: `keycast-oidc-login.sh` takes a signed-in Authentik cookie jar instead of a password; smoke 6c (sign-in link, no password field on the login page, consumed link refused), 6d (Keycast in that browser), 6h (magic link from the login page through Mailpit, unknown address, link works once). Local: smoke 95 ok / 0 failed, onboarding 78 tests, Odoo 36 tests. Checked by hand: Keycast "Sign in with Belfry" without a session → magic link → back in Keycast, signed in; the same link opened in another browser ends in Keycast's 400 (flow cookie missing).

Open from this slice: every new identification submit mails a known address again (authentik limits only the Resend button), so the Caddy rate limit for `login.<domain>`'s flow executor (open in §3g) is the lever against mail flooding; Keycast's 400 page for a link opened in another browser is bare (a friendlier message would be a fork patch); the akadmin password login is only reachable by its direct URL; passkeys for participants are not offered.

## 3k. Steward pages: Belfry's own UI, Odoo as backend only — built 2026-10-23

Decision (user, 2026-10-23, after the first real-user test report): stewards do not work in Odoo's back office; a small Belfry UI covers their tasks and talks to Odoo over JSON-RPC. Odoo's UI stays for admins and accounting. The test report also found the invite list failing with "Expected singleton" (fixed in 908da84, belfry_pass 19.0.1.4.1).

- [x] `services/onboarding/belfry_onboarding/steward.py`, `/steward` on `pass.<domain>`: sign-in through Authentik (blueprint `belfry-steward-app.yaml`: OIDC provider and application `belfry-steward`, group `belfry-stewards`, policy binding), magic link like participants; code exchange and userinfo server to server (client secret + PKCE), HS256 session cookie on `/steward`, CSRF token per form. Invites: create with a note, copy link, revoke. Passes: mark paid, send a new sign-in link, revoke. CLI `add-steward <e-mail> [name]`.
- [x] Odoo `models/belfry_steward.py`: `belfry.invite.belfry_steward_create` (invite with `steward_email`, `steward_id` = the Odoo user with that login if any), `belfry.pass.belfry_steward_mark_paid` (registers the invoice payment in sudo, writes `manual_paid_by`/`manual_paid_at`); both only for Belfry admins (the service user).
- [x] Onboarding: a payment without a Stripe transaction is accepted when the pass carries a steward's `manual_paid_by` (also when a card attempt failed before); anonymous manual payments stay refused (`ALLOW_MANUAL_PAYMENTS` off). This also corrects the first test plan, whose step 3 ("Register Payment" in Odoo) would not have activated the pass.
- [x] bootstrap re-applies `belfry-steward-app` on every run and waits for a newer `last_applied`: the worker applies a new blueprint file as soon as it appears, possibly before the container has `STEWARD_OIDC_CLIENT_SECRET`, and then keeps the placeholder secret.
- [x] Tests: onboarding 90 (11 new: login with PKCE and client secret, non-steward refused, forged state, CSRF, invite, revoke, mark paid activates, mark counts after a failed card attempt, add-steward), Odoo 41 (4 new). Local end to end: steward magic-link login → invite → participant form → Mark paid → pass active with the sign-in mail; a participant is refused at Authentik's authorize step.

Open from this slice: no smoke step for the steward pages yet (the end-to-end check is a scratch script); the pages have no search or paging (latest 50 invites, 100 passes); stewards cannot be added from the UI (CLI only); the Keycast 400 limitation of §3j applies to the steward login as well (open the magic link in the same browser).

## 3l. Branding: Open Source Village, run by Belfry — built 2026-10-23

Decisions (user, 2026-10-23): every participant-facing surface says **Open Source Village, run by Belfry**, dark theme only, mobile first, the look of opensourcevillage.org (green greys, lime accent, Geist); Belfry stays the operator name (footers, issuer display, `iss`); no DNS change yet; a shared `brand/` folder is the single source; stewards never edit text in the Odoo UI (copy lives in code). Recorded in the live doc.

- [x] `brand/`: logos and favicons from the site, Geist woff2 (OFL) with `fonts.css`, `tokens.css` (`--osv-*` variables), a rendered wordmark PNG for mails and the Odoo company logo, `flow-background.svg`; `brand/README.md` lists every place that carries a copy of the values. The onboarding service serves the folder at `/brand/*` (Compose mounts `../brand`, env `BRAND_DIR`; a local checkout finds it itself), so `pass.<domain>/brand/...` is the URL everything else uses.
- [x] Onboarding pages (claim, door, pass sign-in, errors): one dark frame in `claim.page()` with wordmark and footer; QR on a white tile. Type metadata and Issuer2's `credential-issuer-metadata.conf`: "Open Source Village Pass", issuer "Open Source Village, run by Belfry", background `#2b3a31`, logo URL (`BELFRY_BRAND_URL`); the wallet shows the OSV mark.
- [x] Authentik: blueprint `belfry-brand.yaml` (title, logo, favicon, flow background from `BELFRY_BRAND_URL`, theme forced dark, custom CSS mapping PatternFly/authentik variables to the palette); flow titles "Sign in to Open Source Village"; the magic-link mail is a standalone dark template, its `cid:logo` PNG mounted over authentik's; subject "Your Open Source Village sign-in link". Bootstrap re-applies the brand blueprint when the stored logo is not under the configured brand URL (the worker can apply the file before the container has the env var).
- [x] Keycast patch 0004 regenerated: dark OSV theme in `app.css` (Divine's variable names, OSV values, Tailwind greys remapped), popup and claim pages recoloured, Google Fonts replaced by self-hosted Geist, OSV logos, favicons and manifest, `BRAND_NAME` and `brand.ts` "Open Source Village", captions "run by Belfry", landing copy rewritten for passwordless accounts (no "Get Started"/register, no e-mail-and-password claims); `OIDC_PROVIDER_NAME=Open Source Village` ("Sign in with Open Source Village"); the seed tenant renamed in `policy.sql`. Image rebuilt and running locally; `cargo test -p keycast_api --lib` 307 tests.
- [x] Odoo `belfry_pass` 19.0.1.5.0: `brand_frontend_layout` inherits `web.frontend_layout`, so the invite page, the payment page and the portal load the two CSS files from `<belfry.public_url>/brand` and get the palette as Bootstrap overrides (the invoice preview stays a white document); invite page with wordmark and footer; activation mail as a dark card with the wordmark PNG, "Sign in to Open Source Village", "Sign in with Open Source Village"; bootstrap sets the company logo. Odoo's back office is unchanged (stewards use the steward pages, §3k).
- [x] Checked locally: `make smoke` 85 ok / 0 failed on the rebranded stack (new Keycast image, bootstrap with the module upgrade and company logo), onboarding 79 tests, Odoo 41 tests; headless-Chromium screenshots at 900 px and 390 px of the Authentik login, door, claim error, Keycast login and landing, invite and payment pages: all render dark, legible and single-column on the phone width.

Open: Odoo's payment page keeps Odoo's structure (only recoloured; "No payment method" locally is the missing Stripe provider, not the theme); "Powered by authentik" stays in the login footer (authentik's own markup); Keycast's technical-docs page keeps its own dark palette; the OSV `favicon.ico` is used as is for Keycast and the onboarding pages; the authentik custom CSS is a mapping of PatternFly variables and may need touch-ups on pages not screenshotted (consent, user settings); the mail templates render as dark cards, which Gmail's dark mode may invert partially.

## 3m. Real-user test follow-ups: ticketing colours, apps colours, e-mail and pass card — built 2026-10-23

From the first real-user test on the VM (screenshots from the user, requests relayed by the other session):

- [x] Odoo (`belfry_pass` 19.0.1.5.1, commit f77ec10): the invite page's pass rows were black on the dark card (Odoo's frontend forces black on plain list-group items with a 0,3,0 selector; overridden with `#wrapwrap .list-group-item`); the navbar keeps the page background so the company logo PNG (rendered on `#2b3a31`) no longer shows as a lighter box; the invoice page hides the portal chatter and the sidebar's "Send message" (the chatter boots `portal.assets_chatter` in Odoo's light theme and its messages would reach only the service user); the Pay modal's summary box (`text-bg-light`) is themed. Note for the theme: Odoo 19's compiled Bootstrap uses CSS variables **without** the `bs-` prefix (`--list-group-color`, `--body-color`), so the `--bs-*` overrides in `brand_frontend_layout` are inert; the explicit rules do the work.
- [x] Bootstrap sets the company currency (`COMPANY_CURRENCY`, default EUR) on a fresh database only: Odoo refuses the change once journal items exist and the script then warns. Verified on a throwaway database. **The local and the VM databases were created in USD and keep "$" until they are recreated**; the company country is also still the default (United States on the invoice header).
- [x] Keycast fork: every `.btn-primary` and the other lime buttons were white on lime (`--color-divine-on-accent: #1a231d`, folded into patch 0004, with the "Divine Login" badge renamed "App login"); the "Please verify your email address ()" banner is hidden for accounts without an e-mail (folded into 0005). New patch 0006: on every OIDC login the ID token's `email` is stored on the account as verified (`OIDC_TRUST_EMAIL`, default true; an address held by another account of the tenant is left alone), `GET /api/user/pass` reads the pass behind the account's `vine_id` from the onboarding service's `GET /admin/partners/<id>/pass` (the other session's 5124eca; `BELFRY_PASS_INFO_URL`, `PASS_INFO_TOKEN`) and the dashboard shows a "Your pass" card (tier, validity, state, member number, "Add to your wallet" while active and unclaimed; hidden on 404/503); `NIP05_DOMAIN` now wins over the tenant host for the dashboard's NIP-05 name (the apex proxies `nostr.json` to the tenant) and Compose passes it to Keycast. Checks: `cargo check`/`cargo test --lib` (oidc, reauth, pass, nip05), `bun run check` 0 errors, `bun run build`; `make build-keycast`; `make smoke` 85 ok / 0 failed with step 6d checking the stored e-mail and the pass endpoint; headless screenshots of the dashboard (1200 px and 390 px) and the security page with the smoke test's session (`SAVE_KC_JAR`).
- Found on the way: a stale `belfry-onboarding` image answered 404 for the new route until `docker compose build onboarding`; `up -d` alone does not rebuild.

Open: the VM database's currency and country (a reset of the Odoo database or a one-off SQL change before the event); the local `/my/invoices` redirect carries a double slash (`web.base.url` + `/`); Keycast's `OIDC_TRUST_EMAIL` relies on Authentik putting `email` in the ID token (it does with the default scope mapping).

## 3n. NIP-46 for apps outside the network: public bunker relay — built 2026-10-23

The user asked for the two open items of §3m to be built; the other session took the DIDroom item (376d0b3), this one the bunker relay.

- [x] Keycast patch 0007: `BUNKER_PUBLIC_RELAYS` is the relay as clients reach it and goes into every handed-out `bunker://` URL (OAuth token response, "create bunker", team and group bunkers) and into the NIP-05 discovery's `nip46.relay`, overriding the tenant's stored `relay` setting (Keycast seeds it with the internal address when the tenant is created, so the env var alone was not enough). The signer keeps `BUNKER_RELAYS=ws://keycast-relay:8080`; both name the same relay (`bunker.<domain>` is Caddy in front of `keycast-relay`). Compose default `ws://localhost:3080` (the relay's host port), `prod-env.sh` sets `wss://bunker.<domain>`; **existing `.env` files need the line** (gen-secrets appends it).
- [x] `belfry_onboarding.nip46check`: a NIP-46 client in Python (NIP-44 v2 with `coincurve` and `cryptography`, Schnorr-signed kind-24133 requests over `websockets`) for the smoke test: parses the bunker URL, connects with the secret, `get_public_key`, `sign_event` kind 1 (signature verified), a kind outside the policy refused. `--map` rewrites the relay host like the test wallet's; `--debug` prints the relay traffic. 28 unit tests (padding table from the NIP-44 vectors, round trip, tampering, signatures, URL parsing).
- [x] Smoke step 6i: NIP-05 discovery advertises the public relay, the OAuth token's bunker URL names it, and the client signs through it from the onboarding container (`BUNKER_MAP=localhost:3080=keycast-relay:8080` locally, `BUNKER_MAP=""` on a public deployment). `keycast-oidc-login.sh` prints `BUNKER_URL=` with `PRINT_TOKEN=1`.
- [x] Checked through TLS as well: a throwaway Caddy (`tls internal`) on the stack's network in front of `keycast-relay`, the client with `--map ws://localhost:3080=wss://localhost:8443 --insecure-tls`: connect, `get_public_key` and `sign_event` pass, so the production `bunker.<domain>` block (plain `reverse_proxy keycast-relay:8080`) carries the WebSocket.
- [x] Found and fixed: for 30 s after a Keycast container was replaced, NIP-46 requests whose bunker pubkey hashed to the dead instance were dropped silently (Keycast's signer hashring in Redis keeps a member for `STALE_THRESHOLD_SECS = 30` without heartbeat; the request handler skips "another instance's" bunkers without an error event). Docker's default 10 s stop timeout killed Keycast during its 15 s pre-drain phase, before the graceful shutdown deregisters, so every `up -d`/`restart` opened that window. `stop_grace_period: 90s` on the service lets it leave the ring (an orderly restart now takes about 16 s and logs "Deregistered instance"); the check client retries `connect` three times like real clients, for the crash case.
- Found on the way: Keycast's signer uses one subscription for all kind-24133 events on `BUNKER_RELAYS` and ignores the relays stored per authorization, so nostr-login's client-chosen relay (`/api/connect/<nostrconnect://…>`) only works when the client names Belfry's relay; the NIP-05 discovery now tells it which. The signer signs and encrypts responses with the derived bunker key (not the user's key).

## 3o. Community chat app: Coracle at chat.<domain> — built 2026-10-23

User request: "Add a nostr calendar and a nostr chat app." Split with the other session: it hosts the calendar (formstr `nostr-calendar`, `infra/apps/calendar`), this one the chat. Both are static builds behind Caddy, pinned to our relays, with participants signing in through Keycast's bunker (NIP-46). Flotilla was ruled out: it is NIP-29 and rnostr has no groups.

- [x] `infra/apps/chat/` (README there): Coracle (MIT, Svelte, NIP-17 private messages) at commit `2bdb17f5` (2026-10-21), built by a two-stage Dockerfile: clone at the pinned commit, brand logo and wordmark copied from `brand/` (Compose `additional_contexts`), `osv.css` appended (dark text on the lime accent; Coracle puts white on it), the Plausible analytics script and its CSP entry removed, page title set, `.env` rendered by `make-env.sh` (community relay as default, indexer and search relays; `BUNKER_PUBLIC_RELAYS` as signer relays; Belfry's Blossom; no default follows, onboarding lists, zaps, market or DVM relays; the OSV palette as both themes), upstream's `build:web` and `nginx.conf`. Compose service `chat` (`--profile app`, host port 3300 locally: 3200 is the calendar), Caddy `chat.<domain>` → `chat:80`, `CHAT_PUBLIC_URL` and `BLOSSOM_PUBLIC_URL` in `.env.example` and `prod-env.sh`.
- [x] `infra/keycast/policy.sql`: the kinds both apps sign added to `belfry-event` (3, 6, 16, 1984, 1985, 10000, 10003, 10063, 30000, 30002, 30003, 30015, 30078, 31890; calendar extras 31927, 32069, 32123, 32678). Bootstrap applies it on every run.
- [x] Checks: smoke step 6j (the app answers with the village title, the bundle carries the community and the bunker relay and no upstream default relay); `infra/scripts/chat-login-check.mjs` drives headless Chromium through `/login/bunker` with a bunker URL: the session for the expected pubkey appears, and the signer log shows Coracle's `connect`, `get_public_key` and a signed kind 22242, i.e. NIP-42 AUTH to the community relay. `SMOKE_KEEP_PASS=1` leaves the smoke test's participant active (skips step 11) and `SAVE_BUNKER_URL` keeps its bunker URL for such browser checks. Local: smoke 90 ok / 0 failed with the keep flag.
- Sign-in with the least friction, as checked: a `bunker://` URL from the apps dashboard ("Connect to Nostr App", one per app, since Keycast binds the first client that connects: the OAuth bunker URL the smoke test's NIP-46 check had used was refused for a second client). NIP-05 handle discovery (nostr-login style) does not apply: neither app uses nostr-login, and Keycast authorizes per app with a secret. A "Sign in with Open Source Village" button in the apps (Keycast's OAuth popup handing the bunker URL to the app) would remove the copy-and-paste; it is a patch per app and not built.
- Open: no NIP-17 message was sent end to end in a browser yet (needs two active participants; the policy allows kind 13 and NIP-44 is not kind-gated, and the smoke test signs those through the HTTP RPC); Coracle's `switch_relays` NIP-46 call is unknown to Keycast (harmless); the OSV palette is applied through Coracle's theme variables only, so some upstream greys remain; the "What's a signer?" help text still describes generic signers.

## 3p. Community calendar app: Calendar by Form* at calendar.<domain> — built 2026-10-23

User request: "Add a nostr calendar and a nostr chat app" (chat: §3o, the other session). Calendar choice: formstr-hq/nostr-calendar (MIT, NIP-52, private events as NIP-59 gift wraps, NIP-46 sign-in, active); Plektos was the alternative (public meetups, no private events).

- [x] `infra/apps/calendar/` (README there): the app at commit `313596c7` plus one Belfry patch (OSV palette, lime accent and dark mode as defaults, Geist and favicon from `/brand`, OSV logo; sign-in dialog with the remote signer first and the apps-page hint, local keys hidden; sidebar Apps/About to the deployment, Formstr's privacy policy and contact form hidden), built with pnpm 10 (the build runs the app's lint and typecheck) and served by Caddy. Build arguments: `COMMUNITY_RELAY_URL` (new, `prod-env.sh` sets `wss://relay.<domain>`), `BUNKER_PUBLIC_RELAYS`, the brand URL, `KEYCAST_APP_URL`; no calls to Formstr's mail API. Compose service `calendar` (profile app, port 3200 locally), proxy host `calendar.<domain>`.
- [x] Found in the browser test: the pinned app locks `@formstr/local-relay` 0.6.0, which never answers NIP-42 AUTH challenges, so on our AUTH-only relay every read and write came back `auth-required` while the UI looked fine. The patch moves it to 0.6.5 (AUTH answered, `OK true`).
- [x] Checks: smoke step 6k (title, the bundle carries the community and bunker relays, no Formstr relays or API); `infra/scripts/calendar-check.mjs` drives headless Chromium: bunker sign-in with a Keycast "Connect to Nostr App" URL, create a calendar (kind 32123) and an event, then a fresh browser profile reads both back from the relay.
- Open: the busy list (kind 31926) and scheduling-page list (32680) are refused by the `belfry-event` policy (the app logs it, events still save); events are private by default, public events (31923) reach every participant; the calendar is not in the smoke test's browser part (Chromium is not on the VM).

## 3q. Single sign-on for chat and calendar, silent when already signed in — built 2026-10-23

User request: a "Sign in with Open Source Village" button in the apps, and an automatic check on load so a participant who is already signed in never clicks. Split with the other session: it built the calendar side, this one Keycast, the chat and the checks. Decisions (recorded in the live doc): the apps use Keycast's OAuth (authorization code + PKCE) and connect with the token response's `bunker://` URL; the automatic check is a top-level redirect with `prompt=none`, not a hidden iframe (no framing of Keycast or Authentik, no third-party cookie trouble in Safari); Belfry's own apps are trusted clients that skip Keycast's consent screen.

- [x] Keycast patch 0008: `prompt=none` on `/api/oauth/authorize` never renders a page. Signed in and the app approved or trusted: a code. Signed in otherwise: `error=consent_required`. Not signed in: one round through `/api/auth/oidc/start?prompt=none` to Authentik with `prompt=none`; the callback comes back with a Keycast session, or with `oidc_failed=<reason>` (no Authentik session, no signing account), and the app gets `error=login_required`. `KEYCAST_TRUSTED_CLIENTS` (Compose default `belfry-chat,belfry-calendar`) skips consent only for registered clients; with the password login hidden, a trusted app's button goes straight to Authentik's sign-in page instead of Keycast's page with a second button. 4 new unit tests; image rebuilt.
- [x] Bootstrap registers `belfry-chat` and `belfry-calendar` in Keycast's `registered_clients` with `<CHAT_PUBLIC_URL>/oauth/callback` and `<CALENDAR_PUBLIC_URL>/oauth/callback` as the only redirect URIs, for the tenant of `KEYCAST_TENANT_DOMAIN`. Found on the way: Keycast picks the tenant from the Host header, and the seed tenant 1 (`login.divine.video`) is not ours; the first version registered there and Keycast kept the consent screen. **Re-run bootstrap after changing the app URLs.**
- [x] Chat (`infra/apps/chat/patches/`, new): `src/belfry/sso.ts` (PKCE, state check, token exchange, NIP-46 connect like the bunker screen), the silent attempt in `main.js` before the app mounts (once per tab, sessionStorage flag; on failure the login screen opens), the button first on the login screen. Build arg `KEYCAST_APP_URL`; client `belfry-chat`.
- [x] Calendar (infra/apps/calendar, patch, the other session, 988350b): src/belfry/sso.ts. Authorization code + PKCE against Keycast, client belfry-calendar, redirect <origin>/oauth/callback; the token response's bunker_url goes to the app's own NIP-46 login. On load without a signer, one silent attempt per tab (prompt=none, sessionStorage flag); any error (login_required, consent_required, state mismatch, failed exchange) opens the sign-in dialog, where "Sign in with Open Source Village" is first and the bunker-paste option below it. Build arg VITE_KEYCAST_URL (KEYCAST_APP_URL; empty disables both). Check: infra/scripts/calendar-check.mjs with SSO=1 and COOKIE_JAR (the participant's Authentik session) expects the calendar without a dialog.
- [x] Checks: smoke step 6l (`infra/scripts/silent-signin-check.sh`): no session → `login_required` after one silent round through Authentik; Authentik session only → code without a page, Keycast session set, token with a bunker URL and CORS for the app origin, signing works; Keycast session → code in one hop; an unapproved app → `consent_required`; `belfry-chat` with a foreign redirect URI → 400; the button → Authentik → code without Keycast's page or consent. Local: smoke 95 ok / 0 failed. Browser (`chat-login-check.mjs` with `-`, `SAVE_AK_JAR` from the smoke test): opening the chat with the participant's Authentik session signs in with no click; without one, the login screen offers the button and it opens Authentik's sign-in page. The other session ran the same for the calendar (signed in without a dialog, calendar and event loaded; no session → dialog, no loop).

Open from this slice: all four items were closed in §3r; deployed on the VM by the other session.

## 3r. Logout everywhere, sign-in errors, the pass landing page — built 2026-10-24

User requests after the SSO slice: close §3q's open items (logout keeps you signed in, authorizations pile up, tenant name, relay warning), make the bare "State mismatch" page friendly and branded, and give `pass.<domain>/` a page instead of raw JSON.

- [x] Keycast patch 0010: `GET /api/auth/oidc/logout?return_to=` ends the Keycast session and goes through Authentik's end-session endpoint; the keycast provider's new invalidation flow `belfry-signer-logout` (blueprint) really logs out of Authentik (authentik's default provider invalidation flow only shows "you've logged out of <app>") and redirects to `/api/auth/oidc/logged-out`, which returns to the app (same-origin path or a registered client's origin). The dashboard's "Sign out" uses it. `POST /api/oauth/revoke` (RFC 7009, refresh token) lets an app revoke its own authorization; for trusted clients each token exchange also revokes authorizations never connected within 10 minutes and connected ones beyond the five most recently active. One flow cookie per sign-in: the "State mismatch" came from a second sign-in in the same browser (for example the chat's silent try in another tab while a magic link was pending) overwriting the single flow cookie. Error pages in the OSV look with plain messages and "Sign in with Open Source Village".
- [x] Chat patch 0002: logout revokes the app's authorization and goes through Keycast's logout, back to `/notes` signed out, without a silent sign-in on return. **The calendar's logout is the other session's** (contract: revoke with `{token: refresh_token, client_id}` to `<KEYCAST>/api/oauth/revoke`, then navigate to `<KEYCAST>/api/auth/oidc/logout?return_to=<app URL>`; set the silent-try flag first).
- [x] Chat patch 0003 (real-user test on the phone, 2026-10-24): "Log out" as a tile in the mobile menu; upstream Coracle hides it under the account name (menu → name → small "Logout").
- [x] `keycast-relay`: `RELAY_URL` = the public bunker relay URL (`BUNKER_PUBLIC_RELAYS`), so NIP-42 AUTH with the URL clients use is accepted (was compared with `ws://localhost:8080`).
- [x] `policy.sql` renames the tenant of `KEYCAST_TENANT_DOMAIN` as well (bootstrap passes it as a psql variable).
- [x] Onboarding: `pass.<domain>/` is an OSV page (what the site is, how to claim, links to the account page, chat and calendar from `APPS_PUBLIC_URL`, `CHAT_PUBLIC_URL`, `CALENDAR_PUBLIC_URL`); unknown addresses answer browsers with an HTML 404, API clients keep JSON.
- [x] Checks: `silent-signin-check.sh` steps 7 (two sign-ins at once), 8 (branded error page), 9 (revoke stops signing within the HTTP RPC's 5 s recheck), and the logout (`LOGOUT_ONLY=1 LOGOUT_JAR=…`, smoke step 6m in 6h's magic-link browser: Keycast → Authentik end-session → back at the app, Authentik session ended, the silent check answers `login_required`). Local: `make smoke` 97 ok / 0 failed; `cargo test -p keycast_api --lib` (oidc 15); onboarding 134 tests; `chat-login-check.mjs` with `LOGOUT=1`: silent sign-in, logout, signed out, no automatic sign-in, Authentik session gone, the chat's authorization revoked. Screenshots at 390 px of the Keycast error page, the landing page and the 404.

Open: the calendar's logout (other session); the logout is a GET (any page can sign a participant out, a nuisance rather than a risk); after the logout the participant lands on the chat's public feed, not on its login screen.

Follow-up (2026-10-24, real-user test): after signing in, participants land on Keycast's dashboard at `apps.<domain>`, which linked to neither app. Keycast patch 0009 adds "Community apps" cards (Chat, Calendar) at the top of the dashboard, from `BELFRY_COMMUNITY_APPS` (Compose builds it from `CHAT_PUBLIC_URL` and `CALENDAR_PUBLIC_URL`).

Follow-up (2026-10-24): Coracle answered every sign-in with "We're having a hard time finding your profile": after login it waits for the profile, the relay list (10002) and the follow list (3), and participants only had the profile. Activation now publishes those starter events, plus the NIP-17 DM relay list (10050) that senders need, each only if the relay does not have that kind for the key yet (a participant's own choices are never overwritten), with `COMMUNITY_RELAY_URL` (the public relay URL) in the lists. `python -m belfry_onboarding publish-starter-events` backfills existing passes. Found on the way: rnostr's rate limiter (60 events a minute) is per client IP, and behind Caddy every browser shared the proxy's IP; `rnostr.toml` now sets `real_ip_header = "x-forwarded-for"`. The onboarding service's own connections still share one key, so a backfill of more than about 15 passes a minute hits the limit; rerun it.

Follow-up (2026-10-24): getting back to the apps page and installing the apps. The user chose a shared app bar and home-screen apps. `brand/osv-bar.js` is a 40 px Open Source Village bar fixed on top of the chat and the calendar: "Apps" goes back to Keycast's apps page in the same tab, then one link per app, the current one highlighted. Its list comes from `x-community-apps` in Compose, the same list as the dashboard's cards. Each app moves its own top parts down by `--osv-bar-h` (Coracle through `--safe-area-inset-top`, the calendar through `--safe-area-top`, calendar patch 0002). Each app also gets its own home-screen icon (`brand/apps/`, `brand/make-app-icons.py`: the app's symbol in lime with the OSV mark cut out, on the dark green) and a manifest named "Chat" / "Calendar". The calendar's sidebar "Apps" entry now opens in the same tab. Checked in headless Chromium at 1280 px and 390 px, signed in: both apps sit below the bar; dialogs and the chat's login modal cover it. The calendar logs out everywhere too (patch 0003, Keycast 0010's contract): checked with `calendar-check.mjs` `SSO=1 LOGOUT=1`.

## 3r. Anonymous polls at vote.<domain> — built 2026-10-24

User request (relayed by the other session, which took the Cashu mint, the wallet and self-custody): anonymous voting, any participant opens a poll, one anonymous vote per active pass (Semaphore, the plan's Phase 6 item). Details in `services/vote/README.md`. Decisions taken while building (recorded in the live doc):

- One small Node service (`services/vote`, API and web app, SQLite) instead of Python: Semaphore's proof verification and Poseidon Merkle tree are JavaScript.
- The voting key (Semaphore identity) stays on the device, with an export code for another device; no backup encrypted to the Nostr key, because Keycast holds managed keys and Belfry could then link votes to people.
- One group per poll: registered identities of active passes join when it opens, later registrations join open polls; a pass's entry in a poll never changes (one nullifier per pass and poll even after a key change); all of the poll's roots are accepted while it is open.
- Results only after the poll closes; then every proof is published for re-verification. Tree depth 10 (1024 voters per poll), artifacts served by the app with pinned hashes.
- Votes are not Nostr events (a signature would deanonymise); the app needs only kinds 27235 (NIP-98) and nothing new in the policy.

- [x] Onboarding `GET /admin/active-passes` (bearer `PASS_INFO_TOKEN`).
- [x] `services/vote`: server (NIP-98, polls, groups, proof verification), web app (sign in with Open Source Village, silent once per tab, set-up, new poll, vote, results, voting-key export/import, logout everywhere, the app bar), icon `brand/apps/vote-*`.
- [x] Compose `vote` (port 3500 locally), Caddy `vote.<domain>`, `VOTE_PUBLIC_URL`, Keycast client `belfry-vote` registered and trusted, "Vote" in `x-community-apps` (dashboard cards and app bar).
- [x] Checks: `npm test` 5 tests with real proofs; smoke step 6n; `infra/scripts/vote-check.mjs` in Chromium at 390 px: silent sign-in, set-up, new poll, vote counted. Local smoke 115 ok / 0 failed.

Open: Belfry sees IP addresses and request times, so it could correlate a vote with a signed request from the same address (a relay or a delay in the app would blur it); revoked passes stay in the groups of polls already open; no poll moderation or early close; polls are not announced on Nostr; the chat and calendar images need a rebuild to show "Vote" in their bar.

## 3s. Village tokens and the wallet — built 2026-10-24

User request: "voting, self custodian nostr key and a cashu mint with tokens without value. With the pass each holder receives a certain amount of cashu tokens, 50 tokens per day"; then "The cashu wallet should be derived from the same source as the user's nostr key. Users can also bring their own. The relevant pub key is shared via the DID". Decisions (live doc rev 60–61): tokens have no monetary value, minted all at once at activation (50 × pass days), wallet = cashu.me at `wallet.<domain>` plus nutzaps; the wallet derives from the Nostr key and restores on sign-in; the receiving key is published as kind 10019 (it was to go into the member's DID document with Phase 4; DIDs were dropped on 2026-10-24, so kind 10019 stays the only place). Voting is the other session's (§3r, polls).

- [x] Mint (`infra/mint/`, 980ae57): cdk-mintd 0.18.2, fake payment backend, unit `osv`, custom NUT-04 method `belfry`; the proxy closes `/v1/mint/*` and `/v1/melt/*` at `mint.<domain>`, so only the onboarding service mints and participants only swap. `MINT_SEED` in `.env`.
- [x] Issuance (onboarding `clients/mint.py`, Odoo `belfry_pass` 19.0.1.6.0): activation mints 50 × pass days onto the pass (`cashu_token`, `cashu_amount`), `reconcile` retries; the activation mail and the claim page link `pass.<domain>/tokens/<claim token>`, which opens the wallet with the token or says it is in a wallet already. Smoke 8c.
- [x] Keycast patch 0011 `derive_secret`: HKDF-SHA256 of the user's key per purpose (salt `keycast/derive/v1`), allowed per client by `KEYCAST_DERIVE_SECRETS` (`belfry-wallet:cashu-wallet-v1|cashu-nutzap-v1`); test vector in the patch. `belfry-wallet` is a trusted, registered client. Policy `belfry-event` gains 9321 and 10019.
- [x] Wallet (`infra/apps/wallet/`, cashu.me 002a0a36 + two patches): village mint built in, onboarding skipped, unit shown as tokens, no Lightning/on-chain, village theme, app bar and icon (`brand/apps/wallet-*`); "Sign in with Open Source Village" (silent once per tab): seed from `cashu-wallet-v1` (tokens held before are swapped onto it; a new device restores them), nutzap key from `cashu-nutzap-v1` (even Y), kind 10019 published, incoming 9321 redeemed (spent ones skipped), "Send to a participant" (npub or NIP-05). Signing over Keycast's HTTP RPC with the access token. No bitcoin price, no audits, no Nostr mint backup.
- [x] Checks: smoke 6o (silent sign-in of the wallet client, `derive_secret` stable and per purpose, refused for other purposes and for the chat client), 8c; local smoke 110 ok / 0 failed; `infra/scripts/wallet-check.mjs` with two participants: silent sign-in, keys derived, 10019 published, a token link received, a 5-token nutzap from A to B redeemed, balances restored in fresh browsers.

- [x] Self-custody (Keycast patch 0012, decisions in the live doc rev 62): on the dashboard's security page the participant signs a challenge with the key they want to keep (browser extension, or a pasted key that never leaves the browser). Their current key: Keycast deletes its copy ("take over"). Another key: the account moves to it (NIP-05 name, sign-in link, e-mail, Odoo link), the old key is deleted, and the onboarding service (`/hooks/keycast/key-changed`) moves the pass's npub and the relay whitelist; votes key on the pass id. Afterwards the apps sign in with the participant's own signer; "Sign in with Open Source Village" answers `access_denied`/`self_custody` (silent) or "Use your own signer". Smoke 10b (`self-custody-check.sh`): export, take over (key_held false, export refused, apps refused), move to a new key (NIP-05 and pass follow, signing in again lands on the moved account). Onboarding 147 tests.

Open: a self-custodial participant keeps the derived wallet only by computing the HKDF themselves (README shows it), the wallet has no "use my own key" path (nutzaps need the participant's own signer for kind 10019/9321); kind 7376 redemption history (NIP-60) is not published, the wallet remembers redeemed nutzaps locally and skips spent ones; not deployed on the VM yet (the mint is).

## 3t. Security review; recoverable participant keys — 2026-10-24

The user asked for a security review of the architecture and the VM (read-only on the VM) and decided: participant keys derive from a custody seed under the stewards' YubiKeys (live doc rev 63).

Review findings, highest first: no backups at all (Postgres, Odoo filestore, master.key, mint data, issuer keys, .env); Keycast's master.key on the same disk as its database; the issuer CA's private key on the VM; Authentik and Odoo admin UIs public with password only; one flat Docker network for all 20 containers; unpinned images (`:latest`, floating tags, git clones in builds); most containers run as root; NOPASSWD sudo + docker group on one shared SSH account; no rate limits; no monitoring; long-lived shared tokens (hook token in the query string); no CSP on the browser apps (the wallet keeps its seed and tokens in localStorage); Mailpit still running. In place: Hetzner firewall 22/80/443, key-only SSH, no root login, unattended upgrades, secrets 0600, internal paths closed at the proxy, HSTS, separate Postgres roles.

- [x] Derived participant keys (Keycast patch 0014): HKDF of the custody seed per vine_id and key version; the seed in memory only on a deployment (`CUSTODY_SEED_DIR=/run/belfry`, `scripts/custody-seed.sh` init/unlock/status/lock with age + age-plugin-yubikey, either YubiKey decrypts), a local file in development; preload refuses (503) while the seed is locked; Odoo `belfry_pass` 19.0.1.7.0 with `belfry_key_version` and `belfry_key_custody`; onboarding passes the version, marks self-custody moves, and `restore-keys` recreates every partner's account (derived: same npub; own key: keyless account; old random keys: reported). Self-custody refuses "keep this key". Checks: Keycast unit test against an independent HKDF vector, onboarding 150 tests, smoke 10a (Keycast account deleted, restored with the same npub) and 10b, 115 ok / 0 failed; the participant's npub recomputed from the local seed in plain Python matches.

- [x] Nightly encrypted off-VM backups (`infra/scripts/backup.sh`, DEPLOY.md "Backups"): `pg_dumpall`, the non-derived volumes (SQLite/LMDB paused during the copy), the secret files and the deployed code in one age file, encrypted to a backup key whose secret half is kept under the YubiKeys (`keygen`), so the VM can write backups but not read them; sent over SFTP (Hetzner Storage Box with snapshots) or to a directory, pruned after `BACKUP_KEEP` days, systemd timer (`install`), `status` for monitoring. `drill` restores a backup into a throwaway Compose project from the backup's own code and compares file hashes, volume file counts and every table's row count with the manifest. Checked locally: backup (27 MB), decrypt (a wrong key refused), drill passed (697 tables, 21 files, 6 volumes), SFTP upload, listing and pruning against an SFTP container. Not on the VM yet: needs `age` there, the backup key from a steward and a Storage Box.

- [x] Mocked hardware tokens until the YubiKey ceremony (user, 2026-10-24: ceremony in two weeks): two plain age keys stand in for the YubiKeys; seed and backup key created with them, the seed unlocked on the VM (2026-10-24). `custody-seed.sh rewrap` and `backup.sh rewrap` re-encrypt the same seed and backup key to the YubiKeys at the ceremony, so participant keys and old backups stay valid (DEPLOY.md "Custody seed").

Open (next): the YubiKey ceremony (rewrap, destroy the mock tokens); participants activated before the seed keep their random keys; the backups on the VM (backup key, Storage Box, timer) and a first drill there; the CA key off the VM; admin UIs behind an IP allowlist/VPN plus WebAuthn; network segmentation and non-root containers; pinned images; rate limits, monitoring, CSP; rotating a single participant's key (raising the version needs a Keycast move like self-custody, not built).

## 3u. Directory and marketplace (services/village) — built 2026-10-24

User: "Let's continue with more extras" after dropping DIDs; chose photos, a directory and a marketplace with tokens. Split: the other session builds photos, this one the directory and the marketplace. Decisions in the live doc (rev 72). Details: `services/village/README.md`.

- [x] `services/village`: one Node process, two browser apps on the community relay (participant's signer, NIP-42 AUTH), `GET /api/participants` (NIP-98, active pass only) from the onboarding service's active passes. Keycast clients `belfry-people`, `belfry-market` (registered, trusted); Compose `village` (3600/3700), Caddy `people.`/`market.<domain>`, `PEOPLE_PUBLIC_URL`/`MARKET_PUBLIC_URL`, icons `brand/apps/people-*`, `market-*`; both in `x-community-apps`.
- [x] Directory: opt-in by a kind-30078 consent event (`d` = `osv-directory`); "Your entry" edits it and the kind-0 profile; search; person page with "Message" (chat) and "Send tokens" (wallet).
- [x] Marketplace: NIP-99 offers (kind 30402, price in `OSV` tokens, categories, location, photos uploaded to Blossom), mark as gone, delete (kind 5). "Pay" hands off to the wallet (patch 0003: `/?pay=…&amount=…&memo=…&ref=…&return=…`), whose nutzap names the offer in an `a` tag; buyer and seller see the payment on the offer. Policy `belfry-event` gains 30402.
- [x] The app bar scrolls the current app into view (seven apps no longer fit a phone width).
- [x] Checks: `npm test` 4 tests; smoke 6p and 6g (30402, 30078 signed); local smoke 120 ok / 0 failed twice; `infra/scripts/village-check.mjs` in Chromium at 390 px with two participants: opt-in and search, an offer with a photo, payment through the wallet and back, the payment shown to both. Smoke: `SMOKE_KEEP_PASS` now also skips 10b (a self-custodial participant cannot use "Sign in with Open Source Village" in browser checks).

- [x] "What's new" (2026-10-24, user: "a good place to view the latest nostr events across the apps"): `people.<domain>/#/activity`, the last 14 days of readable events of active participants by day and app (notes, articles, photos and comments, offers, public calendar events, directory opt-ins; no DMs, private calendar entries, likes or payments). Checked in `village-check.mjs` step 5.

Open: chat, calendar and vote images need a rebuild to show Directory and Market in their app bar; no moderation or reports; no notifications for sellers; not deployed on the VM.

## 3v. Photos at photos.<domain> — built 2026-10-24

The photos part of §3u's split. Built as a third app inside `services/village`, reusing its sign-in, signer, relay (NIP-42) and Blossom code, rather than patching an upstream NIP-68 client for Keycast SSO, the pass-gated Blossom and the AUTH-only relay (no upstream client was evaluated). Details: `services/village/README.md`, "Photos".

- [x] Posts are NIP-68 pictures (kind 20): up to 8 images as `imeta` tags (`url`, `m`, `x`, `dim`, `alt`) plus `m`/`x` tags, title, caption, hashtags (`t`), place. Images are shrunk in the browser to 2048 px JPEG (drops EXIF/GPS) and uploaded to Blossom (kind 24242). Likes NIP-25 (kind 7 with `k` 20), comments NIP-22 (kind 1111), delete kind 5 with `k` 20. Feed of active passes only, hashtag filters, one person's photos. No new kinds in the policy (20, 7, 1111, 5, 24242 were in).
- [x] Tips: 1, 5 or 10 tokens through the wallet's pay request; wallet patch 0004 turns a 64-hex `ref` into an `e` tag (NIP-61), so the picture shows "You tipped …" to the tipper and the list of tips to the photographer (only nutzaps to the photographer count).
- [x] Compose `village` gains port 3800/8082 and `PHOTOS_PUBLIC_URL`; Caddy `photos.<domain>`; Keycast client `belfry-photos` (registered, trusted); "Photos" in `x-community-apps`; icon `brand/apps/photos-*` (a camera). The prod overlay now also closes the village's host ports (it had none for `village`).
- [x] Checks: `npm test` 10 tests (6 new); smoke step 6q (served, bundle carries the client id and Blossom, NIP-98 bound to the photos host, kinds 20/7/1111 signed); local smoke 128 ok / 0 failed twice; `infra/scripts/photos-check.mjs` in Chromium at 390 px with two participants: upload with alt text, share, feed and hashtag, like, comment, a 1-token tip through the wallet and back, the photographer sees all three, delete.

Open: animated GIFs become still JPEGs; no albums or event links (a NIP-52 calendar event could carry an `a` tag); no moderation or reports; Blossom keeps the files of deleted posts until its retention removes them.

## 3w. The pass on the phone without a wallet — built 2026-10-24

User: "Users should be able to show their pass as qr code on phone without a wallet, it would be enough to be signed in." Decisions (live doc): the QR is a short-lived signed link, not a credential; the door learns tier and validity only, like the wallet check.

- [x] Onboarding `mypass.py`: `pass.<domain>/me` signs in through Authentik (blueprint `belfry-pass-app`: participants only, implicit consent, the `keycast` scope for the partner id), shows tier, dates and a QR renewed every 30 s (token `<pass id>.<expiry>.<HMAC>`, valid 90 s), a live clock against screenshots, and keeps the screen on (Wake Lock). `pass.<domain>/check/<token>`: door staff scan with any camera; valid/not valid, tier, dates; the pass is read from Odoo at scan time; a second scan of the same code says when it was first scanned. New secrets `PASS_PAGE_OIDC_CLIENT_SECRET`, `PASS_PAGE_SECRET` (gen-secrets appends them).
- [x] Links: Keycast patch 0017 puts "Show your pass" first on the dashboard's pass card (`show_url` in the pass info), "Add to your wallet" second; the landing page and the claim page link `/me`.
- [x] Checks: onboarding 161 tests (11 new); smoke step 9b (silent sign-in to `/me`, QR renewal, `/check` discloses tier and dates only, not valid before the pass starts, forged code refused) and a check in step 11 (revoked pass refused at once); local smoke 144 ok / 0 failed; screenshots at 390 px.

Open: the check page shows the server's time (UTC on the VM); the scan memory is per process (one onboarding instance); a pass can still be lent together with the phone (no device binding, unlike the wallet); no offline door check.

## 3x. Hardening and the admin network — 2026-10-24

User: rate limits on the login, claim and bridge pages (magic-link flooding), no root in containers, a split Docker network, a CSP for the browser apps, Mailpit removed, ZeroTier for the admin and steward pages. Decisions in the live doc (rev 75). Details: `infra/DEPLOY.md` ("Admin network", "What the proxy does").

- [x] Proxy (`infra/proxy/Dockerfile`: Caddy 2.11.7 + caddy-ratelimit 0.1.0, user 10001): per-IP limits, generous for the venue NAT; CSP per browser app; Blossom files sandboxed; admin paths only from `ADMIN_NETS`. The proxy mounts `proxy/` (a git update of the Caddyfile is seen).
- [x] Authentik: deny stage before the magic-link mail, five links per account in 30 minutes (expression policy, cache). Smoke 6h checks it.
- [x] Containers non-root (10001; Keycast as `HOST_UID`, which owns `master.key` and the custody seed; `custody-seed.sh unlock` gives the seed to the SSH user), `no-new-privileges`, `cap_drop: ALL` where they start unprivileged; `perms-init` chowns old volumes once.
- [x] Networks: `data` (internal: Postgres, Redis and their clients), `edge` (proxy, static apps; IPv6 so the proxy sees IPv6 visitors' addresses, which came as `172.18.0.1` before), `default`.
- [x] Mailpit only with profile `dev` in production; ZeroTier on the VM (`scripts/zerotier.sh`, network in ZeroTier Central).
- [x] Checks: local smoke 145 ok and every browser check (chat, calendar, vote, wallet, village, photos); on the VM public smoke 142 ok / 0 failed, the browser checks from outside with the CSP enforced, admin paths 403 from outside over IPv4 and IPv6, backup run OK. The only CSP violation: the wallet's Google Fonts link (blocked, harmless).

Open: the VM's ZeroTier node must be authorised in ZeroTier Central, then `ADMIN_NETS` gets the ZeroTier subnet; steward phones need ZeroTier; image pinning and monitoring from the security review are still open.

## 3y. Default avatars and Mollie — built 2026-10-25

User: "Let's do B avatars. Also add what is needed for mollie. Including the web hooks." Decisions (live doc): the default avatar is a mirrored 5×5 pixel pattern in one of five village tints on the card colour, drawn from the public key (option B of the comparison page); Mollie is a second payment provider next to Stripe, through Odoo's own Mollie module.

- [x] Avatars: onboarding `avatar.py` serves `/avatar/<pubkey>.png|.svg` (PNG written with zlib, cached a year, CORS open); the starter profile names the PNG as `picture`, `publish-starter-events` adds it to profiles without a picture (everything else kept); the village apps draw the same picture in the browser (`web/shared/avatar.js`) for profiles without one or with a broken picture. A shared test vector keeps Python and JS in step. Chat, calendar and outside clients show the published `picture`.
- [x] Mollie: `belfry_pass` 19.0.1.9.0 depends on `payment_mollie` and adds refunds from Odoo, and on each webhook call for a paid payment reads its refunds and chargebacks from Mollie's API: refunds become refund transactions (keyed on the refund id, done when Mollie processes them, so the existing refund webhook revokes the pass; dashboard refunds included), chargebacks set `belfry_chargeback_state`, whose new automation posts to the onboarding service's `/hooks/odoo/chargeback` (open: revoke; reversed: give back a pass revoked for it); reconcile revokes open chargebacks it finds. Bootstrap configures the provider from `infra/secrets/mollie_api_key` (`test_`/`live_`) with `MOLLIE_METHODS` and re-syncs the webhook URLs on every run (new webhooks of a module upgrade got the placeholder URL before). No webhook to register at Mollie: Odoo names its own per payment.
- [x] Checks: onboarding 189 tests, village 18, Odoo 54 (7 new, Mollie's API mocked); bootstrap with a dummy test key (module upgrade, provider in test mode, card/iDEAL/Bancontact linked, chargeback webhook URL set; disabled again afterwards); local smoke 159 ok / 0 failed (6b checks the picture and the PNG).

On the VM (Mollie test key, test card in headless Chromium): payment → Mollie's webhook → invoice paid, pass active; a chargeback from Mollie's test page → pass revoked ("chargeback"). Mollie keeps refunds `pending` (test mode always, live mode until the next business day), so a pending refund now counts as done and revokes the pass. Only "Card" is offered while the VM's Odoo is in USD (iDEAL and Bancontact need EUR).

Rate limits (user, 2026-10-25: about 100 participants a day, one venue WiFi): `RATE_LIMIT_EXEMPT` (the venue's public addresses) takes them out of every per-IP limit; the door (`/door`, polled every 1.5 s, about 40 requests a minute per open page) goes from 60 to 600 a minute, pass sign-in (`/bridge`) from 300 to 1200.

Open: the pass field `stripe_payment_intent` holds Mollie's `tr_…` id for Mollie payments (name kept); the VM's participants get the avatar with `publish-starter-events`.

## 3z. Secrets out of the environment; Docker hardening — built 2026-10-25

User: protect the secrets by moving them out of environment variables, harden Docker; no
encrypted secrets in git (dotenvx considered and not used: its key would sit on the same VM, Docker
keeps the decrypted values in the container configuration anyway, and the repo is public). Details:
`infra/README.md` "Secrets", `infra/DEPLOY.md` "Secrets out of `.env`" and "Hardening the host".

- [x] Every secret is a file `infra/secrets/<name>` (mode 640, group 10001 set by `perms-init`);
  `.env` holds settings only. `gen-secrets.sh` generates the files and moves the values of an
  older `.env` into them (same values, nothing to rotate). Compose mounts each file only into the
  containers that need it; no container has a secret in its environment (`docker inspect`).
- [x] Readers: the onboarding service through pydantic's `secrets_dir`; the Node services for
  `PASS_INFO_TOKEN`; Authentik through `file://` and `!File` in the Belfry blueprints; Odoo, Keycast
  (database password as `PGPASSWORD`, which sqlx uses), the mint and authentik's first-start
  bootstrap through the wrapper `infra/secrets-env/with-secrets.sh` (`SECRETS_ENV`). Postgres runs
  as 999 directly (the entrypoint's switch from root would drop the secret group) and reads
  `POSTGRES_PASSWORD_FILE`; `init.sh` reads the role passwords from the files.
- [x] The rendered issuer and verifier configs with private keys were world-readable (664); now
  640 with group 10001, like `issuer.jwk.json` and `bridge.jwk.json` (the onboarding service read
  them only because its uid happened to be the host user's). `odoo.conf` holds only a PBKDF2 hash
  of the master password (2a046d4).
- [x] Host scripts load the files with `scripts/secrets.sh`; bootstrap writes its tokens with
  `set_secret`; the Odoo admin password, Authentik's token and the Stripe key no longer appear in
  `curl` arguments (visible in `ps`).
- [x] Registry images pinned by digest; `pids_limit` on every service; read-only root file system
  for the onboarding service, village and vote. `RATE_LIMIT_EXEMPT` reaches the proxy.
- [x] Checks: smoke 159 ok / 0 failed on the migrated local stack (bootstrap included); a fresh
  Compose project created the Postgres roles from the files and ran authentik's bootstrap; onboarding
  189 tests, village and vote tests.

Open: the VM migration (DEPLOY.md, a few minutes; containers are recreated); the Docker daemon
settings and the split of the shared `belfry` login (DEPLOY.md "Hardening the host", needs root
and changes how the scripts connect); Dockerfile base images are tags, not digests; Keycast,
Odoo and the mint still see their secrets in their own process environment; the issuer CA key is
still on the VM; secrets rotation is manual.

## 3z. Fresh install of the test VM: Belgium, EUR, new keys — 2026-10-25

User: fix the company country and currency (Belgium, EUR); the VM held test data only. A fresh Odoo database alone was not enough: contact ids restart, and participant keys derive from the custody seed and the contact id (keys of smoke-test participants had been exported), the status list is keyed on the pass id (old test credentials would read valid again), and Authentik usernames are the member numbers. So the VM was reinstalled: the other session's secrets change (9f4fc10, 4ba88f2) deployed first, a backup of the old state, then every data volume removed, a new custody seed and a new backup key (both encrypted to the two mock tokens, the old ones kept as `*.old` on the steward laptop), new issuer and verifier keys, a new Keycast master key, every generated secret new (the Mollie key and the SMTP password kept), the steward added again. Old `.env` copies and secrets were shredded on the VM.

- [x] bootstrap: a fresh database gets the company's country (`COMPANY_COUNTRY`, default BE) and currency before accounting, so Odoo installs the Belgian chart (`be_comp`, l10n_be) in EUR; the pass products stay without taxes (VAT still open).
- [x] Status list: every index up to the highest in use is revoked except those of active passes, so a credential whose pass was deleted never reads valid; higher indexes stay 0, so a pass activated after a verifier cached the list (up to `STATUS_LIST_TTL`) is valid at once (the first version revoked those too and failed the door and the pass sign-in for a fresh pass). Credentials from before a reset are refused by the new issuer key.
- [x] bootstrap re-applies the Belfry blueprints while a provider holds a placeholder secret (seen when the worker applied a changed blueprint before the secret file existed); bootstrap offers only the Mollie methods the Mollie account has enabled.
- [x] Checks on the VM: smoke 156 ok / 0 failed after the secrets change and again after the reinstall; no secret in any container's environment; Mollie card payment → pass active, refund from Odoo → revoked; backup with the new key.

Open: iDEAL and Bancontact are off in the Mollie account (enable in Mollie's dashboard, then rerun bootstrap); the backups made before the reinstall are still at `/var/backups/belfry` (old data, old key).

## 3za. Start page, invites from participants, feedback, test notice — built 2026-10-25

For the group test with real testers (`docs/real-user-test.md`, written by the other session). User decisions: the journey starts at `https://<domain>`, a page meaningful for everyone that says you need an invite from someone in the community (no public sign-up); participants can invite friends too; testers pay with Mollie's test card; feedback goes through a page in Belfry.

- [x] Start page: the onboarding service's landing page, rewritten (what the village and Belfry are, how to join, links for participants). `SITE_URL` (prod-env.sh: `https://<domain>`) is its home; `pass.<domain>/` forwards there. The proxy serves the onboarding site on the apex with the same rules as on `pass.` (snippet `onboarding_site`, own rate-limit zones), NIP-05 stays on the apex; the apex no longer redirects to Odoo.
- [x] Invites from participants: `belfry_pass` 19.0.1.10.0 (`belfry_friends.py`): a participant with an active pass has `belfry.invites_per_participant` (`INVITES_PER_PARTICIPANT`, default 3) open or used invites; revoked or expired ones give the slot back. On `/me`, "Invite friends": create (with a private note), copy, share, revoke, see which were used. The invite page says who invited (participant or steward name, `inviter_name`); the steward pages show it. The `/me` session cookie moves from path `/me` to `/`.
- [x] Feedback: `/feedback` (`feedback.py`), linked from the app bar ("Feedback" in `x-community-apps`, with `?from=` the page) and the footer of the onboarding pages: what happened, what was expected, optional screenshot (PNG/JPEG/WebP, 5 MB), named participant when signed in on `/me`, else an optional e-mail; honeypot, 30 a minute per address. Odoo `belfry.feedback`, mailed to the stewards with the screenshot; a Feedback section on the steward pages (done/reopen, screenshot view).
- [x] Test-run notice: `TEST_RUN_NOTICE` on every onboarding page and, through `belfry.test_notice` (bootstrap), on Odoo's invite, payment and portal pages, with Mollie's test card while Mollie is in test mode.
- [x] Polls can be open for 15 minutes (the session shows results at the feedback round).
- [x] Checks: onboarding 199 tests (8 new), Odoo 59 (5 new), vote 5; smoke 9b (a friend's invite names the participant, revoked again) and 9c (start page, forward, notice, feedback form); local smoke run.

Found on the VM (2026-10-25): walt.id Verifier2 kept a status list it had fetched for more than 10 minutes (longer than `STATUS_LIST_TTL` = 300 s) while the door and the bridge were used every few minutes; a verifier restart cleared it. Smoke step 11 (revocation refused after one TTL without traffic) passes, so the cache may expire from the last use rather than from the fetch (the list's `exp` is 24 h). To check before the event: with steady door traffic, does a revocation reach the door within the TTL? If not, a shorter `exp` on the list or a restart hook.

Open: participants' invites have no steward approval step (stewards see them and can revoke the pass); the Keycast account page has no "Invite friends" link of its own ("Show your pass" leads to `/me`); feedback cannot be answered from the steward pages (they show the e-mail).

## 3zb. Joining with an own Nostr key — built 2026-10-25

User: invite someone together with the Nostr key they already have. Before, everyone got a
derived key and could move to their own key only after activation (§3s self-custody).

- [x] Invite page: optional "Join with my own Nostr key". The page's script asks the browser's
  NIP-07 signer for the public key, and on submit for a NIP-98 event (kind 27235, the page's URL,
  `POST`). Odoo checks it in pure Python (`belfry_pass` `models/nostr_proof.py`: event id, BIP-340
  signature, key, path, method, 10 minutes) and refuses a key of another participant, or a returning
  participant who has a key already. The pass stores `own_pubkey` and `own_pubkey_proof`
  (19.0.1.11.0).
- [x] Activation (`core.own_key_of`) checks the proof again with coincurve, creates a keyless
  signer account for that key (as `restore-keys` does for self-custody), whitelists it, sets the
  partner's custody to `self`, and publishes nothing for it (no profile, relay or follow lists;
  `publish-starter-events` skips self-custody participants too, which also fixes it for
  participants who moved to their own key).
- [x] Checks: Odoo 66 tests (7 new: proof check, page, refusals), onboarding 202 (3 new), smoke step
  10c (forged proof refused, pass active with the own key, custody self, signer account without a
  key, key whitelisted), 172 ok / 0 failed; the page in headless Chromium with a stand-in NIP-07
  signer: key shown as npub, signed on submit, on to the payment page.

- [x] Wallet patch 0007: "Use your own signer" (NIP-07 with NIP-44). The seed and the nutzap key
  cannot derive from an own key, so the wallet makes them once and keeps them on the community
  relay as kind 30078 (`d` = `osv-wallet`), NIP-44-encrypted to the participant's key; another
  device reads them back. Checked with `infra/scripts/wallet-own-signer-check.mjs` (a stand-in
  extension signing in the onboarding container): sign-in, the pass's 350 tokens received, a second
  browser profile with the same seed and the tokens restored from the mint, nutzaps in both
  directions with a managed participant. `wallet-check.mjs` (managed key) unchanged.

Open: only NIP-07 extensions, on the invite page and in the wallet (no bunker or phone signer;
those join with a managed key and move later); the other apps' own-signer sign-in was there
before (§3s) and was not re-tested with a participant who joined with their key.

## 3zc. Branded error pages — built 2026-10-25

User: the plain-text "This page is only reachable over Belfry's admin network." on `pass.<domain>/steward` should look like the rest; review the other error pages too.

- [x] Proxy: every error Caddy answers itself (admin paths from outside `ADMIN_NETS`, closed internal paths, the mint's closed mint/melt, rate limits, a service that is down) goes through `handle_errors` in the `security` snippet: browsers (`Accept: text/html`) get `infra/proxy/errors/error.html` (Caddy template, status-specific title and text, the `error` directive's message for 403s, logo inline because most hosts' CSP allows no images from `pass.`), other clients the plain message as before. `respond` became `error` for those paths. Errors a service answers itself pass through.
- [x] Onboarding service: browsers opening a page (GET with `text/html`) get `claim.error_page(status)` for every HTTP error, malformed parameters (400 instead of FastAPI's 422 JSON) and unhandled exceptions (500); POSTs and API clients keep the JSON errors. 3 new tests (205).
- [x] Odoo `belfry_pass` 19.0.1.14.0: `views/error_pages.xml` replaces http_routing's 400/403/404/415/422/4xx and generic pages with the same card, without Odoo's header and its "Sign in" link (the back office is admin-only). Odoo's 500 page loads no assets by design and stays. 1 new test.
- Checked: a throwaway proxy with `tls internal` on the stack's network (403 admin page, 404 closed paths on four hosts, 429 after the feedback limit, 502 for a missing upstream; plain text for curl without `Accept: text/html`; the host's CSP kept on the error page); screenshots at 390 px of the proxy, onboarding and Odoo pages.

Not changed: Keycast's error pages were branded in patch 0010; Authentik's own denial page (a participant at the steward app) uses the brand blueprint's CSS; the village and vote apps answer unknown paths with their app; an Odoo portal link with a wrong access token redirects to `/my` → the admin-only login, which now shows the branded admin-network 403.

## 3zd. Programme and room bookings in the calendar — built 2026-10-25

User: the sessions are defined in the opensourcevillage.org repo; the rooms have public Google calendars (commonshub.brussels `src/settings/rooms.json`); sync both into the calendar app and show the booking titles.

- [x] opensourcevillage.org (branch `feat/schedule-json`, not pushed; the repo is private, so Belfry reads the deployed site): `/schedule.json` serves the current edition's programme grid (date, label, morning/afternoon/evening text, venue, dates) and every edition under `editions`. 2 tests.
- [x] Onboarding `programme.py`, CLI `sync-programme` (cron every 10 minutes, `docs/runbook.md`): every slot of `/schedule.json` (times from `PROGRAMME_SLOTS`, default 09:30–12:30, 14:00–18:00, 18:30–21:30) and every booking of the rooms' public iCal feeds within the event dates (recurring bookings expanded, cancelled ones skipped, the booking's title, "Booked" when it has none) becomes a public NIP-52 event (kind 31923, `d` = `osv-programme-<date>-<slot>` or `osv-room-<slug>-<hash>`, `t` `programme` / `room-<slug>`, `start_tzid` Europe/Brussels, location). Signed with the programme key (new secret `programme_nsec`; `PROGRAMME_PUBKEY` in `.env` follows it, gen-secrets); the relay whitelist adds the key itself. Only changes are published; events whose slot or booking is gone are deleted (kind 5, `a` tags); a source that cannot be read keeps its events. Waits out the relay's 60-events-a-minute limit (a first run of ~110 events takes about two minutes). Dependencies `icalendar`, `recurring-ical-events`. 8 tests.
- [x] Calendar patch 0004: the pinned app never shows other people's public events (its public-event fetch is unused and would filter by `created_at`), so the sidebar gets an **Open Source Village** group: Village programme plus one calendar per room, own colours and checkboxes (kept per browser), events from the programme key without a time filter, deletions honoured, read-only, RSVP works. Build argument `VITE_PROGRAMME_PUBKEY`.
- Checked locally: a dry run against the live room feeds and the 2026 programme gave 114 events (36 programme slots, 78 bookings in six rooms); the first sync published them in two rounds (rate limit), the second published nothing; the calendar in headless Chromium shows the group and the week of 26 January 2026 with programme and bookings, a click opens the event with RSVP.

Open: the website route needs merging and deploying (until then the programme source fails and only room bookings sync; their events are then kept, not deleted). The 2027 grid is still empty. The room calendars also carry Commons Hub bookings that are not part of the village; all of them are shown, as decided. One way only (Google and the website stay the source). Deploy on the VM: `gen-secrets.sh` (programme key), rebuild onboarding and calendar, cron line.

Smoke note: the restore-keys check (step 10a) failed once with the JSON split by interleaved log lines from `docker compose exec ... 2>&1`; the command itself returns valid JSON. Not related to this slice.

## 3ze. Calendar on phones; the programme app — built 2026-10-25

User (phone screenshot of the calendar): the calendar is not mobile friendly; is there a better calendar UI; how to link it with Nostr events. Chose both: fix the calendar, and a programme app built for the event.

- [x] Calendar patch 0005: our own patch 0001 had replaced Formstr's small square logo with the wide OSV wordmark, which pushed the top bar and the whole page past a phone's edge (451 px on 412 px; the day's event list ran off the screen). Phones get the square mark, the date label stays on one line, and phones open on the day view. Checked at 412 px in headless Chromium: no sideways scrolling, upstream's bottom tab bar is back.
- [x] Programme app at `programme.<domain>` (`services/village/web/programme`, fourth app of the village process, Keycast client `belfry-programme`): the programme key's sessions and room bookings plus participants' public events, one day at a time ("On now", "Later today" on today), filters by track, times in Europe/Brussels; "Your sessions". Session page: Going/Maybe/Can't go (NIP-52 kind 31925, Formstr's RSVPs read too), who is going, NIP-22 comments on the session's coordinate, photos taken there (the photos app's "Share a photo from this session" adds an `a` tag; the photo says "At <session>"), "Thank the host" tips for participants' sessions (nutzap with the coordinate), "Add to my calendar" (.ics), Share. "What's new" shows session comments and opens public events in the programme. Reports of sessions and comments (`belfry_pass` 19.0.1.17.0: app `programme`, kind `session`). No new policy kinds.
- [x] Checks: village tests 24 (6 new), onboarding 215, Odoo 69; smoke step 6t, local smoke 178 ok / 0 failed; `infra/scripts/programme-check.mjs` at 390 px with two participants: day list without sideways scrolling, RSVP, comment, .ics, "Your sessions", a photo from the session shown on the session page, the second participant sees the answer and the comment.

- [x] Announce a session (2026-10-26, user: "continue building the programme app"): `#/new` publishes a public kind 31923 in the participant's name (day and times in the village's time, rooms suggested as places, `d` = `session-…`, never the programme key's prefixes); the host edits it on the same coordinate (answers, comments and photos stay) or cancels it (kind 5). The pages no longer write into a page that another navigation has replaced (seen as a `null.onclick` after announcing). Checks: village tests 25, `programme-check.mjs` step 5 (announce, B sees it without Edit/Cancel, edit, cancel).

- [x] Feature complete for the event (2026-10-26, user: "keep building the programme until feature complete and works well on mobile"): bottom tab bar (Programme, Search, Yours, Announce), sticky day chips, swipe between days, today redrawn every minute, "Starting soon" for one's own sessions; search (every word, accents ignored); reminders as a 15-minute `VALARM` in every .ics and "Add all to my calendar" on "Yours"; the last programme kept on the device and drawn at once (refreshed from the relay, kept when the relay does not answer), and a service worker so the app opens offline with it. Polls about a session: the polls app stores a session coordinate (`session`, `session_title`, column added on start), lists by session for other origins (CORS), says "About <session>"; the session page lists them and offers "Start a poll" (`VITE_VOTE_URL`, CSP). Calendar patch 0006: participants' public sessions as a "From participants" calendar. Checks: village tests 27, vote tests 7, smoke 6t (polls link, CORS), local smoke 180 ok / 0 failed, `programme-check.mjs` 20 steps (offline included). Deployed on belfry.tofauti.net the same day (58085bd: village, vote and calendar rebuilt, `caddy reload` for the CSP): public smoke 178 ok / 0 failed, `programme-check.mjs` from outside 20 steps green.

Open: no push notifications (the reminder comes from the phone's calendar); offline, a session's own page needs the network; the calendar app's "From participants" does not drop revoked participants' or steward-hidden sessions (the programme app does); the Calendar by Form* month and week views stay as upstream made them (dots on phones). Deployed on belfry.tofauti.net 2026-10-26 (f390a2f): `PROGRAMME_PUBLIC_URL` in `.env`; calendar, village, onboarding, chat, vote and wallet rebuilt; `up -d`; bootstrap (belfry_pass 19.0.1.17.0, client `belfry-programme`). The proxy mounts `proxy/` and is not recreated by `up -d`, so a new host block needs `docker compose … exec proxy caddy reload --config /etc/caddy/Caddyfile` (until then TLS for the new host fails with "tlsv1 alert internal error"). Public smoke 176 ok / 0 failed; `programme-check.mjs` from outside against the VM, all steps green. From a laptop that resolves the domain through ZeroTier DNS, Chromium's Private Network Access blocks the apps' cross-origin calls; run the browser checks with `--host-resolver-rules="MAP *.<domain> <public IP>"` (a wrapper as `CHROME_BIN`).

## 3zf. Polls on Nostr — built 2026-10-26

User: "Why do we need a vote database when we have nostr relay", then "Do it" for the hybrid: the database keeps what a relay cannot do (verify proofs, refuse double votes by nullifier, fix the group per poll, hold results until the poll closes; a vote as a signed event would name its voter, and the relay takes only participants' keys after AUTH), while polls and results become Nostr events.

- [x] The vote service signs with its own polls key (secret `polls_nsec`, `POLLS_PUBKEY`, gen-secrets): a kind-0 profile, a kind-1 note when a poll opens (options, closing time, link, `a` to the session), a reply with the result whose `x` tag pins the SHA-256 of the published proofs (`server/announce.js`, `services/vote/README.md` "On Nostr"). Retried until the relay takes it; polls opened before stay quiet. The onboarding service whitelists the key; "What's new" shows the notes as "Polls".
- [x] Checks: vote tests 9 (announcement, result reply and hash, once only, retry after a refusal, the proofs body is what the hash covers), village tests 28; smoke 6n (the service announces as `POLLS_PUBKEY`, the key is whitelisted), local smoke 182 ok / 0 failed; end to end locally: a poll opened by `vote-check.mjs`, closed early, both notes on the relay, the hash equal to `sha256sum` of the proofs URL, both in What's new.

Deploy: `gen-secrets.sh` on the VM (writes `secrets/polls_nsec` and `POLLS_PUBKEY`), rebuild vote, village and onboarding, `up -d`.

## 4. Next

Decided 2026-10-24: DIDs are dropped (no did:webvh, no `id.<domain>`, Phase 4 of the plan); identity is the participant's Nostr key. Next are more extras (Phase 6).


1. The real-user happy-path test on `belfry.tofauti.net`, steps and results table in `docs/real-user-test.md` (a real e-mail address, Stripe test mode, a phone wallet against the claim page, door and pass sign-in), following `infra/DEPLOY.md` and a real wallet run: EUDI reference wallet or wwWallet against the claim page, the door and the pass sign-in (§3e open items 1–3), Odoo SMTP, Stripe test mode on the public URL.
2. Phase 2 (continued): check the login, the key export, the pass card (§3m) and a phone NIP-46 client against `wss://bunker.<domain>` (§3n, e.g. Amethyst or Coracle with a bunker URL from "create bunker", or nostr-login via the NIP-05 discovery) live on the VM; pick the marketplace and links protocols (the other app kinds are decided, §3h); choose and deploy the actual client apps (calendar, photos, proposals, NIP-17 chat) against the relay, Blossom and the `belfry-event` scope.
3. Phase 3 hardening, remaining: CA publication for wallets, encrypted verifier responses and the authorization-code claim flow (HAIP), issuer key in a KMS, key rotation (see §3e and §3f).

To-dos from the user (2026-10-25):

- [x] Enable iDEAL and Bancontact in the Mollie account (dashboard, Settings › Payment methods), then rerun bootstrap (it offers only the methods the account has enabled).
- [ ] Decide whether the backups from before the VM reinstall (`/var/backups/belfry`, old test data, old backup key) are deleted.
- [ ] Regenerate the Mollie test key (it passed through a chat) and store it with `infra/scripts/put-secret.sh … mollie_api_key`.
- [ ] Add the venue's IPv4 address to `RATE_LIMIT_EXEMPT` (the IPv6 /64 is in), if the venue has a fixed one.

## Notes

- First commits made on 2026-10-22 (layout agreed: monorepo). Relay slice, Keycast fork, participant login and Phase 3 committed 2026-10-23.
- Local test run: the host Python is 3.14; use `python3.12 -m venv` and `pip install -e 'services/onboarding[dev]'` for `pytest` and `ruff`.
- Research notes: `docs/research/` (spikes dated 2026-10-22), consistency review in `plan-consistency-review.md`.
