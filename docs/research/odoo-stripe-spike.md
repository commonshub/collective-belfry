# Odoo + Stripe spike: pass registry and billing (Phase 1)

Date: 2026-10-22. Tested against Odoo **19.0 Community** (`odoo:19` image, server build `19.0-20261015`) with `postgres:16`, modules `sale_management`, `loyalty`, `sale_loyalty`, `account_payment`, `payment_stripe`, `base_automation`, `website_sale` (see section 6). Stripe itself was not called; the Stripe findings come from the `payment_stripe` source and Stripe docs. Odoo version, edition and hosting that Belfry will actually use are **not known yet** (see open questions). Everything below marked "verified" was run in the container; "source" means read from the odoo/odoo 19.0 branch; "unverified" is my understanding only.

## Summary of recommendations

| Question | Recommendation |
| --- | --- |
| 1 Pricing | Standard pricelists cover tiers, date-bounded early bird and the +50% company surcharge. Discount codes come from the `loyalty` module, which is Community. Nothing here needs Enterprise. |
| 2 Invite links | No standard single-use invite link in Sales. Build a small custom module `belfry_invite` (one model, one public controller). If the hosting is Odoo Online (no custom modules), keep invites in Belfry's own service instead. |
| 3 Stripe | Use Odoo's Stripe provider and Odoo's invoice payment link, not Stripe-hosted Payment Links. Notify Belfry's glue with an outgoing automation-rule webhook as fast path plus JSON-RPC polling as the safety net, keyed on the Odoo invoice and the Stripe PaymentIntent id (`payment.transaction.provider_reference`). Belfry gets a Stripe webhook of its own only for disputes. |
| 4 Refunds | Stripe refunds arrive in Odoo as child `payment.transaction` records (`operation = refund`); no credit note is created. Chargebacks never reach Odoo: handle `charge.dispute.*` in Belfry's own Stripe endpoint. |
| 5 Data model | A custom model `belfry.pass` (one record per purchased pass) plus two stable fields on `res.partner`. Processed Stripe event ids go into a small `belfry.stripe.event` table only if Belfry runs its own Stripe endpoint. |
| 6 Local test | Prices, promo code, payment link, outgoing and incoming webhooks all confirmed in Community 19; stack torn down. |

## 1. Pricing: tiers, early bird, company surcharge, discount codes

**Pricelists (verified, Community).** `product.pricelist.item` has `date_start`/`date_end` (datetime), `applied_on` (all products, category, product, variant), `compute_price` in `fixed`, `percentage`, `formula`, and for formulas `base` in `list_price`, `standard_price`, `pricelist` (another pricelist) with `price_discount`, `price_surcharge`, `price_round`, `price_min_margin`, `min_quantity`. A pricelist is assigned per partner (`res.partner.property_product_pricelist`) or chosen by country group (`country_group_ids`); there is **no pricelist per partner category/tag** in standard. The "Pricelists" checkbox (Sales > Configuration > Settings, group `product.group_product_pricelist`) must be on, otherwise partners get no pricelist and list prices apply. Docs: https://www.odoo.com/documentation/19.0/applications/sales/sales/products_prices/prices/pricing.html

Setup tested:

- Three service products: Day pass €50, Week pass €100, Full access €200.
- Pricelist "Belfry public": three fixed-price rules €40/€80/€160 valid from yesterday to +30 days (early bird).
- Pricelist "Belfry company": one global formula rule, base = "Belfry public", discount −50% (a negative discount is a markup).
- Alternative "Belfry company (percentage)": one global `percentage` rule with `percent_price = -50` on the list price; Odoo accepts the negative value.

Computed on quotations (verified):

| Order | Day | Week | Full |
| --- | --- | --- | --- |
| Individual, today (early bird) | 40 | 80 | 160 |
| Individual, order dated +60 days | 50 | 100 | 200 |
| Company, today, formula on public pricelist | 60 | 120 | 240 |
| Company, +60 days | 75 | 150 | 300 |
| Company, percentage −50% on list price | 75 | 150 | 300 |

So the surcharge can either compound with early bird (formula on the public pricelist) or not (percentage on list price). The plan does not say which; decide (open question). The order date (`date_order`) decides which rule applies, so backdated quotations would get early-bird prices; the glue should not set `date_order`. Something must put company contacts on the company pricelist: either the invite/checkout code sets `property_product_pricelist` when `is_company`/"billing as company" is chosen, or an automation rule on `res.partner` (trigger on create, domain `is_company = True`, action update pricelist). The `+50%` rule shows on the quotation as a plain unit price, not as a visible surcharge line.

**Discount codes (verified, Community).** `loyalty` and `sale_loyalty` ship in odoo/odoo (`sale_loyalty` is `auto_install` with `sale`): https://github.com/odoo/odoo/blob/19.0/addons/sale_loyalty/__manifest__.py. Program types: `coupons`, `gift_card`, `loyalty`, `promotion`, `ewallet`, `promo_code` (Discount Code), `buy_x_get_y`, `next_order_coupons` (source: https://github.com/odoo/odoo/blob/19.0/addons/loyalty/models/loyalty_program.py). A program has `date_from`/`date_to`, `limit_usage` + `max_usage`, and `pricelist_ids` to restrict it to a pricelist; the code lives on `loyalty.rule` (`mode = with_code`, `code`), rewards on `loyalty.reward` (`discount` percent / per order / per point, `discount_applicability` order / cheapest / specific products, `discount_max_amount`). Docs: https://www.odoo.com/documentation/19.0/applications/sales/sales/products_prices/loyalty_discount.html

Tested: program `FRIENDS10` (`promo_code`, 10% on order, valid 90 days, max 100 uses). Applied over XML-RPC with `sale.loyalty.coupon.wizard` (`order_id`, `coupon_code`, method `action_apply`); Odoo answered with a `sale.loyalty.reward.wizard` action to pick the reward, which `action_apply` on that wizard completes. Result: a reward line "10% on your order" of −€16 on the €160 early-bird full pass (−€24 on the company price of €240, so the code applies on top of pricelists). Re-applying raises "This promo code is already applied."; an unknown code raises "This code is invalid (NOPE)."; `total_order_count` reached 2 after confirming two orders, so `max_usage` is enforced per confirmed order. The discount appears as a separate negative line on the quotation and invoice, which is what the certificate does not need to know about.

Single-use codes: use the `coupons` program type, which generates individual `loyalty.card` records with their own code, `expiration_date` and optional `partner_id` (source; not exercised). That is the closest standard thing to an invite code (see section 2).

eCommerce: `website_sale_loyalty` (promo code box in the cart) is in odoo/odoo, so it is Community too; see section 6 for the install result.

**What is Enterprise-only here.** Nothing in pricing or discount codes. Enterprise matters elsewhere: Studio (visual editor for automation rules and custom fields; the rules themselves are `base_automation`, Community), Odoo.sh hosting, Subscriptions, Sign, Helpdesk. Hosting matters more than edition: Odoo Online (SaaS) does not run custom Python modules, only Studio customisations (unverified here, commonly documented); Odoo.sh and on-premise do. The Stripe docs page shows "Connect Stripe" onboarding for Odoo Online and manual API keys for Odoo.sh/on-premise: https://www.odoo.com/documentation/19.0/applications/finance/payment_providers/stripe.html

## 2. Invite links

**Standard options reviewed.**

- Quotation sent by a steward (standard `sale`): the steward creates a quotation for the participant (partner must exist, so the email must be known), `user_id` = steward records who invited, `validity_date` expires the quote, the portal link carries an `access_token`, and "Online payment" on the quotation lets the participant pay. Not single-use in the invite sense (the link works until the quote is paid or expired), and the participant cannot pick a tier themselves. Fine for stewards who invite named people.
- Coupon program (`coupons` type): one `loyalty.card` per invite with code and `expiration_date`, redeemable once. But a coupon only unlocks a discount; standard eCommerce cannot require a coupon to check out, so it cannot gate access. Hidden (unpublished) products in `website_sale` are not a gate either: nothing stops a visitor who knows the product id from adding it (unverified; do not rely on it).
- `website_event` registrations: tickets as products with dates, but no invite tokens and the day/week/full tiers map poorly to one event with ticket types. Not evaluated further.

**Recommendation: a small custom module `belfry_invite`** (needs on-premise or Odoo.sh):

- Model `belfry.invite`: `token` (Char, unique, `secrets.token_urlsafe(24)` default), `steward_id` (Many2one `res.users`, default current user, required), `expires_at` (Datetime, default +7 days), `state` (draft / opened / used / expired / cancelled), `allowed_product_ids` (Many2many `product.template`, optional restriction to tiers), `pricelist_id` (optional override, e.g. company), `invitee_email`/`invitee_name` (optional), `partner_id` and `sale_order_id` (set on use), `note`. Access rights: stewards (a `belfry.group_steward` group) create/read their own; the glue's technical user reads all.
- Controller (`auth='public'`): `GET /invite/<token>` validates token, expiry and state, shows the three tiers with pricelist prices and the billing form (individual/company); `POST /invite/<token>/order` creates the partner (sets the company pricelist if "company"), creates the quotation (`user_id` = steward, `origin` = invite token, lines from the chosen tier), marks the invite `opened` with `sale_order_id`, and redirects to the quotation's portal payment page (standard) or to the invoice payment link. The invite becomes `used` when the order is confirmed/paid (a hook on `sale.order.action_confirm` or the glue), so a bot following the link does not burn it; a cron sets `expired`.
- A `Generate invite` server action on `res.users`/menu for stewards and a list view with "who invited whom" are the whole UI. Roughly 300 lines of Python and XML.

If Belfry must run on Odoo Online, put the same model and controller in Belfry's own service and create partner and quotation over JSON-RPC; the steward then uses Belfry's service, not Odoo, to generate links.

## 3. Stripe

### How Odoo's `payment_stripe` works (source, 19.0)

- Webhook endpoint: `POST /payment/stripe/webhook`, public, CSRF off. Odoo creates the Stripe webhook itself when you click "Generate your webhook" on the provider (`action_stripe_create_webhook` posts to `webhook_endpoints` with `url = <base_url>/payment/stripe/webhook`, `enabled_events = HANDLED_WEBHOOK_EVENTS`, `api_version = '2019-05-16'`) and stores the signing secret in `stripe_webhook_secret`. The docstring says it only works for instances with a public URL; the code does not check. https://github.com/odoo/odoo/blob/19.0/addons/payment_stripe/models/payment_provider.py
- Events consumed: `payment_intent.processing`, `payment_intent.amount_capturable_updated`, `payment_intent.succeeded`, `payment_intent.payment_failed`, `payment_intent.canceled`, `setup_intent.succeeded`, `charge.refunded`, `charge.refund.updated`. No `checkout.session.*`, no `invoice.*`, no `charge.dispute.*`. https://github.com/odoo/odoo/blob/19.0/addons/payment_stripe/const.py
- Status mapping: `succeeded` → `done`; `processing`/`pending` → `pending`; `requires_capture` → `authorized`; `canceled` → `cancel`; `requires_payment_method`/`failed` → `error`.
- Transaction lookup: Odoo sets the PaymentIntent `description` to the transaction `reference` (e.g. `INV/2026/00001` or `S00007-1`) and finds the `payment.transaction` by that reference; no Stripe metadata is used. Signature check is HMAC-SHA256 over `t.payload` with a 10-minute age tolerance (Stripe's own libraries use 5). https://github.com/odoo/odoo/blob/19.0/addons/payment_stripe/controllers/main.py
- Idempotency: there is no store of Stripe event ids. The guard is state-based: `_update_state` skips a transaction already in the target state ("Skipped the update of transaction ... as it is already in state ..."), and refunds are deduplicated by refund id in `provider_reference`. Odoo always answers 200 after a signature-valid event, even when processing raised a `ValidationError` (logged), so Stripe does not retry those. https://github.com/odoo/odoo/blob/19.0/addons/payment/models/payment_transaction.py
- Marking paid: `done` resets `is_post_processed`; the cron `_cron_post_process` (also triggered immediately) runs `account_payment`'s `_post_process`, which posts draft invoices in `invoice_ids`, creates an `account.payment` on the provider's journal, posts it and reconciles it with the invoice receivable lines. The invoice's `payment_state` follows from that reconciliation. https://github.com/odoo/odoo/blob/19.0/addons/account_payment/models/payment_transaction.py
- Provider capabilities: `support_refund = partial`, `support_manual_capture = full_only`, tokenization and express checkout supported (verified on the installed provider record).

**`paid` vs `in_payment`.** `account.move.payment_state` values are `not_paid`, `in_payment`, `paid`, `partial`, `reversed`, `blocked`. With an outstanding receipts account on the journal's payment method line the invoice shows `in_payment` until the bank statement is reconciled; with the bank account itself as outstanding account it goes straight to `paid` (docs: https://www.odoo.com/documentation/19.0/applications/finance/accounting/payments.html). Verified: on the test database the "Manual Payment" method line of the Bank journal has no outstanding account and registering a payment moved the invoice directly to `paid`, `amount_residual = 0`. The Stripe provider only gets a journal and payment method line once it is enabled, so its default could not be checked offline (section 6). Either way the glue should treat `payment_state in ('paid', 'in_payment')` as paid; the money is with Stripe at that point, and `in_payment` only means Belfry's accountant has not matched the payout yet.

### Odoo's payment link vs Stripe Payment Links

Two different things share the name:

- **Odoo payment link** (`payment.link.wizard`, Community, verified): from a posted invoice, "Payment link" produces `https://<odoo>/my/invoices/<id>?access_token=…&move_id=…&amount=280.0&payment_token=<hmac>&payment=True#portal_pay`. The customer pays on Odoo's portal with Stripe Elements; `payment_stripe` creates the PaymentIntent and the webhook above closes the loop, the invoice becomes paid in Odoo by itself. The token is an HMAC over partner, amount and currency; it does not expire and is not single-use by itself, but the page refuses payment once the invoice is paid. https://github.com/odoo/odoo/blob/19.0/addons/payment/wizards/payment_link_wizard.py
- **Stripe Payment Links** (`buy.stripe.com`): Stripe-hosted checkout, emits `checkout.session.completed`; Odoo's module does not know about those sessions (no `description` = reference, no `checkout.session.*` handling), so the invoice would never become paid unless Belfry's service registers the payment over JSON-RPC (`account.payment.register`). https://docs.stripe.com/payment-links/post-payment

The plan says "Odoo generates the invoice and a Stripe payment link". Recommendation: use the Odoo payment link on the invoice (or standard online payment of the quotation) with Odoo's Stripe provider. It keeps Odoo the source of truth for "paid", matches Stripe refunds back to the transaction, and needs no payment registration code. Use Stripe Payment Links only if Belfry wants to bypass Odoo's checkout page entirely; then everything in "option c" below becomes mandatory.

Flow detail to decide: standard Odoo online payment pays the **quotation** and (with Settings > Invoicing > "Automatic Invoice") creates and pays the invoice afterwards; the plan's wording pays the **invoice**. Both end with a paid invoice; invoice-first needs the glue (or the invite controller) to confirm the order and create the invoice before showing the link, which the spike did over RPC (`action_confirm`, `sale.advance.payment.inv`, `action_post`, then the link wizard).

### How Belfry's glue learns that an invoice is paid

| Option | Verified | Latency | Idempotency / reliability | Notes |
| --- | --- | --- | --- | --- |
| (a) Automation rule "Send Webhook Notification" (`base_automation` + `ir.actions.server` state `webhook`, Community) | Yes: rule on `account.move`, trigger "On create and edit" watching `payment_state`, domain `move_type = out_invoice and payment_state in (paid, in_payment)`, posted `{"_model": "account.move", "_id": 1, "_action": "…", "name": "INV/2026/00001", "payment_state": "paid", "partner_id": 6, "amount_total": 280.0, "invoice_origin": "S00002", "state": "posted"}` to a local listener right after the payment was registered | Seconds | Sent post-commit, HTTP POST with `timeout=1`, no retry, no signature or auth header (secret only by URL), failures are logged as warnings. One call per matching write, so a second write that keeps `payment_state` can fire again: the receiver must be idempotent. | Source: https://github.com/odoo/odoo/blob/19.0/odoo/addons/base/models/ir_actions.py. Docs (Studio pages, but the feature is Community): https://www.odoo.com/documentation/19.0/applications/studio/automated_actions.html |
| (b) Polling over JSON-RPC/XML-RPC | Yes (all spike calls) | Poll interval | Fully under Belfry's control; query `account.move` with `payment_state in (paid, in_payment)`, `state = posted`, and a marker Belfry sets (e.g. `belfry.pass.state = pending`) | Needs an API key/technical user; Odoo 19 exposes `/xmlrpc/2` and `/jsonrpc` |
| (c) Belfry's own Stripe webhook endpoint (`payment_intent.succeeded` or `checkout.session.completed`) and then JSON-RPC into Odoo | Not run | Seconds | Stripe retries for 3 days, event ids available for an idempotency table, events may arrive out of order and more than once (https://docs.stripe.com/webhooks) | With Odoo's Stripe provider this endpoint must **not** register payments (Odoo's own webhook already does; a second `account.payment` would double-pay). It can only look up `payment.transaction` by `provider_reference = pi_…` and wait for Odoo's `done`. With Stripe Payment Links it is the only path and must register the payment. |

**Recommendation.** (a) as the fast path and (b) as the reconciliation loop (every few minutes and at startup), both calling one handler `activate_pass(invoice_id)` that is idempotent: it loads the invoice and its `transaction_ids`, requires `state = posted`, `payment_state in (paid, in_payment)` and a `payment.transaction` in state `done` with `provider_reference` set, and creates `belfry.pass` (unique constraint on `invoice_id`, storing `provider_reference`) before creating the Authentik account and the Keycast user. This satisfies "access only after the invoice is marked paid" and gives idempotency keyed on something Odoo actually has. The plan's "keyed on the Stripe event id" cannot be met literally with Odoo's Stripe module, because Odoo does not store event ids; the PaymentIntent id (`pi_…`) is the stable Stripe-side key and is unique per payment. Keep a Belfry Stripe endpoint keyed on event ids only for disputes (section 4) or if Payment Links are chosen after all.

Incoming webhooks into Odoo also work in Community (verified): an automation rule with trigger "On webhook" exposes `POST /web/hook/<uuid>` (unauthenticated, secret in the URL, optional call log), `record_getter` picks the record from the JSON payload and the actions run; a POST of `{"partner_id": 6, "note": …}` updated the partner. Useful if the glue wants to push npub/Authentik ids back into Odoo without an API key, but JSON-RPC with an API key is the cleaner write path.

## 4. Refunds and chargebacks

- **Refund issued in Stripe** (Dashboard or API): Stripe sends `charge.refunded`; Odoo finds the source transaction by reference, creates a child `payment.transaction` with `operation = refund`, negative amount, reference `R-<parent reference>`, `provider_reference = re_…`, sets it `done` and post-processes it into an **outbound `account.payment`**. No credit note (`out_refund`) is created; the invoice stays `paid` and the refund payment remains unreconciled with the invoice (source; `account_payment._create_payment` only reconciles lines that are not yet reconciled). `charge.refund.updated` can later flip the refund to failed. Refunds started from Odoo (`action_refund` on the transaction, partial supported) go through the same child transaction.
- **Detection by glue:** automation rule on `payment.transaction` (trigger on create/edit, domain `operation = refund and state = done`, webhook with `source_transaction_id`, `amount`, `provider_reference`) plus the same polling loop (search child transactions of the pass's transaction). The glue then sets `belfry.pass.state = revoked`, flips the status-list bit and removes the npub. If accounting wants a credit note, Belfry creates it (`account.move.reverse` wizard) or the steward does it by hand; decide in Phase 0 refund policy.
- **Chargebacks / disputes:** Odoo subscribes to no `charge.dispute.*` event, so a dispute is invisible to Odoo (and funds are already withdrawn from the Stripe balance when it opens: https://docs.stripe.com/disputes/how-disputes-work). A dispute does not generate `charge.refunded` (unverified). Belfry needs its own Stripe webhook endpoint for `charge.dispute.created` / `charge.dispute.closed` (and ideally `radar.early_fraud_warning.created`), keyed on event id, which looks up the PaymentIntent id → `belfry.pass` and revokes on creation, reinstates on a won dispute. Record the dispute on the pass (`dispute_id`, `dispute_status`) and let the steward decide about the accounting side.

## 5. Data model for the pass

Recommendation: a custom model `belfry.pass`, not fields on `res.partner`, because a person can hold several passes over time (day pass, then upgrade to full access), each with its own dates, invoice, Stripe payment, status-list index and revocation state, and the nightly expiry job and the whitelist derivation are per pass. Keep on `res.partner` only what is per person: `belfry_authentik_user_id` (Char, unique, indexed) and `belfry_npub` (Char, indexed) (and later `belfry_did`). If a participant gets a new Nostr key the partner field changes and the pass keeps its historical value.

`belfry.pass` fields: `partner_id` (required), `tier` (selection day/week/full/advanced), `date_start`, `date_end` (Date; derived from the product and payment date by the glue), `state` (pending / active / expired / revoked), `product_id`, `sale_order_line_id`, `invoice_id` (`account.move`, unique), `transaction_id` (`payment.transaction`), `stripe_payment_intent` (Char = `provider_reference`, unique), `invite_id`, `npub`, `keycast_user_id`, `authentik_user_id` (copied for audit), `credential_status_index` (Integer, unique per status list), `credential_issued_at`, `revoked_at`, `revoke_reason` (refund / chargeback / steward / expiry), `stripe_dispute_id`. One SQL unique on `invoice_id` and one on `stripe_payment_intent` make the activation handler idempotent at the database level. Processed Stripe event ids: table `belfry.stripe.event` (`event_id` unique, `type`, `received_at`, `pass_id`, `result`) only for Belfry's own Stripe endpoint; Odoo's own webhook does not need it.

If the hosting is Odoo Online, the equivalent is Studio custom fields (`x_` prefix, Enterprise) on `res.partner` and a Studio custom model, or the `belfry.pass` table lives in Belfry's service database with Odoo ids as foreign keys. The second is cleaner than scattering `x_` fields.

## 6. Local test log

Stack: `docker compose` project `belfry-odoo` with `odoo:19` (`--workers=0`, memory limit 2 GB) and `postgres:16`, port 8069, in `/tmp/claude-1000/-home-friedger--repos-github-commonshub-collective-belfry/25a6ddf2-0cae-48ea-9d4d-b384572f5352/scratchpad/spikes/odoo` (`docker-compose.yml`, `spike.py`, `spike2.py`, outputs `spike-output.txt`, `spike2-output.txt`). Database `belfry` created over XML-RPC (`db.create_database`, country BE), modules installed with `ir.module.module.button_immediate_install` in 22 s; Odoo used about 170 MB RAM before `website_sale`.

Steps and results (all over XML-RPC with the admin user):

1. Enabled "Pricelists" and "Discounts" settings via `res.config.settings`. Without the pricelist group, partners had no pricelist and list prices applied.
2. Created the three products and the two pricelists; prices as in the table in section 1. Odoo accepted a negative `percent_price`.
3. Created the `FRIENDS10` promo program and applied it through the coupon and reward wizards; re-apply and invalid code rejected; usage count enforced.
4. Confirmed an order, created and posted invoice `INV/2026/00001` (€280), generated the Odoo payment link (`/my/invoices/1?access_token=…&payment_token=…&payment=True#portal_pay`).
5. Created the automation rule with a "Send Webhook Notification" action, registered a manual payment on the invoice: `payment_state` went to `paid` and the listener received the JSON payload (section 3).
6. Created an "On webhook" rule and POSTed to `/web/hook/<uuid>`: `{"status": "ok"}` and the partner was updated.
7. `website_sale` installed in 9 s (Odoo at ~217 MB RAM) and pulled in `website_sale_loyalty` automatically; the cart templates `website_sale_loyalty.reduction_coupon_code` / `modify_code_form` (the promo code box) exist and `loyalty.program` gained `website_id` and `ecommerce_ok`. So discount codes at eCommerce checkout are Community. The Stripe provider's journal and payment method line are only created when the provider is enabled with credentials, so the outstanding-account default for Stripe payments (`paid` vs `in_payment`) could not be checked offline.
8. Teardown: `docker compose down -v`.

Gotchas for whoever repeats this: Odoo's XML-RPC marshaller rejects `None` in responses, so call `fields_get` with `attributes=[...]`; `_try_apply_code` and other underscore methods are not callable over RPC, use the wizards; Stripe's webhook cannot be exercised without a public URL, use `stripe listen --forward-to localhost:8069/payment/stripe/webhook` with the Stripe CLI.

## Contradictions with the plan and things to change

1. "Stripe payment links": with Odoo's Stripe provider the link should be Odoo's invoice/quotation payment link, not a Stripe-hosted Payment Link. If Stripe Payment Links are wanted, Belfry must register payments in Odoo itself and the idempotency story moves to Belfry's endpoint.
2. "Idempotent webhook keyed on the Stripe event": Odoo's module consumes the Stripe events and keeps no event ids. Key Belfry's activation on the Odoo invoice id and the PaymentIntent id instead; keep event-id keying for Belfry's own dispute endpoint.
3. "Odoo generates the invoice and a payment link" vs Odoo's standard quotation-first online payment: both work; pick one so the invite controller and the glue agree on when the invoice exists.
4. "Refund or chargeback: Odoo revokes the pass": Odoo sees refunds (as refund transactions, not credit notes) but never sees chargebacks; the revocation logic lives in Belfry's glue, fed by Odoo for refunds and by Stripe directly for disputes.
5. Early-bird and company surcharge are easy, but whether +50% compounds with early bird is undecided; it changes the pricelist design (formula on public pricelist vs percentage on list price).
6. Invite links need a custom module (or live in Belfry's service); the plan lists them as if standard.

## Open questions for the user

- Which Odoo version (18 or 19), edition (Community or Enterprise) and hosting (Odoo Online, Odoo.sh, on-premise/Docker)? Custom modules decide between `belfry_invite`/`belfry.pass` in Odoo and keeping both in Belfry's service.
- Is there an existing Stripe account, and may Odoo own the Stripe webhook (one endpoint `/payment/stripe/webhook`) while Belfry adds a second endpoint for disputes?
- Invoice-first (plan wording) or quotation-first with automatic invoicing (Odoo default)?
- Does the +50% company surcharge compound with early-bird prices? Is the surcharge a separate visible line (then a surcharge product line, not a pricelist) or just a higher price?
- VAT on passes (B2C vs B2B reverse charge for companies) and whether Belfry's accountant needs credit notes for refunds.
- Invite semantics: stewards invite named people (quotation flow suffices) or hand out anonymous links (custom module)? How long is an invite valid, and should the tier be fixed by the steward?
- Is `in_payment` acceptable as "paid" for access, or must the journal be configured so Stripe payments land directly on `paid`?
- Who handles disputes and early fraud warnings operationally, and should a dispute revoke immediately or after a steward confirms?

## Sources

- Odoo docs 19.0: pricelists https://www.odoo.com/documentation/19.0/applications/sales/sales/products_prices/prices/pricing.html, discount and loyalty https://www.odoo.com/documentation/19.0/applications/sales/sales/products_prices/loyalty_discount.html, Stripe provider https://www.odoo.com/documentation/19.0/applications/finance/payment_providers/stripe.html, payment providers https://www.odoo.com/documentation/19.0/applications/finance/payment_providers.html, payments and `in_payment` https://www.odoo.com/documentation/19.0/applications/finance/accounting/payments.html, automation rules https://www.odoo.com/documentation/19.0/applications/studio/automated_actions.html and webhooks https://www.odoo.com/documentation/19.0/applications/studio/automated_actions/webhooks.html
- odoo/odoo 19.0 source: `addons/payment_stripe/const.py`, `controllers/main.py`, `models/payment_provider.py`, `models/payment_transaction.py`; `addons/payment/models/payment_transaction.py`; `addons/payment/wizards/payment_link_wizard.py`; `addons/account_payment/models/payment_transaction.py`; `addons/base_automation/models/base_automation.py`; `odoo/addons/base/models/ir_actions.py`; `addons/loyalty/models/loyalty_program.py`; `addons/sale_loyalty/models/sale_order.py`; `addons/sale_loyalty/__manifest__.py` (all under https://github.com/odoo/odoo/tree/19.0)
- Stripe docs: webhooks https://docs.stripe.com/webhooks, payment links post-payment https://docs.stripe.com/payment-links/post-payment, disputes https://docs.stripe.com/disputes/how-disputes-work
