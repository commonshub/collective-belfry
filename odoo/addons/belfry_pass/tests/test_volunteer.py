from datetime import timedelta

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import BelfryCase


@tagged('post_install', '-at_install', 'belfry')
class TestVolunteer(BelfryCase):
    """Volunteers earn a pass with shifts (decision 2026-10-26)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.service_user = cls.env['res.users'].create({
            'name': "Onboarding service",
            'login': 'service-vol@belfry.test',
            'group_ids': [(6, 0, [cls.env.ref('base.group_user').id, cls.env.ref('belfry_pass.group_admin').id])],
        })
        cls.Shift = cls.env['belfry.shift'].with_user(cls.service_user)
        start = fields.Datetime.now() + timedelta(days=10)
        cls.shifts = [cls.Shift.browse(cls.Shift.belfry_steward_shift_create({
            'name': "Bar %d" % i, 'start': start + timedelta(hours=4 * i),
            'end': start + timedelta(hours=4 * i + 3), 'capacity': 2, 'place': "Main hall",
        })) for i in range(4)]

    def _volunteer(self, name="Vic", email="vic@example.org"):
        pass_id = self.env['belfry.pass'].with_user(self.service_user).belfry_steward_volunteer_add(name, email)
        return self.env['belfry.pass'].browse(pass_id)

    def test_application_accepted_as_volunteer(self):
        Application = self.env['belfry.application'].with_user(self.service_user)
        application = Application.browse(Application.belfry_application_create(
            {'name': "Vic", 'email': 'vic@example.org', 'motivation': "Happy to help", 'kind': 'volunteer'}))
        pass_id = application.belfry_application_volunteer('steward@belfry.test')
        belfry_pass = self.env['belfry.pass'].browse(pass_id)
        self.assertEqual((belfry_pass.tier, belfry_pass.state), ('volunteer', 'pending'))
        self.assertEqual(application.state, 'invited')
        mail = self.env['mail.mail'].search([('email_to', '=', 'vic@example.org'),
                                             ('subject', 'ilike', 'shifts')])
        self.assertIn(belfry_pass.belfry_shifts_url(), mail.body_html)
        info = self.Shift.belfry_volunteer_by_token(belfry_pass.volunteer_token)
        self.assertEqual((info['pass_id'], info['count'], info['min_shifts']), (pass_id, 0, 3))
        self.assertFalse(self.Shift.belfry_volunteer_by_token('x' * 43))

    def test_third_shift_makes_the_pass_ready(self):
        belfry_pass = self._volunteer()
        partner = belfry_pass.partner_id.id
        for i, shift in enumerate(self.shifts[:3]):
            info = self.Shift.belfry_shift_signup(partner, shift.id)
            self.assertEqual(info['count'], i + 1)
            self.assertEqual(info['ready'], i == 2)
        board = {row['id']: row for row in self.Shift.belfry_shift_board(partner)}
        self.assertTrue(board[self.shifts[0].id]['mine'])
        self.assertEqual(board[self.shifts[0].id]['people'], ["Vic"])
        self.assertFalse(board[self.shifts[3].id]['mine'])

    def test_full_twice_and_overlap_refused(self):
        a, b, c = self._volunteer("A", "a@example.org"), self._volunteer("B", "b@example.org"), \
            self._volunteer("C", "c@example.org")
        shift = self.shifts[0]
        self.Shift.belfry_shift_signup(a.partner_id.id, shift.id)
        with self.assertRaises(UserError):
            self.Shift.belfry_shift_signup(a.partner_id.id, shift.id)
        self.Shift.belfry_shift_signup(b.partner_id.id, shift.id)
        with self.assertRaisesRegex(UserError, "full"):
            self.Shift.belfry_shift_signup(c.partner_id.id, shift.id)
        overlapping = self.Shift.browse(self.Shift.belfry_steward_shift_create({
            'name': "Door", 'start': shift.start + timedelta(hours=1), 'end': shift.end + timedelta(hours=1)}))
        with self.assertRaisesRegex(UserError, "another shift"):
            self.Shift.belfry_shift_signup(a.partner_id.id, overlapping.id)
        with self.assertRaisesRegex(UserError, "Only volunteers"):
            self.Shift.belfry_shift_signup(self.alice.id, self.shifts[1].id)

    def test_leave_and_the_minimum(self):
        belfry_pass = self._volunteer()
        partner = belfry_pass.partner_id.id
        for shift in self.shifts[:3]:
            self.Shift.belfry_shift_signup(partner, shift.id)
        belfry_pass.write({'state': 'active'})
        info = self.Shift.belfry_shift_leave(partner, self.shifts[0].id, ['steward@belfry.test'])
        self.assertEqual(info['count'], 2)
        self.assertTrue(info['below_minimum'])
        self.assertTrue(self.env['mail.mail'].search([('email_to', '=', 'steward@belfry.test'),
                                                      ('subject', 'ilike', 'left a shift')]))
        soon = self.Shift.browse(self.Shift.belfry_steward_shift_create({
            'name': "Soon", 'start': fields.Datetime.now() + timedelta(hours=5),
            'end': fields.Datetime.now() + timedelta(hours=8)}))
        self.Shift.belfry_shift_signup(partner, soon.id)
        with self.assertRaisesRegex(UserError, "less than 48 hours"):
            self.Shift.belfry_shift_leave(partner, soon.id)

    def test_cancelled_shift_frees_and_tells_the_people(self):
        belfry_pass = self._volunteer()
        self.Shift.belfry_shift_signup(belfry_pass.partner_id.id, self.shifts[0].id)
        self.shifts[0].with_user(self.service_user).belfry_steward_shift_cancel()
        self.assertFalse(self.shifts[0].active)
        self.assertNotIn(self.shifts[0].id, [r['id'] for r in self.Shift.belfry_shift_board()])
        self.assertEqual(self.Shift.belfry_volunteer_of_partner(belfry_pass.partner_id.id)['count'], 0)
        self.assertTrue(self.env['mail.mail'].search([('email_to', '=', 'vic@example.org'),
                                                      ('subject', 'ilike', 'cancelled')]))
