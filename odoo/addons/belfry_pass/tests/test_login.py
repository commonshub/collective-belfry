import re

from odoo import fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from .common import BelfryCase


@tagged('post_install', '-at_install', 'belfry')
class TestLogin(BelfryCase):
    """Sign-in by e-mail link and code (models/belfry_login.py) and stewards as contacts, called
    by the onboarding service (pass.<domain>/login, add-steward)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.service_user = cls.env['res.users'].create({
            'name': "Onboarding service",
            'login': 'service-login@belfry.test',
            'group_ids': [(6, 0, [cls.env.ref('base.group_user').id, cls.env.ref('belfry_pass.group_admin').id])],
        })
        cls.env['ir.config_parameter'].sudo().set_param('belfry.public_url', 'https://pass.test')
        cls.env['belfry.pass'].create({'partner_id': cls.alice.id, 'tier': 'week', 'state': 'active'})
        cls.Code = cls.env['belfry.login.code'].with_user(cls.service_user)
        cls.Partner = cls.env['res.partner'].with_user(cls.service_user)

    def _mail(self, email):
        mails = self.env['mail.mail'].sudo().search([('model', '=', 'belfry.login.code')], order='id desc')
        mail = mails.filtered(lambda m: email in (m.email_to or '') or email in m.recipient_ids.mapped('email'))[:1]
        self.assertTrue(mail, "a sign-in mail went to %s" % email)
        code = re.search(r'(\d{6}) is your', mail.subject).group(1)
        secret = re.search(r'https://pass\.test/login/link/([\w-]+)', mail.body_html).group(1)
        return code, secret

    def test_the_code_signs_in_once(self):
        self.assertEqual(self.Code.belfry_login_send('Alice@Example.org', 'https://chat.test/')['state'], 'sent')
        code, _secret = self._mail('Alice@Example.org')
        wrong = '000000' if code != '000000' else '111111'
        self.assertEqual(self.Code.belfry_login_verify('alice@example.org', wrong),
                         {'state': 'wrong_code', 'tries_left': 4})
        ok = self.Code.belfry_login_verify('alice@example.org', code[:3] + ' ' + code[3:])
        self.assertEqual(ok, {'state': 'ok', 'partner_id': self.alice.id, 'next_url': 'https://chat.test/'})
        self.assertEqual(self.Code.belfry_login_verify('alice@example.org', code)['state'], 'expired')

    def test_the_link_signs_in_once_and_only_the_newest_works(self):
        self.Code.belfry_login_send('alice@example.org')
        _code, first = self._mail('alice@example.org')
        self.Code.belfry_login_send('alice@example.org')
        _code, second = self._mail('alice@example.org')
        self.assertNotEqual(first, second)
        self.assertEqual(self.Code.belfry_login_use_link(first)['state'], 'expired')
        self.assertEqual(self.Code.belfry_login_use_link(second)['state'], 'ok')
        self.assertEqual(self.Code.belfry_login_use_link(second)['state'], 'used')
        self.assertEqual(self.Code.belfry_login_use_link('x' * 43)['state'], 'not_found')
        self.assertFalse(self.env['belfry.login.code'].search([]).filtered(lambda r: second in (r.link_hash or '')),
                         "only the hash of the link's secret is stored")

    def test_unknown_addresses_get_the_same_answer_and_no_mail(self):
        before = self.env['mail.mail'].sudo().search_count([('model', '=', 'belfry.login.code')])
        self.assertEqual(self.Code.belfry_login_send('nobody@example.org')['state'], 'sent')
        self.assertEqual(self.env['mail.mail'].sudo().search_count([('model', '=', 'belfry.login.code')]), before)
        with self.assertRaises(UserError):
            self.Code.belfry_login_send('not an address')

    def test_too_many_mails(self):
        for _ in range(5):
            self.Code.belfry_login_send('alice@example.org')
        self.assertEqual(self.Code.belfry_login_send('alice@example.org')['state'], 'too_many')

    def test_a_closed_pass_no_longer_signs_in(self):
        self.Code.belfry_login_send('alice@example.org')
        code, _secret = self._mail('alice@example.org')
        self.env['belfry.pass'].search([('partner_id', '=', self.alice.id)]).state = 'revoked'
        self.assertEqual(self.Code.belfry_login_verify('alice@example.org', code)['state'], 'not_active')
        self.assertFalse(self.Code.belfry_may_sign_in(self.alice.id))

    def test_stewards_sign_in_without_a_pass_and_get_the_mails(self):
        made = self.Partner.belfry_make_steward('Stew@Example.org', 'Stew')
        self.assertTrue(made['created'])
        again = self.Partner.belfry_make_steward('stew@example.org')
        self.assertEqual((again['partner_id'], again['created']), (made['partner_id'], False))
        self.assertIn('Stew@Example.org', self.Partner.belfry_steward_emails())
        self.assertTrue(self.Code.belfry_may_sign_in(made['partner_id']))
        self.Code.belfry_login_send('stew@example.org')
        code, _secret = self._mail('stew@example.org')
        self.assertEqual(self.Code.belfry_login_verify('stew@example.org', code)['partner_id'], made['partner_id'])

    def test_only_the_service_calls_these(self):
        with self.assertRaises(AccessError):
            self.env['belfry.login.code'].with_user(self.steward_user).belfry_login_send('alice@example.org')
        with self.assertRaises(AccessError):
            self.env['res.partner'].with_user(self.steward_user).belfry_make_steward('x@example.org')

    def test_the_welcome_secret_names_the_partner(self):
        belfry_pass = self.env['belfry.pass'].search([('partner_id', '=', self.alice.id)], limit=1)
        belfry_pass.activated_at = fields.Datetime.now()
        used = self.env['belfry.pass'].with_user(self.service_user).belfry_use_signin('welcome', belfry_pass.welcome_token)
        self.assertEqual(used['result'], 'ok')
        self.assertEqual(used['partner_id'], self.alice.id)
