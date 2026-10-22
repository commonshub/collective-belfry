from datetime import timedelta

from odoo import fields
from odoo.tests import TransactionCase


def ensure_chart_of_accounts(env):
    """A database created without a country has no chart of accounts, hence no sale journal
    and no way to post an invoice. Load the generic chart, as Odoo's own account tests do."""
    company = env.company
    if not company.chart_template:
        env['account.chart.template'].try_loading('generic_coa', company, install_demo=False)


class BelfryCase(TransactionCase):
    """Shared fixtures: the three pass products, the two pricelists, a person and a company."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        ensure_chart_of_accounts(env)
        cls.product_day = env.ref('belfry_pass.product_day')
        cls.product_week = env.ref('belfry_pass.product_week')
        cls.product_full = env.ref('belfry_pass.product_full')
        cls.products = cls.product_day + cls.product_week + cls.product_full
        cls.pricelist_public = env.ref('belfry_pass.pricelist_public')
        cls.pricelist_company = env.ref('belfry_pass.pricelist_company')
        cls.steward_user = env['res.users'].create({
            'name': "Steward Sam",
            'login': 'steward@belfry.test',
            'email': 'steward@belfry.test',
            'group_ids': [(6, 0, [env.ref('base.group_user').id, env.ref('belfry_pass.group_steward').id])],
        })
        cls.alice = env['res.partner'].create({
            'name': "Alice Normal",
            'email': 'alice@example.org',
        })
        cls.acme = env['res.partner'].create({
            'name': "ACME Corp",
            'is_company': True,
            'property_product_pricelist': cls.pricelist_company.id,
        })
        cls.bob = env['res.partner'].create({
            'name': "Bob Acme",
            'email': 'bob@acme.example',
            'parent_id': cls.acme.id,
        })
        cls.now = fields.Datetime.now()
        cls.after_early_bird = cls.now + timedelta(days=5 * 365)

    def _prices(self, pricelist, date=None):
        return [
            pricelist._get_product_price(product.product_variant_id, 1.0, date=date or self.now)
            for product in self.products
        ]

    def _make_order(self, partner, product, promo=None):
        order = self.env['sale.order'].create({
            'partner_id': partner.id,
            'order_line': [(0, 0, {'product_id': product.product_variant_id.id, 'product_uom_qty': 1})],
        })
        if promo:
            result = order._try_apply_code(promo)
            self.assertFalse(result.get('error'), result.get('error'))
            for coupon, rewards in result.items():
                order._apply_program_reward(rewards[:1], coupon)
            order._update_programs_and_rewards()
        return order

    def _make_posted_invoice(self, partner, product):
        order = self._make_order(partner, product)
        order.action_confirm()
        invoice = order._create_invoices()
        invoice.action_post()
        return order, invoice

    def _register_payment(self, invoice):
        """Register a manual payment, as Stripe test mode would end up doing."""
        wizard = self.env['account.payment.register'].with_context(
            active_model='account.move', active_ids=invoice.ids,
        ).create({})
        wizard.action_create_payments()
        return invoice
