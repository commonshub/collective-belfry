# Collective Belfry

Access and identity for Open Source Village: EUDI-wallet pass certificates, and a Nostr-based community behind a managed signer whose keys participants can take into their own custody. Odoo is the registry of who holds which pass.

- Plan: `docs/identity-membership-plan.md` (snapshot of the live Claude Doc) and `docs/NEXT_STEPS.md`
- Research and spike notes: `docs/research/`
- First build slice (Phase 1 onboarding): `docs/slice-1-onboarding.md`

## Layout

| Path | What |
| --- | --- |
| `infra/` | Docker Compose stack: Postgres, Odoo 19, Authentik, Keycast, relay, onboarding service |
| `odoo/addons/belfry_pass/` | Odoo module: passes, steward invites, pricing data, webhook automation |
| `services/onboarding/` | Python service: activates passes after payment, revokes on refund or dispute, expires passes |
| `docs/` | Plan, decisions, research |

## Quick start (local)

```
make up          # start the stack
make bootstrap   # create the Odoo database, admin tokens, Keycast policy
make smoke       # invite → pay → accounts created, replay-safe
make down
```

See `infra/README.md` for details and `.env.example` for the secrets.
