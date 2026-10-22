# Collective Belfry

Belfry (the Collective Bunker) runs access and identity for Open Source Village: EUDI-wallet pass certificates (SD-JWT VC via OpenID4VCI/VP), Nostr-based community apps behind a Keycast signer (Divine's fork), did:webvh identifiers for long-term members, and Odoo as the registry of who holds which pass.

## Current status

Plan verified against the real software on 2026-10-22 (spike notes in `docs/research/`). Building waits on the decisions in @docs/NEXT_STEPS.md §2; once taken, start the first build slice there. Nothing is committed yet.

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
