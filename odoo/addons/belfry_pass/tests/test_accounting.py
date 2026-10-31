from odoo.exceptions import AccessError
from odoo.tests import tagged

from .common import BelfryCase


@tagged('post_install', '-at_install', 'belfry')
class TestAccounting(BelfryCase):
    """Another organisation invoices the tickets: the export the onboarding service books there,
    and the activation mail without Belfry's own invoice."""

    def setUp(self):
        super().setUp()
        self.service = self.env['res.users'].create({
            'name': "Onboarding",
            'login': 'onboarding@belfry.test',
            'group_ids': [(6, 0, [self.env.ref('base.group_user').id,
                                  self.env.ref('belfry_pass.group_admin').id])],
        })

    def _paid_pass(self, partner):
        order, invoice = self._make_posted_invoice(partner, self.product_week)
        self._register_payment(invoice)
        return self.env['belfry.pass'].create({
            'partner_id': partner.id,
            'tier': 'week',
            'product_id': self.product_week.product_variant_id.id,
            'sale_order_id': order.id,
            'invoice_id': invoice.id,
            'manual_paid_by': "steward pass (test)",
        })

    def test_export_for_the_issuer(self):
        belfry_pass = self._paid_pass(self.alice)
        data = belfry_pass.with_user(self.service).belfry_accounting_export()
        invoice = belfry_pass.invoice_id
        self.assertEqual(data['invoice']['name'], invoice.name)
        self.assertEqual(data['invoice']['amount_total'], invoice.amount_total)
        self.assertEqual(data['partner']['email'], 'alice@example.org')
        self.assertEqual(len(data['lines']), 1)
        line = data['lines'][0]
        self.assertEqual(line['tier'], 'week')
        self.assertAlmostEqual(line['price_total'], invoice.amount_total)
        self.assertIn(line['tax'], ('included', 'excluded', 'none'))
        self.assertTrue(data['payment']['date'])
        self.assertEqual(data['payment']['provider'], 'manual')
        self.assertEqual(data['refunded'], 0.0)

    def test_export_is_for_the_service_only(self):
        belfry_pass = self._paid_pass(self.alice)
        with self.assertRaises(AccessError):
            belfry_pass.with_user(self.steward_user).belfry_accounting_export()

    def test_activation_mail_leaves_the_invoice_to_the_issuer(self):
        self.env['ir.config_parameter'].sudo().set_param('belfry.invoice_issuer', "XL Collective")
        belfry_pass = self._paid_pass(self.alice)
        belfry_pass.write({'state': 'active', 'login_setup_url': 'https://login.test/x?flow_token=a'})
        mail = self.env['mail.mail'].search([('model', '=', 'belfry.pass'), ('res_id', '=', belfry_pass.id)])
        self.assertEqual(len(mail), 1)
        self.assertFalse(mail.attachment_ids, "Belfry's invoice stays internal")
        self.assertIn('Your invoice comes from XL Collective in a separate e-mail', mail.body_html)
        self.assertNotIn('Your invoice is attached', mail.body_html)

    def test_internal_invoices_get_an_order_number_with_an_issuer(self):
        Pass = self.env['belfry.pass']
        self.env['ir.config_parameter'].sudo().set_param('belfry.invoice_issuer', False)
        own = Pass._belfry_invoice_order(self._confirmed_order())
        self.assertNotEqual(own.journal_id.code, 'ORD', "without an issuer: the default journal")
        self.env['ir.config_parameter'].sudo().set_param('belfry.invoice_issuer', "XL Collective")
        internal = Pass._belfry_invoice_order(self._confirmed_order())
        self.assertEqual(internal.journal_id.code, 'ORD')
        self.assertEqual(internal.state, 'posted')
        self.assertTrue(internal.name.startswith('ORD/'), internal.name)
        again = Pass._belfry_invoice_order(self._confirmed_order())
        self.assertEqual(again.journal_id, internal.journal_id, "one order journal")

    def _confirmed_order(self):
        order = self._make_order(self.alice, self.product_week)
        order.action_confirm()
        return order
