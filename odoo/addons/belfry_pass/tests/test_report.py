from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from .common import BelfryCase

REPORTER = 'aa' * 32
AUTHOR = 'bb' * 32


@tagged('post_install', '-at_install', 'belfry')
class TestReports(BelfryCase):
    """Reports from the community apps: created by the onboarding service, mailed to the
    stewards, handled on the steward pages, hidden content listed for the apps."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.service_user = cls.env['res.users'].create({
            'name': "Onboarding service",
            'login': 'service@belfry.test',
            'group_ids': [(6, 0, [cls.env.ref('base.group_user').id, cls.env.ref('belfry_pass.group_admin').id])],
        })
        cls.bob = cls.env['res.partner'].create({'name': "Bob Author", 'email': 'bob@example.org'})
        cls.reporter_pass = cls.env['belfry.pass'].create({
            'partner_id': cls.alice.id, 'tier': 'week', 'state': 'active', 'keycast_pubkey': REPORTER})
        cls.author_pass = cls.env['belfry.pass'].create({
            'partner_id': cls.bob.id, 'tier': 'day', 'state': 'active', 'keycast_pubkey': AUTHOR})
        cls.Report = cls.env['belfry.report'].with_user(cls.service_user)

    def _vals(self, **kw):
        return {
            'reporter_pubkey': REPORTER, 'app': 'photos', 'target_kind': 'photo',
            'target_ref': 'cc' * 32, 'target_pubkey': AUTHOR, 'reason': 'spam',
            'note': "selling crypto", 'target_url': 'https://photos.example/#/p/' + 'cc' * 32,
            'target_excerpt': "Buy now", **kw,
        }

    def test_create_links_passes_and_mails_stewards(self):
        report_id = self.Report.belfry_report_create(
            self._vals(), ['steward@belfry.test', 'second@belfry.test', 'not-an-address'])
        report = self.env['belfry.report'].browse(report_id)
        self.assertEqual(report.state, 'open')
        self.assertEqual(report.reporter_pass_id, self.reporter_pass)
        self.assertEqual(report.target_partner_id, self.bob)
        mail = self.env['mail.mail'].search([('subject', 'like', 'new report')], order='id desc', limit=1)
        self.assertEqual(mail.email_to, 'second@belfry.test, steward@belfry.test')
        self.assertIn('Buy now', mail.body_html)
        self.assertIn('/steward#reports', mail.body_html)
        self.assertNotIn('Alice', mail.body_html, "the mail names no reporter")

    def test_unknown_fields_are_ignored_and_keys_required(self):
        report_id = self.Report.belfry_report_create(self._vals(state='hidden', handled_by='x'))
        self.assertEqual(self.env['belfry.report'].browse(report_id).state, 'open')
        with self.assertRaises(UserError):
            self.Report.belfry_report_create(self._vals(target_ref=''))

    def test_handle_and_hidden_refs(self):
        hidden = self.Report.browse(self.Report.belfry_report_create(self._vals()))
        dismissed = self.Report.browse(self.Report.belfry_report_create(self._vals(target_ref='dd' * 32)))
        self.assertEqual(hidden.belfry_report_handle('hide', 'steward@belfry.test', "off-topic"), 'hidden')
        self.assertEqual(dismissed.belfry_report_handle('dismiss', 'steward@belfry.test'), 'dismissed')
        self.assertEqual(hidden.handled_by, 'steward@belfry.test')
        self.assertEqual(self.Report.belfry_hidden_refs(), ['cc' * 32])
        hidden.belfry_report_handle('reopen', 'steward@belfry.test')
        self.assertEqual(self.Report.belfry_hidden_refs(), [])
        with self.assertRaises(UserError):
            hidden.belfry_report_handle('delete', 'steward@belfry.test')

    def test_only_the_service_user(self):
        with self.assertRaises(AccessError):
            self.env['belfry.report'].with_user(self.steward_user).belfry_report_create(self._vals())
