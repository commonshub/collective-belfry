import requests

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

    # -- Phase 2: login setup link mail --------------------------------------------------------

    def _login_mails(self, belfry_pass):
        return self.env['mail.mail'].search([('model', '=', 'belfry.pass'), ('res_id', '=', belfry_pass.id)])

    def test_login_mail_when_link_written_on_active_pass(self):
        belfry_pass = self.env['belfry.pass'].create({'partner_id': self.alice.id, 'tier': 'week'})
        # a link on a pending pass is not mailed
        belfry_pass.write({'login_setup_url': 'https://login.test/if/flow/belfry-account-setup/?flow_token=a'})
        self.assertFalse(self._login_mails(belfry_pass))
        # activation writes the link together with the state: one mail with the link
        belfry_pass.write({
            'state': 'active',
            'date_start': '2027-01-25',
            'date_end': '2027-02-01',
            'npub': 'npub1example',
            'login_setup_url': 'https://login.test/if/flow/belfry-account-setup/?flow_token=b',
            'login_setup_expires_at': '2026-11-22 10:00:00',
        })
        mails = self._login_mails(belfry_pass)
        self.assertEqual(len(mails), 1)
        self.assertIn('flow_token=b', mails.body_html)
        self.assertNotIn('flow_token=a', mails.body_html)
        self.assertEqual(mails.recipient_ids, self.alice)
        self.assertIn(self.alice.belfry_member_number, mails.subject)
        self.assertIn('Week pass', mails.body_html)
        self.assertIn('npub1example', mails.body_html)
        self.assertIn(belfry_pass.belfry_apps_url(), mails.body_html)
        # passwordless: the mail offers a sign-in, never a password
        self.assertIn('Sign in to Open Source Village', mails.body_html)
        self.assertIn(belfry_pass.belfry_login_url(), mails.body_html)
        self.assertNotIn('password', mails.body_html.lower().replace('without a password', ''))
        self.assertTrue(belfry_pass.login_mail_sent_at)
        # other writes do not resend; a new link does
        belfry_pass.write({'date_end': '2027-02-02'})
        self.assertEqual(len(self._login_mails(belfry_pass)), 1)
        belfry_pass.write({'login_setup_url': 'https://login.test/if/flow/belfry-account-setup/?flow_token=c'})
        mails = self._login_mails(belfry_pass)
        self.assertEqual(len(mails), 2)
        self.assertTrue(any('flow_token=c' in m.body_html for m in mails))

    def test_login_mail_skipped_without_email(self):
        nobody = self.env['res.partner'].create({'name': "No Mail"})
        belfry_pass = self.env['belfry.pass'].create({'partner_id': nobody.id, 'tier': 'day'})
        belfry_pass.write({'state': 'active', 'login_setup_url': 'https://login.test/?flow_token=x'})
        self.assertFalse(self._login_mails(belfry_pass))
        self.assertFalse(belfry_pass.login_mail_sent_at)

    def test_send_login_link_button_calls_the_onboarding_service(self):
        from unittest.mock import MagicMock, patch

        Param = self.env['ir.config_parameter'].sudo()
        Param.set_param('belfry.onboarding_url', 'http://glue.test:8100')
        Param.set_param('belfry.hook_token', 'secret-1')
        belfry_pass = self.env['belfry.pass'].create({'partner_id': self.alice.id, 'tier': 'day'})
        with self.assertRaises(UserError):
            belfry_pass.action_send_login_link()  # pending
        belfry_pass.write({'state': 'active'})

        response = MagicMock()
        response.json.return_value = {'result': 'sent', 'pass_id': belfry_pass.id}
        with patch('odoo.addons.belfry_pass.models.belfry_pass.requests.post', return_value=response) as post:
            action = belfry_pass.action_send_login_link()
        post.assert_called_once_with(
            'http://glue.test:8100/admin/passes/%d/login-link?token=secret-1' % belfry_pass.id, timeout=20,
        )
        self.assertEqual(action['params']['type'], 'success')

        response.json.return_value = {'result': 'not_active'}
        with patch('odoo.addons.belfry_pass.models.belfry_pass.requests.post', return_value=response):
            with self.assertRaises(UserError):
                belfry_pass.action_send_login_link()

        with patch('odoo.addons.belfry_pass.models.belfry_pass.requests.post',
                   side_effect=requests.ConnectionError("down")):
            with self.assertRaises(UserError):
                belfry_pass.action_send_login_link()

    # -- Phase 3: claim link and revoke button -------------------------------------------------

    def test_claim_url_from_public_url(self):
        self.env['ir.config_parameter'].sudo().set_param('belfry.public_url', 'https://belfry.test/')
        belfry_pass = self.env['belfry.pass'].create({'partner_id': self.alice.id, 'tier': 'day'})
        self.assertFalse(belfry_pass.claim_url)
        belfry_pass.write({
            'state': 'active', 'claim_token': 'tok-abc', 'credential_status_index': belfry_pass.id,
            'login_setup_url': 'https://login.test/?flow_token=z',
        })
        self.assertEqual(belfry_pass.claim_url, 'https://belfry.test/claim/tok-abc')
        mails = self._login_mails(belfry_pass)
        self.assertEqual(len(mails), 1)
        self.assertIn('https://belfry.test/claim/tok-abc', mails.body_html)
        self.assertIn('Add the pass to your wallet', mails.body_html)

    def test_revoke_button_calls_the_onboarding_service(self):
        from unittest.mock import MagicMock, patch

        Param = self.env['ir.config_parameter'].sudo()
        Param.set_param('belfry.onboarding_url', 'http://glue.test:8100')
        Param.set_param('belfry.hook_token', 'secret-1')
        belfry_pass = self.env['belfry.pass'].create({'partner_id': self.alice.id, 'tier': 'day'})
        with self.assertRaises(UserError):
            belfry_pass.action_revoke()
        belfry_pass.write({'state': 'active'})
        response = MagicMock()
        response.json.return_value = {'result': 'revoked', 'pass_id': belfry_pass.id}
        with patch('odoo.addons.belfry_pass.models.belfry_pass.requests.post', return_value=response) as post:
            belfry_pass.action_revoke()
        post.assert_called_once_with(
            'http://glue.test:8100/admin/passes/%d/revoke?token=secret-1' % belfry_pass.id, timeout=20,
        )
