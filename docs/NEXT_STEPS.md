# Next steps (handoff from 2026-10-22, updated 2026-10-23)

Start here in the next session. Steps 0–3 are done, Phase 2 has started (§3b relay, §3c Keycast login, §3d participant login), Phase 3 is built locally (§3e: pass certificates, door check, pass sign-in), a first hardening pass is in (§3f) and the public deployment is prepared (§3g: proxy overlay, public URLs, `infra/DEPLOY.md`). What is left before the event is in §4. The plan's happy path (invite → pay → accounts → pass in the wallet → door and app sign-in with the pass) runs end to end against the local stack with a scripted wallet; continue with §4.

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
7. Day-pass `exp` is the end of the day (UTC); the plan's `role` claim and the `sub` = did:webvh variant (Phase 4) are not in the credential yet.

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
- [x] Policy kinds: `belfry-event` now allows 0, 1, 5, 7, 22242, 24242, 27235, 30023 (deletion, Blossom auth and NIP-98 added). The app kinds are a decision, see below.

**Policy kinds: what is needed.** Keycast authorizes signing per OAuth app through a policy (`policy:belfry-event` scope) whose `allowed_kinds` permission lists event kinds; other permission types are `content_filter`, `encrypt_to_self`, `decrypt_only`, `full_access`. NIP-44/NIP-04 encryption and decryption are separate RPC methods and are not gated by kinds. To finish the policy, decide:

1. **Which apps and protocols.** For each of the plan's apps one kind set: profile 0 and relay lists 10002; notes 1, reactions 7, deletions 5; directory = profiles plus follow lists 3; calendar (NIP-52) 31922, 31923, 31924, 31925; proposals: NIP-88 polls 1068 and 1018, or long-form 30023 with 1 for discussion; marketplace: NIP-15 (30017, 30018, 4) or NIP-99 classifieds 30402; photos: NIP-68 pictures 20 plus Blossom 24242; links: NIP-B0 bookmarks 39701 or NIP-51 lists 10003/30001; chat: NIP-29 groups (9, 10, 11, 12 plus 39000–39003 by the relay) or NIP-17 DMs (14, 13 seals, 1059 gift wraps, 10050), which also needs `nip44_encrypt`/`nip44_decrypt`. Without the decision the policy stays at the current set and clients get `kind not allowed` for anything else.
2. **One policy or one per app.** Keycast policies are global (`policies` table, no team); one `belfry-event` scope for all apps is simplest, a policy per app (chat may encrypt, the door app may only read) is tighter. The relay also needs its kind allowlist (rnostr `kinds` filter, currently open to whitelisted pubkeys).
3. **Encryption.** NIP-17 chat means the signer decrypts for the app on every message; decide whether a managed key may do that (`decrypt_only`/`encrypt_to_self` permissions exist) and whether the chat should rather be NIP-29 public groups on the community relay.
4. **Where the list lives.** `infra/keycast/policy.sql` (applied by bootstrap, idempotent) is the single source; the relay's `rnostr.toml` should carry the same kinds.

## 4. Next

1. A public test deployment (HTTPS) following `infra/DEPLOY.md` and a real wallet run: EUDI reference wallet or wwWallet against the claim page, the door and the pass sign-in (§3e open items 1–3), Odoo SMTP, Stripe test mode on the public URL.
2. Phase 2 (continued): build the Keycast image with patches 0004–0005 and check the login and the key export live; decide the community apps and their kinds (§3h) and put them into `policy.sql` and the relay config; NIP-05 on Belfry's domain is routed by the proxy.
3. Phase 3 hardening, remaining: CA publication for wallets, encrypted verifier responses and the authorization-code claim flow (HAIP), issuer key in a KMS, key rotation (see §3e and §3f).

## Notes

- First commits made on 2026-10-22 (layout agreed: monorepo). Relay slice, Keycast fork, participant login and Phase 3 committed 2026-10-23.
- Local test run: the host Python is 3.14; use `python3.12 -m venv` and `pip install -e 'services/onboarding[dev]'` for `pytest` and `ruff`.
- Research notes: `docs/research/` (spikes dated 2026-10-22), consistency review in `plan-consistency-review.md`.
