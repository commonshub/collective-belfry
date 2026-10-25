import base64

from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from .common import BelfryCase


@tagged('post_install', '-at_install', 'belfry')
class TestFriendsAndFeedback(BelfryCase):
    """Invites from participants (models/belfry_friends.py) and feedback (belfry_feedback.py),
    both called by the onboarding service."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.service_user = cls.env['res.users'].create({
            'name': "Onboarding service",
            'login': 'service-friends@belfry.test',
            'group_ids': [(6, 0, [cls.env.ref('base.group_user').id, cls.env.ref('belfry_pass.group_admin').id])],
        })
        cls.env['belfry.pass'].create({'partner_id': cls.alice.id, 'tier': 'week', 'state': 'active'})
        cls.Invite = cls.env['belfry.invite'].with_user(cls.service_user)
        cls.Feedback = cls.env['belfry.feedback'].with_user(cls.service_user)

    def test_participant_invites_up_to_the_limit(self):
        self.env['ir.config_parameter'].sudo().set_param('belfry.invites_per_participant', '2')
        first = self.Invite.belfry_participant_create(self.alice.id, "for Carla")
        self.assertEqual(first['state'], 'open')
        self.assertIn('/belfry/invite/', first['url'])
        invite = self.env['belfry.invite'].browse(first['id'])
        self.assertEqual((invite.inviter_partner_id, invite.inviter_name), (self.alice, self.alice.name))
        self.Invite.belfry_participant_create(self.alice.id)
        listing = self.Invite.belfry_participant_invites(self.alice.id)
        self.assertEqual((listing['limit'], listing['left'], len(listing['invites'])), (2, 0, 2))
        with self.assertRaises(UserError):
            self.Invite.belfry_participant_create(self.alice.id)
        # a revoked invite gives its slot back
        self.assertEqual(self.Invite.belfry_participant_revoke(self.alice.id, first['id'])['state'], 'revoked')
        self.assertEqual(self.Invite.belfry_participant_invites(self.alice.id)['left'], 1)

    def test_only_active_participants_and_own_invites(self):
        with self.assertRaises(UserError):
            self.Invite.belfry_participant_create(self.bob.id)
        mine = self.Invite.belfry_participant_create(self.alice.id)
        with self.assertRaises(UserError):
            self.Invite.belfry_participant_revoke(self.bob.id, mine['id'])
        with self.assertRaises(AccessError):
            self.env['belfry.invite'].with_user(self.steward_user).belfry_participant_create(self.alice.id)

    def test_steward_invite_names_the_steward(self):
        row = self.Invite.belfry_steward_create('sam@belfry.test', "for Dan", "Sam Steward")
        self.assertEqual(self.env['belfry.invite'].browse(row['id']).inviter_name, "Sam Steward")

    def test_feedback_is_stored_and_mailed_with_the_screenshot(self):
        png = base64.b64encode(b'\x89PNG\r\n\x1a\n' + b'0' * 100).decode()
        fid = self.Feedback.belfry_feedback_create(
            {'partner_id': self.alice.id, 'page_url': 'https://photos.example/#/new',
             'happened': "Upload hangs", 'expected': "the photo appears", 'state': 'done'},
            ['steward@belfry.test'], png, 'image/png')
        feedback = self.env['belfry.feedback'].browse(fid)
        self.assertEqual((feedback.state, feedback.partner_id), ('new', self.alice))
        self.assertEqual(feedback.screenshot_name, 'screenshot.png')
        mail = self.env['mail.mail'].search([('subject', 'like', 'feedback from')], order='id desc', limit=1)
        self.assertIn('Upload hangs', mail.body_html)
        self.assertEqual(mail.attachment_ids.mapped('name'), ['screenshot.png'])
        self.assertEqual(feedback.belfry_feedback_handle('done', 'sam@belfry.test'), 'done')

    def test_feedback_needs_text_and_a_small_image(self):
        with self.assertRaises(UserError):
            self.Feedback.belfry_feedback_create({'happened': '  '})
        with self.assertRaises(UserError):
            self.Feedback.belfry_feedback_create({'happened': 'x'}, [], base64.b64encode(b'x').decode(), 'image/gif')
        fid = self.Feedback.belfry_feedback_create({'happened': 'x', 'email': 'tester@example.org'})
        self.assertEqual(self.env['belfry.feedback'].browse(fid).email, 'tester@example.org')
