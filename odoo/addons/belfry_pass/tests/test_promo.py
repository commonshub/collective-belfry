from odoo.tests import tagged

from .common import BelfryCase


@tagged('post_install', '-at_install', 'belfry')
class TestPromoCode(BelfryCase):

    def test_program_shape(self):
        program = self.env.ref('belfry_pass.promo_example')
        self.assertEqual(program.program_type, 'promo_code')
        self.assertEqual(program.trigger, 'with_code')
        self.assertEqual(program.rule_ids.mapped('code'), ['OSV10'])
        reward = program.reward_ids
        self.assertEqual(reward.reward_type, 'discount')
        self.assertEqual((reward.discount, reward.discount_mode, reward.discount_applicability), (10.0, 'percent', 'specific'))
        self.assertEqual(reward.discount_product_ids, self.products.product_variant_id)

    def test_osv10_gives_ten_percent(self):
        order = self._make_order(self.alice, self.product_full, promo='OSV10')
        reward_lines = order.order_line.filtered('is_reward_line')
        self.assertEqual(len(reward_lines), 1)
        self.assertAlmostEqual(reward_lines.price_subtotal, -16.0)
        self.assertAlmostEqual(order.amount_total, 144.0)

    def test_osv10_on_company_price(self):
        order = self._make_order(self.acme, self.product_full, promo='OSV10')
        self.assertAlmostEqual(order.amount_total, 216.0)

    def test_bad_code_rejected(self):
        order = self._make_order(self.alice, self.product_day)
        self.assertIn('error', order._try_apply_code('NOPE'))

    def test_second_apply_rejected(self):
        order = self._make_order(self.alice, self.product_day, promo='OSV10')
        self.assertIn('error', order._try_apply_code('OSV10'))
