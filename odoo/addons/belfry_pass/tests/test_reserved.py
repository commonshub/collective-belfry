import re
from datetime import timedelta

from odoo import fields
from odoo.tests import HttpCase, tagged

from .common import ensure_chart_of_accounts


@tagged('post_install', '-at_install', 'belfry')
class TestReservedInvite(HttpCase):
    """The invite stays usable until the pass is paid (user test 2026-10-26: a refresh before
    paying showed "already used")."""

    def setUp(self):
        super().setUp()
        ensure_chart_of_accounts(self.env)
        self.invite = self.env['belfry.invite'].create({'note': "for alice"})
        self.product_week = self.env.ref('belfry_pass.product_week')

    def _csrf(self):
        page = self.url_open('/belfry/invite/%s' % self.invite.token)
        return re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)

    def _sign_up(self):
        data = {'csrf_token': self._csrf(), 'product_id': self.product_week.id,
                'name': "Alice Normal", 'email': 'alice@example.org'}
        response = self.url_open('/belfry/invite/%s' % self.invite.token, data=data, allow_redirects=False)
        self.assertEqual(response.status_code, 303, response.text[:500])
        self.invite.invalidate_recordset()
        return self.invite.pass_id

    def test_reopening_offers_payment_and_start_over(self):
        belfry_pass = self._sign_up()
        self.assertEqual(self.invite.state, 'reserved')
        page = self.url_open('/belfry/invite/%s' % self.invite.token)
        self.assertEqual(page.status_code, 200)
        self.assertIn('Alice, you', page.text)
        self.assertNotIn('Normal', page.text, "the link shows the first name only")
        self.assertIn('id="belfry_continue_payment"', page.text)
        self.assertIn('move_id=%s' % belfry_pass.invoice_id.id, page.text)
        self.assertIn('id="belfry_start_over"', page.text)

    def test_start_over_credits_the_invoice_and_reopens(self):
        belfry_pass = self._sign_up()
        invoice, order = belfry_pass.invoice_id, belfry_pass.sale_order_id
        data = {'csrf_token': self._csrf(), 'start_over': '1'}
        response = self.url_open('/belfry/invite/%s' % self.invite.token, data=data, allow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.invite.invalidate_recordset()
        self.assertEqual(self.invite.state, 'open')
        self.assertFalse(self.invite.pass_id)
        self.assertFalse(belfry_pass.exists())
        self.assertEqual(invoice.payment_state, 'reversed')
        self.assertEqual(order.state, 'cancel')
        self.assertIn('id="belfry_invite_form"', self.url_open('/belfry/invite/%s' % self.invite.token).text)

    def test_paid_invite_is_not_released(self):
        belfry_pass = self._sign_up()
        wizard = self.env['account.payment.register'].with_context(
            active_model='account.move', active_ids=belfry_pass.invoice_id.ids).create({})
        wizard.action_create_payments()
        self.assertEqual(self.invite._belfry_payment_state(), 'paid')
        self.assertFalse(self.invite.belfry_start_over())
        page = self.url_open('/belfry/invite/%s' % self.invite.token)
        self.assertIn('Payment received', page.text)
        self.assertNotIn('belfry_start_over', page.text)

    def test_cron_releases_old_unpaid_reservations_only(self):
        belfry_pass = self._sign_up()
        self.env['belfry.invite']._cron_release_unpaid()
        self.assertEqual(self.invite.state, 'reserved', "younger than the reservation period")
        self.env.cr.execute("UPDATE belfry_pass SET create_date = %s WHERE id = %s",
                            (fields.Datetime.now() - timedelta(days=8), belfry_pass.id))
        belfry_pass.invalidate_recordset()
        self.env['belfry.invite']._cron_release_unpaid()
        self.invite.invalidate_recordset()
        self.assertEqual(self.invite.state, 'open')

    def test_activation_uses_the_invite_and_makes_the_invoice(self):
        belfry_pass = self._sign_up()
        belfry_pass.write({'state': 'active', 'login_setup_url': 'https://login.test/x?flow_token=a'})
        self.invite.invalidate_recordset()
        self.assertEqual(self.invite.state, 'used')
        pdf = belfry_pass.invoice_id.invoice_pdf_report_id
        self.assertTrue(pdf, "the invoice's own document, so the portal no longer shows a proforma")
        mail = self.env['mail.mail'].search([('model', '=', 'belfry.pass'), ('res_id', '=', belfry_pass.id)])
        self.assertEqual(len(mail), 1, "one mail per payment: no separate invoice mail")
        self.assertIn(pdf, mail.attachment_ids)
        self.assertIn('Your invoice is attached', mail.body_html)
        # a new sign-in link later goes without the invoice
        belfry_pass.write({'login_setup_url': 'https://login.test/x?flow_token=b'})
        later = self.env['mail.mail'].search([('model', '=', 'belfry.pass'), ('res_id', '=', belfry_pass.id)]) - mail
        self.assertFalse(later.attachment_ids)
        self.assertNotIn('Your invoice is attached', later.body_html)
        self.assertEqual(self.url_open('/belfry/invite/%s' % self.invite.token).status_code, 410)

    def test_organisation_pays_vat_on_top(self):
        _included, excluded_id = self.env['product.template'].belfry_setup_vat(21.0)
        data = {'csrf_token': self._csrf(), 'product_id': self.product_week.id,
                'name': "Bob Acme", 'email': 'bob@acme.example', 'company_name': "ACME Corp",
                'promo_code': 'OSV10'}
        page = self.url_open('/belfry/invite/%s' % self.invite.token)
        self.assertIn('incl. 21% VAT', page.text)
        self.assertIn('+ VAT', page.text)
        response = self.url_open('/belfry/invite/%s' % self.invite.token, data=data, allow_redirects=False)
        self.assertEqual(response.status_code, 303, response.text[:500])
        self.invite.invalidate_recordset()
        invoice = self.invite.pass_id.invoice_id
        company_price = self.env.ref('belfry_pass.pricelist_company')._get_product_price(
            self.product_week.product_variant_id, 1.0)
        self.assertAlmostEqual(invoice.amount_untaxed, round(company_price * 0.9, 2))
        self.assertAlmostEqual(invoice.amount_total, round(company_price * 0.9 * 1.21, 2))
        self.assertEqual(invoice.invoice_line_ids.tax_ids.ids, [excluded_id])
