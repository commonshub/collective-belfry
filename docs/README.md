# Documentation

## How the system works

| Document | What it covers |
| --- | --- |
| `../README.md` | what Belfry is, what a participant does, the parts |
| `../infra/README.md` | the Compose stack: services, ports, scripts, bootstrap, the smoke test |
| `../infra/DEPLOY.md` | running it on a server: hostnames, proxy, admin network, custody seed, backups |
| `pass-sign-in.md` | signing in with the village pass from an EUDI wallet (the OpenID4VP to OIDC bridge) |
| `../infra/issuer/README.md` | the pass as an SD-JWT VC: issuer, verifier, keys, status list, door check |
| `../infra/keycast/README.md` | the signer and Belfry's patches to it |
| `../services/onboarding/README.md` | the onboarding service: activation, revocation, its endpoints |
| `../odoo/addons/belfry_pass/README.md` | the Odoo module: products, pricing, invites, webhooks |
| `../infra/relay/README.md`, `../infra/blossom/README.md`, `../infra/mint/README.md` | community relay, file server, token mint |
| `../infra/apps/*/README.md`, `../services/vote/README.md`, `../services/village/README.md` | the community apps |
| `../brand/README.md` | colours, fonts, logos and where copies of them live |

## Plan

`identity-membership-plan.md` is a snapshot of the plan: goals, architecture, pass tiers, the
pass certificate, phases, risks and the decisions taken. The plan's source of truth is a shared
online document; the snapshot can lag behind it. Parts of the plan were decided against later
(for example member DIDs); its decision list says which.

## Working records

These are dated records of how the project was built. They explain why things are the way they
are, but describe the state at the time they were written.

| Document | What it is |
| --- | --- |
| `NEXT_STEPS.md` | the build log: every slice with its decisions, checks and open points, and the next steps |
| `slice-1-onboarding.md` | the interface specification written for the first build slice (Odoo, onboarding service, accounts) |
| `real-user-test.md` | the script for testing the whole path with a real person, mailbox and wallet |
| `research/` | technical spikes and evaluations written before building: Keycast and other signers, Authentik, the SUNET verifier, walt.id and other issuers, Odoo and Stripe, a consistency review of the plan |
