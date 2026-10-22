from odoo.tests import tagged

from .common import BelfryCase


@tagged('post_install', '-at_install', 'belfry')
class TestPricing(BelfryCase):

    def test_pricelists_are_installed(self):
        self.assertTrue(self.env['res.groups']._is_feature_enabled('product.group_product_pricelist'))
        for xmlid in ('early_bird_product_day', 'early_bird_product_week', 'early_bird_product_full'):
            item = self.env.ref('belfry_pass.%s' % xmlid)
            self.assertEqual(item.pricelist_id, self.pricelist_public)
            self.assertEqual(item.compute_price, 'fixed')
            self.assertTrue(item.date_end)

    def test_default_pricelist_is_public(self):
        self.assertEqual(self.alice.property_product_pricelist, self.pricelist_public)

    def test_normal_partner_prices(self):
        self.assertEqual(self._prices(self.pricelist_public), [40.0, 80.0, 160.0], "early bird")
        self.assertEqual(self._prices(self.pricelist_public, self.after_early_bird), [50.0, 100.0, 200.0], "after early bird")

    def test_company_prices_compound_with_early_bird(self):
        self.assertEqual(self.acme.property_product_pricelist, self.pricelist_company)
        self.assertEqual(self._prices(self.pricelist_company), [60.0, 120.0, 240.0], "early bird +50%")
        self.assertEqual(self._prices(self.pricelist_company, self.after_early_bird), [75.0, 150.0, 300.0], "+50%")

    def test_order_uses_partner_pricelist(self):
        order = self._make_order(self.alice, self.product_full)
        self.assertEqual(order.pricelist_id, self.pricelist_public)
        self.assertEqual(order.amount_total, 160.0)
        order = self._make_order(self.acme, self.product_full)
        self.assertEqual(order.pricelist_id, self.pricelist_company)
        self.assertEqual(order.amount_total, 240.0)
