# Belfry onboarding service

Glue between Odoo (registry and billing), Authentik (login) and Keycast (Nostr signer). It activates a pass once its invoice is paid, revokes on refund or chargeback, reinstates on a won dispute and expires passes nightly. It is stateless: every piece of state lives in Odoo (`belfry.pass`, `res.partner`, `belfry.stripe.event`). Contract: `docs/slice-1-onboarding.md`, section "Onboarding service".

## Layout

| Path | What |
| --- | --- |
| `belfry_onboarding/settings.py` | Environment (pydantic-settings), names as in the contract |
| `belfry_onboarding/clients/odoo.py` | JSON-RPC `/jsonrpc` client: `common.authenticate` once, then `object.execute_kw`; helpers `search_read`, `read`, `write`, `create`, `call` |
| `belfry_onboarding/clients/authentik.py` | REST client with the bearer token: users and OAuth source connections |
| `belfry_onboarding/clients/keycast.py` | `POST /api/admin/preload-user` (admin UCAN), `PUT /api/admin/users/:pubkey/status` (service token) |
| `belfry_onboarding/core.py` | `activate_pass`, `revoke_pass`, `revoke_for_refund`, `reactivate_pass`, `reconcile`, `expire`, dispute handlers |
| `belfry_onboarding/app.py` | FastAPI routes; `handle_stripe_event` |
| `belfry_onboarding/stripe_sig.py` | `Stripe-Signature` HMAC verification without the Stripe SDK |
| `belfry_onboarding/nostr.py` | hex pubkey to `npub1...` (bech32, NIP-19) |
| `belfry_onboarding/__main__.py` | `python -m belfry_onboarding reconcile` / `expire` |
| `tests/` | `FakeOdoo` (in-memory `execute_kw`), respx fakes for Authentik and Keycast, the contract's cases |

## Run

```
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest                      # unit tests, no services needed
ruff check . && ruff format --check .
uvicorn belfry_onboarding.app:app --port 8100      # needs the environment below
python -m belfry_onboarding reconcile              # cron: every few minutes
python -m belfry_onboarding expire                 # cron: nightly
docker build -t belfry-onboarding:local .
```

## Environment

```
ODOO_URL=http://odoo:8069   ODOO_DB=odoo   ODOO_USER=onboarding@belfry.local   ODOO_API_KEY=...
AUTHENTIK_URL=http://authentik-server:9000   AUTHENTIK_TOKEN=...   AUTHENTIK_SOURCE_SLUG=pass-bridge
KEYCAST_URL=http://keycast:3000   KEYCAST_ADMIN_TOKEN=...   KEYCAST_SERVICE_TOKEN=...
HOOK_TOKEN=...                      # must equal Odoo's belfry.hook_token
STRIPE_WEBHOOK_SECRET=whsec_...     # for /hooks/stripe only
EVENT_START=2027-01-25  EVENT_END=2027-01-31
LOG_LEVEL=info
```

Extras, not in the contract, all optional:

- `KEYCAST_TENANT_DOMAIN` (compose sets it, default none): sent as the `Host` header on every Keycast call, because Keycast takes the tenant from `Host` and rejects hosts outside `ALLOWED_TENANT_DOMAINS` (inside Compose the URL host is `keycast`, which is not a tenant).
- `AUTHENTIK_USER_PATH` (default `belfry/participants`).
- `RECONCILE_DAYS` (default 30): how far back `reconcile` looks, by `invoice_date`.
- `RELAY_URL` (default empty) and `NIP05_DOMAIN` (default `belfry.local`): when `RELAY_URL` is set, activation publishes the participant's kind-0 profile to that relay (see below). Empty: no profile is published.
- `ALLOW_MANUAL_PAYMENTS` (default `false`): accept a posted, paid invoice that has **no** `payment.transaction` at all (a payment registered by hand in Odoo, as the smoke script does). With the default, the contract rule applies: a `done` non-refund transaction with a `provider_reference` is required, otherwise the hook answers `not_paid`. Keep it off in production, where Odoo's Stripe provider always creates the transaction; the smoke script creates one too.

## Endpoints

| Method, path | Auth | Behaviour |
| --- | --- | --- |
| `GET /health` | none | `{"status":"ok"}` |
| `POST /hooks/odoo/invoice-paid?token=` | `HOOK_TOKEN` | reads `_id`, runs `activate_pass`; 200 with `{"result": ...}` even on handled errors (logged); 401 on a bad token |
| `POST /hooks/odoo/refund?token=` | `HOOK_TOKEN` | reads `_id` of the `payment.transaction`, runs `revoke_for_refund` |
| `POST /hooks/stripe` | `Stripe-Signature` | verifies the signature (300 s tolerance), records the event in `belfry.stripe.event` first, then handles `charge.dispute.created` (revoke, reason `chargeback`) and `charge.dispute.closed` with `status = won` (reactivate). Replays answer `{"result": "duplicate"}`. Other types are recorded with result `ignored`. A delivery whose handling raised is recorded as `error: ...` and processed again on Stripe's retry. |
| `POST /admin/reconcile?token=` | `HOOK_TOKEN` | one `reconcile()` pass |

Result codes from `activate_pass`: `activated`, `already_active`, `already_revoked`, `already_expired`, `not_paid`, `not_found`.

## What the operations do, and the details the contract left open

**activate_pass(invoice_id)** follows the contract's seven steps. Decisions taken while coding:

- Only a `pending` pass is activated. A `revoked` or `expired` pass is left alone (`already_revoked` / `already_expired`): a later write to the invoice's `payment_state` must not silently reinstate a refunded pass. Reinstating is `reactivate_pass` (won dispute).
- Pass creation prefers the module's `belfry.pass.belfry_find_or_create_for_invoice(invoice_id)`. If Odoo reports that method as missing (older module), the service reads the invoice lines, takes the first product with `belfry_tier`, and creates the pass itself.
- Member number: `res.partner.belfry_ensure_member_number([partner_id])`, then re-read the field. If the method is missing, the service writes `OSV-<partner id, 4 digits>`.
- Payment date = the `done` transaction's `last_state_change` (date part); without one (manual payment, see `ALLOW_MANUAL_PAYMENTS`) the invoice's `invoice_date`; without that, today. `date_start = max(payment date, EVENT_START)`; `date_end = date_start + belfry_duration_days` (product field; 1/7 by tier when unreadable) for day/week, `EVENT_END` for full or duration 0.
- Authentik: lookup `GET /api/v3/core/users/?attributes={"belfry_member_number": "OSV-0001"}`, then `?email=`, else `POST /api/v3/core/users/` with `username` = member number lowercased, `name`, `email`, `path`, `attributes = {belfry_member_number, odoo_partner_id}`. Then `GET /api/v3/sources/user_connections/oauth/?user=<pk>&source__slug=<slug>`; if empty, `POST /api/v3/sources/user_connections/oauth/` with `{"user": pk, "source": "<slug>", "identifier": "<member number>"}`. This POST is best effort: authentik 2026.8.5 rejects it (`user` is read-only in the serializer, verified 2026-10-22 in the smoke test), so the service logs a warning and continues; the participant links the pass once from user settings. No enrollment or password-reset link is triggered (left to the Phase 2 login work).
- Keycast: `vine_id = odoo-partner-<partner id>`, `username` = member number lowercased, `display_name` = partner name; the response `pubkey` becomes the npub; then status `active`.
- Profile (Phase 2, directory default decided 2026-10-22): with `RELAY_URL` set, the service signs a kind-0 event `{"name", "display_name", "nip05": "<member number>@<NIP05_DOMAIN>"}` through Keycast's `POST /api/nostr` with the user UCAN that preload-user returned, publishes it to the relay, and answers the relay's NIP-42 AUTH challenge the same way. Nothing else about the participant (email, company, role) is published. The UCAN is used for these two signatures only and never stored. Failures are logged and do not block activation; `reconcile` does not retry profiles yet.
- Write-back: partner `belfry_authentik_user_id`, `belfry_npub`; pass `npub`, `keycast_pubkey`, `authentik_user_id`, `transaction_id`, `stripe_payment_intent`, `date_start`, `date_end`, `activated_at` and `state = active` in one write, after every external call succeeded. A failure halfway leaves the pass `pending`; the retry finds the Authentik user by attribute and the Keycast user by `vine_id`, so nothing is duplicated.

**revoke_pass / expire** write the state, then always re-check "does the partner have another active pass" and suspend the Keycast user if not, so a retry after a failed Keycast call completes the suspension. The pubkey comes from the pass, falling back to the partner's most recent pass with one.

**reconcile** looks at posted `out_invoice` records with `payment_state in (paid, in_payment)` and `invoice_date` in the last `RECONCILE_DAYS`, skips those whose pass is active, revoked or expired, and runs `activate_pass` on the rest; then revokes every active pass whose `transaction_id` is the source of a `done` refund transaction.

**Disputes** are matched by `data.object.payment_intent` against `belfry.pass.stripe_payment_intent`; the dispute id is stored in `stripe_dispute_id` and used as a fallback key on `charge.dispute.closed`.

## To verify in the smoke test against the real services

These were coded from the spike notes and API docs, not run against live services:

1. Authentik `POST /api/v3/sources/user_connections/oauth/`: verified 2026-10-22 that authentik 2026.8.5 refuses it (`source` wants the UUID, `user` is read-only), so the link is not pre-created. Open: whether a later authentik release allows it, or switch the source to `username_link` matching once the bridge exists.
2. Authentik `GET /api/v3/core/users/?attributes=<json>` filtering and `?email=` filtering return the expected user.
3. Keycast `PUT /api/admin/users/<pubkey>/status` body shape (`{"status": "active"}` assumed) and that the service token is the right credential. Also whether a suspension takes effect on an already cached HTTP-RPC authorization (spike: unverified).
4. Keycast tenant: the `Host` header the service sends (`KEYCAST_TENANT_DOMAIN`) is accepted by Keycast's tenant extractor, and the admin UCAN was minted for that tenant.
5. Odoo: the exact signatures of `res.partner.belfry_ensure_member_number` and `belfry.pass.belfry_find_or_create_for_invoice` (the service passes `[[partner_id]]` and `[invoice_id]` respectively and ignores the return value, re-reading the records instead). The missing-method detection matches Odoo's `AttributeError` text.
6. Odoo webhook payload: the automation rule's `{"_model", "_id", ...}` body with the token in the query string, and that `payment.transaction.last_state_change` and `invoice_date` are readable by the onboarding user (access rights of the API-key user on `account.move`, `account.move.line`, `payment.transaction`, `product.product`, `res.partner`, `belfry.*`).
7. The smoke script emulates Stripe with a `done` `payment.transaction` carrying a `provider_reference` plus a manual payment on the invoice, which is what `activate_pass` requires; `ALLOW_MANUAL_PAYMENTS` is only for a payment registered by hand without such a transaction.
8. Stripe signature with a real `whsec_` secret via `stripe listen --forward-to localhost:8100/hooks/stripe`.
