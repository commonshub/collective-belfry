from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from .common import BelfryCase


@tagged('post_install', '-at_install', 'belfry')
class TestStewardMethods(BelfryCase):
    """The methods behind the steward pages (onboarding service, /steward)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.service_user = cls.env['res.users'].create({
            'name': "Onboarding service",
            'login': 'service@belfry.test',
            'group_ids': [(6, 0, [cls.env.ref('base.group_user').id, cls.env.ref('belfry_pass.group_admin').id])],
        })

    def _pending_pass(self):
        _order, invoice = self._make_posted_invoice(self.alice, self.product_week)
        return self.env['belfry.pass'].create({
            'partner_id': self.alice.id, 'tier': 'week', 'invoice_id': invoice.id, 'state': 'pending',
        })

    def test_create_invite_for_steward(self):
        Invite = self.env['belfry.invite'].with_user(self.service_user)
        known = Invite.belfry_steward_create('Steward@belfry.test', "for carol")
        invite = self.env['belfry.invite'].browse(known['id'])
        self.assertEqual(invite.steward_id, self.steward_user, "matched by login, case-insensitive")
        self.assertEqual(invite.steward_email, 'Steward@belfry.test')
        self.assertEqual(invite.note, "for carol")
        self.assertTrue(known['url'].endswith('/belfry/invite/%s' % known['token']))
        unknown = self.env['belfry.invite'].browse(Invite.belfry_steward_create('nobody@belfry.test')['id'])
        self.assertEqual(unknown.steward_id, self.service_user, "no Odoo user: the service user")
        self.assertEqual(unknown.steward_email, 'nobody@belfry.test')

    def test_only_admins_may_act_for_stewards(self):
        with self.assertRaises(AccessError):
            self.env['belfry.invite'].with_user(self.steward_user).belfry_steward_create('x@belfry.test')
        belfry_pass = self._pending_pass()
        with self.assertRaises(AccessError):
            belfry_pass.with_user(self.steward_user).belfry_steward_mark_paid('x@belfry.test')

    def test_mark_paid(self):
        belfry_pass = self._pending_pass()
        state = belfry_pass.with_user(self.service_user).belfry_steward_mark_paid('steward@belfry.test')
        self.assertIn(state, ('paid', 'in_payment'))
        self.assertEqual(belfry_pass.manual_paid_by, 'steward@belfry.test')
        self.assertTrue(belfry_pass.manual_paid_at)
        self.assertIn(belfry_pass.invoice_id.payment_state, ('paid', 'in_payment'))
        with self.assertRaises(UserError):
            belfry_pass.with_user(self.service_user).belfry_steward_mark_paid('steward@belfry.test')

    def test_mark_paid_needs_pending_pass(self):
        belfry_pass = self._pending_pass()
        belfry_pass.write({'state': 'revoked'})
        with self.assertRaises(UserError):
            belfry_pass.with_user(self.service_user).belfry_steward_mark_paid('steward@belfry.test')
