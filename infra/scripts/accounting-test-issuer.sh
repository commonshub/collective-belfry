#!/usr/bin/env bash
# Make this Belfry's Odoo stand in for the organisation that sells the tickets, so another
# Belfry (or this one) can book its tickets here (onboarding's accounting.py) before the real
# seller's Odoo is connected. Creates, if missing: an accounting user with an API key, a ticket
# product that is not a pass (so this Belfry never activates anything for it), a "Stripe" bank
# journal; and prints the ACCOUNTING_* lines for the other Belfry's infra/.env.
#
#   infra/scripts/accounting-test-issuer.sh [public Odoo URL]            # on this Belfry's server
#
# stdout: the .env lines, then ACCOUNTING_ODOO_API_KEY=<key> as the last line. The key is new on
# every run (the previous one is removed); store it on the other Belfry without showing it:
#   ssh A 'cd belfry && infra/scripts/accounting-test-issuer.sh https://shop.A' | tail -n1 \
#     | cut -d= -f2- | infra/scripts/put-secret.sh B accounting_odoo_api_key
# The other Belfry's server must reach this Odoo's /json/2 (ADMIN_NETS here lists its address).
set -euo pipefail
INFRA_DIR="$(cd "$(dirname "$0")/.." && pwd)"
URL=${1:-http://odoo:8069}
COMPOSE=(docker compose -f "$INFRA_DIR/docker-compose.yml")  # exec only: the base file will do

out=$("${COMPOSE[@]}" exec -T odoo /belfry/with-secrets odoo shell -d odoo --no-http 2>/dev/null <<'PY'
from odoo import Command
login = 'accounting@belfry.local'
Users = env['res.users'].sudo()
user = Users.with_context(active_test=False).search([('login', '=', login)], limit=1)
groups = [env.ref('base.group_user').id, env.ref('account.group_account_manager').id,
          env.ref('base.group_partner_manager').id]
if not user:
    user = Users.create({'name': "Accounting (ticket seller)", 'login': login,
                         'email': env.company.email or 'accounting@belfry.local',
                         'group_ids': [Command.set(groups)]})
else:
    user.write({'active': True, 'group_ids': [Command.link(g) for g in groups]})
Keys = env['res.users.apikeys'].sudo()
Keys.search([('user_id', '=', user.id), ('name', '=', 'ticket bookings')])._remove()
key = Keys.with_user(user.id).sudo()._generate(None, 'ticket bookings', None)

Product = env['product.product'].sudo()
product = Product.search([('default_code', '=', 'TICKET-BOOKING')], limit=1)
if not product:
    product = Product.create({'name': "Ticket (booked from Belfry)", 'default_code': 'TICKET-BOOKING',
                              'type': 'service', 'sale_ok': True, 'taxes_id': [Command.clear()]})
Journal = env['account.journal'].sudo()
journal = Journal.search([('code', '=', 'STRP'), ('company_id', '=', env.company.id)], limit=1)
if not journal:
    journal = Journal.create({'name': "Stripe", 'code': 'STRP', 'type': 'bank'})
taxes = env['account.tax'].sudo().search([('type_tax_use', '=', 'sale'), ('amount', '=', 21.0),
                                          ('amount_type', '=', 'percent')])
included = taxes.filtered(lambda t: t.price_include)[:1]
excluded = taxes.filtered(lambda t: not t.price_include)[:1]
env.cr.commit()
print('ACCOUNTING_PRODUCTS=*=%d' % product.id)
print('ACCOUNTING_TAX_INCLUDED=%s' % (included.id or ''))
print('ACCOUNTING_TAX_EXCLUDED=%s' % (excluded.id or ''))
print('ACCOUNTING_JOURNAL=%d' % journal.id)
print('APIKEY=' + key)
PY
)
key=$(sed -n 's/^APIKEY=//p' <<<"$out" | tail -n1)
[[ -n "$key" ]] || { echo "$out" >&2; echo "odoo shell did not return an API key" >&2; exit 1; }
echo "ACCOUNTING_ODOO_URL=$URL"
echo "ACCOUNTING_ODOO_DB=odoo"
grep -E '^ACCOUNTING_(PRODUCTS|TAX_INCLUDED|TAX_EXCLUDED|JOURNAL)=' <<<"$out"
echo "ACCOUNTING_ODOO_API_KEY=$key"
