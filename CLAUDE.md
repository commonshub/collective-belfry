# Collective Belfry

Belfry (the Collective Bunker) runs access and identity for Open Source Village: EUDI-wallet pass certificates (SD-JWT VC via OpenID4VCI/VP), Nostr-based community apps behind a Keycast signer (Divine's fork), did:webvh identifiers for long-term members, and Odoo as the registry of who holds which pass.

## Current status

Plan verified against the real software on 2026-10-22 (spike notes in `docs/research/`); decisions recorded in @docs/NEXT_STEPS.md §2. Build slice 1 (Phase 1 onboarding: Compose stack, Odoo module `belfry_pass`, onboarding service) is built and green locally; its contract is @docs/slice-1-onboarding.md. Phase 2 has started: the community relay (rnostr, `infra/relay/`) with a NIP-42 whitelist derived from active passes and kind-0 profiles published at activation, the Keycast fork (patch series in `infra/keycast/build/patches/`) whose login goes through Authentik, and the participants' Authentik login itself: a one-time account-setup link mailed by Odoo at activation (2026-10-23). Phase 3 is built locally: walt.id Issuer2 issues the village-pass SD-JWT VC from the claim page, the onboarding service serves the status list, type metadata, a door check and the OpenID4VP-to-OIDC bridge over walt.id Verifier2 that signs participants in to Authentik with the pass (`infra/issuer/README.md`, 2026-10-23). The whole happy path runs locally with a scripted wallet; a first hardening pass (Odoo mail over SMTP, least-privilege service user, master password, issuer certificate pinning, signed verifier requests) is in (§3f), the Keycast fork carries Belfry branding and key export for OIDC-only accounts (patches 0004–0005, image built and verified) and a gated Blossom file server is in the stack (§3h). A public test deployment runs on `belfry.tofauti.net` (`infra/DEPLOY.md`, §3i) with the full smoke test green over TLS; another Claude session shares that VM, coordinate before touching it. Next work is in @docs/NEXT_STEPS.md §4. Run `make up && make bootstrap && make up-app && make smoke` to see it work.

## Project plan

- Live plan (source of truth): Claude Doc "Belfry Identity & Membership Plan", https://claude.ai/artifact/VUoFGX4mFvZUCPXqKe2amj
- Local snapshot: @docs/identity-membership-plan.md (may be out of date)
- Research notes (read when working on that area):
  - `docs/research/signing-server.md`: why Keycast (Divine's fork) over OpenBunker and other signers
  - `docs/research/authentik-openid4vp.md`: how to add EUDI pass sign-in to Authentik via an OpenID4VP-to-OIDC bridge

Before relying on details from the snapshot, re-read the live doc with the Claude Docs tools (doc id `e6a176e1-49c1-4710-8453-bf70a5e0957e`, tab body node `d86c921a-66eb`). If it changed, refresh the snapshot and update its rev. Record decisions made while coding back in the doc's "Open decisions and risks" section, not only here.

## Key rules from the plan

- Identity and membership stay separate: a did:webvh belongs to the member; membership (Odoo record + signed certificate) belongs to Belfry. Revoking membership never touches the identifier.
- Odoo is the single source of truth for passes; the relay whitelist and certificates are derived from it.
- Payment runs through Odoo's own Stripe provider and Odoo payment links. Belfry's onboarding glue is idempotent, keyed on the Odoo invoice and the Stripe PaymentIntent; Belfry's own Stripe endpoint (disputes only) is keyed on the Stripe event id. Access only after the invoice is paid.
- One credential type (`vct` `village-pass/v1`) for all tiers; tier and dates are claims. Door checks disclose only tier, validity and status.
- Every did:webvh uses pre-rotation from day one, with Belfry as witness.
- Event release = Phases 0–3 (governance, onboarding/registry, Collective Bunker, EUDI pass certificates). DIDs and self-custody come after the event.
