# belfry_pass (Odoo 19 Community)

The pass registry of Open Source Village and its front door: products and prices, steward invites, the invite and payment pages, the passes themselves and the webhooks that tell the onboarding service (`services/onboarding`) when a pass is paid or refunded.

What it adds:

- **Products**: Day pass (50), Week pass (100), Full access (200) as service products with `belfry_tier` / `belfry_duration_days` (xml ids `belfry_pass.product_day|week|full`). No tax by default; prices are in the company currency, so create the database with a eurozone country.
- **Pricing**: pricelist `Belfry public` (default for everyone) with early-bird fixed prices 40 / 80 / 160 until `belfry.early_bird_until`; pricelist `Belfry companies` = public price +50 %, which compounds with early bird (60 / 120 / 240, later 75 / 150 / 300). The post-init hook turns on the Pricelists feature and creates the early-bird rules (`belfry_pass.early_bird_product_*`), because XML cannot read a config parameter.
- **Discount code** `OSV10`: loyalty program `belfry_pass.promo_example`, 10 % on the three pass products.
- **Invites** (`belfry.invite`): a steward creates a record, the form shows the single-use link `<web.base.url>/belfry/invite/<token>`; 14 days validity by default, states open / used / expired / revoked. Stewards see their own invites, Belfry admins all.
- **Public invite page** `/belfry/invite/<token>` (portal, no website module): shows the three passes with the visitor's prices, takes name, email, optional company name, VAT and discount code, then creates or finds the partner (companies become a company partner with the person as contact, on the company pricelist), the sales order (invoice address = company or person, delivery address = person, salesperson = steward), confirms it, creates and posts the invoice, generates Odoo's payment link (`payment.link.wizard`), creates the pending `belfry.pass`, marks the invite used and redirects (303) to the payment link. Used, expired, revoked or unknown links get a plain error page (410 / 404).
- **Registry**: `belfry.pass`, one row per purchased pass, unique per invoice and per Stripe PaymentIntent: tier, dates, state (pending, active, revoked, expired), the holder's npub and signer key, the one-time sign-in link, the claim token and link, the village tokens (`cashu_token`, admins only; `cashu_amount`, `tokens_url`) and the status list index. `belfry.stripe.event` records the dispute events the onboarding service processed. On `res.partner`: `belfry_member_number`, `belfry_authentik_user_id`, `belfry_npub`, and `belfry_key_version` / `belfry_key_custody` (which derived key the participant has, or whether they hold their own).
- **Webhooks to the onboarding service** (automation rules + "Send Webhook Notification" server actions):
  1. `account.move`, on create/update of `payment_state`, domain `move_type = out_invoice, state = posted, payment_state in (paid, in_payment)` → `POST ${belfry.onboarding_url}/hooks/odoo/invoice-paid?token=${belfry.hook_token}`
  2. `payment.transaction`, on create/update of `state`, domain `operation = refund, state = done` → `POST ${belfry.onboarding_url}/hooks/odoo/refund?token=...`

  Payload, as observed: `{"_action": "Belfry: POST invoice-paid(#360)", "_id": 11, "_model": "account.move", "amount_total": 216.0, "id": 11, "name": "INV/2026/00001", "partner_id": 40, "payment_state": "paid", "state": "posted"}` and `{"_action": "Belfry: POST refund(#361)", "_id": 4, "_model": "payment.transaction", "amount": -216.0, "id": 4, "operation": "refund", "provider_reference": "re_...", "reference": "R-...", "source_transaction_id": 3, "state": "done"}`. The receiver reads `_id`. Odoo sends post-commit with a 1 s timeout and no retry; the service's reconcile loop is the safety net.
- **Activation mail** (`mail_template_pass_activated`): queued by `belfry.pass.write` whenever the onboarding service writes a sign-in link on an active pass (activation, "Send new login link"): pass details, the one-time sign-in link, the claim link, the apps URL. The template is `noupdate="0"`, so **every module upgrade rewrites it from `data/mail_template_data.xml`: edits made in the Odoo UI are lost; change the text in git.** (To keep UI edits instead: set noupdate back on and ship text changes as a migration that writes the body once.)
- **Look**: "Open Source Village, run by Belfry", dark theme. `views/templates.xml` adds `brand_frontend_layout` to `web.frontend_layout`, so the invite page, the payment page and the portal load `fonts.css` and `tokens.css` from `<belfry.public_url>/brand` (the repo's `brand/` folder, served by the onboarding service) and get the palette as Bootstrap overrides; the activation mail is a dark card with the wordmark PNG from the same place. Bootstrap sets the company logo from `brand/osv-logo-horizontal-dark.png` (invoices, portal navbar). The back office keeps Odoo's look.
- **Menu** Belfry > Passes, Invites, Stripe events (group `belfry_pass.group_steward`; Stripe events for `belfry_pass.group_admin`). Settings administrators are Belfry admins.

## Config parameters

| Key | Default | Used for |
| --- | --- | --- |
| `belfry.onboarding_url` | `http://onboarding:8100` | base URL of the onboarding service |
| `belfry.hook_token` | `change-me` | shared secret appended as `?token=` to the webhook URLs |
| `belfry.login_url` | `http://localhost:9000` | Authentik's login page in the activation mail (bootstrap sets it from `AUTHENTIK_PUBLIC_URL`) |
| `belfry.event_start` | `2027-01-25` | read by the onboarding service |
| `belfry.event_end` | `2027-01-31` | read by the onboarding service |
| `belfry.early_bird_until` | `2026-12-31` | end date of the early-bird pricelist rules, read once at install |

The webhook URL of a server action is a stored string. `models/ir_config_parameter.py` rewrites the two Belfry server actions whenever `belfry.onboarding_url` or `belfry.hook_token` is created, written or deleted (also through `set_param`), and the post-init hook does it once at install. `ir.config_parameter.belfry_sync_webhook_urls()` is public and can be called over RPC (`execute_kw ir.config_parameter belfry_sync_webhook_urls []`) if the actions were edited by hand. Changing `belfry.early_bird_until` after install does not move the existing rules; edit them under Sales > Products > Pricelists.

## RPC entry points for the onboarding service

- `res.partner.belfry_ensure_member_number()` on a recordset: assigns `OSV-0001`-style numbers from sequence `belfry.member` where missing and returns the list of numbers. `execute_kw res.partner belfry_ensure_member_number [[partner_id]]`.
- `belfry.pass.belfry_find_or_create_for_invoice(invoice_id)`: returns the pass id for a customer invoice, creating a pending pass from the invoice's pass product line when the invoice was not made through the invite flow. The holder is the delivery address (the person) when the invoice is billed to a company. `execute_kw belfry.pass belfry_find_or_create_for_invoice [invoice_id]`.
- `belfry.invite.belfry_steward_create(steward_email, note)` and `belfry.pass.belfry_steward_mark_paid(steward_email)` on a pending pass: what the steward pages do (create an invite in a steward's name; register the invoice payment by hand and record who did it in `manual_paid_by`).
- Writing a pass to `state = active` assigns the holder's member number if it is still missing.

The service user needs only the group `belfry_pass.group_admin` (plus internal user and contact rights); the module grants that group read access to invoices, invoice lines and payment transactions.

## Security

- `belfry_pass.group_steward`: create and read own invites, read passes and partners' Belfry tab.
- `belfry_pass.group_admin` (implies steward, implied by Settings administrator): everything, including Stripe events.
- The invite controller is public and runs with `sudo()`; CSRF is enforced on the POST through the portal form token.

## Running the tests

With the Belfry compose stack (Odoo on `/mnt/extra-addons`):

```
odoo -d test --db_host=... -i belfry_pass --test-enable --test-tags=/belfry_pass --stop-after-init --without-demo --log-level=test --workers=0
```

(`--without-demo=all` is accepted by Odoo 19 with a warning.) `make test-odoo` runs the suite (43 tests) against the stack. It covers pricing with and without early bird for persons and companies, the OSV10 code, member numbers, `belfry_find_or_create_for_invoice`, the automation rules (existence, domains against a paid invoice and a done refund, URL sync from the parameters) and the invite lifecycle over HTTP (page, prices, CSRF, order/invoice/pass/redirect, company billing, partner reuse, bad code, missing email, second use, expired, revoked). The HTTP tests are `post_install`.

The tests load the generic chart of accounts themselves when the test company has none (a database created by the CLI without a country has no sale journal until Odoo's deferred chart installation runs). A real database should be created with a country so the localised chart and EUR are set.

## Notes

- Odoo 19 has no `product.list0`; default pricelists are created per company. The module ships its own `belfry_pass.pricelist_public` with sequence 1, which makes it the fallback pricelist for partners without one; early-bird rules go there.
- The public routes use `website=True`. That flag belongs to `http_routing` (a dependency of `portal`), which prepares the portal rendering environment (languages, layout); the website module is not required and not installed.
- The payment link host comes from `web.base.url`; set it to the public Odoo URL in the deployment.
- `in_payment` counts as paid for the webhook; the onboarding service decides whether the payment is complete.
