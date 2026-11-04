from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from .common import BelfryCase


@tagged('post_install', '-at_install', 'belfry')
class TestFreeDayPass(BelfryCase):
    """Free day passes for week-pass holders (models/belfry_day_pass.py), called by the
    onboarding service from the pass page."""

    TODAY = '2027-01-26'

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.service_user = cls.env['res.users'].create({
            'name': "Onboarding service",
            'login': 'service-daypass@belfry.test',
            'group_ids': [(6, 0, [cls.env.ref('base.group_user').id, cls.env.ref('belfry_pass.group_admin').id])],
        })
        params = cls.env['ir.config_parameter'].sudo()
        params.set_param('belfry.event_start', '2027-01-25')
        params.set_param('belfry.event_end', '2027-01-31')
        cls.week = cls.env['belfry.pass'].create(
            {'partner_id': cls.alice.id, 'tier': 'week', 'state': 'active'})
        cls.Pass = cls.env['belfry.pass'].with_user(cls.service_user)

    def _days(self, partner=None):
        info = self.Pass.belfry_free_day_passes((partner or self.alice).id, self.TODAY)
        return info, {d['date']: d for d in info['days']}

    def test_week_pass_holder_takes_a_day_once(self):
        info, days = self._days()
        self.assertTrue(info['eligible'])
        self.assertEqual(info['week_pass_id'], self.week.id)
        self.assertEqual(len(days), 7)
        self.assertEqual(days['2027-01-25']['state'], 'past')
        self.assertEqual(days['2027-01-27']['state'], 'available')

        pass_id = self.Pass.belfry_free_day_pass_create(self.alice.id, '2027-01-27', self.TODAY)
        day_pass = self.env['belfry.pass'].browse(pass_id)
        self.assertEqual((day_pass.tier, day_pass.state), ('day', 'pending'))
        self.assertEqual((str(day_pass.date_start), str(day_pass.date_end)), ('2027-01-27', '2027-01-27'))
        self.assertEqual(day_pass.free_of_pass_id, self.week)
        self.assertFalse(day_pass.invoice_id)

        _info, days = self._days()
        self.assertEqual((days['2027-01-27']['state'], days['2027-01-27']['pass_id']), ('taken', pass_id))
        with self.assertRaisesRegex(UserError, "already"):
            self.Pass.belfry_free_day_pass_create(self.alice.id, '2027-01-27', self.TODAY)
        # another day is fine: one per event day
        self.Pass.belfry_free_day_pass_create(self.alice.id, '2027-01-28', self.TODAY)

    def test_a_revoked_day_pass_gives_the_day_back(self):
        pass_id = self.Pass.belfry_free_day_pass_create(self.alice.id, '2027-01-29', self.TODAY)
        self.env['belfry.pass'].browse(pass_id).state = 'revoked'
        _info, days = self._days()
        self.assertEqual(days['2027-01-29']['state'], 'available')

    def test_refused_days(self):
        for day, message in [('2027-01-25', "over"), ('2027-02-01', "day of the event"),
                             ('not-a-date', "day of the event"), (False, "day of the event")]:
            with self.assertRaisesRegex(UserError, message):
                self.Pass.belfry_free_day_pass_create(self.alice.id, day, self.TODAY)

    def test_only_with_an_active_week_pass(self):
        bob = self.env['res.partner'].create({'name': "Bob", 'email': 'bob@example.org'})
        self.env['belfry.pass'].create({'partner_id': bob.id, 'tier': 'day', 'state': 'active'})
        self.assertEqual(self.Pass.belfry_free_day_passes(bob.id, self.TODAY), {'eligible': False, 'days': []})
        with self.assertRaisesRegex(UserError, "week pass"):
            self.Pass.belfry_free_day_pass_create(bob.id, '2027-01-27', self.TODAY)
        self.week.state = 'revoked'
        self.assertFalse(self.Pass.belfry_free_day_passes(self.alice.id, self.TODAY)['eligible'])

    def test_service_user_only(self):
        participant = self.env['res.users'].create({
            'name': "Someone", 'login': 'someone-daypass@belfry.test',
            'group_ids': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        with self.assertRaises(AccessError):
            self.env['belfry.pass'].with_user(participant).belfry_free_day_passes(self.alice.id)
