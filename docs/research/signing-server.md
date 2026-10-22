# Signing server evaluation (2026-10-22)

Decision: use Keycast (Divine's fork) for Phase 2 instead of forking OpenBunker. Recorded in the plan's Decisions list.

## OpenBunker (github.com/opencollective/openbunker)

MIT, 127 commits, mostly quiet in 2026. Next.js + Prisma + Supabase (Discord login), ~2,500 lines of core code.

- Reusable: NIP-46 backend (`server/nip-46-backend/`, `multi-bunker-server.ts`), scopes with per-app allow-lists, sessions, connection tokens, login popup, NIP-05 helpers.
- Blockers:
  - Private keys stored unencrypted: the `ncryptsec` column holds raw hex (`src/app/(openbunker)/api/scopes/route.ts:103`, read in `server/nip-46-backend/index.ts:276`).
  - Login tied to Supabase and Discord.
  - No provisioning API for an external webhook.
  - NIP-04 by default; 4 test files.

## Keycast, Divine fork (github.com/divinevideo/keycast) — chosen

MIT, 659 commits (359 in 2026, last 2026-10-19), in production at login.divine.video. Rust (Axum) + SvelteKit.

| Belfry need | Keycast |
| --- | --- |
| Encrypted keys | AES-256-GCM; master key in file, GCP KMS or AWS KMS (`KMS_PROVIDER`) |
| Odoo webhook creates key | `POST /api/admin/preload-user`; idempotent on `vine_id` per tenant (same id returns the existing pubkey). Correction 2026-10-22: `provisioning_operation_id` exists only on `create-minor-account`, not on preload-user; see `keycast-spike.md` |
| Participant takes over account | `POST /api/admin/claim-tokens` + email-confirmed claim flow |
| Per-app permissions | Policies from permission templates (event kinds, encryption targets), checked before every signature |
| App connection | OAuth 2.0 + PKCE; signing via `POST /api/nostr` (HTTP RPC) or NIP-46 bunker URL |
| Key export | `/user/export-key` |

Gaps:
- No upstream OIDC login (own email/password accounts) — open decision on Authentik integration.
- Divine-specific code to adapt or disable: `vine_id` on preload-user, minor-account rules, ATProto/Bluesky provisioning.
- No independent security audit.
- Admin API reference: `docs/keycast-admin-api-reference.md`.

## Rejected alternatives

- kind-0/nsecbunkerd: abandoned (repo reduced to one commit, README "Gigi was right").
- erskingardner/keycast (now marmot-protocol): active, but single-host for individuals/small teams; every admin action needs an external Nostr signer.
- Frostr / Igloo: FROST threshold signing; unaudited. Candidate for Phase 5 self-custody, not for managed event keys.
- nostrband/noauth-enclaved: AWS Nitro Enclave signer; AWS-dependent, authors warned against real keys.
