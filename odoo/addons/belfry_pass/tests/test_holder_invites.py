from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from .common import BelfryCase


@tagged('post_install', '-at_install', 'belfry')
class TestHolderInvitesAndStewardPass(BelfryCase):
    """Invites by e-mail for a pass holder (models/belfry_holder_invites.py) and the stewards'
    free pass (belfry_steward.py), both called by the onboarding service."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.service_user = cls.env['res.users'].create({
            'name': "Onboarding service",
            'login': 'service-holder@belfry.test',
            'group_ids': [(6, 0, [cls.env.ref('base.group_user').id, cls.env.ref('belfry_pass.group_admin').id])],
        })
        cls.env['belfry.pass'].create({'partner_id': cls.alice.id, 'tier': 'week', 'state': 'active'})
        cls.Invite = cls.env['belfry.invite'].with_user(cls.service_user)
        cls.Pass = cls.env['belfry.pass'].with_user(cls.service_user)

    def _mails_to(self, email):
        """The template's mails to ``email`` (Odoo may turn email_to into a recipient partner)."""
        mails = self.env['mail.mail'].sudo().search([('model', '=', 'belfry.invite')])
        return mails.filtered(lambda m: email in (m.email_to or '') or email in m.recipient_ids.mapped('email'))

    def test_participant_invites_by_mail(self):
        row = self.Invite.belfry_holder_invite(self.alice.id, 'carol@example.org', 'Carol', 'met at FOSDEM')
        self.assertEqual((row['state'], row['email'], row['name'], row['npub']),
                         ('open', 'carol@example.org', 'Carol', False))
        invite = self.env['belfry.invite'].browse(row['id'])
        # a participant's invite counts against their limit and names them
        self.assertEqual((invite.inviter_partner_id, invite.requested_by_partner_id), (self.alice, self.alice))
        self.assertTrue(invite.invite_mail_sent_at)
        mail = self._mails_to('carol@example.org')
        self.assertEqual(len(mail), 1)
        self.assertIn(invite.url, mail.body_html)
        self.assertIn(self.alice.name, mail.subject)
        # the same address again: the same invite, no second mail
        again = self.Invite.belfry_holder_invite(self.alice.id, 'Carol@Example.org')
        self.assertEqual(again['id'], row['id'])
        self.assertEqual(len(self._mails_to('carol@example.org')), 1)
        self.assertEqual([r['id'] for r in self.Invite.belfry_holder_invites(self.alice.id)], [row['id']])
        self.assertFalse(self.Invite.belfry_holder_invite_get(self.bob.id, row['id']))
        self.assertEqual(self.Invite.belfry_holder_invite_revoke(self.alice.id, row['id'])['state'], 'revoked')

    def test_participant_invite_as_a_link(self):
        mails_before = self.env['mail.mail'].sudo().search_count([('model', '=', 'belfry.invite')])
        row = self.Invite.belfry_holder_invite(self.alice.id, False, False, 'for my neighbour')
        self.assertEqual((row['state'], row['email'], row['note']), ('open', '', 'for my neighbour'))
        invite = self.env['belfry.invite'].browse(row['id'])
        self.assertIn(invite.token, row['url'])
        # nothing mailed; it counts against the limit like any invite and is listed
        self.assertFalse(invite.invite_mail_sent_at)
        self.assertEqual(self.env['mail.mail'].sudo().search_count([('model', '=', 'belfry.invite')]), mails_before)
        self.assertEqual((invite.inviter_partner_id, invite.requested_by_partner_id), (self.alice, self.alice))
        self.assertEqual([r['id'] for r in self.Invite.belfry_holder_invites(self.alice.id)], [row['id']])
        # each call is a new link
        self.assertNotEqual(self.Invite.belfry_holder_invite(self.alice.id, '')['id'], row['id'])

    def test_npub_once_the_pass_is_active_and_members_are_not_invited(self):
        row = self.Invite.belfry_holder_invite(self.alice.id, 'dan@example.org', 'Dan')
        dan = self.env['res.partner'].create({'name': "Dan", 'email': 'dan@example.org', 'belfry_npub': 'npub1dan'})
        dan_pass = self.env['belfry.pass'].create({'partner_id': dan.id, 'tier': 'day', 'state': 'pending'})
        self.env['belfry.invite'].browse(row['id']).write(
            {'state': 'reserved', 'pass_id': dan_pass.id, 'used_by_partner_id': dan.id})
        waiting = self.Invite.belfry_holder_invite_get(self.alice.id, row['id'])
        self.assertEqual((waiting['state'], waiting['pass_state'], waiting['npub']), ('reserved', 'pending', False))
        dan_pass.state = 'active'
        done = self.Invite.belfry_holder_invite_get(self.alice.id, row['id'])
        self.assertEqual((done['pass_state'], done['npub']), ('active', 'npub1dan'))
        member = self.Invite.belfry_holder_invite(self.alice.id, 'dan@example.org')
        self.assertEqual(member, {'state': 'member', 'email': 'dan@example.org', 'npub': 'npub1dan'})

    def test_limits_and_rights(self):
        self.env['ir.config_parameter'].sudo().set_param('belfry.invites_per_participant', '1')
        self.Invite.belfry_holder_invite(self.alice.id, 'e1@example.org')
        with self.assertRaises(UserError):
            self.Invite.belfry_holder_invite(self.alice.id, 'e2@example.org')
        # a steward's invites have no limit and do not use the participant's
        for n in range(3):
            row = self.Invite.belfry_holder_invite(self.alice.id, 's%d@example.org' % n, False, False,
                                                   'alice@example.org')
        invite = self.env['belfry.invite'].browse(row['id'])
        self.assertEqual((invite.steward_email, invite.inviter_partner_id.id), ('alice@example.org', False))
        self.assertEqual(invite.inviter_name, self.alice.name)
        with self.assertRaises(UserError):
            self.Invite.belfry_holder_invite(self.bob.id, 'x@example.org')  # no active pass
        with self.assertRaises(UserError):
            self.Invite.belfry_holder_invite(self.alice.id, 'not-an-address')
        with self.assertRaises(AccessError):
            self.env['belfry.invite'].with_user(self.steward_user).belfry_holder_invite(self.alice.id, 'y@example.org')

    def test_steward_pass_is_free_paid_and_given_once(self):
        result = self.Pass.belfry_steward_pass('sam.steward@belfry.test', "Sam Steward")
        self.assertTrue(result['created'])
        record = self.env['belfry.pass'].browse(result['pass_id'])
        self.assertEqual((record.state, record.tier, record.steward_pass), ('pending', 'full', True))
        self.assertTrue(record.manual_paid_by)
        invoice = record.invoice_id
        self.assertEqual((invoice.state, invoice.amount_total), ('posted', 0.0))
        self.assertIn(invoice.payment_state, ('paid', 'in_payment'))
        self.assertEqual(record.partner_id.email, 'sam.steward@belfry.test')
        again = self.Pass.belfry_steward_pass('Sam.Steward@belfry.test')
        self.assertEqual((again['pass_id'], again['created']), (record.id, False))
        with self.assertRaises(AccessError):
            self.env['belfry.pass'].with_user(self.steward_user).belfry_steward_pass('x@belfry.test')
