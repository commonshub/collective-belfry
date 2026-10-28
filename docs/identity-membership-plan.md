# Belfry Identity & Membership Plan

> Snapshot of the Claude Doc https://claude.ai/artifact/VUoFGX4mFvZUCPXqKe2amj (rev 39, exported 2026-10-22; rev 43 adds the community relay decision of 2026-10-23, rev 44 the Keycast login through Authentik, rev 45 the participant login through a mailed account-setup link, rev 46 the Phase 3 decisions, rev 47 the community app protocols, rev 49 the passwordless magic-link login; see docs/NEXT_STEPS.md §3b–§3h and §3j, rev 50 the branding decision of 2026-10-23 (§3l), rev 51 single sign-on for the community apps (§3q), rev 59 logout everywhere (§3r); rev 63–71 (not re-exported): derived keys under YubiKeys, and DIDs dropped on 2026-10-24 (Phase 4, did:webvh, id.<domain>); rev 86 (not re-exported): the pass API for agents (§3zm). The live doc is the source of truth.

2026-10-20

## Goals and principles

Belfry (the Collective Bunker) runs access and identity for Open Source Village: every pass is an EUDI-compatible certificate, access to community apps runs over Nostr, and long-term members get rotatable, portable identities. Because the event itself showcases the EU Digital Identity Wallet, EUDI certificates are used at every step a participant touches.

- **Authoritative:** Belfry alone decides who holds a pass or membership, through steward invites, and can revoke it.
- **EUDI showcase:** every pass, from day pass to full access, is issued into the participant's EUDI Wallet and checked with it at the door and in the apps.
- **Nostr-native community:** chat, directory, calendar, proposals, marketplace, photos, links and profiles run on a whitelisted Nostr relay fed by the registry.
- **Portable and rotatable:** full-access members get a did:webvh whose keys can be replaced; advanced members can take it with them.
- **Proportionate:** day and week visitors get a simple wallet pass and a managed Nostr key, without a DID.

The core design rule is to keep **identity** and **membership** separate. The identifier (a did:webvh) belongs to the member and can rotate and move. Membership is a registry record plus a signed certificate, and both belong to Belfry. Revoking membership never touches the identifier, and moving the identifier never grants membership.

## Architecture

The system has six layers. Each answers one question, and Odoo is the single source of truth for who holds a pass or membership.

| Layer | Question it answers | Component | Who controls it |
| --- | --- | --- | --- |
| Registry and billing | Who holds which pass, and has it been paid? | Odoo (contacts, pass products, invoices), Stripe payment links | Belfry |
| Login | Is this the returning participant? | Authentik (OIDC) with e-mail magic links, plus sign-in by presenting the EUDI pass (OpenID4VP) | Belfry |
| Pass certificate | Can anyone verify this pass offline? | SD-JWT VC in the EUDI Wallet, issued via OpenID4VCI, revocable via a status list | Belfry issues; the participant holds it |
| Collective Bunker | Which Nostr key may use the community apps? | Keycast signer (KMS-encrypted keys, signing over HTTPS or NIP-46, login delegated to Authentik), whitelisted Nostr relay, Blossom file server, NIP-05 on Belfry's domain | Belfry |
| Identifier | Which keys speak for a long-term member? | did:webvh at `id.belfry.example` (full access and above) | Belfry (standard) or the member (advanced) |
| Root of trust | Which key signs for Belfry? | Organizational did:webvh and issuer key, on hardware tokens held by 3 stewards | Belfry stewards |

**Pass and custody tiers**

| Tier | Price | EUDI pass certificate | Nostr key | did:webvh |
| --- | --- | --- | --- | --- |
| Day pass | €50 | Yes, valid 1 day | Managed by the bunker | No |
| Week pass | €100 | Yes, valid 7 days | Managed by the bunker | No |
| Full access | €200 | Yes, valid for the event or membership period | Managed, exportable | Yes, Belfry holds the update key |
| Advanced member | Full access + opt-in | Yes, also bound to the member's DID | Self-custody | Yes, member holds the update key |

Early-bird prices, discount codes and the company surcharge (+50%) are pricing rules in Odoo; they don't change the certificate. Upgrading from full access to advanced is one log entry, signed by Belfry, that hands control of the DID to the member's key.

## Happy path: from invite to the door

A participant goes from a steward's invite to a working EUDI pass and app access in six steps; steps 1–4 come from the whiteboard, 5–6 close the loop.

1. **Invite.** A steward generates a single-use, expiring invite link in Odoo. The link records which steward admitted whom.
2. **Pick a pass.** Day (€50), week (€100) or full access (€200), with early-bird prices and discount codes applied by Odoo.
3. **Billing.** The participant enters billing details; companies pay +50%. Odoo generates the invoice and a Stripe payment link. Optional EUDI showcase: prefill name and address by presenting the national ID (PID) from the wallet.
4. **Payment confirmed.** The Stripe webhook marks the invoice paid and triggers user creation: an Odoo contact with pass dates, an Authentik account, and a Nostr key in the bunker. The handler is idempotent, so a retried webhook never creates a second user.
5. **Claim the pass.** The confirmation page and email show an OpenID4VCI QR code; the participant scans it and the pass certificate lands in their EUDI Wallet. Their npub joins the relay whitelist and gets a NIP-05 name.
6. **Use it.** At the door, a verifier app checks the pass (OpenID4VP) and only learns "valid pass, tier, dates". In the apps, the participant signs in by presenting the same pass, then uses chat, directory, calendar, proposals, marketplace, photos, links and profile over Nostr.

**Unhappy paths to build alongside**

- Refund or chargeback: Odoo revokes the pass (status list bit) and removes the npub from the whitelist.
- Pass expiry: the certificate's `exp` lapses and a nightly job removes expired npubs from the whitelist.
- No EUDI Wallet yet: issue the same SD-JWT VC to a sandbox or open-source wallet, or fall back to Authentik login.
- Upgrade (day or week to full access): pay the difference in Odoo, issue a new certificate, create the member's did:webvh once Phase 4 is live.

## did:webvh design

Every full-access member gets a did:webvh: a did:web with a hash-chained, signed history, so keys can rotate and the identifier can move domains without changing. [Spec](https://identity.foundation/didwebvh)

**Identifier and resolution**

- Format: `did:webvh:<SCID>:id.belfry.example:alice`, which resolves to `https://id.belfry.example/alice/did.jsonl`.
- The SCID is a hash of the first log entry, so the identifier is tied to its starting state, not to DNS.
- A resolver replays the log: the SCID matches entry 1, each `versionId` (`<number>-<hash>`) chains to the previous entry, each entry is signed by an authorized `updateKeys` key, and witness proofs meet the threshold. Historical versions can be resolved by `versionTime`.

**Two kinds of keys**

- **Update keys** (Ed25519, `eddsa-jcs-2022`) control the DID by signing log entries. Rotating them changes who controls the identity.
- **Verification methods** in the DID document are the keys the identity uses: P-256 for certificate holder binding, a Nostr key, a PGP key. Rotating them is routine.

**Rules Belfry enforces**

1. **Pre-rotation on every DID from day one.** Each entry commits to the hash of the next update key (`nextKeyHashes`), so a stolen current key cannot take over the DID. If the committed key is lost, the DID cannot be updated again.
2. **Belfry as witness.** Belfry co-signs each log entry (`did-witness.json`), giving authority and an audit trail.
3. **`portable: true`.** A member can republish the log on their own domain with a move entry; the SCID and history stay intact.
4. **did:web fallback.** Serve a plain `did.json` alongside, for resolvers that don't understand did:webvh.
5. **Human handles.** Show names through `alsoKnownAs`, NIP-05 or email; keep the DID under the hood.

**Caveats**

- Implementations are young. One recent Rust release fixed a bug that let an entry skip pre-rotation entirely. Use maintained libraries and test against at least two resolvers. [didwebvh-rs v0.5.3](https://github.com/decentralized-identity/didwebvh-rs/releases/tag/v0.5.3)
- A host can serve an outdated log. Run watchers that cache logs.
- If Belfry's domain disappears before a member moves, live resolution fails, though a saved log still verifies offline.

## EUDI pass and membership certificates

Every pass, from day pass to full access, is an SD-JWT VC issued through OpenID4VCI and following the HAIP profile, so EUDI Wallets accept it as a non-qualified attestation of attributes (EAA). One credential type covers all tiers; the tier and dates are claims.

**Issuer key**

HAIP resolves the issuer key either from web issuer metadata or from an X.509 chain in the `x5c` header; EUDI wallets should not be expected to resolve DIDs. [SD-JWT VC issue #232](https://github.com/oauth-wg/oauth-sd-jwt-vc/issues/232) Belfry publishes one P-256 key in three places:

1. `https://belfry.example/.well-known/jwt-vc-issuer`, used by EUDI wallets.
2. Belfry's organizational did:webvh, as an `assertionMethod`, giving a verifiable rotation history.
3. Optionally, an X.509 certificate for verifiers that require `x5c`.

Old issuer keys stay in the JWKS until every credential they signed has expired.

**Certificate claims**

```json
{
  "iss": "https://belfry.example",
  "vct": "https://belfry.example/vct/village-pass/v1",
  "iat": 1792800000,
  "nbf": 1792800000,
  "exp": 1793404800,
  "pass_tier": "week",
  "cnf": { "jwk": { "kty": "EC", "crv": "P-256", "x": "…", "y": "…" } },
  "status": { "status_list": { "idx": 412, "uri": "https://belfry.example/status/1" } },
  "_sd": ["…display_name", "…npub", "…member_number", "…role", "…sub (did:webvh, full access only)"]
}
```

- `vct` names the credential type; Type Metadata at that URL gives wallets a name, logo and colors.
- `cnf` binds the certificate to a holder key.
- `status` points to a status list; revoking membership flips one bit.
- Selectively disclosable claims let a member prove "valid member" without revealing name, role or number.

**Two variants per member**

| Variant | Who | `cnf` (holder key) | `sub` | Re-issued when |
| --- | --- | --- | --- | --- |
| Wallet pass | Day and week visitors | The EUDI Wallet's device key | None | Upgrade, refund (revoked instead) |
| Wallet membership | Full access | The EUDI Wallet's device key | The member's did:webvh, selectively disclosable | Expiry, role change, Phase 4 DID backfill |
| Advanced copy | Advanced members | A P-256 verification method from the member's did:webvh | The member's did:webvh | Expiry, role change, or the member rotating that key |

**EUDI extras**

- **Four EUDI touchpoints at the event:** pass issuance (OpenID4VCI), door check (OpenID4VP verifier app), app sign-in by presenting the pass, and optional PID-based prefill of billing details. Together they make the event a working EUDI demo.
- **Selective disclosure at the door:** the verifier requests only `pass_tier`, validity and status, never the name.
- **Npub binding:** the selectively disclosable `npub` claim lets an app link a wallet presentation to the participant's Nostr key without a separate login.
- Write an attestation rulebook for the pass type: mark it as a non-qualified EAA, state that it is revocable, and say where the trust anchor lives. [ARF Annex 2](https://eudi.dev/1.9.0/annexes/annex-2/annex-2-high-level-requirements/)
- Non-qualified issuers are not on member-state trusted lists, so the rulebook's trust-anchor pointer is how wallets find Belfry's key. [ARF](https://eudi.dev/2.8.0/architecture-and-reference-framework-main/)
- Requesting PID makes Belfry a relying party, and the relying-party registers go public on 24 December 2026. Before that, use sandbox wallets for the PID step.
- Add the mdoc format only if offline door checks without connectivity are needed.
- SD-JWT VC is still an IETF draft, and acceptance of small private issuers by national wallets is not yet proven in practice.

## Implementation phases

The event release needs Phases 0–3; DIDs and self-custody follow after the event.

1. **Phase 0 – Governance.** Who may generate invites (stewards), who revokes, refund policy, identity-proofing for key recovery, the exit right for advanced members, Directory consent rules, and the GDPR basis for participant data.
2. **Phase 1 – Onboarding and registry (event-critical).** Odoo pass products (day, week, full access) with early-bird, discount-code and company pricing; steward invite links; Stripe payment links; an idempotent webhook that creates the Odoo contact and Authentik account and creates the participant's Nostr key through Keycast's preload-user API (live once Phase 2 ships); refund and expiry handling.
3. **Phase 2 – Collective Bunker (event-critical).** Comes before the certificates, because the certificate carries the participant's npub and pass sign-in needs the apps. Deploy Keycast (Divine's fork) as the signer: keys encrypted with a KMS, per-app policies by event kind, OAuth with PKCE, signing over HTTPS or NIP-46, key export, and a preload-user API with claim links for the Odoo webhook. Adapt it: remove the Divine-specific parts (Vine IDs, minor accounts, Bluesky provisioning), connect its login to Authentik (see Decisions), and add pass-tier and expiry checks to its policies. Plus a Nostr relay whose whitelist syncs from Odoo; Blossom for photos and files; NIP-05 names on Belfry's domain; the community apps (chat, directory, calendar, proposals, marketplace, photos, links, profile).
4. **Phase 3 – EUDI pass certificates (event-critical).** P-256 issuer key in an HSM published at `.well-known/jwt-vc-issuer`; the `village-pass` `vct`, Type Metadata and attestation rulebook; OpenID4VCI issuance with a claim QR after payment, carrying the npub from Phase 2; status lists; an OpenID4VP verifier app for the door; pass-based sign-in for the apps through a small OpenID4VP-to-OIDC bridge (candidate: SUNET vc verifier) added to Authentik as an OAuth source, since Authentik has no OpenID4VP support. Test against the main national and sandbox wallets before the event.
5. **Phase 4 – Belfry root identity and member DIDs (after the event).** Organizational did:webvh with keys on hardware tokens held by 3 stewards, listing the issuer key; a did:webvh server under `id.belfry.example` for full-access members, with mandatory pre-rotation; DIDs backfilled for members who joined before this phase and added to their certificates.
6. **Phase 5 – Advanced members.** A tool to generate keys and take control of a DID; Nostr keys moved to self-custody; a documented move procedure to the member's own domain.
7. **Phase 6 – Extras.** Anonymous voting for proposals (Semaphore groups built from the registry); PGP keys via WKD; a Cashu mint for marketplace payments, only after legal review.

Candidate tooling: walt.id or Credo for SD-JWT VC with OpenID4VCI/VP; the ACA-Py did:webvh plugin and server; Odoo's Stripe integration; see the Fork Bomb section for an alternative issuer, verifier app and wallet testing.

## Key loss, recovery and exit

Self-custody has a real price: an advanced member who loses both keys loses that identifier. Members see these rules before upgrading.

| Situation | What happens |
| --- | --- |
| Day or week pass expires | The certificate's `exp` lapses; a nightly job removes the npub from the relay whitelist |
| Refund or chargeback | Odoo revokes the certificate via the status list and removes the npub from the whitelist |
| Participant loses the phone holding the wallet | Re-issue the pass to the new wallet after login through Authentik; revoke the old copy |
| Full-access member loses device or login | Belfry re-proofs the person (later via EUDI PID) and rotates the update key it holds |
| Advanced member's current key is lost or stolen | The member rotates with the pre-rotated backup key; Belfry witnesses the entry |
| Advanced member loses both keys | The identifier can no longer be updated. Belfry issues a new DID and a "successor of" credential linking it to the old one |
| Member leaves Belfry | Belfry revokes the certificate. Advanced members move their DID to their own domain; Belfry co-signs the move entry under the exit right |
| Belfry's issuer key rotates | The org DID log records the change; old keys stay in the JWKS until their credentials expire |

## Fork Bomb Solutions

Fork Bomb (Amsterdam, a spin-off of Dyne.org) was expected to supply most of Phase 3 off the shelf, but the 2026-10-22 issuer spike found that DIDroom's issuer lacks a custom vct, status lists and x5c, so it is a verifier-app and testing option rather than the issuer: an open-source issuer, verifier and wallet built for EUDI formats, plus a conformance-testing service. They don't cover did:webvh or the membership registry. [forkbomb.solutions](https://forkbomb.solutions/)

| Product | What it is | How it helps Belfry |
| --- | --- | --- |
| [DIDroom](https://forkbomb.solutions/solution/didroom/) | Open-source issuer and verifier microservices, admin dashboard, and Android/iOS holder wallet and verifier app. Supports SD-JWT, mdoc, OpenID4VCI/VP, W3C-DID/VC, and BBS+/Coconut zero-knowledge credentials | Ready-made OpenID4VCI issuer and OpenID4VP verifier for the membership certificate; verifier app for door checks; BBS+ as a future option for unlinkable presentations |
| [Credimi](https://forkbomb.solutions/solution/credimi/) | Conformance and interoperability testing against real EUDI wallets, issuers and verifiers, with a public marketplace. SaaS or on-premises (Docker) | Test Belfry's issuer against national wallets before launch; list the membership credential in the marketplace via auto-import from `.well-known` |
| [Zenroom](https://forkbomb.solutions/component/zenroom/) | Portable cryptography virtual machine (server, mobile, browser) with a readable scripting language, Zencode | Could implement Belfry-specific flows (re-issue on DID rotation) in auditable scripts |
| [W3C DID Federated Service](https://forkbomb.solutions/component/w3c-did-federate-service/) | Dyne's own federated DID implementation | A different DID method from did:webvh, so not a replacement for Phase 4 |

**Where it fits in the plan**

- **Phase 3:** evaluate DIDroom's issuer and verifier microservices against walt.id and Credo. DIDroom's appeal is the matching wallet and verifier app plus EU-funded open-source provenance.
- **Before launch:** run Belfry's issuer through Credimi against the main national wallets. This is the most direct answer to the open question of whether national wallets accept small private issuers.
- **Not covered:** did:webvh hosting and rotation (Phase 4), the registry and Authentik login (Phase 1), and the governance rules.

**Questions to ask Fork Bomb**

- [ ] Can DIDroom's issuer resolve or publish did:webvh, or only Dyne's DID method?
- [ ] Does the issuer support HAIP's `jwt-vc-issuer` metadata and P-256 keys held in an external HSM?
- [ ] Can it publish token status lists for revocation?
- [ ] Licenses and self-hosting terms for DIDroom and Credimi, and pricing for support.
- [ ] The 2026-10-22 spike found no custom `vct` with Type Metadata, no `x5c`, and only the authorization-code flow (no pre-authorized code); is that current, and is there a roadmap?
- [ ] The issuer repository has had no commits since 2025-11 while Fork Bomb forked credo-ts in October 2026; which codebase will be maintained?

## Open decisions and risks

The most urgent open decision is the event date, because it determines which EUDI wallets participants can use; the biggest long-term one is whether Belfry is a required witness for advanced members.

**Decisions**

- [x] Signing server: Keycast (Divine's fork) instead of an OpenBunker fork, decided 2026-10-22. It already has KMS-encrypted keys, per-app policies, provisioning with claim links, and active maintenance; OpenBunker stores keys unencrypted and lacks a provisioning API.
- [x] Login for the Nostr apps: Keycast trusts Authentik via OIDC (one login), decided 2026-10-22. The Keycast fork mints a server-signed UCAN session after the Authentik callback, 3 to 5 days per the spike; the Keycast claim email is not needed.
- [x] Participant login for Phase 2: passwordless. At activation the onboarding service mints a one-time Authentik sign-in link (recovery-link API, flow belfry-account-setup: signs in, no password) and Odoo mails it with the pass details; stewards re-send it from the pass form. Later sign-ins: the login page mails a one-time magic link (10 minutes, same browser for app logins) or the participant presents the pass. Participants never get a password; the Authentik admin keeps one on a separate flow. Decided 2026-10-23 (magic link replaced the first version's set-a-password link the same day).
- [x] Event date: late January 2027, decided 2026-10-22. Some national EUDI wallets may be usable by then; test the pass against the ones participants are likely to hold, and keep sandbox and open-source wallets as the baseline (see Risks).
- [ ] Belfry as required witness for advanced members (recommended), or watcher only.
- [x] Issuer stack: walt.id Issuer2 + Verifier2 (community), built 2026-10-23 as the spike recommended; provisional until the steward's check with the DIDroom maintainer, which can still add Credimi for testing. Belfry hosts the status list and the type metadata itself.
- [x] Pass sign-in bridge: Belfry's own Python OIDC bridge over Verifier2 instead of the SUNET patch (decision of 2026-10-22 superseded 2026-10-23): the SUNET OIDC path verifies nothing and its HEAD does not build locally, Verifier2 runs anyway for the door, and Belfry's code is Python. Authentik links the pass source by username on the first login (`username_link`).
- [x] Community app protocols, decided 2026-10-23: calendar NIP-52, photos NIP-68 with Blossom (gated by an active pass), proposals both NIP-88 polls and NIP-23 long-form with NIP-22 comments, chat NIP-17 (the managed key encrypts and decrypts through the signer). Keycast's `belfry-event` policy carries these kinds. Marketplace and links protocols are still open.
- [x] Branding, decided 2026-10-23: every participant-facing surface (community apps and signer, Authentik login and mails, claim, door and pass sign-in pages, the pass in the wallet, the invite and payment pages, the activation mail) says "Open Source Village, run by Belfry" and uses opensourcevillage.org's look: dark theme only, mobile first, green greys with a lime accent, Geist. Belfry stays the operator name (footers, issuer display name, `iss`, internal identifiers); no DNS change yet. A shared `brand/` folder in the repo is the single source, served by the onboarding service at `/brand`. Stewards do not edit text in the Odoo UI: copy lives in code (docs/NEXT_STEPS.md §3l).
- [x] Single sign-on for the community apps, decided 2026-10-23: chat and calendar offer "Sign in with Open Source Village" (Keycast OAuth with PKCE; the token response's bunker URL connects the app over NIP-46) and try it silently once per tab on load (prompt=none as a top-level redirect, not a hidden iframe). Keycast answers prompt=none with a code when the participant has a Keycast or Authentik session, otherwise with login_required, and the app shows the button. Belfry's own apps are trusted, registered clients that skip Keycast's consent screen; their button goes straight to the Authentik sign-in page. Built as Keycast patch 0008 and one patch per app (docs/NEXT_STEPS.md §3q).
- [x] Logout ends the sign-in everywhere, decided 2026-10-24: logging out of an app revokes that app's authorization at Keycast and ends both the Keycast and the Authentik session (Authentik's end-session endpoint with an invalidation flow that really logs out), then returns to the app signed out; otherwise the silent check would sign the participant straight back in. A trusted app keeps at most five connected authorizations per participant, and abandoned sign-ins are revoked. Keycast patch 0010 (docs/NEXT_STEPS.md §3r).
- [x] Nostr keys are created eagerly at payment through preload-user, decided 2026-10-22, so the certificate carries the npub.
- [ ] Directory and profile defaults: what is visible to other participants without opt-in.
- [ ] Whether to request PID at checkout, and which attributes.
- [ ] Cashu mint: go or no-go after legal review.

**Verification results (2026-10-22)**

Five spikes ran against the real software; the notes are in `docs/research/` of the belfry repository.

- Keycast (commit fe9c6460): preload-user generates the key at once, so the npub exists at payment time. The claim link is a separate step that the happy path does not show. `provisioning_operation_id` is not on preload-user; idempotency comes from `vine_id`, which can hold the Odoo contact id. Policies refuse disallowed kinds over HTTP RPC and NIP-46. Expiry and refunds fit the user status check, not policies. Login through Authentik can mint server-signed UCAN sessions, estimated 3 to 5 days.
- Authentik 2026.8.5: the OIDC-source design holds end to end (identifier matching, no enrollment, linking from user settings). The webhook can pre-create the source link by API, so participants never link by hand. Extra claims such as npub need a user_write stage in the source flow.
- SUNET vc verifier (commit 2725c341): the request side does everything needed, but its OIDC login path verifies no issuer signature, no key binding and no status. Unpatched, anyone could log in as any member. The ID token `sub` can only be a salted hash of member_number.
- walt.id Issuer2 and Verifier2 v1.2.0 issued and verified a village-pass SD-JWT VC with no custom code. The community edition has no status-list service, so Belfry builds one; Type Metadata is self-hosted; x5c should be the primary issuer-key path for EUDI wallets. Credo is the fallback.
- Odoo 19 Community: passes, early-bird prices, the company surcharge and promo codes work with standard modules. Odoo's own Stripe provider handles payment from Odoo payment links (Stripe-hosted Payment Links are ignored by Odoo) and is keyed on the PaymentIntent and invoice, not on the Stripe event id. Chargebacks never reach Odoo. Invite links need a small custom module.

Decisions the spikes added:

- [x] OpenID4VP bridge: patch the SUNET verifier's OIDC path to reuse its verifying code with a pinned issuer key and a status check, decided 2026-10-22; offer the patch upstream and carry a fork meanwhile.
- [x] Payment flow: Odoo payment link plus Odoo's Stripe provider, keyed on invoice and PaymentIntent, with Belfry's own Stripe endpoint only for disputes (keyed on the event id), decided 2026-10-22.
- [x] The company surcharge compounds with early-bird prices (early-bird price × 1.5), decided 2026-10-22.
- [x] Odoo 19 Community, self-hosted in Docker Compose on one VM with the other services, decided 2026-10-22. Belfry's own code is Python, in one monorepo.

**Risks**

| Risk | Mitigation |
| --- | --- |
| Participants arrive without a working EUDI Wallet | Support sandbox and open-source wallets; keep Authentik login and a printed QR fallback at the door |
| National wallets reject or poorly display small private issuers | Conformance testing against national wallets before launch; publish the attestation rulebook |
| Webhook failures create duplicate users or unpaid access | Idempotent handler keyed on invoice and PaymentIntent (Odoo's Stripe provider) and on the Stripe event id (Belfry's dispute endpoint); access only after the invoice is marked paid |
| Custodial Nostr keys in the bunker are breached | KMS encryption, per-app permissions, key export, audit logs; Keycast has no independent audit, so review it before launch |
| Cashu mint triggers payment or e-money regulation | Legal review before launch; keep it out of the event release |
| did:webvh implementations have validation bugs | Maintained libraries, cross-resolver tests, watchers |
| SD-JWT VC spec changes before final | Track the IETF draft and HAIP; isolate issuance in one service |
| GDPR exposure from directory, billing and PID data | Opt-in directory, verify PID without storing it, minimal Odoo records |

## Sources

- [did:webvh specification (DIF)](https://identity.foundation/didwebvh)
- [didwebvh-rs v0.5.3 release notes](https://github.com/decentralized-identity/didwebvh-rs/releases/tag/v0.5.3)
- [SD-JWT VC issuer key resolution discussion](https://github.com/oauth-wg/oauth-sd-jwt-vc/issues/232)
- [EUDI ARF](https://eudi.dev/2.8.0/architecture-and-reference-framework-main/) and [Annex 2](https://eudi.dev/1.9.0/annexes/annex-2/annex-2-high-level-requirements/)
- [Fork Bomb: DIDroom](https://forkbomb.solutions/solution/didroom/) and [Credimi](https://forkbomb.solutions/solution/credimi/)
