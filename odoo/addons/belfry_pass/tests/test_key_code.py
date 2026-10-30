import re

from odoo import fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from ..models.nostr_proof import npub
from .common import BelfryCase

KEY = 'a' * 64
OTHER = 'b' * 64


@tagged('post_install', '-at_install', 'belfry')
class TestKeyCode(BelfryCase):
    """E-mail codes for an app's own key (models/belfry_key_code.py), called by the onboarding
    service (pass.<domain>/api/email-code)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.service_user = cls.env['res.users'].create({
            'name': "Onboarding service",
            'login': 'service-keycode@belfry.test',
            'group_ids': [(6, 0, [cls.env.ref('base.group_user').id, cls.env.ref('belfry_pass.group_admin').id])],
        })
        cls.Code = cls.env['belfry.key.code'].with_user(cls.service_user)

    def _code(self, email):
        """The code in the newest mail to ``email``."""
        mails = self.env['mail.mail'].sudo().search([('model', '=', 'belfry.key.code')], order='id desc')
        mail = mails.filtered(lambda m: email in (m.email_to or '') or email in m.recipient_ids.mapped('email'))[:1]
        self.assertTrue(mail, "a code was mailed to %s" % email)
        return re.search(r'(\d{6}) is your', mail.subject).group(1)

    def test_new_address_gets_a_contact_with_the_key(self):
        sent = self.Code.belfry_key_code_send('Dana@Example.org', KEY, 'Village demo')
        self.assertEqual(sent['state'], 'sent')
        code = self._code('Dana@Example.org')
        record = self.env['belfry.key.code'].search([('pubkey', '=', KEY)], limit=1)
        self.assertNotIn(code, record.code_hash, "only the hash is stored")
        mail = self.env['mail.mail'].sudo().search([('res_id', '=', record.id), ('model', '=', 'belfry.key.code')])
        self.assertIn('Village demo', mail.body_html)

        wrong = '000000' if code != '000000' else '000001'
        self.assertEqual(self.Code.belfry_key_code_verify('dana@example.org', KEY, wrong),
                         {'state': 'wrong_code', 'tries_left': 4})
        result = self.Code.belfry_key_code_verify('dana@example.org', KEY, code, 'Dana')
        self.assertEqual(result, {'state': 'bound', 'email': 'Dana@Example.org', 'npub': npub(KEY), 'member': False})
        partner = self.env['res.partner'].search([('email', '=', 'Dana@Example.org')])
        self.assertEqual((partner.name, partner.belfry_verified_pubkey), ('Dana', KEY))
        self.assertFalse(partner.belfry_npub, "not the participant's key until a pass is ordered with it")
        self.assertEqual(self.Code.belfry_key_code_verify('dana@example.org', KEY, code)['state'], 'expired',
                         "a code works once")

    def test_member_with_another_key_keeps_it(self):
        self.alice.belfry_npub = npub(OTHER)
        self.env['belfry.pass'].create({'partner_id': self.alice.id, 'tier': 'week', 'state': 'active'})
        self.Code.belfry_key_code_send(self.alice.email, KEY)
        result = self.Code.belfry_key_code_verify(self.alice.email, KEY, self._code(self.alice.email))
        self.assertEqual(result, {'state': 'other_key', 'email': self.alice.email, 'npub': npub(OTHER)})
        self.assertFalse(self.alice.belfry_verified_pubkey)

    def test_member_with_this_key(self):
        self.alice.belfry_npub = npub(KEY)
        self.env['belfry.pass'].create({'partner_id': self.alice.id, 'tier': 'week', 'state': 'active'})
        self.Code.belfry_key_code_send(self.alice.email, KEY)
        result = self.Code.belfry_key_code_verify(self.alice.email, KEY, self._code(self.alice.email))
        self.assertEqual((result['state'], result['member']), ('bound', True))

    def test_key_of_someone_else_is_refused(self):
        self.env['res.partner'].create({'name': "Erin", 'email': 'erin@example.org', 'belfry_verified_pubkey': KEY})
        self.Code.belfry_key_code_send('frank@example.org', KEY)
        result = self.Code.belfry_key_code_verify('frank@example.org', KEY, self._code('frank@example.org'))
        self.assertEqual(result, {'state': 'key_taken'})
        self.assertFalse(self.env['res.partner'].search([('email', '=', 'frank@example.org')]))

    def test_tries_expiry_and_limits(self):
        self.Code.belfry_key_code_send('gus@example.org', KEY)
        code = self._code('gus@example.org')
        wrong = '000000' if code != '000000' else '000001'
        for left in (4, 3, 2, 1, 0):
            self.assertEqual(self.Code.belfry_key_code_verify('gus@example.org', KEY, wrong)['tries_left'], left)
        self.assertEqual(self.Code.belfry_key_code_verify('gus@example.org', KEY, code)['state'], 'expired',
                         "five wrong tries end the code")
        # a new code replaces the previous one, and an old code past its time is refused
        self.Code.belfry_key_code_send('gus@example.org', KEY)
        first = self._code('gus@example.org')
        self.Code.belfry_key_code_send('gus@example.org', KEY)
        second = self._code('gus@example.org')
        if first != second:
            self.assertEqual(self.Code.belfry_key_code_verify('gus@example.org', KEY, first)['state'], 'wrong_code')
        self.env['belfry.key.code'].search([('email', '=', 'gus@example.org'), ('used_at', '=', False)]).write(
            {'expires_at': fields.Datetime.now()})
        self.assertEqual(self.Code.belfry_key_code_verify('gus@example.org', KEY, second)['state'], 'expired')
        # five codes per address in half an hour (this test has sent three)
        self.Code.belfry_key_code_send('gus@example.org', KEY)
        self.Code.belfry_key_code_send('gus@example.org', KEY)
        self.assertEqual(self.Code.belfry_key_code_send('gus@example.org', KEY)['state'], 'too_many')
        # a code for another key does not work for this one
        self.Code.belfry_key_code_send('hal@example.org', OTHER)
        self.assertEqual(self.Code.belfry_key_code_verify('hal@example.org', KEY, self._code('hal@example.org'))['state'],
                         'expired')

    def test_input_and_access(self):
        with self.assertRaises(UserError):
            self.Code.belfry_key_code_send('not-an-address', KEY)
        with self.assertRaises(UserError):
            self.Code.belfry_key_code_send('ok@example.org', 'npub1xyz')
        participant = self.env['res.users'].create({
            'name': "Someone", 'login': 'someone-keycode@belfry.test',
            'group_ids': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        with self.assertRaises(AccessError):
            self.env['belfry.key.code'].with_user(participant).belfry_key_code_send('ok@example.org', KEY)
