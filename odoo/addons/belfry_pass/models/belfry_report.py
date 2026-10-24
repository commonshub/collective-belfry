import logging
from html import escape

from odoo import api, fields, models
from odoo.exceptions import UserError

from .belfry_steward import _check_service_caller

_logger = logging.getLogger(__name__)

REASONS = [
    ('spam', "Spam"),
    ('harassment', "Harassment or hate"),
    ('illegal', "Illegal content"),
    ('scam', "Scam or fraud"),
    ('other', "Other"),
]
TARGET_KINDS = [
    ('profile', "Directory entry"),
    ('listing', "Offer"),
    ('photo', "Photo"),
    ('comment', "Comment"),
]


class BelfryReport(models.Model):
    """A participant's report of content in the community apps (directory, marketplace, photos).

    The village apps send it through the onboarding service, which calls belfry_report_create
    as the service user; every steward gets a mail. Stewards handle reports on the steward
    pages: hide the content (the apps stop showing it; it stays on the relay), dismiss, or revoke
    the pass. Only Belfry admins and stewards see reports, the reported person never learns who
    reported."""

    _name = 'belfry.report'
    _description = "Report of content in the community apps"
    _order = 'create_date desc, id desc'

    reporter_pubkey = fields.Char(string="Reporter key (hex)", required=True, readonly=True)
    reporter_pass_id = fields.Many2one('belfry.pass', string="Reporter's pass", readonly=True)
    reporter_partner_id = fields.Many2one(related='reporter_pass_id.partner_id', string="Reporter")
    app = fields.Selection(
        [('people', "Directory"), ('market', "Marketplace"), ('photos', "Photos")],
        required=True, readonly=True,
    )
    target_kind = fields.Selection(TARGET_KINDS, string="What", required=True, readonly=True)
    target_ref = fields.Char(
        string="Reference", required=True, readonly=True,
        help="Event id (photo, comment), the offer's coordinate kind:pubkey:d, or the pubkey "
             "for a directory entry. The village apps hide what is listed as hidden.",
    )
    target_pubkey = fields.Char(string="Author key (hex)", required=True, readonly=True)
    target_pass_id = fields.Many2one('belfry.pass', string="Author's pass", readonly=True)
    target_partner_id = fields.Many2one(related='target_pass_id.partner_id', string="Author")
    target_url = fields.Char(string="Link", readonly=True)
    target_excerpt = fields.Char(string="Excerpt", readonly=True)
    reason = fields.Selection(REASONS, required=True, readonly=True)
    note = fields.Text(string="Reporter's note", readonly=True)
    state = fields.Selection(
        [('open', "Open"), ('hidden', "Content hidden"), ('dismissed', "Dismissed")],
        default='open', required=True, readonly=True,
    )
    handled_by = fields.Char(string="Handled by", readonly=True)
    handled_at = fields.Datetime(string="Handled at", readonly=True)
    steward_note = fields.Text(string="Steward's note")

    def _pass_for_pubkey(self, pubkey):
        Pass = self.env['belfry.pass'].sudo()
        active = Pass.search([('keycast_pubkey', '=', pubkey), ('state', '=', 'active')], limit=1)
        return active or Pass.search([('keycast_pubkey', '=', pubkey)], order='id desc', limit=1)

    @api.model
    def belfry_report_create(self, vals, steward_emails=None):
        """Record a report (the onboarding service has checked the reporter's NIP-98 signature
        and active pass) and mail it to the stewards. Returns the report id."""
        _check_service_caller(self.env)
        allowed = {'reporter_pubkey', 'app', 'target_kind', 'target_ref', 'target_pubkey',
                   'target_url', 'target_excerpt', 'reason', 'note'}
        data = {k: v for k, v in (vals or {}).items() if k in allowed}
        for key in ('reporter_pubkey', 'target_pubkey', 'target_ref'):
            if not data.get(key):
                raise UserError("A report needs %s." % key)
        if data.get('note'):
            data['note'] = data['note'][:2000]
        if data.get('target_excerpt'):
            data['target_excerpt'] = data['target_excerpt'][:300]
        data['reporter_pass_id'] = self._pass_for_pubkey(data['reporter_pubkey']).id or False
        data['target_pass_id'] = self._pass_for_pubkey(data['target_pubkey']).id or False
        report = self.create(data)
        report._mail_stewards([e for e in (steward_emails or []) if e and '@' in e])
        _logger.info("Belfry report %s: %s %s by %s", report.id, report.app, report.reason,
                     report.reporter_pass_id.member_number or report.reporter_pubkey[:12])
        return report.id

    def _mail_stewards(self, emails):
        if not emails:
            return
        self.ensure_one()
        base = (self.env['ir.config_parameter'].sudo().get_param('belfry.public_url') or '').rstrip('/')
        company = self.env.company
        what = dict(TARGET_KINDS).get(self.target_kind)
        author = self.target_pass_id.member_number or "a participant without a pass"
        lines = [
            "<p>A participant reported a %s in the %s.</p>" % (
                escape(what.lower()), escape(dict(self._fields['app'].selection).get(self.app))),
            "<p><strong>Reason:</strong> %s<br/><strong>Author:</strong> %s</p>" % (
                escape(dict(REASONS).get(self.reason)), escape(author)),
        ]
        if self.target_excerpt:
            lines.append("<p><strong>Excerpt:</strong> %s</p>" % escape(self.target_excerpt))
        if self.note:
            lines.append("<p><strong>Note:</strong> %s</p>" % escape(self.note))
        if self.target_url:
            lines.append('<p><a href="%s">Open the content</a></p>' % escape(self.target_url))
        lines.append('<p>Handle it on the <a href="%s/steward#reports">steward pages</a> '
                     '(over the admin network).</p>' % escape(base))
        self.env['mail.mail'].sudo().create({
            'subject': "Open Source Village: new report (%s, %s)" % (
                dict(REASONS).get(self.reason), what),
            'email_from': company.email_formatted or company.email,
            'email_to': ', '.join(sorted(set(emails))),
            'body_html': ''.join(lines),
            'auto_delete': False,
        })

    def belfry_report_handle(self, action, steward_email, steward_note=False):
        """A steward's decision on the steward pages: hide the content or dismiss the report;
        reopen undoes either. Returns the new state."""
        _check_service_caller(self.env)
        self.ensure_one()
        states = {'hide': 'hidden', 'dismiss': 'dismissed', 'reopen': 'open'}
        if action not in states:
            raise UserError("Unknown action %s." % action)
        steward_email = (steward_email or '').strip()
        if not steward_email:
            raise UserError("A steward e-mail is required.")
        vals = {'state': states[action], 'handled_by': steward_email,
                'handled_at': fields.Datetime.now()}
        if steward_note:
            vals['steward_note'] = steward_note[:2000]
        self.write(vals)
        _logger.info("Belfry report %s: %s by %s", self.id, states[action], steward_email)
        return self.state

    @api.model
    def belfry_hidden_refs(self):
        """What the village apps must not show: the references of every report whose content a
        steward hid (event ids, offer coordinates, pubkeys of directory entries)."""
        _check_service_caller(self.env)
        return sorted(set(self.search([('state', '=', 'hidden')]).mapped('target_ref')))
