from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import BelfryCase


@tagged('post_install', '-at_install', 'belfry')
class TestPass(BelfryCase):

    def test_member_number_sequence(self):
        self.assertFalse(self.alice.belfry_member_number)
        numbers = self.alice.belfry_ensure_member_number()
        self.assertEqual(len(numbers), 1)
        self.assertRegex(numbers[0], r'^OSV-\d{4}$')
        self.assertEqual(self.alice.belfry_ensure_member_number(), numbers, "idempotent")
        self.bob.belfry_ensure_member_number()
        self.assertNotEqual(self.bob.belfry_member_number, self.alice.belfry_member_number)

    def test_find_or_create_for_invoice(self):
        order, invoice = self._make_posted_invoice(self.alice, self.product_week)
        Pass = self.env['belfry.pass']
        pass_id = Pass.belfry_find_or_create_for_invoice(invoice.id)
        belfry_pass = Pass.browse(pass_id)
        self.assertEqual(belfry_pass.partner_id, self.alice)
        self.assertEqual(belfry_pass.tier, 'week')
        self.assertEqual(belfry_pass.product_id, self.product_week.product_variant_id)
        self.assertEqual(belfry_pass.sale_order_id, order)
        self.assertEqual(belfry_pass.state, 'pending')
        self.assertEqual(Pass.belfry_find_or_create_for_invoice(invoice.id), pass_id, "idempotent")

    def test_find_or_create_company_invoice_holder_is_person(self):
        order = self.env['sale.order'].create({
            'partner_id': self.acme.id,
            'partner_invoice_id': self.acme.id,
            'partner_shipping_id': self.bob.id,
            'order_line': [(0, 0, {'product_id': self.product_full.product_variant_id.id, 'product_uom_qty': 1})],
        })
        order.action_confirm()
        invoice = order._create_invoices()
        invoice.action_post()
        belfry_pass = self.env['belfry.pass'].browse(
            self.env['belfry.pass'].belfry_find_or_create_for_invoice(invoice.id)
        )
        self.assertEqual(invoice.partner_id, self.acme)
        self.assertEqual(belfry_pass.partner_id, self.bob)
        self.assertEqual(belfry_pass.tier, 'full')

    def test_find_or_create_rejects_invoice_without_pass(self):
        other = self.env['product.product'].create({'name': "Sticker", 'type': 'service', 'list_price': 5})
        order = self.env['sale.order'].create({
            'partner_id': self.alice.id,
            'order_line': [(0, 0, {'product_id': other.id, 'product_uom_qty': 1})],
        })
        order.action_confirm()
        invoice = order._create_invoices()
        with self.assertRaises(UserError):
            self.env['belfry.pass'].belfry_find_or_create_for_invoice(invoice.id)

    def test_invoice_unique(self):
        _order, invoice = self._make_posted_invoice(self.alice, self.product_day)
        self.env['belfry.pass'].create({'partner_id': self.alice.id, 'tier': 'day', 'invoice_id': invoice.id})
        with self.assertRaises(Exception), self.env.cr.savepoint():
            self.env['belfry.pass'].create({'partner_id': self.alice.id, 'tier': 'day', 'invoice_id': invoice.id})

    def test_activation_assigns_member_number(self):
        belfry_pass = self.env['belfry.pass'].create({'partner_id': self.alice.id, 'tier': 'day'})
        belfry_pass.write({'state': 'active'})
        self.assertTrue(self.alice.belfry_member_number)
