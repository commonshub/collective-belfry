# Next steps (handoff from 2026-10-22, updated after the verification session)

Start here in the next session. Steps 0 and 1 are done; step 2 is waiting on the user; step 3 starts once the decisions are in.

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
2. The Keycast image is built from the upstream Dockerfile via `infra/keycast/build/`; the git build context fails on a dangling submodule. The Belfry fork (OIDC login, branding) does not exist yet.
3. Stripe is emulated locally (done transaction + manual payment). Real Stripe test mode needs a public URL for Odoo's webhook and `stripe listen` for the dispute endpoint.
4. No enrollment or password link is sent to the Authentik user yet (Phase 2 login work).
5. VAT, credit notes on refund and the day-pass "which day" question are still open (see the Odoo spike note).

## 4. Next

1. Phase 2: relay whitelist synced from `belfry.pass` (active passes), Blossom, NIP-05 on Belfry's domain (Keycast serves it for its usernames), the Keycast fork with Authentik OIDC login (3–5 days, files listed in `docs/research/keycast-spike.md` §4), remove the Divine UI parts.
2. Phase 3 start: walt.id Issuer2 once the issuer decision is confirmed with the DIDroom maintainer; status-list service; VCTM hosting; SUNET verifier patch for the OIDC path.
3. Run the smoke flow against Stripe test mode on a public URL.

## Notes

- First commits made on 2026-10-22 (layout agreed: monorepo).
- Research notes: `docs/research/` (spikes dated 2026-10-22), consistency review in `plan-consistency-review.md`.
