# Build slice 1: Phase 1 onboarding, end to end, locally

Goal: a pass paid in Stripe test mode (or marked paid in Odoo during local tests) creates every account it should, exactly once. Decisions behind this are in `docs/NEXT_STEPS.md` §2; the facts come from the spike notes in `docs/research/`.

This file is the contract between the three parts. Change it before changing an interface.

Since 2026-11-19 Keycast and Authentik are gone: Belfry's own signer (`services/signer`) holds the
keys and the onboarding service is the sign-in. The activation steps, fields and clients below
describe that; the full service is in `services/onboarding/README.md`, the stack in
`infra/README.md`.

## Layout

```
infra/                      Docker Compose for the whole stack (one VM target)
  docker-compose.yml
  .env.example
  odoo/odoo.conf
  scripts/                  bootstrap and smoke scripts
odoo/addons/belfry_pass/    Odoo 19 Community module: pass model, invites, pricing data, automation rules
services/onboarding/        Python 3.12 service (FastAPI): webhooks, activation, revocation, expiry, sign-in
services/signer/            Rust: the members' Nostr keys, an internal API, the NIP-46 bunker
docs/                       plan snapshot, research notes, this contract
```

## Services and ports (local)

| Service | Image | Port on host | Notes |
| --- | --- | --- | --- |
| postgres | postgres:16 | internal | database `odoo` (and the groups relay's, added later) |
| odoo | odoo:19 | 8069 | `--addons-path` includes `/mnt/extra-addons` = `odoo/addons` |
| signer | built from `services/signer` | internal | the members' keys, derived from the custody seed and sealed with `signer_master_key`; internal network `signer` only |
| bunker-relay | ghcr.io/verse-pbc/relay_builder:latest | 3080 | NIP-46 relay for the signer (was `keycast-relay`) |
| onboarding | built from `services/onboarding` | 8100 | Belfry's glue and sign-in (login, account page, the apps' OAuth), claim pages, status list, door, pass sign-in |
| issuer | waltid/issuer-api2:1.2.0 | 7005 | OpenID4VCI issuer of the pass certificates (Phase 3); config `infra/issuer/` |
| verifier | waltid/verifier-api2:1.2.0 | 7004 | OpenID4VP verifier for the door and the pass sign-in (Phase 3); config `infra/verifier/` |
| blossom, blossom-gate | hzrd149/blossom-server (digest), caddy:2 | 3100 (gate) | file server for the apps; the gate asks the onboarding service whether the uploader holds an active pass (Phase 2) |
| mailpit | axllent/mailpit | 8025 | local SMTP sink for Odoo's mails |

Test stacks used by module or service tests must use other ports (Odoo test stack: 8169) and their own compose project names.

## Odoo module `belfry_pass`

Depends on `sale_management`, `account_payment`, `payment_stripe`, `loyalty`, `sale_loyalty`, `base_automation`, `portal`.

**Fields on `res.partner`**

- `belfry_member_number` Char, unique, from sequence `belfry.member` (`OSV-0001`). Assigned when the partner first gets a pass. This is the immutable `sub` of the pass certificate and Issuer2's sign-in, and finds the participant at the pass sign-in.
- `belfry_npub` Char, indexed.
- `belfry_key_version` Integer, `belfry_key_custody` Selection: the signer's key derivation and whether Belfry or the participant holds the key (added with the custody seed and self-custody).
- `belfry_steward` Boolean, indexed: a steward (signs in to the steward pages, gets the stewards' mails); set by `python -m belfry_onboarding add-steward` (2026-11-19; was the Authentik group `belfry-stewards`).

`belfry_authentik_user_id` was dropped on 2026-11-19.

**Fields on `product.template`**

- `belfry_tier` Selection `day` / `week` / `full`, empty for non-pass products.
- `belfry_duration_days` Integer: 1, 7, 0 (0 = the whole event period).

**Model `belfry.pass`** (one row per purchased pass)

| Field | Type | Notes |
| --- | --- | --- |
| partner_id | Many2one res.partner, required | |
| tier | Selection day/week/full | copied from the product |
| product_id | Many2one product.product | |
| sale_order_id | Many2one sale.order | |
| invoice_id | Many2one account.move | SQL unique |
| transaction_id | Many2one payment.transaction | the `done` transaction |
| stripe_payment_intent | Char | `provider_reference` of the transaction, SQL unique when set |
| date_start, date_end | Date | set by activation |
| state | Selection pending / active / expired / revoked | default pending |
| invite_id | Many2one belfry.invite | |
| npub | Char | copied at activation (audit) |
| nostr_pubkey | Char | hex (`keycast_pubkey` until 2026-11-19; `authentik_user_id` was dropped then) |
| activated_at, revoked_at | Datetime | |
| revoke_reason | Selection refund / chargeback / steward / expiry | |
| stripe_dispute_id | Char | |
| credential_status_index | Integer | the pass's bit in the Token Status List (= pass id), set at activation |
| login_setup_url, login_setup_expires_at | Char, Datetime | one-time sign-in link `<belfry.public_url>/signin/<secret>` (added 2026-10-23 as an Authentik account-setup link; Belfry's own since 2026-11-19); writing the URL on an active pass queues the activation mail |
| signin_token, signin_used_at | Char (admins only), Datetime | the link's secret and when it was spent (once) |
| login_mail_sent_at | Datetime | set when the mail was queued |
| claim_token | Char, indexed, admins only | secret of the claim link (`belfry.public_url` + `/claim/<token>`), set at activation (Phase 3, 2026-10-23) |
| claim_url | Char, computed | the claim link, in the activation mail |

**Model `belfry.invite`**

| Field | Type | Notes |
| --- | --- | --- |
| token | Char, unique | 32 bytes urlsafe random, generated on create |
| steward_id | Many2one res.users, required | default current user |
| expires_at | Datetime, required | default now + 14 days |
| state | Selection open / used / expired / revoked | |
| used_by_partner_id | Many2one res.partner | |
| pass_id | Many2one belfry.pass | |
| note | Char | |
| url | Char, computed | `<base_url>/belfry/invite/<token>` |

Stewards are members of group `belfry_pass.group_steward` (create, read own invites). Admins: `belfry_pass.group_admin`, which also grants read access to `account.move`, `account.move.line` and `payment.transaction` so the onboarding service user needs no accounting or settings groups (2026-10-23).

**Model `belfry.stripe.event`**: `event_id` Char unique, `type` Char, `received_at` Datetime, `pass_id` Many2one, `result` Char. Written only by the onboarding service's Stripe endpoint.

**Data**

- Products `belfry_pass.product_day` (€50, tier day, 1 day), `product_week` (€100, week, 7), `product_full` (€200, full, 0). Type service, no tax by default (VAT is an open question).
- Pricelist rules on the default pricelist: early-bird fixed prices €40 / €80 / €160 valid until `ir.config_parameter` `belfry.early_bird_until` (default 2026-12-31). Applied by a post-init hook or XML with a date.
- Pricelist `belfry_pass.pricelist_company` "Belfry companies": one formula rule, +50% on the public pricelist price (so it compounds with early bird: 60 / 120 / 240).
- Loyalty program `belfry_pass.promo_example` with promo code `OSV10`, 10% off pass products, as the example discount code.
- Config parameters: `belfry.onboarding_url` (default `http://onboarding:8100`), `belfry.hook_token` (shared secret, appended as `?token=` to webhook URLs), `belfry.event_start` (2027-01-25), `belfry.event_end` (2027-01-31), `belfry.apps_url` (the account page with the community apps, in the activation mail; default `http://localhost:8100/account`), `belfry.login_url` (the login page; default `http://localhost:8100/login`), `belfry.public_url` (the onboarding service as browsers see it, for claim links; default `http://localhost:8100`).
- Model `belfry.login.code` (2026-11-19): the login page's e-mail sign-in. Odoo mails a one-time link and a 6-digit code (template `mail_template_login_code`), valid 10 minutes, five tries per code, five mails per address in 30 minutes, only to an address with an active pass or a steward's; `belfry_may_sign_in(partner_id)` answers whether a contact may sign in.
- Mail template `belfry_pass.mail_template_pass_activated` (added 2026-10-23): pass details and the login setup link, addressed to the pass holder, sent by `belfry.pass.write` when `login_setup_url` is written on an active pass (activation and "Send new login link"). Mails are kept (`auto_delete` off).
- Automation rules (`base_automation` + server action of type webhook):
  1. `account.move` on create/update of `payment_state`, domain `move_type = out_invoice and payment_state in (paid, in_payment) and state = posted` → POST `${belfry.onboarding_url}/hooks/odoo/invoice-paid?token=${belfry.hook_token}` with the record's `id`.
  2. `payment.transaction` on create/update of `state`, domain `operation = refund and state = done` → POST `.../hooks/odoo/refund?token=...` with the record's `id`.
  The webhook action sends `{"_model", "_id", "_action", ...fields}`; the receiver reads `_id`.

**Controller** (`portal`, public): `GET /belfry/invite/<token>` shows the three passes with the prices the visitor will pay (early bird applied) and a billing form (name, email, company name optional, VAT optional). `POST` creates or finds the partner by email (company → a company partner with the person as contact; pricelist = company pricelist when a company name is given), creates a `sale.order` with the chosen product (and an optional promo code applied through the loyalty wizard), confirms it, creates and posts the invoice, generates the Odoo payment link (`payment.link.wizard`), creates the `belfry.pass` in state pending linked to invite, order and invoice, marks the invite used, and redirects to the payment link. Expired, used or revoked invites show a plain error page.

**Buttons** on the pass form (stewards): "Send new login link" posts to the onboarding service's `/admin/passes/<id>/login-link`; the service writes the new link and the mail goes out. "Revoke pass" posts to `/admin/passes/<id>/revoke` (reason `steward`).

**Cron**: none in slice 1 (expiry runs in the onboarding service).

**Tests** (Odoo test framework, `odoo -d test --test-enable -i belfry_pass`): prices for a normal and a company partner with and without early bird; promo code; invite lifecycle (open → used, expired refused, second use refused); the automation rules exist and their domains match a paid invoice and a refund transaction.

## Onboarding service (`services/onboarding`)

Python 3.12, FastAPI + httpx, `pyproject.toml` with `uv` or pip; package `belfry_onboarding`. Stateless: all state lives in Odoo. One Dockerfile.

**Environment**

```
ODOO_URL=http://odoo:8069   ODOO_DB=odoo   ODOO_USER=onboarding@belfry.local   ODOO_API_KEY=...
SIGNER_URL=http://signer:8200   SIGNER_TOKEN=...      # Belfry's signer (2026-11-19; was KEYCAST_*)
SESSION_SECRET=...   LOGIN_LINK_DAYS=7                # the sign-in (2026-11-19; was AUTHENTIK_*)
OAUTH_CLIENTS=...    DERIVE_SECRETS=...               # the apps' sign-in
ISSUER_URL, ISSUER_PUBLIC_URL, VERIFIER_URL, PUBLIC_URL, STATUS_LIST_URL, STATUS_LIST_TTL, ISSUER_JWK_PATH, ISSUER_X5C_PATH, BRIDGE_JWK_PATH, ISSUER_OIDC_CLIENT_ID, ISSUER_OIDC_CLIENT_SECRET   # Phase 3, see services/onboarding/README.md
HOOK_TOKEN=...                      # must equal Odoo's belfry.hook_token
STRIPE_WEBHOOK_SECRET=whsec_...     # for /hooks/stripe only
EVENT_START=2027-01-25  EVENT_END=2027-01-31
LOG_LEVEL=info
```

**HTTP endpoints**

| Method, path | Auth | Body | Behaviour |
| --- | --- | --- | --- |
| GET /health | none | | `{"status":"ok"}` |
| POST /hooks/odoo/invoice-paid?token= | HOOK_TOKEN | Odoo webhook payload, reads `_id` | runs `activate_pass(invoice_id)`; always 200 once the token is valid, errors are logged (Odoo does not retry) |
| POST /hooks/odoo/refund?token= | HOOK_TOKEN | reads `_id` (payment.transaction) | runs `revoke_for_refund(transaction_id)` |
| POST /hooks/stripe | Stripe signature | raw Stripe event | handles `charge.dispute.created` (revoke, reason chargeback) and `charge.dispute.closed` with status `won` (reactivate); idempotent on `event.id` via `belfry.stripe.event`; other types recorded and ignored |
| POST /admin/reconcile?token= | HOOK_TOKEN | | runs one `reconcile()` pass (see below) |
| POST /admin/passes/<id>/login-link?token= | HOOK_TOKEN | | new one-time sign-in link for an active pass, written on the pass (Odoo mails it); added 2026-10-23 |
| POST /admin/passes/<id>/revoke?token= | HOOK_TOKEN | | steward revocation |
| GET /claim/<token>, /claim/<token>/offer | claim token | | claim page / offer JSON (Phase 3) |
| GET /status/1, GET /vct/village-pass/v1 | none | | Token Status List, Type Metadata (Phase 3) |
| GET /door, POST /door/sessions, GET /door/sessions/<id> | none | | door check (Phase 3) |
| /login, /login/pass, /signin/<secret>, /account, /api/oauth/*, /api/nostr, /oidc/* | session, tokens | | the sign-in, the account page, the apps' sign-in and Issuer2's (2026-11-19; the OpenID4VP-to-OIDC bridge `/bridge/*` for Authentik is gone); `services/onboarding/README.md` |

**CLI**: `python -m belfry_onboarding reconcile` and `python -m belfry_onboarding expire`, for cron.

**Core operations** (module `belfry_onboarding.core`), each idempotent and safe to retry at any step:

- `activate_pass(invoice_id)`:
  1. Load the invoice. Require `move_type = out_invoice`, `state = posted`, `payment_state in (paid, in_payment)`, and a `payment.transaction` in `state = done` with `operation` not refund and a non-empty `provider_reference`. Otherwise log and return `not_paid`.
  2. Find `belfry.pass` by `invoice_id`; if none, create one (partner from the invoice, tier from the product with `belfry_tier`). If `state = active`, return `already_active` and do nothing else.
  3. Assign `belfry_member_number` to the partner if empty.
  4. Dates: `date_start = max(payment date, EVENT_START)`; `date_end = date_start + duration_days` for day/week, `EVENT_END` for full.
  5. Signer: `PUT /accounts/odoo-partner-<partner id>` with the partner's `belfry_key_version` (idempotent: an existing account is returned; the key derives from the custody seed, and a locked seed holds the activation with 503 until the next webhook or `reconcile`). The pubkey → bech32 `npub`. Then `POST /accounts/<vine_id>/status` `active`, in case the account was suspended by an earlier expiry. A participant who joins with their own key gets a keyless account. (Until 2026-11-19 this step was Keycast's `preload-user`, preceded by an Authentik user and source link, both gone.)
  6. A one-time sign-in link `<PUBLIC_URL>/signin/<secret>`, valid `LOGIN_LINK_DAYS`: spending it sets Belfry's session cookie (until 2026-11-19 an Authentik account-setup link).
  7. Write back: partner `belfry_npub`; pass `npub`, `nostr_pubkey`, `transaction_id`, `stripe_payment_intent`, dates, `activated_at`, `login_setup_url`, `login_setup_expires_at`, `signin_token`, and `state = active` last (one write; Odoo mails the link). Later additions run between 5 and 7 (relay whitelist and profile, village tokens, claim token; `services/onboarding/README.md`).
- `revoke_pass(pass_id, reason)`: set `state = revoked`, `revoked_at`, `revoke_reason`; if the partner has no other active pass, set the signer account's status to `suspended` (nothing is signed for them; their sign-in ends because it asks Odoo for an active pass). Idempotent.
- `revoke_for_refund(transaction_id)`: load the refund transaction, follow `source_transaction_id` to the pass (by `transaction_id`), `revoke_pass(..., "refund")`.
- `reactivate_pass(pass_id)`: for a won dispute; state back to active if `date_end` has not passed, signer account active.
- `reconcile()`: search invoices paid in the last 30 days whose pass is missing or pending and call `activate_pass`; search done refund transactions whose source pass is still active and revoke. The safety net for the fire-and-forget Odoo webhook.
- `expire()`: passes `active` with `date_end < today` → `state = expired`, `revoke_reason = expiry`, signer account suspended if no other active pass.

**Clients** (`belfry_onboarding.clients`): `OdooClient` (JSON-RPC `/jsonrpc`, `execute_kw`, API key as password), `SignerClient` (`signer.py`, the signer's internal API, bearer `SIGNER_TOKEN`). Thin, typed, no business logic. (`AuthentikClient` and `KeycastClient` were removed on 2026-11-19.)

**Tests** (pytest): fake Odoo (in-memory dict-backed `execute_kw` that supports the calls used), an in-memory fake of the signer. Cases: (1) paid invoice → one signer account, pass active with a sign-in link; (2) the same webhook replayed three times → no additional creates, same npub; (3) unpaid or `pending` transaction → nothing created; (4) refund → pass revoked, signer account suspended; (5) a second active pass for the same partner keeps the signer account active on refund of the first; (6) expiry job expires only passes with `date_end < today`; (7) Stripe dispute event delivered twice → one revoke, second call returns `duplicate`; invalid signature → 400.

## Infra

- One `docker-compose.yml` with the services above; `.env.example` lists every secret; `scripts/gen-secrets.sh` writes `.env` (settings) and `infra/secrets/` (one file per secret: the signer's token and master key, the session secrets, hook token, Odoo admin password, …; `infra/README.md`, "Secrets").
- `scripts/bootstrap.sh`: waits for health, creates the Odoo database with `belfry_pass` installed (`odoo -d odoo -i belfry_pass --stop-after-init`), creates the Odoo API key for the onboarding user and writes it into `infra/secrets/`, and checks that the signer is up. (Until 2026-11-19 it also bootstrapped the Keycast admin and policy and Authentik's blueprints.)
- `scripts/smoke.sh`: creates an invite through JSON-RPC, drives the invite form with curl, registers a manual payment on the invoice (as Stripe test mode would), waits for the onboarding webhook, and checks that the pass is active and the signer account exists. Then replays the webhook and checks nothing was duplicated.
- `Makefile` at the repo root: `make up`, `make bootstrap`, `make smoke`, `make down`, `make test-odoo`, `make test-onboarding`.

## Out of scope for this slice

Blossom, NIP-05 names, the Stripe webhook registration on a public URL. Added since on 2026-10-23: the relay whitelist and kind-0 profiles (`infra/relay/README.md`), the Keycast login through Authentik and the participants' setup links (Phase 2), and the pass certificates with status list, door check and pass bridge (Phase 3, `infra/issuer/README.md`); the bridge was Belfry's own Python code over walt.id Verifier2, not the SUNET patch. On 2026-11-19 Keycast and Authentik were replaced by Belfry's signer and the onboarding service's own sign-in (login page, account page, the apps' OAuth, NIP-05 names from Odoo, the pass sign-in at `/login/pass`; `docs/pass-sign-in.md`).
