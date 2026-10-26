from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from .common import BelfryCase


@tagged('post_install', '-at_install', 'belfry')
class TestApplication(BelfryCase):
    """Applications from <domain>/apply, answered on the steward pages."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.service_user = cls.env['res.users'].create({
            'name': "Onboarding service",
            'login': 'service-apply@belfry.test',
            'group_ids': [(6, 0, [cls.env.ref('base.group_user').id, cls.env.ref('belfry_pass.group_admin').id])],
        })
        cls.Application = cls.env['belfry.application'].with_user(cls.service_user)

    def _mails_to(self, address):
        return self.env['mail.mail'].search([('email_to', 'ilike', address)])

    def test_apply_confirms_and_tells_the_stewards(self):
        app_id = self.Application.belfry_application_create(
            {'name': "Carol", 'email': 'carol@example.org', 'motivation': "I build mesh networks.",
             'kind': 'volunteer'}, ['steward@belfry.test'])
        application = self.env['belfry.application'].browse(app_id)
        self.assertEqual((application.state, application.kind), ('new', 'volunteer'))
        self.assertIn("received your application", self._mails_to('carol@example.org').subject)
        to_stewards = self._mails_to('steward@belfry.test')
        self.assertIn("Carol", to_stewards.subject)
        self.assertIn("mesh networks", to_stewards.body_html)

    def test_form_needs_name_motivation_and_email(self):
        for vals in ({'name': "", 'email': 'a@b.c', 'motivation': "x"},
                     {'name': "A", 'email': 'not-an-address', 'motivation': "x"},
                     {'name': "A", 'email': 'a@b.c', 'motivation': "  "}):
            with self.assertRaises(UserError):
                self.Application.belfry_application_create(vals)

    def test_only_the_service_user(self):
        with self.assertRaises(AccessError):
            self.env['belfry.application'].with_user(self.steward_user).belfry_application_create(
                {'name': "A", 'email': 'a@b.c', 'motivation': "x"})

    def test_invite_mails_the_link_once(self):
        application = self.Application.browse(self.Application.belfry_application_create(
            {'name': "Dan", 'email': 'dan@example.org', 'motivation': "Curious."}))
        created = application.belfry_application_invite('steward@belfry.test', "Steward Sam")
        self.assertEqual(application.state, 'invited')
        self.assertEqual(application.invite_id.id, created['id'])
        self.assertEqual(application.invite_id.inviter_name, "Steward Sam")
        invited = self._mails_to('dan@example.org').filtered(lambda m: 'invited' in m.subject)
        self.assertIn(created['url'], invited.body_html)
        with self.assertRaises(UserError):
            application.belfry_application_decline('steward@belfry.test')

    def test_decline_and_duplicates(self):
        first = self.Application.browse(self.Application.belfry_application_create(
            {'name': "Eve", 'email': 'eve@example.org', 'motivation': "Hi."}))
        second = self.Application.browse(self.Application.belfry_application_create(
            {'name': "Eve", 'email': 'EVE@example.org', 'motivation': "Hi again."}))
        self.assertEqual(second.duplicate_note, "1 other application")
        first.belfry_application_decline('steward@belfry.test')
        self.assertEqual(first.state, 'declined')
        self.assertTrue(self._mails_to('eve@example.org').filtered(lambda m: 'cannot offer' in m.body_html))
