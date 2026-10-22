# Plan consistency review (2026-10-22)

Checked against the live doc at rev 33 (unchanged since the local snapshot). Five checks from `docs/NEXT_STEPS.md`.

| # | Check | Result |
| --- | --- | --- |
| 1 | Phase numbers match everywhere | Consistent, no change |
| 2 | Architecture table says Authentik for login while the Keycast login decision is open | Consistent only if the decision is "Keycast trusts Authentik"; fix the table once decided |
| 3 | Happy path step 4 matches Keycast preload-user plus claim | Step 4 matches; the claim step is missing from steps 5–6 (pending the Keycast spike) |
| 4 | Lazy key creation vs. npub in the certificate | Real contradiction: the happy path and Phase 3 already assume eager creation; the open decision should be closed as "eager" or Phase 3 changed |
| 5 | `sub` only for full access vs. tier and variant tables | Consistent; one clarification suggested |

## 1. Phase numbers

Cross-references found: Phase 1 text ("live once Phase 2 ships"), Phase 3 text ("carrying the npub from Phase 2"), unhappy path "Upgrade" ("once Phase 4 is live"), Fork Bomb section (Phase 3 for DIDroom, Phase 4 and Phase 1 under "Not covered"), event release = Phases 0–3 in the intro of the phase list and in CLAUDE.md. All agree with the numbered list (0 Governance, 1 Onboarding, 2 Bunker, 3 Certificates, 4 Root identity and DIDs, 5 Advanced members, 6 Extras).

## 2. Login row in the architecture table

The table's Login row reads "Authentik (OIDC) with passkeys, plus sign-in by presenting the EUDI pass (OpenID4VP)" and the Collective Bunker row lists Keycast with "OAuth with PKCE". Phase 2 says "connect its login to Authentik (see Decisions)". The open decision has two outcomes:

- Keycast trusts Authentik via OIDC: table is right as written. Add "Keycast delegates login to Authentik" to the Bunker row.
- Keycast handles login itself: the Login row must say "Authentik for non-Nostr apps and account recovery; Keycast for the Nostr apps", and pass-based sign-in then has to be built into Keycast, not only into Authentik, which widens Phase 3 (the OpenID4VP bridge would need to serve two consumers).

Recommendation: the first option, because the OpenID4VP bridge in `docs/research/authentik-openid4vp.md` only has to be integrated once. Confirm with the Keycast spike's estimate of the OIDC upstream login effort.

## 3. Happy path step 4 vs. Keycast preload and claim

Step 4 ("a Nostr key in the bunker") matches `POST /api/admin/preload-user`, which creates the account and key at webhook time. What the happy path does not show is the claim: in Keycast the participant takes over a preloaded account through a claim token sent by email (`POST /api/admin/claim-tokens`). Without it the participant cannot sign in to the apps in step 6.

Proposed edit once the Keycast spike confirms the flow: step 5 becomes "The confirmation page and email show an OpenID4VCI QR code and a claim link for the bunker account; …". Also the relay whitelist and NIP-05 name can be derived at preload time, so step 5's "Their npub joins the relay whitelist" is fine.

## 4. Eager vs. lazy Nostr key creation

Open decision: "Whether the bunker creates Nostr keys for every pass, or only when a participant first opens the apps." But:

- Happy path step 4 creates the key at payment time.
- Phase 1 has the webhook call the preload-user API.
- Phase 3 puts the npub in the certificate at issuance, and the "Npub binding" extra depends on it.

Lazy creation would break Phase 3 unless certificates are issued without `npub` and re-issued after first app use, or wallets look the npub up through NIP-05 at presentation time. Both add a step for every participant. Recommendation: close the decision as eager creation; the cost is one unused Keycast account per participant who never opens the apps, which the expiry job cleans up anyway. If the user still prefers lazy creation, Phase 3 must drop the npub claim and the "Npub binding" bullet.

## 5. `sub` and the DID across the tables

Tier table: day and week have no DID, full access has one. Variant table: "Wallet pass" has no `sub`, "Wallet membership" carries the DID as a selectively disclosable `sub`. Claims example: `sub` marked "did:webvh, full access only". All three agree.

One clarification: Phase 4 (DIDs) comes after the event, so at the event every certificate is a "Wallet pass" without `sub`, including full access. Phase 4 says DIDs are backfilled and "added to their certificates", which implies a re-issue for every full-access holder. Suggest adding "Re-issued when: Phase 4 backfill" to the Wallet membership row so the re-issue is planned, not discovered.

## Edits to make in the live doc

Deferred until the decisions in `docs/NEXT_STEPS.md` §2 are taken, then applied together:

- [ ] Login row and Bunker row of the architecture table (check 2).
- [ ] Happy path step 5: add the claim link (check 3), if the Keycast spike confirms.
- [ ] Decisions list: close the eager/lazy key decision (check 4).
- [ ] Variant table: "Phase 4 backfill" in the re-issue column (check 5).
