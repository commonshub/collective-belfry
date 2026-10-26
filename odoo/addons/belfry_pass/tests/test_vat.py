from odoo.tests import tagged

from .common import BelfryCase


@tagged('post_install', '-at_install', 'belfry')
class TestVat(BelfryCase):
    """21% VAT: included for individuals, on top of the company price for organisations."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.included_id, cls.excluded_id = cls.env['product.template'].belfry_setup_vat(21.0)

    def test_setup_is_idempotent_and_sets_the_products(self):
        self.assertEqual(self.env['product.template'].belfry_setup_vat(21.0), [self.included_id, self.excluded_id])
        included = self.env['account.tax'].browse(self.included_id)
        excluded = self.env['account.tax'].browse(self.excluded_id)
        self.assertTrue(included.price_include)
        self.assertFalse(excluded.price_include)
        self.assertEqual((included.amount, excluded.amount), (21.0, 21.0))
        for product in self.products:
            self.assertEqual(product.taxes_id, included)

    def test_individual_price_includes_vat(self):
        _order, invoice = self._make_posted_invoice(self.alice, self.product_full)
        price = self.pricelist_public._get_product_price(self.product_full.product_variant_id, 1.0)
        self.assertAlmostEqual(invoice.amount_total, price)
        self.assertAlmostEqual(invoice.amount_tax, round(price - price / 1.21, 2))
