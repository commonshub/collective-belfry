import base64
import logging
from html import escape

from odoo import api, fields, models
from odoo.exceptions import UserError

from .belfry_steward import _check_service_caller

_logger = logging.getLogger(__name__)

SCREENSHOT_TYPES = {'image/png': 'png', 'image/jpeg': 'jpg', 'image/webp': 'webp'}
SCREENSHOT_MAX_BYTES = 5 * 1024 * 1024


class BelfryFeedback(models.Model):
    """Feedback from participants and testers (pass.<domain>/feedback): what happened, what they
    expected, the page they came from and an optional screenshot. The onboarding service creates
    it as the service user and every steward gets a mail; stewards read it on the steward pages."""

    _name = 'belfry.feedback'
    _description = "Feedback on Open Source Village's apps and pages"
    _order = 'create_date desc, id desc'

    partner_id = fields.Many2one('res.partner', string="From", readonly=True, index=True)
    email = fields.Char(readonly=True, help="Given on the form by someone not signed in.")
    page_url = fields.Char(string="Page", readonly=True)
    happened = fields.Text(string="What happened", required=True, readonly=True)
    expected = fields.Text(string="What they expected", readonly=True)
    user_agent = fields.Char(readonly=True)
    screenshot = fields.Binary(attachment=True, readonly=True)
    screenshot_name = fields.Char(readonly=True)
    state = fields.Selection([('new', "New"), ('done', "Done")], default='new', required=True, readonly=True)
    handled_by = fields.Char(readonly=True)
    handled_at = fields.Datetime(readonly=True)

    @api.model
    def belfry_feedback_create(self, vals, steward_emails=None, screenshot_b64=False, screenshot_type=False):
        """Store feedback and mail it to the stewards. Returns the id."""
        _check_service_caller(self.env)
        allowed = {'partner_id', 'email', 'page_url', 'happened', 'expected', 'user_agent'}
        data = {k: v for k, v in (vals or {}).items() if k in allowed and v}
        if not (data.get('happened') or '').strip():
            raise UserError("Feedback needs a description.")
        for key, limit in (('happened', 4000), ('expected', 4000), ('page_url', 500),
                           ('user_agent', 300), ('email', 200)):
            if data.get(key):
                data[key] = str(data[key])[:limit]
        if data.get('partner_id'):
            data['partner_id'] = self.env['res.partner'].sudo().browse(int(data['partner_id'])).exists().id or False
        if screenshot_b64:
            ext = SCREENSHOT_TYPES.get(screenshot_type)
            raw = base64.b64decode(screenshot_b64, validate=True)
            if not ext or len(raw) > SCREENSHOT_MAX_BYTES:
                raise UserError("The screenshot must be a PNG, JPEG or WebP image of at most 5 MB.")
            data['screenshot'] = screenshot_b64
            data['screenshot_name'] = "screenshot.%s" % ext
        feedback = self.sudo().create(data)
        feedback._mail_stewards([e for e in (steward_emails or []) if e and '@' in e])
        _logger.info("Belfry feedback %s from %s", feedback.id,
                     feedback.partner_id.id or ('e-mail' if feedback.email else 'anonymous'))
        return feedback.id

    def _mail_stewards(self, emails):
        if not emails:
            return
        self.ensure_one()
        base = (self.env['ir.config_parameter'].sudo().get_param('belfry.public_url') or '').rstrip('/')
        company = self.env.company
        who = self.partner_id.name or self.email or "someone not signed in"
        lines = ["<p><strong>From:</strong> %s</p>" % escape(who)]
        if self.page_url:
            lines.append('<p><strong>Page:</strong> <a href="%s">%s</a></p>' % (escape(self.page_url), escape(self.page_url)))
        lines.append("<p><strong>What happened:</strong><br/>%s</p>" % escape(self.happened).replace("\n", "<br/>"))
        if self.expected:
            lines.append("<p><strong>Expected:</strong><br/>%s</p>" % escape(self.expected).replace("\n", "<br/>"))
        lines.append('<p>All feedback is on the <a href="%s/steward#feedback">steward pages</a> '
                     '(over the admin network).</p>' % escape(base))
        mail = self.env['mail.mail'].sudo().create({
            'subject': "Open Source Village: feedback from %s" % who,
            'email_from': company.email_formatted or company.email,
            'email_to': ', '.join(sorted(set(emails))),
            'body_html': ''.join(lines),
            'auto_delete': False,
        })
        if self.screenshot:
            copy = self.env['ir.attachment'].sudo().create({
                'name': self.screenshot_name or 'screenshot', 'datas': self.screenshot,
                'res_model': 'mail.mail', 'res_id': mail.id,
            })
            mail.attachment_ids = [(4, copy.id)]

    def belfry_feedback_handle(self, action, steward_email):
        """A steward marks feedback done or new again."""
        _check_service_caller(self.env)
        self.ensure_one()
        states = {'done': 'done', 'reopen': 'new'}
        if action not in states or not (steward_email or '').strip():
            raise UserError("Unknown action or missing steward.")
        self.write({'state': states[action], 'handled_by': steward_email.strip(),
                    'handled_at': fields.Datetime.now()})
        return self.state
