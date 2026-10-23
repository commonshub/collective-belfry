# Next steps (handoff from 2026-10-22, updated 2026-10-23)

Start here in the next session. Steps 0–3 are done, Phase 2 has started (§3b relay, §3c Keycast login, §3d participant login); continue with §4.

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

## 4. Next

1. Phase 2 (continued): key export for OIDC-only accounts, remove the Divine UI parts and branding (new patches in `infra/keycast/build/patches/`), Blossom, NIP-05 on Belfry's domain (Keycast serves it for its usernames), pick the community apps and extend the policy kinds, Odoo SMTP for the activation mail.
2. Phase 3 start: walt.id Issuer2 once the issuer decision is confirmed with the DIDroom maintainer; status-list service; VCTM hosting; SUNET verifier patch for the OIDC path.
3. Run the smoke flow against Stripe test mode on a public URL.

## Notes

- First commits made on 2026-10-22 (layout agreed: monorepo). Relay slice, Keycast fork and participant login committed 2026-10-23.
- Local test run: the host Python is 3.14; use `python3.12 -m venv` and `pip install -e 'services/onboarding[dev]'` for `pytest` and `ruff`.
- Research notes: `docs/research/` (spikes dated 2026-10-22), consistency review in `plan-consistency-review.md`.
